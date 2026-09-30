"""Catalog-backed Oracle Division state and scene-initialization endpoints.

The current runtime enters ``DailyCityView3D`` and requests architecture,
expedition, legacy-rest, and work snapshots.  The neighboring handlers already
return schema-valid data, but an empty ``SC_WorkDatasRet.WorkList`` leaves the
entertainment-room scene without ``WorkPanelRoleId``.  ``ButtonsTogglePanel``
then dereferences that missing worker before it can select ``EntryOnsen`` and
send the spring-v2 request (1336).

This layer owns the local work-session lifecycle, legacy rest changes, the farm
snapshot, the horse-race selection snapshot, and the login-time fish-tank
cache.  Work timing, fatigue, earnings, pending rewards, and claims are derived
from the pinned static tables and persisted as copy-on-write account roots.
Farm, race, and fishing IDs are restricted to the verified static-table closure
needed by the bundled runtime.  The hot-spring endpoint remains in
``spring_content``; this module supplies the surrounding Oracle state required
to reach it safely.
"""

from __future__ import annotations

import copy
import json
import time
from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Dict, Optional, Sequence, Tuple, Union

try:  # Package imports in tests; fallbacks support direct server execution.
    from .content_profile import (
        DAILY_CITY_HERO_SCENE_IDS,
        ContentCatalog,
        default_content_catalog,
    )
    from .content_responses import ProtobufDecodeError, iter_wire_fields
    from .inventory_content import (
        InventoryContentError,
        normalize_inventory,
        stable_item_uid,
    )
    from .protobuf_codec import ProtocolMessageCodec
except ImportError:  # pragma: no cover - direct-script compatibility
    from content_profile import (  # type: ignore
        DAILY_CITY_HERO_SCENE_IDS,
        ContentCatalog,
        default_content_catalog,
    )
    from content_responses import (  # type: ignore
        ProtobufDecodeError,
        iter_wire_fields,
    )
    from inventory_content import (  # type: ignore
        InventoryContentError,
        normalize_inventory,
        stable_item_uid,
    )
    from protobuf_codec import ProtocolMessageCodec  # type: ignore


CS_REQUEST_WORK_DATAS = 86
SC_WORK_DATAS_RET = 87
CS_WORK_START = 88
SC_WORK_START_RET = 89
CS_TAKE_WORK_REWARD = 90
SC_TAKE_WORK_REWARD_RET = 91
CS_REST_START = 97
SC_REST_START_RET = 98
CS_REST_END = 99
SC_REST_END_RET = 100
CS_WORK_END = 104
SC_WORK_END_RET = 105
CS_REQUEST_FISH_DATAS = 472
SC_FISH_DATAS_RET = 473
CS_REQUEST_RACE_HORSE_DATAS = 482
SC_REQUEST_RACE_HORSE_DATAS_RET = 483
CS_INVITE_HERO_TO_SCENE = 492
SC_INVITE_HERO_TO_SCENE_RET = 493
NOTIFY_UPDATE_FISHING_ATTRIBUTE = 521
CS_REQUEST_FARM_DATAS = 653
SC_REQUEST_FARM_DATAS_RET = 654
CS_REQUEST_FARMLAND_INFO = 693
SC_REQUEST_FARMLAND_INFO_RET = 694
CS_TAKE_WORK_REWARD_ALL = 1113
SC_TAKE_WORK_REWARD_ALL_RET = 1114
CS_WORK_START_BATCH = 1115
SC_WORK_START_BATCH_RET = 1116
CS_WORK_END_BATCH = 1117
SC_WORK_END_BATCH_RET = 1118

RESPONSE_IDS = {
    CS_REQUEST_WORK_DATAS: SC_WORK_DATAS_RET,
    CS_WORK_START: SC_WORK_START_RET,
    CS_TAKE_WORK_REWARD: SC_TAKE_WORK_REWARD_RET,
    CS_REST_START: SC_REST_START_RET,
    CS_REST_END: SC_REST_END_RET,
    CS_WORK_END: SC_WORK_END_RET,
    CS_REQUEST_FISH_DATAS: SC_FISH_DATAS_RET,
    CS_REQUEST_RACE_HORSE_DATAS: SC_REQUEST_RACE_HORSE_DATAS_RET,
    CS_INVITE_HERO_TO_SCENE: SC_INVITE_HERO_TO_SCENE_RET,
    CS_REQUEST_FARM_DATAS: SC_REQUEST_FARM_DATAS_RET,
    CS_REQUEST_FARMLAND_INFO: SC_REQUEST_FARMLAND_INFO_RET,
    CS_TAKE_WORK_REWARD_ALL: SC_TAKE_WORK_REWARD_ALL_RET,
    CS_WORK_START_BATCH: SC_WORK_START_BATCH_RET,
    CS_WORK_END_BATCH: SC_WORK_END_BATCH_RET,
}

# DailyCityView3D.SendRequestCMD.  The other four endpoints are handled by
# existing content/runtime/spring layers.
DAILY_CITY_INITIALIZATION = MappingProxyType(
    {
        391: 392,  # architecture
        56: 57,  # expedition state
        376: 377,  # expedition records
        95: 96,  # legacy GospelRest data, owned by spring_content
        CS_REQUEST_WORK_DATAS: SC_WORK_DATAS_RET,
    }
)

# RiChangCommonSceneModules.awake and EntryOnsen.SubRefresh respectively.
DAILY_SCENE_NAVIGATION = (675, 676)
ONSEN_V2_INITIALIZATION = (1336, 1337)
ONSEN_V2_SCENE_INITIALIZATION = (1338, 1339)

ERROR_SUCCESS = 0
ERROR_ROLE_ID_ILLEGAL = 104
ERROR_NO_REWARD_CAN_BE_TAKEN = 122
ERROR_GOSPEL_IN_WORKING_CANNOT_REST = 124
ERROR_GOSPEL_IN_RESTING_CANNOT_WORK = 125
ERROR_GOSPEL_ALREADY_WORKING = 146
ERROR_GOSPEL_TO_WORK_NO_ENERGY = 199
ERROR_GOSPEL_NOT_FOUND = 424
ERROR_HERO_NOT_FOUND = 516
ERROR_CLIENT_REQUEST_PARAM = 32_000
ERROR_EXCEED_ITEM_LIMIT_COUNT = 429
ERROR_MESSAGE_DATA_ILLEGAL = 32769

UINT32_MAX = (1 << 32) - 1
UINT64_MAX = (1 << 64) - 1
HERO_SCENE_IDS = DAILY_CITY_HERO_SCENE_IDS
ORACLE_STATE_KEY = "oracle_content"
ORACLE_STATE_SCHEMA_VERSION = 5
FISHING_ATTRIBUTE_SCHEMA_VERSION = 1
ENTERTAINMENT_BUILDING_ID = 1_999_703

# ``UserData.SetFishingAttribute`` compares these six fields against the
# corresponding ``a_fishing_ability`` rows.  FishTankView.RefreshCost then
# dereferences ``FishingAttribute.Fishtankcost`` without a null guard.  The
# lower bounds are the smallest values present in the pinned runtime table; the
# upper bounds are its ``top_limit`` values.  Keeping this mapping explicit
# makes the login notification both non-null and static-table-valid without
# inventing any caught fish.
FISHING_ATTRIBUTE_CONFIG = MappingProxyType(
    {
        "goodluck": ("Goodluck", 1_998_201, 10, 1_500),
        "power": ("Power", 1_998_202, 10, 1_500),
        "toughness": ("Toughness", 1_998_203, 10, 1_500),
        "skill": ("Skill", 1_998_204, 10, 1_500),
        "agile": ("Agile", 1_998_205, 10, 1_500),
        "fishtank_cost": ("Fishtankcost", 1_998_206, 30, 100),
    }
)
FISHING_SCENE_IDS = (1_998_401, 1_998_402, 1_998_403, 1_998_404)
FISHING_BAIT_IDS = tuple(range(1_998_301, 1_998_307))

# ``a_farmer_level[1801016]`` is the verified maximum: farmer level 16,
# ``land_num=16`` and ``fertilizer_limit=10``.  FarmData itself also allocates
# exactly sixteen slots and indexes them by ``LandId - 1``.
FARMER_LEVEL_CONFIG_ID = 1_801_016
FARMER_LEVEL = 16
FARM_LAND_IDS = tuple(range(1, 17))
FARM_FERTILIZER_LIMIT = 10

# ``a_farmer_parameter[1805001]`` defines raw temperature 1..100 as the first
# valid runtime temperature band.  Use its upper bound rather than the invalid
# protobuf default zero.
FARM_TEMPERATURE_PARAMETER_ID = 1_805_001
FARM_CURRENT_TEMPERATURE = 100

# The pinned TableConfig ``a_horse`` contains one guide-only horse with
# appear_weight=0 (2016001) and exactly these four selectable horses with
# appear_weight=1000.  HorseRace allocates ``HorseOnChoose = new GirlAi[4]``;
# including the guide horse would therefore be unsafe.
RACE_HORSE_IDS = (2_016_002, 2_016_003, 2_016_004, 2_016_005)
RACE_HORSE_SPEED_LEVEL = 6
RACE_HORSE_STATUS_NORMAL = 2
RACE_REWARD_RANK_COUNT = 4
RACE_BASE_STAKE_CONFIG_ID = 9_990_188

# Exact a_building closure.  The values also equal RichangBuildType and the
# a_gospel.work affinity used by WorkBuildInfo.UpdateSelf.
BUILDING_TYPES = MappingProxyType(
    {
        1_999_701: 1,  # office / FuYinShengDian
        1_999_702: 2,  # main hall / CanTing
        1_999_703: 3,  # recreation room / YuLeShi
        1_999_705: 5,  # private room / ZiZhai
    }
)
REQUIRED_WORK_BUILDING_IDS = tuple(BUILDING_TYPES)


@dataclass(frozen=True)
class VisibleFunctionRoute:
    """One navigation route defined from ``DailyCityView3D``."""

    function_type: str
    building_id: int
    panel_index: Optional[int]
    entry_component: Optional[str] = None


VISIBLE_FUNCTION_ROUTES = (
    VisibleFunctionRoute("BuildingLevelup", 1_999_701, 1),
    VisibleFunctionRoute("GospelRest", 1_999_705, 1),
    VisibleFunctionRoute("Farm", 1_999_705, 2),
    VisibleFunctionRoute("FishTank", 1_999_705, None),
    VisibleFunctionRoute("Cooking", 1_999_702, 1),
    VisibleFunctionRoute("HorseRace", 1_999_703, 1),
    VisibleFunctionRoute("Fishing", 1_999_703, 2),
    VisibleFunctionRoute("Onsen", 1_999_703, 3, "EntryOnsen"),
)


class OracleCatalogError(ValueError):
    """Raised when the generated static tables do not close over this slice."""


class OracleProfileError(ValueError):
    """Raised when no account-owned Gospel can safely initialize a building."""


@dataclass(frozen=True)
class OracleResult:
    """A protobuf response and optional top-level shallow-merge changes."""

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


@dataclass(frozen=True)
class WorkReward:
    """One catalog-backed pending reward in persisted and wire form."""

    res_type: int
    item_id: int
    count: int
    para: int = 0
    position: int = 0
    quality: int = 0
    value: int = 0

    def scaled(self, multiplier: int) -> "WorkReward":
        return WorkReward(
            self.res_type,
            self.item_id,
            self.count * multiplier,
            self.para,
            self.position,
            self.quality,
            self.value,
        )

    def as_state(self) -> Dict[str, int]:
        return {
            "res_type": self.res_type,
            "item_id": self.item_id,
            "count": self.count,
            "para": self.para,
            "position": self.position,
            "quality": self.quality,
            "value": self.value,
        }

    def as_wire(self) -> Dict[str, int]:
        return {
            "ResType": self.res_type,
            "ItemId": self.item_id,
            "Count": self.count,
            "Para": self.para,
            "Position": self.position,
            "Quality": self.quality,
            "Value": self.value,
        }


@dataclass(frozen=True)
class WorkLevelDefinition:
    """Pinned work-session inputs from one ``a_work_level`` row."""

    config_id: int
    building_id: int
    work_level: int
    base_time: int
    fatigue: int
    favor_rate: int
    base_efficiency: int
    stock: int
    rewards: Tuple[WorkReward, ...]


class OracleWorkError(OracleProfileError):
    """A validated work-domain failure with its protocol ErrorCode."""

    def __init__(self, error_code: int, reason: str) -> None:
        super().__init__(reason)
        self.error_code = error_code


def _int(value: Any, context: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise OracleCatalogError(f"{context} must be an integer")
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


def _normalized_config_ids(value: Any, allowed: Tuple[int, ...]) -> list[int]:
    """Return a deterministic, duplicate-free subset of pinned config IDs."""

    if not isinstance(value, (list, tuple)):
        return []
    allowed_ids = set(allowed)
    result = []
    seen = set()
    for raw in value:
        parsed = _safe_uint(raw, 0, UINT32_MAX)
        if parsed not in allowed_ids or parsed in seen:
            continue
        result.append(parsed)
        seen.add(parsed)
    return result


def _coerce_catalog(
    catalog: Optional[Union[ContentCatalog, str, Path]],
) -> ContentCatalog:
    if catalog is None:
        return default_content_catalog()
    if isinstance(catalog, ContentCatalog):
        return catalog
    return ContentCatalog.load(catalog)


class OracleStaticCatalog:
    """Validated projection of buildings, levels, and Gospel work affinity."""

    REQUIRED_FIELDS = {
        "a_building": {"id", "building_type"},
        "a_work_level": {
            "id",
            "building_id",
            "work_level",
            "gambling_count_limit",
            "time",
            "fatigue",
            "favor_rate",
            "reward",
            "efficiency",
            "stock",
        },
        "a_gospel": {
            "id",
            "using_gospel",
            "work",
            "efficiency",
            "work_energy",
            "promotion_content",
        },
        "a_global": {"id", "int_value", "string_value"},
        "a_item": {"id", "max_stack_num"},
    }

    def __init__(self, catalog: ContentCatalog) -> None:
        self.catalog = catalog
        for table_name, required in self.REQUIRED_FIELDS.items():
            try:
                table = catalog.table(table_name)
            except KeyError:
                raise OracleCatalogError(f"catalog is missing {table_name}") from None
            missing = required - set(table.fields)
            if missing:
                raise OracleCatalogError(
                    f"{table_name} is missing required fields: {sorted(missing)}"
                )

        building_types = {
            _int(row.get("id"), "a_building.id"): _int(
                row.get("building_type"), "a_building.building_type"
            )
            for row in catalog.table("a_building").records
        }
        if building_types != dict(BUILDING_TYPES):
            raise OracleCatalogError(
                f"a_building closure changed: {sorted(building_types.items())}"
            )
        self.building_types = MappingProxyType(building_types)
        self.building_ids = tuple(building_types)

        item_max_stacks: Dict[int, int] = {}
        for row in catalog.table("a_item").records:
            item_id = _int(row.get("id"), "a_item.id")
            maximum = _int(row.get("max_stack_num"), "a_item.max_stack_num")
            if item_id <= 0 or maximum < 0:
                raise OracleCatalogError(
                    f"a_item[{item_id}] has invalid max_stack_num {maximum}"
                )
            item_max_stacks[item_id] = maximum
        self.item_max_stacks = MappingProxyType(item_max_stacks)

        levels_by_building: Dict[int, list[int]] = {
            building_id: [] for building_id in self.building_ids
        }
        work_level_definitions: Dict[Tuple[int, int], WorkLevelDefinition] = {}
        for row in catalog.table("a_work_level").records:
            config_id = _int(row.get("id"), "a_work_level.id")
            building_id = _int(row.get("building_id"), "a_work_level.building_id")
            if building_id not in levels_by_building:
                raise OracleCatalogError(
                    f"a_work_level[{config_id}] references unknown building "
                    f"{building_id}"
                )
            work_level = _int(row.get("work_level"), "a_work_level.work_level")
            levels_by_building[building_id].append(work_level)

            raw_reward = row.get("reward")
            if raw_reward is None or raw_reward == "":
                reward_rows: Any = []
            elif isinstance(raw_reward, str):
                try:
                    reward_rows = json.loads(raw_reward)
                except (TypeError, ValueError) as exc:
                    raise OracleCatalogError(
                        f"a_work_level[{config_id}].reward is invalid JSON"
                    ) from exc
            else:
                raise OracleCatalogError(
                    f"a_work_level[{config_id}].reward must be JSON text"
                )
            if not isinstance(reward_rows, list):
                raise OracleCatalogError(
                    f"a_work_level[{config_id}].reward must be a list"
                )
            rewards = []
            for reward in reward_rows:
                if not isinstance(reward, Mapping):
                    raise OracleCatalogError(
                        f"a_work_level[{config_id}].reward entry must be a mapping"
                    )
                res_type = _int(
                    reward.get("type"),
                    f"a_work_level[{config_id}].reward.type",
                )
                item_id = _int(
                    reward.get("id"),
                    f"a_work_level[{config_id}].reward.id",
                )
                count = _int(
                    reward.get("num"),
                    f"a_work_level[{config_id}].reward.num",
                )
                if (
                    res_type != 4
                    or item_id not in item_max_stacks
                    or item_max_stacks[item_id] <= 0
                    or count <= 0
                ):
                    raise OracleCatalogError(
                        f"a_work_level[{config_id}] has unsafe reward "
                        f"{res_type}/{item_id}/{count}"
                    )
                rewards.append(WorkReward(res_type, item_id, count))

            definition = WorkLevelDefinition(
                config_id=config_id,
                building_id=building_id,
                work_level=work_level,
                base_time=_int(row.get("time"), "a_work_level.time"),
                fatigue=_int(row.get("fatigue"), "a_work_level.fatigue"),
                favor_rate=_int(row.get("favor_rate"), "a_work_level.favor_rate"),
                base_efficiency=_int(
                    row.get("efficiency"), "a_work_level.efficiency"
                ),
                stock=_int(row.get("stock"), "a_work_level.stock"),
                rewards=tuple(rewards),
            )
            key = (building_id, work_level)
            if key in work_level_definitions:
                raise OracleCatalogError(
                    f"duplicate a_work_level for building/level {key}"
                )
            if work_level == 0:
                if any(
                    (
                        definition.base_time,
                        definition.fatigue,
                        definition.favor_rate,
                        definition.base_efficiency,
                        definition.stock,
                    )
                ) or definition.rewards:
                    raise OracleCatalogError(
                        f"a_work_level[{config_id}] level zero must be inert"
                    )
            elif (
                definition.base_time <= 0
                or definition.fatigue <= 0
                or definition.base_efficiency <= 0
                or definition.stock <= 0
                or not definition.rewards
            ):
                raise OracleCatalogError(
                    f"a_work_level[{config_id}] active level is incomplete"
                )
            work_level_definitions[key] = definition
        normalized_levels = {
            building_id: tuple(sorted(levels))
            for building_id, levels in levels_by_building.items()
        }
        for building_id, levels in normalized_levels.items():
            if levels != tuple(range(6)):
                raise OracleCatalogError(
                    f"building {building_id} work levels are not exactly 0..5"
                )
        self.work_levels = MappingProxyType(normalized_levels)
        self.work_level_definitions = MappingProxyType(work_level_definitions)

        entertainment_limits = tuple(
            _int(
                row.get("gambling_count_limit"),
                "a_work_level.gambling_count_limit",
            )
            for row in sorted(
                (
                    row
                    for row in catalog.table("a_work_level").records
                    if row.get("building_id") == ENTERTAINMENT_BUILDING_ID
                ),
                key=lambda row: _int(row.get("work_level"), "a_work_level.work_level"),
            )
        )
        if entertainment_limits != (0, 30, 30, 30, 30, 30):
            raise OracleCatalogError(
                "entertainment gambling limits changed: "
                f"{entertainment_limits}"
            )
        self.race_bet_limit = max(entertainment_limits)

        try:
            base_stake_row = catalog.record("a_global", RACE_BASE_STAKE_CONFIG_ID)
        except KeyError:
            raise OracleCatalogError(
                f"a_global is missing race stake {RACE_BASE_STAKE_CONFIG_ID}"
            ) from None
        self.race_base_stake = _int(
            base_stake_row.get("int_value"),
            f"a_global[{RACE_BASE_STAKE_CONFIG_ID}].int_value",
        )
        if self.race_base_stake <= 0:
            raise OracleCatalogError("race base stake must be positive")

        usable_ids = set(catalog.usable_gospel_ids)
        work_type_by_gospel: Dict[int, int] = {}
        work_energy_by_gospel: Dict[int, int] = {}
        work_efficiency_by_gospel: Dict[int, int] = {}
        candidates: Dict[int, list[int]] = {
            building_id: [] for building_id in self.building_ids
        }
        building_by_type = {
            building_type: building_id
            for building_id, building_type in self.building_types.items()
        }
        for row in catalog.table("a_gospel").records:
            gospel_id = _int(row.get("id"), "a_gospel.id")
            if gospel_id not in usable_ids:
                continue
            work_type = _int(row.get("work"), "a_gospel.work")
            efficiency = _int(row.get("efficiency"), "a_gospel.efficiency")
            work_energy = _int(row.get("work_energy"), "a_gospel.work_energy")
            promotion_content = _int(
                row.get("promotion_content"), "a_gospel.promotion_content"
            )
            building_id = building_by_type.get(work_type)
            if (
                building_id is None
                or efficiency <= 0
                or not 0 < work_energy <= UINT32_MAX
            ):
                raise OracleCatalogError(
                    f"usable Gospel {gospel_id} has invalid work affinity "
                    f"{work_type}/{efficiency}/{work_energy}"
                )
            work_type_by_gospel[gospel_id] = work_type
            work_energy_by_gospel[gospel_id] = work_energy
            work_efficiency_by_gospel[gospel_id] = (
                efficiency if promotion_content == 0 else 0
            )
            candidates[building_id].append(gospel_id)

        if set(work_type_by_gospel) != usable_ids:
            missing = sorted(usable_ids - set(work_type_by_gospel))
            raise OracleCatalogError(
                f"usable Gospel work affinity is incomplete: {missing}"
            )
        frozen_candidates = {
            building_id: tuple(sorted(gospel_ids))
            for building_id, gospel_ids in candidates.items()
        }
        empty = [
            building_id
            for building_id, gospel_ids in frozen_candidates.items()
            if not gospel_ids
        ]
        if empty:
            raise OracleCatalogError(
                f"buildings have no compatible usable Gospel: {empty}"
            )
        self.work_type_by_gospel = MappingProxyType(work_type_by_gospel)
        self.work_energy_by_gospel = MappingProxyType(work_energy_by_gospel)
        self.work_efficiency_by_gospel = MappingProxyType(
            work_efficiency_by_gospel
        )
        self.gospel_ids_by_building = MappingProxyType(frozen_candidates)

        unknown_routes = sorted(
            {route.building_id for route in VISIBLE_FUNCTION_ROUTES}
            - set(self.building_ids)
        )
        if unknown_routes:
            raise OracleCatalogError(
                f"DailyCityView3D routes reference unknown buildings: {unknown_routes}"
            )


def _decode_role_id(data: bytes, message_name: str = "CS_RequestWorkDatas") -> int:
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise ProtobufDecodeError("request body must be bytes")
    values = []
    for field_number, wire_type, value in iter_wire_fields(bytes(data)):
        if field_number != 1:
            raise ProtobufDecodeError(
                f"{message_name} has unknown field {field_number}"
            )
        if wire_type != 0:
            raise ProtobufDecodeError(f"{message_name}.RoleId is not a varint")
        parsed = int(value)
        if parsed > UINT64_MAX:
            raise ProtobufDecodeError(f"{message_name}.RoleId exceeds uint64")
        values.append(parsed)
    if len(values) != 1:
        raise ProtobufDecodeError(
            f"{message_name} requires exactly one RoleId field"
        )
    return values[0]


def _decode_work_request(
    data: bytes,
    message_name: str,
    *,
    include_gospel: bool,
) -> Tuple[int, int, int]:
    """Decode one strict role/workplace[/GospelUId] request."""

    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise ProtobufDecodeError("request body must be bytes")
    values: Dict[int, list[int]] = {1: [], 2: [], 3: []}
    allowed = {1, 2, 3} if include_gospel else {1, 2}
    for field_number, wire_type, value in iter_wire_fields(bytes(data)):
        if field_number not in allowed:
            raise ProtobufDecodeError(
                f"{message_name} has unknown field {field_number}"
            )
        if wire_type != 0:
            field_name = {1: "RoleId", 2: "WorkplaceId", 3: "GospelUId"}[
                field_number
            ]
            raise ProtobufDecodeError(
                f"{message_name}.{field_name} is not a varint"
            )
        parsed = int(value)
        maximum = UINT64_MAX if field_number in {1, 3} else UINT32_MAX
        if parsed > maximum:
            field_name = {1: "RoleId", 2: "WorkplaceId", 3: "GospelUId"}[
                field_number
            ]
            raise ProtobufDecodeError(
                f"{message_name}.{field_name} exceeds "
                f"{'uint64' if maximum == UINT64_MAX else 'uint32'}"
            )
        values[field_number].append(parsed)
    for field_number in allowed:
        if len(values[field_number]) != 1:
            field_name = {1: "RoleId", 2: "WorkplaceId", 3: "GospelUId"}[
                field_number
            ]
            raise ProtobufDecodeError(
                f"{message_name} requires exactly one {field_name} field"
            )
    return (
        values[1][0],
        values[2][0],
        values[3][0] if include_gospel else 0,
    )


def _decode_work_batch(
    data: bytes,
    message_name: str,
) -> Tuple[int, Dict[int, int]]:
    """Decode the exact work-batch RoleId/settings map contract."""

    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise ProtobufDecodeError("request body must be bytes")
    role_ids = []
    settings: Dict[int, int] = {}
    for field_number, wire_type, value in iter_wire_fields(bytes(data)):
        if field_number == 1:
            if wire_type != 0:
                raise ProtobufDecodeError(f"{message_name}.RoleId is not a varint")
            parsed = int(value)
            if parsed > UINT64_MAX:
                raise ProtobufDecodeError(f"{message_name}.RoleId exceeds uint64")
            role_ids.append(parsed)
            continue
        if field_number != 2:
            raise ProtobufDecodeError(
                f"{message_name} has unknown field {field_number}"
            )
        if wire_type != 2 or not isinstance(value, bytes):
            raise ProtobufDecodeError(f"{message_name}.Settings is not a map entry")

        keys = []
        gospel_uids = []
        for entry_field, entry_wire, entry_value in iter_wire_fields(value):
            if entry_field not in {1, 2}:
                raise ProtobufDecodeError(
                    f"{message_name}.Settings has unknown field {entry_field}"
                )
            if entry_wire != 0:
                raise ProtobufDecodeError(
                    f"{message_name}.Settings key/value is not a varint"
                )
            parsed = int(entry_value)
            if entry_field == 1:
                if parsed > UINT32_MAX:
                    raise ProtobufDecodeError(
                        f"{message_name}.Settings key exceeds uint32"
                    )
                keys.append(parsed)
            else:
                if parsed > UINT64_MAX:
                    raise ProtobufDecodeError(
                        f"{message_name}.Settings value exceeds uint64"
                    )
                gospel_uids.append(parsed)
        if len(keys) != 1 or len(gospel_uids) != 1:
            raise ProtobufDecodeError(
                f"{message_name}.Settings requires one key and one value"
            )
        building_id = keys[0]
        if building_id in settings:
            raise ProtobufDecodeError(
                f"{message_name}.Settings duplicates building {building_id}"
            )
        settings[building_id] = gospel_uids[0]

    if len(role_ids) != 1:
        raise ProtobufDecodeError(
            f"{message_name} requires exactly one RoleId field"
        )
    if not settings:
        raise ProtobufDecodeError(f"{message_name}.Settings must not be empty")
    return role_ids[0], settings


def _decode_work_start_batch(data: bytes) -> Tuple[int, Dict[int, int]]:
    """Compatibility wrapper retained for Oracle-specific callers/tests."""

    return _decode_work_batch(data, "CS_WorkStartBatch")


def _decode_rest_request(
    data: bytes,
    message_name: str,
) -> Tuple[int, Dict[int, int]]:
    """Decode ``RoleId`` plus the exact slot->GospelUId protobuf map."""

    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise ProtobufDecodeError("request body must be bytes")
    role_ids = []
    gospel_map: Dict[int, int] = {}
    for field_number, wire_type, value in iter_wire_fields(bytes(data)):
        if field_number == 1:
            if wire_type != 0:
                raise ProtobufDecodeError(f"{message_name}.RoleId is not a varint")
            parsed = int(value)
            if parsed > UINT64_MAX:
                raise ProtobufDecodeError(f"{message_name}.RoleId exceeds uint64")
            role_ids.append(parsed)
            continue
        if field_number != 2:
            raise ProtobufDecodeError(
                f"{message_name} has unknown field {field_number}"
            )
        if wire_type != 2 or not isinstance(value, bytes):
            raise ProtobufDecodeError(f"{message_name}.GospelMap is not a map entry")

        slots = []
        gospel_uids = []
        for entry_field, entry_wire, entry_value in iter_wire_fields(value):
            if entry_field not in {1, 2}:
                raise ProtobufDecodeError(
                    f"{message_name}.GospelMap has unknown field {entry_field}"
                )
            if entry_wire != 0:
                raise ProtobufDecodeError(
                    f"{message_name}.GospelMap key/value is not a varint"
                )
            parsed = int(entry_value)
            if entry_field == 1:
                if parsed > UINT32_MAX:
                    raise ProtobufDecodeError(
                        f"{message_name}.GospelMap slot exceeds uint32"
                    )
                slots.append(parsed)
            else:
                if parsed > UINT64_MAX:
                    raise ProtobufDecodeError(
                        f"{message_name}.GospelMap value exceeds uint64"
                    )
                gospel_uids.append(parsed)
        if len(slots) != 1 or len(gospel_uids) != 1:
            raise ProtobufDecodeError(
                f"{message_name}.GospelMap requires one key and one value"
            )
        slot = slots[0]
        gospel_uid = gospel_uids[0]
        if slot not in range(6):
            raise ProtobufDecodeError(
                f"{message_name}.GospelMap slot {slot} is outside 0..5"
            )
        if gospel_uid <= 0:
            raise ProtobufDecodeError(
                f"{message_name}.GospelMap contains zero GospelUId"
            )
        if slot in gospel_map:
            raise ProtobufDecodeError(
                f"{message_name}.GospelMap duplicates slot {slot}"
            )
        gospel_map[slot] = gospel_uid

    if len(role_ids) != 1:
        raise ProtobufDecodeError(
            f"{message_name} requires exactly one RoleId field"
        )
    if not gospel_map:
        raise ProtobufDecodeError(f"{message_name}.GospelMap must not be empty")
    if len(set(gospel_map.values())) != len(gospel_map):
        raise ProtobufDecodeError(
            f"{message_name}.GospelMap reuses one GospelUId in multiple slots"
        )
    return role_ids[0], gospel_map


def _decode_farm_request(data: bytes) -> Tuple[int, int]:
    """Decode ``CS_RequestFarmDatas`` without accepting unknown friends."""

    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise ProtobufDecodeError("request body must be bytes")
    role_ids = []
    friend_ids = []
    for field_number, wire_type, value in iter_wire_fields(bytes(data)):
        if field_number not in {1, 2}:
            raise ProtobufDecodeError(
                f"CS_RequestFarmDatas has unknown field {field_number}"
            )
        if wire_type != 0:
            field_name = "RoleId" if field_number == 1 else "FriendId"
            raise ProtobufDecodeError(
                f"CS_RequestFarmDatas.{field_name} is not a varint"
            )
        parsed = int(value)
        if parsed > UINT64_MAX:
            field_name = "RoleId" if field_number == 1 else "FriendId"
            raise ProtobufDecodeError(
                f"CS_RequestFarmDatas.{field_name} exceeds uint64"
            )
        (role_ids if field_number == 1 else friend_ids).append(parsed)
    if len(role_ids) != 1:
        raise ProtobufDecodeError(
            "CS_RequestFarmDatas requires exactly one RoleId field"
        )
    if len(friend_ids) > 1:
        raise ProtobufDecodeError(
            "CS_RequestFarmDatas accepts at most one FriendId field"
        )
    return role_ids[0], friend_ids[0] if friend_ids else 0


def _decode_empty_request(data: bytes, message_name: str) -> None:
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise ProtobufDecodeError("request body must be bytes")
    fields = list(iter_wire_fields(bytes(data)))
    if fields:
        raise ProtobufDecodeError(
            f"{message_name} must be empty, got field {fields[0][0]}"
        )


def _decode_invite_hero_to_scene(data: bytes) -> Tuple[int, int, bool]:
    """Strictly decode ``CS_InviteHeroToScene`` scalar fields.

    Proto3 omits a scalar whose value is zero, and DailyCity's location zero
    is valid.  A missing field 1 therefore means ``SceneId=0``.  HeroUId is a
    positive account identity and must occur exactly once.  Duplicate/unknown
    fields, wrong wire types, unsigned overflow, and non-boolean IsGuide
    values are rejected instead of being silently accepted by the generic
    endpoint runtime.
    """

    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise ProtobufDecodeError("request body must be bytes")

    scene_ids = []
    hero_uids = []
    guide_values = []
    for field_number, wire_type, value in iter_wire_fields(bytes(data)):
        if field_number not in {1, 2, 3}:
            raise ProtobufDecodeError(
                f"CS_InviteHeroToScene has unknown field {field_number}"
            )
        if wire_type != 0 or not isinstance(value, int):
            field_name = {1: "SceneId", 2: "HeroUId", 3: "IsGuide"}[
                field_number
            ]
            raise ProtobufDecodeError(
                f"CS_InviteHeroToScene.{field_name} is not a varint"
            )
        maximum = UINT64_MAX if field_number == 2 else UINT32_MAX
        if value > maximum:
            field_name = {1: "SceneId", 2: "HeroUId", 3: "IsGuide"}[
                field_number
            ]
            raise ProtobufDecodeError(
                f"CS_InviteHeroToScene.{field_name} exceeds its unsigned range"
            )
        if field_number == 1:
            scene_ids.append(value)
        elif field_number == 2:
            hero_uids.append(value)
        else:
            guide_values.append(value)

    if len(scene_ids) > 1:
        raise ProtobufDecodeError(
            "CS_InviteHeroToScene accepts at most one SceneId field"
        )
    if len(hero_uids) != 1:
        raise ProtobufDecodeError(
            "CS_InviteHeroToScene requires exactly one HeroUId field"
        )
    if len(guide_values) > 1:
        raise ProtobufDecodeError(
            "CS_InviteHeroToScene accepts at most one IsGuide field"
        )
    if guide_values and guide_values[0] not in {0, 1}:
        raise ProtobufDecodeError(
            "CS_InviteHeroToScene.IsGuide must be a protobuf bool"
        )
    return (
        scene_ids[0] if scene_ids else 0,
        hero_uids[0],
        bool(guide_values[0]) if guide_values else False,
    )


class OracleContentLayer:
    """Maintain the persistent Oracle state required by its visible scenes."""

    def __init__(
        self,
        catalog: Optional[Union[ContentCatalog, str, Path]] = None,
        *,
        codec: Optional[ProtocolMessageCodec] = None,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        self.content_catalog = _coerce_catalog(catalog)
        self.catalog = OracleStaticCatalog(self.content_catalog)
        self.codec = codec or ProtocolMessageCodec()
        self.clock = clock or time.time

    def _now(self) -> int:
        try:
            value = int(self.clock())
        except (TypeError, ValueError, OverflowError) as exc:
            raise OracleProfileError("oracle clock must return a timestamp") from exc
        if value < 0 or value > UINT64_MAX:
            raise OracleProfileError("oracle clock timestamp is outside uint64")
        return value

    def _account_gospels(
        self, profile: Mapping[str, Any]
    ) -> Tuple[Dict[int, Mapping[str, Any]], Dict[int, Mapping[str, Any]]]:
        raw_gospels = profile.get("gospels")
        if not isinstance(raw_gospels, list):
            raise OracleProfileError("profile.gospels must be a list")

        by_id: Dict[int, Mapping[str, Any]] = {}
        by_uid: Dict[int, Mapping[str, Any]] = {}
        for raw in raw_gospels:
            if not isinstance(raw, Mapping):
                continue
            gospel_id = _safe_uint(raw.get("id"), 0, UINT32_MAX)
            if gospel_id not in self.catalog.work_type_by_gospel:
                continue
            gospel_uid = _safe_uint(raw.get("uid"), 0, UINT64_MAX)
            if gospel_uid <= 0:
                raise OracleProfileError(
                    f"account Gospel {gospel_id} has no valid GospelUId"
                )
            if gospel_id in by_id:
                raise OracleProfileError(
                    f"profile has duplicate Gospel ID {gospel_id}"
                )
            if gospel_uid in by_uid:
                raise OracleProfileError(
                    f"profile has duplicate GospelUId {gospel_uid}"
                )
            by_id[gospel_id] = raw
            by_uid[gospel_uid] = raw
        return by_id, by_uid

    @staticmethod
    def _previous_schema_version(profile: Mapping[str, Any]) -> int:
        previous = profile.get(ORACLE_STATE_KEY)
        if not isinstance(previous, Mapping):
            return 0
        return _safe_uint(previous.get("schema_version"), 0, UINT32_MAX)

    def _remaining_work_energy(
        self,
        row: Mapping[str, Any],
        gospel_id: int,
    ) -> int:
        """Return a runtime-safe remaining-energy value for one worker.

        The runtime initializes ``FuyingData.EnergyMax`` from
        ``a_gospel.work_energy`` and treats ``Energy / EnergyMax <= 0.15`` as
        tired.  Both work-selection progress bars display those values divided
        by 10,000.  Consequently the catalog value (1,000,000 in this build),
        not protobuf zero or the old profile default 100, is fully available.
        """

        if gospel_id == 0 and _safe_uint(row.get("gospel_uid"), 0, UINT64_MAX) == 0:
            return 0
        maximum = self.catalog.work_energy_by_gospel[gospel_id]
        return _safe_uint(row.get("remaining_energy"), maximum, maximum)

    def _building_work_definition(
        self,
        profile: Mapping[str, Any],
        building_id: int,
    ) -> WorkLevelDefinition:
        raw_buildings = profile.get("buildings")
        source = _mapping_lookup(raw_buildings, building_id)
        source = source if isinstance(source, Mapping) else {}
        maximum_level = max(self.catalog.work_levels[building_id])
        work_level = _safe_uint(
            source.get("work_level"), maximum_level, maximum_level
        )
        definition = self.catalog.work_level_definitions.get(
            (building_id, work_level)
        )
        if definition is None or work_level == 0:
            raise OracleWorkError(
                ERROR_MESSAGE_DATA_ILLEGAL,
                f"building {building_id} has no active a_work_level for level "
                f"{work_level}",
            )
        return definition

    def _work_period_seconds(
        self,
        definition: WorkLevelDefinition,
        gospel_id: int,
    ) -> int:
        efficiency = self.catalog.work_efficiency_by_gospel[gospel_id]
        denominator = definition.base_efficiency * 10_000 + efficiency
        if denominator <= 0:
            raise OracleProfileError("work efficiency denominator must be positive")
        period = definition.base_time * 10_000 // denominator
        if not 0 < period <= UINT32_MAX:
            raise OracleProfileError(
                f"work period for building {definition.building_id} is invalid"
            )
        return period

    def _new_work_session(
        self,
        profile: Mapping[str, Any],
        building_id: int,
        gospel_id: int,
        gospel_uid: int,
        energy: int,
        sequence: int,
        now: int,
    ) -> Dict[str, int]:
        definition = self._building_work_definition(profile, building_id)
        return {
            "session_id": sequence,
            "worker_uid": gospel_uid,
            "worker_id": gospel_id,
            "started_at": now,
            "ended_at": 0,
            "last_settled_at": now,
            "work_level": definition.work_level,
            "work_level_config_id": definition.config_id,
            "period_seconds": self._work_period_seconds(definition, gospel_id),
            "fatigue_per_minute": definition.fatigue,
            "favor_rate": definition.favor_rate,
            "stock": definition.stock,
            "start_energy": energy,
            "energy_spent": 0,
            "worked_seconds": 0,
            "completed_cycles": 0,
        }

    def _reward_from_state(
        self,
        value: Mapping[str, Any],
        context: str,
    ) -> WorkReward:
        res_type = _safe_uint(value.get("res_type"), 0, UINT32_MAX)
        item_id = _safe_uint(value.get("item_id"), 0, UINT32_MAX)
        count = _safe_uint(value.get("count"), 0, UINT32_MAX)
        if (
            res_type != 4
            or item_id not in self.catalog.item_max_stacks
            or self.catalog.item_max_stacks[item_id] <= 0
            or count <= 0
        ):
            raise OracleProfileError(
                f"{context} has invalid reward {res_type}/{item_id}/{count}"
            )
        return WorkReward(
            res_type,
            item_id,
            count,
            _safe_uint(value.get("para"), 0, UINT32_MAX),
            _safe_uint(value.get("position"), 0, UINT32_MAX),
            _safe_uint(value.get("quality"), 0, UINT32_MAX),
            _safe_uint(value.get("value"), 0, UINT32_MAX),
        )

    def _rewards_from_state(
        self,
        value: Any,
        context: str,
    ) -> list[WorkReward]:
        if value is None:
            return []
        if not isinstance(value, list):
            raise OracleProfileError(f"{context} must be a list")
        rewards = []
        for index, raw in enumerate(value):
            if not isinstance(raw, Mapping):
                raise OracleProfileError(f"{context}[{index}] must be a mapping")
            rewards.append(self._reward_from_state(raw, f"{context}[{index}]"))
        return self._aggregate_rewards(rewards)

    @staticmethod
    def _aggregate_rewards(rewards: Sequence[WorkReward]) -> list[WorkReward]:
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
            totals[key] = totals.get(key, 0) + reward.count
        return [
            WorkReward(
                res_type,
                item_id,
                count,
                para,
                position,
                quality,
                value,
            )
            for (
                res_type,
                item_id,
                para,
                position,
                quality,
                value,
            ), count in sorted(totals.items())
            if count > 0
        ]

    def _normalize_session(
        self,
        profile: Mapping[str, Any],
        building_id: int,
        gospel_id: int,
        gospel_uid: int,
        remaining_energy: int,
        source: Mapping[str, Any],
        now: int,
    ) -> Optional[Dict[str, int]]:
        raw_session = source.get("session")
        if not isinstance(raw_session, Mapping):
            return None
        session_id = _safe_uint(raw_session.get("session_id"), 0, UINT64_MAX)
        worker_uid = _safe_uint(raw_session.get("worker_uid"), 0, UINT64_MAX)
        worker_id = _safe_uint(raw_session.get("worker_id"), 0, UINT32_MAX)
        if session_id <= 0 or worker_uid != gospel_uid or worker_id != gospel_id:
            return None

        work_level = _safe_uint(
            raw_session.get("work_level"),
            0,
            max(self.catalog.work_levels[building_id]),
        )
        definition = self.catalog.work_level_definitions.get(
            (building_id, work_level)
        )
        if definition is None or work_level == 0:
            definition = self._building_work_definition(profile, building_id)
            work_level = definition.work_level
        # Timing and capacity remain catalog-authoritative across JSON reloads;
        # persisted fields are an audit snapshot, not runtime-editable inputs.
        period = self._work_period_seconds(definition, gospel_id)
        maximum_energy = self.catalog.work_energy_by_gospel[gospel_id]
        energy_spent = _safe_uint(
            raw_session.get("energy_spent"), 0, maximum_energy
        )
        start_energy = _safe_uint(
            raw_session.get("start_energy"),
            min(maximum_energy, remaining_energy + energy_spent),
            maximum_energy,
        )
        if start_energy < remaining_energy:
            start_energy = remaining_energy
        energy_spent = min(energy_spent, start_energy)
        started_at = _safe_uint(raw_session.get("started_at"), now, UINT64_MAX)
        ended_at = _safe_uint(raw_session.get("ended_at"), 0, UINT64_MAX)
        if ended_at and ended_at < started_at:
            ended_at = started_at
        worked_seconds = _safe_uint(
            raw_session.get("worked_seconds"), 0, UINT32_MAX
        )
        last_settled_at = _safe_uint(
            raw_session.get("last_settled_at"),
            min(UINT64_MAX, started_at + worked_seconds),
            UINT64_MAX,
        )
        return {
            "session_id": session_id,
            "worker_uid": gospel_uid,
            "worker_id": gospel_id,
            "started_at": started_at,
            "ended_at": ended_at,
            "last_settled_at": max(started_at, last_settled_at),
            "work_level": work_level,
            "work_level_config_id": definition.config_id,
            "period_seconds": period,
            "fatigue_per_minute": definition.fatigue,
            "favor_rate": definition.favor_rate,
            "stock": definition.stock,
            "start_energy": start_energy,
            "energy_spent": energy_spent,
            "worked_seconds": worked_seconds,
            "completed_cycles": _safe_uint(
                raw_session.get("completed_cycles"), 0, UINT32_MAX
            ),
        }

    def _session_definition(
        self,
        building_id: int,
        session: Mapping[str, Any],
    ) -> WorkLevelDefinition:
        level = _safe_uint(
            session.get("work_level"),
            0,
            max(self.catalog.work_levels[building_id]),
        )
        definition = self.catalog.work_level_definitions.get((building_id, level))
        if definition is None or level == 0:
            raise OracleProfileError(
                f"work session at building {building_id} has invalid work level"
            )
        return definition

    def _upsert_earning(
        self,
        row: MutableMapping[str, Any],
        session: Mapping[str, Any],
        worked_seconds: int,
        added_rewards: Sequence[WorkReward],
    ) -> None:
        raw_earnings = row.get("earnings")
        earnings = raw_earnings if isinstance(raw_earnings, list) else []
        session_id = _safe_uint(session.get("session_id"), 0, UINT64_MAX)
        target: Optional[MutableMapping[str, Any]] = None
        for raw in earnings:
            if (
                isinstance(raw, MutableMapping)
                and _safe_uint(raw.get("session_id"), 0, UINT64_MAX) == session_id
            ):
                target = raw
                break
        if target is None:
            target = {
                "session_id": session_id,
                "gospel_uid": _safe_uint(
                    session.get("worker_uid"), 0, UINT64_MAX
                ),
                "work_time": 0,
                "fondness": 0,
                "reward_list": [],
            }
            earnings.append(target)
        existing_rewards = self._rewards_from_state(
            target.get("reward_list"), "oracle earning.reward_list"
        )
        target["gospel_uid"] = _safe_uint(
            session.get("worker_uid"), 0, UINT64_MAX
        )
        target["work_time"] = min(worked_seconds, UINT32_MAX)
        favor_rate = _safe_uint(session.get("favor_rate"), 0, UINT32_MAX)
        target["fondness"] = min(
            UINT32_MAX, favor_rate * worked_seconds // 10_000
        )
        target["reward_list"] = [
            reward.as_state()
            for reward in self._aggregate_rewards(
                [*existing_rewards, *added_rewards]
            )
        ]
        row["earnings"] = earnings

    def _settle_work_row(
        self,
        row: MutableMapping[str, Any],
        now: int,
        *,
        force: bool = False,
    ) -> None:
        if _safe_uint(row.get("is_valid"), 0, 1) != 1:
            return
        raw_session = row.get("session")
        if not isinstance(raw_session, MutableMapping):
            raise OracleProfileError("active Oracle worker has no session")
        building_id = _safe_uint(row.get("workplace_id"), 0, UINT32_MAX)
        definition = self._session_definition(building_id, raw_session)
        started_at = _safe_uint(raw_session.get("started_at"), now, UINT64_MAX)
        start_energy = _safe_uint(
            raw_session.get("start_energy"), 0, UINT32_MAX
        )
        fatigue = _safe_uint(
            raw_session.get("fatigue_per_minute"), definition.fatigue, UINT32_MAX
        )
        period = _safe_uint(
            raw_session.get("period_seconds"), 0, UINT32_MAX
        )
        stock = _safe_uint(raw_session.get("stock"), definition.stock, UINT32_MAX)
        if start_energy <= 0 or fatigue <= 0 or period <= 0 or stock <= 0:
            raise OracleProfileError(
                f"work session at building {building_id} has invalid timing inputs"
            )

        depletion_minutes = (start_energy + fatigue - 1) // fatigue
        maximum_elapsed = depletion_minutes * 60
        live_elapsed = max(0, now - started_at)
        effective_elapsed = min(live_elapsed, maximum_elapsed)
        auto_ended = live_elapsed >= maximum_elapsed
        total_cycles = min(UINT32_MAX, effective_elapsed // period)
        previous_cycles = _safe_uint(
            raw_session.get("completed_cycles"), 0, UINT32_MAX
        )
        newly_completed = max(0, total_cycles - previous_cycles)
        pending_count = _safe_uint(
            row.get("pending_reward_count"), 0, UINT32_MAX
        )
        credited_cycles = min(newly_completed, max(0, stock - pending_count))
        added_rewards = [
            reward.scaled(credited_cycles) for reward in definition.rewards
        ]
        if credited_cycles:
            existing = self._rewards_from_state(
                row.get("pending_rewards"), "oracle pending_rewards"
            )
            row["pending_reward_count"] = pending_count + credited_cycles
            row["pending_rewards"] = [
                reward.as_state()
                for reward in self._aggregate_rewards([*existing, *added_rewards])
            ]

        previous_worked = _safe_uint(
            raw_session.get("worked_seconds"), 0, UINT32_MAX
        )
        if force or auto_ended:
            settled_elapsed = effective_elapsed
        else:
            settled_elapsed = max(
                (effective_elapsed // 60) * 60,
                min(effective_elapsed, total_cycles * period),
            )
        settled_elapsed = min(UINT32_MAX, max(previous_worked, settled_elapsed))
        energy_spent = min(start_energy, fatigue * (effective_elapsed // 60))
        previous_spent = _safe_uint(
            raw_session.get("energy_spent"), 0, start_energy
        )
        energy_spent = max(previous_spent, energy_spent)
        progressed = (
            settled_elapsed > previous_worked
            or energy_spent > previous_spent
            or total_cycles > previous_cycles
            or credited_cycles > 0
        )
        raw_session["worked_seconds"] = settled_elapsed
        raw_session["energy_spent"] = energy_spent
        raw_session["completed_cycles"] = max(previous_cycles, total_cycles)
        raw_session["last_settled_at"] = min(
            UINT64_MAX, started_at + settled_elapsed
        )
        row["remaining_energy"] = start_energy - energy_spent
        if progressed:
            self._upsert_earning(
                row,
                raw_session,
                settled_elapsed,
                added_rewards,
            )
        if auto_ended:
            ended_at = min(UINT64_MAX, started_at + maximum_elapsed)
            raw_session["ended_at"] = ended_at
            raw_session["last_settled_at"] = ended_at
            row["is_valid"] = 0

    def _live_worked_seconds(
        self,
        row: Mapping[str, Any],
        now: int,
    ) -> int:
        session = row.get("session")
        if not isinstance(session, Mapping):
            return 0
        started_at = _safe_uint(session.get("started_at"), now, UINT64_MAX)
        ended_at = _safe_uint(session.get("ended_at"), 0, UINT64_MAX)
        endpoint = ended_at or now
        return min(UINT32_MAX, max(0, endpoint - started_at))

    def _work_remain_time(self, row: Mapping[str, Any], now: int) -> int:
        if _safe_uint(row.get("is_valid"), 0, 1) != 1:
            return 0
        session = row.get("session")
        if not isinstance(session, Mapping):
            return 0
        period = _safe_uint(session.get("period_seconds"), 0, UINT32_MAX)
        if period <= 0:
            return 0
        elapsed = self._live_worked_seconds(row, now)
        remainder = elapsed % period
        return period if remainder == 0 else period - remainder

    def _normalized_gospels(
        self,
        profile: Mapping[str, Any],
        state: Mapping[str, Any],
    ) -> list[Any]:
        """Mirror authoritative Oracle energy into the account Gospel rows.

        ``WorkBuildInfo`` reads energy from ``FuyingData`` rather than from the
        ``Msg.Work`` snapshot.  Normalizing only SC87 therefore leaves the UI
        in OVERLOAD.  Legacy Oracle schemas did not persist energy and seeded
        every Gospel with 100, so their first v5 normalization initializes all
        usable Gospels at the static maximum.  Once on v5, legal unassigned
        Gospel values and assigned workers' persisted remaining values survive
        JSON round trips.
        """

        raw_gospels = profile.get("gospels")
        if not isinstance(raw_gospels, list):
            raise OracleProfileError("profile.gospels must be a list")
        previous_version = self._previous_schema_version(profile)
        work = state.get("work")
        if not isinstance(work, Mapping):
            raise OracleProfileError("oracle work state must be a mapping")

        assigned_energy: Dict[int, int] = {}
        for building_id in REQUIRED_WORK_BUILDING_IDS:
            work_row = _mapping_lookup(work, building_id)
            if not isinstance(work_row, Mapping):
                raise OracleProfileError(
                    f"oracle work state is missing building {building_id}"
                )
            gospel_id = _safe_uint(work_row.get("gospel_id"), 0, UINT32_MAX)
            gospel_uid = _safe_uint(work_row.get("gospel_uid"), 0, UINT64_MAX)
            if gospel_id == 0 and gospel_uid == 0 and not work_row.get("is_valid"):
                continue
            if gospel_id not in self.catalog.work_energy_by_gospel or gospel_uid <= 0:
                raise OracleProfileError(
                    f"building {building_id} has invalid Gospel energy reference"
                )
            assigned_energy[gospel_uid] = self._remaining_work_energy(
                work_row, gospel_id
            )

        normalized: list[Any] = []
        for raw in raw_gospels:
            if not isinstance(raw, Mapping):
                normalized.append(copy.deepcopy(raw))
                continue
            row = copy.deepcopy(dict(raw))
            gospel_id = _safe_uint(row.get("id"), 0, UINT32_MAX)
            gospel_uid = _safe_uint(row.get("uid"), 0, UINT64_MAX)
            maximum = self.catalog.work_energy_by_gospel.get(gospel_id)
            if maximum is not None:
                if gospel_uid in assigned_energy:
                    row["energy"] = assigned_energy[gospel_uid]
                elif previous_version < ORACLE_STATE_SCHEMA_VERSION:
                    row["energy"] = maximum
                else:
                    row["energy"] = _safe_uint(
                        row.get("energy"), maximum, maximum
                    )
            normalized.append(row)
        return normalized

    def _changes_for_state(
        self,
        profile: Mapping[str, Any],
        state: Mapping[str, Any],
    ) -> Dict[str, Any]:
        changes: Dict[str, Any] = {}
        if profile.get(ORACLE_STATE_KEY) != state:
            changes[ORACLE_STATE_KEY] = copy.deepcopy(state)
        normalized_gospels = self._normalized_gospels(profile, state)
        if profile.get("gospels") != normalized_gospels:
            changes["gospels"] = normalized_gospels
        return changes

    def _rest_state(
        self,
        previous: Mapping[str, Any],
        by_uid: Mapping[int, Mapping[str, Any]],
    ) -> Dict[str, Any]:
        previous_rest = previous.get("rest")
        previous_rest = previous_rest if isinstance(previous_rest, Mapping) else {}
        previous_slots = previous_rest.get("slots")
        previous_slots = previous_slots if isinstance(previous_slots, Mapping) else {}

        slots: Dict[str, int] = {}
        used_uids = set()
        for raw_slot, raw_uid in previous_slots.items():
            slot = _safe_uint(raw_slot, 6, 5)
            gospel_uid = _safe_uint(raw_uid, 0, UINT64_MAX)
            if slot not in range(6) or gospel_uid not in by_uid:
                continue
            if gospel_uid in used_uids:
                continue
            slots[str(slot)] = gospel_uid
            used_uids.add(gospel_uid)
        return {"slots": dict(sorted(slots.items(), key=lambda item: int(item[0])))}

    def _fishing_attribute_state(self, previous: Mapping[str, Any]) -> Dict[str, Any]:
        """Normalize the account cache required by FishTankView.RefreshCost.

        Fish ownership and live fishing pools intentionally remain empty here:
        the fish tank can render an empty collection safely, while a missing
        ``FishingAttribute`` object crashes before that empty state is shown.
        """

        raw = previous.get("fishing_attribute")
        raw = raw if isinstance(raw, Mapping) else {}
        result: Dict[str, Any] = {
            "schema_version": FISHING_ATTRIBUTE_SCHEMA_VERSION,
        }
        for state_name, (_, _config_id, minimum, maximum) in (
            FISHING_ATTRIBUTE_CONFIG.items()
        ):
            value = _safe_uint(raw.get(state_name), minimum, UINT32_MAX)
            result[state_name] = value if minimum <= value <= maximum else minimum
        result["scene_ids"] = _normalized_config_ids(
            raw.get("scene_ids"), FISHING_SCENE_IDS
        )
        result["bait_ids"] = _normalized_config_ids(
            raw.get("bait_ids"), FISHING_BAIT_IDS
        )
        return result

    def _farm_state(self, previous: Any = None) -> Dict[str, Any]:
        # The dedicated farm layer owns paid planting/maturity/harvest state.
        # Routine Oracle normalization must not turn those plots back into
        # the former empty display-only snapshot on every login or work query.
        if isinstance(previous, Mapping) and previous.get("schema_version"):
            return copy.deepcopy(dict(previous))
        return {
            "farmer_level_config_id": FARMER_LEVEL_CONFIG_ID,
            "farmer_level": FARMER_LEVEL,
            "current_temperature": FARM_CURRENT_TEMPERATURE,
            "temperature_parameter_id": FARM_TEMPERATURE_PARAMETER_ID,
            "available_fertilizer_num": FARM_FERTILIZER_LIMIT,
            "trusteeship": 0,
            "lands": [
                {"land_id": land_id, "seed_id": 0, "harvest_value": 0}
                for land_id in FARM_LAND_IDS
            ],
        }

    def _race_state(self, previous: Mapping[str, Any]) -> Dict[str, Any]:
        previous_race = previous.get("race")
        previous_race = previous_race if isinstance(previous_race, Mapping) else {}
        raw_remaining = previous_race.get("remaining_bets")
        remaining_bets = (
            self.catalog.race_bet_limit
            if raw_remaining is None
            else _safe_uint(
                raw_remaining,
                self.catalog.race_bet_limit,
                self.catalog.race_bet_limit,
            )
        )
        # The server-side formula is absent from the runtime.  Four positive,
        # rank-indexed payouts are the smallest safe closure because
        # HorseRace indexes Profits[rank - 1] for ranks 1..4.  The values are
        # anchored to a_global[9990188]'s verified 500-item base stake.
        profits = [
            self.catalog.race_base_stake * multiplier
            for multiplier in range(RACE_REWARD_RANK_COUNT, 0, -1)
        ]
        return {
            "remaining_bets": remaining_bets,
            "horse_ids": list(RACE_HORSE_IDS),
            "profits": profits,
        }

    def make_state(self, profile: Mapping[str, Any]) -> Dict[str, Any]:
        """Normalize and settle persistent work sessions without mutating input."""

        by_id, by_uid = self._account_gospels(profile)
        previous = profile.get(ORACLE_STATE_KEY)
        previous = previous if isinstance(previous, Mapping) else {}
        previous_version = self._previous_schema_version(profile)
        previous_work = previous.get("work")
        now = self._now()

        work: Dict[str, Dict[str, Any]] = {}
        for building_id in REQUIRED_WORK_BUILDING_IDS:
            compatible_ids = self.catalog.gospel_ids_by_building[building_id]
            source = _mapping_lookup(previous_work, building_id)
            source = source if isinstance(source, Mapping) else {}
            saved_uid = _safe_uint(source.get("gospel_uid"), 0, UINT64_MAX)
            saved_gospel = by_uid.get(saved_uid)
            saved_gospel_id = (
                _safe_uint(saved_gospel.get("id"), 0, UINT32_MAX)
                if saved_gospel is not None
                else 0
            )
            if saved_gospel_id in compatible_ids:
                gospel = saved_gospel
            elif not profile.get("player_progression"):
                gospel = next(
                    (
                        by_id[gospel_id]
                        for gospel_id in compatible_ids
                        if gospel_id in by_id
                    ),
                    None,
                )
            else:
                # Acquiring a character does not automatically assign them
                # to a job. A newcomer may own no eligible worker yet.
                gospel = None
            if gospel is None:
                work[str(building_id)] = {
                    "workplace_id": building_id,
                    "building_type": self.catalog.building_types[building_id],
                    "gospel_id": 0, "gospel_uid": 0, "remaining_energy": 0,
                    "is_valid": 0, "session_sequence": _safe_uint(source.get("session_sequence"), 0, UINT64_MAX),
                    "session": None,
                    "session_history": copy.deepcopy(source.get("session_history", [])),
                    "pending_reward_count": _safe_uint(source.get("pending_reward_count"), 0, UINT32_MAX),
                    "pending_rewards": copy.deepcopy(source.get("pending_rewards", [])),
                    "earnings": copy.deepcopy(source.get("earnings", [])),
                    "claim_sequence": _safe_uint(source.get("claim_sequence"), 0, UINT64_MAX),
                    "claim_history": copy.deepcopy(source.get("claim_history", [])),
                }
                continue
            gospel_id = _safe_uint(gospel.get("id"), 0, UINT32_MAX)
            gospel_uid = _safe_uint(gospel.get("uid"), 0, UINT64_MAX)
            maximum_energy = self.catalog.work_energy_by_gospel[gospel_id]
            same_saved_worker = (
                saved_gospel_id == gospel_id and saved_uid == gospel_uid
            )
            remaining_energy = maximum_energy
            if previous_version >= 4 and same_saved_worker:
                remaining_energy = _safe_uint(
                    source.get("remaining_energy"),
                    maximum_energy,
                    maximum_energy,
                )

            row: Dict[str, Any] = {
                "workplace_id": building_id,
                "building_type": self.catalog.building_types[building_id],
                "gospel_id": gospel_id,
                "gospel_uid": gospel_uid,
                "remaining_energy": remaining_energy,
                "is_valid": 1,
                "session_sequence": 0,
                "session": None,
                "session_history": [],
                "pending_reward_count": 0,
                "pending_rewards": [],
                "earnings": [],
                "claim_sequence": 0,
                "claim_history": [],
            }

            if previous_version < ORACLE_STATE_SCHEMA_VERSION and source:
                row["is_valid"] = _safe_uint(source.get("is_valid"), 1, 1)

            if previous_version >= ORACLE_STATE_SCHEMA_VERSION and same_saved_worker:
                row["is_valid"] = _safe_uint(source.get("is_valid"), 0, 1)
                row["session_sequence"] = _safe_uint(
                    source.get("session_sequence"), 0, UINT64_MAX
                )
                row["pending_reward_count"] = _safe_uint(
                    source.get("pending_reward_count"), 0, UINT32_MAX
                )
                row["pending_rewards"] = [
                    reward.as_state()
                    for reward in self._rewards_from_state(
                        source.get("pending_rewards"),
                        f"oracle work[{building_id}].pending_rewards",
                    )
                ]
                raw_session_history = source.get("session_history")
                if isinstance(raw_session_history, list):
                    row["session_history"] = copy.deepcopy(raw_session_history)
                raw_claim_history = source.get("claim_history")
                if isinstance(raw_claim_history, list):
                    row["claim_history"] = copy.deepcopy(raw_claim_history)
                row["claim_sequence"] = _safe_uint(
                    source.get("claim_sequence"), 0, UINT64_MAX
                )

                raw_earnings = source.get("earnings")
                if raw_earnings is not None and not isinstance(raw_earnings, list):
                    raise OracleProfileError(
                        f"oracle work[{building_id}].earnings must be a list"
                    )
                for index, raw_earning in enumerate(raw_earnings or []):
                    if not isinstance(raw_earning, Mapping):
                        raise OracleProfileError(
                            f"oracle work[{building_id}].earnings[{index}] "
                            "must be a mapping"
                        )
                    earning_rewards = self._rewards_from_state(
                        raw_earning.get("reward_list"),
                        f"oracle work[{building_id}].earnings[{index}].reward_list",
                    )
                    row["earnings"].append(
                        {
                            "session_id": _safe_uint(
                                raw_earning.get("session_id"), 0, UINT64_MAX
                            ),
                            "gospel_uid": _safe_uint(
                                raw_earning.get("gospel_uid"), 0, UINT64_MAX
                            ),
                            "work_time": _safe_uint(
                                raw_earning.get("work_time"), 0, UINT32_MAX
                            ),
                            "fondness": _safe_uint(
                                raw_earning.get("fondness"), 0, UINT32_MAX
                            ),
                            "reward_list": [
                                reward.as_state() for reward in earning_rewards
                            ],
                        }
                    )
                row["session"] = self._normalize_session(
                    profile,
                    building_id,
                    gospel_id,
                    gospel_uid,
                    remaining_energy,
                    source,
                    now,
                )

            active = row["is_valid"] == 1
            session = row.get("session")
            if active and not isinstance(session, Mapping):
                sequence = max(1, int(row["session_sequence"]) + 1)
                row["session_sequence"] = sequence
                row["session"] = self._new_work_session(
                    profile,
                    building_id,
                    gospel_id,
                    gospel_uid,
                    remaining_energy,
                    sequence,
                    now,
                )
            elif active and _safe_uint(session.get("ended_at"), 0, UINT64_MAX):
                row["is_valid"] = 0
            elif not active and isinstance(session, MutableMapping):
                if _safe_uint(session.get("ended_at"), 0, UINT64_MAX) == 0:
                    session["ended_at"] = max(
                        _safe_uint(session.get("started_at"), now, UINT64_MAX),
                        _safe_uint(
                            session.get("last_settled_at"), now, UINT64_MAX
                        ),
                    )

            if row["is_valid"] == 1:
                self._settle_work_row(row, now)
            work[str(building_id)] = row

        state = copy.deepcopy(dict(previous))
        state.update(
            {
                "schema_version": ORACLE_STATE_SCHEMA_VERSION,
                "catalog_version": self.content_catalog.version,
                "work": work,
                "rest": self._rest_state(previous, by_uid),
                "farm": self._farm_state(previous.get("farm")),
                "race": self._race_state(previous),
                "fishing_attribute": self._fishing_attribute_state(previous),
            }
        )
        return state

    def changes_for(self, profile: Mapping[str, Any]) -> Dict[str, Any]:
        """Return the shallow merge needed to persist normalized Oracle data."""

        state = self.make_state(profile)
        return self._changes_for_state(profile, state)

    def seed_profile(self, profile: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
        """Seed the same state eagerly, in place and idempotently."""

        if not isinstance(profile, MutableMapping):
            raise TypeError("profile must be a mutable mapping")
        changes = self.changes_for(profile)
        profile.update(changes)
        return profile

    def make_work_datas_ret(
        self,
        profile: Mapping[str, Any],
        state: Optional[Mapping[str, Any]] = None,
    ) -> bytes:
        """Encode assigned workers; an empty workplace has no Gospel object."""

        actual_role_id = _safe_uint(profile.get("role_id"), 0, UINT64_MAX)
        if actual_role_id <= 0:
            raise OracleProfileError("profile.role_id must be a positive uint64")
        normalized = self.make_state(profile) if state is None else state
        work_state = normalized.get("work")
        if not isinstance(work_state, Mapping):
            raise OracleProfileError("oracle work state must be a mapping")

        now = self._now()
        wire_work: list[Dict[str, Any]] = []
        for building_id in REQUIRED_WORK_BUILDING_IDS:
            row = _mapping_lookup(work_state, building_id)
            if not isinstance(row, Mapping):
                raise OracleProfileError(
                    f"oracle work state is missing building {building_id}"
                )
            if not row.get("gospel_uid") and not row.get("is_valid"):
                continue
            wire_work.append(self._work_wire(row, now))
        return self.codec.encode(
            "SC_WorkDatasRet",
            {"RoleId": actual_role_id, "WorkList": wire_work},
        )

    def _work_wire(self, row: Mapping[str, Any], now: int) -> Dict[str, Any]:
        building_id = _safe_uint(row.get("workplace_id"), 0, UINT32_MAX)
        gospel_id = _safe_uint(row.get("gospel_id"), 0, UINT32_MAX)
        gospel_uid = _safe_uint(row.get("gospel_uid"), 0, UINT64_MAX)
        if (
            building_id not in self.catalog.building_types
            or gospel_id not in self.catalog.work_energy_by_gospel
            or gospel_uid <= 0
        ):
            raise OracleProfileError("oracle work row has invalid worker identity")
        rewards = self._rewards_from_state(
            row.get("pending_rewards"),
            f"oracle work[{building_id}].pending_rewards",
        )
        earnings_wire = []
        raw_earnings = row.get("earnings")
        if raw_earnings is not None and not isinstance(raw_earnings, list):
            raise OracleProfileError(
                f"oracle work[{building_id}].earnings must be a list"
            )
        session = row.get("session")
        session_id = (
            _safe_uint(session.get("session_id"), 0, UINT64_MAX)
            if isinstance(session, Mapping)
            else 0
        )
        live_worked = self._live_worked_seconds(row, now)
        live_earning_seen = False
        for raw in raw_earnings or []:
            if not isinstance(raw, Mapping):
                raise OracleProfileError("oracle earning must be a mapping")
            earning_session_id = _safe_uint(
                raw.get("session_id"), 0, UINT64_MAX
            )
            work_time = _safe_uint(raw.get("work_time"), 0, UINT32_MAX)
            fondness = _safe_uint(raw.get("fondness"), 0, UINT32_MAX)
            if earning_session_id == session_id and isinstance(session, Mapping):
                live_earning_seen = True
                work_time = max(work_time, live_worked)
                favor_rate = _safe_uint(
                    session.get("favor_rate"), 0, UINT32_MAX
                )
                fondness = min(UINT32_MAX, favor_rate * work_time // 10_000)
            earning_rewards = self._rewards_from_state(
                raw.get("reward_list"), "oracle earning.reward_list"
            )
            earnings_wire.append(
                {
                    "GospelUId": _safe_uint(
                        raw.get("gospel_uid"), gospel_uid, UINT64_MAX
                    ),
                    "WorkTime": work_time,
                    "Fondness": fondness,
                    "RewardList": [reward.as_wire() for reward in earning_rewards],
                }
            )
        if (
            isinstance(session, Mapping)
            and live_worked > 0
            and not live_earning_seen
        ):
            favor_rate = _safe_uint(session.get("favor_rate"), 0, UINT32_MAX)
            earnings_wire.append(
                {
                    "GospelUId": gospel_uid,
                    "WorkTime": live_worked,
                    "Fondness": min(
                        UINT32_MAX, favor_rate * live_worked // 10_000
                    ),
                    "RewardList": [],
                }
            )
        pending_count = _safe_uint(
            row.get("pending_reward_count"), 0, UINT32_MAX
        )
        return {
            "WorkplaceId": building_id,
            "GospelUId": gospel_uid,
            "Energy": self._remaining_work_energy(row, gospel_id),
            "RemainTime": self._work_remain_time(row, now),
            "GospelRewardNum": pending_count,
            "RewardNum": pending_count,
            "RewardList": [reward.as_wire() for reward in rewards],
            "Earnings": earnings_wire,
            "IsValid": _safe_uint(row.get("is_valid"), 0, 1),
        }

    def make_fish_datas_ret(self) -> bytes:
        """Encode the smallest safe fish snapshot for an empty local aquarium."""

        return self.codec.encode(
            "SC_FishDatasRet",
            {"FishList": [], "FishrecordList": [], "SceneMap": {}},
        )

    def make_fishing_attribute_notification(
        self, state: Mapping[str, Any]
    ) -> bytes:
        """Encode command 521, which initializes GameData.user's fish cache."""

        attribute = state.get("fishing_attribute")
        if not isinstance(attribute, Mapping):
            raise OracleProfileError("oracle fishing attribute must be a mapping")
        wire_attribute = {
            wire_name: _safe_uint(attribute.get(state_name), minimum, maximum)
            for state_name, (wire_name, _config_id, minimum, maximum) in (
                FISHING_ATTRIBUTE_CONFIG.items()
            )
        }
        wire_attribute["SceneIdList"] = _normalized_config_ids(
            attribute.get("scene_ids"), FISHING_SCENE_IDS
        )
        wire_attribute["BaitIdList"] = _normalized_config_ids(
            attribute.get("bait_ids"), FISHING_BAIT_IDS
        )
        return self.codec.encode(
            "Notify_UpdateFishingAttribute",
            {"Attr": wire_attribute},
        )

    def state_with_settings(
        self,
        profile: Mapping[str, Any],
        settings: Mapping[int, int],
    ) -> Dict[str, Any]:
        """Start all requested workers as one validated state transition."""

        state = self.make_state(profile)
        now = self._now()
        self._start_work_settings(profile, state, settings, now)
        return state

    def _profile_gospel_energy(
        self,
        profile: Mapping[str, Any],
        gospel: Mapping[str, Any],
    ) -> int:
        gospel_id = _safe_uint(gospel.get("id"), 0, UINT32_MAX)
        maximum = self.catalog.work_energy_by_gospel[gospel_id]
        if self._previous_schema_version(profile) < ORACLE_STATE_SCHEMA_VERSION:
            return maximum
        return _safe_uint(gospel.get("energy"), maximum, maximum)

    def _start_work_settings(
        self,
        profile: Mapping[str, Any],
        state: MutableMapping[str, Any],
        settings: Mapping[int, int],
        now: int,
    ) -> None:
        work = state.get("work")
        if not isinstance(work, MutableMapping):
            raise OracleProfileError("oracle work state must be mutable")
        _, by_uid = self._account_gospels(profile)
        if not settings:
            raise OracleWorkError(
                ERROR_MESSAGE_DATA_ILLEGAL, "work settings must not be empty"
            )
        if len(set(settings.values())) != len(settings):
            raise OracleWorkError(
                ERROR_GOSPEL_ALREADY_WORKING,
                "one GospelUId cannot work in multiple buildings",
            )

        rest = state.get("rest")
        slots = rest.get("slots") if isinstance(rest, Mapping) else {}
        resting_uids = {
            _safe_uint(value, 0, UINT64_MAX)
            for value in slots.values()
        } if isinstance(slots, Mapping) else set()
        active_by_uid: Dict[int, int] = {}
        for existing_building in REQUIRED_WORK_BUILDING_IDS:
            existing = _mapping_lookup(work, existing_building)
            if not isinstance(existing, Mapping):
                raise OracleProfileError(
                    f"oracle work state is missing building {existing_building}"
                )
            if _safe_uint(existing.get("is_valid"), 0, 1) == 1:
                active_by_uid[
                    _safe_uint(existing.get("gospel_uid"), 0, UINT64_MAX)
                ] = existing_building

        validated: Dict[int, Tuple[int, int]] = {}
        for raw_building_id, raw_gospel_uid in settings.items():
            building_id = _safe_uint(raw_building_id, 0, UINT32_MAX)
            gospel_uid = _safe_uint(raw_gospel_uid, 0, UINT64_MAX)
            if building_id not in self.catalog.building_types:
                raise OracleWorkError(
                    ERROR_MESSAGE_DATA_ILLEGAL,
                    f"unknown Oracle building {building_id}",
                )
            gospel = by_uid.get(gospel_uid)
            if gospel is None:
                raise OracleWorkError(
                    ERROR_GOSPEL_NOT_FOUND,
                    f"building {building_id} references unknown GospelUId "
                    f"{gospel_uid}",
                )
            gospel_id = _safe_uint(gospel.get("id"), 0, UINT32_MAX)
            expected_type = self.catalog.building_types[building_id]
            actual_type = self.catalog.work_type_by_gospel.get(gospel_id)
            if actual_type != expected_type:
                raise OracleWorkError(
                    ERROR_MESSAGE_DATA_ILLEGAL,
                    f"Gospel {gospel_id} has a_gospel.work={actual_type}, "
                    f"building {building_id} requires {expected_type}",
                )
            if gospel_uid in resting_uids:
                raise OracleWorkError(
                    ERROR_GOSPEL_IN_RESTING_CANNOT_WORK,
                    f"GospelUId {gospel_uid} is currently resting",
                )
            current = _mapping_lookup(work, building_id)
            if not isinstance(current, Mapping):
                raise OracleProfileError(
                    f"oracle work state is missing building {building_id}"
                )
            current_uid = _safe_uint(
                current.get("gospel_uid"), 0, UINT64_MAX
            )
            current_active = _safe_uint(current.get("is_valid"), 0, 1) == 1
            occupied = active_by_uid.get(gospel_uid)
            if current_active and current_uid != gospel_uid:
                raise OracleWorkError(
                    ERROR_GOSPEL_ALREADY_WORKING,
                    f"building {building_id} already has active GospelUId "
                    f"{current_uid}",
                )
            if occupied is not None and occupied != building_id:
                raise OracleWorkError(
                    ERROR_GOSPEL_ALREADY_WORKING,
                    f"GospelUId {gospel_uid} is already working at {occupied}",
                )
            energy = (
                self._remaining_work_energy(current, gospel_id)
                if current_uid == gospel_uid
                else self._profile_gospel_energy(profile, gospel)
            )
            if energy <= 0:
                raise OracleWorkError(
                    ERROR_GOSPEL_TO_WORK_NO_ENERGY,
                    f"GospelUId {gospel_uid} has no work energy",
                )
            self._building_work_definition(profile, building_id)
            validated[building_id] = (gospel_id, energy)

        for building_id, gospel_uid in settings.items():
            row = _mapping_lookup(work, building_id)
            if not isinstance(row, MutableMapping):
                raise OracleProfileError(
                    f"oracle work state is missing mutable building {building_id}"
                )
            if (
                _safe_uint(row.get("is_valid"), 0, 1) == 1
                and _safe_uint(row.get("gospel_uid"), 0, UINT64_MAX)
                == gospel_uid
            ):
                continue
            old_session = row.get("session")
            if isinstance(old_session, Mapping):
                history = row.get("session_history")
                history = history if isinstance(history, list) else []
                old_session_id = _safe_uint(
                    old_session.get("session_id"), 0, UINT64_MAX
                )
                if not any(
                    isinstance(saved, Mapping)
                    and _safe_uint(saved.get("session_id"), 0, UINT64_MAX)
                    == old_session_id
                    for saved in history
                ):
                    history.append(copy.deepcopy(dict(old_session)))
                row["session_history"] = history
            gospel_id, energy = validated[building_id]
            sequence = _safe_uint(
                row.get("session_sequence"), 0, UINT64_MAX - 1
            ) + 1
            row.update(
                {
                    "gospel_id": gospel_id,
                    "gospel_uid": gospel_uid,
                    "remaining_energy": energy,
                    "is_valid": 1,
                    "session_sequence": sequence,
                    "session": self._new_work_session(
                        profile,
                        building_id,
                        gospel_id,
                        gospel_uid,
                        energy,
                        sequence,
                        now,
                    ),
                }
            )

    def state_with_end_settings(
        self,
        profile: Mapping[str, Any],
        settings: Mapping[int, int],
    ) -> Dict[str, Any]:
        state = self.make_state(profile)
        now = self._now()
        self._end_work_settings(state, settings, now)
        return state

    def _end_work_settings(
        self,
        state: MutableMapping[str, Any],
        settings: Mapping[int, int],
        now: int,
    ) -> None:
        work = state.get("work")
        if not isinstance(work, MutableMapping):
            raise OracleProfileError("oracle work state must be mutable")
        if not settings:
            raise OracleWorkError(
                ERROR_MESSAGE_DATA_ILLEGAL, "work settings must not be empty"
            )
        for raw_building_id, raw_gospel_uid in settings.items():
            building_id = _safe_uint(raw_building_id, 0, UINT32_MAX)
            gospel_uid = _safe_uint(raw_gospel_uid, 0, UINT64_MAX)
            if building_id not in self.catalog.building_types:
                raise OracleWorkError(
                    ERROR_MESSAGE_DATA_ILLEGAL,
                    f"unknown Oracle building {building_id}",
                )
            row = _mapping_lookup(work, building_id)
            if not isinstance(row, Mapping):
                raise OracleProfileError(
                    f"oracle work state is missing building {building_id}"
                )
            current_uid = _safe_uint(row.get("gospel_uid"), 0, UINT64_MAX)
            session = row.get("session")
            session_uid = (
                _safe_uint(session.get("worker_uid"), 0, UINT64_MAX)
                if isinstance(session, Mapping)
                else 0
            )
            if current_uid != gospel_uid or session_uid != gospel_uid:
                raise OracleWorkError(
                    ERROR_MESSAGE_DATA_ILLEGAL,
                    f"GospelUId {gospel_uid} is not assigned to building "
                    f"{building_id}",
                )

        for building_id in settings:
            row = _mapping_lookup(work, building_id)
            if not isinstance(row, MutableMapping):
                raise OracleProfileError(
                    f"oracle work state is missing mutable building {building_id}"
                )
            if _safe_uint(row.get("is_valid"), 0, 1) == 0:
                continue
            self._settle_work_row(row, now, force=True)
            session = row.get("session")
            if not isinstance(session, MutableMapping):
                raise OracleProfileError("active Oracle worker has no session")
            if _safe_uint(session.get("ended_at"), 0, UINT64_MAX) == 0:
                ended_at = max(
                    now,
                    _safe_uint(session.get("started_at"), now, UINT64_MAX),
                )
                session["ended_at"] = ended_at
                session["last_settled_at"] = ended_at
            row["is_valid"] = 0

    def state_with_rest(
        self,
        profile: Mapping[str, Any],
        gospel_map: Mapping[int, int],
        *,
        start: bool,
    ) -> Dict[str, Any]:
        """Apply a full rest-start snapshot or remove the echoed end slots."""

        state = self.make_state(profile)
        rest = state.get("rest")
        if not isinstance(rest, MutableMapping):  # pragma: no cover - own shape
            raise OracleProfileError("oracle rest state must be mutable")
        slots = rest.get("slots")
        if not isinstance(slots, MutableMapping):  # pragma: no cover - own shape
            raise OracleProfileError("oracle rest slots must be mutable")
        _, by_uid = self._account_gospels(profile)

        normalized: Dict[int, int] = {}
        for raw_slot, raw_uid in gospel_map.items():
            slot = _safe_uint(raw_slot, 6, 5)
            gospel_uid = _safe_uint(raw_uid, 0, UINT64_MAX)
            if slot not in range(6):
                raise OracleProfileError(f"rest slot {raw_slot!r} is outside 0..5")
            if gospel_uid not in by_uid:
                raise OracleProfileError(
                    f"rest slot {slot} references unknown GospelUId {gospel_uid}"
                )
            normalized[slot] = gospel_uid
        if not normalized:
            raise OracleProfileError("rest GospelMap must not be empty")
        if len(set(normalized.values())) != len(normalized):
            raise OracleProfileError(
                "one GospelUId cannot rest in multiple slots"
            )

        if start:
            work = state.get("work")
            if not isinstance(work, Mapping):
                raise OracleProfileError("oracle work state must be a mapping")
            active_uids = {
                _safe_uint(row.get("gospel_uid"), 0, UINT64_MAX)
                for building_id in REQUIRED_WORK_BUILDING_IDS
                for row in [_mapping_lookup(work, building_id)]
                if isinstance(row, Mapping)
                and _safe_uint(row.get("is_valid"), 0, 1) == 1
            }
            conflict = active_uids & set(normalized.values())
            if conflict:
                gospel_uid = min(conflict)
                raise OracleWorkError(
                    ERROR_GOSPEL_IN_WORKING_CANNOT_REST,
                    f"GospelUId {gospel_uid} is currently working",
                )
            # NetMessageLogic.SendCS_RestStart serializes the complete non-zero
            # RestFyUid array, so replacing the snapshot mirrors the runtime.
            rest["slots"] = {
                str(slot): gospel_uid
                for slot, gospel_uid in sorted(normalized.items())
            }
        else:
            for slot in normalized:
                slots.pop(str(slot), None)
                slots.pop(slot, None)
        return state

    def make_rest_ret(
        self,
        response_id: int,
        profile: Mapping[str, Any],
        gospel_map: Mapping[int, int],
        *,
        error_code: int = ERROR_SUCCESS,
    ) -> bytes:
        message_name = (
            "SC_RestStartRet"
            if response_id == SC_REST_START_RET
            else "SC_RestEndRet"
        )
        return self.codec.encode(
            message_name,
            {
                "Errorcode": error_code,
                "RoleId": _safe_uint(profile.get("role_id"), 0, UINT64_MAX),
                "GospelMap": dict(gospel_map),
            },
        )

    def make_farm_datas_ret(
        self,
        profile: Mapping[str, Any],
        friend_id: int,
        state: Mapping[str, Any],
    ) -> bytes:
        farm = state.get("farm")
        if not isinstance(farm, Mapping):
            raise OracleProfileError("oracle farm state must be a mapping")
        farmer_level = _safe_uint(farm.get("farmer_level"), 0, UINT32_MAX)
        if farmer_level < 1:
            raise OracleProfileError("oracle farm FarmerLvl must be positive")
        lands = farm.get("lands")
        if not isinstance(lands, list):
            raise OracleProfileError("oracle farm lands must be a list")
        land_ids = tuple(
            _safe_uint(row.get("land_id"), 0, UINT32_MAX)
            for row in lands
            if isinstance(row, Mapping)
        )
        if land_ids != FARM_LAND_IDS:
            raise OracleProfileError(
                f"oracle farm land closure must be {FARM_LAND_IDS}"
            )

        farm_wire = {
            "FarmerLvl": farmer_level,
            "CurrentTemperature": _safe_uint(
                farm.get("current_temperature"),
                FARM_CURRENT_TEMPERATURE,
                UINT32_MAX,
            ),
            "LightLvl": 1,
            "BlowLvl": 1,
            "AvailableFertilizerNum": _safe_uint(
                farm.get("available_fertilizer_num"),
                FARM_FERTILIZER_LIMIT,
                UINT32_MAX,
            ),
            "Trusteeship": _safe_uint(farm.get("trusteeship"), 0, UINT32_MAX),
            "PresetTemperature": FARM_CURRENT_TEMPERATURE,
            "PresetLightLvl": 1,
            "PresetHumidity": 1,
            "PresetAutoHarvest": 0,
            "Signature": "",
        }
        farm_land_wire = [{"LandId": land_id} for land_id in FARM_LAND_IDS]
        return self.codec.encode(
            "SC_RequestFarmDatasRet",
            {
                "Sender": _safe_uint(profile.get("role_id"), 0, UINT64_MAX),
                "FriendId": friend_id,
                "Farm": farm_wire,
                "FarmLandList": farm_land_wire,
                "FarmBuffList": [],
                "FarmLogList": [],
                "GoodsList": [],
                "GoodsRemainSecTime": 0,
            },
        )

    def make_farmland_info_ret(self, state: Mapping[str, Any]) -> bytes:
        farm = state.get("farm")
        if not isinstance(farm, Mapping):
            raise OracleProfileError("oracle farm state must be a mapping")
        lands = farm.get("lands")
        if not isinstance(lands, list):
            raise OracleProfileError("oracle farm lands must be a list")
        empty_num = 0
        can_harvest_num = 0
        for row in lands:
            if not isinstance(row, Mapping):
                raise OracleProfileError("oracle farm land must be a mapping")
            seed_id = _safe_uint(row.get("seed_id"), 0, UINT32_MAX)
            harvest_value = _safe_uint(row.get("harvest_value"), 0, UINT32_MAX)
            if seed_id == 0:
                empty_num += 1
            elif harvest_value > 0:
                can_harvest_num += 1
        return self.codec.encode(
            "SC_RequestFarmlandInfoRet",
            {
                "CanHarvestNum": can_harvest_num,
                "EmptyNum": empty_num,
                "Trusteeship": _safe_uint(
                    farm.get("trusteeship"), 0, UINT32_MAX
                ),
            },
        )

    def make_race_horse_datas_ret(self, state: Mapping[str, Any]) -> bytes:
        race = state.get("race")
        if not isinstance(race, Mapping):
            raise OracleProfileError("oracle race state must be a mapping")
        horse_ids = tuple(race.get("horse_ids", ()))
        if horse_ids != RACE_HORSE_IDS:
            raise OracleProfileError(
                f"oracle race horse closure must be {RACE_HORSE_IDS}"
            )
        raw_profits = race.get("profits")
        if not isinstance(raw_profits, list):
            raise OracleProfileError("oracle race profits must be a list")
        profits = [
            _safe_uint(value, 0, UINT32_MAX)
            for value in raw_profits
        ]
        if len(profits) != RACE_REWARD_RANK_COUNT or any(
            value <= 0 for value in profits
        ):
            raise OracleProfileError(
                "oracle race profits must cover four positive rank awards"
            )
        horse_list = [
            {
                "Id": horse_id,
                "Status": RACE_HORSE_STATUS_NORMAL,
                "SpeedLvl": RACE_HORSE_SPEED_LEVEL,
                "LuckLvl": RACE_HORSE_SPEED_LEVEL,
                "Profits": profits,
            }
            for horse_id in horse_ids
        ]
        return self.codec.encode(
            "SC_RequestRaceHorseDatasRet",
            {
                "HorseList": horse_list,
                "TodayBetRaceHorseTimes": _safe_uint(
                    race.get("remaining_bets"),
                    self.catalog.race_bet_limit,
                    self.catalog.race_bet_limit,
                ),
            },
        )

    def make_work_start_batch_ret(
        self,
        profile: Mapping[str, Any],
        settings: Mapping[int, int],
        state: Mapping[str, Any],
    ) -> bytes:
        """Echo the changed workers so WorkData.UpdateWorkInfo can refresh UI."""

        work = state.get("work")
        if not isinstance(work, Mapping):
            raise OracleProfileError("oracle work state must be a mapping")
        now = self._now()
        result: Dict[int, Dict[str, int]] = {}
        for building_id in settings:
            row = _mapping_lookup(work, building_id)
            if not isinstance(row, Mapping):
                raise OracleProfileError(
                    f"oracle work state is missing building {building_id}"
                )
            gospel_uid = _safe_uint(row.get("gospel_uid"), 0, UINT64_MAX)
            gospel_id = _safe_uint(row.get("gospel_id"), 0, UINT32_MAX)
            result[building_id] = {
                "WorkspaceId": building_id,
                "GospelUid": gospel_uid,
                "Energy": self._remaining_work_energy(row, gospel_id),
                "RemainTime": self._work_remain_time(row, now),
            }
        return self.codec.encode(
            "SC_WorkStartBatchRet",
            {"ErrorCode": ERROR_SUCCESS, "Result": result},
        )

    def make_work_start_ret(
        self,
        profile: Mapping[str, Any],
        building_id: int,
        state: Mapping[str, Any],
        *,
        error_code: int = ERROR_SUCCESS,
        gospel_uid: int = 0,
    ) -> bytes:
        work = state.get("work")
        row = _mapping_lookup(work, building_id)
        if error_code == ERROR_SUCCESS and isinstance(row, Mapping):
            gospel_uid = _safe_uint(row.get("gospel_uid"), 0, UINT64_MAX)
            gospel_id = _safe_uint(row.get("gospel_id"), 0, UINT32_MAX)
            energy = self._remaining_work_energy(row, gospel_id)
            remain_time = self._work_remain_time(row, self._now())
        else:
            energy = 0
            remain_time = 0
        return self.codec.encode(
            "SC_WorkStartRet",
            {
                "Errorcode": error_code,
                "RoleId": _safe_uint(profile.get("role_id"), 0, UINT64_MAX),
                "WorkplaceId": building_id,
                "GospelUId": gospel_uid,
                "Energy": energy,
                "RemainTime": remain_time,
            },
        )

    def make_work_end_ret(
        self,
        profile: Mapping[str, Any],
        building_id: int,
        gospel_uid: int,
        *,
        error_code: int = ERROR_SUCCESS,
    ) -> bytes:
        return self.codec.encode(
            "SC_WorkEndRet",
            {
                "Errorcode": error_code,
                "RoleId": _safe_uint(profile.get("role_id"), 0, UINT64_MAX),
                "WorkplaceId": building_id,
                "GospelUId": gospel_uid,
            },
        )

    def make_work_end_batch_ret(
        self,
        settings: Mapping[int, int],
        *,
        error_code: int = ERROR_SUCCESS,
    ) -> bytes:
        result = {
            building_id: {
                "WorkplaceId": building_id,
                "GospelUid": gospel_uid,
            }
            for building_id, gospel_uid in settings.items()
        } if error_code == ERROR_SUCCESS else {}
        return self.codec.encode(
            "SC_WorkEndBatchRet",
            {"ErrorCode": error_code, "Result": result},
        )

    def make_take_work_reward_ret(
        self,
        profile: Mapping[str, Any],
        building_id: int,
        rewards: Sequence[WorkReward],
        *,
        error_code: int = ERROR_SUCCESS,
    ) -> bytes:
        return self.codec.encode(
            "SC_TakeWorkRwardRet",
            {
                "Errorcode": error_code,
                "RoleId": _safe_uint(profile.get("role_id"), 0, UINT64_MAX),
                "WorkplaceId": building_id,
                "RewardList": [reward.as_wire() for reward in rewards],
            },
        )

    def make_take_work_reward_all_ret(
        self,
        rewards: Sequence[WorkReward],
        *,
        error_code: int = ERROR_SUCCESS,
    ) -> bytes:
        return self.codec.encode(
            "SC_TakeWorkRewardAllRet",
            {
                "ErrorCode": error_code,
                "RewardList": [reward.as_wire() for reward in rewards],
            },
        )

    def _inventory_with_work_rewards(
        self,
        profile: Mapping[str, Any],
        rewards: Sequence[WorkReward],
    ) -> Dict[str, Dict[str, int]]:
        try:
            inventory = normalize_inventory(
                copy.deepcopy(profile.get("inventory", {}))
            )
        except InventoryContentError as exc:
            raise OracleWorkError(ERROR_MESSAGE_DATA_ILLEGAL, str(exc)) from exc
        for reward in self._aggregate_rewards(rewards):
            if reward.res_type != 4:
                raise OracleWorkError(
                    ERROR_MESSAGE_DATA_ILLEGAL,
                    f"unsupported Oracle reward type {reward.res_type}",
                )
            maximum = self.catalog.item_max_stacks.get(reward.item_id, 0)
            key = str(reward.item_id)
            previous = inventory.get(key)
            current = _safe_uint(
                previous.get("count") if isinstance(previous, Mapping) else 0,
                0,
                UINT32_MAX,
            )
            updated = current + reward.count
            if maximum <= 0 or updated > maximum or updated > UINT32_MAX:
                raise OracleWorkError(
                    ERROR_EXCEED_ITEM_LIMIT_COUNT,
                    f"item {reward.item_id} would exceed a_item.max_stack_num "
                    f"{maximum}",
                )
            inventory[key] = {
                "uid": (
                    _safe_uint(previous.get("uid"), 0, UINT64_MAX)
                    if isinstance(previous, Mapping)
                    else 0
                ) or stable_item_uid(reward.item_id),
                "id": reward.item_id,
                "count": updated,
                "para": (
                    _safe_uint(previous.get("para"), 0, UINT32_MAX)
                    if isinstance(previous, Mapping)
                    else 0
                ),
            }
        return {
            key: inventory[key]
            for key in sorted(inventory, key=lambda value: int(value))
        }

    def _claim_work_rewards(
        self,
        profile: Mapping[str, Any],
        state: MutableMapping[str, Any],
        building_ids: Sequence[int],
        now: int,
    ) -> Tuple[list[WorkReward], Dict[str, Dict[str, int]]]:
        work = state.get("work")
        if not isinstance(work, MutableMapping):
            raise OracleProfileError("oracle work state must be mutable")
        selected: list[Tuple[int, MutableMapping[str, Any], list[WorkReward]]] = []
        all_rewards: list[WorkReward] = []
        for building_id in building_ids:
            if building_id not in self.catalog.building_types:
                raise OracleWorkError(
                    ERROR_MESSAGE_DATA_ILLEGAL,
                    f"unknown Oracle building {building_id}",
                )
            row = _mapping_lookup(work, building_id)
            if not isinstance(row, MutableMapping):
                raise OracleProfileError(
                    f"oracle work state is missing building {building_id}"
                )
            pending_count = _safe_uint(
                row.get("pending_reward_count"), 0, UINT32_MAX
            )
            rewards = self._rewards_from_state(
                row.get("pending_rewards"),
                f"oracle work[{building_id}].pending_rewards",
            )
            if pending_count > 0 and not rewards:
                raise OracleProfileError(
                    f"oracle work[{building_id}] has a pending count without rewards"
                )
            if pending_count > 0:
                selected.append((building_id, row, rewards))
                all_rewards.extend(rewards)
        if not selected:
            raise OracleWorkError(
                ERROR_NO_REWARD_CAN_BE_TAKEN,
                "no Oracle work reward can be taken",
            )

        aggregated = self._aggregate_rewards(all_rewards)
        inventory = self._inventory_with_work_rewards(profile, aggregated)
        for building_id, row, rewards in selected:
            claim_sequence = _safe_uint(
                row.get("claim_sequence"), 0, UINT64_MAX - 1
            ) + 1
            history = row.get("claim_history")
            history = history if isinstance(history, list) else []
            history.append(
                {
                    "claim_id": claim_sequence,
                    "claimed_at": now,
                    "workplace_id": building_id,
                    "reward_count": _safe_uint(
                        row.get("pending_reward_count"), 0, UINT32_MAX
                    ),
                    "rewards": [reward.as_state() for reward in rewards],
                    "earnings": copy.deepcopy(row.get("earnings", [])),
                }
            )
            row["claim_sequence"] = claim_sequence
            row["claim_history"] = history
            row["pending_reward_count"] = 0
            row["pending_rewards"] = []
            row["earnings"] = []
        return aggregated, inventory

    def _work_data_error(
        self, profile: Mapping[str, Any], error_code: int, reason: str
    ) -> OracleResult:
        role_id = _safe_uint(profile.get("role_id"), 0, UINT64_MAX)
        body = self.codec.encode(
            "SC_WorkDatasRet",
            {"RoleId": role_id, "WorkList": []},
        )
        return OracleResult(
            SC_WORK_DATAS_RET,
            body,
            error_code=error_code,
            supported=False,
            reason=reason,
        )

    def make_invite_hero_to_scene_ret(
        self,
        error_code: int,
        scene_id: int,
        hero_uid: int,
    ) -> bytes:
        """Echo the three scalar fields consumed from response command 493."""

        return self.codec.encode(
            "SC_InviteHeroToSceneRet",
            {
                "Errorcode": error_code,
                "SceneId": scene_id,
                "HeroUId": hero_uid,
            },
        )

    def _batch_error(
        self,
        request_id: int,
        error_code: int,
        reason: str,
    ) -> OracleResult:
        response_id = RESPONSE_IDS[request_id]
        message_name = (
            "SC_WorkStartBatchRet"
            if request_id == CS_WORK_START_BATCH
            else "SC_WorkEndBatchRet"
        )
        return OracleResult(
            response_id,
            self.codec.encode(
                message_name,
                {"ErrorCode": error_code, "Result": {}},
            ),
            error_code=error_code,
            supported=False,
            reason=reason,
        )

    def handle(
        self,
        request_id: int,
        profile: Optional[Mapping[str, Any]],
        request_body: bytes = b"",
    ) -> Optional[OracleResult]:
        """Handle the Oracle endpoints owned by this state layer."""

        if request_id not in RESPONSE_IDS:
            return None
        account = profile if isinstance(profile, Mapping) else {}
        actual_role_id = _safe_uint(account.get("role_id"), 0, UINT64_MAX)

        if request_id == CS_INVITE_HERO_TO_SCENE:
            scene_id = 0
            hero_uid = 0
            try:
                scene_id, hero_uid, _ = _decode_invite_hero_to_scene(request_body)
            except ProtobufDecodeError as exc:
                return OracleResult(
                    SC_INVITE_HERO_TO_SCENE_RET,
                    self.make_invite_hero_to_scene_ret(
                        ERROR_MESSAGE_DATA_ILLEGAL,
                        scene_id,
                        hero_uid,
                    ),
                    error_code=ERROR_MESSAGE_DATA_ILLEGAL,
                    supported=False,
                    reason=str(exc),
                )

            if scene_id not in HERO_SCENE_IDS:
                return OracleResult(
                    SC_INVITE_HERO_TO_SCENE_RET,
                    self.make_invite_hero_to_scene_ret(
                        ERROR_CLIENT_REQUEST_PARAM,
                        scene_id,
                        hero_uid,
                    ),
                    error_code=ERROR_CLIENT_REQUEST_PARAM,
                    supported=False,
                    reason=f"unknown DailyCity Hero scene {scene_id}",
                )

            raw_heroes = account.get("heroes")
            matching_indexes = []
            if isinstance(raw_heroes, list) and hero_uid > 0:
                matching_indexes = [
                    index
                    for index, row in enumerate(raw_heroes)
                    if isinstance(row, Mapping)
                    and _safe_uint(row.get("uid"), 0, UINT64_MAX) == hero_uid
                ]
            if not matching_indexes:
                return OracleResult(
                    SC_INVITE_HERO_TO_SCENE_RET,
                    self.make_invite_hero_to_scene_ret(
                        ERROR_HERO_NOT_FOUND,
                        scene_id,
                        hero_uid,
                    ),
                    error_code=ERROR_HERO_NOT_FOUND,
                    supported=False,
                    reason=f"hero UID {hero_uid} is not owned by this account",
                )
            if len(matching_indexes) != 1:
                return OracleResult(
                    SC_INVITE_HERO_TO_SCENE_RET,
                    self.make_invite_hero_to_scene_ret(
                        ERROR_MESSAGE_DATA_ILLEGAL,
                        scene_id,
                        hero_uid,
                    ),
                    error_code=ERROR_MESSAGE_DATA_ILLEGAL,
                    supported=False,
                    reason=f"profile has duplicate HeroUId {hero_uid}",
                )

            target_index = matching_indexes[0]
            current_scene_id = _safe_uint(
                raw_heroes[target_index].get("scene_id"),  # type: ignore[union-attr]
                UINT32_MAX,
                UINT32_MAX,
            )
            changes: Dict[str, Any] = {}
            if current_scene_id != scene_id:
                heroes = [
                    copy.deepcopy(dict(row))
                    if isinstance(row, Mapping)
                    else copy.deepcopy(row)
                    for row in raw_heroes
                ]
                heroes[target_index]["scene_id"] = scene_id
                changes["heroes"] = heroes
            return OracleResult(
                SC_INVITE_HERO_TO_SCENE_RET,
                self.make_invite_hero_to_scene_ret(
                    ERROR_SUCCESS,
                    scene_id,
                    hero_uid,
                ),
                changes=changes,
            )

        if request_id == CS_REQUEST_FISH_DATAS:
            body = self.make_fish_datas_ret()
            try:
                _decode_empty_request(request_body, "CS_RequestFishDatas")
            except ProtobufDecodeError as exc:
                return OracleResult(
                    SC_FISH_DATAS_RET,
                    body,
                    error_code=ERROR_MESSAGE_DATA_ILLEGAL,
                    supported=False,
                    reason=str(exc),
                )
            try:
                state = self.make_state(account)
                changes = self._changes_for_state(account, state)
                notification = self.make_fishing_attribute_notification(state)
            except OracleProfileError as exc:
                return OracleResult(
                    SC_FISH_DATAS_RET,
                    body,
                    error_code=ERROR_GOSPEL_NOT_FOUND,
                    supported=False,
                    reason=str(exc),
                )
            # The aquarium sends no request when opened.  Prime the runtime-side
            # UserData cache before SC473 completes the login snapshot so
            # FishTankView.RefreshCost can safely read Fishtankcost later.
            return OracleResult(
                SC_FISH_DATAS_RET,
                body,
                changes=changes,
                notifications=((NOTIFY_UPDATE_FISHING_ATTRIBUTE, notification),),
            )

        if request_id == CS_REQUEST_WORK_DATAS:
            try:
                requested_role_id = _decode_role_id(request_body)
            except ProtobufDecodeError as exc:
                return self._work_data_error(
                    account, ERROR_MESSAGE_DATA_ILLEGAL, str(exc)
                )

            if requested_role_id != actual_role_id or actual_role_id <= 0:
                return self._work_data_error(
                    account,
                    ERROR_ROLE_ID_ILLEGAL,
                    f"requested role {requested_role_id} does not match profile role "
                    f"{actual_role_id}",
                )

            try:
                state = self.make_state(account)
                changes = self._changes_for_state(account, state)
                body = self.make_work_datas_ret(account, state)
            except OracleProfileError as exc:
                return self._work_data_error(
                    account, ERROR_GOSPEL_NOT_FOUND, str(exc)
                )
            return OracleResult(SC_WORK_DATAS_RET, body, changes=changes)

        if request_id in {CS_WORK_START, CS_WORK_END}:
            starting = request_id == CS_WORK_START
            response_id = RESPONSE_IDS[request_id]
            message_name = "CS_WorkStart" if starting else "CS_WorkEnd"
            try:
                requested_role_id, building_id, gospel_uid = _decode_work_request(
                    request_body,
                    message_name,
                    include_gospel=True,
                )
            except ProtobufDecodeError as exc:
                body = (
                    self.make_work_start_ret(
                        account,
                        0,
                        {},
                        error_code=ERROR_MESSAGE_DATA_ILLEGAL,
                    )
                    if starting
                    else self.make_work_end_ret(
                        account,
                        0,
                        0,
                        error_code=ERROR_MESSAGE_DATA_ILLEGAL,
                    )
                )
                return OracleResult(
                    response_id,
                    body,
                    error_code=ERROR_MESSAGE_DATA_ILLEGAL,
                    supported=False,
                    reason=str(exc),
                )
            if requested_role_id != actual_role_id or actual_role_id <= 0:
                reason = (
                    f"requested role {requested_role_id} does not match profile role "
                    f"{actual_role_id}"
                )
                body = (
                    self.make_work_start_ret(
                        account,
                        building_id,
                        {},
                        error_code=ERROR_ROLE_ID_ILLEGAL,
                        gospel_uid=gospel_uid,
                    )
                    if starting
                    else self.make_work_end_ret(
                        account,
                        building_id,
                        gospel_uid,
                        error_code=ERROR_ROLE_ID_ILLEGAL,
                    )
                )
                return OracleResult(
                    response_id,
                    body,
                    error_code=ERROR_ROLE_ID_ILLEGAL,
                    supported=False,
                    reason=reason,
                )
            try:
                state = (
                    self.state_with_settings(
                        account, {building_id: gospel_uid}
                    )
                    if starting
                    else self.state_with_end_settings(
                        account, {building_id: gospel_uid}
                    )
                )
                changes = self._changes_for_state(account, state)
                body = (
                    self.make_work_start_ret(account, building_id, state)
                    if starting
                    else self.make_work_end_ret(
                        account, building_id, gospel_uid
                    )
                )
            except OracleProfileError as exc:
                error_code = (
                    exc.error_code
                    if isinstance(exc, OracleWorkError)
                    else ERROR_GOSPEL_NOT_FOUND
                )
                body = (
                    self.make_work_start_ret(
                        account,
                        building_id,
                        {},
                        error_code=error_code,
                        gospel_uid=gospel_uid,
                    )
                    if starting
                    else self.make_work_end_ret(
                        account,
                        building_id,
                        gospel_uid,
                        error_code=error_code,
                    )
                )
                return OracleResult(
                    response_id,
                    body,
                    error_code=error_code,
                    supported=False,
                    reason=str(exc),
                )
            return OracleResult(response_id, body, changes=changes)

        if request_id == CS_TAKE_WORK_REWARD:
            try:
                requested_role_id, building_id, _ = _decode_work_request(
                    request_body,
                    "CS_TakeWorkReward",
                    include_gospel=False,
                )
            except ProtobufDecodeError as exc:
                return OracleResult(
                    SC_TAKE_WORK_REWARD_RET,
                    self.make_take_work_reward_ret(
                        account,
                        0,
                        (),
                        error_code=ERROR_MESSAGE_DATA_ILLEGAL,
                    ),
                    error_code=ERROR_MESSAGE_DATA_ILLEGAL,
                    supported=False,
                    reason=str(exc),
                )
            if requested_role_id != actual_role_id or actual_role_id <= 0:
                reason = (
                    f"requested role {requested_role_id} does not match profile role "
                    f"{actual_role_id}"
                )
                return OracleResult(
                    SC_TAKE_WORK_REWARD_RET,
                    self.make_take_work_reward_ret(
                        account,
                        building_id,
                        (),
                        error_code=ERROR_ROLE_ID_ILLEGAL,
                    ),
                    error_code=ERROR_ROLE_ID_ILLEGAL,
                    supported=False,
                    reason=reason,
                )
            try:
                state = self.make_state(account)
                rewards, inventory = self._claim_work_rewards(
                    account,
                    state,
                    (building_id,),
                    self._now(),
                )
                changes = self._changes_for_state(account, state)
                if account.get("inventory") != inventory:
                    changes["inventory"] = inventory
                body = self.make_take_work_reward_ret(
                    account, building_id, rewards
                )
            except OracleProfileError as exc:
                error_code = (
                    exc.error_code
                    if isinstance(exc, OracleWorkError)
                    else ERROR_MESSAGE_DATA_ILLEGAL
                )
                return OracleResult(
                    SC_TAKE_WORK_REWARD_RET,
                    self.make_take_work_reward_ret(
                        account,
                        building_id,
                        (),
                        error_code=error_code,
                    ),
                    error_code=error_code,
                    supported=False,
                    reason=str(exc),
                )
            return OracleResult(
                SC_TAKE_WORK_REWARD_RET, body, changes=changes
            )

        if request_id == CS_TAKE_WORK_REWARD_ALL:
            try:
                requested_role_id = _decode_role_id(
                    request_body, "CS_TakeWorkRewardAll"
                )
            except ProtobufDecodeError as exc:
                return OracleResult(
                    SC_TAKE_WORK_REWARD_ALL_RET,
                    self.make_take_work_reward_all_ret(
                        (), error_code=ERROR_MESSAGE_DATA_ILLEGAL
                    ),
                    error_code=ERROR_MESSAGE_DATA_ILLEGAL,
                    supported=False,
                    reason=str(exc),
                )
            if requested_role_id != actual_role_id or actual_role_id <= 0:
                reason = (
                    f"requested role {requested_role_id} does not match profile role "
                    f"{actual_role_id}"
                )
                return OracleResult(
                    SC_TAKE_WORK_REWARD_ALL_RET,
                    self.make_take_work_reward_all_ret(
                        (), error_code=ERROR_ROLE_ID_ILLEGAL
                    ),
                    error_code=ERROR_ROLE_ID_ILLEGAL,
                    supported=False,
                    reason=reason,
                )
            try:
                state = self.make_state(account)
                rewards, inventory = self._claim_work_rewards(
                    account,
                    state,
                    REQUIRED_WORK_BUILDING_IDS,
                    self._now(),
                )
                changes = self._changes_for_state(account, state)
                if account.get("inventory") != inventory:
                    changes["inventory"] = inventory
                body = self.make_take_work_reward_all_ret(rewards)
            except OracleProfileError as exc:
                error_code = (
                    exc.error_code
                    if isinstance(exc, OracleWorkError)
                    else ERROR_MESSAGE_DATA_ILLEGAL
                )
                return OracleResult(
                    SC_TAKE_WORK_REWARD_ALL_RET,
                    self.make_take_work_reward_all_ret(
                        (), error_code=error_code
                    ),
                    error_code=error_code,
                    supported=False,
                    reason=str(exc),
                )
            return OracleResult(
                SC_TAKE_WORK_REWARD_ALL_RET, body, changes=changes
            )

        if request_id in {CS_WORK_START_BATCH, CS_WORK_END_BATCH}:
            starting = request_id == CS_WORK_START_BATCH
            message_name = (
                "CS_WorkStartBatch" if starting else "CS_WorkEndBatch"
            )
            try:
                requested_role_id, settings = _decode_work_batch(
                    request_body, message_name
                )
            except ProtobufDecodeError as exc:
                return self._batch_error(
                    request_id, ERROR_MESSAGE_DATA_ILLEGAL, str(exc)
                )
            if requested_role_id != actual_role_id or actual_role_id <= 0:
                return self._batch_error(
                    request_id,
                    ERROR_ROLE_ID_ILLEGAL,
                    f"requested role {requested_role_id} does not match profile role "
                    f"{actual_role_id}",
                )
            try:
                state = (
                    self.state_with_settings(account, settings)
                    if starting
                    else self.state_with_end_settings(account, settings)
                )
                changes = self._changes_for_state(account, state)
                body = (
                    self.make_work_start_batch_ret(account, settings, state)
                    if starting
                    else self.make_work_end_batch_ret(settings)
                )
            except OracleProfileError as exc:
                error_code = (
                    exc.error_code
                    if isinstance(exc, OracleWorkError)
                    else ERROR_GOSPEL_NOT_FOUND
                )
                return self._batch_error(request_id, error_code, str(exc))
            return OracleResult(RESPONSE_IDS[request_id], body, changes=changes)

        if request_id in {CS_REST_START, CS_REST_END}:
            response_id = RESPONSE_IDS[request_id]
            message_name = (
                "CS_RestStart" if request_id == CS_REST_START else "CS_RestEnd"
            )
            try:
                requested_role_id, gospel_map = _decode_rest_request(
                    request_body, message_name
                )
            except ProtobufDecodeError as exc:
                return OracleResult(
                    response_id,
                    self.make_rest_ret(
                        response_id,
                        account,
                        {},
                        error_code=ERROR_MESSAGE_DATA_ILLEGAL,
                    ),
                    error_code=ERROR_MESSAGE_DATA_ILLEGAL,
                    supported=False,
                    reason=str(exc),
                )
            if requested_role_id != actual_role_id or actual_role_id <= 0:
                reason = (
                    f"requested role {requested_role_id} does not match profile role "
                    f"{actual_role_id}"
                )
                return OracleResult(
                    response_id,
                    self.make_rest_ret(
                        response_id,
                        account,
                        {},
                        error_code=ERROR_ROLE_ID_ILLEGAL,
                    ),
                    error_code=ERROR_ROLE_ID_ILLEGAL,
                    supported=False,
                    reason=reason,
                )
            try:
                state = self.state_with_rest(
                    account,
                    gospel_map,
                    start=request_id == CS_REST_START,
                )
                changes = self._changes_for_state(account, state)
                body = self.make_rest_ret(response_id, account, gospel_map)
            except OracleProfileError as exc:
                reason = str(exc)
                error_code = (
                    exc.error_code
                    if isinstance(exc, OracleWorkError)
                    else (
                        ERROR_GOSPEL_NOT_FOUND
                        if "GospelUId" in reason
                        else ERROR_MESSAGE_DATA_ILLEGAL
                    )
                )
                return OracleResult(
                    response_id,
                    self.make_rest_ret(
                        response_id,
                        account,
                        {},
                        error_code=error_code,
                    ),
                    error_code=error_code,
                    supported=False,
                    reason=reason,
                )
            return OracleResult(response_id, body, changes=changes)

        if request_id == CS_REQUEST_FARM_DATAS:
            safe_state = {"farm": self._farm_state()}
            try:
                requested_role_id, friend_id = _decode_farm_request(request_body)
            except ProtobufDecodeError as exc:
                return OracleResult(
                    SC_REQUEST_FARM_DATAS_RET,
                    self.make_farm_datas_ret(account, 0, safe_state),
                    error_code=ERROR_MESSAGE_DATA_ILLEGAL,
                    supported=False,
                    reason=str(exc),
                )
            if requested_role_id != actual_role_id or actual_role_id <= 0:
                reason = (
                    f"requested role {requested_role_id} does not match profile role "
                    f"{actual_role_id}"
                )
                return OracleResult(
                    SC_REQUEST_FARM_DATAS_RET,
                    self.make_farm_datas_ret(account, 0, safe_state),
                    error_code=ERROR_ROLE_ID_ILLEGAL,
                    supported=False,
                    reason=reason,
                )
            if friend_id != 0:
                return OracleResult(
                    SC_REQUEST_FARM_DATAS_RET,
                    self.make_farm_datas_ret(account, 0, safe_state),
                    error_code=ERROR_ROLE_ID_ILLEGAL,
                    supported=False,
                    reason=(
                        f"friend farm {friend_id} is not present in the local "
                        "single-account catalog"
                    ),
                )
            try:
                state = self.make_state(account)
                changes = self._changes_for_state(account, state)
                body = self.make_farm_datas_ret(account, friend_id, state)
            except OracleProfileError as exc:
                return OracleResult(
                    SC_REQUEST_FARM_DATAS_RET,
                    self.make_farm_datas_ret(account, 0, safe_state),
                    error_code=ERROR_GOSPEL_NOT_FOUND,
                    supported=False,
                    reason=str(exc),
                )
            return OracleResult(
                SC_REQUEST_FARM_DATAS_RET, body, changes=changes
            )

        if request_id == CS_REQUEST_FARMLAND_INFO:
            safe_state = {"farm": self._farm_state()}
            try:
                _decode_empty_request(request_body, "CS_RequestFarmlandInfo")
            except ProtobufDecodeError as exc:
                return OracleResult(
                    SC_REQUEST_FARMLAND_INFO_RET,
                    self.make_farmland_info_ret(safe_state),
                    error_code=ERROR_MESSAGE_DATA_ILLEGAL,
                    supported=False,
                    reason=str(exc),
                )
            try:
                state = self.make_state(account)
                changes = self._changes_for_state(account, state)
                body = self.make_farmland_info_ret(state)
            except OracleProfileError as exc:
                return OracleResult(
                    SC_REQUEST_FARMLAND_INFO_RET,
                    self.make_farmland_info_ret(safe_state),
                    error_code=ERROR_GOSPEL_NOT_FOUND,
                    supported=False,
                    reason=str(exc),
                )
            return OracleResult(
                SC_REQUEST_FARMLAND_INFO_RET, body, changes=changes
            )

        if request_id == CS_REQUEST_RACE_HORSE_DATAS:
            safe_state = {"race": self._race_state({})}
            try:
                _decode_empty_request(request_body, "CS_RequestRaceHorseDatas")
            except ProtobufDecodeError as exc:
                return OracleResult(
                    SC_REQUEST_RACE_HORSE_DATAS_RET,
                    self.make_race_horse_datas_ret(safe_state),
                    error_code=ERROR_MESSAGE_DATA_ILLEGAL,
                    supported=False,
                    reason=str(exc),
                )
            try:
                state = self.make_state(account)
                changes = self._changes_for_state(account, state)
                body = self.make_race_horse_datas_ret(state)
            except OracleProfileError as exc:
                return OracleResult(
                    SC_REQUEST_RACE_HORSE_DATAS_RET,
                    self.make_race_horse_datas_ret(safe_state),
                    error_code=ERROR_GOSPEL_NOT_FOUND,
                    supported=False,
                    reason=str(exc),
                )
            return OracleResult(
                SC_REQUEST_RACE_HORSE_DATAS_RET, body, changes=changes
            )

        raise AssertionError(f"owned Oracle command {request_id} was not handled")


@lru_cache(maxsize=1)
def default_layer() -> OracleContentLayer:
    return OracleContentLayer()


def seed_oracle_content(
    profile: MutableMapping[str, Any],
    catalog: Optional[Union[ContentCatalog, str, Path]] = None,
) -> MutableMapping[str, Any]:
    layer = default_layer() if catalog is None else OracleContentLayer(catalog)
    return layer.seed_profile(profile)


def handle_oracle_request(
    request_id: int,
    profile: Optional[Mapping[str, Any]],
    request_body: bytes = b"",
) -> Optional[OracleResult]:
    return default_layer().handle(request_id, profile, request_body)


Layer = OracleContentLayer
Result = OracleResult


__all__ = [
    "BUILDING_TYPES",
    "CS_INVITE_HERO_TO_SCENE",
    "CS_REQUEST_FISH_DATAS",
    "CS_REQUEST_FARM_DATAS",
    "CS_REQUEST_FARMLAND_INFO",
    "CS_REQUEST_RACE_HORSE_DATAS",
    "CS_REQUEST_WORK_DATAS",
    "CS_REST_END",
    "CS_REST_START",
    "CS_TAKE_WORK_REWARD",
    "CS_TAKE_WORK_REWARD_ALL",
    "CS_WORK_END",
    "CS_WORK_END_BATCH",
    "CS_WORK_START",
    "CS_WORK_START_BATCH",
    "DAILY_CITY_INITIALIZATION",
    "DAILY_SCENE_NAVIGATION",
    "ENTERTAINMENT_BUILDING_ID",
    "ERROR_CLIENT_REQUEST_PARAM",
    "ERROR_EXCEED_ITEM_LIMIT_COUNT",
    "ERROR_GOSPEL_ALREADY_WORKING",
    "ERROR_GOSPEL_IN_RESTING_CANNOT_WORK",
    "ERROR_GOSPEL_IN_WORKING_CANNOT_REST",
    "ERROR_GOSPEL_NOT_FOUND",
    "ERROR_GOSPEL_TO_WORK_NO_ENERGY",
    "ERROR_HERO_NOT_FOUND",
    "ERROR_MESSAGE_DATA_ILLEGAL",
    "ERROR_NO_REWARD_CAN_BE_TAKEN",
    "ERROR_ROLE_ID_ILLEGAL",
    "ERROR_SUCCESS",
    "FARMER_LEVEL",
    "FARMER_LEVEL_CONFIG_ID",
    "FARM_CURRENT_TEMPERATURE",
    "FARM_FERTILIZER_LIMIT",
    "FARM_LAND_IDS",
    "FARM_TEMPERATURE_PARAMETER_ID",
    "FISHING_ATTRIBUTE_CONFIG",
    "FISHING_ATTRIBUTE_SCHEMA_VERSION",
    "FISHING_BAIT_IDS",
    "FISHING_SCENE_IDS",
    "HERO_SCENE_IDS",
    "NOTIFY_UPDATE_FISHING_ATTRIBUTE",
    "ONSEN_V2_INITIALIZATION",
    "ONSEN_V2_SCENE_INITIALIZATION",
    "ORACLE_STATE_KEY",
    "ORACLE_STATE_SCHEMA_VERSION",
    "OracleCatalogError",
    "OracleContentLayer",
    "OracleProfileError",
    "OracleResult",
    "OracleStaticCatalog",
    "OracleWorkError",
    "RACE_BASE_STAKE_CONFIG_ID",
    "RACE_HORSE_IDS",
    "RACE_HORSE_SPEED_LEVEL",
    "RACE_HORSE_STATUS_NORMAL",
    "RACE_REWARD_RANK_COUNT",
    "RESPONSE_IDS",
    "REQUIRED_WORK_BUILDING_IDS",
    "SC_REQUEST_FARM_DATAS_RET",
    "SC_REQUEST_FARMLAND_INFO_RET",
    "SC_REQUEST_RACE_HORSE_DATAS_RET",
    "SC_FISH_DATAS_RET",
    "SC_INVITE_HERO_TO_SCENE_RET",
    "SC_TAKE_WORK_REWARD_ALL_RET",
    "SC_TAKE_WORK_REWARD_RET",
    "SC_WORK_DATAS_RET",
    "SC_WORK_END_BATCH_RET",
    "SC_WORK_END_RET",
    "SC_REST_END_RET",
    "SC_REST_START_RET",
    "SC_WORK_START_RET",
    "SC_WORK_START_BATCH_RET",
    "VISIBLE_FUNCTION_ROUTES",
    "VisibleFunctionRoute",
    "WorkLevelDefinition",
    "WorkReward",
    "handle_oracle_request",
    "seed_oracle_content",
]
