"""Adversarial and NVDA-readable qualification for read-only operator diagnostics."""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
from types import MappingProxyType
import unittest

from mvp.autotrade_mvp.operator_observability import build_operator_observability
from mvp.autotrade_mvp.readiness import RuntimeSafetySignals
from mvp.autotrade_mvp.recovery import HostState, RecoveryController


def _signals(**overrides):
    values = dict(
        journal_writable=True,
        emergency_disk_reserve_available=True,
        schema_compatible=True,
        provider_authenticated=True,
        provider_reconciled=True,
        market_data_fresh=True,
        sender_ownership_proven=True,
        old_sender_fenced=True,
        provider_native_protection_present=True,
        emergency_execution_path_qualified=True,
        protection_required_for_new_exposure=False,
        new_exposure_protection_path_qualified=True,
        unknown_send_count=0,
        reconciliation_lag_seconds=Decimal("0"),
        maximum_reconciliation_lag_seconds=Decimal("30"),
        clock_skew_seconds=Decimal("0"),
        maximum_clock_skew_seconds=Decimal("2"),
        unresolved_external_uncertainty=False,
        recovery_in_progress=False,
    )
    values.update(overrides)
    return RuntimeSafetySignals(**values)


def _snapshot():
    return dict(
        state_version="1",
        event_cursor="0",
        server_time="2026-10-08T20:00:00Z",
        host_id="host-fixture",
        account_id="account-fixture",
        environment="PAPER",
        permission_summary=dict(actor="operator", session="sid-" + "a" * 64, role="OWNER"),
        connection_freshness=dict(host="CURRENT"),
        portfolio=dict(
            orders=[dict(client_order_id="private-order", api_secret="secret-order")],
            fills=[dict(fill_id="private-fill", access_token="secret-fill")],
            note="Bearer super-secret-provider-bearer",
        ),
        risk=dict(active_reservations=[dict(secret="secret-reservation")]),
        strategy=dict(model=dict(api_key="secret-model"), decisions=["secret-decision"]),
        jobs=[dict(password="secret-job")],
        reason_codes=[],
    )


def _recovery(state):
    result = RecoveryController()
    result.state = state
    return result


class OperatorObservabilityTests(unittest.TestCase):
    def test_ready_requires_both_canonical_recovery_and_readiness(self):
        snapshot = _snapshot()
        original = deepcopy(snapshot)
        view = build_operator_observability(
            ui_snapshot=snapshot, recovery=_recovery(HostState.READY), signals=_signals()
        )
        self.assertEqual(view.mode, "READY")
        self.assertTrue(view.as_dict()["domains"]["readiness"]["new_exposure_allowed"])
        self.assertEqual(view.as_dict()["financial_authority"], "NONE")
        self.assertEqual(snapshot, original)
        text = view.to_text()
        self.assertIn("Mode: READY", text)
        self.assertIn("Financial authority: NONE", text)
        for domain in ("health", "readiness", "recovery", "risk", "reservations",
                       "orders", "unknown", "portfolio", "jobs", "model", "evidence"):
            self.assertIn(domain + ":", text)

    def test_all_four_recovery_modes_are_explicit(self):
        expected = (
            (HostState.BLOCKED, "BLOCKED"),
            (HostState.STOPPED, "BLOCKED"),
            (HostState.RECOVERING, "RECOVERING"),
            (HostState.DEGRADED, "DEGRADED"),
            (HostState.READY, "READY"),
        )
        for state, mode in expected:
            with self.subTest(state=state):
                actual = build_operator_observability(
                    ui_snapshot=_snapshot(), recovery=_recovery(state), signals=_signals()
                )
                self.assertEqual(actual.mode, mode)

    def test_unknown_sends_and_uncertain_external_state_cannot_report_ready(self):
        actual = build_operator_observability(
            ui_snapshot=_snapshot(),
            recovery=_recovery(HostState.READY),
            signals=_signals(unknown_send_count=2, unresolved_external_uncertainty=True),
        )
        self.assertEqual(actual.mode, "DEGRADED")
        self.assertFalse(actual.as_dict()["domains"]["readiness"]["new_exposure_allowed"])
        self.assertEqual(actual.as_dict()["domains"]["unknown"]["send_count"], 2)
        self.assertIn("unknown_sends_present", actual.reasons)
        self.assertIn("external_uncertainty_unresolved", actual.reasons)

    def test_recovery_not_ready_cannot_be_overridden_by_positive_readiness(self):
        for state in (HostState.BLOCKED, HostState.RECOVERING, HostState.DEGRADED):
            actual = build_operator_observability(
                ui_snapshot=_snapshot(), recovery=_recovery(state), signals=_signals()
            )
            self.assertNotEqual(actual.mode, "READY")
            self.assertFalse(actual.as_dict()["domains"]["readiness"]["new_exposure_allowed"])

    def test_original_private_payloads_never_enter_accessible_text_or_json(self):
        snapshot = _snapshot()
        view = build_operator_observability(
            ui_snapshot=snapshot, recovery=_recovery(HostState.READY), signals=_signals()
        )
        text = view.to_text() + str(view.as_dict())
        for secret in (
            "private-order", "secret-order", "private-fill", "secret-fill",
            "secret-reservation", "secret-model", "secret-job",
            "secret-decision", "super-secret-provider-bearer",
            "sid-" + "a" * 64,
        ):
            self.assertNotIn(secret, text)
        domains = view.as_dict()["domains"]
        self.assertEqual(domains["orders"]["count"], 1)
        self.assertEqual(domains["reservations"]["count"], 1)
        self.assertEqual(domains["portfolio"]["fill_count"], 1)
        self.assertEqual(domains["jobs"]["count"], 1)
        self.assertEqual(domains["model"]["qualification"], "NOT_ESTABLISHED")
        self.assertFalse(domains["evidence"]["science_pass"])

    def test_unavailable_source_is_not_invented_as_zero(self):
        snapshot = _snapshot()
        del snapshot["portfolio"]["orders"]
        del snapshot["risk"]["active_reservations"]
        view = build_operator_observability(
            ui_snapshot=snapshot, recovery=_recovery(HostState.RECOVERING), signals=_signals()
        ).as_dict()
        self.assertIsNone(view["domains"]["orders"]["count"])
        self.assertEqual(view["domains"]["orders"]["source"], "UNAVAILABLE")
        self.assertIsNone(view["domains"]["reservations"]["count"])

    def test_bad_sources_fail_closed_before_untrusted_container_callbacks(self):
        class HostileDict(dict):
            callbacks = 0
            def items(self):
                type(self).callbacks += 1
                raise AssertionError("untrusted items invoked")
            __iter__ = items

        hostile = HostileDict(_snapshot())
        with self.assertRaisesRegex(TypeError, "exact Host mapping"):
            build_operator_observability(
                ui_snapshot=hostile, recovery=_recovery(HostState.READY), signals=_signals()
            )
        self.assertEqual(HostileDict.callbacks, 0)

        snapshot = _snapshot()
        snapshot["portfolio"] = HostileDict(snapshot["portfolio"])
        with self.assertRaisesRegex(TypeError, "exact Host mapping"):
            build_operator_observability(
                ui_snapshot=snapshot, recovery=_recovery(HostState.READY), signals=_signals()
            )
        self.assertEqual(HostileDict.callbacks, 0)

    def test_nested_secret_subclasses_never_need_inspection(self):
        class ExplosiveSecret:
            def __str__(self):
                raise AssertionError("caller secret __str__ executed")
        snapshot = _snapshot()
        snapshot["portfolio"]["orders"][0]["token"] = ExplosiveSecret()
        view = build_operator_observability(
            ui_snapshot=snapshot, recovery=_recovery(HostState.READY), signals=_signals()
        )
        self.assertEqual(view.as_dict()["domains"]["orders"]["count"], 1)

    def test_malformed_snapshot_and_secret_shaped_reason_are_rejected(self):
        snapshot = _snapshot()
        snapshot["reason_codes"] = ["Bearer secret provider credential"]
        with self.assertRaisesRegex(ValueError, "nonsecret"):
            build_operator_observability(
                ui_snapshot=snapshot, recovery=_recovery(HostState.READY), signals=_signals()
            )
        snapshot = _snapshot()
        snapshot["jobs"] = (dict(kind="work"),)
        with self.assertRaisesRegex(ValueError, "bounded exact array"):
            build_operator_observability(
                ui_snapshot=snapshot, recovery=_recovery(HostState.READY), signals=_signals()
            )
        snapshot = _snapshot()
        snapshot["extra"] = "UNEXPECTED"
        with self.assertRaisesRegex(ValueError, "canonical Host contract"):
            build_operator_observability(
                ui_snapshot=snapshot, recovery=_recovery(HostState.READY), signals=_signals()
            )

    def test_out_of_budget_arrays_are_rejected_without_item_materialization(self):
        snapshot = _snapshot()
        snapshot["portfolio"]["orders"] = [0] * 10001
        with self.assertRaisesRegex(ValueError, "item budget"):
            build_operator_observability(
                ui_snapshot=snapshot, recovery=_recovery(HostState.READY), signals=_signals()
            )

    def test_authentic_mappingproxy_is_supported_for_host_snapshot(self):
        view = build_operator_observability(
            ui_snapshot=MappingProxyType(_snapshot()),
            recovery=_recovery(HostState.READY), signals=_signals()
        )
        self.assertEqual(view.mode, "READY")


if __name__ == "__main__":
    unittest.main()
