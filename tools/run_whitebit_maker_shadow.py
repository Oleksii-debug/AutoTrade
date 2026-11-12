#!/usr/bin/env python3
"""Run a durable, public-data-only WhiteBIT maker fill shadow."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import signal
import sys
from threading import Event

ROOT = Path(__file__).resolve().parents[1]
for dependency_root in (ROOT, ROOT / "research"):
    if str(dependency_root) not in sys.path:
        sys.path.insert(0, str(dependency_root))

from research.autotrade_research.artifacts.durable_publish import atomic_write_json
from research.autotrade_research.artifacts.resource_lock import ResourceLock
from research.autotrade_research.integrations.whitebit_public import (
    PublicDataError, WhiteBitPublicClient, _symbol,
)
from research.autotrade_research.io.strict_json import strict_json_loads
from research.autotrade_research.strategies.maker_shadow import (
    SCHEMA, advance_evidence_wave, evaluate_quote, open_quote, quote_admission,
)


def _record_quote_admission(
    state: dict[str, object], admission: dict[str, object],
) -> dict[str, object]:
    """Persist bounded quote-admission evidence across restarts."""
    if type(state) is not dict or type(admission) is not dict:
        raise TypeError("maker admission state must be dictionaries")
    reason = admission.get("reason")
    admitted = admission.get("admitted")
    if type(reason) is not str or not reason or type(admitted) is not bool:
        raise ValueError("maker admission result is invalid")
    counts = dict(state.get("quote_admission_counts", {}))
    if any(type(key) is not str or type(value) is not int or value < 0
           for key, value in counts.items()):
        raise ValueError("maker admission counters are invalid")
    counts[reason] = int(counts.get(reason, 0)) + 1
    attempts = int(state.get("quote_admission_attempts", 0)) + 1
    admitted_count = int(state.get("quote_admitted_count", 0)) + int(admitted)
    return {
        **state,
        "last_quote_admission": admission,
        "quote_admission_attempts": attempts,
        "quote_admitted_count": admitted_count,
        "quote_admission_counts": dict(sorted(counts.items())),
    }


def _append(path: Path, value: dict[str, object]) -> None:
    if path.exists() and path.stat().st_size >= 8 * 1024 * 1024:
        archive = path.with_suffix(".jsonl.1")
        archive.unlink(missing_ok=True)
        os.replace(path, archive)
    body = json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n"
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(body)
        stream.flush()
        os.fsync(stream.fileno())


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--market", required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--interval-seconds", type=float, default=5)
    parser.add_argument("--maximum-quote-seconds", type=int, default=60)
    parser.add_argument("--notional-quote", default="100")
    parser.add_argument("--forced-exit-impact-bps", default="5")
    parser.add_argument("--cycle-cycles", type=int, default=12)
    parser.add_argument("--stop-file", type=Path, required=True)
    args = parser.parse_args(argv)
    market = _symbol(args.market)
    if not 3 <= args.interval_seconds <= 60:
        raise SystemExit("interval-seconds must be 3..60")
    state_dir = args.state_dir.resolve(strict=False)
    state_dir.mkdir(parents=True, exist_ok=True)
    if state_dir.is_symlink() or not state_dir.is_dir():
        raise SystemExit("state-dir must be an ordinary directory")
    stop_file = args.stop_file.resolve(strict=False)
    try:
        stop_file.relative_to(state_dir)
    except ValueError:
        raise SystemExit("stop-file must remain inside state-dir") from None
    stopped = Event()
    previous = {}
    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        signum = getattr(signal, name, None)
        if signum is not None:
            previous[signum] = signal.getsignal(signum)
            signal.signal(signum, lambda *_: stopped.set())
    try:
        with ResourceLock(state_dir / "maker-shadow.lock"):
            stop_file.unlink(missing_ok=True)
            checkpoint_path = state_dir / "checkpoint.json"
            state = {
                "schema": SCHEMA, "phase": "STARTING", "market": market,
                "open_quote": None, "settled_cycles": 0,
                "net_pnl_quote": "0", "positive_cycles": 0,
                "research_phase": "DISCOVERY", "wave_cycles": 0,
                "wave_positive_cycles": 0, "wave_net_pnl_quote": "0",
                "wave_outcomes": {},
                "network_orders": False, "trading_authority": False,
                "paid_model_calls": 0,
            }
            if checkpoint_path.is_file() and not checkpoint_path.is_symlink():
                restored = strict_json_loads(checkpoint_path.read_text(encoding="utf-8"))
                if (type(restored) is not dict or restored.get("schema") != SCHEMA
                        or restored.get("market") != market
                        or restored.get("network_orders") is not False
                        or restored.get("trading_authority") is not False):
                    raise ValueError("maker shadow checkpoint differs")
                legacy_wave = "research_phase" not in restored
                state.update(restored)
                if legacy_wave:
                    state.update({
                        "research_phase": "DISCOVERY",
                        "wave_cycles": int(restored.get("settled_cycles", 0)),
                        "wave_positive_cycles": int(restored.get("positive_cycles", 0)),
                        "wave_net_pnl_quote": str(restored.get("net_pnl_quote", "0")),
                        "wave_outcomes": {},
                    })
                if (state.get("research_phase") == "RESEARCH_ONLY"
                        and (state.get("latest_wave_analysis") or {}).get("stage")
                        in {"DISCOVERY", "EVALUATING"}
                        and not state.get("adaptive_filter_version")):
                    state.update({
                        "research_phase": "RESEARCH_FILTERED",
                        "wave_cycles": 0, "wave_positive_cycles": 0,
                        "wave_net_pnl_quote": "0", "wave_outcomes": {},
                    })
                state["adaptive_filter_version"] = "NEUTRAL_FLOW_V1"
                admission = state.get("last_quote_admission")
                if ("FILTERED" in str(state.get("research_phase"))
                        and type(admission) is dict
                        and admission.get("reason") == "UNFILTERED_DISCOVERY"
                        and type(state.get("open_quote")) is dict):
                    state["discarded_transition_quote_sha256"] = state["open_quote"].get(
                        "quote_sha256"
                    )
                    state["open_quote"] = None
                if not state.get("inventory_hedge_version"):
                    state["invalidated_delayed_hedge_wave"] = {
                        "research_phase": state.get("research_phase"),
                        "wave_cycles": int(state.get("wave_cycles", 0)),
                        "wave_positive_cycles": int(state.get("wave_positive_cycles", 0)),
                        "wave_net_pnl_quote": str(state.get("wave_net_pnl_quote", "0")),
                        "reason": "DELAYED_ONE_SIDED_HEDGE_MODEL",
                        "experience_preserved": True,
                        "network_orders": False, "trading_authority": False,
                    }
                    state.update({
                        "wave_cycles": 0, "wave_positive_cycles": 0,
                        "wave_net_pnl_quote": "0", "wave_outcomes": {},
                        "open_quote": None,
                    })
                state["inventory_hedge_version"] = "IMMEDIATE_NEXT_POLL_V1"
            client = WhiteBitPublicClient()
            instruments = [item for item in client.instruments() if item.market == market]
            if len(instruments) != 1:
                raise ValueError("maker shadow instrument is unavailable")
            instrument = instruments[0]
            while not stopped.is_set() and not stop_file.exists():
                if state.get("research_phase") == "RESEARCH_ONLY":
                    state.update({
                        "phase": "RUNNING",
                        "checkpointed_at": datetime.now(timezone.utc).isoformat(),
                        "automatic_promotion": False,
                    })
                    atomic_write_json(checkpoint_path, state)
                    stopped.wait(max(args.interval_seconds, 60))
                    continue
                try:
                    book = client.instrument_orderbook(market)
                    book.assert_fresh(datetime.now(timezone.utc), maximum_age_seconds=30)
                    batch = client.recent_trades(market)
                    current = state.get("open_quote")
                    if current is None:
                        filtered = "FILTERED" in str(state.get("research_phase"))
                        admission = quote_admission(
                            book, instrument, batch,
                            adverse_selection_reserve_bps=args.forced_exit_impact_bps,
                        ) if filtered else {"admitted": True, "reason": "UNFILTERED_DISCOVERY"}
                        state = _record_quote_admission(state, admission)
                        if admission["admitted"]:
                            state["open_quote"] = open_quote(
                                book, instrument, batch, notional_quote=args.notional_quote,
                            )
                    else:
                        evaluated = evaluate_quote(
                            current, book, batch, now=datetime.now(timezone.utc),
                            maximum_age_seconds=args.maximum_quote_seconds,
                            forced_exit_impact_bps=args.forced_exit_impact_bps,
                        )
                        if evaluated["status"] == "SETTLED":
                            _append(state_dir / "maker-cycles.jsonl", evaluated)
                            state["settled_cycles"] = int(state["settled_cycles"]) + 1
                            net = str(evaluated["net_pnl_quote"])
                            state["net_pnl_quote"] = format(
                                Decimal(str(state["net_pnl_quote"])) + Decimal(net), "f"
                            )
                            state["positive_cycles"] = int(state["positive_cycles"]) + int(
                                Decimal(net) > 0
                            )
                            state = advance_evidence_wave(
                                state, net_pnl_quote=net,
                                outcome=str(evaluated["outcome"]),
                                target_cycles=args.cycle_cycles,
                            )
                            state["last_settled"] = evaluated
                            # Re-enter through the admission path on the next poll.
                            # This is essential when a wave boundary changes policy.
                            state["open_quote"] = None
                        else:
                            state["open_quote"] = evaluated
                    state.update({
                        "phase": "RUNNING",
                        "checkpointed_at": datetime.now(timezone.utc).isoformat(),
                        "maker_fee_rate": instrument.maker_fee_rate,
                        "taker_fee_rate": instrument.taker_fee_rate,
                        "errors": {}, "profit_guaranteed": False,
                        "automatic_promotion": False,
                    })
                except PublicDataError as error:
                    state.update({
                        "phase": "DEGRADED",
                        "checkpointed_at": datetime.now(timezone.utc).isoformat(),
                        "errors": {"public_data": str(error)},
                    })
                atomic_write_json(checkpoint_path, state)
                stopped.wait(args.interval_seconds)
            state.update({
                "phase": "STOPPED",
                "checkpointed_at": datetime.now(timezone.utc).isoformat(),
            })
            atomic_write_json(checkpoint_path, state)
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())





