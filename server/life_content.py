"""Persistent local cooking, fishing, aquarium, and fish-atlas gameplay.

This module is deliberately self-contained so it can be wired into the local
server without changing the existing dispatcher or profile implementation.
Protocol names and field layouts come from ``generated/protocol``.  Dish and
bag-item rows come from ``generated/content``.  The fishing tables which are
not yet in that generated allow-list are pinned below from an authorized
``TableConfig`` projection; an ID is never inferred from display text.

The public contract mirrors the other feature layers in this repository:
``handle(command, profile, body)`` is copy-on-write and returns a shallow set
of profile changes, while ``seed_profile`` applies the same migration eagerly.
"""

from __future__ import annotations

import copy
import hashlib
import json
import time
from collections import Counter
from collections.abc import Mapping, MutableMapping, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Dict, Iterable, Optional, Tuple, Union

try:  # Package imports in tests; fallbacks support direct server execution.
    from .content_profile import ContentCatalog, default_content_catalog
    from .content_responses import ProtobufDecodeError, iter_wire_fields, field_varint
    from .inventory_content import InventoryContentError, normalize_inventory
    from .protobuf_codec import ProtocolMessageCodec
except ImportError:  # pragma: no cover - direct-script compatibility
    from content_profile import ContentCatalog, default_content_catalog  # type: ignore
    from content_responses import ProtobufDecodeError, iter_wire_fields, field_varint  # type: ignore
    from inventory_content import InventoryContentError, normalize_inventory  # type: ignore
    from protobuf_codec import ProtocolMessageCodec  # type: ignore


# Command pairs defined from generated/protocol/protocol_catalog.json.
CS_REQUEST_GOURMET_DATAS = 272
SC_GOURMET_DATAS_RET = 273
CS_MATCH_RECIPE = 274
SC_MATCH_RECIPE_RET = 275
CS_COOK_FOOD = 276
SC_COOK_FOOD_RET = 277
NOTIFY_UPDATE_FOOD = 278
CS_USE_FOOD_PROPS = 281
SC_USE_FOOD_PROPS_RET = 282

CS_REQUEST_FISH_DATAS = 472
SC_FISH_DATAS_RET = 473
CS_GO_FISHING = 474
SC_GO_FISHING_RET = 475
CS_PUT_OR_TAKEOUT_FISH = 476
SC_PUT_OR_TAKEOUT_FISH_RET = 477
CS_GO_FISHING_RESULT = 478
SC_GO_FISHING_RESULT_RET = 479
CS_GET_FISH_ACHIEVE_REWARD = 480
SC_GET_FISH_ACHIEVE_REWARD_RET = 481
NOTIFY_UPDATE_FISHING_ATTRIBUTE = 521
NOTIFY_UPDATE_FISH_RECORD = 522
CS_SELL_FISH = 523
SC_SELL_FISH_RET = 524
CS_REQUEST_SCENE_FISH_COUNT = 558
SC_SCENE_FISH_COUNT_RET = 559

RESPONSE_IDS = MappingProxyType(
    {
        218: 219,
        CS_REQUEST_GOURMET_DATAS: SC_GOURMET_DATAS_RET,
        CS_MATCH_RECIPE: SC_MATCH_RECIPE_RET,
        CS_COOK_FOOD: SC_COOK_FOOD_RET,
        CS_USE_FOOD_PROPS: SC_USE_FOOD_PROPS_RET,
        CS_REQUEST_FISH_DATAS: SC_FISH_DATAS_RET,
        CS_GO_FISHING: SC_GO_FISHING_RET,
        CS_PUT_OR_TAKEOUT_FISH: SC_PUT_OR_TAKEOUT_FISH_RET,
        CS_GO_FISHING_RESULT: SC_GO_FISHING_RESULT_RET,
        CS_GET_FISH_ACHIEVE_REWARD: SC_GET_FISH_ACHIEVE_REWARD_RET,
        CS_SELL_FISH: SC_SELL_FISH_RET,
        CS_REQUEST_SCENE_FISH_COUNT: SC_SCENE_FISH_COUNT_RET,
    }
)

# Existing Msg.ErrorCode values used elsewhere in this repository.
ERROR_SUCCESS = 0
ERROR_NO_REWARD_CAN_BE_TAKEN = 122
ERROR_HERO_NOT_FOUND = 516
ERROR_EXCEED_ITEM_LIMIT_COUNT = 429
ERROR_MESSAGE_DATA_ILLEGAL = 32769

UINT32_MAX = (1 << 32) - 1
UINT64_MAX = (1 << 64) - 1
INT32_MAX = (1 << 31) - 1

LIFE_STATE_KEY = "life_content"
LIFE_SCHEMA_VERSION = 1
LIFE_SEED_VERSION = 1
FOOD_UID_BASE = 30_000_000_000
FISH_UID_BASE = 20_000_000_000
FISH_SALE_RESOURCE_KEY = "2:0"

FISHING_SCENE_IDS = (1_998_401, 1_998_402, 1_998_403, 1_998_404)
FISHING_BAIT_ITEM_IDS = tuple(range(1_010_001, 1_010_007))
FISHING_BAIT_CONFIG_IDS = tuple(range(1_998_301, 1_998_307))

# Unlike the other life tables, a_item is shared by every content system and
# may legitimately gain unrelated rows in a compatibility overlay. Pin the
# exact 1.0 subset consumed by cooking/fishing instead of the whole table.
LIFE_ITEM_PROJECTION_FIELDS = (
    "id",
    "type",
    "quality",
    "correlation",
    "dish_ingredient",
    "ingredient_priority",
    "ingredient_effect",
    "ingredient_effect_num",
    "ingredient_time",
)
LIFE_ITEM_PROJECTION_COUNT = 170
# a_dish_mastery: (cook count, displayed level).
MASTERY_THRESHOLDS = ((0, 0), (5, 1), (10, 2), (15, 3))

# a_dish_effect is a five-by-five matrix.  The tuple values are
# (AttributeType, minimum accumulated ingredient exp, PlusValue).
DISH_EFFECTS = MappingProxyType(
    {
        1: (4, (10, 20, 30, 40, 50), (10, 15, 20, 25, 30)),
        2: (14, (10, 20, 30, 40, 50), (10, 15, 20, 25, 30)),
        3: (21, (10, 20, 30, 40, 50), (10, 15, 20, 25, 30)),
        4: (4001, (10, 20, 30, 40, 50), (10, 15, 20, 25, 30)),
        5: (3001, (10, 20, 30, 40, 50), (10, 15, 20, 25, 30)),
    }
)

# Exact a_fishing_scene fish lists.  The map values are base species IDs
# (a_fishing_fish.id), not the three quality variant IDs in Fishrecord.
SCENE_SPECIES = MappingProxyType(
    {
        1_998_401: (
            1_974_004,
            1_974_009,
            1_974_013,
            1_974_015,
            1_974_021,
            1_974_023,
            1_974_026,
            1_974_027,
            1_974_028,
            1_974_030,
            1_974_039,
            1_974_040,
        ),
        1_998_402: (
            1_974_001,
            1_974_005,
            1_974_006,
            1_974_011,
            1_974_012,
            1_974_018,
            1_974_019,
            1_974_020,
            1_974_022,
            1_974_024,
            1_974_025,
            1_974_033,
        ),
        1_998_403: (
            1_974_002,
            1_974_003,
            1_974_017,
            1_974_021,
            1_974_023,
            1_974_026,
            1_974_029,
            1_974_031,
            1_974_032,
            1_974_034,
            1_974_036,
            1_974_025,
            1_974_037,
        ),
        1_998_404: (
            1_974_007,
            1_974_008,
            1_974_010,
            1_974_012,
            1_974_014,
            1_974_016,
            1_974_018,
            1_974_024,
            1_974_029,
            1_974_033,
            1_974_035,
            1_974_038,
        ),
    }
)

# a_fishing_fish compact projection:
# id, quality, bait type, weight, shape min/max, length min/max.
FISH_SPECIES_ROWS = (
    (1974001, 1, 1, 800, 500, 9500, 30, 80),
    (1974002, 1, 2, 800, 100, 9100, 20, 90),
    (1974003, 1, 3, 800, 275, 6000, 25, 80),
    (1974004, 3, 4, 300, 250, 7500, 29, 90),
    (1974005, 2, 5, 500, 220, 3100, 27, 65),
    (1974006, 2, 1, 500, 300, 1400, 36, 60),
    (1974007, 1, 2, 800, 300, 5200, 31, 80),
    (1974008, 1, 3, 800, 300, 1800, 30, 55),
    (1974009, 1, 4, 800, 200, 4700, 35, 100),
    (1974010, 3, 5, 300, 300, 14400, 33, 120),
    (1974011, 2, 1, 500, 150, 6200, 26, 90),
    (1974012, 2, 2, 500, 300, 34200, 33, 160),
    (1974013, 2, 3, 500, 300, 13400, 31, 110),
    (1974014, 2, 4, 500, 200, 9100, 28, 100),
    (1974015, 2, 5, 500, 240, 10900, 28, 100),
    (1974016, 1, 1, 800, 200, 8700, 27, 95),
    (1974017, 3, 2, 300, 300, 34500, 37, 180),
    (1974018, 2, 3, 500, 300, 15800, 32, 120),
    (1974019, 3, 4, 300, 300, 11100, 30, 100),
    (1974020, 1, 5, 800, 200, 15700, 28, 120),
    (1974021, 1, 1, 800, 300, 2200, 31, 60),
    (1974022, 3, 2, 300, 300, 11100, 27, 90),
    (1974023, 1, 3, 800, 300, 5200, 31, 80),
    (1974024, 2, 4, 500, 300, 8100, 30, 90),
    (1974025, 2, 5, 500, 100, 1600, 22, 55),
    (1974026, 1, 1, 800, 300, 6700, 32, 90),
    (1974027, 3, 2, 300, 200, 8200, 29, 100),
    (1974028, 1, 3, 800, 300, 7300, 31, 90),
    (1974029, 1, 4, 800, 100, 4200, 23, 80),
    (1974030, 1, 5, 800, 100, 1600, 20, 50),
    (1974031, 1, 1, 800, 200, 6000, 29, 90),
    (1974032, 3, 2, 300, 300, 5200, 31, 80),
    (1974033, 2, 3, 500, 300, 28200, 33, 150),
    (1974034, 2, 4, 500, 300, 6700, 32, 90),
    (1974035, 3, 5, 300, 300, 12100, 35, 120),
    (1974036, 2, 1, 500, 300, 8300, 33, 100),
    (1974037, 1, 2, 800, 200, 4700, 28, 80),
    (1974038, 2, 3, 500, 200, 10200, 27, 100),
    (1974039, 2, 4, 500, 300, 6700, 32, 90),
    (1974040, 1, 5, 800, 150, 1200, 25, 50),
)

# a_fishing_fishattribute.fish_cost in ascending variant-ID order.
FISH_COSTS = (
    8,
    10,
    12,
    10,
    12,
    15,
    10,
    12,
    15,
    20,
    24,
    30,
    8,
    10,
    12,
    8,
    10,
    12,
    10,
    12,
    15,
    7,
    8,
    10,
    10,
    12,
    15,
    20,
    24,
    30,
    12,
    15,
    18,
    12,
    15,
    18,
    12,
    15,
    18,
    12,
    15,
    18,
    15,
    18,
    20,
    8,
    10,
    12,
    20,
    25,
    30,
    12,
    15,
    18,
    20,
    25,
    30,
    10,
    12,
    15,
    8,
    10,
    12,
    20,
    25,
    30,
    10,
    12,
    15,
    12,
    15,
    18,
    10,
    12,
    15,
    10,
    12,
    15,
    20,
    25,
    30,
    10,
    12,
    15,
    10,
    12,
    15,
    10,
    12,
    15,
    10,
    12,
    15,
    20,
    25,
    30,
    10,
    12,
    15,
    15,
    18,
    20,
    20,
    25,
    30,
    18,
    20,
    23,
    9,
    10,
    12,
    10,
    12,
    15,
    10,
    12,
    15,
    8,
    10,
    12,
)

# Exact achievement conditions transcribed from a_fishing_achievement and
# resolved to a_fishing_fish IDs using the validationd a_item fish names.
# mode is "any" (one quality of each species) or "all" (all three qualities).
FISH_ACHIEVEMENTS = MappingProxyType(
    {
        1975001: ("any", (1974004, 1974027), 1, 3, 0),
        1975002: ("all", (1974004,), 1, 5, 0),
        1975003: ("all", (1974027,), 1, 5, 0),
        1975004: ("any", (1974040,), 0, 0, 1010002),
        1975005: ("any", (1974013,), 0, 0, 1010003),
        1975006: ("any", (1974019, 1974022), 1, 3, 0),
        1975007: ("all", (1974019,), 1, 5, 0),
        1975008: ("all", (1974022,), 1, 5, 0),
        1975009: ("any", (1974005,), 0, 0, 1010004),
        1975010: ("any", (1974011,), 0, 0, 1010005),
        1975011: ("any", (1974017, 1974032), 1, 3, 0),
        1975012: ("all", (1974017,), 1, 5, 0),
        1975013: ("all", (1974032,), 1, 5, 0),
        1975014: ("any", (1974029,), 0, 0, 1010006),
        1975015: ("any", (1974010, 1974035), 6, 3, 0),
        1975016: ("all", (1974010,), 6, 5, 0),
        1975017: ("all", (1974035,), 6, 5, 0),
        1975018: ("any", (1974038,), 6, 2, 0),
    }
)


class LifeCatalogError(ValueError):
    """Raised when generated cooking data does not match the pinned closure."""


class LifeProfileError(ValueError):
    """Raised when persisted state cannot be normalized safely."""


@dataclass(frozen=True)
class LifeResult:
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
class RecipeRequirement:
    ingredient_type: int
    count: int
    ingredient_id: int = 0


@dataclass(frozen=True)
class DishDefinition:
    id: int
    recipe: Tuple[RecipeRequirement, ...]
    fixed_props: Tuple[Tuple[int, int, int], ...]
    fixed_round: int
    fondness: int


@dataclass(frozen=True)
class IngredientDefinition:
    item_id: int
    ingredient_id: int
    ingredient_type: int
    attr_group: int
    exp: int
    time: int


@dataclass(frozen=True)
class FishSpeciesDefinition:
    id: int
    quality: int
    bait_type: int
    weight: int
    shape_min: int
    shape_max: int
    length_min: int
    length_max: int
    scenes: Tuple[int, ...]


@dataclass(frozen=True)
class FishVariantDefinition:
    id: int
    species_id: int
    grade: int
    star_min: int
    star_max: int
    item_id: int
    fish_cost: int
    sale_value: int


def _coerce_catalog(
    catalog: Optional[Union[ContentCatalog, str, Path]],
) -> ContentCatalog:
    if catalog is None:
        return default_content_catalog()
    if isinstance(catalog, ContentCatalog):
        return catalog
    return ContentCatalog.load(Path(catalog))


def _safe_uint(value: Any, default: int = 0, maximum: int = UINT64_MAX) -> int:
    if isinstance(value, bool):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return parsed if 0 <= parsed <= maximum else default


def _lookup(value: Any, key: int) -> Any:
    if not isinstance(value, Mapping):
        return None
    if key in value:
        return value[key]
    return value.get(str(key))


def _decode_empty(data: bytes, name: str) -> None:
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise ProtobufDecodeError("request body must be bytes")
    fields = list(iter_wire_fields(bytes(data)))
    if fields:
        raise ProtobufDecodeError(f"{name} must be empty, got field {fields[0][0]}")


def _decode_uints(
    data: bytes,
    name: str,
    fields: Mapping[int, Tuple[str, int]],
    defaults: Optional[Mapping[int, int]] = None,
) -> Dict[str, int]:
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise ProtobufDecodeError("request body must be bytes")
    values: Dict[str, int] = {}
    seen = set()
    for number, wire_type, raw in iter_wire_fields(bytes(data)):
        definition = fields.get(number)
        if definition is None:
            raise ProtobufDecodeError(f"{name} has unknown field {number}")
        if number in seen:
            raise ProtobufDecodeError(f"{name}.{definition[0]} is duplicated")
        if wire_type != 0:
            raise ProtobufDecodeError(f"{name}.{definition[0]} must be a varint")
        value = int(raw)
        if value < 0 or value > definition[1]:
            raise ProtobufDecodeError(f"{name}.{definition[0]} exceeds {definition[1]}")
        seen.add(number)
        values[definition[0]] = value
    defaults = defaults or {}
    for number, default in defaults.items():
        if number in fields and number not in seen:
            values[fields[number][0]] = default
    missing = [
        label
        for number, (label, _maximum) in fields.items()
        if number not in seen and number not in defaults
    ]
    if missing:
        raise ProtobufDecodeError(f"{name} is missing {', '.join(missing)}")
    return values


def _decode_materials(data: bytes, name: str) -> Tuple[Tuple[int, int], ...]:
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise ProtobufDecodeError("request body must be bytes")
    materials = []
    for number, wire_type, raw in iter_wire_fields(bytes(data)):
        if number != 1 or wire_type != 2 or not isinstance(raw, bytes):
            raise ProtobufDecodeError(f"{name}.ItemList must contain messages only")
        row = _decode_uints(
            raw,
            "ConsumeMaterial",
            {1: ("ItemUId", UINT64_MAX), 2: ("ItemNum", UINT32_MAX)},
        )
        if row["ItemUId"] <= 0 or row["ItemNum"] <= 0:
            raise ProtobufDecodeError("ConsumeMaterial values must be positive")
        materials.append((row["ItemUId"], row["ItemNum"]))
    if not materials:
        raise ProtobufDecodeError(f"{name}.ItemList must not be empty")
    return tuple(materials)


def _fingerprint(command: int, body: bytes) -> str:
    return hashlib.sha256(str(command).encode("ascii") + b":" + bytes(body)).hexdigest()


def _mastery_level(count: int) -> int:
    level = 0
    for threshold, candidate in MASTERY_THRESHOLDS:
        if count >= threshold:
            level = candidate
    return level


def _life_item_projection_fingerprint(
    records: Sequence[Mapping[str, Any]],
) -> Tuple[int, str]:
    rows = []
    for record in records:
        item_id = int(record["id"])
        ingredient_id = _safe_uint(
            record.get("dish_ingredient"), 0, UINT32_MAX
        )
        if not ingredient_id and item_id not in FISHING_BAIT_ITEM_IDS:
            continue
        rows.append(
            {field: record.get(field) for field in LIFE_ITEM_PROJECTION_FIELDS}
        )
    rows.sort(key=lambda row: int(row["id"]))
    encoded = json.dumps(
        rows,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return len(rows), hashlib.sha256(encoded).hexdigest()


class LifeStaticCatalog:
    """Validated cooking catalog plus the pinned fishing-table projection."""

    def __init__(self, catalog: ContentCatalog) -> None:
        self.catalog = catalog
        try:
            dish_table = catalog.table("a_dish")
            item_table = catalog.table("a_item")
        except KeyError as exc:
            raise LifeCatalogError(
                f"generated catalog is missing {exc.args[0]}"
            ) from None

        if not dish_table.source_sha256:
            raise LifeCatalogError(
                f"{dish_table.name} has no source identity in the generated catalog"
            )
        item_projection = _life_item_projection_fingerprint(item_table.records)
        if item_projection[0] != LIFE_ITEM_PROJECTION_COUNT:
            raise LifeCatalogError(
                "a_item life projection has an unexpected row count: "
                f"got {item_projection[0]}, expected {LIFE_ITEM_PROJECTION_COUNT}"
            )

        dish_required = {"id", "recipe", "fixed_attr", "fixed_round", "favor_exp"}
        item_required = {
            "id",
            "type",
            "quality",
            "correlation",
            "dish_ingredient",
            "ingredient_priority",
            "ingredient_effect",
            "ingredient_effect_num",
            "ingredient_time",
        }
        if dish_required - set(dish_table.fields):
            raise LifeCatalogError("a_dish lacks fields required by cooking")
        if item_required - set(item_table.fields):
            raise LifeCatalogError("a_item lacks fields required by life content")

        self.item_rows = MappingProxyType(
            {int(row["id"]): row for row in item_table.records}
        )
        dishes: Dict[int, DishDefinition] = {}
        for row in dish_table.records:
            dish_id = int(row["id"])
            try:
                raw_recipe = json.loads(row.get("recipe") or "[]")
                raw_fixed = json.loads(row.get("fixed_attr") or "[]")
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise LifeCatalogError(f"a_dish[{dish_id}] has invalid JSON") from exc
            recipe = []
            for raw in raw_recipe:
                if not isinstance(raw, Mapping):
                    raise LifeCatalogError(
                        f"a_dish[{dish_id}].recipe has a non-map row"
                    )
                ingredient_type = _safe_uint(raw.get("ingredient_type"), 0, UINT32_MAX)
                count = _safe_uint(raw.get("count"), 0, UINT32_MAX)
                ingredient_id = _safe_uint(raw.get("id"), 0, UINT32_MAX)
                if ingredient_type <= 0 or count <= 0:
                    raise LifeCatalogError(
                        f"a_dish[{dish_id}] has an invalid requirement"
                    )
                recipe.append(RecipeRequirement(ingredient_type, count, ingredient_id))
            fixed = []
            for raw in raw_fixed:
                if not isinstance(raw, Mapping):
                    raise LifeCatalogError(f"a_dish[{dish_id}].fixed_attr is invalid")
                prop = (
                    _safe_uint(raw.get("effect"), 0, UINT32_MAX),
                    _safe_uint(raw.get("level"), 0, UINT32_MAX),
                    _safe_uint(raw.get("value"), 0, INT32_MAX),
                )
                if min(prop) <= 0:
                    raise LifeCatalogError(
                        f"a_dish[{dish_id}] has an invalid fixed prop"
                    )
                fixed.append(prop)
            dishes[dish_id] = DishDefinition(
                dish_id,
                tuple(recipe),
                tuple(fixed),
                _safe_uint(row.get("fixed_round"), 0, UINT32_MAX),
                _safe_uint(row.get("favor_exp"), 0, UINT32_MAX),
            )
        if len(dishes) != 46 or 1_976_001 not in dishes:
            raise LifeCatalogError("a_dish is not the configured 46-row table")
        self.dishes = MappingProxyType(dishes)

        ingredients: Dict[int, IngredientDefinition] = {}
        for item_id, row in self.item_rows.items():
            ingredient_id = _safe_uint(row.get("dish_ingredient"), 0, UINT32_MAX)
            if not ingredient_id:
                continue
            projected = self._ingredient_projection(ingredient_id)
            if projected is None:
                continue
            ingredient_type, attr_group, exp = projected
            ingredients[item_id] = IngredientDefinition(
                item_id,
                ingredient_id,
                ingredient_type,
                attr_group,
                exp,
                max(1, _safe_uint(row.get("ingredient_time"), 1, UINT32_MAX)),
            )
        self.ingredients = MappingProxyType(ingredients)
        self.recipe_dishes = tuple(
            sorted(
                (dish for dish in dishes.values() if dish.recipe),
                key=lambda dish: (
                    -sum(req.count for req in dish.recipe if req.ingredient_id),
                    dish.id,
                ),
            )
        )

        species = {}
        for row in FISH_SPECIES_ROWS:
            species_id = row[0]
            scenes = tuple(
                scene_id
                for scene_id, fish_ids in SCENE_SPECIES.items()
                if species_id in fish_ids
            )
            if not scenes:
                raise LifeCatalogError(f"fish species {species_id} has no scene")
            species[species_id] = FishSpeciesDefinition(*row, scenes)
        if len(species) != 40 or set(species) != set(range(1_974_001, 1_974_041)):
            raise LifeCatalogError("pinned a_fishing_fish projection is incomplete")
        for scene_id, fish_ids in SCENE_SPECIES.items():
            if len(fish_ids) != len(set(fish_ids)) or not set(fish_ids) <= set(species):
                raise LifeCatalogError(f"scene {scene_id} fish closure is invalid")
        self.species = MappingProxyType(species)

        variants: Dict[int, FishVariantDefinition] = {}
        missing = []
        sale_by_quality = {
            1: (100, 150, 200),
            2: (300, 600, 900),
            3: (1000, 1500, 2000),
        }
        for ordinal, species_id in enumerate(sorted(species), start=1):
            definition = species[species_id]
            for grade in (1, 2, 3):
                variant_id = 1_960_001 + (ordinal - 1) * 3 + grade - 1
                item_id = 1_011_000 + ordinal + (grade - 1) * 100
                item = self.item_rows.get(item_id)
                expected_ingredient = 1_978_100 + ordinal
                if (
                    item is None
                    or _safe_uint(item.get("type"), 0, UINT32_MAX) != 8
                    or _safe_uint(item.get("quality"), 0, UINT32_MAX) != grade
                    or _safe_uint(item.get("dish_ingredient"), 0, UINT32_MAX)
                    != expected_ingredient
                ):
                    missing.append(item_id)
                    continue
                star_min, star_max = ((1, 3), (4, 6), (7, 7))[grade - 1]
                variants[variant_id] = FishVariantDefinition(
                    variant_id,
                    species_id,
                    grade,
                    star_min,
                    star_max,
                    item_id,
                    FISH_COSTS[variant_id - 1_960_001],
                    sale_by_quality[definition.quality][grade - 1],
                )
        self.fish_enabled = not missing and len(variants) == 120
        self.fish_disabled_reason = (
            ""
            if self.fish_enabled
            else "a_item is missing configured fish variants: "
            + ", ".join(map(str, missing[:8]))
        )
        self.variants = MappingProxyType(variants if self.fish_enabled else {})

        bait_config_by_item = {}
        for item_id, config_id in zip(FISHING_BAIT_ITEM_IDS, FISHING_BAIT_CONFIG_IDS):
            row = self.item_rows.get(item_id)
            if (
                row is None
                or _safe_uint(row.get("type"), 0, UINT32_MAX) != 7
                or _safe_uint(row.get("correlation"), 0, UINT32_MAX) != config_id
            ):
                self.fish_enabled = False
                self.fish_disabled_reason = (
                    f"a_item[{item_id}] does not close over bait {config_id}"
                )
                bait_config_by_item = {}
                break
            bait_config_by_item[item_id] = config_id
        self.bait_config_by_item = MappingProxyType(bait_config_by_item)

    @staticmethod
    def _ingredient_projection(ingredient_id: int) -> Optional[Tuple[int, int, int]]:
        # Exact compact projection of a_dish_ingredient's 64 rows.
        if 1_978_001 <= ingredient_id <= 1_978_004:
            return 1, 1, (10, 15, 20, 25)[ingredient_id - 1_978_001]
        if 1_978_005 <= ingredient_id <= 1_978_008:
            return 3, 2, (10, 15, 20, 25)[ingredient_id - 1_978_005]
        if 1_978_009 <= ingredient_id <= 1_978_012:
            return 4, 3, (10, 15, 20, 25)[ingredient_id - 1_978_009]
        if 1_978_013 <= ingredient_id <= 1_978_016:
            return 5, 4, (10, 15, 20, 25)[ingredient_id - 1_978_013]
        if 1_978_017 <= ingredient_id <= 1_978_020:
            return 6, 5, (10, 15, 20, 25)[ingredient_id - 1_978_017]
        if 1_978_021 <= ingredient_id <= 1_978_024:
            return 10, 1, (10, 15, 20, 25)[ingredient_id - 1_978_021]
        if 1_978_101 <= ingredient_id <= 1_978_140:
            return 3, 2, 10
        return None

    def match_recipe(self, ingredient_ids: Sequence[int]) -> Optional[DishDefinition]:
        for dish in self.recipe_dishes:
            if sum(req.count for req in dish.recipe) != len(ingredient_ids):
                continue
            remaining = Counter(int(value) for value in ingredient_ids)
            valid = True
            for requirement in dish.recipe:
                if not requirement.ingredient_id:
                    continue
                if remaining[requirement.ingredient_id] < requirement.count:
                    valid = False
                    break
                remaining[requirement.ingredient_id] -= requirement.count
            if not valid:
                continue
            by_type = Counter()
            for ingredient_id, count in remaining.items():
                if count <= 0:
                    continue
                projection = self._ingredient_projection(ingredient_id)
                if projection is None:
                    valid = False
                    break
                by_type[projection[0]] += count
            if not valid:
                continue
            expected = Counter()
            for requirement in dish.recipe:
                if not requirement.ingredient_id:
                    expected[requirement.ingredient_type] += requirement.count
            if by_type == expected:
                return dish
        return None

    def food_prop(
        self, dish: DishDefinition, ingredients: Sequence[IngredientDefinition]
    ) -> Dict[str, Any]:
        if dish.fixed_props:
            props = [
                {"type": effect, "level": level, "plus_value": value}
                for effect, level, value in dish.fixed_props
            ]
            round_count = dish.fixed_round
        else:
            exp_by_group: Counter[int] = Counter()
            for ingredient in ingredients:
                exp_by_group[ingredient.attr_group] += ingredient.exp
            props = []
            for group in sorted(exp_by_group):
                attribute, thresholds, values = DISH_EFFECTS[group]
                level = 0
                plus_value = 0
                for index, threshold in enumerate(thresholds, start=1):
                    if exp_by_group[group] >= threshold:
                        level = index
                        plus_value = values[index - 1]
                if level:
                    props.append(
                        {"type": attribute, "level": level, "plus_value": plus_value}
                    )
            round_count = sum(ingredient.time for ingredient in ingredients)
        return {"round": max(1, round_count), "props": props}


class LifeContentLayer:
    """Copy-on-write implementation of the defined life-content commands."""

    def __init__(
        self,
        catalog: Optional[Union[ContentCatalog, str, Path]] = None,
        *,
        codec: Optional[ProtocolMessageCodec] = None,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        self.content_catalog = _coerce_catalog(catalog)
        self.catalog = LifeStaticCatalog(self.content_catalog)
        self.codec = codec or ProtocolMessageCodec()
        self.clock = clock or time.time

    def _now(self) -> int:
        try:
            value = int(self.clock())
        except (TypeError, ValueError, OverflowError) as exc:
            raise LifeProfileError(
                "life-content clock must return a timestamp"
            ) from exc
        if value < 0 or value > UINT64_MAX:
            raise LifeProfileError("life-content timestamp is outside uint64")
        return value

    @staticmethod
    def _base_food(dish: DishDefinition) -> Dict[str, Any]:
        # 1976032 is one of the five fixed-attribute dishes, so its nested
        # APlateFoodProp is meaningful even in a sparse first-run profile.
        uid = FOOD_UID_BASE + dish.id
        return {
            "uid": uid,
            "food_id": dish.id,
            "count": 1,
            "fondness": dish.fondness,
            "prop": {
                "round": max(1, dish.fixed_round),
                "props": [
                    {"type": effect, "level": level, "plus_value": value}
                    for effect, level, value in dish.fixed_props
                ],
            },
            "created_at": 0,
        }

    def _base_fish_instance(self) -> Dict[str, Any]:
        variant = self.catalog.variants[1_960_001]
        species = self.catalog.species[variant.species_id]
        return {
            "uid": FISH_UID_BASE + variant.id,
            "fish_id": variant.id,
            "count": 1,
            "tank_count": 0,
            "star": variant.star_min,
            "shape": species.shape_min,
            "length": species.length_min,
            "scene_id": species.scenes[0],
            "caught_at": 0,
        }

    def _canonical_scene_map(self) -> Dict[str, Dict[str, Dict[str, int]]]:
        result: Dict[str, Dict[str, Dict[str, int]]] = {}
        if not self.catalog.fish_enabled:
            return result
        for scene_id, species_ids in SCENE_SPECIES.items():
            positions = {}
            for position, species_id in enumerate(species_ids):
                ordinal = species_id - 1_974_000
                variant_id = 1_960_001 + (ordinal - 1) * 3
                species = self.catalog.species[species_id]
                positions[str(position)] = {
                    "fish_id": variant_id,
                    "star": 1,
                    "shape": species.shape_min,
                    "length": species.length_min,
                    "scene_id": scene_id,
                    "position_id": position,
                }
            result[str(scene_id)] = positions
        return result

    @staticmethod
    def _normal_prop(value: Any) -> Dict[str, Any]:
        source = value if isinstance(value, Mapping) else {}
        props = []
        raw_props = source.get("props")
        if isinstance(raw_props, Sequence) and not isinstance(
            raw_props, (str, bytes, bytearray)
        ):
            for raw in raw_props:
                if not isinstance(raw, Mapping):
                    continue
                prop_type = _safe_uint(raw.get("type"), 0, UINT32_MAX)
                level = _safe_uint(raw.get("level"), 0, UINT32_MAX)
                plus = _safe_uint(raw.get("plus_value"), 0, INT32_MAX)
                if prop_type and level:
                    props.append(
                        {"type": prop_type, "level": level, "plus_value": plus}
                    )
        return {
            "round": max(1, _safe_uint(source.get("round"), 1, UINT32_MAX)),
            "props": props,
        }

    def _normal_food_inventory(self, value: Any) -> Dict[str, Dict[str, Any]]:
        rows: Iterable[Tuple[Any, Any]]
        if isinstance(value, Mapping):
            rows = value.items()
        elif isinstance(value, Sequence) and not isinstance(
            value, (str, bytes, bytearray)
        ):
            rows = ((None, raw) for raw in value)
        else:
            rows = ()
        result = {}
        seen = set()
        for key, raw in rows:
            if not isinstance(raw, Mapping):
                continue
            uid = _safe_uint(raw.get("uid", raw.get("FoodUId", key)), 0, UINT64_MAX)
            food_id = _safe_uint(raw.get("food_id", raw.get("FoodId")), 0, UINT32_MAX)
            if not uid or uid in seen or food_id not in self.catalog.dishes:
                continue
            seen.add(uid)
            result[str(uid)] = {
                "uid": uid,
                "food_id": food_id,
                "count": max(
                    1, _safe_uint(raw.get("count", raw.get("Count")), 1, UINT32_MAX)
                ),
                "fondness": _safe_uint(
                    raw.get("fondness", raw.get("Fondness")),
                    self.catalog.dishes[food_id].fondness,
                    UINT32_MAX,
                ),
                "prop": self._normal_prop(raw.get("prop", raw.get("Prop"))),
                "created_at": _safe_uint(raw.get("created_at"), 0, UINT64_MAX),
            }
        return {key: result[key] for key in sorted(result, key=int)}

    def _normal_proficiency(
        self, value: Any, *, seed: bool
    ) -> Dict[str, Dict[str, int]]:
        source = value if isinstance(value, Mapping) else {}
        result = {}
        for dish_id, dish in self.catalog.dishes.items():
            if not dish.recipe:
                continue
            raw = _lookup(source, dish_id)
            if isinstance(raw, Mapping):
                count = _safe_uint(
                    raw.get("count", raw.get("skilled_degree")), 0, UINT32_MAX
                )
            else:
                count = _safe_uint(raw, 0, UINT32_MAX)
            if seed and raw is None:
                count = 1  # all-unlocked local cookbook, subsequently mutable
            result[str(dish_id)] = {"count": count, "level": _mastery_level(count)}
        return result

    def _normal_fish_instances(self, value: Any) -> Dict[str, Dict[str, int]]:
        if isinstance(value, Mapping):
            rows = value.items()
        elif isinstance(value, Sequence) and not isinstance(
            value, (str, bytes, bytearray)
        ):
            rows = ((None, raw) for raw in value)
        else:
            rows = ()
        result = {}
        seen = set()
        for key, raw in rows:
            if not isinstance(raw, Mapping):
                continue
            uid = _safe_uint(raw.get("uid", raw.get("FishUId", key)), 0, UINT64_MAX)
            fish_id = _safe_uint(raw.get("fish_id", raw.get("FishId")), 0, UINT32_MAX)
            if not uid or uid in seen:
                continue
            if self.catalog.fish_enabled and fish_id not in self.catalog.variants:
                continue
            count = max(
                1, _safe_uint(raw.get("count", raw.get("Count")), 1, UINT32_MAX)
            )
            tank_count = min(count, _safe_uint(raw.get("tank_count"), 0, UINT32_MAX))
            seen.add(uid)
            result[str(uid)] = {
                "uid": uid,
                "fish_id": fish_id,
                "count": count,
                "tank_count": tank_count,
                "star": _safe_uint(raw.get("star"), 0, UINT32_MAX),
                "shape": _safe_uint(raw.get("shape"), 0, UINT32_MAX),
                "length": _safe_uint(raw.get("length"), 0, UINT32_MAX),
                "scene_id": _safe_uint(raw.get("scene_id"), 0, UINT32_MAX),
                "caught_at": _safe_uint(raw.get("caught_at"), 0, UINT64_MAX),
            }
        return {key: result[key] for key in sorted(result, key=int)}

    def _normal_records(
        self,
        value: Any,
        instances: Mapping[str, Mapping[str, Any]],
        *,
        seed: bool,
    ) -> Dict[str, Dict[str, Any]]:
        source = value if isinstance(value, Mapping) else {}
        ids = (
            sorted(self.catalog.variants)
            if self.catalog.fish_enabled
            else sorted(
                {
                    _safe_uint(raw.get("fish_id"), 0, UINT32_MAX)
                    for raw in instances.values()
                    if isinstance(raw, Mapping)
                }
            )
        )
        totals: Counter[int] = Counter()
        tanks: Counter[int] = Counter()
        for raw in instances.values():
            fish_id = _safe_uint(raw.get("fish_id"), 0, UINT32_MAX)
            totals[fish_id] += _safe_uint(raw.get("count"), 0, UINT32_MAX)
            tanks[fish_id] += _safe_uint(raw.get("tank_count"), 0, UINT32_MAX)
        result = {}
        for fish_id in ids:
            raw = _lookup(source, fish_id)
            raw = raw if isinstance(raw, Mapping) else {}
            scene_ids = []
            raw_scenes = raw.get("scene_ids", raw.get("SceneIdList", []))
            if isinstance(raw_scenes, Sequence) and not isinstance(
                raw_scenes, (str, bytes, bytearray)
            ):
                scene_ids = sorted(
                    {
                        _safe_uint(scene, 0, UINT32_MAX)
                        for scene in raw_scenes
                        if _safe_uint(scene, 0, UINT32_MAX)
                    }
                )
            caught_total = max(
                totals[fish_id],
                _safe_uint(
                    raw.get("caught_total", raw.get("total_count")), 0, UINT64_MAX
                ),
            )
            result[str(fish_id)] = {
                "fish_id": fish_id,
                "total_count": totals[fish_id],
                "tank_count": min(totals[fish_id], tanks[fish_id]),
                "caught_total": caught_total,
                "max_star": _safe_uint(raw.get("max_star"), 0, UINT32_MAX),
                "max_shape": _safe_uint(raw.get("max_shape"), 0, UINT32_MAX),
                "place": _safe_uint(raw.get("place"), 0, UINT32_MAX),
                "escaped": _safe_uint(raw.get("escaped"), 0, UINT32_MAX),
                "scene_ids": scene_ids,
            }
        return result

    def _normal_scene_map(self, value: Any) -> Dict[str, Dict[str, Dict[str, int]]]:
        canonical = self._canonical_scene_map()
        if not self.catalog.fish_enabled:
            return copy.deepcopy(value) if isinstance(value, Mapping) else {}
        source = value if isinstance(value, Mapping) else {}
        for scene_key, positions in canonical.items():
            previous = _lookup(source, int(scene_key))
            if not isinstance(previous, Mapping):
                continue
            allowed_species = set(SCENE_SPECIES[int(scene_key)])
            for position_key in tuple(positions):
                raw = _lookup(previous, int(position_key))
                if not isinstance(raw, Mapping):
                    continue
                fish_id = _safe_uint(
                    raw.get("fish_id", raw.get("FishId")), 0, UINT32_MAX
                )
                variant = self.catalog.variants.get(fish_id)
                if variant is None or variant.species_id not in allowed_species:
                    continue
                position = int(position_key)
                positions[position_key] = {
                    "fish_id": fish_id,
                    "star": _safe_uint(
                        raw.get("star", raw.get("Star")), variant.star_min, UINT32_MAX
                    ),
                    "shape": _safe_uint(
                        raw.get("shape", raw.get("Shape")), 0, UINT32_MAX
                    ),
                    "length": _safe_uint(
                        raw.get("length", raw.get("Length")), 0, UINT32_MAX
                    ),
                    "scene_id": int(scene_key),
                    "position_id": position,
                }
        return canonical

    @staticmethod
    def _normal_attributes(value: Any) -> Dict[str, Any]:
        source = value if isinstance(value, Mapping) else {}
        limits = {
            "goodluck": (10, 1500),
            "power": (10, 1500),
            "toughness": (10, 1500),
            "skill": (10, 1500),
            "agile": (10, 1500),
            "fishtank_cost": (30, 100),
        }
        result: Dict[str, Any] = {}
        for name, (minimum, maximum) in limits.items():
            candidate = _safe_uint(source.get(name), minimum, maximum)
            result[name] = max(minimum, candidate)
        scenes = source.get("scene_ids", FISHING_SCENE_IDS)
        result["scene_ids"] = (
            sorted(
                set(FISHING_SCENE_IDS).intersection(
                    _safe_uint(value, 0, UINT32_MAX)
                    for value in scenes
                    if not isinstance(value, bool)
                )
            )
            if isinstance(scenes, Sequence) and not isinstance(scenes, (str, bytes))
            else list(FISHING_SCENE_IDS)
        )
        if not result["scene_ids"]:
            result["scene_ids"] = list(FISHING_SCENE_IDS)
        bait = source.get("unlocked_baits", (FISHING_BAIT_ITEM_IDS[0],))
        result["unlocked_baits"] = (
            sorted(
                set(FISHING_BAIT_ITEM_IDS).intersection(
                    _safe_uint(value, 0, UINT32_MAX)
                    for value in bait
                    if not isinstance(value, bool)
                )
            )
            if isinstance(bait, Sequence) and not isinstance(bait, (str, bytes))
            else [FISHING_BAIT_ITEM_IDS[0]]
        )
        if FISHING_BAIT_ITEM_IDS[0] not in result["unlocked_baits"]:
            result["unlocked_baits"].insert(0, FISHING_BAIT_ITEM_IDS[0])
        return result

    def make_state(self, profile: Mapping[str, Any]) -> Dict[str, Any]:
        previous = profile.get(LIFE_STATE_KEY)
        previous = previous if isinstance(previous, Mapping) else {}
        seed = (
            _safe_uint(previous.get("seed_version"), 0, UINT32_MAX) < LIFE_SEED_VERSION
        ) and not profile.get("player_progression")

        food_source = previous.get("food_inventory", profile.get("food_inventory", {}))
        food_inventory = self._normal_food_inventory(food_source)
        if seed and not food_inventory:
            baseline = self._base_food(self.catalog.dishes[1_976_032])
            food_inventory[str(baseline["uid"])] = baseline

        proficiency = self._normal_proficiency(
            previous.get("recipe_proficiency", profile.get("recipe_proficiency", {})),
            seed=seed,
        )
        fish_instances = self._normal_fish_instances(
            previous.get("fish_instances", profile.get("fish_instances", {}))
        )
        if seed and not fish_instances and self.catalog.fish_enabled:
            baseline_fish = self._base_fish_instance()
            fish_instances[str(baseline_fish["uid"])] = baseline_fish
        fish_records = self._normal_records(
            previous.get("fish_records", profile.get("fish_records", {})),
            fish_instances,
            seed=seed,
        )
        if seed and self.catalog.fish_enabled:
            baseline_record = fish_records[str(1_960_001)]
            baseline = next(iter(fish_instances.values()))
            baseline_record.update(
                {
                    "caught_total": max(1, baseline_record["caught_total"]),
                    "max_star": max(baseline_record["max_star"], baseline["star"]),
                    "max_shape": max(baseline_record["max_shape"], baseline["shape"]),
                    "place": baseline["scene_id"],
                    "scene_ids": sorted(
                        set(baseline_record["scene_ids"] + [baseline["scene_id"]])
                    ),
                }
            )

        scene_map = self._normal_scene_map(previous.get("scene_map", {}))
        scene_counts = {
            scene_id: len(positions) for scene_id, positions in scene_map.items()
        }
        aquarium = {
            fish_id: row["tank_count"]
            for fish_id, row in fish_records.items()
            if row["tank_count"] > 0
        }

        transactions = []
        raw_transactions = previous.get("transactions")
        if isinstance(raw_transactions, Sequence) and not isinstance(
            raw_transactions, (str, bytes, bytearray)
        ):
            transactions = [
                copy.deepcopy(dict(row))
                for row in raw_transactions
                if isinstance(row, Mapping)
            ]

        last_mutation = previous.get("last_mutation")
        if not isinstance(last_mutation, Mapping):
            last_mutation = {}
        else:
            last_mutation = copy.deepcopy(dict(last_mutation))
        pending = previous.get("pending_fishing")
        pending = copy.deepcopy(dict(pending)) if isinstance(pending, Mapping) else {}

        claimed = previous.get("claimed_fish_achievements", [])
        claimed_ids = (
            sorted(
                set(FISH_ACHIEVEMENTS).intersection(
                    _safe_uint(value, 0, UINT32_MAX)
                    for value in claimed
                    if not isinstance(value, bool)
                )
            )
            if isinstance(claimed, Sequence) and not isinstance(claimed, (str, bytes))
            else []
        )

        applications = previous.get("food_prop_applications")
        applications = (
            copy.deepcopy(dict(applications))
            if isinstance(applications, Mapping)
            else {}
        )
        max_food_uid = max((int(key) for key in food_inventory), default=FOOD_UID_BASE)
        max_fish_uid = max((int(key) for key in fish_instances), default=FISH_UID_BASE)

        return {
            "schema_version": LIFE_SCHEMA_VERSION,
            "seed_version": LIFE_SEED_VERSION,
            "catalog_version": self.content_catalog.version,
            "static_species_available": bool(self.catalog.fish_enabled),
            "static_species_reason": self.catalog.fish_disabled_reason,
            "food_inventory": food_inventory,
            "recipe_proficiency": proficiency,
            "food_prop_applications": applications,
            "fish_instances": fish_instances,
            "fish_records": fish_records,
            "scene_map": scene_map,
            "scene_counts": scene_counts,
            "aquarium": aquarium,
            "fishing_attributes": self._normal_attributes(
                previous.get("fishing_attributes")
            ),
            "claimed_fish_achievements": claimed_ids,
            "pending_fishing": pending,
            "fishing_sequence": _safe_uint(
                previous.get("fishing_sequence"), 0, UINT64_MAX
            ),
            "next_food_uid": max(
                max_food_uid + 1,
                _safe_uint(
                    previous.get("next_food_uid"), FOOD_UID_BASE + 1, UINT64_MAX
                ),
            ),
            "next_fish_uid": max(
                max_fish_uid + 1,
                _safe_uint(
                    previous.get("next_fish_uid"), FISH_UID_BASE + 1, UINT64_MAX
                ),
            ),
            "transaction_sequence": max(
                len(transactions),
                _safe_uint(previous.get("transaction_sequence"), 0, UINT64_MAX),
            ),
            "transactions": transactions,
            "last_mutation": last_mutation,
        }

    @staticmethod
    def _normal_balances(profile: Mapping[str, Any]) -> Dict[str, Any]:
        raw = profile.get("resource_balances")
        balances = copy.deepcopy(dict(raw)) if isinstance(raw, Mapping) else {}
        balances[FISH_SALE_RESOURCE_KEY] = _safe_uint(
            balances.get(FISH_SALE_RESOURCE_KEY), 0, UINT64_MAX
        )
        return balances

    @staticmethod
    def _state_changes(
        profile: Mapping[str, Any], state: Mapping[str, Any]
    ) -> Dict[str, Any]:
        return (
            {}
            if profile.get(LIFE_STATE_KEY) == state
            else {LIFE_STATE_KEY: copy.deepcopy(dict(state))}
        )

    def changes_for(self, profile: Mapping[str, Any]) -> Dict[str, Any]:
        state = self.make_state(profile)
        changes = self._state_changes(profile, state)
        balances = self._normal_balances(profile)
        if profile.get("resource_balances") != balances:
            changes["resource_balances"] = balances
        return changes

    def seed_profile(
        self, profile: MutableMapping[str, Any]
    ) -> MutableMapping[str, Any]:
        if not isinstance(profile, MutableMapping):
            raise TypeError("profile must be a mutable mapping")
        profile.update(self.changes_for(profile))
        return profile

    @staticmethod
    def _food_wire(row: Mapping[str, Any]) -> Dict[str, Any]:
        prop = row["prop"]
        return {
            "FoodUId": row["uid"],
            "FoodId": row["food_id"],
            "Count": row["count"],
            "Fondness": row["fondness"],
            "Prop": {
                "Round": prop["round"],
                "PropList": [
                    {
                        "Type": raw["type"],
                        "Level": raw["level"],
                        "PlusValue": raw["plus_value"],
                    }
                    for raw in prop["props"]
                ],
            },
        }

    def make_gourmet_datas_ret(self, state: Mapping[str, Any]) -> bytes:
        foods = state["food_inventory"]
        proficiency = state["recipe_proficiency"]
        return self.codec.encode(
            "SC_GourmetDatasRet",
            {
                "FoodList": [
                    self._food_wire(foods[key]) for key in sorted(foods, key=int)
                ],
                "SkilledDegreeMap": {
                    int(key): row["count"] for key, row in proficiency.items()
                },
            },
        )

    @staticmethod
    def _fish_record_wire(row: Mapping[str, Any]) -> Dict[str, Any]:
        return {
            "FishId": row["fish_id"],
            "Totalcount": row["total_count"],
            "Putfishtankcount": row["tank_count"],
            "MaxStar": row["max_star"],
            "MaxShape": row["max_shape"],
            "Place": row["place"],
            "EscapedFishNum": row["escaped"],
            "SceneIdList": row["scene_ids"],
        }

    @staticmethod
    def _scene_attr_wire(row: Mapping[str, Any]) -> Dict[str, Any]:
        return {
            "FishId": row["fish_id"],
            "Star": row["star"],
            "Shape": row["shape"],
            "Length": row["length"],
            "SceneId": row["scene_id"],
            "PositionId": row["position_id"],
        }

    def make_fish_datas_ret(self, state: Mapping[str, Any]) -> bytes:
        instances = state["fish_instances"]
        records = state["fish_records"]
        scene_map = state["scene_map"]
        return self.codec.encode(
            "SC_FishDatasRet",
            {
                "FishList": [
                    {
                        "FishUId": instances[key]["uid"],
                        "FishId": instances[key]["fish_id"],
                        "Count": instances[key]["count"],
                    }
                    for key in sorted(instances, key=int)
                ],
                "FishrecordList": [
                    self._fish_record_wire(records[key])
                    for key in sorted(records, key=int)
                ],
                "SceneMap": {
                    int(scene_id): {
                        "SceneFishAttrMap": {
                            int(position): self._scene_attr_wire(row)
                            for position, row in positions.items()
                        }
                    }
                    for scene_id, positions in scene_map.items()
                },
            },
        )

    @staticmethod
    def _fishing_attribute_wire(state: Mapping[str, Any]) -> Dict[str, Any]:
        attr = state["fishing_attributes"]
        bait_configs = [
            1_998_301 + (item_id - 1_010_001) for item_id in attr["unlocked_baits"]
        ]
        return {
            "Goodluck": attr["goodluck"],
            "Power": attr["power"],
            "Toughness": attr["toughness"],
            "Skill": attr["skill"],
            "Agile": attr["agile"],
            "Fishtankcost": attr["fishtank_cost"],
            "SceneIdList": attr["scene_ids"],
            "BaitIdList": bait_configs,
        }

    def _fishing_attribute_notification(self, state: Mapping[str, Any]) -> bytes:
        return self.codec.encode(
            "Notify_UpdateFishingAttribute",
            {"Attr": self._fishing_attribute_wire(state)},
        )

    def _resolve_materials(
        self,
        profile: Mapping[str, Any],
        decoded: Sequence[Tuple[int, int]],
    ) -> Tuple[
        Dict[str, Dict[str, int]], Tuple[IngredientDefinition, ...], Tuple[int, ...]
    ]:
        try:
            inventory = normalize_inventory(profile.get("inventory", {}))
        except InventoryContentError as exc:
            raise LifeProfileError(str(exc)) from exc
        by_uid = {row["uid"]: (key, row) for key, row in inventory.items()}
        requested: Counter[int] = Counter()
        for uid, count in decoded:
            requested[uid] += count
            if requested[uid] > UINT32_MAX:
                raise LifeProfileError("material quantity exceeds uint32")
        ingredients = []
        ingredient_ids = []
        for uid, count in requested.items():
            found = by_uid.get(uid)
            if found is None:
                raise LifeProfileError(f"unknown inventory ItemUId {uid}")
            _key, row = found
            if row["count"] < count:
                raise LifeProfileError(f"ItemUId {uid} has only {row['count']} units")
            definition = self.catalog.ingredients.get(row["id"])
            if definition is None:
                raise LifeProfileError(
                    f"a_item[{row['id']}] is not a cooking ingredient"
                )
            ingredients.extend([definition] * count)
            ingredient_ids.extend([definition.ingredient_id] * count)
        return inventory, tuple(ingredients), tuple(ingredient_ids)

    @staticmethod
    def _deduct_inventory(
        inventory: Dict[str, Dict[str, int]], decoded: Sequence[Tuple[int, int]]
    ) -> None:
        by_uid = {row["uid"]: row for row in inventory.values()}
        totals: Counter[int] = Counter()
        for uid, count in decoded:
            totals[uid] += count
        for uid, count in totals.items():
            row = by_uid[uid]
            row["count"] -= count

    @staticmethod
    def _cached(
        state: Mapping[str, Any], command: int, fingerprint: str
    ) -> Optional[Dict[str, Any]]:
        raw = state.get("last_mutation")
        if not isinstance(raw, Mapping):
            return None
        if raw.get("command") != command or raw.get("fingerprint") != fingerprint:
            return None
        response = raw.get("response")
        return copy.deepcopy(dict(response)) if isinstance(response, Mapping) else None

    def _record_mutation(
        self,
        state: Dict[str, Any],
        command: int,
        fingerprint: str,
        response: Mapping[str, Any],
        kind: str,
        details: Optional[Mapping[str, Any]] = None,
    ) -> None:
        sequence = state["transaction_sequence"] + 1
        state["transaction_sequence"] = sequence
        transaction = {
            "sequence": sequence,
            "command": command,
            "kind": kind,
            "fingerprint": fingerprint,
            "timestamp": self._now(),
        }
        if details:
            transaction.update(copy.deepcopy(dict(details)))
        state["transactions"].append(transaction)
        state["last_mutation"] = {
            "command": command,
            "fingerprint": fingerprint,
            "response": copy.deepcopy(dict(response)),
        }

    def _error(
        self,
        command: int,
        message_name: str,
        values: Mapping[str, Any],
        reason: str,
        error_code: int = ERROR_MESSAGE_DATA_ILLEGAL,
    ) -> LifeResult:
        payload = dict(values)
        if "Errorcode" in {
            field["name"] for field in self.codec.schema(message_name)["fields"]
        }:
            payload["Errorcode"] = error_code
        return LifeResult(
            RESPONSE_IDS[command],
            self.codec.encode(message_name, payload),
            error_code=error_code,
            supported=False,
            reason=reason,
        )

    def _mutation_result(
        self,
        command: int,
        message_name: str,
        response: Mapping[str, Any],
        profile: Mapping[str, Any],
        state: Mapping[str, Any],
        *,
        extra_changes: Optional[Mapping[str, Any]] = None,
        notifications: Tuple[Tuple[int, bytes], ...] = (),
    ) -> LifeResult:
        changes = self._state_changes(profile, state)
        if extra_changes:
            changes.update(copy.deepcopy(dict(extra_changes)))
        if command in (CS_PUT_OR_TAKEOUT_FISH, CS_GO_FISHING_RESULT, CS_SELL_FISH):
            # The source runtime replaces records on 522. The following
            # response only animates/refreshes from those records; it does not
            # update their counts. Absolute records also make retry delivery
            # safe, including clearing the tank to zero.
            records = state["fish_records"]
            notification = self.codec.encode("Notify_UpdateFishrecord", {
                "FishrecordList": [self._fish_record_wire(records[key])
                                   for key in sorted(records, key=int)]})
            notifications = ((NOTIFY_UPDATE_FISH_RECORD, notification),) + notifications
        return LifeResult(
            RESPONSE_IDS[command],
            self.codec.encode(message_name, response),
            changes=changes,
            notifications=notifications,
        )

    def _handle_match(self, profile: Mapping[str, Any], body: bytes) -> LifeResult:
        try:
            decoded = _decode_materials(body, "CS_MatchRecipe")
            _inventory, _ingredients, ingredient_ids = self._resolve_materials(
                profile, decoded
            )
            dish = self.catalog.match_recipe(ingredient_ids)
            if dish is None:
                raise LifeProfileError("selected ingredients match no configured recipe")
        except (ProtobufDecodeError, LifeProfileError) as exc:
            return self._error(
                CS_MATCH_RECIPE,
                "SC_MatchRecipeRet",
                {"FoodId": 1_976_001},
                str(exc),
            )
        return LifeResult(
            SC_MATCH_RECIPE_RET,
            self.codec.encode("SC_MatchRecipeRet", {"Errorcode": 0, "FoodId": dish.id}),
        )

    def _handle_cook(self, profile: Mapping[str, Any], body: bytes) -> LifeResult:
        state = self.make_state(profile)
        fingerprint = _fingerprint(CS_COOK_FOOD, body)
        cached = self._cached(state, CS_COOK_FOOD, fingerprint)
        if cached is not None:
            return self._mutation_result(
                CS_COOK_FOOD, "SC_CookFoodRet", cached, profile, state
            )
        try:
            decoded = _decode_materials(body, "CS_CookFood")
            inventory, ingredients, ingredient_ids = self._resolve_materials(
                profile, decoded
            )
            dish = self.catalog.match_recipe(ingredient_ids)
            if dish is None:
                raise LifeProfileError("selected ingredients match no configured recipe")
        except (ProtobufDecodeError, LifeProfileError) as exc:
            return self._error(
                CS_COOK_FOOD,
                "SC_CookFoodRet",
                {"FoodId": 1_976_001, "SkilledDegree": 0, "FoodUId": 0},
                str(exc),
            )

        self._deduct_inventory(inventory, decoded)
        uid = state["next_food_uid"]
        if uid > UINT64_MAX:
            return self._error(
                CS_COOK_FOOD,
                "SC_CookFoodRet",
                {"FoodId": dish.id, "SkilledDegree": 0, "FoodUId": 0},
                "FoodUId namespace is exhausted",
                ERROR_EXCEED_ITEM_LIMIT_COUNT,
            )
        state["next_food_uid"] = uid + 1
        prop = self.catalog.food_prop(dish, ingredients)
        food = {
            "uid": uid,
            "food_id": dish.id,
            "count": 1,
            "fondness": dish.fondness,
            "prop": prop,
            "created_at": self._now(),
        }
        state["food_inventory"][str(uid)] = food
        proficiency = state["recipe_proficiency"][str(dish.id)]
        proficiency["count"] = min(UINT32_MAX, proficiency["count"] + 1)
        proficiency["level"] = _mastery_level(proficiency["count"])
        response = {
            "Errorcode": 0,
            "FoodId": dish.id,
            "SkilledDegree": proficiency["count"],
            "FoodUId": uid,
        }
        self._record_mutation(
            state,
            CS_COOK_FOOD,
            fingerprint,
            response,
            "cook_food",
            {
                "food_uid": uid,
                "food_id": dish.id,
                "materials": [
                    {"item_uid": item_uid, "count": count}
                    for item_uid, count in decoded
                ],
            },
        )
        notification = self.codec.encode(
            "Notify_UpdateFood", {"FoodList": [self._food_wire(food)]}
        )
        return self._mutation_result(
            CS_COOK_FOOD,
            "SC_CookFoodRet",
            response,
            profile,
            state,
            extra_changes={"inventory": inventory},
            notifications=((NOTIFY_UPDATE_FOOD, notification),),
        )

    def _hero_exists(self, profile: Mapping[str, Any], hero_uid: int) -> bool:
        heroes = profile.get("heroes")
        if not isinstance(heroes, Sequence) or isinstance(heroes, (str, bytes)):
            return False
        return any(
            isinstance(row, Mapping)
            and _safe_uint(row.get("uid"), 0, UINT64_MAX) == hero_uid
            for row in heroes
        )

    def feeding_notifications(self, profile, hero_uid, food_uid):
        state = self.make_state(profile)
        food = state['food_inventory'].get(str(food_uid))
        notices = [(278, self.codec.encode('Notify_UpdateFood', {'FoodList': [self._food_wire(food)]}))] if food else [
            (279, self.codec.encode('Notify_RemoveFood', {'FoodIdList': [food_uid]}))]
        hero = next((r for r in profile.get('heroes', []) if r['uid'] == hero_uid), None)
        if hero:
            notices.append((280, self.codec.encode('Notify_UpdateHeroFoodProps', {
                'HeroUId': hero_uid, 'PlatesFoodPropList': hero.get('plates_food_prop_list', [])})))
            notices.append(self.hero_progression._fondness_notification(profile, hero))
        return tuple(notices)

    def _handle_hero_eat(self, profile, body):
        """Consume one owned plate and transfer its actual properties.

        Offline policy: use the plate's catalog-derived base Fondness, without
        inventing the absent source server's preference multipliers. Flavor
        is derived from a_fond_dish, never trusted from the request. The runtime
        uses the queued APlateFoodProp payloads on its food-assisted attack.
        """
        values = {'Errorcode': 0, 'HeroUId': 0, 'State': 0, 'Fondness': 0, 'Flavour': 0}
        try:
            plain = bytearray()
            saw_identity = False
            for number, wire, raw in iter_wire_fields(bytes(body)):
                if number == 127:
                    if saw_identity or wire != 2 or len(raw) != 32 or any(b not in b'0123456789abcdef' for b in raw):
                        raise ProtobufDecodeError('invalid feeding operation identity')
                    saw_identity = True
                    continue
                # Preserve strict request parsing (including duplicate fields).
                if wire != 0: raise ProtobufDecodeError('invalid feeding field')
                plain.extend(field_varint(number, int(raw)))
            request = _decode_uints(bytes(plain), 'CS_HeroEat', {1: ('HeroUId', UINT64_MAX),
                2: ('FoodUId', UINT64_MAX), 3: ('Flavour', 2)}, defaults={3: 0})
            uid, food_uid = request['HeroUId'], request['FoodUId']
            values['HeroUId'] = uid
            if not uid or not food_uid or not self._hero_exists(profile, uid):
                return self._error(218, 'SC_HeroEatRet', values, 'unknown owned hero or food', ERROR_HERO_NOT_FOUND)
            state = self.make_state(profile)
            food = state['food_inventory'].get(str(food_uid))
            if not food or food['count'] < 1:
                return self._error(218, 'SC_HeroEatRet', values, 'food no longer owned', 110)
            if profile.get('battle_runtime', {}).get('active') or profile.get('tower_battles', {}).get('active_battle'):
                return self._error(218, 'SC_HeroEatRet', values, 'cannot feed during battle', 32000)
            _, heroes, index, hero, definition = self.hero_progression._owned_hero(profile, uid)
            preference = self.content_catalog.record('a_fond_dish', int(hero['id']))
            flavour = next((n for n, key in enumerate(('like', 'moderate', 'dislike'))
                            if food['food_id'] in json.loads(preference[key])), None)
            if flavour is None: raise LifeProfileError('missing food preference')
            if not definition.favor_levels: raise LifeProfileError('missing hero favor levels')
            maximum = max(r.cumulative_exp for r in definition.favor_levels.values())
            gain = min(food['fondness'], max(0, maximum - int(hero.get('fondness', 0))))
            hero['fondness'] = int(hero.get('fondness', 0)) + gain
            eaten = copy.deepcopy(hero.get('eaten_food_map', {}))
            count = int(eaten.get(str(food['food_id']), 0)) + 1
            if count > UINT32_MAX: raise LifeProfileError('eaten food count overflow')
            eaten[str(food['food_id'])] = count
            hero['eaten_food_map'] = eaten
            day = (self._now() + 8 * 3600) // 86400
            hero['daily_feed_num'] = (int(hero.get('daily_feed_num', 0)) if hero.get('feed_day') == day else 0) + 1
            if hero['daily_feed_num'] > UINT32_MAX: raise LifeProfileError('feed count overflow')
            hero['feed_day'] = day
            plates = copy.deepcopy(hero.get('plates_food_prop_list', []))
            prop = self._food_wire(food)['Prop']
            if prop['PropList']: plates.append(prop)
            hero['plates_food_prop_list'] = plates
            heroes[index] = hero
            food['count'] -= 1
            if not food['count']: del state['food_inventory'][str(food_uid)]
            values.update(State=int(hero.get('state', 0)), Fondness=gain, Flavour=flavour)
            self._record_mutation(state, 218, _fingerprint(218, body), values, 'hero_eat', {
                'hero_uid': uid, 'food_uid': food_uid, 'food_id': food['food_id'], 'count': 1,
                'fondness': gain, 'flavour': flavour, 'policy_version': 1})
            after = {**profile, LIFE_STATE_KEY: state, 'heroes': heroes}
            return self._mutation_result(218, 'SC_HeroEatRet', values, profile, state,
                extra_changes={'heroes': heroes}, notifications=self.feeding_notifications(after, uid, food_uid))
        except (ValueError, LookupError, TypeError, AttributeError) as exc:
            return self._error(218, 'SC_HeroEatRet', values, str(exc))

    def _handle_use_food_props(
        self, profile: Mapping[str, Any], body: bytes
    ) -> LifeResult:
        try:
            request = _decode_uints(
                body, "CS_UseFoodProps", {1: ("HeroUId", UINT64_MAX)}
            )
        except ProtobufDecodeError as exc:
            return self._error(
                CS_USE_FOOD_PROPS, "SC_UseFoodPropsRet", {"HeroUId": 0}, str(exc)
            )
        hero_uid = request["HeroUId"]
        if not hero_uid or not self._hero_exists(profile, hero_uid):
            return self._error(
                CS_USE_FOOD_PROPS,
                "SC_UseFoodPropsRet",
                {"HeroUId": hero_uid},
                f"unknown account HeroUId {hero_uid}",
                ERROR_HERO_NOT_FOUND,
            )
        state = self.make_state(profile)
        fingerprint = _fingerprint(CS_USE_FOOD_PROPS, body)
        cached = self._cached(state, CS_USE_FOOD_PROPS, fingerprint)
        if cached is not None:
            return self._mutation_result(
                CS_USE_FOOD_PROPS, "SC_UseFoodPropsRet", cached, profile, state
            )
        previous = state["food_prop_applications"].get(str(hero_uid), {})
        generation = _safe_uint(previous.get("generation"), 0, UINT64_MAX) + 1
        state["food_prop_applications"][str(hero_uid)] = {
            "generation": generation,
            "applied_at": self._now(),
        }
        response = {"Errorcode": 0, "HeroUId": hero_uid}
        self._record_mutation(
            state,
            CS_USE_FOOD_PROPS,
            fingerprint,
            response,
            "apply_food_props",
            {"hero_uid": hero_uid, "generation": generation},
        )
        heroes = copy.deepcopy(profile.get('heroes', []))
        hero = next(r for r in heroes if r['uid'] == hero_uid)
        # FightControll.StartAttack sends 281 when PredictData.UseFood is
        # true. These are previously eaten plate effects, not bag food.
        hero['plates_food_prop_list'] = []
        return self._mutation_result(
            CS_USE_FOOD_PROPS, "SC_UseFoodPropsRet", response, profile, state,
            extra_changes={'heroes': heroes}, notifications=((280, self.codec.encode(
                'Notify_UpdateHeroFoodProps', {'HeroUId': hero_uid, 'PlatesFoodPropList': []})),)
        )

    def _ensure_fish_static(
        self, command: int, message: str, values: Mapping[str, Any]
    ) -> Optional[LifeResult]:
        if self.catalog.fish_enabled:
            return None
        return self._error(
            command,
            message,
            values,
            self.catalog.fish_disabled_reason
            or "no configured fish species are available",
        )

    def _handle_go_fishing(self, profile: Mapping[str, Any], body: bytes) -> LifeResult:
        try:
            request = _decode_uints(
                body,
                "CS_GoFishing",
                {1: ("Fishery", UINT32_MAX), 2: ("BaitItemId", UINT32_MAX)},
            )
        except ProtobufDecodeError as exc:
            return self._error(
                CS_GO_FISHING,
                "SC_GoFishingRet",
                {"FishId": 0, "Star": 0, "Shape": 0, "Length": 0, "Attr": {}},
                str(exc),
            )
        static_error = self._ensure_fish_static(
            CS_GO_FISHING,
            "SC_GoFishingRet",
            {"FishId": 0, "Star": 0, "Shape": 0, "Length": 0, "Attr": {}},
        )
        if static_error:
            return static_error
        state = self.make_state(profile)
        fingerprint = _fingerprint(CS_GO_FISHING, body)
        cached = self._cached(state, CS_GO_FISHING, fingerprint)
        if cached is not None:
            return self._mutation_result(
                CS_GO_FISHING, "SC_GoFishingRet", cached, profile, state
            )
        scene_id = request["Fishery"]
        bait_item_id = request["BaitItemId"]
        if scene_id not in FISHING_SCENE_IDS:
            return self._error(
                CS_GO_FISHING,
                "SC_GoFishingRet",
                {"FishId": 0, "Star": 0, "Shape": 0, "Length": 0, "Attr": {}},
                f"unknown fishing scene {scene_id}",
            )
        if bait_item_id not in state["fishing_attributes"]["unlocked_baits"]:
            return self._error(
                CS_GO_FISHING,
                "SC_GoFishingRet",
                {"FishId": 0, "Star": 0, "Shape": 0, "Length": 0, "Attr": {}},
                f"bait item {bait_item_id} is not unlocked",
            )
        if state["pending_fishing"] and not state["pending_fishing"].get("settled"):
            return self._error(
                CS_GO_FISHING,
                "SC_GoFishingRet",
                {"FishId": 0, "Star": 0, "Shape": 0, "Length": 0, "Attr": {}},
                "a fishing encounter is already pending",
            )
        try:
            inventory = normalize_inventory(profile.get("inventory", {}))
        except InventoryContentError as exc:
            return self._error(
                CS_GO_FISHING,
                "SC_GoFishingRet",
                {"FishId": 0, "Star": 0, "Shape": 0, "Length": 0, "Attr": {}},
                str(exc),
            )
        bait_row = inventory.get(str(bait_item_id))
        if not isinstance(bait_row, Mapping) or bait_row["count"] <= 0:
            return self._error(
                CS_GO_FISHING,
                "SC_GoFishingRet",
                {"FishId": 0, "Star": 0, "Shape": 0, "Length": 0, "Attr": {}},
                f"bait item {bait_item_id} is unavailable",
            )
        inventory[str(bait_item_id)]["count"] -= 1

        positions = state["scene_map"][str(scene_id)]
        bait_type = 0 if bait_item_id == 1_010_001 else bait_item_id - 1_010_001
        eligible = [
            position
            for position, raw in positions.items()
            if bait_type == 0
            or self.catalog.species[
                self.catalog.variants[raw["fish_id"]].species_id
            ].bait_type
            == bait_type
        ]
        if not eligible:
            return self._error(
                CS_GO_FISHING,
                "SC_GoFishingRet",
                {"FishId": 0, "Star": 0, "Shape": 0, "Length": 0, "Attr": {}},
                "the selected bait has no configured fish in this scene",
            )
        sequence = state["fishing_sequence"]
        position = eligible[sequence % len(eligible)]
        scene_fish = positions[position]
        variant = self.catalog.variants[scene_fish["fish_id"]]
        species = self.catalog.species[variant.species_id]
        star = variant.star_min
        shape = species.shape_min + (
            sequence % (species.shape_max - species.shape_min + 1)
        )
        length = species.length_min + (
            sequence % (species.length_max - species.length_min + 1)
        )
        encounter_id = sequence + 1
        state["fishing_sequence"] = encounter_id
        state["pending_fishing"] = {
            "encounter_id": encounter_id,
            "fishery": scene_id,
            "bait_item_id": bait_item_id,
            "index": int(position),
            "fish_id": variant.id,
            "star": star,
            "shape": shape,
            "length": length,
            "settled": False,
        }
        response = {
            "Errorcode": 0,
            "FishId": variant.id,
            "Star": star,
            "Shape": shape,
            "Length": length,
            "Attr": self._fishing_attribute_wire(state),
        }
        self._record_mutation(
            state,
            CS_GO_FISHING,
            fingerprint,
            response,
            "start_fishing",
            {
                "encounter_id": encounter_id,
                "scene_id": scene_id,
                "bait_item_id": bait_item_id,
            },
        )
        return self._mutation_result(
            CS_GO_FISHING,
            "SC_GoFishingRet",
            response,
            profile,
            state,
            extra_changes={"inventory": inventory},
        )

    def _aquarium_cost(self, state: Mapping[str, Any]) -> int:
        return sum(
            self.catalog.variants[int(fish_id)].fish_cost * count
            for fish_id, count in state["aquarium"].items()
        )

    def _handle_aquarium(self, profile: Mapping[str, Any], body: bytes) -> LifeResult:
        try:
            request = _decode_uints(
                body,
                "CS_PutOrTakeoutFish",
                {
                    1: ("Opt", UINT32_MAX),
                    2: ("FishId", UINT32_MAX),
                    3: ("Count", UINT32_MAX),
                },
                # Proto3 omits scalar defaults. Opt=0 is take-out, while the
                # clear operation legitimately sends FishId/Count as zero.
                defaults={1: 0, 2: 0, 3: 0},
            )
        except ProtobufDecodeError as exc:
            return self._error(
                CS_PUT_OR_TAKEOUT_FISH,
                "SC_PutOrTakeoutFishRet",
                {"Opt": 0, "FishId": 0},
                str(exc),
            )
        static_error = self._ensure_fish_static(
            CS_PUT_OR_TAKEOUT_FISH,
            "SC_PutOrTakeoutFishRet",
            {"Opt": request["Opt"], "FishId": request["FishId"]},
        )
        if static_error:
            return static_error
        state = self.make_state(profile)
        fingerprint = _fingerprint(CS_PUT_OR_TAKEOUT_FISH, body)
        cached = self._cached(state, CS_PUT_OR_TAKEOUT_FISH, fingerprint)
        if cached is not None:
            return self._mutation_result(
                CS_PUT_OR_TAKEOUT_FISH,
                "SC_PutOrTakeoutFishRet",
                cached,
                profile,
                state,
            )
        opt, fish_id, count = request["Opt"], request["FishId"], request["Count"]
        if opt not in (0, 1, 2):
            return self._error(
                CS_PUT_OR_TAKEOUT_FISH,
                "SC_PutOrTakeoutFishRet",
                {"Opt": opt, "FishId": fish_id},
                f"unknown aquarium Opt {opt}",
            )
        if opt == 2:
            for instance in state["fish_instances"].values():
                instance["tank_count"] = 0
            for record in state["fish_records"].values():
                record["tank_count"] = 0
            state["aquarium"] = {}
        else:
            if fish_id not in self.catalog.variants or count <= 0:
                return self._error(
                    CS_PUT_OR_TAKEOUT_FISH,
                    "SC_PutOrTakeoutFishRet",
                    {"Opt": opt, "FishId": fish_id},
                "aquarium fish and count must be configured and positive",
                )
            record = state["fish_records"][str(fish_id)]
            if opt == 1:
                available = record["total_count"] - record["tank_count"]
                added_cost = self.catalog.variants[fish_id].fish_cost * count
                if count > available:
                    return self._error(
                        CS_PUT_OR_TAKEOUT_FISH,
                        "SC_PutOrTakeoutFishRet",
                        {"Opt": opt, "FishId": fish_id},
                        f"only {available} fish are outside the aquarium",
                    )
                if (
                    self._aquarium_cost(state) + added_cost
                    > state["fishing_attributes"]["fishtank_cost"]
                ):
                    return self._error(
                        CS_PUT_OR_TAKEOUT_FISH,
                        "SC_PutOrTakeoutFishRet",
                        {"Opt": opt, "FishId": fish_id},
                        "aquarium COST capacity would be exceeded",
                    )
                remaining = count
                for instance in state["fish_instances"].values():
                    if instance["fish_id"] != fish_id:
                        continue
                    free = instance["count"] - instance["tank_count"]
                    moved = min(free, remaining)
                    instance["tank_count"] += moved
                    remaining -= moved
                    if not remaining:
                        break
                record["tank_count"] += count
            else:
                if count > record["tank_count"]:
                    return self._error(
                        CS_PUT_OR_TAKEOUT_FISH,
                        "SC_PutOrTakeoutFishRet",
                        {"Opt": opt, "FishId": fish_id},
                        f"only {record['tank_count']} fish are in the aquarium",
                    )
                remaining = count
                for instance in reversed(list(state["fish_instances"].values())):
                    if instance["fish_id"] != fish_id:
                        continue
                    moved = min(instance["tank_count"], remaining)
                    instance["tank_count"] -= moved
                    remaining -= moved
                    if not remaining:
                        break
                record["tank_count"] -= count
            state["aquarium"] = {
                key: row["tank_count"]
                for key, row in state["fish_records"].items()
                if row["tank_count"] > 0
            }
        response = {"Errorcode": 0, "Opt": opt, "FishId": fish_id}
        self._record_mutation(
            state,
            CS_PUT_OR_TAKEOUT_FISH,
            fingerprint,
            response,
            ("clear_aquarium" if opt == 2 else "put_fish" if opt == 1 else "take_fish"),
            {"fish_id": fish_id, "count": count},
        )
        return self._mutation_result(
            CS_PUT_OR_TAKEOUT_FISH,
            "SC_PutOrTakeoutFishRet",
            response,
            profile,
            state,
        )

    def _replacement_scene_fish(
        self, state: Mapping[str, Any], scene_id: int, position: int
    ) -> Dict[str, int]:
        species_ids = SCENE_SPECIES[scene_id]
        sequence = state["fishing_sequence"]
        species_id = species_ids[(position + sequence) % len(species_ids)]
        ordinal = species_id - 1_974_000
        grade = 1 + (sequence % 3)
        variant_id = 1_960_001 + (ordinal - 1) * 3 + grade - 1
        variant = self.catalog.variants[variant_id]
        species = self.catalog.species[species_id]
        return {
            "fish_id": variant_id,
            "star": variant.star_min,
            "shape": species.shape_min,
            "length": species.length_min,
            "scene_id": scene_id,
            "position_id": position,
        }

    def _handle_fishing_result(
        self, profile: Mapping[str, Any], body: bytes
    ) -> LifeResult:
        try:
            request = _decode_uints(
                body,
                "CS_GoFishingResult",
                {
                    1: ("Fishery", UINT32_MAX),
                    2: ("Index", INT32_MAX),
                    3: ("BaitItemId", UINT32_MAX),
                    4: ("Status", UINT32_MAX),
                },
                # A successful catch is Status=0 and the first scene slot is
                # Index=0; both are absent on the real proto3 wire.
                defaults={2: 0, 4: 0},
            )
        except ProtobufDecodeError as exc:
            return self._error(
                CS_GO_FISHING_RESULT,
                "SC_GoFishingResultRet",
                {"Fishery": 0, "Index": 0, "NewFishAttr": {}},
                str(exc),
            )
        static_error = self._ensure_fish_static(
            CS_GO_FISHING_RESULT,
            "SC_GoFishingResultRet",
            {
                "Fishery": request["Fishery"],
                "Index": request["Index"],
                "NewFishAttr": {},
            },
        )
        if static_error:
            return static_error
        state = self.make_state(profile)
        fingerprint = _fingerprint(CS_GO_FISHING_RESULT, body)
        cached = self._cached(state, CS_GO_FISHING_RESULT, fingerprint)
        if cached is not None:
            return self._mutation_result(
                CS_GO_FISHING_RESULT,
                "SC_GoFishingResultRet",
                cached,
                profile,
                state,
            )
        pending = state["pending_fishing"]
        if (
            not pending
            or pending.get("settled")
            or request["Fishery"] != pending.get("fishery")
            or request["Index"] != pending.get("index")
            or request["BaitItemId"] != pending.get("bait_item_id")
            or request["Status"] not in (0, 1)
        ):
            return self._error(
                CS_GO_FISHING_RESULT,
                "SC_GoFishingResultRet",
                {
                    "Fishery": request["Fishery"],
                    "Index": request["Index"],
                    "NewFishAttr": {},
                },
                "fishing result does not match the pending encounter",
            )
        fish_id = pending["fish_id"]
        if request["Status"] == 0:
            uid = state["next_fish_uid"]
            if uid > UINT64_MAX:
                return self._error(
                    CS_GO_FISHING_RESULT,
                    "SC_GoFishingResultRet",
                    {
                        "Fishery": request["Fishery"],
                        "Index": request["Index"],
                        "NewFishAttr": {},
                    },
                    "FishUId namespace is exhausted",
                    ERROR_EXCEED_ITEM_LIMIT_COUNT,
                )
            state["next_fish_uid"] = uid + 1
            state["fish_instances"][str(uid)] = {
                "uid": uid,
                "fish_id": fish_id,
                "count": 1,
                "tank_count": 0,
                "star": pending["star"],
                "shape": pending["shape"],
                "length": pending["length"],
                "scene_id": pending["fishery"],
                "caught_at": self._now(),
            }
            record = state["fish_records"][str(fish_id)]
            record["total_count"] += 1
            record["caught_total"] += 1
            record["max_star"] = max(record["max_star"], pending["star"])
            record["max_shape"] = max(record["max_shape"], pending["shape"])
            record["place"] = pending["fishery"]
            record["scene_ids"] = sorted(
                set(record["scene_ids"] + [pending["fishery"]])
            )
            kind = "catch_fish"
        else:
            uid = 0
            kind = "fish_escaped"
        pending["settled"] = True
        pending["status"] = request["Status"]
        replacement = self._replacement_scene_fish(
            state, request["Fishery"], request["Index"]
        )
        state["scene_map"][str(request["Fishery"])][str(request["Index"])] = replacement
        state["scene_counts"][str(request["Fishery"])] = len(
            state["scene_map"][str(request["Fishery"])]
        )
        response = {
            "Errorcode": 0,
            "Fishery": request["Fishery"],
            "Index": request["Index"],
            "NewFishAttr": self._scene_attr_wire(replacement),
        }
        self._record_mutation(
            state,
            CS_GO_FISHING_RESULT,
            fingerprint,
            response,
            kind,
            {
                "encounter_id": pending["encounter_id"],
                "fish_uid": uid,
                "fish_id": fish_id,
                "status": request["Status"],
            },
        )
        return self._mutation_result(
            CS_GO_FISHING_RESULT,
            "SC_GoFishingResultRet",
            response,
            profile,
            state,
        )

    def _achievement_complete(
        self, state: Mapping[str, Any], achievement_id: int
    ) -> bool:
        mode, species_ids, _attr, _value, _bait = FISH_ACHIEVEMENTS[achievement_id]
        records = state["fish_records"]
        for species_id in species_ids:
            ordinal = species_id - 1_974_000
            variants = [1_960_001 + (ordinal - 1) * 3 + index for index in range(3)]
            counts = [
                records[str(variant_id)]["caught_total"] for variant_id in variants
            ]
            if mode == "all" and not all(count > 0 for count in counts):
                return False
            if mode == "any" and not any(count > 0 for count in counts):
                return False
        return True

    def _handle_achievement(
        self, profile: Mapping[str, Any], body: bytes
    ) -> LifeResult:
        try:
            request = _decode_uints(
                body,
                "CS_GetFishAchieveReward",
                {1: ("FishAchievementId", UINT32_MAX)},
            )
        except ProtobufDecodeError as exc:
            return self._error(
                CS_GET_FISH_ACHIEVE_REWARD,
                "SC_GetFishAchieveRewardRet",
                {"FishAchievementId": 0},
                str(exc),
            )
        achievement_id = request["FishAchievementId"]
        static_error = self._ensure_fish_static(
            CS_GET_FISH_ACHIEVE_REWARD,
            "SC_GetFishAchieveRewardRet",
            {"FishAchievementId": achievement_id},
        )
        if static_error:
            return static_error
        if achievement_id not in FISH_ACHIEVEMENTS:
            return self._error(
                CS_GET_FISH_ACHIEVE_REWARD,
                "SC_GetFishAchieveRewardRet",
                {"FishAchievementId": achievement_id},
                f"unknown fish achievement {achievement_id}",
            )
        state = self.make_state(profile)
        fingerprint = _fingerprint(CS_GET_FISH_ACHIEVE_REWARD, body)
        cached = self._cached(state, CS_GET_FISH_ACHIEVE_REWARD, fingerprint)
        if cached is not None:
            return self._mutation_result(
                CS_GET_FISH_ACHIEVE_REWARD,
                "SC_GetFishAchieveRewardRet",
                cached,
                profile,
                state,
            )
        response = {"Errorcode": 0, "FishAchievementId": achievement_id}
        if achievement_id in state["claimed_fish_achievements"]:
            return self._mutation_result(
                CS_GET_FISH_ACHIEVE_REWARD,
                "SC_GetFishAchieveRewardRet",
                response,
                profile,
                state,
            )
        if not self._achievement_complete(state, achievement_id):
            return self._error(
                CS_GET_FISH_ACHIEVE_REWARD,
                "SC_GetFishAchieveRewardRet",
                {"FishAchievementId": achievement_id},
                "fish achievement condition is not complete",
                ERROR_NO_REWARD_CAN_BE_TAKEN,
            )
        _mode, _species, attr_type, amount, bait = FISH_ACHIEVEMENTS[achievement_id]
        if attr_type == 1:
            state["fishing_attributes"]["goodluck"] = min(
                1500, state["fishing_attributes"]["goodluck"] + amount
            )
        elif attr_type == 6:
            state["fishing_attributes"]["fishtank_cost"] = min(
                100, state["fishing_attributes"]["fishtank_cost"] + amount
            )
        if bait:
            state["fishing_attributes"]["unlocked_baits"] = sorted(
                set(state["fishing_attributes"]["unlocked_baits"] + [bait])
            )
        state["claimed_fish_achievements"].append(achievement_id)
        state["claimed_fish_achievements"].sort()
        self._record_mutation(
            state,
            CS_GET_FISH_ACHIEVE_REWARD,
            fingerprint,
            response,
            "claim_fish_achievement",
            {
                "achievement_id": achievement_id,
                "attribute_type": attr_type,
                "amount": amount,
                "bait_item_id": bait,
            },
        )
        notification = self._fishing_attribute_notification(state)
        return self._mutation_result(
            CS_GET_FISH_ACHIEVE_REWARD,
            "SC_GetFishAchieveRewardRet",
            response,
            profile,
            state,
            notifications=((NOTIFY_UPDATE_FISHING_ATTRIBUTE, notification),),
        )

    def _handle_sell_fish(self, profile: Mapping[str, Any], body: bytes) -> LifeResult:
        try:
            request = _decode_uints(
                body,
                "CS_SellFish",
                {1: ("FishId", UINT32_MAX), 2: ("Count", UINT32_MAX)},
            )
        except ProtobufDecodeError as exc:
            return self._error(
                CS_SELL_FISH, "SC_SellFishRet", {"FishId": 0, "Count": 0}, str(exc)
            )
        fish_id, count = request["FishId"], request["Count"]
        static_error = self._ensure_fish_static(
            CS_SELL_FISH,
            "SC_SellFishRet",
            {"FishId": fish_id, "Count": count},
        )
        if static_error:
            return static_error
        if fish_id not in self.catalog.variants or count <= 0:
            return self._error(
                CS_SELL_FISH,
                "SC_SellFishRet",
                {"FishId": fish_id, "Count": count},
                "fish sale requires a configured variant and positive count",
            )
        state = self.make_state(profile)
        fingerprint = _fingerprint(CS_SELL_FISH, body)
        cached = self._cached(state, CS_SELL_FISH, fingerprint)
        if cached is not None:
            return self._mutation_result(
                CS_SELL_FISH, "SC_SellFishRet", cached, profile, state
            )
        record = state["fish_records"][str(fish_id)]
        available = record["total_count"] - record["tank_count"]
        if count > available:
            return self._error(
                CS_SELL_FISH,
                "SC_SellFishRet",
                {"FishId": fish_id, "Count": count},
                f"only {available} fish are sellable outside the aquarium",
            )
        remaining = count
        for uid in sorted(tuple(state["fish_instances"]), key=int):
            instance = state["fish_instances"][uid]
            if instance["fish_id"] != fish_id:
                continue
            sellable = instance["count"] - instance["tank_count"]
            removed = min(sellable, remaining)
            instance["count"] -= removed
            remaining -= removed
            if instance["count"] == 0:
                del state["fish_instances"][uid]
            if not remaining:
                break
        record["total_count"] -= count
        balances = self._normal_balances(profile)
        before = balances[FISH_SALE_RESOURCE_KEY]
        amount = self.catalog.variants[fish_id].sale_value * count
        if before + amount > UINT64_MAX:
            return self._error(
                CS_SELL_FISH,
                "SC_SellFishRet",
                {"FishId": fish_id, "Count": count},
                "fish sale would overflow the resource balance",
                ERROR_EXCEED_ITEM_LIMIT_COUNT,
            )
        balances[FISH_SALE_RESOURCE_KEY] = before + amount
        response = {"Errorcode": 0, "FishId": fish_id, "Count": count}
        self._record_mutation(
            state,
            CS_SELL_FISH,
            fingerprint,
            response,
            "sell_fish",
            {
                "fish_id": fish_id,
                "count": count,
                "resource_type": 2,
                "amount": amount,
                "balance_before": before,
                "balance_after": before + amount,
            },
        )
        return self._mutation_result(
            CS_SELL_FISH,
            "SC_SellFishRet",
            response,
            profile,
            state,
            extra_changes={"resource_balances": balances},
        )

    def handle(
        self,
        command: int,
        profile: Optional[Mapping[str, Any]],
        body: bytes = b"",
    ) -> Optional[LifeResult]:
        """Handle one life-content command or return ``None`` when unowned."""

        if command not in RESPONSE_IDS:
            return None
        account = profile if isinstance(profile, Mapping) else {}

        if command == CS_REQUEST_GOURMET_DATAS:
            state = self.make_state(account)
            response = self.make_gourmet_datas_ret(state)
            try:
                _decode_empty(body, "CS_RequestGourmetDatas")
            except ProtobufDecodeError as exc:
                return LifeResult(
                    SC_GOURMET_DATAS_RET,
                    response,
                    error_code=ERROR_MESSAGE_DATA_ILLEGAL,
                    supported=False,
                    reason=str(exc),
                )
            return LifeResult(
                SC_GOURMET_DATAS_RET,
                response,
                changes=self._state_changes(account, state),
            )
        if command == CS_MATCH_RECIPE:
            return self._handle_match(account, body)
        if command == CS_COOK_FOOD:
            return self._handle_cook(account, body)
        if command == 218:
            return self._handle_hero_eat(account, body)
        if command == CS_USE_FOOD_PROPS:
            return self._handle_use_food_props(account, body)

        if command == CS_REQUEST_FISH_DATAS:
            state = self.make_state(account)
            response = self.make_fish_datas_ret(state)
            try:
                _decode_empty(body, "CS_RequestFishDatas")
            except ProtobufDecodeError as exc:
                return LifeResult(
                    SC_FISH_DATAS_RET,
                    response,
                    error_code=ERROR_MESSAGE_DATA_ILLEGAL,
                    supported=False,
                    reason=str(exc),
                )
            return LifeResult(
                SC_FISH_DATAS_RET,
                response,
                changes=self._state_changes(account, state),
                notifications=(
                    (
                        NOTIFY_UPDATE_FISHING_ATTRIBUTE,
                        self._fishing_attribute_notification(state),
                    ),
                ),
            )
        if command == CS_GO_FISHING:
            return self._handle_go_fishing(account, body)
        if command == CS_PUT_OR_TAKEOUT_FISH:
            return self._handle_aquarium(account, body)
        if command == CS_GO_FISHING_RESULT:
            return self._handle_fishing_result(account, body)
        if command == CS_GET_FISH_ACHIEVE_REWARD:
            return self._handle_achievement(account, body)
        if command == CS_SELL_FISH:
            return self._handle_sell_fish(account, body)

        state = self.make_state(account)
        response = self.codec.encode(
            "SC_SceneFishCountRet",
            {
                "SceneRecordList": [
                    {"SceneId": int(scene_id), "Count": count}
                    for scene_id, count in sorted(
                        state["scene_counts"].items(), key=lambda item: int(item[0])
                    )
                ]
            },
        )
        try:
            _decode_empty(body, "CS_RequestSceneFishCount")
        except ProtobufDecodeError as exc:
            return LifeResult(
                SC_SCENE_FISH_COUNT_RET,
                response,
                error_code=ERROR_MESSAGE_DATA_ILLEGAL,
                supported=False,
                reason=str(exc),
            )
        return LifeResult(
            SC_SCENE_FISH_COUNT_RET,
            response,
            changes=self._state_changes(account, state),
        )


@lru_cache(maxsize=1)
def default_layer() -> LifeContentLayer:
    return LifeContentLayer()


def seed_life_content(
    profile: MutableMapping[str, Any],
    catalog: Optional[Union[ContentCatalog, str, Path]] = None,
) -> MutableMapping[str, Any]:
    layer = default_layer() if catalog is None else LifeContentLayer(catalog)
    return layer.seed_profile(profile)


def handle_life_request(
    command: int,
    profile: Optional[Mapping[str, Any]],
    body: bytes = b"",
) -> Optional[LifeResult]:
    return default_layer().handle(command, profile, body)


Layer = LifeContentLayer
Result = LifeResult


__all__ = [
    "CS_REQUEST_GOURMET_DATAS",
    "SC_GOURMET_DATAS_RET",
    "CS_MATCH_RECIPE",
    "SC_MATCH_RECIPE_RET",
    "CS_COOK_FOOD",
    "SC_COOK_FOOD_RET",
    "CS_USE_FOOD_PROPS",
    "SC_USE_FOOD_PROPS_RET",
    "CS_REQUEST_FISH_DATAS",
    "SC_FISH_DATAS_RET",
    "CS_GO_FISHING",
    "SC_GO_FISHING_RET",
    "CS_PUT_OR_TAKEOUT_FISH",
    "SC_PUT_OR_TAKEOUT_FISH_RET",
    "CS_GO_FISHING_RESULT",
    "SC_GO_FISHING_RESULT_RET",
    "CS_GET_FISH_ACHIEVE_REWARD",
    "SC_GET_FISH_ACHIEVE_REWARD_RET",
    "CS_SELL_FISH",
    "SC_SELL_FISH_RET",
    "CS_REQUEST_SCENE_FISH_COUNT",
    "SC_SCENE_FISH_COUNT_RET",
    "RESPONSE_IDS",
    "LifeCatalogError",
    "LifeContentLayer",
    "LifeProfileError",
    "LifeResult",
    "Layer",
    "Result",
    "default_layer",
    "handle_life_request",
    "seed_life_content",
]
