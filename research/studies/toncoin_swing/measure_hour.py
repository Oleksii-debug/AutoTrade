"""Measure executable simulation NAV at both ends of one precommitted hour."""

from contextlib import closing
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import sys
from time import sleep
from live import FOLDER, FEE, ADVERSE, bid_for
from source import WhiteBitPublicClient, _write_immutable

OUT = FOLDER / "hour-20261120-morning"


def snapshot():
    book = WhiteBitPublicClient().orderbook("GRAM_USDT")
    book.assert_fresh(datetime.now(UTC))
    with closing(
        sqlite3.connect((FOLDER / "accounts.sqlite3").as_uri() + "?mode=ro", uri=True)
    ) as db:
        rows = db.execute(
            "SELECT payload FROM observations ORDER BY clock DESC"
        ).fetchall()
    state = next(
        json.loads(row[0])
        for row in rows
        if datetime.fromisoformat(json.loads(row[0])["recorded"]) <= book.received_at
    )
    accounts = {}
    for name, account in state["accounts"].items():
        qty, cash = map(Decimal, (account["units"], account["cash"]))
        price = bid_for(book.bids, qty) if qty else None
        if qty and price is None:
            raise ValueError("Insufficient displayed depth")
        nav = cash + (qty * price * (1 - ADVERSE) * (1 - FEE) if qty else 0)
        accounts[name] = dict(
            nav=str(nav),
            units=str(qty),
            cash=str(cash),
            exit_bid=str(price) if qty else None,
            realized=account["realized_usdt"],
            cycles=account["cycles"],
        )
    raw_sha = sha256(book.raw).hexdigest()
    _write_immutable(OUT / (raw_sha + ".raw"), book.raw)
    return dict(
        quote_time=book.received_at.isoformat(),
        exchange_time=book.source_time.isoformat(),
        book_sha=raw_sha,
        account_time=state["recorded"],
        accounts=accounts,
    )


def run(which):
    OUT.mkdir(exist_ok=True, parents=True)
    path = OUT / (which + ".json")
    if path.exists():
        return json.loads(path.read_bytes())
    if which == "end":
        before = json.loads((OUT / "start.json").read_bytes())
        due = datetime.fromisoformat(before["due"])
        now = datetime.now(UTC)
        if now < due:
            raise ValueError("Too early for end quote")
    for attempt in range(4):
        try:
            record = snapshot()
            break
        except (ValueError, RuntimeError, OSError) as error:
            if attempt == 3:
                raise
            sleep(2)
    observed = datetime.fromisoformat(record["quote_time"])
    if which == "start":
        record["due"] = (observed + timedelta(hours=1)).isoformat()
    else:
        record["elapsed_seconds"] = (
            observed - datetime.fromisoformat(before["quote_time"])
        ).total_seconds()
        record["on_time"] = due <= observed <= due + timedelta(seconds=60)
        record["deltas"] = {}
        for name, end in record["accounts"].items():
            first = before["accounts"][name]
            initial, final = Decimal(first["nav"]), Decimal(end["nav"])
            record["deltas"][name] = dict(
                nav_usdt=str(final - initial),
                pct=str((final / initial - 1) * 100),
                realized_usdt=str(
                    Decimal(end["realized"]) - Decimal(first["realized"])
                ),
            )
    _write_immutable(path, json.dumps(record, sort_keys=True, indent=2).encode())
    return record


if __name__ == "__main__" and sys.argv[1] in {"start", "end"}:
    print(json.dumps(run(sys.argv[1]), indent=2))
    from pnl_report import publish

    publish()
