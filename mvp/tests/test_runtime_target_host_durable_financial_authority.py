from __future__ import annotations

from contextlib import nullcontext
from inspect import getclosurevars
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from mvp.autotrade_mvp import persistence
from mvp.autotrade_mvp import runtime_load_measurement
from mvp.autotrade_mvp import runtime_load_plan
from mvp.autotrade_mvp import (
    runtime_target_host_durable_financial as durable,
)
from mvp.autotrade_mvp import (
    runtime_target_host_measurement as measurement_module,
)
from mvp.autotrade_mvp import (
    runtime_target_host_measurement_authority as measurement_authority,
)
from mvp.autotrade_mvp.runtime_target_host_durable_financial_authority import (
    _build_low_level_durable_financial_authority,
    _build_release_bound_durable_financial_authority,
    bind_sealed_durable_financial_latency_to_target_host_measurement,
    bind_sealed_release_bound_durable_financial_latency_to_target_host_measurement,
)


RELEASE_ID = "50000000-0000-4000-8000-000000000001"
RELEASE_SHA = "sha256:" + "e" * 64


class RuntimeTargetHostDurableFinancialAuthorityTests(unittest.TestCase):
    def test_low_level_factory_uses_only_injected_direct_dependencies(self) -> None:
        spec_digest = "sha256:" + "1" * 64
        measurement_digest = "sha256:" + "2" * 64
        plan_digest = "sha256:" + "3" * 64
        spec_digest_constant = spec_digest
        plan_digest_constant = plan_digest

        class Spec:
            def __init__(
                self,
                *,
                scenario_id,
                release_sha,
                configuration_hash,
                host_fingerprint,
                strategy_horizon_us,
                max_p95_financial_latency_us,
                max_financial_staleness_us,
                max_research_interference_us,
                min_financial_samples,
                min_research_samples,
            ):
                self.scenario_id = scenario_id
                self.release_sha = release_sha
                self.configuration_hash = configuration_hash
                self.host_fingerprint = host_fingerprint
                self.strategy_horizon_us = strategy_horizon_us
                self.max_p95_financial_latency_us = max_p95_financial_latency_us
                self.max_financial_staleness_us = max_financial_staleness_us
                self.max_research_interference_us = max_research_interference_us
                self.min_financial_samples = min_financial_samples
                self.min_research_samples = min_research_samples

            @property
            def digest(self):
                return spec_digest

        class Measurement:
            monotonic_clock_id = "clock"
            source_sha = "a" * 40
            scenario_id = "scenario"
            spec_digest = spec_digest_constant
            configuration_hash = "sha256:" + "b" * 64
            host_fingerprint = "sha256:" + "c" * 64
            journal_store_identity_digest = "store-digest"
            start_journal_sequence = 10
            end_journal_sequence = 30

            def __init__(self, samples):
                self.financial_samples = samples

            @property
            def digest(self):
                return measurement_digest

        class DurableSample:
            plan_id = "plan-1"
            plan_digest = plan_digest_constant
            event_id = "event-1"
            event_journal_sequence = 15
            measurement_journal_sequence = 16
            monotonic_start_ns = 1_000
            monotonic_end_ns = 2_000
            latency_us = 1

        target = SimpleNamespace(
            event_id="event-1",
            journal_sequence=15,
            latency_start_monotonic_ns=1_000,
            latency_end_monotonic_ns=2_000,
            sample_id="sample-1",
        )
        measurement = Measurement((target,))
        current_spec = Spec(
            scenario_id="scenario",
            release_sha="a" * 40,
            configuration_hash="sha256:" + "b" * 64,
            host_fingerprint="sha256:" + "c" * 64,
            strategy_horizon_us=1,
            max_p95_financial_latency_us=1,
            max_financial_staleness_us=1,
            max_research_interference_us=1,
            min_financial_samples=1,
            min_research_samples=1,
        )
        plan = SimpleNamespace(
            store_identity_digest="store-digest",
            declared_journal_sequence=5,
            plan_id="plan-1",
            digest=plan_digest,
            expected_events=(SimpleNamespace(event_id="event-1"),),
        )
        durable_sample = DurableSample()
        require_authority = Mock(return_value="store-id")
        plan_loader = Mock(return_value=plan)
        sample_loader = Mock(return_value=(durable_sample,))
        binding_from_sample = Mock(return_value="binding-1")
        captured_kwargs = {}

        def binding_factory(**kwargs):
            captured_kwargs.update(kwargs)
            return SimpleNamespace(**kwargs)

        binder = _build_low_level_durable_financial_authority(
            python_version=(3, 13),
            measurement_type=Measurement,
            measurement_snapshotter=lambda value: value,
            target_clock_id="clock",
            budget_spec_type=Spec,
            durable_sample_type=DurableSample,
            durable_error_type=ValueError,
            require_journal_authority=require_authority,
            journal_authority_scope=lambda store, identity: nullcontext(),
            plan_loader=plan_loader,
            durable_samples_loader=sample_loader,
            binding_from_sample=binding_from_sample,
            binding_factory=binding_factory,
            clock_contract_id="clock-contract",
        )

        result = binder(
            object(),
            current_spec,
            declared_plan_id="plan-1",
            measurement=measurement,
        )

        self.assertEqual(result.target_host_measurement_digest, measurement_digest)
        self.assertEqual(result.declared_plan_digest, plan_digest)
        self.assertEqual(result.bindings, ("binding-1",))
        self.assertEqual(captured_kwargs["clock_contract_id"], "clock-contract")
        self.assertEqual(require_authority.call_count, 2)
        plan_loader.assert_called_once()
        sample_loader.assert_called_once()
        binding_from_sample.assert_called_once_with(
            durable_sample,
            target_sample_id="sample-1",
        )

    def test_low_level_factory_rejects_pre_313_before_measurement_or_journal(self) -> None:
        snapshotter = Mock()
        require_authority = Mock()
        binder = _build_low_level_durable_financial_authority(
            python_version=(3, 12),
            measurement_type=object,
            measurement_snapshotter=snapshotter,
            target_clock_id="clock",
            budget_spec_type=object,
            durable_sample_type=object,
            durable_error_type=ValueError,
            require_journal_authority=require_authority,
            journal_authority_scope=lambda store, identity: nullcontext(),
            plan_loader=Mock(),
            durable_samples_loader=Mock(),
            binding_from_sample=Mock(),
            binding_factory=Mock(),
            clock_contract_id="clock-contract",
        )

        with self.assertRaisesRegex(ValueError, "before Python 3.13"):
            binder(
                object(),
                object(),
                declared_plan_id="not-read",
                measurement=object(),
            )

        snapshotter.assert_not_called()
        require_authority.assert_not_called()

    def test_release_factory_uses_only_injected_direct_dependencies(self) -> None:
        class Measurement:
            release_artifact_id = RELEASE_ID
            release_artifact_sha256 = RELEASE_SHA

        class DurableError(Exception):
            pass

        current = Measurement()
        snapshotter = Mock(return_value=current)
        parent = Mock()
        expected = object()
        mechanics = Mock(return_value=expected)

        binder = _build_release_bound_durable_financial_authority(
            measurement_type=Measurement,
            measurement_snapshotter=snapshotter,
            uuid_type=__import__("uuid").UUID,
            durable_error_type=DurableError,
            parent_collector=parent,
            parent_error_types=(ValueError,),
            durable_binder=mechanics,
        )

        result = binder(
            store=object(),
            spec=object(),
            campaign_plan=object(),
            campaign_cut=object(),
            declared_plan_id="plan-1",
            measurement=current,
            expected_release_artifact_id=RELEASE_ID,
            expected_release_artifact_sha256=RELEASE_SHA,
        )

        self.assertIs(result, expected)
        snapshotter.assert_called_once_with(current)
        parent.assert_called_once()
        mechanics.assert_called_once()

    def test_invalid_release_identity_fails_before_parent_or_mechanics(self) -> None:
        class Measurement:
            release_artifact_id = RELEASE_ID
            release_artifact_sha256 = RELEASE_SHA

        class DurableError(Exception):
            pass

        parent = Mock()
        mechanics = Mock()
        binder = _build_release_bound_durable_financial_authority(
            measurement_type=Measurement,
            measurement_snapshotter=lambda value: value,
            uuid_type=__import__("uuid").UUID,
            durable_error_type=DurableError,
            parent_collector=parent,
            parent_error_types=(ValueError,),
            durable_binder=mechanics,
        )

        with self.assertRaisesRegex(DurableError, "canonical UUID"):
            binder(
                store=object(),
                spec=object(),
                campaign_plan=object(),
                campaign_cut=object(),
                declared_plan_id="plan-1",
                measurement=Measurement(),
                expected_release_artifact_id="not-a-uuid",
                expected_release_artifact_sha256=RELEASE_SHA,
            )

        parent.assert_not_called()
        mechanics.assert_not_called()

    def test_invalid_release_digest_fails_before_parent_or_mechanics(self) -> None:
        class Measurement:
            release_artifact_id = RELEASE_ID
            release_artifact_sha256 = RELEASE_SHA

        class DurableError(Exception):
            pass

        parent = Mock()
        mechanics = Mock()
        binder = _build_release_bound_durable_financial_authority(
            measurement_type=Measurement,
            measurement_snapshotter=lambda value: value,
            uuid_type=__import__("uuid").UUID,
            durable_error_type=DurableError,
            parent_collector=parent,
            parent_error_types=(ValueError,),
            durable_binder=mechanics,
        )

        with self.assertRaisesRegex(DurableError, "canonical sha256"):
            binder(
                store=object(),
                spec=object(),
                campaign_plan=object(),
                campaign_cut=object(),
                declared_plan_id="plan-1",
                measurement=Measurement(),
                expected_release_artifact_id=RELEASE_ID,
                expected_release_artifact_sha256="sha256:" + "G" * 64,
            )

        parent.assert_not_called()
        mechanics.assert_not_called()

    def test_low_level_production_authority_retains_direct_dependencies_after_rebind(self) -> None:
        before = getclosurevars(
            bind_sealed_durable_financial_latency_to_target_host_measurement
        ).nonlocals
        original_snapshotter = measurement_module.snapshot_target_host_measurement
        original_require = persistence.require_exact_journal_store_authority
        original_scope = persistence.journal_store_authority_scope
        original_plan_loader = runtime_load_plan.load_declared_runtime_event_plan
        original_samples_loader = (
            runtime_load_measurement.load_declared_financial_latency_samples
        )
        original_binding_helper = durable._binding_from_durable_sample

        self.assertIs(before["measurement_snapshotter"], original_snapshotter)
        self.assertIs(before["require_journal_authority"], original_require)
        self.assertIs(before["journal_authority_scope"], original_scope)
        self.assertIs(before["plan_loader"], original_plan_loader)
        self.assertIs(before["durable_samples_loader"], original_samples_loader)
        self.assertIs(before["binding_from_sample"], original_binding_helper)

        forged_snapshotter = Mock()
        forged_require = Mock()
        forged_scope = Mock()
        forged_plan_loader = Mock()
        forged_samples_loader = Mock()
        forged_binding_helper = Mock()
        with (
            patch.object(
                measurement_module,
                "snapshot_target_host_measurement",
                forged_snapshotter,
            ),
            patch.object(
                persistence,
                "require_exact_journal_store_authority",
                forged_require,
            ),
            patch.object(
                persistence,
                "journal_store_authority_scope",
                forged_scope,
            ),
            patch.object(
                runtime_load_plan,
                "load_declared_runtime_event_plan",
                forged_plan_loader,
            ),
            patch.object(
                runtime_load_measurement,
                "load_declared_financial_latency_samples",
                forged_samples_loader,
            ),
            patch.object(
                durable,
                "_binding_from_durable_sample",
                forged_binding_helper,
            ),
        ):
            after = getclosurevars(
                bind_sealed_durable_financial_latency_to_target_host_measurement
            ).nonlocals
            self.assertIs(after["measurement_snapshotter"], original_snapshotter)
            self.assertIs(after["require_journal_authority"], original_require)
            self.assertIs(after["journal_authority_scope"], original_scope)
            self.assertIs(after["plan_loader"], original_plan_loader)
            self.assertIs(after["durable_samples_loader"], original_samples_loader)
            self.assertIs(after["binding_from_sample"], original_binding_helper)

        for forged in (
            forged_snapshotter,
            forged_require,
            forged_scope,
            forged_plan_loader,
            forged_samples_loader,
            forged_binding_helper,
        ):
            forged.assert_not_called()

    def test_release_production_authority_captures_sealed_low_level_mechanics(self) -> None:
        before = getclosurevars(
            bind_sealed_release_bound_durable_financial_latency_to_target_host_measurement
        ).nonlocals
        original_snapshotter = measurement_module.snapshot_target_host_measurement
        original_parent = (
            measurement_authority.collect_release_bound_target_host_evidence
        )
        sealed_mechanics = (
            bind_sealed_durable_financial_latency_to_target_host_measurement
        )
        self.assertIs(before["measurement_snapshotter"], original_snapshotter)
        self.assertIs(before["parent_collector"], original_parent)
        self.assertIs(before["durable_binder"], sealed_mechanics)

        forged_snapshotter = Mock()
        forged_parent = Mock()
        forged_public_mechanics = Mock()
        with (
            patch.object(
                measurement_module,
                "snapshot_target_host_measurement",
                forged_snapshotter,
            ),
            patch.object(
                measurement_authority,
                "collect_release_bound_target_host_evidence",
                forged_parent,
            ),
            patch.object(
                durable,
                "bind_durable_financial_latency_to_target_host_measurement",
                forged_public_mechanics,
            ),
        ):
            after = getclosurevars(
                bind_sealed_release_bound_durable_financial_latency_to_target_host_measurement
            ).nonlocals
            self.assertIs(after["measurement_snapshotter"], original_snapshotter)
            self.assertIs(after["parent_collector"], original_parent)
            self.assertIs(after["durable_binder"], sealed_mechanics)

        forged_snapshotter.assert_not_called()
        forged_parent.assert_not_called()
        forged_public_mechanics.assert_not_called()


if __name__ == "__main__":
    unittest.main()
