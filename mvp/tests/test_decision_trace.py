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
