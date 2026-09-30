"""Persistent, table-backed minimum loop for Holy Tower battles.

The socket runtime owns the account transaction.  This module is deliberately
pure: ``handle`` receives one profile snapshot and returns a shallow patch that
can be committed atomically with the response.  Every dungeon, sub-dungeon,
reward and team-level decision comes from the defined content tables.
"""

from __future__ import annotations

import copy
import json
import time
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

try:  # Package imports in tests; fallbacks support direct server execution.
    from .battle_settlement import BattleSettlementLayer, Reward
    from .combat_attribute_resolver import CombatAttributeResolver
    from .content_profile import ContentCatalog
    from .schema_codec import SchemaCodec, SchemaCodecError
except ImportError:  # pragma: no cover
    from battle_settlement import BattleSettlementLayer, Reward  # type: ignore
    from combat_attribute_resolver import CombatAttributeResolver  # type: ignore
    from content_profile import ContentCatalog  # type: ignore
    from schema_codec import SchemaCodec, SchemaCodecError  # type: ignore


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONTENT_DIR = ROOT / "generated" / "content"
DEFAULT_PROTOCOL_CATALOG = ROOT / "generated" / "protocol" / "protocol_catalog.json"

CS_CRAWL_TOWER_INIT = 525
CS_REQUEST_CRAWL_TOWER_DATA = 527
CS_SET_CRAWL_TOWER_PHALANX = 529
CS_CRAWL_TOWER_BATTLE_END = 531
CS_GET_CRAWL_TOWER_PHALANX = 533
CS_CLICK_TOWER_EVENT_OPTION = 544
CS_CRAWL_TOWER_ENTER_DUNGEON = 554
CS_ABANDON_CRAWL_TOWER = 556
CS_REQUEST_CRAWL_TOWER_REMAIN_TIME = 572
CS_REQUEST_CRAWL_TOWER_PERIOD_DATA = 580
CS_REQUEST_TOWER_CANDIDATE_GOSPEL_LIST = 613
CS_REQUEST_TOWER_EFFECT_ITEMS = 739
CS_REQUEST_AVAILABLE_TOWER_GOSPELS = 744
CS_CHOOSE_TOWER_GOSPEL_REWARD = 746
CS_TOWER_REFRESH_GOSPEL_CLASS = 749
CS_CRAWL_TOWER_PREPARE = 751
CS_TOWER_GENERATE_OPEN_ITEMS = 753
CS_REQUEST_TOWER_SHOW_HERO_DATA = 755
CS_CRAWL_TOWER_SET_DIFFICULTY = 757
CS_REQUEST_AVAILABLE_TOWER_GOSPEL_CLASSES = 780
CS_CHOOSE_TOWER_GOSPEL_CLASS = 783
CS_REQUEST_CRAWL_TOWER_STATUS = 818
CS_TOWER_START_BATTLE = 904
CS_GET_OPENING_GOSPEL_LIST = 1150
CS_OPENING_CHOOSE_GOSPEL = 1152
CS_OPENING_CHOOSE_HERO = 1154
CS_GET_LEVEL_SETTLEMENT_GOSPEL_LIST = 1156
CS_LEVEL_SETTLEMENT_BUY_GOSPEL = 1158
CS_GET_HOLY_TOWER_NOW_STEP = 1160
CS_GET_HOLY_TOWER_DATA = 1171
CS_GET_LEVEL_SETTLEMENT_ITEM_LIST = 1173
CS_GET_TOWER_SHOP_ITEM_LIST = 1186
CS_REFRESH_TOWER_SHOP_ITEM_LIST = 1188
CS_CHOOSE_LEVEL_SETTLEMENT_ITEM = 1190
CS_GET_HOLY_TOWER_ITEM_ACTIVATE_EFFECT_LIST = 1181
CS_ACTIVATE_HOLY_TOWER_ITEM_EFFECT = 1183
CS_SET_PROCESS_DUNGEON = 1210
CS_CRAWL_TOWER_CANCEL_DUNGEON = 1228

REQUEST_NAMES = {
    CS_CRAWL_TOWER_INIT: "CS_CrawlTowerInit",
    CS_REQUEST_CRAWL_TOWER_DATA: "CS_RequestCrawlTowerData",
    CS_SET_CRAWL_TOWER_PHALANX: "CS_SetCrawlTowerPhalanx",
    CS_CRAWL_TOWER_BATTLE_END: "CS_CrawlTowerBattleEnd",
    CS_GET_CRAWL_TOWER_PHALANX: "CS_GetCrawlTowerPhalanx",
    CS_CLICK_TOWER_EVENT_OPTION: "CS_ClickTowerEventOption",
    CS_CRAWL_TOWER_ENTER_DUNGEON: "CS_CrawlTowerEnterDungeon",
    CS_ABANDON_CRAWL_TOWER: "CS_AbandonCrawlTower",
    CS_REQUEST_CRAWL_TOWER_REMAIN_TIME: "CS_RequestCrawlTowerRemainTime",
    CS_REQUEST_CRAWL_TOWER_PERIOD_DATA: "CS_RequestCrawlTowerPeriodData",
    CS_REQUEST_TOWER_CANDIDATE_GOSPEL_LIST: "CS_RequestTowerCandidateGospelList",
    CS_REQUEST_TOWER_EFFECT_ITEMS: "CS_RequestTowerEffectItems",
    CS_REQUEST_AVAILABLE_TOWER_GOSPELS: "CS_RequestAvailableTowerGospels",
    CS_CHOOSE_TOWER_GOSPEL_REWARD: "CS_ChooseTowerGospelReward",
    CS_TOWER_REFRESH_GOSPEL_CLASS: "CS_TowerRefreshGospelClass",
    CS_CRAWL_TOWER_PREPARE: "CS_CrawlTowerPrepare",
    CS_TOWER_GENERATE_OPEN_ITEMS: "CS_TowerGenerateOpenItems",
    CS_REQUEST_TOWER_SHOW_HERO_DATA: "CS_RequestTowerShowHeroData",
    CS_CRAWL_TOWER_SET_DIFFICULTY: "CS_CrawlTowerSetDifficulty",
    CS_REQUEST_AVAILABLE_TOWER_GOSPEL_CLASSES: "CS_RequestAvailableTowerGospelClasses",
    CS_CHOOSE_TOWER_GOSPEL_CLASS: "CS_ChooseTowerGospelClass",
    CS_REQUEST_CRAWL_TOWER_STATUS: "CS_RequestCrawlTowerStatus",
    CS_TOWER_START_BATTLE: "CS_TowerStartBattle",
    CS_GET_OPENING_GOSPEL_LIST: "CS_GetOpeningGospelList",
    CS_OPENING_CHOOSE_GOSPEL: "CS_OpeningChooseGospel",
    CS_OPENING_CHOOSE_HERO: "CS_OpeningChooseHero",
    CS_GET_LEVEL_SETTLEMENT_GOSPEL_LIST: "CS_GetLevelSettlementBGospelList",
    CS_LEVEL_SETTLEMENT_BUY_GOSPEL: "CS_LevelSettlementBuyGospel",
    CS_GET_HOLY_TOWER_NOW_STEP: "CS_GetHolyTowerNowStep",
    CS_GET_HOLY_TOWER_DATA: "CS_GetHolyTowerData",
    CS_GET_LEVEL_SETTLEMENT_ITEM_LIST: "CS_GetLevelSettlementItemList",
    CS_GET_HOLY_TOWER_ITEM_ACTIVATE_EFFECT_LIST: "CS_GetHolyTowerItemActivateEffectList",
    CS_ACTIVATE_HOLY_TOWER_ITEM_EFFECT: "CS_ActivateHolyTowerItemEffect",
    CS_GET_TOWER_SHOP_ITEM_LIST: "CS_GetTowerShopItemList",
    CS_REFRESH_TOWER_SHOP_ITEM_LIST: "CS_RefreshTowerShopItemList",
    CS_CHOOSE_LEVEL_SETTLEMENT_ITEM: "CS_ChooseLevelSettlementItem",
    # The endpoint is legacyly named CS_SetProcessDungeon, but the
    # catalog's defined request message schema is CS_SetProcessDungeonId.
    CS_SET_PROCESS_DUNGEON: "CS_SetProcessDungeonId",
    CS_CRAWL_TOWER_CANCEL_DUNGEON: "CS_CrawlTowerCancelDungeon",
}

RESPONSE_IDS = {
    CS_CRAWL_TOWER_INIT: 526,
    CS_REQUEST_CRAWL_TOWER_DATA: 528,
    CS_SET_CRAWL_TOWER_PHALANX: 530,
    CS_CRAWL_TOWER_BATTLE_END: 532,
    CS_GET_CRAWL_TOWER_PHALANX: 534,
    CS_CLICK_TOWER_EVENT_OPTION: 545,
    CS_CRAWL_TOWER_ENTER_DUNGEON: 555,
    CS_ABANDON_CRAWL_TOWER: 557,
    CS_REQUEST_CRAWL_TOWER_REMAIN_TIME: 573,
    CS_REQUEST_CRAWL_TOWER_PERIOD_DATA: 581,
    CS_REQUEST_TOWER_CANDIDATE_GOSPEL_LIST: 614,
    CS_REQUEST_TOWER_EFFECT_ITEMS: 740,
    CS_REQUEST_AVAILABLE_TOWER_GOSPELS: 745,
    CS_CHOOSE_TOWER_GOSPEL_REWARD: 747,
    CS_TOWER_REFRESH_GOSPEL_CLASS: 750,
    CS_CRAWL_TOWER_PREPARE: 752,
    CS_TOWER_GENERATE_OPEN_ITEMS: 754,
    CS_REQUEST_TOWER_SHOW_HERO_DATA: 756,
    CS_CRAWL_TOWER_SET_DIFFICULTY: 758,
    CS_REQUEST_AVAILABLE_TOWER_GOSPEL_CLASSES: 781,
    CS_CHOOSE_TOWER_GOSPEL_CLASS: 784,
    CS_REQUEST_CRAWL_TOWER_STATUS: 819,
    CS_TOWER_START_BATTLE: 905,
    CS_GET_OPENING_GOSPEL_LIST: 1151,
    CS_OPENING_CHOOSE_GOSPEL: 1153,
    CS_OPENING_CHOOSE_HERO: 1155,
    CS_GET_LEVEL_SETTLEMENT_GOSPEL_LIST: 1157,
    CS_LEVEL_SETTLEMENT_BUY_GOSPEL: 1159,
    CS_GET_HOLY_TOWER_NOW_STEP: 1161,
    CS_GET_HOLY_TOWER_DATA: 1172,
    CS_GET_LEVEL_SETTLEMENT_ITEM_LIST: 1174,
    CS_GET_HOLY_TOWER_ITEM_ACTIVATE_EFFECT_LIST: 1182,
    CS_ACTIVATE_HOLY_TOWER_ITEM_EFFECT: 1184,
    CS_GET_TOWER_SHOP_ITEM_LIST: 1187,
    CS_REFRESH_TOWER_SHOP_ITEM_LIST: 1189,
    CS_CHOOSE_LEVEL_SETTLEMENT_ITEM: 1191,
    CS_SET_PROCESS_DUNGEON: 1211,
    CS_CRAWL_TOWER_CANCEL_DUNGEON: 1229,
}

RESPONSE_NAMES = {
    526: "SC_CrawlTowerInitRet",
    528: "SC_CrawlTowerDataRet",
    530: "SC_SetCrawlTowerPhalanxRet",
    532: "SC_CrawlTowerBattleEndRet",
    534: "SC_GetCrawlTowerPhalanxRet",
    545: "SC_ClickTowerEventOptionRet",
    555: "SC_CrawlTowerEnterDungeonRet",
    557: "SC_AbandonCrawlTowerRet",
    573: "SC_RequestCrawlTowerRemainTimeRet",
    581: "SC_CrawlTowerPeriodDataRet",
    614: "SC_TowerCandidateGospelListRet",
    740: "SC_TowerEffectItemsRet",
    745: "SC_AvailableTowerGospelsRet",
    747: "SC_ChooseTowerGospelRewardRet",
    750: "SC_TowerRefreshGospelClassRet",
    752: "SC_CrawlTowerPrepareRet",
    754: "SC_TowerGenerateOpenItemsRet",
    756: "SC_TowerShowHeroDataRet",
    758: "SC_CrawlTowerSetDifficultyRet",
    781: "SC_AvailableTowerGospelClassesRet",
    784: "SC_ChooseTowerGospelClassRet",
    819: "SC_CrawlTowerStatusRet",
    905: "SC_TowerStartBattleRet",
    1151: "SC_GetOpeningGospelListRet",
    1153: "SC_OpeningChooseGospelRet",
    1155: "SC_OpeningChooseHeroRet",
    1157: "SC_GetLevelSettlementBGospelListRet",
    1159: "SC_LevelSettlementBuyGospelRet",
    1161: "SC_GetHolyTowerNowStepRet",
    1172: "SC_GetHolyTowerDataRet",
    1174: "SC_GetLevelSettlementItemListRet",
    1182: "SC_GetHolyTowerItemActivateEffectListRet",
    1184: "SC_ActivateHolyTowerItemEffectRet",
    1187: "SC_GetTowerShopItemListRet",
    1189: "SC_RefreshTowerShopItemListRet",
    1191: "SC_ChooseLevelSettlementItemRet",
    # The endpoint catalog only defined the legacy command name
    # SC_SetProcessDungeonRet, but the runtime handler deserializes the concrete
    # protobuf class below and copies DungeonId into TowerData.ServerDungID.
    1211: "SC_SetProcessDungeonIdRet",
    1229: "SC_CrawlTowerCancelDungeonRet",
}

ERROR_SUCCESS = 0
ERROR_CLIENT_REQUEST_PARAM = 32000
ERROR_MESSAGE_DATA_ILLEGAL = 32769
ERROR_CONFIG = 32772

BATTLE_WIN = 1
CRAWL_TOWER_IDLE = 0
CRAWL_TOWER_RUNNING = 1
STEP_CHOOSE_HERO = 101
STEP_CHOOSE_GOSPEL_1 = 102
STEP_CHOOSE_GOSPEL_2 = 103
STEP_CHOOSE_GOSPEL_3 = 104
STEP_CHOOSE_FINISH = 105
STEP_RUNNING = 106
STEP_BATTLE_SETTLEMENT = 201
STEP_BUY_GOSPEL = 202
STEP_CHOOSE_ITEM = 203
STEP_BATTLE_SETTLED = 204

PREPARATION_UNPREPARED = 0
PREPARATION_TACTIC = 1
PREPARATION_OPEN_ITEM = 2
PREPARATION_HERO = 3
PREPARATION_GOSPEL = 4
PREPARATION_FINISHED = 5

DEFAULT_TOWER_ID = 6_000_001
DEFAULT_DIFFICULTY = 1
DEFAULT_LAYER = 1
DEFAULT_MAP_ID = 1_020_101
DEFAULT_TALENT_TREE_ID = 3_890_001
STATE_KEY = "tower_battles"
STATE_SCHEMA_VERSION = 5
OPENING_CANDIDATE_COUNT = 5
OPENING_CHOOSE_COUNT = 2
OPENING_GOLD = 999_999
OPENING_REFRESH_LIMIT = 99
SHOP_ITEM_COUNT = 6
SHOP_REFRESH_LIMIT = 99
# The server refreshes this rolling window on every query.  It is intentionally
# well below uint32 overflow and long enough that an offline save never enters
# the source live-service settlement window between launches.
OFFLINE_REMAIN_TIME = 10 * 365 * 24 * 60 * 60


class TowerBattleConfigError(ValueError):
    """Raised when defined content and protocol data disagree."""


@dataclass(frozen=True)
class Result:
    response_id: int
    body: bytes
    changes: Dict[str, Any] = field(default_factory=dict)
    rewards: Tuple[Reward, ...] = ()
    supported: bool = True
    reason: str = ""
    error_code: int = ERROR_SUCCESS
    replayed: bool = False
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


def _json(value: Any, label: str) -> Any:
    if value in (None, ""):
        return []
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        raise TowerBattleConfigError(f"{label} is invalid JSON") from exc


def _reward_rows(rewards: Sequence[Reward]) -> List[Dict[str, int]]:
    return [reward.as_dict() for reward in rewards]


def _stored_rewards(value: Any) -> Tuple[Reward, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    rows: List[Reward] = []
    for raw in value:
        if not isinstance(raw, Mapping):
            continue
        rows.append(
            Reward(
                _uint(raw.get("res_type")),
                _uint(raw.get("item_id")),
                _uint(raw.get("count")),
                _uint(raw.get("para")),
                _uint(raw.get("position")),
                _uint(raw.get("quality")),
                _uint(raw.get("value")),
            )
        )
    return tuple(rows)


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


class TowerBattleCatalog:
    """Validated indexed view over the authoritative Holy Tower tables."""

    def __init__(self, catalog: ContentCatalog) -> None:
        self.catalog = catalog
        self.tower = catalog.table("a_holytower").record(DEFAULT_TOWER_ID)
        self.people = catalog.table("a_people").by_id
        self.difficulties = catalog.table("a_holytower_diff").by_id
        self.gospel_pool = {
            _uint(row.get("id")): row
            for row in catalog.table("a_holytower_gospelpool").records
            if _uint(row.get("id"))
        }
        self.gospels = catalog.table("a_gospel").by_id
        self.event_groups = {
            _uint(row.get("id")): tuple(
                event_id
                for event_id in (
                    _uint(value)
                    for value in str(row.get("group", "")).split(";")
                )
                if event_id
            )
            for row in catalog.table("a_holytower_event_group").records
            if _uint(row.get("id"))
        }
        self.events = catalog.table("a_holytower_event").by_id
        self.event_options = catalog.table("a_event_selection").by_id
        self.shops = tuple(
            sorted(
                catalog.table("a_holytower_shop").records,
                key=lambda row: _uint(row.get("id")),
            )
        )
        self.rogue_items = {
            _uint(row.get("id")): row
            for row in catalog.table("a_rogueeft_item").records
            if _uint(row.get("id"))
        }
        global_rows = catalog.table("a_holytower_global").by_id
        self.gospel_refresh_costs = self._global_costs(global_rows, 9_990_003)
        self.item_refresh_costs = self._global_costs(global_rows, 9_990_004)
        self.shop_refresh_costs = self._global_costs(global_rows, 9_990_011)

        original_item_rows = tuple(catalog.table("a_holytower_original_item").records)
        original_by_group: Dict[int, List[Mapping[str, Any]]] = {}
        for row in original_item_rows:
            original_by_group.setdefault(_uint(row.get("group")), []).append(row)
        self.original_items_by_group = {
            group: tuple(sorted(rows, key=lambda row: _uint(row.get("id"))))
            for group, rows in original_by_group.items()
        }

        initial_items: Dict[int, List[int]] = {
            difficulty: [] for difficulty in self.difficulties
        }
        for row in original_item_rows:
            if _uint(row.get("group")) != 1 or _uint(row.get("weight")) <= 0:
                continue
            item_id = _uint(row.get("item_id"))
            allowed = {
                _uint(value)
                for value in str(row.get("difficulty", "")).split(",")
                if _uint(value)
            }
            for difficulty in initial_items:
                if difficulty in allowed and item_id not in initial_items[difficulty]:
                    initial_items[difficulty].append(item_id)
        self.initial_items = {
            difficulty: tuple(sorted(item_ids))
            for difficulty, item_ids in initial_items.items()
        }

        self.floors = {
            (_uint(row.get("tower_diff")), _uint(row.get("floor"))): row
            for row in catalog.table("a_holytower_floor").records
        }
        self.nodes = {
            _uint(row.get("id")): row
            for row in catalog.table("a_holytower_map").records
        }
        self.nodes_by_map: Dict[int, Tuple[int, ...]] = {}
        by_map: Dict[int, List[int]] = {}
        for node_id, row in self.nodes.items():
            by_map.setdefault(_uint(row.get("mapid")), []).append(node_id)
        self.nodes_by_map = {
            map_id: tuple(sorted(node_ids)) for map_id, node_ids in by_map.items()
        }

        self.battles = {
            (
                _uint(row.get("tower_diff")),
                _uint(row.get("floor")),
                _uint(row.get("type")),
            ): row
            for row in catalog.table("a_holytower_battle").records
        }
        pools: Dict[int, List[Mapping[str, Any]]] = {}
        for row in catalog.table("a_holytower_dungeon_pool").records:
            pools.setdefault(_uint(row.get("group")), []).append(row)
        self.pools = {
            group: tuple(sorted(rows, key=lambda row: _uint(row.get("id"))))
            for group, rows in pools.items()
        }
        self.team_rows = tuple(
            sorted(
                catalog.table("a_holytower_team").records,
                key=lambda row: (_uint(row.get("exp")), _uint(row.get("level"))),
            )
        )
        theme_group = _uint(str(self.tower.get("theme", "0")).split(",", 1)[0])
        matching_themes = [
            _uint(row.get("id"))
            for row in catalog.table("a_holytower_theme").records
            if _uint(row.get("group")) == theme_group and _uint(row.get("id"))
        ]
        if not matching_themes:
            raise TowerBattleConfigError(
                f"Holy Tower theme group {theme_group} has no theme rows"
            )
        # The official season has ended.  The offline profile therefore pins
        # the newest theme bundled in the runtime instead of exposing the
        # group key (101), which the runtime cannot resolve as a ThemeId.
        self.theme_id = max(matching_themes)
        self.default_hero_ids = self._default_heroes()

        if set(self.difficulties) != {1, 2, 3, 4, 5}:
            raise TowerBattleConfigError(
                f"unexpected Holy Tower difficulties: {sorted(self.difficulties)}"
            )
        if not self.gospel_pool or any(
            gospel_id not in self.gospels for gospel_id in self.gospel_pool
        ):
            raise TowerBattleConfigError("Holy Tower gospel pool is incomplete")
        if any(len(self.initial_items[difficulty]) < 3 for difficulty in self.difficulties):
            raise TowerBattleConfigError("Holy Tower initial-item pool is incomplete")

        floor = self.floors.get((DEFAULT_DIFFICULTY, DEFAULT_LAYER))
        if floor is None:
            raise TowerBattleConfigError("missing default Holy Tower floor")
        default_maps = self.floor_maps(floor)
        if DEFAULT_MAP_ID not in default_maps:
            raise TowerBattleConfigError(
                f"default map {DEFAULT_MAP_ID} is not configured on floor 1"
            )
        if not self.nodes_by_map.get(DEFAULT_MAP_ID):
            raise TowerBattleConfigError("default Holy Tower map has no nodes")
        if not self.event_groups or any(
            event_id not in self.events
            for event_ids in self.event_groups.values()
            for event_id in event_ids
        ):
            raise TowerBattleConfigError("Holy Tower event groups are incomplete")
        if any(
            not self.event_option_ids(event_id)
            for event_ids in self.event_groups.values()
            for event_id in event_ids
        ):
            raise TowerBattleConfigError("Holy Tower event has no recoverable options")
        if not self.shops or not self.rogue_items:
            raise TowerBattleConfigError("Holy Tower shop content is incomplete")

    def _default_heroes(self) -> Tuple[int, int, int]:
        values = str(self.catalog.table("a_holytower_diff").record(1).get("witch", ""))
        parsed = _json(values, "a_holytower_diff[1].witch")
        hero_ids = []
        if isinstance(parsed, list):
            for row in parsed:
                if isinstance(row, Mapping):
                    hero_id = _uint(row.get("witchid"))
                    if hero_id in self.people and hero_id not in hero_ids:
                        hero_ids.append(hero_id)
        if len(hero_ids) < 3:
            hero_ids.extend(
                hero_id
                for hero_id in sorted(self.people)
                if hero_id >= 3_000_001 and hero_id not in hero_ids
            )
        if len(hero_ids) < 3:
            raise TowerBattleConfigError("Holy Tower needs at least three heroes")
        return tuple(hero_ids[:3])  # type: ignore[return-value]

    def heroes_for_difficulty(self, difficulty: int) -> Tuple[int, ...]:
        row = self.difficulties.get(int(difficulty))
        if row is None:
            return ()
        parsed = _json(row.get("witch"), f"a_holytower_diff[{difficulty}].witch")
        if not isinstance(parsed, list):
            return ()
        return tuple(
            hero_id
            for hero_id in (
                _uint(value.get("witchid"))
                for value in parsed
                if isinstance(value, Mapping)
            )
            if hero_id in self.people and _uint(self.people[hero_id].get("people_display"))
        )

    @property
    def gospel_classes(self) -> Tuple[int, ...]:
        return tuple(
            sorted(
                {
                    _uint(row.get("holytowergospel_class"))
                    for row in self.gospel_pool.values()
                    if _uint(row.get("holytowergospel_class"))
                }
            )
        )

    def gospel_ids_for_class(self, gospel_class: int = 0) -> Tuple[int, ...]:
        return tuple(
            gospel_id
            for gospel_id, row in sorted(self.gospel_pool.items())
            if not gospel_class
            or _uint(row.get("holytowergospel_class")) == int(gospel_class)
        )

    def opening_gospel_ids(
        self, step: int, refresh_count: int, excluded: Sequence[int] = ()
    ) -> Tuple[int, ...]:
        excluded_ids = {_uint(value) for value in excluded if _uint(value)}
        ids = [
            gospel_id
            for gospel_id in self.gospel_ids_for_class()
            if gospel_id not in excluded_ids
        ]
        if not ids:
            return ()
        round_index = max(0, int(step) - STEP_CHOOSE_GOSPEL_1)
        start = (
            round_index * OPENING_CANDIDATE_COUNT
            + max(0, int(refresh_count)) * OPENING_CANDIDATE_COUNT
        ) % len(ids)
        rotated = ids[start:] + ids[:start]
        return tuple(rotated[:OPENING_CANDIDATE_COUNT])

    @staticmethod
    def floor_maps(floor: Mapping[str, Any]) -> Tuple[int, ...]:
        maps: List[int] = []
        for group in str(floor.get("usemapid", "")).split(";"):
            map_id = _uint(group.split(",", 1)[0].strip())
            if map_id and map_id not in maps:
                maps.append(map_id)
        return tuple(maps)

    def map_roots(self, map_id: int) -> Tuple[int, ...]:
        nodes = self.nodes_by_map.get(int(map_id), ())
        referenced = {
            next_id
            for node_id in nodes
            for next_id in self.next_nodes(node_id)
            if next_id in nodes
        }
        roots = tuple(node_id for node_id in nodes if node_id not in referenced)
        return roots or nodes[:1]

    def next_nodes(self, node_id: int) -> Tuple[int, ...]:
        row = self.nodes.get(int(node_id))
        if row is None:
            return ()
        raw = _json(row.get("nextsite"), f"a_holytower_map[{node_id}].nextsite")
        if not isinstance(raw, list):
            return ()
        return tuple(_uint(value) for value in raw if _uint(value))

    def node_type(self, node_id: int) -> int:
        row = self.nodes.get(int(node_id))
        if row is None:
            return 0
        return _uint(str(row.get("type", "0")).split(",", 1)[0])

    @staticmethod
    def tower_event_type(node_type: int) -> int:
        """Mirror the runtime's ``(mapType - 1) / 1000`` classification."""

        return max(0, (int(node_type) - 1) // 1000)

    @staticmethod
    def _weighted_row(
        rows: Sequence[Mapping[str, Any]], weight_field: str, seed: int
    ) -> Mapping[str, Any]:
        eligible = [row for row in rows if _uint(row.get(weight_field)) > 0]
        if not eligible:
            if not rows:
                raise TowerBattleConfigError("weighted Holy Tower pool is empty")
            return rows[0]
        total = sum(_uint(row.get(weight_field)) for row in eligible)
        target = int(seed) % total
        for row in eligible:
            weight = _uint(row.get(weight_field))
            if target < weight:
                return row
            target -= weight
        return eligible[-1]

    @staticmethod
    def _global_costs(
        rows: Mapping[int, Mapping[str, Any]], config_id: int
    ) -> Tuple[int, ...]:
        row = rows.get(int(config_id), {})
        values = _json(
            row.get("global_value_string"),
            f"a_holytower_global[{config_id}].global_value_string",
        )
        if not isinstance(values, list):
            return ()
        return tuple(_uint(value) for value in values)

    @staticmethod
    def _weight_map(value: Any) -> Dict[int, int]:
        result: Dict[int, int] = {}
        for group in str(value or "").split(";"):
            parts = [_uint(part) for part in group.split(",")]
            if len(parts) >= 2 and parts[0]:
                result[parts[0]] = parts[1]
        return result

    def event_ids_for_node_type(self, node_type: int) -> Tuple[int, ...]:
        return tuple(
            event_id
            for event_id in self.event_groups.get(int(node_type), ())
            if event_id in self.events
        )

    def event_option_ids(self, event_id: int) -> Tuple[int, ...]:
        row = self.events.get(int(event_id))
        if row is None:
            return ()
        values = _json(row.get("options"), f"a_holytower_event[{event_id}].options")
        if not isinstance(values, list):
            return ()
        return tuple(
            option_id
            for option_id in (_uint(value) for value in values)
            if option_id in self.event_options
        )

    def event_suboption_ids(self, option_id: int) -> Tuple[int, ...]:
        row = self.event_options.get(int(option_id))
        if row is None:
            return ()
        values = _json(
            row.get("selections"), f"a_event_selection[{option_id}].selections"
        )
        if not isinstance(values, list):
            return ()
        return tuple(
            sub_id
            for sub_id in (_uint(value) for value in values)
            if sub_id in self.event_options
        )

    def event_action(self, option_id: int) -> Tuple[int, int]:
        row = self.event_options.get(int(option_id))
        if row is None:
            return (0, 0)
        effects = _json(row.get("effect"), f"a_event_selection[{option_id}].effect")
        if not isinstance(effects, list):
            return (0, 0)
        for effect in effects:
            if isinstance(effect, Mapping):
                return (_uint(effect.get("type1")), _uint(effect.get("param1")))
        return (0, 0)

    def shop_for_node(self, node_type: int, configured_shop_id: int) -> Mapping[str, Any]:
        matches = [
            row
            for row in self.shops
            if _uint(row.get("shop_type")) == int(node_type)
            and (
                not int(configured_shop_id)
                or _uint(row.get("shop_id")) == int(configured_shop_id)
            )
        ]
        if not matches:
            raise TowerBattleConfigError(
                f"Holy Tower node type {node_type} has no shop configuration"
            )
        return matches[0]

    @staticmethod
    def _shop_condition_rows(shop: Mapping[str, Any]) -> Tuple[Tuple[int, Dict[int, int], int], ...]:
        conditions: List[Tuple[int, Dict[int, int], int]] = []
        for index in range(1, 6):
            raw = str(shop.get(f"quantity_condition_{index}", "")).strip()
            if not raw:
                continue
            parts = raw.split("|")
            if len(parts) != 3:
                continue
            item_type = _uint(parts[0])
            rarity_values = [_uint(value) for value in parts[1].split(",")]
            rarity_weights = {
                rarity_values[pos]: rarity_values[pos + 1]
                for pos in range(0, len(rarity_values) - 1, 2)
                if rarity_values[pos]
            }
            quantity = _uint(parts[2])
            if item_type and quantity:
                conditions.append((item_type, rarity_weights, quantity))
        return tuple(conditions)

    @staticmethod
    def _weighted_rarity(weights: Mapping[int, int], seed: int) -> int:
        positive = [(rare, weight) for rare, weight in sorted(weights.items()) if weight]
        total = sum(weight for _rare, weight in positive)
        if total <= 0:
            return 0
        target = int(seed) % total
        for rare, weight in positive:
            if target < weight:
                return rare
            target -= weight
        return positive[-1][0]

    def shop_inventory(self, shop_id: int, seed: int) -> Tuple[int, ...]:
        shop = next(
            (row for row in self.shops if _uint(row.get("id")) == int(shop_id)),
            None,
        )
        if shop is None:
            raise TowerBattleConfigError(f"unknown Holy Tower shop {shop_id}")
        selected: List[int] = []
        slot = 0
        for item_type, rarity_weights, quantity in self._shop_condition_rows(shop):
            for _ in range(quantity):
                rare = self._weighted_rarity(rarity_weights, int(seed) + slot * 37)
                candidates = [
                    item_id
                    for item_id, row in sorted(self.rogue_items.items())
                    if _uint(row.get("item_type")) == item_type
                    and (not rare or _uint(row.get("rare")) == rare)
                    and _uint(row.get("weight")) > 0
                    and item_id not in selected
                ]
                if not candidates:
                    candidates = [
                        item_id
                        for item_id, row in sorted(self.rogue_items.items())
                        if _uint(row.get("item_type")) == item_type
                        and _uint(row.get("weight")) > 0
                        and item_id not in selected
                    ]
                if candidates:
                    selected.append(candidates[(int(seed) + slot * 17) % len(candidates)])
                slot += 1
        if len(selected) < SHOP_ITEM_COUNT:
            selected.extend(
                item_id
                for item_id, row in sorted(self.rogue_items.items())
                if _uint(row.get("weight")) > 0 and item_id not in selected
                for _unused in (None,)
                if len(selected) < SHOP_ITEM_COUNT
            )
        return tuple(selected[:SHOP_ITEM_COUNT])

    def settlement_gospels(
        self,
        rarity_config: Any,
        seed: int,
        excluded: Sequence[int] = (),
        count: int = 5,
    ) -> Tuple[int, ...]:
        rarity_weights = self._weight_map(rarity_config)
        excluded_ids = {_uint(value) for value in excluded if _uint(value)}
        selected: List[int] = []
        for slot in range(max(1, int(count))):
            rare = self._weighted_rarity(rarity_weights, int(seed) + slot * 31)
            candidates = [
                gospel_id
                for gospel_id, row in sorted(self.gospel_pool.items())
                if _uint(row.get("holytowergospel_rare")) == rare
                and _uint(row.get("weight")) > 0
                and gospel_id not in excluded_ids
                and gospel_id not in selected
            ]
            if not candidates:
                candidates = [
                    gospel_id
                    for gospel_id, row in sorted(self.gospel_pool.items())
                    if _uint(row.get("weight")) > 0
                    and gospel_id not in excluded_ids
                    and gospel_id not in selected
                ]
            if candidates:
                selected.append(candidates[(int(seed) + slot * 17) % len(candidates)])
        return tuple(selected)

    def settlement_items(
        self,
        groups: Sequence[int],
        rarity_config: Any,
        difficulty: int,
        seed: int,
        count: int = 3,
    ) -> Tuple[int, ...]:
        group_ids = [_uint(value) for value in groups if _uint(value)]
        if not group_ids:
            return ()
        rarity_weights = self._weight_map(rarity_config)
        selected: List[int] = []
        for slot in range(max(1, int(count))):
            group_id = group_ids[slot % len(group_ids)]
            rare = self._weighted_rarity(rarity_weights, int(seed) + slot * 29)
            candidates = []
            for row in self.original_items_by_group.get(group_id, ()):
                allowed = {
                    _uint(value)
                    for value in str(row.get("difficulty", "")).split(",")
                    if _uint(value)
                }
                item_id = _uint(row.get("item_id"))
                item = self.rogue_items.get(item_id)
                if (
                    item is None
                    or _uint(row.get("weight")) <= 0
                    or (allowed and int(difficulty) not in allowed)
                    or item_id in selected
                    or (rare and _uint(item.get("rare")) != rare)
                ):
                    continue
                candidates.append(row)
            if not candidates:
                candidates = [
                    row
                    for row in self.original_items_by_group.get(group_id, ())
                    if _uint(row.get("weight")) > 0
                    and _uint(row.get("item_id")) in self.rogue_items
                    and _uint(row.get("item_id")) not in selected
                ]
            if candidates:
                chosen = self._weighted_row(
                    candidates, "weight", int(seed) + slot * 43
                )
                selected.append(_uint(chosen.get("item_id")))
        if len(selected) < count:
            selected.extend(
                item_id
                for item_id, row in sorted(self.rogue_items.items())
                if _uint(row.get("weight")) > 0 and item_id not in selected
                for _unused in (None,)
                if len(selected) < count
            )
        return tuple(selected[:count])

    def resolve_node(
        self, difficulty: int, layer: int, map_id: int, node_id: int
    ) -> Dict[str, Any]:
        row = self.nodes.get(int(node_id))
        if row is None or _uint(row.get("mapid")) != int(map_id):
            raise KeyError(f"node {node_id} is not on map {map_id}")
        node_type = self.node_type(node_id)
        resolved: Dict[str, Any] = {
            "node_type": node_type,
            "sub_id": 0,
            "battle_config_id": 0,
            "reward_exp": 0,
            "rewards": [],
            "next_nodes": list(self.next_nodes(node_id)),
        }
        tower_event_type = self.tower_event_type(node_type)
        if tower_event_type == 1:
            event_ids = self.event_ids_for_node_type(node_type)
            if not event_ids:
                raise TowerBattleConfigError(
                    f"Holy Tower event node type {node_type} has no event group"
                )
            event_rows = [self.events[event_id] for event_id in event_ids]
            event = self._weighted_row(
                event_rows,
                "weight",
                int(node_id) + int(difficulty) * 97 + int(layer) * 997,
            )
            event_id = _uint(event.get("id"))
            resolved.update(
                sub_id=event_id,
                event_id=event_id,
                option_ids=list(self.event_option_ids(event_id)),
            )
            return resolved
        if tower_event_type == 2:
            shop = self.shop_for_node(node_type, _uint(row.get("shop_id")))
            resolved.update(
                sub_id=_uint(shop.get("id")),
                shop_id=_uint(shop.get("id")),
            )
            return resolved
        battle = self.battles.get((int(difficulty), int(layer), node_type))
        # Event/shop node types are real map content but are not battle reward
        # sources.  Persisting their type is safe; synthesising loot is not.
        if battle is None:
            return resolved
        group = _uint(battle.get("dungeon_group"))
        pool = self.pools.get(group, ())
        if not pool:
            raise TowerBattleConfigError(
                f"Holy Tower battle {_uint(battle.get('id'))} has empty pool {group}"
            )
        candidates = [row for row in pool if _uint(row.get("type")) == node_type]
        selected = self._weighted_row(
            candidates or list(pool),
            "stage_weight",
            int(node_id) + int(difficulty) * 97 + int(layer) * 997,
        )
        rewards = self.parse_rewards(
            battle.get("reward"), f"a_holytower_battle[{battle.get('id')}].reward"
        )
        resolved.update(
            sub_id=_uint(selected.get("id")),
            battle_config_id=_uint(battle.get("id")),
            reward_exp=_uint(battle.get("exp_reward")),
            rewards=_reward_rows(rewards),
            gospelpool_rare=str(battle.get("gospelpool_rare", "")),
            item_groups=[
                _uint(value)
                for value in str(battle.get("itemgroup", "")).split(",")
                if _uint(value)
            ],
            itemgroup_rare=str(battle.get("itemgroup_rare", "")),
        )
        return resolved

    def resolve_special_battle(
        self, difficulty: int, layer: int, battle_type: int
    ) -> Dict[str, Any]:
        battle = self.battles.get((int(difficulty), int(layer), int(battle_type)))
        if battle is None:
            raise TowerBattleConfigError(
                f"Holy Tower event battle type {battle_type} is not configured"
            )
        group = _uint(battle.get("dungeon_group"))
        pool = self.pools.get(group, ())
        candidates = [row for row in pool if _uint(row.get("type")) == int(battle_type)]
        if not candidates:
            candidates = list(pool)
        if not candidates:
            raise TowerBattleConfigError(
                f"Holy Tower event battle {_uint(battle.get('id'))} has empty pool {group}"
            )
        selected = self._weighted_row(
            candidates,
            "stage_weight",
            int(battle_type) + int(difficulty) * 97 + int(layer) * 997,
        )
        rewards = self.parse_rewards(
            battle.get("reward"), f"a_holytower_battle[{battle.get('id')}].reward"
        )
        return {
            "sub_id": _uint(selected.get("id")),
            "battle_config_id": _uint(battle.get("id")),
            "reward_exp": _uint(battle.get("exp_reward")),
            "rewards": _reward_rows(rewards),
            "battle_type": int(battle_type),
            "gospelpool_rare": str(battle.get("gospelpool_rare", "")),
            "item_groups": [
                _uint(value)
                for value in str(battle.get("itemgroup", "")).split(",")
                if _uint(value)
            ],
            "itemgroup_rare": str(battle.get("itemgroup_rare", "")),
        }

    @staticmethod
    def parse_rewards(value: Any, label: str) -> Tuple[Reward, ...]:
        raw = _json(value, label)
        if not isinstance(raw, list):
            raise TowerBattleConfigError(f"{label} must be a list")
        rewards: List[Reward] = []
        for index, row in enumerate(raw):
            if not isinstance(row, Mapping):
                raise TowerBattleConfigError(f"{label}[{index}] must be an object")
            reward = Reward(
                _uint(row.get("type")),
                _uint(row.get("id")),
                _uint(row.get("num"), 1),
                _uint(row.get("para")),
                _uint(row.get("position")),
                _uint(row.get("quality")),
                _uint(row.get("value")),
            )
            if reward.count <= 0:
                raise TowerBattleConfigError(f"{label}[{index}].num must be positive")
            rewards.append(reward)
        return tuple(rewards)

    def team_row_for_exp(self, exp: int) -> Mapping[str, Any]:
        selected: Mapping[str, Any] = self.team_rows[0]
        for row in self.team_rows:
            if _uint(row.get("exp")) > int(exp):
                break
            selected = row
        return selected

    def team_level_for_exp(self, exp: int) -> int:
        return _uint(self.team_row_for_exp(exp).get("level"), 1)

    def witch_level_for_exp(self, exp: int) -> int:
        return _uint(self.team_row_for_exp(exp).get("witch_lv"), 1)

    def next_floor_map(self, difficulty: int, layer: int) -> Tuple[int, int]:
        next_layer = int(layer) + 1
        floor = self.floors.get((int(difficulty), next_layer))
        maps = self.floor_maps(floor) if floor is not None else ()
        return (next_layer, maps[0]) if maps else (int(layer), 0)

    def floor_group_map(
        self,
        difficulty: int,
        current_layer: int,
        current_map_id: int,
    ) -> Dict[int, int]:
        """Return the runtime's complete layer-to-map lookup.

        TowerBaseStageDataCollect clears its dictionary whenever it consumes a
        526/528 snapshot.  On both the first and final floor it then indexes the
        maximum configured floor directly.  Returning only the current layer
        therefore raises KeyNotFoundException after an otherwise successful
        first-floor battle.  Future floors use the first table-backed map until
        they are reached; the selected current map is preserved verbatim.
        """

        result: Dict[int, int] = {}
        for (floor_difficulty, layer), floor in sorted(self.floors.items()):
            if int(floor_difficulty) != int(difficulty):
                continue
            maps = self.floor_maps(floor)
            if not maps:
                continue
            selected = maps[0]
            if int(layer) == int(current_layer) and int(current_map_id) in maps:
                selected = int(current_map_id)
            if selected in self.nodes_by_map:
                result[int(layer)] = int(selected)
        return result


class TowerBattlesLayer:
    """Storage-agnostic handlers for the minimum persistent Tower loop."""

    def __init__(
        self,
        content_dir: Path = DEFAULT_CONTENT_DIR,
        protocol_catalog: Path = DEFAULT_PROTOCOL_CATALOG,
        settlement_layer: Optional[BattleSettlementLayer] = None,
        combat_resolver: Optional[CombatAttributeResolver] = None,
        *,
        now: Optional[Any] = None,
    ) -> None:
        content = ContentCatalog.load(Path(content_dir))
        self.catalog = TowerBattleCatalog(content)
        self.codec = SchemaCodec(Path(protocol_catalog))
        self.protocol_catalog = Path(protocol_catalog).expanduser().resolve()
        self.settlement = settlement_layer or BattleSettlementLayer(content)
        self.combat_resolver = combat_resolver or CombatAttributeResolver(content)
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
            raise TowerBattleConfigError(f"Tower protocol mapping changed: {found!r}")
        commands = {int(row["id"]): row for row in document.get("commands", [])}
        for request_id, message_name in REQUEST_NAMES.items():
            endpoint = endpoints[request_id]
            request_types = set(endpoint.get("request_message_types") or [])
            if message_name not in request_types:
                raise TowerBattleConfigError(
                    f"endpoint {request_id} does not use request schema {message_name}"
                )
            endpoint_request_name = str(endpoint.get("request_name") or "")
            if endpoint_request_name not in commands.get(request_id, {}).get("names", []):
                raise TowerBattleConfigError(
                    f"command {request_id} is not {endpoint_request_name}"
                )
            response_id = RESPONSE_IDS[request_id]
            response_name = RESPONSE_NAMES[response_id]
            endpoint_response_name = str(endpoint.get("response_name") or "")
            if endpoint_response_name not in commands.get(response_id, {}).get(
                "names", []
            ):
                raise TowerBattleConfigError(
                    f"command {response_id} is not {endpoint_response_name}"
                )
            response_types = set(endpoint.get("response_message_types") or [])
            if response_id == RESPONSE_IDS[CS_SET_PROCESS_DUNGEON]:
                # Command metadata did not associate 1211 with its concrete
                # class, although SC_SetProcessDungeonIdRet is present in the
                # defined message catalog and is used by the runtime handler.
                if response_types:
                    raise TowerBattleConfigError(
                        "process-dungeon response catalog unexpectedly changed"
                    )
                if response_name not in self.codec.messages:
                    raise TowerBattleConfigError(
                        "missing process-dungeon response schema"
                    )
            elif response_name not in response_types:
                raise TowerBattleConfigError(
                    f"endpoint {request_id} does not use response schema {response_name}"
                )

    def _result(
        self,
        command_id: int,
        values: Mapping[str, Any],
        *,
        changes: Optional[Mapping[str, Any]] = None,
        rewards: Sequence[Reward] = (),
        reason: str = "",
        error_code: int = ERROR_SUCCESS,
        replayed: bool = False,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> Result:
        response_id = RESPONSE_IDS[command_id]
        body = self.codec.encode(RESPONSE_NAMES[response_id], values)
        return Result(
            response_id=response_id,
            body=body,
            changes=copy.deepcopy(dict(changes or {})),
            rewards=tuple(rewards),
            reason=reason,
            error_code=error_code,
            replayed=replayed,
            metadata=copy.deepcopy(dict(metadata or {})),
        )

    def _profile_heroes(self, profile: Mapping[str, Any]) -> Dict[int, Mapping[str, Any]]:
        result: Dict[int, Mapping[str, Any]] = {}
        heroes = profile.get("heroes", [])
        if isinstance(heroes, Sequence) and not isinstance(heroes, (str, bytes)):
            for row in heroes:
                if not isinstance(row, Mapping):
                    continue
                hero_id = _uint(row.get("id", row.get("HeroId")))
                if hero_id:
                    result[hero_id] = row
        return result

    def _formation_row(
        self,
        profile: Mapping[str, Any],
        hero_id: int,
        position: int,
        gospel_uids: Sequence[int] = (),
    ) -> Dict[str, Any]:
        owned = self._profile_heroes(profile).get(hero_id, {})
        uid = _uint(owned.get("uid", owned.get("HeroUId")), hero_id)
        return {
            "hero_id": hero_id,
            "hero_uid": uid or hero_id,
            "combat_pos": position,
            "gospel_uids": [
                _uint(value) for value in gospel_uids[:3] if _uint(value)
            ],
        }

    def _default_formation(self, profile: Mapping[str, Any]) -> List[Dict[str, Any]]:
        return [
            self._formation_row(profile, hero_id, position)
            for position, hero_id in enumerate(self.catalog.default_hero_ids)
        ]

    def _normalize_formation(
        self, profile: Mapping[str, Any], value: Any
    ) -> List[Dict[str, Any]]:
        rows = value if isinstance(value, Sequence) and not isinstance(value, (str, bytes)) else []
        candidates: List[Tuple[int, int, int, Sequence[int]]] = []
        seen: set[int] = set()
        for index, raw in enumerate(rows):
            if not isinstance(raw, Mapping):
                continue
            hero_id = _uint(raw.get("HeroId", raw.get("hero_id")))
            if hero_id not in self.catalog.people or hero_id in seen:
                continue
            position = _int(raw.get("CombatPos", raw.get("combat_pos")), index)
            if position not in {0, 1, 2}:
                position = index
            raw_gospels = raw.get("gospel_uids")
            if not isinstance(raw_gospels, Sequence) or isinstance(
                raw_gospels, (str, bytes)
            ):
                equipped = raw.get("EquipedGospelList", ())
                raw_gospels = [
                    _uint(item.get("GospelUId"))
                    for item in equipped
                    if isinstance(item, Mapping) and _uint(item.get("GospelUId"))
                ] if isinstance(equipped, Sequence) else []
            candidates.append((position, index, hero_id, raw_gospels))
            seen.add(hero_id)
            if len(candidates) == 3:
                break
        candidates.sort(key=lambda item: (item[0], item[1]))
        formation: List[Dict[str, Any]] = [
            self._formation_row(profile, hero_id, position, gospel_uids)
            for position, (_raw_pos, _index, hero_id, gospel_uids) in enumerate(
                candidates
            )
        ]
        for fallback in self._default_formation(profile):
            if fallback["hero_id"] not in seen and len(formation) < 3:
                formation.append(
                    self._formation_row(
                        profile,
                        fallback["hero_id"],
                        len(formation),
                    )
                )
                seen.add(fallback["hero_id"])
        return formation

    def _default_state(self, profile: Mapping[str, Any]) -> Dict[str, Any]:
        roots = self.catalog.map_roots(DEFAULT_MAP_ID)
        formation = self._default_formation(profile)
        witch_level = self.catalog.witch_level_for_exp(0)
        hp_map = {
            str(row["hero_id"]): max(
                1,
                self._tower_hero_snapshot(
                    profile, row["hero_id"], witch_level
                ).hp,
            )
            for row in formation
        }
        return {
            "schema_version": STATE_SCHEMA_VERSION,
            # A profile query must not create an in-progress run.  The Unity
            # runtime treats RUNNING as a resumable/settleable old attempt and
            # immediately sends CS_AbandonCrawlTower when the snapshot is only
            # a skeleton.  Only CS_CrawlTowerInit may enter RUNNING.
            "status": CRAWL_TOWER_IDLE,
            "tower_id": DEFAULT_TOWER_ID,
            "difficulty": DEFAULT_DIFFICULTY,
            "layer": DEFAULT_LAYER,
            "map_id": DEFAULT_MAP_ID,
            "step": STEP_CHOOSE_HERO,
            "exp": 0,
            "team_level": self.catalog.team_level_for_exp(0),
            "formation": formation,
            "preparation": {
                "stage": PREPARATION_UNPREPARED,
                "tactic_id": 0,
                "open_item_id": 0,
                "hero_gospel_class_map": {},
                "hero_gospel_map": {},
                "candidate_gospel_map": {},
            },
            "opening": {
                "selected_hero_ids": [],
                "selected_gospel_ids": [],
                "candidate_gospel_ids": [],
                "refresh_count": 0,
                "residue_gold": OPENING_GOLD,
            },
            "tower_gospels": [],
            "pending_gospel_rewards": [],
            "settlement_runtime": None,
            "event_runtime": {
                "selected_options": [],
                "completed_event_ids": [],
                "pending_battle": None,
            },
            "shop_runtime": {},
            "tower_items": {},
            "tower_item_active_uids": [],
            "item_slots": {},
            "context": {"hero_hp": hp_map},
            "progress": {
                "dungeon_id": 0,
                "sub_id": 0,
                "progress_dungeon_id": 0,
                "top_dungeon_id": 0,
                "battle_id": 0,
            },
            "completed_nodes": [],
            "active_nodes": list(roots),
            "resolved_nodes": {},
            "active_battle": None,
            "next_battle_id": 1,
            "last_settlement": None,
            "process_dungeon_id": 0,
            "started_at": 0,
        }

    def _ensure_state(
        self, profile: Mapping[str, Any]
    ) -> Tuple[Dict[str, Any], bool]:
        source = profile.get(STATE_KEY)
        if not isinstance(source, Mapping):
            return self._default_state(profile), True
        state = copy.deepcopy(dict(source))
        defaults = self._default_state(profile)
        changed = False
        for key, value in defaults.items():
            if key not in state:
                state[key] = copy.deepcopy(value)
                changed = True
        schema_version = _uint(state.get("schema_version"))
        if schema_version < STATE_SCHEMA_VERSION:
            # Version 1 eagerly created RUNNING state on the first read.  Such
            # a state has no resolved node, battle, or settlement and is not a
            # real player attempt; migrate it back to the neutral lobby.
            if (
                _uint(state.get("status")) == CRAWL_TOWER_RUNNING
                and not _mapping(state.get("active_battle"))
                and not _mapping(state.get("resolved_nodes"))
                and not _mapping(state.get("last_settlement"))
            ):
                state["status"] = CRAWL_TOWER_IDLE
                state["started_at"] = 0
                state["step"] = STEP_CHOOSE_HERO
            elif _uint(state.get("status")) == CRAWL_TOWER_IDLE:
                state["step"] = STEP_CHOOSE_HERO
            state["schema_version"] = STATE_SCHEMA_VERSION
            changed = True

        difficulty = _uint(state.get("difficulty"), DEFAULT_DIFFICULTY)
        if difficulty not in self.catalog.difficulties:
            difficulty = DEFAULT_DIFFICULTY
        if state.get("difficulty") != difficulty:
            state["difficulty"] = difficulty
            changed = True

        opening_source = _mapping(state.get("opening"))
        opening = copy.deepcopy(dict(opening_source))
        valid_heroes = set(self.catalog.heroes_for_difficulty(difficulty))
        selected_heroes: List[int] = []
        raw_selected_heroes = opening.get("selected_hero_ids", ())
        if isinstance(raw_selected_heroes, Sequence) and not isinstance(
            raw_selected_heroes, (str, bytes)
        ):
            for value in raw_selected_heroes:
                hero_id = _uint(value)
                if (
                    hero_id in valid_heroes
                    and hero_id not in selected_heroes
                    and len(selected_heroes) < 3
                ):
                    selected_heroes.append(hero_id)

        selected_gospels: List[int] = []
        raw_selected_gospels = opening.get("selected_gospel_ids", ())
        if isinstance(raw_selected_gospels, Sequence) and not isinstance(
            raw_selected_gospels, (str, bytes)
        ):
            for value in raw_selected_gospels:
                gospel_id = _uint(value)
                if (
                    gospel_id in self.catalog.gospel_pool
                    and gospel_id not in selected_gospels
                    and len(selected_gospels) < 6
                ):
                    selected_gospels.append(gospel_id)

        candidate_gospels: List[int] = []
        raw_candidates = opening.get("candidate_gospel_ids", ())
        if isinstance(raw_candidates, Sequence) and not isinstance(
            raw_candidates, (str, bytes)
        ):
            for value in raw_candidates:
                gospel_id = _uint(value)
                if (
                    gospel_id in self.catalog.gospel_pool
                    and gospel_id not in selected_gospels
                    and gospel_id not in candidate_gospels
                    and len(candidate_gospels) < OPENING_CANDIDATE_COUNT
                ):
                    candidate_gospels.append(gospel_id)
        opening.update(
            selected_hero_ids=selected_heroes,
            selected_gospel_ids=selected_gospels,
            candidate_gospel_ids=candidate_gospels,
            candidate_step=(
                _uint(opening.get("candidate_step"))
                if _uint(opening.get("candidate_step"))
                in {
                    STEP_CHOOSE_GOSPEL_1,
                    STEP_CHOOSE_GOSPEL_2,
                    STEP_CHOOSE_GOSPEL_3,
                }
                else 0
            ),
            refresh_count=min(
                OPENING_REFRESH_LIMIT, _uint(opening.get("refresh_count"))
            ),
            residue_gold=_uint(opening.get("residue_gold"), OPENING_GOLD),
        )
        if opening_source != opening:
            state["opening"] = opening
            changed = True

        raw_tower_gospels = state.get("tower_gospels", ())
        tower_gospels: List[int] = []
        if isinstance(raw_tower_gospels, Sequence) and not isinstance(
            raw_tower_gospels, (str, bytes)
        ):
            for value in raw_tower_gospels:
                gospel_id = _uint(value)
                if (
                    gospel_id in self.catalog.gospel_pool
                    and gospel_id not in tower_gospels
                ):
                    tower_gospels.append(gospel_id)
        if not tower_gospels and selected_gospels:
            tower_gospels = list(selected_gospels)
        if state.get("tower_gospels") != tower_gospels:
            state["tower_gospels"] = tower_gospels
            changed = True

        raw_pending_rewards = state.get("pending_gospel_rewards", ())
        pending_rewards: List[int] = []
        if isinstance(raw_pending_rewards, Sequence) and not isinstance(
            raw_pending_rewards, (str, bytes)
        ):
            for value in raw_pending_rewards:
                gospel_id = _uint(value)
                if (
                    gospel_id in self.catalog.gospel_pool
                    and gospel_id not in tower_gospels
                    and gospel_id not in pending_rewards
                ):
                    pending_rewards.append(gospel_id)
        if state.get("pending_gospel_rewards") != pending_rewards:
            state["pending_gospel_rewards"] = pending_rewards
            changed = True

        event_source = _mapping(state.get("event_runtime"))
        event_runtime = copy.deepcopy(dict(event_source))
        selected_options = []
        raw_selected_options = event_runtime.get("selected_options", ())
        if isinstance(raw_selected_options, Sequence) and not isinstance(
            raw_selected_options, (str, bytes)
        ):
            selected_options = list(
                dict.fromkeys(
                    option_id
                    for option_id in (_uint(value) for value in raw_selected_options)
                    if option_id in self.catalog.event_options
                )
            )
        completed_events = []
        raw_completed_events = event_runtime.get("completed_event_ids", ())
        if isinstance(raw_completed_events, Sequence) and not isinstance(
            raw_completed_events, (str, bytes)
        ):
            completed_events = list(
                dict.fromkeys(
                    event_id
                    for event_id in (_uint(value) for value in raw_completed_events)
                    if event_id in self.catalog.events
                )
            )
        pending_battle = event_runtime.get("pending_battle")
        if not isinstance(pending_battle, Mapping):
            pending_battle = None
        event_runtime.update(
            selected_options=selected_options,
            completed_event_ids=completed_events,
            pending_battle=copy.deepcopy(pending_battle),
        )
        if event_source != event_runtime:
            state["event_runtime"] = event_runtime
            changed = True

        shop_source = _mapping(state.get("shop_runtime"))
        shop_runtime: Dict[str, Any] = {}
        for key, value in shop_source.items():
            dungeon_id = _uint(key)
            row = _mapping(value)
            shop_id = _uint(row.get("shop_id"))
            if not dungeon_id or not shop_id:
                continue
            item_ids = []
            raw_items = row.get("item_ids", ())
            if isinstance(raw_items, Sequence) and not isinstance(raw_items, (str, bytes)):
                item_ids = [
                    item_id
                    for item_id in (_uint(item) for item in raw_items)
                    if item_id in self.catalog.rogue_items
                ][:SHOP_ITEM_COUNT]
            purchased = []
            raw_purchased = row.get("purchased", ())
            if isinstance(raw_purchased, Sequence) and not isinstance(
                raw_purchased, (str, bytes)
            ):
                purchased = [
                    item_id
                    for item_id in (_uint(item) for item in raw_purchased)
                    if item_id in item_ids
                ]
            shop_runtime[str(dungeon_id)] = {
                "shop_id": shop_id,
                "item_ids": item_ids,
                "purchased": list(dict.fromkeys(purchased)),
                "refresh_times": min(
                    SHOP_REFRESH_LIMIT, _uint(row.get("refresh_times"))
                ),
            }
        if shop_source != shop_runtime:
            state["shop_runtime"] = shop_runtime
            changed = True

        tower_items_source = _mapping(state.get("tower_items"))
        tower_items = {
            str(item_id): _uint(count)
            for item_id, count in (
                (_uint(key), value) for key, value in tower_items_source.items()
            )
            if item_id in self.catalog.rogue_items and _uint(count)
        }
        if tower_items_source != tower_items:
            state["tower_items"] = tower_items
            changed = True

        valid_item_uids = {
            self._tower_item_uid(item_id, ordinal)
            for item_id, count in (
                (_uint(key), _uint(value)) for key, value in tower_items.items()
            )
            for ordinal in range(1, count + 1)
        }
        raw_active = state.get("tower_item_active_uids", ())
        active_uids = []
        if isinstance(raw_active, Sequence) and not isinstance(
            raw_active, (str, bytes)
        ):
            active_uids = sorted(
                {
                    _uint(value)
                    for value in raw_active
                    if _uint(value) in valid_item_uids
                }
            )
        if state.get("tower_item_active_uids") != active_uids:
            state["tower_item_active_uids"] = active_uids
            changed = True

        raw_slots = _mapping(state.get("item_slots"))
        item_slots = {
            str(_uint(slot)): _uint(uid)
            for slot, uid in raw_slots.items()
            if _uint(slot) and _uint(uid) in valid_item_uids
        }
        if raw_slots != item_slots:
            state["item_slots"] = item_slots
            changed = True

        settlement_runtime = state.get("settlement_runtime")
        if settlement_runtime is not None and not isinstance(
            settlement_runtime, Mapping
        ):
            state["settlement_runtime"] = None
            changed = True

        map_id = _uint(state.get("map_id"), DEFAULT_MAP_ID)
        map_nodes = set(self.catalog.nodes_by_map.get(map_id, ()))
        raw_completed = state.get("completed_nodes", ())
        completed_nodes = []
        if isinstance(raw_completed, Sequence) and not isinstance(
            raw_completed, (str, bytes)
        ):
            completed_nodes = list(
                dict.fromkeys(
                    node_id
                    for node_id in (_uint(value) for value in raw_completed)
                    if node_id in map_nodes
                )
            )
        if state.get("completed_nodes") != completed_nodes:
            state["completed_nodes"] = completed_nodes
            changed = True

        raw_active = state.get("active_nodes", ())
        active_nodes = []
        if isinstance(raw_active, Sequence) and not isinstance(raw_active, (str, bytes)):
            active_nodes = list(
                dict.fromkeys(
                    node_id
                    for node_id in (_uint(value) for value in raw_active)
                    if node_id in map_nodes and node_id not in completed_nodes
                )
            )
        if not active_nodes and not completed_nodes:
            active_nodes = list(self.catalog.map_roots(map_id))
        if state.get("active_nodes") != active_nodes:
            state["active_nodes"] = active_nodes
            changed = True

        preparation_source = _mapping(state.get("preparation"))
        preparation = copy.deepcopy(dict(preparation_source))
        stage = _uint(preparation.get("stage"), PREPARATION_UNPREPARED)
        if stage > PREPARATION_FINISHED:
            stage = PREPARATION_UNPREPARED
        open_item_id = _uint(preparation.get("open_item_id"))
        if open_item_id not in self.catalog.initial_items[difficulty]:
            open_item_id = 0
        preparation.update(
            stage=stage,
            tactic_id=0,
            open_item_id=open_item_id,
        )
        for key in (
            "hero_gospel_class_map",
            "hero_gospel_map",
            "candidate_gospel_map",
            "available_gospel_classes",
        ):
            if not isinstance(preparation.get(key), Mapping):
                preparation[key] = {}
        if preparation_source != preparation:
            state["preparation"] = preparation
            changed = True

        normalized = self._normalize_formation(profile, state.get("formation"))
        if state.get("formation") != normalized:
            state["formation"] = normalized
            changed = True
        context = copy.deepcopy(dict(_mapping(state.get("context"))))
        hp_map = copy.deepcopy(dict(_mapping(context.get("hero_hp"))))
        witch_level = self.catalog.witch_level_for_exp(_uint(state.get("exp")))
        for row in normalized:
            key = str(row["hero_id"])
            if key not in hp_map and row["hero_id"] not in hp_map:
                hp_map[key] = max(
                    1,
                    _uint(
                        self._tower_hero_snapshot(
                            profile, row["hero_id"], witch_level
                        ).hp
                    ),
                )
                changed = True
        context["hero_hp"] = hp_map
        state["context"] = context
        expected_level = self.catalog.team_level_for_exp(_uint(state.get("exp")))
        if _uint(state.get("team_level")) != expected_level:
            state["team_level"] = expected_level
            changed = True
        return state, changed

    @staticmethod
    def _state_changes(state: Mapping[str, Any], changed: bool = True) -> Dict[str, Any]:
        return {STATE_KEY: copy.deepcopy(dict(state))} if changed else {}

    def _tower_hero_snapshot(
        self,
        profile: Mapping[str, Any],
        hero_id: int,
        witch_level: int,
    ) -> Any:
        owned = self._profile_heroes(profile).get(hero_id)
        if owned is None:
            owned = {"id": hero_id, "uid": hero_id}
        return self.combat_resolver.resolve_tower_hero(
            owned,
            witch_level,
            all_unlocked=bool(profile.get("all_content_unlocked")),
        )

    def _hero_attr(
        self,
        profile: Mapping[str, Any],
        state: Mapping[str, Any],
        hero_id: int,
    ) -> Dict[str, int]:
        witch_level = self.catalog.witch_level_for_exp(_uint(state.get("exp")))
        return {
            str(name): _int(value)
            for name, value in self._tower_hero_snapshot(
                profile, hero_id, witch_level
            ).wire_attributes.items()
        }

    def _gospel_seed(self, gospel_id: int) -> Dict[str, Any]:
        pool = self.catalog.gospel_pool[int(gospel_id)]
        level = max(1, _uint(pool.get("gospellevel_0"), 1))
        skill_ids = _json(
            self.catalog.gospels[int(gospel_id)].get("skill"),
            f"a_gospel[{gospel_id}].skill",
        )
        skill_levels = _json(
            pool.get("skilllevel_0"),
            f"a_holytower_gospelpool[{gospel_id}].skilllevel_0",
        )
        if (
            not isinstance(skill_ids, list)
            or not isinstance(skill_levels, list)
            or len(skill_ids) != len(skill_levels)
        ):
            raise TowerBattleConfigError(
                f"Holy Tower gospel {gospel_id} has inconsistent skill seeds"
            )
        return {
            "id": int(gospel_id),
            "uid": int(gospel_id),
            "level": level,
            "awake_level": 1,
            "active_skills": [
                {
                    "id": _uint(skill_id),
                    "level": max(1, _uint(skill_level, 1)),
                    "valid_level": max(1, _uint(skill_level, 1)),
                }
                for skill_id, skill_level in zip(skill_ids, skill_levels)
            ],
        }

    def _gospel_snapshot(self, gospel_id: int) -> Any:
        return self.combat_resolver.resolve_gospel(self._gospel_seed(gospel_id))

    def _simple_gospel_values(self, gospel_id: int) -> Dict[str, Any]:
        pool = self.catalog.gospel_pool[int(gospel_id)]
        snapshot = self._gospel_snapshot(gospel_id)
        level = max(1, _uint(pool.get("gospellevel_0"), 1))
        level_limit = max(level, _uint(pool.get("gospellevel_3"), level))
        return {
            "GospelId": int(gospel_id),
            "Level": level,
            "LevelLimit": level_limit,
            "ActiveSkillList": [
                {"Id": skill_id, "Level": skill_level, "ValidLevel": skill_level}
                for skill_id, skill_level in snapshot.active_skills
            ],
            "AwakeLevel": 1,
            "GospelUId": int(gospel_id),
            "Attr": {
                str(name): _int(value)
                for name, value in snapshot.wire_attributes.items()
            },
            "IsOwn": 1,
            "ForwardedStep": 0,
            "ForwardStep": 0,
            "CombatPower": _uint(snapshot.combat_power),
        }

    def _opening_gospel_values(self, gospel_id: int) -> Dict[str, Any]:
        pool = self.catalog.gospel_pool[int(gospel_id)]
        return {
            "Id": int(gospel_id),
            "Degree": _uint(pool.get("holytowergospel_rare")),
            "Level": max(1, _uint(pool.get("gospellevel_0"), 1)),
            "BreakLevel": 0,
            "AwakeLevel": 1,
            "Purchased": False,
        }

    def _tower_gospel_values(self, gospel_id: int) -> Dict[str, Any]:
        simple = self._simple_gospel_values(gospel_id)
        return {
            "Gos": {
                "GospelUId": simple["GospelUId"],
                "GospelId": simple["GospelId"],
                "Level": simple["Level"],
                "LevelLimit": simple["LevelLimit"],
                "ActiveSkillList": simple["ActiveSkillList"],
                "AwakeLevel": simple["AwakeLevel"],
                "Attr": simple["Attr"],
                "CombatPower": simple["CombatPower"],
            },
            "RealLevel": 0,
            "RepeatNum": 0,
            "IsOwn": 1,
        }

    def _simple_hero_values(
        self, profile: Mapping[str, Any], hero_id: int, level: int
    ) -> Dict[str, Any]:
        static = self.catalog.people[int(hero_id)]
        snapshot = self._tower_hero_snapshot(profile, hero_id, level)
        owned = self._profile_heroes(profile).get(hero_id, {})
        return {
            "Ho": {
                "HeroUId": _uint(owned.get("uid", owned.get("HeroUId")), hero_id),
                "HeroId": hero_id,
                "ActiveSkillList": [
                    {
                        "Id": skill_id,
                        "Level": skill_level,
                        "ValidLevel": skill_level,
                    }
                    for skill_id, skill_level in snapshot.active_skills
                ],
                "AppearanceId": _uint(owned.get("appearance_id")),
                "ProfessionId": _uint(
                    owned.get("profession_id"),
                    _uint(static.get("job_default", static.get("job_id"))),
                ),
                "Attr": {
                    str(name): _int(value)
                    for name, value in snapshot.wire_attributes.items()
                },
                "CombatPower": _uint(snapshot.combat_power),
            },
            "Level": max(1, int(level)),
        }

    def _tower_gospel_ids(self, state: Mapping[str, Any]) -> Tuple[int, ...]:
        raw = state.get("tower_gospels", ())
        if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
            return ()
        return tuple(
            gospel_id
            for gospel_id in (_uint(value) for value in raw)
            if gospel_id in self.catalog.gospel_pool
        )

    def _gospel_class_map(self, state: Mapping[str, Any]) -> Dict[int, int]:
        stored = _mapping(
            _mapping(state.get("preparation")).get("hero_gospel_class_map")
        )
        classes = self.catalog.gospel_classes
        if not classes:
            return {}
        result: Dict[int, int] = {}
        for index, row in enumerate(state.get("formation", ())):
            if not isinstance(row, Mapping):
                continue
            hero_id = _uint(row.get("hero_id"))
            value = _uint(stored.get(str(hero_id), stored.get(hero_id)))
            if value not in classes:
                value = classes[index % len(classes)]
            result[hero_id] = value
        return result

    def _rich_phalanx(
        self, profile: Mapping[str, Any], state: Mapping[str, Any]
    ) -> Dict[str, Any]:
        exp = _uint(state.get("exp"))
        witch_level = self.catalog.witch_level_for_exp(exp)
        tower_gospel_ids = set(self._tower_gospel_ids(state))
        rich = []
        for row in state.get("formation", []):
            if not isinstance(row, Mapping):
                continue
            hero_id = _uint(row.get("hero_id"))
            snapshot = self._tower_hero_snapshot(profile, hero_id, witch_level)
            owned = self._profile_heroes(profile).get(hero_id, {})
            attr = {
                str(name): _int(value)
                for name, value in snapshot.wire_attributes.items()
            }
            # HolyTowerTeamItem always creates exactly three gospel widgets and
            # indexes EquipedGospelList directly for slots 0..2.  The source
            # runtime therefore requires explicit empty messages for unoccupied
            # slots; a compact two-element list aborts the popup during SetData.
            equipped_gospels = [
                _uint(value) if _uint(value) in tower_gospel_ids else 0
                for value in list(row.get("gospel_uids", ()))[:3]
            ]
            equipped_gospels.extend([0] * (3 - len(equipped_gospels)))
            rich.append(
                {
                    "Ho": {
                        "HeroUId": _uint(row.get("hero_uid"), hero_id),
                        "HeroId": hero_id,
                        "ActiveSkillList": [
                            {
                                "Id": skill_id,
                                "Level": skill_level,
                                "ValidLevel": skill_level,
                            }
                            for skill_id, skill_level in snapshot.active_skills
                        ],
                        "ProfessionId": _uint(
                            owned.get("profession_id"),
                            _uint(
                                self.catalog.people[hero_id].get(
                                    "job_default",
                                    self.catalog.people[hero_id].get("job_id"),
                                )
                            ),
                        ),
                        "Attr": attr,
                        "CombatPower": _uint(snapshot.combat_power),
                    },
                    "MaxBattleHp": max(1, _uint(snapshot.hp)),
                    "Level": witch_level,
                    "EquipedGospelList": [
                        {"GospelUId": gospel_uid, "AppearanceId": 0}
                        for gospel_uid in equipped_gospels
                    ],
                    "CombatPos": _int(row.get("combat_pos")),
                    "BattleCombatPower": _uint(snapshot.combat_power),
                }
            )
        return {"RichHeroList": rich}

    def _phalanx_auto_fight_values(
        self, state: Mapping[str, Any]
    ) -> Dict[str, Any]:
        """Return the fixed-width Tower auto-battle formation contract.

        The Unity save path loops through three heroes and three gospel slots
        without checking repeated-field lengths.  Empty unit strategies are
        valid defaults, but the structural rows and slots must still exist.
        """

        tower_gospel_ids = set(self._tower_gospel_ids(state))
        rows: List[Dict[str, Any]] = []
        for row in state.get("formation", ()):
            if not isinstance(row, Mapping):
                continue
            gospel_ids = [
                _uint(value) if _uint(value) in tower_gospel_ids else 0
                for value in list(row.get("gospel_uids", ()))[:3]
            ]
            gospel_ids.extend([0] * (3 - len(gospel_ids)))
            rows.append(
                {
                    "UId": _uint(row.get("hero_uid"), row.get("hero_id")),
                    "Pos": _int(row.get("combat_pos")),
                    "UnitList": [],
                    "GosList": [
                        (
                            {"UId": gospel_uid, "Pos": position, "UnitList": []}
                            if gospel_uid
                            else {}
                        )
                        for position, gospel_uid in enumerate(gospel_ids)
                    ],
                }
            )
        return {"PhalanxId": 0, "AutoList": rows}

    @staticmethod
    def _unlocked_gospel_pos_map() -> Dict[int, Dict[str, Dict[int, int]]]:
        """Project the level-one Tower slot unlocks from a_holytower_team.

        Team level 1 applies effect 9 to positions 1..3 and slots 1..3, so all
        three UI slots exist from the first battle even when only two contain
        opening gospels.
        """

        return {
            combat_pos: {"UnlockedPosMap": {slot: 1 for slot in range(3)}}
            for combat_pos in range(3)
        }

    def _context_values(
        self, profile: Mapping[str, Any], state: Mapping[str, Any]
    ) -> Dict[str, Any]:
        stored = _mapping(_mapping(state.get("context")).get("hero_hp"))
        hp_map: Dict[int, int] = {}
        witch_level = self.catalog.witch_level_for_exp(_uint(state.get("exp")))
        # The protocol contract is HeroId -> HP.  HeroUId must never be used
        # as the map key, even when the owned account has different UIDs.
        for row in state.get("formation", []):
            if not isinstance(row, Mapping):
                continue
            hero_id = _uint(row.get("hero_id"))
            value = stored.get(str(hero_id), stored.get(hero_id))
            hp_map[hero_id] = _int(
                value,
                max(
                    1,
                    _uint(
                        self._tower_hero_snapshot(
                            profile, hero_id, witch_level
                        ).hp
                    ),
                ),
            )
        return {"HeroHpMap": hp_map}

    @staticmethod
    def _progress_values(state: Mapping[str, Any]) -> Dict[str, Any]:
        progress = _mapping(state.get("progress"))
        event_runtime = _mapping(state.get("event_runtime"))
        return {
            "DungeonId": _uint(progress.get("dungeon_id")),
            "SubId": _uint(progress.get("sub_id")),
            "ProgressDungeonId": _uint(progress.get("progress_dungeon_id")),
            "TopDungeonId": _uint(progress.get("top_dungeon_id")),
            "OptionIDList": [
                _uint(value)
                for value in event_runtime.get("selected_options", ())
                if _uint(value)
            ],
            "BattleId": _uint(progress.get("battle_id")),
        }

    @staticmethod
    def _independent_tree() -> Dict[str, Any]:
        return {"TalentTreeId": DEFAULT_TALENT_TREE_ID, "TalentList": []}

    def _preparation_values(self, state: Mapping[str, Any]) -> Dict[str, Any]:
        formation = [row for row in state.get("formation", []) if isinstance(row, Mapping)]
        preparation = _mapping(state.get("preparation"))
        hero_gospel_map = _mapping(preparation.get("hero_gospel_map"))
        candidate_map = _mapping(preparation.get("candidate_gospel_map"))
        return {
            "Stage": _uint(preparation.get("stage"), PREPARATION_UNPREPARED),
            # The shipped runtime bundle has no a_rogue_tactics table.  A
            # positive value is dereferenced unconditionally and produces
            # "table can not find key"; zero is the protobuf/default semantic
            # for no selected live-service tactic.
            "TacticId": 0,
            "OpenItemId": _uint(preparation.get("open_item_id")),
            "HeroGospelClassMap": self._gospel_class_map(state),
            "HeroGospelMap": {
                hero_id: self._simple_gospel_values(gospel_id)
                for hero_id, gospel_id in (
                    (_uint(key), _uint(value))
                    for key, value in hero_gospel_map.items()
                )
                if hero_id and gospel_id in self.catalog.gospel_pool
            },
            "CandidateGospelDataMap": {
                hero_id: {
                    "GospelList": [
                        self._simple_gospel_values(gospel_id)
                        for gospel_id in values
                        if _uint(gospel_id) in self.catalog.gospel_pool
                    ]
                }
                for hero_id, values in (
                    (_uint(key), value) for key, value in candidate_map.items()
                )
                if hero_id
                and isinstance(values, Sequence)
                and not isinstance(values, (str, bytes))
            },
            "HeroCombatPosMap": {
                _uint(row.get("hero_id")): _int(row.get("combat_pos"))
                for row in formation
            },
        }

    @staticmethod
    def _tower_item_uid(item_id: int, ordinal: int) -> int:
        """Return a stable per-run UID for one persisted item instance."""

        return int(item_id) * 1_000_000 + int(ordinal)

    def _tower_item_payloads(
        self, state: Mapping[str, Any]
    ) -> Tuple[Dict[int, Dict[str, Any]], Dict[int, Dict[str, Any]]]:
        item_map: Dict[int, Dict[str, Any]] = {}
        effect_map: Dict[int, Dict[str, Any]] = {}
        active_uids = {
            _uint(value)
            for value in state.get("tower_item_active_uids", ())
            if _uint(value)
        }
        for item_id, count in sorted(
            (
                (_uint(key), _uint(value))
                for key, value in _mapping(state.get("tower_items")).items()
            ),
            key=lambda row: row[0],
        ):
            definition = self.catalog.rogue_items.get(item_id)
            if definition is None:
                continue
            raw_effects = _json(
                definition.get("func_effect"),
                f"a_rogueeft_item[{item_id}].func_effect",
            )
            if not isinstance(raw_effects, list):
                raise TowerBattleConfigError(
                    f"a_rogueeft_item[{item_id}].func_effect must be a list"
                )
            for ordinal in range(1, count + 1):
                item_uid = self._tower_item_uid(item_id, ordinal)
                effect_uids: List[int] = []
                for index, raw in enumerate(raw_effects, 1):
                    if not isinstance(raw, Mapping):
                        raise TowerBattleConfigError(
                            f"a_rogueeft_item[{item_id}].func_effect[{index - 1}] "
                            "must be an object"
                        )
                    effect_type = _uint(raw.get("effect"))
                    if not effect_type:
                        continue
                    effect_uid = item_uid * 100 + index
                    effect_uids.append(effect_uid)
                    effect_map[effect_uid] = {
                        "EffectUId": effect_uid,
                        "EffectType": effect_type,
                        "MaxNum": max(1, _uint(raw.get("count"), 1)),
                        "UsedNum": 0,
                        "Value1": _int(raw.get("value1")),
                        "Value2": _int(raw.get("value2")),
                        "IsValid": 1,
                        "SourceItemUId": item_uid,
                        # Msg.TowerEffectSourceType.TEST_EffectItem
                        "SourceType": 1,
                        "Up": _uint(raw.get("up")),
                        "Value3": _int(raw.get("value3")),
                    }
                automatically_active = (
                    _uint(definition.get("item_type")) != 4
                    or _uint(definition.get("can_use")) == 0
                )
                item_map[item_uid] = {
                    "UId": item_uid,
                    "ItemId": item_id,
                    "IsTemp": 0,
                    "EffectUIdList": effect_uids,
                    "Active": automatically_active or item_uid in active_uids,
                }
        return item_map, effect_map

    def _holy_tower_item_values(
        self, state: Mapping[str, Any]
    ) -> List[Dict[str, Any]]:
        item_map, _effect_map = self._tower_item_payloads(state)
        return [
            {
                "ItemID": _uint(row.get("ItemId")),
                "ItemNum": 1,
                "Activate": 1 if row.get("Active") else 0,
                "EffectiveTimes": 0,
                "ItemUId": item_uid,
            }
            for item_uid, row in sorted(item_map.items())
        ]

    def _data_values(
        self, profile: Mapping[str, Any], state: Mapping[str, Any]
    ) -> Dict[str, Any]:
        map_id = _uint(state.get("map_id"), DEFAULT_MAP_ID)
        resolved = _mapping(state.get("resolved_nodes"))
        dungeon_sub_map = {
            _uint(node_id): {
                "SubId": _uint(_mapping(value).get("sub_id")),
                "CrawlTowerSubMonsterData": {},
            }
            for node_id, value in resolved.items()
            if _uint(node_id)
        }
        tower_effect_item_map, tower_effect_map = self._tower_item_payloads(state)
        item_slots = {
            _uint(slot): _uint(uid)
            for slot, uid in _mapping(state.get("item_slots")).items()
            if _uint(slot) and _uint(uid) in tower_effect_item_map
        }
        available_item_ids = sorted(
            {
                _uint(row.get("ItemId"))
                for row in tower_effect_item_map.values()
                if _uint(row.get("ItemId"))
            }
        )
        effect_combat_power = sum(
            max(
                0,
                _int(
                    self.catalog.rogue_items[item_id].get("power")
                ),
            )
            for item_id in available_item_ids
        )
        preparation = _mapping(state.get("preparation"))
        return {
            "Status": _uint(state.get("status"), CRAWL_TOWER_IDLE),
            "TowerId": _uint(state.get("tower_id"), DEFAULT_TOWER_ID),
            "PhaData": self._rich_phalanx(profile, state),
            "Context": self._context_values(profile, state),
            "Progress": self._progress_values(state),
            "BasicStageDungeonList": [
                {"DungeonId": node_id, "RemainRefreshRogueAwardNum": 0}
                for node_id in state.get("completed_nodes", ())
                if _uint(node_id) in self.catalog.nodes_by_map.get(map_id, ())
            ],
            "GospelList": [
                self._tower_gospel_values(gospel_id)
                for gospel_id in self._tower_gospel_ids(state)
            ],
            "AvailableEffectItemList": available_item_ids,
            "TowerEffectItemMap": tower_effect_item_map,
            "TowerEffectMap": tower_effect_map,
            # HeroControl.ApplyHeroFightBuff unconditionally dereferences
            # CrawlTowerData.Tactic.Id after applying the theme, cultivation
            # and item buffs.  Omitting this message therefore stalls the
            # battle scene at 99% even when no live-service tactic is
            # selected.  The defined bundle has no authoritative
            # a_rogue_tactics table, so preserve protobuf's no-selection
            # semantic with a present, zero-valued message.
            "Tactic": {
                "Id": _uint(preparation.get("tactic_id")),
                "IsNew": 0,
            },
            "ItemSlotMap": item_slots,
            "UnlockedGospelPosMap": self._unlocked_gospel_pos_map(),
            "EffectCombatPower": _uint(effect_combat_power),
            "ShopRefreshNum": 0,
            "CurStage": 1,
            "Exp": _uint(state.get("exp")),
            "TeamLevel": _uint(state.get("team_level"), 1),
            "DungeonSubMap": dungeon_sub_map,
            "FloorGroupMap": self.catalog.floor_group_map(
                _uint(state.get("difficulty"), DEFAULT_DIFFICULTY),
                _uint(state.get("layer"), DEFAULT_LAYER),
                map_id,
            ),
            "CurrentLayers": _uint(state.get("layer"), 1),
            "DungeonTypMap": {
                node_id: self.catalog.node_type(node_id)
                for node_id in self.catalog.nodes_by_map.get(map_id, ())
            },
            "InnerGoldNum": _uint(
                _mapping(state.get("opening")).get("residue_gold"), OPENING_GOLD
            ),
            "MaxUnlockDifficulty": max(self.catalog.difficulties),
        }

    def _period_values(self, state: Mapping[str, Any]) -> Dict[str, Any]:
        return {
            "TowerId": _uint(state.get("tower_id"), DEFAULT_TOWER_ID),
            "DifficultyType": _uint(state.get("difficulty"), DEFAULT_DIFFICULTY),
            "ThemeId": self.catalog.theme_id,
            "TacticList": [],
            "Passport": {"Exp": 0, "Level": 1, "LevelLimit": 1},
            "RecordChoosen": True,
            "IndependentTree": self._independent_tree(),
            "Pos1ProfessionId": _uint(
                self.catalog.people[self.catalog.default_hero_ids[0]].get("job_default")
            ),
            "Pos1HeroId": self.catalog.default_hero_ids[0],
            "IsInfiniteUnlocked": True,
            "InfiniteFirstOpen": True,
            "IsSeniorDifficultyUnlocked": True,
        }

    def _holy_data_values(
        self, profile: Mapping[str, Any], state: Mapping[str, Any]
    ) -> Dict[str, Any]:
        formation = [row for row in state.get("formation", []) if isinstance(row, Mapping)]
        gospel_ids = self._tower_gospel_ids(state)
        correspond = [
            {
                "HeroId": _uint(row.get("hero_id")),
                "GospelIdList": [
                    _uint(value)
                    for value in row.get("gospel_uids", ())
                    if _uint(value) in gospel_ids
                ],
            }
            for row in formation
        ]
        return {
            "ErrCode": ERROR_SUCCESS,
            "TowerId": _uint(state.get("tower_id"), DEFAULT_TOWER_ID),
            "TowerStatus": _uint(state.get("status"), CRAWL_TOWER_IDLE),
            # The outer key is the local formation slot (the runtime calls
            # GetHeroPos(1)); the inner map is HeroId -> zero-based combat pos.
            "HeroPos": {
                1: {
                    "Data": {
                        _uint(row.get("hero_id")): _uint(row.get("combat_pos"))
                        for row in formation
                    }
                }
            },
            "GospelList": [
                self._tower_gospel_values(gospel_id) for gospel_id in gospel_ids
            ],
            "Hcg": {1: {"Correspond": correspond}},
            "HeroAttr": {
                _uint(row.get("hero_id")): self._hero_attr(
                    profile, state, _uint(row.get("hero_id"))
                )
                for row in formation
            },
        }

    def _apply_request_context(
        self, state: Dict[str, Any], request: Mapping[str, Any]
    ) -> None:
        incoming = _mapping(_mapping(request.get("Context")).get("HeroHpMap"))
        if not incoming:
            return
        selected = {
            _uint(row.get("hero_id"))
            for row in state.get("formation", [])
            if isinstance(row, Mapping)
        }
        hp_map = copy.deepcopy(dict(_mapping(_mapping(state.get("context")).get("hero_hp"))))
        for key, value in incoming.items():
            hero_id = _uint(key)
            if hero_id in selected:
                hp_map[str(hero_id)] = _int(value)
        state["context"] = {"hero_hp": hp_map}

    def _grant_changes(
        self, profile: Mapping[str, Any], rewards: Sequence[Reward]
    ) -> Dict[str, Any]:
        if not rewards:
            return {}
        changes = self.settlement.grant_rewards(profile, tuple(rewards))
        if not isinstance(changes, Mapping):
            nested = getattr(changes, "changes", None)
            if not isinstance(nested, Mapping):
                raise TypeError("BattleSettlementLayer.grant_rewards returned no changes")
            changes = nested
        return copy.deepcopy(dict(changes))

    @staticmethod
    def _fingerprint(request: Mapping[str, Any]) -> str:
        return ":".join(
            str(value)
            for value in (
                _uint(request.get("TowerId")),
                _uint(request.get("DungeonId")),
                _uint(request.get("SubId")),
                _uint(request.get("Result")),
                int(bool(request.get("IsGiveUp"))),
            )
        )

    def _query_period(self, profile: Mapping[str, Any]) -> Result:
        state, changed = self._ensure_state(profile)
        values = {
            "PeriodData": self._period_values(state),
            "PeriodRemainTime": OFFLINE_REMAIN_TIME,
            "ThemeRemainTime": OFFLINE_REMAIN_TIME,
            "PassportLevelLimitRemainTime": OFFLINE_REMAIN_TIME,
            "InfiniteRemainTime": OFFLINE_REMAIN_TIME,
        }
        return self._result(
            CS_REQUEST_CRAWL_TOWER_PERIOD_DATA,
            values,
            changes=self._state_changes(state, changed),
        )

    def _query_remain_time(self) -> Result:
        return self._result(
            CS_REQUEST_CRAWL_TOWER_REMAIN_TIME,
            {
                "Errorcode": ERROR_SUCCESS,
                "PeriodRemainTime": OFFLINE_REMAIN_TIME,
                "ThemeRemainTime": OFFLINE_REMAIN_TIME,
                "PassportLevelLimitRemainTime": OFFLINE_REMAIN_TIME,
                "InfiniteRemainTime": OFFLINE_REMAIN_TIME,
            },
        )

    def _query_data(self, profile: Mapping[str, Any]) -> Result:
        state, changed = self._ensure_state(profile)
        return self._result(
            CS_REQUEST_CRAWL_TOWER_DATA,
            {
                "Preparation": self._preparation_values(state),
                "Data": self._data_values(profile, state),
            },
            changes=self._state_changes(state, changed),
        )

    def _request_tower_effect_items(
        self, profile: Mapping[str, Any]
    ) -> Result:
        state, changed = self._ensure_state(profile)
        item_map, _effect_map = self._tower_item_payloads(state)
        return self._result(
            CS_REQUEST_TOWER_EFFECT_ITEMS,
            {"EffectItemList": list(item_map.values())},
            changes=self._state_changes(state, changed),
        )

    def _get_holy_tower_item_activate_effect_list(
        self, profile: Mapping[str, Any]
    ) -> Result:
        state, changed = self._ensure_state(profile)
        return self._result(
            CS_GET_HOLY_TOWER_ITEM_ACTIVATE_EFFECT_LIST,
            {
                "ErrorCode": ERROR_SUCCESS,
                "EffectItemList": self._holy_tower_item_values(state),
            },
            changes=self._state_changes(state, changed),
        )

    def _activate_holy_tower_item_effect(
        self,
        profile: Mapping[str, Any],
        request: Mapping[str, Any],
    ) -> Result:
        state, _changed = self._ensure_state(profile)
        operation = _uint(request.get("Typ"))
        item_uid = _uint(request.get("ItemUId"))
        item_map, _effect_map = self._tower_item_payloads(state)
        item = item_map.get(item_uid)
        if operation not in {1, 2} or item is None:
            return self._error(
                CS_ACTIVATE_HOLY_TOWER_ITEM_EFFECT,
                ERROR_CLIENT_REQUEST_PARAM,
                "invalid Holy Tower item activation request",
            )
        definition = self.catalog.rogue_items.get(_uint(item.get("ItemId")), {})
        if (
            _uint(definition.get("item_type")) != 4
            or _uint(definition.get("can_use")) == 0
        ):
            return self._error(
                CS_ACTIVATE_HOLY_TOWER_ITEM_EFFECT,
                ERROR_CLIENT_REQUEST_PARAM,
                "the selected Holy Tower item is not an activatable consumable",
            )

        active_uids = {
            _uint(value)
            for value in state.get("tower_item_active_uids", ())
            if _uint(value) in item_map
        }
        if operation == 1:
            active_uids.add(item_uid)
        else:
            active_uids.discard(item_uid)
        state["tower_item_active_uids"] = sorted(active_uids)
        return self._result(
            CS_ACTIVATE_HOLY_TOWER_ITEM_EFFECT,
            {
                "ErrorCode": ERROR_SUCCESS,
                "Typ": operation,
                "ItemUId": item_uid,
            },
            changes=self._state_changes(state),
        )

    def _set_difficulty(
        self, profile: Mapping[str, Any], request: Mapping[str, Any]
    ) -> Result:
        state, _changed = self._ensure_state(profile)
        difficulty = _uint(request.get("DifficultyType"))
        if difficulty not in self.catalog.difficulties:
            return self._error(
                CS_CRAWL_TOWER_SET_DIFFICULTY,
                ERROR_CLIENT_REQUEST_PARAM,
                f"unknown Tower difficulty {difficulty}",
            )
        if _uint(state.get("status")) != CRAWL_TOWER_IDLE:
            return self._error(
                CS_CRAWL_TOWER_SET_DIFFICULTY,
                ERROR_CLIENT_REQUEST_PARAM,
                "difficulty cannot change during a running Tower survey",
            )
        state["difficulty"] = difficulty
        return self._result(
            CS_CRAWL_TOWER_SET_DIFFICULTY,
            {"Errorcode": ERROR_SUCCESS, "DifficultyType": difficulty},
            changes=self._state_changes(state),
        )

    def _show_hero_data(
        self, profile: Mapping[str, Any], request: Mapping[str, Any]
    ) -> Result:
        state, changed = self._ensure_state(profile)
        tower_id = _uint(request.get("TowerId"), DEFAULT_TOWER_ID)
        if tower_id != DEFAULT_TOWER_ID:
            return self._error(
                CS_REQUEST_TOWER_SHOW_HERO_DATA,
                ERROR_CLIENT_REQUEST_PARAM,
                "unknown tower",
                tower_id=tower_id,
            )
        hero_ids = self.catalog.heroes_for_difficulty(
            _uint(state.get("difficulty"), DEFAULT_DIFFICULTY)
        )
        level = self.catalog.witch_level_for_exp(_uint(state.get("exp")))
        return self._result(
            CS_REQUEST_TOWER_SHOW_HERO_DATA,
            {
                "Errorcode": ERROR_SUCCESS,
                "TowerId": tower_id,
                "HeroDataList": [
                    self._simple_hero_values(profile, hero_id, level)
                    for hero_id in hero_ids
                ],
                "HeroGospelClassMap": self._gospel_class_map(state),
            },
            changes=self._state_changes(state, changed),
        )

    def _refresh_gospel_classes(
        self, profile: Mapping[str, Any], request: Mapping[str, Any]
    ) -> Result:
        state, _changed = self._ensure_state(profile)
        valid_heroes = set(
            self.catalog.heroes_for_difficulty(
                _uint(state.get("difficulty"), DEFAULT_DIFFICULTY)
            )
        )
        requested = []
        for value in request.get("HeroIdList", ()):
            hero_id = _uint(value)
            if hero_id in valid_heroes and hero_id not in requested:
                requested.append(hero_id)
        if not requested:
            requested = [
                _uint(row.get("hero_id"))
                for row in state.get("formation", ())
                if isinstance(row, Mapping)
            ]
        except_map = _mapping(request.get("ExceptGospelClassMap"))
        classes = self.catalog.gospel_classes
        class_map: Dict[int, int] = {}
        for index, hero_id in enumerate(requested):
            excluded = _uint(except_map.get(hero_id, except_map.get(str(hero_id))))
            options = [value for value in classes if value != excluded]
            class_map[hero_id] = options[index % len(options)]
        preparation = copy.deepcopy(dict(_mapping(state.get("preparation"))))
        stored = copy.deepcopy(
            dict(_mapping(preparation.get("hero_gospel_class_map")))
        )
        stored.update({str(key): value for key, value in class_map.items()})
        preparation["hero_gospel_class_map"] = stored
        refresh_count = _uint(preparation.get("gospel_class_refresh_count")) + 1
        preparation["gospel_class_refresh_count"] = refresh_count
        state["preparation"] = preparation
        return self._result(
            CS_TOWER_REFRESH_GOSPEL_CLASS,
            {
                "Errorcode": ERROR_SUCCESS,
                "HeroGospelClassMap": class_map,
                "RemainRefreshGospelClassNum": max(
                    0, OPENING_REFRESH_LIMIT - refresh_count
                ),
            },
            changes=self._state_changes(state),
        )

    def _candidate_gospels(
        self, profile: Mapping[str, Any], request: Mapping[str, Any]
    ) -> Result:
        state, changed = self._ensure_state(profile)
        tower_id = _uint(request.get("TowerId"), DEFAULT_TOWER_ID)
        gospel_class = _uint(request.get("GospelClass"))
        if tower_id != DEFAULT_TOWER_ID or gospel_class not in self.catalog.gospel_classes:
            return self._error(
                CS_REQUEST_TOWER_CANDIDATE_GOSPEL_LIST,
                ERROR_CLIENT_REQUEST_PARAM,
                "unknown tower or gospel class",
                tower_id=tower_id,
            )
        amount = max(
            1,
            _uint(
                self.catalog.difficulties[
                    _uint(state.get("difficulty"), DEFAULT_DIFFICULTY)
                ].get("gospel_amount"),
                1,
            ),
        )
        gospel_ids = list(self.catalog.gospel_ids_for_class(gospel_class))[:amount]
        return self._result(
            CS_REQUEST_TOWER_CANDIDATE_GOSPEL_LIST,
            {
                "Errorcode": ERROR_SUCCESS,
                "TowerId": tower_id,
                "GospelClass": gospel_class,
                "GospelList": [
                    self._simple_gospel_values(gospel_id)
                    for gospel_id in gospel_ids
                ],
            },
            changes=self._state_changes(state, changed),
        )

    def _generate_open_items(self, profile: Mapping[str, Any]) -> Result:
        state, changed = self._ensure_state(profile)
        difficulty = _uint(state.get("difficulty"), DEFAULT_DIFFICULTY)
        item_ids = list(self.catalog.initial_items[difficulty][:3])
        return self._result(
            CS_TOWER_GENERATE_OPEN_ITEMS,
            {"ItemIdList": item_ids},
            changes=self._state_changes(state, changed),
        )

    def _available_gospel_classes(self, profile: Mapping[str, Any]) -> Result:
        state, changed = self._ensure_state(profile)
        preparation = _mapping(state.get("preparation"))
        stored = _mapping(preparation.get("available_gospel_classes"))
        values = {
            gospel_class: max(
                0,
                _uint(stored.get(str(gospel_class), stored.get(gospel_class)), 1),
            )
            for gospel_class in self.catalog.gospel_classes
        }
        return self._result(
            CS_REQUEST_AVAILABLE_TOWER_GOSPEL_CLASSES,
            {
                "Errorcode": ERROR_SUCCESS,
                "GospelClassMap": values,
                "AvailableGospelClassNum": sum(values.values()),
            },
            changes=self._state_changes(state, changed),
        )

    def _choose_gospel_class(
        self, profile: Mapping[str, Any], request: Mapping[str, Any]
    ) -> Result:
        state, _changed = self._ensure_state(profile)
        gospel_class = _uint(request.get("GospelClass"))
        operation = _uint(request.get("ChooseOrGiveup"))
        if gospel_class not in self.catalog.gospel_classes or operation not in {0, 1}:
            return self._error(
                CS_CHOOSE_TOWER_GOSPEL_CLASS,
                ERROR_CLIENT_REQUEST_PARAM,
                "invalid gospel-class choice",
            )
        preparation = copy.deepcopy(dict(_mapping(state.get("preparation"))))
        values = {
            value: 1 for value in self.catalog.gospel_classes
        }
        stored = _mapping(preparation.get("available_gospel_classes"))
        for value in values:
            values[value] = max(
                0, _uint(stored.get(str(value), stored.get(value)), values[value])
            )
        if operation == 1 and values[gospel_class] > 0:
            values[gospel_class] -= 1
        preparation["available_gospel_classes"] = {
            str(key): value for key, value in values.items()
        }
        state["preparation"] = preparation
        return self._result(
            CS_CHOOSE_TOWER_GOSPEL_CLASS,
            {
                "Errorcode": ERROR_SUCCESS,
                "GospelClass": gospel_class,
                "ChooseOrGiveup": operation,
                "GospelClassMap": values,
                "AvailableGospelClassNum": sum(values.values()),
            },
            changes=self._state_changes(state),
        )

    def _prepare(
        self, profile: Mapping[str, Any], request: Mapping[str, Any]
    ) -> Result:
        state, _changed = self._ensure_state(profile)
        tower_id = _uint(request.get("TowerId"), DEFAULT_TOWER_ID)
        stage = _uint(request.get("Stage"))
        if tower_id != DEFAULT_TOWER_ID or stage > PREPARATION_FINISHED:
            return self._error(
                CS_CRAWL_TOWER_PREPARE,
                ERROR_CLIENT_REQUEST_PARAM,
                "invalid Tower preparation stage",
                tower_id=tower_id,
            )
        preparation = copy.deepcopy(dict(_mapping(state.get("preparation"))))
        preparation["stage"] = stage
        value = _uint(request.get("Value"))
        value_map = _mapping(request.get("ValueMap"))
        if stage == PREPARATION_TACTIC:
            preparation["tactic_id"] = 0
        elif stage == PREPARATION_OPEN_ITEM:
            difficulty = _uint(state.get("difficulty"), DEFAULT_DIFFICULTY)
            if value not in self.catalog.initial_items[difficulty]:
                return self._error(
                    CS_CRAWL_TOWER_PREPARE,
                    ERROR_CLIENT_REQUEST_PARAM,
                    "initial Tower item is not in the configured pool",
                    tower_id=tower_id,
                )
            preparation["open_item_id"] = value
        elif stage == PREPARATION_HERO:
            positions = _mapping(request.get("HeroCombatPosMap"))
            rows = [
                {"HeroId": _uint(hero_id), "CombatPos": _int(position)}
                for hero_id, position in positions.items()
            ]
            state["formation"] = self._normalize_formation(profile, rows)
            valid_classes = set(self.catalog.gospel_classes)
            preparation["hero_gospel_class_map"] = {
                str(_uint(hero_id)): _uint(gospel_class)
                for hero_id, gospel_class in value_map.items()
                if _uint(hero_id)
                and _uint(gospel_class) in valid_classes
            }
        elif stage == PREPARATION_GOSPEL:
            preparation["hero_gospel_map"] = {
                str(_uint(hero_id)): _uint(gospel_id)
                for hero_id, gospel_id in value_map.items()
                if _uint(hero_id) and _uint(gospel_id) in self.catalog.gospel_pool
            }
        state["preparation"] = preparation
        return self._result(
            CS_CRAWL_TOWER_PREPARE,
            {
                "Errorcode": ERROR_SUCCESS,
                "Preparation": self._preparation_values(state),
            },
            changes=self._state_changes(state),
        )

    def _opening_choose_hero(
        self, profile: Mapping[str, Any], request: Mapping[str, Any]
    ) -> Result:
        state, _changed = self._ensure_state(profile)
        step = _uint(request.get("Step"))
        valid = set(
            self.catalog.heroes_for_difficulty(
                _uint(state.get("difficulty"), DEFAULT_DIFFICULTY)
            )
        )
        hero_ids = []
        for value in request.get("HeroList", ()):
            hero_id = _uint(value)
            if hero_id in valid and hero_id not in hero_ids:
                hero_ids.append(hero_id)
        if (
            _uint(state.get("status")) != CRAWL_TOWER_IDLE
            or step != STEP_CHOOSE_HERO
            or _uint(state.get("step")) != STEP_CHOOSE_HERO
            or len(hero_ids) != 3
        ):
            return self._error(
                CS_OPENING_CHOOSE_HERO,
                ERROR_CLIENT_REQUEST_PARAM,
                "opening hero selection must contain three valid witches",
            )
        state["formation"] = [
            self._formation_row(profile, hero_id, position)
            for position, hero_id in enumerate(hero_ids)
        ]
        opening = copy.deepcopy(dict(_mapping(state.get("opening"))))
        opening.update(
            selected_hero_ids=hero_ids,
            selected_gospel_ids=[],
            candidate_gospel_ids=[],
            candidate_step=0,
            refresh_count=0,
            residue_gold=OPENING_GOLD,
        )
        state["opening"] = opening
        state["tower_gospels"] = []
        state["step"] = STEP_CHOOSE_GOSPEL_1
        preparation = copy.deepcopy(dict(_mapping(state.get("preparation"))))
        preparation["stage"] = PREPARATION_HERO
        preparation["hero_gospel_class_map"] = {
            str(key): value for key, value in self._gospel_class_map(state).items()
        }
        state["preparation"] = preparation
        return self._result(
            CS_OPENING_CHOOSE_HERO,
            {"Errorcode": ERROR_SUCCESS, "NextStep": STEP_CHOOSE_GOSPEL_1},
            changes=self._state_changes(state),
        )

    def _opening_candidates(
        self, profile: Mapping[str, Any], request: Mapping[str, Any]
    ) -> Result:
        state, _changed = self._ensure_state(profile)
        step = _uint(request.get("Step"))
        if step not in {
            STEP_CHOOSE_GOSPEL_1,
            STEP_CHOOSE_GOSPEL_2,
            STEP_CHOOSE_GOSPEL_3,
        } or step != _uint(state.get("step")):
            return self._error(
                CS_GET_OPENING_GOSPEL_LIST,
                ERROR_CLIENT_REQUEST_PARAM,
                "opening gospel step does not match persisted state",
            )
        opening = copy.deepcopy(dict(_mapping(state.get("opening"))))
        candidates = [
            _uint(value) for value in opening.get("candidate_gospel_ids", ())
        ]
        if _uint(opening.get("candidate_step")) != step or not candidates:
            candidates = list(
                self.catalog.opening_gospel_ids(
                    step,
                    _uint(opening.get("refresh_count")),
                    opening.get("selected_gospel_ids", ()),
                )
            )
            opening["candidate_gospel_ids"] = candidates
            opening["candidate_step"] = step
            state["opening"] = opening
        return self._result(
            CS_GET_OPENING_GOSPEL_LIST,
            {
                "Errorcode": ERROR_SUCCESS,
                "GospelList": [
                    self._opening_gospel_values(gospel_id)
                    for gospel_id in candidates
                ],
                "RefreshTimes": _uint(opening.get("refresh_count")),
                "ResidueGold": _uint(opening.get("residue_gold"), OPENING_GOLD),
            },
            changes=self._state_changes(state),
        )

    def _opening_choose_gospel(
        self, profile: Mapping[str, Any], request: Mapping[str, Any]
    ) -> Result:
        state, _changed = self._ensure_state(profile)
        step = _uint(request.get("Step"))
        operation = _uint(request.get("Typ"))
        if step not in {
            STEP_CHOOSE_GOSPEL_1,
            STEP_CHOOSE_GOSPEL_2,
            STEP_CHOOSE_GOSPEL_3,
        } or step != _uint(state.get("step")):
            return self._error(
                CS_OPENING_CHOOSE_GOSPEL,
                ERROR_CLIENT_REQUEST_PARAM,
                "opening gospel step does not match persisted state",
            )
        opening = copy.deepcopy(dict(_mapping(state.get("opening"))))
        if operation == 2:
            refresh_count = min(
                OPENING_REFRESH_LIMIT,
                _uint(opening.get("refresh_count")) + 1,
            )
            candidates = list(
                self.catalog.opening_gospel_ids(
                    step,
                    refresh_count,
                    opening.get("selected_gospel_ids", ()),
                )
            )
            opening["refresh_count"] = refresh_count
            opening["candidate_step"] = step
            opening["candidate_gospel_ids"] = candidates
            state["opening"] = opening
            return self._result(
                CS_OPENING_CHOOSE_GOSPEL,
                {
                    "Errorcode": ERROR_SUCCESS,
                    "GospelList": [
                        self._opening_gospel_values(gospel_id)
                        for gospel_id in candidates
                    ],
                    "RefreshTimes": refresh_count,
                    "NextStep": step,
                    "ResidueGold": _uint(
                        opening.get("residue_gold"), OPENING_GOLD
                    ),
                    "Typ": operation,
                },
                changes=self._state_changes(state),
            )
        selected = []
        current = {
            _uint(value) for value in opening.get("candidate_gospel_ids", ())
        }
        already = {
            _uint(value) for value in opening.get("selected_gospel_ids", ())
        }
        for value in request.get("GospelIDList", ()):
            gospel_id = _uint(value)
            if gospel_id in current and gospel_id not in already and gospel_id not in selected:
                selected.append(gospel_id)
        if operation != 1 or len(selected) != OPENING_CHOOSE_COUNT:
            return self._error(
                CS_OPENING_CHOOSE_GOSPEL,
                ERROR_CLIENT_REQUEST_PARAM,
                "opening selection must choose two current gospel candidates",
            )
        all_selected = list(opening.get("selected_gospel_ids", ())) + selected
        next_step = step + 1
        opening["selected_gospel_ids"] = all_selected
        opening["candidate_gospel_ids"] = []
        opening["candidate_step"] = 0
        opening["refresh_count"] = 0
        state["opening"] = opening
        state["tower_gospels"] = all_selected
        state["step"] = next_step
        return self._result(
            CS_OPENING_CHOOSE_GOSPEL,
            {
                "Errorcode": ERROR_SUCCESS,
                "GospelList": [],
                "RefreshTimes": 0,
                "NextStep": next_step,
                "ResidueGold": _uint(opening.get("residue_gold"), OPENING_GOLD),
                "Typ": operation,
            },
            changes=self._state_changes(state),
        )

    def _init(self, profile: Mapping[str, Any], request: Mapping[str, Any]) -> Result:
        previous, _changed = self._ensure_state(profile)
        tower_id = _uint(request.get("TowerId"), DEFAULT_TOWER_ID)
        if tower_id != DEFAULT_TOWER_ID:
            return self._error(CS_CRAWL_TOWER_INIT, ERROR_CLIENT_REQUEST_PARAM, "unknown tower")
        previous_step = _uint(previous.get("step"), STEP_CHOOSE_HERO)
        if (
            _uint(previous.get("status")) == CRAWL_TOWER_IDLE
            and previous_step
            in {
                STEP_CHOOSE_GOSPEL_1,
                STEP_CHOOSE_GOSPEL_2,
                STEP_CHOOSE_GOSPEL_3,
            }
        ):
            return self._error(
                CS_CRAWL_TOWER_INIT,
                ERROR_CLIENT_REQUEST_PARAM,
                "opening gospel selection is incomplete",
            )
        # Starting a survey is a single state transition.  Do not retain map,
        # HP, reward, or battle residue from an abandoned/settled attempt.
        state = self._default_state(profile)
        state["status"] = CRAWL_TOWER_RUNNING
        state["started_at"] = _uint(self.now())
        state["step"] = STEP_RUNNING
        state["difficulty"] = _uint(
            previous.get("difficulty"), DEFAULT_DIFFICULTY
        )
        opening = copy.deepcopy(dict(_mapping(previous.get("opening"))))
        raw_selected = opening.get("selected_gospel_ids", ())
        if not isinstance(raw_selected, Sequence) or isinstance(
            raw_selected, (str, bytes)
        ):
            raw_selected = ()
        selected_gospels = [
            _uint(value)
            for value in raw_selected
            if _uint(value) in self.catalog.gospel_pool
        ]
        if len(selected_gospels) != 6:
            # Compatibility path for legacy saves and direct-init tests.  The
            # normal runtime path reaches step 105 with six player choices.
            selected_gospels = list(self.catalog.gospel_ids_for_class()[:6])
        opening.update(
            selected_gospel_ids=selected_gospels,
            candidate_gospel_ids=[],
            candidate_step=0,
            refresh_count=0,
            residue_gold=_uint(opening.get("residue_gold"), OPENING_GOLD),
        )
        state["opening"] = opening
        state["tower_gospels"] = selected_gospels
        state["formation"] = self._normalize_formation(
            profile, previous.get("formation")
        )
        pha = _mapping(request.get("Pha"))
        if pha.get("HeroList"):
            state["formation"] = self._normalize_formation(profile, pha.get("HeroList"))
        for index, row in enumerate(state["formation"]):
            row["gospel_uids"] = selected_gospels[
                index * OPENING_CHOOSE_COUNT : (index + 1) * OPENING_CHOOSE_COUNT
            ]
        preparation = copy.deepcopy(dict(_mapping(previous.get("preparation"))))
        preparation["stage"] = PREPARATION_FINISHED
        preparation["tactic_id"] = 0
        state["preparation"] = preparation
        witch_level = self.catalog.witch_level_for_exp(_uint(state.get("exp")))
        state["context"] = {
            "hero_hp": {
                str(row["hero_id"]): max(
                    1,
                    _uint(
                        self._tower_hero_snapshot(
                            profile, row["hero_id"], witch_level
                        ).hp
                    ),
                )
                for row in state["formation"]
            }
        }
        return self._result(
            CS_CRAWL_TOWER_INIT,
            {
                "Errorcode": ERROR_SUCCESS,
                "Data": self._data_values(profile, state),
                "InitItemId": _uint(preparation.get("open_item_id")),
                "IndependentTree": self._independent_tree(),
                "PhaAutoFight": self._phalanx_auto_fight_values(state),
            },
            changes=self._state_changes(state),
        )

    def _get_phalanx(self, profile: Mapping[str, Any]) -> Result:
        state, changed = self._ensure_state(profile)
        return self._result(
            CS_GET_CRAWL_TOWER_PHALANX,
            {
                "Errorcode": ERROR_SUCCESS,
                "PhaData": self._rich_phalanx(profile, state),
                "PhaAutoFight": self._phalanx_auto_fight_values(state),
            },
            changes=self._state_changes(state, changed),
        )

    def _set_phalanx(
        self, profile: Mapping[str, Any], request: Mapping[str, Any]
    ) -> Result:
        state, _changed = self._ensure_state(profile)
        pha = _mapping(request.get("Pha"))
        state["formation"] = self._normalize_formation(profile, pha.get("HeroList"))
        previous_hp = _mapping(_mapping(state.get("context")).get("hero_hp"))
        witch_level = self.catalog.witch_level_for_exp(_uint(state.get("exp")))
        state["context"] = {
            "hero_hp": {
                str(row["hero_id"]): _int(
                    previous_hp.get(str(row["hero_id"])),
                    max(
                        1,
                        _uint(
                            self._tower_hero_snapshot(
                                profile, row["hero_id"], witch_level
                            ).hp
                        ),
                    ),
                )
                for row in state["formation"]
            }
        }
        return self._result(
            CS_SET_CRAWL_TOWER_PHALANX,
            {
                "Errorcode": ERROR_SUCCESS,
                "PhaData": self._rich_phalanx(profile, state),
                "Context": self._context_values(profile, state),
                "PhaAutoFight": self._phalanx_auto_fight_values(state),
            },
            changes=self._state_changes(state),
        )

    def _event_effect_values(
        self,
        profile: Mapping[str, Any],
        state: Dict[str, Any],
        option_id: int,
    ) -> Dict[int, str]:
        """Apply the recoverable local subset of ``a_event_selection`` effects.

        The runtime uses this map to substitute placeholders such as ``{7}``
        and ``{78}`` in the event result text.  Returning a successful but
        empty map is what produced the blank, non-clickable event panel.
        """

        option = self.catalog.event_options[int(option_id)]
        raw_effects = _json(
            option.get("func_effect"),
            f"a_event_selection[{option_id}].func_effect",
        )
        effects = raw_effects if isinstance(raw_effects, list) else []
        values: Dict[int, str] = {}
        opening = copy.deepcopy(dict(_mapping(state.get("opening"))))
        tower_items = copy.deepcopy(dict(_mapping(state.get("tower_items"))))
        tower_gospels = list(self._tower_gospel_ids(state))
        context = copy.deepcopy(dict(_mapping(state.get("context"))))
        hp_map = copy.deepcopy(dict(_mapping(context.get("hero_hp"))))

        for index, raw in enumerate(effects):
            if not isinstance(raw, Mapping):
                continue
            effect = _uint(raw.get("effect"))
            value1 = _int(raw.get("value1"))
            if not effect:
                continue
            if effect == 7:
                opening["residue_gold"] = max(
                    0, _uint(opening.get("residue_gold"), OPENING_GOLD) + value1
                )
                values[effect] = str(abs(value1))
            elif effect == 78:
                candidates = [
                    item_id
                    for item_id, row in sorted(self.catalog.rogue_items.items())
                    if _uint(row.get("weight")) > 0
                ]
                if candidates:
                    item_id = candidates[(int(option_id) + index) % len(candidates)]
                    key = str(item_id)
                    count = max(1, _uint(self.catalog.rogue_items[item_id].get("count"), 1))
                    tower_items[key] = _uint(tower_items.get(key)) + count
                    values[effect] = str(
                        self.catalog.rogue_items[item_id].get("name") or item_id
                    )
            elif effect == 101:
                candidates = [
                    gospel_id
                    for gospel_id in self.catalog.gospel_ids_for_class()
                    if gospel_id not in tower_gospels
                ]
                if candidates:
                    gospel_id = candidates[int(option_id) % len(candidates)]
                    tower_gospels.append(gospel_id)
                    values[effect] = str(
                        self.catalog.gospels[gospel_id].get("name") or gospel_id
                    )
            elif effect in {102, 103}:
                if tower_gospels:
                    gospel_id = tower_gospels[int(option_id) % len(tower_gospels)]
                    values[effect] = str(
                        self.catalog.gospels[gospel_id].get("name") or gospel_id
                    )
            elif effect == 105:
                witch_level = self.catalog.witch_level_for_exp(
                    _uint(state.get("exp"))
                )
                for row in state.get("formation", ()):
                    if not isinstance(row, Mapping):
                        continue
                    hero_id = _uint(row.get("hero_id"))
                    key = str(hero_id)
                    maximum = max(
                        1,
                        _uint(
                            self._tower_hero_snapshot(
                                profile, hero_id, witch_level
                            ).hp
                        ),
                    )
                    current = _int(hp_map.get(key, hp_map.get(hero_id)), maximum)
                    delta = maximum * value1 // 10_000
                    hp_map[key] = min(maximum, max(1, current + delta))
                values[effect] = str(abs(value1) / 100).rstrip("0").rstrip(".")
            else:
                values[effect] = str(value1 or _uint(raw.get("value2")) or 1)

        state["opening"] = opening
        state["tower_items"] = tower_items
        state["tower_gospels"] = tower_gospels
        context["hero_hp"] = hp_map
        state["context"] = context
        return values

    def _click_event_option(
        self, profile: Mapping[str, Any], request: Mapping[str, Any]
    ) -> Result:
        state, _changed = self._ensure_state(profile)
        dungeon_id = _uint(request.get("DungeonId"))
        event_id = _uint(request.get("TowerEventId"))
        option_id = _uint(request.get("OptionId"))
        resolved = _mapping(_mapping(state.get("resolved_nodes")).get(str(dungeon_id)))
        root_options = set(self.catalog.event_option_ids(event_id))
        allowed_options = set(root_options)
        for root_option in root_options:
            allowed_options.update(self.catalog.event_suboption_ids(root_option))
        if (
            _uint(state.get("status")) != CRAWL_TOWER_RUNNING
            or _uint(resolved.get("event_id")) != event_id
            or option_id not in allowed_options
        ):
            return self._error(
                CS_CLICK_TOWER_EVENT_OPTION,
                ERROR_CLIENT_REQUEST_PARAM,
                "Tower event or option does not match the entered node",
                dungeon_id=dungeon_id,
                sub_id=event_id,
            )

        runtime = copy.deepcopy(dict(_mapping(state.get("event_runtime"))))
        cached = _mapping(runtime.get("last_response"))
        if (
            _uint(cached.get("DungeonId")) == dungeon_id
            and _uint(cached.get("TowerEventId")) == event_id
            and _uint(cached.get("OptionId")) == option_id
        ):
            return self._result(
                CS_CLICK_TOWER_EVENT_OPTION,
                cached,
                changes=self._state_changes(state, _changed),
                replayed=True,
            )

        option = self.catalog.event_options[option_id]
        opening = copy.deepcopy(dict(_mapping(state.get("opening"))))
        cost = _uint(option.get("cost"))
        residue_gold = _uint(opening.get("residue_gold"), OPENING_GOLD)
        if cost > residue_gold:
            return self._error(
                CS_CLICK_TOWER_EVENT_OPTION,
                ERROR_CLIENT_REQUEST_PARAM,
                "not enough Holy Tower gold for the selected event option",
                dungeon_id=dungeon_id,
                sub_id=event_id,
            )
        opening["residue_gold"] = residue_gold - cost
        state["opening"] = opening

        selected_options = list(runtime.get("selected_options", ()))
        if option_id not in selected_options:
            selected_options.append(option_id)
        runtime["selected_options"] = selected_options
        sub_options = list(self.catalog.event_suboption_ids(option_id))
        action, battle_type = self.catalog.event_action(option_id)
        effect_values = self._event_effect_values(profile, state, option_id)
        special_dungeon_id = 0

        if action == 8:
            try:
                special = self.catalog.resolve_special_battle(
                    _uint(state.get("difficulty")),
                    _uint(state.get("layer")),
                    battle_type,
                )
            except TowerBattleConfigError as exc:
                return self._error(
                    CS_CLICK_TOWER_EVENT_OPTION,
                    ERROR_CONFIG,
                    str(exc),
                    dungeon_id=dungeon_id,
                    sub_id=event_id,
                )
            special_dungeon_id = _uint(special.get("sub_id"))
            runtime["pending_battle"] = {
                "dungeon_id": dungeon_id,
                "event_id": event_id,
                "option_id": option_id,
                **copy.deepcopy(special),
            }
        else:
            completed = list(runtime.get("completed_event_ids", ()))
            if event_id not in completed:
                completed.append(event_id)
            runtime["completed_event_ids"] = completed
            battle_id = max(1, _uint(state.get("next_battle_id"), 1))
            state["next_battle_id"] = battle_id + 1
            state["active_battle"] = {
                "kind": "event",
                "battle_id": battle_id,
                "tower_id": _uint(state.get("tower_id")),
                "dungeon_id": dungeon_id,
                "sub_id": event_id,
                "node_type": _uint(resolved.get("node_type")),
                "reward_exp": 0,
                "rewards": [],
                "started_at": _uint(self.now()),
            }

        values = {
            "Errorcode": ERROR_SUCCESS,
            "DungeonId": dungeon_id,
            "TowerEventId": event_id,
            "OptionId": option_id,
            "ReChooseOptionId": 0,
            "SubOptionList": sub_options,
            "RandValue": (option_id % 100) + 1,
            "SpecialDungeonId": special_dungeon_id,
            "EffectValueMap": effect_values,
        }
        runtime["last_response"] = copy.deepcopy(values)
        state["event_runtime"] = runtime
        return self._result(
            CS_CLICK_TOWER_EVENT_OPTION,
            values,
            changes=self._state_changes(state),
            metadata={"event_action": action, "battle_type": battle_type},
        )

    def _shop_entry(
        self, state: Dict[str, Any], dungeon_id: int, *, refresh: bool = False
    ) -> Dict[str, Any]:
        resolved = _mapping(_mapping(state.get("resolved_nodes")).get(str(dungeon_id)))
        shop_id = _uint(resolved.get("shop_id"))
        if not shop_id:
            raise TowerBattleConfigError(
                f"Holy Tower dungeon {dungeon_id} is not an entered shop node"
            )
        runtime = copy.deepcopy(dict(_mapping(state.get("shop_runtime"))))
        existing = copy.deepcopy(dict(_mapping(runtime.get(str(dungeon_id)))))
        refresh_times = _uint(existing.get("refresh_times"))
        if refresh:
            refresh_times = min(SHOP_REFRESH_LIMIT, refresh_times + 1)
        item_ids = existing.get("item_ids", ())
        if refresh or not isinstance(item_ids, Sequence) or not item_ids:
            item_ids = list(
                self.catalog.shop_inventory(
                    shop_id,
                    dungeon_id + refresh_times * 10_007,
                )
            )
            purchased: List[int] = []
        else:
            item_ids = [
                item_id
                for item_id in (_uint(value) for value in item_ids)
                if item_id in self.catalog.rogue_items
            ]
            purchased = [
                item_id
                for item_id in (
                    _uint(value) for value in existing.get("purchased", ())
                )
                if item_id in item_ids
            ]
        entry = {
            "shop_id": shop_id,
            "item_ids": list(item_ids),
            "purchased": list(dict.fromkeys(purchased)),
            "refresh_times": refresh_times,
        }
        runtime[str(dungeon_id)] = entry
        state["shop_runtime"] = runtime
        return entry

    def _shop_item_values(self, entry: Mapping[str, Any]) -> List[Dict[str, int]]:
        purchased = {_uint(value) for value in entry.get("purchased", ())}
        return [
            {
                "ItemId": item_id,
                "ItemNum": max(
                    1, _uint(self.catalog.rogue_items[item_id].get("count"), 1)
                ),
                "Purchased": int(item_id in purchased),
            }
            for item_id in (_uint(value) for value in entry.get("item_ids", ()))
            if item_id in self.catalog.rogue_items
        ]

    def _get_shop_items(
        self, profile: Mapping[str, Any], request: Mapping[str, Any]
    ) -> Result:
        state, _changed = self._ensure_state(profile)
        dungeon_id = _uint(request.get("DungeonId"))
        try:
            entry = self._shop_entry(state, dungeon_id)
        except TowerBattleConfigError as exc:
            return self._error(
                CS_GET_TOWER_SHOP_ITEM_LIST,
                ERROR_CLIENT_REQUEST_PARAM,
                str(exc),
                dungeon_id=dungeon_id,
            )
        return self._result(
            CS_GET_TOWER_SHOP_ITEM_LIST,
            {
                "ErrorCode": ERROR_SUCCESS,
                "ItemList": self._shop_item_values(entry),
                "RefreshTimes": _uint(entry.get("refresh_times")),
                "ResidueGold": _uint(
                    _mapping(state.get("opening")).get("residue_gold"), OPENING_GOLD
                ),
            },
            changes=self._state_changes(state),
        )

    def _refresh_or_buy_shop_item(
        self, profile: Mapping[str, Any], request: Mapping[str, Any]
    ) -> Result:
        state, _changed = self._ensure_state(profile)
        dungeon_id = _uint(request.get("DungeonId"))
        typ = _uint(request.get("Typ"))
        try:
            entry = self._shop_entry(state, dungeon_id)
        except TowerBattleConfigError as exc:
            return self._error(
                CS_REFRESH_TOWER_SHOP_ITEM_LIST,
                ERROR_CLIENT_REQUEST_PARAM,
                str(exc),
                dungeon_id=dungeon_id,
            )
        if typ == 1:
            refresh_times = _uint(entry.get("refresh_times"))
            costs = self.catalog.shop_refresh_costs
            price = costs[min(refresh_times, len(costs) - 1)] if costs else 0
            opening = copy.deepcopy(dict(_mapping(state.get("opening"))))
            gold = _uint(opening.get("residue_gold"), OPENING_GOLD)
            if price > gold:
                return self._error(
                    CS_REFRESH_TOWER_SHOP_ITEM_LIST,
                    ERROR_CLIENT_REQUEST_PARAM,
                    "not enough Holy Tower gold to refresh the shop",
                    dungeon_id=dungeon_id,
                )
            opening["residue_gold"] = gold - price
            state["opening"] = opening
            entry = self._shop_entry(state, dungeon_id, refresh=True)
        elif typ == 2:
            item_id = _uint(request.get("BuyItemId"))
            item_ids = {_uint(value) for value in entry.get("item_ids", ())}
            purchased = {_uint(value) for value in entry.get("purchased", ())}
            if item_id not in item_ids or item_id in purchased:
                return self._error(
                    CS_REFRESH_TOWER_SHOP_ITEM_LIST,
                    ERROR_CLIENT_REQUEST_PARAM,
                    "shop item is unavailable or already purchased",
                    dungeon_id=dungeon_id,
                )
            item = self.catalog.rogue_items[item_id]
            price = _uint(item.get("price"))
            opening = copy.deepcopy(dict(_mapping(state.get("opening"))))
            gold = _uint(opening.get("residue_gold"), OPENING_GOLD)
            if price > gold:
                return self._error(
                    CS_REFRESH_TOWER_SHOP_ITEM_LIST,
                    ERROR_CLIENT_REQUEST_PARAM,
                    "not enough Holy Tower gold",
                    dungeon_id=dungeon_id,
                )
            opening["residue_gold"] = gold - price
            state["opening"] = opening
            purchased.add(item_id)
            entry["purchased"] = sorted(purchased)
            runtime = copy.deepcopy(dict(_mapping(state.get("shop_runtime"))))
            runtime[str(dungeon_id)] = copy.deepcopy(entry)
            state["shop_runtime"] = runtime
            tower_items = copy.deepcopy(dict(_mapping(state.get("tower_items"))))
            key = str(item_id)
            tower_items[key] = _uint(tower_items.get(key)) + max(
                1, _uint(item.get("count"), 1)
            )
            state["tower_items"] = tower_items
        else:
            return self._error(
                CS_REFRESH_TOWER_SHOP_ITEM_LIST,
                ERROR_CLIENT_REQUEST_PARAM,
                "unknown Holy Tower shop operation",
                dungeon_id=dungeon_id,
            )
        return self._result(
            CS_REFRESH_TOWER_SHOP_ITEM_LIST,
            {
                "ErrorCode": ERROR_SUCCESS,
                "ItemList": self._shop_item_values(entry),
                "RefreshTimes": _uint(entry.get("refresh_times")),
                "ResidueGold": _uint(
                    _mapping(state.get("opening")).get("residue_gold"), OPENING_GOLD
                ),
                "Typ": typ,
            },
            changes=self._state_changes(state),
        )

    def _available_gospel_rewards(self, profile: Mapping[str, Any]) -> Result:
        state, _changed = self._ensure_state(profile)
        pending = [
            gospel_id
            for gospel_id in (
                _uint(value) for value in state.get("pending_gospel_rewards", ())
            )
            if gospel_id in self.catalog.gospel_pool
        ]
        if not pending:
            owned = set(self._tower_gospel_ids(state))
            candidates = [
                gospel_id
                for gospel_id in self.catalog.gospel_ids_for_class()
                if gospel_id not in owned
            ]
            if not candidates:
                candidates = list(self.catalog.gospel_ids_for_class())
            seed = _uint(_mapping(state.get("progress")).get("dungeon_id"))
            if candidates:
                start = seed % len(candidates)
                candidates = candidates[start:] + candidates[:start]
            pending = candidates[:3]
            state["pending_gospel_rewards"] = pending
        return self._result(
            CS_REQUEST_AVAILABLE_TOWER_GOSPELS,
            {
                "Errorcode": ERROR_SUCCESS,
                "GospelList": [
                    self._simple_gospel_values(gospel_id) for gospel_id in pending
                ],
            },
            changes=self._state_changes(state),
        )

    def _choose_gospel_reward(
        self, profile: Mapping[str, Any], request: Mapping[str, Any]
    ) -> Result:
        state, _changed = self._ensure_state(profile)
        gospel_id = _uint(request.get("GospelId"))
        pending = {
            _uint(value) for value in state.get("pending_gospel_rewards", ())
        }
        if gospel_id not in pending:
            return self._error(
                CS_CHOOSE_TOWER_GOSPEL_REWARD,
                ERROR_CLIENT_REQUEST_PARAM,
                "Holy Tower gospel reward is not one of the offered candidates",
            )
        owned = list(self._tower_gospel_ids(state))
        if gospel_id not in owned:
            owned.append(gospel_id)
        state["tower_gospels"] = owned
        state["pending_gospel_rewards"] = []
        return self._result(
            CS_CHOOSE_TOWER_GOSPEL_REWARD,
            {"Errorcode": ERROR_SUCCESS, "GospelId": gospel_id},
            changes=self._state_changes(state),
        )

    def _settlement_gospel_candidates(
        self, state: Dict[str, Any], *, refresh: bool = False
    ) -> Tuple[Dict[str, Any], List[int]]:
        runtime = copy.deepcopy(dict(_mapping(state.get("settlement_runtime"))))
        refresh_times = _uint(runtime.get("gospel_refresh_times"))
        if refresh:
            refresh_times += 1
        candidates = [
            _uint(value) for value in runtime.get("gospel_candidates", ())
        ]
        if refresh or not candidates:
            seed = _uint(runtime.get("battle_id")) + refresh_times * 10_009
            candidates = list(
                self.catalog.settlement_gospels(
                    runtime.get("gospelpool_rare"),
                    seed,
                    self._tower_gospel_ids(state),
                )
            )
        runtime["gospel_candidates"] = candidates
        runtime["gospel_refresh_times"] = refresh_times
        state["settlement_runtime"] = runtime
        return runtime, candidates

    def _settlement_gospel_values(
        self, state: Mapping[str, Any], candidates: Sequence[int]
    ) -> List[Dict[str, Any]]:
        purchased = {
            _uint(value)
            for value in _mapping(state.get("settlement_runtime")).get(
                "purchased_gospels", ()
            )
        }
        values = []
        for gospel_id in candidates:
            if gospel_id not in self.catalog.gospel_pool:
                continue
            row = self._opening_gospel_values(gospel_id)
            row["Purchased"] = gospel_id in purchased
            values.append(row)
        return values

    def _get_settlement_gospels(
        self, profile: Mapping[str, Any], request: Mapping[str, Any]
    ) -> Result:
        state, _changed = self._ensure_state(profile)
        if (
            _uint(request.get("Step")) != STEP_BUY_GOSPEL
            or _uint(state.get("step")) != STEP_BUY_GOSPEL
            or not _mapping(state.get("settlement_runtime"))
        ):
            return self._error(
                CS_GET_LEVEL_SETTLEMENT_GOSPEL_LIST,
                ERROR_CLIENT_REQUEST_PARAM,
                "Holy Tower is not in the gospel settlement step",
            )
        runtime, candidates = self._settlement_gospel_candidates(state)
        return self._result(
            CS_GET_LEVEL_SETTLEMENT_GOSPEL_LIST,
            {
                "Errorcode": ERROR_SUCCESS,
                "GospelList": self._settlement_gospel_values(state, candidates),
                "RefreshTimes": _uint(runtime.get("gospel_refresh_times")),
                "ResidueGold": _uint(
                    _mapping(state.get("opening")).get("residue_gold"), OPENING_GOLD
                ),
            },
            changes=self._state_changes(state),
        )

    def _buy_or_refresh_settlement_gospels(
        self, profile: Mapping[str, Any], request: Mapping[str, Any]
    ) -> Result:
        state, _changed = self._ensure_state(profile)
        if (
            _uint(request.get("NowStep")) != STEP_BUY_GOSPEL
            or _uint(state.get("step")) != STEP_BUY_GOSPEL
            or not _mapping(state.get("settlement_runtime"))
        ):
            return self._error(
                CS_LEVEL_SETTLEMENT_BUY_GOSPEL,
                ERROR_CLIENT_REQUEST_PARAM,
                "Holy Tower is not in the gospel settlement step",
            )
        typ = _uint(request.get("Typ"))
        runtime, candidates = self._settlement_gospel_candidates(state)
        opening = copy.deepcopy(dict(_mapping(state.get("opening"))))
        gold = _uint(opening.get("residue_gold"), OPENING_GOLD)
        next_step = STEP_BUY_GOSPEL
        if typ == 1:
            requested = list(
                dict.fromkeys(_uint(value) for value in request.get("BuyList", ()))
            )
            purchased = {
                _uint(value) for value in runtime.get("purchased_gospels", ())
            }
            selected = [
                gospel_id
                for gospel_id in requested
                if gospel_id in candidates and gospel_id not in purchased
            ]
            if not selected or len(selected) != len(requested):
                return self._error(
                    CS_LEVEL_SETTLEMENT_BUY_GOSPEL,
                    ERROR_CLIENT_REQUEST_PARAM,
                    "requested settlement gospel is unavailable",
                )
            price = sum(
                _uint(self.catalog.gospel_pool[gospel_id].get("sold"))
                for gospel_id in selected
            )
            if price > gold:
                return self._error(
                    CS_LEVEL_SETTLEMENT_BUY_GOSPEL,
                    ERROR_CLIENT_REQUEST_PARAM,
                    "not enough Holy Tower gold to buy settlement gospels",
                )
            gold -= price
            owned = list(self._tower_gospel_ids(state))
            for gospel_id in selected:
                purchased.add(gospel_id)
                if gospel_id not in owned:
                    owned.append(gospel_id)
            runtime["purchased_gospels"] = sorted(purchased)
            state["tower_gospels"] = owned
        elif typ == 2:
            refresh_times = _uint(runtime.get("gospel_refresh_times"))
            costs = self.catalog.gospel_refresh_costs
            price = costs[min(refresh_times, len(costs) - 1)] if costs else 0
            if price > gold:
                return self._error(
                    CS_LEVEL_SETTLEMENT_BUY_GOSPEL,
                    ERROR_CLIENT_REQUEST_PARAM,
                    "not enough Holy Tower gold to refresh settlement gospels",
                )
            gold -= price
            opening["residue_gold"] = gold
            state["opening"] = opening
            runtime, candidates = self._settlement_gospel_candidates(
                state, refresh=True
            )
        elif typ == 3:
            next_step = STEP_CHOOSE_ITEM
            state["step"] = next_step
        else:
            return self._error(
                CS_LEVEL_SETTLEMENT_BUY_GOSPEL,
                ERROR_CLIENT_REQUEST_PARAM,
                "unknown gospel settlement operation",
            )
        opening["residue_gold"] = gold
        state["opening"] = opening
        state["settlement_runtime"] = runtime
        return self._result(
            CS_LEVEL_SETTLEMENT_BUY_GOSPEL,
            {
                "Errorcode": ERROR_SUCCESS,
                "GospelList": self._settlement_gospel_values(state, candidates),
                "RefreshTimes": _uint(runtime.get("gospel_refresh_times")),
                "NextStep": next_step,
                "ResidueGold": gold,
                "Typ": typ,
            },
            changes=self._state_changes(state),
        )

    def _settlement_item_candidates(
        self, state: Dict[str, Any], *, refresh: bool = False
    ) -> Tuple[Dict[str, Any], List[int]]:
        runtime = copy.deepcopy(dict(_mapping(state.get("settlement_runtime"))))
        refresh_times = _uint(runtime.get("item_refresh_times"))
        if refresh:
            refresh_times += 1
        candidates = [_uint(value) for value in runtime.get("item_candidates", ())]
        if refresh or len(candidates) != 3:
            seed = _uint(runtime.get("battle_id")) + refresh_times * 10_037
            candidates = list(
                self.catalog.settlement_items(
                    runtime.get("item_groups", ()),
                    runtime.get("itemgroup_rare"),
                    _uint(state.get("difficulty"), DEFAULT_DIFFICULTY),
                    seed,
                    3,
                )
            )
        runtime["item_candidates"] = candidates
        runtime["item_refresh_times"] = refresh_times
        state["settlement_runtime"] = runtime
        return runtime, candidates

    @staticmethod
    def _settlement_item_values(candidates: Sequence[int]) -> List[Dict[str, int]]:
        return [
            _reward_values(Reward(29, _uint(item_id), 1, 0, 0, 0, 0))
            for item_id in candidates
            if _uint(item_id)
        ]

    def _get_settlement_items(
        self, profile: Mapping[str, Any], request: Mapping[str, Any]
    ) -> Result:
        state, _changed = self._ensure_state(profile)
        if (
            _uint(request.get("NowStep")) != STEP_CHOOSE_ITEM
            or _uint(state.get("step")) != STEP_CHOOSE_ITEM
            or not _mapping(state.get("settlement_runtime"))
        ):
            return self._error(
                CS_GET_LEVEL_SETTLEMENT_ITEM_LIST,
                ERROR_CLIENT_REQUEST_PARAM,
                "Holy Tower is not in the item settlement step",
            )
        runtime, candidates = self._settlement_item_candidates(state)
        if len(candidates) != 3:
            return self._error(
                CS_GET_LEVEL_SETTLEMENT_ITEM_LIST,
                ERROR_CONFIG,
                "Holy Tower settlement item pool did not produce three choices",
            )
        return self._result(
            CS_GET_LEVEL_SETTLEMENT_ITEM_LIST,
            {
                "Errorcode": ERROR_SUCCESS,
                "RewardList": self._settlement_item_values(candidates),
                "RefreshTimes": _uint(runtime.get("item_refresh_times")),
                "ResidueGold": _uint(
                    _mapping(state.get("opening")).get("residue_gold"), OPENING_GOLD
                ),
            },
            changes=self._state_changes(state),
        )

    def _choose_or_refresh_settlement_item(
        self, profile: Mapping[str, Any], request: Mapping[str, Any]
    ) -> Result:
        state, _changed = self._ensure_state(profile)
        if (
            _uint(request.get("NowStep")) != STEP_CHOOSE_ITEM
            or _uint(state.get("step")) != STEP_CHOOSE_ITEM
            or not _mapping(state.get("settlement_runtime"))
        ):
            return self._error(
                CS_CHOOSE_LEVEL_SETTLEMENT_ITEM,
                ERROR_CLIENT_REQUEST_PARAM,
                "Holy Tower is not in the item settlement step",
            )
        typ = _uint(request.get("Typ"))
        runtime, candidates = self._settlement_item_candidates(state)
        opening = copy.deepcopy(dict(_mapping(state.get("opening"))))
        gold = _uint(opening.get("residue_gold"), OPENING_GOLD)
        next_step = STEP_CHOOSE_ITEM
        if typ == 1:
            item_id = _uint(request.get("ItemId"))
            if item_id not in candidates:
                return self._error(
                    CS_CHOOSE_LEVEL_SETTLEMENT_ITEM,
                    ERROR_CLIENT_REQUEST_PARAM,
                    "selected Holy Tower settlement item is unavailable",
                )
            tower_items = copy.deepcopy(dict(_mapping(state.get("tower_items"))))
            key = str(item_id)
            tower_items[key] = _uint(tower_items.get(key)) + 1
            state["tower_items"] = tower_items
            runtime["chosen_item"] = item_id
            next_step = STEP_BATTLE_SETTLED
            state["step"] = next_step
        elif typ == 2:
            refresh_times = _uint(runtime.get("item_refresh_times"))
            costs = self.catalog.item_refresh_costs
            price = costs[min(refresh_times, len(costs) - 1)] if costs else 0
            if price > gold:
                return self._error(
                    CS_CHOOSE_LEVEL_SETTLEMENT_ITEM,
                    ERROR_CLIENT_REQUEST_PARAM,
                    "not enough Holy Tower gold to refresh settlement items",
                )
            gold -= price
            opening["residue_gold"] = gold
            state["opening"] = opening
            runtime, candidates = self._settlement_item_candidates(
                state, refresh=True
            )
        else:
            return self._error(
                CS_CHOOSE_LEVEL_SETTLEMENT_ITEM,
                ERROR_CLIENT_REQUEST_PARAM,
                "unknown item settlement operation",
            )
        opening["residue_gold"] = gold
        state["opening"] = opening
        state["settlement_runtime"] = runtime
        return self._result(
            CS_CHOOSE_LEVEL_SETTLEMENT_ITEM,
            {
                "Errorcode": ERROR_SUCCESS,
                "RewardList": self._settlement_item_values(candidates),
                "RefreshTimes": _uint(runtime.get("item_refresh_times")),
                "NextStep": next_step,
                "ResidueGold": gold,
                "Typ": typ,
            },
            changes=self._state_changes(state),
        )

    def _enter_dungeon(
        self, profile: Mapping[str, Any], request: Mapping[str, Any]
    ) -> Result:
        state, _changed = self._ensure_state(profile)
        tower_id = _uint(request.get("TowerId"))
        dungeon_id = _uint(request.get("DungeonId"))
        if _uint(state.get("status")) != CRAWL_TOWER_RUNNING:
            return self._error(
                CS_CRAWL_TOWER_ENTER_DUNGEON,
                ERROR_CLIENT_REQUEST_PARAM,
                "Tower survey has not been initialized",
                tower_id=tower_id,
                dungeon_id=dungeon_id,
            )
        if tower_id != _uint(state.get("tower_id")):
            return self._error(
                CS_CRAWL_TOWER_ENTER_DUNGEON,
                ERROR_CLIENT_REQUEST_PARAM,
                "tower id does not match the running tower",
                tower_id=tower_id,
                dungeon_id=dungeon_id,
            )
        if dungeon_id not in self.catalog.nodes_by_map.get(_uint(state.get("map_id")), ()):
            return self._error(
                CS_CRAWL_TOWER_ENTER_DUNGEON,
                ERROR_CLIENT_REQUEST_PARAM,
                "dungeon node is not on the running map",
                tower_id=tower_id,
                dungeon_id=dungeon_id,
            )
        active_nodes = {
            _uint(value) for value in state.get("active_nodes", ()) if _uint(value)
        }
        current_dungeon = _uint(_mapping(state.get("progress")).get("dungeon_id"))
        if dungeon_id not in active_nodes and dungeon_id != current_dungeon:
            return self._error(
                CS_CRAWL_TOWER_ENTER_DUNGEON,
                ERROR_CLIENT_REQUEST_PARAM,
                "dungeon node is not active on the current route",
                tower_id=tower_id,
                dungeon_id=dungeon_id,
            )
        resolved_nodes = copy.deepcopy(dict(_mapping(state.get("resolved_nodes"))))
        key = str(dungeon_id)
        replayed = key in resolved_nodes or dungeon_id in resolved_nodes
        resolved = copy.deepcopy(
            dict(_mapping(resolved_nodes.get(key, resolved_nodes.get(dungeon_id))))
        )
        if not resolved:
            try:
                resolved = self.catalog.resolve_node(
                    _uint(state.get("difficulty")),
                    _uint(state.get("layer")),
                    _uint(state.get("map_id")),
                    dungeon_id,
                )
            except (KeyError, TowerBattleConfigError) as exc:
                return self._error(
                    CS_CRAWL_TOWER_ENTER_DUNGEON,
                    ERROR_CONFIG,
                    str(exc),
                    tower_id=tower_id,
                    dungeon_id=dungeon_id,
                )
            resolved_nodes[key] = copy.deepcopy(resolved)
            state["resolved_nodes"] = resolved_nodes
        progress = copy.deepcopy(dict(_mapping(state.get("progress"))))
        progress.update(
            dungeon_id=dungeon_id,
            sub_id=_uint(resolved.get("sub_id")),
            progress_dungeon_id=dungeon_id,
            top_dungeon_id=0,
            battle_id=0,
        )
        state["progress"] = progress
        state["process_dungeon_id"] = dungeon_id
        state["step"] = STEP_RUNNING
        node_category = self.catalog.tower_event_type(
            _uint(resolved.get("node_type"))
        )
        if node_category == 2:
            battle_id = max(1, _uint(state.get("next_battle_id"), 1))
            state["next_battle_id"] = battle_id + 1
            state["active_battle"] = {
                "kind": "shop",
                "battle_id": battle_id,
                "tower_id": tower_id,
                "dungeon_id": dungeon_id,
                "sub_id": _uint(resolved.get("sub_id")),
                "node_type": _uint(resolved.get("node_type")),
                "reward_exp": 0,
                "rewards": [],
                "started_at": _uint(self.now()),
            }
        return self._result(
            CS_CRAWL_TOWER_ENTER_DUNGEON,
            {
                "Errorcode": ERROR_SUCCESS,
                "TowerId": tower_id,
                "DungeonId": dungeon_id,
                "SubId": _uint(resolved.get("sub_id")),
                "GoodsList": [],
                "ShopRefreshNum": _uint(
                    _mapping(
                        _mapping(state.get("shop_runtime")).get(str(dungeon_id))
                    ).get("refresh_times")
                ),
                "CrawlTowerSubMonsterData": {},
            },
            changes=self._state_changes(state),
            replayed=replayed,
            metadata={"node_type": _uint(resolved.get("node_type"))},
        )

    def _set_process(
        self, profile: Mapping[str, Any], request: Mapping[str, Any]
    ) -> Result:
        state, _changed = self._ensure_state(profile)
        dungeon_id = _uint(request.get("DungeonId"))
        if _uint(state.get("status")) != CRAWL_TOWER_RUNNING:
            return self._result(
                CS_SET_PROCESS_DUNGEON,
                {
                    "ErrorCode": ERROR_CLIENT_REQUEST_PARAM,
                    "DungeonId": dungeon_id,
                },
                error_code=ERROR_CLIENT_REQUEST_PARAM,
                reason="Tower survey has not been initialized",
            )
        if dungeon_id not in self.catalog.nodes_by_map.get(_uint(state.get("map_id")), ()):
            return self._result(
                CS_SET_PROCESS_DUNGEON,
                {
                    "ErrorCode": ERROR_CLIENT_REQUEST_PARAM,
                    "DungeonId": dungeon_id,
                },
                error_code=ERROR_CLIENT_REQUEST_PARAM,
                reason="process dungeon is not on the running map",
            )
        active_nodes = {
            _uint(value) for value in state.get("active_nodes", ()) if _uint(value)
        }
        current_dungeon = _uint(_mapping(state.get("progress")).get("dungeon_id"))
        if dungeon_id not in active_nodes and dungeon_id != current_dungeon:
            return self._result(
                CS_SET_PROCESS_DUNGEON,
                {
                    "ErrorCode": ERROR_CLIENT_REQUEST_PARAM,
                    "DungeonId": dungeon_id,
                },
                error_code=ERROR_CLIENT_REQUEST_PARAM,
                reason="process dungeon is not active on the current route",
            )
        state["process_dungeon_id"] = dungeon_id
        progress = copy.deepcopy(dict(_mapping(state.get("progress"))))
        progress["progress_dungeon_id"] = dungeon_id
        state["progress"] = progress
        return self._result(
            CS_SET_PROCESS_DUNGEON,
            {"ErrorCode": ERROR_SUCCESS, "DungeonId": dungeon_id},
            changes=self._state_changes(state),
        )

    def _start_battle(
        self, profile: Mapping[str, Any], request: Mapping[str, Any]
    ) -> Result:
        state, _changed = self._ensure_state(profile)
        tower_id = _uint(request.get("TowerId"))
        dungeon_id = _uint(request.get("DungeonId"))
        if _uint(state.get("status")) != CRAWL_TOWER_RUNNING:
            return self._error(
                CS_TOWER_START_BATTLE,
                ERROR_CLIENT_REQUEST_PARAM,
                "Tower survey has not been initialized",
                tower_id=tower_id,
                dungeon_id=dungeon_id,
            )
        active = _mapping(state.get("active_battle"))
        if active:
            if (
                _uint(active.get("tower_id")) == tower_id
                and _uint(active.get("dungeon_id")) == dungeon_id
            ):
                state["step"] = STEP_RUNNING
                return self._result(
                    CS_TOWER_START_BATTLE,
                    {
                        "Errorcode": ERROR_SUCCESS,
                        "DungeonId": dungeon_id,
                        "TowerId": tower_id,
                        "IsRecoverBattle": True,
                    },
                    changes=self._state_changes(state),
                    replayed=True,
                    metadata={"battle_id": _uint(active.get("battle_id"))},
                )
            return self._error(
                CS_TOWER_START_BATTLE,
                ERROR_CLIENT_REQUEST_PARAM,
                "another Tower battle is active",
                tower_id=tower_id,
                dungeon_id=dungeon_id,
            )
        resolved = _mapping(_mapping(state.get("resolved_nodes")).get(str(dungeon_id)))
        if (
            tower_id != _uint(state.get("tower_id"))
            or not resolved
            or _uint(resolved.get("sub_id")) == 0
        ):
            return self._error(
                CS_TOWER_START_BATTLE,
                ERROR_CLIENT_REQUEST_PARAM,
                "Tower battle node has not been resolved",
                tower_id=tower_id,
                dungeon_id=dungeon_id,
            )
        event_runtime = copy.deepcopy(dict(_mapping(state.get("event_runtime"))))
        pending_event = _mapping(event_runtime.get("pending_battle"))
        is_event_battle = _uint(pending_event.get("dungeon_id")) == dungeon_id
        if (
            self.catalog.tower_event_type(_uint(resolved.get("node_type"))) != 0
            and not is_event_battle
        ):
            return self._error(
                CS_TOWER_START_BATTLE,
                ERROR_CLIENT_REQUEST_PARAM,
                "non-battle Tower node has no pending special battle",
                tower_id=tower_id,
                dungeon_id=dungeon_id,
            )
        battle_id = max(1, _uint(state.get("next_battle_id"), 1))
        state["next_battle_id"] = battle_id + 1
        battle_source = pending_event if is_event_battle else resolved
        state["active_battle"] = {
            "kind": "event_battle" if is_event_battle else "battle",
            "battle_id": battle_id,
            "tower_id": tower_id,
            "dungeon_id": dungeon_id,
            "sub_id": _uint(battle_source.get("sub_id")),
            "node_type": _uint(
                battle_source.get("battle_type"), _uint(resolved.get("node_type"))
            ),
            "battle_config_id": _uint(battle_source.get("battle_config_id")),
            "reward_exp": _uint(battle_source.get("reward_exp")),
            "rewards": copy.deepcopy(list(battle_source.get("rewards", []))),
            "gospelpool_rare": str(battle_source.get("gospelpool_rare", "")),
            "item_groups": copy.deepcopy(list(battle_source.get("item_groups", []))),
            "itemgroup_rare": str(battle_source.get("itemgroup_rare", "")),
            "event_id": _uint(pending_event.get("event_id")),
            "started_at": _uint(self.now()),
        }
        progress = copy.deepcopy(dict(_mapping(state.get("progress"))))
        progress.update(
            dungeon_id=dungeon_id,
            sub_id=_uint(battle_source.get("sub_id")),
            top_dungeon_id=dungeon_id if is_event_battle else 0,
            battle_id=battle_id,
        )
        state["progress"] = progress
        state["step"] = STEP_RUNNING
        return self._result(
            CS_TOWER_START_BATTLE,
            {
                "Errorcode": ERROR_SUCCESS,
                "DungeonId": dungeon_id,
                "TowerId": tower_id,
                "IsRecoverBattle": False,
            },
            changes=self._state_changes(state),
            metadata={
                "battle_id": battle_id,
                "sub_id": _uint(battle_source.get("sub_id")),
                "event_battle": is_event_battle,
            },
        )

    def _advance_after_win(self, state: Dict[str, Any], active: Mapping[str, Any]) -> None:
        dungeon_id = _uint(active.get("dungeon_id"))
        next_nodes = self.catalog.next_nodes(dungeon_id)
        progress = copy.deepcopy(dict(_mapping(state.get("progress"))))
        completed = list(state.get("completed_nodes", ()))
        if dungeon_id not in completed:
            completed.append(dungeon_id)
        state["completed_nodes"] = completed
        state["active_nodes"] = list(next_nodes)
        if next_nodes:
            progress["progress_dungeon_id"] = next_nodes[0]
        elif _uint(active.get("node_type")) == 3:
            layer, map_id = self.catalog.next_floor_map(
                _uint(state.get("difficulty")), _uint(state.get("layer"))
            )
            if map_id:
                state["layer"] = layer
                state["map_id"] = map_id
                state["resolved_nodes"] = {}
                state["completed_nodes"] = []
                roots = self.catalog.map_roots(map_id)
                if roots:
                    state["active_nodes"] = list(roots)
                    progress.update(
                        dungeon_id=0,
                        sub_id=0,
                        progress_dungeon_id=0,
                        top_dungeon_id=0,
                    )
        else:
            progress["progress_dungeon_id"] = 0
            state["active_nodes"] = []
        progress["battle_id"] = 0
        progress["top_dungeon_id"] = 0
        state["progress"] = progress
        state["process_dungeon_id"] = 0

    def _battle_end(
        self, profile: Mapping[str, Any], request: Mapping[str, Any]
    ) -> Result:
        state, _changed = self._ensure_state(profile)
        fingerprint = self._fingerprint(request)
        active = _mapping(state.get("active_battle"))
        if not active:
            cached = _mapping(state.get("last_settlement"))
            if cached.get("fingerprint") == fingerprint:
                values = copy.deepcopy(dict(_mapping(cached.get("response"))))
                return self._result(
                    CS_CRAWL_TOWER_BATTLE_END,
                    values,
                    rewards=_stored_rewards(cached.get("rewards")),
                    replayed=True,
                    reason="duplicate Tower settlement replayed without another grant",
                )
            return self._error(
                CS_CRAWL_TOWER_BATTLE_END,
                ERROR_CLIENT_REQUEST_PARAM,
                "no matching active Tower battle",
                tower_id=_uint(request.get("TowerId")),
                dungeon_id=_uint(request.get("DungeonId")),
                sub_id=_uint(request.get("SubId")),
            )
        if (
            _uint(active.get("tower_id")) != _uint(request.get("TowerId"))
            or _uint(active.get("dungeon_id")) != _uint(request.get("DungeonId"))
            or _uint(active.get("sub_id")) != _uint(request.get("SubId"))
        ):
            return self._error(
                CS_CRAWL_TOWER_BATTLE_END,
                ERROR_CLIENT_REQUEST_PARAM,
                "settlement identifiers do not match the active Tower battle",
                tower_id=_uint(request.get("TowerId")),
                dungeon_id=_uint(request.get("DungeonId")),
                sub_id=_uint(request.get("SubId")),
            )

        self._apply_request_context(state, request)
        kind = str(active.get("kind") or "battle")
        is_non_battle = kind in {"event", "shop"}
        result = BATTLE_WIN if is_non_battle else _uint(request.get("Result"))
        won = (is_non_battle or result == BATTLE_WIN) and not bool(
            request.get("IsGiveUp")
        )
        rewards = _stored_rewards(active.get("rewards")) if won else ()
        reward_exp = _uint(active.get("reward_exp")) if won else 0
        grant_changes = self._grant_changes(profile, rewards)

        if won:
            state["exp"] = _uint(state.get("exp")) + reward_exp
            state["team_level"] = self.catalog.team_level_for_exp(_uint(state["exp"]))
            self._advance_after_win(state, active)
        else:
            progress = copy.deepcopy(dict(_mapping(state.get("progress"))))
            progress["battle_id"] = 0
            state["progress"] = progress
        state["active_battle"] = None
        if kind == "event_battle":
            runtime = copy.deepcopy(dict(_mapping(state.get("event_runtime"))))
            completed_events = list(runtime.get("completed_event_ids", ()))
            event_id = _uint(active.get("event_id"))
            if won and event_id and event_id not in completed_events:
                completed_events.append(event_id)
            runtime["completed_event_ids"] = completed_events
            runtime["pending_battle"] = None
            state["event_runtime"] = runtime
        if won and kind in {"battle", "event_battle"}:
            state["settlement_runtime"] = {
                "battle_id": _uint(active.get("battle_id")),
                "battle_config_id": _uint(active.get("battle_config_id")),
                "gospelpool_rare": str(active.get("gospelpool_rare", "")),
                "item_groups": copy.deepcopy(list(active.get("item_groups", ()))),
                "itemgroup_rare": str(active.get("itemgroup_rare", "")),
                "gospel_candidates": [],
                "gospel_refresh_times": 0,
                "purchased_gospels": [],
                "item_candidates": [],
                "item_refresh_times": 0,
                "chosen_item": 0,
            }
            state["step"] = STEP_BUY_GOSPEL
        else:
            state["step"] = STEP_RUNNING

        values = {
            "Errorcode": ERROR_SUCCESS,
            "Result": result,
            "TowerId": _uint(active.get("tower_id")),
            "DungeonId": _uint(active.get("dungeon_id")),
            "SubId": _uint(active.get("sub_id")),
            "Progress": self._progress_values(state),
            "Context": self._context_values(profile, state),
            "RewardList": [_reward_values(reward) for reward in rewards],
            "RogueAwardList": [],
            "FinishedConditionList": [],
            "IndependentTree": self._independent_tree(),
            "RewardExp": reward_exp,
            "BattleVerifyResult": True,
        }
        state["last_settlement"] = {
            "fingerprint": fingerprint,
            "battle_id": _uint(active.get("battle_id")),
            "response": copy.deepcopy(values),
            "rewards": _reward_rows(rewards),
        }
        changes = dict(grant_changes)
        changes[STATE_KEY] = copy.deepcopy(state)
        return self._result(
            CS_CRAWL_TOWER_BATTLE_END,
            values,
            changes=changes,
            rewards=rewards,
            metadata={
                "battle_id": _uint(active.get("battle_id")),
                "reward_exp": reward_exp,
                "team_level": _uint(state.get("team_level")),
            },
        )

    def _cancel(
        self, profile: Mapping[str, Any], request: Mapping[str, Any]
    ) -> Result:
        state, _changed = self._ensure_state(profile)
        tower_id = _uint(request.get("TowerId"))
        dungeon_id = _uint(request.get("DungeonId"))
        active = _mapping(state.get("active_battle"))
        if active and (
            _uint(active.get("tower_id")) != tower_id
            or _uint(active.get("dungeon_id")) != dungeon_id
        ):
            return self._error(
                CS_CRAWL_TOWER_CANCEL_DUNGEON,
                ERROR_CLIENT_REQUEST_PARAM,
                "cancel identifiers do not match the active battle",
                tower_id=tower_id,
                dungeon_id=dungeon_id,
            )
        state["active_battle"] = None
        state["step"] = STEP_RUNNING
        progress = copy.deepcopy(dict(_mapping(state.get("progress"))))
        progress["battle_id"] = 0
        state["progress"] = progress
        return self._result(
            CS_CRAWL_TOWER_CANCEL_DUNGEON,
            {"Errorcode": ERROR_SUCCESS, "TowerId": tower_id, "DungeonId": dungeon_id},
            changes=self._state_changes(state),
        )

    def _abandon(
        self, profile: Mapping[str, Any], request: Mapping[str, Any]
    ) -> Result:
        state, _changed = self._ensure_state(profile)
        tower_id = _uint(request.get("TowerId"), _uint(state.get("tower_id")))
        if tower_id != _uint(state.get("tower_id")):
            return self._error(
                CS_ABANDON_CRAWL_TOWER,
                ERROR_CLIENT_REQUEST_PARAM,
                "unknown tower",
                tower_id=tower_id,
            )
        # Abandon is a full run reset.  Keeping resolved nodes, HP, EXP or a
        # cached settlement makes the next init look like a partially resumed
        # survey and was the source of the empty "honour return" loop.
        state = self._default_state(profile)
        return self._result(
            CS_ABANDON_CRAWL_TOWER,
            {"Errorcode": ERROR_SUCCESS, "Status": CRAWL_TOWER_IDLE},
            changes=self._state_changes(state),
        )

    def _error(
        self,
        command_id: int,
        code: int,
        reason: str,
        *,
        tower_id: int = DEFAULT_TOWER_ID,
        dungeon_id: int = 0,
        sub_id: int = 0,
    ) -> Result:
        values: Dict[str, Any]
        if command_id == CS_CRAWL_TOWER_INIT:
            values = {"Errorcode": code, "Data": {}, "IndependentTree": {}}
        elif command_id == CS_SET_CRAWL_TOWER_PHALANX:
            values = {"Errorcode": code, "PhaData": {}, "Context": {}}
        elif command_id == CS_GET_CRAWL_TOWER_PHALANX:
            values = {"Errorcode": code, "PhaData": {}}
        elif command_id == CS_CLICK_TOWER_EVENT_OPTION:
            values = {
                "Errorcode": code,
                "DungeonId": dungeon_id,
                "TowerEventId": sub_id,
                "OptionId": 0,
                "ReChooseOptionId": 0,
                "SubOptionList": [],
                "RandValue": 0,
                "SpecialDungeonId": 0,
                "EffectValueMap": {},
            }
        elif command_id == CS_CRAWL_TOWER_ENTER_DUNGEON:
            values = {
                "Errorcode": code,
                "TowerId": tower_id,
                "DungeonId": dungeon_id,
                "SubId": sub_id,
            }
        elif command_id == CS_TOWER_START_BATTLE:
            values = {
                "Errorcode": code,
                "TowerId": tower_id,
                "DungeonId": dungeon_id,
                "IsRecoverBattle": False,
            }
        elif command_id == CS_CRAWL_TOWER_BATTLE_END:
            values = {
                "Errorcode": code,
                "TowerId": tower_id,
                "DungeonId": dungeon_id,
                "SubId": sub_id,
                "Progress": {},
                "Context": {},
                "IndependentTree": {},
                "BattleVerifyResult": False,
            }
        elif command_id == CS_CRAWL_TOWER_CANCEL_DUNGEON:
            values = {"Errorcode": code, "TowerId": tower_id, "DungeonId": dungeon_id}
        elif command_id == CS_ABANDON_CRAWL_TOWER:
            values = {"Errorcode": code, "Status": CRAWL_TOWER_IDLE}
        elif command_id == CS_REQUEST_CRAWL_TOWER_REMAIN_TIME:
            values = {"Errorcode": code}
        elif command_id == CS_CRAWL_TOWER_SET_DIFFICULTY:
            values = {"Errorcode": code, "DifficultyType": 0}
        elif command_id == CS_REQUEST_TOWER_SHOW_HERO_DATA:
            values = {
                "Errorcode": code,
                "TowerId": tower_id,
                "HeroDataList": [],
                "HeroGospelClassMap": {},
            }
        elif command_id == CS_TOWER_REFRESH_GOSPEL_CLASS:
            values = {
                "Errorcode": code,
                "HeroGospelClassMap": {},
                "RemainRefreshGospelClassNum": 0,
            }
        elif command_id == CS_REQUEST_TOWER_CANDIDATE_GOSPEL_LIST:
            values = {
                "Errorcode": code,
                "TowerId": tower_id,
                "GospelClass": 0,
                "GospelList": [],
            }
        elif command_id == CS_REQUEST_AVAILABLE_TOWER_GOSPELS:
            values = {"Errorcode": code, "GospelList": []}
        elif command_id == CS_CHOOSE_TOWER_GOSPEL_REWARD:
            values = {"Errorcode": code, "GospelId": 0}
        elif command_id == CS_CRAWL_TOWER_PREPARE:
            values = {"Errorcode": code, "Preparation": {}}
        elif command_id == CS_TOWER_GENERATE_OPEN_ITEMS:
            values = {"ItemIdList": []}
        elif command_id == CS_REQUEST_AVAILABLE_TOWER_GOSPEL_CLASSES:
            values = {
                "Errorcode": code,
                "GospelClassMap": {},
                "AvailableGospelClassNum": 0,
            }
        elif command_id == CS_CHOOSE_TOWER_GOSPEL_CLASS:
            values = {
                "Errorcode": code,
                "GospelClass": 0,
                "ChooseOrGiveup": 0,
                "GospelClassMap": {},
                "AvailableGospelClassNum": 0,
            }
        elif command_id == CS_GET_OPENING_GOSPEL_LIST:
            values = {
                "Errorcode": code,
                "GospelList": [],
                "RefreshTimes": 0,
                "ResidueGold": 0,
            }
        elif command_id == CS_OPENING_CHOOSE_GOSPEL:
            values = {
                "Errorcode": code,
                "GospelList": [],
                "RefreshTimes": 0,
                "NextStep": 0,
                "ResidueGold": 0,
                "Typ": 0,
            }
        elif command_id == CS_OPENING_CHOOSE_HERO:
            values = {"Errorcode": code, "NextStep": 0}
        elif command_id == CS_GET_LEVEL_SETTLEMENT_GOSPEL_LIST:
            values = {
                "Errorcode": code,
                "GospelList": [],
                "RefreshTimes": 0,
                "ResidueGold": 0,
            }
        elif command_id == CS_LEVEL_SETTLEMENT_BUY_GOSPEL:
            values = {
                "Errorcode": code,
                "GospelList": [],
                "RefreshTimes": 0,
                "NextStep": 0,
                "ResidueGold": 0,
                "Typ": 0,
            }
        elif command_id == CS_GET_LEVEL_SETTLEMENT_ITEM_LIST:
            values = {
                "Errorcode": code,
                "RewardList": [],
                "RefreshTimes": 0,
                "ResidueGold": 0,
            }
        elif command_id == CS_CHOOSE_LEVEL_SETTLEMENT_ITEM:
            values = {
                "Errorcode": code,
                "RewardList": [],
                "RefreshTimes": 0,
                "NextStep": 0,
                "ResidueGold": 0,
                "Typ": 0,
            }
        elif command_id == CS_GET_TOWER_SHOP_ITEM_LIST:
            values = {
                "ErrorCode": code,
                "ItemList": [],
                "RefreshTimes": 0,
                "ResidueGold": 0,
            }
        elif command_id == CS_REFRESH_TOWER_SHOP_ITEM_LIST:
            values = {
                "ErrorCode": code,
                "ItemList": [],
                "RefreshTimes": 0,
                "ResidueGold": 0,
                "Typ": 0,
            }
        elif command_id == CS_GET_HOLY_TOWER_ITEM_ACTIVATE_EFFECT_LIST:
            values = {"ErrorCode": code, "EffectItemList": []}
        elif command_id == CS_ACTIVATE_HOLY_TOWER_ITEM_EFFECT:
            values = {"ErrorCode": code, "Typ": 0, "ItemUId": 0}
        else:
            values = {}
        return self._result(command_id, values, error_code=code, reason=reason)

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
        except (TypeError, ValueError, SchemaCodecError) as exc:
            return self._error(
                command_id,
                ERROR_MESSAGE_DATA_ILLEGAL,
                str(exc),
            )

        if command_id == CS_REQUEST_CRAWL_TOWER_PERIOD_DATA:
            return self._query_period(profile)
        if command_id == CS_REQUEST_CRAWL_TOWER_REMAIN_TIME:
            return self._query_remain_time()
        if command_id == CS_REQUEST_CRAWL_TOWER_STATUS:
            state, changed = self._ensure_state(profile)
            return self._result(
                command_id,
                {"Status": _uint(state.get("status"), CRAWL_TOWER_IDLE)},
                changes=self._state_changes(state, changed),
            )
        if command_id == CS_REQUEST_CRAWL_TOWER_DATA:
            return self._query_data(profile)
        if command_id == CS_REQUEST_TOWER_EFFECT_ITEMS:
            return self._request_tower_effect_items(profile)
        if command_id == CS_CRAWL_TOWER_SET_DIFFICULTY:
            return self._set_difficulty(profile, request)
        if command_id == CS_REQUEST_TOWER_SHOW_HERO_DATA:
            return self._show_hero_data(profile, request)
        if command_id == CS_TOWER_REFRESH_GOSPEL_CLASS:
            return self._refresh_gospel_classes(profile, request)
        if command_id == CS_REQUEST_TOWER_CANDIDATE_GOSPEL_LIST:
            return self._candidate_gospels(profile, request)
        if command_id == CS_TOWER_GENERATE_OPEN_ITEMS:
            return self._generate_open_items(profile)
        if command_id == CS_REQUEST_AVAILABLE_TOWER_GOSPEL_CLASSES:
            return self._available_gospel_classes(profile)
        if command_id == CS_CHOOSE_TOWER_GOSPEL_CLASS:
            return self._choose_gospel_class(profile, request)
        if command_id == CS_CRAWL_TOWER_PREPARE:
            return self._prepare(profile, request)
        if command_id == CS_OPENING_CHOOSE_HERO:
            return self._opening_choose_hero(profile, request)
        if command_id == CS_GET_OPENING_GOSPEL_LIST:
            return self._opening_candidates(profile, request)
        if command_id == CS_OPENING_CHOOSE_GOSPEL:
            return self._opening_choose_gospel(profile, request)
        if command_id == CS_CRAWL_TOWER_INIT:
            return self._init(profile, request)
        if command_id == CS_REQUEST_AVAILABLE_TOWER_GOSPELS:
            return self._available_gospel_rewards(profile)
        if command_id == CS_CHOOSE_TOWER_GOSPEL_REWARD:
            return self._choose_gospel_reward(profile, request)
        if command_id == CS_GET_LEVEL_SETTLEMENT_GOSPEL_LIST:
            return self._get_settlement_gospels(profile, request)
        if command_id == CS_LEVEL_SETTLEMENT_BUY_GOSPEL:
            return self._buy_or_refresh_settlement_gospels(profile, request)
        if command_id == CS_GET_LEVEL_SETTLEMENT_ITEM_LIST:
            return self._get_settlement_items(profile, request)
        if command_id == CS_GET_HOLY_TOWER_ITEM_ACTIVATE_EFFECT_LIST:
            return self._get_holy_tower_item_activate_effect_list(profile)
        if command_id == CS_ACTIVATE_HOLY_TOWER_ITEM_EFFECT:
            return self._activate_holy_tower_item_effect(profile, request)
        if command_id == CS_CHOOSE_LEVEL_SETTLEMENT_ITEM:
            return self._choose_or_refresh_settlement_item(profile, request)
        if command_id == CS_GET_CRAWL_TOWER_PHALANX:
            return self._get_phalanx(profile)
        if command_id == CS_SET_CRAWL_TOWER_PHALANX:
            return self._set_phalanx(profile, request)
        if command_id == CS_CRAWL_TOWER_ENTER_DUNGEON:
            return self._enter_dungeon(profile, request)
        if command_id == CS_CLICK_TOWER_EVENT_OPTION:
            return self._click_event_option(profile, request)
        if command_id == CS_GET_TOWER_SHOP_ITEM_LIST:
            return self._get_shop_items(profile, request)
        if command_id == CS_REFRESH_TOWER_SHOP_ITEM_LIST:
            return self._refresh_or_buy_shop_item(profile, request)
        if command_id == CS_SET_PROCESS_DUNGEON:
            return self._set_process(profile, request)
        if command_id == CS_TOWER_START_BATTLE:
            return self._start_battle(profile, request)
        if command_id == CS_CRAWL_TOWER_BATTLE_END:
            return self._battle_end(profile, request)
        if command_id == CS_GET_HOLY_TOWER_NOW_STEP:
            state, changed = self._ensure_state(profile)
            return self._result(
                command_id,
                {"Step": _uint(state.get("step"), STEP_RUNNING)},
                changes=self._state_changes(state, changed),
            )
        if command_id == CS_GET_HOLY_TOWER_DATA:
            state, changed = self._ensure_state(profile)
            return self._result(
                command_id,
                self._holy_data_values(profile, state),
                changes=self._state_changes(state, changed),
            )
        if command_id == CS_CRAWL_TOWER_CANCEL_DUNGEON:
            return self._cancel(profile, request)
        if command_id == CS_ABANDON_CRAWL_TOWER:
            return self._abandon(profile, request)
        return None


@lru_cache(maxsize=1)
def default_layer() -> TowerBattlesLayer:
    return TowerBattlesLayer()


def handle(
    command_id: int,
    profile: Optional[Mapping[str, Any]],
    body: bytes = b"",
) -> Optional[Result]:
    return default_layer().handle(command_id, profile, body)


__all__ = [
    "BATTLE_WIN",
    "CRAWL_TOWER_IDLE",
    "CRAWL_TOWER_RUNNING",
    "CS_ABANDON_CRAWL_TOWER",
    "CS_CRAWL_TOWER_BATTLE_END",
    "CS_CRAWL_TOWER_CANCEL_DUNGEON",
    "CS_CRAWL_TOWER_ENTER_DUNGEON",
    "CS_CRAWL_TOWER_INIT",
    "CS_CRAWL_TOWER_PREPARE",
    "CS_CRAWL_TOWER_SET_DIFFICULTY",
    "CS_CHOOSE_LEVEL_SETTLEMENT_ITEM",
    "CS_CHOOSE_TOWER_GOSPEL_REWARD",
    "CS_CHOOSE_TOWER_GOSPEL_CLASS",
    "CS_CLICK_TOWER_EVENT_OPTION",
    "CS_ACTIVATE_HOLY_TOWER_ITEM_EFFECT",
    "CS_GET_CRAWL_TOWER_PHALANX",
    "CS_GET_HOLY_TOWER_DATA",
    "CS_GET_HOLY_TOWER_ITEM_ACTIVATE_EFFECT_LIST",
    "CS_GET_HOLY_TOWER_NOW_STEP",
    "CS_GET_LEVEL_SETTLEMENT_GOSPEL_LIST",
    "CS_GET_LEVEL_SETTLEMENT_ITEM_LIST",
    "CS_GET_OPENING_GOSPEL_LIST",
    "CS_GET_TOWER_SHOP_ITEM_LIST",
    "CS_LEVEL_SETTLEMENT_BUY_GOSPEL",
    "CS_OPENING_CHOOSE_GOSPEL",
    "CS_OPENING_CHOOSE_HERO",
    "CS_REQUEST_AVAILABLE_TOWER_GOSPEL_CLASSES",
    "CS_REQUEST_AVAILABLE_TOWER_GOSPELS",
    "CS_REQUEST_CRAWL_TOWER_DATA",
    "CS_REQUEST_CRAWL_TOWER_PERIOD_DATA",
    "CS_REQUEST_CRAWL_TOWER_REMAIN_TIME",
    "CS_REQUEST_CRAWL_TOWER_STATUS",
    "CS_REQUEST_TOWER_CANDIDATE_GOSPEL_LIST",
    "CS_REQUEST_TOWER_EFFECT_ITEMS",
    "CS_REQUEST_TOWER_SHOW_HERO_DATA",
    "CS_REFRESH_TOWER_SHOP_ITEM_LIST",
    "CS_SET_CRAWL_TOWER_PHALANX",
    "CS_SET_PROCESS_DUNGEON",
    "CS_TOWER_GENERATE_OPEN_ITEMS",
    "CS_TOWER_REFRESH_GOSPEL_CLASS",
    "CS_TOWER_START_BATTLE",
    "DEFAULT_CONTENT_DIR",
    "DEFAULT_DIFFICULTY",
    "DEFAULT_LAYER",
    "DEFAULT_MAP_ID",
    "DEFAULT_PROTOCOL_CATALOG",
    "DEFAULT_TOWER_ID",
    "ERROR_CLIENT_REQUEST_PARAM",
    "ERROR_CONFIG",
    "ERROR_MESSAGE_DATA_ILLEGAL",
    "ERROR_SUCCESS",
    "REQUEST_NAMES",
    "RESPONSE_IDS",
    "RESPONSE_NAMES",
    "Result",
    "STATE_KEY",
    "STEP_BATTLE_SETTLED",
    "STEP_BATTLE_SETTLEMENT",
    "STEP_BUY_GOSPEL",
    "STEP_CHOOSE_ITEM",
    "STEP_CHOOSE_FINISH",
    "STEP_CHOOSE_GOSPEL_1",
    "STEP_CHOOSE_GOSPEL_2",
    "STEP_CHOOSE_GOSPEL_3",
    "STEP_CHOOSE_HERO",
    "STEP_RUNNING",
    "TowerBattleCatalog",
    "TowerBattleConfigError",
    "TowerBattlesLayer",
    "default_layer",
    "handle",
]
