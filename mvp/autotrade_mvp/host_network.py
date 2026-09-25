"""Authenticated versioned network transport for the canonical AutoTrade host API.

This module is intentionally a transport adapter only.  Financial/control truth
remains in JournalBackedHostCommandStore and SecurityBoundary.  It never sends
provider orders and never turns command acceptance into financial completion.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import re
import ssl
from types import MappingProxyType
from typing import Callable, Mapping
from urllib.parse import parse_qs, unquote, urlsplit

from .durable_host_api import JournalBackedHostCommandStore
from .host_api import EventGap, command_result_payload, operation_result_payload
from .persistence import JournalStore
from .security import SecurityBoundary, _authenticated_origin


_REQUEST_ORIGIN: ContextVar[str | None] = ContextVar(
    "autotrade_host_request_origin", default=None
)
_AUTHENTICATED_SESSION_TOKEN: ContextVar[str | None] = ContextVar(
    "autotrade_authenticated_session_token", default=None
)
_AUTHENTICATED_SESSION_REFERENCE: ContextVar[str | None] = ContextVar(
    "autotrade_authenticated_session_reference", default=None
)
_OPERATION_PATH = re.compile(
    r"^/api/v1/operations/([0-9a-fA-F-]{36})$"
)
_MAX_BODY_BYTES = 1024 * 1024
_SNAPSHOT_FIELDS = {
    "state_version",
    "event_cursor",
    "server_time",
    "host_id",
    "account_id",
    "environment",
    "permission_summary",
    "connection_freshness",
    "portfolio",
    "risk",
    "strategy",
    "jobs",
    "reason_codes",
}


def public_session_reference(token: str) -> str:
    """Return a non-secret stable reference for one high-entropy bearer session.

    The bearer token remains header-only.  UiCommand.session and UiSnapshot
    permission metadata use this one-way reference so durable command hashing,
    projection state and diagnostics never receive the credential itself.
    """

    if (
        not isinstance(token, str)
        or not token
        or token != token.strip()
    ):
        raise ValueError("session token is invalid")
    material = ("autotrade-ui-session-v1\0" + token).encode("utf-8")
    return "sid-" + sha256(material).hexdigest()


@dataclass(frozen=True)
class HostPrincipal:
    actor: str
    token: str
    session: str

    def __post_init__(self) -> None:
        if not isinstance(self.actor, str) or not self.actor.strip():
            raise ValueError("principal actor is required")
        if (
            not isinstance(self.token, str)
            or not self.token
            or self.token != self.token.strip()
        ):
            raise ValueError("principal bearer token is required")
        if not isinstance(self.session, str) or not self.session.strip():
            raise ValueError("principal session reference is required")
        if self.session != public_session_reference(self.token):
            raise ValueError("principal session reference does not match bearer token")
        object.__setattr__(self, "actor", self.actor.strip())


@dataclass(frozen=True)
class SnapshotPrincipal:
    """Non-secret identity exposed to presentation-only snapshot projection."""

    actor: str
    session: str


@dataclass(frozen=True)
class TransportResponse:
    status: int
    content_type: str
    body: bytes
    headers: tuple[tuple[str, str], ...] = ()


PrincipalResolver = Callable[[Mapping[str, str], str], HostPrincipal]
SnapshotProvider = Callable[
    [Mapping[str, object], SnapshotPrincipal], Mapping[str, object]
]


def header_principal_resolver(
    headers: Mapping[str, str],
    origin: str,
) -> HostPrincipal:
    """Resolve a pre-paired session from explicit request headers.

    Production browser composition may supply a cookie/mTLS resolver instead.
    The network layer intentionally does not mint sessions.
    """

    del origin
    actor = headers.get("x-autotrade-actor")
    authorization = headers.get("authorization")
    prefix = "AutoTrade-Session "
    if (
        not isinstance(actor, str)
        or not actor.strip()
        or not isinstance(authorization, str)
        or not authorization.startswith(prefix)
    ):
        raise PermissionError("Authenticated host session is required")
    token = authorization[len(prefix) :]
    return HostPrincipal(
        actor=actor,
        token=token,
        session=public_session_reference(token),
    )


def _request_origin() -> str:
    value = _REQUEST_ORIGIN.get()
    if value is None:
        raise PermissionError("Current request origin is unavailable")
    return value


def _json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _json_response(
    status: int,
    value: object,
    *,
    headers: tuple[tuple[str, str], ...] = (),
) -> TransportResponse:
    return TransportResponse(
        status=status,
        content_type="application/json; charset=utf-8",
        body=_json_bytes(value),
        headers=headers,
    )


def _error(status: int, code: str) -> TransportResponse:
    return _json_response(status, {"error": code})


def _headers(values: Mapping[str, str]) -> Mapping[str, str]:
    normalized: dict[str, str] = {}
    for key, value in values.items():
        name = str(key).strip().lower()
        if not name or name in normalized:
            raise ValueError("Duplicate or invalid request header")
        normalized[name] = str(value).strip()
    return MappingProxyType(normalized)


def _is_loopback_bind(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class AuthenticatedHostApplication:
    """Pure request dispatcher around one durable host command authority."""

    def __init__(
        self,
        journal: JournalStore,
        *,
        security_boundary: SecurityBoundary,
        account_id: str,
        environment: str,
        host_id: str,
        public_origin: str,
        principal_resolver: PrincipalResolver,
        snapshot_provider: SnapshotProvider,
        max_events: int = 100,
        now: Callable[[], str] | None = None,
    ) -> None:
        if not isinstance(journal, JournalStore):
            raise TypeError("journal must be JournalStore")
        if not isinstance(security_boundary, SecurityBoundary):
            raise TypeError("security_boundary must be SecurityBoundary")
        if not isinstance(host_id, str) or not host_id.strip():
            raise ValueError("host_id is required")
        if not callable(principal_resolver):
            raise TypeError("principal_resolver must be callable")
        if not callable(snapshot_provider):
            raise TypeError("snapshot_provider must be callable")
        self.security_boundary = security_boundary
        self.host_id = host_id.strip()
        self.public_origin = _authenticated_origin(public_origin)
        self._principal_resolver = principal_resolver
        self._snapshot_provider = snapshot_provider
        self._now = now
        self.store = JournalBackedHostCommandStore(
            journal,
            account_id=account_id,
            environment=environment,
            session_validator=self._validate_command_session,
            request_origin_provider=_request_origin,
            max_events=max_events,
            now=now,
        )

    def _validate_command_session(
        self,
        session_reference: str,
        actor: str,
        origin: str,
        action: str,
    ) -> bool:
        bearer = _AUTHENTICATED_SESSION_TOKEN.get()
        expected_reference = _AUTHENTICATED_SESSION_REFERENCE.get()
        if (
            bearer is None
            or expected_reference is None
            or session_reference != expected_reference
        ):
            return False
        return self.security_boundary.validate_host_session(
            bearer,
            actor,
            origin,
            action,
        )

    def _principal(self, headers: Mapping[str, str]) -> HostPrincipal:
        principal = self._principal_resolver(headers, self.public_origin)
        if not isinstance(principal, HostPrincipal):
            raise TypeError("principal_resolver must return HostPrincipal")
        session = self.security_boundary.validate_session(
            principal.token,
            origin=self.public_origin,
        )
        if session.subject != principal.actor:
            raise PermissionError("Authenticated actor mismatch")
        return principal

    def _snapshot(self, principal: HostPrincipal) -> Mapping[str, object]:
        durable = self.store.snapshot()
        projected = self._snapshot_provider(
            MappingProxyType(dict(durable)),
            SnapshotPrincipal(actor=principal.actor, session=principal.session),
        )
        if not isinstance(projected, Mapping):
            raise TypeError("snapshot_provider must return a mapping")
        payload = dict(projected)
        if set(payload) != _SNAPSHOT_FIELDS:
            raise ValueError("UiSnapshot fields do not match the canonical contract")
        for field in ("state_version", "event_cursor", "account_id", "environment"):
            if str(payload[field]) != str(durable[field]):
                raise ValueError(f"UiSnapshot {field} does not match durable host truth")
        if payload["host_id"] != self.host_id:
            raise ValueError("UiSnapshot host_id does not match the configured host")
        for field in (
            "permission_summary",
            "connection_freshness",
            "portfolio",
            "risk",
            "strategy",
        ):
            if not isinstance(payload[field], Mapping):
                raise ValueError(f"UiSnapshot {field} must be an object")
        if not payload["connection_freshness"]:
            raise ValueError("UiSnapshot connection_freshness evidence is required")
        permission = payload["permission_summary"]
        if permission.get("actor") != principal.actor:
            raise ValueError("UiSnapshot actor does not match authenticated principal")
        if permission.get("session") != principal.session:
            raise ValueError("UiSnapshot session does not match authenticated principal")
        if not isinstance(payload["jobs"], list) or any(
            not isinstance(item, Mapping) for item in payload["jobs"]
        ):
            raise ValueError("UiSnapshot jobs must be an array of objects")
        reasons = payload["reason_codes"]
        if not isinstance(reasons, list) or any(
            not isinstance(item, str) or not item for item in reasons
        ):
            raise ValueError("UiSnapshot reason_codes must be non-empty strings")
        server_time = payload["server_time"]
        if (
            not isinstance(server_time, str)
            or re.fullmatch(
                r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z",
                server_time,
            )
            is None
        ):
            raise ValueError("UiSnapshot server_time must be a canonical UTC instant")
        rendered = _json_bytes(payload)
        if principal.token.encode("utf-8") in rendered:
            raise ValueError("UiSnapshot must never contain bearer credential material")
        return MappingProxyType(payload)

    @staticmethod
    def _parse_body(body: bytes, headers: Mapping[str, str]) -> Mapping[str, object]:
        content_type = headers.get("content-type", "")
        if content_type.split(";", 1)[0].strip().lower() != "application/json":
            raise ValueError("JSON request content type is required")
        if not body or len(body) > _MAX_BODY_BYTES:
            raise ValueError("Request body size is invalid")
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("Request body is not canonical JSON") from error
        if not isinstance(value, dict):
            raise ValueError("Command request must be a JSON object")
        return value

    @staticmethod
    def _event_payload(event) -> dict[str, object]:
        return {
            "cursor": str(event.cursor),
            "kind": event.kind,
            "state_version": str(event.state_version),
            "payload": dict(event.payload),
        }

    def dispatch(
        self,
        *,
        method: str,
        target: str,
        headers: Mapping[str, str],
        body: bytes = b"",
    ) -> TransportResponse:
        try:
            normalized_headers = _headers(headers)
            supplied_origin = normalized_headers.get("origin")
            if (
                supplied_origin is not None
                and _authenticated_origin(supplied_origin) != self.public_origin
            ):
                raise PermissionError("Request origin does not match authenticated host origin")
            parsed = urlsplit(target)
            if parsed.scheme or parsed.netloc or parsed.fragment:
                return _error(400, "INVALID_REQUEST_TARGET")
            path = parsed.path
            query = parse_qs(parsed.query, keep_blank_values=True)

            if method == "GET" and path == "/api/v1/health":
                now = (
                    self._now()
                    if self._now is not None
                    else datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
                )
                return _json_response(
                    200,
                    {
                        "component": "HOST_NETWORK",
                        "as_of": now,
                        "status": "READY",
                        "affected_scope": ["HOST_API"],
                        "reason_codes": [],
                    },
                    headers=(("Cache-Control", "no-store"),),
                )

            principal = self._principal(normalized_headers)

            if method == "GET" and path == "/api/v1/state":
                return _json_response(
                    200,
                    dict(self._snapshot(principal)),
                    headers=(("Cache-Control", "no-store"),),
                )

            if method == "POST" and path == "/api/v1/commands":
                command = self._parse_body(body, normalized_headers)
                if command.get("actor") != principal.actor:
                    raise PermissionError("Command actor is not authenticated")
                if command.get("session") != principal.session:
                    raise PermissionError("Command session is not authenticated")
                origin_token = _REQUEST_ORIGIN.set(self.public_origin)
                bearer_token = _AUTHENTICATED_SESSION_TOKEN.set(principal.token)
                reference_token = _AUTHENTICATED_SESSION_REFERENCE.set(
                    principal.session
                )
                try:
                    result = self.store.submit(command)
                finally:
                    _AUTHENTICATED_SESSION_REFERENCE.reset(reference_token)
                    _AUTHENTICATED_SESSION_TOKEN.reset(bearer_token)
                    _REQUEST_ORIGIN.reset(origin_token)
                status = 409 if result.status == "CONFLICT" else 200
                return _json_response(
                    status,
                    command_result_payload(result),
                    headers=(("Cache-Control", "no-store"),),
                )

            operation_match = _OPERATION_PATH.fullmatch(path)
            if method == "GET" and operation_match is not None:
                operation_id = unquote(operation_match.group(1))
                result = self.store.get_operation(operation_id)
                return _json_response(
                    200,
                    operation_result_payload(result),
                    headers=(("Cache-Control", "no-store"),),
                )

            if method == "GET" and path == "/api/v1/events":
                if set(query) - {"after"} or len(query.get("after", ["0"])) != 1:
                    return _error(400, "INVALID_EVENT_CURSOR")
                after = query.get("after", ["0"])[0]
                events = tuple(
                    self._event_payload(event)
                    for event in self.store.events_after(after)
                )
                accept = normalized_headers.get("accept", "application/json")
                if "text/event-stream" in accept:
                    chunks = []
                    for event in events:
                        chunks.append(
                            "id: "
                            + event["cursor"]
                            + "\nevent: "
                            + event["kind"]
                            + "\ndata: "
                            + _json_bytes(event).decode("utf-8")
                            + "\n\n"
                        )
                    return TransportResponse(
                        status=200,
                        content_type="text/event-stream; charset=utf-8",
                        body="".join(chunks).encode("utf-8"),
                        headers=(
                            ("Cache-Control", "no-store"),
                            ("X-Accel-Buffering", "no"),
                        ),
                    )
                return _json_response(
                    200,
                    list(events),
                    headers=(("Cache-Control", "no-store"),),
                )

            return _error(404, "NOT_FOUND")
        except EventGap:
            return _json_response(
                409,
                {
                    "error": "EVENT_CURSOR_GAP",
                    "resnapshot": "/api/v1/state",
                },
                headers=(("Cache-Control", "no-store"),),
            )
        except PermissionError:
            return _error(403, "AUTHENTICATION_OR_AUTHORIZATION_FAILED")
        except KeyError:
            return _error(404, "OPERATION_NOT_FOUND")
        except (TypeError, ValueError, OverflowError):
            return _error(400, "INVALID_REQUEST")


class _HostRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:
        # Request URLs/headers/bodies may contain sensitive session material.
        # Diagnostics belong in the canonical redacted observability path.
        return

    def _handle(self) -> None:
        server = self.server
        if not isinstance(server, AuthenticatedHostServer):
            self.send_error(500)
            return
        try:
            for name in (
                "Authorization",
                "X-AutoTrade-Actor",
                "Origin",
                "Content-Length",
            ):
                if len(self.headers.get_all(name, [])) > 1:
                    raise ValueError("Duplicate sensitive request header")
            length_text = self.headers.get("Content-Length", "0")
            length = int(length_text)
            if length < 0 or length > _MAX_BODY_BYTES:
                response = _error(413, "REQUEST_TOO_LARGE")
            else:
                body = self.rfile.read(length) if length else b""
                header_map = {key: value for key, value in self.headers.items()}
                response = server.application.dispatch(
                    method=self.command,
                    target=self.path,
                    headers=header_map,
                    body=body,
                )
        except (ValueError, OverflowError):
            response = _error(400, "INVALID_REQUEST")
        self.send_response(response.status)
        self.send_header("Content-Type", response.content_type)
        self.send_header("Content-Length", str(len(response.body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        for name, value in response.headers:
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(response.body)
            except (BrokenPipeError, ConnectionResetError):
                # A response drop after durable command commit is transport
                # uncertainty only; the store retains exact idempotency identity.
                pass

    do_GET = _handle
    do_POST = _handle


class AuthenticatedHostServer(ThreadingHTTPServer):
    """Concrete loopback HTTP / remote TLS server for the canonical API routes."""

    daemon_threads = True
    allow_reuse_address = False

    def __init__(
        self,
        server_address: tuple[str, int],
        application: AuthenticatedHostApplication,
        *,
        tls_context: ssl.SSLContext | None = None,
    ) -> None:
        if not isinstance(application, AuthenticatedHostApplication):
            raise TypeError("application must be AuthenticatedHostApplication")
        host, _ = server_address
        if tls_context is None and not _is_loopback_bind(host):
            raise ValueError("Plain HTTP host transport must bind to loopback only")
        if tls_context is not None and not isinstance(tls_context, ssl.SSLContext):
            raise TypeError("tls_context must be ssl.SSLContext or None")
        expected_scheme = "https" if tls_context is not None else "http"
        if urlsplit(application.public_origin).scheme != expected_scheme:
            raise ValueError("public_origin scheme does not match transport encryption")
        super().__init__(server_address, _HostRequestHandler)
        origin = urlsplit(application.public_origin)
        origin_port = origin.port or (443 if origin.scheme == "https" else 80)
        bound_port = int(self.server_address[1])
        if origin_port != bound_port:
            self.server_close()
            raise ValueError("public_origin port does not match the bound listener")
        self.application = application
        if tls_context is not None:
            self.socket = tls_context.wrap_socket(self.socket, server_side=True)
