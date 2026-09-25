"""Fail-closed credential/session boundary for the simulated AutoTrade host.

This foundation deliberately does not implement provider networking or a production
secret vault. It enforces the architectural boundary: UI/research/model callers
receive opaque credential handles, while raw secret material can be resolved only
for an explicitly authorized execution identity, account scope and purpose.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
import secrets
import time
from typing import Callable, Mapping


_REDACT_RE = re.compile(
    r"(api[_-]?key|secret|password|passphrase|authorization|access[_-]?token|refresh[_-]?token|signature)",
    re.IGNORECASE,
)


def _required_text(value: object, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


@dataclass(frozen=True)
class CredentialHandle:
    handle_id: str
    account_id: str
    provider: str
    purpose: str
    generation: int


@dataclass(frozen=True)
class Session:
    token: str
    subject: str
    role: str
    origin: str
    expires_at: float


@dataclass
class _SecretRecord:
    handle: CredentialHandle
    owner_identity: str
    value: str
    active: bool = True


class SecurityBoundary:
    """In-memory boundary used to qualify authorization invariants.

    Production persistence must be backed by an identity-protected host secret
    store. This class intentionally provides no withdrawal/external-transfer
    capability.
    """

    _ROLES = {"OWNER", "OPERATOR", "RESEARCHER", "OBSERVER"}
    _EXECUTION_ROLES = {"OWNER", "OPERATOR"}
    _CREDENTIAL_PURPOSES = {"READ", "TRADE"}

    def __init__(
        self,
        *,
        allowed_origins: set[str],
        now: Callable[[], float] | None = None,
    ) -> None:
        if not allowed_origins:
            raise ValueError("At least one authenticated origin is required")
        normalized_origins = frozenset(
            _required_text(value, name="allowed origin") for value in allowed_origins
        )
        self._allowed_origins = normalized_origins
        self._now = now or time.time
        self._sessions: dict[str, Session] = {}
        self._records: dict[str, _SecretRecord] = {}

    def create_session(
        self,
        *,
        subject: str,
        role: str,
        origin: str,
        ttl_seconds: int = 900,
    ) -> Session:
        normalized_subject = _required_text(subject, name="subject")
        normalized_role = _required_text(role, name="role").upper()
        normalized_origin = _required_text(origin, name="origin")
        if normalized_role not in self._ROLES:
            raise PermissionError("Unknown role")
        if normalized_origin not in self._allowed_origins:
            raise PermissionError("Origin is not paired")
        if ttl_seconds <= 0 or ttl_seconds > 3600:
            raise ValueError("Session lifetime is outside the permitted bound")
        session = Session(
            token=secrets.token_urlsafe(32),
            subject=normalized_subject,
            role=normalized_role,
            origin=normalized_origin,
            expires_at=self._now() + ttl_seconds,
        )
        self._sessions[session.token] = session
        return session

    def validate_session(
        self,
        token: str,
        *,
        required_roles: set[str] | None = None,
        origin: str | None = None,
    ) -> Session:
        session = self._sessions.get(token)
        if session is None:
            raise PermissionError("Unknown session")
        if self._now() >= session.expires_at:
            self._sessions.pop(token, None)
            raise PermissionError("Session expired")
        if origin is not None and origin != session.origin:
            raise PermissionError("Session origin mismatch")
        if required_roles is not None and session.role not in required_roles:
            raise PermissionError("Role is not authorized")
        return session

    def register_secret(
        self,
        token: str,
        *,
        origin: str,
        owner_identity: str,
        account_id: str,
        provider: str,
        purpose: str,
        secret_value: str,
    ) -> CredentialHandle:
        self.validate_session(token, required_roles={"OWNER"}, origin=origin)
        normalized_owner = _required_text(owner_identity, name="owner_identity")
        normalized_account = _required_text(account_id, name="account_id")
        normalized_provider = _required_text(provider, name="provider")
        normalized_purpose = _required_text(purpose, name="purpose").upper()
        if not isinstance(secret_value, str) or not secret_value:
            raise ValueError("Secret value must not be empty")
        if normalized_purpose not in self._CREDENTIAL_PURPOSES:
            raise PermissionError(
                "Only READ and TRADE credential purposes are supported"
            )
        handle_id = "cred_" + secrets.token_hex(16)
        handle = CredentialHandle(
            handle_id=handle_id,
            account_id=normalized_account,
            provider=normalized_provider,
            purpose=normalized_purpose,
            generation=1,
        )
        self._records[handle_id] = _SecretRecord(
            handle=handle,
            owner_identity=normalized_owner,
            value=secret_value,
        )
        return handle

    def rotate_secret(
        self,
        token: str,
        *,
        origin: str,
        handle_id: str,
        owner_identity: str,
        new_secret_value: str,
    ) -> CredentialHandle:
        self.validate_session(token, required_roles={"OWNER"}, origin=origin)
        record = self._require_active_record(handle_id)
        if record.owner_identity != owner_identity:
            raise PermissionError("Secret identity mismatch")
        if not new_secret_value:
            raise ValueError("Secret value must not be empty")
        new_handle = CredentialHandle(
            handle_id=record.handle.handle_id,
            account_id=record.handle.account_id,
            provider=record.handle.provider,
            purpose=record.handle.purpose,
            generation=record.handle.generation + 1,
        )
        record.handle = new_handle
        record.value = new_secret_value
        return new_handle

    def revoke_secret(
        self,
        token: str,
        *,
        origin: str,
        handle_id: str,
        owner_identity: str,
    ) -> None:
        self.validate_session(token, required_roles={"OWNER"}, origin=origin)
        record = self._require_active_record(handle_id)
        if record.owner_identity != owner_identity:
            raise PermissionError("Secret identity mismatch")
        record.active = False

    def resolve_for_execution(
        self,
        token: str,
        *,
        origin: str,
        handle: CredentialHandle,
        execution_identity: str,
        account_id: str,
        provider: str,
        purpose: str,
    ) -> str:
        self.validate_session(token, required_roles=self._EXECUTION_ROLES, origin=origin)
        record = self._require_active_record(handle.handle_id)
        current = record.handle
        if handle != current:
            raise PermissionError("Credential handle generation is stale")
        if execution_identity != record.owner_identity:
            raise PermissionError("Execution identity cannot decrypt this credential")
        if account_id != current.account_id or provider != current.provider:
            raise PermissionError("Credential scope mismatch")
        if purpose.upper() != current.purpose:
            raise PermissionError("Credential purpose mismatch")
        return record.value

    def describe_handle(self, handle_id: str) -> Mapping[str, object]:
        record = self._require_active_record(handle_id)
        h = record.handle
        return {
            "handle_id": h.handle_id,
            "account_id": h.account_id,
            "provider": h.provider,
            "purpose": h.purpose,
            "generation": h.generation,
        }

    @staticmethod
    def redact(value: object) -> object:
        if isinstance(value, dict):
            return {
                key: "[REDACTED]" if _REDACT_RE.search(str(key)) else SecurityBoundary.redact(item)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [SecurityBoundary.redact(item) for item in value]
        if isinstance(value, tuple):
            return tuple(SecurityBoundary.redact(item) for item in value)
        if isinstance(value, str) and _REDACT_RE.search(value):
            return "[REDACTED]"
        return value

    def _require_active_record(self, handle_id: str) -> _SecretRecord:
        record = self._records.get(handle_id)
        if record is None or not record.active:
            raise PermissionError("Credential is unavailable")
        return record
