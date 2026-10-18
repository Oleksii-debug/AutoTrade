"""WhiteBIT production-public data; NO account, credentials or order sender.

Public market data and execution environments are deliberately separate.
The existing AutoTrade market normalizer, evidence store and simulated provider
remain authoritative downstream. Network permission belongs to the collector,
not the model worker. Sources: docs.whitebit.com/api-reference/market-data/.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
from hashlib import sha256
import json
import re
import ssl
import threading
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, HTTPSHandler, ProxyHandler, Request, build_opener

BASE = "https://whitebit.com/api/v4/public/"
WS = "wss://api.whitebit.com/ws"
ADAPTER_VERSION = "whitebit-public-rest-v1"
MAX_BYTES = 1_048_576
MAX_DEPTH = 16
SYMBOL = re.compile(r"[A-Z0-9]{1,20}_[A-Z0-9]{1,20}\Z")
NUMBER = re.compile(r"(?:0|[1-9][0-9]{0,35})(?:\.[0-9]{1,24})?\Z")


class PublicDataError(ValueError):
    """A bounded, non-sensitive error safe for an accessible status surface."""


def _symbol(value: str) -> str:
    if type(value) is not str or SYMBOL.fullmatch(value) is None:
        raise PublicDataError("INVALID_MARKET")
    return value


def _utc(value: datetime) -> datetime:
    if type(value) is not datetime or type(value.tzinfo) is not timezone:
        raise PublicDataError("UTC_TIMESTAMP_REQUIRED")
    return value.astimezone(timezone.utc)


def _decimal(value: Any, *, positive: bool = False) -> Decimal:
    # Provider decimals are strings. Reject float/bool/exponents and oversized input.
    if type(value) is not str or NUMBER.fullmatch(value) is None:
        raise PublicDataError("INVALID_DECIMAL")
    try:
        result = Decimal(value)
    except InvalidOperation:
        raise PublicDataError("INVALID_DECIMAL") from None
    if not result.is_finite() or (positive and result <= 0):
        raise PublicDataError("NON_POSITIVE_DECIMAL")
    return result


def decode_public_json(raw: bytes) -> Any:
    if type(raw) is not bytes or not 0 < len(raw) <= MAX_BYTES:
        raise PublicDataError("RESPONSE_SIZE_LIMIT")
    # Pre-scan before json.loads to bound nesting, including hostile JSON.
    depth = 0
    quoted = escaped = False
    for char in raw:
        if quoted:
            if escaped:
                escaped = False
            elif char == 92:
                escaped = True
            elif char == 34:
                quoted = False
        elif char == 34:
            quoted = True
        elif char in (91, 123):
            depth += 1
            if depth > MAX_DEPTH:
                raise PublicDataError("JSON_DEPTH_LIMIT")
        elif char in (93, 125):
            depth -= 1
            if depth < 0:
                raise PublicDataError("INVALID_JSON")

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise PublicDataError("DUPLICATE_JSON_KEY")
            result[key] = value
        return result

    def integer(token):
        if len(token) > 16:
            raise PublicDataError("INTEGER_SIZE_LIMIT")
        return int(token)

    def number(token):
        if len(token) > 64:
            raise PublicDataError("DECIMAL_SIZE_LIMIT")
        result = Decimal(token)
        if not result.is_finite() or abs(result.adjusted()) > 48:
            raise PublicDataError("INVALID_JSON_NUMBER")
        return result

    def constant(_):
        raise PublicDataError("NON_FINITE_JSON")

    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=pairs,
                          parse_int=integer, parse_float=number, parse_constant=constant)
    except (UnicodeError, json.JSONDecodeError, InvalidOperation, RecursionError):
        raise PublicDataError("INVALID_JSON") from None


@dataclass(frozen=True)
class PublicBook:
    market: str
    source_time: datetime
    received_at: datetime
    bids: tuple[tuple[str, str], ...]
    asks: tuple[tuple[str, str], ...]
    raw: bytes = field(repr=False)
    source_uri: str

    @property
    def digest(self) -> str:
        return "sha256:" + sha256(self.raw).hexdigest()

    def assert_fresh(self, now: datetime, maximum_age_seconds: int = 10) -> None:
        now = _utc(now)
        if type(maximum_age_seconds) is not int or not 1 <= maximum_age_seconds <= 300:
            raise PublicDataError("INVALID_FRESHNESS_LIMIT")
        if now < self.received_at or now < self.source_time:
            raise PublicDataError("CLOCK_MOVED_BACKWARD")
        if (now - self.source_time).total_seconds() > maximum_age_seconds:
            raise PublicDataError("STALE_MARKET_DATA")

    def accessible_status(self, *, now: datetime) -> dict[str, Any]:
        self.assert_fresh(now)
        with localcontext() as context:
            context.prec = 80
            spread = Decimal(self.asks[0][0]) - Decimal(self.bids[0][0])
        return {
            "provider": "WhiteBIT", "market": self.market,
            "market_environment": "PRODUCTION_PUBLIC",
            "execution_environment": "SIMULATION_ONLY",
            "bid": self.bids[0][0], "ask": self.asks[0][0],
            "bid_amount": self.bids[0][1], "ask_amount": self.asks[0][1],
            "spread": format(spread, "f"),
            "source_time": self.source_time.isoformat(),
            "received_at": self.received_at.isoformat(), "source_digest": self.digest,
            "private_account_connected": False, "real_orders_enabled": False,
            "liquidity_limitations": "PUBLIC_BOOK_EXCLUDES_RPI; NO_FILL_GUARANTEE",
        }

    def to_raw_market_update(self, evidence_ref: dict[str, Any], *, now: datetime):
        """Use the EXISTING normalizer contract after actual raw-artifact persistence.

        A matching hash is not a substitute for storage: the calling collector
        must first commit raw bytes through AutoTrade's existing ArtifactStore.
        No synthetic receipt is created here.
        """
        self.assert_fresh(now)
        if (type(evidence_ref) is not dict or evidence_ref.get("sha256") != self.digest
                or evidence_ref.get("source_uri") != self.source_uri):
            raise PublicDataError("RAW_EVIDENCE_BINDING_MISMATCH")
        observed = evidence_ref.get("observed_at")
        expected = self.received_at.isoformat().replace("+00:00", "Z")
        if observed != expected:
            raise PublicDataError("RAW_EVIDENCE_TIME_MISMATCH")
        from mvp.autotrade_mvp.market_data import RawMarketUpdate
        return RawMarketUpdate(
            provider_id="WHITEBIT", venue_id="WHITEBIT_SPOT",
            provider_symbol=self.market, adapter_version=ADAPTER_VERSION,
            kind="BOOK_SNAPSHOT", source_event_at=self.source_time,
            available_at=self.received_at, ingested_at=_utc(now),
            availability_basis="OBSERVED_PUBLIC_REST_RESPONSE", revision=0,
            payload={"bids": [list(x) for x in self.bids], "asks": [list(x) for x in self.asks]},
            raw_evidence_ref=evidence_ref,
        )


def parse_orderbook(raw: bytes, market: str, received_at: datetime) -> PublicBook:
    market, received_at = _symbol(market), _utc(received_at)
    data = decode_public_json(raw)
    if type(data) is not dict or data.get("ticker_id") != market:
        raise PublicDataError("MARKET_ID_MISMATCH")
    seconds = data.get("timestamp")
    if type(seconds) is not int or not 1_000_000_000 <= seconds <= 9_999_999_999:
        raise PublicDataError("EXPECTED_EPOCH_SECONDS")
    try:
        source = datetime.fromtimestamp(seconds, timezone.utc)
    except (OSError, ValueError, OverflowError):
        raise PublicDataError("INVALID_SOURCE_TIME") from None
    if source > received_at:
        raise PublicDataError("FUTURE_MARKET_TIMESTAMP")

    def levels(name: str, descending: bool):
        rows = data.get(name)
        if type(rows) is not list or not 1 <= len(rows) <= 100:
            raise PublicDataError("EMPTY_OR_OVERSIZED_ORDERBOOK")
        result, previous = [], None
        for row in rows:
            if type(row) is not list or len(row) != 2:
                raise PublicDataError("INVALID_PRICE_LEVEL")
            price, amount = _decimal(row[0], positive=True), _decimal(row[1], positive=True)
            if previous is not None and ((descending and price >= previous) or
                                         (not descending and price <= previous)):
                raise PublicDataError("UNSORTED_OR_DUPLICATE_PRICE_LEVEL")
            previous = price
            result.append((format(price, "f"), format(amount, "f")))
        return tuple(result)

    bids, asks = levels("bids", True), levels("asks", False)
    if Decimal(bids[0][0]) >= Decimal(asks[0][0]):
        raise PublicDataError("CROSSED_OR_LOCKED_ORDERBOOK")
    result = PublicBook(market, source, received_at, bids, asks, raw,
                        BASE + "orderbook/" + market + "?limit=100&level=0")
    result.assert_fresh(received_at)
    return result


def parse_spot_markets(raw: bytes) -> tuple[dict[str, Any], ...]:
    data = decode_public_json(raw)
    if type(data) is not list or not 1 <= len(data) <= 10000:
        raise PublicDataError("INVALID_MARKET_LIST")
    result, seen = [], set()
    for row in data:
        if type(row) is not dict:
            raise PublicDataError("INVALID_MARKET_RECORD")
        # This first integration is SPOT only. Derivatives need lifecycle qualification.
        if row.get("type") != "spot":
            continue
        market = _symbol(row.get("name"))
        if market in seen:
            raise PublicDataError("DUPLICATE_MARKET")
        seen.add(market)
        base, quote = market.split("_")
        if row.get("stock") != base or row.get("money") != quote:
            raise PublicDataError("ASSET_ID_MISMATCH")
        if type(row.get("tradesEnabled")) is not bool:
            raise PublicDataError("UNKNOWN_TRADING_STATUS")
        if not row["tradesEnabled"] or row.get("delistedAt") is not None:
            continue
        quantity_precision, quote_precision = row.get("stockPrec"), row.get("moneyPrec")
        if any(type(x) is not str or not re.fullmatch(r"[0-9]{1,2}", x) or int(x) > 24
               for x in (quantity_precision, quote_precision)):
            raise PublicDataError("INVALID_PRECISION")
        result.append({
            "market": market, "base_asset": base, "quote_asset": quote,
            "minimum_quantity": format(_decimal(row.get("minAmount"), positive=True), "f"),
            "minimum_notional": format(_decimal(row.get("minTotal"), positive=True), "f"),
            "quantity_precision": int(quantity_precision), "quote_precision": int(quote_precision),
            # Quote precision is NOT assumed to be a independently qualified price tick.
            "price_tick": None, "account_permission": "UNKNOWN",
            "market_environment": "PRODUCTION_PUBLIC", "execution_environment": "SIMULATION_ONLY",
        })
    return tuple(result)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise PublicDataError("PUBLIC_API_REDIRECT_REJECTED")


class WhiteBitPublicClient:
    """Bounded GET-only collector. No base URL override, credentials or retries."""
    def __init__(self):
        self._opener = build_opener(ProxyHandler({}), _NoRedirect(),
                                    HTTPSHandler(context=ssl.create_default_context()))
        self._lock = threading.Lock()
        self._next_request = 0.0

    def _read(self, endpoint: str) -> tuple[bytes, datetime]:
        if endpoint != "markets" and not re.fullmatch(
                r"orderbook/[A-Z0-9]{1,20}_[A-Z0-9]{1,20}\?limit=100&level=0", endpoint):
            raise PublicDataError("PUBLIC_ENDPOINT_NOT_ALLOWED")
        with self._lock:
            delay = self._next_request - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            self._next_request = time.monotonic() + 1.0
            request = Request(BASE + endpoint, method="GET",
                              headers={"Accept": "application/json", "User-Agent": "AutoTrade-PublicReader/1"})
            try:
                with self._opener.open(request, timeout=10) as response:
                    if response.status != 200:
                        raise PublicDataError("PUBLIC_API_NON_SUCCESS")
                    if response.headers.get_content_type() != "application/json":
                        raise PublicDataError("PUBLIC_API_CONTENT_TYPE")
                    raw = response.read(MAX_BYTES + 1)
            except HTTPError as error:
                code = error.code
                error.close()
                raise PublicDataError("PUBLIC_API_RATE_LIMIT" if code == 429 else "PUBLIC_API_HTTP_ERROR") from None
            except (URLError, OSError, TimeoutError):
                raise PublicDataError("PUBLIC_API_NETWORK_UNAVAILABLE") from None
            if not 0 < len(raw) <= MAX_BYTES:
                raise PublicDataError("RESPONSE_SIZE_LIMIT")
            return raw, datetime.now(timezone.utc)

    def markets(self) -> tuple[dict[str, Any], ...]:
        raw, _ = self._read("markets")
        return parse_spot_markets(raw)

    def orderbook(self, market: str) -> PublicBook:
        market = _symbol(market)
        # A caller cannot route _PERP or unknown instruments into the spot connector.
        available = {item["market"] for item in self.markets()}
        if market not in available:
            raise PublicDataError("SPOT_MARKET_NOT_AVAILABLE")
        raw, received = self._read("orderbook/" + market + "?limit=100&level=0")
        return parse_orderbook(raw, market, received)
