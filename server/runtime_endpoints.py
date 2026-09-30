"""Build responses from the generated endpoint catalog.

Hand-written handlers cover login, battle, and snapshots. Other known
endpoints use schema-valid responses:

* hand-written handlers remain responsible for login, battle, and snapshots;
* every other known endpoint gets a schema-valid response instead of relying
  on the old even-command-number heuristic;
* scalar identity and resource fields come from the persistent account;
* repeated and nested fields stay empty until the catalog defines them.
"""

from __future__ import annotations

import json
import logging
import struct
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple


try:  # Works both as ``server.runtime_endpoints`` and from server.py.
    from . import generated_endpoints
    from .formation_names import (
        default_formation_name,
        is_client_valid_formation_name,
    )
except ImportError:  # pragma: no cover - exercised when running server.py directly
    import generated_endpoints  # type: ignore
    from formation_names import (  # type: ignore
        default_formation_name,
        is_client_valid_formation_name,
    )


LOG = logging.getLogger("local-game-server.endpoints")

# These writes have no implemented business rules. They must never reach
# apply_request: even logging a raw request would rewrite the account and make
# a rejected operation look like durable progress. 32704 is the source
# Msg.ErrorCode.ConfigNotFound; the offline runtime gives it a scoped explanation.
ERROR_OFFLINE_RULES_UNAVAILABLE = 32704
UNIMPLEMENTED_WRITES = {
    498: (499, "decoration_recast"),
    620: (621, "decoration_upgrade"),
    689: (690, "decoration_equip"),
    691: (692, "decoration_recast_result"),
    718: (719, "decoration_explore"),
}


def _normalize(name: str) -> str:
    return "".join(character.lower() for character in name if character.isalnum())


def _as_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _wire_varint(value: int) -> bytes:
    if value < 0:
        value &= (1 << 64) - 1
    output = bytearray()
    while value > 0x7F:
        output.append((value & 0x7F) | 0x80)
        value >>= 7
    output.append(value)
    return bytes(output)


def _wire_tag(number: int, wire_type: int) -> bytes:
    return _wire_varint((number << 3) | wire_type)


def _wire_bytes(number: int, value: bytes) -> bytes:
    return _wire_tag(number, 2) + _wire_varint(len(value)) + value


def _wire_value(number: int, type_info: Mapping[str, Any], value: Any) -> bytes:
    """Encode one scalar/message value described by the generated catalog."""

    kind = type_info.get("kind")
    if kind == "scalar" or kind == "enum":
        scalar = str(type_info.get("scalar", ""))
        if scalar in {"string"}:
            return _wire_bytes(number, str(value).encode("utf-8"))
        if scalar in {"bytes"}:
            if isinstance(value, str):
                value = value.encode("utf-8")
            return _wire_bytes(number, bytes(value))
        if scalar == "float":
            return _wire_tag(number, 5) + struct.pack("<f", float(value))
        if scalar == "double":
            return _wire_tag(number, 1) + struct.pack("<d", float(value))
        return _wire_tag(number, 0) + _wire_varint(_as_int(value))
    if kind == "message":
        return _wire_bytes(number, bytes(value))
    return b""


def _scalar_from_wire(type_info: Mapping[str, Any], value: Any) -> Any:
    kind = type_info.get("kind")
    if kind not in {"scalar", "enum"}:
        return value
    scalar = str(type_info.get("scalar", ""))
    if scalar == "string" and isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if scalar == "bytes":
        return value
    if scalar in {"float", "double"} and isinstance(value, bytes):
        try:
            return struct.unpack("<f" if scalar == "float" else "<d", value)[0]
        except struct.error:
            return 0.0
    return value


def _read_varint(data: bytes, offset: int = 0) -> Tuple[int, int]:
    value = 0
    shift = 0
    while offset < len(data) and shift < 70:
        current = data[offset]
        offset += 1
        value |= (current & 0x7F) << shift
        if not current & 0x80:
            return value, offset
        shift += 7
    raise ValueError("invalid or truncated protobuf varint")


def _decode_wire_fields(data: bytes) -> Dict[int, List[Any]]:
    """Decode the protobuf subset needed by nested formation messages."""

    fields: Dict[int, List[Any]] = {}
    offset = 0
    while offset < len(data):
        tag, offset = _read_varint(data, offset)
        number, wire_type = tag >> 3, tag & 7
        if number == 0:
            raise ValueError("protobuf field number 0")
        if wire_type == 0:
            value, offset = _read_varint(data, offset)
        elif wire_type == 1:
            end = offset + 8
            if end > len(data):
                raise ValueError("truncated fixed64 field")
            value = data[offset:end]
            offset = end
        elif wire_type == 2:
            size, offset = _read_varint(data, offset)
            end = offset + size
            if end > len(data):
                raise ValueError("truncated length-delimited field")
            value = data[offset:end]
            offset = end
        elif wire_type == 5:
            end = offset + 4
            if end > len(data):
                raise ValueError("truncated fixed32 field")
            value = data[offset:end]
            offset = end
        else:
            raise ValueError(f"unsupported protobuf wire type {wire_type}")
        fields.setdefault(number, []).append(value)
    return fields


def _first_nested_int(fields: Mapping[int, List[Any]], number: int, default: int = 0) -> int:
    values = fields.get(number)
    return _as_int(values[0], default) if values else default


def _first_nested_string(fields: Mapping[int, List[Any]], number: int, default: str = "") -> str:
    values = fields.get(number)
    if not values or not isinstance(values[0], bytes):
        return default
    return values[0].decode("utf-8", errors="replace")


def _signed_int32(value: int) -> int:
    value &= 0xFFFFFFFF
    return value - (1 << 32) if value & (1 << 31) else value


class ProtocolEndpointRuntime:
    """Load the generated catalog and provide a generic response path."""

    def __init__(self, catalog_path: Optional[Path] = None) -> None:
        if catalog_path is None:
            catalog_path = Path(__file__).resolve().parents[1] / "generated" / "protocol" / "protocol_catalog.json"
        self.catalog_path = Path(catalog_path).resolve()
        catalog = json.loads(self.catalog_path.read_text(encoding="utf-8"))
        self.catalog = catalog
        self.endpoints: Dict[int, Dict[str, Any]] = {
            int(row["request_id"]): row for row in catalog.get("endpoints", [])
        }
        self.request_to_response: Dict[int, int] = {
            int(key): int(value)
            for key, value in catalog.get("request_to_response", {}).items()
        }
        # Importing generated_endpoints here is intentional: the runtime
        # verifies that the generated registry and JSON catalog agree before
        # serving traffic, instead of silently using a stale generated file.
        if set(self.endpoints) != set(generated_endpoints.ENDPOINTS):
            raise RuntimeError("generated endpoint registry does not match protocol catalog")
        self.messages: Dict[str, Dict[str, Any]] = {
            item["name"]: item for item in catalog.get("message_types", [])
        }
        self.handled_count = 0
        self.generic_count = 0

    def endpoint_for(self, command_id: int) -> Optional[Dict[str, Any]]:
        return self.endpoints.get(int(command_id))

    def response_id_for(self, command_id: int) -> Optional[int]:
        endpoint = self.endpoint_for(command_id)
        if endpoint is not None:
            response_id = endpoint.get("response_id")
            return int(response_id) if response_id is not None else None
        return self.request_to_response.get(int(command_id))

    def reject_unimplemented_write(self, handler: Any, command_id: int) -> bool:
        """Reject an unsupported write without parsing or opening any account."""
        blocked = UNIMPLEMENTED_WRITES.get(command_id)
        if blocked is None:
            return False
        response_id, feature = blocked
        if self.response_id_for(command_id) != response_id:
            raise RuntimeError(f"unsupported write response mismatch: {command_id}")
        handler.send(
            response_id,
            _wire_value(1, {"kind": "scalar", "scalar": "uint32"},
                        ERROR_OFFLINE_RULES_UNAVAILABLE),
        )
        LOG.info("unsupported offline write %d -> %d, feature=%s, error=%d",
                 command_id, response_id, feature, ERROR_OFFLINE_RULES_UNAVAILABLE)
        return True

    def _message_schema(self, names: Sequence[str]) -> Optional[Dict[str, Any]]:
        for name in names:
            if name in self.messages:
                return self.messages[name]
        return None

    def _request_values(
        self,
        endpoint: Mapping[str, Any],
        wire_fields: Mapping[int, List[Any]],
    ) -> Dict[str, Any]:
        values: Dict[str, Any] = {}
        schema = self._message_schema(endpoint.get("request_message_types", []))
        if not schema:
            return values
        for field in schema.get("fields", []):
            number = int(field.get("number", 0))
            raw_values = wire_fields.get(number)
            if not raw_values:
                continue
            type_info = field.get("type", {})
            if type_info.get("kind") == "repeated":
                arguments = type_info.get("arguments", [])
                element = arguments[0] if arguments else {}
                values[_normalize(field["name"])] = [
                    _scalar_from_wire(element, raw) for raw in raw_values
                ]
            else:
                values[_normalize(field["name"])] = _scalar_from_wire(type_info, raw_values[0])
        return values

    def _profile_value(
        self,
        field_name: str,
        profile: Mapping[str, Any],
        request_values: Mapping[str, Any],
    ) -> Any:
        name = _normalize(field_name)
        if name in {"errorcode", "error", "resultcode"}:
            return 0
        if name in {"roleid", "sender", "ownerroleid", "destroleid"}:
            return _as_int(profile.get("role_id"))
        if name in {"rolename", "name"}:
            if name in request_values:
                return request_values[name]
            return str(profile.get("role_name") or "Local Guest")
        if name in {"account", "openid", "userid", "uid"}:
            # UIDs are not guessed from the account string.  If the request
            # supplied a matching scalar, echo it; otherwise use role id for
            # the fields that the runtime treats as the local owner.
            if name in request_values:
                return request_values[name]
            return _as_int(profile.get("role_id")) if name in {"userid", "uid"} else "local"
        aliases = {
            "level": "level",
            "viplevel": "vip_level",
            "exp": "exp",
            "diamond": "diamond",
            "gold": "gold",
            "energy": "energy",
            "minidiamond": "minidiamond",
            "currentphalanxid": "current_phalanx_id",
            "phalanxid": "current_phalanx_id",
            "dungeonid": "last_dungeon_id",
            "targetid": "last_target_id",
        }
        if name in aliases and aliases[name] in profile:
            return profile[aliases[name]]
        if name in {"isfirstpass", "battleverifyresult", "success", "issuccess"}:
            return True
        if name in {"result", "battleresult"}:
            return 1
        if name in request_values and name not in {"errorcode", "resultcode"}:
            return request_values[name]
        return None

    def build_response(
        self,
        endpoint: Mapping[str, Any],
        profile: Mapping[str, Any],
        request_values: Mapping[str, Any],
        wire_fields: Optional[Mapping[int, List[Any]]] = None,
    ) -> bytes:
        """Build conservative schema-valid protobuf bytes for one endpoint."""

        if endpoint.get("request_id") in UNIMPLEMENTED_WRITES:
            return _wire_value(1, {"kind": "scalar", "scalar": "uint32"},
                               ERROR_OFFLINE_RULES_UNAVAILABLE)

        request_name = str(endpoint.get("request_name") or "")
        raw_fields = wire_fields or {}
        if request_name == "CS_SwitchPhalanx":
            return _wire_value(2, {"kind": "scalar", "scalar": "uint32"}, request_values.get("phalanxid", 0)) + _wire_value(
                3,
                {"kind": "scalar", "scalar": "uint32"},
                request_values.get("phatype", 0),
            )
        if request_name == "CS_ChangePhalanxName":
            return _wire_value(2, {"kind": "scalar", "scalar": "uint32"}, request_values.get("phalanxid", 0)) + _wire_value(
                3,
                {"kind": "scalar", "scalar": "string"},
                request_values.get("name", ""),
            )
        if request_name == "CS_SavePhalanx":
            response = bytearray()
            response.extend(
                _wire_value(
                    2,
                    {"kind": "scalar", "scalar": "uint32"},
                    request_values.get("phalanxid", 0),
                )
            )
            for source_number, response_number in ((2, 3), (4, 5)):
                values = raw_fields.get(source_number, [])
                if values and isinstance(values[0], bytes):
                    response.extend(_wire_bytes(response_number, values[0]))
            response.extend(
                _wire_value(
                    4,
                    {"kind": "scalar", "scalar": "uint32"},
                    request_values.get("phatype", 0),
                )
            )
            response.extend(
                _wire_value(
                    6,
                    {"kind": "scalar", "scalar": "uint32"},
                    request_values.get("settype", 0),
                )
            )
            return bytes(response)
        if request_name == "CS_SetCombatHero":
            response = bytearray(
                _wire_value(
                    2,
                    {"kind": "scalar", "scalar": "uint32"},
                    request_values.get("phalanxid", 0),
                )
            )
            for value in raw_fields.get(2, []):
                if isinstance(value, bytes):
                    response.extend(_wire_bytes(3, value))
            return bytes(response)
        if request_name == "CS_SaveAllAutoPhalanx":
            response = bytearray()
            for value in raw_fields.get(1, []):
                if isinstance(value, bytes):
                    response.extend(_wire_bytes(2, value))
            return bytes(response)

        schema = self._message_schema(endpoint.get("response_message_types", []))
        if schema is None:
            # The scanner retained opaque endpoints explicitly.  They still
            # receive their paired command ID, but without inventing a schema.
            return b""
        body = bytearray()
        for field in schema.get("fields", []):
            type_info = field.get("type", {})
            if type_info.get("kind") in {"repeated", "map", "message"}:
                continue
            value = self._profile_value(field.get("name", ""), profile, request_values)
            if value is None:
                continue
            encoded = _wire_value(int(field["number"]), type_info, value)
            if encoded:
                body.extend(encoded)
        return bytes(body)

    def apply_request(
        self,
        state: Any,
        account: str,
        endpoint: Mapping[str, Any],
        body: bytes,
        wire_fields: Mapping[int, List[Any]],
    ) -> Dict[str, Any]:
        """Apply small, generic state transitions inferred from message names."""

        request_values = self._request_values(endpoint, wire_fields)
        request_name = str(endpoint.get("request_name") or "")

        def mutate(profile: Dict[str, Any]) -> None:
            def formation_for(phalanx_id: int) -> Dict[str, Any]:
                formations = profile.get("phalanxes")
                if not isinstance(formations, list):
                    formations = []
                    profile["phalanxes"] = formations
                for candidate in formations:
                    if isinstance(candidate, dict) and _as_int(candidate.get("id"), -1) == phalanx_id:
                        return candidate
                hero_uids = [
                    _as_int(hero.get("uid"), 0)
                    for hero in profile.get("heroes", [])
                    if isinstance(hero, Mapping) and _as_int(hero.get("uid"), 0)
                ]
                candidate = {
                    "id": phalanx_id,
                    "name": default_formation_name(phalanx_id),
                    "hero_uids": hero_uids,
                    "combat_power": 100,
                    "type": 0,
                }
                formations.append(candidate)
                formations.sort(key=lambda row: _as_int(row.get("id"), 0) if isinstance(row, Mapping) else 0)
                return candidate

            def nested(value: Any) -> Dict[int, List[Any]]:
                if not isinstance(value, bytes):
                    return {}
                try:
                    return _decode_wire_fields(value)
                except ValueError as exc:
                    LOG.warning("invalid nested %s payload: %s", request_name, exc)
                    return {}

            def decode_hero_rows(values: Any) -> List[Tuple[int, int]]:
                if not isinstance(values, list):
                    return []
                rows = []
                for raw in values:
                    hero_fields = nested(raw)
                    uid = _first_nested_int(hero_fields, 1, 0)
                    position = _signed_int32(_first_nested_int(hero_fields, 2, -1))
                    if uid:
                        rows.append((uid, position))
                return rows

            if "ChangeName" in request_name:
                new_name = request_values.get("name") or request_values.get("rolename")
                if isinstance(new_name, str) and new_name.strip():
                    profile["role_name"] = new_name.strip()[:32]
            if request_name == "CS_SwitchPhalanx":
                phalanx_id = max(0, min(_as_int(request_values.get("phalanxid"), 0), 4))
                profile["current_phalanx_id"] = phalanx_id
            if request_name == "CS_SavePhalanx":
                phalanx_id = max(0, min(_as_int(request_values.get("phalanxid"), 0), 4))
                formation = formation_for(phalanx_id)
                pha_fields = nested(request_values.get("pha"))
                known_uids = {
                    _as_int(hero.get("uid"), 0)
                    for hero in profile.get("heroes", [])
                    if isinstance(hero, Mapping)
                }
                hero_rows = []
                for raw in pha_fields.get(1, []):
                    battle_hero = nested(raw)
                    uid = _first_nested_int(battle_hero, 1, 0)
                    position = _signed_int32(_first_nested_int(battle_hero, 3, -1))
                    if uid and uid in known_uids and uid not in {item[0] for item in hero_rows}:
                        hero_rows.append((uid, position))
                if hero_rows:
                    formation["hero_uids"] = [uid for uid, _ in hero_rows]
                    formation["combat_positions"] = {
                        str(uid): position for uid, position in hero_rows
                    }
                # Keep the generic fallback aligned with TeamView's exact
                # contract.  The dedicated formation layer normally handles
                # command 82 first, but a fallback must not persist a value
                # that command 214 cannot round-trip through the runtime.
                name = _first_nested_string(pha_fields, 2, "")
                if is_client_valid_formation_name(name):
                    formation["name"] = name
                combat_power = _first_nested_int(pha_fields, 3, 0)
                if combat_power:
                    formation["combat_power"] = combat_power
                formation["type"] = max(0, _as_int(request_values.get("phatype"), 0))
                auto_fields = nested(request_values.get("phaautofight"))
                if auto_fields:
                    auto_id = max(0, min(_first_nested_int(auto_fields, 1, phalanx_id), 4))
                    auto_rows = decode_hero_rows(auto_fields.get(2, []))
                    profile.setdefault("auto_phalanxes", {})[str(auto_id)] = [
                        {"hero_uid": uid, "combat_pos": position}
                        for uid, position in auto_rows
                    ]
                profile["current_phalanx_id"] = phalanx_id
            if request_name == "CS_SetCombatHero":
                phalanx_id = max(0, min(_as_int(request_values.get("phalanxid"), 0), 4))
                formation = formation_for(phalanx_id)
                combat_rows = decode_hero_rows(request_values.get("herolist"))
                if combat_rows:
                    known_uids = {
                        _as_int(hero.get("uid"), 0)
                        for hero in profile.get("heroes", [])
                        if isinstance(hero, Mapping)
                    }
                    combat_rows = [row for row in combat_rows if row[0] in known_uids]
                    combat_uids = [uid for uid, _ in sorted(combat_rows, key=lambda row: row[1])]
                    existing = formation.get("hero_uids")
                    existing = existing if isinstance(existing, list) else []
                    formation["hero_uids"] = combat_uids + [
                        _as_int(uid, 0)
                        for uid in existing
                        if _as_int(uid, 0) in known_uids and _as_int(uid, 0) not in combat_uids
                    ]
                    formation["combat_positions"] = {
                        str(uid): position for uid, position in combat_rows
                    }
            if request_name == "CS_ChangePhalanxName":
                phalanx_id = max(0, min(_as_int(request_values.get("phalanxid"), 0), 4))
                name = request_values.get("name")
                if is_client_valid_formation_name(name):
                    formation_for(phalanx_id)["name"] = name
            if request_name == "CS_SaveAllAutoPhalanx":
                auto_state: Dict[str, List[Dict[str, int]]] = {}
                for raw in request_values.get("phaautofight", []):
                    auto_fields = nested(raw)
                    auto_id = max(0, min(_first_nested_int(auto_fields, 1, 0), 4))
                    auto_state[str(auto_id)] = [
                        {"hero_uid": uid, "combat_pos": position}
                        for uid, position in decode_hero_rows(auto_fields.get(2, []))
                    ]
                if auto_state:
                    profile["auto_phalanxes"] = auto_state
            if "UnlockTeachGuide" in request_name:
                guide_id = _as_int(request_values.get("teachguideid"), 0)
                if guide_id and guide_id not in profile.setdefault("unlocked_guides", []):
                    profile["unlocked_guides"].append(guide_id)
            if request_name == "CS_AddBranchGuideStep":
                # Branch-guide IDs live in the sparse completion map.  They
                # are not the scalar tutorial resume cursor, and this message
                # calls its field simply ``Id`` (not GuideId/StepId).
                branch_id = _as_int(request_values.get("id"), 0)
                if branch_id:
                    profile.setdefault("guide_steps", {})[str(branch_id)] = True
            elif "GuideStep" in request_name or "TeachStep" in request_name:
                step_id = _as_int(
                    request_values.get("teachstepid")
                    or request_values.get("stepid")
                    or request_values.get("guideid"),
                    0,
                )
                if step_id:
                    profile.setdefault("guide_steps", {})[str(step_id)] = True
                    # CS_GuideStepMove is the authoritative resume cursor.  A
                    # set of observed IDs is useful for diagnostics, but the
                    # role-selection response needs one scalar GuideStepId.
                    profile["guide_step"] = max(
                        _as_int(profile.get("guide_step"), 0),
                        step_id,
                    )
            if "Dungeon" in request_name and "Request" not in request_name:
                dungeon_id = _as_int(request_values.get("dungeonid"), 0)
                if dungeon_id:
                    profile["last_dungeon_id"] = dungeon_id
            if "RequestTaskBattleDatas" in request_name:
                profile.setdefault("task_battles", {})["current_cycle_type"] = _as_int(
                    request_values.get("taskcycletype"), 0
                )
            if "StartTaskBattle" in request_name:
                profile.setdefault("task_battles", {}).update(
                    {
                        "current_cycle_type": _as_int(request_values.get("taskcycletype"), 0),
                        "hero_id": _as_int(request_values.get("heroid"), 0),
                        "enemy_level": _as_int(request_values.get("enemylevel"), 1),
                        "main_task_id": _as_int(request_values.get("maintaskid"), 0),
                        "activity_task_id": _as_int(request_values.get("activitytaskid"), 0),
                    }
                )
            if "EndTaskBattle" in request_name:
                profile.setdefault("task_battles", {}).update(
                    {
                        "current_cycle_type": _as_int(request_values.get("taskcycletype"), 0),
                        "hero_id": _as_int(request_values.get("heroid"), 0),
                        "enemy_level": _as_int(request_values.get("enemylevel"), 1),
                        "last_result": 1,
                    }
                )
            if "StartBattle" in request_name or "EndBattle" in request_name:
                dungeon_id = _as_int(request_values.get("dungeonid"), 0)
                target_id = _as_int(request_values.get("targetid"), 0)
                if dungeon_id:
                    profile["last_dungeon_id"] = dungeon_id
                if target_id:
                    profile["last_target_id"] = target_id
                if "EndBattle" in request_name and dungeon_id:
                    result = _as_int(request_values.get("result"), 1)
                    completed_before = str(dungeon_id) in profile.setdefault("completed_battles", {})
                    profile.setdefault("completed_battles", {})[str(dungeon_id)] = {
                        "result": result,
                        "target_id": target_id,
                        "updated_at": int(profile.get("updated_at") or 0),
                    }
                    if result:
                        completed = profile.setdefault("completed_dungeons", [])
                        if dungeon_id not in completed:
                            completed.append(dungeon_id)
                        if dungeon_id == 8600013:
                            profile["tutorial_completed"] = True
                        if not completed_before:
                            reward_count = 10
                            profile["gold"] = _as_int(profile.get("gold"), 0) + reward_count
                            profile.setdefault("reward_history", []).append(
                                {
                                    "source": "battle",
                                    "dungeon_id": dungeon_id,
                                    "res_type": 2,
                                    "item_id": 0,
                                    "count": reward_count,
                                }
                            )
            profile.setdefault("raw_requests", {})[request_name or str(endpoint.get("request_id"))] = body.hex()

        profile = state.store.mutate_account(account, mutate)
        return profile

    def observe_special(
        self,
        state: Any,
        account: str,
        command_id: int,
        body: bytes,
        wire_fields: Mapping[int, List[Any]],
    ) -> None:
        """Record state transitions for endpoints with manual payloads.

        Manual handlers are intentionally evaluated before ``handle``.  This
        hook lets their richer response builders still participate in the
        same persistent state model.
        """

        endpoint = self.endpoint_for(command_id)
        if endpoint is None:
            return
        name = str(endpoint.get("request_name") or "")
        if any(
            token in name
            for token in (
                "StartBattle",
                "EndBattle",
                "StartTaskBattle",
                "EndTaskBattle",
                "GuideStep",
                "UnlockTeachGuide",
            )
        ):
            self.apply_request(state, account, endpoint, body, wire_fields)

    def handle(self, handler: Any, command_id: int, body: bytes, wire_fields: Mapping[int, List[Any]]) -> bool:
        """Serve an unhandled known endpoint through the generated catalog."""

        if self.reject_unimplemented_write(handler, command_id):
            return True

        # These commands now require the dedicated transaction layer. If a
        # catalog-free runtime reaches this fallback, do not issue an empty
        # success or mutate raw request bookkeeping.
        if command_id in (622,624,626,628,630,632,634,636,638,640,642,644,660,947,949):
            handler.send(command_id + 1, _wire_value(
                1, {"kind": "scalar", "scalar": "uint32"}, ERROR_OFFLINE_RULES_UNAVAILABLE))
            return True

        endpoint = self.endpoint_for(command_id)
        if endpoint is None:
            return False
        response_id = self.response_id_for(command_id)
        if response_id is None:
            return False
        profile = self.apply_request(handler.state, handler.account, endpoint, body, wire_fields)
        request_values = self._request_values(endpoint, wire_fields)
        response_body = self.build_response(
            endpoint,
            profile,
            request_values,
            wire_fields,
        )
        handler.send(response_id, response_body)
        self.generic_count += 1
        LOG.info(
            "generated endpoint %d (%s) -> %d (%s), policy=%s, body=%d",
            command_id,
            endpoint.get("request_name", "unknown"),
            response_id,
            endpoint.get("response_name", "unknown"),
            endpoint.get("policy", "unknown"),
            len(response_body),
        )
        return True
