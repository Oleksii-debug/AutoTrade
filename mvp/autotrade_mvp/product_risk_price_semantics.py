"""Product-owned composition of Bybit price semantics into risk snapshots.

This module is deliberately not a risk-state source and does not grant PAPER/LIVE
admission. It composes an already canonical Bybit prepared request with the
existing causal InstrumentRegistry/ArtifactStore price-rule authority, then
provides an immutable binding that a genuine product-owned risk resolver can
attach to its AuthoritativeRiskSnapshot.

Generic callable risk resolvers remain SIMULATION-only in AuthorityService.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from research.autotrade_research.artifacts.store import ArtifactStore

from . import bybit_v5 as _bybit_module
from . import instruments as _instruments_module
from .authority import AuthoritativeRiskSnapshot, RiskAuthorityRequest
from .bybit_v5 import (
    BybitPreparedSubmission,
    require_canonical_bybit_prepared_submission,
)
from .instruments import (
    AuthenticatedPriceSemanticsEvidence,
    InstrumentRegistry,
    InstrumentVersion,
    authenticated_price_semantics_evidence,
)


class ProductRiskPriceSemanticsError(PermissionError):
    """Prepared request and admitted risk/instrument authority do not compose."""


def _install_product_risk_price_semantics_authority():
    """Capture the product-composition TCB outside caller-writable module state."""

    _ERROR_TYPE = ProductRiskPriceSemanticsError
    _REQUEST_TYPE = RiskAuthorityRequest
    _VERSION_TYPE = InstrumentVersion
    _DATETIME_TYPE = datetime
    _TIMEZONE_VALUE = timezone
    _DECIMAL_TYPE = Decimal
    _INVALID_OPERATION_TYPE = InvalidOperation
    _TYPE = type
    _DICT = dict
    _GETATTR = getattr
    _STR = str
    _INT = int
    _BOOL = bool
    _VALUE_ERROR = ValueError
    _TYPE_ERROR = TypeError
    _REPLACE = replace
    _REPLACE_CODE = replace.__code__
    _BYBIT_MODULE = _bybit_module
    _INSTRUMENTS_MODULE = _instruments_module
    _SNAPSHOT_TYPE = AuthoritativeRiskSnapshot
    _PREPARED_TYPE = BybitPreparedSubmission
    _REGISTRY_TYPE = InstrumentRegistry
    _ARTIFACT_STORE_TYPE = ArtifactStore
    _REQUIRE_PREPARED = require_canonical_bybit_prepared_submission
    _REQUIRE_PREPARED_CODE = _REQUIRE_PREPARED.__code__
    _PRICE_EVIDENCE_TYPE = AuthenticatedPriceSemanticsEvidence
    _AUTHENTICATED_EVIDENCE = authenticated_price_semantics_evidence
    _AUTHENTICATED_EVIDENCE_CODE = _AUTHENTICATED_EVIDENCE.__code__
    _AT_KNOWN = InstrumentRegistry.at_known
    _AT_KNOWN_CODE = _AT_KNOWN.__code__
    _METADATA_BINDING = _VERSION_TYPE.metadata_evidence_binding
    _METADATA_BINDING_CODE = _METADATA_BINDING.__code__
    _VALIDATE_QUANTITY = _VERSION_TYPE.validate_quantity
    _VALIDATE_QUANTITY_CODE = _VALIDATE_QUANTITY.__code__
    
    
    def _require_module_authority() -> None:
        if (
            ProductRiskPriceSemanticsError is not _ERROR_TYPE
            or RiskAuthorityRequest is not _REQUEST_TYPE
            or AuthoritativeRiskSnapshot is not _SNAPSHOT_TYPE
            or BybitPreparedSubmission is not _PREPARED_TYPE
            or InstrumentRegistry is not _REGISTRY_TYPE
            or ArtifactStore is not _ARTIFACT_STORE_TYPE
            or InstrumentVersion is not _VERSION_TYPE
            or datetime is not _DATETIME_TYPE
            or timezone is not _TIMEZONE_VALUE
            or Decimal is not _DECIMAL_TYPE
            or InvalidOperation is not _INVALID_OPERATION_TYPE
            or type is not _TYPE
            or dict is not _DICT
            or getattr is not _GETATTR
            or str is not _STR
            or int is not _INT
            or bool is not _BOOL
            or ValueError is not _VALUE_ERROR
            or TypeError is not _TYPE_ERROR
            or replace is not _REPLACE
            or replace.__code__ is not _REPLACE_CODE
            or _bybit_module is not _BYBIT_MODULE
            or _instruments_module is not _INSTRUMENTS_MODULE
            or (
            _BYBIT_MODULE.BybitPreparedSubmission is not _PREPARED_TYPE
            or _BYBIT_MODULE.require_canonical_bybit_prepared_submission
            is not _REQUIRE_PREPARED
            or _REQUIRE_PREPARED.__code__ is not _REQUIRE_PREPARED_CODE
            )
        ):
            raise _ERROR_TYPE(
                "Bybit prepared-request authority changed"
            )
        if (
            _INSTRUMENTS_MODULE.InstrumentRegistry is not _REGISTRY_TYPE
            or _INSTRUMENTS_MODULE.InstrumentVersion is not InstrumentVersion
            or _INSTRUMENTS_MODULE.AuthenticatedPriceSemanticsEvidence
            is not _PRICE_EVIDENCE_TYPE
            or _INSTRUMENTS_MODULE.authenticated_price_semantics_evidence
            is not _AUTHENTICATED_EVIDENCE
            or _AUTHENTICATED_EVIDENCE.__code__ is not _AUTHENTICATED_EVIDENCE_CODE
            or _REGISTRY_TYPE.at_known is not _AT_KNOWN
            or _AT_KNOWN.__code__ is not _AT_KNOWN_CODE
            or _VERSION_TYPE.metadata_evidence_binding is not _METADATA_BINDING
            or _METADATA_BINDING.__code__ is not _METADATA_BINDING_CODE
            or _VERSION_TYPE.validate_quantity is not _VALIDATE_QUANTITY
            or _VALIDATE_QUANTITY.__code__ is not _VALIDATE_QUANTITY_CODE
        ):
            raise _ERROR_TYPE(
                "instrument price-semantics authority changed"
            )
    
    
    def _instant(value: str, *, name: str) -> datetime:
        if _TYPE(value) is not _STR or not value or value != value.strip():
            raise _ERROR_TYPE(f"{name} must be canonical text")
        raw = value[:-1] + "+00:00" if value.endswith("Z") else value
        try:
            point = _DATETIME_TYPE.fromisoformat(raw)
        except _VALUE_ERROR as error:
            raise _ERROR_TYPE(
                f"{name} must be an ISO-8601 instant"
            ) from error
        if point.tzinfo is None or point.utcoffset() is None:
            raise _ERROR_TYPE(f"{name} must include timezone")
        return point.astimezone(_TIMEZONE_VALUE.utc)
    
    
    def _exact_decimal(value: object, *, name: str) -> Decimal:
        if _TYPE(value) not in {_STR, _INT, _DECIMAL_TYPE} or _TYPE(value) is _BOOL:
            raise _ERROR_TYPE(
                f"{name} must use exact decimal-compatible input"
            )
        try:
            result = value if _TYPE(value) is _DECIMAL_TYPE else _DECIMAL_TYPE(value)
        except (_INVALID_OPERATION_TYPE, _VALUE_ERROR) as error:
            raise _ERROR_TYPE(f"{name} is invalid") from error
        if not result.is_finite():
            raise _ERROR_TYPE(f"{name} must be finite")
        return result
    
    
    def _instrument_ref(request: RiskAuthorityRequest) -> str:
        identity = request.instrument_version
        instrument_id = _GETATTR(identity, "instrument_id", None)
        version = _GETATTR(identity, "version", None)
        if _TYPE(instrument_id) is not _STR or _TYPE(version) is not _INT or version < 1:
            raise _ERROR_TYPE(
                "risk request instrument identity is non-canonical"
            )
        return f"{instrument_id}@{version}"
    
    
    @dataclass(frozen=True, slots=True)
    class ProductRiskPriceSemanticsBinding:
        """Immutable evidence linking one prepared request to the admitted risk cut."""
    
        provider_id: str
        account_id: str
        environment: str
        provider_environment: str
        entity_policy_id: str
        instrument_version: str
        capability_snapshot_id: str
        evaluated_at: str
        side: str
        quantity: Decimal
        risk_price: Decimal
        reduce_only: bool
        order_type: str
        prepared_body_sha256: str
        price_semantics_digest: str
        instrument_evidence_binding: str
    
        # This is an immutable value, not a capability. bind_snapshot() recomputes
        # the canonical composition and requires exact value equality before use.
    
    
    class ProductRiskPriceSemanticsComposer:
        """Bind canonical prepared-order semantics to causal instrument authority."""
    
        __slots__ = ("__registry", "__artifact_store")
    
        def __init_subclass__(cls, **_kwargs) -> None:
            raise _TYPE_ERROR("ProductRiskPriceSemanticsComposer is sealed")
    
        def __init__(
            self,
            registry: InstrumentRegistry,
            artifact_store: ArtifactStore,
        ) -> None:
            if _TYPE(registry) is not _REGISTRY_TYPE:
                raise _TYPE_ERROR("registry must be exact InstrumentRegistry")
            if _TYPE(artifact_store) is not _ARTIFACT_STORE_TYPE:
                raise _TYPE_ERROR("artifact_store must be exact ArtifactStore")
            _require_module_authority()
            self.__registry = registry
            self.__artifact_store = artifact_store
    
        def compose(
            self,
            request: RiskAuthorityRequest,
            prepared_request: BybitPreparedSubmission,
        ) -> ProductRiskPriceSemanticsBinding:
            _require_module_authority()
            if _TYPE(request) is not _REQUEST_TYPE:
                raise _TYPE_ERROR("request must be exact RiskAuthorityRequest")
            if _TYPE(prepared_request) is not _PREPARED_TYPE:
                raise _TYPE_ERROR(
                    "prepared_request must be exact BybitPreparedSubmission"
                )
            _REQUIRE_PREPARED(prepared_request)
    
            if request.environment not in {"PAPER", "LIVE"}:
                raise _ERROR_TYPE(
                    "product price-semantics composition is PAPER/LIVE only"
                )
            if request.provider_id != "BYBIT":
                raise _ERROR_TYPE(
                    "prepared Bybit request cannot satisfy another provider"
                )
            if _TYPE(request.provider_environment) is not _STR:
                raise _ERROR_TYPE(
                    "production risk request requires provider_environment"
                )
            if _TYPE(request.entity_policy_id) is not _STR or not request.entity_policy_id:
                raise _ERROR_TYPE(
                    "production risk request requires entity_policy_id"
                )
            if prepared_request.account_id != request.account_id:
                raise _ERROR_TYPE(
                    "prepared request account differs from risk request"
                )
            if prepared_request.environment != request.environment:
                raise _ERROR_TYPE(
                    "prepared request environment differs from risk request"
                )
            if prepared_request.provider_environment != request.provider_environment:
                raise _ERROR_TYPE(
                    "prepared provider environment differs from risk request"
                )
            if prepared_request.capability_snapshot_id != request.capability_snapshot_id:
                raise _ERROR_TYPE(
                    "prepared capability differs from risk request"
                )
    
            instrument_ref = _instrument_ref(request)
            if prepared_request.instrument_version != instrument_ref:
                raise _ERROR_TYPE(
                    "prepared instrument version differs from risk request"
                )
    
            body = _DICT(prepared_request.body)
            intent = request.risk_intent
            if body.get("symbol") != intent.symbol:
                raise _ERROR_TYPE(
                    "prepared symbol differs from risk intent"
                )
            expected_side = "Buy" if intent.side == "BUY" else "Sell"
            if body.get("side") != expected_side:
                raise _ERROR_TYPE(
                    "prepared side differs from risk intent"
                )
            if _exact_decimal(body.get("qty"), name="prepared quantity") != intent.quantity:
                raise _ERROR_TYPE(
                    "prepared quantity differs from risk intent"
                )
            prepared_reduce_only = body.get("reduceOnly", False)
            if (
                _TYPE(prepared_reduce_only) is not _BOOL
                or prepared_reduce_only != intent.reduce_only
            ):
                raise _ERROR_TYPE(
                    "prepared reduce-only semantics differ from risk intent"
                )
    
            wire_order_type = body.get("orderType")
            if wire_order_type == "Limit":
                order_type = "LIMIT"
                if "price" not in body:
                    raise _ERROR_TYPE(
                        "prepared LIMIT request is missing price"
                    )
                wire_price = body["price"]
                if _exact_decimal(wire_price, name="prepared LIMIT price") != intent.price:
                    raise _ERROR_TYPE(
                        "prepared LIMIT price differs from already-risked price"
                    )
                semantic_price: Decimal | str | int | None = wire_price
            elif wire_order_type == "Market":
                order_type = "MARKET"
                if "price" in body:
                    raise _ERROR_TYPE(
                        "prepared MARKET request must not carry wire price"
                    )
                semantic_price = None
            else:
                raise _ERROR_TYPE(
                    "prepared order type has no canonical price-semantics contract"
                )
    
            point = _instant(request.evaluated_at, name="evaluated_at")
            registry = self.__registry
            artifact_store = self.__artifact_store
    
            # Parent #1979 owns causal price-rule authentication and returns the exact
            # metadata binding it used.  Consume that evidence object rather than
            # reconstructing a parallel price authority here.
            price_evidence = _AUTHENTICATED_EVIDENCE(
                registry,
                artifact_store,
                instrument_version=instrument_ref,
                evaluated_at=point,
                provider_id=request.provider_id,
                entity_policy_id=request.entity_policy_id,
                side=intent.side,
                order_type=order_type,
                price=semantic_price,
            )
            if _TYPE(price_evidence) is not _PRICE_EVIDENCE_TYPE:
                raise _ERROR_TYPE(
                    "instrument price-semantics evidence is non-canonical"
                )
            expected_evidence = (
                instrument_ref,
                request.provider_id,
                body.get("symbol"),
                request.entity_policy_id,
                intent.side,
                order_type,
                "EXACT_ADMITTED_PRICE" if order_type == "LIMIT" else "NO_WIRE_PRICE",
            )
            actual_evidence = (
                price_evidence.instrument_version,
                price_evidence.provider_id,
                price_evidence.provider_symbol,
                price_evidence.entity_policy_id,
                price_evidence.side,
                price_evidence.order_type,
                price_evidence.price_constraint,
            )
            if actual_evidence != expected_evidence:
                raise _ERROR_TYPE(
                    "authenticated price semantics differ from prepared risk scope"
                )
    
            # Quantity rules are owned by the same causal instrument version. Re-read
            # that version after price evidence composition and require its metadata
            # binding to be the exact one returned by #1979. This catches an in-process
            # rule mutation/advance across the bounded composition while validating
            # quantity step/min/max without rounding.
            selected = _AT_KNOWN(
                registry,
                request.instrument_version.instrument_id,
                point,
                knowledge_cutoff=point,
                artifact_store=artifact_store,
            )
            if _TYPE(selected) is not _VERSION_TYPE:
                raise _ERROR_TYPE(
                    "instrument registry returned non-canonical version"
                )
            selected_binding = _METADATA_BINDING(selected)
            if (
                selected.version != request.instrument_version.version
                or selected_binding != price_evidence.instrument_metadata_binding
            ):
                raise _ERROR_TYPE(
                    "instrument authority changed during price-semantics composition"
                )
            if selected.provider_symbol != body.get("symbol"):
                raise _ERROR_TYPE(
                    "prepared symbol differs from causal instrument authority"
                )
            validated_quantity = _VALIDATE_QUANTITY(selected, intent.quantity)
            if validated_quantity != intent.quantity:
                raise _ERROR_TYPE(
                    "canonical quantity validation changed admitted quantity"
                )
    
            return ProductRiskPriceSemanticsBinding(
                provider_id=request.provider_id,
                account_id=request.account_id,
                environment=request.environment,
                provider_environment=request.provider_environment,
                entity_policy_id=request.entity_policy_id,
                instrument_version=instrument_ref,
                capability_snapshot_id=request.capability_snapshot_id,
                evaluated_at=request.evaluated_at,
                side=intent.side,
                quantity=intent.quantity,
                risk_price=intent.price,
                reduce_only=intent.reduce_only,
                order_type=order_type,
                prepared_body_sha256=prepared_request.body_sha256,
                price_semantics_digest=price_evidence.digest,
                instrument_evidence_binding=price_evidence.instrument_metadata_binding,
            )
    
        def bind_snapshot(
            self,
            request: RiskAuthorityRequest,
            snapshot: AuthoritativeRiskSnapshot,
            binding: ProductRiskPriceSemanticsBinding,
            prepared_request: BybitPreparedSubmission,
        ) -> AuthoritativeRiskSnapshot:
            """Attach semantics to an already-authoritative base snapshot.
    
            This method does not authenticate the base snapshot. AuthorityService
            still requires a genuine product-owned production resolver before
            PAPER/LIVE admission can consume the result.
            """
    
            _require_module_authority()
            if _TYPE(request) is not _REQUEST_TYPE:
                raise _TYPE_ERROR("request must be exact RiskAuthorityRequest")
            if _TYPE(snapshot) is not _SNAPSHOT_TYPE:
                raise _TYPE_ERROR("snapshot must be exact AuthoritativeRiskSnapshot")
            if _TYPE(binding) is not ProductRiskPriceSemanticsBinding:
                raise _TYPE_ERROR(
                    "binding must be exact ProductRiskPriceSemanticsBinding"
                )
            if _TYPE(prepared_request) is not _PREPARED_TYPE:
                raise _TYPE_ERROR(
                    "prepared_request must be exact BybitPreparedSubmission"
                )
            _REQUIRE_PREPARED(prepared_request)
            if prepared_request.body_sha256 != binding.prepared_body_sha256:
                raise _ERROR_TYPE(
                    "prepared request differs from issued price-semantics binding"
                )
    
            # A binding is never authority by possession. Re-run the same sealed
            # composition at the immutable evaluated cut and require every bound
            # value to match. This makes direct construction or post-issue mutation
            # harmless unless it is exactly equivalent to canonical composition.
            canonical_binding = self.compose(request, prepared_request)
            if binding != canonical_binding:
                raise _ERROR_TYPE(
                    "price-semantics binding differs from fresh canonical composition"
                )
    
            expected = (
                request.provider_id,
                request.account_id,
                request.environment,
                request.provider_environment,
                request.entity_policy_id,
                _instrument_ref(request),
                request.capability_snapshot_id,
                request.evaluated_at,
                request.risk_intent.side,
                request.risk_intent.quantity,
                request.risk_intent.price,
                request.risk_intent.reduce_only,
            )
            actual = (
                binding.provider_id,
                binding.account_id,
                binding.environment,
                binding.provider_environment,
                binding.entity_policy_id,
                binding.instrument_version,
                binding.capability_snapshot_id,
                binding.evaluated_at,
                binding.side,
                binding.quantity,
                binding.risk_price,
                binding.reduce_only,
            )
            if actual != expected:
                raise _ERROR_TYPE(
                    "price-semantics binding belongs to another risk request"
                )
    
            snapshot_scope = (
                snapshot.provider_id,
                snapshot.account_id,
                snapshot.environment,
                snapshot.provider_environment,
                snapshot.entity_policy_id,
                f"{snapshot.instrument_version.instrument_id}@"
                f"{snapshot.instrument_version.version}",
                snapshot.capability_snapshot_id,
                snapshot.evaluated_at,
            )
            request_scope = (
                request.provider_id,
                request.account_id,
                request.environment,
                request.provider_environment,
                request.entity_policy_id,
                _instrument_ref(request),
                request.capability_snapshot_id,
                request.evaluated_at,
            )
            if snapshot_scope != request_scope:
                raise _ERROR_TYPE(
                    "base risk snapshot differs from price-semantics request cut"
                )
            if snapshot.price_semantics_digest is not None:
                raise _ERROR_TYPE(
                    "base risk snapshot already carries price semantics"
                )
    
            refs = _DICT(snapshot.evidence_refs.items())
            existing = refs.get("INSTRUMENT")
            if existing is not None and existing != binding.instrument_evidence_binding:
                raise _ERROR_TYPE(
                    "base risk snapshot INSTRUMENT evidence differs from canonical binding"
                )
            refs["INSTRUMENT"] = binding.instrument_evidence_binding
            return _REPLACE(
                snapshot,
                price_semantics_digest=binding.price_semantics_digest,
                evidence_refs=refs,
            )

    return ProductRiskPriceSemanticsBinding, ProductRiskPriceSemanticsComposer


(
    ProductRiskPriceSemanticsBinding,
    ProductRiskPriceSemanticsComposer,
) = _install_product_risk_price_semantics_authority()
del _install_product_risk_price_semantics_authority
