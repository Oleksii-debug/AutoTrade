"""W08 artifact/DAG integration contracts over explicitly synthetic reports.

These tests exercise the real artifact store, economics, specialist DAG and
runner restart path. They do not claim to train or qualify optional ML backends.
"""
from contextlib import redirect_stdout
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
from hashlib import sha256
import io
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from autotrade_runtime.artifacts import ArtifactStore
from mvp.autotrade_mvp.alpha_dag_binding import (
    MAX_DOCUMENT_BYTES, aggregate_alpha_receipt, publish_alpha_research,
)
from research.autotrade_research.integrations.alpha_economics import CostBudget, opportunity


ROOT = Path(__file__).resolve().parents[2]


def sign_packet(packet):
    body = {key: value for key, value in packet.items() if key != "packet_sha256"}
    packet["packet_sha256"] = sha256(json.dumps(body, sort_keys=True, allow_nan=False,
                                               separators=(",", ":")).encode()).hexdigest()
    return packet


def fixture(*, gross="0.01", lower="0.006", notional="100", single=False):
    series = {"market": "BTC_USDT", "closes": ["100"] * 200,
              "closed_at_ms": [i * 60000 for i in range(200)],
              "available_at_ms": [i * 60000 + 100 for i in range(200)],
              "interval_ms": 60000, "asof_ms": 199 * 60000 + 100,
              "source_ref": "fixture:synthetic-contract-not-market-data",
              "snapshot_id": "synthetic-w08-contract", "evidence_class": "SYNTHETIC_FIXTURE"}
    asof = series["asof_ms"]
    cost = CostBudget("0.001", "0.001", "4", "1", "1", "0", "0.02", notional, "5",
                      "USDT", "fixture:hypothetical-costs", asof - 1, asof + 1000)
    members = ["DLINEAR"] if single else ["DLINEAR", "XGBOOST", "CATBOOST"]
    report = {"gross_midpoint_return_ratio": gross, "lower_return_ratio": lower,
              "upper_return_ratio": "0.02", "coverage_guaranteed": False,
              "holdout_used_for_model_selection": False}
    models = {name: dict(report) for name in members}
    if not single:
        models["FIXED_ENSEMBLE"] = {**report, "members": members,
                                    "weights": "EQUAL_FIXED_BEFORE_HOLDOUT"}
    forecast = {"schema_version": "1.0", "provider": "WHITEBIT", "market": series["market"],
                "snapshot_id": series["snapshot_id"], "source_ref": series["source_ref"],
                "evidence_class": series["evidence_class"], "asof_ms": asof,
                "interval_ms": 60000, "horizon_bars": 1,
                "reference_close_at_ms": series["closed_at_ms"][-1],
                "forecast_target_close_at_ms": 200 * 60000,
                "return_reference": "LAST_OBSERVED_CLOSE_NOT_EXECUTABLE_PRICE",
                "mode": "SHADOW_RESEARCH_ONLY", "live_authority": False,
                "automatic_promotion": False, "whitebit_profitability_proven": False,
                "models": models}
    forecast["input_sha256"] = sha256(json.dumps({"prices": series["closes"],
        "times": series["closed_at_ms"], "availability": series["available_at_ms"]},
        separators=(",", ":")).encode()).hexdigest()
    packet = {"schema_version": "1.0", "forecast": forecast,
              "opportunities": {name: opportunity(gross, lower, cost, market=series["market"],
                                    asof_ms=asof) for name in models},
              "coordination": {"input_snapshot_id": series["snapshot_id"],
                  "correlation_group": "SAME_PRICE_HISTORY", "independent_votes": False,
                  "broker_keys_exposed": False, "refresh_policy": "EXPLICIT_OFFLINE_RESEARCH_TRIGGER",
                  "next_boundary": "CANONICAL_EVIDENCE_STORE_AND_SPECIALIST_DAG"},
              "live_authority": False, "automatic_promotion": False}
    return {"series": series, "costs": asdict(cost)}, sign_packet(packet)


class AlphaDagBindingTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name) / "artifacts"
        self.payload, self.packet = fixture()

    def publish(self):
        return publish_alpha_research(self.root, self.payload, self.packet)

    def replay(self, packet_ref, **overrides):
        context = {"expected_snapshot_id": self.payload["series"]["snapshot_id"],
                   "asof_ms": self.payload["series"]["asof_ms"], **overrides}
        return aggregate_alpha_receipt(self.root, packet_ref, **context)

    def test_existing_dag_consumes_one_cost_aware_group_and_durable_receipts(self):
        before = datetime.now(timezone.utc)
        bound = self.publish()
        result = bound["result"]
        self.assertEqual(result["accepted_roles"], ["whitebit-alpha-shared"])
        self.assertEqual(result["selected_report"], "FIXED_ENSEMBLE")
        self.assertEqual(result["direction"], "LONG")
        self.assertEqual(result["score"], "0.0032")
        self.assertEqual(result["evidence_refs"], sorted([bound["input_ref"], bound["packet_ref"]]))
        self.assertFalse(result["independent_votes"])
        self.assertFalse(result["live_authority_granted"])
        self.assertFalse(result["provider_source_authenticated"])
        self.assertEqual(result["financial_admission"], "NOT_PERFORMED")
        self.assertEqual(result["aggregation_cost"], "0")
        self.assertEqual(result["research_model_cost_quote"], "0.02")
        self.assertEqual(result["quote_currency"], "USDT")
        self.assertGreaterEqual(datetime.fromisoformat(result["completed_at"].replace("Z", "+00:00")), before)
        store = ArtifactStore(self.root)
        for name in ("input_ref", "packet_ref", "result_ref"):
            identifier, digest = bound[name].removeprefix("artifact:").split("@")
            manifest, data = store.read_authenticated_snapshot(identifier)
            self.assertEqual(manifest["sha256"], digest)
            self.assertEqual("sha256:" + sha256(data).hexdigest(), digest)
        envelope = json.loads(store.read_bytes(bound["packet_ref"].split("@")[0].removeprefix("artifact:")))
        self.assertEqual(envelope["input_ref"], bound["input_ref"])

    def test_reopen_and_duplicate_publication_have_identical_result_without_refit(self):
        bound = self.publish()
        self.assertEqual(self.replay(bound["packet_ref"]), bound)
        self.assertEqual(self.publish(), bound)
        self.assertEqual(len(list((self.root / "manifests").glob("*.json"))), 3)

    def test_losses_uncertainty_and_small_notional_persist_flat_results(self):
        for index, kwargs in enumerate(({"gross": "0.001", "lower": "0"},
                                         {"lower": "-0.01"}, {"notional": "1"})):
            with self.subTest(kwargs=kwargs):
                payload, packet = fixture(**kwargs)
                bound = publish_alpha_research(self.root / str(index), payload, packet)
                self.assertEqual(bound["result"]["direction"], "FLAT")
                self.assertEqual(bound["result"]["accepted_roles"], ["whitebit-alpha-shared"])
                self.assertFalse(bound["result"]["profitability_proven"])

    def test_single_explicit_backend_is_not_silently_selected_against_another(self):
        payload, packet = fixture(single=True)
        bound = publish_alpha_research(self.root, payload, packet)
        self.assertEqual(bound["result"]["selected_report"], "DLINEAR")
        self.assertEqual(len(bound["result"]["accepted_roles"]), 1)

    def test_wrong_snapshot_cutoff_root_and_receipt_are_rejected(self):
        bound = self.publish()
        for context in ({"expected_snapshot_id": "other"},
                        {"asof_ms": self.payload["series"]["asof_ms"] + 1}):
            with self.subTest(context=context), self.assertRaises(ValueError):
                self.replay(bound["packet_ref"], **context)
        with self.assertRaises(ValueError):
            self.replay(bound["packet_ref"].split("@sha256:")[0] + "@sha256:" + "0" * 64)
        with self.assertRaises((OSError, ValueError)):
            aggregate_alpha_receipt(Path(self.directory.name) / "other", bound["packet_ref"],
                expected_snapshot_id=self.payload["series"]["snapshot_id"],
                asof_ms=self.payload["series"]["asof_ms"])

    def test_corrupt_input_or_packet_cannot_reach_dag_even_with_saved_receipt(self):
        for name in ("input_ref", "packet_ref"):
            with self.subTest(name=name):
                root = self.root / name
                bound = publish_alpha_research(root, self.payload, self.packet)
                digest = bound[name].split("@sha256:")[1]
                path = root / "objects" / "sha256" / digest[:2] / digest
                data = path.read_bytes()
                path.write_bytes(b"!" + data[1:])
                with self.assertRaises(ValueError):
                    aggregate_alpha_receipt(root, bound["packet_ref"],
                        expected_snapshot_id=self.payload["series"]["snapshot_id"],
                        asof_ms=self.payload["series"]["asof_ms"])

    def test_valid_self_hash_does_not_admit_forged_economics_or_authority(self):
        mutations = (
            lambda p: p.update(live_authority=True),
            lambda p: p.update(automatic_promotion=True),
            lambda p: p["coordination"].update(independent_votes=True),
            lambda p: p["forecast"].update(whitebit_profitability_proven=True),
            lambda p: p["forecast"].update(input_sha256="0" * 64),
            lambda p: p["forecast"]["models"]["FIXED_ENSEMBLE"].update(weights="CHOSEN_ON_HOLDOUT"),
            lambda p: p["opportunities"]["FIXED_ENSEMBLE"].update(lower_net_return_ratio="0.99"),
            lambda p: p["opportunities"]["FIXED_ENSEMBLE"].update(candidate=1),
        )
        for mutate in mutations:
            packet = deepcopy(self.packet)
            mutate(packet)
            sign_packet(packet)
            with self.subTest(packet=packet["packet_sha256"]), self.assertRaises(ValueError):
                publish_alpha_research(self.root, self.payload, packet)
        self.assertFalse(self.root.exists())

    def test_future_bar_cost_and_unbounded_document_are_rejected_before_publication(self):
        for path, value in (("asof_ms", self.payload["series"]["asof_ms"] - 1000),
                            ("evidence_class", "LIVE")):
            payload = deepcopy(self.payload)
            payload["series"][path] = value
            with self.subTest(path=path), self.assertRaises(ValueError):
                publish_alpha_research(self.root, payload, self.packet)
        payload = deepcopy(self.payload)
        payload["costs"]["observed_at_ms"] = self.payload["series"]["asof_ms"] + 1
        with self.assertRaises(ValueError):
            publish_alpha_research(self.root, payload, self.packet)
        with self.assertRaises(ValueError):
            publish_alpha_research(self.root, {"oversized": "x" * MAX_DOCUMENT_BYTES}, self.packet)
        self.assertFalse(self.root.exists())

    def test_rehashed_manifest_cannot_redirect_the_input_chain(self):
        bound = self.publish()
        identifier = bound["packet_ref"].split("@")[0].removeprefix("artifact:")
        path = self.root / "manifests" / (identifier + ".json")
        manifest = json.loads(path.read_text())
        manifest["source_refs"] = [bound["result_ref"]]
        manifest["metadata"]["input_ref"] = bound["result_ref"]
        unsigned = {key: value for key, value in manifest.items() if key != "manifest_hash"}
        manifest["manifest_hash"] = "sha256:" + sha256(json.dumps(unsigned, sort_keys=True,
            separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()).hexdigest()
        path.write_text(json.dumps(manifest))
        with self.assertRaises(ValueError):
            self.replay(bound["packet_ref"])

    def test_existing_runner_publishes_once_and_restarts_without_site_dependencies(self):
        from tools.run_whitebit_alpha_research import main
        source = Path(self.directory.name) / "input.json"
        source.write_text(json.dumps(self.payload))
        output = io.StringIO()
        # Fixture only at the already-tested model producer boundary; artifact,
        # economics, DAG, runner and subsequent independent process remain real.
        with patch("tools.run_whitebit_alpha_research.run_payload", return_value=self.packet) as producer:
            with redirect_stdout(output):
                self.assertEqual(main(["--input", str(source), "--artifact-root", str(self.root)]), 0)
            producer.assert_called_once()
        first = json.loads(output.getvalue())["binding"]
        command = [sys.executable, "-S", str(ROOT / "tools/run_whitebit_alpha_research.py"),
                   "--artifact-root", str(self.root), "--packet-ref", first["packet_ref"],
                   "--snapshot-id", self.payload["series"]["snapshot_id"],
                   "--asof-ms", str(self.payload["series"]["asof_ms"])]
        replay = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, timeout=30)
        self.assertEqual(replay.returncode, 0, replay.stderr)
        self.assertEqual(json.loads(replay.stdout)["binding"], first)


if __name__ == "__main__":
    unittest.main()
