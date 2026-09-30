"""Persistent local account state stored as a small JSON document."""

from __future__ import annotations

import copy
import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, Mapping, MutableMapping, Optional, Tuple

try:  # Package imports in tests; fallback supports ``python server/server.py``.
    from .formation_names import default_formation_name
except ImportError:  # pragma: no cover - direct server execution compatibility
    from formation_names import default_formation_name  # type: ignore


# Version 10 protects source-backed resets and explicit empty-save presets;
# version 9 already protects paid, unresolved rune candidates.
STATE_SCHEMA_VERSION = 11
VALID_LOCAL_HERO_IDS = frozenset({3000000, *range(3000001, 3000007)})

# These three requests were previously handled by the schema-only runtime.  It
# persisted the request bytes and returned ErrorCode=0, but did not project the
# successful operation into the account fields used by reconnect snapshots.
# Version 4 recovers only those statically proved local successes.  Restricting
# the migration to the known IDs prevents an arbitrary raw request from
# becoming account state.
ANNIVERSARY_ACTIVITY_ID = 821020002
ANNIVERSARY_SUB_ACTIVITY_IDS = frozenset({23050042, 23050043, 23050044})
SPRING_V2_GOSPEL_IDS = frozenset(
    {113014009, 123014021, 213016024, 223016188, 233016010, 243016011}
)


class FutureStateSchemaError(ValueError):
    """Raised when newer save data is opened by an older runtime."""


def new_profile(role_id: int, role_name: str = "Local Guest") -> Dict[str, Any]:
    """Return a complete, internally consistent local guest profile."""

    hero_uid = 1000000001
    hero_id = 3000001
    return {
        "role_id": int(role_id),
        "role_name": role_name or "Local Guest",
        "profile_mode": "minimal",
        "profile_revision": 1,
        "save_revision": 0,
        "content_catalog_version": "",
        "all_content_unlocked": False,
        "level": 1,
        "vip_level": 0,
        "exp": 0,
        "diamond": 0,
        "gold": 1000,
        "energy": 100,
        "minidiamond": 0,
        "birthday": "",
        "introduce": "",
        "head_image_id": 7_000_001,
        "head_image_ids": [7_000_001],
        "head_frame_id": 0,
        "head_frame_ids": [],
        "goblin_headframes": [],
        "profile_customization_revision": 0,
        "sophisticate": 0,
        "spring_status": 0,
        "back_image_id": 0,
        "heroes": [
            {
                "uid": hero_uid,
                "id": hero_id,
                "level": 1,
                "exp": 0,
                "state": 0,
                "appearance_id": hero_id,
                "combat_power": 100,
            }
        ],
        "gospels": [],
        "draw_num": 0,
        "single_drawn_num": 0,
        "rng_seed": int(role_id),
        "rng_counter": 0,
        "phalanxes": [
            {
                "id": 0,
                "name": default_formation_name(0),
                "hero_uids": [hero_uid],
                "combat_power": 100,
                "type": 0,
            }
        ],
        "auto_phalanxes": {},
        "current_phalanx_id": 0,
        "formation_state": {},
        "guide_step": 0,
        "guide_steps": {},
        "unlocked_guides": [],
        "selected_dungeon_ids": [],
        "unlocked_dungeons": [],
        "completed_dungeons": [],
        "completed_battles": {},
        "battle_runtime": {},
        "fast_battle_runtime": {},
        "activity_task_battles": {},
        "maze_runtime": {},
        "tower_runtime": {},
        "special_battles": {},
        "tower_battles": {},
        "pending_drop_entitlements": [],
        "tutorial_completed": False,
        "unlocked_features": [],
        "buildings": {},
        "task_battles": {
            "current_cycle_type": 0,
            "hero_id": 0,
            "enemy_id": 1100001,
            "enemy_level": 1,
            "main_task_id": 0,
            "last_result": 0,
        },
        "tasks": {},
        "task_reward_history": [],
        "stage_tasks": {},
        "plots": {},
        "plot_rewards": {},
        "story_progress": {},
        "story_words": {},
        "chapter_rewards": {},
        "chapter_difficulties": {},
        "chapter_progress": {},
        "followed_chapter_id": 0,
        "inventory": {},
        "inventory_seed_version": 0,
        "inventory_seed_catalog_version": "",
        "resource_balances": {},
        "reward_history": [],
        "resource_history": [],
        "friends": [],
        "applied_friends": [],
        "assist_gospel": {
            "owner_uid": 0,
            "uid": 0,
            "friend_point": 0,
        },
        "hero_appearances": {},
        "hero_interactive_counts": {},
        "main_model_id": 0,
        "main_model": {},
        "main_board_item": {},
        "lilia_content": {},
        "gospel_appearances": {},
        "gospel_skins": {},
        "gospel_skin_status": {},
        "rune_items": [],
        "hero_runes": [],
        "collectibles_seed_version": 0,
        "decoration_items": [],
        "medals": [],
        "medal_slots": [],
        "medal_passive_switches": {},
        "archive_state": {},
        "spring_v2": {},
        "life_content": {},
        "activity_state": {},
        "card_pool_groups": {},
        "card_pools": {},
        "draw_history": [],
        "activity_lottery": {},
        "lottery_rng_seed": int(role_id),
        "lottery_rng_counter": 0,
        "soul_gacha": {},
        "first_charge_claimed": False,
        "feature_flags": {},
        "raw_requests": {},
        "created_at": int(time.time()),
        "updated_at": int(time.time()),
    }


def _read_varint(data: bytes, offset: int) -> Tuple[int, int]:
    value = 0
    shift = 0
    while offset < len(data) and shift < 70:
        current = data[offset]
        offset += 1
        value |= (current & 0x7F) << shift
        if not current & 0x80:
            return value, offset
        shift += 7
    raise ValueError("invalid or truncated protobuf varint")


def _raw_uint_fields(value: Any) -> Dict[int, int]:
    """Decode scalar protobuf fields from one legacy ``raw_requests`` value."""

    if not isinstance(value, str):
        return {}
    try:
        data = bytes.fromhex(value)
    except ValueError:
        return {}
    fields: Dict[int, int] = {}
    offset = 0
    try:
        while offset < len(data):
            tag, offset = _read_varint(data, offset)
            number, wire_type = tag >> 3, tag & 7
            if number <= 0:
                return {}
            if wire_type == 0:
                fields[number], offset = _read_varint(data, offset)
            elif wire_type == 1:
                offset += 8
            elif wire_type == 2:
                size, offset = _read_varint(data, offset)
                offset += size
            elif wire_type == 5:
                offset += 4
            else:
                return {}
            if offset > len(data):
                return {}
    except ValueError:
        return {}
    return fields


def _recover_legacy_generic_successes(profile: MutableMapping[str, Any]) -> bool:
    """Load state fields omitted by earlier schema versions."""

    raw_requests = profile.get("raw_requests")
    if not isinstance(raw_requests, Mapping):
        return False
    changed = False

    branch_fields = _raw_uint_fields(raw_requests.get("CS_AddBranchGuideStep"))
    branch_id = branch_fields.get(1, 0)
    if 0 < branch_id <= 0xFFFFFFFF:
        raw_steps = profile.get("guide_steps")
        steps = dict(raw_steps) if isinstance(raw_steps, Mapping) else {}
        key = str(branch_id)
        if steps.get(key) is not True:
            steps[key] = True
            profile["guide_steps"] = steps
            changed = True

    accept_fields = _raw_uint_fields(raw_requests.get("CS_SpringV2AcceptTask"))
    spring_gospel_id = accept_fields.get(1, 0)
    if spring_gospel_id in SPRING_V2_GOSPEL_IDS:
        raw_spring = profile.get("spring_v2")
        spring = dict(raw_spring) if isinstance(raw_spring, Mapping) else {}
        raw_gospels = spring.get("gospels")
        gospels = dict(raw_gospels) if isinstance(raw_gospels, Mapping) else {}
        source = gospels.get(str(spring_gospel_id), gospels.get(spring_gospel_id))
        gospel = dict(source) if isinstance(source, Mapping) else {}
        if gospel.get("daily_task_accepted") is not True:
            gospel["daily_task_accepted"] = True
            gospels[str(spring_gospel_id)] = gospel
            if spring_gospel_id in gospels:
                del gospels[spring_gospel_id]
            spring["gospels"] = gospels
            profile["spring_v2"] = spring
            changed = True

    celebration_fields = _raw_uint_fields(raw_requests.get("CS_ReceiveCelebration"))
    main_activity_id = celebration_fields.get(1, 0)
    sub_activity_id = celebration_fields.get(2, 0)
    if (
        main_activity_id == ANNIVERSARY_ACTIVITY_ID
        and sub_activity_id in ANNIVERSARY_SUB_ACTIVITY_IDS
    ):
        raw_state = profile.get("activity_state")
        state = dict(raw_state) if isinstance(raw_state, Mapping) else {}
        raw_claims = state.get("celebration_claims")
        claims = dict(raw_claims) if isinstance(raw_claims, Mapping) else {}
        key = str(main_activity_id)
        raw_ids = claims.get(key, claims.get(main_activity_id, []))
        claimed_ids = (
            {
                int(item)
                for item in raw_ids
                if type(item) is int and 0 < item <= 0xFFFFFFFF
            }
            if isinstance(raw_ids, (list, tuple, set, frozenset))
            else set()
        )
        if sub_activity_id not in claimed_ids:
            claimed_ids.add(sub_activity_id)
            claims[key] = sorted(claimed_ids)
            if main_activity_id in claims:
                del claims[main_activity_id]
            state["celebration_claims"] = claims
            profile["activity_state"] = state
            changed = True

    return changed


def _merge_profile(profile: MutableMapping[str, Any], role_id: int) -> bool:
    """Add fields introduced by newer server revisions to old save files."""

    defaults = new_profile(role_id, str(profile.get("role_name") or "Local Guest"))
    changed = False
    for key, value in defaults.items():
        if key not in profile:
            profile[key] = copy.deepcopy(value)
            changed = True

    # These are schema-owned records rather than open-ended domain maps.  A
    # top-level-only migration left old records without newly introduced
    # members, while replacing the whole object would discard account values.
    for key in ("task_battles", "assist_gospel"):
        default = defaults[key]
        current = profile.get(key)
        if not isinstance(current, MutableMapping):
            profile[key] = copy.deepcopy(default)
            changed = True
            continue
        for nested_key, nested_default in default.items():
            if nested_key not in current:
                current[nested_key] = copy.deepcopy(nested_default)
                changed = True

    # Profiles created by the earliest compatibility server used a guessed
    # model ID (3013001) as the hero configuration ID.  That row is absent
    # from the bundled a_people table and can make runtime initialization
    # dereference a null configuration.  Preserve the durable UID/formation
    # references while migrating only the invalid local configuration IDs.
    heroes = profile.get("heroes")
    if isinstance(heroes, list):
        for hero in heroes:
            if not isinstance(hero, MutableMapping):
                continue
            try:
                hero_id = int(hero.get("id") or 0)
            except (TypeError, ValueError, OverflowError):
                hero_id = 0
            if hero_id not in VALID_LOCAL_HERO_IDS:
                hero["id"] = 3000001
                hero["appearance_id"] = 3000001
                hero["profession_id"] = 3900000
                changed = True
    try:
        saved_role_id = int(profile.get("role_id") or role_id)
    except (TypeError, ValueError, OverflowError):
        saved_role_id = int(role_id)
    if profile.get("role_id") != saved_role_id:
        profile["role_id"] = saved_role_id
        changed = True
    if changed:
        profile["updated_at"] = int(time.time())
    return changed


class StateSuspendedForImport(RuntimeError):
    """This process has handed its save to the native import manager."""


class PersistentStateStore:
    """Thread-safe JSON-backed account store.

    ``path=None`` creates an in-memory store, which is useful for unit tests.
    Writes are atomic and use a neighbouring temporary file, so a terminated
    server cannot leave a half-written save document behind.
    """

    def __init__(
        self,
        path: Optional[Path] = None,
        role_seed: int = 1000000001,
    ) -> None:
        self.path = Path(path).expanduser().resolve() if path else None
        self.backup_path = (
            self.path.with_name(f"{self.path.name}.bak") if self.path else None
        )
        self.role_seed = int(role_seed)
        self._lock = threading.RLock()
        self._suspended_for_import = False
        self.recovery_source: Optional[Path] = None
        self.corrupt_path: Optional[Path] = None
        self._quarantine_corrupt_main = False
        self._data, migrated = self._load()
        if migrated:
            self.save()

    @property
    def initial_account_mode(self) -> Optional[str]:
        return self._data.get("new_account_mode")

    def _empty(self) -> Dict[str, Any]:
        return {
            "schema_version": STATE_SCHEMA_VERSION,
            "next_role_id": self.role_seed,
            "accounts": {},
        }

    @staticmethod
    def _decode_document(payload: str) -> Optional[Dict[str, Any]]:
        try:
            data = json.loads(payload)
        except (TypeError, ValueError):
            return None
        if not isinstance(data, dict) or not isinstance(data.get("accounts"), dict):
            return None
        if "new_account_mode" in data and data["new_account_mode"] not in ("newcomer", "all-unlocked"):
            raise ValueError("Unknown new-account save preset; refusing to replace it")
        schema_version = data.get("schema_version", 0)
        if isinstance(schema_version, bool):
            return None
        try:
            schema_version = int(schema_version)
        except (TypeError, ValueError, OverflowError):
            return None
        if schema_version > STATE_SCHEMA_VERSION:
            raise FutureStateSchemaError(
                f"save schema {schema_version} is newer than supported "
                f"schema {STATE_SCHEMA_VERSION}"
            )
        if any(
            not isinstance(account, str) or not isinstance(profile, dict)
            for account, profile in data["accounts"].items()
        ):
            return None
        return data

    def _read_document(self, path: Path) -> Optional[Dict[str, Any]]:
        try:
            return self._decode_document(path.read_text(encoding="utf-8"))
        except OSError:
            return None

    def _load(self) -> Tuple[Dict[str, Any], bool]:
        if self.path is None:
            return self._empty(), False
        data = self._read_document(self.path) if self.path.is_file() else None
        if data is None:
            if self.path.is_file():
                self._quarantine_corrupt_main = True
            if self.backup_path is not None and self.backup_path.is_file():
                recovered = self._read_document(self.backup_path)
                if recovered is not None:
                    self.recovery_source = self.backup_path
                    data = recovered
                    # Recreate the primary save immediately after migrations.
                    migrated = True
                else:
                    migrated = False
            else:
                migrated = False
            if data is None:
                return self._empty(), False
        else:
            migrated = False
        accounts = data["accounts"]

        try:
            source_version = int(data.get("schema_version") or 0)
        except (TypeError, ValueError, OverflowError):
            source_version = 0
        if source_version < STATE_SCHEMA_VERSION:
            for profile in accounts.values():
                if not isinstance(profile, MutableMapping):
                    continue
                # This recovery belongs only to the pre-v4 schema. Running
                # it again at every later version can resurrect a legacy
                # raw request whose current projection was intentionally reset.
                if source_version < 4:
                    migrated = _recover_legacy_generic_successes(profile) or migrated
                try:
                    role_id = int(profile.get("role_id") or self.role_seed)
                except (TypeError, ValueError, OverflowError):
                    role_id = self.role_seed
                migrated = _merge_profile(profile, role_id) or migrated
            data["schema_version"] = STATE_SCHEMA_VERSION
            migrated = True

        maximum_role_id = self.role_seed - 1
        for profile in accounts.values():
            if not isinstance(profile, Mapping):
                continue
            try:
                candidate = int(profile.get("role_id") or 0)
            except (TypeError, ValueError, OverflowError):
                continue
            maximum_role_id = max(maximum_role_id, candidate)
        try:
            next_role_id = int(data.get("next_role_id") or self.role_seed)
        except (TypeError, ValueError, OverflowError):
            next_role_id = self.role_seed
        safe_next_role_id = max(self.role_seed, maximum_role_id + 1, next_role_id)
        if data.get("next_role_id") != safe_next_role_id:
            data["next_role_id"] = safe_next_role_id
            migrated = True
        return data, migrated

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        try:
            descriptor = os.open(str(path), flags)
        except OSError:
            return
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _write_atomic(self, path: Path, payload: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=str(path.parent),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as target:
                target.write(payload)
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary_name, path)
            self._fsync_directory(path.parent)
        finally:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass

    def _archive_corrupt_main_locked(self) -> None:
        if (
            not self._quarantine_corrupt_main
            or self.path is None
            or not self.path.is_file()
        ):
            return
        timestamp = int(time.time())
        candidate = self.path.with_name(f"{self.path.name}.corrupt-{timestamp}")
        counter = 1
        while candidate.exists():
            candidate = self.path.with_name(
                f"{self.path.name}.corrupt-{timestamp}-{counter}"
            )
            counter += 1
        os.replace(self.path, candidate)
        self.corrupt_path = candidate
        self._quarantine_corrupt_main = False
        self._fsync_directory(candidate.parent)

    def _save_locked(self) -> None:
        if self._suspended_for_import:
            raise StateSuspendedForImport("save writes are suspended for import")
        if self.path is None:
            return
        payload = (
            json.dumps(
                self._data,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )
        # Validate the exact payload before touching either durable slot.
        if self._decode_document(payload) is None:  # pragma: no cover - defensive
            raise ValueError("refusing to persist an invalid state document")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._archive_corrupt_main_locked()
        if self.path.is_file() and self.backup_path is not None:
            try:
                previous_payload = self.path.read_text(encoding="utf-8")
            except OSError:
                previous_payload = ""
            if self._decode_document(previous_payload) is not None:
                self._write_atomic(self.backup_path, previous_payload)
        self._write_atomic(self.path, payload)

    def save(self) -> None:
        """Atomically persist the current state, if this is a file store."""

        if self.path is None:
            return
        with self._lock:
            self._save_locked()

    @staticmethod
    def _touch_profile(profile: MutableMapping[str, Any]) -> None:
        try:
            revision = int(profile.get("save_revision") or 0)
        except (TypeError, ValueError, OverflowError):
            revision = 0
        profile["save_revision"] = revision + 1
        profile["updated_at"] = int(time.time())

    def _ensure_account_locked(
        self,
        key: str,
        role_name: str,
    ) -> Tuple[Dict[str, Any], bool]:
        accounts = self._data["accounts"]
        profile = accounts.get(key)
        changed = False
        if not isinstance(profile, dict):
            role_id = int(self._data.get("next_role_id", self.role_seed))
            self._data["next_role_id"] = role_id + 1
            profile = new_profile(role_id, role_name)
            accounts[key] = profile
            changed = True
        had_role_name = bool(profile.get("role_name"))
        try:
            role_id = int(profile.get("role_id") or self.role_seed)
        except (TypeError, ValueError, OverflowError):
            role_id = self.role_seed
        changed = _merge_profile(profile, role_id) or changed
        if role_name and role_name != "Local Guest" and not had_role_name:
            profile["role_name"] = role_name
            changed = True
        return profile, changed

    def ensure_account(
        self, account: str, role_name: str = "Local Guest"
    ) -> Dict[str, Any]:
        key = account or "local"
        with self._lock:
            if self._suspended_for_import:
                profile = self._data["accounts"].get(key)
                if isinstance(profile, dict):
                    return copy.deepcopy(profile)
                raise StateSuspendedForImport("cannot create an account during import")
            previous_data = copy.deepcopy(self._data)
            profile, changed = self._ensure_account_locked(key, role_name)
            if changed:
                try:
                    self._touch_profile(profile)
                    self._save_locked()
                except Exception:
                    self._data = previous_data
                    raise
            return copy.deepcopy(profile)

    def get_account(self, account: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            profile = self._data["accounts"].get(account or "local")
            return copy.deepcopy(profile) if isinstance(profile, dict) else None

    def update_account(self, account: str, **changes: Any) -> Dict[str, Any]:
        return self.mutate_account(account, lambda profile: profile.update(changes))

    def transact_account(
        self, account: str, evaluator: Any
    ) -> Tuple[Any, Dict[str, Any]]:
        """Evaluate and commit one serializable account transaction.

        ``evaluator`` runs while the account lock is held and receives a
        local candidate profile.  It may inspect current values and mutate
        the candidate, eliminating the detached read/evaluate/write race.
        Its result and the committed detached profile are returned together.

        A failing evaluator or durable save cannot leak partial authoritative
        memory.  Returned profiles are detached so later edits cannot bypass
        the store's lock or commit boundary.
        """

        key = account or "local"
        with self._lock:
            if self._suspended_for_import:
                raise StateSuspendedForImport("save writes are suspended for import")
            accounts = self._data["accounts"]
            previous_data = copy.deepcopy(self._data)
            try:
                profile, bootstrapped = self._ensure_account_locked(
                    key,
                    "Local Guest",
                )
                original = copy.deepcopy(profile)
                candidate = copy.deepcopy(profile)
                result = evaluator(candidate)
                # Reject non-JSON values before replacing the authoritative
                # in-memory profile or touching the primary/backup files.
                encoded_candidate = json.dumps(
                    candidate,
                    ensure_ascii=False,
                    sort_keys=True,
                )
                # A JSON round trip both validates the persisted value domain
                # and severs aliases inserted by callers through update().
                candidate = json.loads(encoded_candidate)

                if bootstrapped or candidate != original:
                    self._touch_profile(candidate)
                    accounts[key] = candidate
                    self._save_locked()
                else:
                    accounts[key] = profile
            except Exception:
                self._data = previous_data
                raise
            return result, copy.deepcopy(accounts[key])

    def mutate_account(self, account: str, mutator: Any) -> Dict[str, Any]:
        """Apply a copy-on-write account mutation and return its snapshot."""

        _, profile = self.transact_account(account, mutator)
        return profile

    def suspend_for_import(self) -> None:
        """Wait for the current transaction and permanently seal this instance.

        The native manager will terminate this process before taking the shared
        file lock. A new process must load the imported file into a fresh store;
        allowing this instance to resume would risk writing stale memory back.
        """
        with self._lock:
            self._suspended_for_import = True

    def snapshot(self, account: str) -> Dict[str, Any]:
        return self.ensure_account(account)
