"""Real resource costs and the optional, same-process material supply panel.

Content visibility is independent of balances. Supply receipts describe grants,
never purchases or task consumption. Callers commit these pure changes through
the account store before publishing absolute runtime-cache notifications.
"""
from __future__ import annotations

import copy
import re
from datetime import datetime, timedelta, timezone
from collections.abc import Mapping

try:
    from .inventory_content import item_exclusion_reason, normalize_inventory, stable_item_uid
except ImportError:  # Android imports production modules at top level.
    from inventory_content import item_exclusion_reason, normalize_inventory, stable_item_uid


UINT32_MAX = (1 << 32) - 1
NORMAL_ECONOMY = "normal-v1"
LEGACY_ECONOMY = "legacy-free"
SCALAR_RESOURCES = {0: ("diamond", 1000001, 106), 1: ("minidiamond", 1000002, 107),
                    2: ("gold", 1000003, 108), 3: ("energy", 1000004, 109)}


def economy_day(now):
    # BuyTicketPop explicitly says daily 04:00; the defined Chinese runtime
    # uses server time, so device timezone changes must not reset this clock.
    return (datetime.fromtimestamp(now, timezone(timedelta(hours=8))) - timedelta(hours=4)).date()


def synchronize_tickets(profile, catalog, now):
    """Accumulate the source daily 5 tickets up to the source cap 15."""
    daily, maximum = (int(x) for x in catalog.record("a_global", 9991131)["string_value"].split(","))
    current_day = economy_day(now).isoformat()
    state = copy.deepcopy(profile.get("offline_economy", {}))
    previous = state.get("ticket_day")
    if previous and current_day <= previous:
        return {}
    changes = {}
    inventory = normalize_inventory(profile.get("inventory", {}))
    row = inventory.get("1001049")
    current = row["count"] if row else 0
    elapsed = (datetime.fromisoformat(current_day) - datetime.fromisoformat(previous)).days if previous else 0
    added = min(max(0, maximum - current), elapsed * daily)
    if added:
        row = inventory.setdefault("1001049", {"id": 1001049, "uid": stable_item_uid(1001049), "count": 0, "para": 0})
        row["count"] += added
        reward = {"type": 4, "id": 1001049, "num": added}
        changes.update(inventory=inventory, resource_history=[*copy.deepcopy(profile.get("resource_history", [])), {
            "source": "daily_ticket_recovery", "day": current_day,
            "costs": [], "rewards": [reward], "applied_rewards": [reward]}])
    state.update(mode=NORMAL_ECONOMY, ticket_day=current_day)
    changes["offline_economy"] = state
    return changes


def initialize_normal_profile(profile, catalog, gospel_progression, hero_progression):
    """Finite offline starting kit, applied ONLY to a newly created account.

    These are openly local starter choices, not server rewards.
    Preserve content/character visibility; reset only supported growth paths.
    Witch base progression has undefined costs and keeps the playable seed.
    """
    profile.update(gold=50_000, diamond=0, minidiamond=1_000, energy=160,
                   resource_balances={"1:0": 1000},
                   inventory={}, rune_items=[], hero_runes=[],
                   offline_economy={"mode": NORMAL_ECONOMY, "origin": "finite-starter-v1"})
    # Enough for the first few real upgrades/draws; never a login refill.
    starting_items = {1001049: 5, 1006505: 10, 1006223: 10, 1001034: 10,
                      1006250: 10, 1006253: 1}
    items = {int(r["id"]): r for r in catalog.table("a_item").records}
    for item_id, count in starting_items.items():
        if item_id not in items or int(items[item_id]["max_stack_num"]) < count:
            raise EconomyError("初始材料不在已验证的目录中。", 32772)
        profile["inventory"][str(item_id)] = {"id": item_id, "uid": stable_item_uid(item_id), "count": count, "para": 0}
    for row in profile.get("gospels", []):
        definition = gospel_progression.catalog.definitions.get(int(row["id"]))
        if definition is None:
            continue
        row.update(level=1, exp=int(definition.states[1]["exp"]), level_limit=30,
                   fake_level=1, awake_level=1, fake_awake_level=1, star_up=0,
                   fondness=0, fondness_level=0, spirit_breakthrough_num=0,
                   spirit_level_limit=10, is_fondness_limit_breakthrough=0,
                   spirit_is_ready=0, spirit_status=0, break_level_reward_taken={},
                   rewarded_fondness_levels=[])
        for skill in row.get("active_skills", []):
            skill.update(level=1, valid_level=1)
    starting_slots = {}
    slot_gates = [r for r in catalog.table("a_job_skill").records if int(r.get("unlock_gospel", 0)) > 0]
    for row in profile.get("heroes", []):
        profession = hero_progression.catalog.professions[int(row["profession_id"])]
        if not profession.groups:
            continue  # No recoverable growth path: retain its playable seed.
        first = profession.groups[0]
        row.update(profession_level=1, profession_level_limit=first.level_cap,
                   profession_breakthrough_level=0,
                   profession_talent_ids=[first.talents[0].talent_id],
                   profession_unlocked_tree_ids=[first.group_id],
                   profession_talent_tree_id=first.group_id,
                   rewarded_profession_talent_tree_ids=[])
        # TeamGospelItem checks the source a_job_skill.unlock_gospel gate.
        # A former max-level seed must not leave a hidden third star equipped
        # while the new profession is level 1 and that slot is still locked.
        groups = {group.group_id for group in profession.groups}
        locked = [int(gate["unlock_gospel"]) - 1 for gate in slot_gates
                  if int(gate["skillgroup_id"]) in groups and int(gate["id"]) not in row["profession_talent_ids"]]
        starting_slots[str(row["uid"])] = min(locked, default=3)
    for formations in (profile.get("phalanxes", []), profile.get("formation_state", {}).get("phalanxes", [])):
        for formation in formations:
            for uid, equipped in formation.get("equipped_gospels", {}).items():
                formation["equipped_gospels"][uid] = equipped[:starting_slots.get(str(uid), 3)]
    for row in profile.get("private_equipment", []):
        row.update(level=1, awake_level=0)
    # Runes will come from gameplay. No preset may point at removed instances.
    by_type = profile.get("presets_v2", {}).get("by_type", {})
    for group in by_type.get("2", {}).values():
        for slot in group.get("slots", []):
            slot["uid_list"] = []


_GROWTH_FIELDS = {
    "gospels": ("id", ("level", "exp", "level_limit", "fake_level", "awake_level", "fake_awake_level",
                         "active_skills", "fondness", "fondness_level", "fondness_daily_increment",
                         "is_fondness_limit_breakthrough", "spirit_breakthrough_num", "spirit_is_ready",
                         "spirit_level_limit", "spirit_status", "star_up", "break_level_reward_taken",
                         "rewarded_fondness_levels")),
    "heroes": ("id", ("level", "exp", "level_limit", "fake_level", "awakening_level", "breakthrough_level",
                        "active_skills", "profession_level", "profession_level_limit", "profession_breakthrough_level",
                        "profession_talent_ids", "profession_unlocked_tree_ids", "profession_talent_tree_id",
                        "rewarded_profession_talent_tree_ids")),
    "private_equipment": ("pequip_id", ("level", "awake_level", "appearance_flag", "memory_flag")),
}


def capture_existing_economy(profile):
    """Prevent compatibility/catalog seeds from acting as a later refill."""
    saved = {key: copy.deepcopy(profile[key]) for key in
             ("gold", "diamond", "minidiamond", "energy", "level", "exp", "inventory", "resource_balances")
             if key in profile}
    for root, (identity, fields) in _GROWTH_FIELDS.items():
        saved[root] = {r[identity]: {k: copy.deepcopy(r[k]) for k in fields if k in r}
                       for r in profile.get(root, []) if isinstance(r, Mapping) and identity in r}
    return saved


def restore_existing_economy(profile, saved):
    for key, value in saved.items():
        if key not in _GROWTH_FIELDS:
            profile[key] = value
    for root, (identity, _) in _GROWTH_FIELDS.items():
        for row in profile.get(root, []):
            row.update(copy.deepcopy(saved[root].get(row[identity], {})))


class EconomyError(ValueError):
    def __init__(self, message, error_code=32000):
        super().__init__(message)
        self.error_code = error_code


def uint(value, label, *, minimum=0, maximum=UINT32_MAX):
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise EconomyError(f"{label}超出允许范围。")
    return value


def resource_key(kind, item_id):
    if kind in SCALAR_RESOURCES:
        if item_id not in (0, SCALAR_RESOURCES[kind][1]):
            raise EconomyError("货币配置中的道具编号不匹配。", 32772)
        return kind, 0
    if kind == 4 and item_id > 0:
        return kind, item_id
    raise EconomyError("尚不支持此资源的扣除规则。", 32772)


def deduct_resources(profile, costs):
    """Deduct (ResourceType, ItemId, Count) rows without touching the input.

    Currency aliases share one balance; all costs are aggregated and checked
    before returning any patch. Zero-count bag rows retain their stable UID so
    command 26 can also notify a final consumption to zero.
    """
    totals = {}
    for kind, item_id, count in costs:
        key = resource_key(uint(kind, "资源类型"), uint(item_id, "道具编号"))
        totals[key] = uint(totals.get(key, 0) + uint(count, "消耗数量", minimum=1), "总消耗")
    changes = {}
    inventory = None
    for (kind, item_id), count in totals.items():
        if kind in SCALAR_RESOURCES:
            name, _, code = SCALAR_RESOURCES[kind]
            current = uint(profile.get(name, 0), "持有资源")
            if current < count:
                raise EconomyError("持有资源不足。", code)
            changes[name] = current - count
        else:
            if inventory is None:
                inventory = normalize_inventory(profile.get("inventory", {}))
            row = inventory.get(str(item_id))
            if row is None or uint(row.get("count", 0), "持有材料") < count:
                raise EconomyError("持有材料不足。", 110)
            row["count"] -= count
    if inventory is not None:
        changes["inventory"] = inventory
    return changes


class SupplyCatalog:
    """Named, storable materials and canonical currencies, not arbitrary edits."""
    def __init__(self, catalog):
        self.resources = {}
        items = {int(r["id"]): r for r in catalog.table("a_item").records}
        for kind, (field, item_id, _) in SCALAR_RESOURCES.items():
            row = items[item_id]
            self.resources[f"{kind}:0"] = {
                "key": f"{kind}:0", "name": row["name"], "item_id": item_id,
                "kind": kind, "field": field, "category": "货币与体力",
                "maximum": min(UINT32_MAX, int(row["max_stack_num"])),
            }
        for item_id, row in items.items():
            maximum = int(row.get("max_stack_num", 0))
            if item_exclusion_reason(row) or maximum <= 1:
                continue
            self.resources[f"4:{item_id}"] = {
                "key": f"4:{item_id}", "name": str(row["name"]), "item_id": item_id,
                "kind": 4, "category": "材料与道具", "maximum": min(UINT32_MAX, maximum),
            }

    def balance(self, profile, resource):
        if resource["kind"] != 4:
            return uint(profile.get(resource["field"], 0), "持有资源")
        return uint(profile.get("inventory", {}).get(str(resource["item_id"]), {}).get("count", 0), "持有材料")

    def snapshot(self, profile):
        return [{"key": r["key"], "name": r["name"], "category": r["category"],
                 "item_id": r["item_id"], "count": self.balance(profile, r),
                 "maximum": r["maximum"]} for r in self.resources.values()]

    def grant(self, profile, *, key, count, request_id):
        if not isinstance(request_id, str) or not re.fullmatch(r"[a-zA-Z0-9-]{16,64}", request_id):
            raise EconomyError("领取凭据无效，请重新打开补给面板。")
        resource = self.resources.get(key) if isinstance(key, str) else None
        if resource is None:
            raise EconomyError("未找到可领取的材料。")
        uint(count, "每次领取数量", minimum=1, maximum=1_000_000)
        receipts = copy.deepcopy(profile.get("supply_receipts", {}))
        if not isinstance(receipts, dict):
            raise EconomyError("补给记录损坏，未发放材料。", 32772)
        previous = receipts.get(request_id)
        if previous is not None:
            if previous["key"] != key or previous["count"] != count:
                raise EconomyError("此领取凭据已经用于另一笔补给。")
            return {}, {**previous, "balance": self.balance(profile, resource), "replayed": True}
        current = self.balance(profile, resource)
        if current + count > resource["maximum"]:
            raise EconomyError("领取后将超过持有上限，请减少数量。", 429)
        if resource["kind"] == 4:
            inventory = normalize_inventory(profile.get("inventory", {}))
            item_id = resource["item_id"]
            row = inventory.setdefault(str(item_id), {
                "id": item_id, "uid": stable_item_uid(item_id), "count": 0, "para": 0})
            row["count"] = current + count
            changes = {"inventory": inventory}
        else:
            changes = {resource["field"]: current + count}
        receipt = {"key": key, "count": count, "name": resource["name"], "balance": current + count}
        receipts[request_id] = receipt
        reward = {"type": resource["kind"], "id": resource["item_id"] if resource["kind"] == 4 else 0, "num": count}
        changes.update(supply_receipts=receipts,
                       resource_history=[*copy.deepcopy(profile.get("resource_history", [])), {
                           "source": "offline_supply", "transaction_id": request_id,
                           "costs": [], "rewards": [reward], "applied_rewards": [reward]}],
                       reward_history=[*copy.deepcopy(profile.get("reward_history", [])), {
                           "source": "offline_supply", "claim_key": f"supply:{request_id}", "rewards": [reward]}])
        return changes, {**receipt, "replayed": False}
