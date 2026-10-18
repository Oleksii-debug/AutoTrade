"""Dependency-free, read-only WhiteBIT private balance collector.

Only the two documented account-balance routes can cross this boundary. The
default transport connects directly to the fixed WhiteBIT HTTPS origin, ignores
environment proxy configuration and never follows redirects. This module has
no order, transfer, withdrawal, leverage or credential-persistence API.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
import hmac
import http.client
import json
import ssl
from threading import Lock
import time
from typing import Mapping, Protocol


WHITEBIT_PRIVATE_HOST = "whitebit.com"
WHITEBIT_PRIVATE_PORT = 443
MAIN_BALANCE_PATH = "/api/v4/main-account/balance"
TRADING_BALANCE_PATH = "/api/v4/trade-account/balance"
READ_BALANCE_PATHS = frozenset({MAIN_BALANCE_PATH, TRADING_BALANCE_PATH})
MAX_REQUEST_BYTES = 1024
MAX_RESPONSE_BYTES = 1024 * 1024
NETWORK_TIMEOUT_SECONDS = 10.0


class PrivateReadError(RuntimeError):
    """Stable, secret-free failure. Raw remote or transport errors are hidden."""


@dataclass(frozen=True)
class PrivateHttpResponse:
    status_code: int
    content_type: str
    body: bytes = field(repr=False)


class PrivateReadTransport(Protocol):
    def post(
        self,
        *,
        path: str,
        headers: Mapping[str, str],
        body: bytes,
        timeout_seconds: float,
        maximum_response_bytes: int,
    ) -> PrivateHttpResponse: ...

    def close(self) -> None: ...


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> object:
    raise ValueError("non-finite JSON constant")


def _decode_json_object(raw: bytes, *, error_code: str) -> dict[str, object]:
    try:
        decoded = json.loads(
            raw,
            object_pairs_hook=_strict_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise PrivateReadError(error_code) from None
    if type(decoded) is not dict:
        raise PrivateReadError(error_code)
    return decoded


def _validate_request(path: str, headers: Mapping[str, str], body: bytes) -> None:
    """Fail closed before signing or I/O if the fixed read contract is widened."""
    if type(path) is not str or path not in READ_BALANCE_PATHS:
        raise PrivateReadError("WHITEBIT_READ_ROUTE_DENIED")
    if type(body) is not bytes or not body or len(body) > MAX_REQUEST_BYTES:
        raise PrivateReadError("WHITEBIT_READ_REQUEST_INVALID")
    if type(headers) is not dict or set(headers) != {
        "Content-Type", "X-TXC-APIKEY", "X-TXC-PAYLOAD", "X-TXC-SIGNATURE"
    }:
        raise PrivateReadError("WHITEBIT_READ_HEADERS_INVALID")
    if headers["Content-Type"] != "application/json":
        raise PrivateReadError("WHITEBIT_READ_HEADERS_INVALID")
    decoded = _decode_json_object(body, error_code="WHITEBIT_READ_REQUEST_INVALID")
    if (
        type(decoded) is not dict
        or set(decoded) != {"request", "nonce", "nonceWindow"}
        or decoded["request"] != path
        or type(decoded["nonce"]) is not int
        or not 0 < decoded["nonce"] < 2**63
        or decoded["nonceWindow"] is not True
    ):
        raise PrivateReadError("WHITEBIT_READ_REQUEST_INVALID")
    expected_payload = base64.b64encode(body).decode("ascii")
    if headers["X-TXC-PAYLOAD"] != expected_payload:
        raise PrivateReadError("WHITEBIT_READ_HEADERS_INVALID")
    for name, value in headers.items():
        if (
            type(name) is not str
            or type(value) is not str
            or not value
            or "\r" in value
            or "\n" in value
        ):
            raise PrivateReadError("WHITEBIT_READ_HEADERS_INVALID")


class StdlibWhitebitHttpsTransport:
    """One-shot direct TLS transport with a fixed authority and bounded read."""

    def __init__(self) -> None:
        self._closed = False
        self._lock = Lock()

    def post(
        self,
        *,
        path: str,
        headers: Mapping[str, str],
        body: bytes,
        timeout_seconds: float,
        maximum_response_bytes: int,
    ) -> PrivateHttpResponse:
        _validate_request(path, headers, body)
        if (
            type(timeout_seconds) not in (int, float)
            or not 0 < timeout_seconds <= NETWORK_TIMEOUT_SECONDS
            or type(maximum_response_bytes) is not int
            or not 0 < maximum_response_bytes <= MAX_RESPONSE_BYTES
        ):
            raise PrivateReadError("WHITEBIT_READ_LIMIT_INVALID")
        with self._lock:
            if self._closed:
                raise PrivateReadError("WHITEBIT_TRANSPORT_CLOSED")
        connection: http.client.HTTPSConnection | None = None
        try:
            context = ssl.create_default_context()
            connection = http.client.HTTPSConnection(
                WHITEBIT_PRIVATE_HOST,
                WHITEBIT_PRIVATE_PORT,
                timeout=timeout_seconds,
                context=context,
            )
            # Direct fixed-host connection: no proxy handler, URL parser, or
            # redirect handler exists in this path.
            connection.request("POST", path, body=body, headers=dict(headers))
            response = connection.getresponse()
            length = response.getheader("Content-Length")
            if length is not None:
                try:
                    if int(length) > maximum_response_bytes:
                        raise PrivateReadError("WHITEBIT_READ_RESPONSE_TOO_LARGE")
                except ValueError:
                    raise PrivateReadError("WHITEBIT_READ_LENGTH_INVALID") from None
            raw = response.read(maximum_response_bytes + 1)
            if len(raw) > maximum_response_bytes:
                raise PrivateReadError("WHITEBIT_READ_RESPONSE_TOO_LARGE")
            return PrivateHttpResponse(
                status_code=int(response.status),
                content_type=response.getheader("Content-Type", ""),
                body=raw,
            )
        except PrivateReadError:
            raise
        except (OSError, ssl.SSLError, http.client.HTTPException, ValueError):
            raise PrivateReadError("WHITEBIT_PRIVATE_READ_IO_FAILED") from None
        finally:
            if connection is not None:
                try:
                    connection.close()
                except OSError:
                    pass

    def close(self) -> None:
        with self._lock:
            self._closed = True


@dataclass(frozen=True)
class PrivateReadObservation:
    observed_at: datetime
    raw_sha256: str
    body: bytes = field(repr=False)
    path: str = MAIN_BALANCE_PATH
    verified_account_binding: bool = False
    trading_authority: str = "NONE"


_NONCE_LOCK = Lock()
_LAST_NONCE = 0


def _next_nonce() -> int:
    global _LAST_NONCE
    with _NONCE_LOCK:
        _LAST_NONCE = max(_LAST_NONCE + 1, time.time_ns() // 1_000_000)
        if not 0 < _LAST_NONCE < 2**63:
            raise PrivateReadError("WHITEBIT_NONCE_INVALID")
        return _LAST_NONCE


class WhitebitPrivateReader:
    """Read account balances with no route capable of changing account state.

    Network access is disabled by default. Tests inject a transport without
    enabling the network. Production explicitly sets ``enable_network=True``
    and receives the fixed direct-HTTPS transport above.
    """

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        *,
        transport: PrivateReadTransport | None = None,
        enable_network: bool = False,
    ) -> None:
        if type(enable_network) is not bool:
            raise PrivateReadError("WHITEBIT_READ_PERMISSION_INVALID")
        for value in (api_key, api_secret):
            if (
                type(value) is not str
                or not 1 <= len(value) <= 4096
                or any(ord(character) < 33 or ord(character) > 126 for character in value)
            ):
                raise PrivateReadError("WHITEBIT_CREDENTIAL_INPUT_INVALID")
        if transport is None and not enable_network:
            raise PrivateReadError("WHITEBIT_PRIVATE_NETWORK_NOT_ENABLED")
        if transport is not None and enable_network:
            raise PrivateReadError("WHITEBIT_TRANSPORT_AUTHORITY_AMBIGUOUS")
        self._transport = transport if transport is not None else StdlibWhitebitHttpsTransport()
        if not callable(getattr(self._transport, "post", None)) or not callable(
            getattr(self._transport, "close", None)
        ):
            raise PrivateReadError("WHITEBIT_TRANSPORT_INVALID")
        self._api_key = api_key
        self._api_secret = api_secret
        self._verified_account_binding = transport is None and enable_network
        self._closed = False
        self._lock = Lock()

    def main_balance(self) -> PrivateReadObservation:
        """Read the wallet/main account; this is not an order operation."""
        return self._read_balance(MAIN_BALANCE_PATH)

    def trading_balance(self) -> PrivateReadObservation:
        """Read spot available/frozen funds; this cannot place or cancel orders."""
        return self._read_balance(TRADING_BALANCE_PATH)

    def _read_balance(self, path: str) -> PrivateReadObservation:
        if path not in READ_BALANCE_PATHS:
            raise PrivateReadError("WHITEBIT_READ_ROUTE_DENIED")
        with self._lock:
            if self._closed:
                raise PrivateReadError("WHITEBIT_READER_CLOSED")
            nonce = _next_nonce()
            body = json.dumps(
                {"request": path, "nonce": nonce, "nonceWindow": True},
                separators=(",", ":"),
                ensure_ascii=True,
            ).encode("ascii")
            payload = base64.b64encode(body).decode("ascii")
            signature = hmac.new(
                self._api_secret.encode("ascii"), payload.encode("ascii"), "sha512"
            ).hexdigest()
            headers = {
                "Content-Type": "application/json",
                "X-TXC-APIKEY": self._api_key,
                "X-TXC-PAYLOAD": payload,
                "X-TXC-SIGNATURE": signature,
            }
            _validate_request(path, headers, body)
            try:
                response = self._transport.post(
                    path=path,
                    headers=headers,
                    body=body,
                    timeout_seconds=NETWORK_TIMEOUT_SECONDS,
                    maximum_response_bytes=MAX_RESPONSE_BYTES,
                )
            except PrivateReadError:
                raise
            except Exception:
                # Injected transports also cross a secret boundary. Never retain
                # or chain their exception text, which could contain headers.
                raise PrivateReadError("WHITEBIT_PRIVATE_READ_IO_FAILED") from None
            if type(response) is not PrivateHttpResponse:
                raise PrivateReadError("WHITEBIT_READ_RESPONSE_INVALID")
            if (
                type(response.status_code) is not int
                or not 100 <= response.status_code <= 599
                or type(response.content_type) is not str
                or len(response.content_type) > 256
                or "\r" in response.content_type
                or "\n" in response.content_type
                or type(response.body) is not bytes
            ):
                raise PrivateReadError("WHITEBIT_READ_RESPONSE_INVALID")
            if response.status_code != 200:
                raise PrivateReadError(f"WHITEBIT_READ_HTTP_{response.status_code}")
            media = response.content_type.split(";", 1)[0].strip().lower()
            if media != "application/json":
                raise PrivateReadError("WHITEBIT_READ_MEDIA_INVALID")
            if not response.body:
                raise PrivateReadError("WHITEBIT_READ_EMPTY_RESPONSE")
            if len(response.body) > MAX_RESPONSE_BYTES:
                raise PrivateReadError("WHITEBIT_READ_RESPONSE_TOO_LARGE")
            decoded = _decode_json_object(
                response.body, error_code="WHITEBIT_READ_JSON_INVALID"
            )
            if "code" in decoded and "message" in decoded:
                raise PrivateReadError("WHITEBIT_READ_REMOTE_REJECTED")
            return PrivateReadObservation(
                datetime.now(timezone.utc),
                sha256(response.body).hexdigest(),
                response.body,
                path=path,
                verified_account_binding=self._verified_account_binding,
            )

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._closed = True
                self._transport.close()

    def __enter__(self) -> "WhitebitPrivateReader":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
