from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import sqlite3
import unittest

from mvp.autotrade_mvp.model_budget_journal import DurableModelBudget
from mvp.autotrade_mvp.model_gateway import (
    ModelDescriptor,
    ModelRequest,
    RouteStatus,
    RoutingMode,
    RoutingPolicy,
)
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


ROUTE_NOW = datetime(2026, 9, 24, 21, 45, tzinfo=timezone.utc)


def route_request(request_id, *, budget="100"):
    return ModelRequest(
        request_id=request_id,
        allowed_model_ids=("local",),
        privacy_remote_allowed=False,
        budget_remaining=budget,
        deadline_utc=ROUTE_NOW + timedelta(minutes=5),
    )


def route_policy():
    return RoutingPolicy(
        RoutingMode.ALLOWLIST,
        allowed_model_ids=("local",),
        maximum_cost="100",
    )


def route_model_descriptor(*, cost="0.6", revision="r1"):
    return ModelDescriptor(
        model_id="local",
        provider_id="local-provider",
        revision=revision,
        remote=False,
        estimated_cost=cost,
        latency_ms=10,
        quality_score="0.8",
    )


class DurableModelBudgetTests(unittest.TestCase):
    def test_durable_route_ignores_inflated_caller_budget(self):
        with TemporaryDirectory() as directory:
            _, budget = open_budget(directory, ceiling="0")
            decision = budget.admit_route(
                route_policy(),
                route_request("route-zero", budget="100"),
                [route_model_descriptor(cost="0.1")],
                now_utc=ROUTE_NOW,
            )
            self.assertEqual(decision.status, RouteStatus.NO_MODEL)
            self.assertEqual(decision.reserved_cost, Decimal("0"))
            self.assertEqual(budget.snapshot().reserved, Decimal("0"))

    def test_two_routes_cannot_overbook_one_durable_ceiling(self):
        with TemporaryDirectory() as directory:
            _, budget = open_budget(directory, ceiling="1")
            first = budget.admit_route(
                route_policy(),
                route_request("route-a"),
                [route_model_descriptor(cost="0.6")],
                now_utc=ROUTE_NOW,
            )
            second = budget.admit_route(
                route_policy(),
                route_request("route-b"),
                [route_model_descriptor(cost="0.6")],
                now_utc=ROUTE_NOW,
            )
            self.assertEqual(first.status, RouteStatus.ADMITTED)
            self.assertEqual(first.reason, "admitted_durable_budget")
            self.assertEqual(second.status, RouteStatus.NO_MODEL)
            self.assertEqual(budget.snapshot().reserved, Decimal("0.6"))

    def test_durable_route_survives_restart_and_exact_retry_is_idempotent(self):
        with TemporaryDirectory() as directory:
            journal, budget = open_budget(directory, ceiling="1")
            req = route_request("route-restart")
            descriptor = route_model_descriptor(cost="0.6")
            first = budget.admit_route(
                route_policy(),
                req,
                [descriptor],
                now_utc=ROUTE_NOW,
            )
            before = journal.load_events("model_budget", "policy-1")

            _, restarted = open_budget(directory, ceiling="1")
            second = restarted.admit_route(
                route_policy(),
                req,
                [descriptor],
                now_utc=ROUTE_NOW,
            )
            after = restarted.journal.load_events("model_budget", "policy-1")
            self.assertEqual(first, second)
            self.assertEqual(before, after)
            self.assertEqual(restarted.snapshot().reserved, Decimal("0.6"))

    def test_admitted_route_retry_after_deadline_is_rejected_without_releasing_reservation(self):
        with TemporaryDirectory() as directory:
            _, budget = open_budget(directory, ceiling="1")
            req = route_request("route-deadline")
            descriptor = route_model_descriptor(cost="0.6")
            first = budget.admit_route(
                route_policy(),
                req,
                [descriptor],
                now_utc=ROUTE_NOW,
            )
            self.assertEqual(first.status, RouteStatus.ADMITTED)
            self.assertEqual(budget.snapshot().reserved, Decimal("0.6"))

            expired = budget.admit_route(
                route_policy(),
                req,
                [descriptor],
                now_utc=req.deadline_utc,
            )
            self.assertEqual(expired.status, RouteStatus.REJECTED)
            self.assertEqual(expired.reason, "deadline_expired")
            self.assertEqual(expired.reserved_cost, Decimal("0"))
            # The historical reservation remains conservative until an explicit
            # release/settlement path proves the call incurred no cost.
            self.assertEqual(budget.snapshot().reserved, Decimal("0.6"))

    def test_admitted_route_retry_after_cancellation_is_rejected_conservatively(self):
        with TemporaryDirectory() as directory:
            _, budget = open_budget(directory, ceiling="1")
            req = route_request("route-cancel")
            descriptor = route_model_descriptor(cost="0.4")
            first = budget.admit_route(
                route_policy(),
                req,
                [descriptor],
                now_utc=ROUTE_NOW,
            )
            self.assertEqual(first.status, RouteStatus.ADMITTED)

            cancelled = ModelRequest(
                request_id=req.request_id,
                allowed_model_ids=req.allowed_model_ids,
                privacy_remote_allowed=req.privacy_remote_allowed,
                budget_remaining=req.budget_remaining,
                deadline_utc=req.deadline_utc,
                cancelled=True,
            )
            stopped = budget.admit_route(
                route_policy(),
                cancelled,
                [descriptor],
                now_utc=ROUTE_NOW + timedelta(seconds=1),
            )
            self.assertEqual(stopped.status, RouteStatus.REJECTED)
            self.assertEqual(stopped.reason, "request_cancelled")
            self.assertEqual(budget.snapshot().reserved, Decimal("0.4"))

    def test_changed_model_revision_under_route_identity_conflicts(self):
        with TemporaryDirectory() as directory:
            journal, budget = open_budget(directory, ceiling="1")
            req = route_request("route-conflict")
            budget.admit_route(
                route_policy(),
                req,
                [route_model_descriptor(cost="0.4", revision="r1")],
                now_utc=ROUTE_NOW,
            )
            before = journal.load_events("model_budget", "policy-1")
            with self.assertRaisesRegex(
                ValueError,
                "route identity conflicts",
            ):
                budget.admit_route(
                    route_policy(),
                    req,
                    [route_model_descriptor(cost="0.4", revision="r2")],
                    now_utc=ROUTE_NOW,
                )
            self.assertEqual(
                journal.load_events("model_budget", "policy-1"),
                before,
            )
            self.assertEqual(budget.snapshot().reserved, Decimal("0.4"))

    def test_delimiter_characters_cannot_alias_budget_idempotency_identity(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.db"
            journal = JournalStore(path)
            first = DurableModelBudget(
                journal=journal,
                budget_id="scope",
                ceiling="2",
                environment="SIMULATION",
                clock=lambda: NOW,
            )
            second = DurableModelBudget(
                journal=journal,
                budget_id="scope:reserve:req",
                ceiling="2",
                environment="SIMULATION",
                clock=lambda: NOW,
            )
            self.assertTrue(first.reserve("req:release:item", "0.2"))
            self.assertTrue(second.reserve("item", "0.3"))
            # Under the old colon-concatenated identity this RELEASE key was
            # identical to first.reserve("req:release:item").
            self.assertTrue(second.release("item"))

            connection = sqlite3.connect(path)
            try:
                keys = [
                    row[0]
                    for row in connection.execute(
                        "SELECT idempotency_key FROM command_dedupe "
                        "WHERE actor='autotrade-model-budget' ORDER BY idempotency_key"
                    )
                ]
            finally:
                connection.close()
            self.assertEqual(len(keys), 3)
            self.assertEqual(len(set(keys)), 3)

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
                    "WHERE actor = ? AND environment = ?",
                    ("autotrade-model-budget", "PAPER"),
                ).fetchone()
            finally:
                connection.close()
            self.assertEqual(actor, "autotrade-model-budget")
            self.assertEqual(environment, "PAPER")

    def test_budget_aggregate_cannot_change_environment_on_reopen(self):
        with TemporaryDirectory() as directory:
            journal, simulation = open_budget(
                directory,
                environment="SIMULATION",
            )
            self.assertTrue(simulation.reserve("req-scope", "0.4"))
            before = journal.load_events("model_budget", "policy-1")

            with self.assertRaisesRegex(
                ValueError,
                "budget environment conflicts",
            ):
                open_budget(directory, environment="PAPER")

            after = journal.load_events("model_budget", "policy-1")
            self.assertEqual(after, before)
            self.assertEqual(
                before[0]["payload"]["environment"],
                "SIMULATION",
            )

    def test_new_idempotency_identity_cannot_cross_budget_environment(self):
        with TemporaryDirectory() as directory:
            journal, simulation = open_budget(
                directory,
                environment="SIMULATION",
            )
            self.assertTrue(simulation.reserve("req-a", "0.4"))
            before = journal.load_events("model_budget", "policy-1")

            with self.assertRaisesRegex(
                ValueError,
                "budget environment conflicts",
            ):
                DurableModelBudget(
                    journal=journal,
                    budget_id="policy-1",
                    ceiling="1",
                    environment="LIVE",
                    clock=lambda: NOW,
                )

            self.assertEqual(
                journal.load_events("model_budget", "policy-1"),
                before,
            )

    def test_legacy_unscoped_budget_cannot_be_relabelled_by_first_reopen(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.db"
            journal = JournalStore(path)
            payload = {"ceiling": "1"}
            journal.append_event(
                {
                    "event_id": "legacy-model-budget-init",
                    "event_type": "ModelBudgetInitialized",
                    "aggregate_type": "model_budget",
                    "aggregate_id": "legacy-policy",
                    "aggregate_version": "1",
                    "payload": payload,
                    "payload_hash": payload_digest(payload),
                    "committed_at": NOW,
                }
            )
            before = journal.load_events("model_budget", "legacy-policy")

            for environment in ("LIVE", "PAPER", "SIMULATION"):
                with self.subTest(environment=environment):
                    with self.assertRaisesRegex(
                        ValueError,
                        "legacy model budget lacks durable environment binding",
                    ):
                        DurableModelBudget(
                            journal=journal,
                            budget_id="legacy-policy",
                            ceiling="1",
                            environment=environment,
                            clock=lambda: NOW,
                        )

            self.assertEqual(
                journal.load_events("model_budget", "legacy-policy"),
                before,
            )

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
            def _commit(self, *, action, validate, payload, **kwargs):
                if action != "release":
                    return super()._commit(
                        action=action,
                        validate=validate,
                        payload=payload,
                        **kwargs,
                    )
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
                    "aggregate_version": "2",
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
