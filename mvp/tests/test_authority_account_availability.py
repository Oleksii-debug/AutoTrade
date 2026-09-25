from copy import deepcopy
from decimal import Decimal
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from mvp.autotrade_mvp.authority import (
    AuthorityConflict,
    AuthorityPolicy,
    AuthorityService,
)
from mvp.autotrade_mvp.durable_reservations import DurableReservationBook
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.reconciliation import (
    ResourceAvailabilityEvidence,
    SnapshotConsistencyEvidence,
    reconcile_account,
)
from mvp.autotrade_mvp.reconciliation_journal import (
    record_reconciliation_checkpoint,
)
from mvp.autotrade_mvp.risk import RiskContext, RiskIntent, RiskPolicy


INSTRUMENT_ID = "11111111-1111-4111-8111-111111111111"
PROVIDER_ID = "TEST_PROVIDER"
ACCOUNT_ID = "paper-availability"
ENVIRONMENT = "PAPER"
NOW = "2026-09-24T18:01:00Z"


def _policy():
    return AuthorityPolicy.create(
        policy_id="availability-policy",
        account_id=ACCOUNT_ID,
        environments={ENVIRONMENT},
        instruments={(INSTRUMENT_ID, 1)},
        actions={"ORDER.SUBMIT"},
        max_notional="1000",
        valid_from="2026-09-24T00:00:00Z",
        expires_at="2026-09-25T00:00:00Z",
        autonomous=True,
        protection_only=False,
    )


def _risk_context():
    return RiskContext.create(
        state_version=1,
        equity="1000",
        positions={},
        marks={"ABC": "100"},
        reserved_position_delta={},
        daily_pnl="0",
        drawdown_fraction="0",
        market_data_age_seconds="1",
        fx_age_seconds={"USD": "1"},
        margin_headroom="1",
        capability_allowed=True,
        borrow_available=True,
        stress_scenarios=({"ABC": "-0.10"},),
    )


def _risk_policy():
    return RiskPolicy.create(
        max_abs_position="10",
        max_single_notional="1000",
        max_gross_leverage="2",
        max_net_leverage="2",
        max_daily_loss="500",
        max_drawdown_fraction="0.20",
        max_data_age_seconds="5",
        max_fx_age_seconds="60",
        min_margin_headroom="0.20",
        max_stress_loss="500",
    )


def _checkpoint(store, *, cash="1000", available_cash=None):
    if available_cash is None:
        available_cash = cash
    result = reconcile_account(
        provider_id=PROVIDER_ID,
        account_id=ACCOUNT_ID,
        environment=ENVIRONMENT,
        local_cash={"USD": cash},
        provider_cash={"USD": cash},
        local_positions={},
        provider_positions={},
        local_execution_ids=(),
        provider_fills=(),
        snapshot_consistency=SnapshotConsistencyEvidence(
            provider_id=PROVIDER_ID,
            account_id=ACCOUNT_ID,
            environment=ENVIRONMENT,
            mode="ATOMIC",
            query_started_at="2026-09-24T18:00:00Z",
            query_completed_at="2026-09-24T18:00:30Z",
        ),
        coverage_start="2026-09-24T18:00:00Z",
        coverage_end=NOW,
        pagination_complete=True,
        provider_activity_provider_id=PROVIDER_ID,
        provider_activity_account_id=ACCOUNT_ID,
        resource_availability=ResourceAvailabilityEvidence(
            provider_id=PROVIDER_ID,
            account_id=ACCOUNT_ID,
            environment=ENVIRONMENT,
            snapshot_id="availability-snapshot",
            query_started_at="2026-09-24T18:00:00Z",
            query_completed_at="2026-09-24T18:00:30Z",
            valid_until="2026-09-24T18:02:00Z",
            available_resources={"CASH:USD": available_cash},
            provider_as_of="2026-09-24T18:00:30Z",
            evidence_refs=("provider:availability-snapshot",),
        ),
    )
    return record_reconciliation_checkpoint(
        store,
        reconciliation_id="availability-authority",
        result=result,
        observed_at="2026-09-24T18:00:30Z",
        host_id="availability-test-host",
        owner_epoch="1",
    )


def _admit(authority, reservations, checkpoint, **overrides):
    values = dict(
        command_id="availability-command",
        idempotency_key="availability-command",
        admission_id="availability-admission",
        policy_id="availability-policy",
        intent_id="availability-intent",
        intent_hash="sha256:" + "a" * 64,
        account_id=ACCOUNT_ID,
        environment=ENVIRONMENT,
        instrument_id=INSTRUMENT_ID,
        instrument_version=1,
        action="ORDER.SUBMIT",
        notional="100",
        capability_snapshot_id="availability-capability-1",
        risk_intent=RiskIntent.create(
            symbol="ABC",
            side="BUY",
            quantity="1",
            price="100",
            expected_state_version=1,
        ),
        risk_context=_risk_context(),
        risk_policy=_risk_policy(),
        risk_valid_until="2026-09-24T18:05:00Z",
        reservation_book=reservations,
        reservation_id="availability-reservation",
        reservation_requirements={"CASH:USD": "100"},
        reservation_available={"CASH:USD": "1000"},
        reservation_checkpoint_event_id=checkpoint["event_id"],
        reservation_provider_id=PROVIDER_ID,
        reservation_max_age_seconds="60",
        now=NOW,
    )
    values.update(overrides)
    return authority.admit(**values)


class AuthorityAccountAvailabilityTests(unittest.TestCase):
    def test_admission_uses_exact_reconciled_cash_and_survives_restart_retry(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            store = JournalStore(path)
            authority = AuthorityService(store)
            authority.register_policy(_policy())
            checkpoint = _checkpoint(store)
            reservations = DurableReservationBook(
                store,
                environment=ENVIRONMENT,
                account_id=ACCOUNT_ID,
            )

            first = _admit(authority, reservations, checkpoint)
            self.assertEqual(first.outcome, "ADMITTED")
            self.assertEqual(
                reservations.total_reserved("CASH:USD"),
                Decimal("100"),
            )

            risk_event = store.load_events(
                "risk_decision", first.risk_decision_id
            )[0]
            evidence = risk_event["payload"][
                "reservation_availability_evidence"
            ]
            self.assertEqual(
                evidence["checkpoint_event_id"],
                checkpoint["event_id"],
            )
            self.assertEqual(
                evidence["checkpoint_payload_hash"],
                checkpoint["payload_hash"],
            )
            self.assertEqual(
                evidence["availability"],
                {"CASH:USD": "1000"},
            )
            self.assertEqual(
                evidence["resource_snapshot_id"],
                "availability-snapshot",
            )

            restarted_store = JournalStore(path)
            restarted_authority = AuthorityService(restarted_store)
            restarted_reservations = DurableReservationBook(
                restarted_store,
                environment=ENVIRONMENT,
                account_id=ACCOUNT_ID,
            )
            replay = _admit(
                restarted_authority,
                restarted_reservations,
                checkpoint,
            )
            self.assertEqual(replay, first)
            self.assertEqual(
                restarted_reservations.total_reserved("CASH:USD"),
                Decimal("100"),
            )
            self.assertEqual(
                len(
                    [
                        item
                        for item in restarted_store.pending_outbox()
                        if item["topic"] == "financial.admission.ready"
                    ]
                ),
                1,
            )

    def test_inflated_caller_availability_cannot_increase_reservation_capacity(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            authority = AuthorityService(store)
            authority.register_policy(_policy())
            checkpoint = _checkpoint(
                store,
                cash="1000",
                available_cash="100",
            )
            reservations = DurableReservationBook(
                store,
                environment=ENVIRONMENT,
                account_id=ACCOUNT_ID,
            )

            with self.assertRaisesRegex(
                AuthorityConflict,
                "authoritative reconciliation checkpoint",
            ):
                _admit(
                    authority,
                    reservations,
                    checkpoint,
                    reservation_available={"CASH:USD": "1000"},
                )
            self.assertEqual(reservations.version, 0)
            self.assertEqual(
                reservations.total_reserved("CASH:USD"),
                Decimal("0"),
            )

    def test_resource_availability_requires_provider_provenance(self):
        common = dict(
            provider_id=PROVIDER_ID,
            account_id=ACCOUNT_ID,
            environment=ENVIRONMENT,
            snapshot_id="availability-snapshot",
            query_started_at="2026-09-24T18:00:00Z",
            query_completed_at="2026-09-24T18:00:30Z",
            valid_until="2026-09-24T18:02:00Z",
            available_resources={"CASH:USD": "1000"},
        )
        with self.assertRaisesRegex(ValueError, "at least one evidence_ref"):
            ResourceAvailabilityEvidence(**common, evidence_refs=())
        with self.assertRaisesRegex(TypeError, "tuple of strings"):
            ResourceAvailabilityEvidence(
                **common,
                evidence_refs=["provider:availability-snapshot"],
            )
        with self.assertRaisesRegex(ValueError, "must be unique"):
            ResourceAvailabilityEvidence(
                **common,
                evidence_refs=("provider:same", " provider:same "),
            )

    def test_malformed_persisted_capacity_provenance_fails_before_reservation(self):
        cases = (
            ("empty", []),
            ("wrong-type", "provider:availability-snapshot"),
            ("duplicate", ["provider:same", " provider:same "]),
        )
        for label, bad_refs in cases:
            with self.subTest(label=label), TemporaryDirectory() as directory:
                store = JournalStore(f"{directory}/journal.sqlite3")
                authority = AuthorityService(store)
                authority.register_policy(_policy())
                checkpoint = _checkpoint(store)
                reservations = DurableReservationBook(
                    store,
                    environment=ENVIRONMENT,
                    account_id=ACCOUNT_ID,
                )
                tampered = deepcopy(checkpoint)
                tampered["payload"]["resource_availability"][
                    "evidence_refs"
                ] = bad_refs

                with patch.object(store, "get_event", return_value=tampered):
                    with self.assertRaisesRegex(
                        ValueError,
                        "resource availability evidence_refs",
                    ):
                        _admit(authority, reservations, checkpoint)

                self.assertEqual(reservations.version, 0)
                self.assertEqual(
                    reservations.total_reserved("CASH:USD"),
                    Decimal("0"),
                )

    def test_stale_or_cross_account_checkpoint_fails_closed(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            authority = AuthorityService(store)
            authority.register_policy(_policy())
            checkpoint = _checkpoint(store)
            reservations = DurableReservationBook(
                store,
                environment=ENVIRONMENT,
                account_id=ACCOUNT_ID,
            )

            with self.assertRaisesRegex(ValueError, "stale"):
                _admit(
                    authority,
                    reservations,
                    checkpoint,
                    reservation_max_age_seconds="1",
                )
            with self.assertRaisesRegex(ValueError, "scope mismatch"):
                _admit(
                    authority,
                    reservations,
                    checkpoint,
                    account_id="other-account",
                )
            self.assertEqual(reservations.version, 0)


if __name__ == "__main__":
    unittest.main()
