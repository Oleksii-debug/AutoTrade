"""Fail-closed session/authorization boundary for the AutoTrade host.

Credential persistence and decryption are delegated to the canonical protected
credential vault. This module owns roles, paired origins and short-lived sessions;
session minting additionally requires an injected authenticated-identity/role
verifier, so a paired browser origin is never treated as authentication by itself.
It deliberately does not keep a second plaintext credential store.
"""

from __future__ import annotations

import math
import re
import secrets
import time
from dataclasses import dataclass
from typing import Callable, Iterable, Mapping
from urllib.parse import urlsplit

from .host_actions import required_roles_for_host_action
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
    idle_timeout_seconds: int
    idle_expires_at: float


class SecurityBoundary:
    """Authorization facade over one protected credential-vault authority."""

    _ROLES = {"OWNER", "OPERATOR", "RESEARCHER", "OBSERVER"}
    _EXECUTION_ROLES = {"OWNER", "OPERATOR"}
    _CREDENTIAL_PURPOSES = {"TRADE", "READ"}

    def __init__(
        self,
        *,
        allowed_origins: set[str],
        credential_vault: ProtectedCredentialVault,
        session_authorizer: Callable[[str, str, str], bool] | None = None,
        now: Callable[[], float] | None = None,
    ) -> None:
        if not allowed_origins:
            raise ValueError("At least one authenticated origin is required")
        if not isinstance(credential_vault, ProtectedCredentialVault):
            raise TypeError("credential_vault must be a ProtectedCredentialVault")
        self._paired_origins = {_authenticated_origin(value) for value in allowed_origins}
        self._credential_vault = credential_vault
        if session_authorizer is not None and not callable(session_authorizer):
            raise TypeError("session_authorizer must be callable or None")
        self._session_authorizer = session_authorizer
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

    @staticmethod
    def _session_lifetime(value: object, *, name: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{name} must be an integer number of seconds")
        if value <= 0 or value > 3600:
            raise ValueError(f"{name} is outside the permitted bound")
        return value

    def _authenticate_session_identity(
        self,
        *,
        subject: str,
        role: str,
        origin: str,
    ) -> None:
        if self._session_authorizer is None:
            raise PermissionError(
                "Session authentication verifier is unavailable"
            )
        try:
            authenticated = self._session_authorizer(subject, role, origin)
        except Exception as error:
            raise PermissionError("Session authentication failed") from error
        if authenticated is not True:
            raise PermissionError(
                "Session identity and role are not authenticated"
            )

    def _issue_session(
        self,
        *,
        subject: str,
        role: str,
        origin: str,
        ttl_seconds: int,
        idle_timeout_seconds: int,
        now: float,
    ) -> Session:
        idle_timeout = min(idle_timeout_seconds, ttl_seconds)
        expires_at = now + ttl_seconds
        session = Session(
            token=secrets.token_urlsafe(32),
            subject=subject,
            role=role,
            origin=origin,
            expires_at=expires_at,
            idle_timeout_seconds=idle_timeout,
            idle_expires_at=min(expires_at, now + idle_timeout),
        )
        self._sessions[session.token] = session
        return session

    def create_session(
        self,
        *,
        subject: str,
        role: str,
        origin: str,
        ttl_seconds: int = 900,
        idle_timeout_seconds: int | None = None,
    ) -> Session:
        normalized_subject = _required_text(subject, name="subject")
        normalized_role = _required_text(role, name="role").upper()
        normalized_origin = _authenticated_origin(origin)
        if normalized_role not in self._ROLES:
            raise PermissionError("Unknown role")
        if normalized_origin not in self._paired_origins:
            raise PermissionError("Origin is not paired")
        ttl = self._session_lifetime(ttl_seconds, name="Session lifetime")
        idle_timeout = (
            min(300, ttl)
            if idle_timeout_seconds is None
            else self._session_lifetime(
                idle_timeout_seconds,
                name="Session idle timeout",
            )
        )
        self._authenticate_session_identity(
            subject=normalized_subject,
            role=normalized_role,
            origin=normalized_origin,
        )
        now = self._now_value()
        return self._issue_session(
            subject=normalized_subject,
            role=normalized_role,
            origin=normalized_origin,
            ttl_seconds=ttl,
            idle_timeout_seconds=idle_timeout,
            now=now,
        )

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
        now = self._now_value()
        if now >= session.expires_at:
            self._sessions.pop(normalized_token, None)
            raise PermissionError("Session expired")
        if now >= session.idle_expires_at:
            self._sessions.pop(normalized_token, None)
            raise PermissionError("Session idle timeout expired")
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
        refreshed_idle = min(
            session.expires_at,
            now + session.idle_timeout_seconds,
        )
        if refreshed_idle != session.idle_expires_at:
            session = Session(
                token=session.token,
                subject=session.subject,
                role=session.role,
                origin=session.origin,
                expires_at=session.expires_at,
                idle_timeout_seconds=session.idle_timeout_seconds,
                idle_expires_at=refreshed_idle,
            )
            self._sessions[normalized_token] = session
        return session

    def refresh_session(
        self,
        token: str,
        *,
        origin: str,
        ttl_seconds: int = 900,
        idle_timeout_seconds: int | None = None,
    ) -> Session:
        """Re-authenticate and rotate a session without changing its identity scope."""

        normalized_token = _required_text(token, name="session token")
        prior = self._sessions.get(normalized_token)
        current = self.validate_session(normalized_token, origin=origin)
        ttl = self._session_lifetime(ttl_seconds, name="Session lifetime")
        idle_timeout = (
            current.idle_timeout_seconds
            if idle_timeout_seconds is None
            else self._session_lifetime(
                idle_timeout_seconds,
                name="Session idle timeout",
            )
        )
        try:
            self._authenticate_session_identity(
                subject=current.subject,
                role=current.role,
                origin=current.origin,
            )
        except PermissionError:
            if prior is not None and normalized_token in self._sessions:
                self._sessions[normalized_token] = prior
            raise
        now = self._now_value()
        replacement = self._issue_session(
            subject=current.subject,
            role=current.role,
            origin=current.origin,
            ttl_seconds=ttl,
            idle_timeout_seconds=idle_timeout,
            now=now,
        )
        self._sessions.pop(current.token, None)
        return replacement

    def validate_host_session(
        self,
        token: str,
        actor: str,
        origin: str,
        action: str,
    ) -> bool:
        """Validate identity, current origin and action-aware host mutation role.

        Read-only RESEARCHER/OBSERVER sessions must use read surfaces. Authority
        grant/revoke commands are Owner-only; other mutations may also be admitted
        for Operator and still require their normal server-side policy/capability
        checks outside this authentication boundary.
        """
        try:
            normalized_actor = _required_text(actor, name="actor")
            normalized_origin = _authenticated_origin(origin)
            required_roles = set(required_roles_for_host_action(action))
            session = self.validate_session(
                token,
                required_roles=required_roles,
                origin=normalized_origin,
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
