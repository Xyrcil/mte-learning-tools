"""Independent, schema-pinned formation state for the offline Test account.

The bundled runtime has two formation invariants that are easy to miss when
returning a conservative protobuf template:

* ``BattleData.InitPhalanxData`` indexes five ``PhalanxList`` entries and five
  ``PhaAutoFightList`` entries before checking either repeated-field length.
* ``RoleCount`` is taken from the first formation and is then used for every
  formation and auto-battle array.  The all-unlocked profile therefore needs
  the same seven owned hero UIDs in every slot, including Lillia's stable UID
  ``1000000007``.  ``CombatPos`` selects the at-most-three deployed heroes;
  membership in ``HeroList`` is the candidate roster, not deployment.

Handlers in this module are pure.  Mutating requests return one top-level
``changes`` mapping which the public server must shallow-merge under its
per-account lock and persist before sending the response.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping, MutableMapping, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

try:  # Package imports in tests; fallback supports ``python server/server.py``.
    from .formation_names import (
        CLIENT_FORMATION_NAME_WEIGHT_LIMIT,
        formation_name_weight,
        is_client_valid_formation_name,
        normalize_stored_formation_name,
    )
    from .protobuf_codec import ProtocolMessageCodec
except ImportError:  # pragma: no cover - direct server execution compatibility
    from formation_names import (  # type: ignore
        CLIENT_FORMATION_NAME_WEIGHT_LIMIT,
        formation_name_weight,
        is_client_valid_formation_name,
        normalize_stored_formation_name,
    )
    from protobuf_codec import ProtocolMessageCodec  # type: ignore


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROTOCOL_CATALOG = ROOT / "generated" / "protocol" / "protocol_catalog.json"
DEFAULT_HANDLER_DEPENDENCIES = (
    ROOT / "generated" / "protocol" / "handler_dependencies.json"
)

PHALANX_COUNT = 5
HERO_COUNT = 7
ACTIVE_COMBAT_POSITIONS = frozenset({0, 1, 2})
BENCH_COMBAT_POSITION = -1
LILIA_HERO_UID = 1_000_000_007
# Backward-compatible export name.  The runtime limit is a weighted UTF-16
# length, not Python's character count; use ``formation_name_weight`` when
# validating values.
MAX_FORMATION_NAME_LENGTH = CLIENT_FORMATION_NAME_WEIGHT_LIMIT
MAX_EQUIPPED_GOSPELS = 3
FORMATION_STATE_SCHEMA_VERSION = 1
FORMATION_STATE_KEY = "formation_state"

UINT32_MAX = (1 << 32) - 1
UINT64_MAX = (1 << 64) - 1

SC_PHALANX_DATAS = 16
CS_START_BATTLE = 19
SC_START_BATTLE_RET = 20
CS_SWITCH_PHALANX = 43
SC_SWITCH_PHALANX_RET = 44
CS_PHALANX_DATAS = 49
CS_SAVE_PHALANX = 82
SC_SAVE_PHALANX_RET = 83
CS_CHANGE_PHALANX_NAME = 214
SC_CHANGE_PHALANX_NAME_RET = 215
CS_SAVE_ALL_AUTO_PHALANX = 1366
SC_SAVE_ALL_AUTO_PHALANX_RET = 1367

RESPONSE_IDS = {
    CS_SWITCH_PHALANX: SC_SWITCH_PHALANX_RET,
    CS_PHALANX_DATAS: SC_PHALANX_DATAS,
    CS_SAVE_PHALANX: SC_SAVE_PHALANX_RET,
    CS_CHANGE_PHALANX_NAME: SC_CHANGE_PHALANX_NAME_RET,
    CS_SAVE_ALL_AUTO_PHALANX: SC_SAVE_ALL_AUTO_PHALANX_RET,
}
REQUEST_IDS = frozenset(RESPONSE_IDS)

EXPECTED_COMMANDS = {
    SC_PHALANX_DATAS: "SC_PhalanxDatas",
    CS_START_BATTLE: "CS_StartBattle",
    SC_START_BATTLE_RET: "SC_StartBattleRet",
    CS_SWITCH_PHALANX: "CS_SwitchPhalanx",
    SC_SWITCH_PHALANX_RET: "SC_SwitchPhalanxRet",
    CS_PHALANX_DATAS: "CS_PhalanxDatas",
    CS_SAVE_PHALANX: "CS_SavePhalanx",
    SC_SAVE_PHALANX_RET: "SC_SavePhalanxRet",
    CS_CHANGE_PHALANX_NAME: "CS_ChangePhalanxName",
    SC_CHANGE_PHALANX_NAME_RET: "SC_ChangePhalanxNameRet",
    CS_SAVE_ALL_AUTO_PHALANX: "CS_SaveAllAutoPhalanx",
    SC_SAVE_ALL_AUTO_PHALANX_RET: "SC_SaveAllAutoPhalanxRet",
}

EXPECTED_PROTOCOL_FIELDS = {
    "BattleHero": {1: "HeroUId", 2: "EquipedGospelList", 3: "CombatPos"},
    "EquipedGospel": {1: "GospelUId", 2: "AppearanceId"},
    "Phalanx": {1: "HeroList", 2: "Name", 3: "CombatPower"},
    "AutoFightUnitInfo": {1: "SkillInfo", 2: "UseEnergy", 3: "Weight"},
    "AutoFightInfGos": {
        1: "EquipHeroID",
        2: "UId",
        3: "Pos",
        4: "UnitList",
    },
    "AutoFightInfoHero": {1: "UId", 2: "Pos", 3: "UnitList", 4: "GosList"},
    "PhalanxAutoFight": {1: "PhalanxId", 2: "AutoList"},
    "CS_PhalanxDatas": {1: "PhaType", 2: "PlayGos"},
    "SC_PhalanxDatas": {
        1: "PhalanxList",
        2: "CurPhalanxId",
        3: "PhaType",
        4: "PlayGos",
        5: "PhaAutoFightList",
    },
    "CS_SwitchPhalanx": {1: "PhalanxId", 2: "PhaType"},
    "SC_SwitchPhalanxRet": {
        1: "Errorcode",
        2: "PhalanxId",
        3: "PhaType",
    },
    "CS_SavePhalanx": {
        1: "PhalanxId",
        2: "Pha",
        3: "PhaType",
        4: "PhaAutoFight",
        5: "SetType",
    },
    "SC_SavePhalanxRet": {
        1: "Errorcode",
        2: "PhalanxId",
        3: "Pha",
        4: "PhaType",
        5: "PhaAutoFight",
        6: "SetType",
    },
    "CS_ChangePhalanxName": {1: "PhalanxId", 2: "Name"},
    "SC_ChangePhalanxNameRet": {
        1: "Errorcode",
        2: "PhalanxId",
        3: "Name",
        4: "IllegalList",
        5: "Correction",
    },
    "CS_SaveAllAutoPhalanx": {1: "PhaAutoFight"},
    "SC_SaveAllAutoPhalanxRet": {1: "Errorcode", 2: "PhaAutoFight"},
    # CS_StartBattle deliberately has no formation ID.  The runtime obtains the
    # selected formation from BattleData.CurrentPhalanxData before command 19.
    "CS_StartBattle": {
        1: "BattleType",
        2: "RoleId",
        3: "TargetId",
        4: "DungeonId",
        5: "IsRecoverBattle",
        6: "SkillCfgMd5",
        7: "SkillEffectCfgMd5",
        8: "EffectBasicsCfgMD5",
        9: "PassiveBuffCfgMD5",
    },
    "SC_StartBattleRet": {
        1: "Errorcode",
        2: "BattleType",
        3: "RoleId",
        4: "TargetId",
        5: "DungeonId",
        6: "ArtificialTeam",
        7: "IsRecoverBattle",
        8: "MD5VerifyResult",
        9: "UploadBattleEndTestLog",
    },
}

# ErrorCode values defined from runtime assembly.
ERROR_SUCCESS = 0
ERROR_PHALANX_NAME_ILLEGAL = 161
ERROR_ILLEGAL_COMBAT_POS = 205
ERROR_ILLEGAL_PHALANX_ID = 206
ERROR_HERO_NOT_FOUND = 516
ERROR_CLIENT_REQUEST_PARAM = 32_000
ERROR_MESSAGE_DATA_ILLEGAL = 32_769
ERROR_PHALANX_CANNOT_FIND_HERO = 40_014


class FormationContentError(ValueError):
    """Raised when pinned protocol or static validation changed."""


class FormationDecodeError(ValueError):
    """Raised when a request is not valid protobuf for its exact message."""


class FormationApplicationError(ValueError):
    """Raised when a decoded request violates formation semantics."""

    def __init__(self, error_code: int, message: str) -> None:
        super().__init__(message)
        self.error_code = int(error_code)


@dataclass(frozen=True)
class FormationResult:
    """One protocol response and an atomic, shallow-mergeable state patch."""

    response_id: int
    body: bytes
    changes: Dict[str, Any] = field(default_factory=dict)
    error_code: int = ERROR_SUCCESS
    supported: bool = True
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.error_code == ERROR_SUCCESS


@dataclass(frozen=True)
class BattleFormationSelection:
    """The selected local formation consumed implicitly by command 19."""

    phalanx_id: int
    candidate_uids: Tuple[int, ...]
    active_heroes: Tuple[Tuple[int, int], ...]
    auto_fight: Tuple[Dict[str, Any], ...]
    changes: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class _Snapshot:
    state: Dict[str, Any]
    changes: Dict[str, Any]


@dataclass(frozen=True)
class _SaveRequest:
    phalanx_id: int
    phalanx: Optional[bytes]
    phalanx_type: int
    auto_fight: Optional[bytes]
    set_type: int


def _is_sequence(value: Any) -> bool:
    return isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray, memoryview)
    )


def _read_varint(data: bytes, offset: int) -> Tuple[int, int]:
    value = 0
    shift = 0
    while offset < len(data) and shift < 70:
        current = data[offset]
        offset += 1
        value |= (current & 0x7F) << shift
        if not current & 0x80:
            if value > UINT64_MAX:
                raise FormationDecodeError("protobuf varint exceeds uint64")
            return value, offset
        shift += 7
    raise FormationDecodeError("invalid or truncated protobuf varint")


def _iter_wire_fields(data: bytes) -> Iterator[Tuple[int, int, object]]:
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise FormationDecodeError("request body must be bytes")
    raw = bytes(data)
    offset = 0
    while offset < len(raw):
        tag, offset = _read_varint(raw, offset)
        number, wire_type = tag >> 3, tag & 7
        if number <= 0:
            raise FormationDecodeError("protobuf field number zero")
        if wire_type == 0:
            value, offset = _read_varint(raw, offset)
        elif wire_type == 1:
            end = offset + 8
            if end > len(raw):
                raise FormationDecodeError("truncated fixed64 field")
            value = raw[offset:end]
            offset = end
        elif wire_type == 2:
            size, offset = _read_varint(raw, offset)
            end = offset + size
            if end > len(raw):
                raise FormationDecodeError("truncated length-delimited field")
            value = raw[offset:end]
            offset = end
        elif wire_type == 5:
            end = offset + 4
            if end > len(raw):
                raise FormationDecodeError("truncated fixed32 field")
            value = raw[offset:end]
            offset = end
        else:
            raise FormationDecodeError(f"unsupported protobuf wire type {wire_type}")
        yield number, wire_type, value


def _decode_fields(
    data: bytes,
    expected_wire_types: Mapping[int, int],
    *,
    repeated: frozenset[int] = frozenset(),
    context: str,
) -> Dict[int, List[object]]:
    result: Dict[int, List[object]] = {}
    for number, wire_type, value in _iter_wire_fields(data):
        expected = expected_wire_types.get(number)
        if expected is None:
            raise FormationDecodeError(f"{context} has unexpected field {number}")
        if wire_type != expected:
            raise FormationDecodeError(
                f"{context} field {number} has wire type {wire_type}, expected {expected}"
            )
        values = result.setdefault(number, [])
        if values and number not in repeated:
            raise FormationDecodeError(f"{context} field {number} was repeated")
        values.append(value)
    return result


def _first_int(
    fields: Mapping[int, List[object]], number: int, default: int = 0
) -> int:
    values = fields.get(number)
    if not values:
        return default
    value = values[0]
    if not isinstance(value, int):
        raise FormationDecodeError(f"field {number} is not a varint")
    return value


def _first_bytes(fields: Mapping[int, List[object]], number: int) -> Optional[bytes]:
    values = fields.get(number)
    if not values:
        return None
    value = values[0]
    if not isinstance(value, bytes):
        raise FormationDecodeError(f"field {number} is not bytes")
    return value


def _uint(value: Any, maximum: int, context: str) -> int:
    if isinstance(value, bool):
        raise FormationApplicationError(
            ERROR_CLIENT_REQUEST_PARAM, f"{context} must be an unsigned integer"
        )
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise FormationApplicationError(
            ERROR_CLIENT_REQUEST_PARAM, f"{context} must be an unsigned integer"
        ) from exc
    if parsed < 0 or parsed > maximum:
        raise FormationApplicationError(
            ERROR_CLIENT_REQUEST_PARAM, f"{context} is out of range"
        )
    return parsed


def _state_uint(value: Any, default: int = 0, maximum: int = UINT64_MAX) -> int:
    if isinstance(value, bool):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return parsed if 0 <= parsed <= maximum else default


def _signed_int32(value: int) -> int:
    value &= UINT32_MAX
    return value - (1 << 32) if value & (1 << 31) else value


def _decode_utf8(value: Optional[bytes], context: str) -> str:
    if value is None:
        return ""
    try:
        return value.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise FormationDecodeError(f"{context} is not valid UTF-8") from exc


def _validate_position(position: int, context: str) -> int:
    if position not in {BENCH_COMBAT_POSITION, *ACTIVE_COMBAT_POSITIONS}:
        raise FormationApplicationError(
            ERROR_ILLEGAL_COMBAT_POS,
            f"{context} combat position {position} is not -1/0/1/2",
        )
    return position


def _decode_equiped_gospel(data: bytes) -> Dict[str, int]:
    fields = _decode_fields(
        data,
        {1: 0, 2: 0},
        context="EquipedGospel",
    )
    gospel_uid = _uint(_first_int(fields, 1), UINT64_MAX, "GospelUId")
    appearance_id = _uint(_first_int(fields, 2), UINT32_MAX, "AppearanceId")
    return {"gospel_uid": gospel_uid, "appearance_id": appearance_id}


def _decode_auto_unit(data: bytes) -> Dict[str, Any]:
    fields = _decode_fields(
        data,
        {1: 0, 2: 0, 3: 0},
        context="AutoFightUnitInfo",
    )
    use_energy = _first_int(fields, 2)
    if use_energy not in {0, 1}:
        raise FormationApplicationError(
            ERROR_CLIENT_REQUEST_PARAM, "AutoFightUnitInfo.UseEnergy must be bool"
        )
    return {
        "skill_info": _uint(
            _first_int(fields, 1), UINT32_MAX, "AutoFightUnitInfo.SkillInfo"
        ),
        "use_energy": bool(use_energy),
        "weight": _uint(_first_int(fields, 3), UINT32_MAX, "AutoFightUnitInfo.Weight"),
    }


def _decode_auto_gospel(data: bytes) -> Dict[str, Any]:
    fields = _decode_fields(
        data,
        {1: 0, 2: 0, 3: 0, 4: 2},
        repeated=frozenset({4}),
        context="AutoFightInfGos",
    )
    return {
        "equip_hero_id": _uint(
            _first_int(fields, 1), UINT64_MAX, "AutoFightInfGos.EquipHeroID"
        ),
        "gospel_uid": _uint(_first_int(fields, 2), UINT64_MAX, "AutoFightInfGos.UId"),
        "position": _uint(_first_int(fields, 3), UINT32_MAX, "AutoFightInfGos.Pos"),
        "units": [
            _decode_auto_unit(value)
            for value in fields.get(4, [])
            if isinstance(value, bytes)
        ],
    }


def _decode_auto_hero(data: bytes) -> Dict[str, Any]:
    fields = _decode_fields(
        data,
        {1: 0, 2: 0, 3: 2, 4: 2},
        repeated=frozenset({3, 4}),
        context="AutoFightInfoHero",
    )
    return {
        "hero_uid": _uint(_first_int(fields, 1), UINT64_MAX, "AutoFightInfoHero.UId"),
        "combat_pos": _validate_position(
            _signed_int32(_first_int(fields, 2)), "AutoFightInfoHero"
        ),
        "units": [
            _decode_auto_unit(value)
            for value in fields.get(3, [])
            if isinstance(value, bytes)
        ],
        "gospels": [
            _decode_auto_gospel(value)
            for value in fields.get(4, [])
            if isinstance(value, bytes)
        ],
    }


def _decode_auto_fight(data: bytes) -> Tuple[int, List[Dict[str, Any]]]:
    fields = _decode_fields(
        data,
        {1: 0, 2: 2},
        repeated=frozenset({2}),
        context="PhalanxAutoFight",
    )
    phalanx_id = _uint(_first_int(fields, 1), UINT32_MAX, "PhalanxAutoFight.PhalanxId")
    rows = [
        _decode_auto_hero(value)
        for value in fields.get(2, [])
        if isinstance(value, bytes)
    ]
    return phalanx_id, rows


def _decode_battle_hero(data: bytes) -> Dict[str, Any]:
    fields = _decode_fields(
        data,
        {1: 0, 2: 2, 3: 0},
        repeated=frozenset({2}),
        context="BattleHero",
    )
    return {
        "hero_uid": _uint(_first_int(fields, 1), UINT64_MAX, "BattleHero.HeroUId"),
        "combat_pos": _validate_position(
            _signed_int32(_first_int(fields, 3)), "BattleHero"
        ),
        "equipped_gospels": [
            _decode_equiped_gospel(value)
            for value in fields.get(2, [])
            if isinstance(value, bytes)
        ],
    }


def _decode_phalanx(data: bytes) -> Dict[str, Any]:
    fields = _decode_fields(
        data,
        {1: 2, 2: 2, 3: 0},
        repeated=frozenset({1}),
        context="Phalanx",
    )
    return {
        "heroes": [
            _decode_battle_hero(value)
            for value in fields.get(1, [])
            if isinstance(value, bytes)
        ],
        "name": _decode_utf8(_first_bytes(fields, 2), "Phalanx.Name"),
        "combat_power": _uint(_first_int(fields, 3), UINT32_MAX, "Phalanx.CombatPower"),
    }


def _decode_query(data: bytes) -> Tuple[int, Optional[bytes]]:
    fields = _decode_fields(
        data,
        {1: 0, 2: 2},
        context="CS_PhalanxDatas",
    )
    return (
        _uint(_first_int(fields, 1), UINT32_MAX, "CS_PhalanxDatas.PhaType"),
        _first_bytes(fields, 2),
    )


def _decode_switch(data: bytes) -> Tuple[int, int]:
    fields = _decode_fields(
        data,
        {1: 0, 2: 0},
        context="CS_SwitchPhalanx",
    )
    return (
        _uint(_first_int(fields, 1), UINT32_MAX, "CS_SwitchPhalanx.PhalanxId"),
        _uint(_first_int(fields, 2), UINT32_MAX, "CS_SwitchPhalanx.PhaType"),
    )


def _decode_save(data: bytes) -> _SaveRequest:
    fields = _decode_fields(
        data,
        {1: 0, 2: 2, 3: 0, 4: 2, 5: 0},
        context="CS_SavePhalanx",
    )
    return _SaveRequest(
        phalanx_id=_uint(_first_int(fields, 1), UINT32_MAX, "CS_SavePhalanx.PhalanxId"),
        phalanx=_first_bytes(fields, 2),
        phalanx_type=_uint(_first_int(fields, 3), UINT32_MAX, "CS_SavePhalanx.PhaType"),
        auto_fight=_first_bytes(fields, 4),
        set_type=_uint(_first_int(fields, 5), UINT32_MAX, "CS_SavePhalanx.SetType"),
    )


def _decode_rename(data: bytes) -> Tuple[int, str]:
    fields = _decode_fields(
        data,
        {1: 0, 2: 2},
        context="CS_ChangePhalanxName",
    )
    return (
        _uint(_first_int(fields, 1), UINT32_MAX, "CS_ChangePhalanxName.PhalanxId"),
        _decode_utf8(_first_bytes(fields, 2), "CS_ChangePhalanxName.Name"),
    )


def _decode_save_all(data: bytes) -> List[bytes]:
    fields = _decode_fields(
        data,
        {1: 2},
        repeated=frozenset({1}),
        context="CS_SaveAllAutoPhalanx",
    )
    return [value for value in fields.get(1, []) if isinstance(value, bytes)]


class FormationContentLayer:
    """Exact local formation query and mutation protocol layer."""

    def __init__(
        self,
        codec: Optional[ProtocolMessageCodec] = None,
        protocol_catalog: Path = DEFAULT_PROTOCOL_CATALOG,
        handler_dependencies: Path = DEFAULT_HANDLER_DEPENDENCIES,
        *,
        verify_assembly: bool = True,
        combat_resolver: Optional[Any] = None,
    ) -> None:
        self.protocol_catalog = Path(protocol_catalog).expanduser().resolve()
        self.handler_dependencies = Path(handler_dependencies).expanduser().resolve()
        self.codec = codec or ProtocolMessageCodec(self.protocol_catalog)
        self.combat_resolver = combat_resolver
        self._validate_protocol(verify_assembly=verify_assembly)
        self._validate_handler_dependencies()

    def _validate_protocol(self, *, verify_assembly: bool) -> None:
        try:
            document = json.loads(self.protocol_catalog.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise FormationContentError(
                f"cannot load protocol catalog {self.protocol_catalog}"
            ) from exc
        commands = {int(row["id"]): row for row in document.get("commands", [])}
        for command_id, expected_name in EXPECTED_COMMANDS.items():
            names = commands.get(command_id, {}).get("names", [])
            if expected_name not in names:
                raise FormationContentError(
                    f"protocol command {command_id} is not {expected_name}"
                )
        pairs = document.get("request_to_response", {})
        expected_pairs = {**RESPONSE_IDS, CS_START_BATTLE: SC_START_BATTLE_RET}
        for request_id, response_id in expected_pairs.items():
            if int(pairs.get(str(request_id), -1)) != response_id:
                raise FormationContentError(
                    f"protocol request {request_id} does not map to {response_id}"
                )
        for message_name, expected in EXPECTED_PROTOCOL_FIELDS.items():
            schema = self.codec.schema(message_name)
            actual = {
                int(row["number"]): str(row["name"]) for row in schema.get("fields", [])
            }
            if actual != expected:
                raise FormationContentError(
                    f"protocol fields for {message_name} changed: {actual}"
                )
        if verify_assembly:
            assembly = Path(str(document.get("assembly") or "")).expanduser().resolve()
            if not assembly.is_file():
                raise FormationContentError(
                    f"protocol source assembly is missing: {assembly}"
                )

    def _validate_handler_dependencies(self) -> None:
        try:
            document = json.loads(self.handler_dependencies.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise FormationContentError(
                f"cannot load handler dependencies {self.handler_dependencies}"
            ) from exc
        handlers = {str(row.get("key")): row for row in document.get("handlers", [])}
        requirements = {
            "NetMessageLogic::OnSC_PhalanxDatas": {
                "calls": {"BattleData::InitPhalanxData"},
                "getters": set(),
            },
            "NetMessageLogic::OnSC_SwitchPhalanxRet": {
                "calls": {"BattleData::UpdateSelectPhalanxIndex"},
                "getters": {
                    "Msg.SC_SwitchPhalanxRet::get_Errorcode",
                    "Msg.SC_SwitchPhalanxRet::get_PhalanxId",
                },
            },
            "NetMessageLogic::OnSC_SavePhalanxRet": {
                "calls": {
                    "BattleData::UpdatePhalanxData",
                    "BattleData::UpdateAutoBattlePhalanxData",
                },
                "getters": {
                    "Msg.SC_SavePhalanxRet::get_Errorcode",
                    "Msg.SC_SavePhalanxRet::get_PhalanxId",
                    "Msg.SC_SavePhalanxRet::get_Pha",
                    "Msg.SC_SavePhalanxRet::get_PhaAutoFight",
                    "Msg.SC_SavePhalanxRet::get_SetType",
                },
            },
            "AutoBattleSettingView::OnSaveAllAutoPhalanxRet": {
                "calls": {"BattleData::UpdateAutoBattlePhalanxData"},
                "getters": {
                    "Msg.SC_SaveAllAutoPhalanxRet::get_Errorcode",
                    "Msg.SC_SaveAllAutoPhalanxRet::get_PhaAutoFight",
                },
            },
        }
        for key, expected in requirements.items():
            row = handlers.get(key)
            if row is None:
                raise FormationContentError(f"missing static handler metadata {key}")
            calls = set(map(str, row.get("calls", [])))
            getters = set(map(str, row.get("message_getters", [])))
            missing_calls = expected["calls"] - calls
            missing_getters = expected["getters"] - getters
            if missing_calls or missing_getters:
                raise FormationContentError(
                    f"handler metadata changed for {key}: "
                    f"calls={sorted(missing_calls)}, getters={sorted(missing_getters)}"
                )

    @staticmethod
    def _owned_hero_uids(profile: Mapping[str, Any]) -> Tuple[int, ...]:
        heroes = profile.get("heroes")
        if not _is_sequence(heroes):
            raise FormationApplicationError(
                ERROR_HERO_NOT_FOUND, "profile.heroes is not a seven-row list"
            )
        rows = [row for row in heroes if isinstance(row, Mapping)]
        newcomer = bool(profile.get("player_progression"))
        valid_count = 1 <= len(rows) <= HERO_COUNT if newcomer else len(rows) == HERO_COUNT
        if not valid_count or len(rows) != len(heroes):
            raise FormationApplicationError(
                ERROR_HERO_NOT_FOUND,
                f"formation requires exactly {HERO_COUNT} owned heroes",
            )
        uids = tuple(_state_uint(row.get("uid"), 0, UINT64_MAX) for row in rows)
        if any(not uid for uid in uids) or len(set(uids)) != len(rows):
            raise FormationApplicationError(
                ERROR_HERO_NOT_FOUND, "owned hero UIDs must be positive and unique"
            )
        if not newcomer and LILIA_HERO_UID not in uids:
            raise FormationApplicationError(
                ERROR_HERO_NOT_FOUND,
                f"Lillia UID {LILIA_HERO_UID} is missing from the owned roster",
            )
        return uids

    @staticmethod
    def _known_gospel_uids(profile: Mapping[str, Any]) -> frozenset[int]:
        rows = profile.get("gospels")
        if not _is_sequence(rows):
            return frozenset()
        return frozenset(
            uid
            for row in rows
            if isinstance(row, Mapping)
            if (uid := _state_uint(row.get("uid"), 0, UINT64_MAX))
        )

    @staticmethod
    def _total_combat_power(profile: Mapping[str, Any]) -> int:
        rows = profile.get("heroes")
        if not _is_sequence(rows):
            return 100
        total = sum(
            max(1, _state_uint(row.get("combat_power"), 1, UINT32_MAX))
            for row in rows
            if isinstance(row, Mapping)
        )
        return min(UINT32_MAX, max(100, total))

    @staticmethod
    def _source_state(profile: Mapping[str, Any]) -> Mapping[str, Any]:
        value = profile.get(FORMATION_STATE_KEY)
        if (
            isinstance(value, Mapping)
            and _state_uint(value.get("schema_version"), 0)
            == FORMATION_STATE_SCHEMA_VERSION
        ):
            return value
        return profile

    @staticmethod
    def _normalize_stored_gospel(
        value: Any, known_gospels: frozenset[int]
    ) -> Optional[Dict[str, int]]:
        if not isinstance(value, Mapping):
            return None
        gospel_uid = _state_uint(value.get("gospel_uid"), 0, UINT64_MAX)
        if gospel_uid and gospel_uid not in known_gospels:
            return None
        return {
            "gospel_uid": gospel_uid,
            "appearance_id": _state_uint(value.get("appearance_id"), 0, UINT32_MAX),
        }

    @classmethod
    def _normalize_equipped_map(
        cls,
        value: Any,
        hero_uids: Sequence[int],
        known_gospels: frozenset[int],
    ) -> Dict[str, List[Dict[str, int]]]:
        source = value if isinstance(value, Mapping) else {}
        result: Dict[str, List[Dict[str, int]]] = {}
        for uid in hero_uids:
            rows = source.get(str(uid), source.get(uid, []))
            normalized: List[Dict[str, int]] = []
            if _is_sequence(rows):
                for row in rows[:MAX_EQUIPPED_GOSPELS]:
                    item = cls._normalize_stored_gospel(row, known_gospels)
                    if item is not None:
                        normalized.append(item)
            result[str(uid)] = normalized
        return result

    @staticmethod
    def _normalize_unit(value: Any) -> Optional[Dict[str, Any]]:
        if not isinstance(value, Mapping):
            return None
        return {
            "skill_info": _state_uint(value.get("skill_info"), 0, UINT32_MAX),
            "use_energy": bool(value.get("use_energy", False)),
            "weight": _state_uint(value.get("weight"), 0, UINT32_MAX),
        }

    @classmethod
    def _normalize_auto_gospel(
        cls,
        value: Any,
        hero_uids: frozenset[int],
        known_gospels: frozenset[int],
    ) -> Optional[Dict[str, Any]]:
        if not isinstance(value, Mapping):
            return None
        equip_hero_id = _state_uint(value.get("equip_hero_id"), 0, UINT64_MAX)
        gospel_uid = _state_uint(value.get("gospel_uid"), 0, UINT64_MAX)
        if equip_hero_id and equip_hero_id not in hero_uids:
            return None
        if gospel_uid and gospel_uid not in known_gospels:
            return None
        raw_units = value.get("units")
        units = []
        if _is_sequence(raw_units):
            units = [
                normalized
                for row in raw_units
                if (normalized := cls._normalize_unit(row)) is not None
            ]
        return {
            "equip_hero_id": equip_hero_id,
            "gospel_uid": gospel_uid,
            "position": _state_uint(value.get("position"), 0, UINT32_MAX),
            "units": units,
        }

    @classmethod
    def _normalize_auto_rows(
        cls,
        value: Any,
        hero_uids: Sequence[int],
        positions: Mapping[str, int],
        known_gospels: frozenset[int],
    ) -> List[Dict[str, Any]]:
        source_rows = value if _is_sequence(value) else []
        by_uid: Dict[int, Mapping[str, Any]] = {}
        for row in source_rows:
            if not isinstance(row, Mapping):
                continue
            uid = _state_uint(row.get("hero_uid"), 0, UINT64_MAX)
            if uid in hero_uids and uid not in by_uid:
                by_uid[uid] = row
        valid_uids = frozenset(hero_uids)
        result = []
        for uid in hero_uids:
            source = by_uid.get(uid, {})
            raw_units = source.get("units") if isinstance(source, Mapping) else None
            units = []
            if _is_sequence(raw_units):
                units = [
                    normalized
                    for row in raw_units
                    if (normalized := cls._normalize_unit(row)) is not None
                ]
            raw_gospels = source.get("gospels") if isinstance(source, Mapping) else None
            gospels = []
            if _is_sequence(raw_gospels):
                gospels = [
                    normalized
                    for row in raw_gospels[:MAX_EQUIPPED_GOSPELS]
                    if (
                        normalized := cls._normalize_auto_gospel(
                            row, valid_uids, known_gospels
                        )
                    )
                    is not None
                ]
            result.append(
                {
                    "hero_uid": uid,
                    # Formation positions are authoritative; auto arrays may
                    # not silently disagree with the battlefield lineup.
                    "combat_pos": int(positions[str(uid)]),
                    "units": units,
                    "gospels": gospels,
                }
            )
        return result

    def _snapshot(self, profile: Mapping[str, Any]) -> _Snapshot:
        if not isinstance(profile, Mapping):
            raise FormationApplicationError(
                ERROR_CLIENT_REQUEST_PARAM, "profile must be a mapping"
            )
        owned_uids = self._owned_hero_uids(profile)
        known_gospels = self._known_gospel_uids(profile)
        source = self._source_state(profile)
        raw_phalanxes = source.get("phalanxes")
        raw_rows = raw_phalanxes if _is_sequence(raw_phalanxes) else []
        by_id: Dict[int, Mapping[str, Any]] = {}
        for index, row in enumerate(raw_rows):
            if not isinstance(row, Mapping):
                continue
            slot = _state_uint(row.get("id"), index, UINT32_MAX)
            if slot < PHALANX_COUNT and slot not in by_id:
                by_id[slot] = row

        total_power = self._total_combat_power(profile)
        phalanxes: List[Dict[str, Any]] = []
        for slot in range(PHALANX_COUNT):
            source_row = by_id.get(slot, {})
            raw_uids = source_row.get("hero_uids")
            ordered: List[int] = []
            if _is_sequence(raw_uids):
                for raw_uid in raw_uids:
                    uid = _state_uint(raw_uid, 0, UINT64_MAX)
                    if uid in owned_uids and uid not in ordered:
                        ordered.append(uid)
            for uid in owned_uids:
                if uid not in ordered:
                    ordered.append(uid)

            raw_positions = source_row.get("combat_positions")
            has_saved_positions = isinstance(raw_positions, Mapping)
            position_source = raw_positions if has_saved_positions else {}
            positions: Dict[str, int] = {}
            used_active = set()
            for index, uid in enumerate(ordered):
                default = (
                    BENCH_COMBAT_POSITION
                    if has_saved_positions
                    else index
                    if index in ACTIVE_COMBAT_POSITIONS
                    else -1
                )
                raw_position = position_source.get(
                    str(uid), position_source.get(uid, default)
                )
                try:
                    position = int(raw_position)
                except (TypeError, ValueError, OverflowError):
                    position = default
                if position not in {BENCH_COMBAT_POSITION, *ACTIVE_COMBAT_POSITIONS}:
                    position = BENCH_COMBAT_POSITION
                if position in used_active:
                    position = BENCH_COMBAT_POSITION
                if position in ACTIVE_COMBAT_POSITIONS:
                    used_active.add(position)
                positions[str(uid)] = position

            name = normalize_stored_formation_name(source_row.get("name"), slot)
            equipped = self._normalize_equipped_map(
                source_row.get("equipped_gospels"), ordered, known_gospels
            )
            if profile.get('player_progression'):
                for uid, gospel_rows in equipped.items():
                    for index, item in enumerate(gospel_rows,1):
                        if item['gospel_uid'] and not self._gospel_slot_open(profile,int(uid),index):
                            gospel_rows[index-1]={'gospel_uid':0,'appearance_id':0}
            normalized_row = {
                "id": slot,
                "name": name,
                "hero_uids": ordered,
                "combat_positions": positions,
                "equipped_gospels": equipped,
                "combat_power": _state_uint(
                    source_row.get("combat_power"), total_power, UINT32_MAX
                )
                or total_power,
                "type": 0,
            }
            if self.combat_resolver is not None:
                normalized_row["combat_power"] = int(
                    self.combat_resolver.resolve_formation(
                        profile,
                        normalized_row,
                    ).combat_power
                )
            phalanxes.append(normalized_row)

        current_id = _state_uint(source.get("current_phalanx_id"), 0, UINT32_MAX)
        if current_id >= PHALANX_COUNT:
            current_id = 0
        raw_auto = source.get("auto_phalanxes")
        auto_source = raw_auto if isinstance(raw_auto, Mapping) else {}
        auto_phalanxes: Dict[str, List[Dict[str, Any]]] = {}
        for slot, phalanx in enumerate(phalanxes):
            rows = auto_source.get(str(slot), auto_source.get(slot, []))
            auto_phalanxes[str(slot)] = self._normalize_auto_rows(
                rows,
                phalanx["hero_uids"],
                phalanx["combat_positions"],
                known_gospels,
            )

        state = {
            "schema_version": FORMATION_STATE_SCHEMA_VERSION,
            "hero_uids": list(owned_uids),
            "phalanxes": phalanxes,
            "current_phalanx_id": current_id,
            "auto_phalanxes": auto_phalanxes,
        }
        candidates = {
            FORMATION_STATE_KEY: state,
            "phalanxes": phalanxes,
            "current_phalanx_id": current_id,
            "auto_phalanxes": auto_phalanxes,
        }
        changes = {
            key: copy.deepcopy(value)
            for key, value in candidates.items()
            if profile.get(key) != value
        }
        return _Snapshot(state=state, changes=changes)

    def _gospel_slot_open(self, profile, hero_uid, position):
        """Mirror UIGameExt.IsGospelSlotUnlock's one-based source rule."""
        if not profile.get('player_progression'):
            return True
        if position not in (1,2,3):
            return False
        catalog=getattr(self.combat_resolver,'catalog',None)
        if catalog is None:
            return position<=2
        hero=next((row for row in profile.get('heroes',[]) if int(row['uid'])==hero_uid),None)
        if hero is None:
            return False
        profession_id=int(hero.get('profession_id',3900000))
        learned={int(node) for row in profile.get('heroes',[]) for node in row.get('profession_talent_ids',[])}
        if profession_id==3900000:
            threshold=int(catalog.table('a_global').by_id[9990284+position]['int_value'])
            return len(learned)>=threshold
        groups={row['id'] for row in catalog.table('a_job_skillgroup').records
                if row['job_id']==profession_id and row['job_level']==1}
        required=[row['id'] for row in catalog.table('a_job_skill').records
                  if row['skillgroup_id'] in groups and row['unlock_gospel']==position]
        return all(node in learned for node in required)

    @staticmethod
    def _validate_slot(phalanx_id: int) -> None:
        if phalanx_id >= PHALANX_COUNT:
            raise FormationApplicationError(
                ERROR_ILLEGAL_PHALANX_ID,
                f"formation ID {phalanx_id} is outside 0..{PHALANX_COUNT - 1}",
            )

    @staticmethod
    def _validate_common_type(phalanx_type: int) -> None:
        if phalanx_type != 0:
            raise FormationApplicationError(
                ERROR_CLIENT_REQUEST_PARAM,
                f"formation type {phalanx_type} is not the common local formation",
            )

    @staticmethod
    def _validate_gospel_uid(
        gospel_uid: int, known_gospels: frozenset[int], context: str
    ) -> None:
        if gospel_uid and gospel_uid not in known_gospels:
            raise FormationApplicationError(
                ERROR_CLIENT_REQUEST_PARAM,
                f"{context} gospel UID {gospel_uid} is not owned",
            )

    def _canonical_saved_phalanx(
        self,
        profile: Mapping[str, Any],
        decoded: Mapping[str, Any],
        existing: Mapping[str, Any],
        owned_uids: Sequence[int],
        known_gospels: frozenset[int],
    ) -> Dict[str, Any]:
        heroes = decoded.get("heroes")
        if not isinstance(heroes, list) or len(heroes) != len(owned_uids):
            raise FormationApplicationError(
                ERROR_PHALANX_CANNOT_FIND_HERO,
                f"Phalanx.HeroList must contain exactly {len(owned_uids)} owned heroes",
            )
        request_uids = [int(row.get("hero_uid", 0)) for row in heroes]
        if len(set(request_uids)) != len(owned_uids) or set(request_uids) != set(owned_uids):
            unknown = sorted(set(request_uids) - set(owned_uids))
            missing = sorted(set(owned_uids) - set(request_uids))
            raise FormationApplicationError(
                ERROR_HERO_NOT_FOUND,
                f"formation hero roster mismatch; unknown={unknown}, missing={missing}",
            )
        active_positions = [
            int(row["combat_pos"])
            for row in heroes
            if int(row["combat_pos"]) in ACTIVE_COMBAT_POSITIONS
        ]
        if len(active_positions) != len(set(active_positions)):
            raise FormationApplicationError(
                ERROR_ILLEGAL_COMBAT_POS, "two heroes occupy the same combat position"
            )

        equipped: Dict[str, List[Dict[str, int]]] = {}
        globally_equipped = set()
        positions: Dict[str, int] = {}
        for row in heroes:
            uid = int(row["hero_uid"])
            gospel_rows = list(row.get("equipped_gospels", []))
            if len(gospel_rows) > MAX_EQUIPPED_GOSPELS:
                raise FormationApplicationError(
                    ERROR_CLIENT_REQUEST_PARAM,
                    f"hero {uid} has more than {MAX_EQUIPPED_GOSPELS} gospels",
                )
            for position, gospel in enumerate(gospel_rows,1):
                gospel_uid = int(gospel["gospel_uid"])
                self._validate_gospel_uid(
                    gospel_uid, known_gospels, f"BattleHero {uid}"
                )
                if gospel_uid and not self._gospel_slot_open(profile,uid,position):
                    raise FormationApplicationError(ERROR_CLIENT_REQUEST_PARAM,
                        f'hero {uid} Gospel slot {position} requires its source profession talent')
                if gospel_uid and gospel_uid in globally_equipped:
                    raise FormationApplicationError(
                        ERROR_CLIENT_REQUEST_PARAM,
                        f"gospel UID {gospel_uid} is equipped more than once",
                    )
                if gospel_uid:
                    globally_equipped.add(gospel_uid)
            positions[str(uid)] = int(row["combat_pos"])
            equipped[str(uid)] = gospel_rows

        requested_name = decoded.get("name")
        if requested_name and not is_client_valid_formation_name(requested_name):
            raise FormationApplicationError(
                ERROR_PHALANX_NAME_ILLEGAL,
                "formation name exceeds the client weight limit or contains '*'",
            )
        name = str(requested_name) if requested_name else str(existing["name"])
        combat_power = int(decoded.get("combat_power") or existing["combat_power"])
        result = {
            "id": int(existing["id"]),
            "name": name,
            "hero_uids": request_uids,
            "combat_positions": positions,
            "equipped_gospels": equipped,
            "combat_power": combat_power,
            "type": 0,
        }
        if self.combat_resolver is not None:
            result["combat_power"] = int(
                self.combat_resolver.resolve_formation(profile, result).combat_power
            )
        return result

    def _canonical_saved_auto(
        self,
        data: bytes,
        expected_slot: int,
        phalanx: Mapping[str, Any],
        known_gospels: frozenset[int],
    ) -> List[Dict[str, Any]]:
        phalanx_id, rows = _decode_auto_fight(data)
        if phalanx_id != expected_slot:
            raise FormationApplicationError(
                ERROR_ILLEGAL_PHALANX_ID,
                f"auto formation ID {phalanx_id} does not match {expected_slot}",
            )
        expected_uids = [int(uid) for uid in phalanx["hero_uids"]]
        if len(rows) != len(expected_uids):
            raise FormationApplicationError(
                ERROR_PHALANX_CANNOT_FIND_HERO,
                f"PhalanxAutoFight.AutoList must contain {len(expected_uids)} owned heroes",
            )
        by_uid: Dict[int, Dict[str, Any]] = {}
        for row in rows:
            uid = int(row["hero_uid"])
            if uid in by_uid or uid not in expected_uids:
                raise FormationApplicationError(
                    ERROR_HERO_NOT_FOUND,
                    f"auto-battle hero UID {uid} is duplicate or not owned",
                )
            expected_position = int(phalanx["combat_positions"][str(uid)])
            if int(row["combat_pos"]) != expected_position:
                raise FormationApplicationError(
                    ERROR_ILLEGAL_COMBAT_POS,
                    f"auto position for hero {uid} disagrees with formation",
                )
            if len(row.get("gospels", [])) > MAX_EQUIPPED_GOSPELS:
                raise FormationApplicationError(
                    ERROR_CLIENT_REQUEST_PARAM,
                    f"auto hero {uid} has more than three gospel entries",
                )
            for gospel in row.get("gospels", []):
                equip_hero_id = int(gospel["equip_hero_id"])
                if equip_hero_id and equip_hero_id not in expected_uids:
                    raise FormationApplicationError(
                        ERROR_HERO_NOT_FOUND,
                        f"auto gospel references unknown hero UID {equip_hero_id}",
                    )
                self._validate_gospel_uid(
                    int(gospel["gospel_uid"]), known_gospels, f"auto hero {uid}"
                )
            by_uid[uid] = copy.deepcopy(row)
        if set(by_uid) != set(expected_uids):
            raise FormationApplicationError(
                ERROR_HERO_NOT_FOUND, "auto-battle roster does not match formation"
            )
        return [by_uid[uid] for uid in expected_uids]

    @staticmethod
    def _equiped_gospel_message(row: Mapping[str, Any]) -> Dict[str, int]:
        values: Dict[str, int] = {}
        gospel_uid = int(row.get("gospel_uid", 0))
        appearance_id = int(row.get("appearance_id", 0))
        if gospel_uid:
            values["GospelUId"] = gospel_uid
        if appearance_id:
            values["AppearanceId"] = appearance_id
        return values

    @classmethod
    def _phalanx_message(cls, row: Mapping[str, Any]) -> Dict[str, Any]:
        positions = row["combat_positions"]
        equipped = row["equipped_gospels"]
        heroes = []
        for uid in row["hero_uids"]:
            heroes.append(
                {
                    "HeroUId": int(uid),
                    "EquipedGospelList": [
                        cls._equiped_gospel_message(gospel)
                        for gospel in equipped[str(uid)]
                    ],
                    "CombatPos": int(positions[str(uid)]),
                }
            )
        return {
            "HeroList": heroes,
            "Name": str(row["name"]),
            "CombatPower": int(row["combat_power"]),
        }

    @staticmethod
    def _auto_unit_message(row: Mapping[str, Any]) -> Dict[str, Any]:
        return {
            "SkillInfo": int(row.get("skill_info", 0)),
            "UseEnergy": bool(row.get("use_energy", False)),
            "Weight": int(row.get("weight", 0)),
        }

    @classmethod
    def _auto_gospel_message(cls, row: Mapping[str, Any]) -> Dict[str, Any]:
        values: Dict[str, Any] = {}
        equip_hero_id = int(row.get("equip_hero_id", 0))
        gospel_uid = int(row.get("gospel_uid", 0))
        position = int(row.get("position", 0))
        units = row.get("units", [])
        if equip_hero_id:
            values["EquipHeroID"] = equip_hero_id
        if gospel_uid:
            values["UId"] = gospel_uid
            values["Pos"] = position
        if units:
            values["UnitList"] = [cls._auto_unit_message(unit) for unit in units]
        return values

    @classmethod
    def _auto_fight_message(
        cls, phalanx_id: int, rows: Sequence[Mapping[str, Any]]
    ) -> Dict[str, Any]:
        return {
            "PhalanxId": phalanx_id,
            "AutoList": [
                {
                    "UId": int(row["hero_uid"]),
                    "Pos": int(row["combat_pos"]),
                    "UnitList": [
                        cls._auto_unit_message(unit) for unit in row.get("units", [])
                    ],
                    "GosList": [
                        cls._auto_gospel_message(gospel)
                        for gospel in row.get("gospels", [])
                    ],
                }
                for row in rows
            ],
        }

    @staticmethod
    def _changes_for_state(
        profile: Mapping[str, Any], state: Mapping[str, Any]
    ) -> Dict[str, Any]:
        candidates = {
            FORMATION_STATE_KEY: state,
            "phalanxes": state["phalanxes"],
            "current_phalanx_id": state["current_phalanx_id"],
            "auto_phalanxes": state["auto_phalanxes"],
        }
        return {
            key: copy.deepcopy(value)
            for key, value in candidates.items()
            if profile.get(key) != value
        }

    def _query_body(self, state: Mapping[str, Any]) -> bytes:
        return self.codec.encode(
            "SC_PhalanxDatas",
            {
                "PhalanxList": [
                    self._phalanx_message(row) for row in state["phalanxes"]
                ],
                "CurPhalanxId": int(state["current_phalanx_id"]),
                "PhaType": 0,
                "PhaAutoFightList": [
                    self._auto_fight_message(slot, state["auto_phalanxes"][str(slot)])
                    for slot in range(PHALANX_COUNT)
                ],
            },
        )

    def _query(self, profile: Mapping[str, Any], body: bytes) -> FormationResult:
        snapshot = self._snapshot(profile)
        try:
            phalanx_type, play_gospel = _decode_query(body)
            self._validate_common_type(phalanx_type)
            if play_gospel is not None:
                raise FormationApplicationError(
                    ERROR_CLIENT_REQUEST_PARAM,
                    "common local formations do not accept PlayGos",
                )
        except FormationDecodeError as exc:
            return FormationResult(
                SC_PHALANX_DATAS,
                self._query_body(snapshot.state),
                changes=snapshot.changes,
                error_code=ERROR_MESSAGE_DATA_ILLEGAL,
                reason=str(exc),
            )
        except FormationApplicationError as exc:
            return FormationResult(
                SC_PHALANX_DATAS,
                self._query_body(snapshot.state),
                changes=snapshot.changes,
                error_code=exc.error_code,
                reason=str(exc),
            )
        return FormationResult(
            SC_PHALANX_DATAS,
            self._query_body(snapshot.state),
            changes=snapshot.changes,
        )

    def _switch(self, profile: Mapping[str, Any], body: bytes) -> FormationResult:
        try:
            phalanx_id, phalanx_type = _decode_switch(body)
            self._validate_slot(phalanx_id)
            self._validate_common_type(phalanx_type)
            snapshot = self._snapshot(profile)
        except FormationDecodeError as exc:
            return FormationResult(
                SC_SWITCH_PHALANX_RET,
                self.codec.encode(
                    "SC_SwitchPhalanxRet",
                    {"Errorcode": ERROR_MESSAGE_DATA_ILLEGAL},
                ),
                error_code=ERROR_MESSAGE_DATA_ILLEGAL,
                reason=str(exc),
            )
        except FormationApplicationError as exc:
            return FormationResult(
                SC_SWITCH_PHALANX_RET,
                self.codec.encode("SC_SwitchPhalanxRet", {"Errorcode": exc.error_code}),
                error_code=exc.error_code,
                reason=str(exc),
            )
        state = copy.deepcopy(snapshot.state)
        state["current_phalanx_id"] = phalanx_id
        changes = self._changes_for_state(profile, state)
        response = self.codec.encode(
            "SC_SwitchPhalanxRet",
            {"PhalanxId": phalanx_id, "PhaType": phalanx_type},
        )
        return FormationResult(SC_SWITCH_PHALANX_RET, response, changes=changes)

    def _save(self, profile: Mapping[str, Any], body: bytes) -> FormationResult:
        try:
            request = _decode_save(body)
            self._validate_slot(request.phalanx_id)
            self._validate_common_type(request.phalanx_type)
            if request.set_type not in {0, 1, 2}:
                raise FormationApplicationError(
                    ERROR_CLIENT_REQUEST_PARAM,
                    f"CS_SavePhalanx.SetType {request.set_type} is not 0/1/2",
                )
            if request.set_type in {0, 2} and request.phalanx is None:
                raise FormationDecodeError("CS_SavePhalanx.Pha is required")
            if request.set_type in {1, 2} and request.auto_fight is None:
                raise FormationDecodeError("CS_SavePhalanx.PhaAutoFight is required")
            snapshot = self._snapshot(profile)
            state = copy.deepcopy(snapshot.state)
            slot = request.phalanx_id
            known_gospels = self._known_gospel_uids(profile)
            if request.set_type in {0, 2}:
                decoded = _decode_phalanx(request.phalanx or b"")
                state["phalanxes"][slot] = self._canonical_saved_phalanx(
                    profile,
                    decoded,
                    state["phalanxes"][slot],
                    state["hero_uids"],
                    known_gospels,
                )
            if request.set_type in {1, 2}:
                state["auto_phalanxes"][str(slot)] = self._canonical_saved_auto(
                    request.auto_fight or b"",
                    slot,
                    state["phalanxes"][slot],
                    known_gospels,
                )
        except FormationDecodeError as exc:
            return FormationResult(
                SC_SAVE_PHALANX_RET,
                self.codec.encode(
                    "SC_SavePhalanxRet",
                    {"Errorcode": ERROR_MESSAGE_DATA_ILLEGAL},
                ),
                error_code=ERROR_MESSAGE_DATA_ILLEGAL,
                reason=str(exc),
            )
        except FormationApplicationError as exc:
            return FormationResult(
                SC_SAVE_PHALANX_RET,
                self.codec.encode("SC_SavePhalanxRet", {"Errorcode": exc.error_code}),
                error_code=exc.error_code,
                reason=str(exc),
            )

        response_values: Dict[str, Any] = {
            "PhalanxId": request.phalanx_id,
            "PhaType": request.phalanx_type,
            "SetType": request.set_type,
        }
        if request.set_type in {0, 2}:
            response_values["Pha"] = self._phalanx_message(
                state["phalanxes"][request.phalanx_id]
            )
        if request.set_type in {1, 2}:
            response_values["PhaAutoFight"] = self._auto_fight_message(
                request.phalanx_id,
                state["auto_phalanxes"][str(request.phalanx_id)],
            )
        return FormationResult(
            SC_SAVE_PHALANX_RET,
            self.codec.encode("SC_SavePhalanxRet", response_values),
            changes=self._changes_for_state(profile, state),
        )

    def _rename(self, profile: Mapping[str, Any], body: bytes) -> FormationResult:
        try:
            phalanx_id, raw_name = _decode_rename(body)
            self._validate_slot(phalanx_id)
            name = raw_name
            if not is_client_valid_formation_name(name):
                raise FormationApplicationError(
                    ERROR_PHALANX_NAME_ILLEGAL,
                    "formation name must be non-empty, contain no '*', and have "
                    f"client weight <= {CLIENT_FORMATION_NAME_WEIGHT_LIMIT}; "
                    f"got {formation_name_weight(name)}",
                )
            snapshot = self._snapshot(profile)
        except FormationDecodeError as exc:
            return FormationResult(
                SC_CHANGE_PHALANX_NAME_RET,
                self.codec.encode(
                    "SC_ChangePhalanxNameRet",
                    {"Errorcode": ERROR_MESSAGE_DATA_ILLEGAL},
                ),
                error_code=ERROR_MESSAGE_DATA_ILLEGAL,
                reason=str(exc),
            )
        except FormationApplicationError as exc:
            return FormationResult(
                SC_CHANGE_PHALANX_NAME_RET,
                self.codec.encode(
                    "SC_ChangePhalanxNameRet", {"Errorcode": exc.error_code}
                ),
                error_code=exc.error_code,
                reason=str(exc),
            )
        state = copy.deepcopy(snapshot.state)
        state["phalanxes"][phalanx_id]["name"] = name
        return FormationResult(
            SC_CHANGE_PHALANX_NAME_RET,
            self.codec.encode(
                "SC_ChangePhalanxNameRet",
                {"PhalanxId": phalanx_id, "Name": name},
            ),
            changes=self._changes_for_state(profile, state),
        )

    def _save_all_auto(
        self, profile: Mapping[str, Any], body: bytes
    ) -> FormationResult:
        try:
            raw_rows = _decode_save_all(body)
            if len(raw_rows) != PHALANX_COUNT:
                raise FormationApplicationError(
                    ERROR_ILLEGAL_PHALANX_ID,
                    f"CS_SaveAllAutoPhalanx requires {PHALANX_COUNT} slots",
                )
            snapshot = self._snapshot(profile)
            state = copy.deepcopy(snapshot.state)
            known_gospels = self._known_gospel_uids(profile)
            parsed = [_decode_auto_fight(raw)[0] for raw in raw_rows]
            if set(parsed) != set(range(PHALANX_COUNT)) or len(set(parsed)) != len(
                parsed
            ):
                raise FormationApplicationError(
                    ERROR_ILLEGAL_PHALANX_ID,
                    "auto-battle slots must be exactly 0,1,2,3,4",
                )
            by_slot = dict(zip(parsed, raw_rows))
            for slot in range(PHALANX_COUNT):
                state["auto_phalanxes"][str(slot)] = self._canonical_saved_auto(
                    by_slot[slot],
                    slot,
                    state["phalanxes"][slot],
                    known_gospels,
                )
        except FormationDecodeError as exc:
            return FormationResult(
                SC_SAVE_ALL_AUTO_PHALANX_RET,
                self.codec.encode(
                    "SC_SaveAllAutoPhalanxRet",
                    {"Errorcode": ERROR_MESSAGE_DATA_ILLEGAL},
                ),
                error_code=ERROR_MESSAGE_DATA_ILLEGAL,
                reason=str(exc),
            )
        except FormationApplicationError as exc:
            return FormationResult(
                SC_SAVE_ALL_AUTO_PHALANX_RET,
                self.codec.encode(
                    "SC_SaveAllAutoPhalanxRet", {"Errorcode": exc.error_code}
                ),
                error_code=exc.error_code,
                reason=str(exc),
            )
        return FormationResult(
            SC_SAVE_ALL_AUTO_PHALANX_RET,
            self.codec.encode(
                "SC_SaveAllAutoPhalanxRet",
                {
                    "PhaAutoFight": [
                        self._auto_fight_message(
                            slot, state["auto_phalanxes"][str(slot)]
                        )
                        for slot in range(PHALANX_COUNT)
                    ]
                },
            ),
            changes=self._changes_for_state(profile, state),
        )

    def battle_selection(self, profile: Mapping[str, Any]) -> BattleFormationSelection:
        """Resolve the formation command 19 uses implicitly.

        ``CS_StartBattle`` has no PhalanxId field.  Public battle integration
        should call this under the same account lock before accepting command
        19, apply ``changes`` if present, then consume ``active_heroes`` and
        matching ``auto_fight`` from the selected zero-based slot.
        """

        snapshot = self._snapshot(profile)
        slot = int(snapshot.state["current_phalanx_id"])
        phalanx = snapshot.state["phalanxes"][slot]
        active = tuple(
            sorted(
                (
                    (int(uid), int(phalanx["combat_positions"][str(uid)]))
                    for uid in phalanx["hero_uids"]
                    if int(phalanx["combat_positions"][str(uid)])
                    in ACTIVE_COMBAT_POSITIONS
                ),
                key=lambda item: item[1],
            )
        )
        if not active:
            raise FormationApplicationError(
                ERROR_ILLEGAL_COMBAT_POS,
                f"selected formation {slot} has no active hero",
            )
        return BattleFormationSelection(
            phalanx_id=slot,
            candidate_uids=tuple(map(int, phalanx["hero_uids"])),
            active_heroes=active,
            auto_fight=tuple(
                copy.deepcopy(snapshot.state["auto_phalanxes"][str(slot)])
            ),
            changes=snapshot.changes,
        )

    def handle(
        self,
        command_id: int,
        profile: Optional[Mapping[str, Any]],
        body: bytes = b"",
    ) -> Optional[FormationResult]:
        """Handle one formation request, or return ``None`` for another layer."""

        if command_id not in REQUEST_IDS:
            return None
        current = profile or {}
        if command_id == CS_PHALANX_DATAS:
            return self._query(current, body)
        if command_id == CS_SWITCH_PHALANX:
            return self._switch(current, body)
        if command_id == CS_SAVE_PHALANX:
            return self._save(current, body)
        if command_id == CS_CHANGE_PHALANX_NAME:
            return self._rename(current, body)
        if command_id == CS_SAVE_ALL_AUTO_PHALANX:
            return self._save_all_auto(current, body)
        raise AssertionError(f"unreachable formation command {command_id}")

    @staticmethod
    def apply_changes(
        profile: MutableMapping[str, Any], changes: Mapping[str, Any]
    ) -> MutableMapping[str, Any]:
        """Apply one result under the caller's account lock."""

        if not isinstance(profile, MutableMapping):
            raise TypeError("profile must be a mutable mapping")
        if not isinstance(changes, Mapping):
            raise TypeError("changes must be a mapping")
        allowed = {
            FORMATION_STATE_KEY,
            "phalanxes",
            "current_phalanx_id",
            "auto_phalanxes",
        }
        unknown = set(changes) - allowed
        if unknown:
            raise FormationContentError(
                f"formation changes contain foreign keys: {sorted(unknown)}"
            )
        profile.update(copy.deepcopy(dict(changes)))
        return profile


@lru_cache(maxsize=1)
def default_layer() -> FormationContentLayer:
    return FormationContentLayer()


def handle(
    command_id: int,
    profile: Optional[Mapping[str, Any]],
    body: bytes = b"",
) -> Optional[FormationResult]:
    """Process-wide convenience wrapper for public server integration."""

    return default_layer().handle(command_id, profile, body)


__all__ = [
    "ACTIVE_COMBAT_POSITIONS",
    "BENCH_COMBAT_POSITION",
    "BattleFormationSelection",
    "CS_CHANGE_PHALANX_NAME",
    "CS_PHALANX_DATAS",
    "CS_SAVE_ALL_AUTO_PHALANX",
    "CS_SAVE_PHALANX",
    "CS_START_BATTLE",
    "CS_SWITCH_PHALANX",
    "ERROR_CLIENT_REQUEST_PARAM",
    "ERROR_HERO_NOT_FOUND",
    "ERROR_ILLEGAL_COMBAT_POS",
    "ERROR_ILLEGAL_PHALANX_ID",
    "ERROR_MESSAGE_DATA_ILLEGAL",
    "ERROR_PHALANX_NAME_ILLEGAL",
    "EXPECTED_PROTOCOL_FIELDS",
    "FORMATION_STATE_KEY",
    "FormationApplicationError",
    "FormationContentError",
    "FormationContentLayer",
    "FormationDecodeError",
    "FormationResult",
    "HERO_COUNT",
    "LILIA_HERO_UID",
    "PHALANX_COUNT",
    "REQUEST_IDS",
    "RESPONSE_IDS",
    "SC_CHANGE_PHALANX_NAME_RET",
    "SC_PHALANX_DATAS",
    "SC_SAVE_ALL_AUTO_PHALANX_RET",
    "SC_SAVE_PHALANX_RET",
    "SC_START_BATTLE_RET",
    "SC_SWITCH_PHALANX_RET",
    "default_layer",
    "handle",
]
