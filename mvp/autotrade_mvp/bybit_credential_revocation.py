"""Fail-closed Bybit V5 credential-revocation semantics for WP-49.

This module classifies exact provider response bytes only.  It performs no
network I/O, resolves no credential, issues no provider-origin evidence, and
cannot authorize recovery by itself.  A terminal caller must first verify a
Host-issued authenticated-read attestation against an independently selected
Host issuer pin and bind the exact historical credential generation/domain.

Bybit's current V5 error contract distinguishes explicit invalid/expired API-key
codes from generic authentication/signature/permission failures.  Only the
narrow explicit key-invalid/expired cases are eligible here.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Final


class BybitCredentialRevocationError(ValueError):
    """Raised when response bytes cannot establish exact revocation semantics."""


STATE_ACCEPTED: Final = "CREDENTIAL_ACCEPTED"
STATE_REVOKED: Final = "REVOKED_FOR_EXACT_PROVIDER_DOMAIN"
STATE_INCONCLUSIVE: Final = "INCONCLUSIVE"

PRODUCT_SPOT: Final = "SPOT"
PRODUCT_DERIVATIVES: Final = "DERIVATIVES"

_MAX_RESPONSE_BYTES = 64 * 1024
_SUPPORTED_PRODUCTS = frozenset({PRODUCT_SPOT, PRODUCT_DERIVATIVES})


@dataclass(frozen=True, slots=True)
class BybitCredentialRevocationClassification:
    state: str
    ret_code: int | None
    reason_code: str


def classify_bybit_credential_revocation_response(
    *,
    http_status: int,
    response_bytes: bytes,
    product_family: str,
) -> BybitCredentialRevocationClassification:
    """Classify one exact Bybit authenticated response without granting authority.

    HTTP-only authentication failures are deliberately inconclusive.  For a 200
    V5 JSON response:
      * retCode 0 proves the credential was still accepted;
      * 10003 proves the API key is invalid for the exact contacted domain;
      * -2015 is accepted only for SPOT (expired Spot API key);
      * 33004 is accepted only for DERIVATIVES (expired derivatives API key);
      * all other non-zero codes remain inconclusive.

    The caller must separately prove that these exact bytes came from the
    qualified provider route under the exact historical credential generation.
    """

    if type(http_status) is not int or not 100 <= http_status <= 599:
        raise BybitCredentialRevocationError(
            "http_status must be an exact HTTP status integer"
        )
    if type(response_bytes) is not bytes:
        raise TypeError("response_bytes must be exact bytes")
    if type(product_family) is not str or product_family not in _SUPPORTED_PRODUCTS:
        raise BybitCredentialRevocationError(
            "product_family must be exact SPOT or DERIVATIVES"
        )

    if http_status != 200:
        return BybitCredentialRevocationClassification(
            state=STATE_INCONCLUSIVE,
            ret_code=None,
            reason_code="HTTP_STATUS_NOT_PROVIDER_REVOCATION_AUTHORITY",
        )

    payload = _strict_json_object(response_bytes)
    ret_code = payload.get("retCode")
    if type(ret_code) is not int:
        raise BybitCredentialRevocationError(
            "Bybit credential-revocation retCode must be an exact integer"
        )

    if ret_code == 0:
        return BybitCredentialRevocationClassification(
            state=STATE_ACCEPTED,
            ret_code=ret_code,
            reason_code="BYBIT_CREDENTIAL_STILL_ACCEPTED",
        )
    if ret_code == 10003:
        return BybitCredentialRevocationClassification(
            state=STATE_REVOKED,
            ret_code=ret_code,
            reason_code="BYBIT_API_KEY_INVALID_FOR_EXACT_DOMAIN",
        )
    if ret_code == -2015 and product_family == PRODUCT_SPOT:
        return BybitCredentialRevocationClassification(
            state=STATE_REVOKED,
            ret_code=ret_code,
            reason_code="BYBIT_SPOT_API_KEY_EXPIRED",
        )
    if ret_code == 33004 and product_family == PRODUCT_DERIVATIVES:
        return BybitCredentialRevocationClassification(
            state=STATE_REVOKED,
            ret_code=ret_code,
            reason_code="BYBIT_DERIVATIVES_API_KEY_EXPIRED",
        )

    return BybitCredentialRevocationClassification(
        state=STATE_INCONCLUSIVE,
        ret_code=ret_code,
        reason_code="BYBIT_AUTH_REJECTION_NOT_QUALIFIED_AS_REVOCATION",
    )


def require_bybit_credential_revoked(
    *,
    http_status: int,
    response_bytes: bytes,
    product_family: str,
) -> BybitCredentialRevocationClassification:
    """Require explicit exact-domain key invalidation/expiry.

    A still-accepted credential is distinguished from an inconclusive rejection
    so recovery can treat it as a hard takeover failure rather than retry-safe
    absence.  Neither outcome is silently promoted to revocation.
    """

    result = classify_bybit_credential_revocation_response(
        http_status=http_status,
        response_bytes=response_bytes,
        product_family=product_family,
    )
    if result.state == STATE_ACCEPTED:
        raise BybitCredentialRevocationError(
            "old Bybit credential is still accepted by the exact provider domain"
        )
    if result.state != STATE_REVOKED:
        raise BybitCredentialRevocationError(
            "Bybit credential revocation is inconclusive"
        )
    return result


def _strict_json_object(raw: bytes) -> dict[str, object]:
    if not raw or len(raw) > _MAX_RESPONSE_BYTES:
        raise BybitCredentialRevocationError(
            "Bybit credential-revocation response size is invalid"
        )
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise BybitCredentialRevocationError(
            "Bybit credential-revocation response is not valid UTF-8"
        ) from error

    def pairs_hook(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if type(key) is not str or key in result:
                raise BybitCredentialRevocationError(
                    "Bybit credential-revocation response has duplicate/invalid key"
                )
            result[key] = value
        return result

    def reject_constant(value: str) -> object:
        raise BybitCredentialRevocationError(
            "Bybit credential-revocation response contains non-finite JSON"
        )

    try:
        value = json.loads(
            text,
            object_pairs_hook=pairs_hook,
            parse_constant=reject_constant,
        )
    except BybitCredentialRevocationError:
        raise
    except (json.JSONDecodeError, RecursionError) as error:
        raise BybitCredentialRevocationError(
            "Bybit credential-revocation response is invalid JSON"
        ) from error
    if type(value) is not dict:
        raise BybitCredentialRevocationError(
            "Bybit credential-revocation response must be an object"
        )
    return value
