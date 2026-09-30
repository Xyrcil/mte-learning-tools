"""Catalog-backed ordinary-fondness and hot-spring response layer.

The local all-unlocked profile owns runtime UIDs, while every configuration
ID in this module comes from :class:`server.content_profile.ContentCatalog`.
That distinction matters for the two legacy hero endpoints: their wire value
is an account's ``HeroUId``, never an ``a_people.id``.
"""

from __future__ import annotations

import copy
import json
import time
import unicodedata
from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType
from typing import Any, Dict, Optional, Tuple, Union

try:  # Package imports in tests; fallbacks support direct server execution.
    from .content_profile import ContentCatalog, default_content_catalog
    from .content_responses import ProtobufDecodeError, iter_wire_fields
    from .inventory_content import (
        InventoryContentError,
        normalize_inventory,
        stable_item_uid,
    )
    from .protobuf_codec import ProtocolMessageCodec
except ImportError:  # pragma: no cover - direct-script compatibility
    from content_profile import ContentCatalog, default_content_catalog  # type: ignore
    from content_responses import ProtobufDecodeError, iter_wire_fields  # type: ignore
    from inventory_content import (  # type: ignore
        InventoryContentError,
        normalize_inventory,
        stable_item_uid,
    )
    from protobuf_codec import ProtocolMessageCodec  # type: ignore


CS_REQUEST_SPRING_DATAS = 95
SC_SPRING_DATAS_RET = 96
CS_REQUEST_DATING_DATAS = 342
SC_DATING_DATAS_RET = 343
CS_REQUEST_HERO_REWARDED_FONDNESS = 703
SC_HERO_REWARDED_FONDNESS_RET = 704

CS_TOUCH_SPRING_MODEL = 251
SC_TOUCH_SPRING_MODEL_RET = 252
CS_GENERATE_SPRING_PRELUDE_PLOT = 678
SC_GENERATE_SPRING_PRELUDE_PLOT_RET = 679
CS_CHOOSE_SPRING_PRELUDE_PLOT = 680
SC_CHOOSE_SPRING_PRELUDE_PLOT_RET = 681
CS_SPRING_PRELUDE_PLOT_END = 683
SC_SPRING_PRELUDE_PLOT_END_RET = 684
CS_WIPE_STEAM_IN_SPRING = 685
SC_WIPE_STEAM_IN_SPRING_RET = 686

CS_SPRING_V2_REQUEST_DATA = 1336
SC_SPRING_V2_REQUEST_DATA_RET = 1337
CS_SPRING_V2_REQUEST_SCENE_DATA = 1338
SC_SPRING_V2_REQUEST_SCENE_DATA_RET = 1339
CS_SPRING_V2_GENERATE_BUBBLE = 1340
SC_SPRING_V2_GENERATE_BUBBLE_RET = 1341
CS_SPRING_V2_TAKE_BUBBLE_REWARD = 1342
SC_SPRING_V2_TAKE_BUBBLE_REWARD_RET = 1343
CS_SPRING_V2_TAKE_AFFECTION_LEVEL_REWARD = 1344
SC_SPRING_V2_TAKE_AFFECTION_LEVEL_REWARD_RET = 1345
CS_SPRING_V2_ACCEPT_TASK = 1346
SC_SPRING_V2_ACCEPT_TASK_RET = 1347
CS_SPRING_V2_COMMIT_TASK = 1348
SC_SPRING_V2_COMMIT_TASK_RET = 1349
NOTIFY_SPRING_V2_GOSPEL_UPDATE = 1354
CS_SPRING_V2_REQUEST_UNLOCKED_SPRING_GOSPEL = 1350
SC_SPRING_V2_REQUEST_UNLOCKED_SPRING_GOSPEL_RET = 1351
CS_SPRING_V2_INTERACT_WITH_GOSPEL = 1352
SC_SPRING_V2_INTERACT_WITH_GOSPEL_RET = 1353
CS_SPRING_V2_CHANGE_SPRING_GOSPEL_NICKNAME = 1407
SC_SPRING_V2_CHANGE_SPRING_GOSPEL_NICKNAME_RET = 1408
CS_SPRING_V2_REQUEST_SPRING_GOSPEL_AFFECTION_LEVEL = 1424
SC_SPRING_V2_REQUEST_SPRING_GOSPEL_AFFECTION_LEVEL_RET = 1425

# Kept independent from the broad offline-content revision so hot-spring
# save repairs are persisted without re-seeding unrelated account domains.
SPRING_CONTENT_REVISION = 1

RESPONSE_IDS = {
    CS_TOUCH_SPRING_MODEL: SC_TOUCH_SPRING_MODEL_RET,
    CS_GENERATE_SPRING_PRELUDE_PLOT: SC_GENERATE_SPRING_PRELUDE_PLOT_RET,
    CS_CHOOSE_SPRING_PRELUDE_PLOT: SC_CHOOSE_SPRING_PRELUDE_PLOT_RET,
    CS_SPRING_PRELUDE_PLOT_END: SC_SPRING_PRELUDE_PLOT_END_RET,
    CS_WIPE_STEAM_IN_SPRING: SC_WIPE_STEAM_IN_SPRING_RET,
    CS_REQUEST_SPRING_DATAS: SC_SPRING_DATAS_RET,
    CS_REQUEST_DATING_DATAS: SC_DATING_DATAS_RET,
    CS_REQUEST_HERO_REWARDED_FONDNESS: SC_HERO_REWARDED_FONDNESS_RET,
    CS_SPRING_V2_REQUEST_DATA: SC_SPRING_V2_REQUEST_DATA_RET,
    CS_SPRING_V2_REQUEST_SCENE_DATA: SC_SPRING_V2_REQUEST_SCENE_DATA_RET,
    CS_SPRING_V2_GENERATE_BUBBLE: SC_SPRING_V2_GENERATE_BUBBLE_RET,
    CS_SPRING_V2_TAKE_BUBBLE_REWARD: SC_SPRING_V2_TAKE_BUBBLE_REWARD_RET,
    CS_SPRING_V2_TAKE_AFFECTION_LEVEL_REWARD: (
        SC_SPRING_V2_TAKE_AFFECTION_LEVEL_REWARD_RET
    ),
    CS_SPRING_V2_ACCEPT_TASK: SC_SPRING_V2_ACCEPT_TASK_RET,
    CS_SPRING_V2_COMMIT_TASK: SC_SPRING_V2_COMMIT_TASK_RET,
    CS_SPRING_V2_REQUEST_UNLOCKED_SPRING_GOSPEL: (
        SC_SPRING_V2_REQUEST_UNLOCKED_SPRING_GOSPEL_RET
    ),
    CS_SPRING_V2_INTERACT_WITH_GOSPEL: SC_SPRING_V2_INTERACT_WITH_GOSPEL_RET,
    CS_SPRING_V2_CHANGE_SPRING_GOSPEL_NICKNAME: (
        SC_SPRING_V2_CHANGE_SPRING_GOSPEL_NICKNAME_RET
    ),
    CS_SPRING_V2_REQUEST_SPRING_GOSPEL_AFFECTION_LEVEL: (
        SC_SPRING_V2_REQUEST_SPRING_GOSPEL_AFFECTION_LEVEL_RET
    ),
}

# Msg.ErrorCode values defined from runtime assembly.
ERROR_SUCCESS = 0
ERROR_ROLE_ID_ILLEGAL = 104
ERROR_HERO_NOT_FOUND = 516
ERROR_SPRING_V2_SCENE_NOT_EXIST = 6001
ERROR_SPRING_V2_BUBBLE_REFRESH_COUNT_NOT_ENOUGH = 6002
ERROR_SPRING_V2_SCENE_BUBBLE_COUNT_HITS_MAXIMUM = 6003
ERROR_SPRING_V2_SCENE_BUBBLE_IS_NOT_REFRESH_TIME = 6004
ERROR_SPRING_V2_GOSPEL_DOES_NOT_MATCH_SCENE = 6005
ERROR_SPRING_V2_GOSPEL_NOT_IN_SCENE = 6006
ERROR_SPRING_V2_GOSPEL_NOT_UNLOCKED = 6007
ERROR_SPRING_V2_AFFECTION_LEVEL_NOT_ENOUGH = 6008
ERROR_SPRING_V2_AFFECTION_REWARD_ALREADY_TAKEN = 6009
ERROR_SPRING_V2_DAILY_TASK_ALREADY_ACCEPTED = 6010
ERROR_SPRING_V2_DAILY_TASK_NOT_ACCEPTED = 6011
ERROR_SPRING_V2_COMMITTED_TASK_NOT_MATCH = 6012
ERROR_SPRING_V2_GOSPEL_NOT_FOUND = 6013
ERROR_SPRING_V2_NO_TASK_CAN_BE_COMMITTED = 6014
ERROR_SPRING_V2_SCENE_NOT_MATCH = 6015
ERROR_SPRING_V2_NO_POSITION_TO_GENERATE_BUBBLE = 6016
ERROR_SPRING_V2_NO_SPRING_DATA = 6017
ERROR_SPRING_V2_NO_BUBBLE_IN_SCENE = 6018
ERROR_SPRING_V2_NO_BUBBLE_POSITION_AVAILABLE = 6019
ERROR_SPRING_V2_NO_BUBBLE_REWARD_AVAILABLE = 6020
ERROR_SPRING_V2_NO_BUBBLE_GROUP_AVAILABLE = 6021
ERROR_SPRING_V2_BUBBLE_GROUP_RANDOM_FAILED = 6022
ERROR_SPRING_V2_NO_TASK_GROUP_AVAILABLE = 6023
ERROR_SPRING_V2_NICKNAME_UNCHANGED = 6024
ERROR_SPRING_V2_NICKNAME_OUT_OF_LENGTH = 6025
ERROR_SPRING_V2_NICKNAME_INVALID = 6026
ERROR_SPRING_V2_BUBBLE_TAKE_LIMIT_REACHED = 6027
ERROR_EXCEED_ITEM_LIMIT_COUNT = 429
ERROR_CLIENT_REQUEST_PARAM = 32000
ERROR_MESSAGE_DATA_ILLEGAL = 32769
ERROR_CONFIG = 32772

UINT32_MAX = (1 << 32) - 1
UINT64_MAX = (1 << 64) - 1
BUBBLE_LIMIT_GLOBAL_ID = 9_991_127
ORACLE_CONTENT_STATE_KEY = "oracle_content"
LEGACY_REST_SLOT_COUNT = 6

ORDINARY_FONDNESS_LEVEL = 7
ORDINARY_FONDNESS_EXP = 4_800
SPRING_AFFECTION_LEVEL = 5
SPRING_AFFECTION_EXP = 1_000

RESOURCE_MINIDIAMOND = 1
RESOURCE_GOLD = 2
RESOURCE_ITEM = 4
RESOURCE_TOP_LEVEL_FIELDS = {
    RESOURCE_MINIDIAMOND: "minidiamond",
    RESOURCE_GOLD: "gold",
}
TASK_UNFINISHED = 0
TASK_FINISHED = 1
TASK_REWARDED = 2
NICKNAME_UTF16_LIMIT = 16

REQUIRED_TABLE_FIELDS = {
    "a_favor": {"id", "people_id", "level", "exp"},
    "a_global": {"id", "int_value"},
    "a_hotspring_global": {
        "id",
        "hs_gospel_id",
        "location_list",
        "akfreward_pool_id",
    },
    "a_hs_afkreward_pool": {
        "id",
        "spring_id",
        "akfreward_pool_id",
        "initial_rate",
        "item_list",
    },
    "a_hs_gospel": {
        "id",
        "gospel_id",
        "spring_id",
        "daily_convers_list",
    },
    "a_hs_favor_reward": {
        "id",
        "hs_gospel_id",
        "spring_id",
        "favor_level",
        "favor_exp_need",
        "conversation_unlock",
        "favor_reward_storypack",
        "favor_reward_item",
    },
    "a_hs_task": {
        "id",
        "gospel_belong",
        "task_group",
        "front_convers_group_id",
        "after_convers_group_id",
        "favor_score_increase",
    },
    "a_hs_conversation": {
        "id",
        "conversation_group_id",
        "conversation_last",
        "conversation_content",
    },
    "a_item": {"id", "max_stack_num"},
    "a_storypack": {"id"},
    "a_hotspring_scene": {
        "id",
        "special",
        "weight",
        "hotspring_option",
        "hotspring_reward",
    },
    "a_hotspring_feedback": {
        "id",
        "security_max",
        "security_click",
        "satisfy_click",
        "security_drag",
        "satisfy_drag",
    },
    "a_hotspring_satisfy": {"id", "satisfy_judge"},
}


class SpringCatalogError(ValueError):
    """Raised when the verified static tables do not close over this feature."""


class SpringProfileError(ValueError):
    """Raised when a profile cannot be seeded without inventing runtime UIDs."""


class SpringApplicationError(ValueError):
    """An atomic spring mutation that cannot safely be projected."""

    def __init__(self, error_code: int, reason: str) -> None:
        super().__init__(reason)
        self.error_code = int(error_code)
        self.reason = reason


@dataclass(frozen=True)
class OrdinaryFavorDefinition:
    hero_id: int
    level: int
    fondness: int
    reward_levels: Tuple[int, ...]
    record_ids: Tuple[int, ...]


@dataclass(frozen=True)
class SpringReward:
    res_type: int
    item_id: int
    count: int

    def as_state(self) -> Dict[str, int]:
        return {
            "type": self.res_type,
            "id": self.item_id,
            "num": self.count,
        }

    def as_wire(self) -> Dict[str, int]:
        return {
            "ResType": self.res_type,
            "ItemId": self.item_id,
            "Count": self.count,
        }


@dataclass(frozen=True)
class SpringAffectionRewardDefinition:
    record_id: int
    level: int
    affection_required: int
    conversation_ids: Tuple[int, ...]
    storypack_id: int
    rewards: Tuple[SpringReward, ...]


@dataclass(frozen=True)
class SpringBubbleDefinition:
    bubble_id: int
    scene_id: int
    pool_id: int
    weight: int
    reward_string: str
    rewards: Tuple[SpringReward, ...]


@dataclass(frozen=True)
class SpringGospelDefinition:
    spring_gospel_id: int
    gospel_id: int
    scene_id: int
    affection: int
    affection_level: int
    reward_levels: Tuple[int, ...]
    reward_record_ids: Tuple[int, ...]
    affection_rewards: Tuple[SpringAffectionRewardDefinition, ...]


@dataclass(frozen=True)
class SpringTaskDefinition:
    task_id: int
    spring_gospel_id: int
    task_type: int
    front_conversation_group_id: int
    after_conversation_group_id: int
    favor_score_increase: int


@dataclass(frozen=True)
class SpringResult:
    """A protobuf response and optional shallow-merge account changes."""

    response_id: int
    body: bytes
    changes: Dict[str, Any] = field(default_factory=dict)
    error_code: int = ERROR_SUCCESS
    supported: bool = True
    reason: str = ""
    notifications: Tuple[Tuple[int, bytes], ...] = ()

    @property
    def ok(self) -> bool:
        return self.error_code == ERROR_SUCCESS


def _int(value: Any, context: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SpringCatalogError(f"{context} must be an integer")
    return value


def _safe_uint(value: Any, default: int, maximum: int = UINT64_MAX) -> int:
    if isinstance(value, bool):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return parsed if 0 <= parsed <= maximum else default


def _mapping_lookup(value: Any, record_id: int) -> Any:
    if not isinstance(value, Mapping):
        return None
    if record_id in value:
        return value[record_id]
    return value.get(str(record_id))


def _owned_gospel_uids(profile: Mapping[str, Any]) -> frozenset[int]:
    """Return account Gospel UIDs that are unambiguous and runtime-resolvable."""

    raw_gospels = profile.get("gospels")
    if not isinstance(raw_gospels, list):
        return frozenset()

    counts: Dict[int, int] = {}
    for raw in raw_gospels:
        if not isinstance(raw, Mapping):
            continue
        gospel_uid = _safe_uint(raw.get("uid"), 0, UINT64_MAX)
        if gospel_uid > 0:
            counts[gospel_uid] = counts.get(gospel_uid, 0) + 1
    return frozenset(uid for uid, count in counts.items() if count == 1)


def _legacy_rest_list(profile: Mapping[str, Any]) -> list[Dict[str, int]]:
    """Project the persisted Oracle rest slots into a safe legacy snapshot.

    Oracle owns mutations for commands 97/99. Command 95 only reads that
    state, but it must also tolerate pre-normalization saves because the public
    handler is usable independently of the server's login seeding path.
    Sorting candidates before de-duplication makes even mixed int/string keys
    and duplicate dirty values deterministic.
    """

    oracle_state = profile.get(ORACLE_CONTENT_STATE_KEY)
    oracle_state = oracle_state if isinstance(oracle_state, Mapping) else {}
    rest_state = oracle_state.get("rest")
    rest_state = rest_state if isinstance(rest_state, Mapping) else {}
    raw_slots = rest_state.get("slots")
    if not isinstance(raw_slots, Mapping):
        return []

    owned_uids = _owned_gospel_uids(profile)
    candidates: list[Tuple[int, int]] = []
    for raw_position, raw_uid in raw_slots.items():
        position_id = _safe_uint(
            raw_position,
            LEGACY_REST_SLOT_COUNT,
            UINT32_MAX,
        )
        gospel_uid = _safe_uint(raw_uid, 0, UINT64_MAX)
        if (
            position_id >= LEGACY_REST_SLOT_COUNT
            or gospel_uid <= 0
            or gospel_uid not in owned_uids
        ):
            continue
        candidates.append((position_id, gospel_uid))

    result: list[Dict[str, int]] = []
    used_positions = set()
    used_uids = set()
    for position_id, gospel_uid in sorted(candidates):
        if position_id in used_positions or gospel_uid in used_uids:
            continue
        used_positions.add(position_id)
        used_uids.add(gospel_uid)
        result.append(
            {
                "PositionId": position_id,
                "GospelUId": gospel_uid,
            }
        )
    return result


def _csv_ids(value: Any, context: str) -> Tuple[int, ...]:
    if not isinstance(value, str):
        raise SpringCatalogError(f"{context} must be a comma-separated string")
    try:
        result = tuple(int(part.strip()) for part in value.split(",") if part.strip())
    except ValueError as exc:
        raise SpringCatalogError(f"{context} contains a non-integer ID") from exc
    if (
        not result
        or any(item <= 0 for item in result)
        or len(set(result)) != len(result)
    ):
        raise SpringCatalogError(f"{context} contains invalid or duplicate IDs")
    return result


def _optional_csv_ids(value: Any, context: str) -> Tuple[int, ...]:
    if value in (None, ""):
        return ()
    return _csv_ids(value, context)


def _json_value(value: Any, context: str) -> Any:
    if not isinstance(value, str):
        raise SpringCatalogError(f"{context} must be JSON text")
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        raise SpringCatalogError(f"{context} is invalid JSON: {exc}") from exc


def _json_ids(value: Any, context: str) -> Tuple[int, ...]:
    rows = _json_value(value, context)
    if not isinstance(rows, list):
        raise SpringCatalogError(f"{context} must decode to an array")
    ids = tuple(_int(item, context) for item in rows)
    if any(item <= 0 for item in ids) or len(set(ids)) != len(ids):
        raise SpringCatalogError(f"{context} contains invalid or duplicate IDs")
    return ids


def _json_locations(value: Any, context: str) -> Tuple[Tuple[int, int], ...]:
    rows = _json_value(value, context)
    if not isinstance(rows, list):
        raise SpringCatalogError(f"{context} must decode to an array")
    result = []
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise SpringCatalogError(f"{context}[{index}] must be an object")
        x = _int(row.get("x_position"), f"{context}[{index}].x_position")
        y = _int(row.get("y_position"), f"{context}[{index}].y_position")
        if not -(1 << 31) <= x < (1 << 31) or not -(1 << 31) <= y < (1 << 31):
            raise SpringCatalogError(f"{context}[{index}] is outside int32")
        result.append((x, y))
    if not result or len(result) != len(set(result)):
        raise SpringCatalogError(f"{context} is empty or contains duplicate positions")
    return tuple(result)


def _json_pool_ids(value: Any, context: str) -> Tuple[int, ...]:
    rows = _json_value(value, context)
    if not isinstance(rows, Mapping):
        raise SpringCatalogError(f"{context} must decode to an object")
    result = []
    for raw_pool_id, raw_weight in rows.items():
        try:
            pool_id = int(raw_pool_id)
        except (TypeError, ValueError, OverflowError) as exc:
            raise SpringCatalogError(f"{context} has an invalid pool ID") from exc
        weight = _int(raw_weight, f"{context}[{raw_pool_id!r}]")
        if pool_id <= 0 or weight <= 0:
            raise SpringCatalogError(
                f"{context} contains a non-positive pool or weight"
            )
        result.append(pool_id)
    if not result or len(result) != len(set(result)):
        raise SpringCatalogError(f"{context} is empty or contains duplicate pools")
    return tuple(sorted(result))


def _parse_rewards(
    value: Any,
    context: str,
    *,
    item_ids: frozenset[int],
) -> Tuple[SpringReward, ...]:
    if value in (None, ""):
        return ()
    rows = _json_value(value, context)
    if not isinstance(rows, list):
        raise SpringCatalogError(f"{context} must decode to an array")
    result = []
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise SpringCatalogError(f"{context}[{index}] must be an object")
        res_type = _int(row.get("type"), f"{context}[{index}].type")
        item_id = _int(row.get("id"), f"{context}[{index}].id")
        count = _int(row.get("num"), f"{context}[{index}].num")
        if count <= 0 or count > UINT32_MAX:
            raise SpringCatalogError(f"{context}[{index}] has an invalid count")
        if res_type in RESOURCE_TOP_LEVEL_FIELDS:
            if item_id != 0:
                raise SpringCatalogError(
                    f"{context}[{index}] currency has a nonzero item ID"
                )
        elif res_type == RESOURCE_ITEM:
            if item_id not in item_ids:
                raise SpringCatalogError(
                    f"{context}[{index}] references unknown a_item {item_id}"
                )
        else:
            raise SpringCatalogError(
                f"{context}[{index}] has unsupported resource type {res_type}"
            )
        result.append(SpringReward(res_type, item_id, count))
    return tuple(result)


class SpringStaticCatalog:
    """Validated feature projection over the shared immutable catalog."""

    def __init__(self, catalog: ContentCatalog) -> None:
        self.catalog = catalog
        for table_name, required in REQUIRED_TABLE_FIELDS.items():
            try:
                table = catalog.table(table_name)
            except KeyError:
                raise SpringCatalogError(f"catalog is missing {table_name}") from None
            missing = required - set(table.fields)
            if missing:
                raise SpringCatalogError(
                    f"{table_name} is missing required fields: {sorted(missing)}"
                )

        item_max_stacks: Dict[int, int] = {}
        for row in catalog.table("a_item").records:
            item_id = _int(row.get("id"), "a_item.id")
            maximum = _int(row.get("max_stack_num"), f"a_item[{item_id}].max_stack_num")
            if maximum <= 0:
                continue
            item_max_stacks[item_id] = maximum
        if not item_max_stacks:
            raise SpringCatalogError("a_item has no storable items")
        self.item_max_stacks = MappingProxyType(item_max_stacks)
        self.item_ids = frozenset(item_max_stacks)

        self.hero_ids = tuple(catalog.playable_people_ids)
        if len(self.hero_ids) != 6:
            raise SpringCatalogError(
                "ordinary fondness requires exactly six playable heroes"
            )

        favor_rows: Dict[int, list[Mapping[str, Any]]] = {
            hero_id: [] for hero_id in self.hero_ids
        }
        for row in catalog.table("a_favor").records:
            hero_id = _int(row.get("people_id"), "a_favor.people_id")
            if hero_id not in favor_rows:
                raise SpringCatalogError(
                    f"a_favor[{row['id']}] references non-playable hero {hero_id}"
                )
            favor_rows[hero_id].append(row)

        ordinary: Dict[int, OrdinaryFavorDefinition] = {}
        for hero_id in self.hero_ids:
            rows = sorted(
                favor_rows[hero_id],
                key=lambda row: _int(row.get("level"), "a_favor.level"),
            )
            levels = tuple(_int(row.get("level"), "a_favor.level") for row in rows)
            if levels != tuple(range(ORDINARY_FONDNESS_LEVEL + 1)):
                raise SpringCatalogError(
                    f"a_favor hero {hero_id} levels are not 0..{ORDINARY_FONDNESS_LEVEL}"
                )
            maximum = rows[-1]
            fondness = _int(maximum.get("exp"), "a_favor.exp")
            if fondness != ORDINARY_FONDNESS_EXP:
                raise SpringCatalogError(
                    f"a_favor hero {hero_id} level {ORDINARY_FONDNESS_LEVEL} "
                    f"requires {fondness}, expected {ORDINARY_FONDNESS_EXP}"
                )
            ordinary[hero_id] = OrdinaryFavorDefinition(
                hero_id=hero_id,
                level=levels[-1],
                fondness=fondness,
                reward_levels=tuple(level for level in levels if level > 0),
                record_ids=tuple(_int(row.get("id"), "a_favor.id") for row in rows),
            )
        self.ordinary_favor = MappingProxyType(ordinary)

        scene_table = catalog.table("a_hotspring_global")
        self.scene_ids = tuple(scene_table.ids)
        if len(self.scene_ids) != 3:
            raise SpringCatalogError(
                "a_hotspring_global must contain exactly three scenes"
            )

        declared_by_scene = {
            _int(row.get("id"), "a_hotspring_global.id"): _csv_ids(
                row.get("hs_gospel_id"),
                f"a_hotspring_global[{row['id']}].hs_gospel_id",
            )
            for row in scene_table.records
        }
        locations_by_scene: Dict[int, Tuple[Tuple[int, int], ...]] = {}
        pool_ids_by_scene: Dict[int, Tuple[int, ...]] = {}
        for row in scene_table.records:
            scene_id = _int(row.get("id"), "a_hotspring_global.id")
            locations_by_scene[scene_id] = _json_locations(
                row.get("location_list"),
                f"a_hotspring_global[{scene_id}].location_list",
            )
            pool_ids_by_scene[scene_id] = _json_pool_ids(
                row.get("akfreward_pool_id"),
                f"a_hotspring_global[{scene_id}].akfreward_pool_id",
            )
        self.locations_by_scene = MappingProxyType(locations_by_scene)
        self.pool_ids_by_scene = MappingProxyType(pool_ids_by_scene)

        bubble_rows_by_scene: Dict[int, list[SpringBubbleDefinition]] = {
            scene_id: [] for scene_id in self.scene_ids
        }
        bubble_by_id: Dict[int, SpringBubbleDefinition] = {}
        for row in catalog.table("a_hs_afkreward_pool").records:
            bubble_id = _int(row.get("id"), "a_hs_afkreward_pool.id")
            scene_id = _int(row.get("spring_id"), "a_hs_afkreward_pool.spring_id")
            pool_id = _int(
                row.get("akfreward_pool_id"),
                "a_hs_afkreward_pool.akfreward_pool_id",
            )
            if scene_id not in bubble_rows_by_scene:
                raise SpringCatalogError(
                    f"a_hs_afkreward_pool[{bubble_id}] references unknown scene {scene_id}"
                )
            if pool_id not in pool_ids_by_scene[scene_id]:
                raise SpringCatalogError(
                    f"a_hs_afkreward_pool[{bubble_id}] references undeclared pool {pool_id}"
                )
            weight = _int(
                row.get("initial_rate"),
                f"a_hs_afkreward_pool[{bubble_id}].initial_rate",
            )
            if weight <= 0:
                raise SpringCatalogError(
                    f"a_hs_afkreward_pool[{bubble_id}] has a non-positive weight"
                )
            reward_string = row.get("item_list")
            rewards = _parse_rewards(
                reward_string,
                f"a_hs_afkreward_pool[{bubble_id}].item_list",
                item_ids=self.item_ids,
            )
            if not rewards:
                raise SpringCatalogError(
                    f"a_hs_afkreward_pool[{bubble_id}] has no reward"
                )
            definition = SpringBubbleDefinition(
                bubble_id=bubble_id,
                scene_id=scene_id,
                pool_id=pool_id,
                weight=weight,
                reward_string=str(reward_string),
                rewards=rewards,
            )
            bubble_rows_by_scene[scene_id].append(definition)
            bubble_by_id[bubble_id] = definition
        normalized_bubbles: Dict[int, Tuple[SpringBubbleDefinition, ...]] = {}
        for scene_id, rows in bubble_rows_by_scene.items():
            ordered = tuple(sorted(rows, key=lambda item: item.bubble_id))
            if not ordered:
                raise SpringCatalogError(f"scene {scene_id} has no bubble rewards")
            normalized_bubbles[scene_id] = ordered
        self.bubble_rewards_by_scene = MappingProxyType(normalized_bubbles)
        self.bubble_rewards_by_id = MappingProxyType(bubble_by_id)

        hs_rows = catalog.table("a_hs_gospel").records
        if len(hs_rows) != 6:
            raise SpringCatalogError("a_hs_gospel must contain exactly six records")
        hs_by_id = {_int(row.get("id"), "a_hs_gospel.id"): row for row in hs_rows}
        actual_by_scene: Dict[int, list[int]] = {
            scene_id: [] for scene_id in self.scene_ids
        }
        for spring_gospel_id, row in hs_by_id.items():
            scene_id = _int(row.get("spring_id"), "a_hs_gospel.spring_id")
            if scene_id not in actual_by_scene:
                raise SpringCatalogError(
                    f"a_hs_gospel[{spring_gospel_id}] references unknown scene {scene_id}"
                )
            gospel_id = _int(row.get("gospel_id"), "a_hs_gospel.gospel_id")
            try:
                catalog.record("a_gospel", gospel_id)
            except KeyError:
                raise SpringCatalogError(
                    f"a_hs_gospel[{spring_gospel_id}] references unknown gospel {gospel_id}"
                ) from None
            actual_by_scene[scene_id].append(spring_gospel_id)

        scene_members: Dict[int, Tuple[int, ...]] = {}
        for scene_id in self.scene_ids:
            actual = tuple(actual_by_scene[scene_id])
            declared = declared_by_scene[scene_id]
            if len(actual) != 2 or set(actual) != set(declared):
                raise SpringCatalogError(
                    f"scene {scene_id} does not contain the declared two spring gospels"
                )
            scene_members[scene_id] = declared
        self.spring_gospel_ids_by_scene = MappingProxyType(scene_members)

        reward_rows: Dict[int, list[Mapping[str, Any]]] = {
            spring_gospel_id: [] for spring_gospel_id in hs_by_id
        }
        for row in catalog.table("a_hs_favor_reward").records:
            spring_gospel_id = _int(
                row.get("hs_gospel_id"), "a_hs_favor_reward.hs_gospel_id"
            )
            if spring_gospel_id not in reward_rows:
                raise SpringCatalogError(
                    f"a_hs_favor_reward[{row['id']}] references unknown spring gospel "
                    f"{spring_gospel_id}"
                )
            expected_scene = _int(
                hs_by_id[spring_gospel_id].get("spring_id"),
                "a_hs_gospel.spring_id",
            )
            if (
                _int(row.get("spring_id"), "a_hs_favor_reward.spring_id")
                != expected_scene
            ):
                raise SpringCatalogError(
                    f"a_hs_favor_reward[{row['id']}] has a mismatched scene"
                )
            reward_rows[spring_gospel_id].append(row)

        spring_gospels: Dict[int, SpringGospelDefinition] = {}
        by_gospel: Dict[int, list[int]] = {}
        for spring_gospel_id, hs_row in hs_by_id.items():
            rows = sorted(
                reward_rows[spring_gospel_id],
                key=lambda row: _int(row.get("favor_level"), "favor_level"),
            )
            levels = tuple(
                _int(row.get("favor_level"), "a_hs_favor_reward.favor_level")
                for row in rows
            )
            if levels != tuple(range(1, SPRING_AFFECTION_LEVEL + 1)):
                raise SpringCatalogError(
                    f"spring gospel {spring_gospel_id} reward levels are not "
                    f"1..{SPRING_AFFECTION_LEVEL}"
                )
            affection = _int(
                rows[-1].get("favor_exp_need"),
                "a_hs_favor_reward.favor_exp_need",
            )
            if affection != SPRING_AFFECTION_EXP:
                raise SpringCatalogError(
                    f"spring gospel {spring_gospel_id} level {SPRING_AFFECTION_LEVEL} "
                    f"requires {affection}, expected {SPRING_AFFECTION_EXP}"
                )
            gospel_id = _int(hs_row.get("gospel_id"), "a_hs_gospel.gospel_id")
            affection_rewards = []
            for row in rows:
                record_id = _int(row.get("id"), "a_hs_favor_reward.id")
                storypack_id = _int(
                    row.get("favor_reward_storypack"),
                    f"a_hs_favor_reward[{record_id}].favor_reward_storypack",
                )
                if storypack_id:
                    try:
                        catalog.record("a_storypack", storypack_id)
                    except KeyError:
                        raise SpringCatalogError(
                            f"a_hs_favor_reward[{record_id}] references unknown "
                            f"storypack {storypack_id}"
                        ) from None
                affection_rewards.append(
                    SpringAffectionRewardDefinition(
                        record_id=record_id,
                        level=_int(
                            row.get("favor_level"),
                            f"a_hs_favor_reward[{record_id}].favor_level",
                        ),
                        affection_required=_int(
                            row.get("favor_exp_need"),
                            f"a_hs_favor_reward[{record_id}].favor_exp_need",
                        ),
                        conversation_ids=_optional_csv_ids(
                            row.get("conversation_unlock"),
                            f"a_hs_favor_reward[{record_id}].conversation_unlock",
                        ),
                        storypack_id=storypack_id,
                        rewards=_parse_rewards(
                            row.get("favor_reward_item"),
                            f"a_hs_favor_reward[{record_id}].favor_reward_item",
                            item_ids=self.item_ids,
                        ),
                    )
                )
            definition = SpringGospelDefinition(
                spring_gospel_id=spring_gospel_id,
                gospel_id=gospel_id,
                scene_id=_int(hs_row.get("spring_id"), "a_hs_gospel.spring_id"),
                affection=affection,
                affection_level=levels[-1],
                reward_levels=levels,
                reward_record_ids=tuple(
                    _int(row.get("id"), "a_hs_favor_reward.id") for row in rows
                ),
                affection_rewards=tuple(affection_rewards),
            )
            spring_gospels[spring_gospel_id] = definition
            by_gospel.setdefault(gospel_id, []).append(spring_gospel_id)
        self.spring_gospels = MappingProxyType(spring_gospels)
        self.affection_rewards_by_gospel = MappingProxyType(
            {
                spring_gospel_id: MappingProxyType(
                    {reward.level: reward for reward in definition.affection_rewards}
                )
                for spring_gospel_id, definition in spring_gospels.items()
            }
        )
        self.spring_gospel_ids_by_gospel = MappingProxyType(
            {gospel_id: tuple(ids) for gospel_id, ids in by_gospel.items()}
        )

        conversation_groups: Dict[int, list[Mapping[str, Any]]] = {}
        conversations_by_id: Dict[int, Mapping[str, Any]] = {}
        for row in catalog.table("a_hs_conversation").records:
            conversation_id = _int(row.get("id"), "a_hs_conversation.id")
            group_id = _int(
                row.get("conversation_group_id"),
                "a_hs_conversation.conversation_group_id",
            )
            conversation_groups.setdefault(group_id, []).append(row)
            conversations_by_id[conversation_id] = row

        referenced_conversations: Dict[int, str] = {}
        for spring_gospel_id, hs_row in hs_by_id.items():
            for conversation_id in _json_ids(
                hs_row.get("daily_convers_list"),
                f"a_hs_gospel[{spring_gospel_id}].daily_convers_list",
            ):
                referenced_conversations[conversation_id] = (
                    f"a_hs_gospel[{spring_gospel_id}].daily_convers_list"
                )
        for definition in spring_gospels.values():
            for reward in definition.affection_rewards:
                for conversation_id in reward.conversation_ids:
                    referenced_conversations[conversation_id] = (
                        f"a_hs_favor_reward[{reward.record_id}].conversation_unlock"
                    )
        for conversation_id, context in referenced_conversations.items():
            conversation = conversations_by_id.get(conversation_id)
            if conversation is None:
                raise SpringCatalogError(
                    f"{context} references missing conversation {conversation_id}"
                )
            if not str(conversation.get("conversation_content") or "").strip():
                raise SpringCatalogError(
                    f"{context} references empty conversation {conversation_id}"
                )
        self.referenced_conversation_ids = tuple(sorted(referenced_conversations))
        self.conversations_by_id = MappingProxyType(conversations_by_id)

        task_rows: Dict[int, list[SpringTaskDefinition]] = {
            spring_gospel_id: [] for spring_gospel_id in spring_gospels
        }
        tasks_by_id: Dict[int, SpringTaskDefinition] = {}
        for row in catalog.table("a_hs_task").records:
            task_id = _int(row.get("id"), "a_hs_task.id")
            spring_gospel_id = _int(row.get("gospel_belong"), "a_hs_task.gospel_belong")
            if spring_gospel_id not in task_rows:
                raise SpringCatalogError(
                    f"a_hs_task[{task_id}] references unknown spring gospel "
                    f"{spring_gospel_id}"
                )
            front_group = _int(
                row.get("front_convers_group_id"),
                "a_hs_task.front_convers_group_id",
            )
            after_group = _int(
                row.get("after_convers_group_id"),
                "a_hs_task.after_convers_group_id",
            )
            for group_id, label in (
                (front_group, "front"),
                (after_group, "after"),
            ):
                conversations = conversation_groups.get(group_id, [])
                starts = [
                    conversation
                    for conversation in conversations
                    if _int(
                        conversation.get("conversation_last"),
                        "a_hs_conversation.conversation_last",
                    )
                    == 0
                ]
                if len(starts) != 1 or any(
                    not str(conversation.get("conversation_content") or "").strip()
                    for conversation in conversations
                ):
                    raise SpringCatalogError(
                        f"a_hs_task[{task_id}] has an incomplete textual {label} conversation "
                        f"start for group {group_id}"
                    )
            favor_score_increase = _int(
                row.get("favor_score_increase"),
                f"a_hs_task[{task_id}].favor_score_increase",
            )
            if favor_score_increase <= 0:
                raise SpringCatalogError(
                    f"a_hs_task[{task_id}] has a non-positive favor increase"
                )
            definition = SpringTaskDefinition(
                task_id=task_id,
                spring_gospel_id=spring_gospel_id,
                task_type=_int(row.get("task_group"), "a_hs_task.task_group"),
                front_conversation_group_id=front_group,
                after_conversation_group_id=after_group,
                favor_score_increase=favor_score_increase,
            )
            task_rows[spring_gospel_id].append(definition)
            tasks_by_id[task_id] = definition

        normalized_tasks: Dict[int, Tuple[SpringTaskDefinition, ...]] = {}
        for spring_gospel_id, rows in task_rows.items():
            ordered = tuple(sorted(rows, key=lambda row: row.task_id))
            if len(ordered) != 5:
                raise SpringCatalogError(
                    f"spring gospel {spring_gospel_id} must have exactly five tasks"
                )
            normalized_tasks[spring_gospel_id] = ordered
        self.tasks_by_spring_gospel = MappingProxyType(normalized_tasks)
        self.tasks_by_id = MappingProxyType(tasks_by_id)

        legacy_plot_ids = []
        for row in catalog.table("a_hotspring_scene").records:
            plot_id = _int(row.get("id"), "a_hotspring_scene.id")
            # Parse every configured reward even though the legacy
            # flow does not grant it: this proves each selected row is safe for
            # the runtime's eager JSON conversion in the 679 callback.
            _parse_rewards(
                row.get("hotspring_reward"),
                f"a_hotspring_scene[{plot_id}].hotspring_reward",
                item_ids=self.item_ids,
            )
            special = _int(row.get("special"), f"a_hotspring_scene[{plot_id}].special")
            weight = _int(row.get("weight"), f"a_hotspring_scene[{plot_id}].weight")
            if not special and weight > 0:
                legacy_plot_ids.append(plot_id)
        if not legacy_plot_ids:
            raise SpringCatalogError(
                "a_hotspring_scene has no deterministic prelude choice"
            )
        self.legacy_plot_ids = tuple(sorted(legacy_plot_ids))
        self.legacy_default_plot_id = self.legacy_plot_ids[0]

        feedback_rows = tuple(catalog.table("a_hotspring_feedback").records)
        if not feedback_rows:
            raise SpringCatalogError("a_hotspring_feedback is empty")
        self.legacy_initial_safety = min(
            _int(row.get("security_max"), "a_hotspring_feedback.security_max")
            for row in feedback_rows
        )
        click_safety = {
            _int(row.get("security_click"), "a_hotspring_feedback.security_click")
            for row in feedback_rows
        }
        click_satisfaction = {
            _int(row.get("satisfy_click"), "a_hotspring_feedback.satisfy_click")
            for row in feedback_rows
        }
        drag_safety = {
            _int(row.get("security_drag"), "a_hotspring_feedback.security_drag")
            for row in feedback_rows
        }
        drag_satisfaction = {
            _int(row.get("satisfy_drag"), "a_hotspring_feedback.satisfy_drag")
            for row in feedback_rows
        }
        if any(
            len(values) != 1
            for values in (
                click_safety,
                click_satisfaction,
                drag_safety,
                drag_satisfaction,
            )
        ):
            raise SpringCatalogError(
                "a_hotspring_feedback interaction deltas are not globally deterministic"
            )
        self.legacy_interaction_deltas = MappingProxyType(
            {
                0: (next(iter(click_safety)), next(iter(click_satisfaction))),
                1: (next(iter(drag_safety)), next(iter(drag_satisfaction))),
            }
        )
        satisfy_rows = tuple(catalog.table("a_hotspring_satisfy").records)
        self.legacy_max_satisfaction = max(
            _int(row.get("satisfy_judge"), "a_hotspring_satisfy.satisfy_judge")
            for row in satisfy_rows
        )

        limit_row = catalog.record("a_global", BUBBLE_LIMIT_GLOBAL_ID)
        self.bubbles_limit = _int(
            limit_row.get("int_value"), f"a_global[{BUBBLE_LIMIT_GLOBAL_ID}].int_value"
        )
        if self.bubbles_limit <= 0:
            raise SpringCatalogError("spring-v2 bubble limit must be positive")


def _coerce_catalog(
    catalog: Optional[Union[ContentCatalog, str, Path]],
) -> ContentCatalog:
    if catalog is None:
        return default_content_catalog()
    if isinstance(catalog, ContentCatalog):
        return catalog
    return ContentCatalog.load(catalog)


def _decode_uint_field(
    data: bytes,
    field_number: int,
    *,
    maximum: int,
) -> int:
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise ProtobufDecodeError("request body must be bytes")
    result = 0
    for number, wire_type, value in iter_wire_fields(bytes(data)):
        if number != field_number:
            continue
        if wire_type != 0:
            raise ProtobufDecodeError(f"field {field_number} is not a varint")
        result = int(value)
        if result > maximum:
            raise ProtobufDecodeError(
                f"field {field_number} is outside its uint domain"
            )
    return result


def _decode_int32_field(data: bytes, field_number: int) -> int:
    raw = _decode_uint_field(data, field_number, maximum=UINT64_MAX)
    if raw <= (1 << 31) - 1:
        return raw
    if raw >= UINT64_MAX - ((1 << 31) - 1):
        return raw - (1 << 64)
    raise ProtobufDecodeError(f"field {field_number} is outside its int32 domain")


def _decode_bytes_field(data: bytes, field_number: int) -> bytes:
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise ProtobufDecodeError("request body must be bytes")
    result = b""
    for number, wire_type, value in iter_wire_fields(bytes(data)):
        if number != field_number:
            continue
        if wire_type != 2 or not isinstance(value, bytes):
            raise ProtobufDecodeError(f"field {field_number} is not length-delimited")
        result = value
    return result


def _decode_string_field(data: bytes, field_number: int) -> str:
    raw = _decode_bytes_field(data, field_number)
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProtobufDecodeError(f"field {field_number} is not valid UTF-8") from exc


def _decode_maid_info(data: bytes) -> Dict[str, int]:
    raw = _decode_bytes_field(data, 1)
    if not raw:
        raise ProtobufDecodeError("CS_TouchSpringModel.CharInfo is missing")
    char_type = _decode_uint_field(raw, 1, maximum=UINT32_MAX)
    char_id = _decode_uint_field(raw, 2, maximum=UINT32_MAX)
    if char_type not in {0, 1} or char_id <= 0:
        raise ProtobufDecodeError("CS_TouchSpringModel.CharInfo is invalid")
    return {"CharType": char_type, "CharId": char_id}


def _validate_wire(data: bytes) -> None:
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise ProtobufDecodeError("request body must be bytes")
    tuple(iter_wire_fields(bytes(data)))


class SpringContentLayer:
    """Read-only protocol handlers plus deterministic profile seeding."""

    def __init__(
        self,
        catalog: Optional[Union[ContentCatalog, str, Path]] = None,
        *,
        codec: Optional[ProtocolMessageCodec] = None,
    ) -> None:
        self.content_catalog = _coerce_catalog(catalog)
        self.catalog = SpringStaticCatalog(self.content_catalog)
        self.codec = codec or ProtocolMessageCodec()

    @staticmethod
    def _task_state(
        definition: SpringTaskDefinition,
        *,
        accept_time: int = 0,
    ) -> Dict[str, Any]:
        return {
            "task_id": definition.task_id,
            "task_type": definition.task_type,
            "task_status": 0,
            "progress": 0,
            "accept_time": max(0, int(accept_time)),
            "finish_time": 0,
            "commit_time": 0,
        }

    def _normalize_task_state(
        self,
        value: Any,
        spring_gospel_id: int,
    ) -> Optional[Dict[str, Any]]:
        if not isinstance(value, Mapping):
            return None
        task_id = _safe_uint(value.get("task_id"), 0, UINT32_MAX)
        definition = self.catalog.tasks_by_id.get(task_id)
        if definition is None or definition.spring_gospel_id != spring_gospel_id:
            return None
        return {
            "task_id": task_id,
            "task_type": _safe_uint(
                value.get("task_type"), definition.task_type, UINT32_MAX
            ),
            "task_status": _safe_uint(value.get("task_status"), 0, 2),
            "progress": _safe_uint(value.get("progress"), 0, UINT32_MAX),
            "accept_time": _safe_uint(value.get("accept_time"), 0, UINT64_MAX),
            "finish_time": _safe_uint(value.get("finish_time"), 0, UINT64_MAX),
            "commit_time": _safe_uint(value.get("commit_time"), 0, UINT64_MAX),
        }

    @staticmethod
    def _task_message(task: Mapping[str, Any]) -> Dict[str, Any]:
        return {
            "TaskId": _safe_uint(task.get("task_id"), 0, UINT32_MAX),
            "TaskType": _safe_uint(task.get("task_type"), 0, UINT32_MAX),
            "TaskStatus": _safe_uint(task.get("task_status"), 0, 2),
            "Progress": _safe_uint(task.get("progress"), 0, UINT32_MAX),
            "AcceptTime": _safe_uint(task.get("accept_time"), 0, UINT64_MAX),
            "FinishTime": _safe_uint(task.get("finish_time"), 0, UINT64_MAX),
            "CommitTime": _safe_uint(task.get("commit_time"), 0, UINT64_MAX),
        }

    def _affection_level(self, spring_gospel_id: int, affection: int) -> int:
        rewards = self.catalog.affection_rewards_by_gospel[spring_gospel_id]
        eligible = [
            level
            for level, reward in rewards.items()
            if affection >= reward.affection_required
        ]
        return max(eligible, default=0)

    def _normalize_bubble(
        self,
        value: Any,
        scene_id: int,
        fallback_serial: int,
    ) -> Optional[Dict[str, Any]]:
        if not isinstance(value, Mapping):
            return None
        bubble_id = _safe_uint(value.get("bubble_id", value.get("id")), 0, UINT32_MAX)
        definition = self.catalog.bubble_rewards_by_id.get(bubble_id)
        if definition is None or definition.scene_id != scene_id:
            return None
        reward_string = value.get("reward_string", definition.reward_string)
        if reward_string != definition.reward_string:
            return None
        raw_position = value.get("position")
        raw_position = raw_position if isinstance(raw_position, Mapping) else value
        x = raw_position.get("x", raw_position.get("position_x"))
        y = raw_position.get("y", raw_position.get("position_y"))
        if isinstance(x, bool) or not isinstance(x, int):
            return None
        if isinstance(y, bool) or not isinstance(y, int):
            return None
        position = (x, y)
        if position not in self.catalog.locations_by_scene[scene_id]:
            return None
        instance_id = value.get("instance_id")
        if not isinstance(instance_id, str) or not instance_id:
            instance_id = f"{scene_id}:{max(1, fallback_serial)}"
        return {
            "bubble_id": bubble_id,
            "reward_string": reward_string,
            "position": {"x": x, "y": y},
            "instance_id": instance_id,
            "generated_time": _safe_uint(value.get("generated_time"), 0, UINT64_MAX),
        }

    def _normalized_scene_bubbles(
        self, value: Any, scene_id: int
    ) -> list[Dict[str, Any]]:
        if not isinstance(value, list):
            return []
        result = []
        occupied = set()
        for index, raw in enumerate(value, 1):
            bubble = self._normalize_bubble(raw, scene_id, index)
            if bubble is None:
                continue
            position = (
                int(bubble["position"]["x"]),
                int(bubble["position"]["y"]),
            )
            if position in occupied:
                continue
            occupied.add(position)
            result.append(bubble)
        return result

    @staticmethod
    def _bubble_message(bubble: Mapping[str, Any]) -> Dict[str, Any]:
        position = bubble.get("position")
        position = position if isinstance(position, Mapping) else {}
        return {
            "Id": _safe_uint(bubble.get("bubble_id"), 0, UINT32_MAX),
            "RewardString": (
                bubble.get("reward_string")
                if isinstance(bubble.get("reward_string"), str)
                else ""
            ),
            "Position": {
                "PositionX": int(position.get("x", 0)),
                "PositionY": int(position.get("y", 0)),
            },
        }

    def _seeded_copy(self, profile: Mapping[str, Any]) -> Dict[str, Any]:
        working = copy.deepcopy(dict(profile))
        self.seed_profile(working)
        return working

    def needs_seed(self, profile: Mapping[str, Any]) -> bool:
        """Return whether the persisted spring snapshot needs normalization.

        Revision-4 standalone saves could retain ``DailyTaskAccepted=true``
        without a ``TaskHolder``.  The runtime clears its dialogue text before
        command 1346 and reads that holder as soon as 1347 arrives, so merely
        repairing a temporary response copy is insufficient: the repaired
        state must be committed to the account store before entering Onsen.
        """

        state = profile.get("spring_v2")
        if not isinstance(state, Mapping):
            return True
        if _safe_uint(state.get("revision"), 0, UINT32_MAX) < SPRING_CONTENT_REVISION:
            return True
        if state.get("catalog_version") != self.content_catalog.version:
            return True

        scenes = state.get("scenes")
        if not isinstance(scenes, Mapping):
            return True
        if {
            _safe_uint(scene_id, 0, UINT32_MAX) for scene_id in scenes
        } != set(self.catalog.scene_ids):
            return True

        gospels = state.get("gospels")
        if not isinstance(gospels, Mapping):
            return True
        if {
            _safe_uint(gospel_id, 0, UINT32_MAX) for gospel_id in gospels
        } != set(self.catalog.spring_gospels):
            return True
        for spring_gospel_id in self.catalog.spring_gospels:
            gospel = _mapping_lookup(gospels, spring_gospel_id)
            if not isinstance(gospel, Mapping):
                return True
            accepted = bool(gospel.get("daily_task_accepted", False))
            task = self._normalize_task_state(
                gospel.get("task_holder"), spring_gospel_id
            )
            if accepted != (task is not None):
                return True
        return False

    def seed_profile(
        self, profile: MutableMapping[str, Any]
    ) -> MutableMapping[str, Any]:
        """Set the six ordinary and six spring-v2 maxima in place, idempotently.

        Runtime hero UIDs are deliberately required from the existing profile;
        synthesizing one here could make endpoints 342/703 target a different
        entity than the rest of the account snapshot.
        """

        if not isinstance(profile, MutableMapping):
            raise TypeError("profile must be a mutable mapping")
        heroes = profile.get("heroes")
        if not isinstance(heroes, list):
            raise SpringProfileError("profile.heroes must be a list")

        by_id: Dict[int, Tuple[int, MutableMapping[str, Any]]] = {}
        seen_uids = set()
        for index, raw in enumerate(heroes):
            if not isinstance(raw, Mapping):
                continue
            hero_id = _safe_uint(raw.get("id"), 0, UINT32_MAX)
            if hero_id not in self.catalog.ordinary_favor:
                continue
            if hero_id in by_id:
                raise SpringProfileError(f"profile has duplicate hero ID {hero_id}")
            uid = _safe_uint(raw.get("uid"), 0, UINT64_MAX)
            if uid <= 0 or uid in seen_uids:
                raise SpringProfileError(
                    f"hero {hero_id} has an invalid or duplicate HeroUid"
                )
            seen_uids.add(uid)
            row = raw if isinstance(raw, MutableMapping) else dict(raw)
            if row is not raw:
                heroes[index] = row
            by_id[hero_id] = (index, row)

        missing = set(self.catalog.hero_ids) - set(by_id)
        newcomer = bool(profile.get("player_progression"))
        if missing and not newcomer:
            raise SpringProfileError(
                f"profile is missing catalog heroes: {sorted(missing)}"
            )

        for hero_id in self.catalog.hero_ids:
            if hero_id not in by_id:
                continue
            definition = self.catalog.ordinary_favor[hero_id]
            row = by_id[hero_id][1]
            if newcomer:
                row.setdefault("fondness", 0)
                row.setdefault("fondness_level", 0)
                row.setdefault("fondness_fake_level", 0)
                row.setdefault("fondness_switch", 1)
                row.setdefault("rewarded_fondness_levels", [])
                continue
            row.update(
                {
                    "fondness": definition.fondness,
                    "fondness_level": definition.level,
                    "fondness_fake_level": definition.level,
                    "fondness_switch": 1,
                    "rewarded_fondness_levels": list(definition.reward_levels),
                }
            )

        previous = profile.get("spring_v2")
        previous = previous if isinstance(previous, Mapping) else {}
        previous_scenes = previous.get("scenes")
        previous_gospels = previous.get("gospels")

        scenes: Dict[str, Dict[str, Any]] = {}
        for scene_id in self.catalog.scene_ids:
            source = _mapping_lookup(previous_scenes, scene_id)
            source = dict(source) if isinstance(source, Mapping) else {}
            bubbles = self._normalized_scene_bubbles(source.get("bubbles"), scene_id)
            position_count = len(self.catalog.locations_by_scene[scene_id])
            source.update(
                {
                    "scene_id": scene_id,
                    "spring_gospel_ids": list(
                        self.catalog.spring_gospel_ids_by_scene[scene_id]
                    ),
                    "bubbles": bubbles,
                    "last_bubble_refresh_time": _safe_uint(
                        source.get("last_bubble_refresh_time"), 0, UINT64_MAX
                    ),
                    "position_cursor": _safe_uint(
                        source.get("position_cursor"), 0, UINT32_MAX
                    )
                    % position_count,
                    "generated_count": _safe_uint(
                        source.get("generated_count"), len(bubbles), UINT64_MAX
                    ),
                }
            )
            scenes[str(scene_id)] = source

        gospels: Dict[str, Dict[str, Any]] = {}
        for spring_gospel_id, definition in self.catalog.spring_gospels.items():
            source = _mapping_lookup(previous_gospels, spring_gospel_id)
            source = dict(source) if isinstance(source, Mapping) else {}
            nickname = source.get("nickname", "")
            affection = min(
                _safe_uint(source.get("affection"), 0 if newcomer else definition.affection, UINT32_MAX),
                definition.affection,
            )
            raw_reward_levels = source.get("rewarded_affection_levels", [])
            if not isinstance(raw_reward_levels, (list, tuple, set, frozenset)):
                raw_reward_levels = []
            rewarded_levels = {
                _safe_uint(level, 0, UINT32_MAX) for level in raw_reward_levels
            }
            take_times: Dict[str, int] = {}
            raw_take_times = source.get("affection_reward_take_times")
            if isinstance(raw_take_times, Mapping):
                for raw_level, raw_time in raw_take_times.items():
                    level = _safe_uint(raw_level, 0, UINT32_MAX)
                    if level not in definition.reward_levels:
                        continue
                    rewarded_levels.add(level)
                    take_times[str(level)] = _safe_uint(raw_time, 0, UINT64_MAX)
            rewarded_affection_levels = [
                level for level in definition.reward_levels if level in rewarded_levels
            ]
            for level in rewarded_affection_levels:
                take_times.setdefault(str(level), 0)
            daily_task_accepted = bool(source.get("daily_task_accepted", False))
            task_holder = self._normalize_task_state(
                source.get("task_holder"), spring_gospel_id
            )
            # Revision-4 saves could contain DailyTaskAccepted=true without
            # the corresponding task.  Repair that exact contradictory state
            # so reopening a frozen dialogue remains recoverable.
            if daily_task_accepted and task_holder is None:
                tasks = self.catalog.tasks_by_spring_gospel[spring_gospel_id]
                repair_cursor = _safe_uint(
                    source.get("task_cursor"), 0, UINT32_MAX
                ) % len(tasks)
                task_holder = self._task_state(
                    tasks[repair_cursor]
                )
            if task_holder is not None:
                daily_task_accepted = True
            daily_task_chain = []
            raw_chain = source.get("daily_task_chain")
            if isinstance(raw_chain, list):
                for raw_task in raw_chain:
                    normalized = self._normalize_task_state(raw_task, spring_gospel_id)
                    if normalized is not None:
                        daily_task_chain.append(normalized)
            completed_task_ids = []
            raw_completed = source.get("completed_task_ids")
            if isinstance(raw_completed, list):
                allowed_task_ids = {
                    task.task_id
                    for task in self.catalog.tasks_by_spring_gospel[spring_gospel_id]
                }
                for raw_task_id in raw_completed:
                    task_id = _safe_uint(raw_task_id, 0, UINT32_MAX)
                    if (
                        task_id in allowed_task_ids
                        and task_id not in completed_task_ids
                    ):
                        completed_task_ids.append(task_id)
            task_count = len(self.catalog.tasks_by_spring_gospel[spring_gospel_id])
            source.update(
                {
                    "spring_gospel_id": spring_gospel_id,
                    "gospel_id": definition.gospel_id,
                    "scene_id": definition.scene_id,
                    "is_unlocked": bool(source.get("is_unlocked", True)),
                    "affection": affection,
                    "affection_level": self._affection_level(
                        spring_gospel_id, affection
                    ),
                    "rewarded_affection_levels": rewarded_affection_levels,
                    "affection_reward_take_times": take_times,
                    "daily_task_accepted": daily_task_accepted,
                    "task_holder": task_holder,
                    "daily_task_chain": daily_task_chain,
                    "completed_task_ids": completed_task_ids,
                    "task_cursor": _safe_uint(
                        source.get("task_cursor"),
                        len(completed_task_ids) % task_count,
                        UINT32_MAX,
                    )
                    % task_count,
                    "daily_affection_triggered": bool(
                        source.get("daily_affection_triggered", False)
                    ),
                    "nickname": nickname if isinstance(nickname, str) else "",
                }
            )
            gospels[str(spring_gospel_id)] = source

        spring_v2 = dict(previous)
        task_history = previous.get("task_history")
        task_history = (
            copy.deepcopy(task_history) if isinstance(task_history, list) else []
        )
        spring_v2.update(
            {
                "revision": SPRING_CONTENT_REVISION,
                "catalog_version": self.content_catalog.version,
                "bubbles_limit": self.catalog.bubbles_limit,
                "bubbles_taken": _safe_uint(
                    previous.get("bubbles_taken"), 0, self.catalog.bubbles_limit
                ),
                "scene_ids": list(self.catalog.scene_ids),
                "scenes": scenes,
                "gospels": gospels,
                "task_history": task_history,
            }
        )
        profile["spring_v2"] = spring_v2
        return profile

    def _hero_for_uid(
        self, profile: Mapping[str, Any], hero_uid: int
    ) -> Optional[Mapping[str, Any]]:
        heroes = profile.get("heroes")
        if not isinstance(heroes, list):
            return None
        found: Optional[Mapping[str, Any]] = None
        for row in heroes:
            if not isinstance(row, Mapping):
                continue
            if _safe_uint(row.get("uid"), 0, UINT64_MAX) != hero_uid:
                continue
            if found is not None:
                return None
            found = row
        return found

    def _gospel_state(
        self, profile: Mapping[str, Any], spring_gospel_id: int
    ) -> Mapping[str, Any]:
        spring_v2 = profile.get("spring_v2")
        if not isinstance(spring_v2, Mapping):
            return {}
        state = _mapping_lookup(spring_v2.get("gospels"), spring_gospel_id)
        return state if isinstance(state, Mapping) else {}

    def _scene_state(
        self, profile: Mapping[str, Any], scene_id: int
    ) -> Mapping[str, Any]:
        spring_v2 = profile.get("spring_v2")
        if not isinstance(spring_v2, Mapping):
            return {}
        state = _mapping_lookup(spring_v2.get("scenes"), scene_id)
        return state if isinstance(state, Mapping) else {}

    def _ordinary_reward_levels(
        self, hero: Mapping[str, Any], hero_id: int
    ) -> Tuple[int, ...]:
        definition = self.catalog.ordinary_favor.get(hero_id)
        if definition is None:
            # Hidden account heroes such as Lillia have no a_favor row in the
            # pinned runtime.  They still receive a successful identity-scoped
            # snapshot so login initialization does not mistake the absent
            # optional subsystem for an invalid HeroUid.
            return ()
        allowed = definition.reward_levels
        values = hero.get("rewarded_fondness_levels", hero.get("rewarded_fondness"))
        if not isinstance(values, (list, tuple, set, frozenset)):
            return allowed
        selected = {_safe_uint(value, 0, UINT32_MAX) for value in values}
        return tuple(level for level in allowed if level in selected)

    def _spring_reward_levels(
        self, state: Mapping[str, Any], definition: SpringGospelDefinition
    ) -> Tuple[int, ...]:
        values = state.get("rewarded_affection_levels")
        if not isinstance(values, (list, tuple, set, frozenset)):
            return ()
        selected = {_safe_uint(value, 0, UINT32_MAX) for value in values}
        return tuple(level for level in definition.reward_levels if level in selected)

    @staticmethod
    def _aggregate_rewards(
        rewards: Tuple[SpringReward, ...],
    ) -> Tuple[SpringReward, ...]:
        order = []
        totals: Dict[Tuple[int, int], int] = {}
        for reward in rewards:
            key = (reward.res_type, reward.item_id)
            if key not in totals:
                order.append(key)
                totals[key] = 0
            total = totals[key] + reward.count
            if total > UINT32_MAX:
                raise SpringApplicationError(
                    ERROR_CONFIG, "combined spring reward exceeds uint32"
                )
            totals[key] = total
        return tuple(
            SpringReward(res_type, item_id, totals[(res_type, item_id)])
            for res_type, item_id in order
        )

    def _reward_changes(
        self,
        profile: Mapping[str, Any],
        rewards: Tuple[SpringReward, ...],
        *,
        request_id: int,
        operation: str,
        claim_key: str,
        source_id: int,
        timestamp: int,
    ) -> Dict[str, Any]:
        aggregated = self._aggregate_rewards(rewards)
        changes: Dict[str, Any] = {}
        applied = []
        saturated = []
        try:
            inventory = normalize_inventory(copy.deepcopy(profile.get("inventory", {})))
        except InventoryContentError as exc:
            raise SpringApplicationError(ERROR_CONFIG, str(exc)) from exc
        inventory_changed = False

        for reward in aggregated:
            if reward.res_type in RESOURCE_TOP_LEVEL_FIELDS:
                field_name = RESOURCE_TOP_LEVEL_FIELDS[reward.res_type]
                current = _safe_uint(profile.get(field_name), 0, UINT64_MAX)
                current = _safe_uint(changes.get(field_name), current, UINT64_MAX)
                if current + reward.count > UINT64_MAX:
                    raise SpringApplicationError(
                        ERROR_CONFIG,
                        f"spring reward would overflow profile.{field_name}",
                    )
                changes[field_name] = current + reward.count
                applied.append(reward.as_state())
                continue

            key = str(reward.item_id)
            previous = inventory.get(key)
            current = _safe_uint(
                previous.get("count") if isinstance(previous, Mapping) else 0,
                0,
                UINT32_MAX,
            )
            maximum = self.catalog.item_max_stacks.get(reward.item_id)
            if maximum is None or current > maximum:
                raise SpringApplicationError(
                    ERROR_EXCEED_ITEM_LIMIT_COUNT,
                    f"item {reward.item_id} has no safe spring reward capacity",
                )
            if current + reward.count > maximum:
                if profile.get("all_content_unlocked") and current == maximum:
                    saturated.append(reward.as_state())
                    continue
                raise SpringApplicationError(
                    ERROR_EXCEED_ITEM_LIMIT_COUNT,
                    f"item {reward.item_id} would exceed stack cap {maximum}",
                )
            inventory[key] = {
                "uid": (
                    int(previous.get("uid"))
                    if isinstance(previous, Mapping) and previous.get("uid")
                    else stable_item_uid(reward.item_id)
                ),
                "id": reward.item_id,
                "count": current + reward.count,
                "para": (
                    int(previous.get("para", 0)) if isinstance(previous, Mapping) else 0
                ),
            }
            inventory_changed = True
            applied.append(reward.as_state())

        resource_history_value = profile.get("resource_history", [])
        reward_history_value = profile.get("reward_history", [])
        if not isinstance(resource_history_value, list) or not isinstance(
            reward_history_value, list
        ):
            raise SpringApplicationError(
                ERROR_CONFIG, "profile spring reward ledgers are malformed"
            )
        static_rewards = [reward.as_state() for reward in aggregated]
        ledger_entry = {
            "claim_key": claim_key,
            "source": "spring_v2",
            "source_id": source_id,
            "command_id": request_id,
            "operation": operation,
            "timestamp": timestamp,
            "rewards": static_rewards,
            "applied_rewards": applied,
            "saturated_rewards": saturated,
        }
        resource_history = copy.deepcopy(resource_history_value)
        resource_history.append(copy.deepcopy(ledger_entry))
        reward_history = copy.deepcopy(reward_history_value)
        reward_history.append(copy.deepcopy(ledger_entry))
        changes["resource_history"] = resource_history
        changes["reward_history"] = reward_history
        if inventory_changed:
            changes["inventory"] = inventory
        return changes

    @staticmethod
    def _reward_wire_values(
        rewards: Tuple[SpringReward, ...],
    ) -> list[Dict[str, int]]:
        return [reward.as_wire() for reward in rewards]

    def make_legacy_spring_datas_ret(self, profile: Mapping[str, Any]) -> bytes:
        role_id = _safe_uint(profile.get("role_id"), 0, UINT64_MAX)
        return self.codec.encode(
            "SC_SpringDatasRet",
            {"RoleId": role_id, "RestList": _legacy_rest_list(profile)},
        )

    def make_dating_datas_ret(self, profile: Mapping[str, Any], hero_uid: int) -> bytes:
        del profile  # Hero identity was validated before this method is called.
        return self.codec.encode(
            "SC_DatingDatasRet",
            {
                "HeroUId": hero_uid,
                "PreferenceList": [],
                "DateStoryList": [],
            },
        )

    def make_hero_rewarded_fondness_ret(
        self, hero_uid: int, hero: Mapping[str, Any]
    ) -> bytes:
        hero_id = _safe_uint(hero.get("id"), 0, UINT32_MAX)
        return self.codec.encode(
            "SC_HeroRewardedFondnessRet",
            {
                "HeroUId": hero_uid,
                "RewardedFondnessList": self._ordinary_reward_levels(hero, hero_id),
            },
        )

    def make_request_data_ret(
        self, profile: Mapping[str, Any], error_code: int = ERROR_SUCCESS
    ) -> bytes:
        values: Dict[str, Any] = {"ErrorCode": error_code}
        if error_code == ERROR_SUCCESS:
            state = profile.get("spring_v2")
            state = state if isinstance(state, Mapping) else {}
            values.update(
                {
                    "BubblesLimit": self.catalog.bubbles_limit,
                    "BubblesTaken": _safe_uint(
                        state.get("bubbles_taken"), 0, self.catalog.bubbles_limit
                    ),
                }
            )
        return self.codec.encode("SC_SpringV2RequestData_Ret", values)

    def _spring_gospel_message(
        self, profile: Mapping[str, Any], definition: SpringGospelDefinition
    ) -> Dict[str, Any]:
        state = self._gospel_state(profile, definition.spring_gospel_id)
        take_times = state.get("affection_reward_take_times")
        take_times = take_times if isinstance(take_times, Mapping) else {}
        reward_map = {
            level: {
                "TakeTime": _safe_uint(
                    _mapping_lookup(take_times, level), 0, UINT64_MAX
                )
            }
            for level in self._spring_reward_levels(state, definition)
        }
        nickname = state.get("nickname", "")
        values: Dict[str, Any] = {
            "IsUnlock": bool(state.get("is_unlocked", True)),
            "SceneId": definition.scene_id,
            "GospelId": definition.gospel_id,
            "SpringGospelId": definition.spring_gospel_id,
            "Affection": _safe_uint(
                state.get("affection"), definition.affection, UINT32_MAX
            ),
            "AffectionLevelRewardTakeMap": reward_map,
            "DailyTaskAccepted": bool(state.get("daily_task_accepted", False)),
            "DailyAffectionTriggered": bool(
                state.get("daily_affection_triggered", False)
            ),
            "SpringGospelNickname": nickname if isinstance(nickname, str) else "",
        }
        task_holder = self._normalize_task_state(
            state.get("task_holder"), definition.spring_gospel_id
        )
        if task_holder is not None:
            values["TaskHolder"] = self._task_message(task_holder)
        chain = []
        raw_chain = state.get("daily_task_chain")
        if isinstance(raw_chain, list):
            for raw_task in raw_chain:
                normalized = self._normalize_task_state(
                    raw_task, definition.spring_gospel_id
                )
                if normalized is not None:
                    chain.append(self._task_message(normalized))
        if chain:
            values["DailyTaskChain"] = chain
        return values

    def make_scene_data_ret(
        self,
        profile: Mapping[str, Any],
        scene_id: int,
        error_code: int = ERROR_SUCCESS,
    ) -> bytes:
        values: Dict[str, Any] = {"ErrorCode": error_code}
        if error_code == ERROR_SUCCESS:
            scene_state = self._scene_state(profile, scene_id)
            gospel_map = {
                spring_gospel_id: self._spring_gospel_message(
                    profile, self.catalog.spring_gospels[spring_gospel_id]
                )
                for spring_gospel_id in self.catalog.spring_gospel_ids_by_scene[
                    scene_id
                ]
            }
            values["Scene"] = {
                "SceneId": scene_id,
                "Bubbles": [
                    self._bubble_message(bubble)
                    for bubble in self._normalized_scene_bubbles(
                        scene_state.get("bubbles"), scene_id
                    )
                ],
                "SpringGospelIdMap": gospel_map,
                "LastBubbleRefreshTime": _safe_uint(
                    scene_state.get("last_bubble_refresh_time"), 0, UINT64_MAX
                ),
            }
        return self.codec.encode("SC_SpringV2RequestSceneData_Ret", values)

    def make_unlocked_spring_gospel_ret(self) -> bytes:
        rows = [
            {
                "GospelId": gospel_id,
                "SpringGospelId": spring_gospel_ids,
            }
            for gospel_id, spring_gospel_ids in self.catalog.spring_gospel_ids_by_gospel.items()
        ]
        return self.codec.encode(
            "SC_SpringV2RequestUnlockedSpringGospel_Ret",
            {"UnlockedSpringGospel": rows},
        )

    def make_affection_level_ret(
        self, profile: Mapping[str, Any], error_code: int = ERROR_SUCCESS
    ) -> bytes:
        values: Dict[str, Any] = {"ErrorCode": error_code}
        if error_code == ERROR_SUCCESS:
            levels = {}
            for spring_gospel_id, definition in self.catalog.spring_gospels.items():
                state = self._gospel_state(profile, spring_gospel_id)
                levels[spring_gospel_id] = _safe_uint(
                    state.get("affection_level"),
                    definition.affection_level,
                    UINT32_MAX,
                )
            values["SpringGospelAffectionLevelMap"] = levels
        return self.codec.encode(
            "SC_SpringV2RequestSpringGospelAffectionLevel_Ret", values
        )

    def make_accept_task_ret(self, error_code: int = ERROR_SUCCESS) -> bytes:
        return self.codec.encode("SC_SpringV2AcceptTask_Ret", {"ErrorCode": error_code})

    def make_generate_bubble_ret(
        self,
        error_code: int = ERROR_SUCCESS,
        bubble: Optional[Mapping[str, Any]] = None,
    ) -> bytes:
        values: Dict[str, Any] = {"ErrorCode": error_code}
        if error_code == ERROR_SUCCESS and bubble is not None:
            values["Bubble"] = self._bubble_message(bubble)
        return self.codec.encode("SC_SpringV2GenerateBubble_Ret", values)

    def make_take_bubble_reward_ret(
        self,
        error_code: int = ERROR_SUCCESS,
        *,
        rewards: Tuple[SpringReward, ...] = (),
        position: Tuple[int, int] = (0, 0),
    ) -> bytes:
        # The runtime dereferences Position without a null guard, including on
        # replay/error paths.  Always emit the nested message.
        return self.codec.encode(
            "SC_SpringV2TakeBubbleReward_Ret",
            {
                "ErrorCode": error_code,
                "RewardList": self._reward_wire_values(rewards),
                "Position": {
                    "PositionX": position[0],
                    "PositionY": position[1],
                },
            },
        )

    def _affection_take_map(
        self, gospel: Mapping[str, Any], definition: SpringGospelDefinition
    ) -> Dict[int, Dict[str, int]]:
        times = gospel.get("affection_reward_take_times")
        times = times if isinstance(times, Mapping) else {}
        return {
            level: {
                "TakeTime": _safe_uint(_mapping_lookup(times, level), 0, UINT64_MAX)
            }
            for level in self._spring_reward_levels(gospel, definition)
        }

    def make_take_affection_reward_ret(
        self,
        error_code: int = ERROR_SUCCESS,
        *,
        spring_gospel_id: int = 0,
        rewards: Tuple[SpringReward, ...] = (),
        take_map: Optional[Mapping[int, Mapping[str, int]]] = None,
    ) -> bytes:
        return self.codec.encode(
            "SC_SpringV2TakeAffectionLevelReward_Ret",
            {
                "ErrorCode": error_code,
                "SpringGospelId": spring_gospel_id,
                "RewardList": self._reward_wire_values(rewards),
                "AffectionLevelRewardTakeMap": dict(take_map or {}),
            },
        )

    def make_commit_task_ret(
        self,
        error_code: int = ERROR_SUCCESS,
        rewards: Tuple[SpringReward, ...] = (),
    ) -> bytes:
        return self.codec.encode(
            "SC_SpringV2CommitTask_Ret",
            {
                "ErrorCode": error_code,
                "RewardList": self._reward_wire_values(rewards),
            },
        )

    def make_interact_ret(self, error_code: int = ERROR_SUCCESS) -> bytes:
        return self.codec.encode(
            "SC_SpringV2InteractWithGospel_Ret", {"ErrorCode": error_code}
        )

    def make_change_nickname_ret(self, error_code: int = ERROR_SUCCESS) -> bytes:
        return self.codec.encode(
            "SC_SpringV2ChangeSpringGospelNickname_Ret",
            {"ErrorCode": error_code},
        )

    def make_touch_spring_model_ret(
        self,
        error_code: int,
        char_info: Mapping[str, int],
        interact_type: int,
        satisfaction: int,
        safety: int,
    ) -> bytes:
        return self.codec.encode(
            "SC_TouchSpringModelRet",
            {
                "Errorcode": error_code,
                "CharInfo": dict(char_info),
                "CharInteractType": interact_type,
                "Satisfaction": satisfaction,
                "Safety": safety,
            },
        )

    def make_generate_spring_prelude_ret(
        self,
        error_code: int,
        plot_ids: Tuple[int, ...],
        char_info: Mapping[str, int],
    ) -> bytes:
        return self.codec.encode(
            "SC_GenerateSpringPreludePlotRet",
            {
                "Errorcode": error_code,
                "PlotPkgList": plot_ids,
                "CharInfo": dict(char_info),
            },
        )

    def make_choose_spring_prelude_ret(self, error_code: int, plot_id: int) -> bytes:
        return self.codec.encode(
            "SC_ChooseSpringPreludePlotRet",
            {"Errorcode": error_code, "PlotPkgId": plot_id},
        )

    def make_spring_prelude_end_ret(
        self,
        error_code: int,
        plot_id: int,
        spring_status: int,
    ) -> bytes:
        return self.codec.encode(
            "SC_SpringPreludePlotEndRet",
            {
                "Errorcode": error_code,
                "PlotPkgId": plot_id,
                "SpringStatus": spring_status,
                "RewardList": [],
            },
        )

    def make_wipe_steam_ret(self, error_code: int) -> bytes:
        return self.codec.encode("SC_WipeSteamInSpringRet", {"Errorcode": error_code})

    def make_gospel_update_notification(
        self,
        definition: SpringGospelDefinition,
        state: Mapping[str, Any],
        *,
        task_override: Optional[Mapping[str, Any]] = None,
    ) -> bytes:
        task_holder = self._normalize_task_state(
            task_override if task_override is not None else state.get("task_holder"),
            definition.spring_gospel_id,
        )
        values: Dict[str, Any] = {
            "SceneId": definition.scene_id,
            "SpringGospelId": definition.spring_gospel_id,
            "Affection": _safe_uint(
                state.get("affection"), definition.affection, UINT32_MAX
            ),
            "DailyAffectionTriggered": bool(
                state.get("daily_affection_triggered", False)
            ),
            "DailyTaskAccepted": bool(state.get("daily_task_accepted", False)),
            "SpringGospelNickname": (
                state.get("nickname") if isinstance(state.get("nickname"), str) else ""
            ),
        }
        if task_holder is not None:
            values["Task"] = self._task_message(task_holder)
        return self.codec.encode("Notify_SpringV2GospelUpdate", values)

    def _accept_task(
        self, profile: Mapping[str, Any], request_body: bytes
    ) -> SpringResult:
        spring_gospel_id = _decode_uint_field(request_body, 1, maximum=UINT32_MAX)
        definition = self.catalog.spring_gospels.get(spring_gospel_id)
        if definition is None:
            return SpringResult(
                SC_SPRING_V2_ACCEPT_TASK_RET,
                self.make_accept_task_ret(ERROR_SPRING_V2_GOSPEL_NOT_FOUND),
                error_code=ERROR_SPRING_V2_GOSPEL_NOT_FOUND,
                reason=f"unknown spring-v2 gospel ID {spring_gospel_id}",
            )

        # Build the patch from a fully seeded copy.  This preserves every
        # sibling scene/gospel field while satisfying the server's top-level
        # shallow-merge contract, and never mutates the handler input.
        working = self._seeded_copy(profile)
        spring_v2 = copy.deepcopy(dict(working["spring_v2"]))
        gospels = copy.deepcopy(dict(spring_v2["gospels"]))
        gospel = copy.deepcopy(dict(gospels[str(spring_gospel_id)]))
        if not bool(gospel.get("is_unlocked", True)):
            return SpringResult(
                SC_SPRING_V2_ACCEPT_TASK_RET,
                self.make_accept_task_ret(ERROR_SPRING_V2_GOSPEL_NOT_UNLOCKED),
                error_code=ERROR_SPRING_V2_GOSPEL_NOT_UNLOCKED,
                reason=f"spring-v2 gospel {spring_gospel_id} is locked",
            )
        if bool(gospel.get("daily_task_accepted", False)):
            # A retried 1346 still drives OnsenShowDialogue when its 1347
            # callback arrives.  Re-send the authoritative holder first so a
            # stale/recreated runtime cache cannot reopen the empty text layer.
            notification = self.make_gospel_update_notification(
                definition, gospel
            )
            return SpringResult(
                SC_SPRING_V2_ACCEPT_TASK_RET,
                self.make_accept_task_ret(ERROR_SPRING_V2_DAILY_TASK_ALREADY_ACCEPTED),
                error_code=ERROR_SPRING_V2_DAILY_TASK_ALREADY_ACCEPTED,
                reason=(
                    f"spring-v2 gospel {spring_gospel_id} already accepted "
                    "its daily task"
                ),
                notifications=((NOTIFY_SPRING_V2_GOSPEL_UPDATE, notification),),
            )
        tasks = self.catalog.tasks_by_spring_gospel[spring_gospel_id]
        cursor = _safe_uint(gospel.get("task_cursor"), 0, UINT32_MAX) % len(tasks)
        task_definition = tasks[cursor]
        gospel["daily_task_accepted"] = True
        gospel["task_holder"] = self._task_state(
            task_definition,
            accept_time=int(time.time()),
        )
        gospels[str(spring_gospel_id)] = gospel
        spring_v2["gospels"] = gospels
        notification = self.make_gospel_update_notification(definition, gospel)
        return SpringResult(
            SC_SPRING_V2_ACCEPT_TASK_RET,
            self.make_accept_task_ret(),
            changes={"spring_v2": spring_v2},
            notifications=((NOTIFY_SPRING_V2_GOSPEL_UPDATE, notification),),
        )

    def _generate_bubble(
        self, profile: Mapping[str, Any], request_body: bytes
    ) -> SpringResult:
        scene_id = _decode_uint_field(request_body, 1, maximum=UINT32_MAX)
        if scene_id not in self.catalog.spring_gospel_ids_by_scene:
            return SpringResult(
                SC_SPRING_V2_GENERATE_BUBBLE_RET,
                self.make_generate_bubble_ret(ERROR_SPRING_V2_SCENE_NOT_EXIST),
                error_code=ERROR_SPRING_V2_SCENE_NOT_EXIST,
                reason=f"unknown spring-v2 scene ID {scene_id}",
            )
        working = self._seeded_copy(profile)
        spring_v2 = copy.deepcopy(dict(working["spring_v2"]))
        if (
            _safe_uint(spring_v2.get("bubbles_taken"), 0, self.catalog.bubbles_limit)
            >= self.catalog.bubbles_limit
        ):
            return SpringResult(
                SC_SPRING_V2_GENERATE_BUBBLE_RET,
                self.make_generate_bubble_ret(
                    ERROR_SPRING_V2_BUBBLE_REFRESH_COUNT_NOT_ENOUGH
                ),
                error_code=ERROR_SPRING_V2_BUBBLE_REFRESH_COUNT_NOT_ENOUGH,
                reason="the account has exhausted its spring-v2 bubble take limit",
            )
        scenes = copy.deepcopy(dict(spring_v2["scenes"]))
        scene = copy.deepcopy(dict(scenes[str(scene_id)]))
        bubbles = self._normalized_scene_bubbles(scene.get("bubbles"), scene_id)
        occupied = {
            (int(bubble["position"]["x"]), int(bubble["position"]["y"]))
            for bubble in bubbles
        }
        positions = self.catalog.locations_by_scene[scene_id]
        cursor = _safe_uint(scene.get("position_cursor"), 0, UINT32_MAX) % len(
            positions
        )
        selected_index = next(
            (
                (cursor + offset) % len(positions)
                for offset in range(len(positions))
                if positions[(cursor + offset) % len(positions)] not in occupied
            ),
            None,
        )
        if selected_index is None:
            return SpringResult(
                SC_SPRING_V2_GENERATE_BUBBLE_RET,
                self.make_generate_bubble_ret(
                    ERROR_SPRING_V2_NO_POSITION_TO_GENERATE_BUBBLE
                ),
                error_code=ERROR_SPRING_V2_NO_POSITION_TO_GENERATE_BUBBLE,
                reason=f"scene {scene_id} has no unoccupied configured position",
            )
        generated_count = _safe_uint(
            scene.get("generated_count"), len(bubbles), UINT64_MAX
        )
        definitions = self.catalog.bubble_rewards_by_scene[scene_id]
        reward = definitions[generated_count % len(definitions)]
        now = int(time.time())
        x, y = positions[selected_index]
        bubble = {
            "bubble_id": reward.bubble_id,
            "reward_string": reward.reward_string,
            "position": {"x": x, "y": y},
            "instance_id": f"{scene_id}:{generated_count + 1}",
            "generated_time": now,
        }
        bubbles.append(bubble)
        scene.update(
            {
                "bubbles": bubbles,
                "last_bubble_refresh_time": now,
                "position_cursor": (selected_index + 1) % len(positions),
                "generated_count": generated_count + 1,
            }
        )
        scenes[str(scene_id)] = scene
        spring_v2["scenes"] = scenes
        return SpringResult(
            SC_SPRING_V2_GENERATE_BUBBLE_RET,
            self.make_generate_bubble_ret(bubble=bubble),
            changes={"spring_v2": spring_v2},
        )

    def _take_bubble_reward(
        self, profile: Mapping[str, Any], request_body: bytes
    ) -> SpringResult:
        scene_id = _decode_uint_field(request_body, 1, maximum=UINT32_MAX)
        position = (
            _decode_int32_field(request_body, 2),
            _decode_int32_field(request_body, 3),
        )
        if scene_id not in self.catalog.spring_gospel_ids_by_scene:
            return SpringResult(
                SC_SPRING_V2_TAKE_BUBBLE_REWARD_RET,
                self.make_take_bubble_reward_ret(
                    ERROR_SPRING_V2_SCENE_NOT_EXIST, position=position
                ),
                error_code=ERROR_SPRING_V2_SCENE_NOT_EXIST,
                reason=f"unknown spring-v2 scene ID {scene_id}",
            )
        working = self._seeded_copy(profile)
        spring_v2 = copy.deepcopy(dict(working["spring_v2"]))
        taken = _safe_uint(
            spring_v2.get("bubbles_taken"), 0, self.catalog.bubbles_limit
        )
        if taken >= self.catalog.bubbles_limit:
            return SpringResult(
                SC_SPRING_V2_TAKE_BUBBLE_REWARD_RET,
                self.make_take_bubble_reward_ret(
                    ERROR_SPRING_V2_BUBBLE_TAKE_LIMIT_REACHED,
                    position=position,
                ),
                error_code=ERROR_SPRING_V2_BUBBLE_TAKE_LIMIT_REACHED,
                reason="the account has exhausted its spring-v2 bubble take limit",
            )
        scenes = copy.deepcopy(dict(spring_v2["scenes"]))
        scene = copy.deepcopy(dict(scenes[str(scene_id)]))
        bubbles = self._normalized_scene_bubbles(scene.get("bubbles"), scene_id)
        bubble_index = next(
            (
                index
                for index, bubble in enumerate(bubbles)
                if (
                    int(bubble["position"]["x"]),
                    int(bubble["position"]["y"]),
                )
                == position
            ),
            None,
        )
        if bubble_index is None:
            error_code = (
                ERROR_SPRING_V2_NO_BUBBLE_IN_SCENE
                if not bubbles
                else ERROR_SPRING_V2_NO_BUBBLE_POSITION_AVAILABLE
            )
            return SpringResult(
                SC_SPRING_V2_TAKE_BUBBLE_REWARD_RET,
                self.make_take_bubble_reward_ret(error_code, position=position),
                error_code=error_code,
                reason=f"scene {scene_id} has no bubble at {position}",
            )
        bubble = bubbles[bubble_index]
        definition = self.catalog.bubble_rewards_by_id.get(int(bubble["bubble_id"]))
        if definition is None or definition.reward_string != bubble["reward_string"]:
            return SpringResult(
                SC_SPRING_V2_TAKE_BUBBLE_REWARD_RET,
                self.make_take_bubble_reward_ret(
                    ERROR_SPRING_V2_NO_BUBBLE_REWARD_AVAILABLE,
                    position=position,
                ),
                error_code=ERROR_SPRING_V2_NO_BUBBLE_REWARD_AVAILABLE,
                reason="the persisted bubble does not match a static reward",
            )
        now = int(time.time())
        bubbles.pop(bubble_index)
        scene["bubbles"] = bubbles
        scenes[str(scene_id)] = scene
        spring_v2["scenes"] = scenes
        spring_v2["bubbles_taken"] = taken + 1
        try:
            changes = self._reward_changes(
                working,
                definition.rewards,
                request_id=CS_SPRING_V2_TAKE_BUBBLE_REWARD,
                operation="take_bubble_reward",
                claim_key=f"spring_v2:bubble:{bubble['instance_id']}",
                source_id=definition.bubble_id,
                timestamp=now,
            )
        except SpringApplicationError as exc:
            return SpringResult(
                SC_SPRING_V2_TAKE_BUBBLE_REWARD_RET,
                self.make_take_bubble_reward_ret(exc.error_code, position=position),
                error_code=exc.error_code,
                reason=exc.reason,
            )
        changes["spring_v2"] = spring_v2
        return SpringResult(
            SC_SPRING_V2_TAKE_BUBBLE_REWARD_RET,
            self.make_take_bubble_reward_ret(
                rewards=definition.rewards, position=position
            ),
            changes=changes,
        )

    def _take_affection_reward(
        self, profile: Mapping[str, Any], request_body: bytes
    ) -> SpringResult:
        scene_id = _decode_uint_field(request_body, 1, maximum=UINT32_MAX)
        spring_gospel_id = _decode_uint_field(request_body, 2, maximum=UINT32_MAX)
        level = _decode_uint_field(request_body, 3, maximum=UINT32_MAX)
        definition = self.catalog.spring_gospels.get(spring_gospel_id)

        def failure(error_code: int, reason: str) -> SpringResult:
            return SpringResult(
                SC_SPRING_V2_TAKE_AFFECTION_LEVEL_REWARD_RET,
                self.make_take_affection_reward_ret(
                    error_code, spring_gospel_id=spring_gospel_id
                ),
                error_code=error_code,
                reason=reason,
            )

        if scene_id not in self.catalog.spring_gospel_ids_by_scene:
            return failure(
                ERROR_SPRING_V2_SCENE_NOT_EXIST,
                f"unknown spring-v2 scene ID {scene_id}",
            )
        if definition is None:
            return failure(
                ERROR_SPRING_V2_GOSPEL_NOT_FOUND,
                f"unknown spring-v2 gospel ID {spring_gospel_id}",
            )
        if definition.scene_id != scene_id:
            return failure(
                ERROR_SPRING_V2_GOSPEL_DOES_NOT_MATCH_SCENE,
                f"spring-v2 gospel {spring_gospel_id} is not in scene {scene_id}",
            )
        reward_definition = self.catalog.affection_rewards_by_gospel[
            spring_gospel_id
        ].get(level)
        if reward_definition is None:
            return failure(
                ERROR_SPRING_V2_AFFECTION_LEVEL_NOT_ENOUGH,
                f"spring-v2 affection level {level} is not configured",
            )
        working = self._seeded_copy(profile)
        spring_v2 = copy.deepcopy(dict(working["spring_v2"]))
        gospels = copy.deepcopy(dict(spring_v2["gospels"]))
        gospel = copy.deepcopy(dict(gospels[str(spring_gospel_id)]))
        if not bool(gospel.get("is_unlocked", True)):
            return failure(
                ERROR_SPRING_V2_GOSPEL_NOT_UNLOCKED,
                f"spring-v2 gospel {spring_gospel_id} is locked",
            )
        affection = _safe_uint(gospel.get("affection"), 0, UINT32_MAX)
        affection_level = _safe_uint(gospel.get("affection_level"), 0, UINT32_MAX)
        if affection < reward_definition.affection_required or affection_level < level:
            return failure(
                ERROR_SPRING_V2_AFFECTION_LEVEL_NOT_ENOUGH,
                f"spring-v2 gospel {spring_gospel_id} has not reached level {level}",
            )
        rewarded_levels = list(self._spring_reward_levels(gospel, definition))
        if level in rewarded_levels:
            return failure(
                ERROR_SPRING_V2_AFFECTION_REWARD_ALREADY_TAKEN,
                f"spring-v2 gospel {spring_gospel_id} level {level} was claimed",
            )
        now = int(time.time())
        rewarded_levels.append(level)
        gospel["rewarded_affection_levels"] = sorted(rewarded_levels)
        raw_take_times = gospel.get("affection_reward_take_times")
        take_times = (
            copy.deepcopy(dict(raw_take_times))
            if isinstance(raw_take_times, Mapping)
            else {}
        )
        take_times[str(level)] = now
        gospel["affection_reward_take_times"] = take_times
        gospels[str(spring_gospel_id)] = gospel
        spring_v2["gospels"] = gospels
        try:
            changes = self._reward_changes(
                working,
                reward_definition.rewards,
                request_id=CS_SPRING_V2_TAKE_AFFECTION_LEVEL_REWARD,
                operation="take_affection_level_reward",
                claim_key=f"spring_v2:affection:{spring_gospel_id}:{level}",
                source_id=reward_definition.record_id,
                timestamp=now,
            )
        except SpringApplicationError as exc:
            return failure(exc.error_code, exc.reason)
        changes["spring_v2"] = spring_v2
        return SpringResult(
            SC_SPRING_V2_TAKE_AFFECTION_LEVEL_REWARD_RET,
            self.make_take_affection_reward_ret(
                spring_gospel_id=spring_gospel_id,
                rewards=reward_definition.rewards,
                take_map=self._affection_take_map(gospel, definition),
            ),
            changes=changes,
        )

    def _interact_with_gospel(
        self, profile: Mapping[str, Any], request_body: bytes
    ) -> SpringResult:
        spring_gospel_id = _decode_uint_field(request_body, 1, maximum=UINT32_MAX)
        definition = self.catalog.spring_gospels.get(spring_gospel_id)
        if definition is None:
            return SpringResult(
                SC_SPRING_V2_INTERACT_WITH_GOSPEL_RET,
                self.make_interact_ret(ERROR_SPRING_V2_GOSPEL_NOT_FOUND),
                error_code=ERROR_SPRING_V2_GOSPEL_NOT_FOUND,
                reason=f"unknown spring-v2 gospel ID {spring_gospel_id}",
            )
        working = self._seeded_copy(profile)
        spring_v2 = copy.deepcopy(dict(working["spring_v2"]))
        gospels = copy.deepcopy(dict(spring_v2["gospels"]))
        gospel = copy.deepcopy(dict(gospels[str(spring_gospel_id)]))
        if not bool(gospel.get("is_unlocked", True)):
            return SpringResult(
                SC_SPRING_V2_INTERACT_WITH_GOSPEL_RET,
                self.make_interact_ret(ERROR_SPRING_V2_GOSPEL_NOT_UNLOCKED),
                error_code=ERROR_SPRING_V2_GOSPEL_NOT_UNLOCKED,
                reason=f"spring-v2 gospel {spring_gospel_id} is locked",
            )
        task = self._normalize_task_state(gospel.get("task_holder"), spring_gospel_id)
        changes: Dict[str, Any] = {}
        if task is not None and task["task_status"] == TASK_UNFINISHED:
            now = int(time.time())
            task["task_status"] = TASK_FINISHED
            task["progress"] = max(1, int(task["progress"]))
            task["finish_time"] = now
            gospel["task_holder"] = task
            gospels[str(spring_gospel_id)] = gospel
            spring_v2["gospels"] = gospels
            changes["spring_v2"] = spring_v2
        notification = self.make_gospel_update_notification(definition, gospel)
        return SpringResult(
            SC_SPRING_V2_INTERACT_WITH_GOSPEL_RET,
            self.make_interact_ret(),
            changes=changes,
            notifications=((NOTIFY_SPRING_V2_GOSPEL_UPDATE, notification),),
        )

    def _commit_task(
        self, profile: Mapping[str, Any], request_body: bytes
    ) -> SpringResult:
        task_id = _decode_uint_field(request_body, 1, maximum=UINT32_MAX)
        task_definition = self.catalog.tasks_by_id.get(task_id)

        def failure(error_code: int, reason: str) -> SpringResult:
            return SpringResult(
                SC_SPRING_V2_COMMIT_TASK_RET,
                self.make_commit_task_ret(error_code),
                error_code=error_code,
                reason=reason,
            )

        if task_definition is None:
            return failure(
                ERROR_SPRING_V2_NO_TASK_CAN_BE_COMMITTED,
                f"unknown spring-v2 task ID {task_id}",
            )
        spring_gospel_id = task_definition.spring_gospel_id
        definition = self.catalog.spring_gospels[spring_gospel_id]
        working = self._seeded_copy(profile)
        spring_v2 = copy.deepcopy(dict(working["spring_v2"]))
        gospels = copy.deepcopy(dict(spring_v2["gospels"]))
        gospel = copy.deepcopy(dict(gospels[str(spring_gospel_id)]))
        task = self._normalize_task_state(gospel.get("task_holder"), spring_gospel_id)
        if not bool(gospel.get("daily_task_accepted", False)) or task is None:
            return failure(
                ERROR_SPRING_V2_DAILY_TASK_NOT_ACCEPTED,
                f"spring-v2 gospel {spring_gospel_id} has no accepted task",
            )
        if task["task_id"] != task_id:
            return failure(
                ERROR_SPRING_V2_COMMITTED_TASK_NOT_MATCH,
                f"accepted task {task['task_id']} does not match {task_id}",
            )
        if task["task_status"] != TASK_FINISHED:
            return failure(
                ERROR_SPRING_V2_NO_TASK_CAN_BE_COMMITTED,
                f"spring-v2 task {task_id} is not finished",
            )
        now = int(time.time())
        task["task_status"] = TASK_REWARDED
        task["commit_time"] = now
        chain = gospel.get("daily_task_chain")
        chain = copy.deepcopy(chain) if isinstance(chain, list) else []
        chain.append(copy.deepcopy(task))
        gospel["daily_task_chain"] = chain
        completed = gospel.get("completed_task_ids")
        completed = copy.deepcopy(completed) if isinstance(completed, list) else []
        if task_id not in completed:
            completed.append(task_id)
        gospel["completed_task_ids"] = completed
        tasks = self.catalog.tasks_by_spring_gospel[spring_gospel_id]
        task_index = next(
            index
            for index, candidate in enumerate(tasks)
            if candidate.task_id == task_id
        )
        gospel["task_cursor"] = (task_index + 1) % len(tasks)
        previous_affection = _safe_uint(gospel.get("affection"), 0, UINT32_MAX)
        gospel["affection"] = min(
            SPRING_AFFECTION_EXP,
            previous_affection + task_definition.favor_score_increase,
        )
        gospel["affection_level"] = self._affection_level(
            spring_gospel_id, int(gospel["affection"])
        )
        gospel["task_holder"] = None
        gospel["daily_task_accepted"] = False
        gospels[str(spring_gospel_id)] = gospel
        spring_v2["gospels"] = gospels
        history = spring_v2.get("task_history")
        history = copy.deepcopy(history) if isinstance(history, list) else []
        history.append(
            {
                "task_id": task_id,
                "spring_gospel_id": spring_gospel_id,
                "scene_id": definition.scene_id,
                "accept_time": int(task["accept_time"]),
                "finish_time": int(task["finish_time"]),
                "commit_time": now,
                "favor_score_increase": task_definition.favor_score_increase,
                "affection_before": previous_affection,
                "affection_after": int(gospel["affection"]),
            }
        )
        spring_v2["task_history"] = history
        notification = self.make_gospel_update_notification(
            definition, gospel, task_override=task
        )
        return SpringResult(
            SC_SPRING_V2_COMMIT_TASK_RET,
            self.make_commit_task_ret(),
            changes={"spring_v2": spring_v2},
            notifications=((NOTIFY_SPRING_V2_GOSPEL_UPDATE, notification),),
        )

    @staticmethod
    def _nickname_validation_error(nickname: str) -> int:
        if not nickname or nickname != nickname.strip():
            return ERROR_SPRING_V2_NICKNAME_INVALID
        utf16_length = len(nickname.encode("utf-16-le")) // 2
        if utf16_length > NICKNAME_UTF16_LIMIT:
            return ERROR_SPRING_V2_NICKNAME_OUT_OF_LENGTH
        for character in nickname:
            category = unicodedata.category(character)
            if category[0] in {"L", "M", "N"}:
                continue
            if character in {" ", "_", "-", "·"}:
                continue
            return ERROR_SPRING_V2_NICKNAME_INVALID
        return ERROR_SUCCESS

    def _change_nickname(
        self, profile: Mapping[str, Any], request_body: bytes
    ) -> SpringResult:
        scene_id = _decode_uint_field(request_body, 1, maximum=UINT32_MAX)
        spring_gospel_id = _decode_uint_field(request_body, 2, maximum=UINT32_MAX)
        nickname = _decode_string_field(request_body, 3)
        definition = self.catalog.spring_gospels.get(spring_gospel_id)

        def failure(error_code: int, reason: str) -> SpringResult:
            return SpringResult(
                SC_SPRING_V2_CHANGE_SPRING_GOSPEL_NICKNAME_RET,
                self.make_change_nickname_ret(error_code),
                error_code=error_code,
                reason=reason,
            )

        if scene_id not in self.catalog.spring_gospel_ids_by_scene:
            return failure(
                ERROR_SPRING_V2_SCENE_NOT_EXIST,
                f"unknown spring-v2 scene ID {scene_id}",
            )
        if definition is None:
            return failure(
                ERROR_SPRING_V2_GOSPEL_NOT_FOUND,
                f"unknown spring-v2 gospel ID {spring_gospel_id}",
            )
        if definition.scene_id != scene_id:
            return failure(
                ERROR_SPRING_V2_SCENE_NOT_MATCH,
                f"spring-v2 gospel {spring_gospel_id} is not in scene {scene_id}",
            )
        validation_error = self._nickname_validation_error(nickname)
        if validation_error:
            return failure(validation_error, "spring-v2 nickname is not safe")
        working = self._seeded_copy(profile)
        spring_v2 = copy.deepcopy(dict(working["spring_v2"]))
        gospels = copy.deepcopy(dict(spring_v2["gospels"]))
        gospel = copy.deepcopy(dict(gospels[str(spring_gospel_id)]))
        if not bool(gospel.get("is_unlocked", True)):
            return failure(
                ERROR_SPRING_V2_GOSPEL_NOT_UNLOCKED,
                f"spring-v2 gospel {spring_gospel_id} is locked",
            )
        if gospel.get("nickname", "") == nickname:
            return failure(
                ERROR_SPRING_V2_NICKNAME_UNCHANGED,
                "spring-v2 nickname is unchanged",
            )
        gospel["nickname"] = nickname
        gospels[str(spring_gospel_id)] = gospel
        spring_v2["gospels"] = gospels
        notification = self.make_gospel_update_notification(definition, gospel)
        return SpringResult(
            SC_SPRING_V2_CHANGE_SPRING_GOSPEL_NICKNAME_RET,
            self.make_change_nickname_ret(),
            changes={"spring_v2": spring_v2},
            notifications=((NOTIFY_SPRING_V2_GOSPEL_UPDATE, notification),),
        )

    def _default_maid_info(self, profile: Mapping[str, Any]) -> Dict[str, int]:
        legacy = profile.get("legacy_spring")
        if isinstance(legacy, Mapping):
            char_info = legacy.get("char_info")
            if isinstance(char_info, Mapping):
                char_type = _safe_uint(char_info.get("char_type"), UINT32_MAX)
                char_id = _safe_uint(char_info.get("char_id"), 0, UINT32_MAX)
                if char_type in {0, 1} and char_id:
                    return {"CharType": char_type, "CharId": char_id}
        heroes = profile.get("heroes")
        if isinstance(heroes, list):
            for row in heroes:
                if isinstance(row, Mapping):
                    hero_id = _safe_uint(row.get("id"), 0, UINT32_MAX)
                    if hero_id:
                        return {"CharType": 0, "CharId": hero_id}
        gospels = profile.get("gospels")
        if isinstance(gospels, list):
            for row in gospels:
                if isinstance(row, Mapping):
                    gospel_id = _safe_uint(row.get("id"), 0, UINT32_MAX)
                    if gospel_id:
                        return {"CharType": 1, "CharId": gospel_id}
        return {"CharType": 0, "CharId": self.catalog.hero_ids[0]}

    def _touch_spring_model(
        self, profile: Mapping[str, Any], request_body: bytes
    ) -> SpringResult:
        char_info = _decode_maid_info(request_body)
        interact_type = _decode_uint_field(request_body, 2, maximum=UINT32_MAX)
        if interact_type not in self.catalog.legacy_interaction_deltas:
            return SpringResult(
                SC_TOUCH_SPRING_MODEL_RET,
                self.make_touch_spring_model_ret(
                    ERROR_CLIENT_REQUEST_PARAM,
                    char_info,
                    interact_type,
                    0,
                    self.catalog.legacy_initial_safety,
                ),
                error_code=ERROR_CLIENT_REQUEST_PARAM,
                reason=f"unknown MaidInteractType {interact_type}",
            )
        raw_legacy = profile.get("legacy_spring")
        legacy = (
            copy.deepcopy(dict(raw_legacy)) if isinstance(raw_legacy, Mapping) else {}
        )
        safety_cost, satisfaction_gain = self.catalog.legacy_interaction_deltas[
            interact_type
        ]
        safety = _safe_uint(
            legacy.get("safety"), self.catalog.legacy_initial_safety, UINT32_MAX
        )
        satisfaction = _safe_uint(legacy.get("satisfaction"), 0, UINT32_MAX)
        safety = max(0, safety - safety_cost)
        satisfaction = min(
            self.catalog.legacy_max_satisfaction,
            satisfaction + satisfaction_gain,
        )
        legacy.update(
            {
                "char_info": {
                    "char_type": char_info["CharType"],
                    "char_id": char_info["CharId"],
                },
                "last_interact_type": interact_type,
                "safety": safety,
                "satisfaction": satisfaction,
                "last_interact_time": int(time.time()),
            }
        )
        return SpringResult(
            SC_TOUCH_SPRING_MODEL_RET,
            self.make_touch_spring_model_ret(
                ERROR_SUCCESS,
                char_info,
                interact_type,
                satisfaction,
                safety,
            ),
            changes={"legacy_spring": legacy},
        )

    def _generate_spring_prelude(
        self, profile: Mapping[str, Any], request_body: bytes
    ) -> SpringResult:
        _validate_wire(request_body)
        char_info = self._default_maid_info(profile)
        plot_ids = (self.catalog.legacy_default_plot_id,)
        raw_legacy = profile.get("legacy_spring")
        legacy = (
            copy.deepcopy(dict(raw_legacy)) if isinstance(raw_legacy, Mapping) else {}
        )
        legacy.update(
            {
                "char_info": {
                    "char_type": char_info["CharType"],
                    "char_id": char_info["CharId"],
                },
                "generated_plot_pkg_list": list(plot_ids),
                "generated_time": int(time.time()),
            }
        )
        return SpringResult(
            SC_GENERATE_SPRING_PRELUDE_PLOT_RET,
            self.make_generate_spring_prelude_ret(ERROR_SUCCESS, plot_ids, char_info),
            changes={"legacy_spring": legacy},
        )

    def _choose_spring_prelude(
        self, profile: Mapping[str, Any], request_body: bytes
    ) -> SpringResult:
        plot_id = _decode_uint_field(request_body, 1, maximum=UINT32_MAX)
        raw_legacy = profile.get("legacy_spring")
        legacy = (
            copy.deepcopy(dict(raw_legacy)) if isinstance(raw_legacy, Mapping) else {}
        )
        generated = legacy.get("generated_plot_pkg_list")
        allowed = (
            {_safe_uint(value, 0, UINT32_MAX) for value in generated}
            if isinstance(generated, list)
            else set()
        )
        if plot_id not in allowed or plot_id not in self.catalog.legacy_plot_ids:
            return SpringResult(
                SC_CHOOSE_SPRING_PRELUDE_PLOT_RET,
                self.make_choose_spring_prelude_ret(
                    ERROR_CLIENT_REQUEST_PARAM, plot_id
                ),
                error_code=ERROR_CLIENT_REQUEST_PARAM,
                reason=f"old spring plot {plot_id} was not generated",
            )
        legacy["selected_plot_pkg_id"] = plot_id
        legacy["selected_time"] = int(time.time())
        return SpringResult(
            SC_CHOOSE_SPRING_PRELUDE_PLOT_RET,
            self.make_choose_spring_prelude_ret(ERROR_SUCCESS, plot_id),
            changes={"legacy_spring": legacy},
        )

    def _end_spring_prelude(
        self, profile: Mapping[str, Any], request_body: bytes
    ) -> SpringResult:
        plot_id = _decode_uint_field(request_body, 1, maximum=UINT32_MAX)
        raw_legacy = profile.get("legacy_spring")
        legacy = (
            copy.deepcopy(dict(raw_legacy)) if isinstance(raw_legacy, Mapping) else {}
        )
        if _safe_uint(legacy.get("selected_plot_pkg_id"), 0, UINT32_MAX) != plot_id:
            return SpringResult(
                SC_SPRING_PRELUDE_PLOT_END_RET,
                self.make_spring_prelude_end_ret(
                    ERROR_CLIENT_REQUEST_PARAM,
                    plot_id,
                    _safe_uint(profile.get("spring_status"), 0, UINT32_MAX),
                ),
                error_code=ERROR_CLIENT_REQUEST_PARAM,
                reason=f"old spring plot {plot_id} is not the selected plot",
            )
        legacy["ended_plot_pkg_id"] = plot_id
        legacy["ended"] = True
        legacy["end_time"] = int(time.time())
        return SpringResult(
            SC_SPRING_PRELUDE_PLOT_END_RET,
            self.make_spring_prelude_end_ret(ERROR_SUCCESS, plot_id, 1),
            changes={"legacy_spring": legacy, "spring_status": 1},
        )

    def _wipe_steam(
        self, profile: Mapping[str, Any], request_body: bytes
    ) -> SpringResult:
        clean_area = _decode_uint_field(request_body, 1, maximum=UINT32_MAX)
        raw_legacy = profile.get("legacy_spring")
        legacy = (
            copy.deepcopy(dict(raw_legacy)) if isinstance(raw_legacy, Mapping) else {}
        )
        clean_areas = legacy.get("clean_areas")
        clean_areas = (
            copy.deepcopy(clean_areas) if isinstance(clean_areas, list) else []
        )
        if clean_area not in clean_areas:
            clean_areas.append(clean_area)
        legacy["clean_areas"] = clean_areas
        legacy["last_wipe_time"] = int(time.time())
        return SpringResult(
            SC_WIPE_STEAM_IN_SPRING_RET,
            self.make_wipe_steam_ret(ERROR_SUCCESS),
            changes={"legacy_spring": legacy},
        )

    def _wire_error(
        self,
        request_id: int,
        profile: Mapping[str, Any],
        reason: str,
    ) -> SpringResult:
        response_id = RESPONSE_IDS[request_id]
        if request_id == CS_SPRING_V2_REQUEST_DATA:
            body = self.make_request_data_ret(profile, ERROR_MESSAGE_DATA_ILLEGAL)
        elif request_id == CS_SPRING_V2_REQUEST_SCENE_DATA:
            body = self.make_scene_data_ret(
                profile, self.catalog.scene_ids[0], ERROR_MESSAGE_DATA_ILLEGAL
            )
        elif request_id == CS_SPRING_V2_REQUEST_SPRING_GOSPEL_AFFECTION_LEVEL:
            body = self.make_affection_level_ret(profile, ERROR_MESSAGE_DATA_ILLEGAL)
        elif request_id == CS_SPRING_V2_ACCEPT_TASK:
            body = self.make_accept_task_ret(ERROR_MESSAGE_DATA_ILLEGAL)
        elif request_id == CS_SPRING_V2_GENERATE_BUBBLE:
            body = self.make_generate_bubble_ret(ERROR_MESSAGE_DATA_ILLEGAL)
        elif request_id == CS_SPRING_V2_TAKE_BUBBLE_REWARD:
            body = self.make_take_bubble_reward_ret(
                ERROR_MESSAGE_DATA_ILLEGAL, position=(0, 0)
            )
        elif request_id == CS_SPRING_V2_TAKE_AFFECTION_LEVEL_REWARD:
            body = self.make_take_affection_reward_ret(ERROR_MESSAGE_DATA_ILLEGAL)
        elif request_id == CS_SPRING_V2_COMMIT_TASK:
            body = self.make_commit_task_ret(ERROR_MESSAGE_DATA_ILLEGAL)
        elif request_id == CS_SPRING_V2_INTERACT_WITH_GOSPEL:
            body = self.make_interact_ret(ERROR_MESSAGE_DATA_ILLEGAL)
        elif request_id == CS_SPRING_V2_CHANGE_SPRING_GOSPEL_NICKNAME:
            body = self.make_change_nickname_ret(ERROR_MESSAGE_DATA_ILLEGAL)
        elif request_id == CS_TOUCH_SPRING_MODEL:
            body = self.make_touch_spring_model_ret(
                ERROR_MESSAGE_DATA_ILLEGAL,
                self._default_maid_info(profile),
                0,
                0,
                self.catalog.legacy_initial_safety,
            )
        elif request_id == CS_GENERATE_SPRING_PRELUDE_PLOT:
            body = self.make_generate_spring_prelude_ret(
                ERROR_MESSAGE_DATA_ILLEGAL,
                (self.catalog.legacy_default_plot_id,),
                self._default_maid_info(profile),
            )
        elif request_id == CS_CHOOSE_SPRING_PRELUDE_PLOT:
            body = self.make_choose_spring_prelude_ret(ERROR_MESSAGE_DATA_ILLEGAL, 0)
        elif request_id == CS_SPRING_PRELUDE_PLOT_END:
            body = self.make_spring_prelude_end_ret(ERROR_MESSAGE_DATA_ILLEGAL, 0, 0)
        elif request_id == CS_WIPE_STEAM_IN_SPRING:
            body = self.make_wipe_steam_ret(ERROR_MESSAGE_DATA_ILLEGAL)
        elif request_id == CS_REQUEST_SPRING_DATAS:
            body = self.make_legacy_spring_datas_ret(profile)
        elif request_id == CS_REQUEST_DATING_DATAS:
            body = self.codec.encode(
                "SC_DatingDatasRet",
                {"HeroUId": 0, "PreferenceList": [], "DateStoryList": []},
            )
        elif request_id == CS_REQUEST_HERO_REWARDED_FONDNESS:
            body = self.codec.encode(
                "SC_HeroRewardedFondnessRet",
                {"HeroUId": 0, "RewardedFondnessList": []},
            )
        else:
            body = self.codec.encode(
                "SC_SpringV2RequestUnlockedSpringGospel_Ret",
                {"UnlockedSpringGospel": []},
            )
        return SpringResult(
            response_id,
            body,
            error_code=ERROR_MESSAGE_DATA_ILLEGAL,
            reason=reason,
        )

    def handle(
        self,
        request_id: int,
        profile: Optional[Mapping[str, Any]],
        request_body: bytes = b"",
    ) -> Optional[SpringResult]:
        """Handle ordinary-fondness and spring requests, or return ``None``."""

        if request_id not in RESPONSE_IDS:
            return None
        account = profile if isinstance(profile, Mapping) else {}
        try:
            if request_id == CS_REQUEST_SPRING_DATAS:
                requested_role_id = _decode_uint_field(
                    request_body, 1, maximum=UINT64_MAX
                )
                actual_role_id = _safe_uint(account.get("role_id"), 0, UINT64_MAX)
                body = self.make_legacy_spring_datas_ret(account)
                if requested_role_id != actual_role_id:
                    return SpringResult(
                        SC_SPRING_DATAS_RET,
                        body,
                        error_code=ERROR_ROLE_ID_ILLEGAL,
                        reason=(
                            f"requested role {requested_role_id} does not match "
                            f"profile role {actual_role_id}"
                        ),
                    )
                return SpringResult(SC_SPRING_DATAS_RET, body)

            if request_id in {
                CS_REQUEST_DATING_DATAS,
                CS_REQUEST_HERO_REWARDED_FONDNESS,
            }:
                hero_uid = _decode_uint_field(request_body, 1, maximum=UINT64_MAX)
                hero = self._hero_for_uid(account, hero_uid)
                if hero is None:
                    message = (
                        "SC_DatingDatasRet"
                        if request_id == CS_REQUEST_DATING_DATAS
                        else "SC_HeroRewardedFondnessRet"
                    )
                    values = {"HeroUId": hero_uid}
                    if request_id == CS_REQUEST_DATING_DATAS:
                        values.update({"PreferenceList": [], "DateStoryList": []})
                    else:
                        values["RewardedFondnessList"] = []
                    return SpringResult(
                        RESPONSE_IDS[request_id],
                        self.codec.encode(message, values),
                        error_code=ERROR_HERO_NOT_FOUND,
                        reason=f"unknown account HeroUid {hero_uid}",
                    )
                if request_id == CS_REQUEST_DATING_DATAS:
                    body = self.make_dating_datas_ret(account, hero_uid)
                else:
                    body = self.make_hero_rewarded_fondness_ret(hero_uid, hero)
                return SpringResult(RESPONSE_IDS[request_id], body)

            if request_id == CS_SPRING_V2_REQUEST_SCENE_DATA:
                scene_id = _decode_uint_field(request_body, 1, maximum=UINT32_MAX)
                if scene_id not in self.catalog.spring_gospel_ids_by_scene:
                    return SpringResult(
                        SC_SPRING_V2_REQUEST_SCENE_DATA_RET,
                        self.make_scene_data_ret(
                            account,
                            self.catalog.scene_ids[0],
                            ERROR_SPRING_V2_SCENE_NOT_EXIST,
                        ),
                        error_code=ERROR_SPRING_V2_SCENE_NOT_EXIST,
                        reason=f"unknown spring-v2 scene ID {scene_id}",
                    )
                return SpringResult(
                    SC_SPRING_V2_REQUEST_SCENE_DATA_RET,
                    self.make_scene_data_ret(account, scene_id),
                )

            if request_id == CS_SPRING_V2_ACCEPT_TASK:
                return self._accept_task(account, request_body)

            if request_id == CS_SPRING_V2_GENERATE_BUBBLE:
                return self._generate_bubble(account, request_body)
            if request_id == CS_SPRING_V2_TAKE_BUBBLE_REWARD:
                return self._take_bubble_reward(account, request_body)
            if request_id == CS_SPRING_V2_TAKE_AFFECTION_LEVEL_REWARD:
                return self._take_affection_reward(account, request_body)
            if request_id == CS_SPRING_V2_COMMIT_TASK:
                return self._commit_task(account, request_body)
            if request_id == CS_SPRING_V2_INTERACT_WITH_GOSPEL:
                return self._interact_with_gospel(account, request_body)
            if request_id == CS_SPRING_V2_CHANGE_SPRING_GOSPEL_NICKNAME:
                return self._change_nickname(account, request_body)

            if request_id == CS_TOUCH_SPRING_MODEL:
                return self._touch_spring_model(account, request_body)
            if request_id == CS_GENERATE_SPRING_PRELUDE_PLOT:
                return self._generate_spring_prelude(account, request_body)
            if request_id == CS_CHOOSE_SPRING_PRELUDE_PLOT:
                return self._choose_spring_prelude(account, request_body)
            if request_id == CS_SPRING_PRELUDE_PLOT_END:
                return self._end_spring_prelude(account, request_body)
            if request_id == CS_WIPE_STEAM_IN_SPRING:
                return self._wipe_steam(account, request_body)

            _validate_wire(request_body)
            if request_id == CS_SPRING_V2_REQUEST_DATA:
                body = self.make_request_data_ret(account)
            elif request_id == CS_SPRING_V2_REQUEST_UNLOCKED_SPRING_GOSPEL:
                body = self.make_unlocked_spring_gospel_ret()
            else:
                body = self.make_affection_level_ret(account)
            return SpringResult(RESPONSE_IDS[request_id], body)
        except ProtobufDecodeError as exc:
            return self._wire_error(request_id, account, str(exc))


@lru_cache(maxsize=1)
def default_layer() -> SpringContentLayer:
    return SpringContentLayer()


def seed_spring_content(
    profile: MutableMapping[str, Any],
    catalog: Optional[Union[ContentCatalog, str, Path]] = None,
) -> MutableMapping[str, Any]:
    layer = default_layer() if catalog is None else SpringContentLayer(catalog)
    return layer.seed_profile(profile)


def seed_spring_profile(
    profile: MutableMapping[str, Any],
    catalog: Optional[Union[ContentCatalog, str, Path]] = None,
) -> MutableMapping[str, Any]:
    """Compatibility spelling for callers that name seeders after profiles."""

    return seed_spring_content(profile, catalog)


def handle_spring_request(
    request_id: int,
    profile: Optional[Mapping[str, Any]],
    request_body: bytes = b"",
) -> Optional[SpringResult]:
    return default_layer().handle(request_id, profile, request_body)


# Short aliases make GameState integration consistent with the other layers.
Layer = SpringContentLayer
Result = SpringResult


__all__ = [
    "BUBBLE_LIMIT_GLOBAL_ID",
    "CS_REQUEST_DATING_DATAS",
    "CS_REQUEST_HERO_REWARDED_FONDNESS",
    "CS_REQUEST_SPRING_DATAS",
    "CS_SPRING_V2_REQUEST_DATA",
    "CS_SPRING_V2_REQUEST_SCENE_DATA",
    "CS_SPRING_V2_ACCEPT_TASK",
    "CS_SPRING_V2_REQUEST_SPRING_GOSPEL_AFFECTION_LEVEL",
    "CS_SPRING_V2_REQUEST_UNLOCKED_SPRING_GOSPEL",
    "ERROR_HERO_NOT_FOUND",
    "ERROR_MESSAGE_DATA_ILLEGAL",
    "ERROR_ROLE_ID_ILLEGAL",
    "ERROR_SPRING_V2_DAILY_TASK_ALREADY_ACCEPTED",
    "ERROR_SPRING_V2_GOSPEL_NOT_FOUND",
    "ERROR_SPRING_V2_GOSPEL_NOT_UNLOCKED",
    "ERROR_SPRING_V2_SCENE_NOT_EXIST",
    "ERROR_SUCCESS",
    "Layer",
    "NOTIFY_SPRING_V2_GOSPEL_UPDATE",
    "ORDINARY_FONDNESS_EXP",
    "ORDINARY_FONDNESS_LEVEL",
    "OrdinaryFavorDefinition",
    "RESPONSE_IDS",
    "Result",
    "SC_DATING_DATAS_RET",
    "SC_HERO_REWARDED_FONDNESS_RET",
    "SC_SPRING_DATAS_RET",
    "SC_SPRING_V2_REQUEST_DATA_RET",
    "SC_SPRING_V2_REQUEST_SCENE_DATA_RET",
    "SC_SPRING_V2_ACCEPT_TASK_RET",
    "SC_SPRING_V2_REQUEST_SPRING_GOSPEL_AFFECTION_LEVEL_RET",
    "SC_SPRING_V2_REQUEST_UNLOCKED_SPRING_GOSPEL_RET",
    "SPRING_AFFECTION_EXP",
    "SPRING_AFFECTION_LEVEL",
    "SpringCatalogError",
    "SpringContentLayer",
    "SpringGospelDefinition",
    "SpringProfileError",
    "SpringResult",
    "SpringStaticCatalog",
    "SpringTaskDefinition",
    "default_layer",
    "handle_spring_request",
    "seed_spring_content",
    "seed_spring_profile",
]
