from decimal import Decimal
import unittest

from mvp.autotrade_mvp.readiness import (
    ReadinessError,
    RuntimeMode,
    RuntimeSafetySignals,
    evaluate_readiness,
)


def healthy(**overrides):
    value = {
        "journal_writable": True,
        "emergency_disk_reserve_available": True,
        "schema_compatible": True,
        "provider_authenticated": True,
        "market_data_fresh": True,
        "sender_ownership_proven": True,
        "old_sender_fenced": True,
        "provider_native_protection_present": True,
        "emergency_execution_path_qualified": True,
        "unknown_send_count": 0,
        "reconciliation_lag_seconds": "1",
        "maximum_reconciliation_lag_seconds": "5",
        "clock_skew_seconds": "0.1",
        "maximum_clock_skew_seconds": "1",
        "unresolved_external_uncertainty": False,
        "recovery_in_progress": False,
    }
    value.update(overrides)
    return RuntimeSafetySignals(**value)


class RuntimeReadinessTests(unittest.TestCase):
    def test_fully_safe_runtime_is_ready_for_new_exposure(self):
        result = evaluate_readiness(healthy())
        self.assertTrue(result.live)
        self.assertTrue(result.ready)
        self.assertEqual(result.mode, RuntimeMode.READY)
        self.assertTrue(result.ready_for_new_exposure)
        self.assertEqual(result.blockers, ())

    def test_every_material_uncertainty_prevents_false_ready(self):
        cases = (
            ("journal_writable", False, "journal_not_writable"),
            (
                "emergency_disk_reserve_available",
                False,
                "emergency_disk_reserve_unavailable",
            ),
            ("schema_compatible", False, "schema_incompatible"),
            ("provider_authenticated", False, "provider_not_authenticated"),
            ("market_data_fresh", False, "market_data_stale"),
            (
                "sender_ownership_proven",
                False,
                "sender_ownership_unproven",
            ),
            ("old_sender_fenced", False, "old_sender_not_fenced"),
            ("unknown_send_count", 1, "unknown_sends_present"),
            (
                "unresolved_external_uncertainty",
                True,
                "external_uncertainty_unresolved",
            ),
            ("recovery_in_progress", True, "recovery_in_progress"),
        )
        for field, value, code in cases:
            with self.subTest(field=field):
                result = evaluate_readiness(healthy(**{field: value}))
                self.assertFalse(result.ready)
                self.assertFalse(result.ready_for_new_exposure)
                self.assertIn(code, result.blockers)

    def test_clock_and_reconciliation_thresholds_are_exact(self):
        at_limits = evaluate_readiness(
            healthy(
                clock_skew_seconds="1",
                maximum_clock_skew_seconds="1",
                reconciliation_lag_seconds="5",
                maximum_reconciliation_lag_seconds="5",
            )
        )
        self.assertTrue(at_limits.ready)

        over = evaluate_readiness(
            healthy(
                clock_skew_seconds="1.000000000000000001",
                reconciliation_lag_seconds="5.000000000000000001",
            )
        )
        self.assertFalse(over.ready)
        self.assertIn("clock_skew_exceeded", over.blockers)
        self.assertIn("reconciliation_lag_exceeded", over.blockers)

    def test_float_timing_inputs_are_rejected(self):
        with self.assertRaisesRegex(ReadinessError, "exact decimal"):
            healthy(clock_skew_seconds=0.1)
        with self.assertRaisesRegex(ReadinessError, "exact decimal"):
            healthy(reconciliation_lag_seconds=1.0)

    def test_lease_like_ownership_without_external_fencing_never_becomes_ready(self):
        result = evaluate_readiness(
            healthy(
                sender_ownership_proven=True,
                old_sender_fenced=False,
            )
        )
        self.assertFalse(result.ready)
        self.assertIn("old_sender_not_fenced", result.blockers)
        self.assertFalse(result.protection_only_available)

    def test_disk_failure_can_be_protection_only_but_never_ready(self):
        result = evaluate_readiness(
            healthy(
                journal_writable=False,
                emergency_disk_reserve_available=True,
                provider_native_protection_present=True,
            )
        )
        self.assertEqual(result.mode, RuntimeMode.PROTECTION_ONLY)
        self.assertFalse(result.ready_for_new_exposure)
        self.assertTrue(result.protection_only_available)

    def test_unknown_send_stays_visible_even_when_provider_protection_exists(self):
        result = evaluate_readiness(
            healthy(
                unknown_send_count=2,
                unresolved_external_uncertainty=True,
            )
        )
        self.assertEqual(result.mode, RuntimeMode.PROTECTION_ONLY)
        self.assertIn("unknown_sends_present", result.blockers)
        self.assertIn("external_uncertainty_unresolved", result.blockers)

    def test_schema_incompatibility_is_not_even_read_ready(self):
        result = evaluate_readiness(
            healthy(
                schema_compatible=False,
                provider_native_protection_present=False,
                emergency_execution_path_qualified=False,
            )
        )
        self.assertFalse(result.ready_for_read)
        self.assertEqual(result.mode, RuntimeMode.DEGRADED)
        self.assertIn("provider_native_protection_absent", result.warnings)
        self.assertIn("emergency_execution_path_unqualified", result.warnings)

    def test_no_protection_path_yields_degraded_not_protection_only(self):
        result = evaluate_readiness(
            healthy(
                market_data_fresh=False,
                provider_native_protection_present=False,
                emergency_execution_path_qualified=False,
            )
        )
        self.assertEqual(result.mode, RuntimeMode.DEGRADED)
        self.assertFalse(result.protection_only_available)

    def test_boolean_and_count_fields_fail_closed_on_truthy_values(self):
        with self.assertRaisesRegex(ReadinessError, "boolean"):
            healthy(journal_writable=1)
        with self.assertRaisesRegex(ReadinessError, "non-negative integer"):
            healthy(unknown_send_count=True)
        with self.assertRaisesRegex(ReadinessError, "non-negative integer"):
            healthy(unknown_send_count=-1)


if __name__ == "__main__":
    unittest.main()
