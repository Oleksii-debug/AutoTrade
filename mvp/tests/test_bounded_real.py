import hashlib
from tempfile import TemporaryDirectory
import unittest
from uuid import NAMESPACE_URL, uuid5

from research.autotrade_research.artifacts.store import ArtifactStore

from mvp.autotrade_mvp.bounded_real import (
    BoundedRealEnvelope,
    BoundedRealObservations,
    EvidenceVerification,
    ImmutableEvidenceRef,
    QualificationEvidence,
    artifact_store_evidence_verifier,
    assess_bounded_real_qualification,
)
from mvp.autotrade_mvp.qualification_attestation import (
    EvidenceArtifactRef,
    QualificationScope,
    SignedQualificationAttestation,
)
from mvp.tests.test_qualification_attestation import (
    attestation,
    policy as attestation_policy,
    root as attestation_root,
    sign,
)


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
PREREQUISITE_KINDS = (
    "RELEASE_CANDIDATE",
    "SCIENTIFIC_QUALIFICATION",
    "FORWARD_PAPER",
    "PROVIDER_CAPABILITY",
    "ACCESSIBILITY",
    "RECOVERY",
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


def ref(label, *, evidence_kind, **overrides):
    artifact_id = str(uuid5(NAMESPACE_URL, "autotrade:" + label))
    values = dict(
        artifact_id=artifact_id,
        sha256="sha256:" + hashlib.sha256(artifact_id.encode("utf-8")).hexdigest(),
        evidence_kind=evidence_kind,
        source_sha=SHA,
        envelope_id="bounded-1",
        envelope_digest=envelope().envelope_digest,
        provider_id="provider-1",
        account_id="account-1",
    )
    values.update(overrides)
    return ImmutableEvidenceRef(**values)


def evidence(kind, **overrides):
    ref_overrides = overrides.pop("ref_overrides", {})
    values = dict(
        evidence_id="evidence-" + kind.lower(),
        evidence_kind=kind,
        source_sha=SHA,
        envelope_id="bounded-1",
        envelope_digest=envelope().envelope_digest,
        passed=True,
        evidence_ref=ref(
            "prerequisite:" + kind,
            evidence_kind="PREREQUISITE:" + kind,
            **ref_overrides,
        ),
        unresolved_blockers=(),
    )
    values.update(overrides)
    return QualificationEvidence.create(**values)


def prerequisites():
    return [evidence(kind) for kind in PREREQUISITE_KINDS]


def observation_refs(
    *,
    source_sha=SHA,
    envelope_id="bounded-1",
    envelope_digest=None,
    provider_id="provider-1",
    account_id="account-1",
):
    envelope_digest = envelope_digest or envelope().envelope_digest
    return tuple(
        ref(
            "observation:" + kind,
            evidence_kind=kind,
            source_sha=source_sha,
            envelope_id=envelope_id,
            envelope_digest=envelope_digest,
            provider_id=provider_id,
            account_id=account_id,
        )
        for kind in OBSERVATION_KINDS
    )


def observations(**overrides):
    scope = {
        "source_sha": overrides.get("source_sha", SHA),
        "envelope_id": overrides.get("envelope_id", "bounded-1"),
        "envelope_digest": overrides.get(
            "envelope_digest", envelope().envelope_digest
        ),
        "provider_id": overrides.get("provider_id", "provider-1"),
        "account_id": overrides.get("account_id", "account-1"),
    }
    values = dict(
        **scope,
        observed_fill_count=3,
        observed_partial_fill=True,
        all_fills_reconciled=True,
        fees_reconciled=True,
        revocation_verified=True,
        protection_verified=True,
        unauthorized_action_count=0,
        unresolved_unknown_count=0,
        evidence_refs=observation_refs(**scope),
    )
    values.update(overrides)
    return BoundedRealObservations.create(**values)


def valid_verifier(_ref):
    """Deliberately untrusted verifier used to prove callback bypass is closed."""
    return EvidenceVerification(valid=True)


def _publish_ref(store, evidence_ref, *, payload=None, metadata_overrides=None):
    payload = (
        evidence_ref.artifact_id.encode("utf-8")
        if payload is None
        else payload
    )
    metadata = {
        "artifact_kind": "BOUNDED_REAL_EVIDENCE",
        "schema_version": 1,
        "evidence_kind": evidence_ref.evidence_kind,
        "source_sha": evidence_ref.source_sha,
        "envelope_id": evidence_ref.envelope_id,
        "envelope_digest": evidence_ref.envelope_digest,
        "provider_id": evidence_ref.provider_id,
        "account_id": evidence_ref.account_id,
        "outcome": "PASS",
        "producer_id": "qualification-harness",
        "evidence_version": "1",
    }
    metadata.update(metadata_overrides or {})
    manifest = store.publish_bytes(
        artifact_id=evidence_ref.artifact_id,
        data=payload,
        media_type="application/octet-stream",
        rights={"storage": True, "export": False},
        source_refs=[f"git:{evidence_ref.source_sha}"],
        metadata=metadata,
    )
    return manifest


def _populate_bundle(store, prerequisite_items, observed, *, exclude=()):
    excluded = set(exclude)
    for item in prerequisite_items:
        if item.evidence_ref.artifact_id not in excluded:
            _publish_ref(store, item.evidence_ref)
    for evidence_ref in observed.evidence_refs:
        if evidence_ref.artifact_id not in excluded:
            _publish_ref(store, evidence_ref)


def _all_refs(prerequisite_items, observed):
    return tuple(
        [item.evidence_ref for item in prerequisite_items]
        + list(observed.evidence_refs)
    )


def _signed_bounded_receipt(bounded, refs):
    trust_root = attestation_root(
        scopes=(QualificationScope("BOUNDED_REAL", "QUALIFICATION"),)
    )
    trust_policy = attestation_policy(trust_root)
    signed = attestation(
        trust_root,
        source_sha=bounded.source_sha,
        domain="BOUNDED_REAL",
        gate="QUALIFICATION",
        package_id="WP-58",
        protocol_id="bounded-real-qualification-v1",
        protocol_version="1.0.0",
        requirement_ids=(
            "bounded-real-terminal-evidence",
            f"envelope/{bounded.envelope_digest}",
        ),
        evidence_refs=tuple(
            EvidenceArtifactRef(
                artifact_id=item.artifact_id,
                sha256=item.sha256,
                media_type="application/octet-stream",
                evidence_kind=item.evidence_kind,
                source_sha=item.source_sha,
            )
            for item in refs
        ),
        result="PASS",
        release_artifact_id=None,
        release_artifact_sha256=None,
    )
    return (
        SignedQualificationAttestation(signed, sign(signed)),
        trust_policy,
    )


class _ArtifactStoreStub:
    def __init__(self, evidence_ref, *, metadata_overrides=None, payload=b"evidence"):
        self.payload = payload
        self.ref = evidence_ref
        metadata = {
            "artifact_kind": "BOUNDED_REAL_EVIDENCE",
            "schema_version": 1,
            "evidence_kind": evidence_ref.evidence_kind,
            "source_sha": evidence_ref.source_sha,
            "envelope_id": evidence_ref.envelope_id,
            "envelope_digest": evidence_ref.envelope_digest,
            "provider_id": evidence_ref.provider_id,
            "account_id": evidence_ref.account_id,
            "outcome": "PASS",
            "producer_id": "qualification-harness",
            "evidence_version": "1",
        }
        metadata.update(metadata_overrides or {})
        self.manifest = {
            "artifact_id": evidence_ref.artifact_id,
            "sha256": "sha256:" + hashlib.sha256(payload).hexdigest(),
            "metadata": metadata,
        }

    def load_manifest(self, artifact_id):
        if artifact_id != self.ref.artifact_id:
            raise KeyError(artifact_id)
        return self.manifest

    def read_bytes(self, artifact_id):
        if artifact_id != self.ref.artifact_id:
            raise KeyError(artifact_id)
        return self.payload


class BoundedRealQualificationTests(unittest.TestCase):
    def test_self_published_complete_store_is_not_terminal_trust(self):
        bounded = envelope()
        prerequisite_items = prerequisites()
        observed = observations()
        with TemporaryDirectory() as directory:
            store = ArtifactStore(directory)
            _populate_bundle(store, prerequisite_items, observed)
            result = assess_bounded_real_qualification(
                envelope=bounded,
                prerequisite_evidence=prerequisite_items,
                observations=observed,
                evidence_verifier=artifact_store_evidence_verifier(store),
            )
        self.assertFalse(result.complete)
        self.assertIn(
            "independent_evidence_trust_unavailable",
            result.reason_codes,
        )
        self.assertFalse(result.authorizes_trading)
        self.assertEqual(result.exact_source_sha, SHA)
        self.assertEqual(result.envelope_digest, bounded.envelope_digest)
        self.assertTrue(
            result.evidence_verifier_identity.startswith(
                "AUTOTRADE_ARTIFACT_STORE_BOUNDED_REAL_V1:"
            )
        )

    def test_signed_exact_bounded_real_receipt_allows_terminal_completion(self):
        bounded = envelope()
        prerequisite_items = prerequisites()
        observed = observations()
        refs = _all_refs(prerequisite_items, observed)
        receipt, trust_policy = _signed_bounded_receipt(bounded, refs)
        with TemporaryDirectory() as directory:
            store = ArtifactStore(directory)
            _populate_bundle(store, prerequisite_items, observed)
            result = assess_bounded_real_qualification(
                envelope=bounded,
                prerequisite_evidence=prerequisite_items,
                observations=observed,
                evidence_verifier=artifact_store_evidence_verifier(store),
                qualification_receipt=receipt,
                qualification_policy=trust_policy,
                expected_policy_id=trust_policy.policy_id,
                expected_policy_version=trust_policy.policy_version,
            )
        self.assertTrue(result.complete)
        self.assertEqual(result.reason_codes, ())
        self.assertFalse(result.authorizes_trading)
        self.assertIsNotNone(result.qualification_attestation_id)
        self.assertTrue(
            result.qualification_attestation_digest.startswith("sha256:")
        )
        self.assertTrue(result.qualification_policy_id.startswith("sha256:"))
        self.assertTrue(
            result.qualification_trust_root_id.startswith("sha256:")
        )

    def test_signed_bounded_real_receipt_must_cover_exact_evidence_set(self):
        bounded = envelope()
        prerequisite_items = prerequisites()
        observed = observations()
        refs = _all_refs(prerequisite_items, observed)
        receipt, trust_policy = _signed_bounded_receipt(bounded, refs[:-1])
        with TemporaryDirectory() as directory:
            store = ArtifactStore(directory)
            _populate_bundle(store, prerequisite_items, observed)
            result = assess_bounded_real_qualification(
                envelope=bounded,
                prerequisite_evidence=prerequisite_items,
                observations=observed,
                evidence_verifier=artifact_store_evidence_verifier(store),
                qualification_receipt=receipt,
                qualification_policy=trust_policy,
                expected_policy_id=trust_policy.policy_id,
                expected_policy_version=trust_policy.policy_version,
            )
        self.assertFalse(result.complete)
        self.assertIn(
            "independent_evidence_set_mismatch",
            result.reason_codes,
        )

    def test_caller_booleans_without_immutable_verifier_never_pass(self):
        result = assess_bounded_real_qualification(
            envelope=envelope(),
            prerequisite_evidence=prerequisites(),
            observations=observations(),
        )
        self.assertFalse(result.complete)
        self.assertIn(
            "trusted_immutable_evidence_verifier_required",
            result.reason_codes,
        )

        callback = assess_bounded_real_qualification(
            envelope=envelope(),
            prerequisite_evidence=prerequisites(),
            observations=observations(),
            evidence_verifier=valid_verifier,
        )
        self.assertFalse(callback.complete)
        self.assertIn(
            "untrusted_immutable_evidence_verifier",
            callback.reason_codes,
        )
        self.assertIsNone(callback.evidence_verifier_identity)

    def test_missing_failed_or_blocked_prerequisite_fails_closed(self):
        missing = assess_bounded_real_qualification(
            envelope=envelope(),
            prerequisite_evidence=prerequisites()[:-1],
            observations=observations(),
            evidence_verifier=valid_verifier,
        )
        self.assertTrue(
            any(
                reason.startswith("missing_prerequisite_evidence:")
                for reason in missing.reason_codes
            )
        )

        failed_items = prerequisites()
        failed_items[1] = evidence("SCIENTIFIC_QUALIFICATION", passed=False)
        failed = assess_bounded_real_qualification(
            envelope=envelope(),
            prerequisite_evidence=failed_items,
            observations=observations(),
            evidence_verifier=valid_verifier,
        )
        self.assertIn(
            "prerequisite_failed:SCIENTIFIC_QUALIFICATION",
            failed.reason_codes,
        )

        blocked_items = prerequisites()
        blocked_items[2] = evidence(
            "FORWARD_PAPER",
            unresolved_blockers=("paper drawdown gate unresolved",),
        )
        blocked = assess_bounded_real_qualification(
            envelope=envelope(),
            prerequisite_evidence=blocked_items,
            observations=observations(),
            evidence_verifier=valid_verifier,
        )
        self.assertIn("unresolved_blockers:FORWARD_PAPER", blocked.reason_codes)

    def test_real_fill_partial_fill_reconciliation_and_safety_facts_are_required(self):
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
                unauthorized_action_count=1,
                unresolved_unknown_count=1,
            ),
            evidence_verifier=valid_verifier,
        )
        self.assertFalse(result.complete)
        expected = {
            "no_real_fill_evidence",
            "partial_fill_not_evidenced",
            "fills_not_fully_reconciled",
            "fees_not_reconciled",
            "revocation_not_verified",
            "protection_not_verified",
            "unauthorized_action_observed",
            "unresolved_submission_unknown",
        }
        self.assertTrue(expected <= set(result.reason_codes))

    def test_required_observation_artifact_kinds_cannot_be_self_asserted_away(self):
        refs = tuple(
            item for item in observation_refs()
            if item.evidence_kind != "PARTIAL_FILL"
        )
        result = assess_bounded_real_qualification(
            envelope=envelope(),
            prerequisite_evidence=prerequisites(),
            observations=observations(evidence_refs=refs),
            evidence_verifier=valid_verifier,
        )
        self.assertFalse(result.complete)
        self.assertTrue(
            any(
                reason.startswith("missing_observation_evidence:")
                and "PARTIAL_FILL" in reason
                for reason in result.reason_codes
            )
        )

    def test_scope_mismatch_is_explicit_even_when_evidence_is_internally_consistent(self):
        other = observations(
            account_id="other-account",
            evidence_refs=observation_refs(account_id="other-account"),
        )
        result = assess_bounded_real_qualification(
            envelope=envelope(),
            prerequisite_evidence=prerequisites(),
            observations=other,
            evidence_verifier=valid_verifier,
        )
        self.assertFalse(result.complete)
        self.assertIn("observation_scope_mismatch", result.reason_codes)
        self.assertTrue(
            any(
                reason.startswith("immutable_evidence_scope_mismatch:")
                for reason in result.reason_codes
            )
        )

    def test_evidence_verifier_failure_or_conflict_blocks_qualification(self):
        prerequisite_items = prerequisites()
        observed = observations()
        target = prerequisite_items[0].evidence_ref

        with TemporaryDirectory() as directory:
            store = ArtifactStore(directory)
            _populate_bundle(
                store,
                prerequisite_items,
                observed,
                exclude={target.artifact_id},
            )
            result = assess_bounded_real_qualification(
                envelope=envelope(),
                prerequisite_evidence=prerequisite_items,
                observations=observed,
                evidence_verifier=artifact_store_evidence_verifier(store),
            )
        self.assertTrue(
            any(
                reason.startswith("immutable_evidence_unverified:")
                for reason in result.reason_codes
            )
        )

        with TemporaryDirectory() as directory:
            store = ArtifactStore(directory)
            _populate_bundle(store, prerequisite_items, observed)
            digest = target.sha256.removeprefix("sha256:")
            store._object_path(digest).write_bytes(b"tampered")
            result = assess_bounded_real_qualification(
                envelope=envelope(),
                prerequisite_evidence=prerequisite_items,
                observations=observed,
                evidence_verifier=artifact_store_evidence_verifier(store),
            )
        self.assertTrue(
            any(
                reason.startswith("immutable_evidence_conflicted:")
                for reason in result.reason_codes
            )
        )

    def test_duplicate_artifact_or_digest_cannot_be_reused_as_distinct_evidence(self):
        items = prerequisites()
        original = items[0].evidence_ref
        duplicate = ImmutableEvidenceRef(
            artifact_id=original.artifact_id,
            sha256="sha256:" + hashlib.sha256(b"other").hexdigest(),
            evidence_kind="PREREQUISITE:SCIENTIFIC_QUALIFICATION",
            source_sha=SHA,
            envelope_id="bounded-1",
            envelope_digest=original.envelope_digest,
            provider_id="provider-1",
            account_id="account-1",
        )
        items[1] = evidence(
            "SCIENTIFIC_QUALIFICATION",
            evidence_ref=duplicate,
        )
        with self.assertRaisesRegex(ValueError, "artifact_id"):
            assess_bounded_real_qualification(
                envelope=envelope(),
                prerequisite_evidence=items,
                observations=observations(),
                evidence_verifier=valid_verifier,
            )

        first = prerequisites()[0].evidence_ref
        refs = list(observation_refs())
        refs[0] = ImmutableEvidenceRef(
            artifact_id=refs[0].artifact_id,
            sha256=first.sha256,
            evidence_kind=refs[0].evidence_kind,
            source_sha=refs[0].source_sha,
            envelope_id=refs[0].envelope_id,
            envelope_digest=refs[0].envelope_digest,
            provider_id=refs[0].provider_id,
            account_id=refs[0].account_id,
        )
        with self.assertRaisesRegex(ValueError, "digest"):
            assess_bounded_real_qualification(
                envelope=envelope(),
                prerequisite_evidence=prerequisites(),
                observations=observations(evidence_refs=tuple(refs)),
                evidence_verifier=valid_verifier,
            )

    def test_artifact_store_verifier_recomputes_payload_digest_and_semantics(self):
        payload = b"verified bounded-real evidence"
        good_ref = ref(
            "store-backed",
            evidence_kind="ACTUAL_FILL",
            sha256="sha256:" + hashlib.sha256(payload).hexdigest(),
        )
        with TemporaryDirectory() as directory:
            store = ArtifactStore(directory)
            _publish_ref(store, good_ref, payload=payload)
            verifier = artifact_store_evidence_verifier(store)
            good = verifier.verify(good_ref)
            self.assertTrue(good.valid)
            self.assertFalse(good.conflicted)

            digest = good_ref.sha256.removeprefix("sha256:")
            store._object_path(digest).write_bytes(b"changed after manifest")
            bad = verifier.verify(good_ref)
            self.assertFalse(bad.valid)
            self.assertTrue(bad.conflicted)

        wrong_ref = ref("wrong-scope-store", evidence_kind="ACTUAL_FILL")
        with TemporaryDirectory() as directory:
            store = ArtifactStore(directory)
            _publish_ref(
                store,
                wrong_ref,
                metadata_overrides={"account_id": "wrong-account"},
            )
            wrong_scope = artifact_store_evidence_verifier(store).verify(wrong_ref)
            self.assertFalse(wrong_scope.valid)
            self.assertTrue(wrong_scope.conflicted)

        with self.assertRaisesRegex(TypeError, "canonical ArtifactStore"):
            artifact_store_evidence_verifier(_ArtifactStoreStub(good_ref, payload=payload))

    def test_evidence_is_bound_to_exact_bounded_real_envelope_content(self):
        original = envelope()
        prerequisite_items = prerequisites()
        observed = observations()
        changed = (
            {"max_capital": "1001"},
            {"max_single_notional": "101"},
            {"policy_id": "policy-2"},
            {
                "allowed_actions": {
                    "ORDER.SUBMIT",
                    "ORDER.CANCEL",
                    "ORDER.REPLACE",
                    "FLATTEN",
                }
            },
        )
        for overrides in changed:
            with self.subTest(overrides=overrides):
                candidate = envelope(**overrides)
                self.assertNotEqual(
                    candidate.envelope_digest,
                    original.envelope_digest,
                )
                result = assess_bounded_real_qualification(
                    envelope=candidate,
                    prerequisite_evidence=prerequisite_items,
                    observations=observed,
                    evidence_verifier=valid_verifier,
                )
                self.assertFalse(result.complete)
                self.assertIn("observation_scope_mismatch", result.reason_codes)
                self.assertTrue(
                    any(
                        reason.startswith("envelope_digest_mismatch:")
                        for reason in result.reason_codes
                    )
                )
                self.assertTrue(
                    any(
                        reason.startswith("immutable_evidence_scope_mismatch:")
                        for reason in result.reason_codes
                    )
                )

        self.assertEqual(
            envelope(
                max_capital="1000.00",
                max_single_notional="100.0",
                max_gross_leverage="1.500",
            ).envelope_digest,
            original.envelope_digest,
        )

    def test_single_notional_cannot_exceed_bounded_capital(self):
        with self.assertRaisesRegex(ValueError, "cannot exceed max_capital"):
            envelope(max_capital="100", max_single_notional="101")

    def test_exact_source_and_artifact_digests_reject_uppercase_spelling(self):
        with self.assertRaisesRegex(ValueError, "lowercase Git SHA"):
            envelope(source_sha=SHA.upper())
        with self.assertRaisesRegex(ValueError, "lowercase hexadecimal"):
            ref(
                "upper-digest",
                evidence_kind="ACTUAL_FILL",
                sha256=("sha256:" + "A" * 64),
            )

    def test_forbidden_actions_float_bounds_and_direct_bool_bypasses_are_rejected(self):
        for action in ("WITHDRAW", "TRANSFER", "CREDENTIAL.ROTATE", "OTHER"):
            with self.subTest(action=action):
                with self.assertRaises(ValueError):
                    envelope(allowed_actions={"ORDER.SUBMIT", action})
        with self.assertRaises(TypeError):
            envelope(max_capital=1000.0)
        with self.assertRaises(ValueError):
            envelope(max_single_notional="0")
        with self.assertRaises(TypeError):
            QualificationEvidence(
                evidence_id="bad",
                evidence_kind="RECOVERY",
                source_sha=SHA,
                envelope_id="bounded-1",
                envelope_digest=envelope().envelope_digest,
                passed=1,
                evidence_ref=ref(
                    "bad-bool",
                    evidence_kind="PREREQUISITE:RECOVERY",
                ),
            )
        with self.assertRaises(ValueError):
            observations(observed_fill_count=True)

    def test_prerequisite_reference_kind_and_scope_are_immutable_contracts(self):
        with self.assertRaisesRegex(ValueError, "kind"):
            evidence(
                "RECOVERY",
                evidence_ref=ref(
                    "wrong-kind",
                    evidence_kind="RECOVERY",
                ),
            )
        with self.assertRaisesRegex(ValueError, "scope"):
            evidence(
                "RECOVERY",
                evidence_ref=ref(
                    "wrong-source",
                    evidence_kind="PREREQUISITE:RECOVERY",
                    source_sha="b" * 40,
                ),
            )


if __name__ == "__main__":
    unittest.main()
