"""Atomic use of defined bag items, through the source CS_UseItem UI.

Fixed rewards and selection lists come from the pinned runtime tables. Random
packages use a versioned offline draw rule: independent group-zero chances;
one weighted draw per positive group, with unallocated probability retained.
The saved sequence makes a failed transaction/reload reproduce the same roll.
This is not a claim to have defined the previous server's RNG implementation.
"""
from __future__ import annotations

import copy
import hashlib
import json
import random
import re
from collections import defaultdict

try:
    from . import shop_content as shop
    from .inventory_content import normalize_inventory, InventoryContentError
    from .content_profile import ContentCatalog
    from .energy_content import EnergyRecoveryLayer
except ImportError:  # Embedded direct-module imports.
    import shop_content as shop
    from inventory_content import normalize_inventory, InventoryContentError
    from content_profile import ContentCatalog
    from energy_content import EnergyRecoveryLayer


STATE_KEY = "item_use_state"
FOOD = 11
RUNE = 25
CURRENCY_ITEMS = {1000001: 0, 1000002: 1, 1000003: 2, 1000004: 3}


def fail(reason, code=shop.ERROR_CLIENT_REQUEST_PARAM):
    raise shop.ShopApplicationError(code, reason)


class ItemUseLayer:
    def __init__(self, host, *, catalog=None, battle=None, life=None):
        self.host = host
        self.catalog = catalog or ContentCatalog.load(host.catalog.content_dir)
        self.energy = EnergyRecoveryLayer(self.catalog)
        self.optional = {int(r["id"]): r for r in self.catalog.table("a_optional_package").records}
        self.rune_packages = {int(r["id"]): r for r in self.catalog.table("a_rune_package").records}
        self.drops = defaultdict(list)
        for row in self.catalog.table("a_drop_item").records:
            self.drops[int(row["drop_id"])].append(row)
        self.potions = set(json.loads(host.catalog.globals[9990007]["string_value"]))
        self.potion_energy_limit = int(host.catalog.globals[9990999]["int_value"])
        self.select_limit = int(host.catalog.globals[9990264]["int_value"])
        self.battle = battle
        self.life = life
        self.skin_projector = None

    def handle(self, command_id, profile, body):
        return self.rename(profile, body) if command_id == 10 else None

    def rename(self, profile, body):
        """First nickname is free; subsequent changes spend one rename card.

        The server-owned flag decides the cost. IsInPlot is merely the
        source UI's hint and cannot be used to regain a free change.
        """
        role_id = int(profile.get("role_id", 0))
        name = ""
        try:
            values = {}
            for number, wire, value in shop.iter_wire_fields(body):
                if number not in (1, 2, 3) or number in values or wire != (2 if number == 2 else 0):
                    fail("malformed nickname request", shop.ERROR_MESSAGE_DATA_ILLEGAL)
                values[number] = value
            if values.get(1) != role_id or values.get(3, 0) not in (0, 1):
                fail("nickname request role or mode is invalid")
            name = values.get(2, b"").decode("utf-8")
            # StringExtend.IsHaveSymbol and GetStringLen in the pinned runtime.
            width = sum(2 if "\u4e00" <= ch <= "\u9fa5" else 1 for ch in name)
            if not re.fullmatch(r"[0-9A-Za-z\u4e00-\u9fff\u3040-\u30ff]+", name) or width > 16:
                fail("nickname contains invalid characters or exceeds the client length")
            if name == profile.get("role_name"):
                fail("nickname is unchanged")
            changed = shop._state_uint(profile.get("has_changed_name", 0), "nickname flag", maximum=1)
            changes = {"role_name": name, "has_changed_name": 1}
            notices = ()
            costs = []
            if changed:
                inventory = normalize_inventory(copy.deepcopy(profile.get("inventory", {})))
                card = inventory.get("1001013")
                if card is None or card["count"] < 1:
                    fail("rename card is not owned")
                card["count"] -= 1
                changes["inventory"] = inventory
                notices = (self.host._absolute_item_notice(profile, changes, (1001013,)),)
                costs = [shop.Reward(4, 1001013, 1).as_dict()]
            history = profile.get("resource_history", [])
            if not isinstance(history, list):
                fail("resource history is not a list", shop.ERROR_CONFIG)
            changes["resource_history"] = [*copy.deepcopy(history), {
                "source": "nickname_change", "command_id": 10, "costs": costs, "rewards": []}]
            return shop.Result(11, self.host.codec.encode("SC_ChangeNameRet", {
                "Errorcode": 0, "RoleId": role_id, "RoleName": name}), changes=changes, notifications=notices)
        except (shop.ShopApplicationError, ValueError, TypeError, AttributeError, OverflowError) as exc:
            code = exc.error_code if isinstance(exc, shop.ShopApplicationError) else shop.ERROR_MESSAGE_DATA_ILLEGAL
            return shop.Result(11, self.host.codec.encode("SC_ChangeNameRet", {
                "Errorcode": code, "RoleId": role_id}), error_code=code, reason=str(exc))

    def _battle(self):
        if self.battle is None:
            try:
                from .battle_settlement import BattleSettlementLayer
            except ImportError:
                from battle_settlement import BattleSettlementLayer
            self.battle = BattleSettlementLayer(self.catalog, normal_economy=True)
        return self.battle

    def _life(self):
        if self.life is None:
            try:
                from .life_content import LifeContentLayer
            except ImportError:
                from life_content import LifeContentLayer
            self.life = LifeContentLayer(self.catalog)
        return self.life

    def _rewards(self, raw, context):
        rewards = shop._parse_rewards(raw, context)
        # Wage tables explicitly spell currency rewards as Item + currency ID.
        return tuple(shop.Reward(CURRENCY_ITEMS[r.item_id], 0, r.count)
                     if r.res_type == 4 and r.item_id in CURRENCY_ITEMS else r for r in rewards)

    def _expand(self, rewards, ancestors=()):
        result = []
        for reward in rewards:
            row = self.host.catalog.items.get(reward.item_id) if reward.res_type == 4 else None
            if row is not None and int(row["max_stack_num"]) == 0:
                if (row["type"] != 2 or not row["is_use"] or reward.item_id in ancestors
                        or len(ancestors) >= 16):
                    fail("unproved or cyclic non-storable container", shop.ERROR_CONFIG)
                contents = self._rewards(row["add_num"], "nested fixed package")
                if not contents:
                    fail("empty fixed package", shop.ERROR_CONFIG)
                result.extend(self._expand(tuple(r.scaled(reward.count) for r in contents),
                                           (*ancestors, reward.item_id)))
            else:
                result.append(reward)
        return tuple(result)

    def _random_rewards(self, raw_groups, count, rng):
        refs = json.loads(raw_groups or "[]")
        if not isinstance(refs, list) or len(refs) > 20:
            fail("invalid package drop references", shop.ERROR_CONFIG)
        result = []
        for ref in refs:
            rows = self.drops.get(int(ref["drop_id"]), ())
            if not rows or any(r["time_limit"] or r["drop_time"] != 0 for r in rows):
                fail("missing or time-conditioned package drops", shop.ERROR_CONFIG)
            groups = defaultdict(list)
            for row in sorted(rows, key=lambda r: r["id"]):
                if type(row["ratio"]) is not int or row["ratio"] < 0 or row["drop_group"] < 0:
                    fail("invalid package drop weight", shop.ERROR_CONFIG)
                groups[row["drop_group"]].append(row)
            for _ in range(count):
                for group, entries in sorted(groups.items()):
                    selected = []
                    if group == 0:
                        for entry in entries:
                            if entry["ratio"] > 10000:
                                fail("independent drop ratio exceeds 10000", shop.ERROR_CONFIG)
                            if rng.randrange(10000) < entry["ratio"]:
                                selected.append(entry)
                    else:
                        roll = rng.randrange(max(10000, sum(e["ratio"] for e in entries)))
                        for entry in entries:
                            roll -= entry["ratio"]
                            if roll < 0:
                                selected.append(entry)
                                break
                    for entry in selected:
                        result.extend(self._rewards(entry["drop_content"], "a_drop_item"))
        return tuple(result)

    def _runes(self, profile, item, request, rng):
        definition = self.rune_packages.get(item["correlation"])
        if not definition or definition["suit"] != 1:
            fail("unproved rune package", shop.ERROR_CONFIG)
        suits = json.loads(definition["suit_id"])
        positions = json.loads(definition["position_list"] or "[]")
        if request.optional_suit_item_id not in suits:
            fail("selected rune suit is not an option")
        if definition["position"] == 1:
            if request.optional_item_id not in positions:
                fail("selected rune position is not an option")
        elif definition["position"] != 2 or request.optional_item_id != 0:
            fail("random-position rune package does not accept a position")
        battle = self._battle()
        candidates = [rid for rid, row in battle.rune_static.items()
                      if row["suit"] == request.optional_suit_item_id
                      and row["quality"] == definition["quality"]
                      and (not request.optional_item_id or row["position"] == request.optional_item_id)]
        if not candidates:
            fail("rune package has no concrete rune", shop.ERROR_CONFIG)
        try:
            from .battle_settlement import Reward as RuneReward
        except ImportError:
            from battle_settlement import Reward as RuneReward
        rolls = tuple(RuneReward(RUNE, rng.choice(sorted(candidates)), 1) for _ in range(request.count))
        # This helper receives ONLY resolved rune rewards; item/currency grants
        # use the strict, non-clamping path below.
        changes = battle._grant_changes(profile, rolls)
        old = {r["rune_item_uid"] for r in profile.get("rune_items", [])}
        uids = [r["rune_item_uid"] for r in changes["rune_items"] if r["rune_item_uid"] not in old]
        if len(uids) != request.count:
            fail("rune grant did not create the requested instances", shop.ERROR_CONFIG)
        return changes, tuple(shop.Reward(RUNE, r.item_id, 1) for r in rolls), uids

    def _foods(self, profile, rewards):
        life = self._life()
        state = life.make_state(profile)
        rows = []
        trails = []
        for reward in rewards:
            dish = life.catalog.dishes.get(reward.item_id)
            if dish is None or not dish.fixed_props:
                fail("package food has no fixed client attributes", shop.ERROR_CONFIG)
            # Source ObjectTrail refers to individual food instances.
            if reward.count > 1000:
                fail("too many food instances", shop.ERROR_EXCEED_ITEM_LIMIT_COUNT)
            for _ in range(reward.count):
                uid = state["next_food_uid"]
                if uid >= shop.UINT64_MAX:
                    fail("food UID namespace exhausted", shop.ERROR_EXCEED_ITEM_LIMIT_COUNT)
                state["next_food_uid"] = uid + 1
                food = life._base_food(dish)
                food.update(uid=uid, created_at=int(self.host._now()))
                state["food_inventory"][str(uid)] = food
                rows.append(life._food_wire(food))
                trails.append({"ResType": FOOD, "Uid": uid})
        notice = (278, self.host.codec.encode("Notify_UpdateFood", {"FoodList": rows}))
        return {"life_content": state}, (notice,), trails

    def _skins(self, profile, rewards):
        if not rewards:
            return {}
        if self.skin_projector is None:
            try:
                from .content_responses import ContentResponseLayer
            except ImportError:
                from content_responses import ContentResponseLayer
            self.skin_projector = ContentResponseLayer(self.host.catalog.content_dir)
        # Use the SAME ownership resolution as login/status/equipment. Full
        # catalog baselines and explicit override maps outrank a missing row.
        owned = self.skin_projector._owned_skin_ids(profile)
        skins = copy.deepcopy(profile.get("gospel_skins", {}))
        content = copy.deepcopy(profile.get("content_state", {}))
        if not isinstance(skins, dict) or not isinstance(content, dict):
            fail("skin state is malformed", shop.ERROR_CONFIG)
        overrides = content.setdefault("overrides", {})
        if not isinstance(overrides, dict):
            fail("skin override state is malformed", shop.ERROR_CONFIG)
        skin_overrides = overrides.setdefault("a_gospel_skin", {})
        if not isinstance(skin_overrides, dict):
            fail("skin override state is malformed", shop.ERROR_CONFIG)
        for reward in rewards:
            if reward.count != 1 or reward.item_id in owned:
                fail("skin is already owned or quantity exceeds one", shop.ERROR_EXCEED_ITEM_LIMIT_COUNT)
            owned.add(reward.item_id)
            key = str(reward.item_id)
            skins[key] = {"owned": 1, "unlocked": 1}
            row = skin_overrides.setdefault(key, {})
            if not isinstance(row, dict):
                row = {}
                skin_overrides[key] = row
            row.update(owned=1, unlocked=1)
        return {"gospel_skins": skins, "content_state": content}

    def use(self, profile, body):
        try:
            request = shop._parse_use_item(body)
        except shop.ProtobufDecodeError as exc:
            return self.host._use_item_failure(shop._UseItemRequest(0, 0, 0, 0),
                                              shop.ERROR_MESSAGE_DATA_ILLEGAL, str(exc))
        item_id = 0
        try:
            if request.item_uid <= 0 or request.count <= 0:
                fail("positive item UID and count required")
            inventory = normalize_inventory(copy.deepcopy(profile.get("inventory", {})))
            found = [(k, r) for k, r in inventory.items() if r["uid"] == request.item_uid]
            if len(found) != 1:
                fail("item UID is not uniquely owned")
            key, owned = found[0]
            item_id = int(key)
            item = self.host.catalog.items.get(item_id)
            if item is None or item["is_use"] != 1 or item["max_stack_num"] <= 0:
                fail("item is not a usable stored item")
            maximum = self.select_limit if item["type"] in (3, 11, 12) else max(1, int(item["simultaneous"]))
            if item_id in self.potions:
                fixed = self._rewards(item["add_num"], "energy potion")
                if len(fixed) != 1 or fixed[0].res_type != 3 or fixed[0].item_id != 0:
                    fail("potion has an unproved energy grant", shop.ERROR_CONFIG)
                energy = shop._state_uint(profile.get("energy", 0), "energy")
                # The live backpack opens BuyTiLiPromptPop, whose ceiling is
                # a_global[9990999].int_value (9999), not natural recovery.
                maximum = max(0, (self.potion_energy_limit - energy) // fixed[0].count)
            if request.count > min(owned["count"], maximum):
                fail("requested count exceeds inventory or the client use limit")
            if item["type"] not in (11, 12) and (request.optional_item_id or request.optional_suit_item_id):
                fail("this item does not accept a selection")
            state = copy.deepcopy(profile.get(STATE_KEY, {"version": 1, "sequence": 0}))
            if not isinstance(state, dict) or state.get("version") != 1:
                fail("unsupported item-use state", shop.ERROR_CONFIG)
            sequence = shop._state_uint(state.get("sequence"), "item use sequence", maximum=shop.UINT64_MAX)
            if sequence == shop.UINT64_MAX:
                fail("item use sequence exhausted", shop.ERROR_CONFIG)
            seed = f"mte-item-use-v1:{profile.get('role_id', 0)}:{sequence}:{request.item_uid}"
            rng = random.Random(int.from_bytes(hashlib.sha256(seed.encode()).digest(), "big"))
            configured = ()
            changes = {}
            notifications = ()
            trails, rune_uids = [], []
            if item["type"] == 12:
                changes, configured, rune_uids = self._runes(profile, item, request, rng)
            elif item["type"] == 11 and item["correlation"] and not item["random_group"]:
                definition = self.optional.get(item["correlation"])
                if not definition or request.optional_suit_item_id:
                    fail("missing optional package or invalid suit", shop.ERROR_CONFIG)
                choices = self._rewards(definition["item_id"], "a_optional_package")
                chosen = [r for r in choices if r.item_id == request.optional_item_id]
                if len(chosen) != 1:
                    fail("selected reward is not an option for this package")
                configured = (chosen[0].scaled(request.count),)
            elif item["type"] == 5:
                configured = (shop.Reward(shop.RESOURCE_GOSPEL_SKIN, item["correlation"], request.count),)
            elif item["type"] in (1, 2, 3, 11):
                if item["type"] == 11 and (request.optional_item_id or request.optional_suit_item_id):
                    fail("random package does not accept a selection")
                fixed = self._rewards(item["add_num"], "a_item.add_num")
                if not fixed and not item["random_group"]:
                    fail("item requires a dedicated effect or UI; no grant is proved", shop.ERROR_CONFIG)
                configured = tuple(r.scaled(request.count) for r in fixed)
                configured += self._random_rewards(item["random_group"], request.count, rng)
            else:
                fail("item type has no configured use rule", shop.ERROR_CONFIG)
            configured = self._expand(configured)
            ordinary = tuple(r for r in configured if r.res_type not in (FOOD, RUNE))
            self.host.catalog._validate_rewards(ordinary, "item use")
            skins = tuple(r for r in ordinary if r.res_type == 28)
            skin_changes = self._skins(profile, skins)
            inventory[key]["count"] -= request.count
            staged = {**profile, **changes, "inventory": inventory}
            reward_changes, actual, _ = self.host._reward_changes(staged,
                tuple(r for r in ordinary if r.res_type != 28), reject_saturated=True)
            actual = (*actual, *skins)
            changes = {**changes, "inventory": inventory, **reward_changes, **skin_changes}
            # New gospels from packages enter the same level-one growth path.
            before_gospel_ids = {r["id"] for r in profile.get("gospels", [])}
            for row in changes.get("gospels", []):
                if row["id"] not in before_gospel_ids:
                    row.update(level=1, fake_level=1, exp=0)
                    trails.append({"ResType": 7, "Uid": row["uid"]})
            foods = tuple(r for r in configured if r.res_type == FOOD)
            if foods:
                food_changes, notifications, food_trails = self._foods(staged, foods)
                changes.update(food_changes)
                trails.extend(food_trails)
            for reward in ordinary:
                if reward.res_type == shop.RESOURCE_GOSPEL_SKIN:
                    gospel_id = self.host.catalog.gospel_skins[reward.item_id]["gospel_id"]
                    gospel = next((r for r in profile.get("gospels", []) if r["id"] == gospel_id), None)
                    if gospel:
                        notifications += ((1108, self.host.codec.encode("Notify_GospelSkinUnlocked", {
                            "GospelUId": gospel["uid"], "SkinList": [reward.item_id]})),)
            grants = (*actual, *foods, *(r for r in configured if r.res_type == RUNE))
            state["sequence"] = sequence + 1
            changes[STATE_KEY] = state
            history = profile.get("resource_history", [])
            if not isinstance(history, list):
                fail("resource history is not a list", shop.ERROR_CONFIG)
            changes["resource_history"] = [*copy.deepcopy(history), {
                "source": "item_use", "command_id": 63, "sequence": sequence + 1,
                "item_uid": request.item_uid, "item_id": item_id, "count": request.count,
                "optional_item_id": request.optional_item_id, "optional_suit_item_id": request.optional_suit_item_id,
                "costs": [shop.Reward(4, item_id, request.count).as_dict()],
                "rewards": [r.as_dict() for r in grants], "rune_uids": rune_uids}]
            # UseItemRes reads newly notified entity caches synchronously.
            notice = self.host._absolute_item_notice(profile, changes,
                (item_id, *(r.item_id for r in actual if r.res_type == 4)))
            if notice:
                notifications += (notice,)
            values = {"Errorcode": 0, "ItemUId": request.item_uid, "ItemId": item_id, "Num": request.count,
                      # The skin-specific source callback opens its own
                      # reward popup using the consumed skin item identity.
                      "GeneratedItemList": self.host._reward_values((*tuple(r for r in actual if r.res_type != 28), *foods))}
            if request.optional_item_id:
                values["OptionalItemId"] = request.optional_item_id
            if request.optional_suit_item_id:
                values["OptionalSuitItemId"] = request.optional_suit_item_id
            if trails:
                values["ObjTrailList"] = trails
            if rune_uids:
                values["RuneItemUIdList"] = rune_uids
            return shop.Result(64, self.host.codec.encode("SC_UseItemRet", values), changes=changes,
                               rewards=configured, grants=tuple(grants), notifications=notifications)
        except (shop.ShopApplicationError, shop.ShopConfigError, InventoryContentError,
                ValueError, KeyError, TypeError, OverflowError) as exc:
            code = exc.error_code if isinstance(exc, shop.ShopApplicationError) else shop.ERROR_CONFIG
            return self.host._use_item_failure(request, code, str(exc), item_id=item_id)
