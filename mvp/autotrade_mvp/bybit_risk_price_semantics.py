"""Bind authenticated instrument price semantics into one Bybit production risk cut.

This module is a narrow WP-16 composition seam.  It creates no risk engine,
instrument registry, provider adapter, dispatcher or send permission.  It
cross-binds one already canonical Bybit prepared request to the exact
RiskAuthorityRequest/AuthoritativeRiskSnapshot cut and lets the existing
instrument authority mint the price-semantics identity.

AuthorityService still rejects generic PAPER/LIVE callable risk resolvers.  A
future product-owned resolver may call this seam while composing its canonical
owner graph; callers cannot use this module to turn a snapshot into send
authority.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from research.autotrade_research.artifacts import ArtifactStore

from .authority import AuthoritativeRiskSnapshot, RiskAuthorityRequest
from .bybit_v5 import (
    BybitPreparedSubmission,
    guarded_order_projection,
    require_canonical_bybit_prepared_submission,
)
from .instruments import (
    AuthenticatedPriceSemanticsEvidence,
    InstrumentRegistry,
    InstrumentRegistryError,
    authenticated_price_semantics_evidence,
)
from .provider_core import ProviderCoreError


class BybitRiskPriceSemanticsError(PermissionError):
    """Raised when one prepared Bybit request differs from the admitted risk cut."""


def _install_bybit_risk_price_semantics_composer():
    request_type = RiskAuthorityRequest
    snapshot_type = AuthoritativeRiskSnapshot
    prepared_type = BybitPreparedSubmission
    registry_type = InstrumentRegistry
    artifact_store_type = ArtifactStore
    evidence_type = AuthenticatedPriceSemanticsEvidence

    replace_value = replace
    replace_code = replace.__code__
    datetime_type = datetime
    timezone_value = timezone
    decimal_type = Decimal
    invalid_operation_type = InvalidOperation

    prepared_verifier = require_canonical_bybit_prepared_submission
    prepared_verifier_code = prepared_verifier.__code__
    prepared_projection = guarded_order_projection
    prepared_projection_code = prepared_projection.__code__
    price_composer = authenticated_price_semantics_evidence
    price_composer_code = price_composer.__code__

    def require_executable_authority() -> None:
        if (
            RiskAuthorityRequest is not request_type
            or AuthoritativeRiskSnapshot is not snapshot_type
            or BybitPreparedSubmission is not prepared_type
            or InstrumentRegistry is not registry_type
            or ArtifactStore is not artifact_store_type
            or AuthenticatedPriceSemanticsEvidence is not evidence_type
            or replace is not replace_value
            or replace.__code__ is not replace_code
            or datetime is not datetime_type
            or timezone is not timezone_value
            or Decimal is not decimal_type
            or InvalidOperation is not invalid_operation_type
            or require_canonical_bybit_prepared_submission is not prepared_verifier
            or require_canonical_bybit_prepared_submission.__code__
            is not prepared_verifier_code
            or guarded_order_projection is not prepared_projection
            or guarded_order_projection.__code__ is not prepared_projection_code
            or authenticated_price_semantics_evidence is not price_composer
            or authenticated_price_semantics_evidence.__code__
            is not price_composer_code
        ):
            raise BybitRiskPriceSemanticsError(
                "Bybit risk price-semantics executable authority changed"
            )

    def decimal_from_wire(value: object, *, name: str) -> Decimal:
        if type(value) is not str or not value:
            raise BybitRiskPriceSemanticsError(
                f"prepared Bybit {name} must be canonical decimal text"
            )
        try:
            parsed = decimal_type(value)
        except (invalid_operation_type, ValueError) as error:
            raise BybitRiskPriceSemanticsError(
                f"prepared Bybit {name} must be canonical decimal text"
            ) from error
        if not parsed.is_finite() or parsed <= 0:
            raise BybitRiskPriceSemanticsError(
                f"prepared Bybit {name} must be positive finite decimal text"
            )
        return parsed

    def compose(
        request: RiskAuthorityRequest,
        snapshot: AuthoritativeRiskSnapshot,
        *,
        prepared_request: BybitPreparedSubmission,
        instrument_registry: InstrumentRegistry,
        artifact_store: ArtifactStore,
    ) -> AuthoritativeRiskSnapshot:
        require_executable_authority()
        if type(request) is not request_type:
            raise TypeError("request must be exact RiskAuthorityRequest")
        if type(snapshot) is not snapshot_type:
            raise TypeError("snapshot must be exact AuthoritativeRiskSnapshot")
        if type(prepared_request) is not prepared_type:
            raise TypeError("prepared_request must be exact BybitPreparedSubmission")
        if type(instrument_registry) is not registry_type:
            raise TypeError("instrument_registry must be exact InstrumentRegistry")
        if type(artifact_store) is not artifact_store_type:
            raise TypeError("artifact_store must be the canonical ArtifactStore")

        try:
            canonical_request = replace_value(request)
            canonical_snapshot = replace_value(snapshot)
        except (TypeError, ValueError, RuntimeError, PermissionError) as error:
            raise BybitRiskPriceSemanticsError(
                "risk cut cannot be canonically revalidated"
            ) from error

        if canonical_request.environment not in {"PAPER", "LIVE"}:
            raise BybitRiskPriceSemanticsError(
                "Bybit authenticated price semantics is a PAPER/LIVE product seam"
            )
        if canonical_request.provider_id != "BYBIT":
            raise BybitRiskPriceSemanticsError(
                "Bybit price semantics requires BYBIT financial scope"
            )
        if (
            type(canonical_request.provider_environment) is not str
            or type(canonical_request.entity_policy_id) is not str
            or type(canonical_request.instrument_family) is not str
        ):
            raise BybitRiskPriceSemanticsError(
                "production risk cut lacks provider-domain RiskPolicy scope"
            )

        request_policy = canonical_request.resolved_risk_policy
        snapshot_policy = canonical_snapshot.resolved_risk_policy
        request_policy_evidence = (
            request_policy.evidence_payload if request_policy is not None else None
        )
        snapshot_policy_evidence = (
            snapshot_policy.evidence_payload if snapshot_policy is not None else None
        )
        expected_cut = (
            request_policy_evidence,
            canonical_request.account_id,
            canonical_request.environment,
            canonical_request.provider_id,
            canonical_request.instrument_version,
            canonical_request.capability_snapshot_id,
            canonical_request.reconciliation_checkpoint_event_id,
            canonical_request.journal_sequence_cut,
            canonical_request.reservation_version,
            canonical_request.reservation_state_digest,
            canonical_request.authority_policy_id,
            canonical_request.authority_policy_version,
            canonical_request.evaluated_at,
            canonical_request.provider_environment,
            canonical_request.entity_policy_id,
            canonical_request.instrument_family,
        )
        actual_cut = (
            snapshot_policy_evidence,
            canonical_snapshot.account_id,
            canonical_snapshot.environment,
            canonical_snapshot.provider_id,
            canonical_snapshot.instrument_version,
            canonical_snapshot.capability_snapshot_id,
            canonical_snapshot.reconciliation_checkpoint_event_id,
            canonical_snapshot.journal_sequence_cut,
            canonical_snapshot.reservation_version,
            canonical_snapshot.reservation_state_digest,
            canonical_snapshot.authority_policy_id,
            canonical_snapshot.authority_policy_version,
            canonical_snapshot.evaluated_at,
            canonical_snapshot.provider_environment,
            canonical_snapshot.entity_policy_id,
            canonical_snapshot.instrument_family,
        )
        if actual_cut != expected_cut:
            raise BybitRiskPriceSemanticsError(
                "risk snapshot differs from exact financial request cut"
            )

        if (
            BybitPreparedSubmission is not prepared_type
            or require_canonical_bybit_prepared_submission is not prepared_verifier
            or require_canonical_bybit_prepared_submission.__code__
            is not prepared_verifier_code
            or guarded_order_projection is not prepared_projection
            or guarded_order_projection.__code__ is not prepared_projection_code
        ):
            raise BybitRiskPriceSemanticsError(
                "Bybit prepared-request authority changed"
            )
        try:
            prepared_verifier(prepared_request)
            projection = prepared_projection(prepared_request)
        except (ProviderCoreError, TypeError, ValueError) as error:
            raise BybitRiskPriceSemanticsError(
                "prepared Bybit request lacks canonical issuance provenance"
            ) from error
        if type(projection) is not type(projection):
            raise AssertionError("unreachable")
        body = projection.get("body")
        if type(body) is not dict:
            raise BybitRiskPriceSemanticsError(
                "canonical Bybit prepared body is unavailable"
            )

        instrument_ref = (
            f"{canonical_request.instrument_version.instrument_id}"
            f"@{canonical_request.instrument_version.version}"
        )
        projection_cut = (
            projection.get("account_id"),
            projection.get("environment"),
            projection.get("provider_environment"),
            projection.get("capability_snapshot_id"),
            prepared_request.instrument_version,
        )
        expected_projection_cut = (
            canonical_request.account_id,
            canonical_request.environment,
            canonical_request.provider_environment,
            canonical_request.capability_snapshot_id,
            instrument_ref,
        )
        if projection_cut != expected_projection_cut:
            raise BybitRiskPriceSemanticsError(
                "prepared Bybit provider scope differs from exact risk cut"
            )

        wire_side = body.get("side")
        side = {"Buy": "BUY", "Sell": "SELL"}.get(wire_side)
        if side is None or side != canonical_request.risk_intent.side:
            raise BybitRiskPriceSemanticsError(
                "prepared Bybit side differs from evaluated risk intent"
            )

        wire_quantity = decimal_from_wire(body.get("qty"), name="quantity")
        if wire_quantity != canonical_request.risk_intent.quantity:
            raise BybitRiskPriceSemanticsError(
                "prepared Bybit quantity differs from evaluated risk intent"
            )

        wire_reduce_only = body.get("reduceOnly", False)
        if type(wire_reduce_only) is not bool:
            raise BybitRiskPriceSemanticsError(
                "prepared Bybit reduce-only semantics is non-canonical"
            )
        if wire_reduce_only != canonical_request.risk_intent.reduce_only:
            raise BybitRiskPriceSemanticsError(
                "prepared Bybit reduce-only differs from evaluated risk intent"
            )

        wire_order_type = body.get("orderType")
        order_type = {"Limit": "LIMIT", "Market": "MARKET"}.get(wire_order_type)
        if order_type is None:
            raise BybitRiskPriceSemanticsError(
                "prepared Bybit order type is unsupported"
            )

        if order_type == "LIMIT":
            wire_price = body.get("price")
            prepared_price = decimal_from_wire(wire_price, name="price")
            if prepared_price != canonical_request.risk_intent.price:
                raise BybitRiskPriceSemanticsError(
                    "prepared Bybit LIMIT price differs from evaluated risk intent"
                )
            semantic_price: object | None = wire_price
        else:
            if "price" in body:
                raise BybitRiskPriceSemanticsError(
                    "prepared Bybit MARKET request unexpectedly carries price"
                )
            semantic_price = None

        evaluated_at = canonical_request.evaluated_at
        try:
            point = datetime_type.fromisoformat(
                evaluated_at.replace("Z", "+00:00")
            )
        except ValueError as error:
            raise BybitRiskPriceSemanticsError(
                "risk evaluated_at is not canonical ISO time"
            ) from error
        if point.tzinfo is None:
            raise BybitRiskPriceSemanticsError(
                "risk evaluated_at must be timezone-aware"
            )
        point = point.astimezone(timezone_value.utc)

        if (
            authenticated_price_semantics_evidence is not price_composer
            or authenticated_price_semantics_evidence.__code__
            is not price_composer_code
        ):
            raise BybitRiskPriceSemanticsError(
                "instrument price-semantics composer authority changed"
            )
        try:
            evidence = price_composer(
                instrument_registry,
                artifact_store,
                instrument_version=instrument_ref,
                evaluated_at=point,
                provider_id=canonical_request.provider_id,
                entity_policy_id=canonical_request.entity_policy_id,
                side=side,
                order_type=order_type,
                price=semantic_price,
            )
        except (InstrumentRegistryError, TypeError, ValueError) as error:
            raise BybitRiskPriceSemanticsError(
                "authenticated instrument price semantics cannot be composed"
            ) from error
        if type(evidence) is not evidence_type:
            raise BybitRiskPriceSemanticsError(
                "instrument price-semantics evidence is non-canonical"
            )
        if body.get("symbol") != evidence.provider_symbol:
            raise BybitRiskPriceSemanticsError(
                "prepared Bybit symbol differs from authenticated instrument metadata"
            )

        if (
            canonical_snapshot.price_semantics_digest is not None
            and canonical_snapshot.price_semantics_digest != evidence.digest
        ):
            raise BybitRiskPriceSemanticsError(
                "risk snapshot carries different price-semantics identity"
            )
        refs = dict(canonical_snapshot.evidence_refs.items())
        existing_instrument = refs.get("INSTRUMENT")
        if (
            existing_instrument is not None
            and existing_instrument != evidence.instrument_metadata_binding
        ):
            raise BybitRiskPriceSemanticsError(
                "risk snapshot carries different instrument metadata binding"
            )
        refs["INSTRUMENT"] = evidence.instrument_metadata_binding

        try:
            bound = replace_value(
                canonical_snapshot,
                evidence_refs=refs,
                price_semantics_digest=evidence.digest,
            )
        except (TypeError, ValueError, RuntimeError, PermissionError) as error:
            raise BybitRiskPriceSemanticsError(
                "price semantics cannot be bound into authoritative risk snapshot"
            ) from error
        if type(bound) is not snapshot_type:
            raise BybitRiskPriceSemanticsError(
                "price-semantics composition returned non-canonical snapshot"
            )
        return bound

    return compose


compose_bybit_risk_price_semantics = _install_bybit_risk_price_semantics_composer()
del _install_bybit_risk_price_semantics_composer
