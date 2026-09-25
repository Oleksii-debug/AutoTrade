"""Durable exactly-once futures variation-margin adapter.

The pure futures state machine remains the economic authority.  This module only
binds accepted immutable settlement evidence and the resulting canonical
double-entry transaction to the existing JournalStore atomically.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from fractions import Fraction
from typing import Any, Mapping

from .accounting import JournalTransaction, canonical_transaction
from .futures import (
    FuturesError,
    FuturesSettlementEvidence,
    FuturesSettlementScope,
    InverseVariationMarginState,
    VariationMarginState,
    apply_inverse_variation_margin,
    apply_variation_margin,
    book_variation_margin,
    settle_and_book_inverse_variation_margin,
    settlement_identity_digest,
)
from .persistence import JournalStore, payload_digest


_AGGREGATE_TYPE = "FUTURES_VARIATION_MARGIN"
_EVENT_TYPE = "FuturesVariationMarginSettled"
_ACTOR = "autotrade-futures-settlement"


def _decimal_text(value: Decimal) -> str:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise FuturesError("durable decimal value must be finite Decimal")
    if value == 0:
        return "0"
    text = format(value.normalize(), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _fraction_payload(value: Fraction) -> dict[str, int]:
    if not isinstance(value, Fraction):
        raise FuturesError("durable inverse amount must be exact Fraction")
    return {"numerator": value.numerator, "denominator": value.denominator}


def _fraction_from_payload(value: object, *, name: str) -> Fraction:
    if not isinstance(value, Mapping):
        raise FuturesError(f"{name} must be an exact fraction object")
    numerator = value.get("numerator")
    denominator = value.get("denominator")
    if (
        isinstance(numerator, bool)
        or not isinstance(numerator, int)
        or isinstance(denominator, bool)
        or not isinstance(denominator, int)
        or denominator <= 0
    ):
        raise FuturesError(f"{name} fraction is invalid")
    return Fraction(numerator, denominator)


def _instant_text(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise FuturesError("settlement effective_at must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _instant_from_text(value: object) -> datetime:
    if not isinstance(value, str) or not value:
        raise FuturesError("durable settlement effective_at is required")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise FuturesError("durable settlement effective_at is invalid") from error
    if parsed.tzinfo is None:
        raise FuturesError("durable settlement effective_at must include timezone")
    return parsed.astimezone(timezone.utc)


def _scope_payload(scope: FuturesSettlementScope) -> dict[str, str | None]:
    return {
        "source_id": scope.source_id,
        "provider_id": scope.provider_id,
        "account_id": scope.account_id,
        "environment": scope.environment,
    }


def _evidence_payload(evidence: FuturesSettlementEvidence) -> dict[str, Any]:
    return {
        "settlement_id": evidence.settlement_id,
        "observation_id": evidence.observation_id,
        "supersedes_observation_id": evidence.supersedes_observation_id,
        "instrument_id": evidence.instrument_id,
        "instrument_version": evidence.instrument_version,
        "scope": _scope_payload(evidence.scope),
        "effective_at": _instant_text(evidence.effective_at),
        "sequence": evidence.sequence,
        "revision": evidence.revision,
        "settlement_price": _decimal_text(evidence.settlement_price),
        "price_currency": evidence.price_currency,
        "settlement_currency": evidence.settlement_currency,
    }


def _evidence_from_payload(value: object) -> FuturesSettlementEvidence:
    if not isinstance(value, Mapping):
        raise FuturesError("durable settlement evidence must be an object")
    scope_value = value.get("scope")
    if not isinstance(scope_value, Mapping):
        raise FuturesError("durable settlement scope must be an object")
    return FuturesSettlementEvidence(
        settlement_id=value.get("settlement_id"),
        observation_id=value.get("observation_id"),
        supersedes_observation_id=value.get("supersedes_observation_id"),
        instrument_id=value.get("instrument_id"),
        instrument_version=value.get("instrument_version"),
        scope=FuturesSettlementScope(
            source_id=scope_value.get("source_id"),
            provider_id=scope_value.get("provider_id"),
            account_id=scope_value.get("account_id"),
            environment=scope_value.get("environment"),
        ),
        effective_at=_instant_from_text(value.get("effective_at")),
        sequence=value.get("sequence"),
        revision=value.get("revision"),
        settlement_price=value.get("settlement_price"),
        price_currency=value.get("price_currency"),
        settlement_currency=value.get("settlement_currency"),
    )


def _durable_scope(
    state: VariationMarginState | InverseVariationMarginState,
) -> tuple[str, str]:
    version = state.contract.canonical_instrument
    if version is None:
        raise FuturesError("durable settlement requires canonical InstrumentVersion")
    scope = state.settlement_scope
    if scope.provider_id is None or scope.account_id is None or scope.environment is None:
        raise FuturesError(
            "durable provider settlement requires provider_id, account_id and environment"
        )
    identity = {
        "instrument_id": version.instrument_id,
        "instrument_version": version.version,
        "provider_id": scope.provider_id,
        "account_id": scope.account_id,
        "environment": scope.environment,
        "source_id": scope.source_id,
    }
    aggregate_id = "futures-vm:" + payload_digest(identity).removeprefix("sha256:")
    return aggregate_id, scope.environment


def _linear_event_payload(
    *,
    prior_price: Decimal,
    state: VariationMarginState,
    settlement: FuturesSettlementEvidence,
    delta: Decimal,
    transaction: JournalTransaction | None,
) -> dict[str, Any]:
    return {
        "schema_version": "1.0.0",
        "kind": "LINEAR",
        "settlement": _evidence_payload(settlement),
        "prior_settlement_price": _decimal_text(prior_price),
        "new_settlement_price": _decimal_text(state.last_settlement_price),
        "variation_margin_delta": _decimal_text(delta),
        "cumulative_variation_margin": _decimal_text(
            state.cumulative_variation_margin
        ),
        "transaction": (
            canonical_transaction(transaction) if transaction is not None else None
        ),
    }


def _inverse_event_payload(
    *,
    prior_price: Decimal,
    state: InverseVariationMarginState,
    settlement: FuturesSettlementEvidence,
    exact_delta: Fraction,
    settlement_quantum: Decimal,
    rounding: str,
    settled_cash: Decimal,
    transaction: JournalTransaction | None,
) -> dict[str, Any]:
    return {
        "schema_version": "1.0.0",
        "kind": "INVERSE",
        "settlement": _evidence_payload(settlement),
        "prior_settlement_price": _decimal_text(prior_price),
        "new_settlement_price": _decimal_text(state.last_settlement_price),
        "variation_margin_delta_exact": _fraction_payload(exact_delta),
        "cumulative_variation_margin_exact": _fraction_payload(
            state.cumulative_variation_margin
        ),
        "settlement_quantum": _decimal_text(settlement_quantum),
        "rounding": rounding,
        "settled_cash_delta": _decimal_text(settled_cash),
        "transaction": (
            canonical_transaction(transaction) if transaction is not None else None
        ),
    }


def _envelope(
    *,
    aggregate_id: str,
    version: int,
    event_id: str,
    environment: str,
    settlement: FuturesSettlementEvidence,
    payload: dict[str, Any],
) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    return {
        "event_id": event_id,
        "event_type": _EVENT_TYPE,
        "aggregate_type": _AGGREGATE_TYPE,
        "aggregate_id": aggregate_id,
        "aggregate_version": str(version),
        "environment": environment,
        "occurred_at": _instant_text(settlement.effective_at),
        "observed_at": _instant_text(settlement.effective_at),
        "committed_at": now,
        "payload": payload,
        "payload_hash": payload_digest(payload),
    }


def restore_linear_variation_margin(
    store: JournalStore,
    opening_state: VariationMarginState,
) -> VariationMarginState:
    """Rebuild and independently verify durable linear VM economics."""

    if not isinstance(store, JournalStore):
        raise TypeError("store must be JournalStore")
    if not isinstance(opening_state, VariationMarginState):
        raise TypeError("opening_state must be VariationMarginState")
    if opening_state.settlement_history:
        raise FuturesError("durable opening state must have empty settlement history")
    aggregate_id, _ = _durable_scope(opening_state)
    state = opening_state
    for expected_version, event in enumerate(
        store.load_events(_AGGREGATE_TYPE, aggregate_id), start=1
    ):
        if (
            event["event_type"] != _EVENT_TYPE
            or event["aggregate_version"] != expected_version
        ):
            raise FuturesError("durable futures settlement event sequence is invalid")
        payload = event["payload"]
        if not isinstance(payload, Mapping) or payload.get("kind") != "LINEAR":
            raise FuturesError("durable futures settlement kind mismatch")
        settlement = _evidence_from_payload(payload.get("settlement"))
        prior_price = state.last_settlement_price
        next_state, delta = apply_variation_margin(state, settlement)
        if next_state is state:
            raise FuturesError("durable journal contains duplicate settlement observation")
        transaction = (
            book_variation_margin(settlement=settlement, amount=delta)
            if delta != 0
            else None
        )
        expected_payload = _linear_event_payload(
            prior_price=prior_price,
            state=next_state,
            settlement=settlement,
            delta=delta,
            transaction=transaction,
        )
        if dict(payload) != expected_payload:
            raise FuturesError("durable linear settlement economics do not reproduce")
        state = next_state
    return state


def commit_linear_variation_margin(
    store: JournalStore,
    opening_state: VariationMarginState,
    settlement: FuturesSettlementEvidence,
) -> tuple[VariationMarginState, Decimal, JournalTransaction | None, bool]:
    """Atomically accept one linear settlement and its double-entry economics."""

    current = restore_linear_variation_margin(store, opening_state)
    next_state, delta = apply_variation_margin(current, settlement)
    if next_state is current:
        return current, Decimal("0"), None, False

    aggregate_id, environment = _durable_scope(opening_state)
    prior_price = current.last_settlement_price
    transaction = (
        book_variation_margin(settlement=settlement, amount=delta)
        if delta != 0
        else None
    )
    payload = _linear_event_payload(
        prior_price=prior_price,
        state=next_state,
        settlement=settlement,
        delta=delta,
        transaction=transaction,
    )
    digest = settlement_identity_digest(settlement)
    suffix = digest.removeprefix("sha256:")
    version = len(store.load_events(_AGGREGATE_TYPE, aggregate_id)) + 1
    envelope = _envelope(
        aggregate_id=aggregate_id,
        version=version,
        event_id=f"futures-vm-event:{suffix}",
        environment=environment,
        settlement=settlement,
        payload=payload,
    )
    request = {
        "schema_version": "1.0.0",
        "kind": "LINEAR",
        "aggregate_id": aggregate_id,
        "settlement": _evidence_payload(settlement),
    }
    _, inserted, _ = store.commit_command(
        command_id=f"futures-vm-command:{suffix}",
        actor=_ACTOR,
        environment=environment,
        idempotency_key=f"futures-settlement:{suffix}",
        request=request,
        result={"accepted": True, "payload_hash": payload_digest(payload)},
        state_version=version,
        events=[(envelope, None)],
    )
    rebuilt = restore_linear_variation_margin(store, opening_state)
    if not inserted:
        return rebuilt, Decimal("0"), None, False
    if rebuilt != next_state:
        raise FuturesError("durable linear settlement replay diverged after commit")
    return rebuilt, delta, transaction, True


def restore_inverse_variation_margin(
    store: JournalStore,
    opening_state: InverseVariationMarginState,
) -> InverseVariationMarginState:
    """Rebuild and independently verify durable inverse VM economics."""

    if not isinstance(store, JournalStore):
        raise TypeError("store must be JournalStore")
    if not isinstance(opening_state, InverseVariationMarginState):
        raise TypeError("opening_state must be InverseVariationMarginState")
    if opening_state.settlement_history:
        raise FuturesError("durable opening state must have empty settlement history")
    aggregate_id, _ = _durable_scope(opening_state)
    state = opening_state
    for expected_version, event in enumerate(
        store.load_events(_AGGREGATE_TYPE, aggregate_id), start=1
    ):
        if (
            event["event_type"] != _EVENT_TYPE
            or event["aggregate_version"] != expected_version
        ):
            raise FuturesError("durable futures settlement event sequence is invalid")
        payload = event["payload"]
        if not isinstance(payload, Mapping) or payload.get("kind") != "INVERSE":
            raise FuturesError("durable futures settlement kind mismatch")
        settlement = _evidence_from_payload(payload.get("settlement"))
        quantum = Decimal(str(payload.get("settlement_quantum")))
        rounding = payload.get("rounding")
        prior_price = state.last_settlement_price
        next_state, exact_delta = apply_inverse_variation_margin(state, settlement)
        if next_state is state:
            raise FuturesError("durable journal contains duplicate settlement observation")
        settled_cash, transaction = settle_and_book_inverse_variation_margin(
            settlement=settlement,
            contract=state.contract,
            exact_amount=exact_delta,
            settlement_quantum=quantum,
            rounding=rounding,
        )
        expected_payload = _inverse_event_payload(
            prior_price=prior_price,
            state=next_state,
            settlement=settlement,
            exact_delta=exact_delta,
            settlement_quantum=quantum,
            rounding=rounding,
            settled_cash=settled_cash,
            transaction=transaction,
        )
        if dict(payload) != expected_payload:
            raise FuturesError("durable inverse settlement economics do not reproduce")
        state = next_state
    return state


def commit_inverse_variation_margin(
    store: JournalStore,
    opening_state: InverseVariationMarginState,
    settlement: FuturesSettlementEvidence,
    *,
    settlement_quantum: Decimal | str,
    rounding: str = "HALF_EVEN",
) -> tuple[
    InverseVariationMarginState,
    Fraction,
    Decimal,
    JournalTransaction | None,
    bool,
]:
    """Atomically accept one inverse settlement and its rounded cash economics."""

    quantum = (
        settlement_quantum
        if isinstance(settlement_quantum, Decimal)
        else Decimal(str(settlement_quantum))
    )
    if not quantum.is_finite() or quantum <= 0:
        raise FuturesError("settlement_quantum must be positive")
    if rounding not in {"HALF_EVEN", "DOWN"}:
        raise FuturesError("unsupported rounding policy")

    current = restore_inverse_variation_margin(store, opening_state)
    next_state, exact_delta = apply_inverse_variation_margin(current, settlement)
    if next_state is current:
        return current, Fraction(0, 1), Decimal("0"), None, False

    aggregate_id, environment = _durable_scope(opening_state)
    prior_price = current.last_settlement_price
    settled_cash, transaction = settle_and_book_inverse_variation_margin(
        settlement=settlement,
        contract=current.contract,
        exact_amount=exact_delta,
        settlement_quantum=quantum,
        rounding=rounding,
    )
    payload = _inverse_event_payload(
        prior_price=prior_price,
        state=next_state,
        settlement=settlement,
        exact_delta=exact_delta,
        settlement_quantum=quantum,
        rounding=rounding,
        settled_cash=settled_cash,
        transaction=transaction,
    )
    digest = settlement_identity_digest(settlement)
    suffix = digest.removeprefix("sha256:")
    version = len(store.load_events(_AGGREGATE_TYPE, aggregate_id)) + 1
    envelope = _envelope(
        aggregate_id=aggregate_id,
        version=version,
        event_id=f"futures-vm-event:{suffix}",
        environment=environment,
        settlement=settlement,
        payload=payload,
    )
    request = {
        "schema_version": "1.0.0",
        "kind": "INVERSE",
        "aggregate_id": aggregate_id,
        "settlement": _evidence_payload(settlement),
        "settlement_quantum": _decimal_text(quantum),
        "rounding": rounding,
    }
    _, inserted, _ = store.commit_command(
        command_id=f"futures-vm-command:{suffix}",
        actor=_ACTOR,
        environment=environment,
        idempotency_key=f"futures-settlement:{suffix}",
        request=request,
        result={"accepted": True, "payload_hash": payload_digest(payload)},
        state_version=version,
        events=[(envelope, None)],
    )
    rebuilt = restore_inverse_variation_margin(store, opening_state)
    if not inserted:
        return rebuilt, Fraction(0, 1), Decimal("0"), None, False
    if rebuilt != next_state:
        raise FuturesError("durable inverse settlement replay diverged after commit")
    return rebuilt, exact_delta, settled_cash, transaction, True
