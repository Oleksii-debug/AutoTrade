"""Record Sunday decisions before Monday and settle only from completed WhiteBIT daily bars."""

import argparse
import json
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal as D
from decimal import localcontext
from hashlib import sha256
from pathlib import Path

from research.autotrade_research.artifacts.durable_publish import atomic_write_json
from research.autotrade_research.artifacts.resource_lock import ResourceLock
from tools.whitebit_historical_klines import (
    WhiteBitPublicClient,
    _validate_page,
    _write_immutable,
)

from .accounts import PROTOCOL, Account, configuration

FIRST = date(2026, 11, 23)
END = FIRST + timedelta(weeks=26)
DAY = timedelta(days=1)


def midnight(day):
    return datetime.combine(day, time(), tzinfo=UTC)


def packet(path, sources, record=None):
    if record is not None:
        body = json.dumps(record, sort_keys=True, separators=(",", ":")).encode()
        _write_immutable(
            path,
            json.dumps(
                {"record": record, "sha256": sha256(body).hexdigest()}, sort_keys=True
            ).encode(),
        )
    saved = json.loads(path.read_bytes())
    body = json.dumps(saved["record"], sort_keys=True, separators=(",", ":")).encode()
    if sha256(body).hexdigest() != saved["sha256"]:
        raise ValueError("Invalid forward receipt")
    raw_digest = saved["record"]["raw_sha256"]
    if len(raw_digest) != 64 or any(c not in "0123456789abcdef" for c in raw_digest):
        raise ValueError("Invalid raw source identity")
    if (
        sha256((sources / (raw_digest + ".json")).read_bytes()).hexdigest()
        != raw_digest
    ):
        raise ValueError("Raw market evidence changed")
    return saved["record"]


def read_days(first, last):
    begin = int(midnight(first).timestamp())
    stop = int(midnight(last + DAY).timestamp())
    now = datetime.now(UTC)
    count = (last - first).days + 1
    batch = WhiteBitPublicClient().klines(
        "BTC_USDT", interval="1d", end=stop, limit=count, training=True
    )
    _validate_page(batch, start=begin, interval="1d", count=count, cutoff=now)
    return batch


def capture(today, root, fetch=read_days):
    day = FIRST
    cfg = configuration()
    sources = root / "sources"
    accounts = {
        (case, arm): Account(D(cfg["capital"]))
        for case in ("base", "stress")
        for arm in ("momentum", "passive", "opposite", "cash")
    }
    value = {}
    valid = True
    closed = 0
    marks = []
    while day <= END and day - DAY <= today:
        decision = root / "decisions" / f"{day}.json"
        opening = root / "opens" / f"{day}.json"
        if day == END:
            signal = False
            inverse = False
        else:
            if not decision.exists() and today == day - DAY:
                batch = fetch(day - 9 * DAY, day - 2 * DAY)
                first, last = batch.candles[0], batch.candles[-1]
                if not midnight(day - DAY) <= batch.received_at < midnight(day):
                    raise ValueError("Response outside prospective decision window")
                if min(D(first.base_volume), D(last.base_volume)) <= 0:
                    raise ValueError("No activity on the signal candles")
                _write_immutable(
                    root / "sources" / (sha256(batch.raw).hexdigest() + ".json"),
                    batch.raw,
                )
                packet(
                    decision,
                    sources,
                    {
                        "entry": str(day),
                        "buy_or_hold_btc": D(last.close) > D(first.close),
                        "close_prices": [str(first.close), str(last.close)],
                        "received_at": batch.received_at.isoformat(),
                        "raw_sha256": sha256(batch.raw).hexdigest(),
                        "protocol": PROTOCOL,
                    },
                )
            signal = None
            if decision.exists():
                doc = packet(decision, sources)
                at = datetime.fromisoformat(doc["received_at"])
                if (
                    doc["entry"] != str(day)
                    or doc["protocol"] != PROTOCOL
                    or not midnight(day - DAY) <= at < midnight(day)
                ):
                    raise ValueError("Invalid decision identity")
                signal = doc["buy_or_hold_btc"]
                inverse = D(doc["close_prices"][1]) < D(doc["close_prices"][0])
        if signal is None and today >= day:
            valid = False
        if today <= day:
            break
        if not opening.exists():
            batch = fetch(day, day)
            candle = batch.candles[0]
            if D(candle.base_volume) <= 0 or D(candle.open) <= 0:
                raise ValueError("Missing execution activity")
            _write_immutable(
                root / "sources" / (sha256(batch.raw).hexdigest() + ".json"), batch.raw
            )
            packet(
                opening,
                sources,
                {
                    "entry": str(day),
                    "price": str(candle.open),
                    "raw_sha256": sha256(batch.raw).hexdigest(),
                },
            )
        fill = packet(opening, sources)
        if fill["entry"] != str(day):
            raise ValueError("Execution date mismatch")
        price = D(fill["price"])
        if valid:
            for (case, arm), account in accounts.items():
                fee = cfg["fee_each_side"]
                slip = cfg["adverse_price_each_side_" + case]
                active = (
                    day != END
                    and {
                        "momentum": signal,
                        "passive": True,
                        "opposite": inverse,
                        "cash": False,
                    }[arm]
                )
                account.update(active, price, fee, slip, cfg["stake_at_entry"])
                value[f"{arm}_{case}"] = str(account.liquidation(price, fee, slip))
            marks.append({"date": str(day), "liquidation_value": dict(value)})
            closed += int(day > FIRST)
        day += 7 * DAY
    return {
        "protocol": PROTOCOL,
        "first_monday_utc": str(FIRST),
        "end_monday_utc": str(END),
        "checked_utc": datetime.now(UTC).isoformat(),
        "complete_decision_chain": valid,
        "weeks_observed": closed,
        "monday_marks": marks,
        "execution": "DAILY_OPEN_SIMULATION",
        "orders": 0,
        "paid_model_calls": 0,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    root = parser.parse_args().output.resolve() / "forward"
    root.mkdir(exist_ok=True, parents=True)
    with ResourceLock(root / "study.lock"), localcontext() as ctx:
        ctx.prec = 36
        atomic_write_json(root / "status.json", capture(datetime.now(UTC).date(), root))
