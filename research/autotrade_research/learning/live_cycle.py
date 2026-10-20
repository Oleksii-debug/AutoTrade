"""Bounded public-data research lifecycle; never a ChampionRegistry authority.

The parent SQLite transaction commits observations, decisions and this state
together. JSON files are rebuildable projections. Evaluation is prospective,
paired shadow scoring using the parent's existing one-step cost model.
"""
from datetime import datetime
from decimal import Decimal, localcontext
from hashlib import sha256
import json

from .waves import LearningWavePolicy, MarketWaveSnapshot, evaluate_pause


def digest(value):
    return "sha256:" + sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


class LiveCycle:
    def _configure_cycle(self, episodes, seconds, evaluation, drawdown, cooldown, trials,
                         max_gap, min_trades):
        for value in (episodes, seconds, evaluation, cooldown, trials, max_gap, min_trades):
            if type(value) is not int or value < 1:
                raise ValueError("cycle bounds must be positive integers")
        dd = Decimal(drawdown)
        if not dd.is_finite() or dd <= 0:
            raise ValueError("cycle drawdown must be finite and positive")
        self.cycle_config = dict(episodes=episodes, seconds=seconds,
            evaluation=evaluation, drawdown=str(dd), cooldown=cooldown, trials=trials,
            notional=str(self.notional), fee_bps=str(self.fee_bps),
            slippage_bps=str(self.slippage_bps), evaluation_every=self.evaluation_every)
        self.cycle_config.update(max_observation_gap_seconds=max_gap,
                                 min_evaluation_trades=min_trades)

    def _initialize_cycle(self, con):
        con.executescript("""
            CREATE TABLE IF NOT EXISTS live_cycle(id INTEGER PRIMARY KEY CHECK(id=1), state TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS live_cycle_events(
                sequence INTEGER PRIMARY KEY, state TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS live_shadow_episodes(
                sequence INTEGER PRIMARY KEY, cycle INTEGER NOT NULL,
                evidence TEXT NOT NULL);
        """)
        row = con.execute("SELECT state FROM live_cycle WHERE id=1").fetchone()
        if row:
            if json.loads(row[0])["config"] != self.cycle_config:
                raise ValueError("persisted learning cycle configuration differs")
            return
        count = con.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
        version = con.execute("SELECT MAX(version) FROM models").fetchone()[0]
        markets = con.execute("SELECT DISTINCT market FROM observations").fetchall()
        if len(markets) > 1:
            raise ValueError("legacy live memory contains multiple markets")
        state = dict(schema="autotrade-live-learning-cycle-v1", config=self.cycle_config,
            market=markets[0][0] if markets else None,
            phase="PAUSED" if count else "RUNNING", cycle=1, trials=0,
            incumbent_version=version, candidate_version=None, start_sequence=0,
            started_at=None, net="0", peak="0", drawdown="0", episodes=0,
            reason="LEGACY_MEMORY_REVIEW" if count else "BOUNDED_BOOTSTRAP",
            network_orders=False, champion_promotion=False, trading_authority=False)
        self._save_cycle(con, state, event=True)
        con.commit()

    @staticmethod
    def _load_cycle(con):
        return json.loads(con.execute("SELECT state FROM live_cycle WHERE id=1").fetchone()[0])

    @staticmethod
    def _save_cycle(con, state, *, event=False):
        body = json.dumps(state, sort_keys=True)
        con.execute("INSERT OR REPLACE INTO live_cycle VALUES(1,?)", (body,))
        if event:
            con.execute("INSERT INTO live_cycle_events(state) VALUES(?)", (body,))

    def _buy_outcome(self, ask, bid):
        # Shared by recorded episodes, TRAIN counterfactuals and paired shadow eval.
        with localcontext() as context:
            context.prec = 42
            entry = Decimal(ask) * (1 + self.slippage_bps / 10000)
            exit_price = Decimal(bid) * (1 - self.slippage_bps / 10000)
            quantity = self.notional / entry
            gross = quantity * (exit_price - entry)
            fees = (self.notional + quantity * exit_price) * self.fee_bps / 10000
            return quantity, gross, fees, gross - fees

    def _cycle_action(self, parameter, feature, snapshot):
        signed = -feature if parameter.startswith("CONTRARIAN") else feature
        if not parameter.endswith("COST_HURDLE"):
            return signed > 0
        # Price movement is merely a preregistered signal, not expected profit.
        # Exact break-even next bid includes both fees and both slippage legs.
        with localcontext() as context:
            context.prec = 42
            fee = self.fee_bps / 10000
            slip = self.slippage_bps / 10000
            ask, bid = Decimal(snapshot["ask_price"]), Decimal(snapshot["bid_price"])
            required_bid = ask * (1 + slip) * (1 + fee) / ((1 - slip) * (1 - fee))
            return signed > required_bid - bid

    def _training_rows(self, con, cutoff):
        rows = con.execute("""SELECT d.*,o.ask,o.bid,x.bid AS exit_bid,
            o.available_at AS entry_at,x.available_at AS exit_at
            FROM decisions d JOIN observations o ON o.observation_key=d.observation_key
            JOIN observations x ON x.observation_key=d.outcome_observation_key
            WHERE d.split='TRAIN' AND d.episode_json IS NOT NULL
            AND d.decision_sequence<=? ORDER BY d.decision_sequence""", (cutoff,)).fetchall()
        return [r for r in rows if json.loads(r["episode_json"]).get("training_eligible") is not False
                and 0 < (datetime.fromisoformat(r["exit_at"])-datetime.fromisoformat(r["entry_at"])).total_seconds()
                <= self.cycle_config["max_observation_gap_seconds"]]

    def _advance_cycle(self, con, current, key, episode):
        state = self._load_cycle(con)
        old_phase = state["phase"]
        now = current["available_at"]
        if state["started_at"] is None:
            state["started_at"] = now
        if old_phase in {"RUNNING", "RESEARCH"}:
            if episode:
                net = Decimal(state["net"]) + Decimal(episode["net_outcome_usdt"])
                peak = max(Decimal(state["peak"]), net)
                state.update(net=str(net), peak=str(peak),
                    drawdown=str(max(Decimal(state["drawdown"]), peak-net)),
                    episodes=state["episodes"]+1)
            policy = LearningWavePolicy("live-zero-bounded-v1",
                max_market_seconds=self.cycle_config["seconds"],
                min_evidence_events=self.cycle_config["episodes"],
                max_drawdown=Decimal(self.cycle_config["drawdown"]))
            model = con.execute("SELECT model_sha256 FROM models WHERE version=?",
                                (state["incumbent_version"],)).fetchone()[0]
            pause = evaluate_pause(policy, MarketWaveSnapshot(
                wave_id=str(state["cycle"]), segment_id="public-zero",
                champion_artifact_hash=model, source_cut_hash=current["raw_evidence_ref"]["sha256"],
                segment_started_at=datetime.fromisoformat(state["started_at"]),
                observed_at=datetime.fromisoformat(now), trades_since_pause=state["episodes"],
                evidence_events_since_pause=state["episodes"],
                drawdown_since_pause=Decimal(state["drawdown"]),
                previous_regime_id="UNCLASSIFIED", current_regime_id="UNCLASSIFIED"))
            if pause.should_pause:
                state.update(phase="PAUSED", pause_reasons=list(pause.reasons),
                             pause_hash=pause.decision_hash)
        elif old_phase == "PAUSED":
            # Legacy pending decisions have matured before freezing this cutoff.
            cutoff = con.execute("SELECT COALESCE(MAX(decision_sequence),0) FROM decisions "
                                 "WHERE episode_json IS NOT NULL").fetchone()[0]
            state.update(phase="ANALYZING", training_cutoff=cutoff, paused_at=now)
        elif old_phase == "ANALYZING":
            rows = self._training_rows(con, state["training_cutoff"])
            episodes = [json.loads(r["episode_json"]) for r in rows]
            state["analysis"] = dict(scope="PRIOR_TRAIN_ONLY", episodes=len(rows),
                population_hash=digest([r["episode_id"] for r in rows]),
                gross_usdt=str(sum((Decimal(e["gross_outcome_usdt"]) for e in episodes), Decimal(0))),
                fees_usdt=str(sum((Decimal(e["fees_usdt"]) for e in episodes), Decimal(0))),
                spread_cost_usdt=str(sum((Decimal(e.get("spread_cost_usdt", "0")) for e in episodes), Decimal(0))),
                slippage_cost_usdt=str(sum((Decimal(e.get("slippage_cost_usdt", "0")) for e in episodes), Decimal(0))),
                legacy_missing_cost_attribution=sum("spread_cost_usdt" not in e for e in episodes),
                net_usdt=str(sum((Decimal(e["net_outcome_usdt"]) for e in episodes), Decimal(0))),
                correct_direction_but_loss=sum(e["correct"] and Decimal(e["net_outcome_usdt"]) < 0 for e in episodes),
                finding="Direction alone does not establish an after-cost edge; test cost-hurdle abstention prospectively.",
                limitation="Counterfactual replay is a hypothesis, not causal proof of missed profit.")
            state["phase"] = "TRAINING"
        elif old_phase == "TRAINING":
            rows = self._training_rows(con, state["training_cutoff"])
            if not rows or state["trials"] >= self.cycle_config["trials"]:
                state.update(phase="RESEARCH_ONLY", reason="INSUFFICIENT_TRAIN_OR_TRIAL_BUDGET")
            else:
                scores = {}
                for family in ("MOMENTUM_COST_HURDLE", "CONTRARIAN_COST_HURDLE"):
                    scores[family] = sum((self._buy_outcome(r["ask"], r["exit_bid"])[3]
                        for r in rows if self._cycle_action(family, Decimal(r["feature_momentum"]),
                            dict(ask_price=r["ask"], bid_price=r["bid"]))), Decimal(0))
                family = max(scores, key=scores.get)
                version = con.execute("SELECT MAX(version)+1 FROM models").fetchone()[0]
                self._insert_model(con, version=version, created_at=now, parameter=family,
                    trained=len(rows), momentum_correct=0, contrarian_correct=0)
                state.update(phase="EVALUATING", candidate_version=version, frozen_at=now,
                    candidate_sha256=con.execute("SELECT model_sha256 FROM models WHERE version=?", (version,)).fetchone()[0],
                    incumbent_sha256=con.execute("SELECT model_sha256 FROM models WHERE version=?", (state["incumbent_version"],)).fetchone()[0],
                    train_scores={k: str(v) for k,v in scores.items()}, trials=state["trials"]+1,
                    evaluation=dict(count=0, candidate="0", incumbent="0", peak="0",
                                    drawdown="0", no_trade="0", pending=None,
                                    candidate_trades=0, incumbent_trades=0, invalid_gap_count=0))
        elif old_phase == "EVALUATING":
            self._evaluate_shadow(con, state, current, key)
        elif old_phase == "COOLDOWN":
            if (datetime.fromisoformat(now)-datetime.fromisoformat(state["cooldown_at"])).total_seconds() >= self.cycle_config["cooldown"]:
                state.update(phase="RESEARCH" if state["trials"] < self.cycle_config["trials"] else "RESEARCH_ONLY",
                    started_at=now, episodes=0, net="0", peak="0", drawdown="0", cycle=state["cycle"]+1)
        self._save_cycle(con, state, event=old_phase != state["phase"])
        return state

    def _evaluate_shadow(self, con, state, current, key):
        evaluation = state["evaluation"]
        pending = evaluation["pending"]
        if pending and (datetime.fromisoformat(current["available_at"]) -
                        datetime.fromisoformat(pending["available_at"])).total_seconds() > self.cycle_config["max_observation_gap_seconds"]:
            con.execute("INSERT INTO live_shadow_episodes(cycle,evidence) VALUES(?,?)",
                (state["cycle"], json.dumps(dict(entry=pending, outcome_key=key,
                    status="INVALID_OBSERVATION_GAP", training_eligible=False), sort_keys=True)))
            evaluation.update(pending=None, invalid_gap_count=evaluation["invalid_gap_count"]+1)
            return
        if pending:
            outcome = self._buy_outcome(pending["ask"], current["bid_price"])[3]
            con.execute("INSERT INTO live_shadow_episodes(cycle,evidence) VALUES(?,?)",
                (state["cycle"], json.dumps(dict(entry=pending, outcome_key=key,
                    outcome_available_at=current["available_at"],
                    candidate_version=state["candidate_version"],
                    incumbent_version=state["incumbent_version"],
                    buy_net_usdt=str(outcome), shadow_only=True,
                    training_eligible=False), sort_keys=True)))
            for name in ("candidate", "incumbent"):
                evaluation[name] = str(Decimal(evaluation[name]) + (outcome if pending[name] else Decimal(0)))
                evaluation[name+"_trades"] += int(pending[name])
            total = Decimal(evaluation["candidate"])
            peak = max(Decimal(evaluation["peak"]), total)
            evaluation.update(count=evaluation["count"]+1, peak=str(peak),
                drawdown=str(max(Decimal(evaluation["drawdown"]), peak-total)), pending=None,
                last_outcome_key=key)
        remaining = self.cycle_config["evaluation"] - evaluation["count"]
        minimum_trades_unreachable = (
            self.cycle_config["min_evaluation_trades"]
            <= self.cycle_config["evaluation"]
            and evaluation["candidate_trades"] + remaining
            < self.cycle_config["min_evaluation_trades"]
        )
        if evaluation["count"] >= self.cycle_config["evaluation"] or minimum_trades_unreachable:
            qualifies = (not minimum_trades_unreachable and
                Decimal(evaluation["candidate"]) > 0 and
                evaluation["candidate_trades"] >= self.cycle_config["min_evaluation_trades"] and
                Decimal(evaluation["candidate"]) >= Decimal(evaluation["incumbent"]) and
                Decimal(evaluation["drawdown"]) < Decimal(self.cycle_config["drawdown"]))
            evaluation["qualifies_for_research_simulation"] = qualifies
            evaluation["profitability_proven"] = False
            if qualifies:
                state.update(phase="RUNNING", incumbent_version=state["candidate_version"],
                    started_at=current["available_at"], episodes=0, net="0", peak="0", drawdown="0",
                    cycle=state["cycle"]+1, reason="PROSPECTIVE_RESEARCH_GATE_PASSED")
            else:
                state.update(phase="COOLDOWN", cooldown_at=current["available_at"],
                    reason=("PROSPECTIVE_MINIMUM_TRADE_COUNT_UNREACHABLE"
                        if minimum_trades_unreachable else
                        "PROSPECTIVE_COST_OR_RISK_GATE_FAILED"))
            return
        previous = con.execute("SELECT mid,available_at FROM observations WHERE observation_key != ? "
                               "ORDER BY available_at DESC LIMIT 1", (key,)).fetchone()
        if previous and (datetime.fromisoformat(current["available_at"]) -
                         datetime.fromisoformat(previous["available_at"])).total_seconds() <= self.cycle_config["max_observation_gap_seconds"]:
            feature = self._mid(Decimal(current["bid_price"]), Decimal(current["ask_price"])) - Decimal(previous[0])
            pending = dict(ask=current["ask_price"], observation_key=key,
                           available_at=current["available_at"])
            for name in ("candidate", "incumbent"):
                parameter = con.execute("SELECT parameter FROM models WHERE version=?",
                    (state[name+"_version"],)).fetchone()[0]
                pending[name] = self._cycle_action(parameter, feature, current)
            evaluation["pending"] = pending
