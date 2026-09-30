"""New-player ownership, earned unlocks and source-backed account progress.

Existing all-owned accounts never enter this policy implicitly. Catalog
availability and account ownership are separate, persistent facts.
"""
from __future__ import annotations

import bisect
import copy
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

try:
    from .gospel_acquisition import GospelFactory
    from .offline_economy import deduct_resources, EconomyError, NORMAL_ECONOMY
except ImportError:
    from gospel_acquisition import GospelFactory
    from offline_economy import deduct_resources, EconomyError, NORMAL_ECONOMY

MODE = "newcomer-v1"
REVISION = 1
RESPONSE_IDS = {256: 257, 391: 392, 616: 617, 709: 710, 937: 938, 939: 940}
BUILDING_FEATURES = {1999701: 121, 1999702: 123, 1999703: 124, 1999705: 125}
REQUEST_FEATURES = {
    39: 31, 53: 41, 177: 41, 127: 41,
    28: 82, 30: 84, 32: 95, 68: 83, 494: 85,
    465: 88, 951: 90, 957: 90, 959: 90,
    88: 121, 1115: 121, 97: 231,
    1380: 500, 1382: 500, 1376: 502,
}


def enabled(profile: Mapping[str, Any]) -> bool:
    value = profile.get("player_progression")
    return isinstance(value, Mapping) and value.get("mode") == MODE


def _ids(value):
    if value in (None, ""): return ()
    values = json.loads(value) if isinstance(value, str) else value
    if type(values) is int:
        values = [values]
    if not isinstance(values, (list, tuple)) or any(type(v) is not int or v <= 0 for v in values):
        raise ValueError("Progression prerequisites must be positive ID arrays")
    return tuple(values)


def main_entry_requirements(dungeons, chapters, unlocks):
    """Link otherwise unconnected roots using source terminal sections.

    a_dungeon.unlock_dungeon is an OUTGOING unlock list (proved by
    HardestChapterViewItem.Init). UI receives eligibility in dungeon snapshots;
    inferred root prerequisites must never be written into that source field.
    """
    incoming = {i for r in dungeons.values() for field in ('next_dungeon','unlock_dungeon')
                for i in _ids(r.get(field))}
    groups = {}
    for row in dungeons.values():
        if chapters.get(int(row["belong_chapte"]), {}).get("dungeon_type") == 0:
            groups.setdefault((int(row["belong_chapte"]), int(row["difficulty_type"])), []).append(row)
    order = sorted({cid for cid,_ in groups}, key=lambda i:(int(chapters[i]["scenario"]),i))
    result = {}
    def terminal(key):
        rows = groups.get(key, [])
        if not rows: return ()
        section = max(int(r["section"]) for r in rows)
        return tuple(int(r["id"]) for r in rows if int(r["section"]) == section)
    hard_gate = int(unlocks.get(182, {}).get("unlock_dungeon", 0))
    for (cid,difficulty),rows in groups.items():
        for row in rows:
            did = int(row["id"])
            if did in incoming or did == 8600011: continue
            required = set()
            earlier = [r for r in rows if int(r["section"]) < int(row["section"])]
            if earlier:
                section = max(int(r["section"]) for r in earlier)
                required.update(int(r["id"]) for r in earlier if int(r["section"]) == section)
            else:
                position = order.index(cid)
                if position: required.update(terminal((order[position-1],difficulty)))
            if difficulty > 0:
                required.update(terminal((cid,difficulty-1)))
                if hard_gate: required.add(hard_gate)
            result[did] = tuple(sorted(required))
    return result


@dataclass(frozen=True)
class ProgressionResult:
    response_id: int
    body: bytes
    error_code: int = 0
    reason: str = ""
    changes: dict = field(default_factory=dict)
    notifications: tuple = ()
    @property
    def ok(self): return self.error_code == 0


class PlayerProgression:
    def __init__(self, catalog, hero_progression, codec):
        self.catalog = catalog
        self.hero_progression = hero_progression
        self.codec = codec
        self.factory = GospelFactory(catalog.root)
        self.people = catalog.table("a_people").by_id
        self.jobs = catalog.table("a_job_skill").by_id
        self.unlocks = {int(r["function_type"]): r for r in catalog.table("a_unlock").records}
        self.branch_guide_ids = set(catalog.table("a_guide_force").by_id)
        self.teaching_guide_ids = set(catalog.table("a_teaching_guide").by_id)
        self.dungeons = catalog.table("a_dungeon").by_id
        self.chapters = catalog.table("a_chapte").by_id
        self.main_stage_ids = {}
        for dungeon in self.dungeons.values():
            chapter_id = int(dungeon['belong_chapte'])
            chapter = self.chapters.get(chapter_id)
            if chapter and chapter['dungeon_type'] == 0 and dungeon['difficulty_type'] == 0:
                self.main_stage_ids.setdefault(chapter_id, set()).add(int(dungeon['id']))
        self.work_levels = catalog.table("a_work_level").by_id
        self.level_rows = sorted(catalog.table("a_playerstate").records, key=lambda r: r["level"])
        self.exp_thresholds = [r["exp"] for r in self.level_rows]
        self.incoming = {}
        self.unlock_sources = {}
        for row in self.dungeons.values():
            for target in _ids(row.get("next_dungeon")):
                if target in self.dungeons:
                    self.incoming.setdefault(target, set()).add(int(row["id"]))
            for target in _ids(row.get('unlock_dungeon')):
                if target in self.dungeons:
                    self.unlock_sources.setdefault(target, set()).add(int(row['id']))
        self.main_entries = main_entry_requirements(self.dungeons,self.chapters,self.unlocks)
        source = Path(catalog.root) / "a_condition.json"
        self.conditions = {}
        self.condition_hash = ""
        if source.is_file():
            raw = source.read_bytes()
            self.conditions = {r["id"]: r for r in json.loads(raw)["records"]}
            self.condition_hash = hashlib.sha256(raw).hexdigest()
        self.chapter_condition_ids = {i for r in self.chapters.values() for i in _ids(r["unlocking_conditions"])}

    def initial_hero(self, hero_id, uid, *, initial_profession=None):
        person = self.people[int(hero_id)]
        job_id = int(initial_profession if initial_profession is not None else person["job_default"])
        row = {
            "uid": int(uid), "id": int(hero_id), "level": 1, "level_limit": 140,
            "exp": 0, "fake_level": 1, "breakthrough_level": 0, "awakening_level": 0,
            "appearance_id": int(hero_id), "state": 0, "birth_time": 1,
            "has_dated": 0, "scene_id": 0, "profession_id": job_id,
            "fondness": 0, "fondness_level": 0, "fondness_fake_level": 0,
            "fondness_switch": 1, "rewarded_fondness_levels": [],
            "active_skills": [{"id": int(i), "level": 1} for i in _ids(person["skill"])],
        }
        if job_id != 3900000:
            profession = self.hero_progression.catalog.professions[job_id]
            group = profession.groups[0]
            row.update(profession_level=1, profession_level_limit=group.level_cap,
                profession_breakthrough_level=0, profession_talent_ids=[profession.talents[0].talent_id],
                profession_unlocked_tree_ids=[group.group_id], profession_talent_tree_id=group.group_id,
                profession_rewarded_tree_ids=[])
        return row

    def initialize(self, profile):
        if not self.chapter_condition_ids <= set(self.conditions):
            raise ValueError("Newcomer mode requires the configured a_condition chapter closure")
        initial = self.catalog.table("a_initial").records[0]
        people, jobs = _ids(initial["people"]), _ids(initial["job"])
        if len(people) != len(jobs): raise ValueError("Initial hero/profession mismatch")
        profile.update(
            profile_mode=MODE, all_content_unlocked=False, profile_revision=REVISION,
            content_catalog_version=self.catalog.version,
            player_progression={"mode": MODE, "revision": REVISION, "condition_sha256": self.condition_hash},
            offline_economy={"mode": NORMAL_ECONOMY, "origin": "source-newcomer-v1"},
            level=1, exp=0, gold=0, diamond=0, minidiamond=0, energy=0,
            heroes=[self.initial_hero(h, 1000000001 + n, initial_profession=jobs[n]) for n,h in enumerate(people)],
            gospels=[], inventory={}, rune_items=[], hero_runes=[], decoration_items=[],
            private_equipment=[], medals=[], buildings={}, content_state={},
            guide_step=0, guide_steps={}, tutorial_completed=False,
            unlocked_dungeons=[8600011], completed_dungeons=[], selected_dungeon_ids=[],
            formation_state={}, phalanxes=[], current_phalanx_id=0,
            oracle_content={}, life_content={}, resource_balances={},
        )
        for reward in json.loads(initial["warehouse"]):
            field = {0:"diamond",1:"minidiamond",2:"gold",3:"energy"}.get(int(reward["type"]))
            if field is None or int(reward["id"]) != 0: raise ValueError("Unmapped initial resource")
            profile[field] += int(reward["num"])
        self.normalize(profile)

    def condition_met(self, profile, condition_id):
        row = self.conditions.get(int(condition_id))
        if row is None: return False
        kind = int(row["achievements_type"])
        first, second, target = (int(row[k]) for k in (
            "achievements_parameter1", "achievements_parameter2", "achievements_parameter3"))
        completed = set(profile.get("completed_dungeons", []))
        if kind in (3000, 3008): return first in completed
        if kind == 3004: return first in completed and int(profile.get("level", 1)) >= target
        if kind in (3013, 3014):
            unlock = 1 if kind == 3013 else 2
            return any(int(self.jobs.get(i, {}).get("unlock_skill", 0)) == unlock
                       for h in profile.get("heroes", []) for i in h.get("profession_talent_ids", []))
        # Unimplemented achievement mechanics are unfinished, never granted
        # merely because their catalog entries exist.
        return False

    def work_condition_met(self, profile, work_level_id):
        if not work_level_id: return True
        requirement = self.work_levels.get(int(work_level_id))
        if requirement is None: return False
        current = profile.get("buildings", {}).get(str(requirement["building_id"]))
        return isinstance(current, Mapping) and int(current.get("level", 0)) >= int(requirement["work_level"])

    def function_open(self, profile, function_type):
        if not enabled(profile): return True
        if not function_type: return True
        row = self.unlocks.get(int(function_type))
        if row is None or int(row["unlock_level"]) >= 999: return False
        if int(profile.get("level", 1)) < int(row["unlock_level"]): return False
        if row["unlock_dungeon"] and int(row["unlock_dungeon"]) not in profile.get("completed_dungeons", []): return False
        work_open = self.work_condition_met(profile, row["unlock_work"])
        if int(function_type) == 265:
            alternate = self.catalog.table("a_global").by_id[9991020]["int_value"]
            work_open = work_open or self.work_condition_met(profile, alternate)
        return work_open

    def dungeon_open(self, profile, dungeon_id):
        if not enabled(profile): return True
        row = self.dungeons.get(int(dungeon_id))
        if row is None: return False
        completed = set(profile.get("completed_dungeons", []))
        if int(dungeon_id) in completed: return True
        if int(profile.get("level", 1)) < int(row["unlock_level"]): return False
        chapter = self.chapters.get(int(row["belong_chapte"]))
        if chapter is None: return False
        if not all(self.condition_met(profile, c) for c in _ids(chapter["unlocking_conditions"])): return False
        kind = int(chapter["dungeon_type"])
        # Offline legacy activities stay available after the first
        # chapter's existing 1-10 gate; their higher source gates still apply.
        if kind not in (0,1,2,3,4,5,6,19) and 8600023 not in completed:
            return False
        unlock_sources = self.unlock_sources.get(int(dungeon_id), set())
        if unlock_sources & completed:
            return True
        predecessors = self.incoming.get(int(dungeon_id), set())
        if int(dungeon_id) == 8600011: return True
        if predecessors:
            return predecessors <= completed
        if unlock_sources:
            return False
        # Main-line roots beyond the first chapter need an actual predecessor
        # or a defined condition; side routes also require their entry UI.
        if kind == 0:
            required = set(self.main_entries.get(int(dungeon_id), ()))
            return bool(required or _ids(chapter["unlocking_conditions"])) and required <= completed
        feature = 183 if kind in (1,2,3,4,5) else 501 if kind == 19 else 111
        return self.function_open(profile, feature)

    def normalize(self, profile):
        if not enabled(profile): return
        # Earlier captures used unlocked_guides for 937 while the login and
        # article catalog read unlocked_teaching_guide_ids. Preserve the actual
        # observed article IDs, excluding the separate forced-step domain.
        guides = self.teaching_guide_ids & (set(profile.get('unlocked_guides', [])) |
                                           set(profile.get('unlocked_teaching_guide_ids', [])))
        profile['unlocked_teaching_guide_ids'] = sorted(guides)
        read = set(profile.get('read_teaching_guide_ids', [])) | {
            r['param1'] for r in profile.get('cleared_red_dots', []) if r.get('type') == 2012}
        profile['read_teaching_guide_ids'] = sorted(guides & read)
        level = min(len(self.level_rows), bisect.bisect_right(self.exp_thresholds, int(profile.get("exp", 0))))
        profile["level"] = max(1, int(profile.get("level", 1)), level)
        # Source cursor 4 is reached after the dream/prologue, before 1-1.
        # The earned opening route ends only after actual 1-3 completion.
        profile["tutorial_completed"] = 8600013 in profile.get("completed_dungeons",[])
        # Base Witch enhancement costs are not present in the runtime tables.
        # In this explicit offline mode base level follows the account; real
        # profession/Gospel costs remain separate. Old accounts are untouched.
        heroes = []
        for source in profile.get("heroes", []):
            row = self.initial_hero(source["id"], source["uid"], initial_profession=source.get("profession_id"))
            row.update(copy.deepcopy(source))
            if row.get("profession_id") == 3900000 and 8600012 in profile.get("completed_dungeons", []):
                upgraded = self.initial_hero(row["id"], row["uid"])
                row.update({k:v for k,v in upgraded.items() if k.startswith("profession_")})
            old_level = int(row.get("level", 1))
            if int(row.get("fake_level", old_level)) == old_level:
                row["fake_level"] = max(old_level, profile["level"])
            row["level"] = max(old_level, profile["level"])
            heroes.append(row)
        profile["heroes"] = heroes
        profile["gospels"] = [self.factory.repair_missing(r) if self.factory.needs_repair(r) else r
                               for r in profile.get("gospels", [])]
        profile["unlocked_features"] = sorted(int(r["id"]) for k,r in self.unlocks.items() if self.function_open(profile,k))
        profile["unlocked_dungeons"] = sorted(d for d in self.dungeons if self.dungeon_open(profile,d))
        # Explicit offline presentation policy: completed ordinary mainline
        # nodes (including story-only stages) / source nodes, rounded down.
        # Difficulty variants and unrelated activities never inflate it.
        completed = set(profile.get('completed_dungeons', []))
        chapter_progress = dict(profile.get('chapter_progress', {}))
        main_total = main_completed = 0
        for chapter_id, stage_ids in self.main_stage_ids.items():
            done = len(completed & stage_ids)
            chapter_progress[str(chapter_id)] = 100 * done // len(stage_ids)
            main_completed += done
            main_total += len(stage_ids)
        profile['chapter_progress'] = chapter_progress
        profile['overall_progress'] = 100 * main_completed // main_total if main_total else 0
        profile["progression_achievements"] = {
            str(i): {"Id":i,"Type":int(self.conditions[i]["achievements_type"]),
                     "Progress":int(self.condition_met(profile,i)),
                     "Status":int(self.condition_met(profile,i))}
            for i in sorted(self.chapter_condition_ids) if i in self.conditions}
        profile["player_progression"]["revision"] = REVISION
        profile["player_progression"]["condition_sha256"] = self.condition_hash

    def award_level_up_energy(self, profile, previous_level):
        """Actual EXP transactions grant the energy advertised by UserLevelupPop.

        Called after normalization inside the same account commit, never on
        profile reads/imports. Multi-level gains award each reached source row.
        """
        if not enabled(profile) or profile['level'] <= previous_level:
            return
        claimed = set(profile.get('account_level_energy_rewards', []))
        levels = [r for r in self.level_rows if previous_level < r['level'] <= profile['level']
                  and r['level'] not in claimed]
        if not levels:
            return
        offered = sum(int(r['energy_limit']) for r in levels)
        limit = int(self.catalog.table('a_global').by_id[9990999]['int_value'])
        applied = min(offered, max(0, limit-int(profile.get('energy', 0))))
        profile['energy'] = int(profile.get('energy', 0)) + applied
        profile['account_level_energy_rewards'] = sorted(claimed | {r['level'] for r in levels})
        profile['resource_history'] = [*copy.deepcopy(profile.get('resource_history', [])), {
            'source': 'account_level_up', 'from_level': previous_level, 'to_level': profile['level'],
            'source_rows': [r['id'] for r in levels], 'costs': [],
            'rewards': [{'res_type':3,'item_id':0,'count':applied}],
            'offered_energy': offered, 'saturated_energy': offered-applied}]

    def handle(self, command_id, profile, body):
        if command_id not in RESPONSE_IDS or not enabled(profile): return None
        if command_id in (616, 937, 939):
            try:
                from .content_responses import iter_wire_fields
            except ImportError:
                from content_responses import iter_wire_fields
            fields = list(iter_wire_fields(body))
            domain = self.branch_guide_ids if command_id == 616 else self.teaching_guide_ids
            valid = len(fields) == 1 and fields[0][0:2] == (1, 0) and fields[0][2] in domain
            branch = fields[0][2] if valid else 0
            if command_id == 939 and branch not in profile.get('unlocked_teaching_guide_ids', []):
                valid = False
            changes = {}
            if valid:
                if command_id == 616:
                    steps = copy.deepcopy(profile.get('guide_steps', {}))
                    steps[str(branch)] = True
                    changes['guide_steps'] = steps
                else:
                    key = 'unlocked_teaching_guide_ids' if command_id == 937 else 'read_teaching_guide_ids'
                    changes[key] = sorted(set(profile.get(key, [])) | {branch})
            name = {616:'SC_AddBranchGuideStepRet',937:'SC_UnlockTeachGuideRet',939:'SC_ReadTeachGuideRet'}[command_id]
            return ProgressionResult(command_id+1, self.codec.encode(name, {
                'Errorcode': 0 if valid else 32000, 'Id' if command_id == 616 else 'TeachGuideId': branch}),
                0 if valid else 32000, '' if valid else 'Unknown, locked or malformed guide', changes)
        if command_id == 391:
            rows = [{"ArchitectureId":r["id"],"Level":r["level"]} for r in profile.get("buildings",{}).values()]
            return ProgressionResult(392,self.codec.encode("SC_ArchitectureDatasRet",{"ArchitectureList":rows}))
        request_name = "CS_UnlockArchitecture" if command_id == 709 else "CS_UpgradeArchitecture"
        response_name = "SC_UnlockArchitectureRet" if command_id == 709 else "SC_UpgradeArchitectureRet"
        values = self.codec.decode(request_name, body)
        bid = int(values.get("ArchitectureId",0))
        def result(error=0, reason="", level=0, changes=None):
            return ProgressionResult(RESPONSE_IDS[command_id],self.codec.encode(response_name,
                {"Errorcode":error,"ArchitectureId":bid,"Level":level}),error,reason,changes or {})
        if bid not in BUILDING_FEATURES: return result(32000,"Unknown architecture")
        if not self.function_open(profile,BUILDING_FEATURES[bid]): return result(314,"Architecture story prerequisite is incomplete")
        buildings=copy.deepcopy(profile.get("buildings",{})); current=buildings.get(str(bid))
        if command_id == 709:
            if current is not None: return result(level=int(current["level"]))
            level=0
        else:
            if current is None: return result(314,"Architecture is not unlocked")
            level=int(current["level"])
        source=next((r for r in self.work_levels.values() if r["building_id"]==bid and r["work_level"]==level),None)
        if source is None or not source["enhance_Item"]: return result(112,"Architecture is at its level cap",level)
        if not self.work_condition_met(profile,source["enhance_condition"]): return result(314,"Prerequisite architecture level is incomplete",level)
        costs=[(r["type"],r["id"],r["num"]) for r in json.loads(source["enhance_Item"])]
        try: changes=deduct_resources(profile,costs)
        except EconomyError as exc: return result(exc.error_code,str(exc),level)
        history=copy.deepcopy(profile.get("resource_history",[]))
        history.append({"source":"architecture","operation":"build" if command_id==709 else "upgrade","architecture_id":bid,
                        "from_level":level,"to_level":level+1,
                        "costs":[{"type":t,"id":i,"count":n} for t,i,n in costs]})
        changes["resource_history"]=history
        target=level+1
        buildings[str(bid)]={"id":bid,"level":target,"work_level":target}
        changes["buildings"]=buildings
        return result(level=target,changes=changes)
