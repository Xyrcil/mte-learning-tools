"""Table-backed combat attributes for the offline profile.

The shipped runtime calculates ordinary battles locally.  The server is still
responsible for providing three pieces of authoritative state which the runtime
does not reconstruct on its own:

* a non-null, progression-adjusted ``Msg.Gospel.Attr``;
* a legal profession talent prefix for each witch;
* the exact Gospel UIDs equipped by each formation.

This module keeps those concerns in one place.  It deliberately does not apply
an account-wide damage multiplier.  All values come from the defined runtime
tables and from the persisted account progression selected by the player.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Dict, Iterable, Optional, Tuple

try:
    from .independent_growth import bonus_for_hero
except ImportError:
    from independent_growth import bonus_for_hero


UINT32_MAX = (1 << 32) - 1

# PassBuffEff values defined from runtime assembly.
ATK_BASE = 1
ATK_PERCENT = 2
ATK_FLAT = 3
ATK_BATTLE_PERCENT = 4
ATK_BATTLE_VALUE = 5
ATK_BATTLE_PERCENT_UNLIMITED = 6
HP_BASE = 11
HP_PERCENT = 12
HP_FLAT = 13
HP_BATTLE_PERCENT = 14
HP_BATTLE_VALUE = 15
ARMOR_BASE = 23
ARMOR_PERCENT = 24
ARMOR_FLAT = 25
ARMOR_BATTLE_PERCENT = 26
ARMOR_BATTLE_VALUE = 27
TRUE_DAMAGE = 81
TRUE_DAMAGE_ADD = 91
BREAK_POWER = 401
HEAL_EFFECT = 2011
BP_EFFICIENCY = 3001
WEAK_EFFICIENCY = 4001
SUPPRESS_EFFECTS = frozenset(range(8000, 8008))

WIRE_FIELDS = MappingProxyType(
    {
        "Hp": HP_BASE,
        "Atk": ATK_BASE,
        "Weakness": WEAK_EFFICIENCY,
        "Crit": -1,
        "BP": BP_EFFICIENCY,
        "Defence": ARMOR_BASE,
        "Hit": -2,
        "RealDamageProb": TRUE_DAMAGE,
        "RealDamageAppendParam": TRUE_DAMAGE_ADD,
        "TreatEffect": HEAL_EFFECT,
    }
)


class CombatAttributeError(ValueError):
    """Raised when persisted combat state is inconsistent with static tables."""


def _integer(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _effective_value(row: Mapping[str, Any], real: str, fake: str, default: int) -> int:
    value = _integer(row.get(real), default)
    fake_value = _integer(row.get(fake), 0)
    if fake_value and fake_value != value:
        return fake_value
    return value


def _json_list(value: Any, label: str) -> list[Any]:
    if value in (None, ""):
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise CombatAttributeError(f"{label} is not valid JSON") from exc
    if not isinstance(value, list):
        raise CombatAttributeError(f"{label} must be a JSON list")
    return value


def _effect_values(value: Any, label: str) -> Dict[int, int]:
    result: Dict[int, int] = {}
    for index, item in enumerate(_json_list(value, label)):
        if not isinstance(item, Mapping):
            raise CombatAttributeError(f"{label}[{index}] must be an object")
        effect = _integer(item.get("effect"), -1)
        amount = _integer(item.get("value"), 0)
        if effect <= 0:
            raise CombatAttributeError(f"{label}[{index}] has an invalid effect")
        result[effect] = result.get(effect, 0) + amount
    return result


def _merge_effects(target: Dict[int, int], source: Mapping[int, int]) -> None:
    for effect, value in source.items():
        target[int(effect)] = target.get(int(effect), 0) + int(value)


def _parse_ids(value: Any, label: str) -> Tuple[int, ...]:
    values = _json_list(value, label)
    result = tuple(_integer(item, 0) for item in values)
    if any(item <= 0 for item in result):
        raise CombatAttributeError(f"{label} contains an invalid ID")
    return result


def _buff_ids(value: Any) -> Tuple[int, ...]:
    """Extract configured buff IDs without trying to emulate buff execution."""

    if value in (None, ""):
        return ()
    if isinstance(value, str) and value.lstrip().startswith("["):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            parsed = []
        return tuple(
            _integer(item.get("id"), 0)
            for item in parsed
            if isinstance(item, Mapping) and _integer(item.get("id"), 0) > 0
        )
    result = []
    for group in str(value).split("|"):
        for token in group.split(","):
            buff_id = _integer(token.strip(), 0)
            if buff_id > 0:
                result.append(buff_id)
    return tuple(result)


def _scaled_base(effects: Mapping[int, int], base: int, percent: int, flat: int) -> int:
    # Runtime formulas use float + Mathf.RoundToInt.  All defined percentages
    # are basis points, so this integer form is deterministic for positive data.
    raw = effects.get(base, 0)
    return max(0, int(round(raw * (10_000 + effects.get(percent, 0)) / 10_000)) + effects.get(flat, 0))


@dataclass(frozen=True)
class ProfessionProjection:
    profession_id: int
    talent_ids: Tuple[int, ...]
    unlocked_group_ids: Tuple[int, ...]
    current_group_id: int
    effects: Mapping[int, int]
    passive_buff_ids: Tuple[int, ...]


@dataclass(frozen=True)
class EntityCombatSnapshot:
    entity_id: int
    entity_uid: int
    effects: Mapping[int, int]
    wire_attributes: Mapping[str, int]
    active_skills: Tuple[Tuple[int, int], ...]
    passive_buff_ids: Tuple[int, ...]
    attack: int
    hp: int
    armor: int
    combat_power: int


@dataclass(frozen=True)
class FormationHeroSnapshot:
    hero: EntityCombatSnapshot
    gospels: Tuple[EntityCombatSnapshot, ...]
    attack: int
    hp: int
    armor: int
    combat_power: int


@dataclass(frozen=True)
class FormationCombatSnapshot:
    formation_id: int
    heroes: Tuple[FormationHeroSnapshot, ...]
    combat_power: int
    attack: int
    hp: int
    armor: int


@dataclass(frozen=True)
class DungeonRegressionResult:
    dungeon_id: int
    recommended_power: int
    formation_power: int
    passes_power_gate: bool


@dataclass(frozen=True)
class RuneProjection:
    """Resolved attributes for the five persisted rune positions of one witch."""

    rune_uids: Tuple[int, ...]
    effects: Mapping[int, int]
    passive_buff_ids: Tuple[int, ...]
    active_suit_effect_ids: Tuple[int, ...]


@dataclass(frozen=True)
class DecorationProjection:
    """Active appearance decorations and their level-scaled passive effects.

    ``conditional_effects`` is deliberately diagnostic/display-power state.
    The runtime owns passive trigger execution, so these values must never be
    folded into the always-on wire attributes sent for the witch.
    """

    decoration_ids: Tuple[int, ...]
    passive_buffs: Tuple[Tuple[int, int], ...]
    conditional_effects: Mapping[int, int]


class CombatAttributeResolver:
    """Resolve runtime-equivalent static combat state from one content catalog."""

    def __init__(self, catalog: Any) -> None:
        self.catalog = catalog
        required = {
            "a_people",
            "a_skill",
            "a_job_skillgroup",
            "a_job_skill",
            "a_gospel",
            "a_gospelstate",
            "a_gospel_awake",
            "a_gospelascension",
            "a_favor_gospel",
            "a_spiritbreak",
            "a_global",
            "a_dungeon",
            "a_rune_main",
            "a_main_attr_poor",
            "a_secondary_attr_poor",
            "a_decoration_suiteffect",
            "a_decoration",
            "a_passive_buff",
        }
        missing = sorted(required - set(catalog.tables))
        if missing:
            raise CombatAttributeError(f"content catalog is missing {missing}")

        self.people = {int(row["id"]): row for row in catalog.table("a_people").records}
        self.skills = {int(row["id"]): row for row in catalog.table("a_skill").records}
        self.gospels = {int(row["id"]): row for row in catalog.table("a_gospel").records}
        self.dungeons = {int(row["id"]): row for row in catalog.table("a_dungeon").records}
        self.runes = {
            int(row["id"]): row for row in catalog.table("a_rune_main").records
        }
        self.decorations = {
            int(row["id"]): row for row in catalog.table("a_decoration").records
        }
        self.passive_buffs = {
            int(row["id"]): row
            for row in catalog.table("a_passive_buff").records
        }
        self.rune_main_attributes = {
            int(row["id"]): row
            for row in catalog.table("a_main_attr_poor").records
        }
        self.rune_secondary_attributes = {
            int(row["id"]): row
            for row in catalog.table("a_secondary_attr_poor").records
        }
        suit_effects: Dict[int, list[Mapping[str, Any]]] = {}
        for row in catalog.table("a_decoration_suiteffect").records:
            suit_effects.setdefault(int(row["suit_id"]), []).append(row)
        self.rune_suit_effects = MappingProxyType(
            {
                suit_id: tuple(
                    sorted(rows, key=lambda item: (int(item["quantity"]), int(item["id"])))
                )
                for suit_id, rows in suit_effects.items()
            }
        )

        states: Dict[int, Dict[int, Mapping[str, Any]]] = {}
        for row in catalog.table("a_gospelstate").records:
            states.setdefault(int(row["gospel_id"]), {})[int(row["level"])] = row
        self.gospel_states = MappingProxyType(
            {key: MappingProxyType(value) for key, value in states.items()}
        )

        awake: Dict[int, list[Mapping[str, Any]]] = {}
        for row in catalog.table("a_gospel_awake").records:
            awake.setdefault(int(row["gospel_id"]), []).append(row)
        self.gospel_awake = MappingProxyType(
            {
                key: tuple(sorted(value, key=lambda item: int(item["awaken_level"])))
                for key, value in awake.items()
            }
        )
        self.gospel_ascension = MappingProxyType(
            {
                int(row["gospel_id"]): row
                for row in catalog.table("a_gospelascension").records
            }
        )

        favor_rows: Dict[int, list[Mapping[str, Any]]] = {}
        for row in catalog.table("a_favor_gospel").records:
            favor_rows.setdefault(int(row["gospel_id"]), []).append(row)
        self.gospel_favor = MappingProxyType(
            {
                gospel_id: tuple(
                    sorted(rows, key=lambda item: (int(item["level"]), int(item["id"])))
                )
                for gospel_id, rows in favor_rows.items()
            }
        )
        spirit_rows: Dict[int, list[Mapping[str, Any]]] = {}
        for row in catalog.table("a_spiritbreak").records:
            spirit_rows.setdefault(int(row["gospel_rare"]), []).append(row)
        self.gospel_spirit_breaks = MappingProxyType(
            {
                rarity: tuple(
                    sorted(rows, key=lambda item: int(item["break_time"]))
                )
                for rarity, rows in spirit_rows.items()
            }
        )
        globals_by_id = {
            int(row["id"]): row for row in catalog.table("a_global").records
        }
        self.gospel_spirit_attr_level = max(
            1,
            _integer(globals_by_id.get(9_990_312, {}).get("int_value"), 15),
        )
        self.private_equip_unlock_level = _integer(
            globals_by_id.get(9_991_129, {}).get("int_value"), 120)
        self.private_equip_rows = {}
        for table_name, level_field in (("a_soul_level", "level"), ("a_soul_awake", "awaken_level")):
            try:
                table_rows = catalog.table(table_name).records
            except KeyError:
                table_rows = ()
            grouped = {}
            for row in table_rows:
                grouped.setdefault(int(row["gospel_id"]), []).append(row)
            self.private_equip_rows[table_name] = {
                key: tuple(sorted(rows, key=lambda r: int(r[level_field])))
                for key, rows in grouped.items()
            }

        groups_by_job: Dict[int, list[Mapping[str, Any]]] = {}
        for row in catalog.table("a_job_skillgroup").records:
            groups_by_job.setdefault(int(row["job_id"]), []).append(row)
        self.profession_groups = MappingProxyType(
            {
                job_id: tuple(
                    sorted(rows, key=lambda item: (int(item["job_level"]), int(item["id"])))
                )
                for job_id, rows in groups_by_job.items()
            }
        )
        talents_by_group: Dict[int, list[Mapping[str, Any]]] = {}
        for row in catalog.table("a_job_skill").records:
            talents_by_group.setdefault(int(row["skillgroup_id"]), []).append(row)
        self.profession_talents = MappingProxyType(
            {
                group_id: tuple(
                    sorted(rows, key=lambda item: (int(item["number"]), int(item["id"])))
                )
                for group_id, rows in talents_by_group.items()
            }
        )

    @staticmethod
    def _wire_from_effects(effects: Mapping[int, int], *, crit: int = 0) -> Mapping[str, int]:
        values = {
            name: max(0, int(effects.get(effect, 0)))
            for name, effect in WIRE_FIELDS.items()
        }
        values["Crit"] = max(0, int(crit))
        return MappingProxyType(values)

    @staticmethod
    def _power(
        attack: int,
        hp: int,
        armor: int,
        effects: Mapping[int, int],
        active_skills: Sequence[Tuple[int, int]],
        passive_buff_ids: Sequence[int],
        conditional_effects: Optional[Mapping[int, int]] = None,
    ) -> int:
        """Stable display/regression power derived from real combat inputs.

        The source server's display-only power formula is not present in the
        runtime.  The weights below intentionally use only attributes and
        configured skill/buff state; battle damage itself continues to use the
        runtime's formulas and never this score.
        """

        utility = (
            abs(int(effects.get(WEAK_EFFICIENCY, 0)))
            + abs(int(effects.get(BP_EFFICIENCY, 0)))
            + abs(int(effects.get(HEAL_EFFECT, 0)))
            + abs(int(effects.get(BREAK_POWER, 0)))
        ) // 10
        skill_power = sum(max(1, int(level)) * 30 for _, level in active_skills)
        passive_power = len(tuple(passive_buff_ids)) * 100
        projected = conditional_effects or {}

        # Appearance passives execute conditionally in the runtime.  For the
        # display/regression score, project only positive, directly comparable
        # battle-stat modifiers.  Negative trade-offs and formula-driven
        # utility are represented by the passive's base score instead of being
        # guessed with an invented server formula.
        projected_attack = max(0, int(projected.get(ATK_BATTLE_VALUE, 0)))
        projected_attack += max(
            0,
            int(round(attack * int(projected.get(ATK_BATTLE_PERCENT, 0)) / 10_000)),
        )
        projected_attack += max(
            0,
            int(
                round(
                    attack
                    * int(projected.get(ATK_BATTLE_PERCENT_UNLIMITED, 0))
                    / 10_000
                )
            ),
        )
        projected_hp = max(0, int(projected.get(HP_BATTLE_VALUE, 0)))
        projected_hp += max(
            0,
            int(round(hp * int(projected.get(HP_BATTLE_PERCENT, 0)) / 10_000)),
        )
        projected_armor = max(0, int(projected.get(ARMOR_BATTLE_VALUE, 0)))
        projected_armor += max(
            0,
            int(
                round(
                    armor * int(projected.get(ARMOR_BATTLE_PERCENT, 0)) / 10_000
                )
            ),
        )
        conditional_power = (
            projected_attack * 6 + projected_hp // 2 + projected_armor * 4
        )
        result = (
            max(0, attack) * 6
            + max(0, hp) // 2
            + max(0, armor) * 4
            + utility
            + skill_power
            + passive_power
            + conditional_power
        )
        return min(UINT32_MAX, max(1, int(result)))

    @staticmethod
    def _base_snapshot_effects(state: Mapping[str, Any]) -> Dict[int, int]:
        return {
            ATK_BASE: _integer(state.get("atk"), 0),
            HP_BASE: _integer(state.get("hp"), 0),
            ARMOR_BASE: _integer(state.get("arm"), 0),
            WEAK_EFFICIENCY: _integer(
                state.get("weak_efficiency", state.get("weakness", 0)), 0
            ),
            BP_EFFICIENCY: _integer(state.get("bp_efficiency", state.get("bp", 0)), 0),
            TRUE_DAMAGE: _integer(state.get("true_damage"), 0),
            TRUE_DAMAGE_ADD: _integer(state.get("true_damage_add"), 0),
            HEAL_EFFECT: _integer(state.get("heal_effect"), 0),
            ATK_PERCENT: _integer(state.get("atk_extra_percent"), 0),
            HP_PERCENT: _integer(state.get("hp_extra_percent"), 0),
        }

    def profession_projection(
        self,
        hero: Mapping[str, Any],
        *,
        all_unlocked: bool = False,
    ) -> ProfessionProjection:
        profession_id = _integer(hero.get("profession_id"), 0)
        groups = self.profession_groups.get(profession_id, ())
        if not groups:
            return ProfessionProjection(
                profession_id,
                (),
                (),
                0,
                MappingProxyType({}),
                (),
            )

        flattened = tuple(
            talent for group in groups for talent in self.profession_talents.get(int(group["id"]), ())
        )
        full_ids = tuple(int(row["id"]) for row in flattened)
        raw_ids = hero.get("profession_talent_ids")
        if isinstance(raw_ids, list):
            talent_ids = tuple(_integer(value, 0) for value in raw_ids)
            if talent_ids != full_ids[: len(talent_ids)]:
                raise CombatAttributeError(
                    f"profession {profession_id} talents are not a legal prefix"
                )
        else:
            level_default = len(full_ids) if all_unlocked else 0
            level = min(len(full_ids), max(0, _integer(hero.get("profession_level"), level_default)))
            talent_ids = full_ids[:level]

        full_group_ids = tuple(int(row["id"]) for row in groups)
        raw_groups = hero.get("profession_unlocked_tree_ids")
        if isinstance(raw_groups, list):
            unlocked = tuple(_integer(value, 0) for value in raw_groups)
            if not unlocked or unlocked != full_group_ids[: len(unlocked)]:
                raise CombatAttributeError(
                    f"profession {profession_id} groups are not a legal prefix"
                )
        elif all_unlocked:
            unlocked = full_group_ids
        else:
            level = len(talent_ids)
            unlocked_count = 1
            cumulative = 0
            for index, group in enumerate(groups, 1):
                cumulative += len(self.profession_talents.get(int(group["id"]), ()))
                if level > cumulative:
                    unlocked_count = min(len(groups), index + 1)
            unlocked = full_group_ids[:unlocked_count]

        requested = _integer(hero.get("profession_talent_tree_id"), unlocked[-1])
        current = requested if requested in unlocked else unlocked[-1]
        current_stage = int(next(row for row in groups if int(row["id"]) == current)["job_level"])
        allowed_groups = {
            int(row["id"]) for row in groups if int(row["job_level"]) <= current_stage
        }
        selected = tuple(
            row
            for row in flattened
            if int(row["id"]) in set(talent_ids)
            and int(row["skillgroup_id"]) in allowed_groups
        )
        effects: Dict[int, int] = {}
        passive_ids = []
        for row in selected:
            effects[ATK_BASE] = effects.get(ATK_BASE, 0) + _integer(row.get("atk"), 0)
            effects[HP_BASE] = effects.get(HP_BASE, 0) + _integer(row.get("hp"), 0)
            effects[WEAK_EFFICIENCY] = effects.get(WEAK_EFFICIENCY, 0) + _integer(
                row.get("weakness"), 0
            )
            effects[BP_EFFICIENCY] = effects.get(BP_EFFICIENCY, 0) + _integer(
                row.get("bp"), 0
            )
            _merge_effects(
                effects,
                _effect_values(row.get("attr"), f"a_job_skill[{row['id']}].attr"),
            )
            passive_ids.extend(_buff_ids(row.get("passive_buff")))
            passive_ids.extend(_buff_ids(row.get("people_skill_buff")))
        return ProfessionProjection(
            profession_id,
            tuple(int(row["id"]) for row in selected),
            unlocked,
            current,
            MappingProxyType(effects),
            tuple(passive_ids),
        )

    def tower_profession_projection(
        self,
        hero: Mapping[str, Any],
        tower_level: int,
    ) -> ProfessionProjection:
        """Project the exact profession prefix used by ``RoleData.SetTower``.

        Holy Tower does not use the account's learned profession prefix.  The
        runtime derives it from the temporary Witch level: every completed
        20-level group is active and only ``level % 20`` nodes of the current
        group are active.
        """

        hero_id = _integer(hero.get("id"), 0)
        definition = self.people.get(hero_id, {})
        profession_id = _integer(
            hero.get("profession_id"),
            _integer(
                definition.get("job_default", definition.get("job_id")), 0
            ),
        )
        groups = self.profession_groups.get(profession_id, ())
        if not groups:
            return ProfessionProjection(
                profession_id,
                (),
                (),
                0,
                MappingProxyType({}),
                (),
            )

        level = max(0, _integer(tower_level, 0))
        current_stage = level // 20 + 1
        current_node = level % 20
        selected: list[Mapping[str, Any]] = []
        unlocked_groups: list[int] = []
        current_group_id = 0
        for group in groups:
            group_stage = _integer(group.get("job_level"), 0)
            if group_stage > current_stage:
                break
            group_id = _integer(group.get("id"), 0)
            unlocked_groups.append(group_id)
            current_group_id = group_id
            for talent in self.profession_talents.get(group_id, ()):
                if (
                    group_stage == current_stage
                    and _integer(talent.get("number"), 0) > current_node
                ):
                    continue
                selected.append(talent)

        effects: Dict[int, int] = {}
        passive_ids: list[int] = []
        for row in selected:
            effects[ATK_BASE] = effects.get(ATK_BASE, 0) + _integer(
                row.get("atk"), 0
            )
            effects[HP_BASE] = effects.get(HP_BASE, 0) + _integer(
                row.get("hp"), 0
            )
            effects[WEAK_EFFICIENCY] = effects.get(
                WEAK_EFFICIENCY, 0
            ) + _integer(row.get("weakness"), 0)
            effects[BP_EFFICIENCY] = effects.get(BP_EFFICIENCY, 0) + _integer(
                row.get("bp"), 0
            )
            _merge_effects(
                effects,
                _effect_values(
                    row.get("attr"), f"a_job_skill[{row['id']}].attr"
                ),
            )
            passive_ids.extend(_buff_ids(row.get("passive_buff")))
            passive_ids.extend(_buff_ids(row.get("people_skill_buff")))

        return ProfessionProjection(
            profession_id,
            tuple(_integer(row.get("id"), 0) for row in selected),
            tuple(unlocked_groups),
            current_group_id,
            MappingProxyType(effects),
            tuple(passive_ids),
        )

    def resolve_tower_hero(
        self,
        hero: Mapping[str, Any],
        tower_level: int,
        *,
        all_unlocked: bool = False,
    ) -> EntityCombatSnapshot:
        """Resolve a Holy Tower Witch without account-only stat sources.

        This mirrors the runtime's ``RoleData.SetTower`` boundary: static Witch
        attributes plus the tower-level profession prefix.  Account runes,
        appearance decorations, fondness and the account profession tree are
        intentionally excluded.  Active skills remain in the payload so the
        battle controller can initialize the Witch's actual moves.
        """

        hero_id = _integer(hero.get("id"), 0)
        definition = self.people.get(hero_id)
        if definition is None:
            raise CombatAttributeError(f"unknown a_people[{hero_id}]")
        effects: Dict[int, int] = {
            ATK_BASE: _integer(definition.get("atk"), 0),
            HP_BASE: _integer(definition.get("hp"), 0),
            WEAK_EFFICIENCY: _integer(definition.get("weakness"), 0),
            BP_EFFICIENCY: _integer(definition.get("bp"), 0),
            TRUE_DAMAGE_ADD: _integer(definition.get("true_damage_add"), 0),
        }
        profession = self.tower_profession_projection(hero, tower_level)
        _merge_effects(effects, profession.effects)

        configured_ids = _parse_ids(
            definition.get("skill"), f"a_people[{hero_id}].skill"
        )
        saved = hero.get("active_skills", hero.get("skills", []))
        saved_rows = saved if isinstance(saved, list) else []
        by_id = {
            _integer(row.get("id"), 0): row
            for row in saved_rows
            if isinstance(row, Mapping) and _integer(row.get("id"), 0) > 0
        }
        active: list[Tuple[int, int]] = []
        passive_ids = list(profession.passive_buff_ids)
        for skill_id in configured_ids:
            skill = self.skills.get(skill_id)
            if skill is None:
                raise CombatAttributeError(f"missing a_skill[{skill_id}]")
            maximum = max(1, _integer(skill.get("upgrade_max"), 1))
            saved_skill = by_id.get(skill_id, {})
            default_level = maximum if all_unlocked else 1
            level = min(
                maximum,
                max(
                    1,
                    _integer(
                        saved_skill.get(
                            "valid_level", saved_skill.get("level")
                        ),
                        default_level,
                    ),
                ),
            )
            active.append((skill_id, level))
            passive_ids.extend(_buff_ids(skill.get("passive_buff")))

        attack = _scaled_base(effects, ATK_BASE, ATK_PERCENT, ATK_FLAT)
        hp = _scaled_base(effects, HP_BASE, HP_PERCENT, HP_FLAT)
        armor = _scaled_base(effects, ARMOR_BASE, ARMOR_PERCENT, ARMOR_FLAT)
        wire = self._wire_from_effects(effects)
        power = self._power(
            attack, hp, armor, effects, active, passive_ids
        )
        return EntityCombatSnapshot(
            hero_id,
            _integer(hero.get("uid"), 0),
            MappingProxyType(effects),
            wire,
            tuple(active),
            tuple(passive_ids),
            attack,
            hp,
            armor,
            power,
        )

    def rune_projection(
        self,
        profile: Optional[Mapping[str, Any]],
        hero_id: int,
    ) -> RuneProjection:
        """Project persisted RuneItem ownership with the runtime's exact scaling.

        ``RuneItem.HeroUId`` is misleadingly named in the defined schema: the
        managed runtime compares it with ``a_people.id``.  Main attributes grow
        by ``(base + (level - 1) * growth) / 100`` and persisted random values
        are also hundredths.  Set effects activate cumulatively at two/four
        pieces, matching ``RoleData.GetRoleDecoSuitAttr``.
        """

        if profile is None:
            return RuneProjection((), MappingProxyType({}), (), ())
        raw_rows = profile.get("rune_items", ())
        if not isinstance(raw_rows, Sequence) or isinstance(raw_rows, (str, bytes)):
            return RuneProjection((), MappingProxyType({}), (), ())

        equipped: Dict[int, Mapping[str, Any]] = {}
        for row in raw_rows:
            if not isinstance(row, Mapping) or _integer(row.get("hero_uid"), 0) != hero_id:
                continue
            rune_id = _integer(row.get("id"), 0)
            definition = self.runes.get(rune_id)
            if definition is None:
                raise CombatAttributeError(f"equipped rune references unknown a_rune_main[{rune_id}]")
            position = _integer(definition.get("position"), 0)
            if position not in {1, 2, 3, 4, 5}:
                raise CombatAttributeError(f"a_rune_main[{rune_id}] has invalid position {position}")
            if position in equipped:
                raise CombatAttributeError(
                    f"witch {hero_id} has multiple runes in position {position}"
                )
            equipped[position] = row

        effects: Dict[int, int] = {}
        suit_counts: Dict[int, int] = {}
        rune_uids = []
        for position in sorted(equipped):
            row = equipped[position]
            rune_id = _integer(row.get("id"), 0)
            definition = self.runes[rune_id]
            rune_uids.append(_integer(row.get("rune_item_uid"), 0))

            main_id = _integer(row.get("basic_attr"), 0)
            main = self.rune_main_attributes.get(main_id)
            if main is None:
                raise CombatAttributeError(
                    f"rune UID {row.get('rune_item_uid', 0)} has unknown main attribute {main_id}"
                )
            level = max(1, _integer(row.get("level"), 1))
            effect = _integer(main.get("attr_id"), 0)
            value = (
                _integer(main.get("original_attr_value"), 0)
                + (level - 1) * _integer(main.get("attr_growth_value"), 0)
            ) // 100
            if effect <= 0 or value < 0:
                raise CombatAttributeError(f"a_main_attr_poor[{main_id}] is invalid")
            effects[effect] = effects.get(effect, 0) + value

            for index in (1, 2, 3):
                secondary_id = _integer(row.get(f"rand_attr_{index}"), 0)
                secondary_value = _integer(row.get(f"attr_{index}_value"), 0)
                if secondary_id == 0 and secondary_value == 0:
                    continue
                secondary = self.rune_secondary_attributes.get(secondary_id)
                if secondary is None:
                    raise CombatAttributeError(
                        f"rune UID {row.get('rune_item_uid', 0)} has unknown random attribute {secondary_id}"
                    )
                secondary_effect = _integer(secondary.get("attr_id"), 0)
                if secondary_effect <= 0 or secondary_value < 0:
                    raise CombatAttributeError(
                        f"a_secondary_attr_poor[{secondary_id}] is invalid"
                    )
                effects[secondary_effect] = (
                    effects.get(secondary_effect, 0) + secondary_value // 100
                )

            suit_id = _integer(definition.get("suit"), 0)
            if suit_id > 0:
                suit_counts[suit_id] = suit_counts.get(suit_id, 0) + 1

        passive_ids = []
        active_suit_effect_ids = []
        for suit_id in sorted(suit_counts):
            for row in self.rune_suit_effects.get(suit_id, ()):
                if _integer(row.get("quantity"), 0) > suit_counts[suit_id]:
                    continue
                effect_id = _integer(row.get("id"), 0)
                _merge_effects(
                    effects,
                    _effect_values(
                        row.get("attr"),
                        f"a_decoration_suiteffect[{effect_id}].attr",
                    ),
                )
                passive_id = _integer(row.get("passive_buff"), 0)
                if passive_id > 0:
                    passive_ids.append(passive_id)
                active_suit_effect_ids.append(effect_id)

        return RuneProjection(
            tuple(rune_uids),
            MappingProxyType(effects),
            tuple(passive_ids),
            tuple(active_suit_effect_ids),
        )

    @staticmethod
    def _profile_rows(value: Any) -> Tuple[Mapping[str, Any], ...]:
        if isinstance(value, Mapping):
            return tuple(row for row in value.values() if isinstance(row, Mapping))
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            return tuple(row for row in value if isinstance(row, Mapping))
        return ()

    def _passive_effect_projection(
        self,
        passive_id: int,
        level: int,
        trail: Tuple[int, ...] = (),
    ) -> Dict[int, int]:
        """Resolve numeric passive effects with runtime-equivalent level growth.

        Child buffs inherit the parent level.  Three-field children activate
        together, while four-field entries are weighted alternatives.  The
        latter use a deterministic expected value solely for display power;
        actual random selection remains in ``PassiveBuff`` on the runtime.
        """

        if passive_id in trail:
            raise CombatAttributeError(
                f"a_passive_buff cycle: {' -> '.join(map(str, trail + (passive_id,)))}"
            )
        row = self.passive_buffs.get(passive_id)
        if row is None:
            raise CombatAttributeError(f"unknown a_passive_buff[{passive_id}]")

        effects: Dict[int, int] = {}
        for index, item in enumerate(
            _json_list(row.get("effect"), f"a_passive_buff[{passive_id}].effect")
        ):
            if not isinstance(item, Mapping):
                raise CombatAttributeError(
                    f"a_passive_buff[{passive_id}].effect[{index}] must be an object"
                )
            effect = _integer(item.get("effect"), -1)
            if effect <= 0:
                raise CombatAttributeError(
                    f"a_passive_buff[{passive_id}].effect[{index}] has an invalid effect"
                )
            amount = _integer(item.get("value"), 0)
            amount += (level - 1) * _integer(item.get("up"), 0)
            effects[effect] = effects.get(effect, 0) + amount

        child_groups = []
        selection_count: Optional[int] = None
        selection_is_dynamic = False
        for group_index, raw_group in enumerate(
            str(row.get("buff") or "").split("|")
        ):
            raw_group = raw_group.strip()
            if not raw_group:
                continue
            parts = tuple(part.strip() for part in raw_group.split(","))
            child_token = parts[0]
            if "^" in child_token:
                if group_index != 0 or selection_count is not None:
                    raise CombatAttributeError(
                        f"a_passive_buff[{passive_id}].buff has an invalid selection prefix"
                    )
                count_token, child_token = child_token.split("^", 1)
                selection_count = _integer(count_token, -1)
                selection_is_dynamic = selection_count < 0
            child_id = _integer(child_token, 0)
            if child_id <= 0:
                raise CombatAttributeError(
                    f"a_passive_buff[{passive_id}].buff has an invalid child"
                )
            weight = _integer(parts[3], 0) if len(parts) > 3 else 0
            child_groups.append((child_id, weight, len(parts) > 3))

        children = [
            (
                self._passive_effect_projection(
                    child_id,
                    level,
                    trail + (passive_id,),
                ),
                weight,
                weighted,
            )
            for child_id, weight, weighted in child_groups
        ]
        if selection_count is not None:
            if any(weighted for _, _, weighted in children):
                raise CombatAttributeError(
                    f"a_passive_buff[{passive_id}].buff mixes selection and weighted branches"
                )
            # ``N^`` randomly keeps N children from the candidate set.  A
            # constant N has a uniform expected contribution; a runtime
            # expression such as ``(1+atk:...)^`` cannot be projected without
            # battle context and therefore contributes only the root score.
            if not selection_is_dynamic and children:
                keep = min(len(children), max(0, int(selection_count)))
                effect_ids = {
                    effect for child, _, _ in children for effect in child
                }
                for effect in effect_ids:
                    total_value = sum(child.get(effect, 0) for child, _, _ in children)
                    effects[effect] = effects.get(effect, 0) + int(
                        round(total_value * keep / len(children))
                    )
        elif any(weighted for _, _, weighted in children):
            if not all(weighted and weight > 0 for _, weight, weighted in children):
                raise CombatAttributeError(
                    f"a_passive_buff[{passive_id}].buff mixes invalid random branches"
                )
            total_weight = sum(weight for _, weight, _ in children)
            effect_ids = {
                effect for child, _, _ in children for effect in child
            }
            for effect in effect_ids:
                weighted_value = sum(
                    child.get(effect, 0) * weight
                    for child, weight, _ in children
                )
                effects[effect] = effects.get(effect, 0) + int(
                    round(weighted_value / total_weight)
                )
        else:
            for child, _, _ in children:
                _merge_effects(effects, child)
        return effects

    def decoration_projection(
        self,
        profile: Optional[Mapping[str, Any]],
        hero: Mapping[str, Any],
    ) -> DecorationProjection:
        """Resolve only decorations on the witch's current appearance preset."""

        empty = DecorationProjection((), (), MappingProxyType({}))
        if profile is None:
            return empty
        hero_uid = _integer(hero.get("uid"), 0)
        root = profile.get("hero_appearances")
        if hero_uid <= 0 or not isinstance(root, Mapping):
            return empty
        container = root.get(str(hero_uid), root.get(hero_uid))
        if not isinstance(container, Mapping):
            return empty
        appearances = self._profile_rows(
            container.get("appearances", container.get("appearance_list"))
        )
        if not appearances:
            return empty
        has_current_id = "current_id" in container
        current_id = _integer(container.get("current_id"), -1)
        current = next(
            (row for row in appearances if _integer(row.get("id", row.get("Id")), -2) == current_id),
            None,
        )
        if current is None and not has_current_id:
            current = next(
                (
                    row
                    for row in appearances
                    if _integer(row.get("is_used", row.get("IsUsed")), 0) > 0
                ),
                None,
            )
        if current is None and has_current_id:
            # ``current_id`` is authoritative.  Do not let a stale IsUsed bit
            # on an old preset silently activate combat passives.
            current = next(
                (
                    row
                    for row in appearances
                    if _integer(row.get("id", row.get("Id")), -1) == 0
                ),
                None,
            )
        if current is None:
            return empty

        inventory: Dict[int, Mapping[str, Any]] = {}
        for row in self._profile_rows(profile.get("decoration_items")):
            item_id = _integer(row.get("id", row.get("Id")), 0)
            if item_id > 0:
                # CollectiblesLayer uses the first persisted item for a static
                # ID; match that rule even when resolving a pre-seed profile.
                inventory.setdefault(item_id, row)
        decoration_ids = []
        passive_levels = []
        projected: Dict[int, int] = {}
        raw_decorations = current.get(
            "decorations", current.get("decoration_list", current.get("DecorationList"))
        )
        for row in self._profile_rows(raw_decorations):
            decoration_id = _integer(row.get("id", row.get("Id")), 0)
            if decoration_id <= 0:
                continue
            definition = self.decorations.get(decoration_id)
            if definition is None:
                # Network writes reject unknown IDs.  Reads stay tolerant so a
                # stale legacy preset cannot make the entire account unloadable.
                continue
            item = inventory.get(decoration_id)
            if item is None:
                # Matches GameData.user.GetDecorationItem: a visual preset may
                # survive inventory migration, but it contributes no passive.
                continue
            passive_id = _integer(definition.get("attr_basic"), 0)
            if passive_id <= 0:
                continue
            level = _integer(
                item.get("basic_attr_level", item.get("BasicAttrLvl")),
                0,
            )
            if level <= 0 or level > UINT32_MAX:
                level = 1
            decoration_ids.append(decoration_id)
            passive_levels.append((passive_id, level))
            _merge_effects(
                projected,
                self._passive_effect_projection(passive_id, level),
            )
        return DecorationProjection(
            tuple(decoration_ids),
            tuple(passive_levels),
            MappingProxyType(projected),
        )

    def resolve_hero(
        self,
        hero: Mapping[str, Any],
        *,
        all_unlocked: bool = False,
        profile: Optional[Mapping[str, Any]] = None,
    ) -> EntityCombatSnapshot:
        hero_id = _integer(hero.get("id"), 0)
        definition = self.people.get(hero_id)
        if definition is None:
            raise CombatAttributeError(f"unknown a_people[{hero_id}]")
        effects: Dict[int, int] = {
            ATK_BASE: _integer(definition.get("atk"), 0),
            HP_BASE: _integer(definition.get("hp"), 0),
            WEAK_EFFICIENCY: _integer(definition.get("weakness"), 0),
            BP_EFFICIENCY: _integer(definition.get("bp"), 0),
            TRUE_DAMAGE_ADD: _integer(definition.get("true_damage_add"), 0),
        }
        profession = self.profession_projection(hero, all_unlocked=all_unlocked)
        _merge_effects(effects, profession.effects)
        runes = self.rune_projection(profile, hero_id)
        _merge_effects(effects, runes.effects)
        decorations = self.decoration_projection(profile, hero)

        configured_ids = _parse_ids(definition.get("skill"), f"a_people[{hero_id}].skill")
        saved = hero.get("active_skills", hero.get("skills", []))
        saved_rows = saved if isinstance(saved, list) else []
        by_id = {
            _integer(row.get("id"), 0): row
            for row in saved_rows
            if isinstance(row, Mapping) and _integer(row.get("id"), 0) > 0
        }
        active = []
        passive_ids = list(profession.passive_buff_ids)
        passive_ids.extend(runes.passive_buff_ids)
        passive_ids.extend(passive_id for passive_id, _ in decorations.passive_buffs)
        for skill_id in configured_ids:
            skill = self.skills.get(skill_id)
            if skill is None:
                raise CombatAttributeError(f"missing a_skill[{skill_id}]")
            saved_skill = by_id.get(skill_id, {})
            maximum = max(1, _integer(skill.get("upgrade_max"), 1))
            level = min(maximum, max(1, _integer(saved_skill.get("valid_level", saved_skill.get("level")), maximum if all_unlocked else 1)))
            active.append((skill_id, level))
            _merge_effects(effects, _effect_values(skill.get("attr"), f"a_skill[{skill_id}].attr"))
            passive_ids.extend(_buff_ids(skill.get("passive_buff")))

        growth = bonus_for_hero(profile, hero, self.decorations)
        for effect, amount in zip((ATK_FLAT, HP_FLAT, ARMOR_FLAT), growth):
            if amount:
                effects[effect] = effects.get(effect, 0) + amount
        attack = _scaled_base(effects, ATK_BASE, ATK_PERCENT, ATK_FLAT)
        hp = _scaled_base(effects, HP_BASE, HP_PERCENT, HP_FLAT)
        armor = _scaled_base(effects, ARMOR_BASE, ARMOR_PERCENT, ARMOR_FLAT)
        wire = self._wire_from_effects(effects)
        if any(growth):
            wire = MappingProxyType({**wire, **dict(zip(("_GrowthAtk", "_GrowthHp", "_GrowthDef"), growth))})
        power = self._power(
            attack,
            hp,
            armor,
            effects,
            active,
            passive_ids,
            decorations.conditional_effects,
        )
        return EntityCombatSnapshot(
            hero_id,
            _integer(hero.get("uid"), 0),
            MappingProxyType(effects),
            wire,
            tuple(active),
            tuple(passive_ids),
            attack,
            hp,
            armor,
            power,
        )

    def _gospel_fondness_effects(
        self,
        gospel: Mapping[str, Any],
        definition: Mapping[str, Any],
    ) -> Mapping[int, int]:
        """Reproduce ``FuyingData.GetFondnessAttr`` from persisted claims."""

        gospel_id = _integer(gospel.get("id"), 0)
        favor_rows = self.gospel_favor.get(gospel_id, ())

        def cumulative(level: int) -> Tuple[int, int]:
            attack = 0
            hp = 0
            for row in favor_rows:
                if _integer(row.get("level"), 0) > level:
                    break
                attack = max(attack, _integer(row.get("atk"), 0))
                hp = max(hp, _integer(row.get("hp"), 0))
            return attack, hp

        raw_rewards = gospel.get("rewarded_fondness_levels", ())
        if not isinstance(raw_rewards, Sequence) or isinstance(
            raw_rewards, (str, bytes)
        ):
            raw_rewards = ()
        attack = 0
        hp = 0
        for raw_level in raw_rewards:
            level = max(0, _integer(raw_level, 0))
            if level <= 0:
                continue
            previous_attack, previous_hp = cumulative(level - 1)
            current_attack, current_hp = cumulative(level)
            attack += current_attack - previous_attack
            hp += current_hp - previous_hp

        if _integer(gospel.get("is_fondness_limit_breakthrough"), 0):
            spirit_level = max(0, _integer(gospel.get("fondness_level"), 0))
            attack += (
                _integer(definition.get("peak_atk"), 0) * spirit_level
            ) // self.gospel_spirit_attr_level
            hp += (
                _integer(definition.get("peak_hp"), 0) * spirit_level
            ) // self.gospel_spirit_attr_level

        return MappingProxyType({ATK_BASE: attack, HP_BASE: hp})

    def _private_equipment_effects(self, gospel: Mapping[str, Any], profile: Optional[Mapping[str, Any]]):
        """Mirror SoulData's cumulative rows and latest awakening skill.

        Account equipment is intentionally absent from Tower/synthetic
        snapshots, matching HeroControl.GetGospelPassive's Tower exclusion.
        """
        if profile is None or _integer(gospel.get("level"), 0) < self.private_equip_unlock_level:
            return {}, ()
        gid = _integer(gospel.get("id"), 0)
        matches = [r for r in profile.get("private_equipment", [])
                   if isinstance(r, Mapping) and r.get("pequip_id") == gid]
        if len(matches) != 1 or _integer(matches[0].get("pequip_uid"), 0) <= 0:
            return {}, ()
        equip = matches[0]
        level, awake = equip.get("level"), equip.get("awake_level")
        if type(level) is not int or type(awake) is not int or level < 1 or awake < 0:
            return {}, ()
        levels = self.private_equip_rows["a_soul_level"].get(gid, ())
        awakening = self.private_equip_rows["a_soul_awake"].get(gid, ())
        if not any(int(r["level"]) == level for r in levels):
            return {}, ()  # No invented values for 2.0-only compatibility souls.
        if awake and not any(int(r["awaken_level"]) == awake for r in awakening):
            return {}, ()
        effects, skill = {}, None
        for table, rows, limit, field in (
                ("a_soul_level", levels, level, "level"),
                ("a_soul_awake", awakening, awake, "awaken_level")):
            for row in rows:
                if int(row[field]) > limit:
                    continue
                _merge_effects(effects, _effect_values(row.get("attr"), f"{table}[{row['id']}].attr"))
                change = str(row.get("skill_change") or "")
                if change:
                    parts = change.split(",")
                    if len(parts) != 2:
                        raise CombatAttributeError("invalid private-equipment skill change")
                    skill = (int(parts[0]), int(parts[1]))
        passive = ()
        if skill is not None:
            definition = self.skills.get(skill[0])
            if definition is None:
                raise CombatAttributeError("private-equipment skill is absent from a_skill")
            passive = tuple(_buff_ids(definition.get("passive_buff")))
        return effects, passive

    def resolve_gospel(self, gospel: Mapping[str, Any], profile: Optional[Mapping[str, Any]] = None) -> EntityCombatSnapshot:
        gospel_id = _integer(gospel.get("id"), 0)
        definition = self.gospels.get(gospel_id)
        if definition is None:
            raise CombatAttributeError(f"unknown a_gospel[{gospel_id}]")
        states = self.gospel_states.get(gospel_id)
        if not states:
            raise CombatAttributeError(f"a_gospel[{gospel_id}] has no level states")
        level = _effective_value(gospel, "level", "fake_level", 1)
        state = states.get(level)
        if state is None:
            raise CombatAttributeError(
                f"a_gospel[{gospel_id}] has no table-backed level {level}"
            )
        effects = self._base_snapshot_effects(state)
        # Favor claim rows are cumulative maxima.  The runtime telescopes each
        # claimed level's delta, then adds heart-contract peak attributes, and
        # finally lets awakening percentages scale the combined base.
        _merge_effects(
            effects,
            self._gospel_fondness_effects(gospel, definition),
        )

        # 30/70/120/180 are cumulative table levels.  Heart levels 181..183
        # retain breakthrough stage three; they must never select unreleased
        # a_gospelstate rows 184/185.
        break_stage = 0
        for boundary in (30, 70, 120, 180):
            if min(level, 180) > boundary:
                break_stage += 1
        if level >= 180:
            break_stage = 3
        ascension = self.gospel_ascension.get(gospel_id)
        if ascension is not None and break_stage:
            _merge_effects(
                effects,
                _effect_values(
                    ascension.get(f"level_break_{break_stage}"),
                    f"a_gospelascension[{gospel_id}].level_break_{break_stage}",
                ),
            )

        awake_level = _effective_value(gospel, "awake_level", "fake_awake_level", 1)
        awake_level = min(6, max(1, awake_level))
        awake_rows = self.gospel_awake.get(gospel_id, ())
        awake_skill_add = 0
        transformed: Dict[int, int] = {}
        for awake in awake_rows:
            if int(awake["awaken_level"]) > awake_level:
                continue
            _merge_effects(
                effects,
                _effect_values(
                    awake.get("attr"),
                    f"a_gospel_awake[{awake['id']}].attr",
                ),
            )
            awake_skill_add += _integer(awake.get("skill_level_up"), 0)
            change = str(awake.get("skill_change") or "")
            if change:
                parts = change.split(",")
                if len(parts) != 2:
                    raise CombatAttributeError(
                        f"a_gospel_awake[{awake['id']}].skill_change is invalid"
                    )
                transformed[_integer(parts[0], 0) - 1] = _integer(parts[1], 0)

        # The runtime expects awakening and ascension base modifiers to be
        # folded into Msg.Attribute; it adds only skill attrs and the first
        # (race-suppression) ascension effect locally.
        effects[ATK_BASE] = _scaled_base(effects, ATK_BASE, ATK_PERCENT, ATK_FLAT)
        effects[HP_BASE] = _scaled_base(effects, HP_BASE, HP_PERCENT, HP_FLAT)
        effects[ARMOR_BASE] = _scaled_base(effects, ARMOR_BASE, ARMOR_PERCENT, ARMOR_FLAT)
        effects[ATK_PERCENT] = 0
        effects[ATK_FLAT] = 0
        effects[HP_PERCENT] = 0
        effects[HP_FLAT] = 0
        effects[ARMOR_PERCENT] = 0
        effects[ARMOR_FLAT] = 0
        # SoulData sums level/awake attributes independently of the Gospel's
        # base awakening percentages. The runtime GetPBE consumes Msg.Attribute;
        # it does not add these soul flats again in battle.
        soul_effects, soul_passives = self._private_equipment_effects(gospel, profile)
        _merge_effects(effects, soul_effects)
        for base, percent, flat in ((ATK_BASE, ATK_PERCENT, ATK_FLAT),
                                    (HP_BASE, HP_PERCENT, HP_FLAT),
                                    (ARMOR_BASE, ARMOR_PERCENT, ARMOR_FLAT)):
            effects[base] = _scaled_base(effects, base, percent, flat)
            effects[percent] = effects[flat] = 0
        wire_effects = dict(effects)

        configured = list(_parse_ids(definition.get("skill"), f"a_gospel[{gospel_id}].skill"))
        for position, skill_id in transformed.items():
            if position < 0 or position >= len(configured) or skill_id not in self.skills:
                raise CombatAttributeError(
                    f"a_gospel[{gospel_id}] has an invalid awakened skill mapping"
                )
            configured[position] = skill_id
        saved = gospel.get("active_skills", gospel.get("skills", []))
        saved_rows = saved if isinstance(saved, list) else []
        active = []
        passive_ids = list(soul_passives)
        for index, skill_id in enumerate(configured):
            raw_saved = saved_rows[index] if index < len(saved_rows) and isinstance(saved_rows[index], Mapping) else {}
            configured_base_id = _parse_ids(definition.get("skill"), f"a_gospel[{gospel_id}].skill")[index]
            base_cfg = self.skills[configured_base_id]
            base_maximum = max(1, _integer(base_cfg.get("upgrade_max"), 1))
            level_value = max(1, _integer(raw_saved.get("valid_level", raw_saved.get("level")), base_maximum))
            level_value = min(base_maximum, level_value) + awake_skill_add
            active.append((skill_id, level_value))
            skill = self.skills[skill_id]
            _merge_effects(effects, _effect_values(skill.get("attr"), f"a_skill[{skill_id}].attr"))
            passive_ids.extend(_buff_ids(skill.get("passive_buff")))

        wire = self._wire_from_effects(
            wire_effects,
            crit=_integer(state.get("crit"), 0),
        )
        attack = _scaled_base(effects, ATK_BASE, ATK_PERCENT, ATK_FLAT)
        hp = _scaled_base(effects, HP_BASE, HP_PERCENT, HP_FLAT)
        armor = _scaled_base(effects, ARMOR_BASE, ARMOR_PERCENT, ARMOR_FLAT)
        power = self._power(attack, hp, armor, effects, active, passive_ids)
        return EntityCombatSnapshot(
            gospel_id,
            _integer(gospel.get("uid"), 0),
            MappingProxyType(effects),
            wire,
            tuple(active),
            tuple(passive_ids),
            attack,
            hp,
            armor,
            power,
        )

    def resolve_formation(
        self,
        profile: Mapping[str, Any],
        phalanx: Mapping[str, Any],
    ) -> FormationCombatSnapshot:
        heroes = {
            _integer(row.get("uid"), 0): row
            for row in profile.get("heroes", [])
            if isinstance(row, Mapping) and _integer(row.get("uid"), 0) > 0
        }
        gospels = {
            _integer(row.get("uid"), 0): row
            for row in profile.get("gospels", [])
            if isinstance(row, Mapping) and _integer(row.get("uid"), 0) > 0
        }
        hero_uids = [
            _integer(value, 0)
            for value in phalanx.get("hero_uids", [])
            if _integer(value, 0) in heroes
        ]
        positions = phalanx.get("combat_positions")
        position_map = positions if isinstance(positions, Mapping) else {}
        equipped = phalanx.get("equipped_gospels")
        equipped_map = equipped if isinstance(equipped, Mapping) else {}
        active_rows = []
        for index, hero_uid in enumerate(hero_uids):
            position = _integer(
                position_map.get(str(hero_uid), position_map.get(hero_uid, index if index < 3 else -1)),
                -1,
            )
            if position not in {0, 1, 2}:
                continue
            hero_snapshot = self.resolve_hero(
                heroes[hero_uid],
                all_unlocked=bool(profile.get("all_content_unlocked")),
                profile=profile,
            )
            raw_gospels = equipped_map.get(str(hero_uid), equipped_map.get(hero_uid, []))
            gospel_snapshots = []
            if isinstance(raw_gospels, Sequence) and not isinstance(raw_gospels, (str, bytes)):
                for entry in raw_gospels[:3]:
                    if not isinstance(entry, Mapping):
                        continue
                    gospel_uid = _integer(entry.get("gospel_uid"), 0)
                    if gospel_uid in gospels:
                        gospel_snapshots.append(self.resolve_gospel(gospels[gospel_uid], profile))
            attack = hero_snapshot.attack + sum(row.attack for row in gospel_snapshots)
            hp = hero_snapshot.hp + sum(row.hp for row in gospel_snapshots)
            armor = hero_snapshot.armor + sum(row.armor for row in gospel_snapshots)
            combat_power = min(
                UINT32_MAX,
                hero_snapshot.combat_power
                + sum(row.combat_power for row in gospel_snapshots),
            )
            active_rows.append(
                FormationHeroSnapshot(
                    hero_snapshot,
                    tuple(gospel_snapshots),
                    attack,
                    hp,
                    armor,
                    combat_power,
                )
            )
        return FormationCombatSnapshot(
            _integer(phalanx.get("id"), 0),
            tuple(active_rows),
            min(UINT32_MAX, sum(row.combat_power for row in active_rows)),
            sum(row.attack for row in active_rows),
            sum(row.hp for row in active_rows),
            sum(row.armor for row in active_rows),
        )

    def strongest_gospel_uids(
        self, profile: Mapping[str, Any], count: int
    ) -> Tuple[int, ...]:
        rows = [
            row for row in profile.get("gospels", []) if isinstance(row, Mapping)
        ]
        ranked = sorted(
            (
                (self.resolve_gospel(row, profile).combat_power, _integer(row.get("uid"), 0))
                for row in rows
                if _integer(row.get("uid"), 0) > 0
            ),
            key=lambda item: (-item[0], item[1]),
        )
        return tuple(uid for _, uid in ranked[: max(0, count)])

    def treasure_regression(
        self,
        profile: Mapping[str, Any],
        phalanx: Mapping[str, Any],
        dungeon_ids: Iterable[int] = (8_600_927, 8_600_928, 8_600_929),
    ) -> Tuple[DungeonRegressionResult, ...]:
        formation = self.resolve_formation(profile, phalanx)
        results = []
        for dungeon_id in dungeon_ids:
            row = self.dungeons.get(int(dungeon_id))
            if row is None:
                raise CombatAttributeError(f"unknown a_dungeon[{dungeon_id}]")
            recommended = max(1, _integer(row.get("power"), 1))
            results.append(
                DungeonRegressionResult(
                    int(dungeon_id),
                    recommended,
                    formation.combat_power,
                    formation.combat_power >= recommended,
                )
            )
        return tuple(results)


__all__ = [
    "CombatAttributeError",
    "CombatAttributeResolver",
    "DecorationProjection",
    "DungeonRegressionResult",
    "EntityCombatSnapshot",
    "FormationCombatSnapshot",
    "FormationHeroSnapshot",
    "ProfessionProjection",
    "RuneProjection",
]
