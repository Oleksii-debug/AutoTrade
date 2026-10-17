"""Read-only WhiteBIT bridge using the exact upstream MIT HMAC implementation.

This is an optional collector component, not a credential store or trading sender.
It returns opaque response bytes for canonical normalization and durable evidence.
Private balances are NOT model input and must not be put in a public test report.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
import json
from threading import Lock
import time

import httpx

from .vendor.whitebit_hmac_transport import HmacTransport

MAIN_BALANCE_PATH = "/api/v4/main-account/balance"
MAX_RESPONSE_BYTES = 1024 * 1024


class PrivateReadError(RuntimeError):
    """Stable, secret-free failure. Never attach raw request/headers as context."""


class ReadOnlyGate(httpx.BaseTransport):
    """Reject all but the one source-verified read route, before signing and I/O."""

    def __init__(self, inner: httpx.BaseTransport):
        self._inner = inner

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        u = request.url
        if (request.method != "POST" or u.scheme != "https" or
                u.host != "whitebit.com" or u.port not in (None, 443) or
                u.username or u.password or u.query or u.fragment or
                u.raw_path != MAIN_BALANCE_PATH.encode("ascii")):
            raise PrivateReadError("WHITEBIT_READ_ROUTE_DENIED")
        raw = request.read()
        if len(raw) > 1024:
            raise PrivateReadError("WHITEBIT_READ_REQUEST_TOO_LARGE")
        try:
            body = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise PrivateReadError("WHITEBIT_READ_REQUEST_INVALID") from None
        if (type(body) is not dict or set(body) != {"request", "nonce", "nonceWindow"} or
                body["request"] != MAIN_BALANCE_PATH or type(body["nonce"]) is not int or
                not 0 < body["nonce"] < 2**63 or body["nonceWindow"] is not True):
            raise PrivateReadError("WHITEBIT_READ_REQUEST_INVALID")
        return self._inner.handle_request(request)


@dataclass(frozen=True)
class PrivateReadObservation:
    observed_at: datetime
    raw_sha256: str
    body: bytes = field(repr=False)
    path: str = MAIN_BALANCE_PATH
    verified_account_binding: bool = False
    trading_authority: str = "NONE"


class WhitebitPrivateReader:
    """One collector/session per credential; no shared key access by model workers.

    Default is network-disabled. A supplied transport is a test seam, not proof of
    account access. A production host must issue read permission and supply vault
    values before setting enable_network=True. Never grant order/withdrawal rights.
    """

    def __init__(self, api_key: str, api_secret: str, *,
                 transport: httpx.BaseTransport | None = None,
                 enable_network: bool = False):
        if type(enable_network) is not bool:
            raise PrivateReadError("WHITEBIT_READ_PERMISSION_INVALID")
        for value in (api_key, api_secret):
            if (type(value) is not str or not 1 <= len(value) <= 4096 or
                    any(ord(c) < 33 or ord(c) > 126 for c in value)):
                raise PrivateReadError("WHITEBIT_CREDENTIAL_INPUT_INVALID")
        if transport is None and not enable_network:
            raise PrivateReadError("WHITEBIT_PRIVATE_NETWORK_NOT_ENABLED")
        self._leaf = transport if transport is not None else httpx.HTTPTransport(retries=0)
        self._closed = False
        self._lock = Lock()
        self._nonce = 0
        # Upstream knows signing, NOT our route policy. Gate both sides of it.
        signer = HmacTransport(api_secret, wrapped=ReadOnlyGate(self._leaf))
        self._client = httpx.Client(
            transport=ReadOnlyGate(signer),
            headers={"X-TXC-APIKEY": api_key, "Content-Type": "application/json"},
            timeout=httpx.Timeout(10.0), follow_redirects=False, trust_env=False,
        )

    def main_balance(self) -> PrivateReadObservation:
        with self._lock:
            if self._closed:
                raise PrivateReadError("WHITEBIT_READER_CLOSED")
            self._nonce = max(self._nonce + 1, time.time_ns() // 1_000_000)
            payload = {"request": MAIN_BALANCE_PATH, "nonce": self._nonce, "nonceWindow": True}
            try:
                with self._client.stream("POST", "https://whitebit.com" + MAIN_BALANCE_PATH,
                                         json=payload) as response:
                    if response.status_code != 200:
                        raise PrivateReadError(f"WHITEBIT_READ_HTTP_{response.status_code}")
                    media = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                    if media != "application/json":
                        raise PrivateReadError("WHITEBIT_READ_MEDIA_INVALID")
                    raw = bytearray()
                    for chunk in response.iter_bytes(chunk_size=65536):
                        if len(raw) + len(chunk) > MAX_RESPONSE_BYTES:
                            raise PrivateReadError("WHITEBIT_READ_RESPONSE_TOO_LARGE")
                        raw.extend(chunk)
            except httpx.HTTPError:
                raise PrivateReadError("WHITEBIT_PRIVATE_READ_IO_FAILED") from None
            if not raw:
                raise PrivateReadError("WHITEBIT_READ_EMPTY_RESPONSE")
            body = bytes(raw)
            return PrivateReadObservation(datetime.now(timezone.utc), sha256(body).hexdigest(), body)

    def close(self):
        with self._lock:
            if not self._closed:
                self._closed = True
                try:
                    self._client.close()
                finally:
                    # The pinned upstream HmacTransport does not close its wrapped transport.
                    self._leaf.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
