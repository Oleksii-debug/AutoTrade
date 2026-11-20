"""Freeze completed-day TON signals during the hour before the modeled 01:00 UTC open."""

from datetime import datetime, UTC
from hashlib import sha256
import json
from channel import signal
from source import CODE, ROOT, WhiteBitPublicClient, _write_immutable


def freeze(now=None):
    now = now or datetime.now(UTC)
    if now.hour != 0:
        return
    folder = ROOT / "forward"
    folder.mkdir(exist_ok=True, parents=True)
    name = folder / f"{now.date()}.json"
    if name.exists():
        return
    midnight = int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    start = midnight - 43 * 86400
    batch = WhiteBitPublicClient().klines(
        "GRAM_USDT", interval="1d", start=start, end=midnight, limit=43, training=True
    )
    candles = batch.candles
    if [int(c.open_time.timestamp()) for c in candles] != list(
        range(start, midnight, 86400)
    ):
        raise ValueError("Incomplete TON daily history")
    closes = [float(c.close) for c in candles]
    setup = signal(closes)
    raw_hash = sha256(batch.raw).hexdigest()
    _write_immutable(folder / (raw_hash + ".raw"), batch.raw)
    at = datetime.now(UTC)
    if at.date() != now.date() or at.hour != 0:
        return
    doc = dict(
        market="GRAM_USDT",
        recorded=at.isoformat(),
        entry_epoch=midnight + 3600,
        source=batch.source_uri,
        raw_sha256=raw_hash,
        close=closes[-1],
        drawdown=1 - closes[-1] / max(closes[-15:-1]),
        channel=setup,
        channel_candidate=bool(setup and setup["ready"]),
        channel_sha=sha256((CODE / "range-protocol.json").read_bytes()).hexdigest(),
        dip_sha=sha256((CODE / "protocol.json").read_bytes()).hexdigest(),
    )
    _write_immutable(name, json.dumps(doc, sort_keys=True, indent=2).encode())


if __name__ == "__main__":
    freeze()
