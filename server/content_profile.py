"""Catalog-constrained seeding for the local all-unlocked Test profile.

The generated content directory is the authority for every configuration ID
used here.  Large, uniform pieces of account state are represented as an
immutable catalog baseline plus sparse per-account overrides; they are not
expanded into thousands of repeated save-file entries.
"""

from __future__ import annotations

import hashlib
import json
import struct
from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType
from typing import Any, Dict, Iterable, Optional, Tuple, Union

try:  # Package imports in tests; fallback supports direct server execution.
    from .formation_names import normalize_stored_formation_name
except ImportError:  # pragma: no cover - ``python server/server.py`` compatibility
    from formation_names import normalize_stored_formation_name  # type: ignore


UINT32_MAX = (1 << 32) - 1
TEST_CURRENCY = 1_000_000_000
ALL_UNLOCKED_PROFILE_MODE = "test-all-unlocked"
ALL_UNLOCKED_PROFILE_REVISION = 5
CONTENT_STATE_SCHEMA_VERSION = 3
DEFAULT_CONTENT_DIRECTORY = Path(__file__).resolve().parents[1] / "generated" / "content"

PLAYABLE_HERO_IDS = tuple(range(3_000_001, 3_000_007))
HIDDEN_STORY_HERO_ID = 3_000_000
DEFAULT_PLAYABLE_HERO_ID = 3_000_001
# Hero.SceneId uses a_building.id (or zero for no invitation), as captured
# from DailyInvitationView command 492. location_0..6 are portrait asset
# suffixes derived from Hero.Id, not scene identifiers.
DAILY_CITY_HERO_SCENE_IDS = (0, 1_999_701, 1_999_702, 1_999_703, 1_999_705)
HERO_UID_BASE = 1_000_000_001
GOSPEL_UID_BASE = 2_000_000_001
MAX_GOSPEL_BREAK_LEVEL = 180
MAX_GOSPEL_LEVEL = 183
MAX_GOSPEL_DISPLAY_LEVEL = 63
MAX_GOSPEL_AWAKE_LEVEL = 6
MAX_GOSPEL_SPIRIT_BREAKTHROUGH = 2
MAX_GOSPEL_SPIRIT_LEVEL = 30

# The runtime displays a formal witch's profession tree as seven stages of
# 20 levels.  The final stage is therefore LV140 (three level-limit
# breakthroughs above the ordinary LV100 cap).  These are profile-side
# values: ``Msg.Hero`` itself has no Level field, and the runtime derives the
# visible level from ``Msg.Profession``.  Keeping the values in the durable
# profile is still important for battle/skip payloads and for later
# breakthrough requests.
MAX_HERO_LEVEL = 140
MAX_HERO_BREAKTHROUGH_LEVEL = 3
MAX_HERO_AWAKENING_LEVEL = 5
HERO_PROGRESS_REVISION = 1
GOSPEL_PROGRESS_REVISION = 3

TASK_TABLES = (
    "a_task_main",
    "a_task_daily",
    "a_task_weekly",
    "a_task_activity",
    "a_task_activity_daily",
)

REQUIRED_TABLES = frozenset(
    {
        "a_people",
        "a_initial",
        "a_skill",
        "a_building",
        "a_gospel",
        "a_break",
        "a_gospel_awake",
        "a_gospelascension",
        "a_gospelstate",
        "a_spiritbreak",
        "a_favor_gospel",
        "a_gospel_skin",
        "a_summon",
        "a_summonpool",
        "a_summon_limited",
        "a_skin_trial",
        "a_dungeon",
        "a_chapte",
        "a_task_main",
        "a_task_daily",
        "a_task_weekly",
        "a_task_activity",
        "a_task_activity_daily",
        "a_work_level",
        "a_unlock",
        "a_guide_function",
        "a_plot_review",
        "a_storypack",
    }
)

REQUIRED_FIELDS = {
    "a_people": {"id", "people_display", "job_default", "skill"},
    "a_initial": {"id", "people"},
    "a_skill": {"id", "upgrade_max"},
    "a_gospel": {"id", "using_gospel", "skill", "gospel_rare"},
    "a_break": {"id", "break_time", "gospel_rare", "level"},
    "a_gospel_awake": {"id", "gospel_id", "awaken_level"},
    "a_gospelascension": {"id", "gospel_id", "break_skill_level"},
    "a_gospelstate": {
        "id",
        "gospel_id",
        "level",
        "displaylevel",
        "displaylevel_max",
        "exp",
    },
    "a_spiritbreak": {
        "id",
        "gospel_rare",
        "break_time",
        "level",
        "spirit_level",
        "star_up",
    },
    "a_favor_gospel": {"id", "gospel_id", "level", "exp"},
    "a_gospel_skin": {"id", "gospel_id", "skin_open_time"},
    "a_summon": {
        "id",
        "summonpool_group",
        "summonpool_starttime",
        "summonpool_endtime",
    },
    "a_summonpool": {"id", "pool_group", "gospel_id"},
    "a_building": {"id", "building_type"},
    "a_work_level": {"id", "building_id", "work_level"},
    "a_dungeon": {"id", "belong_chapte", "unlock_dungeon", "unlock_level"},
    "a_storypack": {"id", "storypack_name", "storypack_type"},
    "a_plot_review": {"id", "story_type", "story_group"},
}


class ContentCatalogError(ValueError):
    """Raised when generated content exists but cannot be trusted."""


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _read_json(path: Path, label: str) -> Tuple[Dict[str, Any], bytes]:
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        raise FileNotFoundError(f"missing {label}: {path}") from None
    except OSError as exc:
        raise ContentCatalogError(f"cannot read {label} {path}: {exc}") from exc
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContentCatalogError(f"invalid JSON in {label} {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ContentCatalogError(f"{label} must contain a JSON object: {path}")
    return value, raw


def _integer(value: Any, context: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ContentCatalogError(f"{context} must be an integer")
    return value


def _positive_ids(values: Any, context: str) -> Tuple[int, ...]:
    if not isinstance(values, list):
        raise ContentCatalogError(f"{context} must be a JSON array")
    ids = tuple(_integer(value, context) for value in values)
    if any(value <= 0 for value in ids):
        raise ContentCatalogError(f"{context} contains a non-positive ID")
    if len(ids) != len(set(ids)):
        raise ContentCatalogError(f"{context} contains duplicate IDs")
    return ids


def _id_list_sha256(ids: Iterable[int], context: str) -> str:
    encoded = bytearray()
    for record_id in ids:
        if not -(1 << 31) <= record_id < (1 << 31):
            raise ContentCatalogError(f"{context} ID is outside signed int32")
        encoded.extend(struct.pack("<i", record_id))
    return hashlib.sha256(encoded).hexdigest()


def _parse_active_skill_ids(
    value: Any,
    context: str,
    *,
    reject_ultimate_target_variant: bool = False,
) -> Tuple[int, int, int]:
    if not isinstance(value, str):
        raise ContentCatalogError(f"{context} must be a JSON-array string")
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ContentCatalogError(f"invalid {context}: {exc}") from exc
    if not isinstance(parsed, list) or len(parsed) != 3:
        raise ContentCatalogError(f"{context} must contain exactly three skills")
    skills = tuple(_integer(item, context) for item in parsed)
    if any(skill <= 0 for skill in skills) or len(set(skills)) != 3:
        raise ContentCatalogError(f"{context} contains invalid or duplicate skills")
    if reject_ultimate_target_variant and any(skill % 10 == 4 for skill in skills):
        raise ContentCatalogError(f"{context} contains an ultimate target variant")
    return skills  # type: ignore[return-value]


@dataclass(frozen=True)
class ContentTable:
    """One validated and recursively immutable generated table."""

    name: str
    source_sha256: str
    id_list_sha256: str
    ids: Tuple[int, ...]
    fields: frozenset
    records: Tuple[Mapping[str, Any], ...]
    by_id: Mapping[int, Mapping[str, Any]]

    @property
    def count(self) -> int:
        return len(self.ids)

    def record(self, record_id: int) -> Mapping[str, Any]:
        try:
            return self.by_id[int(record_id)]
        except (KeyError, TypeError, ValueError):
            raise KeyError(f"{record_id!r} is not present in {self.name}") from None


@dataclass(frozen=True)
class ContentCatalog:
    """Validated immutable view of ``generated/content``.

    ``version`` hashes the manifest and every manifest-listed JSON file.  A
    changed runtime catalog therefore produces a migration boundary without
    inventing a server-side content version.
    """

    root: Path
    version: str
    format_version: int
    document: Mapping[str, Any]
    tables: Mapping[str, ContentTable]
    table_counts: Mapping[str, int]
    playable_people_ids: Tuple[int, ...]
    hero_active_skill_ids: Mapping[int, Tuple[int, int, int]]
    usable_gospel_ids: Tuple[int, ...]
    gospel_active_skills: Mapping[int, Tuple[Tuple[int, int], ...]]
    gospel_skin_ids: Tuple[int, ...]
    summon_ids: Tuple[int, ...]
    building_max_levels: Mapping[int, int]
    summon_targets: Mapping[int, Tuple[int, ...]]
    client_content_cutoff: str

    @classmethod
    def load(
        cls,
        path: Optional[Union[str, Path]] = None,
    ) -> "ContentCatalog":
        """Load and validate a catalog directory (or its ``catalog.json``).

        Missing files raise ``FileNotFoundError``.  Malformed, inconsistent,
        unsupported, or path-escaping catalog data raises
        ``ContentCatalogError``; no fallback IDs are synthesized.
        """

        candidate = Path(path) if path is not None else DEFAULT_CONTENT_DIRECTORY
        candidate = candidate.expanduser()
        if candidate.name == "catalog.json":
            catalog_path = candidate.resolve()
            root = catalog_path.parent
        else:
            root = candidate.resolve()
            if not root.exists():
                raise FileNotFoundError(f"content directory does not exist: {root}")
            if not root.is_dir():
                raise NotADirectoryError(f"content path is not a directory: {root}")
            catalog_path = root / "catalog.json"

        catalog, catalog_raw = _read_json(catalog_path, "content catalog")
        format_version = _integer(catalog.get("format_version"), "catalog format_version")
        if format_version <= 0:
            raise ContentCatalogError("catalog format_version must be positive")
        if catalog.get("unsupported_tables") not in ([], None):
            raise ContentCatalogError("catalog contains unsupported tables")
        if catalog.get("unsupported_fields") not in ([], None):
            raise ContentCatalogError("catalog contains unsupported fields")
        provenance = catalog.get("schema_provenance")
        if not isinstance(provenance, dict) or provenance.get("heuristic_ids") is not False:
            raise ContentCatalogError("catalog IDs are not verified static IDs")

        summaries = catalog.get("tables")
        if not isinstance(summaries, list):
            raise ContentCatalogError("catalog tables must be a JSON array")
        if _integer(catalog.get("table_count"), "catalog table_count") != len(summaries):
            raise ContentCatalogError("catalog table_count does not match tables")

        digest = hashlib.sha256()
        digest.update(b"catalog.json\0")
        digest.update(catalog_raw)
        tables: Dict[str, ContentTable] = {}
        files_seen = set()
        for index, summary in enumerate(summaries):
            context = f"catalog tables[{index}]"
            if not isinstance(summary, dict):
                raise ContentCatalogError(f"{context} must be an object")
            name = summary.get("name")
            filename = summary.get("file")
            if not isinstance(name, str) or not name.startswith("a_"):
                raise ContentCatalogError(f"{context} has an invalid table name")
            if name in tables:
                raise ContentCatalogError(f"catalog contains duplicate table {name}")
            if filename != f"{name}.json":
                raise ContentCatalogError(f"{name} has a non-canonical table filename")
            relative = Path(filename)
            table_path = (root / relative).resolve()
            if relative.is_absolute() or table_path.parent != root:
                raise ContentCatalogError(f"{name} table path escapes the content directory")
            if table_path in files_seen:
                raise ContentCatalogError(f"catalog reuses table file {filename}")
            files_seen.add(table_path)

            payload, payload_raw = _read_json(table_path, f"table {name}")
            digest.update(filename.encode("utf-8") + b"\0")
            digest.update(payload_raw)
            if payload.get("table") != name:
                raise ContentCatalogError(f"{name} payload declares a different table")
            if payload.get("format_version") != format_version:
                raise ContentCatalogError(f"{name} format_version does not match catalog")
            if payload.get("unsupported_fields") not in ([], None):
                raise ContentCatalogError(f"{name} contains unsupported fields")
            if summary.get("unsupported_fields") not in ([], None):
                raise ContentCatalogError(f"{name} summary contains unsupported fields")

            ids = _positive_ids(payload.get("ids"), f"{name}.ids")
            count = _integer(payload.get("count"), f"{name}.count")
            summary_count = _integer(summary.get("count"), f"{name} summary count")
            records_value = payload.get("records")
            if not isinstance(records_value, list):
                raise ContentCatalogError(f"{name}.records must be a JSON array")
            if count != summary_count or count != len(ids) or count != len(records_value):
                raise ContentCatalogError(f"{name} count, IDs, and records disagree")

            records = []
            record_ids = []
            for record_index, record in enumerate(records_value):
                if not isinstance(record, dict):
                    raise ContentCatalogError(
                        f"{name}.records[{record_index}] must be an object"
                    )
                record_id = _integer(
                    record.get("id"), f"{name}.records[{record_index}].id"
                )
                record_ids.append(record_id)
                records.append(_freeze(record))
            if tuple(record_ids) != ids:
                raise ContentCatalogError(f"{name} record IDs do not match its verified ID list")

            actual_id_hash = _id_list_sha256(ids, name)
            declared_id_hash = payload.get("id_list_sha256")
            if (
                declared_id_hash != actual_id_hash
                or summary.get("id_list_sha256") != actual_id_hash
            ):
                raise ContentCatalogError(f"{name} ID-list hash mismatch")
            source = payload.get("source")
            source_sha256 = source.get("sha256") if isinstance(source, dict) else None
            if not isinstance(source_sha256, str) or summary.get("source_sha256") != source_sha256:
                raise ContentCatalogError(f"{name} source hash mismatch")

            field_rows = payload.get("fields")
            if not isinstance(field_rows, list):
                raise ContentCatalogError(f"{name}.fields must be a JSON array")
            fields = frozenset(
                row.get("name")
                for row in field_rows
                if isinstance(row, dict) and isinstance(row.get("name"), str)
            )
            missing_fields = REQUIRED_FIELDS.get(name, set()) - fields
            if missing_fields:
                raise ContentCatalogError(
                    f"{name} is missing required fields: {sorted(missing_fields)}"
                )
            frozen_records = tuple(records)
            by_id = MappingProxyType(
                {record_id: record for record_id, record in zip(ids, frozen_records)}
            )
            tables[name] = ContentTable(
                name=name,
                source_sha256=source_sha256,
                id_list_sha256=actual_id_hash,
                ids=ids,
                fields=fields,
                records=frozen_records,
                by_id=by_id,
            )

        missing_tables = REQUIRED_TABLES - set(tables)
        if missing_tables:
            raise ContentCatalogError(
                f"catalog is missing required tables: {sorted(missing_tables)}"
            )

        people = tables["a_people"]
        hidden = people.by_id.get(HIDDEN_STORY_HERO_ID)
        if hidden is None or hidden.get("people_display") != 0:
            raise ContentCatalogError("a_people does not contain hidden story entity 3000000")
        playable_people_ids = tuple(
            int(record["id"])
            for record in people.records
            if record.get("people_display") == 1
        )
        if playable_people_ids != PLAYABLE_HERO_IDS:
            raise ContentCatalogError(
                "a_people playable IDs must be exactly 3000001..3000006"
            )
        initial = tables["a_initial"]
        if initial.count != 1:
            raise ContentCatalogError("a_initial must contain exactly one record")
        initial_people = initial.records[0].get("people")
        try:
            parsed_initial_people = json.loads(initial_people)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ContentCatalogError("a_initial.people is not valid JSON") from exc
        if parsed_initial_people != [DEFAULT_PLAYABLE_HERO_ID]:
            raise ContentCatalogError("a_initial default hero is not 3000001")
        hero_skills = MappingProxyType(
            {
                hero_id: _parse_active_skill_ids(
                    people.record(hero_id).get("skill"),
                    f"a_people[{hero_id}].skill",
                    reject_ultimate_target_variant=True,
                )
                for hero_id in playable_people_ids
            }
        )
        skills = tables["a_skill"]
        for hero_id, skill_ids in hero_skills.items():
            for skill_id in skill_ids:
                skill = skills.by_id.get(skill_id)
                if skill is None:
                    raise ContentCatalogError(
                        f"a_people[{hero_id}] references missing a_skill[{skill_id}]"
                    )
                if skill.get("upgrade_max") != 14:
                    raise ContentCatalogError(
                        f"a_skill[{skill_id}] does not have verified max level 14"
                    )

        gospel = tables["a_gospel"]
        usable_gospel_ids = tuple(
            int(record["id"])
            for record in gospel.records
            if record.get("using_gospel") == 1
        )
        if not usable_gospel_ids:
            raise ContentCatalogError("a_gospel contains no using_gospel=1 records")
        usable_gospel_set = set(usable_gospel_ids)
        gospel_active_skills: Dict[int, Tuple[Tuple[int, int], ...]] = {}
        for gospel_id in usable_gospel_ids:
            skill_ids = _parse_active_skill_ids(
                gospel.record(gospel_id).get("skill"),
                f"a_gospel[{gospel_id}].skill",
            )
            active_skills = []
            for skill_id in skill_ids:
                skill = skills.by_id.get(skill_id)
                if skill is None:
                    raise ContentCatalogError(
                        f"a_gospel[{gospel_id}] references missing a_skill[{skill_id}]"
                    )
                maximum = _integer(
                    skill.get("upgrade_max"), f"a_skill[{skill_id}].upgrade_max"
                )
                if maximum <= 0 or maximum > UINT32_MAX:
                    raise ContentCatalogError(
                        f"a_skill[{skill_id}] has an unsafe upgrade_max"
                    )
                active_skills.append((skill_id, maximum))
            gospel_active_skills[gospel_id] = tuple(active_skills)

        cls._validate_gospel_progression_tables(
            tables,
            gospel,
            usable_gospel_ids,
        )

        skins = tables["a_gospel_skin"]
        for record in skins.records:
            if record.get("gospel_id") not in usable_gospel_set:
                raise ContentCatalogError(
                    f"a_gospel_skin[{record['id']}] targets a non-usable gospel"
                )

        building_max_levels = cls._load_building_max_levels(catalog, tables)
        summon_targets = cls._load_summon_targets(tables, usable_gospel_set)
        client_content_cutoff = cls._load_client_content_cutoff(tables["a_summon"])
        version = "sha256:" + digest.hexdigest()
        immutable_tables = MappingProxyType(dict(tables))
        return cls(
            root=root,
            version=version,
            format_version=format_version,
            document=_freeze(catalog),
            tables=immutable_tables,
            table_counts=MappingProxyType(
                {name: table.count for name, table in immutable_tables.items()}
            ),
            playable_people_ids=playable_people_ids,
            hero_active_skill_ids=hero_skills,
            usable_gospel_ids=usable_gospel_ids,
            gospel_active_skills=MappingProxyType(gospel_active_skills),
            gospel_skin_ids=skins.ids,
            summon_ids=tables["a_summon"].ids,
            building_max_levels=building_max_levels,
            summon_targets=summon_targets,
            client_content_cutoff=client_content_cutoff,
        )

    @staticmethod
    def _validate_gospel_progression_tables(
        tables: Mapping[str, ContentTable],
        gospel_table: ContentTable,
        usable_gospel_ids: Tuple[int, ...],
    ) -> None:
        """Prove the real/display level and awakening caps from runtime tables."""

        expected_break_levels = (30, 70, 120, MAX_GOSPEL_BREAK_LEVEL)
        expected_spirit = (
            (0, 181, 15),
            (1, 182, 20),
            (MAX_GOSPEL_SPIRIT_BREAKTHROUGH, MAX_GOSPEL_LEVEL, MAX_GOSPEL_SPIRIT_LEVEL),
        )
        for rarity in (3, 4, 5):
            break_rows = sorted(
                (
                    row
                    for row in tables["a_break"].records
                    if row.get("gospel_rare") == rarity
                ),
                key=lambda row: int(row["break_time"]),
            )
            if tuple(int(row["break_time"]) for row in break_rows) != (0, 1, 2, 3):
                raise ContentCatalogError(
                    f"a_break rarity {rarity} does not contain stages 0..3"
                )
            if tuple(int(row["level"]) for row in break_rows) != expected_break_levels:
                raise ContentCatalogError(
                    f"a_break rarity {rarity} does not encode 30/70/120/180"
                )

            spirit_rows = sorted(
                (
                    row
                    for row in tables["a_spiritbreak"].records
                    if row.get("gospel_rare") == rarity
                ),
                key=lambda row: int(row["break_time"]),
            )
            observed_spirit = tuple(
                (
                    int(row["break_time"]),
                    int(row["level"]),
                    int(row["spirit_level"]),
                )
                for row in spirit_rows
            )
            if observed_spirit != expected_spirit:
                raise ContentCatalogError(
                    f"a_spiritbreak rarity {rarity} does not encode 181/182/183"
                )
            if sum(int(row["star_up"]) for row in spirit_rows) != 5 - rarity:
                raise ContentCatalogError(
                    f"a_spiritbreak rarity {rarity} has an invalid five-star delta"
                )

        ascension_by_gospel = {
            int(row["gospel_id"]): row
            for row in tables["a_gospelascension"].records
        }
        awake_by_gospel: Dict[int, list[int]] = {}
        for row in tables["a_gospel_awake"].records:
            awake_by_gospel.setdefault(int(row["gospel_id"]), []).append(
                int(row["awaken_level"])
            )
        state_by_gospel: Dict[int, Dict[int, Mapping[str, Any]]] = {}
        for row in tables["a_gospelstate"].records:
            state_by_gospel.setdefault(int(row["gospel_id"]), {})[
                int(row["level"])
            ] = row

        expected_display = {30: 30, 70: 40, 120: 50, 180: 60, 183: 63}
        for gospel_id in usable_gospel_ids:
            rarity = _integer(
                gospel_table.record(gospel_id).get("gospel_rare"),
                f"a_gospel[{gospel_id}].gospel_rare",
            )
            if rarity not in (3, 4, 5):
                raise ContentCatalogError(
                    f"a_gospel[{gospel_id}] has unsupported rarity {rarity}"
                )
            ascension = ascension_by_gospel.get(gospel_id)
            if ascension is None:
                raise ContentCatalogError(
                    f"a_gospelascension is missing usable gospel {gospel_id}"
                )
            try:
                break_skill_levels = tuple(
                    int(value) for value in json.loads(ascension["break_skill_level"])
                )
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ContentCatalogError(
                    f"a_gospelascension[{ascension['id']}] has invalid break_skill_level"
                ) from exc
            if break_skill_levels != (3, 5, 7, 10):
                raise ContentCatalogError(
                    f"a_gospelascension[{ascension['id']}] does not encode 3/5/7/10"
                )
            if tuple(sorted(awake_by_gospel.get(gospel_id, ()))) != tuple(
                range(1, MAX_GOSPEL_AWAKE_LEVEL + 1)
            ):
                raise ContentCatalogError(
                    f"a_gospel_awake gospel {gospel_id} does not contain levels 1..6"
                )
            states = state_by_gospel.get(gospel_id, {})
            for real_level, display_level in expected_display.items():
                state = states.get(real_level)
                if (
                    state is None
                    or int(state["displaylevel"]) != display_level
                    or int(state["displaylevel_max"]) != display_level
                ):
                    raise ContentCatalogError(
                        f"a_gospelstate gospel {gospel_id} has invalid level {real_level}"
                    )
            if int(states[MAX_GOSPEL_LEVEL]["exp"]) <= 0:
                raise ContentCatalogError(
                    f"a_gospelstate gospel {gospel_id} has no max-level experience"
                )

    @staticmethod
    def _load_building_max_levels(
        catalog: Mapping[str, Any],
        tables: Mapping[str, ContentTable],
    ) -> Mapping[int, int]:
        requirements = catalog.get("unlock_requirements")
        if not isinstance(requirements, dict) or requirements.get("heuristic_ids") is not False:
            raise ContentCatalogError("catalog unlock requirements are missing or heuristic")
        rows = requirements.get("building_max_levels")
        if not isinstance(rows, list):
            raise ContentCatalogError("catalog building_max_levels must be an array")
        levels: Dict[int, int] = {}
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                raise ContentCatalogError(f"building_max_levels[{index}] must be an object")
            building_id = _integer(row.get("building_id"), "building_id")
            maximum = _integer(row.get("max_work_level"), "max_work_level")
            if building_id in levels or maximum < 0:
                raise ContentCatalogError("invalid or duplicate building maximum")
            levels[building_id] = maximum
        building_ids = set(tables["a_building"].ids)
        if set(levels) != building_ids:
            raise ContentCatalogError("building_max_levels do not cover a_building exactly")
        observed: Dict[int, int] = {}
        for record in tables["a_work_level"].records:
            building_id = int(record["building_id"])
            work_level = int(record["work_level"])
            observed[building_id] = max(observed.get(building_id, work_level), work_level)
        if observed != levels:
            raise ContentCatalogError("building_max_levels disagree with a_work_level")
        return MappingProxyType(levels)

    @staticmethod
    def _load_summon_targets(
        tables: Mapping[str, ContentTable],
        usable_gospel_ids: set,
    ) -> Mapping[int, Tuple[int, ...]]:
        targets_by_group: Dict[int, list] = {}
        seen_by_group: Dict[int, set] = {}
        for record in tables["a_summonpool"].records:
            group_id = int(record["pool_group"])
            gospel_id = int(record["gospel_id"])
            if gospel_id not in usable_gospel_ids:
                raise ContentCatalogError(
                    f"a_summonpool[{record['id']}] targets unavailable gospel {gospel_id}"
                )
            seen = seen_by_group.setdefault(group_id, set())
            if gospel_id not in seen:
                targets_by_group.setdefault(group_id, []).append(gospel_id)
                seen.add(gospel_id)

        result: Dict[int, Tuple[int, ...]] = {}
        for summon in tables["a_summon"].records:
            summon_id = int(summon["id"])
            group_id = int(summon["summonpool_group"])
            targets = tuple(targets_by_group.get(group_id, ()))
            if not targets:
                raise ContentCatalogError(
                    f"a_summon[{summon_id}] has no client-local summon targets"
                )
            result[summon_id] = targets
        return MappingProxyType(result)

    @staticmethod
    def _load_client_content_cutoff(summon: ContentTable) -> str:
        dates = []
        for record in summon.records:
            value = record.get("summonpool_endtime")
            if not value:
                continue
            if not isinstance(value, str):
                raise ContentCatalogError(
                    f"a_summon[{record['id']}].summonpool_endtime must be text"
                )
            try:
                dates.append(datetime.strptime(value.strip(), "%Y-%m-%d %H:%M"))
            except ValueError as exc:
                raise ContentCatalogError(
                    f"invalid a_summon[{record['id']}].summonpool_endtime: {value!r}"
                ) from exc
        if not dates:
            raise ContentCatalogError("a_summon contains no content cutoff date")
        return max(dates).date().isoformat()

    def table(self, name: str) -> ContentTable:
        try:
            return self.tables[name]
        except KeyError:
            raise KeyError(f"table {name!r} is not present in the content catalog") from None

    def ids_for(self, name: str) -> Tuple[int, ...]:
        return self.table(name).ids

    def record(self, name: str, record_id: int) -> Mapping[str, Any]:
        return self.table(name).record(record_id)

    def summon_target_ids(self, summon_id: int) -> Tuple[int, ...]:
        try:
            return self.summon_targets[int(summon_id)]
        except (KeyError, TypeError, ValueError):
            raise KeyError(f"unknown client-local summon {summon_id!r}") from None

    def state_for(
        self,
        profile: Mapping[str, Any],
        table_name: str,
        record_id: int,
    ) -> Dict[str, Any]:
        return resolve_content_state(profile, self, table_name, record_id)


# These defaults describe visibility and entry state only.  Completion and
# reward claims remain account events and are overlaid from the persisted
# profile.  In particular, story status 0 means Entered, not Finished.
_BASELINE_DEFAULTS = MappingProxyType(
    {
        "a_dungeon": MappingProxyType({"unlocked": True, "completed": False}),
        "a_chapte": MappingProxyType({"unlocked": True, "completed": False}),
        "a_gospel_skin": MappingProxyType({"unlocked": True, "owned": True}),
        "a_summon": MappingProxyType({"unlocked": True, "available": True}),
        "a_task_main": MappingProxyType(
            {"unlocked": True, "status": "available", "reward_claimed": False}
        ),
        "a_task_daily": MappingProxyType(
            {"unlocked": True, "status": "available", "reward_claimed": False}
        ),
        "a_task_weekly": MappingProxyType(
            {"unlocked": True, "status": "available", "reward_claimed": False}
        ),
        "a_task_activity": MappingProxyType(
            {"unlocked": True, "status": "available", "reward_claimed": False}
        ),
        "a_task_activity_daily": MappingProxyType(
            {"unlocked": True, "status": "available", "reward_claimed": False}
        ),
        "a_storypack": MappingProxyType(
            {"unlocked": True, "status": 0, "reward_claimed": False}
        ),
        "a_plot_review": MappingProxyType(
            {"unlocked": True, "status": 0, "reward_claimed": False}
        ),
        "a_unlock": MappingProxyType({"unlocked": True}),
        "a_guide_function": MappingProxyType({"unlocked": True}),
    }
)


@lru_cache(maxsize=1)
def default_content_catalog() -> ContentCatalog:
    """Return the process-wide immutable catalog for the bundled runtime."""

    return ContentCatalog.load(DEFAULT_CONTENT_DIRECTORY)


def _safe_uint(
    value: Any,
    default: int,
    *,
    minimum: int = 0,
    maximum: int = UINT32_MAX,
) -> int:
    if isinstance(value, bool):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    if parsed < minimum or parsed > maximum:
        return default
    return parsed


def _rows_by_id(value: Any, allowed_ids: set) -> Dict[int, MutableMapping[str, Any]]:
    result: Dict[int, MutableMapping[str, Any]] = {}
    if not isinstance(value, list):
        return result
    for row in value:
        if not isinstance(row, MutableMapping):
            continue
        try:
            record_id = int(row.get("id") or 0)
        except (TypeError, ValueError, OverflowError):
            continue
        if record_id in allowed_ids and record_id not in result:
            result[record_id] = row
    return result


def _stable_uids(
    record_ids: Iterable[int],
    existing: Mapping[int, Mapping[str, Any]],
    base: int,
    reserved_uids: Iterable[int] = (),
) -> Dict[int, int]:
    result: Dict[int, int] = {}
    used = set(reserved_uids)
    ordered_ids = tuple(record_ids)
    for record_id in ordered_ids:
        row = existing.get(record_id)
        uid = _safe_uint(row.get("uid") if row else None, 0, minimum=1)
        if uid and uid not in used:
            result[record_id] = uid
            used.add(uid)
    candidate = base
    for record_id in ordered_ids:
        if record_id in result:
            continue
        while candidate in used:
            candidate += 1
        if candidate > UINT32_MAX:
            raise ValueError("catalog entity count exhausted the uint32 UID range")
        result[record_id] = candidate
        used.add(candidate)
        candidate += 1
    return result


def _seed_heroes(
    profile: MutableMapping[str, Any],
    catalog: ContentCatalog,
) -> Dict[int, int]:
    allowed_ids = set(catalog.playable_people_ids)
    original_rows = profile.get("heroes")
    existing = _rows_by_id(original_rows, allowed_ids)

    # Earliest local saves used either hidden 3000000 or a guessed gospel/model
    # ID as the default hero.  Carry mutable fields from that row to the real
    # a_initial default hero if 3000001 is not already present.
    migrated_source: Optional[MutableMapping[str, Any]] = None
    if DEFAULT_PLAYABLE_HERO_ID not in existing and isinstance(original_rows, list):
        for row in original_rows:
            if not isinstance(row, MutableMapping):
                continue
            try:
                old_id = int(row.get("id") or 0)
            except (TypeError, ValueError, OverflowError):
                old_id = 0
            if old_id not in allowed_ids:
                migrated_source = row
                break
        if migrated_source is not None:
            existing[DEFAULT_PLAYABLE_HERO_ID] = migrated_source

    uids = _stable_uids(catalog.playable_people_ids, existing, HERO_UID_BASE)
    old_uid_to_new: Dict[int, int] = {}
    heroes = []
    for hero_id in catalog.playable_people_ids:
        source = existing.get(hero_id)
        row = dict(source) if source is not None else {}
        persisted_scene_id = _safe_uint(
            row.get("scene_id"),
            UINT32_MAX,
        )
        if persisted_scene_id not in DAILY_CITY_HERO_SCENE_IDS:
            persisted_scene_id = 0
        old_uid = _safe_uint(row.get("uid"), 0, minimum=1)
        if old_uid:
            old_uid_to_new[old_uid] = uids[hero_id]
        person = catalog.record("a_people", hero_id)
        active_skills = [
            {"id": skill_id, "level": 14}
            for skill_id in catalog.hero_active_skill_ids[hero_id]
        ]
        row.update(
            {
                "uid": uids[hero_id],
                "id": hero_id,
                "profession_id": int(person["job_default"]),
                "active_skills": active_skills,
                "birth_time": _safe_uint(row.get("birth_time"), 1, minimum=1),
                "has_dated": 1,
                # New/malformed rows start without an invitation.
                # Valid account-owned placement survives every later seed.
                "scene_id": persisted_scene_id,
            }
        )
        row["level"] = _safe_uint(
            row.get("level"),
            MAX_HERO_LEVEL,
            minimum=1,
            maximum=MAX_HERO_LEVEL,
        )
        row["level_limit"] = _safe_uint(
            row.get("level_limit"),
            MAX_HERO_LEVEL,
            minimum=row["level"],
            maximum=MAX_HERO_LEVEL,
        )
        row["fake_level"] = _safe_uint(
            row.get("fake_level"),
            row["level"],
            minimum=1,
            maximum=MAX_HERO_LEVEL,
        )
        row["breakthrough_level"] = _safe_uint(
            row.get("breakthrough_level"),
            MAX_HERO_BREAKTHROUGH_LEVEL,
            maximum=MAX_HERO_BREAKTHROUGH_LEVEL,
        )
        row["awakening_level"] = _safe_uint(
            row.get("awakening_level"),
            MAX_HERO_AWAKENING_LEVEL,
            maximum=MAX_HERO_AWAKENING_LEVEL,
        )
        row["exp"] = _safe_uint(row.get("exp"), 0)
        row["state"] = _safe_uint(row.get("state"), 0)
        appearance_id = _safe_uint(row.get("appearance_id"), hero_id, minimum=1)
        row["appearance_id"] = appearance_id if appearance_id in allowed_ids else hero_id
        row["combat_power"] = _safe_uint(
            row.get("combat_power"), 100_000, minimum=1
        )
        heroes.append(row)

    # Every discarded legacy hero UID is a reference to the migrated default.
    if isinstance(original_rows, list):
        default_uid = uids[DEFAULT_PLAYABLE_HERO_ID]
        for row in original_rows:
            if not isinstance(row, Mapping):
                continue
            old_uid = _safe_uint(row.get("uid"), 0, minimum=1)
            try:
                old_id = int(row.get("id") or 0)
            except (TypeError, ValueError, OverflowError):
                old_id = 0
            if old_uid and old_id not in allowed_ids:
                old_uid_to_new[old_uid] = default_uid

    profile["heroes"] = heroes
    return old_uid_to_new


def _seed_gospels(
    profile: MutableMapping[str, Any],
    catalog: ContentCatalog,
) -> Dict[int, int]:
    allowed_ids = set(catalog.usable_gospel_ids)
    original_rows = profile.get("gospels")
    existing = _rows_by_id(original_rows, allowed_ids)
    hero_uids = {
        int(row["uid"])
        for row in profile.get("heroes", [])
        if isinstance(row, Mapping) and row.get("uid") is not None
    }
    uids = _stable_uids(
        catalog.usable_gospel_ids,
        existing,
        GOSPEL_UID_BASE,
        reserved_uids=hero_uids,
    )
    skins_by_gospel: Dict[int, set] = {}
    skin_model_ids: Dict[int, int] = {}
    for skin in catalog.table("a_gospel_skin").records:
        skin_id = int(skin["id"])
        gospel_id = int(skin["gospel_id"])
        skins_by_gospel.setdefault(gospel_id, set()).add(skin_id)
        try:
            model_id = int(str(skin.get("model_id", "")).strip())
        except (TypeError, ValueError):
            model_id = gospel_id
        skin_model_ids[skin_id] = model_id or gospel_id
    favor_by_gospel: Dict[int, list] = {gospel_id: [] for gospel_id in allowed_ids}
    for favor in catalog.table("a_favor_gospel").records:
        gospel_id = int(favor["gospel_id"])
        if gospel_id in favor_by_gospel:
            favor_by_gospel[gospel_id].append(favor)
    max_state_by_gospel = {
        int(state["gospel_id"]): state
        for state in catalog.table("a_gospelstate").records
        if int(state["level"]) == MAX_GOSPEL_LEVEL
    }

    old_uid_to_new: Dict[int, int] = {}
    gospels = []
    for gospel_id in catalog.usable_gospel_ids:
        source = existing.get(gospel_id)
        row = dict(source) if source is not None else {}
        old_uid = _safe_uint(row.get("uid"), 0, minimum=1)
        if old_uid:
            old_uid_to_new[old_uid] = uids[gospel_id]
        row["uid"] = uids[gospel_id]
        row["id"] = gospel_id
        row["active_skills"] = [
            {"id": skill_id, "level": maximum, "valid_level": maximum}
            for skill_id, maximum in catalog.gospel_active_skills[gospel_id]
        ]
        row["level"] = _safe_uint(
            row.get("level"),
            MAX_GOSPEL_LEVEL,
            minimum=1,
            maximum=MAX_GOSPEL_LEVEL,
        )
        max_state = max_state_by_gospel[gospel_id]
        row["exp"] = _safe_uint(
            row.get("exp"),
            int(max_state["exp"]),
            maximum=UINT32_MAX,
        )
        row["level_limit"] = _safe_uint(
            row.get("level_limit"),
            MAX_GOSPEL_BREAK_LEVEL,
            minimum=1,
            maximum=MAX_GOSPEL_BREAK_LEVEL,
        )
        row["energy"] = _safe_uint(row.get("energy"), 100, maximum=1_000_000)
        allowed_appearances = skins_by_gospel.get(gospel_id, set()) | {gospel_id}
        appearance_id = _safe_uint(row.get("appearance_id"), gospel_id, minimum=1)
        if appearance_id not in allowed_appearances:
            appearance_id = gospel_id
        elif (
            appearance_id != gospel_id
            and skin_model_ids.get(appearance_id, gospel_id) == gospel_id
        ):
            # A 2D-only cosmetic may be present here in profiles produced by
            # older offline builds.  Project it back to the owner's real 3D
            # model on every reload; AppearanceId2D below remains independent.
            appearance_id = gospel_id
        appearance_id_2d = _safe_uint(
            row.get("appearance_id_2d"), appearance_id, minimum=1
        )
        if appearance_id_2d not in allowed_appearances:
            appearance_id_2d = appearance_id
        row["appearance_id"] = appearance_id
        row["appearance_id_2d"] = appearance_id_2d
        row["fake_level"] = _safe_uint(
            row.get("fake_level"),
            row["level"],
            minimum=1,
            maximum=MAX_GOSPEL_LEVEL,
        )
        # FuyingData.Get_skillList indexes a fixed six-element awakening
        # array with (effectiveAwakeLevel - 1).  Zero therefore crashes the
        # warehouse before it can draw a single Gospel row.  The all-unlocked
        # profile owns the completed awakening state, while the wire encoder
        # still clamps defensive/minimal profiles to the legal 1..6 range.
        row["awake_level"] = MAX_GOSPEL_AWAKE_LEVEL
        row["fake_awake_level"] = MAX_GOSPEL_AWAKE_LEVEL
        row["is_new"] = _safe_uint(row.get("is_new"), 0, maximum=1)
        raw_unlocked_voice_ids = row.get("unlocked_voice_ids")
        if isinstance(raw_unlocked_voice_ids, (list, tuple, set, frozenset)):
            row["unlocked_voice_ids"] = sorted(
                {
                    _safe_uint(value, 0, maximum=UINT32_MAX)
                    for value in raw_unlocked_voice_ids
                    if _safe_uint(value, 0, maximum=UINT32_MAX) > 0
                }
            )
        else:
            row["unlocked_voice_ids"] = []
        row["combat_power"] = _safe_uint(
            row.get("combat_power"), 100_000, minimum=1
        )
        favor_rows = favor_by_gospel[gospel_id]
        if not favor_rows:
            raise ContentCatalogError(
                f"a_favor_gospel has no rows for usable gospel {gospel_id}"
            )
        # Once ordinary favor reaches the bond state, the same wire fields are
        # reinterpreted as per-level heart progress.  The final runtime table
        # row is level 30; a zero remainder is a valid completed state because
        # the UI keys its MAX branch from level == SpiritLevelLimit.
        row["fondness"] = 0
        row["fondness_level"] = MAX_GOSPEL_SPIRIT_LEVEL
        row["fondness_daily_increment"] = 0
        row["birth_time"] = _safe_uint(row.get("birth_time"), 1, minimum=1)
        row["is_new"] = 0
        row["is_fondness_limit_breakthrough"] = 1
        row["fondness_switch"] = 1
        row["spirit_switch"] = 1
        row["spirit_status"] = 0
        row["spirit_breakthrough_num"] = MAX_GOSPEL_SPIRIT_BREAKTHROUGH
        row["spirit_level_limit"] = MAX_GOSPEL_SPIRIT_LEVEL
        rarity = int(catalog.record("a_gospel", gospel_id)["gospel_rare"])
        row["star_up"] = 5 - rarity
        row["spirit_is_ready"] = 0
        row["break_level_reward_taken"] = {"1": True, "2": True, "3": True}
        row["rewarded_fondness_levels"] = sorted(
            {
                int(favor["level"])
                for favor in favor_rows
                if int(favor["level"]) > 0
            }
        )
        gospels.append(row)
    profile["gospels"] = gospels
    return old_uid_to_new


def _remap_formation_heroes(
    profile: MutableMapping[str, Any],
    old_uid_to_new: Mapping[int, int],
    *,
    preserve_saved_layout: bool = False,
) -> None:
    del old_uid_to_new  # Entity migration is already reflected in the full roster.
    hero_uids = [int(row["uid"]) for row in profile["heroes"]]
    valid_hero_uids = set(hero_uids)
    combat_power = min(
        UINT32_MAX,
        sum(_safe_uint(row.get("combat_power"), 1, minimum=1) for row in profile["heroes"]),
    )
    phalanxes = profile.get("phalanxes")
    source_rows = phalanxes if isinstance(phalanxes, list) else []
    normalized = []
    for slot in range(5):
        source = source_rows[slot] if slot < len(source_rows) else None
        row = dict(source) if isinstance(source, Mapping) else {}
        name = normalize_stored_formation_name(row.get("name"), slot)
        saved_uids = row.get("hero_uids")
        if preserve_saved_layout and isinstance(saved_uids, list):
            selected_uids = []
            for value in saved_uids:
                uid = _safe_uint(value, 0)
                if uid in valid_hero_uids and uid not in selected_uids:
                    selected_uids.append(uid)
        else:
            selected_uids = []
        if not selected_uids:
            selected_uids = list(hero_uids)
        row.update(
            {
                "id": slot,
                "name": name,
                "hero_uids": selected_uids,
                "combat_power": _safe_uint(
                    row.get("combat_power"), combat_power, minimum=1
                )
                if preserve_saved_layout
                else combat_power,
                "type": _safe_uint(row.get("type"), 0),
            }
        )
        normalized.append(row)
    profile["phalanxes"] = normalized
    current_id = _safe_uint(profile.get("current_phalanx_id"), 0, maximum=4)
    profile["current_phalanx_id"] = current_id if preserve_saved_layout else 0


def _resolve_seed_combat_state(
    profile: MutableMapping[str, Any],
    catalog: ContentCatalog,
    *,
    preserve_saved_layout: bool,
) -> None:
    """Seed one usable loadout and replace guessed display power with derivation."""

    # Local import avoids making the static catalog loader depend on the
    # combat projection while this module itself is being imported.
    try:
        from .combat_attribute_resolver import CombatAttributeResolver
    except ImportError:  # pragma: no cover - direct server execution
        from combat_attribute_resolver import CombatAttributeResolver  # type: ignore

    resolver = CombatAttributeResolver(catalog)
    profile_view = dict(profile)
    profile_view["all_content_unlocked"] = True

    for hero in profile.get("heroes", []):
        if isinstance(hero, MutableMapping):
            hero["combat_power"] = resolver.resolve_hero(
                hero,
                all_unlocked=True,
            ).combat_power
    for gospel in profile.get("gospels", []):
        if isinstance(gospel, MutableMapping):
            gospel["combat_power"] = resolver.resolve_gospel(gospel, profile).combat_power

    ranked_uids = resolver.strongest_gospel_uids(profile_view, 18)
    phalanxes = profile.get("phalanxes")
    if not isinstance(phalanxes, list):
        return
    for phalanx in phalanxes:
        if not isinstance(phalanx, MutableMapping):
            continue
        hero_uids = [
            _safe_uint(value, 0, minimum=1)
            for value in phalanx.get("hero_uids", [])
        ]
        if not preserve_saved_layout:
            phalanx["combat_positions"] = {
                str(uid): index if index < 3 else -1
                for index, uid in enumerate(hero_uids)
            }
            equipped: Dict[str, list[Dict[str, int]]] = {}
            for index, uid in enumerate(hero_uids):
                selected = ranked_uids[index * 3 : index * 3 + 3]
                equipped[str(uid)] = [
                    {"gospel_uid": gospel_uid, "appearance_id": 0}
                    for gospel_uid in selected
                ]
            phalanx["equipped_gospels"] = equipped
        phalanx["combat_power"] = resolver.resolve_formation(
            profile_view,
            phalanx,
        ).combat_power


def _seed_buildings(profile: MutableMapping[str, Any], catalog: ContentCatalog) -> None:
    existing = profile.get("buildings")
    existing = existing if isinstance(existing, Mapping) else {}
    buildings = {}
    for building_id, maximum in catalog.building_max_levels.items():
        source = existing.get(str(building_id), existing.get(building_id))
        if isinstance(source, Mapping):
            row = dict(source)
        elif source is not None:
            row = {"level": source}
        else:
            row = {}
        row.update({"id": building_id, "level": maximum, "work_level": maximum})
        buildings[str(building_id)] = row
    profile["buildings"] = buildings


def _baseline_descriptors(catalog: ContentCatalog) -> Dict[str, Dict[str, Any]]:
    result: Dict[str, Dict[str, Any]] = {}
    for table_name, defaults in _BASELINE_DEFAULTS.items():
        table = catalog.table(table_name)
        descriptor: Dict[str, Any] = {
            "table": table_name,
            "count": table.count,
            "id_list_sha256": table.id_list_sha256,
            "default": dict(defaults),
        }
        if table_name == "a_summon":
            descriptor.update(
                {
                    "target_table": "a_summonpool",
                    "target_record_count": catalog.table("a_summonpool").count,
                    "target_gospel_count": len(
                        {
                            gospel_id
                            for targets in catalog.summon_targets.values()
                            for gospel_id in targets
                        }
                    ),
                    "target_policy": "client-catalog-only",
                }
            )
        result[table_name] = descriptor
    return result


def _seed_scalar_defaults(
    profile: MutableMapping[str, Any],
    previous_defaults: Any,
) -> Dict[str, int]:
    desired = {
        "level": 100,
        "exp": 1_666_400,
        "energy": 160,
        "diamond": TEST_CURRENCY,
        "gold": TEST_CURRENCY,
        "minidiamond": TEST_CURRENCY,
    }
    previous = previous_defaults if isinstance(previous_defaults, Mapping) else None
    for key, default in desired.items():
        if previous is None or key not in profile or profile.get(key) == previous.get(key):
            profile[key] = default
        else:
            # Values changed after a prior seed are account overrides.  Keep
            # them as long as they still fit the protocol's uint32 fields.
            profile[key] = _safe_uint(profile.get(key), default)
    return desired


def _migrate_hero_progress(profile: MutableMapping[str, Any]) -> bool:
    """Raise the initial all-unlocked roster to the verified max state once.

    Early local profiles were created before the profession tree was sent
    correctly and consequently retained level 1/100 values.  Do not repair
    those values on every request: a later runtime-side level change is an
    account override.  A dedicated revision makes this migration both
    idempotent and independent from formation/content migrations.
    """

    current = _safe_uint(profile.get("hero_progress_revision"), 0)
    if current >= HERO_PROGRESS_REVISION:
        return False

    rows = profile.get("heroes")
    if isinstance(rows, list):
        valid_ids = set(PLAYABLE_HERO_IDS) | {HIDDEN_STORY_HERO_ID}
        for row in rows:
            if not isinstance(row, MutableMapping):
                continue
            try:
                hero_id = int(row.get("id") or 0)
            except (TypeError, ValueError, OverflowError):
                continue
            if hero_id not in valid_ids:
                continue
            row["level"] = MAX_HERO_LEVEL
            row["level_limit"] = MAX_HERO_LEVEL
            row["fake_level"] = MAX_HERO_LEVEL
            row["breakthrough_level"] = MAX_HERO_BREAKTHROUGH_LEVEL
            row["awakening_level"] = MAX_HERO_AWAKENING_LEVEL

    profile["hero_progress_revision"] = HERO_PROGRESS_REVISION
    return True


def _migrate_gospel_progress(
    profile: MutableMapping[str, Any],
    catalog: ContentCatalog,
) -> bool:
    """Raise every owned star trace to verified breakthrough/heart maxima once."""

    current = _safe_uint(profile.get("gospel_progress_revision"), 0)
    if current >= GOSPEL_PROGRESS_REVISION:
        return False

    rows = profile.get("gospels")
    if isinstance(rows, list):
        max_states = {
            int(state["gospel_id"]): state
            for state in catalog.table("a_gospelstate").records
            if int(state["level"]) == MAX_GOSPEL_LEVEL
        }
        for row in rows:
            if not isinstance(row, MutableMapping):
                continue
            gospel_id = _safe_uint(row.get("id"), 0, minimum=1)
            if gospel_id not in max_states:
                continue
            rarity = int(catalog.record("a_gospel", gospel_id)["gospel_rare"])
            row["level"] = MAX_GOSPEL_LEVEL
            row["exp"] = int(max_states[gospel_id]["exp"])
            row["level_limit"] = MAX_GOSPEL_BREAK_LEVEL
            row["fake_level"] = MAX_GOSPEL_LEVEL
            row["awake_level"] = MAX_GOSPEL_AWAKE_LEVEL
            row["fake_awake_level"] = MAX_GOSPEL_AWAKE_LEVEL
            row["active_skills"] = [
                {"id": skill_id, "level": maximum, "valid_level": maximum}
                for skill_id, maximum in catalog.gospel_active_skills[gospel_id]
            ]
            row["fondness"] = 0
            row["fondness_level"] = MAX_GOSPEL_SPIRIT_LEVEL
            row["is_fondness_limit_breakthrough"] = 1
            row["fondness_switch"] = 1
            row["spirit_switch"] = 1
            row["spirit_status"] = 0
            row["spirit_breakthrough_num"] = MAX_GOSPEL_SPIRIT_BREAKTHROUGH
            row["spirit_level_limit"] = MAX_GOSPEL_SPIRIT_LEVEL
            row["star_up"] = 5 - rarity
            row["spirit_is_ready"] = 0
            row["break_level_reward_taken"] = {"1": True, "2": True, "3": True}

    profile["gospel_progress_revision"] = GOSPEL_PROGRESS_REVISION
    return True


def seed_all_unlocked_profile(
    profile: MutableMapping[str, Any],
    catalog: Optional[Union[ContentCatalog, str, Path]] = None,
) -> MutableMapping[str, Any]:
    """Seed or migrate one all-unlocked Test profile in place.

    ``role_id``, ``role_name``, event histories, completion records, reward
    claims, and sparse mutable overrides are retained.  Reapplying the same
    seed is a value-level no-op.  Catalog/schema changes add newly verified
    entities and refresh seed-owned metadata without resetting account events.
    """

    if not isinstance(profile, MutableMapping):
        raise TypeError("profile must be a mutable mapping")
    if catalog is None:
        content = default_content_catalog()
    elif isinstance(catalog, ContentCatalog):
        content = catalog
    else:
        content = ContentCatalog.load(catalog)

    preserve_saved_layout = (
        profile.get("profile_mode") == ALL_UNLOCKED_PROFILE_MODE
        and _safe_uint(profile.get("profile_revision"), 0)
        >= 4
    )
    previous_state = profile.get("content_state")
    state = dict(previous_state) if isinstance(previous_state, Mapping) else {}
    desired_scalars = _seed_scalar_defaults(profile, state.get("seed_defaults"))

    hero_uid_remap = _seed_heroes(profile, content)
    _migrate_hero_progress(profile)
    _seed_gospels(profile, content)
    _migrate_gospel_progress(profile, content)
    _remap_formation_heroes(
        profile,
        hero_uid_remap,
        preserve_saved_layout=preserve_saved_layout,
    )
    _resolve_seed_combat_state(
        profile,
        content,
        preserve_saved_layout=preserve_saved_layout,
    )
    _seed_buildings(profile, content)

    # These fields are sparse event/override stores.  Never populate them with
    # all catalog IDs: unlocked visibility is represented by the baseline.
    for key, empty in (
        ("unlocked_dungeons", []),
        ("completed_dungeons", []),
        ("tasks", {}),
        ("stage_tasks", {}),
        ("plots", {}),
        ("plot_rewards", {}),
        ("story_progress", {}),
        ("story_words", {}),
        ("gospel_skins", {}),
        ("gospel_skin_status", {}),
        ("card_pool_groups", {}),
        ("card_pools", {}),
    ):
        if key not in profile:
            profile[key] = empty

    # The activity lottery's defined cost table uses ResourceType 1,
    # ItemId 0 as the exact fallback balance.  Keep this namespace separate
    # from item inventory and seed it only once so later spending persists.
    resource_balances = profile.get("resource_balances")
    if not isinstance(resource_balances, Mapping):
        resource_balances = {}
    else:
        resource_balances = dict(resource_balances)
    resource_balances.setdefault("1:0", TEST_CURRENCY)
    profile["resource_balances"] = resource_balances

    overrides = state.get("overrides")
    if not isinstance(overrides, MutableMapping):
        overrides = dict(overrides) if isinstance(overrides, Mapping) else {}
    state.update(
        {
            "schema_version": CONTENT_STATE_SCHEMA_VERSION,
            "catalog_version": content.version,
            "client_content_cutoff": content.client_content_cutoff,
            "baselines": _baseline_descriptors(content),
            "overrides": overrides,
            "seed_defaults": desired_scalars,
        }
    )
    profile["content_state"] = state

    profile["profile_mode"] = ALL_UNLOCKED_PROFILE_MODE
    profile["profile_revision"] = max(
        _safe_uint(profile.get("profile_revision"), 0),
        ALL_UNLOCKED_PROFILE_REVISION,
    )
    profile["content_catalog_version"] = content.version
    profile["client_content_cutoff"] = content.client_content_cutoff
    profile["all_content_unlocked"] = True
    profile["guide_step"] = max(_safe_uint(profile.get("guide_step"), 0), 4)
    profile["tutorial_completed"] = True
    return profile


def _mapping_override(container: Any, record_id: int) -> Optional[Any]:
    if not isinstance(container, Mapping):
        return None
    if str(record_id) in container:
        return container[str(record_id)]
    return container.get(record_id)


def _apply_override(state: Dict[str, Any], override: Any) -> None:
    if isinstance(override, Mapping):
        state.update(override)


def resolve_content_state(
    profile: Mapping[str, Any],
    catalog: ContentCatalog,
    table_name: str,
    record_id: int,
) -> Dict[str, Any]:
    """Resolve one static baseline with legacy and canonical account overrides."""

    if not profile.get("all_content_unlocked"):
        raise ValueError("profile is not an all-unlocked profile")
    if table_name not in _BASELINE_DEFAULTS:
        raise KeyError(f"{table_name!r} has no all-unlocked state baseline")
    record_id = int(record_id)
    catalog.record(table_name, record_id)  # Explicitly reject unknown IDs.
    state = dict(_BASELINE_DEFAULTS[table_name])

    if table_name == "a_dungeon":
        completed = profile.get("completed_dungeons")
        if isinstance(completed, list) and any(
            _safe_uint(value, 0, minimum=1) == record_id for value in completed
        ):
            state["completed"] = True
    elif table_name in ("a_storypack", "a_plot_review"):
        progress = _mapping_override(profile.get("story_progress"), record_id)
        if progress is not None:
            if isinstance(progress, Mapping):
                _apply_override(state, progress)
            else:
                state["status"] = _safe_uint(progress, 0, maximum=1)
        _apply_override(state, _mapping_override(profile.get("plots"), record_id))
        _apply_override(state, _mapping_override(profile.get("plot_rewards"), record_id))
    elif table_name in TASK_TABLES:
        _apply_override(state, _mapping_override(profile.get("tasks"), record_id))
        _apply_override(state, _mapping_override(profile.get("stage_tasks"), record_id))
    elif table_name == "a_gospel_skin":
        _apply_override(state, _mapping_override(profile.get("gospel_skins"), record_id))
        _apply_override(
            state, _mapping_override(profile.get("gospel_skin_status"), record_id)
        )
    elif table_name == "a_summon":
        _apply_override(state, _mapping_override(profile.get("card_pools"), record_id))
    elif table_name == "a_unlock":
        values = profile.get("unlocked_features")
        if isinstance(values, list) and record_id in values:
            state["unlocked"] = True
    elif table_name == "a_guide_function":
        values = profile.get("unlocked_guides")
        if isinstance(values, list) and record_id in values:
            state["unlocked"] = True

    content_state = profile.get("content_state")
    overrides = content_state.get("overrides") if isinstance(content_state, Mapping) else None
    table_overrides = overrides.get(table_name) if isinstance(overrides, Mapping) else None
    _apply_override(state, _mapping_override(table_overrides, record_id))
    return state


__all__ = [
    "ALL_UNLOCKED_PROFILE_MODE",
    "ALL_UNLOCKED_PROFILE_REVISION",
    "CONTENT_STATE_SCHEMA_VERSION",
    "ContentCatalog",
    "ContentCatalogError",
    "ContentTable",
    "DAILY_CITY_HERO_SCENE_IDS",
    "DEFAULT_CONTENT_DIRECTORY",
    "DEFAULT_PLAYABLE_HERO_ID",
    "GOSPEL_PROGRESS_REVISION",
    "HERO_PROGRESS_REVISION",
    "HIDDEN_STORY_HERO_ID",
    "MAX_HERO_AWAKENING_LEVEL",
    "MAX_HERO_BREAKTHROUGH_LEVEL",
    "MAX_HERO_LEVEL",
    "MAX_GOSPEL_BREAK_LEVEL",
    "MAX_GOSPEL_DISPLAY_LEVEL",
    "MAX_GOSPEL_LEVEL",
    "MAX_GOSPEL_AWAKE_LEVEL",
    "MAX_GOSPEL_SPIRIT_BREAKTHROUGH",
    "MAX_GOSPEL_SPIRIT_LEVEL",
    "PLAYABLE_HERO_IDS",
    "TASK_TABLES",
    "TEST_CURRENCY",
    "UINT32_MAX",
    "default_content_catalog",
    "resolve_content_state",
    "seed_all_unlocked_profile",
]
