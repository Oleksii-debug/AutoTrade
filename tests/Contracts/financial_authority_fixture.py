"""Canonical financial-authority fixture for provider contract tests.

Provider contract tests intentionally exercise durable PAPER/LIVE submission
provenance.  They therefore must obtain send authority through the same
AuthorityService admission/risk/reservation path as production rather than
minting an allow-all PreparedSubmissionAuthorityCheck.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
from typing import Mapping

from mvp.autotrade_mvp.authority import (
    AuthoritativeRiskSnapshot,
    AuthorityPolicy,
    AuthorityService,
)
from mvp.autotrade_mvp.dispatch import PreparedSubmissionAuthorityCheck
from mvp.autotrade_mvp.durable_reservations import DurableReservationBook
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.reconciliation import (
    ResourceAvailabilityEvidence,
    SnapshotConsistencyEvidence,
    reconcile_account,
)
from mvp.autotrade_mvp.reconciliation_journal import record_reconciliation_checkpoint
from mvp.autotrade_mvp.risk import RiskContext, RiskIntent, RiskPolicy


_CONTRACT_INSTRUMENT_ID = "11111111-1111-4111-8111-111111111111"
_EVIDENCE_DIMENSIONS = (
    "PORTFOLIO",
    "MARKET",
    "MARGIN",
    "POLICY",
    "RECONCILIATION",
    "CAPABILITY",
    "BORROW",
    "STRESS",
    "FX",
    "FACTORS",
    "LIQUIDITY",
    "LIQUIDATION",
    "SETTLEMENT",
    "OPTION_LIFECYCLE",
    "FUTURES_LIFECYCLE",
)


def _utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("contract fixture time must include timezone")
    return parsed.astimezone(timezone.utc)


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def canonical_financial_dispatch_guard(
    store: JournalStore,
    *,
    provider_id: str,
    provider_environment: str,
    account_id: str,
    environment: str,
    capability_snapshot_id: str,
    intent_id: str,
    submission_scope: Mapping[str, object],
    now: str,
) -> tuple[PreparedSubmissionAuthorityCheck, str]:
    """Return a genuine AuthorityService-issued guard and its bound intent hash."""

    provider = provider_id.strip().upper()
    provider_env = provider_environment.strip().upper()
    account = account_id.strip()
    runtime_environment = environment.strip().upper()
    capability = capability_snapshot_id.strip()
    intent = intent_id.strip()
    point = _utc(now)
    query_started = _utc_text(point - timedelta(seconds=60))
    query_completed = _utc_text(point - timedelta(seconds=30))
    coverage_start = _utc_text(point - timedelta(seconds=90))
    valid_until = _utc_text(point + timedelta(minutes=5))
    policy_valid_from = _utc_text(point - timedelta(days=1))
    policy_expires_at = _utc_text(point + timedelta(days=1))
    key = sha256(
        f"{provider}|{provider_env}|{account}|{runtime_environment}|{intent}".encode(
            "utf-8"
        )
    ).hexdigest()[:20]
    policy_id = f"contract-policy-{key}"
    admission_id = f"contract-admission-{key}"
    reservation_id = f"contract-reservation-{key}"
    command_id = f"contract-command-{key}"
    intent_hash = "sha256:" + sha256(f"contract-intent|{key}".encode("utf-8")).hexdigest()

    authority = AuthorityService(store)
    authority.register_policy(
        AuthorityPolicy.create(
            policy_id=policy_id,
            account_id=account,
            environments={runtime_environment},
            instruments={(_CONTRACT_INSTRUMENT_ID, 1)},
            actions={"ORDER.SUBMIT"},
            max_notional="1000",
            valid_from=policy_valid_from,
            expires_at=policy_expires_at,
            autonomous=True,
            protection_only=False,
        )
    )

    reconciliation = reconcile_account(
        provider_id=provider,
        account_id=account,
        environment=runtime_environment,
        provider_environment=provider_env,
        local_cash={"USD": "1000"},
        provider_cash={"USD": "1000"},
        local_positions={},
        provider_positions={},
        local_execution_ids=(),
        provider_fills=(),
        snapshot_consistency=SnapshotConsistencyEvidence(
            provider_id=provider,
            account_id=account,
            environment=runtime_environment,
            provider_environment=provider_env,
            mode="ATOMIC",
            query_started_at=query_started,
            query_completed_at=query_completed,
        ),
        coverage_start=coverage_start,
        coverage_end=now,
        pagination_complete=True,
        provider_activity_provider_id=provider,
        provider_activity_account_id=account,
        resource_availability=ResourceAvailabilityEvidence(
            provider_id=provider,
            account_id=account,
            environment=runtime_environment,
            provider_environment=provider_env,
            snapshot_id=f"contract-snapshot-{key}",
            query_started_at=query_started,
            query_completed_at=query_completed,
            valid_until=valid_until,
            available_resources={"CASH:USD": "1000"},
            provider_as_of=query_completed,
            evidence_refs=(f"provider:contract-snapshot-{key}",),
        ),
    )
    checkpoint = record_reconciliation_checkpoint(
        store,
        reconciliation_id=f"contract-reconciliation-{key}",
        result=reconciliation,
        observed_at=query_completed,
        host_id="provider-contract-fixture",
        owner_epoch="1",
    )

    risk_context = RiskContext.create(
        state_version=1,
        equity="1000",
        positions={},
        marks={"CONTRACT": "100"},
        reserved_position_delta={},
        daily_pnl="0",
        drawdown_fraction="0",
        market_data_age_seconds="1",
        fx_age_seconds={"USD": "1"},
        margin_headroom="1",
        capability_allowed=True,
        borrow_available=True,
        stress_scenarios=({"CONTRACT": "-0.10"},),
    )
    risk_policy = RiskPolicy.create(
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

    def resolve(request):
        return AuthoritativeRiskSnapshot(
            context=risk_context,
            risk_policy=risk_policy,
            account_id=request.account_id,
            environment=request.environment,
            provider_id=request.provider_id,
            provider_environment=request.provider_environment,
            instrument_version=request.instrument_version,
            capability_snapshot_id=request.capability_snapshot_id,
            reconciliation_checkpoint_event_id=request.reconciliation_checkpoint_event_id,
            journal_sequence_cut=request.journal_sequence_cut,
            reservation_version=request.reservation_version,
            reservation_state_digest=request.reservation_state_digest,
            authority_policy_id=request.authority_policy_id,
            authority_policy_version=request.authority_policy_version,
            evaluated_at=request.evaluated_at,
            valid_until=valid_until,
            evidence_refs={
                dimension: "sha256:"
                + sha256(f"{dimension}|{key}".encode("utf-8")).hexdigest()
                for dimension in _EVIDENCE_DIMENSIONS
            },
        )

    authority.risk_authority_resolver = resolve
    reservations = DurableReservationBook(
        store,
        environment=runtime_environment,
        account_id=account,
    )
    admitted = authority.admit(
        command_id=command_id,
        idempotency_key=command_id,
        admission_id=admission_id,
        policy_id=policy_id,
        intent_id=intent,
        intent_hash=intent_hash,
        account_id=account,
        environment=runtime_environment,
        instrument_id=_CONTRACT_INSTRUMENT_ID,
        instrument_version=1,
        action="ORDER.SUBMIT",
        notional="100",
        capability_snapshot_id=capability,
        risk_intent=RiskIntent.create(
            symbol="CONTRACT",
            side="BUY",
            quantity="1",
            price="100",
            expected_state_version=1,
        ),
        risk_context=risk_context,
        risk_policy=risk_policy,
        risk_valid_until=valid_until,
        reservation_book=reservations,
        reservation_id=reservation_id,
        reservation_requirements={"CASH:USD": "100"},
        reservation_available={"CASH:USD": "1000"},
        reservation_checkpoint_event_id=checkpoint["event_id"],
        reservation_provider_id=provider,
        reservation_provider_environment=provider_env,
        reservation_max_age_seconds="300",
        now=now,
    )
    if admitted.outcome != "ADMITTED":
        raise AssertionError(f"contract fixture financial admission failed: {admitted}")

    guard = authority.dispatch_guard(
        admitted.admission_id,
        account_id=admitted.account_id,
        environment=admitted.environment,
        instrument_id=admitted.instrument_version.instrument_id,
        instrument_version=admitted.instrument_version.version,
        action=admitted.action,
        capability_snapshot_id=admitted.capability_snapshot_id,
        submission_scope=submission_scope,
    )
    return guard, admitted.intent_hash
