"""Catalog-backed activity discovery and request responses.

The runtime owns the definition of an activity.  This layer therefore builds
its wire snapshots only from the three defined static tables:

* ``a_main_activity`` selects the fifteen ``activity_type == 3001`` events,
  the activity center, and the separate anniversary parent, then provides
  each activity's ordered ``activity_sub_id`` JSON list;
* ``a_activity_function`` proves every sub-activity used by those events; and
* ``a_activitycenter_function`` proves the sub-activities attached to the
  activity-center main row ``820040001``.

Legacy event dates are deliberately ignored.  The offline profile uses a
``simultaneous`` activity mode so all defined large events can coexist.
No IDs are synthesized from numeric ranges or naming conventions.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Callable, Dict, List, Optional, Tuple

try:  # Package imports in tests; fallback supports direct server execution.
    from .content_profile import ContentCatalog, default_content_catalog
    from .inventory_content import (
        InventoryContentError,
        normalize_inventory,
        stable_item_uid,
    )
    from .protobuf_codec import ProtocolMessageCodec
except ImportError:  # pragma: no cover - ``python server/server.py`` compatibility
    from content_profile import ContentCatalog, default_content_catalog  # type: ignore
    from inventory_content import (  # type: ignore
        InventoryContentError,
        normalize_inventory,
        stable_item_uid,
    )
    from protobuf_codec import ProtocolMessageCodec  # type: ignore


# Message IDs defined in generated/protocol/protocol_catalog.json.
CS_REQUEST_ACHIEVE_DATAS = 316
SC_ACHIEVE_DATAS_RET = 317
CS_REQUEST_DICE_DATAS = 370
SC_DICE_DATAS_RET = 371
NOTIFY_UPDATE_ACTIVITY = 971
CS_REQUEST_ACTIVITY_FORECAST = 1104
SC_REQUEST_ACTIVITY_FORECAST_RET = 1105
CS_REQUEST_ACTIVITY = 1204
SC_REQUEST_ACTIVITY_RET = 1205
CS_RECEIVE_CELEBRATION = 1450
SC_RECEIVE_CELEBRATION = 1451

RESPONSE_IDS = {
    372: 373,
    374: 375,
    CS_REQUEST_ACHIEVE_DATAS: SC_ACHIEVE_DATAS_RET,
    CS_REQUEST_DICE_DATAS: SC_DICE_DATAS_RET,
    CS_REQUEST_ACTIVITY_FORECAST: SC_REQUEST_ACTIVITY_FORECAST_RET,
    CS_REQUEST_ACTIVITY: SC_REQUEST_ACTIVITY_RET,
    CS_RECEIVE_CELEBRATION: SC_RECEIVE_CELEBRATION,
}
INITIAL_NOTIFICATION_IDS = (NOTIFY_UPDATE_ACTIVITY,)

# Static activity identities/types defined from a_main_activity.
LARGE_ACTIVITY_TYPE = 3001
ACTIVITY_CENTER_TYPE = 4001
ACTIVITY_CENTER_ID = 820040001
ANNIVERSARY_ACTIVITY_TYPE = 3003
ANNIVERSARY_ACTIVITY_ID = 821020002
ANNIVERSARY_SUB_ACTIVITY_IDS = (23050042, 23050043, 23050044)
EXPECTED_LARGE_ACTIVITY_COUNT = 15
EXPECTED_ACHIEVEMENT_COUNT = 347
ACTIVITY_STATE_MODE = "simultaneous"

# The defined runtime journey UI has five speed levels.  An empty generic
# response leaves all distance values at zero and its progress calculation
# underflows; this completed snapshot keeps that view internally consistent.
DICE_WALK_SPEED_LEVEL = 5
DICE_WALK_DISTANCE = 1_000
DICE_REMAIN_COUNT = 1
DICE_MOVE_STRENGTH = 1_000

# Msg.TaskStatus value 2 is the completed/rewarded state used by the runtime.
# Collectibles uses this same epoch base for Medal.TakeTime, so achievement
# FinishTime remains aligned with the medal ownership snapshot.
ACHIEVEMENT_STATUS_REWARDED = 2
ACHIEVEMENT_FINISH_TIME_BASE = 1_700_100_000

# Msg.ErrorCode values defined from runtime assembly.
ERROR_SUCCESS = 0
ERROR_ACTIVITY_MAIN_NOT_EXIST = 5001
ERROR_ACTIVITY_SUB_NOT_EXIST = 5002
ERROR_CELEBRATION_ALREADY_RECEIVED = 40044
ERROR_EXCEED_ITEM_LIMIT_COUNT = 429
ERROR_MESSAGE_DATA_ILLEGAL = 32769
ERROR_CONFIG = 32772

UINT32_MAX = (1 << 32) - 1
UINT64_MAX = (1 << 64) - 1

# Msg.ResourceType values proved by the anniversary reward rows.
RESOURCE_ITEM = 4
RESOURCE_GOSPEL = 7
GOSPEL_UID_FLOOR = 2_000_000_000
CELEBRATION_HISTORY_SOURCE = "celebration"

_REQUIRED_TABLE_FIELDS = {
    "a_main_activity": {"id", "activity_type", "activity_sub_id"},
    "a_activity_function": {"id"},
    "a_activitycenter_function": {"id", "reward"},
    "a_medal": {"id", "achieve_id", "condition_count_max", "condition_type"},
    "a_item": {"id", "max_stack_num"},
    "a_gospel": {"id", "using_gospel"},
}

_EXPECTED_PROTOCOL_FIELDS = {
    "Activity": {1: "Id", 2: "ActivitySubFunctionList"},
    "ActivitySubFunction": {1: "Id", 19: "Celebration"},
    "Celebration": {1: "IsOpen", 2: "HadReward"},
    "Notify_UpdateActivity": {1: "ActivityList", 2: "CloseSubActivityIdList"},
    "CS_RequestActivity": {1: "MainActivityId", 2: "SubActivityId"},
    "SC_RequestActivityRet": {
        1: "Errorcode",
        2: "MainActivityId",
        3: "SubActivityId",
        4: "ActivityData",
    },
    "CS_ReceiveCelebration": {1: "MainActivityId", 2: "SubActivityId"},
    "SC_ReceiveCelebration": {
        1: "ErrorCode",
        2: "MainActivityId",
        3: "SubActivityId",
        4: "RewardList",
    },
    "SC_RequestActivityForecastRet": {
        1: "Errorcode",
        2: "Data1",
        3: "Data2",
        4: "Data3",
        5: "PhyrecoTaskList",
        6: "DeadlineTime",
    },
    "ActivityForecastTower": {
        1: "TaskRewardList",
        2: "SeasonRewardList",
        3: "TalentRewardList",
    },
    "ActivityForecastWeek": {1: "WeekRewardList", 2: "CutOffTime"},
    "ActivityForecastTask": {
        1: "DayRewardList",
        2: "WeekRewardList",
        3: "CutOffDayTime",
        4: "CutOffWeekTime",
    },
    "SC_DiceDatasRet": {
        1: "WalkSpeedLevel",
        2: "CurWalkPosition",
        3: "SumWalkDistance",
        4: "RemainDiceNum",
        5: "LogList",
        6: "IsWalkSpeedLvlup",
        7: "HighestWalkSpeedLevel",
        8: "MoveStrength",
        9: "DaysLeftToNextLvl",
    },
    "SC_AchieveDatasRet": {1: "AchieveList"},
    "Achieve": {
        1: "Id",
        2: "Type",
        4: "Progress",
        5: "Status",
        6: "FinishTime",
        7: "IdList",
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


def _celebration_claim_key(main_activity_id: int, sub_activity_id: int) -> str:
    return f"celebration:{main_activity_id}:{sub_activity_id}"


def _claimed_celebration_ids(
    profile: Optional[Mapping[str, Any]], activity_id: int
) -> frozenset[int]:
    if not isinstance(profile, Mapping):
        return frozenset()
    claimed = set()
    state = profile.get("activity_state")
    claims = state.get("celebration_claims") if isinstance(state, Mapping) else None
    values = (
        claims.get(str(activity_id), claims.get(activity_id, ()))
        if isinstance(claims, Mapping)
        else ()
    )
    if isinstance(values, (list, tuple, set, frozenset)):
        claimed.update(
            value
            for value in values
            if type(value) is int and value in ANNIVERSARY_SUB_ACTIVITY_IDS
        )

    # The activity claim and its reward-history entry are written in one
    # shallow-merge patch.  Treat either durable identity as authoritative so
    # a manually defined/legacy save cannot receive the same grant twice.
    history = profile.get("reward_history")
    if isinstance(history, list):
        for entry in history:
            if not isinstance(entry, Mapping):
                continue
            for sub_activity_id in ANNIVERSARY_SUB_ACTIVITY_IDS:
                if entry.get("claim_key") == _celebration_claim_key(
                    activity_id, sub_activity_id
                ):
                    claimed.add(sub_activity_id)
                    continue
                if (
                    entry.get("source") == CELEBRATION_HISTORY_SOURCE
                    and entry.get("main_activity_id") == activity_id
                    and entry.get("sub_activity_id") == sub_activity_id
                ):
                    claimed.add(sub_activity_id)
    return frozenset(
        value
        for value in claimed
        if value in ANNIVERSARY_SUB_ACTIVITY_IDS
    )


class ActivityCatalogError(ValueError):
    """Raised when the generated activity tables do not form a safe closure."""


class ActivityRequestDecodeError(ValueError):
    """Raised when ``CS_RequestActivity`` is malformed protobuf."""


class CelebrationApplicationError(ValueError):
    """Atomic celebration planning failure with a protocol error code."""

    def __init__(
        self,
        error_code: int,
        reason: str,
        *,
        supported: bool = True,
    ) -> None:
        super().__init__(reason)
        self.error_code = int(error_code)
        self.reason = reason
        self.supported = bool(supported)


@dataclass(frozen=True)
class CelebrationReward:
    """One exact ``a_activitycenter_function.reward`` entry."""

    res_type: int
    item_id: int
    count: int

    def protocol_values(self) -> Dict[str, int]:
        return {
            "ResType": self.res_type,
            "ItemId": self.item_id,
            "Count": self.count,
        }

    def history_values(self) -> Dict[str, int]:
        return {
            "res_type": self.res_type,
            "item_id": self.item_id,
            "count": self.count,
        }


@dataclass(frozen=True)
class ActivitySnapshot:
    """One parent activity and its exact, source-ordered child ID list."""

    id: int
    sub_activity_ids: Tuple[int, ...]
    child_table: str

    @property
    def activity_id(self) -> int:
        """Compatibility alias used by service integrations."""

        return self.id

    @property
    def subactivity_ids(self) -> Tuple[int, ...]:
        """Compatibility alias retaining the protocol's spelling."""

        return self.sub_activity_ids

    def protocol_values(
        self, profile: Optional[Mapping[str, Any]] = None
    ) -> Dict[str, Any]:
        """Return a complete ``Msg.Activity`` value for the known state.

        The three counters are explicit zero defaults.  Each sub-function is
        represented by its proved ID; unsupported feature-specific state is
        left at protobuf defaults rather than fabricated.
        """

        return {
            "Id": self.id,
            "ActivitySubFunctionList": [
                self._sub_function(sub_activity_id, profile)
                for sub_activity_id in self.sub_activity_ids
            ],
            "ActivityLoginDays": 0,
            "ActivityTaskFightNumber": 0,
            "TodayPuzzleTimes": 0,
        }

    def _sub_function(
        self,
        sub_activity_id: int,
        profile: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        values: Dict[str, Any] = {"Id": sub_activity_id}
        if self.id == ANNIVERSARY_ACTIVITY_ID:
            values["Celebration"] = {
                "IsOpen": True,
                "HadReward": sub_activity_id
                in _claimed_celebration_ids(profile, self.id),
            }
        return values


@dataclass(frozen=True)
class AchievementSnapshot:
    """One completed medal achievement derived from ``a_medal``."""

    medal_id: int
    achievement_id: int
    achievement_type: int
    progress: int
    finish_time: int

    def protocol_values(self, medal_take_times: Mapping[int, int]) -> Dict[str, Any]:
        take_time = medal_take_times.get(self.medal_id, self.finish_time)
        if type(take_time) is not int or take_time <= 0:
            take_time = self.finish_time
        return {
            "Id": self.achievement_id,
            "Type": self.achievement_type,
            "Progress": self.progress,
            "Status": ACHIEVEMENT_STATUS_REWARDED,
            "FinishTime": take_time,
            "IdList": [],
        }


@dataclass(frozen=True)
class ActivityResult:
    """One activity response plus a shallow-mergeable profile patch."""

    response_id: int
    body: bytes
    changes: Dict[str, Any] = field(default_factory=dict)
    error_code: int = ERROR_SUCCESS
    main_activity_id: int = 0
    sub_activity_id: int = 0
    rewards: Tuple[CelebrationReward, ...] = ()
    supported: bool = True
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.error_code == ERROR_SUCCESS


def _uint32(value: Any, context: str) -> int:
    if type(value) is not int or value <= 0 or value > UINT32_MAX:
        raise ActivityCatalogError(f"{context} must be a positive uint32")
    return value


def _nonnegative_uint32(value: Any, context: str) -> int:
    if type(value) is not int or value < 0 or value > UINT32_MAX:
        raise ActivityCatalogError(f"{context} must be a uint32")
    return value


def _parse_celebration_rewards(
    value: Any,
    context: str,
) -> Tuple[CelebrationReward, ...]:
    """Decode one exact static reward JSON list without inferring defaults."""

    if value in (None, ""):
        return ()
    if not isinstance(value, str):
        raise ActivityCatalogError(f"{context} must be a JSON-array string")
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ActivityCatalogError(f"invalid {context}: {exc}") from exc
    if not isinstance(parsed, list):
        raise ActivityCatalogError(f"{context} must contain a JSON array")

    rewards: List[CelebrationReward] = []
    for index, raw in enumerate(parsed):
        label = f"{context}[{index}]"
        if not isinstance(raw, Mapping):
            raise ActivityCatalogError(f"{label} must be an object")
        rewards.append(
            CelebrationReward(
                res_type=_nonnegative_uint32(raw.get("type"), f"{label}.type"),
                item_id=_nonnegative_uint32(raw.get("id"), f"{label}.id"),
                count=_uint32(raw.get("num"), f"{label}.num"),
            )
        )
    return tuple(rewards)


def _account_uint(
    value: Any,
    context: str,
    *,
    maximum: int = UINT32_MAX,
) -> int:
    """Validate persisted unsigned state without accepting bools/strings."""

    if type(value) is not int or value < 0 or value > maximum:
        raise CelebrationApplicationError(
            ERROR_CONFIG,
            f"{context} must be an unsigned integer",
        )
    return value


def _profile_uint(
    value: Any,
    default: int,
    *,
    minimum: int = 0,
    maximum: int = UINT32_MAX,
) -> int:
    """Normalize persisted integer state without accepting booleans/strings."""

    if type(value) is not int or value < minimum or value > maximum:
        return default
    return value


def _dice_state(profile: Optional[Mapping[str, Any]]) -> Dict[str, int]:
    state = profile.get("activity_state") if isinstance(profile, Mapping) else None
    raw = state.get("dice") if isinstance(state, Mapping) else None
    raw = raw if isinstance(raw, Mapping) else {}
    return {
        "walk_speed_level": DICE_WALK_SPEED_LEVEL,
        "cur_walk_position": DICE_WALK_DISTANCE,
        "sum_walk_distance": DICE_WALK_DISTANCE,
        "remain_dice_num": _profile_uint(
            raw.get("remain_dice_num"),
            DICE_REMAIN_COUNT,
            minimum=DICE_REMAIN_COUNT,
        ),
        "highest_walk_speed_level": DICE_WALK_SPEED_LEVEL,
        "move_strength": _profile_uint(
            raw.get("move_strength"),
            DICE_MOVE_STRENGTH,
            minimum=1,
            maximum=DICE_MOVE_STRENGTH,
        ),
        "days_left_to_next_lvl": _profile_uint(
            raw.get("days_left_to_next_lvl"),
            0,
        ),
    }


def _activity_state_with_dice(
    profile: Optional[Mapping[str, Any]],
) -> Dict[str, Any]:
    previous = profile.get("activity_state") if isinstance(profile, Mapping) else None
    state = copy.deepcopy(dict(previous)) if isinstance(previous, Mapping) else {}
    state["mode"] = ACTIVITY_STATE_MODE
    state["dice"] = _dice_state(profile)
    return state


def _medal_take_times(profile: Optional[Mapping[str, Any]]) -> Dict[int, int]:
    """Index positive persisted Medal.TakeTime values by static medal ID."""

    rows = profile.get("medals") if isinstance(profile, Mapping) else None
    if not isinstance(rows, (list, tuple)):
        return {}
    result: Dict[int, int] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        medal_id = _profile_uint(
            row.get("medal_id", row.get("MedalId", row.get("id"))),
            0,
        )
        take_time = row.get("take_time", row.get("TakeTime"))
        if medal_id and type(take_time) is int and take_time > 0:
            result[medal_id] = take_time
    return result


def _json_uint32_list(value: Any, context: str) -> Tuple[int, ...]:
    """Parse one static JSON-array string without accepting guessed formats."""

    if not isinstance(value, str):
        raise ActivityCatalogError(f"{context} must be a JSON-array string")
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ActivityCatalogError(f"invalid {context}: {exc}") from exc
    if not isinstance(parsed, list):
        raise ActivityCatalogError(f"{context} must contain a JSON array")
    values = tuple(
        _uint32(item, f"{context}[{index}]") for index, item in enumerate(parsed)
    )
    if len(values) != len(set(values)):
        raise ActivityCatalogError(f"{context} contains duplicate IDs")
    return values


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
    raise ActivityRequestDecodeError("invalid or truncated protobuf varint")


def _skip_wire_value(data: bytes, offset: int, wire_type: int) -> int:
    if wire_type == 0:
        _, offset = _read_varint(data, offset)
        return offset
    if wire_type == 1:
        end = offset + 8
    elif wire_type == 2:
        size, offset = _read_varint(data, offset)
        end = offset + size
    elif wire_type == 5:
        end = offset + 4
    else:
        raise ActivityRequestDecodeError(f"unsupported protobuf wire type {wire_type}")
    if end > len(data):
        raise ActivityRequestDecodeError("truncated protobuf field")
    return end


def parse_request_activity(data: bytes) -> Tuple[int, int]:
    """Decode ``CS_RequestActivity`` using normal proto3 last-value wins."""

    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise ActivityRequestDecodeError("request body must be bytes")
    raw = bytes(data)
    offset = 0
    main_activity_id = 0
    sub_activity_id = 0
    while offset < len(raw):
        tag, offset = _read_varint(raw, offset)
        field_number, wire_type = tag >> 3, tag & 7
        if field_number <= 0:
            raise ActivityRequestDecodeError("protobuf field number zero")
        if field_number in {1, 2}:
            if wire_type != 0:
                raise ActivityRequestDecodeError(
                    f"field {field_number} must be a uint32 varint"
                )
            value, offset = _read_varint(raw, offset)
            if value > UINT32_MAX:
                raise ActivityRequestDecodeError(f"field {field_number} exceeds uint32")
            if field_number == 1:
                main_activity_id = value
            else:
                sub_activity_id = value
            continue
        offset = _skip_wire_value(raw, offset, wire_type)
    return main_activity_id, sub_activity_id


def seed_activity_state(
    profile: MutableMapping[str, Any],
) -> MutableMapping[str, Any]:
    """Select coexistence mode and materialize the safe journey snapshot."""

    if not isinstance(profile, MutableMapping):
        raise TypeError("profile must be a mutable mapping")
    profile["activity_state"] = _activity_state_with_dice(profile)
    return profile


class ActivityContentLayer:
    """Build activity notifications and handle ``CS_RequestActivity``."""

    def __init__(
        self,
        catalog: Optional[ContentCatalog] = None,
        codec: Optional[ProtocolMessageCodec] = None,
        achievement_provider: Optional[Callable] = None,
    ) -> None:
        self.catalog = catalog or default_content_catalog()
        self.codec = codec or ProtocolMessageCodec()
        self.achievement_provider = achievement_provider
        self._validate_protocol()
        (
            self.main_activities,
            self.activity_center,
            self.anniversary_activity,
        ) = self._load_activities()
        self.activities = self.main_activities + (
            self.activity_center,
            self.anniversary_activity,
        )
        self.activity_by_id = {activity.id: activity for activity in self.activities}
        self.main_activity_ids = tuple(activity.id for activity in self.main_activities)
        self.activity_center_ids = self.activity_center.sub_activity_ids
        self.activity_battle_tasks = self._load_activity_battle_tasks()
        (
            self.celebration_rewards,
            self.item_max_stacks,
            self.usable_gospel_ids,
        ) = self._load_celebration_reward_catalog()
        self.achievements = self._load_achievements()

    def _load_activity_battle_tasks(self) -> Dict[Tuple[int, int], Tuple[int, ...]]:
        """Map function_type=5 sub-activities to their exact attack-task rows."""

        function_table = self.catalog.table("a_activity_function")
        task_table = self.catalog.table("a_task_activity")
        task_ids_by_function: Dict[int, List[int]] = {}
        for row in task_table.records:
            function_id = _profile_uint(row.get("relate_activity_function"), 0)
            attack = _profile_uint(row.get("attack"), 0)
            task_id = _profile_uint(row.get("id"), 0)
            if function_id and attack and task_id:
                task_ids_by_function.setdefault(function_id, []).append(task_id)

        result: Dict[Tuple[int, int], Tuple[int, ...]] = {}
        for activity in self.activities:
            for sub_activity_id in activity.sub_activity_ids:
                function = function_table.by_id.get(sub_activity_id)
                if not isinstance(function, Mapping):
                    continue
                if _profile_uint(function.get("function_type"), 0) != 5:
                    continue
                function_id = _profile_uint(function.get("function_id"), 0)
                task_ids = tuple(sorted(task_ids_by_function.get(function_id, ())))
                if task_ids:
                    result[(activity.id, sub_activity_id)] = task_ids
        return result

    @staticmethod
    def _activity_task_fight_number(
        profile: Optional[Mapping[str, Any]], activity_id: int, sub_activity_id: int
    ) -> int:
        if not isinstance(profile, Mapping):
            return 0
        special = profile.get("special_battles")
        activity = special.get("activity_task") if isinstance(special, Mapping) else None
        battles = activity.get("battles") if isinstance(activity, Mapping) else None
        battle = (
            battles.get(f"{activity_id}:{sub_activity_id}")
            if isinstance(battles, Mapping)
            else None
        )
        return _profile_uint(
            battle.get("fight_number") if isinstance(battle, Mapping) else None,
            0,
        )

    def _activity_protocol_values(
        self,
        activity: ActivitySnapshot,
        profile: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        values = activity.protocol_values(profile)
        sub_values = values["ActivitySubFunctionList"]
        fight_number = 0
        for index, sub_activity_id in enumerate(activity.sub_activity_ids):
            task_ids = self.activity_battle_tasks.get((activity.id, sub_activity_id))
            if not task_ids:
                continue
            sub_values[index]["OpenTaskList"] = [
                {"Id": task_id, "Progress": 1, "Status": 2}
                for task_id in task_ids
            ]
            fight_number = max(
                fight_number,
                self._activity_task_fight_number(
                    profile, activity.id, sub_activity_id
                ),
            )
        values["ActivityTaskFightNumber"] = fight_number
        return values

    def _validate_protocol(self) -> None:
        for message_name, required in _EXPECTED_PROTOCOL_FIELDS.items():
            schema = self.codec.schema(message_name)
            actual = {
                int(item["number"]): str(item["name"])
                for item in schema.get("fields", [])
            }
            mismatches = {
                number: (name, actual.get(number))
                for number, name in required.items()
                if actual.get(number) != name
            }
            if mismatches:
                raise ActivityCatalogError(
                    f"protocol schema mismatch for {message_name}: {mismatches}"
                )

    def _table(self, name: str) -> Any:
        try:
            table = self.catalog.table(name)
        except KeyError as exc:
            raise ActivityCatalogError(f"content catalog is missing {name}") from exc
        missing = _REQUIRED_TABLE_FIELDS[name] - set(table.fields)
        if missing:
            raise ActivityCatalogError(
                f"{name} is missing required fields: {sorted(missing)}"
            )
        return table

    @staticmethod
    def _validate_children(
        activity_id: int,
        sub_activity_ids: Tuple[int, ...],
        table: Any,
    ) -> None:
        if not sub_activity_ids:
            raise ActivityCatalogError(
                f"a_main_activity[{activity_id}].activity_sub_id is empty"
            )
        missing = [
            sub_activity_id
            for sub_activity_id in sub_activity_ids
            if sub_activity_id not in table.by_id
        ]
        if missing:
            raise ActivityCatalogError(
                f"a_main_activity[{activity_id}] references missing "
                f"{table.name} IDs: {missing}"
            )

    def _load_activities(
        self,
    ) -> Tuple[Tuple[ActivitySnapshot, ...], ActivitySnapshot, ActivitySnapshot]:
        main_table = self._table("a_main_activity")
        function_table = self._table("a_activity_function")
        center_table = self._table("a_activitycenter_function")

        large_activities = []
        center: Optional[ActivitySnapshot] = None
        anniversary: Optional[ActivitySnapshot] = None
        used_large_sub_ids = set()
        for row in main_table.records:
            activity_id = _uint32(row.get("id"), "a_main_activity.id")
            activity_types = _json_uint32_list(
                row.get("activity_type"),
                f"a_main_activity[{activity_id}].activity_type",
            )
            if LARGE_ACTIVITY_TYPE in activity_types:
                child_table = function_table
                child_table_name = function_table.name
            elif activity_id in {ACTIVITY_CENTER_ID, ANNIVERSARY_ACTIVITY_ID}:
                expected_type = (
                    ACTIVITY_CENTER_TYPE
                    if activity_id == ACTIVITY_CENTER_ID
                    else ANNIVERSARY_ACTIVITY_TYPE
                )
                if expected_type not in activity_types:
                    raise ActivityCatalogError(
                        f"a_main_activity[{activity_id}] is not type "
                        f"{expected_type}"
                    )
                child_table = center_table
                child_table_name = center_table.name
            else:
                continue

            sub_activity_ids = _json_uint32_list(
                row.get("activity_sub_id"),
                f"a_main_activity[{activity_id}].activity_sub_id",
            )
            self._validate_children(activity_id, sub_activity_ids, child_table)
            snapshot = ActivitySnapshot(
                id=activity_id,
                sub_activity_ids=sub_activity_ids,
                child_table=child_table_name,
            )
            if activity_id == ACTIVITY_CENTER_ID:
                if center is not None:
                    raise ActivityCatalogError(
                        f"duplicate activity-center main ID {ACTIVITY_CENTER_ID}"
                    )
                center = snapshot
            elif activity_id == ANNIVERSARY_ACTIVITY_ID:
                if anniversary is not None:
                    raise ActivityCatalogError(
                        f"duplicate anniversary main ID {ANNIVERSARY_ACTIVITY_ID}"
                    )
                if sub_activity_ids != ANNIVERSARY_SUB_ACTIVITY_IDS:
                    raise ActivityCatalogError(
                        f"anniversary {ANNIVERSARY_ACTIVITY_ID} has unexpected "
                        f"children {sub_activity_ids}"
                    )
                anniversary = snapshot
            else:
                duplicates = used_large_sub_ids.intersection(sub_activity_ids)
                if duplicates:
                    raise ActivityCatalogError(
                        f"large activities reuse sub-activity IDs: {sorted(duplicates)}"
                    )
                used_large_sub_ids.update(sub_activity_ids)
                large_activities.append(snapshot)

        if len(large_activities) != EXPECTED_LARGE_ACTIVITY_COUNT:
            raise ActivityCatalogError(
                f"expected {EXPECTED_LARGE_ACTIVITY_COUNT} type "
                f"{LARGE_ACTIVITY_TYPE} activities, got {len(large_activities)}"
            )
        if center is None:
            raise ActivityCatalogError(
                f"a_main_activity is missing activity center {ACTIVITY_CENTER_ID}"
            )
        if anniversary is None:
            raise ActivityCatalogError(
                f"a_main_activity is missing anniversary {ANNIVERSARY_ACTIVITY_ID}"
            )
        activity_ids = [activity.id for activity in large_activities]
        if len(activity_ids) != len(set(activity_ids)):
            raise ActivityCatalogError("a_main_activity contains duplicate large IDs")
        return tuple(large_activities), center, anniversary

    def _load_celebration_reward_catalog(
        self,
    ) -> Tuple[
        Dict[int, Tuple[CelebrationReward, ...]],
        Dict[int, int],
        frozenset[int],
    ]:
        """Load the exact anniversary grants and their proved destinations."""

        center_table = self._table("a_activitycenter_function")
        reward_map = {
            sub_activity_id: _parse_celebration_rewards(
                center_table.by_id[sub_activity_id].get("reward"),
                f"a_activitycenter_function[{sub_activity_id}].reward",
            )
            for sub_activity_id in ANNIVERSARY_SUB_ACTIVITY_IDS
        }

        item_max_stacks: Dict[int, int] = {}
        for row in self._table("a_item").records:
            item_id = _uint32(row.get("id"), "a_item.id")
            item_max_stacks[item_id] = _nonnegative_uint32(
                row.get("max_stack_num"),
                f"a_item[{item_id}].max_stack_num",
            )

        usable_gospel_ids = frozenset(
            gospel_id
            for row in self._table("a_gospel").records
            for gospel_id in (_uint32(row.get("id"), "a_gospel.id"),)
            if _nonnegative_uint32(
                row.get("using_gospel"),
                f"a_gospel[{gospel_id}].using_gospel",
            )
            != 0
        )
        return reward_map, item_max_stacks, usable_gospel_ids

    def _load_achievements(self) -> Tuple[AchievementSnapshot, ...]:
        """Build the exact achievement closure referenced by all medals."""

        medal_table = self._table("a_medal")
        achievements = []
        seen_medal_ids = set()
        seen_achievement_ids = set()
        for index, row in enumerate(medal_table.records):
            medal_id = _uint32(row.get("id"), f"a_medal[{index}].id")
            achievement_id = _uint32(
                row.get("achieve_id"), f"a_medal[{medal_id}].achieve_id"
            )
            progress = _uint32(
                row.get("condition_count_max"),
                f"a_medal[{medal_id}].condition_count_max",
            )
            achievement_type = _uint32(
                row.get("condition_type"),
                f"a_medal[{medal_id}].condition_type",
            )
            if medal_id in seen_medal_ids:
                raise ActivityCatalogError(f"a_medal contains duplicate ID {medal_id}")
            if achievement_id in seen_achievement_ids:
                raise ActivityCatalogError(
                    f"a_medal reuses achievement ID {achievement_id}"
                )
            seen_medal_ids.add(medal_id)
            seen_achievement_ids.add(achievement_id)
            achievements.append(
                AchievementSnapshot(
                    medal_id=medal_id,
                    achievement_id=achievement_id,
                    achievement_type=achievement_type,
                    progress=progress,
                    finish_time=ACHIEVEMENT_FINISH_TIME_BASE + index,
                )
            )
        if len(achievements) != EXPECTED_ACHIEVEMENT_COUNT:
            raise ActivityCatalogError(
                f"expected {EXPECTED_ACHIEVEMENT_COUNT} medal achievements, "
                f"got {len(achievements)}"
            )
        return tuple(achievements)

    @staticmethod
    def seed(profile: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
        return seed_activity_state(profile)

    def make_notify_update_activity(
        self,
        profile: Optional[Mapping[str, Any]] = None,
    ) -> bytes:
        """Encode the non-empty simultaneous activity initialization payload."""

        return self.codec.encode(
            "Notify_UpdateActivity",
            {
                "ActivityList": [
                    self._activity_protocol_values(activity, profile)
                    for activity in self.activities
                ],
                "CloseSubActivityIdList": [],
            },
        )

    def initial(
        self,
        profile: Optional[Mapping[str, Any]] = None,
    ) -> Tuple[Tuple[int, bytes], ...]:
        """Return initial server notifications as ``(message_id, body)`` pairs."""

        body = self.make_notify_update_activity(profile)
        if not body:  # A catalog closure with seventeen activities cannot encode empty.
            raise ActivityCatalogError("Notify_UpdateActivity encoded an empty body")
        return ((NOTIFY_UPDATE_ACTIVITY, body),)

    def initial_result(
        self,
        profile: Optional[Mapping[str, Any]] = None,
    ) -> ActivityResult:
        """Result-shaped form of :meth:`initial` for integrations using handlers."""

        message_id, body = self.initial(profile)[0]
        return ActivityResult(response_id=message_id, body=body)

    def request_activity_forecast(self) -> ActivityResult:
        """Return all three forecast objects so the runtime never sees nulls."""

        body = self.codec.encode(
            "SC_RequestActivityForecastRet",
            {
                "Errorcode": ERROR_SUCCESS,
                # Empty claimed-reward lists are semantically safer than
                # inventing reward IDs, but each owning message must exist.
                "Data1": {
                    "TaskRewardList": [],
                    "SeasonRewardList": [],
                    "TalentRewardList": [],
                },
                "Data2": {"WeekRewardList": [], "CutOffTime": 0},
                "Data3": {
                    "DayRewardList": [],
                    "WeekRewardList": [],
                    "CutOffDayTime": 0,
                    "CutOffWeekTime": 0,
                },
                "PhyrecoTaskList": [],
                "DeadlineTime": 0,
            },
        )
        return ActivityResult(
            response_id=SC_REQUEST_ACTIVITY_FORECAST_RET,
            body=body,
        )

    def request_dice_datas(
        self,
        profile: Optional[Mapping[str, Any]],
    ) -> ActivityResult:
        """Return and, when necessary, persist the completed journey state."""

        state = _activity_state_with_dice(profile)
        dice = state["dice"]
        current = profile.get("activity_state") if isinstance(profile, Mapping) else None
        changes = {} if current == state else {"activity_state": state}
        body = self.codec.encode(
            "SC_DiceDatasRet",
            {
                "WalkSpeedLevel": dice["walk_speed_level"],
                "CurWalkPosition": dice["cur_walk_position"],
                "SumWalkDistance": dice["sum_walk_distance"],
                "RemainDiceNum": dice["remain_dice_num"],
                "LogList": [],
                "IsWalkSpeedLvlup": False,
                "HighestWalkSpeedLevel": dice["highest_walk_speed_level"],
                "MoveStrength": dice["move_strength"],
                "DaysLeftToNextLvl": dice["days_left_to_next_lvl"],
            },
        )
        return ActivityResult(
            response_id=SC_DICE_DATAS_RET,
            body=body,
            changes=changes,
        )

    def request_achievement_datas(
        self,
        profile: Optional[Mapping[str, Any]],
    ) -> ActivityResult:
        """Return every medal prerequisite as completed and already rewarded."""

        take_times = _medal_take_times(profile)
        achievements = [achievement.protocol_values(take_times)
                        for achievement in self.achievements]
        if profile and profile.get("player_progression"):
            for row in achievements:
                row.update(Progress=0, Status=0, FinishTime=0)
            projected = {row["Id"]: row for row in achievements}
            for row in profile.get("progression_achievements", {}).values():
                projected[row["Id"]] = dict(row)
            achievements = list(projected.values())
        if self.achievement_provider is not None:
            # UserData's incremental 320 handler cannot create a new ID.
            # Include mutable task criteria in every full 317 snapshot.
            projected = {row['Id']: row for row in achievements}
            projected.update({row['Id']: row for row in self.achievement_provider(profile or {})})
            achievements = list(projected.values())
        body = self.codec.encode(
            "SC_AchieveDatasRet",
            {
                "AchieveList": achievements
            },
        )
        return ActivityResult(response_id=SC_ACHIEVE_DATAS_RET, body=body)

    def _response(
        self,
        error_code: int,
        main_activity_id: int,
        sub_activity_id: int,
        *,
        activity: Optional[ActivitySnapshot] = None,
        profile: Optional[Mapping[str, Any]] = None,
        supported: bool = True,
        reason: str = "",
    ) -> ActivityResult:
        values: Dict[str, Any] = {
            "Errorcode": error_code,
            "MainActivityId": main_activity_id,
            "SubActivityId": sub_activity_id,
        }
        if activity is not None:
            values["ActivityData"] = self._activity_protocol_values(activity, profile)
        return ActivityResult(
            response_id=SC_REQUEST_ACTIVITY_RET,
            body=self.codec.encode("SC_RequestActivityRet", values),
            error_code=error_code,
            main_activity_id=main_activity_id,
            sub_activity_id=sub_activity_id,
            supported=supported,
            reason=reason,
        )

    def request_activity(
        self,
        request_body: bytes,
        profile: Optional[Mapping[str, Any]] = None,
    ) -> ActivityResult:
        try:
            main_activity_id, sub_activity_id = parse_request_activity(request_body)
        except ActivityRequestDecodeError as exc:
            return self._response(
                ERROR_MESSAGE_DATA_ILLEGAL,
                0,
                0,
                supported=False,
                reason=str(exc),
            )

        activity = self.activity_by_id.get(main_activity_id)
        if activity is None:
            return self._response(
                ERROR_ACTIVITY_MAIN_NOT_EXIST,
                main_activity_id,
                sub_activity_id,
                reason=f"unknown main activity ID {main_activity_id}",
            )
        if sub_activity_id not in activity.sub_activity_ids:
            return self._response(
                ERROR_ACTIVITY_SUB_NOT_EXIST,
                main_activity_id,
                sub_activity_id,
                reason=(
                    f"sub-activity ID {sub_activity_id} is not attached to "
                    f"main activity {main_activity_id}"
                ),
            )
        return self._response(
            ERROR_SUCCESS,
            main_activity_id,
            sub_activity_id,
            activity=activity,
            profile=profile,
        )

    def _celebration_reward_changes(
        self,
        profile: Mapping[str, Any],
        main_activity_id: int,
        sub_activity_id: int,
        rewards: Tuple[CelebrationReward, ...],
        claimed: frozenset[int],
    ) -> Dict[str, Any]:
        """Plan one complete celebration transaction without mutating input."""

        if not rewards:
            raise CelebrationApplicationError(
                ERROR_CONFIG,
                f"celebration {sub_activity_id} has no static reward definition",
                supported=False,
            )

        raw_state = profile.get("activity_state")
        if raw_state is not None and not isinstance(raw_state, Mapping):
            raise CelebrationApplicationError(
                ERROR_CONFIG,
                "profile.activity_state must be a mapping",
            )
        raw_claims = (
            raw_state.get("celebration_claims")
            if isinstance(raw_state, Mapping)
            else None
        )
        if raw_claims is not None and not isinstance(raw_claims, Mapping):
            raise CelebrationApplicationError(
                ERROR_CONFIG,
                "profile.activity_state.celebration_claims must be a mapping",
            )

        history_value = profile.get("reward_history", [])
        if not isinstance(history_value, list):
            raise CelebrationApplicationError(
                ERROR_CONFIG,
                "profile.reward_history must be a list",
            )

        # Reject the entire grant before constructing any claim/history state.
        # This keeps mixed or future ResourceType lists all-or-nothing.
        for reward in rewards:
            if reward.res_type == RESOURCE_ITEM:
                maximum = self.item_max_stacks.get(reward.item_id)
                if maximum is None or maximum <= 0:
                    raise CelebrationApplicationError(
                        ERROR_CONFIG,
                        f"celebration item {reward.item_id} is not a storable a_item",
                        supported=False,
                    )
            elif reward.res_type == RESOURCE_GOSPEL:
                if reward.item_id not in self.usable_gospel_ids:
                    raise CelebrationApplicationError(
                        ERROR_CONFIG,
                        f"celebration Gospel {reward.item_id} is not account-usable",
                        supported=False,
                    )
            else:
                raise CelebrationApplicationError(
                    ERROR_CONFIG,
                    f"unsupported celebration ResourceType {reward.res_type}",
                    supported=False,
                )

        has_items = any(reward.res_type == RESOURCE_ITEM for reward in rewards)
        has_gospels = any(reward.res_type == RESOURCE_GOSPEL for reward in rewards)

        inventory: Dict[str, Dict[str, int]] = {}
        if has_items:
            try:
                inventory = normalize_inventory(
                    copy.deepcopy(profile.get("inventory", {}))
                )
            except InventoryContentError as exc:
                raise CelebrationApplicationError(ERROR_CONFIG, str(exc)) from exc

        gospels_value = profile.get("gospels", [])
        if gospels_value is None:
            gospels_value = []
        if has_gospels and not isinstance(gospels_value, list):
            raise CelebrationApplicationError(
                ERROR_CONFIG,
                "profile.gospels must be a list",
            )
        gospels = copy.deepcopy(gospels_value) if isinstance(gospels_value, list) else []

        for reward in rewards:
            if reward.res_type == RESOURCE_ITEM:
                key = str(reward.item_id)
                previous = inventory.get(key)
                current = _account_uint(
                    previous.get("count", 0) if isinstance(previous, Mapping) else 0,
                    f"inventory[{reward.item_id}].count",
                )
                maximum = self.item_max_stacks[reward.item_id]
                updated = current + reward.count
                if current > maximum or updated > maximum:
                    raise CelebrationApplicationError(
                        ERROR_EXCEED_ITEM_LIMIT_COUNT,
                        (
                            f"item {reward.item_id} would exceed "
                            f"a_item.max_stack_num {maximum}"
                        ),
                    )
                inventory[key] = {
                    "uid": int(previous["uid"])
                    if isinstance(previous, Mapping)
                    else stable_item_uid(reward.item_id),
                    "id": reward.item_id,
                    "count": updated,
                    "para": int(previous["para"])
                    if isinstance(previous, Mapping)
                    else 0,
                }
                continue

            for _ in range(reward.count):
                existing_index: Optional[int] = None
                for index, row in enumerate(gospels):
                    if not isinstance(row, Mapping):
                        raise CelebrationApplicationError(
                            ERROR_CONFIG,
                            f"profile.gospels[{index}] must be a mapping",
                        )
                    gospel_id = _account_uint(
                        row.get("id"),
                        f"profile.gospels[{index}].id",
                    )
                    if gospel_id == reward.item_id:
                        existing_index = index
                        break

                if existing_index is not None:
                    row = dict(gospels[existing_index])
                    repeated_num = _account_uint(
                        row.get("repeated_num", 0),
                        f"profile.gospels[{existing_index}].repeated_num",
                    )
                    if repeated_num == UINT32_MAX:
                        raise CelebrationApplicationError(
                            ERROR_CONFIG,
                            "gospel.repeated_num would overflow uint32",
                        )
                    row["repeated_num"] = repeated_num + 1
                    gospels[existing_index] = row
                    continue

                occupied_uids = {GOSPEL_UID_FLOOR}
                for collection_name in ("heroes", "gospels"):
                    rows = gospels if collection_name == "gospels" else profile.get("heroes", [])
                    if not isinstance(rows, list):
                        continue
                    for row in rows:
                        if not isinstance(row, Mapping):
                            continue
                        uid = row.get("uid")
                        if type(uid) is int and 0 < uid <= UINT64_MAX:
                            occupied_uids.add(uid)
                uid = max(occupied_uids) + 1
                if uid > UINT64_MAX:
                    raise CelebrationApplicationError(
                        ERROR_CONFIG,
                        "no uint64 Gospel UID remains",
                    )
                gospels.append(
                    {
                        "uid": uid,
                        "id": reward.item_id,
                        "level": 1,
                        "exp": 0,
                        "is_new": 1,
                        "appearance_id": reward.item_id,
                        "appearance_id_2d": reward.item_id,
                        "repeated_num": 0,
                    }
                )

        state = _activity_state_with_dice(profile)
        claims = copy.deepcopy(dict(raw_claims)) if isinstance(raw_claims, Mapping) else {}
        claims[str(main_activity_id)] = sorted(set(claimed) | {sub_activity_id})
        claims.pop(main_activity_id, None)
        state["celebration_claims"] = claims

        history = copy.deepcopy(history_value)
        history.append(
            {
                "claim_key": _celebration_claim_key(
                    main_activity_id, sub_activity_id
                ),
                "source": CELEBRATION_HISTORY_SOURCE,
                "main_activity_id": main_activity_id,
                "sub_activity_id": sub_activity_id,
                "rewards": [reward.history_values() for reward in rewards],
            }
        )

        changes: Dict[str, Any] = {"activity_state": state}
        if has_items:
            changes["inventory"] = {
                key: inventory[key]
                for key in sorted(inventory, key=lambda value: int(value))
            }
        if has_gospels:
            changes["gospels"] = gospels
        changes["reward_history"] = history
        return changes

    def _celebration_response(
        self,
        error_code: int,
        main_activity_id: int,
        sub_activity_id: int,
        *,
        changes: Optional[Dict[str, Any]] = None,
        rewards: Tuple[CelebrationReward, ...] = (),
        supported: bool = True,
        reason: str = "",
    ) -> ActivityResult:
        return ActivityResult(
            response_id=SC_RECEIVE_CELEBRATION,
            body=self.codec.encode(
                "SC_ReceiveCelebration",
                {
                    "ErrorCode": error_code,
                    "MainActivityId": main_activity_id,
                    "SubActivityId": sub_activity_id,
                    "RewardList": [
                        reward.protocol_values() for reward in rewards
                    ],
                },
            ),
            changes=changes or {},
            error_code=error_code,
            main_activity_id=main_activity_id,
            sub_activity_id=sub_activity_id,
            rewards=rewards,
            supported=supported,
            reason=reason,
        )

    def receive_celebration(
        self,
        profile: Optional[Mapping[str, Any]],
        request_body: bytes,
    ) -> ActivityResult:
        try:
            main_activity_id, sub_activity_id = parse_request_activity(request_body)
        except ActivityRequestDecodeError as exc:
            return self._celebration_response(
                ERROR_MESSAGE_DATA_ILLEGAL,
                0,
                0,
                reason=str(exc),
            )

        if main_activity_id not in self.activity_by_id:
            return self._celebration_response(
                ERROR_ACTIVITY_MAIN_NOT_EXIST,
                main_activity_id,
                sub_activity_id,
                reason=f"unknown main activity ID {main_activity_id}",
            )
        if (
            main_activity_id != ANNIVERSARY_ACTIVITY_ID
            or sub_activity_id not in ANNIVERSARY_SUB_ACTIVITY_IDS
        ):
            return self._celebration_response(
                ERROR_ACTIVITY_SUB_NOT_EXIST,
                main_activity_id,
                sub_activity_id,
                reason=(
                    f"sub-activity ID {sub_activity_id} has no proved "
                    "Celebration state"
                ),
            )
        claimed = _claimed_celebration_ids(profile, main_activity_id)
        if sub_activity_id in claimed:
            return self._celebration_response(
                ERROR_CELEBRATION_ALREADY_RECEIVED,
                main_activity_id,
                sub_activity_id,
                reason=f"celebration {sub_activity_id} was already received",
            )
        rewards = self.celebration_rewards.get(sub_activity_id, ())
        account = profile if isinstance(profile, Mapping) else {}
        try:
            changes = self._celebration_reward_changes(
                account,
                main_activity_id,
                sub_activity_id,
                rewards,
                claimed,
            )
        except CelebrationApplicationError as exc:
            return self._celebration_response(
                exc.error_code,
                main_activity_id,
                sub_activity_id,
                supported=exc.supported,
                reason=exc.reason,
            )
        return self._celebration_response(
            ERROR_SUCCESS,
            main_activity_id,
            sub_activity_id,
            changes=changes,
            rewards=rewards,
        )

    def handle(
        self,
        request_id: int,
        profile: Optional[Mapping[str, Any]],
        request_body: bytes = b"",
    ) -> Optional[ActivityResult]:
        """Handle activity discovery/celebration or leave another layer alone."""

        if request_id in (370, 372, 374) and getattr(self, 'journey', None) is not None:
            return self.journey.handle(request_id, profile, request_body)

        if int(request_id) == CS_REQUEST_ACHIEVE_DATAS:
            return self.request_achievement_datas(profile)
        if int(request_id) == CS_REQUEST_DICE_DATAS:
            return self.request_dice_datas(profile)
        if int(request_id) == CS_REQUEST_ACTIVITY_FORECAST:
            return self.request_activity_forecast()
        if int(request_id) == CS_REQUEST_ACTIVITY:
            return self.request_activity(request_body, profile)
        if int(request_id) == CS_RECEIVE_CELEBRATION:
            return self.receive_celebration(profile, request_body)
        return None


@lru_cache(maxsize=1)
def default_layer() -> ActivityContentLayer:
    return ActivityContentLayer()


def make_notify_update_activity(
    profile: Optional[Mapping[str, Any]] = None,
) -> bytes:
    return default_layer().make_notify_update_activity(profile)


def initial_activity_messages(
    profile: Optional[Mapping[str, Any]] = None,
) -> Tuple[Tuple[int, bytes], ...]:
    return default_layer().initial(profile)


def handle_activity_request(
    request_id: int,
    profile: Optional[Mapping[str, Any]],
    request_body: bytes = b"",
) -> Optional[ActivityResult]:
    return default_layer().handle(request_id, profile, request_body)


__all__ = [
    "ACHIEVEMENT_FINISH_TIME_BASE",
    "ACHIEVEMENT_STATUS_REWARDED",
    "ACTIVITY_CENTER_ID",
    "ACTIVITY_CENTER_TYPE",
    "ACTIVITY_STATE_MODE",
    "ANNIVERSARY_ACTIVITY_ID",
    "ANNIVERSARY_ACTIVITY_TYPE",
    "ANNIVERSARY_SUB_ACTIVITY_IDS",
    "ActivityCatalogError",
    "ActivityContentLayer",
    "ActivityRequestDecodeError",
    "ActivityResult",
    "ActivitySnapshot",
    "AchievementSnapshot",
    "CELEBRATION_HISTORY_SOURCE",
    "CelebrationApplicationError",
    "CelebrationReward",
    "CS_REQUEST_ACHIEVE_DATAS",
    "CS_REQUEST_ACTIVITY_FORECAST",
    "CS_REQUEST_DICE_DATAS",
    "CS_RECEIVE_CELEBRATION",
    "CS_REQUEST_ACTIVITY",
    "DICE_MOVE_STRENGTH",
    "DICE_REMAIN_COUNT",
    "DICE_WALK_DISTANCE",
    "DICE_WALK_SPEED_LEVEL",
    "ERROR_ACTIVITY_MAIN_NOT_EXIST",
    "ERROR_ACTIVITY_SUB_NOT_EXIST",
    "ERROR_CELEBRATION_ALREADY_RECEIVED",
    "ERROR_CONFIG",
    "ERROR_EXCEED_ITEM_LIMIT_COUNT",
    "ERROR_MESSAGE_DATA_ILLEGAL",
    "ERROR_SUCCESS",
    "EXPECTED_LARGE_ACTIVITY_COUNT",
    "EXPECTED_ACHIEVEMENT_COUNT",
    "INITIAL_NOTIFICATION_IDS",
    "LARGE_ACTIVITY_TYPE",
    "NOTIFY_UPDATE_ACTIVITY",
    "RESOURCE_GOSPEL",
    "RESOURCE_ITEM",
    "RESPONSE_IDS",
    "SC_RECEIVE_CELEBRATION",
    "SC_ACHIEVE_DATAS_RET",
    "SC_DICE_DATAS_RET",
    "SC_REQUEST_ACTIVITY_FORECAST_RET",
    "SC_REQUEST_ACTIVITY_RET",
    "default_layer",
    "handle_activity_request",
    "initial_activity_messages",
    "make_notify_update_activity",
    "parse_request_activity",
    "seed_activity_state",
]
