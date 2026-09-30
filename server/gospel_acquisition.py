"""Source-backed initial Gospel records shared by every acquisition path.

Catalog visibility does not grant ownership. This module only constructs a
requested object or repairs missing fields on an already owned object.
"""
from __future__ import annotations

import copy
import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping


@lru_cache(maxsize=8)
def _templates(root: str, revisions: tuple) -> dict:
    def rows(name):
        value = json.loads((Path(root) / (name + ".json")).read_text())
        return value["records"]

    starts = {int(r["gospel_id"]): int(r["exp"])
              for r in rows("a_gospelstate") if int(r["level"]) == 1}
    caps = {}
    for row in rows("a_break"):
        caps.setdefault(int(row["gospel_rare"]), []).append(
            (int(row["break_time"]), int(row["level"])))
    result = {}
    for source in rows("a_gospel"):
        if int(source.get("using_gospel", 0)) != 1:
            continue
        gid = int(source["id"])
        skills = json.loads(source["skill"])
        limits = [limit for _, limit in sorted(caps[int(source["gospel_rare"])])]
        if gid not in starts or len(skills) != 3 or len(set(skills)) != 3 or not limits:
            raise ValueError(f"Gospel {gid} has no complete initial template")
        result[gid] = {
            "id": gid, "level": 1, "exp": starts[gid],
            "level_limit": limits[0], "fake_level": 1,
            "awake_level": 1, "fake_awake_level": 1,
            "active_skills": [{"id": int(s), "level": 1, "valid_level": 1} for s in skills],
            "appearance_id": gid, "appearance_id_2d": gid,
            "energy": int(source.get("work_energy", 100)),
            "fondness": 0, "fondness_level": 0, "fondness_daily_increment": 0,
            "is_fondness_limit_breakthrough": 0, "spirit_breakthrough_num": 0,
            "spirit_level_limit": 0, "spirit_is_ready": 0, "spirit_status": 0,
            "fondness_switch": 0, "spirit_switch": 0, "star_up": 0,
            "is_new": 1, "repeated_num": 0, "skin_uid": 0,
            "rewarded_fondness_levels": [], "break_level_reward_taken": {},
            "unlocked_voice_ids": [], "birth_time": 1,
            "last_work_end_time": 0, "lay_days_on_secretary": 0,
        }
    return result


class GospelFactory:
    def __init__(self, content_root: str | Path):
        root = Path(content_root).resolve()
        names = ("a_gospel", "a_gospelstate", "a_break")
        revisions = tuple((root / (n + ".json")).stat().st_mtime_ns for n in names)
        self.templates = _templates(str(root), revisions)

    @staticmethod
    def needs_repair(source: Mapping[str, Any]) -> bool:
        required = {"level", "level_limit", "awake_level", "active_skills",
                    "fake_level", "fake_awake_level", "is_fondness_limit_breakthrough"}
        return not required.issubset(source) or (
            not source.get("active_skills") and int(source.get("awake_level", 1)) == 1)

    def create(self, gospel_id: int, uid: int) -> dict[str, Any]:
        if type(uid) is not int or not 0 < uid < 2**64:
            raise ValueError("Gospel UID must be a positive uint64")
        if type(gospel_id) is not int or gospel_id not in self.templates:
            raise ValueError("Unknown Gospel acquisition ID")
        row = copy.deepcopy(self.templates[gospel_id])
        row["uid"] = uid
        return row

    def repair_missing(self, source: Mapping[str, Any]) -> dict[str, Any]:
        """Preserve real/fake growth and UID; never add another owned object."""
        base = self.create(int(source["id"]), int(source["uid"]))
        base.update(copy.deepcopy(dict(source)))
        # The old first-acquisition branch omitted skills. Empty was not an
        # intentional skill deletion supported by any gameplay operation.
        if not base.get("active_skills") and int(base.get("awake_level", 1)) == 1:
            base["active_skills"] = copy.deepcopy(self.templates[base["id"]]["active_skills"])
        if "fake_level" not in source:
            base["fake_level"] = int(base.get("level", 1))
        if "fake_awake_level" not in source:
            base["fake_awake_level"] = int(base.get("awake_level", 1))
        return base
