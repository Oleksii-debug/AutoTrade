"""Plan 8 S4: one real product Host continuation after verified restore.

Provider-free only. Financial truth comes exclusively from the retained journal.
A new recovery receipt is not permission to create a second order/fill/settlement.
"""
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import os
import unittest

from mvp.autotrade_mvp.backup import verify_backup, restore_requires_reconciliation
from mvp.autotrade_mvp.product_runtime import restore_product_backup
from mvp.autotrade_mvp.simulation_commands import (
    _protocol, _completed_quarantined_restore_is_read_only,
)
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.backup import BackupIntegrityError
from mvp.autotrade_mvp.provider_activity_accounting import DurableProviderEconomicBook
from mvp.autotrade_mvp.simulation_session import ACCOUNT, ENVIRONMENT, PROVIDER, INSTRUMENT
from mvp.tests.test_provider_free_product import ProductClient


class Plan8M1RecoveryContinuation(unittest.TestCase):
    def test_backup_inventory_captures_only_verified_uuid_manifests_and_objects(self):
        from autotrade_runtime.artifacts.store import ArtifactStore
        from mvp.autotrade_mvp.backup import (
            _artifact_source_files, _expected_kind, _verify_v1_artifact_manifest,
            BackupIntegrityError,
        )
        from hashlib import sha256
        import json

        with TemporaryDirectory() as directory:
            root = Path(directory) / "artifacts"
            store = ArtifactStore(root)
            manifest = store.publish_bytes(
                artifact_id="399b5809-1985-59f0-a8b1-3204decdaaf2",
                data=b"immutable SETTLEMENT rule evidence",
                media_type="application/octet-stream",
                rights={"storage": True, "export": False},
                source_refs=[],
                metadata={"purpose": "signed-settlement-rule"},
            )
            manifest_path = root / "manifests" / (manifest["artifact_id"] + ".json")
            sources = {(p.relative_to(root).as_posix(), kind)
                       for p, kind in _artifact_source_files(root)}
            self.assertIn(("manifests/" + manifest["artifact_id"] + ".json",
                           "artifact-manifest-v1"), sources)
            self.assertIn(
                ("objects/sha256/" + manifest["sha256"][7:9] + "/" +
                 manifest["sha256"][7:], "artifact-object"),
                sources,
            )
            self.assertEqual(
                _expected_kind("artifacts/manifests/" + manifest["artifact_id"] + ".json"),
                "artifact-manifest-v1",
            )
            _verify_v1_artifact_manifest(root, manifest_path)
            with self.assertRaises(BackupIntegrityError):
                _verify_v1_artifact_manifest(root, manifest_path,
                    present_paths=set())
            raw = manifest_path.read_bytes()
            changed = json.loads(raw)
            changed["rights"]["export"] = True
            manifest_path.write_text(json.dumps(changed), encoding="utf-8")
            with self.assertRaises(BackupIntegrityError):
                _verify_v1_artifact_manifest(root, manifest_path)
            manifest_path.write_bytes(raw)
            digest = sha256(b"immutable SETTLEMENT rule evidence").hexdigest()
            (root / "objects" / "sha256" / digest[:2] / digest).write_bytes(
                b"tampered SETTLEMENT rule evidence"
            )
            with self.assertRaises(BackupIntegrityError):
                _verify_v1_artifact_manifest(root, manifest_path)

    def test_restored_product_repeated_recovery_and_restart_conserve_financial_effects(self):
        with TemporaryDirectory() as directory:
            source = Path(directory) / "source"
            client = ProductClient(source)
            try:
                _, started = client.command("START_SIMULATION")
                self.assertEqual(started["phase"], "SUCCEEDED", started)
                before = client.state()
                self.assertEqual(before["portfolio"]["status"]["session_status"], "COMPLETED")
                self.assertEqual(before["risk"]["real_order_submission"], "UNAVAILABLE")
                aggregates = ("settlement_book", "reservation_book", "risk_decision", "simulation_agent_decision")
                counts = {
                    name: len(client.runtime.journal.load_events_by_aggregate_type(name))
                    for name in aggregates
                }
                for name in aggregates:
                    self.assertGreater(counts[name], 0, name)
                original_orders = before["portfolio"]["orders"]
                original_fills = before["portfolio"]["fills"]
                original_cash = before["portfolio"]["status"]["cash"]
                original_position = before["portfolio"]["status"]["position"]
                backup_id, saved = client.command("BACKUP_SIMULATION")
                self.assertEqual(saved["phase"], "SUCCEEDED", saved)
            finally:
                client.close()

            backup = source / "backups" / backup_id
            verify_backup(backup)
            restored = Path(directory) / "restored"
            self.assertEqual(restore_product_backup(backup, restored), restored)
            self.assertTrue(restore_requires_reconciliation(restored))

            def verify_cut(current):
                state = current.state()
                self.assertEqual(state["portfolio"]["orders"], original_orders)
                self.assertEqual(state["portfolio"]["fills"], original_fills)
                self.assertEqual(state["portfolio"]["status"]["cash"], original_cash)
                self.assertEqual(state["portfolio"]["status"]["position"], original_position)
                self.assertTrue(state["portfolio"]["status"]["replay_verified"])
                self.assertTrue(state["portfolio"]["economic_report"]["reconciled"])
                self.assertEqual(state["risk"]["real_order_submission"], "UNAVAILABLE")
                self.assertEqual(state["risk"]["restore_trading_gate"], "RECONCILIATION_REQUIRED")
                store = current.runtime.journal
                self.assertEqual({
                    name: len(store.load_events_by_aggregate_type(name))
                    for name in aggregates
                }, counts)
                book = DurableProviderEconomicBook(
                    store, provider_id=PROVIDER, account_id=ACCOUNT,
                    environment=ENVIRONMENT,
                )
                self.assertEqual(str(book.position(INSTRUMENT)), original_position)

            client = ProductClient(restored)
            try:
                verify_cut(client)
                for _ in range(2):
                    # Record only the canonical worker stage/exception class on
                    # failure. Keep UNKNOWN fail-closed and never accept it as
                    # a successful recovered financial state.
                    with patch.dict(os.environ, {"AUTOTRADE_TEST_DIAGNOSTIC": "1"}):
                        _, recovered = client.command("RECOVER_SIMULATION")
                    diagnostic = restored / "worker-stage-diagnostic.txt"
                    self.assertEqual(
                        recovered["phase"], "SUCCEEDED",
                        {"operation": recovered, "worker_stage":
                         diagnostic.read_text(encoding="utf-8")
                         if diagnostic.is_file() else "NO_WORKER_DIAGNOSTIC"},
                    )
                    verify_cut(client)
            finally:
                client.close()

            # A completely new Host process lifecycle must not replay financial
            # effects, clear reconciliation, or expand live submission authority.
            client = ProductClient(restored)
            try:
                verify_cut(client)
            finally:
                client.close()

            # Read-only acknowledgement never forges the original runtime
            # signer, never clears the restore gate, and never repeats fills.
            state_root = restored / "state"
            protocol = _protocol(JournalStore(state_root / "journal.sqlite3"))
            self.assertFalse((state_root / ".autonomous-runtime-authority.key").exists())
            self.assertFalse((state_root / "autonomous-runtime-checkpoint.json").exists())
            self.assertTrue(restore_requires_reconciliation(restored))
            self.assertTrue(_completed_quarantined_restore_is_read_only(state_root, protocol))

            forged_key = state_root / ".autonomous-runtime-authority.key"
            forged_key.write_bytes(b"X" * 32)
            forged_key.chmod(0o600)
            with self.assertRaisesRegex(ValueError, "unexpected executable runtime authority"):
                _completed_quarantined_restore_is_read_only(state_root, protocol)
            forged_key.unlink()

            evidence = restored / "restore-evidence" / "autonomous-runtime-checkpoint.json"
            original = evidence.read_bytes()
            evidence.write_bytes(original + b"tamper")
            with self.assertRaises(BackupIntegrityError):
                _completed_quarantined_restore_is_read_only(state_root, protocol)
            evidence.write_bytes(original)
            self.assertTrue(_completed_quarantined_restore_is_read_only(state_root, protocol))
            verify_backup(backup)


if __name__ == "__main__":
    unittest.main()
