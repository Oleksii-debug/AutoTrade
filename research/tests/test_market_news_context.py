import json
from datetime import UTC, datetime, timedelta
from threading import Barrier, Lock
from time import sleep

from research.autotrade_research.integrations.market_news_context import (
    FED_MONETARY, SOLANA_INCIDENTS, SOLANA_STATUS, WHITEBIT_STATUS,
    MarketContextError, collect_market_context,
)


NOW = datetime(2026, 11, 12, 12, 0, tzinfo=UTC)


def _feed(published: datetime) -> bytes:
    stamp = published.strftime("%a, %d %b %Y %H:%M:%S GMT")
    return (f"<?xml version='1.0'?><rss><channel><item>"
        f"<title>Federal Reserve issues FOMC statement</title>"
        f"<link>https://www.federalreserve.gov/example.htm</link>"
        f"<pubDate>{stamp}</pubDate></item></channel></rss>").encode()


def _fetch(*, solana="none", incidents=None, whitebit=1,
           published=NOW - timedelta(days=1)):
    values = {
        SOLANA_STATUS: json.dumps({"status": {"indicator": solana,
            "description": "state"}}).encode(),
        SOLANA_INCIDENTS: json.dumps({"incidents": incidents or []}).encode(),
        WHITEBIT_STATUS: json.dumps({"status": whitebit}).encode(),
        FED_MONETARY: _feed(published),
    }
    return values.__getitem__


def test_official_context_allows_only_when_every_gate_is_clear():
    value = collect_market_context(now=NOW, fetch=_fetch())
    assert value["decision"] == "ALLOW"
    assert value["reasons"] == []
    assert value["news_can_create_trade"] is False
    assert value["trading_authority"] is False


def test_official_incident_and_fresh_macro_release_block_new_exposure():
    value = collect_market_context(now=NOW, fetch=_fetch(solana="major",
        incidents=[{"id": "incident"}], published=NOW - timedelta(minutes=5)))
    assert value["decision"] == "BLOCK"
    assert "SOLANA_OFFICIAL_INCIDENT" in value["reasons"]
    assert "FRESH_FED_MONETARY_RELEASE" in value["reasons"]


def test_unavailable_source_fails_closed_without_inventing_sentiment():
    def unavailable(url):
        if url == WHITEBIT_STATUS:
            raise OSError("offline")
        return _fetch()(url)
    value = collect_market_context(now=NOW, fetch=unavailable)
    assert value["decision"] == "BLOCK"
    assert value["details"] == {}


def test_independent_feeds_overlap_but_failures_keep_source_order():
    all_started = Barrier(4)
    lock = Lock()
    arrivals = []
    delays = {SOLANA_STATUS: .04, SOLANA_INCIDENTS: .03,
              WHITEBIT_STATUS: .02, FED_MONETARY: .01}

    def overlapping_fetch(url):
        with lock:
            arrivals.append(url)
        all_started.wait(timeout=1)
        sleep(delays[url])
        if url in (SOLANA_STATUS, SOLANA_INCIDENTS):
            raise MarketContextError("temporary failure")
        return _fetch()(url)

    value = collect_market_context(now=NOW, fetch=overlapping_fetch)

    assert set(arrivals) == {SOLANA_STATUS, SOLANA_INCIDENTS,
                             WHITEBIT_STATUS, FED_MONETARY}
    assert value["reasons"] == ["SOLANA_STATUS_MARKETCONTEXTERROR",
                                "SOLANA_INCIDENTS_MARKETCONTEXTERROR"]
    assert value["observed_at"] == NOW.isoformat()
