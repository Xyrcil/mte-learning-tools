"""Static shop data and local purchase state.

Handlers receive a profile snapshot and return a protobuf response plus a
small state change. The caller persists that change before sending it.
"""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import re
import sys
import time
import zlib
from collections import defaultdict
from collections.abc import Iterable, Mapping, MutableMapping, Sequence
from dataclasses import dataclass, field, replace as dataclass_replace
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from types import ModuleType
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

try:  # Package import and direct ``python server/shop_content.py`` support.
    from .gospel_acquisition import GospelFactory
    from .offline_economy import EconomyError, deduct_resources, economy_day
    from .inventory_content import (
        InventoryContentError,
        normalize_inventory,
        stable_item_uid,
    )
    from .protobuf_codec import ProtocolMessageCodec
except ImportError:  # pragma: no cover - direct script compatibility
    from gospel_acquisition import GospelFactory
    from offline_economy import EconomyError, deduct_resources, economy_day
    from inventory_content import (  # type: ignore
        InventoryContentError,
        normalize_inventory,
        stable_item_uid,
    )
    from protobuf_codec import ProtocolMessageCodec  # type: ignore


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONTENT_DIR = ROOT / "generated" / "content"
DEFAULT_PROTOCOL_CATALOG = ROOT / "generated" / "protocol" / "protocol_catalog.json"
DEFAULT_DATA_ROOT = ROOT / "data"
DEFAULT_ASSEMBLY = ROOT / "assemblies" / "runtime-assembly.bin"
CATALOG_GENERATOR = ROOT / "tools" / "generate_content_catalog.py"

# MsgID values defined from runtime assembly and protocol_catalog.json.
CS_SHOP_PURCHASE = 53
SC_SHOP_PURCHASE_RET = 54
CS_USE_ITEM = 63
SC_USE_ITEM_RET = 64
NOTIFY_UPDATE_MULTI_ITEM = 70
CS_REQUEST_BOUGHT_ITEM_DATAS = 92
SC_BOUGHT_ITEM_DATAS_RET = 93
CS_CREATE_ORDER = 177
SC_CREATE_ORDER_RET = 178
CS_REQUEST_BOUGHT_RMB_ITEM_DATAS = 432
SC_BOUGHT_RMB_ITEM_DATAS_RET = 433
CS_REQUEST_SHOP_DATAS = 576
SC_SHOP_DATAS_RET = 577
CS_SHOP_REFRESH = 578
SC_SHOP_REFRESH_RET = 579
CS_SHOP_UPDATE = 807
SC_SHOP_UPDATE_RET = 808
NOTIFY_CHARGE_SUCCESS = 934
CS_REQUEST_SHOP_REFRESH_DATA = 1140
CS_ORDER_CHARGE = 1162
SC_ORDER_CHARGE_RET = 1163
CS_REQUEST_SHOP_CHILD_DATAS = 1214
SC_SHOP_CHILD_DATAS_RET = 1215
CS_OPEN_CLIENT_WINDOW = 831
SC_OPEN_CLIENT_WINDOW_RET = 832

RESPONSE_IDS = {
    CS_SHOP_PURCHASE: SC_SHOP_PURCHASE_RET,
    CS_USE_ITEM: SC_USE_ITEM_RET,
    CS_REQUEST_BOUGHT_ITEM_DATAS: SC_BOUGHT_ITEM_DATAS_RET,
    CS_CREATE_ORDER: SC_CREATE_ORDER_RET,
    CS_REQUEST_BOUGHT_RMB_ITEM_DATAS: SC_BOUGHT_RMB_ITEM_DATAS_RET,
    CS_REQUEST_SHOP_DATAS: SC_SHOP_DATAS_RET,
    CS_SHOP_REFRESH: SC_SHOP_REFRESH_RET,
    CS_SHOP_UPDATE: SC_SHOP_UPDATE_RET,
    CS_REQUEST_SHOP_REFRESH_DATA: SC_SHOP_REFRESH_RET,
    CS_ORDER_CHARGE: SC_ORDER_CHARGE_RET,
    CS_REQUEST_SHOP_CHILD_DATAS: SC_SHOP_CHILD_DATAS_RET,
    CS_OPEN_CLIENT_WINDOW: SC_OPEN_CLIENT_WINDOW_RET,
}

# Msg.ResourceType values used by the statically defined shop tables.
RESOURCE_DIAMOND = 0
RESOURCE_MINIDIAMOND = 1
RESOURCE_GOLD = 2
RESOURCE_ENERGY = 3
RESOURCE_ITEM = 4
RESOURCE_GOSPEL = 7
RESOURCE_HEAD_IMAGE = 14
RESOURCE_DECORATION = 17
RESOURCE_MAILBOX_SKIN = 20
RESOURCE_HEAD_FRAME = 27
RESOURCE_GOSPEL_SKIN = 28
RESOURCE_MAIN_BACK_IMAGE = 54

# Msg.ErrorCode values defined from the pinned enum.
ERROR_SUCCESS = 0
ERROR_EXCEED_MAX_BUY_TIMES = 132
ERROR_SHOP_GOODS_ID = 265
ERROR_EXCEED_ITEM_LIMIT_COUNT = 429
ERROR_CLIENT_REQUEST_PARAM = 32000
ERROR_MESSAGE_DATA_ILLEGAL = 32769
ERROR_CONFIG = 32772

UINT32_MAX = (1 << 32) - 1
UINT64_MAX = (1 << 64) - 1
SHOP_STATE_KEY = "shop_state"
SHOP_STATE_SCHEMA_VERSION = 2
LOCAL_FREE_GOODS_ID = 1_134_001
LOCAL_FREE_PRODUCT_ID = "com.bilibili.aoi.1134001"
LOCAL_FREE_PACKAGE_ID = 1_025_001
LOCAL_ORDER_REQUEST_FIELD = 127
LOCAL_MONTH_CARD_GOODS_ID = 1_130_011
# Explicit offline duration: rewards come from the source globals, while
# the previous server's renewal calendar is represented by 30 local days.
LOCAL_MONTH_CARD_SECONDS = 30 * 86400


def month_card_values(profile, now=None):
    """One authority for login, incremental properties and shop projection."""
    now = int(time.time() if now is None else now)
    state = profile.get("local_month_card", {})
    expires = _state_uint(state.get("expires_at", 0), "month card expiry", maximum=UINT64_MAX)
    remaining = max(0, expires - now)
    count = (remaining + LOCAL_MONTH_CARD_SECONDS - 1) // LOCAL_MONTH_CARD_SECONDS
    day = economy_day(now).isoformat()
    taken = bool(state.get("last_claim_day", "") >= day)
    # The source login trigger checks a nonzero MonthlyCardTime instead of
    # comparing it with now. Retain expiry in the save but project inactive
    # entitlement as zero so an expired card cannot open a fake reward UI.
    return expires if remaining else 0, count, int(taken), remaining

EXPECTED_COMMANDS = {
    CS_SHOP_PURCHASE: "CS_ShopPurchase",
    SC_SHOP_PURCHASE_RET: "SC_ShopPurchaseRet",
    CS_USE_ITEM: "CS_UseItem",
    SC_USE_ITEM_RET: "SC_UseItemRet",
    NOTIFY_UPDATE_MULTI_ITEM: "Notify_UpdateMultiItem",
    CS_REQUEST_BOUGHT_ITEM_DATAS: "CS_RequestBoughtItemDatas",
    SC_BOUGHT_ITEM_DATAS_RET: "SC_BoughtItemDatasRet",
    CS_CREATE_ORDER: "CS_CreateOrder",
    SC_CREATE_ORDER_RET: "SC_CreateOrderRet",
    CS_REQUEST_BOUGHT_RMB_ITEM_DATAS: "CS_RequestBoughtRmbitemDatas",
    SC_BOUGHT_RMB_ITEM_DATAS_RET: "SC_BoughtRmbitemDatasRet",
    CS_REQUEST_SHOP_DATAS: "CS_RequestShopDatas",
    SC_SHOP_DATAS_RET: "SC_ShopDatasRet",
    CS_SHOP_REFRESH: "CS_ShopRefresh",
    SC_SHOP_REFRESH_RET: "SC_ShopRefreshRet",
    CS_SHOP_UPDATE: "CS_ShopUpdate",
    SC_SHOP_UPDATE_RET: "SC_ShopUpdateRet",
    NOTIFY_CHARGE_SUCCESS: "Notify_ChargeSuccess",
    CS_REQUEST_SHOP_REFRESH_DATA: "CS_RequestShopRefreshData",
    CS_ORDER_CHARGE: "CS_OrderCharge",
    SC_ORDER_CHARGE_RET: "SC_OrderChargeRet",
    CS_REQUEST_SHOP_CHILD_DATAS: "CS_RequestShopChildDatas",
    SC_SHOP_CHILD_DATAS_RET: "SC_ShopChildDatasRet",
}

EXPECTED_PROTOCOL_FIELDS = {
    "CS_UseItem": {
        1: "ItemUId",
        2: "Num",
        3: "OptionalItemId",
        4: "OptionalSuitItemId",
    },
    "SC_UseItemRet": {
        1: "Errorcode",
        2: "ItemUId",
        3: "Num",
        4: "ItemId",
        5: "GeneratedItemList",
        6: "OptionalItemId",
        7: "ObjTrailList",
        8: "OptionalSuitItemId",
        9: "RuneItemUIdList",
    },
    "Notify_UpdateMultiItem": {1: "ItemList"},
    "BagItem": {1: "ItemUId", 2: "ItemId", 3: "Count", 4: "Para"},
    "RewardItem": {
        1: "ResType",
        2: "ItemId",
        3: "Count",
        4: "Para",
        5: "Position",
        6: "Quality",
        7: "Value",
    },
    "CS_ShopPurchase": {
        1: "GoodsId",
        2: "Count",
        3: "IsActivity",
        4: "MainActivityId",
        5: "SubActivityId",
    },
    "SC_ShopPurchaseRet": {
        1: "Errorcode",
        2: "GoodsId",
        3: "Count",
        4: "McExpireTime",
        5: "TotalCount",
        6: "IsActivity",
        7: "MainActivityId",
        8: "SubActivityId",
        9: "IsNewGospel",
    },
    "CS_CreateOrder": {1: "RoleId", 2: "ProductId", 3: "IsSandbox"},
    "SC_CreateOrderRet": {
        1: "Errorcode",
        2: "RoleId",
        3: "Money",
        4: "GameMoney",
        5: "ProductId",
        6: "OutTradeNo",
        7: "OrderSign",
        8: "NotifyUrl",
        9: "ExtensionInfo",
    },
    "CS_RequestBoughtItemDatas": {},
    "SC_BoughtItemDatasRet": {1: "ItemList"},
    "CS_RequestBoughtRMBItemDatas": {},
    "SC_BoughtRMBItemDatasRet": {1: "GoodsList"},
    "CS_RequestShopDatas": {},
    "SC_ShopDatasRet": {
        1: "Errorcode",
        2: "ShopDataList",
        3: "RecycleShopDataList",
        4: "ItemList",
        5: "GiftMCShopDataList",
        6: "CrystalShopDataList",
        7: "BoughtRMBItemList",
        8: "ShopParentList",
    },
    "ShopData": {
        1: "ShopId",
        2: "ShopNextRefreshTime",
        3: "ShopRefreshCount",
        7: "GoodsList",
    },
    "GoodsItem": {
        1: "GoodsId",
        2: "NextRefreshTime",
        3: "DiscountRefreshTime",
    },
    "BoughtItem": {1: "ItemId", 2: "Count"},
    "BoughtRMBItem": {1: "GoodsId", 2: "Count"},
    "GiftMCShopData": {
        1: "ShopId",
        2: "GiftGoodsList",
        3: "McGoodsItemList",
    },
    "GiftGoodsItem": {
        1: "GoodsId",
        2: "NextRefreshTime",
        3: "FreeFirst",
        4: "Status",
        5: "RemainderTakeCount",
        6: "DiscountRefreshTime",
    },
    "MCGoodsItem": {
        1: "GoodsId",
        3: "RemainderTime",
        4: "ExtraReward",
    },
    "CrystalShopData": {1: "ShopId", 2: "CrystalGoodsList"},
    "CrystalShopItem": {1: "GoodsId", 2: "IsFirstPurchase"},
    "CS_ShopRefresh": {1: "ShopId"},
    "CS_RequestShopRefreshData": {1: "ShopId"},
    "SC_ShopRefreshRet": {
        1: "Errorcode",
        2: "ShopId",
        3: "RefreshCount",
        4: "ItemList",
        5: "GoodsList",
    },
    "CS_ShopUpdate": {},
    "SC_ShopUpdateRet": {
        1: "Errorcode",
        2: "ShopDataList",
        3: "RecycleShopDataList",
        4: "GiftMCShopDataList",
        5: "ItemList",
    },
    "CS_RequestShopChildDatas": {1: "ShopChildId"},
    "SC_ShopChildDatasRet": {
        1: "Errorcode",
        2: "ShopChildId",
        3: "ShopData",
        4: "RecycleShopData",
        5: "GiftMCShopData",
        6: "CrystalShopData",
        7: "ItemList",
        8: "BoughtRMBItemList",
    },
    "Notify_ChargeSuccess": {
        1: "ProductId",
        2: "OrderNo",
        3: "OutTradeNo",
        4: "RewardList",
        5: "IsTakeMCExtraReward",
    },
    "CS_OrderCharge": {1: "OrderNo", 2: "ProductID"},
    "SC_OrderChargeRet": {1: "Errorcode"},
}


class ShopConfigError(ValueError):
    """Raised when static shop validation is missing or internally inconsistent."""


class ProtobufDecodeError(ValueError):
    """Raised when a shop request is not the defined protobuf shape."""


class ShopApplicationError(ValueError):
    """Atomic reward/state failure carrying the runtime ErrorCode."""

    def __init__(self, error_code: int, message: str) -> None:
        super().__init__(message)
        self.error_code = int(error_code)


@dataclass(frozen=True)
class Reward:
    res_type: int
    item_id: int
    count: int

    def scaled(self, units: int) -> "Reward":
        total = self.count * units
        if total <= 0 or total > UINT32_MAX:
            raise ShopApplicationError(
                ERROR_CLIENT_REQUEST_PARAM,
                "scaled shop reward count is outside uint32",
            )
        return Reward(self.res_type, self.item_id, total)

    def as_dict(self) -> Dict[str, int]:
        return {
            "res_type": self.res_type,
            "item_id": self.item_id,
            "count": self.count,
        }


@dataclass(frozen=True)
class GoodsDefinition:
    goods_id: int
    shop_id: int
    rewards: Tuple[Reward, ...]
    costs: Tuple[Reward, ...]
    limit: int
    limit_type: int
    index: int
    is_activity: bool = False
    discount: int = 0


@dataclass(frozen=True)
class RmbGoodsDefinition:
    goods_id: int
    product_id: str
    shop_id: int
    shop_type: int
    price_type: int
    static_price: int
    rewards: Tuple[Reward, ...]
    first_rewards: Tuple[Reward, ...]
    recurring_rewards: Tuple[Reward, ...]
    limit: int
    limit_type: int
    index: int

    @property
    def reward_proven(self) -> bool:
        return bool(self.rewards or self.first_rewards or self.recurring_rewards)


@dataclass(frozen=True)
class Result:
    """One response plus a pure top-level account patch."""

    response_id: int
    body: bytes
    changes: Dict[str, Any] = field(default_factory=dict)
    error_code: int = ERROR_SUCCESS
    rewards: Tuple[Reward, ...] = ()
    grants: Tuple[Reward, ...] = ()
    post_notifications: Tuple[Tuple[int, bytes], ...] = ()
    notifications: Tuple[Tuple[int, bytes], ...] = ()
    goods_id: int = 0
    reason: str = ""

    @property
    def command_id(self) -> int:
        return self.response_id

    @property
    def ok(self) -> bool:
        return self.error_code == ERROR_SUCCESS


@dataclass(frozen=True)
class _PurchaseRequest:
    goods_id: int
    count: int
    is_activity: bool
    main_activity_id: int
    subactivity_id: int


@dataclass(frozen=True)
class _CreateOrderRequest:
    role_id: int
    product_id: str
    is_sandbox: int
    local_request_id: str = ""


@dataclass(frozen=True)
class _OrderChargeRequest:
    order_no: str
    product_id: str


@dataclass(frozen=True)
class _UseItemRequest:
    item_uid: int
    count: int
    optional_item_id: int
    optional_suit_item_id: int


def _as_static_int(value: Any, context: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ShopConfigError(f"{context}: expected integer >= {minimum}")
    if value > UINT32_MAX:
        raise ShopConfigError(f"{context}: value exceeds uint32")
    return value


def _state_uint(value: Any, context: str, *, maximum: int = UINT32_MAX) -> int:
    if isinstance(value, bool):
        raise ShopApplicationError(ERROR_CONFIG, f"{context} is not an integer")
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ShopApplicationError(
            ERROR_CONFIG, f"{context} is not an integer"
        ) from exc
    if result < 0 or result > maximum:
        raise ShopApplicationError(ERROR_CONFIG, f"{context} is outside 0..{maximum}")
    return result


def _load_generated_records(
    content_dir: Path, table_name: str
) -> Tuple[Mapping[str, Any], ...]:
    path = Path(content_dir) / f"{table_name}.json"
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ShopConfigError(f"cannot load {path}") from exc
    records = document.get("records") if isinstance(document, Mapping) else None
    if document.get("table") != table_name or not isinstance(records, list):
        raise ShopConfigError(f"{path}: invalid generated table document")
    if document.get("count") != len(records):
        raise ShopConfigError(f"{path}: record count mismatch")
    if not all(isinstance(row, Mapping) for row in records):
        raise ShopConfigError(f"{path}: every record must be an object")
    return tuple(records)


@lru_cache(maxsize=1)
def _generator_module() -> ModuleType:
    """Load the existing verified bundle reader without generating files."""

    name = "_offline_shop_content_catalog_reader"
    existing = sys.modules.get(name)
    if isinstance(existing, ModuleType):
        return existing
    spec = importlib.util.spec_from_file_location(name, CATALOG_GENERATOR)
    if spec is None or spec.loader is None:
        raise ShopConfigError(f"cannot load {CATALOG_GENERATOR}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _raw_schemas(generator: ModuleType) -> Mapping[str, Any]:
    """Pinned layouts derived from the same assembly during the audit."""

    integer = generator.I
    string = generator.S
    table = generator.TableSchema
    return {
        "a_goods": table(
            136,
            (
                integer("id", 0),
                integer("shop_child", 4),
                string("item_id", 8),
                string("goodsname", 16),
                string("price_num", 24),
                string("label_type", 32),
                integer("discount", 40),
                integer("discount_price", 44),
                string("discount_time", 48),
                integer("times_limit_type", 56),
                integer("exchange_priority", 60),
                integer("times", 64),
                integer("limit_level", 68),
                string("time_slot", 72),
                string("timeweek_slot", 80),
                integer("index", 88),
                integer("weight", 92),
                integer("join_way", 96),
                integer("force_exist", 100),
                integer("weight_group", 104),
                integer("secondary_weight", 108),
                integer("group_counts", 112),
                integer("fast_purch", 116),
                integer("buy_condition", 120),
                string("base_image", 124),
                integer("display_type", 132),
            ),
        ),
        "a_shop_child": table(
            108,
            (
                integer("id", 0),
                string("shop_child", 4),
                string("shop_name", 12),
                string("shop_name_eng", 20),
                string("auto_refresh_icon", 28),
                string("start_time", 36),
                integer("shop_child_hide", 44),
                integer("auto_refresh", 48),
                integer("auto_refresh_num", 52),
                integer("goods_num", 56),
                integer("refresh_time", 60),
                string("refresh_price", 64),
                string("refresh_resource", 72),
                integer("order", 80),
                string("shop_icon", 84),
                integer("shop_type", 92),
                integer("module_type", 96),
                string("shop_money_type", 100),
            ),
        ),
        "a_shop_parent": table(
            40,
            (
                integer("id", 0),
                string("shop_parent", 4),
                string("shop_child", 12),
                integer("activity_switch", 20),
                integer("tab_order", 24),
                string("tab_icon", 28),
                integer("module_type", 36),
            ),
        ),
        "a_playeravatar": table(
            68,
            (
                integer("id", 0),
                integer("profile_show", 4),
                integer("condition_type", 8),
                integer("condition_id", 12),
                string("avatar_icon", 16),
                string("avatar_name", 24),
                string("avatar_tip", 32),
                string("avatar_unlock", 40),
                integer("quality", 48),
                integer("time_limit", 52),
                integer("icon", 56),
                string("get_tip", 60),
            ),
        ),
        "a_headframe": table(
            72,
            (
                integer("id", 0),
                integer("unlock_ways", 4),
                integer("group", 8),
                integer("lv", 12),
                string("name", 16),
                integer("type", 24),
                string("effect_path_file", 28),
                string("icon", 36),
                integer("quality", 44),
                string("describe", 48),
                string("unlock_tips", 56),
                integer("need_exp", 64),
                integer("repeat_exp", 68),
            ),
        ),
        "a_mailboxskin": table(
            60,
            (
                integer("id", 0),
                string("skin_name", 4),
                string("name", 12),
                integer("icon", 20),
                integer("quality", 24),
                string("function", 28),
                string("describe", 36),
                string("skin_describe", 44),
                integer("resource", 52),
                integer("unlock_way", 56),
            ),
        ),
        "a_shop_rmb": table(
            184,
            (
                integer("id", 0),
                string("product_id", 4),
                string("discount_product_id", 12),
                integer("index", 20),
                integer("shop_child", 24),
                integer("shop_type", 28),
                integer("goods_type", 32),
                integer("price_type", 36),
                integer("price", 40),
                integer("voucher_enable", 44),
                integer("region_type", 48),
                string("voucher_price", 52),
                integer("times_limit_type", 60),
                integer("times", 64),
                integer("discount", 68),
                integer("discount_price", 72),
                string("discount_time", 76),
                string("type_title", 84),
                string("gift_desc", 92),
                string("use_illust", 100),
                integer("unlock_grade", 108),
                integer("unlock_way", 112),
                integer("unlock_group", 116),
                string("item_id", 120),
                string("first_reward", 128),
                string("reward", 136),
                string("time_sale", 144),
                integer("linkId", 152),
                integer("package_last_day", 156),
                string("timeweek_slot", 160),
                string("free_slot", 168),
                string("gift_tag", 176),
            ),
        ),
    }


@lru_cache(maxsize=4)
def _load_raw_tables_cached(
    data_root_text: str, assembly_text: str
) -> Tuple[Mapping[str, Tuple[Mapping[str, Any], ...]], Mapping[str, str]]:
    data_root = Path(data_root_text)
    assembly = Path(assembly_text)
    framework_path = data_root / "FrameworkList.dat"
    bundle_path = data_root / "Configs" / "TableConfig.dat"
    for path in (assembly, framework_path, bundle_path):
        if not path.is_file():
            raise ShopConfigError(f"missing static shop source: {path}")

    assembly_bytes = assembly.read_bytes()
    assembly_sha256 = hashlib.sha256(assembly_bytes).hexdigest()

    generator = _generator_module()
    schemas = _raw_schemas(generator)
    generator._validate_schemas(schemas)
    framework_bytes = framework_path.read_bytes()
    record = generator.locate_local_record(framework_bytes, generator.TARGET_RESOURCE)
    stored_bundle = bundle_path.read_bytes()
    if len(stored_bundle) != record.length:
        raise ShopConfigError("TableConfig length disagrees with FrameworkList")
    bundle = generator.xor_prefix(stored_bundle, record.hash_code)
    expected_crc = record.hash_code & UINT32_MAX
    actual_crc = zlib.crc32(bundle) & UINT32_MAX
    if actual_crc != expected_crc:
        raise ShopConfigError(
            f"TableConfig CRC32 is 0x{actual_crc:08x}, expected 0x{expected_crc:08x}"
        )
    assets, _, _ = generator._extract_target_assets(bundle, schemas)
    documents = {
        name: generator._table_document(name, schema, assets[name])
        for name, schema in schemas.items()
    }
    records = {name: tuple(document["records"]) for name, document in documents.items()}
    evidence = {
        "assembly_sha256": assembly_sha256,
        "framework_sha256": hashlib.sha256(framework_bytes).hexdigest(),
        "stored_bundle_sha256": hashlib.sha256(stored_bundle).hexdigest(),
        "decrypted_bundle_sha256": hashlib.sha256(bundle).hexdigest(),
        "decrypted_bundle_crc32": f"0x{actual_crc:08x}",
    }
    return records, evidence


def _load_raw_tables(
    data_root: Path, assembly: Path
) -> Tuple[Mapping[str, Tuple[Mapping[str, Any], ...]], Mapping[str, str]]:
    return _load_raw_tables_cached(
        str(Path(data_root).expanduser().resolve()),
        str(Path(assembly).expanduser().resolve()),
    )


def _load_precomputed_raw_tables(
    path: Path,
) -> Tuple[Mapping[str, Tuple[Mapping[str, Any], ...]], Mapping[str, str]]:
    """Load the verified shop projection without importing UnityPy.

    The Android embedded build cannot use the desktop-only UnityPy/dnfile
    decoder at runtime.  ``shop_raw_tables.json`` is generated once from the
    pinned TableConfig bundle during the build and retains the exact decoded
    rows plus source digests.  Desktop callers continue to use the source
    decoder when the sidecar is absent.
    """

    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ShopConfigError(f"cannot load precomputed shop tables {path}") from exc
    if not isinstance(document, Mapping):
        raise ShopConfigError(f"{path}: precomputed shop tables must be an object")
    raw = document.get("tables")
    evidence = document.get("evidence")
    if not isinstance(raw, Mapping) or not isinstance(evidence, Mapping):
        raise ShopConfigError(f"{path}: missing tables/evidence")
    result: Dict[str, Tuple[Mapping[str, Any], ...]] = {}
    expected_names = (
        "a_goods",
        "a_shop_child",
        "a_shop_parent",
        "a_playeravatar",
        "a_headframe",
        "a_mailboxskin",
        "a_shop_rmb",
    )
    for name in expected_names:
        rows = raw.get(name)
        if not isinstance(rows, list) or not all(
            isinstance(row, Mapping) for row in rows
        ):
            raise ShopConfigError(f"{path}: invalid rows for {name}")
        result[name] = tuple(rows)
    return result, dict(evidence)


def _parse_rewards(value: Any, context: str) -> Tuple[Reward, ...]:
    if value in (None, ""):
        return ()
    try:
        decoded = json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError) as exc:
        raise ShopConfigError(f"{context}: invalid reward JSON") from exc
    if not isinstance(decoded, list):
        raise ShopConfigError(f"{context}: reward must be a list")
    rewards: List[Reward] = []
    for index, raw in enumerate(decoded):
        if not isinstance(raw, Mapping):
            raise ShopConfigError(f"{context}[{index}]: reward must be an object")
        reward = Reward(
            _as_static_int(raw.get("type"), f"{context}[{index}].type"),
            _as_static_int(raw.get("id"), f"{context}[{index}].id"),
            _as_static_int(raw.get("num"), f"{context}[{index}].num", minimum=1),
        )
        rewards.append(reward)
    return tuple(rewards)


def _rows_by_id(
    rows: Iterable[Mapping[str, Any]], table_name: str
) -> Dict[int, Mapping[str, Any]]:
    result: Dict[int, Mapping[str, Any]] = {}
    for row in rows:
        row_id = _as_static_int(row.get("id"), f"{table_name}.id", minimum=1)
        if row_id in result:
            raise ShopConfigError(f"{table_name}: duplicate id {row_id}")
        result[row_id] = row
    return result


class ShopCatalog:
    """Validated projection of only the static tables used by this layer."""

    def __init__(
        self,
        content_dir: Path = DEFAULT_CONTENT_DIR,
        data_root: Path = DEFAULT_DATA_ROOT,
        assembly: Path = DEFAULT_ASSEMBLY,
        raw_tables_path: Optional[Path] = None,
    ) -> None:
        self.content_dir = Path(content_dir).expanduser().resolve()
        sidecar = (
            Path(raw_tables_path).expanduser().resolve()
            if raw_tables_path is not None
            else self.content_dir / "shop_raw_tables.json"
        )
        if sidecar.is_file():
            raw, self.source_evidence = _load_precomputed_raw_tables(sidecar)
        else:
            raw, self.source_evidence = _load_raw_tables(data_root, assembly)

        item_rows = _load_generated_records(self.content_dir, "a_item")
        gospel_rows = _load_generated_records(self.content_dir, "a_gospel")
        skin_rows = _load_generated_records(self.content_dir, "a_gospel_skin")
        background_rows = _load_generated_records(self.content_dir, "a_background")
        decoration_rows = _load_generated_records(self.content_dir, "a_decoration")
        activity_rows = _load_generated_records(self.content_dir, "a_goods_activity")

        self.items = _rows_by_id(item_rows, "a_item")
        self.globals = _rows_by_id(_load_generated_records(self.content_dir, "a_global"), "a_global")
        self.gospels = _rows_by_id(gospel_rows, "a_gospel")
        self.gospel_skins = _rows_by_id(skin_rows, "a_gospel_skin")
        self.backgrounds = _rows_by_id(background_rows, "a_background")
        self.decorations = _rows_by_id(decoration_rows, "a_decoration")
        self.player_avatars = _rows_by_id(raw["a_playeravatar"], "a_playeravatar")
        self.head_frames = _rows_by_id(raw["a_headframe"], "a_headframe")
        self.mailbox_skins = _rows_by_id(raw["a_mailboxskin"], "a_mailboxskin")
        self.shop_children = _rows_by_id(raw["a_shop_child"], "a_shop_child")
        self.shop_parents = _rows_by_id(raw["a_shop_parent"], "a_shop_parent")

        self.item_max_stacks: Dict[int, int] = {}
        for item_id, row in self.items.items():
            maximum = _as_static_int(
                row.get("max_stack_num"), f"a_item[{item_id}].max_stack_num"
            )
            self.item_max_stacks[item_id] = maximum

        self.gospel_repeat_rewards: Dict[int, Tuple[Reward, ...]] = {}
        for gospel_id, row in self.gospels.items():
            rewards = _parse_rewards(
                row.get("repeat_item"), f"a_gospel[{gospel_id}].repeat_item"
            )
            for reward in rewards:
                if reward.res_type != RESOURCE_ITEM or reward.item_id not in self.items:
                    raise ShopConfigError(
                        f"a_gospel[{gospel_id}].repeat_item is not a proved item"
                    )
            self.gospel_repeat_rewards[gospel_id] = rewards

        ordinary = self._goods_definitions(raw["a_goods"], is_activity=False)
        activity = self._goods_definitions(activity_rows, is_activity=True)
        self.ordinary_goods = tuple(ordinary)
        self.activity_goods = tuple(activity)
        self.ordinary_by_id = {row.goods_id: row for row in ordinary}
        self.activity_by_id = {row.goods_id: row for row in activity}
        if len(self.ordinary_by_id) != len(ordinary):
            raise ShopConfigError("a_goods has duplicate IDs")
        if len(self.activity_by_id) != len(activity):
            raise ShopConfigError("a_goods_activity has duplicate IDs")
        overlap = self.ordinary_by_id.keys() & self.activity_by_id.keys()
        if overlap:
            raise ShopConfigError(
                f"ordinary/activity goods IDs overlap: {sorted(overlap)}"
            )

        self.ordinary_by_shop = self._group_goods(ordinary)
        self.activity_by_shop = self._group_goods(activity)
        unknown_shops = set(self.ordinary_by_shop) - set(self.shop_children)
        if unknown_shops:
            raise ShopConfigError(
                f"a_goods references missing a_shop_child rows: {sorted(unknown_shops)}"
            )

        rmb_goods = self._rmb_definitions(raw["a_shop_rmb"])
        self.rmb_goods = tuple(rmb_goods)
        self.rmb_by_id = {row.goods_id: row for row in rmb_goods}
        self.rmb_by_product_id = {row.product_id: row for row in rmb_goods}
        if len(self.rmb_by_id) != len(rmb_goods):
            raise ShopConfigError("a_shop_rmb has duplicate goods IDs")
        if len(self.rmb_by_product_id) != len(rmb_goods):
            raise ShopConfigError("a_shop_rmb has duplicate product IDs")
        self.rmb_by_shop: Dict[int, Tuple[RmbGoodsDefinition, ...]] = {
            shop_id: tuple(sorted(rows, key=lambda row: (row.index, row.goods_id)))
            for shop_id, rows in self._group_rmb(rmb_goods).items()
        }
        self.unproved_rmb_reward_ids = tuple(
            row.goods_id for row in rmb_goods if not row.reward_proven
        )
        self.parent_ids = tuple(
            row_id
            for row_id, row in sorted(
                self.shop_parents.items(),
                key=lambda item: (
                    _as_static_int(
                        item[1].get("tab_order"),
                        f"a_shop_parent[{item[0]}].tab_order",
                    ),
                    item[0],
                ),
            )
        )
        self.closure_errors: Tuple[str, ...] = ()

    def _goods_definitions(
        self, rows: Iterable[Mapping[str, Any]], *, is_activity: bool
    ) -> List[GoodsDefinition]:
        table_name = "a_goods_activity" if is_activity else "a_goods"
        result = []
        for row in rows:
            goods_id = _as_static_int(row.get("id"), f"{table_name}.id", minimum=1)
            rewards = _parse_rewards(
                row.get("item_id"), f"{table_name}[{goods_id}].item_id"
            )
            costs = _parse_rewards(
                row.get("price_num"), f"{table_name}[{goods_id}].price_num"
            )
            if not rewards:
                raise ShopConfigError(f"{table_name}[{goods_id}] has no reward")
            self._validate_rewards(rewards, f"{table_name}[{goods_id}].item_id")
            self._validate_costs(costs, f"{table_name}[{goods_id}].price_num")
            discount = _as_static_int(row.get("discount", 0), f"{table_name}[{goods_id}].discount")
            if not 0 <= discount < 100:
                raise ShopConfigError(f"{table_name}[{goods_id}]: unsupported discount {discount}")
            result.append(
                GoodsDefinition(
                    goods_id=goods_id,
                    shop_id=_as_static_int(
                        row.get("shop_child"),
                        f"{table_name}[{goods_id}].shop_child",
                        minimum=1,
                    ),
                    rewards=rewards,
                    costs=costs,
                    limit=_as_static_int(
                        row.get("times"), f"{table_name}[{goods_id}].times"
                    ),
                    limit_type=_as_static_int(
                        row.get("times_limit_type"),
                        f"{table_name}[{goods_id}].times_limit_type",
                    ),
                    index=_as_static_int(
                        row.get("index"), f"{table_name}[{goods_id}].index"
                    ),
                    is_activity=is_activity,
                    discount=discount,
                )
            )
        return result

    def _rmb_definitions(
        self, rows: Iterable[Mapping[str, Any]]
    ) -> List[RmbGoodsDefinition]:
        result = []
        for row in rows:
            goods_id = _as_static_int(row.get("id"), "a_shop_rmb.id", minimum=1)
            product_id = row.get("product_id")
            if not isinstance(product_id, str) or not product_id:
                raise ShopConfigError(f"a_shop_rmb[{goods_id}].product_id is empty")
            rewards = _parse_rewards(
                row.get("item_id"), f"a_shop_rmb[{goods_id}].item_id"
            )
            first_rewards = _parse_rewards(
                row.get("first_reward"),
                f"a_shop_rmb[{goods_id}].first_reward",
            )
            recurring_rewards = _parse_rewards(
                row.get("reward"), f"a_shop_rmb[{goods_id}].reward"
            )
            for field_name, values in (
                ("item_id", rewards),
                ("first_reward", first_rewards),
                ("reward", recurring_rewards),
            ):
                self._validate_rewards(
                    values,
                    f"a_shop_rmb[{goods_id}].{field_name}",
                    allow_package=True,
                )
            result.append(
                RmbGoodsDefinition(
                    goods_id=goods_id,
                    product_id=product_id,
                    shop_id=_as_static_int(
                        row.get("shop_child"),
                        f"a_shop_rmb[{goods_id}].shop_child",
                        minimum=1,
                    ),
                    shop_type=_as_static_int(
                        row.get("shop_type"),
                        f"a_shop_rmb[{goods_id}].shop_type",
                    ),
                    price_type=_as_static_int(
                        row.get("price_type"),
                        f"a_shop_rmb[{goods_id}].price_type",
                    ),
                    static_price=_as_static_int(
                        row.get("price"), f"a_shop_rmb[{goods_id}].price"
                    ),
                    rewards=rewards,
                    first_rewards=first_rewards,
                    recurring_rewards=recurring_rewards,
                    limit=_as_static_int(
                        row.get("times"), f"a_shop_rmb[{goods_id}].times"
                    ),
                    limit_type=_as_static_int(
                        row.get("times_limit_type"),
                        f"a_shop_rmb[{goods_id}].times_limit_type",
                    ),
                    index=_as_static_int(
                        row.get("index"), f"a_shop_rmb[{goods_id}].index"
                    ),
                )
            )
        return result

    def _validate_costs(self, costs: Sequence[Reward], context: str) -> None:
        if not costs:
            raise ShopConfigError(f"{context}: ordinary goods must have a static cost")
        for reward in costs:
            if reward.res_type == RESOURCE_ITEM:
                if reward.item_id not in self.items:
                    raise ShopConfigError(
                        f"{context}: cost item {reward.item_id} is absent from a_item"
                    )
            # Some defined Diamond costs retain the runtime's concrete
            # currency item identity (for example 1000001) instead of zero.
            # The economy layer validates these aliases against their exact
            # currency type before deducting the canonical scalar balance.

    def _validate_rewards(
        self,
        rewards: Sequence[Reward],
        context: str,
        *,
        allow_package: bool = False,
    ) -> None:
        for reward in rewards:
            if reward.res_type in {
                RESOURCE_DIAMOND,
                RESOURCE_MINIDIAMOND,
                RESOURCE_GOLD,
                RESOURCE_ENERGY,
            }:
                if reward.item_id != 0:
                    raise ShopConfigError(
                        f"{context}: currency has an unproved nonzero item ID"
                    )
            elif reward.res_type == RESOURCE_ITEM:
                if reward.item_id not in self.items:
                    raise ShopConfigError(
                        f"{context}: item {reward.item_id} is absent from a_item"
                    )
                maximum = self.item_max_stacks[reward.item_id]
                if maximum == 0 and not allow_package:
                    raise ShopConfigError(
                        f"{context}: item {reward.item_id} is not a storable a_item"
                    )
            elif reward.res_type == RESOURCE_GOSPEL:
                if reward.item_id not in self.gospels:
                    raise ShopConfigError(
                        f"{context}: Gospel {reward.item_id} is absent from a_gospel"
                    )
            elif reward.res_type == RESOURCE_GOSPEL_SKIN:
                if reward.item_id not in self.gospel_skins:
                    raise ShopConfigError(
                        f"{context}: skin {reward.item_id} is absent from a_gospel_skin"
                    )
            elif reward.res_type == RESOURCE_MAIN_BACK_IMAGE:
                if reward.item_id not in self.backgrounds:
                    raise ShopConfigError(
                        f"{context}: image {reward.item_id} is absent from a_background"
                    )
            elif reward.res_type == RESOURCE_DECORATION:
                if reward.item_id not in self.decorations:
                    raise ShopConfigError(
                        f"{context}: decoration {reward.item_id} is absent from a_decoration"
                    )
            elif reward.res_type == RESOURCE_HEAD_IMAGE:
                if reward.item_id not in self.player_avatars:
                    raise ShopConfigError(
                        f"{context}: avatar {reward.item_id} is absent from a_playeravatar"
                    )
            elif reward.res_type == RESOURCE_MAILBOX_SKIN:
                if reward.item_id not in self.mailbox_skins:
                    raise ShopConfigError(
                        f"{context}: skin {reward.item_id} is absent from a_mailboxskin"
                    )
            elif reward.res_type == RESOURCE_HEAD_FRAME:
                if reward.item_id not in self.head_frames:
                    raise ShopConfigError(
                        f"{context}: frame {reward.item_id} is absent from a_headframe"
                    )
            else:
                raise ShopConfigError(
                    f"{context}: unsupported ResourceType {reward.res_type}"
                )

    @staticmethod
    def _group_goods(
        goods: Iterable[GoodsDefinition],
    ) -> Dict[int, Tuple[GoodsDefinition, ...]]:
        grouped: Dict[int, List[GoodsDefinition]] = defaultdict(list)
        for row in goods:
            grouped[row.shop_id].append(row)
        return {
            shop_id: tuple(sorted(rows, key=lambda row: (row.index, row.goods_id)))
            for shop_id, rows in grouped.items()
        }

    @staticmethod
    def _group_rmb(
        goods: Iterable[RmbGoodsDefinition],
    ) -> Dict[int, List[RmbGoodsDefinition]]:
        grouped: Dict[int, List[RmbGoodsDefinition]] = defaultdict(list)
        for row in goods:
            grouped[row.shop_id].append(row)
        return dict(grouped)


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


def _parse_uint_fields(data: bytes, allowed: Iterable[int]) -> Dict[int, int]:
    allowed_set = set(allowed)
    values: Dict[int, int] = {}
    for number, wire_type, value in iter_wire_fields(data):
        if number not in allowed_set:
            raise ProtobufDecodeError(f"unexpected protobuf field {number}")
        if wire_type != 0 or not isinstance(value, int):
            raise ProtobufDecodeError(f"field {number} must be a varint")
        if number in values:
            raise ProtobufDecodeError(f"field {number} was repeated")
        if value > UINT32_MAX:
            raise ProtobufDecodeError(f"field {number} exceeds uint32")
        values[number] = value
    return values


def _parse_purchase(data: bytes) -> _PurchaseRequest:
    values = _parse_uint_fields(data, range(1, 6))
    is_activity = values.get(3, 0)
    if is_activity not in {0, 1}:
        raise ProtobufDecodeError("CS_ShopPurchase.IsActivity must be bool")
    return _PurchaseRequest(
        goods_id=values.get(1, 0),
        count=values.get(2, 0),
        is_activity=bool(is_activity),
        main_activity_id=values.get(4, 0),
        subactivity_id=values.get(5, 0),
    )


def _parse_mixed_fields(
    data: bytes, field_types: Mapping[int, str]
) -> Dict[int, object]:
    values: Dict[int, object] = {}
    for number, wire_type, value in iter_wire_fields(data):
        expected = field_types.get(number)
        if expected is None:
            raise ProtobufDecodeError(f"unexpected protobuf field {number}")
        if number in values:
            raise ProtobufDecodeError(f"field {number} was repeated")
        if expected == "string":
            if wire_type != 2 or not isinstance(value, bytes):
                raise ProtobufDecodeError(f"field {number} must be a string")
            if len(value) > 1024:
                raise ProtobufDecodeError(f"field {number} string is too long")
            try:
                decoded = value.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ProtobufDecodeError(f"field {number} is not valid UTF-8") from exc
            if "\x00" in decoded:
                raise ProtobufDecodeError(f"field {number} contains NUL")
            values[number] = decoded
            continue
        if wire_type != 0 or not isinstance(value, int):
            raise ProtobufDecodeError(f"field {number} must be a varint")
        maximum = UINT64_MAX if expected == "uint64" else UINT32_MAX
        if value > maximum:
            raise ProtobufDecodeError(f"field {number} exceeds {expected}")
        values[number] = value
    return values


def _parse_create_order(data: bytes) -> _CreateOrderRequest:
    values = _parse_mixed_fields(
        data,
        {1: "uint64", 2: "string", 3: "uint32", LOCAL_ORDER_REQUEST_FIELD: "string"},
    )
    return _CreateOrderRequest(
        role_id=int(values.get(1, 0)),
        product_id=str(values.get(2, "")),
        is_sandbox=int(values.get(3, 0)),
        local_request_id=str(values.get(LOCAL_ORDER_REQUEST_FIELD, "")),
    )


def _parse_order_charge(data: bytes) -> _OrderChargeRequest:
    values = _parse_mixed_fields(data, {1: "string", 2: "string"})
    return _OrderChargeRequest(
        order_no=str(values.get(1, "")),
        product_id=str(values.get(2, "")),
    )


def _parse_use_item(data: bytes) -> _UseItemRequest:
    values = _parse_mixed_fields(
        data,
        {1: "uint64", 2: "uint32", 3: "uint32", 4: "uint32"},
    )
    return _UseItemRequest(
        item_uid=int(values.get(1, 0)),
        count=int(values.get(2, 0)),
        optional_item_id=int(values.get(3, 0)),
        optional_suit_item_id=int(values.get(4, 0)),
    )


def _empty_request(data: bytes, message_name: str) -> None:
    if data:
        raise ProtobufDecodeError(f"{message_name} must have an empty body")


class ShopContentLayer:
    """Schema-checked shop queries and one-transaction purchases."""

    def __init__(
        self,
        catalog: Optional[ShopCatalog] = None,
        codec: Optional[ProtocolMessageCodec] = None,
        protocol_catalog: Path = DEFAULT_PROTOCOL_CATALOG,
        today_provider: Optional[Callable[[], date]] = None,
        normal_economy: bool = False,
        now_provider: Optional[Callable[[], float]] = None,
    ) -> None:
        self.catalog = catalog or ShopCatalog()
        self.gospel_factory = GospelFactory(self.catalog.content_dir)
        self.codec = codec or ProtocolMessageCodec(protocol_catalog)
        self.protocol_catalog = Path(protocol_catalog).expanduser().resolve()
        self.normal_economy = bool(normal_economy)
        self._now = now_provider or time.time
        self._today_provider = today_provider or (
            (lambda: datetime.now(timezone(timedelta(hours=8))).date())
            if self.normal_economy else date.today
        )
        self._validate_protocol()
        self.item_use_layer = None
        sold_skins = set()
        for goods in (*self.catalog.ordinary_goods, *self.catalog.activity_goods, *self.catalog.rmb_goods):
            for reward in goods.rewards:
                if reward.res_type == RESOURCE_GOSPEL_SKIN:
                    sold_skins.add(reward.item_id)
                if reward.res_type == RESOURCE_ITEM:
                    item = self.catalog.items.get(reward.item_id, {})
                    if item.get("type") == 5:
                        sold_skins.add(int(item.get("correlation", 0)))
        self.unlisted_skins = frozenset(skin_id for skin_id, skin in self.catalog.gospel_skins.items()
            if int(skin.get("get_method_type", -1)) == 0 and skin_id not in sold_skins)

    def _validate_protocol(self) -> None:
        try:
            document = json.loads(self.protocol_catalog.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ShopConfigError(f"cannot load {self.protocol_catalog}") from exc
        commands = {int(row["id"]): row for row in document.get("commands", [])}
        for command_id, expected_name in EXPECTED_COMMANDS.items():
            names = commands.get(command_id, {}).get("names", [])
            if expected_name not in names:
                raise ShopConfigError(
                    f"protocol command {command_id} is not {expected_name}"
                )
        pairs = document.get("request_to_response", {})
        for request_id, response_id in RESPONSE_IDS.items():
            if int(pairs.get(str(request_id), -1)) != response_id:
                raise ShopConfigError(
                    f"protocol request {request_id} does not map to {response_id}"
                )
        for message_name, expected in EXPECTED_PROTOCOL_FIELDS.items():
            schema = self.codec.schema(message_name)
            actual = {
                int(field["number"]): str(field["name"])
                for field in schema.get("fields", [])
            }
            if actual != expected:
                raise ShopConfigError(
                    f"protocol fields for {message_name} changed: {actual}"
                )

    @staticmethod
    def _count_map(state: Mapping[str, Any], key: str) -> Dict[str, int]:
        value = state.get(key, {})
        if value is None:
            return {}
        if not isinstance(value, Mapping):
            raise ShopApplicationError(ERROR_CONFIG, f"shop_state.{key} is not a map")
        result: Dict[str, int] = {}
        for raw_id, raw_count in value.items():
            try:
                goods_id = int(raw_id)
            except (TypeError, ValueError, OverflowError) as exc:
                raise ShopApplicationError(
                    ERROR_CONFIG, f"shop_state.{key} has invalid goods ID"
                ) from exc
            if goods_id <= 0 or goods_id > UINT32_MAX:
                raise ShopApplicationError(
                    ERROR_CONFIG, f"shop_state.{key} has invalid goods ID"
                )
            count = _state_uint(raw_count, f"shop_state.{key}[{goods_id}]")
            if count:
                result[str(goods_id)] = count
        return result

    def _today(self) -> date:
        value = self._today_provider()
        if not isinstance(value, date):
            raise ShopApplicationError(
                ERROR_CONFIG, "shop today provider did not return a date"
            )
        return value

    @staticmethod
    def _rmb_period_key(definition: RmbGoodsDefinition, today: date) -> str:
        if definition.limit_type == 1:
            return f"daily:{today.isoformat()}"
        if definition.limit_type == 2:
            iso_year, iso_week, _ = today.isocalendar()
            return f"weekly:{iso_year:04d}-W{iso_week:02d}"
        if definition.limit_type == 3:
            return f"monthly:{today.year:04d}-{today.month:02d}"
        # Type 5 is the defined one-off availability limit.  Unknown and
        # unlimited types are kept lifetime-scoped rather than guessed into
        # a reset cadence.
        return "lifetime"

    def _period_counts(
        self,
        raw: Mapping[str, Any],
        legacy_counts: Mapping[str, int],
        today: date,
    ) -> Dict[str, Dict[str, int]]:
        value = raw.get("rmb_period_counts")
        if value is None:
            value = {}
        if not isinstance(value, Mapping):
            raise ShopApplicationError(
                ERROR_CONFIG, "shop_state.rmb_period_counts is not a map"
            )
        result: Dict[str, Dict[str, int]] = {}
        for raw_period, raw_counts in value.items():
            if (
                not isinstance(raw_period, str)
                or not raw_period
                or len(raw_period) > 64
            ):
                raise ShopApplicationError(
                    ERROR_CONFIG,
                    "shop_state.rmb_period_counts has an invalid period key",
                )
            counts = self._count_map(
                {"period": raw_counts},
                "period",
            )
            unknown = sorted(
                int(goods_id)
                for goods_id in counts
                if int(goods_id) not in self.catalog.rmb_by_id
            )
            if unknown:
                raise ShopApplicationError(
                    ERROR_CONFIG,
                    "shop_state.rmb_period_counts contains unknown static IDs: "
                    f"{unknown}",
                )
            if counts:
                result[raw_period] = counts

        # Schema-v1 saves had only rmb_counts.  Treat those counts as belonging
        # to the current defined cadence, so the first v2 transaction can
        # persist a lossless migration and future period rollover works.
        if not result and legacy_counts:
            for raw_goods_id, count in legacy_counts.items():
                definition = self.catalog.rmb_by_id.get(int(raw_goods_id))
                if definition is None:
                    raise ShopApplicationError(
                        ERROR_CONFIG,
                        "shop_state.rmb_counts contains an unknown static ID: "
                        f"{raw_goods_id}",
                    )
                period = self._rmb_period_key(definition, today)
                result.setdefault(period, {})[raw_goods_id] = count
        return result

    def _order_map(self, raw: Mapping[str, Any]) -> Dict[str, Dict[str, Any]]:
        value = raw.get("rmb_orders", {})
        if value is None:
            value = {}
        if not isinstance(value, Mapping):
            raise ShopApplicationError(
                ERROR_CONFIG, "shop_state.rmb_orders is not a map"
            )
        result: Dict[str, Dict[str, Any]] = {}
        for raw_key, raw_order in value.items():
            if not isinstance(raw_key, str) or not raw_key or len(raw_key) > 256:
                raise ShopApplicationError(
                    ERROR_CONFIG, "shop_state.rmb_orders has an invalid key"
                )
            if not isinstance(raw_order, Mapping):
                raise ShopApplicationError(
                    ERROR_CONFIG, f"shop_state.rmb_orders[{raw_key}] is not a row"
                )
            goods_id = _state_uint(
                raw_order.get("goods_id", 0),
                f"shop_state.rmb_orders[{raw_key}].goods_id",
            )
            definition = self.catalog.rmb_by_id.get(goods_id)
            if definition is None:
                raise ShopApplicationError(
                    ERROR_CONFIG,
                    f"shop_state.rmb_orders[{raw_key}] has unknown goods ID",
                )
            product_id = raw_order.get("product_id")
            if product_id != definition.product_id:
                raise ShopApplicationError(
                    ERROR_CONFIG,
                    f"shop_state.rmb_orders[{raw_key}] product disagrees with catalog",
                )
            row = copy.deepcopy(dict(raw_order))
            for field_name in ("period", "order_no", "out_trade_no", "status"):
                if not isinstance(row.get(field_name), str) or not row[field_name]:
                    raise ShopApplicationError(
                        ERROR_CONFIG,
                        f"shop_state.rmb_orders[{raw_key}].{field_name} is invalid",
                    )
            result[raw_key] = row
        return result

    def _shop_state(self, profile: Mapping[str, Any]) -> Dict[str, Any]:
        raw = profile.get(SHOP_STATE_KEY, {})
        if raw is None:
            raw = {}
        if not isinstance(raw, Mapping):
            raise ShopApplicationError(ERROR_CONFIG, "profile shop_state is not a map")
        history = raw.get("purchase_history", [])
        if history is None:
            history = []
        if not isinstance(history, list) or not all(
            isinstance(row, Mapping) for row in history
        ):
            raise ShopApplicationError(
                ERROR_CONFIG, "shop_state.purchase_history is not a row list"
            )
        today = self._today()
        legacy_rmb_counts = self._count_map(raw, "rmb_counts")
        period_counts = self._period_counts(raw, legacy_rmb_counts, today)
        current_rmb_counts: Dict[str, int] = {}
        for definition in self.catalog.rmb_goods:
            period = self._rmb_period_key(definition, today)
            count = int(period_counts.get(period, {}).get(str(definition.goods_id), 0))
            if count:
                current_rmb_counts[str(definition.goods_id)] = count
        state = {
            "schema_version": SHOP_STATE_SCHEMA_VERSION,
            "ordinary_counts": self._count_map(raw, "ordinary_counts"),
            "activity_counts": self._count_map(raw, "activity_counts"),
            "rmb_counts": current_rmb_counts,
            "rmb_period_counts": period_counts,
            "rmb_orders": self._order_map(raw),
            "refresh_counts": self._count_map(raw, "refresh_counts"),
            "purchase_history": copy.deepcopy(history),
        }
        allowed_ids = {
            "ordinary_counts": self.catalog.ordinary_by_id,
            "activity_counts": self.catalog.activity_by_id,
            "rmb_counts": self.catalog.rmb_by_id,
            "refresh_counts": self.catalog.ordinary_by_shop,
        }
        for key, allowed in allowed_ids.items():
            unknown = sorted(
                int(raw_id) for raw_id in state[key] if int(raw_id) not in allowed
            )
            if unknown:
                raise ShopApplicationError(
                    ERROR_CONFIG,
                    f"shop_state.{key} contains unknown static IDs: {unknown}",
                )
        return state

    @staticmethod
    def _messages(
        counts: Mapping[str, int], *, rmb: bool = False
    ) -> List[Dict[str, int]]:
        name = "GoodsId" if rmb else "ItemId"
        return [
            {name: int(goods_id), "Count": count}
            for goods_id, count in sorted(counts.items(), key=lambda item: int(item[0]))
            if count > 0
        ]

    def _shop_data_messages(self, state: Mapping[str, Any]) -> List[Dict[str, Any]]:
        refresh_counts = state["refresh_counts"]
        result = []
        for shop_id, rows in sorted(self.catalog.ordinary_by_shop.items()):
            message: Dict[str, Any] = {
                "ShopId": shop_id,
                "GoodsList": [{"GoodsId": row.goods_id} for row in rows],
            }
            refresh_count = int(refresh_counts.get(str(shop_id), 0))
            if refresh_count:
                message["ShopRefreshCount"] = refresh_count
            result.append(message)
        return result

    def _gift_messages(self, state: Mapping[str, Any], profile=None) -> List[Dict[str, Any]]:
        counts = state["rmb_counts"]
        result = []
        for shop_id, rows in sorted(self.catalog.rmb_by_shop.items()):
            gifts = []
            month_cards = []
            for row in rows:
                count = int(counts.get(str(row.goods_id), 0))
                if self.normal_economy and row.goods_id == LOCAL_MONTH_CARD_GOODS_ID:
                    _, _, taken, remaining = month_card_values(profile or {}, self._now())
                    month_cards.append({"GoodsId": row.goods_id, "RemainderTime": min(UINT32_MAX, remaining),
                                        "ExtraReward": taken})
                elif row.shop_type == 1:
                    item: Dict[str, int] = {"GoodsId": row.goods_id, "Status": 1}
                    if row.static_price == 0 and count == 0:
                        item["FreeFirst"] = 1
                    if row.limit:
                        item["RemainderTakeCount"] = max(0, row.limit - count)
                    gifts.append(item)
                elif row.shop_type == 2:
                    month_cards.append({"GoodsId": row.goods_id})
            if gifts or month_cards:
                result.append(
                    {
                        "ShopId": shop_id,
                        "GiftGoodsList": gifts,
                        "McGoodsItemList": month_cards,
                    }
                )
        return result

    def _crystal_messages(self, state: Mapping[str, Any]) -> List[Dict[str, Any]]:
        counts = state["rmb_counts"]
        result = []
        for shop_id, rows in sorted(self.catalog.rmb_by_shop.items()):
            crystals = [
                {
                    "GoodsId": row.goods_id,
                    "IsFirstPurchase": 1 if counts.get(str(row.goods_id), 0) else 0,
                }
                for row in rows
                if row.shop_type == 0
            ]
            if crystals:
                result.append({"ShopId": shop_id, "CrystalGoodsList": crystals})
        return result

    def shop_datas_body(
        self, profile: Mapping[str, Any], *, error_code: int = ERROR_SUCCESS
    ) -> bytes:
        if error_code != ERROR_SUCCESS:
            return self.codec.encode("SC_ShopDatasRet", {"Errorcode": error_code})
        state = self._shop_state(profile)
        return self.codec.encode(
            "SC_ShopDatasRet",
            {
                "Errorcode": ERROR_SUCCESS,
                "ShopDataList": self._shop_data_messages(state),
                "ItemList": self._messages(state["ordinary_counts"]),
                "GiftMCShopDataList": self._gift_messages(state, profile),
                "CrystalShopDataList": self._crystal_messages(state),
                "BoughtRMBItemList": self._messages(state["rmb_counts"], rmb=True),
                "ShopParentList": list(self.catalog.parent_ids),
            },
        )

    def bought_item_datas_body(self, profile: Mapping[str, Any]) -> bytes:
        state = self._shop_state(profile)
        return self.codec.encode(
            "SC_BoughtItemDatasRet",
            {"ItemList": self._messages(state["ordinary_counts"])},
        )

    def bought_rmb_item_datas_body(self, profile: Mapping[str, Any]) -> bytes:
        state = self._shop_state(profile)
        return self.codec.encode(
            "SC_BoughtRMBItemDatasRet",
            {"GoodsList": self._messages(state["rmb_counts"], rmb=True)},
        )

    @staticmethod
    def _aggregate(rewards: Iterable[Reward]) -> Tuple[Reward, ...]:
        order: List[Tuple[int, int]] = []
        totals: Dict[Tuple[int, int], int] = {}
        for reward in rewards:
            key = (reward.res_type, reward.item_id)
            if key not in totals:
                order.append(key)
                totals[key] = 0
            total = totals[key] + reward.count
            if total > UINT32_MAX:
                raise ShopApplicationError(
                    ERROR_CONFIG, "combined reward exceeds uint32"
                )
            totals[key] = total
        return tuple(Reward(*key, totals[key]) for key in order)

    def _reward_changes(
        self, profile: Mapping[str, Any], rewards: Sequence[Reward], *, reject_saturated: bool = False
    ) -> Tuple[Dict[str, Any], Tuple[Reward, ...], bool]:
        changes: Dict[str, Any] = {}
        actual: List[Reward] = []
        is_new_gospel = False
        try:
            inventory = normalize_inventory(copy.deepcopy(profile.get("inventory", {})))
        except InventoryContentError as exc:
            raise ShopApplicationError(ERROR_CONFIG, str(exc)) from exc
        inventory_changed = False

        gospels_value = profile.get("gospels", [])
        if gospels_value is None:
            gospels_value = []
        if not isinstance(gospels_value, list):
            raise ShopApplicationError(ERROR_CONFIG, "profile gospels is not a list")
        gospels = copy.deepcopy(gospels_value)
        gospels_changed = False

        balances_value = profile.get("resource_balances", {})
        if balances_value is None:
            balances_value = {}
        if not isinstance(balances_value, Mapping):
            raise ShopApplicationError(
                ERROR_CONFIG, "profile resource_balances is not a map"
            )
        balances = copy.deepcopy(dict(balances_value))
        balances_changed = False

        skin_rewards = tuple(r for r in rewards if r.res_type == RESOURCE_GOSPEL_SKIN)
        skin_changes = {}
        granted_skins = set()
        if skin_rewards:
            item_use = self._item_use()
            if item_use.skin_projector is None:
                try:
                    from .content_responses import ContentResponseLayer
                except ImportError:
                    from content_responses import ContentResponseLayer
                item_use.skin_projector = ContentResponseLayer(self.catalog.content_dir)
            owned = item_use.skin_projector._owned_skin_ids(profile)
            if not reject_saturated and profile.get("all_content_unlocked"):
                skin_rewards = tuple(r for r in skin_rewards if r.item_id not in owned)
            skin_changes = item_use._skins(profile, skin_rewards)
            granted_skins = {r.item_id for r in skin_rewards}

        backgrounds_value = profile.get("unlocked_background_ids", [])
        if backgrounds_value is None:
            backgrounds_value = []
        if not isinstance(backgrounds_value, list):
            raise ShopApplicationError(
                ERROR_CONFIG, "profile unlocked_background_ids is not a list"
            )
        background_ids = {
            _state_uint(value, "unlocked_background_ids entry")
            for value in backgrounds_value
        }
        backgrounds_changed = False

        def grant_item(item_id: int, count: int) -> None:
            nonlocal inventory_changed
            maximum = self.catalog.item_max_stacks[item_id]
            if maximum <= 0:
                raise ShopApplicationError(
                    ERROR_CONFIG, f"item {item_id} is not a storable a_item"
                )
            key = str(item_id)
            previous = inventory.get(key)
            current = _state_uint(
                previous.get("count", 0) if isinstance(previous, Mapping) else 0,
                f"inventory[{item_id}].count",
            )
            updated = current + count
            if updated > maximum:
                if not reject_saturated and not self.normal_economy and profile.get("all_content_unlocked") and current == maximum:
                    return
                raise ShopApplicationError(
                    ERROR_EXCEED_ITEM_LIMIT_COUNT,
                    f"item {item_id} would exceed a_item.max_stack_num {maximum}",
                )
            inventory[key] = {
                "uid": int(previous.get("uid"))
                if isinstance(previous, Mapping) and previous.get("uid")
                else stable_item_uid(item_id),
                "id": item_id,
                "count": updated,
                "para": int(previous.get("para", 0))
                if isinstance(previous, Mapping)
                else 0,
            }
            inventory_changed = True
            actual.append(Reward(RESOURCE_ITEM, item_id, count))

        def grant_unique_balance(reward: Reward) -> None:
            nonlocal balances_changed
            key = f"{reward.res_type}:{reward.item_id}"
            current = _state_uint(balances.get(key, 0), f"resource_balances[{key}]")
            if current:
                if not reject_saturated and profile.get("all_content_unlocked"):
                    return
                raise ShopApplicationError(
                    ERROR_EXCEED_ITEM_LIMIT_COUNT,
                    f"unique resource {key} is already owned",
                )
            if reward.count != 1:
                raise ShopApplicationError(
                    ERROR_CONFIG, f"unique resource {key} has count {reward.count}"
                )
            balances[key] = 1
            balances_changed = True
            actual.append(reward)

        for reward in self._aggregate(rewards):
            if reward.res_type in {
                RESOURCE_DIAMOND,
                RESOURCE_MINIDIAMOND,
                RESOURCE_GOLD,
                RESOURCE_ENERGY,
            }:
                field_name = {
                    RESOURCE_DIAMOND: "diamond",
                    RESOURCE_MINIDIAMOND: "minidiamond",
                    RESOURCE_GOLD: "gold",
                    RESOURCE_ENERGY: "energy",
                }[reward.res_type]
                current = _state_uint(changes.get(field_name, profile.get(field_name, 0)), field_name)
                updated = current + reward.count
                if updated > UINT32_MAX:
                    raise ShopApplicationError(
                        ERROR_CONFIG, f"reward would overflow profile {field_name}"
                    )
                changes[field_name] = updated
                actual.append(reward)
            elif reward.res_type == RESOURCE_ITEM:
                grant_item(reward.item_id, reward.count)
            elif reward.res_type == RESOURCE_GOSPEL:
                for _ in range(reward.count):
                    found_index = next(
                        (
                            index
                            for index, row in enumerate(gospels)
                            if isinstance(row, Mapping)
                            and _state_uint(row.get("id", 0), "gospel.id")
                            == reward.item_id
                        ),
                        None,
                    )
                    if found_index is None:
                        occupied = {
                            _state_uint(row.get("uid", 0), "gospel.uid")
                            for row in gospels
                            if isinstance(row, Mapping)
                        }
                        uid = max(2_000_000_000, max(occupied, default=0)) + 1
                        gospels.append(self.gospel_factory.create(reward.item_id, uid))
                        gospels_changed = True
                        is_new_gospel = True
                        actual.append(Reward(RESOURCE_GOSPEL, reward.item_id, 1))
                    else:
                        source = gospels[found_index]
                        row = dict(source) if isinstance(source, Mapping) else {}
                        repeated_num = _state_uint(
                            row.get("repeated_num", 0), "gospel.repeated_num"
                        )
                        if repeated_num == UINT32_MAX:
                            raise ShopApplicationError(
                                ERROR_CONFIG, "gospel.repeated_num would overflow"
                            )
                        row["repeated_num"] = repeated_num + 1
                        gospels[found_index] = row
                        gospels_changed = True
                        for conversion in self.catalog.gospel_repeat_rewards.get(
                            reward.item_id, ()
                        ):
                            grant_item(conversion.item_id, conversion.count)
            elif reward.res_type == RESOURCE_GOSPEL_SKIN:
                if reward.item_id in granted_skins:
                    actual.append(reward)
            elif reward.res_type == RESOURCE_MAIN_BACK_IMAGE:
                if reward.item_id in background_ids:
                    if not reject_saturated and profile.get("all_content_unlocked"):
                        continue
                    raise ShopApplicationError(
                        ERROR_EXCEED_ITEM_LIMIT_COUNT,
                        f"background {reward.item_id} is already owned",
                    )
                if reward.count != 1:
                    raise ShopApplicationError(
                        ERROR_CONFIG, "background reward count is not one"
                    )
                background_ids.add(reward.item_id)
                backgrounds_changed = True
                actual.append(reward)
            elif reward.res_type in {
                RESOURCE_HEAD_IMAGE,
                RESOURCE_DECORATION,
                RESOURCE_MAILBOX_SKIN,
                RESOURCE_HEAD_FRAME,
            }:
                grant_unique_balance(reward)
            else:  # Constructor validation should make this unreachable.
                raise ShopApplicationError(
                    ERROR_CONFIG, f"unsupported shop reward type {reward.res_type}"
                )

        if inventory_changed:
            changes["inventory"] = {
                key: inventory[key]
                for key in sorted(inventory, key=lambda value: int(value))
            }
        if gospels_changed:
            changes["gospels"] = gospels
        if balances_changed:
            changes["resource_balances"] = balances
        changes.update(skin_changes)
        if backgrounds_changed:
            changes["unlocked_background_ids"] = sorted(background_ids)
        return changes, self._aggregate(actual), is_new_gospel

    @staticmethod
    def _reward_values(rewards: Iterable[Reward]) -> List[Dict[str, int]]:
        return [
            {
                "ResType": reward.res_type,
                "ItemId": reward.item_id,
                "Count": reward.count,
            }
            for reward in rewards
        ]

    @staticmethod
    def _order_identifiers(idempotency_key: str) -> Tuple[str, str]:
        digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
        return (
            f"LOCAL-{digest[:24].upper()}",
            f"local-{digest[:32]}",
        )

    @staticmethod
    def _order_key(role_id: int, goods_id: int, period: str) -> str:
        return f"{role_id}:{goods_id}:{period}"

    def _stored_rewards(self, order: Mapping[str, Any]) -> Tuple[Reward, ...]:
        value = order.get("configured_rewards")
        if not isinstance(value, list):
            raise ShopApplicationError(
                ERROR_CONFIG, "completed RMB order has no configured rewards"
            )
        rewards: List[Reward] = []
        for index, raw in enumerate(value):
            if not isinstance(raw, Mapping):
                raise ShopApplicationError(
                    ERROR_CONFIG,
                    f"completed RMB order reward {index} is not a row",
                )
            reward = Reward(
                _state_uint(raw.get("res_type"), f"order reward {index}.res_type"),
                _state_uint(raw.get("item_id"), f"order reward {index}.item_id"),
                _state_uint(
                    raw.get("count"),
                    f"order reward {index}.count",
                ),
            )
            if reward.count <= 0:
                raise ShopApplicationError(
                    ERROR_CONFIG, f"completed RMB order reward {index} is empty"
                )
            rewards.append(reward)
        return self._aggregate(rewards)

    def _expand_local_order_rewards(self, rewards: Sequence[Reward]) -> Tuple[Reward, ...]:
        """Grant fixed purchase contents exactly as the source preview.

        Some internal containers have a positive stack cap but are named
        'test'. They still represent the fixed purchase preview, not a
        player-facing reward. Choice/random boxes remain unopened items.
        """
        expanded = []

        def visit(reward, ancestors=()):
            if reward.res_type != RESOURCE_ITEM:
                expanded.append(reward)
                return
            item = self.catalog.items[reward.item_id]
            if int(item.get("type", 0)) != 2 or int(item.get("is_use", 0)) != 1:
                if self.catalog.item_max_stacks[reward.item_id] > 0:
                    expanded.append(reward)
                    return
                raise ShopApplicationError(ERROR_CONFIG, "non-storable reward is not a fixed gift container")
            if reward.item_id in ancestors or len(ancestors) >= 16:
                raise ShopApplicationError(ERROR_CONFIG, "cyclic or overly nested gift contents")
            try:
                contents = _parse_rewards(item.get("add_num"), f"a_item[{reward.item_id}].add_num")
                self.catalog._validate_rewards(contents, f"a_item[{reward.item_id}].add_num", allow_package=True)
            except ShopConfigError as exc:
                raise ShopApplicationError(ERROR_CONFIG, str(exc)) from exc
            if not contents:
                raise ShopApplicationError(ERROR_CONFIG, "gift contents are unavailable")
            for content in contents:
                visit(content.scaled(reward.count), (*ancestors, reward.item_id))

        for reward in rewards:
            visit(reward)
        return self._aggregate(expanded)

    def _absolute_item_notice(
        self,
        profile: Mapping[str, Any],
        changes: Mapping[str, Any],
        item_ids: Iterable[int],
    ) -> Optional[Tuple[int, bytes]]:
        raw_inventory = changes.get("inventory", profile.get("inventory", {}))
        try:
            inventory = normalize_inventory(copy.deepcopy(raw_inventory))
        except InventoryContentError as exc:
            raise ShopApplicationError(ERROR_CONFIG, str(exc)) from exc
        rows = []
        for item_id in sorted(set(int(value) for value in item_ids)):
            row = inventory.get(str(item_id))
            rows.append(
                {
                    "ItemUId": int(row.get("uid"))
                    if isinstance(row, Mapping) and row.get("uid")
                    else stable_item_uid(item_id),
                    "ItemId": item_id,
                    "Count": int(row.get("count", 0))
                    if isinstance(row, Mapping)
                    else 0,
                    "Para": int(row.get("para", 0)) if isinstance(row, Mapping) else 0,
                }
            )
        if not rows:
            return None
        return (
            NOTIFY_UPDATE_MULTI_ITEM,
            self.codec.encode("Notify_UpdateMultiItem", {"ItemList": rows}),
        )

    def _charge_success_notice(
        self,
        product_id: str,
        order_no: str,
        out_trade_no: str,
        rewards: Sequence[Reward],
        month_taken: int = 0,
    ) -> Tuple[int, bytes]:
        return (
            NOTIFY_CHARGE_SUCCESS,
            self.codec.encode(
                "Notify_ChargeSuccess",
                {
                    "ProductId": product_id,
                    "OrderNo": order_no,
                    "OutTradeNo": out_trade_no,
                    "RewardList": self._reward_values(rewards),
                    "IsTakeMCExtraReward": month_taken,
                },
            ),
        )

    def _order_post_notifications(
        self,
        profile: Mapping[str, Any],
        changes: Mapping[str, Any],
        definition: RmbGoodsDefinition,
        order_no: str,
        out_trade_no: str,
        rewards: Sequence[Reward],
    ) -> Tuple[Tuple[int, bytes], ...]:
        notifications: List[Tuple[int, bytes]] = []
        current = dict(profile)
        current.update(changes)
        if self.normal_economy:
            # Replays also repair a cache which missed a notification. These
            # are current absolute balances, never re-applied reward deltas.
            for prop_type, key in ((0, "diamond"), (1, "minidiamond"), (2, "gold"), (3, "energy")):
                notifications.append((25, self.codec.encode("Notify_UpdateRoleProp", {
                    "RoleId": int(current.get("role_id", 0)),
                    "PropType": prop_type, "PropValue": int(current.get(key, 0))})))
        item_notice = self._absolute_item_notice(
            profile,
            changes,
            (reward.item_id for reward in rewards if reward.res_type == RESOURCE_ITEM),
        )
        if item_notice is not None:
            notifications.append(item_notice)
        notifications.extend(self.skin_notifications(current, rewards))
        notifications.append(
            self._charge_success_notice(
                definition.product_id,
                order_no,
                out_trade_no,
                rewards,
                month_card_values(current, self._now())[2] if self.normal_economy else 0,
            )
        )
        if self.normal_economy:
            notifications.append((SC_BOUGHT_RMB_ITEM_DATAS_RET, self.bought_rmb_item_datas_body(current)))
            expires, count, taken, _ = month_card_values(current, self._now())
            for prop_type, value in ((5, expires), (18, count), (19, taken)):
                notifications.append((25, self.codec.encode("Notify_UpdateRoleProp", {
                    "RoleId": int(current.get("role_id", 0)), "PropType": prop_type, "PropValue": value})))
            # Refresh month-card expiry and gift limits on the current page.
            refresh = self.handle(CS_SHOP_UPDATE, current, b"")
            notifications.append((refresh.response_id, refresh.body))
        return tuple(notifications)

    def claim_month_card(self, profile, body):
        try:
            fields = _parse_uint_fields(body, {1, 2})
        except ProtobufDecodeError as exc:
            return Result(SC_OPEN_CLIENT_WINDOW_RET,
                          self.codec.encode("SC_OpenClientWindowRet", {"Errorcode": ERROR_MESSAGE_DATA_ILLEGAL}),
                          error_code=ERROR_MESSAGE_DATA_ILLEGAL, reason=str(exc))
        if fields.get(1, 0) != 3:
            return None
        values = {"Errorcode": 0, "WindowID": 3, "Param": fields.get(2, 0)}
        try:
            expires, _, taken, remaining = month_card_values(profile, self._now())
            if not remaining:
                raise ShopApplicationError(ERROR_CLIENT_REQUEST_PARAM, "month card is not active")
            if taken:
                return Result(SC_OPEN_CLIENT_WINDOW_RET, self.codec.encode("SC_OpenClientWindowRet", values),
                              reason="daily month-card reward already claimed")
            configured = _parse_rewards(self.catalog.globals[9990011]["string_value"], "a_global[9990011]")
            self.catalog._validate_rewards(configured, "a_global[9990011]")
            changes, grants, _ = self._reward_changes(profile, configured)
            day = economy_day(self._now()).isoformat()
            card = copy.deepcopy(profile.get("local_month_card", {}))
            card["last_claim_day"] = day
            changes["local_month_card"] = card
            receipt = {"source": "month_card_daily", "transaction_id": "month-card:" + day,
                       "grants": [r.as_dict() for r in grants]}
            changes["reward_history"] = [*copy.deepcopy(profile.get("reward_history", [])), receipt]
            changes["resource_history"] = [*copy.deepcopy(profile.get("resource_history", [])),
                                          {**receipt, "costs": [], "rewards": receipt["grants"]}]
            current = dict(profile)
            current.update(changes)
            notices = [(25, self.codec.encode("Notify_UpdateRoleProp", {
                "RoleId": int(profile.get("role_id", 0)), "PropType": prop, "PropValue": value}))
                for prop, value in ((1, int(current.get("minidiamond", 0))), (5, expires), (19, 1))]
            item_notice = self._absolute_item_notice(profile, changes, [r.item_id for r in grants if r.res_type == RESOURCE_ITEM])
            if item_notice: notices.append(item_notice)
            refresh = self.handle(CS_SHOP_UPDATE, current, b"")
            notices.append((refresh.response_id, refresh.body))
            return Result(SC_OPEN_CLIENT_WINDOW_RET, self.codec.encode("SC_OpenClientWindowRet", values),
                          changes=changes, rewards=configured, grants=grants, post_notifications=tuple(notices))
        except (ShopApplicationError, ShopConfigError) as exc:
            values["Errorcode"] = exc.error_code if isinstance(exc, ShopApplicationError) else ERROR_CONFIG
            return Result(SC_OPEN_CLIENT_WINDOW_RET, self.codec.encode("SC_OpenClientWindowRet", values),
                          error_code=values["Errorcode"], reason=str(exc))

    def _create_order_response(
        self,
        request: _CreateOrderRequest,
        error_code: int,
        *,
        order_no: str = "",
        out_trade_no: str = "",
    ) -> bytes:
        values: Dict[str, Any] = {
            "Errorcode": error_code,
            "RoleId": request.role_id,
            "Money": 0,
            "GameMoney": 0,
            "ProductId": request.product_id,
        }
        if out_trade_no:
            values.update(
                {
                    "OutTradeNo": out_trade_no,
                    "OrderSign": "LOCAL-OFFLINE",
                    "ExtensionInfo": order_no,
                }
            )
        return self.codec.encode("SC_CreateOrderRet", values)

    def _create_order_failure(
        self,
        request: _CreateOrderRequest,
        error_code: int,
        reason: str,
    ) -> Result:
        return Result(
            SC_CREATE_ORDER_RET,
            self._create_order_response(request, error_code),
            error_code=error_code,
            reason=reason,
        )

    def create_order(self, profile: Mapping[str, Any], body: bytes) -> Result:
        try:
            request = _parse_create_order(body)
        except ProtobufDecodeError as exc:
            request = _CreateOrderRequest(0, "", 0)
            return self._create_order_failure(
                request, ERROR_MESSAGE_DATA_ILLEGAL, str(exc)
            )
        if request.role_id <= 0 or not request.product_id:
            return self._create_order_failure(
                request,
                ERROR_CLIENT_REQUEST_PARAM,
                "CreateOrder requires RoleId and ProductId",
            )
        if request.is_sandbox not in {0, 1}:
            return self._create_order_failure(
                request,
                ERROR_CLIENT_REQUEST_PARAM,
                "CreateOrder.IsSandbox is not bool-compatible",
            )
        if request.local_request_id and not re.fullmatch(r"[0-9a-f]{32}", request.local_request_id):
            return self._create_order_failure(
                request, ERROR_MESSAGE_DATA_ILLEGAL, "invalid local purchase request ID"
            )
        local_purchase = self.normal_economy and bool(request.local_request_id)
        try:
            profile_role_id = _state_uint(
                profile.get("role_id", 0), "role_id", maximum=UINT64_MAX
            )
        except ShopApplicationError as exc:
            return self._create_order_failure(request, exc.error_code, str(exc))
        if request.role_id != profile_role_id:
            return self._create_order_failure(
                request,
                ERROR_CLIENT_REQUEST_PARAM,
                "CreateOrder.RoleId does not match the bound profile",
            )
        definition = self.catalog.rmb_by_product_id.get(request.product_id)
        if (
            definition is None
            or not definition.reward_proven
            or definition.price_type not in {0, 1}
            or (not local_purchase and (definition.static_price != 0 or definition.limit != 1))
        ):
            return self._create_order_failure(
                request,
                ERROR_SHOP_GOODS_ID,
                "product has no proved reward or requires a local purchase request ID",
            )
        try:
            state = self._shop_state(profile)
            today = self._today()
            period = self._rmb_period_key(definition, today)
            # Keep a click UUID stable through network retries and calendar
            # rollover. Distinct deliberate purchases use distinct UUIDs.
            idempotency_key = (
                f"{request.role_id}:local:{request.local_request_id}"
                if local_purchase else self._order_key(request.role_id, definition.goods_id, period)
            )
            existing = state["rmb_orders"].get(idempotency_key)
            if existing is not None:
                if existing.get("product_id") != request.product_id or int(existing.get("role_id", 0)) != request.role_id:
                    raise ShopApplicationError(ERROR_CLIENT_REQUEST_PARAM, "purchase request ID belongs to another product")
                if existing.get("status") != "completed":
                    raise ShopApplicationError(
                        ERROR_CONFIG, "local RMB order is not completed"
                    )
                rewards = self._stored_rewards(existing)
                order_no = str(existing["order_no"])
                out_trade_no = str(existing["out_trade_no"])
                post = self._order_post_notifications(
                    profile,
                    {},
                    definition,
                    order_no,
                    out_trade_no,
                    rewards,
                )
                return Result(
                    SC_CREATE_ORDER_RET,
                    self._create_order_response(
                        request,
                        ERROR_SUCCESS,
                        order_no=order_no,
                        out_trade_no=out_trade_no,
                    ),
                    rewards=rewards,
                    grants=(),
                    post_notifications=post,
                    goods_id=definition.goods_id,
                    reason="idempotent replay",
                )

            current = int(state["rmb_counts"].get(str(definition.goods_id), 0))
            if definition.limit and current >= definition.limit:
                return self._create_order_failure(
                    request,
                    ERROR_EXCEED_MAX_BUY_TIMES,
                    "RMB product reached its purchase limit for this period",
                )
            if current >= UINT32_MAX:
                raise ShopApplicationError(ERROR_EXCEED_MAX_BUY_TIMES, "purchase count would overflow")
            first_ever = not any(
                int(row.get("goods_id", 0)) == definition.goods_id
                and row.get("status") == "completed"
                for row in state["rmb_orders"].values()
            )
            configured = self._aggregate(
                (
                    *definition.rewards,
                    *(
                        definition.first_rewards
                        if first_ever
                        else definition.recurring_rewards
                    ),
                )
            )
            if local_purchase and definition.goods_id == LOCAL_MONTH_CARD_GOODS_ID:
                configured = _parse_rewards(self.catalog.globals[9990010]["string_value"], "a_global[9990010]")
                _, active_count, _, _ = month_card_values(profile, self._now())
                if active_count >= int(self.catalog.globals[9990296]["int_value"]):
                    raise ShopApplicationError(ERROR_EXCEED_MAX_BUY_TIMES, "month-card duration reached the local renewal limit")
            if not configured:
                raise ShopApplicationError(
                    ERROR_CONFIG, "RMB product has no configured reward for this purchase"
                )
            if local_purchase:
                configured = self._expand_local_order_rewards(configured)
            # Only real-money checkout is free. price_type=1 still denotes
            # the source in-game diamond wallet in ShopView.
            costs = ()
            staged = dict(profile)
            changes = {}
            if local_purchase and definition.price_type == 1 and definition.static_price:
                costs = (Reward(RESOURCE_DIAMOND, 0, definition.static_price),)
                try:
                    changes.update(deduct_resources(profile, [(r.res_type, r.item_id, r.count) for r in costs]))
                except EconomyError as exc:
                    raise ShopApplicationError(exc.error_code, str(exc)) from exc
                staged.update(changes)
            reward_changes, grants, _ = self._reward_changes(staged, configured)
            if local_purchase and not grants:
                raise ShopApplicationError(ERROR_EXCEED_ITEM_LIMIT_COUNT, "all product rewards are already owned")
            changes.update(reward_changes)
            if local_purchase and definition.goods_id == LOCAL_MONTH_CARD_GOODS_ID:
                card = copy.deepcopy(profile.get("local_month_card", {}))
                card["expires_at"] = max(int(card.get("expires_at", 0)), int(self._now())) + LOCAL_MONTH_CARD_SECONDS
                card["duration_rule"] = "offline-30-days-v1"
                changes["local_month_card"] = card
            total = current + 1
            state["rmb_period_counts"].setdefault(period, {})[
                str(definition.goods_id)
            ] = total
            state["rmb_counts"][str(definition.goods_id)] = total
            order_no, out_trade_no = self._order_identifiers(idempotency_key)
            order = {
                "role_id": request.role_id,
                "goods_id": definition.goods_id,
                "product_id": definition.product_id,
                "period": period,
                "order_no": order_no,
                "out_trade_no": out_trade_no,
                "status": "completed",
                "count": 1,
                "configured_rewards": [row.as_dict() for row in configured],
                "grants": [row.as_dict() for row in grants],
            }
            if local_purchase:
                order.update(local_request_id=request.local_request_id,
                             completion="offline_confirmed", money_charged=0,
                             catalog_price=definition.static_price, price_type=definition.price_type)
            state["rmb_orders"][idempotency_key] = order
            state["purchase_history"].append(
                {
                    "sequence": len(state["purchase_history"]) + 1,
                    "kind": "rmb_order",
                    **copy.deepcopy(order),
                }
            )
            changes[SHOP_STATE_KEY] = state
            if self.normal_economy:
                changes["resource_history"] = [*copy.deepcopy(profile.get("resource_history", [])), {
                    "source": "local_purchase", "command_id": CS_CREATE_ORDER,
                    "transaction_id": order_no, "goods_id": definition.goods_id,
                    "costs": [r.as_dict() for r in costs], "rewards": [r.as_dict() for r in grants]}]
                changes["reward_history"] = [*copy.deepcopy(profile.get("reward_history", [])), {
                    "source": "local_purchase", "transaction_id": order_no,
                    "goods_id": definition.goods_id, "grants": [r.as_dict() for r in grants]}]
            post = self._order_post_notifications(
                profile,
                changes,
                definition,
                order_no,
                out_trade_no,
                configured,
            )
            return Result(
                SC_CREATE_ORDER_RET,
                self._create_order_response(
                    request,
                    ERROR_SUCCESS,
                    order_no=order_no,
                    out_trade_no=out_trade_no,
                ),
                changes=changes,
                rewards=configured,
                grants=grants,
                post_notifications=post,
                goods_id=definition.goods_id,
            )
        except ShopApplicationError as exc:
            return self._create_order_failure(request, exc.error_code, str(exc))

    def order_charge(self, profile: Mapping[str, Any], body: bytes) -> Result:
        try:
            request = _parse_order_charge(body)
            state = self._shop_state(profile)
        except ProtobufDecodeError as exc:
            return Result(
                SC_ORDER_CHARGE_RET,
                self.codec.encode(
                    "SC_OrderChargeRet", {"Errorcode": ERROR_MESSAGE_DATA_ILLEGAL}
                ),
                error_code=ERROR_MESSAGE_DATA_ILLEGAL,
                reason=str(exc),
            )
        except ShopApplicationError as exc:
            return Result(
                SC_ORDER_CHARGE_RET,
                self.codec.encode("SC_OrderChargeRet", {"Errorcode": exc.error_code}),
                error_code=exc.error_code,
                reason=str(exc),
            )
        matching = next(
            (
                row
                for row in state["rmb_orders"].values()
                if row.get("order_no") == request.order_no
                and row.get("product_id") == request.product_id
                and row.get("status") == "completed"
            ),
            None,
        )
        if matching is None:
            error_code = ERROR_CLIENT_REQUEST_PARAM
            return Result(
                SC_ORDER_CHARGE_RET,
                self.codec.encode("SC_OrderChargeRet", {"Errorcode": error_code}),
                error_code=error_code,
                reason="OrderCharge does not identify a completed local order",
            )
        try:
            definition = self.catalog.rmb_by_id[int(matching["goods_id"])]
            rewards = self._stored_rewards(matching)
            post = (
                self._charge_success_notice(
                    definition.product_id,
                    str(matching["order_no"]),
                    str(matching["out_trade_no"]),
                    rewards,
                ),
            )
        except (KeyError, TypeError, ValueError, ShopApplicationError) as exc:
            return Result(
                SC_ORDER_CHARGE_RET,
                self.codec.encode("SC_OrderChargeRet", {"Errorcode": ERROR_CONFIG}),
                error_code=ERROR_CONFIG,
                reason=str(exc),
            )
        return Result(
            SC_ORDER_CHARGE_RET,
            self.codec.encode("SC_OrderChargeRet", {"Errorcode": ERROR_SUCCESS}),
            post_notifications=post,
        )

    def _use_item_failure(
        self,
        request: _UseItemRequest,
        error_code: int,
        reason: str,
        *,
        item_id: int = 0,
    ) -> Result:
        return Result(
            SC_USE_ITEM_RET,
            self.codec.encode(
                "SC_UseItemRet",
                {
                    "Errorcode": error_code,
                    "ItemUId": request.item_uid,
                    "Num": request.count,
                    "ItemId": item_id,
                    "OptionalItemId": request.optional_item_id,
                    "OptionalSuitItemId": request.optional_suit_item_id,
                },
            ),
            error_code=error_code,
            reason=reason,
        )

    def _item_use(self):
        if self.item_use_layer is None:
            try:
                from .item_use_content import ItemUseLayer
            except ImportError:
                from item_use_content import ItemUseLayer
            self.item_use_layer = ItemUseLayer(self)
        return self.item_use_layer

    def use_item(self, profile: Mapping[str, Any], body: bytes) -> Result:
        return self._item_use().use(profile, body)

    def skin_notifications(self, profile, rewards):
        groups = defaultdict(list)
        for reward in rewards:
            if reward.res_type != RESOURCE_GOSPEL_SKIN:
                continue
            owner_id = self.catalog.gospel_skins[reward.item_id]["gospel_id"]
            owner = next((r for r in profile.get("gospels", []) if r["id"] == owner_id), None)
            groups[int(owner["uid"]) if owner else 0].append(reward.item_id)
        if not groups:
            return ()
        try:
            from .content_responses import ContentResponseLayer, field_varint
        except ImportError:
            from content_responses import ContentResponseLayer, field_varint
        item_use = self._item_use()
        if item_use.skin_projector is None:
            item_use.skin_projector = ContentResponseLayer(self.catalog.content_dir)
        projector = item_use.skin_projector
        result = []
        for uid, skins in sorted(groups.items()):
            skins = sorted(set(skins) & projector._owned_skin_ids(profile, uid))
            if not skins:
                continue
            result.append((1108, self.codec.encode("Notify_GospelSkinUnlocked", {
                "GospelUId": uid, "SkinList": skins})))
            if uid:
                result.append((1107, projector.make_gospel_skin_data_ret(profile, field_varint(1, uid))))
        return tuple(result)

    def claim_unlisted_skin(self, profile, body):
        """1409 remains a read unless field126 explicitly requests a claim.

        A known, owned character's shop-type cosmetic without any defined
        SKU may be claimed once, for free, under offline policy v1. Event and
        fondness rewards and every real SKU are deliberately excluded.
        """
        try:
            from .content_responses import iter_wire_fields, parse_packed_uint32_request, field_varint
        except ImportError:
            from content_responses import iter_wire_fields, parse_packed_uint32_request, field_varint
        skin_id = 0
        def result(code, reason="", changes=None, rewards=()):
            current = {**profile, **(changes or {})}
            payload = self.codec.encode("SC_RequestGospelSkinStatus_Ret", {
                "ErrorCode": code, "GospelSkinStatus": {skin_id: 1} if code == 0 else {}})
            return Result(1410, payload + field_varint(126, skin_id or 1),
                changes=changes or {}, error_code=code, reason=reason, rewards=rewards, grants=rewards,
                notifications=self.skin_notifications(current, rewards))
        try:
            fields = defaultdict(list)
            for number, kind, value in iter_wire_fields(body): fields[number].append((kind, value))
            if 126 not in fields:
                return None
            if set(fields) - {1, 126} or fields[126] != [(0, 1)]:
                return result(ERROR_MESSAGE_DATA_ILLEGAL, "invalid offline skin claim")
            requested = parse_packed_uint32_request(body, 1)
            if len(requested) != 1:
                return result(ERROR_MESSAGE_DATA_ILLEGAL, "claim exactly one skin")
            skin_id = requested[0]
            if skin_id not in self.unlisted_skins:
                return result(ERROR_SHOP_GOODS_ID, "use the original purchase/event/fondness route")
            owner_id = self.catalog.gospel_skins[skin_id]["gospel_id"]
            if not any(r["id"] == owner_id for r in profile.get("gospels", [])):
                return result(424, "own this Gospel before claiming its offline cosmetic")
            item_use = self._item_use()
            if item_use.skin_projector is None:
                try:
                    from .content_responses import ContentResponseLayer
                except ImportError:
                    from content_responses import ContentResponseLayer
                item_use.skin_projector = ContentResponseLayer(self.catalog.content_dir)
            reward = Reward(RESOURCE_GOSPEL_SKIN, skin_id, 1)
            if skin_id in item_use.skin_projector._owned_skin_ids(profile):
                # Current authority is returned after a lost acknowledgement,
                # without another grant or another history entry.
                replay = result(0, "offline cosmetic is already owned")
                return dataclass_replace(replay, notifications=self.skin_notifications(profile, (reward,)))
            changes = item_use._skins(profile, (reward,))
            history = profile.get("reward_history", [])
            resources = profile.get("resource_history", [])
            if not isinstance(history, list) or not isinstance(resources, list):
                return result(ERROR_CONFIG, "invalid reward ledger")
            changes["reward_history"] = [*copy.deepcopy(history), {
                "claim_key": f"offline-skin-v1:{skin_id}", "source": "offline_skin", "source_id": skin_id,
                "policy_version": 1, "rewards": [reward.as_dict()]}]
            changes["resource_history"] = [*copy.deepcopy(resources), {
                "source": "offline_skin", "command_id": 1409, "skin_id": skin_id,
                "policy_version": 1, "costs": [], "rewards": [reward.as_dict()]}]
            return result(0, "unlisted cosmetic granted under offline policy v1", changes, (reward,))
        except ShopApplicationError as exc:
            return result(exc.error_code, str(exc))
        except (ValueError, TypeError, KeyError) as exc:
            return result(ERROR_MESSAGE_DATA_ILLEGAL, str(exc))

    def _purchase_response(
        self,
        request: _PurchaseRequest,
        error_code: int,
        total_count: int,
        *,
        is_new_gospel: bool = False,
    ) -> bytes:
        return self.codec.encode(
            "SC_ShopPurchaseRet",
            {
                "Errorcode": error_code,
                "GoodsId": request.goods_id,
                "Count": request.count,
                "TotalCount": total_count,
                "IsActivity": request.is_activity,
                "MainActivityId": request.main_activity_id,
                "SubActivityId": request.subactivity_id,
                "IsNewGospel": is_new_gospel,
            },
        )

    def _purchase_failure(
        self,
        request: _PurchaseRequest,
        error_code: int,
        total_count: int,
        reason: str,
    ) -> Result:
        return Result(
            SC_SHOP_PURCHASE_RET,
            self._purchase_response(request, error_code, total_count),
            error_code=error_code,
            goods_id=request.goods_id,
            reason=reason,
        )

    def purchase(self, profile: Mapping[str, Any], body: bytes) -> Result:
        try:
            request = _parse_purchase(body)
        except ProtobufDecodeError as exc:
            request = _PurchaseRequest(0, 0, False, 0, 0)
            return self._purchase_failure(
                request, ERROR_MESSAGE_DATA_ILLEGAL, 0, str(exc)
            )
        if request.goods_id <= 0 or request.count <= 0:
            return self._purchase_failure(
                request,
                ERROR_CLIENT_REQUEST_PARAM,
                0,
                "goods ID and count must both be positive",
            )
        if request.is_activity and (
            request.main_activity_id <= 0 or request.subactivity_id <= 0
        ):
            return self._purchase_failure(
                request,
                ERROR_CLIENT_REQUEST_PARAM,
                0,
                "activity purchases require main and subactivity IDs",
            )
        if not request.is_activity and (
            request.main_activity_id or request.subactivity_id
        ):
            return self._purchase_failure(
                request,
                ERROR_CLIENT_REQUEST_PARAM,
                0,
                "ordinary purchases cannot carry activity IDs",
            )
        definitions = (
            self.catalog.activity_by_id
            if request.is_activity
            else self.catalog.ordinary_by_id
        )
        definition = definitions.get(request.goods_id)
        if definition is None:
            return self._purchase_failure(
                request,
                ERROR_SHOP_GOODS_ID,
                0,
                "goods ID does not exist in the selected static table",
            )
        try:
            state = self._shop_state(profile)
        except ShopApplicationError as exc:
            return self._purchase_failure(request, exc.error_code, 0, str(exc))
        count_key = "activity_counts" if request.is_activity else "ordinary_counts"
        counts = state[count_key]
        current = int(counts.get(str(request.goods_id), 0))
        total = current + request.count
        if total > UINT32_MAX:
            return self._purchase_failure(
                request,
                ERROR_CLIENT_REQUEST_PARAM,
                current,
                "purchase total exceeds uint32",
            )
        if definition.limit and total > definition.limit:
            return self._purchase_failure(
                request,
                ERROR_EXCEED_MAX_BUY_TIMES,
                current,
                f"purchase would exceed static limit {definition.limit}",
            )
        try:
            configured = tuple(
                reward.scaled(request.count) for reward in definition.rewards
            )
            # BuyItemCanvas uses price - floor(price * discount / 100) per
            # unit, then multiplies by Count. Round in precisely that order.
            costs = tuple(Reward(r.res_type, r.item_id,
                                r.count - r.count * definition.discount // 100).scaled(request.count)
                          for r in definition.costs) if self.normal_economy else ()
            deductions = deduct_resources(profile, ((r.res_type, r.item_id, r.count) for r in costs))
            working = {**profile, **deductions}
            rewards_patch, grants, is_new_gospel = self._reward_changes(working, configured)
            if self.normal_economy and not grants:
                raise ShopApplicationError(ERROR_EXCEED_ITEM_LIMIT_COUNT, "all purchased rewards are already owned")
            changes = {**deductions, **rewards_patch}
        except EconomyError as exc:
            return self._purchase_failure(request, exc.error_code, current, str(exc))
        except ShopApplicationError as exc:
            return self._purchase_failure(request, exc.error_code, current, str(exc))

        counts[str(request.goods_id)] = total
        history = state["purchase_history"]
        history.append(
            {
                "sequence": len(history) + 1,
                "goods_id": request.goods_id,
                "count": request.count,
                "total_count": total,
                "is_activity": request.is_activity,
                "main_activity_id": request.main_activity_id,
                "subactivity_id": request.subactivity_id,
                "configured_rewards": [reward.as_dict() for reward in configured],
                "grants": [reward.as_dict() for reward in grants],
                "costs": [reward.as_dict() for reward in costs],
            }
        )
        changes[SHOP_STATE_KEY] = state
        if self.normal_economy:
            changes["resource_history"] = [*copy.deepcopy(profile.get("resource_history", [])), {
                "source": "shop", "command_id": CS_SHOP_PURCHASE,
                "goods_id": request.goods_id, "count": request.count,
                "costs": [r.as_dict() for r in costs], "rewards": [r.as_dict() for r in grants]}]
        return Result(
            SC_SHOP_PURCHASE_RET,
            self._purchase_response(
                request,
                ERROR_SUCCESS,
                total,
                is_new_gospel=is_new_gospel,
            ),
            changes=changes,
            rewards=configured,
            grants=grants,
            goods_id=request.goods_id,
            notifications=self.skin_notifications({**profile, **changes}, grants),
        )

    def _refresh_result(
        self,
        profile: Mapping[str, Any],
        body: bytes,
        *,
        increment: bool,
    ) -> Result:
        try:
            values = _parse_uint_fields(body, {1})
        except ProtobufDecodeError as exc:
            response = self.codec.encode(
                "SC_ShopRefreshRet", {"Errorcode": ERROR_MESSAGE_DATA_ILLEGAL}
            )
            return Result(
                SC_SHOP_REFRESH_RET,
                response,
                error_code=ERROR_MESSAGE_DATA_ILLEGAL,
                reason=str(exc),
            )
        shop_id = values.get(1, 0)
        rows = self.catalog.ordinary_by_shop.get(shop_id)
        if rows is None:
            response = self.codec.encode(
                "SC_ShopRefreshRet",
                {"Errorcode": ERROR_SHOP_GOODS_ID, "ShopId": shop_id},
            )
            return Result(
                SC_SHOP_REFRESH_RET,
                response,
                error_code=ERROR_SHOP_GOODS_ID,
                reason="shop child has no static ordinary goods",
            )
        try:
            state = self._shop_state(profile)
        except ShopApplicationError as exc:
            response = self.codec.encode(
                "SC_ShopRefreshRet",
                {"Errorcode": exc.error_code, "ShopId": shop_id},
            )
            return Result(
                SC_SHOP_REFRESH_RET,
                response,
                error_code=exc.error_code,
                reason=str(exc),
            )
        refresh_count = int(state["refresh_counts"].get(str(shop_id), 0))
        changes: Dict[str, Any] = {}
        if increment:
            if self.normal_economy:
                return Result(SC_SHOP_REFRESH_RET,
                    self.codec.encode("SC_ShopRefreshRet", {"Errorcode": 32704, "ShopId": shop_id}),
                    error_code=32704, reason="random shop rotation rules are unavailable; nothing was charged")
            if refresh_count >= UINT32_MAX:
                response = self.codec.encode(
                    "SC_ShopRefreshRet",
                    {"Errorcode": ERROR_CONFIG, "ShopId": shop_id},
                )
                return Result(
                    SC_SHOP_REFRESH_RET,
                    response,
                    error_code=ERROR_CONFIG,
                    reason="shop refresh count would overflow",
                )
            refresh_count += 1
            state["refresh_counts"][str(shop_id)] = refresh_count
            changes[SHOP_STATE_KEY] = state
        response = self.codec.encode(
            "SC_ShopRefreshRet",
            {
                "Errorcode": ERROR_SUCCESS,
                "ShopId": shop_id,
                "RefreshCount": refresh_count,
                "ItemList": self._messages(state["ordinary_counts"]),
                "GoodsList": [{"GoodsId": row.goods_id} for row in rows],
            },
        )
        return Result(SC_SHOP_REFRESH_RET, response, changes=changes)

    def _child_result(self, profile: Mapping[str, Any], body: bytes) -> Result:
        try:
            values = _parse_uint_fields(body, {1})
            state = self._shop_state(profile)
        except ProtobufDecodeError as exc:
            response = self.codec.encode(
                "SC_ShopChildDatasRet", {"Errorcode": ERROR_MESSAGE_DATA_ILLEGAL}
            )
            return Result(
                SC_SHOP_CHILD_DATAS_RET,
                response,
                error_code=ERROR_MESSAGE_DATA_ILLEGAL,
                reason=str(exc),
            )
        except ShopApplicationError as exc:
            response = self.codec.encode(
                "SC_ShopChildDatasRet", {"Errorcode": exc.error_code}
            )
            return Result(
                SC_SHOP_CHILD_DATAS_RET,
                response,
                error_code=exc.error_code,
                reason=str(exc),
            )
        shop_id = values.get(1, 0)
        values_out: Dict[str, Any] = {
            "Errorcode": ERROR_SUCCESS,
            "ShopChildId": shop_id,
            "ItemList": self._messages(state["ordinary_counts"]),
            "BoughtRMBItemList": self._messages(state["rmb_counts"], rmb=True),
        }
        ordinary = self.catalog.ordinary_by_shop.get(shop_id)
        rmb = self.catalog.rmb_by_shop.get(shop_id, ())
        if ordinary:
            refresh_count = int(state["refresh_counts"].get(str(shop_id), 0))
            shop_data: Dict[str, Any] = {
                "ShopId": shop_id,
                "GoodsList": [{"GoodsId": row.goods_id} for row in ordinary],
            }
            if refresh_count:
                shop_data["ShopRefreshCount"] = refresh_count
            values_out["ShopData"] = shop_data
        if any(row.shop_type in {1, 2} for row in rmb):
            values_out["GiftMCShopData"] = next(
                row for row in self._gift_messages(state, profile) if row["ShopId"] == shop_id
            )
        if any(row.shop_type == 0 for row in rmb):
            values_out["CrystalShopData"] = next(
                row for row in self._crystal_messages(state) if row["ShopId"] == shop_id
            )
        if not ordinary and not rmb:
            values_out = {"Errorcode": ERROR_SHOP_GOODS_ID, "ShopChildId": shop_id}
        error_code = int(values_out["Errorcode"])
        return Result(
            SC_SHOP_CHILD_DATAS_RET,
            self.codec.encode("SC_ShopChildDatasRet", values_out),
            error_code=error_code,
            reason="" if error_code == ERROR_SUCCESS else "unknown shop child",
        )

    def handle(
        self,
        command_id: int,
        profile: Optional[Mapping[str, Any]],
        body: bytes = b"",
    ) -> Optional[Result]:
        """Handle one shop request, or return ``None`` for another layer."""

        profile = profile or {}
        if command_id == 1409:
            return self.claim_unlisted_skin(profile, body)
        if command_id == CS_OPEN_CLIENT_WINDOW:
            return self.claim_month_card(profile, body) if self.normal_economy else None
        if command_id == CS_CREATE_ORDER:
            return self.create_order(profile, body)
        if command_id == CS_ORDER_CHARGE:
            return self.order_charge(profile, body)
        if command_id == CS_USE_ITEM:
            return self.use_item(profile, body)
        if command_id == CS_SHOP_PURCHASE:
            return self.purchase(profile, body)
        if command_id in {
            CS_REQUEST_BOUGHT_ITEM_DATAS,
            CS_REQUEST_BOUGHT_RMB_ITEM_DATAS,
            CS_REQUEST_SHOP_DATAS,
            CS_SHOP_UPDATE,
        }:
            try:
                _empty_request(body, EXPECTED_COMMANDS[command_id])
                if command_id == CS_REQUEST_BOUGHT_ITEM_DATAS:
                    response_id = SC_BOUGHT_ITEM_DATAS_RET
                    response_body = self.bought_item_datas_body(profile)
                elif command_id == CS_REQUEST_BOUGHT_RMB_ITEM_DATAS:
                    response_id = SC_BOUGHT_RMB_ITEM_DATAS_RET
                    response_body = self.bought_rmb_item_datas_body(profile)
                elif command_id == CS_REQUEST_SHOP_DATAS:
                    response_id = SC_SHOP_DATAS_RET
                    response_body = self.shop_datas_body(profile)
                else:
                    state = self._shop_state(profile)
                    response_id = SC_SHOP_UPDATE_RET
                    response_body = self.codec.encode(
                        "SC_ShopUpdateRet",
                        {
                            "Errorcode": ERROR_SUCCESS,
                            "ShopDataList": self._shop_data_messages(state),
                            "GiftMCShopDataList": self._gift_messages(state, profile),
                            "ItemList": self._messages(state["ordinary_counts"]),
                        },
                    )
                return Result(response_id, response_body)
            except ProtobufDecodeError as exc:
                response_id = RESPONSE_IDS[command_id]
                if command_id == CS_REQUEST_SHOP_DATAS:
                    response_body = self.shop_datas_body(
                        profile, error_code=ERROR_MESSAGE_DATA_ILLEGAL
                    )
                elif command_id == CS_SHOP_UPDATE:
                    response_body = self.codec.encode(
                        "SC_ShopUpdateRet",
                        {"Errorcode": ERROR_MESSAGE_DATA_ILLEGAL},
                    )
                else:
                    message_name = (
                        "SC_BoughtItemDatasRet"
                        if command_id == CS_REQUEST_BOUGHT_ITEM_DATAS
                        else "SC_BoughtRMBItemDatasRet"
                    )
                    response_body = self.codec.encode(message_name, {})
                return Result(
                    response_id,
                    response_body,
                    error_code=ERROR_MESSAGE_DATA_ILLEGAL,
                    reason=str(exc),
                )
            except ShopApplicationError as exc:
                response_id = RESPONSE_IDS[command_id]
                message_name = {
                    CS_REQUEST_BOUGHT_ITEM_DATAS: "SC_BoughtItemDatasRet",
                    CS_REQUEST_BOUGHT_RMB_ITEM_DATAS: "SC_BoughtRMBItemDatasRet",
                    CS_REQUEST_SHOP_DATAS: "SC_ShopDatasRet",
                    CS_SHOP_UPDATE: "SC_ShopUpdateRet",
                }[command_id]
                values = (
                    {"Errorcode": exc.error_code}
                    if message_name in {"SC_ShopDatasRet", "SC_ShopUpdateRet"}
                    else {}
                )
                return Result(
                    response_id,
                    self.codec.encode(message_name, values),
                    error_code=exc.error_code,
                    reason=str(exc),
                )
        if command_id == CS_SHOP_REFRESH:
            return self._refresh_result(profile, body, increment=True)
        if command_id == CS_REQUEST_SHOP_REFRESH_DATA:
            return self._refresh_result(profile, body, increment=False)
        if command_id == CS_REQUEST_SHOP_CHILD_DATAS:
            return self._child_result(profile, body)
        return None

    @staticmethod
    def apply_changes(
        profile: MutableMapping[str, Any], changes: Mapping[str, Any]
    ) -> None:
        """Apply a result exactly as the server's shallow merge does."""

        profile.update(copy.deepcopy(dict(changes)))


# Concise alias matching the other state-layer modules.
Layer = ShopContentLayer


@lru_cache(maxsize=1)
def default_layer() -> ShopContentLayer:
    return ShopContentLayer()


def handle(
    command_id: int,
    profile: Optional[Mapping[str, Any]],
    body: bytes = b"",
) -> Optional[Result]:
    return default_layer().handle(command_id, profile, body)


__all__ = [
    "CS_CREATE_ORDER",
    "CS_ORDER_CHARGE",
    "CS_REQUEST_BOUGHT_ITEM_DATAS",
    "CS_REQUEST_BOUGHT_RMB_ITEM_DATAS",
    "CS_REQUEST_SHOP_CHILD_DATAS",
    "CS_REQUEST_SHOP_DATAS",
    "CS_REQUEST_SHOP_REFRESH_DATA",
    "CS_SHOP_PURCHASE",
    "CS_SHOP_REFRESH",
    "CS_SHOP_UPDATE",
    "CS_USE_ITEM",
    "DEFAULT_ASSEMBLY",
    "DEFAULT_CONTENT_DIR",
    "DEFAULT_DATA_ROOT",
    "DEFAULT_PROTOCOL_CATALOG",
    "ERROR_CLIENT_REQUEST_PARAM",
    "ERROR_CONFIG",
    "ERROR_EXCEED_ITEM_LIMIT_COUNT",
    "ERROR_EXCEED_MAX_BUY_TIMES",
    "ERROR_MESSAGE_DATA_ILLEGAL",
    "ERROR_SHOP_GOODS_ID",
    "ERROR_SUCCESS",
    "GoodsDefinition",
    "Layer",
    "LOCAL_FREE_GOODS_ID",
    "LOCAL_FREE_PACKAGE_ID",
    "LOCAL_FREE_PRODUCT_ID",
    "NOTIFY_CHARGE_SUCCESS",
    "NOTIFY_UPDATE_MULTI_ITEM",
    "ProtobufDecodeError",
    "RESOURCE_DIAMOND",
    "RESOURCE_ENERGY",
    "RESOURCE_GOLD",
    "RESOURCE_GOSPEL",
    "RESOURCE_GOSPEL_SKIN",
    "RESOURCE_ITEM",
    "RESOURCE_MAILBOX_SKIN",
    "RESOURCE_MAIN_BACK_IMAGE",
    "RESOURCE_MINIDIAMOND",
    "RESPONSE_IDS",
    "Result",
    "Reward",
    "RmbGoodsDefinition",
    "SC_BOUGHT_ITEM_DATAS_RET",
    "SC_BOUGHT_RMB_ITEM_DATAS_RET",
    "SC_CREATE_ORDER_RET",
    "SC_ORDER_CHARGE_RET",
    "SC_SHOP_CHILD_DATAS_RET",
    "SC_SHOP_DATAS_RET",
    "SC_SHOP_PURCHASE_RET",
    "SC_SHOP_REFRESH_RET",
    "SC_SHOP_UPDATE_RET",
    "SC_USE_ITEM_RET",
    "SHOP_STATE_KEY",
    "ShopApplicationError",
    "ShopCatalog",
    "ShopConfigError",
    "ShopContentLayer",
    "default_layer",
    "handle",
    "iter_wire_fields",
    "read_varint",
]
