"""Authority-free Bybit credential probe evidence for WP-49.

This boundary binds one already-authenticated Bybit V5 credential probe to the
exact historical TRADE credential generation that was used for the request and
to the exact Bybit provider environment contacted.  It deliberately retains
only a canonical response digest plus non-secret scope metadata; the response
body (including an API-key value returned by ``/v5/user/query-api`` on success)
is never retained here.

The resulting value is observation evidence only.  It cannot retire a vault
credential generation, prove that an old process stopped, grant send authority,
or authorize recovery takeover.  Those decisions remain owned by their
separate canonical fences.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
import json
import re
from typing import Any

from .bybit_credential_nonacceptance import (
    BybitCredentialNonAcceptance,
    classify_bybit_credential_nonacceptance,
)
from .provider_core import ProviderCoreError
from .windows_secrets import PersistentCredentialHandle


_BYBIT_QUERY_API_PATH = "/v5/user/query-api"
_REST_BASE_BY_PROVIDER_ENVIRONMENT = {
    "MAINNET": "https://api.bybit.com",
    "TESTNET": "https://api-testnet.bybit.com",
    "DEMO": "https://api-demo.bybit.com",
}
_RUNTIME_ENVIRONMENT_BY_PROVIDER_ENVIRONMENT = {
    "MAINNET": "LIVE",
    "TESTNET": "PAPER",
    "DEMO": "PAPER",
}
_SUPPORTED_FAMILIES = frozenset(
    {
        "SPOT",
        "MARGIN",
        "LINEAR_DERIVATIVES",
        "INVERSE_DERIVATIVES",
        "OPTIONS",
    }
)
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")


def _exact_text(value: object, *, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ProviderCoreError(f"{name} must be canonical non-empty text")
    return value


def _utc_text(value: object, *, name: str) -> str:
    text = _exact_text(value, name=name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise ProviderCoreError(f"{name} must be an ISO timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ProviderCoreError(f"{name} must include timezone")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _response_digest(response: object) -> tuple[int, str]:
    """Return exact retCode plus a digest without retaining provider payload."""

    if type(response) is not dict:
        raise ProviderCoreError("Bybit credential probe response must be an exact object")
    if "retCode" not in response:
        raise ProviderCoreError("Bybit credential probe response must include retCode")
    ret_code = response["retCode"]
    if type(ret_code) is not int:
        raise ProviderCoreError("Bybit credential probe retCode must be an exact integer")
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


@dataclass(frozen=True, slots=True)
class BybitCredentialProbeEvidence:
    """Detached non-secret evidence for one exact credential-generation probe.

    ``credential_handle`` identifies the local historical generation selected
    for the authenticated request.  ``source_uri`` identifies the exact
    environment-specific Bybit endpoint.  Neither claim is self-authenticating:
    the transport boundary that creates this value must already have performed
    TLS/origin validation and used the selected credential generation.
    """

    credential_handle: PersistentCredentialHandle
    provider_environment: str
    source_uri: str
    response_surface: str
    product_family: str
    ret_code: int
    response_sha256: str
    observed_at: str
    classification: BybitCredentialNonAcceptance = field(init=False)

    def __post_init__(self) -> None:
        if type(self.credential_handle) is not PersistentCredentialHandle:
            raise TypeError(
                "credential_handle must be an exact PersistentCredentialHandle"
            )
        if self.credential_handle.provider != "BYBIT":
            raise ProviderCoreError("Bybit probe evidence requires a BYBIT credential")
        if self.credential_handle.purpose != "TRADE":
            raise ProviderCoreError("Bybit probe evidence requires a TRADE credential")

        provider_environment = _exact_text(
            self.provider_environment,
            name="provider_environment",
        )
        if provider_environment not in _REST_BASE_BY_PROVIDER_ENVIRONMENT:
            raise ProviderCoreError(
                "Bybit provider_environment must be MAINNET, TESTNET or DEMO"
            )
        expected_runtime_environment = (
            _RUNTIME_ENVIRONMENT_BY_PROVIDER_ENVIRONMENT[provider_environment]
        )
        if self.credential_handle.environment != expected_runtime_environment:
            raise ProviderCoreError(
                "Bybit probe provider environment does not match credential environment"
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
        if type(self.ret_code) is not int:
            raise ProviderCoreError("Bybit credential probe ret_code must be exact integer")
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
        object.__setattr__(self, "observed_at", observed_at)
        object.__setattr__(self, "classification", classification)

    @property
    def send_authority(self) -> bool:
        """Provider probe evidence can never authorize an external send."""

        return False

    @property
    def retirement_authority(self) -> bool:
        """Provider probe evidence cannot mutate or replace vault retirement truth."""

        return False

    @property
    def takeover_authority(self) -> bool:
        """Provider probe evidence alone can never authorize recovery takeover."""

        return False


def capture_bybit_credential_probe_evidence(
    *,
    credential_handle: PersistentCredentialHandle,
    provider_environment: str,
    source_uri: str,
    product_family: str,
    response_surface: str,
    response: dict[str, Any],
    observed_at: str,
) -> BybitCredentialProbeEvidence:
    """Scrub one authenticated ``query-api`` response into detached evidence.

    The caller must pass the exact ``PersistentCredentialHandle`` whose secret
    material was used to authenticate the request.  This function stores no API
    key, API secret, provider response body, session token, or permission set.
    A successful ``query-api`` response may therefore prove that the selected
    credential was accepted at that instant, but the returned evidence still
    grants no trading or takeover authority.
    """

    ret_code, response_sha256 = _response_digest(response)
    return BybitCredentialProbeEvidence(
        credential_handle=credential_handle,
        provider_environment=provider_environment,
        source_uri=source_uri,
        response_surface=response_surface,
        product_family=product_family,
        ret_code=ret_code,
        response_sha256=response_sha256,
        observed_at=observed_at,
    )
