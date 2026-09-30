"""Catalog-backed teaching/new-player task protocol state.

This module is deliberately storage agnostic.  A request receives an account
profile mapping and returns protobuf bytes plus a top-level ``changes`` mapping.
The caller can persist a transition with the same shallow merge used by
``PersistentStateStore.update_account``::

    profile.update(result.changes)

The mutable profile keys owned by this layer are:

``teaching_tasks``
    Sparse mapping from teaching-task ID to ``unlocked``, ``status``, and
    ``reward_claimed``.  Unlocking controls whether a task is sent; claiming
    controls ``TeachTask.Status == TsRewarded``.

``teaching_rewarded_ids``
    Complete list of claimed ``a_teaching_reward`` IDs.  These are stage/EXP
    rewards and are intentionally distinct from unlocked tasks and per-task
    reward claims.

An all-unlocked profile needs neither expanded field: its catalog baseline is
all sixteen tasks at ``TsRewarded`` and all three stage rewards claimed.
Explicit sparse values override that baseline.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

try:  # Package import in tests; fallback supports ``python server/server.py``.
    from .content_responses import (
        ProtobufDecodeError,
        field_message,
        field_packed_uint32,
        field_varint,
        iter_wire_fields,
    )
except ImportError:  # pragma: no cover - direct-script compatibility
    from content_responses import (  # type: ignore
        ProtobufDecodeError,
        field_message,
        field_packed_uint32,
        field_varint,
        iter_wire_fields,
    )


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONTENT_DIR = ROOT / "generated" / "content"

CS_REQUEST_TEACH_STEP_DATA = 820
SC_TEACH_STEP_DATA_RET = 821
CS_SET_TEACH_STEP = 822
SC_SET_TEACH_STEP_RET = 823
CS_TAKE_TEACH_TASK_REWARD = 824
SC_TAKE_TEACH_TASK_REWARD_RET = 825
CS_TAKE_TEACH_TASK_EXP_REWARD = 826
SC_TAKE_TEACH_TASK_EXP_REWARD_RET = 827

RESPONSE_IDS = {
    CS_REQUEST_TEACH_STEP_DATA: SC_TEACH_STEP_DATA_RET,
    CS_SET_TEACH_STEP: SC_SET_TEACH_STEP_RET,
    CS_TAKE_TEACH_TASK_REWARD: SC_TAKE_TEACH_TASK_REWARD_RET,
    CS_TAKE_TEACH_TASK_EXP_REWARD: SC_TAKE_TEACH_TASK_EXP_REWARD_RET,
}

# Msg.TaskStatus, defined from runtime assembly.
TS_UNFINISHED = 0
TS_FINISHED = 1
TS_REWARDED = 2

# Relevant Msg.ErrorCode values.  A supported endpoint can still return a
# domain error; ``TeachingResult.supported`` remains true in those cases.
ERROR_SUCCESS = 0
ERROR_NO_REWARD_CAN_BE_TAKEN = 122
ERROR_TASK_NOT_UNLOCKED = 316
ERROR_TASK_NOT_FOUND = 5503
ERROR_TASK_HAVE_NOT_FINISHED = 5504
ERROR_MESSAGE_DATA_ILLEGAL = 32769

EXPECTED_TABLE_COUNTS = {
    "a_teaching_task": 16,
    "a_teaching_reward": 3,
    "a_teaching_guide": 138,
    "a_guide_force": 34,
}

FULL_PROFILE_MODES = frozenset(
    {"full", "all-unlocked", "test-all-unlocked"}
)


@dataclass(frozen=True)
class RewardItem:
    """The exact three values present in generated ``task_reward`` JSON."""

    res_type: int
    item_id: int
    count: int


@dataclass(frozen=True)
class TeachingTaskSnapshot:
    task_id: int
    status: int
    unlocked: bool = True


@dataclass(frozen=True)
class TeachingResult:
    """One response and a pure, shallow-mergeable profile patch."""

    response_id: int
    body: bytes
    changes: Dict[str, Any] = field(default_factory=dict)
    supported: bool = True
    reason: str = ""


def _as_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _mapping(value: Any) -> Mapping[Any, Any]:
    return value if isinstance(value, Mapping) else {}


def _lookup(mapping: Mapping[Any, Any], record_id: int, default: Any = None) -> Any:
    if record_id in mapping:
        return mapping[record_id]
    return mapping.get(str(record_id), default)


def _id_set(values: Any, allowed: frozenset[int]) -> set[int]:
    if not isinstance(values, (list, tuple, set, frozenset)):
        return set()
    return {
        parsed
        for value in values
        if (parsed := _as_int(value, -1)) in allowed
    }


def _full_catalog(profile: Mapping[str, Any]) -> bool:
    return bool(profile.get("all_content_unlocked")) or profile.get(
        "profile_mode"
    ) in FULL_PROFILE_MODES


def _status(value: Any, default: int = TS_UNFINISHED) -> int:
    if isinstance(value, str):
        normalized = value.strip().lower().replace("_", "-")
        if normalized in {"finished", "complete", "completed", "tsfinished"}:
            return TS_FINISHED
        if normalized in {
            "rewarded",
            "claimed",
            "reward-claimed",
            "tsrewarded",
        }:
            return TS_REWARDED
        if normalized in {"unfinished", "available", "tsunfinished"}:
            return TS_UNFINISHED
        return default
    parsed = _as_int(value, default)
    return parsed if parsed in {TS_UNFINISHED, TS_FINISHED, TS_REWARDED} else default


def _bool_value(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on", "unlocked", "claimed"}:
            return True
        if normalized in {"0", "false", "no", "off", "locked", "unclaimed"}:
            return False
    return None


def _first_varint(data: bytes, field_number: int, default: int = 0) -> int:
    for number, wire_type, value in iter_wire_fields(data):
        if number != field_number:
            continue
        if wire_type != 0:
            raise ProtobufDecodeError(f"field {field_number} is not a varint")
        return int(value)
    return int(default)


class TeachingCatalog:
    """Validated view of the four generated tables used by teaching state."""

    _REQUIRED_FIELDS = {
        "a_teaching_task": {
            "id",
            "task_group",
            "task_reward",
            "task_condition",
            "task_guide",
        },
        "a_teaching_reward": {"id", "task_group", "task_reward"},
        "a_teaching_guide": {"id", "type", "sub_type"},
        "a_guide_force": {"id", "task_name", "guide_function"},
    }

    def __init__(self, content_dir: Path = DEFAULT_CONTENT_DIR) -> None:
        self.content_dir = Path(content_dir).expanduser().resolve()
        tables = {
            name: self._records(name) for name in EXPECTED_TABLE_COUNTS
        }

        self.task_rows = tables["a_teaching_task"]
        self.reward_rows = tables["a_teaching_reward"]
        self.teaching_guide_rows = tables["a_teaching_guide"]
        self.force_guide_rows = tables["a_guide_force"]

        self.task_ids = tuple(int(row["id"]) for row in self.task_rows)
        self.reward_ids = tuple(int(row["id"]) for row in self.reward_rows)
        self.teaching_guide_ids = tuple(
            int(row["id"]) for row in self.teaching_guide_rows
        )
        self.force_guide_ids = tuple(
            int(row["id"]) for row in self.force_guide_rows
        )
        self.task_id_set = frozenset(self.task_ids)
        self.reward_id_set = frozenset(self.reward_ids)
        self.teaching_guide_id_set = frozenset(self.teaching_guide_ids)
        self.force_guide_id_set = frozenset(self.force_guide_ids)

        self.task_by_id = {int(row["id"]): row for row in self.task_rows}
        self.reward_by_id = {int(row["id"]): row for row in self.reward_rows}
        self.task_ids_by_group: Dict[int, Tuple[int, ...]] = {}
        for group_id in sorted({int(row["task_group"]) for row in self.task_rows}):
            self.task_ids_by_group[group_id] = tuple(
                int(row["id"])
                for row in self.task_rows
                if int(row["task_group"]) == group_id
            )
        reward_groups = {int(row["task_group"]) for row in self.reward_rows}
        if set(self.task_ids_by_group) != reward_groups:
            raise ValueError(
                "a_teaching_task and a_teaching_reward task_group domains differ"
            )
        if len(reward_groups) != len(self.reward_rows):
            raise ValueError("a_teaching_reward must contain one row per task_group")

        self.task_reward_items = {
            task_id: self._parse_reward_items(
                self.task_by_id[task_id]["task_reward"],
                f"a_teaching_task id {task_id}.task_reward",
            )
            for task_id in self.task_ids
        }
        self.stage_reward_items = {
            reward_id: self._parse_reward_items(
                self.reward_by_id[reward_id]["task_reward"],
                f"a_teaching_reward id {reward_id}.task_reward",
            )
            for reward_id in self.reward_ids
        }

    def _records(self, table_name: str) -> Tuple[Dict[str, Any], ...]:
        path = self.content_dir / f"{table_name}.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise FileNotFoundError(f"missing generated teaching table: {path}") from None
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"cannot read generated teaching table {path}: {exc}") from exc
        if not isinstance(payload, dict):
            raise ValueError(f"{path} must contain a JSON object")
        records = payload.get("records")
        ids = payload.get("ids")
        expected_count = EXPECTED_TABLE_COUNTS[table_name]
        if not isinstance(records, list) or not isinstance(ids, list):
            raise ValueError(f"{path} does not contain generated records/ids")
        if payload.get("table") != table_name:
            raise ValueError(f"{path} declares a different table")
        if payload.get("count") != expected_count or len(records) != expected_count:
            raise ValueError(
                f"{table_name} has {len(records)} rows, expected {expected_count}"
            )
        if len(ids) != expected_count:
            raise ValueError(f"{table_name}.ids has an invalid count")

        normalized: list[Dict[str, Any]] = []
        record_ids: list[int] = []
        required = self._REQUIRED_FIELDS[table_name]
        for index, raw in enumerate(records):
            if not isinstance(raw, dict) or not required <= raw.keys():
                raise ValueError(
                    f"{table_name}.records[{index}] is missing required fields"
                )
            record_id = raw.get("id")
            if isinstance(record_id, bool) or not isinstance(record_id, int):
                raise ValueError(f"{table_name}.records[{index}].id is not an integer")
            if record_id <= 0 or record_id > 0xFFFFFFFF:
                raise ValueError(f"{table_name}.records[{index}].id is outside uint32")
            record_ids.append(record_id)
            normalized.append(dict(raw))
        if record_ids != ids or len(set(record_ids)) != len(record_ids):
            raise ValueError(f"{table_name} record IDs do not match its generated ID list")
        return tuple(normalized)

    @staticmethod
    def _parse_reward_items(value: Any, context: str) -> Tuple[RewardItem, ...]:
        if not isinstance(value, str):
            raise ValueError(f"{context} must be a JSON string")
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{context} is invalid JSON") from exc
        if not isinstance(parsed, list):
            raise ValueError(f"{context} must decode to a list")
        result = []
        for index, raw in enumerate(parsed):
            if not isinstance(raw, dict) or set(raw) != {"type", "id", "num"}:
                raise ValueError(
                    f"{context}[{index}] must contain exactly type, id, and num"
                )
            values = (raw["type"], raw["id"], raw["num"])
            if any(isinstance(item, bool) or not isinstance(item, int) for item in values):
                raise ValueError(f"{context}[{index}] contains a non-integer")
            if any(item < 0 or item > 0xFFFFFFFF for item in values):
                raise ValueError(f"{context}[{index}] contains a value outside uint32")
            result.append(RewardItem(*values))
        return tuple(result)


class TeachingStateLayer:
    """Build teaching snapshots and pure account-state transitions."""

    def __init__(self, content_dir: Path = DEFAULT_CONTENT_DIR) -> None:
        self.catalog = TeachingCatalog(content_dir)

    @staticmethod
    def _apply_task_override(
        value: Any,
        *,
        present: bool,
        unlocked: bool,
        status: int,
    ) -> Tuple[bool, bool, int]:
        if isinstance(value, Mapping):
            present = True
            unlock_value: Optional[bool] = None
            for key in ("unlocked", "visible"):
                if key in value:
                    unlock_value = _bool_value(value.get(key))
                    break
            if unlock_value is None:
                unlocked = True
            else:
                unlocked = unlock_value
            if "status" in value:
                status = _status(value.get("status"), status)
            claim_value: Optional[bool] = None
            for key in ("reward_claimed", "rewarded", "claimed"):
                if key in value:
                    claim_value = _bool_value(value.get(key))
                    break
            if claim_value is True:
                status = TS_REWARDED
            elif claim_value is False and status == TS_REWARDED:
                # Removing a claim from the all-unlocked baseline leaves the
                # task complete and claimable; it does not undo completion.
                status = TS_FINISHED
            return present, unlocked, status
        if value is not None:
            return True, True, _status(value, status)
        return present, unlocked, status

    def task_state(
        self,
        profile: Optional[Mapping[str, Any]],
        task_id: int,
    ) -> TeachingTaskSnapshot:
        """Resolve one task without conflating visibility and reward status."""

        profile = profile or {}
        if task_id not in self.catalog.task_id_set:
            raise KeyError(f"unknown teaching task ID {task_id}")
        full = _full_catalog(profile)
        present = full
        unlocked = full
        status = TS_REWARDED if full else TS_UNFINISHED

        for key in ("unlocked_teaching_task_ids", "unlocked_teaching_tasks"):
            if key in profile and isinstance(
                profile.get(key), (list, tuple, set, frozenset)
            ):
                present = True
                unlocked = task_id in _id_set(
                    profile.get(key), self.catalog.task_id_set
                )
                break

        marker = object()
        tasks = _mapping(profile.get("tasks"))
        sources = (
            _lookup(tasks, task_id, marker),
            _lookup(_mapping(tasks.get("teaching")), task_id, marker),
            _lookup(_mapping(profile.get("teach_tasks")), task_id, marker),
            _lookup(_mapping(profile.get("teaching_task_statuses")), task_id, marker),
        )
        content_state = _mapping(profile.get("content_state"))
        overrides = _mapping(content_state.get("overrides"))
        sources += (
            _lookup(_mapping(overrides.get("a_teaching_task")), task_id, marker),
            _lookup(_mapping(profile.get("teaching_tasks")), task_id, marker),
        )
        for value in sources:
            if value is marker:
                continue
            present, unlocked, status = self._apply_task_override(
                value,
                present=present,
                unlocked=unlocked,
                status=status,
            )

        if "teaching_task_rewarded_ids" in profile:
            claimed = _id_set(
                profile.get("teaching_task_rewarded_ids"), self.catalog.task_id_set
            )
            if task_id in claimed:
                status = TS_REWARDED
            elif status == TS_REWARDED:
                status = TS_FINISHED
        return TeachingTaskSnapshot(task_id, status, present and unlocked)

    def task_snapshots(
        self,
        profile: Optional[Mapping[str, Any]] = None,
        *,
        include_all: bool = False,
    ) -> Tuple[TeachingTaskSnapshot, ...]:
        rows = tuple(
            self.task_state(profile, task_id) for task_id in self.catalog.task_ids
        )
        return rows if include_all else tuple(row for row in rows if row.unlocked)

    @staticmethod
    def _claimed_override(value: Any, current: bool) -> bool:
        if isinstance(value, Mapping):
            for key in ("reward_claimed", "rewarded", "claimed"):
                if key in value:
                    parsed = _bool_value(value.get(key))
                    return current if parsed is None else parsed
            return current
        parsed = _bool_value(value)
        return current if parsed is None else parsed

    def rewarded_ids(
        self, profile: Optional[Mapping[str, Any]] = None
    ) -> Tuple[int, ...]:
        """Return claimed stage rewards, never merely unlocked reward rows."""

        profile = profile or {}
        claimed = {
            reward_id: _full_catalog(profile) for reward_id in self.catalog.reward_ids
        }
        marker = object()
        content_state = _mapping(profile.get("content_state"))
        overrides = _mapping(content_state.get("overrides"))
        roots = (
            _mapping(profile.get("teach_rewards")),
            _mapping(overrides.get("a_teaching_reward")),
            _mapping(profile.get("teaching_rewards")),
        )
        for reward_id in self.catalog.reward_ids:
            for root in roots:
                value = _lookup(root, reward_id, marker)
                if value is not marker:
                    claimed[reward_id] = self._claimed_override(
                        value, claimed[reward_id]
                    )

        # This canonical list is a complete persisted claim set.  Its presence
        # therefore overrides the all-unlocked baseline and mapping aliases.
        if "teaching_rewarded_ids" in profile:
            explicit = _id_set(
                profile.get("teaching_rewarded_ids"), self.catalog.reward_id_set
            )
            claimed = {
                reward_id: reward_id in explicit for reward_id in self.catalog.reward_ids
            }
        return tuple(
            reward_id for reward_id in self.catalog.reward_ids if claimed[reward_id]
        )

    def _unlocked_catalog_ids(
        self,
        profile: Optional[Mapping[str, Any]],
        *,
        ids: Tuple[int, ...],
        id_set: frozenset[int],
        list_key: str,
        mapping_key: str,
        table_name: str,
    ) -> Tuple[int, ...]:
        profile = profile or {}
        unlocked = {record_id: _full_catalog(profile) for record_id in ids}
        if list_key in profile:
            explicit = _id_set(profile.get(list_key), id_set)
            unlocked = {record_id: record_id in explicit for record_id in ids}
        content_state = _mapping(profile.get("content_state"))
        overrides = _mapping(_mapping(content_state.get("overrides")).get(table_name))
        roots = (overrides, _mapping(profile.get(mapping_key)))
        marker = object()
        for record_id in ids:
            for root in roots:
                value = _lookup(root, record_id, marker)
                if value is marker:
                    continue
                if isinstance(value, Mapping):
                    value = value.get("unlocked", value.get("visible"))
                parsed = _bool_value(value)
                if parsed is not None:
                    unlocked[record_id] = parsed
        return tuple(record_id for record_id in ids if unlocked[record_id])

    def unlocked_teaching_guide_ids(
        self, profile: Optional[Mapping[str, Any]] = None
    ) -> Tuple[int, ...]:
        """Resolve ``a_teaching_guide`` article IDs (the 936 domain)."""

        return self._unlocked_catalog_ids(
            profile,
            ids=self.catalog.teaching_guide_ids,
            id_set=self.catalog.teaching_guide_id_set,
            list_key="unlocked_teaching_guide_ids",
            mapping_key="teaching_guides",
            table_name="a_teaching_guide",
        )

    def unlocked_force_guide_ids(
        self, profile: Optional[Mapping[str, Any]] = None
    ) -> Tuple[int, ...]:
        """Resolve ``a_guide_force`` IDs (the 618 domain)."""

        return self._unlocked_catalog_ids(
            profile,
            ids=self.catalog.force_guide_ids,
            id_set=self.catalog.force_guide_id_set,
            list_key="unlocked_force_guide_ids",
            mapping_key="force_guides",
            table_name="a_guide_force",
        )

    @staticmethod
    def _encode_task(task: TeachingTaskSnapshot) -> bytes:
        return field_varint(1, task.task_id) + field_varint(2, task.status)

    @staticmethod
    def _encode_reward_item(item: RewardItem) -> bytes:
        # Include ItemId even when it is zero (Gold/EXP-like resources).  The
        # generated JSON tuple is reproduced exactly and in source order.
        return b"".join(
            (
                field_varint(1, item.res_type),
                field_varint(2, item.item_id),
                field_varint(3, item.count),
            )
        )

    def make_teach_step_data_ret(
        self, profile: Optional[Mapping[str, Any]] = None
    ) -> bytes:
        payload = bytearray()
        for task in self.task_snapshots(profile):
            payload.extend(field_message(1, self._encode_task(task)))
        payload.extend(field_packed_uint32(2, self.rewarded_ids(profile)))
        return bytes(payload)

    def _task_changes(
        self,
        profile: Mapping[str, Any],
        task_id: int,
        status: int,
    ) -> Dict[str, Any]:
        root = copy.deepcopy(dict(_mapping(profile.get("teaching_tasks"))))
        key: Any = task_id if task_id in root else str(task_id)
        previous = root.get(key)
        entry = copy.deepcopy(dict(previous)) if isinstance(previous, Mapping) else {}
        entry.update(
            {
                "unlocked": True,
                "status": status,
                "reward_claimed": status == TS_REWARDED,
            }
        )
        for alias in ("rewarded", "claimed"):
            if alias in entry:
                entry[alias] = status == TS_REWARDED
        root[key] = entry
        return {"teaching_tasks": root}

    def _set_teach_step(
        self, profile: Mapping[str, Any], request_body: bytes
    ) -> TeachingResult:
        try:
            requested_id = _first_varint(request_body, 1)
        except ProtobufDecodeError as exc:
            return TeachingResult(
                SC_SET_TEACH_STEP_RET,
                field_varint(1, ERROR_MESSAGE_DATA_ILLEGAL),
                reason=str(exc),
            )
        if requested_id and requested_id not in self.catalog.task_id_set:
            return TeachingResult(
                SC_SET_TEACH_STEP_RET,
                field_varint(1, ERROR_TASK_NOT_FOUND),
                reason=f"unknown teaching task ID {requested_id}",
            )

        states = self.task_snapshots(profile, include_all=True)
        if requested_id:
            selected = next(row for row in states if row.task_id == requested_id)
        else:
            selected = next(
                (row for row in states if row.unlocked and row.status == TS_UNFINISHED),
                next(
                    (row for row in states if row.unlocked),
                    states[0],
                ),
            )
        new_status = max(selected.status, TS_FINISHED)
        new_task = TeachingTaskSnapshot(selected.task_id, new_status, True)
        changed = not selected.unlocked or selected.status != new_status
        changes = self._task_changes(profile, selected.task_id, new_status) if changed else {}
        body = field_varint(1, ERROR_SUCCESS) + field_message(
            2, self._encode_task(new_task)
        )
        return TeachingResult(SC_SET_TEACH_STEP_RET, body, changes=changes)

    def _reward_body(
        self,
        response_id: int,
        record_id: int,
        error_code: int,
        rewards: Sequence[RewardItem] = (),
    ) -> TeachingResult:
        payload = bytearray(field_varint(1, error_code))
        payload.extend(field_varint(2, record_id))
        if error_code == ERROR_SUCCESS:
            for reward in rewards:
                payload.extend(field_message(3, self._encode_reward_item(reward)))
        return TeachingResult(response_id, bytes(payload))

    def _take_task_reward(
        self, profile: Mapping[str, Any], request_body: bytes
    ) -> TeachingResult:
        try:
            task_id = _first_varint(request_body, 1)
        except ProtobufDecodeError as exc:
            result = self._reward_body(
                SC_TAKE_TEACH_TASK_REWARD_RET,
                0,
                ERROR_MESSAGE_DATA_ILLEGAL,
            )
            return TeachingResult(
                result.response_id, result.body, reason=str(exc)
            )
        if task_id not in self.catalog.task_id_set:
            return self._reward_body(
                SC_TAKE_TEACH_TASK_REWARD_RET, task_id, ERROR_TASK_NOT_FOUND
            )
        state = self.task_state(profile, task_id)
        if not state.unlocked:
            return self._reward_body(
                SC_TAKE_TEACH_TASK_REWARD_RET,
                task_id,
                ERROR_TASK_NOT_UNLOCKED,
            )
        if state.status == TS_REWARDED:
            return self._reward_body(
                SC_TAKE_TEACH_TASK_REWARD_RET,
                task_id,
                ERROR_NO_REWARD_CAN_BE_TAKEN,
            )
        if state.status != TS_FINISHED:
            return self._reward_body(
                SC_TAKE_TEACH_TASK_REWARD_RET,
                task_id,
                ERROR_TASK_HAVE_NOT_FINISHED,
            )

        result = self._reward_body(
            SC_TAKE_TEACH_TASK_REWARD_RET,
            task_id,
            ERROR_SUCCESS,
            self.catalog.task_reward_items[task_id],
        )
        return TeachingResult(
            result.response_id,
            result.body,
            changes=self._task_changes(profile, task_id, TS_REWARDED),
        )

    def _take_stage_reward(
        self, profile: Mapping[str, Any], request_body: bytes
    ) -> TeachingResult:
        try:
            reward_id = _first_varint(request_body, 1)
        except ProtobufDecodeError as exc:
            result = self._reward_body(
                SC_TAKE_TEACH_TASK_EXP_REWARD_RET,
                0,
                ERROR_MESSAGE_DATA_ILLEGAL,
            )
            return TeachingResult(
                result.response_id, result.body, reason=str(exc)
            )
        if reward_id not in self.catalog.reward_id_set:
            return self._reward_body(
                SC_TAKE_TEACH_TASK_EXP_REWARD_RET,
                reward_id,
                ERROR_TASK_NOT_FOUND,
            )
        if reward_id in self.rewarded_ids(profile):
            return self._reward_body(
                SC_TAKE_TEACH_TASK_EXP_REWARD_RET,
                reward_id,
                ERROR_NO_REWARD_CAN_BE_TAKEN,
            )

        reward_row = self.catalog.reward_by_id[reward_id]
        group_id = int(reward_row["task_group"])
        task_states = (
            self.task_state(profile, task_id)
            for task_id in self.catalog.task_ids_by_group[group_id]
        )
        if not all(state.unlocked and state.status == TS_REWARDED for state in task_states):
            return self._reward_body(
                SC_TAKE_TEACH_TASK_EXP_REWARD_RET,
                reward_id,
                ERROR_NO_REWARD_CAN_BE_TAKEN,
            )

        result = self._reward_body(
            SC_TAKE_TEACH_TASK_EXP_REWARD_RET,
            reward_id,
            ERROR_SUCCESS,
            self.catalog.stage_reward_items[reward_id],
        )
        claimed = set(self.rewarded_ids(profile))
        claimed.add(reward_id)
        changes = {
            "teaching_rewarded_ids": [
                item for item in self.catalog.reward_ids if item in claimed
            ]
        }
        return TeachingResult(
            result.response_id,
            result.body,
            changes=changes,
        )

    def handle(
        self,
        request_id: int,
        profile: Optional[Mapping[str, Any]],
        request_body: bytes = b"",
    ) -> Optional[TeachingResult]:
        """Handle commands 820/822/824/826 or return ``None`` for another layer."""

        profile = profile or {}
        if request_id == CS_REQUEST_TEACH_STEP_DATA:
            return TeachingResult(
                SC_TEACH_STEP_DATA_RET,
                self.make_teach_step_data_ret(profile),
            )
        if request_id == CS_SET_TEACH_STEP:
            return self._set_teach_step(profile, request_body)
        if request_id == CS_TAKE_TEACH_TASK_REWARD:
            return self._take_task_reward(profile, request_body)
        if request_id == CS_TAKE_TEACH_TASK_EXP_REWARD:
            return self._take_stage_reward(profile, request_body)
        return None


@lru_cache(maxsize=1)
def default_layer() -> TeachingStateLayer:
    return TeachingStateLayer()


def make_teach_step_data_ret(
    profile: Optional[Mapping[str, Any]] = None,
) -> bytes:
    return default_layer().make_teach_step_data_ret(profile)


def handle_teaching_request(
    request_id: int,
    profile: Optional[Mapping[str, Any]],
    request_body: bytes = b"",
) -> Optional[TeachingResult]:
    return default_layer().handle(request_id, profile, request_body)


__all__ = [
    "CS_REQUEST_TEACH_STEP_DATA",
    "CS_SET_TEACH_STEP",
    "CS_TAKE_TEACH_TASK_EXP_REWARD",
    "CS_TAKE_TEACH_TASK_REWARD",
    "DEFAULT_CONTENT_DIR",
    "ERROR_MESSAGE_DATA_ILLEGAL",
    "ERROR_NO_REWARD_CAN_BE_TAKEN",
    "ERROR_SUCCESS",
    "ERROR_TASK_HAVE_NOT_FINISHED",
    "ERROR_TASK_NOT_FOUND",
    "ERROR_TASK_NOT_UNLOCKED",
    "EXPECTED_TABLE_COUNTS",
    "RESPONSE_IDS",
    "RewardItem",
    "SC_SET_TEACH_STEP_RET",
    "SC_TAKE_TEACH_TASK_EXP_REWARD_RET",
    "SC_TAKE_TEACH_TASK_REWARD_RET",
    "SC_TEACH_STEP_DATA_RET",
    "TS_FINISHED",
    "TS_REWARDED",
    "TS_UNFINISHED",
    "TeachingCatalog",
    "TeachingResult",
    "TeachingStateLayer",
    "TeachingTaskSnapshot",
    "default_layer",
    "handle_teaching_request",
    "make_teach_step_data_ret",
]
