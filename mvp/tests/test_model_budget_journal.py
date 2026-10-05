from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext, ROUND_DOWN, Inexact, Rounded
from pathlib import Path
from tempfile import TemporaryDirectory
import sqlite3
import threading
import unittest
from unittest.mock import patch

import mvp.autotrade_mvp.model_budget_journal as model_budget_module
from mvp.autotrade_mvp.model_budget_journal import DurableModelBudget
from mvp.autotrade_mvp.model_gateway import (
    ModelDescriptor,
    ModelRequest,
    RouteStatus,
    RoutingMode,
    RoutingPolicy,
)
from mvp.autotrade_mvp.persistence import JournalStore, canonical_json, payload_digest


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


def route_request(request_id, *, budget="100", cancelled=False):
    return ModelRequest(
        request_id=request_id,
        allowed_model_ids=("local",),
        privacy_remote_allowed=False,
        budget_remaining=budget,
        deadline_utc=ROUTE_NOW + timedelta(minutes=5),
        cancelled=cancelled,
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


class RecordingJournalStore(JournalStore):
    def __init__(self, path):
        self.last_appended_envelope = None
        super().__init__(path)

    def append_event(self, envelope, *, outbox_topic=None):
        self.last_appended_envelope = dict(envelope)
        return super().append_event(envelope, outbox_topic=outbox_topic)


class RejectingInitializationJournal(JournalStore):
    def append_event(self, envelope, *, outbox_topic=None):
        raise ValueError("synthetic malformed initialization")


class DurableModelBudgetTests(unittest.TestCase):
    def test_clock_object_truthiness_is_never_consulted(self):
        class Clock:
            truth_calls = 0
            call_count = 0

            def __bool__(self):
                type(self).truth_calls += 1
                raise AssertionError("clock truthiness executed")

            def __call__(self):
                type(self).call_count += 1
                return NOW

        with TemporaryDirectory() as directory:
            journal = JournalStore(Path(directory) / "journal.db")
            clock = Clock()
            budget = DurableModelBudget(
                journal=journal,
                budget_id="policy-clock",
                ceiling="1",
                environment="SIMULATION",
                clock=clock,
            )
            self.assertEqual(Clock.truth_calls, 0)
            self.assertGreaterEqual(Clock.call_count, 1)
            self.assertEqual(budget.snapshot().ceiling, Decimal("1"))

    def test_clock_cannot_redirect_clock_text_normalizer(self):
        with TemporaryDirectory() as directory:
            journal = JournalStore(Path(directory) / "journal.db")
            canonical_clock_text = model_budget_module._clock_text
            forged_calls = []

            def hostile_clock():
                model_budget_module._clock_text = (
                    lambda _value: forged_calls.append("forged")
                    or "2099-01-01T00:00:00+00:00"
                )
                return NOW

            try:
                with self.assertRaisesRegex(
                    ValueError,
                    r"model budget clock mutated authority:.*module\._clock_text",
                ):
                    DurableModelBudget(
                        journal=journal,
                        budget_id="policy-clock-text-alias",
                        ceiling="1",
                        environment="SIMULATION",
                        clock=hostile_clock,
                    )
            finally:
                model_budget_module._clock_text = canonical_clock_text

            self.assertEqual(forged_calls, [])
            self.assertIs(
                model_budget_module._clock_text,
                canonical_clock_text,
            )
            self.assertEqual(
                journal.load_events(
                    "model_budget",
                    "policy-clock-text-alias",
                ),
                [],
            )

    def test_clock_cannot_redirect_cleanup_through_module_budget_alias(self):
        class DecoyBudget:
            restore_calls = 0

            @staticmethod
            def _restore_clock_authority(*_args, **_kwargs):
                DecoyBudget.restore_calls += 1
                raise AssertionError("clock redirected budget cleanup")

        with TemporaryDirectory() as directory:
            journal = JournalStore(Path(directory) / "journal.db")
            canonical_budget = model_budget_module.DurableModelBudget

            def hostile_clock():
                model_budget_module.DurableModelBudget = DecoyBudget
                return NOW

            try:
                with self.assertRaisesRegex(
                    ValueError,
                    r"model budget clock mutated authority:.*module\.DurableModelBudget",
                ):
                    canonical_budget(
                        journal=journal,
                        budget_id="policy-module-alias-clock",
                        ceiling="1",
                        environment="SIMULATION",
                        clock=hostile_clock,
                    )
            finally:
                model_budget_module.DurableModelBudget = canonical_budget

            self.assertEqual(DecoyBudget.restore_calls, 0)
            self.assertIs(
                model_budget_module.DurableModelBudget,
                canonical_budget,
            )
            self.assertEqual(
                journal.load_events(
                    "model_budget",
                    "policy-module-alias-clock",
                ),
                [],
            )

    def test_initialization_clock_cannot_shadow_journal_append(self):
        with TemporaryDirectory() as directory:
            journal = JournalStore(Path(directory) / "journal.db")
            forged_calls = []

            def hostile_clock():
                journal.append_event = (
                    lambda *_args, **_kwargs: forged_calls.append("forged")
                )
                return NOW

            with self.assertRaisesRegex(
                ValueError,
                r"model budget clock mutated authority:.*journal\.append_event",
            ):
                DurableModelBudget(
                    journal=journal,
                    budget_id="policy-hostile-init-clock",
                    ceiling="1",
                    environment="SIMULATION",
                    clock=hostile_clock,
                )

            self.assertEqual(forged_calls, [])
            self.assertNotIn("append_event", journal.__dict__)
            self.assertEqual(
                journal.load_events(
                    "model_budget",
                    "policy-hostile-init-clock",
                ),
                [],
            )

    def test_commit_clock_cannot_redirect_budget_or_journal_authority(self):
        with TemporaryDirectory() as directory:
            journal = JournalStore(Path(directory) / "journal.db")
            forged_calls = []
            armed = False
            budget = None

            def hostile_clock():
                if armed:
                    budget.environment = "LIVE"
                    budget.journal.commit_command = (
                        lambda *_args, **_kwargs: forged_calls.append("forged")
                    )
                return NOW

            budget = DurableModelBudget(
                journal=journal,
                budget_id="policy-1",
                ceiling="1",
                environment="SIMULATION",
                clock=hostile_clock,
            )
            before = journal.load_events("model_budget", "policy-1")
            armed = True
            with self.assertRaisesRegex(
                ValueError,
                "model budget clock mutated authority:",
            ):
                budget.reserve("req-hostile-clock", "0.2")

            self.assertEqual(forged_calls, [])
            self.assertEqual(budget.environment, "SIMULATION")
            self.assertNotIn("commit_command", journal.__dict__)
            self.assertEqual(
                journal.load_events("model_budget", "policy-1"),
                before,
            )
            self.assertEqual(budget.snapshot().reserved, Decimal("0"))

    def test_non_callable_clock_fails_before_journal_mutation(self):
        with TemporaryDirectory() as directory:
            journal = JournalStore(Path(directory) / "journal.db")
            with self.assertRaisesRegex(TypeError, "clock must be callable"):
                DurableModelBudget(
                    journal=journal,
                    budget_id="policy-bad-clock",
                    ceiling="1",
                    environment="SIMULATION",
                    clock=object(),
                )
            self.assertEqual(
                journal.load_events("model_budget", "policy-bad-clock"),
                [],
            )

    def test_hostile_clock_result_is_rejected_before_text_method_or_journal_mutation(self):
        class HostileText(str):
            strip_calls = 0

            def strip(self, *args, **kwargs):
                type(self).strip_calls += 1
                raise AssertionError("hostile clock result strip executed")

        with TemporaryDirectory() as directory:
            journal = JournalStore(Path(directory) / "journal.db")
            with self.assertRaisesRegex(ValueError, "canonical UTC text"):
                DurableModelBudget(
                    journal=journal,
                    budget_id="policy-hostile-clock-result",
                    ceiling="1",
                    environment="SIMULATION",
                    clock=lambda: HostileText(NOW),
                )
            self.assertEqual(HostileText.strip_calls, 0)
            self.assertEqual(
                journal.load_events("model_budget", "policy-hostile-clock-result"),
                [],
            )

    def test_noncanonical_clock_text_fails_before_journal_mutation(self):
        invalid_values = (
            "not-a-time",
            "2026-09-24T21:45:00",
            "2026-09-24T23:45:00+02:00",
            "2026-09-24T21:45:00Z",
        )
        for invalid in invalid_values:
            with self.subTest(invalid=invalid), TemporaryDirectory() as directory:
                journal = JournalStore(Path(directory) / "journal.db")
                budget_id = "policy-invalid-clock"
                with self.assertRaisesRegex(ValueError, "canonical UTC text"):
                    DurableModelBudget(
                        journal=journal,
                        budget_id=budget_id,
                        ceiling="1",
                        environment="SIMULATION",
                        clock=lambda value=invalid: value,
                    )
                self.assertEqual(
                    journal.load_events("model_budget", budget_id),
                    [],
                )

    def test_initialization_uses_canonical_sequence_text(self):
        with TemporaryDirectory() as directory:
            journal = RecordingJournalStore(Path(directory) / "journal.db")
            budget = DurableModelBudget(
                journal=journal,
                budget_id="policy-sequence",
                ceiling="1",
                environment="SIMULATION",
                clock=lambda: NOW,
            )
            self.assertEqual(
                journal.last_appended_envelope["aggregate_version"],
                "1",
            )
            self.assertEqual(budget.snapshot().ceiling, Decimal("1"))

    def test_initialization_contract_error_is_not_swallowed_as_race(self):
        with TemporaryDirectory() as directory:
            journal = RejectingInitializationJournal(Path(directory) / "journal.db")
            with self.assertRaisesRegex(
                ValueError,
                "synthetic malformed initialization",
            ):
                DurableModelBudget(
                    journal=journal,
                    budget_id="policy-reject",
                    ceiling="1",
                    environment="SIMULATION",
                    clock=lambda: NOW,
                )


    def test_budget_id_rejects_hostile_text_before_strip(self):
        class HostileText(str):
            strip_calls = 0

            def strip(self, *args, **kwargs):
                type(self).strip_calls += 1
                raise AssertionError("hostile strip executed")

        with TemporaryDirectory() as directory:
            journal = JournalStore(Path(directory) / "journal.db")
            hostile = HostileText("policy-hostile")
            with self.assertRaisesRegex(ValueError, "budget_id is required"):
                DurableModelBudget(
                    journal=journal,
                    budget_id=hostile,
                    ceiling="1",
                    environment="SIMULATION",
                    clock=lambda: NOW,
                )
            self.assertEqual(HostileText.strip_calls, 0)

    def test_environment_rejects_hostile_text_before_strip(self):
        class HostileText(str):
            strip_calls = 0

            def strip(self, *args, **kwargs):
                type(self).strip_calls += 1
                raise AssertionError("hostile strip executed")

        with TemporaryDirectory() as directory:
            journal = JournalStore(Path(directory) / "journal.db")
            hostile = HostileText("SIMULATION")
            with self.assertRaisesRegex(ValueError, "environment must be"):
                DurableModelBudget(
                    journal=journal,
                    budget_id="policy-hostile-environment",
                    ceiling="1",
                    environment=hostile,
                    clock=lambda: NOW,
                )
            self.assertEqual(HostileText.strip_calls, 0)

    def test_route_reservation_context_rejects_dict_subclass_before_iteration(self):
        class HostileDict(dict):
            items_calls = 0

            def items(self):
                type(self).items_calls += 1
                raise AssertionError("hostile items executed")

        with TemporaryDirectory() as directory:
            _, budget = open_budget(directory, ceiling="1")
            hostile = HostileDict({"attempt_id": "attempt-1"})
            with self.assertRaisesRegex(TypeError, "exact dict"):
                budget.admit_route(
                    route_policy(),
                    route_request("route-hostile-context"),
                    [route_model_descriptor(cost="0.1")],
                    now_utc=ROUTE_NOW,
                    reservation_context=hostile,
                )
            self.assertEqual(HostileDict.items_calls, 0)
            self.assertEqual(budget.snapshot().reserved, Decimal("0"))

    def test_route_reservation_context_rejects_hostile_key_before_strip(self):
        class HostileText(str):
            strip_calls = 0

            def strip(self, *args, **kwargs):
                type(self).strip_calls += 1
                raise AssertionError("hostile strip executed")

        with TemporaryDirectory() as directory:
            _, budget = open_budget(directory, ceiling="1")
            hostile_key = HostileText("attempt_id")
            with self.assertRaisesRegex(TypeError, "keys must be text"):
                budget.admit_route(
                    route_policy(),
                    route_request("route-hostile-key"),
                    [route_model_descriptor(cost="0.1")],
                    now_utc=ROUTE_NOW,
                    reservation_context={hostile_key: "attempt-1"},
                )
            self.assertEqual(HostileText.strip_calls, 0)
            self.assertEqual(budget.snapshot().reserved, Decimal("0"))

    def test_route_reservation_context_rejects_hostile_value_before_strip(self):
        class HostileText(str):
            strip_calls = 0

            def strip(self, *args, **kwargs):
                type(self).strip_calls += 1
                raise AssertionError("hostile strip executed")

        with TemporaryDirectory() as directory:
            _, budget = open_budget(directory, ceiling="1")
            hostile_value = HostileText("attempt-1")
            with self.assertRaisesRegex(TypeError, "values must be text"):
                budget.admit_route(
                    route_policy(),
                    route_request("route-hostile-value"),
                    [route_model_descriptor(cost="0.1")],
                    now_utc=ROUTE_NOW,
                    reservation_context={"attempt_id": hostile_value},
                )
            self.assertEqual(HostileText.strip_calls, 0)
            self.assertEqual(budget.snapshot().reserved, Decimal("0"))

    def test_route_rejects_policy_subclass_before_financial_identity_read(self):
        class DerivedRoutingPolicy(RoutingPolicy):
            pass

        with TemporaryDirectory() as directory:
            _, budget = open_budget(directory, ceiling="1")
            base = route_policy()
            derived = DerivedRoutingPolicy(
                base.mode,
                allowed_model_ids=base.allowed_model_ids,
                fixed_model_id=base.fixed_model_id,
                allow_remote=base.allow_remote,
                maximum_cost=base.maximum_cost,
                maximum_latency_ms=base.maximum_latency_ms,
            )
            with self.assertRaisesRegex(TypeError, "exact RoutingPolicy"):
                budget.admit_route(
                    derived,
                    route_request("route-derived-policy"),
                    [route_model_descriptor(cost="0.1")],
                    now_utc=ROUTE_NOW,
                )
            self.assertEqual(budget.snapshot().reserved, Decimal("0"))

    def test_route_rejects_request_subclass_before_financial_identity_read(self):
        class DerivedModelRequest(ModelRequest):
            pass

        with TemporaryDirectory() as directory:
            _, budget = open_budget(directory, ceiling="1")
            base = route_request("route-derived-request")
            derived = DerivedModelRequest(
                request_id=base.request_id,
                allowed_model_ids=base.allowed_model_ids,
                privacy_remote_allowed=base.privacy_remote_allowed,
                budget_remaining=base.budget_remaining,
                deadline_utc=base.deadline_utc,
                cancelled=base.cancelled,
            )
            with self.assertRaisesRegex(TypeError, "exact ModelRequest"):
                budget.admit_route(
                    route_policy(),
                    derived,
                    [route_model_descriptor(cost="0.1")],
                    now_utc=ROUTE_NOW,
                )
            self.assertEqual(budget.snapshot().reserved, Decimal("0"))

    def test_inventory_free_route_outcomes_do_not_enumerate_descriptors(self):
        class ExplodingInventory:
            def __iter__(self):
                raise AssertionError("descriptor inventory must not be touched")

        cases = (
            (
                RoutingPolicy(RoutingMode.ZERO, maximum_cost="0"),
                route_request("route-zero-no-inventory"),
                RouteStatus.NO_MODEL,
                "zero_model_policy",
            ),
            (
                route_policy(),
                route_request("route-cancel-no-inventory", cancelled=True),
                RouteStatus.REJECTED,
                "request_cancelled",
            ),
        )
        for policy, request, expected_status, expected_reason in cases:
            with self.subTest(expected_reason=expected_reason), TemporaryDirectory() as directory:
                journal, budget = open_budget(directory, ceiling="1")
                decision = budget.admit_route(
                    policy,
                    request,
                    ExplodingInventory(),
                    now_utc=ROUTE_NOW,
                    reservation_context={"attempt_id": "unused"},
                )
                self.assertEqual(decision.status, expected_status)
                self.assertEqual(decision.reason, expected_reason)
                self.assertEqual(budget.snapshot().reserved, Decimal("0"))
                self.assertEqual(
                    [
                        event["event_type"]
                        for event in journal.load_events("model_budget", "policy-1")
                        if event["event_type"] == "ModelRouteReserved"
                    ],
                    [],
                )

    def test_route_rejects_descriptor_subclass_before_financial_identity_read(self):
        class DerivedModelDescriptor(ModelDescriptor):
            pass

        with TemporaryDirectory() as directory:
            _, budget = open_budget(directory, ceiling="1")
            base = route_model_descriptor(cost="0.1")
            derived = DerivedModelDescriptor(
                model_id=base.model_id,
                provider_id=base.provider_id,
                revision=base.revision,
                remote=base.remote,
                estimated_cost=base.estimated_cost,
                latency_ms=base.latency_ms,
                quality_score=base.quality_score,
            )
            with self.assertRaisesRegex(TypeError, "exact ModelDescriptor"):
                budget.admit_route(
                    route_policy(),
                    route_request("route-derived-descriptor"),
                    [derived],
                    now_utc=ROUTE_NOW,
                )
            self.assertEqual(budget.snapshot().reserved, Decimal("0"))

    def test_route_rejects_datetime_subclass_before_truth_or_comparison(self):
        class HostileDatetime(datetime):
            truth_calls = 0
            compare_calls = 0

            def __bool__(self):
                type(self).truth_calls += 1
                raise AssertionError("hostile datetime truthiness executed")

            def __ge__(self, other):
                type(self).compare_calls += 1
                raise AssertionError("hostile datetime comparison executed")

        hostile = HostileDatetime(
            2026,
            9,
            24,
            21,
            45,
            tzinfo=timezone.utc,
        )
        with TemporaryDirectory() as directory:
            _, budget = open_budget(directory, ceiling="1")
            with self.assertRaisesRegex(ValueError, "exact timezone-aware datetime"):
                budget.admit_route(
                    route_policy(),
                    route_request("route-hostile-now"),
                    [route_model_descriptor(cost="0.1")],
                    now_utc=hostile,
                )
            self.assertEqual(HostileDatetime.truth_calls, 0)
            self.assertEqual(HostileDatetime.compare_calls, 0)
            self.assertEqual(budget.snapshot().reserved, Decimal("0"))

    def test_route_detaches_financial_identity_before_journal_callbacks(self):
        with TemporaryDirectory() as directory:
            journal, budget = open_budget(directory, ceiling="1")
            policy = route_policy()
            request = route_request("route-detached-identity", budget="1")
            descriptor = route_model_descriptor(cost="0.1")
            canonical_snapshot = budget.snapshot
            mutated = False

            def mutating_snapshot():
                nonlocal mutated
                if not mutated:
                    mutated = True
                    object.__setattr__(policy, "allowed_model_ids", ())
                    object.__setattr__(request, "budget_remaining", Decimal("0"))
                    object.__setattr__(descriptor, "estimated_cost", Decimal("99"))
                return canonical_snapshot()

            with patch.object(budget, "snapshot", side_effect=mutating_snapshot):
                decision = budget.admit_route(
                    policy,
                    request,
                    [descriptor],
                    now_utc=ROUTE_NOW,
                    reservation_context={"attempt_id": "attempt-detached"},
                )

            self.assertTrue(mutated)
            self.assertEqual(decision.status, RouteStatus.ADMITTED)
            self.assertEqual(decision.reserved_cost, Decimal("0.1"))
            event = journal.load_events(
                "model_budget",
                "policy-1",
            )[-1]
            self.assertEqual(event["event_type"], "ModelRouteReserved")
            routing_input = event["payload"]["routing_input"]
            self.assertEqual(routing_input["policy"]["allowed_model_ids"], ["local"])
            self.assertEqual(routing_input["request"]["budget_cap"], "1")
            self.assertEqual(
                routing_input["descriptors"][0]["estimated_cost"],
                "0.1",
            )

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

    def test_v9_event_batch_retries_preserve_journal_sequence_after_restart(self):
        with TemporaryDirectory() as directory:
            journal, budget = open_budget(directory, ceiling="2")

            self.assertTrue(budget.reserve("req-reserve", "0.2"))
            before = journal.current_journal_sequence()
            _, restarted = open_budget(directory, ceiling="2")
            self.assertFalse(restarted.reserve("req-reserve", "0.2"))
            self.assertEqual(
                restarted.journal.current_journal_sequence(),
                before,
            )

            self.assertTrue(restarted.reserve("req-release", "0.2"))
            self.assertTrue(restarted.release("req-release"))
            before = restarted.journal.current_journal_sequence()
            _, restarted = open_budget(directory, ceiling="2")
            self.assertFalse(restarted.release("req-release"))
            self.assertEqual(
                restarted.journal.current_journal_sequence(),
                before,
            )

            self.assertTrue(restarted.reserve("req-settle", "0.2"))
            self.assertTrue(
                restarted.settle(
                    "req-settle",
                    incurred="0.1",
                    estimated_unbilled="0.05",
                )
            )
            before = restarted.journal.current_journal_sequence()
            _, restarted = open_budget(directory, ceiling="2")
            self.assertFalse(
                restarted.settle(
                    "req-settle",
                    incurred="0.1",
                    estimated_unbilled="0.05",
                )
            )
            self.assertEqual(
                restarted.journal.current_journal_sequence(),
                before,
            )

            self.assertTrue(restarted.reserve("req-billing", "0.2"))
            self.assertTrue(
                restarted.settle(
                    "req-billing",
                    incurred="0.05",
                    estimated_unbilled="0.1",
                )
            )
            self.assertTrue(
                restarted.reconcile_unbilled(
                    billing_id="invoice-v9",
                    request_id="req-billing",
                    billed="0.05",
                )
            )
            before = restarted.journal.current_journal_sequence()
            _, restarted = open_budget(directory, ceiling="2")
            self.assertFalse(
                restarted.reconcile_unbilled(
                    billing_id="invoice-v9",
                    request_id="req-billing",
                    billed="0.05",
                )
            )
            self.assertEqual(
                restarted.journal.current_journal_sequence(),
                before,
            )

    def test_v9_retry_rejects_resealed_result_inconsistent_with_durable_event(self):
        with TemporaryDirectory() as directory:
            journal, budget = open_budget(directory)
            self.assertTrue(budget.reserve("req-1", "0.4"))
            forged = {
                "reserved": False,
                "request_id": "req-1",
                "amount": "0.4",
            }
            connection = sqlite3.connect(journal.path)
            try:
                connection.execute(
                    """
                    UPDATE command_dedupe
                    SET result_json = ?, result_hash = ?
                    """,
                    (
                        canonical_json(forged),
                        payload_digest(forged),
                    ),
                )
                connection.commit()
            finally:
                connection.close()

            _, restarted = open_budget(directory)
            with self.assertRaisesRegex(
                ValueError,
                "result conflicts with durable event",
            ):
                restarted.reserve("req-1", "0.4")

    def test_fresh_reserve_concurrent_exact_winner_is_idempotent_without_growth(self):
        with TemporaryDirectory() as directory:
            journal, budget = open_budget(directory)
            original_commit = budget.journal.commit_command
            before = journal.current_journal_sequence()
            raced = False

            def race_then_commit(*args, **kwargs):
                nonlocal raced
                if not raced:
                    raced = True
                    winner = JournalStore(journal.path)
                    saved, inserted, _ = winner.commit_command(
                        *args,
                        **kwargs,
                    )
                    self.assertTrue(inserted)
                    self.assertEqual(saved, kwargs["result"])
                return original_commit(*args, **kwargs)

            with patch.object(
                budget.journal,
                "commit_command",
                side_effect=race_then_commit,
            ):
                self.assertFalse(budget.reserve("req-race", "0.4"))

            self.assertEqual(
                journal.current_journal_sequence(),
                before + 1,
            )
            self.assertEqual(
                len(journal.load_events("model_budget", "policy-1")),
                2,
            )
            self.assertEqual(budget.snapshot().reserved, Decimal("0.4"))

    def test_fresh_reserve_rejects_concurrent_domain_inconsistent_saved_result(self):
        with TemporaryDirectory() as directory:
            journal, budget = open_budget(directory)
            original_commit = budget.journal.commit_command
            before = journal.current_journal_sequence()
            raced = False

            def race_then_commit(*args, **kwargs):
                nonlocal raced
                if not raced:
                    raced = True
                    forged_kwargs = dict(kwargs)
                    forged_result = dict(kwargs["result"])
                    forged_result["reserved"] = False
                    forged_kwargs["result"] = forged_result
                    winner = JournalStore(journal.path)
                    _saved, inserted, _ = winner.commit_command(
                        *args,
                        **forged_kwargs,
                    )
                    self.assertTrue(inserted)
                return original_commit(*args, **kwargs)

            with patch.object(
                budget.journal,
                "commit_command",
                side_effect=race_then_commit,
            ):
                with self.assertRaisesRegex(
                    ValueError,
                    "result conflicts with durable event",
                ):
                    budget.reserve("req-race-forged", "0.4")

            self.assertEqual(
                journal.current_journal_sequence(),
                before + 1,
            )
            self.assertEqual(
                len(journal.load_events("model_budget", "policy-1")),
                2,
            )
            self.assertEqual(budget.snapshot().reserved, Decimal("0.4"))

    def test_v9_retry_does_not_lazily_promote_orphan_event(self):
        with TemporaryDirectory() as directory:
            journal, budget = open_budget(directory)
            self.assertTrue(budget.reserve("req-1", "0.4"))
            connection = sqlite3.connect(journal.path)
            try:
                connection.execute("DELETE FROM command_dedupe")
                connection.commit()
            finally:
                connection.close()

            _, restarted = open_budget(directory)
            before = restarted.journal.current_journal_sequence()
            with self.assertRaisesRegex(
                ValueError,
                "event_id already exists",
            ):
                restarted.reserve("req-1", "0.4")
            self.assertEqual(
                restarted.journal.current_journal_sequence(),
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
                # Simulate a concurrent release only at the release commit
                # boundary, after the durable setup reservation exists.
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


class ExactDurableModelBudgetTests(unittest.TestCase):
    def test_restart_rebuilds_exact_costs_with_hostile_decimal_context(self):
        with TemporaryDirectory() as directory:
            journal, budget = open_budget(directory, ceiling="1.11")
            with localcontext() as ctx:
                ctx.prec = 1
                ctx.rounding = ROUND_DOWN
                ctx.traps[Inexact] = True
                ctx.traps[Rounded] = True
                budget.reserve("a", "1")
                budget.reserve("b", "0.11")
                before_events = len(journal.load_events("model_budget", "policy-1"))
                with self.assertRaisesRegex(ValueError, "budget exhausted"):
                    budget.reserve("over", "0.01")
                self.assertEqual(len(journal.load_events("model_budget", "policy-1")), before_events)
                budget.settle("a", incurred="0.8", estimated_unbilled="0.1")
                budget.reconcile_unbilled(billing_id="bill", request_id="a", billed="0.06")
                expected = budget.snapshot()
                restarted_journal, restarted = open_budget(directory, ceiling="1.11")
                self.assertEqual(restarted.snapshot(), expected)
                self.assertEqual(expected.available, Decimal("0.1"))
                self.assertFalse(restarted.reconcile_unbilled(billing_id="bill", request_id="a", billed="0.06"))

    def test_unrepresentable_settlement_never_commits_or_consumes_reservation(self):
        with TemporaryDirectory() as directory:
            journal, budget = open_budget(directory, ceiling="1.001")
            budget.reserve("a", "1.001")
            count = len(journal.load_events("model_budget", "policy-1"))
            with localcontext() as ctx:
                ctx.prec = 3
                ctx.rounding = ROUND_DOWN
                with self.assertRaisesRegex(ValueError, "exceeds reserved"):
                    budget.settle("a", incurred="1.001", estimated_unbilled="0.00009")
            self.assertEqual(len(journal.load_events("model_budget", "policy-1")), count)
            self.assertEqual(budget.active_reservation("a"), Decimal("1.001"))
            self.assertTrue(budget.release("a"))


class ModelBudgetAdmissionRaceTests(unittest.TestCase):
    def test_commit_version_cannot_come_from_a_newer_cut_than_budget_validation(self):
        with TemporaryDirectory() as directory:
            journal, stale = open_budget(directory, ceiling="1")
            _, winner = open_budget(directory, ceiling="1")
            original_events = stale._events
            raced = False
            def read_then_concurrent_commit():
                nonlocal raced
                frozen = original_events()
                if not raced:
                    raced = True
                    winner.reserve("winner", "0.6")
                return frozen
            with patch.object(stale, "_events", side_effect=read_then_concurrent_commit):
                with self.assertRaisesRegex(ValueError, "budget exhausted"):
                    stale.reserve("stale", "0.6")
            self.assertEqual(stale.snapshot().reserved, Decimal("0.6"))
            self.assertIsNone(stale.active_reservation("stale"))
            self.assertEqual(len(journal.load_events("model_budget", "policy-1")), 2)


    def test_two_real_threads_cannot_overbook_from_the_same_frozen_cut(self):
        with TemporaryDirectory() as directory:
            _, first = open_budget(directory, ceiling="1")
            _, second = open_budget(directory, ceiling="1")
            barrier = threading.Barrier(2)
            results, failures = [], []
            def worker(budget, identity):
                original_events = budget._events
                first_read = True
                def synchronized_cut():
                    nonlocal first_read
                    frozen = original_events()
                    if first_read:
                        first_read = False
                        barrier.wait(timeout=5)
                    return frozen
                try:
                    with patch.object(budget, "_events", side_effect=synchronized_cut):
                        results.append(budget.reserve(identity, "0.6"))
                except ValueError as error:
                    failures.append(str(error))
                except BaseException as error:
                    failures.append(type(error).__name__)
            threads = [threading.Thread(target=worker, args=(first, "a")),
                       threading.Thread(target=worker, args=(second, "b"))]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)
                self.assertFalse(thread.is_alive(), "budget concurrency test deadlocked")
            self.assertEqual(results, [True])
            self.assertEqual(failures, ["budget exhausted"])
            self.assertEqual(first.snapshot().reserved, Decimal("0.6"))
            self.assertEqual(first.snapshot().available, Decimal("0.4"))
