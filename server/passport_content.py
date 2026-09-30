"""Catalog-backed offline battle-pass state for the latest bundled season.

The runtime stores seventeen legacy ``a_main_activity`` passport rows but
can display one ``ValidPassportGroup`` at a time.  This layer selects the
latest statically present group, opens both premium tracks locally, exposes
the maximum base/endless progress, and persists reward-claim records.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

try:
    from .content_profile import ContentCatalog, default_content_catalog
    from .inventory_content import normalize_inventory, stable_item_uid
    from .protobuf_codec import ProtocolMessageCodec
except ImportError:  # pragma: no cover - direct server execution
    from content_profile import ContentCatalog, default_content_catalog  # type: ignore
    from inventory_content import normalize_inventory, stable_item_uid  # type: ignore
    from protobuf_codec import ProtocolMessageCodec  # type: ignore


CS_REQUEST_PASSPORT_DATA = 906
SC_REQUEST_PASSPORT_DATA_RET = 907
CS_TAKE_PASSPORT_REWARD = 908
SC_TAKE_PASSPORT_REWARD_RET = 909
CS_PURCHASE_PASSPORT_LEVEL = 910
SC_PURCHASE_PASSPORT_LEVEL_RET = 911

RESPONSE_IDS = {
    CS_REQUEST_PASSPORT_DATA: SC_REQUEST_PASSPORT_DATA_RET,
    CS_TAKE_PASSPORT_REWARD: SC_TAKE_PASSPORT_REWARD_RET,
    CS_PURCHASE_PASSPORT_LEVEL: SC_PURCHASE_PASSPORT_LEVEL_RET,
}

PASSPORT_ACTIVITY_TYPE = 2001
PASSPORT_STATE_REVISION = 1
OFFLINE_END_TIMESTAMP = 3_376_684_800  # 2077-01-01T00:00:00Z
RESOURCE_GOLD = 2
RESOURCE_ITEM = 4
RESOURCE_CAP = 1_000_000_000
ERROR_SUCCESS = 0
ERROR_MESSAGE_DATA_ILLEGAL = 32769
ERROR_CONFIG = 32772
UINT32_MAX = (1 << 32) - 1

_REQUIRED_FIELDS = {
    "a_main_activity": {"id", "activity_type", "activity_sub_id"},
    "a_battlepass": {
        "battlepass_group",
        "level",
        "level_exp",
        "free_reward",
        "pay_reward",
    },
    "a_battlepass_endless": {
        "battlepass_group",
        "level",
        "level_exp",
        "free_reward",
        "pay_reward",
    },
    "a_battlepass_senior": {"battlepass_group", "id_link"},
    "a_item": {"id", "max_stack_num"},
}

_PROTOCOL_FIELDS = {
    "PassportData": {
        1: "PassportGroup",
        2: "PassportLevel",
        3: "PassportExp",
        5: "PassportExpWeekIncrement",
        6: "HasBoughtPassportGear1",
        7: "HasBoughtPassportGear2",
        8: "ValidPassportGroup",
    },
    "PassportRewardRecord": {1: "PassportLevel", 2: "IsPay", 3: "Stage"},
    "SC_RequestPassportDataRet": {
        1: "Passport",
        2: "RewardRecordList",
        3: "EndTimestamp",
    },
    "SC_TakePassportRewardRet": {
        1: "Errorcode",
        2: "PassportLevel",
        3: "IsPayReward",
        4: "RewardRecordList",
        5: "RewardList",
        6: "IsTakeAll",
        7: "Stage",
    },
    "SC_PurchasePassportLevelRet": {1: "Errorcode", 2: "LevelNum"},
    "RewardItem": {1: "ResType", 2: "ItemId", 3: "Count"},
}


class PassportCatalogError(ValueError):
    """Raised when the defined passport tables are not a closed model."""


@dataclass(frozen=True)
class PassportReward:
    res_type: int
    item_id: int
    count: int

    def protocol_values(self) -> Dict[str, int]:
        return {
            "ResType": self.res_type,
            "ItemId": self.item_id,
            "Count": self.count,
        }


@dataclass(frozen=True)
class PassportLevel:
    stage: int
    level: int
    level_exp: int
    free_rewards: Tuple[PassportReward, ...]
    pay_rewards: Tuple[PassportReward, ...]


@dataclass(frozen=True)
class PassportResult:
    response_id: int
    body: bytes
    changes: Dict[str, Any] = field(default_factory=dict)
    error_code: int = ERROR_SUCCESS
    supported: bool = True
    reason: str = ""


def _as_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return default


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
    raise ValueError("invalid or truncated protobuf varint")


def _scalar_fields(data: bytes) -> Dict[int, int]:
    values: Dict[int, int] = {}
    offset = 0
    while offset < len(data):
        tag, offset = _read_varint(data, offset)
        number, wire_type = tag >> 3, tag & 7
        if number <= 0 or wire_type != 0:
            raise ValueError("passport request must contain scalar varints")
        value, offset = _read_varint(data, offset)
        if value > UINT32_MAX:
            raise ValueError("passport request value exceeds uint32")
        values[number] = value
    return values


def _json_int_list(value: Any, label: str) -> Tuple[int, ...]:
    if not isinstance(value, str):
        raise PassportCatalogError(f"{label} must be JSON text")
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise PassportCatalogError(f"{label} is invalid JSON") from exc
    if not isinstance(parsed, list) or any(type(item) is not int for item in parsed):
        raise PassportCatalogError(f"{label} must be an integer array")
    return tuple(parsed)


def _rewards(value: Any, label: str) -> Tuple[PassportReward, ...]:
    if value in (None, ""):
        return ()
    if not isinstance(value, str):
        raise PassportCatalogError(f"{label} must be JSON text")
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise PassportCatalogError(f"{label} is invalid JSON") from exc
    if not isinstance(parsed, list):
        raise PassportCatalogError(f"{label} must be an array")
    result = []
    for index, row in enumerate(parsed):
        if not isinstance(row, Mapping):
            raise PassportCatalogError(f"{label}[{index}] must be an object")
        res_type = _as_int(row.get("type"), -1)
        item_id = _as_int(row.get("id"), -1)
        count = _as_int(row.get("num"), -1)
        if res_type not in {RESOURCE_GOLD, RESOURCE_ITEM} or item_id < 0 or count <= 0:
            raise PassportCatalogError(f"{label}[{index}] has unsupported reward data")
        if res_type == RESOURCE_GOLD and item_id != 0:
            raise PassportCatalogError(f"{label}[{index}] gold ID must be zero")
        if res_type == RESOURCE_ITEM and item_id <= 0:
            raise PassportCatalogError(f"{label}[{index}] item ID must be positive")
        result.append(PassportReward(res_type, item_id, count))
    return tuple(result)


def _record_key(level: int, is_pay: int, stage: int) -> str:
    return f"{stage}:{level}:{is_pay}"


class PassportContentLayer:
    """Serve and persist the latest bundled battle pass."""

    def __init__(
        self,
        catalog: Optional[ContentCatalog] = None,
        codec: Optional[ProtocolMessageCodec] = None,
    ) -> None:
        self.catalog = catalog or default_content_catalog()
        self.codec = codec or ProtocolMessageCodec()
        self._validate_protocol()
        self.group, self.base_levels, self.endless_levels = self._load_levels()
        self.level_by_stage = {
            1: {row.level: row for row in self.base_levels},
            2: {row.level: row for row in self.endless_levels},
        }
        self.max_level = max(row.level for row in self.base_levels)
        self.max_exp = max(row.level_exp for row in self.endless_levels)
        item_table = self._table("a_item")
        self.item_caps = {
            _as_int(row.get("id")): max(1, _as_int(row.get("max_stack_num"), 1))
            for row in item_table.records
        }
        self._validate_reward_items()

    def _table(self, name: str) -> Any:
        try:
            table = self.catalog.table(name)
        except KeyError as exc:
            raise PassportCatalogError(f"content catalog is missing {name}") from exc
        missing = _REQUIRED_FIELDS[name] - set(table.fields)
        if missing:
            raise PassportCatalogError(f"{name} is missing fields {sorted(missing)}")
        return table

    def _validate_protocol(self) -> None:
        for name, required in _PROTOCOL_FIELDS.items():
            schema = self.codec.schema(name)
            actual = {
                int(row["number"]): str(row["name"])
                for row in schema.get("fields", [])
            }
            if any(actual.get(number) != field for number, field in required.items()):
                raise PassportCatalogError(f"protocol schema mismatch for {name}")

    def _load_levels(
        self,
    ) -> Tuple[int, Tuple[PassportLevel, ...], Tuple[PassportLevel, ...]]:
        main = self._table("a_main_activity")
        groups = []
        for row in main.records:
            activity_id = _as_int(row.get("id"))
            types = _json_int_list(
                row.get("activity_type"), f"a_main_activity[{activity_id}].activity_type"
            )
            if PASSPORT_ACTIVITY_TYPE not in types:
                continue
            group = _as_int(row.get("activity_sub_id"))
            if group <= 0:
                raise PassportCatalogError(f"passport activity {activity_id} has no group")
            groups.append(group)
        if groups != list(range(1, 18)):
            raise PassportCatalogError(f"unexpected passport groups {groups}")
        group = max(groups)

        def levels(table_name: str, stage: int) -> Tuple[PassportLevel, ...]:
            table = self._table(table_name)
            rows = []
            for row in table.records:
                if _as_int(row.get("battlepass_group")) != group:
                    continue
                level = _as_int(row.get("level"))
                rows.append(
                    PassportLevel(
                        stage=stage,
                        level=level,
                        level_exp=_as_int(row.get("level_exp")),
                        free_rewards=_rewards(
                            row.get("free_reward"), f"{table_name}[{level}].free_reward"
                        ),
                        pay_rewards=_rewards(
                            row.get("pay_reward"), f"{table_name}[{level}].pay_reward"
                        ),
                    )
                )
            rows.sort(key=lambda item: item.level)
            if not rows or [row.level for row in rows] != list(range(1, len(rows) + 1)):
                raise PassportCatalogError(f"{table_name} group {group} is not contiguous")
            return tuple(rows)

        senior = self._table("a_battlepass_senior")
        senior_links = {
            _as_int(row.get("id_link"))
            for row in senior.records
            if _as_int(row.get("battlepass_group")) == group
        }
        if senior_links != {1130021, 1130022, 1130023}:
            raise PassportCatalogError(f"passport group {group} premium tracks are incomplete")
        return group, levels("a_battlepass", 1), levels("a_battlepass_endless", 2)

    def _validate_reward_items(self) -> None:
        missing = sorted(
            {
                reward.item_id
                for level in self.base_levels + self.endless_levels
                for reward in level.free_rewards + level.pay_rewards
                if reward.res_type == RESOURCE_ITEM and reward.item_id not in self.item_caps
            }
        )
        if missing:
            raise PassportCatalogError(f"passport rewards reference missing items {missing}")

    def seed(self, profile: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
        previous = profile.get("passport_state")
        state = copy.deepcopy(previous) if isinstance(previous, Mapping) else {}
        records = state.get("reward_records")
        if not isinstance(records, Mapping):
            records = {}
        state.update(
            {
                "revision": PASSPORT_STATE_REVISION,
                "group": self.group,
                "level": self.max_level,
                "exp": self.max_exp,
                "weekly_exp": 0,
                "premium_gear_1": True,
                "premium_gear_2": True,
                "end_timestamp": OFFLINE_END_TIMESTAMP,
                "reward_records": dict(records),
            }
        )
        profile["passport_state"] = state
        return profile

    def _state(self, profile: Mapping[str, Any]) -> Dict[str, Any]:
        state: Dict[str, Any] = {}
        previous = profile.get("passport_state")
        if isinstance(previous, Mapping):
            state.update(copy.deepcopy(previous))
        holder: Dict[str, Any] = {"passport_state": state}
        self.seed(holder)
        return dict(holder["passport_state"])

    def _record_values(self, state: Mapping[str, Any]) -> List[Dict[str, int]]:
        records = state.get("reward_records", {})
        if not isinstance(records, Mapping):
            return []
        result = []
        for row in records.values():
            if not isinstance(row, Mapping):
                continue
            result.append(
                {
                    "PassportLevel": _as_int(row.get("level")),
                    "IsPay": _as_int(row.get("is_pay")),
                    "Stage": _as_int(row.get("stage"), 1),
                }
            )
        result.sort(key=lambda row: (row["Stage"], row["PassportLevel"], row["IsPay"]))
        return result

    def _passport_values(self, state: Mapping[str, Any]) -> Dict[str, int]:
        return {
            "PassportGroup": self.group,
            "PassportLevel": self.max_level,
            "PassportExp": self.max_exp,
            "PassportExpWeekIncrement": 0,
            "HasBoughtPassportGear1": 1,
            "HasBoughtPassportGear2": 1,
            "ValidPassportGroup": self.group,
        }

    def query(self, profile: Mapping[str, Any]) -> PassportResult:
        state = self._state(profile)
        body = self.codec.encode(
            "SC_RequestPassportDataRet",
            {
                "Passport": self._passport_values(state),
                "RewardRecordList": self._record_values(state),
                "EndTimestamp": OFFLINE_END_TIMESTAMP,
            },
        )
        changes = {} if profile.get("passport_state") == state else {"passport_state": state}
        return PassportResult(SC_REQUEST_PASSPORT_DATA_RET, body, changes)

    @staticmethod
    def _aggregate(rewards: Iterable[PassportReward]) -> Tuple[PassportReward, ...]:
        order: List[Tuple[int, int]] = []
        totals: Dict[Tuple[int, int], int] = {}
        for reward in rewards:
            key = (reward.res_type, reward.item_id)
            if key not in totals:
                order.append(key)
                totals[key] = 0
            totals[key] += reward.count
        return tuple(PassportReward(kind, item, totals[(kind, item)]) for kind, item in order)

    def _reward_changes(
        self, profile: Mapping[str, Any], rewards: Sequence[PassportReward]
    ) -> Dict[str, Any]:
        inventory = normalize_inventory(copy.deepcopy(profile.get("inventory", {})))
        inventory_changed = False
        gold = max(0, _as_int(profile.get("gold")))
        original_gold = gold
        for reward in rewards:
            if reward.res_type == RESOURCE_GOLD:
                gold = min(RESOURCE_CAP, gold + reward.count)
                continue
            key = str(reward.item_id)
            previous = inventory.get(key)
            current = _as_int(previous.get("count")) if isinstance(previous, Mapping) else 0
            updated = min(self.item_caps[reward.item_id], current + reward.count)
            inventory[key] = {
                "uid": _as_int(previous.get("uid"))
                if isinstance(previous, Mapping) and _as_int(previous.get("uid")) > 0
                else stable_item_uid(reward.item_id),
                "id": reward.item_id,
                "count": updated,
                "para": _as_int(previous.get("para")) if isinstance(previous, Mapping) else 0,
            }
            inventory_changed = inventory_changed or updated != current
        changes: Dict[str, Any] = {}
        if gold != original_gold:
            changes["gold"] = gold
        if inventory_changed:
            changes["inventory"] = inventory
        return changes

    def take_reward(self, profile: Mapping[str, Any], body: bytes) -> PassportResult:
        try:
            values = _scalar_fields(body)
        except ValueError as exc:
            return PassportResult(
                SC_TAKE_PASSPORT_REWARD_RET,
                self.codec.encode("SC_TakePassportRewardRet", {"Errorcode": ERROR_MESSAGE_DATA_ILLEGAL}),
                error_code=ERROR_MESSAGE_DATA_ILLEGAL,
                supported=False,
                reason=str(exc),
            )
        level = values.get(1, 0)
        is_pay = 1 if values.get(2, 0) else 0
        take_all = 1 if values.get(3, 0) else 0
        stage = values.get(4, 1) or 1
        levels = self.level_by_stage.get(stage)
        if levels is None or (not take_all and level not in levels):
            response = {
                "Errorcode": ERROR_CONFIG,
                "PassportLevel": level,
                "IsPayReward": is_pay,
                "IsTakeAll": take_all,
                "Stage": stage,
            }
            return PassportResult(
                SC_TAKE_PASSPORT_REWARD_RET,
                self.codec.encode("SC_TakePassportRewardRet", response),
                error_code=ERROR_CONFIG,
                reason="unknown passport stage or level",
            )

        state = self._state(profile)
        records = dict(state.get("reward_records", {}))
        selected: List[Tuple[PassportLevel, int]] = []
        if take_all:
            for row in levels.values():
                for paid in (0, 1):
                    if _record_key(row.level, paid, stage) not in records:
                        selected.append((row, paid))
        elif _record_key(level, is_pay, stage) not in records:
            selected.append((levels[level], is_pay))

        rewards = self._aggregate(
            reward
            for row, paid in selected
            for reward in (row.pay_rewards if paid else row.free_rewards)
        )
        for row, paid in selected:
            key = _record_key(row.level, paid, stage)
            records[key] = {"level": row.level, "is_pay": paid, "stage": stage}
        state["reward_records"] = records
        changes = self._reward_changes(profile, rewards)
        changes["passport_state"] = state
        response = {
            "Errorcode": ERROR_SUCCESS,
            "PassportLevel": level,
            "IsPayReward": is_pay,
            "RewardRecordList": self._record_values(state),
            "RewardList": [reward.protocol_values() for reward in rewards],
            "IsTakeAll": take_all,
            "Stage": stage,
        }
        return PassportResult(
            SC_TAKE_PASSPORT_REWARD_RET,
            self.codec.encode("SC_TakePassportRewardRet", response),
            changes,
        )

    def purchase_level(self, profile: Mapping[str, Any], body: bytes) -> PassportResult:
        del profile
        try:
            _scalar_fields(body)
        except ValueError as exc:
            return PassportResult(
                SC_PURCHASE_PASSPORT_LEVEL_RET,
                self.codec.encode("SC_PurchasePassportLevelRet", {"Errorcode": ERROR_MESSAGE_DATA_ILLEGAL}),
                error_code=ERROR_MESSAGE_DATA_ILLEGAL,
                supported=False,
                reason=str(exc),
            )
        return PassportResult(
            SC_PURCHASE_PASSPORT_LEVEL_RET,
            self.codec.encode(
                "SC_PurchasePassportLevelRet",
                {"Errorcode": ERROR_SUCCESS, "LevelNum": self.max_level},
            ),
        )

    def handle(
        self, request_id: int, profile: Mapping[str, Any], body: bytes = b""
    ) -> Optional[PassportResult]:
        if request_id == CS_REQUEST_PASSPORT_DATA:
            return self.query(profile)
        if request_id == CS_TAKE_PASSPORT_REWARD:
            return self.take_reward(profile, body)
        if request_id == CS_PURCHASE_PASSPORT_LEVEL:
            return self.purchase_level(profile, body)
        return None


@lru_cache(maxsize=1)
def default_layer() -> PassportContentLayer:
    return PassportContentLayer()


__all__ = [
    "CS_PURCHASE_PASSPORT_LEVEL",
    "CS_REQUEST_PASSPORT_DATA",
    "CS_TAKE_PASSPORT_REWARD",
    "PassportCatalogError",
    "PassportContentLayer",
    "PassportResult",
    "RESPONSE_IDS",
    "SC_PURCHASE_PASSPORT_LEVEL_RET",
    "SC_REQUEST_PASSPORT_DATA_RET",
    "SC_TAKE_PASSPORT_REWARD_RET",
    "default_layer",
]
