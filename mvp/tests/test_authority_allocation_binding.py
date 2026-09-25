from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.allocation import (
    AllocationPolicy,
    ImmutableAllocationEvidence,
    ObjectiveCandidate,
    allocate_evidence_bound_objective_targets,
)
from mvp.autotrade_mvp.authority import (
    AllocationAuthoritySnapshot,
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
ACCOUNT_ID = "paper-allocation"
ENVIRONMENT = "PAPER"
CAPABILITY_ID = "allocation-capability-1"
DECISION_TIME = "2026-09-24T18:00:45Z"
NOW = "2026-09-24T18:01:00Z"
VALID_UNTIL = "2026-09-24T18:02:00Z"


def _authority_policy():
    return AuthorityPolicy.create(
        policy_id="allocation-authority-policy",
        account_id=ACCOUNT_ID,
        environments={ENVIRONMENT},
        instruments={(INSTRUMENT_ID, 1)},
        actions={"ORDER.SUBMIT"},
        max_notional="1000",
        valid_from="2026-09-24T00:00:00Z",
        expires_at="2026-09-25T00:00:00Z",
        autonomous=True,
        protection_only=False,
        version=7,
    )


def _risk_context(*, reserved_delta="0"):
    return RiskContext.create(
        state_version=1,
        equity="1000",
        positions={"ABC": "0"},
        marks={"ABC": "100"},
        reserved_position_delta={"ABC": reserved_delta},
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


def _checkpoint(store):
    result = reconcile_account(
        provider_id=PROVIDER_ID,
        account_id=ACCOUNT_ID,
        environment=ENVIRONMENT,
        local_cash={"USD": "1000"},
        provider_cash={"USD": "1000"},
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
            valid_until="2026-09-24T18:03:00Z",
            available_resources={"CASH:USD": "1000"},
            provider_as_of="2026-09-24T18:00:30Z",
            evidence_refs=("provider:availability-snapshot",),
        ),
    )
    return record_reconciliation_checkpoint(
        store,
        reconciliation_id="allocation-authority",
        result=result,
        observed_at="2026-09-24T18:00:30Z",
        host_id="allocation-test-host",
        owner_epoch="1",
    )


def _evidence(*, evidence_id, kind, payload, environment=ENVIRONMENT):
    return ImmutableAllocationEvidence.create(
        evidence_id=evidence_id,
        kind=kind,
        environment=environment,
        schema_version="1.0.0",
        observed_at="2026-09-24T18:00:00Z",
        valid_until=VALID_UNTIL,
        payload=payload,
    )


def _allocation_bundle(reservations, *, environment=ENVIRONMENT):
    objective = _evidence(
        evidence_id="objective:abc:v1",
        kind="OBJECTIVE",
        environment=environment,
        payload={
            "symbol": "ABC",
            "candidate_id": "candidate:abc:v1",
            "proposal_id": "proposal:abc:v1",
            "strategy_version": "strategy:v1",
            "protocol_digest": "1" * 64,
            "input_snapshot_digest": "2" * 64,
            "information_cutoff": "2026-09-24T18:00:40Z",
            "desired_notional": "100",
            "expected_return_rate": "0.10",
            "risk_penalty_rate": "0.01",
        },
    )
    market = _evidence(
        evidence_id="market:abc:v1",
        kind="MARKET_CONSTRAINT",
        environment=environment,
        payload={
            "symbol": "ABC",
            "instrument_version": "instrument:abc:v1",
            "provider_id": PROVIDER_ID,
            "account_id": ACCOUNT_ID,
            "capability_snapshot_id": CAPABILITY_ID,
            "source_as_of": "2026-09-24T18:00:30Z",
            "price": "100",
            "lot_size": "1",
            "cost_rate": "0",
            "capital_requirement_rate": "1",
            "min_notional": "0",
            "fee_floor": "0",
            "max_executable_notional": "100",
        },
    )
    capital = _evidence(
        evidence_id="capital:paper-allocation:v7",
        kind="CAPITAL_STATE",
        environment=environment,
        payload={
            "provider_id": PROVIDER_ID,
            "account_id": ACCOUNT_ID,
            "account_snapshot_id": "account-snapshot:v7",
            "reconciliation_run_id": "reconciliation:v7",
            "account_state_version": 7,
            "reservation_state_version": reservations.version,
            "reservation_state_digest": reservations.state_digest,
            "cash_available": "1000",
        },
    )
    stress = _evidence(
        evidence_id="stress:abc:v1",
        kind="STRESS_SCENARIO",
        environment=environment,
        payload={
            "name": "gap_down",
            "shocks": {"ABC": "-0.10"},
            "instrument_versions": {"ABC": "instrument:abc:v1"},
        },
    )
    resolved = {
        item.evidence_id: item
        for item in (objective, market, capital, stress)
    }
    result = allocate_evidence_bound_objective_targets(
        (
            ObjectiveCandidate.create(
                symbol="ABC",
                desired_notional="100",
                price="100",
                lot_size="1",
                expected_return_rate="0.10",
                risk_penalty_rate="0.01",
                cost_rate="0",
                capital_requirement_rate="1",
                min_notional="0",
                fee_floor="0",
                max_executable_notional="100",
            ),
        ),
        AllocationPolicy.create(
            cash_available="1000",
            max_gross_notional="1000",
            max_net_notional="1000",
            max_symbol_notional="1000",
            max_total_cost="50",
            max_stress_loss="500",
        ),
        objective_evidence={"ABC": objective},
        market_evidence={"ABC": market},
        capital_evidence=capital,
        stress_source_evidence=(stress,),
        resolved_evidence=resolved,
        environment=environment,
        decision_time=DECISION_TIME,
        policy_version="allocation-policy:v1",
    )
    return result, resolved


def _snapshot(
    result,
    resolved,
    *,
    account_state_version=7,
    policy_version=None,
    financial_instruments=None,
):
    return AllocationAuthoritySnapshot(
        resolved_evidence=resolved,
        provider_id=PROVIDER_ID,
        account_id=ACCOUNT_ID,
        policy_version=result.policy_version if policy_version is None else policy_version,
        instrument_versions=dict(result.instrument_versions),
        financial_instruments=(
            {"ABC": (INSTRUMENT_ID, 1)}
            if financial_instruments is None
            else financial_instruments
        ),
        capability_snapshot_ids=dict(result.capability_snapshot_ids),
        account_snapshot_id="account-snapshot:v7",
        reconciliation_run_id="reconciliation:v7",
        account_state_version=account_state_version,
    )


def _admit(authority, reservations, checkpoint, allocation_result, **overrides):
    values = dict(
        command_id="allocation-command",
        idempotency_key="allocation-command",
        admission_id="allocation-admission",
        policy_id="allocation-authority-policy",
        intent_id="allocation-intent",
        intent_hash="sha256:" + "a" * 64,
        account_id=ACCOUNT_ID,
        environment=ENVIRONMENT,
        instrument_id=INSTRUMENT_ID,
        instrument_version=1,
        action="ORDER.SUBMIT",
        notional="100",
        capability_snapshot_id=CAPABILITY_ID,
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
        reservation_id="allocation-reservation",
        reservation_requirements={"CASH:USD": "100"},
        reservation_available={"CASH:USD": "1000"},
        reservation_checkpoint_event_id=checkpoint["event_id"],
        reservation_provider_id=PROVIDER_ID,
        reservation_max_age_seconds="120",
        now=NOW,
        allocation_result=allocation_result,
    )
    values.update(overrides)
    return authority.admit(**values)


class AuthorityAllocationBindingTests(unittest.TestCase):
    def test_allocation_binding_is_committed_atomically_and_restart_retry_is_exact(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            store = JournalStore(path)
            reservations = DurableReservationBook(
                store,
                environment=ENVIRONMENT,
                account_id=ACCOUNT_ID,
            )
            result, resolved = _allocation_bundle(reservations)
            authority = AuthorityService(
                store,
                allocation_authority_resolver=lambda _result: _snapshot(
                    result,
                    resolved,
                ),
            )
            authority.register_policy(_authority_policy())
            checkpoint = _checkpoint(store)

            first = _admit(authority, reservations, checkpoint, result)
            self.assertEqual(first.outcome, "ADMITTED")
            self.assertEqual(reservations.version, 1)

            risk_event = store.load_events(
                "risk_decision",
                first.risk_decision_id,
            )[0]
            binding = risk_event["payload"]["allocation_evidence"]
            self.assertEqual(binding["decision_digest"], result.decision_digest)
            self.assertEqual(
                binding["reservation_state_version"],
                0,
            )
            self.assertEqual(
                binding["reservation_state_digest"],
                _allocation_bundle(
                    DurableReservationBook(
                        JournalStore(f"{directory}/empty.sqlite3"),
                        environment=ENVIRONMENT,
                        account_id=ACCOUNT_ID,
                    )
                )[0].reservation_state_digest,
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
                result,
            )
            self.assertEqual(replay, first)
            self.assertEqual(restarted_reservations.version, 1)

    def test_dispatch_barrier_blocks_after_allocation_evidence_expires(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            reservations = DurableReservationBook(
                store,
                environment=ENVIRONMENT,
                account_id=ACCOUNT_ID,
            )
            result, resolved = _allocation_bundle(reservations)
            authority = AuthorityService(
                store,
                allocation_authority_resolver=lambda _result: _snapshot(
                    result,
                    resolved,
                ),
            )
            authority.register_policy(_authority_policy())
            checkpoint = _checkpoint(store)

            admitted = _admit(authority, reservations, checkpoint, result)
            self.assertEqual(admitted.outcome, "ADMITTED")
            binding = store.load_events(
                "risk_decision",
                admitted.risk_decision_id,
            )[0]["payload"]["allocation_evidence"]
            self.assertEqual(binding["valid_until"], VALID_UNTIL)

            self.assertEqual(
                authority.dispatch_allowed(
                    admitted.admission_id,
                    intent_hash="sha256:" + "a" * 64,
                    account_id=ACCOUNT_ID,
                    environment=ENVIRONMENT,
                    instrument_id=INSTRUMENT_ID,
                    instrument_version=1,
                    action="ORDER.SUBMIT",
                    now=VALID_UNTIL,
                    capability_snapshot_id=CAPABILITY_ID,
                ),
                (True, "allowed"),
            )
            self.assertEqual(
                authority.dispatch_allowed(
                    admitted.admission_id,
                    intent_hash="sha256:" + "a" * 64,
                    account_id=ACCOUNT_ID,
                    environment=ENVIRONMENT,
                    instrument_id=INSTRUMENT_ID,
                    instrument_version=1,
                    action="ORDER.SUBMIT",
                    now="2026-09-24T18:02:00.000001Z",
                    capability_snapshot_id=CAPABILITY_ID,
                ),
                (False, "allocation_evidence_expired"),
            )


    def test_self_minted_bundle_absent_from_trusted_resolver_is_rejected(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            reservations = DurableReservationBook(
                store,
                environment=ENVIRONMENT,
                account_id=ACCOUNT_ID,
            )
            result, _resolved = _allocation_bundle(reservations)
            authority = AuthorityService(
                store,
                allocation_authority_resolver=lambda _result: AllocationAuthoritySnapshot(
                    resolved_evidence={},
                    provider_id=PROVIDER_ID,
                    account_id=ACCOUNT_ID,
                    policy_version=result.policy_version,
                    instrument_versions=dict(result.instrument_versions),
                    financial_instruments={"ABC": (INSTRUMENT_ID, 1)},
                    capability_snapshot_ids=dict(result.capability_snapshot_ids),
                    account_snapshot_id="account-snapshot:v7",
                    reconciliation_run_id="reconciliation:v7",
                    account_state_version=7,
                ),
            )
            authority.register_policy(_authority_policy())
            checkpoint = _checkpoint(store)

            with self.assertRaisesRegex(
                AuthorityConflict,
                "evidence set is incomplete",
            ):
                _admit(authority, reservations, checkpoint, result)
            self.assertEqual(reservations.version, 0)

    def test_advanced_account_or_reservation_state_rejects_before_new_reservation(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            reservations = DurableReservationBook(
                store,
                environment=ENVIRONMENT,
                account_id=ACCOUNT_ID,
            )
            result, resolved = _allocation_bundle(reservations)
            authority = AuthorityService(
                store,
                allocation_authority_resolver=lambda _result: _snapshot(
                    result,
                    resolved,
                    account_state_version=8,
                ),
            )
            authority.register_policy(_authority_policy())
            checkpoint = _checkpoint(store)

            with self.assertRaisesRegex(
                AuthorityConflict,
                "stale, mismatched, or non-authoritative",
            ):
                _admit(authority, reservations, checkpoint, result)
            self.assertEqual(reservations.version, 0)

            authority = AuthorityService(
                store,
                allocation_authority_resolver=lambda _result: _snapshot(
                    result,
                    resolved,
                ),
            )
            reservations.reserve(
                command_id="other-command",
                idempotency_key="other-command",
                reservation_id="other-reservation",
                intent_id="other-intent",
                requirements={"CASH:USD": "1"},
                available={"CASH:USD": "1000"},
            )
            self.assertEqual(reservations.version, 1)
            with self.assertRaisesRegex(
                AuthorityConflict,
                "stale, mismatched, or non-authoritative",
            ):
                _admit(authority, reservations, checkpoint, result)
            self.assertEqual(reservations.version, 1)

    def test_trusted_allocation_policy_version_is_independent_of_financial_policy(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            reservations = DurableReservationBook(
                store,
                environment=ENVIRONMENT,
                account_id=ACCOUNT_ID,
            )
            result, resolved = _allocation_bundle(reservations)
            authority = AuthorityService(
                store,
                allocation_authority_resolver=lambda _result: _snapshot(
                    result,
                    resolved,
                ),
            )
            authority.register_policy(_authority_policy())
            checkpoint = _checkpoint(store)

            admitted = _admit(
                authority,
                reservations,
                checkpoint,
                result,
            )
            self.assertEqual(admitted.outcome, "ADMITTED")
            self.assertEqual(
                store.load_events("risk_decision", admitted.risk_decision_id)[0][
                    "payload"
                ]["allocation_evidence"]["policy_version"],
                "allocation-policy:v1",
            )

    def test_stale_allocation_policy_version_rejects_before_transaction_a(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            reservations = DurableReservationBook(
                store,
                environment=ENVIRONMENT,
                account_id=ACCOUNT_ID,
            )
            result, resolved = _allocation_bundle(reservations)
            authority = AuthorityService(
                store,
                allocation_authority_resolver=lambda _result: _snapshot(
                    result,
                    resolved,
                    policy_version="allocation-policy:v2",
                ),
            )
            authority.register_policy(_authority_policy())
            checkpoint = _checkpoint(store)

            with self.assertRaisesRegex(
                AuthorityConflict,
                "stale, mismatched, or non-authoritative",
            ):
                _admit(authority, reservations, checkpoint, result)
            self.assertEqual(reservations.version, 0)
            self.assertFalse(
                any(
                    item["topic"] == "financial.admission.ready"
                    for item in store.pending_outbox()
                )
            )

    def test_allocation_symbol_must_resolve_to_admitted_canonical_instrument(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            reservations = DurableReservationBook(
                store,
                environment=ENVIRONMENT,
                account_id=ACCOUNT_ID,
            )
            result, resolved = _allocation_bundle(reservations)
            other_instrument = "22222222-2222-4222-8222-222222222222"
            authority = AuthorityService(
                store,
                allocation_authority_resolver=lambda _result: _snapshot(
                    result,
                    resolved,
                    financial_instruments={"ABC": (other_instrument, 1)},
                ),
            )
            authority.register_policy(_authority_policy())
            checkpoint = _checkpoint(store)

            with self.assertRaisesRegex(
                AuthorityConflict,
                "allocation instrument does not match admitted instrument version",
            ):
                _admit(authority, reservations, checkpoint, result)
            self.assertEqual(reservations.version, 0)
            self.assertFalse(
                any(
                    item["topic"] == "financial.admission.ready"
                    for item in store.pending_outbox()
                )
            )


    def test_order_cannot_exceed_or_move_away_from_bound_target(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            reservations = DurableReservationBook(
                store,
                environment=ENVIRONMENT,
                account_id=ACCOUNT_ID,
            )
            result, resolved = _allocation_bundle(reservations)
            authority = AuthorityService(
                store,
                allocation_authority_resolver=lambda _result: _snapshot(
                    result,
                    resolved,
                ),
            )
            authority.register_policy(_authority_policy())
            checkpoint = _checkpoint(store)

            with self.assertRaisesRegex(
                AuthorityConflict,
                "exceeds remaining allocation target",
            ):
                _admit(
                    authority,
                    reservations,
                    checkpoint,
                    result,
                    risk_intent=RiskIntent.create(
                        symbol="ABC",
                        side="BUY",
                        quantity="2",
                        price="100",
                        expected_state_version=1,
                    ),
                    notional="200",
                    reservation_requirements={"CASH:USD": "200"},
                )
            self.assertEqual(reservations.version, 0)

            with self.assertRaisesRegex(
                AuthorityConflict,
                "moves away from allocation target",
            ):
                _admit(
                    authority,
                    reservations,
                    checkpoint,
                    result,
                    risk_intent=RiskIntent.create(
                        symbol="ABC",
                        side="SELL",
                        quantity="1",
                        price="100",
                        expected_state_version=1,
                    ),
                )
            self.assertEqual(reservations.version, 0)


if __name__ == "__main__":
    unittest.main()
