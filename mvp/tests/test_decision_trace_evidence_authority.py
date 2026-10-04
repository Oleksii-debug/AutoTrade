from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.decision_trace import DecisionTraceStore


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


class DecisionTraceEvidenceAuthorityTests(unittest.TestCase):
    def test_empty_evidence_refs_fail_closed(self):
        with TemporaryDirectory() as directory:
            store = DecisionTraceStore(Path(directory) / "decision-traces.jsonl")
            item = trace()
            item["evidence_refs"] = []
            with self.assertRaisesRegex(
                ValueError,
                "evidence_refs must contain at least one canonical non-empty string",
            ):
                store.append(item)

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

    def test_accessible_export_marks_verified_only_with_complete_linked_evidence(self):
        with TemporaryDirectory() as directory:
            store = DecisionTraceStore(Path(directory) / "decision-traces.jsonl")
            store.append(trace())

            exported = store.accessible_export(
                "trace-evidence",
                available_event_ids=["event-1"],
                available_evidence_ids=["evidence-1"],
            )

            self.assertIn("Evidence status: VERIFIED", exported)
            self.assertIn(
                "All linked durable events and evidence were available at export time.",
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
