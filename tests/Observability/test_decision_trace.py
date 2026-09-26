import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.decision_trace import BoundedMetricBacklog, DecisionTraceStore


def evidence_trace(trace_id: str = "decision-1") -> dict:
    return {
        "trace_id": trace_id,
        "input_hash": "b" * 64,
        "strategy_version": "baseline-v2",
        "decision": "NO_TRADE",
        "decision_reason": "insufficient_after_cost_edge",
        "risk_outcome": "not_applicable",
        "evidence_refs": ["dataset-1", "risk-evidence-1"],
        "correlation_id": "corr-1",
        "event_ids": ["event-market", "event-decision"],
        "attributes": {
            "strategy": "baseline",
            "token": "super-secret",
            "nested": {"api_key": "hidden", "safe": "ok"},
        },
    }


class DecisionTraceEvidenceTests(unittest.TestCase):
    def test_durable_trace_redacts_sensitive_diagnostics_before_persistence(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "decision-traces.jsonl"
            store = DecisionTraceStore(path)
            item = evidence_trace()
            self.assertTrue(store.append(item))

            persisted = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(persisted["attributes"]["token"], "[REDACTED]")
            self.assertEqual(persisted["attributes"]["nested"]["api_key"], "[REDACTED]")
            self.assertEqual(persisted["attributes"]["nested"]["safe"], "ok")
            self.assertNotIn("super-secret", path.read_text(encoding="utf-8"))
            self.assertTrue(store.verify())

    def test_reconstruction_requires_all_durable_links(self):
        with TemporaryDirectory() as directory:
            store = DecisionTraceStore(Path(directory) / "decision-traces.jsonl")
            store.append(evidence_trace())

            with self.assertRaisesRegex(ValueError, "trace evidence incomplete"):
                store.reconstruct(
                    "decision-1",
                    available_event_ids=["event-market"],
                    available_evidence_ids=["dataset-1", "risk-evidence-1"],
                )

            with self.assertRaisesRegex(ValueError, "trace evidence incomplete"):
                store.reconstruct(
                    "decision-1",
                    available_event_ids=["event-market", "event-decision"],
                    available_evidence_ids=["dataset-1"],
                )

            record = store.reconstruct(
                "decision-1",
                available_event_ids=["event-market", "event-decision"],
                available_evidence_ids=["dataset-1", "risk-evidence-1"],
            )
            self.assertEqual(record["trace_id"], "decision-1")
            self.assertEqual(record["event_ids"][-1], "event-decision")

    def test_accessible_export_is_linear_verified_and_redacted(self):
        with TemporaryDirectory() as directory:
            store = DecisionTraceStore(Path(directory) / "decision-traces.jsonl")
            store.append(evidence_trace())
            exported = store.accessible_export("decision-1")
            self.assertIn("Decision trace: decision-1", exported)
            self.assertIn("- event-decision", exported)
            self.assertIn("- dataset-1", exported)
            self.assertIn("[REDACTED]", exported)
            self.assertNotIn("super-secret", exported)

    def test_conflicting_retry_compares_redacted_persisted_semantics(self):
        with TemporaryDirectory() as directory:
            store = DecisionTraceStore(Path(directory) / "decision-traces.jsonl")
            first = evidence_trace()
            second = evidence_trace()
            second["attributes"]["token"] = "different-secret"
            self.assertTrue(store.append(first))
            self.assertFalse(store.append(second))

            third = evidence_trace()
            third["attributes"]["strategy"] = "changed"
            with self.assertRaisesRegex(ValueError, "different decision content"):
                store.append(third)

    def test_event_identity_must_be_unique_and_non_empty(self):
        with TemporaryDirectory() as directory:
            store = DecisionTraceStore(Path(directory) / "decision-traces.jsonl")
            duplicate = evidence_trace()
            duplicate["event_ids"] = ["event-1", "event-1"]
            with self.assertRaisesRegex(ValueError, "must not contain duplicates"):
                store.append(duplicate)

    def test_input_hash_must_be_canonical_sha256(self):
        with TemporaryDirectory() as directory:
            store = DecisionTraceStore(Path(directory) / "decision-traces.jsonl")
            for invalid in ("abc", "B" * 64, "g" * 64):
                item = evidence_trace()
                item["input_hash"] = invalid
                with self.assertRaisesRegex(ValueError, "lowercase SHA-256"):
                    store.append(item)

    def test_link_identities_reject_surrounding_whitespace(self):
        with TemporaryDirectory() as directory:
            store = DecisionTraceStore(Path(directory) / "decision-traces.jsonl")
            item = evidence_trace()
            item["evidence_refs"] = [" dataset-1"]
            with self.assertRaisesRegex(ValueError, "canonical"):
                store.append(item)

            item = evidence_trace()
            item["event_ids"] = ["event-market "]
            with self.assertRaisesRegex(ValueError, "canonical"):
                store.append(item)

    def test_non_finite_attributes_never_enter_durable_hash_chain(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "decision-traces.jsonl"
            store = DecisionTraceStore(path)
            item = evidence_trace()
            item["attributes"]["diagnostic_score"] = float("nan")
            with self.assertRaisesRegex(ValueError, "JSON compliant"):
                store.append(item)
            self.assertFalse(path.exists())

    def test_tampered_non_finite_json_is_not_verified(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "decision-traces.jsonl"
            store = DecisionTraceStore(path)
            store.append(evidence_trace())
            raw = path.read_text(encoding="utf-8")
            raw = raw.replace('"strategy":"baseline"', '"strategy":NaN')
            path.write_text(raw, encoding="utf-8")
            self.assertFalse(store.verify())

    def test_idempotent_retry_never_masks_existing_chain_corruption(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "decision-traces.jsonl"
            store = DecisionTraceStore(path)
            item = evidence_trace()
            self.assertTrue(store.append(item))

            records = path.read_text(encoding="utf-8")
            records = records.replace(
                '"previous_hash":"' + "0" * 64 + '"',
                '"previous_hash":"' + "1" * 64 + '"',
            )
            path.write_text(records, encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "chain is corrupt"):
                store.append(item)

    def test_secret_aliases_remain_redacted_after_semantic_merge(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "decision-traces.jsonl"
            store = DecisionTraceStore(path)
            item = evidence_trace()
            item["attributes"].update(
                {
                    "Authorization-Header": "Bearer hidden",
                    "client.secret": "hidden-client",
                    "credential_id": "hidden-credential-id",
                    "proxy authorization": "Basic hidden-proxy-auth",
                    "refresh-token": "hidden-refresh",
                    "private key pem": "hidden-key",
                }
            )
            store.append(item)
            persisted = json.loads(path.read_text(encoding="utf-8"))
            attrs = persisted["attributes"]
            self.assertEqual(attrs["Authorization-Header"], "[REDACTED]")
            self.assertEqual(attrs["client.secret"], "[REDACTED]")
            self.assertEqual(attrs["credential_id"], "[REDACTED]")
            self.assertEqual(attrs["proxy authorization"], "[REDACTED]")
            self.assertEqual(attrs["refresh-token"], "[REDACTED]")
            self.assertEqual(attrs["private key pem"], "[REDACTED]")

    def test_metric_backlog_rejects_non_finite_or_non_numeric_values(self):
        backlog = BoundedMetricBacklog(max_items=2)
        for value in (
            float("nan"),
            float("inf"),
            float("-inf"),
            True,
            "1.0",
        ):
            with self.subTest(value=value), self.assertRaisesRegex(
                ValueError,
                "finite number",
            ):
                backlog.record("queue.delay", value)
        self.assertEqual(backlog.snapshot(), ())
        backlog.record("queue.delay", 1)
        self.assertEqual(backlog.snapshot()[0]["value"], 1)

    def test_metric_backlog_is_bounded_and_redacts_labels(self):
        backlog = BoundedMetricBacklog(max_items=2)
        backlog.record("queue.delay", 1.0, token="a")
        backlog.record("queue.delay", 2.0, provider="sim")
        backlog.record("queue.delay", 3.0, password="b")
        self.assertEqual(backlog.dropped, 1)
        snapshot = backlog.snapshot()
        self.assertEqual(len(snapshot), 2)
        self.assertEqual(snapshot[-1]["labels"]["password"], "[REDACTED]")

    def test_metric_backlog_rejects_unbounded_or_non_json_labels(self):
        backlog = BoundedMetricBacklog(max_items=2, max_label_bytes=32)
        with self.assertRaisesRegex(ValueError, "bounded size"):
            backlog.record("queue.delay", 1.0, detail="x" * 100)
        with self.assertRaisesRegex(ValueError, "finite JSON values"):
            backlog.record("queue.delay", 1.0, score=float("nan"))
        with self.assertRaisesRegex(ValueError, "finite JSON values"):
            backlog.record("queue.delay", 1.0, marker=object())
        self.assertEqual(backlog.snapshot(), ())


if __name__ == "__main__":
    unittest.main()
