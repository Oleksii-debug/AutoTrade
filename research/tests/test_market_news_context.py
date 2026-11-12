import json
from datetime import UTC, datetime, timedelta

from research.autotrade_research.integrations.market_news_context import (
    FED_MONETARY, SOLANA_INCIDENTS, SOLANA_STATUS, WHITEBIT_STATUS,
    collect_market_context,
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
