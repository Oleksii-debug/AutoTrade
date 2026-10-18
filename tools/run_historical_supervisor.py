"""Restartable, simulation-only monthly learning supervisor.

Only completed UTC months can become holdouts.  Each cut uses the existing
verified public archive, frozen model, validation, ZERO accounts and journal.
The process can stay up for days; between completed months it waits.  This is
retrospective research and does not claim live-market execution or returns.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import time
from uuid import uuid4

from research.autotrade_research.io.strict_json import strict_json_loads
from tools.historical_public_klines import _reject_symlink_ancestors
from tools.run_historical_campaign import campaign
from tools.run_historical_walkforward import _month, _shift
from tools.update_historical_data import update_history
from tools.historical_diagnostic_sampler import HistoricalDiagnosticSampler

SCHEMA = "autotrade-monthly-supervisor-v1"


def _completed_month(now: datetime) -> str:
    if now.tzinfo is None or now.utcoffset() != timezone.utc.utcoffset(now):
        raise ValueError("supervisor clock must be UTC")
    return _shift(now.strftime("%Y-%m"), -1)


@contextmanager
def _single_owner(path: Path):
    """A held OS file lock, released even if the process crashes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError("supervisor lock redirected")
    with path.open("a+b") as handle:
        try:
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError("another historical supervisor owns this workspace") from exc
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _read_state(path: Path, identity: str, first_holdout: str) -> dict:
    if not path.exists():
        return {"schema": SCHEMA, "identity_sha256": identity,
                "next_holdout_month": first_holdout, "completed": []}
    if path.is_symlink() or path.stat().st_size > 1_000_000:
        raise ValueError("supervisor checkpoint redirected or too large")
    state = strict_json_loads(path.read_text(encoding="utf-8"))
    if (type(state) is not dict or set(state) !=
            {"schema", "identity_sha256", "next_holdout_month", "completed"}
            or state["schema"] != SCHEMA or state["identity_sha256"] != identity
            or type(state["completed"]) is not list
            or type(state["next_holdout_month"]) is not str):
        raise ValueError("supervisor checkpoint identity or schema differs")
    expected = first_holdout
    for row in state["completed"]:
        if (type(row) is not dict or set(row) != {"month", "model_sha256", "report_sha256"}
                or row["month"] != expected or any(
                    type(row[key]) is not str or len(row[key]) != 64 or
                    any(c not in "0123456789abcdef" for c in row[key])
                    for key in ("model_sha256", "report_sha256"))):
            raise ValueError("supervisor checkpoint has a gap or malformed cut")
        expected = _shift(expected, 1)
    if state["next_holdout_month"] != expected:
        raise ValueError("supervisor checkpoint cursor differs")
    return state


def _publish_state(path: Path, state: dict) -> None:
    encoded = (json.dumps(state, sort_keys=True, indent=2) + "\n").encode("utf-8")
    temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    finally:
        temporary.unlink(missing_ok=True)


def supervise(*, data_root: Path, workspace: Path, symbol: str,
              first_holdout: str, training_months: int = 6,
              poll_seconds: int = 3600, max_cuts: int | None = None,
              episode_limit: int | None = None, auto_refresh: bool = True,
              clock=lambda: datetime.now(timezone.utc), sleep=time.sleep,
              progress=None) -> dict:
    """Run until interrupted, or return after max_cuts; restart reuses journals.

    A failed collector/campaign leaves the checkpoint cursor unchanged. The
    next invocation retries the same cut, whose account and model paths are
    immutable and recoverable by their existing owners.
    """
    if symbol not in ("BTCUSDT", "ETHUSDT") or _month(first_holdout) < (2024, 3):
        raise ValueError("unsupported instrument or first holdout")
    if type(training_months) is not int or not 1 <= training_months <= 10:
        raise ValueError("training_months must be in 1..10")
    if type(poll_seconds) is not int or not 1 <= poll_seconds <= 86400:
        raise ValueError("poll_seconds must be in 1..86400")
    if max_cuts is not None and (type(max_cuts) is not int or not 1 <= max_cuts <= 120):
        raise ValueError("max_cuts must be in 1..120")
    if episode_limit is not None and (type(episode_limit) is not int or episode_limit < 1):
        raise ValueError("episode_limit must be positive")
    for path in (data_root, workspace):
        _reject_symlink_ancestors(path)
    # The first holdout needs at least one training month and one separate
    # validation month.  Keep this identity stable across every restart.
    training_start = _shift(first_holdout, -(training_months + 1))
    if training_start < "2024-01":
        training_start = "2024-01"
    identity = sha256(json.dumps({
        "symbol": symbol, "first_holdout": first_holdout,
        "training_months": training_months, "episode_limit": episode_limit,
        "data_root": str(data_root.resolve()),
    }, sort_keys=True).encode()).hexdigest()
    state_dir = workspace / "supervisor" / symbol
    _reject_symlink_ancestors(state_dir)
    with _single_owner(state_dir / "owner.lock"), HistoricalDiagnosticSampler(
            state_dir / "telemetry") as diagnostics:
        def emit(update: dict) -> None:
            diagnostics.update_phase(update["status"], update.get("next_holdout_month"))
            if progress:
                progress(update)

        state_path = state_dir / "state.json"
        state = _read_state(state_path, identity, first_holdout)
        for row in state["completed"]:
            month = row["month"]
            report = workspace / "reports" / (
                f"{symbol}-{month}-{month}-"
                f"{'full' if episode_limit is None else episode_limit}.json")
            if (report.is_symlink() or not report.is_file() or
                    sha256(report.read_bytes()).hexdigest() != row["report_sha256"]):
                raise ValueError("supervisor completed report missing or changed")
            payload = strict_json_loads(report.read_text(encoding="utf-8"))
            if (type(payload) is not dict or payload.get("model_sha256") != row["model_sha256"]
                    or payload.get("periods", {}).get("holdout") != [month, month]):
                raise ValueError("supervisor completed report content differs")
        if state["completed"]:
            # Re-enter the most recent cut through the canonical account
            # recovery path before advancing. This checks its model, source,
            # and both durable financial journals after a crashed process.
            last = state["completed"][-1]
            month = last["month"]
            recovered = campaign(
                data_root=data_root, workspace=workspace, symbol=symbol,
                train_from=max(training_start, _shift(month, -(training_months + 1))),
                train_through=_shift(month, -2),
                validate_from=_shift(month, -1),
                validate_through=_shift(month, -1),
                holdout_from=month, holdout_through=month,
                episode_limit=episode_limit,
            )
            if recovered["model_sha256"] != last["model_sha256"]:
                raise ValueError("recovered supervisor model differs")
        completed_now = 0
        while max_cuts is None or completed_now < max_cuts:
            month = state["next_holdout_month"]
            if _month(month) > _month(_completed_month(clock())):
                emit({"status": "WAITING_FOR_COMPLETED_UTC_MONTH", "next_holdout_month": month})
                if max_cuts is not None:
                    break
                sleep(poll_seconds)
                continue
            validation = _shift(month, -1)
            train_from = max(training_start, _shift(month, -(training_months + 1)))
            train_through = _shift(month, -2)
            if auto_refresh:
                update = update_history(data_root, from_month=train_from,
                                        through_month=month, symbols=(symbol,))
                if not update["complete"]:
                    unavailable = {entry["status"] for entry in update["entries"]
                                   if entry["status"] not in ("VERIFIED_SOURCE", "VERIFIED_CACHE")}
                    if unavailable <= {"TRANSIENT", "MISSING"}:
                        emit({"status": "WAITING_FOR_VERIFIED_PUBLIC_ARCHIVE",
                              "next_holdout_month": month,
                              "source_statuses": sorted(unavailable)})
                        if max_cuts is not None:
                            break
                        sleep(poll_seconds)
                        continue
                    raise ValueError("verified public archive refresh rejected; cut not advanced")
            result = campaign(
                data_root=data_root, workspace=workspace, symbol=symbol,
                train_from=train_from, train_through=train_through,
                validate_from=validation, validate_through=validation,
                holdout_from=month, holdout_through=month,
                episode_limit=episode_limit,
            )
            # campaign publishes this immutable report after both journals have
            # returned. A crash before checkpoint publication retries it.
            report_path = workspace / "reports" / (
                f"{symbol}-{month}-{month}-"
                f"{'full' if episode_limit is None else episode_limit}.json")
            report_digest = sha256(report_path.read_bytes()).hexdigest()
            row = {"month": month, "model_sha256": result["model_sha256"],
                   "report_sha256": report_digest}
            state["completed"].append(row)
            state["next_holdout_month"] = _shift(month, 1)
            _publish_state(state_path, state)
            completed_now += 1
            emit({"status": "CUT_COMPLETED", **row,
                  "next_holdout_month": state["next_holdout_month"],
                  "accounts": result["accounts"]})
        return {"status": "PAUSED_AT_COMPLETED_CUT", "completed_this_run": completed_now,
                "next_holdout_month": state["next_holdout_month"],
                "total_completed": len(state["completed"])}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", required=True)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--symbol", choices=("BTCUSDT", "ETHUSDT"), required=True)
    parser.add_argument("--first-holdout", required=True)
    parser.add_argument("--training-months", type=int, default=6)
    parser.add_argument("--poll-seconds", type=int, default=3600)
    parser.add_argument("--max-cuts", type=int)
    parser.add_argument("--episode-limit", type=int)
    parser.add_argument("--offline", action="store_true", help="Use only already verified local archives")
    args = parser.parse_args(argv)
    try:
        outcome = supervise(
            data_root=Path(args.data_root), workspace=Path(args.workspace),
            symbol=args.symbol, first_holdout=args.first_holdout,
            training_months=args.training_months, poll_seconds=args.poll_seconds,
            max_cuts=args.max_cuts, episode_limit=args.episode_limit,
            auto_refresh=not args.offline,
            progress=lambda event: print(json.dumps(event, sort_keys=True), flush=True),
        )
        print(json.dumps(outcome, sort_keys=True))
        return 0
    except KeyboardInterrupt:
        print("AutoTrade historical supervisor paused; rerun with the same arguments to resume.")
        return 0
    except (OSError, RuntimeError, ValueError, TypeError, ArithmeticError, KeyError) as exc:
        print(f"AutoTrade historical supervisor blocked: {type(exc).__name__}: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
