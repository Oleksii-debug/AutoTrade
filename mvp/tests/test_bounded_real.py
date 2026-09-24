import hashlib
from tempfile import TemporaryDirectory
import unittest
from uuid import NAMESPACE_URL, uuid5

from mvp.autotrade_mvp.bounded_real import (
    BoundedRealEnvelope,
    BoundedRealObservations,
    EvidenceVerification,
    ImmutableEvidenceRef,
    QualificationEvidence,
    artifact_store_evidence_verifier,
    assess_bounded_real_qualification as _assess_bounded_real_qualification,
)
from research.autotrade_research.artifacts.store import ArtifactStore


SHA = "a" * 40
OBSERVATION_KINDS = (
    "ACTUAL_FILL",
    "PARTIAL_FILL",
    "FILL_RECONCILIATION",
    "FEE_RECONCILIATION",
    "REVOCATION",
    "PROTECTION",
    "AUTHORITY_AUDIT",
    "UNKNOWN_SUBMISSION_AUDIT",
)


def _seed(
    kind,
    *,
    source_sha=SHA,
    envelope_id="bounded-1",
    provider_id="provider-1",
    account_id="account-1",
):
    return (
        f"{kind}|{source_sha}|{envelope_id}|{provider_id}|{account_id}"
    ).encode("utf-8")


def evidence_ref(
    kind,
    *,
    source_sha=SHA,
    envelope_id="bounded-1",
    provider_id="provider-1",
    account_id="account-1",
):
    payload = _seed(
        kind,
        source_sha=source_sha,
        envelope_id=envelope_id,
        provider_id=provider_id,
        account_id=account_id,
    )
    return ImmutableEvidenceRef(
        artifact_id=str(uuid5(NAMESPACE_URL, payload.decode("utf-8"))),
        sha256="sha256:" + hashlib.sha256(payload).hexdigest(),
        evidence_kind=kind,
        source_sha=source_sha,
        envelope_id=envelope_id,
        provider_id=provider_id,
        account_id=account_id,
    )


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
    source_sha = overrides.pop("source_sha", SHA)
    envelope_id = overrides.pop("envelope_id", "bounded-1")
    provider_id = overrides.pop("provider_id", "provider-1")
    account_id = overrides.pop("account_id", "account-1")
    ref = overrides.pop(
        "evidence_ref",
        evidence_ref(
            f"PREREQUISITE:{kind}",
            source_sha=source_sha,
            envelope_id=envelope_id,
            provider_id=provider_id,
            account_id=account_id,
        ),
    )
    values = dict(
        evidence_id="e-" + kind.lower(),
        evidence_kind=kind,
        source_sha=source_sha,
        envelope_id=envelope_id,
        passed=True,
        evidence_ref=ref,
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


def observation_evidence(
    *,
    source_sha=SHA,
    envelope_id="bounded-1",
    provider_id="provider-1",
    account_id="account-1",
):
    return tuple(
        evidence_ref(
            kind,
            source_sha=source_sha,
            envelope_id=envelope_id,
            provider_id=provider_id,
            account_id=account_id,
        )
        for kind in OBSERVATION_KINDS
    )


def observations(**overrides):
    source_sha = overrides.get("source_sha", SHA)
    envelope_id = overrides.get("envelope_id", "bounded-1")
    provider_id = overrides.get("provider_id", "provider-1")
    account_id = overrides.get("account_id", "account-1")
    values = dict(
        source_sha=source_sha,
        envelope_id=envelope_id,
        provider_id=provider_id,
        account_id=account_id,
        observed_fill_count=3,
        observed_partial_fill=True,
        all_fills_reconciled=True,
        fees_reconciled=True,
        revocation_verified=True,
        protection_verified=True,
        unauthorized_action_count=0,
        unresolved_unknown_count=0,
        evidence_refs=observation_evidence(
            source_sha=source_sha,
            envelope_id=envelope_id,
            provider_id=provider_id,
            account_id=account_id,
        ),
    )
    values.update(overrides)
    return BoundedRealObservations.create(**values)


def trusted_evidence_verifier(_ref):
    return EvidenceVerification(valid=True)


def assess_bounded_real_qualification(**kwargs):
    kwargs.setdefault("evidence_verifier", trusted_evidence_verifier)
    return _assess_bounded_real_qualification(**kwargs)


def publish_ref(store, ref):
    payload = _seed(
        ref.evidence_kind,
        source_sha=ref.source_sha,
        envelope_id=ref.envelope_id,
        provider_id=ref.provider_id,
        account_id=ref.account_id,
    )
    manifest = store.publish_bytes(
        artifact_id=ref.artifact_id,
        data=payload,
        media_type="application/json",
        rights={
            "storage": True,
            "export": False,
            "rights_id": "bounded-real-qualification",
        },
        metadata={
            "artifact_kind": "BOUNDED_REAL_EVIDENCE",
            "schema_version": 1,
            "evidence_kind": ref.evidence_kind,
            "source_sha": ref.source_sha,
            "envelope_id": ref.envelope_id,
            "provider_id": ref.provider_id,
            "account_id": ref.account_id,
            "outcome": "PASS",
            "producer_id": "qualification-runner-v1",
            "evidence_version": "1",
        },
    )
    assert manifest["sha256"] == ref.sha256


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

    def test_no_verifier_means_self_asserted_bundle_can_never_complete(self):
        result = _assess_bounded_real_qualification(
            envelope=envelope(),
            prerequisite_evidence=prerequisites(),
            observations=observations(),
        )
        self.assertFalse(result.complete)
        self.assertIn("immutable_evidence_verifier_required", result.reason_codes)

    def test_missing_observation_artifacts_block_self_declared_green_flags(self):
        green_without_refs = observations(evidence_refs=())
        result = assess_bounded_real_qualification(
            envelope=envelope(),
            prerequisite_evidence=prerequisites(),
            observations=green_without_refs,
        )
        self.assertFalse(result.complete)
        self.assertTrue(
            any(
                reason.startswith("missing_observation_evidence:")
                for reason in result.reason_codes
            )
        )

    def test_missing_artifacts_in_canonical_store_never_verify(self):
        with TemporaryDirectory() as directory:
            store = ArtifactStore(directory)
            result = _assess_bounded_real_qualification(
                envelope=envelope(),
                prerequisite_evidence=prerequisites(),
                observations=observations(),
                evidence_verifier=artifact_store_evidence_verifier(store),
            )
            self.assertFalse(result.complete)
            self.assertTrue(
                any(
                    reason.startswith("immutable_evidence_unverified:")
                    for reason in result.reason_codes
                )
            )

    def test_exact_immutable_artifacts_can_make_evidence_bundle_complete(self):
        with TemporaryDirectory() as directory:
            store = ArtifactStore(directory)
            prereq = prerequisites()
            observed = observations()
            for item in prereq:
                publish_ref(store, item.evidence_ref)
            for ref in observed.evidence_refs:
                publish_ref(store, ref)

            result = _assess_bounded_real_qualification(
                envelope=envelope(),
                prerequisite_evidence=prereq,
                observations=observed,
                evidence_verifier=artifact_store_evidence_verifier(store),
            )
            self.assertTrue(result.complete)
            self.assertFalse(result.authorizes_trading)

    def test_artifact_digest_mismatch_is_conflicted(self):
        with TemporaryDirectory() as directory:
            store = ArtifactStore(directory)
            prereq = prerequisites()
            observed = observations()
            for item in prereq:
                publish_ref(store, item.evidence_ref)
            for ref in observed.evidence_refs:
                publish_ref(store, ref)

            original = prereq[0]
            wrong_ref = ImmutableEvidenceRef(
                artifact_id=original.evidence_ref.artifact_id,
                sha256="sha256:" + "b" * 64,
                evidence_kind=original.evidence_ref.evidence_kind,
                source_sha=original.evidence_ref.source_sha,
                envelope_id=original.evidence_ref.envelope_id,
                provider_id=original.evidence_ref.provider_id,
                account_id=original.evidence_ref.account_id,
            )
            prereq[0] = QualificationEvidence(
                evidence_id=original.evidence_id,
                evidence_kind=original.evidence_kind,
                source_sha=original.source_sha,
                envelope_id=original.envelope_id,
                passed=True,
                evidence_ref=wrong_ref,
            )
            result = _assess_bounded_real_qualification(
                envelope=envelope(),
                prerequisite_evidence=prereq,
                observations=observed,
                evidence_verifier=artifact_store_evidence_verifier(store),
            )
            self.assertFalse(result.complete)
            self.assertIn(
                "immutable_evidence_conflicted:PREREQUISITE:RELEASE_CANDIDATE",
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

    def test_exact_build_provider_account_and_envelope_must_match(self):
        items = prerequisites()
        items[0] = evidence("RELEASE_CANDIDATE", source_sha="b" * 40)
        result = assess_bounded_real_qualification(
            envelope=envelope(),
            prerequisite_evidence=items,
            observations=observations(),
        )
        self.assertIn("source_sha_mismatch:RELEASE_CANDIDATE", result.reason_codes)
        self.assertIn(
            "immutable_evidence_scope_mismatch:PREREQUISITE:RELEASE_CANDIDATE",
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
        self.assertIn("unauthorized_action_observed", result.reason_codes)
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

    def test_direct_construction_cannot_bypass_evidence_scope(self):
        ref = evidence_ref("PREREQUISITE:RECOVERY")
        with self.assertRaises(TypeError):
            QualificationEvidence(
                evidence_id="e-recovery",
                evidence_kind="RECOVERY",
                source_sha=SHA,
                envelope_id="bounded-1",
                passed=1,
                evidence_ref=ref,
            )
        with self.assertRaises(ValueError):
            QualificationEvidence(
                evidence_id="e-recovery",
                evidence_kind="RECOVERY",
                source_sha=SHA,
                envelope_id="bounded-1",
                passed=True,
                evidence_ref=evidence_ref("PREREQUISITE:ACCESSIBILITY"),
            )

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
