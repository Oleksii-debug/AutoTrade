"""Fail-closed provider×asset lifecycle qualification crosswalk for WP-61.

This module does not implement provider transport or financial lifecycle math. It
derives advertised product families from the canonical provider registry and
checks whether exact-build integration evidence covers the lifecycle semantics
required for those families. Passing this gate never grants trading authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Iterable, Mapping
from uuid import UUID

from autotrade_runtime.artifacts import trusted_authenticated_reader
from autotrade_runtime.artifacts.store import ArtifactIntegrityError, ArtifactStore

from .provider_core import PROVIDERS, provider_definition
from .qualification_attestation import (
    QualificationTrustError,
    SignedQualificationAttestation,
    verify_canonical_qualification_attestation,
)


class CrosswalkError(ValueError):
    pass


class Lifecycle(StrEnum):
    FUTURES = "FUTURES"
    PERPETUAL = "PERPETUAL"
    OPTIONS = "OPTIONS"
    CORPORATE = "CORPORATE"


_COMMON_CASES = frozenset({"reconciliation"})
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_EVIDENCE_MEDIA_TYPE = "application/vnd.autotrade.asset-provider-lifecycle"
_EVIDENCE_KIND = "ASSET_PROVIDER_LIFECYCLE"
_QUALIFICATION_DOMAIN = "ASSET_PROVIDER_CROSSWALK"
_QUALIFICATION_GATE = "INTEGRATION"
_QUALIFICATION_PACKAGE = "WP-61"
_QUALIFICATION_PROTOCOL = "asset-provider-crosswalk-v1"
_QUALIFICATION_PROTOCOL_VERSION = "1.0.0"
_QUALIFICATION_REQUIREMENT = "complete-advertised-lifecycle-matrix"
_LIFECYCLE_CASES: Mapping[Lifecycle, frozenset[str]] = {
    Lifecycle.FUTURES: frozenset({"expiry", "cross_currency_fees"}),
    Lifecycle.PERPETUAL: frozenset({"funding", "cross_currency_fees"}),
    Lifecycle.OPTIONS: frozenset(
        {"expiry", "assignment", "adjusted_contracts", "cross_currency_fees"}
    ),
    Lifecycle.CORPORATE: frozenset({"adjusted_contracts", "cross_currency_fees"}),
}

# This is the integration crosswalk itself, not a second provider registry.
# Keys must already exist in provider_core.PROVIDERS; validation below enforces it.
_PROVIDER_FAMILY_LIFECYCLES: Mapping[
    tuple[str, str], tuple[Lifecycle, ...]
] = {
    ("BYBIT", "LINEAR_DERIVATIVES"): (Lifecycle.FUTURES, Lifecycle.PERPETUAL),
    ("BYBIT", "INVERSE_DERIVATIVES"): (Lifecycle.FUTURES, Lifecycle.PERPETUAL),
    ("BYBIT", "OPTIONS"): (Lifecycle.OPTIONS,),
    ("KRAKEN", "DERIVATIVES"): (Lifecycle.FUTURES, Lifecycle.PERPETUAL),
    ("WHITEBIT", "FUTURES"): (Lifecycle.FUTURES, Lifecycle.PERPETUAL),
    ("BINANCE", "USD_M"): (Lifecycle.FUTURES, Lifecycle.PERPETUAL),
    ("BINANCE", "COIN_M"): (Lifecycle.FUTURES, Lifecycle.PERPETUAL),
    ("BINANCE", "OPTIONS"): (Lifecycle.OPTIONS,),
    ("IBKR", "FUTURES"): (Lifecycle.FUTURES,),
    ("IBKR", "OPTIONS"): (Lifecycle.OPTIONS,),
    ("IBKR", "EQUITIES"): (Lifecycle.CORPORATE,),
    ("ALPACA", "EQUITIES"): (Lifecycle.CORPORATE,),
    ("ALPACA", "OPTIONS"): (Lifecycle.OPTIONS,),
}


def _text(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CrosswalkError(f"{name} is required")
    return value.strip()


def _exact_text(value: object, name: str) -> str:
    """Admit only non-polymorphic strings to the terminal crosswalk graph."""

    if type(value) is not str or not value.strip():
        raise CrosswalkError(f"{name} must be an exact non-empty string")
    return value.strip()


def _sha(value: str, name: str) -> str:
    normalized = _text(value, name)
    if len(normalized) != 40 or any(ch not in "0123456789abcdef" for ch in normalized):
        raise CrosswalkError(f"{name} must be a 40-character lowercase Git SHA")
    return normalized


def _artifact_id(value: str) -> str:
    try:
        return str(UUID(_text(value, "artifact_id")))
    except (ValueError, AttributeError, TypeError) as error:
        raise CrosswalkError("artifact_id must be a UUID") from error


def _digest(value: str) -> str:
    result = _text(value, "artifact_sha256")
    if _DIGEST.fullmatch(result) is None:
        raise CrosswalkError("artifact_sha256 must be canonical sha256")
    return result


@dataclass(frozen=True, order=True)
class CrosswalkKey:
    provider_id: str
    product_family: str
    lifecycle: Lifecycle

    def __post_init__(self) -> None:
        provider = _text(self.provider_id, "provider_id").upper()
        family = _text(self.product_family, "product_family")
        definition = provider_definition(provider)
        if family not in definition.product_families:
            raise CrosswalkError("product family is not advertised by provider registry")
        if not isinstance(self.lifecycle, Lifecycle):
            raise CrosswalkError("lifecycle must be a Lifecycle")
        allowed = _PROVIDER_FAMILY_LIFECYCLES.get((provider, family), ())
        if self.lifecycle not in allowed:
            raise CrosswalkError("lifecycle is not declared for provider product family")
        object.__setattr__(self, "provider_id", provider)
        object.__setattr__(self, "product_family", family)


@dataclass(frozen=True)
class LifecycleEvidence:
    key: CrosswalkKey
    source_sha: str
    adapter_sha: str
    cases: frozenset[str]
    reconciliation_complete: bool
    economic_units_exact: bool
    artifact_id: str
    artifact_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_sha", _sha(self.source_sha, "source_sha"))
        object.__setattr__(self, "adapter_sha", _sha(self.adapter_sha, "adapter_sha"))
        normalized_cases = frozenset(_text(case, "case") for case in self.cases)
        object.__setattr__(self, "cases", normalized_cases)
        if not isinstance(self.reconciliation_complete, bool):
            raise CrosswalkError("reconciliation_complete must be boolean")
        if not isinstance(self.economic_units_exact, bool):
            raise CrosswalkError("economic_units_exact must be boolean")
        object.__setattr__(self, "artifact_id", _artifact_id(self.artifact_id))
        object.__setattr__(self, "artifact_sha256", _digest(self.artifact_sha256))


def _snapshot_crosswalk_key(value: CrosswalkKey) -> CrosswalkKey:
    """Detach one exact provider×asset key before any authority callback."""

    if type(value) is not CrosswalkKey:
        raise CrosswalkError("evidence key must be exact CrosswalkKey")
    provider_id = _exact_text(
        object.__getattribute__(value, "provider_id"), "provider_id"
    )
    product_family = _exact_text(
        object.__getattribute__(value, "product_family"), "product_family"
    )
    lifecycle = object.__getattribute__(value, "lifecycle")
    if type(lifecycle) is not Lifecycle:
        raise CrosswalkError("lifecycle must be exact Lifecycle")
    return CrosswalkKey(provider_id, product_family, lifecycle)


def _snapshot_lifecycle_evidence(value: LifecycleEvidence) -> LifecycleEvidence:
    """Freeze the full decision-relevant lifecycle graph once at entry."""

    if type(value) is not LifecycleEvidence:
        raise CrosswalkError("evidence entries must be exact LifecycleEvidence")
    key = _snapshot_crosswalk_key(object.__getattribute__(value, "key"))
    source_sha = _exact_text(
        object.__getattribute__(value, "source_sha"), "source_sha"
    )
    adapter_sha = _exact_text(
        object.__getattribute__(value, "adapter_sha"), "adapter_sha"
    )
    cases = object.__getattribute__(value, "cases")
    if type(cases) is not frozenset or any(type(case) is not str for case in cases):
        raise CrosswalkError("cases must be an exact frozenset of exact strings")
    reconciliation_complete = object.__getattribute__(
        value, "reconciliation_complete"
    )
    economic_units_exact = object.__getattribute__(value, "economic_units_exact")
    if type(reconciliation_complete) is not bool:
        raise CrosswalkError("reconciliation_complete must be exact boolean")
    if type(economic_units_exact) is not bool:
        raise CrosswalkError("economic_units_exact must be exact boolean")
    artifact_id = _exact_text(
        object.__getattribute__(value, "artifact_id"), "artifact_id"
    )
    artifact_sha256 = _exact_text(
        object.__getattribute__(value, "artifact_sha256"), "artifact_sha256"
    )
    return LifecycleEvidence(
        key=key,
        source_sha=source_sha,
        adapter_sha=adapter_sha,
        cases=cases,
        reconciliation_complete=reconciliation_complete,
        economic_units_exact=economic_units_exact,
        artifact_id=artifact_id,
        artifact_sha256=artifact_sha256,
    )


def _snapshot_adapter_shas(
    value: Mapping[tuple[str, str], str],
) -> dict[tuple[str, str], str]:
    """Detach caller adapter authority before filesystem or signer callbacks."""

    if type(value) is not dict:
        raise CrosswalkError("exact_adapter_shas must be an exact dict")
    snapshot: dict[tuple[str, str], str] = {}
    for key, adapter_sha in value.items():
        if (
            type(key) is not tuple
            or len(key) != 2
            or any(type(part) is not str for part in key)
        ):
            raise CrosswalkError(
                "exact_adapter_shas keys must be exact (provider, family) string tuples"
            )
        provider_id = _exact_text(key[0], "adapter provider_id").upper()
        product_family = _exact_text(key[1], "adapter product_family")
        adapter = _sha(_exact_text(adapter_sha, "exact adapter sha"), "exact adapter sha")
        normalized_key = (provider_id, product_family)
        if normalized_key in snapshot:
            raise CrosswalkError("duplicate exact adapter identity")
        snapshot[normalized_key] = adapter
    return snapshot


def lifecycle_evidence_payload(item: LifecycleEvidence) -> dict[str, object]:
    if not isinstance(item, LifecycleEvidence):
        raise TypeError("item must be LifecycleEvidence")
    return {
        "evidence_kind": _EVIDENCE_KIND,
        "provider_id": item.key.provider_id,
        "product_family": item.key.product_family,
        "lifecycle": item.key.lifecycle.value,
        "source_sha": item.source_sha,
        "adapter_sha": item.adapter_sha,
        "cases": sorted(item.cases),
        "reconciliation_complete": item.reconciliation_complete,
        "economic_units_exact": item.economic_units_exact,
    }


def lifecycle_evidence_bytes(item: LifecycleEvidence) -> bytes:
    return json.dumps(
        lifecycle_evidence_payload(item),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")


def _stored_evidence_matches(read_snapshot, item: LifecycleEvidence) -> bool:
    try:
        manifest, data = read_snapshot(item.artifact_id)
        if manifest.get("sha256") != item.artifact_sha256:
            return False
        if "sha256:" + sha256(data).hexdigest() != item.artifact_sha256:
            return False
        if data != lifecycle_evidence_bytes(item):
            return False
        if manifest.get("media_type") != _EVIDENCE_MEDIA_TYPE:
            return False
        if manifest.get("source_refs") != [f"git:{item.source_sha}"]:
            return False
        if manifest.get("metadata") != lifecycle_evidence_payload(item):
            return False
        if not isinstance(manifest.get("manifest_hash"), str):
            return False
    except (
        ArtifactIntegrityError,
        FileNotFoundError,
        OSError,
        TypeError,
        ValueError,
    ):
        return False
    return True


@dataclass(frozen=True)
class CrosswalkVerdict:
    status: str
    missing_keys: tuple[CrosswalkKey, ...]
    invalid_keys: tuple[CrosswalkKey, ...]
    trading_authority_granted: bool = False
    reason_codes: tuple[str, ...] = ()


def advertised_lifecycle_keys() -> tuple[CrosswalkKey, ...]:
    """Return every lifecycle combination explicitly advertised by the crosswalk.

    Importantly, this function validates every pair against provider_core so the
    crosswalk cannot silently advertise a provider family absent from the
    canonical provider registry.
    """

    keys: list[CrosswalkKey] = []
    for (provider_id, product_family), lifecycles in _PROVIDER_FAMILY_LIFECYCLES.items():
        definition = PROVIDERS.get(provider_id)
        if definition is None or product_family not in definition.product_families:
            raise CrosswalkError("crosswalk drifted from canonical provider registry")
        for lifecycle in lifecycles:
            keys.append(CrosswalkKey(provider_id, product_family, lifecycle))
    return tuple(sorted(keys))


def required_cases(lifecycle: Lifecycle) -> frozenset[str]:
    if not isinstance(lifecycle, Lifecycle):
        raise CrosswalkError("lifecycle must be a Lifecycle")
    return _COMMON_CASES | _LIFECYCLE_CASES[lifecycle]


def qualify_asset_provider_crosswalk(
    evidence: Iterable[LifecycleEvidence],
    *,
    exact_source_sha: str,
    exact_adapter_shas: Mapping[tuple[str, str], str],
    evidence_store: ArtifactStore | None = None,
    evidence_root: str | Path | None = None,
    qualification_receipt: SignedQualificationAttestation | None = None,
) -> CrosswalkVerdict:
    """Fail closed unless every advertised combination has exact complete evidence."""

    source_sha = _sha(
        _exact_text(exact_source_sha, "exact_source_sha"), "exact_source_sha"
    )
    if type(evidence) not in (list, tuple):
        raise CrosswalkError("evidence must be an exact list or tuple")
    evidence_items = tuple(_snapshot_lifecycle_evidence(item) for item in evidence)
    adapter_shas = _snapshot_adapter_shas(exact_adapter_shas)

    if evidence_store is not None and type(evidence_store) is not ArtifactStore:
        raise TypeError(
            "evidence_store must be ArtifactStore (canonical exact type required)"
        )
    trusted_read = None
    if evidence_store is not None and evidence_root is not None:
        try:
            trusted_read = trusted_authenticated_reader(
                evidence_root,
                publication_store=evidence_store,
            )
        except (
            ArtifactIntegrityError,
            FileNotFoundError,
            OSError,
            TypeError,
            ValueError,
        ):
            trusted_read = None
    expected = advertised_lifecycle_keys()
    expected_set = set(expected)

    by_key: dict[CrosswalkKey, LifecycleEvidence] = {}
    invalid: set[CrosswalkKey] = set()
    reasons: list[str] = []

    for item in evidence_items:
        if item.key not in expected_set:
            raise CrosswalkError("evidence contains a non-advertised lifecycle combination")
        if item.key in by_key:
            raise CrosswalkError("duplicate lifecycle evidence key")
        by_key[item.key] = item
        if trusted_read is None or not _stored_evidence_matches(trusted_read, item):
            invalid.add(item.key)

        expected_adapter = adapter_shas.get(
            (item.key.provider_id, item.key.product_family)
        )
        if expected_adapter is None:
            invalid.add(item.key)
            continue
        missing_cases = required_cases(item.key.lifecycle) - item.cases
        if (
            item.source_sha != source_sha
            or item.adapter_sha != expected_adapter
            or missing_cases
            or not item.reconciliation_complete
            or not item.economic_units_exact
        ):
            invalid.add(item.key)

    if evidence_store is None:
        reasons.append("immutable_evidence_store_unavailable")
    elif evidence_root is None:
        reasons.append("immutable_evidence_root_unavailable")
    elif trusted_read is None:
        reasons.append("immutable_evidence_root_invalid")
    if qualification_receipt is None:
        reasons.append("independent_evidence_trust_unavailable")
    elif evidence_store is None or evidence_root is None:
        reasons.append("independent_evidence_trust_incomplete")
    else:
        expected_refs = {
            (
                item.artifact_id,
                item.artifact_sha256,
                item.source_sha,
                _EVIDENCE_MEDIA_TYPE,
                _EVIDENCE_KIND,
            )
            for item in evidence_items
        }
        try:
            accepted = verify_canonical_qualification_attestation(
                qualification_receipt,
                evidence_store=evidence_store,
                evidence_root=evidence_root,
                expected_source_sha=source_sha,
                expected_domain=_QUALIFICATION_DOMAIN,
                expected_gate=_QUALIFICATION_GATE,
                expected_package_id=_QUALIFICATION_PACKAGE,
                expected_protocol_id=_QUALIFICATION_PROTOCOL,
                expected_protocol_version=_QUALIFICATION_PROTOCOL_VERSION,
                expected_requirement_id=_QUALIFICATION_REQUIREMENT,
            )
        except (QualificationTrustError, TypeError, ValueError):
            reasons.append("independent_evidence_trust_invalid")
        else:
            if accepted.result != "PASS":
                reasons.append(
                    "independent_evidence_result_" + accepted.result.lower()
                )
            accepted_refs = {
                (
                    ref.artifact_id,
                    ref.sha256,
                    ref.source_sha,
                    ref.media_type,
                    ref.evidence_kind,
                )
                for ref in accepted.evidence_refs
            }
            if accepted_refs != expected_refs:
                reasons.append("independent_evidence_set_mismatch")

    missing = tuple(sorted(expected_set - set(by_key)))
    invalid_keys = tuple(sorted(invalid))

    status = "PASS"
    if missing or invalid_keys or reasons:
        status = "INCOMPLETE"

    return CrosswalkVerdict(
        status=status,
        missing_keys=missing,
        invalid_keys=invalid_keys,
        trading_authority_granted=False,
        reason_codes=tuple(dict.fromkeys(reasons)),
    )
