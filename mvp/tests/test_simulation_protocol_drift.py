"""Protocol-drift regressions for canonical simulation durable identity."""

from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import mvp.autotrade_mvp.simulation_session as simulation_session
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.simulation_session import run_canonical_simulation


BUY = ["100", "101", "103"]
SOURCE_SHA = "a" * 40
NOW = "2026-09-30T12:00:00Z"


class SimulationProtocolDriftTests(unittest.TestCase):
    @staticmethod
    def _durable_cut(directory: str):
        store = JournalStore(Path(directory) / "journal.sqlite3")
        return (
            store.load_events("canonical_simulation_session", "single-episode"),
            store.load_events_by_aggregate_type("submission_attempt"),
        )

    def _assert_existing_state_rejects_protocol_drift(self, mutation):
        with TemporaryDirectory() as directory:
            first = run_canonical_simulation(
                BUY, directory, episode_id="protocol-drift", source_sha=SOURCE_SHA,
                now=NOW,
            )
            self.assertEqual(first["new_outbound_requests"], 1)
            before = self._durable_cut(directory)

            with mutation:
                with self.assertRaisesRegex(ValueError, "protocol/build identity"):
                    run_canonical_simulation(
                        BUY, directory, episode_id="protocol-drift",
                        source_sha=SOURCE_SHA, now=NOW,
                    )

            self.assertEqual(self._durable_cut(directory), before)

    def test_fee_rate_drift_is_rejected_before_submission_mutation(self):
        self._assert_existing_state_rejects_protocol_drift(
            patch.object(simulation_session, "FEE_RATE", Decimal("0.002"))
        )

    def test_strategy_configuration_drift_is_rejected_before_submission_mutation(self):
        self._assert_existing_state_rejects_protocol_drift(
            patch.dict(simulation_session._STRATEGY_CONFIG, {"fast": 1})
        )

    def test_risk_policy_drift_is_rejected_before_submission_mutation(self):
        self._assert_existing_state_rejects_protocol_drift(
            patch.dict(
                simulation_session._RISK_POLICY_CONFIG,
                {"max_single_notional": "900"},
            )
        )


if __name__ == "__main__":
    unittest.main()
