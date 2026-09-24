from datetime import datetime, timedelta, timezone
import unittest

from mvp.autotrade_mvp.capabilities import (
    CapabilityClaim,
    evaluate_capability,
    require_verified,
)


NOW = datetime(2026, 9, 24, 16, 0, tzinfo=timezone.utc)


def claim(kind, value="ALLOW", *, observed_delta=timedelta(minutes=-1), expiry_delta=timedelta(minutes=10)):
    return CapabilityClaim(
        kind=kind,
        value=value,
        observed_at=NOW + observed_delta,
        expires_at=NOW + expiry_delta,
        evidence_ref=f"evidence:{kind.lower()}:{value.lower()}",
    )


def full_claims():
    return tuple(claim(kind) for kind in ("DOCUMENTATION", "API", "ACCOUNT", "INSTRUMENT"))


def evaluate(claims):
    return evaluate_capability(
        provider_id="provider-a",
        account_id="account-a",
        instrument_id="instrument-a",
        action="ORDER.SUBMIT",
        evaluated_at=NOW,
        claims=claims,
    )


class CapabilityFoundationTests(unittest.TestCase):
    def test_all_current_layers_must_allow_before_verified(self):
        snapshot = evaluate(full_claims())
        self.assertEqual(snapshot.status, "VERIFIED")
        self.assertEqual(snapshot.reason_codes, ())
        self.assertEqual(snapshot.valid_until, NOW + timedelta(minutes=10))
        require_verified(snapshot, action="ORDER.SUBMIT", at=NOW)

    def test_missing_account_evidence_fails_closed(self):
        snapshot = evaluate(tuple(
            claim(kind) for kind in ("DOCUMENTATION", "API", "INSTRUMENT")
        ))
        self.assertEqual(snapshot.status, "UNKNOWN")
        self.assertIn("CAPABILITY.MISSING_ACCOUNT", snapshot.reason_codes)
        with self.assertRaises(PermissionError):
            require_verified(snapshot, action="ORDER.SUBMIT", at=NOW)

    def test_expired_evidence_is_not_reused(self):
        claims = list(full_claims())
        claims[2] = claim(
            "ACCOUNT",
            observed_delta=timedelta(minutes=-10),
            expiry_delta=timedelta(seconds=-1),
        )
        snapshot = evaluate(claims)
        self.assertEqual(snapshot.status, "UNKNOWN")
        self.assertIn("CAPABILITY.EXPIRED_ACCOUNT", snapshot.reason_codes)

    def test_expired_historical_claim_does_not_override_newer_live_evidence(self):
        claims = list(full_claims())
        claims.append(claim(
            "ACCOUNT",
            observed_delta=timedelta(minutes=-20),
            expiry_delta=timedelta(minutes=-10),
        ))
        snapshot = evaluate(claims)
        self.assertEqual(snapshot.status, "VERIFIED")
        self.assertEqual(snapshot.reason_codes, ())

    def test_any_current_deny_blocks_matching_action(self):
        claims = list(full_claims())
        claims[3] = claim("INSTRUMENT", "DENY")
        snapshot = evaluate(claims)
        self.assertEqual(snapshot.status, "DENIED")
        self.assertIn("CAPABILITY.DENIED_INSTRUMENT", snapshot.reason_codes)

    def test_conflicting_current_evidence_is_not_resolved_by_preference(self):
        claims = list(full_claims())
        claims.append(claim("ACCOUNT", "DENY"))
        snapshot = evaluate(claims)
        self.assertEqual(snapshot.status, "CONFLICT")
        self.assertIn("CAPABILITY.CONFLICT_ACCOUNT", snapshot.reason_codes)

    def test_future_dated_evidence_is_a_conflict(self):
        claims = list(full_claims())
        claims[1] = claim(
            "API",
            observed_delta=timedelta(minutes=1),
            expiry_delta=timedelta(minutes=20),
        )
        snapshot = evaluate(claims)
        self.assertEqual(snapshot.status, "CONFLICT")
        self.assertIn("EVIDENCE.FUTURE", snapshot.reason_codes)

    def test_action_binding_prevents_capability_coercion(self):
        snapshot = evaluate(full_claims())
        with self.assertRaises(PermissionError):
            require_verified(snapshot, action="ORDER.CANCEL", at=NOW)

    def test_verified_snapshot_expires_at_earliest_claim(self):
        claims = list(full_claims())
        claims[0] = claim("DOCUMENTATION", expiry_delta=timedelta(minutes=2))
        snapshot = evaluate(claims)
        self.assertEqual(snapshot.status, "VERIFIED")
        self.assertEqual(snapshot.valid_until, NOW + timedelta(minutes=2))
        with self.assertRaises(PermissionError):
            require_verified(
                snapshot,
                action="ORDER.SUBMIT",
                at=NOW + timedelta(minutes=2),
            )


if __name__ == "__main__":
    unittest.main()
