#!/usr/bin/env python3
"""Durably repeat the public-only WhiteBIT maker-universe screen."""
from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import signal
from threading import Event
from time import monotonic
import sys

ROOT = Path(__file__).resolve().parents[1]
for dependency_root in (ROOT, ROOT / "research"):
    if str(dependency_root) not in sys.path:
        sys.path.insert(0, str(dependency_root))

from research.autotrade_research.artifacts.durable_publish import atomic_write_json
from research.autotrade_research.artifacts.resource_lock import ResourceLock
from research.autotrade_research.integrations.whitebit_public import (
    PublicDataError,
    WhiteBitPublicClient,
)
from research.autotrade_research.strategies.maker_universe import SCHEMA as RESULT_SCHEMA
from tools.screen_whitebit_maker_universe import DEFAULT_EXCLUDED, screen


SCHEMA = "autotrade-whitebit-maker-universe-supervisor-v1"


def _decimal(value: str, name: str) -> Decimal:
    try:
        result = Decimal(value)
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError(f"{name} is invalid") from None
    if not result.is_finite() or result < 0:
        raise ValueError(f"{name} is invalid")
    return result


def execute_cycle(
    *,
    state_dir: Path,
    now: datetime,
    notional_quote: str,
    minimum_quote_volume_24h: str,
    prefilter_limit: int,
    candidate_limit: int,
    client: WhiteBitPublicClient | None = None,
) -> dict:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("maker-universe supervisor clock must be timezone-aware")
    report = screen(
        client=client or WhiteBitPublicClient(),
        now=now.astimezone(UTC),
        notional_quote=notional_quote,
        minimum_quote_volume_24h=minimum_quote_volume_24h,
        prefilter_limit=prefilter_limit,
        candidate_limit=candidate_limit,
        excluded_markets=DEFAULT_EXCLUDED,
    )
    if (report.get("schema") != RESULT_SCHEMA
            or report.get("network_orders") is not False
            or report.get("trading_authority") is not False
            or report.get("automatic_promotion") is not False):
        raise ValueError("maker-universe result authority contract differs")
    evidence = state_dir / "runs" / f"{report['result_sha256']}.json"
    atomic_write_json(evidence, report)
    return {
        "result_sha256": report["result_sha256"],
        "evidence_path": str(evidence),
        "evaluated_count": report["evaluated_count"],
        "candidate_count": report["candidate_count"],
        "candidates": report["candidates"],
        "isolated_error_count": len(report["isolated_errors"]),
        "network_orders": False,
        "trading_authority": False,
        "automatic_promotion": False,
    }


def _wait(stopped: Event, stop_file: Path, seconds: float) -> bool:
    deadline = monotonic() + max(0, seconds)
    while monotonic() < deadline:
        if stopped.wait(min(1.0, deadline - monotonic())) or stop_file.exists():
            return True
    return stopped.is_set() or stop_file.exists()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--notional-quote", default="100")
    parser.add_argument("--minimum-quote-volume-24h", default="100000")
    parser.add_argument("--prefilter-limit", type=int, default=16)
    parser.add_argument("--candidate-limit", type=int, default=4)
    parser.add_argument("--interval-seconds", type=int, default=3600)
    parser.add_argument("--stop-file", type=Path, required=True)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    _decimal(args.notional_quote, "notional_quote")
    _decimal(args.minimum_quote_volume_24h, "minimum_quote_volume_24h")
    if not 1 <= args.candidate_limit <= args.prefilter_limit <= 32:
        parser.error("maker-universe limits are invalid")
    if not 300 <= args.interval_seconds <= 604800:
        parser.error("interval-seconds must be 300..604800")
    state_dir = args.state_dir.resolve(strict=False)
    state_dir.mkdir(parents=True, exist_ok=True)
    if state_dir.is_symlink() or not state_dir.is_dir():
        raise SystemExit("state-dir must be an ordinary directory")
    stop_file = args.stop_file.resolve(strict=False)
    try:
        stop_file.relative_to(state_dir)
    except ValueError:
        raise SystemExit("stop-file must remain inside state-dir") from None
    checkpoint_path = state_dir / "checkpoint.json"
    stopped = Event()
    previous = {}
    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        signum = getattr(signal, name, None)
        if signum is not None:
            previous[signum] = signal.getsignal(signum)
            signal.signal(signum, lambda *_: stopped.set())
    try:
        with ResourceLock(state_dir / "maker-universe-supervisor.lock"):
            stop_file.unlink(missing_ok=True)
            checkpoint = {}
            if checkpoint_path.is_file():
                if checkpoint_path.is_symlink() or checkpoint_path.stat().st_size > 4 << 20:
                    raise ValueError("maker-universe checkpoint is redirected or unbounded")
                checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
                if checkpoint.get("schema") != SCHEMA:
                    raise ValueError("maker-universe supervisor schema differs")
            while not stopped.is_set() and not stop_file.exists():
                now = datetime.now(UTC)
                due_text = checkpoint.get("next_run_at")
                due = datetime.fromisoformat(due_text) if type(due_text) is str else now
                if now < due and not args.once:
                    if _wait(stopped, stop_file, (due - now).total_seconds()):
                        break
                    continue
                started = monotonic()
                try:
                    result = execute_cycle(
                        state_dir=state_dir, now=now,
                        notional_quote=args.notional_quote,
                        minimum_quote_volume_24h=args.minimum_quote_volume_24h,
                        prefilter_limit=args.prefilter_limit,
                        candidate_limit=args.candidate_limit,
                    )
                    status, error = "RUNNING", None
                except (PublicDataError, OSError, ValueError, TypeError, KeyError,
                        RuntimeError) as exc:
                    result = checkpoint.get("last_result")
                    status, error = "RECOVERING", type(exc).__name__
                checkpoint = {
                    "schema": SCHEMA,
                    "status": status,
                    "cycle_count": int(checkpoint.get("cycle_count", 0)) + 1,
                    "last_result": result,
                    "last_error_class": error,
                    "last_cycle_seconds": monotonic() - started,
                    "updated_at": datetime.now(UTC).isoformat(),
                    "next_run_at": (now + timedelta(seconds=args.interval_seconds)).isoformat(),
                    "paid_model_calls": 0,
                    "network_orders": False,
                    "trading_authority": False,
                    "automatic_promotion": False,
                    "profit_guaranteed": False,
                }
                atomic_write_json(checkpoint_path, checkpoint)
                if args.once:
                    return 0 if error is None else 1
            checkpoint.update(status="STOPPED", updated_at=datetime.now(UTC).isoformat())
            atomic_write_json(checkpoint_path, checkpoint)
            return 0
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


if __name__ == "__main__":
    raise SystemExit(main())
