"""Product-owned composition of Bybit price semantics into risk snapshots.

This module is deliberately not a risk-state source and does not grant PAPER/LIVE
admission. It composes an already canonical Bybit prepared request with the
existing causal InstrumentRegistry/ArtifactStore price-rule authority, then
provides an immutable binding that a genuine product-owned risk resolver can
attach to its AuthoritativeRiskSnapshot.

Generic callable risk resolvers remain SIMULATION-only in AuthorityService.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
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
    InstrumentRegistry,
    InstrumentVersion,
    authenticated_price_semantics_digest,
)


class ProductRiskPriceSemanticsError(PermissionError):
    """Prepared request and admitted risk/instrument authority do not compose."""


_FACTORY_TOKEN = object()
_REQUEST_TYPE = RiskAuthorityRequest
_SNAPSHOT_TYPE = AuthoritativeRiskSnapshot
_PREPARED_TYPE = BybitPreparedSubmission
_REGISTRY_TYPE = InstrumentRegistry
_ARTIFACT_STORE_TYPE = ArtifactStore
_REQUIRE_PREPARED = require_canonical_bybit_prepared_submission
_REQUIRE_PREPARED_CODE = _REQUIRE_PREPARED.__code__
_AUTHENTICATED_DIGEST = authenticated_price_semantics_digest
_AUTHENTICATED_DIGEST_CODE = _AUTHENTICATED_DIGEST.__code__
_AT_KNOWN = InstrumentRegistry.at_known
_AT_KNOWN_CODE = _AT_KNOWN.__code__
_METADATA_BINDING = InstrumentVersion.metadata_evidence_binding
_METADATA_BINDING_CODE = _METADATA_BINDING.__code__


def _require_module_authority() -> None:
    if (
        _bybit_module.BybitPreparedSubmission is not _PREPARED_TYPE
        or _bybit_module.require_canonical_bybit_prepared_submission
        is not _REQUIRE_PREPARED
        or _REQUIRE_PREPARED.__code__ is not _REQUIRE_PREPARED_CODE
    ):
        raise ProductRiskPriceSemanticsError(
            "Bybit prepared-request authority changed"
        )
    if (
        _instruments_module.InstrumentRegistry is not _REGISTRY_TYPE
        or _instruments_module.InstrumentVersion is not InstrumentVersion
        or _instruments_module.authenticated_price_semantics_digest
        is not _AUTHENTICATED_DIGEST
        or _AUTHENTICATED_DIGEST.__code__ is not _AUTHENTICATED_DIGEST_CODE
        or _REGISTRY_TYPE.at_known is not _AT_KNOWN
        or _AT_KNOWN.__code__ is not _AT_KNOWN_CODE
        or InstrumentVersion.metadata_evidence_binding is not _METADATA_BINDING
        or _METADATA_BINDING.__code__ is not _METADATA_BINDING_CODE
    ):
        raise ProductRiskPriceSemanticsError(
            "instrument price-semantics authority changed"
        )


def _instant(value: str, *, name: str) -> datetime:
    if type(value) is not str or not value or value != value.strip():
        raise ProductRiskPriceSemanticsError(f"{name} must be canonical text")
    raw = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        point = datetime.fromisoformat(raw)
    except ValueError as error:
        raise ProductRiskPriceSemanticsError(
            f"{name} must be an ISO-8601 instant"
        ) from error
    if point.tzinfo is None or point.utcoffset() is None:
        raise ProductRiskPriceSemanticsError(f"{name} must include timezone")
    return point.astimezone(timezone.utc)


def _exact_decimal(value: object, *, name: str) -> Decimal:
    if type(value) not in {str, int, Decimal} or type(value) is bool:
        raise ProductRiskPriceSemanticsError(
            f"{name} must use exact decimal-compatible input"
        )
    try:
        result = value if type(value) is Decimal else Decimal(value)
    except (InvalidOperation, ValueError) as error:
        raise ProductRiskPriceSemanticsError(f"{name} is invalid") from error
    if not result.is_finite():
        raise ProductRiskPriceSemanticsError(f"{name} must be finite")
    return result


def _instrument_ref(request: RiskAuthorityRequest) -> str:
    identity = request.instrument_version
    instrument_id = getattr(identity, "instrument_id", None)
    version = getattr(identity, "version", None)
    if type(instrument_id) is not str or type(version) is not int or version < 1:
        raise ProductRiskPriceSemanticsError(
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
    _factory_token: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._factory_token is not _FACTORY_TOKEN:
            raise ProductRiskPriceSemanticsError(
                "price-semantics binding requires canonical product composition"
            )


class ProductRiskPriceSemanticsComposer:
    """Bind canonical prepared-order semantics to causal instrument authority."""

    __slots__ = ("__registry", "__artifact_store")

    def __init_subclass__(cls, **_kwargs) -> None:
        raise TypeError("ProductRiskPriceSemanticsComposer is sealed")

    def __init__(
        self,
        registry: InstrumentRegistry,
        artifact_store: ArtifactStore,
    ) -> None:
        if type(registry) is not _REGISTRY_TYPE:
            raise TypeError("registry must be exact InstrumentRegistry")
        if type(artifact_store) is not _ARTIFACT_STORE_TYPE:
            raise TypeError("artifact_store must be exact ArtifactStore")
        _require_module_authority()
        self.__registry = registry
        self.__artifact_store = artifact_store

    def compose(
        self,
        request: RiskAuthorityRequest,
        prepared_request: BybitPreparedSubmission,
    ) -> ProductRiskPriceSemanticsBinding:
        _require_module_authority()
        if type(request) is not _REQUEST_TYPE:
            raise TypeError("request must be exact RiskAuthorityRequest")
        if type(prepared_request) is not _PREPARED_TYPE:
            raise TypeError(
                "prepared_request must be exact BybitPreparedSubmission"
            )
        _REQUIRE_PREPARED(prepared_request)

        if request.environment not in {"PAPER", "LIVE"}:
            raise ProductRiskPriceSemanticsError(
                "product price-semantics composition is PAPER/LIVE only"
            )
        if request.provider_id != "BYBIT":
            raise ProductRiskPriceSemanticsError(
                "prepared Bybit request cannot satisfy another provider"
            )
        if type(request.provider_environment) is not str:
            raise ProductRiskPriceSemanticsError(
                "production risk request requires provider_environment"
            )
        if type(request.entity_policy_id) is not str or not request.entity_policy_id:
            raise ProductRiskPriceSemanticsError(
                "production risk request requires entity_policy_id"
            )
        if prepared_request.account_id != request.account_id:
            raise ProductRiskPriceSemanticsError(
                "prepared request account differs from risk request"
            )
        if prepared_request.environment != request.environment:
            raise ProductRiskPriceSemanticsError(
                "prepared request environment differs from risk request"
            )
        if prepared_request.provider_environment != request.provider_environment:
            raise ProductRiskPriceSemanticsError(
                "prepared provider environment differs from risk request"
            )
        if prepared_request.capability_snapshot_id != request.capability_snapshot_id:
            raise ProductRiskPriceSemanticsError(
                "prepared capability differs from risk request"
            )

        instrument_ref = _instrument_ref(request)
        if prepared_request.instrument_version != instrument_ref:
            raise ProductRiskPriceSemanticsError(
                "prepared instrument version differs from risk request"
            )

        body = dict(prepared_request.body)
        intent = request.risk_intent
        if body.get("symbol") != intent.symbol:
            raise ProductRiskPriceSemanticsError(
                "prepared symbol differs from risk intent"
            )
        expected_side = "Buy" if intent.side == "BUY" else "Sell"
        if body.get("side") != expected_side:
            raise ProductRiskPriceSemanticsError(
                "prepared side differs from risk intent"
            )
        if _exact_decimal(body.get("qty"), name="prepared quantity") != intent.quantity:
            raise ProductRiskPriceSemanticsError(
                "prepared quantity differs from risk intent"
            )
        prepared_reduce_only = body.get("reduceOnly", False)
        if (
            type(prepared_reduce_only) is not bool
            or prepared_reduce_only != intent.reduce_only
        ):
            raise ProductRiskPriceSemanticsError(
                "prepared reduce-only semantics differ from risk intent"
            )

        wire_order_type = body.get("orderType")
        if wire_order_type == "Limit":
            order_type = "LIMIT"
            if "price" not in body:
                raise ProductRiskPriceSemanticsError(
                    "prepared LIMIT request is missing price"
                )
            wire_price = body["price"]
            if _exact_decimal(wire_price, name="prepared LIMIT price") != intent.price:
                raise ProductRiskPriceSemanticsError(
                    "prepared LIMIT price differs from already-risked price"
                )
            semantic_price: Decimal | str | int | None = wire_price
        elif wire_order_type == "Market":
            order_type = "MARKET"
            if "price" in body:
                raise ProductRiskPriceSemanticsError(
                    "prepared MARKET request must not carry wire price"
                )
            semantic_price = None
        else:
            raise ProductRiskPriceSemanticsError(
                "prepared order type has no canonical price-semantics contract"
            )

        point = _instant(request.evaluated_at, name="evaluated_at")
        registry = self.__registry
        artifact_store = self.__artifact_store

        # The digest helper performs causal evidence authentication. Surrounding
        # reads make an instrument-rule advance during composition fail closed.
        before = _AT_KNOWN(
            registry,
            request.instrument_version.instrument_id,
            point,
            knowledge_cutoff=point,
            artifact_store=artifact_store,
        )
        if type(before) is not InstrumentVersion:
            raise ProductRiskPriceSemanticsError(
                "instrument registry returned non-canonical version"
            )
        before_binding = _METADATA_BINDING(before)

        digest = _AUTHENTICATED_DIGEST(
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

        after = _AT_KNOWN(
            registry,
            request.instrument_version.instrument_id,
            point,
            knowledge_cutoff=point,
            artifact_store=artifact_store,
        )
        if type(after) is not InstrumentVersion:
            raise ProductRiskPriceSemanticsError(
                "instrument registry returned non-canonical version"
            )
        after_binding = _METADATA_BINDING(after)
        if (
            before.version != request.instrument_version.version
            or after.version != request.instrument_version.version
            or before_binding != after_binding
        ):
            raise ProductRiskPriceSemanticsError(
                "instrument authority changed during price-semantics composition"
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
            price_semantics_digest=digest,
            instrument_evidence_binding=after_binding,
            _factory_token=_FACTORY_TOKEN,
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
        if type(request) is not _REQUEST_TYPE:
            raise TypeError("request must be exact RiskAuthorityRequest")
        if type(snapshot) is not _SNAPSHOT_TYPE:
            raise TypeError("snapshot must be exact AuthoritativeRiskSnapshot")
        if type(binding) is not ProductRiskPriceSemanticsBinding:
            raise TypeError(
                "binding must be exact ProductRiskPriceSemanticsBinding"
            )
        if type(prepared_request) is not _PREPARED_TYPE:
            raise TypeError(
                "prepared_request must be exact BybitPreparedSubmission"
            )
        if object.__getattribute__(binding, "_factory_token") is not _FACTORY_TOKEN:
            raise ProductRiskPriceSemanticsError(
                "price-semantics binding provenance changed"
            )
        _REQUIRE_PREPARED(prepared_request)
        if prepared_request.body_sha256 != binding.prepared_body_sha256:
            raise ProductRiskPriceSemanticsError(
                "prepared request differs from issued price-semantics binding"
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
            raise ProductRiskPriceSemanticsError(
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
            raise ProductRiskPriceSemanticsError(
                "base risk snapshot differs from price-semantics request cut"
            )
        if snapshot.price_semantics_digest is not None:
            raise ProductRiskPriceSemanticsError(
                "base risk snapshot already carries price semantics"
            )

        refs = dict(snapshot.evidence_refs.items())
        existing = refs.get("INSTRUMENT")
        if existing is not None and existing != binding.instrument_evidence_binding:
            raise ProductRiskPriceSemanticsError(
                "base risk snapshot INSTRUMENT evidence differs from canonical binding"
            )
        refs["INSTRUMENT"] = binding.instrument_evidence_binding
        return replace(
            snapshot,
            price_semantics_digest=binding.price_semantics_digest,
            evidence_refs=refs,
        )
