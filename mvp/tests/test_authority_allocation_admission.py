import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from mvp.autotrade_mvp.allocation import (
    AllocationPolicy,
    ImmutableAllocationEvidence,
    ObjectiveCandidate,
    allocate_evidence_bound_objective_targets,
)
from mvp.autotrade_mvp.authority import (
    AuthorityConflict,
    AuthorityPolicy,
    AuthorityService,
    InstrumentVersionIdentity,
    allocation_admission_policy_version,
    record_canonical_allocation_evidence,
)
from mvp.autotrade_mvp.capabilities import (
    CapabilityClaim,
    CapabilityRegistry,
    EvidenceVerification,
    derive_capability_snapshot,
)
from mvp.autotrade_mvp.durable_reservations import DurableReservationBook
from mvp.autotrade_mvp.instruments import InstrumentRegistry, InstrumentVersion
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
CAPABILITY_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
NEW_CAPABILITY_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
INSTRUMENT_REF = f"{INSTRUMENT_ID}@1"
NOW = "2026-09-25T18:31:00Z"
DECISION_TIME = "2026-09-25T18:30:00Z"


def instant(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(
        timezone.utc
    )


class AllocationAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.store = JournalStore(Path(self.temp.name) / "journal.sqlite")
        self.instruments = InstrumentRegistry(
            versions=(
                InstrumentVersion(
                    instrument_id=INSTRUMENT_ID,
                    version=1,
                    provider_id="TEST_PROVIDER",
                    venue_id="TEST_VENUE",
                    provider_symbol="ABC",
                    asset_class="CASH_EQUITY",
                    base_currency="ABC",
                    quote_currency="USD",
                    settlement_currency="USD",
                    quantity_unit="ABC",
                    contract_multiplier="1",
                    price_tick="0.01",
                    quantity_step="1",
                    minimum_quantity="1",
                    calendar_id="CONTINUOUS_24_7",
                    timezone_id="UTC",
                    effective_from=instant("2026-09-25T00:00:00Z"),
                ),
            )
        )
        self.capabilities = CapabilityRegistry()
        self.capabilities.add(self.capability(CAPABILITY_ID))
        self.authority = AuthorityService(
            self.store,
            instrument_registry=self.instruments,
            capability_registry=self.capabilities,
        )
        self.policy = AuthorityPolicy.create(
            policy_id="allocation-policy",
            account_id="paper-1",
            environments={"PAPER"},
            instruments={InstrumentVersionIdentity(INSTRUMENT_ID, 1)},
            actions={"ORDER.SUBMIT"},
            max_notional="1000",
            valid_from="2026-09-25T18:00:00Z",
            expires_at="2026-09-25T20:00:00Z",
            autonomous=True,
            protection_only=False,
        )
        self.authority.register_policy(self.policy)
        self.risk_policy = RiskPolicy.create(
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
        self.book = DurableReservationBook(
            self.store,
            environment="PAPER",
            account_id="paper-1",
        )
        self.checkpoint = self.record_reconciliation()

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def capability(
        snapshot_id: str,
        *,
        observed_at: str = "2026-09-25T18:00:00Z",
        expires_at: str = "2026-09-25T19:00:00Z",
    ):
        observed = instant(observed_at)
        expires = instant(expires_at)
        artifact_ids = {
            "DOCUMENTED": "10000000-0000-4000-8000-000000000001",
            "API": "10000000-0000-4000-8000-000000000002",
            "ACCOUNT": "10000000-0000-4000-8000-000000000003",
            "INSTRUMENT": "10000000-0000-4000-8000-000000000004",
        }
        claims = tuple(
            CapabilityClaim(
                source=source,
                provider_id="TEST_PROVIDER",
                account_id="paper-1",
                entity_id="ABC",
                environment="PAPER",
                instrument_version=INSTRUMENT_REF,
                observed_at=observed,
                expires_at=expires,
                supported_order_types=frozenset({"LIMIT"}),
                time_in_force=frozenset({"GTC"}),
                permission_scopes=frozenset({"ORDER.WRITE"}),
                position_mode="NET",
                native_protection=frozenset(),
                rate_limit_policy_id="test-v1",
                data_entitlements=frozenset({"QUOTE", "TRADE"}),
                evidence_ref={
                    "artifact_id": artifact_ids[source],
                    "sha256": "sha256:" + "a" * 64,
                    "observed_at": observed_at,
                },
            )
            for source in ("DOCUMENTED", "API", "ACCOUNT", "INSTRUMENT")
        )
        return derive_capability_snapshot(
            snapshot_id=snapshot_id,
            claims=claims,
            observed_at=observed,
            evidence_verifier=lambda _claim: EvidenceVerification(valid=True),
        )

    def record_reconciliation(self):
        result = reconcile_account(
            provider_id="TEST_PROVIDER",
            account_id="paper-1",
            environment="PAPER",
            local_cash={"USD": "1000"},
            provider_cash={"USD": "1000"},
            local_positions={},
            provider_positions={},
            local_execution_ids=(),
            provider_fills=(),
            snapshot_consistency=SnapshotConsistencyEvidence(
                provider_id="TEST_PROVIDER",
                account_id="paper-1",
                environment="PAPER",
                mode="ATOMIC",
                query_started_at="2026-09-25T18:29:00Z",
                query_completed_at="2026-09-25T18:30:30Z",
            ),
            coverage_start="2026-09-25T18:29:00Z",
            coverage_end="2026-09-25T18:30:30Z",
            pagination_complete=True,
            provider_activity_provider_id="TEST_PROVIDER",
            provider_activity_account_id="paper-1",
            resource_availability=ResourceAvailabilityEvidence(
                provider_id="TEST_PROVIDER",
                account_id="paper-1",
                environment="PAPER",
                snapshot_id="account-snapshot-1",
                query_started_at="2026-09-25T18:29:00Z",
                query_completed_at="2026-09-25T18:30:30Z",
                provider_as_of="2026-09-25T18:30:20Z",
                valid_until="2026-09-25T18:35:00Z",
                available_resources={"CASH:USD": "1000"},
                evidence_refs=("provider:account-snapshot-1",),
            ),
        )
        checkpoint = record_reconciliation_checkpoint(
            self.store,
            reconciliation_id="paper-1:allocation-admission",
            result=result,
            observed_at="2026-09-25T18:30:30Z",
            host_id="allocation-test-host",
            owner_epoch="1",
        )
        for pending in self.store.pending_outbox():
            if pending["event_id"] == checkpoint["event_id"]:
                self.store.mark_outbox_delivered(
                    pending["outbox_id"],
                    expected_envelope_hash=pending["envelope_hash"],
                )
        return checkpoint

    @staticmethod
    def allocation_policy():
        return AllocationPolicy.create(
            cash_available="1000",
            max_gross_notional="1000",
            max_net_notional="1000",
            max_symbol_notional="1000",
            max_total_cost="50",
            max_stress_loss="500",
            require_adverse_stress_evidence=True,
            require_fresh_stress_evidence=True,
        )

    @staticmethod
    def candidate():
        return ObjectiveCandidate.create(
            symbol="ABC",
            desired_notional="100",
            price="100",
            lot_size="1",
            expected_return_rate="0.10",
            risk_penalty_rate="0.01",
            cost_rate="0",
            capital_requirement_rate="1",
            min_notional="1",
            fee_floor="0",
            max_executable_notional="100",
        )

    @staticmethod
    def evidence(
        *,
        evidence_id: str,
        kind: str,
        payload,
        environment: str,
    ):
        return ImmutableAllocationEvidence.create(
            evidence_id=evidence_id,
            kind=kind,
            environment=environment,
            schema_version="1.0.0",
            observed_at="2026-09-25T18:00:00Z",
            valid_until="2026-09-25T19:00:00Z",
            payload=payload,
        )

    def allocation(self, *, environment="PAPER", persist=True):
        objective = self.evidence(
            evidence_id=f"objective:abc:{environment.lower()}",
            kind="OBJECTIVE",
            environment=environment,
            payload={
                "symbol": "ABC",
                "candidate_id": "candidate:abc:v1",
                "proposal_id": "proposal:abc:v1",
                "strategy_version": "strategy:v1",
                "protocol_digest": "1" * 64,
                "input_snapshot_digest": "2" * 64,
                "information_cutoff": "2026-09-25T18:25:00Z",
                "desired_notional": "100",
                "expected_return_rate": "0.10",
                "risk_penalty_rate": "0.01",
            },
        )
        market = self.evidence(
            evidence_id=f"market:abc:{environment.lower()}",
            kind="MARKET_CONSTRAINT",
            environment=environment,
            payload={
                "symbol": "ABC",
                "instrument_version": INSTRUMENT_REF,
                "provider_id": "TEST_PROVIDER",
                "account_id": "paper-1",
                "capability_snapshot_id": CAPABILITY_ID,
                "source_as_of": "2026-09-25T18:29:00Z",
                "price": "100",
                "lot_size": "1",
                "cost_rate": "0",
                "capital_requirement_rate": "1",
                "min_notional": "1",
                "fee_floor": "0",
                "max_executable_notional": "100",
            },
        )
        capital = self.evidence(
            evidence_id=f"capital:paper-1:{environment.lower()}",
            kind="CAPITAL_STATE",
            environment=environment,
            payload={
                "provider_id": "TEST_PROVIDER",
                "account_id": "paper-1",
                "account_snapshot_id": "account-snapshot-1",
                "reconciliation_run_id": self.checkpoint["event_id"],
                "account_state_version": 7,
                "reservation_state_version": self.book.version,
                "reservation_state_digest": self.book.state_digest,
                "cash_available": "1000",
            },
        )
        stress = self.evidence(
            evidence_id=f"stress:abc:{environment.lower()}",
            kind="STRESS_SCENARIO",
            environment=environment,
            payload={
                "name": "gap_down",
                "shocks": {"ABC": "-0.10"},
                "instrument_versions": {"ABC": INSTRUMENT_REF},
            },
        )
        resolved = {
            item.evidence_id: item
            for item in (objective, market, capital, stress)
        }
        result = allocate_evidence_bound_objective_targets(
            (self.candidate(),),
            self.allocation_policy(),
            objective_evidence={"ABC": objective},
            market_evidence={"ABC": market},
            capital_evidence=capital,
            stress_source_evidence=(stress,),
            resolved_evidence=resolved,
            environment=environment,
            decision_time=DECISION_TIME,
            policy_version=allocation_admission_policy_version(
                self.policy,
                self.risk_policy,
            ),
        )
        self.assertEqual(result.objective.allocation.status, "ALLOCATED")
        self.assertEqual(
            result.objective.allocation.targets[0].quantity,
            1,
        )
        if persist:
            for item in resolved.values():
                record_canonical_allocation_evidence(
                    self.store,
                    item,
                    committed_at="2026-09-25T18:30:10Z",
                )
        return result

    @staticmethod
    def risk_intent(*, quantity="1", price="100"):
        return RiskIntent.create(
            symbol="ABC",
            side="BUY",
            quantity=quantity,
            price=price,
            expected_state_version=7,
        )

    @staticmethod
    def risk_context():
        return RiskContext.create(
            state_version=7,
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

    def kwargs(self, allocation_result, **overrides):
        values = dict(
            command_id="allocation-command",
            idempotency_key="allocation-command",
            admission_id="allocation-admission",
            policy_id=self.policy.policy_id,
            intent_id="allocation-intent",
            intent_hash="sha256:" + "a" * 64,
            account_id="paper-1",
            environment="PAPER",
            instrument_id=INSTRUMENT_ID,
            instrument_version=1,
            action="ORDER.SUBMIT",
            notional="100",
            capability_snapshot_id=CAPABILITY_ID,
            risk_intent=self.risk_intent(),
            risk_context=self.risk_context(),
            risk_policy=self.risk_policy,
            risk_valid_until="2026-09-25T18:40:00Z",
            reservation_book=self.book,
            reservation_id="allocation-reservation",
            reservation_requirements={"CASH:USD": "100"},
            reservation_available={"CASH:USD": "1000"},
            reservation_checkpoint_event_id=self.checkpoint["event_id"],
            reservation_provider_id="TEST_PROVIDER",
            reservation_max_age_seconds="120",
            allocation_result=allocation_result,
            now=NOW,
        )
        values.update(overrides)
        return values

    def test_allocation_admission_binds_canonical_evidence_and_survives_restart(self):
        allocation = self.allocation()
        initial_digest = self.book.state_digest

        first = self.authority.admit(**self.kwargs(allocation))
        self.assertEqual(first.outcome, "ADMITTED")
        self.assertEqual(self.book.version, 1)
        self.assertNotEqual(self.book.state_digest, initial_digest)

        risk_event = self.store.load_events(
            "risk_decision",
            first.risk_decision_id,
        )[0]
        payload = risk_event["payload"]
        self.assertEqual(
            payload["allocation_binding"]["decision_digest"],
            allocation.decision_digest,
        )
        self.assertEqual(
            payload["allocation_binding"]["reservation_state_digest"],
            initial_digest,
        )
        self.assertEqual(
            len(payload["financial_risk_fingerprint"]),
            64,
        )

        restarted_book = DurableReservationBook(
            self.store,
            environment="PAPER",
            account_id="paper-1",
        )
        restarted = AuthorityService(
            self.store,
            instrument_registry=self.instruments,
            capability_registry=self.capabilities,
        )
        replayed = restarted.admit(
            **self.kwargs(
                allocation,
                reservation_book=restarted_book,
            )
        )
        self.assertEqual(replayed, first)
        self.assertEqual(restarted_book.version, 1)
        self.assertEqual(
            restarted_book.total_reserved("CASH:USD"),
            100,
        )

    def test_caller_minted_evidence_absent_from_canonical_resolver_is_rejected(self):
        allocation = self.allocation(persist=False)
        with self.assertRaisesRegex(
            AuthorityConflict,
            "absent or ambiguous",
        ):
            self.authority.admit(**self.kwargs(allocation))
        self.assertEqual(self.book.version, 0)

    def test_reservation_version_advance_invalidates_allocation_before_target_reserve(self):
        allocation = self.allocation()
        self.book.reserve(
            command_id="other-command",
            idempotency_key="other-command",
            reservation_id="other-reservation",
            intent_id="other-intent",
            requirements={"CASH:USD": "1"},
            available={"CASH:USD": "1000"},
        )
        with self.assertRaisesRegex(
            AuthorityConflict,
            "reservation state version advanced",
        ):
            self.authority.admit(**self.kwargs(allocation))
        self.assertEqual(self.book.version, 1)
        with self.assertRaises(KeyError):
            self.book.get("allocation-reservation")

    def test_order_leg_must_match_digest_bound_allocation_target(self):
        allocation = self.allocation()
        with self.assertRaisesRegex(
            AuthorityConflict,
            "quantity differs from allocation target",
        ):
            self.authority.admit(
                **self.kwargs(
                    allocation,
                    notional="200",
                    risk_intent=self.risk_intent(quantity="2"),
                    reservation_requirements={"CASH:USD": "200"},
                )
            )
        self.assertEqual(self.book.version, 0)

    def test_simulation_allocation_cannot_cross_into_paper_admission(self):
        allocation = self.allocation(environment="SIMULATION")
        with self.assertRaisesRegex(
            AuthorityConflict,
            "final admission revalidation",
        ):
            self.authority.admit(**self.kwargs(allocation))
        self.assertEqual(self.book.version, 0)

    def test_superseded_capability_invalidates_bound_allocation(self):
        allocation = self.allocation()
        self.capabilities.add(
            self.capability(
                NEW_CAPABILITY_ID,
                observed_at="2026-09-25T18:30:30Z",
                expires_at="2026-09-25T19:30:00Z",
            )
        )
        with self.assertRaisesRegex(
            AuthorityConflict,
            "superseded after proposal",
        ):
            self.authority.admit(**self.kwargs(allocation))
        self.assertEqual(self.book.version, 0)

    def test_reservation_state_digest_is_restart_stable_and_content_sensitive(self):
        before = self.book.state_digest
        restarted = DurableReservationBook(
            self.store,
            environment="PAPER",
            account_id="paper-1",
        )
        self.assertEqual(restarted.state_digest, before)
        self.book.reserve(
            command_id="digest-command",
            idempotency_key="digest-command",
            reservation_id="digest-reservation",
            intent_id="digest-intent",
            requirements={"CASH:USD": "1"},
            available={"CASH:USD": "1000"},
        )
        self.assertNotEqual(self.book.state_digest, before)
        self.assertEqual(
            DurableReservationBook(
                self.store,
                environment="PAPER",
                account_id="paper-1",
            ).state_digest,
            self.book.state_digest,
        )


if __name__ == "__main__":
    unittest.main()
