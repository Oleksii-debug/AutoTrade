from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from zipfile import ZIP_DEFLATED, ZipFile

from tools.qualify_historical_e2e import qualify_workspace


SOURCE_SHA = "a" * 40
BUILD_SHA = "b" * 64
TRAIN_SHA = "1" * 64
VALIDATION_SHA = "2" * 64
HOLDOUT_SHA = "3" * 64


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8")


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def diagnostic_zip(path: Path, kind: str) -> None:
    if kind == "AGENT_REPORT":
        name = "agent-actions.jsonl"
        body = b'{"event_type":"SimulationAgentDecisionRecorded"}\n'
        specific = {"complete_agent_evidence": True, "agent_action_count": 1}
        scope = "LOCAL_SIMULATION"
    else:
        name = "technical-heartbeats.jsonl"
        body = b'{"sequence":1,"previous_sha256":"' + b"0" * 64 + b'"}\n'
        specific = {
            "financial_authority": False,
            "sample_count": 1,
            "retention": {"persistent_across_restart": True},
        }
        scope = "LOCAL_RUNTIME"
    manifest = {
        "schema_version": "1.0.0",
        "scope": scope,
        "report_kind": kind,
        "source_revision": SOURCE_SHA,
        "simulation_only": True,
        "members": {name: {"bytes": len(body), "sha256": sha256(body).hexdigest()}},
        **specific,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        archive.writestr(name, body)
        archive.writestr("manifest.json", json.dumps(manifest, sort_keys=True))


def complete_workspace(root: Path) -> None:
    model_payload = {
        "schema": "autotrade-causal-ma-training-v1",
        "strategy_id": "moving-average-causal-experts",
        "strategy_version": "1",
        "source_dataset_sha256": TRAIN_SHA,
        "symbol": "BTCUSDT",
        "interval": "1h",
        "training_start_open_utc": "2024-01-01T00:00:00+00:00",
        "training_last_close_utc": "2024-02-01T00:00:00+00:00",
        "label_rule": "NEXT_CLOSED_BAR_DIRECTION_KNOWN_ONLY_AT_ITS_CLOSE",
        "update_rule": "MULTIPLICATIVE_DIRECTIONAL_SCORE_ETA_0.05_V1",
        "candidates": [],
        "selected_parameters": {"fast": 2, "slow": 3},
        "economic_edge_status": "NOT_EVALUATED_USE_INDEPENDENT_HOLDOUT",
    }
    model = {**model_payload, "model_sha256": sha256(canonical(model_payload)).hexdigest()}
    write_json(root / "models" / "BTCUSDT-2024-01-2024-01.json", model)
    validation_payload = {
        "status": "INDEPENDENT_DIRECTIONAL_VALIDATION_NOT_TRADING_PERFORMANCE",
        "model_sha256": model["model_sha256"],
        "validation_dataset_sha256": VALIDATION_SHA,
        "symbol": "BTCUSDT",
        "interval": "1h",
        "validation_start_open_utc": "2024-02-01T00:00:00+00:00",
        "validation_last_close_utc": "2024-03-01T00:00:00+00:00",
        "selected_parameters": {"fast": 2, "slow": 3},
        "matured_labels": 10,
        "correct_labels": 5,
        "accuracy_fraction": "0.5",
        "model_updated": False,
    }
    validation = {
        **validation_payload,
        "report_sha256": sha256(canonical(validation_payload)).hexdigest(),
    }
    write_json(root / "validation" / "BTCUSDT-2024-02-2024-02-model-2024-01.json",
               validation)
    account_report = {
        "status": "COMPLETED",
        "completed_episodes": 10,
        "cash_usd": "900",
        "position": "0.001",
        "agent_mark_pnl_usd": "1",
        "agent_max_mark_drawdown_fraction": "0.01",
        "cash_baseline_equity_usd": "1000",
        "buy_hold_liquidation_scenario_pnl_usd": "2",
        "missed_fill_metrics": {"count": 0},
        "causal_regime_analysis": {"status": "INCONCLUSIVE"},
        "conservative_uncertainty": {"status": "INCONCLUSIVE"},
        "dataset_identity_sha256": HOLDOUT_SHA,
    }
    report = {
        "status": "INTERNAL_TWO_ACCOUNT_HISTORICAL_CUT_NOT_PRODUCT_QUALIFICATION",
        "symbol": "BTCUSDT",
        "interval": "1h",
        "periods": {
            "training": ["2024-01", "2024-01"],
            "validation": ["2024-02", "2024-02"],
            "holdout": ["2024-03", "2024-03"],
        },
        "model_sha256": model["model_sha256"],
        "selected_strategy_parameters": {"fast": 2, "slow": 3},
        "matured_training_labels": 10,
        "validation_report_sha256": validation["report_sha256"],
        "accounts": {"200": dict(account_report), "1000": dict(account_report)},
    }
    write_json(root / "reports" / "BTCUSDT-2024-03-2024-03-full.json", report)
    immutable = {
        path.relative_to(root).as_posix(): sha256(path.read_bytes()).hexdigest()
        for directory in ("models", "validation", "reports")
        for path in (root / directory).glob("*.json")
    }
    write_json(root / "qualification" / "identity.json", {
        "schema": "autotrade-historical-e2e-identity-v1",
        "source_sha": SOURCE_SHA,
        "build_sha256": BUILD_SHA,
        "mode": "ZERO",
        "provider_credentials": "NONE",
        "real_order_route": "UNAVAILABLE",
        "network_orders": False,
        "immutable_artifacts": immutable,
    })

    restart_rows = {}
    for budget in ("200", "1000"):
        relative = f"accounts/BTCUSDT/2024-03-2024-03/usd-{budget}"
        account = root / Path(relative)
        checkpoint = {
            "schema_version": "4.0.0",
            "build_sha": BUILD_SHA,
            "fingerprint": "4" * 64,
            "protocol_ref": "autonomous-simulation:sha256:" + "5" * 64,
            "replay": {"clock": "2024-03-01T10:00:00Z", "cursor": 10,
                       "dataset_digest": "6" * 64},
            "runtime_components": {
                "execution_state": "7" * 64,
                "portfolio_accounting_state": "8" * 64,
                "provider_state": "9" * 64,
                "strategy_state": "c" * 64,
            },
        }
        checkpoint_path = account / "autonomous-runtime-checkpoint.json"
        write_json(checkpoint_path, checkpoint)
        journal_path = account / "journal.sqlite3"
        connection = sqlite3.connect(journal_path)
        try:
            connection.execute(
                "CREATE TABLE events(event_type TEXT NOT NULL, "
                "journal_sequence INTEGER NOT NULL, payload_json TEXT NOT NULL)")
            event_types = (
                "SimulationAgentDecisionRecorded",
                "RiskDecisionRecorded",
                "AutonomousEpisodeFillObserved",
                "AutonomousEpisodeCompleted",
                "AccountReconciled",
            )
            connection.executemany(
                "INSERT INTO events(event_type,journal_sequence,payload_json) VALUES(?,?,?)",
                [(event, index, "{}") for index, event in enumerate(event_types, 1)],
            )
            connection.commit()
        finally:
            connection.close()
        digest = sha256(checkpoint_path.read_bytes()).hexdigest()
        restart_rows[relative] = {
            "checkpoint_sha256_before": digest,
            "checkpoint_sha256_after": digest,
            "journal_sequence_before": 5,
            "journal_sequence_after": 5,
            "recovered_without_duplicate_effects": True,
        }
    write_json(root / "qualification" / "restart-evidence.json", {
        "schema": "autotrade-historical-e2e-restart-v1",
        "status": "PASS",
        "source_sha": SOURCE_SHA,
        "build_sha256": BUILD_SHA,
        "accounts": restart_rows,
    })
    diagnostic_zip(root / "diagnostics" / "autotrade-agent-report.zip", "AGENT_REPORT")
    diagnostic_zip(root / "diagnostics" / "autotrade-technical-diagnostics.zip",
                   "TECHNICAL_DIAGNOSTICS")


class HistoricalE2EQualificationTests(unittest.TestCase):
    def qualify(self, root: Path):
        return qualify_workspace(root, source_sha=SOURCE_SHA, build_sha256=BUILD_SHA)

    def test_complete_workspace_passes_without_network_or_campaign_execution(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            complete_workspace(root)
            result = self.qualify(root)
            self.assertEqual(result["status"], "PASS")
            self.assertTrue(all(row["status"] == "PASS" for row in result["checks"]))
            self.assertEqual(result["campaign_report_count"], 1)
            self.assertEqual(result["account_workspace_count"], 2)

    def test_absent_components_are_incomplete_not_fabricated_pass(self):
        with TemporaryDirectory() as temporary:
            result = self.qualify(Path(temporary))
            self.assertEqual(result["status"], "INCOMPLETE")
            self.assertIn("INCOMPLETE", {row["status"] for row in result["checks"]})
            self.assertNotIn("FAIL", {row["status"] for row in result["checks"]})

    def test_source_identity_mismatch_fails_closed(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            complete_workspace(root)
            identity = json.loads((root / "qualification" / "identity.json").read_text())
            identity["source_sha"] = "d" * 40
            write_json(root / "qualification" / "identity.json", identity)
            result = self.qualify(root)
            self.assertEqual(result["status"], "FAIL")
            self.assertEqual(next(row for row in result["checks"]
                                  if row["id"] == "exact_identity")["status"], "FAIL")

    def test_reused_holdout_digest_is_a_dataset_separation_failure(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            complete_workspace(root)
            report_path = root / "reports" / "BTCUSDT-2024-03-2024-03-full.json"
            report = json.loads(report_path.read_text())
            for account in report["accounts"].values():
                account["dataset_identity_sha256"] = TRAIN_SHA
            write_json(report_path, report)
            result = self.qualify(root)
            self.assertEqual(result["status"], "FAIL")
            self.assertEqual(next(row for row in result["checks"]
                                  if row["id"] == "dataset_separation")["status"], "FAIL")

    def test_missing_dual_diagnostics_keeps_other_valid_evidence_incomplete(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            complete_workspace(root)
            (root / "diagnostics" / "autotrade-agent-report.zip").unlink()
            result = self.qualify(root)
            self.assertEqual(result["status"], "INCOMPLETE")
            self.assertEqual(next(row for row in result["checks"]
                                  if row["id"] == "dual_diagnostics")["status"], "INCOMPLETE")

    def test_explicit_bounded_prefix_is_complete_without_full_month_claim(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            complete_workspace(root)
            report_path = root / "reports" / "BTCUSDT-2024-03-2024-03-full.json"
            report = json.loads(report_path.read_text())
            report["qualification_cut"] = {
                "scope": "BOUNDED_CAUSAL_PREFIX_NOT_FULL_MONTH_PERFORMANCE",
                "episode_limit": 12,
                "all_accounts_reached_limit": True,
            }
            for account in report["accounts"].values():
                account.update({
                    "status": "BOUNDED_CAUSAL_PREFIX_COMPLETED_NOT_FULL_MONTH",
                    "completed_episodes": 12,
                    "runtime_status": "PAUSED",
                })
            write_json(report_path, report)
            identity_path = root / "qualification" / "identity.json"
            identity = json.loads(identity_path.read_text())
            relative = report_path.relative_to(root).as_posix()
            identity["immutable_artifacts"][relative] = sha256(
                report_path.read_bytes()
            ).hexdigest()
            write_json(identity_path, identity)

            result = self.qualify(root)
            self.assertEqual(result["status"], "PASS")
            summary = next(
                row["summary"] for row in result["checks"]
                if row["id"] == "campaign_reports"
            )
            self.assertIn("not a full-month performance claim", summary)

    def test_secret_in_public_log_fails_without_echoing_secret(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            complete_workspace(root)
            secret = "do-not-print-this-secret-value"
            (root / "campaign.log").write_text("api_key=" + secret, encoding="utf-8")
            result = self.qualify(root)
            self.assertEqual(result["status"], "FAIL")
            encoded = json.dumps(result)
            self.assertNotIn(secret, encoded)
            self.assertEqual(next(row for row in result["checks"]
                                  if row["id"] == "secret_leakage")["status"], "FAIL")

    def test_stale_restart_checkpoint_digest_fails(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            complete_workspace(root)
            evidence_path = root / "qualification" / "restart-evidence.json"
            evidence = json.loads(evidence_path.read_text())
            first = next(iter(evidence["accounts"].values()))
            first["checkpoint_sha256_after"] = "f" * 64
            write_json(evidence_path, evidence)
            result = self.qualify(root)
            self.assertEqual(result["status"], "FAIL")
            self.assertEqual(next(row for row in result["checks"]
                                  if row["id"] == "restart_recovery")["status"], "FAIL")

    def test_real_route_marker_in_durable_journal_fails(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            complete_workspace(root)
            journal = (root / "accounts" / "BTCUSDT" / "2024-03-2024-03" /
                       "usd-1000" / "journal.sqlite3")
            connection = sqlite3.connect(journal)
            try:
                connection.execute(
                    "UPDATE events SET payload_json=? WHERE journal_sequence=1",
                    ('{"environment":"LIVE","api_key":"NONE"}',),
                )
                connection.commit()
            finally:
                connection.close()
            result = self.qualify(root)
            self.assertEqual(result["status"], "FAIL")
            self.assertEqual(next(row for row in result["checks"]
                                  if row["id"] == "financial_journals")["status"], "FAIL")


if __name__ == "__main__":
    unittest.main()
