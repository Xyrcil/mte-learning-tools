"""Stateful offline support for star-trace equipment.

The runtime creates level-one, UID-zero placeholders before command 1385.  A
successful but empty response therefore means "not owned".  This layer seeds
one catalog-backed piece for every usable Gospel and returns durable objects
for the snapshot and the small set of mutation endpoints used by its UI.
"""

from __future__ import annotations

import copy
import json
import re
import time
from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Dict, Optional, Tuple, Union

try:  # Package imports in tests; fallback supports direct server execution.
    from .combat_attribute_resolver import CombatAttributeResolver
    from .content_profile import ContentCatalog, default_content_catalog
    from .content_responses import ProtobufDecodeError, iter_wire_fields
    from .inventory_content import (
        InventoryContentError,
        make_remove_item_packet,
        make_update_item_packet,
        normalize_inventory,
        stable_item_uid,
        update_inventory_item,
    )
    from .protobuf_codec import ProtocolMessageCodec
except ImportError:  # pragma: no cover
    from combat_attribute_resolver import CombatAttributeResolver  # type: ignore
    from content_profile import ContentCatalog, default_content_catalog  # type: ignore
    from content_responses import ProtobufDecodeError, iter_wire_fields  # type: ignore
    from inventory_content import (  # type: ignore
        InventoryContentError,
        make_remove_item_packet,
        make_update_item_packet,
        normalize_inventory,
        stable_item_uid,
        update_inventory_item,
    )
    from protobuf_codec import ProtocolMessageCodec  # type: ignore


CS_PRIVATE_EQUIP_LEVEL_UP = 1380
SC_PRIVATE_EQUIP_LEVEL_UP_RET = 1381
CS_PRIVATE_EQUIP_AWAKE = 1382
SC_PRIVATE_EQUIP_AWAKE_RET = 1383
CS_GET_PRIVATE_EQUIP_DATAS = 1385
SC_GET_PRIVATE_EQUIP_DATAS_RET = 1386
CS_EXCHANGE_PRIVATE_EQUIP_TICKET = 1387
SC_EXCHANGE_PRIVATE_EQUIP_TICKET_RET = 1388
CS_SET_PRIVATE_EQUIP_APPEARANCE = 1389
SC_SET_PRIVATE_EQUIP_APPEARANCE_RET = 1390
CS_LOOKUP_PRIVATE_EQUIP_MEMORY = 1391
SC_LOOKUP_PRIVATE_EQUIP_MEMORY_RET = 1392
CS_REQUEST_PRIVATE_EQUIP_STAGE_TASK = 1393
SC_REQUEST_PRIVATE_EQUIP_STAGE_TASK_RET = 1394
CS_TAKE_PRIVATE_EQUIP_STAGE_REWARD = 1395
SC_TAKE_PRIVATE_EQUIP_STAGE_REWARD_RET = 1396

RESPONSE_IDS = {
    CS_PRIVATE_EQUIP_LEVEL_UP: SC_PRIVATE_EQUIP_LEVEL_UP_RET,
    CS_PRIVATE_EQUIP_AWAKE: SC_PRIVATE_EQUIP_AWAKE_RET,
    CS_GET_PRIVATE_EQUIP_DATAS: SC_GET_PRIVATE_EQUIP_DATAS_RET,
    CS_EXCHANGE_PRIVATE_EQUIP_TICKET: SC_EXCHANGE_PRIVATE_EQUIP_TICKET_RET,
    CS_SET_PRIVATE_EQUIP_APPEARANCE: SC_SET_PRIVATE_EQUIP_APPEARANCE_RET,
    CS_LOOKUP_PRIVATE_EQUIP_MEMORY: SC_LOOKUP_PRIVATE_EQUIP_MEMORY_RET,
    CS_REQUEST_PRIVATE_EQUIP_STAGE_TASK: SC_REQUEST_PRIVATE_EQUIP_STAGE_TASK_RET,
    CS_TAKE_PRIVATE_EQUIP_STAGE_REWARD: SC_TAKE_PRIVATE_EQUIP_STAGE_REWARD_RET,
}

ERROR_SUCCESS = 0
ERROR_ITEM_COUNT_NOT_ENOUGH = 110
ERROR_GOLD_NOT_ENOUGH = 108
ERROR_GOSPEL_LEVEL_NOT_ENOUGH = 207
ERROR_RULES_UNAVAILABLE = 32704
ERROR_EXCEED_ITEM_LIMIT_COUNT = 429
ERROR_MESSAGE_DATA_ILLEGAL = 32769
PRIVATE_EQUIP_UID_BASE = 3_000_000_001
PRIVATE_EQUIP_REVISION = 2
UINT32_MAX = (1 << 32) - 1
UINT64_MAX = (1 << 64) - 1

# These IDs are hard-coded by the defined BuyTicketPop runtime.  The ratio
# and storage cap remain catalog-backed below (a_global 9991130/9991131).
PRIVATE_EQUIP_TICKET_ITEM_ID = 1_001_049
PRIVATE_EQUIP_TICKET_MATERIAL_ID = 1_001_051
PRIVATE_EQUIP_EXCHANGE_GLOBAL_ID = 9_991_130
PRIVATE_EQUIP_TICKET_LIMIT_GLOBAL_ID = 9_991_131


class PrivateEquipCatalogError(ValueError):
    pass


class PrivateEquipRequestError(ValueError):
    def __init__(self, code: int, reason: str):
        self.code = code
        super().__init__(reason)


def _strict_uint(value: Any, maximum: int = UINT64_MAX, minimum: int = 0) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise PrivateEquipRequestError(ERROR_MESSAGE_DATA_ILLEGAL, "invalid persisted integer")
    return value


@dataclass(frozen=True)
class PrivateEquipDefinition:
    pequip_id: int
    max_level: int
    max_awake: int


@dataclass(frozen=True)
class PrivateEquipExchangeDefinition:
    material_item_id: int
    ticket_item_id: int
    material_per_exchange: int
    tickets_per_exchange: int
    daily_recovery: int
    ticket_limit: int


@dataclass(frozen=True)
class PrivateEquipResult:
    response_id: int
    body: bytes
    changes: Dict[str, Any] = field(default_factory=dict)
    error_code: int = ERROR_SUCCESS
    reason: str = ""
    notifications: Tuple[Tuple[int, bytes], ...] = ()

    @property
    def ok(self) -> bool:
        return self.error_code == ERROR_SUCCESS


def _safe_uint(value: Any, default: int, maximum: int = UINT64_MAX) -> int:
    if isinstance(value, bool):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return parsed if 0 <= parsed <= maximum else default


def _decode_uint(data: bytes, field_number: int, maximum: int) -> int:
    result = 0
    found = False
    for number, wire_type, value in iter_wire_fields(bytes(data)):
        if number != field_number:
            continue
        if wire_type != 0:
            raise ProtobufDecodeError(f"field {field_number} is not a varint")
        result = int(value)
        found = True
    if not found or result > maximum:
        raise ProtobufDecodeError(f"field {field_number} is missing or invalid")
    return result


class PrivateEquipCatalog:
    def __init__(self, catalog: ContentCatalog) -> None:
        try:
            table = catalog.table("a_soul_information")
        except KeyError:
            raise PrivateEquipCatalogError(
                "catalog is missing a_soul_information"
            ) from None
        required = {"id", "max_level", "max_awake", "effects_display"}
        missing = required - set(table.fields)
        if missing:
            raise PrivateEquipCatalogError(
                f"a_soul_information is missing fields: {sorted(missing)}"
            )
        rows = {int(row["id"]): row for row in table.records}
        if set(rows) != set(catalog.usable_gospel_ids):
            raise PrivateEquipCatalogError(
                "a_soul_information must cover every usable Gospel exactly"
            )
        definitions: Dict[int, PrivateEquipDefinition] = {}
        for gospel_id in catalog.usable_gospel_ids:
            row = rows[gospel_id]
            maximum = int(row["max_level"])
            max_awake = int(row["max_awake"])
            effects_display = int(row["effects_display"])
            # Six early/special pieces intentionally have max_awake=0 and an
            # unreachable effects_display=99: they are owned tokens without a
            # repair tree or 3D attachment, not malformed rows.
            if maximum <= 0 or max_awake < 0 or effects_display < 0:
                raise PrivateEquipCatalogError(
                    f"a_soul_information[{gospel_id}] has invalid maxima"
                )
            definitions[gospel_id] = PrivateEquipDefinition(
                pequip_id=gospel_id,
                max_level=maximum,
                max_awake=max_awake,
            )
        self.definitions = MappingProxyType(definitions)
        self.ids = tuple(catalog.usable_gospel_ids)
        self.exchange = self._load_exchange(catalog)
        self.content = catalog
        # These are the exact defined runtime tables, not values inferred
        # from the names/descriptions of the items. Compatible 2.0-only souls
        # may lack 1.0 rows; their missing growth rules are explicit refusals.
        try:
            self.levels = {
                (int(row["gospel_id"]), int(row["level"])): row
                for row in catalog.table("a_soul_level").records
            }
            self.awake = {
                (int(row["gospel_id"]), int(row["awaken_level"])): row
                for row in catalog.table("a_soul_awake").records
            }
        except KeyError:
            self.levels, self.awake = {}, {}
        self.unlock_level = int(catalog.record("a_global", 9_991_129)["int_value"])

    def item_costs(self, raw: Any) -> Dict[int, int]:
        try:
            values = json.loads(raw)
            if not isinstance(values, list):
                raise ValueError("cost must be an array")
            result: Dict[int, int] = {}
            for row in values:
                if not isinstance(row, dict) or set(row) != {"type", "id", "num"}:
                    raise ValueError("unexpected cost record")
                if row["type"] != 4:
                    raise ValueError("unproven private equipment cost type")
                item = _strict_uint(row["id"], UINT32_MAX, 1)
                count = _strict_uint(row["num"], UINT32_MAX, 1)
                self.content.record("a_item", item)
                result[item] = result.get(item, 0) + count
            return result
        except (KeyError, TypeError, ValueError) as exc:
            raise PrivateEquipRequestError(ERROR_RULES_UNAVAILABLE, str(exc)) from exc

    @staticmethod
    def _positive_pair(raw: Any, label: str) -> Tuple[int, int]:
        if not isinstance(raw, str):
            raise PrivateEquipCatalogError(f"{label} must be text")
        pieces = raw.split(",")
        if len(pieces) != 2:
            raise PrivateEquipCatalogError(f"{label} must contain two integers")
        try:
            values = tuple(int(piece.strip()) for piece in pieces)
        except ValueError:
            raise PrivateEquipCatalogError(
                f"{label} must contain two integers"
            ) from None
        if any(value <= 0 for value in values):
            raise PrivateEquipCatalogError(f"{label} values must be positive")
        return values  # type: ignore[return-value]

    @classmethod
    def _load_exchange(
        cls, catalog: ContentCatalog
    ) -> PrivateEquipExchangeDefinition:
        try:
            exchange_row = catalog.record(
                "a_global", PRIVATE_EQUIP_EXCHANGE_GLOBAL_ID
            )
            limit_row = catalog.record(
                "a_global", PRIVATE_EQUIP_TICKET_LIMIT_GLOBAL_ID
            )
            item_table = catalog.table("a_item")
        except KeyError as exc:
            raise PrivateEquipCatalogError(
                "catalog is missing private-equipment ticket configuration"
            ) from exc
        item_rows = {int(row["id"]): row for row in item_table.records}
        for item_id in (
            PRIVATE_EQUIP_TICKET_ITEM_ID,
            PRIVATE_EQUIP_TICKET_MATERIAL_ID,
        ):
            if item_id not in item_rows:
                raise PrivateEquipCatalogError(
                    f"a_item is missing private-equipment item {item_id}"
                )
        material_cost, ticket_reward = cls._positive_pair(
            exchange_row.get("string_value"),
            f"a_global[{PRIVATE_EQUIP_EXCHANGE_GLOBAL_ID}].string_value",
        )
        daily_recovery, ticket_limit = cls._positive_pair(
            limit_row.get("string_value"),
            f"a_global[{PRIVATE_EQUIP_TICKET_LIMIT_GLOBAL_ID}].string_value",
        )
        maximum = int(item_rows[PRIVATE_EQUIP_TICKET_ITEM_ID]["max_stack_num"])
        if ticket_limit > maximum:
            raise PrivateEquipCatalogError(
                "private-equipment ticket limit exceeds a_item max_stack_num"
            )
        return PrivateEquipExchangeDefinition(
            material_item_id=PRIVATE_EQUIP_TICKET_MATERIAL_ID,
            ticket_item_id=PRIVATE_EQUIP_TICKET_ITEM_ID,
            material_per_exchange=material_cost,
            tickets_per_exchange=ticket_reward,
            daily_recovery=daily_recovery,
            ticket_limit=ticket_limit,
        )


class PrivateEquipContentLayer:
    def __init__(
        self,
        catalog: Optional[Union[ContentCatalog, str, Path]] = None,
        *,
        codec: Optional[ProtocolMessageCodec] = None,
    ) -> None:
        if catalog is None:
            self.content_catalog = default_content_catalog()
        elif isinstance(catalog, ContentCatalog):
            self.content_catalog = catalog
        else:
            self.content_catalog = ContentCatalog.load(catalog)
        self.catalog = PrivateEquipCatalog(self.content_catalog)
        self.codec = codec or ProtocolMessageCodec()
        self.combat_resolver = CombatAttributeResolver(self.content_catalog)
        self.tasks = {int(r["id"]): r for r in self.content_catalog.table("a_soul_task").records}
        self.stage_rewards = {int(r["count_stage"]): r for r in self.content_catalog.table("a_soul_task_reward").records}

    @staticmethod
    def note_login(profile, now=None):
        """Track observed local login days; never invent pre-migration history."""
        # Local compatibility calendar is UTC+8, matching this offline build.
        day = ((int(time.time()) if now is None else int(now)) + 8 * 3600) // 86400
        state = copy.deepcopy(profile.get("private_equipment_task_state", {}))
        days = list(state.get("login_days", []))
        if day not in days:
            days.append(day)
            state["login_days"] = sorted(days)
            profile["private_equipment_task_state"] = state

    @staticmethod
    def observe_action(before, after, *, soul_draws=0):
        """Only actual successful deltas count toward consumption tasks."""
        state = copy.deepcopy(after.get("private_equipment_task_state", {}))
        metrics = dict(state.get("consumed", {}))
        deltas = {"energy": max(0, int(before.get("energy", 0)) - int(after.get("energy", 0))),
                  "soul_draws": max(0, int(soul_draws))}
        for key, item in (("tickets", 1001049), ("coins", 1001050)):
            old = before.get("inventory", {}).get(str(item), {}).get("count", 0)
            new = after.get("inventory", {}).get(str(item), {}).get("count", 0)
            deltas[key] = max(0, int(old) - int(new))
        if not any(deltas.values()):
            return
        for key, value in deltas.items():
            metrics[key] = int(metrics.get(key, 0)) + value
        state["consumed"] = metrics
        after["private_equipment_task_state"] = state

    def _task_metric(self, profile, task):
        # Type IDs and thresholds were checked against the source
        # a_condition rows referenced by every a_soul_task. Progress comes
        # from real local facts; full-unlock visibility grants no history.
        words = task["words"]
        state = profile.get("private_equipment_task_state", {})
        metrics = state.get("consumed", {})
        if match := re.fullmatch(r"累积登录(\d+)天", words):
            return 7119, len(set(state.get("login_days", []))), int(match[1])
        if match := re.fullmatch(r"(\d+)个星痕信物补强达到(\d+)级", words):
            owned = {r.get("pequip_id") for r in profile.get("private_equipment", [])
                     if isinstance(r, Mapping) and int(r.get("pequip_uid", 0)) > 0
                     and int(r.get("level", 0)) >= int(match[2])}
            return 2132, len(owned), int(match[1])
        if match := re.fullmatch(r"进行(\d+)次信物修缮", words):
            actual = sum(r.get("source") == "private_equipment" and r.get("operation") == "awake_level"
                         for r in profile.get("resource_history", []) if isinstance(r, Mapping))
            return 2133, actual, int(match[1])
        if match := re.fullmatch(r"通关骚灵之境(\d+)", words):
            targets = {int(r["id"]) for r in self.content_catalog.table("a_dungeon").records if r.get("name") == words[2:]}
            return 3000, int(bool(targets & set(profile.get("completed_dungeons", [])))), 1
        for pattern, key, kind in ((r"在骚灵之境消耗(\d+)次挑战次数", "tickets", 2131),
                                   (r"消耗(\d+)星魂力", "energy", 2000),
                                   (r"消耗(\d+)骚灵币", "coins", 2130),
                                   (r"进行(\d+)次唤物", "soul_draws", 2134)):
            if match := re.fullmatch(pattern, words):
                return kind, int(metrics.get(key, 0)), int(match[1])
        return 0, 0, 1

    def _task_ready(self, profile, task):
        kind, progress, target = self._task_metric(profile, task)
        return bool(kind and progress >= target)

    def task_achievements(self, profile):
        achievements = []
        for task_id, task in sorted(self.tasks.items()):
            kind, progress, target = self._task_metric(profile, task)
            achievements.append({"Id": int(task["achievement"]), "Type": kind,
                "Progress": min(target, max(0, progress)),
                "Status": 1 if progress >= target else 0})
        return achievements

    def task_achievement_notification(self, profile):
        # 320 updates existing records only; 317 must first seed their IDs.
        return (320, self.codec.encode("Notify_UpdateAchieve", {
            "AchieveList": self.task_achievements(profile)}))

    def task_snapshot(self, profile):
        state = profile.get("private_equipment_task_state", {})
        claimed = set(state.get("claimed_tasks", []))
        return {"StageIdList": list(state.get("claimed_stages", [])),
                "TaskList": [{"Id": task_id, "Status": 2 if task_id in claimed else
                              (1 if self._task_ready(profile, task) else 0)}
                             for task_id, task in sorted(self.tasks.items())]}

    def _claim_task(self, profile, body):
        message = "SC_TakePEquipStageTaskRewardRet"
        values = {"Errorcode": 0, "StageTaskId": 0, "StageId": 0, "TakeType": 0, "RewardList": []}
        def error(code, reason):
            return PrivateEquipResult(1396, self.codec.encode(message, {**values, "Errorcode": code}),
                                      error_code=code, reason=reason)
        try:
            decoded = {}
            for number, wire_type, value in iter_wire_fields(bytes(body)):
                if number not in (1, 2, 3) or wire_type != 0 or number in decoded:
                    return error(ERROR_MESSAGE_DATA_ILLEGAL, "invalid stage claim fields")
                decoded[number] = _strict_uint(value, UINT32_MAX)
            task_id, stage, mode = (decoded.get(i, 0) for i in (1, 2, 3))
            values.update(StageTaskId=task_id, StageId=stage, TakeType=mode)
            if stage not in self.stage_rewards or mode not in (1, 2, 3):
                return error(ERROR_MESSAGE_DATA_ILLEGAL, "unknown stage/claim type")
            state = copy.deepcopy(profile.get("private_equipment_task_state", {}))
            claimed, stages = set(state.get("claimed_tasks", [])), set(state.get("claimed_stages", []))
            if any(previous not in stages for previous in self.stage_rewards if previous < stage):
                return error(260, "earlier stages have not been claimed")
            tasks = {tid for tid, row in self.tasks.items() if int(row["count_stage_id"]) == stage}
            if mode == 1:
                if task_id not in tasks:
                    return error(ERROR_MESSAGE_DATA_ILLEGAL, "task does not belong to stage")
                selected = {task_id} - claimed
                if not selected:
                    return PrivateEquipResult(1396, self.codec.encode(message, values), reason="already claimed")
                if not self._task_ready(profile, self.tasks[task_id]):
                    return error(5504, "task has not been completed")
            elif mode == 2:
                if task_id:
                    return error(ERROR_MESSAGE_DATA_ILLEGAL, "bulk claim must omit task ID")
                selected = {tid for tid in tasks - claimed if self._task_ready(profile, self.tasks[tid])}
                if not selected:
                    return error(5504, "no claimable tasks")
            else:
                if task_id:
                    return error(ERROR_MESSAGE_DATA_ILLEGAL, "stage claim must omit task ID")
                selected = set()
                if stage in stages:
                    return PrivateEquipResult(1396, self.codec.encode(message, values), reason="already claimed")
                if not tasks <= claimed:
                    return error(260, "not all stage task rewards claimed")
            rewards = {}
            rows = [self.tasks[tid] for tid in sorted(selected)] if mode != 3 else [self.stage_rewards[stage]]
            for row in rows:
                for item, count in self.catalog.item_costs(row["sign_awards"]).items():
                    rewards[item] = rewards.get(item, 0) + count
            working = copy.deepcopy(dict(profile))
            for item, count in rewards.items():
                before = int(profile.get("inventory", {}).get(str(item), {}).get("count", 0))
                limit = int(self.content_catalog.record("a_item", item)["max_stack_num"])
                if before < 0 or before + count > limit:
                    return error(ERROR_EXCEED_ITEM_LIMIT_COUNT, "stage reward would exceed inventory cap")
            notifications = []
            for item, count in sorted(rewards.items()):
                row = profile.get("inventory", {}).get(str(item), {})
                updated = update_inventory_item(working, item, int(row.get("count", 0)) + count,
                                                uid=row.get("uid"), para=row.get("para", 0))
                notifications.append(make_update_item_packet(int(profile.get("role_id", 0)), updated, self.codec))
                values["RewardList"].append({"ResType": 4, "ItemId": item, "Count": count})
            state["claimed_tasks"] = sorted(claimed | selected)
            state["claimed_stages"] = sorted(stages | ({stage} if mode == 3 else set()))
            working["private_equipment_task_state"] = state
            history = copy.deepcopy(profile.get("reward_history", []))
            history.append({"claim_key": f"private-equipment-stage:{stage}:{mode}:{task_id}",
                            "source": "private_equipment_stage", "task_ids": sorted(selected),
                            "rewards": [{"type": 4, "id": item, "num": n} for item, n in sorted(rewards.items())]})
            resources = copy.deepcopy(profile.get("resource_history", []))
            resources.append({"source": "private_equipment_stage", "command_id": 1395,
                              "stage_id": stage, "task_ids": sorted(selected), "costs": [],
                              "rewards": copy.deepcopy(history[-1]["rewards"])})
            notifications.append((1397, self.codec.encode("Notify_UpdatePEquipStageTask", {
                "TaskList": self.task_snapshot(working)["TaskList"]})))
            return PrivateEquipResult(1396, self.codec.encode(message, values),
                changes={"inventory": working["inventory"], "private_equipment_task_state": state,
                         "reward_history": history, "resource_history": resources}, notifications=tuple(notifications))
        except PrivateEquipRequestError as exc:
            return error(exc.code, str(exc))
        except (InventoryContentError, ProtobufDecodeError, TypeError, ValueError, KeyError) as exc:
            return error(ERROR_MESSAGE_DATA_ILLEGAL, str(exc))

    def seed_profile(
        self, profile: MutableMapping[str, Any]
    ) -> MutableMapping[str, Any]:
        previous = profile.get("private_equipment")
        previous_by_id: Dict[int, Mapping[str, Any]] = {}
        if isinstance(previous, list):
            for row in previous:
                if not isinstance(row, Mapping):
                    continue
                pequip_id = _safe_uint(row.get("pequip_id"), 0, UINT32_MAX)
                if pequip_id in self.catalog.definitions:
                    previous_by_id[pequip_id] = row

        rows = []
        newcomer = bool(profile.get("player_progression"))
        owned_gospels = {int(row["id"]) for row in profile.get("gospels", [])}
        for index, pequip_id in enumerate(self.catalog.ids):
            if newcomer and pequip_id not in owned_gospels:
                continue
            definition = self.catalog.definitions[pequip_id]
            source = previous_by_id.get(pequip_id, {})
            rows.append(
                {
                    "pequip_uid": PRIVATE_EQUIP_UID_BASE + index,
                    "pequip_id": pequip_id,
                    "level": _safe_uint(
                        source.get("level"), 1 if newcomer else definition.max_level, definition.max_level
                    )
                    or definition.max_level,
                    "awake_level": _safe_uint(
                        source.get("awake_level"),
                        0 if newcomer else definition.max_awake,
                        definition.max_awake,
                    ),
                    "appearance_flag": _safe_uint(
                        source.get("appearance_flag"), 0, 1
                    ),
                    "memory_flag": _safe_uint(source.get("memory_flag"), 0 if newcomer else 1, 1),
                }
            )
        profile["private_equipment"] = rows
        # The generic all-items seed uses the a_item stack size, but the
        # local-equipment UI has a stricter business cap in a_global
        # 9991131.  Clamp legacy/full-unlock saves once during this content
        # migration so the exchange screen is usable instead of permanently
        # returning ExceedItemLimitCount.  Spent balances below the cap remain
        # untouched.
        inventory = normalize_inventory(profile.get("inventory", {}))
        ticket = inventory.get(str(self.catalog.exchange.ticket_item_id))
        if (
            ticket is not None
            and int(ticket["count"]) > self.catalog.exchange.ticket_limit
        ):
            update_inventory_item(
                profile,
                self.catalog.exchange.ticket_item_id,
                self.catalog.exchange.ticket_limit,
                uid=int(ticket["uid"]),
                para=int(ticket["para"]),
            )
        profile["private_equipment_revision"] = PRIVATE_EQUIP_REVISION
        return profile

    @staticmethod
    def _message(row: Mapping[str, Any]) -> Dict[str, Any]:
        return {
            "PequipUid": _safe_uint(row.get("pequip_uid"), 0),
            "PequipId": _safe_uint(row.get("pequip_id"), 0, UINT32_MAX),
            "Level": _safe_uint(row.get("level"), 0, UINT32_MAX),
            "AwakeLevel": _safe_uint(row.get("awake_level"), 0, UINT32_MAX),
            "AppearanceFlag": _safe_uint(
                row.get("appearance_flag"), 0, UINT32_MAX
            ),
            "MemoryFlag": _safe_uint(row.get("memory_flag"), 0, UINT32_MAX),
        }

    def _seeded_rows(self, profile: Mapping[str, Any]) -> list[Dict[str, Any]]:
        working = copy.deepcopy(dict(profile))
        self.seed_profile(working)
        return copy.deepcopy(working["private_equipment"])

    @staticmethod
    def _find_by_uid(rows: list[Dict[str, Any]], uid: int) -> Optional[Dict[str, Any]]:
        matches = [row for row in rows if int(row.get("pequip_uid", 0)) == uid]
        return matches[0] if len(matches) == 1 else None

    def _wire_error(self, request_id: int, reason: str) -> PrivateEquipResult:
        response_id = RESPONSE_IDS[request_id]
        message_name = {
            CS_PRIVATE_EQUIP_LEVEL_UP: "SC_PrivateEquipLevelUpRet",
            CS_PRIVATE_EQUIP_AWAKE: "SC_PrivateEquipAwakeRet",
            CS_GET_PRIVATE_EQUIP_DATAS: "SC_GetPrivateEquipDatasRet",
            CS_EXCHANGE_PRIVATE_EQUIP_TICKET: "SC_ExchangePEquipTicketRet",
            CS_SET_PRIVATE_EQUIP_APPEARANCE: "SC_SetPEquipAppearanceRet",
            CS_LOOKUP_PRIVATE_EQUIP_MEMORY: "SC_LookupPEquipMemoryRet",
            CS_REQUEST_PRIVATE_EQUIP_STAGE_TASK: "SC_RequestPEquipStageTaskDataRet",
        }[request_id]
        values: Dict[str, Any] = {}
        if request_id not in {CS_REQUEST_PRIVATE_EQUIP_STAGE_TASK}:
            values["Errorcode"] = ERROR_MESSAGE_DATA_ILLEGAL
        return PrivateEquipResult(
            response_id,
            self.codec.encode(message_name, values),
            error_code=ERROR_MESSAGE_DATA_ILLEGAL,
            reason=reason,
        )

    def _grow(self, request_id: int, profile: Mapping[str, Any], body: bytes) -> PrivateEquipResult:
        message = ("SC_PrivateEquipLevelUpRet" if request_id == CS_PRIVATE_EQUIP_LEVEL_UP
                   else "SC_PrivateEquipAwakeRet")
        try:
            fields = list(iter_wire_fields(bytes(body)))
            if len(fields) != 1 or fields[0][0:2] != (1, 0):
                raise PrivateEquipRequestError(ERROR_MESSAGE_DATA_ILLEGAL, "expected one private equip UID")
            uid = _strict_uint(fields[0][2], UINT64_MAX, 1)
            source = profile.get("private_equipment")
            if not isinstance(source, list):
                raise PrivateEquipRequestError(ERROR_MESSAGE_DATA_ILLEGAL, "missing owned equipment")
            matches = [r for r in source if isinstance(r, Mapping) and r.get("pequip_uid") == uid]
            if len(matches) != 1:
                raise PrivateEquipRequestError(ERROR_MESSAGE_DATA_ILLEGAL, "unknown or duplicate equip UID")
            row = copy.deepcopy(matches[0])
            gid = _strict_uint(row.get("pequip_id"), UINT32_MAX, 1)
            if sum(isinstance(r, Mapping) and r.get("pequip_id") == gid for r in source) != 1:
                raise PrivateEquipRequestError(ERROR_MESSAGE_DATA_ILLEGAL, "duplicate equipment identity")
            definition = self.catalog.definitions.get(gid)
            if definition is None:
                raise PrivateEquipRequestError(ERROR_MESSAGE_DATA_ILLEGAL, "unknown equipment definition")
            gospels = [r for r in profile.get("gospels", []) if isinstance(r, Mapping) and r.get("id") == gid]
            if len(gospels) != 1:
                raise PrivateEquipRequestError(ERROR_MESSAGE_DATA_ILLEGAL, "equipment owner not uniquely held")
            if _strict_uint(gospels[0].get("level"), UINT32_MAX) < self.catalog.unlock_level:
                raise PrivateEquipRequestError(ERROR_GOSPEL_LEVEL_NOT_ENOUGH, "equipment owner below unlock level")
            level = _strict_uint(row.get("level"), definition.max_level, 1)
            awake = _strict_uint(row.get("awake_level"), definition.max_awake)
            is_level = request_id == CS_PRIVATE_EQUIP_LEVEL_UP
            field_name = "level" if is_level else "awake_level"
            previous = level if is_level else awake
            maximum = definition.max_level if is_level else definition.max_awake
            if previous == maximum:
                return PrivateEquipResult(RESPONSE_IDS[request_id], self.codec.encode(
                    message, {"Errorcode": 0, "PequipInfo": self._message(row)}), reason="already at cap")
            if is_level:
                config = self.catalog.levels.get((gid, level))
                if config is None or (gid, level + 1) not in self.catalog.levels:
                    raise PrivateEquipRequestError(ERROR_RULES_UNAVAILABLE, "missing original level cost")
                gold_cost = _strict_uint(config.get("gold"), UINT32_MAX)
                costs = self.catalog.item_costs(config.get("item"))
            else:
                if (gid, awake + 1) not in self.catalog.awake:
                    raise PrivateEquipRequestError(ERROR_RULES_UNAVAILABLE, "missing original awakening step")
                info = self.catalog.content.record("a_soul_information", gid)
                costs = self.catalog.item_costs(info.get("awaken_item"))
                if not costs:
                    raise PrivateEquipRequestError(ERROR_RULES_UNAVAILABLE, "missing original awakening cost")
                gold_cost = 0
            working = copy.deepcopy(dict(profile))
            gold = _strict_uint(profile.get("gold", 0))
            if gold < gold_cost:
                raise PrivateEquipRequestError(ERROR_GOLD_NOT_ENOUGH, "insufficient gold")
            raw_inventory = profile.get("inventory", {})
            if not isinstance(raw_inventory, Mapping):
                raise PrivateEquipRequestError(ERROR_MESSAGE_DATA_ILLEGAL, "invalid inventory")
            for item, cost in costs.items():
                item_row = raw_inventory.get(str(item))
                count = 0 if item_row is None else _strict_uint(item_row.get("count"), UINT64_MAX)
                if count < cost:
                    raise PrivateEquipRequestError(ERROR_ITEM_COUNT_NOT_ENOUGH, "insufficient equipment material")
            history = profile.get("resource_history", [])
            if not isinstance(history, list):
                raise PrivateEquipRequestError(ERROR_MESSAGE_DATA_ILLEGAL, "invalid resource history")
            notifications = []
            for item, cost in sorted(costs.items()):
                item_row = raw_inventory[str(item)]
                updated = update_inventory_item(working, item, int(item_row["count"]) - cost,
                                                uid=item_row.get("uid"), para=item_row.get("para", 0))
                # Keep a zero-count absolute notification: several runtime UI
                # counters hold onto the material instance until refreshed.
                notifications.append(make_update_item_packet(int(profile.get("role_id", 0)), updated, self.codec))
            changes = {"inventory": working["inventory"]} if costs else {}
            if gold_cost:
                changes["gold"] = gold - gold_cost
                notifications.insert(0, (25, self.codec.encode("Notify_UpdateRoleProp", {
                    "RoleId": int(profile.get("role_id", 0)), "PropType": 2, "PropValue": gold - gold_cost})))
            row[field_name] = previous + 1
            rows = copy.deepcopy(source)
            rows[next(i for i, r in enumerate(source) if r is matches[0])] = row
            changes["private_equipment"] = rows
            working.update(changes)
            gospel_rows = copy.deepcopy(profile["gospels"])
            for gospel in gospel_rows:
                if gospel["id"] == gid:
                    gospel["combat_power"] = self.combat_resolver.resolve_gospel(gospel, working).combat_power
            changes["gospels"] = gospel_rows
            ledger = copy.deepcopy(history)
            paid = ([{"type": 2, "id": 0, "num": gold_cost}] if gold_cost else [])
            paid.extend({"type": 4, "id": item, "num": cost} for item, cost in sorted(costs.items()))
            ledger.append({"source": "private_equipment", "command_id": request_id,
                           "operation": field_name, "pequip_uid": uid, "pequip_id": gid,
                           "from": previous, "to": previous + 1, "costs": paid,
                           "rewards": [], "applied_rewards": []})
            changes["resource_history"] = ledger
            return PrivateEquipResult(RESPONSE_IDS[request_id], self.codec.encode(
                message, {"Errorcode": 0, "PequipInfo": self._message(row)}),
                changes=changes, notifications=tuple(notifications))
        except PrivateEquipRequestError as exc:
            return PrivateEquipResult(RESPONSE_IDS[request_id], self.codec.encode(
                message, {"Errorcode": exc.code}), error_code=exc.code, reason=str(exc))
        except (ProtobufDecodeError, InventoryContentError, TypeError, ValueError, AttributeError) as exc:
            return self._wire_error(request_id, str(exc))

    def handle(
        self,
        request_id: int,
        profile: Optional[Mapping[str, Any]],
        request_body: bytes = b"",
    ) -> Optional[PrivateEquipResult]:
        if request_id not in RESPONSE_IDS:
            return None
        account = profile if isinstance(profile, Mapping) else {}
        if request_id in (CS_PRIVATE_EQUIP_LEVEL_UP, CS_PRIVATE_EQUIP_AWAKE):
            return self._grow(request_id, account, request_body)
        if request_id == CS_TAKE_PRIVATE_EQUIP_STAGE_REWARD:
            return self._claim_task(account, request_body)
        rows = self._seeded_rows(account)
        try:
            if request_id == CS_GET_PRIVATE_EQUIP_DATAS:
                tuple(iter_wire_fields(bytes(request_body)))
                body = self.codec.encode(
                    "SC_GetPrivateEquipDatasRet",
                    {
                        "Errorcode": ERROR_SUCCESS,
                        "EquipList": [self._message(row) for row in rows],
                    },
                )
                return PrivateEquipResult(SC_GET_PRIVATE_EQUIP_DATAS_RET, body)

            if request_id == CS_REQUEST_PRIVATE_EQUIP_STAGE_TASK:
                tuple(iter_wire_fields(bytes(request_body)))
                body = self.codec.encode(
                    "SC_RequestPEquipStageTaskDataRet",
                    self.task_snapshot(account),
                )
                return PrivateEquipResult(
                    SC_REQUEST_PRIVATE_EQUIP_STAGE_TASK_RET, body,
                    notifications=(self.task_achievement_notification(account),),
                )

            if request_id == CS_EXCHANGE_PRIVATE_EQUIP_TICKET:
                material_count = _decode_uint(request_body, 1, UINT32_MAX)
                exchange = self.catalog.exchange
                if material_count <= 0:
                    return self._wire_error(request_id, "material count must be positive")
                if material_count % exchange.material_per_exchange != 0:
                    return self._wire_error(
                        request_id,
                        "material count is not divisible by the catalog exchange cost",
                    )
                working = copy.deepcopy(dict(account))
                inventory = normalize_inventory(working.get("inventory", {}))
                material = inventory.get(str(exchange.material_item_id))
                ticket = inventory.get(str(exchange.ticket_item_id))
                material_before = int(material["count"]) if material else 0
                ticket_before = int(ticket["count"]) if ticket else 0
                if material_count > material_before:
                    return PrivateEquipResult(
                        SC_EXCHANGE_PRIVATE_EQUIP_TICKET_RET,
                        self.codec.encode(
                            "SC_ExchangePEquipTicketRet",
                            {"Errorcode": ERROR_ITEM_COUNT_NOT_ENOUGH},
                        ),
                        error_code=ERROR_ITEM_COUNT_NOT_ENOUGH,
                        reason="private-equipment ticket material is insufficient",
                    )
                ticket_gain = (
                    material_count
                    // exchange.material_per_exchange
                    * exchange.tickets_per_exchange
                )
                if ticket_before + ticket_gain > exchange.ticket_limit:
                    return PrivateEquipResult(
                        SC_EXCHANGE_PRIVATE_EQUIP_TICKET_RET,
                        self.codec.encode(
                            "SC_ExchangePEquipTicketRet",
                            {"Errorcode": ERROR_EXCEED_ITEM_LIMIT_COUNT},
                        ),
                        error_code=ERROR_EXCEED_ITEM_LIMIT_COUNT,
                        reason="private-equipment ticket storage limit would be exceeded",
                    )

                material_after = update_inventory_item(
                    working,
                    exchange.material_item_id,
                    material_before - material_count,
                    uid=(material or {}).get(
                        "uid", stable_item_uid(exchange.material_item_id)
                    ),
                )
                ticket_after = update_inventory_item(
                    working,
                    exchange.ticket_item_id,
                    ticket_before + ticket_gain,
                    uid=(ticket or {}).get(
                        "uid", stable_item_uid(exchange.ticket_item_id)
                    ),
                )
                role_id = _safe_uint(account.get("role_id"), 0, UINT64_MAX)
                material_packet = (
                    make_remove_item_packet(role_id, material_after, self.codec)
                    if material_after["count"] == 0
                    else make_update_item_packet(role_id, material_after, self.codec)
                )
                ticket_packet = make_update_item_packet(
                    role_id, ticket_after, self.codec
                )
                return PrivateEquipResult(
                    SC_EXCHANGE_PRIVATE_EQUIP_TICKET_RET,
                    self.codec.encode(
                        "SC_ExchangePEquipTicketRet", {"Errorcode": ERROR_SUCCESS}
                    ),
                    changes={"inventory": working["inventory"]},
                    notifications=(material_packet, ticket_packet),
                )

            uid = _decode_uint(request_body, 1, UINT64_MAX)
            row = self._find_by_uid(rows, uid)
            if row is None:
                return self._wire_error(request_id, f"unknown private equip UID {uid}")
            definition = self.catalog.definitions[int(row["pequip_id"])]

            if request_id == CS_SET_PRIVATE_EQUIP_APPEARANCE:
                row["appearance_flag"] = _decode_uint(request_body, 2, UINT32_MAX)
                body = self.codec.encode(
                    "SC_SetPEquipAppearanceRet",
                    {
                        "Errorcode": ERROR_SUCCESS,
                        "PequipUid": uid,
                        "AppearanceFlag": row["appearance_flag"],
                    },
                )
            else:
                row["memory_flag"] = 1
                body = self.codec.encode(
                    "SC_LookupPEquipMemoryRet",
                    {"Errorcode": ERROR_SUCCESS, "PequipUid": uid},
                )
            return PrivateEquipResult(
                RESPONSE_IDS[request_id],
                body,
                changes={"private_equipment": rows},
            )
        except (
            InventoryContentError,
            ProtobufDecodeError,
            TypeError,
            ValueError,
        ) as exc:
            return self._wire_error(request_id, str(exc))


Layer = PrivateEquipContentLayer
Result = PrivateEquipResult


__all__ = [
    "CS_GET_PRIVATE_EQUIP_DATAS",
    "CS_EXCHANGE_PRIVATE_EQUIP_TICKET",
    "CS_LOOKUP_PRIVATE_EQUIP_MEMORY",
    "CS_PRIVATE_EQUIP_AWAKE",
    "CS_PRIVATE_EQUIP_LEVEL_UP",
    "CS_REQUEST_PRIVATE_EQUIP_STAGE_TASK",
    "CS_SET_PRIVATE_EQUIP_APPEARANCE",
    "ERROR_MESSAGE_DATA_ILLEGAL",
    "ERROR_EXCEED_ITEM_LIMIT_COUNT",
    "ERROR_ITEM_COUNT_NOT_ENOUGH",
    "ERROR_SUCCESS",
    "Layer",
    "PRIVATE_EQUIP_REVISION",
    "PRIVATE_EQUIP_UID_BASE",
    "PrivateEquipCatalog",
    "PrivateEquipCatalogError",
    "PrivateEquipContentLayer",
    "PrivateEquipDefinition",
    "PrivateEquipExchangeDefinition",
    "PrivateEquipResult",
    "PRIVATE_EQUIP_TICKET_ITEM_ID",
    "PRIVATE_EQUIP_TICKET_MATERIAL_ID",
    "RESPONSE_IDS",
    "Result",
]
