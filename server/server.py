#!/usr/bin/env python3
"""Small local protocol server for offline testing.

It accepts no real account credentials and does not implement payments.
"""

from __future__ import annotations

import argparse
import copy
import http.server
import json
import logging
import os
import shutil
import socket
import socketserver
import struct
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field, replace
from functools import partial
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple
from urllib.parse import unquote


# Use a bundled dependency directory when one is available.
_workspace_lib = Path(__file__).resolve().parents[1] / "tools" / "python-libs"
if _workspace_lib.is_dir():
    sys.path.insert(0, str(_workspace_lib))

from Crypto.Cipher import Blowfish  # noqa: E402

try:  # Support both package imports in tests and direct script execution.
    from .combat_attribute_resolver import CombatAttributeResolver
    from .independent_growth import IndependentGrowthLayer, bonus_for_hero as independent_hero_bonus
    from .player_progression import PlayerProgression, RESPONSE_IDS as PLAYER_PROGRESSION_RESPONSE_IDS
    from .player_progression import enabled as newcomer_enabled, REQUEST_FEATURES as PROGRESSION_REQUEST_FEATURES
    from .activity_content import RESPONSE_IDS as ACTIVITY_RESPONSE_IDS
    from .activity_content import ActivityContentLayer
    from .archive_content import RESPONSE_IDS as ARCHIVE_RESPONSE_IDS
    from .archive_content import ArchiveContentLayer
    from .battle_settlement import BattleSettlementLayer
    from .battle_settlement import Reward as BattleReward
    from .battle_settlement import SettlementResult as BattleSettlementResult
    from .collectibles import RESPONSE_IDS as COLLECTIBLE_RESPONSE_IDS
    from .farm_content import FarmContentLayer, RESPONSE_IDS as FARM_RESPONSE_IDS
    from .race_content import RaceContentLayer, RESPONSE_IDS as RACE_RESPONSE_IDS
    from .collectibles import CollectiblesLayer, GOSPEL_DECORATION_LINK_REVISION, GOSPEL_DECORATION_LINK_REVISION_KEY
    from .collectibles import NOTIFY_UPDATE_RUNE
    from .content_profile import (
        ALL_UNLOCKED_PROFILE_MODE,
        ALL_UNLOCKED_PROFILE_REVISION,
        ContentCatalog,
        DAILY_CITY_HERO_SCENE_IDS,
        GOSPEL_PROGRESS_REVISION,
        HERO_PROGRESS_REVISION,
        MAX_GOSPEL_BREAK_LEVEL,
        seed_all_unlocked_profile,
    )
    from .content_responses import RESPONSE_IDS as CONTENT_RESPONSE_IDS
    from .content_responses import ContentResponseLayer
    from .energy_content import EnergyRecoveryLayer
    from .energy_content import LAST_RECOVER_ENERGY_TIME_KEY
    from .offline_economy import (EconomyError, LEGACY_ECONOMY, NORMAL_ECONOMY, SCALAR_RESOURCES,
                                  SupplyCatalog, initialize_normal_profile, synchronize_tickets,
                                  capture_existing_economy, restore_existing_economy)
    from .formation_content import RESPONSE_IDS as FORMATION_RESPONSE_IDS
    from .formation_content import FormationApplicationError
    from .formation_content import FormationContentLayer
    from .formation_names import default_formation_name, normalize_stored_formation_name
    from .gospel_progression import RESPONSE_IDS as GOSPEL_PROGRESSION_RESPONSE_IDS
    from .gospel_progression import GospelProgressionLayer
    from .gospel_reset import GospelResetLayer
    from .hero_progression import RESPONSE_IDS as HERO_PROGRESSION_RESPONSE_IDS
    from .hero_progression import HeroProgressionLayer
    from .gacha import CS_DRAW_GOSPEL as GACHA_REQUEST_ID
    from .gacha import GachaEngine
    from .life_content import RESPONSE_IDS as LIFE_RESPONSE_IDS
    from .life_content import LifeContentLayer
    from .lottery import CS_ACTIVITY_LOTTERY as LOTTERY_REQUEST_ID
    from .lottery import LotteryEngine
    from .inventory_content import (
        make_item_datas_packet,
        make_update_item_packet,
        normalize_inventory,
        seed_inventory,
    )
    from .lilia_content import RESPONSE_IDS as LILIA_RESPONSE_IDS
    from .lilia_content import LiliaContentLayer
    from .oracle_content import CS_INVITE_HERO_TO_SCENE
    from .oracle_content import RESPONSE_IDS as ORACLE_RESPONSE_IDS
    from .oracle_content import OracleContentLayer
    from .passport_content import RESPONSE_IDS as PASSPORT_RESPONSE_IDS
    from .passport_content import PassportContentLayer
    from .private_equip_content import RESPONSE_IDS as PRIVATE_EQUIP_RESPONSE_IDS
    from .private_equip_content import PrivateEquipContentLayer
    from .profile_customization import (
        DEFAULT_HEAD_IMAGE_ID,
        RESPONSE_IDS as PROFILE_CUSTOMIZATION_RESPONSE_IDS,
        ProfileCustomizationLayer,
    )
    from .rewards import RESPONSE_IDS as REWARD_RESPONSE_IDS
    from .rewards import Layer as RewardLayer
    from .runtime_endpoints import ProtocolEndpointRuntime
    from .shop_content import RESPONSE_IDS as SHOP_RESPONSE_IDS
    from .shop_content import ShopCatalog, ShopContentLayer, month_card_values
    from .soul_gacha import RESPONSE_IDS as SOUL_GACHA_RESPONSE_IDS
    from .soul_gacha import SoulGachaEngine
    from .spring_content import RESPONSE_IDS as SPRING_RESPONSE_IDS
    from .spring_content import SpringContentLayer
    from .special_battles import RESPONSE_IDS as SPECIAL_BATTLE_RESPONSE_IDS
    from .special_battles import SpecialBattlesLayer
    from .state import PersistentStateStore
    from .teaching import RESPONSE_IDS as TEACHING_RESPONSE_IDS
    from .teaching import TeachingStateLayer
    from .tower_battles import RESPONSE_IDS as TOWER_BATTLE_RESPONSE_IDS
    from .tower_battles import TowerBattlesLayer
    from .tower_progression import RESPONSE_IDS as TOWER_PROGRESSION_RESPONSE_IDS
    from .tower_progression import TowerProgressionLayer
except ImportError:  # pragma: no cover - used by ``python server/server.py``
    _server_dir = str(Path(__file__).resolve().parent)
    if _server_dir not in sys.path:
        sys.path.insert(0, _server_dir)
    from combat_attribute_resolver import CombatAttributeResolver  # type: ignore
    from independent_growth import IndependentGrowthLayer, bonus_for_hero as independent_hero_bonus
    from player_progression import PlayerProgression, RESPONSE_IDS as PLAYER_PROGRESSION_RESPONSE_IDS
    from player_progression import enabled as newcomer_enabled, REQUEST_FEATURES as PROGRESSION_REQUEST_FEATURES
    from activity_content import RESPONSE_IDS as ACTIVITY_RESPONSE_IDS  # type: ignore
    from activity_content import ActivityContentLayer  # type: ignore
    from archive_content import RESPONSE_IDS as ARCHIVE_RESPONSE_IDS  # type: ignore
    from archive_content import ArchiveContentLayer  # type: ignore
    from battle_settlement import BattleSettlementLayer  # type: ignore
    from battle_settlement import Reward as BattleReward  # type: ignore
    from battle_settlement import SettlementResult as BattleSettlementResult  # type: ignore
    from collectibles import RESPONSE_IDS as COLLECTIBLE_RESPONSE_IDS  # type: ignore
    from farm_content import FarmContentLayer, RESPONSE_IDS as FARM_RESPONSE_IDS
    from race_content import RaceContentLayer, RESPONSE_IDS as RACE_RESPONSE_IDS
    from collectibles import CollectiblesLayer, GOSPEL_DECORATION_LINK_REVISION, GOSPEL_DECORATION_LINK_REVISION_KEY  # type: ignore
    from collectibles import NOTIFY_UPDATE_RUNE  # type: ignore
    from content_profile import (  # type: ignore
        ALL_UNLOCKED_PROFILE_MODE,
        ALL_UNLOCKED_PROFILE_REVISION,
        ContentCatalog,
        DAILY_CITY_HERO_SCENE_IDS,
        GOSPEL_PROGRESS_REVISION,
        HERO_PROGRESS_REVISION,
        MAX_GOSPEL_BREAK_LEVEL,
        seed_all_unlocked_profile,
    )
    from content_responses import RESPONSE_IDS as CONTENT_RESPONSE_IDS  # type: ignore
    from content_responses import ContentResponseLayer  # type: ignore
    from energy_content import EnergyRecoveryLayer  # type: ignore
    from energy_content import LAST_RECOVER_ENERGY_TIME_KEY  # type: ignore
    from offline_economy import (EconomyError, LEGACY_ECONOMY, NORMAL_ECONOMY, SCALAR_RESOURCES,
                                 SupplyCatalog, initialize_normal_profile, synchronize_tickets,
                                 capture_existing_economy, restore_existing_economy)
    from formation_content import RESPONSE_IDS as FORMATION_RESPONSE_IDS  # type: ignore
    from formation_content import FormationApplicationError  # type: ignore
    from formation_content import FormationContentLayer  # type: ignore
    from formation_names import (  # type: ignore
        default_formation_name,
        normalize_stored_formation_name,
    )
    from gospel_progression import (  # type: ignore
        RESPONSE_IDS as GOSPEL_PROGRESSION_RESPONSE_IDS,
    )
    from gospel_progression import GospelProgressionLayer  # type: ignore
    from gospel_reset import GospelResetLayer  # type: ignore
    from hero_progression import (  # type: ignore
        RESPONSE_IDS as HERO_PROGRESSION_RESPONSE_IDS,
    )
    from hero_progression import HeroProgressionLayer  # type: ignore
    from gacha import CS_DRAW_GOSPEL as GACHA_REQUEST_ID  # type: ignore
    from gacha import GachaEngine  # type: ignore
    from life_content import RESPONSE_IDS as LIFE_RESPONSE_IDS  # type: ignore
    from life_content import LifeContentLayer  # type: ignore
    from lottery import CS_ACTIVITY_LOTTERY as LOTTERY_REQUEST_ID  # type: ignore
    from lottery import LotteryEngine  # type: ignore
    from inventory_content import (  # type: ignore
        make_item_datas_packet,
        make_update_item_packet,
        normalize_inventory,
        seed_inventory,
    )
    from lilia_content import RESPONSE_IDS as LILIA_RESPONSE_IDS  # type: ignore
    from lilia_content import LiliaContentLayer  # type: ignore
    from oracle_content import CS_INVITE_HERO_TO_SCENE  # type: ignore
    from oracle_content import RESPONSE_IDS as ORACLE_RESPONSE_IDS  # type: ignore
    from oracle_content import OracleContentLayer  # type: ignore
    from passport_content import RESPONSE_IDS as PASSPORT_RESPONSE_IDS  # type: ignore
    from passport_content import PassportContentLayer  # type: ignore
    from private_equip_content import (  # type: ignore
        RESPONSE_IDS as PRIVATE_EQUIP_RESPONSE_IDS,
    )
    from private_equip_content import PrivateEquipContentLayer  # type: ignore
    from profile_customization import (  # type: ignore
        DEFAULT_HEAD_IMAGE_ID,
        RESPONSE_IDS as PROFILE_CUSTOMIZATION_RESPONSE_IDS,
        ProfileCustomizationLayer,
    )
    from rewards import RESPONSE_IDS as REWARD_RESPONSE_IDS  # type: ignore
    from rewards import Layer as RewardLayer  # type: ignore
    from runtime_endpoints import ProtocolEndpointRuntime  # type: ignore
    from shop_content import RESPONSE_IDS as SHOP_RESPONSE_IDS  # type: ignore
    from shop_content import ShopCatalog, ShopContentLayer, month_card_values  # type: ignore
    from soul_gacha import RESPONSE_IDS as SOUL_GACHA_RESPONSE_IDS  # type: ignore
    from soul_gacha import SoulGachaEngine  # type: ignore
    from spring_content import RESPONSE_IDS as SPRING_RESPONSE_IDS  # type: ignore
    from spring_content import SpringContentLayer  # type: ignore
    from special_battles import (  # type: ignore
        RESPONSE_IDS as SPECIAL_BATTLE_RESPONSE_IDS,
    )
    from special_battles import SpecialBattlesLayer  # type: ignore
    from state import PersistentStateStore  # type: ignore
    from teaching import RESPONSE_IDS as TEACHING_RESPONSE_IDS  # type: ignore
    from teaching import TeachingStateLayer  # type: ignore
    from tower_battles import RESPONSE_IDS as TOWER_BATTLE_RESPONSE_IDS  # type: ignore
    from tower_battles import TowerBattlesLayer  # type: ignore
    from tower_progression import (  # type: ignore
        RESPONSE_IDS as TOWER_PROGRESSION_RESPONSE_IDS,
    )
    from tower_progression import TowerProgressionLayer  # type: ignore


LOG = logging.getLogger("local-game-server")
MAX_FRAME = 4 * 1024 * 1024
OFFLINE_CONTENT_REVISION = 8


def _frame_key() -> bytes:
    """Load the locally authorized protocol key without storing it in source."""

    value = os.environ.get("MTE_FRAME_KEY")
    if not value:
        raise RuntimeError("MTE_FRAME_KEY must be set for encrypted protocol frames")
    key = value.encode("utf-8")
    if not 4 <= len(key) <= 56:
        raise ValueError("MTE_FRAME_KEY must encode to 4-56 bytes")
    return key

CS_LOGIN = 0
SC_LOGIN_RET = 1
CS_CREATE_ROLE = 4
SC_CREATE_ROLE_RET = 5
CS_SELECT_ROLE = 6
SC_SELECT_ROLE_RET = 7
CS_HEARTBEAT = 8
SC_HEARTBEAT_RET = 9
CS_RECONNECT = 138
SC_RECONNECT_RET = 139
SC_HERO_DATAS = 13
SC_GOSPEL_DATAS = 14
SC_CHAPTER_DATAS = 15
SC_PHALANX_DATAS = 16
CS_PHALANX_DATAS = 49
SC_PROFESSION_DATAS = 464
SC_BRANCH_GUIDE_DATAS = 618
SC_TEACH_GUIDE_DATAS = 936
CS_START_BATTLE = 19
SC_START_BATTLE_RET = 20
CS_END_BATTLE = 21
SC_END_BATTLE_RET = 22
NOTIFY_UPDATE_ROLE_PROP = 25
NOTIFY_UPDATE_DUNGEON = 27
NOTIFY_UPDATE_LAST_RECOVER_ENERGY_TIME = 935
CS_REQUEST_LAST_RECOVER_ENERGY_TIME = 1226
SC_LAST_RECOVER_ENERGY_TIME_RET = 1227
CS_DRAW_GOSPEL = 39
SC_DRAW_GOSPEL_RET = 40
CS_READ_PLOT_OVER = 514
SC_READ_PLOT_OVER_RET = 515
CS_START_TASK_BATTLE = 438
SC_START_TASK_BATTLE_RET = 439
CS_END_TASK_BATTLE = 440
SC_END_TASK_BATTLE_RET = 441
CS_REQUEST_TASK_BATTLE_DATAS = 444
SC_TASK_BATTLE_DATAS_RET = 445
CS_GUIDE_STEP_MOVE = 456
SC_GUIDE_STEP_MOVE_RET = 457
CS_REQUEST_FRIEND_DATAS = 195
SC_FRIEND_DATAS_RET = 196
CS_REQUEST_CHOSEN_DUNGEON_IDS = 867
SC_CHOSEN_DUNGEON_IDS_RET = 868
CS_REQUEST_FIRST_CHARGE = 928
SC_FIRST_CHARGE_RET = 929
CS_REQUEST_IN_USE_ASSIST_GOSPEL = 981
SC_REQUEST_IN_USE_ASSIST_GOSPEL_RET = 982
CS_UNLOCK_TEACH_GUIDE = 937
SC_UNLOCK_TEACH_GUIDE_RET = 938
CS_REQUEST_PASSPORT_DATA = 906
SC_REQUEST_PASSPORT_DATA_RET = 907
CS_REQUEST_HERO_APPEARANCE_DATAS = 292
SC_HERO_APPEARANCE_DATAS_RET = 293
CS_REQUEST_CRAWL_TOWER_PERIOD_DATA = 580
SC_CRAWL_TOWER_PERIOD_DATA_RET = 581

# ``a_people.bytes`` contains six ordinary witches plus the hidden but fully
# battle-backed Lillia row 3000000.  The local all-unlocked profile deliberately
# exposes all seven; 3013001 remains a gospel ID and would make
# RoleData.TalentData.LoadTalent dereference a missing a_people row.
DEFAULT_HERO_ID = 3000001
VALID_LOCAL_HERO_IDS = frozenset({3000000, *range(3000001, 3000007)})
# 3900000 is the initial no-profession sentinel from a_initial.  The full
# profile assigns 3900001..3900006 only after their complete profession trees
# can be sent; arbitrary IDs make the runtime query missing skill-group nodes.
DEFAULT_PROFESSION_ID = 3900000
DEFAULT_TUTORIAL_GOSPEL_ID = 3013023
LOCAL_CHAPTER_ID = 3930001
LOCAL_DUNGEON_IDS = (8600011, 8600012, 8600013)
LOCAL_DUNGEON_NEXT = {
    8600011: 8600012,
    8600012: 8600013,
    8600013: 8600015,
}
# The bundled a_dungeon table marks 8600012 as dungeon_type=1 and assigns
# story package 5801003.  BattleMainFieldView still wraps that story in a
# StartBattle/EndBattle pair, but StoryLineManager reports the finished plot
# first.  Treat that earlier, authoritative event as a completion checkpoint:
# old runtimes can throw while constructing the following CS_EndBattle and
# would otherwise leave BattleMainFieldView._LoadViewShow waiting at 99%.
STORY_ONLY_DUNGEON_BY_PLOT = {
    5801003: 8600012,
}

# Kept as an empty compatibility hook for replay tooling.  Every known game
# request now receives either a hand-written or generated response.
IGNORED_REQUESTS = set()

# These requests are issued by LoginManager.InitData.  Several are odd
# command IDs, so the old even-ID fallback did not send their adjacent
# response and the runtime timed out its Promise batch after 30 seconds.
INITIAL_EMPTY_RESPONSES = {
    CS_REQUEST_FRIEND_DATAS: SC_FRIEND_DATAS_RET,
    125: 126,  # task data
    391: 392,  # architecture data
    517: 518,  # hero special event data
    1385: 1386,  # local equipment data
    1393: 1394,  # local equipment stage task data
    1409: 1410,  # gospel skin status
    CS_REQUEST_IN_USE_ASSIST_GOSPEL: SC_REQUEST_IN_USE_ASSIST_GOSPEL_RET,
}

CR_REQUEST_ENTER_QUEUE = 30000
RC_ENTER_QUEUE_RET = 30001
NOTIFY_GAME_SERVER_READY = 30002


def encode_varint(value: int) -> bytes:
    """Encode an unsigned protobuf varint."""

    if value < 0:
        value &= (1 << 64) - 1
    out = bytearray()
    while value > 0x7F:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value)
    return bytes(out)


def field_varint(number: int, value: int) -> bytes:
    return encode_varint((number << 3) | 0) + encode_varint(value)


def field_bytes(number: int, value: bytes) -> bytes:
    return encode_varint((number << 3) | 2) + encode_varint(len(value)) + value


def field_string(number: int, value: str) -> bytes:
    return field_bytes(number, value.encode("utf-8"))


def field_message(number: int, value: bytes) -> bytes:
    return field_bytes(number, value)


def parse_fields(data: bytes) -> Dict[int, List[object]]:
    """Parse the protobuf wire types used by login/queue messages.

    Unknown fields are retained as raw bytes or integers.  This is not a
    replacement for generated protobuf classes; it is enough for the small
    local handshake and keeps the server dependency-free apart from Blowfish.
    """

    result: Dict[int, List[object]] = {}
    offset = 0
    length = len(data)
    while offset < length:
        tag, offset = read_varint(data, offset)
        number, wire = tag >> 3, tag & 7
        if number == 0:
            raise ValueError("protobuf field number 0")
        if wire == 0:
            value, offset = read_varint(data, offset)
        elif wire == 1:
            if offset + 8 > length:
                raise ValueError("truncated fixed64 field")
            value = data[offset : offset + 8]
            offset += 8
        elif wire == 2:
            size, offset = read_varint(data, offset)
            end = offset + size
            if end > length:
                raise ValueError("truncated length-delimited field")
            value = data[offset:end]
            offset = end
        elif wire == 5:
            if offset + 4 > length:
                raise ValueError("truncated fixed32 field")
            value = data[offset : offset + 4]
            offset += 4
        else:
            raise ValueError(f"unsupported protobuf wire type {wire}")
        result.setdefault(number, []).append(value)
    return result


def read_varint(data: bytes, offset: int) -> Tuple[int, int]:
    value = 0
    shift = 0
    while offset < len(data) and shift < 70:
        current = data[offset]
        offset += 1
        value |= (current & 0x7F) << shift
        if not current & 0x80:
            return value, offset
        shift += 7
    raise ValueError("invalid protobuf varint")


def first_string(
    fields: Dict[int, List[object]], number: int, default: str = ""
) -> str:
    value = fields.get(number, [b""])[0]
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def first_int(fields: Dict[int, List[object]], number: int, default: int = 0) -> int:
    value = fields.get(number, [default])[0]
    return int(value) if isinstance(value, int) else default


def make_role_info(role_id: int, role_name: str) -> bytes:
    return b"".join(
        (
            field_varint(1, role_id),
            field_string(2, role_name),
            field_varint(3, 1),
            field_varint(4, 0),
        )
    )


def make_login_ret(
    account: str,
    role_id: int,
    host: str,
    port: int,
    token: str,
    role_name: str = "Local Guest",
) -> bytes:
    # Msg.SC_LoginRet.  The non-empty fields are deliberately conservative so
    # the runtime can construct UserData and proceed to role selection.
    return b"".join(
        (
            field_message(2, make_role_info(role_id, role_name)),
            field_varint(5, int(time.time())),
            field_string(10, account),
            field_string(14, token),
            field_string(15, host),
            field_varint(16, port),
        )
    )


def make_reconnect_ret(account: str, role_id: int) -> bytes:
    """Acknowledge reconnect with the profile bound to this TCP session."""

    return b"".join(
        (
            field_varint(1, 0),
            field_string(2, account),
            field_varint(3, role_id),
            field_varint(4, int(time.time())),
        )
    )


def make_create_role_ret(role_id: int, role_name: str) -> bytes:
    return b"".join(
        (
            field_varint(2, role_id),
            field_string(3, role_name),
            field_varint(4, 1),
            field_varint(5, 0),
        )
    )


def make_select_role_ret(
    account: str,
    role_id: int,
    role_name: str,
    profile: Optional[Dict[str, object]] = None,
) -> bytes:
    # Msg.SC_SelectRoleRet.  Empty repeated sections are valid protobuf and
    # let the runtime load its local tables before more handlers are mapped.
    profile = profile or {}
    model_id = int(profile.get("main_model_id", DEFAULT_HERO_ID) or DEFAULT_HERO_ID)
    if model_id not in VALID_LOCAL_HERO_IDS:
        model_id = DEFAULT_HERO_ID
    level = int(profile.get("level", 1))
    exp = int(profile.get("exp", 0))
    vip_level = int(profile.get("vip_level", 0))
    diamond = int(profile.get("diamond", 0))
    gold = int(profile.get("gold", 1000))
    energy = int(profile.get("energy", 100))
    minidiamond = int(profile.get("minidiamond", 0))
    single_drawn_num = int(profile.get("single_drawn_num", 0))
    draw_num = int(profile.get("draw_num", 0))
    all_unlocked = bool(profile.get("all_content_unlocked"))
    sophisticate = max(
        int(profile.get("sophisticate", 0)),
        6500 if all_unlocked else 0,
    )
    spring_status = max(
        int(profile.get("spring_status", 0)),
        1 if all_unlocked else 0,
    )
    create_time = max(1, int(profile.get("created_at", 1)))
    back_image_id = int(
        profile.get("background_id", profile.get("back_image_id", 0)) or 0
    )
    head_image_id = int(profile.get("head_image_id") or DEFAULT_HEAD_IMAGE_ID)
    head_frame_id = int(profile.get("head_frame_id") or 0)
    last_dungeon_id = int(profile.get("last_dungeon_id") or 0)
    guide_step = int(profile.get("guide_step") or 0)
    if guide_step == 0 and not newcomer_enabled(profile):
        # Migrate profiles created before guide_step became a first-class
        # field.  The old recorder kept every observed step in a dictionary.
        guide_steps = profile.get("guide_steps", {})
        if isinstance(guide_steps, dict):
            guide_step = max(
                (
                    int(step)
                    for step, reached in guide_steps.items()
                    if reached and str(step).isdigit()
                ),
                default=0,
            )
    month_card_projection = b""
    if profile.get("local_month_card"):
        card_expires, card_count, card_taken, _ = month_card_values(profile)
        month_card_projection = (field_varint(21, card_expires) + field_varint(47, card_count)
                                 + field_varint(48, card_taken))
    return b"".join(
        (
            field_varint(2, role_id),
            field_string(3, role_name),
            field_varint(4, model_id),
            field_varint(5, exp),
            field_varint(6, level),
            field_varint(7, vip_level),
            field_varint(8, diamond),
            field_varint(9, gold),
            field_varint(10, 1),
            field_varint(11, energy),
            field_varint(12, minidiamond),
            field_varint(15, head_image_id),
            field_varint(19, single_drawn_num),
            field_varint(20, int(time.time())),
            field_varint(22, last_dungeon_id),
            field_varint(24, guide_step),
            field_varint(25, 1) if profile.get("has_changed_name") else b"",
            field_string(28, account),
            field_varint(30, sophisticate),
            field_varint(35, spring_status),
            field_varint(38, create_time),
            field_varint(43, draw_num),
            field_string(45, "Local"),
            field_varint(49, head_frame_id),
            field_varint(51, back_image_id),
            month_card_projection,
        )
    )


def make_heartbeat_ret(role_id: int) -> bytes:
    return b"".join(
        (
            field_varint(2, role_id),
            field_varint(3, int(time.time())),
        )
    )


def make_local_hero(
    hero_uid: int = 1000000001,
    hero_id: int = DEFAULT_HERO_ID,
    hero: Optional[Dict[str, object]] = None,
    attributes: Optional[Mapping[str, int]] = None,
    resolved_combat_power: Optional[int] = None,
) -> bytes:
    """Build the smallest hero object that can be used by the fight scene.

    The model/appearance ID is present in the bundled runtime resources.  The
    remaining values are deliberately benign; the local server is not trying
    to recreate the source account, only to give the runtime a non-null
    formation and a resolvable visual hero.
    """

    hero = hero or {}
    hero_uid = int(hero.get("uid", hero_uid))
    hero_id = int(hero.get("id", hero_id))
    if hero_id not in VALID_LOCAL_HERO_IDS:
        hero_id = DEFAULT_HERO_ID
    state = int(hero.get("state", 0))
    appearance_id = int(hero.get("appearance_id", hero_id))
    if appearance_id not in VALID_LOCAL_HERO_IDS:
        appearance_id = hero_id
    combat_power = int(
        resolved_combat_power
        if resolved_combat_power is not None
        else hero.get("combat_power", 100)
    )
    profession_id = int(hero.get("profession_id", DEFAULT_PROFESSION_ID))
    if profession_id <= 0:
        profession_id = DEFAULT_PROFESSION_ID
    active_skills = hero.get("active_skills", hero.get("skills", []))
    if not isinstance(active_skills, list):
        active_skills = []
    skill_payloads = []
    for skill in active_skills:
        if not isinstance(skill, dict):
            continue
        skill_id = int(skill.get("id") or 0)
        if skill_id <= 0:
            continue
        skill_payloads.append(
            field_message(
                9,
                b"".join(
                    (
                        field_varint(1, skill_id),
                        field_varint(2, max(1, int(skill.get("level", 1)))),
                        field_varint(
                            3,
                            max(
                                0,
                                int(
                                    skill.get(
                                        "valid_level",
                                        skill.get("level", 1),
                                    )
                                ),
                            ),
                        ),
                    )
                ),
            )
        )
    return b"".join(
        (
            field_varint(1, hero_uid),  # HeroUId
            field_varint(2, hero_id),  # HeroId
            field_varint(6, max(0, int(hero.get("fondness", 0)))),
            field_varint(7, max(0, int(hero.get("fondness_level", 0)))),
            field_varint(8, max(0, int(hero.get("fondness_daily_increment", 0)))),
            b"".join(skill_payloads),
            field_varint(19, state),  # State
            b''.join(field_message(21, field_varint(1, plate.get('Round', 0)) +
                b''.join(field_message(2, field_varint(2, prop['Type']) +
                    field_varint(3, prop['Level']) + field_varint(4, prop['PlusValue'] & ((1 << 64) - 1)))
                    for prop in plate.get('PropList', []))) for plate in hero.get('plates_food_prop_list', [])),
            field_varint(22, appearance_id),  # AppearanceId
            field_varint(24, max(1, int(hero.get("birth_time", 1)))),
            field_varint(25, max(0, int(hero.get("skin_uid", 0)))),
            field_varint(27, profession_id),  # ProfessionId
            field_varint(28, max(0, int(hero.get("scene_id", 0)))),
            field_varint(29, max(0, int(hero.get("feeling", 0)))),
            field_varint(31, max(0, int(hero.get("daily_dialog_num", 0)))),
            field_varint(
                32,
                max(
                    0,
                    int(
                        hero.get(
                            "fondness_fake_level",
                            hero.get("fondness_level", 0),
                        )
                    ),
                ),
            ),
            field_varint(33, max(0, int(hero.get("special_status", 0)))),
            field_varint(34, max(0, int(hero.get('daily_feed_num', 0)))),
            b''.join(field_message(39, field_varint(1, int(food)) + field_varint(2, int(count)))
                     for food, count in sorted(hero.get('eaten_food_map', {}).items(), key=lambda r: int(r[0]))),
            field_message(35, make_attribute(attributes or {})),
            field_varint(37, combat_power),  # CombatPower
            field_varint(43, max(0, int(hero.get("fondness_switch", 0)))),
            field_varint(45, max(0, int(hero.get("has_dated", 1)))),
            field_varint(50, max(0, int(hero.get("mood", 100)))),
            field_varint(51, max(0, int(hero.get("decoration_presets_id", 0)))),
            field_varint(52, max(0, int(hero.get("rune_presets_id", 0)))),
        )
    )


def make_hero_datas(
    profile: Optional[Dict[str, object]] = None,
    combat_resolver: Optional[CombatAttributeResolver] = None,
) -> bytes:
    """Send the local guest's minimal hero collection (SC_HeroDatas)."""

    rows = profile.get("heroes") if profile else None
    if not isinstance(rows, list) or not rows:
        rows = [{"uid": 1000000001, "id": DEFAULT_HERO_ID}]
    payloads = []
    for row in rows:
        hero = row if isinstance(row, dict) else None
        snapshot = (
            combat_resolver.resolve_hero(
                hero or {},
                all_unlocked=bool(profile and profile.get("all_content_unlocked")),
                profile=profile,
            )
            if combat_resolver is not None and hero is not None
            else None
        )
        payloads.append(
            field_message(
                1,
                make_local_hero(
                    hero=hero,
                    attributes=snapshot.wire_attributes if snapshot else None,
                    resolved_combat_power=snapshot.combat_power if snapshot else None,
                ),
            )
        )
    return b"".join(payloads)


def make_attribute(attributes: Mapping[str, int]) -> bytes:
    """Pinned ten fields, plus optional offline-only flat growth metadata."""

    fields = (
        "Hp",
        "Atk",
        "Weakness",
        "Crit",
        "BP",
        "Defence",
        "Hit",
        "RealDamageProb",
        "RealDamageAppendParam",
        "TreatEffect",
    )
    body = b"".join(
        field_varint(index, max(0, int(attributes.get(name, 0))))
        for index, name in enumerate(fields, 1)
        if int(attributes.get(name, 0)) != 0
    )
    growth = [int(attributes.get(name, 0)) for name in ("_GrowthAtk", "_GrowthHp", "_GrowthDef")]
    if any(growth):
        if any(n < 0 or n > 1_000_000 for n in growth):
            raise ValueError("invalid offline growth attribute")
        body += field_message(125, field_varint(1, 1) + b"".join(
            field_varint(i, n) for i, n in enumerate(growth, 2)))
    return body


def make_local_gospel(
    gospel: Optional[Dict[str, object]] = None,
    attributes: Optional[Mapping[str, int]] = None,
    resolved_combat_power: Optional[int] = None,
) -> bytes:
    """Build the persisted subset consumed by ``FuyingData.Init``."""

    gospel = gospel or {}
    gospel_id = int(gospel.get("id", DEFAULT_TUTORIAL_GOSPEL_ID))
    if gospel_id <= 0:
        gospel_id = DEFAULT_TUTORIAL_GOSPEL_ID
    level = max(1, int(gospel.get("level", 1)))
    appearance_id = int(gospel.get("appearance_id", gospel_id)) or gospel_id
    awake_level = min(6, max(1, int(gospel.get("awake_level", 1))))
    fake_awake_level = min(
        6,
        max(1, int(gospel.get("fake_awake_level", awake_level))),
    )
    active_skills = gospel.get("active_skills", gospel.get("skills", []))
    if not isinstance(active_skills, list):
        active_skills = []
    skill_payloads = []
    for skill in active_skills:
        if not isinstance(skill, dict):
            continue
        skill_id = int(skill.get("id") or 0)
        if skill_id <= 0:
            continue
        skill_payloads.append(
            field_message(
                7,
                b"".join(
                    (
                        field_varint(1, skill_id),
                        field_varint(2, max(1, int(skill.get("level", 1)))),
                        field_varint(
                            3,
                            max(
                                0,
                                int(
                                    skill.get(
                                        "valid_level",
                                        skill.get("level", 1),
                                    )
                                ),
                            ),
                        ),
                    )
                ),
            )
        )
    rewarded_fondness = gospel.get("rewarded_fondness_levels", [])
    if not isinstance(rewarded_fondness, list):
        rewarded_fondness = []
    rewarded_fondness_payload = b"".join(
        field_varint(30, max(0, int(level_value))) for level_value in rewarded_fondness
    )
    break_rewards = gospel.get("break_level_reward_taken", {})
    if not isinstance(break_rewards, dict):
        break_rewards = {}
    break_reward_payload = b"".join(
        field_message(
            36,
            b"".join(
                (
                    field_varint(1, max(0, int(break_level))),
                    field_varint(2, 1 if taken else 0),
                )
            ),
        )
        for break_level, taken in sorted(
            break_rewards.items(), key=lambda item: int(item[0])
        )
    )
    return b"".join(
        (
            field_varint(1, int(gospel.get("uid", 2000000001))),
            field_varint(2, gospel_id),
            field_varint(3, level),
            field_varint(4, int(gospel.get("exp", 0))),
            field_varint(
                5,
                int(gospel.get("level_limit", MAX_GOSPEL_BREAK_LEVEL)),
            ),
            field_varint(6, int(gospel.get("energy", 100))),
            b"".join(skill_payloads),
            field_varint(11, max(0, int(gospel.get("fondness", 0)))),
            field_varint(12, max(0, int(gospel.get("fondness_level", 0)))),
            field_varint(13, max(0, int(gospel.get("fondness_daily_increment", 0)))),
            field_varint(15, max(1, int(gospel.get("birth_time", 1)))),
            field_varint(16, int(gospel.get("is_new", 1))),
            field_varint(17, appearance_id),
            field_varint(18, int(gospel.get("fake_level", level))),
            field_varint(19, max(0, int(gospel.get("skin_uid", 0)))),
            field_varint(20, fake_awake_level),
            field_varint(
                22,
                max(0, int(gospel.get("is_fondness_limit_breakthrough", 1))),
            ),
            field_varint(23, awake_level),
            # FuyingData.GetPBE dereferences this message and uses its base
            # values directly in battle.  It must be both present and derived
            # from level/breakthrough/awakening/heart progression.
            field_message(24, make_attribute(attributes or {})),
            field_varint(25, max(0, int(gospel.get("repeated_num", 0)))),
            field_varint(
                26,
                int(
                    resolved_combat_power
                    if resolved_combat_power is not None
                    else gospel.get("combat_power", 100)
                ),
            ),
            field_varint(27, max(0, int(gospel.get("spirit_status", 0)))),
            field_varint(28, max(0, int(gospel.get("fondness_switch", 0)))),
            field_varint(29, max(0, int(gospel.get("spirit_switch", 0)))),
            rewarded_fondness_payload,
            field_varint(
                32,
                max(0, int(gospel.get("spirit_breakthrough_num", 0))),
            ),
            field_varint(33, max(0, int(gospel.get("star_up", 0)))),
            field_varint(34, max(0, int(gospel.get("spirit_level_limit", 0)))),
            field_varint(35, max(0, int(gospel.get("spirit_is_ready", 0)))),
            break_reward_payload,
            field_varint(37, int(gospel.get("appearance_id_2d", appearance_id))),
        )
    )


def make_gospel_datas(
    profile: Optional[Dict[str, object]] = None,
    combat_resolver: Optional[CombatAttributeResolver] = None,
) -> bytes:
    """Send the local account's durable star-scar collection (message 14).

    ``LoginNetMessageLogic.OnGospelDatasResp`` clears the runtime's collection
    before loading this list.  Omitting the snapshot therefore makes a gospel
    granted by the tutorial disappear from runtime memory after a restart.
    """

    rows = profile.get("gospels") if profile else None
    if not isinstance(rows, list):
        rows = []
    payloads = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        snapshot = combat_resolver.resolve_gospel(row, profile) if combat_resolver else None
        payloads.append(
            field_message(
                1,
                make_local_gospel(
                    row,
                    attributes=snapshot.wire_attributes if snapshot else None,
                    resolved_combat_power=snapshot.combat_power if snapshot else None,
                ),
            )
        )
    return b"".join(payloads)


def make_hero_appearance_datas(profile: Optional[Dict[str, object]] = None) -> bytes:
    """Return the default, empty decoration preset for every local hero.

    ``RoleData.CurAppearance`` is populated by SC_HeroAppearanceDatasRet, not
    by SC_HeroDatas.  An empty response therefore is not a valid minimal
    snapshot: HeroControl.InitData later enumerates CurAppearance.DecorationList
    and aborts scene initialization when CurAppearance is null.
    """

    rows = profile.get("heroes") if profile else None
    if not isinstance(rows, list) or not rows:
        rows = [{"uid": 1000000001, "id": DEFAULT_HERO_ID}]
    payloads = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        hero_uid = int(row.get("uid", 1000000001))
        appearance = b"".join(
            (
                field_varint(1, 0),  # Appearance.Id; 0 is the active preset.
                field_varint(3, 1),  # IsUsed
                field_string(4, "Default"),
            )
        )
        hero_appearance = b"".join(
            (
                field_varint(1, hero_uid),
                field_message(2, appearance),
            )
        )
        payloads.append(field_message(1, hero_appearance))
    return b"".join(payloads)


def make_profession_datas(
    profile: Optional[Dict[str, object]] = None,
    catalog: Optional[ContentCatalog] = None,
    combat_resolver: Optional[CombatAttributeResolver] = None,
) -> bytes:
    """Provide the dictionary entry RoleData needs for its tower attributes.

    The source login snapshot contains profession data before the runtime
    constructs HeroControl instances.  An empty SC_ProfessionDatas packet
    leaves UserData.GetProfession(ProfessionId) null, which makes the first
    GetRoleTowerAttr call abort the loading coroutine.  The protobuf message
    getters do not instantiate singular message fields on demand, however:
    the profession must carry explicit empty TalentTree and IndependentTree
    messages so the attribute code can safely keep all tower bonuses at zero.
    """

    rows = profile.get("heroes") if profile else None
    profession_ids = {
        int(row.get("profession_id", DEFAULT_PROFESSION_ID))
        for row in rows or []
        if isinstance(row, dict)
    }
    if not profession_ids:
        profession_ids = {DEFAULT_PROFESSION_ID}
    payloads = []
    for profession_id in sorted(profession_ids):
        group_rows = []
        talent_rows = []
        owner = next(
            (
                row
                for row in rows or []
                if isinstance(row, dict)
                and int(row.get("profession_id", DEFAULT_PROFESSION_ID))
                == profession_id
            ),
            None,
        )
        projection = (
            combat_resolver.profession_projection(
                owner,
                all_unlocked=bool(profile and profile.get("all_content_unlocked")),
            )
            if combat_resolver is not None and isinstance(owner, dict)
            else None
        )
        if catalog is not None:
            all_group_rows = sorted(
                (
                    row
                    for row in catalog.table("a_job_skillgroup").records
                    if int(row.get("job_id", 0)) == profession_id
                ),
                key=lambda row: (int(row.get("job_level", 0)), int(row["id"])),
            )
            all_group_ids = {int(row["id"]) for row in all_group_rows}
            all_talent_rows = sorted(
                (
                    row
                    for row in catalog.table("a_job_skill").records
                    if int(row.get("skillgroup_id", 0)) in all_group_ids
                ),
                key=lambda row: (
                    int(row.get("skillgroup_id", 0)),
                    int(row.get("number", 0)),
                    int(row["id"]),
                ),
            )
            explicit_group_ids = (
                owner.get("profession_unlocked_tree_ids")
                if isinstance(owner, dict)
                else None
            )
            if projection is not None:
                selected_group_ids = set(projection.unlocked_group_ids)
                group_rows = [
                    row for row in all_group_rows if int(row["id"]) in selected_group_ids
                ]
            elif isinstance(explicit_group_ids, list):
                selected_group_ids = {
                    int(value)
                    for value in explicit_group_ids
                    if not isinstance(value, bool)
                    and str(value).lstrip("-").isdigit()
                    and int(value) in all_group_ids
                }
                group_rows = [
                    row for row in all_group_rows if int(row["id"]) in selected_group_ids
                ]
            elif profile and profile.get("all_content_unlocked"):
                group_rows = all_group_rows

            explicit_talent_ids = (
                owner.get("profession_talent_ids")
                if isinstance(owner, dict)
                else None
            )
            if projection is not None:
                selected_talent_ids = set(projection.talent_ids)
                talent_rows = [
                    row
                    for row in all_talent_rows
                    if int(row["id"]) in selected_talent_ids
                ]
            elif isinstance(explicit_talent_ids, list):
                selected_talent_ids = {
                    int(value)
                    for value in explicit_talent_ids
                    if not isinstance(value, bool)
                    and str(value).lstrip("-").isdigit()
                }
                talent_rows = [
                    row
                    for row in all_talent_rows
                    if int(row["id"]) in selected_talent_ids
                ]
            elif isinstance(owner, dict) and "profession_level" in owner:
                try:
                    level = max(0, int(owner.get("profession_level") or 0))
                except (TypeError, ValueError, OverflowError):
                    level = 0
                talent_rows = all_talent_rows[:level]
            elif profile and profile.get("all_content_unlocked"):
                talent_rows = all_talent_rows

        current_tree_id = projection.current_group_id if projection is not None else 0
        if current_tree_id == 0 and isinstance(owner, dict):
            try:
                requested_tree_id = int(owner.get("profession_talent_tree_id") or 0)
            except (TypeError, ValueError, OverflowError):
                requested_tree_id = 0
            if any(int(row["id"]) == requested_tree_id for row in group_rows):
                current_tree_id = requested_tree_id
        if current_tree_id == 0 and group_rows:
            current_tree_id = int(group_rows[-1]["id"])
        talent_tree = field_varint(1, current_tree_id) if current_tree_id else b""
        profession = b"".join(
            (
                field_varint(1, profession_id),  # Msg.Profession.Id
                field_varint(2, current_tree_id),
                b"".join(
                    field_message(3, field_varint(1, int(row["id"])))
                    for row in talent_rows
                ),
                field_message(4, talent_tree),  # Msg.Profession.TalentTree
                field_message(5, b""),  # Msg.Profession.IndependentTree
                b"".join(field_varint(6, int(row["id"])) for row in group_rows),
            )
        )
        payloads.append(field_message(1, profession))
    rewarded_tree_ids = sorted(
        {
            int(tree_id)
            for row in rows or []
            if isinstance(row, dict)
            and isinstance(row.get("rewarded_profession_talent_tree_ids"), list)
            for tree_id in row["rewarded_profession_talent_tree_ids"]
            if not isinstance(tree_id, bool) and str(tree_id).lstrip("-").isdigit()
        }
    )
    return b"".join(payloads) + b"".join(
        field_varint(2, tree_id) for tree_id in rewarded_tree_ids
    )


def make_battle_hero(
    hero: Optional[Dict[str, object]] = None,
    combat_pos: int = 0,
) -> bytes:
    """Build the ``Msg.BattleHero`` used inside ``Msg.Phalanx``.

    A ``Msg.Hero`` and a ``Msg.BattleHero`` both start with HeroUId, which
    made the previous response look plausible on the wire.  They are not
    interchangeable: the latter has CombatPos and Gospel entries, and the
    Unity formation loader casts/reads it as BattleHero.
    """

    hero = hero or {}
    hero_uid = int(hero.get("uid", 1000000001))
    combat_pos = int(hero.get("combat_pos", combat_pos))
    return b"".join(
        (
            field_varint(1, hero_uid),  # HeroUId
            field_varint(3, combat_pos),  # CombatPos
        )
    )


def make_phalanx_datas(profile: Optional[Dict[str, object]] = None) -> bytes:
    """Send the five formation slots expected by ``BattleData``.

    ``BattleData`` starts with ``DuilieCount == 5`` and indexes both
    ``PhalanxList`` and ``PhaAutoFightList`` for all five slots before it
    recalculates that value from the first formation.  A single formation is
    therefore not a valid minimal response: it fails in the initialization
    loop even when the current formation ID is 1.

    ``CurPhalanxId`` is consumed as a zero-based list index.
    """

    heroes = profile.get("heroes") if profile else None
    if not isinstance(heroes, list) or not heroes:
        heroes = [{"uid": 1000000001, "id": DEFAULT_HERO_ID}]
    hero_by_uid = {
        int(row.get("uid", 0)): row for row in heroes if isinstance(row, dict)
    }
    phalanxes = profile.get("phalanxes") if profile else None
    if not isinstance(phalanxes, list) or not phalanxes:
        phalanxes = [
            {
                "id": 0,
                "name": default_formation_name(0),
                "hero_uids": [1000000001],
                "combat_power": 100,
            }
        ]
    source_phalanxes = [row for row in phalanxes if isinstance(row, dict)]
    if not source_phalanxes:
        source_phalanxes = [
            {
                "id": 0,
                "name": default_formation_name(0),
                "hero_uids": [1000000001],
                "combat_power": 100,
            }
        ]
    saved_auto = profile.get("auto_phalanxes") if profile else None
    saved_auto = saved_auto if isinstance(saved_auto, dict) else {}
    phalanx_payloads = []
    auto_fight_payloads = []
    for slot_index in range(5):
        # Preserve saved slots when present, then pad the remaining protocol
        # slots from the first local formation.  The runtime treats the list
        # position as the formation ID for Msg.Phalanx.
        row = dict(source_phalanxes[min(slot_index, len(source_phalanxes) - 1)])
        phalanx_id = slot_index
        hero_uids = row.get("hero_uids", [])
        if not isinstance(hero_uids, list):
            hero_uids = []
        selected_heroes = [hero_by_uid[uid] for uid in hero_uids if uid in hero_by_uid]
        if not selected_heroes:
            selected_heroes = heroes[:1]
        position_state = row.get("combat_positions")
        position_state = position_state if isinstance(position_state, dict) else {}

        def combat_position(hero: Dict[str, object], index: int) -> int:
            uid = int(hero.get("uid", 0))
            saved = position_state.get(str(uid), position_state.get(uid))
            if saved is None:
                return index if index < 3 else -1
            return int(saved)

        phalanx_payloads.append(
            b"".join(
                (
                    b"".join(
                        field_message(
                            1,
                            make_battle_hero(
                                hero={
                                    **hero,
                                    "combat_pos": combat_position(hero, index),
                                },
                                combat_pos=combat_position(hero, index),
                            ),
                        )
                        for index, hero in enumerate(selected_heroes)
                    ),
                    field_string(
                        2,
                        normalize_stored_formation_name(row.get("name"), slot_index),
                    ),
                    field_varint(3, int(row.get("combat_power", 100))),
                )
            )
        )
        # BattleData.InitPhalanxData indexes PhaAutoFightList once for every
        # formation.  An omitted repeated field is not equivalent to an empty
        # AutoList: protobuf then has no element at index 0.
        auto_rows = saved_auto.get(str(phalanx_id), saved_auto.get(phalanx_id))
        if isinstance(auto_rows, list):
            auto_entries = [
                (
                    int(item.get("hero_uid", 0)),
                    int(item.get("combat_pos", -1)),
                )
                for item in auto_rows
                if isinstance(item, dict)
                and int(item.get("hero_uid", 0)) in hero_by_uid
            ]
        else:
            auto_entries = [
                (int(hero.get("uid", 0)), combat_position(hero, index))
                for index, hero in enumerate(selected_heroes)
            ]
        auto_fight_payloads.append(
            b"".join(
                (
                    field_varint(1, phalanx_id),
                    b"".join(
                        field_message(
                            2,
                            field_varint(1, hero_uid) + field_varint(2, position),
                        )
                        for hero_uid, position in auto_entries
                    ),
                )
            )
        )
    current_id = int(profile.get("current_phalanx_id", 0)) if profile else 0
    current_id = max(0, min(current_id, 4))
    return b"".join(
        (
            b"".join(field_message(1, phalanx) for phalanx in phalanx_payloads),
            field_varint(2, current_id),  # CurPhalanxId
            field_varint(3, 0),  # PhaType
            b"".join(field_message(5, data) for data in auto_fight_payloads),
        )
    )


def dungeon_progress_values(
    profile: Optional[Mapping[str, object]],
    dungeon_id: int,
) -> Tuple[int, Tuple[int, ...]]:
    """Read persisted star and cumulative achieved-condition state."""

    if not isinstance(profile, Mapping):
        return 0, ()
    battles = profile.get("completed_battles", {})
    previous = battles.get(str(dungeon_id), battles.get(dungeon_id, {})) if isinstance(battles, Mapping) else {}
    if not isinstance(previous, Mapping):
        previous = {}
    stars = profile.get("dungeon_stars", {})
    raw_star = None
    if isinstance(stars, Mapping):
        raw_star = stars.get(str(dungeon_id), stars.get(dungeon_id))
    if raw_star is None:
        raw_star = previous.get("star", 0)
    star = max(0, min(3, int(raw_star or 0)))
    achieved_source = profile.get("dungeon_achieved_conditions", {})
    achieved_raw = (
        achieved_source.get(str(dungeon_id), achieved_source.get(dungeon_id))
        if isinstance(achieved_source, Mapping)
        else None
    )
    if achieved_raw is None:
        achieved_raw = previous.get("achieved_conditions", [])
    achieved = (
        tuple(
            int(value)
            for value in achieved_raw
            if isinstance(value, int) or str(value).isdigit()
        )
        if isinstance(achieved_raw, (list, tuple))
        else ()
    )
    return star, achieved


def make_dungeon_data(
    dungeon_id: int,
    state: int = 0,
    *,
    star: int = 0,
    achieved_conditions: Tuple[int, ...] = (),
) -> bytes:
    """Build the common Msg.Dungeon subset used by snapshots and pushes."""

    return b"".join(
        (
            field_varint(1, dungeon_id),
            field_varint(2, state),
            field_varint(3, max(0, min(3, int(star)))),
            b"".join(
                field_varint(4, max(0, int(condition_id)))
                for condition_id in achieved_conditions
            ),
            field_varint(9, 0),  # IsCovered
        )
    )


def make_notify_update_dungeon(
    role_id: int,
    finished_dungeon_id: int,
    new_dungeon_id: int = 0,
    *,
    star: int = 0,
    achieved_conditions: Tuple[int, ...] = (),
) -> bytes:
    """Build Notify_UpdateDungeon for a successful stage completion.

    The notification must arrive before SC_EndBattleRet.  The latter causes
    BattleMainFieldView.OnShow to immediately evaluate the next tutorial
    stage, so its in-memory ChapterData has to be updated first.
    """

    return b"".join(
        (
            field_varint(1, role_id),
            field_message(
                2,
                make_dungeon_data(
                    finished_dungeon_id,
                    1,
                    star=star,
                    achieved_conditions=achieved_conditions,
                ),
            ),
            (
                field_message(3, make_dungeon_data(new_dungeon_id, 0))
                if new_dungeon_id
                else b""
            ),
        )
    )


def make_chapter_datas(profile: Optional[Dict[str, object]] = None) -> bytes:
    """Send the small chapter snapshot required by the battle-map view.

    The local runtime has the complete ``a_dungeon``/``a_dungeon_map`` tables,
    but ChapterData still needs at least one server-side Dungeon record to
    choose a map progress entry.  Keep the tutorial chain local and derive
    finished states from the persisted profile so reconnects remain coherent.
    """

    profile = profile or {}
    completed = {
        int(value)
        for value in profile.get("completed_dungeons", [])
        if isinstance(value, int) or str(value).isdigit()
    }
    last_dungeon = int(profile.get("last_dungeon_id") or 0)
    if last_dungeon not in LOCAL_DUNGEON_IDS:
        last_dungeon = LOCAL_DUNGEON_IDS[0]
    last_index = LOCAL_DUNGEON_IDS.index(last_dungeon)
    dungeon_payloads = []
    for index, dungeon_id in enumerate(LOCAL_DUNGEON_IDS):
        # DungeonState.Unfinished is the protobuf default (0); completed
        # entries use Finished (1), which lets GetMaxMapProgress advance.
        state = 1 if dungeon_id in completed or index < last_index else 0
        star, achieved = dungeon_progress_values(profile, dungeon_id)
        dungeon_payloads.append(
            make_dungeon_data(
                dungeon_id,
                state,
                star=star,
                achieved_conditions=achieved,
            )
        )
    chapter = b"".join(
        (
            field_varint(1, LOCAL_CHAPTER_ID),
            field_varint(2, 0),  # MainBattleDifficultyType.Easy
            field_varint(3, 0),  # DoubleRewardRemainNum
            field_varint(4, 0),  # Progress
            field_varint(5, 0),  # Follow
        )
    )
    return b"".join(
        (
            b"".join(field_message(1, data) for data in dungeon_payloads),
            field_message(3, chapter),
            field_varint(4, 0),  # OverallProgress
            field_varint(5, 0),  # FastBattleDailyNum
        )
    )


def make_branch_guide_datas(catalog: ContentCatalog, profile: Optional[Mapping[str, Any]] = None) -> bytes:
    """Project actual branch completions for newcomers; legacy keeps its seed.

    ``UserData.CheckForceGuideStatus`` compares this list with
    ``a_guide_force.id``.  Linked ``a_guide_function`` IDs describe the UI
    steps and therefore belong to a different identifier domain.
    """

    guide_ids = sorted(int(row["id"]) for row in catalog.table("a_guide_force").records)
    if profile is not None and newcomer_enabled(profile):
        completed = set(profile.get("completed_branch_guide_ids", []))
        completed.update(int(key) for key, done in profile.get('guide_steps', {}).items()
                         if done and str(key).isdigit())
        guide_ids = sorted(set(guide_ids) & completed)
    return field_bytes(1, b"".join(encode_varint(guide_id) for guide_id in guide_ids))


def make_teach_guide_datas(catalog: ContentCatalog, profile: Optional[Mapping[str, Any]] = None) -> bytes:
    """Project actual article unlock/read flags; preserve the legacy full seed."""

    table = catalog.tables.get("a_teaching_guide")
    if table is None:
        return b""
    newcomer = profile is not None and newcomer_enabled(profile)
    unlocked = set(profile.get('unlocked_teaching_guide_ids', [])) | set(profile.get('unlocked_guides', [])) if newcomer else set(table.ids)
    read = set(profile.get('read_teaching_guide_ids', [])) | {
        r['param1'] for r in profile.get('cleared_red_dots', []) if r.get('type') == 2012} if newcomer else set(table.ids)
    return b"".join(
        field_message(
            1,
            field_varint(1, int(guide_id)) + field_varint(2, int(guide_id in read)),
        )
        for guide_id in table.ids
        if guide_id in unlocked
    )


def make_passport_data_ret() -> bytes:
    """Return a non-null, inactive passport snapshot.

    PermitData.OnNetSync dereferences SC_RequestPassportDataRet.Passport
    before checking ValidPassportGroup.  An absent field therefore raises a
    NullReferenceException even though the local account has no passport.
    """

    return field_message(1, b"")


def make_crawl_tower_period_data_ret() -> bytes:
    """Return the non-null tower closure dereferenced by ParseTowerBaseData.

    The pinned runtime immediately reads PeriodData, Passport and
    IndependentTree without null checks, then resolves ThemeId and
    TalentTreeId through local static tables.  These IDs are the latest valid
    theme and the first Ritsu profession group in the bundled TableConfig.
    """

    passport = b"".join(
        (
            field_varint(1, 0),  # Exp
            field_varint(2, 1),  # Level
            field_varint(3, 1),  # LevelLimit
        )
    )
    independent_tree = field_varint(1, 3_890_001)
    period_data = b"".join(
        (
            field_varint(1, 6_000_001),  # a_holytower.id
            field_varint(2, 1),  # TowerDifficultyType: shallow survey
            field_varint(4, 6_080_041),  # latest bundled a_holytower_theme
            field_message(6, passport),
            field_varint(17, 1),
            field_varint(18, 1),
            field_varint(20, 1),
            field_varint(21, 1),
            field_message(34, independent_tree),
            field_varint(35, 3_900_001),
            field_varint(36, 3_000_001),
        )
    )
    return b"".join(
        (
            field_message(1, period_data),
            field_varint(2, 2_592_000),
            field_varint(3, 1_209_600),
            field_varint(4, 2_592_000),
            field_varint(5, 2_592_000),
        )
    )


def make_chosen_dungeon_ids_ret(profile: Optional[Dict[str, object]] = None) -> bytes:
    """Return the persisted chosen-dungeon map."""

    selected = profile.get("selected_dungeon_ids") if profile else None
    if not isinstance(selected, list):
        selected = []
    return b"".join(
        field_message(
            1,
            field_varint(1, int(dungeon_id)) + field_varint(2, 1),
        )
        for dungeon_id in selected
    )


def record_gospel_draw(
    state: "GameState",
    account: str,
    gospel_id: int = DEFAULT_TUTORIAL_GOSPEL_ID,
) -> Tuple[bool, int]:
    """Persist one deterministic local draw and return ``(is_new, count)``.

    The forced draw embedded in plot 5801003 uses card-pool group 2.  The
    bundled ``a_initial`` table names gospel 3013023 as the starter gospel,
    and that ID is also present in group 2 of ``a_summonpool``.  Keeping this
    deterministic preserves the authored tutorial while avoiding a fake ID
    whose presentation assets do not exist.
    """

    result = {"is_new": False, "draw_num": 0}

    def mutate(profile: Dict[str, object]) -> None:
        rows = profile.setdefault("gospels", [])
        if not isinstance(rows, list):
            rows = []
            profile["gospels"] = rows
        existing = next(
            (
                row
                for row in rows
                if isinstance(row, dict) and int(row.get("id") or 0) == gospel_id
            ),
            None,
        )
        if existing is None:
            rows.append(
                {
                    "uid": 2000000001 + len(rows),
                    "id": gospel_id,
                    "level": 1,
                    "exp": 0,
                    "level_limit": 60,
                    "energy": 100,
                    "appearance_id": gospel_id,
                    "is_new": 1,
                    "combat_power": 100,
                }
            )
            result["is_new"] = True
        draw_num = int(profile.get("draw_num") or 0) + 1
        profile["draw_num"] = draw_num
        result["draw_num"] = draw_num

    state.store.mutate_account(account, mutate)
    return bool(result["is_new"]), int(result["draw_num"])


def complete_tutorial_story_from_draw(
    state: "GameState",
    account: str,
) -> int:
    """Commit tutorial dungeon 8600012 at its last reliable checkpoint.

    Plot 5801003 embeds the forced card-pool-2 summon.  On this legacy runtime,
    closing the summon result can return directly to ``BattleMainFieldView``
    without emitting ``CS_ReadPlotOver(PrsFinished)`` or ``CS_EndBattle``.
    The map then sees 8600012 as unfinished and starts the same story again.

    A successful draw is later than every required interaction in this
    story-only dungeon, so it is a safe completion checkpoint.  Restrict the
    repair to a profile whose active dungeon is 8600012; ordinary pool-2 draws
    after the tutorial must not advance chapter progress.
    """

    plot_id = 5801003
    dungeon_id = STORY_ONLY_DUNGEON_BY_PLOT[plot_id]
    completed_now = {"dungeon_id": 0}

    def mutate(profile: Dict[str, object]) -> None:
        if int(profile.get("last_dungeon_id") or 0) != dungeon_id:
            return
        now = int(time.time())
        previous_plot = profile.setdefault("plots", {}).get(str(plot_id), {})
        is_skip = (
            int(previous_plot.get("is_skip") or 0)
            if isinstance(previous_plot, dict)
            else 0
        )
        profile["plots"][str(plot_id)] = {
            "status": 1,
            "is_skip": is_skip,
            "updated_at": now,
        }
        profile.setdefault("story_progress", {})[str(plot_id)] = 1
        completed = profile.setdefault("completed_dungeons", [])
        if dungeon_id in completed:
            return
        completed.append(dungeon_id)
        profile.setdefault("completed_battles", {})[str(dungeon_id)] = {
            "result": 1,
            "target_id": 0,
            "source": "tutorial-draw-checkpoint",
            "updated_at": now,
        }
        completed_now["dungeon_id"] = dungeon_id

    state.store.mutate_account(account, mutate)
    return completed_now["dungeon_id"]


def make_draw_gospel_ret(
    request_fields: Dict[int, List[object]],
    role_id: int,
    is_new: bool,
    draw_num: int,
    gospel_id: int = DEFAULT_TUTORIAL_GOSPEL_ID,
) -> bytes:
    """Build the complete subset consumed by ``NewSummonView``.

    The generated endpoint skeleton only echoed role/card-pool scalars.  Its
    empty ``StuffList`` left ``UserData.NewFyIdList`` empty; the result view
    then dereferenced a null ``a_gospel`` row while loading effects.  One
    valid ``DrawnStuff`` entry is sufficient for the authored result flow.
    """

    drawn_stuff = b"".join(
        (
            field_varint(1, 7),  # Msg.ResourceType.Gospel
            field_varint(2, gospel_id),
            field_varint(3, 1),
            field_varint(5, 1 if is_new else 0),
        )
    )
    return b"".join(
        (
            field_varint(1, 0),  # Errorcode
            field_varint(2, role_id),
            field_varint(3, first_int(request_fields, 2, 0)),
            field_varint(4, first_int(request_fields, 3, 0)),
            field_message(5, drawn_stuff),
            field_varint(6, draw_num),
            field_varint(9, draw_num),
        )
    )


def make_read_plot_over_ret(request_fields: Dict[int, List[object]]) -> bytes:
    """Close a plot-read callback with a self-consistent ``ReadData``.

    ``CmdManager.OnSc_ReadPlotOverRet`` reads the nested record even when the
    server has no persistent plot database.  Omitting the response leaves the
    loading/plot state machine waiting forever; returning only ``Errorcode``
    makes it process an empty record.  Echo the runtime record instead and use
    ``PrsFinished`` (enum value 1) when the old runtime omitted ``Status``.
    """

    raw_read_data = request_fields.get(1, [b""])[0]
    nested_fields: Dict[int, List[object]] = {}
    if isinstance(raw_read_data, bytes) and raw_read_data:
        try:
            nested_fields = parse_fields(raw_read_data)
        except ValueError:
            nested_fields = {}

    plot_pkg_id = first_int(nested_fields, 1, 0)
    status = first_int(nested_fields, 2, 1)  # Msg.PlotReadStatus.PrsFinished
    is_skip = first_int(nested_fields, 3, 0)
    is_rewarded = first_int(nested_fields, 4, 0)
    read_data = b"".join(
        (
            field_varint(1, plot_pkg_id),
            field_varint(2, status),
            field_varint(3, is_skip),
            field_varint(4, is_rewarded),
        )
    )
    return b"".join(
        (
            field_varint(1, 0),  # Errorcode
            field_message(2, read_data),  # ReadData
            field_varint(3, first_int(request_fields, 2, 0)),  # IsSophisticate
            field_varint(5, first_int(request_fields, 3, 0)),  # ActivityId
            field_varint(6, first_int(request_fields, 4, 0)),  # SubActivityId
        )
    )


def parse_plot_read_progress(
    request_fields: Dict[int, List[object]],
) -> Tuple[int, int, int]:
    """Return ``(plot_id, status, is_skip)`` from CS_ReadPlotOver."""

    raw_read_data = request_fields.get(1, [b""])[0]
    if not isinstance(raw_read_data, bytes) or not raw_read_data:
        return 0, 0, 0
    try:
        nested_fields = parse_fields(raw_read_data)
    except ValueError:
        return 0, 0, 0
    return (
        first_int(nested_fields, 1, 0),
        first_int(nested_fields, 2, 0),
        first_int(nested_fields, 3, 0),
    )


def record_plot_progress(
    state: "GameState",
    account: str,
    request_fields: Dict[int, List[object]],
) -> int:
    """Persist plot state and return a newly completed story-only dungeon.

    PlotReadStatus.PrsFinished is enum value 1.  The fallback is deliberately
    scoped to a story package whose matching dungeon was just started, so
    replaying the story later cannot manufacture unrelated chapter progress.
    """

    plot_id, status, is_skip = parse_plot_read_progress(request_fields)
    if not plot_id:
        return 0
    completed_now = {"dungeon_id": 0}

    def mutate(profile: Dict[str, object]) -> None:
        now = int(time.time())
        old_status = int(profile.get("plots", {}).get(str(plot_id), {}).get("status", 0))
        profile.setdefault("plots", {})[str(plot_id)] = {
            "status": max(old_status, status) if newcomer_enabled(profile) else status,
            "is_skip": is_skip,
            "updated_at": now,
        }
        profile.setdefault("story_progress", {})[str(plot_id)] = max(old_status, status) if newcomer_enabled(profile) else status
        if newcomer_enabled(profile):
            if status == 1:
                completed_now["dungeon_id"] = state.complete_story_admission(profile, plot_id)
            return
        dungeon_id = STORY_ONLY_DUNGEON_BY_PLOT.get(plot_id, 0)
        if status != 1 or not dungeon_id:
            return
        if int(profile.get("last_dungeon_id") or 0) != dungeon_id:
            return
        completed = profile.setdefault("completed_dungeons", [])
        if dungeon_id in completed:
            return
        completed.append(dungeon_id)
        profile.setdefault("completed_battles", {})[str(dungeon_id)] = {
            "result": 1,
            "target_id": 0,
            "source": "plot-finished-fallback",
            "updated_at": now,
        }
        completed_now["dungeon_id"] = dungeon_id

    state.store.mutate_account(account, mutate)
    return completed_now["dungeon_id"]


def make_unlock_teach_guide_ret(request_fields: Dict[int, List[object]]) -> bytes:
    """Acknowledge the guide ID without reporting an error."""

    return field_varint(2, first_int(request_fields, 1, 0))  # TeachGuideId


def make_start_battle_ret(
    request_fields: Dict[int, List[object]],
    role_id: int,
    error_code: int = 0,
) -> bytes:
    """A successful start-battle acknowledgement for the local guest.

    The runtime sends ``DungeonId`` as field 4 in CS_StartBattle and consumes
    it as field 5 in SC_StartBattleRet.  The response handler also reads the
    role ID, so preserve both values instead of returning an empty protobuf.
    Optional artificial-team/config fields are intentionally omitted: the
    defined runtime does not read them in this path and an empty nested team
    would be less faithful than leaving the optional field absent.
    """

    requested_role = first_int(request_fields, 2, role_id) or role_id
    dungeon_id = first_int(request_fields, 4, 0)
    return b"".join(
        (
            field_varint(1, error_code),  # Errorcode
            field_varint(2, first_int(request_fields, 1, 0)),  # BattleType
            field_varint(3, requested_role),  # RoleId
            field_varint(4, first_int(request_fields, 3, 0)),  # TargetId
            field_varint(5, dungeon_id),  # DungeonId
            field_varint(7, first_int(request_fields, 5, 0)),  # IsRecoverBattle
        )
    )


def make_reward_item(
    res_type: int,
    item_id: int,
    count: int,
    *,
    para: int = 0,
    position: int = 0,
    quality: int = 0,
    value: int = 0,
) -> bytes:
    """Encode the small RewardItem record used by battle result lists."""

    return b"".join(
        (
            field_varint(1, res_type),  # ResourceType.Gold == 2
            field_varint(2, item_id),
            field_varint(3, count),
            field_varint(4, para),
            field_varint(5, position),
            field_varint(6, quality),
            field_varint(7, value),
        )
    )


def make_battle_reward_item(reward: BattleReward) -> bytes:
    return make_reward_item(
        reward.res_type,
        reward.item_id,
        reward.count,
        para=reward.para,
        position=reward.position,
        quality=reward.quality,
        value=reward.value,
    )


ROLE_PROP_FIELDS: Tuple[Tuple[str, int], ...] = (
    ("diamond", 0),
    ("minidiamond", 1),
    ("gold", 2),
    ("energy", 3),
    ("exp", 4),
)


def make_update_role_prop(role_id: int, prop_type: int, value: int) -> bytes:
    """Encode one absolute ``Notify_UpdateRoleProp`` value.

    The defined ``Msg.PropType`` enum is sequential for Diamond through Exp
    (0..4).  Absolute notifications keep the runtime cache synchronized with
    the already-committed local save and are safe to replay.
    """

    return b"".join(
        (
            field_varint(1, max(0, int(role_id))),
            field_varint(2, max(0, int(prop_type))),
            field_varint(3, max(0, int(value))),
        )
    )


def make_last_recover_energy_time(timestamp: int) -> bytes:
    """Encode the shared uint64 field used by commands 935 and 1227."""

    return field_varint(1, max(0, int(timestamp)))


def make_battle_state_notifications(
    role_id: int,
    before: Mapping[str, object],
    after: Mapping[str, object],
    collectibles: Optional[CollectiblesLayer] = None,
    combat_resolver: Optional[CombatAttributeResolver] = None,
    stage_tasks: Any = None,
) -> Tuple[Tuple[int, bytes], ...]:
    """Build runtime-cache updates for one committed battle transaction.

    ``SC_EndBattleRet`` displays reward rows but does not mutate the global
    role-property, bag, or rune caches.  The source runtime has dedicated
    handlers for commands 25, 26, and 953, so emit absolute post-transaction
    values for only the rows which changed.
    """

    notifications: List[Tuple[int, bytes]] = []
    if newcomer_enabled(after):
        old_progress = before.get('chapter_progress', {})
        new_progress = after.get('chapter_progress', {})
        overall = max(0, min(100, int(after.get('overall_progress', 0))))
        for chapter_id, progress in sorted(new_progress.items(), key=lambda row: int(row[0])):
            if old_progress.get(chapter_id) != progress:
                notifications.append((804, field_varint(1, int(chapter_id)) +
                    field_varint(2, max(0, min(100, int(progress)))) + field_varint(3, overall)))
    for key, prop_type in ROLE_PROP_FIELDS:
        old_value = max(0, int(before.get(key, 0) or 0))
        new_value = max(0, int(after.get(key, 0) or 0))
        if old_value != new_value:
            notifications.append(
                (
                    NOTIFY_UPDATE_ROLE_PROP,
                    make_update_role_prop(role_id, prop_type, new_value),
                )
            )

    old_energy_anchor = max(
        0, int(before.get(LAST_RECOVER_ENERGY_TIME_KEY, 0) or 0)
    )
    new_energy_anchor = max(
        0, int(after.get(LAST_RECOVER_ENERGY_TIME_KEY, 0) or 0)
    )
    if old_energy_anchor != new_energy_anchor:
        notifications.append(
            (
                NOTIFY_UPDATE_LAST_RECOVER_ENERGY_TIME,
                make_last_recover_energy_time(new_energy_anchor),
            )
        )

    before_inventory = normalize_inventory(before.get("inventory", {}))
    after_inventory = normalize_inventory(after.get("inventory", {}))
    for item_id in sorted(set(before_inventory) | set(after_inventory), key=int):
        row = after_inventory.get(item_id) or {**before_inventory[item_id], "count": 0}
        if before_inventory.get(item_id) != row:
            notifications.append(make_update_item_packet(role_id, row))

    if collectibles is not None:
        def rune_index(profile: Mapping[str, object]) -> Dict[int, Mapping[str, object]]:
            rows = profile.get("rune_items", [])
            if not isinstance(rows, list):
                return {}
            result: Dict[int, Mapping[str, object]] = {}
            for row in rows:
                if not isinstance(row, Mapping):
                    continue
                uid = max(0, int(row.get("rune_item_uid", 0) or 0))
                if uid:
                    result[uid] = row
            return result

        before_runes = rune_index(before)
        after_runes = rune_index(after)
        for uid in sorted(after_runes):
            row = after_runes[uid]
            if before_runes.get(uid) != row:
                notifications.append(
                    (NOTIFY_UPDATE_RUNE, collectibles.make_update_rune(row))
                )

    if combat_resolver is not None:
        if after.get("level") != before.get("level"):
            notifications.append((55, field_varint(1, role_id) + field_varint(3, int(after["level"]))
                                  + field_varint(4, int(after.get("exp", 0)))))
        if collectibles is not None and after.get("private_equipment") != before.get("private_equipment"):
            notifications.append((1386, collectibles.codec.encode("SC_GetPrivateEquipDatasRet", {
                "Errorcode":0,"EquipList":[PrivateEquipContentLayer._message(r) for r in after.get("private_equipment",[])]})))
        old_heroes = {r["uid"]:r for r in before.get("heroes",[]) if isinstance(r,Mapping)}
        old_professions = {int(r.get('profession_id',DEFAULT_PROFESSION_ID)) for r in old_heroes.values()}
        new_professions = {int(r.get('profession_id',DEFAULT_PROFESSION_ID)) for r in after.get('heroes',[]) if isinstance(r,Mapping)}-old_professions
        if new_professions:
            # UpdateHeroNotify immediately builds talent notices. Its new
            # profession must exist first; repeating SC_ProfessionDatas would
            # recreate login notice roots, so publish only incremental rows.
            profession_snapshot = make_profession_datas(after,combat_resolver.catalog,combat_resolver)
            for profession_body in parse_fields(profession_snapshot).get(1,[]):
                if first_int(parse_fields(profession_body),1,0) in new_professions:
                    notifications.append((461,field_message(1,profession_body)))
        for row in after.get("heroes",[]):
            if isinstance(row,Mapping) and (old_heroes.get(row["uid"]) != row or
                    independent_hero_bonus(before, row, combat_resolver.decorations) !=
                    independent_hero_bonus(after, row, combat_resolver.decorations)):
                resolved = combat_resolver.resolve_hero(row,all_unlocked=bool(after.get("all_content_unlocked")),profile=after)
                notifications.append((41,field_message(1,make_local_hero(
                    hero=row,attributes=resolved.wire_attributes,resolved_combat_power=resolved.combat_power))))
        if any(isinstance(row,Mapping) and row['uid'] not in old_heroes for row in after.get('heroes',[])):
            # HeroData does not initialize RoleData.CurAppearance. The actual
            # preset message must follow new HeroData before combat can read
            # its decoration list, even when that list is empty.
            appearances = (collectibles.make_hero_appearance_datas_ret(after) if collectibles is not None
                           else make_hero_appearance_datas(after))
            notifications.append((293,appearances))
        old_gospels = {r["uid"]: r for r in before.get("gospels", []) if isinstance(r, Mapping)}
        for row in after.get("gospels", []):
            if isinstance(row, Mapping) and old_gospels.get(row["uid"]) != row:
                resolved = combat_resolver.resolve_gospel(row, after)
                notifications.append((42, field_message(1, make_local_gospel(
                    row, resolved.wire_attributes, resolved.combat_power))))
                if newcomer_enabled(after) and row.get("spirit_is_ready",0) != old_gospels.get(row["uid"],{}).get("spirit_is_ready",0):
                    notifications.append((941,field_varint(1,row["uid"])+field_varint(2,row.get("spirit_is_ready",0))))
    if collectibles is not None:
        old_decorations = {r["id"]: r for r in before.get("decoration_items", [])}
        for row in after.get("decoration_items", []):
            if old_decorations.get(row["id"]) != row:
                notifications.append((291, collectibles.make_update_decoration(row)))
        if newcomer_enabled(after):
            # New entries must reach ChapterData before the result callback
            # navigates to the next stage or reevaluates a feature gate.
            new_dungeons = set(after.get("unlocked_dungeons", [])) - set(before.get("unlocked_dungeons", []))
            for dungeon_id in sorted(new_dungeons):
                notifications.append((27, field_varint(1,role_id) + field_message(3,make_dungeon_data(dungeon_id))))
            changed = [r for k,r in after.get("progression_achievements",{}).items()
                       if before.get("progression_achievements",{}).get(k) != r]
            if changed:
                notifications.append((320, b"".join(field_message(1,collectibles.codec.encode("Achieve",r)) for r in changed)))
    if stage_tasks is not None and stage_tasks.achievements(before) != stage_tasks.achievements(after):
        notifications.extend(stage_tasks.notifications(after))
    return tuple(notifications)


def parse_end_battle_stats(
    request_fields: Mapping[int, List[object]],
) -> Optional[Dict[str, int]]:
    """Decode the verified star-condition subset of CS_EndBattle.FieldData."""

    payloads = request_fields.get(6, [])
    if not payloads or not isinstance(payloads[0], bytes):
        return None
    field_data = parse_fields(payloads[0])
    return {
        "be_attacked_num": first_int(field_data, 6, 0),
        "death_num": first_int(field_data, 7, 0),
        "round_num": first_int(field_data, 9, 0),
        "round_max_harm": first_int(field_data, 56, 0),
    }


def make_end_battle_ret(
    request_fields: Dict[int, List[object]],
    role_id: int,
    previous_profile: Optional[Dict[str, object]] = None,
    settlement: Optional[BattleSettlementResult] = None,
) -> bytes:
    """Return the authoritative table-backed battle result when available."""

    requested_role = first_int(request_fields, 2, role_id) or role_id
    target_id = first_int(request_fields, 3, 0)
    dungeon_id = first_int(request_fields, 4, 0)
    result = first_int(request_fields, 5, 1)
    completed_before = set()
    if isinstance(previous_profile, dict):
        completed_before = {
            int(value)
            for value in previous_profile.get("completed_dungeons", [])
            if isinstance(value, int) or str(value).isdigit()
        }
        completed_before.update(
            int(value)
            for value in previous_profile.get("completed_battles", {})
            if str(value).isdigit()
        )
    is_first_pass = bool(result and dungeon_id not in completed_before)
    if settlement is not None:
        return b"".join(
            (
                field_varint(1, settlement.error_code),
                field_varint(2, requested_role),
                field_varint(3, target_id),
                field_varint(4, dungeon_id),
                field_varint(5, result),
                b"".join(
                    field_message(6, make_battle_reward_item(reward))
                    for reward in settlement.fixed_rewards
                ),
                field_varint(7, int(settlement.ok)),
                field_varint(8, int(settlement.is_first_pass)),
                b"".join(
                    field_varint(9, condition_id)
                    for condition_id in settlement.achieved_conditions
                ),
                b"".join(
                    field_message(10, make_battle_reward_item(reward))
                    for reward in settlement.star_rewards
                ),
                b"".join(
                    field_message(11, make_battle_reward_item(reward))
                    for reward in settlement.first_rewards
                ),
                b"".join(
                    field_message(12, make_battle_reward_item(reward))
                    for reward in settlement.random_rewards
                ),
            )
        )

    return b"".join(
        (
            field_varint(1, 0),  # Errorcode
            field_varint(2, requested_role),  # RoleId
            field_varint(3, target_id),  # TargetId
            field_varint(4, dungeon_id),  # DungeonId
            field_varint(5, result),  # Result
            field_message(6, make_reward_item(2, 0, 10))
            if is_first_pass and dungeon_id
            else b"",
            field_varint(7, 1),  # BattleVerifyResult
            field_varint(8, int(is_first_pass)),  # IsFirstPass
        )
    )


def make_guide_step_move_ret(request_fields: Dict[int, List[object]]) -> bytes:
    """Echo the guide step so local guide progression stays synchronized."""

    return field_varint(1, first_int(request_fields, 1, 0))


def make_first_charge_ret() -> bytes:
    """Return an initialized, but non-paying, first-charge data object.

    The runtime calls ``SyncNetData`` immediately and assumes the nested Data
    message exists.  An empty nested message keeps all reward maps empty and
    therefore represents the local guest's no-charge state.
    """

    return field_message(1, b"")


def _task_hero_id(profile: Optional[Dict[str, object]]) -> int:
    heroes = profile.get("heroes") if isinstance(profile, dict) else None
    if isinstance(heroes, list):
        for row in heroes:
            if isinstance(row, dict) and int(row.get("id", 0) or 0):
                return int(row["id"])
    return 1000001


def _task_enemy(profile: Optional[Dict[str, object]]) -> Tuple[int, int, int]:
    task_battles = profile.get("task_battles") if isinstance(profile, dict) else None
    if not isinstance(task_battles, dict):
        task_battles = {}
    enemy_id = int(task_battles.get("enemy_id", 1100001) or 1100001)
    enemy_level = int(task_battles.get("enemy_level", 1) or 1)
    enemy_hp = max(1, 100 * max(1, enemy_level))
    return enemy_id, enemy_level, enemy_hp


def make_task_battle_datas_ret(
    request_fields: Optional[Dict[int, List[object]]] = None,
    profile: Optional[Dict[str, object]] = None,
) -> bytes:
    """Return the smallest safe daily-task battle snapshot.

    QuestData indexes HeroDataList[0] and later looks up the enemy in the
    local ``a_task_enemy`` table.  The table shipped with this build uses
    enemy 1100001 for the first daily task, so include that entry with benign
    local values instead of sending an empty response.
    """

    request_fields = request_fields or {}
    cycle_type = first_int(request_fields, 1, 0)
    hero_id = first_int(request_fields, 2, _task_hero_id(profile)) or _task_hero_id(
        profile
    )
    enemy_id, _, enemy_hp = _task_enemy(profile)
    hero_data = b"".join(
        (
            field_varint(1, hero_id),  # HeroId
            field_varint(2, enemy_id),  # EnemyId
            field_varint(3, enemy_hp),  # EnemyHp
            field_varint(4, 100),  # HeroHp
            field_varint(5, 100),  # HeroAtk
        )
    )
    battle_data = b"".join(
        (
            field_varint(1, cycle_type),
            field_message(2, hero_data),  # HeroDataList[0]
        )
    )
    task_enemy = b"".join(
        (
            field_varint(1, enemy_id),
            field_varint(2, enemy_hp),
        )
    )
    return b"".join(
        (
            field_varint(1, cycle_type),
            field_message(2, battle_data),
            field_message(3, task_enemy),
        )
    )


def make_start_task_battle_ret(
    request_fields: Dict[int, List[object]],
    profile: Optional[Dict[str, object]] = None,
) -> bytes:
    """Return the fields read by QuestData.StartBattle.

    Task battles are server-simulated in this runtime.  A scalar-only generic
    response is insufficient because the handler immediately reads the
    nested BattleHeroData and DailyTaskEnemyList records.
    """

    cycle_type = first_int(request_fields, 1, 0)
    hero_id = first_int(request_fields, 2, _task_hero_id(profile)) or _task_hero_id(
        profile
    )
    enemy_level = first_int(request_fields, 3, 1) or 1
    main_task_id = first_int(request_fields, 4, 0)
    activity_task_id = first_int(request_fields, 7, 0)
    enemy_id, _, enemy_hp = _task_enemy(profile)
    battle_hero = b"".join(
        (
            field_varint(1, hero_id),
            field_varint(2, enemy_id),
            field_varint(3, enemy_hp),
            field_varint(4, 100),
            field_varint(5, 100),
        )
    )
    daily_enemy = field_varint(1, enemy_id) + field_varint(2, enemy_hp)
    return b"".join(
        (
            field_varint(1, 0),  # Errorcode
            field_varint(2, cycle_type),
            field_varint(3, hero_id),
            field_varint(4, enemy_level),
            field_varint(5, 100),  # Hp
            field_varint(6, 100),  # Atk
            field_varint(7, enemy_hp),
            field_varint(8, main_task_id),
            field_varint(9, 100),  # WinProbability
            field_varint(10, 1),  # Result
            field_varint(11, 0),  # SelfDamage
            field_varint(12, 0),  # EnemyDamage
            field_message(22, battle_hero),
            field_message(23, daily_enemy),
            field_varint(24, activity_task_id),
        )
    )


def make_end_task_battle_ret(request_fields: Dict[int, List[object]]) -> bytes:
    """Acknowledge task-battle completion without inventing rewards."""

    return b"".join(
        (
            field_varint(1, 0),
            field_varint(2, first_int(request_fields, 1, 0)),
            field_varint(3, first_int(request_fields, 2, 0)),
            field_varint(4, first_int(request_fields, 3, 1)),
        )
    )


def make_queue_ret(account: str) -> bytes:
    return field_string(3, account)


def make_server_ready(account: str, host: str, port: int) -> bytes:
    return b"".join(
        (
            field_string(1, account),
            field_string(2, host),
            field_varint(3, port),
        )
    )


def encrypt_frame(command_id: int, body: bytes) -> bytes:
    """Build the exact frame emitted by ByteArrayUtil.CreateCmdData."""

    inner_length = 2 + len(body)
    plain = struct.pack(">I", inner_length) + struct.pack(">H", command_id) + body
    padding = 8 - (len(plain) % 8)
    plain += bytes([padding]) * padding
    assert len(plain) % Blowfish.block_size == 0
    encrypted = Blowfish.new(_frame_key(), Blowfish.MODE_ECB).encrypt(plain)
    return struct.pack(">I", len(encrypted)) + encrypted


def decrypt_frame(payload: bytes) -> Tuple[int, bytes]:
    """Decode the bytes after the outer four-byte TCP length."""

    if len(payload) == 0 or len(payload) % Blowfish.block_size:
        raise ValueError("encrypted payload is not a Blowfish block sequence")
    plain = Blowfish.new(_frame_key(), Blowfish.MODE_ECB).decrypt(payload)
    if len(plain) < 8:
        raise ValueError("encrypted payload is too short")
    inner_length = struct.unpack(">I", plain[:4])[0]
    command_id = struct.unpack(">H", plain[4:6])[0]
    padding = plain[-1]
    if padding < 1 or padding > 8 or padding > len(plain) - 6:
        raise ValueError("invalid protocol padding")
    body_end = len(plain) - padding
    body = plain[6:body_end]
    if inner_length != 2 + len(body):
        raise ValueError(f"inner length mismatch: {inner_length} != {2 + len(body)}")
    return command_id, body


def _json_safe(value: object) -> object:
    """Convert protobuf field values into compact, lossless JSON values."""

    if isinstance(value, bytes):
        return {
            "hex": value.hex(),
            "utf8": value.decode("utf-8", errors="replace"),
        }
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    return value


class SessionRecorder:
    """Persist every decrypted queue/game packet for protocol implementation.

    The ``.bin`` file is the exact framed packet, including the outer four-byte
    length and encrypted payload.  ``packets.jsonl`` contains the command ID,
    decoded protobuf body, and the best-effort wire-field view.  Repeated
    command IDs receive a numeric suffix instead of overwriting an earlier
    packet.
    """

    def __init__(self, root: Path):
        base = Path(root).expanduser().resolve()
        base.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        session = base / stamp
        suffix = 1
        while session.exists():
            session = base / f"{stamp}-{suffix}"
            suffix += 1
        session.mkdir()
        self.session_dir = session
        self.metadata_path = session / "packets.jsonl"
        self._lock = threading.Lock()
        self._counts: Dict[str, int] = {}
        (session / "manifest.json").write_text(
            json.dumps(
                {
                    "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    "protocol": "blowfish-ecb-framed-protobuf",
                    "frame_file": "outer-length(4) + encrypted-payload",
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    def record(
        self,
        kind: str,
        direction: str,
        frame: bytes,
        command_id: Optional[int],
        body: bytes = b"",
        address: Optional[Tuple[str, int]] = None,
        error: Optional[str] = None,
    ) -> Path:
        """Write a raw frame and one JSONL description, then return its path."""

        command_label = str(command_id) if command_id is not None else "unknown"
        stem = f"{kind}-{direction}-{command_label}"
        with self._lock:
            count = self._counts.get(stem, 0) + 1
            self._counts[stem] = count
            filename = f"{stem}.bin" if count == 1 else f"{stem}-{count:04d}.bin"
            frame_path = self.session_dir / filename
            frame_path.write_bytes(frame)

            fields: object = {}
            fields_error: Optional[str] = None
            if body:
                try:
                    fields = _json_safe(parse_fields(body))
                except Exception as exc:  # malformed/unknown protobuf is still useful
                    fields = {}
                    fields_error = str(exc)
            entry = {
                "index": sum(self._counts.values()),
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "kind": kind,
                "direction": direction,
                "command_id": command_id,
                "peer": list(address) if address else None,
                "frame_file": filename,
                "frame_length": len(frame),
                "body_length": len(body),
                "body_hex": body.hex(),
                "fields": fields,
            }
            if fields_error:
                entry["fields_error"] = fields_error
            if error:
                entry["error"] = error
            with self.metadata_path.open("a", encoding="utf-8") as metadata:
                metadata.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return frame_path


@dataclass
class GameState:
    host: str
    queue_port: int
    game_port: int
    http_port: int = 18080
    internal_game_version: int = 2
    internal_resource_version: int = 793
    role_id: int = 1000000001
    role_name: str = "Local Guest"
    token: str = "local-token"
    accounts: Dict[str, int] = field(default_factory=dict)
    selected_accounts: set[str] = field(
        init=False,
        default_factory=set,
        repr=False,
    )
    selected_accounts_lock: threading.Lock = field(
        init=False,
        default_factory=threading.Lock,
        repr=False,
    )
    state_file: Optional[Path] = None
    profile_mode: str = "minimal"
    economy_mode: str = LEGACY_ECONOMY
    new_account_mode: str = "all-owned"
    progression: Optional[PlayerProgression] = field(init=False, default=None, repr=False)
    dispatch_lock: threading.RLock = field(init=False, default_factory=threading.RLock, repr=False)
    game_handlers: set = field(init=False, default_factory=set, repr=False)
    supply_catalog: Optional[SupplyCatalog] = field(init=False, default=None, repr=False)
    independent_growth: Optional[IndependentGrowthLayer] = field(init=False, default=None, repr=False)
    growth_session: str = field(init=False, default_factory=lambda: uuid.uuid4().hex, repr=False)
    content_root: Optional[Path] = None
    protocol_catalog_path: Optional[Path] = None
    handler_dependencies_path: Optional[Path] = None
    data_root: Optional[Path] = None
    assembly_path: Optional[Path] = None
    store: PersistentStateStore = field(init=False, repr=False)
    content_catalog: Optional[ContentCatalog] = field(
        init=False, default=None, repr=False
    )
    combat_attributes: Optional[CombatAttributeResolver] = field(
        init=False, default=None, repr=False
    )
    content_responses: Optional[ContentResponseLayer] = field(
        init=False,
        default=None,
        repr=False,
    )
    energy_recovery: Optional[EnergyRecoveryLayer] = field(
        init=False,
        default=None,
        repr=False,
    )
    activity: Optional[ActivityContentLayer] = field(
        init=False,
        default=None,
        repr=False,
    )
    archive: Optional[ArchiveContentLayer] = field(
        init=False,
        default=None,
        repr=False,
    )
    passport: Optional[PassportContentLayer] = field(
        init=False,
        default=None,
        repr=False,
    )
    collectibles: Optional[CollectiblesLayer] = field(
        init=False,
        default=None,
        repr=False,
    )
    farm: Optional[FarmContentLayer] = field(init=False, default=None, repr=False)
    race: Optional[RaceContentLayer] = field(init=False, default=None, repr=False)
    gospel_reset: Optional[GospelResetLayer] = field(init=False, default=None, repr=False)
    formation: Optional[FormationContentLayer] = field(
        init=False,
        default=None,
        repr=False,
    )
    gospel_progression: Optional[GospelProgressionLayer] = field(
        init=False,
        default=None,
        repr=False,
    )
    hero_progression: Optional[HeroProgressionLayer] = field(
        init=False,
        default=None,
        repr=False,
    )
    spring: Optional[SpringContentLayer] = field(
        init=False,
        default=None,
        repr=False,
    )
    private_equip: Optional[PrivateEquipContentLayer] = field(
        init=False,
        default=None,
        repr=False,
    )
    lilia: Optional[LiliaContentLayer] = field(
        init=False,
        default=None,
        repr=False,
    )
    oracle: Optional[OracleContentLayer] = field(
        init=False,
        default=None,
        repr=False,
    )
    life: Optional[LifeContentLayer] = field(
        init=False,
        default=None,
        repr=False,
    )
    shop: Optional[ShopContentLayer] = field(
        init=False,
        default=None,
        repr=False,
    )
    profile_customization: Optional[ProfileCustomizationLayer] = field(
        init=False,
        default=None,
        repr=False,
    )
    gacha: Optional[GachaEngine] = field(init=False, default=None, repr=False)
    lottery: Optional[LotteryEngine] = field(init=False, default=None, repr=False)
    rewards: Optional[RewardLayer] = field(init=False, default=None, repr=False)
    soul_gacha: Optional[SoulGachaEngine] = field(
        init=False,
        default=None,
        repr=False,
    )
    teaching: Optional[TeachingStateLayer] = field(
        init=False,
        default=None,
        repr=False,
    )
    battle: Optional[BattleSettlementLayer] = field(
        init=False,
        default=None,
        repr=False,
    )
    special_battles: Optional[SpecialBattlesLayer] = field(
        init=False,
        default=None,
        repr=False,
    )
    tower_battles: Optional[TowerBattlesLayer] = field(
        init=False,
        default=None,
        repr=False,
    )
    tower_progression: Optional[TowerProgressionLayer] = field(
        init=False,
        default=None,
        repr=False,
    )

    def __post_init__(self) -> None:
        if self.economy_mode not in (LEGACY_ECONOMY, NORMAL_ECONOMY):
            raise ValueError(f"unsupported economy mode: {self.economy_mode}")
        self.store = PersistentStateStore(self.state_file, role_seed=self.role_id)
        if self.store.initial_account_mode is not None:
            self.new_account_mode = self.store.initial_account_mode
        if self.profile_mode == "all-unlocked":
            self.profile_mode = ALL_UNLOCKED_PROFILE_MODE
        if self.profile_mode == ALL_UNLOCKED_PROFILE_MODE:
            self.content_catalog = ContentCatalog.load(self.content_root)
            response_root = self.content_catalog.root
            catalog_root = Path(response_root).resolve()
            generated_root = catalog_root.parent
            protocol_catalog = Path(
                self.protocol_catalog_path
                or (generated_root / "protocol" / "protocol_catalog.json")
            ).resolve()
            handler_dependencies = Path(
                self.handler_dependencies_path
                or (generated_root / "protocol" / "handler_dependencies.json")
            ).resolve()
            data_root = (
                Path(self.data_root).resolve()
                if self.data_root is not None
                else Path(__file__).resolve().parents[1] / "data"
            )
            assembly_path = (
                Path(self.assembly_path).resolve()
                if self.assembly_path is not None
                else Path(__file__).resolve().parents[1]
                / "assemblies"
                / "runtime-assembly.bin"
            )
            self.content_responses = ContentResponseLayer(response_root)
            self.energy_recovery = EnergyRecoveryLayer(self.content_catalog)
            self.combat_attributes = CombatAttributeResolver(self.content_catalog)
            self.independent_growth = IndependentGrowthLayer(self.content_catalog)
            self.battle = BattleSettlementLayer(self.content_catalog, normal_economy=self.economy_mode == NORMAL_ECONOMY)
            if self.economy_mode == NORMAL_ECONOMY:
                self.supply_catalog = SupplyCatalog(self.content_catalog)
            self.special_battles = SpecialBattlesLayer(
                response_root,
                protocol_catalog,
                self.battle,
            )
            self.tower_battles = TowerBattlesLayer(
                response_root,
                protocol_catalog,
                self.battle,
                self.combat_attributes,
            )
            self.tower_progression = TowerProgressionLayer(
                response_root,
                protocol_catalog,
            )
            self.activity = ActivityContentLayer(self.content_catalog)
            self.archive = ArchiveContentLayer(self.content_catalog)
            self.passport = PassportContentLayer(self.content_catalog)
            self.collectibles = CollectiblesLayer(self.content_catalog)
            self.formation = FormationContentLayer(
                protocol_catalog=protocol_catalog,
                handler_dependencies=handler_dependencies,
                verify_assembly=assembly_path.is_file(),
                combat_resolver=self.combat_attributes,
            )
            self.gospel_progression = GospelProgressionLayer(
                self.content_catalog,
                protocol_catalog=protocol_catalog,
                handler_dependencies=handler_dependencies,
                combat_resolver=self.combat_attributes,
                collectibles=self.collectibles,
            )
            self.gospel_reset = GospelResetLayer(self.gospel_progression)
            self.hero_progression = HeroProgressionLayer(
                self.content_catalog,
                protocol_catalog=protocol_catalog,
                handler_dependencies=handler_dependencies,
            )
            self.spring = SpringContentLayer(self.content_catalog)
            self.private_equip = PrivateEquipContentLayer(self.content_catalog)
            self.activity.achievement_provider = self.private_equip.task_achievements
            self.lilia = LiliaContentLayer(self.content_catalog)
            self.oracle = OracleContentLayer(self.content_catalog)
            self.farm = FarmContentLayer(self.content_catalog, codec=self.collectibles.codec)
            self.race = RaceContentLayer(self.content_catalog, codec=self.collectibles.codec)
            self.life = LifeContentLayer(self.content_catalog)
            self.life.hero_progression = self.hero_progression
            shop_catalog = ShopCatalog(
                content_dir=response_root,
                data_root=data_root,
                assembly=assembly_path,
            )
            self.profile_customization = ProfileCustomizationLayer(
                catalog=shop_catalog,
                protocol_catalog=protocol_catalog,
            )
            self.shop = ShopContentLayer(
                catalog=shop_catalog,
                protocol_catalog=protocol_catalog,
                normal_economy=self.economy_mode == NORMAL_ECONOMY,
            )
            try:
                from .item_use_content import ItemUseLayer
            except ImportError:
                from item_use_content import ItemUseLayer
            self.shop.item_use_layer = ItemUseLayer(self.shop, catalog=self.content_catalog,
                                                    battle=self.battle, life=self.life)
            try:
                from .journey_content import JourneyLayer
            except ImportError:
                from journey_content import JourneyLayer
            self.activity.journey = JourneyLayer(self.shop, self.content_catalog)
            try:
                from .stage_tasks import StageTaskLayer
            except ImportError:
                from stage_tasks import StageTaskLayer
            self.stage_tasks = StageTaskLayer(self.content_catalog, self.shop, self.battle)
            self.activity.achievement_provider = lambda profile: (
                self.private_equip.task_achievements(profile) + self.stage_tasks.achievements(profile))
            self.stage_tasks.achievement_snapshot = self.activity.request_achievement_datas
            self.gacha = GachaEngine(response_root)
            self.progression = PlayerProgression(self.content_catalog, self.hero_progression,
                                                 self.tower_progression.codec)
            self.lottery = LotteryEngine(response_root, normal_economy=self.economy_mode == NORMAL_ECONOMY)
            self.rewards = RewardLayer(
                response_root,
                protocol_catalog=protocol_catalog,
            )
            self.soul_gacha = SoulGachaEngine(
                response_root,
                offline_all_pools=True,
            )
            self.teaching = TeachingStateLayer(response_root)
        elif self.profile_mode != "minimal":
            raise ValueError(f"unsupported profile mode: {self.profile_mode}")

    def _profile_for(self, account: str) -> Dict[str, object]:
        key = account or "local"
        if self.content_catalog is None:
            profile = self.store.ensure_account(key)
        else:

            # get_account is read-only. Existing imported/legacy accounts must
            # never have their resources or growth reset by an APK upgrade.
            is_new_account = self.store.get_account(key) is None

            def prepare_profile(profile: Dict[str, object]) -> None:
                """Migrate and normalize one profile inside a store transaction."""

                assert self.content_catalog is not None
                if is_new_account and self.new_account_mode == "newcomer":
                    self.progression.initialize(profile)
                if newcomer_enabled(profile):
                    self._prepare_newcomer(profile)
                    return
                saved_economy = capture_existing_economy(profile) if self.economy_mode == NORMAL_ECONOMY and not is_new_account else None
                # SceneId is mutable account state.  Compatibility seeds own
                # malformed/missing defaults, but a catalog or Lillia
                # migration must not undo a valid command-492 placement.
                raw_heroes = profile.get("heroes")
                hero_scene_ids_are_valid = isinstance(raw_heroes, list) and bool(
                    raw_heroes
                )
                saved_scene_ids: Dict[int, int] = {}
                duplicate_scene_uids = set()
                if isinstance(raw_heroes, list):
                    for row in raw_heroes:
                        if not isinstance(row, Mapping):
                            hero_scene_ids_are_valid = False
                            continue
                        raw_uid = row.get("uid")
                        raw_scene_id = row.get("scene_id")
                        try:
                            uid = int(raw_uid)
                            scene_id = int(raw_scene_id)
                        except (TypeError, ValueError, OverflowError):
                            hero_scene_ids_are_valid = False
                            continue
                        if isinstance(raw_uid, bool) or isinstance(raw_scene_id, bool):
                            hero_scene_ids_are_valid = False
                            continue
                        if scene_id not in DAILY_CITY_HERO_SCENE_IDS:
                            hero_scene_ids_are_valid = False
                            continue
                        if uid <= 0:
                            continue
                        if uid in saved_scene_ids:
                            duplicate_scene_uids.add(uid)
                        else:
                            saved_scene_ids[uid] = scene_id
                for uid in duplicate_scene_uids:
                    saved_scene_ids.pop(uid, None)
                try:
                    profile_revision = int(profile.get("profile_revision") or 0)
                except (TypeError, ValueError, OverflowError):
                    profile_revision = 0
                try:
                    hero_progress_revision = int(
                        profile.get("hero_progress_revision") or 0
                    )
                except (TypeError, ValueError, OverflowError):
                    hero_progress_revision = 0
                try:
                    gospel_progress_revision = int(
                        profile.get("gospel_progress_revision") or 0
                    )
                except (TypeError, ValueError, OverflowError):
                    gospel_progress_revision = 0
                needs_profile_migration = not (
                    profile.get("profile_mode") == ALL_UNLOCKED_PROFILE_MODE
                    and profile_revision >= ALL_UNLOCKED_PROFILE_REVISION
                    and profile.get("content_catalog_version")
                    == self.content_catalog.version
                    and profile.get("all_content_unlocked") is True
                    and hero_progress_revision >= HERO_PROGRESS_REVISION
                    and gospel_progress_revision >= GOSPEL_PROGRESS_REVISION
                )
                if needs_profile_migration:
                    seed_all_unlocked_profile(profile, self.content_catalog)
                elif not hero_scene_ids_are_valid:
                    # A legacy portrait suffix is only an invalid room ID.
                    # Do not reseed unrelated player growth to repair it.
                    for hero in profile.get("heroes", []):
                        if isinstance(hero, dict):
                            hero["scene_id"] = saved_scene_ids.get(hero.get("uid"), 0)
                if (
                    profile.get("offline_content_revision") != OFFLINE_CONTENT_REVISION
                    or profile.get("offline_content_catalog_version")
                    != self.content_catalog.version
                ):
                    seed_inventory(profile, self.content_catalog)
                    if self.spring is not None:
                        self.spring.seed_profile(profile)
                        profile["spring_status"] = 1
                    if self.private_equip is not None:
                        self.private_equip.seed_profile(profile)
                    if self.collectibles is not None:
                        self.collectibles.seed_profile(profile)
                    if self.activity is not None:
                        self.activity.seed(profile)
                    if self.archive is not None:
                        self.archive.seed(profile)
                    if self.passport is not None:
                        self.passport.seed(profile)
                    if self.life is not None:
                        self.life.seed_profile(profile)
                    profile["offline_content_revision"] = OFFLINE_CONTENT_REVISION
                    profile["offline_content_catalog_version"] = (
                        self.content_catalog.version
                    )
                # Lillia must be migrated before hero-dependent collectible
                # roots.  Otherwise the first read seeds presets/appearances
                # for six witches, Lillia is appended afterwards, and the next
                # ordinary read performs a second observable migration.
                if self.lilia is not None and self.lilia.needs_seed(profile):
                    self.lilia.seed_profile(profile)
                # Collectible ownership has its own schema revision.  Keep it
                # independent from the broad offline-content revision so a
                # decomposed rune cannot be recreated by an old profile whose
                # global revision already happens to be current.
                if self.collectibles is not None and self.collectibles.needs_seed(
                    profile
                ):
                    self.collectibles.seed_profile(profile)
                if self.collectibles is not None:
                    # This is a narrow, idempotent repair, not a collectible
                    # reseed. Never bump the rune seed or resurrect decomposed
                    # inventory just to synchronize awakening-bound effects.
                    if self.economy_mode == NORMAL_ECONOMY and is_new_account and self.new_account_mode != "all-unlocked":
                        initialize_normal_profile(profile, self.content_catalog,
                                                  self.gospel_progression, self.hero_progression)
                    elif saved_economy is not None:
                        restore_existing_economy(profile, saved_economy)
                    self.collectibles.sync_gospel_decoration_levels(profile)
                    profile[GOSPEL_DECORATION_LINK_REVISION_KEY] = GOSPEL_DECORATION_LINK_REVISION
                # Spring-v2 has an independent migration because old saves
                # could say a daily task was accepted while omitting the task
                # holder required by the runtime's dialogue callback.  Persist
                # that repair even when the broad offline revision is current.
                if self.spring is not None and self.spring.needs_seed(profile):
                    self.spring.seed_profile(profile)
                if self.gacha is not None:
                    factory = self.gacha.gospel_factory
                    profile["gospels"] = [factory.repair_missing(row) if factory.needs_repair(row) else row
                                          for row in profile.get("gospels", [])]
                if self.oracle is not None:
                    self.oracle.seed_profile(profile)
                if self.profile_customization is not None:
                    self.profile_customization.seed_profile(profile)
                if self.energy_recovery is not None:
                    self.energy_recovery.seed_profile(profile)
                if self.economy_mode == NORMAL_ECONOMY and not profile.get("offline_economy"):
                    profile["offline_economy"] = {"mode": NORMAL_ECONOMY, "origin": "preserved-existing-save"}
                if is_new_account and self.new_account_mode == "all-unlocked":
                    self.independent_growth.seed_full_preset(profile)
                if self.combat_attributes is not None:
                    self.gospel_progression.repair_paid_breakthroughs(profile)
                    for gospel in profile.get("gospels", []):
                        if isinstance(gospel, dict):
                            gospel["combat_power"] = self.combat_attributes.resolve_gospel(gospel, profile).combat_power
                # Restore the exact formation layer after compatibility seeds
                # so ordinary reads cannot repopulate intentionally benched
                # heroes or drift from command 49/19 snapshots.
                if self.formation is not None:
                    formation_result = self.formation.handle(
                        CS_PHALANX_DATAS,
                        profile,
                        b"",
                    )
                    if formation_result is None:  # pragma: no cover - pinned map
                        raise RuntimeError("formation layer did not normalize profile")
                    if formation_result.changes:
                        self.formation.apply_changes(
                            profile,
                            formation_result.changes,
                        )
                normalized_heroes = profile.get("heroes")
                if isinstance(normalized_heroes, list) and saved_scene_ids:
                    for row in normalized_heroes:
                        if not isinstance(row, dict):
                            continue
                        try:
                            uid = int(row.get("uid") or 0)
                        except (TypeError, ValueError, OverflowError):
                            continue
                        if uid in saved_scene_ids:
                            row["scene_id"] = saved_scene_ids[uid]

            profile = self.store.mutate_account(key, prepare_profile)
        self.accounts[key] = int(profile.get("role_id") or self.role_id)
        return profile

    def _prepare_newcomer(self, profile: Dict[str, object]) -> None:
        """Normalize only owned entities; never invoke the all-owned seed."""
        assert self.progression is not None
        self.progression.normalize(profile)
        self.gospel_progression.repair_paid_breakthroughs(profile)
        self.private_equip.seed_profile(profile)
        self.collectibles.seed_profile(profile)
        self.life.seed_profile(profile)
        self.archive.seed_profile(profile)
        self.profile_customization.seed_profile(profile)
        self.energy_recovery.seed_profile(profile)
        self.oracle.seed_profile(profile)
        self.spring.seed_profile(profile)
        self.activity.seed(profile)
        for gospel in profile.get("gospels", []):
            gospel["combat_power"] = self.combat_attributes.resolve_gospel(gospel, profile).combat_power
            gospel["spirit_is_ready"] = self.gospel_progression._ready_value(
                gospel,self.gospel_progression.catalog.definitions[gospel["id"]])
        formation = self.formation.handle(CS_PHALANX_DATAS, profile, b"")
        if formation is not None and formation.changes:
            self.formation.apply_changes(profile, formation.changes)

    def finalize_progression(self, profile: Dict[str, object]) -> None:
        if self.progression is not None and newcomer_enabled(profile):
            previous_level = int(profile.get('level', 1))
            self.progression.normalize(profile)
            self.progression.award_level_up_energy(profile, previous_level)
            self.private_equip.seed_profile(profile)
            self.collectibles.seed_profile(profile)
            self.archive.seed_profile(profile)

        if self.combat_attributes is not None:
            for gospel in profile.get("gospels", []):
                gospel["combat_power"] = self.combat_attributes.resolve_gospel(gospel, profile).combat_power
                if newcomer_enabled(profile):
                    gospel["spirit_is_ready"] = self.gospel_progression._ready_value(
                        gospel,self.gospel_progression.catalog.definitions[gospel["id"]])

    def complete_story_admission(self, profile: Dict[str, object], plot_id: int) -> int:
        """Finish only the active story-only stage, with its real first rewards."""
        active = profile.get("battle_runtime", {}).get("active")
        if not isinstance(active, Mapping): return 0
        dungeon_id = int(active.get("dungeon_id", 0))
        source = self.content_catalog.table("a_dungeon").by_id.get(dungeon_id)
        if source is None or source["dungeon_type"] != 1 or source["story_id"] != plot_id:
            return 0
        completed = dungeon_id in profile.get("completed_dungeons", [])
        old_gospels = {r["id"] for r in profile.get("gospels", [])}
        outcome = self.battle.settle(profile, dungeon_id=dungeon_id,
            role_id=int(profile["role_id"]),result=1)
        if not outcome.ok:
            raise ValueError("Story admission could not settle atomically: " + outcome.reason)
        profile.update(copy.deepcopy(outcome.changes))
        new_gospels = {r["id"] for r in profile.get("gospels", [])} - old_gospels
        if new_gospels: self.collectibles.grant_gospel_decorations(profile,new_gospels)
        self.finalize_progression(profile)
        return 0 if completed else dungeon_id

    def role_for(self, account: str) -> int:
        profile = self._profile_for(account)
        role_id = int(profile.get("role_id") or self.role_id)
        self.accounts[account or "local"] = role_id
        return role_id

    def role_was_selected(self, account: str) -> bool:
        key = account or "local"
        with self.selected_accounts_lock:
            return key in self.selected_accounts

    def mark_role_selected(self, account: str) -> None:
        key = account or "local"
        with self.selected_accounts_lock:
            self.selected_accounts.add(key)

    def profile_for(self, account: str) -> Dict[str, object]:
        return self._profile_for(account)

    def player_supply(self, request: Mapping[str, object]) -> Dict[str, object]:
        """Same-process native UI only; there is no HTTP/game admin endpoint.

        Use the same dispatch lock as normal protocol requests, so a native
        grant cannot publish an old absolute balance after a later purchase.
        The active authenticated game session supplies the account identity.
        """
        with self.dispatch_lock:
            if self.supply_catalog is None:
                raise EconomyError("此版本未启用材料补给。")
            handlers = [h for h in self.game_handlers if h.initial_snapshots_sent]
            if len(handlers) != 1:
                raise EconomyError("请进入游戏主页后再打开材料补给。")
            handler = handlers[0]
            if request.get("operation") == "catalog" and set(request) == {"operation"}:
                return {"ok": True, "resources": self.supply_catalog.snapshot(self.store.snapshot(handler.account))}
            if request.get("operation") != "grant" or set(request) != {"operation", "key", "count", "request_id"}:
                raise EconomyError("补给请求格式无效。")

            def grant(current):
                before = copy.deepcopy(current)
                changes, receipt = self.supply_catalog.grant(current, key=request["key"],
                    count=request["count"], request_id=request["request_id"])
                current.update(changes)
                if changes and self.energy_recovery is not None and before.get("energy") != current.get("energy"):
                    current.update(self.energy_recovery.reconcile_energy_change(current,
                        previous_energy=int(before.get("energy", 0))))
                return before, receipt

            (before, receipt), after = self.store.transact_account(handler.account, grant)
            notifications = make_battle_state_notifications(handler.role_id, before, after, self.collectibles, self.combat_attributes, getattr(self, 'stage_tasks', None))
            if receipt["replayed"]:
                resource = self.supply_catalog.resources[request["key"]]
                if resource["kind"] == 4:
                    notifications = (make_update_item_packet(handler.role_id, after["inventory"][str(resource["item_id"])]),)
                else:
                    prop = dict(ROLE_PROP_FIELDS)[resource["field"]]
                    notifications = ((NOTIFY_UPDATE_ROLE_PROP, make_update_role_prop(handler.role_id, prop, after.get(resource["field"], 0))),)
            notified = True
            try:
                for command, payload in notifications:
                    handler.send(command, payload)
            except OSError:
                notified = False  # The committed receipt makes retry safe.
                LOG.warning("supply persisted; client disconnected before projection")
            return {"ok": True, "receipt": receipt, "notified": notified}

    def player_growth(self, request: Mapping[str, object]) -> Dict[str, object]:
        """Durable two-phase player intent, scoped to the live save/session.

        No account parameter, no HTTP endpoint, no application-global journal.
        Prepare is free; payment and completed receipt commit in one disk write.
        """
        with self.dispatch_lock:
            layer = self.independent_growth
            handlers = [h for h in self.game_handlers if h.initial_snapshots_sent]
            if layer is None or len(handlers) != 1:
                raise EconomyError("请进入游戏主页后再打开离线养成。")
            handler = handlers[0]
            operation = request.get("operation")
            if operation == "catalog" and set(request) == {"operation"}:
                return {"ok": True, "session": self.growth_session, "snapshot": layer.snapshot(self.store.snapshot(handler.account))}
            if not isinstance(operation, str) or operation not in {"prepare", "commit", "ack", "cancel"}:
                raise EconomyError("未知离线养成操作。")
            fields = {"operation", "session", "id", "spec", "quote"} if operation == "prepare" else {"operation", "session", "id"}
            if set(request) != fields:
                raise EconomyError("离线养成请求格式无效。")
            if request["session"] != self.growth_session:
                raise EconomyError("当前存档连接已改变，请重新打开养成面板。")

            def mutate(current):
                before = copy.deepcopy(current)
                receipt = None
                if operation == "prepare":
                    changes, receipt = layer.prepare(current, request["spec"], request["id"], request["quote"])
                elif operation == "commit":
                    changes, receipt = layer.commit(current, request["id"])
                else:
                    changes = layer.clear_pending(current, request["id"], cancel=operation == "cancel")
                current.update(changes)
                return before, receipt

            (before, receipt), after = self.store.transact_account(handler.account, mutate)
            notifications = make_battle_state_notifications(handler.role_id, before, after,
                self.collectibles, self.combat_attributes, getattr(self, "stage_tasks", None))
            if operation == "commit" and receipt and receipt.get("replayed"):
                # Replay an absolute projection as well as the receipt; the
                # first notification may have been lost after disk commit.
                notifications = ((NOTIFY_UPDATE_ROLE_PROP, make_update_role_prop(handler.role_id,
                    dict(ROLE_PROP_FIELDS)["gold"], after["gold"])),) + tuple(
                    (41, field_message(1, make_local_hero(hero=row,
                        attributes=(resolved := self.combat_attributes.resolve_hero(row,
                            all_unlocked=bool(after.get("all_content_unlocked")), profile=after)).wire_attributes,
                        resolved_combat_power=resolved.combat_power))) for row in after.get("heroes", []))
            notified = True
            try:
                for command, body in notifications:
                    handler.send(command, body)
            except OSError:
                notified = False
                LOG.warning("growth committed; client disconnected before projection")
            return {"ok": True, "receipt": receipt, "notified": notified, "snapshot": layer.snapshot(after)}


class ProtocolHandler:
    def __init__(
        self,
        sock: socket.socket,
        address: Tuple[str, int],
        kind: str,
        state: GameState,
        recorder: Optional[SessionRecorder] = None,
        endpoint_runtime: Optional[ProtocolEndpointRuntime] = None,
    ):
        self.sock = sock
        self.address = address
        self.kind = kind
        self.state = state
        self.recorder = recorder
        self.endpoint_runtime = endpoint_runtime or ProtocolEndpointRuntime()
        self.account = "local"
        self.role_id = state.role_id
        self.role_name = state.role_name
        self.initial_snapshots_sent = False

    def run(self) -> None:
        LOG.info("%s connection from %s", self.kind, self.address)
        self.sock.settimeout(120)
        pending = bytearray()
        if self.kind == "game":
            with self.state.dispatch_lock:
                self.state.game_handlers.add(self)
        try:
            while True:
                chunk = self.sock.recv(65536)
                if not chunk:
                    return
                pending.extend(chunk)
                while True:
                    if len(pending) < 4:
                        break
                    frame_size = struct.unpack(">I", pending[:4])[0]
                    if frame_size <= 0 or frame_size > MAX_FRAME:
                        raise ValueError(f"invalid frame size {frame_size}")
                    if len(pending) < frame_size + 4:
                        break
                    frame = bytes(pending[: frame_size + 4])
                    encrypted = bytes(pending[4 : frame_size + 4])
                    del pending[: frame_size + 4]
                    try:
                        command_id, body = decrypt_frame(encrypted)
                    except Exception as exc:
                        if self.recorder:
                            self.recorder.record(
                                self.kind,
                                "request",
                                frame,
                                None,
                                address=self.address,
                                error=str(exc),
                            )
                        raise
                    if self.recorder:
                        self.recorder.record(
                            self.kind,
                            "request",
                            frame,
                            command_id,
                            body,
                            self.address,
                        )
                    self.handle(command_id, body)
        except (ConnectionError, OSError, socket.timeout):
            LOG.info("%s connection closed: %s", self.kind, self.address)
        except Exception:
            LOG.exception("%s protocol error from %s", self.kind, self.address)
        finally:
            with self.state.dispatch_lock:
                self.state.game_handlers.discard(self)
            try:
                self.sock.close()
            except OSError:
                pass

    def send(self, command_id: int, body: bytes = b"") -> None:
        frame = encrypt_frame(command_id, body)
        if self.recorder:
            self.recorder.record(
                self.kind,
                "response",
                frame,
                command_id,
                body,
                self.address,
            )
        self.sock.sendall(frame)
        LOG.debug("%s -> id=%d body=%d", self.kind, command_id, len(body))

    def _evaluate_domain_request(
        self,
        layer: Any,
        command_id: int,
        body: bytes,
        candidate: Dict[str, object],
    ) -> Any:
        """Evaluate and apply one domain result to the locked candidate."""

        try:
            from . import mutation_receipts
        except ImportError:
            import mutation_receipts
        receipt_key, replayed = mutation_receipts.find(candidate,command_id,body)
        if replayed is not None:
            if layer is getattr(self.state, 'race', None):
                replayed = layer.replay(candidate,command_id,replayed)
            if replayed.ok and layer is getattr(self.state, "farm", None):
                # Farm receipts acknowledge the source payment, but display
                # current absolute farm/resources even after later actions.
                farm_notes = layer.current_state_notifications(candidate)
                farm_notes += ((25,make_update_role_prop(self.role_id,2,int(candidate.get('gold',0)))),)
                farm_notes += tuple(make_update_item_packet(self.role_id,row)
                                    for row in candidate.get('inventory',{}).values()
                                    if int(row['id']) in layer.catalog.item_max_stack)
                reply = replayed.body
                post = ()
                if command_id in (640,642):
                    current_root, _ = layer._state(candidate,layer._now())
                    name = 'SC_FarmerTrainRet' if command_id == 642 else 'SC_SetTrusteeshipRet'
                    values = {'Errorcode':0, **({'FarmerLvl':current_root['farm']['farmer_level']} if command_id == 642
                                               else {'Opt':current_root['farm']['trusteeship']})}
                    reply = layer.codec.encode(name,values) + field_string(127,receipt_key[0])
                    # Source delegate reply resets its displayed start time;
                    # restore the persisted start after acknowledging it.
                    post = tuple(note for note in farm_notes if note[0] == 655)
                replayed = replace(replayed,body=reply,notifications=farm_notes,post_notifications=post)
            if replayed.ok and layer is getattr(self.state, "gospel_reset", None):
                replayed = replace(replayed, notifications=layer.current_state_notifications(candidate, body))
            if replayed.ok and command_id == 218 and layer is getattr(self.state, 'life', None):
                fields = parse_fields(body)
                replayed = replace(replayed, notifications=layer.feeding_notifications(
                    candidate, first_int(fields, 1), first_int(fields, 2)))
            return replayed

        before = {"energy": candidate.get("energy", 0), "inventory": {
            key: copy.deepcopy(candidate.get("inventory", {}).get(key, {}))
            for key in ("1001049", "1001050")}}
        old_gospel_ids = {r["id"] for r in candidate.get("gospels", [])}
        result = layer.handle(command_id, candidate, body)
        if result is not None and result.changes:
            candidate.update(copy.deepcopy(result.changes))
            if (layer is getattr(self.state,"content_responses",None)
                    and command_id == CS_READ_PLOT_OVER and newcomer_enabled(candidate)
                    and not int(getattr(result,"error_code",0) or 0)):
                plot_id, status, _ = parse_plot_read_progress(parse_fields(body))
                if status == 1:
                    self.state.complete_story_admission(candidate,plot_id)
            if getattr(self.state,"collectibles",None) is not None and "gospels" in result.changes:
                new_ids = {r["id"] for r in candidate.get("gospels", [])} - old_gospel_ids
                if new_ids:
                    self.state.collectibles.grant_gospel_decorations(candidate, new_ids)
            if getattr(self.state, "progression", None) is not None:
                self.state.finalize_progression(candidate)
            if (self.state.private_equip is not None
                    and int(getattr(result, "error_code", 0) or 0) == 0):
                self.state.private_equip.observe_action(
                    before, candidate,
                    soul_draws=len(getattr(result, "draws", ())) if layer is self.state.soul_gacha else 0)
            if getattr(self.state, 'stage_tasks', None) is not None:
                self.state.stage_tasks.observe(candidate)
        return mutation_receipts.commit(candidate,command_id,receipt_key,result)

    def _record_login_day(self) -> Dict[str, object]:
        if self.state.private_equip is not None:
            self.state.store.mutate_account(self.account, self.state.private_equip.note_login)
        return self.state.store.snapshot(self.account)

    def _transact_visible_resources(self, layer, command_id, body):
        if self.state.economy_mode != NORMAL_ECONOMY:
            return self._transact_domain_request(layer, command_id, body)
        result, before, after = self._transact_domain_request_with_snapshots(layer, command_id, body)
        for notification_id, payload in make_battle_state_notifications(self.role_id, {} if getattr(result,"replayed",False) else before, after, self.state.collectibles, self.state.combat_attributes, getattr(self.state, 'stage_tasks', None)):
            self.send(notification_id, payload)
        return result

    def _transact_domain_request(
        self,
        layer: Any,
        command_id: int,
        body: bytes,
    ) -> Any:
        """Run a domain handler against the latest account revision."""

        if self.state.profile_mode == ALL_UNLOCKED_PROFILE_MODE:
            # Keep all-unlocked migration/seeding ahead of domain evaluation.
            # The returned profile is deliberately ignored: transact_account
            # supplies a fresh candidate after taking the account lock.
            self._prepare_profile_for_request()
        evaluator = partial(
            self._evaluate_domain_request,
            layer,
            command_id,
            body,
        )
        result, _ = self.state.store.transact_account(self.account, evaluator)
        return result

    def _transact_domain_request_with_snapshots(
        self,
        layer: Any,
        command_id: int,
        body: bytes,
    ) -> Tuple[Any, Dict[str, object], Dict[str, object]]:
        """Commit one battle domain request and retain its exact state delta."""

        if self.state.profile_mode == ALL_UNLOCKED_PROFILE_MODE:
            self._prepare_profile_for_request()

        def evaluator(candidate: Dict[str, object]) -> Tuple[Any, Dict[str, object]]:
            before = copy.deepcopy(candidate)
            result = self._evaluate_domain_request(
                layer,
                command_id,
                body,
                candidate,
            )
            # Fast/special battles spend through the same settlement layer as
            # normal battles, but previously left a full-to-below-cap timer
            # unset until the next request repaired it. Anchor it in this very
            # transaction, so a duplicate settlement neither delays recovery
            # nor creates an otherwise unexplained save revision.
            if (self.state.energy_recovery is not None
                    and result is not None
                    and int(getattr(result, "error_code", 0) or 0) == 0
                    and candidate.get("energy") != before.get("energy")):
                candidate.update(self.state.energy_recovery.reconcile_energy_change(
                    candidate, previous_energy=int(before.get("energy", 0) or 0),
                ))
            return result, before

        (result, before), after = self.state.store.transact_account(
            self.account,
            evaluator,
        )
        return result, before, after

    def _synchronize_energy(
        self,
        *,
        send_notifications: bool,
    ) -> Dict[str, object]:
        """Commit elapsed six-minute ticks and optionally refresh runtime caches."""

        layer = self.state.energy_recovery
        if layer is None:
            return self.state.profile_for(self.account)
        # Run all-unlocked migrations before taking the exact recovery delta.
        self._prepare_profile_for_request()

        def evaluator(candidate: Dict[str, object]) -> Dict[str, object]:
            before = copy.deepcopy(candidate)
            recovered = layer.synchronize(candidate)
            candidate.update(copy.deepcopy(recovered.changes))
            if self.state.economy_mode == NORMAL_ECONOMY:
                candidate.update(synchronize_tickets(candidate, self.state.content_catalog, layer.now()))
            return before

        before, after = self.state.store.transact_account(self.account, evaluator)
        if send_notifications:
            for notification_id, notification_body in make_battle_state_notifications(
                self.role_id,
                before,
                after,
                self.state.collectibles,
                self.state.combat_attributes,
                getattr(self.state, 'stage_tasks', None),
            ):
                self.send(notification_id, notification_body)
        return after

    def ensure_initial_snapshots(self, profile: Dict[str, object]) -> None:
        """Send role-owned snapshots once per game connection.

        A reconnect gets a new ProtocolHandler and therefore still receives
        the snapshots.  Repeating them during an ordinary battle start makes
        the legacy runtime recreate singleton notice roots and abort its scene
        callbacks.
        """

        if self.initial_snapshots_sent:
            return
        self.send(
            SC_PROFESSION_DATAS,
            make_profession_datas(
                profile,
                self.state.content_catalog,
                self.state.combat_attributes,
            ),
        )
        self.send(
            SC_HERO_DATAS,
            make_hero_datas(profile, self.state.combat_attributes),
        )
        self.send(
            SC_GOSPEL_DATAS,
            make_gospel_datas(profile, self.state.combat_attributes),
        )
        chapter_body = (
            self.state.content_responses.make_chapter_datas(profile)
            if self.state.content_responses is not None
            else make_chapter_datas(profile)
        )
        self.send(SC_CHAPTER_DATAS, chapter_body)
        if self.state.formation is not None:
            formation_result = self.state.formation.handle(
                CS_PHALANX_DATAS,
                profile,
                b"",
            )
            if formation_result is None:  # pragma: no cover - pinned request map
                raise RuntimeError("formation layer did not handle command 49")
            if formation_result.changes:
                changes = copy.deepcopy(formation_result.changes)

                def apply_formation_snapshot(current: Dict[str, object]) -> None:
                    self.state.formation.apply_changes(current, changes)

                self.state.store.mutate_account(
                    self.account,
                    apply_formation_snapshot,
                )
            self.send(formation_result.response_id, formation_result.body)
        else:
            self.send(SC_PHALANX_DATAS, make_phalanx_datas(profile))
        if self.state.content_catalog is not None:
            self.send(
                SC_BRANCH_GUIDE_DATAS,
                make_branch_guide_datas(self.state.content_catalog, profile),
            )
            self.send(
                SC_TEACH_GUIDE_DATAS,
                make_teach_guide_datas(self.state.content_catalog, profile),
            )
            item_message_id, item_body = make_item_datas_packet(profile)
            self.send(item_message_id, item_body)
            if self.state.collectibles is not None:
                for (
                    message_id,
                    snapshot_body,
                ) in self.state.collectibles.initial_snapshots(profile):
                    self.send(message_id, snapshot_body)
            if self.state.profile_customization is not None:
                for snapshot in self.state.profile_customization.initial_snapshots(
                    profile
                ):
                    self.send(snapshot.command_id, snapshot.body)
            if self.state.activity is not None:
                for message_id, snapshot_body in self.state.activity.initial(profile):
                    self.send(message_id, snapshot_body)
            if self.state.archive is not None:
                for snapshot in self.state.archive.initial(profile):
                    self.send(snapshot.command_id, snapshot.body)
        if self.state.energy_recovery is not None:
            self.send(
                NOTIFY_UPDATE_LAST_RECOVER_ENERGY_TIME,
                make_last_recover_energy_time(
                    int(profile.get(LAST_RECOVER_ENERGY_TIME_KEY, 0) or 0)
                ),
            )
        self.initial_snapshots_sent = True

    def handle(self, command_id: int, body: bytes) -> None:
        if self.state.economy_mode == NORMAL_ECONOMY:
            with self.state.dispatch_lock:
                self._handle_prepared_request(command_id, body)
        else:
            self._handle_prepared_request(command_id, body)

    def _prepare_profile_for_request(self) -> None:
        """Normalize once within a request; never cache its mutable snapshot."""
        prepared = getattr(self, "_prepared_profile_accounts", None)
        if prepared is not None and self.account in prepared:
            return
        self.state.profile_for(self.account)
        if prepared is not None:
            prepared.add(self.account)

    def _handle_prepared_request(self, command_id: int, body: bytes) -> None:
        # Recovery and domain dispatch both require normalized state, but
        # repeating the full roster/formation migration for the same packet
        # can delay startup replies beyond the source runtime's deadlines.
        # Only this completed-preparation fact is shared. Every transaction
        # still takes a fresh locked candidate, and the next packet prepares
        # again, including after an exception or an intervening mutation.
        previous = getattr(self, "_prepared_profile_accounts", None)
        self._prepared_profile_accounts = set()
        try:
            self._handle_request(command_id, body)
        finally:
            self._prepared_profile_accounts = previous

    def _handle_request(self, command_id: int, body: bytes) -> None:
        LOG.info("%s <- id=%d body=%d", self.kind, command_id, len(body))
        # Unsupported writes are explicit failures even for malformed payloads.
        # Reject before identity bootstrap, energy recovery or raw-request
        # bookkeeping can mutate a save. Queue traffic keeps its own protocol.
        if self.kind != "queue" and self.endpoint_runtime.reject_unimplemented_write(
            self, command_id
        ):
            return
        try:
            fields = parse_fields(body) if body else {}
        except ValueError:
            # Command 492 has a schema-strict decoder in OracleContentLayer.
            # Let it turn malformed protobuf into an explicit 493 error
            # instead of dropping the socket before the domain handler runs.
            if command_id not in FARM_RESPONSE_IDS and command_id not in RACE_RESPONSE_IDS and command_id not in (CS_INVITE_HERO_TO_SCENE, 947, 949, 1092, 1409):
                raise
            fields = {}

        # A fresh game-server socket starts with the legacy local defaults.
        # Reconnect is therefore an identity-binding operation, not a generic
        # empty endpoint: every later stateful handler must read/write the
        # account named by CS_Reconnect (normally the durable Test profile).
        if self.kind != "queue" and command_id == CS_RECONNECT:
            requested_account = first_string(fields, 1, self.account) or self.account
            self.account = requested_account
            profile = self._synchronize_energy(send_notifications=False)
            profile = self._record_login_day()
            self.role_id = int(profile.get("role_id") or self.state.role_id)
            self.role_name = str(profile.get("role_name") or self.state.role_name)
            requested_role_id = first_int(fields, 2, self.role_id)
            if requested_role_id and requested_role_id != self.role_id:
                LOG.warning(
                    "reconnect role mismatch account=%s requested=%d authoritative=%d",
                    self.account,
                    requested_role_id,
                    self.role_id,
                )
            self.send(
                SC_RECONNECT_RET,
                make_reconnect_ret(self.account, self.role_id),
            )
            cold_backend_bootstrap = not self.state.role_was_selected(self.account)
            if cold_backend_bootstrap:
                # Android kills the embedded Python backend together with the
                # Unity process, while the runtime retains its reconnect token.
                # On the next cold launch the new backend has never observed
                # CS_SelectRole. SC_ReconnectRet alone does not initialize the
                # 1.0 GameData.user singleton, so sending singleton snapshots
                # immediately makes Gospel/Atlas/Decoration/Medal handlers
                # throw "read after login" and leaves loading at 99%.
                # Bootstrap the ordinary select-role projection first. A
                # socket reconnect inside the same backend skips this frame,
                # preserving the legacy mid-battle reconnect sequence.
                self.send(
                    SC_SELECT_ROLE_RET,
                    make_select_role_ret(
                        self.account,
                        self.role_id,
                        self.role_name,
                        profile,
                    ),
                )
                self.state.mark_role_selected(self.account)
                LOG.info(
                    "cold reconnect bootstrapped select-role account=%s role=%d",
                    self.account,
                    self.role_id,
                )
            # A new socket has no runtime-side singleton snapshots associated
            # with this handler.  Rebind and resynchronize the authoritative
            # save before any resumed gameplay command can observe stale
            # currencies, inventory, roster, chapter, or formation data.
            self.ensure_initial_snapshots(profile)
            LOG.info(
                "reconnect bound game session account=%s role=%d",
                self.account,
                self.role_id,
            )
            return

        current_profile = self.state.store.get_account(self.account)
        if self.kind != "queue" and current_profile is not None and newcomer_enabled(current_profile):
            feature = 41 if command_id == 1409 and first_int(fields, 126) == 1 else PROGRESSION_REQUEST_FEATURES.get(command_id)
            active = current_profile.get("battle_runtime", {}).get("active", {})
            tutorial_draw = command_id == 39 and first_int(fields,2,0) == 0 and first_int(fields,3,0) == 2 and (
                isinstance(active,Mapping) and active.get("dungeon_id") == 8600012 or
                current_profile.get("tutorial_draw") and not current_profile.get("tutorial_completed")
                and current_profile.get("last_dungeon_id") == 8600012)
            if feature and not tutorial_draw and not self.state.progression.function_open(current_profile,feature):
                if command_id in (30,39):
                    try:
                        from . import mutation_receipts
                    except ImportError:
                        import mutation_receipts
                    def rejected_intent(candidate):
                        key, replayed = mutation_receipts.find(candidate,command_id,body)
                        if replayed is not None: return replayed
                        denied = mutation_receipts.ReplayedResult(command_id+1,field_varint(1,314),
                            error_code=314,role_id=self.role_id,
                            card_pool_id=first_int(fields,3,0) if command_id==39 else 0)
                        return mutation_receipts.commit(candidate,command_id,key,denied)
                    denied, _ = self.state.store.transact_account(self.account,rejected_intent)
                    self.send(denied.response_id,denied.body)
                else:
                    self.send(command_id+1,field_varint(1,314) + (field_varint(126,1) if command_id == 1409 else b""))
                return
            if command_id == CS_START_BATTLE and not self.state.progression.dungeon_open(current_profile,first_int(fields,4,0)):
                self.send(SC_START_BATTLE_RET,make_start_battle_ret(fields,self.role_id,314))
                return
            if command_id == CS_READ_PLOT_OVER:
                plot_id, status, _ = parse_plot_read_progress(fields)
                if plot_id not in self.state.content_catalog.table("a_storypack").by_id or status not in (0,1):
                    self.send(SC_READ_PLOT_OVER_RET,field_varint(1,32000))
                    return

        previous_profile = None
        table_battle_command = self.state.battle is not None and command_id in {
            CS_START_BATTLE,
            CS_END_BATTLE,
        }
        independent_battle_command = bool(
            (
                self.state.special_battles is not None
                and command_id in SPECIAL_BATTLE_RESPONSE_IDS
            )
            or (
                self.state.tower_battles is not None
                and command_id in TOWER_BATTLE_RESPONSE_IDS
            )
            or (
                self.state.tower_progression is not None
                and command_id in TOWER_PROGRESSION_RESPONSE_IDS
            )
        )
        if (
            command_id in {CS_END_BATTLE, CS_END_TASK_BATTLE}
            and not table_battle_command
        ):
            previous_profile = self.state.store.snapshot(self.account)
        if not table_battle_command and not independent_battle_command and not (
                command_id in (616, 937, 939) and self.state.progression is not None
                and newcomer_enabled(self.state.store.snapshot(self.account))):
            self.endpoint_runtime.observe_special(
                self.state,
                self.account,
                command_id,
                body,
                fields,
            )

        if self.kind == "queue":
            if command_id == CR_REQUEST_ENTER_QUEUE:
                self.account = first_string(fields, 1, "local")
                self.role_id = self.state.role_for(self.account)
                profile = self.state.profile_for(self.account)
                self.role_name = str(profile.get("role_name") or self.state.role_name)
                self.send(RC_ENTER_QUEUE_RET, make_queue_ret(self.account))
                self.send(
                    NOTIFY_GAME_SERVER_READY,
                    make_server_ready(
                        self.account, self.state.host, self.state.game_port
                    ),
                )
            elif self.endpoint_runtime.handle(self, command_id, body, fields):
                return
            return

        # Once the role snapshot has initialized the runtime, any ordinary
        # request can project elapsed recovery. Heartbeat remains the normal
        # automatic path, while this also covers a runtime kept on a quiet UI.
        if self.initial_snapshots_sent and command_id not in {
            CS_HEARTBEAT,
            CS_REQUEST_LAST_RECOVER_ENERGY_TIME,
            CS_SELECT_ROLE,
        }:
            self._synchronize_energy(send_notifications=True)

        # Task/activity/fast/maze and Holy Tower battles use their own request
        # families rather than CS_StartBattle/CS_EndBattle.  Keep them ahead
        # of the generated observer and legacy stubs so their state, reward
        # ledger, reconnect snapshot, and duplicate-settlement guarantees are
        # committed as one account transaction.
        for layer_name, layer, response_ids in (
            (
                "special-battle",
                self.state.special_battles,
                SPECIAL_BATTLE_RESPONSE_IDS,
            ),
            (
                "tower-battle",
                self.state.tower_battles,
                TOWER_BATTLE_RESPONSE_IDS,
            ),
            (
                "tower-progression",
                self.state.tower_progression,
                TOWER_PROGRESSION_RESPONSE_IDS,
            ),
        ):
            if layer is None or command_id not in response_ids:
                continue
            specialized_result, before, after = (
                self._transact_domain_request_with_snapshots(
                    layer,
                    command_id,
                    body,
                )
            )
            if specialized_result is None:
                continue
            for notification_id, notification_body in make_battle_state_notifications(
                self.role_id,
                before,
                after,
                self.state.collectibles,
                self.state.combat_attributes,
                getattr(self.state, 'stage_tasks', None),
            ):
                self.send(notification_id, notification_body)
            self.send(specialized_result.response_id, specialized_result.body)
            LOG.info(
                "%s endpoint %d -> %d, error=%d replay=%s rewards=%d changes=%s%s",
                layer_name,
                command_id,
                specialized_result.response_id,
                int(getattr(specialized_result, "error_code", 0) or 0),
                bool(getattr(specialized_result, "replayed", False)),
                len(tuple(getattr(specialized_result, "rewards", ()) or ())),
                sorted(getattr(specialized_result, "changes", {}) or {}),
                f", reason={specialized_result.reason}"
                if getattr(specialized_result, "reason", "")
                else "",
            )
            return

        # Rich catalog-backed content layers take precedence over both the
        # older hand-written subset and the schema-only generic responder.
        # Their change sets contain only top-level profile keys and can use
        # the same atomic shallow-merge contract as the existing services.
        for layer_name, layer, response_ids in (
            ("player-progression", self.state.progression, PLAYER_PROGRESSION_RESPONSE_IDS),
            ("gospel-reset", getattr(self.state, "gospel_reset", None), {1092: 1093}),
            (
                "gospel-progression",
                self.state.gospel_progression,
                GOSPEL_PROGRESSION_RESPONSE_IDS,
            ),
            (
                "hero-progression",
                self.state.hero_progression,
                HERO_PROGRESSION_RESPONSE_IDS,
            ),
            ("collectibles", self.state.collectibles, COLLECTIBLE_RESPONSE_IDS),
            ("spring", self.state.spring, SPRING_RESPONSE_IDS),
            (
                "private-equip",
                self.state.private_equip,
                PRIVATE_EQUIP_RESPONSE_IDS,
            ),
            ("activity", self.state.activity, ACTIVITY_RESPONSE_IDS),
            ("stage-tasks", getattr(self.state, "stage_tasks", None), {592: 593, 594: 595, 596: 597}),
            ("archive", self.state.archive, ARCHIVE_RESPONSE_IDS),
            ("passport", self.state.passport, PASSPORT_RESPONSE_IDS),
            ("formation", self.state.formation, FORMATION_RESPONSE_IDS),
            ("lilia", self.state.lilia, LILIA_RESPONSE_IDS),
            # Both legacy Oracle scaffolding and Life know command 472.
            # The owned-fish state must win over Oracle's empty compatibility
            # snapshot, otherwise the aquarium hides every persisted fish.
            ("life", self.state.life, LIFE_RESPONSE_IDS),
            ("farm", getattr(self.state, "farm", None), FARM_RESPONSE_IDS),
            ("race", getattr(self.state, "race", None), RACE_RESPONSE_IDS),
            ("oracle", self.state.oracle, ORACLE_RESPONSE_IDS),
            (
                "profile-customization",
                self.state.profile_customization,
                PROFILE_CUSTOMIZATION_RESPONSE_IDS,
            ),
            ("item-use", getattr(self.state.shop, "item_use_layer", None), {10: 11}),
            ("shop", self.state.shop, {**SHOP_RESPONSE_IDS, 1409: 1410}),
        ):
            if layer is None or command_id not in response_ids:
                continue
            resource_notifications = ()
            if self.state.economy_mode == NORMAL_ECONOMY or (layer_name == "shop" and command_id == 63):
                specialized_result, before, after = self._transact_domain_request_with_snapshots(layer, command_id, body)
                resource_notifications = make_battle_state_notifications(self.role_id, {} if getattr(specialized_result,"replayed",False) else before, after, self.state.collectibles, self.state.combat_attributes, getattr(self.state, 'stage_tasks', None))
            else:
                specialized_result = self._transact_domain_request(layer, command_id, body)
            if specialized_result is None:
                continue
            notifications = tuple(
                getattr(specialized_result, "notifications", ()) or ()
            )
            post_notifications = tuple(
                getattr(specialized_result, "post_notifications", ()) or ()
            )
            missing_resources = tuple(n for n in resource_notifications if n not in notifications and n not in post_notifications)
            if layer_name == "shop" and post_notifications:
                post_notifications = missing_resources + post_notifications
            else:
                notifications = missing_resources + notifications
            # Some response callbacks immediately read runtime-side state
            # populated by a preceding server notification.  Preserve that
            # source ordering (notably spring task 1354 before 1347).
            for notification_id, notification_body in notifications:
                self.send(int(notification_id), bytes(notification_body))
            if (layer_name == "private-equip" and command_id in (1380, 1382)
                    and specialized_result.ok and specialized_result.changes):
                current = self.state.store.snapshot(self.account)
                changed_uid = first_int(fields, 1)
                equipment = next(r for r in current["private_equipment"] if r["pequip_uid"] == changed_uid)
                gospel = next(r for r in current["gospels"] if r["id"] == equipment["pequip_id"])
                resolved = self.state.combat_attributes.resolve_gospel(gospel, current)
                self.send(42, field_message(1, make_local_gospel(
                    gospel, resolved.wire_attributes, resolved.combat_power)))
            self.send(specialized_result.response_id, specialized_result.body)
            # Local shop orders complete synchronously, but the runtime only
            # accepts inventory/charge notifications after SC_CreateOrderRet.
            # Keep those notifications distinct from the pre-response stream
            # used by modules such as spring-dialog initialization.
            for notification_id, notification_body in post_notifications:
                self.send(int(notification_id), bytes(notification_body))
            LOG.info(
                "%s endpoint %d -> %d, changes=%s, notifications=%s, "
                "post_notifications=%s%s",
                layer_name,
                command_id,
                specialized_result.response_id,
                sorted(specialized_result.changes),
                [notification_id for notification_id, _ in notifications],
                [notification_id for notification_id, _ in post_notifications],
                f", reason={specialized_result.reason}"
                if specialized_result.reason
                else "",
            )
            return

        # The all-unlocked profile uses catalog-backed handlers for endpoints
        # whose repeated/nested payloads cannot be represented by the generic
        # scalar runtime.  Apply each returned change set through the atomic
        # account store before acknowledging it to the runtime.
        if self.state.rewards is not None and command_id in REWARD_RESPONSE_IDS:
            reward_result = self._transact_visible_resources(
                self.state.rewards,
                command_id,
                body,
            )
            if reward_result is not None:
                for notification_id, notification_body in reward_result.notifications:
                    self.send(notification_id, notification_body)
                self.send(reward_result.response_id, reward_result.body)
                LOG.info(
                    "reward endpoint %d -> %d, error=%d, claims=%s, changes=%s, supported=%s%s",
                    command_id,
                    reward_result.response_id,
                    reward_result.error_code,
                    reward_result.claimed_ids,
                    sorted(reward_result.changes),
                    reward_result.supported,
                    f", reason={reward_result.reason}" if reward_result.reason else "",
                )
                return

        if (
            self.state.content_responses is not None
            and command_id in CONTENT_RESPONSE_IDS
        ):
            if command_id == CS_READ_PLOT_OVER and newcomer_enabled(current_profile or {}):
                content_result, before_content, after_content = self._transact_domain_request_with_snapshots(
                    self.state.content_responses,command_id,body)
                for notification_id, notification_body in make_battle_state_notifications(
                        self.role_id,before_content,after_content,self.state.collectibles,self.state.combat_attributes,getattr(self.state, 'stage_tasks', None)):
                    self.send(notification_id,notification_body)
                newly_completed=set(after_content.get('completed_dungeons',[]))-set(before_content.get('completed_dungeons',[]))
                for dungeon_id in sorted(newly_completed):
                    self.send(NOTIFY_UPDATE_DUNGEON,make_notify_update_dungeon(self.role_id,dungeon_id))
            else:
                content_result = self._transact_domain_request(
                    self.state.content_responses,command_id,body)
            if content_result is not None:
                self.send(content_result.response_id, content_result.body)
                LOG.info(
                    "content endpoint %d -> %d, supported=%s, changes=%s%s",
                    command_id,
                    content_result.response_id,
                    content_result.supported,
                    sorted(content_result.changes),
                    f", reason={content_result.reason}"
                    if content_result.reason
                    else "",
                )
                return

        if self.state.teaching is not None and command_id in TEACHING_RESPONSE_IDS:
            teaching_result = self._transact_domain_request(
                self.state.teaching,
                command_id,
                body,
            )
            if teaching_result is not None:
                self.send(teaching_result.response_id, teaching_result.body)
                LOG.info(
                    "teaching endpoint %d -> %d, changes=%s%s",
                    command_id,
                    teaching_result.response_id,
                    sorted(teaching_result.changes),
                    f", reason={teaching_result.reason}"
                    if teaching_result.reason
                    else "",
                )
                return

        if self.state.soul_gacha is not None and command_id in SOUL_GACHA_RESPONSE_IDS:
            soul_result = self._transact_visible_resources(
                self.state.soul_gacha,
                command_id,
                body,
            )
            if soul_result is not None:
                self.send(soul_result.response_id, soul_result.body)
                LOG.info(
                    "soul endpoint %d -> %d, error=%d, pool=%d, draws=%d, changes=%s%s",
                    command_id,
                    soul_result.response_id,
                    soul_result.error_code,
                    soul_result.pool_id,
                    len(soul_result.draws),
                    sorted(soul_result.changes),
                    f", reason={soul_result.reason}" if soul_result.reason else "",
                )
                return

        if self.state.lottery is not None and command_id == LOTTERY_REQUEST_ID:
            lottery_result = self._transact_visible_resources(
                self.state.lottery,
                command_id,
                body,
            )
            if lottery_result is not None:
                self.send(lottery_result.response_id, lottery_result.body)
                LOG.info(
                    "lottery endpoint %d -> %d, error=%d, subactivity=%d, pulls=%d, changes=%s%s",
                    command_id,
                    lottery_result.response_id,
                    lottery_result.error_code,
                    lottery_result.subactivity_id,
                    len(lottery_result.pulls),
                    sorted(lottery_result.changes),
                    f", reason={lottery_result.reason}"
                    if lottery_result.reason
                    else "",
                )
                return

        if self.state.gacha is not None and command_id == GACHA_REQUEST_ID:
            gacha_result = self._transact_visible_resources(
                self.state.gacha,
                command_id,
                body,
            )
            if gacha_result is not None:
                if "tutorial_draw" in gacha_result.changes:
                    self.send(NOTIFY_UPDATE_DUNGEON,make_notify_update_dungeon(self.role_id,8600012))
                self.send(gacha_result.response_id, gacha_result.body)
                if gacha_result.error_code == 0 and self.state.content_responses is not None:
                    self.send(369, self.state.content_responses.make_card_pool_group_datas_ret(
                        self.state.store.snapshot(self.account)))
                LOG.info(
                    "gacha endpoint %d -> %d, error=%d, pool=%d, draws=%d, changes=%s%s",
                    command_id,
                    gacha_result.response_id,
                    gacha_result.error_code,
                    gacha_result.card_pool_id,
                    len(gacha_result.draws),
                    sorted(gacha_result.changes),
                    f", reason={gacha_result.reason}" if gacha_result.reason else "",
                )
                return

        if command_id == CS_LOGIN:
            self.account = first_string(fields, 1, "local") or "local"
            self.role_id = self.state.role_for(self.account)
            profile = self.state.profile_for(self.account)
            self.role_name = str(profile.get("role_name") or self.state.role_name)
            self.send(
                SC_LOGIN_RET,
                make_login_ret(
                    self.account,
                    self.role_id,
                    self.state.host,
                    self.state.game_port,
                    self.state.token,
                    self.role_name,
                ),
            )
        elif command_id == CS_CREATE_ROLE:
            self.account = first_string(fields, 1, self.account) or self.account
            requested_name = first_string(fields, 2, self.state.role_name)
            self.role_name = requested_name or self.state.role_name
            self.role_id = self.state.role_for(self.account)
            self.state.store.update_account(
                self.account,
                role_name=self.role_name,
            )
            self.send(
                SC_CREATE_ROLE_RET, make_create_role_ret(self.role_id, self.role_name)
            )
        elif command_id == CS_SELECT_ROLE:
            requested_role = first_int(fields, 2, self.role_id)
            self.role_id = requested_role or self.role_id
            profile = self._synchronize_energy(send_notifications=False)
            profile = self._record_login_day()
            if self.role_id != int(profile.get("role_id") or self.role_id):
                # A reconnect may carry the role ID from the runtime.  Preserve
                # the existing local profile while making the active session
                # use the requested ID for protocol echoes.
                self.role_id = int(profile.get("role_id") or self.role_id)
            self.role_name = str(profile.get("role_name") or self.role_name)
            self.send(
                SC_SELECT_ROLE_RET,
                make_select_role_ret(
                    self.account,
                    self.role_id,
                    self.role_name,
                    profile,
                ),
            )
            self.state.mark_role_selected(self.account)
            # These are server notifications in the source flow.  A legacy
            # runtime treats parts of them as singleton initialization, so they
            # must only be sent once for this connection.
            self.ensure_initial_snapshots(profile)
        elif command_id == CS_HEARTBEAT:
            self._synchronize_energy(send_notifications=True)
            self.send(SC_HEARTBEAT_RET, make_heartbeat_ret(self.role_id))
        elif command_id == CS_REQUEST_LAST_RECOVER_ENERGY_TIME:
            profile = self._synchronize_energy(send_notifications=True)
            self.send(
                SC_LAST_RECOVER_ENERGY_TIME_RET,
                make_last_recover_energy_time(
                    int(profile.get(LAST_RECOVER_ENERGY_TIME_KEY, 0) or 0)
                ),
            )
        elif command_id == CS_DRAW_GOSPEL:
            is_new, draw_num = record_gospel_draw(
                self.state,
                self.account,
            )
            completed_story_dungeon = complete_tutorial_story_from_draw(
                self.state,
                self.account,
            )
            if completed_story_dungeon:
                # Update ChapterData before the summon result can close and
                # BattleMainFieldView.OnShow evaluates the tutorial chain.
                self.send(
                    NOTIFY_UPDATE_DUNGEON,
                    make_notify_update_dungeon(
                        self.role_id,
                        completed_story_dungeon,
                        0,
                    ),
                )
                LOG.info(
                    "tutorial draw advanced story-only dungeon %d",
                    completed_story_dungeon,
                )
            self.send(
                SC_DRAW_GOSPEL_RET,
                make_draw_gospel_ret(
                    fields,
                    self.role_id,
                    is_new,
                    draw_num,
                ),
            )
        elif command_id == CS_START_BATTLE:
            profile = self._synchronize_energy(send_notifications=True)
            # A reconnect/resume path can reach StartBattle before SelectRole.
            # In that case this remains the one-time initialization point.
            self.ensure_initial_snapshots(profile)
            start_notifications: Tuple[Tuple[int, bytes], ...] = ()
            if self.state.battle is not None:
                before_start = copy.deepcopy(profile)
                try:

                    battle_now = (
                        self.state.energy_recovery.now()
                        if self.state.energy_recovery is not None
                        else int(time.time())
                    )

                    def begin_battle(current: Dict[str, object]) -> Tuple[Any, Any]:
                        previous_energy = int(current.get("energy", 0) or 0)
                        before_costs = {"energy": previous_energy, "inventory": {
                            key: copy.deepcopy(current.get("inventory", {}).get(key, {}))
                            for key in ("1001049", "1001050")}}
                        selection = (
                            self.state.formation.battle_selection(current)
                            if self.state.formation is not None
                            else None
                        )
                        started = self.state.battle.start(
                            current,
                            dungeon_id=first_int(fields, 4, 0),
                            role_id=self.role_id,
                            target_id=first_int(fields, 3, 0),
                            now=battle_now,
                        )
                        if started.ok:
                            if selection is not None and selection.changes:
                                self.state.formation.apply_changes(
                                    current,
                                    selection.changes,
                                )
                            current.update(copy.deepcopy(started.changes))
                            if self.state.energy_recovery is not None:
                                current.update(
                                    self.state.energy_recovery.reconcile_energy_change(
                                        current,
                                        previous_energy=previous_energy,
                                        now=battle_now,
                                    )
                                )
                            if self.state.private_equip is not None:
                                self.state.private_equip.observe_action(before_costs, current)
                            if getattr(self.state, 'stage_tasks', None) is not None:
                                self.state.stage_tasks.observe(current)
                        return selection, started

                    (selection, started), _ = self.state.store.transact_account(
                        self.account,
                        begin_battle,
                    )
                except FormationApplicationError as exc:
                    self.send(
                        SC_START_BATTLE_RET,
                        self.state.formation.codec.encode(
                            "SC_StartBattleRet",
                            {"Errorcode": exc.error_code},
                        ),
                    )
                    LOG.warning("start battle rejected by formation: %s", exc)
                    return
                if not started.ok:
                    self.send(
                        SC_START_BATTLE_RET,
                        make_start_battle_ret(
                            fields,
                            self.role_id,
                            error_code=started.error_code,
                        ),
                    )
                    LOG.warning(
                        "start battle rejected dungeon=%d error=%d reason=%s",
                        first_int(fields, 4, 0),
                        started.error_code,
                        started.reason,
                    )
                    return
                if selection is not None:
                    LOG.info(
                        "start battle uses formation=%d active=%s candidates=%d",
                        selection.phalanx_id,
                        selection.active_heroes,
                        len(selection.candidate_uids),
                    )
                LOG.info(
                    "battle session=%d dungeon=%d energy_cost=%d",
                    started.session_id,
                    first_int(fields, 4, 0),
                    started.energy_cost,
                )
                after_start = self.state.store.snapshot(self.account)
                start_notifications = make_battle_state_notifications(
                    self.role_id,
                    before_start,
                    after_start,
                    self.state.collectibles,
                    self.state.combat_attributes,
                    getattr(self.state, 'stage_tasks', None),
                )
            elif self.state.formation is not None:
                try:
                    selection = self.state.formation.battle_selection(profile)
                except FormationApplicationError as exc:
                    self.send(
                        SC_START_BATTLE_RET,
                        self.state.formation.codec.encode(
                            "SC_StartBattleRet",
                            {"Errorcode": exc.error_code},
                        ),
                    )
                    LOG.warning("start battle rejected by formation: %s", exc)
                    return
                if selection.changes:
                    changes = copy.deepcopy(selection.changes)

                    def apply_battle_formation(current: Dict[str, object]) -> None:
                        self.state.formation.apply_changes(current, changes)

                    self.state.store.mutate_account(
                        self.account,
                        apply_battle_formation,
                    )
            for notification_id, notification_body in start_notifications:
                self.send(notification_id, notification_body)
            self.send(
                SC_START_BATTLE_RET,
                make_start_battle_ret(fields, self.role_id),
            )
        elif command_id == CS_END_BATTLE:
            dungeon_id = first_int(fields, 4, 0)
            result = first_int(fields, 5, 1)
            settlement = None
            settlement_notifications: Tuple[Tuple[int, bytes], ...] = ()
            before_settlement = self.state.store.snapshot(self.account)
            after_settlement = before_settlement
            if self.state.battle is not None:

                def settle_battle(current: Dict[str, object]) -> BattleSettlementResult:
                    previous_energy = int(current.get("energy", 0) or 0)
                    planned = self.state.battle.settle(
                        current,
                        dungeon_id=dungeon_id,
                        role_id=self.role_id,
                        target_id=first_int(fields, 3, 0),
                        result=result,
                        battle_stats=parse_end_battle_stats(fields),
                    )
                    current.update(copy.deepcopy(planned.changes))
                    if planned.ok:
                        if "gospels" in planned.changes:
                            old_ids = {r["id"] for r in before_settlement.get("gospels",[])}
                            new_ids = {r["id"] for r in current.get("gospels",[])} - old_ids
                            if new_ids: self.state.collectibles.grant_gospel_decorations(current,new_ids)
                        self.state.finalize_progression(current)
                    if self.state.energy_recovery is not None and planned.ok:
                        current.update(
                            self.state.energy_recovery.reconcile_energy_change(
                                current,
                                previous_energy=previous_energy,
                            )
                        )
                    return planned

                settlement, _ = self.state.store.transact_account(
                    self.account,
                    settle_battle,
                )
                after_settlement = self.state.store.snapshot(self.account)
                if settlement.ok and not settlement.replayed:
                    settlement_notifications = make_battle_state_notifications(
                        self.role_id,
                        before_settlement,
                        after_settlement,
                        self.state.collectibles,
                        self.state.combat_attributes,
                        getattr(self.state, 'stage_tasks', None),
                    )
            elif previous_profile is not None:
                # The schema-runtime fallback records its small legacy battle
                # transaction in observe_special() before reaching this
                # branch.  Compare against the snapshot captured immediately
                # before that mutation, both for first-pass calculation and
                # for the same cache notifications as catalog-backed battles.
                settlement_notifications = make_battle_state_notifications(
                    self.role_id,
                    previous_profile,
                    before_settlement,
                    self.state.collectibles,
                    self.state.combat_attributes,
                    getattr(self.state, 'stage_tasks', None),
                )
            for notification_id, notification_body in settlement_notifications:
                self.send(notification_id, notification_body)
            if (
                result == 1
                and dungeon_id
                and (settlement is None or settlement.ok)
                and not (settlement is not None and settlement.replayed)
            ):
                next_dungeon = LOCAL_DUNGEON_NEXT.get(dungeon_id, 0)
                if self.state.battle is not None:
                    dungeon = self.state.battle.dungeons.get(dungeon_id)
                    if dungeon is not None and dungeon.next_dungeons:
                        next_dungeon = dungeon.next_dungeons[0]
                dungeon_star, achieved_conditions = dungeon_progress_values(
                    after_settlement,
                    dungeon_id,
                )
                self.send(
                    NOTIFY_UPDATE_DUNGEON,
                    make_notify_update_dungeon(
                        self.role_id,
                        dungeon_id,
                        next_dungeon,
                        star=dungeon_star,
                        achieved_conditions=achieved_conditions,
                    ),
                )
            self.send(
                SC_END_BATTLE_RET,
                make_end_battle_ret(
                    fields,
                    self.role_id,
                    previous_profile
                    if settlement is None and previous_profile is not None
                    else before_settlement,
                    settlement,
                ),
            )
            if settlement is not None:
                LOG.info(
                    "battle settlement session=%d dungeon=%d first=%s replay=%s rewards=%d/%d/%d",
                    settlement.session_id,
                    dungeon_id,
                    settlement.is_first_pass,
                    settlement.replayed,
                    len(settlement.fixed_rewards),
                    len(settlement.first_rewards),
                    len(settlement.random_rewards),
                )
        elif command_id == CS_READ_PLOT_OVER:
            before_plot = self.state.store.snapshot(self.account)
            completed_story_dungeon = record_plot_progress(
                self.state,
                self.account,
                fields,
            )
            if completed_story_dungeon:
                after_plot = self.state.store.snapshot(self.account)
                for notification_id, payload in make_battle_state_notifications(
                        self.role_id,before_plot,after_plot,self.state.collectibles,self.state.combat_attributes,getattr(self.state, 'stage_tasks', None)):
                    self.send(notification_id,payload)
                self.send(
                    NOTIFY_UPDATE_DUNGEON,
                    make_notify_update_dungeon(
                        self.role_id,
                        completed_story_dungeon,
                        0 if newcomer_enabled(after_plot) else LOCAL_DUNGEON_NEXT.get(completed_story_dungeon, 0),
                    ),
                )
                LOG.info(
                    "plot completion advanced story-only dungeon %d",
                    completed_story_dungeon,
                )
            self.send(SC_READ_PLOT_OVER_RET, make_read_plot_over_ret(fields))
        elif command_id == CS_START_TASK_BATTLE:
            self.send(
                SC_START_TASK_BATTLE_RET,
                make_start_task_battle_ret(
                    fields,
                    self.state.profile_for(self.account),
                ),
            )
        elif command_id == CS_END_TASK_BATTLE:
            self.send(SC_END_TASK_BATTLE_RET, make_end_task_battle_ret(fields))
        elif command_id == CS_REQUEST_FIRST_CHARGE:
            self.send(SC_FIRST_CHARGE_RET, make_first_charge_ret())
        elif command_id == CS_REQUEST_PASSPORT_DATA:
            self.send(SC_REQUEST_PASSPORT_DATA_RET, make_passport_data_ret())
        elif command_id == CS_REQUEST_CRAWL_TOWER_PERIOD_DATA:
            self.send(
                SC_CRAWL_TOWER_PERIOD_DATA_RET,
                make_crawl_tower_period_data_ret(),
            )
        elif command_id == CS_REQUEST_TASK_BATTLE_DATAS:
            self.send(
                SC_TASK_BATTLE_DATAS_RET,
                make_task_battle_datas_ret(
                    fields,
                    self.state.profile_for(self.account),
                ),
            )
        elif command_id == CS_GUIDE_STEP_MOVE:
            self.send(SC_GUIDE_STEP_MOVE_RET, make_guide_step_move_ret(fields))
        elif command_id == CS_REQUEST_HERO_APPEARANCE_DATAS:
            self.send(
                SC_HERO_APPEARANCE_DATAS_RET,
                make_hero_appearance_datas(self.state.profile_for(self.account)),
            )
        elif command_id == CS_REQUEST_CHOSEN_DUNGEON_IDS:
            self.send(
                SC_CHOSEN_DUNGEON_IDS_RET,
                make_chosen_dungeon_ids_ret(self.state.profile_for(self.account)),
            )
        elif command_id == CS_PHALANX_DATAS:
            self.send(
                SC_PHALANX_DATAS,
                make_phalanx_datas(self.state.profile_for(self.account)),
            )
        elif command_id == CS_UNLOCK_TEACH_GUIDE:
            self.send(SC_UNLOCK_TEACH_GUIDE_RET, make_unlock_teach_guide_ret(fields))
        elif self.endpoint_runtime.handle(self, command_id, body, fields):
            # Every statically known endpoint reaches this path unless it has
            # a deliberately richer hand-written handler above.  This keeps
            # the generated registry active without replacing battle/login
            # payloads built by the hand-written handlers.
            return
        elif command_id in INITIAL_EMPTY_RESPONSES:
            # Compatibility fallback for a command not present in the current
            # catalog.  Known endpoints are handled by the generated runtime
            # above, so this table can no longer hide a stateful response.
            self.send(INITIAL_EMPTY_RESPONSES[command_id])
        elif command_id < 30000 and command_id % 2 == 0:
            # Most game requests use adjacent CS/SC command IDs.  An empty
            # response is valid protobuf and is useful while the initial data
            # handlers are being mapped one by one.
            self.send(command_id + 1)
        else:
            LOG.debug("ignoring command id=%d", command_id)


class ThreadedTCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class TCPFactory:
    def __init__(
        self,
        kind: str,
        state: GameState,
        recorder: Optional[SessionRecorder] = None,
        endpoint_runtime: Optional[ProtocolEndpointRuntime] = None,
    ):
        self.kind = kind
        self.state = state
        self.recorder = recorder
        self.endpoint_runtime = endpoint_runtime or ProtocolEndpointRuntime()

    def __call__(
        self,
        request: socket.socket,
        client_address: Tuple[str, int],
        server: socketserver.BaseServer,
    ):
        ProtocolHandler(
            request,
            client_address,
            self.kind,
            self.state,
            self.recorder,
            self.endpoint_runtime,
        ).run()


class FinderHandler(http.server.BaseHTTPRequestHandler):
    state: GameState
    static_root: Optional[Path] = None

    def _serve_static(self, path: str) -> bool:
        root = self.static_root
        if root is None:
            return False

        relative = unquote(path).lstrip("/")
        parts = Path(relative).parts
        if not relative or any(part in ("", ".", "..") or part.startswith(".") for part in parts):
            return False

        candidate = (root / Path(*parts)).resolve()
        if not candidate.is_file() and candidate.suffix == "":
            candidate = candidate.with_name(candidate.name + ".dat")
        try:
            candidate.relative_to(root)
        except ValueError:
            return False
        if not candidate.is_file():
            return False

        size = candidate.stat().st_size
        content_type = "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(size))
        self.end_headers()
        with candidate.open("rb") as source:
            shutil.copyfileobj(source, self.wfile, length=1024 * 1024)
        LOG.info("http static %s -> %s (%d bytes)", path, candidate, size)
        return True

    def do_GET(self) -> None:  # noqa: N802 - stdlib callback name
        path = self.path.split("?", 1)[0]
        content_type = "application/json"
        if path == "/v2/keys/serverlist":
            # The runtime treats code=32768 as IPBLOCKED.  A normal response
            # must carry the queue list in `server_addr`; NetLogic then
            # extracts the `name-host:port` entry from that string.
            body = json.dumps(
                {
                    "code": 0,
                    "server_addr": f"queue-{self.state.host}:{self.state.queue_port}",
                },
                separators=(",", ":"),
            ).encode("utf-8")
        elif path.lower().endswith("version.txt"):
            # A locally authorized runtime may point its bootstrap URL here.
            # Matching the local resource version keeps version checks offline.
            body = json.dumps(
                {
                    "ForceUpdateGame": False,
                    "LatestGameVersion": "1.0.0",
                    "InternalGameVersion": self.state.internal_game_version,
                    "InternalResourceVersion": self.state.internal_resource_version,
                    "UpdatePrefixUri": f"http://{self.state.host}:{self.state.http_port}/",
                    "VersionListLength": 0,
                    "VersionListHashCode": 0,
                    "VersionListCompressedLength": 0,
                    "VersionListCompressedHashCode": 0,
                    "ForceUpdatUri": "",
                    "BlockUpdateFix": 0,
                },
                separators=(",", ":"),
            ).encode("utf-8")
        else:
            if self._serve_static(path):
                return
            LOG.warning("http 404 %s", path)
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        LOG.info("http %s -> %s", path, body.decode("utf-8"))

    def do_POST(self) -> None:  # noqa: N802 - stdlib callback name
        self.send_error(404)

    def log_message(self, format: str, *args: object) -> None:
        LOG.debug("finder %s - %s", self.address_string(), format % args)


class ThreadedHTTPServer(http.server.ThreadingHTTPServer):
    daemon_threads = True


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="local guest server for the application protocol"
    )
    parser.add_argument(
        "--bind", default="127.0.0.1", help="listen address; default is loopback only"
    )
    parser.add_argument(
        "--advertise-host", default=None, help="host sent to the Android client"
    )
    parser.add_argument("--http-port", type=int, default=18080)
    parser.add_argument("--queue-port", type=int, default=19001)
    parser.add_argument("--game-port", type=int, default=19000)
    parser.add_argument(
        "--static-root",
        type=Path,
        default=None,
        help="read-only directory exposed for Framework/resource HTTP requests",
    )
    parser.add_argument(
        "--record-root",
        type=Path,
        default=None,
        help="write one timestamped session directory with raw queue/game packets",
    )
    parser.add_argument(
        "--state-file",
        type=Path,
        default=Path("runtime/local-state.json"),
        help="persistent local account state JSON; use an empty path only in tests",
    )
    parser.add_argument(
        "--profile-mode",
        choices=("minimal", "all-unlocked"),
        default="all-unlocked",
        help="seed Test as a catalog-constrained all-unlocked profile by default",
    )
    parser.add_argument(
        "--content-root",
        type=Path,
        default=Path("generated/content"),
        help="generated static content catalog used by all-unlocked mode",
    )
    parser.add_argument(
        "--log-file",
        type=Path,
        default=None,
        help="also persist structured server logs to this local file",
    )
    parser.add_argument("--debug", action="store_true")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    log_handlers: List[logging.Handler] = [logging.StreamHandler()]
    if args.log_file is not None:
        log_path = args.log_file.resolve()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_handlers.append(logging.FileHandler(log_path, encoding="utf-8"))
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=log_handlers,
    )
    advertise_host = args.advertise_host or args.bind
    FinderHandler.static_root = args.static_root.resolve() if args.static_root else None
    state = GameState(
        host=advertise_host,
        queue_port=args.queue_port,
        game_port=args.game_port,
        http_port=args.http_port,
        state_file=args.state_file,
        profile_mode=args.profile_mode,
        content_root=args.content_root,
    )
    recorder = SessionRecorder(args.record_root) if args.record_root else None
    endpoint_runtime = ProtocolEndpointRuntime()
    if recorder:
        LOG.info("recording session: %s", recorder.session_dir)

    FinderHandler.state = state
    finder = ThreadedHTTPServer((args.bind, args.http_port), FinderHandler)
    queue = ThreadedTCPServer(
        (args.bind, args.queue_port),
        TCPFactory("queue", state, recorder, endpoint_runtime),
    )
    game = ThreadedTCPServer(
        (args.bind, args.game_port),
        TCPFactory("game", state, recorder, endpoint_runtime),
    )

    servers = (finder, queue, game)
    threads = [
        threading.Thread(target=finder.serve_forever, name="finder", daemon=True),
        threading.Thread(target=queue.serve_forever, name="queue", daemon=True),
        threading.Thread(target=game.serve_forever, name="game", daemon=True),
    ]
    for thread in threads:
        thread.start()

    LOG.info("finder: http://%s:%d/v2/keys/serverlist", args.bind, args.http_port)
    LOG.info(
        "queue: %s:%d; game: %s:%d",
        args.bind,
        args.queue_port,
        advertise_host,
        args.game_port,
    )
    LOG.info("local guest mode; persistent state: %s", state.store.path or "memory")
    LOG.info(
        "profile mode: %s; content catalog: %s",
        state.profile_mode,
        state.content_catalog.version if state.content_catalog else "disabled",
    )
    LOG.info(
        "generated endpoint runtime ready: %d endpoints, catalog=%s",
        len(endpoint_runtime.endpoints),
        endpoint_runtime.catalog_path,
    )
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        LOG.info("stopping")
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    main()
