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
import weakref
from typing import Iterable, Literal, Mapping
import re

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


_SUBMISSION_OBSERVED_RESPONSE_TOKEN = object()


def _utc_text(value: datetime, name: str) -> str:
    return _utc(value, name).isoformat().replace("+00:00", "Z")


def _canonical_query_values(
    values: Mapping[str, str] | None,
) -> Mapping[str, str]:
    if values is None:
        return MappingProxyType({})
    if type(values) not in {dict, MappingProxyType}:
        raise ProviderCoreError("query must be an exact inert mapping")
    normalized: dict[str, str] = {}
    for raw_key, raw_value in values.items():
        if type(raw_key) is not str or not raw_key or raw_key != raw_key.strip():
            raise ProviderCoreError(
                "authenticated-read query keys must be canonical strings"
            )
        key = raw_key
        if key in normalized:
            raise ProviderCoreError("query keys must be unique after normalization")
        if type(raw_value) is not str or raw_value != raw_value.strip():
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


@dataclass(frozen=True, init=False)
class AuthenticatedReadQueryBinding:
    """Credential/capability scope minted only by verified preparation."""

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

    def __init__(self, *_args, **_kwargs) -> None:
        raise ProviderCoreError(
            "authenticated-read bindings must come from verified capability preparation"
        )

    def require_scope(
        self,
        *,
        provider_id: str,
        surface: Surface,
        endpoint: str,
        account_id: str | None = None,
        environment: str | None = None,
    ) -> None:
        _require_authenticated_read_query_binding_authority(self)
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


def _prepare_authenticated_read_query_impl(
    *,
    capability: CapabilitySnapshot,
    surface: Surface,
    endpoint: str,
    query: Mapping[str, str] | None,
    at: datetime,
    permission_scope: str = "ORDER.READ",
    _register_authority,
) -> AuthenticatedReadQueryBinding:
    """Prepare one authenticated query from canonical capability identity.

    Account/environment are intentionally not parameters: they are inherited
    from the VERIFIED capability snapshot before any provider response exists.
    """

    if type(capability) is not CapabilitySnapshot:
        raise TypeError("capability must be exact CapabilitySnapshot")
    if type(surface) is not Surface:
        raise TypeError("surface must be exact Surface")
    if surface not in {Surface.AUTHENTICATED_READ, Surface.ACTIVITIES}:
        raise ProviderCoreError(
            "authenticated-read binding requires AUTHENTICATED_READ or ACTIVITIES"
        )
    if type(at) is not datetime or type(at.tzinfo) is not timezone:
        raise ProviderCoreError("at must be an exact timezone-aware datetime")
    point = _utc(at, "at")
    if (
        type(permission_scope) is not str
        or not permission_scope
        or permission_scope != permission_scope.strip()
    ):
        raise ProviderCoreError("permission_scope must be a canonical string")
    scope = permission_scope
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
    if (
        type(endpoint) is not str
        or not endpoint
        or endpoint != endpoint.strip()
    ):
        raise ProviderCoreError(
            "authenticated-read endpoint must be a canonical string"
        )
    normalized_endpoint = endpoint
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
    binding = object.__new__(AuthenticatedReadQueryBinding)
    object.__setattr__(binding, "provider_id", provider)
    object.__setattr__(binding, "account_id", capability.account_id)
    object.__setattr__(binding, "entity_id", capability.entity_id)
    object.__setattr__(binding, "environment", capability.environment)
    object.__setattr__(binding, "capability_snapshot_id", capability.snapshot_id)
    object.__setattr__(binding, "instrument_version", capability.instrument_version)
    object.__setattr__(binding, "surface", surface)
    object.__setattr__(binding, "endpoint", normalized_endpoint)
    object.__setattr__(binding, "query", normalized_query)
    object.__setattr__(binding, "prepared_at", prepared_at)
    object.__setattr__(binding, "permission_scope", scope)
    object.__setattr__(
        binding,
        "query_digest",
        "sha256:" + sha256(encoded).hexdigest(),
    )
    _register_authority(binding)
    return binding


@dataclass(frozen=True, init=False)
class ProviderResponseObservation:
    """Exact response bytes minted only by the canonical observation path."""

    query_binding: AuthenticatedReadQueryBinding
    observed_at: str
    http_status: int
    response_sha256: str
    evidence_ref: str
    payload: object

    def __init__(self, *_args, **_kwargs) -> None:
        raise ProviderCoreError(
            "provider response observations must come from exact response bytes"
        )

    @property
    def provider_id(self) -> str:
        _require_provider_response_observation_authority(self)
        return self.query_binding.provider_id

    @property
    def account_id(self) -> str:
        _require_provider_response_observation_authority(self)
        return self.query_binding.account_id

    @property
    def environment(self) -> str:
        _require_provider_response_observation_authority(self)
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
        _require_provider_response_observation_authority(self)
        self.query_binding.require_scope(
            provider_id=provider_id,
            surface=surface,
            endpoint=endpoint,
            account_id=account_id,
            environment=environment,
        )


def _install_authenticated_provider_read_authority():
    """Retain provider-read construction authority outside frozen dataclass state."""

    query_states: dict[int, tuple[weakref.ReferenceType, tuple[object, ...]]] = {}
    response_states: dict[int, tuple[weakref.ReferenceType, tuple[object, ...]]] = {}
    canonical_type = type
    canonical_id = id
    canonical_str = str
    canonical_int = int
    canonical_weakref_ref = weakref.ref
    object_getattribute = object.__getattribute__
    mapping_proxy_type = MappingProxyType
    canonical_text = _text

    def prune(states: dict[int, tuple[weakref.ReferenceType, tuple[object, ...]]]) -> None:
        for object_id, (value_ref, _snapshot) in tuple(states.items()):
            if value_ref() is None:
                states.pop(object_id, None)

    def query_snapshot(value: object) -> tuple[object, ...]:
        return (
            object_getattribute(value, "provider_id"),
            object_getattribute(value, "account_id"),
            object_getattribute(value, "entity_id"),
            object_getattribute(value, "environment"),
            object_getattribute(value, "capability_snapshot_id"),
            object_getattribute(value, "instrument_version"),
            object_getattribute(value, "surface"),
            object_getattribute(value, "endpoint"),
            object_getattribute(value, "query"),
            object_getattribute(value, "prepared_at"),
            object_getattribute(value, "permission_scope"),
            object_getattribute(value, "query_digest"),
        )

    def register_query(value: object) -> None:
        if canonical_type(value) is not AuthenticatedReadQueryBinding:
            raise ProviderCoreError(
                "authenticated-read construction authority requires exact binding"
            )
        prune(query_states)
        object_id = canonical_id(value)
        current = query_states.get(object_id)
        if current is not None and current[0]() is not None:
            raise ProviderCoreError(
                "authenticated-read construction authority identity collision"
            )
        query_states[object_id] = (
            canonical_weakref_ref(value),
            query_snapshot(value),
        )

    def require_query(value: object) -> tuple[object, ...]:
        if canonical_type(value) is not AuthenticatedReadQueryBinding:
            raise ProviderCoreError(
                "authenticated-read construction authority requires exact binding"
            )
        prune(query_states)
        state = query_states.get(canonical_id(value))
        if state is None or state[0]() is not value:
            raise ProviderCoreError(
                "authenticated-read construction authority is unavailable"
            )
        expected = state[1]
        current = query_snapshot(value)
        for index in (0, 1, 2, 3, 4, 5, 7, 9, 10, 11):
            if (
                canonical_type(current[index]) is not canonical_str
                or current[index] != expected[index]
            ):
                raise ProviderCoreError(
                    "authenticated-read binding changed after preparation"
                )
        if (
            canonical_type(current[6]) is not Surface
            or current[6] is not expected[6]
            or current[8] is not expected[8]
        ):
            raise ProviderCoreError(
                "authenticated-read binding changed after preparation"
            )
        return expected

    def response_snapshot(value: object) -> tuple[object, ...]:
        return (
            object_getattribute(value, "query_binding"),
            object_getattribute(value, "observed_at"),
            object_getattribute(value, "http_status"),
            object_getattribute(value, "response_sha256"),
            object_getattribute(value, "evidence_ref"),
            object_getattribute(value, "payload"),
        )

    def register_response(value: object) -> None:
        if canonical_type(value) is not ProviderResponseObservation:
            raise ProviderCoreError(
                "provider-response construction authority requires exact observation"
            )
        current = response_snapshot(value)
        require_query(current[0])
        prune(response_states)
        object_id = canonical_id(value)
        previous = response_states.get(object_id)
        if previous is not None and previous[0]() is not None:
            raise ProviderCoreError(
                "provider-response construction authority identity collision"
            )
        response_states[object_id] = (
            canonical_weakref_ref(value),
            current,
        )

    def require_response(value: object) -> tuple[object, ...]:
        if canonical_type(value) is not ProviderResponseObservation:
            raise ProviderCoreError(
                "provider-response construction authority requires exact observation"
            )
        prune(response_states)
        state = response_states.get(canonical_id(value))
        if state is None or state[0]() is not value:
            raise ProviderCoreError(
                "provider-response construction authority is unavailable"
            )
        expected = state[1]
        current = response_snapshot(value)
        if current[0] is not expected[0]:
            raise ProviderCoreError(
                "provider response changed after exact-byte observation"
            )
        require_query(current[0])
        if (
            canonical_type(current[1]) is not canonical_str
            or current[1] != expected[1]
            or canonical_type(current[2]) is not canonical_int
            or current[2] != expected[2]
            or canonical_type(current[3]) is not canonical_str
            or current[3] != expected[3]
            or canonical_type(current[4]) is not canonical_str
            or current[4] != expected[4]
            or current[5] is not expected[5]
        ):
            raise ProviderCoreError(
                "provider response changed after exact-byte observation"
            )
        return expected

    def provider_response_observation_projection(value: object):
        response = require_response(value)
        query = require_query(response[0])
        return mapping_proxy_type(
            {
                "query_binding": response[0],
                "provider_id": query[0],
                "account_id": query[1],
                "entity_id": query[2],
                "environment": query[3],
                "capability_snapshot_id": query[4],
                "instrument_version": query[5],
                "surface": query[6],
                "endpoint": query[7],
                "query": query[8],
                "prepared_at": query[9],
                "permission_scope": query[10],
                "query_digest": query[11],
                "observed_at": response[1],
                "http_status": response[2],
                "response_sha256": response[3],
                "evidence_ref": response[4],
                "payload": response[5],
            }
        )

    def provider_response_observation_require_scope(
        value: object,
        *,
        provider_id: str,
        surface: Surface,
        endpoint: str,
        account_id: str | None = None,
        environment: str | None = None,
    ) -> Mapping[str, object]:
        projection = provider_response_observation_projection(value)
        if canonical_type(surface) is not Surface:
            raise TypeError("surface must be exact Surface")
        if (
            canonical_text(provider_id, "provider_id").upper()
            != projection["provider_id"]
        ):
            raise ProviderCoreError("provider-read provenance provider mismatch")
        if surface is not projection["surface"]:
            raise ProviderCoreError("provider-read provenance surface mismatch")
        if canonical_text(endpoint, "endpoint") != projection["endpoint"]:
            raise ProviderCoreError("provider-read provenance endpoint mismatch")
        if (
            account_id is not None
            and canonical_text(account_id, "account_id") != projection["account_id"]
        ):
            raise ProviderCoreError("provider-read provenance account mismatch")
        if (
            environment is not None
            and canonical_text(environment, "environment").upper()
            != projection["environment"]
        ):
            raise ProviderCoreError("provider-read provenance environment mismatch")
        return projection

    return (
        register_query,
        require_query,
        register_response,
        require_response,
        provider_response_observation_projection,
        provider_response_observation_require_scope,
    )


(
    _register_authenticated_read_query_binding_authority,
    _require_authenticated_read_query_binding_authority,
    _register_provider_response_observation_authority,
    _require_provider_response_observation_authority,
    provider_response_observation_projection,
    provider_response_observation_require_scope,
) = _install_authenticated_provider_read_authority()
del _install_authenticated_provider_read_authority


def _observe_authenticated_json_response_impl(
    *,
    query_binding: AuthenticatedReadQueryBinding,
    http_status: int,
    response_bytes: bytes,
    observed_at: datetime,
    _register_authority,
) -> ProviderResponseObservation:
    if type(query_binding) is not AuthenticatedReadQueryBinding:
        raise TypeError("query_binding must be exact AuthenticatedReadQueryBinding")
    _require_authenticated_read_query_binding_authority(query_binding)
    if (
        isinstance(http_status, bool)
        or not isinstance(http_status, int)
        or http_status < 200
        or http_status > 299
    ):
        raise ProviderCoreError(
            "authenticated provider state requires an HTTP 2xx response"
        )
    if type(observed_at) is not datetime or type(observed_at.tzinfo) is not timezone:
        raise ProviderCoreError(
            "observed_at must be an exact stdlib timezone datetime"
        )
    payload = _decode_exact_json(response_bytes)
    observed = _utc_text(observed_at, "observed_at")
    prepared = datetime.fromisoformat(
        query_binding.prepared_at.replace("Z", "+00:00")
    )
    observed_point = datetime.fromisoformat(observed.replace("Z", "+00:00"))
    if observed_point < prepared:
        raise ProviderCoreError(
            "provider response observation must be canonical UTC at/after query preparation"
        )
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
    observation = object.__new__(ProviderResponseObservation)
    object.__setattr__(observation, "query_binding", query_binding)
    object.__setattr__(observation, "observed_at", observed)
    object.__setattr__(observation, "http_status", http_status)
    object.__setattr__(observation, "response_sha256", response_digest)
    object.__setattr__(observation, "evidence_ref", evidence_ref)
    object.__setattr__(observation, "payload", payload)
    _register_authority(observation)
    return observation




def _bind_authenticated_provider_read_minting(
    prepare_impl,
    observe_impl,
    register_query,
    register_response,
):
    def prepare_authenticated_read_query(
        *,
        capability: CapabilitySnapshot,
        surface: Surface,
        endpoint: str,
        query: Mapping[str, str] | None,
        at: datetime,
        permission_scope: str = "ORDER.READ",
    ) -> AuthenticatedReadQueryBinding:
        return prepare_impl(
            capability=capability,
            surface=surface,
            endpoint=endpoint,
            query=query,
            at=at,
            permission_scope=permission_scope,
            _register_authority=register_query,
        )

    def observe_authenticated_json_response(
        *,
        query_binding: AuthenticatedReadQueryBinding,
        http_status: int,
        response_bytes: bytes,
        observed_at: datetime,
    ) -> ProviderResponseObservation:
        return observe_impl(
            query_binding=query_binding,
            http_status=http_status,
            response_bytes=response_bytes,
            observed_at=observed_at,
            _register_authority=register_response,
        )

    return prepare_authenticated_read_query, observe_authenticated_json_response


(
    prepare_authenticated_read_query,
    observe_authenticated_json_response,
) = _bind_authenticated_provider_read_minting(
    _prepare_authenticated_read_query_impl,
    _observe_authenticated_json_response_impl,
    _register_authenticated_read_query_binding_authority,
    _register_provider_response_observation_authority,
)
del _bind_authenticated_provider_read_minting
del _prepare_authenticated_read_query_impl
del _observe_authenticated_json_response_impl
del _register_authenticated_read_query_binding_authority
del _register_provider_response_observation_authority


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
        submission_response_binding_projection(self.response_binding)
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
        return provider_submission_observation_projection(self)["provider_id"]

    @property
    def account_id(self) -> str:
        return provider_submission_observation_projection(self)["account_id"]

    @property
    def environment(self) -> str:
        return provider_submission_observation_projection(self)["environment"]

    @property
    def client_order_id(self) -> str:
        return provider_submission_observation_projection(self)["client_order_id"]

    @property
    def response_sha256(self) -> str:
        return provider_submission_observation_projection(self)["response_sha256"]

    @property
    def observed_at(self) -> str:
        return provider_submission_observation_projection(self)["sent_at"]

    @property
    def request_sha256(self) -> str:
        return provider_submission_observation_projection(self)["request_sha256"]

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
        projection = provider_submission_observation_projection(self)
        if _text(provider_id, "provider_id").upper() != projection["provider_id"]:
            raise ProviderCoreError("provider-write provenance provider mismatch")
        if _text(endpoint, "endpoint") != projection["endpoint"]:
            raise ProviderCoreError("provider-write provenance endpoint mismatch")
        if prepared_request_sha256 != projection["request_sha256"]:
            raise ProviderCoreError("provider-write provenance request digest mismatch")
        if tuple(capability_snapshot_ids) != projection["capability_snapshot_ids"]:
            raise ProviderCoreError("provider-write provenance capability mismatch")
        if tuple(instrument_versions) != projection["instrument_versions"]:
            raise ProviderCoreError("provider-write provenance instrument mismatch")
        if (
            account_id is not None
            and _text(account_id, "account_id") != projection["account_id"]
        ):
            raise ProviderCoreError("provider-write provenance account mismatch")
        if (
            environment is not None
            and _text(environment, "environment").upper()
            != projection["environment"]
        ):
            raise ProviderCoreError("provider-write provenance environment mismatch")
        if (
            client_order_id is not None
            and _text(client_order_id, "client_order_id")
            != projection["client_order_id"]
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

    binding = submission_response_binding_projection(response_binding)
    if binding["terminal_state"] != "SENT":
        raise ProviderCoreError(
            "provider submission observation requires definitive SENT response"
        )
    if binding["response_encoding"] != "utf-8-json":
        raise ProviderCoreError(
            "provider-write JSON observation requires durable utf-8-json response bytes"
        )
    provider = _text(provider_id, "provider_id").upper()
    if provider not in PROVIDERS:
        raise ProviderCoreError("unknown provider")
    if binding["provider"].upper() != provider:
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
    if binding["request_hash"] != prepared_request_sha256:
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
    actual_scope = _thaw_json(binding["submission_scope"])
    if type(actual_scope) is not dict:
        raise ProviderCoreError("durable submission scope is non-canonical")
    if any(actual_scope.get(key) != value for key, value in expected_scope.items()):
        raise ProviderCoreError(
            "durable submission scope does not match prepared provider request"
        )

    # Provider-route and financial authority may extend the prepared-request
    # scope. Those extensions remain authenticated by submission_scope_hash and
    # therefore by this observation's evidence_ref; this neutral verifier owns
    # only the prepared-request axes plus cross-layer identities it can prove
    # from the durable response binding itself.
    provider_extension_keys = {"provider_environment"}
    financial_extension_keys = {
        "provider_id",
        "account_id",
        "environment",
        "capability_snapshot_id",
    }
    route_extension_keys = {
        "provider_route_qualification_id",
        "provider_route_capability_snapshot_id",
        "provider_route_decision_journal_sequence_cut",
        "provider_route_provider_environment",
        "provider_route_adapter_code_sha",
        "provider_route_packaged_artifact_digest",
        "provider_route_protocol_id",
        "provider_route_protocol_version",
        "provider_route_entity_policy_id",
        "provider_route_entity_id",
    }
    extension_keys = set(actual_scope) - set(expected_scope)
    allowed_extension_keys = (
        provider_extension_keys | financial_extension_keys | route_extension_keys
    )
    if not extension_keys <= allowed_extension_keys:
        raise ProviderCoreError("durable submission scope has unknown authority axes")

    route_shape_present = bool(
        extension_keys & (financial_extension_keys | route_extension_keys)
    )
    if route_shape_present:
        required_route_shape = (
            provider_extension_keys | financial_extension_keys | route_extension_keys
        )
        if not required_route_shape <= set(actual_scope):
            raise ProviderCoreError(
                "durable financial route submission scope is incomplete"
            )

    financial_scope = {
        "provider_id": provider,
        "account_id": binding["account_id"],
        "environment": binding["environment"],
        "provider_environment": binding["provider_environment"],
        "provider_route_provider_environment": binding["provider_environment"],
    }
    for key, value in financial_scope.items():
        if key in actual_scope and actual_scope[key] != value:
            raise ProviderCoreError(
                "durable submission financial scope does not match response binding"
            )
    for key in (
        "capability_snapshot_id",
        "provider_route_capability_snapshot_id",
    ):
        if key in actual_scope and (
            len(capabilities) != 1 or actual_scope[key] != capabilities[0]
        ):
            raise ProviderCoreError(
                "durable submission capability scope does not match prepared request"
            )

    if "provider_route_provider_environment" in actual_scope:
        if (
            "provider_environment" not in actual_scope
            or actual_scope["provider_route_provider_environment"]
            != actual_scope["provider_environment"]
        ):
            raise ProviderCoreError(
                "durable submission provider-route environment scope mismatch"
            )

    identity_material = json.dumps(
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
        "provider-write:sha256:" + sha256(identity_material).hexdigest()
    )
    return ProviderSubmissionObservation(
        response_binding=response_binding,
        endpoint=normalized_endpoint,
        capability_snapshot_ids=capabilities,
        instrument_versions=instruments,
        evidence_ref=evidence_ref,
        # Never consume SubmissionResponseBinding.payload here: dispatch's
        # transport-only JSON preview is not the exact numeric authority.
        # Reparse the SHA-bound durable bytes through the neutral bounded
        # numeric callbacks before constructing an authenticated observation.
        payload=_decode_exact_json(binding["response_bytes"]),
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


def _install_provider_submission_observation_authority(binding_projection):
    """Mint write observations only from sealed durable response bindings."""

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
    canonical_object = object
    object_getattribute = canonical_object.__getattribute__
    object_setattr = canonical_object.__setattr__
    mapping_proxy_type = MappingProxyType
    canonical_binding_projection = binding_projection
    binding_projection_code = binding_projection.__code__
    canonical_decode = _decode_exact_json
    decode_code = canonical_decode.__code__
    canonical_depth_guard = require_provider_json_depth
    depth_guard_code = canonical_depth_guard.__code__
    canonical_number_parser = parse_bounded_json_number_token
    number_parser_code = canonical_number_parser.__code__
    canonical_integer_parser = parse_bounded_json_integer_token
    integer_parser_code = canonical_integer_parser.__code__
    canonical_freeze_json = _freeze_json
    freeze_json_code = canonical_freeze_json.__code__
    canonical_sha256 = sha256
    canonical_json_module = json
    canonical_json_loads = json.loads
    canonical_json_dumps = json.dumps
    canonical_exact_decimal_error = ExactDecimalError
    canonical_value_error = ValueError
    canonical_unicode_decode_error = UnicodeDecodeError
    canonical_recursion_error = RecursionError
    canonical_bytes = bytes
    canonical_isinstance = isinstance
    canonical_dict = dict
    canonical_list = list
    canonical_decimal = Decimal
    canonical_float = float
    canonical_bool = bool
    canonical_int = int
    canonical_re_module = re
    digest_pattern = re.compile(r"^sha256:[0-9a-f]{64}$")
    evidence_pattern = re.compile(r"^provider-write:sha256:[0-9a-f]{64}$")
    canonical_weakref_module = weakref
    canonical_weakref_ref = weakref.ref
    provider_ids = frozenset(PROVIDERS)
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
    states: dict[int, tuple[object, tuple[object, ...]]] = {}
    observation_require_scope_code = None

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
            or canonical_type_getattribute(observation_type, "__getattribute__")
            is not observation_getattribute
            or canonical_type_getattribute(observation_type, "require_scope")
            is not observation_require_scope
            or observation_require_scope_code is None
            or observation_require_scope.__code__ is not observation_require_scope_code
            or id is not canonical_id
            or tuple is not canonical_tuple
            or len is not canonical_len
            or frozenset is not canonical_frozenset
            or str is not canonical_str
            or object is not canonical_object
            or MappingProxyType is not mapping_proxy_type
            or submission_response_binding_projection
            is not canonical_binding_projection
            or canonical_binding_projection.__code__ is not binding_projection_code
            or _decode_exact_json is not canonical_decode
            or canonical_decode.__code__ is not decode_code
            or require_provider_json_depth is not canonical_depth_guard
            or canonical_depth_guard.__code__ is not depth_guard_code
            or parse_bounded_json_number_token is not canonical_number_parser
            or canonical_number_parser.__code__ is not number_parser_code
            or parse_bounded_json_integer_token is not canonical_integer_parser
            or canonical_integer_parser.__code__ is not integer_parser_code
            or _freeze_json is not canonical_freeze_json
            or canonical_freeze_json.__code__ is not freeze_json_code
            or sha256 is not canonical_sha256
            or json is not canonical_json_module
            or json.loads is not canonical_json_loads
            or json.dumps is not canonical_json_dumps
            or ExactDecimalError is not canonical_exact_decimal_error
            or ValueError is not canonical_value_error
            or UnicodeDecodeError is not canonical_unicode_decode_error
            or RecursionError is not canonical_recursion_error
            or bytes is not canonical_bytes
            or isinstance is not canonical_isinstance
            or dict is not canonical_dict
            or list is not canonical_list
            or Decimal is not canonical_decimal
            or float is not canonical_float
            or bool is not canonical_bool
            or int is not canonical_int
            or re is not canonical_re_module
            or weakref is not canonical_weakref_module
            or weakref.ref is not canonical_weakref_ref
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
        normalized = canonical_tuple(canonical_text(item, name) for item in value)
        if canonical_len(canonical_frozenset(normalized)) != canonical_len(normalized):
            raise error_type(f"{name} must be unique")
        return normalized

    field_names = (
        "response_binding",
        "endpoint",
        "capability_snapshot_ids",
        "instrument_versions",
        "evidence_ref",
        "payload",
    )
    exact_field_names = canonical_frozenset(field_names)

    def raw_snapshot(value):
        state = object_getattribute(value, "__dict__")
        if canonical_type(state) is not canonical_dict:
            authority_changed()
        if canonical_frozenset(state) != exact_field_names:
            authority_changed()
        return canonical_tuple(state[name] for name in field_names)

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
        return expected

    def provider_submission_observation_projection(value):
        current = require_canonical_provider_submission_observation(value)
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
                "response_encoding": binding["response_encoding"],
                "terminal_state": binding["terminal_state"],
                "ambiguity_reason": binding["ambiguity_reason"],
                "retry_disposition": binding["retry_disposition"],
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
        # Scope admission is itself financial/provider authority. Keep it inside
        # the closure-backed registry instead of trusting a replaceable class
        # method or derived property after the observation has been minted.
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
            canonical_text_tuple(
                capability_snapshot_ids,
                "capability_snapshot_ids",
            )
            != projection["capability_snapshot_ids"]
        ):
            raise error_type("provider-write provenance capability mismatch")
        if (
            canonical_text_tuple(
                instrument_versions,
                "instrument_versions",
            )
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
        # Frozen dataclass syntax is not an authority boundary: object.__setattr__
        # can still retarget stored fields. Route every normal read of the
        # authority-bearing observation payload/scope through the external
        # issuance registry so post-mint relabelling fails before consumption.
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
        if binding["terminal_state"] != "SENT":
            raise error_type(
                "provider submission observation requires definitive SENT response"
            )
        if binding["response_encoding"] != "utf-8-json":
            raise error_type(
                "provider-write JSON observation requires durable utf-8-json response bytes"
            )

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
        required_keys = canonical_frozenset(
            (
                "endpoint",
                "prepared_request_sha256",
                "capability_snapshot_ids",
                "instrument_versions",
                *(
                    ("provider_environment",)
                    if provider == "BYBIT"
                    else ()
                ),
            )
        )
        scope_keys = canonical_frozenset(scope.keys())
        if not required_keys.issubset(scope_keys):
            raise error_type(
                "durable submission scope does not match prepared provider request"
            )
        financial_route_keys = canonical_frozenset(
            (
                "provider_id",
                "account_id",
                "environment",
                "provider_environment",
                "capability_snapshot_id",
                "provider_route_qualification_id",
                "provider_route_capability_snapshot_id",
                "provider_route_decision_journal_sequence_cut",
                "provider_route_provider_environment",
                "provider_route_adapter_code_sha",
                "provider_route_packaged_artifact_digest",
                "provider_route_protocol_id",
                "provider_route_protocol_version",
                "provider_route_entity_policy_id",
                "provider_route_entity_id",
            )
        )
        extension_keys = scope_keys.difference(required_keys)
        if extension_keys:
            if not extension_keys.issubset(financial_route_keys):
                raise error_type(
                    "durable submission scope contains unknown authority axes"
                )
            if not financial_route_keys.issubset(scope_keys):
                raise error_type(
                    "financial route submission scope is incomplete"
                )
        if "provider_environment" in scope:
            raw_provider_environment = scope["provider_environment"]
            scoped_provider_environment = canonical_text(
                raw_provider_environment,
                "submission_scope.provider_environment",
            ).upper()
            if scoped_provider_environment != raw_provider_environment:
                raise error_type(
                    "durable submission provider environment is not canonical"
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

        # Financial/provider-route extensions are sealed by submission_scope_hash.
        # Accept only the canonical base write shape or the complete financial-route
        # shape; partial/unknown authority axes fail closed.
        provider_extension_keys = canonical_frozenset(("provider_environment",))
        financial_extension_keys = canonical_frozenset(
            ("provider_id", "account_id", "environment", "capability_snapshot_id")
        )
        route_extension_keys = canonical_frozenset(
            (
                "provider_route_qualification_id",
                "provider_route_capability_snapshot_id",
                "provider_route_decision_journal_sequence_cut",
                "provider_route_provider_environment",
                "provider_route_adapter_code_sha",
                "provider_route_packaged_artifact_digest",
                "provider_route_protocol_id",
                "provider_route_protocol_version",
                "provider_route_entity_policy_id",
                "provider_route_entity_id",
            )
        )
        extension_keys = scope_keys.difference(required_keys)
        allowed_extension_keys = (
            provider_extension_keys
            | financial_extension_keys
            | route_extension_keys
        )
        if not extension_keys.issubset(allowed_extension_keys):
            raise error_type("durable submission scope has unknown authority axes")
        route_shape_present = bool(
            extension_keys.intersection(
                financial_extension_keys | route_extension_keys
            )
        )
        if route_shape_present:
            required_route_shape = (
                required_keys
                | provider_extension_keys
                | financial_extension_keys
                | route_extension_keys
            )
            if scope_keys != required_route_shape:
                raise error_type(
                    "durable financial route submission scope is incomplete"
                )

        financial_scope = {
            "provider_id": provider,
            "account_id": binding["account_id"],
            "environment": binding["environment"],
        }
        for key, value in financial_scope.items():
            if key in scope and canonical_text(
                scope[key],
                "submission_scope." + key,
            ) != value:
                raise error_type(
                    "durable submission financial scope does not match response binding"
                )
        for key in (
            "capability_snapshot_id",
            "provider_route_capability_snapshot_id",
        ):
            if key in scope and (
                len(capabilities) != 1
                or canonical_text(
                    scope[key],
                    "submission_scope." + key,
                )
                != capabilities[0]
            ):
                raise error_type(
                    "durable submission capability scope does not match prepared request"
                )
        if "provider_route_provider_environment" in scope:
            if "provider_environment" not in scope:
                raise error_type(
                    "durable submission provider-route environment scope mismatch"
                )
            raw_route_provider_environment = scope[
                "provider_route_provider_environment"
            ]
            route_provider_environment = canonical_text(
                raw_route_provider_environment,
                "submission_scope.provider_route_provider_environment",
            ).upper()
            if route_provider_environment != raw_route_provider_environment:
                raise error_type(
                    "durable submission provider-route environment is not canonical"
                )
            scoped_provider_environment = canonical_text(
                scope["provider_environment"],
                "submission_scope.provider_environment",
            ).upper()
            if route_provider_environment != scoped_provider_environment:
                raise error_type(
                    "durable submission provider-route environment scope mismatch"
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
