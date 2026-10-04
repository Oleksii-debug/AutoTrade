from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.decision_trace import (
    GENESIS_HASH,
    DecisionTraceStore,
    _hash_record,
    canonical_json,
)


def trace(trace_id: str = "trace-evidence") -> dict:
    return {
        "trace_id": trace_id,
        "input_hash": "a" * 64,
        "strategy_version": "strategy-v1",
        "decision": "HOLD",
        "decision_reason": "risk_budget_preserved",
        "risk_outcome": "admitted",
        "event_ids": ["event-1"],
        "evidence_refs": ["evidence-1"],
    }


def write_legacy_unbound_trace(path: Path) -> None:
    record = trace("trace-legacy-unbound")
    record["evidence_refs"] = []
    record["recorded_at"] = "2026-09-01T00:00:00+00:00"
    record["previous_hash"] = GENESIS_HASH
    record["record_hash"] = _hash_record(record)
    path.write_text(canonical_json(record) + "\n", encoding="utf-8")


class DecisionTraceEvidenceAuthorityTests(unittest.TestCase):
    def test_empty_evidence_refs_fail_closed_for_new_append(self):
        with TemporaryDirectory() as directory:
            store = DecisionTraceStore(Path(directory) / "decision-traces.jsonl")
            item = trace()
            item["evidence_refs"] = []
            with self.assertRaisesRegex(
                ValueError,
                "evidence_refs must contain at least one canonical non-empty string",
            ):
                store.append(item)

    def test_legacy_unbound_trace_keeps_integrity_but_never_verifies_evidence(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "decision-traces.jsonl"
            write_legacy_unbound_trace(path)
            store = DecisionTraceStore(path)

            self.assertTrue(store.verify())
            self.assertEqual(store.records()[0]["trace_id"], "trace-legacy-unbound")

            exported = store.accessible_export("trace-legacy-unbound")
            self.assertIn("Evidence status: UNVERIFIED", exported)
            self.assertIn(
                "this legacy trace has no linked evidence",
                exported,
            )
            self.assertIn("Evidence:\n- none", exported)

            with self.assertRaisesRegex(
                ValueError,
                "trace evidence incomplete: no linked evidence",
            ):
                store.accessible_export(
                    "trace-legacy-unbound",
                    available_event_ids=["event-1"],
                    available_evidence_ids=[],
                )

    def test_evidence_bound_append_can_continue_after_valid_legacy_chain(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "decision-traces.jsonl"
            write_legacy_unbound_trace(path)
            store = DecisionTraceStore(path)

            self.assertTrue(store.append(trace("trace-new-bound")))
            self.assertTrue(store.verify())
            self.assertEqual(
                [item["trace_id"] for item in store.records()],
                ["trace-legacy-unbound", "trace-new-bound"],
            )

    def test_accessible_export_without_availability_is_explicitly_unverified(self):
        with TemporaryDirectory() as directory:
            store = DecisionTraceStore(Path(directory) / "decision-traces.jsonl")
            store.append(trace())

            exported = store.accessible_export("trace-evidence")

            self.assertIn("Evidence status: UNVERIFIED", exported)
            self.assertIn(
                "Explanation is diagnostic only; linked evidence availability was not checked.",
                exported,
            )
            self.assertIn("Evidence:\n- evidence-1", exported)

    def test_complete_caller_availability_remains_explicitly_unverified(self):
        with TemporaryDirectory() as directory:
            store = DecisionTraceStore(Path(directory) / "decision-traces.jsonl")
            store.append(trace())

            exported = store.accessible_export(
                "trace-evidence",
                available_event_ids=["event-1"],
                available_evidence_ids=["evidence-1"],
            )

            self.assertIn("Evidence status: UNVERIFIED", exported)
            self.assertNotIn("Evidence status: VERIFIED", exported)
            self.assertIn(
                "Caller-declared linked evidence is complete, but no product-selected durable evidence authority verified those identifiers.",
                exported,
            )

    def test_accessible_export_fails_closed_when_linked_evidence_is_missing(self):
        with TemporaryDirectory() as directory:
            store = DecisionTraceStore(Path(directory) / "decision-traces.jsonl")
            store.append(trace())

            with self.assertRaisesRegex(ValueError, "trace evidence incomplete"):
                store.accessible_export(
                    "trace-evidence",
                    available_event_ids=["event-1"],
                    available_evidence_ids=[],
                )

            with self.assertRaisesRegex(ValueError, "trace evidence incomplete"):
                store.accessible_export(
                    "trace-evidence",
                    available_event_ids=[],
                    available_evidence_ids=["evidence-1"],
                )

    def test_accessible_export_rejects_half_specified_availability(self):
        with TemporaryDirectory() as directory:
            store = DecisionTraceStore(Path(directory) / "decision-traces.jsonl")
            store.append(trace())

            with self.assertRaisesRegex(
                ValueError,
                "available_event_ids and available_evidence_ids must be supplied together",
            ):
                store.accessible_export(
                    "trace-evidence",
                    available_event_ids=["event-1"],
                )


if __name__ == "__main__":
    unittest.main()
