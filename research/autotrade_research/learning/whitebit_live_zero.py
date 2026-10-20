"""Restart-safe online research over authenticated WhiteBIT public BBO cuts.

This module has no network, credential, provider-account or order route.  It
turns already-authenticated public observations into one-step ZERO research
episodes with explicit hypothetical fees/slippage.  Training and evaluation
episodes are disjoint; evaluation observations never update the model.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from typing import Any
from .live_cycle import LiveCycle


SCHEMA = "autotrade-whitebit-live-zero-learning-v1"
SNAPSHOT_SCHEMA = "autotrade-whitebit-public-supervisor-v1"
_SNAPSHOT_FIELDS = frozenset({
    "schema", "market", "venue", "bid_price", "bid_quantity",
    "ask_price", "ask_quantity", "source_event_at", "available_at",
    "published_at", "update_id", "stream_generation", "raw_evidence_ref",
    "public_data_only", "trading_authority",
})
_EVIDENCE_FIELDS = frozenset({
    "artifact_id", "sha256", "source_uri", "observed_at", "rights_id",
})


def _canonical(value: object) -> bytes:
    return (json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    ) + "\n").encode("utf-8")


def _decimal(value: object, *, name: str, positive: bool = False) -> Decimal:
    if isinstance(value, (bool, float)):
        raise TypeError(f"{name} must use exact decimal text")
    try:
        result = Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a finite decimal") from exc
    if not result.is_finite() or (positive and result <= 0):
        raise ValueError(f"{name} is outside its exact range")
    return result


def _instant(value: object, *, name: str) -> datetime:
    if type(value) is not str:
        raise TypeError(f"{name} must be exact text")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _digest(value: object) -> str:
    return "sha256:" + sha256(_canonical(value)).hexdigest()


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = _canonical(value)
    descriptor, temporary = tempfile.mkstemp(prefix=".whitebit-live-zero-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def validate_public_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Return a canonical public-only observation or fail before mutation."""
    if type(snapshot) is not dict or frozenset(snapshot) != _SNAPSHOT_FIELDS:
        raise ValueError("WhiteBIT public snapshot schema differs")
    if (snapshot["schema"] != SNAPSHOT_SCHEMA
            or snapshot["venue"] != "WHITEBIT_SPOT"
            or snapshot["public_data_only"] is not True
            or snapshot["trading_authority"] is not False
            or type(snapshot["market"]) is not str
            or not snapshot["market"]):
        raise ValueError("WhiteBIT public snapshot authority differs")
    if type(snapshot["update_id"]) not in {str, int}:
        raise TypeError("WhiteBIT update identity must be exact text or integer")
    if (type(snapshot["stream_generation"]) is not int
            or snapshot["stream_generation"] < 1):
        raise ValueError("WhiteBIT stream generation is invalid")
    bid = _decimal(snapshot["bid_price"], name="bid_price", positive=True)
    ask = _decimal(snapshot["ask_price"], name="ask_price", positive=True)
    _decimal(snapshot["bid_quantity"], name="bid_quantity", positive=True)
    _decimal(snapshot["ask_quantity"], name="ask_quantity", positive=True)
    if bid > ask:
        raise ValueError("WhiteBIT BBO is crossed")
    source = _instant(snapshot["source_event_at"], name="source_event_at")
    available = _instant(snapshot["available_at"], name="available_at")
    published = _instant(snapshot["published_at"], name="published_at")
    if not source <= available <= published:
        raise ValueError("WhiteBIT public availability chronology differs")
    evidence = snapshot["raw_evidence_ref"]
    if type(evidence) is not dict or frozenset(evidence) != _EVIDENCE_FIELDS:
        raise ValueError("WhiteBIT public evidence schema differs")
    if (evidence["source_uri"] != "wss://wss.whitebit.com/ws#bookTicker"
            or evidence["rights_id"] != "whitebit-public-websocket-internal-v1"
            or evidence["observed_at"] != available.isoformat().replace("+00:00", "Z")
            or type(evidence["sha256"]) is not str
            or not evidence["sha256"].startswith("sha256:")
            or len(evidence["sha256"]) != 71):
        raise ValueError("WhiteBIT public evidence identity differs")
    return json.loads(json.dumps(snapshot, sort_keys=True))


class WhiteBitLiveZeroLearning(LiveCycle):
    """One durable authority for observations, episodes and model versions."""

    def __init__(
        self,
        state_dir: Path,
        *,
        virtual_notional: str = "100",
        fee_bps_per_leg: str = "10",
        slippage_bps_per_leg: str = "5",
        evaluation_every: int = 5,
        cycle_episodes: int = 20,
        cycle_seconds: int = 1200,
        evaluation_episodes: int = 12,
        max_drawdown_usdt: str = "2",
        cooldown_seconds: int = 3600,
        max_trials: int = 3,
        max_observation_gap_seconds: int = 180,
        min_evaluation_trades: int = 3,
    ) -> None:
        if type(state_dir) is not type(Path()):
            raise TypeError("state_dir must be an exact Path")
        if state_dir.exists() and (state_dir.is_symlink() or not state_dir.is_dir()):
            raise ValueError("live research state must be an ordinary directory")
        if (type(evaluation_every) is not int or isinstance(evaluation_every, bool)
                or evaluation_every < 2):
            raise ValueError("evaluation cadence must be at least two")
        self.state_dir = state_dir
        self.notional = _decimal(virtual_notional, name="virtual_notional", positive=True)
        self.fee_bps = _decimal(fee_bps_per_leg, name="fee_bps_per_leg")
        self.slippage_bps = _decimal(
            slippage_bps_per_leg, name="slippage_bps_per_leg"
        )
        if not Decimal("0") <= self.fee_bps <= Decimal("100"):
            raise ValueError("fee scenario is outside 0..100 bps")
        if not Decimal("0") <= self.slippage_bps <= Decimal("100"):
            raise ValueError("slippage scenario is outside 0..100 bps")
        self.evaluation_every = evaluation_every
        self._configure_cycle(cycle_episodes, cycle_seconds, evaluation_episodes,
                              max_drawdown_usdt, cooldown_seconds, max_trials,
                              max_observation_gap_seconds, min_evaluation_trades)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.database = self.state_dir / "episode-memory.sqlite3"
        self._initialize()
        self._publish_projections()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    @contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS observations(
                    observation_key TEXT PRIMARY KEY,
                    market TEXT NOT NULL,
                    update_id TEXT NOT NULL,
                    generation INTEGER NOT NULL,
                    available_at TEXT NOT NULL UNIQUE,
                    bid TEXT NOT NULL,
                    ask TEXT NOT NULL,
                    mid TEXT NOT NULL,
                    evidence_sha256 TEXT NOT NULL,
                    snapshot_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS models(
                    version INTEGER PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    parameter TEXT NOT NULL,
                    trained_episode_count INTEGER NOT NULL,
                    momentum_correct INTEGER NOT NULL,
                    contrarian_correct INTEGER NOT NULL,
                    model_sha256 TEXT NOT NULL UNIQUE,
                    model_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS decisions(
                    decision_sequence INTEGER PRIMARY KEY,
                    decision_id TEXT NOT NULL UNIQUE,
                    observation_key TEXT NOT NULL UNIQUE REFERENCES observations(observation_key),
                    split TEXT NOT NULL CHECK(split IN ('TRAIN','EVALUATION')),
                    model_version INTEGER NOT NULL REFERENCES models(version),
                    model_sha256 TEXT NOT NULL,
                    feature_momentum TEXT NOT NULL,
                    prediction TEXT NOT NULL CHECK(prediction IN ('UP','DOWN','FLAT')),
                    action TEXT NOT NULL CHECK(action IN ('SIMULATED_BUY','HOLD')),
                    outcome_observation_key TEXT UNIQUE REFERENCES observations(observation_key),
                    episode_id TEXT UNIQUE,
                    episode_json TEXT
                );
            """)
            if connection.execute("SELECT COUNT(*) FROM models").fetchone()[0] == 0:
                self._insert_model(
                    connection,
                    version=0,
                    created_at="1970-01-01T00:00:00+00:00",
                    parameter="MOMENTUM",
                    trained=0,
                    momentum_correct=0,
                    contrarian_correct=0,
                )
            connection.commit()
            self._initialize_cycle(connection)

    def _insert_model(self, connection: sqlite3.Connection, *, version: int,
                      created_at: str, parameter: str, trained: int,
                      momentum_correct: int, contrarian_correct: int) -> dict[str, Any]:
        payload = {
            "schema": SCHEMA,
            "model_version": version,
            "created_at": created_at,
            "strategy_family": parameter,
            "trained_episode_count": trained,
            "momentum_correct": momentum_correct,
            "contrarian_correct": contrarian_correct,
            "label_rule": "NEXT_AUTHENTICATED_PUBLIC_BBO_MID_DIRECTION",
            "training_split": "TRAIN_ONLY",
            "automatic_trading_promotion": False,
            "trading_authority": False,
        }
        model = {**payload, "model_sha256": _digest(payload)}
        connection.execute(
            "INSERT INTO models VALUES(?,?,?,?,?,?,?,?)",
            (version, created_at, parameter, trained, momentum_correct,
             contrarian_correct, model["model_sha256"], json.dumps(model, sort_keys=True)),
        )
        return model

    @staticmethod
    def _observation_key(snapshot: dict[str, Any]) -> str:
        return f"{snapshot['stream_generation']}:{snapshot['update_id']}"

    @staticmethod
    def _mid(bid: Decimal, ask: Decimal) -> Decimal:
        with localcontext() as context:
            context.prec = 42
            return (bid + ask) / Decimal("2")

    def _mature(self, connection: sqlite3.Connection, decision: sqlite3.Row,
                current: dict[str, Any], current_key: str) -> dict[str, Any]:
        source = connection.execute(
            "SELECT * FROM observations WHERE observation_key=?",
            (decision["observation_key"],),
        ).fetchone()
        if source is None:
            raise ValueError("pending decision lacks its public observation")
        entry_mid = _decimal(source["mid"], name="entry_mid", positive=True)
        exit_bid = _decimal(current["bid_price"], name="exit_bid", positive=True)
        exit_ask = _decimal(current["ask_price"], name="exit_ask", positive=True)
        exit_mid = self._mid(exit_bid, exit_ask)
        actual = "UP" if exit_mid > entry_mid else "DOWN" if exit_mid < entry_mid else "FLAT"
        correct = decision["prediction"] == actual
        if decision["action"] == "SIMULATED_BUY":
            quantity, gross, fees, net = self._buy_outcome(source["ask"], current["bid_price"])
        else:
            quantity = gross = fees = net = Decimal("0")
        with localcontext() as context:
            context.prec = 42
            spread_cost = quantity * (Decimal(source["ask"]) - entry_mid + exit_mid - exit_bid)
            slippage_cost = quantity * (Decimal(source["ask"]) + exit_bid) * self.slippage_bps / 10000
        episode_payload = {
            "schema": SCHEMA,
            "decision_id": decision["decision_id"],
            "decision_available_at": source["available_at"],
            "outcome_available_at": current["available_at"],
            "split": decision["split"],
            "model_version": decision["model_version"],
            "model_sha256": decision["model_sha256"],
            "feature_momentum": decision["feature_momentum"],
            "prediction": decision["prediction"],
            "actual_direction": actual,
            "correct": correct,
            "action": decision["action"],
            "virtual_notional_usdt": format(self.notional, "f"),
            "virtual_quantity": format(quantity, "f"),
            "gross_outcome_usdt": format(gross, "f"),
            "fees_usdt": format(fees, "f"),
            "spread_cost_usdt": format(spread_cost, "f"),
            "slippage_cost_usdt": format(slippage_cost, "f"),
            "slippage_bps_per_leg": format(self.slippage_bps, "f"),
            "net_outcome_usdt": format(net, "f"),
            "cost_scope": "HYPOTHETICAL_CONSERVATIVE_NOT_ACCOUNT_FEE",
            "source_evidence_sha256": source["evidence_sha256"],
            "outcome_evidence_sha256": current["raw_evidence_ref"]["sha256"],
            "environment": "SIMULATION",
            "mode": "ZERO",
            "network_orders": False,
            "trading_authority": False,
        }
        episode_id = _digest(episode_payload)
        connection.execute(
            "UPDATE decisions SET outcome_observation_key=?,episode_id=?,episode_json=? "
            "WHERE decision_sequence=? AND outcome_observation_key IS NULL",
            (current_key, episode_id, json.dumps(episode_payload, sort_keys=True),
             decision["decision_sequence"]),
        )
        return episode_payload

    def process_snapshot(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        canonical = validate_public_snapshot(snapshot)
        key = self._observation_key(canonical)
        result: dict[str, Any]
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cycle_state = self._load_cycle(connection)
            if cycle_state.get("market") not in (None, canonical["market"]):
                raise ValueError("live learning market differs from bound market")
            if cycle_state.get("market") is None:
                cycle_state["market"] = canonical["market"]
                self._save_cycle(connection, cycle_state)
            if connection.execute(
                "SELECT 1 FROM observations WHERE observation_key=?", (key,)
            ).fetchone() is not None:
                connection.rollback()
                return {"status": "DUPLICATE", "observation_key": key}
            previous = connection.execute(
                "SELECT * FROM observations ORDER BY available_at DESC LIMIT 1"
            ).fetchone()
            if previous is not None and _instant(
                canonical["available_at"], name="available_at"
            ) <= _instant(previous["available_at"], name="previous_available_at"):
                connection.rollback()
                raise ValueError("WhiteBIT live observation is stale or reordered")
            bid = _decimal(canonical["bid_price"], name="bid", positive=True)
            ask = _decimal(canonical["ask_price"], name="ask", positive=True)
            current_mid = self._mid(bid, ask)
            connection.execute(
                "INSERT INTO observations VALUES(?,?,?,?,?,?,?,?,?,?)",
                (key, canonical["market"], str(canonical["update_id"]),
                 canonical["stream_generation"], canonical["available_at"],
                 canonical["bid_price"], canonical["ask_price"], format(current_mid, "f"),
                 canonical["raw_evidence_ref"]["sha256"],
                 json.dumps(canonical, sort_keys=True)),
            )
            pending = connection.execute(
                "SELECT * FROM decisions WHERE outcome_observation_key IS NULL"
            ).fetchall()
            if len(pending) > 1:
                connection.rollback()
                raise ValueError("live research has multiple unresolved decisions")
            gap = previous is not None and (
                _instant(canonical["available_at"], name="available_at") -
                _instant(previous["available_at"], name="previous_available_at")
            ).total_seconds() > self.cycle_config["max_observation_gap_seconds"]
            episode = None
            if pending:
                if gap:
                    invalid = dict(status="INVALID_OBSERVATION_GAP", training_eligible=False,
                        decision_id=pending[0]["decision_id"], outcome_key=key,
                        available_at=canonical["available_at"], split=pending[0]["split"])
                    connection.execute("UPDATE decisions SET outcome_observation_key=?, "
                        "episode_id=?,episode_json=? WHERE decision_sequence=?",
                        (key, _digest(invalid), json.dumps(invalid, sort_keys=True), pending[0]["decision_sequence"]))
                else:
                    episode = self._mature(connection, pending[0], canonical, key)
            cycle = self._advance_cycle(connection, canonical, key, episode)
            decision_payload = None
            if previous is not None and not gap and cycle["phase"] in {"RUNNING", "RESEARCH"}:
                feature = current_mid - _decimal(previous["mid"], name="previous_mid")
                model = connection.execute(
                    "SELECT * FROM models WHERE version=?", (cycle["incumbent_version"],)
                ).fetchone()
                prediction = (
                    "UP" if feature > 0 else "DOWN" if feature < 0 else "FLAT"
                )
                if model["parameter"].startswith("CONTRARIAN"):
                    prediction = (
                        "DOWN" if prediction == "UP" else
                        "UP" if prediction == "DOWN" else "FLAT"
                    )
                sequence = connection.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] + 1
                split = "EVALUATION" if sequence % self.evaluation_every == 0 else "TRAIN"
                decision_payload = {
                    "schema": SCHEMA,
                    "decision_sequence": sequence,
                    "observation_key": key,
                    "available_at": canonical["available_at"],
                    "split": split,
                    "model_version": model["version"],
                    "model_sha256": model["model_sha256"],
                    "feature_momentum": format(feature, "f"),
                    "prediction": prediction,
                    "action": "SIMULATED_BUY" if cycle["phase"] == "RUNNING" and
                        self._cycle_action(model["parameter"], feature, canonical) else "HOLD",
                    "environment": "SIMULATION",
                    "mode": "ZERO",
                    "network_orders": False,
                    "trading_authority": False,
                }
                decision_id = _digest(decision_payload)
                connection.execute(
                    "INSERT INTO decisions(decision_sequence,decision_id,observation_key,"
                    "split,model_version,model_sha256,feature_momentum,prediction,action) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (sequence, decision_id, key, split, model["version"],
                     model["model_sha256"], format(feature, "f"), prediction,
                     decision_payload["action"]),
                )
                decision_payload["decision_id"] = decision_id
            connection.commit()
            result = {
                "status": "PROCESSED",
                "observation_key": key,
                "matured_episode": episode,
                "new_decision": decision_payload,
            }
        self._publish_projections()
        return result

    def _publish_projections(self) -> None:
        with self._connection() as connection:
            model_row = connection.execute(
                "SELECT model_json FROM models ORDER BY version DESC LIMIT 1"
            ).fetchone()
            latest = connection.execute(
                "SELECT observation_key,available_at,evidence_sha256 FROM observations "
                "ORDER BY available_at DESC LIMIT 1"
            ).fetchone()
            pending = connection.execute(
                "SELECT decision_id FROM decisions WHERE outcome_observation_key IS NULL"
            ).fetchall()
            episodes = connection.execute(
                "SELECT split,episode_json FROM decisions WHERE episode_json IS NOT NULL "
                "ORDER BY decision_sequence"
            ).fetchall()
            model_count = connection.execute("SELECT COUNT(*) FROM models").fetchone()[0]
            cycle = self._load_cycle(connection)
            incumbent = json.loads(connection.execute(
                "SELECT model_json FROM models WHERE version=?",
                (cycle["incumbent_version"],)).fetchone()[0])
        model = json.loads(model_row["model_json"])
        evaluation = [json.loads(row["episode_json"]) for row in episodes
                      if row["split"] == "EVALUATION" and
                      json.loads(row["episode_json"]).get("training_eligible") is not False]
        correct = sum(item["correct"] for item in evaluation)
        net = sum((_decimal(item["net_outcome_usdt"], name="net") for item in evaluation),
                  Decimal("0"))
        evaluation_report = {
            "schema": SCHEMA,
            "status": "COMPLETE" if len(evaluation) >= 5 else "INSUFFICIENT_SAMPLE",
            "split": "EVALUATION_ONLY_NO_MODEL_UPDATE",
            "episode_count": len(evaluation),
            "correct_count": correct,
            "accuracy_fraction": (
                format(Decimal(correct) / Decimal(len(evaluation)), "f")
                if evaluation else None
            ),
            "net_outcome_after_costs_usdt": format(net, "f"),
            "profitability_proven": False,
            "trading_authority": False,
        }
        checkpoint = {
            "schema": SCHEMA,
            "last_observation_key": None if latest is None else latest["observation_key"],
            "last_available_at": None if latest is None else latest["available_at"],
            "last_evidence_sha256": None if latest is None else latest["evidence_sha256"],
            "episode_count": len(episodes),
            "invalid_episode_count": sum(json.loads(row["episode_json"]).get(
                "training_eligible") is False for row in episodes),
            "pending_decision_ids": [row["decision_id"] for row in pending],
            "model_version_count": model_count,
            "latest_model_sha256": model["model_sha256"],
            "mode": "ZERO",
            "public_data_only": True,
            "network_orders": False,
            "trading_authority": False,
            "learning_cycle": cycle,
        }
        _write_json(self.state_dir / "checkpoint.json", checkpoint)
        _write_json(self.state_dir / "learning-cycle.json", cycle)
        _write_json(self.state_dir / "latest-model.json", model)
        _write_json(self.state_dir / "incumbent-model.json", incumbent)
        _write_json(self.state_dir / "prospective-shadow-evaluation.json", {
            "schema": "autotrade-live-shadow-evaluation-v1",
            "phase": cycle["phase"], "cycle": cycle["cycle"],
            "candidate_version": cycle["candidate_version"],
            "evaluation": cycle.get("evaluation"),
            "active_simulated_trades_during_evaluation": False,
            "profitability_proven": False, "champion_promotion": False,
            "network_orders": False,
        })
        _write_json(self.state_dir / "independent-evaluation.json", evaluation_report)

    def status(self) -> dict[str, Any]:
        return json.loads((self.state_dir / "checkpoint.json").read_text(encoding="utf-8"))
