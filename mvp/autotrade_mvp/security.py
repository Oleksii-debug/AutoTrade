"""Fail-closed session/authorization boundary for the AutoTrade host.

Credential persistence and decryption are delegated to the canonical protected
credential vault. This module owns roles, paired origins and short-lived sessions;
it deliberately does not keep a second plaintext credential store.
"""

from __future__ import annotations

import math
import re
import secrets
import time
from dataclasses import dataclass
from typing import Callable, Iterable, Mapping
from urllib.parse import urlsplit

from .windows_secrets import PersistentCredentialHandle, ProtectedCredentialVault


_REDACT_RE = re.compile(
    r"(api[_-]?key|secret|password|passphrase|authorization|access[_-]?token|refresh[_-]?token|signature)",
    re.IGNORECASE,
)

CredentialHandle = PersistentCredentialHandle


def _required_text(value: object, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _authenticated_origin(value: object) -> str:
    origin = _required_text(value, name="origin")
    parsed = urlsplit(origin)
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("Origin must not contain user information")
    if not parsed.hostname or parsed.query or parsed.fragment:
        raise ValueError("Origin must be an absolute origin without query or fragment")
    if parsed.path not in ("", "/"):
        raise ValueError("Origin must not contain a path")
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError("Origin port is invalid") from error
    scheme = parsed.scheme.lower()
    host = parsed.hostname.lower()
    if scheme == "https":
        default_port = 443
    elif scheme == "http" and host in {"localhost", "127.0.0.1", "::1"}:
        default_port = 80
    else:
        raise ValueError("Origin must use HTTPS or loopback HTTP")
    rendered_host = f"[{host}]" if ":" in host else host
    suffix = "" if port is None or port == default_port else f":{port}"
    return f"{scheme}://{rendered_host}{suffix}"


@dataclass(frozen=True)
class Session:
    token: str
    subject: str
    role: str
    origin: str
    expires_at: float


class SecurityBoundary:
    """Authorization facade over one protected credential-vault authority."""

    _ROLES = {"OWNER", "OPERATOR", "RESEARCHER", "OBSERVER"}
    _EXECUTION_ROLES = {"OWNER", "OPERATOR"}
    _HOST_COMMAND_ROLES = {"OWNER", "OPERATOR"}
    _CREDENTIAL_PURPOSES = {"TRADE", "READ"}

    def __init__(
        self,
        *,
        allowed_origins: set[str],
        credential_vault: ProtectedCredentialVault,
        now: Callable[[], float] | None = None,
    ) -> None:
        if not allowed_origins:
            raise ValueError("At least one authenticated origin is required")
        if not isinstance(credential_vault, ProtectedCredentialVault):
            raise TypeError("credential_vault must be a ProtectedCredentialVault")
        self._paired_origins = {_authenticated_origin(value) for value in allowed_origins}
        self._credential_vault = credential_vault
        self._now = now or time.time
        self._sessions: dict[str, Session] = {}

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
        normalized_origin = _authenticated_origin(origin)
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
        if origin is not None and _authenticated_origin(origin) != session.origin:
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
        """Validate identity and role for the state-mutating host command layer.

        Read-only RESEARCHER/OBSERVER sessions must use read surfaces; they cannot
        become mutation authority merely by presenting a valid bearer token.
        """
        try:
            normalized_actor = _required_text(actor, name="actor")
            session = self.validate_session(
                token,
                required_roles=self._HOST_COMMAND_ROLES,
            )
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
        normalized = _authenticated_origin(new_origin)
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
        normalized = _authenticated_origin(paired_origin)
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
        environment: str,
        purpose: str,
        secret_value: str,
    ) -> CredentialHandle:
        self.validate_session(token, required_roles={"OWNER"}, origin=origin)
        normalized_purpose = _required_text(purpose, name="purpose").upper()
        if normalized_purpose not in self._CREDENTIAL_PURPOSES:
            raise PermissionError("Credential purpose is unsupported")
        return self._credential_vault.register(
            owner_identity=_required_text(owner_identity, name="owner_identity"),
            account_id=_required_text(account_id, name="account_id"),
            provider=_required_text(provider, name="provider"),
            environment=_required_text(environment, name="environment").upper(),
            purpose=normalized_purpose,
            secret_value=secret_value,
        )

    def _current_handle(self, handle_id: str) -> CredentialHandle:
        metadata = self._credential_vault.describe(
            _required_text(handle_id, name="handle_id")
        )
        return CredentialHandle(
            handle_id=str(metadata["handle_id"]),
            account_id=str(metadata["account_id"]),
            provider=str(metadata["provider"]),
            environment=str(metadata["environment"]),
            purpose=str(metadata["purpose"]),
            generation=int(metadata["generation"]),
        )

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
        current = self._current_handle(handle_id)
        return self._credential_vault.rotate(
            current,
            execution_identity=_required_text(owner_identity, name="owner_identity"),
            new_secret_value=new_secret_value,
        )

    def revoke_secret(
        self,
        token: str,
        *,
        origin: str,
        handle_id: str,
        owner_identity: str,
    ) -> None:
        self.validate_session(token, required_roles={"OWNER"}, origin=origin)
        current = self._current_handle(handle_id)
        self._credential_vault.revoke(
            current,
            execution_identity=_required_text(owner_identity, name="owner_identity"),
        )

    def resolve_for_execution(
        self,
        token: str,
        *,
        origin: str,
        handle: CredentialHandle,
        execution_identity: str,
        account_id: str,
        provider: str,
        environment: str,
        purpose: str,
    ) -> str:
        self.validate_session(token, required_roles=self._EXECUTION_ROLES, origin=origin)
        if not isinstance(handle, CredentialHandle):
            raise PermissionError("Credential handle is invalid")
        return self._credential_vault.resolve(
            handle,
            execution_identity=_required_text(
                execution_identity, name="execution_identity"
            ),
            account_id=_required_text(account_id, name="account_id"),
            provider=_required_text(provider, name="provider"),
            environment=_required_text(environment, name="environment").upper(),
            purpose=_required_text(purpose, name="purpose").upper(),
        )

    def describe_handle(self, handle_id: str) -> Mapping[str, object]:
        return self._credential_vault.describe(
            _required_text(handle_id, name="handle_id")
        )

    @staticmethod
    def redact(value: object) -> object:
        if isinstance(value, Mapping):
            return {
                key: "[REDACTED]"
                if _REDACT_RE.search(str(key))
                else SecurityBoundary.redact(item)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [SecurityBoundary.redact(item) for item in value]
        if isinstance(value, tuple):
            return tuple(SecurityBoundary.redact(item) for item in value)
        if isinstance(value, set):
            return {SecurityBoundary.redact(item) for item in value}
        if isinstance(value, frozenset):
            return frozenset(SecurityBoundary.redact(item) for item in value)
        if isinstance(value, str) and _REDACT_RE.search(value):
            return "[REDACTED]"
        return value

    @staticmethod
    def redact_for_diagnostics(
        value: object,
        *,
        sensitive_values: Iterable[str] = (),
    ) -> object:
        """Scrub sensitive keys and caller-owned ephemeral secret values.

        The boundary intentionally does not retain plaintext values just to support
        later redaction. Callers that still hold a plaintext transient may supply it
        for this single redaction operation.
        """
        keyed = SecurityBoundary.redact(value)
        known: list[str] = []
        for candidate in sensitive_values:
            if not isinstance(candidate, str) or not candidate:
                raise ValueError("sensitive_values must contain non-empty strings")
            known.append(candidate)
        known.sort(key=len, reverse=True)

        def scrub(item: object) -> object:
            if isinstance(item, Mapping):
                return {key: scrub(child) for key, child in item.items()}
            if isinstance(item, list):
                return [scrub(child) for child in item]
            if isinstance(item, tuple):
                return tuple(scrub(child) for child in item)
            if isinstance(item, set):
                return {scrub(child) for child in item}
            if isinstance(item, frozenset):
                return frozenset(scrub(child) for child in item)
            if isinstance(item, str):
                result = item
                for secret_value in known:
                    if secret_value in result:
                        result = result.replace(secret_value, "[REDACTED]")
                return result
            return item

        return scrub(keyed)
