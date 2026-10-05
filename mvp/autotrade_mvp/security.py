"""Fail-closed session/authorization boundary for the AutoTrade host.

Credential persistence and decryption are delegated to the canonical protected
credential vault. This module owns roles, paired origins and short-lived sessions;
session minting additionally requires an injected authenticated-identity/role
verifier, so a paired browser origin is never treated as authentication by itself.
It deliberately does not keep a second plaintext credential store.
"""

from __future__ import annotations

from contextlib import contextmanager
import math
import re
import secrets
import time
from dataclasses import dataclass
from types import FunctionType
from typing import Callable, Iterable, Mapping
from urllib.parse import urlsplit
from weakref import WeakKeyDictionary

from . import windows_secrets as _windows_secrets
from .host_actions import required_roles_for_host_action
from .windows_secrets import PersistentCredentialHandle, ProtectedCredentialVault


_REDACT_RE = re.compile(
    r"(api[_-]?key|secret|password|passphrase|authorization|access[_-]?token|refresh[_-]?token|signature)",
    re.IGNORECASE,
)

CredentialHandle = PersistentCredentialHandle


class _RetainedCredentialScopePolicy:
    """Immutable scope allowlists used by the terminal credential lease."""

    ALLOWED_PURPOSES = frozenset(ProtectedCredentialVault.ALLOWED_PURPOSES)
    ALLOWED_ENVIRONMENTS = frozenset(ProtectedCredentialVault.ALLOWED_ENVIRONMENTS)


def _retain_function_globals(function, *, globals_override=None):
    """Retain one Python executable while snapshotting its direct global bindings."""

    code = getattr(function, "__code__", None)
    globals_map = getattr(function, "__globals__", None)
    if code is None or type(globals_map) is not dict:
        raise TypeError("credential authority executable is not canonical Python code")
    retained_globals = dict(globals_map)
    if globals_override:
        retained_globals.update(globals_override)
    retained = FunctionType(
        code,
        retained_globals,
        function.__name__,
        function.__defaults__,
        function.__closure__,
    )
    if function.__kwdefaults__ is not None:
        retained.__kwdefaults__ = dict(function.__kwdefaults__)
    retained.__doc__ = function.__doc__
    retained.__qualname__ = function.__qualname__
    return retained


def _required_text(value: object, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _credential_text(value: object, *, name: str, uppercase: bool = False) -> str:
    """Reject executable text subclasses before credential-scope callbacks."""
    if type(value) is not str or not value.strip():
        raise ValueError(f"{name} must be exact non-empty text")
    normalized = value.strip()
    return normalized.upper() if uppercase else normalized


class _RetainedCredentialUnprotector:
    """Hold the exact protector unprotect binding selected at composition."""

    __slots__ = ("__unprotect", "__function", "__code")

    def __init__(self, protector: object) -> None:
        unprotect = getattr(protector, "unprotect", None)
        if not callable(unprotect):
            raise TypeError("credential protector must retain callable unprotect")
        function = getattr(unprotect, "__func__", None)
        executable = function if function is not None else unprotect
        self.__unprotect = unprotect
        self.__function = function
        self.__code = getattr(executable, "__code__", None)

    def unprotect(self, ciphertext: bytes, *, entropy: bytes) -> bytes:
        function = self.__function
        if function is not None:
            if getattr(self.__unprotect, "__func__", None) is not function:
                raise PermissionError("Credential protector binding changed")
            if (
                self.__code is not None
                and getattr(function, "__code__", None) is not self.__code
            ):
                raise PermissionError("Credential protector code changed")
        elif (
            self.__code is not None
            and getattr(self.__unprotect, "__code__", None) is not self.__code
        ):
            raise PermissionError("Credential protector code changed")
        return self.__unprotect(ciphertext, entropy=entropy)


class _RetainedCredentialLeaseView:
    """Read current durable vault state through construction-retained authority."""

    __slots__ = (
        "path",
        "lock_path",
        "FORMAT_VERSION",
        "_protector",
        "__normalize_scope",
        "__normalize_scope_code",
        "__load",
        "__load_code",
        "__handle",
        "__handle_code",
    )

    def __init__(
        self,
        vault: ProtectedCredentialVault,
        *,
        normalize_scope,
        load,
        handle_reader,
        format_version: int,
    ) -> None:
        if type(vault) is not ProtectedCredentialVault:
            raise TypeError("terminal credential vault authority must be exact")
        if type(format_version) is not int or format_version < 1:
            raise TypeError("credential vault format authority is invalid")
        normalize_scope_code = getattr(normalize_scope, "__code__", None)
        load_code = getattr(load, "__code__", None)
        handle_code = getattr(handle_reader, "__code__", None)
        if (
            normalize_scope_code is None
            or load_code is None
            or handle_code is None
        ):
            raise TypeError("credential vault helper authority is not canonical")
        self.path = vault.path
        self.lock_path = vault.lock_path
        self.FORMAT_VERSION = format_version
        self._protector = _RetainedCredentialUnprotector(vault._protector)
        self.__normalize_scope = normalize_scope
        self.__normalize_scope_code = normalize_scope_code
        self.__load = load
        self.__load_code = load_code
        self.__handle = handle_reader
        self.__handle_code = handle_code

    def _normalize_scope(self, **kwargs):
        normalize_scope = self.__normalize_scope
        if getattr(normalize_scope, "__code__", None) is not self.__normalize_scope_code:
            raise PermissionError("Credential scope normalization code changed")
        return normalize_scope(**kwargs)

    def _load(self):
        load = self.__load
        if getattr(load, "__code__", None) is not self.__load_code:
            raise PermissionError("Credential vault load code changed")
        return load(self)

    def _handle(self, record):
        handle_reader = self.__handle
        if getattr(handle_reader, "__code__", None) is not self.__handle_code:
            raise PermissionError("Credential handle reader code changed")
        return handle_reader(record)


def _build_execution_lease_authority(
    *,
    validate_session,
    vault_lease,
    vault_for_boundary,
    credential_text,
    handle_type,
    execution_roles,
):
    """Install the terminal credential-use path over retained executables.

    The returned public method deliberately exposes no callback/override parameters.
    It retains the exact session validator, scope normalizer and vault lease selected
    when this module is constructed, while the retained vault lease continues to
    read current durable credential state on every use.
    """

    validate_session_code = getattr(validate_session, "__code__", None)
    credential_text_code = getattr(credential_text, "__code__", None)
    vault_lease_code = getattr(vault_lease, "__code__", None)
    vault_lease_generator = getattr(vault_lease, "__wrapped__", None)
    vault_lease_generator_code = getattr(vault_lease_generator, "__code__", None)
    vault_for_boundary_code = getattr(vault_for_boundary, "__code__", None)
    if (
        validate_session_code is None
        or credential_text_code is None
        or vault_lease_code is None
        or not callable(vault_lease_generator)
        or vault_lease_generator_code is None
        or vault_for_boundary_code is None
    ):
        raise TypeError("credential lease authority is not canonical")
    retained_roles = frozenset(execution_roles)

    @contextmanager
    def lease_for_execution(
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
        provider_environment: str | None = None,
    ):
        """Authorize and hold one exact credential generation for terminal use."""
        if getattr(validate_session, "__code__", None) is not validate_session_code:
            raise PermissionError("Session validation authority code changed")
        if getattr(credential_text, "__code__", None) is not credential_text_code:
            raise PermissionError("Credential scope authority code changed")
        if getattr(vault_lease, "__code__", None) is not vault_lease_code:
            raise PermissionError("Credential vault lease authority code changed")
        if getattr(vault_lease, "__wrapped__", None) is not vault_lease_generator:
            raise PermissionError("Credential vault lease implementation changed")
        if (
            getattr(vault_lease_generator, "__code__", None)
            is not vault_lease_generator_code
        ):
            raise PermissionError("Credential vault lease implementation code changed")
        if getattr(vault_for_boundary, "__code__", None) is not vault_for_boundary_code:
            raise PermissionError("Credential vault object authority code changed")

        validate_session(
            self,
            token,
            required_roles=set(retained_roles),
            origin=origin,
        )
        if not isinstance(handle, handle_type):
            raise PermissionError("Credential handle is invalid")
        vault = vault_for_boundary(self)
        with vault_lease(
            vault,
            handle,
            execution_identity=credential_text(
                execution_identity,
                name="execution_identity",
            ),
            account_id=credential_text(account_id, name="account_id"),
            provider=credential_text(provider, name="provider"),
            environment=credential_text(
                environment,
                name="environment",
                uppercase=True,
            ),
            purpose=credential_text(
                purpose,
                name="purpose",
                uppercase=True,
            ),
            provider_environment=provider_environment,
        ) as plaintext:
            yield plaintext

    return lease_for_execution


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
        provider_environment: str | None = None,
    ) -> CredentialHandle:
        self.validate_session(token, required_roles={"OWNER"}, origin=origin)
        normalized_purpose = _credential_text(
            purpose,
            name="purpose",
            uppercase=True,
        )
        if normalized_purpose not in self._CREDENTIAL_PURPOSES:
            raise PermissionError("Credential purpose is unsupported")
        return self._credential_vault.register(
            owner_identity=_credential_text(
                owner_identity,
                name="owner_identity",
            ),
            account_id=_credential_text(account_id, name="account_id"),
            provider=_credential_text(provider, name="provider"),
            environment=_credential_text(
                environment,
                name="environment",
                uppercase=True,
            ),
            purpose=normalized_purpose,
            secret_value=secret_value,
            provider_environment=provider_environment,
        )

    def _current_handle(self, handle_id: str) -> CredentialHandle:
        metadata = self._credential_vault.describe(
            _credential_text(handle_id, name="handle_id")
        )
        return CredentialHandle(
            handle_id=metadata["handle_id"],
            account_id=metadata["account_id"],
            provider=metadata["provider"],
            environment=metadata["environment"],
            provider_environment=metadata["provider_environment"],
            purpose=metadata["purpose"],
            generation=metadata["generation"],
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
            execution_identity=_credential_text(
                owner_identity,
                name="owner_identity",
            ),
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
            execution_identity=_credential_text(
                owner_identity,
                name="owner_identity",
            ),
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
        provider_environment: str | None = None,
    ) -> str:
        self.validate_session(token, required_roles=self._EXECUTION_ROLES, origin=origin)
        if not isinstance(handle, CredentialHandle):
            raise PermissionError("Credential handle is invalid")
        return self._credential_vault.resolve(
            handle,
            execution_identity=_credential_text(
                execution_identity,
                name="execution_identity",
            ),
            account_id=_credential_text(account_id, name="account_id"),
            provider=_credential_text(provider, name="provider"),
            environment=_credential_text(
                environment,
                name="environment",
                uppercase=True,
            ),
            purpose=_credential_text(
                purpose,
                name="purpose",
                uppercase=True,
            ),
            provider_environment=provider_environment,
        )

    def describe_handle(self, handle_id: str) -> Mapping[str, object]:
        return self._credential_vault.describe(
            _credential_text(handle_id, name="handle_id")
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


def _install_security_boundary_execution_authority(boundary_type) -> None:
    """Bind each initialized boundary to one retained terminal vault view."""

    lease_views = WeakKeyDictionary()
    original_init = boundary_type.__init__
    original_init_code = getattr(original_init, "__code__", None)

    retained_text = _retain_function_globals(_windows_secrets._text)
    retained_provider_environment = _retain_function_globals(
        _windows_secrets._provider_environment,
        globals_override={"_text": retained_text},
    )
    retained_vault_leaf = _retain_function_globals(
        _windows_secrets._require_vault_leaf
    )
    retained_lock_binding = _retain_function_globals(
        _windows_secrets._assert_posix_lock_binding
    )
    lock_generator = getattr(
        _windows_secrets._exclusive_file_lock,
        "__wrapped__",
        None,
    )
    retained_lock_generator = _retain_function_globals(
        lock_generator,
        globals_override={"_assert_posix_lock_binding": retained_lock_binding},
    )
    retained_file_lock = contextmanager(retained_lock_generator)
    retained_scope_entropy = _retain_function_globals(_windows_secrets._scope_entropy)
    retained_b64decode = _windows_secrets.b64decode

    vault_normalize_scope = _retain_function_globals(
        ProtectedCredentialVault._normalize_scope,
        globals_override={
            "_text": retained_text,
            "_provider_environment": retained_provider_environment,
            "ProtectedCredentialVault": _RetainedCredentialScopePolicy,
        },
    )
    vault_load = _retain_function_globals(
        ProtectedCredentialVault._load,
        globals_override={
            "_require_vault_leaf": retained_vault_leaf,
            "_text": retained_text,
            "PersistentCredentialHandle": PersistentCredentialHandle,
            "b64decode": retained_b64decode,
        },
    )
    vault_handle = _retain_function_globals(
        ProtectedCredentialVault._handle,
        globals_override={"PersistentCredentialHandle": PersistentCredentialHandle},
    )
    vault_lease_generator = getattr(
        ProtectedCredentialVault.lease,
        "__wrapped__",
        None,
    )
    retained_vault_lease_generator = _retain_function_globals(
        vault_lease_generator,
        globals_override={
            "PersistentCredentialHandle": PersistentCredentialHandle,
            "_exclusive_file_lock": retained_file_lock,
            "_scope_entropy": retained_scope_entropy,
            "b64decode": retained_b64decode,
        },
    )
    retained_vault_lease = contextmanager(retained_vault_lease_generator)
    vault_format_version = ProtectedCredentialVault.FORMAT_VERSION
    if (
        original_init_code is None
        or getattr(vault_normalize_scope, "__code__", None) is None
        or getattr(vault_load, "__code__", None) is None
        or getattr(vault_handle, "__code__", None) is None
        or type(vault_format_version) is not int
    ):
        raise TypeError("SecurityBoundary credential authority is not canonical")

    def vault_for_boundary(boundary):
        try:
            return lease_views[boundary]
        except KeyError as error:
            raise PermissionError(
                "Credential vault object authority is unavailable"
            ) from error

    def retained_init(
        self,
        *,
        allowed_origins: set[str],
        credential_vault: ProtectedCredentialVault,
        session_authorizer: Callable[[str, str, str], bool] | None = None,
        now: Callable[[], float] | None = None,
    ) -> None:
        if getattr(original_init, "__code__", None) is not original_init_code:
            raise PermissionError("SecurityBoundary constructor authority code changed")
        original_init(
            self,
            allowed_origins=allowed_origins,
            credential_vault=credential_vault,
            session_authorizer=session_authorizer,
            now=now,
        )
        lease_views[self] = _RetainedCredentialLeaseView(
            credential_vault,
            normalize_scope=vault_normalize_scope,
            load=vault_load,
            handle_reader=vault_handle,
            format_version=vault_format_version,
        )

    boundary_type.__init__ = retained_init
    boundary_type.lease_for_execution = _build_execution_lease_authority(
        validate_session=boundary_type.validate_session,
        vault_lease=retained_vault_lease,
        vault_for_boundary=vault_for_boundary,
        credential_text=_credential_text,
        handle_type=PersistentCredentialHandle,
        execution_roles=boundary_type._EXECUTION_ROLES,
    )


_install_security_boundary_execution_authority(SecurityBoundary)
