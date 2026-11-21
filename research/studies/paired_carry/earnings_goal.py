"""Measure one prospective learner's capital along its recorded decisions."""
from datetime import datetime, timedelta, UTC
from decimal import Decimal as D
from hashlib import sha256
import json

from data import CODE
from swing import BUY, SELL, buy_qty


def evaluate(db, first, weeks, today, rules):
    raw = (CODE / "earnings-goal.json").read_bytes()
    spec = json.loads(raw)
    initial = D(spec["initial_simulated_capital"])
    target = D(spec["target_net_fraction_per_week"])
    capital = initial
    path = []
    missing = []
    for index in range(weeks):
        day = first + timedelta(weeks=index)
        if today <= day + timedelta(weeks=1):
            break
        key = day.isoformat()
        row = db.execute(
            "SELECT p.recorded, p.body, o.body, o.recorded FROM predictions p "
            "JOIN outcomes o USING(d) WHERE p.d=?", (key,)
        ).fetchone()
        if not row:
            missing.append(key)
            continue
        prediction_time = datetime.fromisoformat(row[0])
        outcome_time = datetime.fromisoformat(row[3])
        if prediction_time.tzinfo is None or outcome_time.tzinfo is None:
            raise ValueError("Weekly evidence requires UTC-aware timestamps")
        recorded = prediction_time.astimezone(UTC).date()
        settled = outcome_time.astimezone(UTC).date()
        if recorded != day - timedelta(days=1) or settled <= day + timedelta(weeks=1):
            raise ValueError("Forecast or settlement outside its permitted clock")
        if settled > today:
            missing.append(key)
            continue
        if missing:
            continue  # Capital after an unobserved week is unknown.
        decision, outcome = json.loads(row[1]), json.loads(row[2])
        market = decision["target"]
        before = capital
        if market:
            prices = outcome["moves"][market]
            opened, closed = D(prices["open"]), D(prices["close"])
            q = buy_qty(capital / 2, opened, rules[market])
            capital += q * (closed * SELL - opened * BUY)
        weekly = capital / before - 1
        path.append(dict(week=key, market=market, net_usdt=str(capital-before),
                         capital=str(capital), weekly_return=str(weekly), hit=weekly >= target))
    complete = len(path) == weeks
    count = sum(r["hit"] for r in path)
    geometric = (capital/initial) ** (D(1)/len(path)) - 1 if path else None
    status = "collecting"
    if missing:
        status = "missing_evidence"
    elif complete:
        status = "weekly_target_verified" if count == weeks else "weekly_target_not_verified"
    return dict(objective=spec["objective_uk"], goal_sha256=sha256(raw).hexdigest(),
                status=status, simulated=True, verified_weeks=len(path), planned_weeks=weeks,
                weeks_at_target=count, missing=missing, last_verified_capital_usdt=str(capital),
                geometric_weekly_return=str(geometric) if geometric is not None else None, path=path)
