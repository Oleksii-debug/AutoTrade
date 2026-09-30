"""Regression for the canonical simulation provider-domain send binding."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.simulation_session import (
    ACCOUNT,
    ENVIRONMENT,
    PROVIDER,
    run_canonical_simulation,
)


NOW = "2026-09-30T12:00:00Z"
BUY = ["100", "101", "103"]


class SimulationProviderDomainBindingTests(unittest.TestCase):
    def test_submission_prepared_retains_exact_simulation_provider_domain(self):
        with TemporaryDirectory() as directory:
            result = run_canonical_simulation(
                BUY,
                directory,
                episode_id="provider-domain-binding",
                now=NOW,
            )
            self.assertEqual(
                result["status"],
                "FILL_RECONCILED_ORDER_UNCONFIRMED",
            )
            store = JournalStore(Path(directory) / "journal.sqlite3")
            events = store.load_events_by_aggregate_type("submission_attempt")
            prepared = next(
                event
                for event in events
                if event["event_type"] == "SubmissionPrepared"
            )
            self.assertEqual(
                prepared["payload"]["submission_scope"],
                {
                    "provider_id": PROVIDER,
                    "provider_environment": ENVIRONMENT,
                    "account_id": ACCOUNT,
                    "environment": ENVIRONMENT,
                },
            )


if __name__ == "__main__":
    unittest.main()
