import unittest

from mvp.autotrade_mvp.bounded_real import (
    BoundedRealEnvelope,
    BoundedRealObservations,
    QualificationEvidence,
    assess_bounded_real_qualification,
)


SHA = "a" * 40


def envelope(**overrides):
    values = dict(
        envelope_id="bounded-1",
        source_sha=SHA,
        account_id="account-1",
        provider_id="provider-1",
        policy_id="policy-1",
        allowed_actions={"ORDER.SUBMIT", "ORDER.CANCEL", "FLATTEN"},
        max_capital="1000",
        max_single_notional="100",
        max_gross_leverage="1.5",
    )
    values.update(overrides)
    return BoundedRealEnvelope.create(**values)


def evidence(kind, **overrides):
    values = dict(
        evidence_id="e-" + kind.lower(),
        evidence_kind=kind,
        source_sha=SHA,
        envelope_id="bounded-1",
        passed=True,
    )
    values.update(overrides)
    return QualificationEvidence.create(**values)


def prerequisites():
    return [
        evidence("RELEASE_CANDIDATE"),
        evidence("SCIENTIFIC_QUALIFICATION"),
        evidence("FORWARD_PAPER"),
        evidence("PROVIDER_CAPABILITY"),
        evidence("ACCESSIBILITY"),
        evidence("RECOVERY"),
    ]


def observations(**overrides):
    values = dict(
        source_sha=SHA,
        envelope_id="bounded-1",
        provider_id="provider-1",
        account_id="account-1",
        observed_fill_count=3,
        observed_partial_fill=True,
        all_fills_reconciled=True,
        fees_reconciled=True,
        revocation_verified=True,
        protection_verified=True,
        unauthorized_action_count=0,
        unresolved_unknown_count=0,
    )
    values.update(overrides)
    return BoundedRealObservations.create(**values)


class BoundedRealQualificationTests(unittest.TestCase):
    def test_complete_bundle_is_evidence_complete_but_never_authority(self):
        result = assess_bounded_real_qualification(
            envelope=envelope(),
            prerequisite_evidence=prerequisites(),
            observations=observations(),
        )
        self.assertTrue(result.complete)
        self.assertEqual(result.reason_codes, ())
        self.assertFalse(result.authorizes_trading)
        self.assertEqual(result.exact_source_sha, SHA)

    def test_missing_or_failed_prerequisite_fails_closed(self):
        missing = prerequisites()[:-1]
        result = assess_bounded_real_qualification(
            envelope=envelope(),
            prerequisite_evidence=missing,
            observations=observations(),
        )
        self.assertFalse(result.complete)
        self.assertTrue(
            any(reason.startswith("missing_prerequisite_evidence:") for reason in result.reason_codes)
        )

        failed = prerequisites()
        failed[1] = evidence("SCIENTIFIC_QUALIFICATION", passed=False)
        result = assess_bounded_real_qualification(
            envelope=envelope(),
            prerequisite_evidence=failed,
            observations=observations(),
        )
        self.assertIn(
            "prerequisite_failed:SCIENTIFIC_QUALIFICATION",
            result.reason_codes,
        )

    def test_exact_build_and_envelope_must_match_every_evidence_item(self):
        items = prerequisites()
        items[0] = evidence("RELEASE_CANDIDATE", source_sha="b" * 40)
        items[1] = evidence(
            "SCIENTIFIC_QUALIFICATION",
            envelope_id="different-envelope",
        )
        result = assess_bounded_real_qualification(
            envelope=envelope(),
            prerequisite_evidence=items,
            observations=observations(),
        )
        self.assertIn("source_sha_mismatch:RELEASE_CANDIDATE", result.reason_codes)
        self.assertIn(
            "envelope_mismatch:SCIENTIFIC_QUALIFICATION",
            result.reason_codes,
        )

    def test_actual_fill_reconciliation_revocation_and_protection_are_required(self):
        result = assess_bounded_real_qualification(
            envelope=envelope(),
            prerequisite_evidence=prerequisites(),
            observations=observations(
                observed_fill_count=0,
                observed_partial_fill=False,
                all_fills_reconciled=False,
                fees_reconciled=False,
                revocation_verified=False,
                protection_verified=False,
                unresolved_unknown_count=1,
            ),
        )
        self.assertFalse(result.complete)
        self.assertTrue(
            {
                "no_real_fill_evidence",
                "partial_fill_not_evidenced",
                "fills_not_fully_reconciled",
                "fees_not_reconciled",
                "revocation_not_verified",
                "protection_not_verified",
                "unresolved_submission_unknown",
            }
            <= set(result.reason_codes)
        )

    def test_any_unauthorized_action_blocks_qualification(self):
        result = assess_bounded_real_qualification(
            envelope=envelope(),
            prerequisite_evidence=prerequisites(),
            observations=observations(unauthorized_action_count=1),
        )
        self.assertEqual(result.reason_codes, ("unauthorized_action_observed",))
        self.assertFalse(result.complete)

    def test_observation_scope_must_match_exact_account_provider_build_and_envelope(self):
        result = assess_bounded_real_qualification(
            envelope=envelope(),
            prerequisite_evidence=prerequisites(),
            observations=observations(account_id="other-account"),
        )
        self.assertIn("observation_scope_mismatch", result.reason_codes)

    def test_withdrawals_transfers_and_unknown_actions_cannot_enter_bounded_envelope(self):
        for action in ("WITHDRAW", "TRANSFER", "CREDENTIAL.ROTATE", "OTHER"):
            with self.subTest(action=action):
                with self.assertRaises(ValueError):
                    envelope(allowed_actions={"ORDER.SUBMIT", action})

    def test_financial_bounds_reject_float_and_nonpositive_values(self):
        with self.assertRaises(TypeError):
            envelope(max_capital=1000.0)
        with self.assertRaises(ValueError):
            envelope(max_single_notional="0")
        with self.assertRaises(ValueError):
            envelope(max_gross_leverage="-1")

    def test_duplicate_evidence_identity_or_kind_fails_closed(self):
        duplicate_id = prerequisites()
        duplicate_id[1] = evidence(
            "SCIENTIFIC_QUALIFICATION",
            evidence_id=duplicate_id[0].evidence_id,
        )
        with self.assertRaisesRegex(ValueError, "evidence_id"):
            assess_bounded_real_qualification(
                envelope=envelope(),
                prerequisite_evidence=duplicate_id,
                observations=observations(),
            )

        duplicate_kind = prerequisites()
        duplicate_kind.append(
            evidence("RECOVERY", evidence_id="different-recovery")
        )
        with self.assertRaisesRegex(ValueError, "duplicate prerequisite evidence kind"):
            assess_bounded_real_qualification(
                envelope=envelope(),
                prerequisite_evidence=duplicate_kind,
                observations=observations(),
            )


if __name__ == "__main__":
    unittest.main()
