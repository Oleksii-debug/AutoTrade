from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.decision_trace import DecisionTraceStore


class DecisionTraceCallerAvailabilityAuthorityTests(unittest.TestCase):
    def test_caller_supplied_identifier_sets_cannot_mint_verified_evidence_status(self):
        """Availability labels are not durable evidence authority by themselves.

        A caller that knows the identifiers referenced by a trace can always echo
        those identifiers back.  That must not be enough to make an operator-facing
        explanation say VERIFIED without a product-selected durable resolver/store
        actually proving the linked event/evidence records exist at the accepted cut.
        """

        with TemporaryDirectory() as directory:
            store = DecisionTraceStore(Path(directory) / "decision-traces.jsonl")
            store.append(
                {
                    "trace_id": "trace-caller-claims-availability",
                    "input_hash": "a" * 64,
                    "strategy_version": "strategy-v1",
                    "decision": "HOLD",
                    "decision_reason": "risk_budget_preserved",
                    "risk_outcome": "admitted",
                    "event_ids": ["event-does-not-exist-in-authority"],
                    "evidence_refs": ["evidence-does-not-exist-in-authority"],
                }
            )

            rendered = store.accessible_export(
                "trace-caller-claims-availability",
                available_event_ids=["event-does-not-exist-in-authority"],
                available_evidence_ids=["evidence-does-not-exist-in-authority"],
            )

            self.assertNotIn("Evidence status: VERIFIED", rendered)


if __name__ == "__main__":
    unittest.main()
