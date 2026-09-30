"""Deterministic summon state and protobuf responses.

The engine reads its tables from the configured content directory. Handlers
return copy-on-write changes, and time plus the counter-based RNG are
injectable for tests.
"""

from __future__ import annotations

import copy
import hashlib
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Tuple


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONTENT_DIR = ROOT / "generated" / "content"
CONTENT_TIMEZONE = timezone(timedelta(hours=8), name="Asia/Shanghai")

# generated/protocol/protocol_catalog.json
CS_GET_WISH_SOUL_SHARD = 1372
SC_GET_WISH_SOUL_SHARD_RET = 1373
CS_SET_WISH_SOUL_SHARD = 1374
SC_SET_WISH_SOUL_SHARD_RET = 1375
CS_DRAW_SOUL = 1376
SC_DRAW_SOUL_RET = 1377
CS_REQUEST_SOUL_CARD_POOL_DATAS = 1378
SC_REQUEST_SOUL_CARD_POOL_DATAS_RET = 1379
CS_REQUEST_SOUL_DRAW_CARD_RECORD = 1398
SC_REQUEST_SOUL_DRAW_CARD_RECORD_RET = 1399

RESPONSE_IDS = {
    CS_GET_WISH_SOUL_SHARD: SC_GET_WISH_SOUL_SHARD_RET,
    CS_SET_WISH_SOUL_SHARD: SC_SET_WISH_SOUL_SHARD_RET,
    CS_DRAW_SOUL: SC_DRAW_SOUL_RET,
    CS_REQUEST_SOUL_CARD_POOL_DATAS: SC_REQUEST_SOUL_CARD_POOL_DATAS_RET,
    CS_REQUEST_SOUL_DRAW_CARD_RECORD: SC_REQUEST_SOUL_DRAW_CARD_RECORD_RET,
}

# Msg.DrawCardType
DRAW_SINGLE = 0
DRAW_TEN = 1
DRAW_COUNTS = {DRAW_SINGLE: 1, DRAW_TEN: 10}

# Msg.ResourceType
RESOURCE_ITEM = 4
RESOURCE_GOSPEL = 7

# Msg.ErrorCode values defined from runtime assembly.
ERROR_SUCCESS = 0
ERROR_MINIDIAMOND_NOT_ENOUGH = 107
ERROR_ITEM_NOT_ENOUGH = 110
ERROR_CARD_POOL_NOT_OPEN = 192
ERROR_CARD_POOL_NOT_FOUND = 308
ERROR_CARD_POOL_USE_REACHED_MAX = 355
ERROR_MESSAGE_DATA_ILLEGAL = 32769
ERROR_CONFIG = 32772

STATE_KEY = "soul_gacha"
STATE_SCHEMA_VERSION = 1
SOUL_RNG_SEED_KEY = "soul_rng_seed"
SOUL_RNG_COUNTER_KEY = "soul_rng_counter"
# Kept as public aliases for callers that use this module's constants.  Their
# persisted values are Soul-specific and cannot collide with gacha.py's
# top-level ``rng_seed`` / ``rng_counter`` keys.
RNG_SEED_KEY = SOUL_RNG_SEED_KEY
RNG_COUNTER_KEY = SOUL_RNG_COUNTER_KEY

# SoulSummonView requests records in chunks of 10,000 and a_soul_global
# supplies the retention/cooldown values.
RECORD_PAGE_SIZE = 10_000

EXPECTED_COUNTS = {
    "a_soulsummon_main": 14,
    "a_soulsummon_group": 30,
    "a_soulsummon_strategy": 2,
    "a_soul_commonpool": 200,
    "a_soul_commonpool_up": 12,
    "a_soul_information": 72,
    "a_soul_global": 7,
}


class SoulCatalogError(ValueError):
    """Raised when the defined local tables are not internally closed."""


class ProtobufDecodeError(ValueError):
    """Raised when a request is malformed for the supported protobuf subset."""


def _as_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _nonnegative(value: Any, default: int = 0) -> int:
    return max(0, _as_int(value, default))


def _mapping(value: Any) -> Mapping[Any, Any]:
    return value if isinstance(value, Mapping) else {}


def _lookup(mapping: Mapping[Any, Any], key: int, default: Any = None) -> Any:
    if key in mapping:
        return mapping[key]
    return mapping.get(str(key), default)


def _store_indexed(mapping: Dict[Any, Any], key: int, value: Any) -> None:
    mapping.pop(key, None)
    mapping[str(key)] = value


def _timestamp(value: Optional[Any]) -> int:
    if value is None:
        return int(time.time())
    if isinstance(value, datetime):
        current = value
        if current.tzinfo is None:
            current = current.replace(tzinfo=CONTENT_TIMEZONE)
        return int(current.timestamp())
    return int(value)


def _parse_table_datetime(value: Any, context: str) -> int:
    if not isinstance(value, str) or not value:
        raise SoulCatalogError(f"{context}: expected a non-empty datetime")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d %H:%M")
    except ValueError as exc:
        raise SoulCatalogError(f"{context}: invalid datetime {value!r}") from exc
    return int(parsed.replace(tzinfo=CONTENT_TIMEZONE).timestamp())


def encode_varint(value: int) -> bytes:
    if value < 0:
        value &= (1 << 64) - 1
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


def parse_fields(data: bytes) -> Dict[int, List[Tuple[int, object]]]:
    fields: Dict[int, List[Tuple[int, object]]] = {}
    for number, wire_type, value in iter_wire_fields(data):
        fields.setdefault(number, []).append((wire_type, value))
    return fields


def first_int(
    fields: Mapping[int, Sequence[Tuple[int, object]]],
    number: int,
    default: int = 0,
) -> int:
    values = fields.get(number)
    if not values:
        return int(default)
    wire_type, value = values[0]
    if wire_type != 0 or not isinstance(value, int):
        raise ProtobufDecodeError(f"field {number} must be a varint")
    return int(value)


def repeated_uint32(
    fields: Mapping[int, Sequence[Tuple[int, object]]], number: int
) -> Tuple[int, ...]:
    result: List[int] = []
    for wire_type, raw in fields.get(number, ()):
        if wire_type == 0 and isinstance(raw, int):
            result.append(raw)
            continue
        if wire_type != 2 or not isinstance(raw, bytes):
            raise ProtobufDecodeError(f"field {number} must contain uint32 values")
        offset = 0
        while offset < len(raw):
            value, offset = read_varint(raw, offset)
            result.append(value)
    if any(value < 0 or value > 0xFFFFFFFF for value in result):
        raise ProtobufDecodeError(f"field {number} contains a non-uint32 value")
    return tuple(result)


@dataclass(frozen=True)
class Reward:
    res_type: int
    item_id: int
    count: int


@dataclass(frozen=True)
class SoulInfo:
    soul_id: int
    name: str
    quality: int
    max_awake: int
    awaken_rewards: Tuple[Reward, ...]
    overflow_rewards: Tuple[Reward, ...]


@dataclass(frozen=True)
class PoolMember:
    config_id: int
    pool_group: int
    soul_id: int
    rarity: int
    weight: int
    is_up: bool


@dataclass(frozen=True)
class SoulGroup:
    group_id: int
    up_pool_group: int
    up_weight: int
    common_pool_group: int
    common_weight: int
    locked_soul_ids: Tuple[int, ...]
    rarity: int
    up_members: Tuple[PoolMember, ...]
    common_members: Tuple[PoolMember, ...]


@dataclass(frozen=True)
class RateRule:
    key: int
    rarity: int
    base_rate: int
    rate_add_value: int
    rate_max: int
    soft_count: int
    hard_count: int
    up_guarantee_losses: int


@dataclass(frozen=True)
class SoulStrategyConfig:
    strategy_id: int
    rules: Tuple[RateRule, RateRule]

    def for_rarity(self, rarity: int) -> RateRule:
        for rule in self.rules:
            if rule.rarity == rarity:
                return rule
        raise SoulCatalogError(
            f"strategy {self.strategy_id} has no rarity {rarity} rule"
        )


@dataclass(frozen=True)
class SoulPool:
    pool_id: int
    sort: int
    pool_type: int
    open_type: int
    open_condition: str
    highest_num: int
    start_timestamp: int
    end_timestamp: int
    pool_text: int
    strategy_id: int
    groups_by_key: Tuple[Tuple[int, int], ...]
    wish_key: int
    wish_number: int
    wish_counts: int
    wish_cooldown: int
    wish_pool_group: int
    wish_candidates: Tuple[PoolMember, ...]

    def group_id(self, key: int) -> int:
        for candidate_key, group_id in self.groups_by_key:
            if candidate_key == key:
                return group_id
        raise SoulCatalogError(f"pool {self.pool_id} has no group key {key}")


@dataclass(frozen=True)
class PoolDirectoryEntry:
    pool_id: int
    pool_type: int
    sort: int
    strategy_id: int
    start_timestamp: int
    end_timestamp: int
    is_valid: bool
    validity_reason: str
    group_ids_by_key: Tuple[Tuple[int, int], ...]
    wish_enabled: bool
    wish_number: int
    wish_counts: int


@dataclass(frozen=True)
class DrawOutcome:
    soul_id: int
    rarity: int
    is_new: bool
    is_up: bool = False
    is_wish: bool = False
    hard_pity_5: bool = False
    hard_pity_4: bool = False
    conversions: Tuple[Reward, ...] = ()


@dataclass(frozen=True)
class SoulGachaResult:
    """A protobuf response and a pure, top-level profile change set."""

    response_id: int
    body: bytes
    changes: Dict[str, Any] = field(default_factory=dict)
    error_code: int = ERROR_SUCCESS
    request_id: int = 0
    pool_id: int = 0
    draw_type: int = DRAW_SINGLE
    draws: Tuple[DrawOutcome, ...] = ()
    ticket_used: int = 0
    minidiamond_used: int = 0
    diamond_used: int = 0
    supported: bool = True
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.error_code == ERROR_SUCCESS


class CounterRng:
    """Counter-based RNG whose complete replay state fits in two integers."""

    def __init__(self, seed: int, counter: int = 0) -> None:
        self.seed = int(seed)
        self.counter = max(0, int(counter))

    def randbelow(self, upper: int) -> int:
        if upper <= 0:
            raise ValueError("upper must be positive")
        payload = f"soul-gacha:{self.seed}:{self.counter}".encode("ascii")
        value = int.from_bytes(hashlib.sha256(payload).digest(), "big")
        self.counter += 1
        return value % upper

    def chance(self, rate: int, scale: int = 10_000) -> bool:
        if rate <= 0:
            return False
        if rate >= scale:
            return True
        return self.randbelow(scale) < rate


def _parse_json_list(value: Any, context: str) -> List[Any]:
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError) as exc:
        raise SoulCatalogError(f"{context}: invalid JSON") from exc
    if not isinstance(parsed, list):
        raise SoulCatalogError(f"{context}: expected a JSON list")
    return parsed


def _parse_rewards(value: Any, context: str) -> Tuple[Reward, ...]:
    if value in (None, ""):
        return ()
    rows = _parse_json_list(value, context)
    rewards: List[Reward] = []
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise SoulCatalogError(f"{context}[{index}]: expected an object")
        reward = Reward(
            res_type=_as_int(row.get("type"), -1),
            item_id=_as_int(row.get("id"), -1),
            count=_as_int(row.get("num"), -1),
        )
        if reward.res_type < 0 or reward.item_id <= 0 or reward.count <= 0:
            raise SoulCatalogError(f"{context}[{index}]: invalid reward")
        rewards.append(reward)
    return tuple(rewards)


class SoulGachaCatalog:
    """Validated view of every Soul pool in the defined runtime snapshot."""

    def __init__(self, content_dir: Path = DEFAULT_CONTENT_DIR) -> None:
        self.content_dir = Path(content_dir).resolve()
        documents = {
            name: self._load_table(name)
            for name in (
                *EXPECTED_COUNTS,
                "a_global",
                "a_item",
                "a_gospel",
            )
        }
        self.documents = documents

        item_ids = frozenset(_as_int(row["id"]) for row in documents["a_item"]["records"])
        gospel_ids = frozenset(
            _as_int(row["id"]) for row in documents["a_gospel"]["records"]
        )

        self.soul_infos: Dict[int, SoulInfo] = {}
        for row in documents["a_soul_information"]["records"]:
            soul_id = _as_int(row.get("id"), -1)
            awaken = _parse_rewards(
                row.get("awaken_item"), f"a_soul_information[{soul_id}].awaken_item"
            )
            overflow = _parse_rewards(
                row.get("repitem_over"), f"a_soul_information[{soul_id}].repitem_over"
            )
            for reward in awaken + overflow:
                if reward.res_type != RESOURCE_ITEM:
                    raise SoulCatalogError(
                        f"soul {soul_id}: local reward type {reward.res_type} is unsupported"
                    )
                if reward.item_id not in item_ids:
                    raise SoulCatalogError(
                        f"soul {soul_id}: reward item {reward.item_id} is missing"
                    )
            if soul_id in self.soul_infos or soul_id not in gospel_ids:
                raise SoulCatalogError(
                    f"a_soul_information id {soul_id} is duplicate or absent from a_gospel"
                )
            self.soul_infos[soul_id] = SoulInfo(
                soul_id=soul_id,
                name=str(row.get("soul_name") or ""),
                quality=_as_int(row.get("soul_quality"), -1),
                max_awake=_nonnegative(row.get("max_awake")),
                awaken_rewards=awaken,
                overflow_rewards=overflow,
            )

        self.common_by_pool = self._members_by_pool(
            documents["a_soul_commonpool"]["records"], is_up=False
        )
        self.up_by_pool = self._members_by_pool(
            documents["a_soul_commonpool_up"]["records"], is_up=True
        )

        self.groups: Dict[int, SoulGroup] = {}
        for row in documents["a_soulsummon_group"]["records"]:
            group_id = _as_int(row.get("id"), -1)
            common_pool = _as_int(row.get("group_common"), -1)
            up_pool = _nonnegative(row.get("group_up"))
            common = tuple(self.common_by_pool.get(common_pool, ()))
            up = tuple(self.up_by_pool.get(up_pool, ())) if up_pool else ()
            if not common:
                raise SoulCatalogError(
                    f"a_soulsummon_group {group_id}: missing common pool {common_pool}"
                )
            if up_pool and not up:
                raise SoulCatalogError(
                    f"a_soulsummon_group {group_id}: missing UP pool {up_pool}"
                )
            lock_rows = _parse_json_list(
                row.get("lock_id") or "[]",
                f"a_soulsummon_group[{group_id}].lock_id",
            )
            if any(not isinstance(value, int) for value in lock_rows):
                raise SoulCatalogError(f"group {group_id}: lock_id must contain integers")
            locked = tuple(int(value) for value in lock_rows)
            if len(locked) != len(set(locked)):
                raise SoulCatalogError(f"group {group_id}: duplicate lock_id")
            unknown_locked = set(locked) - set(self.soul_infos)
            if unknown_locked:
                raise SoulCatalogError(
                    f"group {group_id}: unknown locked souls {sorted(unknown_locked)}"
                )
            effective_common = tuple(
                member for member in common if member.soul_id not in set(locked)
            )
            if not effective_common:
                raise SoulCatalogError(f"group {group_id}: common pool is empty after locks")
            rarities = {member.rarity for member in effective_common + up}
            if len(rarities) != 1:
                raise SoulCatalogError(
                    f"group {group_id}: mixed member rarities {sorted(rarities)}"
                )
            if up and set(locked) != {member.soul_id for member in up}:
                raise SoulCatalogError(
                    f"group {group_id}: lock_id must exactly cover local UP members"
                )
            up_weight = _nonnegative(row.get("up_weight"))
            common_weight = _nonnegative(row.get("common_weight"))
            if common_weight <= 0 or (up and up_weight <= 0):
                raise SoulCatalogError(f"group {group_id}: non-positive branch weight")
            if not up and up_weight != 0:
                raise SoulCatalogError(f"group {group_id}: UP weight without UP members")
            if group_id in self.groups:
                raise SoulCatalogError(f"duplicate Soul group id {group_id}")
            self.groups[group_id] = SoulGroup(
                group_id=group_id,
                up_pool_group=up_pool,
                up_weight=up_weight,
                common_pool_group=common_pool,
                common_weight=common_weight,
                locked_soul_ids=locked,
                rarity=next(iter(rarities)),
                up_members=up,
                common_members=effective_common,
            )

        strategy_rows = {
            _as_int(row["id"]): row
            for row in documents["a_soulsummon_strategy"]["records"]
        }
        self._strategy_rows = strategy_rows
        self.strategies: Dict[int, SoulStrategyConfig] = {}

        self.pools: Dict[int, SoulPool] = {}
        referenced_groups: set[int] = set()
        for row in documents["a_soulsummon_main"]["records"]:
            pool_id = _as_int(row.get("id"), -1)
            strategy_id = _as_int(row.get("strategy_group"), -1)
            strategy_row = strategy_rows.get(strategy_id)
            if strategy_row is None:
                raise SoulCatalogError(
                    f"pool {pool_id}: missing strategy {strategy_id}"
                )
            group_rows = _parse_json_list(
                row.get("group"), f"a_soulsummon_main[{pool_id}].group"
            )
            by_key: Dict[int, int] = {}
            for index, entry in enumerate(group_rows):
                if not isinstance(entry, Mapping):
                    raise SoulCatalogError(f"pool {pool_id}.group[{index}] is not an object")
                key = _as_int(entry.get("key"), -1)
                group_id = _as_int(entry.get("group"), -1)
                if key in by_key:
                    raise SoulCatalogError(f"pool {pool_id}: duplicate group key {key}")
                if group_id not in self.groups:
                    raise SoulCatalogError(f"pool {pool_id}: unknown group {group_id}")
                by_key[key] = group_id
                referenced_groups.add(group_id)

            strategy = self._strategy(strategy_id, strategy_row, by_key)
            previous = self.strategies.get(strategy_id)
            if previous is not None and previous != strategy:
                raise SoulCatalogError(f"strategy {strategy_id}: inconsistent rarity mapping")
            self.strategies[strategy_id] = strategy
            expected_keys = {0, *(rule.key for rule in strategy.rules)}
            if set(by_key) != expected_keys:
                raise SoulCatalogError(
                    f"pool {pool_id}: group keys {sorted(by_key)} != {sorted(expected_keys)}"
                )
            if self.groups[by_key[0]].rarity != 3:
                raise SoulCatalogError(f"pool {pool_id}: key 0 is not rarity 3")

            wish_pool = _nonnegative(row.get("wish_pool"))
            wish_candidates = tuple(self.common_by_pool.get(wish_pool, ()))
            if wish_pool and not wish_candidates:
                raise SoulCatalogError(
                    f"pool {pool_id}: missing wish pool group {wish_pool}"
                )
            pool_type = _as_int(row.get("summonpool_type"), -1)
            wish_key = _as_int(row.get("wish_key"), -1)
            wish_number = _nonnegative(row.get("wish_number"))
            if pool_type == 2 and wish_number:
                if wish_key not in by_key:
                    raise SoulCatalogError(f"pool {pool_id}: unknown wish_key {wish_key}")
                wish_rarity = self.groups[by_key[wish_key]].rarity
                if {member.rarity for member in wish_candidates} != {wish_rarity}:
                    raise SoulCatalogError(f"pool {pool_id}: wish pool rarity mismatch")
                if self.groups[by_key[wish_key]].common_pool_group != wish_pool:
                    raise SoulCatalogError(
                        f"pool {pool_id}: wish pool is not the wish-key common pool"
                    )

            start = _parse_table_datetime(
                row.get("summonpool_starttime"), f"pool {pool_id}.start"
            )
            end = _parse_table_datetime(
                row.get("summonpool_endtime"), f"pool {pool_id}.end"
            )
            if end <= start:
                raise SoulCatalogError(f"pool {pool_id}: non-positive availability window")
            if pool_id in self.pools:
                raise SoulCatalogError(f"duplicate Soul pool id {pool_id}")
            self.pools[pool_id] = SoulPool(
                pool_id=pool_id,
                sort=_as_int(row.get("summonpool_sort")),
                pool_type=pool_type,
                open_type=_as_int(row.get("open_type"), -1),
                open_condition=str(row.get("open_condition") or ""),
                highest_num=_nonnegative(row.get("highest_num")),
                start_timestamp=start,
                end_timestamp=end,
                pool_text=_as_int(row.get("pool_text")),
                strategy_id=strategy_id,
                groups_by_key=tuple(sorted(by_key.items())),
                wish_key=wish_key,
                wish_number=wish_number,
                wish_counts=_nonnegative(row.get("wish_counts")),
                wish_cooldown=_nonnegative(row.get("cooldown")),
                wish_pool_group=wish_pool,
                wish_candidates=wish_candidates,
            )

        if referenced_groups != set(self.groups):
            missing = sorted(set(self.groups) - referenced_groups)
            extra = sorted(referenced_groups - set(self.groups))
            raise SoulCatalogError(
                f"main/group closure mismatch: unreferenced={missing}, unknown={extra}"
            )
        if set(self.strategies) != set(strategy_rows):
            raise SoulCatalogError("not every Soul strategy is referenced")

        soul_global = {
            _as_int(row["id"]): row for row in documents["a_soul_global"]["records"]
        }
        general_global = {
            _as_int(row["id"]): row for row in documents["a_global"]["records"]
        }
        self.ticket_item_id = _as_int(soul_global[100000]["global_value_int"], -1)
        if self.ticket_item_id not in item_ids:
            raise SoulCatalogError("a_soul_global[100000] ticket item is missing")
        one_cost = _parse_rewards(
            soul_global[100002].get("global_value_string"),
            "a_soul_global[100002]",
        )
        ten_cost = _parse_rewards(
            soul_global[100003].get("global_value_string"),
            "a_soul_global[100003]",
        )
        if (
            len(one_cost) != 1
            or len(ten_cost) != 1
            or one_cost[0].res_type != RESOURCE_ITEM
            or ten_cost[0].res_type != RESOURCE_ITEM
            or one_cost[0].item_id != self.ticket_item_id
            or ten_cost[0].item_id != self.ticket_item_id
        ):
            raise SoulCatalogError("Soul single/ten costs do not reference the ticket item")
        self.ticket_costs = {
            DRAW_SINGLE: one_cost[0].count,
            DRAW_TEN: ten_cost[0].count,
        }
        self.minidiamond_per_ticket = _nonnegative(
            soul_global[100001].get("global_value_int")
        )
        self.diamond_exchange_rate = _nonnegative(
            general_global.get(9990005, {}).get("int_value")
        )
        self.history_retention_days = _nonnegative(
            soul_global[100005].get("global_value_int")
        )
        self.history_request_cooldown = _nonnegative(
            soul_global[100006].get("global_value_int")
        )
        if self.minidiamond_per_ticket <= 0 or self.diamond_exchange_rate <= 0:
            raise SoulCatalogError("Soul currency conversion globals must be positive")
        if self.history_retention_days <= 0 or self.history_request_cooldown <= 0:
            raise SoulCatalogError("Soul history globals must be positive")

        self.pool_ids = tuple(sorted(self.pools))
        self.group_ids = tuple(sorted(self.groups))
        self.strategy_ids = tuple(sorted(self.strategies))
        self.information_ids = tuple(sorted(self.soul_infos))
        referenced_souls = {
            member.soul_id
            for members in (*self.common_by_pool.values(), *self.up_by_pool.values())
            for member in members
        }
        self.unpooled_information_ids = tuple(sorted(set(self.soul_infos) - referenced_souls))

    def _load_table(self, name: str) -> Dict[str, Any]:
        path = self.content_dir / f"{name}.json"
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise SoulCatalogError(f"cannot load {path}") from exc
        if not isinstance(document, dict) or document.get("table") != name:
            raise SoulCatalogError(f"{path}: wrong table wrapper")
        records = document.get("records")
        ids = document.get("ids")
        if not isinstance(records, list) or not isinstance(ids, list):
            raise SoulCatalogError(f"{path}: records/ids are missing")
        if document.get("count") != len(records) or len(ids) != len(records):
            raise SoulCatalogError(f"{path}: inconsistent record count")
        if [row.get("id") for row in records if isinstance(row, Mapping)] != ids:
            raise SoulCatalogError(f"{path}: id list does not match records")
        if len(ids) != len(set(ids)):
            raise SoulCatalogError(f"{path}: duplicate IDs")
        expected = EXPECTED_COUNTS.get(name)
        if expected is not None:
            # Character compatibility overlays may append local-equipment
            # information without adding that experimental character to any
            # defined 1.0 summon pool. Pool membership tables remain exact;
            # extra information rows are surfaced as unpooled_information_ids.
            valid_count = (
                len(records) >= expected
                if name == "a_soul_information"
                else len(records) == expected
            )
            if not valid_count:
                qualifier = "at least" if name == "a_soul_information" else "exactly"
                raise SoulCatalogError(
                    f"{path}: expected {qualifier} {expected} local snapshot "
                    f"records, got {len(records)}"
                )
        return document

    def _members_by_pool(
        self, rows: Sequence[Mapping[str, Any]], *, is_up: bool
    ) -> Dict[int, Tuple[PoolMember, ...]]:
        result: Dict[int, List[PoolMember]] = {}
        seen_ids: set[int] = set()
        for row in rows:
            config_id = _as_int(row.get("id"), -1)
            pool_group = _as_int(row.get("pool_group"), -1)
            soul_id = _as_int(row.get("soul_id"), -1)
            rarity = _as_int(row.get("soul_rare"), -1)
            weight = _as_int(row.get("weight"), -1)
            unlock_time = _as_int(row.get("unlock_time"), -1)
            if config_id in seen_ids:
                raise SoulCatalogError(f"duplicate Soul member row {config_id}")
            seen_ids.add(config_id)
            info = self.soul_infos.get(soul_id)
            if info is None:
                raise SoulCatalogError(f"member {config_id}: unknown soul {soul_id}")
            if info.quality != rarity:
                raise SoulCatalogError(
                    f"member {config_id}: rarity {rarity} != information {info.quality}"
                )
            if weight <= 0:
                raise SoulCatalogError(f"member {config_id}: non-positive weight")
            # All 212 rows in this runtime snapshot are zero.  A future nonzero
            # value needs server semantics before it can be interpreted.
            if unlock_time != 0:
                raise SoulCatalogError(
                    f"member {config_id}: unsupported nonzero unlock_time {unlock_time}"
                )
            result.setdefault(pool_group, []).append(
                PoolMember(config_id, pool_group, soul_id, rarity, weight, is_up)
            )
        return {
            pool_group: tuple(sorted(members, key=lambda member: member.config_id))
            for pool_group, members in result.items()
        }

    def _strategy(
        self,
        strategy_id: int,
        row: Mapping[str, Any],
        groups_by_key: Mapping[int, int],
    ) -> SoulStrategyConfig:
        rules: List[RateRule] = []
        for suffix in (1, 2):
            key = _as_int(row.get(f"key_group{suffix}"), -1)
            group_id = groups_by_key.get(key)
            if group_id is None:
                raise SoulCatalogError(
                    f"strategy {strategy_id}: key {key} is absent from pool groups"
                )
            rarity = self.groups[group_id].rarity
            if rarity not in {4, 5}:
                raise SoulCatalogError(
                    f"strategy {strategy_id}: key {key} maps to rarity {rarity}"
                )
            rule = RateRule(
                key=key,
                rarity=rarity,
                base_rate=_as_int(row.get(f"baserate_group{suffix}"), -1),
                rate_add_value=_as_int(
                    row.get(f"rate_addvalue_group{suffix}"), -1
                ),
                rate_max=_as_int(row.get(f"rate_max_group{suffix}"), -1),
                soft_count=_as_int(row.get(f"count_group{suffix}"), -1),
                hard_count=_as_int(row.get(f"rate_floor_group{suffix}"), -1),
                up_guarantee_losses=_nonnegative(
                    row.get(f"up_count_group{suffix}")
                ),
            )
            if not (
                0 <= rule.base_rate <= rule.rate_max <= 10_000
                and rule.rate_add_value >= 0
                and rule.soft_count >= 0
                and rule.hard_count > 0
            ):
                raise SoulCatalogError(f"strategy {strategy_id}: invalid rate rule")
            rules.append(rule)
        if {rule.rarity for rule in rules} != {4, 5}:
            raise SoulCatalogError(f"strategy {strategy_id}: missing R4/R5 rule")
        return SoulStrategyConfig(strategy_id, tuple(rules))  # type: ignore[arg-type]

    def validity(
        self,
        pool_id: int,
        at: Optional[Any] = None,
        *,
        ignore_date_window: bool = False,
    ) -> Tuple[bool, str]:
        pool = self.pools.get(int(pool_id))
        if pool is None:
            return False, "unknown_pool"
        if pool.open_type != 0 or pool.open_condition:
            return False, "unsupported_open_condition"
        if ignore_date_window:
            return True, "offline_all_pools"
        current = _timestamp(at)
        if current < pool.start_timestamp:
            return False, "before_start"
        if current >= pool.end_timestamp:
            return False, "ended"
        return True, "active"

    def directory(
        self,
        at: Optional[Any] = None,
        *,
        ignore_date_window: bool = False,
    ) -> Tuple[PoolDirectoryEntry, ...]:
        rows: List[PoolDirectoryEntry] = []
        for pool_id in self.pool_ids:
            pool = self.pools[pool_id]
            valid, reason = self.validity(
                pool_id, at, ignore_date_window=ignore_date_window
            )
            rows.append(
                PoolDirectoryEntry(
                    pool_id=pool.pool_id,
                    pool_type=pool.pool_type,
                    sort=pool.sort,
                    strategy_id=pool.strategy_id,
                    start_timestamp=pool.start_timestamp,
                    end_timestamp=pool.end_timestamp,
                    is_valid=valid,
                    validity_reason=reason,
                    group_ids_by_key=pool.groups_by_key,
                    wish_enabled=pool.pool_type == 2 and pool.wish_number > 0,
                    wish_number=pool.wish_number,
                    wish_counts=pool.wish_counts,
                )
            )
        return tuple(rows)

    def active_pool_ids(
        self,
        at: Optional[Any] = None,
        *,
        ignore_date_window: bool = False,
    ) -> Tuple[int, ...]:
        return tuple(
            row.pool_id
            for row in self.directory(at, ignore_date_window=ignore_date_window)
            if row.is_valid
        )

    def describe_pool(
        self,
        pool_id: int,
        at: Optional[Any] = None,
        *,
        ignore_date_window: bool = False,
    ) -> Dict[str, Any]:
        pool = self.pools.get(int(pool_id))
        if pool is None:
            raise KeyError(pool_id)
        valid, reason = self.validity(
            pool.pool_id, at, ignore_date_window=ignore_date_window
        )
        groups = []
        for key, group_id in pool.groups_by_key:
            group = self.groups[group_id]
            groups.append(
                {
                    "key": key,
                    "rarity": group.rarity,
                    "group_id": group.group_id,
                    "up_weight": group.up_weight,
                    "common_weight": group.common_weight,
                    "up_members": tuple(
                        (member.soul_id, member.weight) for member in group.up_members
                    ),
                    "common_members": tuple(
                        (member.soul_id, member.weight)
                        for member in group.common_members
                    ),
                }
            )
        return {
            "pool_id": pool.pool_id,
            "pool_type": pool.pool_type,
            "strategy_id": pool.strategy_id,
            "start_timestamp": pool.start_timestamp,
            "end_timestamp": pool.end_timestamp,
            "is_valid": valid,
            "validity_reason": reason,
            "groups": tuple(groups),
            "wish_candidates": tuple(member.soul_id for member in pool.wish_candidates),
            "wish_number": pool.wish_number,
            "wish_counts": pool.wish_counts,
            "wish_cooldown": pool.wish_cooldown,
        }


def make_reward_item(reward: Reward) -> bytes:
    # Msg.RewardItem: ResType=1, ItemId=2, Count=3.
    return b"".join(
        (
            field_varint(1, reward.res_type),
            field_varint(2, reward.item_id),
            field_varint(3, reward.count),
        )
    )


def make_drawn_stuff(outcome: DrawOutcome) -> bytes:
    # Msg.DrawnStuff: ResType=1, ItemId=2, Count=3,
    # ConvertItemList=4, IsNew=5.
    return b"".join(
        (
            field_varint(1, RESOURCE_GOSPEL),
            field_varint(2, outcome.soul_id),
            field_varint(3, 1),
            b"".join(
                field_message(4, make_reward_item(reward))
                for reward in outcome.conversions
            ),
            field_varint(5, 1 if outcome.is_new else 0),
        )
    )


def make_soul_card_pool(pool_id: int, state: Mapping[str, Any]) -> bytes:
    # Msg.SoulCardPool: CardPoolId=1, DrawNum=2, TenDrawNum=3.
    return b"".join(
        (
            field_varint(1, pool_id),
            field_varint(2, _nonnegative(state.get("draw_num"))),
            field_varint(3, _nonnegative(state.get("ten_draw_num"))),
        )
    )


def make_soul_strategy(strategy_id: int, state: Mapping[str, Any]) -> bytes:
    # Msg.SoulStrategy fields 1..7.
    names = (
        "star5_fail_draw_num",
        "star4_fail_draw_num",
        "star5_prob",
        "star4_prob",
        "star5_not_up_num",
        "star4_not_up_num",
    )
    payload = bytearray(field_varint(1, strategy_id))
    for number, name in enumerate(names, start=2):
        payload.extend(field_varint(number, _nonnegative(state.get(name))))
    return bytes(payload)


def make_wish_soul_shard(pool_id: int, state: Mapping[str, Any]) -> bytes:
    # Msg.WishSoulShard: Id=1, SoulShardIds=2, SetTimestamp=3,
    # TakeEffectCount=4.
    soul_ids = state.get("soul_ids")
    values = soul_ids if isinstance(soul_ids, (list, tuple)) else ()
    return b"".join(
        (
            field_varint(1, pool_id),
            field_packed_uint32(2, (_nonnegative(value) for value in values)),
            field_varint(3, _nonnegative(state.get("set_timestamp"))),
            field_varint(4, _nonnegative(state.get("take_effect_count"))),
        )
    )


def make_draw_card_record(row: Mapping[str, Any]) -> bytes:
    # Msg.DrawCardRecordData: GroupId=1, GospelId=2, Timestamp=3.
    return b"".join(
        (
            field_varint(1, _nonnegative(row.get("pool_id"))),
            field_varint(2, _nonnegative(row.get("soul_id"))),
            field_varint(3, _nonnegative(row.get("timestamp"))),
        )
    )


class SoulGachaEngine:
    """Configuration-backed Soul summon state machine."""

    def __init__(
        self,
        content_dir: Path = DEFAULT_CONTENT_DIR,
        *,
        clock: Optional[Callable[[], Any]] = None,
        seed: Optional[int] = None,
        include_inactive_pools: bool = False,
        offline_all_pools: bool = False,
        rng_factory: Callable[[int, int], CounterRng] = CounterRng,
    ) -> None:
        self.catalog = SoulGachaCatalog(content_dir)
        self.clock = clock or time.time
        self.seed = seed
        self.include_inactive_pools = bool(include_inactive_pools)
        self.offline_all_pools = bool(offline_all_pools)
        self.rng_factory = rng_factory

    def _now(self, at: Optional[Any]) -> int:
        return _timestamp(self.clock() if at is None else at)

    def _validity(self, pool_id: int, at: Any) -> Tuple[bool, str]:
        return self.catalog.validity(
            pool_id,
            at,
            ignore_date_window=self.offline_all_pools,
        )

    def directory(
        self, *, at: Optional[Any] = None
    ) -> Tuple[PoolDirectoryEntry, ...]:
        """Return all local pools with validity under this engine's policy."""

        return self.catalog.directory(
            self._now(at),
            ignore_date_window=self.offline_all_pools,
        )

    def describe_pool(
        self, pool_id: int, *, at: Optional[Any] = None
    ) -> Dict[str, Any]:
        return self.catalog.describe_pool(
            pool_id,
            self._now(at),
            ignore_date_window=self.offline_all_pools,
        )

    @staticmethod
    def rate_for_failures(rule: RateRule, failures: int) -> int:
        """Probability for the next draw on the table's 10,000 scale."""

        failures = max(0, int(failures))
        if failures >= rule.hard_count - 1:
            return 10_000
        steps = max(0, failures - rule.soft_count)
        return min(rule.rate_max, rule.base_rate + steps * rule.rate_add_value)

    @staticmethod
    def apply_changes(
        profile: Optional[Mapping[str, Any]], changes: Mapping[str, Any]
    ) -> Dict[str, Any]:
        result = copy.deepcopy(dict(profile or {}))
        result.update(copy.deepcopy(dict(changes)))
        return result

    def _state(self, profile: Mapping[str, Any]) -> Dict[str, Any]:
        state = copy.deepcopy(dict(_mapping(profile.get(STATE_KEY))))
        state["schema_version"] = STATE_SCHEMA_VERSION
        for key in ("pools", "strategies", "wishes", "owned"):
            state[key] = copy.deepcopy(dict(_mapping(state.get(key))))
        history = state.get("history")
        state["history"] = copy.deepcopy(history) if isinstance(history, list) else []
        daily = state.get("daily")
        state["daily"] = copy.deepcopy(dict(daily)) if isinstance(daily, Mapping) else {}
        state[RNG_COUNTER_KEY] = _nonnegative(state.get(RNG_COUNTER_KEY))
        return state

    def _pool_state(self, state: Mapping[str, Any], pool_id: int) -> Dict[str, int]:
        source = _mapping(_lookup(_mapping(state.get("pools")), pool_id, {}))
        return {
            "draw_num": _nonnegative(source.get("draw_num")),
            "ten_draw_num": _nonnegative(source.get("ten_draw_num")),
        }

    def _strategy_state(
        self, state: Mapping[str, Any], strategy_id: int
    ) -> Dict[str, int]:
        source = _mapping(
            _lookup(_mapping(state.get("strategies")), strategy_id, {})
        )
        result = {
            "star5_fail_draw_num": _nonnegative(source.get("star5_fail_draw_num")),
            "star4_fail_draw_num": _nonnegative(source.get("star4_fail_draw_num")),
            "star5_not_up_num": _nonnegative(source.get("star5_not_up_num")),
            "star4_not_up_num": _nonnegative(source.get("star4_not_up_num")),
        }
        config = self.catalog.strategies[strategy_id]
        result["star5_prob"] = self.rate_for_failures(
            config.for_rarity(5), result["star5_fail_draw_num"]
        )
        result["star4_prob"] = self.rate_for_failures(
            config.for_rarity(4), result["star4_fail_draw_num"]
        )
        return result

    @staticmethod
    def _inventory_count(inventory: Mapping[Any, Any], item_id: int) -> int:
        value = _lookup(inventory, item_id, 0)
        if isinstance(value, Mapping):
            return _nonnegative(value.get("count"))
        return _nonnegative(value)

    @staticmethod
    def _set_inventory_count(
        inventory: Dict[Any, Any], item_id: int, count: int
    ) -> None:
        existing = _lookup(inventory, item_id, None)
        if isinstance(existing, Mapping):
            updated = copy.deepcopy(dict(existing))
            updated["count"] = max(0, int(count))
            _store_indexed(inventory, item_id, updated)
        else:
            _store_indexed(inventory, item_id, max(0, int(count)))

    @staticmethod
    def _weighted_member(
        members: Sequence[PoolMember], rng: CounterRng
    ) -> PoolMember:
        if not members:
            raise SoulCatalogError("weighted Soul member pool is empty")
        if len(members) == 1:
            return members[0]
        total = sum(member.weight for member in members)
        if total <= 0:
            raise SoulCatalogError("weighted Soul member pool has no positive weight")
        ticket = rng.randbelow(total)
        cursor = 0
        for member in members:
            cursor += member.weight
            if ticket < cursor:
                return member
        raise AssertionError("weighted Soul selection fell through")

    def _roll_rarity(
        self,
        strategy: SoulStrategyConfig,
        strategy_state: Mapping[str, int],
        rng: CounterRng,
    ) -> Tuple[int, bool, bool]:
        rate5 = self.rate_for_failures(
            strategy.for_rarity(5), strategy_state["star5_fail_draw_num"]
        )
        rate4 = self.rate_for_failures(
            strategy.for_rarity(4), strategy_state["star4_fail_draw_num"]
        )
        hard5 = rate5 >= 10_000
        hard4 = rate4 >= 10_000
        if hard5:
            return 5, True, hard4
        roll = rng.randbelow(10_000)
        if roll < rate5:
            return 5, False, hard4
        if hard4 or roll < min(10_000, rate5 + rate4):
            return 4, False, hard4
        return 3, False, False

    def _wish_state(
        self, state: Mapping[str, Any], pool_id: int
    ) -> Optional[Dict[str, Any]]:
        source = _lookup(_mapping(state.get("wishes")), pool_id, None)
        if not isinstance(source, Mapping):
            return None
        values = source.get("soul_ids")
        soul_ids = [
            _nonnegative(value) for value in values
        ] if isinstance(values, (list, tuple)) else []
        return {
            "soul_ids": soul_ids,
            "set_timestamp": _nonnegative(source.get("set_timestamp")),
            "take_effect_count": _nonnegative(source.get("take_effect_count")),
        }

    def _select_member(
        self,
        pool: SoulPool,
        rarity: int,
        strategy_state: Dict[str, int],
        wish: Optional[Dict[str, Any]],
        rng: CounterRng,
    ) -> Tuple[PoolMember, bool, bool]:
        strategy = self.catalog.strategies[pool.strategy_id]
        rule = strategy.for_rarity(rarity) if rarity in {4, 5} else None
        key = rule.key if rule is not None else 0
        group = self.catalog.groups[pool.group_id(key)]

        if (
            wish is not None
            and key == pool.wish_key
            and pool.pool_type == 2
            and wish["take_effect_count"] < pool.wish_counts
            and len(wish["soul_ids"]) == pool.wish_number
        ):
            selected_ids = set(wish["soul_ids"])
            candidates = tuple(
                member for member in pool.wish_candidates if member.soul_id in selected_ids
            )
            if len(candidates) == pool.wish_number:
                selected = self._weighted_member(candidates, rng)
                wish["take_effect_count"] += 1
                return selected, False, True

        if not group.up_members:
            return self._weighted_member(group.common_members, rng), False, False

        assert rule is not None
        not_up_key = (
            "star5_not_up_num" if rarity == 5 else "star4_not_up_num"
        )
        force_up = (
            rule.up_guarantee_losses > 0
            and strategy_state[not_up_key] >= rule.up_guarantee_losses
        )
        if force_up:
            choose_up = True
        else:
            branch_total = group.up_weight + group.common_weight
            choose_up = rng.randbelow(branch_total) < group.up_weight
        if choose_up:
            selected = self._weighted_member(group.up_members, rng)
            strategy_state[not_up_key] = 0
            return selected, True, False
        selected = self._weighted_member(group.common_members, rng)
        strategy_state[not_up_key] += 1
        return selected, False, False

    def _materialize(
        self,
        state: Dict[str, Any],
        inventory: Dict[Any, Any],
        soul_id: int,
        now: int,
    ) -> Tuple[bool, Tuple[Reward, ...]]:
        owned = state["owned"]
        source = _lookup(owned, soul_id, None)
        previous = dict(source) if isinstance(source, Mapping) else {}
        count = _nonnegative(previous.get("count"))
        is_new = count == 0
        info = self.catalog.soul_infos[soul_id]
        conversions: Tuple[Reward, ...] = ()
        if not is_new:
            awake_level = _nonnegative(previous.get("awake_level"))
            if awake_level >= info.max_awake and info.overflow_rewards:
                conversions = info.overflow_rewards
            else:
                conversions = info.awaken_rewards
            for reward in conversions:
                current = self._inventory_count(inventory, reward.item_id)
                self._set_inventory_count(
                    inventory, reward.item_id, current + reward.count
                )
        previous.update(
            {
                "count": count + 1,
                "first_timestamp": _nonnegative(
                    previous.get("first_timestamp"), now
                )
                or now,
                "last_timestamp": now,
                "awake_level": _nonnegative(previous.get("awake_level")),
            }
        )
        _store_indexed(owned, soul_id, previous)
        return is_new, conversions

    def _pruned_history(
        self, state: Mapping[str, Any], now: int
    ) -> List[Dict[str, Any]]:
        cutoff = now - self.catalog.history_retention_days * 86_400
        rows: List[Dict[str, Any]] = []
        history = state.get("history")
        if not isinstance(history, list):
            return rows
        for value in history:
            if not isinstance(value, Mapping):
                continue
            pool_id = _nonnegative(value.get("pool_id"))
            soul_id = _nonnegative(value.get("soul_id"))
            timestamp = _nonnegative(value.get("timestamp"))
            if (
                pool_id in self.catalog.pools
                and soul_id in self.catalog.soul_infos
                and timestamp >= cutoff
            ):
                rows.append(copy.deepcopy(dict(value)))
        rows.sort(key=lambda row: (_nonnegative(row.get("timestamp")), _nonnegative(row.get("draw_index"))))
        return rows

    def _consume(
        self,
        profile: Mapping[str, Any],
        draw_type: int,
    ) -> Tuple[Optional[Dict[Any, Any]], int, int, int, int, int]:
        required_tickets = self.catalog.ticket_costs[draw_type]
        source_inventory = _mapping(profile.get("inventory"))
        inventory: Dict[Any, Any] = copy.deepcopy(dict(source_inventory))
        ticket_balance = self._inventory_count(inventory, self.catalog.ticket_item_id)
        ticket_used = min(ticket_balance, required_tickets)
        missing = required_tickets - ticket_used
        premium_cost = missing * self.catalog.minidiamond_per_ticket
        minidiamond = _nonnegative(profile.get("minidiamond"))
        diamond = _nonnegative(profile.get("diamond"))
        minidiamond_used = min(minidiamond, premium_cost)
        remaining = premium_cost - minidiamond_used
        diamond_used = (
            (remaining + self.catalog.diamond_exchange_rate - 1)
            // self.catalog.diamond_exchange_rate
        )
        if diamond_used > diamond:
            return None, ticket_used, minidiamond_used, diamond_used, minidiamond, diamond
        self._set_inventory_count(
            inventory, self.catalog.ticket_item_id, ticket_balance - ticket_used
        )
        return (
            inventory,
            ticket_used,
            minidiamond_used,
            diamond_used,
            minidiamond - minidiamond_used,
            diamond - diamond_used,
        )

    def pool_catalog_response(
        self,
        profile: Optional[Mapping[str, Any]] = None,
        *,
        at: Optional[Any] = None,
        include_inactive: Optional[bool] = None,
    ) -> SoulGachaResult:
        profile = profile or {}
        state = self._state(profile)
        current = self._now(at)
        include_all = (
            self.include_inactive_pools
            if include_inactive is None
            else bool(include_inactive)
        )
        pool_ids = (
            self.catalog.pool_ids
            if include_all
            else self.catalog.active_pool_ids(
                current,
                ignore_date_window=self.offline_all_pools,
            )
        )
        payload = bytearray()
        strategy_ids: set[int] = set()
        for pool_id in pool_ids:
            pool = self.catalog.pools[pool_id]
            strategy_ids.add(pool.strategy_id)
            payload.extend(
                field_message(1, make_soul_card_pool(pool_id, self._pool_state(state, pool_id)))
            )
        for strategy_id in sorted(strategy_ids):
            payload.extend(
                field_message(
                    2,
                    make_soul_strategy(
                        strategy_id, self._strategy_state(state, strategy_id)
                    ),
                )
            )
        return SoulGachaResult(
            response_id=SC_REQUEST_SOUL_CARD_POOL_DATAS_RET,
            body=bytes(payload),
            request_id=CS_REQUEST_SOUL_CARD_POOL_DATAS,
        )

    def get_wishes(
        self, profile: Optional[Mapping[str, Any]] = None
    ) -> SoulGachaResult:
        state = self._state(profile or {})
        payload = bytearray()
        for pool_id in self.catalog.pool_ids:
            wish = self._wish_state(state, pool_id)
            if wish is not None:
                payload.extend(field_message(1, make_wish_soul_shard(pool_id, wish)))
        return SoulGachaResult(
            response_id=SC_GET_WISH_SOUL_SHARD_RET,
            body=bytes(payload),
            request_id=CS_GET_WISH_SOUL_SHARD,
        )

    def _set_wish_failure(self, error_code: int, reason: str) -> SoulGachaResult:
        return SoulGachaResult(
            response_id=SC_SET_WISH_SOUL_SHARD_RET,
            body=field_varint(1, error_code),
            error_code=error_code,
            request_id=CS_SET_WISH_SOUL_SHARD,
            reason=reason,
        )

    def set_wish(
        self,
        profile: Optional[Mapping[str, Any]],
        pool_id: int,
        soul_ids: Sequence[int],
        *,
        at: Optional[Any] = None,
    ) -> SoulGachaResult:
        profile = profile or {}
        pool_id = _as_int(pool_id, -1)
        pool = self.catalog.pools.get(pool_id)
        if pool is None:
            return self._set_wish_failure(
                ERROR_CARD_POOL_NOT_FOUND, f"unknown Soul pool {pool_id}"
            )
        current = self._now(at)
        valid, reason = self._validity(pool_id, current)
        if not valid:
            return self._set_wish_failure(
                ERROR_CARD_POOL_NOT_OPEN, f"pool {pool_id} is {reason}"
            )
        if pool.pool_type != 2 or pool.wish_number <= 0:
            return self._set_wish_failure(
                ERROR_MESSAGE_DATA_ILLEGAL, f"pool {pool_id} has no wishlist"
            )
        values = tuple(_as_int(value, -1) for value in soul_ids)
        if len(values) != pool.wish_number or len(set(values)) != len(values):
            return self._set_wish_failure(
                ERROR_MESSAGE_DATA_ILLEGAL,
                f"pool {pool_id} requires {pool.wish_number} unique wish souls",
            )
        allowed = {member.soul_id for member in pool.wish_candidates}
        if not set(values) <= allowed:
            return self._set_wish_failure(
                ERROR_MESSAGE_DATA_ILLEGAL,
                f"pool {pool_id} wishlist contains a soul outside wish_pool",
            )
        state = self._state(profile)
        previous = self._wish_state(state, pool_id)
        take_effect_count = 0
        if previous is not None:
            take_effect_count = previous["take_effect_count"]
            if take_effect_count >= pool.wish_counts:
                return self._set_wish_failure(
                    ERROR_CARD_POOL_USE_REACHED_MAX,
                    f"pool {pool_id} wishlist has no remaining effects",
                )
            available_at = previous["set_timestamp"] + pool.wish_cooldown
            if current < available_at:
                return self._set_wish_failure(
                    ERROR_MESSAGE_DATA_ILLEGAL,
                    f"pool {pool_id} wishlist cooldown ends at {available_at}",
                )
        wish = {
            "soul_ids": list(values),
            "set_timestamp": current,
            "take_effect_count": take_effect_count,
        }
        _store_indexed(state["wishes"], pool_id, wish)
        body = field_varint(1, ERROR_SUCCESS) + field_message(
            2, make_wish_soul_shard(pool_id, wish)
        )
        return SoulGachaResult(
            response_id=SC_SET_WISH_SOUL_SHARD_RET,
            body=body,
            changes={STATE_KEY: state},
            request_id=CS_SET_WISH_SOUL_SHARD,
            pool_id=pool_id,
        )

    def _draw_failure(
        self,
        profile: Mapping[str, Any],
        draw_type: int,
        pool_id: int,
        error_code: int,
        reason: str,
    ) -> SoulGachaResult:
        role_id = _nonnegative(profile.get("role_id"))
        body = b"".join(
            (
                field_varint(1, error_code),
                field_varint(2, role_id),
                field_varint(3, max(0, draw_type)),
                field_varint(4, max(0, pool_id)),
            )
        )
        return SoulGachaResult(
            response_id=SC_DRAW_SOUL_RET,
            body=body,
            error_code=error_code,
            request_id=CS_DRAW_SOUL,
            pool_id=max(0, pool_id),
            draw_type=max(0, draw_type),
            reason=reason,
        )

    def draw(
        self,
        profile: Optional[Mapping[str, Any]],
        draw_type: int,
        pool_id: int,
        *,
        role_id: Optional[int] = None,
        at: Optional[Any] = None,
    ) -> SoulGachaResult:
        """Execute one transactional single/ten draw without mutating profile."""

        profile = profile or {}
        draw_type = _as_int(draw_type, -1)
        pool_id = _as_int(pool_id, -1)
        if draw_type not in DRAW_COUNTS:
            return self._draw_failure(
                profile,
                draw_type,
                pool_id,
                ERROR_MESSAGE_DATA_ILLEGAL,
                f"unsupported DrawCardType {draw_type}",
            )
        pool = self.catalog.pools.get(pool_id)
        if pool is None:
            return self._draw_failure(
                profile,
                draw_type,
                pool_id,
                ERROR_CARD_POOL_NOT_FOUND,
                f"unknown Soul pool {pool_id}",
            )
        current = self._now(at)
        valid, validity_reason = self._validity(pool_id, current)
        if not valid:
            return self._draw_failure(
                profile,
                draw_type,
                pool_id,
                ERROR_CARD_POOL_NOT_OPEN,
                f"pool {pool_id} is {validity_reason}",
            )

        state = self._state(profile)
        pool_state = self._pool_state(state, pool_id)
        draw_count = DRAW_COUNTS[draw_type]
        if pool.highest_num and pool_state["draw_num"] + draw_count > pool.highest_num:
            return self._draw_failure(
                profile,
                draw_type,
                pool_id,
                ERROR_CARD_POOL_USE_REACHED_MAX,
                f"pool {pool_id} exceeds highest_num={pool.highest_num}",
            )
        consumption = self._consume(profile, draw_type)
        inventory = consumption[0]
        if inventory is None:
            return self._draw_failure(
                profile,
                draw_type,
                pool_id,
                ERROR_MINIDIAMOND_NOT_ENOUGH,
                "Soul ticket/minidiamond/diamond balance is insufficient",
            )
        (
            _,
            ticket_used,
            minidiamond_used,
            diamond_used,
            remaining_minidiamond,
            remaining_diamond,
        ) = consumption

        strategy = self.catalog.strategies[pool.strategy_id]
        strategy_state = self._strategy_state(state, pool.strategy_id)
        wish = self._wish_state(state, pool_id)
        history = self._pruned_history(state, current)
        seed = _as_int(
            state.get(RNG_SEED_KEY),
            self.seed
            if self.seed is not None
            else _nonnegative(profile.get("role_id")),
        )
        rng = self.rng_factory(seed, _nonnegative(state.get(RNG_COUNTER_KEY)))
        outcomes: List[DrawOutcome] = []

        for index in range(draw_count):
            rarity, hard5, hard4 = self._roll_rarity(strategy, strategy_state, rng)
            member, is_up, is_wish = self._select_member(
                pool, rarity, strategy_state, wish, rng
            )
            if rarity == 5:
                strategy_state["star5_fail_draw_num"] = 0
            else:
                strategy_state["star5_fail_draw_num"] += 1
            if rarity >= 4:
                strategy_state["star4_fail_draw_num"] = 0
            else:
                strategy_state["star4_fail_draw_num"] += 1
            strategy_state["star5_prob"] = self.rate_for_failures(
                strategy.for_rarity(5), strategy_state["star5_fail_draw_num"]
            )
            strategy_state["star4_prob"] = self.rate_for_failures(
                strategy.for_rarity(4), strategy_state["star4_fail_draw_num"]
            )

            is_new, conversions = self._materialize(
                state, inventory, member.soul_id, current
            )
            outcome = DrawOutcome(
                soul_id=member.soul_id,
                rarity=rarity,
                is_new=is_new,
                is_up=is_up,
                is_wish=is_wish,
                hard_pity_5=hard5,
                hard_pity_4=hard4,
                conversions=conversions,
            )
            outcomes.append(outcome)
            pool_state["draw_num"] += 1
            history.append(
                {
                    "draw_index": pool_state["draw_num"],
                    "pool_id": pool_id,
                    "strategy_id": pool.strategy_id,
                    "soul_id": member.soul_id,
                    "rarity": rarity,
                    "timestamp": current,
                    "is_new": 1 if is_new else 0,
                    "is_up": 1 if is_up else 0,
                    "is_wish": 1 if is_wish else 0,
                }
            )

        if draw_type == DRAW_TEN:
            pool_state["ten_draw_num"] += 1
        _store_indexed(state["pools"], pool_id, pool_state)
        _store_indexed(state["strategies"], pool.strategy_id, strategy_state)
        if wish is not None:
            _store_indexed(state["wishes"], pool_id, wish)
        state["history"] = history
        state[RNG_SEED_KEY] = seed
        state[RNG_COUNTER_KEY] = _nonnegative(getattr(rng, "counter", 0))
        day = datetime.fromtimestamp(current, CONTENT_TIMEZONE).date().isoformat()
        daily = state["daily"]
        if daily.get("date") != day:
            daily = {"date": day, "drawn_num": 0}
        daily["drawn_num"] = _nonnegative(daily.get("drawn_num")) + draw_count
        state["daily"] = daily

        response_role_id = _nonnegative(
            profile.get("role_id") if role_id is None else role_id
        )
        body = bytearray(
            field_varint(1, ERROR_SUCCESS)
            + field_varint(2, response_role_id)
            + field_varint(3, draw_type)
            + field_varint(4, pool_id)
        )
        for outcome in outcomes:
            body.extend(field_message(5, make_drawn_stuff(outcome)))
        body.extend(field_varint(6, daily["drawn_num"]))
        body.extend(
            field_message(
                7, make_soul_strategy(pool.strategy_id, strategy_state)
            )
        )
        body.extend(field_varint(8, draw_count))
        body.extend(field_message(9, make_soul_card_pool(pool_id, pool_state)))
        if wish is not None:
            body.extend(field_message(10, make_wish_soul_shard(pool_id, wish)))

        return SoulGachaResult(
            response_id=SC_DRAW_SOUL_RET,
            body=bytes(body),
            changes={
                STATE_KEY: state,
                "inventory": inventory,
                "minidiamond": remaining_minidiamond,
                "diamond": remaining_diamond,
            },
            request_id=CS_DRAW_SOUL,
            pool_id=pool_id,
            draw_type=draw_type,
            draws=tuple(outcomes),
            ticket_used=ticket_used,
            minidiamond_used=minidiamond_used,
            diamond_used=diamond_used,
        )

    def records(
        self,
        profile: Optional[Mapping[str, Any]],
        request_index: int,
        *,
        at: Optional[Any] = None,
    ) -> SoulGachaResult:
        profile = profile or {}
        request_index = _as_int(request_index, -1)
        if request_index < 0 or request_index > 0xFFFFFFFF:
            return SoulGachaResult(
                response_id=SC_REQUEST_SOUL_DRAW_CARD_RECORD_RET,
                body=field_varint(1, ERROR_MESSAGE_DATA_ILLEGAL),
                error_code=ERROR_MESSAGE_DATA_ILLEGAL,
                request_id=CS_REQUEST_SOUL_DRAW_CARD_RECORD,
                reason="request index is outside uint32",
            )
        current = self._now(at)
        state = self._state(profile)
        history = self._pruned_history(state, current)
        start = request_index * RECORD_PAGE_SIZE
        page = history[start : start + RECORD_PAGE_SIZE]
        next_timestamp = current + self.catalog.history_request_cooldown
        payload = bytearray(
            field_varint(1, ERROR_SUCCESS)
            + field_varint(2, len(history))
        )
        for row in page:
            payload.extend(field_message(3, make_draw_card_record(row)))
        payload.extend(field_varint(4, next_timestamp))
        payload.extend(field_varint(5, request_index))
        state["history"] = history
        state["record_next_request_timestamp"] = next_timestamp
        return SoulGachaResult(
            response_id=SC_REQUEST_SOUL_DRAW_CARD_RECORD_RET,
            body=bytes(payload),
            changes={STATE_KEY: state},
            request_id=CS_REQUEST_SOUL_DRAW_CARD_RECORD,
        )

    def handle(
        self,
        request_id: int,
        profile: Optional[Mapping[str, Any]],
        request_body: bytes = b"",
        *,
        at: Optional[Any] = None,
    ) -> Optional[SoulGachaResult]:
        request_id = int(request_id)
        if request_id not in RESPONSE_IDS:
            return None
        try:
            fields = parse_fields(request_body)
            if request_id == CS_GET_WISH_SOUL_SHARD:
                return self.get_wishes(profile)
            if request_id == CS_REQUEST_SOUL_CARD_POOL_DATAS:
                return self.pool_catalog_response(profile, at=at)
            if request_id == CS_SET_WISH_SOUL_SHARD:
                return self.set_wish(
                    profile,
                    first_int(fields, 1, 0),
                    repeated_uint32(fields, 2),
                    at=at,
                )
            if request_id == CS_DRAW_SOUL:
                role_id = first_int(fields, 1, _nonnegative(_mapping(profile).get("role_id")))
                return self.draw(
                    profile,
                    first_int(fields, 2, DRAW_SINGLE),
                    first_int(fields, 3, 0),
                    role_id=role_id,
                    at=at,
                )
            if request_id == CS_REQUEST_SOUL_DRAW_CARD_RECORD:
                return self.records(
                    profile, first_int(fields, 1, 0), at=at
                )
        except ProtobufDecodeError as exc:
            response_id = RESPONSE_IDS[request_id]
            error_field = b"" if request_id in {
                CS_GET_WISH_SOUL_SHARD,
                CS_REQUEST_SOUL_CARD_POOL_DATAS,
            } else field_varint(1, ERROR_MESSAGE_DATA_ILLEGAL)
            return SoulGachaResult(
                response_id=response_id,
                body=error_field,
                error_code=ERROR_MESSAGE_DATA_ILLEGAL,
                request_id=request_id,
                reason=str(exc),
            )
        raise AssertionError(f"unreachable Soul request {request_id}")


@lru_cache(maxsize=1)
def default_engine() -> SoulGachaEngine:
    return SoulGachaEngine()


def handle_soul_gacha_request(
    request_id: int,
    profile: Optional[Mapping[str, Any]],
    request_body: bytes = b"",
    *,
    at: Optional[Any] = None,
) -> Optional[SoulGachaResult]:
    return default_engine().handle(request_id, profile, request_body, at=at)


def draw_soul(
    profile: Optional[Mapping[str, Any]],
    draw_type: int,
    pool_id: int,
    *,
    role_id: Optional[int] = None,
    at: Optional[Any] = None,
) -> SoulGachaResult:
    return default_engine().draw(
        profile, draw_type, pool_id, role_id=role_id, at=at
    )
