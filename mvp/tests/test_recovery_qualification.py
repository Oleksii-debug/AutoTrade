import unittest

from mvp.autotrade_mvp.recovery_qualification import (
    RecoveryEvidenceStatus,
    RecoveryQualificationPolicy,
    RecoveryScenario,
    RecoveryScenarioEvidence,
    qualify_recovery_release,
)


SOURCE_SHA = "a" * 40
ARTIFACT_SHA = "sha256:" + "b" * 64


def policy(**overrides):
    limits = {scenario: 60_000 for scenario in RecoveryScenario}
    limits.update(overrides.pop("limits", {}))
    return RecoveryQualificationPolicy(
        source_sha=overrides.pop("source_sha", SOURCE_SHA),
        release_artifact_sha256=overrides.pop("artifact", ARTIFACT_SHA),
        max_downtime_ms=limits,
        **overrides,
    )


def evidence(
    scenario,
    *,
    status=RecoveryEvidenceStatus.PASS,
    source_sha=SOURCE_SHA,
    artifact=ARTIFACT_SHA,
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
):
    return RecoveryScenarioEvidence(
        scenario=scenario,
        status=status,
        source_sha=source_sha,
        release_artifact_sha256=artifact,
        evidence_refs=(f"artifact://recovery/{scenario.value.lower()}",),
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


def complete_evidence():
    return [evidence(scenario) for scenario in RecoveryScenario]


def _trusted_evidence_verifier(_item):
    return True


def qualify(*, policy, evidence):
    return qualify_recovery_release(
        policy=policy,
        evidence=evidence,
        evidence_verifier=_trusted_evidence_verifier,
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
                    blocker == f"{scenario.value.lower()}:evidence_unverified"
                    for blocker in decision.blockers
                )
                for scenario in RecoveryScenario
            )
        )
        self.assertFalse(decision.authorizes_trading)

    def test_broken_evidence_verifier_fails_closed(self):
        def broken(_item):
            raise RuntimeError("evidence store unavailable")

        decision = qualify_recovery_release(
            policy=policy(),
            evidence=complete_evidence(),
            evidence_verifier=broken,
        )
        self.assertEqual(decision.status, RecoveryEvidenceStatus.INCONCLUSIVE)

    def test_uppercase_release_identities_are_rejected_not_normalized(self):
        with self.assertRaisesRegex(ValueError, "lowercase"):
            policy(source_sha="A" * 40)
        with self.assertRaisesRegex(ValueError, "lowercase"):
            policy(artifact="sha256:" + "B" * 64)

    def test_complete_same_build_recovery_evidence_can_pass_without_granting_authority(self):
        decision = qualify(
            policy=policy(),
            evidence=complete_evidence(),
        )
        self.assertEqual(decision.status, RecoveryEvidenceStatus.PASS)
        self.assertEqual(decision.blockers, ())
        self.assertFalse(decision.authorizes_trading)
        self.assertEqual(
            set(decision.measured_downtime_ms),
            set(RecoveryScenario),
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
                release_artifact_sha256=ARTIFACT_SHA,
                evidence_refs=(),
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


if __name__ == "__main__":
    unittest.main()
