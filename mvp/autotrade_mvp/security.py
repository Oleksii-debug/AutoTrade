"""Fail-closed credential/session boundary for the simulated AutoTrade host.

This foundation deliberately does not implement provider networking or a production
secret vault. It enforces the architectural boundary: UI/research/model callers
receive opaque credential handles, while raw secret material can be resolved only
for an explicitly authorized execution identity, account scope and purpose.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
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
    _CREDENTIAL_PURPOSES = {"TRADE", "READ"}

    def __init__(
        self,
        *,
        allowed_origins: set[str],
        now: Callable[[], float] | None = None,
    ) -> None:
        if not allowed_origins:
            raise ValueError("At least one authenticated origin is required")
        normalized_origins = {
            _required_text(value, name="allowed origin") for value in allowed_origins
        }
        self._paired_origins = set(normalized_origins)
        self._now = now or time.time
        self._sessions: dict[str, Session] = {}
        self._records: dict[str, _SecretRecord] = {}
        # Redaction history is deliberately separate from credential usability:
        # rotated/revoked values remain scrubbed from future diagnostics.
        self._secret_redactions: set[str] = set()

    def _now_value(self) -> float:
        value = self._now()
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise RuntimeError("Session clock must return a numeric timestamp")
        numeric = float(value)
        if not math.isfinite(numeric) or numeric < 0:
            raise RuntimeError("Session clock is not trustworthy")
        return numeric

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
        if normalized_origin not in self._paired_origins:
            raise PermissionError("Origin is not paired")
        if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int):
            raise ValueError("Session lifetime must be an integer number of seconds")
        if ttl_seconds <= 0 or ttl_seconds > 3600:
            raise ValueError("Session lifetime is outside the permitted bound")
        session = Session(
            token=secrets.token_urlsafe(32),
            subject=normalized_subject,
            role=normalized_role,
            origin=normalized_origin,
            expires_at=self._now_value() + ttl_seconds,
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
        normalized_token = _required_text(token, name="session token")
        session = self._sessions.get(normalized_token)
        if session is None:
            raise PermissionError("Unknown session")
        if session.origin not in self._paired_origins:
            self._sessions.pop(normalized_token, None)
            raise PermissionError("Session origin is no longer paired")
        if self._now_value() >= session.expires_at:
            self._sessions.pop(normalized_token, None)
            raise PermissionError("Session expired")
        if origin is not None and _required_text(origin, name="origin") != session.origin:
            raise PermissionError("Session origin mismatch")
        if required_roles is not None:
            normalized_roles = {
                _required_text(role, name="required role").upper() for role in required_roles
            }
            if not normalized_roles or not normalized_roles.issubset(self._ROLES):
                raise PermissionError("Unknown required role")
            if session.role not in normalized_roles:
                raise PermissionError("Role is not authorized")
        return session

    def validate_host_session(self, token: str, actor: str) -> bool:
        """Fail-closed adapter for HostCommandStore session identity checks.

        Action authorization remains a separate server-side policy concern.
        This method proves only that the bearer session is current and belongs
        to the exact actor named by the command.
        """
        try:
            normalized_actor = _required_text(actor, name="actor")
            session = self.validate_session(token)
        except (ValueError, PermissionError, RuntimeError):
            return False
        return secrets.compare_digest(session.subject, normalized_actor)

    def revoke_session(self, token: str) -> None:
        normalized_token = _required_text(token, name="session token")
        if self._sessions.pop(normalized_token, None) is None:
            raise PermissionError("Unknown session")

    def pair_origin(
        self,
        token: str,
        *,
        origin: str,
        new_origin: str,
    ) -> str:
        self.validate_session(token, required_roles={"OWNER"}, origin=origin)
        normalized = _required_text(new_origin, name="new origin")
        self._paired_origins.add(normalized)
        return normalized

    def unpair_origin(
        self,
        token: str,
        *,
        origin: str,
        paired_origin: str,
    ) -> None:
        owner = self.validate_session(token, required_roles={"OWNER"}, origin=origin)
        normalized = _required_text(paired_origin, name="paired origin")
        if normalized not in self._paired_origins:
            raise PermissionError("Origin is not paired")
        if normalized == owner.origin and len(self._paired_origins) == 1:
            raise PermissionError("Cannot remove the final authenticated origin")
        self._paired_origins.remove(normalized)
        for session_token, session in list(self._sessions.items()):
            if session.origin == normalized:
                self._sessions.pop(session_token, None)

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
        if normalized_purpose not in self._CREDENTIAL_PURPOSES:
            raise PermissionError("Credential purpose is unsupported")
        if not isinstance(secret_value, str) or not secret_value:
            raise ValueError("Secret value must not be empty")
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
        self._secret_redactions.add(secret_value)
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
        normalized_owner = _required_text(owner_identity, name="owner_identity")
        if record.owner_identity != normalized_owner:
            raise PermissionError("Secret identity mismatch")
        if not isinstance(new_secret_value, str) or not new_secret_value:
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
        self._secret_redactions.add(new_secret_value)
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
        normalized_owner = _required_text(owner_identity, name="owner_identity")
        if record.owner_identity != normalized_owner:
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
        if not isinstance(handle, CredentialHandle):
            raise PermissionError("Credential handle is invalid")
        record = self._require_active_record(handle.handle_id)
        current = record.handle
        if handle != current:
            raise PermissionError("Credential handle generation is stale")
        if _required_text(execution_identity, name="execution_identity") != record.owner_identity:
            raise PermissionError("Execution identity cannot decrypt this credential")
        if (
            _required_text(account_id, name="account_id") != current.account_id
            or _required_text(provider, name="provider") != current.provider
        ):
            raise PermissionError("Credential scope mismatch")
        if _required_text(purpose, name="purpose").upper() != current.purpose:
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
        return value

    def redact_for_diagnostics(self, value: object) -> object:
        """Redact sensitive keys plus any currently known raw secret material."""
        keyed = self.redact(value)
        known = tuple(sorted(self._secret_redactions, key=len, reverse=True))

        def scrub(item: object) -> object:
            if isinstance(item, dict):
                return {key: scrub(child) for key, child in item.items()}
            if isinstance(item, list):
                return [scrub(child) for child in item]
            if isinstance(item, tuple):
                return tuple(scrub(child) for child in item)
            if isinstance(item, str):
                result = item
                for secret_value in known:
                    if secret_value in result:
                        result = result.replace(secret_value, "[REDACTED]")
                return result
            return item

        return scrub(keyed)

    def _require_active_record(self, handle_id: str) -> _SecretRecord:
        normalized_handle = _required_text(handle_id, name="handle_id")
        record = self._records.get(normalized_handle)
        if record is None or not record.active:
            raise PermissionError("Credential is unavailable")
        return record
