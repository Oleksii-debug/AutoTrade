"""Authority-free Bybit credential non-acceptance semantics for WP-49.

This module performs no networking, owns no credential/session authority, and
cannot authorize recovery takeover.  It classifies an already-authenticated,
exact-domain Bybit V5 ``retCode`` only after another boundary has established
where the response came from and which historical credential generation was
used.  Callers must keep provider-origin, account/domain/generation, build,
network-route, chronology, and reconciliation authority separate.
"""

from __future__ import annotations

from enum import Enum

from .provider_core import ProviderCoreError


class BybitCredentialNonAcceptance(str, Enum):
    """What one exact-domain Bybit response says about the tested credential."""

    STILL_ACCEPTED = "STILL_ACCEPTED"
    REJECTED_EXACT_DOMAIN = "REJECTED_EXACT_DOMAIN"
    INCONCLUSIVE = "INCONCLUSIVE"


_SUPPORTED_FAMILIES = frozenset(
    {
        "SPOT",
        "MARGIN",
        "LINEAR_DERIVATIVES",
        "INVERSE_DERIVATIVES",
        "OPTIONS",
    }
)
_SPOT_FAMILIES = frozenset({"SPOT", "MARGIN"})
_DERIVATIVE_FAMILIES = frozenset(
    {"LINEAR_DERIVATIVES", "INVERSE_DERIVATIVES"}
)

# Bybit V5 codes whose meaning is deliberately *not* promoted to credential
# non-acceptance.  They can reflect a live credential with a bad request,
# permissions, signature, IP policy, or a generic authentication condition.
_EXPLICITLY_INCONCLUSIVE_AUTH_CODES = frozenset({10004, 10005, 10007, 10010})


def classify_bybit_credential_nonacceptance(
    *,
    ret_code: int | None,
    product_family: str,
    response_surface: str,
) -> BybitCredentialNonAcceptance:
    """Classify provider semantics without minting fence authority.

    ``ret_code`` must come from a separately authenticated exact-domain Bybit
    V5 UTA REST response. Bybit reuses code 10003 on its WebSocket OE surface
    for a different condition (too many sessions), so response-surface identity
    is mandatory before any rejection semantic is interpreted. ``None`` means
    no canonical Bybit JSON response code was
    established (for example an HTTP-only failure) and therefore fails closed
    as ``INCONCLUSIVE``.

    Semantics are intentionally narrow:
    - ``0`` proves the tested credential was still accepted on that request;
    - ``10003`` means invalid API key for the exact contacted Bybit domain;
    - ``-2015`` means an expired Spot API key and is accepted only for the
      SPOT/MARGIN product families;
    - ``33004`` means an expired derivatives API key and is accepted only for
      LINEAR/INVERSE derivatives;
    - signature/permission/generic-auth/IP and every other code remain
      inconclusive.

    This function never proves that the response is authentic, that the key was
    historically valid for the claimed account, that an old process stopped,
    or that recovery takeover is safe.
    """

    if type(product_family) is not str or product_family not in _SUPPORTED_FAMILIES:
        raise ProviderCoreError(
            "Bybit credential product_family must be a canonical supported family"
        )
    if type(response_surface) is not str or response_surface != "V5_UTA_REST":
        raise ProviderCoreError(
            "Bybit credential non-acceptance requires exact V5_UTA_REST surface"
        )
    if ret_code is not None and type(ret_code) is not int:
        raise ProviderCoreError("Bybit credential ret_code must be an exact integer or None")

    if ret_code is None:
        return BybitCredentialNonAcceptance.INCONCLUSIVE
    if ret_code == 0:
        return BybitCredentialNonAcceptance.STILL_ACCEPTED
    if ret_code == 10003:
        return BybitCredentialNonAcceptance.REJECTED_EXACT_DOMAIN
    if ret_code == -2015:
        if product_family in _SPOT_FAMILIES:
            return BybitCredentialNonAcceptance.REJECTED_EXACT_DOMAIN
        return BybitCredentialNonAcceptance.INCONCLUSIVE
    if ret_code == 33004:
        if product_family in _DERIVATIVE_FAMILIES:
            return BybitCredentialNonAcceptance.REJECTED_EXACT_DOMAIN
        return BybitCredentialNonAcceptance.INCONCLUSIVE
    if ret_code in _EXPLICITLY_INCONCLUSIVE_AUTH_CODES:
        return BybitCredentialNonAcceptance.INCONCLUSIVE
    return BybitCredentialNonAcceptance.INCONCLUSIVE
