"""Bounded official-source context gate for SOL/WhiteBIT research.

News is context, never an order signal.  The gate only vetoes new exposure when
an official source reports an infrastructure incident, WhiteBIT maintenance,
or a just-released Federal Reserve monetary-policy announcement.  Every fetch
is pinned to an allow-listed HTTPS endpoint and timestamped when received so a
later replay cannot leak future information into an earlier decision.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from hashlib import sha256
import json
import ssl
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, HTTPSHandler, ProxyHandler, Request, build_opener
from xml.etree import ElementTree


SCHEMA = "autotrade-official-market-context-v1"
SOLANA_STATUS = "https://status.solana.com/api/v2/status.json"
SOLANA_INCIDENTS = "https://status.solana.com/api/v2/incidents/unresolved.json"
WHITEBIT_STATUS = "https://whitebit.com/api/v4/public/platform/status"
FED_MONETARY = "https://www.federalreserve.gov/feeds/press_monetary.xml"
ALLOWED_URLS = frozenset((SOLANA_STATUS, SOLANA_INCIDENTS, WHITEBIT_STATUS, FED_MONETARY))
OFFICIAL_SOURCES = (("solana_status", SOLANA_STATUS),
                    ("solana_incidents", SOLANA_INCIDENTS),
                    ("whitebit_status", WHITEBIT_STATUS),
                    ("fed_monetary", FED_MONETARY))
MAX_BYTES = 262_144


class MarketContextError(ValueError):
    pass


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise MarketContextError("OFFICIAL_SOURCE_REDIRECT_REJECTED")


def _read(url: str) -> bytes:
    if url not in ALLOWED_URLS:
        raise MarketContextError("SOURCE_NOT_ALLOWLISTED")
    opener = build_opener(ProxyHandler({}), _NoRedirect(),
                          HTTPSHandler(context=ssl.create_default_context()))
    request = Request(url, method="GET", headers={
        "Accept": "application/json, application/rss+xml, text/xml",
        "User-Agent": "AutoTrade-OfficialContext/1",
    })
    try:
        with opener.open(request, timeout=10) as response:
            if response.status != 200:
                raise MarketContextError("OFFICIAL_SOURCE_NON_SUCCESS")
            payload = response.read(MAX_BYTES + 1)
    except HTTPError as exc:
        exc.close()
        raise MarketContextError("OFFICIAL_SOURCE_HTTP_ERROR") from None
    except (URLError, OSError, TimeoutError):
        raise MarketContextError("OFFICIAL_SOURCE_UNAVAILABLE") from None
    if not 0 < len(payload) <= MAX_BYTES:
        raise MarketContextError("OFFICIAL_SOURCE_SIZE_LIMIT")
    return payload


def _json(payload: bytes) -> dict:
    try:
        value = json.loads(payload)
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        raise MarketContextError("INVALID_OFFICIAL_JSON") from None
    if type(value) is not dict:
        raise MarketContextError("INVALID_OFFICIAL_JSON")
    return value


def _fed_items(payload: bytes, *, limit: int = 5) -> list[dict]:
    try:
        root = ElementTree.fromstring(payload.lstrip(b"\xef\xbb\xbf"))
    except ElementTree.ParseError:
        raise MarketContextError("INVALID_FED_RSS") from None
    result = []
    for item in root.findall("./channel/item")[:limit]:
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        published = (item.findtext("pubDate") or "").strip()
        if not title or not link.startswith("https://www.federalreserve.gov/"):
            raise MarketContextError("INVALID_FED_RSS_ITEM")
        try:
            at = parsedate_to_datetime(published).astimezone(timezone.utc)
        except (TypeError, ValueError, OverflowError):
            raise MarketContextError("INVALID_FED_RSS_TIME") from None
        result.append({"title": title[:300], "url": link[:1000],
                       "published_at": at.isoformat()})
    if not result:
        raise MarketContextError("EMPTY_FED_RSS")
    return result


def collect_market_context(*, now: datetime | None = None,
                           fetch: Callable[[str], bytes] = _read,
                           macro_blackout_minutes: int = 90) -> dict:
    """Collect one point-in-time context record and return ALLOW or BLOCK."""
    if now is not None and (type(now) is not datetime or now.tzinfo != timezone.utc):
        raise MarketContextError("UTC_TIMESTAMP_REQUIRED")
    if not 15 <= macro_blackout_minutes <= 360:
        raise MarketContextError("INVALID_MACRO_BLACKOUT")
    payloads = {}
    failures = []
    # These feeds are independent snapshots.  Start them together so one slow
    # authority cannot consume the timeout budget of every source that follows.
    # Results are still read in the declared order, preserving deterministic
    # failure ordering and an observation time after every fetch has completed.
    with ThreadPoolExecutor(max_workers=len(OFFICIAL_SOURCES),
                            thread_name_prefix="official-context") as executor:
        pending = [(name, executor.submit(fetch, url))
                   for name, url in OFFICIAL_SOURCES]
        for name, future in pending:
            try:
                payloads[name] = future.result()
            except (MarketContextError, OSError, TimeoutError, ValueError, TypeError) as exc:
                failures.append(f"{name.upper()}_{type(exc).__name__.upper()}")
    observed = now or datetime.now(timezone.utc)
    reasons = list(failures)
    details: dict[str, object] = {}
    if not failures:
        solana = _json(payloads["solana_status"])
        incidents = _json(payloads["solana_incidents"])
        whitebit = _json(payloads["whitebit_status"])
        status = solana.get("status")
        unresolved = incidents.get("incidents")
        if (type(status) is not dict or type(status.get("indicator")) is not str
                or type(unresolved) is not list or type(whitebit.get("status")) is not int):
            raise MarketContextError("INVALID_OFFICIAL_STATUS_SCHEMA")
        details["solana"] = {"indicator": status["indicator"],
            "description": str(status.get("description", ""))[:300],
            "unresolved_incident_count": len(unresolved)}
        details["whitebit"] = {"platform_status": whitebit["status"]}
        fed = _fed_items(payloads["fed_monetary"])
        details["federal_reserve"] = {"latest_items": fed}
        if status["indicator"] != "none" or unresolved:
            reasons.append("SOLANA_OFFICIAL_INCIDENT")
        if whitebit["status"] != 1:
            reasons.append("WHITEBIT_PLATFORM_NOT_OPERATIONAL")
        cutoff = observed - timedelta(minutes=macro_blackout_minutes)
        if any(cutoff <= datetime.fromisoformat(item["published_at"]) <= observed
               for item in fed):
            reasons.append("FRESH_FED_MONETARY_RELEASE")
    record = {"schema": SCHEMA, "observed_at": observed.isoformat(),
        "decision": "BLOCK" if reasons else "ALLOW", "reasons": reasons,
        "details": details, "official_sources": sorted(ALLOWED_URLS),
        "news_can_create_trade": False, "network_orders": False,
        "trading_authority": False}
    record["evidence_sha256"] = sha256(json.dumps(record, sort_keys=True,
        separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    return record
