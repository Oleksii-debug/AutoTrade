"""Recovery tests for retained autonomous simulated fills.

A durable internal fill observation may complete an interrupted ZERO episode only
through historical admission/send evidence and the canonical atomic OMS+financial
writer. Missing retained fill evidence remains sticky UNKNOWN.
"""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from mvp.autotrade_mvp import simulation_session as session
from mvp.autotrade_mvp.accounting import book_external_cash_flow
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.provider_activity_accounting import (
    DurableProviderEconomicBook,
)
from mvp.autotrade_mvp.simulated_provider import SimulatedProvider
from mvp.tests.test_autonomous_simulation import PRICES, run


class AutonomousObservedFillRecoveryTests(unittest.TestCase):
    def test_crash_matrix_recovers_same_fill_without_admission_or_resend(self):
        prices = ["100", "101", "103"]
        with TemporaryDirectory() as reference_dir:
            reference = run(reference_dir, prices)

        for phase in ("before_atomic", "after_atomic", "before_completed"):
            with self.subTest(phase=phase), TemporaryDirectory() as directory:
                original_atomic = (
                    session.commit_order_fill_with_reservation_consumption
                )
                original_event = session._loop_event

                def atomic(*args, **kwargs):
                    if phase == "before_atomic":
                        raise RuntimeError("simulated crash")
                    result = original_atomic(*args, **kwargs)
                    if phase == "after_atomic":
                        raise RuntimeError("simulated crash")
                    return result

                def event(store, run_id, kind, key, payload, now):
                    if (
                        phase == "before_completed"
                        and kind == "AutonomousEpisodeCompleted"
                        and payload["episode"] == 3
                    ):
                        raise RuntimeError("simulated crash")
                    return original_event(
                        store, run_id, kind, key, payload, now
                    )

                with patch.object(
                    session,
                    "commit_order_fill_with_reservation_consumption",
                    atomic,
                ), patch.object(session, "_loop_event", event):
                    with self.assertRaisesRegex(
                        RuntimeError, "simulated crash"
                    ):
                        run(directory, prices)

                store = JournalStore(
                    Path(directory) / "journal.sqlite3"
                )
                observations = [
                    event
                    for event in store.load_events_by_aggregate_type(
                        "canonical_autonomous_simulation"
                    )
                    if event["event_type"]
                    == "AutonomousEpisodeFillObserved"
                ]
                self.assertEqual(len(observations), 1)

                from mvp.autotrade_mvp.cli import get_status

                cut = store.current_journal_sequence()
                status = get_status(directory)
                self.assertEqual(status["status"], "needs_recovery")
                self.assertTrue(status["retained_fill_observed"])
                self.assertEqual(
                    status["recovery_disposition"],
                    "RETAINED_FILL_RECOVERY",
                )
                self.assertEqual(store.current_journal_sequence(), cut)

                with patch.object(
                    SimulatedProvider,
                    "transport_send",
                    side_effect=AssertionError("no resend"),
                ), patch.object(
                    session.AuthorityService,
                    "admit",
                    side_effect=AssertionError("no fresh risk admission"),
                ):
                    recovered = run(directory, prices)

                self.assertEqual(recovered["status"], "COMPLETED")
                self.assertEqual(
                    recovered["new_outbound_requests"], 0
                )
                self.assertEqual(
                    recovered["decisions"], reference["decisions"]
                )
                self.assertEqual(recovered["cash"], reference["cash"])
                self.assertEqual(recovered["position"], "1")

                replay_cut = store.current_journal_sequence()
                replay = run(directory, prices)
                self.assertEqual(replay["new_outbound_requests"], 0)
                self.assertEqual(
                    store.current_journal_sequence(), replay_cut
                )

    def test_zero_wire_episode_recovers_across_started_and_reconciled_crashes(self):
        with TemporaryDirectory() as reference_dir:
            reference = run(reference_dir)
        self.assertIn(
            reference["decisions"][3]["decision"],
            {"HOLD", "NO_TRADE"},
        )

        for phase in ("after_started", "before_completed"):
            with self.subTest(phase=phase), TemporaryDirectory() as directory:
                original_event = session._loop_event

                def event(store, run_id, kind, key, payload, now):
                    if (
                        phase == "before_completed"
                        and kind == "AutonomousEpisodeCompleted"
                        and payload["episode"] == 4
                    ):
                        raise RuntimeError("zero-wire completion response lost")
                    value = original_event(
                        store, run_id, kind, key, payload, now
                    )
                    if (
                        phase == "after_started"
                        and kind == "AutonomousEpisodeStarted"
                        and payload["episode"] == 4
                    ):
                        self.assertIn(
                            payload["decision"], {"HOLD", "NO_TRADE"}
                        )
                        raise RuntimeError("zero-wire started response lost")
                    return value

                with patch.object(session, "_loop_event", event):
                    with self.assertRaisesRegex(
                        RuntimeError, "zero-wire .* response lost"
                    ):
                        run(directory)

                store = JournalStore(
                    Path(directory) / "journal.sqlite3"
                )
                before = store.current_journal_sequence()

                from mvp.autotrade_mvp.cli import get_status

                zero_status = get_status(directory)
                self.assertEqual(
                    zero_status["recovery_disposition"],
                    "ZERO_WIRE_COMPLETION",
                )
                self.assertEqual(
                    store.current_journal_sequence(), before
                )
                with patch.object(
                    SimulatedProvider,
                    "transport_send",
                    side_effect=AssertionError(
                        "zero-wire recovery cannot send"
                    ),
                ), patch.object(
                    session.AuthorityService,
                    "admit",
                    side_effect=AssertionError(
                        "zero-wire recovery cannot perform risk admission"
                    ),
                ):
                    recovered = run(
                        directory,
                        stop_after_episodes=4,
                    )

                self.assertEqual(recovered["status"], "PAUSED")
                self.assertEqual(recovered["completed_episodes"], 4)
                self.assertEqual(recovered["new_outbound_requests"], 0)
                self.assertEqual(
                    recovered["decisions"],
                    reference["decisions"][:4],
                )
                self.assertGreater(
                    store.current_journal_sequence(), before
                )

                completed = run(directory)
                self.assertEqual(completed["status"], "COMPLETED")
                self.assertEqual(
                    completed["decisions"], reference["decisions"]
                )
                self.assertEqual(completed["cash"], reference["cash"])
                self.assertEqual(
                    completed["position"], reference["position"]
                )

    def test_zero_wire_recovery_rejects_offsetting_post_start_economics(self):
        original_event = session._loop_event

        def crash_after_started(store, run_id, kind, key, payload, now):
            value = original_event(
                store, run_id, kind, key, payload, now
            )
            if (
                kind == "AutonomousEpisodeStarted"
                and payload["episode"] == 4
            ):
                self.assertIn(
                    payload["decision"], {"HOLD", "NO_TRADE"}
                )
                raise RuntimeError("zero-wire crash")
            return value

        with TemporaryDirectory() as directory:
            with patch.object(
                session, "_loop_event", crash_after_started
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "zero-wire crash"
                ):
                    run(directory)

            store = JournalStore(
                Path(directory) / "journal.sqlite3"
            )
            economics = DurableProviderEconomicBook(
                store,
                provider_id=session.PROVIDER,
                account_id=session.ACCOUNT,
                environment=session.ENVIRONMENT,
            )
            economics.append(
                book_external_cash_flow(
                    transaction_id="zero-wire-foreign-plus",
                    cause_event_id="zero-wire-foreign-plus",
                    currency="USD",
                    amount="1",
                )
            )
            economics.append(
                book_external_cash_flow(
                    transaction_id="zero-wire-foreign-minus",
                    cause_event_id="zero-wire-foreign-minus",
                    currency="USD",
                    amount="-1",
                )
            )
            cut = store.current_journal_sequence()

            with self.assertRaisesRegex(
                ValueError, "cut changed after start"
            ):
                run(directory)
            self.assertEqual(
                store.current_journal_sequence(), cut
            )

    def test_reduce_commit_response_loss_recovers_then_continues_once(self):
        with TemporaryDirectory() as reference_dir:
            reference = run(reference_dir)

        with TemporaryDirectory() as directory:
            original_atomic = (
                session.commit_order_fill_with_reservation_consumption
            )

            def crash_after_reduce_commit(*args, **kwargs):
                result = original_atomic(*args, **kwargs)
                if kwargs["committed_at"] == "2026-10-03T00:00:04Z":
                    raise RuntimeError("reduce commit response lost")
                return result

            with patch.object(
                session,
                "commit_order_fill_with_reservation_consumption",
                crash_after_reduce_commit,
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "reduce commit response lost"
                ):
                    run(directory)

            with patch.object(
                SimulatedProvider,
                "transport_send",
                side_effect=AssertionError("recovery cannot resend reduce"),
            ), patch.object(
                session.AuthorityService,
                "admit",
                side_effect=AssertionError(
                    "recovery cannot perform fresh risk admission"
                ),
            ):
                recovered = run(
                    directory,
                    stop_after_episodes=5,
                )

            self.assertEqual(recovered["status"], "PAUSED")
            self.assertEqual(recovered["completed_episodes"], 5)
            self.assertEqual(recovered["position"], "0")
            self.assertEqual(recovered["new_outbound_requests"], 0)
            self.assertEqual(
                recovered["decisions"],
                reference["decisions"][:5],
            )

            completed = run(directory)
            self.assertEqual(completed["status"], "COMPLETED")
            self.assertEqual(
                completed["decisions"], reference["decisions"]
            )
            self.assertEqual(completed["cash"], reference["cash"])
            self.assertEqual(completed["position"], reference["position"])
            self.assertEqual(completed["new_outbound_requests"], 1)

    def test_missing_retained_fill_stays_unknown_without_resend(self):
        original_event = session._loop_event

        def crash_before_observation(
            store, run_id, kind, key, payload, now
        ):
            if kind == "AutonomousEpisodeFillObserved":
                raise RuntimeError("crash-before-observation")
            return original_event(
                store, run_id, kind, key, payload, now
            )

        with TemporaryDirectory() as directory:
            with patch.object(
                session,
                "_loop_event",
                crash_before_observation,
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "crash-before-observation"
                ):
                    run(directory, PRICES[:3])

            store = JournalStore(
                Path(directory) / "journal.sqlite3"
            )
            before = store.current_journal_sequence()

            from mvp.autotrade_mvp.cli import get_status

            unresolved_status = get_status(directory)
            self.assertEqual(
                unresolved_status["recovery_disposition"],
                "RECONCILIATION_REQUIRED",
            )
            self.assertEqual(
                store.current_journal_sequence(), before
            )
            with patch.object(
                SimulatedProvider,
                "transport_send",
                side_effect=AssertionError("no resend"),
            ):
                recovered = run(directory, PRICES[:3])

            self.assertEqual(recovered["status"], "UNKNOWN")
            self.assertEqual(
                recovered["reason"],
                "unfinished_episode_requires_reconciliation",
            )
            self.assertEqual(recovered["new_outbound_requests"], 0)
            self.assertEqual(
                store.current_journal_sequence(), before
            )

    def test_forged_retained_fill_fails_before_recovery_mutation(self):
        original_event = session._loop_event

        def forge(store, run_id, kind, key, payload, now):
            if kind == "AutonomousEpisodeFillObserved":
                payload = {
                    **payload,
                    "fill": {
                        **payload["fill"],
                        "side": "SELL",
                    },
                }
            return original_event(
                store, run_id, kind, key, payload, now
            )

        with TemporaryDirectory() as directory:
            with patch.object(
                session, "_loop_event", forge
            ), patch.object(
                session,
                "commit_order_fill_with_reservation_consumption",
                side_effect=RuntimeError("crash"),
            ):
                with self.assertRaises(RuntimeError):
                    run(directory, PRICES[:3])

            store = JournalStore(
                Path(directory) / "journal.sqlite3"
            )
            cut = store.current_journal_sequence()

            from mvp.autotrade_mvp.cli import get_status

            self.assertEqual(
                get_status(directory)["status"], "corrupt"
            )
            self.assertEqual(
                store.current_journal_sequence(), cut
            )
            with self.assertRaisesRegex(
                ValueError, "simulator history differs"
            ):
                run(directory, PRICES[:3])
            self.assertEqual(
                store.current_journal_sequence(), cut
            )

    def test_offsetting_foreign_economics_fail_before_recovery(self):
        with TemporaryDirectory() as directory:
            with patch.object(
                session,
                "commit_order_fill_with_reservation_consumption",
                side_effect=RuntimeError("crash"),
            ):
                with self.assertRaises(RuntimeError):
                    run(directory, PRICES[:3])

            store = JournalStore(
                Path(directory) / "journal.sqlite3"
            )
            economics = DurableProviderEconomicBook(
                store,
                provider_id=session.PROVIDER,
                account_id=session.ACCOUNT,
                environment=session.ENVIRONMENT,
            )
            for key, value in (
                ("foreign-plus", "1"),
                ("foreign-minus", "-1"),
            ):
                economics.append(
                    book_external_cash_flow(
                        transaction_id=key,
                        cause_event_id=key,
                        currency="USD",
                        amount=value,
                    )
                )
            cut = store.current_journal_sequence()

            with self.assertRaisesRegex(
                ValueError, "canonical economic history"
            ):
                run(directory, PRICES[:3])
            self.assertEqual(
                store.current_journal_sequence(), cut
            )

    def test_writer_race_cannot_commit_recovered_fill_money(self):
        with TemporaryDirectory() as directory:
            with patch.object(
                session,
                "commit_order_fill_with_reservation_consumption",
                side_effect=RuntimeError("crash"),
            ):
                with self.assertRaises(RuntimeError):
                    run(directory, PRICES[:3])

            original_prepare = (
                DurableProviderEconomicBook.prepare_batch_mutation
            )
            injected = False

            def race(book, *args, **kwargs):
                nonlocal injected
                if not injected:
                    injected = True
                    book.append(
                        book_external_cash_flow(
                            transaction_id="concurrent",
                            cause_event_id="concurrent",
                            currency="USD",
                            amount="1",
                        )
                    )
                return original_prepare(book, *args, **kwargs)

            with patch.object(
                DurableProviderEconomicBook,
                "prepare_batch_mutation",
                race,
            ):
                with self.assertRaisesRegex(
                    ValueError, "journal sequence changed"
                ):
                    run(directory, PRICES[:3])

            store = JournalStore(
                Path(directory) / "journal.sqlite3"
            )
            economics = DurableProviderEconomicBook(
                store,
                provider_id=session.PROVIDER,
                account_id=session.ACCOUNT,
                environment=session.ENVIRONMENT,
            )
            self.assertEqual(str(economics.cash("USD")), "1001")
            self.assertEqual(
                str(economics.position(session.INSTRUMENT)), "0"
            )


if __name__ == "__main__":
    unittest.main()
