"""Bounded read-only public ZERO learning projection; never imports the learner."""
from datetime import datetime, timezone
from contextlib import closing
from decimal import Decimal
import json
from pathlib import Path
import re
import sqlite3
import stat


_PHASES = {"RUNNING", "RESEARCH", "PAUSED", "ANALYZING", "TRAINING", "EVALUATING", "COOLDOWN", "RESEARCH_ONLY"}
_MAX_STATE_BYTES = 65536


def _count(value):
    if type(value) is not int or not 0 <= value <= 9007199254740991:
        raise ValueError("count")
    return value


def _money(value):
    if type(value) is not str or len(value) > 64 or not re.fullmatch(r"-?\d+(?:\.\d+)?", value):
        raise ValueError("decimal")
    number = Decimal(value)
    if not number.is_finite() or abs(number) > Decimal("1e18"):
        raise ValueError("decimal range")
    return value


def _time(value):
    if type(value) is not str or len(value) > 40:
        raise ValueError("timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError("timestamp zone")
    return parsed


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate field")
        result[key] = value
    return result


def _bad_constant(value):
    raise ValueError("nonfinite JSON")


def _identity(state, name):
    version = state.get(name + "_version")
    if version is None:
        return None
    value = {"version": _count(version), "sha256": None}
    token = state.get(name + "_sha256")
    if token is not None:
        if type(token) is not str or not re.fullmatch(r"sha256:[a-f0-9]{64}", token):
            raise ValueError("model digest")
        value["sha256"] = token
    return value


def _ordinary(path):
    for item in (path, *path.parents):
        info = item.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("aliased path")
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not 0 < info.st_size <= 1024**3:
        raise ValueError("database file")
    return info.st_dev, info.st_ino


def read_whitebit_live_learning_status(state_dir: Path, *, now=None, max_age_seconds=180):
    """Return a whitelist projection, with no SQLite creation or runtime activation.

    ``episodes`` is the active bounded cycle count. Legacy total/invalid counts
    are intentionally absent (null), since obtaining them would scan memory.
    STALE preserves validated last-known state but does not imply a live worker.
    """
    result = dict(schema="autotrade-whitebit-live-learning-status-v1", status="MISSING",
        reason_codes=["LEARNING_MEMORY_MISSING"], phase=None, market=None, cycle=None,
        trials=None, max_trials=None, episodes=None, invalid_episodes=None,
        pending_decisions=None, model_version_count=None, last_available_at=None,
        incumbent_model=None, candidate_model=None, analysis=None, evaluation=None,
        mode="ZERO", public_data_only=True, network_orders=False, trading_authority=False)
    try:
        path = Path(state_dir).absolute() / "episode-memory.sqlite3"
        identity = _ordinary(path)
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None or type(max_age_seconds) is not int or not 1 <= max_age_seconds <= 86400:
            raise ValueError("clock")
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0.05)) as con:
            try:
                con.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, _MAX_STATE_BYTES)
                con.execute("PRAGMA query_only=ON")
                con.execute("PRAGMA trusted_schema=OFF")
                budget = [0]
                def bounded():
                    budget[0] += 1
                    return int(budget[0] > 100)
                con.set_progress_handler(bounded, 1000)
                con.execute("BEGIN")
                schemas = con.execute("SELECT name,type,sql FROM sqlite_schema WHERE name IN ('live_cycle','observations','models')").fetchall()
                if len(schemas) != 3 or any(kind != "table" or "VIRTUAL" in sql.upper() for _,kind,sql in schemas):
                    raise ValueError("source schema")
                row = con.execute("SELECT CASE WHEN length(CAST(state AS BLOB))<=? THEN state END FROM live_cycle WHERE id=1", (_MAX_STATE_BYTES,)).fetchone()
                if row is None or type(row[0]) is not str:
                    raise ValueError("state size")
                state = json.loads(row[0], object_pairs_hook=_object, parse_constant=_bad_constant)
                for name in ("incumbent", "candidate"):
                    version = state.get(name + "_version")
                    if version is not None:
                        model = con.execute("SELECT model_sha256 FROM models WHERE version=?", (_count(version),)).fetchone()
                        if model is None:
                            raise ValueError("missing model")
                        state[name + "_sha256"] = model[0]
                latest = con.execute("SELECT available_at FROM observations ORDER BY available_at DESC LIMIT 1").fetchone()
                con.rollback()
            finally:
                con.close()
        if _ordinary(path) != identity:
            raise ValueError("source replaced")
        if type(state) is not dict or state.get("schema") != "autotrade-live-learning-cycle-v1":
            raise ValueError("state schema")
        if any(state.get(field) is not False for field in ("network_orders", "trading_authority", "champion_promotion")):
            raise ValueError("authority")
        if state.get("phase") not in _PHASES:
            raise ValueError("phase")
        market = state.get("market")
        if market is not None and (type(market) is not str or not re.fullmatch(r"[A-Z0-9]{1,15}_[A-Z0-9]{1,15}", market)):
            raise ValueError("market")
        projection = dict(phase=state["phase"], market=market,
            cycle=_count(state["cycle"]), trials=_count(state["trials"]),
            episodes=_count(state["episodes"]), max_trials=_count(state["config"]["trials"]),
            incumbent_model=_identity(state, "incumbent"), candidate_model=_identity(state, "candidate"))
        analysis = state.get("analysis")
        if analysis is not None:
            projection["analysis"] = {key: _money(analysis[key]) for key in
                ("gross_usdt", "fees_usdt", "net_usdt", "spread_cost_usdt", "slippage_cost_usdt") if key in analysis}
            projection["analysis"].update({key: _count(analysis[key]) for key in
                ("episodes", "correct_direction_but_loss", "legacy_missing_cost_attribution") if key in analysis})
        evaluation = state.get("evaluation")
        if evaluation is not None:
            projection["evaluation"] = {key: _money(evaluation[key]) for key in
                ("candidate", "incumbent", "drawdown", "no_trade") if key in evaluation}
            projection["evaluation"].update({key: _count(evaluation[key]) for key in
                ("count", "candidate_trades", "incumbent_trades", "invalid_gap_count") if key in evaluation})
            qualified = evaluation.get("qualifies_for_research_simulation")
            if qualified is not None and type(qualified) is not bool:
                raise ValueError("evaluation gate")
            projection["evaluation"]["qualifies_for_research_simulation"] = qualified
            projection["evaluation"]["profitability_proven"] = False
        age = None
        if latest:
            timestamp = _time(latest[0])
            age = (current-timestamp).total_seconds()
            if age < -5:
                raise ValueError("future observation")
            projection["last_available_at"] = timestamp.isoformat().replace("+00:00", "Z")
        result.update(projection)
        result.update(status="STALE" if age is None or age > max_age_seconds else "READY",
            reason_codes=["LEARNING_OBSERVATION_STALE"] if age is None or age > max_age_seconds else [])
    except FileNotFoundError:
        pass
    except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError, sqlite3.Error):
        # Reconstruct defaults so a partial validated projection never escapes.
        result.update(status="CORRUPT", reason_codes=["LEARNING_MEMORY_UNAVAILABLE"])
    return result
