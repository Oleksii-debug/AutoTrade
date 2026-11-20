"""Count distinct 10% pullback episodes, including opportunities while the simulator held TON."""

from datetime import UTC, datetime
import json
from itertools import pairwise
from source import ROOT, stamp, load_venue
from replay import DAY, HOUR, CONFIG


def count(bars, first, last):
    rows = []
    for t in range(first, last, DAY):
        history = [t - 2 * HOUR - i * DAY for i in range(16)]
        future = range(t + HOUR, t + 14 * DAY, HOUR)
        if (
            future.stop >= last
            or t not in bars
            or not all(x in bars for x in (*history, *future))
        ):
            continue
        closes = [bars[x][3] for x in history]
        if (
            1 - closes[0] / max(closes[1:15]) < 0.10
            or 1 - closes[1] / max(closes[2:16]) >= 0.10
        ):
            continue
        entry = bars[t][0] * 1.0015
        hit = next(
            (
                x
                for x in future
                if bars[x][4] > 0 and bars[x][1] > entry * 1.10 * 1.0015
            ),
            None,
        )
        rows.append(
            dict(
                day=datetime.fromtimestamp(t, UTC).date().isoformat(),
                target_14d=hit is not None,
            )
        )
    return dict(
        episodes=len(rows), hits=sum(x["target_14d"] for x in rows), observations=rows
    )


if __name__ == "__main__":
    cuts = [stamp(d + "T01:00") for d in CONFIG["phase_boundaries_monday_utc"]]
    data = {}
    for venue in ("Bybit", "WhiteBIT"):
        bars = load_venue(venue)
        items = [count(bars, a, b) for a, b in pairwise(cuts)]
        print(venue, [(x["hits"], x["episodes"]) for x in items])
        data[venue] = items
    (ROOT / "opportunities.json").write_text(json.dumps(data, indent=2))
