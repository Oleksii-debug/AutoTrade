import unittest

from mvp.autotrade_mvp.bounded_real import (
    BoundedRealEnvelope,
    BoundedRealRequest,
    BoundedRealState,
    admit_bounded_real,
)


class BoundedRealTests(unittest.TestCase):
    def envelope(self, **changes):
        values = dict(
            authorization_id="auth-1",
            owner_confirmation_id="owner-confirm-1",
            owner_confirmed=True,
            release_candidate_hash="release-hash",
            forward_evidence_hash="forward-hash",
            provider_id="ALPACA",
            account_fingerprint="acct-hash",
            allowed_assets=frozenset({"EQUITY"}),
            max_order_notional="100",
            max_total_notional="250",
            max_daily_loss="25",
            max_orders=3,
            expires_at="2026-09-25T00:00:00Z",
        )
        values.update(changes)
        return BoundedRealEnvelope(**values)

    def state(self, **changes):
        values = dict(
            provider_id="ALPACA",
            account_fingerprint="acct-hash",
            current_total_notional="50",
            realized_daily_loss="5",
            orders_already_sent=1,
            reconciliation_fresh=True,
            kill_switch_ready=True,
            release_qualified=True,
            forward_qualified=True,
        )
        values.update(changes)
        return BoundedRealState(**values)

    def request(self, **changes):
        values = dict(asset_family="EQUITY", requested_notional="75", now="2026-09-24T18:00:00Z")
        values.update(changes)
        return BoundedRealRequest(**values)

    def test_valid_envelope_only_admits_with_all_hard_gates(self):
        result = admit_bounded_real(self.envelope(), self.state(), self.request())
        self.assertTrue(result.allowed)
        self.assertEqual(result.reasons, ())

    def test_owner_confirmation_is_structurally_required(self):
        with self.assertRaisesRegex(ValueError, "explicit owner confirmation"):
            admit_bounded_real(self.envelope(owner_confirmed=False), self.state(), self.request())

    def test_caps_are_independent_and_fail_closed(self):
        result = admit_bounded_real(
            self.envelope(),
            self.state(current_total_notional="200", realized_daily_loss="25", orders_already_sent=3),
            self.request(requested_notional="101"),
        )
        self.assertFalse(result.allowed)
        self.assertIn("per_order_cap_exceeded", result.reasons)
        self.assertIn("total_notional_cap_exceeded", result.reasons)
        self.assertIn("daily_loss_cap_reached", result.reasons)
        self.assertIn("order_count_cap_reached", result.reasons)

    def test_stale_reconciliation_or_missing_kill_switch_blocks(self):
        result = admit_bounded_real(
            self.envelope(),
            self.state(reconciliation_fresh=False, kill_switch_ready=False),
            self.request(),
        )
        self.assertFalse(result.allowed)
        self.assertIn("reconciliation_not_fresh", result.reasons)
        self.assertIn("kill_switch_not_ready", result.reasons)

    def test_release_and_forward_evidence_cannot_be_substituted(self):
        result = admit_bounded_real(
            self.envelope(),
            self.state(release_qualified=False, forward_qualified=False),
            self.request(),
        )
        self.assertFalse(result.allowed)
        self.assertIn("release_not_qualified", result.reasons)
        self.assertIn("forward_not_qualified", result.reasons)

    def test_expired_or_wrong_account_authority_blocks(self):
        result = admit_bounded_real(
            self.envelope(),
            self.state(account_fingerprint="other"),
            self.request(now="2026-09-25T00:00:00Z"),
        )
        self.assertFalse(result.allowed)
        self.assertIn("authorization_expired", result.reasons)
        self.assertIn("account_mismatch", result.reasons)


if __name__ == "__main__":
    unittest.main()
