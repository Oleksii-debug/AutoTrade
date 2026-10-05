"""Immutable terminal-settlement convention identity for derivative contracts.

The value in this module is contract metadata, not provider qualification or
settlement evidence.  `InstrumentVersion` owns whether a convention is accepted
for a specific immutable instrument version; terminal settlement consumers must
not accept caller-selected quantum/rounding independently of that instrument.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import re
from uuid import UUID

from .exact_decimal import (
    ExactDecimalError,
    canonical_decimal_text,
    parse_canonical_decimal_text,
)
from .persistence import canonical_json


_SCHEMA_VERSION = "1.0.0"
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_ALLOWED_ROUNDING = frozenset({"HALF_EVEN", "DOWN"})


class SettlementConventionError(ValueError):
    """Raised when terminal settlement contract metadata is non-canonical."""


def _text(value: object, *, name: str, upper: bool = False) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise SettlementConventionError(f"{name} must be canonical non-empty text")
    return value.upper() if upper else value


def _uuid(value: object, *, name: str) -> str:
    raw = _text(value, name=name)
    try:
        canonical = str(UUID(raw))
    except (TypeError, ValueError, AttributeError) as error:
        raise SettlementConventionError(f"{name} must be a canonical UUID") from error
    if raw != canonical:
        raise SettlementConventionError(f"{name} must use canonical UUID text")
    return canonical


def _digest(value: object, *, name: str) -> str:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        raise SettlementConventionError(
            f"{name} must be canonical lowercase sha256:<64-hex>"
        )
    return value


def _positive_int(value: object, *, name: str) -> int:
    if type(value) is not int or value < 1:
        raise SettlementConventionError(f"{name} must be a positive exact integer")
    return value


def _positive_decimal_text(value: object, *, name: str) -> str:
    if type(value) is not str:
        raise SettlementConventionError(f"{name} must be canonical Decimal text")
    try:
        parsed = parse_canonical_decimal_text(value)
        canonical = canonical_decimal_text(parsed)
    except (ExactDecimalError, TypeError, ValueError) as error:
        raise SettlementConventionError(
            f"{name} must be canonical bounded Decimal text"
        ) from error
    if canonical != value or parsed <= 0:
        raise SettlementConventionError(f"{name} must be canonical and positive")
    return canonical


@dataclass(frozen=True, slots=True)
class SettlementConvention:
    """One versioned terminal settlement quantum/rounding contract."""

    provider_id: str
    instrument_id: str
    instrument_version: int
    settlement_currency: str
    quantum: str
    rounding: str
    evidence_artifact_id: str
    evidence_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "provider_id",
            _text(self.provider_id, name="provider_id", upper=True),
        )
        object.__setattr__(
            self,
            "instrument_id",
            _uuid(self.instrument_id, name="instrument_id"),
        )
        object.__setattr__(
            self,
            "instrument_version",
            _positive_int(self.instrument_version, name="instrument_version"),
        )
        object.__setattr__(
            self,
            "settlement_currency",
            _text(self.settlement_currency, name="settlement_currency", upper=True),
        )
        object.__setattr__(
            self,
            "quantum",
            _positive_decimal_text(self.quantum, name="quantum"),
        )
        rounding = _text(self.rounding, name="rounding", upper=True)
        if rounding not in _ALLOWED_ROUNDING:
            raise SettlementConventionError(
                f"rounding must be one of {sorted(_ALLOWED_ROUNDING)}"
            )
        object.__setattr__(self, "rounding", rounding)
        object.__setattr__(
            self,
            "evidence_artifact_id",
            _uuid(self.evidence_artifact_id, name="evidence_artifact_id"),
        )
        object.__setattr__(
            self,
            "evidence_sha256",
            _digest(self.evidence_sha256, name="evidence_sha256"),
        )

    def payload(self) -> dict[str, object]:
        return {
            "schema_version": _SCHEMA_VERSION,
            "provider_id": self.provider_id,
            "instrument_id": self.instrument_id,
            "instrument_version": self.instrument_version,
            "settlement_currency": self.settlement_currency,
            "quantum": self.quantum,
            "rounding": self.rounding,
            "evidence_artifact_id": self.evidence_artifact_id,
            "evidence_sha256": self.evidence_sha256,
        }

    @property
    def convention_id(self) -> str:
        digest = sha256(canonical_json(self.payload()).encode("utf-8")).hexdigest()
        return "settlement-convention:sha256:" + digest
