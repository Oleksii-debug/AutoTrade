"""Immutable content identity for one accepted provider-account read cut.

This value is deliberately not an issuer.  WP-20/#697 owns acquisition,
provider-origin verification, coverage/consistency proof and current-head
issuance.  This module gives those authorities one exact value to seal and gives
risk/reservation/dispatch consumers one exact historical identity to retain.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import re

from .persistence import canonical_json
from .provider_domain import ProviderFinancialScope


_SCHEMA_VERSION = "1.0.0"
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_PROVIDER_QUALIFICATION_DIGEST_RE = re.compile(
    r"^provider-qualification:sha256:[0-9a-f]{64}$"
)
_MODES = frozenset({"PROVIDER_NATIVE_GENERATION", "SERIALIZED_ACQUISITION_GENERATION"})


class ProviderAccountCutError(ValueError):
    """Raised when provider-account cut identity is non-canonical."""


def _text(value: object, *, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ProviderAccountCutError(f"{name} must be canonical non-empty text")
    return value


def _digest(value: object, *, name: str) -> str:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        raise ProviderAccountCutError(
            f"{name} must be canonical lowercase sha256:<64-hex>"
        )
    return value


def _qualification_identity_digest(value: object) -> str:
    if (
        type(value) is not str
        or _PROVIDER_QUALIFICATION_DIGEST_RE.fullmatch(value) is None
    ):
        raise ProviderAccountCutError(
            "qualification_identity_digest must be canonical "
            "provider-qualification:sha256:<64-hex>"
        )
    return value


def _nonnegative_int(value: object, *, name: str) -> int:
    if type(value) is not int or value < 0:
        raise ProviderAccountCutError(f"{name} must be a non-negative exact integer")
    return value


def _positive_int(value: object, *, name: str) -> int:
    if type(value) is not int or value < 1:
        raise ProviderAccountCutError(f"{name} must be a positive exact integer")
    return value


@dataclass(frozen=True, slots=True)
class ProviderAccountCutIdentity:
    """Canonical immutable identity of the account evidence set used financially."""

    provider_scope: ProviderFinancialScope
    account_id: str
    acquisition_mode: str
    acquisition_id: str
    acquisition_generation: int
    acquisition_journal_sequence_cut: int
    qualification_identity_digest: str
    consistency_method_id: str
    consistency_method_version: int
    origin_binding_set_digest: str
    stream_binding_set_digest: str | None
    backfill_binding_set_digest: str | None
    coverage_window_digest: str
    provider_native_generation_token: str | None = None

    def __post_init__(self) -> None:
        if type(self.provider_scope) is not ProviderFinancialScope:
            raise ProviderAccountCutError(
                "provider_scope must be exact ProviderFinancialScope"
            )
        object.__setattr__(self, "account_id", _text(self.account_id, name="account_id"))
        mode = _text(self.acquisition_mode, name="acquisition_mode")
        if mode not in _MODES:
            raise ProviderAccountCutError(
                "acquisition_mode must be an exact canonical acquisition model"
            )
        object.__setattr__(self, "acquisition_mode", mode)
        object.__setattr__(
            self,
            "acquisition_id",
            _text(self.acquisition_id, name="acquisition_id"),
        )
        object.__setattr__(
            self,
            "acquisition_generation",
            _positive_int(self.acquisition_generation, name="acquisition_generation"),
        )
        object.__setattr__(
            self,
            "acquisition_journal_sequence_cut",
            _nonnegative_int(
                self.acquisition_journal_sequence_cut,
                name="acquisition_journal_sequence_cut",
            ),
        )
        object.__setattr__(
            self,
            "qualification_identity_digest",
            _qualification_identity_digest(self.qualification_identity_digest),
        )
        for name in (
            "origin_binding_set_digest",
            "coverage_window_digest",
        ):
            object.__setattr__(self, name, _digest(getattr(self, name), name=name))
        for name in ("stream_binding_set_digest", "backfill_binding_set_digest"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _digest(value, name=name))
        object.__setattr__(
            self,
            "consistency_method_id",
            _text(self.consistency_method_id, name="consistency_method_id"),
        )
        object.__setattr__(
            self,
            "consistency_method_version",
            _positive_int(
                self.consistency_method_version,
                name="consistency_method_version",
            ),
        )
        token = self.provider_native_generation_token
        if mode == "PROVIDER_NATIVE_GENERATION":
            if token is None:
                raise ProviderAccountCutError(
                    "provider_native_generation_token is required for provider-native acquisition"
                )
            object.__setattr__(
                self,
                "provider_native_generation_token",
                _text(token, name="provider_native_generation_token"),
            )
        elif token is not None:
            raise ProviderAccountCutError(
                "serialized acquisition cannot carry provider-native generation token"
            )

    def payload(self) -> dict[str, object]:
        return {
            "schema_version": _SCHEMA_VERSION,
            "provider_scope": self.provider_scope.payload(),
            "provider_scope_digest": self.provider_scope.content_digest,
            "account_id": self.account_id,
            "acquisition_mode": self.acquisition_mode,
            "acquisition_id": self.acquisition_id,
            "acquisition_generation": self.acquisition_generation,
            "acquisition_journal_sequence_cut": self.acquisition_journal_sequence_cut,
            "qualification_identity_digest": self.qualification_identity_digest,
            "consistency_method_id": self.consistency_method_id,
            "consistency_method_version": self.consistency_method_version,
            "origin_binding_set_digest": self.origin_binding_set_digest,
            "stream_binding_set_digest": self.stream_binding_set_digest,
            "backfill_binding_set_digest": self.backfill_binding_set_digest,
            "coverage_window_digest": self.coverage_window_digest,
            "provider_native_generation_token": self.provider_native_generation_token,
        }

    @property
    def content_digest(self) -> str:
        digest = sha256(canonical_json(self.payload()).encode("utf-8")).hexdigest()
        return "provider-account-cut:sha256:" + digest
