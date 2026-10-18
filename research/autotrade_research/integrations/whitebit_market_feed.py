"""Continuously deliver evidence-bound WhiteBIT public books to the market pipeline.

The consumer receives the existing RawMarketUpdate contract. It owns admission
to the instrument registry and downstream simulation; this collector cannot
submit an order or make an account request. Delivery is sequential and a
consumer failure stops the loop rather than silently dropping a market event.
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from time import monotonic
from threading import Event
from uuid import uuid4

from ..artifacts.store import ArtifactIntegrityError, ArtifactStore
from .whitebit_public import PublicBook, PublicDataError, WhiteBitPublicClient, _symbol


class WhiteBitMarketFeed:
    """Poll public spot books, persist the original bytes, then deliver updates."""

    def __init__(self, *, client: WhiteBitPublicClient, artifacts: ArtifactStore,
                 market: str):
        if type(client) is not WhiteBitPublicClient:
            raise TypeError("client must be WhiteBitPublicClient")
        if type(artifacts) is not ArtifactStore:
            raise TypeError("artifacts must be canonical ArtifactStore")
        self.client = client
        self.artifacts = artifacts
        self.market = _symbol(market)

    def poll_once(self):
        """Return one actual RawMarketUpdate after committing authenticated bytes."""
        book = self.client.orderbook(self.market)
        if type(book) is not PublicBook or book.market != self.market:
            raise PublicDataError("PUBLIC_BOOK_MARKET_MISMATCH")
        book.assert_fresh(datetime.now(timezone.utc))
        artifact_id = str(uuid4())
        manifest = self.artifacts.publish_bytes(
            artifact_id=artifact_id,
            data=book.raw,
            media_type="application/json",
            rights={"storage": True, "export": False,
                    "rights_id": "whitebit-public-book-internal-v1"},
            source_refs=[book.source_uri],
            metadata={"provider": "WHITEBIT", "market": book.market,
                      "source_time": book.source_time.isoformat(),
                      "observed_at": book.received_at.isoformat().replace("+00:00", "Z")},
        )
        # A committed hash is a content check, not a substitute for loading
        # the object through the canonical store's integrity checks.
        if (manifest["sha256"] != book.digest or
                ArtifactStore.read_bytes(self.artifacts, artifact_id) != book.raw):
            raise ArtifactIntegrityError("WhiteBIT raw book evidence mismatch")
        evidence = {
            "artifact_id": artifact_id,
            "sha256": manifest["sha256"],
            "source_uri": book.source_uri,
            "observed_at": book.received_at.isoformat().replace("+00:00", "Z"),
            "rights_id": "whitebit-public-book-internal-v1",
        }
        return book.to_raw_market_update(evidence, now=datetime.now(timezone.utc))

    def run(self, consumer: Callable, stop: Event, *, interval_seconds: float = 5.0,
            max_backoff_seconds: float = 60.0,
            on_public_error: Callable[[str], None] | None = None) -> None:
        """Poll until stopped; retry public read failures with a bounded backoff.

        No trading callback, persistence error, or downstream exception is
        retried: the supervisor must explicitly recover their durable state.
        """
        if not callable(consumer) or type(stop) is not Event:
            raise TypeError("consumer and threading.Event are required")
        if (type(interval_seconds) not in (int, float) or
                type(max_backoff_seconds) not in (int, float) or
                not 2 <= interval_seconds <= max_backoff_seconds <= 3600):
            raise ValueError("invalid market polling interval/backoff")
        if on_public_error is not None and not callable(on_public_error):
            raise TypeError("on_public_error must be callable")
        backoff = interval_seconds
        while not stop.is_set():
            started = monotonic()
            try:
                update = self.poll_once()
            except PublicDataError as error:
                if on_public_error is not None:
                    on_public_error(str(error))
                if stop.wait(backoff):
                    return
                backoff = min(max_backoff_seconds, backoff * 2)
                continue
            backoff = interval_seconds
            if stop.is_set():
                return
            consumer(update)
            stop.wait(max(0, interval_seconds - (monotonic() - started)))
