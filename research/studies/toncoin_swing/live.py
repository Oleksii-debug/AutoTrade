"""Two independent spot simulation accounts using only previously saved decisions and displayed depth."""

from contextlib import closing
from datetime import datetime, UTC
from decimal import Decimal as D
from hashlib import sha256
import json
import sqlite3
from time import sleep

from source import ROOT, WhiteBitPublicClient, _write_immutable

FOLDER = ROOT / "forward"
FEE = D(".001")
ADVERSE = D(".0015")
START = "2026-11-20"
LAST_ENTRY = "2027-02-17"


def buy(asks, cash):
    left = cash / ((1 + FEE) * (1 + ADVERSE))
    budget = left
    units = D(0)
    for price, size in asks:
        price = D(price)
        spent = min(left, price * D(size))
        units += spent / price
        left -= spent
        if left == 0:
            return units, budget * (1 + ADVERSE) / units
    return None


def bid_for(bids, units):
    left = units
    value = D(0)
    for price, size in bids:
        taken = min(D(size), left)
        value += taken * D(price)
        left -= taken
        if left == 0:
            return value / units
    return None


def initial():
    return dict(
        cash="1000",
        units="0",
        armed=True,
        cycles=0,
        realized_usdt="0",
        last_mark="1000",
    )


def apply(state, arm, book, decision, is_entry, at):
    s = state.copy()
    units = D(s["units"])
    event = "OBSERVE"
    if units:
        bid = bid_for(book["bids"], units)
        if bid is None:
            return dict(s, last_mark=None), "INSUFFICIENT_BID"
        value = units * bid * (1 - ADVERSE) * (1 - FEE)
        s["last_mark"] = str(value)
        reason = None
        if str(at.date()) > LAST_ENTRY:
            reason = "END_OF_STUDY"
        elif bid >= D(s["target"]):
            reason = "TARGET"
        elif arm == "channel" and bid <= D(s["stop"]):
            reason = "STOP"
        elif arm == "channel" and at.timestamp() - s["entry_epoch"] >= 14 * 86400:
            reason = "TIME"
        if reason:
            s.update(
                cash=str(value),
                units="0",
                armed=False,
                cycles=s["cycles"] + 1,
                realized_usdt=str(value - 1000),
            )
            event = "SELL_" + reason
    elif is_entry:
        if arm == "wait10":
            deep = D(str(decision["drawdown"])) >= D(".10")
            if not deep:
                s["armed"] = True
            enter = deep
        else:
            if not s["armed"] and D(str(decision["close"])) > D(s["lower"]):
                s["armed"] = True
            enter = decision["channel_candidate"]
        if not (s["armed"] and enter):
            return s, "HOLD"
        fill = buy(book["asks"], D(s["cash"]))
        if fill is None:
            return s, "INSUFFICIENT_ASK"
        units, price = fill
        target = price * D("1.10")
        if arm == "channel":
            setup = decision["channel"]
            target = D(str(setup["target"]))
            if not (
                D(str(setup["floor"])) <= price <= D(str(setup["lower"]))
                and target / price - 1 >= D(".05")
            ):
                return s, "OPEN_PRICE_OUTSIDE_RANGE"
            s.update(stop=str(setup["stop"]), lower=str(setup["lower"]))
        s.update(
            cash="0",
            units=str(units),
            paid=str(price),
            target=str(target),
            entry_epoch=at.timestamp(),
        )
        liquidation = bid_for(book["bids"], units)
        s["last_mark"] = (
            str(units * liquidation * (1 - ADVERSE) * (1 - FEE))
            if liquidation
            else None
        )
        event = "BUY_SIMULATED"
    return s, event


def saved_book(meta):
    raw = (FOLDER / (meta["book_sha"] + ".raw")).read_bytes()
    if sha256(raw).hexdigest() != meta["book_sha"]:
        raise ValueError("Market depth changed")
    book = json.loads(raw)
    if book["ticker_id"] != "GRAM_USDT":
        raise ValueError("Wrong market")
    return book


def tick():
    FOLDER.mkdir(exist_ok=True, parents=True)
    now = datetime.now(UTC)
    day = str(now.date())
    dbfile = FOLDER / "accounts.sqlite3"
    with closing(sqlite3.connect(dbfile, timeout=20)) as db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS observations (clock INTEGER PRIMARY KEY, payload TEXT NOT NULL)"
        )
        db.execute("BEGIN IMMEDIATE")
        last = db.execute(
            "SELECT clock,payload FROM observations ORDER BY clock DESC LIMIT 1"
        ).fetchone()
        accounts = (
            json.loads(last[1])["accounts"]
            if last
            else {k: initial() for k in ("wait10", "channel")}
        )
        active = any(D(s["units"]) for s in accounts.values())
        clock = int(now.timestamp()) // 3600 * 3600
        if last and last[0] >= clock:
            return
        if day < START or (day > LAST_ENTRY and not active):
            return
        signal_path = FOLDER / (day + ".json")
        opening = FOLDER / (day + ".book.json")
        is_entry = (
            now.hour == 1
            and day <= LAST_ENTRY
            and opening.exists()
            and signal_path.exists()
        )
        decision = None
        if is_entry:
            meta = json.loads(opening.read_bytes())
            decision = json.loads(signal_path.read_bytes())
            at = datetime.fromisoformat(meta["recorded"])
            daily_raw = (FOLDER / (decision["raw_sha256"] + ".raw")).read_bytes()
            if (
                meta["decision_sha"] != sha256(signal_path.read_bytes()).hexdigest()
                or sha256(daily_raw).hexdigest() != decision["raw_sha256"]
                or not datetime.fromisoformat(decision["recorded"]).timestamp() < clock
                or decision["entry_epoch"] != clock
                or not clock <= at.timestamp() < clock + 120
            ):
                raise ValueError("Clock or frozen decision mismatch")
        elif active:
            book = WhiteBitPublicClient().orderbook("GRAM_USDT")
            at = book.received_at
            if int(at.timestamp()) // 3600 * 3600 != clock:
                return
            book.assert_fresh(at)
            identity = sha256(book.raw).hexdigest()
            _write_immutable(FOLDER / (identity + ".raw"), book.raw)
            meta = dict(recorded=at.isoformat(), book_sha=identity)
        else:
            return
        book = saved_book(meta)
        actions = {}
        for name, state in list(accounts.items()):
            accounts[name], actions[name] = apply(
                state, name, book, decision, is_entry, at
            )
        packet = dict(
            recorded=at.isoformat(),
            book=meta,
            accounts=accounts,
            actions=actions,
            missed_hourly_clocks=(clock - last[0]) // 3600 - 1 if last else None,
            actual_orders=0,
            paid_model_calls=0,
        )
        db.execute(
            "INSERT INTO observations VALUES (?,?)",
            (clock, json.dumps(packet, sort_keys=True)),
        )
        db.commit()
        print(json.dumps(packet, sort_keys=True))


def monitored_tick():
    for attempt in range(3):
        try:
            from freeze_daily import freeze
            from open_quote import capture

            if str(datetime.now(UTC).date()) <= LAST_ENTRY:
                freeze()
                capture()
            tick()
            return
        except (OSError, ValueError, RuntimeError) as exc:
            with (FOLDER / "errors.jsonl").open("a", encoding="utf-8") as log:
                print(
                    json.dumps(
                        dict(at=datetime.now(UTC).isoformat(), error=type(exc).__name__)
                    ),
                    file=log,
                )
            if attempt == 2:
                raise
            sleep(5)
