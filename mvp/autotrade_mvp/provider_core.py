"""Provider-neutral safety contract for real adapter implementations.

The registry names architectural targets only. A provider/product/environment
combination remains unqualified until exact evidence proves the required
surfaces on the exact adapter build. Nothing in this module grants live trading
authority.
"""

from __future__ import annotations

from dataclasses import InitVar, dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from hashlib import sha256
import json
from types import MappingProxyType
from typing import Iterable, Literal, Mapping
import re

from .capabilities import CapabilitySnapshot
from .dispatch import SubmissionResponseBinding


class ProviderCoreError(ValueError):
    pass


_GIT_OBJECT_ID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


def _code_sha(value: str, name: str = "adapter_code_sha") -> str:
    if not isinstance(value, str) or value != value.strip():
        raise ProviderCoreError(
            f"{name} must be a canonical 40- or 64-character lowercase Git object id"
        )
    sha = _text(value, name)
    if _GIT_OBJECT_ID.fullmatch(sha) is None:
        raise ProviderCoreError(
            f"{name} must be a canonical 40- or 64-character lowercase Git object id"
        )
    return sha


def _text(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProviderCoreError(f"{name} is required")
    return value.strip()


def _decimal(value, name: str, *, non_negative: bool = False) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise ProviderCoreError(f"{name} must use exact decimal input")
    try:
        result = value if isinstance(value, Decimal) else Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ProviderCoreError(f"{name} must be a finite decimal") from error
    if not result.is_finite():
        raise ProviderCoreError(f"{name} must be a finite decimal")
    if non_negative and result < 0:
        raise ProviderCoreError(f"{name} cannot be negative")
    return result


def _utc(value: datetime, name: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ProviderCoreError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


class Surface(StrEnum):
    PUBLIC_DATA = "PUBLIC_DATA"
    AUTHENTICATED_READ = "AUTHENTICATED_READ"
    TRADING = "TRADING"
    ACTIVITIES = "ACTIVITIES"
    STREAM = "STREAM"


_PREPARED_READ_TOKEN = object()
_OBSERVED_RESPONSE_TOKEN = object()
_SUBMISSION_OBSERVED_RESPONSE_TOKEN = object()


def _utc_text(value: datetime, name: str) -> str:
    return _utc(value, name).isoformat().replace("+00:00", "Z")


def _canonical_query_values(
    values: Mapping[str, str] | None,
) -> Mapping[str, str]:
    if values is None:
        return MappingProxyType({})
    if not isinstance(values, Mapping):
        raise ProviderCoreError("query must be a mapping")
    normalized: dict[str, str] = {}
    for raw_key, raw_value in values.items():
        key = _text(raw_key, "query key")
        if key in normalized:
            raise ProviderCoreError("query keys must be unique after normalization")
        if not isinstance(raw_value, str) or raw_value != raw_value.strip():
            raise ProviderCoreError(
                "authenticated-read query values must be canonical strings"
            )
        normalized[key] = raw_value
    return MappingProxyType(dict(sorted(normalized.items())))


def _freeze_json(value: object, *, depth: int = 0) -> object:
    if depth > 64:
        raise ProviderCoreError("provider response exceeds maximum JSON depth")
    if isinstance(value, dict):
        frozen: dict[str, object] = {}
        for raw_key, nested in value.items():
            if not isinstance(raw_key, str):
                raise ProviderCoreError("provider response object keys must be strings")
            frozen[raw_key] = _freeze_json(nested, depth=depth + 1)
        return MappingProxyType(frozen)
    if isinstance(value, list):
        return tuple(_freeze_json(item, depth=depth + 1) for item in value)
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ProviderCoreError("provider response contains non-finite decimal")
        return value
    if isinstance(value, float):
        raise ProviderCoreError("provider response must not contain binary float values")
    if value is None or isinstance(value, (str, bool, int)):
        return value
    raise ProviderCoreError("provider response contains unsupported JSON value")


def _decode_exact_json(raw: bytes) -> object:
    if type(raw) is not bytes or not raw:
        raise ProviderCoreError("provider response bytes must be non-empty bytes")

    def no_duplicate_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ProviderCoreError(
                    f"provider response contains duplicate JSON key: {key}"
                )
            result[key] = value
        return result

    try:
        text = raw.decode("utf-8", errors="strict")
        decoded = json.loads(
            text,
            object_pairs_hook=no_duplicate_keys,
            parse_float=Decimal,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ProviderCoreError(
                    f"provider response contains non-finite JSON constant: {value}"
                )
            ),
        )
    except ProviderCoreError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProviderCoreError(
            "provider response must be exact UTF-8 JSON bytes"
        ) from error
    return _freeze_json(decoded)


@dataclass(frozen=True)
class AuthenticatedReadQueryBinding:
    """Immutable credential/capability scope fixed before provider read I/O."""

    provider_id: str
    account_id: str
    entity_id: str
    environment: str
    capability_snapshot_id: str
    instrument_version: str
    surface: Surface
    endpoint: str
    query: Mapping[str, str]
    prepared_at: str
    permission_scope: str
    query_digest: str
    _preparation_token: InitVar[object | None] = None

    def __post_init__(self, _preparation_token: object | None) -> None:
        if _preparation_token is not _PREPARED_READ_TOKEN:
            raise ProviderCoreError(
                "authenticated-read bindings must come from verified capability preparation"
            )
        provider = _text(self.provider_id, "provider_id").upper()
        if provider not in PROVIDERS:
            raise ProviderCoreError("unknown provider")
        object.__setattr__(self, "provider_id", provider)
        object.__setattr__(self, "account_id", _text(self.account_id, "account_id"))
        object.__setattr__(self, "entity_id", _text(self.entity_id, "entity_id"))
        object.__setattr__(
            self,
            "environment",
            _text(self.environment, "environment").upper(),
        )
        object.__setattr__(
            self,
            "capability_snapshot_id",
            _text(self.capability_snapshot_id, "capability_snapshot_id"),
        )
        object.__setattr__(
            self,
            "instrument_version",
            _text(self.instrument_version, "instrument_version"),
        )
        if not isinstance(self.surface, Surface):
            raise ProviderCoreError("surface must be a provider Surface")
        if self.surface not in {Surface.AUTHENTICATED_READ, Surface.ACTIVITIES}:
            raise ProviderCoreError(
                "authenticated-read binding requires AUTHENTICATED_READ or ACTIVITIES"
            )
        endpoint = _text(self.endpoint, "endpoint")
        if not endpoint.startswith("/") or "://" in endpoint:
            raise ProviderCoreError(
                "authenticated-read endpoint must be a canonical provider-relative path"
            )
        object.__setattr__(self, "endpoint", endpoint)
        object.__setattr__(self, "query", _canonical_query_values(self.query))
        object.__setattr__(
            self,
            "prepared_at",
            _text(self.prepared_at, "prepared_at"),
        )
        try:
            parsed = datetime.fromisoformat(self.prepared_at.replace("Z", "+00:00"))
        except ValueError as error:
            raise ProviderCoreError("prepared_at must be an ISO timestamp") from error
        if parsed.tzinfo is None or not self.prepared_at.endswith("Z"):
            raise ProviderCoreError("prepared_at must be canonical UTC text")
        canonical_time = parsed.astimezone(timezone.utc).isoformat().replace(
            "+00:00", "Z"
        )
        if canonical_time != self.prepared_at:
            raise ProviderCoreError("prepared_at must be canonical UTC text")
        object.__setattr__(
            self,
            "permission_scope",
            _text(self.permission_scope, "permission_scope"),
        )
        if re.fullmatch(r"sha256:[0-9a-f]{64}", self.query_digest) is None:
            raise ProviderCoreError("query_digest must be a canonical SHA-256 digest")

    def require_scope(
        self,
        *,
        provider_id: str,
        surface: Surface,
        endpoint: str,
        account_id: str | None = None,
        environment: str | None = None,
    ) -> None:
        if _text(provider_id, "provider_id").upper() != self.provider_id:
            raise ProviderCoreError("provider-read provenance provider mismatch")
        if surface != self.surface:
            raise ProviderCoreError("provider-read provenance surface mismatch")
        if _text(endpoint, "endpoint") != self.endpoint:
            raise ProviderCoreError("provider-read provenance endpoint mismatch")
        if account_id is not None and _text(account_id, "account_id") != self.account_id:
            raise ProviderCoreError("provider-read provenance account mismatch")
        if (
            environment is not None
            and _text(environment, "environment").upper() != self.environment
        ):
            raise ProviderCoreError("provider-read provenance environment mismatch")


def prepare_authenticated_read_query(
    *,
    capability: CapabilitySnapshot,
    surface: Surface,
    endpoint: str,
    query: Mapping[str, str] | None,
    at: datetime,
    permission_scope: str = "ORDER.READ",
) -> AuthenticatedReadQueryBinding:
    """Prepare one authenticated query from canonical capability identity.

    Account/environment are intentionally not parameters: they are inherited
    from the VERIFIED capability snapshot before any provider response exists.
    """

    if not isinstance(capability, CapabilitySnapshot):
        raise TypeError("capability must be CapabilitySnapshot")
    point = _utc(at, "at")
    scope = _text(permission_scope, "permission_scope")
    if (
        capability.status != "VERIFIED"
        or not (capability.observed_at <= point < capability.expires_at)
        or scope not in capability.permission_scopes
    ):
        raise ProviderCoreError(
            "exact verified capability does not admit authenticated provider read"
        )
    provider = capability.provider_id.upper()
    if provider not in PROVIDERS:
        raise ProviderCoreError("unknown provider")
    normalized_endpoint = _text(endpoint, "endpoint")
    if not normalized_endpoint.startswith("/") or "://" in normalized_endpoint:
        raise ProviderCoreError(
            "authenticated-read endpoint must be a canonical provider-relative path"
        )
    normalized_query = _canonical_query_values(query)
    prepared_at = _utc_text(point, "at")
    material = {
        "provider_id": provider,
        "account_id": capability.account_id,
        "entity_id": capability.entity_id,
        "environment": capability.environment,
        "capability_snapshot_id": capability.snapshot_id,
        "instrument_version": capability.instrument_version,
        "surface": surface.value if isinstance(surface, Surface) else str(surface),
        "endpoint": normalized_endpoint,
        "query": dict(normalized_query),
        "prepared_at": prepared_at,
        "permission_scope": scope,
    }
    encoded = json.dumps(
        material,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return AuthenticatedReadQueryBinding(
        provider_id=provider,
        account_id=capability.account_id,
        entity_id=capability.entity_id,
        environment=capability.environment,
        capability_snapshot_id=capability.snapshot_id,
        instrument_version=capability.instrument_version,
        surface=surface,
        endpoint=normalized_endpoint,
        query=normalized_query,
        prepared_at=prepared_at,
        permission_scope=scope,
        query_digest="sha256:" + sha256(encoded).hexdigest(),
        _preparation_token=_PREPARED_READ_TOKEN,
    )


@dataclass(frozen=True)
class ProviderResponseObservation:
    """Exact response bytes bound to one immutable authenticated query."""

    query_binding: AuthenticatedReadQueryBinding
    provider_environment: str
    observed_at: str
    http_status: int
    response_sha256: str
    evidence_ref: str
    payload: object
    _observation_token: InitVar[object | None] = None

    def __post_init__(self, _observation_token: object | None) -> None:
        if _observation_token is not _OBSERVED_RESPONSE_TOKEN:
            raise ProviderCoreError(
                "provider response observations must come from exact response bytes"
            )
        if not isinstance(self.query_binding, AuthenticatedReadQueryBinding):
            raise TypeError(
                "query_binding must be AuthenticatedReadQueryBinding"
            )
        object.__setattr__(
            self,
            "provider_environment",
            _text(self.provider_environment, "provider_environment").upper(),
        )
        if (
            isinstance(self.http_status, bool)
            or not isinstance(self.http_status, int)
            or self.http_status < 200
            or self.http_status > 299
        ):
            raise ProviderCoreError(
                "successful provider response observation requires HTTP 2xx status"
            )
        if re.fullmatch(r"sha256:[0-9a-f]{64}", self.response_sha256) is None:
            raise ProviderCoreError(
                "response_sha256 must be a canonical SHA-256 digest"
            )
        if re.fullmatch(
            r"provider-read:sha256:[0-9a-f]{64}",
            self.evidence_ref,
        ) is None:
            raise ProviderCoreError("evidence_ref must be canonical")
        observed = _text(self.observed_at, "observed_at")
        try:
            point = datetime.fromisoformat(observed.replace("Z", "+00:00"))
            prepared = datetime.fromisoformat(
                self.query_binding.prepared_at.replace("Z", "+00:00")
            )
        except ValueError as error:
            raise ProviderCoreError(
                "provider response timestamps must be ISO timestamps"
            ) from error
        if (
            point.tzinfo is None
            or not observed.endswith("Z")
            or point < prepared
        ):
            raise ProviderCoreError(
                "provider response observation must be canonical UTC at/after query preparation"
            )
        canonical_time = point.astimezone(timezone.utc).isoformat().replace(
            "+00:00", "Z"
        )
        if canonical_time != observed:
            raise ProviderCoreError("observed_at must be canonical UTC text")
        object.__setattr__(self, "observed_at", observed)

    @property
    def provider_id(self) -> str:
        return self.query_binding.provider_id

    @property
    def account_id(self) -> str:
        return self.query_binding.account_id

    @property
    def environment(self) -> str:
        return self.query_binding.environment

    def require_scope(
        self,
        *,
        provider_id: str,
        surface: Surface,
        endpoint: str,
        account_id: str | None = None,
        environment: str | None = None,
        provider_environment: str | None = None,
    ) -> None:
        self.query_binding.require_scope(
            provider_id=provider_id,
            surface=surface,
            endpoint=endpoint,
            account_id=account_id,
            environment=environment,
        )
        if (
            provider_environment is not None
            and _text(
                provider_environment,
                "provider_environment",
            ).upper()
            != self.provider_environment
        ):
            raise ProviderCoreError(
                "provider-read provenance provider-environment mismatch"
            )


def observe_authenticated_json_response(
    *,
    query_binding: AuthenticatedReadQueryBinding,
    http_status: int,
    response_bytes: bytes,
    observed_at: datetime,
    provider_environment: str | None = None,
) -> ProviderResponseObservation:
    if not isinstance(query_binding, AuthenticatedReadQueryBinding):
        raise TypeError("query_binding must be AuthenticatedReadQueryBinding")
    if (
        isinstance(http_status, bool)
        or not isinstance(http_status, int)
        or http_status < 200
        or http_status > 299
    ):
        raise ProviderCoreError(
            "authenticated provider state requires an HTTP 2xx response"
        )
    payload = _decode_exact_json(response_bytes)
    observed = _utc_text(observed_at, "observed_at")
    if provider_environment is None and query_binding.provider_id == "BYBIT":
        raise ProviderCoreError(
            "BYBIT authenticated provider read requires explicit provider_environment"
        )
    provider_env = (
        query_binding.environment
        if provider_environment is None
        else _text(provider_environment, "provider_environment").upper()
    )
    if (
        query_binding.provider_id == "BYBIT"
        and provider_env not in {"MAINNET", "TESTNET", "DEMO"}
    ):
        raise ProviderCoreError(
            "BYBIT provider_environment must be MAINNET, TESTNET or DEMO"
        )
    response_digest = "sha256:" + sha256(response_bytes).hexdigest()
    identity_material = (
        query_binding.query_digest
        + "\n"
        + provider_env
        + "\n"
        + str(http_status)
        + "\n"
        + response_digest
        + "\n"
        + observed
    ).encode("utf-8")
    evidence_ref = "provider-read:sha256:" + sha256(identity_material).hexdigest()
    return ProviderResponseObservation(
        query_binding=query_binding,
        provider_environment=provider_env,
        observed_at=observed,
        http_status=http_status,
        response_sha256=response_digest,
        evidence_ref=evidence_ref,
        payload=payload,
        _observation_token=_OBSERVED_RESPONSE_TOKEN,
    )




def _thaw_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _thaw_json(nested) for key, nested in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


@dataclass(frozen=True)
class ProviderSubmissionObservation:
    """Exact write response proven by the canonical durable send journal."""

    response_binding: SubmissionResponseBinding
    endpoint: str
    capability_snapshot_ids: tuple[str, ...]
    instrument_versions: tuple[str, ...]
    evidence_ref: str
    payload: object
    _observation_token: InitVar[object | None] = None

    def __post_init__(self, _observation_token: object | None) -> None:
        if _observation_token is not _SUBMISSION_OBSERVED_RESPONSE_TOKEN:
            raise ProviderCoreError(
                "provider submission observations must come from durable exact response binding"
            )
        if not isinstance(self.response_binding, SubmissionResponseBinding):
            raise TypeError("response_binding must be SubmissionResponseBinding")
        endpoint = _text(self.endpoint, "endpoint")
        if not endpoint.startswith("/") or "://" in endpoint:
            raise ProviderCoreError(
                "submission endpoint must be a canonical provider-relative path"
            )
        object.__setattr__(self, "endpoint", endpoint)
        capabilities = tuple(
            _text(value, "capability_snapshot_id")
            for value in self.capability_snapshot_ids
        )
        instruments = tuple(
            _text(value, "instrument_version")
            for value in self.instrument_versions
        )
        if not capabilities or len(set(capabilities)) != len(capabilities):
            raise ProviderCoreError(
                "capability_snapshot_ids must be non-empty and unique"
            )
        if not instruments or len(set(instruments)) != len(instruments):
            raise ProviderCoreError(
                "instrument_versions must be non-empty and unique"
            )
        object.__setattr__(self, "capability_snapshot_ids", capabilities)
        object.__setattr__(self, "instrument_versions", instruments)
        if re.fullmatch(
            r"provider-write:sha256:[0-9a-f]{64}",
            self.evidence_ref,
        ) is None:
            raise ProviderCoreError("submission evidence_ref must be canonical")

    @property
    def provider_id(self) -> str:
        return self.response_binding.provider.upper()

    @property
    def account_id(self) -> str:
        return self.response_binding.account_id

    @property
    def environment(self) -> str:
        return self.response_binding.environment

    @property
    def client_order_id(self) -> str:
        return self.response_binding.client_order_id

    @property
    def response_sha256(self) -> str:
        return self.response_binding.response_sha256

    @property
    def observed_at(self) -> str:
        return self.response_binding.sent_at

    @property
    def request_sha256(self) -> str:
        return self.response_binding.request_hash

    def require_scope(
        self,
        *,
        provider_id: str,
        endpoint: str,
        prepared_request_sha256: str,
        capability_snapshot_ids: tuple[str, ...],
        instrument_versions: tuple[str, ...],
        account_id: str | None = None,
        environment: str | None = None,
        client_order_id: str | None = None,
    ) -> None:
        if _text(provider_id, "provider_id").upper() != self.provider_id:
            raise ProviderCoreError("provider-write provenance provider mismatch")
        if _text(endpoint, "endpoint") != self.endpoint:
            raise ProviderCoreError("provider-write provenance endpoint mismatch")
        if prepared_request_sha256 != self.request_sha256:
            raise ProviderCoreError("provider-write provenance request digest mismatch")
        if tuple(capability_snapshot_ids) != self.capability_snapshot_ids:
            raise ProviderCoreError("provider-write provenance capability mismatch")
        if tuple(instrument_versions) != self.instrument_versions:
            raise ProviderCoreError("provider-write provenance instrument mismatch")
        if account_id is not None and _text(account_id, "account_id") != self.account_id:
            raise ProviderCoreError("provider-write provenance account mismatch")
        if (
            environment is not None
            and _text(environment, "environment").upper() != self.environment
        ):
            raise ProviderCoreError("provider-write provenance environment mismatch")
        if (
            client_order_id is not None
            and _text(client_order_id, "client_order_id") != self.client_order_id
        ):
            raise ProviderCoreError("provider-write provenance client-order mismatch")


def observe_submission_json_response(
    *,
    response_binding: SubmissionResponseBinding,
    provider_id: str,
    endpoint: str,
    prepared_request_sha256: str,
    capability_snapshot_ids: tuple[str, ...],
    instrument_versions: tuple[str, ...],
) -> ProviderSubmissionObservation:
    """Project one exact durable write response into provider-neutral evidence."""

    if not isinstance(response_binding, SubmissionResponseBinding):
        raise TypeError("response_binding must be SubmissionResponseBinding")
    provider = _text(provider_id, "provider_id").upper()
    if provider not in PROVIDERS:
        raise ProviderCoreError("unknown provider")
    if response_binding.provider.upper() != provider:
        raise ProviderCoreError("durable submission provider mismatch")
    normalized_endpoint = _text(endpoint, "endpoint")
    if not normalized_endpoint.startswith("/") or "://" in normalized_endpoint:
        raise ProviderCoreError(
            "submission endpoint must be a canonical provider-relative path"
        )
    if re.fullmatch(r"sha256:[0-9a-f]{64}", prepared_request_sha256) is None:
        raise ProviderCoreError(
            "prepared_request_sha256 must be a canonical SHA-256 digest"
        )
    if response_binding.request_hash != prepared_request_sha256:
        raise ProviderCoreError("durable submission request digest mismatch")
    capabilities = tuple(
        _text(value, "capability_snapshot_id")
        for value in capability_snapshot_ids
    )
    instruments = tuple(
        _text(value, "instrument_version")
        for value in instrument_versions
    )
    if not capabilities or len(set(capabilities)) != len(capabilities):
        raise ProviderCoreError(
            "capability_snapshot_ids must be non-empty and unique"
        )
    if not instruments or len(set(instruments)) != len(instruments):
        raise ProviderCoreError("instrument_versions must be non-empty and unique")

    expected_scope = {
        "endpoint": normalized_endpoint,
        "prepared_request_sha256": prepared_request_sha256,
        "capability_snapshot_ids": list(capabilities),
        "instrument_versions": list(instruments),
    }
    actual_scope = _thaw_json(response_binding.submission_scope)
    if actual_scope != expected_scope:
        raise ProviderCoreError(
            "durable submission scope does not match prepared provider request"
        )

    identity_material = json.dumps(
        {
            "aggregate_id": response_binding.aggregate_id,
            "provider_id": provider,
            "request_sha256": response_binding.request_hash,
            "submission_scope_hash": response_binding.submission_scope_hash,
            "response_sha256": response_binding.response_sha256,
            "sent_at": response_binding.sent_at,
            "endpoint": normalized_endpoint,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    evidence_ref = (
        "provider-write:sha256:" + sha256(identity_material).hexdigest()
    )
    return ProviderSubmissionObservation(
        response_binding=response_binding,
        endpoint=normalized_endpoint,
        capability_snapshot_ids=capabilities,
        instrument_versions=instruments,
        evidence_ref=evidence_ref,
        payload=response_binding.payload,
        _observation_token=_SUBMISSION_OBSERVED_RESPONSE_TOKEN,
    )

@dataclass(frozen=True)
class ProviderDefinition:
    provider_id: str
    product_families: tuple[str, ...]
    surfaces: tuple[Surface, ...]
    test_environment_note: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "provider_id", _text(self.provider_id, "provider_id"))
        products = tuple(_text(x, "product family") for x in self.product_families)
        if not products or len(set(products)) != len(products):
            raise ProviderCoreError("provider product families must be non-empty and unique")
        object.__setattr__(self, "product_families", products)
        surfaces = tuple(self.surfaces)
        if not surfaces or len(set(surfaces)) != len(surfaces):
            raise ProviderCoreError("provider surfaces must be non-empty and unique")
        object.__setattr__(self, "surfaces", surfaces)
        object.__setattr__(
            self, "test_environment_note", _text(self.test_environment_note, "test_environment_note")
        )


PROVIDERS: Mapping[str, ProviderDefinition] = {
    "BYBIT": ProviderDefinition(
        "BYBIT",
        ("SPOT", "MARGIN", "LINEAR_DERIVATIVES", "INVERSE_DERIVATIVES", "OPTIONS"),
        tuple(Surface),
        "Separate documented test/demo/live surfaces must be qualified per product.",
    ),
    "KRAKEN": ProviderDefinition(
        "KRAKEN",
        ("SPOT", "MARGIN", "DERIVATIVES"),
        tuple(Surface),
        "Spot and derivatives are separate API families; derivatives demo does not prove spot sandbox parity.",
    ),
    "WHITEBIT": ProviderDefinition(
        "WHITEBIT",
        ("SPOT", "COLLATERAL", "FUTURES"),
        tuple(Surface),
        "Recorded fixtures and any official test facility precede separately authorized live probes.",
    ),
    "BINANCE": ProviderDefinition(
        "BINANCE",
        ("SPOT", "MARGIN", "USD_M", "COIN_M", "OPTIONS"),
        tuple(Surface),
        "Product families use separate test facilities and must not share assumed filters or order semantics.",
    ),
    "IBKR": ProviderDefinition(
        "IBKR",
        ("EQUITIES", "FUTURES", "OPTIONS", "FX", "OTHER_ENTITLED"),
        tuple(Surface),
        "Paper account, data entitlements and session lifecycle must be qualified on the selected API.",
    ),
    "ALPACA": ProviderDefinition(
        "ALPACA",
        ("EQUITIES", "CRYPTO", "OPTIONS"),
        tuple(Surface),
        "Paper credentials are distinct; paper execution does not establish live execution realism.",
    ),
}


REQUIRED_QUALIFICATION_CASES = frozenset(
    {
        "metadata",
        "authentication",
        "clock",
        "quota",
        "stream_gap_reconnect",
        "market_order",
        "limit_order",
        "partial_fill",
        "cancel_fill_race",
        "rejection",
        "timeout_after_send",
        "duplicate_event",
        "lost_event",
        "terminal_correction",
        "account_mode_change",
        "manual_activity",
        "snapshot_reconciliation",
        "secret_redaction",
    }
)


@dataclass(frozen=True)
class QualificationEvidence:
    provider_id: str
    product_family: str
    environment: str
    adapter_code_sha: str
    documentation_ref: str
    observed_at: datetime
    expires_at: datetime
    passed_cases: frozenset[str]
    unsupported_features: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        provider = _text(self.provider_id, "provider_id").upper()
        if provider not in PROVIDERS:
            raise ProviderCoreError("unknown provider")
        object.__setattr__(self, "provider_id", provider)
        family = _text(self.product_family, "product_family")
        if family not in PROVIDERS[provider].product_families:
            raise ProviderCoreError("product family is not declared for provider")
        object.__setattr__(self, "product_family", family)
        object.__setattr__(
            self,
            "environment",
            _text(self.environment, "environment").upper(),
        )
        object.__setattr__(self, "adapter_code_sha", _code_sha(self.adapter_code_sha))
        object.__setattr__(self, "documentation_ref", _text(self.documentation_ref, "documentation_ref"))
        observed = _utc(self.observed_at, "observed_at")
        expires = _utc(self.expires_at, "expires_at")
        if expires <= observed:
            raise ProviderCoreError("qualification expiry must be after observation")
        object.__setattr__(self, "observed_at", observed)
        object.__setattr__(self, "expires_at", expires)
        cases = frozenset(_text(x, "passed case") for x in self.passed_cases)
        object.__setattr__(self, "passed_cases", cases)
        object.__setattr__(
            self,
            "unsupported_features",
            tuple(sorted({_text(x, "unsupported feature") for x in self.unsupported_features})),
        )

    def status(self, *, now: datetime, exact_code_sha: str) -> str:
        point = _utc(now, "now")
        if _code_sha(exact_code_sha, "exact_code_sha") != self.adapter_code_sha:
            return "CODE_MISMATCH"
        if point < self.observed_at:
            return "FUTURE_EVIDENCE"
        if point >= self.expires_at:
            return "EXPIRED"
        missing = REQUIRED_QUALIFICATION_CASES - self.passed_cases
        if missing:
            return "INCOMPLETE"
        if self.environment == "LIVE":
            return "LIVE_REQUIRES_BOUNDED_REAL"
        return "QUALIFIED_FOR_NONLIVE"


@dataclass
class QuotaBucket:
    capacity: Decimal
    recovery_reserve: Decimal
    used: Decimal = Decimal("0")

    def __post_init__(self) -> None:
        self.capacity = _decimal(self.capacity, "capacity", non_negative=True)
        self.recovery_reserve = _decimal(
            self.recovery_reserve, "recovery_reserve", non_negative=True
        )
        self.used = _decimal(self.used, "used", non_negative=True)
        if self.recovery_reserve > self.capacity:
            raise ProviderCoreError("recovery reserve cannot exceed capacity")
        if self.used > self.capacity:
            raise ProviderCoreError("used quota cannot exceed capacity")

    def available(self) -> Decimal:
        return self.capacity - self.used

    def acquire(self, cost, *, purpose: Literal["RECOVERY", "TRADING", "RESEARCH"]) -> None:
        amount = _decimal(cost, "quota cost", non_negative=True)
        if purpose not in {"RECOVERY", "TRADING", "RESEARCH"}:
            raise ProviderCoreError("unknown quota purpose")
        if amount == 0:
            return
        remaining = self.available()
        if amount > remaining:
            raise ProviderCoreError("provider quota exhausted")
        if purpose != "RECOVERY" and remaining - amount < self.recovery_reserve:
            raise ProviderCoreError("recovery quota reserve is protected")
        self.used += amount

    def release(self, cost) -> None:
        amount = _decimal(cost, "quota cost", non_negative=True)
        if amount > self.used:
            raise ProviderCoreError("cannot release more quota than was acquired")
        self.used -= amount

    def reset(self) -> None:
        self.used = Decimal("0")


@dataclass(frozen=True)
class ClockGuard:
    maximum_absolute_skew: timedelta

    def __post_init__(self) -> None:
        if not isinstance(self.maximum_absolute_skew, timedelta) or self.maximum_absolute_skew <= timedelta(0):
            raise ProviderCoreError("maximum_absolute_skew must be positive")

    def require_safe(self, *, host_time: datetime, provider_time: datetime) -> None:
        host = _utc(host_time, "host_time")
        provider = _utc(provider_time, "provider_time")
        if abs(host - provider) > self.maximum_absolute_skew:
            raise ProviderCoreError("provider authentication clock skew exceeds safe bound")


@dataclass(frozen=True)
class WriteOutcome:
    status: Literal["NOT_SENT", "ACKNOWLEDGED", "REJECTED", "UNKNOWN"]
    retry_same_economic_action: bool
    reconciliation_required: bool

    def __post_init__(self) -> None:
        if self.status not in {"NOT_SENT", "ACKNOWLEDGED", "REJECTED", "UNKNOWN"}:
            raise ProviderCoreError("unsupported write outcome status")
        if type(self.retry_same_economic_action) is not bool:
            raise ProviderCoreError("retry_same_economic_action must be boolean")
        if type(self.reconciliation_required) is not bool:
            raise ProviderCoreError("reconciliation_required must be boolean")
        expected = {
            "NOT_SENT": (True, False),
            "ACKNOWLEDGED": (False, False),
            "REJECTED": (False, False),
            "UNKNOWN": (False, True),
        }[self.status]
        actual = (
            self.retry_same_economic_action,
            self.reconciliation_required,
        )
        if actual != expected:
            raise ProviderCoreError(
                "write outcome flags contradict the canonical send-state invariant"
            )


def classify_write_outcome(
    *,
    transport_started: bool,
    provider_acknowledged: bool,
    provider_rejected: bool,
) -> WriteOutcome:
    """Classify write ambiguity without inventing a retry after possible send."""

    if provider_acknowledged and provider_rejected:
        raise ProviderCoreError("write cannot be both acknowledged and rejected")
    if not transport_started:
        if provider_acknowledged or provider_rejected:
            raise ProviderCoreError("provider result cannot predate transport")
        return WriteOutcome("NOT_SENT", True, False)
    if provider_acknowledged:
        return WriteOutcome("ACKNOWLEDGED", False, False)
    if provider_rejected:
        return WriteOutcome("REJECTED", False, False)
    return WriteOutcome("UNKNOWN", False, True)


def provider_definition(provider_id: str) -> ProviderDefinition:
    key = _text(provider_id, "provider_id").upper()
    try:
        return PROVIDERS[key]
    except KeyError as error:
        raise ProviderCoreError("unknown provider") from error
