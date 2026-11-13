"""Observe one shared virtual portfolio from the BTC/ETH/SOL learner journal."""
from __future__ import annotations

import argparse
from collections import Counter
from collections.abc import Callable
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import json
from pathlib import Path
import time

from research.autotrade_research.artifacts.durable_publish import atomic_write_json
from research.autotrade_research.artifacts.resource_lock import ResourceLock
from research.autotrade_research.learning.observed_quote_learning import propose_from_observed_outcomes
from research.autotrade_research.strategies.technical_confluence import digest, summarize_hour_windows


SCHEMA = "autotrade-observed-portfolio-shadow-v1"
SOURCE_SCHEMA = "autotrade-whitebit-subhour-forward-shadow-v1"
HORIZON = 1800
OBSERVATION_STEP = 900
TAIL_BYTES = 256 * 1024
MARKETS = ("BTC_PERP", "ETH_PERP", "SOL_PERP")
LABEL_POLICY = "m15-delayed-entry-m30-hold-v1"
LEGACY_POLICY = "decision-quote-training-v1"
RECENT_LIMIT = 336


def new_state(now: datetime) -> dict:
    return {"schema": SCHEMA, "started_at": now.isoformat(),
            "last_boundary_epoch": None, "queued": None, "pending": None, "recent": [],
            "settled_count": 0, "unmatched_count": 0,
            "network_orders": False, "trading_authority": False, "paid_model_calls": 0}


def _market_entries(event: dict, markets: tuple[str, ...]):
    if "SOL_PERP" in markets:
        yield "SOL_PERP", event.get("execution_observation"), event.get("execution_outcomes", [])
    for market, context in event.get("additional_market_research", {}).items():
        if market in markets:
            yield market, context.get("observation"), context.get("settled", [])


def _decision(market: str, observation: dict | None, boundary: int,
              observed: datetime, now: datetime) -> dict | None:
    if not observation or observation["entry_boundary_epoch"] != boundary:
        return None
    received = datetime.fromisoformat(observation["quote_received_at"])
    if not timedelta(0) <= observed - received <= timedelta(seconds=5):
        return None
    return {"market": market, "decision_boundary_epoch": boundary,
            "planned_entry_epoch": boundary + OBSERVATION_STEP, "frozen_at": now.isoformat(),
            "decision_quote_sha256": observation["entry_quote_sha256"],
            "decision_quote_received_at": observation["quote_received_at"]}


def _choose(proposals: list[tuple[dict, dict]]) -> dict | None:
    candidates = []
    for decision, proposal in proposals:
        action = proposal["action"]
        score = Decimal(0)
        if proposal["status"] == "ESTIMATED" and action in ("LONG", "SHORT"):
            score = Decimal(proposal["expected_net_equity_returns"][action])
        if score.is_finite():
            candidates.append((score, decision["market"], decision, proposal))
    if not candidates:
        return None
    score, _, decision, proposal = max(candidates, key=lambda item: (item[0], item[1]))
    return {**decision, "action": proposal["action"] if score > 0 else "HOLD",
            "expected_net_equity_return": str(max(score, Decimal(0))),
            "model_sha256": digest({"label_policy": LABEL_POLICY, "fit": proposal["model_sha256"]}),
            "label_policy": LABEL_POLICY, "training_outcome_count": proposal["training_outcome_count"]}


def _later_entry(queued: dict, observation: dict, boundary: int) -> dict | None:
    received = datetime.fromisoformat(observation["quote_received_at"])
    frozen = datetime.fromisoformat(queued["frozen_at"])
    if (observation["entry_boundary_epoch"] != boundary or received <= frozen
            or observation["entry_quote_sha256"] == queued["decision_quote_sha256"]):
        return None
    return {**queued, "entry_boundary_epoch": boundary, "due_boundary_epoch": boundary + HORIZON,
            "entry_quote_sha256": observation["entry_quote_sha256"],
            "entry_quote_received_at": observation["quote_received_at"]}


def _enter(queued: dict, entries: list, boundary: int) -> dict | None:
    for market, observation, _ in entries:
        if market == queued["market"] and observation:
            return _later_entry(queued, observation, boundary)
    return None


def _outcome_available(outcome: dict, observed: datetime) -> bool:
    received = datetime.fromisoformat(outcome["quote_received_at"])
    exited = datetime.fromisoformat(outcome["exit_quote_received_at"])
    return received < exited <= observed


def _matching_outcome(pending: dict, entries: list, boundary: int, observed: datetime) -> dict | None:
    for market, _, outcomes in entries:
        if market != pending["market"]:
            continue
        for item in outcomes:
            if (item["kind"] == "OBSERVED_FIXED_TIME_EXECUTION_OUTCOME"
                    and item["entry_boundary_epoch"] == pending["entry_boundary_epoch"]
                    and item["exit_boundary_epoch"] == boundary
                    and item["entry_quote_sha256"] == pending["entry_quote_sha256"]
                    and item["quote_received_at"] == pending["entry_quote_received_at"]
                    and _outcome_available(item, observed)):
                return item
    return None


def _resolve(pending: dict, entries: list, boundary: int, observed: datetime) -> dict | None:
    item = _matching_outcome(pending, entries, boundary, observed)
    if item is None:
        return None
    action = pending["action"]
    net = Decimal(0) if action == "HOLD" else Decimal(item["outcomes"][action]["net_equity_return"])
    if not net.is_finite() or net <= -1:
        return None
    return {**pending, "exit_quote_sha256": item["exit_quote_sha256"],
            "exit_quote_received_at": item["exit_quote_received_at"],
            "net_equity_return": str(net)}


def _advance_training(state: dict, entries: list, boundary: int,
                      observed: datetime, now: datetime) -> list[tuple[dict, dict]]:
    """Associate decision features with only later, settled execution quotes."""
    training = state.setdefault("delayed_training", {})
    proposals = []
    for market, observation, outcomes in entries:
        memory = training.setdefault(market, {"decisions": [], "recent": []})
        remaining = []
        for decision in memory["decisions"]:
            entry_epoch = decision["planned_entry_epoch"]
            if entry_epoch + HORIZON > boundary:
                remaining.append(decision)
            elif entry_epoch + HORIZON == boundary:
                for label in outcomes:
                    if (label["kind"] != "OBSERVED_FIXED_TIME_EXECUTION_OUTCOME"
                            or label["exit_boundary_epoch"] != boundary):
                        continue
                    entry = _later_entry(decision, label, entry_epoch)
                    if entry is None or not _outcome_available(label, observed):
                        continue
                    memory["recent"] = (memory["recent"] + [{
                        **entry, "exit_boundary_epoch": boundary, "outcome_id": label["outcome_id"],
                        "exit_quote_sha256": label["exit_quote_sha256"],
                        "exit_quote_received_at": label["exit_quote_received_at"],
                        "outcomes": label["outcomes"],
                    }])[-RECENT_LIMIT:]
                    break
        memory["decisions"] = remaining

        if boundary % HORIZON or now - observed > timedelta(seconds=60):
            continue
        decision = _decision(market, observation, boundary, observed, now)
        if decision is None:
            continue
        technical = observation.get("technical")
        memory["decisions"].append({**decision, "technical": deepcopy(technical)})
        # The shared region estimator indexes the feature time as entry time.
        # Keep the actual execution entry in durable memory and adapt only its fit input.
        fit_rows = [{**item, "entry_boundary_epoch": item["decision_boundary_epoch"]}
                    for item in memory["recent"]]
        proposal = propose_from_observed_outcomes(
            fit_rows, entry_epoch=boundary,
            quote_received_at=datetime.fromisoformat(decision["decision_quote_received_at"]),
            technical=technical,
        )
        proposals.append((decision, proposal))
    return proposals


def _report(recent: list[dict]) -> dict:
    by_entry = {row["entry_boundary_epoch"]: row for row in recent}
    hours = []
    for epoch, row in sorted(by_entry.items()):
        if (epoch - OBSERVATION_STEP) % 3600:
            continue
        second = by_entry.get(epoch + HORIZON)
        if second is None or row.get("label_policy", LEGACY_POLICY) != second.get("label_policy", LEGACY_POLICY):
            continue
        hours.append((row.get("label_policy", LEGACY_POLICY),
                      (1 + Decimal(row["net_equity_return"])) * (1 + Decimal(second["net_equity_return"])) - 1))

    values = [value for policy, value in hours if policy == LABEL_POLICY]
    compound = Decimal(1)
    for value in values:
        compound *= 1 + value
    return {"hourly": summarize_hour_windows(values, 1),
            "compound_complete_hour_return": str(compound - 1),
            "hour_start_minute_utc": OBSERVATION_STEP // 60,
            "label_policy": LABEL_POLICY,
            "legacy_hourly": summarize_hour_windows([value for policy, value in hours
                                                      if policy == LEGACY_POLICY], 1),
            "recent_action_counts": dict(Counter(row["action"] for row in recent
                                                 if row.get("label_policy") == LABEL_POLICY))}


def advance(
    state: dict, event: dict, *, now: datetime, markets: tuple[str, ...] = MARKETS,
    capacity_check: Callable[[dict], bool] | None = None,
) -> bool:
    """Settle a prior frozen selection and record a timely new selection."""
    if event.get("schema") != SOURCE_SCHEMA or "processed_bar_open_epoch" not in event:
        return False
    observed = datetime.fromisoformat(event["observed_at"])
    if observed < datetime.fromisoformat(state["started_at"]) or observed > now:
        return False
    boundary = event["processed_bar_open_epoch"] + 900
    last = state["last_boundary_epoch"]
    if last is not None and boundary <= last:
        return False
    state["last_boundary_epoch"] = boundary
    entries = list(_market_entries(event, markets))
    pending = state["pending"]
    if pending is not None and boundary >= pending["due_boundary_epoch"]:
        result = _resolve(pending, entries, boundary, observed) if boundary == pending["due_boundary_epoch"] else None
        state["pending"] = None
        if result is None:
            state["unmatched_count"] += 1
        else:
            if capacity_check is not None and result["action"] in ("LONG", "SHORT"):
                result["observed_capacity_check"] = capacity_check(result)
            state["recent"] = (state["recent"] + [result])[-RECENT_LIMIT:]
            state["settled_count"] += 1
            state["evaluation"] = _report(state["recent"])
    queued = state.get("queued")
    if queued is not None and boundary >= queued["planned_entry_epoch"]:
        state["queued"] = None
        if boundary == queued["planned_entry_epoch"] and state["pending"] is None:
            state["pending"] = _enter(queued, entries, boundary)
        if state["pending"] is None:
            state["unmatched_count"] += 1
    proposals = _advance_training(state, entries, boundary, observed, now)
    if proposals:
        state["queued"] = _choose(proposals)
    return True


def _recent_events(path: Path):
    with path.open("rb") as stream:
        length = stream.seek(0, 2)
        start = max(0, length - TAIL_BYTES)
        stream.seek(start)
        if start:
            stream.readline()
        for line in stream.read().splitlines(keepends=True):
            if line.endswith(b"\n"):
                try:
                    event = json.loads(line)
                except (UnicodeError, json.JSONDecodeError):
                    continue
                if type(event) is dict:
                    yield event


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-journal", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    state_dir = args.state_dir.resolve()
    state_dir.mkdir(parents=True, exist_ok=True)
    checkpoint = state_dir / "checkpoint.json"
    stop = state_dir / "managed.stop.json"
    with ResourceLock(state_dir / "observed-portfolio.lock"):
        state = json.loads(checkpoint.read_bytes()) if checkpoint.is_file() else new_state(datetime.now(UTC))
        if state.get("schema") != SCHEMA:
            raise ValueError("observed portfolio checkpoint schema differs")
        state["status"] = "OBSERVING"
        atomic_write_json(checkpoint, state)
        previous = None
        while not stop.exists():
            if args.source_journal.is_file():
                stat = args.source_journal.stat()
                identity = (stat.st_size, stat.st_mtime_ns, stat.st_ino)
                if identity != previous:
                    for event in _recent_events(args.source_journal):
                        opened = event.get("processed_bar_open_epoch")
                        last = state["last_boundary_epoch"]
                        if type(opened) is not int or (last is not None and opened + OBSERVATION_STEP <= last):
                            continue
                        candidate = deepcopy(state)
                        try:
                            changed = advance(candidate, event, now=datetime.now(UTC))
                        except (KeyError, TypeError, ValueError, ArithmeticError):
                            continue
                        if changed:
                            state = candidate
                            state["updated_at"] = datetime.now(UTC).isoformat()
                            atomic_write_json(checkpoint, state)
                    previous = identity
            if args.once:
                return 0
            time.sleep(5)
        state["status"] = "STOPPED"
        atomic_write_json(checkpoint, state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

