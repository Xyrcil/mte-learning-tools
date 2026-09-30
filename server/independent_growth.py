"""Explicit offline growth v1, independent of the source profession tree.

Pure proposals only: the caller commits each returned change set under the
account store transaction and projects the result through the live connection.
Preparing an action is durable but does not pay. Committing pays and records
the result together. Acknowledging never repeats a paid action.
"""
from __future__ import annotations

import copy
import hashlib
import json
import random
import re
from collections.abc import Mapping

try:
    from .offline_economy import EconomyError, deduct_resources
except ImportError:
    from offline_economy import EconomyError, deduct_resources

KEY = "independent_growth"
VERSION = 1
HERO_IDS = frozenset(range(3000001, 3000007))
HERO_CAPS = (20, 30, 40, 60)
AFFIX_RANGES = {"atk": (1, 50), "hp": (10, 500), "def": (1, 10)}
AFFIX_NAMES = {"atk": "攻击", "hp": "生命", "def": "防御"}
MAX_RECEIPTS = 256
ATTRIBUTE_EXTENSION = 125
MAX_BONUS = 1_000_000


class GrowthError(EconomyError):
    pass


def integer(value, name, low=0, high=(1 << 32) - 1):
    if type(value) is not int or not low <= value <= high:
        raise GrowthError(f"{name}无效。", 32769)
    return value


def token(value, label="操作凭据"):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{32}", value):
        raise GrowthError(f"{label}无效。", 32769)
    return value


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def fingerprint(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def empty_state():
    return {"version": VERSION, "revision": 0, "reroll_counter": 0,
            "heroes": {}, "decorations": {}, "pending_candidate": None,
            "pending_intent": None, "receipts": {}}


def hero_state(root, uid):
    row = root["heroes"].get(str(uid), {"level": 1, "breakthrough": 0, "awakening": 0})
    if not isinstance(row, dict) or set(row) != {"level", "breakthrough", "awakening"}:
        raise GrowthError("基础培养存档格式无效。", 32769)
    tier = integer(row["breakthrough"], "基础突破", 0, 3)
    level = integer(row["level"], "基础等级", 1, HERO_CAPS[tier])
    awake = integer(row["awakening"], "基础觉醒", 0, 5)
    if (tier and level < HERO_CAPS[tier - 1]) or (awake and level != 60):
        raise GrowthError("基础培养阶段不一致。", 32769)
    return row


def validate_affix(value):
    if value is None:
        return
    if (not isinstance(value, dict) or set(value) != {"kind", "value"}
            or not isinstance(value["kind"], str) or value["kind"] not in AFFIX_RANGES):
        raise GrowthError("饰品词条存档无效。", 32769)
    low, high = AFFIX_RANGES[value["kind"]]
    integer(value["value"], "饰品词条", low, high)


def decoration_state(root, item_id):
    row = root["decorations"].get(str(item_id), {"level": 0, "studied": 0, "affix": None})
    if not isinstance(row, dict) or set(row) != {"level", "studied", "affix"}:
        raise GrowthError("饰品培养存档格式无效。", 32769)
    studied = integer(row["studied"], "研习阶段", 0, 1)
    level = integer(row["level"], "饰品等级", 0, 10 if studied else 5)
    if studied and level < 5:
        raise GrowthError("饰品研习阶段不一致。", 32769)
    validate_affix(row["affix"])
    if row["affix"] and level < 1:
        raise GrowthError("未培养饰品不能已有词条。", 32769)
    return row


def state_view(profile):
    value = profile.get(KEY)
    if value is None:
        return empty_state()
    if not isinstance(value, dict) or set(value) != set(empty_state()):
        raise GrowthError("离线养成存档格式无效。", 32769)
    if type(value["version"]) is not int or value["version"] != VERSION:
        raise GrowthError("离线养成规则版本不兼容，请使用对应新版。", 32772)
    integer(value["revision"], "培养记录版本")
    integer(value["reroll_counter"], "洗炼计数")
    for key, maximum, validator in (("heroes", 7, hero_state), ("decorations", 200, decoration_state)):
        rows = value[key]
        if not isinstance(rows, dict) or len(rows) > maximum:
            raise GrowthError("离线养成目标列表无效。", 32769)
        for ident in rows:
            if not isinstance(ident, str) or not ident.isascii() or not ident.isdigit() or str(int(ident)) != ident or int(ident) <= 0:
                raise GrowthError("离线养成目标编号无效。", 32769)
            validator(value, int(ident))
    pending = value["pending_candidate"]
    if pending is not None:
        if not isinstance(pending, dict) or set(pending) != {"id", "target", "old", "candidate"}:
            raise GrowthError("待定洗炼存档无效。", 32769)
        token(pending["id"], "候选编号")
        integer(pending["target"], "洗炼目标", 1)
        validate_affix(pending["old"])
        validate_affix(pending["candidate"])
        if pending["candidate"] is None or decoration_state(value, pending["target"])["level"] < 1:
            raise GrowthError("待定洗炼缺少已培养目标。", 32769)
        if pending["old"] != decoration_state(value, pending["target"])["affix"]:
            raise GrowthError("待定洗炼与当前词条不一致。", 32769)
    intent = value["pending_intent"]
    if intent is not None:
        if not isinstance(intent, dict) or set(intent) != {"id", "spec", "quote", "gold", "description", "status"}:
            raise GrowthError("待确认操作存档无效。", 32769)
        token(intent["id"])
        if intent["status"] not in ("prepared", "committed"):
            raise GrowthError("待确认操作状态无效。", 32769)
        integer(intent["gold"], "待确认费用")
        validate_spec(intent["spec"])
        if not isinstance(intent["quote"], str) or not re.fullmatch(r"[0-9a-f]{64}", intent["quote"]):
            raise GrowthError("待确认报价无效。", 32769)
        if not isinstance(intent["description"], str) or len(intent["description"]) > 300:
            raise GrowthError("待确认说明无效。", 32769)
    receipts = value["receipts"]
    if not isinstance(receipts, dict) or len(receipts) > MAX_RECEIPTS:
        raise GrowthError("养成回执列表无效。", 32769)
    for ident, receipt in receipts.items():
        token(ident)
        if (not isinstance(receipt, dict) or set(receipt) != {"id", "binding", "revision", "spec", "gold", "description", "replayed"}
                or receipt["id"] != ident):
            raise GrowthError("养成回执无效。", 32769)
        validate_spec(receipt["spec"])
        integer(receipt["revision"], "回执版本", 1, value["revision"])
        integer(receipt["gold"], "回执费用")
        if (not isinstance(receipt["binding"], str) or not re.fullmatch(r"[0-9a-f]{64}", receipt["binding"])
                or not isinstance(receipt["description"], str) or len(receipt["description"]) > 300
                or receipt["replayed"] is not False):
            raise GrowthError("养成回执内容无效。", 32769)
    if intent and intent["status"] == "committed" and intent["id"] not in receipts:
        raise GrowthError("已提交操作缺少持久回执。", 32769)
    if intent and intent["status"] == "committed":
        receipt = receipts[intent["id"]]
        if (receipt["binding"] != fingerprint({"spec": intent["spec"], "quote": intent["quote"]})
                or any(receipt[k] != intent[k] for k in ("spec", "gold", "description"))):
            raise GrowthError("待确认操作与回执不匹配。", 32769)
    return value


def validate_spec(spec):
    if not isinstance(spec, dict) or set(spec) != {"action", "target", "amount"}:
        raise GrowthError("养成请求格式无效。", 32769)
    action = spec["action"]
    if not isinstance(action, str) or action not in {"hero_level", "hero_break", "hero_awake", "decor_level",
                      "decor_study", "decor_recast", "decor_keep", "decor_discard"}:
        raise GrowthError("未知养成操作。", 32769)
    integer(spec["target"], "养成目标", 1, (1 << 64) - 1)
    if type(spec["amount"]) is not int or spec["amount"] not in ((1, 10) if action in {"hero_level", "decor_level"} else (1,)):
        raise GrowthError("养成数量无效。", 32769)


def affix_text(value):
    return "无" if not value else f"{AFFIX_NAMES[value['kind']]}+{value['value']}"


def hero_bonus(row):
    return (3 * (row["level"] - 1) + 20 * row["awakening"],
            30 * (row["level"] - 1) + 200 * row["awakening"], 5 * row["awakening"])


def decoration_bonus(row):
    result = [row["level"] * 5, row["level"] * 50, 0]
    if row["affix"]:
        result[{"atk": 0, "hp": 1, "def": 2}[row["affix"]["kind"]]] += row["affix"]["value"]
    return tuple(result)


def equipped_ids(profile, hero):
    """Same active appearance as the source passive chain; each ID once."""
    appearances = profile.get("hero_appearances", {})
    if not isinstance(appearances, Mapping):
        return set()
    holder = appearances.get(str(hero["uid"]), appearances.get(hero["uid"], {}))
    if not isinstance(holder, Mapping):
        return set()
    rows = holder.get("appearances", holder.get("appearance_list", []))
    if not isinstance(rows, list):
        return set()
    rows = [a for a in rows if isinstance(a, Mapping)]
    current_id = holder.get("current_id", -1)
    current = next((a for a in rows if a.get("id", a.get("Id")) == current_id), None)
    if current is None:
        if "current_id" in holder:
            current = next((a for a in rows if a.get("id", a.get("Id")) == 0), None)
        else:
            current = next((a for a in rows if a.get("is_used", a.get("IsUsed", 0)) > 0), None)
    if current is None:
        return set()
    entries = current.get("decorations", current.get("decoration_list", current.get("DecorationList", [])))
    if not isinstance(entries, list):
        return set()
    return {r.get("id", r.get("Id")) for r in entries if isinstance(r, Mapping)
            and type(r.get("id", r.get("Id"))) is int and r.get("id", r.get("Id")) > 0}


def bonus_for_hero(profile, hero, definitions):
    if not profile or KEY not in profile:
        return (0, 0, 0)
    root = state_view(profile)
    result = list(hero_bonus(hero_state(root, hero["uid"]))) if hero["id"] in HERO_IDS else [0, 0, 0]
    owned = {r.get("id") for r in profile.get("decoration_items", []) if isinstance(r, Mapping)}
    for ident in equipped_ids(profile, hero) & owned:
        if ident not in definitions or not definitions[ident].get("attr_basic"):
            continue
        for i, amount in enumerate(decoration_bonus(decoration_state(root, ident))):
            result[i] += amount
    if any(not 0 <= n <= MAX_BONUS for n in result):
        raise GrowthError("离线属性超出安全范围。", 32769)
    return tuple(result)


class IndependentGrowthLayer:
    def __init__(self, catalog):
        self.people = {r["id"]: r for r in catalog.table("a_people").records if r["id"] in HERO_IDS}
        self.decorations = {r["id"]: r for r in catalog.table("a_decoration").records if r["attr_basic"] > 0}
        if set(self.people) != HERO_IDS:
            raise GrowthError("六名正式魔女来源不完整。", 32772)

    def _target(self, profile, spec):
        if spec["action"].startswith("hero_"):
            found = [r for r in profile.get("heroes", []) if isinstance(r, Mapping) and r.get("uid") == spec["target"]]
            if len(found) != 1 or found[0].get("id") not in self.people:
                raise GrowthError("未持有或暂不支持这名魔女。", 516)
            return self.people[found[0]["id"]]["people_name"]
        found = [r for r in profile.get("decoration_items", []) if isinstance(r, Mapping) and r.get("id") == spec["target"]]
        if len(found) != 1 or spec["target"] not in self.decorations:
            raise GrowthError("仅可培养已持有且有源战斗属性的饰品。", 32769)
        definition = self.decorations[spec["target"]]
        return str(definition.get("name", definition.get("decoration_name", spec["target"])))

    def plan(self, profile, spec, root=None):
        validate_spec(spec)
        root = state_view(profile) if root is None else root
        name = self._target(profile, spec)
        action, target, amount = spec["action"], spec["target"], spec["amount"]
        gold = 0
        current = copy.deepcopy(hero_state(root, target) if action.startswith("hero_") else decoration_state(root, target))
        desired = copy.deepcopy(current)
        if action == "hero_level":
            cap = HERO_CAPS[current["breakthrough"]]
            if current["level"] >= cap:
                raise GrowthError("基础等级已到当前上限，请先突破。", 114)
            desired["level"] = min(cap, current["level"] + amount)
            gold = sum(500 * n for n in range(current["level"] + 1, desired["level"] + 1))
            detail = f"基础等级 {current['level']} → {desired['level']}"
        elif action == "hero_break":
            if current["breakthrough"] >= 3:
                raise GrowthError("基础突破已满。", 112)
            if current["level"] != HERO_CAPS[current["breakthrough"]]:
                raise GrowthError("基础等级达到当前上限后才能突破。", 118)
            desired["breakthrough"] += 1
            gold = 10_000 * desired["breakthrough"]
            detail = f"基础突破 {current['breakthrough']} → {desired['breakthrough']}，上限{HERO_CAPS[desired['breakthrough']]}"
        elif action == "hero_awake":
            if current["level"] != 60 or current["awakening"] >= 5:
                raise GrowthError("基础60级后可觉醒，最多5阶。", 118)
            desired["awakening"] += 1
            gold = 20_000 * desired["awakening"]
            detail = f"基础觉醒 {current['awakening']} → {desired['awakening']}"
        elif action == "decor_level":
            cap = 10 if current["studied"] else 5
            if current["level"] >= cap:
                raise GrowthError("饰品已到当前上限；5级可先研习。", 114)
            desired["level"] = min(cap, current["level"] + amount)
            gold = sum(1_000 * n * n for n in range(current["level"] + 1, desired["level"] + 1))
            detail = f"独立等级 {current['level']} → {desired['level']}"
        elif action == "decor_study":
            if current["level"] != 5 or current["studied"]:
                raise GrowthError("饰品5级时可研习一次。", 118)
            desired["studied"] = 1
            gold, detail = 50_000, "研习，开放独立等级上限10"
        elif action == "decor_recast":
            if current["level"] < 1:
                raise GrowthError("饰品培养到1级后可洗炼。", 118)
            if root["pending_candidate"] is not None:
                raise GrowthError("请先保留或放弃上一份饰品洗炼候选。", 32000)
            gold, detail = 20_000, "生成一条洗炼候选；保留后生效，放弃不退费"
        else:
            pending = root["pending_candidate"]
            if not pending or pending["target"] != target or pending["old"] != current["affix"]:
                raise GrowthError("没有匹配的待定饰品词条。", 32769)
            if action == "decor_keep":
                desired["affix"] = copy.deepcopy(pending["candidate"])
            detail = ("保留 " if action == "decor_keep" else "放弃 ") + affix_text(pending["candidate"])
        description = name + " · " + detail
        quote = fingerprint({"version": VERSION, "role_id": profile["role_id"], "spec": spec,
                             "revision": root["revision"], "current": current, "gold": gold,
                             "candidate": root["pending_candidate"], "counter": root["reroll_counter"]})
        return {"spec": copy.deepcopy(spec), "quote": quote, "gold": gold,
                "description": description, "current": current, "desired": desired}

    @staticmethod
    def _not_fighting(profile):
        if profile.get("battle_runtime", {}).get("active") or profile.get("tower_battles", {}).get("active_battle"):
            raise GrowthError("请结束或退出当前战斗后再培养。", 32000)

    def prepare(self, profile, spec, ident, quote):
        token(ident)
        self._not_fighting(profile)
        root = copy.deepcopy(state_view(profile))
        validate_spec(spec)
        binding = fingerprint({"spec": spec, "quote": quote})
        receipt = root["receipts"].get(ident)
        if receipt:
            if receipt["binding"] != binding:
                raise GrowthError("操作凭据不能用于另一笔养成。", 32769)
            return {}, {**receipt, "replayed": True}
        pending = root["pending_intent"]
        if pending:
            if pending["id"] == ident and pending["spec"] == spec and pending["quote"] == quote:
                return {}, pending
            raise GrowthError("请先核对上一笔待确认操作。", 32000)
        planned = self.plan(profile, spec, root)
        if quote != planned["quote"]:
            raise GrowthError("培养状态已变化，请刷新后重新确认费用。", 32769)
        if planned["gold"]:
            deduct_resources(profile, [(2, 0, planned["gold"])])
        pending = {"id": ident, "spec": copy.deepcopy(spec), "quote": quote,
                   "gold": planned["gold"], "description": planned["description"], "status": "prepared"}
        root["pending_intent"] = pending
        return {KEY: root}, pending

    def commit(self, profile, ident):
        token(ident)
        root = copy.deepcopy(state_view(profile))
        receipt = root["receipts"].get(ident)
        if receipt:
            return {}, {**receipt, "replayed": True}
        self._not_fighting(profile)
        pending = root["pending_intent"]
        if not pending or pending["id"] != ident or pending["status"] != "prepared":
            raise GrowthError("没有匹配的待提交操作。", 32769)
        planned = self.plan(profile, pending["spec"], root)
        if planned["quote"] != pending["quote"] or planned["gold"] != pending["gold"]:
            raise GrowthError("原报价已过期，请取消待操作并重新确认。", 32769)
        changes = deduct_resources(profile, [(2, 0, planned["gold"])]) if planned["gold"] else {}
        spec, action = planned["spec"], planned["spec"]["action"]
        holder = "heroes" if action.startswith("hero_") else "decorations"
        root[holder][str(spec["target"])] = planned["desired"]
        if action == "decor_recast":
            counter = integer(root["reroll_counter"] + 1, "洗炼计数", 1)
            seed = fingerprint({"policy": VERSION, "role": profile["role_id"], "target": spec["target"], "counter": counter})
            rng = random.Random(int(seed, 16))
            kind = rng.choice(tuple(AFFIX_RANGES))
            candidate = {"kind": kind, "value": rng.randint(*AFFIX_RANGES[kind])}
            root["pending_candidate"] = {"id": seed[:32], "target": spec["target"],
                                         "old": planned["current"]["affix"], "candidate": candidate}
            root["reroll_counter"] = counter
        elif action in {"decor_keep", "decor_discard"}:
            root["pending_candidate"] = None
        root["revision"] = integer(root["revision"] + 1, "培养版本", 1)
        receipt = {"id": ident, "binding": fingerprint({"spec": spec, "quote": pending["quote"]}),
                   "revision": root["revision"], "spec": copy.deepcopy(spec), "gold": planned["gold"],
                   "description": planned["description"], "replayed": False}
        root["receipts"][ident] = receipt
        if len(root["receipts"]) > MAX_RECEIPTS:
            oldest = min(root["receipts"], key=lambda k: root["receipts"][k]["revision"])
            del root["receipts"][oldest]
        root["pending_intent"]["status"] = "committed"
        history = copy.deepcopy(profile.get("resource_history", []))
        history.append({"source": KEY, "policy_version": VERSION, "operation": action,
                        "transaction_id": ident, "target": spec["target"],
                        "costs": [{"type": 2, "id": 0, "num": planned["gold"]}] if planned["gold"] else [],
                        "rewards": [], "context": {"before": planned["current"], "after": planned["desired"]}})
        changes.update({KEY: root, "resource_history": history})
        return changes, receipt

    def clear_pending(self, profile, ident, *, cancel=False):
        token(ident)
        root = copy.deepcopy(state_view(profile))
        pending = root["pending_intent"]
        if pending is None:
            if ident in root["receipts"]:
                return {}
            raise GrowthError("没有匹配的待确认操作。", 32769)
        if pending["id"] != ident:
            raise GrowthError("不能清除另一笔操作。", 32769)
        if (pending["status"] == "prepared") != cancel:
            raise GrowthError("操作状态已变化，请先核对结果。", 32769)
        root["pending_intent"] = None
        return {KEY: root}

    def seed_full_preset(self, profile):
        """Call ONLY while explicitly creating a new all-unlocked save."""
        if KEY in profile:
            return
        root = empty_state()
        for row in profile.get("heroes", []):
            if row["id"] in self.people:
                root["heroes"][str(row["uid"])] = {"level": 60, "breakthrough": 3, "awakening": 5}
        for row in profile.get("decoration_items", []):
            if row["id"] in self.decorations:
                root["decorations"][str(row["id"])] = {"level": 10, "studied": 1, "affix": None}
        profile[KEY] = root

    def snapshot(self, profile):
        root = state_view(profile)
        rows = []
        pending = root["pending_candidate"]
        for hero in profile.get("heroes", []):
            if hero["id"] not in self.people:
                continue
            state = hero_state(root, hero["uid"])
            bonus = hero_bonus(state)
            rows.append({"kind": "hero", "id": hero["uid"], "name": self.people[hero["id"]]["people_name"],
                         "summary": f"基础等级{state['level']}/{HERO_CAPS[state['breakthrough']]} · 突破{state['breakthrough']}/3 · 觉醒{state['awakening']}/5",
                         "details": f"独立增益：攻击+{bonus[0]}，生命+{bonus[1]}，防御+{bonus[2]}。职业、原技能与好感不变。",
                         "actions": [], "action_names": ("hero_level", "hero_break", "hero_awake")})
        for item in profile.get("decoration_items", []):
            ident = item["id"]
            if ident not in self.decorations:
                continue
            state = decoration_state(root, ident)
            definition = self.decorations[ident]
            worn = [self.people.get(h["id"], {}).get("people_name", str(h["id"])) for h in profile.get("heroes", []) if ident in equipped_ids(profile, h)]
            details = "当前装备：" + ("、".join(worn) if worn else "无（未装备不加成）") + f"。独立词条：{affix_text(state['affix'])}。原觉醒被动不变。"
            if pending and pending["target"] == ident:
                details += f" 待定候选：{affix_text(pending['candidate'])}（尚未生效）。"
            rows.append({"kind": "decor", "id": ident,
                         "name": str(definition.get("name", definition.get("decoration_name", ident))),
                         "summary": f"独立等级{state['level']}/{10 if state['studied'] else 5} · 研习{state['studied']}/1",
                         "details": details, "actions": [],
                         "action_names": ("decor_level", "decor_study", "decor_recast", "decor_keep", "decor_discard")})
        for row in rows:
            for action in row.pop("action_names"):
                for amount in ((1, 10) if action in {"hero_level", "decor_level"} else (1,)):
                    spec = {"action": action, "target": row["id"], "amount": amount}
                    try:
                        plan = self.plan(profile, spec, root)
                    except GrowthError:
                        continue
                    row["actions"].append({k: plan[k] for k in ("spec", "quote", "gold", "description")})
        return {"version": VERSION, "gold": integer(profile.get("gold", 0), "本勒"),
                "revision": root["revision"], "rows": rows,
                "pending_intent": copy.deepcopy(root["pending_intent"]),
                "pending_candidate": copy.deepcopy(pending),
                "limits": "离线规则v1。只作用持有目标；饰品须在原外观页装备；塔内不计新增增益。战斗中不可培养。"}
