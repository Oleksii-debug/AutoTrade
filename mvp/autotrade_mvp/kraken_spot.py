"""Kraken Spot adapter contract and guarded REST-order projection.

The Spot and Derivatives API families are intentionally not merged. This module
owns deterministic cash-spot request/response semantics only. Credential
resolution, nonce allocation and network I/O stay in the shared guarded
provider transport; financial authority remains outside this module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Mapping
from uuid import NAMESPACE_URL, UUID, uuid5
from hashlib import sha256
import json
import re

from .capabilities import CapabilitySnapshot
from .provider_core import ProviderResponseObservation, ProviderSubmissionObservation, Surface
from .reconciliation import CoverageSurfaceEvidence, ProviderFillEvidence


class KrakenSpotAdapterError(ValueError):
    """Raised when a Kraken Spot request cannot be represented safely."""


KRAKEN_SPOT_DOCS = MappingProxyType(
    {
        "order_contract": "https://docs.kraken.com/api-reference/trading/add-order",
        "authentication": "https://docs.kraken.com/exchange/guides/rest/authentication",
    }
)

_FREE_CLIENT_ID = re.compile(r"^[\x21-\x7e]{1,18}$")
_ORDER_TYPES = frozenset({"MARKET", "LIMIT"})
_SIDES = frozenset({"BUY", "SELL"})
_TIME_IN_FORCE = frozenset({"GTC", "IOC"})
_ENVIRONMENTS = frozenset({"REPLAY", "SIMULATION", "PAPER", "LIVE"})


def _text(value: str, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise KrakenSpotAdapterError(f"{name} is required")
    return value.strip()


def _environment(value: str) -> str:
    environment = _text(value, name="environment").upper()
    if environment not in _ENVIRONMENTS:
        raise KrakenSpotAdapterError(
            "environment must be REPLAY, SIMULATION, PAPER, or LIVE"
        )
    return environment


def _decimal(value, *, name: str, positive: bool = False) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise KrakenSpotAdapterError(f"{name} must use exact decimal input")
    try:
        result = value if isinstance(value, Decimal) else Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as error:
        raise KrakenSpotAdapterError(f"{name} must be a finite decimal") from error
    if not result.is_finite():
        raise KrakenSpotAdapterError(f"{name} must be a finite decimal")
    if positive and result <= 0:
        raise KrakenSpotAdapterError(f"{name} must be positive")
    return result


def _instant(value: datetime, *, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise KrakenSpotAdapterError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _decimal_text(value: Decimal) -> str:
    return format(value, "f")


def validate_spot_client_order_id(value: str) -> str:
    """Validate Kraken Spot cl_ord_id without inventing a provider format.

    Kraken accepts canonical UUID, 32 hexadecimal UUID text, or free-form ASCII
    text up to 18 characters. AutoTrade's guarded dispatcher must use its
    deterministic UUID client-order format here: truncating the normal token to
    18 characters would violate the dispatch identity entropy floor.
    """

    client_id = _text(value, name="client_order_id")
    try:
        parsed = UUID(client_id)
    except (ValueError, TypeError, AttributeError):
        parsed = None
    if parsed is not None and (str(parsed) == client_id.lower() or parsed.hex == client_id.lower()):
        return client_id
    if re.fullmatch(r"[0-9a-fA-F]{32}", client_id):
        return client_id
    if _FREE_CLIENT_ID.fullmatch(client_id) is None:
        raise KrakenSpotAdapterError(
            "client_order_id must be UUID/32-hex or printable ASCII of at most 18 characters"
        )
    return client_id


@dataclass(frozen=True)
class KrakenSpotOrderIntent:
    instrument_version: str
    pair: str
    side: str
    order_type: str
    volume: Decimal
    price: Decimal | None = None
    time_in_force: str = "GTC"
    post_only: bool = False

    @classmethod
    def create(
        cls,
        *,
        instrument_version: str,
        pair: str,
        side: str,
        order_type: str,
        volume,
        price=None,
        time_in_force: str = "GTC",
        post_only: bool = False,
    ) -> "KrakenSpotOrderIntent":
        side_value = _text(side, name="side").upper()
        order_value = _text(order_type, name="order_type").upper()
        tif = _text(time_in_force, name="time_in_force").upper()
        if side_value not in _SIDES:
            raise KrakenSpotAdapterError("side must be BUY or SELL")
        if order_value not in _ORDER_TYPES:
            raise KrakenSpotAdapterError("only MARKET and LIMIT are admitted by this Spot foundation")
        if tif not in _TIME_IN_FORCE:
            raise KrakenSpotAdapterError("only GTC and IOC are admitted by this Spot foundation")
        if type(post_only) is not bool:
            raise KrakenSpotAdapterError("post_only must be boolean")
        volume_value = _decimal(volume, name="volume", positive=True)
        price_value = None if price is None else _decimal(price, name="price", positive=True)
        if order_value == "LIMIT" and price_value is None:
            raise KrakenSpotAdapterError("price is required for a limit order")
        if order_value == "MARKET" and price_value is not None:
            raise KrakenSpotAdapterError("price must be omitted for a market order")
        if post_only and order_value != "LIMIT":
            raise KrakenSpotAdapterError("post_only is valid only for limit orders")
        if post_only and tif == "IOC":
            raise KrakenSpotAdapterError("post_only and IOC are mutually exclusive")
        return cls(
            instrument_version=_text(instrument_version, name="instrument_version"),
            pair=_text(pair, name="pair").upper(),
            side=side_value,
            order_type=order_value,
            volume=volume_value,
            price=price_value,
            time_in_force=tif,
            post_only=post_only,
        )


_KRAKEN_SPOT_PREPARED_REQUEST_FACTORY_TOKEN = object()


@dataclass(frozen=True)
class KrakenSpotPreparedRequest:
    endpoint: str
    body: Mapping[str, object]
    account_id: str
    environment: str
    capability_snapshot_id: str
    documentation_refs: tuple[str, ...]
    instrument_version: str
    body_sha256: str = field(init=False)
    _factory_token: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._factory_token is not _KRAKEN_SPOT_PREPARED_REQUEST_FACTORY_TOKEN:
            raise KrakenSpotAdapterError(
                "KrakenSpotPreparedRequest must be created by the canonical preparation factory"
            )
        endpoint = _text(self.endpoint, name="endpoint")
        if endpoint != "/0/private/AddOrder":
            raise KrakenSpotAdapterError(
                "prepared endpoint must be the canonical Kraken Spot AddOrder path"
            )
        if not isinstance(self.body, Mapping):
            raise TypeError("body must be a mapping")
        body = dict(self.body)
        required = {
            "pair",
            "type",
            "ordertype",
            "volume",
            "cl_ord_id",
            "timeinforce",
        }
        optional = {"price", "oflags"}
        if not required <= set(body) or set(body) - required - optional:
            raise KrakenSpotAdapterError(
                "prepared AddOrder body does not match the canonical request shape"
            )
        client_id = validate_spot_client_order_id(body.get("cl_ord_id"))
        pair = _text(body.get("pair"), name="pair")
        if pair != pair.upper():
            raise KrakenSpotAdapterError("prepared pair must be canonical uppercase text")
        side = _text(body.get("type"), name="type")
        if side not in {"buy", "sell"}:
            raise KrakenSpotAdapterError("prepared type must be buy or sell")
        order_type = _text(body.get("ordertype"), name="ordertype")
        if order_type not in {"market", "limit"}:
            raise KrakenSpotAdapterError("prepared ordertype must be market or limit")
        tif = _text(body.get("timeinforce"), name="timeinforce")
        if tif not in {"GTC", "IOC"}:
            raise KrakenSpotAdapterError("prepared timeinforce must be GTC or IOC")
        volume_text = _text(body.get("volume"), name="volume")
        if _decimal_text(_decimal(volume_text, name="volume", positive=True)) != volume_text:
            raise KrakenSpotAdapterError("prepared volume must be exact canonical decimal text")
        price_text = body.get("price")
        if order_type == "limit":
            price = _text(price_text, name="price")
            if _decimal_text(_decimal(price, name="price", positive=True)) != price:
                raise KrakenSpotAdapterError("prepared price must be exact canonical decimal text")
        elif price_text is not None:
            raise KrakenSpotAdapterError("market request cannot contain price")
        flags = body.get("oflags")
        if flags is not None and flags != "post":
            raise KrakenSpotAdapterError("unsupported prepared AddOrder flags")
        if flags == "post" and (order_type != "limit" or tif == "IOC"):
            raise KrakenSpotAdapterError("prepared post-only order shape is invalid")
        try:
            rendered_body = json.dumps(
                body,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            )
        except (TypeError, ValueError) as error:
            raise KrakenSpotAdapterError(
                "prepared AddOrder body must be canonical JSON"
            ) from error
        account = _text(self.account_id, name="account_id")
        environment = _environment(self.environment)
        capability_snapshot_id = _text(
            self.capability_snapshot_id,
            name="capability_snapshot_id",
        )
        instrument_version = _text(
            self.instrument_version,
            name="instrument_version",
        )
        if not isinstance(self.documentation_refs, tuple):
            raise TypeError("documentation_refs must be a tuple")
        refs = tuple(
            _text(value, name="documentation_ref")
            for value in self.documentation_refs
        )
        if not refs:
            raise KrakenSpotAdapterError("documentation_refs must not be empty")
        body["cl_ord_id"] = client_id
        object.__setattr__(self, "endpoint", endpoint)
        object.__setattr__(self, "body", MappingProxyType(body))
        object.__setattr__(self, "account_id", account)
        object.__setattr__(self, "environment", environment)
        object.__setattr__(self, "capability_snapshot_id", capability_snapshot_id)
        object.__setattr__(self, "instrument_version", instrument_version)
        object.__setattr__(self, "documentation_refs", refs)
        object.__setattr__(
            self,
            "body_sha256",
            "sha256:" + sha256(rendered_body.encode("utf-8")).hexdigest(),
        )

    def to_guarded_dispatch_request(self) -> Mapping[str, object]:
        """Project canonical Kraken preparation into GuardedDispatcher payload."""
        return MappingProxyType(
            {
                "endpoint": self.endpoint,
                "body": dict(self.body),
                "capability_snapshot_id": self.capability_snapshot_id,
            }
        )


def prepare_spot_order_request(
    intent: KrakenSpotOrderIntent,
    *,
    client_order_id: str,
    account_id: str,
    environment: str,
    capability: CapabilitySnapshot,
    at: datetime,
) -> KrakenSpotPreparedRequest:
    """Prepare a logical AddOrder request without nonce, signature or deadline.

    Nonce/authentication and any wall-clock deadline belong at the transport
    boundary after GuardedDispatcher's final authority check. Account and
    environment are explicit because Kraken's private path itself does not carry
    account identity; credential selection must not be allowed to retarget a
    request after capability admission.
    """

    if not isinstance(intent, KrakenSpotOrderIntent):
        raise TypeError("intent must be KrakenSpotOrderIntent")
    if not isinstance(capability, CapabilitySnapshot):
        raise TypeError("capability must be CapabilitySnapshot")
    point = _instant(at, name="at")
    client_id = validate_spot_client_order_id(client_order_id)
    account = _text(account_id, name="account_id")
    env = _environment(environment)
    if capability.provider_id.upper() != "KRAKEN":
        raise KrakenSpotAdapterError("capability belongs to another provider")
    if capability.account_id != account:
        raise KrakenSpotAdapterError("capability account does not match target account")
    if capability.environment.upper() != env:
        raise KrakenSpotAdapterError("capability environment does not match target environment")
    if capability.instrument_version != intent.instrument_version:
        raise KrakenSpotAdapterError("capability instrument version does not match intent")
    if not capability.admits(
        at=point,
        order_type=intent.order_type,
        time_in_force=intent.time_in_force,
        permission_scope="ORDER_WRITE",
    ):
        raise KrakenSpotAdapterError("exact capability evidence does not admit this order")

    body: dict[str, object] = {
        "pair": intent.pair,
        "type": intent.side.lower(),
        "ordertype": intent.order_type.lower(),
        "volume": _decimal_text(intent.volume),
        "cl_ord_id": client_id,
        "timeinforce": intent.time_in_force,
    }
    if intent.price is not None:
        body["price"] = _decimal_text(intent.price)
    if intent.post_only:
        body["oflags"] = "post"

    return KrakenSpotPreparedRequest(
        endpoint="/0/private/AddOrder",
        body=body,
        account_id=account,
        environment=env,
        capability_snapshot_id=capability.snapshot_id,
        documentation_refs=tuple(KRAKEN_SPOT_DOCS.values()),
        instrument_version=intent.instrument_version,
        _factory_token=_KRAKEN_SPOT_PREPARED_REQUEST_FACTORY_TOKEN,
    )

def _uuid_text(value: object, *, name: str) -> str:
    text = _text(value, name=name)
    try:
        UUID(text)
    except ValueError as error:
        raise KrakenSpotAdapterError(f"{name} must be a UUID") from error
    return text


def _iso_utc_text(value: object, *, name: str) -> str:
    text = _text(value, name=name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise KrakenSpotAdapterError(f"{name} must be an ISO timestamp") from error
    if parsed.tzinfo is None:
        raise KrakenSpotAdapterError(f"{name} must include timezone")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _validate_submission_scope(
    prepared_request: KrakenSpotPreparedRequest,
    *,
    source_uri: str,
) -> str:
    if not isinstance(prepared_request, KrakenSpotPreparedRequest):
        raise TypeError("prepared_request must be KrakenSpotPreparedRequest")
    if prepared_request.environment != "LIVE":
        raise KrakenSpotAdapterError(
            "Kraken Spot provider submission evidence is qualified only for LIVE"
        )
    source = _text(source_uri, name="source_uri")
    if source != "https://api.kraken.com/0/private/AddOrder":
        raise KrakenSpotAdapterError(
            "source_uri must be the exact HTTPS Kraken Spot AddOrder endpoint"
        )
    return source


def _submission_evidence(
    observation: ProviderSubmissionObservation,
    *,
    prepared_request: KrakenSpotPreparedRequest,
    source_uri: str,
) -> dict[str, str]:
    if not isinstance(observation, ProviderSubmissionObservation):
        raise TypeError(
            "observation must be durable ProviderSubmissionObservation"
        )
    source = _validate_submission_scope(
        prepared_request,
        source_uri=source_uri,
    )
    cid = validate_spot_client_order_id(
        prepared_request.body.get("cl_ord_id")
    )
    observation.require_scope(
        provider_id="KRAKEN",
        endpoint=prepared_request.endpoint,
        prepared_request_sha256=prepared_request.body_sha256,
        capability_snapshot_ids=(prepared_request.capability_snapshot_id,),
        instrument_versions=(prepared_request.instrument_version,),
        account_id=prepared_request.account_id,
        environment=prepared_request.environment,
        client_order_id=cid,
    )
    return {
        "artifact_id": str(
            uuid5(
                NAMESPACE_URL,
                f"{source}#{observation.evidence_ref}",
            )
        ),
        "sha256": observation.response_sha256,
        "source_uri": source,
        "observed_at": observation.observed_at,
        "rights_id": "provider-observation-kraken-spot",
    }


def spot_submission_requires_reconciliation(payload: object) -> bool:
    """Return whether exact AddOrder bytes require reconcile-before-retry."""

    if not isinstance(payload, Mapping):
        return False
    errors = payload.get("error")
    if isinstance(errors, (str, bytes)) or not isinstance(errors, (list, tuple)):
        return False
    return any(
        isinstance(item, str) and item == "EService:Deadline elapsed"
        for item in errors
    )


def parse_spot_submission_response(
    *,
    attempt_id: str,
    prepared_request: KrakenSpotPreparedRequest,
    source_uri: str,
    observation: ProviderSubmissionObservation | None = None,
    transport_ambiguous: bool = False,
) -> dict[str, object]:
    """Map one durable exact AddOrder response without confusing ACK with fill.

    ACK/REJECT authority comes only from the exact provider response bytes bound
    by the canonical guarded submission journal. A caller-decoded Mapping cannot
    mint financial submission evidence. Transport ambiguity remains UNKNOWN and
    requires reconciliation before any economic retry.
    """

    aid = _uuid_text(attempt_id, name="attempt_id")
    source = _validate_submission_scope(
        prepared_request,
        source_uri=source_uri,
    )
    cid = validate_spot_client_order_id(
        prepared_request.body.get("cl_ord_id")
    )
    if type(transport_ambiguous) is not bool:
        raise TypeError("transport_ambiguous must be boolean")
    if transport_ambiguous:
        if observation is not None:
            raise KrakenSpotAdapterError(
                "ambiguous transport must not fabricate a provider response"
            )
        return {
            "attempt_id": aid,
            "outcome": "UNKNOWN",
            "client_order_id": cid,
            "reason_code": "KRAKEN_SPOT_TRANSPORT_AMBIGUOUS",
            "evidence": [],
            "retry_disposition": "RECONCILE_FIRST",
        }

    if not isinstance(observation, ProviderSubmissionObservation):
        raise TypeError(
            "observation must be durable ProviderSubmissionObservation"
        )
    if observation.response_binding.attempt_id != aid:
        raise KrakenSpotAdapterError("submission observation attempt_id mismatch")
    evidence = [
        _submission_evidence(
            observation,
            prepared_request=prepared_request,
            source_uri=source,
        )
    ]
    payload = observation.payload
    if not isinstance(payload, Mapping):
        raise KrakenSpotAdapterError("provider response payload must be an object")
    errors = payload.get("error", ())
    if isinstance(errors, (str, bytes)) or not isinstance(errors, (list, tuple)):
        raise KrakenSpotAdapterError("Kraken error field must be a sequence")
    nonempty_errors = tuple(str(item) for item in errors if str(item))
    if spot_submission_requires_reconciliation(payload):
        return {
            "attempt_id": aid,
            "outcome": "UNKNOWN",
            "client_order_id": cid,
            "reason_code": "KRAKEN_SPOT_DEADLINE_ELAPSED_AMBIGUOUS",
            "evidence": evidence,
            "retry_disposition": "RECONCILE_FIRST",
        }
    if nonempty_errors:
        return {
            "attempt_id": aid,
            "outcome": "REJECTED",
            "client_order_id": cid,
            "reason_code": "KRAKEN_SPOT_" + ";".join(nonempty_errors),
            "evidence": evidence,
            "retry_disposition": "NEVER",
        }

    result = payload.get("result")
    if not isinstance(result, Mapping):
        raise KrakenSpotAdapterError("successful response must contain a result object")
    txids = result.get("txid")
    if isinstance(txids, (str, bytes)) or not isinstance(txids, (list, tuple)) or not txids:
        raise KrakenSpotAdapterError("successful response must contain exactly one transaction id")
    normalized = tuple(_text(value, name="txid") for value in txids)
    if len(normalized) != 1:
        raise KrakenSpotAdapterError(
            "canonical SubmissionResult requires exactly one provider order id"
        )
    return {
        "attempt_id": aid,
        "outcome": "ACKNOWLEDGED",
        "provider_order_id": normalized[0],
        "client_order_id": cid,
        "evidence": evidence,
        "retry_disposition": "NEVER",
    }


@dataclass(frozen=True)
class KrakenSpotAbsenceEvidence:
    order_found: bool
    open_orders_complete: bool
    closed_orders_complete: bool
    trades_complete: bool
    ledgers_complete: bool
    consistency_horizon_satisfied: bool
    qualified_exclusion_semantics: bool = False

    def __post_init__(self) -> None:
        for field_name in (
            "order_found",
            "open_orders_complete",
            "closed_orders_complete",
            "trades_complete",
            "ledgers_complete",
            "consistency_horizon_satisfied",
            "qualified_exclusion_semantics",
        ):
            if type(getattr(self, field_name)) is not bool:
                raise TypeError(f"{field_name} must be boolean")
        if self.qualified_exclusion_semantics:
            raise KrakenSpotAdapterError(
                "Kraken Spot foundation cannot self-assert provider exclusion semantics"
            )

    def verdict(self) -> str:
        if self.order_found:
            return "FOUND"
        return "INCONCLUSIVE"


_KRAKEN_SPOT_PAGINATION_ENDPOINTS = MappingProxyType(
    {
        "ORDER_HISTORY": ("/0/private/ClosedOrders", "closed", 50),
        "EXECUTIONS": ("/0/private/TradesHistory", "trades", 100),
        "ACTIVITIES": ("/0/private/Ledgers", "ledger", 50),
    }
)


_KRAKEN_SPOT_PAGE_EVIDENCE_FACTORY_TOKEN = object()


@dataclass(frozen=True)
class KrakenSpotPageEvidence:
    """Exact-response-bound evidence for one Kraken offset page."""

    surface: str
    account_id: str
    environment: str
    offset: int
    limit: int
    record_count: int
    total_count: int
    evidence_ref: str
    filter_items: tuple[tuple[str, str], ...]
    _factory_token: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._factory_token is not _KRAKEN_SPOT_PAGE_EVIDENCE_FACTORY_TOKEN:
            raise KrakenSpotAdapterError(
                "Kraken page evidence must come from exact response observation"
            )
        normalized = _text(self.surface, name="surface").upper()
        if normalized not in _KRAKEN_SPOT_PAGINATION_ENDPOINTS:
            raise KrakenSpotAdapterError("unsupported Kraken pagination surface")
        object.__setattr__(self, "surface", normalized)
        object.__setattr__(
            self,
            "account_id",
            _text(self.account_id, name="account_id"),
        )
        object.__setattr__(
            self,
            "environment",
            _environment(self.environment),
        )
        for name in ("offset", "limit", "record_count", "total_count"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool):
                raise KrakenSpotAdapterError(f"{name} must be an integer")
        if self.offset < 0:
            raise KrakenSpotAdapterError("offset cannot be negative")
        if self.limit < 1:
            raise KrakenSpotAdapterError("limit must be positive")
        if self.record_count < 0 or self.record_count > self.limit:
            raise KrakenSpotAdapterError(
                "record_count must be between zero and the page limit"
            )
        if self.total_count < 0:
            raise KrakenSpotAdapterError("total_count cannot be negative")
        if self.offset + self.record_count > self.total_count:
            raise KrakenSpotAdapterError(
                "Kraken page extends beyond provider total count"
            )
        if (
            self.record_count < self.limit
            and self.offset + self.record_count < self.total_count
        ):
            raise KrakenSpotAdapterError(
                "Kraken short page cannot precede remaining provider records"
            )
        evidence = _text(self.evidence_ref, name="evidence_ref")
        if not evidence.startswith("provider-read:sha256:"):
            raise KrakenSpotAdapterError(
                "pagination evidence must reference exact provider response bytes"
            )
        object.__setattr__(self, "evidence_ref", evidence)
        if not isinstance(self.filter_items, tuple):
            raise TypeError("filter_items must be a tuple")
        normalized_filters: list[tuple[str, str]] = []
        for item in self.filter_items:
            if (
                not isinstance(item, tuple)
                or len(item) != 2
                or not all(isinstance(value, str) for value in item)
            ):
                raise TypeError(
                    "filter_items must contain canonical string pairs"
                )
            normalized_filters.append(item)
        if tuple(sorted(normalized_filters)) != tuple(normalized_filters):
            raise KrakenSpotAdapterError(
                "filter_items must be sorted canonically"
            )

    @property
    def proves_last_page(self) -> bool:
        return self.offset + self.record_count >= self.total_count


class KrakenSpotPaginationCoverage:
    """Contiguous exact-response offset coverage for one Kraken history surface."""

    def __init__(self, *, surface: str) -> None:
        normalized = _text(surface, name="surface").upper()
        if normalized not in _KRAKEN_SPOT_PAGINATION_ENDPOINTS:
            raise KrakenSpotAdapterError("unsupported Kraken pagination surface")
        self.surface = normalized
        self._pages: list[KrakenSpotPageEvidence] = []

    def add_page(self, page: KrakenSpotPageEvidence) -> None:
        if not isinstance(page, KrakenSpotPageEvidence):
            raise TypeError("page must be KrakenSpotPageEvidence")
        if page.surface != self.surface:
            raise KrakenSpotAdapterError(
                "Kraken pagination page surface mismatch"
            )
        if self.complete:
            raise KrakenSpotAdapterError(
                "Kraken pagination coverage is already complete"
            )
        expected = 0 if not self._pages else self._pages[-1].offset + self._pages[-1].record_count
        if page.offset != expected:
            raise KrakenSpotAdapterError(
                f"Kraken pagination gap: expected offset {expected}"
            )
        if self._pages:
            first = self._pages[0]
            if (
                page.account_id != first.account_id
                or page.environment != first.environment
            ):
                raise KrakenSpotAdapterError(
                    "Kraken pagination account/environment scope changed during coverage"
                )
            if page.total_count != first.total_count:
                raise KrakenSpotAdapterError(
                    "Kraken pagination total count changed during coverage"
                )
            if page.filter_items != first.filter_items:
                raise KrakenSpotAdapterError(
                    "Kraken pagination filters changed during coverage"
                )
            if page.evidence_ref in {item.evidence_ref for item in self._pages}:
                raise KrakenSpotAdapterError(
                    "Kraken pagination response evidence is duplicated"
                )
        self._pages.append(page)

    @property
    def complete(self) -> bool:
        return bool(self._pages and self._pages[-1].proves_last_page)

    @property
    def next_offset(self) -> int:
        if not self._pages:
            return 0
        return self._pages[-1].offset + self._pages[-1].record_count

    @property
    def pages(self) -> tuple[KrakenSpotPageEvidence, ...]:
        return tuple(self._pages)

    @property
    def evidence_refs(self) -> tuple[str, ...]:
        return tuple(page.evidence_ref for page in self._pages)

    @property
    def scope(self) -> tuple[str, str]:
        if not self._pages:
            raise KrakenSpotAdapterError(
                "Kraken pagination coverage has no evidenced scope"
            )
        first = self._pages[0]
        return first.account_id, first.environment


def pagination_page_from_observation(
    observation: ProviderResponseObservation,
    *,
    surface: str,
) -> KrakenSpotPageEvidence:
    """Derive one Kraken history page only from exact authenticated response bytes."""

    if not isinstance(observation, ProviderResponseObservation):
        raise TypeError("observation must be ProviderResponseObservation")
    normalized = _text(surface, name="surface").upper()
    spec = _KRAKEN_SPOT_PAGINATION_ENDPOINTS.get(normalized)
    if spec is None:
        raise KrakenSpotAdapterError("unsupported Kraken pagination surface")
    endpoint, records_key, maximum_limit = spec
    observation.require_scope(
        provider_id="KRAKEN",
        surface=Surface.ACTIVITIES,
        endpoint=endpoint,
    )
    payload = observation.payload
    if not isinstance(payload, Mapping):
        raise KrakenSpotAdapterError("Kraken pagination response must be an object")
    raw_errors = payload.get("error")
    if isinstance(raw_errors, (str, bytes)) or not isinstance(
        raw_errors, (list, tuple)
    ):
        raise KrakenSpotAdapterError("Kraken error field must be a sequence")
    if any(str(value) for value in raw_errors):
        raise KrakenSpotAdapterError(
            "Kraken pagination response was not successful"
        )
    result = payload.get("result")
    if not isinstance(result, Mapping):
        raise KrakenSpotAdapterError("Kraken pagination result must be an object")
    records = result.get(records_key)
    if not isinstance(records, Mapping):
        raise KrakenSpotAdapterError(
            f"Kraken pagination result must contain {records_key} object"
        )
    total_count = result.get("count")
    if (
        not isinstance(total_count, int)
        or isinstance(total_count, bool)
        or total_count < 0
    ):
        raise KrakenSpotAdapterError(
            "Kraken pagination requires non-negative provider count"
        )

    query = observation.query_binding.query
    if query.get("without_count") == "true":
        raise KrakenSpotAdapterError(
            "without_count response cannot prove Kraken pagination coverage"
        )
    raw_offset = query.get("ofs", "0")
    if not raw_offset.isdigit() or str(int(raw_offset, 10)) != raw_offset:
        raise KrakenSpotAdapterError("Kraken pagination offset is not canonical")
    offset = int(raw_offset, 10)

    if endpoint == "/0/private/TradesHistory":
        raw_limit = query.get("limit", "50")
        if not raw_limit.isdigit() or str(int(raw_limit, 10)) != raw_limit:
            raise KrakenSpotAdapterError("Kraken pagination limit is not canonical")
        limit = int(raw_limit, 10)
        if limit < 1 or limit > maximum_limit:
            raise KrakenSpotAdapterError(
                "Kraken TradesHistory page limit is outside 1..100"
            )
    else:
        limit = maximum_limit

    return KrakenSpotPageEvidence(
        surface=normalized,
        account_id=observation.account_id,
        environment=observation.environment,
        offset=offset,
        limit=limit,
        record_count=len(records),
        total_count=total_count,
        evidence_ref=observation.evidence_ref,
        filter_items=tuple(
            sorted(
                (str(key), str(value))
                for key, value in query.items()
                if key != "ofs"
            )
        ),
        _factory_token=_KRAKEN_SPOT_PAGE_EVIDENCE_FACTORY_TOKEN,
    )


_KRAKEN_SPOT_OPEN_ORDERS_FACTORY_TOKEN = object()


@dataclass(frozen=True)
class KrakenSpotOpenOrdersSnapshotEvidence:
    """Exact unfiltered OpenOrders snapshot for one Kraken account/environment."""

    account_id: str
    environment: str
    evidence_ref: str
    order_ids: tuple[str, ...]
    filter_items: tuple[tuple[str, str], ...]
    _factory_token: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._factory_token is not _KRAKEN_SPOT_OPEN_ORDERS_FACTORY_TOKEN:
            raise KrakenSpotAdapterError(
                "Kraken open-orders evidence must come from exact response observation"
            )
        object.__setattr__(
            self,
            "account_id",
            _text(self.account_id, name="account_id"),
        )
        object.__setattr__(
            self,
            "environment",
            _environment(self.environment),
        )
        evidence = _text(self.evidence_ref, name="evidence_ref")
        if not evidence.startswith("provider-read:sha256:"):
            raise KrakenSpotAdapterError(
                "open-orders evidence must reference exact provider response bytes"
            )
        object.__setattr__(self, "evidence_ref", evidence)
        if not isinstance(self.order_ids, tuple):
            raise TypeError("order_ids must be a tuple")
        normalized_ids = tuple(
            _text(value, name="provider_order_id")
            for value in self.order_ids
        )
        if len(set(normalized_ids)) != len(normalized_ids):
            raise KrakenSpotAdapterError(
                "open-orders provider order ids must be unique"
            )
        if tuple(sorted(normalized_ids)) != normalized_ids:
            raise KrakenSpotAdapterError(
                "open-orders provider order ids must be sorted canonically"
            )
        object.__setattr__(self, "order_ids", normalized_ids)
        if not isinstance(self.filter_items, tuple):
            raise TypeError("filter_items must be a tuple")
        if tuple(sorted(self.filter_items)) != self.filter_items:
            raise KrakenSpotAdapterError(
                "open-orders filter_items must be sorted canonically"
            )

    @property
    def complete_for_account(self) -> bool:
        return True


def open_orders_snapshot_from_observation(
    observation: ProviderResponseObservation,
) -> KrakenSpotOpenOrdersSnapshotEvidence:
    """Bind account-wide OpenOrders completeness to one exact provider response."""

    if not isinstance(observation, ProviderResponseObservation):
        raise TypeError("observation must be ProviderResponseObservation")
    observation.require_scope(
        provider_id="KRAKEN",
        surface=Surface.ACTIVITIES,
        endpoint="/0/private/OpenOrders",
    )
    payload = observation.payload
    if not isinstance(payload, Mapping):
        raise KrakenSpotAdapterError(
            "Kraken open-orders response must be an object"
        )
    raw_errors = payload.get("error")
    if isinstance(raw_errors, (str, bytes)) or not isinstance(
        raw_errors, (list, tuple)
    ):
        raise KrakenSpotAdapterError("Kraken error field must be a sequence")
    if any(str(value) for value in raw_errors):
        raise KrakenSpotAdapterError(
            "Kraken open-orders response was not successful"
        )
    result = payload.get("result")
    if not isinstance(result, Mapping):
        raise KrakenSpotAdapterError(
            "Kraken open-orders result must be an object"
        )
    orders = result.get("open")
    if not isinstance(orders, Mapping):
        raise KrakenSpotAdapterError(
            "Kraken open-orders result must contain open object"
        )
    query = observation.query_binding.query
    restricting = {"userref", "cl_ord_id"} & set(query)
    if restricting:
        raise KrakenSpotAdapterError(
            "filtered Kraken OpenOrders cannot prove account-wide completeness"
        )
    return KrakenSpotOpenOrdersSnapshotEvidence(
        account_id=observation.account_id,
        environment=observation.environment,
        evidence_ref=observation.evidence_ref,
        order_ids=tuple(sorted(_text(str(value), name="provider_order_id") for value in orders)),
        filter_items=tuple(sorted((str(key), str(value)) for key, value in query.items())),
        _factory_token=_KRAKEN_SPOT_OPEN_ORDERS_FACTORY_TOKEN,
    )


def absence_evidence_from_pagination(
    *,
    order_found: bool,
    open_orders: KrakenSpotOpenOrdersSnapshotEvidence,
    order_history: KrakenSpotPaginationCoverage,
    executions: KrakenSpotPaginationCoverage,
    activities: KrakenSpotPaginationCoverage,
    consistency_horizon_satisfied: bool,
    qualified_exclusion_semantics: bool = False,
) -> KrakenSpotAbsenceEvidence:
    """Bind Kraken absence inputs to concrete history pagination coverage."""

    if not isinstance(open_orders, KrakenSpotOpenOrdersSnapshotEvidence):
        raise TypeError(
            "open_orders must be KrakenSpotOpenOrdersSnapshotEvidence"
        )
    for coverage, expected in (
        (order_history, "ORDER_HISTORY"),
        (executions, "EXECUTIONS"),
        (activities, "ACTIVITIES"),
    ):
        if not isinstance(coverage, KrakenSpotPaginationCoverage):
            raise TypeError(f"{expected} coverage has wrong type")
        if coverage.surface != expected:
            raise KrakenSpotAdapterError(
                f"coverage surface mismatch: expected {expected}"
            )
        if not coverage.complete:
            raise KrakenSpotAdapterError(
                f"{expected} pagination coverage is incomplete"
            )
        if coverage.scope != (
            open_orders.account_id,
            open_orders.environment,
        ):
            raise KrakenSpotAdapterError(
                "Kraken absence evidence account/environment scope mismatch"
            )
    for name, value in (
        ("order_found", order_found),
        ("consistency_horizon_satisfied", consistency_horizon_satisfied),
        ("qualified_exclusion_semantics", qualified_exclusion_semantics),
    ):
        if type(value) is not bool:
            raise TypeError(f"{name} must be boolean")
    return KrakenSpotAbsenceEvidence(
        order_found=order_found,
        open_orders_complete=open_orders.complete_for_account,
        closed_orders_complete=order_history.complete,
        trades_complete=executions.complete,
        ledgers_complete=activities.complete,
        consistency_horizon_satisfied=consistency_horizon_satisfied,
        qualified_exclusion_semantics=qualified_exclusion_semantics,
    )


def derivatives_supported_by_this_module() -> bool:
    """Make the separation explicit: Kraken Derivatives needs its own adapter."""

    return False


def _seconds_to_utc(value, *, name: str) -> str:
    seconds = _decimal(value, name=name)
    if seconds < 0:
        raise KrakenSpotAdapterError(f"{name} cannot be negative")
    micros = seconds * Decimal("1000000")
    if micros != micros.to_integral_value():
        raise KrakenSpotAdapterError(f"{name} has precision finer than one microsecond")
    total_micros = int(micros)
    whole_seconds, remainder = divmod(total_micros, 1_000_000)
    instant = datetime.fromtimestamp(whole_seconds, tz=timezone.utc) + timedelta(
        microseconds=remainder
    )
    return instant.isoformat(timespec="microseconds").replace("+00:00", "Z")


def parse_trade_history(
    observation: ProviderResponseObservation,
    *,
    instrument_versions: Mapping[str, str],
    client_ids_by_provider_order: Mapping[str, str],
    fee_currency_by_pair: Mapping[str, str],
) -> tuple[ProviderFillEvidence, ...]:
    """Map one authenticated exact-byte Kraken TradesHistory read into fills.

    Kraken trade rows do not safely imply AutoTrade instrument versions, client
    identities or fee currency. Those mappings must come from separately
    evidenced metadata/order state and are therefore explicit inputs.
    """

    if not isinstance(observation, ProviderResponseObservation):
        raise TypeError("observation must be ProviderResponseObservation")
    observation.require_scope(
        provider_id="KRAKEN",
        surface=Surface.ACTIVITIES,
        endpoint="/0/private/TradesHistory",
    )
    response = observation.payload
    account_id = observation.account_id
    environment = observation.environment
    if not isinstance(response, Mapping):
        raise TypeError("response must be a mapping")
    raw_errors = response.get("error")
    if isinstance(raw_errors, (str, bytes)) or not isinstance(raw_errors, (list, tuple)):
        raise KrakenSpotAdapterError("Kraken error field must be a sequence")
    if any(str(value) for value in raw_errors):
        raise KrakenSpotAdapterError("Kraken TradesHistory response was not successful")
    result = response.get("result")
    if not isinstance(result, Mapping):
        raise KrakenSpotAdapterError("TradesHistory result must be an object")
    trades = result.get("trades")
    if not isinstance(trades, Mapping):
        raise KrakenSpotAdapterError("TradesHistory trades must be an object")
    for name, value in (
        ("instrument_versions", instrument_versions),
        ("client_ids_by_provider_order", client_ids_by_provider_order),
        ("fee_currency_by_pair", fee_currency_by_pair),
    ):
        if not isinstance(value, Mapping):
            raise TypeError(f"{name} must be a mapping")

    fills: list[ProviderFillEvidence] = []
    for trade_id, raw in trades.items():
        execution_id = _text(str(trade_id), name="trade id")
        if not isinstance(raw, Mapping):
            raise KrakenSpotAdapterError(f"trade {execution_id} must be an object")
        pair = _text(str(raw.get("pair", "")), name="pair")
        provider_order_id = _text(str(raw.get("ordertxid", "")), name="ordertxid")
        if pair not in instrument_versions:
            raise KrakenSpotAdapterError(f"unmapped Kraken pair: {pair}")
        if pair not in fee_currency_by_pair:
            raise KrakenSpotAdapterError(
                f"missing evidenced fee currency for Kraken pair: {pair}"
            )
        client_id = client_ids_by_provider_order.get(provider_order_id)
        if client_id is not None:
            client_id = validate_spot_client_order_id(client_id)
        if "fee" not in raw or raw["fee"] is None:
            raise KrakenSpotAdapterError(
                f"missing provider fee amount for Kraken trade: {execution_id}"
            )
        raw_side = _text(raw.get("type"), name="trade.type")
        side_by_provider_value = {"buy": "BUY", "sell": "SELL"}
        if raw_side not in side_by_provider_value:
            raise KrakenSpotAdapterError(
                "Kraken trade type must be provider-evidenced buy or sell"
            )
        side = side_by_provider_value[raw_side]
        fills.append(
            ProviderFillEvidence.create(
                provider_id="KRAKEN",
                account_id=account_id,
                environment=environment,
                provider_execution_id=execution_id,
                client_order_id=client_id,
                instrument=_text(instrument_versions[pair], name="instrument_version"),
                side=side,
                quantity=raw.get("vol"),
                price=raw.get("price"),
                fee_amount=raw["fee"],
                fee_currency=_text(fee_currency_by_pair[pair], name="fee_currency"),
                trade_time=_seconds_to_utc(raw.get("time"), name="time"),
                evidence_refs=(observation.evidence_ref,),
            )
        )
    return tuple(fills)


def coverage_evidence(
    *,
    account_id: str,
    environment: str,
    surface: str,
    coverage_start: str,
    coverage_end: str,
    pagination_complete: bool,
    consistency_horizon_satisfied: bool,
    qualified_exclusion_semantics: bool = False,
) -> CoverageSurfaceEvidence:
    """Create canonical coverage without guessing provider absence semantics."""

    normalized = _text(surface, name="surface").upper()
    if normalized not in {
        "OPEN_ORDERS",
        "ORDER_HISTORY",
        "EXECUTIONS",
        "ACTIVITIES",
    }:
        raise KrakenSpotAdapterError("unsupported Kraken reconciliation surface")
    for name, value in (
        ("pagination_complete", pagination_complete),
        ("consistency_horizon_satisfied", consistency_horizon_satisfied),
        ("qualified_exclusion_semantics", qualified_exclusion_semantics),
    ):
        if type(value) is not bool:
            raise TypeError(f"{name} must be boolean")
    if qualified_exclusion_semantics:
        raise KrakenSpotAdapterError(
            "Kraken Spot foundation cannot self-assert provider exclusion semantics"
        )
    return CoverageSurfaceEvidence(
        provider_id="KRAKEN",
        account_id=account_id,
        environment=environment,
        surface=normalized,
        coverage_start=coverage_start,
        coverage_end=coverage_end,
        pagination_complete=pagination_complete,
        consistency_horizon_satisfied=consistency_horizon_satisfied,
        provider_semantics_exclude_execution=False,
    )
