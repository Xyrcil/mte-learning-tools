"""Persistent, table-backed farm gameplay for the offline server.

The farm tables are kept outside ``generated/content`` because they were
defined as a separate, auditable source bundle.  This layer owns only the
farm commands; the surrounding Oracle snapshot remains under the existing
``oracle_content`` profile key.
"""

from __future__ import annotations

import copy
import json
import math
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Dict, Iterable, Optional, Tuple

try:
    from .content_profile import ContentCatalog
    from .content_responses import ProtobufDecodeError, iter_wire_fields
    from .inventory_content import (
        InventoryContentError,
        make_update_item,
        normalize_inventory,
        stable_item_uid,
    )
    from .protobuf_codec import ProtocolMessageCodec
except ImportError:  # pragma: no cover
    from content_profile import ContentCatalog  # type: ignore
    from content_responses import ProtobufDecodeError, iter_wire_fields  # type: ignore
    from inventory_content import (  # type: ignore
        InventoryContentError,
        make_update_item,
        normalize_inventory,
        stable_item_uid,
    )
    from protobuf_codec import ProtocolMessageCodec  # type: ignore


CS_PLANT = 622
SC_PLANT_RET = 623
CS_UPROOT = 624
SC_UPROOT_RET = 625
CS_WATERING = 626
SC_WATERING_RET = 627
CS_APPLY_FERTILIZER = 628
SC_APPLY_FERTILIZER_RET = 629
CS_HARVEST = 630
SC_HARVEST_RET = 631
NOTIFY_UPDATE_FARM_LAND_DATAS = 656
CS_REQUEST_FARM_DATAS = 653
SC_REQUEST_FARM_DATAS_RET = 654
CS_FARM_CROPS_MATURE_TIME = 658
SC_FARM_CROPS_MATURE_TIME_RET = 659
CS_REQUEST_FARMLAND_INFO = 693
SC_REQUEST_FARMLAND_INFO_RET = 694

RESPONSE_IDS = MappingProxyType(
    {
        CS_PLANT: SC_PLANT_RET,
        CS_UPROOT: SC_UPROOT_RET,
        CS_HARVEST: SC_HARVEST_RET,
        CS_REQUEST_FARM_DATAS: SC_REQUEST_FARM_DATAS_RET,
        CS_FARM_CROPS_MATURE_TIME: SC_FARM_CROPS_MATURE_TIME_RET,
        CS_REQUEST_FARMLAND_INFO: SC_REQUEST_FARMLAND_INFO_RET,
        **{command: command+1 for command in (626,628,632,634,636,638,640,642,644,660)},
    }
)
UNSUPPORTED_COMMANDS = MappingProxyType({})

ORACLE_STATE_KEY = "oracle_content"
FARM_STATE_VERSION = 1
FARM_LAND_IDS = tuple(range(1, 17))
FARM_FERTILIZER_ITEM_ID = 1_001_020
FARM_FERTILIZER_PARAMETER_ID = 1_805_030
FARM_MATURE_MARKER_PARAMETER_ID = 1_805_035
FARM_DEFAULT_TEMPERATURE = 100
FARM_TEMPERATURE_PARAMETER_ID = 1_805_001
FARM_DEFAULT_HUMIDITY = 200
FARM_DEFAULT_QUALITY = 1
FARM_MAX_BATCH = 16
UINT32_MAX = (1 << 32) - 1
UINT64_MAX = (1 << 64) - 1

ERROR_SUCCESS = 0
ERROR_ITEM_NUM_NOT_ENOUGH = 110
ERROR_NO_REWARD_CAN_BE_TAKEN = 122
ERROR_ROLE_ID_ILLEGAL = 104
ERROR_EXCEED_ITEM_LIMIT_COUNT = 429
ERROR_CONFIG = 32772
ERROR_MESSAGE_DATA_ILLEGAL = 32769
OPERATION_ID_FIELD = 127
LIVE_SNAPSHOT_FIELD = 125
OPERATION_ID_PATTERN = re.compile(rb"[0-9a-f]{32}")

REQUIRED_FARM_TABLES = (
    "a_farmer_seed",
    "a_farmer_crops",
    "a_farmer_level",
    "a_farmer_parameter",
    "a_farmer_facility",
    "a_farmer_event",
    "a_farmer_buff",
)


class FarmContentError(ValueError):
    """Raised when the farm source closure or persisted farm is unsafe."""


class FarmProfileError(FarmContentError):
    pass


@dataclass(frozen=True)
class FarmResult:
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
class SeedDefinition:
    seed_id: int
    item_id: int
    crop_ids: Tuple[int, ...]
    harvest_index: int
    grow_speed: int


@dataclass(frozen=True)
class FarmCatalog:
    seeds: Mapping[int, SeedDefinition]
    levels: Mapping[int, Mapping[str, int]]
    item_rows: Mapping[int, Mapping[str, Any]]
    item_max_stack: Mapping[int, int]
    fertilizer_item_id: int
    mature_marker: int


def _uint(value: Any, name: str, maximum: int = UINT32_MAX) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise FarmContentError(f"{name} must be an integer")
    if value < 0 or value > maximum:
        raise FarmContentError(f"{name} is outside uint range")
    return value


def _records(path: Path) -> list[Mapping[str, Any]]:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FarmContentError(f"cannot load farm table {path}") from exc
    rows = doc.get("records") if isinstance(doc, Mapping) else None
    if not isinstance(rows, list) or not rows or not all(isinstance(r, Mapping) for r in rows):
        raise FarmContentError(f"farm table {path.name} has no valid records")
    return list(rows)


def _find_source(content_root: Path, source_directory: Optional[Path]) -> Path:
    if source_directory is not None:
        candidate = Path(source_directory).resolve()
        if not candidate.is_dir():
            raise FarmContentError(f"farm source directory does not exist: {candidate}")
        return candidate
    root = Path(content_root).resolve()
    if all((root / f"{table}.json").is_file() for table in REQUIRED_FARM_TABLES):
        return root
    raise FarmContentError(f"farm source closure is missing from catalog root: {root}")


def _load_catalog(content_root: Path | ContentCatalog, source_directory: Optional[Path]) -> FarmCatalog:
    if isinstance(content_root, ContentCatalog):
        if source_directory is not None:
            raise FarmContentError("source_directory is not used with ContentCatalog")
        try:
            tables = {name: list(content_root.table(name).records) for name in REQUIRED_FARM_TABLES}
            item_rows_list = list(content_root.table("a_item").records)
        except (AttributeError, KeyError) as exc:
            raise FarmContentError("ContentCatalog is missing the farm table closure") from exc
    else:
        source = _find_source(content_root, source_directory)
        tables = {name: _records(source / f"{name}.json") for name in REQUIRED_FARM_TABLES}
        item_rows_list = _records(Path(content_root).resolve() / "a_item.json")
    seed_rows = tables["a_farmer_seed"]
    crop_rows = tables["a_farmer_crops"]
    level_rows = tables["a_farmer_level"]
    seeds: Dict[int, SeedDefinition] = {}
    crops_by_seed: Dict[int, list[Mapping[str, Any]]] = {}
    for row in crop_rows:
        seed_id = _uint(row.get("seed"), "a_farmer_crops.seed")
        crops_by_seed.setdefault(seed_id, []).append(row)
    for row in seed_rows:
        seed_id = _uint(row.get("id"), "a_farmer_seed.id")
        raw_crops = row.get("farmer_crops")
        try:
            crop_ids = tuple(_uint(int(v), "farmer_crops.id") for v in json.loads(raw_crops))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise FarmContentError(f"a_farmer_seed[{seed_id}] has invalid farmer_crops") from exc
        linked = crops_by_seed.get(seed_id, [])
        # a_farmer_crops rows describe quality-score intervals; their IDs are
        # interval IDs, while farmer_crops contains the resulting a_item IDs.
        if len(crop_ids) != 3 or len(linked) != 6:
            raise FarmContentError(f"a_farmer_seed[{seed_id}] crop closure is incomplete")
        seeds[seed_id] = SeedDefinition(
            seed_id,
            _uint(row.get("item_id"), f"a_farmer_seed[{seed_id}].item_id"),
            crop_ids,
            _uint(row.get("harvest_index"), f"a_farmer_seed[{seed_id}].harvest_index"),
            _uint(row.get("grow_speed"), f"a_farmer_seed[{seed_id}].grow_speed"),
        )
    if len(seeds) != 12:
        raise FarmContentError(f"expected 12 farmer seeds, got {len(seeds)}")
    levels: Dict[int, Mapping[str, int]] = {}
    for row in level_rows:
        level = _uint(row.get("farmer_level"), "a_farmer_level.farmer_level")
        levels[level] = {
            "id": _uint(row.get("id"), "a_farmer_level.id"),
            "land_num": _uint(row.get("land_num"), "a_farmer_level.land_num"),
            "grow_speed": _uint(row.get("grow_speed"), "a_farmer_level.grow_speed"),
            "fertilizer_limit": _uint(row.get("fertilizer_limit"), "a_farmer_level.fertilizer_limit"),
        }
    if not levels or max(levels) < 16:
        raise FarmContentError("a_farmer_level does not close over 16 lands")
    item_rows: Dict[int, Mapping[str, Any]] = {}
    for row in item_rows_list:
        item_rows[_uint(row.get("id"), "a_item.id")] = row
    needed = {FARM_FERTILIZER_ITEM_ID}
    needed.update(seed.item_id for seed in seeds.values())
    needed.update(seed.crop_ids[0] for seed in seeds.values())
    missing = sorted(item_id for item_id in needed if item_id not in item_rows)
    if missing:
        raise FarmContentError(f"a_item is missing farm items: {missing[:8]}")
    item_max_stack: Dict[int, int] = {}
    for item_id in sorted(needed):
        row = item_rows[item_id]
        if "max_stack_num" not in row:
            raise FarmContentError(f"a_item[{item_id}] has no max_stack_num")
        maximum = _uint(row.get("max_stack_num"), f"a_item[{item_id}].max_stack_num")
        if maximum <= 0:
            raise FarmContentError(f"a_item[{item_id}] has no positive stack cap")
        item_max_stack[item_id] = maximum
    parameter_rows = {int(row.get("id")): row for row in tables["a_farmer_parameter"]}
    if FARM_FERTILIZER_PARAMETER_ID not in parameter_rows or FARM_MATURE_MARKER_PARAMETER_ID not in parameter_rows:
        raise FarmContentError("a_farmer_parameter is missing fertilizer/mature markers")
    fertilizer = _uint(parameter_rows[FARM_FERTILIZER_PARAMETER_ID].get("int_value"), "fertilizer item")
    mature_marker = _uint(parameter_rows[FARM_MATURE_MARKER_PARAMETER_ID].get("int_value"), "mature marker")
    if fertilizer != FARM_FERTILIZER_ITEM_ID or mature_marker != 50:
        raise FarmContentError("farm parameter closure disagrees with verified IDs")
    return FarmCatalog(seeds, levels, item_rows, item_max_stack, fertilizer, mature_marker)


def _decode_varint_bytes(raw: bytes) -> list[int]:
    values: list[int] = []
    value = 0
    shift = 0
    for byte in raw:
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            values.append(value)
            value = 0
            shift = 0
        else:
            shift += 7
            if shift >= 70:
                raise ProtobufDecodeError("invalid packed varint")
    if shift:
        raise ProtobufDecodeError("truncated packed varint")
    return values


def _decode_request(
    data: bytes,
    message: str,
    fields: Mapping[int, Tuple[str, int, bool]],
    *,
    allow_operation_id: bool = False,
) -> Dict[int, list[Any]]:
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise ProtobufDecodeError("request body must be bytes")
    result: Dict[int, list[Any]] = {number: [] for number in fields}
    for number, wire, value in iter_wire_fields(bytes(data)):
        if number == OPERATION_ID_FIELD and allow_operation_id:
            if wire != 2 or not isinstance(value, bytes) or OPERATION_ID_PATTERN.fullmatch(value) is None:
                raise ProtobufDecodeError(
                    f"{message}.field127 must be one lowercase 32-hex-byte UUID"
                )
            result.setdefault(OPERATION_ID_FIELD, []).append(value)
            continue
        if number not in fields:
            raise ProtobufDecodeError(f"{message} has unknown field {number}")
        name, maximum, repeated = fields[number]
        if repeated:
            if wire == 0:
                values = [int(value)]
            elif wire == 2 and isinstance(value, bytes):
                values = _decode_varint_bytes(value)
            else:
                raise ProtobufDecodeError(f"{message}.{name} is not a packed varint")
            result[number].extend(values)
        else:
            if wire != 0:
                raise ProtobufDecodeError(f"{message}.{name} is not a varint")
            result[number].append(int(value))
    for number, (name, maximum, repeated) in fields.items():
        if not repeated and len(result[number]) > 1:
            raise ProtobufDecodeError(f"{message} requires exactly one {name}")
        if repeated and (not result[number] or len(result[number]) > FARM_MAX_BATCH):
            raise ProtobufDecodeError(f"{message}.{name} must contain 1..{FARM_MAX_BATCH} values")
        if len(set(result[number])) != len(result[number]) and repeated:
            raise ProtobufDecodeError(f"{message}.{name} contains duplicates")
        if any(value < 0 or value > maximum for value in result[number]):
            raise ProtobufDecodeError(f"{message} field exceeds uint range")
    if allow_operation_id and len(result.get(OPERATION_ID_FIELD, [])) > 1:
        raise ProtobufDecodeError(f"{message}.field127 may occur only once")
    return result


def _scalar(values: Mapping[int, Sequence[Any]], number: int, default: int = 0) -> int:
    entries = values.get(number, ())
    return int(entries[0]) if entries else default


def _decode_overview(data: bytes) -> bool:
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise ProtobufDecodeError("request body must be bytes")
    fields = list(iter_wire_fields(bytes(data)))
    if not fields:
        return False
    if fields == [(LIVE_SNAPSHOT_FIELD, 0, 1)]:
        return True
    raise ProtobufDecodeError("CS_RequestFarmlandInfo only accepts the offline live-snapshot marker")


class FarmContentLayer:
    """Copy-on-write farm commands with deterministic offline timing."""

    def __init__(
        self,
        content_root: Optional[Path | str] = None,
        *,
        codec: Optional[ProtocolMessageCodec] = None,
        clock: Optional[Callable[[], float]] = None,
        source_directory: Optional[Path | str] = None,
    ) -> None:
        source = content_root if content_root is not None else Path(__file__).resolve().parents[1] / "generated" / "content"
        self.content_root = source.root if isinstance(source, ContentCatalog) else Path(source).resolve()
        self.catalog = _load_catalog(source if isinstance(source, ContentCatalog) else self.content_root, Path(source_directory) if source_directory is not None else None)
        self.codec = codec or ProtocolMessageCodec()
        self.clock = clock or time.time
        try:
            from .farm_management import FarmManagement
        except ImportError:
            from farm_management import FarmManagement
        management_tables = {name: list(source.table(name).records) if isinstance(source, ContentCatalog)
                             else _records(_find_source(self.content_root, Path(source_directory) if source_directory else None) / f'{name}.json')
                             for name in ('a_farmer_level','a_farmer_facility','a_farmer_event','a_farmer_buff')}
        self.management = FarmManagement(self, management_tables)

    def _now(self) -> int:
        try:
            value = int(self.clock())
        except (TypeError, ValueError, OverflowError) as exc:
            raise FarmProfileError("farm clock must return a timestamp") from exc
        if value < 0 or value > UINT64_MAX:
            raise FarmProfileError("farm timestamp is outside uint64")
        return value

    def _default_farm(self, profile: Mapping[str, Any]) -> Dict[str, Any]:
        level_number = max(self.catalog.levels) if profile.get("all_content_unlocked") else 1
        level = self.catalog.levels[level_number]
        return {
            "schema_version": FARM_STATE_VERSION,
            "farmer_level_config_id": level["id"],
            "farmer_level": level_number,
            "current_temperature": FARM_DEFAULT_TEMPERATURE,
            "temperature_parameter_id": FARM_TEMPERATURE_PARAMETER_ID,
            "available_fertilizer_num": level["fertilizer_limit"],
            "trusteeship": 0,
            "last_clock": 0,
            "lands": [self._empty_land(land_id) for land_id in FARM_LAND_IDS],
        }

    @staticmethod
    def _empty_land(land_id: int) -> Dict[str, int]:
        return {
            "land_id": land_id,
            "seed_id": 0,
            "harvest_value": 0,
            "land_humidity": FARM_DEFAULT_HUMIDITY,
            "score": 0,
            "super_weight": 0,
            "quality": 0,
            "planted_at": 0,
            "generation": 0,
        }

    def _state(self, profile: Mapping[str, Any], now: int) -> Tuple[Dict[str, Any], int]:
        raw_root = profile.get(ORACLE_STATE_KEY)
        if raw_root is None:
            root: Dict[str, Any] = {}
        elif not isinstance(raw_root, Mapping):
            raise FarmProfileError("oracle_content must be a mapping")
        else:
            root = copy.deepcopy(raw_root)
        raw = root.get("farm")
        if raw is None:
            farm = self._default_farm(profile)
        elif not isinstance(raw, Mapping):
            raise FarmProfileError("oracle_content.farm must be a mapping")
        else:
            farm = copy.deepcopy(raw)
            version = farm.get("schema_version", FARM_STATE_VERSION)
            if isinstance(version, bool) or not isinstance(version, int):
                raise FarmProfileError("farm schema_version must be an integer")
            if version != FARM_STATE_VERSION:
                raise FarmProfileError(f"unsupported farm schema_version {version}")
            allowed_farm = {
                "schema_version", "farmer_level_config_id", "farmer_level",
                "current_temperature", "temperature_parameter_id",
                "available_fertilizer_num", "trusteeship", "last_clock", "lands", "management",
            }
            unknown = set(farm) - allowed_farm
            if unknown:
                raise FarmProfileError(f"farm has unknown fields: {sorted(unknown)}")
            for required in ("farmer_level", "lands"):
                if required not in farm:
                    raise FarmProfileError(f"farm is missing {required}")
            farm["schema_version"] = FARM_STATE_VERSION
        level_number = _uint(farm.get("farmer_level"), "farm.farmer_level")
        level = self.catalog.levels.get(level_number)
        if level is None:
            raise FarmProfileError(f"farm farmer_level {level_number} is not in catalog")
        config_id = farm.get("farmer_level_config_id", level["id"])
        if _uint(config_id, "farm.farmer_level_config_id") != level["id"]:
            raise FarmProfileError("farm farmer level/config ID mismatch")
        farm["farmer_level_config_id"] = level["id"]
        temperature = _uint(farm.get("current_temperature", FARM_DEFAULT_TEMPERATURE), "farm.current_temperature")
        parameter_id = _uint(farm.get("temperature_parameter_id", FARM_TEMPERATURE_PARAMETER_ID), "farm.temperature_parameter_id")
        if parameter_id != FARM_TEMPERATURE_PARAMETER_ID:
            raise FarmProfileError("farm temperature parameter is not supported")
        farm["current_temperature"] = temperature
        farm["temperature_parameter_id"] = parameter_id
        farm["available_fertilizer_num"] = _uint(farm.get("available_fertilizer_num", level["fertilizer_limit"]), "farm.available_fertilizer_num")
        farm["trusteeship"] = _uint(farm.get("trusteeship", 0), "farm.trusteeship")
        previous_clock = _uint(farm.get("last_clock", 0), "farm.last_clock", UINT64_MAX)
        raw_lands = farm.get("lands")
        if not isinstance(raw_lands, list) or len(raw_lands) != len(FARM_LAND_IDS):
            raise FarmProfileError("farm.lands must contain exactly 16 rows")
        lands: list[Dict[str, int]] = []
        allowed_land = {"land_id", "seed_id", "harvest_value", "land_humidity", "score", "super_weight", "quality", "planted_at", "generation"}
        seen: set[int] = set()
        for raw_land in raw_lands:
            if not isinstance(raw_land, Mapping):
                raise FarmProfileError("farm land must be a mapping")
            unknown = set(raw_land) - allowed_land
            if unknown:
                raise FarmProfileError(f"farm land has unknown fields: {sorted(unknown)}")
            land_id = _uint(raw_land.get("land_id"), "farm.land_id")
            if land_id not in FARM_LAND_IDS or land_id in seen:
                raise FarmProfileError("farm land IDs must be exactly 1..16")
            seen.add(land_id)
            row = self._empty_land(land_id)
            for key in allowed_land - {"land_id"}:
                if key in raw_land:
                    row[key] = _uint(raw_land[key], f"farm.land[{land_id}].{key}", UINT64_MAX if key == "planted_at" else UINT32_MAX)
            seed_id = row["seed_id"]
            seed = self.catalog.seeds.get(seed_id)
            if seed_id and seed is None:
                raise FarmProfileError(f"farm land {land_id} has unknown seed {seed_id}")
            if not seed_id and (row["harvest_value"] or row["planted_at"] or row["quality"]):
                raise FarmProfileError(f"empty farm land {land_id} has planted state")
            if seed is not None and row["harvest_value"] > seed.harvest_index:
                raise FarmProfileError(f"farm land {land_id} harvest_value exceeds seed index")
            if seed is not None and land_id > level["land_num"]:
                raise FarmProfileError(f"farm land {land_id} is planted beyond the saved farmer level; state preserved")
            lands.append(row)
        if seen != set(FARM_LAND_IDS):
            raise FarmProfileError("farm land IDs must be exactly 1..16")
        lands.sort(key=lambda row: row["land_id"])
        farm["lands"] = lands
        farm["last_clock"] = previous_clock
        effective_now = max(now, previous_clock)
        try:
            managed = self.management.validate(farm)
        except ValueError as exc:
            raise FarmProfileError(str(exc)) from exc
        if managed:
            effective_now = max(effective_now, managed['last_tick'])
        has_planted = False
        for land in lands:
            seed = self.catalog.seeds.get(land["seed_id"])
            if seed is None:
                continue
            has_planted = True
            if self.management.tracked(farm,land):
                continue
            elapsed = max(0, effective_now - land["planted_at"])
            land["harvest_value"] = min(seed.harvest_index, max(land["harvest_value"], elapsed * 10))
        if has_planted:
            farm["last_clock"] = effective_now
        if managed:
            self.management.advance(profile,farm,effective_now)
        root["farm"] = farm
        return root, effective_now

    def _changes(self, profile: Mapping[str, Any], root: Dict[str, Any], inventory: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        changes: Dict[str, Any] = {}
        if profile.get(ORACLE_STATE_KEY) != root:
            changes[ORACLE_STATE_KEY] = root
        if inventory is not None and normalize_inventory(profile.get("inventory", {})) != inventory:
            changes["inventory"] = inventory
        return changes

    def _land(self, farm: Mapping[str, Any], land_id: int) -> Dict[str, Any]:
        for land in farm["lands"]:
            if land["land_id"] == land_id:
                return land
        raise FarmProfileError("land is outside the 1..16 closure")

    @staticmethod
    def _farm_write_access(profile: Mapping[str, Any]) -> Tuple[bool, str]:
        """Require the same local-entry prerequisites as the Oracle scene."""

        buildings = profile.get("buildings")
        building = None
        if isinstance(buildings, Mapping):
            building = buildings.get("1999705", buildings.get(1_999_705))
        if isinstance(building, Mapping):
            level = building.get("level")
        else:
            level = building
        if isinstance(level, bool) or not isinstance(level, int) or level < 1:
            return False, "farm write requires building 1999705 at level >= 1"

        root = profile.get(ORACLE_STATE_KEY)
        work = root.get("work") if isinstance(root, Mapping) else None
        home = work.get("1999705") if isinstance(work, Mapping) else None
        gospels = profile.get("gospels")
        if not isinstance(home, Mapping) or not isinstance(gospels, Sequence) or isinstance(gospels, (str, bytes, bytearray)):
            return False, "farm write requires a worker assigned to the Oracle home"
        owned = {
            row["uid"]: row.get("id")
            for row in gospels
            if isinstance(row, Mapping)
            and isinstance(row.get("uid"), int)
            and not isinstance(row.get("uid"), bool)
            and row.get("uid", 0) > 0
        }
        uid = home.get("gospel_uid")
        if (not isinstance(uid, int) or isinstance(uid, bool) or uid <= 0
                or uid not in owned or home.get("gospel_id") != owned[uid]
                or type(home.get("is_valid")) is not int or home["is_valid"] != 1):
            return False, "farm write requires an active owned worker in building 1999705"
        return True, ""

    def _land_wire(self, land: Mapping[str, Any], now: int) -> Dict[str, int]:
        seed_id = int(land["seed_id"])
        seed = self.catalog.seeds.get(seed_id)
        elapsed = max(0, now - int(land.get("planted_at", 0))) if seed_id else 0
        return {
            "LandId": int(land["land_id"]),
            "SeedId": seed_id,
            "LandHumidity": int(land['land_humidity']) if seed_id else 0,
            "Score": int(land.get("score", 0)),
            "HarvestValue": int(land.get("harvest_value", 0)) if seed else 0,
            "SuperWeight": int(land.get("super_weight", 0)),
            "Quality": int(land.get("quality", 0)),
            "PlantTimeDiff": elapsed if seed_id else 0,
        }

    def _notify(self, lands: Iterable[Mapping[str, Any]], now: int) -> Tuple[int, bytes]:
        return NOTIFY_UPDATE_FARM_LAND_DATAS, self.codec.encode(
            "Notify_UpdateFarmLandDatas",
            {"FarmLandList": [self._land_wire(land, now) for land in lands]},
        )

    def _available_lands(self, farm: Mapping[str, Any]) -> list[Mapping[str, Any]]:
        limit = self.catalog.levels[int(farm["farmer_level"])]["land_num"]
        return [land for land in farm["lands"] if land["land_id"] <= limit]

    def current_state_notifications(self, profile: Mapping[str, Any]) -> Tuple[Tuple[int, bytes], ...]:
        """Project today's land state when acknowledging an older operation.

        Never replay the source delta: the player may already have uprooted
        or replanted that land. This read-only projection spends/grants nothing.
        """
        root, now = self._state(profile, self._now())
        if 'management' in root['farm']:
            return self.management.notifications(profile,root['farm'],now)
        return (self._notify(self._available_lands(root["farm"]), now),)

    @staticmethod
    def _history(profile: Mapping[str, Any], key: str) -> list[Any]:
        value = profile.get(key, [])
        if not isinstance(value, list):
            raise FarmProfileError(f"profile.{key} must be a list")
        return copy.deepcopy(value)

    def _seed_inventory(self, profile: Mapping[str, Any], item_id: int, delta: int) -> Dict[str, Any]:
        inventory = normalize_inventory(profile.get("inventory", {}))
        row = inventory.get(str(item_id))
        current = int(row["count"]) if row else 0
        new_count = current + delta
        try:
            maximum = self.catalog.item_max_stack[item_id]
        except KeyError as exc:
            raise FarmProfileError(f"item {item_id} has no verified stack cap") from exc
        if new_count < 0:
            raise FarmProfileError("item count is insufficient")
        if new_count > maximum:
            raise FarmProfileError("item stack cap would be exceeded")
        inventory[str(item_id)] = {
            "uid": int(row["uid"]) if row else stable_item_uid(item_id),
            "id": item_id,
            "count": new_count,
            "para": int(row.get("para", 0)) if row else 0,
        }
        return {key: inventory[key] for key in sorted(inventory, key=lambda raw: int(raw))}

    def _error(self, response_id: int, message: str, values: Mapping[str, Any], code: int, reason: str) -> FarmResult:
        return FarmResult(response_id, self.codec.encode(message, values), error_code=code, supported=False, reason=reason)

    def _handle_plant(self, profile: Mapping[str, Any], body: bytes) -> FarmResult:
        fields = {1: ("SeedId", UINT32_MAX, False), 2: ("LandIdList", UINT32_MAX, True), 3: ("IsGuide", 1, False)}
        try:
            parsed = _decode_request(body, "CS_Plant", fields, allow_operation_id=True)
            seed_id, land_ids = _scalar(parsed, 1), parsed[2]
            if seed_id <= 0:
                raise FarmProfileError("SeedId must be positive")
            allowed, reason = self._farm_write_access(profile)
            if not allowed:
                return self._error(SC_PLANT_RET, "SC_PlantRet", {"Errorcode": ERROR_CONFIG, "SeedId": seed_id, "LandIdList": land_ids}, ERROR_CONFIG, reason)
            now = self._now()
            root, effective_now = self._state(profile, now)
            farm = root["farm"]
            seed = self.catalog.seeds.get(seed_id)
            if seed is None:
                raise FarmProfileError("unknown seed")
            level = self.catalog.levels.get(int(farm.get("farmer_level", max(self.catalog.levels))))
            if level is None or any(land_id < 1 or land_id > int(level["land_num"]) for land_id in land_ids):
                raise FarmProfileError("land is not unlocked")
            selected = [self._land(farm, land_id) for land_id in land_ids]
            if any(int(land["seed_id"]) for land in selected):
                raise FarmProfileError("land is already planted")
            inventory = normalize_inventory(profile.get("inventory", {}))
            current = int(inventory.get(str(seed.item_id), {}).get("count", 0))
            if current < len(land_ids):
                return self._error(SC_PLANT_RET, "SC_PlantRet", {"Errorcode": ERROR_ITEM_NUM_NOT_ENOUGH, "SeedId": seed_id, "LandIdList": land_ids}, ERROR_ITEM_NUM_NOT_ENOUGH, "seed count is insufficient")
            new_inventory = self._seed_inventory(profile, seed.item_id, -len(land_ids))
            for land in selected:
                land.update({"seed_id": seed_id, "harvest_value": 0, "land_humidity": FARM_DEFAULT_HUMIDITY, "quality": FARM_DEFAULT_QUALITY, "score": 0, "super_weight": 0, "planted_at": effective_now})
            farm["last_clock"] = effective_now
            plant_generations = []
            for land in selected:
                land["generation"] += 1
                plant_generations.append(land["generation"])
                self.management.adopt(farm,land)
                if farm.get('management') and farm['trusteeship']:
                    land['land_humidity'] = farm['management']['preset']['humidity']*100-50
            changes = self._changes(profile, root, new_inventory)
            history = self._history(profile, "resource_history")
            history.append({"source": "farm", "command_id": CS_PLANT, "operation": "plant", "land_ids": list(land_ids), "plant_generation": plant_generations, "costs": [{"type": 4, "id": seed.item_id, "num": len(land_ids)}], "rewards": []})
            changes["resource_history"] = history
            notes = [self._notify(selected, effective_now)]
            row = new_inventory[str(seed.item_id)]
            notes.append((26, make_update_item(int(profile.get("role_id", 0)), row, self.codec)))
            return FarmResult(SC_PLANT_RET, self.codec.encode("SC_PlantRet", {"Errorcode": 0, "SeedId": seed_id, "LandIdList": land_ids}), changes=changes, notifications=tuple(notes))
        except (FarmContentError, InventoryContentError, ProtobufDecodeError) as exc:
            return self._error(SC_PLANT_RET, "SC_PlantRet", {"Errorcode": ERROR_MESSAGE_DATA_ILLEGAL, "SeedId": 0, "LandIdList": []}, ERROR_MESSAGE_DATA_ILLEGAL, str(exc))

    def _handle_uproot(self, profile: Mapping[str, Any], body: bytes) -> FarmResult:
        try:
            parsed = _decode_request(body, "CS_Uproot", {1: ("LandId", UINT32_MAX, False), 2: ("IsGuide", 1, False)}, allow_operation_id=True)
            land_id = _scalar(parsed, 1)
            if land_id <= 0:
                raise FarmProfileError("LandId must be positive")
            allowed, reason = self._farm_write_access(profile)
            if not allowed:
                return self._error(SC_UPROOT_RET, "SC_UprootRet", {"Errorcode": ERROR_CONFIG, "LandId": land_id}, ERROR_CONFIG, reason)
            root, now = self._state(profile, self._now())
            land = self._land(root["farm"], land_id)
            if not land["seed_id"]:
                return self._error(SC_UPROOT_RET, "SC_UprootRet", {"Errorcode": ERROR_NO_REWARD_CAN_BE_TAKEN, "LandId": land_id}, ERROR_NO_REWARD_CAN_BE_TAKEN, "land is empty")
            plant_generation = int(land["generation"])
            empty = self._empty_land(land_id)
            empty["generation"] = plant_generation
            land.update(empty)
            changes = self._changes(profile, root)
            history = self._history(profile, "resource_history")
            history.append({"source": "farm", "command_id": CS_UPROOT, "operation": "uproot", "land_ids": [land_id], "plant_generation": [plant_generation], "costs": [], "rewards": []})
            changes["resource_history"] = history
            return FarmResult(SC_UPROOT_RET, self.codec.encode("SC_UprootRet", {"Errorcode": 0, "LandId": land_id}), changes=changes, notifications=(self._notify([land], now),))
        except (FarmContentError, ProtobufDecodeError) as exc:
            return self._error(SC_UPROOT_RET, "SC_UprootRet", {"Errorcode": ERROR_MESSAGE_DATA_ILLEGAL, "LandId": 0}, ERROR_MESSAGE_DATA_ILLEGAL, str(exc))

    def _handle_harvest(self, profile: Mapping[str, Any], body: bytes) -> FarmResult:
        try:
            parsed = _decode_request(body, "CS_Harvest", {1: ("LandId", UINT32_MAX, False), 2: ("IsGuide", 1, False)}, allow_operation_id=True)
            land_id = _scalar(parsed, 1)
            if land_id <= 0:
                raise FarmProfileError("LandId must be positive")
            allowed, reason = self._farm_write_access(profile)
            if not allowed:
                return self._error(SC_HARVEST_RET, "SC_HarvestRet", {"Errorcode": ERROR_CONFIG, "LandId": land_id}, ERROR_CONFIG, reason)
            root, now = self._state(profile, self._now())
            land = self._land(root["farm"], land_id)
            seed = self.catalog.seeds.get(int(land["seed_id"]))
            if seed is None:
                return self._error(SC_HARVEST_RET, "SC_HarvestRet", {"Errorcode": ERROR_NO_REWARD_CAN_BE_TAKEN, "LandId": land_id}, ERROR_NO_REWARD_CAN_BE_TAKEN, "land is empty")
            if int(land["harvest_value"]) < seed.harvest_index:
                return self._error(SC_HARVEST_RET, "SC_HarvestRet", {"Errorcode": ERROR_NO_REWARD_CAN_BE_TAKEN, "LandId": land_id}, ERROR_NO_REWARD_CAN_BE_TAKEN, "crop is not mature")
            crop_id = seed.crop_ids[0]
            new_inventory = self._seed_inventory(profile, crop_id, 1)
            reward = {"ResType": 4, "ItemId": crop_id, "Count": 1, "Para": 0, "Quality": FARM_DEFAULT_QUALITY}
            plant_generation = int(land["generation"])
            empty = self._empty_land(land_id)
            empty["generation"] = plant_generation
            land.update(empty)
            changes = self._changes(profile, root, new_inventory)
            reward_history = self._history(profile, "reward_history")
            rewards = [{"type": 4, "id": crop_id, "num": 1}]
            reward_history.append({"claim_key": f"farm:{land_id}:{plant_generation}", "source": "farm", "command_id": CS_HARVEST, "operation": "harvest", "land_ids": [land_id], "plant_generation": [plant_generation], "rewards": rewards})
            resource_history = self._history(profile, "resource_history")
            resource_history.append({"source": "farm", "command_id": CS_HARVEST, "operation": "harvest", "land_ids": [land_id], "plant_generation": [plant_generation], "costs": [], "rewards": rewards})
            changes["resource_history"] = resource_history
            changes["reward_history"] = reward_history
            notes = [self._notify([land], now), (26, make_update_item(int(profile.get("role_id", 0)), new_inventory[str(crop_id)], self.codec))]
            return FarmResult(SC_HARVEST_RET, self.codec.encode("SC_HarvestRet", {"Errorcode": 0, "LandId": land_id, "RewardItem": reward}), changes=changes, notifications=tuple(notes))
        except (FarmContentError, InventoryContentError, ProtobufDecodeError) as exc:
            code = ERROR_EXCEED_ITEM_LIMIT_COUNT if "cap" in str(exc) else ERROR_MESSAGE_DATA_ILLEGAL
            return self._error(SC_HARVEST_RET, "SC_HarvestRet", {"Errorcode": code, "LandId": 0}, code, str(exc))

    def _handle_query(self, profile: Mapping[str, Any], body: bytes) -> FarmResult:
        try:
            parsed = _decode_request(body, "CS_RequestFarmDatas", {1: ("RoleId", UINT64_MAX, False), 2: ("FriendId", UINT64_MAX, False)})
            requested_role_id = _scalar(parsed, 1)
            friend_id = _scalar(parsed, 2)
            actual_role_id = profile.get("role_id", 0)
            if isinstance(actual_role_id, bool) or not isinstance(actual_role_id, int) or actual_role_id <= 0 or requested_role_id != actual_role_id:
                return self._error(SC_REQUEST_FARM_DATAS_RET, "SC_RequestFarmDatasRet", {"Sender": actual_role_id if isinstance(actual_role_id, int) and actual_role_id > 0 else 0, "FriendId": 0, "Farm": {"FarmerLvl": 1}, "FarmLandList": [], "FarmBuffList": [], "FarmLogList": [], "GoodsList": [], "GoodsRemainSecTime": 0}, ERROR_ROLE_ID_ILLEGAL, f"requested role {requested_role_id} does not match profile role {actual_role_id}")
            if friend_id != 0:
                return self._error(SC_REQUEST_FARM_DATAS_RET, "SC_RequestFarmDatasRet", {"Sender": actual_role_id, "FriendId": 0, "Farm": {"FarmerLvl": 1}, "FarmLandList": [], "FarmBuffList": [], "FarmLogList": [], "GoodsList": [], "GoodsRemainSecTime": 0}, ERROR_ROLE_ID_ILLEGAL, f"friend farm {friend_id} is not present in local single-account state")
            root, now = self._state(profile, self._now())
            farm = root["farm"]
            level = self.catalog.levels[int(farm["farmer_level"])]
            self.management.activate(farm,now)
            changes = self.management.automatic_harvest(profile,root,now)
            farm_wire = self.management.farm_wire(profile,farm)
            body_out = self.codec.encode("SC_RequestFarmDatasRet", {"Sender": requested_role_id, "FriendId": friend_id, "Farm": farm_wire, "FarmLandList": [self._land_wire(land, now) for land in self._available_lands(farm)], "FarmBuffList": self.management.buff_wire(farm), "FarmLogList": [], "GoodsList": [], "GoodsRemainSecTime": 0})
            changes.update(self._changes(profile,root))
            return FarmResult(SC_REQUEST_FARM_DATAS_RET, body_out, changes=changes)
        except (FarmContentError, ProtobufDecodeError) as exc:
            return self._error(SC_REQUEST_FARM_DATAS_RET, "SC_RequestFarmDatasRet", {"Sender": 0, "FriendId": 0, "Farm": {"FarmerLvl": 1}, "FarmLandList": [], "FarmBuffList": [], "FarmLogList": [], "GoodsList": [], "GoodsRemainSecTime": 0}, ERROR_MESSAGE_DATA_ILLEGAL, str(exc))

    def _handle_mature_time(self, profile: Mapping[str, Any], body: bytes) -> FarmResult:
        try:
            parsed = _decode_request(body, "CS_FarmCropsMatureTime", {1: ("LandId", UINT32_MAX, False)})
            land_id = _scalar(parsed, 1)
            if land_id <= 0:
                raise FarmProfileError("LandId must be positive")
            root, now = self._state(profile, self._now())
            land = self._land(root["farm"], land_id)
            seed = self.catalog.seeds.get(int(land["seed_id"]))
            remaining = 0
            code = ERROR_SUCCESS
            if seed is None:
                code = ERROR_NO_REWARD_CAN_BE_TAKEN
            else:
                if self.management.tracked(root['farm'],land):
                    rate = self.management.rate(profile,root['farm'],land)
                    remaining = max(0, math.ceil((seed.harvest_index-land['harvest_value'])*10000/rate))
                else:
                    remaining = max(0, math.ceil(seed.harvest_index / 10 - max(0, now - int(land.get("planted_at", 0)))))
            return FarmResult(SC_FARM_CROPS_MATURE_TIME_RET, self.codec.encode("SC_FarmCropsMatureTimeRet", {"Errorcode": code, "LandId": land_id, "Time": remaining}), changes=self._changes(profile, root), error_code=code)
        except (FarmContentError, ProtobufDecodeError) as exc:
            return self._error(SC_FARM_CROPS_MATURE_TIME_RET, "SC_FarmCropsMatureTimeRet", {"Errorcode": ERROR_MESSAGE_DATA_ILLEGAL, "LandId": 0, "Time": 0}, ERROR_MESSAGE_DATA_ILLEGAL, str(exc))

    def handle(self, command: int, profile: Optional[Mapping[str, Any]], body: bytes = b"") -> Optional[FarmResult]:
        if command not in RESPONSE_IDS and command not in UNSUPPORTED_COMMANDS:
            return None
        account = profile if isinstance(profile, Mapping) else {}
        if command in (626,628,632,634,636,638,640,642,644,660):
            return self.management.handle(command,account,body)
        if command == CS_PLANT:
            return self._handle_plant(account, body)
        if command == CS_UPROOT:
            return self._handle_uproot(account, body)
        if command == CS_HARVEST:
            return self._handle_harvest(account, body)
        if command == CS_REQUEST_FARM_DATAS:
            return self._handle_query(account, body)
        if command == CS_FARM_CROPS_MATURE_TIME:
            return self._handle_mature_time(account, body)
        if command == CS_REQUEST_FARMLAND_INFO:
            try:
                live_snapshot = _decode_overview(body)
                root, now = self._state(account, self._now())
                extra = self.management.automatic_harvest(account,root,now)
                lands = self._available_lands(root["farm"])
                notes = self.management.notifications(account,root['farm'],now,force=False) if 'management' in root['farm'] else (self._notify(lands,now),)
                return FarmResult(SC_REQUEST_FARMLAND_INFO_RET, self.codec.encode("SC_RequestFarmlandInfoRet", {"CanHarvestNum": sum(1 for land in lands if land["seed_id"] and land["harvest_value"] >= self.catalog.seeds[land["seed_id"]].harvest_index), "EmptyNum": sum(1 for land in lands if not land["seed_id"]), "Trusteeship": int(root["farm"].get("trusteeship", 0))}), changes={**self._changes(account,root),**extra}, notifications=notes if live_snapshot else ())
            except (FarmContentError, ProtobufDecodeError) as exc:
                return self._error(SC_REQUEST_FARMLAND_INFO_RET, "SC_RequestFarmlandInfoRet", {"CanHarvestNum": 0, "EmptyNum": 0, "Trusteeship": 0}, ERROR_MESSAGE_DATA_ILLEGAL, str(exc))
        # The tables do not close over the server formulas for these two
        # write operations.  Keep their response shape, but never mutate.
        response_id, reason = UNSUPPORTED_COMMANDS[command]
        if command == CS_WATERING:
            return self._error(response_id, "SC_WateringRet", {"Errorcode": ERROR_CONFIG, "LandId": 0, "Humidity": FARM_DEFAULT_HUMIDITY}, ERROR_CONFIG, reason)
        return self._error(response_id, "SC_ApplyFertilizerRet", {"Errorcode": ERROR_CONFIG, "LandId": 0, "ItemId": FARM_FERTILIZER_ITEM_ID}, ERROR_CONFIG, reason)


def default_layer() -> FarmContentLayer:
    return FarmContentLayer()


Layer = FarmContentLayer
Result = FarmResult


__all__ = [name for name in globals() if name.startswith(("CS_", "SC_", "NOTIFY_", "ERROR_"))] + ["FarmCatalog", "FarmContentError", "FarmContentLayer", "FarmProfileError", "FarmResult", "RESPONSE_IDS", "Layer", "Result", "default_layer"]
