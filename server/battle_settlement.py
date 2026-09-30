"""Table-backed local battle admission and settlement transactions.

The legacy compatibility path accepted ``CS_EndBattle`` without a matching
start and granted a hard-coded ten Gold.  This module instead treats one
battle as a durable session and derives admission cost and loot from
``a_dungeon`` plus ``a_drop_item``.  It is deliberately independent from the
socket handler: callers evaluate it inside ``PersistentStateStore``'s account
transaction, apply the returned top-level changes, and only then acknowledge
the runtime.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

try:
    from .gospel_acquisition import GospelFactory
    from .collectibles import CollectiblesLayer, RUNE_CREATE_TIME_BASE, RUNE_UID_BASE
    from .content_profile import ContentCatalog
    from .inventory_content import normalize_inventory, stable_item_uid
    from .offline_economy import EconomyError, deduct_resources
except ImportError:  # pragma: no cover - direct module execution
    from gospel_acquisition import GospelFactory
    from collectibles import (  # type: ignore
        CollectiblesLayer,
        RUNE_CREATE_TIME_BASE,
        RUNE_UID_BASE,
    )
    from content_profile import ContentCatalog  # type: ignore
    from inventory_content import normalize_inventory, stable_item_uid  # type: ignore
    from offline_economy import EconomyError, deduct_resources


ERROR_SUCCESS = 0
ERROR_ENERGY_NOT_ENOUGH = 109
ERROR_CLIENT_REQUEST_PARAM = 32000
ERROR_CONFIG = 32772

RESOURCE_DIAMOND = 0
RESOURCE_MINIDIAMOND = 1
RESOURCE_GOLD = 2
RESOURCE_ENERGY = 3
RESOURCE_ITEM = 4
RESOURCE_EXP = 5
RESOURCE_HERO = 6
RESOURCE_GOSPEL = 7
RESOURCE_HERO_EXP = 8
RESOURCE_GOSPEL_EXP = 9
RESOURCE_FOOD = 11
RESOURCE_GOSPEL_FONDNESS = 13
RESOURCE_RUNE = 25

UINT32_MAX = (1 << 32) - 1
UINT64_MAX = (1 << 64) - 1
BATTLE_STATE_KEY = "battle_runtime"
LOG = logging.getLogger(__name__)


class BattleCatalogError(ValueError):
    """Raised when defined static battle data is malformed."""


@dataclass(frozen=True)
class Reward:
    res_type: int
    item_id: int
    count: int
    para: int = 0
    position: int = 0
    quality: int = 0
    value: int = 0

    def as_dict(self) -> Dict[str, int]:
        return {
            "res_type": self.res_type,
            "item_id": self.item_id,
            "count": self.count,
            "para": self.para,
            "position": self.position,
            "quality": self.quality,
            "value": self.value,
        }


@dataclass(frozen=True)
class DropEntry:
    row_id: int
    drop_id: int
    ratio: int
    rewards: Tuple[Reward, ...]


@dataclass(frozen=True)
class StarCondition:
    condition_type: int
    target_id: int
    count: int


@dataclass(frozen=True)
class Dungeon:
    dungeon_id: int
    energy: int
    energy_first: int
    fixed_rewards: Tuple[Reward, ...]
    first_rewards: Tuple[Reward, ...]
    star_condition_ids: Tuple[int, ...]
    star_rewards: Tuple[Reward, ...]
    drop_ids: Tuple[int, ...]
    missing_drop_ids: Tuple[int, ...]
    next_dungeons: Tuple[int, ...]
    extra_costs: Tuple[Reward, ...] = ()
    extra_drop_ids: Tuple[int, ...] = ()


@dataclass(frozen=True)
class StartResult:
    changes: Dict[str, Any] = field(default_factory=dict)
    error_code: int = ERROR_SUCCESS
    session_id: int = 0
    energy_cost: int = 0
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.error_code == ERROR_SUCCESS


@dataclass(frozen=True)
class SettlementResult:
    changes: Dict[str, Any] = field(default_factory=dict)
    error_code: int = ERROR_SUCCESS
    fixed_rewards: Tuple[Reward, ...] = ()
    first_rewards: Tuple[Reward, ...] = ()
    random_rewards: Tuple[Reward, ...] = ()
    star_rewards: Tuple[Reward, ...] = ()
    achieved_conditions: Tuple[int, ...] = ()
    star: int = 0
    missing_drop_ids: Tuple[int, ...] = ()
    is_first_pass: bool = False
    replayed: bool = False
    session_id: int = 0
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.error_code == ERROR_SUCCESS


def _strict_uint(value: Any, label: str, maximum: int = UINT32_MAX) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise BattleCatalogError(f"{label} must be an integer")
    if value < 0 or value > maximum:
        raise BattleCatalogError(f"{label} is outside 0..{maximum}")
    return value


def _state_uint(value: Any, default: int = 0) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return max(0, parsed)


def _json_list(value: Any, label: str) -> List[Any]:
    if value in (None, ""):
        return []
    if isinstance(value, list):
        return value
    if not isinstance(value, str):
        raise BattleCatalogError(f"{label} must be JSON text")
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise BattleCatalogError(f"{label} is invalid JSON") from exc
    if not isinstance(parsed, list):
        raise BattleCatalogError(f"{label} must decode to a list")
    return parsed


def _rewards(value: Any, label: str) -> Tuple[Reward, ...]:
    result: List[Reward] = []
    for index, raw in enumerate(_json_list(value, label)):
        if not isinstance(raw, Mapping):
            raise BattleCatalogError(f"{label}[{index}] must be an object")
        result.append(
            Reward(
                _strict_uint(raw.get("type"), f"{label}[{index}].type"),
                _strict_uint(raw.get("id"), f"{label}[{index}].id"),
                _strict_uint(raw.get("num", 1), f"{label}[{index}].num"),
                _strict_uint(raw.get("para", 0), f"{label}[{index}].para"),
                _strict_uint(raw.get("position", 0), f"{label}[{index}].position"),
                _strict_uint(raw.get("quality", 0), f"{label}[{index}].quality"),
                _strict_uint(raw.get("value", 0), f"{label}[{index}].value"),
            )
        )
        if result[-1].count <= 0:
            raise BattleCatalogError(f"{label}[{index}].num must be positive")
    return tuple(result)


def _star_conditions(value: Any, label: str) -> Tuple[StarCondition, ...]:
    result: List[StarCondition] = []
    for index, raw in enumerate(_json_list(value, label)):
        if not isinstance(raw, Mapping):
            raise BattleCatalogError(f"{label}[{index}] must be an object")
        result.append(
            StarCondition(
                _strict_uint(raw.get("type"), f"{label}[{index}].type"),
                _strict_uint(raw.get("id", 0), f"{label}[{index}].id"),
                _strict_uint(raw.get("count", 0), f"{label}[{index}].count"),
            )
        )
    return tuple(result)


def _ids(value: Any, key: str, label: str) -> Tuple[int, ...]:
    result = []
    for index, raw in enumerate(_json_list(value, label)):
        if isinstance(raw, Mapping):
            raw = raw.get(key)
        result.append(_strict_uint(raw, f"{label}[{index}].{key}"))
    return tuple(item for item in result if item)


def _plain_ids(value: Any, label: str) -> Tuple[int, ...]:
    if value in (None, ""):
        values: List[Any] = []
    elif isinstance(value, list):
        values = value
    elif isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise BattleCatalogError(f"{label} is invalid JSON") from exc
        values = parsed if isinstance(parsed, list) else [parsed]
    else:
        values = [value]
    return tuple(
        item
        for item in (
            _strict_uint(raw, f"{label}[{index}]") for index, raw in enumerate(values)
        )
        if item
    )


def aggregate(rewards: Iterable[Reward]) -> Tuple[Reward, ...]:
    order: List[Tuple[int, int, int, int, int, int]] = []
    totals: Dict[Tuple[int, int, int, int, int, int], int] = {}
    for reward in rewards:
        key = (
            reward.res_type,
            reward.item_id,
            reward.para,
            reward.position,
            reward.quality,
            reward.value,
        )
        if key not in totals:
            order.append(key)
            totals[key] = 0
        totals[key] += reward.count
        if totals[key] > UINT32_MAX:
            raise BattleCatalogError("aggregated reward exceeds uint32")
    return tuple(
        Reward(kind, item, totals[key], para, position, quality, value)
        for key in order
        for kind, item, para, position, quality, value in (key,)
    )


class BattleSettlementLayer:
    """Validated, deterministic dungeon settlement model."""

    def __init__(self, catalog: ContentCatalog, *, normal_economy: bool = False) -> None:
        self.catalog = catalog
        self.gospel_factory = GospelFactory(catalog.root)
        self.normal_economy = bool(normal_economy)
        dungeon_table = catalog.table("a_dungeon")
        drop_table = catalog.table("a_drop_item")
        item_table = catalog.table("a_item")
        gospel_table = catalog.table("a_gospel")
        people_table = catalog.table("a_people")
        star_table = catalog.table("a_star_level")

        self.item_max = {
            int(row["id"]): max(0, _state_uint(row.get("max_stack_num")))
            for row in item_table.records
        }
        self.gospel_ids = frozenset(int(row["id"]) for row in gospel_table.records)
        self.hero_ids = frozenset(int(row["id"]) for row in people_table.records)
        self.favor_rows = {}
        for row in catalog.table("a_favor_gospel").records:
            self.favor_rows.setdefault(int(row["gospel_id"]),[]).append(row)
        for rows in self.favor_rows.values():rows.sort(key=lambda r:int(r["level"]))
        self.spirit_exp_per_level = int(catalog.table("a_global").by_id[9990230]["int_value"])
        self.star_conditions: Dict[int, Tuple[StarCondition, ...]] = {
            int(row["id"]): _star_conditions(
                row.get("star_condition"),
                f"a_star_level[{row.get('id')}].star_condition",
            )
            for row in star_table.records
        }

        # Rune rewards identify a suit plus quality, while owned RuneItem rows
        # identify one concrete suit/quality/position configuration.  Build the
        # concrete templates through CollectiblesLayer itself so battle drops
        # cannot drift from its 17-field RuneItem structure, deterministic
        # attributes, or UID namespace.
        collectibles = CollectiblesLayer(catalog)
        seeded_collectibles: Dict[str, Any] = {
            "heroes": [],
            "gospels": [],
            "rune_items": [],
        }
        collectibles.seed_profile(seeded_collectibles)
        seeded_runes = seeded_collectibles.get("rune_items", [])
        self.rune_templates: Dict[int, Dict[str, Any]] = {
            _state_uint(row.get("id")): copy.deepcopy(dict(row))
            for row in seeded_runes
            if isinstance(row, Mapping) and _state_uint(row.get("id")) > 0
        }
        self.rune_static: Dict[int, Dict[str, Any]] = {
            _state_uint(row.get("id")): dict(row)
            for row in collectibles.rune_rows
            if _state_uint(row.get("id")) > 0
        }
        rune_candidates: Dict[Tuple[int, int], List[Tuple[int, int]]] = {}
        for rune_id, row in self.rune_static.items():
            key = (_state_uint(row.get("suit")), _state_uint(row.get("quality")))
            rune_candidates.setdefault(key, []).append(
                (_state_uint(row.get("position")), rune_id)
            )
        self.rune_candidates: Dict[Tuple[int, int], Tuple[int, ...]] = {
            key: tuple(
                rune_id for _position, rune_id in sorted(rows, key=lambda value: value)
            )
            for key, rows in rune_candidates.items()
        }
        if set(self.rune_templates) != set(self.rune_static):
            raise BattleCatalogError(
                "collectible rune templates do not cover every a_rune_main row"
            )

        drops: Dict[int, List[DropEntry]] = {}
        for row in drop_table.records:
            drop_id = _strict_uint(row.get("drop_id"), "a_drop_item.drop_id")
            entry = DropEntry(
                row_id=_strict_uint(row.get("id"), "a_drop_item.id"),
                drop_id=drop_id,
                ratio=_strict_uint(row.get("ratio"), "a_drop_item.ratio"),
                rewards=_rewards(
                    row.get("drop_content"),
                    f"a_drop_item[{row.get('id')}].drop_content",
                ),
            )
            drops.setdefault(drop_id, []).append(entry)
        self.drops = {
            drop_id: tuple(sorted(entries, key=lambda entry: entry.row_id))
            for drop_id, entries in drops.items()
        }
        for entries in self.drops.values():
            for entry in entries:
                for reward in entry.rewards:
                    if (
                        reward.res_type == RESOURCE_RUNE
                        and not self._rune_candidate_ids(reward)
                    ):
                        raise BattleCatalogError(
                            "rune drop has no a_rune_main mapping: "
                            f"entry={entry.row_id} suit_or_id={reward.item_id} "
                            f"quality={reward.quality} position={reward.position}"
                        )

        dungeons: Dict[int, Dungeon] = {}
        for row in dungeon_table.records:
            dungeon_id = _strict_uint(row.get("id"), "a_dungeon.id")
            star_condition_ids = _plain_ids(
                row.get("star_level"),
                f"a_dungeon[{dungeon_id}].star_level",
            )
            missing_star_conditions = tuple(
                condition_id
                for condition_id in star_condition_ids
                if condition_id not in self.star_conditions
            )
            if missing_star_conditions:
                raise BattleCatalogError(
                    f"a_dungeon[{dungeon_id}] references missing star conditions "
                    f"{missing_star_conditions}"
                )
            drop_ids = _ids(
                row.get("drop_id"),
                "drop_id",
                f"a_dungeon[{dungeon_id}].drop_id",
            )
            extra_drop_ids = _ids(
                row.get("extra_drop"),
                "drop_id",
                f"a_dungeon[{dungeon_id}].extra_drop",
            )
            extra_costs = _rewards(row.get("extra_consume"), f"a_dungeon[{dungeon_id}].extra_consume")
            if extra_costs and (int(row.get("belong_chapte", 0)) != 3939012 or
                                any((r.res_type, r.item_id, r.count) != (4, 1001049, 1) for r in extra_costs)):
                raise BattleCatalogError(f"unproved extra-cost policy for dungeon {dungeon_id}")
            if not self.normal_economy:
                drop_ids += extra_drop_ids
            missing_drop_ids = tuple(
                drop_id for drop_id in set((*drop_ids, *extra_drop_ids)) if drop_id not in self.drops
            )
            dungeons[dungeon_id] = Dungeon(
                dungeon_id=dungeon_id,
                energy=_strict_uint(row.get("energy", 0), "a_dungeon.energy"),
                energy_first=_strict_uint(
                    row.get("energy_first", 0), "a_dungeon.energy_first"
                ),
                fixed_rewards=_rewards(
                    row.get("rwadids_exp"),
                    f"a_dungeon[{dungeon_id}].rwadids_exp",
                ),
                first_rewards=_rewards(
                    row.get("first_reward"),
                    f"a_dungeon[{dungeon_id}].first_reward",
                ),
                star_condition_ids=star_condition_ids,
                star_rewards=_rewards(
                    row.get("star_level_reward"),
                    f"a_dungeon[{dungeon_id}].star_level_reward",
                ),
                drop_ids=drop_ids,
                missing_drop_ids=missing_drop_ids,
                extra_costs=extra_costs,
                extra_drop_ids=extra_drop_ids,
                next_dungeons=_plain_ids(
                    row.get("next_dungeon"),
                    f"a_dungeon[{dungeon_id}].next_dungeon",
                ),
            )
        self.dungeons = dungeons
        supported_star_types = {3, 4, 8, 22}
        unsupported_star_conditions = {
            condition_id: sorted(
                {
                    condition.condition_type
                    for condition in self.star_conditions[condition_id]
                    if condition.condition_type not in supported_star_types
                }
            )
            for dungeon in dungeons.values()
            for condition_id in dungeon.star_condition_ids
            if self.star_conditions[condition_id]
            and any(
                condition.condition_type not in supported_star_types
                for condition in self.star_conditions[condition_id]
            )
        }
        if unsupported_star_conditions:
            raise BattleCatalogError(
                "referenced star-condition types are unsupported: "
                f"{unsupported_star_conditions}"
            )
        self.missing_drop_references = {
            dungeon_id: dungeon.missing_drop_ids
            for dungeon_id, dungeon in dungeons.items()
            if dungeon.missing_drop_ids
        }

    def _rune_candidate_ids(self, reward: Reward) -> Tuple[int, ...]:
        """Resolve a rune reward to concrete static RuneItem identifiers."""

        direct = self.rune_static.get(reward.item_id)
        if direct is not None and reward.quality in (
            0,
            _state_uint(direct.get("quality")),
        ):
            candidates = (reward.item_id,)
        else:
            candidates = self.rune_candidates.get((reward.item_id, reward.quality), ())
        if reward.position:
            candidates = tuple(
                rune_id
                for rune_id in candidates
                if _state_uint(self.rune_static[rune_id].get("position"))
                == reward.position
            )
        return candidates

    @staticmethod
    def _star_condition_achieved(
        condition: StarCondition,
        battle_stats: Mapping[str, int],
    ) -> bool:
        """Evaluate the star-condition types used by defined dungeons.

        ``a_star_level`` is now extracted from the same pinned TableConfig as
        ``a_dungeon``.  Every non-empty condition referenced by the current
        dungeon catalog uses one of these four runtime-verified comparisons.
        """

        value_by_type = {
            3: _state_uint(battle_stats.get("be_attacked_num")),
            4: _state_uint(battle_stats.get("death_num")),
            8: _state_uint(battle_stats.get("round_num")),
            22: _state_uint(battle_stats.get("round_max_harm")),
        }
        if condition.condition_type not in value_by_type:
            return False
        value = value_by_type[condition.condition_type]
        if condition.condition_type in (3, 8):
            return value <= condition.count
        if condition.condition_type == 4:
            return value < condition.count
        return value > condition.count  # type 22

    def achieved_star_conditions(
        self,
        dungeon_id: int,
        battle_stats: Optional[Mapping[str, int]],
    ) -> Tuple[int, ...]:
        """Return configured condition IDs achieved by one reported battle."""

        dungeon = self.dungeons.get(int(dungeon_id))
        if dungeon is None or battle_stats is None:
            return ()
        achieved: List[int] = []
        for condition_id in dungeon.star_condition_ids:
            conditions = self.star_conditions.get(condition_id, ())
            # Empty conditions belong to score/task-driven special modes and
            # cannot be inferred from a generic CS_EndBattle payload.
            if conditions and all(
                self._star_condition_achieved(condition, battle_stats)
                for condition in conditions
            ):
                achieved.append(condition_id)
        return tuple(achieved)

    @staticmethod
    def _owned_entity_uids(
        profile: Mapping[str, Any], rune_items: Sequence[Any]
    ) -> set[int]:
        """Mirror CollectiblesLayer's shared hero/gospel/rune UID namespace."""

        used: set[int] = set()
        for key in ("heroes", "gospels"):
            rows = profile.get(key, [])
            if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
                continue
            for row in rows:
                if not isinstance(row, Mapping):
                    continue
                uid = _state_uint(
                    row.get("uid", row.get("HeroUId", row.get("hero_uid")))
                )
                if uid > 0:
                    used.add(uid)
        for row in rune_items:
            if not isinstance(row, Mapping):
                continue
            uid = _state_uint(
                row.get(
                    "rune_item_uid",
                    row.get("uid", row.get("RuneItemUId")),
                )
            )
            if uid > 0:
                used.add(uid)
        return used

    @staticmethod
    def _successful_before(profile: Mapping[str, Any], dungeon_id: int) -> bool:
        completed = profile.get("completed_dungeons", [])
        if isinstance(completed, Sequence) and not isinstance(completed, (str, bytes)):
            if any(_state_uint(value) == dungeon_id for value in completed):
                return True
        battles = profile.get("completed_battles", {})
        if isinstance(battles, Mapping):
            previous = battles.get(str(dungeon_id), battles.get(dungeon_id))
            if isinstance(previous, Mapping):
                return bool(_state_uint(previous.get("result")))
        return False

    @staticmethod
    def _runtime(profile: Mapping[str, Any]) -> Dict[str, Any]:
        source = profile.get(BATTLE_STATE_KEY, {})
        return copy.deepcopy(dict(source)) if isinstance(source, Mapping) else {}

    def start(
        self,
        profile: Mapping[str, Any],
        *,
        dungeon_id: int,
        role_id: int,
        target_id: int = 0,
        now: int | None = None,
    ) -> StartResult:
        dungeon = self.dungeons.get(int(dungeon_id))
        if dungeon is None:
            return StartResult(
                error_code=ERROR_CONFIG,
                reason=f"unknown dungeon {dungeon_id}",
            )
        runtime = self._runtime(profile)
        active = runtime.get("active")
        if isinstance(active, Mapping) and not active.get("settled"):
            if (
                _state_uint(active.get("dungeon_id")) == dungeon_id
                and _state_uint(active.get("target_id")) == target_id
            ):
                return StartResult(
                    session_id=_state_uint(active.get("session_id")),
                    energy_cost=_state_uint(active.get("energy_cost")),
                    reason="replayed active battle start",
                )

        first_pass = not self._successful_before(profile, dungeon_id)
        energy_cost = dungeon.energy_first if first_pass else dungeon.energy
        extra_costs = ()
        deductions = {}
        if self.normal_economy and dungeon.extra_costs:
            # BattleDetailBoardSide selects tickets automatically when held:
            # a ticket replaces energy and enables extra_drop for this run.
            # With no ticket, the same source button uses ordinary energy.
            try:
                deductions = deduct_resources(profile, ((r.res_type, r.item_id, r.count) for r in dungeon.extra_costs))
                extra_costs = dungeon.extra_costs
                energy_cost = 0
            except EconomyError as exc:
                if exc.error_code != 110:
                    return StartResult(error_code=exc.error_code, reason=str(exc))
        energy = _state_uint(profile.get("energy"))
        if energy_cost > energy:
            return StartResult(
                error_code=ERROR_ENERGY_NOT_ENOUGH,
                energy_cost=energy_cost,
                reason="account energy is below dungeon admission cost",
            )

        session_id = _state_uint(runtime.get("next_session_id"), 1)
        if session_id <= 0:
            session_id = 1
        runtime["next_session_id"] = session_id + 1
        runtime["active"] = {
            "session_id": session_id,
            "role_id": int(role_id),
            "target_id": int(target_id),
            "dungeon_id": int(dungeon_id),
            "energy_cost": energy_cost,
            "extra_costs": [r.as_dict() for r in extra_costs],
            "extra_drop_enabled": bool(extra_costs),
            "started_at": int(now if now is not None else time.time()),
            "settled": False,
        }
        if profile.get("player_progression",{}).get("mode") == "newcomer-v1":
            formation=profile.get("formation_state",{})
            slots=formation.get("phalanxes",[])
            selected=int(formation.get("current_phalanx_id",0))
            slot=slots[selected] if 0 <= selected < len(slots) else {}
            owned={int(g["uid"]) for g in profile.get("gospels",[])}
            active_heroes={str(uid) for uid,pos in slot.get("combat_positions",{}).items() if pos in (0,1,2)}
            runtime["active"]["gospel_uids"] = sorted({int(g["gospel_uid"])
                for uid,rows in slot.get("equipped_gospels",{}).items() if str(uid) in active_heroes
                for g in rows if int(g.get("gospel_uid",0)) in owned})
        changes = {**deductions, "energy": energy - energy_cost, BATTLE_STATE_KEY: runtime,
                   "last_dungeon_id": int(dungeon_id)}
        if self.normal_economy:
            costs = [r.as_dict() for r in extra_costs]
            if energy_cost:
                costs.append(Reward(RESOURCE_ENERGY, 0, energy_cost).as_dict())
            changes["resource_history"] = [*copy.deepcopy(profile.get("resource_history", [])), {
                "source": "battle_admission", "transaction_id": f"battle-start:{session_id}",
                "dungeon_id": dungeon_id, "costs": costs, "rewards": []}]
        return StartResult(
            changes=changes,
            session_id=session_id,
            energy_cost=energy_cost,
        )

    @staticmethod
    def _roll(entry: DropEntry, seed: str) -> bool:
        if entry.ratio >= 10_000:
            return True
        digest = hashlib.sha256(f"{seed}:{entry.row_id}".encode("utf-8")).digest()
        return int.from_bytes(digest[:8], "big") % 10_000 < entry.ratio

    def _random_rewards(
        self, dungeon: Dungeon, *, role_id: int, run_number: int, session_id: int,
        extra_drop_enabled: bool = False,
    ) -> Tuple[Reward, ...]:
        selected: List[Reward] = []
        seed = f"{role_id}:{dungeon.dungeon_id}:{run_number}:{session_id}"
        drop_ids = dungeon.drop_ids + (dungeon.extra_drop_ids if self.normal_economy and extra_drop_enabled else ())
        for drop_id in drop_ids:
            for entry in self.drops.get(drop_id, ()):
                if self._roll(entry, seed):
                    selected.extend(entry.rewards)
        return aggregate(selected)

    def _grant_changes(
        self,
        profile: Mapping[str, Any],
        rewards: Sequence[Reward],
        *, battle_favor_uids: Optional[Sequence[int]] = None, favor_day: int = 0,
    ) -> Dict[str, Any]:
        changes: Dict[str, Any] = {}
        inventory = normalize_inventory(copy.deepcopy(profile.get("inventory", {})))
        balances_source = profile.get("resource_balances", {})
        balances = (
            copy.deepcopy(dict(balances_source))
            if isinstance(balances_source, Mapping)
            else {}
        )
        heroes_source = profile.get("heroes", [])
        heroes = copy.deepcopy(heroes_source) if isinstance(heroes_source, list) else []
        gospels_source = profile.get("gospels", [])
        gospels = (
            copy.deepcopy(gospels_source) if isinstance(gospels_source, list) else []
        )
        foods_source = profile.get("food_inventory", {})
        foods = (
            copy.deepcopy(dict(foods_source))
            if isinstance(foods_source, Mapping)
            else {}
        )
        rune_items_source = profile.get("rune_items", [])
        rune_items = (
            copy.deepcopy(list(rune_items_source))
            if isinstance(rune_items_source, Sequence)
            and not isinstance(rune_items_source, (str, bytes))
            else []
        )
        used_entity_uids = self._owned_entity_uids(profile, rune_items)
        next_rune_uid = RUNE_UID_BASE
        existing_create_times = [
            _state_uint(row.get("create_time"))
            for row in rune_items
            if isinstance(row, Mapping)
        ]
        next_rune_create_time = max(
            RUNE_CREATE_TIME_BASE,
            max(existing_create_times, default=RUNE_CREATE_TIME_BASE - 1) + 1,
        )
        rune_id_counts: Dict[int, int] = {}
        for row in rune_items:
            if isinstance(row, Mapping):
                rune_id = _state_uint(row.get("id"))
                if rune_id in self.rune_templates:
                    rune_id_counts[rune_id] = rune_id_counts.get(rune_id, 0) + 1

        inventory_changed = balances_changed = heroes_changed = False
        gospels_changed = foods_changed = runes_changed = False
        scalar_values = {
            "diamond": _state_uint(profile.get("diamond")),
            "minidiamond": _state_uint(profile.get("minidiamond")),
            "gold": _state_uint(profile.get("gold")),
            "energy": _state_uint(profile.get("energy")),
            "exp": _state_uint(profile.get("exp")),
        }
        scalar_changed: set[str] = set()

        for reward in aggregate(rewards):
            if reward.res_type == RESOURCE_GOSPEL_FONDNESS and reward.item_id == 1 and battle_favor_uids is not None:
                for row in gospels:
                    if int(row["uid"]) not in battle_favor_uids:continue
                    old=int(row.get("fondness",0));old_level=int(row.get("fondness_level",0))
                    if row.get("is_fondness_limit_breakthrough"):
                        limit=int(row.get("spirit_level_limit",old_level))
                        room=max(0,(limit-old_level)*self.spirit_exp_per_level-old)
                        earned=min(room,reward.count)
                        value=old+earned
                        row["fondness_level"]=min(limit,old_level+value//self.spirit_exp_per_level)
                        row["fondness"]=value%self.spirit_exp_per_level
                    else:
                        levels=self.favor_rows.get(int(row["id"]),[])
                        if not levels:raise BattleCatalogError("Missing Gospel favor source")
                        earned=min(max(0,int(levels[-1]["exp"])-old),reward.count)
                        row["fondness"]=old+earned
                        row["fondness_level"]=max(int(r["level"]) for r in levels if int(r["exp"])<=row["fondness"])
                    row["fondness_daily_increment"]=(int(row.get("fondness_daily_increment",0)) if row.get("fondness_day")==favor_day else 0)+earned
                    row["fondness_day"]=favor_day
                    gospels_changed=True
                continue
            scalar = {
                RESOURCE_DIAMOND: "diamond",
                RESOURCE_MINIDIAMOND: "minidiamond",
                RESOURCE_GOLD: "gold",
                RESOURCE_ENERGY: "energy",
                RESOURCE_EXP: "exp",
            }.get(reward.res_type)
            if scalar is not None and reward.item_id == 0:
                scalar_values[scalar] = min(
                    UINT32_MAX, scalar_values[scalar] + reward.count
                )
                scalar_changed.add(scalar)
                continue

            if reward.res_type == RESOURCE_ITEM and reward.item_id in self.item_max:
                maximum = self.item_max[reward.item_id]
                key = str(reward.item_id)
                previous = inventory.get(key, {})
                current = _state_uint(previous.get("count"))
                updated_count = min(maximum, current + reward.count)
                inventory[key] = {
                    "uid": _state_uint(previous.get("uid"))
                    or stable_item_uid(reward.item_id),
                    "id": reward.item_id,
                    "count": updated_count,
                    "para": _state_uint(previous.get("para")),
                }
                inventory_changed = inventory_changed or updated_count != current
                continue

            if reward.res_type == RESOURCE_HERO and reward.item_id in self.hero_ids:
                existing = next(
                    (
                        row
                        for row in heroes
                        if isinstance(row, Mapping)
                        and _state_uint(row.get("id")) == reward.item_id
                    ),
                    None,
                )
                if existing is None:
                    occupied = [
                        _state_uint(row.get("uid"))
                        for row in heroes
                        if isinstance(row, Mapping)
                    ]
                    heroes.append(
                        {
                            "uid": max(occupied, default=1_000_000_000) + 1,
                            "id": reward.item_id,
                            "level": 1,
                            "exp": 0,
                            "state": 0,
                            "appearance_id": reward.item_id,
                            "combat_power": 100,
                        }
                    )
                    heroes_changed = True
                continue

            if reward.res_type == RESOURCE_GOSPEL and reward.item_id in self.gospel_ids:
                existing_index = next(
                    (
                        index
                        for index, row in enumerate(gospels)
                        if isinstance(row, Mapping)
                        and _state_uint(row.get("id")) == reward.item_id
                    ),
                    None,
                )
                if existing_index is None:
                    occupied = [
                        _state_uint(row.get("uid"))
                        for row in gospels
                        if isinstance(row, Mapping)
                    ]
                    gospels.append(self.gospel_factory.create(
                        reward.item_id, max(2_000_000_000, max(occupied, default=0)) + 1))
                else:
                    row = dict(gospels[existing_index])
                    row["repeated_num"] = _state_uint(row.get("repeated_num")) + 1
                    gospels[existing_index] = row
                gospels_changed = True
                continue

            if reward.res_type == RESOURCE_FOOD:
                key = str(reward.item_id)
                foods[key] = _state_uint(foods.get(key)) + reward.count
                foods_changed = True
                continue

            if reward.res_type == RESOURCE_RUNE:
                candidates = self._rune_candidate_ids(reward)
                if not candidates:
                    raise BattleCatalogError(
                        "rune reward has no concrete static mapping: "
                        f"suit_or_id={reward.item_id} quality={reward.quality} "
                        f"position={reward.position}"
                    )

                # Defined rune rewards put their per-roll quantity in Value;
                # Count is the aggregation count when identical rolls combine.
                # Materialising their product preserves both fields without
                # adding non-protocol metadata to RuneItem rows.
                value_count = reward.value if reward.value > 0 else 1
                entity_count = reward.count * value_count
                if entity_count > UINT32_MAX:
                    raise BattleCatalogError("rune reward entity count exceeds uint32")

                for _ in range(entity_count):
                    rune_id = min(
                        candidates,
                        key=lambda candidate: (
                            rune_id_counts.get(candidate, 0),
                            _state_uint(self.rune_static[candidate].get("position")),
                            candidate,
                        ),
                    )
                    while next_rune_uid in used_entity_uids:
                        next_rune_uid += 1
                    if next_rune_uid > UINT64_MAX:
                        raise BattleCatalogError("rune UID range exhausted")

                    row = copy.deepcopy(self.rune_templates[rune_id])
                    row["rune_item_uid"] = next_rune_uid
                    row["hero_uid"] = 0
                    # Battle rewards are newly acquired instances.  The
                    # bootstrap template is maxed for an all-unlocked profile;
                    # earned runes must enter the real growth loop at level one.
                    row["level"] = 1
                    row["cur_exp"] = 0
                    row["state"] = 0
                    row["create_time"] = max(
                        next_rune_create_time,
                        RUNE_CREATE_TIME_BASE + next_rune_uid - RUNE_UID_BASE,
                    )
                    rune_items.append(row)
                    rune_id_counts[rune_id] = rune_id_counts.get(rune_id, 0) + 1
                    used_entity_uids.add(next_rune_uid)
                    next_rune_create_time = row["create_time"] + 1
                    next_rune_uid += 1
                    runes_changed = True
                continue

            # Unbound sources keep their contextual values. Newcomer battle
            # favor above uses only the persisted admission party.
            key = f"{reward.res_type}:{reward.item_id}"
            balances[key] = _state_uint(balances.get(key)) + reward.count
            balances_changed = True

        for name in scalar_changed:
            changes[name] = scalar_values[name]
        if inventory_changed:
            changes["inventory"] = inventory
        if balances_changed:
            changes["resource_balances"] = balances
        if heroes_changed:
            changes["heroes"] = heroes
        if gospels_changed:
            changes["gospels"] = gospels
        if foods_changed:
            changes["food_inventory"] = foods
        if runes_changed:
            changes["rune_items"] = rune_items
        return changes

    def grant_rewards(
        self,
        profile: Mapping[str, Any],
        rewards: Sequence[Reward],
    ) -> Dict[str, Any]:
        """Project statically proven rewards onto an account profile.

        Independent battle families (task, tower, maze, and fast battle) use
        the same inventory/UID/resource rules as ordinary dungeon settlement.
        Keeping one public entry point prevents those protocol layers from
        implementing subtly different item caps or entity UID allocation.
        """

        return self._grant_changes(profile, rewards)

    @staticmethod
    def _stored_rewards(value: Any) -> Tuple[Reward, ...]:
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
            return ()
        result = []
        for raw in value:
            if not isinstance(raw, Mapping):
                continue
            result.append(
                Reward(
                    _state_uint(raw.get("res_type")),
                    _state_uint(raw.get("item_id")),
                    _state_uint(raw.get("count")),
                    _state_uint(raw.get("para")),
                    _state_uint(raw.get("position")),
                    _state_uint(raw.get("quality")),
                    _state_uint(raw.get("value")),
                )
            )
        return tuple(result)

    def settle(
        self,
        profile: Mapping[str, Any],
        *,
        dungeon_id: int,
        role_id: int,
        target_id: int = 0,
        result: int = 1,
        battle_stats: Optional[Mapping[str, int]] = None,
        now: int | None = None,
    ) -> SettlementResult:
        dungeon = self.dungeons.get(int(dungeon_id))
        if dungeon is None:
            return SettlementResult(
                error_code=ERROR_CONFIG,
                reason=f"unknown dungeon {dungeon_id}",
            )
        runtime = self._runtime(profile)
        fingerprint = f"{role_id}:{target_id}:{dungeon_id}:{int(result)}"
        active = runtime.get("active")
        session_id = 0
        if isinstance(active, Mapping) and not active.get("settled"):
            if _state_uint(active.get("dungeon_id")) != dungeon_id or _state_uint(
                active.get("role_id")
            ) not in (0, role_id):
                return SettlementResult(
                    error_code=ERROR_CLIENT_REQUEST_PARAM,
                    reason="battle end does not match active session",
                )
            session_id = _state_uint(active.get("session_id"))
        else:
            previous = runtime.get("last_settlement")
            if (
                isinstance(previous, Mapping)
                and previous.get("fingerprint") == fingerprint
            ):
                return SettlementResult(
                    fixed_rewards=self._stored_rewards(previous.get("fixed_rewards")),
                    first_rewards=self._stored_rewards(previous.get("first_rewards")),
                    random_rewards=self._stored_rewards(previous.get("random_rewards")),
                    star_rewards=self._stored_rewards(previous.get("star_rewards")),
                    achieved_conditions=tuple(
                        _state_uint(value)
                        for value in previous.get("achieved_conditions", [])
                        if _state_uint(value)
                    ),
                    star=_state_uint(previous.get("star")),
                    missing_drop_ids=tuple(
                        _state_uint(value)
                        for value in previous.get("missing_drop_ids", [])
                        if _state_uint(value)
                    ),
                    is_first_pass=bool(previous.get("is_first_pass")),
                    replayed=True,
                    session_id=_state_uint(previous.get("session_id")),
                    reason="duplicate battle end replayed without another grant",
                )
            if self.normal_economy:
                return SettlementResult(error_code=ERROR_CLIENT_REQUEST_PARAM,
                                        reason="battle settlement requires a matching paid admission")
            session_id = _state_uint(runtime.get("next_session_id"), 1)
            if session_id <= 0:
                session_id = 1
            runtime["next_session_id"] = session_id + 1

        timestamp = int(now if now is not None else time.time())
        prior_success = self._successful_before(profile, dungeon_id)
        won = int(result) == 1
        successful_runs_source = runtime.get("successful_runs", {})
        successful_runs = (
            copy.deepcopy(dict(successful_runs_source))
            if isinstance(successful_runs_source, Mapping)
            else {}
        )
        run_number = _state_uint(successful_runs.get(str(dungeon_id)))
        fixed = dungeon.fixed_rewards if won else ()
        first = dungeon.first_rewards if won and not prior_success else ()
        random = (
            self._random_rewards(
                dungeon,
                role_id=role_id,
                run_number=run_number + 1,
                session_id=session_id,
                extra_drop_enabled=bool(isinstance(active, Mapping) and active.get("extra_drop_enabled")),
            )
            if won
            else ()
        )
        if won and dungeon.missing_drop_ids:
            LOG.warning(
                "dungeon=%d references unavailable drop groups=%s; "
                "fixed and first-pass rewards remain authoritative",
                dungeon_id,
                ",".join(str(value) for value in dungeon.missing_drop_ids),
            )

        this_achieved = (
            self.achieved_star_conditions(dungeon_id, battle_stats) if won else ()
        )
        configured_conditions = set(dungeon.star_condition_ids)
        achieved_source = profile.get("dungeon_achieved_conditions", {})
        achieved_mapping = (
            copy.deepcopy(dict(achieved_source))
            if isinstance(achieved_source, Mapping)
            else {}
        )
        prior_achieved_raw = achieved_mapping.get(
            str(dungeon_id), achieved_mapping.get(dungeon_id, [])
        )
        prior_achieved = (
            {
                _state_uint(value)
                for value in prior_achieved_raw
                if _state_uint(value) in configured_conditions
            }
            if isinstance(prior_achieved_raw, Sequence)
            and not isinstance(prior_achieved_raw, (str, bytes))
            else set()
        )

        stars_source = profile.get("dungeon_stars", {})
        dungeon_stars = (
            copy.deepcopy(dict(stars_source))
            if isinstance(stars_source, Mapping)
            else {}
        )
        previous_star_raw = dungeon_stars.get(
            str(dungeon_id), dungeon_stars.get(dungeon_id)
        )
        previous_battle = profile.get("completed_battles", {})
        previous_battle_row = (
            previous_battle.get(str(dungeon_id), previous_battle.get(dungeon_id, {}))
            if isinstance(previous_battle, Mapping)
            else {}
        )
        if previous_star_raw is None and isinstance(previous_battle_row, Mapping):
            previous_star_raw = previous_battle_row.get("star")
        if (
            str(dungeon_id) not in achieved_mapping
            and dungeon_id not in achieved_mapping
            and isinstance(previous_battle_row, Mapping)
        ):
            saved_conditions = previous_battle_row.get("achieved_conditions", [])
            if isinstance(saved_conditions, Sequence) and not isinstance(saved_conditions, (str, bytes)):
                prior_achieved.update(_state_uint(value) for value in saved_conditions
                                      if _state_uint(value) in configured_conditions)

        if (
            previous_star_raw is None
            and profile.get("profile_mode") == "test-all-unlocked"
            and not self.normal_economy
        ):
            prior_achieved.update(dungeon.star_condition_ids)
            previous_star = min(3, len(dungeon.star_condition_ids))
        else:
            previous_star = min(3, _state_uint(previous_star_raw))

        cumulative_achieved = tuple(
            condition_id
            for condition_id in dungeon.star_condition_ids
            if condition_id in prior_achieved or condition_id in this_achieved
        )
        current_star = max(previous_star, min(3, len(cumulative_achieved)))
        star_rewards = (
            dungeon.star_rewards
            if won and previous_star < 3 and current_star >= 3
            else ()
        )

        changes: Dict[str, Any] = {}
        if won:
            successful_runs[str(dungeon_id)] = run_number + 1
            runtime["successful_runs"] = successful_runs
            changes.update(
                self._grant_changes(
                    profile,
                    (*fixed, *first, *random, *star_rewards),
                    battle_favor_uids=(active.get("gospel_uids",[]) if isinstance(active,Mapping)
                        and profile.get("player_progression",{}).get("mode")=="newcomer-v1" else None),
                    favor_day=(timestamp+28800)//86400,
                )
            )
            if current_star != previous_star or set(cumulative_achieved) != prior_achieved:
                dungeon_stars[str(dungeon_id)] = current_star
                achieved_mapping[str(dungeon_id)] = list(cumulative_achieved)
                changes["dungeon_stars"] = dungeon_stars
                changes["dungeon_achieved_conditions"] = achieved_mapping

        completed_battles_source = profile.get("completed_battles", {})
        completed_battles = (
            copy.deepcopy(dict(completed_battles_source))
            if isinstance(completed_battles_source, Mapping)
            else {}
        )
        completed_battles[str(dungeon_id)] = {
            "result": 1 if won else 0,
            "target_id": int(target_id),
            "session_id": session_id,
            "star": current_star,
            "achieved_conditions": list(cumulative_achieved),
            "updated_at": timestamp,
        }
        changes["completed_battles"] = completed_battles

        if won:
            completed_source = profile.get("completed_dungeons", [])
            completed = (
                list(completed_source) if isinstance(completed_source, list) else []
            )
            if dungeon_id not in completed:
                completed.append(dungeon_id)
            changes["completed_dungeons"] = completed
            unlocked_source = profile.get("unlocked_dungeons", [])
            unlocked = (
                list(unlocked_source) if isinstance(unlocked_source, list) else []
            )
            for next_dungeon in dungeon.next_dungeons:
                if next_dungeon not in unlocked:
                    unlocked.append(next_dungeon)
            changes["unlocked_dungeons"] = unlocked
            if dungeon_id == 8_600_013:
                changes["tutorial_completed"] = True

        runtime["active"] = None
        runtime["last_settlement"] = {
            "fingerprint": fingerprint,
            "session_id": session_id,
            "dungeon_id": dungeon_id,
            "result": 1 if won else 0,
            "is_first_pass": bool(won and not prior_success),
            "fixed_rewards": [reward.as_dict() for reward in fixed],
            "first_rewards": [reward.as_dict() for reward in first],
            "random_rewards": [reward.as_dict() for reward in random],
            "star_rewards": [reward.as_dict() for reward in star_rewards],
            "achieved_conditions": list(this_achieved),
            "star": current_star,
            "missing_drop_ids": list(dungeon.missing_drop_ids),
            "settled_at": timestamp,
        }
        changes[BATTLE_STATE_KEY] = runtime

        history_source = profile.get("reward_history", [])
        history = list(history_source) if isinstance(history_source, list) else []
        history_entry = {
            "transaction_id": f"battle:{session_id}",
            "source": "battle",
            "dungeon_id": dungeon_id,
            "result": 1 if won else 0,
            "fixed": [reward.as_dict() for reward in fixed],
            "first": [reward.as_dict() for reward in first],
            "random": [reward.as_dict() for reward in random],
            "created_at": timestamp,
        }
        if dungeon.missing_drop_ids:
            history_entry["missing_drop_ids"] = list(dungeon.missing_drop_ids)
        if this_achieved:
            history_entry["achieved_conditions"] = list(this_achieved)
            history_entry["star"] = current_star
        if star_rewards:
            history_entry["star3"] = [reward.as_dict() for reward in star_rewards]
        history.append(history_entry)
        changes["reward_history"] = history

        # Twelve drop groups referenced by the shipped dungeon table are not
        # present in the source a_drop_item TextAsset.  Preserve a durable,
        # replayable entitlement instead of silently discarding the request or
        # inventing a neighbouring group's probability table.  An authoritative
        # table defined later can replay this exact deterministic seed once.
        if won and dungeon.missing_drop_ids:
            pending_source = profile.get("pending_drop_entitlements", [])
            pending = list(pending_source) if isinstance(pending_source, list) else []
            existing = {
                (str(row.get("transaction_id")), _state_uint(row.get("requested_drop_id")))
                for row in pending
                if isinstance(row, Mapping)
            }
            transaction_id = f"battle:{session_id}"
            deterministic_seed = (
                f"{role_id}:{dungeon_id}:{run_number + 1}:{session_id}"
            )
            for drop_id in dungeon.missing_drop_ids:
                key = (transaction_id, drop_id)
                if key in existing:
                    continue
                pending.append(
                    {
                        "transaction_id": transaction_id,
                        "source": "missing-a_drop_item",
                        "status": "pending_source",
                        "requested_drop_id": drop_id,
                        "dungeon_id": dungeon_id,
                        "session_id": session_id,
                        "run_number": run_number + 1,
                        "deterministic_seed": deterministic_seed,
                        "content_catalog_version": self.catalog.version,
                        "created_at": timestamp,
                    }
                )
            changes["pending_drop_entitlements"] = pending

        return SettlementResult(
            changes=changes,
            fixed_rewards=fixed,
            first_rewards=first,
            random_rewards=random,
            star_rewards=star_rewards,
            achieved_conditions=this_achieved,
            star=current_star,
            missing_drop_ids=dungeon.missing_drop_ids,
            is_first_pass=bool(won and not prior_success),
            session_id=session_id,
        )


__all__ = [
    "BATTLE_STATE_KEY",
    "BattleCatalogError",
    "BattleSettlementLayer",
    "Dungeon",
    "DropEntry",
    "Reward",
    "SettlementResult",
    "StarCondition",
    "StartResult",
    "aggregate",
]
