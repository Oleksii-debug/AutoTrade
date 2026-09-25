from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import sqlite3
import unittest

from mvp.autotrade_mvp.model_budget_journal import DurableModelBudget
from mvp.autotrade_mvp.persistence import JournalStore, payload_digest


NOW = "2026-09-24T21:45:00+00:00"


def open_budget(root, *, ceiling="1", budget_id="policy-1", environment="SIMULATION"):
    journal = JournalStore(Path(root) / "journal.db")
    budget = DurableModelBudget(
        journal=journal,
        budget_id=budget_id,
        ceiling=ceiling,
        environment=environment,
        clock=lambda: NOW,
    )
    return journal, budget


class DurableModelBudgetTests(unittest.TestCase):
    def test_environment_is_required_and_validated_before_mutation(self):
        with TemporaryDirectory() as directory:
            journal = JournalStore(Path(directory) / "journal.db")
            for invalid in ("", "STAGING", None):
                with self.subTest(invalid=invalid):
                    with self.assertRaisesRegex(
                        ValueError,
                        "environment must be REPLAY, SIMULATION, PAPER, or LIVE",
                    ):
                        DurableModelBudget(
                            journal=journal,
                            budget_id="policy-1",
                            ceiling="1",
                            environment=invalid,
                            clock=lambda: NOW,
                        )
            self.assertEqual(
                journal.load_events("model_budget", "policy-1"),
                [],
            )

    def test_command_scope_uses_stable_actor_and_caller_environment(self):
        with TemporaryDirectory() as directory:
            journal, budget = open_budget(directory, environment="PAPER")
            self.assertTrue(budget.reserve("req-paper", "0.2"))
            connection = sqlite3.connect(journal.path)
            try:
                actor, environment = connection.execute(
                    "SELECT actor, environment FROM command_dedupe "
                    "WHERE idempotency_key LIKE ?",
                    ("%:reserve:req-paper",),
                ).fetchone()
            finally:
                connection.close()
            self.assertEqual(actor, "autotrade-model-budget")
            self.assertEqual(environment, "PAPER")

    def test_exact_retry_cannot_change_command_environment(self):
        with TemporaryDirectory() as directory:
            _, simulation = open_budget(directory, environment="SIMULATION")
            self.assertTrue(simulation.reserve("req-scope", "0.4"))
            _, paper = open_budget(directory, environment="PAPER")
            with self.assertRaisesRegex(
                ValueError,
                "command_id already exists",
            ):
                paper.reserve("req-scope", "0.4")
            self.assertEqual(paper.snapshot().reserved, Decimal("0.4"))

    def test_changed_economics_cannot_escape_through_different_environment(self):
        with TemporaryDirectory() as directory:
            _, first = open_budget(directory, environment="SIMULATION")
            self.assertTrue(first.reserve("req-1", "0.4"))
            _, paper = open_budget(directory, environment="PAPER")
            with self.assertRaisesRegex(ValueError, "idempotency identity conflicts"):
                paper.reserve("req-1", "0.5")
            self.assertEqual(paper.snapshot().reserved, Decimal("0.4"))

    def test_reservation_survives_restart(self):
        with TemporaryDirectory() as directory:
            _, first = open_budget(directory)
            self.assertTrue(first.reserve("req-1", "0.4"))
            self.assertEqual(first.snapshot().reserved, Decimal("0.4"))

            _, restarted = open_budget(directory)
            self.assertEqual(restarted.snapshot().reserved, Decimal("0.4"))
            self.assertEqual(restarted.snapshot().available, Decimal("0.6"))

    def test_duplicate_reserve_after_restart_is_idempotent_but_conflict_fails(self):
        with TemporaryDirectory() as directory:
            _, first = open_budget(directory)
            self.assertTrue(first.reserve("req-1", "0.4"))

            _, restarted = open_budget(directory)
            self.assertFalse(restarted.reserve("req-1", "0.4"))
            self.assertEqual(restarted.snapshot().reserved, Decimal("0.4"))
            with self.assertRaisesRegex(ValueError, "idempotency identity conflicts"):
                restarted.reserve("req-1", "0.5")
            self.assertEqual(restarted.snapshot().reserved, Decimal("0.4"))

    def test_settlement_retry_after_restart_is_noop_and_conflict_fails(self):
        with TemporaryDirectory() as directory:
            _, first = open_budget(directory)
            first.reserve("req-1", "0.5")
            self.assertTrue(
                first.settle(
                    "req-1",
                    incurred="0.2",
                    estimated_unbilled="0.2",
                )
            )
            expected = first.snapshot()

            _, restarted = open_budget(directory)
            self.assertFalse(
                restarted.settle(
                    "req-1",
                    incurred="0.2",
                    estimated_unbilled="0.2",
                )
            )
            self.assertEqual(restarted.snapshot(), expected)
            with self.assertRaisesRegex(ValueError, "idempotency identity conflicts"):
                restarted.settle(
                    "req-1",
                    incurred="0.3",
                    estimated_unbilled="0.1",
                )
            self.assertEqual(restarted.snapshot(), expected)

    def test_billing_retry_after_restart_is_noop_and_conflict_fails(self):
        with TemporaryDirectory() as directory:
            _, first = open_budget(directory)
            first.reserve("req-1", "0.5")
            first.settle(
                "req-1",
                incurred="0.1",
                estimated_unbilled="0.3",
            )
            self.assertTrue(
                first.reconcile_unbilled(
                    billing_id="invoice-1",
                    request_id="req-1",
                    billed="0.2",
                )
            )
            expected = first.snapshot()

            _, restarted = open_budget(directory)
            self.assertFalse(
                restarted.reconcile_unbilled(
                    billing_id="invoice-1",
                    request_id="req-1",
                    billed="0.2",
                )
            )
            self.assertEqual(restarted.snapshot(), expected)
            with self.assertRaisesRegex(ValueError, "idempotency identity conflicts"):
                restarted.reconcile_unbilled(
                    billing_id="invoice-1",
                    request_id="req-1",
                    billed="0.1",
                )
            self.assertEqual(restarted.snapshot(), expected)

    def test_billing_identity_cannot_move_between_requests_after_restart(self):
        with TemporaryDirectory() as directory:
            _, first = open_budget(directory, ceiling="2")
            first.reserve("req-1", "0.5")
            first.reserve("req-2", "0.5")
            first.settle("req-1", incurred="0.1", estimated_unbilled="0.2")
            first.settle("req-2", incurred="0.1", estimated_unbilled="0.2")
            self.assertTrue(
                first.reconcile_unbilled(
                    billing_id="invoice-shared",
                    request_id="req-1",
                    billed="0.1",
                )
            )

            _, restarted = open_budget(directory, ceiling="2")
            with self.assertRaisesRegex(
                ValueError,
                "idempotency identity conflicts",
            ):
                restarted.reconcile_unbilled(
                    billing_id="invoice-shared",
                    request_id="req-2",
                    billed="0.1",
                )

    def test_release_revalidates_exact_amount_before_commit(self):
        class InterleavingBudget(DurableModelBudget):
            def _commit(self, *, validate, payload, **kwargs):
                candidate = self._replay()
                candidate.release(payload["request_id"])
                validate(candidate)
                return True

        with TemporaryDirectory() as directory:
            journal = JournalStore(Path(directory) / "journal.db")
            budget = InterleavingBudget(
                journal=journal,
                budget_id="policy-1",
                ceiling="1",
                environment="SIMULATION",
                clock=lambda: NOW,
            )
            budget.reserve("req-1", "0.4")
            before = tuple(journal.load_events("model_budget", "policy-1"))
            with self.assertRaisesRegex(
                ValueError,
                "release amount changed before commit",
            ):
                budget.release("req-1")
            self.assertEqual(
                tuple(journal.load_events("model_budget", "policy-1")),
                before,
            )

    def test_release_is_durable_across_restart(self):
        with TemporaryDirectory() as directory:
            _, first = open_budget(directory)
            first.reserve("req-1", "0.4")
            self.assertTrue(first.release("req-1"))
            self.assertEqual(first.snapshot().reserved, Decimal("0"))

            _, restarted = open_budget(directory)
            self.assertEqual(restarted.snapshot().reserved, Decimal("0"))
            self.assertFalse(restarted.release("req-1"))

    def test_ceiling_conflict_after_restart_fails_closed(self):
        with TemporaryDirectory() as directory:
            open_budget(directory, ceiling="1")
            with self.assertRaisesRegex(ValueError, "ceiling conflicts"):
                open_budget(directory, ceiling="2")

    def test_over_budget_or_float_reservation_does_not_append_event(self):
        with TemporaryDirectory() as directory:
            journal, budget = open_budget(directory, ceiling="0.1")
            initial = journal.load_events("model_budget", "policy-1")
            self.assertEqual(len(initial), 1)

            with self.assertRaisesRegex(ValueError, "budget exhausted"):
                budget.reserve("too-large", "0.2")
            self.assertEqual(
                len(journal.load_events("model_budget", "policy-1")),
                1,
            )

            with self.assertRaisesRegex(ValueError, "exact decimal"):
                budget.reserve("float", 0.01)
            self.assertEqual(
                len(journal.load_events("model_budget", "policy-1")),
                1,
            )

    def test_invalid_settlement_leaves_reservation_durable(self):
        with TemporaryDirectory() as directory:
            journal, budget = open_budget(directory)
            budget.reserve("req-1", "0.5")
            before = tuple(journal.load_events("model_budget", "policy-1"))

            with self.assertRaisesRegex(ValueError, "exceeds reserved"):
                budget.settle(
                    "req-1",
                    incurred="0.4",
                    estimated_unbilled="0.2",
                )
            self.assertEqual(
                tuple(journal.load_events("model_budget", "policy-1")),
                before,
            )

            _, restarted = open_budget(directory)
            self.assertEqual(restarted.snapshot().reserved, Decimal("0.5"))
            self.assertEqual(restarted.snapshot().incurred, Decimal("0"))

    def test_unknown_durable_event_type_fails_closed_on_replay(self):
        with TemporaryDirectory() as directory:
            journal, budget = open_budget(directory)
            payload = {}
            journal.append_event(
                {
                    "event_id": "alien-budget-event",
                    "event_type": "AlienBudgetMutation",
                    "aggregate_type": "model_budget",
                    "aggregate_id": "policy-1",
                    "aggregate_version": 2,
                    "payload": payload,
                    "payload_hash": payload_digest(payload),
                    "committed_at": NOW,
                }
            )
            with self.assertRaisesRegex(
                ValueError,
                "unsupported durable model budget event",
            ):
                budget.snapshot()

    def test_two_budget_aggregates_do_not_share_cost_state(self):
        with TemporaryDirectory() as directory:
            _, first = open_budget(directory, budget_id="policy-a")
            _, second = open_budget(directory, budget_id="policy-b")
            first.reserve("req-1", "0.7")
            second.reserve("req-1", "0.2")
            self.assertEqual(first.snapshot().reserved, Decimal("0.7"))
            self.assertEqual(second.snapshot().reserved, Decimal("0.2"))


if __name__ == "__main__":
    unittest.main()
