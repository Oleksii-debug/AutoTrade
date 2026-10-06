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
import weakref

from autotrade_numeric.exact_decimal import (
    ExactDecimalError,
    exact_add,
    exact_subtract,
    parse_bounded_exact_decimal,
    parse_bounded_json_integer_token,
    parse_bounded_json_number_token,
)

from .capabilities import CapabilitySnapshot
from .dispatch import (
    SubmissionResponseBinding,
    submission_response_binding_projection,
)
from .provider_response_limits import require_provider_json_depth


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
    # The single installed neutral exact-number TCB admits provider/domain
    # presentation before Decimal construction or financial use. Do not grow a
    # second adapter-local resource policy or coerce polymorphic authority.
    try:
        result = parse_bounded_exact_decimal(value)
    except (ExactDecimalError, TypeError) as error:
        raise ProviderCoreError(f"{name} must be a bounded finite decimal") from error
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

    # Consume one shared #652 byte/depth resource budget before recursive
    # JSON materialization; avoid retaining the raw helper error context.
    resource_failure = False
    try:
        require_provider_json_depth(raw)
    except ValueError:
        resource_failure = True
    if resource_failure:
        raise ProviderCoreError(
            "provider response exceeds maximum JSON depth or resource budget"
        )

    def no_duplicate_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ProviderCoreError(
                    "provider response contains duplicate JSON keys"
                )
            result[key] = value
        return result

    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        text = None
    if text is None:
        raise ProviderCoreError(
            "provider response must be exact UTF-8 JSON bytes"
        )

    parse_failure = None
    try:
        decoded = json.loads(
            text,
            object_pairs_hook=no_duplicate_keys,
            parse_float=parse_bounded_json_number_token,
            parse_int=parse_bounded_json_integer_token,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                ProviderCoreError(
                    "provider response contains non-finite JSON constant"
                )
            ),
        )
    except ProviderCoreError:
        raise
    except ExactDecimalError:
        parse_failure = "numeric"
    except json.JSONDecodeError:
        parse_failure = "json"
    except RecursionError:
        parse_failure = "depth"

    # Translate after leaving the parser handler. Merely suppressing display
    # chaining would still leave raw parser state reachable via __context__.
    if parse_failure == "depth":
        raise ProviderCoreError(
            "provider response exceeds maximum JSON depth"
        )
    if parse_failure == "numeric":
        raise ProviderCoreError(
            "provider response contains invalid or oversized exact JSON number"
        )
    if parse_failure == "json":
        raise ProviderCoreError(
            "provider response must be exact UTF-8 JSON bytes"
        )
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
    ) -> None:
        self.query_binding.require_scope(
            provider_id=provider_id,
            surface=surface,
            endpoint=endpoint,
            account_id=account_id,
            environment=environment,
        )


def observe_authenticated_json_response(
    *,
    query_binding: AuthenticatedReadQueryBinding,
    http_status: int,
    response_bytes: bytes,
    observed_at: datetime,
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
    response_digest = "sha256:" + sha256(response_bytes).hexdigest()
    identity_material = (
        query_binding.query_digest
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


def _install_provider_submission_observation_authority(binding_projection):
    """Mint and verify write observations only from durable binding authority."""

    observation_type = ProviderSubmissionObservation
    binding_type = SubmissionResponseBinding
    error_type = ProviderCoreError
    canonical_type = type
    canonical_type_setattr = canonical_type.__setattr__
    canonical_type_getattribute = canonical_type.__getattribute__
    canonical_id = id
    canonical_tuple = tuple
    canonical_len = len
    canonical_frozenset = frozenset
    canonical_str = str
    canonical_dict = dict
    canonical_object = object
    object_getattribute = canonical_object.__getattribute__
    object_setattr = canonical_object.__setattr__
    mapping_proxy_type = MappingProxyType
    canonical_binding_projection = binding_projection
    binding_projection_code = binding_projection.__code__
    canonical_decode = _decode_exact_json
    decode_code = canonical_decode.__code__
    canonical_sha256 = sha256
    canonical_json_module = json
    canonical_json_dumps = json.dumps
    canonical_re_module = re
    digest_pattern = re.compile(r"^sha256:[0-9a-f]{64}$")
    evidence_pattern = re.compile(r"^provider-write:sha256:[0-9a-f]{64}$")
    canonical_weakref_module = weakref
    canonical_weakref_ref = weakref.ref
    provider_ids = canonical_frozenset(PROVIDERS)
    original_require_scope = ProviderSubmissionObservation.require_scope
    observation_require_scope_code = None
    observation_getattribute = None

    states: dict[int, tuple[object, tuple[object, ...]]] = {}
    field_names = (
        "response_binding",
        "endpoint",
        "capability_snapshot_ids",
        "instrument_versions",
        "evidence_ref",
        "payload",
    )
    expected_instance_fields = canonical_frozenset(field_names)
    sensitive_observation_fields = canonical_frozenset(
        (
            "response_binding",
            "provider_id",
            "account_id",
            "environment",
            "client_order_id",
            "response_sha256",
            "observed_at",
            "request_sha256",
            "endpoint",
            "capability_snapshot_ids",
            "instrument_versions",
            "evidence_ref",
            "payload",
        )
    )

    def authority_changed():
        raise error_type("provider submission observation authority is unavailable")

    def implementation_changed():
        if (
            ProviderSubmissionObservation is not observation_type
            or SubmissionResponseBinding is not binding_type
            or ProviderCoreError is not error_type
            or type is not canonical_type
            or canonical_type.__setattr__ is not canonical_type_setattr
            or canonical_type.__getattribute__ is not canonical_type_getattribute
            or id is not canonical_id
            or tuple is not canonical_tuple
            or len is not canonical_len
            or frozenset is not canonical_frozenset
            or str is not canonical_str
            or dict is not canonical_dict
            or object is not canonical_object
            or MappingProxyType is not mapping_proxy_type
            or submission_response_binding_projection
            is not canonical_binding_projection
            or canonical_binding_projection.__code__ is not binding_projection_code
            or _decode_exact_json is not canonical_decode
            or canonical_decode.__code__ is not decode_code
            or sha256 is not canonical_sha256
            or json is not canonical_json_module
            or json.dumps is not canonical_json_dumps
            or re is not canonical_re_module
            or weakref is not canonical_weakref_module
            or weakref.ref is not canonical_weakref_ref
            or observation_getattribute is None
            or canonical_type_getattribute(observation_type, "__getattribute__")
            is not observation_getattribute
            or observation_require_scope_code is None
            or canonical_type_getattribute(observation_type, "require_scope")
            is not observation_require_scope
            or observation_require_scope.__code__ is not observation_require_scope_code
        ):
            authority_changed()

    def canonical_text(value, name):
        if canonical_type(value) is not canonical_str:
            raise error_type(f"{name} must be exact text")
        normalized = value.strip()
        if not normalized or normalized != value:
            raise error_type(f"{name} must be canonical text")
        return normalized

    def canonical_text_tuple(value, name):
        if canonical_type(value) is not canonical_tuple or not value:
            raise error_type(f"{name} must be a non-empty exact tuple")
        normalized = canonical_tuple(
            canonical_text(item, name)
            for item in value
        )
        if canonical_len(canonical_frozenset(normalized)) != canonical_len(normalized):
            raise error_type(f"{name} must be unique")
        return normalized

    def raw_snapshot(value):
        try:
            state = object_getattribute(value, "__dict__")
        except (AttributeError, TypeError):
            authority_changed()
        if canonical_type(state) is not canonical_dict:
            authority_changed()
        if canonical_frozenset(state) != expected_instance_fields:
            authority_changed()
        return canonical_tuple(
            object_getattribute(value, name)
            for name in field_names
        )

    def prune():
        for object_id, (value_ref, _snapshot) in canonical_tuple(states.items()):
            if value_ref() is None:
                states.pop(object_id, None)

    def register(value):
        implementation_changed()
        if canonical_type(value) is not observation_type:
            authority_changed()
        current = raw_snapshot(value)
        canonical_binding_projection(current[0])
        if canonical_type(current[1]) is not canonical_str:
            authority_changed()
        if canonical_type(current[2]) is not canonical_tuple:
            authority_changed()
        if canonical_type(current[3]) is not canonical_tuple:
            authority_changed()
        if canonical_type(current[4]) is not canonical_str:
            authority_changed()
        prune()
        object_id = canonical_id(value)
        previous = states.get(object_id)
        if previous is not None and previous[0]() is not None:
            authority_changed()
        states[object_id] = (canonical_weakref_ref(value), current)

    def require_canonical_provider_submission_observation(value):
        implementation_changed()
        if canonical_type(value) is not observation_type:
            authority_changed()
        prune()
        state = states.get(canonical_id(value))
        if state is None or state[0]() is not value:
            authority_changed()
        expected = state[1]
        current = raw_snapshot(value)
        if current[0] is not expected[0]:
            authority_changed()
        canonical_binding_projection(current[0])
        if (
            canonical_type(current[1]) is not canonical_str
            or current[1] != expected[1]
            or current[2] is not expected[2]
            or current[3] is not expected[3]
            or canonical_type(current[4]) is not canonical_str
            or current[4] != expected[4]
            or current[5] is not expected[5]
        ):
            authority_changed()
        return value

    def provider_submission_observation_projection(value):
        require_canonical_provider_submission_observation(value)
        current = raw_snapshot(value)
        binding = canonical_binding_projection(current[0])
        return mapping_proxy_type(
            {
                "response_binding": current[0],
                "attempt_id": binding["attempt_id"],
                "aggregate_id": binding["aggregate_id"],
                "provider_id": binding["provider"].upper(),
                "request_sha256": binding["request_hash"],
                "client_order_id": binding["client_order_id"],
                "environment": binding["environment"],
                "account_id": binding["account_id"],
                "sent_at": binding["sent_at"],
                "observed_at": binding["sent_at"],
                "response_sha256": binding["response_sha256"],
                "http_status": binding["http_status"],
                "submission_scope": binding["submission_scope"],
                "submission_scope_hash": binding["submission_scope_hash"],
                "endpoint": current[1],
                "capability_snapshot_ids": current[2],
                "instrument_versions": current[3],
                "evidence_ref": current[4],
                "payload": current[5],
            }
        )

    def observation_require_scope(
        value,
        *,
        provider_id,
        endpoint,
        prepared_request_sha256,
        capability_snapshot_ids,
        instrument_versions,
        account_id=None,
        environment=None,
        client_order_id=None,
    ):
        projection = provider_submission_observation_projection(value)
        if canonical_text(provider_id, "provider_id").upper() != projection["provider_id"]:
            raise error_type("provider-write provenance provider mismatch")
        if canonical_text(endpoint, "endpoint") != projection["endpoint"]:
            raise error_type("provider-write provenance endpoint mismatch")
        if (
            canonical_text(prepared_request_sha256, "prepared_request_sha256")
            != projection["request_sha256"]
        ):
            raise error_type("provider-write provenance request digest mismatch")
        if (
            canonical_text_tuple(capability_snapshot_ids, "capability_snapshot_ids")
            != projection["capability_snapshot_ids"]
        ):
            raise error_type("provider-write provenance capability mismatch")
        if (
            canonical_text_tuple(instrument_versions, "instrument_versions")
            != projection["instrument_versions"]
        ):
            raise error_type("provider-write provenance instrument mismatch")
        if (
            account_id is not None
            and canonical_text(account_id, "account_id") != projection["account_id"]
        ):
            raise error_type("provider-write provenance account mismatch")
        if (
            environment is not None
            and canonical_text(environment, "environment").upper()
            != projection["environment"]
        ):
            raise error_type("provider-write provenance environment mismatch")
        if (
            client_order_id is not None
            and canonical_text(client_order_id, "client_order_id")
            != projection["client_order_id"]
        ):
            raise error_type("provider-write provenance client-order mismatch")

    def observation_getattribute(value, name):
        if canonical_type(name) is canonical_str:
            if name == "require_scope":
                implementation_changed()
            if name in sensitive_observation_fields:
                return provider_submission_observation_projection(value)[name]
        return object_getattribute(value, name)

    observation_require_scope_code = observation_require_scope.__code__
    canonical_type_setattr(
        observation_type,
        "require_scope",
        observation_require_scope,
    )
    canonical_type_setattr(
        observation_type,
        "__getattribute__",
        observation_getattribute,
    )

    def observe_submission_json_response(
        *,
        response_binding: SubmissionResponseBinding,
        provider_id: str,
        endpoint: str,
        prepared_request_sha256: str,
        capability_snapshot_ids: tuple[str, ...],
        instrument_versions: tuple[str, ...],
    ) -> ProviderSubmissionObservation:
        implementation_changed()
        binding = canonical_binding_projection(response_binding)

        provider = canonical_text(provider_id, "provider_id").upper()
        if provider not in provider_ids:
            raise error_type("unknown provider")
        if binding["provider"].upper() != provider:
            raise error_type("durable submission provider mismatch")

        normalized_endpoint = canonical_text(endpoint, "endpoint")
        if not normalized_endpoint.startswith("/") or "://" in normalized_endpoint:
            raise error_type(
                "submission endpoint must be a canonical provider-relative path"
            )

        request_sha = canonical_text(
            prepared_request_sha256,
            "prepared_request_sha256",
        )
        if digest_pattern.fullmatch(request_sha) is None:
            raise error_type(
                "prepared_request_sha256 must be a canonical SHA-256 digest"
            )
        if binding["request_hash"] != request_sha:
            raise error_type("durable submission request digest mismatch")

        capabilities = canonical_text_tuple(
            capability_snapshot_ids,
            "capability_snapshot_ids",
        )
        instruments = canonical_text_tuple(
            instrument_versions,
            "instrument_versions",
        )
        scope = binding["submission_scope"]
        if canonical_type(scope) is not mapping_proxy_type:
            authority_changed()
        expected_keys = canonical_frozenset(
            (
                "endpoint",
                "prepared_request_sha256",
                "capability_snapshot_ids",
                "instrument_versions",
            )
        )
        if canonical_frozenset(scope.keys()) != expected_keys:
            raise error_type(
                "durable submission scope does not match prepared provider request"
            )
        if (
            scope["endpoint"] != normalized_endpoint
            or scope["prepared_request_sha256"] != request_sha
            or canonical_type(scope["capability_snapshot_ids"]) is not canonical_tuple
            or scope["capability_snapshot_ids"] != capabilities
            or canonical_type(scope["instrument_versions"]) is not canonical_tuple
            or scope["instrument_versions"] != instruments
        ):
            raise error_type(
                "durable submission scope does not match prepared provider request"
            )

        identity_material = canonical_json_dumps(
            {
                "aggregate_id": binding["aggregate_id"],
                "provider_id": provider,
                "request_sha256": binding["request_hash"],
                "submission_scope_hash": binding["submission_scope_hash"],
                "response_sha256": binding["response_sha256"],
                "sent_at": binding["sent_at"],
                "endpoint": normalized_endpoint,
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        evidence_ref = (
            "provider-write:sha256:"
            + canonical_sha256(identity_material).hexdigest()
        )
        if evidence_pattern.fullmatch(evidence_ref) is None:
            authority_changed()

        payload = canonical_decode(binding["response_bytes"])
        observation = canonical_object.__new__(observation_type)
        object_setattr(observation, "response_binding", response_binding)
        object_setattr(observation, "endpoint", normalized_endpoint)
        object_setattr(observation, "capability_snapshot_ids", capabilities)
        object_setattr(observation, "instrument_versions", instruments)
        object_setattr(observation, "evidence_ref", evidence_ref)
        object_setattr(observation, "payload", payload)
        register(observation)
        return observation

    return (
        observe_submission_json_response,
        require_canonical_provider_submission_observation,
        provider_submission_observation_projection,
    )


(
    observe_submission_json_response,
    require_canonical_provider_submission_observation,
    provider_submission_observation_projection,
) = _install_provider_submission_observation_authority(
    submission_response_binding_projection
)
del _install_provider_submission_observation_authority


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
        try:
            return exact_subtract(self.capacity, self.used)
        except ExactDecimalError as error:
            raise ProviderCoreError("provider quota exceeds exact arithmetic envelope") from error

    def acquire(self, cost, *, purpose: Literal["RECOVERY", "TRADING", "RESEARCH"]) -> None:
        # Financial-purpose identity must not dispatch polymorphic equality.
        if type(purpose) is not str or purpose not in {
            "RECOVERY", "TRADING", "RESEARCH"
        }:
            raise ProviderCoreError("unknown quota purpose")
        amount = _decimal(cost, "quota cost", non_negative=True)
        if amount == 0:
            return
        # Resource authority must not depend on ambient Decimal precision.
        # Calculate and validate the entire next state before mutating used.
        try:
            next_used = exact_add(self.used, amount)
            if next_used > self.capacity:
                raise ProviderCoreError("provider quota exhausted")
            after = exact_subtract(self.capacity, next_used)
        except ExactDecimalError as error:
            raise ProviderCoreError("provider quota exceeds exact arithmetic envelope") from error
        if purpose != "RECOVERY" and after < self.recovery_reserve:
            raise ProviderCoreError("recovery quota reserve is protected")
        self.used = next_used

    def release(self, cost) -> None:
        amount = _decimal(cost, "quota cost", non_negative=True)
        if amount > self.used:
            raise ProviderCoreError("cannot release more quota than was acquired")
        try:
            next_used = exact_subtract(self.used, amount)
        except ExactDecimalError as error:
            raise ProviderCoreError("provider quota exceeds exact arithmetic envelope") from error
        self.used = next_used

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
