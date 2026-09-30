"""Offline rune-recast candidate policy.

The defined tables support a deterministic simulator, but not a claim about
the source server's RNG implementation.  This module deliberately limits
itself to producing a candidate mapping; transactions, costs, notifications,
and persistence belong to the collectibles layer.
"""

from __future__ import annotations

import copy
import json
import random
from collections.abc import Mapping, Sequence
from typing import Any, TypeVar


POLICY_VERSION = 1


class RuneRecastPolicyError(ValueError):
    """Raised when a rune or source table cannot produce a valid candidate."""


_T = TypeVar("_T")


def _int_field(value: Any, field: str, *, minimum: int | None = None) -> int:
    """Read an actual integer, rejecting bools and coercion-by-accident."""

    if isinstance(value, bool) or not isinstance(value, int):
        raise RuneRecastPolicyError(f"{field} must be an integer")
    if minimum is not None and value < minimum:
        raise RuneRecastPolicyError(f"{field} must be >= {minimum}")
    return value


def _weighted_pick(
    rows: Sequence[_T], weights: Sequence[int], rng: random.Random, *, what: str
) -> _T:
    """Pick one item with positive integer weights using only ``rng.random``."""

    if len(rows) != len(weights) or not rows:
        raise RuneRecastPolicyError(f"no eligible {what}")
    total = sum(weight for weight in weights if weight > 0)
    if total <= 0:
        raise RuneRecastPolicyError(f"no positive-weight {what}")

    threshold = rng.random() * total
    cumulative = 0
    for row, weight in zip(rows, weights):
        if weight <= 0:
            continue
        cumulative += weight
        if threshold < cumulative:
            return row
    # random.Random.random() is strictly below 1.0.  Keep a defensive fallback
    # for a compatible RNG implementation that returns an endpoint.
    for row, weight in reversed(list(zip(rows, weights))):
        if weight > 0:
            return row
    raise RuneRecastPolicyError(f"no positive-weight {what}")


def _quality_range(row: Mapping[str, Any], quality: int) -> tuple[int, int]:
    field = f"attr_value_quality_{quality}"
    raw = row.get(field)
    if not isinstance(raw, str):
        raise RuneRecastPolicyError(f"source row {row.get('id', '?')} {field} must be JSON text")
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise RuneRecastPolicyError(
            f"source row {row.get('id', '?')} {field} is not valid JSON"
        ) from exc
    if (
        not isinstance(parsed, list)
        or len(parsed) != 2
        or any(isinstance(value, bool) or not isinstance(value, int) for value in parsed)
    ):
        raise RuneRecastPolicyError(
            f"source row {row.get('id', '?')} {field} must be a two-integer JSON array"
        )
    return min(parsed), max(parsed)


def _validate_source_rows(
    rows: Sequence[Mapping[str, Any]], quality_limit: int
) -> None:
    row_ids: set[int] = set()
    for index, row in enumerate(rows):
        for field in ("id", "weight", "attr_group", "attr_id"):
            if field not in row:
                raise RuneRecastPolicyError(
                    f"secondary row {index} is missing {field}"
                )
        row_id = _int_field(row["id"], f"secondary row {index} id", minimum=1)
        if row_id in row_ids:
            raise RuneRecastPolicyError(f"duplicate secondary row id {row_id}")
        row_ids.add(row_id)
        _int_field(row["weight"], f"secondary row {index} weight")
        _int_field(row["attr_group"], f"secondary row {index} attr_group", minimum=1)
        _int_field(row["attr_id"], f"secondary row {index} attr_id", minimum=1)
        for quality in range(1, quality_limit + 1):
            weight_field = f"attr_value_quality_weight_{quality}"
            if weight_field not in row:
                raise RuneRecastPolicyError(
                    f"secondary row {index} is missing {weight_field}"
                )
            _int_field(row[weight_field], f"secondary row {index} {weight_field}")
            _quality_range(row, quality)


def _source_scope(
    static: Mapping[str, Any], secondary_rows: Sequence[Mapping[str, Any]]
) -> list[Mapping[str, Any]]:
    """Use a static row's pool when the caller supplied the complete table.

    Callers may also pass an already-filtered pool.  The optional pool field is
    therefore only used when every supplied row carries it; this keeps the
    policy useful with small test fixtures without hiding a pool mismatch.
    """

    raw_rows = list(secondary_rows)
    pool = static.get("secondary_attr_poor")
    if pool is None or not raw_rows or not all(
        "secondary_attr_poor" in row for row in raw_rows
    ):
        return raw_rows
    pool_id = _int_field(pool, "static secondary_attr_poor", minimum=1)
    scoped: list[Mapping[str, Any]] = []
    for index, row in enumerate(raw_rows):
        row_pool = _int_field(
            row["secondary_attr_poor"],
            f"secondary row {index} secondary_attr_poor",
            minimum=1,
        )
        if row_pool == pool_id:
            scoped.append(row)
    if not scoped:
        raise RuneRecastPolicyError(
            f"no secondary rows for pool {pool_id}"
        )
    return scoped


def _slot_field(slot: int, suffix: str) -> str:
    return f"attr_{slot}_{suffix}" if suffix != "rand_attr" else f"rand_attr_{slot}"


def generate_candidate(
    static: Mapping[str, Any],
    current: Mapping[str, Any],
    secondary_rows: Sequence[Mapping[str, Any]],
    locked_indexes: Sequence[int],
    rng: random.Random,
) -> dict[str, Any]:
    """Generate a three-line offline recast candidate.

    Selection is sequential.  Each selected row has positive ``weight`` and
    is excluded after selection by both ``attr_group`` and ``attr_id``.
    ``rand_attr_N`` stores the selected source row's ``id``; ``attr_id`` is
    only the attribute identity used for deduplication.  The selected quality
    uses the source row's quality weights through the static quality limit, and
    the value is a uniform inclusive integer in the normalized source
    interval.  ``group_weight`` and misspelled strengthen fields are
    intentionally not read.
    """

    if not isinstance(static, Mapping):
        raise RuneRecastPolicyError("static must be a mapping")
    if not isinstance(current, Mapping):
        raise RuneRecastPolicyError("current must be a mapping")
    if isinstance(secondary_rows, (str, bytes)) or not isinstance(
        secondary_rows, Sequence
    ):
        raise RuneRecastPolicyError("secondary_rows must be a sequence")
    if isinstance(locked_indexes, (str, bytes)) or not isinstance(
        locked_indexes, Sequence
    ):
        raise RuneRecastPolicyError("locked_indexes must be a sequence")
    if not isinstance(rng, random.Random):
        raise RuneRecastPolicyError("rng must be random.Random")

    if "secondary_attr_quality_limit" not in static:
        raise RuneRecastPolicyError(
            "static is missing secondary_attr_quality_limit"
        )
    quality_limit = _int_field(
        static["secondary_attr_quality_limit"],
        "static secondary_attr_quality_limit",
        minimum=1,
    )
    if quality_limit > 4:
        raise RuneRecastPolicyError(
            "static secondary_attr_quality_limit must be <= 4"
        )

    locks: tuple[int, ...] = tuple(
        _int_field(index, "locked index", minimum=1) for index in locked_indexes
    )
    if len(locks) > 2:
        raise RuneRecastPolicyError("at most two rune attributes may be locked")
    if len(set(locks)) != len(locks):
        raise RuneRecastPolicyError("locked indexes must be unique")
    if any(index > 3 for index in locks):
        raise RuneRecastPolicyError("locked indexes must be in 1..3")
    locked = set(locks)

    raw_rows: list[Mapping[str, Any]] = []
    for index, row in enumerate(secondary_rows):
        if not isinstance(row, Mapping):
            raise RuneRecastPolicyError(f"secondary row {index} must be a mapping")
        raw_rows.append(row)
    rows = _source_scope(static, raw_rows)
    _validate_source_rows(rows, quality_limit)

    by_row_id: dict[int, Mapping[str, Any]] = {}
    for row in rows:
        by_row_id[int(row["id"])] = row

    used_groups: set[int] = set()
    used_attr_ids: set[int] = set()
    for slot in sorted(locked):
        attr_field = _slot_field(slot, "rand_attr")
        quality_field = _slot_field(slot, "quality")
        value_field = _slot_field(slot, "value")
        for field in (attr_field, quality_field, value_field):
            if field not in current:
                raise RuneRecastPolicyError(
                    f"current is missing locked field {field}"
                )
        row_id = _int_field(current[attr_field], f"current {attr_field}", minimum=1)
        quality = _int_field(
            current[quality_field], f"current {quality_field}", minimum=1
        )
        if quality > quality_limit:
            raise RuneRecastPolicyError(
                f"current {quality_field} exceeds quality limit"
            )
        value = _int_field(current[value_field], f"current {value_field}")
        row = by_row_id.get(row_id)
        if row is None:
            raise RuneRecastPolicyError(
                f"locked {attr_field} does not identify a source row id"
            )
        attr_id = _int_field(
            row["attr_id"], f"source row {row_id} attr_id", minimum=1
        )
        low, high = _quality_range(row, quality)
        if not low <= value <= high:
            raise RuneRecastPolicyError(
                f"locked {value_field} is outside the source interval"
            )
        group = int(row["attr_group"])
        if group in used_groups or attr_id in used_attr_ids:
            raise RuneRecastPolicyError(
                "locked attributes repeat an attr_group or attr_id"
            )
        used_groups.add(group)
        used_attr_ids.add(attr_id)

    result = copy.deepcopy(dict(current))
    for slot in range(1, 4):
        if slot in locked:
            continue
        eligible = [
            row
            for row in rows
            if int(row["weight"]) > 0
            and int(row["attr_group"]) not in used_groups
            and int(row["attr_id"]) not in used_attr_ids
        ]
        if not eligible:
            raise RuneRecastPolicyError(
                f"no eligible secondary attribute for slot {slot}"
            )
        row = _weighted_pick(
            eligible,
            [int(item["weight"]) for item in eligible],
            rng,
            what=f"secondary attribute for slot {slot}",
        )
        quality_rows = list(range(1, quality_limit + 1))
        quality = _weighted_pick(
            quality_rows,
            [int(row[f"attr_value_quality_weight_{item}"]) for item in quality_rows],
            rng,
            what=f"quality for slot {slot}",
        )
        low, high = _quality_range(row, quality)
        value = rng.randint(low, high)
        result[_slot_field(slot, "rand_attr")] = int(row["id"])
        result[_slot_field(slot, "quality")] = quality
        result[_slot_field(slot, "value")] = value
        used_groups.add(int(row["attr_group"]))
        used_attr_ids.add(int(row["attr_id"]))

    return result
