"""Pure state handlers for task, activity, fast, and local maze battles.

The socket server is intentionally not imported here.  ``handle`` accepts a
profile snapshot and returns a shallow-mergeable patch; callers commit that
patch in their existing account transaction.  Command IDs, message names and
wire schemas are loaded from ``protocol_catalog.json`` rather than duplicated
as handwritten protobuf definitions.
"""

from __future__ import annotations

import copy
import hashlib
import json
import time
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import (
    Any,
    Dict,
    Iterable,
    Iterator,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)

try:  # Package imports in tests; fallbacks support direct server execution.
    from .battle_settlement import (
        BattleSettlementLayer,
        Reward,
        aggregate,
    )
    from .content_profile import ContentCatalog
    from .protobuf_codec import ProtocolMessageCodec
except ImportError:  # pragma: no cover
    from battle_settlement import BattleSettlementLayer, Reward, aggregate  # type: ignore
    from content_profile import ContentCatalog  # type: ignore
    from protobuf_codec import ProtocolMessageCodec  # type: ignore


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONTENT_DIR = ROOT / "generated" / "content"
DEFAULT_PROTOCOL_CATALOG = ROOT / "generated" / "protocol" / "protocol_catalog.json"

CS_START_TASK_BATTLE = 438
CS_END_TASK_BATTLE = 440
CS_REQUEST_TASK_BATTLE_DATAS = 444
CS_START_ACTIVITY_TASK_BATTLE = 972
CS_FAST_BATTLE_SETUP = 974
CS_START_FAST_BATTLE = 976
CS_END_FAST_BATTLE = 978
CS_REQUEST_MAZE_DATAS = 983
CS_MATCH_MAZE = 1003
CS_MAZE_BATTLE_END = 1020
CS_GIVEUP_FIGHT_MAZE = 1024
CS_REQUEST_MAZE_SOCIAL_DATAS = 1056
CS_REQUEST_MAZE_PARAM_DATAS = 1063
CS_ENTER_MAZE_BATTLE = 1069
CS_REQUEST_MAZE_BATTLE_REPORT = 1102
CS_ON_MAZE_MAIN_VIEW = 1164

RESPONSE_IDS = {
    CS_START_TASK_BATTLE: 439,
    CS_END_TASK_BATTLE: 441,
    CS_REQUEST_TASK_BATTLE_DATAS: 445,
    CS_START_ACTIVITY_TASK_BATTLE: 973,
    CS_FAST_BATTLE_SETUP: 975,
    CS_START_FAST_BATTLE: 977,
    CS_END_FAST_BATTLE: 979,
    CS_REQUEST_MAZE_DATAS: 984,
    CS_MATCH_MAZE: 1004,
    CS_MAZE_BATTLE_END: 1021,
    CS_GIVEUP_FIGHT_MAZE: 1025,
    CS_REQUEST_MAZE_SOCIAL_DATAS: 1057,
    CS_REQUEST_MAZE_PARAM_DATAS: 1064,
    CS_ENTER_MAZE_BATTLE: 1070,
    CS_REQUEST_MAZE_BATTLE_REPORT: 1103,
    CS_ON_MAZE_MAIN_VIEW: 1165,
}

REQUEST_NAMES = {
    CS_START_TASK_BATTLE: "CS_StartTaskBattle",
    CS_END_TASK_BATTLE: "CS_EndTaskBattle",
    CS_REQUEST_TASK_BATTLE_DATAS: "CS_RequestTaskBattleDatas",
    CS_START_ACTIVITY_TASK_BATTLE: "CS_StartActivityTaskBattle",
    CS_FAST_BATTLE_SETUP: "CS_FastBattleSetup",
    CS_START_FAST_BATTLE: "CS_StartFastBattle",
    CS_END_FAST_BATTLE: "CS_EndFastBattle",
    CS_REQUEST_MAZE_DATAS: "CS_RequestMazeDatas",
    CS_MATCH_MAZE: "CS_MatchMaze",
    CS_MAZE_BATTLE_END: "CS_MazeBattleEnd",
    CS_GIVEUP_FIGHT_MAZE: "CS_GiveupFightMaze",
    CS_REQUEST_MAZE_SOCIAL_DATAS: "CS_RequestMazeSocialDatas",
    CS_REQUEST_MAZE_PARAM_DATAS: "CS_RequestMazeParamDatas",
    CS_ENTER_MAZE_BATTLE: "CS_EnterMazeBattle",
    CS_REQUEST_MAZE_BATTLE_REPORT: "CS_RequestMazeBattleReport",
    CS_ON_MAZE_MAIN_VIEW: "CS_OnMazeMainView",
}

RESPONSE_NAMES = {
    439: "SC_StartTaskBattleRet",
    441: "SC_EndTaskBattleRet",
    445: "SC_TaskBattleDatasRet",
    973: "SC_StartActivityTaskBattleRet",
    975: "SC_FastBattleSetupRet",
    977: "SC_StartFastBattleRet",
    979: "SC_EndFastBattleRet",
    984: "SC_MazeDatasRet",
    1004: "SC_MatchMazeRet",
    1021: "SC_MazeBattleEndRet",
    1025: "SC_GiveupFightMazeRet",
    1057: "SC_MazeSocialDatasRet",
    1064: "SC_MazeParamDatasRet",
    1070: "SC_EnterMazeBattleRet",
    1103: "SC_MazeBattleReportRet",
    1165: "SC_OnMazeMainViewRet",
}

OPAQUE_RESPONSE_IDS = frozenset({1165})

ERROR_SUCCESS = 0
ERROR_CLIENT_REQUEST_PARAM = 32000
ERROR_MESSAGE_DATA_ILLEGAL = 32769
ERROR_CONFIG = 32772
BATTLE_WIN = 1
STATE_KEY = "special_battles"
MAX_FAST_FIGHTS = 100
MAZE_SEASON_ID = 3_000_306
MAZE_SCENE_ID = 3_000_501
MAZE_BUFF_ID = 3_000_417
MAZE_GOBLIN_ID = 3_100_160
MAZE_GOBLIN_UID = 90_001
MAZE_INITIAL_SCORE = 200
MAZE_SCORE_DELTA = 20


class SpecialBattleConfigError(ValueError):
    """Raised when generated tables or protocol mappings are inconsistent."""


class ProtobufDecodeError(ValueError):
    """Raised for malformed catalog-described protobuf input."""


@dataclass(frozen=True)
class Result:
    response_id: int
    body: bytes
    changes: Dict[str, Any] = field(default_factory=dict)
    rewards: Tuple[Reward, ...] = ()
    error_code: int = ERROR_SUCCESS
    replayed: bool = False
    supported: bool = True
    reason: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.error_code == ERROR_SUCCESS


def _int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return int(value)
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _uint(value: Any, default: int = 0) -> int:
    return max(0, _int(value, default))


def _mapping(value: Any) -> Mapping[Any, Any]:
    return value if isinstance(value, Mapping) else {}


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
    raise ProtobufDecodeError("invalid or truncated protobuf varint")


def _wire_fields(data: bytes) -> Iterator[Tuple[int, int, object]]:
    offset = 0
    while offset < len(data):
        tag, offset = _read_varint(data, offset)
        number, wire_type = tag >> 3, tag & 7
        if number <= 0:
            raise ProtobufDecodeError("protobuf field number zero")
        if wire_type == 0:
            value, offset = _read_varint(data, offset)
        elif wire_type == 1:
            end = offset + 8
            if end > len(data):
                raise ProtobufDecodeError("truncated fixed64 field")
            value, offset = data[offset:end], end
        elif wire_type == 2:
            size, offset = _read_varint(data, offset)
            end = offset + size
            if end > len(data):
                raise ProtobufDecodeError("truncated length-delimited field")
            value, offset = data[offset:end], end
        elif wire_type == 5:
            end = offset + 4
            if end > len(data):
                raise ProtobufDecodeError("truncated fixed32 field")
            value, offset = data[offset:end], end
        else:
            raise ProtobufDecodeError(f"unsupported protobuf wire type {wire_type}")
        yield number, wire_type, value


class CatalogCodec:
    """Catalog-backed encoder plus the matching request decoder."""

    def __init__(self, catalog_path: Path = DEFAULT_PROTOCOL_CATALOG) -> None:
        self.encoder = ProtocolMessageCodec(Path(catalog_path))
        self.messages = self.encoder.messages

    def encode(self, message_name: str, values: Mapping[str, Any]) -> bytes:
        return self.encoder.encode(message_name, values)

    def decode(self, message_name: str, data: bytes) -> Dict[str, Any]:
        schema = self.encoder.schema(message_name)
        fields = {int(row["number"]): row for row in schema.get("fields", [])}
        result: Dict[str, Any] = {}
        for number, wire_type, raw in _wire_fields(bytes(data)):
            field_schema = fields.get(number)
            if field_schema is None:
                continue
            name = str(field_schema["name"])
            type_info = field_schema.get("type", {})
            if type_info.get("kind") == "map":
                if wire_type != 2 or not isinstance(raw, bytes):
                    raise ProtobufDecodeError(f"{message_name}.{name} is malformed")
                arguments = type_info.get("arguments") or []
                if len(arguments) != 2:
                    raise ProtobufDecodeError(
                        f"{message_name}.{name} has no map schema"
                    )
                entry: Dict[int, Any] = {}
                for entry_number, entry_wire, entry_raw in _wire_fields(raw):
                    if entry_number in {1, 2}:
                        entry[entry_number] = self._decode_value(
                            arguments[entry_number - 1],
                            entry_raw,
                            entry_wire,
                            f"{name}[{entry_number}]",
                        )
                if 1 in entry:
                    result.setdefault(name, {})[entry[1]] = entry.get(2, 0)
                continue
            if type_info.get("kind") == "repeated":
                argument = (type_info.get("arguments") or [{}])[0]
                values = result.setdefault(name, [])
                if (
                    argument.get("kind") in {"scalar", "enum"}
                    and argument.get("scalar") not in {"string", "bytes"}
                    and wire_type == 2
                ):
                    if not isinstance(raw, bytes):
                        raise ProtobufDecodeError(f"{message_name}.{name} is malformed")
                    packed_offset = 0
                    while packed_offset < len(raw):
                        item, packed_offset = _read_varint(raw, packed_offset)
                        values.append(self._decode_scalar(argument, item, 0, name))
                else:
                    values.append(self._decode_value(argument, raw, wire_type, name))
                continue
            result[name] = self._decode_value(type_info, raw, wire_type, name)
        return result

    def _decode_value(
        self, type_info: Mapping[str, Any], raw: object, wire_type: int, context: str
    ) -> Any:
        kind = type_info.get("kind")
        if kind == "message":
            if wire_type != 2 or not isinstance(raw, bytes):
                raise ProtobufDecodeError(f"{context} must be length-delimited")
            return self.decode(str(type_info.get("type") or ""), raw)
        if kind in {"scalar", "enum"}:
            return self._decode_scalar(type_info, raw, wire_type, context)
        raise ProtobufDecodeError(f"unsupported request field kind {kind!r}")

    @staticmethod
    def _decode_scalar(
        type_info: Mapping[str, Any], raw: object, wire_type: int, context: str
    ) -> Any:
        scalar = str(type_info.get("scalar") or "")
        if scalar in {"string", "bytes"}:
            if wire_type != 2 or not isinstance(raw, bytes):
                raise ProtobufDecodeError(f"{context} must be length-delimited")
            return raw.decode("utf-8") if scalar == "string" else raw
        if wire_type != 0 or not isinstance(raw, int):
            raise ProtobufDecodeError(f"{context} must be a varint")
        if scalar in {"int32", "sint32"} and raw & (1 << 31):
            raw -= 1 << 32
        elif scalar in {"int64", "sint64"} and raw & (1 << 63):
            raw -= 1 << 64
        return bool(raw) if scalar == "bool" else raw


def _json_sequence(value: Any, label: str) -> List[Any]:
    if value in (None, ""):
        return []
    if isinstance(value, list):
        return value
    if not isinstance(value, str):
        raise SpecialBattleConfigError(f"{label} must be JSON text")
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as exc:
        raise SpecialBattleConfigError(f"{label} is invalid JSON") from exc
    if not isinstance(decoded, list):
        raise SpecialBattleConfigError(f"{label} must decode to a list")
    return decoded


def _interval(value: Any, label: str) -> Tuple[int, int]:
    values = _json_sequence(value, label)
    if len(values) != 2:
        raise SpecialBattleConfigError(f"{label} must contain two bounds")
    lower, upper = (_int(values[0], -1), _int(values[1], -1))
    if lower < 0 or upper <= lower:
        raise SpecialBattleConfigError(f"{label} has invalid bounds")
    return lower, upper


def _rewards(value: Any, label: str) -> Tuple[Reward, ...]:
    rewards: List[Reward] = []
    for index, raw in enumerate(_json_sequence(value, label)):
        if not isinstance(raw, Mapping):
            raise SpecialBattleConfigError(f"{label}[{index}] must be an object")
        reward = Reward(
            _uint(raw.get("type")),
            _uint(raw.get("id")),
            _uint(raw.get("num")),
            _uint(raw.get("para")),
            _uint(raw.get("position")),
            _uint(raw.get("quality")),
            _uint(raw.get("value")),
        )
        if reward.count <= 0:
            raise SpecialBattleConfigError(f"{label}[{index}].num must be positive")
        rewards.append(reward)
    return tuple(rewards)


class SpecialBattleCatalog:
    """Validated view of task-combat validation in generated content."""

    REQUIRED = {
        "a_task_people": {"id", "people_id", "people_attack", "people_hp", "task_type"},
        "a_task_enemy": {
            "id",
            "cycle_type",
            "enemy_id",
            "enemy_level",
            "enemy_attcak",
            "enemy_hp",
            "reward",
        },
        "a_task_probability": {"id", "we_atk", "monster_atk", "probability", "we_hp"},
        "a_task_daily_damage": {
            "id",
            "we_power",
            "enemy_power",
            "enemy_cost_hp",
            "we_cost_hp",
        },
    }

    def __init__(self, content_dir: Path = DEFAULT_CONTENT_DIR) -> None:
        self.content_dir = Path(content_dir).expanduser().resolve()
        tables = {name: self._records(name) for name in self.REQUIRED}
        self.people = {int(row["task_type"]): row for row in tables["a_task_people"]}
        self.enemies = tuple(tables["a_task_enemy"])
        self.enemies_by_cycle: Dict[int, Tuple[Mapping[str, Any], ...]] = {}
        for cycle in sorted({int(row["cycle_type"]) for row in self.enemies}):
            self.enemies_by_cycle[cycle] = tuple(
                row for row in self.enemies if int(row["cycle_type"]) == cycle
            )
        self.probability_rows = tuple(
            (row, _interval(row["we_atk"], f"a_task_probability[{row['id']}].we_atk"))
            for row in tables["a_task_probability"]
        )
        self.damage_rows = tuple(
            (
                row,
                _interval(
                    row["we_power"], f"a_task_daily_damage[{row['id']}].we_power"
                ),
            )
            for row in tables["a_task_daily_damage"]
        )
        self.enemy_rewards = {
            int(row["id"]): _rewards(
                row.get("reward"), f"a_task_enemy[{row['id']}].reward"
            )
            for row in self.enemies
        }

        # Activity task rewards are optional validation.  They are used only
        # when a request identifier exactly selects a row; no relation or ID
        # is guessed when the validation is ambiguous.
        self.activity_tasks: Tuple[Mapping[str, Any], ...] = ()
        activity_path = self.content_dir / "a_task_activity.json"
        if activity_path.exists():
            rows = self._records("a_task_activity", required=None)
            if rows and {"id", "relate_activity_function", "tesk_reward"}.issubset(
                rows[0]
            ):
                self.activity_tasks = tuple(rows)
        self.activity_rewards = {
            int(row["id"]): _rewards(
                row.get("tesk_reward"), f"a_task_activity[{row['id']}].tesk_reward"
            )
            for row in self.activity_tasks
        }

        dungeon_rows = self._records("a_dungeon", required=set())
        self.dungeons = {
            _uint(row.get("id")): row
            for row in dungeon_rows
            if _uint(row.get("id"))
        }

        function_rows = self._records("a_activity_function", required=set())
        main_rows = self._records("a_main_activity", required=set())
        task_enemy_rows = {
            _uint(row.get("id")): row for row in self.enemies if _uint(row.get("id"))
        }
        task_rows_by_function: Dict[int, List[Mapping[str, Any]]] = {}
        for row in self.activity_tasks:
            function_id = _uint(row.get("relate_activity_function"))
            if function_id and _uint(row.get("attack")):
                task_rows_by_function.setdefault(function_id, []).append(row)
        parent_by_sub: Dict[int, int] = {}
        for row in main_rows:
            activity_id = _uint(row.get("id"))
            try:
                children = _json_sequence(
                    row.get("activity_sub_id"),
                    f"a_main_activity[{activity_id}].activity_sub_id",
                )
            except SpecialBattleConfigError:
                continue
            for sub_id in children:
                if _uint(sub_id):
                    parent_by_sub[_uint(sub_id)] = activity_id
        self.activity_battles: Dict[Tuple[int, int], Dict[str, Any]] = {}
        for function in function_rows:
            if _uint(function.get("function_type")) != 5:
                continue
            sub_id = _uint(function.get("id"))
            activity_id = parent_by_sub.get(sub_id, 0)
            function_id = _uint(function.get("function_id"))
            enemy = task_enemy_rows.get(function_id)
            tasks = tuple(
                sorted(
                    task_rows_by_function.get(function_id, ()),
                    key=lambda row: _uint(row.get("id")),
                )
            )
            if not activity_id or enemy is None or not tasks:
                continue
            self.activity_battles[(activity_id, sub_id)] = {
                "function_id": function_id,
                "enemy_id": _uint(enemy.get("enemy_id")),
                "enemy_hp": _uint(enemy.get("enemy_hp")),
                "task_ids": tuple(_uint(row.get("id")) for row in tasks),
                "attack_capacity": sum(_uint(row.get("attack")) for row in tasks),
            }

    def _records(
        self, table: str, required: Optional[set[str]] = None
    ) -> Tuple[Mapping[str, Any], ...]:
        path = self.content_dir / f"{table}.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SpecialBattleConfigError(f"cannot read {path}: {exc}") from exc
        rows = payload.get("records") if isinstance(payload, Mapping) else None
        if payload.get("table") != table or not isinstance(rows, list):
            raise SpecialBattleConfigError(f"{path} is not a generated {table} table")
        if payload.get("count") != len(rows):
            raise SpecialBattleConfigError(f"{table}.count disagrees with records")
        required = self.REQUIRED.get(table, set()) if required is None else required
        for index, row in enumerate(rows):
            if not isinstance(row, Mapping) or not required.issubset(row):
                raise SpecialBattleConfigError(
                    f"{table}[{index}] lacks required fields"
                )
        return tuple(rows)

    def enemy(self, cycle: int, level: int) -> Optional[Mapping[str, Any]]:
        rows = self.enemies_by_cycle.get(cycle, ())
        exact = [row for row in rows if _uint(row.get("enemy_level")) == level]
        if exact:
            return exact[0]
        if level == 0 and len(rows) == 1:
            return rows[0]
        return None

    def probability(self, attack: int, enemy_attack: int, hero_hp: int) -> int:
        hp_bucket = min(3, max(0, hero_hp))
        for row, (lower, upper) in self.probability_rows:
            if (
                _uint(row.get("monster_atk")) == enemy_attack
                and _uint(row.get("we_hp")) == hp_bucket
                and lower <= attack < upper
            ):
                return min(10_000, _uint(row.get("probability")))
        return 0

    def damage(self, power: int, enemy_power: int) -> Tuple[int, int]:
        for row, (lower, upper) in self.damage_rows:
            if _uint(row.get("enemy_power")) == enemy_power and lower <= power < upper:
                enemy_cost = _json_sequence(
                    row.get("enemy_cost_hp"),
                    f"a_task_daily_damage[{row['id']}].enemy_cost_hp",
                )
                self_cost = _json_sequence(
                    row.get("we_cost_hp"),
                    f"a_task_daily_damage[{row['id']}].we_cost_hp",
                )
                # The table stores ranges in millionths.  Choosing the upper
                # bound is deterministic and remains inside the defined row.
                return max(map(_uint, enemy_cost), default=0), max(
                    map(_uint, self_cost), default=0
                )
        return 0, 0

    def fast_battle_reason(
        self, profile: Mapping[str, Any], dungeon_id: int
    ) -> str:
        """Return an exact local sweep-admission failure, or an empty string."""

        row = self.dungeons.get(int(dungeon_id))
        if row is None:
            return "unknown dungeon"
        # auto_fight_on controls in-battle automation. The source sweep
        # button uses activity/type/mopping_mod and earned stars instead.
        # Keep the older compatibility admission unchanged outside this build.
        normal_economy = _mapping(profile.get("offline_economy")).get("mode") == "normal-v1"
        if not normal_economy and _uint(row.get("auto_fight_on")) != 1:
            return "dungeon does not allow auto fight"
        if _uint(row.get("dungeon_activity_type")) != 0:
            return "activity dungeon uses its own battle flow"
        if _uint(row.get("mopping_mod")) != 0:
            return "unsupported mopping mode"
        if _uint(row.get("dungeon_type")) in {1, 2, 6}:
            return "dungeon type is not sweepable"
        stars = _mapping(profile.get("dungeon_stars"))
        raw_star = stars.get(str(dungeon_id), stars.get(dungeon_id))
        if (raw_star is None and profile.get("profile_mode") == "test-all-unlocked"
                and _mapping(profile.get("offline_economy")).get("mode") != "normal-v1"):
            raw_star = 3
        if min(3, _uint(raw_star)) < 3:
            return "dungeon has not been cleared with three stars"
        return ""


def _reward_values(reward: Reward) -> Dict[str, int]:
    return {
        "ResType": reward.res_type,
        "ItemId": reward.item_id,
        "Count": reward.count,
        "Para": reward.para,
        "Position": reward.position,
        "Quality": reward.quality,
        "Value": reward.value,
    }


def _stored_rewards(value: Any) -> Tuple[Reward, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    rewards = []
    for row in value:
        if isinstance(row, Mapping):
            rewards.append(
                Reward(
                    _uint(row.get("res_type")),
                    _uint(row.get("item_id")),
                    _uint(row.get("count")),
                    _uint(row.get("para")),
                    _uint(row.get("position")),
                    _uint(row.get("quality")),
                    _uint(row.get("value")),
                )
            )
    return tuple(rewards)


def _reward_rows(rewards: Iterable[Reward]) -> List[Dict[str, int]]:
    return [reward.as_dict() for reward in rewards]


def _state(profile: Mapping[str, Any]) -> Dict[str, Any]:
    return copy.deepcopy(dict(_mapping(profile.get(STATE_KEY))))


def _with_patch(
    profile: Mapping[str, Any], changes: Mapping[str, Any]
) -> Dict[str, Any]:
    result = copy.deepcopy(dict(profile))
    result.update(copy.deepcopy(dict(changes)))
    return result


class SpecialBattlesLayer:
    """Storage-agnostic protocol state for the four special battle families."""

    def __init__(
        self,
        content_dir: Path = DEFAULT_CONTENT_DIR,
        protocol_catalog: Path = DEFAULT_PROTOCOL_CATALOG,
        settlement_layer: Optional[BattleSettlementLayer] = None,
        *,
        now: Optional[Any] = None,
        maze_rewards: Sequence[Reward] = (),
    ) -> None:
        self.catalog = SpecialBattleCatalog(content_dir)
        self.codec = CatalogCodec(protocol_catalog)
        self.protocol_catalog = Path(protocol_catalog).expanduser().resolve()
        self.response_ids = self._load_response_ids()
        self.settlement = settlement_layer or BattleSettlementLayer(
            ContentCatalog.load(Path(content_dir))
        )
        self.now = now or (lambda: int(time.time()))
        self.maze_rewards = tuple(maze_rewards)

    def _load_response_ids(self) -> Dict[int, int]:
        document = json.loads(self.protocol_catalog.read_text(encoding="utf-8"))
        endpoints = {
            int(row["request_id"]): int(row["response_id"])
            for row in document.get("endpoints", [])
            if _int(row.get("request_id"), -1) in REQUEST_NAMES
        }
        if endpoints != RESPONSE_IDS:
            raise SpecialBattleConfigError(
                f"special battle protocol mapping changed: {endpoints!r}"
            )
        commands = {int(row["id"]): row for row in document.get("commands", [])}
        for request_id, request_name in REQUEST_NAMES.items():
            response_id = endpoints[request_id]
            if request_name not in commands.get(request_id, {}).get("names", []):
                raise SpecialBattleConfigError(
                    f"command {request_id} is not {request_name}"
                )
            response_name = RESPONSE_NAMES[response_id]
            if response_name not in commands.get(response_id, {}).get("names", []):
                raise SpecialBattleConfigError(
                    f"command {response_id} is not {response_name}"
                )
        return endpoints

    def _result(
        self,
        request_id: int,
        values: Mapping[str, Any],
        *,
        changes: Optional[Mapping[str, Any]] = None,
        rewards: Sequence[Reward] = (),
        error_code: int = ERROR_SUCCESS,
        replayed: bool = False,
        reason: str = "",
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> Result:
        response_id = self.response_ids[request_id]
        return Result(
            response_id,
            b""
            if response_id in OPAQUE_RESPONSE_IDS
            else self.codec.encode(RESPONSE_NAMES[response_id], values),
            changes=copy.deepcopy(dict(changes or {})),
            rewards=tuple(rewards),
            error_code=error_code,
            replayed=replayed,
            reason=reason,
            metadata=copy.deepcopy(dict(metadata or {})),
        )

    def _grant_changes(
        self, profile: Mapping[str, Any], rewards: Sequence[Reward]
    ) -> Dict[str, Any]:
        if not rewards:
            return {}
        grant = getattr(self.settlement, "grant_rewards", None)
        if callable(grant):
            granted = grant(profile, tuple(rewards))
            if isinstance(granted, Mapping):
                return copy.deepcopy(dict(granted))
            changes = getattr(granted, "changes", None)
            if isinstance(changes, Mapping):
                return copy.deepcopy(dict(changes))
            raise TypeError(
                "BattleSettlementLayer.grant_rewards returned no changes mapping"
            )
        compatibility = getattr(self.settlement, "_grant_changes", None)
        if not callable(compatibility):
            raise TypeError(
                "settlement layer exposes neither grant_rewards nor _grant_changes"
            )
        granted = compatibility(profile, tuple(rewards))
        if not isinstance(granted, Mapping):
            raise TypeError("BattleSettlementLayer._grant_changes returned no mapping")
        return copy.deepcopy(dict(granted))

    @staticmethod
    def _task_key(cycle: int) -> str:
        return str(max(0, cycle))

    def _task_snapshot(
        self, profile: Mapping[str, Any], state: Mapping[str, Any], cycle: int
    ) -> Dict[str, int]:
        people = self.catalog.people.get(cycle, {})
        task = _mapping(_mapping(state.get("task")).get("cycles"))
        saved = _mapping(task.get(self._task_key(cycle)))
        hero_id = _uint(saved.get("hero_id")) or _uint(people.get("people_id"))
        hero_hp = _uint(saved.get("hero_hp"), _uint(people.get("people_hp")))
        hero_atk = _uint(saved.get("hero_atk"), _uint(people.get("people_attack")))
        if hero_atk <= 0:
            hero_atk = 10
        enemy_level = _uint(saved.get("enemy_level"), 1)
        enemy = self.catalog.enemy(cycle, enemy_level)
        if enemy is None:
            rows = self.catalog.enemies_by_cycle.get(cycle, ())
            enemy = rows[0] if rows else {}
            enemy_level = _uint(enemy.get("enemy_level"))
        return {
            "HeroId": hero_id,
            "EnemyId": _uint(saved.get("enemy_id")) or _uint(enemy.get("enemy_id")),
            "EnemyHp": _uint(saved.get("enemy_hp"), _uint(enemy.get("enemy_hp"))),
            "HeroHp": hero_hp,
            "HeroAtk": hero_atk,
            "DungeonId": _uint(saved.get("dungeon_id")),
        }

    def _task_enemies(
        self, state: Mapping[str, Any], cycle: int
    ) -> List[Dict[str, int]]:
        saved = _mapping(_mapping(state.get("task")).get("enemy_hp"))
        return [
            {
                "EnemyId": _uint(row.get("enemy_id")),
                "EnemyHp": _uint(
                    saved.get(str(_uint(row.get("enemy_id")))),
                    _uint(row.get("enemy_hp")),
                ),
            }
            for row in self.catalog.enemies_by_cycle.get(cycle, ())
        ]

    def _request_task_data(
        self, profile: Mapping[str, Any], request: Mapping[str, Any]
    ) -> Result:
        cycle = _uint(request.get("TaskCycleType"))
        state = _state(profile)
        snapshot = self._task_snapshot(profile, state, cycle)
        values = {
            "TaskCycleType": cycle,
            # An explicit message value emits field 2 even when its repeated
            # list is empty, so QuestData.get_BattleData() is never null.
            "BattleData": {"TaskCycleType": cycle, "HeroDataList": [snapshot]},
            "TaskEnemyList": self._task_enemies(state, cycle),
        }
        return self._result(CS_REQUEST_TASK_BATTLE_DATAS, values)

    @staticmethod
    def _roll(seed: str, probability: int) -> bool:
        if probability >= 10_000:
            return True
        if probability <= 0:
            return False
        digest = hashlib.sha256(seed.encode("utf-8")).digest()
        return int.from_bytes(digest[:8], "big") % 10_000 < probability

    def _start_task(
        self, profile: Mapping[str, Any], request: Mapping[str, Any]
    ) -> Result:
        cycle = _uint(request.get("TaskCycleType"))
        hero_id = _uint(request.get("HeroId"))
        enemy_level = _uint(request.get("EnemyLevel"))
        main_task_id = _uint(request.get("MainTaskId"))
        activity_task_id = _uint(request.get("ActivityTaskId"))
        state = _state(profile)
        task = copy.deepcopy(dict(_mapping(state.get("task"))))
        fingerprint = (
            f"{cycle}:{hero_id}:{enemy_level}:{main_task_id}:{activity_task_id}"
        )
        active = _mapping(task.get("active"))
        if active.get("fingerprint") == fingerprint:
            rewards = _stored_rewards(active.get("rewards"))
            return self._result(
                CS_START_TASK_BATTLE,
                _mapping(active.get("response")),
                rewards=rewards,
                replayed=True,
                reason="duplicate active task battle",
            )
        if active:
            return self._task_error(
                CS_START_TASK_BATTLE,
                request,
                ERROR_CLIENT_REQUEST_PARAM,
                "another task battle is active",
            )
        people = self.catalog.people.get(cycle)
        enemy = self.catalog.enemy(cycle, enemy_level)
        if people is None or enemy is None:
            return self._task_error(
                CS_START_TASK_BATTLE,
                request,
                ERROR_CONFIG,
                "unknown task battle configuration",
            )

        snapshot = self._task_snapshot(profile, state, cycle)
        if hero_id:
            snapshot["HeroId"] = hero_id
        snapshot["EnemyId"] = _uint(enemy.get("enemy_id"))
        snapshot["EnemyHp"] = _uint(enemy.get("enemy_hp"))
        hero_atk = snapshot["HeroAtk"]
        hero_hp = snapshot["HeroHp"]
        enemy_atk = _uint(enemy.get("enemy_attcak"))
        probability = self.catalog.probability(hero_atk, enemy_atk, hero_hp)
        attempt = _uint(task.get("next_attempt"), 1) or 1
        won = self._roll(
            f"{_uint(profile.get('role_id'))}:{fingerprint}:{attempt}", probability
        )
        enemy_fraction, hero_fraction = self.catalog.damage(hero_atk, enemy_atk)
        enemy_hp = _uint(enemy.get("enemy_hp"))
        self_damage = min(hero_hp, (hero_hp * hero_fraction + 999_999) // 1_000_000)
        enemy_damage = min(
            enemy_hp,
            enemy_hp if won else (enemy_hp * enemy_fraction + 999_999) // 1_000_000,
        )
        snapshot["HeroHp"] = max(0, hero_hp - self_damage)
        snapshot["EnemyHp"] = max(0, enemy_hp - enemy_damage)
        won = bool(won and snapshot["EnemyHp"] == 0)
        rewards = self.catalog.enemy_rewards[_uint(enemy.get("id"))] if won else ()

        enemy_hp_state = copy.deepcopy(dict(_mapping(task.get("enemy_hp"))))
        enemy_hp_state[str(snapshot["EnemyId"])] = snapshot["EnemyHp"]
        projected_task = copy.deepcopy(task)
        projected_task["enemy_hp"] = enemy_hp_state
        projected_state = copy.deepcopy(state)
        projected_state["task"] = projected_task

        values = {
            "Errorcode": ERROR_SUCCESS,
            "TaskCycleType": cycle,
            "HeroId": snapshot["HeroId"],
            "EnemyLevel": enemy_level,
            "Hp": snapshot["HeroHp"],
            "Atk": hero_atk,
            "EnemyHp": snapshot["EnemyHp"],
            "MainTaskId": main_task_id,
            "WinProbability": probability,
            "Result": BATTLE_WIN if won else 0,
            "SelfDamage": self_damage,
            "EnemyDamage": enemy_damage,
            "RewardList": [_reward_values(row) for row in rewards],
            "BeatenEnemyIdList": [snapshot["EnemyId"]] if won else [],
            "BattleHeroData": snapshot,
            "DailyTaskEnemyList": self._task_enemies(projected_state, cycle),
            "ActivityTaskId": activity_task_id,
        }
        grant_changes = self._grant_changes(profile, rewards)
        working = _with_patch(profile, grant_changes)
        state = _state(working)
        task = copy.deepcopy(dict(_mapping(state.get("task"))))
        cycles = copy.deepcopy(dict(_mapping(task.get("cycles"))))
        cycles[self._task_key(cycle)] = {
            "hero_id": snapshot["HeroId"],
            "hero_hp": snapshot["HeroHp"],
            "hero_atk": snapshot["HeroAtk"],
            "enemy_id": snapshot["EnemyId"],
            "enemy_level": enemy_level,
            "enemy_hp": snapshot["EnemyHp"],
            "dungeon_id": snapshot["DungeonId"],
        }
        task["cycles"] = cycles
        task["enemy_hp"] = enemy_hp_state
        task["next_attempt"] = attempt + 1
        task["active"] = {
            "fingerprint": fingerprint,
            "won": won,
            "response": values,
            "rewards": _reward_rows(rewards),
            "created_at": _uint(self.now()),
        }
        ledger = (
            list(task.get("reward_ledger", []))
            if isinstance(task.get("reward_ledger"), list)
            else []
        )
        ledger.append(
            {
                "transaction_id": f"task:{attempt}",
                "won": won,
                "cycle": cycle,
                "enemy_id": snapshot["EnemyId"],
                "rewards": _reward_rows(rewards),
            }
        )
        task["reward_ledger"] = ledger
        state["task"] = task
        changes = dict(grant_changes)
        changes[STATE_KEY] = state
        return self._result(
            CS_START_TASK_BATTLE, values, changes=changes, rewards=rewards
        )

    def _task_error(
        self,
        command: int,
        request: Mapping[str, Any],
        error: int,
        reason: str,
    ) -> Result:
        if command == CS_END_TASK_BATTLE:
            values = {
                "Errorcode": error,
                "TaskCycleType": _uint(request.get("TaskCycleType")),
                "HeroId": _uint(request.get("HeroId")),
                "EnemyLevel": _uint(request.get("EnemyLevel")),
                "RewardList": [],
            }
        else:
            values = {
                "Errorcode": error,
                "TaskCycleType": _uint(request.get("TaskCycleType")),
                "HeroId": _uint(request.get("HeroId")),
                "EnemyLevel": _uint(request.get("EnemyLevel")),
                "Result": 0,
                "RewardList": [],
                "BeatenEnemyIdList": [],
                "BattleHeroData": {
                    "HeroId": _uint(request.get("HeroId")),
                    "EnemyId": 0,
                    "EnemyHp": 0,
                    "HeroHp": 0,
                    "HeroAtk": 0,
                    "DungeonId": 0,
                },
                "DailyTaskEnemyList": [],
                "ActivityTaskId": _uint(request.get("ActivityTaskId")),
            }
        return self._result(command, values, error_code=error, reason=reason)

    def _end_task(
        self, profile: Mapping[str, Any], request: Mapping[str, Any]
    ) -> Result:
        cycle = _uint(request.get("TaskCycleType"))
        hero_id = _uint(request.get("HeroId"))
        enemy_level = _uint(request.get("EnemyLevel"))
        end_fingerprint = f"{cycle}:{hero_id}:{enemy_level}"
        state = _state(profile)
        task = copy.deepcopy(dict(_mapping(state.get("task"))))
        active = _mapping(task.get("active"))
        if not active:
            previous = _mapping(task.get("last_end"))
            if previous.get("fingerprint") == end_fingerprint:
                rewards = _stored_rewards(previous.get("rewards"))
                return self._result(
                    CS_END_TASK_BATTLE,
                    _mapping(previous.get("response")),
                    rewards=rewards,
                    replayed=True,
                    reason="duplicate task battle end",
                )
            return self._task_error(
                CS_END_TASK_BATTLE,
                request,
                ERROR_CLIENT_REQUEST_PARAM,
                "no active task battle",
            )
        start_parts = str(active.get("fingerprint", "")).split(":")
        if len(start_parts) < 3 or [cycle, hero_id, enemy_level] != [
            _uint(start_parts[0]),
            _uint(start_parts[1]),
            _uint(start_parts[2]),
        ]:
            return self._task_error(
                CS_END_TASK_BATTLE,
                request,
                ERROR_CLIENT_REQUEST_PARAM,
                "task battle end does not match active battle",
            )
        rewards = _stored_rewards(active.get("rewards"))
        values = {
            "Errorcode": ERROR_SUCCESS,
            "TaskCycleType": cycle,
            "HeroId": hero_id,
            "EnemyLevel": enemy_level,
            "RewardList": [_reward_values(row) for row in rewards],
        }
        task["active"] = None
        task["last_end"] = {
            "fingerprint": end_fingerprint,
            "response": values,
            "rewards": _reward_rows(rewards),
        }
        state["task"] = task
        return self._result(
            CS_END_TASK_BATTLE,
            values,
            changes={STATE_KEY: state},
            rewards=rewards,
        )

    def _legacy_activity_task(
        self, profile: Mapping[str, Any], request: Mapping[str, Any]
    ) -> Result:
        activity_id = _uint(request.get("ActivityId"))
        subactivity_id = _uint(request.get("SubActivityId"))
        multi = _uint(request.get("IsMultiAttack"))
        evidence = self._activity_reward_evidence(profile, activity_id)
        evidence_ids = tuple(int(row["id"]) for row in evidence)
        key = (
            f"{activity_id}:{subactivity_id}:{multi}:{','.join(map(str, evidence_ids))}"
        )
        state = _state(profile)
        activity = copy.deepcopy(dict(_mapping(state.get("activity_task"))))
        ledger = copy.deepcopy(dict(_mapping(activity.get("ledger"))))
        previous = _mapping(ledger.get(key))
        if previous:
            rewards = _stored_rewards(previous.get("rewards"))
            previous_task_ids = previous.get("activity_task_ids", [])
            if not isinstance(previous_task_ids, Sequence) or isinstance(
                previous_task_ids, (str, bytes)
            ):
                previous_task_ids = []
            values = {
                "Errorcode": ERROR_SUCCESS,
                "ActivityId": activity_id,
                "ActivityTaskFightNumber": _uint(previous.get("fight_number"), 1),
                "RewardList": [_reward_values(row) for row in rewards],
                "OpenTaskList": [
                    {"Id": _uint(task_id), "Progress": 1, "Status": 1}
                    for task_id in previous_task_ids
                ],
                "IsMultiAttack": multi,
                "SubActivityId": subactivity_id,
            }
            return self._result(
                CS_START_ACTIVITY_TASK_BATTLE,
                values,
                rewards=rewards,
                replayed=True,
                reason="duplicate activity task battle",
            )
        rewards = aggregate(
            reward
            for row in evidence
            for reward in self.catalog.activity_rewards[int(row["id"])]
        )
        values = {
            "Errorcode": ERROR_SUCCESS,
            "ActivityId": activity_id,
            "ActivityTaskFightNumber": 1,
            "RewardList": [_reward_values(row) for row in rewards],
            "OpenTaskList": [
                {"Id": task_id, "Progress": 1, "Status": 1} for task_id in evidence_ids
            ],
            "IsMultiAttack": multi,
            "SubActivityId": subactivity_id,
        }
        if not evidence:
            # A 972 request proves that a battle was attempted, not which of
            # the many tasks related to one activity function was completed.
            # Do not let a no-validation call create a no-op ledger entry that
            # could block a later validationd completion.
            return self._result(
                CS_START_ACTIVITY_TASK_BATTLE,
                values,
                reason="no completed static activity-task reward evidence",
            )
        grant_changes = self._grant_changes(profile, rewards)
        working = _with_patch(profile, grant_changes)
        state = _state(working)
        activity = copy.deepcopy(dict(_mapping(state.get("activity_task"))))
        ledger = copy.deepcopy(dict(_mapping(activity.get("ledger"))))
        ledger[key] = {
            "activity_task_ids": list(evidence_ids),
            "fight_number": 1,
            "rewards": _reward_rows(rewards),
            "has_static_evidence": True,
        }
        activity["ledger"] = ledger
        state["activity_task"] = activity
        changes = dict(grant_changes)
        changes[STATE_KEY] = state
        return self._result(
            CS_START_ACTIVITY_TASK_BATTLE,
            values,
            changes=changes,
            rewards=rewards,
        )

    def _activity_reward_evidence(
        self, profile: Mapping[str, Any], activity_id: int
    ) -> Tuple[Mapping[str, Any], ...]:
        """Return completed, unclaimed static tasks for one activity function."""

        explicit_raw = profile.get("activity_battle_reward_task_ids", [])
        explicit = (
            {_uint(value) for value in explicit_raw}
            if isinstance(explicit_raw, Sequence)
            and not isinstance(explicit_raw, (str, bytes))
            else set()
        )
        tasks = _mapping(profile.get("tasks"))
        roots = (
            _mapping(tasks.get("activity")),
            _mapping(
                _mapping(_mapping(profile.get("content_state")).get("overrides")).get(
                    "a_task_activity"
                )
            ),
        )

        def saved_state(task_id: int) -> Mapping[str, Any]:
            for root in roots:
                value = root.get(task_id, root.get(str(task_id)))
                if isinstance(value, Mapping):
                    return value
            return {}

        selected = []
        for row in self.catalog.activity_tasks:
            task_id = int(row["id"])
            if _uint(row.get("relate_activity_function")) != activity_id:
                continue
            saved = saved_state(task_id)
            status = saved.get("status")
            normalized = str(status).strip().lower()
            completed = (
                task_id in explicit
                or bool(saved.get("completed"))
                or bool(saved.get("battle_reward_ready"))
                or status == 1
                or normalized in {"1", "finished", "complete", "completed"}
            )
            claimed = (
                bool(saved.get("reward_claimed"))
                or bool(saved.get("claimed"))
                or status == 2
                or normalized in {"2", "rewarded", "claimed"}
            )
            if completed and not claimed:
                selected.append(row)
        return tuple(selected)

    def _activity_task(
        self, profile: Mapping[str, Any], request: Mapping[str, Any]
    ) -> Result:
        """Resolve the activity enemy attack, not the separate task reward claim."""

        activity_id = _uint(request.get("ActivityId"))
        subactivity_id = _uint(request.get("SubActivityId"))
        multi = _uint(request.get("IsMultiAttack"))
        config = self.catalog.activity_battles.get((activity_id, subactivity_id))
        if config is None:
            values = {
                "Errorcode": ERROR_CONFIG,
                "ActivityId": activity_id,
                "ActivityTaskFightNumber": 0,
                "RewardList": [],
                "OpenTaskList": [],
                "IsMultiAttack": multi,
                "SubActivityId": subactivity_id,
            }
            return self._result(
                CS_START_ACTIVITY_TASK_BATTLE,
                values,
                error_code=ERROR_CONFIG,
                reason="activity/sub-activity has no complete task-battle chain",
            )

        key = f"{activity_id}:{subactivity_id}"
        state = _state(profile)
        activity = copy.deepcopy(dict(_mapping(state.get("activity_task"))))
        battles = copy.deepcopy(dict(_mapping(activity.get("battles"))))
        battle = copy.deepcopy(dict(_mapping(battles.get(key))))
        previous_fights = min(
            _uint(config.get("enemy_hp")), _uint(battle.get("fight_number"))
        )
        attack_capacity = min(
            _uint(config.get("enemy_hp")), _uint(config.get("attack_capacity"))
        )
        remaining = max(0, attack_capacity - previous_fights)
        consumed = remaining if multi else min(1, remaining)
        fight_number = previous_fights + consumed
        task_ids = tuple(_uint(value) for value in config.get("task_ids", ()))
        values = {
            "Errorcode": ERROR_SUCCESS,
            "ActivityId": activity_id,
            "ActivityTaskFightNumber": fight_number,
            # The linked a_task_enemy.reward is empty.  a_task_activity's
            # tesk_reward belongs to its separate task-claim endpoint.
            "RewardList": [],
            "OpenTaskList": [
                {"Id": task_id, "Progress": 1, "Status": 2}
                for task_id in task_ids
            ],
            "IsMultiAttack": multi,
            "SubActivityId": subactivity_id,
        }
        battle.update(
            {
                "activity_id": activity_id,
                "sub_activity_id": subactivity_id,
                "function_id": _uint(config.get("function_id")),
                "enemy_id": _uint(config.get("enemy_id")),
                "enemy_hp": _uint(config.get("enemy_hp")),
                "fight_number": fight_number,
                "attack_capacity": attack_capacity,
                "last_consumed": consumed,
                "updated_at": _uint(self.now()),
            }
        )
        battles[key] = battle
        activity["battles"] = battles
        ledger = (
            list(activity.get("ledger", []))
            if isinstance(activity.get("ledger"), list)
            else []
        )
        ledger.append(
            {
                "transaction_id": f"activity-task:{key}:{len(ledger) + 1}",
                "fight_number_before": previous_fights,
                "fight_number_after": fight_number,
                "consumed": consumed,
                "multi": bool(multi),
                "rewards": [],
            }
        )
        activity["ledger"] = ledger
        state["activity_task"] = activity
        return self._result(
            CS_START_ACTIVITY_TASK_BATTLE,
            values,
            changes={STATE_KEY: state},
            metadata={
                "fight_number_before": previous_fights,
                "fight_number_after": fight_number,
                "consumed": consumed,
                "enemy_hp": _uint(config.get("enemy_hp")),
            },
            reason="activity task enemy already defeated" if not consumed else "",
        )

    def _fast_setup(
        self, profile: Mapping[str, Any], request: Mapping[str, Any]
    ) -> Result:
        dungeon_id = _uint(request.get("DungeonId"))
        fight_num = _uint(request.get("FightNum"))
        target_num = _uint(request.get("TargetItemNum"))
        error = ERROR_SUCCESS
        reason = ""
        if dungeon_id <= 0 or fight_num <= 0 or fight_num > MAX_FAST_FIGHTS:
            error = ERROR_CLIENT_REQUEST_PARAM
            reason = "invalid fast battle setup"
        elif self.catalog.fast_battle_reason(profile, dungeon_id):
            error = ERROR_CLIENT_REQUEST_PARAM
            reason = self.catalog.fast_battle_reason(profile, dungeon_id)
        state = _state(profile)
        fast = copy.deepcopy(dict(_mapping(state.get("fast"))))
        setup = _mapping(fast.get("setup"))
        fingerprint = f"{dungeon_id}:{fight_num}:{target_num}"
        replayed = bool(
            setup
            and setup.get("fingerprint") == fingerprint
            and setup.get("status") == "pending"
        )
        changes: Dict[str, Any] = {}
        if error == ERROR_SUCCESS and not replayed:
            batch_id = _uint(fast.get("next_batch_id"), 1) or 1
            fast["next_batch_id"] = batch_id + 1
            fast["setup"] = {
                "batch_id": batch_id,
                "fingerprint": fingerprint,
                "dungeon_id": dungeon_id,
                "fight_num": fight_num,
                "target_item_num": target_num,
                "status": "pending",
            }
            if getattr(self.settlement, "normal_economy", False):
                dungeon = self.settlement.dungeons.get(dungeon_id)
                if dungeon and dungeon.extra_costs and _uint(
                        _mapping(_mapping(profile.get("inventory")).get("1001049")).get("count")) > 0:
                    fast["setup"]["admission_mode"] = "ticket"
            state["fast"] = fast
            changes[STATE_KEY] = state
        auto = {
            "HeroPhalandXInfo": [],
            "Turns": [],
            "DungeonId": dungeon_id,
            "RoleId": _uint(profile.get("role_id")),
            "UserName": str(profile.get("name") or profile.get("account") or "Local"),
            "CreateTime": str(_uint(self.now())),
            "Mvp": 0,
            "Counter": 0,
            "Round": fight_num,
        }
        values = {
            "Errorcode": error,
            "DungeonId": dungeon_id,
            "FightNum": fight_num,
            "TargetItemNum": target_num,
            "AutoFightData": auto,
        }
        return self._result(
            CS_FAST_BATTLE_SETUP,
            values,
            changes=changes,
            error_code=error,
            replayed=replayed,
            reason=reason,
        )

    def _start_fast(
        self, profile: Mapping[str, Any], request: Mapping[str, Any]
    ) -> Result:
        dungeon_id = _uint(request.get("DungeonId"))
        requested_num = _uint(request.get("FightNum"))
        state = _state(profile)
        fast = copy.deepcopy(dict(_mapping(state.get("fast"))))
        setup = _mapping(fast.get("setup"))
        previous = _mapping(fast.get("last_batch"))
        if (
            setup
            and setup.get("status") == "completed"
            and _uint(setup.get("dungeon_id")) == dungeon_id
            and _uint(setup.get("fight_num")) == requested_num
            and _uint(previous.get("batch_id")) == _uint(setup.get("batch_id"))
        ):
            fixed = _stored_rewards(previous.get("fixed"))
            random = _stored_rewards(previous.get("random"))
            values = self._fast_start_values(
                dungeon_id, _uint(previous.get("fight_num")), fixed, random, []
            )
            return self._result(
                CS_START_FAST_BATTLE,
                values,
                rewards=(*fixed, *random),
                replayed=True,
                reason="duplicate fast battle settlement",
            )
        if (
            not setup
            or setup.get("status") != "pending"
            or _uint(setup.get("dungeon_id")) != dungeon_id
            or _uint(setup.get("fight_num")) != requested_num
        ):
            values = self._fast_start_values(dungeon_id, 0, (), (), [])
            values["Errorcode"] = ERROR_CLIENT_REQUEST_PARAM
            return self._result(
                CS_START_FAST_BATTLE,
                values,
                error_code=ERROR_CLIENT_REQUEST_PARAM,
                reason="fast battle does not match a pending setup",
            )

        working = copy.deepcopy(dict(profile))
        combined: Dict[str, Any] = {}
        fixed_rows: List[Reward] = []
        random_rows: List[Reward] = []
        rune_uids: List[int] = []
        completed = 0
        error = ERROR_SUCCESS
        for _ in range(requested_num):
            if setup.get("admission_mode") == "ticket" and _uint(
                    _mapping(_mapping(working.get("inventory")).get("1001049")).get("count")) == 0:
                # The source sweep UI selected a ticket count. Do not
                # silently switch the remainder to an energy purchase.
                error = 110
                break
            before_run = copy.deepcopy(working)
            before_runes = {
                _uint(row.get("rune_item_uid"))
                for row in working.get("rune_items", [])
                if isinstance(row, Mapping)
            }
            started = self.settlement.start(
                working,
                dungeon_id=dungeon_id,
                role_id=_uint(profile.get("role_id")),
                target_id=0,
                now=_uint(self.now()),
            )
            if not getattr(started, "ok", False):
                error = _uint(
                    getattr(started, "error_code", ERROR_CONFIG), ERROR_CONFIG
                )
                break
            working.update(copy.deepcopy(dict(getattr(started, "changes", {}))))
            settled = self.settlement.settle(
                working,
                dungeon_id=dungeon_id,
                role_id=_uint(profile.get("role_id")),
                target_id=0,
                result=BATTLE_WIN,
                now=_uint(self.now()),
            )
            if not getattr(settled, "ok", False):
                error = _uint(
                    getattr(settled, "error_code", ERROR_CONFIG), ERROR_CONFIG
                )
                working = before_run
                break
            settle_changes = copy.deepcopy(dict(getattr(settled, "changes", {})))
            working.update(settle_changes)
            combined.update(copy.deepcopy(dict(getattr(started, "changes", {}))))
            combined.update(settle_changes)
            fixed_rows.extend(getattr(settled, "fixed_rewards", ()))
            fixed_rows.extend(getattr(settled, "first_rewards", ()))
            fixed_rows.extend(getattr(settled, "star_rewards", ()))
            random_rows.extend(getattr(settled, "random_rewards", ()))
            after_runes = {
                _uint(row.get("rune_item_uid"))
                for row in working.get("rune_items", [])
                if isinstance(row, Mapping)
            }
            rune_uids.extend(sorted(after_runes - before_runes))
            completed += 1

        fixed = aggregate(fixed_rows)
        random = aggregate(random_rows)
        # A partially completed batch is valid and durable; if no run could be
        # admitted, preserve the admission error and leave the setup pending.
        if completed:
            state = _state(working)
            fast = copy.deepcopy(dict(_mapping(state.get("fast"))))
            setup = copy.deepcopy(dict(_mapping(fast.get("setup"))))
            setup["status"] = "completed"
            setup["completed_fights"] = completed
            fast["setup"] = setup
            fast["last_batch"] = {
                "batch_id": _uint(setup.get("batch_id")),
                "dungeon_id": dungeon_id,
                "fight_num": completed,
                "fixed": _reward_rows(fixed),
                "random": _reward_rows(random),
                "rune_uids": rune_uids,
            }
            state["fast"] = fast
            combined[STATE_KEY] = state
            error = ERROR_SUCCESS
        values = self._fast_start_values(
            dungeon_id, completed, fixed, random, rune_uids
        )
        values["Errorcode"] = error
        return self._result(
            CS_START_FAST_BATTLE,
            values,
            changes=combined,
            rewards=(*fixed, *random),
            error_code=error,
            metadata={"completed_fights": completed},
            reason="fast battle admission failed" if error else "",
        )

    @staticmethod
    def _fast_start_values(
        dungeon_id: int,
        fight_num: int,
        fixed: Sequence[Reward],
        random: Sequence[Reward],
        rune_uids: Sequence[int],
    ) -> Dict[str, Any]:
        return {
            "Errorcode": ERROR_SUCCESS,
            "DungeonId": dungeon_id,
            "FightNum": fight_num,
            "IsUpLevel": False,
            "FixedRewardList": [_reward_values(row) for row in fixed],
            "RandRewardList": [_reward_values(row) for row in random],
            "RuneItemUIdList": list(rune_uids),
        }

    def _end_fast(
        self, profile: Mapping[str, Any], request: Mapping[str, Any]
    ) -> Result:
        dungeon_id = _uint(request.get("DungeonId"))
        state = _state(profile)
        fast = copy.deepcopy(dict(_mapping(state.get("fast"))))
        setup = copy.deepcopy(dict(_mapping(fast.get("setup"))))
        if (
            setup.get("status") == "ended"
            and _uint(setup.get("dungeon_id")) == dungeon_id
        ):
            return self._result(
                CS_END_FAST_BATTLE,
                {"Errorcode": ERROR_SUCCESS, "DungeonId": dungeon_id},
                replayed=True,
                reason="duplicate fast battle end",
            )
        if (
            setup.get("status") != "completed"
            or _uint(setup.get("dungeon_id")) != dungeon_id
        ):
            return self._result(
                CS_END_FAST_BATTLE,
                {"Errorcode": ERROR_CLIENT_REQUEST_PARAM, "DungeonId": dungeon_id},
                error_code=ERROR_CLIENT_REQUEST_PARAM,
                reason="no completed fast battle to end",
            )
        setup["status"] = "ended"
        fast["setup"] = setup
        state["fast"] = fast
        return self._result(
            CS_END_FAST_BATTLE,
            {"Errorcode": ERROR_SUCCESS, "DungeonId": dungeon_id},
            changes={STATE_KEY: state},
        )

    def _ensure_maze(
        self, profile: Mapping[str, Any], state: Dict[str, Any]
    ) -> Tuple[Dict[str, Any], bool]:
        maze = copy.deepcopy(dict(_mapping(state.get("maze"))))
        role_id = _uint(profile.get("role_id")) or 1
        if _mapping(maze.get("seed")):
            original = copy.deepcopy(maze)
            seed = copy.deepcopy(dict(_mapping(maze.get("seed"))))
            seed.update(
                {
                    "scene_id": MAZE_SCENE_ID,
                    "buff_id": MAZE_BUFF_ID,
                    "is_artificial": 0,
                    # Earlier local builds fabricated a 100-gold settlement.
                    # No defined row supports that grant, so migrate it out.
                    "rewards": [],
                }
            )
            maze["seed"] = seed
            maze.setdefault(
                "opponent",
                {
                    "maze_id": 9_840_000_001,
                    "author_id": 9_840_001,
                    "author_name": "Local Challenger",
                    "maze_name": "Training Maze",
                    "scene_id": MAZE_SCENE_ID,
                    "music_id": 0,
                    "buff_id": MAZE_BUFF_ID,
                    "is_artificial": 0,
                    "created_at": _uint(self.now()),
                    "rewards": [],
                },
            )
            maze["season_id"] = MAZE_SEASON_ID
            maze["score"] = max(MAZE_INITIAL_SCORE, _int(maze.get("score")))
            maze.setdefault("next_report_id", 1)
            maze.setdefault("reports", {})
            maze.setdefault("reward_ledger", [])
            maze.setdefault("active", None)
            maze.setdefault("last_match", None)
            changed = maze != original
            if changed:
                state["maze"] = maze
            return maze, changed
        maze_id = 9_830_000_000 + role_id
        maze["seed"] = {
            "maze_id": maze_id,
            "author_id": role_id,
            "author_name": str(
                profile.get("name") or profile.get("account") or "Local"
            ),
            "maze_name": "Local Seed Maze",
            "scene_id": MAZE_SCENE_ID,
            "music_id": 0,
            "buff_id": MAZE_BUFF_ID,
            "is_artificial": 0,
            "created_at": _uint(self.now()),
            "rewards": _reward_rows(self.maze_rewards),
        }
        maze["opponent"] = {
            "maze_id": 9_840_000_001,
            "author_id": 9_840_001,
            "author_name": "Local Challenger",
            "maze_name": "Training Maze",
            "scene_id": MAZE_SCENE_ID,
            "music_id": 0,
            "buff_id": MAZE_BUFF_ID,
            "is_artificial": 0,
            "created_at": _uint(self.now()),
            "rewards": [],
        }
        maze["season_id"] = MAZE_SEASON_ID
        maze["score"] = MAZE_INITIAL_SCORE
        maze["next_report_id"] = 1
        maze["reports"] = {}
        maze["reward_ledger"] = []
        maze["active"] = None
        maze["last_match"] = None
        state["maze"] = maze
        return maze, True

    @staticmethod
    def _maze_values(seed: Mapping[str, Any], score: int) -> Dict[str, Any]:
        positions = [{"Position": position} for position in range(1, 10)]
        positions[0] = {
            "Position": 1,
            "GoblinUId": MAZE_GOBLIN_UID,
            "Gob": {
                "GoblinUId": MAZE_GOBLIN_UID,
                "GoblinId": MAZE_GOBLIN_ID,
                "Level": 1,
                "LevelLimit": 30,
                "MazeCount": 1,
                "Attr": {
                    "Hp": 3696,
                    "Atk": 184,
                    "Weakness": 1478,
                    "Defence": 27,
                },
            },
        }
        return {
            "RoleId": _uint(seed.get("author_id")),
            "MazeId": _uint(seed.get("maze_id")),
            "AuthorName": str(seed.get("author_name") or "Local"),
            "MazeName": str(seed.get("maze_name") or "Local Seed Maze"),
            "Score": score,
            "Thumbsup": 0,
            # Maze.Pha is dereferenced by the runtime maze views.
            "Pha": {"PosList": positions},
            "IsPublished": 1,
            "CreateTime": _uint(seed.get("created_at")),
            "PublishTime": _uint(seed.get("created_at")),
            "ChallengeNum": _uint(seed.get("challenge_num")),
            "PassNum": _uint(seed.get("pass_num")),
            "SceneId": _uint(seed.get("scene_id"), MAZE_SCENE_ID),
            "MusicId": _uint(seed.get("music_id")),
            "BuffId": _uint(seed.get("buff_id")),
            "IsArtificial": _uint(seed.get("is_artificial")),
            "RewardPlus": 0,
            "IsTryPlay": 0,
        }

    def _request_maze_data(self, profile: Mapping[str, Any]) -> Result:
        state = _state(profile)
        maze, created = self._ensure_maze(profile, state)
        seed = _mapping(maze.get("seed"))
        values = {
            "MazeList": [self._maze_values(seed, _uint(maze.get("score")))],
            "FightMazeScore": _int(maze.get("score")),
            "FightMazeCDTimestamp": 0,
        }
        return self._result(
            CS_REQUEST_MAZE_DATAS,
            values,
            changes={STATE_KEY: state} if created else {},
        )

    def _request_maze_social(self, profile: Mapping[str, Any]) -> Result:
        state = _state(profile)
        maze, created = self._ensure_maze(profile, state)
        seed = _mapping(maze.get("seed"))
        values = {
            "CollectMazeList": [
                {
                    "RoleId": _uint(seed.get("author_id")),
                    "MazeId": _uint(seed.get("maze_id")),
                }
            ],
            "OtherSharedMazeList": [],
            "MeSharedRecordList": [],
            "ThumbupMap": {},
        }
        return self._result(
            CS_REQUEST_MAZE_SOCIAL_DATAS,
            values,
            changes={STATE_KEY: state} if created else {},
        )

    def _request_maze_params(self, profile: Mapping[str, Any]) -> Result:
        state = _state(profile)
        maze, changed = self._ensure_maze(profile, state)
        seed = _mapping(maze.get("seed"))
        values = {
            "MazeSeasonId": _uint(maze.get("season_id"), MAZE_SEASON_ID),
            "FightMazeCDTimestamp": 0,
            "ChallengeMazeNum": _uint(seed.get("challenge_num")),
            "PassMazeNum": _uint(seed.get("pass_num")),
            "MazeWeekRewardTime": 0,
            "MazeSeasonRewardTime": 0,
        }
        return self._result(
            CS_REQUEST_MAZE_PARAM_DATAS,
            values,
            changes={STATE_KEY: state} if changed else {},
        )

    def _match_maze(
        self, profile: Mapping[str, Any], request: Mapping[str, Any]
    ) -> Result:
        state = _state(profile)
        maze, _changed = self._ensure_maze(profile, state)
        opponent = _mapping(maze.get("opponent"))
        matched_time = _uint(self.now()) or 1
        previous = _mapping(maze.get("last_match"))
        if previous and _uint(previous.get("maze_id")) == _uint(
            opponent.get("maze_id")
        ):
            matched_time = _uint(previous.get("matched_time"), matched_time)
        maze["last_match"] = {
            "maze_id": _uint(opponent.get("maze_id")),
            "author_id": _uint(opponent.get("author_id")),
            "matched_time": matched_time,
        }
        state["maze"] = maze
        score = _int(maze.get("score"), MAZE_INITIAL_SCORE)
        return self._result(
            CS_MATCH_MAZE,
            {
                "Sender": _uint(profile.get("role_id")),
                "Errorcode": ERROR_SUCCESS,
                "RoleId": _uint(opponent.get("author_id")),
                "Score": score,
                "MazeList": [self._maze_values(opponent, score)],
                "MatchedTime": matched_time,
            },
            changes={STATE_KEY: state},
        )

    def _maze_main_view(self, profile: Mapping[str, Any]) -> Result:
        state = _state(profile)
        _maze, changed = self._ensure_maze(profile, state)
        return self._result(
            CS_ON_MAZE_MAIN_VIEW,
            {},
            changes={STATE_KEY: state} if changed else {},
        )

    def _giveup_maze(
        self, profile: Mapping[str, Any], request: Mapping[str, Any]
    ) -> Result:
        state = _state(profile)
        maze, _changed = self._ensure_maze(profile, state)
        requested_id = _uint(request.get("MazeId"))
        active = _mapping(maze.get("active"))
        error = ERROR_SUCCESS
        if active and requested_id not in {0, _uint(active.get("maze_id"))}:
            error = ERROR_CLIENT_REQUEST_PARAM
        if not error:
            maze["active"] = None
            state["maze"] = maze
        return self._result(
            CS_GIVEUP_FIGHT_MAZE,
            {"Errorcode": error, "CdTime": 0},
            changes={STATE_KEY: state} if not error else {},
            error_code=error,
            reason="maze give-up does not match active entry" if error else "",
        )

    @staticmethod
    def _maze_seed_by_ref(
        maze: Mapping[str, Any], maze_id: int, author_id: int
    ) -> Mapping[str, Any]:
        for key in ("seed", "opponent"):
            candidate = _mapping(maze.get(key))
            if maze_id == _uint(candidate.get("maze_id")) and author_id in {
                0,
                _uint(candidate.get("author_id")),
            }:
                return candidate
        return {}

    def _enter_maze(
        self, profile: Mapping[str, Any], request: Mapping[str, Any]
    ) -> Result:
        state = _state(profile)
        maze, _ = self._ensure_maze(profile, state)
        maze_id = _uint(request.get("MazeId"))
        author_id = _uint(request.get("AuthorId"))
        matched = _uint(request.get("MatchedTime"))
        seed = self._maze_seed_by_ref(maze, maze_id, author_id)
        last_match = _mapping(maze.get("last_match"))
        is_own_seed = maze_id == _uint(_mapping(maze.get("seed")).get("maze_id"))
        has_match_ticket = bool(
            last_match
            and maze_id == _uint(last_match.get("maze_id"))
            and _uint(seed.get("author_id")) == _uint(last_match.get("author_id"))
            and matched in {0, _uint(last_match.get("matched_time"))}
        )
        if not seed or (not is_own_seed and not has_match_ticket):
            return self._result(
                CS_ENTER_MAZE_BATTLE,
                {
                    "Errorcode": ERROR_CLIENT_REQUEST_PARAM,
                    "MazeId": maze_id,
                    "AuthorId": author_id,
                    "MatchedTime": matched,
                },
                error_code=ERROR_CLIENT_REQUEST_PARAM,
                reason="unknown local maze",
            )
        active = _mapping(maze.get("active"))
        if active and _uint(active.get("maze_id")) == maze_id:
            return self._result(
                CS_ENTER_MAZE_BATTLE,
                {
                    "Errorcode": ERROR_SUCCESS,
                    "MazeId": maze_id,
                    "AuthorId": _uint(seed.get("author_id")),
                    "MatchedTime": _uint(active.get("matched_time")),
                },
                replayed=True,
                reason="duplicate maze entry",
            )
        entry_id = _uint(maze.get("next_entry_id"), 1) or 1
        maze["next_entry_id"] = entry_id + 1
        maze["active"] = {
            "entry_id": entry_id,
            "maze_id": maze_id,
            "author_id": _uint(seed.get("author_id")),
            "matched_time": matched or _uint(self.now()),
            "target": "seed"
            if maze_id == _uint(_mapping(maze.get("seed")).get("maze_id"))
            else "opponent",
        }
        state["maze"] = maze
        return self._result(
            CS_ENTER_MAZE_BATTLE,
            {
                "Errorcode": ERROR_SUCCESS,
                "MazeId": maze_id,
                "AuthorId": _uint(seed.get("author_id")),
                "MatchedTime": _uint(maze["active"]["matched_time"]),
            },
            changes={STATE_KEY: state},
        )

    @staticmethod
    def _report_values(
        report_id: int, result: int, request_report: Any = None
    ) -> Dict[str, Any]:
        source = dict(request_report) if isinstance(request_report, Mapping) else {}
        source.update({"Id": report_id, "Winlose": result})
        source.setdefault("Round", 1)
        for key in (
            "Witchs",
            "Gospels",
            "Monsters1",
            "Monsters2",
            "Monsters3",
            "WitchAbnormals",
            "MonsterAbnormals",
        ):
            source.setdefault(key, [])
        return source

    def _maze_end(
        self, profile: Mapping[str, Any], request: Mapping[str, Any]
    ) -> Result:
        state = _state(profile)
        maze, _ = self._ensure_maze(profile, state)
        maze_id = _uint(request.get("MazeId"))
        author_id = _uint(request.get("AuthorId"))
        result = _uint(request.get("Result"))
        active = _mapping(maze.get("active"))
        target_key = str(active.get("target") or "seed")
        seed = copy.deepcopy(dict(_mapping(maze.get(target_key))))
        if not seed:
            seed = copy.deepcopy(dict(_mapping(maze.get("seed"))))
        previous = _mapping(maze.get("last_settlement"))
        fingerprint = f"{maze_id}:{author_id}:{result}"
        if not active and previous.get("fingerprint") == fingerprint:
            rewards = _stored_rewards(previous.get("rewards"))
            values = copy.deepcopy(dict(_mapping(previous.get("response"))))
            return self._result(
                CS_MAZE_BATTLE_END,
                values,
                rewards=rewards,
                replayed=True,
                reason="duplicate maze settlement",
            )
        valid = bool(
            active
            and maze_id == _uint(active.get("maze_id")) == _uint(seed.get("maze_id"))
            and author_id in {0, _uint(active.get("author_id"))}
        )
        if not valid:
            values = {
                "Errorcode": ERROR_CLIENT_REQUEST_PARAM,
                "Result": result,
                "BattleVerifyResult": False,
                "MazeId": maze_id,
                "AuthorId": author_id,
                "Score": _int(maze.get("score")),
                "ScoreReward": 0,
                "Mz": self._maze_values(seed, _int(maze.get("score"))),
                "HasCollected": True,
                "HasFocused": True,
                "RewardList": [],
            }
            return self._result(
                CS_MAZE_BATTLE_END,
                values,
                error_code=ERROR_CLIENT_REQUEST_PARAM,
                reason="maze settlement has no matching entry",
            )

        won = result == BATTLE_WIN
        score_reward = MAZE_SCORE_DELTA if won else -MAZE_SCORE_DELTA
        score = max(
            MAZE_INITIAL_SCORE,
            _int(maze.get("score"), MAZE_INITIAL_SCORE) + score_reward,
        )
        # No defined maze row proves an immediate item/currency settlement.
        rewards: Tuple[Reward, ...] = ()
        report_id = _uint(maze.get("next_report_id"), 1) or 1
        report = self._report_values(report_id, result, request.get("Report"))
        reports = copy.deepcopy(dict(_mapping(maze.get("reports"))))
        reports[str(report_id)] = report
        seed["challenge_num"] = _uint(seed.get("challenge_num")) + 1
        if won:
            seed["pass_num"] = _uint(seed.get("pass_num")) + 1
        maze[target_key] = seed
        maze["score"] = score
        maze["next_report_id"] = report_id + 1
        maze["reports"] = reports
        maze["active"] = None
        ledger = (
            list(maze.get("reward_ledger", []))
            if isinstance(maze.get("reward_ledger"), list)
            else []
        )
        ledger.append(
            {
                "transaction_id": f"maze:{_uint(active.get('entry_id'))}",
                "entry_id": _uint(active.get("entry_id")),
                "report_id": report_id,
                "result": result,
                "score_reward": score_reward,
                "rewards": _reward_rows(rewards),
            }
        )
        maze["reward_ledger"] = ledger
        values = {
            "Errorcode": ERROR_SUCCESS,
            "Result": result,
            "BattleVerifyResult": True,
            "MazeId": maze_id,
            "AuthorId": _uint(seed.get("author_id")),
            "Score": score,
            "ScoreReward": score_reward,
            "Mz": self._maze_values(seed, score),
            "HasCollected": True,
            "HasFocused": True,
            "RewardList": [_reward_values(row) for row in rewards],
        }
        maze["last_settlement"] = {
            "fingerprint": fingerprint,
            "response": values,
            "rewards": _reward_rows(rewards),
            "report_id": report_id,
        }
        state["maze"] = maze
        return self._result(
            CS_MAZE_BATTLE_END,
            values,
            changes={STATE_KEY: state},
            rewards=rewards,
            metadata={"report_id": report_id, "score_reward": score_reward},
        )

    def _maze_report(
        self, profile: Mapping[str, Any], request: Mapping[str, Any]
    ) -> Result:
        report_id = _uint(request.get("ReportId"))
        state = _state(profile)
        maze, created = self._ensure_maze(profile, state)
        report = _mapping(_mapping(maze.get("reports")).get(str(report_id)))
        error = ERROR_SUCCESS if report else ERROR_CLIENT_REQUEST_PARAM
        values = {
            "Errorcode": error,
            "ReportId": report_id,
            # Emit an explicit nested report even on a miss; callers can safely
            # dereference it before inspecting Errorcode.
            "Report": dict(report) if report else self._report_values(report_id, 0),
        }
        return self._result(
            CS_REQUEST_MAZE_BATTLE_REPORT,
            values,
            changes={STATE_KEY: state} if created else {},
            error_code=error,
            reason="unknown maze report" if error else "",
        )

    def handle(
        self,
        command_id: int,
        profile: Optional[Mapping[str, Any]],
        body: bytes = b"",
    ) -> Optional[Result]:
        """Handle one supported command without mutating ``profile``."""

        if command_id not in REQUEST_NAMES:
            return None
        profile = profile or {}
        try:
            request = self.codec.decode(REQUEST_NAMES[command_id], bytes(body))
        except (TypeError, ValueError, ProtobufDecodeError) as exc:
            response_id = self.response_ids[command_id]
            response_name = RESPONSE_NAMES[response_id]
            values: Dict[str, Any] = {}
            schema = self.codec.encoder.schema(response_name)
            if any(row["name"] == "Errorcode" for row in schema.get("fields", [])):
                values["Errorcode"] = ERROR_MESSAGE_DATA_ILLEGAL
            # Keep the known required nested response messages valid.
            if response_name == "SC_StartTaskBattleRet":
                values["BattleHeroData"] = {}
            elif response_name == "SC_TaskBattleDatasRet":
                values["BattleData"] = {}
            elif response_name == "SC_FastBattleSetupRet":
                values["AutoFightData"] = {}
            elif response_name == "SC_MazeBattleEndRet":
                values["Mz"] = {"Pha": {}}
            elif response_name == "SC_MazeBattleReportRet":
                values["Report"] = {}
            return self._result(
                command_id,
                values,
                error_code=ERROR_MESSAGE_DATA_ILLEGAL,
                reason=str(exc),
            )

        if command_id == CS_REQUEST_TASK_BATTLE_DATAS:
            return self._request_task_data(profile, request)
        if command_id == CS_START_TASK_BATTLE:
            return self._start_task(profile, request)
        if command_id == CS_END_TASK_BATTLE:
            return self._end_task(profile, request)
        if command_id == CS_START_ACTIVITY_TASK_BATTLE:
            return self._activity_task(profile, request)
        if command_id == CS_FAST_BATTLE_SETUP:
            return self._fast_setup(profile, request)
        if command_id == CS_START_FAST_BATTLE:
            return self._start_fast(profile, request)
        if command_id == CS_END_FAST_BATTLE:
            return self._end_fast(profile, request)
        if command_id == CS_REQUEST_MAZE_DATAS:
            return self._request_maze_data(profile)
        if command_id == CS_MATCH_MAZE:
            return self._match_maze(profile, request)
        if command_id == CS_GIVEUP_FIGHT_MAZE:
            return self._giveup_maze(profile, request)
        if command_id == CS_REQUEST_MAZE_SOCIAL_DATAS:
            return self._request_maze_social(profile)
        if command_id == CS_REQUEST_MAZE_PARAM_DATAS:
            return self._request_maze_params(profile)
        if command_id == CS_ENTER_MAZE_BATTLE:
            return self._enter_maze(profile, request)
        if command_id == CS_MAZE_BATTLE_END:
            return self._maze_end(profile, request)
        if command_id == CS_ON_MAZE_MAIN_VIEW:
            return self._maze_main_view(profile)
        return self._maze_report(profile, request)


# Singular/state-oriented aliases make integration with the other server
# layers explicit without maintaining duplicate implementations.
SpecialBattleLayer = SpecialBattlesLayer
SpecialBattleStateLayer = SpecialBattlesLayer


@lru_cache(maxsize=1)
def default_layer() -> SpecialBattlesLayer:
    return SpecialBattlesLayer()


def handle(
    command_id: int,
    profile: Optional[Mapping[str, Any]],
    body: bytes = b"",
) -> Optional[Result]:
    """Convenience entry point matching ``handle(command_id, profile, body)``."""

    return default_layer().handle(command_id, profile, body)


__all__ = [
    "CatalogCodec",
    "CS_END_FAST_BATTLE",
    "CS_END_TASK_BATTLE",
    "CS_ENTER_MAZE_BATTLE",
    "CS_FAST_BATTLE_SETUP",
    "CS_GIVEUP_FIGHT_MAZE",
    "CS_MATCH_MAZE",
    "CS_MAZE_BATTLE_END",
    "CS_ON_MAZE_MAIN_VIEW",
    "CS_REQUEST_MAZE_BATTLE_REPORT",
    "CS_REQUEST_MAZE_DATAS",
    "CS_REQUEST_MAZE_PARAM_DATAS",
    "CS_REQUEST_MAZE_SOCIAL_DATAS",
    "CS_REQUEST_TASK_BATTLE_DATAS",
    "CS_START_ACTIVITY_TASK_BATTLE",
    "CS_START_FAST_BATTLE",
    "CS_START_TASK_BATTLE",
    "DEFAULT_CONTENT_DIR",
    "DEFAULT_PROTOCOL_CATALOG",
    "ERROR_CLIENT_REQUEST_PARAM",
    "ERROR_CONFIG",
    "ERROR_MESSAGE_DATA_ILLEGAL",
    "ERROR_SUCCESS",
    "RESPONSE_IDS",
    "REQUEST_NAMES",
    "RESPONSE_NAMES",
    "Result",
    "STATE_KEY",
    "SpecialBattleCatalog",
    "SpecialBattleConfigError",
    "SpecialBattleLayer",
    "SpecialBattleStateLayer",
    "SpecialBattlesLayer",
    "handle",
]
