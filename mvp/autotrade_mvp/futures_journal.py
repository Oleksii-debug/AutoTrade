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
from uuid import UUID

from research.autotrade_research.artifacts.store import (
    ArtifactIntegrityError,
    ArtifactStore,
)
from research.autotrade_research.io.strict_json import strict_json_loads

from .accounting import (
    EconomicBook,
    JournalTransaction,
    canonical_transaction,
    posting,
)
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
from .persistence import JournalStore, canonical_json, payload_digest


_AGGREGATE_TYPE = "FUTURES_VARIATION_MARGIN"
_EVENT_TYPE = "FuturesVariationMarginSettled"
_ACTOR = "autotrade-futures-settlement"
_SETTLEMENT_EVIDENCE_MEDIA_TYPE = (
    "application/vnd.autotrade.futures-settlement-evidence+json"
)
_SETTLEMENT_EVIDENCE_TYPE = "AUTOTRADE_FUTURES_SETTLEMENT_EVIDENCE"
_SETTLEMENT_EVIDENCE_SCHEMA_VERSION = 1


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
        "evidence_ref": evidence.evidence_ref,
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
        evidence_ref=value.get("evidence_ref"),
    )


def _immutable_settlement_evidence_ref(value: object) -> tuple[str, str, str]:
    if not isinstance(value, str) or not value.strip():
        raise FuturesError(
            "provider settlement requires immutable artifact evidence_ref"
        )
    reference = value.strip()
    marker = "@sha256:"
    if not reference.startswith("artifact:") or marker not in reference:
        raise FuturesError(
            "settlement evidence_ref must bind artifact UUID and SHA-256 digest"
        )
    artifact_id, digest = reference[len("artifact:"):].split(marker, 1)
    try:
        artifact_id = str(UUID(artifact_id))
    except (ValueError, TypeError, AttributeError) as error:
        raise FuturesError("settlement evidence artifact identity must be a UUID") from error
    if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
        raise FuturesError(
            "settlement evidence_ref must use canonical lowercase SHA-256"
        )
    canonical = f"artifact:{artifact_id}@sha256:{digest}"
    if reference != canonical:
        raise FuturesError("settlement evidence_ref must be canonical")
    return artifact_id, digest, canonical


def provider_settlement_evidence_receipt(
    evidence: FuturesSettlementEvidence,
) -> dict[str, Any]:
    """Canonical preserved-provider receipt, excluding its self-reference."""

    if not isinstance(evidence, FuturesSettlementEvidence):
        raise TypeError("evidence must be FuturesSettlementEvidence")
    settlement = _evidence_payload(evidence)
    settlement.pop("evidence_ref", None)
    return {
        "schema_version": _SETTLEMENT_EVIDENCE_SCHEMA_VERSION,
        "evidence_type": _SETTLEMENT_EVIDENCE_TYPE,
        "settlement": settlement,
    }


def provider_settlement_evidence_metadata(
    evidence: FuturesSettlementEvidence,
) -> dict[str, object]:
    if not isinstance(evidence, FuturesSettlementEvidence):
        raise TypeError("evidence must be FuturesSettlementEvidence")
    scope = evidence.scope
    if scope.provider_id is None or scope.account_id is None or scope.environment is None:
        raise FuturesError("provider settlement evidence requires provider/account/environment")
    return {
        "evidence_type": _SETTLEMENT_EVIDENCE_TYPE,
        "provider_id": scope.provider_id,
        "account_id": scope.account_id,
        "environment": scope.environment,
        "source_id": scope.source_id,
        "instrument_id": evidence.instrument_id,
        "instrument_version": evidence.instrument_version,
        "observation_id": evidence.observation_id,
    }


def _verify_provider_settlement_evidence(
    evidence: FuturesSettlementEvidence,
    artifact_store: ArtifactStore,
) -> str:
    if not isinstance(artifact_store, ArtifactStore):
        raise FuturesError(
            "durable provider settlement requires trusted ArtifactStore"
        )
    artifact_id, digest, canonical_ref = _immutable_settlement_evidence_ref(
        evidence.evidence_ref
    )
    try:
        manifest = artifact_store.load_manifest(artifact_id)
        manifest_hash = manifest.get("manifest_hash")
        if (
            not isinstance(manifest_hash, str)
            or len(manifest_hash) != 71
            or not manifest_hash.startswith("sha256:")
        ):
            raise ArtifactIntegrityError(
                "settlement evidence manifest lacks integrity binding"
            )
        if manifest.get("sha256") != f"sha256:{digest}":
            raise ArtifactIntegrityError(
                "settlement evidence digest does not match manifest"
            )
        if manifest.get("media_type") != _SETTLEMENT_EVIDENCE_MEDIA_TYPE:
            raise ArtifactIntegrityError(
                "settlement evidence has unsupported media type"
            )
        if manifest.get("metadata") != provider_settlement_evidence_metadata(evidence):
            raise ArtifactIntegrityError(
                "settlement evidence manifest metadata does not match financial scope"
            )
        rights = manifest.get("rights")
        if not isinstance(rights, dict) or rights.get("storage") is not True:
            raise ArtifactIntegrityError(
                "settlement evidence manifest lacks storage provenance"
            )
        raw = artifact_store.read_bytes(artifact_id)
        receipt = strict_json_loads(raw.decode("utf-8"))
    except (
        ArtifactIntegrityError,
        FileNotFoundError,
        UnicodeError,
        ValueError,
        TypeError,
    ) as error:
        raise FuturesError("settlement provider evidence verification failed") from error
    expected = provider_settlement_evidence_receipt(evidence)
    if receipt != expected:
        raise FuturesError(
            "settlement provider evidence does not match supplied economics"
        )
    if raw != canonical_json(expected).encode("utf-8"):
        raise FuturesError("settlement provider evidence must use canonical JSON bytes")
    return canonical_ref


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


def variation_margin_aggregate_id(
    state: VariationMarginState | InverseVariationMarginState,
) -> str:
    """Return the deterministic durable aggregate identity for one account contract."""

    return _durable_scope(state)[0]


def _transaction_from_payload(value: object) -> JournalTransaction | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise FuturesError("durable transaction must be an object")
    postings_value = value.get("postings")
    if not isinstance(postings_value, list):
        raise FuturesError("durable transaction postings must be a list")
    transaction = JournalTransaction(
        transaction_id=value.get("transaction_id"),
        cause_event_id=value.get("cause_event_id"),
        reverses_transaction_id=value.get("reverses_transaction_id"),
        postings=tuple(
            posting(
                item.get("ledger_account"),
                item.get("asset_or_currency"),
                item.get("signed_amount"),
            )
            for item in postings_value
            if isinstance(item, Mapping)
        ),
    )
    if len(transaction.postings) != len(postings_value):
        raise FuturesError("durable transaction posting is not an object")
    if canonical_transaction(transaction) != dict(value):
        raise FuturesError("durable canonical transaction does not reproduce")
    return transaction


def rebuild_variation_margin_book(
    store: JournalStore,
    opening_state: VariationMarginState | InverseVariationMarginState,
) -> EconomicBook:
    """Rebuild the canonical double-entry projection from durable settlement events."""

    if isinstance(opening_state, VariationMarginState):
        restore_linear_variation_margin(store, opening_state)
    elif isinstance(opening_state, InverseVariationMarginState):
        restore_inverse_variation_margin(store, opening_state)
    else:
        raise TypeError("opening_state must be a variation-margin state")

    aggregate_id, _ = _durable_scope(opening_state)
    book = EconomicBook()
    for event in store.load_events(_AGGREGATE_TYPE, aggregate_id):
        payload = event["payload"]
        if not isinstance(payload, Mapping):
            raise FuturesError("durable futures settlement payload must be an object")
        transaction = _transaction_from_payload(payload.get("transaction"))
        if transaction is not None:
            book.append(transaction)
    return book


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
