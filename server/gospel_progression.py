"""Stateful, catalog-backed Gospel (star-trace) progression endpoints.

The generic protocol runtime can encode a successful ``Errorcode=0`` for the
commands in this module, but several runtime callbacks immediately copy scalar
response values into ``FuyingData``.  A schema-only response therefore turns a
perfectly valid owned Gospel into UID/level/status zero.  The four ordinary
growth operations also rely on server notifications for the state which is
not present in their response messages.

This layer keeps the account's existing ``gospels`` list authoritative.  It
derives every progression boundary from the defined tables named below and
returns a top-level ``changes`` set suitable for ``PersistentStateStore``'s
atomic shallow-merge contract.
"""

from __future__ import annotations

import copy
import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Dict, Iterable, Optional, Tuple, Union

try:  # Package imports in tests; fallbacks support direct server execution.
    from .collectibles import CollectiblesLayer, NOTIFY_UPDATE_DECORATION_ITEM
    from .combat_attribute_resolver import CombatAttributeResolver
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
    from collectibles import CollectiblesLayer, NOTIFY_UPDATE_DECORATION_ITEM  # type: ignore
    from combat_attribute_resolver import CombatAttributeResolver  # type: ignore
    from content_profile import ContentCatalog, default_content_catalog  # type: ignore
    from content_responses import ProtobufDecodeError, iter_wire_fields  # type: ignore
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


CS_GOSPEL_ENHANCE = 28
SC_GOSPEL_ENHANCE_RET = 29
CS_GOSPEL_BREAKTHROUGH = 30
SC_GOSPEL_BREAKTHROUGH_RET = 31
CS_GOSPEL_AWAKE = 32
SC_GOSPEL_AWAKE_RET = 33
CS_GOSPEL_SKILL_ENHANCE = 68
SC_GOSPEL_SKILL_ENHANCE_RET = 69
CS_ADJUST_GOSPEL_FAKE_LEVEL = 310
SC_ADJUST_GOSPEL_FAKE_LEVEL_RET = 311
CS_ADJUST_GOSPEL_FAKE_AWAKE_LEVEL = 405
SC_ADJUST_GOSPEL_FAKE_AWAKE_LEVEL_RET = 406
CS_BREAKTHROUGH_GOSPEL_FONDNESS_LIMIT = 574
SC_BREAKTHROUGH_GOSPEL_FONDNESS_LIMIT_RET = 575
CS_CHOOSE_GOSPEL_SPIRIT_STATUS = 786
SC_CHOOSE_GOSPEL_SPIRIT_STATUS_RET = 787
CS_ADJUST_GOSPEL_FAKE_SKILL_LEVEL = 791
SC_ADJUST_GOSPEL_FAKE_SKILL_LEVEL_RET = 792
CS_ADJUST_GOSPEL_FONDNESS_SWITCH = 793
SC_ADJUST_GOSPEL_FONDNESS_SWITCH_RET = 794
CS_BREAKTHROUGH_GOSPEL_SPIRIT = 873
SC_BREAKTHROUGH_GOSPEL_SPIRIT_RET = 874

NOTIFY_GOSPEL_LEVEL_UP = 18
NOTIFY_UPDATE_GOSPEL = 42
NOTIFY_GOSPEL_SPIRIT_IS_READY = 941

RESPONSE_IDS = MappingProxyType(
    {
        CS_GOSPEL_ENHANCE: SC_GOSPEL_ENHANCE_RET,
        CS_GOSPEL_BREAKTHROUGH: SC_GOSPEL_BREAKTHROUGH_RET,
        CS_GOSPEL_AWAKE: SC_GOSPEL_AWAKE_RET,
        CS_GOSPEL_SKILL_ENHANCE: SC_GOSPEL_SKILL_ENHANCE_RET,
        CS_ADJUST_GOSPEL_FAKE_LEVEL: SC_ADJUST_GOSPEL_FAKE_LEVEL_RET,
        CS_ADJUST_GOSPEL_FAKE_AWAKE_LEVEL: (
            SC_ADJUST_GOSPEL_FAKE_AWAKE_LEVEL_RET
        ),
        CS_BREAKTHROUGH_GOSPEL_FONDNESS_LIMIT: (
            SC_BREAKTHROUGH_GOSPEL_FONDNESS_LIMIT_RET
        ),
        CS_CHOOSE_GOSPEL_SPIRIT_STATUS: SC_CHOOSE_GOSPEL_SPIRIT_STATUS_RET,
        CS_ADJUST_GOSPEL_FAKE_SKILL_LEVEL: (
            SC_ADJUST_GOSPEL_FAKE_SKILL_LEVEL_RET
        ),
        CS_ADJUST_GOSPEL_FONDNESS_SWITCH: (
            SC_ADJUST_GOSPEL_FONDNESS_SWITCH_RET
        ),
        CS_BREAKTHROUGH_GOSPEL_SPIRIT: SC_BREAKTHROUGH_GOSPEL_SPIRIT_RET,
    }
)

RESPONSE_MESSAGES = MappingProxyType(
    {
        CS_GOSPEL_ENHANCE: "SC_GospelEnhanceRet",
        CS_GOSPEL_BREAKTHROUGH: "SC_GospelBreakthroughRet",
        CS_GOSPEL_AWAKE: "SC_GospelAwakeRet",
        CS_GOSPEL_SKILL_ENHANCE: "SC_GospelSkillEnhanceRet",
        CS_ADJUST_GOSPEL_FAKE_LEVEL: "SC_AdjustGospelFakeLevelRet",
        CS_ADJUST_GOSPEL_FAKE_AWAKE_LEVEL: (
            "SC_AdjustGospelFakeAwakeLevelRet"
        ),
        CS_BREAKTHROUGH_GOSPEL_FONDNESS_LIMIT: (
            "SC_BreakthroughGospelFondnessLimitRet"
        ),
        CS_CHOOSE_GOSPEL_SPIRIT_STATUS: "SC_ChooseGospelSpiritStatusRet",
        CS_ADJUST_GOSPEL_FAKE_SKILL_LEVEL: (
            "SC_AdjustGospelFakeSkillLevelRet"
        ),
        CS_ADJUST_GOSPEL_FONDNESS_SWITCH: (
            "SC_AdjustGospelFondnessSwitchRet"
        ),
        CS_BREAKTHROUGH_GOSPEL_SPIRIT: "SC_BreakthroughGospelSpiritRet",
    }
)

# Values are literal members of Msg.ErrorCode in the defined managed
# assembly.  They deliberately distinguish malformed requests, missing owned
# entities, stage-cap failures, and display levels above their real values.
ERROR_SUCCESS = 0
ERROR_ROLE_ID_ILLEGAL = 104
ERROR_GOLD_NOT_ENOUGH = 108
ERROR_ITEM_NUM_NOT_ENOUGH = 110
ERROR_ALREADY_HIGHEST_LIMIT_LEVEL = 112
ERROR_ALREADY_REACH_LIMIT_LEVEL = 114
ERROR_MATERIAL_ILLEGAL = 117
ERROR_GOSPEL_INCREMENT_EXP_EMPTY = 131
ERROR_FAKE_LEVEL_CANNOT_EXCEED_REAL = 184
ERROR_SKILL_ALREADY_HIGHEST_LEVEL = 190
ERROR_SKILL_NOT_FOUND = 191
ERROR_GOSPEL_LEVEL_NOT_ENOUGH = 207
ERROR_FONDNESS_LEVEL_NOT_REACH_HIGHEST = 255
ERROR_CHOOSE_GOSPEL_SPIRIT_EXCEED_MAX = 297
ERROR_CHOOSE_GOSPEL_SPIRIT_NOT_BREAKTHROUGH_YET = 298
ERROR_LEVEL_NOT_REACHED_LEVEL_LIMIT = 319
ERROR_ALREADY_HIGHEST_SPIRIT_BREAKTHROUGH_NUM = 320
ERROR_CANNOT_BREAKTHROUGH_IN_SPIRIT_STAGE = 322
ERROR_GOSPEL_NOT_FOUND = 424
ERROR_GOSPEL_BREAK_CONFIG = 438
ERROR_GOSPEL_BREAK_LEVEL = 439
ERROR_EXCEED_ITEM_LIMIT_COUNT = 429
ERROR_MESSAGE_DATA_ILLEGAL = 32769

RESOURCE_DIAMOND = 0
RESOURCE_MINI_DIAMOND = 1
RESOURCE_GOLD = 2
RESOURCE_ENERGY = 3
RESOURCE_ITEM = 4
RESOURCE_EXP = 5
RESOURCE_GOSPEL_EXP = 9

RESOURCE_TOP_LEVEL_FIELDS = MappingProxyType(
    {
        RESOURCE_DIAMOND: "diamond",
        RESOURCE_MINI_DIAMOND: "minidiamond",
        RESOURCE_GOLD: "gold",
        RESOURCE_ENERGY: "energy",
        RESOURCE_EXP: "exp",
    }
)

GOSPEL_EXP_RATIO_CONFIG_ID = 9_990_118
GOSPEL_EXP_GOLD_RATE_CONFIG_ID = 9_990_369

UINT32_MAX = (1 << 32) - 1
UINT64_MAX = (1 << 64) - 1

DEFAULT_HANDLER_DEPENDENCIES = (
    Path(__file__).resolve().parents[1]
    / "generated"
    / "protocol"
    / "handler_dependencies.json"
)


class GospelProgressionCatalogError(ValueError):
    """Raised when the five progression tables do not form a closed model."""


class GospelProtocolContractError(ValueError):
    """Raised when generated command/schema/handler validation has drifted."""


class GospelProfileError(ValueError):
    """Raised when persisted Gospel state cannot be safely updated."""


class GospelEconomyError(ValueError):
    """Raised when one Gospel transaction cannot be applied atomically."""

    def __init__(self, error_code: int, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


@dataclass(frozen=True)
class SpiritStage:
    break_time: int
    level: int
    spirit_level: int
    star_up: int
    initial_stance: int
    ascension_items: Tuple[Mapping[str, int], ...]
    rewards: Tuple[Mapping[str, int], ...]


@dataclass(frozen=True)
class GospelDefinition:
    gospel_id: int
    rarity: int
    states: Mapping[int, Mapping[str, Any]]
    awake_levels: Tuple[int, ...]
    break_limits: Tuple[int, ...]
    break_rewards: Tuple[Tuple[Mapping[str, int], ...], ...]
    ascension_costs: Tuple[Tuple[Mapping[str, int], ...], ...]
    awaken_costs: Tuple[Tuple[Mapping[str, int], ...], ...]
    skill_limits: Tuple[int, ...]
    skill_costs: Mapping[
        int, Mapping[int, Tuple[Mapping[str, int], ...]]
    ]
    spirit_stages: Tuple[SpiritStage, ...]

    @property
    def max_level(self) -> int:
        return self.spirit_stages[-1].level

    @property
    def max_awake_level(self) -> int:
        return self.awake_levels[-1]

    @property
    def max_skill_level(self) -> int:
        return self.skill_limits[-1]

    @property
    def max_spirit_breakthrough(self) -> int:
        return self.spirit_stages[-1].break_time

    @property
    def max_spirit_level(self) -> int:
        return self.spirit_stages[-1].spirit_level

    @property
    def max_star_up(self) -> int:
        return sum(stage.star_up for stage in self.spirit_stages)


@dataclass(frozen=True)
class GospelProgressionResult:
    response_id: int
    body: bytes
    changes: Dict[str, Any] = field(default_factory=dict)
    notifications: Tuple[Tuple[int, bytes], ...] = ()
    error_code: int = ERROR_SUCCESS
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.error_code == ERROR_SUCCESS


def _config_int(
    value: Any,
    label: str,
    *,
    minimum: int = 0,
    maximum: int = UINT32_MAX,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise GospelProgressionCatalogError(f"{label} must be an integer")
    if value < minimum or value > maximum:
        raise GospelProgressionCatalogError(
            f"{label} is outside {minimum}..{maximum}"
        )
    return value


def _json_array(value: Any, label: str) -> list[Any]:
    if not isinstance(value, str):
        raise GospelProgressionCatalogError(f"{label} must be JSON text")
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise GospelProgressionCatalogError(f"{label} is invalid JSON") from exc
    if not isinstance(parsed, list):
        raise GospelProgressionCatalogError(f"{label} must decode to an array")
    return parsed


def _resource_records(
    parsed: Sequence[Any], label: str
) -> Tuple[Mapping[str, int], ...]:
    rewards = []
    for index, item in enumerate(parsed):
        if not isinstance(item, Mapping):
            raise GospelProgressionCatalogError(
                f"{label}[{index}] must be an object"
            )
        rewards.append(
            MappingProxyType(
                {
                    "type": _config_int(
                        item.get("type"), f"{label}[{index}].type"
                    ),
                    "id": _config_int(item.get("id"), f"{label}[{index}].id"),
                    "num": _config_int(
                        item.get("num"),
                        f"{label}[{index}].num",
                        minimum=1,
                    ),
                }
            )
        )
    return tuple(rewards)


def _reward_records(value: Any, label: str) -> Tuple[Mapping[str, int], ...]:
    parsed = _json_array(value, label) if value else []
    return _resource_records(parsed, label)


def _resource_dicts(
    resources: Iterable[Mapping[str, int]],
) -> list[Dict[str, int]]:
    return [
        {
            "type": int(resource["type"]),
            "id": int(resource["id"]),
            "num": int(resource["num"]),
        }
        for resource in resources
    ]


def _coerce_catalog(
    catalog: Optional[Union[ContentCatalog, str, Path]],
) -> ContentCatalog:
    if catalog is None:
        return default_content_catalog()
    if isinstance(catalog, ContentCatalog):
        return catalog
    return ContentCatalog.load(catalog)


class GospelProgressionCatalog:
    """Validated projection of the defined Gospel progression tables."""

    REQUIRED_FIELDS = MappingProxyType(
        {
            "a_break": {"gospel_rare", "break_time", "level", "reward"},
            "a_global": {"id", "int_value", "string_value"},
            "a_gospel": {
                "id",
                "gospel_rare",
                "skill",
                "awaken_item",
                "awaken_general_item",
            },
            "a_gospel_awake": {"gospel_id", "awaken_level"},
            "a_gospelascension": {
                "gospel_id",
                "break_skill_level",
                "ascension_item_1",
                "ascension_item_2",
                "ascension_item_3",
            },
            "a_gospelstate": {
                "gospel_id",
                "level",
                "exp",
                "atk",
                "arm",
                "hp",
            },
            "a_item": {
                "id",
                "type",
                "sub_type",
                "add_num",
                "max_stack_num",
            },
            "a_skill": {"id", "upgrade_max", "enhance_Item"},
            "a_spiritbreak": {
                "gospel_rare",
                "break_time",
                "level",
                "spirit_level",
                "star_up",
                "initial_stance",
                "ascension_item",
                "rewards",
            },
        }
    )

    def __init__(self, catalog: ContentCatalog) -> None:
        self.content_catalog = catalog
        tables = {}
        for name, required in self.REQUIRED_FIELDS.items():
            try:
                table = catalog.table(name)
            except KeyError:
                raise GospelProgressionCatalogError(
                    f"catalog is missing {name}"
                ) from None
            missing = required - set(table.fields)
            if missing:
                raise GospelProgressionCatalogError(
                    f"{name} is missing required fields: {sorted(missing)}"
                )
            tables[name] = table

        item_rows: Dict[int, Mapping[str, Any]] = {}
        item_max_stacks: Dict[int, int] = {}
        for row in tables["a_item"].records:
            item_id = _config_int(row.get("id"), "a_item.id", minimum=1)
            if item_id in item_rows:
                raise GospelProgressionCatalogError(
                    f"a_item duplicates item {item_id}"
                )
            item_rows[item_id] = row
            item_max_stacks[item_id] = _config_int(
                row.get("max_stack_num"),
                f"a_item[{item_id}].max_stack_num",
            )

        def validate_resources(
            resources: Tuple[Mapping[str, int], ...],
            label: str,
            *,
            cost: bool,
            item_only: bool = False,
            allow_empty: bool = False,
        ) -> Tuple[Mapping[str, int], ...]:
            if not resources and not allow_empty:
                raise GospelProgressionCatalogError(f"{label} is empty")
            seen = set()
            for resource in resources:
                resource_type = int(resource["type"])
                item_id = int(resource["id"])
                key = (resource_type, item_id)
                if key in seen:
                    raise GospelProgressionCatalogError(
                        f"{label} duplicates resource {resource_type}:{item_id}"
                    )
                seen.add(key)
                if resource_type == RESOURCE_ITEM:
                    if item_id <= 0 or item_id not in item_rows:
                        raise GospelProgressionCatalogError(
                            f"{label} references missing a_item[{item_id}]"
                        )
                    if item_max_stacks[item_id] <= 0:
                        raise GospelProgressionCatalogError(
                            f"{label} references non-storable a_item[{item_id}]"
                        )
                    continue
                if item_only:
                    raise GospelProgressionCatalogError(
                        f"{label} must contain only item resources"
                    )
                if cost:
                    if resource_type != RESOURCE_GOLD or item_id != 0:
                        raise GospelProgressionCatalogError(
                            f"{label} contains unsupported cost {resource_type}:{item_id}"
                        )
                elif (
                    resource_type not in RESOURCE_TOP_LEVEL_FIELDS
                    or item_id != 0
                ):
                    raise GospelProgressionCatalogError(
                        f"{label} contains unsupported reward {resource_type}:{item_id}"
                    )
            return resources

        def parsed_resources(
            value: Any,
            label: str,
            *,
            cost: bool,
            item_only: bool = False,
            allow_empty: bool = False,
        ) -> Tuple[Mapping[str, int], ...]:
            return validate_resources(
                _reward_records(value, label),
                label,
                cost=cost,
                item_only=item_only,
                allow_empty=allow_empty,
            )

        global_rows = {
            _config_int(row.get("id"), "a_global.id", minimum=1): row
            for row in tables["a_global"].records
        }
        ratio_row = global_rows.get(GOSPEL_EXP_RATIO_CONFIG_ID)
        rate_row = global_rows.get(GOSPEL_EXP_GOLD_RATE_CONFIG_ID)
        if ratio_row is None or rate_row is None:
            raise GospelProgressionCatalogError(
                "a_global is missing Gospel EXP economy configuration"
            )
        exp_ratio_percent = _config_int(
            ratio_row.get("int_value"),
            f"a_global[{GOSPEL_EXP_RATIO_CONFIG_ID}].int_value",
            minimum=1,
        )
        raw_gold_rates = _json_array(
            rate_row.get("string_value"),
            f"a_global[{GOSPEL_EXP_GOLD_RATE_CONFIG_ID}].string_value",
        )
        gospel_exp_gold_rates = tuple(
            _config_int(
                value,
                f"a_global[{GOSPEL_EXP_GOLD_RATE_CONFIG_ID}].string_value",
                minimum=1,
            )
            for value in raw_gold_rates
        )
        if len(gospel_exp_gold_rates) != 4:
            raise GospelProgressionCatalogError(
                "Gospel EXP gold rates must contain four break stages"
            )

        gospel_exp_by_item: Dict[int, int] = {}
        gospel_exp_gold_by_item: Dict[int, Tuple[int, ...]] = {}
        for item_id, row in item_rows.items():
            if int(row.get("type", -1)) != 6 or int(row.get("sub_type", -1)) != 2:
                continue
            additions = _reward_records(
                row.get("add_num"), f"a_item[{item_id}].add_num"
            )
            if (
                len(additions) != 1
                or int(additions[0]["type"]) != RESOURCE_GOSPEL_EXP
                or int(additions[0]["id"]) != 0
            ):
                continue
            base_exp = int(additions[0]["num"])
            effective_exp = base_exp * exp_ratio_percent // 100
            if effective_exp <= 0:
                raise GospelProgressionCatalogError(
                    f"a_item[{item_id}].add_num has no effective Gospel EXP"
                )
            gospel_exp_by_item[item_id] = effective_exp
            gospel_exp_gold_by_item[item_id] = tuple(
                base_exp * rate // 10_000 for rate in gospel_exp_gold_rates
            )
            if any(value <= 0 for value in gospel_exp_gold_by_item[item_id]):
                raise GospelProgressionCatalogError(
                    f"a_item[{item_id}] has a zero Gospel EXP gold cost"
                )
        if not gospel_exp_by_item:
            raise GospelProgressionCatalogError(
                "a_item has no storable Gospel EXP materials"
            )

        break_rows: Dict[int, list[Mapping[str, Any]]] = {}
        for row in tables["a_break"].records:
            rarity = _config_int(row.get("gospel_rare"), "a_break.gospel_rare")
            break_rows.setdefault(rarity, []).append(row)
        if tuple(sorted(break_rows)) != (3, 4, 5):
            raise GospelProgressionCatalogError(
                "a_break rarities must be exactly 3, 4, and 5"
            )

        break_limits: Dict[int, Tuple[int, ...]] = {}
        break_rewards: Dict[
            int, Tuple[Tuple[Mapping[str, int], ...], ...]
        ] = {}
        for rarity, rows in break_rows.items():
            ordered = sorted(rows, key=lambda row: int(row["break_time"]))
            times = tuple(
                _config_int(row.get("break_time"), "a_break.break_time")
                for row in ordered
            )
            limits = tuple(
                _config_int(row.get("level"), "a_break.level", minimum=1)
                for row in ordered
            )
            if times != tuple(range(len(ordered))) or len(ordered) != 4:
                raise GospelProgressionCatalogError(
                    f"a_break rarity {rarity} must contain stages 0..3"
                )
            if tuple(sorted(set(limits))) != limits:
                raise GospelProgressionCatalogError(
                    f"a_break rarity {rarity} levels must strictly increase"
                )
            break_limits[rarity] = limits
            break_rewards[rarity] = tuple(
                parsed_resources(
                    row.get("reward"),
                    f"a_break[{row.get('id')}].reward",
                    cost=False,
                    allow_empty=True,
                )
                for row in ordered
            )

        spirit_rows: Dict[int, list[Mapping[str, Any]]] = {}
        for row in tables["a_spiritbreak"].records:
            rarity = _config_int(
                row.get("gospel_rare"), "a_spiritbreak.gospel_rare"
            )
            if rarity not in break_rows:
                raise GospelProgressionCatalogError(
                    f"a_spiritbreak has unsupported rarity {rarity}"
                )
            spirit_rows.setdefault(rarity, []).append(row)

        stages_by_rarity: Dict[int, Tuple[SpiritStage, ...]] = {}
        for rarity in break_rows:
            ordered = sorted(
                spirit_rows.get(rarity, []),
                key=lambda row: int(row["break_time"]),
            )
            stages = tuple(
                SpiritStage(
                    break_time=_config_int(
                        row.get("break_time"), "a_spiritbreak.break_time"
                    ),
                    level=_config_int(
                        row.get("level"),
                        "a_spiritbreak.level",
                        minimum=1,
                    ),
                    spirit_level=_config_int(
                        row.get("spirit_level"),
                        "a_spiritbreak.spirit_level",
                        minimum=1,
                    ),
                    star_up=_config_int(
                        row.get("star_up"), "a_spiritbreak.star_up"
                    ),
                    initial_stance=_config_int(
                        row.get("initial_stance"),
                        "a_spiritbreak.initial_stance",
                    ),
                    ascension_items=parsed_resources(
                        row.get("ascension_item"),
                        f"a_spiritbreak[{row.get('id')}].ascension_item",
                        cost=True,
                    ),
                    rewards=parsed_resources(
                        row.get("rewards"),
                        f"a_spiritbreak[{row.get('id')}].rewards",
                        cost=False,
                        allow_empty=True,
                    ),
                )
                for row in ordered
            )
            if tuple(stage.break_time for stage in stages) != (0, 1, 2):
                raise GospelProgressionCatalogError(
                    f"a_spiritbreak rarity {rarity} must contain stages 0..2"
                )
            if tuple(stage.level for stage in stages) != tuple(
                sorted({stage.level for stage in stages})
            ):
                raise GospelProgressionCatalogError(
                    f"a_spiritbreak rarity {rarity} levels must increase"
                )
            if tuple(stage.spirit_level for stage in stages) != tuple(
                sorted({stage.spirit_level for stage in stages})
            ):
                raise GospelProgressionCatalogError(
                    f"a_spiritbreak rarity {rarity} spirit levels must increase"
                )
            if sum(stage.star_up for stage in stages) != 5 - rarity:
                raise GospelProgressionCatalogError(
                    f"a_spiritbreak rarity {rarity} has an invalid star delta"
                )
            stages_by_rarity[rarity] = stages

        awake_by_gospel: Dict[int, set[int]] = {}
        for row in tables["a_gospel_awake"].records:
            gospel_id = _config_int(
                row.get("gospel_id"), "a_gospel_awake.gospel_id", minimum=1
            )
            level = _config_int(
                row.get("awaken_level"),
                f"a_gospel_awake[{row.get('id')}].awaken_level",
                minimum=1,
            )
            if level in awake_by_gospel.setdefault(gospel_id, set()):
                raise GospelProgressionCatalogError(
                    f"a_gospel_awake duplicates Gospel {gospel_id} level {level}"
                )
            awake_by_gospel[gospel_id].add(level)
        if not awake_by_gospel:
            raise GospelProgressionCatalogError("a_gospel_awake is empty")

        ascension_by_gospel: Dict[int, Mapping[str, Any]] = {}
        ascension_costs_by_gospel: Dict[
            int, Tuple[Tuple[Mapping[str, int], ...], ...]
        ] = {}
        for row in tables["a_gospelascension"].records:
            gospel_id = _config_int(
                row.get("gospel_id"), "a_gospelascension.gospel_id", minimum=1
            )
            if gospel_id in ascension_by_gospel:
                raise GospelProgressionCatalogError(
                    f"a_gospelascension duplicates Gospel {gospel_id}"
                )
            ascension_by_gospel[gospel_id] = row
            ascension_costs_by_gospel[gospel_id] = tuple(
                parsed_resources(
                    row.get(f"ascension_item_{position}"),
                    (
                        f"a_gospelascension[{row.get('id')}]"
                        f".ascension_item_{position}"
                    ),
                    cost=True,
                )
                for position in range(1, 4)
            )

        awake_ids = tuple(sorted(awake_by_gospel))
        missing_ascension = set(awake_ids) - set(ascension_by_gospel)
        if missing_ascension:
            raise GospelProgressionCatalogError(
                f"a_gospelascension is missing Gospels: {sorted(missing_ascension)}"
            )

        gospel_rows: Dict[int, Mapping[str, Any]] = {}
        for row in tables["a_gospel"].records:
            gospel_id = _config_int(row.get("id"), "a_gospel.id", minimum=1)
            if gospel_id in gospel_rows:
                raise GospelProgressionCatalogError(
                    f"a_gospel duplicates Gospel {gospel_id}"
                )
            gospel_rows[gospel_id] = row
        missing_gospels = set(awake_ids) - set(gospel_rows)
        if missing_gospels:
            raise GospelProgressionCatalogError(
                f"a_gospel is missing Gospels: {sorted(missing_gospels)}"
            )

        skill_rows: Dict[int, Mapping[str, Any]] = {}
        for row in tables["a_skill"].records:
            skill_id = _config_int(row.get("id"), "a_skill.id", minimum=1)
            if skill_id in skill_rows:
                raise GospelProgressionCatalogError(
                    f"a_skill duplicates skill {skill_id}"
                )
            skill_rows[skill_id] = row

        states_by_gospel: Dict[int, Dict[int, Mapping[str, Any]]] = {}
        for row in tables["a_gospelstate"].records:
            gospel_id = _config_int(
                row.get("gospel_id"), "a_gospelstate.gospel_id", minimum=1
            )
            level = _config_int(
                row.get("level"), "a_gospelstate.level", minimum=1
            )
            states = states_by_gospel.setdefault(gospel_id, {})
            if level in states:
                raise GospelProgressionCatalogError(
                    f"a_gospelstate duplicates Gospel {gospel_id} level {level}"
                )
            states[level] = row

        definitions: Dict[int, GospelDefinition] = {}
        for gospel_id in awake_ids:
            awake_levels = tuple(sorted(awake_by_gospel[gospel_id]))
            if awake_levels != (1, 2, 3, 4, 5, 6):
                raise GospelProgressionCatalogError(
                    f"Gospel {gospel_id} awaken levels are not exactly 1..6"
                )
            ascension = ascension_by_gospel[gospel_id]
            gospel_row = gospel_rows[gospel_id]
            rarity = _config_int(
                gospel_row.get("gospel_rare"),
                f"a_gospel[{gospel_id}].gospel_rare",
            )
            if rarity not in break_limits:
                raise GospelProgressionCatalogError(
                    f"a_gospel[{gospel_id}] has unsupported rarity {rarity}"
                )
            raw_skill_limits = _json_array(
                ascension.get("break_skill_level"),
                f"a_gospelascension[{ascension.get('id')}].break_skill_level",
            )
            skill_limits = tuple(
                _config_int(
                    value,
                    f"a_gospelascension[{ascension.get('id')}].break_skill_level",
                    minimum=1,
                )
                for value in raw_skill_limits
            )
            if len(skill_limits) != len(break_limits[rarity]) or tuple(
                sorted(set(skill_limits))
            ) != skill_limits:
                raise GospelProgressionCatalogError(
                    f"Gospel {gospel_id} skill limits do not match break stages"
                )
            stages = stages_by_rarity[rarity]
            max_level = stages[-1].level
            states = states_by_gospel.get(gospel_id, {})
            missing_levels = set(range(1, max_level + 1)) - set(states)
            if missing_levels:
                raise GospelProgressionCatalogError(
                    f"a_gospelstate Gospel {gospel_id} is missing levels "
                    f"{min(missing_levels)}..{max(missing_levels)}"
                )
            if _config_int(
                states[max_level].get("exp"),
                f"a_gospelstate[{gospel_id}:{max_level}].exp",
                minimum=1,
                maximum=UINT64_MAX,
            ) <= 0:
                raise GospelProgressionCatalogError(
                    f"Gospel {gospel_id} has no max-level experience"
                )
            thresholds = tuple(
                _config_int(
                    states[level].get("exp"),
                    f"a_gospelstate[{gospel_id}:{level}].exp",
                    maximum=UINT64_MAX,
                )
                for level in range(1, max_level + 1)
            )
            if tuple(sorted(set(thresholds))) != thresholds:
                raise GospelProgressionCatalogError(
                    f"a_gospelstate Gospel {gospel_id} EXP must strictly increase"
                )

            awaken_costs = tuple(
                cost
                for cost in (
                    parsed_resources(
                        gospel_row.get("awaken_item"),
                        f"a_gospel[{gospel_id}].awaken_item",
                        cost=True,
                        item_only=True,
                        allow_empty=True,
                    ),
                    parsed_resources(
                        gospel_row.get("awaken_general_item"),
                        f"a_gospel[{gospel_id}].awaken_general_item",
                        cost=True,
                        item_only=True,
                        allow_empty=True,
                    ),
                )
                if cost
            )
            if not awaken_costs:
                raise GospelProgressionCatalogError(
                    f"a_gospel[{gospel_id}] has no awaken material option"
                )

            skill_ids_raw = _json_array(
                gospel_row.get("skill"), f"a_gospel[{gospel_id}].skill"
            )
            skill_ids = tuple(
                _config_int(
                    value,
                    f"a_gospel[{gospel_id}].skill",
                    minimum=1,
                )
                for value in skill_ids_raw
            )
            if len(skill_ids) != 3 or len(set(skill_ids)) != len(skill_ids):
                raise GospelProgressionCatalogError(
                    f"a_gospel[{gospel_id}].skill must contain three unique IDs"
                )
            skill_costs: Dict[
                int, Mapping[int, Tuple[Mapping[str, int], ...]]
            ] = {}
            for skill_id in skill_ids:
                skill_row = skill_rows.get(skill_id)
                if skill_row is None:
                    raise GospelProgressionCatalogError(
                        f"a_gospel[{gospel_id}] references missing a_skill[{skill_id}]"
                    )
                upgrade_max = _config_int(
                    skill_row.get("upgrade_max"),
                    f"a_skill[{skill_id}].upgrade_max",
                    minimum=1,
                )
                if upgrade_max != skill_limits[-1]:
                    raise GospelProgressionCatalogError(
                        f"a_skill[{skill_id}].upgrade_max does not match Gospel cap"
                    )
                raw_enhance = _json_array(
                    skill_row.get("enhance_Item"),
                    f"a_skill[{skill_id}].enhance_Item",
                )
                if len(raw_enhance) != 1 or not isinstance(
                    raw_enhance[0], Mapping
                ):
                    raise GospelProgressionCatalogError(
                        f"a_skill[{skill_id}].enhance_Item has invalid shape"
                    )
                levels = raw_enhance[0]
                by_level: Dict[int, Tuple[Mapping[str, int], ...]] = {}
                for current_level in range(1, upgrade_max):
                    key = f"level{current_level}"
                    raw_cost = levels.get(key)
                    if not isinstance(raw_cost, list):
                        raise GospelProgressionCatalogError(
                            f"a_skill[{skill_id}].enhance_Item.{key} is missing"
                        )
                    by_level[current_level] = validate_resources(
                        _resource_records(
                            raw_cost,
                            f"a_skill[{skill_id}].enhance_Item.{key}",
                        ),
                        f"a_skill[{skill_id}].enhance_Item.{key}",
                        cost=True,
                    )
                skill_costs[skill_id] = MappingProxyType(by_level)

            definitions[gospel_id] = GospelDefinition(
                gospel_id=gospel_id,
                rarity=rarity,
                states=MappingProxyType(dict(states)),
                awake_levels=awake_levels,
                break_limits=break_limits[rarity],
                break_rewards=break_rewards[rarity],
                ascension_costs=ascension_costs_by_gospel[gospel_id],
                awaken_costs=awaken_costs,
                skill_limits=skill_limits,
                skill_costs=MappingProxyType(skill_costs),
                spirit_stages=stages,
            )

        self.definitions = MappingProxyType(definitions)
        self.gospel_ids = tuple(definitions)
        self.item_max_stacks = MappingProxyType(item_max_stacks)
        self.gospel_exp_by_item = MappingProxyType(gospel_exp_by_item)
        self.gospel_exp_gold_by_item = MappingProxyType(
            gospel_exp_gold_by_item
        )
        self.gospel_exp_gold_rates = gospel_exp_gold_rates


EXPECTED_MESSAGE_FIELDS = MappingProxyType(
    {
        "CS_GospelEnhance": {"RoleId", "GospelUId", "ItemList"},
        "SC_GospelEnhanceRet": {
            "Errorcode",
            "RoleId",
            "GospelUId",
            "ItemList",
        },
        "CS_GospelBreakthrough": {"RoleId", "GospelUId", "ItemList"},
        "SC_GospelBreakthroughRet": {
            "Errorcode",
            "RoleId",
            "GospelUId",
            "ItemList",
            "LevelLimit",
        },
        "CS_GospelAwake": {"RoleId", "GospelUId", "ItemList"},
        "SC_GospelAwakeRet": {
            "Errorcode",
            "RoleId",
            "GospelUId",
            "ItemList",
        },
        "CS_GospelSkillEnhance": {"RoleId", "GospelUId", "SkillId"},
        "SC_GospelSkillEnhanceRet": {
            "Errorcode",
            "RoleId",
            "GospelUId",
            "SkillId",
            "NewLevel",
        },
        "CS_AdjustGospelFakeLevel": {"GospelUId", "FakeLevel"},
        "SC_AdjustGospelFakeLevelRet": {
            "Errorcode",
            "GospelUId",
            "FakeLevel",
        },
        "CS_AdjustGospelFakeAwakeLevel": {
            "GospelUId",
            "FakeAwakeLevel",
        },
        "SC_AdjustGospelFakeAwakeLevelRet": {
            "Errorcode",
            "GospelUId",
            "FakeAwakeLevel",
        },
        "CS_BreakthroughGospelFondnessLimit": {"GospelUId"},
        "SC_BreakthroughGospelFondnessLimitRet": {
            "Errorcode",
            "GospelUId",
            "IsFondnessLimitBreakthrough",
        },
        "CS_ChooseGospelSpiritStatus": {"GospelUId", "SpiritStatus"},
        "SC_ChooseGospelSpiritStatusRet": {
            "Errorcode",
            "GospelUId",
            "SpiritStatus",
        },
        "CS_AdjustGospelFakeSkillLevel": {
            "GospelUId",
            "SkillFakeLevelMap",
        },
        "SC_AdjustGospelFakeSkillLevelRet": {
            "Errorcode",
            "GospelUId",
            "SkillFakeLevelMap",
        },
        "CS_AdjustGospelFondnessSwitch": {
            "GospelUId",
            "FondnessSwitch",
            "SpiritSwitch",
        },
        "SC_AdjustGospelFondnessSwitchRet": {
            "Errorcode",
            "GospelUId",
            "FondnessSwitch",
            "SpiritSwitch",
        },
        "CS_BreakthroughGospelSpirit": {"GospelUId"},
        "SC_BreakthroughGospelSpiritRet": {
            "Errorcode",
            "GospelUId",
            "SpiritBreakthroughNum",
            "RewardList",
        },
        "Notify_GospelLevelUp": {"RoleId", "GospelUId", "Level", "Exp"},
        "Notify_UpdateGospel": {"GospelList"},
        "Notify_GospelSpiritIsReady": {"GospelUId", "SpiritIsReady"},
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
        if actual != expected:
            raise GospelProtocolContractError(
                f"{message_name} fields changed: expected {sorted(expected)}, "
                f"got {sorted(actual)}"
            )

    try:
        catalog_document = json.loads(protocol_catalog.read_text(encoding="utf-8"))
        dependency_document = json.loads(
            handler_dependencies.read_text(encoding="utf-8")
        )
    except (OSError, ValueError) as exc:
        raise GospelProtocolContractError(
            f"cannot read generated protocol metadata: {exc}"
        ) from exc

    response_map = catalog_document.get("request_to_response")
    if not isinstance(response_map, Mapping):
        raise GospelProtocolContractError("protocol request_to_response is missing")
    for request_id, response_id in RESPONSE_IDS.items():
        if int(response_map.get(str(request_id), -1)) != response_id:
            raise GospelProtocolContractError(
                f"protocol maps request {request_id} to an unexpected response"
            )

    endpoints = {
        int(item.get("request_id")): item
        for item in dependency_document.get("endpoints", [])
        if isinstance(item, Mapping) and isinstance(item.get("request_id"), int)
    }
    for request_id, response_id in RESPONSE_IDS.items():
        endpoint = endpoints.get(request_id)
        if endpoint is None or endpoint.get("response_id") != response_id:
            raise GospelProtocolContractError(
                f"handler dependencies are missing endpoint {request_id} -> {response_id}"
            )
        response_name = RESPONSE_MESSAGES[request_id]
        known_fields = EXPECTED_MESSAGE_FIELDS[response_name]
        for field_info in endpoint.get("response_fields_read", []):
            field_name = str(field_info.get("field"))
            if field_name not in known_fields:
                raise GospelProtocolContractError(
                    f"handler for {request_id} reads unknown field {field_name}"
                )


def _profile_uint(
    source: Mapping[str, Any],
    key: str,
    default: int,
    *,
    minimum: int = 0,
    maximum: int = UINT64_MAX,
) -> int:
    if key not in source:
        return default
    value = source.get(key)
    if isinstance(value, bool):
        raise GospelProfileError(f"Gospel {key} must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise GospelProfileError(f"Gospel {key} must be an integer") from exc
    if parsed < minimum or parsed > maximum:
        raise GospelProfileError(
            f"Gospel {key} is outside {minimum}..{maximum}"
        )
    return parsed


def _wire_field_map(data: bytes) -> Dict[int, list[Tuple[int, object]]]:
    fields: Dict[int, list[Tuple[int, object]]] = {}
    for number, wire_type, value in iter_wire_fields(bytes(data)):
        fields.setdefault(number, []).append((wire_type, value))
    return fields


def _required_uint(
    fields: Mapping[int, list[Tuple[int, object]]],
    number: int,
    *,
    minimum: int = 0,
    maximum: int = UINT64_MAX,
) -> int:
    values = fields.get(number, [])
    if len(values) != 1 or values[0][0] != 0:
        raise ProtobufDecodeError(
            f"field {number} must be one varint"
        )
    parsed = int(values[0][1])
    if parsed < minimum or parsed > maximum:
        raise ProtobufDecodeError(
            f"field {number} is outside {minimum}..{maximum}"
        )
    return parsed


def _materials(
    fields: Mapping[int, list[Tuple[int, object]]], number: int
) -> Tuple[Mapping[str, int], ...]:
    result = []
    for wire_type, raw in fields.get(number, []):
        if wire_type != 2 or not isinstance(raw, bytes):
            raise ProtobufDecodeError(f"field {number} is not a material message")
        material_fields = _wire_field_map(raw)
        result.append(
            MappingProxyType(
                {
                    "ItemUId": _required_uint(
                        material_fields, 1, minimum=1, maximum=UINT64_MAX
                    ),
                    "ItemNum": _required_uint(
                        material_fields, 2, minimum=1, maximum=UINT32_MAX
                    ),
                }
            )
        )
    return tuple(result)


def _skill_fake_map(
    fields: Mapping[int, list[Tuple[int, object]]], number: int
) -> Dict[int, int]:
    result: Dict[int, int] = {}
    for wire_type, raw in fields.get(number, []):
        if wire_type != 2 or not isinstance(raw, bytes):
            raise ProtobufDecodeError(f"field {number} is not a map entry")
        entry = _wire_field_map(raw)
        key = _required_uint(entry, 1, minimum=1, maximum=UINT32_MAX)
        value = _required_uint(entry, 2, minimum=1, maximum=UINT32_MAX)
        if key in result:
            raise ProtobufDecodeError(f"field {number} duplicates key {key}")
        result[key] = value
    if not result:
        raise ProtobufDecodeError(f"field {number} must not be empty")
    return result


class GospelProgressionLayer:
    """Pure-state implementation of the eleven runtime-reachable endpoints."""

    def __init__(
        self,
        catalog: Optional[Union[ContentCatalog, str, Path]] = None,
        *,
        codec: Optional[ProtocolMessageCodec] = None,
        protocol_catalog: Optional[Path] = None,
        handler_dependencies: Optional[Path] = None,
        combat_resolver: Optional[CombatAttributeResolver] = None,
        collectibles: Optional[CollectiblesLayer] = None,
    ) -> None:
        self.content_catalog = _coerce_catalog(catalog)
        self.catalog = GospelProgressionCatalog(self.content_catalog)
        self.combat_resolver = combat_resolver or CombatAttributeResolver(
            self.content_catalog
        )
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
        self.collectibles = collectibles or CollectiblesLayer(self.content_catalog, codec=self.codec)
        _validate_protocol_contract(
            self.codec,
            protocol_path,
            dependency_path,
        )

    @staticmethod
    def _normalize_skills(
        source: Mapping[str, Any], definition: GospelDefinition
    ) -> list[Dict[str, int]]:
        raw = source.get("active_skills", source.get("skills", []))
        if raw is None:
            raw = []
        if not isinstance(raw, list):
            raise GospelProfileError("Gospel active_skills must be a list")
        skills = []
        seen = set()
        for index, item in enumerate(raw):
            if not isinstance(item, Mapping):
                raise GospelProfileError(
                    f"Gospel active_skills[{index}] must be an object"
                )
            skill_id = _profile_uint(
                item, "id", 0, minimum=1, maximum=UINT32_MAX
            )
            if skill_id in seen:
                raise GospelProfileError(f"duplicate Gospel skill {skill_id}")
            seen.add(skill_id)
            level = _profile_uint(
                item,
                "level",
                1,
                minimum=1,
                maximum=definition.max_skill_level,
            )
            valid_level = _profile_uint(
                item,
                "valid_level",
                level,
                minimum=1,
                maximum=level,
            )
            skills.append(
                {"id": skill_id, "level": level, "valid_level": valid_level}
            )
        return skills

    def _normalize_row(
        self, source: Mapping[str, Any], definition: GospelDefinition
    ) -> Dict[str, Any]:
        row = copy.deepcopy(dict(source))
        uid = _profile_uint(row, "uid", 0, minimum=1, maximum=UINT64_MAX)
        gospel_id = _profile_uint(
            row, "id", 0, minimum=1, maximum=UINT32_MAX
        )
        if gospel_id != definition.gospel_id:
            raise GospelProfileError(
                f"Gospel UID {uid} changed ID from {definition.gospel_id} to {gospel_id}"
            )
        level = _profile_uint(
            row, "level", 1, minimum=1, maximum=definition.max_level
        )
        if level not in definition.states:
            raise GospelProfileError(
                f"Gospel {gospel_id} has no state row for level {level}"
            )
        state_exp = _config_int(
            definition.states[level].get("exp"),
            f"a_gospelstate[{gospel_id}:{level}].exp",
            maximum=UINT64_MAX,
        )
        exp = _profile_uint(row, "exp", state_exp, maximum=UINT64_MAX)
        level_limit = _profile_uint(
            row,
            "level_limit",
            definition.break_limits[0],
            minimum=1,
            maximum=definition.break_limits[-1],
        )
        if level_limit not in definition.break_limits:
            raise GospelProfileError(
                f"Gospel {gospel_id} has unknown level limit {level_limit}"
            )
        awake = _profile_uint(
            row,
            "awake_level",
            definition.awake_levels[0],
            minimum=definition.awake_levels[0],
            maximum=definition.max_awake_level,
        )
        if awake not in definition.awake_levels:
            raise GospelProfileError(
                f"Gospel {gospel_id} has unknown awake level {awake}"
            )
        fake_level = _profile_uint(
            row, "fake_level", level, minimum=1, maximum=level
        )
        fake_awake = _profile_uint(
            row,
            "fake_awake_level",
            awake,
            minimum=1,
            maximum=awake,
        )
        fondness_break = _profile_uint(
            row, "is_fondness_limit_breakthrough", 0, maximum=1
        )
        spirit_num = _profile_uint(
            row,
            "spirit_breakthrough_num",
            0,
            maximum=definition.max_spirit_breakthrough,
        )
        default_spirit_limit = (
            definition.spirit_stages[spirit_num].spirit_level
            if fondness_break
            else 0
        )
        spirit_limit = _profile_uint(
            row,
            "spirit_level_limit",
            default_spirit_limit,
            maximum=definition.max_spirit_level,
        )
        status_max = 2 * len(definition.spirit_stages)
        status = _profile_uint(row, "spirit_status", 0, maximum=status_max)
        star_up = _profile_uint(
            row, "star_up", 0, maximum=definition.max_star_up
        )
        rewarded = source.get("rewarded_fondness_levels", [])
        if rewarded is None:
            rewarded = []
        if not isinstance(rewarded, list):
            raise GospelProfileError("rewarded_fondness_levels must be a list")
        rewarded_levels = sorted(
            {
                _profile_uint(
                    {"value": item},
                    "value",
                    0,
                    maximum=definition.max_spirit_level,
                )
                for item in rewarded
            }
        )
        raw_break_rewards = source.get("break_level_reward_taken", {})
        if raw_break_rewards is None:
            raw_break_rewards = {}
        if not isinstance(raw_break_rewards, Mapping):
            raise GospelProfileError("break_level_reward_taken must be a map")
        break_rewards: Dict[str, bool] = {}
        for key, value in raw_break_rewards.items():
            try:
                break_level = int(key)
            except (TypeError, ValueError, OverflowError) as exc:
                raise GospelProfileError("invalid break reward level") from exc
            if break_level <= 0 or break_level >= len(definition.break_limits):
                raise GospelProfileError(
                    f"invalid break reward level {break_level}"
                )
            break_rewards[str(break_level)] = bool(value)

        row.update(
            {
                "uid": uid,
                "id": gospel_id,
                "level": level,
                "exp": exp,
                "level_limit": level_limit,
                "energy": _profile_uint(
                    # Oracle work may legitimately exhaust a Gospel. Work
                    # energy is not a growth prerequisite or awakening cost.
                    row, "energy", 100, maximum=UINT32_MAX
                ),
                "active_skills": self._normalize_skills(row, definition),
                "fondness": _profile_uint(
                    row, "fondness", 0, maximum=UINT32_MAX
                ),
                "fondness_level": _profile_uint(
                    row,
                    "fondness_level",
                    0,
                    maximum=definition.max_spirit_level,
                ),
                "fondness_daily_increment": _profile_uint(
                    row, "fondness_daily_increment", 0, maximum=UINT32_MAX
                ),
                "birth_time": _profile_uint(
                    row, "birth_time", 1, minimum=1, maximum=UINT64_MAX
                ),
                "is_new": _profile_uint(row, "is_new", 0, maximum=1),
                "appearance_id": _profile_uint(
                    row, "appearance_id", gospel_id, minimum=1, maximum=UINT32_MAX
                ),
                "appearance_id_2d": _profile_uint(
                    row,
                    "appearance_id_2d",
                    _profile_uint(
                        row,
                        "appearance_id",
                        gospel_id,
                        minimum=1,
                        maximum=UINT32_MAX,
                    ),
                    minimum=1,
                    maximum=UINT32_MAX,
                ),
                "fake_level": fake_level,
                "skin_uid": _profile_uint(
                    row, "skin_uid", 0, maximum=UINT64_MAX
                ),
                "fake_awake_level": fake_awake,
                "lay_days_on_secretary": _profile_uint(
                    row, "lay_days_on_secretary", 0, maximum=UINT32_MAX
                ),
                "is_fondness_limit_breakthrough": fondness_break,
                "awake_level": awake,
                "repeated_num": _profile_uint(
                    row, "repeated_num", 0, maximum=UINT32_MAX
                ),
                "combat_power": _profile_uint(
                    row, "combat_power", 100, minimum=1, maximum=UINT32_MAX
                ),
                "spirit_status": status,
                "fondness_switch": _profile_uint(
                    row, "fondness_switch", 0, maximum=1
                ),
                "spirit_switch": _profile_uint(
                    row, "spirit_switch", 0, maximum=1
                ),
                "rewarded_fondness_levels": rewarded_levels,
                "last_work_end_time": _profile_uint(
                    row, "last_work_end_time", 0, maximum=UINT64_MAX
                ),
                "spirit_breakthrough_num": spirit_num,
                "star_up": star_up,
                "spirit_level_limit": spirit_limit,
                "spirit_is_ready": _profile_uint(
                    row, "spirit_is_ready", 0, maximum=1
                ),
                "break_level_reward_taken": break_rewards,
            }
        )
        return row

    @staticmethod
    def _enter_break_stage(row, definition, previous_limit):
        # Source FuyingData derives the ascension stage from raw level, and
        # a_gospelstate resets display level at 31/71/121. Its reset refund
        # explicitly subtracts these boundary EXP gaps: ascension pays them.
        previous_level = int(row["level"])
        level = max(previous_level, previous_limit + 1)
        row["level"] = level
        row["exp"] = max(int(row["exp"]), int(definition.states[level]["exp"]))
        if int(row.get("fake_level", previous_level)) == previous_level:
            row["fake_level"] = level

    def repair_paid_breakthroughs(self, profile):
        """Repair the old cap-only result only where a paid ledger proves it."""
        history = profile.get("resource_history", [])
        if not isinstance(history, list):
            return
        for row in profile.get("gospels", []):
            definition = self.catalog.definitions.get(int(row.get("id", 0)))
            if definition is None:
                continue
            limit = int(row.get("level_limit", 0))
            if limit not in definition.break_limits[1:]:
                continue
            position = definition.break_limits.index(limit)
            previous = definition.break_limits[position - 1]
            if int(row.get("level", 0)) != previous:
                continue
            expected = [dict(cost) for cost in definition.ascension_costs[position - 1]]
            for event in reversed(history):
                if not isinstance(event, Mapping):
                    continue
                context = event.get("context", {})
                if (event.get("source") == "gospel_progression"
                        and event.get("operation") == "breakthrough"
                        and event.get("command_id") == CS_GOSPEL_BREAKTHROUGH
                        and event.get("gospel_uid") == row.get("uid")
                        and event.get("gospel_id") == row.get("id")
                        and context.get("from_limit") == previous
                        and context.get("to_limit") == limit
                        and event.get("costs") == expected):
                    self._enter_break_stage(row, definition, previous)
                    break

    def _owned_gospel(
        self, profile: Mapping[str, Any], uid: int
    ) -> Tuple[list[Any], list[Any], int, Dict[str, Any], GospelDefinition]:
        source_rows = profile.get("gospels")
        if not isinstance(source_rows, list):
            raise GospelProfileError("profile.gospels must be a list")
        matches = []
        for index, item in enumerate(source_rows):
            if not isinstance(item, Mapping):
                continue
            try:
                candidate = _profile_uint(
                    item, "uid", 0, minimum=1, maximum=UINT64_MAX
                )
            except GospelProfileError:
                continue
            if candidate == uid:
                matches.append((index, item))
        if not matches:
            raise LookupError(f"unknown Gospel UID {uid}")
        if len(matches) != 1:
            raise GospelProfileError(f"duplicate Gospel UID {uid}")
        index, source = matches[0]
        gospel_id = _profile_uint(
            source, "id", 0, minimum=1, maximum=UINT32_MAX
        )
        definition = self.catalog.definitions.get(gospel_id)
        if definition is None:
            raise GospelProfileError(
                f"Gospel UID {uid} has unsupported config ID {gospel_id}"
            )
        original = copy.deepcopy(source_rows)
        rows = copy.deepcopy(source_rows)
        row = self._normalize_row(source, definition)
        rows[index] = row
        return original, rows, index, row, definition

    @staticmethod
    def _role_id(profile: Mapping[str, Any]) -> int:
        try:
            value = int(profile.get("role_id") or 0)
        except (TypeError, ValueError, OverflowError):
            return 0
        return value if 0 <= value <= UINT64_MAX else 0

    @staticmethod
    def _skill_map(row: Optional[Mapping[str, Any]]) -> Dict[int, int]:
        if not isinstance(row, Mapping):
            return {}
        skills = row.get("active_skills")
        if not isinstance(skills, list):
            return {}
        return {
            index: int(skill.get("valid_level", skill.get("level", 1)))
            for index, skill in enumerate(skills, 1)
            if isinstance(skill, Mapping)
        }

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
        new_level: int = 0,
        skill_map: Optional[Mapping[int, int]] = None,
        rewards: Iterable[Mapping[str, int]] = (),
    ) -> Dict[str, Any]:
        authoritative_uid = int(row.get("uid", uid)) if row is not None else uid
        values: Dict[str, Any] = {"Errorcode": error_code}
        if request_id in {
            CS_GOSPEL_ENHANCE,
            CS_GOSPEL_BREAKTHROUGH,
            CS_GOSPEL_AWAKE,
        }:
            values.update(
                {
                    "RoleId": self._role_id(profile),
                    "GospelUId": authoritative_uid,
                    "ItemList": list(materials),
                }
            )
            if request_id == CS_GOSPEL_BREAKTHROUGH:
                values["LevelLimit"] = (
                    int(row.get("level_limit", 0)) if row is not None else 0
                )
        elif request_id == CS_GOSPEL_SKILL_ENHANCE:
            values.update(
                {
                    "RoleId": self._role_id(profile),
                    "GospelUId": authoritative_uid,
                    "SkillId": skill_id,
                    "NewLevel": new_level,
                }
            )
        elif request_id == CS_ADJUST_GOSPEL_FAKE_LEVEL:
            values.update(
                {
                    "GospelUId": authoritative_uid,
                    "FakeLevel": int(row.get("fake_level", 0))
                    if row is not None
                    else 0,
                }
            )
        elif request_id == CS_ADJUST_GOSPEL_FAKE_AWAKE_LEVEL:
            values.update(
                {
                    "GospelUId": authoritative_uid,
                    "FakeAwakeLevel": int(row.get("fake_awake_level", 0))
                    if row is not None
                    else 0,
                }
            )
        elif request_id == CS_BREAKTHROUGH_GOSPEL_FONDNESS_LIMIT:
            values.update(
                {
                    "GospelUId": authoritative_uid,
                    "IsFondnessLimitBreakthrough": int(
                        row.get("is_fondness_limit_breakthrough", 0)
                    )
                    if row is not None
                    else 0,
                }
            )
        elif request_id == CS_CHOOSE_GOSPEL_SPIRIT_STATUS:
            values.update(
                {
                    "GospelUId": authoritative_uid,
                    "SpiritStatus": int(row.get("spirit_status", 0))
                    if row is not None
                    else 0,
                }
            )
        elif request_id == CS_ADJUST_GOSPEL_FAKE_SKILL_LEVEL:
            values.update(
                {
                    "GospelUId": authoritative_uid,
                    "SkillFakeLevelMap": dict(
                        skill_map if skill_map is not None else self._skill_map(row)
                    ),
                }
            )
        elif request_id == CS_ADJUST_GOSPEL_FONDNESS_SWITCH:
            values.update(
                {
                    "GospelUId": authoritative_uid,
                    "FondnessSwitch": int(row.get("fondness_switch", 0))
                    if row is not None
                    else 0,
                    "SpiritSwitch": int(row.get("spirit_switch", 0))
                    if row is not None
                    else 0,
                }
            )
        else:
            values.update(
                {
                    "GospelUId": authoritative_uid,
                    "SpiritBreakthroughNum": int(
                        row.get("spirit_breakthrough_num", 0)
                    )
                    if row is not None
                    else 0,
                    "RewardList": [
                        {
                            "ResType": int(item["type"]),
                            "ItemId": int(item["id"]),
                            "Count": int(item["num"]),
                        }
                        for item in rewards
                    ],
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
        new_level: int = 0,
        skill_map: Optional[Mapping[int, int]] = None,
        rewards: Iterable[Mapping[str, int]] = (),
        changes: Optional[Dict[str, Any]] = None,
        notifications: Tuple[Tuple[int, bytes], ...] = (),
        reason: str = "",
    ) -> GospelProgressionResult:
        values = self._response_values(
            request_id,
            error_code,
            profile,
            uid=uid,
            row=row,
            materials=materials,
            skill_id=skill_id,
            new_level=new_level,
            skill_map=skill_map,
            rewards=rewards,
        )
        return GospelProgressionResult(
            RESPONSE_IDS[request_id],
            self.codec.encode(RESPONSE_MESSAGES[request_id], values),
            changes=changes or {},
            notifications=notifications,
            error_code=error_code,
            reason=reason,
        )

    @staticmethod
    def _changes(original: list[Any], rows: list[Any]) -> Dict[str, Any]:
        return {"gospels": rows} if rows != original else {}

    @staticmethod
    def _economy_uint(value: Any, label: str) -> int:
        if isinstance(value, bool):
            raise GospelEconomyError(
                ERROR_GOSPEL_BREAK_CONFIG, f"{label} must be an integer"
            )
        try:
            parsed = int(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise GospelEconomyError(
                ERROR_GOSPEL_BREAK_CONFIG, f"{label} must be an integer"
            ) from exc
        if parsed < 0 or parsed > UINT64_MAX:
            raise GospelEconomyError(
                ERROR_GOSPEL_BREAK_CONFIG,
                f"{label} is outside 0..{UINT64_MAX}",
            )
        return parsed

    def _normalized_inventory(
        self, profile: Mapping[str, Any]
    ) -> Dict[str, Dict[str, int]]:
        try:
            return normalize_inventory(copy.deepcopy(profile.get("inventory", {})))
        except InventoryContentError as exc:
            raise GospelEconomyError(
                ERROR_GOSPEL_BREAK_CONFIG, str(exc)
            ) from exc

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
                raise GospelEconomyError(
                    ERROR_GOSPEL_BREAK_CONFIG,
                    f"resource {resource_type}:{resource_id} has no amount",
                )
            key = (resource_type, resource_id)
            if key not in totals:
                order.append(key)
                totals[key] = 0
            total = totals[key] + amount
            if total > UINT64_MAX:
                raise GospelEconomyError(
                    ERROR_GOSPEL_BREAK_CONFIG,
                    f"resource {resource_type}:{resource_id} exceeds uint64",
                )
            totals[key] = total
        return tuple(
            MappingProxyType(
                {"type": key[0], "id": key[1], "num": totals[key]}
            )
            for key in order
        )

    def _requested_item_costs(
        self,
        profile: Mapping[str, Any],
        materials: Sequence[Mapping[str, int]],
        *,
        allowed_item_ids: Optional[Iterable[int]] = None,
    ) -> Tuple[Tuple[Mapping[str, int], ...], Dict[str, Dict[str, int]]]:
        inventory = self._normalized_inventory(profile)
        uid_to_item: Dict[int, int] = {}
        for key, item in inventory.items():
            item_id = int(item["id"])
            uid = int(item["uid"])
            if uid in uid_to_item and uid_to_item[uid] != item_id:
                raise GospelEconomyError(
                    ERROR_GOSPEL_BREAK_CONFIG,
                    f"inventory UID {uid} is not unique",
                )
            if str(item_id) != key:
                raise GospelEconomyError(
                    ERROR_GOSPEL_BREAK_CONFIG,
                    f"inventory key {key} does not match item {item_id}",
                )
            uid_to_item[uid] = item_id

        allowed = (
            {int(item_id) for item_id in allowed_item_ids}
            if allowed_item_ids is not None
            else None
        )
        seen_uids = set()
        requested = []
        for material in materials:
            uid = int(material["ItemUId"])
            amount = int(material["ItemNum"])
            if uid in seen_uids:
                raise GospelEconomyError(
                    ERROR_MATERIAL_ILLEGAL,
                    f"material UID {uid} is duplicated",
                )
            seen_uids.add(uid)
            item_id = uid_to_item.get(uid)
            if item_id is None:
                raise GospelEconomyError(
                    ERROR_MATERIAL_ILLEGAL,
                    f"material UID {uid} is not in the account inventory",
                )
            if allowed is not None and item_id not in allowed:
                raise GospelEconomyError(
                    ERROR_MATERIAL_ILLEGAL,
                    f"item {item_id} is not valid for this Gospel operation",
                )
            requested.append(
                MappingProxyType(
                    {
                        "type": RESOURCE_ITEM,
                        "id": item_id,
                        "num": amount,
                    }
                )
            )
        return self._aggregate_resources(requested), inventory

    @staticmethod
    def _item_signature(
        resources: Iterable[Mapping[str, int]],
    ) -> Dict[int, int]:
        return {
            int(resource["id"]): int(resource["num"])
            for resource in resources
            if int(resource["type"]) == RESOURCE_ITEM
        }

    @staticmethod
    def _configured_materials(
        resources: Iterable[Mapping[str, int]],
        inventory: Mapping[str, Mapping[str, int]],
    ) -> Tuple[Mapping[str, int], ...]:
        materials = []
        for resource in resources:
            if int(resource["type"]) != RESOURCE_ITEM:
                continue
            item_id = int(resource["id"])
            item = inventory.get(str(item_id))
            uid = (
                int(item["uid"])
                if isinstance(item, Mapping)
                else stable_item_uid(item_id)
            )
            materials.append(
                MappingProxyType(
                    {"ItemUId": uid, "ItemNum": int(resource["num"])}
                )
            )
        return tuple(materials)

    def _economy_changes(
        self,
        profile: Mapping[str, Any],
        *,
        request_id: int,
        operation: str,
        row: Mapping[str, Any],
        costs: Iterable[Mapping[str, int]],
        rewards: Iterable[Mapping[str, int]] = (),
        context: Optional[Mapping[str, Any]] = None,
        allow_saturated_rewards: bool = True,
    ) -> Dict[str, Any]:
        """Preflight and project one JSON-safe, all-or-nothing transaction."""

        aggregated_costs = self._aggregate_resources(costs)
        aggregated_rewards = self._aggregate_resources(rewards)
        inventory = self._normalized_inventory(profile)

        resource_history_value = profile.get("resource_history", [])
        reward_history_value = profile.get("reward_history", [])
        if not isinstance(resource_history_value, list):
            raise GospelEconomyError(
                ERROR_GOSPEL_BREAK_CONFIG,
                "profile.resource_history must be a list",
            )
        if not isinstance(reward_history_value, list):
            raise GospelEconomyError(
                ERROR_GOSPEL_BREAK_CONFIG,
                "profile.reward_history must be a list",
            )
        resource_history = copy.deepcopy(resource_history_value)
        reward_history = copy.deepcopy(reward_history_value)

        scalar_values: Dict[str, int] = {}
        touched_scalars = set()
        has_item_resource = False

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
                has_item_resource = True
                key = str(resource_id)
                item = inventory.get(key)
                current = int(item["count"]) if item is not None else 0
                if current < amount:
                    raise GospelEconomyError(
                        ERROR_ITEM_NUM_NOT_ENOUGH,
                        f"item {resource_id} needs {amount}, account has {current}",
                    )
                assert item is not None
                item["count"] = current - amount
                continue
            field_name = RESOURCE_TOP_LEVEL_FIELDS.get(resource_type)
            if field_name is None or resource_id != 0:
                raise GospelEconomyError(
                    ERROR_GOSPEL_BREAK_CONFIG,
                    f"unsupported Gospel cost {resource_type}:{resource_id}",
                )
            current = scalar_value(field_name)
            if current < amount:
                error_code = (
                    ERROR_GOLD_NOT_ENOUGH
                    if resource_type == RESOURCE_GOLD
                    else ERROR_ITEM_NUM_NOT_ENOUGH
                )
                raise GospelEconomyError(
                    error_code,
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
                has_item_resource = True
                key = str(resource_id)
                item = inventory.get(key)
                current = int(item["count"]) if item is not None else 0
                maximum = self.catalog.item_max_stacks.get(resource_id)
                if maximum is None or maximum <= 0:
                    raise GospelEconomyError(
                        ERROR_GOSPEL_BREAK_CONFIG,
                        f"reward item {resource_id} has no storable a_item row",
                    )
                if current > maximum:
                    raise GospelEconomyError(
                        ERROR_EXCEED_ITEM_LIMIT_COUNT,
                        f"item {resource_id} already exceeds stack cap {maximum}",
                    )
                if current + amount > maximum:
                    if allow_saturated_rewards and profile.get("all_content_unlocked") and current == maximum:
                        applied = 0
                        saturated_rewards.append(dict(reward))
                    else:
                        raise GospelEconomyError(
                            ERROR_EXCEED_ITEM_LIMIT_COUNT,
                            f"item {resource_id} would exceed stack cap {maximum}",
                        )
                if applied:
                    inventory[key] = {
                        "uid": int(item["uid"])
                        if item is not None
                        else stable_item_uid(resource_id),
                        "id": resource_id,
                        "count": current + applied,
                        "para": int(item.get("para", 0))
                        if item is not None
                        else 0,
                    }
            else:
                field_name = RESOURCE_TOP_LEVEL_FIELDS.get(resource_type)
                if field_name is None or resource_id != 0:
                    raise GospelEconomyError(
                        ERROR_GOSPEL_BREAK_CONFIG,
                        f"unsupported Gospel reward {resource_type}:{resource_id}",
                    )
                current = scalar_value(field_name)
                if current + amount > UINT64_MAX:
                    raise GospelEconomyError(
                        ERROR_GOSPEL_BREAK_CONFIG,
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

        static_costs = _resource_dicts(aggregated_costs)
        static_rewards = _resource_dicts(aggregated_rewards)
        transaction_context = copy.deepcopy(dict(context or {}))
        history_entry: Dict[str, Any] = {
            "source": "gospel_progression",
            "command_id": request_id,
            "operation": operation,
            "gospel_uid": int(row["uid"]),
            "gospel_id": int(row["id"]),
            "costs": static_costs,
            "rewards": static_rewards,
            "applied_rewards": applied_rewards,
            "saturated_rewards": saturated_rewards,
        }
        if transaction_context:
            history_entry["context"] = transaction_context
        resource_history.append(history_entry)

        if aggregated_rewards:
            claim_suffix = transaction_context.get("target_stage", "reward")
            reward_history.append(
                {
                    "claim_key": (
                        f"gospel:{int(row['uid'])}:{operation}:{claim_suffix}"
                    ),
                    "source": "gospel_progression",
                    "source_id": int(row["id"]),
                    "command_id": request_id,
                    "gospel_uid": int(row["uid"]),
                    "rewards": static_rewards,
                    "applied_rewards": applied_rewards,
                    "saturated_rewards": saturated_rewards,
                }
            )

        changes: Dict[str, Any] = {
            "resource_history": resource_history,
        }
        if has_item_resource:
            changes["inventory"] = inventory
        for field_name in sorted(touched_scalars):
            changes[field_name] = scalar_values[field_name]
        if aggregated_rewards:
            changes["reward_history"] = reward_history
        return changes

    def _attribute(
        self, row: Mapping[str, Any], definition: GospelDefinition, profile=None
    ) -> Dict[str, int]:
        del definition
        return dict(self.combat_resolver.resolve_gospel(row, profile).wire_attributes)

    def _gospel_message(
        self, row: Mapping[str, Any], definition: GospelDefinition, profile=None
    ) -> Dict[str, Any]:
        return {
            "GospelUId": int(row["uid"]),
            "GospelId": int(row["id"]),
            "Level": int(row["level"]),
            "Exp": int(row["exp"]),
            "LevelLimit": int(row["level_limit"]),
            "Energy": int(row["energy"]),
            "ActiveSkillList": [
                {
                    "Id": int(skill["id"]),
                    "Level": int(skill["level"]),
                    "ValidLevel": int(skill["valid_level"]),
                }
                for skill in row["active_skills"]
            ],
            "Fondness": int(row["fondness"]),
            "FondnessLevel": int(row["fondness_level"]),
            "FondnessDailyIncrement": int(row["fondness_daily_increment"]),
            "BirthTime": int(row["birth_time"]),
            "IsNew": int(row["is_new"]),
            "AppearanceId": int(row["appearance_id"]),
            "FakeLevel": int(row["fake_level"]),
            "Skinuid": int(row["skin_uid"]),
            "FakeAwakeLevel": int(row["fake_awake_level"]),
            "LayDaysOnSecretary": int(row["lay_days_on_secretary"]),
            "IsFondnessLimitBreakthrough": int(
                row["is_fondness_limit_breakthrough"]
            ),
            "AwakeLevel": int(row["awake_level"]),
            "Attr": self._attribute(row, definition, profile),
            "RepeatedNum": int(row["repeated_num"]),
            "CombatPower": int(
                self.combat_resolver.resolve_gospel(row, profile).combat_power
            ),
            "SpiritStatus": int(row["spirit_status"]),
            "FondnessSwitch": int(row["fondness_switch"]),
            "SpiritSwitch": int(row["spirit_switch"]),
            "RewardedFondnessList": list(row["rewarded_fondness_levels"]),
            "LastWorkEndTime": int(row["last_work_end_time"]),
            "SpiritBreakthroughNum": int(row["spirit_breakthrough_num"]),
            "StarUp": int(row["star_up"]),
            "SpiritLevelLimit": int(row["spirit_level_limit"]),
            "SpiritIsReady": int(row["spirit_is_ready"]),
            "BreakLevelRewardTakenMap": {
                int(key): bool(value)
                for key, value in row["break_level_reward_taken"].items()
            },
            "AppearanceId2D": int(row["appearance_id_2d"]),
        }

    def _update_notification(
        self, row: Mapping[str, Any], definition: GospelDefinition, profile=None
    ) -> Tuple[int, bytes]:
        return (
            NOTIFY_UPDATE_GOSPEL,
            self.codec.encode(
                "Notify_UpdateGospel",
                {"GospelList": [self._gospel_message(row, definition, profile)]},
            ),
        )

    def _level_notification(
        self, profile: Mapping[str, Any], row: Mapping[str, Any]
    ) -> Tuple[int, bytes]:
        return (
            NOTIFY_GOSPEL_LEVEL_UP,
            self.codec.encode(
                "Notify_GospelLevelUp",
                {
                    "RoleId": self._role_id(profile),
                    "GospelUId": int(row["uid"]),
                    "Level": int(row["level"]),
                    "Exp": int(row["exp"]),
                },
            ),
        )

    def _ready_notification(self, row: Mapping[str, Any]) -> Tuple[int, bytes]:
        return (
            NOTIFY_GOSPEL_SPIRIT_IS_READY,
            self.codec.encode(
                "Notify_GospelSpiritIsReady",
                {
                    "GospelUId": int(row["uid"]),
                    "SpiritIsReady": int(row["spirit_is_ready"]),
                },
            ),
        )

    @staticmethod
    def _current_level_cap(
        row: Mapping[str, Any], definition: GospelDefinition
    ) -> int:
        if int(row["is_fondness_limit_breakthrough"]) and int(row["level_limit"]) == definition.break_limits[-1]:
            return definition.spirit_stages[
                int(row["spirit_breakthrough_num"])
            ].level
        return int(row["level_limit"])

    @staticmethod
    def _current_skill_cap(
        row: Mapping[str, Any], definition: GospelDefinition
    ) -> int:
        if int(row["is_fondness_limit_breakthrough"]) and int(row["level_limit"]) == definition.break_limits[-1]:
            return definition.max_skill_level
        try:
            index = definition.break_limits.index(int(row["level_limit"]))
        except ValueError as exc:  # defensive after normalization
            raise GospelProfileError("unknown Gospel skill break stage") from exc
        return definition.skill_limits[index]

    @staticmethod
    def _ready_value(
        row: Mapping[str, Any], definition: GospelDefinition
    ) -> int:
        if not int(row["is_fondness_limit_breakthrough"]):
            return 0
        stage_num = int(row["spirit_breakthrough_num"])
        if stage_num >= definition.max_spirit_breakthrough:
            return 0
        stage = definition.spirit_stages[stage_num]
        return int(
            int(row["level"]) >= stage.level
            and int(row["fondness_level"]) >= stage.spirit_level
        )

    def handle(
        self,
        request_id: int,
        profile: Optional[Mapping[str, Any]],
        request_body: bytes = b"",
    ) -> Optional[GospelProgressionResult]:
        if request_id not in RESPONSE_IDS:
            return None
        account = profile if isinstance(profile, Mapping) else {}
        role_id = self._role_id(account)
        uid = 0
        materials: Tuple[Mapping[str, int], ...] = ()
        requested_uint = 0
        requested_map: Dict[int, int] = {}
        try:
            fields = _wire_field_map(request_body)
            if request_id in {
                CS_GOSPEL_ENHANCE,
                CS_GOSPEL_BREAKTHROUGH,
                CS_GOSPEL_AWAKE,
                CS_GOSPEL_SKILL_ENHANCE,
            }:
                requested_role = _required_uint(
                    fields, 1, minimum=1, maximum=UINT64_MAX
                )
                uid = _required_uint(fields, 2, minimum=1, maximum=UINT64_MAX)
                if requested_role != role_id:
                    return self._result(
                        request_id,
                        ERROR_ROLE_ID_ILLEGAL,
                        account,
                        uid=uid,
                        reason=(
                            f"requested role {requested_role} does not match "
                            f"account role {role_id}"
                        ),
                    )
                if request_id == CS_GOSPEL_SKILL_ENHANCE:
                    requested_uint = _required_uint(
                        fields, 3, minimum=1, maximum=UINT32_MAX
                    )
                else:
                    materials = _materials(fields, 3)
            else:
                uid = _required_uint(fields, 1, minimum=1, maximum=UINT64_MAX)
                if request_id in {
                    CS_ADJUST_GOSPEL_FAKE_LEVEL,
                    CS_ADJUST_GOSPEL_FAKE_AWAKE_LEVEL,
                    CS_CHOOSE_GOSPEL_SPIRIT_STATUS,
                }:
                    requested_uint = _required_uint(
                        fields, 2, minimum=1, maximum=UINT32_MAX
                    )
                elif request_id == CS_ADJUST_GOSPEL_FAKE_SKILL_LEVEL:
                    requested_map = _skill_fake_map(fields, 2)
                elif request_id == CS_ADJUST_GOSPEL_FONDNESS_SWITCH:
                    # Zero is an explicit, legal switch value; requiring the
                    # field's presence prevents an omitted field from silently
                    # clearing persisted state.
                    requested_map = {
                        2: _required_uint(fields, 2, maximum=1),
                        3: _required_uint(fields, 3, maximum=1),
                    }
        except (ProtobufDecodeError, TypeError, ValueError) as exc:
            return self._result(
                request_id,
                ERROR_MESSAGE_DATA_ILLEGAL,
                account,
                uid=uid,
                reason=str(exc),
            )

        try:
            original, rows, index, row, definition = self._owned_gospel(
                account, uid
            )
        except LookupError as exc:
            return self._result(
                request_id,
                ERROR_GOSPEL_NOT_FOUND,
                account,
                uid=uid,
                materials=materials,
                skill_id=requested_uint
                if request_id == CS_GOSPEL_SKILL_ENHANCE
                else 0,
                reason=str(exc),
            )
        except GospelProfileError as exc:
            return self._result(
                request_id,
                ERROR_GOSPEL_BREAK_CONFIG,
                account,
                uid=uid,
                materials=materials,
                reason=str(exc),
            )

        def error(
            code: int,
            reason: str,
            *,
            skill_id: int = 0,
            new_level: int = 0,
        ) -> GospelProgressionResult:
            return self._result(
                request_id,
                code,
                account,
                uid=uid,
                row=row,
                materials=materials,
                skill_id=skill_id,
                new_level=new_level,
                reason=reason,
            )

        if request_id == CS_GOSPEL_ENHANCE:
            current_level = int(row["level"])
            cap = self._current_level_cap(row, definition)
            if current_level >= cap:
                if current_level >= definition.max_level:
                    return self._result(
                        request_id,
                        ERROR_SUCCESS,
                        account,
                        row=row,
                        materials=materials,
                    )
                return error(
                    ERROR_ALREADY_REACH_LIMIT_LEVEL,
                    f"Gospel level {current_level} already reached cap {cap}",
                )
            if not materials:
                return error(
                    ERROR_GOSPEL_INCREMENT_EXP_EMPTY,
                    "Gospel enhance material list is empty",
                )
            try:
                item_costs, _ = self._requested_item_costs(
                    account,
                    materials,
                    allowed_item_ids=self.catalog.gospel_exp_by_item,
                )
                raw_exp = sum(
                    self.catalog.gospel_exp_by_item[int(cost["id"])]
                    * int(cost["num"])
                    for cost in item_costs
                )
                current_exp = int(row["exp"])
                state_exp = int(definition.states[current_level]["exp"])
                cap_exp = int(definition.states[cap]["exp"])
                if current_exp < state_exp or current_exp >= cap_exp:
                    raise GospelEconomyError(
                        ERROR_GOSPEL_BREAK_CONFIG,
                        (
                            f"Gospel level {current_level} EXP {current_exp} "
                            f"is outside {state_exp}..{cap_exp - 1}"
                        ),
                    )
                target_exp = min(current_exp + raw_exp, cap_exp)
                wasted_exp = current_exp + raw_exp - target_exp
                accepted_exp = target_exp - current_exp
                stage_index = definition.break_limits.index(
                    int(row["level_limit"])
                )
                base_gold = sum(
                    self.catalog.gospel_exp_gold_by_item[int(cost["id"])][
                        stage_index
                    ]
                    * int(cost["num"])
                    for cost in item_costs
                )
                refund = (
                    wasted_exp
                    * self.catalog.gospel_exp_gold_rates[stage_index]
                    // 10_000
                )
                gold_cost = max(0, base_gold - refund)
                costs: list[Mapping[str, int]] = list(item_costs)
                if gold_cost:
                    costs.append(
                        MappingProxyType(
                            {
                                "type": RESOURCE_GOLD,
                                "id": 0,
                                "num": gold_cost,
                            }
                        )
                    )
                new_level = current_level
                for level in range(current_level + 1, cap + 1):
                    if int(definition.states[level]["exp"]) > target_exp:
                        break
                    new_level = level
                economy = self._economy_changes(
                    account,
                    request_id=request_id,
                    operation="enhance",
                    row=row,
                    costs=costs,
                    context={
                        "from_level": current_level,
                        "to_level": new_level,
                        "raw_exp": raw_exp,
                        "accepted_exp": accepted_exp,
                        "wasted_exp": wasted_exp,
                        "gold_cost": gold_cost,
                    },
                )
            except GospelEconomyError as exc:
                return error(exc.error_code, str(exc))

            # An unreduced simulated level follows ordinary growth. Preserve
            # a deliberately lower setting instead of silently resetting it.
            follow_real_level = int(row["fake_level"]) == current_level
            row["level"] = new_level
            if follow_real_level:
                row["fake_level"] = new_level
            row["exp"] = target_exp
            old_ready = int(row["spirit_is_ready"])
            row["spirit_is_ready"] = self._ready_value(row, definition)
            rows[index] = row
            changes = {"gospels": rows, **economy}
            notifications = [self._level_notification(account, row)]
            if follow_real_level and new_level != current_level:
                # Notify_GospelLevelUp has no FakeLevel/attribute fields.
                # Formation and combat require this complete updated row.
                notifications.append(self._update_notification(row, definition, account))
            if int(row["spirit_is_ready"]) != old_ready:
                notifications.append(self._ready_notification(row))
            return self._result(
                request_id,
                ERROR_SUCCESS,
                account,
                row=row,
                materials=materials,
                changes=changes,
                notifications=tuple(notifications),
            )

        if request_id == CS_GOSPEL_BREAKTHROUGH:
            limit = int(row["level_limit"])
            position = definition.break_limits.index(limit)
            if position == len(definition.break_limits) - 1:
                if (
                    int(row["is_fondness_limit_breakthrough"])
                    and int(row["spirit_breakthrough_num"])
                    == definition.max_spirit_breakthrough
                    and int(row["level"]) >= definition.max_level
                ):
                    return self._result(
                        request_id,
                        ERROR_SUCCESS,
                        account,
                        row=row,
                        materials=materials,
                    )
                return error(
                    ERROR_ALREADY_HIGHEST_LIMIT_LEVEL,
                    f"Gospel level limit is already {limit}",
                )
            if int(row["level"]) < limit:
                return error(
                    ERROR_GOSPEL_LEVEL_NOT_ENOUGH,
                    f"Gospel level {row['level']} has not reached {limit}",
                )
            costs = definition.ascension_costs[position]
            expected_items = self._item_signature(costs)
            try:
                if materials:
                    requested_costs, _ = self._requested_item_costs(
                        account,
                        materials,
                        allowed_item_ids=expected_items,
                    )
                    if self._item_signature(requested_costs) != expected_items:
                        raise GospelEconomyError(
                            ERROR_MATERIAL_ILLEGAL,
                            "Gospel breakthrough materials do not match ascension_item",
                        )
                else:
                    inventory = self._normalized_inventory(account)
                    materials = self._configured_materials(costs, inventory)
                economy = self._economy_changes(
                    account,
                    request_id=request_id,
                    operation="breakthrough",
                    row=row,
                    costs=costs,
                    context={
                        "from_limit": limit,
                        "to_limit": definition.break_limits[position + 1],
                        "ascension_index": position + 1,
                    },
                )
            except GospelEconomyError as exc:
                return error(exc.error_code, str(exc))
            row["level_limit"] = definition.break_limits[position + 1]
            self._enter_break_stage(row, definition, limit)
            rows[index] = row
            changes = {"gospels": rows, **economy}
            notifications = (
                (self._update_notification(row, definition, account),) if changes else ()
            )
            return self._result(
                request_id,
                ERROR_SUCCESS,
                account,
                row=row,
                materials=materials,
                changes=changes,
                notifications=notifications,
            )

        if request_id == CS_GOSPEL_AWAKE:
            awake = int(row["awake_level"])
            if awake >= definition.max_awake_level:
                return self._result(
                    request_id,
                    ERROR_SUCCESS,
                    account,
                    row=row,
                    materials=materials,
                )
            if not materials:
                return error(
                    ERROR_MATERIAL_ILLEGAL,
                    "Gospel awake material list is empty",
                )
            try:
                allowed_awaken_items = {
                    item_id
                    for option in definition.awaken_costs
                    for item_id in self._item_signature(option)
                }
                requested_costs, _ = self._requested_item_costs(
                    account,
                    materials,
                    allowed_item_ids=allowed_awaken_items,
                )
                requested_signature = self._item_signature(requested_costs)
                costs = next(
                    (
                        option
                        for option in definition.awaken_costs
                        if self._item_signature(option) == requested_signature
                    ),
                    None,
                )
                if costs is None:
                    raise GospelEconomyError(
                        ERROR_MATERIAL_ILLEGAL,
                        "Gospel awake materials do not match an awaken_item option",
                    )
                economy = self._economy_changes(
                    account,
                    request_id=request_id,
                    operation="awake",
                    row=row,
                    costs=costs,
                    context={
                        "from_awake": awake,
                        "to_awake": definition.awake_levels[
                            definition.awake_levels.index(awake) + 1
                        ],
                    },
                )
            except GospelEconomyError as exc:
                return error(exc.error_code, str(exc))
            row["awake_level"] = definition.awake_levels[
                definition.awake_levels.index(awake) + 1
            ]
            if int(row["fake_awake_level"]) == awake:
                row["fake_awake_level"] = row["awake_level"]
            rows[index] = row
            changes = {"gospels": rows, **economy}
            candidate = {"gospels": rows, "decoration_items": copy.deepcopy(account.get("decoration_items"))}
            updated_decorations = self.collectibles.sync_gospel_decoration_levels(candidate, gospel_id=int(row["id"]))
            if updated_decorations:
                changes["decoration_items"] = candidate["decoration_items"]
            # SC_GospelAwakeRet echoes consumed ItemList, but its managed
            # success callback only refreshes the view: it does not apply
            # remaining bag counts. Send authoritative absolute counts before
            # that callback, as the existing rune/settlement paths do.
            material_notifications = tuple(
                make_update_item_packet(self._role_id(account), economy["inventory"][str(item_id)], self.codec)
                for item_id in sorted({int(cost["id"]) for cost in costs if int(cost["type"]) == RESOURCE_ITEM})
            )
            notifications = (self._update_notification(row, definition, account),) + material_notifications + tuple(
                (NOTIFY_UPDATE_DECORATION_ITEM, self.collectibles.make_update_decoration(item))
                for item in updated_decorations
            )
            return self._result(
                request_id,
                ERROR_SUCCESS,
                account,
                row=row,
                materials=materials,
                changes=changes,
                notifications=notifications,
            )

        if request_id == CS_GOSPEL_SKILL_ENHANCE:
            skill_id = requested_uint
            skill = next(
                (
                    item
                    for item in row["active_skills"]
                    if int(item["id"]) == skill_id
                ),
                None,
            )
            if skill is None:
                return error(
                    ERROR_SKILL_NOT_FOUND,
                    f"Gospel UID {uid} has no active skill {skill_id}",
                    skill_id=skill_id,
                )
            current = int(skill["level"])
            cap = self._current_skill_cap(row, definition)
            if current >= cap:
                if current >= definition.max_skill_level:
                    return self._result(
                        request_id,
                        ERROR_SUCCESS,
                        account,
                        row=row,
                        skill_id=skill_id,
                        new_level=current,
                    )
                return error(
                    ERROR_SKILL_ALREADY_HIGHEST_LEVEL,
                    f"skill {skill_id} reached current cap {cap}",
                    skill_id=skill_id,
                    new_level=current,
                )
            try:
                skill_costs = definition.skill_costs.get(skill_id)
                if skill_costs is None or current not in skill_costs:
                    raise GospelEconomyError(
                        ERROR_GOSPEL_BREAK_CONFIG,
                        f"a_skill[{skill_id}] has no level{current} cost",
                    )
                economy = self._economy_changes(
                    account,
                    request_id=request_id,
                    operation="skill_enhance",
                    row=row,
                    costs=skill_costs[current],
                    context={
                        "skill_id": skill_id,
                        "from_level": current,
                        "to_level": current + 1,
                    },
                )
            except GospelEconomyError as exc:
                return error(
                    exc.error_code,
                    str(exc),
                    skill_id=skill_id,
                    new_level=current,
                )
            skill["level"] = current + 1
            if int(skill["valid_level"]) == current:
                skill["valid_level"] = current + 1
            rows[index] = row
            changes = {"gospels": rows, **economy}
            return self._result(
                request_id,
                ERROR_SUCCESS,
                account,
                row=row,
                skill_id=skill_id,
                new_level=int(skill["level"]),
                changes=changes,
                notifications=(self._update_notification(row, definition, account),),
            )

        economy_projection: Dict[str, Any] = {}

        if request_id == CS_ADJUST_GOSPEL_FAKE_LEVEL:
            if requested_uint > int(row["level"]):
                return error(
                    ERROR_FAKE_LEVEL_CANNOT_EXCEED_REAL,
                    f"fake level {requested_uint} exceeds real level {row['level']}",
                )
            row["fake_level"] = requested_uint

        elif request_id == CS_ADJUST_GOSPEL_FAKE_AWAKE_LEVEL:
            if requested_uint > int(row["awake_level"]):
                return error(
                    ERROR_FAKE_LEVEL_CANNOT_EXCEED_REAL,
                    f"fake awake {requested_uint} exceeds real awake {row['awake_level']}",
                )
            row["fake_awake_level"] = requested_uint

        elif request_id == CS_BREAKTHROUGH_GOSPEL_FONDNESS_LIMIT:
            if int(row["is_fondness_limit_breakthrough"]):
                return self._result(
                    request_id, ERROR_SUCCESS, account, row=row
                )
            if (
                int(row["level_limit"]) != definition.break_limits[-1]
                or int(row["level"]) < definition.break_limits[-1]
            ):
                return error(
                    ERROR_LEVEL_NOT_REACHED_LEVEL_LIMIT,
                    "Gospel has not reached the ordinary level limit",
                )
            if int(row["fondness_level"]) < definition.max_spirit_level:
                return error(
                    ERROR_FONDNESS_LEVEL_NOT_REACH_HIGHEST,
                    "Gospel fondness has not reached its maximum",
                )
            first_stage = definition.spirit_stages[0]
            try:
                economy_projection = self._economy_changes(
                    account,
                    request_id=request_id,
                    operation="fondness_limit_breakthrough",
                    row=row,
                    costs=first_stage.ascension_items,
                    context={
                        "target_stage": first_stage.break_time,
                        "level_cap": first_stage.level,
                        "spirit_level_cap": first_stage.spirit_level,
                    },
                )
            except GospelEconomyError as exc:
                return error(exc.error_code, str(exc))
            row["is_fondness_limit_breakthrough"] = 1
            # Ordinary favor is cumulative; heart-contract favor is the
            # remainder inside the source runtime's 2,400-point level.
            row["fondness"] = 0
            row["spirit_breakthrough_num"] = 0
            row["spirit_level_limit"] = first_stage.spirit_level
            row["star_up"] = first_stage.star_up
            if not int(row["spirit_status"]):
                row["spirit_status"] = max(1, first_stage.initial_stance)
            row["spirit_is_ready"] = 0

        elif request_id == CS_CHOOSE_GOSPEL_SPIRIT_STATUS:
            if not int(row["is_fondness_limit_breakthrough"]):
                return error(
                    ERROR_CHOOSE_GOSPEL_SPIRIT_NOT_BREAKTHROUGH_YET,
                    "Gospel has not entered the spirit stage",
                )
            spirit_num = int(row["spirit_breakthrough_num"])
            max_status = 2 * spirit_num + 1
            if int(row["fondness_level"]) >= int(row["spirit_level_limit"]):
                max_status += 1
            if requested_uint > max_status:
                return error(
                    ERROR_CHOOSE_GOSPEL_SPIRIT_EXCEED_MAX,
                    f"spirit status {requested_uint} exceeds unlocked {max_status}",
                )
            row["spirit_status"] = requested_uint

        elif request_id == CS_ADJUST_GOSPEL_FAKE_SKILL_LEVEL:
            skills = row["active_skills"]
            for position, fake_level in requested_map.items():
                if position > len(skills):
                    return error(
                        ERROR_SKILL_NOT_FOUND,
                        f"Gospel UID {uid} has no skill position {position}",
                    )
                actual = int(skills[position - 1]["level"])
                if fake_level > actual:
                    return error(
                        ERROR_FAKE_LEVEL_CANNOT_EXCEED_REAL,
                        f"fake skill level {fake_level} exceeds real level {actual}",
                    )
            for position, fake_level in requested_map.items():
                skills[position - 1]["valid_level"] = fake_level

        elif request_id == CS_ADJUST_GOSPEL_FONDNESS_SWITCH:
            row["fondness_switch"] = requested_map[2]
            row["spirit_switch"] = requested_map[3]

        else:  # CS_BREAKTHROUGH_GOSPEL_SPIRIT
            if not int(row["is_fondness_limit_breakthrough"]):
                return error(
                    ERROR_CANNOT_BREAKTHROUGH_IN_SPIRIT_STAGE,
                    "Gospel has not entered the spirit stage",
                )
            current_num = int(row["spirit_breakthrough_num"])
            if current_num >= definition.max_spirit_breakthrough:
                if (
                    int(row["level"]) >= definition.max_level
                    and int(row["fondness_level"])
                    >= definition.max_spirit_level
                ):
                    return self._result(
                        request_id, ERROR_SUCCESS, account, row=row
                    )
                return error(
                    ERROR_ALREADY_HIGHEST_SPIRIT_BREAKTHROUGH_NUM,
                    "Gospel spirit breakthrough is already at its maximum",
                )
            current_stage = definition.spirit_stages[current_num]
            if int(row["level"]) < current_stage.level:
                return error(
                    ERROR_LEVEL_NOT_REACHED_LEVEL_LIMIT,
                    f"Gospel level has not reached {current_stage.level}",
                )
            if int(row["fondness_level"]) < current_stage.spirit_level:
                return error(
                    ERROR_FONDNESS_LEVEL_NOT_REACH_HIGHEST,
                    f"Gospel spirit level has not reached {current_stage.spirit_level}",
                )
            target_stage = definition.spirit_stages[current_num + 1]
            try:
                economy = self._economy_changes(
                    account,
                    request_id=request_id,
                    operation="spirit_breakthrough",
                    row=row,
                    costs=target_stage.ascension_items,
                    rewards=target_stage.rewards,
                    context={
                        "from_stage": current_num,
                        "target_stage": target_stage.break_time,
                        "level_cap": target_stage.level,
                        "spirit_level_cap": target_stage.spirit_level,
                    },
                )
            except GospelEconomyError as exc:
                return error(exc.error_code, str(exc))
            row["spirit_breakthrough_num"] = target_stage.break_time
            row["spirit_level_limit"] = target_stage.spirit_level
            row["star_up"] = sum(
                stage.star_up
                for stage in definition.spirit_stages[: current_num + 2]
            )
            row["spirit_is_ready"] = 0
            rows[index] = row
            changes = {"gospels": rows, **economy}
            notifications = (
                self._update_notification(row, definition, account),
                self._ready_notification(row),
            ) if changes else ()
            return self._result(
                request_id,
                ERROR_SUCCESS,
                account,
                row=row,
                rewards=target_stage.rewards,
                changes=changes,
                notifications=notifications,
            )

        rows[index] = row
        changes = self._changes(original, rows)
        if changes and economy_projection:
            changes.update(economy_projection)
        notifications: Tuple[Tuple[int, bytes], ...] = ()
        if request_id == CS_BREAKTHROUGH_GOSPEL_FONDNESS_LIMIT and changes:
            notifications = (
                self._update_notification(row, definition, account),
                self._ready_notification(row),
            )
        return self._result(
            request_id,
            ERROR_SUCCESS,
            account,
            row=row,
            changes=changes,
            notifications=notifications,
        )


Layer = GospelProgressionLayer
Result = GospelProgressionResult


__all__ = [
    "CS_ADJUST_GOSPEL_FAKE_AWAKE_LEVEL",
    "CS_ADJUST_GOSPEL_FAKE_LEVEL",
    "CS_ADJUST_GOSPEL_FAKE_SKILL_LEVEL",
    "CS_ADJUST_GOSPEL_FONDNESS_SWITCH",
    "CS_BREAKTHROUGH_GOSPEL_FONDNESS_LIMIT",
    "CS_BREAKTHROUGH_GOSPEL_SPIRIT",
    "CS_CHOOSE_GOSPEL_SPIRIT_STATUS",
    "CS_GOSPEL_AWAKE",
    "CS_GOSPEL_BREAKTHROUGH",
    "CS_GOSPEL_ENHANCE",
    "CS_GOSPEL_SKILL_ENHANCE",
    "ERROR_ALREADY_HIGHEST_LIMIT_LEVEL",
    "ERROR_ALREADY_HIGHEST_SPIRIT_BREAKTHROUGH_NUM",
    "ERROR_ALREADY_REACH_LIMIT_LEVEL",
    "ERROR_CANNOT_BREAKTHROUGH_IN_SPIRIT_STAGE",
    "ERROR_CHOOSE_GOSPEL_SPIRIT_EXCEED_MAX",
    "ERROR_CHOOSE_GOSPEL_SPIRIT_NOT_BREAKTHROUGH_YET",
    "ERROR_FAKE_LEVEL_CANNOT_EXCEED_REAL",
    "ERROR_FONDNESS_LEVEL_NOT_REACH_HIGHEST",
    "ERROR_EXCEED_ITEM_LIMIT_COUNT",
    "ERROR_GOLD_NOT_ENOUGH",
    "ERROR_GOSPEL_BREAK_CONFIG",
    "ERROR_GOSPEL_INCREMENT_EXP_EMPTY",
    "ERROR_GOSPEL_LEVEL_NOT_ENOUGH",
    "ERROR_GOSPEL_NOT_FOUND",
    "ERROR_ITEM_NUM_NOT_ENOUGH",
    "ERROR_LEVEL_NOT_REACHED_LEVEL_LIMIT",
    "ERROR_MATERIAL_ILLEGAL",
    "ERROR_MESSAGE_DATA_ILLEGAL",
    "ERROR_ROLE_ID_ILLEGAL",
    "ERROR_SKILL_ALREADY_HIGHEST_LEVEL",
    "ERROR_SKILL_NOT_FOUND",
    "ERROR_SUCCESS",
    "GospelDefinition",
    "GospelEconomyError",
    "GospelProgressionCatalog",
    "GospelProgressionCatalogError",
    "GospelProgressionLayer",
    "GospelProgressionResult",
    "GospelProtocolContractError",
    "Layer",
    "NOTIFY_GOSPEL_LEVEL_UP",
    "NOTIFY_GOSPEL_SPIRIT_IS_READY",
    "NOTIFY_UPDATE_GOSPEL",
    "RESPONSE_IDS",
    "RESOURCE_GOLD",
    "RESOURCE_ITEM",
    "Result",
    "SpiritStage",
]
