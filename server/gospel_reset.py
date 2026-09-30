"""Atomic star-trace reset, matching FuyingData.CalcResetMaterial's preview.

This is an offline policy backed by the defined runtime, not a claim about
the lost server. In particular EXP is greedily converted in descending item
ID order and a remainder rounds up to one smallest EXP item, just like the UI.
Awakening/fondness/spirit/appearances and already-taken rewards are preserved.
"""
from __future__ import annotations

import json
from collections.abc import Mapping

try:
    from . import gospel_progression as growth
except ImportError:  # Android embedded runtime
    import gospel_progression as growth

RESPONSE_IDS = {1092: 1093}
POLICY_VERSION = 1


class GospelResetLayer:
    def __init__(self, progression):
        self.progression = progression
        self.codec = progression.codec
        content = progression.content_catalog
        globals_ = content.table("a_global").by_id
        self.roots = {
            int(globals_[9991078]["int_value"]): 80,
            int(globals_[9991079]["int_value"]): 100,
        }
        if len(self.roots) != 2 or min(self.roots) <= 0:
            raise ValueError("Invalid reset root configuration")
        self.boundaries = tuple(json.loads(globals_[9990289]["string_value"]))[:3]
        self.exp_items = []
        for item_id in sorted(progression.catalog.gospel_exp_by_item, reverse=True):
            additions = json.loads(content.table("a_item").by_id[item_id]["add_num"])
            self.exp_items.append((item_id, int(additions[0]["num"])))
        if not self.exp_items or any(amount <= 0 for _, amount in self.exp_items):
            raise ValueError("Invalid reset EXP item configuration")

    def refund(self, row, definition, root):
        percent = self.roots[root]
        stage = sum(int(row["level"]) > level for level in self.boundaries)
        if stage != definition.break_limits.index(int(row["level_limit"])):
            raise growth.GospelProfileError("Gospel level and ascension stage disagree")
        exp = int(row["exp"])
        if not int(definition.states[int(row["level"])]["exp"]) <= exp <= int(definition.states[definition.max_level]["exp"]):
            raise growth.GospelProfileError("Gospel EXP is outside its state table")
        resources = []
        remaining = exp * percent // 100
        if remaining:
            for item_id, amount in self.exp_items:
                count, remaining = divmod(remaining, amount)
                if count:
                    resources.append(dict(type=4, id=item_id, num=count))
            if remaining:
                resources.append(dict(type=4, id=self.exp_items[-1][0], num=1))
            gap = sum(int(definition.states[level + 1]["exp"]) - int(definition.states[level]["exp"])
                      for level in self.boundaries[:stage])
            gold = (exp - gap) * self.progression.catalog.gospel_exp_gold_rates[stage] // 10000
            if gold:
                resources.append(dict(type=2, id=0, num=gold))
        for costs in definition.ascension_costs[:stage]:
            resources.extend(costs)
        for skill in row["active_skills"]:
            for level in range(1, int(skill["level"])):
                resources.extend(definition.skill_costs[int(skill["id"])][level])
        rewards = [dict(r) for r in self.progression._aggregate_resources(resources)]
        for reward in rewards:
            if reward["type"] == 2:
                reward["num"] = reward["num"] * percent // 100
            if reward["type"] not in (2, 4) or reward["num"] > growth.UINT32_MAX:
                raise growth.GospelProfileError("Reset reward cannot be represented by the original UI")
        return tuple(r for r in rewards if r["num"] > 0)

    def current_state_notifications(self, profile, body):
        fields = growth._wire_field_map(body)
        uid = growth._required_uint(fields, 1, minimum=1)
        try:
            _, _, _, row, definition = self.progression._owned_gospel(profile, uid)
        except LookupError:
            return ()
        return (self.progression._update_notification(row, definition, profile),
                self.progression._ready_notification(row))

    def handle(self, command, profile, request_body=b""):
        if command not in RESPONSE_IDS:
            return None

        def error(code, reason):
            return growth.GospelProgressionResult(1093, self.codec.encode("SC_ResetGospelRet", {"Errorcode": code}),
                                                  error_code=code, reason=reason)
        if not isinstance(profile, Mapping):
            return error(32769, "No local account")
        try:
            fields = growth._wire_field_map(request_body)
            if set(fields) - {1, 2, 127}:
                raise ValueError("Unknown reset request field")
            uid = growth._required_uint(fields, 1, minimum=1)
            root = growth._required_uint(fields, 2, minimum=1, maximum=growth.UINT32_MAX)
            if root not in self.roots:
                raise ValueError("Unknown reset root")
        except (ValueError, TypeError, growth.ProtobufDecodeError) as exc:
            return error(32769, str(exc))
        # No growth mutation may invalidate an in-flight combat snapshot.
        if profile.get("battle_runtime", {}).get("active") or profile.get("tower_battles", {}).get("active_battle"):
            return error(32000, "Finish or leave the current battle before resetting")
        try:
            _, rows, index, row, definition = self.progression._owned_gospel(profile, uid)
            rewards = self.refund(row, definition, root)
            if not rewards:
                return error(32769, "Gospel has no resettable investment")
            reset_number = growth._profile_uint(row, "reset_count", 0, maximum=growth.UINT32_MAX - 1) + 1
            changes = self.progression._economy_changes(
                profile, request_id=1092, operation="reset", row=row,
                costs=({"type": 4, "id": root, "num": 1},), rewards=rewards,
                allow_saturated_rewards=False,
                context={"target_stage": reset_number, "policy_version": POLICY_VERSION,
                         "root_id": root, "from_level": row["level"], "from_limit": row["level_limit"]})
            row.update(level=1, fake_level=1, level_limit=definition.break_limits[0],
                       exp=int(definition.states[1]["exp"]), spirit_is_ready=0,
                       reset_count=reset_number, reset_policy_version=POLICY_VERSION)
            for skill in row["active_skills"]:
                skill.update(level=1, valid_level=1)
            rows[index] = row
            changes["gospels"] = rows
            projected = {**profile, **changes}
            gold = sum(r["num"] for r in rewards if r["type"] == 2)
            # Source OnResetGospelAction treats ItemUId as static item ID,
            # with 0 reserved for the gold icon. These are display records;
            # the economy above grants the actual currency exactly once.
            body = self.codec.encode("SC_ResetGospelRet", {
                "Errorcode": 0, "ReturnGold": gold,
                "ItemList": [{"ItemUId": r["id"], "ItemNum": r["num"]} for r in rewards]})
            notifications = (self.progression._update_notification(row, definition, projected),
                             self.progression._ready_notification(row))
            return growth.GospelProgressionResult(1093, body, changes, notifications,
                                                  reason="source-preview reset committed")
        except LookupError as exc:
            return error(424, str(exc))
        except growth.GospelEconomyError as exc:
            return error(exc.error_code, str(exc))
        except (growth.GospelProfileError, KeyError, ValueError, TypeError) as exc:
            return error(438, str(exc))
