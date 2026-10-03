import unittest

from mvp.autotrade_mvp.runtime_target_host_measurement import (
    ResourceTargetHostSample,
    RuntimeTargetHostMeasurementError,
    TargetHostMeasurementArtifact,
)


SHA = "sha256:" + "1" * 64


def _artifact(*, monotonic_clock_id: str) -> TargetHostMeasurementArtifact:
    return TargetHostMeasurementArtifact(
        source_sha="a" * 40,
        release_artifact_id="40000000-0000-4000-8000-000000000001",
        release_artifact_sha256=SHA,
        scenario_id="wp65-clock-domain",
        spec_digest=SHA,
        configuration_hash=SHA,
        host_fingerprint=SHA,
        workload_profile_hash=SHA,
        plan_digest=SHA,
        journal_taxonomy_digest=SHA,
        journal_store_identity_digest=SHA,
        start_journal_sequence=0,
        end_journal_sequence=0,
        monotonic_clock_id=monotonic_clock_id,
        staleness_basis="host-monotonic-financial-state-age",
        research_interference_basis="host-monotonic-contention-delay",
        financial_samples=(),
        research_samples=(),
        resource_samples=(
            ResourceTargetHostSample(
                sample_id="resource-1",
                monotonic_ns=1,
                phase="steady",
                metrics={"cpu_busy_milli_pct": 1},
            ),
        ),
    )


class RuntimeTargetHostClockDomainTests(unittest.TestCase):
    def test_foreign_monotonic_clock_domain_is_rejected(self):
        with self.assertRaisesRegex(
            RuntimeTargetHostMeasurementError,
            "monotonic_clock_id",
        ):
            _artifact(monotonic_clock_id="foreign-host-clock")

    def test_canonical_campaign_monotonic_clock_domain_is_accepted(self):
        artifact = _artifact(monotonic_clock_id="python-time.monotonic_ns")
        self.assertEqual(artifact.monotonic_clock_id, "python-time.monotonic_ns")


if __name__ == "__main__":
    unittest.main()
