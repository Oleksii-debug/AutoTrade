"""Current-main provider-free Host, artifact backup and restore acceptance."""

from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
from uuid import uuid4

from mvp.autotrade_mvp.backup import (
    BackupIntegrityError,
    restore_requires_reconciliation,
    verify_backup,
)
from mvp.autotrade_mvp.product_runtime import restore_product_backup
from mvp.tests.test_provider_free_product import ProductClient, ACCOUNT, ENVIRONMENT


class CurrentProductBackupBridge(unittest.TestCase):
    def _action(self, client, action, payload=None):
        state = client.state()
        command_id = str(uuid4())
        request = {
            "command_id": command_id,
            "idempotency_key": command_id,
            "expected_state_version": state["state_version"],
            "actor": "local-owner",
            "session": state["permission_summary"]["session"],
            "account_id": ACCOUNT,
            "environment": ENVIRONMENT,
            "action": action,
            "payload": payload or {},
        }
        status, accepted, _ = client.request("POST", "/api/v1/commands", request)
        self.assertEqual(status, 200, accepted)
        self.assertEqual(accepted["status"], "ACCEPTED")
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            status, operation, _ = client.request(
                "GET", "/api/v1/operations/" + accepted["operation_id"]
            )
            self.assertEqual(status, 200, operation)
            if operation["phase"] in {"SUCCEEDED", "FAILED", "UNKNOWN"}:
                return operation
            time.sleep(0.1)
        self.fail("durable Host operation did not reach a terminal phase")

    def test_current_host_backup_restores_authenticated_artifacts(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "product"
            restored = root / "restored"
            client = ProductClient(source)
            try:
                self.assertEqual(client.state()["environment"], "SIMULATION")
                started = self._action(
                    client, "START_SIMULATION", {"stop_after_episodes": 5}
                )
                self.assertEqual(started["phase"], "SUCCEEDED", started)
                state = client.state()
                self.assertEqual(state["portfolio"]["status"]["session_status"], "PAUSED")
                self.assertGreater(len(state["portfolio"]["fills"]), 0)
                self.assertEqual(state["risk"]["real_order_submission"], "UNAVAILABLE")
                backup_result = self._action(client, "BACKUP_SIMULATION")
                self.assertEqual(backup_result["phase"], "SUCCEEDED", backup_result)
            finally:
                client.close()

            backups = list((source / "backups").iterdir())
            self.assertEqual(len(backups), 1)
            backup = backups[0]
            manifest = verify_backup(backup)
            self.assertTrue(any(
                entry["kind"] == "runtime-artifact-manifest"
                for entry in manifest["files"]
            ))
            restore_product_backup(backup, restored)
            self.assertTrue(restore_requires_reconciliation(restored))

            client = ProductClient(restored)
            try:
                resumed = client.state()
                self.assertEqual(
                    resumed["portfolio"]["status"]["cash"],
                    state["portfolio"]["status"]["cash"],
                )
                self.assertEqual(
                    len(resumed["portfolio"]["fills"]),
                    len(state["portfolio"]["fills"]),
                )
                self.assertEqual(
                    resumed["risk"]["restore_trading_gate"],
                    "RECONCILIATION_REQUIRED",
                )
                # The source-local runtime signing key is deliberately absent
                # from portable backups. Recovery stays UNKNOWN until a fresh
                # authority is reconstituted and the restore gate is cleared.
                recovery = self._action(client, "RECOVER_SIMULATION")
                self.assertEqual(recovery["phase"], "UNKNOWN", recovery)
                stopped = client.state()
                self.assertEqual(
                    stopped["portfolio"]["status"]["session_status"],
                    "PAUSED",
                )
                self.assertEqual(
                    len(stopped["portfolio"]["fills"]),
                    len(resumed["portfolio"]["fills"]),
                )
            finally:
                client.close()

            runtime_manifest = next((backup / "artifacts" / "manifests").glob("*.json"))
            runtime_manifest.write_bytes(b"corrupt")
            with self.assertRaises(BackupIntegrityError):
                verify_backup(backup)


if __name__ == "__main__":
    unittest.main()
