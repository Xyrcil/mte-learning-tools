"""Catalog-constrained task and plot reward application.

The source server is unavailable, so this module accepts only reward
semantics that can be proved from the defined managed assembly and the 49
generated content tables.  It is intentionally a pure state layer: callers
pass an account profile and receive a top-level change set suitable for one
atomic shallow merge by :mod:`server.state`.

Supported task reward resources are the statically defined
``Msg.ResourceType`` core currencies and validated ``a_item`` rows.  A reward
whose resource target cannot be proved is rejected before any state is
changed.  The generated plot tables contain IDs and read state but no reward
definition themselves; archive rewards instead use the defined
``a_memoryname`` points and ``a_memory`` level rewards. Unmapped plots refuse.
"""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Tuple

try:  # Canonical bag rows shared with the all-unlocked inventory layer.
    from .inventory_content import InventoryContentError, normalize_inventory, stable_item_uid
except ImportError:  # pragma: no cover - direct ``python server/rewards.py`` use
    from inventory_content import (  # type: ignore
        InventoryContentError,
        normalize_inventory,
        stable_item_uid,
    )


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONTENT_DIR = ROOT / "generated" / "content"
DEFAULT_PROTOCOL_CATALOG = ROOT / "generated" / "protocol" / "protocol_catalog.json"

EXPECTED_CONTENT_TABLE_COUNT = 49

# Command IDs and message names are verified against protocol_catalog.json at
# Layer construction time.  The response type retains the runtime's source
# legacy "Rward" spelling.
CS_TAKE_TASK_REWARD = 127
SC_TAKE_TASK_REWARD_RET = 128
CS_TAKE_ALL_TASK_REWARD = 140
SC_TAKE_ALL_TASK_REWARD_RET = 141
CS_TAKE_PLOT_REWARD = 711
SC_TAKE_PLOT_REWARD_RET = 712
CS_TAKE_SOPHISTICATE_REWARD = 508
CS_REQUEST_SOPHISTICATE_REWARDS = 510

RESPONSE_IDS = {
    CS_TAKE_TASK_REWARD: SC_TAKE_TASK_REWARD_RET,
    CS_TAKE_ALL_TASK_REWARD: SC_TAKE_ALL_TASK_REWARD_RET,
    CS_TAKE_PLOT_REWARD: SC_TAKE_PLOT_REWARD_RET,
    CS_TAKE_SOPHISTICATE_REWARD: 509,
    CS_REQUEST_SOPHISTICATE_REWARDS: 511,
}

# Msg.TaskCycleType, defined from runtime assembly.
TASK_CYCLE_DAILY = 0
TASK_CYCLE_WEEKLY = 1
TASK_CYCLE_MAIN = 2
TASK_CYCLE_ACTIVITY = 3
TASK_CYCLE_MAZE = 4
TASK_CYCLE_ACTIVITY_DAILY = 5
TASK_CYCLE_PHY_RECO = 6

# Msg.TaskStatus values used by Task.Status and the existing content layer.
TASK_UNFINISHED = 0
TASK_FINISHED = 1
TASK_REWARDED = 2

# Msg.PlotReadStatus values used by PlotReadData.Status.
PLOT_ENTERED = 0
PLOT_FINISHED = 1

# Msg.ResourceType, defined from runtime assembly.  Only types with a
# proven account destination are applied here.
RESOURCE_DIAMOND = 0
RESOURCE_MINIDIAMOND = 1
RESOURCE_GOLD = 2
RESOURCE_ENERGY = 3
RESOURCE_ITEM = 4
RESOURCE_EXP = 5

RESOURCE_TOP_LEVEL_FIELDS = {
    RESOURCE_DIAMOND: "diamond",
    RESOURCE_MINIDIAMOND: "minidiamond",
    RESOURCE_GOLD: "gold",
    RESOURCE_ENERGY: "energy",
    RESOURCE_EXP: "exp",
}

# Msg.ErrorCode values defined from runtime assembly.
ERROR_SUCCESS = 0
ERROR_ROLE_ID_ILLEGAL = 104
ERROR_NO_REWARD_CAN_BE_TAKEN = 122
ERROR_PLOT_PACKAGE_NOT_UNLOCKED = 329
ERROR_PLOT_REWARD_ALREADY_TAKEN = 353
ERROR_PLOT_SKIP_CANNOT_TAKE_REWARD = 354
ERROR_PLOT_NOT_UNLOCKED = 425
ERROR_EXCEED_ITEM_LIMIT_COUNT = 429
ERROR_TASK_TYPE_NOT_MATCH = 5502
ERROR_TASK_NOT_FOUND = 5503
ERROR_TASK_HAVE_NOT_FINISHED = 5504
ERROR_TASK_STATUS_INCORRECT = 5505
ERROR_CLIENT_REQUEST_PARAM = 32000
ERROR_MESSAGE_DATA_ILLEGAL = 32769
ERROR_CONFIG = 32772

UINT32_MAX = (1 << 32) - 1
UINT64_MAX = (1 << 64) - 1
INT32_MAX = (1 << 31) - 1

TASK_TABLES = {
    TASK_CYCLE_DAILY: ("daily", "a_task_daily"),
    TASK_CYCLE_WEEKLY: ("weekly", "a_task_weekly"),
    TASK_CYCLE_MAIN: ("main", "a_task_main"),
    TASK_CYCLE_ACTIVITY: ("activity", "a_task_activity"),
    TASK_CYCLE_ACTIVITY_DAILY: ("activity_daily", "a_task_activity_daily"),
}

REQUIRED_CONTENT_TABLES = frozenset(
    {
        "a_task_main",
        "a_task_daily",
        "a_task_weekly",
        "a_task_activity",
        "a_task_activity_daily",
        "a_storypack",
        "a_plot_review",
        "a_memoryname",
        "a_memory",
        "a_item",
    }
)

EXPECTED_PROTOCOL_FIELDS = {
    "CS_TakeSophisticateReward": {1: "Level", 2: "IsTakeAll"},
    "SC_TakeSophisticateRewardRet": {1: "Errorcode", 2: "Level", 3: "IsTakeAll", 4: "LevelList"},
    "CS_RequestRewardedSophisticateData": {},
    "SC_RewardedSophisticateDataRet": {1: "SophisticateLevelMap"},
    "Notify_SophisticateLevelUp": {1: "RoleId", 3: "Sophisticatelevel", 4: "Sophisticate"},
    "CS_TakeTaskReward": {
        1: "RoleId",
        2: "TaskId",
        3: "CycleType",
        4: "HeroId",
        5: "ActivityId",
        6: "SubActivityId",
    },
    "SC_TakeTaskRwardRet": {
        1: "Errorcode",
        2: "RoleId",
        3: "TaskId",
        4: "CycleType",
        5: "RewardList",
        6: "HeroId",
        7: "Atk",
        8: "ActivityId",
        9: "SubActivityId",
    },
    "CS_TakeAllTaskReward": {
        1: "RoleId",
        2: "CycleType",
        3: "ChapterId",
        4: "ActivityId",
        5: "SubActivityId",
    },
    "SC_TakeAllTaskRwardRet": {
        1: "Errorcode",
        2: "RoleId",
        3: "CycleType",
        4: "RewardList",
        5: "ActiveRewardIdList",
        6: "Atk",
        7: "ChapterId",
        8: "ActivityId",
        9: "SubActivityId",
    },
    "CS_TakePlotReward": {1: "PlotPkgId"},
    "SC_TakePlotRewardRet": {
        1: "Errorcode",
        2: "PlotPkgId",
        3: "IsRewarded",
        4: "RewardList",
    },
    "RewardItem": {
        1: "ResType",
        2: "ItemId",
        3: "Count",
        4: "Para",
        5: "Position",
        6: "Quality",
        7: "Value",
    },
}

EXPECTED_ENDPOINTS = {
    508: (509, "CS_TakeSophisticateReward", "SC_TakeSophisticateRewardRet"),
    510: (511, "CS_RequestRewardedSophisticateData", "SC_RewardedSophisticateDataRet"),
    CS_TAKE_TASK_REWARD: (SC_TAKE_TASK_REWARD_RET, "CS_TakeTaskReward", "SC_TakeTaskRwardRet"),
    CS_TAKE_ALL_TASK_REWARD: (
        SC_TAKE_ALL_TASK_REWARD_RET,
        "CS_TakeAllTaskReward",
        "SC_TakeAllTaskRwardRet",
    ),
    CS_TAKE_PLOT_REWARD: (SC_TAKE_PLOT_REWARD_RET, "CS_TakePlotReward", "SC_TakePlotRewardRet"),
}


class RewardCatalogError(ValueError):
    """Raised when generated static validation is absent or inconsistent."""


class ProtobufDecodeError(ValueError):
    """Raised when an incoming request is not valid protobuf wire data."""


class RewardApplicationError(ValueError):
    """Internal atomic-planning failure carrying a protocol error code."""

    def __init__(self, error_code: int, reason: str, *, supported: bool = True) -> None:
        super().__init__(reason)
        self.error_code = int(error_code)
        self.reason = reason
        self.supported = bool(supported)


@dataclass(frozen=True)
class Reward:
    """One statically decoded ``Msg.RewardItem`` value."""

    res_type: int
    item_id: int
    count: int

    def as_dict(self) -> Dict[str, int]:
        return {
            "res_type": self.res_type,
            "item_id": self.item_id,
            "count": self.count,
        }


@dataclass(frozen=True)
class TaskDefinition:
    """One task row and its statically parsed reward list."""

    cycle_type: int
    category: str
    table_name: str
    task_id: int
    rewards: Tuple[Reward, ...]
    attack: int = 0
    chapter_id: int = 0
    activity_function_id: int = 0


@dataclass(frozen=True)
class Result:
    """One protobuf response plus a pure shallow-merge profile patch."""

    response_id: int
    body: bytes
    changes: Dict[str, Any] = field(default_factory=dict)
    error_code: int = ERROR_SUCCESS
    rewards: Tuple[Reward, ...] = ()
    claimed_ids: Tuple[int, ...] = ()
    supported: bool = True
    reason: str = ""
    notifications: Tuple[Tuple[int, bytes], ...] = ()

    @property
    def ok(self) -> bool:
        return self.error_code == ERROR_SUCCESS


@dataclass(frozen=True)
class _TaskRequest:
    role_id: int
    task_id: int
    cycle_type: int
    hero_id: int
    activity_id: int
    subactivity_id: int


@dataclass(frozen=True)
class _BulkTaskRequest:
    role_id: int
    cycle_type: int
    chapter_id: int
    activity_id: int
    subactivity_id: int


def encode_varint(value: int) -> bytes:
    if value < 0:
        value &= UINT64_MAX
    output = bytearray()
    while value > 0x7F:
        output.append((value & 0x7F) | 0x80)
        value >>= 7
    output.append(value)
    return bytes(output)


def field_varint(number: int, value: int) -> bytes:
    return encode_varint(number << 3) + encode_varint(int(value))


def field_bytes(number: int, value: bytes) -> bytes:
    return encode_varint((number << 3) | 2) + encode_varint(len(value)) + value


def field_message(number: int, value: bytes) -> bytes:
    return field_bytes(number, value)


def field_packed_uint32(number: int, values: Iterable[int]) -> bytes:
    payload = b"".join(encode_varint(int(value)) for value in values)
    return field_bytes(number, payload) if payload else b""


def read_varint(data: bytes, offset: int = 0) -> Tuple[int, int]:
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


def iter_wire_fields(data: bytes) -> Iterator[Tuple[int, int, object]]:
    offset = 0
    while offset < len(data):
        tag, offset = read_varint(data, offset)
        number, wire_type = tag >> 3, tag & 7
        if number <= 0:
            raise ProtobufDecodeError("protobuf field number zero")
        if wire_type == 0:
            value, offset = read_varint(data, offset)
        elif wire_type == 1:
            end = offset + 8
            if end > len(data):
                raise ProtobufDecodeError("truncated fixed64 field")
            value = data[offset:end]
            offset = end
        elif wire_type == 2:
            size, offset = read_varint(data, offset)
            end = offset + size
            if end > len(data):
                raise ProtobufDecodeError("truncated length-delimited field")
            value = data[offset:end]
            offset = end
        elif wire_type == 5:
            end = offset + 4
            if end > len(data):
                raise ProtobufDecodeError("truncated fixed32 field")
            value = data[offset:end]
            offset = end
        else:
            raise ProtobufDecodeError(f"unsupported protobuf wire type {wire_type}")
        yield number, wire_type, value


def _parse_scalar_fields(data: bytes, widths: Mapping[int, int], *, strict: bool = False) -> Dict[int, int]:
    if not isinstance(data, bytes):
        raise ProtobufDecodeError("request body must be bytes")
    values: Dict[int, int] = {}
    for number, wire_type, raw in iter_wire_fields(data):
        if strict and (number not in widths or number in values):
            raise ProtobufDecodeError(f'unknown or repeated field {number}')
        if number not in widths:
            continue
        if wire_type != 0 or not isinstance(raw, int):
            raise ProtobufDecodeError(f"field {number} must be a varint")
        maximum = UINT64_MAX if widths[number] == 64 else UINT32_MAX
        if raw < 0 or raw > maximum:
            raise ProtobufDecodeError(f"field {number} exceeds uint{widths[number]}")
        # Generated protobuf MergeFrom semantics retain the last occurrence of
        # a singular scalar field.
        values[number] = raw
    return values


def _strict_int(value: Any, label: str, *, minimum: int = 0, maximum: int = UINT32_MAX) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RewardCatalogError(f"{label} must be an integer")
    if value < minimum or value > maximum:
        raise RewardCatalogError(f"{label} is outside {minimum}..{maximum}")
    return value


def _state_uint(value: Any, label: str, *, maximum: int = UINT32_MAX) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > maximum:
        raise RewardApplicationError(ERROR_CONFIG, f"profile {label} is not a valid uint")
    return value


def _mapping(value: Any) -> Mapping[Any, Any]:
    return value if isinstance(value, Mapping) else {}


def _lookup(mapping: Mapping[Any, Any], key: int, default: Any = None) -> Any:
    if key in mapping:
        return mapping[key]
    return mapping.get(str(key), default)


def _normalize_task_status(value: Any) -> int:
    if isinstance(value, str):
        normalized = value.strip().lower().replace("_", "-")
        if normalized in {"finished", "completed", "complete"}:
            return TASK_FINISHED
        if normalized in {"rewarded", "claimed", "reward-claimed"}:
            return TASK_REWARDED
        return TASK_UNFINISHED
    if isinstance(value, bool):
        return TASK_UNFINISHED
    try:
        status = int(value)
    except (TypeError, ValueError, OverflowError):
        return TASK_UNFINISHED
    return status if status in {TASK_UNFINISHED, TASK_FINISHED, TASK_REWARDED} else TASK_UNFINISHED


def _history_id(value: Any, *names: str) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if not isinstance(value, Mapping):
        return None
    for name in names:
        candidate = value.get(name)
        if isinstance(candidate, int) and not isinstance(candidate, bool):
            return candidate
    return None


def _encode_reward(reward: Reward) -> bytes:
    return b"".join(
        (
            field_varint(1, reward.res_type),
            field_varint(2, reward.item_id),
            field_varint(3, reward.count),
        )
    )


class Layer:
    """Static reward catalog plus pure task/plot state transitions."""

    def __init__(
        self,
        content_dir: Path = DEFAULT_CONTENT_DIR,
        protocol_catalog: Path = DEFAULT_PROTOCOL_CATALOG,
    ) -> None:
        self.content_dir = Path(content_dir).expanduser().resolve()
        protocol_path = Path(protocol_catalog).expanduser().resolve()
        if protocol_path.is_dir():
            protocol_path /= "protocol_catalog.json"
        self.protocol_catalog_path = protocol_path

        manifest = self._read_json(self.content_dir / "catalog.json", "content catalog")
        summaries = self._validate_manifest(manifest)
        self.content_catalog_version = self._catalog_version(manifest, summaries)
        self.protocol_catalog_sha256 = self._validate_protocol(protocol_path)

        documents = {
            name: self._load_table(name, summaries[name])
            for name in sorted(REQUIRED_CONTENT_TABLES)
        }
        item_rows = documents["a_item"]
        self.item_max_stacks = {
            _strict_int(row.get("id"), "a_item.id", minimum=1): _strict_int(
                row.get("max_stack_num"),
                f"a_item[{row.get('id')}].max_stack_num",
            )
            for row in item_rows
        }
        self.item_ids = frozenset(self.item_max_stacks)

        task_by_cycle: Dict[int, Dict[int, TaskDefinition]] = {}
        cycles_by_task: Dict[int, set[int]] = {}
        for cycle_type, (category, table_name) in TASK_TABLES.items():
            rows = documents[table_name]
            definitions: Dict[int, TaskDefinition] = {}
            for row in rows:
                task_id = _strict_int(row.get("id"), f"{table_name}.id", minimum=1)
                if task_id in definitions:
                    raise RewardCatalogError(f"duplicate {table_name} task ID {task_id}")
                rewards = self._parse_static_rewards(
                    row.get("tesk_reward"), f"{table_name}[{task_id}].tesk_reward"
                )
                attack = _strict_int(
                    row.get("attack", 0), f"{table_name}[{task_id}].attack"
                )
                chapter_id = 0
                activity_function_id = 0
                if table_name == "a_task_main":
                    chapter_id = _strict_int(
                        row.get("relate_chapter"),
                        f"{table_name}[{task_id}].relate_chapter",
                        minimum=1,
                    )
                elif table_name in {"a_task_activity", "a_task_activity_daily"}:
                    activity_function_id = _strict_int(
                        row.get("relate_activity_function"),
                        f"{table_name}[{task_id}].relate_activity_function",
                    )
                definition = TaskDefinition(
                    cycle_type=cycle_type,
                    category=category,
                    table_name=table_name,
                    task_id=task_id,
                    rewards=rewards,
                    attack=attack,
                    chapter_id=chapter_id,
                    activity_function_id=activity_function_id,
                )
                definitions[task_id] = definition
                cycles_by_task.setdefault(task_id, set()).add(cycle_type)
            task_by_cycle[cycle_type] = definitions
        self.task_by_cycle = {
            cycle: dict(sorted(definitions.items()))
            for cycle, definitions in task_by_cycle.items()
        }
        self.cycles_by_task = {
            task_id: frozenset(cycles) for task_id, cycles in cycles_by_task.items()
        }
        self.task_counts = {
            TASK_TABLES[cycle][0]: len(definitions)
            for cycle, definitions in self.task_by_cycle.items()
        }

        self.plot_ids = frozenset(
            _strict_int(row.get("id"), "a_storypack.id", minimum=1)
            for row in documents["a_storypack"]
        )
        self.plot_review_ids = frozenset(
            _strict_int(row.get("id"), "a_plot_review.id", minimum=1)
            for row in documents["a_plot_review"]
        )
        self.plot_memories = {}
        for row in documents["a_memoryname"]:
            plot_id = _strict_int(row.get("memorypack_id"), "a_memoryname.memorypack_id", minimum=1)
            if plot_id in self.plot_memories:
                raise RewardCatalogError("ambiguous memory package reward")
            self.plot_memories[plot_id] = (
                _strict_int(row.get("memory_number"), "a_memoryname.memory_number"),
                self._parse_static_rewards(row.get("memory_rewardid"), "a_memoryname.memory_rewardid"))
        self.memory_levels = {}
        for row in documents["a_memory"]:
            level = _strict_int(row.get("memory_level"), "a_memory.memory_level", minimum=1)
            self.memory_levels[level] = (
                _strict_int(row.get("memoryexp_number"), "a_memory.memoryexp_number", minimum=1),
                self._parse_static_rewards(row.get("rewardid"), "a_memory.rewardid"))
        if sorted(self.memory_levels) != list(range(1, 61)):
            raise RewardCatalogError("memory reward levels must cover source levels 1..60")
        if [self.memory_levels[level][0] for level in sorted(self.memory_levels)] != sorted({r[0] for r in self.memory_levels.values()}):
            raise RewardCatalogError("memory level thresholds must strictly increase")
        self.plot_reward_mapping_proven = True

    @staticmethod
    def _read_json(path: Path, label: str) -> Dict[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RewardCatalogError(f"cannot read {label} at {path}: {exc}") from exc
        if not isinstance(value, dict):
            raise RewardCatalogError(f"{label} at {path} must be an object")
        return value

    def _validate_manifest(self, manifest: Mapping[str, Any]) -> Dict[str, Mapping[str, Any]]:
        table_count = _strict_int(manifest.get("table_count"), "catalog.table_count")
        tables = manifest.get("tables")
        if table_count < EXPECTED_CONTENT_TABLE_COUNT or not isinstance(tables, list):
            raise RewardCatalogError(
                f"content catalog must contain at least {EXPECTED_CONTENT_TABLE_COUNT} tables"
            )
        if len(tables) != table_count:
            raise RewardCatalogError("catalog.table_count does not match catalog.tables")
        if manifest.get("unsupported_tables") not in (None, []):
            raise RewardCatalogError("content catalog contains unsupported tables")
        if manifest.get("unsupported_fields") not in (None, []):
            raise RewardCatalogError("content catalog contains unsupported fields")
        provenance = manifest.get("schema_provenance")
        if not isinstance(provenance, Mapping) or provenance.get("heuristic_ids") is not False:
            raise RewardCatalogError("content catalog lacks non-heuristic schema provenance")

        summaries: Dict[str, Mapping[str, Any]] = {}
        for index, summary in enumerate(tables):
            if not isinstance(summary, Mapping):
                raise RewardCatalogError(f"catalog.tables[{index}] must be an object")
            name = summary.get("name")
            if not isinstance(name, str) or not name:
                raise RewardCatalogError(f"catalog.tables[{index}].name is invalid")
            if name in summaries:
                raise RewardCatalogError(f"duplicate catalog table {name}")
            summaries[name] = summary
        missing = REQUIRED_CONTENT_TABLES - summaries.keys()
        if missing:
            raise RewardCatalogError(f"content catalog is missing {sorted(missing)}")
        return summaries

    @staticmethod
    def _catalog_version(
        manifest: Mapping[str, Any], summaries: Mapping[str, Mapping[str, Any]]
    ) -> str:
        digest = hashlib.sha256()
        digest.update(
            json.dumps(manifest, ensure_ascii=False, sort_keys=True).encode("utf-8")
        )
        for name in sorted(summaries):
            digest.update(name.encode("utf-8"))
            digest.update(str(summaries[name].get("source_sha256", "")).encode("ascii"))
            digest.update(str(summaries[name].get("id_list_sha256", "")).encode("ascii"))
        return "sha256:" + digest.hexdigest()

    def _load_table(
        self, table_name: str, summary: Mapping[str, Any]
    ) -> Tuple[Mapping[str, Any], ...]:
        filename = summary.get("file")
        if not isinstance(filename, str) or not filename:
            raise RewardCatalogError(f"catalog summary for {table_name} has no file")
        path = (self.content_dir / filename).resolve()
        if path.parent != self.content_dir or path.name != f"{table_name}.json":
            raise RewardCatalogError(f"unsafe or mismatched table path for {table_name}")
        document = self._read_json(path, table_name)
        if document.get("table") != table_name:
            raise RewardCatalogError(f"{path} declares the wrong table")
        records = document.get("records")
        ids = document.get("ids")
        if not isinstance(records, list) or not isinstance(ids, list):
            raise RewardCatalogError(f"{table_name} has no generated records/ids")
        expected_count = _strict_int(document.get("count"), f"{table_name}.count")
        summary_count = _strict_int(summary.get("count"), f"catalog.{table_name}.count")
        if expected_count != len(records) or summary_count != len(records):
            raise RewardCatalogError(f"{table_name} row count is inconsistent")
        result: List[Mapping[str, Any]] = []
        record_ids: List[int] = []
        for index, row in enumerate(records):
            if not isinstance(row, Mapping):
                raise RewardCatalogError(f"{table_name}.records[{index}] is not an object")
            result.append(row)
            record_ids.append(
                _strict_int(row.get("id"), f"{table_name}.records[{index}].id", minimum=1)
            )
        generated_ids = [
            _strict_int(value, f"{table_name}.ids[{index}]", minimum=1)
            for index, value in enumerate(ids)
        ]
        if record_ids != generated_ids or len(set(record_ids)) != len(record_ids):
            raise RewardCatalogError(f"{table_name} generated ID list is inconsistent")
        return tuple(result)

    @staticmethod
    def _validate_protocol(path: Path) -> str:
        try:
            raw = path.read_bytes()
            catalog = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RewardCatalogError(f"cannot read protocol catalog at {path}: {exc}") from exc
        if not isinstance(catalog, dict):
            raise RewardCatalogError("protocol catalog must be an object")
        message_rows = catalog.get("message_types")
        endpoint_rows = catalog.get("endpoints")
        if not isinstance(message_rows, list) or not isinstance(endpoint_rows, list):
            raise RewardCatalogError("protocol catalog lacks message_types/endpoints")

        messages: Dict[str, Mapping[str, Any]] = {}
        for row in message_rows:
            if not isinstance(row, Mapping) or not isinstance(row.get("name"), str):
                continue
            name = str(row["name"])
            if name in messages:
                raise RewardCatalogError(f"duplicate protocol message type {name}")
            messages[name] = row
        for message_name, expected in EXPECTED_PROTOCOL_FIELDS.items():
            row = messages.get(message_name)
            if row is None or not isinstance(row.get("fields"), list):
                raise RewardCatalogError(f"protocol message {message_name} is missing")
            actual: Dict[int, str] = {}
            for field_row in row["fields"]:
                if not isinstance(field_row, Mapping):
                    raise RewardCatalogError(f"{message_name} contains a malformed field")
                number = field_row.get("number")
                name = field_row.get("name")
                if not isinstance(number, int) or not isinstance(name, str):
                    raise RewardCatalogError(f"{message_name} contains an unresolved field")
                actual[number] = name
            if actual != expected:
                raise RewardCatalogError(
                    f"protocol fields for {message_name} differ from the verified schema"
                )

        endpoints = {
            row.get("request_id"): row
            for row in endpoint_rows
            if isinstance(row, Mapping) and isinstance(row.get("request_id"), int)
        }
        for request_id, (response_id, request_name, response_name) in EXPECTED_ENDPOINTS.items():
            row = endpoints.get(request_id)
            if row is None:
                raise RewardCatalogError(f"protocol endpoint {request_id} is missing")
            if (
                row.get("request_name") != request_name
                or row.get("response_id") != response_id
                or row.get("response_name") != response_name
            ):
                raise RewardCatalogError(f"protocol endpoint {request_id} mapping is inconsistent")
        return hashlib.sha256(raw).hexdigest()

    @staticmethod
    def _parse_static_rewards(value: Any, label: str) -> Tuple[Reward, ...]:
        if value in (None, ""):
            return ()
        if not isinstance(value, str):
            raise RewardCatalogError(f"{label} must be a JSON string")
        try:
            rows = json.loads(value)
        except json.JSONDecodeError as exc:
            raise RewardCatalogError(f"{label} is invalid JSON: {exc}") from exc
        if not isinstance(rows, list):
            raise RewardCatalogError(f"{label} must decode to an array")
        rewards: List[Reward] = []
        for index, row in enumerate(rows):
            if not isinstance(row, Mapping):
                raise RewardCatalogError(f"{label}[{index}] must be an object")
            rewards.append(
                Reward(
                    _strict_int(row.get("type"), f"{label}[{index}].type"),
                    _strict_int(row.get("id"), f"{label}[{index}].id"),
                    _strict_int(row.get("num"), f"{label}[{index}].num", minimum=1),
                )
            )
        return tuple(rewards)

    @staticmethod
    def _profile_role_id(profile: Mapping[str, Any]) -> int:
        value = profile.get("role_id", 0)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > UINT64_MAX:
            return 0
        return value

    @staticmethod
    def _validate_role(profile_role_id: int, requested_role_id: int) -> int:
        effective = requested_role_id or profile_role_id
        if profile_role_id <= 0 or effective != profile_role_id:
            raise RewardApplicationError(ERROR_ROLE_ID_ILLEGAL, "request role ID does not match profile")
        return effective

    @staticmethod
    def _parse_task_request(body: bytes, profile_role_id: int) -> _TaskRequest:
        values = _parse_scalar_fields(body, {1: 64, 2: 32, 3: 32, 4: 32, 5: 32, 6: 32})
        return _TaskRequest(
            role_id=values.get(1, profile_role_id),
            task_id=values.get(2, 0),
            cycle_type=values.get(3, TASK_CYCLE_DAILY),
            hero_id=values.get(4, 0),
            activity_id=values.get(5, 0),
            subactivity_id=values.get(6, 0),
        )

    @staticmethod
    def _parse_bulk_request(body: bytes, profile_role_id: int) -> _BulkTaskRequest:
        values = _parse_scalar_fields(body, {1: 64, 2: 32, 3: 32, 4: 32, 5: 32})
        return _BulkTaskRequest(
            role_id=values.get(1, profile_role_id),
            cycle_type=values.get(2, TASK_CYCLE_DAILY),
            chapter_id=values.get(3, 0),
            activity_id=values.get(4, 0),
            subactivity_id=values.get(5, 0),
        )

    @staticmethod
    def _task_history_claimed(profile: Mapping[str, Any], definition: TaskDefinition) -> bool:
        history = profile.get("task_reward_history", [])
        if not isinstance(history, list):
            raise RewardApplicationError(ERROR_CONFIG, "profile task_reward_history is not a list")
        for entry in history:
            task_id = _history_id(entry, "task_id", "source_id")
            if task_id != definition.task_id:
                continue
            if isinstance(entry, Mapping):
                source = entry.get("source")
                if source not in (None, "", "task"):
                    continue
                cycle = entry.get("cycle_type")
                if isinstance(cycle, int) and not isinstance(cycle, bool):
                    if cycle != definition.cycle_type:
                        continue
                if entry.get("claimed") is False:
                    continue
            return True

        reward_history = profile.get("reward_history", [])
        if not isinstance(reward_history, list):
            raise RewardApplicationError(ERROR_CONFIG, "profile reward_history is not a list")
        claim_key = f"task:{definition.cycle_type}:{definition.task_id}"
        for entry in reward_history:
            if not isinstance(entry, Mapping):
                continue
            if entry.get("claim_key") == claim_key:
                return True
            if entry.get("source") != "task":
                continue
            if _history_id(entry, "source_id", "task_id") != definition.task_id:
                continue
            cycle = entry.get("cycle_type")
            if cycle is None or cycle == definition.cycle_type:
                return True
        return False

    def _task_state(
        self, profile: Mapping[str, Any], definition: TaskDefinition
    ) -> Tuple[bool, int, bool]:
        marker = object()
        present = False
        status = TASK_UNFINISHED
        claimed = False

        def apply(value: Any) -> None:
            nonlocal present, status, claimed
            if value is marker:
                return
            present = True
            if isinstance(value, Mapping):
                if "status" in value:
                    status = _normalize_task_status(value.get("status"))
                if any(bool(value.get(name)) for name in ("reward_claimed", "claimed")):
                    claimed = True
                    status = TASK_REWARDED
            else:
                status = _normalize_task_status(value)

        tasks = _mapping(profile.get("tasks"))
        stage_tasks = _mapping(profile.get("stage_tasks"))
        apply(_lookup(tasks, definition.task_id, marker))
        apply(_lookup(_mapping(tasks.get(definition.category)), definition.task_id, marker))
        apply(_lookup(stage_tasks, definition.task_id, marker))
        apply(
            _lookup(
                _mapping(stage_tasks.get(definition.category)),
                definition.task_id,
                marker,
            )
        )
        content_state = _mapping(profile.get("content_state"))
        overrides = _mapping(content_state.get("overrides"))
        apply(
            _lookup(
                _mapping(overrides.get(definition.table_name)),
                definition.task_id,
                marker,
            )
        )
        if status == TASK_REWARDED or self._task_history_claimed(profile, definition):
            claimed = True
            status = TASK_REWARDED
        return present, status, claimed

    @staticmethod
    def _aggregate_rewards(reward_groups: Iterable[Sequence[Reward]]) -> Tuple[Reward, ...]:
        order: List[Tuple[int, int]] = []
        totals: Dict[Tuple[int, int], int] = {}
        for group in reward_groups:
            for reward in group:
                key = (reward.res_type, reward.item_id)
                if key not in totals:
                    order.append(key)
                    totals[key] = 0
                total = totals[key] + reward.count
                if total > UINT32_MAX:
                    raise RewardApplicationError(ERROR_CONFIG, "combined reward count exceeds uint32")
                totals[key] = total
        return tuple(Reward(res_type, item_id, totals[(res_type, item_id)]) for res_type, item_id in order)

    def _reward_changes(
        self, profile: Mapping[str, Any], rewards: Sequence[Reward]
    ) -> Dict[str, Any]:
        changes: Dict[str, Any] = {}
        inventory_value = profile.get("inventory", {})
        if inventory_value is not None and not isinstance(inventory_value, Mapping):
            raise RewardApplicationError(ERROR_CONFIG, "profile inventory is not a mapping")
        try:
            inventory = normalize_inventory(copy.deepcopy(inventory_value))
        except InventoryContentError as exc:
            raise RewardApplicationError(ERROR_CONFIG, str(exc)) from exc
        inventory_changed = False

        for reward in rewards:
            if reward.res_type in RESOURCE_TOP_LEVEL_FIELDS:
                if reward.item_id != 0:
                    raise RewardApplicationError(
                        ERROR_CONFIG,
                        f"resource type {reward.res_type} has an unproved nonzero item ID",
                        supported=False,
                    )
                field_name = RESOURCE_TOP_LEVEL_FIELDS[reward.res_type]
                current = _state_uint(profile.get(field_name, 0), field_name)
                updated = current + reward.count
                if updated > UINT32_MAX:
                    raise RewardApplicationError(
                        ERROR_CONFIG, f"reward would overflow profile {field_name}"
                    )
                changes[field_name] = updated
                continue

            if reward.res_type != RESOURCE_ITEM:
                raise RewardApplicationError(
                    ERROR_CONFIG,
                    (
                        f"resource type {reward.res_type} has no statically proved "
                        "profile destination in the 49-table catalog"
                    ),
                    supported=False,
                )
            if reward.item_id not in self.item_ids:
                raise RewardApplicationError(
                    ERROR_CONFIG,
                    f"reward item {reward.item_id} is absent from a_item",
                    supported=False,
                )
            string_key = str(reward.item_id)
            previous = inventory.get(string_key)
            current = _state_uint(
                previous.get("count", 0) if isinstance(previous, Mapping) else 0,
                f"inventory[{string_key}].count",
            )
            maximum = self.item_max_stacks[reward.item_id]
            updated = current + reward.count
            if current > maximum:
                raise RewardApplicationError(
                    ERROR_EXCEED_ITEM_LIMIT_COUNT,
                    f"item {reward.item_id} would exceed a_item.max_stack_num {maximum}",
                )
            if updated > maximum:
                if profile.get("all_content_unlocked") and current == maximum:
                    # A Test profile may intentionally start at the proved
                    # stack cap.  Treat the already-saturated grant as a
                    # successful no-op so claiming a story/task reward cannot
                    # deadlock content progression.
                    updated = maximum
                else:
                    raise RewardApplicationError(
                        ERROR_EXCEED_ITEM_LIMIT_COUNT,
                        f"item {reward.item_id} would exceed a_item.max_stack_num {maximum}",
                    )
            inventory[string_key] = {
                "uid": int(previous.get("uid"))
                if isinstance(previous, Mapping) and previous.get("uid")
                else stable_item_uid(reward.item_id),
                "id": reward.item_id,
                "count": updated,
                "para": int(previous.get("para", 0))
                if isinstance(previous, Mapping)
                else 0,
            }
            inventory_changed = True

        if inventory_changed:
            changes["inventory"] = inventory
        return changes

    @staticmethod
    def _task_state_changes(
        profile: Mapping[str, Any], definitions: Sequence[TaskDefinition]
    ) -> Dict[str, Any]:
        tasks_value = profile.get("tasks", {})
        if tasks_value is not None and not isinstance(tasks_value, Mapping):
            raise RewardApplicationError(ERROR_CONFIG, "profile tasks is not a mapping")
        tasks = copy.deepcopy(dict(_mapping(tasks_value)))
        for definition in definitions:
            category_value = tasks.get(definition.category, {})
            if category_value is not None and not isinstance(category_value, Mapping):
                raise RewardApplicationError(
                    ERROR_CONFIG, f"profile tasks.{definition.category} is not a mapping"
                )
            category_tasks = copy.deepcopy(dict(_mapping(category_value)))
            previous = _lookup(category_tasks, definition.task_id, {})
            task_state = copy.deepcopy(dict(previous)) if isinstance(previous, Mapping) else {}
            task_state["status"] = TASK_REWARDED
            task_state["reward_claimed"] = True
            category_tasks.pop(definition.task_id, None)
            category_tasks[str(definition.task_id)] = task_state
            tasks[definition.category] = category_tasks

        task_history_value = profile.get("task_reward_history", [])
        reward_history_value = profile.get("reward_history", [])
        if not isinstance(task_history_value, list) or not isinstance(reward_history_value, list):
            raise RewardApplicationError(ERROR_CONFIG, "profile reward history is malformed")
        task_history = copy.deepcopy(task_history_value)
        reward_history = copy.deepcopy(reward_history_value)
        for definition in definitions:
            static_rewards = [reward.as_dict() for reward in definition.rewards]
            task_history.append(
                {
                    "task_id": definition.task_id,
                    "cycle_type": definition.cycle_type,
                    "claimed": True,
                }
            )
            reward_history.append(
                {
                    "claim_key": f"task:{definition.cycle_type}:{definition.task_id}",
                    "source": "task",
                    "source_id": definition.task_id,
                    "cycle_type": definition.cycle_type,
                    "attack": definition.attack,
                    "rewards": static_rewards,
                }
            )
        return {
            "tasks": tasks,
            "task_reward_history": task_history,
            "reward_history": reward_history,
        }

    @staticmethod
    def _task_response_body(
        error_code: int,
        request: _TaskRequest,
        rewards: Sequence[Reward] = (),
        attack: int = 0,
    ) -> bytes:
        payload = bytearray(field_varint(1, error_code))
        payload.extend(field_varint(2, request.role_id))
        payload.extend(field_varint(3, request.task_id))
        payload.extend(field_varint(4, request.cycle_type))
        for reward in rewards:
            payload.extend(field_message(5, _encode_reward(reward)))
        payload.extend(field_varint(6, request.hero_id))
        payload.extend(field_varint(7, attack))
        payload.extend(field_varint(8, request.activity_id))
        payload.extend(field_varint(9, request.subactivity_id))
        return bytes(payload)

    @staticmethod
    def _bulk_response_body(
        error_code: int,
        request: _BulkTaskRequest,
        rewards: Sequence[Reward] = (),
        attack: int = 0,
        active_reward_ids: Sequence[int] = (),
    ) -> bytes:
        payload = bytearray(field_varint(1, error_code))
        payload.extend(field_varint(2, request.role_id))
        payload.extend(field_varint(3, request.cycle_type))
        for reward in rewards:
            payload.extend(field_message(4, _encode_reward(reward)))
        payload.extend(field_packed_uint32(5, active_reward_ids))
        payload.extend(field_varint(6, attack))
        payload.extend(field_varint(7, request.chapter_id))
        payload.extend(field_varint(8, request.activity_id))
        payload.extend(field_varint(9, request.subactivity_id))
        return bytes(payload)

    @staticmethod
    def _plot_response_body(
        error_code: int,
        plot_id: int,
        *,
        rewarded: bool = False,
        rewards: Sequence[Reward] = (),
    ) -> bytes:
        payload = bytearray(
            field_varint(1, error_code)
            + field_varint(2, plot_id)
            + field_varint(3, 1 if rewarded else 0)
        )
        for reward in rewards:
            payload.extend(field_message(4, _encode_reward(reward)))
        return bytes(payload)

    def _task_failure(
        self,
        request: _TaskRequest,
        error_code: int,
        reason: str,
        *,
        supported: bool = True,
    ) -> Result:
        return Result(
            SC_TAKE_TASK_REWARD_RET,
            self._task_response_body(error_code, request),
            error_code=error_code,
            supported=supported,
            reason=reason,
        )

    def _bulk_failure(
        self,
        request: _BulkTaskRequest,
        error_code: int,
        reason: str,
        *,
        supported: bool = True,
    ) -> Result:
        return Result(
            SC_TAKE_ALL_TASK_REWARD_RET,
            self._bulk_response_body(error_code, request),
            error_code=error_code,
            supported=supported,
            reason=reason,
        )

    def _take_task_reward(self, profile: Mapping[str, Any], body: bytes) -> Result:
        profile_role_id = self._profile_role_id(profile)
        try:
            request = self._parse_task_request(body, profile_role_id)
        except ProtobufDecodeError as exc:
            fallback = _TaskRequest(profile_role_id, 0, TASK_CYCLE_DAILY, 0, 0, 0)
            return self._task_failure(
                fallback, ERROR_MESSAGE_DATA_ILLEGAL, f"malformed CS_TakeTaskReward: {exc}"
            )
        try:
            effective_role = self._validate_role(profile_role_id, request.role_id)
        except RewardApplicationError as exc:
            return self._task_failure(request, exc.error_code, exc.reason)
        request = _TaskRequest(
            effective_role,
            request.task_id,
            request.cycle_type,
            request.hero_id,
            request.activity_id,
            request.subactivity_id,
        )

        definitions = self.task_by_cycle.get(request.cycle_type)
        if definitions is None:
            return self._task_failure(
                request, ERROR_TASK_TYPE_NOT_MATCH, f"unsupported task cycle {request.cycle_type}"
            )
        definition = definitions.get(request.task_id)
        if definition is None:
            error_code = (
                ERROR_TASK_TYPE_NOT_MATCH
                if request.task_id in self.cycles_by_task
                else ERROR_TASK_NOT_FOUND
            )
            return self._task_failure(
                request,
                error_code,
                f"task {request.task_id} is not in cycle {request.cycle_type}",
            )
        if definition.activity_function_id:
            contexts = {value for value in (request.activity_id, request.subactivity_id) if value}
            if contexts and definition.activity_function_id not in contexts:
                return self._task_failure(
                    request,
                    ERROR_TASK_TYPE_NOT_MATCH,
                    (
                        f"task {request.task_id} belongs to activity function "
                        f"{definition.activity_function_id}"
                    ),
                )

        try:
            _, status, claimed = self._task_state(profile, definition)
            if claimed:
                return self._task_failure(
                    request,
                    ERROR_TASK_STATUS_INCORRECT,
                    f"task {request.task_id} reward was already claimed",
                )
            if status != TASK_FINISHED:
                return self._task_failure(
                    request,
                    ERROR_TASK_HAVE_NOT_FINISHED,
                    f"task {request.task_id} is not finished",
                )
            if not definition.rewards:
                return self._task_failure(
                    request,
                    ERROR_CONFIG,
                    (
                        f"{definition.table_name}[{definition.task_id}] has no "
                        "configured reward field"
                    ),
                    supported=False,
                )
            rewards = self._aggregate_rewards((definition.rewards,))
            changes = self._reward_changes(profile, rewards)
            changes.update(self._task_state_changes(profile, (definition,)))
        except RewardApplicationError as exc:
            return self._task_failure(
                request,
                exc.error_code,
                exc.reason,
                supported=exc.supported,
            )
        return Result(
            SC_TAKE_TASK_REWARD_RET,
            self._task_response_body(
                ERROR_SUCCESS, request, definition.rewards, definition.attack
            ),
            changes=changes,
            rewards=definition.rewards,
            claimed_ids=(definition.task_id,),
        )

    @staticmethod
    def _bulk_definition_matches(
        definition: TaskDefinition, request: _BulkTaskRequest
    ) -> bool:
        if definition.chapter_id:
            return not request.chapter_id or definition.chapter_id == request.chapter_id
        if definition.activity_function_id:
            contexts = {value for value in (request.activity_id, request.subactivity_id) if value}
            return not contexts or definition.activity_function_id in contexts
        return not any((request.chapter_id, request.activity_id, request.subactivity_id))

    def _take_all_task_rewards(self, profile: Mapping[str, Any], body: bytes) -> Result:
        profile_role_id = self._profile_role_id(profile)
        try:
            request = self._parse_bulk_request(body, profile_role_id)
        except ProtobufDecodeError as exc:
            fallback = _BulkTaskRequest(profile_role_id, TASK_CYCLE_DAILY, 0, 0, 0)
            return self._bulk_failure(
                fallback,
                ERROR_MESSAGE_DATA_ILLEGAL,
                f"malformed CS_TakeAllTaskReward: {exc}",
            )
        try:
            effective_role = self._validate_role(profile_role_id, request.role_id)
        except RewardApplicationError as exc:
            return self._bulk_failure(request, exc.error_code, exc.reason)
        request = _BulkTaskRequest(
            effective_role,
            request.cycle_type,
            request.chapter_id,
            request.activity_id,
            request.subactivity_id,
        )

        definitions = self.task_by_cycle.get(request.cycle_type)
        if definitions is None:
            return self._bulk_failure(
                request, ERROR_TASK_TYPE_NOT_MATCH, f"unsupported task cycle {request.cycle_type}"
            )
        candidates = [
            definition
            for definition in definitions.values()
            if self._bulk_definition_matches(definition, request)
        ]
        if not candidates:
            return self._bulk_failure(
                request,
                ERROR_TASK_NOT_FOUND,
                "no static task matches the requested cycle/context",
            )

        claimable: List[TaskDefinition] = []
        finished_without_mapping: List[TaskDefinition] = []
        try:
            for definition in candidates:
                _, status, claimed = self._task_state(profile, definition)
                if status == TASK_FINISHED and not claimed:
                    if definition.rewards:
                        claimable.append(definition)
                    else:
                        finished_without_mapping.append(definition)
            if finished_without_mapping:
                missing_ids = ", ".join(
                    str(definition.task_id)
                    for definition in finished_without_mapping
                )
                return self._bulk_failure(
                    request,
                    ERROR_CONFIG,
                    f"finished tasks have no configured rewards: {missing_ids}",
                    supported=False,
                )
            if not claimable:
                return self._bulk_failure(
                    request,
                    ERROR_NO_REWARD_CAN_BE_TAKEN,
                    "no finished, unclaimed task reward matches the request",
                )
            rewards = self._aggregate_rewards(
                definition.rewards for definition in claimable
            )
            attack = sum(definition.attack for definition in claimable)
            if attack > INT32_MAX:
                raise RewardApplicationError(ERROR_CONFIG, "combined task attack exceeds int32")
            changes = self._reward_changes(profile, rewards)
            changes.update(self._task_state_changes(profile, claimable))
        except RewardApplicationError as exc:
            return self._bulk_failure(
                request,
                exc.error_code,
                exc.reason,
                supported=exc.supported,
            )
        return Result(
            SC_TAKE_ALL_TASK_REWARD_RET,
            self._bulk_response_body(ERROR_SUCCESS, request, rewards, attack),
            changes=changes,
            rewards=rewards,
            claimed_ids=tuple(definition.task_id for definition in claimable),
        )

    @staticmethod
    def _plot_history_claimed(profile: Mapping[str, Any], plot_id: int) -> bool:
        reward_history = profile.get("reward_history", [])
        if not isinstance(reward_history, list):
            raise RewardApplicationError(ERROR_CONFIG, "profile reward_history is not a list")
        for entry in reward_history:
            if not isinstance(entry, Mapping) or entry.get("source") != "plot":
                continue
            if _history_id(entry, "source_id", "plot_id") == plot_id:
                return True
        return False

    def _plot_state(self, profile: Mapping[str, Any], plot_id: int) -> Tuple[bool, int, bool, bool]:
        marker = object()
        unlocked = bool(profile.get("all_content_unlocked"))
        status = PLOT_ENTERED
        skipped = False
        claimed = False

        def apply(value: Any, *, reward_only: bool = False) -> None:
            nonlocal unlocked, status, skipped, claimed
            if value is marker:
                return
            unlocked = True
            if isinstance(value, Mapping):
                if not reward_only and "status" in value:
                    raw_status = value.get("status")
                    if isinstance(raw_status, str):
                        if raw_status.strip().lower() in {"finished", "completed", "complete"}:
                            status = PLOT_FINISHED
                    elif raw_status == PLOT_FINISHED:
                        status = PLOT_FINISHED
                if not reward_only and bool(value.get("is_skip")):
                    skipped = True
                if any(bool(value.get(name)) for name in ("is_rewarded", "reward_claimed", "claimed")):
                    claimed = True
            elif reward_only:
                claimed = bool(value)
            elif value == PLOT_FINISHED:
                status = PLOT_FINISHED

        apply(_lookup(_mapping(profile.get("story_progress")), plot_id, marker))
        apply(_lookup(_mapping(profile.get("plots")), plot_id, marker))
        apply(
            _lookup(_mapping(profile.get("plot_rewards")), plot_id, marker),
            reward_only=True,
        )
        content_state = _mapping(profile.get("content_state"))
        overrides = _mapping(content_state.get("overrides"))
        apply(_lookup(_mapping(overrides.get("a_storypack")), plot_id, marker))
        if self._plot_history_claimed(profile, plot_id):
            claimed = True
        canonical = _mapping(_lookup(_mapping(profile.get("plots")), plot_id, {}))
        if canonical.get("read_without_skip") is True:
            unlocked, status, skipped = True, PLOT_FINISHED, False
        return unlocked, status, skipped, claimed

    def _take_plot_reward(self, profile: Mapping[str, Any], body: bytes) -> Result:
        try:
            values = _parse_scalar_fields(body, {1: 32}, strict=True)
            plot_id = values.get(1, 0)
        except ProtobufDecodeError as exc:
            return Result(
                SC_TAKE_PLOT_REWARD_RET,
                self._plot_response_body(ERROR_MESSAGE_DATA_ILLEGAL, 0),
                error_code=ERROR_MESSAGE_DATA_ILLEGAL,
                reason=f"malformed CS_TakePlotReward: {exc}",
            )
        if plot_id not in self.plot_ids:
            return Result(
                SC_TAKE_PLOT_REWARD_RET,
                self._plot_response_body(ERROR_PLOT_NOT_UNLOCKED, plot_id),
                error_code=ERROR_PLOT_NOT_UNLOCKED,
                reason=f"plot package {plot_id} is absent from a_storypack",
            )
        try:
            unlocked, status, skipped, claimed = self._plot_state(profile, plot_id)
        except RewardApplicationError as exc:
            return Result(
                SC_TAKE_PLOT_REWARD_RET,
                self._plot_response_body(exc.error_code, plot_id),
                error_code=exc.error_code,
                supported=exc.supported,
                reason=exc.reason,
            )
        if claimed:
            error_code = ERROR_PLOT_REWARD_ALREADY_TAKEN
            reason = f"plot package {plot_id} reward was already claimed"
        elif skipped:
            error_code = ERROR_PLOT_SKIP_CANNOT_TAKE_REWARD
            reason = f"plot package {plot_id} was skipped"
        elif not unlocked or status != PLOT_FINISHED:
            error_code = ERROR_PLOT_NOT_UNLOCKED
            reason = f"plot package {plot_id} is not unlocked and finished"
        else:
            memory = self.plot_memories.get(plot_id)
            if memory is None:
                return Result(712, self._plot_response_body(ERROR_CONFIG, plot_id), error_code=ERROR_CONFIG,
                              supported=False, reason="no source a_memoryname reward for this story package")
            points, items = memory
            try:
                changes = self._reward_changes(profile, items)
                total = _state_uint(profile.get("sophisticate", 0), "sophisticate") + points
                if total > UINT32_MAX:
                    raise RewardApplicationError(ERROR_EXCEED_ITEM_LIMIT_COUNT, "memory points would overflow the client")
                level = self.memory_level(total)
                changes.update(sophisticate=total, sophisticate_level=level)
                plots = copy.deepcopy(dict(_mapping(profile.get("plots"))))
                row = _lookup(plots, plot_id, {})
                row = copy.deepcopy(dict(row)) if isinstance(row, Mapping) else {}
                row.update(status=PLOT_FINISHED, is_rewarded=1)
                plots.pop(plot_id, None)
                plots[str(plot_id)] = row
                claimed_plots = copy.deepcopy(dict(_mapping(profile.get("plot_rewards"))))
                claimed_plots.pop(plot_id, None)
                claimed_plots[str(plot_id)] = 1
                rewards = (*items, *((Reward(18, 0, points),) if points else ()))
                history = profile.get("reward_history", [])
                resource_history = profile.get("resource_history", [])
                if not isinstance(history, list) or not isinstance(resource_history, list):
                    raise RewardApplicationError(ERROR_CONFIG, "invalid plot reward history")
                changes.update(plots=plots, plot_rewards=claimed_plots,
                    reward_history=[*copy.deepcopy(history), {"claim_key": f"plot:{plot_id}", "source": "plot",
                        "source_id": plot_id, "sophisticate": points, "rewards": [r.as_dict() for r in rewards]}],
                    resource_history=[*copy.deepcopy(resource_history), {"source": "plot", "command_id": 711,
                        "plot_id": plot_id, "costs": [], "rewards": [r.as_dict() for r in rewards]}])
                notice = field_varint(1, self._profile_role_id(profile)) + field_varint(3, level) + field_varint(4, total)
                return Result(712, self._plot_response_body(0, plot_id, rewarded=True, rewards=rewards),
                    changes=changes, rewards=rewards, claimed_ids=(plot_id,), notifications=((944, notice),))
            except RewardApplicationError as exc:
                return Result(712, self._plot_response_body(exc.error_code, plot_id),
                              error_code=exc.error_code, reason=exc.reason)
        return Result(
            SC_TAKE_PLOT_REWARD_RET,
            self._plot_response_body(error_code, plot_id),
            error_code=error_code,
            supported=error_code != ERROR_CONFIG,
            reason=reason,
        )

    def memory_level(self, points):
        return max((level for level, (threshold, _) in self.memory_levels.items() if points >= threshold), default=0)

    def _memory_claimed(self, profile):
        levels = profile.get("rewarded_sophisticate_levels", [])
        if not isinstance(levels, list) or any(type(n) is not int or n not in self.memory_levels for n in levels):
            raise RewardApplicationError(ERROR_CONFIG, "invalid claimed memory levels")
        return set(levels)

    def _memory_reward(self, command, profile, body):
        level = all_ = 0
        def result(code, reason="", changes=None, claimed=(), rewards=()):
            payload = (field_varint(1, code) + field_varint(2, level) + field_varint(3, all_)
                       + field_packed_uint32(4, claimed))
            return Result(509, payload, changes=changes or {}, error_code=code, reason=reason,
                          rewards=rewards, claimed_ids=tuple(claimed))
        try:
            old = self._memory_claimed(profile)
            if command == 510:
                if body: return Result(511, b"", error_code=ERROR_MESSAGE_DATA_ILLEGAL, reason="expected empty memory reward query")
                return Result(511, b"".join(field_message(1, field_varint(1, n) + field_varint(2, 1)) for n in sorted(old)))
            values = _parse_scalar_fields(body, {1: 32, 2: 32}, strict=True)
            level, all_ = values.get(1, 0), values.get(2, 0)
            if all_ not in (0, 1) or (level and level not in self.memory_levels) or (not all_ and not level):
                return result(ERROR_MESSAGE_DATA_ILLEGAL, "invalid memory reward selection")
            points = _state_uint(profile.get("sophisticate", 0), "sophisticate")
            current = self.memory_level(points)
            targets = sorted((set(range(1, current + 1)) if all_ else {level}) - old)
            if not targets or any(n > current for n in targets):
                return result(ERROR_NO_REWARD_CAN_BE_TAKEN, "no reached and unclaimed memory reward")
            rewards = self._aggregate_rewards(self.memory_levels[n][1] for n in targets)
            changes = self._reward_changes(profile, rewards)
            claimed = sorted(old | set(targets))
            history = profile.get("reward_history", [])
            if not isinstance(history, list): raise RewardApplicationError(ERROR_CONFIG, "invalid memory reward history")
            changes["rewarded_sophisticate_levels"] = claimed
            changes["reward_history"] = [*copy.deepcopy(history), *({"claim_key": f"memory-level:{n}",
                "source": "sophisticate", "source_id": n,
                "rewards": [r.as_dict() for r in self.memory_levels[n][1]]} for n in targets)]
            changes["sophisticate_level"] = current
            resources = profile.get('resource_history', [])
            if not isinstance(resources, list): raise RewardApplicationError(ERROR_CONFIG, 'invalid resource history')
            changes['resource_history'] = [*copy.deepcopy(resources), {
                'source': 'sophisticate', 'command_id': 508, 'levels': targets,
                'costs': [], 'rewards': [r.as_dict() for r in rewards]}]
            return result(0, changes=changes, claimed=claimed, rewards=rewards)
        except ProtobufDecodeError as exc:
            return result(ERROR_MESSAGE_DATA_ILLEGAL, str(exc))
        except RewardApplicationError as exc:
            if command == 510: return Result(511, b"", error_code=exc.error_code, reason=exc.reason)
            return result(exc.error_code, exc.reason)

    def handle(
        self,
        command_id: int,
        profile: Optional[Mapping[str, Any]],
        body: bytes = b"",
    ) -> Optional[Result]:
        """Handle one reward request or return ``None`` for another layer."""

        profile = profile or {}
        if command_id in (508, 510):
            return self._memory_reward(command_id, profile, body)
        if command_id == CS_TAKE_TASK_REWARD:
            return self._take_task_reward(profile, body)
        if command_id == CS_TAKE_ALL_TASK_REWARD:
            return self._take_all_task_rewards(profile, body)
        if command_id == CS_TAKE_PLOT_REWARD:
            return self._take_plot_reward(profile, body)
        return None

    @staticmethod
    def apply_changes(profile: Dict[str, Any], changes: Mapping[str, Any]) -> None:
        """Apply a result using the compatibility server's shallow merge."""

        profile.update(copy.deepcopy(dict(changes)))


__all__ = [
    "CS_TAKE_ALL_TASK_REWARD",
    "CS_TAKE_PLOT_REWARD",
    "CS_TAKE_TASK_REWARD",
    "DEFAULT_CONTENT_DIR",
    "DEFAULT_PROTOCOL_CATALOG",
    "ERROR_CONFIG",
    "ERROR_EXCEED_ITEM_LIMIT_COUNT",
    "ERROR_MESSAGE_DATA_ILLEGAL",
    "ERROR_NO_REWARD_CAN_BE_TAKEN",
    "ERROR_PLOT_NOT_UNLOCKED",
    "ERROR_PLOT_REWARD_ALREADY_TAKEN",
    "ERROR_PLOT_SKIP_CANNOT_TAKE_REWARD",
    "ERROR_ROLE_ID_ILLEGAL",
    "ERROR_SUCCESS",
    "ERROR_TASK_HAVE_NOT_FINISHED",
    "ERROR_TASK_NOT_FOUND",
    "ERROR_TASK_STATUS_INCORRECT",
    "ERROR_TASK_TYPE_NOT_MATCH",
    "Layer",
    "PLOT_ENTERED",
    "PLOT_FINISHED",
    "RESOURCE_DIAMOND",
    "RESOURCE_ENERGY",
    "RESOURCE_EXP",
    "RESOURCE_GOLD",
    "RESOURCE_ITEM",
    "RESOURCE_MINIDIAMOND",
    "RESPONSE_IDS",
    "Result",
    "Reward",
    "RewardCatalogError",
    "SC_TAKE_ALL_TASK_REWARD_RET",
    "SC_TAKE_PLOT_REWARD_RET",
    "SC_TAKE_TASK_REWARD_RET",
    "TASK_CYCLE_ACTIVITY",
    "TASK_CYCLE_ACTIVITY_DAILY",
    "TASK_CYCLE_DAILY",
    "TASK_CYCLE_MAIN",
    "TASK_CYCLE_WEEKLY",
    "TASK_FINISHED",
    "TASK_REWARDED",
    "TASK_UNFINISHED",
    "encode_varint",
    "field_message",
    "field_varint",
    "iter_wire_fields",
    "read_varint",
]
