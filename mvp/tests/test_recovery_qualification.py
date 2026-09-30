from hashlib import sha256
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import NAMESPACE_URL, uuid5

from research.autotrade_research.artifacts.store import ArtifactStore

import mvp.autotrade_mvp.recovery_qualification as recovery_qualification_module
from mvp.autotrade_mvp.qualification_attestation import (
    EvidenceArtifactRef,
    QualificationScope,
    SignedQualificationAttestation,
    verify_qualification_attestation,
)
from mvp.autotrade_mvp.recovery_qualification import (
    RecoveryEvidenceStatus,
    RecoveryQualificationDecision,
    RecoveryQualificationPolicy,
    RecoveryScenario,
    RecoveryScenarioEvidence,
    qualify_recovery_release,
    recovery_evidence_receipt_bytes,
    recovery_evidence_receipt_metadata,
    recovery_policy_subject_requirement,
)
from mvp.tests.test_qualification_attestation import (
    attestation,
    policy as attestation_policy,
    root as attestation_root,
    sign,
)


SOURCE_SHA = "a" * 40
RELEASE_ARTIFACT_BYTES = b"autotrade-recovery-release-artifact"
ARTIFACT_SHA = "sha256:" + sha256(RELEASE_ARTIFACT_BYTES).hexdigest()
RELEASE_ARTIFACT_ID = str(
    uuid5(NAMESPACE_URL, "autotrade-recovery-release-artifact")
)
EVIDENCE_SCHEMA = "recovery-evidence/1.0.0"
PROTOCOL_ID = "wp59-recovery-protocol-v1"
DECISION_EVIDENCE_SET_SHA = "sha256:" + ("c" * 64)
REQUIRED_TESTS = {
    scenario: (f"recovery::{scenario.value.lower()}",)
    for scenario in RecoveryScenario
}


def policy(**overrides):
    limits = {scenario: 60_000 for scenario in RecoveryScenario}
    limits.update(overrides.pop("limits", {}))
    return RecoveryQualificationPolicy(
        source_sha=overrides.pop("source_sha", SOURCE_SHA),
        release_artifact_id=overrides.pop("artifact_id", RELEASE_ARTIFACT_ID),
        release_artifact_sha256=overrides.pop("artifact", ARTIFACT_SHA),
        evidence_schema_version=overrides.pop("evidence_schema_version", EVIDENCE_SCHEMA),
        protocol_id=overrides.pop("protocol_id", PROTOCOL_ID),
        max_downtime_ms=limits,
        required_tests=overrides.pop("required_tests", REQUIRED_TESTS),
        **overrides,
    )


def evidence(
    scenario,
    *,
    status=RecoveryEvidenceStatus.PASS,
    source_sha=SOURCE_SHA,
    artifact_id=RELEASE_ARTIFACT_ID,
    artifact=ARTIFACT_SHA,
    evidence_artifact_id=None,
    evidence_artifact_sha256=None,
    downtime_ms=1_000,
    data_loss_events=0,
    duplicate_external_actions=0,
    unknown_submissions=0,
    unresolved_reconciliation_items=0,
    journal_integrity_verified=True,
    backup_integrity_verified=True,
    reconciliation_complete=True,
    authority_reacquired=True,
    old_sender_fenced=True,
    rollback_completed=True,
    open_risk_present=False,
    protection_state="NO_OPEN_RISK",
    evidence_schema_version=EVIDENCE_SCHEMA,
    protocol_id=PROTOCOL_ID,
    test_run_id=None,
    tests_run=None,
    unresolved_limits=(),
):
    receipt_id = evidence_artifact_id or str(
        uuid5(
            NAMESPACE_URL,
            f"autotrade-recovery-evidence:{scenario.value}",
        )
    )
    values = dict(
        scenario=scenario,
        status=status,
        source_sha=source_sha,
        release_artifact_id=artifact_id,
        release_artifact_sha256=artifact,
        evidence_artifact_id=receipt_id,
        evidence_artifact_sha256="sha256:" + "0" * 64,
        evidence_refs=(f"artifact://recovery/{scenario.value.lower()}",),
        evidence_schema_version=evidence_schema_version,
        protocol_id=protocol_id,
        test_run_id=test_run_id or f"run-{scenario.value.lower()}-001",
        tests_run=REQUIRED_TESTS[scenario] if tests_run is None else tests_run,
        unresolved_limits=unresolved_limits,
        downtime_ms=downtime_ms,
        data_loss_events=data_loss_events,
        duplicate_external_actions=duplicate_external_actions,
        unknown_submissions=unknown_submissions,
        unresolved_reconciliation_items=unresolved_reconciliation_items,
        journal_integrity_verified=journal_integrity_verified,
        backup_integrity_verified=backup_integrity_verified,
        reconciliation_complete=reconciliation_complete,
        authority_reacquired=authority_reacquired,
        old_sender_fenced=old_sender_fenced,
        rollback_completed=rollback_completed,
        open_risk_present=open_risk_present,
        protection_state=protection_state,
    )
    provisional = RecoveryScenarioEvidence(**values)
    receipt_hash = (
        evidence_artifact_sha256
        or "sha256:" + sha256(
            recovery_evidence_receipt_bytes(provisional)
        ).hexdigest()
    )
    return RecoveryScenarioEvidence(
        **{**values, "evidence_artifact_sha256": receipt_hash}
    )


def complete_evidence():
    return [evidence(scenario) for scenario in RecoveryScenario]


def _receipt_bytes(item):
    return recovery_evidence_receipt_bytes(item)


def qualify(
    *,
    policy,
    evidence,
    omit_evidence_ids=(),
    corrupt_evidence_id=None,
    omit_release_artifact=False,
    trusted=False,
    omit_attestation_scenarios=(),
    attested_policy=None,
    mutate_during_verify=None,
):
    with TemporaryDirectory() as directory:
        store = ArtifactStore(directory)
        if not omit_release_artifact:
            store.publish_bytes(
                artifact_id=policy.release_artifact_id,
                data=RELEASE_ARTIFACT_BYTES,
                media_type="application/vnd.autotrade.release-artifact",
                rights={"storage": True, "export": False},
                source_refs=[f"git:{policy.source_sha}"],
                metadata={
                    "evidence_kind": "RECOVERY_RELEASE_ARTIFACT",
                    "source_sha": policy.source_sha,
                },
            )
        for item in evidence:
            if item.evidence_artifact_id in omit_evidence_ids:
                continue
            store.publish_bytes(
                artifact_id=item.evidence_artifact_id,
                data=_receipt_bytes(item),
                media_type="application/vnd.autotrade.recovery-evidence",
                rights={"storage": True, "export": False},
                source_refs=[f"git:{item.source_sha}"],
                metadata=recovery_evidence_receipt_metadata(item),
            )
        if corrupt_evidence_id is not None:
            manifest = store.load_manifest(corrupt_evidence_id)
            digest = manifest["sha256"].removeprefix("sha256:")
            (store.objects / digest[:2] / digest).write_bytes(b"corrupt")
        trust_kwargs = {}
        canonical_policy = None
        if trusted:
            trust_root = attestation_root(
                scopes=(QualificationScope("RECOVERY", "RELEASE"),)
            )
            canonical_policy = attestation_policy(trust_root)
            bound_policy = policy if attested_policy is None else attested_policy
            attested_refs = tuple(
                EvidenceArtifactRef(
                    artifact_id=item.evidence_artifact_id,
                    sha256=item.evidence_artifact_sha256,
                    media_type="application/vnd.autotrade.recovery-evidence",
                    evidence_kind="RECOVERY_SCENARIO_EVIDENCE",
                    source_sha=item.source_sha,
                )
                for item in evidence
                if item.scenario not in set(omit_attestation_scenarios)
            )
            signed = attestation(
                trust_root,
                source_sha=policy.source_sha,
                domain="RECOVERY",
                gate="RELEASE",
                package_id="WP-59",
                protocol_id=policy.protocol_id,
                protocol_version=policy.evidence_schema_version,
                requirement_ids=(
                    "recovery-release-qualification",
                    recovery_policy_subject_requirement(bound_policy),
                ),
                evidence_refs=attested_refs,
                release_artifact_id=policy.release_artifact_id,
                release_artifact_sha256=policy.release_artifact_sha256,
                result="PASS",
            )
            trust_kwargs = {
                "qualification_receipt": SignedQualificationAttestation(
                    signed, sign(signed)
                ),
                "qualification_policy": canonical_policy,
                "expected_policy_id": canonical_policy.policy_id,
                "expected_policy_version": canonical_policy.policy_version,
            }

        def canonical_verify(receipt_arg, **kwargs):
            if canonical_policy is None:
                raise AssertionError("canonical verifier used without trusted fixture")
            if mutate_during_verify is not None:
                mutate_during_verify()
            return verify_qualification_attestation(
                receipt_arg,
                policy=canonical_policy,
                expected_policy_id=canonical_policy.policy_id,
                expected_policy_version=canonical_policy.policy_version,
                **kwargs,
            )

        with patch.object(
            recovery_qualification_module,
            "verify_canonical_qualification_attestation",
            side_effect=canonical_verify,
        ):
            return qualify_recovery_release(
                policy=policy,
                evidence=evidence,
                evidence_store=store,
                evidence_root=directory,
                **trust_kwargs,
            )


class RecoveryReleaseQualificationTests(unittest.TestCase):
    def test_self_asserted_recovery_evidence_is_inconclusive_without_verifier(self):
        decision = qualify_recovery_release(
            policy=policy(),
            evidence=complete_evidence(),
        )
        self.assertEqual(decision.status, RecoveryEvidenceStatus.INCONCLUSIVE)
        self.assertTrue(
            all(
                any(
                    blocker == f"{scenario.value.lower()}:evidence_integrity_unverified"
                    for blocker in decision.blockers
                )
                for scenario in RecoveryScenario
            )
        )
        self.assertFalse(decision.authorizes_trading)

    def test_arbitrary_callback_cannot_self_approve_recovery(self):
        with self.assertRaisesRegex(TypeError, "evidence_store must be ArtifactStore"):
            qualify_recovery_release(
                policy=policy(),
                evidence=complete_evidence(),
                evidence_store=lambda _item: True,
            )

    def test_recovery_terminal_rejects_subclass_and_hostile_scalar_graphs(self):
        base = evidence(RecoveryScenario.NETWORK_LOSS)

        class DerivedRecoveryScenarioEvidence(RecoveryScenarioEvidence):
            pass

        derived = DerivedRecoveryScenarioEvidence(
            **{
                name: getattr(base, name)
                for name in base.__dataclass_fields__
            }
        )
        with self.assertRaisesRegex(TypeError, "canonical RecoveryScenarioEvidence"):
            qualify_recovery_release(
                policy=policy(),
                evidence=[derived],
            )

        class HostileText(str):
            pass

        poisoned = evidence(RecoveryScenario.NETWORK_LOSS)
        object.__setattr__(poisoned, "source_sha", HostileText(poisoned.source_sha))
        with self.assertRaisesRegex(TypeError, "source_sha must use exact str"):
            qualify_recovery_release(
                policy=policy(),
                evidence=[poisoned],
            )

    def test_verifier_side_mutation_cannot_change_detached_recovery_decision(self):
        items = complete_evidence()
        target = items[0]
        original_downtime = target.downtime_ms

        def mutate_original():
            object.__setattr__(
                target,
                "downtime_ms",
                policy().max_downtime_ms[target.scenario] + 1,
            )

        decision = qualify(
            policy=policy(),
            evidence=items,
            trusted=True,
            mutate_during_verify=mutate_original,
        )

        self.assertEqual(decision.status, RecoveryEvidenceStatus.PASS)
        self.assertEqual(decision.blockers, ())
        self.assertEqual(
            decision.measured_downtime_ms[target.scenario],
            original_downtime,
        )
        self.assertGreater(
            target.downtime_ms,
            policy().max_downtime_ms[target.scenario],
        )

    def test_missing_or_corrupt_recovery_receipt_fails_closed(self):
        items = complete_evidence()
        target = items[0]
        missing = qualify(
            policy=policy(),
            evidence=items,
            omit_evidence_ids={target.evidence_artifact_id},
        )
        self.assertEqual(missing.status, RecoveryEvidenceStatus.INCONCLUSIVE)
        self.assertIn(
            f"{target.scenario.value.lower()}:evidence_integrity_unverified",
            missing.blockers,
        )

        corrupt = qualify(
            policy=policy(),
            evidence=items,
            corrupt_evidence_id=target.evidence_artifact_id,
        )
        self.assertEqual(corrupt.status, RecoveryEvidenceStatus.INCONCLUSIVE)
        self.assertIn(
            f"{target.scenario.value.lower()}:evidence_integrity_unverified",
            corrupt.blockers,
        )

    def test_recovery_receipt_bytes_bind_decision_relevant_facts(self):
        items = complete_evidence()
        original = items[0]
        original_bytes = recovery_evidence_receipt_bytes(original)
        mutated = evidence(
            original.scenario,
            downtime_ms=original.downtime_ms + 1,
            evidence_artifact_id=original.evidence_artifact_id,
            evidence_artifact_sha256=original.evidence_artifact_sha256,
        )
        self.assertNotEqual(
            recovery_evidence_receipt_bytes(mutated),
            original_bytes,
        )
        with TemporaryDirectory() as directory:
            store = ArtifactStore(directory)
            current_policy = policy()
            store.publish_bytes(
                artifact_id=current_policy.release_artifact_id,
                data=RELEASE_ARTIFACT_BYTES,
                media_type="application/vnd.autotrade.release-artifact",
                rights={"storage": True, "export": False},
                source_refs=[f"git:{current_policy.source_sha}"],
                metadata={
                    "evidence_kind": "RECOVERY_RELEASE_ARTIFACT",
                    "source_sha": current_policy.source_sha,
                },
            )
            store.publish_bytes(
                artifact_id=mutated.evidence_artifact_id,
                data=original_bytes,
                media_type="application/vnd.autotrade.recovery-evidence",
                rights={"storage": True, "export": False},
                source_refs=[f"git:{mutated.source_sha}"],
                metadata=recovery_evidence_receipt_metadata(mutated),
            )
            for item in items[1:]:
                store.publish_bytes(
                    artifact_id=item.evidence_artifact_id,
                    data=recovery_evidence_receipt_bytes(item),
                    media_type="application/vnd.autotrade.recovery-evidence",
                    rights={"storage": True, "export": False},
                    source_refs=[f"git:{item.source_sha}"],
                    metadata=recovery_evidence_receipt_metadata(item),
                )
            decision = qualify_recovery_release(
                policy=current_policy,
                evidence=[mutated, *items[1:]],
                evidence_store=store,
                evidence_root=directory,
            )
        self.assertEqual(
            decision.status,
            RecoveryEvidenceStatus.INCONCLUSIVE,
        )
        self.assertIn(
            f"{mutated.scenario.value.lower()}:evidence_integrity_unverified",
            decision.blockers,
        )

    def test_missing_release_artifact_fails_closed(self):
        decision = qualify(
            policy=policy(),
            evidence=complete_evidence(),
            omit_release_artifact=True,
        )
        self.assertEqual(decision.status, RecoveryEvidenceStatus.INCONCLUSIVE)
        self.assertIn("release_artifact:integrity_unverified", decision.blockers)

    def test_uppercase_release_identities_are_rejected_not_normalized(self):
        with self.assertRaisesRegex(ValueError, "lowercase"):
            policy(source_sha="A" * 40)
        with self.assertRaisesRegex(ValueError, "lowercase"):
            policy(artifact="sha256:" + "B" * 64)

    def test_self_populated_complete_store_cannot_establish_recovery_pass(self):
        decision = qualify(
            policy=policy(),
            evidence=complete_evidence(),
        )
        self.assertEqual(decision.status, RecoveryEvidenceStatus.INCONCLUSIVE)
        self.assertIn(
            "independent_evidence_trust_incomplete",
            decision.blockers,
        )
        self.assertFalse(decision.authorizes_trading)
        self.assertEqual(
            set(decision.measured_downtime_ms),
            set(RecoveryScenario),
        )

    def test_signed_attestation_allows_terminal_recovery_pass(self):
        decision = qualify(
            policy=policy(),
            evidence=complete_evidence(),
            trusted=True,
        )
        self.assertEqual(decision.status, RecoveryEvidenceStatus.PASS)
        self.assertEqual(decision.blockers, ())
        self.assertIsNotNone(decision.qualification_attestation_id)
        self.assertTrue(
            decision.qualification_attestation_digest.startswith("sha256:")
        )
        self.assertTrue(decision.qualification_policy_id.startswith("sha256:"))
        self.assertTrue(
            decision.qualification_trust_root_id.startswith("sha256:")
        )
        self.assertFalse(decision.authorizes_trading)

    def test_signed_recovery_policy_cannot_be_loosened_after_outcome(self):
        original_policy = policy()
        loosened_policy = policy(
            limits={scenario: 120_000 for scenario in RecoveryScenario},
        )
        decision = qualify(
            policy=loosened_policy,
            evidence=complete_evidence(),
            trusted=True,
            attested_policy=original_policy,
        )
        self.assertEqual(decision.status, RecoveryEvidenceStatus.FAIL)
        self.assertIn("independent_recovery_policy_mismatch", decision.blockers)
        self.assertFalse(decision.matches_policy(original_policy))
        self.assertTrue(decision.matches_policy(loosened_policy))

    def test_signed_attestation_must_cover_exact_scenario_evidence_set(self):
        decision = qualify(
            policy=policy(),
            evidence=complete_evidence(),
            trusted=True,
            omit_attestation_scenarios=(RecoveryScenario.NETWORK_LOSS,),
        )
        self.assertEqual(decision.status, RecoveryEvidenceStatus.FAIL)
        self.assertIn(
            "independent_evidence_set_mismatch",
            decision.blockers,
        )

    def test_missing_scenario_is_inconclusive_not_silently_passed(self):
        items = [
            item
            for item in complete_evidence()
            if item.scenario is not RecoveryScenario.SESSION_LOSS
        ]
        decision = qualify(policy=policy(), evidence=items)
        self.assertEqual(decision.status, RecoveryEvidenceStatus.INCONCLUSIVE)
        self.assertIn("missing_scenario:SESSION_LOSS", decision.blockers)

    def test_explicit_inconclusive_scenario_remains_inconclusive(self):
        items = [
            evidence(
                scenario,
                status=(
                    RecoveryEvidenceStatus.INCONCLUSIVE
                    if scenario is RecoveryScenario.NETWORK_LOSS
                    else RecoveryEvidenceStatus.PASS
                ),
            )
            for scenario in RecoveryScenario
        ]
        decision = qualify(policy=policy(), evidence=items)
        self.assertEqual(decision.status, RecoveryEvidenceStatus.INCONCLUSIVE)
        self.assertIn("network_loss:scenario_inconclusive", decision.blockers)

    def test_exact_source_and_release_artifact_are_hard_boundaries(self):
        wrong_source = complete_evidence()
        wrong_source[0] = evidence(
            wrong_source[0].scenario,
            source_sha="c" * 40,
        )
        decision = qualify(policy=policy(), evidence=wrong_source)
        self.assertEqual(decision.status, RecoveryEvidenceStatus.FAIL)
        self.assertTrue(
            any(blocker.endswith(":source_sha_mismatch") for blocker in decision.blockers)
        )

        wrong_artifact = complete_evidence()
        wrong_artifact[1] = evidence(
            wrong_artifact[1].scenario,
            artifact="sha256:" + "d" * 64,
        )
        decision = qualify(policy=policy(), evidence=wrong_artifact)
        self.assertEqual(decision.status, RecoveryEvidenceStatus.FAIL)
        self.assertTrue(
            any(blocker.endswith(":release_artifact_mismatch") for blocker in decision.blockers)
        )

    def test_protocol_schema_and_required_test_set_are_hard_boundaries(self):
        cases = (
            (
                {"evidence_schema_version": "recovery-evidence/2.0.0"},
                "evidence_schema_version_mismatch",
            ),
            ({"protocol_id": "different-protocol"}, "protocol_id_mismatch"),
            ({"tests_run": ("some-other-test",)}, "required_test_missing"),
        )
        for kwargs, suffix in cases:
            with self.subTest(suffix=suffix):
                items = complete_evidence()
                items[0] = evidence(items[0].scenario, **kwargs)
                decision = qualify(policy=policy(), evidence=items)
                self.assertEqual(decision.status, RecoveryEvidenceStatus.FAIL)
                self.assertTrue(
                    any(suffix in blocker for blocker in decision.blockers)
                )

    def test_unresolved_limit_is_explicit_and_prevents_full_pass(self):
        items = complete_evidence()
        items[0] = evidence(
            items[0].scenario,
            unresolved_limits=("power-cut harness did not cover RAID controller cache",),
        )
        decision = qualify(policy=policy(), evidence=items)
        self.assertEqual(decision.status, RecoveryEvidenceStatus.INCONCLUSIVE)
        self.assertTrue(
            any(":unresolved_limit:" in blocker for blocker in decision.blockers)
        )

    def test_test_run_and_test_lists_are_required_unique_text(self):
        with self.assertRaisesRegex(ValueError, "test_run_id"):
            evidence(RecoveryScenario.NETWORK_LOSS, test_run_id=" ")
        with self.assertRaisesRegex(ValueError, "tests_run"):
            evidence(RecoveryScenario.NETWORK_LOSS, tests_run=())
        with self.assertRaisesRegex(ValueError, "unique"):
            evidence(
                RecoveryScenario.NETWORK_LOSS,
                tests_run=("same-test", "same-test"),
            )

    def test_loss_duplicate_send_unknown_and_unreconciled_truth_fail_closed(self):
        cases = (
            ("data_loss_events", {"data_loss_events": 1}, "data_loss_observed"),
            (
                "duplicate_external_actions",
                {"duplicate_external_actions": 1},
                "duplicate_external_action",
            ),
            (
                "unknown_submissions",
                {"unknown_submissions": 1},
                "unknown_submission_unresolved",
            ),
            (
                "unresolved_reconciliation_items",
                {"unresolved_reconciliation_items": 1},
                "reconciliation_unresolved",
            ),
        )
        for name, kwargs, suffix in cases:
            with self.subTest(name=name):
                items = complete_evidence()
                items[0] = evidence(items[0].scenario, **kwargs)
                decision = qualify(policy=policy(), evidence=items)
                self.assertEqual(decision.status, RecoveryEvidenceStatus.FAIL)
                self.assertTrue(
                    any(blocker.endswith(":" + suffix) for blocker in decision.blockers)
                )

    def test_integrity_reconciliation_authority_and_fencing_are_required(self):
        cases = (
            ({"journal_integrity_verified": False}, "journal_integrity_unverified"),
            ({"backup_integrity_verified": False}, "backup_integrity_unverified"),
            ({"reconciliation_complete": False}, "reconciliation_incomplete"),
            ({"authority_reacquired": False}, "authority_not_reacquired"),
            ({"old_sender_fenced": False}, "old_sender_not_fenced"),
        )
        for kwargs, suffix in cases:
            with self.subTest(suffix=suffix):
                items = complete_evidence()
                items[0] = evidence(items[0].scenario, **kwargs)
                decision = qualify(policy=policy(), evidence=items)
                self.assertEqual(decision.status, RecoveryEvidenceStatus.FAIL)
                self.assertTrue(
                    any(blocker.endswith(":" + suffix) for blocker in decision.blockers)
                )

    def test_split_brain_old_sender_must_be_fenced(self):
        items = complete_evidence()
        index = next(
            i
            for i, item in enumerate(items)
            if item.scenario is RecoveryScenario.SPLIT_BRAIN_ATTEMPT
        )
        items[index] = evidence(
            RecoveryScenario.SPLIT_BRAIN_ATTEMPT,
            old_sender_fenced=False,
        )
        decision = qualify(policy=policy(), evidence=items)
        self.assertEqual(decision.status, RecoveryEvidenceStatus.FAIL)
        self.assertIn(
            "split_brain_attempt:old_sender_not_fenced",
            decision.blockers,
        )

    def test_upgrade_failure_requires_successful_rollback(self):
        items = complete_evidence()
        index = next(
            i
            for i, item in enumerate(items)
            if item.scenario is RecoveryScenario.UPGRADE_FAILURE
        )
        items[index] = evidence(
            RecoveryScenario.UPGRADE_FAILURE,
            rollback_completed=False,
        )
        decision = qualify(policy=policy(), evidence=items)
        self.assertEqual(decision.status, RecoveryEvidenceStatus.FAIL)
        self.assertIn("upgrade_failure:rollback_incomplete", decision.blockers)

    def test_declared_downtime_limit_is_enforced_per_scenario(self):
        items = complete_evidence()
        items[0] = evidence(items[0].scenario, downtime_ms=60_001)
        decision = qualify(policy=policy(), evidence=items)
        self.assertEqual(decision.status, RecoveryEvidenceStatus.FAIL)
        self.assertTrue(
            any(
                blocker.endswith(":downtime_limit_exceeded")
                for blocker in decision.blockers
            )
        )

    def test_open_risk_requires_qualified_protection_evidence(self):
        protected = evidence(
            RecoveryScenario.NETWORK_LOSS,
            open_risk_present=True,
            protection_state="PROVIDER_NATIVE",
        )
        self.assertTrue(protected.open_risk_present)
        with self.assertRaisesRegex(ValueError, "requires qualified protection"):
            evidence(
                RecoveryScenario.NETWORK_LOSS,
                open_risk_present=True,
                protection_state="NO_OPEN_RISK",
            )

    def test_evidence_identity_and_shape_fail_closed(self):
        with self.assertRaisesRegex(ValueError, "evidence_refs"):
            RecoveryScenarioEvidence(
                scenario=RecoveryScenario.POWER_LOSS,
                status=RecoveryEvidenceStatus.PASS,
                source_sha=SOURCE_SHA,
                release_artifact_id=RELEASE_ARTIFACT_ID,
                release_artifact_sha256=ARTIFACT_SHA,
                evidence_artifact_id=str(
                    uuid5(NAMESPACE_URL, "recovery-direct-evidence")
                ),
                evidence_artifact_sha256="sha256:" + sha256(b"direct").hexdigest(),
                evidence_refs=(),
                evidence_schema_version=EVIDENCE_SCHEMA,
                protocol_id=PROTOCOL_ID,
                test_run_id="run-direct",
                tests_run=REQUIRED_TESTS[RecoveryScenario.POWER_LOSS],
                unresolved_limits=(),
                downtime_ms=0,
                data_loss_events=0,
                duplicate_external_actions=0,
                unknown_submissions=0,
                unresolved_reconciliation_items=0,
                journal_integrity_verified=True,
                backup_integrity_verified=True,
                reconciliation_complete=True,
                authority_reacquired=True,
                old_sender_fenced=True,
                rollback_completed=True,
                open_risk_present=False,
                protection_state="NO_OPEN_RISK",
            )
        with self.assertRaisesRegex(ValueError, "non-negative integer"):
            evidence(RecoveryScenario.POWER_LOSS, downtime_ms=True)

    def test_duplicate_scenario_evidence_is_rejected(self):
        item = evidence(RecoveryScenario.POWER_LOSS)
        with self.assertRaisesRegex(ValueError, "duplicate evidence"):
            qualify(
                policy=policy(),
                evidence=[item, item],
            )


    def test_direct_pass_decision_cannot_omit_required_scenarios(self):
        with self.assertRaisesRegex(ValueError, "measure every required scenario"):
            RecoveryQualificationDecision(
                status=RecoveryEvidenceStatus.PASS,
                source_sha=SOURCE_SHA,
                release_artifact_id=RELEASE_ARTIFACT_ID,
                release_artifact_sha256=ARTIFACT_SHA,
                evidence_schema_version=EVIDENCE_SCHEMA,
                protocol_id=PROTOCOL_ID,
                evidence_set_sha256=DECISION_EVIDENCE_SET_SHA,
                recovery_policy_requirement=recovery_policy_subject_requirement(policy()),
                blockers=(),
                measured_downtime_ms={RecoveryScenario.POWER_LOSS: 10},
            )

    def test_direct_pass_decision_cannot_hide_blockers(self):
        with self.assertRaisesRegex(ValueError, "cannot contain blockers"):
            RecoveryQualificationDecision(
                status=RecoveryEvidenceStatus.PASS,
                source_sha=SOURCE_SHA,
                release_artifact_id=RELEASE_ARTIFACT_ID,
                release_artifact_sha256=ARTIFACT_SHA,
                evidence_schema_version=EVIDENCE_SCHEMA,
                protocol_id=PROTOCOL_ID,
                evidence_set_sha256=DECISION_EVIDENCE_SET_SHA,
                recovery_policy_requirement=recovery_policy_subject_requirement(policy()),
                blockers=("forged:blocker",),
                measured_downtime_ms={scenario: 10 for scenario in RecoveryScenario},
            )

    def test_nonpass_decision_requires_explicit_blocker(self):
        with self.assertRaisesRegex(ValueError, "requires blockers"):
            RecoveryQualificationDecision(
                status=RecoveryEvidenceStatus.INCONCLUSIVE,
                source_sha=SOURCE_SHA,
                release_artifact_id=RELEASE_ARTIFACT_ID,
                release_artifact_sha256=ARTIFACT_SHA,
                evidence_schema_version=EVIDENCE_SCHEMA,
                protocol_id=PROTOCOL_ID,
                evidence_set_sha256=DECISION_EVIDENCE_SET_SHA,
                recovery_policy_requirement=recovery_policy_subject_requirement(policy()),
                blockers=(),
                measured_downtime_ms={},
            )

    def test_qualification_decision_is_bound_to_exact_release_protocol_and_evidence_set(self):
        current_policy = policy()
        decision = qualify(
            policy=current_policy,
            evidence=complete_evidence(),
        )
        self.assertEqual(decision.source_sha, current_policy.source_sha)
        self.assertEqual(
            decision.release_artifact_id,
            current_policy.release_artifact_id,
        )
        self.assertEqual(
            decision.release_artifact_sha256,
            current_policy.release_artifact_sha256,
        )
        self.assertEqual(
            decision.evidence_schema_version,
            current_policy.evidence_schema_version,
        )
        self.assertEqual(decision.protocol_id, current_policy.protocol_id)
        self.assertRegex(decision.evidence_set_sha256, r"^sha256:[0-9a-f]{64}$")
        self.assertTrue(decision.matches_policy(current_policy))
        changed_thresholds = policy(
            limits={RecoveryScenario.POWER_LOSS: 120_000},
        )
        self.assertFalse(decision.matches_policy(changed_thresholds))

        other_release = policy(
            artifact_id=str(
                uuid5(NAMESPACE_URL, "different-recovery-release-artifact")
            ),
            artifact="sha256:" + ("d" * 64),
        )
        self.assertFalse(decision.matches_policy(other_release))

    def test_recovery_identity_rejects_whitespace_and_accepts_git_sha256(self):
        sha256_policy = policy(source_sha="d" * 64)
        self.assertEqual(sha256_policy.source_sha, "d" * 64)
        for invalid in (" " + SOURCE_SHA, SOURCE_SHA + " ", "A" * 40, "D" * 64):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(
                ValueError,
                "canonical lowercase",
            ):
                policy(source_sha=invalid)
        with self.assertRaisesRegex(ValueError, "canonical sha256"):
            policy(artifact=ARTIFACT_SHA + " ")

    def test_direct_pass_decision_requires_independently_verifiable_trust(self):
        with self.assertRaisesRegex(
            ValueError,
            "requires accepted qualification trust",
        ):
            RecoveryQualificationDecision(
                status=RecoveryEvidenceStatus.PASS,
                source_sha=SOURCE_SHA,
                release_artifact_id=RELEASE_ARTIFACT_ID,
                release_artifact_sha256=ARTIFACT_SHA,
                evidence_schema_version=EVIDENCE_SCHEMA,
                protocol_id=PROTOCOL_ID,
                evidence_set_sha256=DECISION_EVIDENCE_SET_SHA,
                recovery_policy_requirement=recovery_policy_subject_requirement(policy()),
                blockers=(),
                measured_downtime_ms={
                    scenario: 10 for scenario in RecoveryScenario
                },
            )

    def test_decision_copies_measured_mapping_and_rejects_boolean_downtime(self):
        measured = {scenario: 10 for scenario in RecoveryScenario}
        decision = RecoveryQualificationDecision(
            status=RecoveryEvidenceStatus.INCONCLUSIVE,
            source_sha=SOURCE_SHA,
            release_artifact_id=RELEASE_ARTIFACT_ID,
            release_artifact_sha256=ARTIFACT_SHA,
            evidence_schema_version=EVIDENCE_SCHEMA,
            protocol_id=PROTOCOL_ID,
            evidence_set_sha256=DECISION_EVIDENCE_SET_SHA,
            recovery_policy_requirement=recovery_policy_subject_requirement(policy()),
            blockers=("fixture:inconclusive",),
            measured_downtime_ms=measured,
        )
        measured[RecoveryScenario.POWER_LOSS] = 999
        self.assertEqual(
            decision.measured_downtime_ms[RecoveryScenario.POWER_LOSS],
            10,
        )
        with self.assertRaisesRegex(ValueError, "non-negative integer"):
            RecoveryQualificationDecision(
                status=RecoveryEvidenceStatus.INCONCLUSIVE,
                source_sha=SOURCE_SHA,
                release_artifact_id=RELEASE_ARTIFACT_ID,
                release_artifact_sha256=ARTIFACT_SHA,
                evidence_schema_version=EVIDENCE_SCHEMA,
                protocol_id=PROTOCOL_ID,
                evidence_set_sha256=DECISION_EVIDENCE_SET_SHA,
                recovery_policy_requirement=recovery_policy_subject_requirement(policy()),
                blockers=("fixture:inconclusive",),
                measured_downtime_ms={
                    scenario: (True if scenario is RecoveryScenario.POWER_LOSS else 10)
                    for scenario in RecoveryScenario
                },
            )


if __name__ == "__main__":
    unittest.main()
