"""Save an executable-depth reference when the daily decision clock arrives."""

from datetime import datetime, UTC
from decimal import Decimal as D
from hashlib import sha256
import json
from source import ROOT, WhiteBitPublicClient, _write_immutable


def capture():
    now = datetime.now(UTC)
    folder = ROOT / "forward"
    signal = folder / f"{now.date()}.json"
    output = folder / f"{now.date()}.book.json"
    if not signal.exists() or output.exists():
        return
    doc = json.loads(signal.read_bytes())
    if not doc["entry_epoch"] <= now.timestamp() < doc["entry_epoch"] + 120:
        return
    book = WhiteBitPublicClient().orderbook("GRAM_USDT")
    at = book.received_at
    book.assert_fresh(at)
    if not doc["entry_epoch"] <= at.timestamp() < doc["entry_epoch"] + 120:
        return
    left = D("1000")
    units = D(0)
    for p, q in book.asks:
        spend = min(left, D(p) * D(q))
        units += spend / D(p)
        left -= spend
        if left == 0:
            break
    sha = sha256(book.raw).hexdigest()
    _write_immutable(folder / (sha + ".raw"), book.raw)
    _write_immutable(
        output,
        json.dumps(
            dict(
                recorded=at.isoformat(),
                decision_sha=sha256(signal.read_bytes()).hexdigest(),
                book_sha=sha,
                bid=book.bids[0],
                ask=book.asks[0],
                quote_notional="1000",
                full_depth=left == 0,
                ask_vwap=str(D(1000) / units) if left == 0 else None,
            ),
            sort_keys=True,
            indent=2,
        ).encode(),
    )


if __name__ == "__main__":
    capture()
