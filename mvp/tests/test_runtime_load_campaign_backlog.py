from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.performance_qualification import RuntimeBudgetSpec
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.runtime_load_campaign import (
    _observed_source_sha,
    _sha256_identity,
    collect_vertical_slice_load_evidence,
)


class RuntimeLoadCampaignBacklogTests(unittest.TestCase):
    def test_campaign_reports_the_canonical_pending_outbox_instead_of_synthetic_zero(self):
        configuration = {"mode": "SIMULATION", "symbol": "SIM"}
        host = {"machine": "wp65-backlog", "runtime": "python"}
        spec = RuntimeBudgetSpec(
            scenario_id="vertical-slice-backlog",
            release_sha=_observed_source_sha(),
            configuration_hash=_sha256_identity(configuration),
            host_fingerprint=_sha256_identity(host),
            strategy_horizon_us=10_000_000,
            max_p95_financial_latency_us=10_000_000,
            max_financial_staleness_us=10_000_000,
            max_research_interference_us=10_000_000,
            min_financial_samples=1,
            min_research_samples=1,
        )
        with TemporaryDirectory() as directory:
            root = Path(directory)
            evidence = collect_vertical_slice_load_evidence(
                spec,
                state_dir=root,
                episodes=(("100", "101", "102", "103"),),
                configuration=configuration,
                host_identity=host,
                declared_duration_us=10_000_000,
            )
            durable_backlog = JournalStore(root / "journal.sqlite3").pending_outbox_count()

        self.assertEqual(durable_backlog, 1)
        self.assertEqual(
            evidence.observation.reconnect_backlog_remaining,
            durable_backlog,
        )


if __name__ == "__main__":
    unittest.main()
