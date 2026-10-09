"""Plan 8 S4: one real product Host continuation after verified restore.

Provider-free only. Financial truth comes exclusively from the retained journal.
A new recovery receipt is not permission to create a second order/fill/settlement.
"""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.backup import verify_backup, restore_requires_reconciliation
from mvp.autotrade_mvp.product_runtime import restore_product_backup
from mvp.autotrade_mvp.provider_activity_accounting import DurableProviderEconomicBook
from mvp.autotrade_mvp.simulation_session import ACCOUNT, ENVIRONMENT, PROVIDER, INSTRUMENT
from mvp.tests.test_provider_free_product import ProductClient


class Plan8M1RecoveryContinuation(unittest.TestCase):
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
                aggregates = ("settlement_book", "risk_decision", "simulation_agent_decision")
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
                    _, recovered = client.command("RECOVER_SIMULATION")
                    self.assertEqual(recovered["phase"], "SUCCEEDED", recovered)
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
            verify_backup(backup)


if __name__ == "__main__":
    unittest.main()
