"""Table-backed, atomic progression for the runtime's witch/hero commands.

The shipped witch screen derives its displayed level from
``Msg.Profession.TalentList`` and ``a_job_skill.skill_value``.  Its real learn
button sends ``CS_UnlockProfessionTalent`` with ``HeroUId == 0`` and only a
``TalentId`` on the wire; the owning witch is therefore resolved from the
talent's profession.  The seven 20-node groups are the screen's seven
``breakthrough`` stages, and each click cost is defined from
``a_job_skill.open_item``.  Completing a group's twentieth node advances
``SC_UnlockProfessionTalentRet.TalentTreeId`` to the next group; the runtime
uses that response field as the actual stage unlock.  The separate tree-reward
request only grants the configured completion reward.

The legacy ``CS_HeroEnhance``/``CS_HeroBreakthrough`` paths are distinct: the
current runtime has no active breakthrough sender and the defined content has
no closed legacy Hero-level, breakthrough-cost, or witch-awakening table.  We
conservatively reject those operations instead of borrowing profession costs.
This module never emits a sparse ``Notify_UpdateHero`` which could replace
unrelated Hero fields with defaults.

Every successful operation returns a top-level ``changes`` mapping suitable
for ``PersistentStateStore.transact_account``'s shallow ``profile.update``
commit.  Economy projections are preflighted in detached copies, so an error
never returns a partial inventory, currency, reward-claim, or hero mutation.
"""

from __future__ import annotations

import copy
import json
import os
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import MappingProxyType
from typing import Any, Dict, Optional, Tuple, Union

try:  # Package imports in tests; fallbacks support direct server execution.
    from .content_profile import ContentCatalog, default_content_catalog
    from .content_responses import ProtobufDecodeError, iter_wire_fields
    from .inventory_content import (
        InventoryContentError,
        make_update_item_packet,
        normalize_inventory,
        stable_item_uid,
    )
    from .protobuf_codec import DEFAULT_PROTOCOL_CATALOG, ProtocolMessageCodec
except ImportError:  # pragma: no cover - ``python server/server.py``
    from content_profile import (  # type: ignore
        ContentCatalog,
        default_content_catalog,
    )
    from content_responses import (  # type: ignore
        ProtobufDecodeError,
        iter_wire_fields,
    )
    from inventory_content import (  # type: ignore
        InventoryContentError,
        make_update_item_packet,
        normalize_inventory,
        stable_item_uid,
    )
    from protobuf_codec import (  # type: ignore
        DEFAULT_PROTOCOL_CATALOG,
        ProtocolMessageCodec,
    )


CS_HERO_BREAKTHROUGH = 35
SC_HERO_BREAKTHROUGH_RET = 36
CS_HERO_ENHANCE = 51
SC_HERO_ENHANCE_RET = 52
CS_HERO_SKILL_ENHANCE = 329
SC_HERO_SKILL_ENHANCE_RET = 330
CS_SWITCH_PROFESSION = 465
SC_SWITCH_PROFESSION_RET = 466
CS_UNLOCK_PROFESSION_TALENT = 494
SC_UNLOCK_PROFESSION_TALENT_RET = 495
CS_FONDNESS_LEVEL_UP = 496
SC_FONDNESS_LEVEL_UP_RET = 497
CS_TAKE_HERO_FONDNESS_REWARD = 705
SC_TAKE_HERO_FONDNESS_REWARD_RET = 706
CS_TAKE_PROFESSION_TALENT_TREE_REWARD = 887
SC_TAKE_PROFESSION_TALENT_TREE_REWARD_RET = 888

NOTIFY_HERO_FONDNESS_LEVEL_UP = 79
NOTIFY_UPDATE_PROFESSION = 461
NOTIFY_PROFESSION_LEVEL_UP = 462
NOTIFY_UPDATE_HERO_SKILL = 516

RESPONSE_IDS = MappingProxyType(
    {
        CS_HERO_BREAKTHROUGH: SC_HERO_BREAKTHROUGH_RET,
        CS_HERO_ENHANCE: SC_HERO_ENHANCE_RET,
        CS_HERO_SKILL_ENHANCE: SC_HERO_SKILL_ENHANCE_RET,
        CS_SWITCH_PROFESSION: SC_SWITCH_PROFESSION_RET,
        CS_UNLOCK_PROFESSION_TALENT: SC_UNLOCK_PROFESSION_TALENT_RET,
        CS_FONDNESS_LEVEL_UP: SC_FONDNESS_LEVEL_UP_RET,
        CS_TAKE_HERO_FONDNESS_REWARD: (SC_TAKE_HERO_FONDNESS_REWARD_RET),
        CS_TAKE_PROFESSION_TALENT_TREE_REWARD: (
            SC_TAKE_PROFESSION_TALENT_TREE_REWARD_RET
        ),
    }
)

REQUEST_MESSAGES = MappingProxyType(
    {
        CS_HERO_BREAKTHROUGH: "CS_HeroBreakthrough",
        CS_HERO_ENHANCE: "CS_HeroEnhance",
        CS_HERO_SKILL_ENHANCE: "CS_HeroSkillEnhance",
        CS_SWITCH_PROFESSION: "CS_SwitchProfession",
        CS_UNLOCK_PROFESSION_TALENT: "CS_UnlockProfessionTalent",
        CS_FONDNESS_LEVEL_UP: "CS_FondnessLevelUp",
        CS_TAKE_HERO_FONDNESS_REWARD: "CS_TakeHeroFondnessReward",
        CS_TAKE_PROFESSION_TALENT_TREE_REWARD: ("CS_TakeProfessionTalentTreeReward"),
    }
)

RESPONSE_MESSAGES = MappingProxyType(
    {
        CS_HERO_BREAKTHROUGH: "SC_HeroBreakthroughRet",
        CS_HERO_ENHANCE: "SC_HeroEnhanceRet",
        CS_HERO_SKILL_ENHANCE: "SC_HeroSkillEnhanceRet",
        CS_SWITCH_PROFESSION: "SC_SwitchProfessionRet",
        CS_UNLOCK_PROFESSION_TALENT: "SC_UnlockProfessionTalentRet",
        CS_FONDNESS_LEVEL_UP: "SC_FondnessLevelUpRet",
        # The misspelling is part of the shipped protocol and command 706.
        CS_TAKE_HERO_FONDNESS_REWARD: "SC_TakeHeroFondnessRwardRet",
        CS_TAKE_PROFESSION_TALENT_TREE_REWARD: ("SC_TakeProfessionTalentTreeRewardRet"),
    }
)

# Literal Msg.ErrorCode members defined from runtime assembly.
ERROR_SUCCESS = 0
ERROR_ROLE_ID_ILLEGAL = 104
ERROR_GOLD_NOT_ENOUGH = 108
ERROR_ITEM_NUM_NOT_ENOUGH = 110
ERROR_ALREADY_HIGHEST_LIMIT_LEVEL = 112
ERROR_ALREADY_REACH_LIMIT_LEVEL = 114
ERROR_MATERIAL_ILLEGAL = 117
ERROR_HERO_LEVEL_NOT_ENOUGH = 118
ERROR_NO_REWARD_CAN_BE_TAKEN = 122
ERROR_FONDNESS_REWARD_ALREADY_TAKEN = 186
ERROR_FONDNESS_LEVEL_NOT_ENOUGH = 187
ERROR_SKILL_ALREADY_HIGHEST_LEVEL = 190
ERROR_SKILL_NOT_FOUND = 191
ERROR_HERO_FONDNESS_LEVEL_UP_FAILED = 238
ERROR_TALENT_TREE_ALREADY_REWARDED = 325
ERROR_NOT_ALL_TALENT_UNLOCKED_IN_TREE = 326
ERROR_PROFESSION_TALENT_ALREADY_UNLOCKED = 332
ERROR_TALENT_TREE_ILLEGAL = 333
ERROR_FRONT_TALENT_NOT_UNLOCKED = 334
ERROR_PROFESSION_LEVEL_NOT_ENOUGH = 335
ERROR_HERO_NOT_FOUND = 516
ERROR_CLIENT_REQUEST_PARAM = 32_000
ERROR_MESSAGE_DATA_ILLEGAL = 32_769
ERROR_DATA_REPEATED = 32_771
ERROR_CONFIG = 32_772
ERROR_EXCEED_ITEM_LIMIT_COUNT = 429

RESOURCE_DIAMOND = 0
RESOURCE_MINI_DIAMOND = 1
RESOURCE_GOLD = 2
RESOURCE_ENERGY = 3
RESOURCE_ITEM = 4
RESOURCE_EXP = 5

RESOURCE_TOP_LEVEL_FIELDS = MappingProxyType(
    {
        RESOURCE_DIAMOND: "diamond",
        RESOURCE_MINI_DIAMOND: "minidiamond",
        RESOURCE_GOLD: "gold",
        RESOURCE_ENERGY: "energy",
        RESOURCE_EXP: "exp",
    }
)

NO_PROFESSION_ID = 3_900_000
LILIA_HERO_ID = 3_000_000
UINT32_MAX = (1 << 32) - 1
UINT64_MAX = (1 << 64) - 1

DEFAULT_HANDLER_DEPENDENCIES = (
    Path(__file__).resolve().parents[1]
    / "generated"
    / "protocol"
    / "handler_dependencies.json"
)

PROFESSION_PROJECTION_FIELDS = (
    "profession_level",
    "profession_exp",
    "profession_level_limit",
    "profession_breakthrough_level",
    "profession_talent_ids",
    "profession_unlocked_tree_ids",
    "profession_talent_tree_id",
    "rewarded_profession_talent_tree_ids",
)


class HeroProgressionCatalogError(ValueError):
    """Raised when the defined content tables do not form a closed model."""


class HeroProtocolContractError(ValueError):
    """Raised when command/schema/handler validation has drifted."""


class HeroProfileError(ValueError):
    """Raised when persisted hero state cannot be projected safely."""


class HeroActionError(ValueError):
    """A validated domain failure with its wire-level error code."""

    def __init__(self, error_code: int, message: str) -> None:
        super().__init__(message)
        self.error_code = int(error_code)


class HeroEconomyError(HeroActionError):
    """An economy preflight failure which must roll back the whole action."""


@dataclass(frozen=True)
class HeroSkillDefinition:
    skill_id: int
    max_level: int


@dataclass(frozen=True)
class FavorLevelDefinition:
    level: int
    cumulative_exp: int
    rewards: Tuple[Mapping[str, int], ...]


@dataclass(frozen=True)
class ProfessionTalentDefinition:
    talent_id: int
    group_id: int
    number: int
    skill_value: int
    costs: Tuple[Mapping[str, int], ...]


@dataclass(frozen=True)
class ProfessionGroupDefinition:
    group_id: int
    profession_id: int
    stage: int
    talents: Tuple[ProfessionTalentDefinition, ...]
    rewards: Tuple[Mapping[str, int], ...]
    level_cap: int


@dataclass(frozen=True)
class ProfessionDefinition:
    profession_id: int
    repeat_type: int
    groups: Tuple[ProfessionGroupDefinition, ...]
    talents: Tuple[ProfessionTalentDefinition, ...]
    talent_by_id: Mapping[int, ProfessionTalentDefinition]
    group_by_id: Mapping[int, ProfessionGroupDefinition]
    level_caps: Tuple[int, ...]

    @property
    def max_level(self) -> int:
        return self.level_caps[-1] if self.level_caps else 1


@dataclass(frozen=True)
class HeroDefinition:
    hero_id: int
    displayed: bool
    default_profession_id: int
    active_skills: Tuple[HeroSkillDefinition, ...]
    favor_levels: Mapping[int, FavorLevelDefinition]

    @property
    def max_fondness_level(self) -> int:
        return max(self.favor_levels, default=0)


@dataclass(frozen=True)
class ProfessionProgress:
    profession_id: int
    talent_ids: Tuple[int, ...]
    unlocked_tree_ids: Tuple[int, ...]
    rewarded_tree_ids: Tuple[int, ...]
    current_tree_id: int
    level: int
    level_limit: int


@dataclass(frozen=True)
class HeroProgressionResult:
    response_id: int
    body: bytes
    changes: Dict[str, Any] = field(default_factory=dict)
    notifications: Tuple[Tuple[int, bytes], ...] = ()
    error_code: int = ERROR_SUCCESS
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.error_code == ERROR_SUCCESS


def _coerce_catalog(
    value: Optional[Union[ContentCatalog, str, Path]],
) -> ContentCatalog:
    if value is None:
        return default_content_catalog()
    if isinstance(value, ContentCatalog):
        return value
    return ContentCatalog.load(value)


def _strict_int(
    value: Any,
    label: str,
    *,
    minimum: int = 0,
    maximum: int = UINT64_MAX,
) -> int:
    if isinstance(value, bool):
        raise HeroProgressionCatalogError(f"{label} must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise HeroProgressionCatalogError(f"{label} must be an integer") from exc
    if parsed < minimum or parsed > maximum:
        raise HeroProgressionCatalogError(f"{label} is outside {minimum}..{maximum}")
    return parsed


def _json_value(value: Any, label: str) -> Any:
    if not isinstance(value, str):
        raise HeroProgressionCatalogError(f"{label} must be a JSON string")
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        raise HeroProgressionCatalogError(f"{label} is invalid JSON") from exc


def _resource_records(
    value: Any,
    label: str,
    *,
    allow_empty_string: bool = True,
) -> Tuple[Mapping[str, int], ...]:
    if value == "" and allow_empty_string:
        return ()
    parsed = _json_value(value, label)
    if not isinstance(parsed, list):
        raise HeroProgressionCatalogError(f"{label} must be a JSON array")
    records = []
    for index, raw in enumerate(parsed):
        context = f"{label}[{index}]"
        if not isinstance(raw, Mapping):
            raise HeroProgressionCatalogError(f"{context} must be an object")
        if set(raw) != {"type", "id", "num"}:
            raise HeroProgressionCatalogError(
                f"{context} must contain only type/id/num"
            )
        resource_type = _strict_int(
            raw.get("type"), f"{context}.type", maximum=UINT32_MAX
        )
        resource_id = _strict_int(raw.get("id"), f"{context}.id", maximum=UINT32_MAX)
        amount = _strict_int(
            raw.get("num"),
            f"{context}.num",
            minimum=1,
            maximum=UINT32_MAX,
        )
        records.append(
            MappingProxyType({"type": resource_type, "id": resource_id, "num": amount})
        )
    return tuple(records)


def _json_id_list(value: Any, label: str) -> Tuple[int, ...]:
    parsed = _json_value(value, label)
    if not isinstance(parsed, list):
        raise HeroProgressionCatalogError(f"{label} must be a JSON array")
    result = tuple(
        _strict_int(item, f"{label}[{index}]", minimum=1, maximum=UINT32_MAX)
        for index, item in enumerate(parsed)
    )
    if len(set(result)) != len(result):
        raise HeroProgressionCatalogError(f"{label} contains duplicate IDs")
    return result


class HeroProgressionCatalog:
    """Closed projection of the seven tables used by witch progression."""

    def __init__(self, catalog: ContentCatalog) -> None:
        self.content_catalog = catalog
        required = {
            "a_people",
            "a_skill",
            "a_job",
            "a_job_skillgroup",
            "a_job_skill",
            "a_favor",
            "a_item",
        }
        missing = sorted(name for name in required if name not in catalog.tables)
        if missing:
            raise HeroProgressionCatalogError(
                f"content catalog is missing tables: {missing}"
            )

        item_rows = catalog.table("a_item").records
        item_ids = {int(row["id"]) for row in item_rows}
        self.item_max_stacks = MappingProxyType(
            {
                int(row["id"]): _strict_int(
                    row.get("max_stack_num"),
                    f"a_item[{row['id']}].max_stack_num",
                    maximum=UINT64_MAX,
                )
                for row in item_rows
            }
        )

        def validate_resources(
            resources: Iterable[Mapping[str, int]], label: str
        ) -> None:
            for resource in resources:
                resource_type = int(resource["type"])
                resource_id = int(resource["id"])
                if resource_type == RESOURCE_ITEM:
                    if resource_id not in item_ids:
                        raise HeroProgressionCatalogError(
                            f"{label} references missing a_item[{resource_id}]"
                        )
                elif resource_type not in RESOURCE_TOP_LEVEL_FIELDS or resource_id != 0:
                    raise HeroProgressionCatalogError(
                        f"{label} has unsupported resource "
                        f"{resource_type}:{resource_id}"
                    )

        job_rows = {int(row["id"]): row for row in catalog.table("a_job").records}
        if NO_PROFESSION_ID not in job_rows:
            raise HeroProgressionCatalogError(
                f"a_job is missing no-profession sentinel {NO_PROFESSION_ID}"
            )
        group_rows_by_job: Dict[int, list[Mapping[str, Any]]] = {}
        for row in catalog.table("a_job_skillgroup").records:
            job_id = int(row["job_id"])
            if job_id not in job_rows or job_id == NO_PROFESSION_ID:
                raise HeroProgressionCatalogError(
                    f"a_job_skillgroup[{row['id']}] has illegal job {job_id}"
                )
            group_rows_by_job.setdefault(job_id, []).append(row)

        talent_rows_by_group: Dict[int, list[Mapping[str, Any]]] = {}
        for row in catalog.table("a_job_skill").records:
            talent_rows_by_group.setdefault(int(row["skillgroup_id"]), []).append(row)

        professions: Dict[int, ProfessionDefinition] = {}
        used_group_ids: set[int] = set()
        used_talent_ids: set[int] = set()
        for job_id, job_row in sorted(job_rows.items()):
            repeat_type = _strict_int(
                job_row.get("repeat_type"),
                f"a_job[{job_id}].repeat_type",
                maximum=UINT32_MAX,
            )
            if job_id == NO_PROFESSION_ID:
                professions[job_id] = ProfessionDefinition(
                    job_id,
                    repeat_type,
                    (),
                    (),
                    MappingProxyType({}),
                    MappingProxyType({}),
                    (),
                )
                continue

            raw_groups = sorted(
                group_rows_by_job.get(job_id, []),
                key=lambda row: (int(row["job_level"]), int(row["id"])),
            )
            if not raw_groups:
                raise HeroProgressionCatalogError(
                    f"a_job[{job_id}] has no a_job_skillgroup rows"
                )
            observed_stages = tuple(int(row["job_level"]) for row in raw_groups)
            expected_stages = tuple(range(1, len(raw_groups) + 1))
            if observed_stages != expected_stages:
                raise HeroProgressionCatalogError(
                    f"a_job[{job_id}] stages are not contiguous: {observed_stages}"
                )

            groups: list[ProfessionGroupDefinition] = []
            flattened: list[ProfessionTalentDefinition] = []
            cumulative_level = 0
            for raw_group in raw_groups:
                group_id = int(raw_group["id"])
                used_group_ids.add(group_id)
                raw_talents = sorted(
                    talent_rows_by_group.get(group_id, []),
                    key=lambda row: (int(row["number"]), int(row["id"])),
                )
                if not raw_talents:
                    raise HeroProgressionCatalogError(
                        f"a_job_skillgroup[{group_id}] has no talents"
                    )
                observed_numbers = tuple(int(row["number"]) for row in raw_talents)
                expected_numbers = tuple(range(1, len(raw_talents) + 1))
                if observed_numbers != expected_numbers:
                    raise HeroProgressionCatalogError(
                        f"a_job_skillgroup[{group_id}] numbers are not contiguous"
                    )
                talents = []
                for raw_talent in raw_talents:
                    talent_id = int(raw_talent["id"])
                    used_talent_ids.add(talent_id)
                    skill_value = _strict_int(
                        raw_talent.get("skill_value"),
                        f"a_job_skill[{talent_id}].skill_value",
                        minimum=1,
                        maximum=UINT32_MAX,
                    )
                    # The shipped GetProfessionLevel fallback sums this field.
                    # All defined nodes are one level; rejecting a drift keeps
                    # the persisted prefix representation unambiguous.
                    if skill_value != 1:
                        raise HeroProgressionCatalogError(
                            f"a_job_skill[{talent_id}] skill_value is not 1"
                        )
                    costs = _resource_records(
                        raw_talent.get("open_item"),
                        f"a_job_skill[{talent_id}].open_item",
                        allow_empty_string=False,
                    )
                    validate_resources(costs, f"a_job_skill[{talent_id}].open_item")
                    talent = ProfessionTalentDefinition(
                        talent_id=talent_id,
                        group_id=group_id,
                        number=int(raw_talent["number"]),
                        skill_value=skill_value,
                        costs=costs,
                    )
                    talents.append(talent)
                    flattened.append(talent)
                    cumulative_level += skill_value
                rewards = _resource_records(
                    raw_group.get("reward"),
                    f"a_job_skillgroup[{group_id}].reward",
                )
                validate_resources(rewards, f"a_job_skillgroup[{group_id}].reward")
                groups.append(
                    ProfessionGroupDefinition(
                        group_id=group_id,
                        profession_id=job_id,
                        stage=int(raw_group["job_level"]),
                        talents=tuple(talents),
                        rewards=rewards,
                        level_cap=cumulative_level,
                    )
                )

            # a_job_skill.next_skill proves one linear 140-node chain.  The
            # first node of a newly unlocked tree is reached only after the
            # previous tree's final node.
            raw_by_id = {
                int(row["id"]): row
                for group in raw_groups
                for row in talent_rows_by_group[int(group["id"])]
            }
            for index, talent in enumerate(flattened):
                raw_next = raw_by_id[talent.talent_id].get("next_skill")
                expected = (
                    (flattened[index + 1].talent_id,)
                    if index + 1 < len(flattened)
                    else ()
                )
                actual = (
                    ()
                    if raw_next == ""
                    else _json_id_list(
                        raw_next, f"a_job_skill[{talent.talent_id}].next_skill"
                    )
                )
                if actual != expected:
                    raise HeroProgressionCatalogError(
                        f"a_job_skill[{talent.talent_id}] next_skill is "
                        f"{actual}, expected {expected}"
                    )

            professions[job_id] = ProfessionDefinition(
                profession_id=job_id,
                repeat_type=repeat_type,
                groups=tuple(groups),
                talents=tuple(flattened),
                talent_by_id=MappingProxyType(
                    {talent.talent_id: talent for talent in flattened}
                ),
                group_by_id=MappingProxyType(
                    {group.group_id: group for group in groups}
                ),
                level_caps=tuple(group.level_cap for group in groups),
            )

        all_group_ids = {
            int(row["id"]) for row in catalog.table("a_job_skillgroup").records
        }
        all_talent_ids = {
            int(row["id"]) for row in catalog.table("a_job_skill").records
        }
        if used_group_ids != all_group_ids or used_talent_ids != all_talent_ids:
            raise HeroProgressionCatalogError(
                "a_job_skillgroup/a_job_skill contains unprojected rows"
            )
        self.professions = MappingProxyType(professions)
        self.groups = MappingProxyType(
            {
                group.group_id: group
                for profession in professions.values()
                for group in profession.groups
            }
        )
        self.talents = MappingProxyType(
            {
                talent.talent_id: talent
                for profession in professions.values()
                for talent in profession.talents
            }
        )

        skill_rows = {int(row["id"]): row for row in catalog.table("a_skill").records}
        favor_by_hero: Dict[int, Dict[int, FavorLevelDefinition]] = {}
        for raw in catalog.table("a_favor").records:
            hero_id = int(raw["people_id"])
            level = _strict_int(
                raw.get("level"),
                f"a_favor[{raw['id']}].level",
                maximum=UINT32_MAX,
            )
            rewards = _resource_records(
                raw.get("reward"), f"a_favor[{raw['id']}].reward"
            )
            validate_resources(rewards, f"a_favor[{raw['id']}].reward")
            levels = favor_by_hero.setdefault(hero_id, {})
            if level in levels:
                raise HeroProgressionCatalogError(
                    f"a_favor has duplicate hero {hero_id} level {level}"
                )
            levels[level] = FavorLevelDefinition(
                level=level,
                cumulative_exp=_strict_int(
                    raw.get("exp"),
                    f"a_favor[{raw['id']}].exp",
                    maximum=UINT32_MAX,
                ),
                rewards=rewards,
            )

        heroes: Dict[int, HeroDefinition] = {}
        for person in catalog.table("a_people").records:
            hero_id = int(person["id"])
            skill_ids = _json_id_list(person.get("skill"), f"a_people[{hero_id}].skill")
            active_skills = []
            for skill_id in skill_ids:
                raw_skill = skill_rows.get(skill_id)
                if raw_skill is None:
                    raise HeroProgressionCatalogError(
                        f"a_people[{hero_id}] references missing a_skill[{skill_id}]"
                    )
                # Hero skill requests have no material field.  For all seven
                # defined witches, a_skill explicitly carries an empty
                # enhance_Item and a verified scalar max of 14.
                if raw_skill.get("enhance_Item") != "":
                    raise HeroProgressionCatalogError(
                        f"a_skill[{skill_id}] has unsupported Hero costs"
                    )
                active_skills.append(
                    HeroSkillDefinition(
                        skill_id=skill_id,
                        max_level=_strict_int(
                            raw_skill.get("upgrade_max"),
                            f"a_skill[{skill_id}].upgrade_max",
                            minimum=1,
                            maximum=UINT32_MAX,
                        ),
                    )
                )
            levels = favor_by_hero.get(hero_id, {})
            if levels:
                observed = tuple(sorted(levels))
                expected = tuple(range(0, max(levels) + 1))
                if observed != expected:
                    raise HeroProgressionCatalogError(
                        f"a_favor hero {hero_id} levels are not contiguous"
                    )
                cumulative = tuple(levels[level].cumulative_exp for level in observed)
                if cumulative != tuple(sorted(cumulative)):
                    raise HeroProgressionCatalogError(
                        f"a_favor hero {hero_id} EXP is not monotonic"
                    )
            displayed = int(person.get("people_display", 0)) == 1
            if displayed and not levels:
                raise HeroProgressionCatalogError(
                    f"displayed a_people[{hero_id}] has no a_favor rows"
                )
            default_job = int(person["job_default"])
            if default_job not in professions:
                raise HeroProgressionCatalogError(
                    f"a_people[{hero_id}] has unknown default job {default_job}"
                )
            heroes[hero_id] = HeroDefinition(
                hero_id=hero_id,
                displayed=displayed,
                default_profession_id=default_job,
                active_skills=tuple(active_skills),
                favor_levels=MappingProxyType(dict(sorted(levels.items()))),
            )
        self.heroes = MappingProxyType(heroes)


# Msg.Hero intentionally has no Level/Exp/LevelLimit field.  Keeping its real
# field set in the contract validator prevents a future sparse "level Hero"
# notification from silently becoming a destructive full-entity replacement.
HERO_MESSAGE_FIELDS = frozenset(
    {
        "HeroUId",
        "HeroId",
        "Fondness",
        "FondnessLevel",
        "FondnessDailyIncrement",
        "ActiveSkillList",
        "State",
        "PlatesFoodPropList",
        "AppearanceId",
        "BirthTime",
        "Skinuid",
        "FeedUnpalatableNum",
        "ProfessionId",
        "SceneId",
        "Feeling",
        "FeelingDailyIncrement",
        "DailyDialogNum",
        "FondnessFakeLevel",
        "SpecialStatus",
        "DailyFeedNum",
        "Attr",
        "DailyFondnessMap",
        "CombatPower",
        "DialogNum",
        "EatenFoodMap",
        "RewardedPlotEventMap",
        "RewardedFondnessPlotEventMap",
        "FondnessSwitch",
        "SpecialStatusEnterTime",
        "HasDated",
        "InviteDatingTime",
        "DatingTimeLong",
        "DatingTimeSpeed",
        "DatingPlotPkgId",
        "Mood",
        "DecorationPresetsId",
        "RunePresetsId",
    }
)

EXPECTED_MESSAGE_FIELDS = MappingProxyType(
    {
        "CS_HeroBreakthrough": {"RoleId", "HeroUId", "ItemList"},
        "SC_HeroBreakthroughRet": {
            "Errorcode",
            "RoleId",
            "HeroUId",
            "ItemList",
            "LevelLimit",
        },
        "CS_HeroEnhance": {"RoleId", "HeroUId", "ItemList"},
        "SC_HeroEnhanceRet": {
            "Errorcode",
            "RoleId",
            "HeroUId",
            "ItemList",
        },
        "CS_HeroSkillEnhance": {"RoleId", "HeroUId", "SkillId"},
        "SC_HeroSkillEnhanceRet": {
            "Errorcode",
            "RoleId",
            "HeroUId",
            "SkillId",
            "NewSkillId",
        },
        "CS_SwitchProfession": {"HeroUId1", "HeroUId2", "ProfessionId"},
        "SC_SwitchProfessionRet": {
            "Errorcode",
            "HeroUId1",
            "HeroUId2",
            "Hero1ProfessionId",
            "Hero2ProfessionId",
        },
        "CS_UnlockProfessionTalent": {"HeroUId", "TalentId"},
        "SC_UnlockProfessionTalentRet": {
            "Errorcode",
            "HeroUId",
            "ProfessionId",
            "TalentTreeId",
            "Talent",
        },
        "CS_FondnessLevelUp": {"HeroUId"},
        "SC_FondnessLevelUpRet": {"Errorcode", "HeroUId", "Level"},
        "CS_TakeHeroFondnessReward": {"HeroUId", "FondnessLevel"},
        "SC_TakeHeroFondnessRwardRet": {
            "Errorcode",
            "HeroUId",
            "FondnessLevel",
            "RewardList",
        },
        "CS_TakeProfessionTalentTreeReward": {"TalentTreeId"},
        "SC_TakeProfessionTalentTreeRewardRet": {
            "Errorcode",
            "TalentTreeId",
            "RewardList",
        },
        "Notify_UpdateProfession": {"ProfData"},
        "Notify_ProfessionLevelUp": {
            "RoleId",
            "ProfessionId",
            "Level",
            "Exp",
        },
        "Notify_UpdateHeroSkill": {"HeroUId", "ActiveSkillList"},
        "Notify_HeroFondnessLevelUp": {
            "RoleId",
            "HeroUId",
            "FondnessLevel",
            "Fondness",
            "DailyFondnessMap",
        },
        "Hero": HERO_MESSAGE_FIELDS,
    }
)


def _validate_protocol_contract(
    codec: ProtocolMessageCodec,
    protocol_catalog: Path,
    handler_dependencies: Path,
) -> None:
    for message_name, expected in EXPECTED_MESSAGE_FIELDS.items():
        schema = codec.schema(message_name)
        actual = {str(item.get("name")) for item in schema.get("fields", [])}
        if actual != set(expected):
            raise HeroProtocolContractError(
                f"{message_name} fields changed: expected {sorted(expected)}, "
                f"got {sorted(actual)}"
            )

    try:
        protocol = json.loads(protocol_catalog.read_text(encoding="utf-8"))
        dependencies = json.loads(handler_dependencies.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise HeroProtocolContractError(
            f"cannot read generated protocol metadata: {exc}"
        ) from exc

    response_map = protocol.get("request_to_response")
    if not isinstance(response_map, Mapping):
        raise HeroProtocolContractError("protocol request_to_response is missing")
    for request_id, response_id in RESPONSE_IDS.items():
        if int(response_map.get(str(request_id), -1)) != response_id:
            raise HeroProtocolContractError(
                f"protocol maps request {request_id} to an unexpected response"
            )

    endpoints = {
        int(item.get("request_id")): item
        for item in dependencies.get("endpoints", [])
        if isinstance(item, Mapping) and isinstance(item.get("request_id"), int)
    }
    for request_id, response_id in RESPONSE_IDS.items():
        endpoint = endpoints.get(request_id)
        if endpoint is None or int(endpoint.get("response_id", -1)) != response_id:
            raise HeroProtocolContractError(
                f"handler dependencies are missing endpoint "
                f"{request_id} -> {response_id}"
            )
        if endpoint.get("request_name") != REQUEST_MESSAGES[request_id]:
            raise HeroProtocolContractError(
                f"handler request name changed for endpoint {request_id}"
            )
        if endpoint.get("response_name") != RESPONSE_MESSAGES[request_id]:
            raise HeroProtocolContractError(
                f"handler response name changed for endpoint {request_id}"
            )
        known_fields = EXPECTED_MESSAGE_FIELDS[RESPONSE_MESSAGES[request_id]]
        for field_info in endpoint.get("response_fields_read", []):
            if str(field_info.get("field")) not in known_fields:
                raise HeroProtocolContractError(
                    f"handler for {request_id} reads unknown field "
                    f"{field_info.get('field')}"
                )


def _wire_field_map(data: bytes) -> Dict[int, list[Tuple[int, object]]]:
    result: Dict[int, list[Tuple[int, object]]] = {}
    for number, wire_type, value in iter_wire_fields(bytes(data)):
        result.setdefault(number, []).append((wire_type, value))
    return result


def _required_uint(
    fields: Mapping[int, list[Tuple[int, object]]],
    number: int,
    *,
    minimum: int = 0,
    maximum: int = UINT64_MAX,
) -> int:
    values = fields.get(number, [])
    if len(values) != 1 or values[0][0] != 0:
        raise ProtobufDecodeError(f"field {number} must be one varint")
    parsed = int(values[0][1])
    if parsed < minimum or parsed > maximum:
        raise ProtobufDecodeError(f"field {number} is outside {minimum}..{maximum}")
    return parsed


def _optional_uint(
    fields: Mapping[int, list[Tuple[int, object]]],
    number: int,
    *,
    default: int = 0,
    minimum: int = 0,
    maximum: int = UINT64_MAX,
) -> int:
    values = fields.get(number, [])
    if not values:
        return default
    if len(values) != 1 or values[0][0] != 0:
        raise ProtobufDecodeError(f"field {number} must be at most one varint")
    parsed = int(values[0][1])
    if parsed < minimum or parsed > maximum:
        raise ProtobufDecodeError(f"field {number} is outside {minimum}..{maximum}")
    return parsed


def _materials(
    fields: Mapping[int, list[Tuple[int, object]]], number: int
) -> Tuple[Mapping[str, int], ...]:
    result = []
    for wire_type, raw in fields.get(number, []):
        if wire_type != 2 or not isinstance(raw, bytes):
            raise ProtobufDecodeError(f"field {number} is not a material message")
        nested = _wire_field_map(raw)
        if set(nested) - {1, 2}:
            raise ProtobufDecodeError(
                f"field {number} material contains unknown fields"
            )
        result.append(
            MappingProxyType(
                {
                    "ItemUId": _required_uint(nested, 1, minimum=1, maximum=UINT64_MAX),
                    "ItemNum": _required_uint(nested, 2, minimum=1, maximum=UINT32_MAX),
                }
            )
        )
    return tuple(result)


def _profile_uint(
    source: Mapping[str, Any],
    key: str,
    default: int,
    *,
    minimum: int = 0,
    maximum: int = UINT64_MAX,
) -> int:
    if key not in source:
        return int(default)
    value = source.get(key)
    if isinstance(value, bool):
        raise HeroProfileError(f"Hero {key} must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise HeroProfileError(f"Hero {key} must be an integer") from exc
    if parsed < minimum or parsed > maximum:
        raise HeroProfileError(f"Hero {key} is outside {minimum}..{maximum}")
    return parsed


def _profile_uint_list(
    source: Mapping[str, Any], key: str
) -> Optional[Tuple[int, ...]]:
    if key not in source:
        return None
    value = source.get(key)
    if not isinstance(value, list):
        raise HeroProfileError(f"Hero {key} must be a list")
    result = []
    for index, item in enumerate(value):
        if isinstance(item, bool):
            raise HeroProfileError(f"Hero {key}[{index}] must be an integer")
        try:
            parsed = int(item)
        except (TypeError, ValueError, OverflowError) as exc:
            raise HeroProfileError(f"Hero {key}[{index}] must be an integer") from exc
        if parsed <= 0 or parsed > UINT32_MAX:
            raise HeroProfileError(f"Hero {key}[{index}] is outside 1..{UINT32_MAX}")
        result.append(parsed)
    if len(set(result)) != len(result):
        raise HeroProfileError(f"Hero {key} contains duplicates")
    return tuple(result)


class HeroProgressionLayer:
    """Pure-state implementation of the eight witch progression endpoints."""

    def __init__(
        self,
        catalog: Optional[Union[ContentCatalog, str, Path]] = None,
        *,
        codec: Optional[ProtocolMessageCodec] = None,
        protocol_catalog: Optional[Path] = None,
        handler_dependencies: Optional[Path] = None,
    ) -> None:
        self.content_catalog = _coerce_catalog(catalog)
        self.catalog = HeroProgressionCatalog(self.content_catalog)
        configured_protocol = os.environ.get("MTE_PROTOCOL_CATALOG")
        protocol_path = Path(
            protocol_catalog or configured_protocol or DEFAULT_PROTOCOL_CATALOG
        ).resolve()
        dependency_path = Path(
            handler_dependencies
            or os.environ.get("MTE_HANDLER_DEPENDENCIES")
            or DEFAULT_HANDLER_DEPENDENCIES
        ).resolve()
        self.codec = codec or ProtocolMessageCodec(protocol_path)
        _validate_protocol_contract(self.codec, protocol_path, dependency_path)

    @staticmethod
    def _role_id(profile: Mapping[str, Any]) -> int:
        try:
            value = int(profile.get("role_id") or 0)
        except (TypeError, ValueError, OverflowError):
            return 0
        return value if 0 <= value <= UINT64_MAX else 0

    @staticmethod
    def _profession_id(row: Optional[Mapping[str, Any]]) -> int:
        if not isinstance(row, Mapping):
            return 0
        try:
            value = int(row.get("profession_id") or NO_PROFESSION_ID)
        except (TypeError, ValueError, OverflowError):
            return 0
        return value if 0 <= value <= UINT32_MAX else 0

    def _owned_hero(
        self, profile: Mapping[str, Any], uid: int
    ) -> Tuple[
        list[Any],
        list[Any],
        int,
        Dict[str, Any],
        HeroDefinition,
    ]:
        source_rows = profile.get("heroes")
        if not isinstance(source_rows, list):
            raise HeroProfileError("profile.heroes must be a list")
        matches = []
        for index, source in enumerate(source_rows):
            if not isinstance(source, Mapping):
                continue
            try:
                candidate = int(source.get("uid") or 0)
            except (TypeError, ValueError, OverflowError):
                continue
            if candidate == uid:
                matches.append(index)
        if not matches:
            raise LookupError(f"Hero UID {uid} is not owned")
        if len(matches) != 1:
            raise HeroProfileError(f"Hero UID {uid} is duplicated")
        index = matches[0]
        source = source_rows[index]
        assert isinstance(source, Mapping)
        hero_id = _profile_uint(source, "id", 0, minimum=1, maximum=UINT32_MAX)
        definition = self.catalog.heroes.get(hero_id)
        if definition is None:
            raise HeroProfileError(
                f"Hero UID {uid} has unsupported a_people ID {hero_id}"
            )
        original = copy.deepcopy(source_rows)
        rows = copy.deepcopy(source_rows)
        row = dict(rows[index])
        rows[index] = row
        return original, rows, index, row, definition

    def _profession_progress(
        self,
        row: Mapping[str, Any],
        hero: HeroDefinition,
        *,
        all_unlocked: bool = False,
    ) -> Tuple[ProfessionDefinition, ProfessionProgress]:
        # Lillia is a real owned a_people row, but her job_default is the
        # no-profession sentinel while job/job_id reference Ritsu's table.
        # No a_favor rows exist for her.  Preserve ownership and refuse to
        # synthesize a seventh formal profession projection.
        if hero.hero_id == LILIA_HERO_ID or not hero.displayed:
            raise HeroActionError(
                ERROR_TALENT_TREE_ILLEGAL,
                "Lillia has no closed formal profession progression table",
            )
        profession_id = _profile_uint(
            row,
            "profession_id",
            NO_PROFESSION_ID,
            maximum=UINT32_MAX,
        )
        profession = self.catalog.professions.get(profession_id)
        if profession is None or profession_id == NO_PROFESSION_ID:
            raise HeroActionError(
                ERROR_TALENT_TREE_ILLEGAL,
                f"Hero profession {profession_id} has no talent tree",
            )

        explicit_talents = _profile_uint_list(row, "profession_talent_ids")
        if explicit_talents is None:
            level = _profile_uint(
                row,
                "profession_level",
                profession.max_level if all_unlocked else 0,
                maximum=profession.max_level,
            )
            talent_ids = tuple(
                talent.talent_id for talent in profession.talents[:level]
            )
        else:
            expected_prefix = tuple(
                talent.talent_id
                for talent in profession.talents[: len(explicit_talents)]
            )
            if explicit_talents != expected_prefix:
                raise HeroProfileError(
                    "Hero profession_talent_ids is not a verified chain prefix"
                )
            talent_ids = explicit_talents
            level = len(talent_ids)
            if (
                "profession_level" in row
                and _profile_uint(
                    row,
                    "profession_level",
                    level,
                    maximum=profession.max_level,
                )
                != level
            ):
                raise HeroProfileError(
                    "Hero profession_level disagrees with profession_talent_ids"
                )

        explicit_trees = _profile_uint_list(row, "profession_unlocked_tree_ids")
        group_ids = tuple(group.group_id for group in profession.groups)
        if explicit_trees is None:
            if "profession_level_limit" in row:
                level_limit = _profile_uint(
                    row,
                    "profession_level_limit",
                    profession.level_caps[0],
                    minimum=1,
                    maximum=profession.max_level,
                )
                if level_limit not in profession.level_caps:
                    raise HeroProfileError(
                        f"Hero profession_level_limit {level_limit} is not table-backed"
                    )
                tree_count = profession.level_caps.index(level_limit) + 1
            elif all_unlocked:
                tree_count = len(profession.groups)
            else:
                tree_count = 1
                for index, cap in enumerate(profession.level_caps, 1):
                    if level <= cap:
                        tree_count = index
                        break
            unlocked_tree_ids = group_ids[:tree_count]
        else:
            expected_prefix = group_ids[: len(explicit_trees)]
            if not explicit_trees or explicit_trees != expected_prefix:
                raise HeroProfileError(
                    "Hero profession_unlocked_tree_ids is not a non-empty prefix"
                )
            unlocked_tree_ids = explicit_trees
            level_limit = profession.groups[len(unlocked_tree_ids) - 1].level_cap
            if (
                "profession_level_limit" in row
                and _profile_uint(
                    row,
                    "profession_level_limit",
                    level_limit,
                    minimum=1,
                    maximum=profession.max_level,
                )
                != level_limit
            ):
                raise HeroProfileError(
                    "Hero profession_level_limit disagrees with unlocked "
                    "profession trees"
                )
        level_limit = profession.groups[len(unlocked_tree_ids) - 1].level_cap
        if level > level_limit:
            raise HeroProfileError(
                f"Hero level {level} exceeds profession cap {level_limit}"
            )

        current_tree_id = _profile_uint(
            row,
            "profession_talent_tree_id",
            unlocked_tree_ids[-1],
            minimum=1,
            maximum=UINT32_MAX,
        )
        if current_tree_id not in unlocked_tree_ids:
            raise HeroProfileError("Hero profession_talent_tree_id is not unlocked")
        explicit_rewards = _profile_uint_list(
            row, "rewarded_profession_talent_tree_ids"
        )
        rewarded = explicit_rewards or ()
        if not set(rewarded).issubset(set(group_ids)):
            raise HeroProfileError(
                "Hero rewarded_profession_talent_tree_ids contains another job"
            )
        return profession, ProfessionProgress(
            profession_id=profession_id,
            talent_ids=talent_ids,
            unlocked_tree_ids=unlocked_tree_ids,
            rewarded_tree_ids=rewarded,
            current_tree_id=current_tree_id,
            level=level,
            level_limit=level_limit,
        )

    @staticmethod
    def _write_progress(
        row: Dict[str, Any],
        updated: ProfessionProgress,
    ) -> None:
        row["profession_level"] = updated.level
        row["profession_level_limit"] = updated.level_limit
        row["profession_breakthrough_level"] = max(
            0, len(updated.unlocked_tree_ids) - 1
        )
        row["profession_talent_ids"] = list(updated.talent_ids)
        row["profession_unlocked_tree_ids"] = list(updated.unlocked_tree_ids)
        row["profession_talent_tree_id"] = updated.current_tree_id
        row["rewarded_profession_talent_tree_ids"] = list(updated.rewarded_tree_ids)

    @staticmethod
    def _active_skill_values(row: Mapping[str, Any]) -> list[Dict[str, int]]:
        raw = row.get("active_skills", row.get("skills", []))
        if not isinstance(raw, list):
            raise HeroProfileError("Hero active_skills must be a list")
        result = []
        seen = set()
        for index, skill in enumerate(raw):
            if not isinstance(skill, Mapping):
                raise HeroProfileError(f"Hero active_skills[{index}] must be an object")
            skill_id = _profile_uint(skill, "id", 0, minimum=1, maximum=UINT32_MAX)
            if skill_id in seen:
                raise HeroProfileError(f"Hero active skill {skill_id} is duplicated")
            seen.add(skill_id)
            level = _profile_uint(skill, "level", 1, minimum=1, maximum=UINT32_MAX)
            valid_level = _profile_uint(
                skill,
                "valid_level",
                level,
                minimum=1,
                maximum=UINT32_MAX,
            )
            result.append({"Id": skill_id, "Level": level, "ValidLevel": valid_level})
        return result

    @staticmethod
    def _reward_values(
        resources: Iterable[Mapping[str, int]],
    ) -> list[Dict[str, int]]:
        return [
            {
                "ResType": int(resource["type"]),
                "ItemId": int(resource["id"]),
                "Count": int(resource["num"]),
            }
            for resource in resources
        ]

    @staticmethod
    def _profession_message(
        profession: ProfessionDefinition,
        progress: ProfessionProgress,
    ) -> Dict[str, Any]:
        last_talent = progress.talent_ids[-1] if progress.talent_ids else 0
        return {
            "Id": profession.profession_id,
            "TalentTreeId": progress.current_tree_id,
            "TalentList": [
                {"Id": talent_id, "PropList": []} for talent_id in progress.talent_ids
            ],
            "TalentTree": {
                "TalentTreeId": progress.current_tree_id,
                "TalentId": last_talent,
            },
            # The runtime dereferences this singular message without a null
            # guard when calculating tower attributes.
            "IndependentTree": {},
            "UnlockedTreeIdList": list(progress.unlocked_tree_ids),
        }

    def _profession_notifications(
        self,
        profile: Mapping[str, Any],
        profession: ProfessionDefinition,
        progress: ProfessionProgress,
    ) -> Tuple[Tuple[int, bytes], ...]:
        return (
            (
                NOTIFY_UPDATE_PROFESSION,
                self.codec.encode(
                    "Notify_UpdateProfession",
                    {"ProfData": self._profession_message(profession, progress)},
                ),
            ),
            (
                NOTIFY_PROFESSION_LEVEL_UP,
                self.codec.encode(
                    "Notify_ProfessionLevelUp",
                    {
                        "RoleId": self._role_id(profile),
                        "ProfessionId": profession.profession_id,
                        "Level": progress.level,
                        "Exp": 0,
                    },
                ),
            ),
        )

    def _skill_notification(self, row: Mapping[str, Any]) -> Tuple[int, bytes]:
        return (
            NOTIFY_UPDATE_HERO_SKILL,
            self.codec.encode(
                "Notify_UpdateHeroSkill",
                {
                    "HeroUId": int(row["uid"]),
                    "ActiveSkillList": self._active_skill_values(row),
                },
            ),
        )

    def _fondness_notification(
        self, profile: Mapping[str, Any], row: Mapping[str, Any]
    ) -> Tuple[int, bytes]:
        daily_value = row.get("daily_fondness_map", {})
        if not isinstance(daily_value, Mapping):
            raise HeroProfileError("Hero daily_fondness_map must be a mapping")
        daily: Dict[int, int] = {}
        for raw_key, raw_value in daily_value.items():
            try:
                key = int(raw_key)
                value = int(raw_value)
            except (TypeError, ValueError, OverflowError) as exc:
                raise HeroProfileError(
                    "Hero daily_fondness_map must contain integers"
                ) from exc
            if key <= 0 or key > UINT32_MAX or value < 0 or value > UINT32_MAX:
                raise HeroProfileError(
                    "Hero daily_fondness_map value is outside uint32"
                )
            daily[key] = value
        return (
            NOTIFY_HERO_FONDNESS_LEVEL_UP,
            self.codec.encode(
                "Notify_HeroFondnessLevelUp",
                {
                    "RoleId": self._role_id(profile),
                    "HeroUId": int(row["uid"]),
                    "FondnessLevel": int(row.get("fondness_level", 0)),
                    "Fondness": int(row.get("fondness", 0)),
                    "DailyFondnessMap": daily,
                },
            ),
        )

    def _response_values(
        self,
        request_id: int,
        error_code: int,
        profile: Mapping[str, Any],
        *,
        uid: int = 0,
        row: Optional[Mapping[str, Any]] = None,
        materials: Iterable[Mapping[str, int]] = (),
        skill_id: int = 0,
        new_skill_id: int = 0,
        uid2: int = 0,
        row2: Optional[Mapping[str, Any]] = None,
        target_profession_id: int = 0,
        talent_id: int = 0,
        talent_tree_id: int = 0,
        fondness_level: int = 0,
        rewards: Iterable[Mapping[str, int]] = (),
    ) -> Dict[str, Any]:
        authoritative_uid = int(row.get("uid", uid)) if row is not None else uid
        values: Dict[str, Any] = {"Errorcode": error_code}
        if request_id in {CS_HERO_BREAKTHROUGH, CS_HERO_ENHANCE}:
            values.update(
                {
                    "RoleId": self._role_id(profile),
                    "HeroUId": authoritative_uid,
                    "ItemList": list(materials),
                }
            )
            if request_id == CS_HERO_BREAKTHROUGH:
                values["LevelLimit"] = (
                    int(row.get("level_limit", 0)) if row is not None else 0
                )
        elif request_id == CS_HERO_SKILL_ENHANCE:
            values.update(
                {
                    "RoleId": self._role_id(profile),
                    "HeroUId": authoritative_uid,
                    "SkillId": skill_id,
                    "NewSkillId": new_skill_id,
                }
            )
        elif request_id == CS_SWITCH_PROFESSION:
            values.update(
                {
                    "HeroUId1": authoritative_uid,
                    "HeroUId2": int(row2.get("uid", uid2))
                    if row2 is not None
                    else uid2,
                    "Hero1ProfessionId": self._profession_id(row),
                    "Hero2ProfessionId": self._profession_id(row2),
                }
            )
        elif request_id == CS_UNLOCK_PROFESSION_TALENT:
            values.update(
                {
                    "HeroUId": authoritative_uid,
                    "ProfessionId": self._profession_id(row) or target_profession_id,
                    "TalentTreeId": talent_tree_id,
                    "Talent": {"Id": talent_id, "PropList": []},
                }
            )
        elif request_id == CS_FONDNESS_LEVEL_UP:
            values.update(
                {
                    "HeroUId": authoritative_uid,
                    "Level": int(row.get("fondness_level", fondness_level))
                    if row is not None
                    else fondness_level,
                }
            )
        elif request_id == CS_TAKE_HERO_FONDNESS_REWARD:
            values.update(
                {
                    "HeroUId": authoritative_uid,
                    "FondnessLevel": fondness_level,
                    "RewardList": self._reward_values(rewards),
                }
            )
        else:
            values.update(
                {
                    "TalentTreeId": talent_tree_id,
                    "RewardList": self._reward_values(rewards),
                }
            )
        return values

    def _result(
        self,
        request_id: int,
        error_code: int,
        profile: Mapping[str, Any],
        *,
        uid: int = 0,
        row: Optional[Mapping[str, Any]] = None,
        materials: Iterable[Mapping[str, int]] = (),
        skill_id: int = 0,
        new_skill_id: int = 0,
        uid2: int = 0,
        row2: Optional[Mapping[str, Any]] = None,
        target_profession_id: int = 0,
        talent_id: int = 0,
        talent_tree_id: int = 0,
        fondness_level: int = 0,
        rewards: Iterable[Mapping[str, int]] = (),
        changes: Optional[Dict[str, Any]] = None,
        notifications: Tuple[Tuple[int, bytes], ...] = (),
        reason: str = "",
    ) -> HeroProgressionResult:
        values = self._response_values(
            request_id,
            error_code,
            profile,
            uid=uid,
            row=row,
            materials=materials,
            skill_id=skill_id,
            new_skill_id=new_skill_id,
            uid2=uid2,
            row2=row2,
            target_profession_id=target_profession_id,
            talent_id=talent_id,
            talent_tree_id=talent_tree_id,
            fondness_level=fondness_level,
            rewards=rewards,
        )
        return HeroProgressionResult(
            response_id=RESPONSE_IDS[request_id],
            body=self.codec.encode(RESPONSE_MESSAGES[request_id], values),
            changes=changes or {},
            notifications=notifications,
            error_code=error_code,
            reason=reason,
        )

    def _normalized_inventory(
        self, profile: Mapping[str, Any]
    ) -> Dict[str, Dict[str, int]]:
        try:
            return normalize_inventory(copy.deepcopy(profile.get("inventory", {})))
        except InventoryContentError as exc:
            raise HeroEconomyError(ERROR_CONFIG, str(exc)) from exc

    @staticmethod
    def _aggregate_resources(
        resources: Iterable[Mapping[str, int]],
    ) -> Tuple[Mapping[str, int], ...]:
        order: list[Tuple[int, int]] = []
        totals: Dict[Tuple[int, int], int] = {}
        for resource in resources:
            resource_type = int(resource["type"])
            resource_id = int(resource["id"])
            amount = int(resource["num"])
            if amount <= 0:
                raise HeroEconomyError(
                    ERROR_CONFIG,
                    f"resource {resource_type}:{resource_id} has no amount",
                )
            key = (resource_type, resource_id)
            if key not in totals:
                order.append(key)
                totals[key] = 0
            totals[key] += amount
            if totals[key] > UINT64_MAX:
                raise HeroEconomyError(
                    ERROR_CONFIG,
                    f"resource {resource_type}:{resource_id} exceeds uint64",
                )
        return tuple(
            MappingProxyType({"type": key[0], "id": key[1], "num": totals[key]})
            for key in order
        )

    @staticmethod
    def _item_signature(
        resources: Iterable[Mapping[str, int]],
    ) -> Dict[int, int]:
        return {
            int(resource["id"]): int(resource["num"])
            for resource in resources
            if int(resource["type"]) == RESOURCE_ITEM
        }

    def _validate_requested_materials(
        self,
        profile: Mapping[str, Any],
        materials: Sequence[Mapping[str, int]],
        expected_costs: Iterable[Mapping[str, int]],
    ) -> None:
        expected = self._item_signature(expected_costs)
        if not materials and expected:
            raise HeroEconomyError(
                ERROR_MATERIAL_ILLEGAL, "configured material list is required"
            )
        inventory = self._normalized_inventory(profile)
        uid_to_id = {int(item["uid"]): int(item["id"]) for item in inventory.values()}
        requested: Dict[int, int] = {}
        seen_uids = set()
        for material in materials:
            uid = int(material["ItemUId"])
            amount = int(material["ItemNum"])
            if uid in seen_uids:
                raise HeroEconomyError(
                    ERROR_MATERIAL_ILLEGAL, f"material UID {uid} is duplicated"
                )
            seen_uids.add(uid)
            item_id = uid_to_id.get(uid)
            if item_id is None:
                raise HeroEconomyError(
                    ERROR_MATERIAL_ILLEGAL,
                    f"material UID {uid} is not in account inventory",
                )
            requested[item_id] = requested.get(item_id, 0) + amount
        if requested != expected:
            raise HeroEconomyError(
                ERROR_MATERIAL_ILLEGAL,
                f"requested items {requested} do not match configured {expected}",
            )

    @staticmethod
    def _economy_uint(value: Any, label: str) -> int:
        if isinstance(value, bool):
            raise HeroEconomyError(ERROR_CONFIG, f"{label} must be an integer")
        try:
            parsed = int(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise HeroEconomyError(ERROR_CONFIG, f"{label} must be an integer") from exc
        if parsed < 0 or parsed > UINT64_MAX:
            raise HeroEconomyError(ERROR_CONFIG, f"{label} is outside uint64")
        return parsed

    def _economy_changes(
        self,
        profile: Mapping[str, Any],
        *,
        request_id: int,
        operation: str,
        row: Mapping[str, Any],
        costs: Iterable[Mapping[str, int]] = (),
        rewards: Iterable[Mapping[str, int]] = (),
        context: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        aggregated_costs = self._aggregate_resources(costs)
        aggregated_rewards = self._aggregate_resources(rewards)
        inventory = self._normalized_inventory(profile)

        resource_history_value = profile.get("resource_history", [])
        reward_history_value = profile.get("reward_history", [])
        if not isinstance(resource_history_value, list):
            raise HeroEconomyError(
                ERROR_CONFIG, "profile.resource_history must be a list"
            )
        if not isinstance(reward_history_value, list):
            raise HeroEconomyError(
                ERROR_CONFIG, "profile.reward_history must be a list"
            )
        resource_history = copy.deepcopy(resource_history_value)
        reward_history = copy.deepcopy(reward_history_value)
        scalar_values: Dict[str, int] = {}
        touched_scalars: set[str] = set()
        touched_inventory = False

        def scalar_value(field_name: str) -> int:
            if field_name not in scalar_values:
                scalar_values[field_name] = self._economy_uint(
                    profile.get(field_name, 0), f"profile.{field_name}"
                )
            return scalar_values[field_name]

        for cost in aggregated_costs:
            resource_type = int(cost["type"])
            resource_id = int(cost["id"])
            amount = int(cost["num"])
            if resource_type == RESOURCE_ITEM:
                touched_inventory = True
                item = inventory.get(str(resource_id))
                current = int(item["count"]) if item is not None else 0
                if current < amount:
                    raise HeroEconomyError(
                        ERROR_ITEM_NUM_NOT_ENOUGH,
                        f"item {resource_id} needs {amount}, account has {current}",
                    )
                assert item is not None
                item["count"] = current - amount
                continue
            field_name = RESOURCE_TOP_LEVEL_FIELDS.get(resource_type)
            if field_name is None or resource_id != 0:
                raise HeroEconomyError(
                    ERROR_CONFIG,
                    f"unsupported Hero cost {resource_type}:{resource_id}",
                )
            current = scalar_value(field_name)
            if current < amount:
                code = (
                    ERROR_GOLD_NOT_ENOUGH
                    if resource_type == RESOURCE_GOLD
                    else ERROR_ITEM_NUM_NOT_ENOUGH
                )
                raise HeroEconomyError(
                    code,
                    f"{field_name} needs {amount}, account has {current}",
                )
            scalar_values[field_name] = current - amount
            touched_scalars.add(field_name)

        applied_rewards = []
        saturated_rewards = []
        for reward in aggregated_rewards:
            resource_type = int(reward["type"])
            resource_id = int(reward["id"])
            amount = int(reward["num"])
            applied = amount
            if resource_type == RESOURCE_ITEM:
                touched_inventory = True
                key = str(resource_id)
                item = inventory.get(key)
                current = int(item["count"]) if item is not None else 0
                maximum = self.catalog.item_max_stacks.get(resource_id)
                if maximum is None or maximum <= 0:
                    raise HeroEconomyError(
                        ERROR_CONFIG,
                        f"reward item {resource_id} has no storable a_item row",
                    )
                if current > maximum:
                    raise HeroEconomyError(
                        ERROR_EXCEED_ITEM_LIMIT_COUNT,
                        f"item {resource_id} already exceeds cap {maximum}",
                    )
                if current + amount > maximum:
                    if profile.get("all_content_unlocked") and current == maximum:
                        applied = 0
                        saturated_rewards.append(dict(reward))
                    else:
                        raise HeroEconomyError(
                            ERROR_EXCEED_ITEM_LIMIT_COUNT,
                            f"item {resource_id} would exceed cap {maximum}",
                        )
                if applied:
                    inventory[key] = {
                        "uid": int(item["uid"])
                        if item is not None
                        else stable_item_uid(resource_id),
                        "id": resource_id,
                        "count": current + applied,
                        "para": int(item.get("para", 0)) if item is not None else 0,
                    }
            else:
                field_name = RESOURCE_TOP_LEVEL_FIELDS.get(resource_type)
                if field_name is None or resource_id != 0:
                    raise HeroEconomyError(
                        ERROR_CONFIG,
                        f"unsupported Hero reward {resource_type}:{resource_id}",
                    )
                current = scalar_value(field_name)
                if current + amount > UINT64_MAX:
                    raise HeroEconomyError(
                        ERROR_CONFIG,
                        f"reward would overflow profile.{field_name}",
                    )
                scalar_values[field_name] = current + amount
                touched_scalars.add(field_name)
            if applied:
                applied_rewards.append(
                    {
                        "type": resource_type,
                        "id": resource_id,
                        "num": applied,
                    }
                )

        static_costs = [dict(resource) for resource in aggregated_costs]
        static_rewards = [dict(resource) for resource in aggregated_rewards]
        transaction_context = copy.deepcopy(dict(context or {}))
        history_entry: Dict[str, Any] = {
            "source": "hero_progression",
            "command_id": request_id,
            "operation": operation,
            "hero_uid": int(row["uid"]),
            "hero_id": int(row["id"]),
            "profession_id": self._profession_id(row),
            "costs": static_costs,
            "rewards": static_rewards,
            "applied_rewards": applied_rewards,
            "saturated_rewards": saturated_rewards,
        }
        if transaction_context:
            history_entry["context"] = transaction_context
        resource_history.append(history_entry)

        if aggregated_rewards:
            claim_suffix = transaction_context.get("claim", "reward")
            reward_history.append(
                {
                    "claim_key": (f"hero:{int(row['uid'])}:{operation}:{claim_suffix}"),
                    "source": "hero_progression",
                    "source_id": int(row["id"]),
                    "command_id": request_id,
                    "hero_uid": int(row["uid"]),
                    "rewards": static_rewards,
                    "applied_rewards": applied_rewards,
                    "saturated_rewards": saturated_rewards,
                }
            )

        changes: Dict[str, Any] = {"resource_history": resource_history}
        if touched_inventory:
            changes["inventory"] = inventory
        for field_name in sorted(touched_scalars):
            changes[field_name] = scalar_values[field_name]
        if aggregated_rewards:
            changes["reward_history"] = reward_history
        return changes

    def _hero_failure(
        self,
        request_id: int,
        account: Mapping[str, Any],
        uid: int,
        exc: Exception,
        *,
        materials: Iterable[Mapping[str, int]] = (),
        skill_id: int = 0,
        talent_id: int = 0,
        fondness_level: int = 0,
    ) -> HeroProgressionResult:
        code = (
            ERROR_HERO_NOT_FOUND
            if isinstance(exc, LookupError)
            else exc.error_code
            if isinstance(exc, HeroActionError)
            else ERROR_CONFIG
        )
        return self._result(
            request_id,
            code,
            account,
            uid=uid,
            materials=materials,
            skill_id=skill_id,
            new_skill_id=skill_id,
            talent_id=talent_id,
            fondness_level=fondness_level,
            reason=str(exc),
        )

    def _handle_enhance(
        self,
        account: Mapping[str, Any],
        uid: int,
        materials: Tuple[Mapping[str, int], ...],
    ) -> HeroProgressionResult:
        try:
            _, _, _, row, _ = self._owned_hero(account, uid)
            level = _profile_uint(row, "level", 1, minimum=1, maximum=UINT32_MAX)
            level_limit = _profile_uint(
                row,
                "level_limit",
                level,
                minimum=level,
                maximum=UINT32_MAX,
            )
            breakthrough_level = _profile_uint(
                row,
                "breakthrough_level",
                0,
                maximum=UINT32_MAX,
            )
        except (LookupError, HeroProfileError) as exc:
            return self._hero_failure(
                CS_HERO_ENHANCE,
                account,
                uid,
                exc,
                materials=materials,
            )
        if (
            bool(account.get("all_content_unlocked"))
            and level >= level_limit
            and breakthrough_level >= 3
        ):
            return self._result(
                CS_HERO_ENHANCE,
                ERROR_ALREADY_HIGHEST_LIMIT_LEVEL,
                account,
                uid=uid,
                row=row,
                materials=materials,
                reason="all-unlocked Hero is already at its persisted maximum",
            )
        if level >= level_limit:
            return self._result(
                CS_HERO_ENHANCE,
                ERROR_ALREADY_REACH_LIMIT_LEVEL,
                account,
                uid=uid,
                row=row,
                materials=materials,
                reason=f"Hero reached current cap {level_limit}",
            )
        # No generated content table relates a Hero level to EXP, ItemList,
        # or a next level.  a_job_skill.open_item is explicitly described by
        # a_item[1006223] as profession-level material and must not be reused.
        return self._result(
            CS_HERO_ENHANCE,
            ERROR_CONFIG,
            account,
            uid=uid,
            row=row,
            materials=materials,
            reason=(
                "generated content has no closed Hero level/material table; "
                "request rejected without mutation"
            ),
        )

    def _handle_breakthrough(
        self,
        account: Mapping[str, Any],
        uid: int,
        materials: Tuple[Mapping[str, int], ...],
    ) -> HeroProgressionResult:
        try:
            _, _, _, row, _ = self._owned_hero(account, uid)
            level = _profile_uint(row, "level", 1, minimum=1, maximum=UINT32_MAX)
            level_limit = _profile_uint(
                row,
                "level_limit",
                level,
                minimum=level,
                maximum=UINT32_MAX,
            )
            breakthrough_level = _profile_uint(
                row,
                "breakthrough_level",
                0,
                maximum=UINT32_MAX,
            )
        except (LookupError, HeroProfileError) as exc:
            return self._hero_failure(
                CS_HERO_BREAKTHROUGH,
                account,
                uid,
                exc,
                materials=materials,
            )
        if (
            bool(account.get("all_content_unlocked"))
            and level >= level_limit
            and breakthrough_level >= 3
        ):
            return self._result(
                CS_HERO_BREAKTHROUGH,
                ERROR_ALREADY_HIGHEST_LIMIT_LEVEL,
                account,
                uid=uid,
                row=row,
                materials=materials,
                reason="all-unlocked Hero is already at its persisted maximum",
            )
        if level < level_limit:
            return self._result(
                CS_HERO_BREAKTHROUGH,
                ERROR_HERO_LEVEL_NOT_ENOUGH,
                account,
                uid=uid,
                row=row,
                materials=materials,
                reason=f"Hero has not reached level {level_limit}",
            )
        # a_break is keyed by gospel_rare and is therefore not Hero validation.
        # No other generated table closes Hero breakthrough caps or costs.
        return self._result(
            CS_HERO_BREAKTHROUGH,
            ERROR_CONFIG,
            account,
            uid=uid,
            row=row,
            materials=materials,
            reason=(
                "generated content has no closed Hero breakthrough table; "
                "request rejected without mutation"
            ),
        )

    def _handle_skill_enhance(
        self, account: Mapping[str, Any], uid: int, skill_id: int
    ) -> HeroProgressionResult:
        try:
            _, rows, index, row, hero = self._owned_hero(account, uid)
            values = self._active_skill_values(row)
        except (LookupError, HeroProfileError) as exc:
            return self._hero_failure(
                CS_HERO_SKILL_ENHANCE,
                account,
                uid,
                exc,
                skill_id=skill_id,
            )
        definition = {skill.skill_id: skill for skill in hero.active_skills}.get(
            skill_id
        )
        position = next(
            (i for i, value in enumerate(values) if value["Id"] == skill_id),
            None,
        )
        if definition is None or position is None:
            return self._result(
                CS_HERO_SKILL_ENHANCE,
                ERROR_SKILL_NOT_FOUND,
                account,
                uid=uid,
                row=row,
                skill_id=skill_id,
                new_skill_id=skill_id,
                reason=f"Hero UID {uid} does not own skill {skill_id}",
            )
        current = values[position]["Level"]
        if current >= definition.max_level:
            return self._result(
                CS_HERO_SKILL_ENHANCE,
                ERROR_SKILL_ALREADY_HIGHEST_LEVEL,
                account,
                uid=uid,
                row=row,
                skill_id=skill_id,
                new_skill_id=skill_id,
                reason=f"skill {skill_id} is already level {current}",
            )
        raw_skills = copy.deepcopy(row.get("active_skills", row.get("skills")))
        assert isinstance(raw_skills, list)
        raw_skill = raw_skills[position]
        assert isinstance(raw_skill, dict)
        raw_skill["level"] = current + 1
        if int(raw_skill.get("valid_level", current)) == current:
            raw_skill["valid_level"] = current + 1
        row["active_skills"] = raw_skills
        rows[index] = row
        return self._result(
            CS_HERO_SKILL_ENHANCE,
            ERROR_SUCCESS,
            account,
            uid=uid,
            row=row,
            skill_id=skill_id,
            new_skill_id=skill_id,
            changes={"heroes": rows},
            notifications=(self._skill_notification(row),),
        )

    def _switch_rows(
        self, profile: Mapping[str, Any]
    ) -> Tuple[list[Any], list[Any], Dict[int, int]]:
        source = profile.get("heroes")
        if not isinstance(source, list):
            raise HeroProfileError("profile.heroes must be a list")
        original = copy.deepcopy(source)
        rows = copy.deepcopy(source)
        by_uid: Dict[int, int] = {}
        for index, row in enumerate(rows):
            if not isinstance(row, Mapping):
                continue
            uid = _profile_uint(row, "uid", 0, minimum=1, maximum=UINT64_MAX)
            if uid in by_uid:
                raise HeroProfileError(f"Hero UID {uid} is duplicated")
            by_uid[uid] = index
        return original, rows, by_uid

    @staticmethod
    def _swap_projection_fields(first: Dict[str, Any], second: Dict[str, Any]) -> None:
        # Progress belongs to the Profession dictionary on the runtime.  When
        # two exclusive professions exchange holders, move any persisted
        # projections which exist on both rows.  Missing legacy fields are not
        # deleted or synthesized.
        for key in PROFESSION_PROJECTION_FIELDS:
            if key in first and key in second:
                first[key], second[key] = second[key], first[key]

    def _handle_switch(
        self,
        account: Mapping[str, Any],
        uid1: int,
        uid2: int,
        profession_id: int,
    ) -> HeroProgressionResult:
        try:
            _, rows, by_uid = self._switch_rows(account)
        except HeroProfileError as exc:
            return self._result(
                CS_SWITCH_PROFESSION,
                ERROR_CONFIG,
                account,
                uid=uid1,
                uid2=uid2,
                reason=str(exc),
            )
        first_index = by_uid.get(uid1)
        second_index = by_uid.get(uid2) if uid2 else None
        first = (
            dict(rows[first_index])
            if first_index is not None and isinstance(rows[first_index], Mapping)
            else None
        )
        second = (
            dict(rows[second_index])
            if second_index is not None and isinstance(rows[second_index], Mapping)
            else None
        )

        def error(code: int, reason: str) -> HeroProgressionResult:
            return self._result(
                CS_SWITCH_PROFESSION,
                code,
                account,
                uid=uid1,
                row=first,
                uid2=uid2,
                row2=second,
                target_profession_id=profession_id,
                reason=reason,
            )

        if first is None or (uid2 and second is None):
            return error(ERROR_HERO_NOT_FOUND, "switch references an unowned UID")
        if uid2 and uid1 == uid2:
            return error(ERROR_CLIENT_REQUEST_PARAM, "switch UIDs must differ")
        target = self.catalog.professions.get(profession_id)
        if target is None:
            return error(
                ERROR_TALENT_TREE_ILLEGAL,
                f"profession {profession_id} is absent from a_job",
            )
        old_profession = self._profession_id(first)
        if old_profession == profession_id and uid2 == 0:
            return error(ERROR_DATA_REPEATED, "Hero already has this profession")

        owner_indices = [
            index
            for index, row in enumerate(rows)
            if index != first_index
            and isinstance(row, Mapping)
            and self._profession_id(row) == profession_id
        ]
        if target.repeat_type == 0:
            if owner_indices:
                if second_index not in owner_indices:
                    return error(
                        ERROR_CLIENT_REQUEST_PARAM,
                        "exclusive profession owner UID does not match HeroUId2",
                    )
            elif uid2:
                return error(
                    ERROR_CLIENT_REQUEST_PARAM,
                    "HeroUId2 does not own the requested profession",
                )
        elif uid2:
            return error(
                ERROR_CLIENT_REQUEST_PARAM,
                "repeatable/no-profession switch must not name HeroUId2",
            )

        first["profession_id"] = profession_id
        rows[first_index] = first
        if second is not None and second_index is not None:
            second["profession_id"] = old_profession
            first_id = int(first.get("id") or 0)
            second_id = int(second.get("id") or 0)
            if first_id != LILIA_HERO_ID and second_id != LILIA_HERO_ID:
                self._swap_projection_fields(first, second)
            rows[first_index] = first
            rows[second_index] = second
        return self._result(
            CS_SWITCH_PROFESSION,
            ERROR_SUCCESS,
            account,
            uid=uid1,
            row=first,
            uid2=uid2,
            row2=second,
            target_profession_id=profession_id,
            changes={"heroes": rows},
        )

    def _handle_unlock_talent(
        self, account: Mapping[str, Any], uid: int, talent_id: int
    ) -> HeroProgressionResult:
        talent = self.catalog.talents.get(talent_id)
        group = self.catalog.groups.get(talent.group_id) if talent is not None else None
        if uid == 0:
            # TalentInfoView deliberately calls CS_UnlockProfessionTalent with
            # belongUid=0.  Proto3 omits that default-valued field entirely,
            # so resolve the one formal owned witch whose profession contains
            # the clicked talent.  This is also how the runtime itself locates
            # the RoleData used to render the same screen.
            if talent is None or group is None:
                return self._result(
                    CS_UNLOCK_PROFESSION_TALENT,
                    ERROR_TALENT_TREE_ILLEGAL,
                    account,
                    talent_id=talent_id,
                    reason=f"talent {talent_id} is absent from a_job_skill",
                )
            try:
                rows, index, row, _, profession, progress = self._tree_reward_holder(
                    account, group
                )
            except (HeroActionError, HeroProfileError) as exc:
                code = (
                    exc.error_code if isinstance(exc, HeroActionError) else ERROR_CONFIG
                )
                return self._result(
                    CS_UNLOCK_PROFESSION_TALENT,
                    code,
                    account,
                    target_profession_id=group.profession_id,
                    talent_id=talent_id,
                    talent_tree_id=group.group_id,
                    reason=str(exc),
                )
        else:
            try:
                _, rows, index, row, hero = self._owned_hero(account, uid)
                profession, progress = self._profession_progress(
                    row,
                    hero,
                    all_unlocked=bool(account.get("all_content_unlocked")),
                )
            except (LookupError, HeroActionError, HeroProfileError) as exc:
                return self._hero_failure(
                    CS_UNLOCK_PROFESSION_TALENT,
                    account,
                    uid,
                    exc,
                    talent_id=talent_id,
                )
            talent = profession.talent_by_id.get(talent_id)
            group = (
                self.catalog.groups.get(talent.group_id)
                if talent is not None
                else None
            )
        tree_id = talent.group_id if talent is not None else progress.current_tree_id
        if talent is None:
            return self._result(
                CS_UNLOCK_PROFESSION_TALENT,
                ERROR_TALENT_TREE_ILLEGAL,
                account,
                uid=uid,
                row=row,
                talent_id=talent_id,
                talent_tree_id=tree_id,
                reason=f"talent {talent_id} is not in profession {profession.profession_id}",
            )
        if talent_id in progress.talent_ids:
            return self._result(
                CS_UNLOCK_PROFESSION_TALENT,
                ERROR_PROFESSION_TALENT_ALREADY_UNLOCKED,
                account,
                uid=uid,
                row=row,
                talent_id=talent_id,
                talent_tree_id=tree_id,
                reason=f"talent {talent_id} is already unlocked",
            )
        expected = (
            profession.talents[progress.level]
            if progress.level < profession.max_level
            else None
        )
        if expected is None:
            return self._result(
                CS_UNLOCK_PROFESSION_TALENT,
                ERROR_ALREADY_HIGHEST_LIMIT_LEVEL,
                account,
                uid=uid,
                row=row,
                talent_id=talent_id,
                talent_tree_id=tree_id,
                reason="profession is already at maximum level",
            )
        if talent.talent_id != expected.talent_id:
            return self._result(
                CS_UNLOCK_PROFESSION_TALENT,
                ERROR_FRONT_TALENT_NOT_UNLOCKED,
                account,
                uid=uid,
                row=row,
                talent_id=talent_id,
                talent_tree_id=tree_id,
                reason=f"next verified talent is {expected.talent_id}",
            )
        if talent.group_id not in progress.unlocked_tree_ids:
            return self._result(
                CS_UNLOCK_PROFESSION_TALENT,
                ERROR_PROFESSION_LEVEL_NOT_ENOUGH,
                account,
                uid=uid,
                row=row,
                talent_id=talent_id,
                talent_tree_id=tree_id,
                reason=f"talent tree {talent.group_id} is not unlocked",
            )
        try:
            economy = self._economy_changes(
                account,
                request_id=CS_UNLOCK_PROFESSION_TALENT,
                operation="unlock_talent",
                row=row,
                costs=talent.costs,
                context={
                    "talent_id": talent_id,
                    "talent_tree_id": talent.group_id,
                    "profession_id": profession.profession_id,
                    "stage": group.stage if group is not None else 0,
                    "stage_node": talent.number,
                    "request_hero_uid": uid,
                    "resolved_hero_uid": int(row["uid"]),
                    "from_level": progress.level,
                    "to_level": progress.level + talent.skill_value,
                },
            )
        except HeroEconomyError as exc:
            return self._result(
                CS_UNLOCK_PROFESSION_TALENT,
                exc.error_code,
                account,
                uid=uid,
                row=row,
                talent_id=talent_id,
                talent_tree_id=tree_id,
                reason=str(exc),
            )
        unlocked_tree_ids = progress.unlocked_tree_ids
        current_tree_id = talent.group_id
        level_limit = progress.level_limit
        advanced_tree_id = 0
        if group is not None and talent.number == len(group.talents):
            group_index = profession.groups.index(group)
            if group_index + 1 < len(profession.groups):
                next_group = profession.groups[group_index + 1]
                advanced_tree_id = next_group.group_id
                required_tree_count = group_index + 2
                if len(unlocked_tree_ids) < required_tree_count:
                    unlocked_tree_ids = tuple(
                        candidate.group_id
                        for candidate in profession.groups[:required_tree_count]
                    )
                current_tree_id = next_group.group_id
                level_limit = profession.groups[len(unlocked_tree_ids) - 1].level_cap
        updated = replace(
            progress,
            talent_ids=progress.talent_ids + (talent_id,),
            unlocked_tree_ids=unlocked_tree_ids,
            current_tree_id=current_tree_id,
            level=progress.level + talent.skill_value,
            level_limit=level_limit,
        )
        self._write_progress(row, updated)
        rows[index] = row
        cost_text = "+".join(
            f"{int(cost['type'])}:{int(cost['id'])}x{int(cost['num'])}"
            for cost in talent.costs
        ) or "none"
        stage = group.stage if group is not None else 0
        stage_count = len(profession.groups)
        stage_size = len(group.talents) if group is not None else 0
        return self._result(
            CS_UNLOCK_PROFESSION_TALENT,
            ERROR_SUCCESS,
            account,
            uid=uid,
            row=row,
            talent_id=talent_id,
            talent_tree_id=updated.current_tree_id,
            changes={"heroes": rows, **economy},
            # The source talent response carries no material list. Publish
            # absolute inventory counts before profession events refresh UI.
            notifications=tuple(
                make_update_item_packet(self._role_id(account), item, self.codec)
                for key, item in economy.get("inventory", {}).items()
                if item != account.get("inventory", {}).get(key)
            ) + self._profession_notifications(account, profession, updated),
            reason=(
                "profession-talent click "
                f"request_uid={uid} resolved_uid={int(row['uid'])} "
                f"profession={profession.profession_id} talent={talent_id} "
                f"stage={stage}/{stage_count} node={talent.number}/{stage_size} "
                f"level={progress.level}->{updated.level} "
                f"tree={progress.current_tree_id}->{updated.current_tree_id} "
                f"cap={progress.level_limit}->{updated.level_limit} "
                f"advanced_tree={advanced_tree_id} cost={cost_text}"
            ),
        )

    def _handle_fondness_level_up(
        self, account: Mapping[str, Any], uid: int
    ) -> HeroProgressionResult:
        try:
            _, rows, index, row, hero = self._owned_hero(account, uid)
            if not hero.favor_levels:
                raise HeroActionError(
                    ERROR_CONFIG,
                    f"a_favor has no verified rows for Hero {hero.hero_id}",
                )
            current = _profile_uint(
                row,
                "fondness_level",
                0,
                maximum=hero.max_fondness_level,
            )
            fondness = _profile_uint(row, "fondness", 0, maximum=UINT32_MAX)
        except (LookupError, HeroActionError, HeroProfileError) as exc:
            return self._hero_failure(CS_FONDNESS_LEVEL_UP, account, uid, exc)
        if current >= hero.max_fondness_level:
            return self._result(
                CS_FONDNESS_LEVEL_UP,
                ERROR_HERO_FONDNESS_LEVEL_UP_FAILED,
                account,
                uid=uid,
                row=row,
                reason="Hero fondness is already at its table maximum",
            )
        target = hero.favor_levels[current + 1]
        if fondness < target.cumulative_exp:
            return self._result(
                CS_FONDNESS_LEVEL_UP,
                ERROR_HERO_FONDNESS_LEVEL_UP_FAILED,
                account,
                uid=uid,
                row=row,
                reason=(f"fondness {fondness} has not reached {target.cumulative_exp}"),
            )
        row["fondness_level"] = target.level
        rows[index] = row
        try:
            notification = self._fondness_notification(account, row)
        except HeroProfileError as exc:
            return self._result(
                CS_FONDNESS_LEVEL_UP,
                ERROR_CONFIG,
                account,
                uid=uid,
                row=dict(account.get("heroes", [])[index]),
                reason=str(exc),
            )
        return self._result(
            CS_FONDNESS_LEVEL_UP,
            ERROR_SUCCESS,
            account,
            uid=uid,
            row=row,
            changes={"heroes": rows},
            notifications=(notification,),
        )

    def _handle_fondness_reward(
        self, account: Mapping[str, Any], uid: int, level: int
    ) -> HeroProgressionResult:
        try:
            _, rows, index, row, hero = self._owned_hero(account, uid)
            definition = hero.favor_levels.get(level)
            if definition is None or not definition.rewards:
                raise HeroActionError(
                    ERROR_NO_REWARD_CAN_BE_TAKEN,
                    f"a_favor has no reward for Hero {hero.hero_id} level {level}",
                )
            current = _profile_uint(
                row,
                "fondness_level",
                0,
                maximum=hero.max_fondness_level,
            )
            claims = _profile_uint_list(row, "rewarded_fondness_levels") or ()
        except (LookupError, HeroActionError, HeroProfileError) as exc:
            return self._hero_failure(
                CS_TAKE_HERO_FONDNESS_REWARD,
                account,
                uid,
                exc,
                fondness_level=level,
            )
        if level in claims:
            return self._result(
                CS_TAKE_HERO_FONDNESS_REWARD,
                ERROR_FONDNESS_REWARD_ALREADY_TAKEN,
                account,
                uid=uid,
                row=row,
                fondness_level=level,
                reason=f"fondness reward {level} is already claimed",
            )
        if current < level:
            return self._result(
                CS_TAKE_HERO_FONDNESS_REWARD,
                ERROR_FONDNESS_LEVEL_NOT_ENOUGH,
                account,
                uid=uid,
                row=row,
                fondness_level=level,
                reason=f"Hero fondness level {current} is below {level}",
            )
        try:
            economy = self._economy_changes(
                account,
                request_id=CS_TAKE_HERO_FONDNESS_REWARD,
                operation="take_fondness_reward",
                row=row,
                rewards=definition.rewards,
                context={"claim": level, "fondness_level": level},
            )
        except HeroEconomyError as exc:
            return self._result(
                CS_TAKE_HERO_FONDNESS_REWARD,
                exc.error_code,
                account,
                uid=uid,
                row=row,
                fondness_level=level,
                reason=str(exc),
            )
        row["rewarded_fondness_levels"] = sorted((*claims, level))
        rows[index] = row
        return self._result(
            CS_TAKE_HERO_FONDNESS_REWARD,
            ERROR_SUCCESS,
            account,
            uid=uid,
            row=row,
            fondness_level=level,
            rewards=definition.rewards,
            changes={"heroes": rows, **economy},
        )

    def _tree_reward_holder(
        self,
        account: Mapping[str, Any],
        group: ProfessionGroupDefinition,
    ) -> Tuple[
        list[Any],
        int,
        Dict[str, Any],
        HeroDefinition,
        ProfessionDefinition,
        ProfessionProgress,
    ]:
        source = account.get("heroes")
        if not isinstance(source, list):
            raise HeroProfileError("profile.heroes must be a list")
        rows = copy.deepcopy(source)
        candidates = []
        for index, raw in enumerate(rows):
            if not isinstance(raw, Mapping):
                continue
            if self._profession_id(raw) != group.profession_id:
                continue
            hero_id = _profile_uint(raw, "id", 0, minimum=1, maximum=UINT32_MAX)
            hero = self.catalog.heroes.get(hero_id)
            if hero is None or hero_id == LILIA_HERO_ID or not hero.displayed:
                continue
            candidates.append((index, dict(raw), hero))
        if not candidates:
            raise HeroActionError(
                ERROR_TALENT_TREE_ILLEGAL,
                f"no formal Hero owns profession {group.profession_id}",
            )
        if len(candidates) > 1:
            canonical = [
                item
                for item in candidates
                if item[2].default_profession_id == group.profession_id
            ]
            if len(canonical) != 1:
                raise HeroProfileError(
                    f"profession {group.profession_id} has ambiguous owners"
                )
            candidates = canonical
        index, row, hero = candidates[0]
        profession, progress = self._profession_progress(
            row,
            hero,
            all_unlocked=bool(account.get("all_content_unlocked")),
        )
        rows[index] = row
        return rows, index, row, hero, profession, progress

    def _handle_tree_reward(
        self, account: Mapping[str, Any], tree_id: int
    ) -> HeroProgressionResult:
        group = self.catalog.groups.get(tree_id)
        if group is None:
            return self._result(
                CS_TAKE_PROFESSION_TALENT_TREE_REWARD,
                ERROR_TALENT_TREE_ILLEGAL,
                account,
                talent_tree_id=tree_id,
                reason=f"talent tree {tree_id} is absent from a_job_skillgroup",
            )
        try:
            rows, index, row, _, profession, progress = self._tree_reward_holder(
                account, group
            )
        except (HeroActionError, HeroProfileError) as exc:
            code = exc.error_code if isinstance(exc, HeroActionError) else ERROR_CONFIG
            return self._result(
                CS_TAKE_PROFESSION_TALENT_TREE_REWARD,
                code,
                account,
                talent_tree_id=tree_id,
                reason=str(exc),
            )
        if tree_id in progress.rewarded_tree_ids:
            return self._result(
                CS_TAKE_PROFESSION_TALENT_TREE_REWARD,
                ERROR_TALENT_TREE_ALREADY_REWARDED,
                account,
                talent_tree_id=tree_id,
                reason=f"talent tree {tree_id} reward is already claimed",
            )
        learned = set(progress.talent_ids)
        required = {talent.talent_id for talent in group.talents}
        if not required.issubset(learned):
            return self._result(
                CS_TAKE_PROFESSION_TALENT_TREE_REWARD,
                ERROR_NOT_ALL_TALENT_UNLOCKED_IN_TREE,
                account,
                talent_tree_id=tree_id,
                reason=f"talent tree {tree_id} is not complete",
            )
        try:
            economy = self._economy_changes(
                account,
                request_id=CS_TAKE_PROFESSION_TALENT_TREE_REWARD,
                operation="take_talent_tree_reward",
                row=row,
                rewards=group.rewards,
                context={"claim": tree_id, "talent_tree_id": tree_id},
            )
        except HeroEconomyError as exc:
            return self._result(
                CS_TAKE_PROFESSION_TALENT_TREE_REWARD,
                exc.error_code,
                account,
                talent_tree_id=tree_id,
                reason=str(exc),
            )
        updated = replace(
            progress,
            rewarded_tree_ids=tuple(sorted((*progress.rewarded_tree_ids, tree_id))),
        )
        self._write_progress(row, updated)
        rows[index] = row
        return self._result(
            CS_TAKE_PROFESSION_TALENT_TREE_REWARD,
            ERROR_SUCCESS,
            account,
            talent_tree_id=tree_id,
            rewards=group.rewards,
            changes={"heroes": rows, **economy},
        )

    def handle(
        self,
        request_id: int,
        profile: Optional[Mapping[str, Any]],
        request_body: bytes = b"",
    ) -> Optional[HeroProgressionResult]:
        if request_id not in RESPONSE_IDS:
            return None
        account = profile if isinstance(profile, Mapping) else {}
        uid = 0
        uid2 = 0
        requested = 0
        materials: Tuple[Mapping[str, int], ...] = ()
        try:
            fields = _wire_field_map(request_body)
            allowed_fields = {
                CS_HERO_BREAKTHROUGH: {1, 2, 3},
                CS_HERO_ENHANCE: {1, 2, 3},
                CS_HERO_SKILL_ENHANCE: {1, 2, 3},
                CS_SWITCH_PROFESSION: {1, 2, 3},
                CS_UNLOCK_PROFESSION_TALENT: {1, 4},
                CS_FONDNESS_LEVEL_UP: {1},
                CS_TAKE_HERO_FONDNESS_REWARD: {1, 2},
                CS_TAKE_PROFESSION_TALENT_TREE_REWARD: {1},
            }[request_id]
            if set(fields) - allowed_fields:
                raise ProtobufDecodeError("request contains unknown fields")
            if request_id in {
                CS_HERO_BREAKTHROUGH,
                CS_HERO_ENHANCE,
                CS_HERO_SKILL_ENHANCE,
            }:
                requested_role = _required_uint(
                    fields, 1, minimum=1, maximum=UINT64_MAX
                )
                uid = _required_uint(fields, 2, minimum=1, maximum=UINT64_MAX)
                if requested_role != self._role_id(account):
                    return self._result(
                        request_id,
                        ERROR_ROLE_ID_ILLEGAL,
                        account,
                        uid=uid,
                        reason=(
                            f"requested role {requested_role} does not match "
                            f"account role {self._role_id(account)}"
                        ),
                    )
                if request_id == CS_HERO_SKILL_ENHANCE:
                    requested = _required_uint(fields, 3, minimum=1, maximum=UINT32_MAX)
                else:
                    materials = _materials(fields, 3)
            elif request_id == CS_SWITCH_PROFESSION:
                uid = _required_uint(fields, 1, minimum=1, maximum=UINT64_MAX)
                uid2 = _required_uint(fields, 2, maximum=UINT64_MAX)
                requested = _required_uint(fields, 3, minimum=1, maximum=UINT32_MAX)
            elif request_id == CS_UNLOCK_PROFESSION_TALENT:
                # The real TalentInfoView sender supplies ulong zero, which
                # proto3 omits.  A non-zero UID is still accepted for replay
                # tools and older callers, but it is not required on the wire.
                uid = _optional_uint(fields, 1, maximum=UINT64_MAX)
                requested = _required_uint(fields, 4, minimum=1, maximum=UINT32_MAX)
            elif request_id == CS_FONDNESS_LEVEL_UP:
                uid = _required_uint(fields, 1, minimum=1, maximum=UINT64_MAX)
            elif request_id == CS_TAKE_HERO_FONDNESS_REWARD:
                uid = _required_uint(fields, 1, minimum=1, maximum=UINT64_MAX)
                requested = _required_uint(fields, 2, minimum=1, maximum=UINT32_MAX)
            else:
                requested = _required_uint(fields, 1, minimum=1, maximum=UINT32_MAX)
        except (ProtobufDecodeError, TypeError, ValueError) as exc:
            return self._result(
                request_id,
                ERROR_MESSAGE_DATA_ILLEGAL,
                account,
                uid=uid,
                uid2=uid2,
                materials=materials,
                skill_id=requested if request_id == CS_HERO_SKILL_ENHANCE else 0,
                talent_id=requested if request_id == CS_UNLOCK_PROFESSION_TALENT else 0,
                fondness_level=requested
                if request_id == CS_TAKE_HERO_FONDNESS_REWARD
                else 0,
                talent_tree_id=requested
                if request_id == CS_TAKE_PROFESSION_TALENT_TREE_REWARD
                else 0,
                reason=str(exc),
            )

        if request_id == CS_HERO_ENHANCE:
            return self._handle_enhance(account, uid, materials)
        if request_id == CS_HERO_BREAKTHROUGH:
            return self._handle_breakthrough(account, uid, materials)
        if request_id == CS_HERO_SKILL_ENHANCE:
            return self._handle_skill_enhance(account, uid, requested)
        if request_id == CS_SWITCH_PROFESSION:
            return self._handle_switch(account, uid, uid2, requested)
        if request_id == CS_UNLOCK_PROFESSION_TALENT:
            return self._handle_unlock_talent(account, uid, requested)
        if request_id == CS_FONDNESS_LEVEL_UP:
            return self._handle_fondness_level_up(account, uid)
        if request_id == CS_TAKE_HERO_FONDNESS_REWARD:
            return self._handle_fondness_reward(account, uid, requested)
        return self._handle_tree_reward(account, requested)


Layer = HeroProgressionLayer
Result = HeroProgressionResult


__all__ = [
    "CS_FONDNESS_LEVEL_UP",
    "CS_HERO_BREAKTHROUGH",
    "CS_HERO_ENHANCE",
    "CS_HERO_SKILL_ENHANCE",
    "CS_SWITCH_PROFESSION",
    "CS_TAKE_HERO_FONDNESS_REWARD",
    "CS_TAKE_PROFESSION_TALENT_TREE_REWARD",
    "CS_UNLOCK_PROFESSION_TALENT",
    "ERROR_ALREADY_HIGHEST_LIMIT_LEVEL",
    "ERROR_ALREADY_REACH_LIMIT_LEVEL",
    "ERROR_CLIENT_REQUEST_PARAM",
    "ERROR_CONFIG",
    "ERROR_DATA_REPEATED",
    "ERROR_EXCEED_ITEM_LIMIT_COUNT",
    "ERROR_FONDNESS_LEVEL_NOT_ENOUGH",
    "ERROR_FONDNESS_REWARD_ALREADY_TAKEN",
    "ERROR_FRONT_TALENT_NOT_UNLOCKED",
    "ERROR_GOLD_NOT_ENOUGH",
    "ERROR_HERO_FONDNESS_LEVEL_UP_FAILED",
    "ERROR_HERO_LEVEL_NOT_ENOUGH",
    "ERROR_HERO_NOT_FOUND",
    "ERROR_ITEM_NUM_NOT_ENOUGH",
    "ERROR_MATERIAL_ILLEGAL",
    "ERROR_MESSAGE_DATA_ILLEGAL",
    "ERROR_NOT_ALL_TALENT_UNLOCKED_IN_TREE",
    "ERROR_NO_REWARD_CAN_BE_TAKEN",
    "ERROR_PROFESSION_LEVEL_NOT_ENOUGH",
    "ERROR_PROFESSION_TALENT_ALREADY_UNLOCKED",
    "ERROR_ROLE_ID_ILLEGAL",
    "ERROR_SKILL_ALREADY_HIGHEST_LEVEL",
    "ERROR_SKILL_NOT_FOUND",
    "ERROR_SUCCESS",
    "ERROR_TALENT_TREE_ALREADY_REWARDED",
    "ERROR_TALENT_TREE_ILLEGAL",
    "FavorLevelDefinition",
    "HERO_MESSAGE_FIELDS",
    "HeroDefinition",
    "HeroEconomyError",
    "HeroProgressionCatalog",
    "HeroProgressionCatalogError",
    "HeroProgressionLayer",
    "HeroProgressionResult",
    "HeroProtocolContractError",
    "Layer",
    "NOTIFY_HERO_FONDNESS_LEVEL_UP",
    "NOTIFY_PROFESSION_LEVEL_UP",
    "NOTIFY_UPDATE_HERO_SKILL",
    "NOTIFY_UPDATE_PROFESSION",
    "ProfessionDefinition",
    "ProfessionGroupDefinition",
    "ProfessionTalentDefinition",
    "RESPONSE_IDS",
    "RESPONSE_MESSAGES",
    "REQUEST_MESSAGES",
    "RESOURCE_GOLD",
    "RESOURCE_ITEM",
    "Result",
]
