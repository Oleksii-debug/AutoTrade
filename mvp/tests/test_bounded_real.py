import hashlib
import unittest
from uuid import NAMESPACE_URL, uuid5

from mvp.autotrade_mvp.bounded_real import (
    BoundedRealEnvelope,
    BoundedRealObservations,
    EvidenceRef,
    ObservedFillEvidence,
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


def ref(label, **overrides):
    values = dict(
        artifact_id=str(uuid5(NAMESPACE_URL, "autotrade:" + label)),
        sha256="sha256:" + hashlib.sha256(label.encode("utf-8")).hexdigest(),
        source_sha=SHA,
        envelope_id="bounded-1",
        provider_id="provider-1",
        account_id="account-1",
    )
    values.update(overrides)
    return EvidenceRef.create(**values)


def evidence(kind, **overrides):
    ref_overrides = overrides.pop("ref_overrides", {})
    values = dict(
        evidence_kind=kind,
        evidence_ref=ref("prerequisite:" + kind, **ref_overrides),
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


def fill(label, *, partial=False, **ref_overrides):
    return ObservedFillEvidence(
        provider_execution_id="execution-" + label,
        partial_fill=partial,
        evidence_ref=ref("fill:" + label, **ref_overrides),
    )


def observations(**overrides):
    values = dict(
        source_sha=SHA,
        envelope_id="bounded-1",
        provider_id="provider-1",
        account_id="account-1",
        fill_evidence=(
            fill("1"),
            fill("2", partial=True),
            fill("3"),
        ),
        reconciliation_evidence=ref("reconciliation"),
        revocation_evidence=ref("revocation"),
        protection_evidence=ref("protection"),
        audit_evidence=ref("audit"),
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

    def test_prerequisite_requires_immutable_digest_and_exact_scope(self):
        with self.assertRaisesRegex(ValueError, "canonical SHA-256"):
            ref("bad-digest", sha256="not-a-digest")

        items = prerequisites()
        items[0] = evidence(
            "RELEASE_CANDIDATE",
            ref_overrides={"source_sha": "b" * 40},
        )
        result = assess_bounded_real_qualification(
            envelope=envelope(),
            prerequisite_evidence=items,
            observations=observations(),
        )
        self.assertIn(
            "evidence_scope_mismatch:RELEASE_CANDIDATE",
            result.reason_codes,
        )

    def test_missing_or_failed_prerequisite_fails_closed(self):
        result = assess_bounded_real_qualification(
            envelope=envelope(),
            prerequisite_evidence=prerequisites()[:-1],
            observations=observations(),
        )
        self.assertFalse(result.complete)
        self.assertTrue(
            any(
                reason.startswith("missing_prerequisite_evidence:")
                for reason in result.reason_codes
            )
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

    def test_real_fill_partial_fill_and_reconciliation_evidence_are_required(self):
        result = assess_bounded_real_qualification(
            envelope=envelope(),
            prerequisite_evidence=prerequisites(),
            observations=observations(
                fill_evidence=(),
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

    def test_fill_evidence_is_bound_to_provider_account_build_and_envelope(self):
        bad_fill = fill("bad", account_id="other-account")
        result = assess_bounded_real_qualification(
            envelope=envelope(),
            prerequisite_evidence=prerequisites(),
            observations=observations(
                fill_evidence=(bad_fill, fill("partial", partial=True)),
            ),
        )
        self.assertIn("fill_evidence_scope_mismatch:0", result.reason_codes)
        self.assertFalse(result.complete)

    def test_operational_evidence_refs_are_scope_bound(self):
        result = assess_bounded_real_qualification(
            envelope=envelope(),
            prerequisite_evidence=prerequisites(),
            observations=observations(
                reconciliation_evidence=ref(
                    "bad-reconciliation",
                    provider_id="other-provider",
                )
            ),
        )
        self.assertIn("reconciliation_evidence_scope_mismatch", result.reason_codes)

    def test_counts_and_booleans_cannot_stand_in_for_fill_artifacts(self):
        empty = observations(fill_evidence=())
        self.assertEqual(empty.observed_fill_count, 0)
        self.assertFalse(empty.observed_partial_fill)
        result = assess_bounded_real_qualification(
            envelope=envelope(),
            prerequisite_evidence=prerequisites(),
            observations=empty,
        )
        self.assertIn("no_real_fill_evidence", result.reason_codes)
        self.assertIn("partial_fill_not_evidenced", result.reason_codes)

    def test_duplicate_artifact_or_digest_cannot_be_reused_as_distinct_evidence(self):
        items = prerequisites()
        duplicate = ref("prerequisite:RELEASE_CANDIDATE")
        items[1] = QualificationEvidence.create(
            evidence_kind="SCIENTIFIC_QUALIFICATION",
            evidence_ref=duplicate,
            passed=True,
        )
        with self.assertRaisesRegex(ValueError, "artifact_id"):
            assess_bounded_real_qualification(
                envelope=envelope(),
                prerequisite_evidence=items,
                observations=observations(),
            )

        first = ref("digest-source-a")
        second = ref(
            "digest-source-b",
            sha256=first.sha256,
        )
        obs = observations(
            reconciliation_evidence=first,
            revocation_evidence=second,
        )
        with self.assertRaisesRegex(ValueError, "sha256"):
            assess_bounded_real_qualification(
                envelope=envelope(),
                prerequisite_evidence=prerequisites(),
                observations=obs,
            )

    def test_provider_execution_ids_must_be_unique(self):
        first = fill("same")
        with self.assertRaisesRegex(ValueError, "provider_execution_id"):
            observations(
                fill_evidence=(
                    first,
                    ObservedFillEvidence(
                        provider_execution_id=first.provider_execution_id,
                        partial_fill=True,
                        evidence_ref=ref("fill:other"),
                    ),
                )
            )

    def test_any_unauthorized_action_or_unresolved_unknown_blocks_qualification(self):
        result = assess_bounded_real_qualification(
            envelope=envelope(),
            prerequisite_evidence=prerequisites(),
            observations=observations(
                unauthorized_action_count=1,
                unresolved_unknown_count=1,
            ),
        )
        self.assertIn("unauthorized_action_observed", result.reason_codes)
        self.assertIn("unresolved_submission_unknown", result.reason_codes)
        self.assertFalse(result.complete)

    def test_direct_construction_cannot_bypass_safety_types(self):
        base = envelope()
        with self.assertRaises(ValueError):
            BoundedRealEnvelope(
                envelope_id=base.envelope_id,
                source_sha=base.source_sha,
                account_id=base.account_id,
                provider_id=base.provider_id,
                policy_id=base.policy_id,
                allowed_actions=frozenset({"ORDER.SUBMIT", "WITHDRAW"}),
                max_capital=base.max_capital,
                max_single_notional=base.max_single_notional,
                max_gross_leverage=base.max_gross_leverage,
            )
        with self.assertRaises(TypeError):
            QualificationEvidence(
                evidence_kind="RECOVERY",
                evidence_ref=ref("direct-recovery"),
                passed=1,
            )
        with self.assertRaises(TypeError):
            ObservedFillEvidence(
                provider_execution_id="execution",
                partial_fill="true",
                evidence_ref=ref("direct-fill"),
            )

    def test_withdrawals_transfers_unknown_actions_and_float_bounds_are_rejected(self):
        for action in ("WITHDRAW", "TRANSFER", "CREDENTIAL.ROTATE", "OTHER"):
            with self.subTest(action=action):
                with self.assertRaises(ValueError):
                    envelope(allowed_actions={"ORDER.SUBMIT", action})
        with self.assertRaises(TypeError):
            envelope(max_capital=1000.0)
        with self.assertRaises(ValueError):
            envelope(max_single_notional="0")


if __name__ == "__main__":
    unittest.main()
