"""Catalog-backed rune, decoration, appearance, and medal snapshots.

The compatibility server owns persistence and locking.  This layer accepts a
profile snapshot and returns shallow-mergeable changes, matching the other
hand-written service layers in :mod:`server`.  Static identifiers always come
from :class:`content_profile.ContentCatalog`; protobuf bodies are encoded by
the defined schema in :mod:`protobuf_codec`.
"""

from __future__ import annotations

import copy
import hashlib
import json
import random
import re
from collections import defaultdict
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import (
    Any,
    DefaultDict,
    Dict,
    Iterable,
    List,
    Mapping,
    MutableMapping,
    Optional,
    Sequence,
    Tuple,
)

try:  # Support package imports and direct ``python server/collectibles.py`` use.
    from .content_profile import ContentCatalog, DEFAULT_CONTENT_DIRECTORY
    from .inventory_content import (
        InventoryContentError,
        make_update_item_packet,
        normalize_inventory,
    )
    from .protobuf_codec import ProtocolMessageCodec
    from .rune_recast_policy import POLICY_VERSION, RuneRecastPolicyError, generate_candidate
except ImportError:  # pragma: no cover - exercised only by direct script use.
    from content_profile import ContentCatalog, DEFAULT_CONTENT_DIRECTORY
    from inventory_content import (  # type: ignore
        InventoryContentError,
        make_update_item_packet,
        normalize_inventory,
    )
    from protobuf_codec import ProtocolMessageCodec
    from rune_recast_policy import POLICY_VERSION, RuneRecastPolicyError, generate_candidate


# MsgID values defined in generated/protocol/protocol_catalog.json.
CS_REQUEST_DECORATION_ITEM_LIST = 289
SC_DECORATION_ITEM_LIST_RET = 290
NOTIFY_UPDATE_DECORATION_ITEM = 291
GOSPEL_DECORATION_LINK_REVISION = 1
GOSPEL_DECORATION_LINK_REVISION_KEY = "gospel_decoration_link_revision"
CS_REQUEST_HERO_APPEARANCE_DATAS = 292
SC_HERO_APPEARANCE_DATAS_RET = 293
CS_SAVE_HERO_APPEARANCE = 294
SC_SAVE_HERO_APPEARANCE_RET = 295
CS_SET_HERO_CUR_APPEARANCE_ID = 296
SC_SET_HERO_CUR_APPEARANCE_ID_RET = 297

CS_REQUEST_MEDAL_DATAS = 349
SC_MEDAL_DATAS_RET = 350
CS_VIEW_MEDAL = 395
SC_VIEW_MEDAL_RET = 396
CS_REQUEST_MEDAL_SLOT_DATAS = 397
SC_MEDAL_SLOT_DATAS_RET = 398
CS_SET_MEDAL_SLOT = 399
SC_SET_MEDAL_SLOT_RET = 400
CS_SET_MEDAL_STATE = 401
SC_SET_MEDAL_STATE_RET = 402
CS_SET_ALL_MEDAL_STATE = 403
SC_SET_ALL_MEDAL_STATE_RET = 404
CS_TAKE_MEDAL = 504
SC_TAKE_MEDAL_RET = 505

# SC_RuneDatas is an unsolicited login snapshot.  The defined protocol has
# no CS_RequestRuneDatas command.
CS_RECAST_RUNE = 947
SC_RECAST_RUNE_RET = 948
CS_RECAST_RUNE_RESULT = 949
SC_RECAST_RUNE_RESULT_RET = 950
CS_UPGRADE_RUNE = 951
SC_UPGRADE_RUNE_RET = 952
NOTIFY_UPDATE_RUNE = 953
SC_RUNE_DATAS = 954
CS_LOCK_RUNE = 955
SC_LOCK_RUNE_RET = 956
CS_DECOMPOSE_RUNE = 957
SC_DECOMPOSE_RUNE_RET = 958
CS_EQUIP_RUNE = 959
SC_EQUIP_RUNE_RET = 960
NOTIFY_UPDATE_HERO_RUNE = 1088

# The current Managed preset UI uses the generic protocol below.  Commands
# 961-966 belong to an older decoration/rune preset panel and intentionally
# remain outside this layer until a live caller requires the compatibility
# path.
CS_REQUEST_PRESETS = 1330
SC_REQUEST_PRESETS_RET = 1331
CS_SAVE_PRESETS = 1332
SC_SAVE_PRESETS_RET = 1333
CS_USE_PRESETS = 1334
SC_USE_PRESETS_RET = 1335

RESPONSE_IDS = {
    CS_REQUEST_DECORATION_ITEM_LIST: SC_DECORATION_ITEM_LIST_RET,
    CS_REQUEST_HERO_APPEARANCE_DATAS: SC_HERO_APPEARANCE_DATAS_RET,
    CS_SAVE_HERO_APPEARANCE: SC_SAVE_HERO_APPEARANCE_RET,
    CS_SET_HERO_CUR_APPEARANCE_ID: SC_SET_HERO_CUR_APPEARANCE_ID_RET,
    CS_REQUEST_MEDAL_DATAS: SC_MEDAL_DATAS_RET,
    CS_VIEW_MEDAL: SC_VIEW_MEDAL_RET,
    CS_REQUEST_MEDAL_SLOT_DATAS: SC_MEDAL_SLOT_DATAS_RET,
    CS_SET_MEDAL_SLOT: SC_SET_MEDAL_SLOT_RET,
    CS_SET_MEDAL_STATE: SC_SET_MEDAL_STATE_RET,
    CS_SET_ALL_MEDAL_STATE: SC_SET_ALL_MEDAL_STATE_RET,
    CS_TAKE_MEDAL: SC_TAKE_MEDAL_RET,
    CS_RECAST_RUNE: SC_RECAST_RUNE_RET,
    CS_RECAST_RUNE_RESULT: SC_RECAST_RUNE_RESULT_RET,
    CS_UPGRADE_RUNE: SC_UPGRADE_RUNE_RET,
    CS_LOCK_RUNE: SC_LOCK_RUNE_RET,
    CS_DECOMPOSE_RUNE: SC_DECOMPOSE_RUNE_RET,
    CS_EQUIP_RUNE: SC_EQUIP_RUNE_RET,
    CS_REQUEST_PRESETS: SC_REQUEST_PRESETS_RET,
    CS_SAVE_PRESETS: SC_SAVE_PRESETS_RET,
    CS_USE_PRESETS: SC_USE_PRESETS_RET,
}

ERROR_SUCCESS = 0
ERROR_ITEM_NUM_NOT_ENOUGH = 110
ERROR_MATERIAL_ILLEGAL = 117
ERROR_EXCEED_ITEM_LIMIT_COUNT = 429
ERROR_PRESET_DECORATION_EMPTY = 514
ERROR_PRESET_RUNE_EMPTY = 515
ERROR_MESSAGE_DATA_ILLEGAL = 32769

RUNE_UID_BASE = 3_000_000_001
RUNE_CREATE_TIME_BASE = 1_700_000_000
RUNE_POWDER_ITEM_ID = 1_001_034
RUNE_POWDER_EXP = 10
RUNE_RECAST_KEY = "rune_recast"
RUNE_RECAST_ATTR_KEYS = tuple(
    key for index in range(1, 4)
    for key in (f"rand_attr_{index}", f"attr_{index}_quality", f"attr_{index}_value")
)
COLLECTIBLES_SEED_VERSION = 1
COLLECTIBLES_SEED_VERSION_KEY = "collectibles_seed_version"
PRESETS_V2_SCHEMA_VERSION = 1
PRESETS_V2_KEY = "presets_v2"
PRESET_TYPE_DECORATION = 1
PRESET_TYPE_RUNE = 2
PRESET_SLOT_COUNT = 3
MEDAL_TAKE_TIME_BASE = 1_700_100_000
MEDAL_SLOT_COUNT = 10
UINT32_MAX = (1 << 32) - 1
UINT64_MAX = (1 << 64) - 1

EXPECTED_TABLE_COUNTS = {
    "a_rune_main": 80,
    "a_rune_growth_cost": 60,
    "a_secondary_attr_strengthen": 214,
    "a_decoration": 117,
    "a_medal": 347,
}
COMPATIBLE_EXTENSION_TABLE_COUNTS = {
    # The 2.0 compatibility catalog adds resource-closed character cosmetics.
    # 119 is the current Elvira/Clarissa closure; 120 is reserved for Menghe
    # if her missing model bundle is defined later. The canonical 1.0 root
    # remains exactly 117.
    "a_decoration": frozenset({117, 119, 120}),
}


class CollectiblesConfigError(ValueError):
    """Raised when the generated collectible tables are incomplete."""


class CollectiblesRequestError(ValueError):
    """Raised internally for malformed or catalog-invalid request data."""


@dataclass(frozen=True)
class CollectiblesResult:
    """One protobuf response plus a pure shallow-merge profile patch."""

    response_id: int
    body: bytes
    changes: Dict[str, Any] = field(default_factory=dict)
    error_code: int = ERROR_SUCCESS
    reason: str = ""
    notifications: Tuple[Tuple[int, bytes], ...] = ()
    post_notifications: Tuple[Tuple[int, bytes], ...] = ()

    @property
    def ok(self) -> bool:
        return self.error_code == ERROR_SUCCESS


WireField = Tuple[int, object]


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
    raise CollectiblesRequestError("invalid or truncated protobuf varint")


def _wire_fields(data: bytes) -> Dict[int, List[WireField]]:
    """Decode the small protobuf request surface used by this layer."""

    fields: DefaultDict[int, List[WireField]] = defaultdict(list)
    offset = 0
    while offset < len(data):
        tag, offset = _read_varint(data, offset)
        number, wire_type = tag >> 3, tag & 7
        if number <= 0:
            raise CollectiblesRequestError("protobuf field number zero")
        if wire_type == 0:
            value, offset = _read_varint(data, offset)
        elif wire_type == 1:
            end = offset + 8
            if end > len(data):
                raise CollectiblesRequestError("truncated fixed64 protobuf field")
            value = data[offset:end]
            offset = end
        elif wire_type == 2:
            size, offset = _read_varint(data, offset)
            end = offset + size
            if end > len(data):
                raise CollectiblesRequestError("truncated length-delimited protobuf field")
            value = data[offset:end]
            offset = end
        elif wire_type == 5:
            end = offset + 4
            if end > len(data):
                raise CollectiblesRequestError("truncated fixed32 protobuf field")
            value = data[offset:end]
            offset = end
        else:
            raise CollectiblesRequestError(
                f"unsupported protobuf wire type {wire_type}"
            )
        fields[number].append((wire_type, value))
    return dict(fields)


def _last_varint(
    fields: Mapping[int, Sequence[WireField]],
    number: int,
    default: int = 0,
) -> int:
    values = fields.get(number)
    if not values:
        return default
    wire_type, value = values[-1]
    if wire_type != 0 or not isinstance(value, int):
        raise CollectiblesRequestError(f"protobuf field {number} must be a varint")
    return int(value)


def _messages(
    fields: Mapping[int, Sequence[WireField]], number: int
) -> Tuple[bytes, ...]:
    result = []
    for wire_type, value in fields.get(number, ()):
        if wire_type != 2 or not isinstance(value, bytes):
            raise CollectiblesRequestError(f"protobuf field {number} must be a message")
        result.append(value)
    return tuple(result)


def _repeated_varints(
    fields: Mapping[int, Sequence[WireField]], number: int
) -> Tuple[int, ...]:
    """Decode both packed and unpacked repeated protobuf integers."""

    result: List[int] = []
    for wire_type, value in fields.get(number, ()):
        if wire_type == 0 and isinstance(value, int):
            result.append(int(value))
            continue
        if wire_type == 2 and isinstance(value, bytes):
            offset = 0
            while offset < len(value):
                item, offset = _read_varint(value, offset)
                result.append(item)
            continue
        raise CollectiblesRequestError(
            f"protobuf field {number} must be a repeated varint"
        )
    return tuple(result)


def _last_message(
    fields: Mapping[int, Sequence[WireField]], number: int
) -> Optional[bytes]:
    values = _messages(fields, number)
    return values[-1] if values else None


def _last_string(
    fields: Mapping[int, Sequence[WireField]], number: int, default: str = ""
) -> str:
    raw = _last_message(fields, number)
    if raw is None:
        return default
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CollectiblesRequestError(
            f"protobuf field {number} is not valid UTF-8"
        ) from exc


def _signed_int32(value: int) -> int:
    value &= UINT32_MAX
    return value - (1 << 32) if value & (1 << 31) else value


def _as_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _uint(value: Any, default: int = 0, maximum: int = UINT64_MAX) -> int:
    parsed = _as_int(value, default)
    return parsed if 0 <= parsed <= maximum else default


def _value(row: Mapping[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        if name in row:
            return row[name]
    return default


def _lookup(mapping: Mapping[Any, Any], key: int, default: Any = None) -> Any:
    if key in mapping:
        return mapping[key]
    return mapping.get(str(key), default)


def _row_index(value: Any, *id_names: str) -> Dict[int, Mapping[str, Any]]:
    """Index the first row per positive ID from a list or JSON object."""

    result: Dict[int, Mapping[str, Any]] = {}
    if isinstance(value, Mapping):
        candidates: Iterable[Tuple[Any, Any]] = value.items()
    elif isinstance(value, (list, tuple)):
        candidates = enumerate(value)
    else:
        return result
    for key, candidate in candidates:
        if not isinstance(candidate, Mapping):
            continue
        row_id = _as_int(_value(candidate, *id_names, default=key), 0)
        if row_id > 0 and row_id not in result:
            result[row_id] = candidate
    return result


def _parse_quality_value(value: Any) -> int:
    """Pick the deterministic upper endpoint from a defined ``[max,min]``."""

    if not isinstance(value, str):
        return 0
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return 0
    if not isinstance(parsed, list) or not parsed:
        return 0
    return _uint(parsed[0], 0, UINT32_MAX)


class CollectiblesLayer:
    """Build and mutate collectible state without owning persistence."""

    def __init__(
        self,
        root: Any = DEFAULT_CONTENT_DIRECTORY,
        *,
        protocol_catalog_path: Optional[Path] = None,
        codec: Optional[ProtocolMessageCodec] = None,
    ) -> None:
        self.catalog = root if isinstance(root, ContentCatalog) else ContentCatalog.load(root)
        self.codec = codec or ProtocolMessageCodec(protocol_catalog_path)

        self.rune_table = self.catalog.table("a_rune_main")
        self.rune_growth_table = self.catalog.table("a_rune_growth_cost")
        self.rune_recast_cost_table = self.catalog.table(
            "a_secondary_attr_strengthen"
        )
        self.decoration_table = self.catalog.table("a_decoration")
        self.medal_table = self.catalog.table("a_medal")
        self.medal_passive_table = self.catalog.table("a_medalpassive")
        self.item_table = self.catalog.table("a_item")
        self.rune_rows = self.rune_table.records
        self.rune_growth_rows = self.rune_growth_table.records
        self.rune_recast_cost_rows = self.rune_recast_cost_table.records
        self.decoration_rows = self.decoration_table.records
        self.medal_rows = self.medal_table.records
        self.medal_passive_rows = self.medal_passive_table.records
        self.rune_ids = self.rune_table.ids
        self.decoration_ids = self.decoration_table.ids
        self.medal_ids = self.medal_table.ids
        self.rune_id_set = frozenset(self.rune_ids)
        self.rune_static = {
            _as_int(row.get("id")): row for row in self.rune_rows
        }
        self.rune_growth = {
            (_as_int(row.get("rune_quality")), _as_int(row.get("rune_level"))): row
            for row in self.rune_growth_rows
        }
        self.rune_recast_cost = {
            _as_int(row.get("rune_group")): row
            for row in self.rune_recast_cost_rows
            if _as_int(row.get("rune_group")) > 0
        }
        self.rune_max_level = {
            quality: max(
                level
                for row_quality, level in self.rune_growth
                if row_quality == quality
            )
            for quality in (1, 2, 3)
        }
        self.item_max_stack = {
            _as_int(row.get("id")): _uint(
                row.get("max_stack_num"), 0, UINT32_MAX
            )
            for row in self.item_table.records
        }
        self.decoration_id_set = frozenset(self.decoration_ids)
        self.gospel_decoration_bindings = {
            int(row["id"]): (_as_int(row.get("gospel_id")), _as_int(row.get("attr_basic")))
            for row in self.decoration_rows
            if _as_int(row.get("gospel_id")) > 0 and _as_int(row.get("attr_basic")) > 0
        }
        # GospelAwakeView reads this exact table entry, not a Wiki-derived
        # offset. Stored Gospel awake levels include the initial stage (1..6).
        config = self.catalog.table("a_global").by_id.get(9_991_055)
        try:
            steps = json.loads(str(config.get("string_value", ""))) if config else None
        except (ValueError, TypeError):
            steps = None
        if (not isinstance(steps, list) or not steps or steps[0] != 1
                or any(type(step) is not int or step <= 0 for step in steps)
                or steps != sorted(set(steps))):
            raise ValueError("a_global[9991055] must define ordered awakening decoration stages")
        self.gospel_decoration_awake_steps = tuple(steps)
        self.gospel_real_awake_levels: Dict[int, set[int]] = {}
        for row in self.catalog.table("a_gospel_awake").records:
            self.gospel_real_awake_levels.setdefault(int(row["gospel_id"]), set()).add(int(row["awaken_level"]))
        self.medal_id_set = frozenset(self.medal_ids)
        self.medal_passive_types = tuple(
            sorted(
                {
                    _as_int(row.get("medalpassive_type"))
                    for row in self.medal_passive_rows
                    if _as_int(row.get("medalpassive_type")) > 0
                }
            )
        )
        self.medal_passive_type_set = frozenset(self.medal_passive_types)
        self._validate_tables()
        self._main_attr_by_pool, self._secondary_attrs_by_pool = self._rune_attr_rows()

    def _validate_tables(self) -> None:
        for table_name, expected in EXPECTED_TABLE_COUNTS.items():
            actual = self.catalog.table(table_name).count
            allowed = COMPATIBLE_EXTENSION_TABLE_COUNTS.get(
                table_name,
                frozenset({expected}),
            )
            if actual not in allowed:
                raise CollectiblesConfigError(
                    f"{table_name} has {actual} rows; expected one of "
                    f"{sorted(allowed)}"
                )
        required_fields = {
            "a_rune_main": {
                "id",
                "position",
                "quality",
                "main_attr_poor",
                "secondary_attr_poor",
                "secondary_attr_quality_limit",
            },
            "a_rune_growth_cost": {
                "rune_quality",
                "rune_level",
                "cost",
                "change_into_experience",
                "change_into_up_item",
            },
            "a_secondary_attr_strengthen": {
                "rune_group",
                "item_id_cost",
                "item_counts",
                "item_id_cost_1_locked",
                "item_counts_1_locked",
                "item_id_cost_2_locked",
                "item_counts_2_locked",
            },
            "a_decoration": {"id", "attr_basic"},
            "a_medal": {"id", "medalpassive_type"},
            "a_medalpassive": {"id", "medalpassive_type"},
        }
        for table_name, required in required_fields.items():
            missing = required - self.catalog.table(table_name).fields
            if missing:
                raise CollectiblesConfigError(
                    f"{table_name} is missing collectible fields: {sorted(missing)}"
                )
        missing_recast_costs = sorted(
            {
                _as_int(row.get("strengthen_group"))
                for row in self.rune_rows
                if _as_int(row.get("strengthen_group")) > 0
            }
            - self.rune_recast_cost.keys()
        )
        if missing_recast_costs:
            raise CollectiblesConfigError(
                "a_secondary_attr_strengthen is missing rune groups: "
                + ", ".join(str(value) for value in missing_recast_costs)
            )
        if not self.medal_passive_types:
            raise CollectiblesConfigError("a_medalpassive contains no passive type")

    def _rune_attr_rows(
        self,
    ) -> Tuple[Dict[int, Mapping[str, Any]], Dict[int, Tuple[Mapping[str, Any], ...]]]:
        """Resolve deterministic attribute rows when the supporting tables exist."""

        main: Dict[int, Mapping[str, Any]] = {}
        secondary: Dict[int, List[Mapping[str, Any]]] = {}
        main_table = self.catalog.tables.get("a_main_attr_poor")
        if main_table is not None:
            for row in main_table.records:
                pool = _as_int(row.get("main_attr_poor_id"))
                if pool > 0 and pool not in main:
                    main[pool] = row
        secondary_table = self.catalog.tables.get("a_secondary_attr_poor")
        if secondary_table is not None:
            groups_seen: Dict[int, set] = {}
            for row in secondary_table.records:
                pool = _as_int(row.get("secondary_attr_poor"))
                group = _as_int(row.get("attr_group"))
                if pool <= 0 or group in groups_seen.setdefault(pool, set()):
                    continue
                groups_seen[pool].add(group)
                secondary.setdefault(pool, []).append(row)
        return main, {key: tuple(rows) for key, rows in secondary.items()}

    @staticmethod
    def _entity_uids(profile: Mapping[str, Any], key: str) -> Tuple[int, ...]:
        rows = profile.get(key)
        if not isinstance(rows, (list, tuple)):
            return ()
        result = []
        for row in rows:
            if isinstance(row, Mapping):
                uid = _uint(_value(row, "uid", "HeroUId", "hero_uid"))
                if uid > 0 and uid not in result:
                    result.append(uid)
        return tuple(result)

    def _seed_runes(
        self,
        profile: MutableMapping[str, Any],
        *,
        add_missing_catalog_rows: bool,
    ) -> None:
        # An owned RuneItem is an *instance*, not a catalog row.  The old
        # implementation indexed by static rune ID and therefore collapsed
        # every duplicate earned from battle the next time an all-unlocked
        # profile was seeded.  Keep every valid instance, while still adding
        # one deterministic baseline instance for static IDs that are absent.
        existing_by_id: DefaultDict[int, List[Mapping[str, Any]]] = defaultdict(list)
        raw_existing = profile.get("rune_items")
        if isinstance(raw_existing, Mapping):
            candidates: Iterable[Tuple[Any, Any]] = raw_existing.items()
        elif isinstance(raw_existing, (list, tuple)):
            candidates = enumerate(raw_existing)
        else:
            candidates = ()
        for key, candidate_row in candidates:
            if not isinstance(candidate_row, Mapping):
                continue
            rune_id = _as_int(
                _value(
                    candidate_row,
                    "id",
                    "Id",
                    "rune_id",
                    "RuneId",
                    default=key,
                ),
                0,
            )
            if rune_id in self.rune_id_set:
                existing_by_id[rune_id].append(candidate_row)

        ordered_instances: List[Tuple[Mapping[str, Any], Mapping[str, Any]]] = []
        for static in self.rune_rows:
            rune_id = int(static["id"])
            sources = existing_by_id.get(rune_id)
            if not sources and add_missing_catalog_rows:
                sources = [{}]
            if not sources:
                continue
            ordered_instances.extend((static, source) for source in sources)

        reserved = set(self._entity_uids(profile, "heroes"))
        reserved.update(self._entity_uids(profile, "gospels"))
        accepted_uids: List[int] = []
        used_uids = set(reserved)
        for _static, source in ordered_instances:
            uid = _uint(
                _value(source, "rune_item_uid", "uid", "RuneItemUId"),
                0,
            )
            if uid > 0 and uid not in used_uids:
                accepted_uids.append(uid)
                used_uids.add(uid)
            else:
                accepted_uids.append(0)

        candidate = RUNE_UID_BASE
        for index, uid in enumerate(accepted_uids):
            if uid:
                continue
            while candidate in used_uids:
                candidate += 1
            if candidate > UINT64_MAX:
                raise CollectiblesConfigError("rune UID range exhausted")
            accepted_uids[index] = candidate
            used_uids.add(candidate)
            candidate += 1

        rune_items = []
        for index, ((static, source), rune_uid) in enumerate(
            zip(ordered_instances, accepted_uids)
        ):
            rune_id = int(static["id"])
            row = dict(source)
            main_pool = _as_int(static.get("main_attr_poor"))
            main_row = self._main_attr_by_pool.get(main_pool)
            basic_attr = _as_int(main_row.get("id")) if main_row else main_pool
            secondary_pool = _as_int(static.get("secondary_attr_poor"))
            secondary_rows = self._secondary_attrs_by_pool.get(secondary_pool, ())[:3]
            quality_limit = max(
                1,
                min(4, _as_int(static.get("secondary_attr_quality_limit"), 1)),
            )
            random_ids = [
                _as_int(item.get("id")) for item in secondary_rows
            ]
            random_values = [
                _parse_quality_value(item.get(f"attr_value_quality_{quality_limit}"))
                for item in secondary_rows
            ]
            while len(random_ids) < 3:
                random_ids.append(0)
                random_values.append(0)
            rune_quality = max(1, min(3, _as_int(static.get("quality"), 1)))
            row.update(
                {
                    "id": rune_id,
                    "rune_item_uid": rune_uid,
                    "hero_uid": _uint(
                        _value(source, "hero_uid", "HeroUId"), 0
                    ),
                    "basic_attr": basic_attr,
                    "rand_attr_1": _uint(
                        _value(source, "rand_attr_1", "RandAttr1"),
                        random_ids[0],
                        UINT32_MAX,
                    ),
                    "rand_attr_2": _uint(
                        _value(source, "rand_attr_2", "RandAttr2"),
                        random_ids[1],
                        UINT32_MAX,
                    ),
                    "rand_attr_3": _uint(
                        _value(source, "rand_attr_3", "RandAttr3"),
                        random_ids[2],
                        UINT32_MAX,
                    ),
                    "attr_1_quality": _uint(
                        _value(source, "attr_1_quality", "Attr1Quality"),
                        quality_limit,
                        UINT32_MAX,
                    ),
                    "attr_2_quality": _uint(
                        _value(source, "attr_2_quality", "Attr2Quality"),
                        quality_limit,
                        UINT32_MAX,
                    ),
                    "attr_3_quality": _uint(
                        _value(source, "attr_3_quality", "Attr3Quality"),
                        quality_limit,
                        UINT32_MAX,
                    ),
                    "level": _uint(
                        _value(source, "level", "Level"),
                        rune_quality * 10,
                        UINT32_MAX,
                    ),
                    "cur_exp": _uint(
                        _value(source, "cur_exp", "CurExp"), 0, UINT32_MAX
                    ),
                    "state": _uint(
                        _value(source, "state", "State"), 0, UINT32_MAX
                    ),
                    "create_time": _uint(
                        _value(source, "create_time", "CreateTime"),
                        RUNE_CREATE_TIME_BASE + index,
                    ),
                    "attr_1_value": _uint(
                        _value(source, "attr_1_value", "Attr1Value"),
                        random_values[0],
                        UINT32_MAX,
                    ),
                    "attr_2_value": _uint(
                        _value(source, "attr_2_value", "Attr2Value"),
                        random_values[1],
                        UINT32_MAX,
                    ),
                    "attr_3_value": _uint(
                        _value(source, "attr_3_value", "Attr3Value"),
                        random_values[2],
                        UINT32_MAX,
                    ),
                }
            )
            rune_items.append(row)
        profile["rune_items"] = rune_items

    @staticmethod
    def _hero_identity_maps(
        profile: Mapping[str, Any],
    ) -> Tuple[frozenset[int], Dict[int, int]]:
        """Return valid static witch IDs and persistent-UID aliases.

        The runtime schema calls ``RuneItem.HeroUId`` a UID, but the managed
        battle/UI code compares it to ``a_people.id`` and the recorded equip
        request sends ``3000001``.  Keep that defined wire semantic while
        accepting old saves which used the actual persistent hero UID.
        """

        hero_ids = set()
        uid_to_id: Dict[int, int] = {}
        rows = profile.get("heroes")
        if isinstance(rows, (list, tuple)):
            for row in rows:
                if not isinstance(row, Mapping):
                    continue
                hero_id = _uint(_value(row, "id", "HeroId"), 0, UINT32_MAX)
                hero_uid = _uint(_value(row, "uid", "HeroUId", "hero_uid"))
                if hero_id > 0:
                    hero_ids.add(hero_id)
                    if hero_uid > 0:
                        uid_to_id[hero_uid] = hero_id
        return frozenset(hero_ids), uid_to_id

    @staticmethod
    def _hero_id_to_uid_map(profile: Mapping[str, Any]) -> Dict[int, int]:
        """Map a preset's static ``HeroId`` to appearance storage ``HeroUId``."""

        result: Dict[int, int] = {}
        rows = profile.get("heroes")
        if not isinstance(rows, (list, tuple)):
            return result
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            hero_id = _uint(_value(row, "id", "HeroId"), 0, UINT32_MAX)
            hero_uid = _uint(_value(row, "uid", "HeroUId", "hero_uid"))
            if hero_id > 0:
                # Real profiles have a persistent UID.  The static-ID fallback
                # keeps small fixtures and repaired legacy saves addressable.
                result[hero_id] = hero_uid or hero_id
        return result

    def _rune_position(self, rune_id: int) -> int:
        row = self.rune_static.get(int(rune_id))
        return _uint(row.get("position"), 0, UINT32_MAX) if row else 0

    def _normalize_rune_equipment(self, profile: MutableMapping[str, Any]) -> None:
        """Canonicalize equipment onto RuneItem rows and rebuild HeroRuneList."""

        rune_items = profile.get("rune_items")
        if not isinstance(rune_items, list):
            rune_items = []
        hero_ids, uid_to_id = self._hero_identity_maps(profile)
        by_uid = {
            _uint(row.get("rune_item_uid")): row
            for row in rune_items
            if isinstance(row, MutableMapping)
            and _uint(row.get("rune_item_uid")) > 0
        }

        # Migrate the older explicit HeroRuneList representation only when the
        # corresponding RuneItem has no authoritative owner yet.
        legacy = profile.get("hero_runes")
        if isinstance(legacy, (list, tuple)):
            for hero_rune in legacy:
                if not isinstance(hero_rune, Mapping):
                    continue
                raw_hero = _uint(_value(hero_rune, "hero_uid", "HeroUId"))
                hero_id = uid_to_id.get(raw_hero, raw_hero)
                if hero_id not in hero_ids:
                    continue
                slots = hero_rune.get("slots", hero_rune.get("hero_rune_slots", ()))
                if not isinstance(slots, (list, tuple)):
                    continue
                for slot in slots:
                    if not isinstance(slot, Mapping):
                        continue
                    rune_uid = _uint(
                        _value(slot, "rune_item_uid", "RuneItemUId")
                    )
                    rune = by_uid.get(rune_uid)
                    if rune is None or _uint(rune.get("hero_uid")) > 0:
                        continue
                    requested_position = _uint(
                        _value(slot, "position", "Position"), 0, UINT32_MAX
                    )
                    if requested_position and requested_position != self._rune_position(
                        _uint(rune.get("id"), 0, UINT32_MAX)
                    ):
                        continue
                    rune["hero_uid"] = hero_id

        # One witch can own at most one rune for each of the five static
        # positions.  Keep the lowest stable UID when repairing a legacy
        # conflict and clear every invalid owner.
        occupied: Dict[Tuple[int, int], int] = {}
        for rune in sorted(
            (row for row in rune_items if isinstance(row, MutableMapping)),
            key=lambda row: _uint(row.get("rune_item_uid")),
        ):
            raw_hero = _uint(rune.get("hero_uid"))
            hero_id = uid_to_id.get(raw_hero, raw_hero)
            position = self._rune_position(_uint(rune.get("id"), 0, UINT32_MAX))
            key = (hero_id, position)
            if (
                hero_id not in hero_ids
                or position not in {1, 2, 3, 4, 5}
                or key in occupied
            ):
                rune["hero_uid"] = 0
                continue
            rune["hero_uid"] = hero_id
            occupied[key] = _uint(rune.get("rune_item_uid"))

        grouped: DefaultDict[int, List[Dict[str, int]]] = defaultdict(list)
        for (hero_id, position), rune_uid in occupied.items():
            grouped[hero_id].append(
                {"position": position, "rune_item_uid": rune_uid}
            )
        profile["hero_runes"] = [
            {
                "hero_uid": hero_id,
                "slots": sorted(grouped[hero_id], key=lambda row: row["position"]),
            }
            for hero_id in sorted(grouped)
        ]

    def _seed_decorations(self, profile: MutableMapping[str, Any]) -> None:
        existing = _row_index(profile.get("decoration_items"), "id", "Id")
        rows = []
        owned_gospels = {int(row["id"]) for row in profile.get("gospels", [])}
        for static in self.decoration_rows:
            decoration_id = int(static["id"])
            if (profile.get("player_progression") and decoration_id not in existing
                    and int(static.get("gospel_id", 0)) not in owned_gospels):
                continue
            source = existing.get(decoration_id, {})
            row = dict(source)
            basic_attr = _uint(static.get("attr_basic"), 0, UINT32_MAX)
            basic_attr_level = _uint(
                _value(source, "basic_attr_level", "BasicAttrLvl"),
                1 if basic_attr else 0,
                UINT32_MAX,
            )
            if basic_attr and basic_attr_level == 0:
                # Old/incomplete saves can contain zero here, while the runtime
                # creates owned combat decorations at level one.  Repair the
                # persisted boundary before PassiveBuff consumes the value.
                basic_attr_level = 1
            row.update(
                {
                    "id": decoration_id,
                    "basic_attr": basic_attr,
                    "hero_id": _uint(
                        _value(source, "hero_id", "HeroId"), 0, UINT32_MAX
                    ),
                    "basic_attr_level": basic_attr_level,
                    "is_show": 1,
                }
            )
            rows.append(row)
        profile["decoration_items"] = rows

    def grant_gospel_decorations(self, profile: MutableMapping[str, Any], gospel_ids) -> None:
        """First ownership grants only the source-bound decorations, once."""
        owned = {int(row["id"]) for row in profile.get("gospels", [])}
        targets = set(gospel_ids) & owned
        rows = list(profile.get("decoration_items", []))
        existing = {int(row["id"]) for row in rows}
        for static in self.decoration_rows:
            item_id = int(static["id"])
            if int(static.get("gospel_id", 0)) not in targets or item_id in existing:
                continue
            passive = int(static.get("attr_basic", 0))
            rows.append({"id": item_id, "basic_attr": passive,
                         "basic_attr_level": 1 if passive else 0,
                         "hero_id": 0, "is_show": 1})
            existing.add(item_id)
        profile["decoration_items"] = rows
        self.sync_gospel_decoration_levels(profile)

    def sync_gospel_decoration_levels(
        self, profile: MutableMapping[str, Any], *, gospel_id: Optional[int] = None
    ) -> Tuple[Dict[str, Any], ...]:
        """Repair only owned, source-bound combat decorations in a candidate.

        Does not grant ownership, touch runes/appearance selection, or infer
        effects for cosmetic/2.0-only attr_basic=0 items. Fake awakening is a
        display/combat override on the Gospel, not this item's owned upgrade.
        Call under the account transaction when persisting the returned rows.
        """
        levels: Dict[int, int] = {}
        ambiguous: set[int] = set()
        gospels = profile.get("gospels")
        for row in gospels if isinstance(gospels, (list, tuple)) else ():
            if not isinstance(row, Mapping):
                continue
            owner, awake = row.get("id"), row.get("awake_level")
            if type(owner) is not int or type(awake) is not int:
                continue
            if awake not in self.gospel_real_awake_levels.get(owner, ()):
                continue
            if owner in levels and levels[owner] != awake:
                ambiguous.add(owner)
            levels[owner] = awake
        existing = profile.get("decoration_items")
        if not isinstance(existing, list):
            return ()
        changed = []
        rows = list(existing)
        for index, source in enumerate(existing):
            if not isinstance(source, Mapping):
                continue
            item_id = _as_int(_value(source, "id", "Id"))
            binding = self.gospel_decoration_bindings.get(item_id)
            if binding is None:
                continue
            owner, passive = binding
            if owner not in levels or owner in ambiguous or (gospel_id is not None and owner != gospel_id):
                continue
            level = sum(step <= levels[owner] for step in self.gospel_decoration_awake_steps)
            if _as_int(_value(source, "basic_attr_level", "BasicAttrLvl")) == level:
                continue
            row = dict(source)
            row.update(id=item_id, basic_attr=passive, basic_attr_level=level,
                       hero_id=_uint(_value(source, "hero_id", "HeroId"), 0, UINT32_MAX),
                       is_show=_uint(_value(source, "is_show", "IsShow"), 1, UINT32_MAX))
            if "BasicAttrLvl" in row:
                row["BasicAttrLvl"] = level
            rows[index] = row
            changed.append(row)
        if changed:
            profile["decoration_items"] = rows
        return tuple(changed)

    @staticmethod
    def _appearance_rows(container: Any) -> Tuple[Any, ...]:
        if isinstance(container, Mapping):
            rows = container.get("appearances", container.get("appearance_list", ()))
            if isinstance(rows, (list, tuple)):
                return tuple(rows)
        elif isinstance(container, (list, tuple)):
            return tuple(container)
        return ()

    def _normalize_decoration(self, value: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
        decoration_id = _uint(_value(value, "id", "Id"), 0, UINT32_MAX)
        if decoration_id not in self.decoration_id_set:
            return None
        return {
            "position": _uint(_value(value, "position", "Position"), 0, UINT32_MAX),
            "id": decoration_id,
            "length": _as_int(_value(value, "length", "Length"), 0),
            "width": _as_int(_value(value, "width", "Width"), 0),
            "depth": _as_int(_value(value, "depth", "Depth"), 0),
            "rotate_x": _as_int(_value(value, "rotate_x", "Rotatex"), 0),
            "rotate_y": _as_int(_value(value, "rotate_y", "Rotatey"), 0),
            "rotate_z": _as_int(_value(value, "rotate_z", "Rotatez"), 0),
            "size": _as_int(_value(value, "size", "Size"), 0),
            "is_self_define": _uint(
                _value(value, "is_self_define", "IsSelfDefine"), 0, UINT32_MAX
            ),
            "model_id": _uint(_value(value, "model_id", "ModelId"), 0, UINT32_MAX),
            "is_show": _uint(_value(value, "is_show", "IsShow"), 1, UINT32_MAX),
        }

    def _normalize_appearance(
        self, value: Mapping[str, Any], current_id: int
    ) -> Dict[str, Any]:
        appearance_id = _uint(_value(value, "id", "Id"), 0, UINT32_MAX)
        raw_decorations = value.get("decorations", value.get("decoration_list", ()))
        decorations = []
        if isinstance(raw_decorations, (list, tuple)):
            for raw in raw_decorations:
                if isinstance(raw, Mapping):
                    normalized = self._normalize_decoration(raw)
                    if normalized is not None:
                        decorations.append(normalized)
        name = _value(value, "name", "Name", default="")
        if not isinstance(name, str) or not name:
            name = "Default" if appearance_id == 0 else f"Preset {appearance_id}"
        return {
            "id": appearance_id,
            "decorations": decorations,
            "is_used": 1 if appearance_id == current_id else 0,
            "name": name[:64],
        }

    def _seed_appearances(self, profile: MutableMapping[str, Any]) -> None:
        hero_uids = self._entity_uids(profile, "heroes") or (1_000_000_001,)
        existing_root = profile.get("hero_appearances")
        existing_root = existing_root if isinstance(existing_root, Mapping) else {}
        root: Dict[str, Any] = {}
        for hero_uid in hero_uids:
            container = _lookup(existing_root, hero_uid, {})
            current_id = (
                _uint(container.get("current_id"), 0, UINT32_MAX)
                if isinstance(container, Mapping)
                else 0
            )
            indexed: Dict[int, Mapping[str, Any]] = {}
            for raw in self._appearance_rows(container):
                if not isinstance(raw, Mapping):
                    continue
                appearance_id = _uint(_value(raw, "id", "Id"), 0, UINT32_MAX)
                indexed[appearance_id] = raw
            indexed.setdefault(0, {"id": 0, "name": "Default"})
            if current_id not in indexed:
                current_id = 0
            appearances = [
                self._normalize_appearance(indexed[key], current_id)
                for key in sorted(indexed)
            ]
            root[str(hero_uid)] = {
                "current_id": current_id,
                "appearances": appearances,
            }
        profile["hero_appearances"] = root

    @staticmethod
    def _default_preset_slot(preset_type: int, preset_id: int) -> Dict[str, Any]:
        return {
            "preset_id": preset_id,
            "name": f"预设{preset_id + 1}",
            "uid_list": [],
            "decoration_info": {"decorations": []}
            if preset_type == PRESET_TYPE_DECORATION
            else None,
        }

    def _normalize_preset_slot(
        self, preset_type: int, preset_id: int, source: Any
    ) -> Dict[str, Any]:
        slot = self._default_preset_slot(preset_type, preset_id)
        if not isinstance(source, Mapping):
            return slot

        name = _value(source, "name", "preset_name", "PresetName", default="")
        if isinstance(name, str) and name:
            slot["name"] = name[:64]

        raw_uids = source.get("uid_list", source.get("UidList", ()))
        uid_list: List[int] = []
        if isinstance(raw_uids, (list, tuple)):
            for raw_uid in raw_uids:
                uid = _uint(raw_uid)
                if uid <= 0:
                    continue
                if preset_type == PRESET_TYPE_RUNE and uid in uid_list:
                    continue
                if (
                    preset_type == PRESET_TYPE_DECORATION
                    and uid not in self.decoration_id_set
                ):
                    continue
                uid_list.append(uid)

        if preset_type == PRESET_TYPE_RUNE:
            slot["uid_list"] = uid_list
            return slot

        raw_info = source.get(
            "decoration_info", source.get("PresetDecorationInfo", {})
        )
        raw_decorations: Any = ()
        if isinstance(raw_info, Mapping):
            raw_decorations = raw_info.get(
                "decorations",
                raw_info.get("decoration_list", raw_info.get("DecorationList", ())),
            )
        decorations = []
        if isinstance(raw_decorations, (list, tuple)):
            for raw_decoration in raw_decorations:
                if not isinstance(raw_decoration, Mapping):
                    continue
                normalized = self._normalize_decoration(raw_decoration)
                if normalized is not None:
                    decorations.append(normalized)
        if decorations:
            # The full transform records are authoritative for an appearance
            # preset.  UidList is the runtime-side catalog lookup mirror.
            uid_list = [row["id"] for row in decorations]
        else:
            # A decoration preset cannot be rebuilt from catalog IDs
            # alone because placement/scale/rotation live in DecorationInfo.
            uid_list = []
        slot["uid_list"] = uid_list
        slot["decoration_info"] = {"decorations": decorations}
        return slot

    def _seed_presets_v2(self, profile: MutableMapping[str, Any]) -> None:
        """Normalize the current three-slot preset model without losing saves."""

        raw_root = profile.get(PRESETS_V2_KEY)
        raw_root = raw_root if isinstance(raw_root, Mapping) else {}
        raw_by_type = raw_root.get("by_type")
        raw_by_type = raw_by_type if isinstance(raw_by_type, Mapping) else {}
        hero_ids, _uid_to_id = self._hero_identity_maps(profile)
        by_type: Dict[str, Dict[str, Any]] = {}

        for preset_type in (PRESET_TYPE_DECORATION, PRESET_TYPE_RUNE):
            raw_heroes = _lookup(raw_by_type, preset_type, {})
            raw_heroes = raw_heroes if isinstance(raw_heroes, Mapping) else {}
            heroes: Dict[str, Any] = {}
            for hero_id in sorted(hero_ids):
                raw_group = _lookup(raw_heroes, hero_id, {})
                raw_group = raw_group if isinstance(raw_group, Mapping) else {}
                raw_slots = raw_group.get("slots", ())
                indexed_slots: Dict[int, Mapping[str, Any]] = {}
                if isinstance(raw_slots, Mapping):
                    candidates: Iterable[Tuple[Any, Any]] = raw_slots.items()
                elif isinstance(raw_slots, (list, tuple)):
                    candidates = enumerate(raw_slots)
                else:
                    candidates = ()
                for key, raw_slot in candidates:
                    if not isinstance(raw_slot, Mapping):
                        continue
                    slot_id = _as_int(
                        _value(
                            raw_slot,
                            "preset_id",
                            "PresetsId",
                            default=key,
                        ),
                        -1,
                    )
                    if 0 <= slot_id < PRESET_SLOT_COUNT and slot_id not in indexed_slots:
                        indexed_slots[slot_id] = raw_slot

                slots = [
                    self._normalize_preset_slot(
                        preset_type, preset_id, indexed_slots.get(preset_id)
                    )
                    for preset_id in range(PRESET_SLOT_COUNT)
                ]
                raw_used = _as_int(
                    _value(
                        raw_group,
                        "used_preset_id",
                        "UsedPresetsId",
                        default=-1,
                    ),
                    -1,
                )
                used_preset_id: Optional[int] = (
                    raw_used
                    if 0 <= raw_used < PRESET_SLOT_COUNT
                    and slots[raw_used]["uid_list"]
                    else None
                )
                heroes[str(hero_id)] = {
                    "used_preset_id": used_preset_id,
                    "slots": slots,
                }
            by_type[str(preset_type)] = heroes

        profile[PRESETS_V2_KEY] = {
            "schema_version": PRESETS_V2_SCHEMA_VERSION,
            "by_type": by_type,
        }

    def _seed_medals(self, profile: MutableMapping[str, Any]) -> None:
        existing = _row_index(profile.get("medals"), "medal_id", "MedalId", "id")
        medals = []
        for index, medal_id in enumerate(self.medal_ids):
            if profile.get("player_progression") and medal_id not in existing:
                continue
            source = existing.get(medal_id, {})
            medals.append(
                {
                    "medal_id": medal_id,
                    "is_new": _uint(
                        _value(source, "is_new", "IsNew"), 0, UINT32_MAX
                    ),
                    "take_time": _uint(
                        _value(source, "take_time", "TakeTime"),
                        MEDAL_TAKE_TIME_BASE + index,
                    ),
                }
            )
        profile["medals"] = medals
        owned_medals = {int(row["medal_id"]) for row in medals}

        raw_switches = profile.get("medal_passive_switches")
        switches: Dict[str, int] = {}
        if isinstance(raw_switches, Mapping):
            for passive_type in self.medal_passive_types:
                raw = _lookup(raw_switches, passive_type, 1)
                if isinstance(raw, Mapping):
                    raw = _value(raw, "is_open", "open", "IsOpen", default=1)
                switches[str(passive_type)] = 1 if _uint(raw, 1, UINT32_MAX) else 0
        else:
            indexed = _row_index(
                raw_switches,
                "medal_passive_type",
                "MedalPassiveType",
                "id",
            )
            for passive_type in self.medal_passive_types:
                raw = indexed.get(passive_type, {})
                switches[str(passive_type)] = (
                    1
                    if _uint(_value(raw, "is_open", "open", "IsOpen"), 1, UINT32_MAX)
                    else 0
                )
        profile["medal_passive_switches"] = switches

        slots: Dict[int, int] = {slot_id: 0 for slot_id in range(1, MEDAL_SLOT_COUNT + 1)}
        raw_slots = profile.get("medal_slots")
        if isinstance(raw_slots, Mapping):
            for slot_id in slots:
                value = _lookup(raw_slots, slot_id, 0)
                if isinstance(value, Mapping):
                    value = _value(value, "medal_id", "MedalId", default=0)
                medal_id = _uint(value, 0, UINT32_MAX)
                slots[slot_id] = medal_id if medal_id in owned_medals else 0
        else:
            indexed_slots = _row_index(
                raw_slots, "medal_slot_id", "MedalSlotId", "slot_id", "id"
            )
            for slot_id in slots:
                source = indexed_slots.get(slot_id, {})
                medal_id = _uint(
                    _value(source, "medal_id", "MedalId"), 0, UINT32_MAX
                )
                slots[slot_id] = medal_id if medal_id in owned_medals else 0
        profile["medal_slots"] = [
            {"medal_slot_id": slot_id, "medal_id": slots[slot_id]}
            for slot_id in slots
        ]

    def seed_profile(self, profile: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
        """Idempotently materialize all collectible ownership and default state."""

        if not isinstance(profile, MutableMapping):
            raise TypeError("collectible profile must be a mutable mapping")
        seed_version = _as_int(profile.get(COLLECTIBLES_SEED_VERSION_KEY), 0)
        self._seed_runes(
            profile,
            add_missing_catalog_rows=seed_version < COLLECTIBLES_SEED_VERSION and not profile.get("player_progression"),
        )
        self._normalize_rune_equipment(profile)
        self._seed_decorations(profile)
        self.sync_gospel_decoration_levels(profile)
        profile[GOSPEL_DECORATION_LINK_REVISION_KEY] = GOSPEL_DECORATION_LINK_REVISION
        self._seed_appearances(profile)
        self._seed_presets_v2(profile)
        self._seed_medals(profile)
        profile[COLLECTIBLES_SEED_VERSION_KEY] = COLLECTIBLES_SEED_VERSION
        return profile

    @staticmethod
    def needs_seed(profile: Mapping[str, Any]) -> bool:
        if (
            _as_int(profile.get(COLLECTIBLES_SEED_VERSION_KEY), 0)
            < COLLECTIBLES_SEED_VERSION
        ):
            return True
        root = profile.get(PRESETS_V2_KEY)
        if not isinstance(root, Mapping) or _as_int(root.get("schema_version"), 0) != (
            PRESETS_V2_SCHEMA_VERSION
        ):
            return True
        by_type = root.get("by_type")
        if not isinstance(by_type, Mapping):
            return True
        hero_ids, _uid_to_id = CollectiblesLayer._hero_identity_maps(profile)
        if set(by_type) != {
            str(PRESET_TYPE_DECORATION),
            str(PRESET_TYPE_RUNE),
        }:
            return True
        for preset_type in (PRESET_TYPE_DECORATION, PRESET_TYPE_RUNE):
            heroes = _lookup(by_type, preset_type, {})
            if not isinstance(heroes, Mapping):
                return True
            if set(heroes) != {str(hero_id) for hero_id in hero_ids}:
                return True
            for hero_id in hero_ids:
                group = _lookup(heroes, hero_id, {})
                slots = group.get("slots") if isinstance(group, Mapping) else None
                if not isinstance(slots, list) or len(slots) != PRESET_SLOT_COUNT:
                    return True
                if any(
                    not isinstance(slot, Mapping)
                    or _as_int(slot.get("preset_id"), -1) != preset_id
                    for preset_id, slot in enumerate(slots)
                ):
                    return True
                for slot in slots:
                    name = slot.get("name")
                    uid_list = slot.get("uid_list")
                    if (
                        not isinstance(name, str)
                        or not name
                        or len(name) > 64
                        or not isinstance(uid_list, list)
                        or any(_uint(uid) <= 0 for uid in uid_list)
                    ):
                        return True
                    if preset_type == PRESET_TYPE_RUNE:
                        if len(uid_list) != len(set(uid_list)) or slot.get(
                            "decoration_info"
                        ) is not None:
                            return True
                    else:
                        info = slot.get("decoration_info")
                        decorations = (
                            info.get("decorations") if isinstance(info, Mapping) else None
                        )
                        if not isinstance(decorations, list):
                            return True
                        decoration_ids = [
                            _uint(_value(row, "id", "Id"))
                            for row in decorations
                            if isinstance(row, Mapping)
                        ]
                        if (
                            len(decoration_ids) != len(decorations)
                            or decoration_ids != uid_list
                        ):
                            return True
                used = group.get("used_preset_id")
                if used is not None and (
                    not isinstance(used, int)
                    or isinstance(used, bool)
                    or not 0 <= used < PRESET_SLOT_COUNT
                    or not slots[used]["uid_list"]
                ):
                    return True
        return False

    def _seeded_copy(self, profile: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
        result = copy.deepcopy(dict(profile or {}))
        self.seed_profile(result)
        return result

    @staticmethod
    def _rune_message(row: Mapping[str, Any]) -> Dict[str, int]:
        return {
            "Id": _uint(row.get("id"), 0, UINT32_MAX),
            "RuneItemUId": _uint(row.get("rune_item_uid")),
            "HeroUId": _uint(row.get("hero_uid")),
            "BasicAttr": _uint(row.get("basic_attr"), 0, UINT32_MAX),
            "RandAttr1": _uint(row.get("rand_attr_1"), 0, UINT32_MAX),
            "RandAttr2": _uint(row.get("rand_attr_2"), 0, UINT32_MAX),
            "RandAttr3": _uint(row.get("rand_attr_3"), 0, UINT32_MAX),
            "Attr1Quality": _uint(row.get("attr_1_quality"), 0, UINT32_MAX),
            "Attr2Quality": _uint(row.get("attr_2_quality"), 0, UINT32_MAX),
            "Attr3Quality": _uint(row.get("attr_3_quality"), 0, UINT32_MAX),
            "Level": _uint(row.get("level"), 0, UINT32_MAX),
            "CurExp": _uint(row.get("cur_exp"), 0, UINT32_MAX),
            "State": _uint(row.get("state"), 0, UINT32_MAX),
            "CreateTime": _uint(row.get("create_time")),
            "Attr1Value": _uint(row.get("attr_1_value"), 0, UINT32_MAX),
            "Attr2Value": _uint(row.get("attr_2_value"), 0, UINT32_MAX),
            "Attr3Value": _uint(row.get("attr_3_value"), 0, UINT32_MAX),
        }

    @staticmethod
    def _decoration_item_message(row: Mapping[str, Any]) -> Dict[str, int]:
        return {
            "Id": _uint(row.get("id"), 0, UINT32_MAX),
            "BasicAttr": _uint(row.get("basic_attr"), 0, UINT32_MAX),
            "HeroId": _uint(row.get("hero_id"), 0, UINT32_MAX),
            "BasicAttrLvl": _uint(row.get("basic_attr_level"), 0, UINT32_MAX),
            "IsShow": _uint(row.get("is_show"), 0, UINT32_MAX),
        }

    @staticmethod
    def _decoration_message(row: Mapping[str, Any]) -> Dict[str, int]:
        return {
            "Position": _uint(row.get("position"), 0, UINT32_MAX),
            "Id": _uint(row.get("id"), 0, UINT32_MAX),
            "Length": _as_int(row.get("length"), 0),
            "Width": _as_int(row.get("width"), 0),
            "Depth": _as_int(row.get("depth"), 0),
            "Rotatex": _as_int(row.get("rotate_x"), 0),
            "Rotatey": _as_int(row.get("rotate_y"), 0),
            "Rotatez": _as_int(row.get("rotate_z"), 0),
            "Size": _as_int(row.get("size"), 0),
            "IsSelfDefine": _uint(row.get("is_self_define"), 0, UINT32_MAX),
            "ModelId": _uint(row.get("model_id"), 0, UINT32_MAX),
            "IsShow": _uint(row.get("is_show"), 0, UINT32_MAX),
        }

    def _appearance_message(self, row: Mapping[str, Any]) -> Dict[str, Any]:
        decorations = row.get("decorations")
        return {
            "Id": _uint(row.get("id"), 0, UINT32_MAX),
            "DecorationList": [
                self._decoration_message(item)
                for item in decorations
                if isinstance(item, Mapping)
            ]
            if isinstance(decorations, (list, tuple))
            else [],
            "IsUsed": _uint(row.get("is_used"), 0, UINT32_MAX),
            "Name": str(row.get("name") or "")[:64],
        }

    def _hero_appearance_messages(self, profile: Mapping[str, Any]) -> List[Dict[str, Any]]:
        root = profile.get("hero_appearances")
        root = root if isinstance(root, Mapping) else {}
        rows = []
        for hero_uid in self._entity_uids(profile, "heroes") or (1_000_000_001,):
            container = _lookup(root, hero_uid, {})
            appearances = self._appearance_rows(container)
            rows.append(
                {
                    "HeroUId": hero_uid,
                    "AppearanceList": [
                        self._appearance_message(item)
                        for item in appearances
                        if isinstance(item, Mapping)
                    ],
                }
            )
        return rows

    def _encode_decoration_items(self, profile: Mapping[str, Any]) -> bytes:
        return self.codec.encode(
            "SC_DecorationItemListRet",
            {
                "DecorationItemList": [
                    self._decoration_item_message(row)
                    for row in profile["decoration_items"]
                    if isinstance(row, Mapping)
                ]
            },
        )

    def _encode_hero_appearances(self, profile: Mapping[str, Any]) -> bytes:
        return self.codec.encode(
            "SC_HeroAppearanceDatasRet",
            {"HeroAppearanceList": self._hero_appearance_messages(profile)},
        )

    def _encode_medals(self, profile: Mapping[str, Any]) -> bytes:
        switches = profile.get("medal_passive_switches")
        switches = switches if isinstance(switches, Mapping) else {}
        return self.codec.encode(
            "SC_MedalDatasRet",
            {
                "MedalList": [
                    {
                        "MedalId": _uint(row.get("medal_id"), 0, UINT32_MAX),
                        "IsNew": _uint(row.get("is_new"), 0, UINT32_MAX),
                        "TakeTime": _uint(row.get("take_time")),
                    }
                    for row in profile["medals"]
                    if isinstance(row, Mapping)
                ],
                "MedalAttrSwitchList": [
                    {
                        "MedalPassiveType": passive_type,
                        "IsOpen": 1 if _uint(_lookup(switches, passive_type, 1)) else 0,
                    }
                    for passive_type in self.medal_passive_types
                ],
            },
        )

    def _encode_medal_slots(self, profile: Mapping[str, Any]) -> bytes:
        return self.codec.encode(
            "SC_MedalSlotDatasRet",
            {
                "MedalSlotList": [
                    {
                        "MedalSlotId": _uint(
                            row.get("medal_slot_id"), 0, UINT32_MAX
                        ),
                        "MedalId": _uint(row.get("medal_id"), 0, UINT32_MAX),
                    }
                    for row in profile["medal_slots"]
                    if isinstance(row, Mapping)
                ]
            },
        )

    def _hero_rune_messages(
        self, profile: Mapping[str, Any]
    ) -> List[Dict[str, Any]]:
        hero_runes = profile.get("hero_runes")
        hero_rune_messages = []
        if isinstance(hero_runes, (list, tuple)):
            for hero_rune in hero_runes:
                if not isinstance(hero_rune, Mapping):
                    continue
                slots = hero_rune.get("slots", hero_rune.get("hero_rune_slots", ()))
                hero_rune_messages.append(
                    {
                        "HeroUId": _uint(
                            _value(hero_rune, "hero_uid", "HeroUId")
                        ),
                        "HeroRuneSlotList": [
                            {
                                "Position": _uint(
                                    _value(slot, "position", "Position"),
                                    0,
                                    UINT32_MAX,
                                ),
                                "RuneItemUId": _uint(
                                    _value(slot, "rune_item_uid", "RuneItemUId")
                                ),
                            }
                            for slot in slots
                            if isinstance(slot, Mapping)
                        ]
                        if isinstance(slots, (list, tuple))
                        else [],
                    }
                )
        return hero_rune_messages

    def _encode_runes(self, profile: Mapping[str, Any]) -> bytes:
        return self.codec.encode(
            "SC_RuneDatas",
            {
                "RuneItemList": [
                    self._rune_message(row)
                    for row in profile["rune_items"]
                    if isinstance(row, Mapping)
                ],
                "HeroRuneList": self._hero_rune_messages(profile),
            },
        )

    def make_decoration_item_list_ret(
        self, profile: Optional[Mapping[str, Any]] = None
    ) -> bytes:
        return self._encode_decoration_items(self._seeded_copy(profile))

    def make_hero_appearance_datas_ret(
        self, profile: Optional[Mapping[str, Any]] = None
    ) -> bytes:
        return self._encode_hero_appearances(self._seeded_copy(profile))

    def make_medal_datas_ret(
        self, profile: Optional[Mapping[str, Any]] = None
    ) -> bytes:
        return self._encode_medals(self._seeded_copy(profile))

    def make_medal_slot_datas_ret(
        self, profile: Optional[Mapping[str, Any]] = None
    ) -> bytes:
        return self._encode_medal_slots(self._seeded_copy(profile))

    def make_rune_datas(
        self, profile: Optional[Mapping[str, Any]] = None
    ) -> bytes:
        return self._encode_runes(self._seeded_copy(profile))

    def make_update_rune(self, row: Mapping[str, Any]) -> bytes:
        """Encode the authoritative notification for one persisted rune item."""

        return self.codec.encode(
            "Notify_UpdateRune",
            {"RuneItem": self._rune_message(row)},
        )

    def make_update_decoration(self, row: Mapping[str, Any]) -> bytes:
        return self.codec.encode("Notify_UpdateDecorationItem", {"DecoItem": self._decoration_item_message(row)})

    def make_update_hero_runes(self, profile: Mapping[str, Any]) -> bytes:
        """Encode command 1088 for runtimes which register the newer handler."""

        seeded = self._seeded_copy(profile)
        return self.codec.encode(
            "Notify_UpdateHeroRune",
            {"HeroRuneList": self._hero_rune_messages(seeded)},
        )

    def _rune_notifications(
        self,
        before: Sequence[Mapping[str, Any]],
        after: Sequence[Mapping[str, Any]],
    ) -> Tuple[Tuple[int, bytes], ...]:
        before_by_uid = {
            _uint(row.get("rune_item_uid")): row
            for row in before
            if _uint(row.get("rune_item_uid")) > 0
        }
        packets = []
        for row in after:
            uid = _uint(row.get("rune_item_uid"))
            if uid > 0 and before_by_uid.get(uid) != row:
                packets.append((NOTIFY_UPDATE_RUNE, self.make_update_rune(row)))
        return tuple(packets)

    def _preset_slot_message(
        self, preset_type: int, slot: Mapping[str, Any]
    ) -> Dict[str, Any]:
        values: Dict[str, Any] = {
            "PresetsId": _uint(slot.get("preset_id"), 0, UINT32_MAX),
            "PresetName": str(slot.get("name") or "")[:64],
            "UidList": [
                _uint(uid)
                for uid in slot.get("uid_list", ())
                if _uint(uid) > 0
            ]
            if isinstance(slot.get("uid_list"), (list, tuple))
            else [],
        }
        if preset_type == PRESET_TYPE_DECORATION:
            info = slot.get("decoration_info")
            decorations = info.get("decorations", ()) if isinstance(info, Mapping) else ()
            values["PresetDecorationInfo"] = {
                "DecorationList": [
                    self._decoration_message(row)
                    for row in decorations
                    if isinstance(row, Mapping)
                ]
                if isinstance(decorations, (list, tuple))
                else []
            }
        return values

    def _preset_info_message(
        self, profile: Mapping[str, Any], preset_type: int, hero_id: int
    ) -> Dict[str, Any]:
        root = profile.get(PRESETS_V2_KEY)
        root = root if isinstance(root, Mapping) else {}
        by_type = root.get("by_type")
        by_type = by_type if isinstance(by_type, Mapping) else {}
        heroes = _lookup(by_type, preset_type, {})
        heroes = heroes if isinstance(heroes, Mapping) else {}
        group = _lookup(heroes, hero_id, {})
        group = group if isinstance(group, Mapping) else {}
        slots = group.get("slots", ())
        slots = slots if isinstance(slots, (list, tuple)) else ()
        used = _as_int(group.get("used_preset_id"), -1)
        return {
            "PresetsType": preset_type,
            "HeroId": hero_id,
            # The defined message is proto3 uint32 and has no explicit
            # "none" value.  Empty UidList distinguishes a fresh slot zero.
            "UsedPresetsId": used if 0 <= used < PRESET_SLOT_COUNT else 0,
            "PresetsList": [
                self._preset_slot_message(preset_type, slot)
                for slot in slots
                if isinstance(slot, Mapping)
            ],
        }

    def _preset_info_messages(self, profile: Mapping[str, Any]) -> List[Dict[str, Any]]:
        hero_ids, _uid_to_id = self._hero_identity_maps(profile)
        return [
            self._preset_info_message(profile, preset_type, hero_id)
            for hero_id in sorted(hero_ids)
            for preset_type in (PRESET_TYPE_DECORATION, PRESET_TYPE_RUNE)
        ]

    def initial_snapshots(
        self, profile: Optional[Mapping[str, Any]] = None
    ) -> Tuple[Tuple[int, bytes], ...]:
        """Return only the unsolicited rune snapshot used at role bootstrap.

        Decoration, appearance, medal, and slot lists are query-driven.  The
        runtime requests them immediately after role selection; sending them
        here as well recreates notice roots and raises duplicate-red-dot
        exceptions in the Unity runtime.
        """

        seeded = self._seeded_copy(profile)
        return ((SC_RUNE_DATAS, self._encode_runes(seeded)),)

    def _error(
        self,
        response_id: int,
        message_name: str,
        reason: str,
        *,
        error_code: int = ERROR_MESSAGE_DATA_ILLEGAL,
        **values: Any,
    ) -> CollectiblesResult:
        payload = {"Errorcode": error_code}
        payload.update(values)
        return CollectiblesResult(
            response_id=response_id,
            body=self.codec.encode(message_name, payload),
            error_code=error_code,
            reason=reason,
        )

    def _request_presets(self, profile: Mapping[str, Any]) -> CollectiblesResult:
        seeded = self._seeded_copy(profile)
        presets = copy.deepcopy(seeded[PRESETS_V2_KEY])
        return CollectiblesResult(
            SC_REQUEST_PRESETS_RET,
            self.codec.encode(
                "SC_RequestPresetsRet",
                {"PresetInfoList": self._preset_info_messages(seeded)},
            ),
            # Persist a missing/outdated root even when the first interaction
            # is the query issued by MainView.Awake.
            changes={PRESETS_V2_KEY: presets},
        )

    @staticmethod
    def _preset_selector(body: bytes) -> Tuple[int, int, int, Dict[int, List[WireField]]]:
        fields = _wire_fields(body)
        return (
            _last_varint(fields, 1),
            _last_varint(fields, 2),
            _last_varint(fields, 3),
            fields,
        )

    def _current_appearance_decorations(
        self, profile: Mapping[str, Any], hero_id: int
    ) -> Tuple[int, List[Dict[str, Any]]]:
        hero_uid = self._hero_id_to_uid_map(profile).get(hero_id, 0)
        if hero_uid <= 0:
            raise CollectiblesRequestError(f"unknown witch ID {hero_id}")
        root = profile.get("hero_appearances")
        root = root if isinstance(root, Mapping) else {}
        container = _lookup(root, hero_uid, {})
        if not isinstance(container, Mapping):
            raise CollectiblesRequestError(
                f"witch {hero_id} has no appearance state"
            )
        current_id = _uint(container.get("current_id"), 0, UINT32_MAX)
        current = next(
            (
                row
                for row in self._appearance_rows(container)
                if isinstance(row, Mapping)
                and _uint(row.get("id"), 0, UINT32_MAX) == current_id
            ),
            None,
        )
        if current is None:
            raise CollectiblesRequestError(
                f"witch {hero_id} has no current appearance"
            )
        raw_decorations = current.get("decorations", ())
        decorations: List[Dict[str, Any]] = []
        if isinstance(raw_decorations, (list, tuple)):
            for raw in raw_decorations:
                if not isinstance(raw, Mapping):
                    continue
                normalized = self._normalize_decoration(raw)
                if normalized is not None:
                    decorations.append(normalized)
        return hero_uid, decorations

    def _save_presets(
        self, profile: Mapping[str, Any], body: bytes
    ) -> CollectiblesResult:
        preset_type = preset_id = hero_id = 0
        try:
            preset_type, preset_id, hero_id, fields = self._preset_selector(body)
            name = _last_string(fields, 4, "")[:64]
            if preset_type not in {PRESET_TYPE_DECORATION, PRESET_TYPE_RUNE}:
                raise CollectiblesRequestError(
                    f"unsupported PresetsType {preset_type}"
                )
            if not 0 <= preset_id < PRESET_SLOT_COUNT:
                raise CollectiblesRequestError(f"invalid preset slot {preset_id}")

            seeded = self._seeded_copy(profile)
            hero_ids, _uid_to_id = self._hero_identity_maps(seeded)
            if hero_id not in hero_ids:
                raise CollectiblesRequestError(f"unknown witch ID {hero_id}")
            presets = copy.deepcopy(seeded[PRESETS_V2_KEY])
            group = presets["by_type"][str(preset_type)][str(hero_id)]
            slot = group["slots"][preset_id]

            if preset_type == PRESET_TYPE_RUNE:
                equipped = [
                    row
                    for row in seeded["rune_items"]
                    if isinstance(row, Mapping)
                    and _uint(row.get("hero_uid")) == hero_id
                    and self._rune_position(
                        _uint(row.get("id"), 0, UINT32_MAX)
                    )
                    in {1, 2, 3, 4, 5}
                ]
                equipped.sort(
                    key=lambda row: (
                        self._rune_position(
                            _uint(row.get("id"), 0, UINT32_MAX)
                        ),
                        _uint(row.get("rune_item_uid")),
                    )
                )
                if not equipped:
                    return self._error(
                        SC_SAVE_PRESETS_RET,
                        "SC_SavePresetsRet",
                        f"witch {hero_id} has no equipped rune",
                        error_code=ERROR_PRESET_RUNE_EMPTY,
                    )
                slot["uid_list"] = [
                    _uint(row.get("rune_item_uid")) for row in equipped
                ]
                slot["decoration_info"] = None
            else:
                _hero_uid, decorations = self._current_appearance_decorations(
                    seeded, hero_id
                )
                if not decorations:
                    return self._error(
                        SC_SAVE_PRESETS_RET,
                        "SC_SavePresetsRet",
                        f"witch {hero_id} has no equipped decoration",
                        error_code=ERROR_PRESET_DECORATION_EMPTY,
                    )
                slot["uid_list"] = [row["id"] for row in decorations]
                slot["decoration_info"] = {
                    "decorations": copy.deepcopy(decorations)
                }

            if name:
                slot["name"] = name
            seeded[PRESETS_V2_KEY] = presets
            preset_info = self._preset_info_message(
                seeded, preset_type, hero_id
            )
        except CollectiblesRequestError as exc:
            return self._error(
                SC_SAVE_PRESETS_RET,
                "SC_SavePresetsRet",
                str(exc),
            )

        return CollectiblesResult(
            SC_SAVE_PRESETS_RET,
            self.codec.encode(
                "SC_SavePresetsRet",
                {"Errorcode": ERROR_SUCCESS, "PresetInfo": preset_info},
            ),
            changes={PRESETS_V2_KEY: presets},
        )

    def _apply_decoration_preset(
        self,
        profile: Mapping[str, Any],
        hero_id: int,
        slot: Mapping[str, Any],
    ) -> Dict[str, Any]:
        hero_uid, _current = self._current_appearance_decorations(profile, hero_id)
        info = slot.get("decoration_info")
        raw_decorations = info.get("decorations", ()) if isinstance(info, Mapping) else ()
        decorations: List[Dict[str, Any]] = []
        if isinstance(raw_decorations, (list, tuple)):
            for raw in raw_decorations:
                if not isinstance(raw, Mapping):
                    continue
                normalized = self._normalize_decoration(raw)
                if normalized is not None:
                    decorations.append(normalized)
        uid_list = slot.get("uid_list", ())
        expected_ids = [row["id"] for row in decorations]
        if (
            not decorations
            or not isinstance(uid_list, (list, tuple))
            or [_uint(value) for value in uid_list] != expected_ids
        ):
            raise CollectiblesRequestError("decoration preset is empty or inconsistent")

        root = copy.deepcopy(dict(profile["hero_appearances"]))
        container = copy.deepcopy(dict(_lookup(root, hero_uid, {})))
        current_id = _uint(container.get("current_id"), 0, UINT32_MAX)
        appearances = [
            dict(row)
            for row in self._appearance_rows(container)
            if isinstance(row, Mapping)
        ]
        found = False
        for appearance in appearances:
            if _uint(appearance.get("id"), 0, UINT32_MAX) == current_id:
                appearance["decorations"] = copy.deepcopy(decorations)
                found = True
                break
        if not found:
            raise CollectiblesRequestError(
                f"witch {hero_id} has no current appearance"
            )
        container["appearances"] = [
            self._normalize_appearance(row, current_id) for row in appearances
        ]
        root[str(hero_uid)] = container
        return root

    def _apply_rune_preset(
        self,
        profile: MutableMapping[str, Any],
        hero_id: int,
        slot: Mapping[str, Any],
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        raw_uids = slot.get("uid_list", ())
        if not isinstance(raw_uids, (list, tuple)) or not raw_uids:
            raise CollectiblesRequestError("rune preset is empty")
        rune_uids = [_uint(value) for value in raw_uids]
        if any(uid <= 0 for uid in rune_uids) or len(rune_uids) != len(set(rune_uids)):
            raise CollectiblesRequestError("rune preset contains invalid UIDs")

        before = copy.deepcopy(profile["rune_items"])
        runes = copy.deepcopy(profile["rune_items"])
        by_uid = {
            _uint(row.get("rune_item_uid")): row
            for row in runes
            if isinstance(row, MutableMapping)
            and _uint(row.get("rune_item_uid")) > 0
        }
        selected: List[Tuple[int, MutableMapping[str, Any]]] = []
        positions = set()
        for rune_uid in rune_uids:
            rune = by_uid.get(rune_uid)
            if rune is None:
                raise CollectiblesRequestError(
                    f"rune preset references unknown UID {rune_uid}"
                )
            position = self._rune_position(
                _uint(rune.get("id"), 0, UINT32_MAX)
            )
            if position not in {1, 2, 3, 4, 5}:
                raise CollectiblesRequestError(
                    f"rune UID {rune_uid} has no valid position"
                )
            if position in positions:
                raise CollectiblesRequestError(
                    f"rune preset repeats position {position}"
                )
            positions.add(position)
            selected.append((position, rune))

        # Validation is complete.  From this point the mutation happens only
        # on the local copy, so moving a rune from another witch and clearing
        # the target loadout commit as one profile patch.
        for rune in runes:
            if isinstance(rune, MutableMapping) and _uint(rune.get("hero_uid")) == hero_id:
                rune["hero_uid"] = 0
        for _position, rune in selected:
            rune["hero_uid"] = hero_id
        profile["rune_items"] = runes
        profile["hero_runes"] = []
        self._normalize_rune_equipment(profile)
        return before, profile["rune_items"]

    def _use_presets(
        self, profile: Mapping[str, Any], body: bytes
    ) -> CollectiblesResult:
        preset_type = preset_id = hero_id = 0
        try:
            preset_type, preset_id, hero_id, _fields = self._preset_selector(body)
            if preset_type not in {PRESET_TYPE_DECORATION, PRESET_TYPE_RUNE}:
                raise CollectiblesRequestError(
                    f"unsupported PresetsType {preset_type}"
                )
            if not 0 <= preset_id < PRESET_SLOT_COUNT:
                raise CollectiblesRequestError(f"invalid preset slot {preset_id}")

            seeded = self._seeded_copy(profile)
            hero_ids, _uid_to_id = self._hero_identity_maps(seeded)
            if hero_id not in hero_ids:
                raise CollectiblesRequestError(f"unknown witch ID {hero_id}")
            presets = copy.deepcopy(seeded[PRESETS_V2_KEY])
            group = presets["by_type"][str(preset_type)][str(hero_id)]
            slot = group["slots"][preset_id]
            if not slot.get("uid_list"):
                raise CollectiblesRequestError(f"preset slot {preset_id} is empty")

            changes: Dict[str, Any] = {PRESETS_V2_KEY: presets}
            notifications: Tuple[Tuple[int, bytes], ...] = ()
            if preset_type == PRESET_TYPE_RUNE:
                before_runes, runes = self._apply_rune_preset(
                    seeded, hero_id, slot
                )
                changes.update(
                    {
                        "rune_items": runes,
                        "hero_runes": seeded["hero_runes"],
                        COLLECTIBLES_SEED_VERSION_KEY: COLLECTIBLES_SEED_VERSION,
                    }
                )
                # ProtocolHandler sends ``notifications`` before the response;
                # the Managed 1335 callback therefore observes authoritative
                # RuneItem ownership instead of stale equipment state.
                notifications = self._rune_notifications(before_runes, runes)
            else:
                changes["hero_appearances"] = self._apply_decoration_preset(
                    seeded, hero_id, slot
                )

            group["used_preset_id"] = preset_id
        except CollectiblesRequestError as exc:
            return self._error(
                SC_USE_PRESETS_RET,
                "SC_UsePresetsRet",
                str(exc),
                PresetsType=preset_type,
                PresetsId=preset_id,
                HeroId=hero_id,
            )

        return CollectiblesResult(
            SC_USE_PRESETS_RET,
            self.codec.encode(
                "SC_UsePresetsRet",
                {
                    "Errorcode": ERROR_SUCCESS,
                    "PresetsType": preset_type,
                    "PresetsId": preset_id,
                    "HeroId": hero_id,
                },
            ),
            changes=changes,
            notifications=notifications,
        )

    def _valid_hero_uids(self, profile: Mapping[str, Any]) -> frozenset:
        return frozenset(self._entity_uids(profile, "heroes") or (1_000_000_001,))

    def _decode_decoration(self, raw: bytes) -> Optional[Dict[str, Any]]:
        fields = _wire_fields(raw)
        decoration_id = _last_varint(fields, 2)
        if decoration_id == 0:
            # Source WitchLooksView.DecoTry(false) retains the slot and
            # transforms but clears Id/ModelId/IsShow (omitted by proto3).
            # Store no item for that slot; malformed zero IDs still fail closed.
            if (_last_varint(fields, 1) not in {1, 2, 3, 4, 5}
                    or _last_varint(fields, 11) != 0
                    or _last_varint(fields, 12) != 0):
                raise CollectiblesRequestError("invalid empty decoration slot")
            return None
        if decoration_id not in self.decoration_id_set:
            raise CollectiblesRequestError(f"unknown decoration ID {decoration_id}")
        return {
            "position": _last_varint(fields, 1),
            "id": decoration_id,
            "length": _signed_int32(_last_varint(fields, 3)),
            "width": _signed_int32(_last_varint(fields, 4)),
            "depth": _signed_int32(_last_varint(fields, 5)),
            "rotate_x": _signed_int32(_last_varint(fields, 6)),
            "rotate_y": _signed_int32(_last_varint(fields, 7)),
            "rotate_z": _signed_int32(_last_varint(fields, 8)),
            "size": _signed_int32(_last_varint(fields, 9)),
            "is_self_define": _last_varint(fields, 10),
            "model_id": _last_varint(fields, 11),
            "is_show": _last_varint(fields, 12, 1),
        }

    def _decode_appearance(self, raw: bytes) -> Dict[str, Any]:
        fields = _wire_fields(raw)
        decorations = [self._decode_decoration(item) for item in _messages(fields, 2)]
        return {
            "id": _last_varint(fields, 1),
            "decorations": [
                item for item in decorations if item is not None
            ],
            "is_used": 1 if _last_varint(fields, 3) else 0,
            "name": _last_string(fields, 4, "")[:64],
        }

    def _save_hero_appearance(
        self, profile: Mapping[str, Any], body: bytes
    ) -> CollectiblesResult:
        try:
            fields = _wire_fields(body)
            hero_uid = _last_varint(fields, 1)
            raw_appearance = _last_message(fields, 2)
            skin_uid = _last_varint(fields, 3)
            setting_group_id = _last_varint(fields, 4)
            if hero_uid not in self._valid_hero_uids(profile):
                raise CollectiblesRequestError(f"unknown hero UID {hero_uid}")
            if raw_appearance is None:
                raise CollectiblesRequestError("CS_SaveHeroAppearance has no Appear")
            appearance = self._decode_appearance(raw_appearance)
        except CollectiblesRequestError as exc:
            return self._error(
                SC_SAVE_HERO_APPEARANCE_RET,
                "SC_SaveHeroAppearanceRet",
                str(exc),
                Skinuid=locals().get("skin_uid", 0),
                HeroAppearanceList=[],
                SettingGroupId=locals().get("setting_group_id", 0),
            )

        seeded = self._seeded_copy(profile)
        root = copy.deepcopy(dict(seeded["hero_appearances"]))
        container = copy.deepcopy(dict(_lookup(root, hero_uid, {})))
        indexed = {
            _uint(item.get("id"), 0, UINT32_MAX): dict(item)
            for item in self._appearance_rows(container)
            if isinstance(item, Mapping)
        }
        indexed[appearance["id"]] = appearance
        current_id = _uint(container.get("current_id"), 0, UINT32_MAX)
        if appearance["is_used"]:
            current_id = appearance["id"]
        normalized = [
            self._normalize_appearance(indexed[key], current_id)
            for key in sorted(indexed)
        ]
        root[str(hero_uid)] = {
            "current_id": current_id,
            "appearances": normalized,
        }
        changed = dict(seeded)
        changed["hero_appearances"] = root
        response_values = {
            "Errorcode": ERROR_SUCCESS,
            "Skinuid": skin_uid,
            "HeroAppearanceList": self._hero_appearance_messages(changed),
            "SettingGroupId": setting_group_id,
        }
        return CollectiblesResult(
            SC_SAVE_HERO_APPEARANCE_RET,
            self.codec.encode("SC_SaveHeroAppearanceRet", response_values),
            changes={"hero_appearances": root},
        )

    def _set_current_appearance(
        self, profile: Mapping[str, Any], body: bytes
    ) -> CollectiblesResult:
        try:
            fields = _wire_fields(body)
            hero_uid = _last_varint(fields, 1)
            appearance_id = _last_varint(fields, 2)
            if hero_uid not in self._valid_hero_uids(profile):
                raise CollectiblesRequestError(f"unknown hero UID {hero_uid}")
            seeded = self._seeded_copy(profile)
            root = copy.deepcopy(dict(seeded["hero_appearances"]))
            container = copy.deepcopy(dict(_lookup(root, hero_uid, {})))
            appearances = [
                dict(item)
                for item in self._appearance_rows(container)
                if isinstance(item, Mapping)
            ]
            if appearance_id not in {
                _uint(item.get("id"), 0, UINT32_MAX) for item in appearances
            }:
                raise CollectiblesRequestError(
                    f"unknown appearance ID {appearance_id} for hero {hero_uid}"
                )
        except CollectiblesRequestError as exc:
            return self._error(
                SC_SET_HERO_CUR_APPEARANCE_ID_RET,
                "SC_SetHeroCurAppearanceIdRet",
                str(exc),
                HeroUId=locals().get("hero_uid", 0),
                AppearanceId=locals().get("appearance_id", 0),
            )

        container["current_id"] = appearance_id
        container["appearances"] = [
            self._normalize_appearance(item, appearance_id) for item in appearances
        ]
        root[str(hero_uid)] = container
        values = {
            "Errorcode": ERROR_SUCCESS,
            "HeroUId": hero_uid,
            "AppearanceId": appearance_id,
        }
        return CollectiblesResult(
            SC_SET_HERO_CUR_APPEARANCE_ID_RET,
            self.codec.encode("SC_SetHeroCurAppearanceIdRet", values),
            changes={"hero_appearances": root},
        )

    def _view_medal(
        self, profile: Mapping[str, Any], body: bytes
    ) -> CollectiblesResult:
        try:
            medal_id = _last_varint(_wire_fields(body), 2)
            if medal_id not in self.medal_id_set:
                raise CollectiblesRequestError(f"unknown medal ID {medal_id}")
        except CollectiblesRequestError as exc:
            return self._error(
                SC_VIEW_MEDAL_RET,
                "SC_ViewMedalRet",
                str(exc),
                MedalId=locals().get("medal_id", 0),
            )
        seeded = self._seeded_copy(profile)
        medals = copy.deepcopy(seeded["medals"])
        for row in medals:
            if row["medal_id"] == medal_id:
                row["is_new"] = 0
                break
        return CollectiblesResult(
            SC_VIEW_MEDAL_RET,
            self.codec.encode(
                "SC_ViewMedalRet", {"Errorcode": ERROR_SUCCESS, "MedalId": medal_id}
            ),
            changes={"medals": medals},
        )

    @staticmethod
    def _slot_map(profile: Mapping[str, Any]) -> Dict[int, int]:
        result = {slot_id: 0 for slot_id in range(1, MEDAL_SLOT_COUNT + 1)}
        rows = profile.get("medal_slots")
        if isinstance(rows, (list, tuple)):
            for row in rows:
                if not isinstance(row, Mapping):
                    continue
                slot_id = _uint(row.get("medal_slot_id"), 0, UINT32_MAX)
                if slot_id in result:
                    result[slot_id] = _uint(row.get("medal_id"), 0, UINT32_MAX)
        return result

    def _decode_medal_map(self, fields: Mapping[int, Sequence[WireField]]) -> Dict[int, int]:
        result: Dict[int, int] = {}
        for raw in _messages(fields, 1):
            entry = _wire_fields(raw)
            slot_id = _last_varint(entry, 1)
            medal_id = _last_varint(entry, 2)
            if not 1 <= slot_id <= MEDAL_SLOT_COUNT:
                raise CollectiblesRequestError(f"unknown medal slot ID {slot_id}")
            if medal_id and medal_id not in self.medal_id_set:
                raise CollectiblesRequestError(f"unknown medal ID {medal_id}")
            result[slot_id] = medal_id
        return result

    def _set_medal_slots(
        self, profile: Mapping[str, Any], body: bytes
    ) -> CollectiblesResult:
        try:
            fields = _wire_fields(body)
            requested = self._decode_medal_map(fields)
            remove_all = bool(_last_varint(fields, 2))
        except CollectiblesRequestError as exc:
            return self._error(
                SC_SET_MEDAL_SLOT_RET,
                "SC_SetMedalSlotRet",
                str(exc),
                MedalMap={},
                RemoveAll=False,
            )
        seeded = self._seeded_copy(profile)
        slots = self._slot_map(seeded)
        owned = {int(row["medal_id"]) for row in seeded["medals"]}
        if not remove_all and any(mid and mid not in owned for mid in requested.values()):
            return self._error(SC_SET_MEDAL_SLOT_RET,"SC_SetMedalSlotRet",
                "Medal is not owned",MedalMap={},RemoveAll=False)
        if remove_all:
            slots = {slot_id: 0 for slot_id in slots}
        else:
            slots.update(requested)
        slot_rows = [
            {"medal_slot_id": slot_id, "medal_id": slots[slot_id]}
            for slot_id in sorted(slots)
        ]
        return CollectiblesResult(
            SC_SET_MEDAL_SLOT_RET,
            self.codec.encode(
                "SC_SetMedalSlotRet",
                {
                    "Errorcode": ERROR_SUCCESS,
                    "MedalMap": slots,
                    "RemoveAll": remove_all,
                },
            ),
            changes={"medal_slots": slot_rows},
        )

    def _set_medal_state(
        self, profile: Mapping[str, Any], body: bytes
    ) -> CollectiblesResult:
        try:
            fields = _wire_fields(body)
            passive_type = _last_varint(fields, 1)
            opened = 1 if _last_varint(fields, 2) else 0
            if passive_type not in self.medal_passive_type_set:
                raise CollectiblesRequestError(
                    f"unknown medal passive type {passive_type}"
                )
        except CollectiblesRequestError as exc:
            return self._error(
                SC_SET_MEDAL_STATE_RET,
                "SC_SetMedalStateRet",
                str(exc),
                MedalPassiveType=locals().get("passive_type", 0),
                Open=locals().get("opened", 0),
            )
        seeded = self._seeded_copy(profile)
        switches = copy.deepcopy(dict(seeded["medal_passive_switches"]))
        switches[str(passive_type)] = opened
        return CollectiblesResult(
            SC_SET_MEDAL_STATE_RET,
            self.codec.encode(
                "SC_SetMedalStateRet",
                {
                    "Errorcode": ERROR_SUCCESS,
                    "MedalPassiveType": passive_type,
                    "Open": opened,
                },
            ),
            changes={"medal_passive_switches": switches},
        )

    def _set_all_medal_states(
        self, profile: Mapping[str, Any], body: bytes
    ) -> CollectiblesResult:
        try:
            opened = 1 if _last_varint(_wire_fields(body), 1) else 0
        except CollectiblesRequestError as exc:
            return self._error(
                SC_SET_ALL_MEDAL_STATE_RET,
                "SC_SetAllMedalStateRet",
                str(exc),
                Open=0,
            )
        switches = {str(passive_type): opened for passive_type in self.medal_passive_types}
        return CollectiblesResult(
            SC_SET_ALL_MEDAL_STATE_RET,
            self.codec.encode(
                "SC_SetAllMedalStateRet",
                {"Errorcode": ERROR_SUCCESS, "Open": opened},
            ),
            changes={"medal_passive_switches": switches},
        )

    def _take_medal(
        self, profile: Mapping[str, Any], body: bytes
    ) -> CollectiblesResult:
        try:
            medal_id = _last_varint(_wire_fields(body), 1)
            if medal_id not in self.medal_id_set:
                raise CollectiblesRequestError(f"unknown medal ID {medal_id}")
        except CollectiblesRequestError as exc:
            return self._error(
                SC_TAKE_MEDAL_RET,
                "SC_TakeMedalRet",
                str(exc),
                MedalId=locals().get("medal_id", 0),
                IsNew=0,
                RewardList=[],
                TakeTime=0,
            )
        seeded = self._seeded_copy(profile)
        medals = copy.deepcopy(seeded["medals"])
        medal = next((row for row in medals if row["medal_id"] == medal_id),None)
        if medal is None:
            return self._error(SC_TAKE_MEDAL_RET,"SC_TakeMedalRet",
                "Medal prerequisites have not been earned",MedalId=medal_id,IsNew=0,RewardList=[],TakeTime=0)
        medal["is_new"] = 0
        values = {
            "Errorcode": ERROR_SUCCESS,
            "MedalId": medal_id,
            "IsNew": 0,
            "RewardList": [],
            "TakeTime": medal["take_time"],
        }
        return CollectiblesResult(
            SC_TAKE_MEDAL_RET,
            self.codec.encode("SC_TakeMedalRet", values),
            changes={"medals": medals},
        )

    @staticmethod
    def _history(profile: Mapping[str, Any], key: str) -> List[Dict[str, Any]]:
        value = profile.get(key, [])
        if not isinstance(value, list):
            raise CollectiblesRequestError(f"profile.{key} must be a list")
        return copy.deepcopy(value)

    @staticmethod
    def _rune_by_uid(
        runes: Sequence[Mapping[str, Any]], rune_uid: int
    ) -> Optional[MutableMapping[str, Any]]:
        return next(
            (
                row
                for row in runes
                if isinstance(row, MutableMapping)
                and _uint(row.get("rune_item_uid")) == rune_uid
            ),
            None,
        )

    def _rune_quality(self, rune: Mapping[str, Any]) -> int:
        static = self.rune_static.get(_uint(rune.get("id"), 0, UINT32_MAX))
        return max(1, min(3, _uint(static.get("quality"), 1, 3))) if static else 0

    def _rune_growth_row(
        self, rune: Mapping[str, Any]
    ) -> Optional[Mapping[str, Any]]:
        return self.rune_growth.get(
            (self._rune_quality(rune), _uint(rune.get("level"), 0, UINT32_MAX))
        )

    @staticmethod
    def _decode_consumed_items(
        fields: Mapping[int, Sequence[WireField]], number: int
    ) -> Tuple[Dict[str, int], ...]:
        rows = []
        seen = set()
        for raw in _messages(fields, number):
            item = _wire_fields(raw)
            uid = _last_varint(item, 1)
            count = _last_varint(item, 2)
            if uid <= 0 or count <= 0:
                raise CollectiblesRequestError(
                    "ConsumeMaterial requires positive ItemUId and ItemNum"
                )
            if uid in seen:
                raise CollectiblesRequestError(
                    f"ConsumeMaterial UID {uid} is duplicated"
                )
            seen.add(uid)
            rows.append({"item_uid": uid, "count": count})
        return tuple(rows)

    @staticmethod
    def _recast_token(fields: Mapping[int, Sequence[WireField]], number: int) -> str:
        values = fields.get(number, ())
        if not values:
            return ""
        if (len(values) != 1 or values[0][0] != 2
                or not isinstance(values[0][1], bytes)
                or re.fullmatch(rb"[0-9a-f]{32}", values[0][1]) is None):
            raise CollectiblesRequestError(f"invalid rune operation identity {number}")
        return values[0][1].decode("ascii")

    @staticmethod
    def _pending_rune_uid(profile: Mapping[str, Any]) -> int:
        state = profile.get(RUNE_RECAST_KEY, {})
        pending = state.get("pending") if isinstance(state, Mapping) else None
        return _uint(pending.get("target_uid")) if isinstance(pending, Mapping) else 0

    @staticmethod
    def _validate_recast_pending(pending: Any, *, candidate_required: bool = True) -> None:
        if not isinstance(pending, dict):
            raise CollectiblesRequestError("saved rune candidate must be a mapping")
        if (type(pending.get("target_uid")) is not int
                or not 0 < pending["target_uid"] <= UINT64_MAX
                or not isinstance(pending.get("candidate_id"), str)
                or re.fullmatch(r"[0-9a-f]{32}", pending["candidate_id"]) is None):
            raise CollectiblesRequestError("saved rune candidate identity is invalid")
        if not candidate_required:
            return  # A bound discard can safely clear a damaged candidate.
        request = pending.get("request")
        if pending.get("policy_version") != POLICY_VERSION or not isinstance(request, dict):
            raise CollectiblesRequestError("saved rune candidate policy/request is invalid")
        locks, consumed = request.get("locked_indexes"), request.get("consumed_rune_uids")
        if (request.get("target_uid") != pending["target_uid"] or not isinstance(locks, list)
                or len(locks) > 2 or any(type(i) is not int or i not in (1, 2, 3) for i in locks)
                or len(set(locks)) != len(locks) or not isinstance(consumed, list)
                or any(type(i) is not int or not 0 < i <= UINT64_MAX for i in consumed)
                or len(set(consumed)) != len(consumed)):
            raise CollectiblesRequestError("saved rune candidate request is malformed")
        for name in ("candidate", "base_attrs"):
            values = pending.get(name)
            if not isinstance(values, dict) or any(
                type(values.get(key)) is not int or not 0 <= values[key] <= UINT32_MAX
                for key in RUNE_RECAST_ATTR_KEYS
            ):
                raise CollectiblesRequestError("saved rune candidate attributes are incomplete")

    def _recast_response(self, pending: Mapping[str, Any], *, recovery: bool = False) -> bytes:
        # Candidate identity is an explicit offline extension. The source
        # generated protobuf preserves unknown fields, including this token.
        body = self.codec.encode("SC_RecastRuneRet", {
            "Errorcode": 0,
            "RuneUId": pending["target_uid"],
            "CostRuneIdList": [] if recovery else pending["request"]["consumed_rune_uids"],
            "LockAttrList": pending["request"]["locked_indexes"],
            "RuneItem": self._rune_message(pending["candidate"]),
        })
        return body + b"\xf2\x07\x20" + pending["candidate_id"].encode("ascii")

    def pending_recast_response(self, profile: Mapping[str, Any], rune_uid: int) -> bytes:
        """Read-only recovery for the source refine page; never rolls or pays."""
        if self._pending_rune_uid(profile) != rune_uid:
            return b""
        pending = profile[RUNE_RECAST_KEY]["pending"]
        self._validate_recast_pending(pending)
        target = self._rune_by_uid(profile.get("rune_items", []), rune_uid)
        if target is None:
            return b""
        # The live rune may have been upgraded/equipped while the choice was
        # pending. Its identity, growth and ownership stay authoritative.
        restored = copy.deepcopy(pending)
        restored["candidate"] = dict(target, **{
            key: pending["candidate"][key] for key in RUNE_RECAST_ATTR_KEYS
        })
        return self._recast_response(restored, recovery=True)

    def _recast_rune(self, profile: Mapping[str, Any], body: bytes) -> CollectiblesResult:
        rune_uid = 0
        try:
            fields = _wire_fields(body)
            if set(fields) - {1, 2, 3, 127} or len(fields.get(1, ())) != 1:
                raise CollectiblesRequestError("invalid recast request fields")
            rune_uid = _last_varint(fields, 1)
            if not 0 < rune_uid <= UINT64_MAX:
                raise CollectiblesRequestError("invalid recast rune UID")
            consumed_uids = tuple(sorted(_repeated_varints(fields, 2)))
            locks = tuple(sorted(_repeated_varints(fields, 3)))
            token = self._recast_token(fields, 127)
            if len(locks) > 2 or len(set(locks)) != len(locks) or any(i not in (1, 2, 3) for i in locks):
                raise CollectiblesRequestError("lock indexes must be distinct slots 1..3, at most two")
            if (len(set(consumed_uids)) != len(consumed_uids) or rune_uid in consumed_uids
                    or any(not 0 < uid <= UINT64_MAX for uid in consumed_uids)):
                raise CollectiblesRequestError("invalid consumed rune UID list")
            request = {"target_uid": rune_uid, "consumed_rune_uids": list(consumed_uids),
                       "locked_indexes": list(locks)}
            recast = copy.deepcopy(profile.get(RUNE_RECAST_KEY, {}))
            if not isinstance(recast, dict):
                raise CollectiblesRequestError("invalid saved rune recast state")
            pending = recast.get("pending")
            if pending:
                self._validate_recast_pending(pending)
                if pending.get("request") != request:
                    raise CollectiblesRequestError("resolve the existing rune candidate before another recast")
                if token and token != pending.get("operation_id", pending["candidate_id"]):
                    raise CollectiblesRequestError("a different operation already owns the pending candidate")
                return CollectiblesResult(SC_RECAST_RUNE_RET, self._recast_response(pending),
                                          reason="pending candidate restored without another charge")

            seeded = self._seeded_copy(profile)
            runes = copy.deepcopy(seeded["rune_items"])
            target = self._rune_by_uid(runes, rune_uid)
            if target is None:
                raise CollectiblesRequestError(f"unknown recast rune UID {rune_uid}")
            static = self.rune_static.get(target["id"])
            if static is None or self._rune_growth_row(target) is None:
                raise CollectiblesRequestError("invalid target rune configuration or growth")
            cost = self.rune_recast_cost.get(_as_int(static.get("strengthen_group")))
            if cost is None:
                raise CollectiblesRequestError("missing rune recast cost")
            suffix = "" if not locks else f"_{len(locks)}_locked"
            item_id = _as_int(cost.get("item_id_cost" + suffix))
            item_count = _as_int(cost.get("item_counts" + suffix))
            base_count = _as_int(cost.get("item_counts"))
            if item_id <= 0 or item_count <= 0 or base_count <= 0:
                raise CollectiblesRequestError("invalid rune recast cost")
            item_costs, notifications = [], []
            inventory = None
            if consumed_uids:
                required_runes = (item_count + base_count - 1) // base_count
                if len(consumed_uids) != required_runes:
                    raise CollectiblesRequestError(f"recast requires exactly {required_runes} equivalent runes")
                for uid in consumed_uids:
                    material = self._rune_by_uid(runes, uid)
                    if material is None or material["id"] != target["id"]:
                        raise CollectiblesRequestError("recast material must have the exact same rune ID")
                    if _uint(material.get("state")) or _uint(material.get("hero_uid")):
                        raise CollectiblesRequestError("recast material is locked or equipped")
                    if self._rune_growth_row(material) is None:
                        raise CollectiblesRequestError("invalid consumed rune growth")
                runes = [row for row in runes if row["rune_item_uid"] not in consumed_uids]
            else:
                inventory = normalize_inventory(copy.deepcopy(seeded.get("inventory", {})))
                item = inventory.get(str(item_id))
                if item is None or item["count"] < item_count:
                    return self._error(SC_RECAST_RUNE_RET, "SC_RecastRuneRet",
                                       "rune recast material count is insufficient",
                                       error_code=ERROR_ITEM_NUM_NOT_ENOUGH, RuneUId=rune_uid)
                item["count"] -= item_count
                item_costs = [{"type": 4, "id": item_id, "num": item_count}]
                notifications.append(make_update_item_packet(_uint(seeded.get("role_id")), item, self.codec))

            counter = recast.get("counter", 0)
            if type(counter) is not int or counter < 0:
                raise CollectiblesRequestError("invalid rune recast counter")
            counter += 1
            seed = hashlib.sha256(json.dumps([
                "mte-rune-recast", POLICY_VERSION, seeded.get("role_id"),
                seeded.get("rng_seed", seeded.get("role_id")), counter, rune_uid,
            ], separators=(",", ":")).encode()).hexdigest()
            candidate = generate_candidate(static, target,
                self.catalog.table("a_secondary_attr_poor").records, locks, random.Random(int(seed, 16)))
            pending = {"candidate_id": token or seed[:32], "target_uid": rune_uid,
                       "operation_id": token,
                       "policy_version": POLICY_VERSION, "request": request, "candidate": candidate,
                       "base_attrs": {key: target[key] for key in RUNE_RECAST_ATTR_KEYS}}
            recast.update(policy_version=POLICY_VERSION, counter=counter, pending=pending)
            history = self._history(seeded, "resource_history")
            history.append({"source": "rune_recast", "command_id": CS_RECAST_RUNE,
                            "operation": "candidate", "rune_uid": rune_uid,
                            "candidate_id": pending["candidate_id"], "policy_version": POLICY_VERSION,
                            "locked_indexes": list(locks), "consumed_rune_uids": list(consumed_uids),
                            "costs": item_costs})
        except (CollectiblesRequestError, RuneRecastPolicyError, InventoryContentError) as exc:
            return self._error(SC_RECAST_RUNE_RET, "SC_RecastRuneRet", str(exc), RuneUId=rune_uid)

        changes = {RUNE_RECAST_KEY: recast, "resource_history": history}
        if consumed_uids:
            changes["rune_items"] = runes
            changes[COLLECTIBLES_SEED_VERSION_KEY] = COLLECTIBLES_SEED_VERSION
        if inventory is not None:
            changes["inventory"] = inventory
        return CollectiblesResult(SC_RECAST_RUNE_RET, self._recast_response(pending),
                                  changes=changes, notifications=tuple(notifications))

    def _recast_rune_result(self, profile: Mapping[str, Any], body: bytes) -> CollectiblesResult:
        try:
            fields = _wire_fields(body)
            if set(fields) - {1, 126, 127} or len(fields.get(1, ())) > 1:
                raise CollectiblesRequestError("invalid recast confirmation fields")
            save = _last_varint(fields, 1)
            if save not in (0, 1):
                raise CollectiblesRequestError("Save must be boolean")
            candidate_id = self._recast_token(fields, 126)
            self._recast_token(fields, 127)
            recast = copy.deepcopy(profile.get(RUNE_RECAST_KEY, {}))
            if not isinstance(recast, dict):
                raise CollectiblesRequestError("invalid saved rune recast state")
            pending = recast.get("pending")
            if not isinstance(pending, dict) or not pending:
                raise CollectiblesRequestError("no pending rune candidate")
            self._validate_recast_pending(pending, candidate_required=bool(save))
            if not candidate_id or candidate_id != pending.get("candidate_id"):
                raise CollectiblesRequestError("confirmation does not match the pending rune candidate")
            runes = copy.deepcopy(profile.get("rune_items", []))
            target = self._rune_by_uid(runes, pending["target_uid"])
            if target is None:
                raise CollectiblesRequestError("pending target rune no longer exists")
            if save:
                if {key: target.get(key) for key in RUNE_RECAST_ATTR_KEYS} != pending["base_attrs"]:
                    raise CollectiblesRequestError("target rune attributes changed before confirmation")
                for key in RUNE_RECAST_ATTR_KEYS:
                    target[key] = pending["candidate"][key]
            recast["pending"] = None
            history = self._history(profile, "resource_history")
            history.append({"source": "rune_recast", "command_id": CS_RECAST_RUNE_RESULT,
                            "operation": "save" if save else "discard", "rune_uid": pending["target_uid"],
                            "candidate_id": candidate_id, "policy_version": pending.get("policy_version"), "costs": []})
            response = self.codec.encode("SC_RecastRuneResultRet", {
                "Errorcode": 0, "RuneItem": self._rune_message(target),
            }) + b"\xf2\x07\x20" + candidate_id.encode("ascii")
        except CollectiblesRequestError as exc:
            return self._error(SC_RECAST_RUNE_RESULT_RET, "SC_RecastRuneResultRet", str(exc))
        changes = {RUNE_RECAST_KEY: recast, "resource_history": history}
        if save:
            changes["rune_items"] = runes
        return CollectiblesResult(SC_RECAST_RUNE_RESULT_RET, response, changes=changes,
            notifications=((NOTIFY_UPDATE_RUNE, self.make_update_rune(target)),))

    def _upgrade_rune(
        self, profile: Mapping[str, Any], body: bytes
    ) -> CollectiblesResult:
        rune_uid = 0
        cost_rune_uids: Tuple[int, ...] = ()
        consumed_items: Tuple[Dict[str, int], ...] = ()
        try:
            fields = _wire_fields(body)
            rune_uid = _last_varint(fields, 1)
            cost_rune_uids = _repeated_varints(fields, 2)
            consumed_items = self._decode_consumed_items(fields, 3)
            if rune_uid <= 0:
                raise CollectiblesRequestError("CS_UpGradeRune has no RuneUId")
            if len(cost_rune_uids) != len(set(cost_rune_uids)):
                raise CollectiblesRequestError("CostRuneIdList contains duplicates")
            if rune_uid in cost_rune_uids:
                raise CollectiblesRequestError("target rune cannot consume itself")
            if self._pending_rune_uid(profile) in cost_rune_uids:
                raise CollectiblesRequestError("cannot consume a rune with a pending recast candidate")
            if not cost_rune_uids and not consumed_items:
                raise CollectiblesRequestError("rune upgrade has no material")

            seeded = self._seeded_copy(profile)
            before_runes = copy.deepcopy(seeded["rune_items"])
            runes = copy.deepcopy(seeded["rune_items"])
            target = self._rune_by_uid(runes, rune_uid)
            if target is None:
                raise CollectiblesRequestError(f"unknown rune UID {rune_uid}")
            quality = self._rune_quality(target)
            maximum = self.rune_max_level.get(quality, 0)
            level = _uint(target.get("level"), 0, UINT32_MAX)
            if quality <= 0 or maximum <= 0 or not 1 <= level <= maximum:
                raise CollectiblesRequestError("target rune growth state is invalid")
            if level >= maximum:
                raise CollectiblesRequestError("target rune is already max level")

            added_exp = 0
            consumed_uid_set = set(cost_rune_uids)
            for consumed_uid in cost_rune_uids:
                consumed = self._rune_by_uid(runes, consumed_uid)
                if consumed is None:
                    raise CollectiblesRequestError(
                        f"unknown consumed rune UID {consumed_uid}"
                    )
                if _uint(consumed.get("state"), 0, UINT32_MAX) != 0:
                    raise CollectiblesRequestError(
                        f"consumed rune UID {consumed_uid} is locked"
                    )
                if _uint(consumed.get("hero_uid")) != 0:
                    raise CollectiblesRequestError(
                        f"consumed rune UID {consumed_uid} is equipped"
                    )
                growth = self._rune_growth_row(consumed)
                if growth is None:
                    raise CollectiblesRequestError(
                        f"consumed rune UID {consumed_uid} has invalid growth state"
                    )
                added_exp += _uint(
                    growth.get("change_into_experience"), 0, UINT32_MAX
                ) + _uint(consumed.get("cur_exp"), 0, UINT32_MAX)

            try:
                inventory = normalize_inventory(
                    copy.deepcopy(seeded.get("inventory", {}))
                )
            except InventoryContentError as exc:
                raise CollectiblesRequestError(str(exc)) from exc
            inventory_by_uid = {row["uid"]: row for row in inventory.values()}
            item_costs = []
            for consumed in consumed_items:
                item = inventory_by_uid.get(consumed["item_uid"])
                if item is None or item["id"] != RUNE_POWDER_ITEM_ID:
                    raise CollectiblesRequestError(
                        f"material UID {consumed['item_uid']} is not rune powder"
                    )
                if item["count"] < consumed["count"]:
                    return self._error(
                        SC_UPGRADE_RUNE_RET,
                        "SC_UpGradeRuneRet",
                        "rune powder count is insufficient",
                        error_code=ERROR_ITEM_NUM_NOT_ENOUGH,
                        RuneUId=rune_uid,
                        CostRuneIdList=list(cost_rune_uids),
                        CostItemList=[
                            {
                                "ItemUId": row["item_uid"],
                                "ItemNum": row["count"],
                            }
                            for row in consumed_items
                        ],
                    )
                item["count"] -= consumed["count"]
                added_exp += consumed["count"] * RUNE_POWDER_EXP
                item_costs.append(
                    {
                        "type": 4,
                        "id": item["id"],
                        "num": consumed["count"],
                    }
                )

            current_exp = _uint(target.get("cur_exp"), 0, UINT32_MAX) + added_exp
            while level < maximum:
                growth = self.rune_growth.get((quality, level))
                required = _uint(growth.get("cost"), 0, UINT32_MAX) if growth else 0
                if required <= 0:
                    raise CollectiblesRequestError(
                        f"missing rune growth cost for quality {quality} level {level}"
                    )
                if current_exp < required:
                    break
                current_exp -= required
                level += 1
            target["level"] = level
            target["cur_exp"] = 0 if level >= maximum else current_exp
            runes = [
                row
                for row in runes
                if _uint(row.get("rune_item_uid")) not in consumed_uid_set
            ]
            seeded["rune_items"] = runes
            self._normalize_rune_equipment(seeded)

            resource_history = self._history(seeded, "resource_history")
            resource_history.append(
                {
                    "source": "rune_progression",
                    "command_id": CS_UPGRADE_RUNE,
                    "operation": "upgrade",
                    "rune_uid": rune_uid,
                    "consumed_rune_uids": list(cost_rune_uids),
                    "costs": item_costs,
                    "experience_added": added_exp,
                    "result_level": level,
                    "result_exp": target["cur_exp"],
                }
            )
        except CollectiblesRequestError as exc:
            return self._error(
                SC_UPGRADE_RUNE_RET,
                "SC_UpGradeRuneRet",
                str(exc),
                error_code=(
                    ERROR_MATERIAL_ILLEGAL
                    if "material" in str(exc).lower()
                    or "consum" in str(exc).lower()
                    else ERROR_MESSAGE_DATA_ILLEGAL
                ),
                RuneUId=rune_uid,
                CostRuneIdList=list(cost_rune_uids),
                CostItemList=[
                    {"ItemUId": row["item_uid"], "ItemNum": row["count"]}
                    for row in consumed_items
                ],
            )

        target_message = self._rune_message(target)
        notifications = list(self._rune_notifications(before_runes, runes))
        for consumed in consumed_items:
            item = inventory_by_uid[consumed["item_uid"]]
            notifications.append(
                make_update_item_packet(
                    _uint(seeded.get("role_id")), item, self.codec
                )
            )
        response_items = [
            {"ItemUId": row["item_uid"], "ItemNum": row["count"]}
            for row in consumed_items
        ]
        return CollectiblesResult(
            SC_UPGRADE_RUNE_RET,
            self.codec.encode(
                "SC_UpGradeRuneRet",
                {
                    "Errorcode": ERROR_SUCCESS,
                    "RuneUId": rune_uid,
                    "CostRuneIdList": list(cost_rune_uids),
                    "CostItemList": response_items,
                    "RuneItem": target_message,
                },
            ),
            changes={
                "rune_items": runes,
                "hero_runes": seeded["hero_runes"],
                "inventory": inventory,
                "resource_history": resource_history,
                COLLECTIBLES_SEED_VERSION_KEY: COLLECTIBLES_SEED_VERSION,
            },
            notifications=tuple(notifications),
        )

    def _decompose_rewards(
        self, rune: Mapping[str, Any]
    ) -> Tuple[Dict[str, int], ...]:
        growth = self._rune_growth_row(rune)
        if growth is None:
            raise CollectiblesRequestError("rune has invalid decomposition growth state")
        raw = growth.get("change_into_up_item")
        try:
            values = json.loads(raw) if isinstance(raw, str) else raw
        except json.JSONDecodeError as exc:
            raise CollectiblesRequestError("rune decomposition reward is invalid") from exc
        if not isinstance(values, list) or not values:
            raise CollectiblesRequestError("rune decomposition reward is empty")
        rewards = []
        for value in values:
            if not isinstance(value, Mapping):
                raise CollectiblesRequestError("rune decomposition reward is malformed")
            reward = {
                "type": _uint(value.get("type"), 0, UINT32_MAX),
                "id": _uint(value.get("id"), 0, UINT32_MAX),
                "num": _uint(value.get("num"), 0, UINT32_MAX),
            }
            if reward["type"] != 4 or reward["id"] <= 0 or reward["num"] <= 0:
                raise CollectiblesRequestError(
                    "rune decomposition has an unsupported reward"
                )
            rewards.append(reward)
        return tuple(rewards)

    def _decompose_runes(
        self, profile: Mapping[str, Any], body: bytes
    ) -> CollectiblesResult:
        rune_uids: Tuple[int, ...] = ()
        try:
            rune_uids = _repeated_varints(_wire_fields(body), 1)
            if not rune_uids:
                raise CollectiblesRequestError("decomposition list is empty")
            if len(rune_uids) != len(set(rune_uids)):
                raise CollectiblesRequestError("decomposition list contains duplicates")
            if self._pending_rune_uid(profile) in rune_uids:
                raise CollectiblesRequestError("cannot decompose a rune with a pending recast candidate")
            seeded = self._seeded_copy(profile)
            runes = copy.deepcopy(seeded["rune_items"])
            selected = []
            for rune_uid in rune_uids:
                rune = self._rune_by_uid(runes, rune_uid)
                if rune is None:
                    raise CollectiblesRequestError(f"unknown rune UID {rune_uid}")
                if _uint(rune.get("state"), 0, UINT32_MAX) != 0:
                    raise CollectiblesRequestError(f"rune UID {rune_uid} is locked")
                if _uint(rune.get("hero_uid")) != 0:
                    raise CollectiblesRequestError(f"rune UID {rune_uid} is equipped")
                selected.append(rune)

            rewards_by_key: Dict[Tuple[int, int], int] = {}
            for rune in selected:
                for reward in self._decompose_rewards(rune):
                    key = (reward["type"], reward["id"])
                    rewards_by_key[key] = rewards_by_key.get(key, 0) + reward["num"]
                    if rewards_by_key[key] > UINT32_MAX:
                        raise CollectiblesRequestError(
                            "combined decomposition reward exceeds uint32"
                        )
            rewards = [
                {"type": key[0], "id": key[1], "num": rewards_by_key[key]}
                for key in sorted(rewards_by_key)
            ]

            try:
                inventory = normalize_inventory(
                    copy.deepcopy(seeded.get("inventory", {}))
                )
            except InventoryContentError as exc:
                raise CollectiblesRequestError(str(exc)) from exc
            changed_inventory_ids = []
            applied_rewards = []
            for reward in rewards:
                key = str(reward["id"])
                item = inventory.get(key)
                if item is None:
                    raise CollectiblesRequestError(
                        f"decomposition reward item {reward['id']} is not in inventory"
                    )
                maximum = self.item_max_stack.get(reward["id"], 0)
                updated = item["count"] + reward["num"]
                applied = reward["num"]
                if maximum <= 0:
                    raise CollectiblesRequestError(
                        f"decomposition reward item {reward['id']} has no stack cap"
                    )
                if updated > maximum:
                    if seeded.get("all_content_unlocked") and item["count"] == maximum:
                        applied = 0
                        updated = maximum
                    else:
                        return self._error(
                            SC_DECOMPOSE_RUNE_RET,
                            "SC_DecomposeRuneRet",
                            f"decomposition reward item {reward['id']} exceeds cap",
                            error_code=ERROR_EXCEED_ITEM_LIMIT_COUNT,
                            DecomposeRuneIdList=list(rune_uids),
                            RewardList=[],
                        )
                item["count"] = updated
                if applied:
                    changed_inventory_ids.append(reward["id"])
                applied_rewards.append(
                    {"type": reward["type"], "id": reward["id"], "num": applied}
                )

            removed = set(rune_uids)
            runes = [
                row
                for row in runes
                if _uint(row.get("rune_item_uid")) not in removed
            ]
            seeded["rune_items"] = runes
            self._normalize_rune_equipment(seeded)
            reward_history = self._history(seeded, "reward_history")
            resource_history = self._history(seeded, "resource_history")
            transaction_number = len(resource_history) + 1
            reward_history.append(
                {
                    "claim_key": f"rune:decompose:{transaction_number}",
                    "source": "rune_decompose",
                    "source_id": transaction_number,
                    "command_id": CS_DECOMPOSE_RUNE,
                    "rune_uids": list(rune_uids),
                    "rewards": copy.deepcopy(rewards),
                    "applied_rewards": copy.deepcopy(applied_rewards),
                }
            )
            resource_history.append(
                {
                    "source": "rune_progression",
                    "command_id": CS_DECOMPOSE_RUNE,
                    "operation": "decompose",
                    "consumed_rune_uids": list(rune_uids),
                    "rewards": copy.deepcopy(rewards),
                    "applied_rewards": copy.deepcopy(applied_rewards),
                }
            )
        except CollectiblesRequestError as exc:
            return self._error(
                SC_DECOMPOSE_RUNE_RET,
                "SC_DecomposeRuneRet",
                str(exc),
                error_code=(
                    ERROR_MATERIAL_ILLEGAL
                    if "reward" in str(exc).lower()
                    else ERROR_MESSAGE_DATA_ILLEGAL
                ),
                DecomposeRuneIdList=list(rune_uids),
                RewardList=[],
            )

        notifications = tuple(
            make_update_item_packet(
                _uint(seeded.get("role_id")), inventory[str(item_id)], self.codec
            )
            for item_id in changed_inventory_ids
        )
        return CollectiblesResult(
            SC_DECOMPOSE_RUNE_RET,
            self.codec.encode(
                "SC_DecomposeRuneRet",
                {
                    "Errorcode": ERROR_SUCCESS,
                    "DecomposeRuneIdList": list(rune_uids),
                    "RewardList": [
                        {
                            "ResType": reward["type"],
                            "ItemId": reward["id"],
                            "Count": reward["num"],
                        }
                        for reward in rewards
                    ],
                },
            ),
            changes={
                "rune_items": runes,
                "hero_runes": seeded["hero_runes"],
                "inventory": inventory,
                "reward_history": reward_history,
                "resource_history": resource_history,
                COLLECTIBLES_SEED_VERSION_KEY: COLLECTIBLES_SEED_VERSION,
            },
            notifications=notifications,
        )

    def _equip_rune(
        self, profile: Mapping[str, Any], body: bytes
    ) -> CollectiblesResult:
        rune_uid = 0
        requested_hero = 0
        opt = 0
        requested_position = 0
        requested_hero_id = 0
        try:
            fields = _wire_fields(body)
            rune_uid = _last_varint(fields, 1)
            requested_hero = _last_varint(fields, 2)
            opt = _last_varint(fields, 3)
            requested_position = _last_varint(fields, 4)
            requested_hero_id = _last_varint(fields, 5)
            if rune_uid <= 0:
                raise CollectiblesRequestError("CS_EquipRune has no RuneUId")
            if opt not in {0, 1}:
                raise CollectiblesRequestError(f"unsupported rune equip Opt {opt}")

            seeded = self._seeded_copy(profile)
            before_runes = copy.deepcopy(seeded["rune_items"])
            runes = copy.deepcopy(seeded["rune_items"])
            rune = self._rune_by_uid(runes, rune_uid)
            if rune is None:
                raise CollectiblesRequestError(f"unknown rune UID {rune_uid}")
            position = self._rune_position(_uint(rune.get("id"), 0, UINT32_MAX))
            if position not in {1, 2, 3, 4, 5}:
                raise CollectiblesRequestError("rune has no valid static position")
            if requested_position and requested_position != position:
                raise CollectiblesRequestError(
                    f"rune belongs to position {position}, not {requested_position}"
                )

            hero_id = 0
            if opt == 0:
                hero_ids, uid_to_id = self._hero_identity_maps(seeded)
                raw_hero = requested_hero or requested_hero_id
                hero_id = uid_to_id.get(raw_hero, raw_hero)
                if hero_id not in hero_ids:
                    raise CollectiblesRequestError(f"unknown witch ID {raw_hero}")
                for other in runes:
                    if (
                        other is not rune
                        and _uint(other.get("hero_uid")) == hero_id
                        and self._rune_position(
                            _uint(other.get("id"), 0, UINT32_MAX)
                        )
                        == position
                    ):
                        other["hero_uid"] = 0
                rune["hero_uid"] = hero_id
            else:
                rune["hero_uid"] = 0

            seeded["rune_items"] = runes
            # HeroRuneList is a derived compatibility snapshot.  During a
            # live mutation it still describes the *previous* loadout and must
            # not be treated as migration input, or it would silently undo a
            # replacement/unequip before the response is sent.
            seeded["hero_runes"] = []
            self._normalize_rune_equipment(seeded)
            runes = seeded["rune_items"]
        except CollectiblesRequestError as exc:
            return self._error(
                SC_EQUIP_RUNE_RET,
                "SC_EquipRuneRet",
                str(exc),
                RuneUId=rune_uid,
                HeroUId=requested_hero,
                Opt=opt,
                Position=requested_position,
                HeroId=requested_hero_id,
            )

        notifications = self._rune_notifications(before_runes, runes)
        return CollectiblesResult(
            SC_EQUIP_RUNE_RET,
            self.codec.encode(
                "SC_EquipRuneRet",
                {
                    "Errorcode": ERROR_SUCCESS,
                    "RuneUId": rune_uid,
                    "HeroUId": hero_id,
                    "Opt": opt,
                    "Position": position,
                    "HeroId": requested_hero_id,
                },
            ),
            changes={
                "rune_items": runes,
                "hero_runes": seeded["hero_runes"],
                COLLECTIBLES_SEED_VERSION_KEY: COLLECTIBLES_SEED_VERSION,
            },
            notifications=notifications,
        )

    def _lock_rune(
        self, profile: Mapping[str, Any], body: bytes
    ) -> CollectiblesResult:
        try:
            fields = _wire_fields(body)
            rune_uid = _last_varint(fields, 1)
            state = _last_varint(fields, 2)
            seeded = self._seeded_copy(profile)
            before_runes = copy.deepcopy(seeded["rune_items"])
            runes = copy.deepcopy(seeded["rune_items"])
            rune = next(
                (row for row in runes if row["rune_item_uid"] == rune_uid), None
            )
            if rune is None:
                raise CollectiblesRequestError(f"unknown rune UID {rune_uid}")
        except CollectiblesRequestError as exc:
            return self._error(
                SC_LOCK_RUNE_RET,
                "SC_LockRuneRet",
                str(exc),
                RuneUId=locals().get("rune_uid", 0),
                State=locals().get("state", 0),
            )
        rune["state"] = _uint(state, 0, UINT32_MAX)
        return CollectiblesResult(
            SC_LOCK_RUNE_RET,
            self.codec.encode(
                "SC_LockRuneRet",
                {
                    "Errorcode": ERROR_SUCCESS,
                    "RuneUId": rune_uid,
                    "State": rune["state"],
                },
            ),
            changes={
                "rune_items": runes,
                COLLECTIBLES_SEED_VERSION_KEY: COLLECTIBLES_SEED_VERSION,
            },
            notifications=self._rune_notifications(before_runes, runes),
        )

    def handle(
        self,
        request_id: int,
        profile: Optional[Mapping[str, Any]],
        request_body: bytes = b"",
    ) -> Optional[CollectiblesResult]:
        """Handle collectible requests or return ``None`` for another layer."""

        profile = profile or {}
        request_id = int(request_id)
        if request_id == CS_REQUEST_DECORATION_ITEM_LIST:
            return CollectiblesResult(
                SC_DECORATION_ITEM_LIST_RET,
                self.make_decoration_item_list_ret(profile),
            )
        if request_id == CS_REQUEST_HERO_APPEARANCE_DATAS:
            return CollectiblesResult(
                SC_HERO_APPEARANCE_DATAS_RET,
                self.make_hero_appearance_datas_ret(profile),
            )
        if request_id == CS_SAVE_HERO_APPEARANCE:
            return self._save_hero_appearance(profile, request_body)
        if request_id == CS_SET_HERO_CUR_APPEARANCE_ID:
            return self._set_current_appearance(profile, request_body)
        if request_id == CS_REQUEST_MEDAL_DATAS:
            return CollectiblesResult(SC_MEDAL_DATAS_RET, self.make_medal_datas_ret(profile))
        if request_id == CS_VIEW_MEDAL:
            return self._view_medal(profile, request_body)
        if request_id == CS_REQUEST_MEDAL_SLOT_DATAS:
            return CollectiblesResult(
                SC_MEDAL_SLOT_DATAS_RET, self.make_medal_slot_datas_ret(profile)
            )
        if request_id == CS_SET_MEDAL_SLOT:
            return self._set_medal_slots(profile, request_body)
        if request_id == CS_SET_MEDAL_STATE:
            return self._set_medal_state(profile, request_body)
        if request_id == CS_SET_ALL_MEDAL_STATE:
            return self._set_all_medal_states(profile, request_body)
        if request_id == CS_TAKE_MEDAL:
            return self._take_medal(profile, request_body)
        if request_id == CS_RECAST_RUNE:
            return self._recast_rune(profile, request_body)
        if request_id == CS_RECAST_RUNE_RESULT:
            return self._recast_rune_result(profile, request_body)
        if request_id == CS_UPGRADE_RUNE:
            return self._upgrade_rune(profile, request_body)
        if request_id == CS_LOCK_RUNE:
            return self._lock_rune(profile, request_body)
        if request_id == CS_DECOMPOSE_RUNE:
            return self._decompose_runes(profile, request_body)
        if request_id == CS_EQUIP_RUNE:
            return self._equip_rune(profile, request_body)
        if request_id == CS_REQUEST_PRESETS:
            return self._request_presets(profile)
        if request_id == CS_SAVE_PRESETS:
            return self._save_presets(profile, request_body)
        if request_id == CS_USE_PRESETS:
            return self._use_presets(profile, request_body)
        return None

    @staticmethod
    def apply_changes(profile: MutableMapping[str, Any], changes: Mapping[str, Any]) -> None:
        profile.update(copy.deepcopy(dict(changes)))


@lru_cache(maxsize=1)
def default_layer() -> CollectiblesLayer:
    return CollectiblesLayer()


def seed_collectibles_profile(
    profile: MutableMapping[str, Any],
) -> MutableMapping[str, Any]:
    return default_layer().seed_profile(profile)


def handle_collectibles_request(
    request_id: int,
    profile: Optional[Mapping[str, Any]],
    request_body: bytes = b"",
) -> Optional[CollectiblesResult]:
    return default_layer().handle(request_id, profile, request_body)


__all__ = [
    "COLLECTIBLES_SEED_VERSION",
    "COLLECTIBLES_SEED_VERSION_KEY",
    "CS_DECOMPOSE_RUNE",
    "CS_EQUIP_RUNE",
    "CS_LOCK_RUNE",
    "CS_REQUEST_PRESETS",
    "CS_REQUEST_DECORATION_ITEM_LIST",
    "CS_REQUEST_HERO_APPEARANCE_DATAS",
    "CS_REQUEST_MEDAL_DATAS",
    "CS_REQUEST_MEDAL_SLOT_DATAS",
    "CS_SAVE_HERO_APPEARANCE",
    "CS_SAVE_PRESETS",
    "CS_SET_ALL_MEDAL_STATE",
    "CS_SET_HERO_CUR_APPEARANCE_ID",
    "CS_SET_MEDAL_SLOT",
    "CS_SET_MEDAL_STATE",
    "CS_TAKE_MEDAL",
    "CS_UPGRADE_RUNE",
    "CS_USE_PRESETS",
    "CS_VIEW_MEDAL",
    "CollectiblesConfigError",
    "CollectiblesLayer",
    "CollectiblesRequestError",
    "CollectiblesResult",
    "ERROR_EXCEED_ITEM_LIMIT_COUNT",
    "ERROR_ITEM_NUM_NOT_ENOUGH",
    "ERROR_MATERIAL_ILLEGAL",
    "ERROR_MESSAGE_DATA_ILLEGAL",
    "ERROR_PRESET_DECORATION_EMPTY",
    "ERROR_PRESET_RUNE_EMPTY",
    "ERROR_SUCCESS",
    "EXPECTED_TABLE_COUNTS",
    "MEDAL_SLOT_COUNT",
    "NOTIFY_UPDATE_HERO_RUNE",
    "NOTIFY_UPDATE_RUNE",
    "PRESETS_V2_KEY",
    "PRESETS_V2_SCHEMA_VERSION",
    "PRESET_SLOT_COUNT",
    "PRESET_TYPE_DECORATION",
    "PRESET_TYPE_RUNE",
    "RUNE_POWDER_EXP",
    "RUNE_POWDER_ITEM_ID",
    "RUNE_UID_BASE",
    "RESPONSE_IDS",
    "SC_DECOMPOSE_RUNE_RET",
    "SC_DECORATION_ITEM_LIST_RET",
    "SC_EQUIP_RUNE_RET",
    "SC_HERO_APPEARANCE_DATAS_RET",
    "SC_LOCK_RUNE_RET",
    "SC_MEDAL_DATAS_RET",
    "SC_MEDAL_SLOT_DATAS_RET",
    "SC_RUNE_DATAS",
    "SC_REQUEST_PRESETS_RET",
    "SC_SAVE_PRESETS_RET",
    "SC_SAVE_HERO_APPEARANCE_RET",
    "SC_SET_ALL_MEDAL_STATE_RET",
    "SC_SET_HERO_CUR_APPEARANCE_ID_RET",
    "SC_SET_MEDAL_SLOT_RET",
    "SC_SET_MEDAL_STATE_RET",
    "SC_TAKE_MEDAL_RET",
    "SC_UPGRADE_RUNE_RET",
    "SC_USE_PRESETS_RET",
    "SC_VIEW_MEDAL_RET",
    "default_layer",
    "handle_collectibles_request",
    "seed_collectibles_profile",
]
