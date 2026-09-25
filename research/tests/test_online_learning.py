from datetime import datetime, timedelta, timezone
from decimal import Decimal
import unittest

from research.autotrade_research.learning.online import (
    OnlineUpdateEnvelope,
    OnlineUpdateInput,
    ParameterRule,
    evaluate_online_update,
)


H = "sha256:" + "a" * 64
NOW = datetime(2026, 9, 25, 0, 0, tzinfo=timezone.utc)


def envelope(**overrides):
    values = dict(
        envelope_id="env-1",
        champion_artifact_hash=H,
        parameter_rules=[
            ParameterRule.create(name="alpha", minimum="0", maximum="1", max_absolute_step="0.10"),
            ParameterRule.create(name="beta", minimum="-1", maximum="1", max_absolute_step="0.20"),
        ],
        eligible_label_versions=["labels-v1"],
        min_seconds_between_updates=60,
        max_updates_per_window=4,
        max_compute_units_per_update="10",
        max_drift_score="0.25",
    )
    values.update(overrides)
    return OnlineUpdateEnvelope.create(**values)


def update(**overrides):
    values = dict(
        current_parameters={"alpha": Decimal("0.40"), "beta": Decimal("0.10")},
        proposed_parameters={"alpha": Decimal("0.45"), "beta": Decimal("0.20")},
        label_version="labels-v1",
        label_available_at=NOW - timedelta(minutes=1),
        outcome_horizon_at=NOW - timedelta(minutes=2),
        execution_reconciled_at=NOW - timedelta(seconds=90),
        observed_at=NOW,
        last_update_at=NOW - timedelta(minutes=5),
        updates_in_window=1,
        reserved_compute_units="3",
        drift_score="0.10",
    )
    values.update(overrides)
    return OnlineUpdateInput.create(**values)


class OnlineLearningEnvelopeTests(unittest.TestCase):
    def test_factory_rejects_normalized_parameter_name_collision(self):
        with self.assertRaisesRegex(ValueError, "duplicate normalized current"):
            update(
                current_parameters={
                    "alpha": Decimal("0.40"),
                    " alpha ": Decimal("0.99"),
                    "beta": Decimal("0.10"),
                },
            )

    def test_direct_online_update_cannot_bypass_causal_label_invariants(self):
        values = dict(
            current_parameters={"alpha": Decimal("0.40"), "beta": Decimal("0.10")},
            proposed_parameters={"alpha": Decimal("0.45"), "beta": Decimal("0.20")},
            label_version="labels-v1",
            label_available_at=NOW + timedelta(seconds=1),
            outcome_horizon_at=NOW - timedelta(minutes=2),
            execution_reconciled_at=NOW - timedelta(seconds=90),
            observed_at=NOW,
            last_update_at=NOW - timedelta(minutes=5),
            updates_in_window=1,
            reserved_compute_units=Decimal("3"),
            drift_score=Decimal("0.10"),
        )
        with self.assertRaisesRegex(ValueError, "after observed_at"):
            OnlineUpdateInput(**values)

    def test_direct_online_objects_cannot_bypass_exact_type_and_identity_rules(self):
        with self.assertRaises(TypeError):
            ParameterRule(
                name="alpha",
                minimum=0.0,
                maximum=Decimal("1"),
                max_absolute_step=Decimal("0.1"),
            )
        with self.assertRaisesRegex(ValueError, "sha256"):
            OnlineUpdateEnvelope(
                envelope_id="env-1",
                champion_artifact_hash="not-a-hash",
                parameter_rules=(
                    ParameterRule.create(
                        name="alpha",
                        minimum="0",
                        maximum="1",
                        max_absolute_step="0.1",
                    ),
                ),
                eligible_label_versions=("labels-v1",),
                min_seconds_between_updates=60,
                max_updates_per_window=4,
                max_compute_units_per_update=Decimal("10"),
                max_drift_score=Decimal("0.25"),
            )
        with self.assertRaisesRegex(TypeError, "risk_envelope_violated"):
            OnlineUpdateInput(
                current_parameters={"alpha": Decimal("0.4")},
                proposed_parameters={"alpha": Decimal("0.5")},
                label_version="labels-v1",
                label_available_at=NOW - timedelta(minutes=1),
                outcome_horizon_at=NOW - timedelta(minutes=2),
                execution_reconciled_at=NOW - timedelta(seconds=90),
                observed_at=NOW,
                last_update_at=None,
                updates_in_window=0,
                reserved_compute_units=Decimal("1"),
                drift_score=Decimal("0.1"),
                risk_envelope_violated="false",
            )

    def test_update_inside_registered_envelope_is_allowed_without_trading_authority(self):
        result = evaluate_online_update(envelope(), update())
        self.assertEqual(result.status, "ALLOW")
        self.assertFalse(result.grants_trading_authority)
        self.assertEqual(result.reasons, ())
        self.assertTrue(result.decision_id.startswith("online-update-"))

    def test_parameter_range_crossing_requires_new_candidate(self):
        result = evaluate_online_update(
            envelope(),
            update(proposed_parameters={"alpha": Decimal("1.01"), "beta": Decimal("0.20")}),
        )
        self.assertEqual(result.status, "CANDIDATE_REQUIRED")
        self.assertIn("LEARNING.PARAMETER_RANGE_EXCEEDED:alpha", result.reasons)

    def test_parameter_step_crossing_requires_new_candidate(self):
        result = evaluate_online_update(
            envelope(),
            update(proposed_parameters={"alpha": Decimal("0.55"), "beta": Decimal("0.20")}),
        )
        self.assertEqual(result.status, "CANDIDATE_REQUIRED")
        self.assertIn("LEARNING.PARAMETER_STEP_EXCEEDED:alpha", result.reasons)

    def test_new_parameter_cannot_be_smuggled_into_online_update(self):
        result = evaluate_online_update(
            envelope(),
            update(
                current_parameters={"alpha": Decimal("0.40"), "beta": Decimal("0.10")},
                proposed_parameters={
                    "alpha": Decimal("0.45"),
                    "beta": Decimal("0.20"),
                    "gamma": Decimal("1"),
                },
            ),
        )
        self.assertEqual(result.status, "CANDIDATE_REQUIRED")
        self.assertIn("LEARNING.PARAMETER_SET_CHANGED", result.reasons)
        self.assertIn("LEARNING.UNAUTHORIZED_PARAMETER", result.reasons)

    def test_unregistered_label_requires_candidate_requalification(self):
        result = evaluate_online_update(envelope(), update(label_version="labels-v2"))
        self.assertEqual(result.status, "CANDIDATE_REQUIRED")
        self.assertIn("LEARNING.LABEL_OUTSIDE_ENVELOPE", result.reasons)

    def test_drift_limit_stops_online_updates(self):
        result = evaluate_online_update(envelope(), update(drift_score="0.251"))
        self.assertEqual(result.status, "STOP")
        self.assertIn("LEARNING.DRIFT_LIMIT_EXCEEDED", result.reasons)

    def test_risk_or_operational_violation_stops_even_if_parameter_change_needs_candidate(self):
        result = evaluate_online_update(
            envelope(),
            update(
                proposed_parameters={"alpha": Decimal("2"), "beta": Decimal("0.20")},
                risk_envelope_violated=True,
            ),
        )
        self.assertEqual(result.status, "STOP")
        self.assertIn("LEARNING.RISK_ENVELOPE_VIOLATED", result.reasons)
        self.assertIn("LEARNING.PARAMETER_RANGE_EXCEEDED:alpha", result.reasons)

    def test_compute_budget_and_frequency_are_hard_stops(self):
        result = evaluate_online_update(
            envelope(),
            update(reserved_compute_units="10.01", updates_in_window=4),
        )
        self.assertEqual(result.status, "STOP")
        self.assertIn("LEARNING.COMPUTE_BUDGET_EXCEEDED", result.reasons)
        self.assertIn("LEARNING.UPDATE_FREQUENCY_EXCEEDED", result.reasons)

    def test_minimum_update_interval_is_enforced(self):
        result = evaluate_online_update(
            envelope(),
            update(last_update_at=NOW - timedelta(seconds=59)),
        )
        self.assertEqual(result.status, "STOP")
        self.assertIn("LEARNING.UPDATE_INTERVAL_TOO_SHORT", result.reasons)

    def test_float_financial_or_control_values_fail_closed(self):
        with self.assertRaises(TypeError):
            ParameterRule.create(name="alpha", minimum=0.0, maximum="1", max_absolute_step="0.1")
        with self.assertRaises(TypeError):
            update(reserved_compute_units=1.5)

    def test_decision_identity_binds_exact_parameter_values(self):
        baseline = evaluate_online_update(envelope(), update())
        changed = evaluate_online_update(
            envelope(),
            update(proposed_parameters={"alpha": Decimal("0.46"), "beta": Decimal("0.20")}),
        )
        self.assertEqual(baseline.status, changed.status)
        self.assertNotEqual(baseline.decision_id, changed.decision_id)

    def test_decision_identity_binds_exact_envelope_policy(self):
        baseline = evaluate_online_update(envelope(max_drift_score="0.25"), update())
        revised = evaluate_online_update(envelope(max_drift_score="0.30"), update())
        self.assertEqual(baseline.status, revised.status)
        self.assertNotEqual(baseline.decision_id, revised.decision_id)

    def test_future_last_update_timestamp_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "last_update_at cannot be after observed_at"):
            update(last_update_at=NOW + timedelta(seconds=1))

    def test_future_label_is_rejected_before_online_update(self):
        with self.assertRaisesRegex(ValueError, "after observed_at"):
            update(label_available_at=NOW + timedelta(seconds=1))

    def test_label_cannot_mature_before_registered_outcome_horizon(self):
        with self.assertRaisesRegex(ValueError, "outcome horizon"):
            update(
                label_available_at=NOW - timedelta(minutes=2),
                outcome_horizon_at=NOW - timedelta(minutes=1),
            )

    def test_label_cannot_mature_before_execution_reconciliation(self):
        with self.assertRaisesRegex(ValueError, "execution reconciliation"):
            update(
                label_available_at=NOW - timedelta(minutes=2),
                outcome_horizon_at=NOW - timedelta(minutes=3),
                execution_reconciled_at=NOW - timedelta(minutes=1),
            )

    def test_decision_identity_binds_label_maturity_evidence_times(self):
        baseline = evaluate_online_update(envelope(), update())
        later_reconciliation = evaluate_online_update(
            envelope(),
            update(
                label_available_at=NOW - timedelta(seconds=30),
                execution_reconciled_at=NOW - timedelta(seconds=45),
            ),
        )
        self.assertEqual(baseline.status, later_reconciliation.status)
        self.assertNotEqual(baseline.decision_id, later_reconciliation.decision_id)


if __name__ == "__main__":
    unittest.main()
