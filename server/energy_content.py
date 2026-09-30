"""Table-backed, durable player-energy recovery for the 1.0 runtime.

The defined runtime uses ``a_playerstate.energy_limit`` for the account-level
cap and ``a_global[9990999]`` for the recovery cadence.  The shipped 1.0 data
sets that cadence to one point every 360 seconds.  This module keeps the
server-owned recovery anchor in the local save so elapsed time continues to
count while the app or embedded server is stopped.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Mapping, MutableMapping

try:  # Package imports in tests; fallback supports direct server execution.
    from .content_profile import ContentCatalog
except ImportError:  # pragma: no cover - ``python server/server.py``
    from content_profile import ContentCatalog  # type: ignore


ENERGY_RECOVERY_REVISION = 1
ENERGY_RECOVERY_INTERVAL_SECONDS = 360
LAST_RECOVER_ENERGY_TIME_KEY = "last_recover_energy_time"
ENERGY_RECOVERY_REVISION_KEY = "energy_recovery_revision"


def _uint(value: object, default: int = 0) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return max(0, int(default))
    return max(0, parsed)


@dataclass(frozen=True)
class EnergyRecoveryResult:
    """One deterministic recovery evaluation."""

    changes: Dict[str, int] = field(default_factory=dict)
    previous_energy: int = 0
    energy: int = 0
    capacity: int = 0
    recovered: int = 0
    last_recover_energy_time: int = 0


class EnergyRecoveryLayer:
    """Apply the exact 1.0 six-minute recovery rule to a durable profile."""

    def __init__(
        self,
        catalog: ContentCatalog,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.clock = clock
        rows = sorted(
            catalog.table("a_playerstate").records,
            key=lambda row: _uint(row.get("level")),
        )
        if not rows:
            raise ValueError("a_playerstate contains no energy limits")
        limits: Dict[int, int] = {}
        for row in rows:
            level = _uint(row.get("level"))
            limit = _uint(row.get("energy_limit"))
            if level <= 0 or limit <= 0:
                raise ValueError("a_playerstate has an invalid level/energy_limit")
            if level in limits:
                raise ValueError(f"a_playerstate duplicates level {level}")
            limits[level] = limit
        expected = set(range(1, max(limits) + 1))
        if set(limits) != expected:
            raise ValueError("a_playerstate energy limits are not a closed level range")
        self._limits = limits
        self._maximum_level = max(limits)

        global_row = catalog.record("a_global", 9_990_999)
        configured_interval = _uint(global_row.get("string_value"))
        if configured_interval not in (0, ENERGY_RECOVERY_INTERVAL_SECONDS):
            raise ValueError(
                "a_global[9990999] recovery interval differs from recovered 1.0 "
                f"semantics: {configured_interval}"
            )
        self.interval_seconds = (
            configured_interval or ENERGY_RECOVERY_INTERVAL_SECONDS
        )

    def now(self) -> int:
        return max(0, int(self.clock()))

    def capacity(self, profile: Mapping[str, object]) -> int:
        level = _uint(profile.get("level"), 1)
        level = min(max(level, 1), self._maximum_level)
        return self._limits[level]

    def seed_profile(
        self,
        profile: MutableMapping[str, Any],
        *,
        now: int | None = None,
    ) -> bool:
        """Migrate an old save without granting retroactive, unproved energy."""

        timestamp = self.now() if now is None else max(0, int(now))
        capacity = self.capacity(profile)
        energy = _uint(profile.get("energy"))
        anchor = _uint(profile.get(LAST_RECOVER_ENERGY_TIME_KEY))
        revision = _uint(profile.get(ENERGY_RECOVERY_REVISION_KEY))

        if energy >= capacity:
            desired_anchor = 0
        elif anchor <= 0 or anchor > timestamp:
            # Start counting from migration/repair time.  Awarding elapsed
            # energy before this feature existed would invent account history.
            desired_anchor = timestamp
        else:
            desired_anchor = anchor

        changed = False
        if anchor != desired_anchor:
            profile[LAST_RECOVER_ENERGY_TIME_KEY] = desired_anchor
            changed = True
        if revision < ENERGY_RECOVERY_REVISION:
            profile[ENERGY_RECOVERY_REVISION_KEY] = ENERGY_RECOVERY_REVISION
            changed = True
        return changed

    def synchronize(
        self,
        profile: Mapping[str, object],
        *,
        now: int | None = None,
    ) -> EnergyRecoveryResult:
        """Return the authoritative recovery delta at ``now``.

        The anchor represents the start of the current six-minute interval.
        Whole intervals are consumed; the remainder stays attached to the next
        point.  Reaching or exceeding the level cap clears the anchor, matching
        the runtime's no-countdown-at-cap behaviour.
        """

        timestamp = self.now() if now is None else max(0, int(now))
        capacity = self.capacity(profile)
        previous_energy = _uint(profile.get("energy"))
        energy = previous_energy
        previous_anchor = _uint(profile.get(LAST_RECOVER_ENERGY_TIME_KEY))
        anchor = previous_anchor

        if energy >= capacity:
            anchor = 0
        elif anchor <= 0 or anchor > timestamp:
            # Also repairs clock rollback/future timestamps without granting a
            # point from a negative interval.
            anchor = timestamp
        else:
            elapsed_intervals = (timestamp - anchor) // self.interval_seconds
            if elapsed_intervals > 0:
                energy = min(capacity, energy + elapsed_intervals)
                if energy >= capacity:
                    anchor = 0
                else:
                    anchor += elapsed_intervals * self.interval_seconds

        changes: Dict[str, int] = {}
        if energy != previous_energy:
            changes["energy"] = energy
        if anchor != previous_anchor:
            changes[LAST_RECOVER_ENERGY_TIME_KEY] = anchor
        if _uint(profile.get(ENERGY_RECOVERY_REVISION_KEY)) < ENERGY_RECOVERY_REVISION:
            changes[ENERGY_RECOVERY_REVISION_KEY] = ENERGY_RECOVERY_REVISION
        return EnergyRecoveryResult(
            changes=changes,
            previous_energy=previous_energy,
            energy=energy,
            capacity=capacity,
            recovered=max(0, energy - previous_energy),
            last_recover_energy_time=anchor,
        )

    def reconcile_energy_change(
        self,
        profile: Mapping[str, object],
        *,
        previous_energy: int,
        now: int | None = None,
    ) -> Dict[str, int]:
        """Update the timer after a non-timer operation changes energy.

        Spending from a full/overflowing balance starts a fresh interval.
        Rewards which reach the cap stop recovery.  Spending while already
        below cap preserves the partial interval.
        """

        timestamp = self.now() if now is None else max(0, int(now))
        capacity = self.capacity(profile)
        old_energy = _uint(previous_energy)
        energy = _uint(profile.get("energy"))
        previous_anchor = _uint(profile.get(LAST_RECOVER_ENERGY_TIME_KEY))

        if energy >= capacity:
            anchor = 0
        elif old_energy >= capacity or previous_anchor <= 0 or previous_anchor > timestamp:
            anchor = timestamp
        else:
            anchor = previous_anchor

        changes: Dict[str, int] = {}
        if anchor != previous_anchor:
            changes[LAST_RECOVER_ENERGY_TIME_KEY] = anchor
        if _uint(profile.get(ENERGY_RECOVERY_REVISION_KEY)) < ENERGY_RECOVERY_REVISION:
            changes[ENERGY_RECOVERY_REVISION_KEY] = ENERGY_RECOVERY_REVISION
        return changes


__all__ = [
    "ENERGY_RECOVERY_INTERVAL_SECONDS",
    "ENERGY_RECOVERY_REVISION",
    "ENERGY_RECOVERY_REVISION_KEY",
    "EnergyRecoveryLayer",
    "EnergyRecoveryResult",
    "LAST_RECOVER_ENERGY_TIME_KEY",
]
