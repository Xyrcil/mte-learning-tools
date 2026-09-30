"""Safe, catalog-backed inventory seeding and protobuf snapshots.

The defined ``a_item`` table contains currencies that belong outside the
bag, unfinished/test records, and zero-stack placeholders.  This module keeps
those records out of the all-unlocked inventory while retaining one stable
``BagItem`` identity per seeded item.

Profiles written by older server revisions used ``{"item_id": count}``.
Readers and mutation helpers accept that form, but every write performed here
uses the canonical ``uid/id/count/para`` record form.
"""

from __future__ import annotations

from collections.abc import Mapping, MutableMapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Dict, Iterable, Optional, Tuple

try:  # Support package imports and direct ``python server/...`` execution.
    from .content_profile import ContentCatalog, default_content_catalog
    from .protobuf_codec import ProtocolMessageCodec
except ImportError:  # pragma: no cover - direct script compatibility
    from content_profile import ContentCatalog, default_content_catalog  # type: ignore
    from protobuf_codec import ProtocolMessageCodec  # type: ignore


UINT32_MAX = (1 << 32) - 1
UINT64_MAX = (1 << 64) - 1

SC_ITEM_DATAS = 12
NOTIFY_UPDATE_ITEM = 26
NOTIFY_REMOVE_ITEM = 34

INVENTORY_SEED_VERSION = 1
INVENTORY_SEED_VERSION_KEY = "inventory_seed_version"
INVENTORY_SEED_CATALOG_KEY = "inventory_seed_catalog_version"

# Item IDs occupy a much smaller uint32 namespace.  A fixed offset makes the
# server-owned uint64 identity deterministic and independent of table order.
ITEM_UID_BASE = 10_000_000_000

UNIQUE_ITEM_COUNT = 1
INGREDIENT_ITEM_COUNT = 999
ORDINARY_ITEM_COUNT = 999_999

SEED_BUCKET_UNIQUE = "unique"
SEED_BUCKET_INGREDIENT = "ingredient"
SEED_BUCKET_ORDINARY = "ordinary"

EXCLUDED_TEXT_MARKERS = ("测试", "待定")
EXCLUDED_TEXT_FIELDS = ("name", "function", "describe")


class InventoryContentError(ValueError):
    """Raised when catalog or persisted inventory data is unsafe."""


def is_explicitly_excluded_item_id(item_id: int) -> bool:
    """Return whether an item ID is a known unsafe/non-bag resource."""

    return (
        1_000_001 <= item_id <= 1_000_005
        or item_id in {1_001_018, 1_001_031}
        or 1_015_001 <= item_id <= 1_015_036
        or 1_015_040 <= item_id <= 1_015_050
    )


def stable_item_uid(item_id: int) -> int:
    """Derive the stable server-owned ``BagItem.ItemUId`` for an item ID."""

    item_id = _strict_int(item_id, "item_id", minimum=1, maximum=UINT32_MAX)
    uid = ITEM_UID_BASE + item_id
    if uid > UINT64_MAX:  # Defensive if the constants are changed later.
        raise InventoryContentError("stable item UID exceeds uint64")
    return uid


@dataclass(frozen=True)
class InventoryItem:
    """Canonical persisted form of one bag stack."""

    uid: int
    id: int
    count: int
    para: int = 0

    def as_dict(self) -> Dict[str, int]:
        return {
            "uid": self.uid,
            "id": self.id,
            "count": self.count,
            "para": self.para,
        }

    def as_protocol(self) -> Dict[str, int]:
        return {
            "ItemUId": self.uid,
            "ItemId": self.id,
            "Count": self.count,
            "Para": self.para,
        }


@dataclass(frozen=True)
class SeedItem:
    """One catalog item selected for the one-time inventory seed."""

    item: InventoryItem
    bucket: str

    @property
    def id(self) -> int:
        return self.item.id

    @property
    def count(self) -> int:
        return self.item.count


def _strict_int(
    value: Any,
    label: str,
    *,
    minimum: int = 0,
    maximum: int = UINT32_MAX,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InventoryContentError(f"{label} must be an integer")
    if value < minimum or value > maximum:
        raise InventoryContentError(
            f"{label} is outside {minimum}..{maximum}"
        )
    return value


def _row_int(
    row: Mapping[str, Any],
    name: str,
    item_id: Any,
    *,
    minimum: int = 0,
) -> int:
    return _strict_int(
        row.get(name),
        f"a_item[{item_id!r}].{name}",
        minimum=minimum,
    )


def item_exclusion_reason(row: Mapping[str, Any]) -> Optional[str]:
    """Return a stable exclusion label, or ``None`` for a seedable row."""

    item_id = _row_int(row, "id", row.get("id"), minimum=1)
    maximum = _row_int(row, "max_stack_num", item_id)
    if maximum <= 0:
        return "max_stack_num"
    if is_explicitly_excluded_item_id(item_id):
        return "unsafe_id"
    for field_name in EXCLUDED_TEXT_FIELDS:
        value = row.get(field_name, "")
        if value is None:
            continue
        if not isinstance(value, str):
            raise InventoryContentError(
                f"a_item[{item_id}].{field_name} must be text"
            )
        if any(marker in value for marker in EXCLUDED_TEXT_MARKERS):
            return "test_or_pending"
    return None


def _seed_item(row: Mapping[str, Any]) -> Optional[SeedItem]:
    if item_exclusion_reason(row) is not None:
        return None

    item_id = _row_int(row, "id", row.get("id"), minimum=1)
    maximum = _row_int(row, "max_stack_num", item_id, minimum=1)
    item_type = _row_int(row, "type", item_id)
    ingredient = _row_int(row, "dish_ingredient", item_id)

    if item_type == 5 or maximum == 1:
        bucket = SEED_BUCKET_UNIQUE
        count = min(UNIQUE_ITEM_COUNT, maximum)
    elif ingredient > 0:
        bucket = SEED_BUCKET_INGREDIENT
        count = min(INGREDIENT_ITEM_COUNT, maximum)
    else:
        # Type 1 includes legitimate event currencies.  They are deliberately
        # retained, subject to the same conservative seed cap as ordinary
        # stackable items.
        bucket = SEED_BUCKET_ORDINARY
        count = min(ORDINARY_ITEM_COUNT, maximum)

    return SeedItem(
        InventoryItem(stable_item_uid(item_id), item_id, count, 0),
        bucket,
    )


def inventory_seed_plan(
    catalog: Optional[ContentCatalog] = None,
) -> Tuple[SeedItem, ...]:
    """Build the deterministic safe seed plan from ``ContentCatalog.a_item``."""

    content = catalog or default_content_catalog()
    try:
        table = content.table("a_item")
    except (AttributeError, KeyError) as exc:
        raise InventoryContentError("content catalog has no a_item table") from exc

    required = {"id", "type", "max_stack_num", "dish_ingredient"}
    missing = required - set(table.fields)
    if missing:
        raise InventoryContentError(
            f"a_item is missing required fields: {sorted(missing)}"
        )

    planned = []
    seen_ids = set()
    for row in table.records:
        selected = _seed_item(row)
        if selected is None:
            continue
        if selected.id in seen_ids:
            raise InventoryContentError(f"duplicate seed item ID {selected.id}")
        seen_ids.add(selected.id)
        planned.append(selected)
    planned.sort(key=lambda selected: selected.id)
    return tuple(planned)


def inventory_seed_buckets(
    catalog: Optional[ContentCatalog] = None,
) -> Dict[str, Tuple[SeedItem, ...]]:
    """Return the seed plan grouped into non-overlapping count-policy buckets."""

    grouped = {
        SEED_BUCKET_UNIQUE: [],
        SEED_BUCKET_INGREDIENT: [],
        SEED_BUCKET_ORDINARY: [],
    }
    for selected in inventory_seed_plan(catalog):
        grouped[selected.bucket].append(selected)
    return {name: tuple(rows) for name, rows in grouped.items()}


def _mapping_value(
    value: Mapping[str, Any], lower_name: str, protocol_name: str, default: Any
) -> Any:
    if lower_name in value:
        return value[lower_name]
    if protocol_name in value:
        return value[protocol_name]
    return default


def _key_item_id(key: Any) -> Optional[int]:
    if isinstance(key, bool):
        return None
    if isinstance(key, int):
        return key if 1 <= key <= UINT32_MAX else None
    if isinstance(key, str):
        try:
            parsed = int(key, 10)
        except ValueError:
            return None
        return parsed if 1 <= parsed <= UINT32_MAX else None
    return None


def _inventory_item(key: Any, value: Any) -> InventoryItem:
    key_id = _key_item_id(key)
    if isinstance(value, Mapping):
        raw_id = _mapping_value(value, "id", "ItemId", key_id)
        item_id = _strict_int(raw_id, "inventory item id", minimum=1)
        if key_id is not None and key_id != item_id:
            raise InventoryContentError(
                f"inventory key {key!r} disagrees with item id {item_id}"
            )
        count = _strict_int(
            _mapping_value(value, "count", "Count", None),
            f"inventory[{item_id}].count",
        )
        uid = _strict_int(
            _mapping_value(
                value, "uid", "ItemUId", stable_item_uid(item_id)
            ),
            f"inventory[{item_id}].uid",
            minimum=1,
            maximum=UINT64_MAX,
        )
        para = _strict_int(
            _mapping_value(value, "para", "Para", 0),
            f"inventory[{item_id}].para",
        )
        return InventoryItem(uid, item_id, count, para)

    if key_id is None:
        raise InventoryContentError(
            f"legacy inventory key {key!r} is not a uint32 item ID"
        )
    count = _strict_int(value, f"inventory[{key_id}].count")
    return InventoryItem(stable_item_uid(key_id), key_id, count, 0)


def normalize_inventory(value: Any) -> Dict[str, Dict[str, int]]:
    """Normalize canonical rows and legacy ``item_id -> count`` mappings.

    The returned dictionary is keyed by decimal item ID.  Duplicate aliases
    are accepted only when their canonical records are identical.
    """

    if value is None:
        entries: Iterable[Tuple[Any, Any]] = ()
    elif isinstance(value, Mapping):
        entries = value.items()
    elif isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray, memoryview)
    ):
        entries = ((None, row) for row in value)
    else:
        raise InventoryContentError("inventory must be a mapping or row sequence")

    by_id: Dict[int, InventoryItem] = {}
    uids: Dict[int, int] = {}
    for key, raw in entries:
        item = _inventory_item(key, raw)
        previous = by_id.get(item.id)
        if previous is not None and previous != item:
            raise InventoryContentError(
                f"inventory contains conflicting aliases for item {item.id}"
            )
        previous_id = uids.get(item.uid)
        if previous_id is not None and previous_id != item.id:
            raise InventoryContentError(
                f"inventory UID {item.uid} is reused by items {previous_id} and {item.id}"
            )
        by_id[item.id] = item
        uids[item.uid] = item.id

    return {
        str(item_id): by_id[item_id].as_dict()
        for item_id in sorted(by_id)
    }


def _seed_version(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return 0


def seed_inventory(
    profile: MutableMapping[str, Any],
    catalog: Optional[ContentCatalog] = None,
) -> MutableMapping[str, Any]:
    """Apply the current catalog seed once, in place, and version the result.

    Calls after this seed version are strict no-ops, so spending or deleting a
    seeded item is never undone by reconnecting.  During the first migration,
    old scalar counts are retained (and capped by the safe bucket policy).
    """

    if not isinstance(profile, MutableMapping):
        raise InventoryContentError("profile must be a mutable mapping")
    if _seed_version(profile.get(INVENTORY_SEED_VERSION_KEY)) >= INVENTORY_SEED_VERSION:
        return profile

    content = catalog or default_content_catalog()
    inventory = normalize_inventory(profile.get("inventory", {}))
    for selected in inventory_seed_plan(content):
        key = str(selected.id)
        existing = inventory.get(key)
        count = selected.count
        if existing is not None:
            count = min(existing["count"], selected.count)
        # Seed-owned metadata is deterministic.  Existing count zero remains
        # zero, which preserves a pre-migration spent balance.
        inventory[key] = InventoryItem(
            selected.item.uid,
            selected.id,
            count,
            0,
        ).as_dict()

    profile["inventory"] = inventory
    profile[INVENTORY_SEED_VERSION_KEY] = INVENTORY_SEED_VERSION
    profile[INVENTORY_SEED_CATALOG_KEY] = content.version
    return profile


def inventory_items(profile: Mapping[str, Any]) -> Tuple[InventoryItem, ...]:
    """Read positive-count bag rows from canonical or legacy profile state."""

    if not isinstance(profile, Mapping):
        raise InventoryContentError("profile must be a mapping")
    normalized = normalize_inventory(profile.get("inventory", {}))
    return tuple(
        InventoryItem(**row)
        for row in normalized.values()
        if row["count"] > 0
    )


def update_inventory_item(
    profile: MutableMapping[str, Any],
    item_id: int,
    count: int,
    *,
    uid: Optional[int] = None,
    para: Optional[int] = None,
) -> Dict[str, int]:
    """Set one absolute stack count and return its canonical record."""

    if not isinstance(profile, MutableMapping):
        raise InventoryContentError("profile must be a mutable mapping")
    item_id = _strict_int(item_id, "item_id", minimum=1)
    count = _strict_int(count, "count")
    inventory = normalize_inventory(profile.get("inventory", {}))
    previous = inventory.get(str(item_id))
    chosen_uid = (
        previous["uid"]
        if uid is None and previous is not None
        else stable_item_uid(item_id)
        if uid is None
        else _strict_int(uid, "uid", minimum=1, maximum=UINT64_MAX)
    )
    chosen_para = (
        previous["para"]
        if para is None and previous is not None
        else 0
        if para is None
        else _strict_int(para, "para")
    )
    item = InventoryItem(chosen_uid, item_id, count, chosen_para)

    for other_key, other in inventory.items():
        if other_key != str(item_id) and other["uid"] == item.uid:
            raise InventoryContentError(
                f"inventory UID {item.uid} is already used by item {other['id']}"
            )
    inventory[str(item_id)] = item.as_dict()
    profile["inventory"] = {
        key: inventory[key]
        for key in sorted(inventory, key=lambda raw: int(raw))
    }
    return item.as_dict()


def delete_inventory_item(
    profile: MutableMapping[str, Any], item_id: int
) -> Optional[Dict[str, int]]:
    """Delete one stack by item ID and return the removed canonical record."""

    if not isinstance(profile, MutableMapping):
        raise InventoryContentError("profile must be a mutable mapping")
    item_id = _strict_int(item_id, "item_id", minimum=1)
    inventory = normalize_inventory(profile.get("inventory", {}))
    removed = inventory.pop(str(item_id), None)
    profile["inventory"] = inventory
    return removed


@lru_cache(maxsize=1)
def _default_codec() -> ProtocolMessageCodec:
    return ProtocolMessageCodec()


def _codec(value: Optional[ProtocolMessageCodec]) -> ProtocolMessageCodec:
    return value if value is not None else _default_codec()


def make_item_datas(
    profile: Mapping[str, Any],
    codec: Optional[ProtocolMessageCodec] = None,
) -> bytes:
    """Encode command 12's ``SC_ItemDatas`` login snapshot body."""

    values = {"ItemList": [item.as_protocol() for item in inventory_items(profile)]}
    return _codec(codec).encode("SC_ItemDatas", values)


def make_item_datas_packet(
    profile: Mapping[str, Any],
    codec: Optional[ProtocolMessageCodec] = None,
) -> Tuple[int, bytes]:
    """Return ``(12, body)`` for callers that dispatch complete packets."""

    return SC_ITEM_DATAS, make_item_datas(profile, codec)


def _coerce_item(value: Any) -> InventoryItem:
    if isinstance(value, InventoryItem):
        return value
    if isinstance(value, Mapping):
        return _inventory_item(None, value)
    raise InventoryContentError("item must be an InventoryItem or mapping")


def make_update_item(
    role_id: int,
    item: Any,
    codec: Optional[ProtocolMessageCodec] = None,
) -> bytes:
    """Encode a ``Notify_UpdateItem`` body for a canonical item record."""

    role_id = _strict_int(role_id, "role_id", maximum=UINT64_MAX)
    row = _coerce_item(item)
    return _codec(codec).encode(
        "Notify_UpdateItem",
        {
            "RoleId": role_id,
            "ItemUId": row.uid,
            "ItemId": row.id,
            "Count": row.count,
            "Para": row.para,
        },
    )


def make_update_item_packet(
    role_id: int,
    item: Any,
    codec: Optional[ProtocolMessageCodec] = None,
) -> Tuple[int, bytes]:
    return NOTIFY_UPDATE_ITEM, make_update_item(role_id, item, codec)


def make_remove_item(
    role_id: int,
    item_or_uid: Any,
    codec: Optional[ProtocolMessageCodec] = None,
) -> bytes:
    """Encode a ``Notify_RemoveItem`` body from a row or uint64 item UID."""

    role_id = _strict_int(role_id, "role_id", maximum=UINT64_MAX)
    uid = (
        _coerce_item(item_or_uid).uid
        if isinstance(item_or_uid, (InventoryItem, Mapping))
        else _strict_int(item_or_uid, "item_uid", minimum=1, maximum=UINT64_MAX)
    )
    return _codec(codec).encode(
        "Notify_RemoveItem",
        {"RoleId": role_id, "ItemUId": uid},
    )


def make_remove_item_packet(
    role_id: int,
    item_or_uid: Any,
    codec: Optional[ProtocolMessageCodec] = None,
) -> Tuple[int, bytes]:
    return NOTIFY_REMOVE_ITEM, make_remove_item(role_id, item_or_uid, codec)


class InventoryContent:
    """Bound catalog/codec facade for server integration."""

    def __init__(
        self,
        catalog: Optional[ContentCatalog] = None,
        codec: Optional[ProtocolMessageCodec] = None,
    ) -> None:
        self.catalog = catalog or default_content_catalog()
        self.codec = codec or ProtocolMessageCodec()
        self.seed_plan = inventory_seed_plan(self.catalog)

    def seed(self, profile: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
        return seed_inventory(profile, self.catalog)

    def make_item_datas(self, profile: Mapping[str, Any]) -> bytes:
        return make_item_datas(profile, self.codec)

    def update_item(
        self,
        profile: MutableMapping[str, Any],
        item_id: int,
        count: int,
        *,
        uid: Optional[int] = None,
        para: Optional[int] = None,
    ) -> Dict[str, int]:
        return update_inventory_item(
            profile, item_id, count, uid=uid, para=para
        )

    def delete_item(
        self, profile: MutableMapping[str, Any], item_id: int
    ) -> Optional[Dict[str, int]]:
        return delete_inventory_item(profile, item_id)

    def make_update_item(self, role_id: int, item: Any) -> bytes:
        return make_update_item(role_id, item, self.codec)

    def make_remove_item(self, role_id: int, item_or_uid: Any) -> bytes:
        return make_remove_item(role_id, item_or_uid, self.codec)


# Naming compatibility with the other server subsystems' ``...Layer`` style.
InventoryContentLayer = InventoryContent
remove_inventory_item = delete_inventory_item


__all__ = [
    "EXCLUDED_TEXT_MARKERS",
    "INGREDIENT_ITEM_COUNT",
    "INVENTORY_SEED_CATALOG_KEY",
    "INVENTORY_SEED_VERSION",
    "INVENTORY_SEED_VERSION_KEY",
    "ITEM_UID_BASE",
    "InventoryContent",
    "InventoryContentError",
    "InventoryContentLayer",
    "InventoryItem",
    "NOTIFY_REMOVE_ITEM",
    "NOTIFY_UPDATE_ITEM",
    "ORDINARY_ITEM_COUNT",
    "SC_ITEM_DATAS",
    "SEED_BUCKET_INGREDIENT",
    "SEED_BUCKET_ORDINARY",
    "SEED_BUCKET_UNIQUE",
    "SeedItem",
    "UNIQUE_ITEM_COUNT",
    "delete_inventory_item",
    "inventory_items",
    "inventory_seed_buckets",
    "inventory_seed_plan",
    "is_explicitly_excluded_item_id",
    "item_exclusion_reason",
    "make_item_datas",
    "make_item_datas_packet",
    "make_remove_item",
    "make_remove_item_packet",
    "make_update_item",
    "make_update_item_packet",
    "normalize_inventory",
    "remove_inventory_item",
    "seed_inventory",
    "stable_item_uid",
    "update_inventory_item",
]
