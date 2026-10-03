"""Authority-free Bybit credential probe evidence for WP-49.

This boundary binds one already-authenticated Bybit V5 credential probe to the
exact historical TRADE credential generation that was used for the request and
to the exact Bybit provider environment contacted. It deliberately retains
only a canonical response digest plus non-secret scope metadata; the response
body (including an API-key value returned by ``/v5/user/query-api`` on success)
is never retained here.

The resulting value is observation evidence only. It cannot retire a vault
credential generation, prove that an old process stopped, grant send authority,
or authorize recovery takeover. Those decisions remain owned by their
separate canonical fences.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
import hmac
import json
import re
from types import MappingProxyType
from typing import Any, Callable, Mapping

from .bybit_credential_nonacceptance import (
    BybitCredentialNonAcceptance,
    classify_bybit_credential_nonacceptance,
)
from .provider_core import ProviderCoreError
from .provider_transport import (
    BYBIT_V5_ENDPOINT_POLICIES,
    AuthenticatedReadHttpRequest,
    AuthenticatedReadWireResponse,
    BybitV5Credential,
    ProviderTransportScopeError,
    UrllibJsonWireClient,
)
from .windows_secrets import (
    PersistentCredentialHandle,
    ProtectedCredentialVault,
    SecretVaultError,
)


_BYBIT_QUERY_API_PATH = "/v5/user/query-api"
_REST_BASE_BY_PROVIDER_ENVIRONMENT = {
    "MAINNET": "https://api.bybit.com",
    "TESTNET": "https://api-testnet.bybit.com",
    "DEMO": "https://api-demo.bybit.com",
}
_ALLOWED_PROBE_SOURCE_URIS = frozenset(
    base + _BYBIT_QUERY_API_PATH
    for base in _REST_BASE_BY_PROVIDER_ENVIRONMENT.values()
)
_SUPPORTED_FAMILIES = frozenset(
    {
        "SPOT",
        "MARGIN",
        "LINEAR_DERIVATIVES",
        "INVERSE_DERIVATIVES",
        "OPTIONS",
    }
)
_PROBE_HEADER_NAMES = frozenset(
    {
        "Accept",
        "X-BAPI-API-KEY",
        "X-BAPI-TIMESTAMP",
        "X-BAPI-RECV-WINDOW",
        "X-BAPI-SIGN",
    }
)
_CREDENTIAL_HANDLE_FIELDS = (
    "handle_id",
    "account_id",
    "provider",
    "environment",
    "provider_environment",
    "purpose",
    "generation",
)
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_HEX_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _exact_text(value: object, *, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ProviderCoreError(f"{name} must be canonical non-empty text")
    return value


def _snapshot_credential_handle(value: object) -> PersistentCredentialHandle:
    """Detach exact canonical credential subject state from caller-owned objects."""

    if type(value) is not PersistentCredentialHandle:
        raise TypeError("credential_handle must be an exact PersistentCredentialHandle")
    captured: dict[str, object] = {}
    for name in _CREDENTIAL_HANDLE_FIELDS:
        current = getattr(value, name)
        if name == "generation":
            if type(current) is not int or current < 1:
                raise ProviderCoreError(
                    "Bybit credential handle generation must be exact positive integer"
                )
        elif type(current) is not str or not current or current != current.strip():
            raise ProviderCoreError(
                f"Bybit credential handle {name} must be canonical exact text"
            )
        captured[name] = current
    try:
        snapshot = PersistentCredentialHandle(**captured)
    except (SecretVaultError, TypeError, ValueError) as error:
        raise ProviderCoreError("Bybit credential handle state is invalid") from error
    for name in _CREDENTIAL_HANDLE_FIELDS:
        original = captured[name]
        canonical = getattr(snapshot, name)
        if type(original) is not type(canonical) or original != canonical:
            raise ProviderCoreError(
                f"Bybit credential handle {name} is not canonical"
            )
    return snapshot


def _utc_text(value: object, *, name: str) -> str:
    text = _exact_text(value, name=name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise ProviderCoreError(f"{name} must be an ISO timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ProviderCoreError(f"{name} must include timezone")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _clock_utc_text(value: object) -> str:
    if (
        type(value) is not datetime
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ProviderCoreError(
            "Bybit credential probe clock_utc must return exact timezone-aware datetime"
        )
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _exact_request_timestamp(value: object) -> int:
    if type(value) is not int or value < 0:
        raise ProviderCoreError(
            "Bybit credential probe timestamp must be an exact non-negative integer"
        )
    return value


def _exact_recv_window(value: object) -> int:
    if type(value) is not int or value < 1 or value > 60000:
        raise ProviderCoreError(
            "Bybit credential probe recv_window_ms must be exact integer 1..60000"
        )
    return value


def _require_exact_json_data(value: object, *, path: str = "$") -> None:
    """Reject Python-only or polymorphic values before hashing provider JSON."""

    if value is None or type(value) in {str, int, float, bool}:
        return
    if type(value) is list:
        for index, item in enumerate(value):
            _require_exact_json_data(item, path=f"{path}[{index}]")
        return
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str:
                raise ProviderCoreError(
                    f"Bybit credential probe response key at {path} must be exact text"
                )
            _require_exact_json_data(item, path=f"{path}.{key}")
        return
    raise ProviderCoreError(
        f"Bybit credential probe response value at {path} is not exact JSON data"
    )


def _response_digest(response: object) -> tuple[int, str]:
    """Return exact retCode plus a digest without retaining provider payload."""

    if type(response) is not dict:
        raise ProviderCoreError(
            "Bybit credential probe response must be an exact object"
        )
    if "retCode" not in response:
        raise ProviderCoreError("Bybit credential probe response must include retCode")
    ret_code = response["retCode"]
    if type(ret_code) is not int:
        raise ProviderCoreError(
            "Bybit credential probe retCode must be an exact integer"
        )
    _require_exact_json_data(response)
    try:
        encoded = json.dumps(
            response,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ProviderCoreError(
            "Bybit credential probe response must be canonical JSON data"
        ) from error
    return ret_code, "sha256:" + sha256(encoded).hexdigest()


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ProviderCoreError(
                "Bybit credential probe wire JSON contains duplicate object key"
            )
        result[key] = value
    return result


def _reject_json_constant(value: str) -> object:
    raise ProviderCoreError(
        f"Bybit credential probe wire JSON contains non-finite constant {value}"
    )


def _decode_wire_json(body: object) -> dict[str, Any]:
    if type(body) is not bytes or not body:
        raise ProviderCoreError(
            "Bybit credential probe wire body must be exact non-empty bytes"
        )
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ProviderCoreError(
            "Bybit credential probe wire body must be UTF-8 JSON"
        ) from error
    try:
        value = json.loads(
            text,
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except json.JSONDecodeError as error:
        raise ProviderCoreError(
            "Bybit credential probe wire body must be valid JSON"
        ) from error
    if type(value) is not dict:
        raise ProviderCoreError(
            "Bybit credential probe wire JSON must be an exact object"
        )
    _require_exact_json_data(value)
    return value


@dataclass(frozen=True, slots=True)
class BybitCredentialProbeWireResponse:
    """Exact HTTP+JSON result returned by the probe's wire boundary."""

    http_status: int
    response: dict[str, Any]

    def __post_init__(self) -> None:
        if type(self.http_status) is not int or not 100 <= self.http_status <= 599:
            raise ProviderCoreError(
                "Bybit credential probe HTTP status must be an exact three-digit integer"
            )
        if type(self.response) is not dict:
            raise ProviderCoreError(
                "Bybit credential probe wire response must contain an exact JSON object"
            )
        _require_exact_json_data(self.response)


BybitCredentialProbeWireQuery = Callable[..., BybitCredentialProbeWireResponse]


@dataclass(frozen=True, slots=True)
class BybitCredentialProbeEvidence:
    """Detached non-secret evidence for one exact credential-generation probe."""

    credential_handle: PersistentCredentialHandle
    provider_environment: str
    source_uri: str
    response_surface: str
    product_family: str
    request_timestamp_ms: int
    recv_window_ms: int
    http_status: int
    ret_code: int
    response_sha256: str
    observed_at: str
    classification: BybitCredentialNonAcceptance = field(init=False)

    def __post_init__(self) -> None:
        credential_handle = _snapshot_credential_handle(self.credential_handle)
        object.__setattr__(self, "credential_handle", credential_handle)
        if credential_handle.provider != "BYBIT":
            raise ProviderCoreError("Bybit probe evidence requires a BYBIT credential")
        if credential_handle.purpose != "TRADE":
            raise ProviderCoreError("Bybit probe evidence requires a TRADE credential")

        provider_environment = _exact_text(
            self.provider_environment,
            name="provider_environment",
        )
        if provider_environment not in _REST_BASE_BY_PROVIDER_ENVIRONMENT:
            raise ProviderCoreError(
                "Bybit provider_environment must be MAINNET, TESTNET or DEMO"
            )
        if credential_handle.provider_environment != provider_environment:
            raise ProviderCoreError(
                "Bybit probe provider environment does not match credential provider domain"
            )

        source_uri = _exact_text(self.source_uri, name="source_uri")
        expected_source_uri = (
            _REST_BASE_BY_PROVIDER_ENVIRONMENT[provider_environment]
            + _BYBIT_QUERY_API_PATH
        )
        if source_uri != expected_source_uri:
            raise ProviderCoreError(
                "Bybit credential probe source_uri does not match exact provider environment"
            )

        response_surface = _exact_text(
            self.response_surface,
            name="response_surface",
        )
        if response_surface != "V5_UTA_REST":
            raise ProviderCoreError(
                "Bybit credential probe requires exact V5_UTA_REST surface"
            )

        product_family = _exact_text(self.product_family, name="product_family")
        if product_family not in _SUPPORTED_FAMILIES:
            raise ProviderCoreError(
                "Bybit credential probe product_family must be canonical and supported"
            )
        request_timestamp_ms = _exact_request_timestamp(self.request_timestamp_ms)
        recv_window_ms = _exact_recv_window(self.recv_window_ms)
        if type(self.http_status) is not int or self.http_status != 200:
            raise ProviderCoreError(
                "Bybit credential probe evidence requires exact HTTP 200"
            )
        if type(self.ret_code) is not int:
            raise ProviderCoreError(
                "Bybit credential probe ret_code must be exact integer"
            )
        response_sha256 = _exact_text(self.response_sha256, name="response_sha256")
        if _SHA256.fullmatch(response_sha256) is None:
            raise ProviderCoreError(
                "Bybit credential probe response_sha256 must be canonical sha256"
            )
        observed_at = _utc_text(self.observed_at, name="observed_at")

        classification = classify_bybit_credential_nonacceptance(
            ret_code=self.ret_code,
            product_family=product_family,
            response_surface=response_surface,
        )
        object.__setattr__(self, "request_timestamp_ms", request_timestamp_ms)
        object.__setattr__(self, "recv_window_ms", recv_window_ms)
        object.__setattr__(self, "observed_at", observed_at)
        object.__setattr__(self, "classification", classification)

    @property
    def send_authority(self) -> bool:
        return False

    @property
    def retirement_authority(self) -> bool:
        return False

    @property
    def takeover_authority(self) -> bool:
        return False


def bybit_credential_probe_receipt_metadata(
    evidence: BybitCredentialProbeEvidence,
) -> dict[str, object]:
    """Return deterministic non-secret metadata for an immutable receipt."""

    if type(evidence) is not BybitCredentialProbeEvidence:
        raise TypeError("evidence must be exact BybitCredentialProbeEvidence")
    handle = evidence.credential_handle
    return {
        "evidence_kind": "BYBIT_CREDENTIAL_PROBE",
        "provider": "BYBIT",
        "provider_environment": evidence.provider_environment,
        "credential_handle_id": handle.handle_id,
        "account_id": handle.account_id,
        "credential_environment": handle.environment,
        "credential_provider_environment": handle.provider_environment,
        "credential_purpose": handle.purpose,
        "credential_generation": handle.generation,
        "source_uri": evidence.source_uri,
        "response_surface": evidence.response_surface,
        "product_family": evidence.product_family,
        "request_timestamp_ms": evidence.request_timestamp_ms,
        "recv_window_ms": evidence.recv_window_ms,
        "http_status": evidence.http_status,
        "ret_code": evidence.ret_code,
        "response_sha256": evidence.response_sha256,
        "observed_at": evidence.observed_at,
        "classification": evidence.classification.value,
        "send_authority": False,
        "retirement_authority": False,
        "takeover_authority": False,
    }


def capture_bybit_credential_probe_evidence(
    *,
    credential_handle: PersistentCredentialHandle,
    provider_environment: str,
    source_uri: str,
    product_family: str,
    response_surface: str,
    request_timestamp_ms: int,
    recv_window_ms: int,
    http_status: int,
    response: dict[str, Any],
    observed_at: str,
) -> BybitCredentialProbeEvidence:
    """Scrub one authenticated ``query-api`` response into detached evidence."""

    ret_code, response_sha256 = _response_digest(response)
    return BybitCredentialProbeEvidence(
        credential_handle=credential_handle,
        provider_environment=provider_environment,
        source_uri=source_uri,
        response_surface=response_surface,
        product_family=product_family,
        request_timestamp_ms=request_timestamp_ms,
        recv_window_ms=recv_window_ms,
        http_status=http_status,
        ret_code=ret_code,
        response_sha256=response_sha256,
        observed_at=observed_at,
    )


def _probe_headers(
    *,
    credential_plaintext: str,
    timestamp_ms: object,
    recv_window_ms: object,
) -> Mapping[str, str]:
    timestamp_ms = _exact_request_timestamp(timestamp_ms)
    recv_window_ms = _exact_recv_window(recv_window_ms)
    try:
        credential = BybitV5Credential.parse(credential_plaintext)
    except ProviderTransportScopeError as error:
        raise ProviderCoreError(
            "Bybit credential probe TRADE credential material is invalid"
        ) from error
    signing_material = (
        str(timestamp_ms) + credential.api_key + str(recv_window_ms)
    ).encode("utf-8")
    signature = hmac.new(
        credential.api_secret.encode("utf-8"),
        signing_material,
        sha256,
    ).hexdigest()
    return MappingProxyType(
        {
            "Accept": "application/json",
            "X-BAPI-API-KEY": credential.api_key,
            "X-BAPI-TIMESTAMP": str(timestamp_ms),
            "X-BAPI-RECV-WINDOW": str(recv_window_ms),
            "X-BAPI-SIGN": signature,
        }
    )


def _validate_probe_wire_headers(headers: object) -> Mapping[str, str]:
    if not isinstance(headers, Mapping) or set(headers) != _PROBE_HEADER_NAMES:
        raise ProviderCoreError(
            "Bybit credential probe wire headers must have exact auth shape"
        )
    normalized: dict[str, str] = {}
    for key in _PROBE_HEADER_NAMES:
        value = _exact_text(headers[key], name=f"header {key}")
        normalized[key] = value
    if normalized["Accept"] != "application/json":
        raise ProviderCoreError("Bybit credential probe Accept header is not canonical")
    timestamp = normalized["X-BAPI-TIMESTAMP"]
    recv_window = normalized["X-BAPI-RECV-WINDOW"]
    if not timestamp.isascii() or not timestamp.isdigit() or str(int(timestamp)) != timestamp:
        raise ProviderCoreError("Bybit credential probe timestamp header is not canonical")
    if (
        not recv_window.isascii()
        or not recv_window.isdigit()
        or str(int(recv_window)) != recv_window
    ):
        raise ProviderCoreError("Bybit credential probe recv-window header is not canonical")
    _exact_request_timestamp(int(timestamp))
    _exact_recv_window(int(recv_window))
    if _HEX_SHA256.fullmatch(normalized["X-BAPI-SIGN"]) is None:
        raise ProviderCoreError("Bybit credential probe signature header is not canonical")
    return MappingProxyType(normalized)


def execute_bybit_credential_probe_wire_query(
    *,
    source_uri: str,
    headers: Mapping[str, str],
    timeout_seconds: int,
    wire_client: object | None = None,
) -> BybitCredentialProbeWireResponse:
    """Execute one redirect-safe query-api GET through the shared wire client.

    This adapter is deliberately endpoint-closed: even a direct caller cannot
    reuse probe authentication headers for an arbitrary URL. The shared urllib
    client supplies the existing bounded-response/no-redirect/no-retry policy.
    """

    source_uri = _exact_text(source_uri, name="source_uri")
    if source_uri not in _ALLOWED_PROBE_SOURCE_URIS:
        raise ProviderCoreError(
            "Bybit credential probe wire source is outside exact query-api origins"
        )
    headers = _validate_probe_wire_headers(headers)
    if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 120:
        raise ProviderCoreError(
            "Bybit credential probe timeout must be exact integer 1..120"
        )
    client = wire_client if wire_client is not None else UrllibJsonWireClient()
    if not hasattr(client, "send"):
        raise TypeError("wire_client must implement send")
    request = AuthenticatedReadHttpRequest(
        url=source_uri,
        headers=headers,
        timeout_seconds=timeout_seconds,
    )
    raw_response = client.send(request)
    if type(raw_response) is not AuthenticatedReadWireResponse:
        raise TypeError(
            "Bybit credential probe wire client must return exact AuthenticatedReadWireResponse"
        )
    return BybitCredentialProbeWireResponse(
        http_status=raw_response.http_status,
        response=_decode_wire_json(raw_response.body),
    )


def probe_bybit_credential_with_vault(
    *,
    vault: ProtectedCredentialVault,
    credential_handle: PersistentCredentialHandle,
    execution_identity: str,
    product_family: str,
    wire_query: BybitCredentialProbeWireQuery,
    clock_millis: Callable[[], int],
    clock_utc: Callable[[], datetime],
    recv_window_ms: int = 5000,
) -> BybitCredentialProbeEvidence:
    """Perform one generation-locked authenticated credential probe."""

    if type(vault) is not ProtectedCredentialVault:
        raise TypeError("vault must be exact ProtectedCredentialVault")
    credential_handle = _snapshot_credential_handle(credential_handle)
    if credential_handle.provider != "BYBIT":
        raise ProviderCoreError("Bybit probe requires a BYBIT credential")
    if credential_handle.purpose != "TRADE":
        raise ProviderCoreError("Bybit probe requires a TRADE credential")
    provider_environment = credential_handle.provider_environment
    if (
        type(provider_environment) is not str
        or provider_environment not in _REST_BASE_BY_PROVIDER_ENVIRONMENT
    ):
        raise ProviderCoreError(
            "Bybit credential provider environment must be MAINNET, TESTNET or DEMO"
        )
    product_family = _exact_text(product_family, name="product_family")
    if product_family not in _SUPPORTED_FAMILIES:
        raise ProviderCoreError(
            "Bybit credential probe product_family must be canonical and supported"
        )
    execution_identity = _exact_text(
        execution_identity,
        name="execution_identity",
    )
    if not callable(wire_query):
        raise TypeError("wire_query must be callable")
    if not callable(clock_millis) or not callable(clock_utc):
        raise TypeError("probe clocks must be callable")
    recv_window_ms = _exact_recv_window(recv_window_ms)

    policy = BYBIT_V5_ENDPOINT_POLICIES.get(provider_environment)
    if policy is None or policy.environment != credential_handle.environment:
        raise ProviderCoreError(
            "Bybit credential runtime environment does not match provider domain"
        )
    source_uri = policy.absolute_url(_BYBIT_QUERY_API_PATH)
    expected_source_uri = (
        _REST_BASE_BY_PROVIDER_ENVIRONMENT[provider_environment]
        + _BYBIT_QUERY_API_PATH
    )
    if source_uri != expected_source_uri:
        raise ProviderCoreError(
            "Bybit canonical endpoint policy drifted from credential probe origin"
        )

    with vault.lease(
        credential_handle,
        execution_identity=execution_identity,
        account_id=credential_handle.account_id,
        provider="BYBIT",
        environment=credential_handle.environment,
        provider_environment=provider_environment,
        purpose="TRADE",
    ) as credential_plaintext:
        try:
            request_timestamp_ms = _exact_request_timestamp(clock_millis())
            headers = _probe_headers(
                credential_plaintext=credential_plaintext,
                timestamp_ms=request_timestamp_ms,
                recv_window_ms=recv_window_ms,
            )
        finally:
            credential_plaintext = None

        wire_response = wire_query(
            source_uri=source_uri,
            headers=headers,
            timeout_seconds=policy.timeout_seconds,
        )
        if type(wire_response) is not BybitCredentialProbeWireResponse:
            raise TypeError(
                "wire_query must return exact BybitCredentialProbeWireResponse"
            )
        if wire_response.http_status != 200:
            raise ProviderCoreError(
                "Bybit credential probe non-200 HTTP result is not rejection evidence"
            )
        observed_at = _clock_utc_text(clock_utc())
        return capture_bybit_credential_probe_evidence(
            credential_handle=credential_handle,
            provider_environment=provider_environment,
            source_uri=source_uri,
            product_family=product_family,
            response_surface="V5_UTA_REST",
            request_timestamp_ms=request_timestamp_ms,
            recv_window_ms=recv_window_ms,
            http_status=wire_response.http_status,
            response=wire_response.response,
            observed_at=observed_at,
        )


def probe_bybit_credential_with_shared_wire(
    *,
    vault: ProtectedCredentialVault,
    credential_handle: PersistentCredentialHandle,
    execution_identity: str,
    product_family: str,
    clock_millis: Callable[[], int],
    clock_utc: Callable[[], datetime],
    recv_window_ms: int = 5000,
    wire_client: object | None = None,
) -> BybitCredentialProbeEvidence:
    """Production-capable probe using the repository's redirect-safe wire seam."""

    def wire_query(**kwargs: object) -> BybitCredentialProbeWireResponse:
        return execute_bybit_credential_probe_wire_query(
            source_uri=kwargs["source_uri"],
            headers=kwargs["headers"],
            timeout_seconds=kwargs["timeout_seconds"],
            wire_client=wire_client,
        )

    return probe_bybit_credential_with_vault(
        vault=vault,
        credential_handle=credential_handle,
        execution_identity=execution_identity,
        product_family=product_family,
        wire_query=wire_query,
        clock_millis=clock_millis,
        clock_utc=clock_utc,
        recv_window_ms=recv_window_ms,
    )
