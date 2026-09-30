"""Transactional and recovery guarantees for the local account store."""

from __future__ import annotations

import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from server.state import (
    FutureStateSchemaError,
    PersistentStateStore,
    STATE_SCHEMA_VERSION,
)


class StateTransactionTests(unittest.TestCase):
    def test_failed_mutation_rolls_back_memory_and_disk(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state.json"
            store = PersistentStateStore(path, role_seed=700)
            baseline = store.update_account("Test", gold=123)

            def fail(profile: dict[str, object]) -> None:
                profile["gold"] = 999
                profile.setdefault("inventory", {})["broken"] = {"count": 1}
                raise RuntimeError("simulated handler failure")

            with self.assertRaisesRegex(RuntimeError, "simulated"):
                store.mutate_account("Test", fail)

            current = store.snapshot("Test")
            self.assertEqual(current["gold"], 123)
            self.assertNotIn("broken", current["inventory"])
            self.assertEqual(current["save_revision"], baseline["save_revision"])
            self.assertEqual(
                PersistentStateStore(path, role_seed=700).snapshot("Test"),
                current,
            )

    def test_read_results_are_detached_from_authoritative_state(self) -> None:
        store = PersistentStateStore(role_seed=701)
        first = store.ensure_account("Test")
        first["gold"] = 999_999
        first.setdefault("inventory", {})["external"] = {"count": 1}

        second = store.get_account("Test")
        assert second is not None
        self.assertEqual(second["gold"], 1000)
        self.assertNotIn("external", second["inventory"])

        second["gold"] = -1
        self.assertEqual(store.snapshot("Test")["gold"], 1000)

    def test_noop_transaction_does_not_rewrite_or_increment_revision(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state.json"
            store = PersistentStateStore(path, role_seed=702)
            initial = store.ensure_account("Test")
            before_bytes = path.read_bytes()

            returned = store.mutate_account("Test", lambda profile: None)

            self.assertEqual(returned["save_revision"], initial["save_revision"])
            self.assertEqual(path.read_bytes(), before_bytes)

    def test_valid_backup_recovers_corrupt_primary_without_data_loss(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state.json"
            store = PersistentStateStore(path, role_seed=703)
            store.update_account("Test", gold=111)
            store.update_account("Test", gold=222)
            assert store.backup_path is not None
            backup = json.loads(store.backup_path.read_text(encoding="utf-8"))
            self.assertEqual(backup["accounts"]["Test"]["gold"], 111)

            path.write_text("{broken-json", encoding="utf-8")
            recovered = PersistentStateStore(path, role_seed=703)

            self.assertEqual(recovered.recovery_source, recovered.backup_path)
            self.assertEqual(recovered.snapshot("Test")["gold"], 111)
            self.assertIsNotNone(recovered.corrupt_path)
            assert recovered.corrupt_path is not None
            self.assertEqual(
                recovered.corrupt_path.read_text(encoding="utf-8"),
                "{broken-json",
            )
            restored_document = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(restored_document["schema_version"], STATE_SCHEMA_VERSION)
            self.assertEqual(restored_document["accounts"]["Test"]["gold"], 111)

    def test_concurrent_transactions_do_not_lose_updates(self) -> None:
        store = PersistentStateStore(role_seed=704)
        store.update_account("Test", gold=0)

        def increment(_: int) -> None:
            for _ in range(100):
                store.mutate_account(
                    "Test",
                    lambda profile: profile.update(gold=int(profile["gold"]) + 1),
                )

        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(increment, range(8)))

        self.assertEqual(store.snapshot("Test")["gold"], 800)

    def test_non_json_mutation_is_rejected_before_commit(self) -> None:
        store = PersistentStateStore(role_seed=705)
        before = store.ensure_account("Test")

        with self.assertRaises(TypeError):
            store.mutate_account(
                "Test",
                lambda profile: profile.update(not_serializable={object()}),
            )

        self.assertEqual(store.snapshot("Test"), before)

    def test_transaction_returns_result_and_detached_committed_profile(self) -> None:
        store = PersistentStateStore(role_seed=706)

        def grant(profile: dict[str, object]) -> str:
            profile["gold"] = int(profile["gold"]) + 7
            return "granted"

        result, committed = store.transact_account("Test", grant)

        self.assertEqual(result, "granted")
        self.assertEqual(committed["gold"], 1007)
        committed["gold"] = -1
        self.assertEqual(store.snapshot("Test")["gold"], 1007)

    def test_primary_save_failure_keeps_memory_and_disk_at_previous_revision(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state.json"
            store = PersistentStateStore(path, role_seed=707)
            before = store.ensure_account("Test")
            original_save = store._save_locked

            def fail_save() -> None:
                raise OSError("simulated durable write failure")

            store._save_locked = fail_save
            try:
                with self.assertRaisesRegex(OSError, "simulated"):
                    store.mutate_account(
                        "Test",
                        lambda profile: profile.__setitem__("gold", 999999),
                    )
            finally:
                store._save_locked = original_save

            self.assertEqual(store.get_account("Test"), before)
            self.assertEqual(
                PersistentStateStore(path, role_seed=707).snapshot("Test"),
                before,
            )

    def test_update_account_severs_nested_caller_aliases(self) -> None:
        store = PersistentStateStore(role_seed=708)
        caller_value = {"nested": [1, 2]}
        committed = store.update_account("Test", extension=caller_value)
        caller_value["nested"].append(3)

        self.assertEqual(committed["extension"], {"nested": [1, 2]})
        self.assertEqual(
            store.snapshot("Test")["extension"],
            {"nested": [1, 2]},
        )

    def test_semantically_invalid_primary_recovers_valid_backup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state.json"
            store = PersistentStateStore(path, role_seed=709)
            store.update_account("Test", gold=111)
            store.update_account("Test", gold=222)
            document = json.loads(path.read_text(encoding="utf-8"))
            document["accounts"]["Test"] = "not-a-profile"
            path.write_text(json.dumps(document), encoding="utf-8")

            recovered = PersistentStateStore(path, role_seed=709)

            self.assertEqual(recovered.snapshot("Test")["gold"], 111)
            self.assertEqual(recovered.recovery_source, recovered.backup_path)

    def test_future_schema_is_rejected_without_rewrite(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state.json"
            payload = json.dumps(
                {
                    "schema_version": STATE_SCHEMA_VERSION + 1,
                    "next_role_id": 710,
                    "accounts": {},
                }
            )
            path.write_text(payload, encoding="utf-8")

            with self.assertRaises(FutureStateSchemaError):
                PersistentStateStore(path, role_seed=710)

            self.assertEqual(path.read_text(encoding="utf-8"), payload)


if __name__ == "__main__":
    unittest.main()
