"""Static-table-backed state engine for the local activity lottery.

The module is deliberately independent from ``server.py``.  It accepts an
account profile snapshot and returns a shallow-mergeable ``changes`` mapping,
so the caller can commit the complete draw transaction through
``PersistentStateStore.mutate_account``.  No generated protobuf package is
needed; the small wire surface is encoded from the field numbers defined
from ``runtime assembly`` and recorded in ``protocol_catalog.json``.

Identifiers are kept in their source namespaces:

* ``a_lottery_set.id`` identifies one independently rolled reward set;
* ``a_lottery_pool.id`` is the reward ID returned by the draw response;
* ``Reward.res_type``/``item_id`` are kept exactly as configured.  Only
  ``ResourceType.ITEM == 4`` is joined to ``a_item``.  Other resource types
  are persisted by the lossless ``"type:id"`` key and are never guessed to
  refer to another content table.
"""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    Iterator,
    List,
    Mapping,
    Optional,
    Protocol,
    Sequence,
    Tuple,
)


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONTENT_DIR = ROOT / "generated" / "content"

# MsgID values defined from runtime assembly / protocol_catalog.json.
CS_ACTIVITY_LOTTERY = 1234
SC_ACTIVITY_LOTTERY_RET = 1235

# Msg.LotteryType.
LOTTERY_ONCE = 0
LOTTERY_TEN = 1
LOTTERY_COUNTS = {LOTTERY_ONCE: 1, LOTTERY_TEN: 10}

# Msg.ResourceType.  Resource values remain protocol identities in state.
RESOURCE_ITEM = 4
PRESERVED_NON_ITEM_RESOURCE_TYPES = frozenset({1, 2, 14, 27})

# Msg.ErrorCode values defined from runtime assembly.
ERROR_SUCCESS = 0
ERROR_ACTIVITY_LOTTERY_NOT_OPEN = 444
ERROR_ACTIVITY_LOTTERY_MAX_DRAW_COUNT = 445
ERROR_ACTIVITY_LOTTERY_CONSUME_NUM = 446
ERROR_ACTIVITY_LOTTERY_CONSUME_TYPE = 447
ERROR_ACTIVITY_LOTTERY_CONSUME_NOT_ENOUGH = 448
ERROR_ACTIVITY_LOTTERY_CONFIG = 449
ERROR_CLIENT_REQUEST_PARAM = 32000

# Define.GetMiniDiaExRate reads this exact a_global row in the runtime.  It is
# exposed as catalog validation only; a_global values are otherwise untyped and
# are not inferred as foreign keys.
GLOBAL_MINIDIAMOND_EXCHANGE_RATE = 9990005

RATE_SCALE = 10_000
RESET_HOUR_UTC8 = 4
UTC8 = timezone(timedelta(hours=8))
RNG_SEED_KEY = "lottery_rng_seed"
RNG_COUNTER_KEY = "lottery_rng_counter"
STATE_KEY = "activity_lottery"
RESOURCE_BALANCES_KEY = "resource_balances"

CONTENT_TABLES = (
    "a_lottery_set",
    "a_lottery_cost",
    "a_lottery_pool",
    "a_item",
    "a_global",
)


class LotteryConfigError(ValueError):
    """Raised when the defined local tables are internally inconsistent."""


class ProtobufDecodeError(ValueError):
    """Raised when a lottery request is malformed protobuf."""


class RandomSource(Protocol):
    """Minimal injectable random source used by the engine."""

    def randbelow(self, upper: int) -> int: ...


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
    payload = b"".join(encode_varint(max(0, int(value))) for value in values)
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


def parse_lottery_request(data: bytes) -> Tuple[int, int]:
    """Decode CS_Activitylottery, applying normal proto3 last-value wins."""

    subactivity_id: Optional[int] = None
    lottery_type = LOTTERY_ONCE
    for number, wire_type, value in iter_wire_fields(data):
        if number not in {1, 2}:
            continue
        if wire_type != 0 or not isinstance(value, int):
            raise ProtobufDecodeError(f"field {number} must be a varint")
        if value > 0xFFFFFFFF:
            raise ProtobufDecodeError(f"field {number} exceeds uint32")
        if number == 1:
            subactivity_id = value
        else:
            lottery_type = value
    if subactivity_id is None or subactivity_id <= 0:
        raise ProtobufDecodeError("SubActivityId is missing")
    return subactivity_id, lottery_type


@dataclass(frozen=True)
class Resource:
    res_type: int
    item_id: int
    count: int

    @property
    def balance_key(self) -> str:
        return f"{self.res_type}:{self.item_id}"

    def scaled(self, multiplier: int) -> "Resource":
        return Resource(self.res_type, self.item_id, self.count * int(multiplier))

    def as_dict(self) -> Dict[str, int]:
        return {"type": self.res_type, "id": self.item_id, "num": self.count}


@dataclass(frozen=True)
class LotterySetConfig:
    set_id: int
    subactivity_id: int
    ball_colors: Tuple[int, ...]
    base_rate: int
    increase_interval: int
    increase_amount: int
    hard_pity: int
    special_reward: Tuple[int, ...]
    effect: int


@dataclass(frozen=True)
class LotteryRewardConfig:
    reward_id: int
    subactivity_id: int
    set_id: int
    show_slot: int
    pool_type: int
    weight: int
    stock: int
    reward_type: int
    gospel_reward: int
    resources: Tuple[Resource, ...]


@dataclass(frozen=True)
class LotteryCostConfig:
    config_id: int
    subactivity_id: int
    score_per_draw: int
    ten_discount_percent: int
    ten_discount_type: int
    primary_costs: Tuple[Resource, ...]
    fallback_costs: Tuple[Resource, ...]
    max_draw_count: int
    shown_sets: Tuple[int, ...]
    page1_sets: Tuple[int, ...]
    page2_sets: Tuple[int, ...]
    hard_pity_set_id: int
    hard_pity_reward_id: int


@dataclass(frozen=True)
class LotterySetState:
    set_id: int
    failures: int
    add_rate: int
    successes: int
    probability: int


@dataclass(frozen=True)
class LotteryRewardState:
    reward_id: int
    get_num: int


@dataclass(frozen=True)
class ActivityLotterySnapshot:
    subactivity_id: int
    is_first_daily_ten_lottery: int
    total_lottery_num: int
    total_score: int
    set_states: Tuple[LotterySetState, ...]
    reward_states: Tuple[LotteryRewardState, ...]
    reset_key: str


@dataclass(frozen=True)
class PullOutcome:
    ordinal: int
    reward_ids: Tuple[int, ...]
    grants: Tuple[Resource, ...]


@dataclass(frozen=True)
class LotteryResult:
    """One SC_ActivitylotteryRet and its atomic profile change set."""

    response_id: int
    body: bytes
    changes: Dict[str, Any] = field(default_factory=dict)
    error_code: int = ERROR_SUCCESS
    subactivity_id: int = 0
    lottery_type: int = LOTTERY_ONCE
    pulls: Tuple[PullOutcome, ...] = ()
    costs: Tuple[Resource, ...] = ()
    grants: Tuple[Resource, ...] = ()
    snapshot: Optional[ActivityLotterySnapshot] = None
    supported: bool = True
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.error_code == ERROR_SUCCESS

    @property
    def reward_ids(self) -> Tuple[int, ...]:
        return tuple(reward_id for pull in self.pulls for reward_id in pull.reward_ids)


class CounterRng:
    """Counter-based default RNG whose position can be persisted in changes."""

    def __init__(self, seed: int, counter: int = 0) -> None:
        self.seed = int(seed)
        self.counter = max(0, int(counter))

    def randbelow(self, upper: int) -> int:
        if upper <= 0:
            raise ValueError("upper must be positive")
        payload = f"activity-lottery:{self.seed}:{self.counter}".encode("ascii")
        value = int.from_bytes(hashlib.sha256(payload).digest(), "big")
        self.counter += 1
        return value % upper


def _randbelow(rng: Any, upper: int) -> int:
    if upper <= 0:
        raise ValueError("upper must be positive")
    method = getattr(rng, "randbelow", None)
    if callable(method):
        value = int(method(upper))
    else:
        method = getattr(rng, "randrange", None)
        if not callable(method):
            raise TypeError("RNG must provide randbelow(upper) or randrange(upper)")
        value = int(method(upper))
    if not 0 <= value < upper:
        raise ValueError(f"RNG returned {value} outside [0, {upper})")
    return value


def _chance(rng: Any, rate: int) -> bool:
    if rate <= 0:
        return False
    if rate >= RATE_SCALE:
        return True
    return _randbelow(rng, RATE_SCALE) < rate


def _parse_json_list(value: Any, context: str) -> Tuple[Any, ...]:
    if value in (None, ""):
        return ()
    try:
        decoded = json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError) as exc:
        raise LotteryConfigError(f"{context}: invalid JSON") from exc
    if not isinstance(decoded, list):
        raise LotteryConfigError(f"{context}: value must be a list")
    return tuple(decoded)


def _parse_int_list(value: Any, context: str) -> Tuple[int, ...]:
    values = _parse_json_list(value, context)
    result = tuple(_as_int(item, -1) for item in values)
    if any(item < 0 for item in result):
        raise LotteryConfigError(f"{context}: list contains a non-integer")
    return result


def _parse_resources(value: Any, context: str) -> Tuple[Resource, ...]:
    rows = _parse_json_list(value, context)
    resources: List[Resource] = []
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise LotteryConfigError(f"{context}[{index}]: resource must be an object")
        resource = Resource(
            res_type=_as_int(row.get("type"), -1),
            item_id=_as_int(row.get("id"), -1),
            count=_as_int(row.get("num"), -1),
        )
        if resource.res_type < 0 or resource.item_id < 0 or resource.count <= 0:
            raise LotteryConfigError(f"{context}[{index}]: invalid resource values")
        if resource.res_type == RESOURCE_ITEM and resource.item_id <= 0:
            raise LotteryConfigError(f"{context}[{index}]: item ID must be positive")
        resources.append(resource)
    return tuple(resources)


class LotteryCatalog:
    """Validated view of the five generated tables used by this engine."""

    def __init__(self, content_dir: Path = DEFAULT_CONTENT_DIR) -> None:
        self.content_dir = Path(content_dir).resolve()
        tables = {name: self._records(name) for name in CONTENT_TABLES}
        self.items = self._rows_by_id(tables["a_item"], "a_item")
        self.globals = self._rows_by_id(tables["a_global"], "a_global")
        exchange = self.globals.get(GLOBAL_MINIDIAMOND_EXCHANGE_RATE)
        if exchange is None or _as_int(exchange.get("int_value"), 0) <= 0:
            raise LotteryConfigError(
                f"a_global[{GLOBAL_MINIDIAMOND_EXCHANGE_RATE}] is missing/invalid"
            )
        self.minidiamond_exchange_rate = _as_int(exchange["int_value"])

        sets: Dict[int, LotterySetConfig] = {}
        for row in tables["a_lottery_set"]:
            config = LotterySetConfig(
                set_id=_as_int(row.get("id"), -1),
                subactivity_id=_as_int(row.get("subactivityid"), -1),
                ball_colors=_parse_int_list(
                    row.get("ball_color"), f"a_lottery_set[{row.get('id')}].ball_color"
                ),
                base_rate=_as_int(row.get("rare_chance"), -1),
                increase_interval=_nonnegative(row.get("rare_increase_time")),
                increase_amount=_nonnegative(row.get("rare_chance_increase")),
                hard_pity=_nonnegative(row.get("abs_rare_times")),
                special_reward=_parse_int_list(
                    row.get("abs_special_reward_times"),
                    f"a_lottery_set[{row.get('id')}].abs_special_reward_times",
                ),
                effect=_nonnegative(row.get("lottery_effect")),
            )
            if (
                config.set_id <= 0
                or config.subactivity_id <= 0
                or not 0 <= config.base_rate <= RATE_SCALE
                or config.set_id in sets
            ):
                raise LotteryConfigError(
                    f"a_lottery_set: invalid/duplicate row {config}"
                )
            sets[config.set_id] = config
        self.sets = sets

        rewards: Dict[int, LotteryRewardConfig] = {}
        members: Dict[int, List[LotteryRewardConfig]] = {set_id: [] for set_id in sets}
        for row in tables["a_lottery_pool"]:
            resources = _parse_resources(
                row.get("item_list"), f"a_lottery_pool[{row.get('id')}].item_list"
            )
            config = LotteryRewardConfig(
                reward_id=_as_int(row.get("id"), -1),
                subactivity_id=_as_int(row.get("subactivityid"), -1),
                set_id=_as_int(row.get("lottery_ID"), -1),
                show_slot=_nonnegative(row.get("reward_show")),
                pool_type=_nonnegative(row.get("reward_pool_type")),
                weight=_as_int(row.get("initial_rate"), -1),
                stock=_nonnegative(row.get("reward_get_times")),
                reward_type=_nonnegative(row.get("reward_type")),
                gospel_reward=_nonnegative(row.get("gospel_rewards")),
                resources=resources,
            )
            parent = sets.get(config.set_id)
            if (
                config.reward_id <= 0
                or parent is None
                or config.subactivity_id != parent.subactivity_id
                or config.weight <= 0
                or config.stock <= 0
                or not resources
                or config.reward_id in rewards
            ):
                raise LotteryConfigError(
                    f"a_lottery_pool: invalid/duplicate row {config}"
                )
            self._validate_resources(resources, f"a_lottery_pool[{config.reward_id}]")
            rewards[config.reward_id] = config
            members[config.set_id].append(config)
        self.rewards = rewards
        self.members_by_set = {
            set_id: tuple(sorted(rows, key=lambda item: item.reward_id))
            for set_id, rows in members.items()
        }
        for set_id, rows in self.members_by_set.items():
            if not rows or sum(row.weight for row in rows) != RATE_SCALE:
                raise LotteryConfigError(
                    f"a_lottery_pool set {set_id} weights do not total {RATE_SCALE}"
                )

        costs: Dict[int, LotteryCostConfig] = {}
        for row in tables["a_lottery_cost"]:
            primary = _parse_resources(
                row.get("lottery_cost_way1"),
                f"a_lottery_cost[{row.get('id')}].lottery_cost_way1",
            )
            fallback = _parse_resources(
                row.get("lottery_cost_way2"),
                f"a_lottery_cost[{row.get('id')}].lottery_cost_way2",
            )
            config = LotteryCostConfig(
                config_id=_as_int(row.get("id"), -1),
                subactivity_id=_as_int(row.get("subactivityid"), -1),
                score_per_draw=_nonnegative(row.get("lottery_score")),
                ten_discount_percent=_nonnegative(row.get("multi_draw_discount")),
                ten_discount_type=_nonnegative(row.get("multi_draw_type")),
                primary_costs=primary,
                fallback_costs=fallback,
                max_draw_count=_nonnegative(row.get("max_draw_count")),
                shown_sets=_parse_int_list(
                    row.get("show_lottery"),
                    f"a_lottery_cost[{row.get('id')}].show_lottery",
                ),
                page1_sets=_parse_int_list(
                    row.get("page1_show_lottery"),
                    f"a_lottery_cost[{row.get('id')}].page1_show_lottery",
                ),
                page2_sets=_parse_int_list(
                    row.get("page2_show_lottery"),
                    f"a_lottery_cost[{row.get('id')}].page2_show_lottery",
                ),
                hard_pity_set_id=_nonnegative(row.get("abs_rare_pool")),
                hard_pity_reward_id=_nonnegative(row.get("abs_rare_item")),
            )
            activity_sets = {
                set_id
                for set_id, item in sets.items()
                if item.subactivity_id == config.subactivity_id
            }
            referenced_sets = set(
                config.shown_sets + config.page1_sets + config.page2_sets
            )
            if config.hard_pity_set_id:
                referenced_sets.add(config.hard_pity_set_id)
            hard_reward = rewards.get(config.hard_pity_reward_id)
            if (
                config.config_id <= 0
                or config.subactivity_id <= 0
                or config.subactivity_id in costs
                or not activity_sets
                or not referenced_sets.issubset(activity_sets)
                or len(primary) != 1
                or primary[0].res_type != RESOURCE_ITEM
                or len(fallback) != 1
                or config.max_draw_count <= 0
                or not 0 < config.ten_discount_percent <= 100
                or hard_reward is None
                or hard_reward.set_id != config.hard_pity_set_id
            ):
                raise LotteryConfigError(
                    f"a_lottery_cost: invalid/duplicate row {config}"
                )
            self._validate_resources(
                primary, f"a_lottery_cost[{config.config_id}].way1"
            )
            self._validate_resources(
                fallback, f"a_lottery_cost[{config.config_id}].way2"
            )
            costs[config.subactivity_id] = config
        self.costs_by_activity = costs
        self.subactivity_ids = tuple(sorted(costs))
        self.set_ids = tuple(sorted(sets))
        self.reward_ids = tuple(sorted(rewards))

        for set_id, config in sets.items():
            if config.subactivity_id not in costs:
                raise LotteryConfigError(f"a_lottery_set[{set_id}] has no cost row")

    def _records(self, name: str) -> Tuple[Mapping[str, Any], ...]:
        if name not in CONTENT_TABLES:
            raise LotteryConfigError(f"table outside lottery scope: {name}")
        path = self.content_dir / f"{name}.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise LotteryConfigError(f"cannot load {path}") from exc
        if not isinstance(payload, Mapping) or payload.get("table") != name:
            raise LotteryConfigError(f"{path}: unexpected table envelope")
        rows = payload.get("records")
        if not isinstance(rows, list) or not all(
            isinstance(row, Mapping) for row in rows
        ):
            raise LotteryConfigError(f"{path}: records must be an object list")
        if _as_int(payload.get("count"), -1) != len(rows):
            raise LotteryConfigError(f"{path}: declared count does not match records")
        return tuple(rows)

    @staticmethod
    def _rows_by_id(
        rows: Sequence[Mapping[str, Any]], table_name: str
    ) -> Dict[int, Mapping[str, Any]]:
        result: Dict[int, Mapping[str, Any]] = {}
        for row in rows:
            row_id = _as_int(row.get("id"), -1)
            if row_id <= 0 or row_id in result:
                raise LotteryConfigError(f"{table_name}: invalid/duplicate id {row_id}")
            result[row_id] = row
        return result

    def _validate_resources(self, resources: Iterable[Resource], context: str) -> None:
        for resource in resources:
            if resource.res_type != RESOURCE_ITEM:
                # Deliberately no target-table inference for protocol resource types.
                continue
            item = self.items.get(resource.item_id)
            if item is None:
                raise LotteryConfigError(
                    f"{context}: item {resource.item_id} absent from a_item"
                )
            maximum = _nonnegative(item.get("max_stack_num"))
            if maximum and resource.count > maximum:
                raise LotteryConfigError(
                    f"{context}: item {resource.item_id} count exceeds max_stack_num"
                )

    def sets_for(self, subactivity_id: int) -> Tuple[LotterySetConfig, ...]:
        return tuple(
            self.sets[set_id]
            for set_id in self.set_ids
            if self.sets[set_id].subactivity_id == int(subactivity_id)
        )

    def rewards_for(self, subactivity_id: int) -> Tuple[LotteryRewardConfig, ...]:
        return tuple(
            self.rewards[reward_id]
            for reward_id in self.reward_ids
            if self.rewards[reward_id].subactivity_id == int(subactivity_id)
        )


def make_activity_lottery_info(state: LotterySetState) -> bytes:
    # Msg.ActivityLotteryInfo fields 1..5.
    return b"".join(
        (
            field_varint(1, state.set_id),
            field_varint(2, state.failures),
            field_varint(3, state.add_rate),
            field_varint(4, state.successes),
            field_varint(5, state.probability),
        )
    )


def make_activity_lottery_reward_info(state: LotteryRewardState) -> bytes:
    # Msg.ActivityLotteryRewardInfo: RewardId=1, GetNum=2.
    return field_varint(1, state.reward_id) + field_varint(2, state.get_num)


def make_activity_lottery(snapshot: ActivityLotterySnapshot) -> bytes:
    """Encode Msg.ActivityLottery for SC_RequestActivity nesting."""

    payload = bytearray()
    for state in snapshot.set_states:
        payload.extend(field_message(1, make_activity_lottery_info(state)))
    for state in snapshot.reward_states:
        payload.extend(field_message(2, make_activity_lottery_reward_info(state)))
    payload.extend(field_varint(3, snapshot.is_first_daily_ten_lottery))
    payload.extend(field_varint(4, snapshot.total_lottery_num))
    payload.extend(field_varint(5, snapshot.total_score))
    return bytes(payload)


def _map_entry(key: int, value: bytes) -> bytes:
    return field_varint(1, key) + field_message(2, value)


def make_activity_lottery_ret(
    *,
    error_code: int,
    subactivity_id: int,
    lottery_type: int,
    snapshot: Optional[ActivityLotterySnapshot] = None,
    reward_ids: Sequence[int] = (),
) -> bytes:
    """Encode the complete SC_ActivitylotteryRet schema-valid subset."""

    payload = bytearray(
        field_varint(1, max(0, int(error_code)))
        + field_varint(2, max(0, int(subactivity_id)))
        + field_varint(3, max(0, int(lottery_type)))
    )
    if snapshot is None:
        return bytes(payload)
    payload.extend(field_varint(4, snapshot.is_first_daily_ten_lottery))
    payload.extend(field_varint(5, snapshot.total_lottery_num))
    payload.extend(field_packed_uint32(6, reward_ids))
    for state in snapshot.set_states:
        payload.extend(
            field_message(
                7, _map_entry(state.set_id, make_activity_lottery_info(state))
            )
        )
    for state in snapshot.reward_states:
        payload.extend(
            field_message(
                8,
                _map_entry(state.reward_id, make_activity_lottery_reward_info(state)),
            )
        )
    payload.extend(field_varint(9, snapshot.total_score))
    return bytes(payload)


class LotteryEngine:
    """Configuration-backed activity-lottery state machine."""

    def __init__(
        self,
        content_dir: Path = DEFAULT_CONTENT_DIR,
        *,
        rng: Optional[Any] = None,
        clock: Optional[Callable[[], datetime]] = None,
        normal_economy: bool = False,
    ) -> None:
        self.catalog = LotteryCatalog(content_dir)
        self.rng = rng
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.normal_economy = bool(normal_economy)

    @staticmethod
    def reset_key(now: datetime) -> str:
        """Return the activity day keyed at 04:00 in UTC+8."""

        if not isinstance(now, datetime):
            raise TypeError("now must be datetime")
        local = now.replace(tzinfo=UTC8) if now.tzinfo is None else now.astimezone(UTC8)
        boundary_date: date = local.date()
        if local.timetz().replace(tzinfo=None) < time(RESET_HOUR_UTC8):
            boundary_date -= timedelta(days=1)
        return boundary_date.isoformat()

    @staticmethod
    def rate_for_failures(config: LotterySetConfig, failures: int) -> Tuple[int, int]:
        """Return ``(soft add, next probability)`` on the configured 10k scale."""

        failures = max(0, int(failures))
        steps = failures // config.increase_interval if config.increase_interval else 0
        add_rate = steps * config.increase_amount
        probability = min(RATE_SCALE, config.base_rate + add_rate)
        if config.hard_pity and failures >= config.hard_pity - 1:
            probability = RATE_SCALE
        return add_rate, probability

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
    def _resource_count(balances: Mapping[Any, Any], resource: Resource) -> int:
        return _nonnegative(balances.get(resource.balance_key, 0))

    @staticmethod
    def _set_resource_count(
        balances: Dict[Any, Any], resource: Resource, count: int
    ) -> None:
        balances[resource.balance_key] = max(0, int(count))

    def _now(self, value: Optional[datetime]) -> datetime:
        return value if value is not None else self.clock()

    def _normalized_state(
        self,
        profile: Mapping[str, Any],
        subactivity_id: int,
        now: datetime,
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        cost = self.catalog.costs_by_activity[subactivity_id]
        root = copy.deepcopy(dict(_mapping(profile.get(STATE_KEY))))
        source = _mapping(_lookup(root, subactivity_id, {}))
        current_reset_key = self.reset_key(now)
        saved_reset_key = str(source.get("reset_key") or "")
        first_ten = _nonnegative(source.get("is_first_daily_ten_lottery"))
        if saved_reset_key != current_reset_key:
            first_ten = 0

        total_draws = min(
            cost.max_draw_count, _nonnegative(source.get("total_lottery_num"))
        )
        activity: Dict[str, Any] = {
            "subactivity_id": subactivity_id,
            "reset_key": current_reset_key,
            "is_first_daily_ten_lottery": 1 if first_ten else 0,
            "total_lottery_num": total_draws,
            "total_score": _nonnegative(source.get("total_score")),
            "lottery_info": {},
            "reward_info": {},
            "history": copy.deepcopy(
                list(source.get("history"))
                if isinstance(source.get("history"), list)
                else []
            ),
        }
        source_sets = _mapping(source.get("lottery_info"))
        for config in self.catalog.sets_for(subactivity_id):
            saved = _mapping(_lookup(source_sets, config.set_id, {}))
            failures = _nonnegative(saved.get("rare_pool_fail_lottery_num"))
            add_rate, probability = self.rate_for_failures(config, failures)
            activity["lottery_info"][str(config.set_id)] = {
                "pool_id": config.set_id,
                "rare_pool_fail_lottery_num": failures,
                "rare_pool_reward_add_rate": add_rate,
                "rare_pool_success_lottery_num": _nonnegative(
                    saved.get("rare_pool_success_lottery_num")
                ),
                "rare_pool_probability": probability,
            }
        source_rewards = _mapping(source.get("reward_info"))
        for config in self.catalog.rewards_for(subactivity_id):
            saved = _mapping(_lookup(source_rewards, config.reward_id, {}))
            activity["reward_info"][str(config.reward_id)] = {
                "reward_id": config.reward_id,
                "get_num": min(config.stock, _nonnegative(saved.get("get_num"))),
            }
        return root, activity

    @staticmethod
    def _snapshot(activity: Mapping[str, Any]) -> ActivityLotterySnapshot:
        set_states = tuple(
            LotterySetState(
                set_id=_nonnegative(row.get("pool_id")),
                failures=_nonnegative(row.get("rare_pool_fail_lottery_num")),
                add_rate=_nonnegative(row.get("rare_pool_reward_add_rate")),
                successes=_nonnegative(row.get("rare_pool_success_lottery_num")),
                probability=_nonnegative(row.get("rare_pool_probability")),
            )
            for row in sorted(
                (
                    item
                    for item in _mapping(activity.get("lottery_info")).values()
                    if isinstance(item, Mapping)
                ),
                key=lambda item: _nonnegative(item.get("pool_id")),
            )
        )
        reward_states = tuple(
            LotteryRewardState(
                reward_id=_nonnegative(row.get("reward_id")),
                get_num=_nonnegative(row.get("get_num")),
            )
            for row in sorted(
                (
                    item
                    for item in _mapping(activity.get("reward_info")).values()
                    if isinstance(item, Mapping)
                ),
                key=lambda item: _nonnegative(item.get("reward_id")),
            )
        )
        return ActivityLotterySnapshot(
            subactivity_id=_nonnegative(activity.get("subactivity_id")),
            is_first_daily_ten_lottery=1
            if _nonnegative(activity.get("is_first_daily_ten_lottery"))
            else 0,
            total_lottery_num=_nonnegative(activity.get("total_lottery_num")),
            total_score=_nonnegative(activity.get("total_score")),
            set_states=set_states,
            reward_states=reward_states,
            reset_key=str(activity.get("reset_key") or ""),
        )

    def snapshot(
        self,
        profile: Optional[Mapping[str, Any]],
        subactivity_id: int,
        *,
        now: Optional[datetime] = None,
    ) -> ActivityLotterySnapshot:
        profile = profile or {}
        subactivity_id = int(subactivity_id)
        if subactivity_id not in self.catalog.costs_by_activity:
            raise ValueError(f"unknown lottery subactivity {subactivity_id}")
        _, activity = self._normalized_state(profile, subactivity_id, self._now(now))
        return self._snapshot(activity)

    def state_changes(
        self,
        profile: Optional[Mapping[str, Any]],
        *,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Initialize/normalize every local activity and return one state patch."""

        profile = profile or {}
        current = self._now(now)
        root = copy.deepcopy(dict(_mapping(profile.get(STATE_KEY))))
        for subactivity_id in self.catalog.subactivity_ids:
            temporary_profile = dict(profile)
            temporary_profile[STATE_KEY] = root
            root, activity = self._normalized_state(
                temporary_profile, subactivity_id, current
            )
            _store_indexed(root, subactivity_id, activity)
        return {STATE_KEY: root}

    def make_activity_snapshot(
        self,
        profile: Optional[Mapping[str, Any]],
        subactivity_id: int,
        *,
        now: Optional[datetime] = None,
    ) -> bytes:
        return make_activity_lottery(self.snapshot(profile, subactivity_id, now=now))

    @staticmethod
    def _weighted_reward(
        candidates: Sequence[LotteryRewardConfig], rng: Any
    ) -> LotteryRewardConfig:
        if not candidates:
            raise LotteryConfigError("weighted lottery set is empty")
        if len(candidates) == 1:
            return candidates[0]
        total = sum(candidate.weight for candidate in candidates)
        if total <= 0:
            raise LotteryConfigError("weighted lottery set has no positive weight")
        ticket = _randbelow(rng, total)
        cursor = 0
        for candidate in candidates:
            cursor += candidate.weight
            if ticket < cursor:
                return candidate
        raise AssertionError("weighted lottery selection fell through")

    def _balance(
        self,
        inventory: Mapping[Any, Any],
        balances: Mapping[Any, Any],
        resource: Resource,
    ) -> int:
        if resource.res_type == RESOURCE_ITEM:
            return self._inventory_count(inventory, resource.item_id)
        return self._resource_count(balances, resource)

    def _set_balance(
        self,
        inventory: Dict[Any, Any],
        balances: Dict[Any, Any],
        resource: Resource,
        count: int,
    ) -> None:
        if resource.res_type == RESOURCE_ITEM:
            self._set_inventory_count(inventory, resource.item_id, count)
        else:
            self._set_resource_count(balances, resource, count)

    def _costs(
        self,
        profile: Mapping[str, Any],
        cost: LotteryCostConfig,
        lottery_type: int,
        first_ten_used: int,
    ) -> Tuple[Tuple[Resource, ...], Optional[str]]:
        units = LOTTERY_COUNTS[lottery_type]
        if lottery_type == LOTTERY_TEN and not first_ten_used:
            units = units * cost.ten_discount_percent // 100
        primary = cost.primary_costs[0]
        required_primary = primary.scaled(units)
        inventory = _mapping(profile.get("inventory"))
        balances = _mapping(profile.get(RESOURCE_BALANCES_KEY))
        available = self._balance(inventory, balances, required_primary)
        used_primary = min(available, required_primary.count)
        missing_units = (
            required_primary.count - used_primary + primary.count - 1
        ) // primary.count
        actual: List[Resource] = []
        if used_primary:
            actual.append(Resource(primary.res_type, primary.item_id, used_primary))
        for fallback in cost.fallback_costs:
            needed = fallback.scaled(missing_units)
            if (
                needed.count
                and self._balance(inventory, balances, needed) < needed.count
            ):
                return (), (
                    f"need {required_primary.count} primary unit(s); "
                    f"fallback {needed.balance_key} requires {needed.count}"
                )
            if needed.count:
                actual.append(needed)
        return tuple(actual), None

    def _failure(
        self,
        *,
        error_code: int,
        subactivity_id: int,
        lottery_type: int,
        reason: str,
    ) -> LotteryResult:
        return LotteryResult(
            response_id=SC_ACTIVITY_LOTTERY_RET,
            body=make_activity_lottery_ret(
                error_code=error_code,
                subactivity_id=max(0, subactivity_id),
                lottery_type=max(0, lottery_type),
            ),
            error_code=error_code,
            subactivity_id=max(0, subactivity_id),
            lottery_type=max(0, lottery_type),
            reason=reason,
        )

    def draw(
        self,
        profile: Optional[Mapping[str, Any]],
        subactivity_id: int,
        lottery_type: int,
        *,
        rng: Optional[Any] = None,
        now: Optional[datetime] = None,
    ) -> LotteryResult:
        """Execute a single/ten draw without mutating ``profile``."""

        profile = profile or {}
        if self.normal_economy:
            # Type 1/2 are visible currencies, not a second legacy wallet.
            balances = copy.deepcopy(dict(_mapping(profile.get(RESOURCE_BALANCES_KEY))))
            for kind, key in ((1, "minidiamond"), (2, "gold")):
                balances[f"{kind}:0"] = _nonnegative(profile.get(key))
            profile = {**profile, RESOURCE_BALANCES_KEY: balances}
        subactivity_id = _as_int(subactivity_id, -1)
        lottery_type = _as_int(lottery_type, -1)
        if lottery_type not in LOTTERY_COUNTS:
            return self._failure(
                error_code=ERROR_ACTIVITY_LOTTERY_CONSUME_NUM,
                subactivity_id=max(0, subactivity_id),
                lottery_type=max(0, lottery_type),
                reason=f"unsupported LotteryType {lottery_type}",
            )
        cost = self.catalog.costs_by_activity.get(subactivity_id)
        if cost is None:
            return self._failure(
                error_code=ERROR_ACTIVITY_LOTTERY_NOT_OPEN,
                subactivity_id=max(0, subactivity_id),
                lottery_type=lottery_type,
                reason=f"unknown lottery subactivity {subactivity_id}",
            )

        current = self._now(now)
        root, activity = self._normalized_state(profile, subactivity_id, current)
        draw_count = LOTTERY_COUNTS[lottery_type]
        if activity["total_lottery_num"] + draw_count > cost.max_draw_count:
            return self._failure(
                error_code=ERROR_ACTIVITY_LOTTERY_MAX_DRAW_COUNT,
                subactivity_id=subactivity_id,
                lottery_type=lottery_type,
                reason=f"draw would exceed max_draw_count={cost.max_draw_count}",
            )

        costs, cost_error = self._costs(
            profile,
            cost,
            lottery_type,
            _nonnegative(activity["is_first_daily_ten_lottery"]),
        )
        if cost_error:
            return self._failure(
                error_code=ERROR_ACTIVITY_LOTTERY_CONSUME_NOT_ENOUGH,
                subactivity_id=subactivity_id,
                lottery_type=lottery_type,
                reason=cost_error,
            )

        # Validation/affordability precede all copies and RNG use.  A failed
        # request therefore cannot consume resources or replay state.
        inventory: Dict[Any, Any] = copy.deepcopy(
            dict(_mapping(profile.get("inventory")))
        )
        balances: Dict[Any, Any] = copy.deepcopy(
            dict(_mapping(profile.get(RESOURCE_BALANCES_KEY)))
        )
        for resource in costs:
            old = self._balance(inventory, balances, resource)
            self._set_balance(inventory, balances, resource, old - resource.count)

        external_rng = rng if rng is not None else self.rng
        owns_rng = external_rng is None
        if external_rng is None:
            seed = _as_int(profile.get(RNG_SEED_KEY), _as_int(profile.get("role_id")))
            active_rng: Any = CounterRng(
                seed, _nonnegative(profile.get(RNG_COUNTER_KEY))
            )
        else:
            seed = _as_int(profile.get(RNG_SEED_KEY), _as_int(profile.get("role_id")))
            active_rng = external_rng

        pulls: List[PullOutcome] = []
        all_grants: List[Resource] = []
        reward_info = activity["reward_info"]
        set_info = activity["lottery_info"]
        old_total = _nonnegative(activity["total_lottery_num"])
        for pull_index in range(draw_count):
            selected_ids: List[int] = []
            pull_grants: List[Resource] = []
            for set_config in self.catalog.sets_for(subactivity_id):
                state = set_info[str(set_config.set_id)]
                candidates = tuple(
                    reward
                    for reward in self.catalog.members_by_set[set_config.set_id]
                    if reward_info[str(reward.reward_id)]["get_num"] < reward.stock
                )
                probability = _nonnegative(state["rare_pool_probability"])
                won = bool(candidates) and _chance(active_rng, probability)
                if won:
                    selected = self._weighted_reward(candidates, active_rng)
                    reward_state = reward_info[str(selected.reward_id)]
                    reward_state["get_num"] = _nonnegative(reward_state["get_num"]) + 1
                    state["rare_pool_fail_lottery_num"] = 0
                    state["rare_pool_success_lottery_num"] = (
                        _nonnegative(state["rare_pool_success_lottery_num"]) + 1
                    )
                    selected_ids.append(selected.reward_id)
                    for grant in selected.resources:
                        old = self._balance(inventory, balances, grant)
                        new = old + grant.count
                        if grant.res_type == RESOURCE_ITEM:
                            item = self.catalog.items[grant.item_id]
                            maximum = _nonnegative(item.get("max_stack_num"))
                            if maximum:
                                new = min(maximum, new)
                        self._set_balance(inventory, balances, grant, new)
                        pull_grants.append(grant)
                        all_grants.append(grant)
                else:
                    state["rare_pool_fail_lottery_num"] = (
                        _nonnegative(state["rare_pool_fail_lottery_num"]) + 1
                    )
                add_rate, next_probability = self.rate_for_failures(
                    set_config, state["rare_pool_fail_lottery_num"]
                )
                state["rare_pool_reward_add_rate"] = add_rate
                state["rare_pool_probability"] = next_probability

            activity["total_lottery_num"] += 1
            activity["total_score"] += cost.score_per_draw
            pulls.append(
                PullOutcome(
                    ordinal=old_total + pull_index + 1,
                    reward_ids=tuple(selected_ids),
                    grants=tuple(pull_grants),
                )
            )

        if lottery_type == LOTTERY_TEN:
            activity["is_first_daily_ten_lottery"] = 1
        activity["history"].append(
            {
                "from_total_lottery_num": old_total,
                "lottery_type": lottery_type,
                "draw_count": draw_count,
                "reward_ids": [
                    reward_id for pull in pulls for reward_id in pull.reward_ids
                ],
                "costs": [resource.as_dict() for resource in costs],
                "grants": [resource.as_dict() for resource in all_grants],
                "reset_key": activity["reset_key"],
            }
        )
        _store_indexed(root, subactivity_id, activity)
        snapshot = self._snapshot(activity)
        flattened_ids = tuple(
            reward_id for pull in pulls for reward_id in pull.reward_ids
        )
        changes: Dict[str, Any] = {
            STATE_KEY: root,
            "inventory": inventory,
            RESOURCE_BALANCES_KEY: balances,
        }
        if self.normal_economy:
            changes.update(minidiamond=_nonnegative(balances.get("1:0")),
                           gold=_nonnegative(balances.get("2:0")))
            changes["resource_history"] = [*copy.deepcopy(profile.get("resource_history", [])), {
                "source": "activity_lottery", "subactivity_id": subactivity_id,
                "costs": [r.as_dict() for r in costs], "rewards": [r.as_dict() for r in all_grants]}]
        if owns_rng:
            changes[RNG_SEED_KEY] = seed
            changes[RNG_COUNTER_KEY] = active_rng.counter
        return LotteryResult(
            response_id=SC_ACTIVITY_LOTTERY_RET,
            body=make_activity_lottery_ret(
                error_code=ERROR_SUCCESS,
                subactivity_id=subactivity_id,
                lottery_type=lottery_type,
                snapshot=snapshot,
                reward_ids=flattened_ids,
            ),
            changes=changes,
            subactivity_id=subactivity_id,
            lottery_type=lottery_type,
            pulls=tuple(pulls),
            costs=costs,
            grants=tuple(all_grants),
            snapshot=snapshot,
        )

    def draw_request(
        self,
        profile: Optional[Mapping[str, Any]],
        request_body: bytes,
        *,
        rng: Optional[Any] = None,
        now: Optional[datetime] = None,
    ) -> LotteryResult:
        try:
            subactivity_id, lottery_type = parse_lottery_request(request_body)
        except ProtobufDecodeError as exc:
            return self._failure(
                error_code=ERROR_CLIENT_REQUEST_PARAM,
                subactivity_id=0,
                lottery_type=LOTTERY_ONCE,
                reason=str(exc),
            )
        return self.draw(
            profile,
            subactivity_id,
            lottery_type,
            rng=rng,
            now=now,
        )

    def handle(
        self,
        request_id: int,
        profile: Optional[Mapping[str, Any]],
        request_body: bytes = b"",
        *,
        rng: Optional[Any] = None,
        now: Optional[datetime] = None,
    ) -> Optional[LotteryResult]:
        if int(request_id) != CS_ACTIVITY_LOTTERY:
            return None
        return self.draw_request(profile, request_body, rng=rng, now=now)

    @staticmethod
    def apply_changes(profile: Dict[str, Any], changes: Mapping[str, Any]) -> None:
        """Apply a result patch using the same shallow merge as the server."""

        profile.update(copy.deepcopy(dict(changes)))

    def handle_persisted(
        self,
        store: Any,
        account: str,
        request_id: int,
        request_body: bytes = b"",
        *,
        rng: Optional[Any] = None,
        now: Optional[datetime] = None,
    ) -> Optional[LotteryResult]:
        """Run and commit a request inside a PersistentStateStore transaction."""

        holder: Dict[str, Optional[LotteryResult]] = {"result": None}

        def mutate(profile: Dict[str, Any]) -> None:
            result = self.handle(
                request_id,
                profile,
                request_body,
                rng=rng,
                now=now,
            )
            holder["result"] = result
            if result is not None and result.changes:
                self.apply_changes(profile, result.changes)

        store.mutate_account(account, mutate)
        return holder["result"]


@lru_cache(maxsize=1)
def default_engine() -> LotteryEngine:
    return LotteryEngine()


def draw_lottery(
    profile: Optional[Mapping[str, Any]],
    subactivity_id: int,
    lottery_type: int,
    *,
    rng: Optional[Any] = None,
    now: Optional[datetime] = None,
) -> LotteryResult:
    return default_engine().draw(
        profile,
        subactivity_id,
        lottery_type,
        rng=rng,
        now=now,
    )


def handle_lottery_request(
    request_id: int,
    profile: Optional[Mapping[str, Any]],
    request_body: bytes = b"",
    *,
    rng: Optional[Any] = None,
    now: Optional[datetime] = None,
) -> Optional[LotteryResult]:
    return default_engine().handle(
        request_id,
        profile,
        request_body,
        rng=rng,
        now=now,
    )
