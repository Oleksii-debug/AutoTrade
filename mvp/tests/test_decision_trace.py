import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.decision_trace import DecisionTraceStore, GENESIS_HASH


def trace(trace_id: str, *, decision: str = "BUY") -> dict:
    return {
        "trace_id": trace_id,
        "input_hash": "a" * 64,
        "strategy_version": "moving-average-v1",
        "decision": decision,
        "decision_reason": "fast_above_slow" if decision == "BUY" else "averages_equal",
        "risk_outcome": "admitted" if decision == "BUY" else "not_applicable",
        "order_id": "intent-1" if decision == "BUY" else None,
        "fill_id": "fill-1" if decision == "BUY" else None,
        "evidence_refs": ["evidence-1"],
    }


class DecisionTraceStoreTests(unittest.TestCase):
    def test_append_is_idempotent_and_chain_is_valid(self):
        with TemporaryDirectory() as directory:
            store = DecisionTraceStore(Path(directory) / "decision-traces.jsonl")
            self.assertTrue(store.append(trace("trace-1")))
            self.assertFalse(store.append(trace("trace-1")))
            self.assertTrue(store.verify())
            rows = store.records()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["previous_hash"], GENESIS_HASH)

    def test_conflicting_retry_fails_closed(self):
        with TemporaryDirectory() as directory:
            store = DecisionTraceStore(Path(directory) / "decision-traces.jsonl")
            store.append(trace("trace-1"))
            with self.assertRaisesRegex(ValueError, "different decision content"):
                store.append(trace("trace-1", decision="HOLD"))

    def test_multiple_records_form_hash_chain(self):
        with TemporaryDirectory() as directory:
            store = DecisionTraceStore(Path(directory) / "decision-traces.jsonl")
            store.append(trace("trace-1"))
            second = trace("trace-2", decision="HOLD")
            second["evidence_refs"] = ["evidence-2"]
            store.append(second)
            rows = store.records()
            self.assertEqual(rows[1]["previous_hash"], rows[0]["record_hash"])
            self.assertTrue(store.verify())

    def test_tampering_is_detected(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "decision-traces.jsonl"
            store = DecisionTraceStore(path)
            store.append(trace("trace-1"))
            row = json.loads(path.read_text(encoding="utf-8"))
            row["decision"] = "SELL"
            path.write_text(json.dumps(row) + "\n", encoding="utf-8")
            self.assertFalse(store.verify())
            with self.assertRaisesRegex(ValueError, "chain is corrupt"):
                store.records()

    def test_duplicate_evidence_refs_are_rejected(self):
        with TemporaryDirectory() as directory:
            store = DecisionTraceStore(Path(directory) / "decision-traces.jsonl")
            item = trace("trace-1")
            item["evidence_refs"] = ["evidence-1", "evidence-1"]
            with self.assertRaisesRegex(ValueError, "must not contain duplicates"):
                store.append(item)

    def test_common_secret_aliases_are_redacted_before_persistence(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "decision-traces.jsonl"
            store = DecisionTraceStore(path)
            item = trace("trace-secret-aliases")
            item["attributes"] = {
                "access-token": "access-value",
                "refresh_token": "refresh-value",
                "client secret": "client-value",
                "Authorization-Header": "Bearer value",
                "x-api-key": "key-value",
                "private-key-pem": "pem-value",
                "token_budget": 100,
            }
            store.append(item)
            persisted = json.loads(path.read_text(encoding="utf-8"))
            attributes = persisted["attributes"]
            for key in (
                "access-token",
                "refresh_token",
                "client secret",
                "Authorization-Header",
                "x-api-key",
                "private-key-pem",
            ):
                self.assertEqual(attributes[key], "[REDACTED]")
            self.assertEqual(attributes["token_budget"], 100)
            raw = path.read_text(encoding="utf-8")
            for leaked in (
                "access-value",
                "refresh-value",
                "client-value",
                "Bearer value",
                "key-value",
                "pem-value",
            ):
                self.assertNotIn(leaked, raw)

    def test_embedded_secrets_in_safe_named_strings_are_redacted(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "decision-traces.jsonl"
            store = DecisionTraceStore(path)
            item = trace("trace-embedded-secrets")
            item["attributes"] = {
                "url": "https://provider.test/orders?api_key=abc123&symbol=BTC",
                "message": "Authorization: Bearer bearer-secret",
                "dsn": "host=db;password=hunter2;database=autotrade",
                "key_material": "-----BEGIN PRIVATE KEY-----\\nsecret\\n-----END PRIVATE KEY-----",
                "token_text": "token=plain-token-secret",
                "session_text": "session: plain-session-secret",
                "safe": "symbol=BTC",
            }
            store.append(item)

            raw = path.read_text(encoding="utf-8")
            for leaked in ("abc123", "bearer-secret", "hunter2", "plain-token-secret", "plain-session-secret", "\\nsecret\\n"):
                self.assertNotIn(leaked, raw)
            persisted = json.loads(raw)
            attrs = persisted["attributes"]
            self.assertIn("api_key=[REDACTED]", attrs["url"])
            self.assertIn("Authorization: [REDACTED]", attrs["message"])
            self.assertIn("password=[REDACTED]", attrs["dsn"])
            self.assertEqual(attrs["key_material"], "[REDACTED]")
            self.assertIn("token=[REDACTED]", attrs["token_text"])
            self.assertIn("session:[REDACTED]", attrs["session_text"])
            self.assertEqual(attrs["safe"], "symbol=BTC")

    def test_non_finite_diagnostic_numbers_cannot_enter_durable_trace(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "decision-traces.jsonl"
            store = DecisionTraceStore(path)
            item = trace("trace-nonfinite")
            item["attributes"] = {"score": float("nan")}
            with self.assertRaises(ValueError):
                store.append(item)
            self.assertFalse(path.exists())

    def test_corrupt_existing_chain_blocks_append(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "decision-traces.jsonl"
            store = DecisionTraceStore(path)
            store.append(trace("trace-1"))
            path.write_text(path.read_text(encoding="utf-8").replace('"risk_outcome":"admitted"', '"risk_outcome":"rejected"'), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "chain is corrupt"):
                store.append(trace("trace-2"))


if __name__ == "__main__":
    unittest.main()
