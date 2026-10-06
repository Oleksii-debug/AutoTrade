from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import gc
import unittest
import weakref
from unittest.mock import patch

import mvp.autotrade_mvp.authority as authority_module
from mvp.autotrade_mvp.accounting import book_external_cash_flow
from mvp.autotrade_mvp.authority import (
    AuthoritativeRiskSnapshot,
    AuthorityConflict,
    AuthorityPolicy,
    AuthorityService,
)
from mvp.autotrade_mvp.durable_reservations import DurableReservationBook
from mvp.autotrade_mvp.durable_settlement import DurableSettlementBook
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.provider_activity_accounting import (
    DurableProviderEconomicBook,
)
from mvp.autotrade_mvp.reconciliation import (
    ResourceAvailabilityEvidence,
    SnapshotConsistencyEvidence,
    reconcile_account,
)
from mvp.autotrade_mvp.reconciliation_journal import (
    record_reconciliation_checkpoint,
)
from mvp.autotrade_mvp.risk import RiskContext, RiskIntent, RiskPolicy
from mvp.autotrade_mvp.settlement import (
    BuyingPowerEvidence,
    SettlementAccountScope,
)
from research.autotrade_research.artifacts.store import ArtifactStore


INSTRUMENT_ID = "11111111-1111-4111-8111-111111111111"
PROVIDER_ID = "TEST_PROVIDER"
ACCOUNT_ID = "paper-availability"
ENVIRONMENT = "SIMULATION"
NOW = "2026-09-24T18:01:00Z"


class _ExplosiveAdmissionText(str):
    calls = 0

    def _explode(self, *_args, **_kwargs):
        type(self).calls += 1
        raise AssertionError("financial admission invoked polymorphic text")

    strip = _explode
    upper = _explode
    lower = _explode
    __eq__ = _explode


class _ExplosiveAdmissionDict(dict):
    calls = 0

    def items(self, *_args, **_kwargs):
        type(self).calls += 1
        raise AssertionError("financial admission invoked polymorphic mapping")

    def get(self, *_args, **_kwargs):
        type(self).calls += 1
        raise AssertionError("financial admission invoked polymorphic mapping")


class _ExplosiveReservationBook(DurableReservationBook):
    calls = 0

    def __getattribute__(self, name):
        if name == "store":
            type(self).calls += 1
            raise AssertionError("financial admission read subclass store descriptor")
        return super().__getattribute__(name)


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


def _install_risk_resolver(authority, *, context, risk_policy):
    if authority.risk_authority_resolver is not None:
        return

    def resolve(request):
        return AuthoritativeRiskSnapshot(
            context=context,
            risk_policy=risk_policy,
            account_id=request.account_id,
            environment=request.environment,
            provider_id=request.provider_id,
            instrument_version=request.instrument_version,
            capability_snapshot_id=request.capability_snapshot_id,
            reconciliation_checkpoint_event_id=request.reconciliation_checkpoint_event_id,
            journal_sequence_cut=request.journal_sequence_cut,
            reservation_version=request.reservation_version,
            reservation_state_digest=request.reservation_state_digest,
            authority_policy_id=request.authority_policy_id,
            authority_policy_version=request.authority_policy_version,
            evaluated_at=request.evaluated_at,
            valid_until="2026-09-24T18:05:00Z",
            evidence_refs={
                dimension: f"test:{dimension.lower()}"
                for dimension in (
                    "PORTFOLIO", "MARKET", "MARGIN", "POLICY",
                    "RECONCILIATION", "CAPABILITY", "BORROW", "STRESS",
                    "FX", "FACTORS", "LIQUIDITY", "LIQUIDATION",
                    "SETTLEMENT", "OPTION_LIFECYCLE", "FUTURES_LIFECYCLE",
                )
            },
        )

    authority.risk_authority_resolver = resolve


def _checkpoint(
    store,
    *,
    cash="1000",
    available_cash=None,
    observed_at="2026-09-24T18:00:30Z",
    reconciliation_id="availability-authority",
    snapshot_id="availability-snapshot",
    force_incomplete=False,
    margin_credit=None,
):
    if available_cash is None:
        available_cash = cash
    available_resources = {"CASH:USD": available_cash}
    resource_details = None
    if margin_credit is not None:
        buying_power = BuyingPowerEvidence(
            evidence_id=f"{snapshot_id}-margin-credit",
            scope=SettlementAccountScope(
                provider_id=PROVIDER_ID,
                account_id=ACCOUNT_ID,
                environment=ENVIRONMENT,
            ),
            currency="USD",
            additional_credit=margin_credit,
            observed_at=datetime(
                2026, 9, 24, 18, 0, 20, tzinfo=timezone.utc
            ),
            valid_until=datetime(
                2026, 9, 24, 18, 2, 0, tzinfo=timezone.utc
            ),
            evidence_refs=(f"provider:{snapshot_id}:margin-credit",),
        )
        available_resources[buying_power.resource_key] = margin_credit
        resource_details = {
            buying_power.resource_key: buying_power.resource_detail(),
        }
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
            snapshot_id=snapshot_id,
            query_started_at="2026-09-24T18:00:00Z",
            query_completed_at="2026-09-24T18:00:30Z",
            valid_until="2026-09-24T18:02:00Z",
            available_resources=available_resources,
            provider_as_of="2026-09-24T18:00:30Z",
            evidence_refs=(f"provider:{snapshot_id}",),
            resource_details=resource_details,
        ),
    )
    if force_incomplete:
        result = replace(
            result,
            complete=False,
            snapshot_consistent=False,
            blocking_resources=("ACCOUNT",),
            reasons=("forced-incomplete-provider-truth",),
        )
    return record_reconciliation_checkpoint(
        store,
        reconciliation_id=reconciliation_id,
        result=result,
        observed_at=observed_at,
        host_id="availability-test-host",
        owner_epoch="1",
    )


def _capital_authorities(store, directory, *, amount="50"):
    economic = DurableProviderEconomicBook(
        store,
        provider_id=PROVIDER_ID,
        account_id=ACCOUNT_ID,
        environment=ENVIRONMENT,
    )
    economic.append(
        book_external_cash_flow(
            transaction_id="capital-opening-cash",
            cause_event_id="capital-opening-cash-event",
            currency="USD",
            amount=amount,
        ),
        committed_at="2026-09-24T17:59:00Z",
    )
    artifact_root = Path(directory) / "settlement-evidence"
    artifacts = ArtifactStore(artifact_root)
    settlement = DurableSettlementBook(
        store,
        provider_id=PROVIDER_ID,
        account_id=ACCOUNT_ID,
        environment=ENVIRONMENT,
        evidence_artifact_root=artifact_root,
        evidence_artifact_store=artifacts,
    )
    return settlement, economic


def _dispatch(authority, record, *, now=NOW):
    return authority.dispatch_allowed(
        record.admission_id,
        intent_hash=record.intent_hash,
        account_id=record.account_id,
        environment=record.environment,
        instrument_id=record.instrument_version.instrument_id,
        instrument_version=record.instrument_version.version,
        action=record.action,
        now=now,
        capability_snapshot_id=record.capability_snapshot_id,
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
    _install_risk_resolver(
        authority,
        context=values["risk_context"],
        risk_policy=values["risk_policy"],
    )
    return authority.admit(**values)


class AuthorityAccountAvailabilityTests(unittest.TestCase):
    def test_financial_admission_rejects_polymorphic_scope_text_before_callbacks(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            authority = AuthorityService(store)
            authority.register_policy(_policy())
            checkpoint = _checkpoint(store, available_cash="1000")
            reservations = DurableReservationBook(
                store,
                environment=ENVIRONMENT,
                account_id=ACCOUNT_ID,
            )
            fields = (
                "command_id",
                "idempotency_key",
                "admission_id",
                "policy_id",
                "intent_id",
                "intent_hash",
                "account_id",
                "environment",
                "instrument_id",
                "action",
                "notional",
                "capability_snapshot_id",
                "risk_valid_until",
                "reservation_id",
                "reservation_checkpoint_event_id",
                "reservation_provider_id",
                "now",
            )
            for field in fields:
                with self.subTest(field=field):
                    _ExplosiveAdmissionText.calls = 0
                    value = {
                        "environment": ENVIRONMENT,
                        "notional": "100",
                        "risk_valid_until": "2026-09-24T18:05:00Z",
                        "now": NOW,
                        "reservation_provider_id": PROVIDER_ID,
                        "reservation_checkpoint_event_id": checkpoint["event_id"],
                        "instrument_id": INSTRUMENT_ID,
                    }.get(field, f"hostile-{field}")
                    with self.assertRaises(TypeError):
                        _admit(
                            authority,
                            reservations,
                            checkpoint,
                            **{field: _ExplosiveAdmissionText(value)},
                        )
                    self.assertEqual(_ExplosiveAdmissionText.calls, 0)
                    self.assertEqual(
                        reservations.total_reserved("CASH:USD"),
                        Decimal("0"),
                    )

    def test_financial_admission_rejects_reservation_book_subclass_before_store_access(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            authority = AuthorityService(store)
            authority.register_policy(_policy())
            checkpoint = _checkpoint(store, available_cash="1000")
            hostile_book = object.__new__(_ExplosiveReservationBook)
            _ExplosiveReservationBook.calls = 0

            with self.assertRaisesRegex(
                TypeError,
                "reservation_book must be exact DurableReservationBook",
            ):
                _admit(
                    authority,
                    hostile_book,
                    checkpoint,
                )
            self.assertEqual(_ExplosiveReservationBook.calls, 0)

    def test_financial_admission_rejects_polymorphic_availability_mapping_before_callbacks(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            authority = AuthorityService(store)
            authority.register_policy(_policy())
            checkpoint = _checkpoint(store, available_cash="1000")
            reservations = DurableReservationBook(
                store,
                environment=ENVIRONMENT,
                account_id=ACCOUNT_ID,
            )
            _ExplosiveAdmissionDict.calls = 0
            with self.assertRaisesRegex(
                TypeError,
                "reservation_available must be a mapping backed by an exact dict",
            ):
                _admit(
                    authority,
                    reservations,
                    checkpoint,
                    reservation_available=_ExplosiveAdmissionDict(
                        {"CASH:USD": "1000"}
                    ),
                )
            self.assertEqual(_ExplosiveAdmissionDict.calls, 0)
            self.assertEqual(
                reservations.total_reserved("CASH:USD"),
                Decimal("0"),
            )

    def test_capital_binding_releases_retained_books_when_service_dies(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            settlement, economic = _capital_authorities(store, directory)
            authority = AuthorityService(
                store,
                settlement_book=settlement,
                economic_book=economic,
            )
            authority_ref = weakref.ref(authority)
            settlement_ref = weakref.ref(settlement)
            economic_ref = weakref.ref(economic)

            del authority
            del settlement
            del economic
            gc.collect()

            self.assertIsNone(authority_ref())
            self.assertIsNone(settlement_ref())
            self.assertIsNone(economic_ref())

    def test_configured_settlement_capital_clamps_provider_cash(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            settlement, economic = _capital_authorities(
                store,
                directory,
                amount="50",
            )
            authority = AuthorityService(
                store,
                settlement_book=settlement,
                economic_book=economic,
            )
            authority.register_policy(_policy())
            checkpoint = _checkpoint(store, available_cash="1000")
            reservations = DurableReservationBook(
                store,
                environment=ENVIRONMENT,
                account_id=ACCOUNT_ID,
            )

            admitted = _admit(
                authority,
                reservations,
                checkpoint,
                reservation_requirements={"CASH:USD": "40"},
            )
            self.assertEqual(admitted.outcome, "ADMITTED")
            self.assertEqual(
                reservations.total_reserved("CASH:USD"),
                Decimal("40"),
            )
            risk_event = store.load_events(
                "risk_decision",
                admitted.risk_decision_id,
            )[0]
            capital = risk_event["payload"][
                "reservation_availability_evidence"
            ]["settlement_capital_adjustment"]
            self.assertEqual(
                capital["resources"]["CASH:USD"],
                {
                    "provider_available": "1000",
                    "local_available": "50",
                    "effective_available": "50",
                },
            )
            with self.assertRaisesRegex(
                AuthorityConflict,
                "does not match expected journal cut",
            ):
                authority_module._canonical_settlement_capital_adjustment(
                    deepcopy(capital),
                    provider_available={"CASH:USD": "1000"},
                    required_resources=("CASH:USD",),
                    provider_id=PROVIDER_ID,
                    account_id=ACCOUNT_ID,
                    environment=ENVIRONMENT,
                    risk_journal_sequence=risk_event["journal_sequence"],
                    expected_journal_sequence=capital["journal_sequence"] + 1,
                )
            reservation_event = [
                event
                for event in store.load_events(
                    "reservation_book",
                    reservations.scope_id,
                )
                if event["payload"].get("operation") == "RESERVE"
            ][0]
            self.assertEqual(
                reservation_event["payload"]["request"]["available"]["CASH:USD"],
                "50",
            )
            self.assertEqual(_dispatch(authority, admitted), (True, "allowed"))


    def test_configured_margin_credit_is_separate_from_settled_cash(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            settlement, economic = _capital_authorities(
                store,
                directory,
                amount="50",
            )
            authority = AuthorityService(
                store,
                settlement_book=settlement,
                economic_book=economic,
            )
            authority.register_policy(_policy())
            checkpoint = _checkpoint(
                store,
                available_cash="1000",
                margin_credit="250",
            )
            reservations = DurableReservationBook(
                store,
                environment=ENVIRONMENT,
                account_id=ACCOUNT_ID,
            )

            admitted = _admit(
                authority,
                reservations,
                checkpoint,
                reservation_requirements={
                    "CASH:USD": "40",
                    "MARGIN_CREDIT:USD": "200",
                },
                reservation_available={
                    "CASH:USD": "1000",
                    "MARGIN_CREDIT:USD": "250",
                },
            )
            self.assertEqual(admitted.outcome, "ADMITTED")
            self.assertEqual(
                reservations.total_reserved("CASH:USD"),
                Decimal("40"),
            )
            self.assertEqual(
                reservations.total_reserved("MARGIN_CREDIT:USD"),
                Decimal("200"),
            )

            risk_event = store.load_events(
                "risk_decision",
                admitted.risk_decision_id,
            )[0]
            capital = risk_event["payload"][
                "reservation_availability_evidence"
            ]["settlement_capital_adjustment"]
            self.assertEqual(
                capital["resources"]["CASH:USD"],
                {
                    "provider_available": "1000",
                    "local_available": "50",
                    "effective_available": "50",
                },
            )
            self.assertEqual(
                capital["resources"]["MARGIN_CREDIT:USD"],
                {
                    "provider_available": "250",
                    "local_available": "250",
                    "effective_available": "250",
                },
            )

            reservation_event = [
                event
                for event in store.load_events(
                    "reservation_book",
                    reservations.scope_id,
                )
                if event["payload"].get("operation") == "RESERVE"
            ][0]
            self.assertEqual(
                reservation_event["payload"]["request"]["available"],
                {
                    "CASH:USD": "50",
                    "MARGIN_CREDIT:USD": "250",
                },
            )
            self.assertEqual(_dispatch(authority, admitted), (True, "allowed"))

    def test_margin_credit_never_increases_cash_capacity(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            settlement, economic = _capital_authorities(
                store,
                directory,
                amount="50",
            )
            authority = AuthorityService(
                store,
                settlement_book=settlement,
                economic_book=economic,
            )
            authority.register_policy(_policy())
            checkpoint = _checkpoint(
                store,
                available_cash="1000",
                margin_credit="250",
            )
            reservations = DurableReservationBook(
                store,
                environment=ENVIRONMENT,
                account_id=ACCOUNT_ID,
            )

            with self.assertRaisesRegex(
                ValueError,
                "Insufficient CASH:USD",
            ):
                _admit(
                    authority,
                    reservations,
                    checkpoint,
                    reservation_requirements={"CASH:USD": "60"},
                )
            self.assertEqual(
                reservations.total_reserved("CASH:USD"),
                Decimal("0"),
            )
            self.assertEqual(
                reservations.total_reserved("MARGIN_CREDIT:USD"),
                Decimal("0"),
            )

    def test_dispatch_rechecks_margin_credit_as_separate_capital(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            settlement, economic = _capital_authorities(
                store,
                directory,
                amount="50",
            )
            authority = AuthorityService(
                store,
                settlement_book=settlement,
                economic_book=economic,
            )
            authority.register_policy(_policy())
            checkpoint = _checkpoint(
                store,
                available_cash="1000",
                margin_credit="250",
            )
            reservations = DurableReservationBook(
                store,
                environment=ENVIRONMENT,
                account_id=ACCOUNT_ID,
            )
            admitted = _admit(
                authority,
                reservations,
                checkpoint,
                reservation_requirements={
                    "CASH:USD": "40",
                    "MARGIN_CREDIT:USD": "200",
                },
                reservation_available={
                    "CASH:USD": "1000",
                    "MARGIN_CREDIT:USD": "250",
                },
            )
            self.assertEqual(_dispatch(authority, admitted), (True, "allowed"))

            _checkpoint(
                store,
                available_cash="1000",
                margin_credit="100",
                observed_at="2026-09-24T18:01:10Z",
                reconciliation_id="availability-margin-credit-reduced",
                snapshot_id="availability-margin-credit-reduced",
            )
            self.assertEqual(
                _dispatch(authority, admitted),
                (False, "financial_evidence_invalid"),
            )

    def test_admission_rejects_local_economic_truth_after_provider_query_started(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            settlement, economic = _capital_authorities(
                store,
                directory,
                amount="50",
            )
            economic.append(
                book_external_cash_flow(
                    transaction_id="capital-during-provider-query",
                    cause_event_id="capital-during-provider-query-event",
                    currency="USD",
                    amount="900",
                ),
                committed_at="2026-09-24T18:00:10Z",
            )
            # A later journal event with a backdated committed_at must not hide
            # the earlier economic mutation that occurred inside the provider cut.
            economic.append(
                book_external_cash_flow(
                    transaction_id="capital-backdated-after-query-mutation",
                    cause_event_id="capital-backdated-after-query-mutation-event",
                    currency="USD",
                    amount="50",
                ),
                committed_at="2026-09-24T17:59:30Z",
            )
            authority = AuthorityService(
                store,
                settlement_book=settlement,
                economic_book=economic,
            )
            authority.register_policy(_policy())
            checkpoint = _checkpoint(store, available_cash="1000")
            reservations = DurableReservationBook(
                store,
                environment=ENVIRONMENT,
                account_id=ACCOUNT_ID,
            )

            with self.assertRaisesRegex(
                AuthorityConflict,
                "provider availability predates local economic financial truth",
            ):
                _admit(
                    authority,
                    reservations,
                    checkpoint,
                    reservation_requirements={"CASH:USD": "900"},
                )
            self.assertEqual(
                reservations.total_reserved("CASH:USD"),
                Decimal("0"),
            )

    def test_admission_rejects_local_economic_truth_after_provider_checkpoint(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            settlement, economic = _capital_authorities(
                store,
                directory,
                amount="50",
            )
            authority = AuthorityService(
                store,
                settlement_book=settlement,
                economic_book=economic,
            )
            authority.register_policy(_policy())
            checkpoint = _checkpoint(store, available_cash="1000")
            economic.append(
                book_external_cash_flow(
                    transaction_id="capital-after-provider-checkpoint",
                    cause_event_id="capital-after-provider-checkpoint-event",
                    currency="USD",
                    amount="950",
                ),
                committed_at="2026-09-24T18:00:40Z",
            )
            reservations = DurableReservationBook(
                store,
                environment=ENVIRONMENT,
                account_id=ACCOUNT_ID,
            )

            with self.assertRaisesRegex(
                AuthorityConflict,
                "provider availability predates local economic financial truth",
            ):
                _admit(
                    authority,
                    reservations,
                    checkpoint,
                    reservation_requirements={"CASH:USD": "900"},
                )
            self.assertEqual(
                reservations.total_reserved("CASH:USD"),
                Decimal("0"),
            )

    def test_admission_rejects_capital_change_after_projection_before_commit(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            settlement, economic = _capital_authorities(
                store,
                directory,
                amount="50",
            )
            authority = AuthorityService(
                store,
                settlement_book=settlement,
                economic_book=economic,
            )
            authority.register_policy(_policy())
            checkpoint = _checkpoint(store, available_cash="1000")
            reservations = DurableReservationBook(
                store,
                environment=ENVIRONMENT,
                account_id=ACCOUNT_ID,
            )
            original_resolve = authority_module._resolve_authority_service_capital

            def resolve_then_withdraw(*args, **kwargs):
                capital_cut = original_resolve(*args, **kwargs)
                economic.append(
                    book_external_cash_flow(
                        transaction_id="capital-interleaving-withdrawal",
                        cause_event_id="capital-interleaving-withdrawal-event",
                        currency="USD",
                        amount="-30",
                    ),
                    committed_at="2026-09-24T18:00:30Z",
                )
                return capital_cut

            with patch.object(
                authority_module,
                "_resolve_authority_service_capital",
                side_effect=resolve_then_withdraw,
            ):
                with self.assertRaisesRegex(
                    AuthorityConflict,
                    "settlement capital cut changed before financial commit",
                ):
                    _admit(
                        authority,
                        reservations,
                        checkpoint,
                        reservation_requirements={"CASH:USD": "40"},
                    )

            self.assertEqual(economic.cash("USD"), Decimal("20"))
            self.assertEqual(
                reservations.total_reserved("CASH:USD"),
                Decimal("0"),
            )

    def test_admission_rejects_cash_writer_after_causal_history_read(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            settlement, economic = _capital_authorities(store, directory, amount="50")
            authority = AuthorityService(store, settlement_book=settlement, economic_book=economic)
            authority.register_policy(_policy())
            checkpoint = _checkpoint(store, available_cash="1000")
            reservations = DurableReservationBook(store, environment=ENVIRONMENT, account_id=ACCOUNT_ID)
            original = authority_module._authority_store_call
            injected = False

            def read_then_deposit(service, method, *args, **kwargs):
                nonlocal injected
                result = original(service, method, *args, **kwargs)
                if not injected and method == "load_events" and args == ("economic_book", economic.book_id):
                    injected = True
                    economic.append(book_external_cash_flow(transaction_id="racing-deposit",
                        cause_event_id="racing-deposit-cause", currency="USD", amount="950"),
                        committed_at="2026-09-24T18:00:40Z")
                return result

            with patch.object(authority_module, "_authority_store_call", side_effect=read_then_deposit):
                with self.assertRaisesRegex(AuthorityConflict, "capital authority changed while spendable cash was projected"):
                    _admit(authority, reservations, checkpoint, reservation_requirements={"CASH:USD": "900"})
            self.assertTrue(injected)
            self.assertEqual(reservations.total_reserved("CASH:USD"), Decimal("0"))

    def test_dispatch_rechecks_current_local_settlement_capital(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            settlement, economic = _capital_authorities(
                store,
                directory,
                amount="50",
            )
            authority = AuthorityService(
                store,
                settlement_book=settlement,
                economic_book=economic,
            )
            authority.register_policy(_policy())
            checkpoint = _checkpoint(store, available_cash="1000")
            reservations = DurableReservationBook(
                store,
                environment=ENVIRONMENT,
                account_id=ACCOUNT_ID,
            )
            admitted = _admit(
                authority,
                reservations,
                checkpoint,
                reservation_requirements={"CASH:USD": "40"},
            )
            self.assertEqual(_dispatch(authority, admitted), (True, "allowed"))

            economic.append(
                book_external_cash_flow(
                    transaction_id="capital-withdrawal",
                    cause_event_id="capital-withdrawal-event",
                    currency="USD",
                    amount="-20",
                ),
                committed_at="2026-09-24T18:01:10Z",
            )
            self.assertEqual(
                _dispatch(authority, admitted),
                (False, "financial_evidence_invalid"),
            )

    def test_dispatch_rejects_reconciliation_advance_during_local_capital_read(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            settlement, economic = _capital_authorities(
                store,
                directory,
                amount="50",
            )
            authority = AuthorityService(
                store,
                settlement_book=settlement,
                economic_book=economic,
            )
            authority.register_policy(_policy())
            checkpoint = _checkpoint(store, available_cash="1000")
            reservations = DurableReservationBook(
                store,
                environment=ENVIRONMENT,
                account_id=ACCOUNT_ID,
            )
            admitted = _admit(
                authority,
                reservations,
                checkpoint,
                reservation_requirements={"CASH:USD": "40"},
            )
            self.assertEqual(_dispatch(authority, admitted), (True, "allowed"))

            original_resolve = authority_module._resolve_authority_service_capital
            injected = False

            def resolve_after_provider_read(*args, **kwargs):
                nonlocal injected
                if not injected:
                    injected = True
                    _checkpoint(
                        store,
                        available_cash="0",
                        observed_at="2026-09-24T18:00:40Z",
                        reconciliation_id="availability-authority-newer",
                        snapshot_id="availability-snapshot-newer",
                    )
                return original_resolve(*args, **kwargs)

            with patch.object(
                authority_module,
                "_resolve_authority_service_capital",
                side_effect=resolve_after_provider_read,
            ):
                self.assertEqual(
                    _dispatch(authority, admitted),
                    (False, "financial_evidence_invalid"),
                )
            self.assertTrue(injected)

    def test_dispatch_rejects_reconciliation_advance_after_provider_read_without_capital(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            authority = AuthorityService(store)
            authority.register_policy(_policy())
            checkpoint = _checkpoint(store, available_cash="1000")
            reservations = DurableReservationBook(
                store,
                environment=ENVIRONMENT,
                account_id=ACCOUNT_ID,
            )
            admitted = _admit(
                authority,
                reservations,
                checkpoint,
                reservation_requirements={"CASH:USD": "40"},
            )
            self.assertEqual(_dispatch(authority, admitted), (True, "allowed"))

            original_load = authority_module.load_account_resource_availability_evidence
            injected = False

            def load_then_advance(*args, **kwargs):
                nonlocal injected
                evidence = original_load(*args, **kwargs)
                if not injected:
                    injected = True
                    _checkpoint(
                        store,
                        cash="0",
                        available_cash="0",
                        observed_at="2026-09-24T18:00:40Z",
                        reconciliation_id="availability-dispatch-race-newer",
                        snapshot_id="availability-dispatch-race-newer",
                    )
                return evidence

            with patch.object(
                authority_module,
                "load_account_resource_availability_evidence",
                side_effect=load_then_advance,
            ):
                self.assertEqual(
                    _dispatch(authority, admitted),
                    (False, "financial_evidence_invalid"),
                )
            self.assertTrue(injected)

    def test_restart_with_local_capital_rejects_legacy_cash_admission_without_capital_cut(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            store = JournalStore(path)
            _seed_settlement, _seed_economic = _capital_authorities(
                store,
                directory,
                amount="50",
            )
            legacy_authority = AuthorityService(store)
            legacy_authority.register_policy(_policy())
            checkpoint = _checkpoint(store, available_cash="1000")
            reservations = DurableReservationBook(
                store,
                environment=ENVIRONMENT,
                account_id=ACCOUNT_ID,
            )
            admitted = _admit(
                legacy_authority,
                reservations,
                checkpoint,
                reservation_requirements={"CASH:USD": "40"},
            )
            risk_event = store.load_events(
                "risk_decision",
                admitted.risk_decision_id,
            )[0]
            self.assertNotIn(
                "settlement_capital_adjustment",
                risk_event["payload"]["reservation_availability_evidence"],
            )
            self.assertEqual(
                _dispatch(legacy_authority, admitted),
                (True, "allowed"),
            )

            restarted_store = JournalStore(path)
            artifact_root = Path(directory) / "settlement-evidence"
            artifacts = ArtifactStore(artifact_root)
            restarted_settlement = DurableSettlementBook(
                restarted_store,
                provider_id=PROVIDER_ID,
                account_id=ACCOUNT_ID,
                environment=ENVIRONMENT,
                evidence_artifact_root=artifact_root,
                evidence_artifact_store=artifacts,
            )
            restarted_economic = DurableProviderEconomicBook(
                restarted_store,
                provider_id=PROVIDER_ID,
                account_id=ACCOUNT_ID,
                environment=ENVIRONMENT,
            )
            restarted_authority = AuthorityService(
                restarted_store,
                settlement_book=restarted_settlement,
                economic_book=restarted_economic,
            )
            with self.assertRaisesRegex(
                AuthorityConflict,
                "lacks required settlement capital evidence",
            ):
                restarted_authority._validate_durable_financial_evidence(
                    admitted,
                    restarted_authority._policies[admitted.policy_id],
                    require_transaction_cut=True,
                )
            self.assertEqual(
                _dispatch(restarted_authority, admitted),
                (False, "financial_evidence_invalid"),
            )

    def test_provider_domain_capital_requires_exact_economic_book_domain(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            artifact_root = Path(directory) / "settlement-evidence"
            artifacts = ArtifactStore(artifact_root)
            settlement = DurableSettlementBook(
                store,
                provider_id="BYBIT",
                account_id="bybit-account",
                environment="PAPER",
                provider_environment="TESTNET",
                evidence_artifact_root=artifact_root,
                evidence_artifact_store=artifacts,
            )
            economic = DurableProviderEconomicBook(
                store,
                provider_id="BYBIT",
                account_id="bybit-account",
                environment="PAPER",
                provider_environment="TESTNET",
            )
            authority = AuthorityService(
                store,
                settlement_book=settlement,
                economic_book=economic,
            )
            self.assertIsNotNone(authority)

            demo_economic = DurableProviderEconomicBook(
                store,
                provider_id="BYBIT",
                account_id="bybit-account",
                environment="PAPER",
                provider_environment="DEMO",
            )
            with self.assertRaisesRegex(
                AuthorityConflict,
                "settlement and economic capital scopes do not match",
            ):
                AuthorityService(
                    store,
                    settlement_book=settlement,
                    economic_book=demo_economic,
                )

            capital = {
                "schema_version": "settlement-capital-cut.v1",
                "journal_sequence": 0,
                "provider_id": "BYBIT",
                "account_id": "bybit-account",
                "environment": "PAPER",
                "provider_environment": "TESTNET",
                "settlement_scope_id": "settlement:testnet",
                "economic_book_id": economic.book_id,
                "resources": {
                    "CASH:USD": {
                        "provider_available": "100",
                        "local_available": "80",
                        "effective_available": "80",
                    }
                },
            }
            canonical, effective = (
                authority_module._canonical_settlement_capital_adjustment(
                    capital,
                    provider_available={"CASH:USD": Decimal("100")},
                    required_resources=("CASH:USD",),
                    provider_id="BYBIT",
                    account_id="bybit-account",
                    environment="PAPER",
                    provider_environment="TESTNET",
                )
            )
            self.assertEqual(canonical["provider_environment"], "TESTNET")
            self.assertEqual(effective["CASH:USD"], Decimal("80"))
            with self.assertRaisesRegex(
                AuthorityConflict,
                "provider domain is inconsistent",
            ):
                authority_module._canonical_settlement_capital_adjustment(
                    capital,
                    provider_available={"CASH:USD": Decimal("100")},
                    required_resources=("CASH:USD",),
                    provider_id="BYBIT",
                    account_id="bybit-account",
                    environment="PAPER",
                    provider_environment="DEMO",
                )

    def test_authoritative_risk_provider_domain_fences_availability_evidence(self):
        snapshot = {
            "provider_id": "BYBIT",
            "account_id": "bybit-account",
            "environment": "PAPER",
            "provider_environment": "TESTNET",
        }
        matching = {
            "provider_id": "BYBIT",
            "account_id": "bybit-account",
            "environment": "PAPER",
            "provider_environment": "TESTNET",
        }
        self.assertEqual(
            authority_module._require_provider_scope_matches_authoritative_risk_snapshot(
                snapshot,
                matching,
                evidence_name="test availability",
            ),
            ("BYBIT", "TESTNET"),
        )

        wrong_domain = {
            **matching,
            "provider_environment": "DEMO",
        }
        with self.assertRaisesRegex(
            AuthorityConflict,
            "provider/account scope differs from authoritative risk snapshot",
        ):
            authority_module._require_provider_scope_matches_authoritative_risk_snapshot(
                snapshot,
                wrong_domain,
                evidence_name="test availability",
            )

        wrong_account = {
            **matching,
            "account_id": "another-bybit-account",
        }
        with self.assertRaisesRegex(
            AuthorityConflict,
            "provider/account scope differs from authoritative risk snapshot",
        ):
            authority_module._require_provider_scope_matches_authoritative_risk_snapshot(
                snapshot,
                wrong_account,
                evidence_name="test availability",
            )

        wrong_provider = {
            **matching,
            "provider_id": "KRAKEN",
            "provider_environment": "PAPER",
        }
        with self.assertRaisesRegex(
            AuthorityConflict,
            "provider/account scope differs from authoritative risk snapshot",
        ):
            authority_module._require_provider_scope_matches_authoritative_risk_snapshot(
                snapshot,
                wrong_provider,
                evidence_name="test availability",
            )

        legacy_snapshot = {
            "provider_id": "TEST_PROVIDER",
            "account_id": "legacy-account",
            "environment": "SIMULATION",
        }
        legacy_evidence = dict(legacy_snapshot)
        self.assertEqual(
            authority_module._require_provider_scope_matches_authoritative_risk_snapshot(
                legacy_snapshot,
                legacy_evidence,
                evidence_name="legacy availability",
            ),
            ("TEST_PROVIDER", "SIMULATION"),
        )

        incompatible_runtime_snapshot = {
            "provider_id": "BYBIT",
            "account_id": "bybit-account",
            "environment": "PAPER",
            "provider_environment": "MAINNET",
        }
        with self.assertRaisesRegex(
            AuthorityConflict,
            "provider domain does not match runtime",
        ):
            authority_module._require_provider_scope_matches_authoritative_risk_snapshot(
                incompatible_runtime_snapshot,
                matching,
                evidence_name="test availability",
            )

        missing_snapshot_domain = {
            "provider_id": "BYBIT",
            "account_id": "bybit-account",
            "environment": "PAPER",
        }
        with self.assertRaisesRegex(
            AuthorityConflict,
            "lacks provider_environment",
        ):
            authority_module._require_provider_scope_matches_authoritative_risk_snapshot(
                missing_snapshot_domain,
                matching,
                evidence_name="test availability",
            )

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
                evidence["scope_latest_checkpoint_event_id"],
                checkpoint["event_id"],
            )
            self.assertEqual(
                evidence["scope_latest_checkpoint_aggregate_id"],
                checkpoint["aggregate_id"],
            )
            self.assertEqual(
                evidence["scope_latest_checkpoint_aggregate_version"],
                checkpoint["aggregate_version"],
            )
            self.assertEqual(
                evidence["scope_latest_checkpoint_journal_sequence"],
                checkpoint["journal_sequence"],
            )
            self.assertEqual(
                evidence["availability"],
                {"CASH:USD": "1000"},
            )
            self.assertEqual(
                evidence["resource_snapshot_id"],
                "availability-snapshot",
            )
            self.assertEqual(
                evidence["resource_evidence_refs"],
                ["provider:availability-snapshot"],
            )
            self.assertIsInstance(evidence["resource_evidence_refs"], list)

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

    def test_new_admission_cannot_select_superseded_reconciliation_truth(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            authority = AuthorityService(store)
            authority.register_policy(_policy())
            older = _checkpoint(
                store,
                available_cash="1000",
                observed_at="2026-09-24T18:00:30Z",
                reconciliation_id="older-reconciliation",
                snapshot_id="availability-old",
            )
            _checkpoint(
                store,
                cash="50",
                available_cash="50",
                observed_at="2026-09-24T18:00:40Z",
                reconciliation_id="newer-reconciliation",
                snapshot_id="availability-new",
            )
            reservations = DurableReservationBook(
                store,
                environment=ENVIRONMENT,
                account_id=ACCOUNT_ID,
            )

            with self.assertRaisesRegex(AuthorityConflict, "superseded"):
                _admit(authority, reservations, older)
            self.assertEqual(reservations.total_reserved("CASH:USD"), Decimal("0"))

    def test_newer_incomplete_truth_supersedes_older_complete_checkpoint(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            authority = AuthorityService(store)
            authority.register_policy(_policy())
            older = _checkpoint(
                store,
                available_cash="1000",
                observed_at="2026-09-24T18:00:30Z",
                reconciliation_id="complete-reconciliation",
                snapshot_id="availability-complete",
            )
            _checkpoint(
                store,
                available_cash="1000",
                observed_at="2026-09-24T18:00:40Z",
                reconciliation_id="incomplete-reconciliation",
                snapshot_id="availability-incomplete",
                force_incomplete=True,
            )
            reservations = DurableReservationBook(
                store,
                environment=ENVIRONMENT,
                account_id=ACCOUNT_ID,
            )

            with self.assertRaisesRegex(AuthorityConflict, "superseded"):
                _admit(authority, reservations, older)
            self.assertEqual(reservations.total_reserved("CASH:USD"), Decimal("0"))

    def test_exact_retry_keeps_immutable_checkpoint_but_dispatch_rechecks_current_truth(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            store = JournalStore(path)
            authority = AuthorityService(store)
            authority.register_policy(_policy())
            older = _checkpoint(
                store,
                available_cash="1000",
                observed_at="2026-09-24T18:00:30Z",
                reconciliation_id="retry-reconciliation-a",
                snapshot_id="availability-retry-a",
            )
            reservations = DurableReservationBook(
                store,
                environment=ENVIRONMENT,
                account_id=ACCOUNT_ID,
            )
            first = _admit(authority, reservations, older)
            self.assertEqual(first.outcome, "ADMITTED")
            risk_event = store.load_events(
                "risk_decision",
                first.risk_decision_id,
            )[0]
            journal_cut = risk_event["payload"]["journal_sequence_cut"]
            self.assertIs(type(journal_cut), int)
            self.assertGreaterEqual(journal_cut, 0)
            self.assertGreater(risk_event["journal_sequence"], journal_cut)
            pending_before = len(store.pending_outbox())
            self.assertEqual(_dispatch(authority, first), (True, "allowed"))

            _checkpoint(
                store,
                cash="50",
                available_cash="50",
                observed_at="2026-09-24T18:00:40Z",
                reconciliation_id="retry-reconciliation-b",
                snapshot_id="availability-retry-b",
            )
            replay = _admit(authority, reservations, older)
            self.assertEqual(replay, first)
            self.assertEqual(len(store.pending_outbox()), pending_before + 1)
            self.assertEqual(
                _dispatch(authority, first),
                (False, "financial_evidence_invalid"),
            )

            restarted_store = JournalStore(path)
            restarted_authority = AuthorityService(restarted_store)
            restarted_reservations = DurableReservationBook(
                restarted_store,
                environment=ENVIRONMENT,
                account_id=ACCOUNT_ID,
            )
            restarted_replay = _admit(
                restarted_authority,
                restarted_reservations,
                older,
            )
            self.assertEqual(restarted_replay, first)
            self.assertEqual(
                restarted_reservations.total_reserved("CASH:USD"),
                Decimal("100"),
            )
            self.assertEqual(
                _dispatch(restarted_authority, restarted_replay),
                (False, "financial_evidence_invalid"),
            )

    def test_dispatch_rechecks_resource_expiry_at_send_time_and_after_restart(self):
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
            admitted = _admit(
                authority,
                reservations,
                checkpoint,
                reservation_max_age_seconds="300",
            )
            self.assertEqual(
                _dispatch(
                    authority,
                    admitted,
                    now="2026-09-24T18:01:30Z",
                ),
                (True, "allowed"),
            )
            self.assertEqual(
                _dispatch(
                    authority,
                    admitted,
                    now="2026-09-24T18:02:00Z",
                ),
                (False, "financial_evidence_invalid"),
            )

            restarted = AuthorityService(JournalStore(path))
            self.assertEqual(
                _dispatch(
                    restarted,
                    admitted,
                    now="2026-09-24T18:02:00Z",
                ),
                (False, "financial_evidence_invalid"),
            )
            self.assertEqual(
                restarted._admissions[admitted.admission_id],
                admitted,
            )

    def test_transaction_a_rejects_reconciliation_race_after_latest_check(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            authority = AuthorityService(store)
            authority.register_policy(_policy())
            selected = _checkpoint(
                store,
                available_cash="1000",
                observed_at="2026-09-24T18:00:30Z",
                reconciliation_id="race-reconciliation-a",
                snapshot_id="availability-race-a",
            )
            reservations = DurableReservationBook(
                store,
                environment=ENVIRONMENT,
                account_id=ACCOUNT_ID,
            )
            original_commit = store.commit_command
            injected = False

            def commit_with_newer_truth(**kwargs):
                nonlocal injected
                if not injected:
                    injected = True
                    _checkpoint(
                        store,
                        cash="50",
                        available_cash="50",
                        observed_at="2026-09-24T18:00:40Z",
                        reconciliation_id="race-reconciliation-b",
                        snapshot_id="availability-race-b",
                    )
                return original_commit(**kwargs)

            injection = patch.object(JournalStore, "commit_command", lambda _store, **kwargs: commit_with_newer_truth(**kwargs))
            injection.start()
            self.addCleanup(injection.stop)
            with self.assertRaisesRegex(ValueError, "journal sequence changed"):
                _admit(
                    authority,
                    reservations,
                    selected,
                    command_id="availability-command-race",
                    idempotency_key="availability-command-race",
                    admission_id="availability-admission-race",
                    intent_id="availability-intent-race",
                    intent_hash="sha256:" + "d" * 64,
                    reservation_id="availability-reservation-race",
                )

            self.assertTrue(injected)
            self.assertEqual(
                reservations.total_reserved("CASH:USD"),
                Decimal("0"),
            )
            self.assertFalse(
                any(
                    item["topic"] == "financial.admission.ready"
                    for item in store.pending_outbox()
                )
            )


    def test_decimal_scale_is_canonical_across_admission_restart_replay(self):
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

            first = _admit(
                authority,
                reservations,
                checkpoint,
                notional=Decimal("100.00"),
                reservation_requirements={"CASH:USD": Decimal("100.00")},
                reservation_max_age_seconds=Decimal("60.00"),
            )
            self.assertEqual(first.outcome, "ADMITTED")
            risk_event = store.load_events(
                "risk_decision", first.risk_decision_id
            )[0]
            self.assertEqual(
                risk_event["payload"]["reservation_availability_evidence"][
                    "max_age_seconds"
                ],
                "60",
            )
            admission_event = next(
                item
                for item in store.load_events("authority_state", "canonical")
                if item["event_type"] == "AuthorityAdmissionRecorded"
            )
            self.assertEqual(admission_event["payload"]["notional"], "100")

            restarted_store = JournalStore(path)
            restarted_authority = AuthorityService(restarted_store)
            restarted_reservations = DurableReservationBook(
                restarted_store,
                environment=ENVIRONMENT,
                account_id=ACCOUNT_ID,
            )
            pending_before_replay = len(restarted_store.pending_outbox())
            replay = _admit(
                restarted_authority,
                restarted_reservations,
                checkpoint,
                notional=Decimal("100.0"),
                reservation_requirements={"CASH:USD": Decimal("100.0")},
                reservation_max_age_seconds=Decimal("60.0"),
            )
            self.assertEqual(replay, first)
            self.assertEqual(
                len(restarted_store.pending_outbox()),
                pending_before_replay,
            )

            with self.assertRaises(AuthorityConflict):
                _admit(
                    restarted_authority,
                    restarted_reservations,
                    checkpoint,
                    notional=Decimal("101"),
                    reservation_requirements={"CASH:USD": Decimal("101")},
                    reservation_max_age_seconds=Decimal("60"),
                )
            self.assertEqual(
                len(restarted_store.pending_outbox()),
                pending_before_replay,
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

                with patch.object(JournalStore, "get_event", return_value=tampered):
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
            with self.assertRaisesRegex(
                ValueError, "account scope|scope mismatch"
            ):
                _admit(
                    authority,
                    reservations,
                    checkpoint,
                    account_id="other-account",
                )
            self.assertEqual(reservations.version, 0)


if __name__ == "__main__":
    unittest.main()
