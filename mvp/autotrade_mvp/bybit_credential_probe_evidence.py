"""Authority-free Bybit credential probe evidence for WP-49.

The live probe is generation-locked to the exact TRADE credential leased from
``ProtectedCredentialVault`` and endpoint-locked to Bybit's environment-specific
``GET /v5/user/query-api`` origin. Durable evidence retains only non-secret
request facts and a canonical response digest. It never grants send, credential
retirement, or recovery-takeover authority.
"""

from __future__ import annotations

from dataclasses import InitVar, dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
from http.client import HTTPException
import hmac
import json
import math
import re
from types import MappingProxyType
from typing import Any, Callable, Mapping
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .bybit_credential_nonacceptance import (
    BybitCredentialNonAcceptance,
    classify_bybit_credential_nonacceptance,
)
from .provider_core import ProviderCoreError
from .provider_response_limits import (
    DEFAULT_MAX_PROVIDER_RESPONSE_BYTES,
    HARD_MAX_PROVIDER_RESPONSE_BYTES,
    MAX_PROVIDER_JSON_DEPTH,
    require_provider_json_depth,
    require_provider_response_bytes,
)
from .provider_transport import (
    BYBIT_V5_ENDPOINT_POLICIES,
    BybitV5Credential,
    ProviderTransportScopeError,
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
_EPOCH_UTC = datetime(1970, 1, 1, tzinfo=timezone.utc)
_PROVIDER_ECHO_ATTESTATION = object()


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


def _utc_datetime(value: object, *, name: str) -> datetime:
    text = _exact_text(value, name=name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise ProviderCoreError(f"{name} must be an ISO timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ProviderCoreError(f"{name} must include timezone")
    return parsed.astimezone(timezone.utc)


def _utc_text(value: object, *, name: str) -> str:
    return _utc_datetime(value, name=name).isoformat().replace("+00:00", "Z")


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


def _datetime_epoch_millis(value: datetime) -> int:
    delta = value - _EPOCH_UTC
    return (
        delta.days * 86_400_000
        + delta.seconds * 1_000
        + delta.microseconds // 1_000
    )


def _exact_request_timestamp(value: object) -> int:
    if type(value) is not int or value < 0:
        raise ProviderCoreError(
            "Bybit credential probe timestamp must be an exact non-negative integer"
        )
    return value


def _exact_recv_window(value: object) -> int:
    if type(value) is not int or not 1 <= value <= 60000:
        raise ProviderCoreError(
            "Bybit credential probe recv_window_ms must be exact integer 1..60000"
        )
    return value


def _snapshot_exact_json_data(
    value: object,
    *,
    path: str = "$",
    depth: int = 0,
    _active_container_ids: set[int] | None = None,
) -> object:
    """Detach one bounded exact finite JSON tree from caller-owned containers."""

    if depth > MAX_PROVIDER_JSON_DEPTH:
        raise ProviderCoreError(
            "Bybit credential probe response exceeds structural depth budget"
        )
    if value is None or type(value) in {str, int, bool}:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ProviderCoreError(
                f"Bybit credential probe response value at {path} must be finite"
            )
        return value
    if type(value) not in {list, dict}:
        raise ProviderCoreError(
            f"Bybit credential probe response value at {path} is not exact JSON data"
        )

    active = _active_container_ids if _active_container_ids is not None else set()
    identity = id(value)
    if identity in active:
        raise ProviderCoreError("Bybit credential probe response contains a JSON cycle")
    active.add(identity)
    try:
        if type(value) is list:
            try:
                items = tuple(value)
            except RuntimeError as error:
                raise ProviderCoreError(
                    "Bybit credential probe response mutated during JSON snapshot"
                ) from error
            return [
                _snapshot_exact_json_data(
                    item,
                    path=f"{path}[{index}]",
                    depth=depth + 1,
                    _active_container_ids=active,
                )
                for index, item in enumerate(items)
            ]

        try:
            items = tuple(value.items())
        except RuntimeError as error:
            raise ProviderCoreError(
                "Bybit credential probe response mutated during JSON snapshot"
            ) from error
        result: dict[str, object] = {}
        for key, item in items:
            if type(key) is not str:
                raise ProviderCoreError(
                    f"Bybit credential probe response key at {path} must be exact text"
                )
            result[key] = _snapshot_exact_json_data(
                item,
                path=f"{path}.{key}",
                depth=depth + 1,
                _active_container_ids=active,
            )
        return result
    finally:
        active.remove(identity)


def _require_exact_json_data(value: object, *, path: str = "$") -> None:
    _snapshot_exact_json_data(value, path=path)


def _response_digest(response: object) -> tuple[int, str]:
    if type(response) is not dict:
        raise ProviderCoreError(
            "Bybit credential probe response must be an exact object"
        )
    snapshot = _snapshot_exact_json_data(response)
    if type(snapshot) is not dict:
        raise ProviderCoreError(
            "Bybit credential probe response must be an exact object"
        )
    if "retCode" not in snapshot:
        raise ProviderCoreError("Bybit credential probe response must include retCode")
    ret_code = snapshot["retCode"]
    if type(ret_code) is not int:
        raise ProviderCoreError(
            "Bybit credential probe retCode must be an exact integer"
        )
    try:
        encoded = json.dumps(
            snapshot,
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
        require_provider_json_depth(body)
    except ValueError as error:
        raise ProviderCoreError(
            "Bybit credential probe wire JSON exceeds structural depth budget"
        ) from error
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
    except ProviderCoreError:
        raise
    except (json.JSONDecodeError, RecursionError, ValueError) as error:
        raise ProviderCoreError(
            "Bybit credential probe wire body must be valid JSON"
        ) from error
    if type(value) is not dict:
        raise ProviderCoreError(
            "Bybit credential probe wire JSON must be an exact object"
        )
    snapshot = _snapshot_exact_json_data(value)
    if type(snapshot) is not dict:
        raise ProviderCoreError(
            "Bybit credential probe wire JSON must be an exact object"
        )
    return snapshot


def _confirm_success_api_key_echo(
    response: dict[str, Any],
    *,
    expected_api_key: str,
) -> bool:
    """Bind retCode=0 to the exact API key used to authenticate the request."""

    ret_code, _digest = _response_digest(response)
    if ret_code != 0:
        return False
    expected = _exact_text(expected_api_key, name="expected_api_key")
    result = response.get("result")
    if type(result) is not dict:
        raise ProviderCoreError(
            "successful Bybit credential probe must include exact result object"
        )
    echoed_api_key = result.get("apiKey")
    if type(echoed_api_key) is not str or echoed_api_key != expected:
        raise ProviderCoreError(
            "successful Bybit credential probe API key echo does not match request credential"
        )
    secret_echo = result.get("secret")
    if type(secret_echo) is not str or secret_echo != "":
        raise ProviderCoreError(
            "successful Bybit credential probe must return an empty secret field"
        )
    return True


def _classify_query_api_credential_nonacceptance(
    *,
    ret_code: int,
    product_family: str,
    response_surface: str,
) -> BybitCredentialNonAcceptance:
    """Classify only semantics bound by the familyless query-api request itself.

    The query-api endpoint has no category/product-family request parameter.  A
    caller-selected family therefore cannot safely promote family-specific
    expiry codes (-2015/33004) into exact-domain rejection evidence.  Those
    codes remain inconclusive here; the generic classifier remains available to
    boundaries whose request itself binds the relevant product family.
    """

    if ret_code not in {0, 10003}:
        return BybitCredentialNonAcceptance.INCONCLUSIVE
    return classify_bybit_credential_nonacceptance(
        ret_code=ret_code,
        product_family=product_family,
        response_surface=response_surface,
    )


@dataclass(frozen=True, slots=True)
class BybitCredentialProbeWireResponse:
    """Scrubbed HTTP result: raw provider JSON is consumed, not retained."""

    http_status: int
    response: InitVar[dict[str, Any]]
    api_key_echo_confirmed: bool = False
    _provider_echo_attestation: InitVar[object | None] = None
    ret_code: int = field(init=False)
    response_sha256: str = field(init=False)

    def __post_init__(
        self,
        response: dict[str, Any],
        _provider_echo_attestation: object | None,
    ) -> None:
        if type(self.http_status) is not int or not 100 <= self.http_status <= 599:
            raise ProviderCoreError(
                "Bybit credential probe HTTP status must be an exact three-digit integer"
            )
        if type(self.api_key_echo_confirmed) is not bool:
            raise ProviderCoreError(
                "Bybit credential probe API key echo flag must be exact boolean"
            )
        ret_code, response_sha256 = _response_digest(response)
        if ret_code == 0 and not self.api_key_echo_confirmed:
            raise ProviderCoreError(
                "successful Bybit credential probe requires exact API key echo confirmation"
            )
        if ret_code == 0 and _provider_echo_attestation is not _PROVIDER_ECHO_ATTESTATION:
            raise ProviderCoreError(
                "successful Bybit credential probe requires provider-derived API key echo attestation"
            )
        if ret_code != 0 and self.api_key_echo_confirmed:
            raise ProviderCoreError(
                "Bybit credential probe API key echo confirmation is valid only for success"
            )
        if ret_code != 0 and _provider_echo_attestation is not None:
            raise ProviderCoreError(
                "Bybit credential probe provider echo attestation is valid only for success"
            )
        object.__setattr__(self, "ret_code", ret_code)
        object.__setattr__(self, "response_sha256", response_sha256)


BybitCredentialProbeWireQuery = Callable[..., BybitCredentialProbeWireResponse]


@dataclass(frozen=True, slots=True)
class BybitCredentialProbeEvidence:
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
    api_key_echo_confirmed: bool = False
    _provider_echo_attestation: InitVar[object | None] = None
    classification: BybitCredentialNonAcceptance = field(init=False)

    def __post_init__(self, _provider_echo_attestation: object | None) -> None:
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
        if type(self.api_key_echo_confirmed) is not bool:
            raise ProviderCoreError(
                "Bybit credential probe API key echo flag must be exact boolean"
            )
        if self.ret_code == 0 and not self.api_key_echo_confirmed:
            raise ProviderCoreError(
                "successful Bybit credential evidence requires API key echo confirmation"
            )
        if self.ret_code == 0 and _provider_echo_attestation is not _PROVIDER_ECHO_ATTESTATION:
            raise ProviderCoreError(
                "successful Bybit credential evidence requires provider-derived API key echo attestation"
            )
        if self.ret_code != 0 and self.api_key_echo_confirmed:
            raise ProviderCoreError(
                "API key echo confirmation is valid only for successful Bybit evidence"
            )
        if self.ret_code != 0 and _provider_echo_attestation is not None:
            raise ProviderCoreError(
                "provider echo attestation is valid only for successful Bybit evidence"
            )
        response_sha256 = _exact_text(self.response_sha256, name="response_sha256")
        if _SHA256.fullmatch(response_sha256) is None:
            raise ProviderCoreError(
                "Bybit credential probe response_sha256 must be canonical sha256"
            )
        observed_datetime = _utc_datetime(self.observed_at, name="observed_at")
        observed_at = observed_datetime.isoformat().replace("+00:00", "Z")
        if _datetime_epoch_millis(observed_datetime) < request_timestamp_ms:
            raise ProviderCoreError(
                "Bybit credential probe observed_at cannot precede signed request timestamp"
            )
        classification = _classify_query_api_credential_nonacceptance(
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
        "api_key_echo_confirmed": evidence.api_key_echo_confirmed,
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
    api_key_echo_confirmed: bool = False,
) -> BybitCredentialProbeEvidence:
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
        api_key_echo_confirmed=api_key_echo_confirmed,
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
    for raw_key, raw_value in headers.items():
        if type(raw_key) is not str or raw_key not in _PROBE_HEADER_NAMES:
            raise ProviderCoreError(
                "Bybit credential probe wire header names must be exact canonical text"
            )
        value = _exact_text(raw_value, name=f"header {raw_key}")
        if "\r" in value or "\n" in value:
            raise ProviderCoreError(
                "Bybit credential probe header values must not contain line breaks"
            )
        normalized[raw_key] = value
    if normalized["Accept"] != "application/json":
        raise ProviderCoreError("Bybit credential probe Accept header is not canonical")
    timestamp = normalized["X-BAPI-TIMESTAMP"]
    recv_window = normalized["X-BAPI-RECV-WINDOW"]
    if (
        not timestamp.isascii()
        or not timestamp.isdigit()
        or str(int(timestamp)) != timestamp
    ):
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


@dataclass(frozen=True, slots=True)
class BybitCredentialProbeHttpRequest:
    url: str
    headers: Mapping[str, str] = field(repr=False)
    timeout_seconds: int

    def __post_init__(self) -> None:
        url = _exact_text(self.url, name="url")
        if url not in _ALLOWED_PROBE_SOURCE_URIS:
            raise ProviderCoreError(
                "Bybit credential probe request URL is outside exact query-api origins"
            )
        headers = _validate_probe_wire_headers(self.headers)
        if (
            type(self.timeout_seconds) is not int
            or not 1 <= self.timeout_seconds <= 120
        ):
            raise ProviderCoreError(
                "Bybit credential probe timeout must be exact integer 1..120"
            )
        object.__setattr__(self, "url", url)
        object.__setattr__(self, "headers", headers)


@dataclass(frozen=True, slots=True)
class BybitCredentialProbeRawHttpResponse:
    http_status: int
    body: bytes = field(repr=False)

    def __post_init__(self) -> None:
        if type(self.http_status) is not int or not 100 <= self.http_status <= 599:
            raise ProviderCoreError(
                "Bybit credential probe HTTP status must be exact integer 100..599"
            )
        try:
            require_provider_response_bytes(
                self.body,
                max_bytes=HARD_MAX_PROVIDER_RESPONSE_BYTES,
            )
        except (TypeError, ValueError) as error:
            raise ProviderCoreError(
                "Bybit credential probe raw response body is invalid or oversized"
            ) from error


class _NoProbeRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        del req, fp, code, msg, headers, newurl
        return None


class BybitCredentialProbeUrllibClient:
    """Direct one-shot TLS GET with redirects/retries/proxies disabled."""

    def __init__(
        self,
        *,
        max_response_bytes: int = DEFAULT_MAX_PROVIDER_RESPONSE_BYTES,
    ) -> None:
        if (
            type(max_response_bytes) is not int
            or not 1 <= max_response_bytes <= HARD_MAX_PROVIDER_RESPONSE_BYTES
        ):
            raise ProviderCoreError(
                "Bybit credential probe response byte budget is invalid"
            )
        self.max_response_bytes = max_response_bytes
        self._opener = build_opener(ProxyHandler({}), _NoProbeRedirectHandler())

    def send(
        self,
        request: BybitCredentialProbeHttpRequest,
    ) -> BybitCredentialProbeRawHttpResponse:
        if type(request) is not BybitCredentialProbeHttpRequest:
            raise TypeError(
                "request must be exact BybitCredentialProbeHttpRequest"
            )
        budget = self.max_response_bytes
        if (
            type(budget) is not int
            or not 1 <= budget <= HARD_MAX_PROVIDER_RESPONSE_BYTES
        ):
            raise ProviderCoreError(
                "Bybit credential probe response byte budget is invalid"
            )
        outbound = Request(
            request.url,
            data=None,
            headers=dict(request.headers),
            method="GET",
        )
        http_status: int | None = None
        http_error_status: int | None = None
        raw: bytes | None = None
        redirect = False
        transport_unavailable = False
        read_failed = False
        try:
            with self._opener.open(
                outbound,
                timeout=request.timeout_seconds,
            ) as response:
                observed_status = response.status
                if type(observed_status) is int and 100 <= observed_status <= 599:
                    http_status = observed_status
                raw = response.read(budget + 1)
        except HTTPError as error:
            observed_status = error.code
            if type(observed_status) is int and 100 <= observed_status <= 599:
                http_error_status = observed_status
                if 300 <= observed_status < 400:
                    redirect = True
                else:
                    try:
                        raw = error.read(budget + 1)
                    except (OSError, HTTPException, ValueError):
                        read_failed = True
            else:
                read_failed = True
        except (OSError, HTTPException, ValueError):
            transport_unavailable = True

        if transport_unavailable:
            raise ProviderCoreError(
                "Bybit credential probe HTTP transport unavailable"
            ) from None
        if redirect:
            raise ProviderCoreError(
                "Bybit credential probe redirect is prohibited"
            ) from None
        if read_failed:
            raise ProviderCoreError(
                "Bybit credential probe HTTP error response unavailable"
            ) from None
        final_status = http_error_status if http_error_status is not None else http_status
        if final_status is None:
            raise ProviderCoreError(
                "Bybit credential probe HTTP status is unavailable"
            )
        if type(raw) is not bytes or not raw:
            raise ProviderCoreError(
                "Bybit credential probe HTTP response body is unavailable"
            )
        try:
            bounded = require_provider_response_bytes(raw, max_bytes=budget)
        except (TypeError, ValueError) as error:
            raise ProviderCoreError(
                "Bybit credential probe HTTP response is invalid or oversized"
            ) from error
        return BybitCredentialProbeRawHttpResponse(
            http_status=final_status,
            body=bounded,
        )


def execute_bybit_credential_probe_wire_query(
    *,
    source_uri: str,
    headers: Mapping[str, str],
    timeout_seconds: int,
    wire_client: object | None = None,
) -> BybitCredentialProbeWireResponse:
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
    client = wire_client if wire_client is not None else BybitCredentialProbeUrllibClient()
    if not hasattr(client, "send"):
        raise TypeError("wire_client must implement send")
    request = BybitCredentialProbeHttpRequest(
        url=source_uri,
        headers=headers,
        timeout_seconds=timeout_seconds,
    )
    raw_response = client.send(request)
    if type(raw_response) is not BybitCredentialProbeRawHttpResponse:
        raise TypeError(
            "Bybit credential probe wire client must return exact BybitCredentialProbeRawHttpResponse"
        )
    if raw_response.http_status != 200:
        raise ProviderCoreError(
            "Bybit credential probe non-200 HTTP result is not rejection evidence"
        )
    decoded = _decode_wire_json(raw_response.body)
    api_key_echo_confirmed = _confirm_success_api_key_echo(
        decoded,
        expected_api_key=headers["X-BAPI-API-KEY"],
    )
    return BybitCredentialProbeWireResponse(
        http_status=raw_response.http_status,
        response=decoded,
        api_key_echo_confirmed=api_key_echo_confirmed,
        _provider_echo_attestation=(
            _PROVIDER_ECHO_ATTESTATION if api_key_echo_confirmed else None
        ),
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
        return BybitCredentialProbeEvidence(
            credential_handle=credential_handle,
            provider_environment=provider_environment,
            source_uri=source_uri,
            product_family=product_family,
            response_surface="V5_UTA_REST",
            request_timestamp_ms=request_timestamp_ms,
            recv_window_ms=recv_window_ms,
            http_status=wire_response.http_status,
            ret_code=wire_response.ret_code,
            response_sha256=wire_response.response_sha256,
            observed_at=observed_at,
            api_key_echo_confirmed=wire_response.api_key_echo_confirmed,
            _provider_echo_attestation=(
                _PROVIDER_ECHO_ATTESTATION
                if wire_response.api_key_echo_confirmed
                else None
            ),
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
    def wire_query(
        *,
        source_uri: str,
        headers: Mapping[str, str],
        timeout_seconds: int,
    ) -> BybitCredentialProbeWireResponse:
        return execute_bybit_credential_probe_wire_query(
            source_uri=source_uri,
            headers=headers,
            timeout_seconds=timeout_seconds,
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
