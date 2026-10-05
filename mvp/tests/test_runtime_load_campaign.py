from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from mvp.autotrade_mvp.performance_qualification import (
    RuntimeBudgetSpec,
    RuntimeLoadObservation,
    evaluate_runtime_budget,
)
from mvp.autotrade_mvp.persistence import JournalStore, payload_digest
from mvp.autotrade_mvp.pipeline import run_vertical_slice as real_run_vertical_slice
from mvp.autotrade_mvp.runtime_load_campaign import (
    _observed_source_sha,
    RuntimeLoadCampaignEvidence,
    _sha256_identity,
    capture_runtime_host_identity,
    collect_vertical_slice_load_evidence,
    collect_vertical_slice_load_observation,
    load_runtime_load_campaign_evidence,
    serialize_runtime_load_campaign_evidence,
    write_runtime_load_campaign_evidence,
)


class RuntimeLoadCampaignTests(unittest.TestCase):
    def spec(self, configuration, host):
        return RuntimeBudgetSpec(
            scenario_id="vertical-slice-load",
            release_sha=_observed_source_sha(),
            configuration_hash=_sha256_identity(configuration),
            host_fingerprint=_sha256_identity(host),
            strategy_horizon_us=10_000_000,
            max_p95_financial_latency_us=10_000_000,
            max_financial_staleness_us=10_000_000,
            max_research_interference_us=10_000_000,
            min_financial_samples=3,
            min_research_samples=1,
        )

    def test_identity_hashing_rejects_polymorphic_containers_before_callbacks(self):
        callbacks = []

        class HostileMapping(dict):
            def items(self):
                callbacks.append("items")
                raise AssertionError("identity mapping callback executed")

            def __iter__(self):
                callbacks.append("iter")
                raise AssertionError("identity mapping callback executed")

        class HostileList(list):
            def __iter__(self):
                callbacks.append("list-iter")
                raise AssertionError("identity list callback executed")

        for hostile in (
            HostileMapping({"machine": "host"}),
            MappingProxyType(HostileMapping({"machine": "host"})),
            HostileList(["host"]),
        ):
            with self.subTest(type=type(hostile).__name__), self.assertRaisesRegex(
                TypeError,
                "unsupported non-JSON value",
            ):
                _sha256_identity(hostile)

        self.assertEqual(callbacks, [])

    def test_campaign_evidence_rejects_polymorphic_identity_and_tuple_carriers(self):
        callbacks = []

        class HostileMapping(dict):
            def items(self):
                callbacks.append("items")
                raise AssertionError("host identity callback executed")

        class HostileTuple(tuple):
            def __len__(self):
                callbacks.append("len")
                raise AssertionError("tuple length callback executed")

            def __iter__(self):
                callbacks.append("iter")
                raise AssertionError("tuple iteration callback executed")

        observation = RuntimeLoadObservation.create(
            scenario_id="ingress-regression",
            spec_digest="sha256:" + ("b" * 64),
            release_sha="a" * 40,
            configuration_hash="sha256:" + ("c" * 64),
            host_fingerprint="sha256:" + ("d" * 64),
            expected_financial_events=1,
            recovered_financial_events=1,
            financial_latency_us=(1,),
            financial_staleness_us=(),
            research_interference_us=(),
            recovered_financial_event_ids=("event-1",),
            financial_latency_event_ids=("event-1",),
            financial_staleness_event_ids=(),
            reconnect_backlog_remaining=0,
            declared_duration_us=10,
            observed_duration_us=1,
        )

        with self.assertRaisesRegex(TypeError, "unsupported non-JSON value"):
            RuntimeLoadCampaignEvidence(
                observation=observation,
                journal_sequence_before=0,
                journal_sequence_after=1,
                recovered_event_ids=("event-1",),
                recovered_journal_sequences=(1,),
                host_identity=HostileMapping({"machine": "host"}),
            )

        with self.assertRaisesRegex(ValueError, "exact non-empty strings"):
            RuntimeLoadCampaignEvidence(
                observation=observation,
                journal_sequence_before=0,
                journal_sequence_after=1,
                recovered_event_ids=HostileTuple(("event-1",)),
                recovered_journal_sequences=(1,),
                host_identity={"machine": "host"},
            )

        self.assertEqual(callbacks, [])

    def test_runtime_host_capture_returns_detached_exact_dict(self):
        identity = capture_runtime_host_identity()
        self.assertIs(type(identity), dict)
        self.assertIn("system", identity)
        self.assertIn("python_version", identity)
        self.assertIn("cpu_count", identity)

    def test_campaign_rejects_release_sha_not_matching_actual_checkout(self):
        configuration = {"mode": "SIMULATION", "symbol": "SIM"}
        host = {"machine": "test-host", "runtime": "python"}
        spec = self.spec(configuration, host)
        different = ("0" if spec.release_sha[0] != "0" else "1") + spec.release_sha[1:]
        mismatched = RuntimeBudgetSpec(
            scenario_id=spec.scenario_id,
            release_sha=different,
            configuration_hash=spec.configuration_hash,
            host_fingerprint=spec.host_fingerprint,
            strategy_horizon_us=spec.strategy_horizon_us,
            max_p95_financial_latency_us=spec.max_p95_financial_latency_us,
            max_financial_staleness_us=spec.max_financial_staleness_us,
            max_research_interference_us=spec.max_research_interference_us,
            min_financial_samples=spec.min_financial_samples,
            min_research_samples=spec.min_research_samples,
        )
        with TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError, "actual Git checkout"):
                collect_vertical_slice_load_observation(
                    mismatched,
                    state_dir=directory,
                    episodes=(("100", "101", "102", "103"),),
                    configuration=configuration,
                    host_identity=host,
                    declared_duration_us=10_000_000,
                )
            self.assertFalse((Path(directory) / "journal.sqlite3").exists())

    def test_declared_duration_is_not_derived_from_observed_runtime(self):
        configuration = {"mode": "SIMULATION", "symbol": "SIM"}
        host = {"machine": "test-host", "runtime": "python"}
        spec = self.spec(configuration, host)
        with TemporaryDirectory() as directory:
            observation = collect_vertical_slice_load_observation(
                spec,
                state_dir=directory,
                episodes=(
                    ("100", "101", "102", "103"),
                    ("103", "102", "101", "100"),
                    ("100", "101", "102", "104"),
                ),
                configuration=configuration,
                host_identity=host,
                declared_duration_us=9_999_999,
            )
            self.assertEqual(observation.declared_duration_us, 9_999_999)
            self.assertGreater(observation.observed_duration_us, 0)

    def test_campaign_derives_event_counts_from_durable_journal(self):
        configuration = {"mode": "SIMULATION", "symbol": "SIM"}
        host = {"machine": "test-host", "runtime": "python"}
        spec = self.spec(configuration, host)
        with TemporaryDirectory() as directory:
            observation = collect_vertical_slice_load_observation(
                spec,
                state_dir=Path(directory),
                episodes=(
                    ("100", "101", "102", "103"),
                    ("103", "102", "101", "100"),
                    ("100", "101", "102", "104"),
                ),
                configuration=configuration,
                host_identity=host,
                declared_duration_us=10_000_000,
            )
            self.assertEqual(observation.expected_financial_events, 3)
            self.assertEqual(observation.recovered_financial_events, 3)
            self.assertEqual(len(observation.financial_latency_us), 3)
            self.assertEqual(observation.financial_staleness_us, ())
            self.assertEqual(observation.research_interference_us, ())
            decision = evaluate_runtime_budget(spec, observation)
            # The vertical slice leaves its canonical outbox undelivered.  The
            # hardened collector now derives that backlog from JournalStore
            # instead of inventing zero, so the campaign must FAIL rather than
            # merely remain inconclusive.
            self.assertGreater(observation.reconnect_backlog_remaining, 0)
            self.assertEqual(decision.status, "FAIL")
            self.assertIn("reconnect_backlog_not_drained", decision.reasons)
            self.assertIn("insufficient_staleness_samples", decision.reasons)
            self.assertIn("insufficient_research_interference_samples", decision.reasons)

    def test_unrelated_journal_event_cannot_inflate_recovered_financial_count(self):
        configuration = {"mode": "SIMULATION", "symbol": "SIM"}
        host = {"machine": "test-host", "runtime": "python"}
        spec = self.spec(configuration, host)
        with TemporaryDirectory() as directory:
            root = Path(directory)
            injected = False

            def run_with_unrelated_event(episode, state_dir):
                nonlocal injected
                result = real_run_vertical_slice(episode, state_dir)
                if not injected:
                    store = JournalStore(root / "journal.sqlite3")
                    payload = {"kind": "qualification-diagnostic"}
                    committed_at = datetime.now(timezone.utc).isoformat()
                    store.append_event(
                        {
                            "event_id": "qualification-diagnostic-1",
                            "event_type": "QualificationDiagnosticRecorded",
                            "aggregate_type": "qualification_diagnostic",
                            "aggregate_id": "wp65",
                            "aggregate_version": "1",
                            "committed_at": committed_at,
                            "payload": payload,
                            "payload_hash": payload_digest(payload),
                        }
                    )
                    injected = True
                return result

            with patch(
                "mvp.autotrade_mvp.runtime_load_campaign.run_vertical_slice",
                side_effect=run_with_unrelated_event,
            ):
                observation = collect_vertical_slice_load_observation(
                    spec,
                    state_dir=root,
                    episodes=(
                        ("100", "101", "102", "103"),
                        ("103", "102", "101", "100"),
                        ("100", "101", "102", "104"),
                    ),
                    configuration=configuration,
                    host_identity=host,
                    declared_duration_us=10_000_000,
                )

            store = JournalStore(root / "journal.sqlite3")
            self.assertEqual(store.current_journal_sequence(), 4)
            self.assertEqual(observation.expected_financial_events, 3)
            self.assertEqual(observation.recovered_financial_events, 3)
            decision = evaluate_runtime_budget(spec, observation)
            # This regression owns durable financial-event classification, not
            # host throughput. A slow qualification host may legitimately fail
            # the separately evaluated throughput budget. The injected durable
            # non-financial event must still never become financial event loss,
            # and absent latency/staleness authority must never produce PASS.
            self.assertNotEqual(decision.status, "PASS")
            self.assertNotIn("financial_event_loss", decision.reasons)

    def test_campaign_retains_unique_durable_event_identity_cut(self):
        configuration = {"mode": "SIMULATION", "symbol": "SIM"}
        host = {"machine": "test-host", "runtime": "python"}
        spec = self.spec(configuration, host)
        with TemporaryDirectory() as directory:
            evidence = collect_vertical_slice_load_evidence(
                spec,
                state_dir=directory,
                episodes=(
                    ("100", "101", "102", "103"),
                    ("103", "102", "101", "100"),
                    ("100", "102", "103", "105"),
                ),
                configuration=configuration,
                host_identity=host,
                declared_duration_us=10_000_000,
            )
            self.assertEqual(len(evidence.recovered_event_ids), 3)
            self.assertEqual(len(set(evidence.recovered_event_ids)), 3)
            self.assertEqual(
                tuple(sorted(evidence.recovered_journal_sequences)),
                evidence.recovered_journal_sequences,
            )
            self.assertTrue(evidence.evidence_digest.startswith("sha256:"))
            self.assertEqual(evidence.observation.recovered_financial_events, 3)

    def test_injected_missing_runtime_event_fails_event_conservation(self):
        configuration = {"mode": "SIMULATION", "symbol": "SIM"}
        host = {"machine": "test-host", "runtime": "python"}
        spec = self.spec(configuration, host)
        with TemporaryDirectory() as directory:
            calls = 0

            def drop_one_episode(episode, state_dir):
                nonlocal calls
                calls += 1
                if calls == 2:
                    return None
                return real_run_vertical_slice(episode, state_dir)

            with patch(
                "mvp.autotrade_mvp.runtime_load_campaign.run_vertical_slice",
                side_effect=drop_one_episode,
            ):
                observation = collect_vertical_slice_load_observation(
                    spec,
                    state_dir=directory,
                    episodes=(
                        ("100", "101", "102", "103"),
                        ("103", "102", "101", "100"),
                        ("100", "101", "103", "106"),
                    ),
                    configuration=configuration,
                    host_identity=host,
                    declared_duration_us=10_000_000,
                )

            decision = evaluate_runtime_budget(spec, observation)
            self.assertEqual(observation.expected_financial_events, 3)
            self.assertEqual(observation.recovered_financial_events, 2)
            self.assertEqual(decision.status, "FAIL")
            self.assertIn("financial_event_loss", decision.reasons)

    def test_runtime_host_capture_is_hashable_qualification_identity(self):
        identity = capture_runtime_host_identity()
        digest = _sha256_identity(identity)
        self.assertTrue(digest.startswith("sha256:"))
        self.assertIn("system", identity)
        self.assertIn("python_version", identity)
        self.assertIn("cpu_count", identity)

    def test_captured_host_identity_flows_through_campaign_collector(self):
        configuration = {"mode": "SIMULATION", "symbol": "SIM"}
        identity = capture_runtime_host_identity()
        spec = self.spec(configuration, identity)
        with TemporaryDirectory() as directory:
            evidence = collect_vertical_slice_load_evidence(
                spec,
                state_dir=directory,
                episodes=(
                    ("100", "101", "102", "103"),
                    ("103", "102", "101", "100"),
                    ("100", "102", "103", "105"),
                ),
                configuration=configuration,
                host_identity=identity,
                declared_duration_us=10_000_000,
            )
        self.assertEqual(dict(evidence.host_identity), dict(identity))
        self.assertEqual(
            evidence.observation.host_fingerprint,
            _sha256_identity(identity),
        )

    def test_retained_campaign_evidence_roundtrip_is_canonical_and_tamper_evident(self):
        configuration = {"mode": "SIMULATION", "symbol": "SIM"}
        host = {"machine": "test-host", "runtime": "python"}
        spec = self.spec(configuration, host)
        with TemporaryDirectory() as directory:
            root = Path(directory)
            evidence = collect_vertical_slice_load_evidence(
                spec,
                state_dir=root / "runtime",
                episodes=(
                    ("100", "101", "102", "103"),
                    ("103", "102", "101", "100"),
                    ("100", "102", "103", "105"),
                ),
                configuration=configuration,
                host_identity=host,
                declared_duration_us=10_000_000,
            )
            path = root / "qualification" / "runtime-load.json"
            artifact_sha = write_runtime_load_campaign_evidence(evidence, path)
            self.assertEqual(
                artifact_sha,
                "sha256:" + __import__("hashlib").sha256(path.read_bytes()).hexdigest(),
            )
            restored = load_runtime_load_campaign_evidence(path)
            self.assertEqual(restored, evidence)
            self.assertEqual(
                path.read_bytes(),
                serialize_runtime_load_campaign_evidence(restored),
            )

            document = __import__("json").loads(path.read_text(encoding="utf-8"))
            document["observation"]["recovered_financial_events"] = 2
            path.write_text(
                __import__("json").dumps(
                    document,
                    sort_keys=True,
                    separators=(",", ":"),
                ) + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "digest does not match"):
                load_runtime_load_campaign_evidence(path)

    def test_retained_campaign_evidence_rejects_unknown_fields_and_noncanonical_bytes(self):
        configuration = {"mode": "SIMULATION", "symbol": "SIM"}
        host = {"machine": "test-host", "runtime": "python"}
        spec = self.spec(configuration, host)
        with TemporaryDirectory() as directory:
            root = Path(directory)
            evidence = collect_vertical_slice_load_evidence(
                spec,
                state_dir=root / "runtime",
                episodes=(
                    ("100", "101", "102", "103"),
                    ("103", "102", "101", "100"),
                    ("100", "102", "103", "105"),
                ),
                configuration=configuration,
                host_identity=host,
                declared_duration_us=10_000_000,
            )
            path = root / "evidence.json"
            write_runtime_load_campaign_evidence(evidence, path)
            document = __import__("json").loads(path.read_text(encoding="utf-8"))
            document["surprise"] = True
            path.write_text(
                __import__("json").dumps(
                    document,
                    sort_keys=True,
                    separators=(",", ":"),
                ) + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "structure is not canonical"):
                load_runtime_load_campaign_evidence(path)

            write_runtime_load_campaign_evidence(evidence, path)
            path.write_text(
                path.read_text(encoding="utf-8").replace(",", ", "),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "bytes are not canonical"):
                load_runtime_load_campaign_evidence(path)

    def test_campaign_rejects_configuration_or_host_identity_drift(self):
        configuration = {"mode": "SIMULATION"}
        host = {"machine": "test-host"}
        spec = self.spec(configuration, host)
        with TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "configuration"):
                collect_vertical_slice_load_observation(
                    spec,
                    state_dir=directory,
                    episodes=((100, 101, 102, 103),),
                    configuration={"mode": "PAPER"},
                    host_identity=host,
                    declared_duration_us=10_000_000,
                )
            with self.assertRaisesRegex(ValueError, "host identity"):
                collect_vertical_slice_load_observation(
                    spec,
                    state_dir=directory,
                    episodes=((100, 101, 102, 103),),
                    configuration=configuration,
                    host_identity={"machine": "other"},
                    declared_duration_us=10_000_000,
                )


if __name__ == "__main__":
    unittest.main()
