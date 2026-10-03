import gc
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
import weakref

from research.autotrade_research.artifacts.store import ArtifactStore

from mvp.autotrade_mvp.bounded_real import (
    ArtifactStoreEvidenceVerifier,
    BoundedRealEnvelope,
    BoundedRealObservations,
    ImmutableEvidenceRef,
    QualificationEvidence,
    assess_bounded_real_qualification,
)


class BoundedRealAuthorityUnforgeabilityTests(unittest.TestCase):
    @staticmethod
    def _envelope() -> BoundedRealEnvelope:
        return BoundedRealEnvelope.create(
            envelope_id="bounded-real-test",
            source_sha="a" * 40,
            account_id="acct-1",
            provider_id="PROVIDER-A",
            policy_id="policy-1",
            allowed_actions=frozenset({"ORDER.SUBMIT"}),
            max_capital="1000",
            max_single_notional="100",
            max_gross_leverage="2",
        )

    @staticmethod
    def _observations(envelope: BoundedRealEnvelope) -> BoundedRealObservations:
        return BoundedRealObservations.create(
            source_sha=envelope.source_sha,
            envelope_id=envelope.envelope_id,
            envelope_digest=envelope.envelope_digest,
            provider_id=envelope.provider_id,
            account_id=envelope.account_id,
            observed_fill_count=0,
            observed_partial_fill=False,
            all_fills_reconciled=True,
            fees_reconciled=True,
            revocation_verified=True,
            protection_verified=True,
            unauthorized_action_count=0,
            unresolved_unknown_count=0,
            evidence_refs=(),
        )

    def test_envelope_subclass_is_rejected_before_financial_scope_dispatch(self):
        calls = []

        class HostileEnvelope(BoundedRealEnvelope):
            def __getattribute__(self, name):
                if name in {
                    "source_sha",
                    "envelope_id",
                    "envelope_digest",
                    "provider_id",
                    "account_id",
                }:
                    calls.append(name)
                return super().__getattribute__(name)

        hostile = HostileEnvelope(
            envelope_id="bounded-real-test",
            source_sha="a" * 40,
            account_id="acct-1",
            provider_id="PROVIDER-A",
            policy_id="policy-1",
            allowed_actions=frozenset({"ORDER.SUBMIT"}),
            max_capital="1000",
            max_single_notional="100",
            max_gross_leverage="2",
        )
        observations = self._observations(self._envelope())
        calls.clear()

        with self.assertRaises(TypeError):
            assess_bounded_real_qualification(
                envelope=hostile,
                prerequisite_evidence=(),
                observations=observations,
            )

        self.assertEqual(calls, [])

    def test_observations_subclass_is_rejected_before_scope_dispatch(self):
        envelope = self._envelope()
        calls = []

        class HostileObservations(BoundedRealObservations):
            def __getattribute__(self, name):
                if name in {
                    "source_sha",
                    "envelope_id",
                    "envelope_digest",
                    "provider_id",
                    "account_id",
                    "evidence_refs",
                }:
                    calls.append(name)
                return super().__getattribute__(name)

        hostile = HostileObservations(
            source_sha=envelope.source_sha,
            envelope_id=envelope.envelope_id,
            envelope_digest=envelope.envelope_digest,
            provider_id=envelope.provider_id,
            account_id=envelope.account_id,
            observed_fill_count=0,
            observed_partial_fill=False,
            all_fills_reconciled=True,
            fees_reconciled=True,
            revocation_verified=True,
            protection_verified=True,
            unauthorized_action_count=0,
            unresolved_unknown_count=0,
            evidence_refs=(),
        )
        calls.clear()

        with self.assertRaises(TypeError):
            assess_bounded_real_qualification(
                envelope=envelope,
                prerequisite_evidence=(),
                observations=hostile,
            )

        self.assertEqual(calls, [])

    def test_prerequisite_evidence_subclass_is_rejected_before_field_dispatch(self):
        envelope = self._envelope()
        observations = self._observations(envelope)
        calls = []
        ref = ImmutableEvidenceRef(
            artifact_id="11111111-1111-4111-8111-111111111111",
            sha256="sha256:" + "b" * 64,
            evidence_kind="PREREQUISITE:RELEASE_CANDIDATE",
            source_sha=envelope.source_sha,
            envelope_id=envelope.envelope_id,
            envelope_digest=envelope.envelope_digest,
            provider_id=envelope.provider_id,
            account_id=envelope.account_id,
        )

        class HostileEvidence(QualificationEvidence):
            def __getattribute__(self, name):
                if name in {
                    "evidence_id",
                    "evidence_kind",
                    "source_sha",
                    "envelope_id",
                    "envelope_digest",
                    "passed",
                    "evidence_ref",
                    "unresolved_blockers",
                }:
                    calls.append(name)
                return super().__getattribute__(name)

        hostile = HostileEvidence(
            evidence_id="evidence-1",
            evidence_kind="RELEASE_CANDIDATE",
            source_sha=envelope.source_sha,
            envelope_id=envelope.envelope_id,
            envelope_digest=envelope.envelope_digest,
            passed=True,
            evidence_ref=ref,
        )
        calls.clear()

        with self.assertRaises(TypeError):
            assess_bounded_real_qualification(
                envelope=envelope,
                prerequisite_evidence=(hostile,),
                observations=observations,
            )

        self.assertEqual(calls, [])

    def test_exact_verifier_private_state_cannot_retarget_authority(self):
        envelope = self._envelope()
        with TemporaryDirectory() as selected_directory, TemporaryDirectory() as replacement_directory:
            selected_store = ArtifactStore(selected_directory)
            replacement_store = ArtifactStore(replacement_directory)
            verifier = ArtifactStoreEvidenceVerifier(
                selected_store,
                evidence_root=selected_directory,
            )
            original_identity = verifier.identity
            original_root = verifier.evidence_root
            calls = []

            def hostile_reader(*_args, **_kwargs):
                calls.append("read_snapshot")
                raise AssertionError("caller-injected reader must not run")

            vars(verifier)["_store"] = replacement_store
            vars(verifier)["_evidence_root"] = Path(replacement_directory).absolute()
            vars(verifier)["_store_identity"] = "sha256:" + "f" * 64
            vars(verifier)["_read_snapshot"] = hostile_reader

            with self.assertRaisesRegex(ValueError, "composition was modified"):
                _ = verifier.store
            with self.assertRaisesRegex(ValueError, "composition was modified"):
                _ = verifier.evidence_root
            with self.assertRaisesRegex(ValueError, "composition was modified"):
                _ = verifier.identity

            missing_ref = ImmutableEvidenceRef(
                artifact_id="22222222-2222-4222-8222-222222222222",
                sha256="sha256:" + "c" * 64,
                evidence_kind="PREREQUISITE:RELEASE_CANDIDATE",
                source_sha=envelope.source_sha,
                envelope_id=envelope.envelope_id,
                envelope_digest=envelope.envelope_digest,
                provider_id=envelope.provider_id,
                account_id=envelope.account_id,
            )
            with self.assertRaisesRegex(ValueError, "composition was modified"):
                ArtifactStoreEvidenceVerifier.verify(verifier, missing_ref)
            self.assertEqual(calls, [])

    def test_verifier_binding_exposes_no_erasable_weakref_callback(self):
        with TemporaryDirectory() as directory:
            verifier = ArtifactStoreEvidenceVerifier(
                ArtifactStore(directory),
                evidence_root=directory,
            )
            registry_refs = weakref.getweakrefs(verifier)
            self.assertTrue(registry_refs)
            self.assertTrue(
                all(ref.__callback__ is None for ref in registry_refs)
            )

    def test_exact_verifier_explicit_reinit_cannot_change_binding(self):
        with TemporaryDirectory() as selected_directory, TemporaryDirectory() as replacement_directory:
            selected_store = ArtifactStore(selected_directory)
            replacement_store = ArtifactStore(replacement_directory)
            verifier = ArtifactStoreEvidenceVerifier(
                selected_store,
                evidence_root=selected_directory,
            )
            original_identity = verifier.identity
            original_root = verifier.evidence_root

            with self.assertRaisesRegex(ValueError, "already initialized"):
                ArtifactStoreEvidenceVerifier.__init__(
                    verifier,
                    replacement_store,
                    evidence_root=replacement_directory,
                )

            self.assertIs(verifier.store, selected_store)
            self.assertEqual(verifier.evidence_root, original_root)
            self.assertEqual(verifier.identity, original_identity)

    def test_nested_evidence_ref_subclass_is_rejected_before_field_dispatch(self):
        envelope = self._envelope()
        calls = []

        class HostileRef(ImmutableEvidenceRef):
            def __getattribute__(self, name):
                if name in {
                    "artifact_id",
                    "sha256",
                    "evidence_kind",
                    "source_sha",
                    "envelope_id",
                    "envelope_digest",
                    "provider_id",
                    "account_id",
                }:
                    calls.append(name)
                return super().__getattribute__(name)

        hostile = HostileRef(
            artifact_id="33333333-3333-4333-8333-333333333333",
            sha256="sha256:" + "d" * 64,
            evidence_kind="PREREQUISITE:RELEASE_CANDIDATE",
            source_sha=envelope.source_sha,
            envelope_id=envelope.envelope_id,
            envelope_digest=envelope.envelope_digest,
            provider_id=envelope.provider_id,
            account_id=envelope.account_id,
        )
        calls.clear()

        with self.assertRaisesRegex(TypeError, "exact ImmutableEvidenceRef"):
            QualificationEvidence(
                evidence_id="evidence-hostile-ref",
                evidence_kind="RELEASE_CANDIDATE",
                source_sha=envelope.source_sha,
                envelope_id=envelope.envelope_id,
                envelope_digest=envelope.envelope_digest,
                passed=True,
                evidence_ref=hostile,
            )
        self.assertEqual(calls, [])

        calls.clear()
        with self.assertRaisesRegex(TypeError, "exact ImmutableEvidenceRef"):
            BoundedRealObservations(
                source_sha=envelope.source_sha,
                envelope_id=envelope.envelope_id,
                envelope_digest=envelope.envelope_digest,
                provider_id=envelope.provider_id,
                account_id=envelope.account_id,
                observed_fill_count=0,
                observed_partial_fill=False,
                all_fills_reconciled=True,
                fees_reconciled=True,
                revocation_verified=True,
                protection_verified=True,
                unauthorized_action_count=0,
                unresolved_unknown_count=0,
                evidence_refs=(hostile,),
            )
        self.assertEqual(calls, [])

    def test_observation_evidence_collection_must_be_exact_tuple(self):
        envelope = self._envelope()
        with self.assertRaisesRegex(TypeError, "exact tuple"):
            BoundedRealObservations(
                source_sha=envelope.source_sha,
                envelope_id=envelope.envelope_id,
                envelope_digest=envelope.envelope_digest,
                provider_id=envelope.provider_id,
                account_id=envelope.account_id,
                observed_fill_count=0,
                observed_partial_fill=False,
                all_fills_reconciled=True,
                fees_reconciled=True,
                revocation_verified=True,
                protection_verified=True,
                unauthorized_action_count=0,
                unresolved_unknown_count=0,
                evidence_refs=[],
            )

    def test_dead_verifier_releases_bound_artifact_store(self):
        with TemporaryDirectory() as directory:
            def create_refs():
                store = ArtifactStore(directory)
                verifier = ArtifactStoreEvidenceVerifier(
                    store,
                    evidence_root=directory,
                )
                return weakref.ref(verifier), weakref.ref(store)

            verifier_ref, store_ref = create_refs()
            gc.collect()

            self.assertIsNone(verifier_ref())
            self.assertIsNone(store_ref())

    def test_verifier_subclass_is_rejected_before_verdict_dispatch(self):
        envelope = self._envelope()
        observations = self._observations(envelope)
        calls = []

        class HostileVerifier(ArtifactStoreEvidenceVerifier):
            def __init__(self):
                pass

            @property
            def identity(self):
                calls.append("identity")
                return "FORGED"

            def verify(self, _ref):
                calls.append("verify")
                raise AssertionError("hostile verifier callback must not run")

        with self.assertRaises(TypeError):
            assess_bounded_real_qualification(
                envelope=envelope,
                prerequisite_evidence=(),
                observations=observations,
                evidence_verifier=HostileVerifier(),
            )

        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
