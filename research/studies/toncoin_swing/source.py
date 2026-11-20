"""Archive public Toncoin spot candles with their original exchange response and SHA."""

from datetime import datetime, UTC
from hashlib import sha256
import json
import os
from pathlib import Path
import sys
from urllib.parse import urlencode
from urllib.request import urlopen
from time import sleep

CODE = Path(__file__).resolve().parent
checkout = next(
    (p for p in CODE.parents if (p / "tools/whitebit_historical_klines.py").is_file()),
    None,
)
ROOT = Path(
    os.environ.get(
        "AUTOTRADE_TON_STATE_DIR",
        CODE
        if checkout is None
        else Path.home() / "AutoTradeData" / "toncoin-swing-20261120",
    )
).resolve()
sys.path.insert(
    0,
    str(
        Path(
            os.environ.get(
                "AUTOTRADE_CHECKOUT",
                checkout or CODE.parent / "20261120-weekly-integration",
            )
        ).resolve()
    ),
)
from tools.whitebit_historical_klines import WhiteBitPublicClient, _write_immutable


def stamp(s):
    return int(datetime.fromisoformat(s).replace(tzinfo=UTC).timestamp())


def load_venue(venue, market=None):
    start = stamp("2023-10-01" if venue == "Bybit" or market else "2026-06-24")
    end = stamp("2026-11-16T01:00")
    folder = ROOT / "raw" / venue
    if market:
        assert venue == "WhiteBIT" and market in ("BTC_USDT", "ETH_USDT", "LTC_USDT")
        folder = folder / market
    folder.mkdir(exist_ok=True, parents=True)
    client = WhiteBitPublicClient()
    data = {}
    manifest = []
    for first in range(start, end + 1, 1000 * 3600):
        last = min(end, first + 999 * 3600)
        path = folder / f"{first}.json"
        if venue == "Bybit":
            url = "https://api.bybit.com/v5/market/kline?" + urlencode(
                dict(
                    category="spot",
                    symbol="GRAMUSDT",
                    interval=60,
                    start=first * 1000,
                    end=last * 1000,
                    limit=1000,
                )
            )
            if not path.exists():
                with urlopen(url, timeout=30) as resp:
                    _write_immutable(path, resp.read(2_000_000))
                sleep(0.12)
            payload = json.loads(path.read_bytes())
            if (
                payload["retCode"] != 0
                or payload["result"]["symbol"] != "GRAMUSDT"
                or payload["result"]["category"] != "spot"
            ):
                raise ValueError("Invalid Bybit spot response")
            rows = [
                (int(r[0]) // 1000, *r[1:6])
                for r in payload["result"]["list"]
                if first * 1000 <= int(r[0]) <= last * 1000
            ]
        else:
            opts = dict(
                interval="1h",
                start=first,
                end=last + 3600,
                limit=(last - first) // 3600 + 1,
            )
            if not path.exists():
                response = client.klines(market or "GRAM_USDT", **opts, training=True)
                _write_immutable(path, response.raw)
                _write_immutable(path.with_suffix(".url"), response.source_uri.encode())
            url = path.with_suffix(".url").read_text()
            raw = json.loads(path.read_bytes())
            rows = [(int(r[0]), r[1], r[3], r[4], r[2], r[5]) for r in raw["result"]]
        for t, o, h, l, c, v in rows:
            bar = tuple(map(float, (o, h, l, c, v)))
            import math

            if (
                not all(map(math.isfinite, bar))
                or t % 3600
                or not first <= t <= last
                or t in data
                or not 0
                < bar[2]
                <= min(bar[0], bar[3])
                <= max(bar[0], bar[3])
                <= bar[1]
                or bar[4] < 0
            ):
                raise ValueError(f"Invalid {venue} candle {t}")
            data[t] = bar
        manifest.append(
            dict(
                source=url,
                file=path.name,
                sha256=sha256(path.read_bytes()).hexdigest(),
                rows=len(rows),
            )
        )
    _write_immutable(
        folder / "manifest.json", json.dumps(manifest, sort_keys=True).encode()
    )
    return data


if __name__ == "__main__":
    for name in sys.argv[1:]:
        print(name, len(load_venue(name)), flush=True)
