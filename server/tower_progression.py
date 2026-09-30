"""Persistent, table-backed Holy Tower progression query handlers.

The socket runtime owns account transactions.  This module is deliberately
storage agnostic: :func:`handle` receives a profile snapshot and returns a
shallow patch without mutating the snapshot.  Only defined protocol schemas
and generated content tables are used here; ranking, payment and reward-grant
behaviour are intentionally outside this query-only layer.
"""

from __future__ import annotations

import copy
import json
import re
import time
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

try:  # Package imports in tests; fallbacks support direct server execution.
    from .content_profile import ContentCatalog
    from .schema_codec import SchemaCodec, SchemaCodecError
except ImportError:  # pragma: no cover
    from content_profile import ContentCatalog  # type: ignore
    from schema_codec import SchemaCodec, SchemaCodecError  # type: ignore


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONTENT_DIR = ROOT / "generated" / "content"
DEFAULT_PROTOCOL_CATALOG = ROOT / "generated" / "protocol" / "protocol_catalog.json"

CS_REQUEST_TOWER_TASK_DATAS = 720
SC_TOWER_TASK_DATAS_RET = 721
CS_REQUEST_EFFECT_ITEM_HANDBOOK = 766
SC_EFFECT_ITEM_HANDBOOK_RET = 767
CS_REQUEST_TOWER_EVENT_HANDBOOK = 771
SC_TOWER_EVENT_HANDBOOK_RET = 772
CS_GET_TOWER_SCORE_DATA = 1129
SC_GET_TOWER_SCORE_DATA_RET = 1130

REQUEST_NAMES = {
    CS_REQUEST_TOWER_TASK_DATAS: "CS_RequestTowerTaskDatas",
    CS_REQUEST_EFFECT_ITEM_HANDBOOK: "CS_RequestEffectItemHandbook",
    CS_REQUEST_TOWER_EVENT_HANDBOOK: "CS_RequestTowerEventHandbook",
    CS_GET_TOWER_SCORE_DATA: "CS_GetTowerScoreData",
}

RESPONSE_IDS = {
    CS_REQUEST_TOWER_TASK_DATAS: SC_TOWER_TASK_DATAS_RET,
    CS_REQUEST_EFFECT_ITEM_HANDBOOK: SC_EFFECT_ITEM_HANDBOOK_RET,
    CS_REQUEST_TOWER_EVENT_HANDBOOK: SC_TOWER_EVENT_HANDBOOK_RET,
    CS_GET_TOWER_SCORE_DATA: SC_GET_TOWER_SCORE_DATA_RET,
}

RESPONSE_NAMES = {
    SC_TOWER_TASK_DATAS_RET: "SC_TowerTaskDatasRet",
    SC_EFFECT_ITEM_HANDBOOK_RET: "SC_EffectItemHandbookRet",
    SC_TOWER_EVENT_HANDBOOK_RET: "SC_TowerEventHandbookRet",
    SC_GET_TOWER_SCORE_DATA_RET: "SC_GetTowerScoreDataRet",
}

ERROR_SUCCESS = 0
ERROR_CLIENT_REQUEST_PARAM = 32000
ERROR_MESSAGE_DATA_ILLEGAL = 32769

TOWER_TASK_UNFINISHED = 0
TOWER_TASK_FINISHED = 1
TOWER_TASK_REWARDED = 2
TOWER_TASK_END = 3

EFFECT_ITEM_UNLOCKED = 0
EFFECT_ITEM_OBTAINED = 1

SCORE_REWARD_TYPE_SCORE = 1
SCORE_REWARD_TYPE_TASK = 2
SCORE_REWARD_TYPES = (SCORE_REWARD_TYPE_SCORE, SCORE_REWARD_TYPE_TASK)

STATE_KEY = "tower_progression"
STATE_SCHEMA_VERSION = 1
WEEKLY_TASK_COUNT = 4
OFFLINE_RESET_SECONDS = 30 * 24 * 60 * 60
UINT32_MAX = (1 << 32) - 1


class TowerProgressionConfigError(ValueError):
    """Raised when defined content and protocol data no longer agree."""


@dataclass(frozen=True)
class Result:
    """One encoded response and an atomic profile patch for ``server.py``."""

    response_id: int
    body: bytes
    changes: Dict[str, Any] = field(default_factory=dict)
    rewards: Tuple[Any, ...] = ()
    supported: bool = True
    reason: str = ""
    error_code: int = ERROR_SUCCESS
    replayed: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.error_code == ERROR_SUCCESS


@dataclass(frozen=True)
class TowerTaskDefinition:
    task_id: int
    condition_id: int
    need_count: int
    difficulty: int


@dataclass(frozen=True)
class ScoreRewardDefinition:
    reward_id: int
    reward_type: int
    need_score: int


def _int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return int(value)
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _uint(value: Any, default: int = 0, maximum: int = UINT32_MAX) -> int:
    return min(maximum, max(0, _int(value, default)))


def _mapping(value: Any) -> Mapping[Any, Any]:
    return value if isinstance(value, Mapping) else {}


def _sequence(value: Any) -> Sequence[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return value
    return ()


def _config_uint(value: Any, label: str, *, allow_zero: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TowerProgressionConfigError(f"{label} must be an integer")
    if value < 0 or (value == 0 and not allow_zero) or value > UINT32_MAX:
        raise TowerProgressionConfigError(f"{label} is outside uint32 range")
    return value


def _require_fields(table: Any, fields: Iterable[str]) -> None:
    missing = set(fields) - set(table.fields)
    if missing:
        raise TowerProgressionConfigError(
            f"{table.name} is missing required fields: {sorted(missing)}"
        )


class TowerProgressionCatalog:
    """Validated projection of the four authoritative Tower tables."""

    def __init__(self, content: ContentCatalog) -> None:
        self.version = content.version
        try:
            task_table = content.table("a_holytower_task")
            reward_table = content.table("a_holytower_scorereward")
            event_table = content.table("a_holytower_event")
            original_item_table = content.table("a_holytower_original_item")
        except KeyError as exc:
            raise TowerProgressionConfigError(str(exc)) from exc

        _require_fields(
            task_table,
            {"id", "condition", "describe", "difficulty", "reward"},
        )
        _require_fields(
            reward_table,
            {
                "id",
                "task_type",
                "need_scroe",
                "holytowertask_condition",
                "holytowertask_scorereward",
            },
        )
        _require_fields(event_table, {"id", "collection_desc"})
        _require_fields(original_item_table, {"id", "item_id", "group"})

        tasks: List[TowerTaskDefinition] = []
        for row in task_table.records:
            task_id = _config_uint(row.get("id"), "a_holytower_task.id")
            condition_id = _config_uint(
                row.get("condition"), f"a_holytower_task[{task_id}].condition"
            )
            description = row.get("describe")
            if not isinstance(description, str):
                raise TowerProgressionConfigError(
                    f"a_holytower_task[{task_id}].describe must be text"
                )
            # The extracted catalog contains no condition-parameter table.  Its
            # verified task text does contain the target count (3 for the two
            # collection tasks, 1 for every other task), so retain that exact
            # table-backed value instead of guessing from the condition ID.
            count_match = re.search(r"[0-9]+", description)
            if count_match is None:
                raise TowerProgressionConfigError(
                    f"a_holytower_task[{task_id}] has no recoverable target count"
                )
            need_count = _config_uint(
                int(count_match.group(0)),
                f"a_holytower_task[{task_id}].need_count",
            )
            difficulty = _config_uint(
                row.get("difficulty"),
                f"a_holytower_task[{task_id}].difficulty",
                allow_zero=True,
            )
            tasks.append(
                TowerTaskDefinition(task_id, condition_id, need_count, difficulty)
            )
        tasks.sort(key=lambda row: row.task_id)
        if len(tasks) < WEEKLY_TASK_COUNT:
            raise TowerProgressionConfigError(
                f"a_holytower_task has fewer than {WEEKLY_TASK_COUNT} tasks"
            )
        if len({row.task_id for row in tasks}) != len(tasks):
            raise TowerProgressionConfigError("a_holytower_task contains duplicate IDs")
        self.tasks = tuple(tasks)
        self.tasks_by_id = {row.task_id: row for row in tasks}

        reward_rows: Dict[int, List[ScoreRewardDefinition]] = {
            reward_type: [] for reward_type in SCORE_REWARD_TYPES
        }
        for row in reward_table.records:
            reward_id = _config_uint(row.get("id"), "a_holytower_scorereward.id")
            reward_type = _config_uint(
                row.get("task_type"),
                f"a_holytower_scorereward[{reward_id}].task_type",
            )
            if reward_type not in reward_rows:
                continue
            need_score = _config_uint(
                row.get("need_scroe"),
                f"a_holytower_scorereward[{reward_id}].need_scroe",
                allow_zero=reward_type == SCORE_REWARD_TYPE_TASK,
            )
            reward_rows[reward_type].append(
                ScoreRewardDefinition(reward_id, reward_type, need_score)
            )
        for reward_type, rows in reward_rows.items():
            rows.sort(key=lambda row: (row.need_score, row.reward_id))
            minimum = 2 if reward_type == SCORE_REWARD_TYPE_SCORE else 1
            if len(rows) < minimum:
                raise TowerProgressionConfigError(
                    f"a_holytower_scorereward type {reward_type} has too few rows"
                )
            if len({row.reward_id for row in rows}) != len(rows):
                raise TowerProgressionConfigError(
                    f"a_holytower_scorereward type {reward_type} has duplicate IDs"
                )
        self.score_rewards = {
            reward_type: tuple(rows) for reward_type, rows in reward_rows.items()
        }

        event_ids = sorted(
            _config_uint(row.get("id"), "a_holytower_event.id")
            for row in event_table.records
            if str(row.get("collection_desc")) == "1"
        )
        if not event_ids or len(event_ids) != len(set(event_ids)):
            raise TowerProgressionConfigError(
                "a_holytower_event has no unique collectible events"
            )
        self.event_ids = tuple(event_ids)

        # The runtime handbook header is backed by source-item group 5 and
        # renders a fixed ``0/76`` collection total.  Other groups are runtime
        # drop pools (some repeat or combine the same item IDs); exposing all
        # 201 unique IDs makes the protocol response disagree with the
        # runtime's static directory and can break its collection indexing.
        effect_item_ids = sorted(
            {
                _config_uint(
                    row.get("item_id"),
                    f"a_holytower_original_item[{row.get('id')}].item_id",
                )
                for row in original_item_table.records
                if _config_uint(
                    row.get("group"),
                    f"a_holytower_original_item[{row.get('id')}].group",
                )
                == 5
            }
        )
        if not effect_item_ids:
            raise TowerProgressionConfigError(
                "a_holytower_original_item group 5 has no handbook item IDs"
            )
        if len(effect_item_ids) != 76:
            raise TowerProgressionConfigError(
                "a_holytower_original_item group 5 handbook count changed: "
                f"{len(effect_item_ids)}"
            )
        self.effect_item_ids = tuple(effect_item_ids)

    def reward_ids(self, reward_type: int) -> Tuple[int, ...]:
        return tuple(row.reward_id for row in self.score_rewards[reward_type])


class TowerProgressionLayer:
    """Pure handlers for tasks, score rewards and both Tower handbooks."""

    def __init__(
        self,
        content_dir: Path = DEFAULT_CONTENT_DIR,
        protocol_catalog: Path = DEFAULT_PROTOCOL_CATALOG,
        *,
        now: Optional[Any] = None,
    ) -> None:
        content = ContentCatalog.load(Path(content_dir))
        self.catalog = TowerProgressionCatalog(content)
        self.codec = SchemaCodec(Path(protocol_catalog))
        self.protocol_catalog = Path(protocol_catalog).expanduser().resolve()
        self.now = now or (lambda: int(time.time()))
        self._validate_protocol()

    def _validate_protocol(self) -> None:
        document = json.loads(self.protocol_catalog.read_text(encoding="utf-8"))
        endpoints = {
            int(row["request_id"]): row
            for row in document.get("endpoints", [])
            if _uint(row.get("request_id")) in REQUEST_NAMES
        }
        found = {
            request_id: _uint(row.get("response_id"))
            for request_id, row in endpoints.items()
        }
        if found != RESPONSE_IDS:
            raise TowerProgressionConfigError(
                f"Tower progression protocol mapping changed: {found!r}"
            )

        commands = {int(row["id"]): row for row in document.get("commands", [])}
        for request_id, request_name in REQUEST_NAMES.items():
            endpoint = endpoints[request_id]
            if request_name not in set(endpoint.get("request_message_types") or []):
                raise TowerProgressionConfigError(
                    f"endpoint {request_id} does not use request schema {request_name}"
                )
            response_id = RESPONSE_IDS[request_id]
            response_name = RESPONSE_NAMES[response_id]
            if response_name not in set(endpoint.get("response_message_types") or []):
                raise TowerProgressionConfigError(
                    f"endpoint {request_id} does not use response schema {response_name}"
                )
            if request_name not in commands.get(request_id, {}).get("names", []):
                raise TowerProgressionConfigError(
                    f"command {request_id} is not {request_name}"
                )
            if response_name not in commands.get(response_id, {}).get("names", []):
                raise TowerProgressionConfigError(
                    f"command {response_id} is not {response_name}"
                )

        expected_fields = {
            "CS_RequestTowerTaskDatas": (),
            "SC_TowerTaskDatasRet": (
                "WeeklyTaskList",
                "RefreshTaskNum",
                "ResetRemainTime",
            ),
            "TowerTask": ("Id", "Type", "Progress", "NeedCount", "Status"),
            "CS_RequestEffectItemHandbook": (),
            "SC_EffectItemHandbookRet": ("EffectItemMap",),
            "TowerEffectItemHandbook": ("ItemId", "Status", "IsNew"),
            "CS_RequestTowerEventHandbook": (),
            "SC_TowerEventHandbookRet": ("EventIdList",),
            "TowerEventHandbook": ("Id", "IsNew"),
            "CS_GetTowerScoreData": ("Typ",),
            "SC_GetTowerScoreDataRet": (
                "TotalScore",
                "ShowRewardList",
                "FetchRewardList",
                "NextRestTime",
                "Typ",
                "CanFetchRewardList",
            ),
        }
        for message_name, field_names in expected_fields.items():
            schema = self.codec.messages.get(message_name)
            actual = tuple(
                str(field.get("name")) for field in (schema or {}).get("fields", [])
            )
            if actual != field_names:
                raise TowerProgressionConfigError(
                    f"message {message_name} fields changed: {actual!r}"
                )

        field_kinds = {
            ("SC_TowerTaskDatasRet", "WeeklyTaskList"): "repeated",
            ("SC_EffectItemHandbookRet", "EffectItemMap"): "map",
            ("SC_TowerEventHandbookRet", "EventIdList"): "repeated",
            ("SC_GetTowerScoreDataRet", "ShowRewardList"): "repeated",
            ("SC_GetTowerScoreDataRet", "FetchRewardList"): "repeated",
            ("SC_GetTowerScoreDataRet", "CanFetchRewardList"): "repeated",
        }
        for (message_name, field_name), expected_kind in field_kinds.items():
            fields = self.codec.messages[message_name]["fields"]
            field_row = next(row for row in fields if row["name"] == field_name)
            actual_kind = str(field_row.get("type", {}).get("kind") or "")
            if actual_kind != expected_kind:
                raise TowerProgressionConfigError(
                    f"{message_name}.{field_name} is no longer {expected_kind}"
                )

    def _default_state(self, now: int) -> Dict[str, Any]:
        reset_at = now + OFFLINE_RESET_SECONDS
        return {
            "schema_version": STATE_SCHEMA_VERSION,
            "catalog_version": self.catalog.version,
            "score": {
                "total_score": 0,
                "next_reset_at": reset_at,
                "rewards": {
                    str(reward_type): {
                        "claimable_ids": [],
                        "claimed_ids": [],
                    }
                    for reward_type in SCORE_REWARD_TYPES
                },
            },
            "tasks": {
                "weekly": [
                    {
                        "id": definition.task_id,
                        "type": definition.condition_id,
                        "progress": 0,
                        "need_count": definition.need_count,
                        "status": TOWER_TASK_UNFINISHED,
                    }
                    for definition in self.catalog.tasks[:WEEKLY_TASK_COUNT]
                ],
                "refresh_count": 0,
                "reset_at": reset_at,
            },
            # Source-item group 5 is the defined source that enumerates
            # the runtime's 76-entry handbook. Status 0 means unlocked, not
            # obtained; no collection reward or purchase is inferred here.
            "handbook": {
                "effect_items": {
                    str(item_id): {
                        "status": EFFECT_ITEM_UNLOCKED,
                        "is_new": 0,
                    }
                    for item_id in self.catalog.effect_item_ids
                },
                "events": {
                    str(event_id): {"is_new": 0} for event_id in self.catalog.event_ids
                },
            },
        }

    @staticmethod
    def _ordered_ids(value: Any, valid_ids: Sequence[int]) -> List[int]:
        if isinstance(value, Mapping):
            source: Iterable[Any] = value.keys()
        elif isinstance(value, Sequence) and not isinstance(
            value, (str, bytes, bytearray)
        ):
            source = value
        elif isinstance(value, (set, frozenset)):
            source = value
        else:
            source = ()
        selected = {_uint(item) for item in source}
        return [item for item in valid_ids if item in selected]

    def _normalize_score(
        self, raw_state: Mapping[Any, Any], now: int
    ) -> Dict[str, Any]:
        raw_score = _mapping(raw_state.get("score"))
        total_score = _uint(
            raw_score.get("total_score", raw_state.get("total_score", 0))
        )
        next_reset_at = _uint(
            raw_score.get("next_reset_at", raw_state.get("score_reset_at", 0)),
            maximum=(1 << 63) - 1,
        )
        if next_reset_at <= now:
            next_reset_at = now + OFFLINE_RESET_SECONDS

        raw_tracks = _mapping(raw_score.get("rewards"))
        tracks: Dict[str, Dict[str, List[int]]] = {}
        for reward_type in SCORE_REWARD_TYPES:
            raw_track = _mapping(
                raw_tracks.get(str(reward_type), raw_tracks.get(reward_type, {}))
            )
            valid_ids = self.catalog.reward_ids(reward_type)
            claimed = self._ordered_ids(
                raw_track.get("claimed_ids", raw_track.get("claimed", ())),
                valid_ids,
            )
            claimed_set = set(claimed)
            if reward_type == SCORE_REWARD_TYPE_SCORE:
                claimable = [
                    definition.reward_id
                    for definition in self.catalog.score_rewards[reward_type]
                    if definition.need_score <= total_score
                    and definition.reward_id not in claimed_set
                ]
            else:
                # Type-2 rows are gated by condition IDs, not TotalScore.  The
                # extracted data does not prove those condition semantics, so
                # only explicitly persisted eligibility is surfaced.
                claimable = [
                    reward_id
                    for reward_id in self._ordered_ids(
                        raw_track.get("claimable_ids", raw_track.get("claimable", ())),
                        valid_ids,
                    )
                    if reward_id not in claimed_set
                ]
            tracks[str(reward_type)] = {
                "claimable_ids": claimable,
                "claimed_ids": claimed,
            }
        return {
            "total_score": total_score,
            "next_reset_at": next_reset_at,
            "rewards": tracks,
        }

    def _normalize_tasks(
        self, raw_state: Mapping[Any, Any], now: int
    ) -> Dict[str, Any]:
        raw_tasks = _mapping(raw_state.get("tasks"))
        raw_weekly_value = raw_tasks.get("weekly", raw_state.get("weekly_tasks", ()))
        raw_by_id: Dict[int, Mapping[Any, Any]] = {}
        if isinstance(raw_weekly_value, Mapping):
            for key, value in raw_weekly_value.items():
                row = dict(_mapping(value))
                row.setdefault("id", key)
                task_id = _uint(row.get("id"))
                if task_id in self.catalog.tasks_by_id and task_id not in raw_by_id:
                    raw_by_id[task_id] = row
        else:
            for value in _sequence(raw_weekly_value):
                row = _mapping(value)
                task_id = _uint(row.get("id"))
                if task_id in self.catalog.tasks_by_id and task_id not in raw_by_id:
                    raw_by_id[task_id] = row

        chosen_ids = [
            definition.task_id
            for definition in self.catalog.tasks
            if definition.task_id in raw_by_id
        ][:WEEKLY_TASK_COUNT]
        for definition in self.catalog.tasks:
            if len(chosen_ids) >= WEEKLY_TASK_COUNT:
                break
            if definition.task_id not in chosen_ids:
                chosen_ids.append(definition.task_id)

        weekly: List[Dict[str, int]] = []
        for task_id in chosen_ids:
            definition = self.catalog.tasks_by_id[task_id]
            raw = raw_by_id.get(task_id, {})
            progress = min(
                definition.need_count,
                _uint(raw.get("progress")),
            )
            status = _uint(raw.get("status"), maximum=TOWER_TASK_END)
            if status in (TOWER_TASK_FINISHED, TOWER_TASK_REWARDED):
                progress = definition.need_count
            elif status == TOWER_TASK_UNFINISHED and progress >= definition.need_count:
                status = TOWER_TASK_FINISHED
            weekly.append(
                {
                    "id": definition.task_id,
                    "type": definition.condition_id,
                    "progress": progress,
                    "need_count": definition.need_count,
                    "status": status,
                }
            )

        reset_at = _uint(
            raw_tasks.get("reset_at", raw_state.get("task_reset_at", 0)),
            maximum=(1 << 63) - 1,
        )
        if reset_at <= now:
            reset_at = now + OFFLINE_RESET_SECONDS
        return {
            "weekly": weekly,
            "refresh_count": _uint(
                raw_tasks.get("refresh_count", raw_state.get("task_refresh_count", 0))
            ),
            "reset_at": reset_at,
        }

    def _normalize_handbook(self, raw_state: Mapping[Any, Any]) -> Dict[str, Any]:
        raw_handbook = _mapping(raw_state.get("handbook"))
        raw_effects = _mapping(
            raw_handbook.get("effect_items", raw_state.get("effect_item_handbook", {}))
        )
        effects: Dict[str, Dict[str, int]] = {}
        for item_id in self.catalog.effect_item_ids:
            raw = _mapping(raw_effects.get(str(item_id), raw_effects.get(item_id, {})))
            effects[str(item_id)] = {
                "status": min(
                    EFFECT_ITEM_OBTAINED,
                    _uint(raw.get("status"), EFFECT_ITEM_UNLOCKED),
                ),
                "is_new": int(bool(_uint(raw.get("is_new")))),
            }

        raw_events = _mapping(
            raw_handbook.get("events", raw_state.get("event_handbook", {}))
        )
        events: Dict[str, Dict[str, int]] = {}
        for event_id in self.catalog.event_ids:
            raw = _mapping(raw_events.get(str(event_id), raw_events.get(event_id, {})))
            events[str(event_id)] = {"is_new": int(bool(_uint(raw.get("is_new"))))}
        return {"effect_items": effects, "events": events}

    def _ensure_state(self, profile: Mapping[str, Any]) -> Tuple[Dict[str, Any], bool]:
        now = _uint(self.now(), maximum=(1 << 63) - 1)
        raw_value = profile.get(STATE_KEY)
        raw_state = _mapping(raw_value)
        if not raw_state:
            state = self._default_state(now)
        else:
            state = {
                "schema_version": STATE_SCHEMA_VERSION,
                "catalog_version": self.catalog.version,
                "score": self._normalize_score(raw_state, now),
                "tasks": self._normalize_tasks(raw_state, now),
                "handbook": self._normalize_handbook(raw_state),
            }
        changed = not isinstance(raw_value, Mapping) or dict(raw_value) != state
        return state, changed

    @staticmethod
    def _state_changes(state: Mapping[str, Any], changed: bool) -> Dict[str, Any]:
        return {STATE_KEY: copy.deepcopy(dict(state))} if changed else {}

    def _task_values(self, state: Mapping[str, Any]) -> Dict[str, Any]:
        tasks = _mapping(state.get("tasks"))
        now = _uint(self.now(), maximum=(1 << 63) - 1)
        remain = max(1, _uint(tasks.get("reset_at"), maximum=(1 << 63) - 1) - now)
        return {
            "WeeklyTaskList": [
                {
                    "Id": _uint(row.get("id")),
                    "Type": _uint(row.get("type")),
                    "Progress": _uint(row.get("progress")),
                    "NeedCount": max(1, _uint(row.get("need_count"), 1)),
                    "Status": _uint(row.get("status"), maximum=TOWER_TASK_END),
                }
                for row in (_mapping(item) for item in _sequence(tasks.get("weekly")))
            ],
            "RefreshTaskNum": _uint(tasks.get("refresh_count")),
            "ResetRemainTime": min(UINT32_MAX, remain),
        }

    def _effect_values(self, state: Mapping[str, Any]) -> Dict[str, Any]:
        effects = _mapping(_mapping(state.get("handbook")).get("effect_items"))
        return {
            "EffectItemMap": {
                item_id: {
                    "ItemId": item_id,
                    "Status": _uint(
                        _mapping(effects.get(str(item_id))).get("status"),
                        maximum=EFFECT_ITEM_OBTAINED,
                    ),
                    "IsNew": int(
                        bool(_uint(_mapping(effects.get(str(item_id))).get("is_new")))
                    ),
                }
                for item_id in self.catalog.effect_item_ids
            }
        }

    def _event_values(self, state: Mapping[str, Any]) -> Dict[str, Any]:
        events = _mapping(_mapping(state.get("handbook")).get("events"))
        return {
            "EventIdList": [
                {
                    "Id": event_id,
                    "IsNew": int(
                        bool(_uint(_mapping(events.get(str(event_id))).get("is_new")))
                    ),
                }
                for event_id in self.catalog.event_ids
            ]
        }

    def _score_values(
        self, state: Mapping[str, Any], reward_type: int
    ) -> Dict[str, Any]:
        score = _mapping(state.get("score"))
        tracks = _mapping(score.get("rewards"))
        track = _mapping(tracks.get(str(reward_type)))
        # ShowRewardList is deliberately the complete table-backed sequence.
        # The runtime indexes its final two elements without a count guard.
        show_ids = list(self.catalog.reward_ids(reward_type))
        return {
            "TotalScore": _uint(score.get("total_score")),
            "ShowRewardList": show_ids,
            "FetchRewardList": list(_sequence(track.get("claimed_ids"))),
            "NextRestTime": _uint(score.get("next_reset_at"), maximum=(1 << 63) - 1),
            "Typ": reward_type,
            "CanFetchRewardList": list(_sequence(track.get("claimable_ids"))),
        }

    def _result(
        self,
        command_id: int,
        values: Mapping[str, Any],
        *,
        changes: Optional[Mapping[str, Any]] = None,
        reason: str = "",
        error_code: int = ERROR_SUCCESS,
    ) -> Result:
        response_id = RESPONSE_IDS[command_id]
        return Result(
            response_id=response_id,
            body=self.codec.encode(RESPONSE_NAMES[response_id], values),
            changes=copy.deepcopy(dict(changes or {})),
            reason=reason,
            error_code=error_code,
            metadata={
                "repeated_or_map_counts": {
                    key: len(value)
                    for key, value in values.items()
                    if isinstance(value, (list, tuple, dict))
                }
            },
        )

    def _values_for(
        self,
        command_id: int,
        state: Mapping[str, Any],
        request: Mapping[str, Any],
        *,
        safe_score_type: int = SCORE_REWARD_TYPE_SCORE,
    ) -> Dict[str, Any]:
        if command_id == CS_REQUEST_TOWER_TASK_DATAS:
            return self._task_values(state)
        if command_id == CS_REQUEST_EFFECT_ITEM_HANDBOOK:
            return self._effect_values(state)
        if command_id == CS_REQUEST_TOWER_EVENT_HANDBOOK:
            return self._event_values(state)
        reward_type = _uint(request.get("Typ"))
        if reward_type not in SCORE_REWARD_TYPES:
            reward_type = safe_score_type
        return self._score_values(state, reward_type)

    def handle(
        self,
        command_id: int,
        profile: Optional[Mapping[str, Any]],
        body: bytes = b"",
    ) -> Optional[Result]:
        """Handle one supported query without mutating ``profile``."""

        if command_id not in REQUEST_NAMES:
            return None
        profile = profile or {}
        try:
            request = self.codec.decode(REQUEST_NAMES[command_id], bytes(body))
        except (TypeError, ValueError, SchemaCodecError) as exc:
            state, changed = self._ensure_state(profile)
            return self._result(
                command_id,
                self._values_for(command_id, state, {}),
                changes=self._state_changes(state, changed),
                reason=str(exc),
                error_code=ERROR_MESSAGE_DATA_ILLEGAL,
            )

        state, changed = self._ensure_state(profile)
        if command_id == CS_GET_TOWER_SCORE_DATA:
            reward_type = _uint(request.get("Typ"))
            if reward_type not in SCORE_REWARD_TYPES:
                return self._result(
                    command_id,
                    self._score_values(state, SCORE_REWARD_TYPE_SCORE),
                    changes=self._state_changes(state, changed),
                    reason=f"unsupported Tower score type {reward_type}",
                    error_code=ERROR_CLIENT_REQUEST_PARAM,
                )
        return self._result(
            command_id,
            self._values_for(command_id, state, request),
            changes=self._state_changes(state, changed),
        )


@lru_cache(maxsize=1)
def default_layer() -> TowerProgressionLayer:
    return TowerProgressionLayer()


def handle(
    command_id: int,
    profile: Optional[Mapping[str, Any]],
    body: bytes = b"",
) -> Optional[Result]:
    """Module-level entry point for later ``server.py`` integration."""

    return default_layer().handle(command_id, profile, body)


__all__ = [
    "CS_GET_TOWER_SCORE_DATA",
    "CS_REQUEST_EFFECT_ITEM_HANDBOOK",
    "CS_REQUEST_TOWER_EVENT_HANDBOOK",
    "CS_REQUEST_TOWER_TASK_DATAS",
    "DEFAULT_CONTENT_DIR",
    "DEFAULT_PROTOCOL_CATALOG",
    "EFFECT_ITEM_OBTAINED",
    "EFFECT_ITEM_UNLOCKED",
    "ERROR_CLIENT_REQUEST_PARAM",
    "ERROR_MESSAGE_DATA_ILLEGAL",
    "ERROR_SUCCESS",
    "OFFLINE_RESET_SECONDS",
    "REQUEST_NAMES",
    "RESPONSE_IDS",
    "RESPONSE_NAMES",
    "Result",
    "SCORE_REWARD_TYPE_SCORE",
    "SCORE_REWARD_TYPE_TASK",
    "STATE_KEY",
    "STATE_SCHEMA_VERSION",
    "TOWER_TASK_END",
    "TOWER_TASK_FINISHED",
    "TOWER_TASK_REWARDED",
    "TOWER_TASK_UNFINISHED",
    "TowerProgressionCatalog",
    "TowerProgressionConfigError",
    "TowerProgressionLayer",
    "WEEKLY_TASK_COUNT",
    "default_layer",
    "handle",
]
