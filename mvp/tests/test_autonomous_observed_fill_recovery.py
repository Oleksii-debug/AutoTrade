"""Crash recovery consumes retained simulation fills, never ACK or a new send."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from mvp.autotrade_mvp import simulation_session as session
from mvp.autotrade_mvp.durable_order_projection import DurableOrderBookProjection
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.simulated_provider import SimulatedProvider
from mvp.tests.test_autonomous_simulation import run, PRICES
from mvp.autotrade_mvp.accounting import book_external_cash_flow
from mvp.autotrade_mvp.provider_activity_accounting import DurableProviderEconomicBook


class ObservedFillRecoveryTests(unittest.TestCase):
    def test_crash_matrix_recovers_same_fill_without_authority_or_resend(self):
        prices = ["100", "101", "103"]
        with TemporaryDirectory() as reference_dir:
            reference = run(reference_dir, prices)
        for phase in ("before_oms", "after_oms", "before_finance", "after_finance", "before_completed"):
            with self.subTest(phase=phase), TemporaryDirectory() as directory:
                original_fill = DurableOrderBookProjection.record_fill
                original_finance = session.commit_economic_batch_with_reservation_consumption
                original_event = session._loop_event

                def fill(book, **kwargs):
                    if phase == "before_oms":
                        raise RuntimeError("simulated crash")
                    result = original_fill(book, **kwargs)
                    if phase == "after_oms":
                        raise RuntimeError("simulated crash")
                    return result

                def finance(*args, **kwargs):
                    if phase == "before_finance":
                        raise RuntimeError("simulated crash")
                    result = original_finance(*args, **kwargs)
                    if phase == "after_finance":
                        raise RuntimeError("simulated crash")
                    return result

                def event(store, run_id, kind, key, payload, now):
                    if phase == "before_completed" and kind == "AutonomousEpisodeCompleted" and payload["episode"] == 3:
                        raise RuntimeError("simulated crash")
                    return original_event(store, run_id, kind, key, payload, now)

                with patch.object(DurableOrderBookProjection, "record_fill", fill), \
                     patch.object(session, "commit_economic_batch_with_reservation_consumption", finance), \
                     patch.object(session, "_loop_event", event):
                    with self.assertRaisesRegex(RuntimeError, "simulated crash"):
                        run(directory, prices)
                # Operator inspection remains read-only and exposes recovery.
                from mvp.autotrade_mvp.cli import get_status
                store = JournalStore(Path(directory) / "journal.sqlite3")
                cut = store.current_journal_sequence()
                self.assertEqual(get_status(directory)["status"], "needs_recovery")
                self.assertEqual(store.current_journal_sequence(), cut)

                with patch.object(SimulatedProvider, "transport_send", side_effect=AssertionError("no resend")), \
                     patch.object(session.AuthorityService, "admit", side_effect=AssertionError("no fresh risk admission")):
                    recovered = run(directory, prices)
                self.assertEqual(recovered["status"], "COMPLETED")
                self.assertEqual(recovered["new_outbound_requests"], 0)
                self.assertEqual(recovered["decisions"], reference["decisions"])
                self.assertEqual(recovered["cash"], reference["cash"])
                self.assertEqual(recovered["position"], "1")
                cut = store.current_journal_sequence()
                self.assertEqual(run(directory, prices)["new_outbound_requests"], 0)
                self.assertEqual(store.current_journal_sequence(), cut)

    def test_recovered_buy_continues_and_sell_commit_ack_loss_recovers(self):
        with TemporaryDirectory() as reference_dir:
            reference = run(reference_dir)
        with TemporaryDirectory() as directory:
            original = session.commit_economic_batch_with_reservation_consumption

            def crash_sell(*args, **kwargs):
                result = original(*args, **kwargs)
                if kwargs["committed_at"] == "2026-10-03T00:00:04Z":
                    raise RuntimeError("sell commit response lost")
                return result

            with patch.object(session, "commit_economic_batch_with_reservation_consumption", crash_sell):
                with self.assertRaisesRegex(RuntimeError, "sell commit response lost"):
                    run(directory)
            with patch.object(SimulatedProvider, "transport_send", side_effect=AssertionError("no resend")):
                recovered = run(directory, stop_after_episodes=5)
            self.assertEqual(recovered["completed_episodes"], 5)
            self.assertEqual(recovered["position"], "0")
            self.assertEqual(recovered["new_outbound_requests"], 0)
            completed = run(directory)
            self.assertEqual(completed["decisions"], reference["decisions"])
            self.assertEqual(completed["new_outbound_requests"], 1)

    def test_foreign_offsetting_economics_fail_before_recovery_mutation(self):
        with TemporaryDirectory() as directory:
            with patch.object(DurableOrderBookProjection, "record_fill", side_effect=RuntimeError("crash")):
                with self.assertRaises(RuntimeError):
                    run(directory, PRICES[:3])
            store = JournalStore(Path(directory) / "journal.sqlite3")
            economics = DurableProviderEconomicBook(store, provider_id=session.PROVIDER,
                account_id=session.ACCOUNT, environment=session.ENVIRONMENT)
            for key, value in (("foreign-plus", "1"), ("foreign-minus", "-1")):
                economics.append(book_external_cash_flow(transaction_id=key, cause_event_id=key,
                    currency="USD", amount=value))
            cut = store.current_journal_sequence()
            with self.assertRaisesRegex(ValueError, "canonical economic history"):
                run(directory, PRICES[:3])
            self.assertEqual(store.current_journal_sequence(), cut)

    def test_changed_retained_fill_cannot_recover_from_matching_ack(self):
        original = session._loop_event

        def forge(store, run_id, kind, key, payload, now):
            if kind == "AutonomousEpisodeFillObserved":
                payload = {**payload, "fill": {**payload["fill"], "side": "SELL"}}
            return original(store, run_id, kind, key, payload, now)

        with TemporaryDirectory() as directory:
            with patch.object(session, "_loop_event", forge), \
                 patch.object(DurableOrderBookProjection, "record_fill", side_effect=RuntimeError("crash")):
                with self.assertRaises(RuntimeError):
                    run(directory, PRICES[:3])
            store = JournalStore(Path(directory) / "journal.sqlite3")
            cut = store.current_journal_sequence()
            with self.assertRaisesRegex(ValueError, "simulator history differs"):
                run(directory, PRICES[:3])
            self.assertEqual(store.current_journal_sequence(), cut)

    def test_owner_advance_during_financial_preparation_cannot_commit_fill_money(self):
        with TemporaryDirectory() as directory:
            with patch.object(DurableOrderBookProjection, "record_fill", side_effect=RuntimeError("crash")):
                with self.assertRaises(RuntimeError):
                    run(directory, PRICES[:3])
            original = DurableProviderEconomicBook.prepare_batch_mutation
            injected = False

            def race(book, *args, **kwargs):
                nonlocal injected
                if not injected:
                    injected = True
                    book.append(book_external_cash_flow(transaction_id="concurrent", cause_event_id="concurrent",
                        currency="USD", amount="1"))
                return original(book, *args, **kwargs)

            with patch.object(DurableProviderEconomicBook, "prepare_batch_mutation", race):
                with self.assertRaisesRegex(ValueError, "journal"):
                    run(directory, PRICES[:3])
            store = JournalStore(Path(directory) / "journal.sqlite3")
            economics = DurableProviderEconomicBook(store, provider_id=session.PROVIDER,
                account_id=session.ACCOUNT, environment=session.ENVIRONMENT)
            self.assertEqual(str(economics.cash("USD")), "1001")
            self.assertEqual(str(economics.position(session.INSTRUMENT)), "0")



if __name__ == "__main__":
    unittest.main()
