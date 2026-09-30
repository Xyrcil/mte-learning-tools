"""Profile and protocol support for the additional hero with ID 3000000.

The layer keeps the local profile consistent with the catalog and returns
copy-on-write changes for the caller to persist.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping, MutableMapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Iterator, Optional, Tuple, Union

try:  # Package imports in tests; fallback supports direct server execution.
    from .content_profile import DAILY_CITY_HERO_SCENE_IDS
    from .formation_names import normalize_stored_formation_name
    from .protobuf_codec import ProtocolMessageCodec
except ImportError:  # pragma: no cover - ``python server/server.py`` compatibility
    from content_profile import DAILY_CITY_HERO_SCENE_IDS  # type: ignore
    from formation_names import normalize_stored_formation_name  # type: ignore
    from protobuf_codec import ProtocolMessageCodec  # type: ignore

if TYPE_CHECKING:
    from .content_profile import ContentCatalog


UINT32_MAX = (1 << 32) - 1
UINT64_MAX = (1 << 64) - 1

LILIA_HERO_ID = 3_000_000
LILIA_HERO_UID = 1_000_000_007
LILIA_SCENE_ID = 0
VISIBLE_HERO_IDS = tuple(range(3_000_001, 3_000_007))

LILIA_PROFESSION_ID = 3_900_000
CONFLICTING_FORMAL_JOB_ID = 3_900_001
LILIA_ACTIVE_SKILL_IDS = (30_000_001, 30_000_002, 30_000_003)
LILIA_NORMAL_SKILL_ID = 30_000_001
LILIA_ULTIMATE_SKILL_IDS = (30_000_002, 30_000_004)
LILIA_QTE_SKILL_ID = 30_000_003
CONFLICTING_SKILL_QTE_ID = 30_000_013
ACTIVE_SKILL_LEVEL = 14
LILIA_DISPLAY_LEVEL = 140

# Formation limits used by the local profile:
# - five saved formations;
# - seven owned heroes per formation;
# - three active combat positions.
PHALANX_COUNT = 5
FORMATION_ROLE_COUNT = 7
ACTIVE_COMBAT_POSITIONS = (0, 1, 2)
LILIA_TEST_PHALANX_INDEX = 3

CS_CHANGE_MAIN_MODEL_ID = 235
SC_CHANGE_MAIN_MODEL_ID_RET = 236
CS_TOUCH_HERO = 321
SC_TOUCH_HERO_RET = 322

RESPONSE_IDS = {
    CS_CHANGE_MAIN_MODEL_ID: SC_CHANGE_MAIN_MODEL_ID_RET,
    CS_TOUCH_HERO: SC_TOUCH_HERO_RET,
}
REQUEST_IDS = frozenset(RESPONSE_IDS)

# Error codes shared with neighboring content layers.
ERROR_SUCCESS = 0
ERROR_HERO_NOT_FOUND = 516
ERROR_CLIENT_REQUEST_PARAM = 32_000
ERROR_MESSAGE_DATA_ILLEGAL = 32_769

# TouchHeroType uses 0 for None and 1-4 for body positions.
TOUCH_BODY_POSITIONS = frozenset({1, 2, 3, 4})

# Default main-board values used by the local profile.
BOARD_MAIN = 1
MODEL_HERO = 2
SKIN_BASE_HERO = 4
DEFAULT_KANBAN_INFO = (
    "SecretaryPosInfo:(0.0, 0.0);"
    "SecretarySpinePosInfo:(0.0, 0.0);"
    "TriggerAreaPosInfo:(0.0, 0.0);"
    "SecretaryScaleInfo:1000;formatType:2"
)

LILIA_CONTENT_REVISION = 3

# Asset names read by the defined runtime.  ``real`` entries exist under the
# requested bundle; ``ritsu_fallback`` entries are missing for 3000000 and are
# aliased by tools/patch_lilia_runtime.py only in that UI bundle.  Fight model,
# fight portraits, effects, and skills never use a fallback.
LILIA_UI_ASSET_MAP = {
    "witch_list": {
        "real": (
            "Models/3000000",
            "UI/Image/Witch/Main/ui_yc_mn_tiao_1_3900000",
        ),
        "ritsu_fallback": (),
    },
    "witch_detail": {
        "real": (
            "Models/3000000",
            "UI/Image/Icon/Badge/3000000_huizhang-bai",
            "UI/Image/Common/TeamInfo/ui_zd_baodou_3900000",
            "UI/Image/Icon/Skill/3000000_pg_icon",
            "UI/Image/Icon/Skill/3000000_ay_icon",
            "UI/Image/Icon/Skill/3000000_qte_icon",
        ),
        "ritsu_fallback": (
            "UI/Image/Icon/headicon/head_3000000_2",
            "UI/Image/Witch/Details/ui_yc_mn_tiao_1_3900000",
        ),
    },
    "avatar": {
        "real": ("UI/Image/Icon/Badge/3000000_huizhang-bai",),
        "ritsu_fallback": (
            "UI/Image/Icon/headicon/head_3000000",
            "UI/Image/Icon/headicon/head_3000000_1",
            "UI/Image/Icon/headicon/head_3000000_2",
        ),
    },
    "main_ui": {
        "real": (
            "Models/3000000",
            "UI/Image/lihui/3000000_lihui",
        ),
        "ritsu_fallback": (),
    },
    "formation": {
        "real": (
            "UI/Image/Team/ui_bd_3900000",
            "UI/Image/Common/TeamInfo/ui_zd_baodou_3900000",
        ),
        "ritsu_fallback": (
            "UI/Image/Icon/headicon/head_3000000_2",
            "UI/Image/Team/3000000_3_bd",
            "UI/Image/Team/3000000_4_bd",
            "UI/Image/Team/Main/ui_bd_di_mn_x_1_3000000",
            "UI/Image/Team/RecommendTeam/ui_yc_ck_tj_kuang_3900000",
        ),
    },
    "battle_ui": {
        "real": (
            "Models/3000000",
            "Effects/eff_fight/3000000",
            "UI/Image/Fight/Board/witch/3000000_1_zd",
            "UI/Image/Fight/Board/witch/3000000_2_zd",
            "UI/Image/Fight/Board/witch/3000000_3_zd",
            "UI/Image/Fight/Board/witch/ui_zd_baodou_3900000",
            "UI/Image/Icon/Skill/3000000_pg_icon",
            "UI/Image/Icon/Skill/3000000_ay_icon",
            "UI/Image/Icon/Skill/3000000_qte_icon",
        ),
        "ritsu_fallback": (),
    },
}

_EXACT_PROTOCOL_FIELDS = {
    "CS_ChangeMainModelId": {1: "ModelId"},
    "SC_ChangeMainModelIdRet": {1: "Errorcode", 2: "ModelId"},
    "CS_TouchHero": {1: "HeroUId", 2: "BodyPos"},
    "SC_TouchHeroRet": {1: "Errorcode", 2: "HeroUId", 3: "BodyPos"},
}

_REQUIRED_PROTOCOL_FIELDS = {
    "Hero": {
        1: "HeroUId",
        2: "HeroId",
        9: "ActiveSkillList",
        19: "State",
        22: "AppearanceId",
        27: "ProfessionId",
        37: "CombatPower",
        50: "Mood",
    },
    "ActiveSkill": {1: "Id", 2: "Level", 3: "ValidLevel"},
    "BattleHero": {1: "HeroUId", 3: "CombatPos"},
    "BoardPageItem": {
        1: "BoardType",
        2: "ModelType",
        3: "ModelId",
        4: "SkinType",
        5: "SkinId",
        6: "ShowType",
        7: "KanbanInfo",
    },
}


class LiliaContentError(ValueError):
    """Raised when Lillia's static or profile closure is unsafe."""


class LiliaRequestDecodeError(ValueError):
    """Raised when a Lillia-layer request is malformed protobuf."""


@dataclass(frozen=True)
class LiliaStaticClosure:
    """The exact static IDs selected for Lillia's local runtime state."""

    hero_id: int
    name: str
    english_name: str
    people_display: int
    profession_id: int
    active_skill_ids: Tuple[int, int, int]
    normal_skill_id: int
    ultimate_skill_ids: Tuple[int, int]
    qte_skill_id: int
    conflicting_formal_job_id: int
    conflicting_skill_qte_id: int
    model_id: int
    appearance_id: int


@dataclass(frozen=True)
class LiliaContentResult:
    """One formal response and a shallow-mergeable persistent state patch."""

    response_id: int
    body: bytes
    changes: Dict[str, Any] = field(default_factory=dict)
    error_code: int = ERROR_SUCCESS
    supported: bool = True
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.error_code == ERROR_SUCCESS


def _resolve_catalog(
    value: Optional[Union["ContentCatalog", str, Path]],
) -> "ContentCatalog":
    try:
        from .content_profile import ContentCatalog, default_content_catalog
    except ImportError:  # pragma: no cover - direct server execution
        from content_profile import (  # type: ignore
            ContentCatalog,
            default_content_catalog,
        )

    if value is None:
        return default_content_catalog()
    if isinstance(value, ContentCatalog):
        return value
    return ContentCatalog.load(value)


def _json_id_triplet(value: Any, context: str) -> Tuple[int, int, int]:
    if not isinstance(value, str):
        raise LiliaContentError(f"{context} must be a JSON-array string")
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise LiliaContentError(f"invalid {context}: {exc}") from exc
    if (
        not isinstance(parsed, list)
        or len(parsed) != 3
        or any(type(item) is not int or item <= 0 for item in parsed)
        or len(set(parsed)) != 3
    ):
        raise LiliaContentError(f"{context} must contain three unique positive IDs")
    return tuple(parsed)  # type: ignore[return-value]


def _uint(value: Any, default: int, maximum: int = UINT32_MAX) -> int:
    if isinstance(value, bool):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return parsed if 0 <= parsed <= maximum else default


def _positive_uint(value: Any, default: int, maximum: int = UINT32_MAX) -> int:
    parsed = _uint(value, 0, maximum)
    return parsed if parsed > 0 else default


def _read_varint(data: bytes, offset: int) -> Tuple[int, int]:
    value = 0
    shift = 0
    while offset < len(data) and shift < 70:
        current = data[offset]
        offset += 1
        value |= (current & 0x7F) << shift
        if not current & 0x80:
            return value, offset
        shift += 7
    raise LiliaRequestDecodeError("invalid or truncated protobuf varint")


def _iter_wire_fields(data: bytes) -> Iterator[Tuple[int, int, object]]:
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise LiliaRequestDecodeError("request body must be bytes")
    raw = bytes(data)
    offset = 0
    while offset < len(raw):
        tag, offset = _read_varint(raw, offset)
        number, wire_type = tag >> 3, tag & 7
        if number <= 0:
            raise LiliaRequestDecodeError("protobuf field number zero")
        if wire_type == 0:
            value, offset = _read_varint(raw, offset)
        elif wire_type == 1:
            end = offset + 8
            if end > len(raw):
                raise LiliaRequestDecodeError("truncated fixed64 field")
            value = raw[offset:end]
            offset = end
        elif wire_type == 2:
            size, offset = _read_varint(raw, offset)
            end = offset + size
            if end > len(raw):
                raise LiliaRequestDecodeError("truncated length-delimited field")
            value = raw[offset:end]
            offset = end
        elif wire_type == 5:
            end = offset + 4
            if end > len(raw):
                raise LiliaRequestDecodeError("truncated fixed32 field")
            value = raw[offset:end]
            offset = end
        else:
            raise LiliaRequestDecodeError(
                f"unsupported protobuf wire type {wire_type}"
            )
        yield number, wire_type, value


def _decode_change_main_model(data: bytes) -> int:
    model_id = 0
    for number, wire_type, value in _iter_wire_fields(data):
        if number != 1:
            continue
        if wire_type != 0 or not isinstance(value, int):
            raise LiliaRequestDecodeError("CS_ChangeMainModelId.ModelId is not uint32")
        if value > UINT32_MAX:
            raise LiliaRequestDecodeError("CS_ChangeMainModelId.ModelId exceeds uint32")
        model_id = value
    return model_id


def _decode_touch_hero(data: bytes) -> Tuple[int, int]:
    hero_uid = 0
    body_pos = 0
    for number, wire_type, value in _iter_wire_fields(data):
        if number not in {1, 2}:
            continue
        if wire_type != 0 or not isinstance(value, int):
            raise LiliaRequestDecodeError(
                f"CS_TouchHero field {number} is not a varint"
            )
        maximum = UINT64_MAX if number == 1 else UINT32_MAX
        if value > maximum:
            raise LiliaRequestDecodeError(
                f"CS_TouchHero field {number} exceeds its unsigned range"
            )
        if number == 1:
            hero_uid = value
        else:
            body_pos = value
    return hero_uid, body_pos


def main_board_item(model_id: int = LILIA_HERO_ID) -> Dict[str, Any]:
    """Return the decompiler-proven base-hero board mapping for ``model_id``."""

    return {
        "board_type": BOARD_MAIN,
        "model_type": MODEL_HERO,
        "model_id": int(model_id),
        "skin_type": SKIN_BASE_HERO,
        "skin_id": int(model_id),
        "show_type": 0,
        "kanban_info": DEFAULT_KANBAN_INFO,
    }


class LiliaContentLayer:
    """Validate, seed, and serve the independent Lillia content slice."""

    def __init__(
        self,
        catalog: Optional[Union["ContentCatalog", str, Path]] = None,
        codec: Optional[ProtocolMessageCodec] = None,
    ) -> None:
        self.catalog = _resolve_catalog(catalog)
        self.codec = codec or ProtocolMessageCodec()
        self._validate_protocol()
        self.closure = self._load_static_closure()

    def _validate_protocol(self) -> None:
        for message_name, expected in _EXACT_PROTOCOL_FIELDS.items():
            schema = self.codec.schema(message_name)
            actual = {
                int(row["number"]): str(row["name"])
                for row in schema.get("fields", [])
            }
            if actual != expected:
                raise LiliaContentError(
                    f"{message_name} protocol fields disagree: {actual}"
                )
        for message_name, required in _REQUIRED_PROTOCOL_FIELDS.items():
            schema = self.codec.schema(message_name)
            actual = {
                int(row["number"]): str(row["name"])
                for row in schema.get("fields", [])
            }
            mismatches = {
                number: (name, actual.get(number))
                for number, name in required.items()
                if actual.get(number) != name
            }
            if mismatches:
                raise LiliaContentError(
                    f"{message_name} protocol fields disagree: {mismatches}"
                )

    def _load_static_closure(self) -> LiliaStaticClosure:
        person = self.catalog.record("a_people", LILIA_HERO_ID)
        biography = self.catalog.record("a_people_data", LILIA_HERO_ID)
        sentinel_job = self.catalog.record("a_job", LILIA_PROFESSION_ID)
        formal_job = self.catalog.record("a_job", CONFLICTING_FORMAL_JOB_ID)
        ritsu = self.catalog.record("a_people", VISIBLE_HERO_IDS[0])

        expected_people = {
            "people_name": "莉莉娅",
            "people_display": 0,
            "job_default": LILIA_PROFESSION_ID,
            "job_id": CONFLICTING_FORMAL_JOB_ID,
            "job": CONFLICTING_FORMAL_JOB_ID,
            "skill_normal": LILIA_NORMAL_SKILL_ID,
            "skill_ub": "30000002,30000004",
            "skill_qte": CONFLICTING_SKILL_QTE_ID,
            "model": "",
        }
        mismatches = {
            name: (expected, person.get(name))
            for name, expected in expected_people.items()
            if person.get(name) != expected
        }
        if mismatches:
            raise LiliaContentError(f"a_people[3000000] disagrees: {mismatches}")

        skill_ids = _json_id_triplet(person.get("skill"), "a_people[3000000].skill")
        if skill_ids != LILIA_ACTIVE_SKILL_IDS:
            raise LiliaContentError(
                f"a_people[3000000].skill changed from {LILIA_ACTIVE_SKILL_IDS}"
            )
        if sentinel_job.get("qte") != LILIA_QTE_SKILL_ID:
            raise LiliaContentError("a_job[3900000].qte is not Lillia's 30000003")
        if formal_job.get("qte") != CONFLICTING_SKILL_QTE_ID:
            raise LiliaContentError("a_job[3900001].qte conflict is no longer 30000013")
        ritsu_skills = _json_id_triplet(ritsu.get("skill"), "a_people[3000001].skill")
        if CONFLICTING_SKILL_QTE_ID not in ritsu_skills:
            raise LiliaContentError("30000013 is no longer part of Ritsu's skill closure")

        skill_types = {
            LILIA_NORMAL_SKILL_ID: 1,
            LILIA_ULTIMATE_SKILL_IDS[0]: 3,
            LILIA_ULTIMATE_SKILL_IDS[1]: 3,
            LILIA_QTE_SKILL_ID: 2,
        }
        for skill_id, expected_type in skill_types.items():
            skill = self.catalog.record("a_skill", skill_id)
            if skill.get("upgrade_max") != ACTIVE_SKILL_LEVEL:
                raise LiliaContentError(
                    f"a_skill[{skill_id}].upgrade_max is not {ACTIVE_SKILL_LEVEL}"
                )
            if skill.get("skill_type") != expected_type:
                raise LiliaContentError(
                    f"a_skill[{skill_id}].skill_type is not {expected_type}"
                )
        self.catalog.record("a_skill", CONFLICTING_SKILL_QTE_ID)

        sentinel_groups = [
            row
            for row in self.catalog.table("a_job_skillgroup").records
            if row.get("job_id") == LILIA_PROFESSION_ID
        ]
        formal_groups = [
            row
            for row in self.catalog.table("a_job_skillgroup").records
            if row.get("job_id") == CONFLICTING_FORMAL_JOB_ID
        ]
        if sentinel_groups or len(formal_groups) != 7:
            raise LiliaContentError(
                "3900000 must have no talent groups while 3900001 has seven"
            )
        if biography.get("people_name") != "莉莉娅" or biography.get(
            "peoplename_en"
        ) != "Lillia":
            raise LiliaContentError("a_people_data[3000000] identity disagrees")

        return LiliaStaticClosure(
            hero_id=LILIA_HERO_ID,
            name="莉莉娅",
            english_name="Lillia",
            people_display=0,
            profession_id=LILIA_PROFESSION_ID,
            active_skill_ids=LILIA_ACTIVE_SKILL_IDS,
            normal_skill_id=LILIA_NORMAL_SKILL_ID,
            ultimate_skill_ids=LILIA_ULTIMATE_SKILL_IDS,
            qte_skill_id=LILIA_QTE_SKILL_ID,
            conflicting_formal_job_id=CONFLICTING_FORMAL_JOB_ID,
            conflicting_skill_qte_id=CONFLICTING_SKILL_QTE_ID,
            model_id=LILIA_HERO_ID,
            appearance_id=LILIA_HERO_ID,
        )

    @staticmethod
    def _hero_rows(profile: Mapping[str, Any]) -> list[Mapping[str, Any]]:
        rows = profile.get("heroes")
        if not isinstance(rows, Sequence) or isinstance(
            rows, (str, bytes, bytearray)
        ):
            return []
        return [row for row in rows if isinstance(row, Mapping)]

    @staticmethod
    def _interactive_counts(profile: Mapping[str, Any]) -> Dict[str, Dict[str, int]]:
        raw = profile.get("hero_interactive_counts")
        result: Dict[str, Dict[str, int]] = {}
        if not isinstance(raw, Mapping):
            return result
        for raw_uid, raw_counts in raw.items():
            uid = _uint(raw_uid, 0, UINT64_MAX)
            if not uid or not isinstance(raw_counts, Mapping):
                continue
            counts: Dict[str, int] = {}
            for raw_pos, raw_count in raw_counts.items():
                position = _uint(raw_pos, UINT32_MAX)
                count = _uint(raw_count, 0)
                if position in TOUCH_BODY_POSITIONS and count:
                    counts[str(position)] = count
            if counts:
                result[str(uid)] = counts
        return result

    def _make_lilia_hero(
        self,
        source: Optional[Mapping[str, Any]],
        counts: Mapping[str, int],
    ) -> Dict[str, Any]:
        row = dict(source) if source is not None else {}
        row.update(
            {
                "uid": LILIA_HERO_UID,
                "id": LILIA_HERO_ID,
                "profession_id": LILIA_PROFESSION_ID,
                "active_skills": [
                    {
                        "id": skill_id,
                        "level": ACTIVE_SKILL_LEVEL,
                        "valid_level": ACTIVE_SKILL_LEVEL,
                    }
                    for skill_id in LILIA_ACTIVE_SKILL_IDS
                ],
                "qte_skill_id": LILIA_QTE_SKILL_ID,
                "appearance_id": LILIA_HERO_ID,
                "birth_time": _positive_uint(row.get("birth_time"), 1, UINT64_MAX),
                "has_dated": 0,
                "combat_pos": -1,
                "scene_id": (_uint(row.get("scene_id"), UINT32_MAX)
                             if _uint(row.get("scene_id"), UINT32_MAX) in DAILY_CITY_HERO_SCENE_IDS
                             else LILIA_SCENE_ID),
                "interactive_counts": dict(counts),
            }
        )
        # Hero has no wire-level Level field.  Keep the all-unlocked profile's
        # audit mirror at 140; tools/patch_lilia_runtime.py repairs the actual
        # RoleData.Get_level path, which otherwise hard-codes job 3900000 to 1.
        row["level"] = LILIA_DISPLAY_LEVEL
        row["level_limit"] = LILIA_DISPLAY_LEVEL
        row["fake_level"] = LILIA_DISPLAY_LEVEL
        row["breakthrough_level"] = 3
        row["awakening_level"] = 5
        row["exp"] = _uint(row.get("exp"), 0)
        row["state"] = _uint(row.get("state"), 0)
        row["combat_power"] = _positive_uint(row.get("combat_power"), 100_000)
        row["fondness"] = _uint(row.get("fondness"), 0)
        row["fondness_level"] = _uint(row.get("fondness_level"), 0)
        row["fondness_daily_increment"] = _uint(
            row.get("fondness_daily_increment"), 0
        )
        row["skin_uid"] = _uint(row.get("skin_uid"), 0, UINT64_MAX)
        row["feeling"] = _uint(row.get("feeling"), 0)
        row["daily_dialog_num"] = _uint(row.get("daily_dialog_num"), 0)
        row["fondness_fake_level"] = _uint(row.get("fondness_fake_level"), 0)
        row["special_status"] = _uint(row.get("special_status"), 0)
        row["fondness_switch"] = _uint(row.get("fondness_switch"), 0, 1)
        row["mood"] = _uint(row.get("mood"), 6_000)
        row["decoration_presets_id"] = _uint(
            row.get("decoration_presets_id"), 0
        )
        row["rune_presets_id"] = _uint(row.get("rune_presets_id"), 0)
        return row

    @staticmethod
    def _formation_rows(
        profile: Mapping[str, Any], hero_rows: Sequence[Mapping[str, Any]]
    ) -> list[Dict[str, Any]]:
        raw_rows = profile.get("phalanxes")
        sources = (
            list(raw_rows)
            if isinstance(raw_rows, Sequence)
            and not isinstance(raw_rows, (str, bytes, bytearray))
            else []
        )
        all_uids = [
            _uint(row.get("uid"), 0, UINT64_MAX)
            for row in hero_rows
            if _uint(row.get("uid"), 0, UINT64_MAX)
        ]
        power_by_uid = {
            _uint(hero.get("uid"), 0, UINT64_MAX): _positive_uint(
                hero.get("combat_power"), 1
            )
            for hero in hero_rows
        }
        result = []
        for slot in range(PHALANX_COUNT):
            source = sources[slot] if slot < len(sources) else None
            row = dict(source) if isinstance(source, Mapping) else {}
            raw_uids = row.get("hero_uids")
            selected = []
            if isinstance(raw_uids, Sequence) and not isinstance(
                raw_uids, (str, bytes, bytearray)
            ):
                for raw_uid in raw_uids:
                    uid = _uint(raw_uid, 0, UINT64_MAX)
                    if (
                        uid in all_uids
                        and uid not in selected
                        and len(selected) < FORMATION_ROLE_COUNT
                    ):
                        selected.append(uid)

            # Ordinary formations enumerate every owned hero.  The captured
            # runtime save has seven entries in both HeroList and AutoList;
            # BattleData.CheckNewHero likewise appends from current RoleCount.
            for uid in all_uids:
                if uid not in selected and len(selected) < FORMATION_ROLE_COUNT:
                    selected.append(uid)

            selected = selected[:FORMATION_ROLE_COUNT]
            if len(selected) != FORMATION_ROLE_COUNT:
                raise LiliaContentError(
                    "all-unlocked profile cannot supply seven formation roles"
                )

            raw_positions = row.get("combat_positions")
            saved_positions = (
                dict(raw_positions) if isinstance(raw_positions, Mapping) else {}
            )
            positions: Dict[str, int] = {}
            used_active_positions = set()
            for index, uid in enumerate(selected):
                default = index if index in ACTIVE_COMBAT_POSITIONS else -1
                raw_position = saved_positions.get(
                    str(uid), saved_positions.get(uid, default)
                )
                try:
                    position = int(raw_position)
                except (TypeError, ValueError, OverflowError):
                    position = default
                if position not in {-1, *ACTIVE_COMBAT_POSITIONS}:
                    position = default
                if position in used_active_positions:
                    position = -1
                if position in ACTIVE_COMBAT_POSITIONS:
                    used_active_positions.add(position)
                positions[str(uid)] = position

            if slot == LILIA_TEST_PHALANX_INDEX:
                # Keep all seven candidates in their stable list order.  Only
                # move the active position: demote the prior position-2 hero
                # and activate Lillia, matching the captured ordinary-team UI.
                for key, position in positions.items():
                    if position == ACTIVE_COMBAT_POSITIONS[-1]:
                        positions[key] = -1
                positions[str(LILIA_HERO_UID)] = ACTIVE_COMBAT_POSITIONS[-1]
                used_active_positions = {
                    position
                    for position in positions.values()
                    if position in ACTIVE_COMBAT_POSITIONS
                }

            # Fill any missing active slot after malformed/duplicate persisted
            # positions.  This never creates more than the three runtime-backed
            # positions and gives Lillia position 2 in the test formation.
            missing_positions = [
                position
                for position in ACTIVE_COMBAT_POSITIONS
                if position not in used_active_positions
            ]
            for uid in selected:
                key = str(uid)
                if not missing_positions:
                    break
                if positions[key] == -1:
                    positions[key] = missing_positions.pop(0)

            formation_power = min(
                UINT32_MAX,
                sum(power_by_uid.get(uid, 1) for uid in selected),
            )
            row.update(
                {
                    "id": slot,
                    "name": normalize_stored_formation_name(row.get("name"), slot),
                    "hero_uids": selected,
                    "combat_positions": positions,
                    "combat_power": max(
                        formation_power,
                        _positive_uint(row.get("combat_power"), formation_power),
                    ),
                    "type": _uint(row.get("type"), 0),
                }
            )
            result.append(row)
        return result

    @staticmethod
    def _main_model_values(model_id: int) -> Dict[str, int]:
        return {
            "model_type": MODEL_HERO,
            "model_id": model_id,
            "skin_type": SKIN_BASE_HERO,
            "skin_id": model_id,
        }

    @staticmethod
    def _compat_board_items(
        profile: Mapping[str, Any], model_id: int
    ) -> Optional[list[Any]]:
        """Build the one-time legacy main-board initialization, if needed.

        ``CS_SaveBoardPageInfo`` owns persisted board presentation.  This
        compatibility path therefore only fills a profile that has no
        BoardType 1 row at all; an existing row is never normalized or synced
        to the independently selected main witch model.
        """

        raw = profile.get("board_items")
        rows = (
            copy.deepcopy(list(raw))
            if isinstance(raw, Sequence)
            and not isinstance(raw, (str, bytes, bytearray))
            else []
        )
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            board_type = _uint(row.get("board_type", row.get("BoardType")), 0)
            if board_type == BOARD_MAIN:
                return None
        return [main_board_item(model_id), *rows]

    def _model_changes(
        self,
        profile: Mapping[str, Any],
        model_id: int,
    ) -> Dict[str, Any]:
        candidates: Dict[str, Any] = {
            "main_model_id": model_id,
            "main_model": self._main_model_values(model_id),
            "main_board_item": main_board_item(model_id),
        }
        return {
            key: value for key, value in candidates.items() if profile.get(key) != value
        }

    @staticmethod
    def _profile_model_ids(profile: Mapping[str, Any]) -> frozenset[int]:
        return frozenset(
            _uint(row.get("id"), 0)
            for row in LiliaContentLayer._hero_rows(profile)
            if _uint(row.get("id"), 0) in {LILIA_HERO_ID, *VISIBLE_HERO_IDS}
        )

    def _current_model_id(self, profile: Mapping[str, Any]) -> int:
        valid = self._profile_model_ids(profile)
        current = _uint(profile.get("main_model_id"), 0)
        if current in valid:
            return current
        if LILIA_HERO_ID in valid:
            return LILIA_HERO_ID
        if VISIBLE_HERO_IDS[0] in valid:
            return VISIBLE_HERO_IDS[0]
        return 0

    def needs_seed(self, profile: Mapping[str, Any]) -> bool:
        """Return whether a persisted profile needs the Lillia migration.

        A current profile keeps Lillia as ordinary account state.  This check
        deliberately avoids re-seeding on every read while still repairing
        old saves, catalog migrations, duplicate rows, and broken static IDs.
        Mutable progress and interaction counters are not migration triggers.
        """

        state = profile.get("lilia_content")
        if not isinstance(state, Mapping):
            return True
        if _uint(state.get("revision"), 0) < LILIA_CONTENT_REVISION:
            return True
        if state.get("catalog_version") != self.catalog.version:
            return True
        if _uint(state.get("hero_id"), 0) != LILIA_HERO_ID:
            return True
        if _uint(state.get("hero_uid"), 0, UINT64_MAX) != LILIA_HERO_UID:
            return True

        heroes = self._hero_rows(profile)
        lilia_rows = [
            row for row in heroes if _uint(row.get("id"), 0) == LILIA_HERO_ID
        ]
        if len(lilia_rows) != 1:
            return True
        row = lilia_rows[0]
        if _uint(row.get("uid"), 0, UINT64_MAX) != LILIA_HERO_UID:
            return True
        if _uint(row.get("profession_id"), 0) != LILIA_PROFESSION_ID:
            return True
        if _uint(row.get("appearance_id"), 0) != LILIA_HERO_ID:
            return True
        if _uint(row.get("qte_skill_id"), 0) != LILIA_QTE_SKILL_ID:
            return True
        if _uint(row.get("scene_id"), UINT32_MAX) not in DAILY_CITY_HERO_SCENE_IDS:
            return True

        active_skills = row.get("active_skills")
        if not isinstance(active_skills, Sequence) or isinstance(
            active_skills, (str, bytes, bytearray)
        ):
            return True
        skill_ids = tuple(
            _uint(skill.get("id"), 0)
            for skill in active_skills
            if isinstance(skill, Mapping)
        )
        if skill_ids != LILIA_ACTIVE_SKILL_IDS:
            return True

        stable_uid_owners = sum(
            1
            for hero in heroes
            if _uint(hero.get("uid"), 0, UINT64_MAX) == LILIA_HERO_UID
        )
        return stable_uid_owners != 1

    def seed_profile(
        self, profile: MutableMapping[str, Any]
    ) -> MutableMapping[str, Any]:
        """Add Lillia as the seventh all-unlocked hero, in place.

        Reapplying the seed is a value-level no-op.  Account-owned scalar hero
        progress and touch counts are preserved, while static IDs, profession,
        skills, appearance, UID, and bench formation position are repaired.
        """

        if not isinstance(profile, MutableMapping):
            raise TypeError("profile must be a mutable mapping")
        if not profile.get("all_content_unlocked"):
            raise LiliaContentError("Lillia may only be seeded into all-unlocked profiles")

        original = self._hero_rows(profile)
        visible_ids = {_uint(row.get("id"), 0) for row in original}
        missing = set(VISIBLE_HERO_IDS) - visible_ids
        if missing:
            raise LiliaContentError(
                f"all-unlocked profile is missing visible heroes: {sorted(missing)}"
            )

        lilia_sources = [
            row for row in original if _uint(row.get("id"), 0) == LILIA_HERO_ID
        ]
        source = lilia_sources[0] if lilia_sources else None
        old_uid = _uint(source.get("uid"), 0, UINT64_MAX) if source else 0
        for row in original:
            if row is source:
                continue
            if _uint(row.get("uid"), 0, UINT64_MAX) == LILIA_HERO_UID:
                raise LiliaContentError(
                    f"stable Lillia UID {LILIA_HERO_UID} is already in use"
                )

        counts = self._interactive_counts(profile)
        if old_uid and old_uid != LILIA_HERO_UID:
            previous = counts.pop(str(old_uid), {})
            target = counts.setdefault(str(LILIA_HERO_UID), {})
            for position, count in previous.items():
                target[position] = max(target.get(position, 0), count)
        lilia_counts = counts.get(str(LILIA_HERO_UID), {})
        lilia = self._make_lilia_hero(source, lilia_counts)

        heroes = [
            dict(row)
            for row in original
            if _uint(row.get("id"), 0) != LILIA_HERO_ID
        ]
        heroes.append(lilia)
        profile["heroes"] = heroes
        profile["hero_interactive_counts"] = counts
        profile["phalanxes"] = self._formation_rows(profile, heroes)

        previous_lilia_state = profile.get("lilia_content")
        first_seed = not (
            isinstance(previous_lilia_state, Mapping)
            and _uint(previous_lilia_state.get("revision"), 0)
            >= LILIA_CONTENT_REVISION
        )
        current_model = self._current_model_id(profile)
        if first_seed:
            boards = self._compat_board_items(profile, current_model)
            if boards is not None:
                profile["board_items"] = boards
        profile.update(self._model_changes(profile, current_model))
        profile["lilia_content"] = {
            "revision": LILIA_CONTENT_REVISION,
            "catalog_version": self.catalog.version,
            "hero_id": LILIA_HERO_ID,
            "hero_uid": LILIA_HERO_UID,
            "display_level": LILIA_DISPLAY_LEVEL,
            "profession_strategy": "job_default_sentinel",
            "ui_asset_map": copy.deepcopy(LILIA_UI_ASSET_MAP),
        }
        return profile

    def _change_main_model(
        self, profile: Mapping[str, Any], request_body: bytes
    ) -> LiliaContentResult:
        current = self._current_model_id(profile)
        try:
            requested = _decode_change_main_model(request_body)
        except LiliaRequestDecodeError as exc:
            error_code = ERROR_MESSAGE_DATA_ILLEGAL
            selected = current
            changes: Dict[str, Any] = {}
            reason = str(exc)
        else:
            if requested not in self._profile_model_ids(profile):
                error_code = ERROR_CLIENT_REQUEST_PARAM
                selected = current
                changes = {}
                reason = f"model ID {requested} is not an unlocked local hero"
            else:
                error_code = ERROR_SUCCESS
                selected = requested
                changes = self._model_changes(profile, selected)
                reason = ""
        body = self.codec.encode(
            "SC_ChangeMainModelIdRet",
            {"Errorcode": error_code, "ModelId": selected},
        )
        return LiliaContentResult(
            SC_CHANGE_MAIN_MODEL_ID_RET,
            body,
            changes=changes,
            error_code=error_code,
            reason=reason,
        )

    def _touch_hero(
        self, profile: Mapping[str, Any], request_body: bytes
    ) -> LiliaContentResult:
        try:
            hero_uid, body_pos = _decode_touch_hero(request_body)
        except LiliaRequestDecodeError as exc:
            error_code = ERROR_MESSAGE_DATA_ILLEGAL
            hero_uid = 0
            body_pos = 0
            changes: Dict[str, Any] = {}
            reason = str(exc)
        else:
            hero_by_uid = {
                _uint(row.get("uid"), 0, UINT64_MAX): row
                for row in self._hero_rows(profile)
            }
            if not hero_uid or hero_uid not in hero_by_uid:
                error_code = ERROR_HERO_NOT_FOUND
                changes = {}
                reason = f"hero UID {hero_uid} is not owned"
            elif body_pos not in TOUCH_BODY_POSITIONS:
                error_code = ERROR_CLIENT_REQUEST_PARAM
                changes = {}
                reason = f"body position {body_pos} is not Head/Chest/Hand/Leg"
            else:
                error_code = ERROR_SUCCESS
                counts = self._interactive_counts(profile)
                hero_counts = counts.setdefault(str(hero_uid), {})
                hero_counts[str(body_pos)] = min(
                    UINT32_MAX,
                    hero_counts.get(str(body_pos), 0) + 1,
                )
                heroes = [dict(row) for row in self._hero_rows(profile)]
                for hero in heroes:
                    if _uint(hero.get("uid"), 0, UINT64_MAX) == hero_uid:
                        hero["interactive_counts"] = dict(hero_counts)
                        break
                changes = {
                    "hero_interactive_counts": counts,
                    "heroes": heroes,
                }
                reason = ""
        body = self.codec.encode(
            "SC_TouchHeroRet",
            {
                "Errorcode": error_code,
                "HeroUId": hero_uid,
                "BodyPos": body_pos,
            },
        )
        return LiliaContentResult(
            SC_TOUCH_HERO_RET,
            body,
            changes=changes,
            error_code=error_code,
            reason=reason,
        )

    def handle(
        self,
        request_id: int,
        profile: Mapping[str, Any],
        request_body: bytes,
    ) -> Optional[LiliaContentResult]:
        """Handle the two Lillia-relevant requests with formal responses."""

        if request_id == CS_CHANGE_MAIN_MODEL_ID:
            return self._change_main_model(profile, request_body)
        if request_id == CS_TOUCH_HERO:
            return self._touch_hero(profile, request_body)
        return None

    @staticmethod
    def apply_changes(
        profile: MutableMapping[str, Any], changes: Mapping[str, Any]
    ) -> None:
        """Apply a result patch without retaining caller-owned references."""

        profile.update(copy.deepcopy(dict(changes)))


def seed_lilia_content(
    profile: MutableMapping[str, Any],
    catalog: Optional[Union["ContentCatalog", str, Path]] = None,
) -> MutableMapping[str, Any]:
    """Convenience entry point for integrations that do not retain a layer."""

    return LiliaContentLayer(catalog).seed_profile(profile)


__all__ = [
    "ACTIVE_SKILL_LEVEL",
    "ACTIVE_COMBAT_POSITIONS",
    "BOARD_MAIN",
    "CONFLICTING_FORMAL_JOB_ID",
    "CONFLICTING_SKILL_QTE_ID",
    "CS_CHANGE_MAIN_MODEL_ID",
    "CS_TOUCH_HERO",
    "DEFAULT_KANBAN_INFO",
    "ERROR_CLIENT_REQUEST_PARAM",
    "ERROR_HERO_NOT_FOUND",
    "ERROR_MESSAGE_DATA_ILLEGAL",
    "ERROR_SUCCESS",
    "FORMATION_ROLE_COUNT",
    "LILIA_ACTIVE_SKILL_IDS",
    "LILIA_CONTENT_REVISION",
    "LILIA_DISPLAY_LEVEL",
    "LILIA_HERO_ID",
    "LILIA_HERO_UID",
    "LILIA_NORMAL_SKILL_ID",
    "LILIA_PROFESSION_ID",
    "LILIA_QTE_SKILL_ID",
    "LILIA_TEST_PHALANX_INDEX",
    "LILIA_UI_ASSET_MAP",
    "LILIA_ULTIMATE_SKILL_IDS",
    "LiliaContentError",
    "LiliaContentLayer",
    "LiliaContentResult",
    "LiliaRequestDecodeError",
    "LiliaStaticClosure",
    "MODEL_HERO",
    "PHALANX_COUNT",
    "REQUEST_IDS",
    "RESPONSE_IDS",
    "SC_CHANGE_MAIN_MODEL_ID_RET",
    "SC_TOUCH_HERO_RET",
    "SKIN_BASE_HERO",
    "TOUCH_BODY_POSITIONS",
    "main_board_item",
    "seed_lilia_content",
]
