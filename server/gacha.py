"""Pure-state Gospel gacha and hand-written protobuf responses.

The compatibility server owns persistence and locking.  This module only
loads the defined static tables, accepts an immutable profile snapshot, and
returns a top-level change set that can be committed with one shallow merge.
No generated protobuf classes or wall-clock state are required.

There are three deliberately different identifiers in this subsystem:

* ``a_summon.id`` identifies a static configuration row;
* ``a_summon.summonpool_group`` is sent as ``CS_DrawGospel.CardPoolId`` and
  keys per-pool ``CardPoolData`` state;
* ``a_summon.strategy_group`` keys shared pity ``CardPoolGroup`` state.

Keeping those namespaces separate is important: most legacy limited
pools have independent draw/discount counts but share strategy group 3 pity.
"""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Tuple

try:
    from .gospel_acquisition import GospelFactory
except ImportError:
    from gospel_acquisition import GospelFactory


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONTENT_DIR = ROOT / "generated" / "content"

CS_DRAW_GOSPEL = 39
SC_DRAW_GOSPEL_RET = 40

DRAW_SINGLE = 0
DRAW_TEN = 1
DRAW_COUNTS = {DRAW_SINGLE: 1, DRAW_TEN: 10}
PORTED_POOL_IDS = frozenset((3009, 3012, 3015))

# Msg.ResourceType values defined from runtime assembly.
RESOURCE_ITEM = 4
RESOURCE_GOSPEL = 7

# Msg.ErrorCode values defined from runtime assembly.
ERROR_SUCCESS = 0
ERROR_MINIDIAMOND_NOT_ENOUGH = 107
ERROR_EXCEED_MAX_DRAW_NUM = 135
ERROR_CARD_POOL_NOT_FOUND = 308
ERROR_CARD_POOL_NOT_UNLOCKED = 314
ERROR_CARD_POOL_USE_REACHED_MAX = 355
ERROR_CLIENT_REQUEST_PARAM = 32000
ERROR_CONFIG = 32772

# These a_global keys are the exact rows read by NewSummonView.SetCostInfo and
# NewSummonView.StartDraw in the defined runtime.  Their values, including
# the ticket item ID and prices, are still loaded from generated content.
GLOBAL_SINGLE_TICKET = 9990180
GLOBAL_TEN_TICKET = 9990181
GLOBAL_MINIDIAMOND_PER_TICKET = 9990365
GLOBAL_MINIDIAMOND_EXCHANGE_RATE = 9990005

RNG_SEED_KEY = "rng_seed"
RNG_COUNTER_KEY = "rng_counter"

CONTENT_TABLES = (
    "a_summon",
    "a_summonpool",
    "a_summonstrategy",
    "a_summonfloor",
    "a_summoncount",
    "a_summon_limited",
    "a_gospel",
    "a_item",
    "a_summon_global",
    "a_global",
    "a_initial",
)

GROUP_COUNTER_FIELDS = (
    "star5_fail_draw_num",
    "star4_fail_draw_num",
    "star5_prob",
    "star4_prob",
    "star5_not_up_num",
    "star4_not_up_num",
    "flare_fail_draw_num",
    "flare_prob",
)

POOL_COUNTER_FIELDS = (
    "first_star5_claimed",
    "star5_guaranteed_num",
    "star4_guaranteed_num",
    "draw_num",
    "ten_draw_num",
    "strategy_cfg_id",
)


class GachaConfigError(ValueError):
    """Raised when generated content is missing or internally inconsistent."""


class ProtobufDecodeError(ValueError):
    """Raised for malformed protobuf wire bytes accepted by this module."""


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
    """Store a JSON-safe numeric key without leaving an integer alias behind."""

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


def parse_fields(data: bytes) -> Dict[int, List[object]]:
    fields: Dict[int, List[object]] = {}
    for number, _wire_type, value in iter_wire_fields(data):
        fields.setdefault(number, []).append(value)
    return fields


def first_int(fields: Mapping[int, Sequence[object]], number: int, default: int = 0) -> int:
    values = fields.get(number)
    if not values or not isinstance(values[0], int):
        return default
    return int(values[0])


@dataclass(frozen=True)
class Reward:
    res_type: int
    item_id: int
    count: int


@dataclass(frozen=True)
class SummonConfig:
    config_id: int
    pool_group: int
    strategy_group: int
    pool_type: int
    up_rate_4: int
    up_rate_5: int
    highest_num: int
    open_type: int = 0
    open_condition: str = ""


@dataclass(frozen=True)
class PoolEntry:
    config_id: int
    pool_group: int
    gospel_id: int
    rare: int
    up_type: int
    weight: int


@dataclass(frozen=True)
class StrategyRule:
    config_id: int
    pool_group: int
    begin_draw_num: int
    end_draw_num: int
    strategy_id: int
    reset_5: bool
    reset_4: bool
    strategy_id_4: int
    strategy_id_5: int


@dataclass(frozen=True)
class FloorRule:
    config_id: int
    strategy_id: int
    rare: int
    base_rate: int
    rate_add_value: int
    rate_max: int
    soft_count: int
    up_rate: int
    guaranteed: bool
    rate_floor: int


@dataclass(frozen=True)
class LimitedRule:
    pool_group: int
    gospel_id: int
    base_rate: int
    rate_add_value: int
    rate_max: int
    soft_count: int
    rate_floor: int
    wish_number: int
    cooldown: int
    wish_counts: int


@dataclass(frozen=True)
class SummonCountRule:
    config_id: int
    pool_group: int
    min_count: int
    max_count: int
    cost_count: int


@dataclass(frozen=True)
class DrawOutcome:
    gospel_id: int
    rare: int
    is_new: bool
    is_up: bool = False
    is_flare: bool = False
    hard_pity_5: bool = False
    hard_pity_4: bool = False
    conversions: Tuple[Reward, ...] = ()


@dataclass(frozen=True)
class GachaResult:
    """One SC_DrawGospelRet plus a pure, shallow-mergeable profile update."""

    response_id: int
    body: bytes
    changes: Dict[str, Any] = field(default_factory=dict)
    error_code: int = ERROR_SUCCESS
    role_id: int = 0
    draw_type: int = DRAW_SINGLE
    card_pool_id: int = 0
    strategy_group: int = 0
    draws: Tuple[DrawOutcome, ...] = ()
    ticket_used: int = 0
    minidiamond_used: int = 0
    supported: bool = True
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.error_code == ERROR_SUCCESS


class CounterRng:
    """Small counter-based deterministic RNG suitable for replaying a save."""

    def __init__(self, seed: int, counter: int = 0) -> None:
        self.seed = int(seed)
        self.counter = max(0, int(counter))

    def randbelow(self, upper: int) -> int:
        if upper <= 0:
            raise ValueError("upper must be positive")
        payload = f"gospel-gacha:{self.seed}:{self.counter}".encode("ascii")
        value = int.from_bytes(hashlib.sha256(payload).digest(), "big")
        self.counter += 1
        return value % upper

    def chance(self, rate: int, scale: int = 10_000) -> bool:
        if rate <= 0:
            return False
        if rate >= scale:
            return True
        return self.randbelow(scale) < rate


def _parse_rewards(value: Any, context: str) -> Tuple[Reward, ...]:
    if value in (None, ""):
        return ()
    try:
        decoded = json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError) as exc:
        raise GachaConfigError(f"{context}: invalid reward JSON") from exc
    if not isinstance(decoded, list):
        raise GachaConfigError(f"{context}: rewards must be a list")
    rewards: List[Reward] = []
    for index, row in enumerate(decoded):
        if not isinstance(row, Mapping):
            raise GachaConfigError(f"{context}[{index}]: reward must be an object")
        reward = Reward(
            res_type=_as_int(row.get("type"), -1),
            item_id=_as_int(row.get("id"), -1),
            count=_as_int(row.get("num"), -1),
        )
        if reward.res_type < 0 or reward.item_id <= 0 or reward.count <= 0:
            raise GachaConfigError(f"{context}[{index}]: invalid reward values")
        rewards.append(reward)
    return tuple(rewards)


class GachaCatalog:
    """Validated view of only the generated tables used by Gospel draws."""

    def __init__(self, content_dir: Path = DEFAULT_CONTENT_DIR) -> None:
        self.content_dir = Path(content_dir).resolve()
        tables = {name: self._records(name) for name in CONTENT_TABLES}

        self.items: Dict[int, Mapping[str, Any]] = {}
        for row in tables["a_item"]:
            item_id = _as_int(row.get("id"), -1)
            if item_id <= 0 or item_id in self.items:
                raise GachaConfigError(f"a_item: invalid/duplicate id {item_id}")
            self.items[item_id] = row

        self.globals = self._rows_by_id(tables["a_global"], "a_global")
        self.summon_globals = self._rows_by_id(
            tables["a_summon_global"], "a_summon_global"
        )
        single_ticket = self._global_rewards(GLOBAL_SINGLE_TICKET)
        ten_ticket = self._global_rewards(GLOBAL_TEN_TICKET)
        if len(single_ticket) != 1 or len(ten_ticket) != 1:
            raise GachaConfigError("single/ten ticket globals must each contain one item")
        if single_ticket[0].item_id != ten_ticket[0].item_id:
            raise GachaConfigError("single and ten draws use different ticket items")
        if single_ticket[0].res_type != RESOURCE_ITEM or ten_ticket[0].res_type != RESOURCE_ITEM:
            raise GachaConfigError("Gospel ticket globals are not item rewards")
        if single_ticket[0].item_id not in self.items:
            raise GachaConfigError("Gospel ticket item is absent from a_item")
        self.ticket_item_id = single_ticket[0].item_id
        self.single_ticket_count = single_ticket[0].count
        self.ten_ticket_count = ten_ticket[0].count
        self.minidiamond_per_ticket = self._global_int(
            GLOBAL_MINIDIAMOND_PER_TICKET, minimum=1
        )
        self.minidiamond_exchange_rate = self._global_int(
            GLOBAL_MINIDIAMOND_EXCHANGE_RATE, minimum=1
        )

        all_gospels = self._rows_by_id(tables["a_gospel"], "a_gospel")
        self.gospels = {
            gospel_id: row
            for gospel_id, row in all_gospels.items()
            if _as_int(row.get("using_gospel")) == 1
        }
        self.usable_gospel_ids = frozenset(self.gospels)
        self.repeat_rewards: Dict[int, Tuple[Reward, ...]] = {}
        self.repeat_over_rewards: Dict[int, Tuple[Reward, ...]] = {}
        for gospel_id, row in self.gospels.items():
            normal = _parse_rewards(row.get("repeat_item"), f"a_gospel[{gospel_id}].repeat_item")
            overflow = _parse_rewards(
                row.get("repitem_over"), f"a_gospel[{gospel_id}].repitem_over"
            )
            self._validate_item_rewards(normal, f"a_gospel[{gospel_id}].repeat_item")
            self._validate_item_rewards(overflow, f"a_gospel[{gospel_id}].repitem_over")
            self.repeat_rewards[gospel_id] = normal
            self.repeat_over_rewards[gospel_id] = overflow

        summons: List[SummonConfig] = []
        by_config: Dict[int, SummonConfig] = {}
        by_pool: Dict[int, SummonConfig] = {}
        for row in tables["a_summon"]:
            summon = SummonConfig(
                config_id=_as_int(row.get("id"), -1),
                pool_group=_as_int(row.get("summonpool_group"), -1),
                strategy_group=_as_int(row.get("strategy_group"), -1),
                pool_type=_as_int(row.get("summonpool_type"), -1),
                up_rate_4=_nonnegative(row.get("uprate_4")),
                up_rate_5=_nonnegative(row.get("uprate_5")),
                highest_num=_nonnegative(row.get("highest_num")),
                open_type=_as_int(row.get("open_type")),
                open_condition=str(row.get("open_condition") or ""),
            )
            if (
                summon.config_id <= 0
                or summon.pool_group <= 0
                or summon.strategy_group <= 0
                or summon.pool_type <= 0
            ):
                raise GachaConfigError(f"a_summon: invalid row {summon}")
            if summon.config_id in by_config or summon.pool_group in by_pool:
                raise GachaConfigError("a_summon has duplicate config/pool identifiers")
            summons.append(summon)
            by_config[summon.config_id] = summon
            by_pool[summon.pool_group] = summon
        self.summons = tuple(summons)
        self.summons_by_config = by_config
        self.summons_by_pool = by_pool
        self.config_ids = frozenset(by_config)
        self.pool_ids = frozenset(by_pool)
        self.strategy_group_ids = frozenset(row.strategy_group for row in summons)

        all_entries: Dict[int, List[PoolEntry]] = {}
        raw_pool_groups = set()
        self.filtered_nonpositive_count = 0
        self.filtered_unusable_count = 0
        for row in tables["a_summonpool"]:
            pool_group = _as_int(row.get("pool_group"), -1)
            raw_pool_groups.add(pool_group)
            weight = _as_int(row.get("weight"), 0)
            if weight <= 0:
                self.filtered_nonpositive_count += 1
                continue
            gospel_id = _as_int(row.get("gospel_id"), -1)
            if gospel_id not in self.usable_gospel_ids:
                self.filtered_unusable_count += 1
                continue
            entry = PoolEntry(
                config_id=_as_int(row.get("id"), -1),
                pool_group=pool_group,
                gospel_id=gospel_id,
                rare=_as_int(row.get("gospel_rare"), -1),
                up_type=_as_int(row.get("up_type"), 0),
                weight=weight,
            )
            if entry.config_id <= 0 or entry.pool_group <= 0 or entry.rare not in {3, 4, 5}:
                raise GachaConfigError(f"a_summonpool: invalid row {entry}")
            all_entries.setdefault(pool_group, []).append(entry)
        self.raw_pool_groups = frozenset(raw_pool_groups)
        self.orphan_pool_groups = frozenset(raw_pool_groups - self.pool_ids)
        self.entries_by_pool = {
            pool_id: tuple(all_entries.get(pool_id, ())) for pool_id in self.pool_ids
        }
        for pool_id, entries in self.entries_by_pool.items():
            rares = {entry.rare for entry in entries}
            if rares != {3, 4, 5}:
                raise GachaConfigError(
                    f"a_summonpool[{pool_id}] has no positive usable row for rares {sorted({3,4,5} - rares)}"
                )

        strategies: Dict[int, List[StrategyRule]] = {}
        for row in tables["a_summonstrategy"]:
            strategy = StrategyRule(
                config_id=_as_int(row.get("id"), -1),
                pool_group=_as_int(row.get("summonpoolgroup"), -1),
                begin_draw_num=_nonnegative(row.get("begin_drawnum")),
                end_draw_num=_nonnegative(row.get("end_drawnum")),
                strategy_id=_as_int(row.get("strategy_id"), -1),
                reset_5=bool(_as_int(row.get("reset_5"))),
                reset_4=bool(_as_int(row.get("reset_4"))),
                strategy_id_4=_as_int(row.get("strategy_id_4"), -1),
                strategy_id_5=_as_int(row.get("strategy_id_5"), -1),
            )
            if (
                strategy.config_id <= 0
                or strategy.pool_group <= 0
                or strategy.strategy_id <= 0
                or strategy.end_draw_num < strategy.begin_draw_num
            ):
                raise GachaConfigError(f"a_summonstrategy: invalid row {strategy}")
            strategies.setdefault(strategy.pool_group, []).append(strategy)
        self.strategies_by_pool = {
            pool_id: tuple(
                sorted(strategies.get(pool_id, ()), key=lambda row: row.begin_draw_num)
            )
            for pool_id in self.pool_ids
        }
        for pool_id, rows in self.strategies_by_pool.items():
            if not rows:
                raise GachaConfigError(f"a_summonstrategy missing pool {pool_id}")

        floors: Dict[Tuple[int, int], FloorRule] = {}
        for row in tables["a_summonfloor"]:
            floor_rule = FloorRule(
                config_id=_as_int(row.get("id"), -1),
                strategy_id=_as_int(row.get("strategy_id"), -1),
                rare=_as_int(row.get("rare"), -1),
                base_rate=_nonnegative(row.get("baserate")),
                rate_add_value=_nonnegative(row.get("rate_addvalue")),
                rate_max=_nonnegative(row.get("rate_max")),
                soft_count=_nonnegative(row.get("count")),
                up_rate=_nonnegative(row.get("uprate")),
                guaranteed=bool(_as_int(row.get("guaranteed"))),
                rate_floor=_nonnegative(row.get("rate_floor")),
            )
            key = (floor_rule.strategy_id, floor_rule.rare)
            if (
                floor_rule.config_id <= 0
                or floor_rule.strategy_id <= 0
                or floor_rule.rare not in {4, 5}
                or floor_rule.base_rate > 10_000
                or floor_rule.rate_max > 10_000
                or key in floors
            ):
                raise GachaConfigError(f"a_summonfloor: invalid/duplicate row {floor_rule}")
            floors[key] = floor_rule
        self.floors = floors
        for rows in self.strategies_by_pool.values():
            for strategy in rows:
                for rare in (4, 5):
                    if (strategy.strategy_id, rare) not in floors:
                        raise GachaConfigError(
                            f"a_summonfloor missing strategy={strategy.strategy_id}, rare={rare}"
                        )

        count_rules: Dict[int, List[SummonCountRule]] = {}
        for row in tables["a_summoncount"]:
            rule = SummonCountRule(
                config_id=_as_int(row.get("id"), -1),
                pool_group=_as_int(row.get("summonpoolgroup"), -1),
                min_count=_nonnegative(row.get("mincount")),
                max_count=_nonnegative(row.get("maxcnt")),
                cost_count=_nonnegative(row.get("costcnt")),
            )
            if (
                rule.config_id <= 0
                or rule.pool_group <= 0
                or rule.min_count <= 0
                or rule.max_count < rule.min_count
                or rule.cost_count <= 0
            ):
                raise GachaConfigError(f"a_summoncount: invalid row {rule}")
            count_rules.setdefault(rule.pool_group, []).append(rule)
        self.count_rules_by_pool = {
            pool_id: tuple(sorted(rows, key=lambda row: row.min_count))
            for pool_id, rows in count_rules.items()
            if pool_id in self.pool_ids
        }

        limited: Dict[int, LimitedRule] = {}
        for row in tables["a_summon_limited"]:
            rule = LimitedRule(
                pool_group=_as_int(row.get("id"), -1),
                gospel_id=_as_int(row.get("gospel_id"), -1),
                base_rate=_nonnegative(row.get("baserate")),
                rate_add_value=_nonnegative(row.get("rate_addvalue")),
                rate_max=_nonnegative(row.get("rate_max")),
                soft_count=_nonnegative(row.get("count")),
                rate_floor=_nonnegative(row.get("rate_floor")),
                wish_number=_nonnegative(row.get("wish_number")),
                cooldown=_nonnegative(row.get("cooldown")),
                wish_counts=_nonnegative(row.get("wish_counts")),
            )
            if (
                rule.pool_group not in self.pool_ids
                or rule.gospel_id not in self.usable_gospel_ids
                or rule.base_rate > 10_000
                or rule.rate_max > 10_000
                or rule.rate_floor <= 0
                or rule.pool_group in limited
            ):
                raise GachaConfigError(f"a_summon_limited: invalid row {rule}")
            limited[rule.pool_group] = rule
        self.limited_by_pool = limited
        for summon in self.summons:
            if summon.pool_type == 7 and summon.pool_group not in limited:
                raise GachaConfigError(
                    f"type-7 summon pool {summon.pool_group} has no a_summon_limited row"
                )

    def _records(self, name: str) -> Tuple[Mapping[str, Any], ...]:
        if name not in CONTENT_TABLES:
            raise GachaConfigError(f"table is outside Gospel gacha scope: {name}")
        path = self.content_dir / f"{name}.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise GachaConfigError(f"cannot load {path}") from exc
        if not isinstance(payload, Mapping) or payload.get("table") != name:
            raise GachaConfigError(f"{path}: unexpected table envelope")
        rows = payload.get("records")
        if not isinstance(rows, list) or not all(isinstance(row, Mapping) for row in rows):
            raise GachaConfigError(f"{path}: records must be an object list")
        declared_count = _as_int(payload.get("count"), -1)
        if declared_count != len(rows):
            raise GachaConfigError(
                f"{path}: declared count {declared_count} != {len(rows)} records"
            )
        return tuple(rows)

    @staticmethod
    def _rows_by_id(
        rows: Sequence[Mapping[str, Any]], table_name: str
    ) -> Dict[int, Mapping[str, Any]]:
        result: Dict[int, Mapping[str, Any]] = {}
        for row in rows:
            row_id = _as_int(row.get("id"), -1)
            if row_id <= 0 or row_id in result:
                raise GachaConfigError(f"{table_name}: invalid/duplicate id {row_id}")
            result[row_id] = row
        return result

    def _global_rewards(self, global_id: int) -> Tuple[Reward, ...]:
        row = self.globals.get(global_id)
        if row is None:
            raise GachaConfigError(f"a_global missing id {global_id}")
        rewards = _parse_rewards(row.get("string_value"), f"a_global[{global_id}]")
        self._validate_item_rewards(rewards, f"a_global[{global_id}]")
        return rewards

    def _global_int(self, global_id: int, minimum: int = 0) -> int:
        row = self.globals.get(global_id)
        if row is None:
            raise GachaConfigError(f"a_global missing id {global_id}")
        value = _as_int(row.get("int_value"), minimum - 1)
        if value < minimum:
            raise GachaConfigError(f"a_global[{global_id}] has invalid int_value {value}")
        return value

    def _validate_item_rewards(self, rewards: Iterable[Reward], context: str) -> None:
        for reward in rewards:
            if reward.res_type != RESOURCE_ITEM or reward.item_id not in self.items:
                raise GachaConfigError(
                    f"{context}: reward {reward} is not a configured a_item"
                )

    def strategy_for(self, pool_group: int, current_draw_num: int) -> StrategyRule:
        ordinal = max(0, int(current_draw_num)) + 1
        for rule in self.strategies_by_pool.get(int(pool_group), ()):
            if rule.begin_draw_num <= ordinal <= rule.end_draw_num:
                return rule
        raise GachaConfigError(
            f"no a_summonstrategy row for pool={pool_group}, draw={ordinal}"
        )

    def floor_for(
        self, pool_group: int, current_draw_num: int, rare: int
    ) -> FloorRule:
        strategy = self.strategy_for(pool_group, current_draw_num)
        try:
            return self.floors[(strategy.strategy_id, int(rare))]
        except KeyError as exc:
            raise GachaConfigError(
                f"no a_summonfloor for strategy={strategy.strategy_id}, rare={rare}"
            ) from exc

    def entries_for(
        self, pool_group: int, rare: int, up_type: Optional[int] = None
    ) -> Tuple[PoolEntry, ...]:
        entries = tuple(
            entry
            for entry in self.entries_by_pool.get(int(pool_group), ())
            if entry.rare == int(rare)
            and (up_type is None or entry.up_type == int(up_type))
        )
        return entries

    def ticket_cost(self, pool_group: int, draw_type: int, ten_draw_num: int) -> int:
        if draw_type == DRAW_SINGLE:
            return self.single_ticket_count
        if draw_type != DRAW_TEN:
            raise ValueError(f"unknown DrawCardType {draw_type}")
        ordinal = max(0, int(ten_draw_num)) + 1
        for rule in self.count_rules_by_pool.get(int(pool_group), ()):
            if rule.min_count <= ordinal <= rule.max_count:
                return rule.cost_count
        return self.ten_ticket_count


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
            field_varint(2, outcome.gospel_id),
            field_varint(3, 1),
            b"".join(
                field_message(4, make_reward_item(reward))
                for reward in outcome.conversions
            ),
            field_varint(5, 1 if outcome.is_new else 0),
        )
    )


def make_card_pool_group(group_id: int, state: Optional[Mapping[str, Any]]) -> bytes:
    state = _mapping(state)
    return b"".join(
        (
            field_varint(1, max(0, int(group_id))),
            field_varint(4, _nonnegative(state.get("star5_fail_draw_num"))),
            field_varint(5, _nonnegative(state.get("star4_fail_draw_num"))),
            field_varint(6, _nonnegative(state.get("star5_prob"))),
            field_varint(7, _nonnegative(state.get("star4_prob"))),
            field_varint(8, _nonnegative(state.get("star5_not_up_num"))),
            field_varint(9, _nonnegative(state.get("star4_not_up_num"))),
            field_varint(10, _nonnegative(state.get("flare_fail_draw_num"))),
            field_varint(11, _nonnegative(state.get("flare_prob"))),
        )
    )


def make_draw_gospel_ret(
    *,
    error_code: int,
    role_id: int,
    draw_type: int,
    card_pool_id: int,
    outcomes: Sequence[DrawOutcome] = (),
    single_drawn_num: int = 0,
    strategy_group: int = 0,
    group_state: Optional[Mapping[str, Any]] = None,
    draw_num: int = 0,
) -> bytes:
    """Encode the protocol_catalog SC_DrawGospelRet/DrawnStuff subset."""

    payload = bytearray(
        field_varint(1, max(0, int(error_code)))
        + field_varint(2, max(0, int(role_id)))
        + field_varint(3, max(0, int(draw_type)))
        + field_varint(4, max(0, int(card_pool_id)))
    )
    for outcome in outcomes:
        payload.extend(field_message(5, make_drawn_stuff(outcome)))
    payload.extend(field_varint(6, max(0, int(single_drawn_num))))
    if strategy_group > 0:
        payload.extend(
            field_message(8, make_card_pool_group(strategy_group, group_state))
        )
    payload.extend(field_varint(9, max(0, int(draw_num))))
    return bytes(payload)


class GachaEngine:
    """Configuration-backed, deterministic Gospel draw state machine."""

    def __init__(self, content_dir: Path = DEFAULT_CONTENT_DIR) -> None:
        self.catalog = GachaCatalog(content_dir)
        self.gospel_factory = GospelFactory(content_dir)

    @staticmethod
    def rate_for_failures(rule: FloorRule, failures: int) -> int:
        """Return the next R4/R5 probability on the configured 10,000 scale."""

        failures = max(0, int(failures))
        if rule.guaranteed and rule.rate_floor > 0 and failures >= rule.rate_floor - 1:
            return 10_000
        steps = max(0, failures - rule.soft_count)
        return min(rule.rate_max, rule.base_rate + steps * rule.rate_add_value)

    @staticmethod
    def limited_rate_for_failures(rule: LimitedRule, failures: int) -> int:
        """Return the next flare probability on the configured 10,000 scale.

        ``a_summon_limited.count`` is the number of completed misses before
        the first increased-rate draw, whereas ``a_summonfloor.count`` stores
        the last base-rate failure index.  They are separate defined table
        contracts, hence the intentional one-step difference here.
        """

        failures = max(0, int(failures))
        if rule.rate_floor > 0 and failures >= rule.rate_floor - 1:
            return 10_000
        steps = max(0, failures - rule.soft_count + 1)
        return min(rule.rate_max, rule.base_rate + steps * rule.rate_add_value)

    @staticmethod
    def _group_state(value: Any) -> Dict[str, int]:
        source = _mapping(value)
        return {name: _nonnegative(source.get(name)) for name in GROUP_COUNTER_FIELDS}

    @staticmethod
    def _pool_state(value: Any) -> Dict[str, int]:
        source = _mapping(value)
        result = {name: _nonnegative(source.get(name)) for name in POOL_COUNTER_FIELDS}
        result["is_valid"] = 1
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
    def _weighted_entry(entries: Sequence[PoolEntry], rng: CounterRng) -> PoolEntry:
        if not entries:
            raise GachaConfigError("weighted pool is empty")
        if len(entries) == 1:
            return entries[0]
        total = sum(entry.weight for entry in entries)
        if total <= 0:
            raise GachaConfigError("weighted pool has no positive weight")
        ticket = rng.randbelow(total)
        cursor = 0
        for entry in entries:
            cursor += entry.weight
            if ticket < cursor:
                return entry
        raise AssertionError("weighted selection fell through")

    def _select_entry(
        self,
        summon: SummonConfig,
        rare: int,
        floor_rule: FloorRule,
        group: Dict[str, int],
        rng: CounterRng,
        *, first_up: bool = False,
    ) -> Tuple[PoolEntry, bool]:
        entries = self.catalog.entries_for(summon.pool_group, rare)
        up_entries = tuple(entry for entry in entries if entry.up_type == 1)
        ordinary_entries = tuple(entry for entry in entries if entry.up_type == 0)
        if not up_entries:
            return self._weighted_entry(entries, rng), False

        not_up_key = "star5_not_up_num" if rare == 5 else "star4_not_up_num"
        force_up = first_up or (floor_rule.guaranteed and group[not_up_key] > 0
                    and not (summon.pool_group in PORTED_POOL_IDS and rare == 4))
        choose_up = force_up or rng.chance(floor_rule.up_rate)
        candidates = up_entries if choose_up else ordinary_entries
        if not candidates:
            candidates = up_entries if up_entries else entries
            choose_up = bool(up_entries)
        selected = self._weighted_entry(candidates, rng)
        return selected, selected.up_type == 1

    @staticmethod
    def _roll_normal_rarity(
        floor_5: FloorRule,
        floor_4: FloorRule,
        group: Mapping[str, int],
        rng: CounterRng,
        *, ported: bool = False,
    ) -> Tuple[int, bool, bool]:
        rate_5 = GachaEngine.rate_for_failures(
            floor_5, group["star5_fail_draw_num"]
        )
        rate_4 = GachaEngine.rate_for_failures(
            floor_4, group["star4_fail_draw_num"]
        )
        if ported and rate_4 < 10_000:
            # 2.0 Gacha.WeightAddValue = 55,-81,-9,90 on a 1,000
            # scale. Eight steps transfer probability from R/SR into SSR.
            steps = min(8, max(0, group["star5_fail_draw_num"] - 54))
            rate_4 = 1000 - 90 * steps
        hard_5 = rate_5 >= 10_000
        hard_4 = rate_4 >= 10_000
        if hard_5:
            return 5, True, hard_4
        roll = rng.randbelow(10_000)
        if roll < rate_5:
            return 5, False, hard_4
        if hard_4 or roll < min(10_000, rate_5 + rate_4):
            return 4, False, hard_4
        return 3, False, False

    def _materialize_gospel(
        self,
        gospel_id: int,
        gospels: List[Any],
        inventory: Dict[Any, Any],
        occupied_uids: set[int],
    ) -> Tuple[bool, Tuple[Reward, ...]]:
        index = next(
            (
                offset
                for offset, row in enumerate(gospels)
                if isinstance(row, Mapping) and _as_int(row.get("id"), -1) == gospel_id
            ),
            None,
        )
        if index is None:
            uid = max(2_000_000_000, max(occupied_uids, default=0)) + 1
            occupied_uids.add(uid)
            gospels.append(self.gospel_factory.create(gospel_id, uid))
            return True, ()

        source = gospels[index]
        row = dict(source) if isinstance(source, Mapping) else {"id": gospel_id}
        row["repeated_num"] = _nonnegative(row.get("repeated_num")) + 1
        gospels[index] = row
        conversions = self.catalog.repeat_rewards[gospel_id]
        overflow = self.catalog.repeat_over_rewards[gospel_id]
        if _nonnegative(row.get("awake_level")) >= 5 and overflow:
            conversions = overflow
        for reward in conversions:
            current = self._inventory_count(inventory, reward.item_id)
            self._set_inventory_count(inventory, reward.item_id, current + reward.count)
        return False, conversions

    def _failure(
        self,
        profile: Mapping[str, Any],
        *,
        error_code: int,
        role_id: int,
        draw_type: int,
        card_pool_id: int,
        reason: str,
    ) -> GachaResult:
        summon = self.catalog.summons_by_pool.get(int(card_pool_id))
        strategy_group = summon.strategy_group if summon is not None else 0
        groups = _mapping(profile.get("card_pool_groups"))
        group_state = self._group_state(_lookup(groups, strategy_group, {}))
        single_drawn_num = _nonnegative(profile.get("single_drawn_num"))
        draw_num = _nonnegative(profile.get("draw_num"))
        body = make_draw_gospel_ret(
            error_code=error_code,
            role_id=role_id,
            draw_type=draw_type,
            card_pool_id=card_pool_id,
            single_drawn_num=single_drawn_num,
            strategy_group=strategy_group,
            group_state=group_state,
            draw_num=draw_num,
        )
        return GachaResult(
            response_id=SC_DRAW_GOSPEL_RET,
            body=body,
            error_code=error_code,
            role_id=role_id,
            draw_type=draw_type,
            card_pool_id=card_pool_id,
            strategy_group=strategy_group,
            reason=reason,
        )

    def draw(
        self,
        profile: Optional[Mapping[str, Any]],
        draw_type: int,
        card_pool_id: int,
        *,
        role_id: Optional[int] = None,
    ) -> GachaResult:
        """Execute one single/ten draw without mutating ``profile``."""

        profile = profile or {}
        role_id = _nonnegative(
            profile.get("role_id") if role_id is None else role_id
        )
        draw_type = _as_int(draw_type, -1)
        card_pool_id = _as_int(card_pool_id, -1)
        if draw_type not in DRAW_COUNTS:
            return self._failure(
                profile,
                error_code=ERROR_CLIENT_REQUEST_PARAM,
                role_id=role_id,
                draw_type=max(0, draw_type),
                card_pool_id=max(0, card_pool_id),
                reason=f"unsupported DrawCardType {draw_type}",
            )
        summon = self.catalog.summons_by_pool.get(card_pool_id)
        if summon is None:
            orphan = card_pool_id in self.catalog.orphan_pool_groups
            return self._failure(
                profile,
                error_code=ERROR_CARD_POOL_NOT_FOUND,
                role_id=role_id,
                draw_type=draw_type,
                card_pool_id=max(0, card_pool_id),
                reason=("orphan" if orphan else "unknown")
                + f" summonpool_group {card_pool_id}",
            )

        pools_root = _mapping(profile.get("card_pools"))
        if (_mapping(profile.get("player_progression")).get("mode") == "newcomer-v1"
                and summon.open_type == 1 and summon.open_condition):
            prerequisite, count = map(int, summon.open_condition.split(","))
            source = self.catalog.summons_by_config.get(prerequisite)
            drawn = _nonnegative(_mapping(_lookup(pools_root, source.pool_group, {})).get("draw_num")) if source else 0
            if source is None or drawn < count:
                return self._failure(profile, error_code=ERROR_CARD_POOL_NOT_UNLOCKED,
                    role_id=role_id, draw_type=draw_type, card_pool_id=card_pool_id,
                    reason=f"pool requires {count} completed draws in {prerequisite}")
        pool = self._pool_state(_lookup(pools_root, card_pool_id, {}))
        result_count = DRAW_COUNTS[draw_type]
        if summon.highest_num and pool["draw_num"] + result_count > summon.highest_num:
            return self._failure(
                profile,
                error_code=ERROR_CARD_POOL_USE_REACHED_MAX,
                role_id=role_id,
                draw_type=draw_type,
                card_pool_id=card_pool_id,
                reason=f"pool {card_pool_id} exceeds highest_num={summon.highest_num}",
            )

        required_tickets = self.catalog.ticket_cost(
            card_pool_id, draw_type, pool["ten_draw_num"]
        )
        source_inventory = _mapping(profile.get("inventory"))
        ticket_balance = self._inventory_count(
            source_inventory, self.catalog.ticket_item_id
        )
        ticket_used = min(ticket_balance, required_tickets)
        missing_tickets = required_tickets - ticket_used
        minidiamond_used = missing_tickets * self.catalog.minidiamond_per_ticket
        minidiamond = _nonnegative(profile.get("minidiamond"))
        if minidiamond < minidiamond_used:
            return self._failure(
                profile,
                error_code=ERROR_MINIDIAMOND_NOT_ENOUGH,
                role_id=role_id,
                draw_type=draw_type,
                card_pool_id=card_pool_id,
                reason=(
                    f"need {required_tickets} ticket(s); {missing_tickets} missing cost "
                    f"{minidiamond_used} minidiamond"
                ),
            )

        # All validation and affordability checks happen before the first
        # copy/RNG step.  A failed transaction therefore returns no changes
        # and consumes no replay counter.
        inventory: Dict[Any, Any] = copy.deepcopy(dict(source_inventory))
        self._set_inventory_count(
            inventory, self.catalog.ticket_item_id, ticket_balance - ticket_used
        )
        groups: Dict[Any, Any] = copy.deepcopy(
            dict(_mapping(profile.get("card_pool_groups")))
        )
        pools: Dict[Any, Any] = copy.deepcopy(dict(pools_root))
        group = self._group_state(_lookup(groups, summon.strategy_group, {}))
        gospels = copy.deepcopy(
            list(profile.get("gospels"))
            if isinstance(profile.get("gospels"), list)
            else []
        )
        draw_history = copy.deepcopy(
            list(profile.get("draw_history"))
            if isinstance(profile.get("draw_history"), list)
            else []
        )
        occupied_uids = {
            _nonnegative(row.get("uid"))
            for key in ("heroes", "gospels")
            for row in (
                profile.get(key) if isinstance(profile.get(key), list) else []
            )
            if isinstance(row, Mapping) and _nonnegative(row.get("uid")) > 0
        }
        seed = _as_int(profile.get(RNG_SEED_KEY), role_id)
        rng = CounterRng(seed, _nonnegative(profile.get(RNG_COUNTER_KEY)))
        old_global_draw_num = _nonnegative(profile.get("draw_num"))
        old_single_drawn_num = _nonnegative(profile.get("single_drawn_num"))
        outcomes: List[DrawOutcome] = []

        for index in range(result_count):
            strategy = self.catalog.strategy_for(card_pool_id, pool["draw_num"])
            if pool["strategy_cfg_id"] != strategy.config_id:
                if strategy.reset_5:
                    group["star5_fail_draw_num"] = 0
                    group["star5_prob"] = 0
                if strategy.reset_4:
                    group["star4_fail_draw_num"] = 0
                    group["star4_prob"] = 0
                pool["strategy_cfg_id"] = strategy.config_id

            floor_5 = self.catalog.floors[(strategy.strategy_id, 5)]
            floor_4 = self.catalog.floors[(strategy.strategy_id, 4)]
            rate_5 = self.rate_for_failures(
                floor_5, group["star5_fail_draw_num"]
            )
            rate_4 = self.rate_for_failures(
                floor_4, group["star4_fail_draw_num"]
            )
            hard_5 = rate_5 >= 10_000
            hard_4 = rate_4 >= 10_000

            limited = self.catalog.limited_by_pool.get(card_pool_id)
            is_flare = False
            if limited is not None:
                flare_rate = self.limited_rate_for_failures(
                    limited, group["flare_fail_draw_num"]
                )
                is_flare = flare_rate >= 10_000 or rng.chance(flare_rate)

            if is_flare:
                assert limited is not None
                gospel_id = limited.gospel_id
                rare = _nonnegative(self.catalog.gospels[gospel_id].get("gospel_rare"), 5)
                is_up = False
                group["flare_fail_draw_num"] = 0

                # A flare is above ordinary R4 and therefore satisfies that
                # floor.  It does not consume ordinary R5 pity.  At the R5
                # hard boundary the counter is intentionally retained, so a
                # normal five-star is guaranteed on the immediately following
                # draw (including the next slot of the same ten-pull).
                if hard_4:
                    pool["star4_guaranteed_num"] += 1
                group["star4_fail_draw_num"] = 0
                if not hard_5:
                    group["star5_fail_draw_num"] += 1
            else:
                if limited is not None:
                    group["flare_fail_draw_num"] += 1
                rare, hard_5, hard_4 = self._roll_normal_rarity(
                    floor_5, floor_4, group, rng, ported=card_pool_id in PORTED_POOL_IDS
                )
                selected, is_up = self._select_entry(
                    summon,
                    rare,
                    floor_5 if rare == 5 else floor_4,
                    group,
                    rng,
                    first_up=(card_pool_id in PORTED_POOL_IDS and rare == 5 and not pool["first_star5_claimed"]),
                )
                gospel_id = selected.gospel_id
                if rare == 5:
                    if card_pool_id in PORTED_POOL_IDS:
                        pool["first_star5_claimed"] = 1
                    if hard_5:
                        pool["star5_guaranteed_num"] += 1
                    group["star5_fail_draw_num"] = 0
                    if self.catalog.entries_for(card_pool_id, 5, up_type=1):
                        group["star5_not_up_num"] = (
                            0 if is_up else group["star5_not_up_num"] + 1
                        )
                else:
                    group["star5_fail_draw_num"] += 1

                if rare >= 4:
                    if hard_4:
                        pool["star4_guaranteed_num"] += 1
                    group["star4_fail_draw_num"] = 0
                    if rare == 4 and self.catalog.entries_for(
                        card_pool_id, 4, up_type=1
                    ):
                        group["star4_not_up_num"] = (
                            0 if is_up else group["star4_not_up_num"] + 1
                        )
                else:
                    group["star4_fail_draw_num"] += 1

            group["star5_prob"] = self.rate_for_failures(
                floor_5, group["star5_fail_draw_num"]
            )
            group["star4_prob"] = self.rate_for_failures(
                floor_4, group["star4_fail_draw_num"]
            )
            if card_pool_id in PORTED_POOL_IDS and group["star4_prob"] < 10_000:
                group["star4_prob"] = 1000 - 90 * min(8, max(0, group["star5_fail_draw_num"] - 54))
            if limited is not None:
                group["flare_prob"] = self.limited_rate_for_failures(
                    limited, group["flare_fail_draw_num"]
                )

            is_new, conversions = self._materialize_gospel(
                gospel_id, gospels, inventory, occupied_uids
            )
            outcome = DrawOutcome(
                gospel_id=gospel_id,
                rare=rare,
                is_new=is_new,
                is_up=is_up,
                is_flare=is_flare,
                hard_pity_5=hard_5,
                hard_pity_4=hard_4,
                conversions=conversions,
            )
            outcomes.append(outcome)
            pool["draw_num"] += 1
            draw_history.append(
                {
                    "draw_num": old_global_draw_num + index + 1,
                    "card_pool_id": card_pool_id,
                    "summon_config_id": summon.config_id,
                    "strategy_group": summon.strategy_group,
                    "draw_type": draw_type,
                    "gospel_id": gospel_id,
                    "rare": rare,
                    "is_new": 1 if is_new else 0,
                    "is_up": 1 if is_up else 0,
                    "is_flare": 1 if is_flare else 0,
                    "rng_seed": seed,
                    "rng_counter": rng.counter,
                }
            )

        if draw_type == DRAW_TEN:
            pool["ten_draw_num"] += 1
        _store_indexed(groups, summon.strategy_group, group)
        _store_indexed(pools, card_pool_id, pool)
        new_draw_num = old_global_draw_num + result_count
        new_single_drawn_num = old_single_drawn_num + result_count
        changes: Dict[str, Any] = {
            "card_pool_groups": groups,
            "card_pools": pools,
            "draw_num": new_draw_num,
            "single_drawn_num": new_single_drawn_num,
            "draw_history": draw_history,
            "gospels": gospels,
            "inventory": inventory,
            "minidiamond": minidiamond - minidiamond_used,
            RNG_SEED_KEY: seed,
            RNG_COUNTER_KEY: rng.counter,
        }
        body = make_draw_gospel_ret(
            error_code=ERROR_SUCCESS,
            role_id=role_id,
            draw_type=draw_type,
            card_pool_id=card_pool_id,
            outcomes=outcomes,
            single_drawn_num=new_single_drawn_num,
            strategy_group=summon.strategy_group,
            group_state=group,
            draw_num=new_draw_num,
        )
        return GachaResult(
            response_id=SC_DRAW_GOSPEL_RET,
            body=body,
            changes=changes,
            role_id=role_id,
            draw_type=draw_type,
            card_pool_id=card_pool_id,
            strategy_group=summon.strategy_group,
            draws=tuple(outcomes),
            ticket_used=ticket_used,
            minidiamond_used=minidiamond_used,
        )

    def draw_request(
        self,
        profile: Optional[Mapping[str, Any]],
        request_body: bytes,
    ) -> GachaResult:
        profile = profile or {}
        try:
            fields = parse_fields(request_body)
        except ProtobufDecodeError as exc:
            return self._failure(
                profile,
                error_code=ERROR_CLIENT_REQUEST_PARAM,
                role_id=_nonnegative(profile.get("role_id")),
                draw_type=DRAW_SINGLE,
                card_pool_id=0,
                reason=str(exc),
            )
        draw_type = first_int(fields, 2, DRAW_SINGLE)
        pool_id = first_int(fields, 3, 0)
        active = _mapping(_mapping(profile.get("battle_runtime")).get("active"))
        pending_tutorial = (
            _mapping(profile.get("player_progression")).get("mode") == "newcomer-v1"
            and draw_type == DRAW_SINGLE and pool_id == 2
            and (active.get("dungeon_id") == 8600012 or (
                profile.get("tutorial_draw") and not profile.get("tutorial_completed")
                and profile.get("last_dungeon_id") == 8600012))
        )
        if pending_tutorial:
            return self._tutorial_draw(profile)
        return self.draw(
            profile,
            draw_type,
            pool_id,
            role_id=first_int(fields, 1, _nonnegative(profile.get("role_id"))),
        )

    def _tutorial_draw(self, profile):
        """One source tutorial draw, with the deferred a_initial starter gift.

        The offline zero-owned opening defers the starter Gospel until this
        transaction. It is not counted as another draw. The source tutorial
        target is a_global[9990251], also required by its formation guide.
        """
        gid = int(self.catalog.globals[9990251]['int_value'])
        initial_ids = json.loads(self.catalog._records('a_initial')[0]['gospel'])
        rows = copy.deepcopy(profile.get("gospels", []))
        receipt = profile.get("tutorial_draw")
        current = next((r for r in rows if r["id"] == gid), None)
        is_new = current is None
        changes = {}
        count = _nonnegative(profile.get("draw_num"))
        singles = _nonnegative(profile.get("single_drawn_num"))
        if not receipt:
            granted_initial = []
            for initial_id in initial_ids:
                if not any(r['id']==initial_id for r in rows):
                    occupied = [r['uid'] for r in rows] + [r['uid'] for r in profile.get('heroes',[])]
                    rows.append(self.gospel_factory.create(initial_id,max([2_000_000_000,*occupied])+1))
                    granted_initial.append(initial_id)
            if current is None:
                occupied = [r['uid'] for r in rows] + [r['uid'] for r in profile.get('heroes',[])]
                current = self.gospel_factory.create(gid, max([2_000_000_000, *occupied]) + 1)
                rows.append(current)
            count += 1; singles += 1
            history = copy.deepcopy(profile.get("draw_history", []))
            history.append({"draw_num":count,"gospel_id":gid,"gospel_uid":current["uid"],
                            "card_pool_id":2,"source":"tutorial","is_new":int(is_new)})
            changes = {"gospels":rows,"draw_num":count,"single_drawn_num":singles,
                       "draw_history":history,"tutorial_draw":{"gospel_id":gid,"gospel_uid":current["uid"]},
                       "tutorial_initial_gifts":{"source":"a_initial.gospel","gospel_ids":granted_initial}}
        else:
            is_new = False
        outcome = DrawOutcome(gid, int(self.catalog.gospels[gid]["gospel_rare"]), is_new)
        role_id = _nonnegative(profile.get("role_id"))
        body = make_draw_gospel_ret(error_code=0, role_id=role_id, draw_type=0,card_pool_id=2,
            outcomes=(outcome,),single_drawn_num=singles,draw_num=count,strategy_group=2,
            group_state=_lookup(profile.get("card_pool_groups",{}),2,{}))
        return GachaResult(40,body,changes,role_id=role_id,card_pool_id=2,strategy_group=2,draws=(outcome,))

    def handle(
        self,
        request_id: int,
        profile: Optional[Mapping[str, Any]],
        request_body: bytes = b"",
    ) -> Optional[GachaResult]:
        if int(request_id) != CS_DRAW_GOSPEL:
            return None
        return self.draw_request(profile, request_body)


@lru_cache(maxsize=1)
def default_engine() -> GachaEngine:
    return GachaEngine()


def draw_gospel(
    profile: Optional[Mapping[str, Any]],
    draw_type: int,
    card_pool_id: int,
    *,
    role_id: Optional[int] = None,
) -> GachaResult:
    return default_engine().draw(
        profile, draw_type, card_pool_id, role_id=role_id
    )


def handle_gacha_request(
    request_id: int,
    profile: Optional[Mapping[str, Any]],
    request_body: bytes = b"",
) -> Optional[GachaResult]:
    return default_engine().handle(request_id, profile, request_body)
