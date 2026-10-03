"""Fail-closed public facade for WP-64 supply-chain qualification.

The reviewed qualification implementation remains byte-for-byte in
``_supply_chain_qualification_impl``.  This facade strengthens only the durable
proof ingress: authenticated proof bytes must pass the repository's existing
bounded strict-JSON resource fence before semantic reconstruction and canonical
byte equality are evaluated.
"""

from __future__ import annotations

import sys

from autotrade_runtime.strict_json import strict_json_loads

from . import _supply_chain_qualification_impl as _impl


def _parse_supply_chain_proof_bytes_bounded(
    raw: bytes,
) -> tuple[_impl.SupplyChainEvidence, _impl.SignedQualificationAttestation]:
    """Parse canonical durable WP-64 proof bytes through shared resource bounds."""

    if type(raw) is not bytes or not raw:
        raise ValueError("supply-chain proof bytes are required")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("supply-chain proof is not valid UTF-8 JSON") from error
    try:
        payload = strict_json_loads(text)
    except ValueError as error:
        raise ValueError(f"supply-chain proof JSON rejected: {error}") from error
    if type(payload) is not dict or set(payload) != {
        "schema_version",
        "evidence",
        "receipt",
    }:
        raise ValueError("supply-chain proof has unsupported structure")
    if payload["schema_version"] != _impl._SUPPLY_CHAIN_PROOF_SCHEMA_VERSION:
        raise ValueError("supply-chain proof schema version is unsupported")

    evidence = _impl._parse_supply_chain_evidence_payload(payload["evidence"])
    receipt = _impl.parse_signed_qualification_attestation(payload["receipt"])
    canonical = _impl.canonical_supply_chain_proof_bytes(evidence, receipt)
    if canonical != raw:
        raise ValueError("supply-chain proof is not canonical")
    return evidence, receipt


_impl.parse_supply_chain_proof_bytes = _parse_supply_chain_proof_bytes_bounded
sys.modules[__name__] = _impl
