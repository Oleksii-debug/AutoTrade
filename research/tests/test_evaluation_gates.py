from dataclasses import replace
from decimal import Decimal, ROUND_DOWN, ROUND_UP, localcontext
import json
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import uuid4

from research.autotrade_research.artifacts.store import ArtifactStore
from research.autotrade_research.evaluation import gates as gate_module
from research.autotrade_research.evaluation.gates import (
    EvaluationEvidence,
    GateDecision,
    GateEvidenceRef,
    GateProfile,
    evaluate_gates,
    gate_report_payload,
)


BUNDLE_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
SOURCE_SHA = "a" * 40
EVIDENCE_KINDS = (
    "profile",
    "code",
    "data",
    "model",
    "config",
    "cost",
    "rights",
    "environment",
    "trial_log",
    "causal_audit",
    "financial_invariants",
    "retention",
    "locked_evaluation",
    "metrics",
    "independent_review",
)
REPORT_KINDS = frozenset(
    {
        "profile",
        "trial_log",
        "causal_audit",
        "financial_invariants",
        "retention",
        "locked_evaluation",
        "metrics",
    }
)
# This is deliberately not a valid signed qualification receipt. Positive unit
# paths patch only the external-review verifier; production code never accepts it.
UNTRUSTED_REVIEW = b'{"attestation":{},"signature_b64":"AA=="}'


def profile():
    return GateProfile.create(
        profile_id="gate-v1",
        minimum_net_advantage="0.01",
        max_drawdown="0.10",
        max_adverse_cost_loss="0.03",
        min_power="0.80",
        primary_baseline_id="champion",
        baseline_ids=("cash", "passive", "champion"),
        selection_correction="holm-v1",
        max_trials=20,
        required_regimes=("normal", "stress"),
    )


def evidence(**overrides):
    values = dict(
        registered_profile_id="gate-v1",
        profile_unchanged_after_results=True,
        reproducible=True,
        causal_audit_passed=True,
        financial_invariants_passed=True,
        trial_log_complete=True,
        dependence_aware_lower_bound="0.02",
        estimated_power="0.85",
        net_advantage="0.03",
        drawdown="0.05",
        adverse_cost_loss="0.01",
        retention_passed=True,
        untouched_holdout_passed=True,
        valid_sequential_evaluation_passed=False,
        walk_forward_passed=True,
        baseline_advantages={
            "cash": "0.04",
            "passive": "0.035",
            "champion": "0.03",
        },
        selection_correction_applied="holm-v1",
        trials_attempted=12,
        regime_coverage=frozenset({"normal", "stress"}),
        evidence_bundle_id=BUNDLE_ID,
    )
    values.update(overrides)
    return EvaluationEvidence.create(**values)


def _artifact_payload(kind, gate_profile, evaluation_evidence):
    if kind in REPORT_KINDS:
        return gate_report_payload(kind, gate_profile, evaluation_evidence)
    if kind == "independent_review":
        return UNTRUSTED_REVIEW
    return f"{gate_profile.profile_id}:{kind}".encode("utf-8")


def _publish_bundle(
    store,
    gate_profile,
    evaluation_evidence,
    *,
    kinds=EVIDENCE_KINDS,
    source_sha=SOURCE_SHA,
    payload_mutator=None,
    metadata_mutator=None,
):
    refs = {}
    for kind in kinds:
        artifact_id = str(uuid4())
        payload = _artifact_payload(kind, gate_profile, evaluation_evidence)
        if payload_mutator is not None:
            payload = payload_mutator(kind, payload)
        metadata = {
            "evidence_kind": kind,
            "profile_id": gate_profile.profile_id,
            "evidence_bundle_id": evaluation_evidence.evidence_bundle_id,
        }
        if metadata_mutator is not None:
            metadata = metadata_mutator(kind, metadata)
        media_type = (
            "application/json"
            if kind in REPORT_KINDS or kind == "independent_review"
            else "application/octet-stream"
        )
        manifest = store.publish_bytes(
            artifact_id=artifact_id,
            data=payload,
            media_type=media_type,
            rights={"storage": True, "export": False},
            source_refs=[f"git:{source_sha}"],
            metadata=metadata,
        )
        refs[kind] = GateEvidenceRef(
            artifact_id=artifact_id,
            sha256=manifest["sha256"],
        )
    return refs


def _accepted_review(*args, **kwargs):
    return True, {
        "review_attestation_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        "review_attestation_digest": "sha256:" + "b" * 64,
        "review_policy_id": "sha256:" + "c" * 64,
        "review_policy_version": "test-v1",
        "review_trust_root_id": "sha256:" + "d" * 64,
        "review_producer_id": "test-producer",
        "review_verifier_id": "test-verifier",
        "review_requirement_id": kwargs["requirement_id"],
        "review_source_sha": kwargs["expected_source_sha"],
    }


def evaluate_with_verified_bundle(gate_profile, evaluation_evidence):
    with TemporaryDirectory() as directory:
        store = ArtifactStore(directory)
        refs = _publish_bundle(store, gate_profile, evaluation_evidence)
        bound = replace(evaluation_evidence, evidence_refs=refs)
        with patch.object(
            gate_module,
            "_verify_independent_review",
            side_effect=_accepted_review,
        ):
            return evaluate_gates(
                gate_profile,
                bound,
                artifact_store=store,
                evidence_root=directory,
                expected_source_sha=SOURCE_SHA,
            )


class EvaluationGateTests(unittest.TestCase):
    def test_direct_profile_construction_cannot_bypass_registered_thresholds(self):
        with self.assertRaisesRegex(ValueError, "minimum_net_advantage"):
            GateProfile(
                profile_id="direct-bad",
                minimum_net_advantage="-0.01",
                max_drawdown="0.10",
                max_adverse_cost_loss="0.03",
                min_power="0.80",
                primary_baseline_id="champion",
                baseline_ids=("cash", "champion"),
                selection_correction="holm-v1",
                max_trials=20,
                required_regimes=("normal",),
                require_complete_trials=True,
                require_causal_audit=True,
                require_financial_invariants=True,
            )

    def test_direct_evidence_construction_enforces_exact_types_and_ranges(self):
        base = dict(
            registered_profile_id="gate-v1",
            profile_unchanged_after_results=True,
            reproducible=True,
            causal_audit_passed=True,
            financial_invariants_passed=True,
            trial_log_complete=True,
            dependence_aware_lower_bound="0.02",
            estimated_power="0.85",
            net_advantage="0.03",
            drawdown="0.05",
            adverse_cost_loss="0.01",
            retention_passed=True,
            baseline_advantages={"champion": "0.03"},
            selection_correction_applied="holm-v1",
            trials_attempted=1,
            regime_coverage=frozenset({"normal"}),
            evidence_bundle_id=BUNDLE_ID,
        )
        with self.assertRaisesRegex(TypeError, "reproducible"):
            EvaluationEvidence(**{**base, "reproducible": 1})
        with self.assertRaisesRegex(ValueError, "estimated_power"):
            EvaluationEvidence(**{**base, "estimated_power": "1.01"})
        with self.assertRaisesRegex(TypeError, "regime_coverage"):
            EvaluationEvidence(**{**base, "regime_coverage": frozenset({"normal", 7})})

    def test_reviewed_graph_without_semantic_owner_evidence_is_inconclusive(self):
        decision = evaluate_with_verified_bundle(profile(), evidence())
        self.assertEqual(decision.status, "INCONCLUSIVE")
        self.assertEqual(decision.checks["evidence_bundle"], "PASS")
        self.assertEqual(
            decision.checks["semantic_owner_evidence"],
            "INCONCLUSIVE",
        )
        self.assertTrue(decision.provenance["evidence_graph_digest"].startswith("sha256:"))

    def test_locked_evaluation_and_walk_forward_are_required_for_terminal_pass(self):
        no_locked_path = evaluate_with_verified_bundle(
            profile(),
            evidence(
                untouched_holdout_passed=False,
                valid_sequential_evaluation_passed=False,
            ),
        )
        self.assertEqual(no_locked_path.status, "FAIL")
        self.assertEqual(no_locked_path.checks["locked_evaluation"], "FAIL")

        sequential = evaluate_with_verified_bundle(
            profile(),
            evidence(
                untouched_holdout_passed=False,
                valid_sequential_evaluation_passed=True,
            ),
        )
        self.assertEqual(sequential.status, "INCONCLUSIVE")
        self.assertEqual(sequential.checks["locked_evaluation"], "PASS")

        walk_forward = evaluate_with_verified_bundle(
            profile(),
            evidence(walk_forward_passed=False),
        )
        self.assertEqual(walk_forward.status, "FAIL")
        self.assertEqual(walk_forward.checks["walk_forward"], "FAIL")

        missing = evaluate_gates(
            profile(),
            evidence(
                untouched_holdout_passed=None,
                valid_sequential_evaluation_passed=None,
                walk_forward_passed=None,
            ),
        )
        self.assertEqual(missing.status, "INCONCLUSIVE")
        self.assertEqual(missing.checks["locked_evaluation"], "INCONCLUSIVE")
        self.assertEqual(missing.checks["walk_forward"], "INCONCLUSIVE")

    def test_locked_evaluation_artifact_cannot_forge_holdout_or_walk_forward_pass(self):
        gate_profile = profile()
        base_evidence = evidence()

        for forged_field in (
            "untouched_holdout_passed",
            "valid_sequential_evaluation_passed",
            "walk_forward_passed",
        ):
            with self.subTest(forged_field=forged_field), TemporaryDirectory() as directory:
                store = ArtifactStore(directory)

                def forge(kind, payload):
                    if kind != "locked_evaluation":
                        return payload
                    forged = json.loads(payload.decode("utf-8"))
                    forged[forged_field] = not forged[forged_field]
                    return json.dumps(
                        forged,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")

                refs = _publish_bundle(
                    store,
                    gate_profile,
                    base_evidence,
                    payload_mutator=forge,
                )
                decision = evaluate_gates(
                    gate_profile,
                    replace(base_evidence, evidence_refs=refs),
                    artifact_store=store,
                )
                self.assertEqual(decision.status, "FAIL")
                self.assertEqual(decision.checks["evidence_bundle"], "FAIL")

    def test_all_true_metrics_without_resolvable_evidence_cannot_pass(self):
        decision = evaluate_gates(profile(), evidence())
        self.assertEqual(decision.status, "INCONCLUSIVE")
        self.assertEqual(decision.checks["evidence_bundle"], "INCONCLUSIVE")

    def test_incomplete_or_mismatched_evidence_bundle_fails_closed(self):
        gate_profile = profile()
        with TemporaryDirectory() as directory:
            store = ArtifactStore(directory)
            artifact_id = str(uuid4())
            manifest = store.publish_bytes(
                artifact_id=artifact_id,
                data=b"wrong-kind",
                media_type="application/octet-stream",
                rights={"storage": True, "export": False},
                metadata={
                    "evidence_kind": "code",
                    "profile_id": gate_profile.profile_id,
                    "evidence_bundle_id": BUNDLE_ID,
                },
            )
            bound = replace(
                evidence(),
                evidence_refs={
                    "code": GateEvidenceRef(
                        artifact_id=artifact_id,
                        sha256=manifest["sha256"],
                    )
                },
            )
            decision = evaluate_gates(
                gate_profile,
                bound,
                artifact_store=store,
            )
            self.assertEqual(decision.status, "FAIL")
            self.assertEqual(decision.checks["evidence_bundle"], "FAIL")

    def test_verified_bundle_requires_independent_review_artifact(self):
        gate_profile = profile()
        with TemporaryDirectory() as directory:
            store = ArtifactStore(directory)
            base_evidence = evidence()
            refs = _publish_bundle(
                store,
                gate_profile,
                base_evidence,
                kinds=tuple(kind for kind in EVIDENCE_KINDS if kind != "independent_review"),
            )
            decision = evaluate_gates(
                gate_profile,
                replace(base_evidence, evidence_refs=refs),
                artifact_store=store,
            )
            self.assertEqual(decision.status, "FAIL")
            self.assertEqual(decision.checks["evidence_bundle"], "FAIL")

    def test_evidence_digest_and_profile_binding_are_verified(self):
        gate_profile = profile()
        with TemporaryDirectory() as directory:
            store = ArtifactStore(directory)
            base_evidence = evidence()

            def mutate(kind, metadata):
                if kind == "metrics":
                    metadata = dict(metadata)
                    metadata["profile_id"] = "other-profile"
                return metadata

            refs = _publish_bundle(
                store,
                gate_profile,
                base_evidence,
                metadata_mutator=mutate,
            )
            decision = evaluate_gates(
                gate_profile,
                replace(base_evidence, evidence_refs=refs),
                artifact_store=store,
            )
            self.assertEqual(decision.status, "FAIL")
            self.assertEqual(decision.checks["evidence_bundle"], "FAIL")

    def test_metrics_artifact_must_exactly_bind_reported_gate_values(self):
        gate_profile = profile()
        base_evidence = evidence()
        with TemporaryDirectory() as directory:
            store = ArtifactStore(directory)

            def mutate(kind, payload):
                if kind != "metrics":
                    return payload
                decoded = json.loads(payload.decode("utf-8"))
                decoded["net_advantage"] = "0.30"
                return json.dumps(
                    decoded,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")

            refs = _publish_bundle(
                store,
                gate_profile,
                base_evidence,
                payload_mutator=mutate,
            )
            decision = evaluate_gates(
                gate_profile,
                replace(base_evidence, evidence_refs=refs),
                artifact_store=store,
            )
            self.assertEqual(decision.status, "FAIL")
            self.assertEqual(decision.checks["evidence_bundle"], "FAIL")

    def test_lower_bound_cannot_exceed_reported_point_estimate(self):
        decision = evaluate_gates(
            profile(),
            evidence(
                dependence_aware_lower_bound="999",
                net_advantage="0.03",
            ),
        )
        self.assertEqual(decision.status, "FAIL")
        self.assertEqual(decision.checks["uncertainty_consistency"], "FAIL")
        self.assertIn("lower bound exceeds", " ".join(decision.reasons))

    def test_consistent_lower_bound_and_point_estimate_pass_check_but_not_terminal_gate(self):
        decision = evaluate_with_verified_bundle(
            profile(),
            evidence(
                dependence_aware_lower_bound="0.02",
                net_advantage="0.03",
            ),
        )
        self.assertEqual(decision.checks["uncertainty_consistency"], "PASS")
        self.assertEqual(decision.status, "INCONCLUSIVE")

    def test_missing_uncertainty_is_inconclusive_not_pass(self):
        self.assertEqual(
            evaluate_gates(profile(), evidence(dependence_aware_lower_bound=None)).status,
            "INCONCLUSIVE",
        )

    def test_insufficient_power_is_fail(self):
        self.assertEqual(
            evaluate_gates(profile(), evidence(estimated_power="0.50")).status,
            "FAIL",
        )

    def test_hidden_failed_trials_fail(self):
        self.assertEqual(
            evaluate_gates(profile(), evidence(trial_log_complete=False)).status,
            "FAIL",
        )

    def test_adverse_cost_stress_can_fail_good_base_result(self):
        self.assertEqual(
            evaluate_gates(profile(), evidence(adverse_cost_loss="0.10")).status,
            "FAIL",
        )

    def test_negative_adverse_cost_loss_cannot_create_a_false_pass(self):
        with self.assertRaisesRegex(ValueError, "adverse_cost_loss must be non-negative"):
            evidence(adverse_cost_loss="-0.01")

    def test_negative_adverse_cost_limit_is_invalid_protocol(self):
        with self.assertRaisesRegex(ValueError, "max_adverse_cost_loss must be non-negative"):
            GateProfile.create(
                profile_id="bad-gate",
                minimum_net_advantage="0.01",
                max_drawdown="0.10",
                max_adverse_cost_loss="-0.03",
                min_power="0.80",
                primary_baseline_id="champion",
                baseline_ids=("cash", "passive", "champion"),
                selection_correction="holm-v1",
                max_trials=20,
                required_regimes=("normal", "stress"),
            )

    def test_negative_minimum_net_advantage_is_invalid_protocol(self):
        with self.assertRaisesRegex(ValueError, "minimum_net_advantage"):
            GateProfile.create(
                profile_id="negative-edge",
                minimum_net_advantage="-0.01",
                max_drawdown="0.10",
                max_adverse_cost_loss="0.03",
                min_power="0.80",
                primary_baseline_id="champion",
                baseline_ids=("cash", "champion"),
                selection_correction="holm-v1",
                max_trials=20,
                required_regimes=("normal",),
            )

    def test_protocol_identity_collections_reject_non_text_values(self):
        with self.assertRaises(TypeError):
            GateProfile.create(
                profile_id="bad-baseline-type",
                minimum_net_advantage="0.01",
                max_drawdown="0.10",
                max_adverse_cost_loss="0.03",
                min_power="0.80",
                primary_baseline_id="champion",
                baseline_ids=("champion", None),
                selection_correction="holm-v1",
                max_trials=20,
                required_regimes=("normal",),
            )
        with self.assertRaises(TypeError):
            GateProfile.create(
                profile_id="bad-regime-type",
                minimum_net_advantage="0.01",
                max_drawdown="0.10",
                max_adverse_cost_loss="0.03",
                min_power="0.80",
                primary_baseline_id="champion",
                baseline_ids=("champion",),
                selection_correction="holm-v1",
                max_trials=20,
                required_regimes=("normal", None),
            )

    def test_profile_changed_after_result_fails(self):
        self.assertEqual(
            evaluate_gates(
                profile(), evidence(profile_unchanged_after_results=False)
            ).status,
            "FAIL",
        )

    def test_retention_regression_fails(self):
        self.assertEqual(
            evaluate_gates(profile(), evidence(retention_passed=False)).status,
            "FAIL",
        )

    def test_missing_causal_audit_is_inconclusive(self):
        self.assertEqual(
            evaluate_gates(profile(), evidence(causal_audit_passed=None)).status,
            "INCONCLUSIVE",
        )

    def test_selection_correction_mismatch_fails(self):
        decision = evaluate_gates(
            profile(),
            evidence(selection_correction_applied="none"),
        )
        self.assertEqual(decision.status, "FAIL")
        self.assertEqual(decision.checks["selection_correction"], "FAIL")

    def test_trial_budget_exhaustion_fails_and_missing_count_is_inconclusive(self):
        self.assertEqual(
            evaluate_gates(profile(), evidence(trials_attempted=21)).status,
            "FAIL",
        )
        self.assertEqual(
            evaluate_gates(profile(), evidence(trials_attempted=None)).status,
            "INCONCLUSIVE",
        )

    def test_missing_registered_regime_fails(self):
        decision = evaluate_gates(
            profile(),
            evidence(regime_coverage=frozenset({"normal"})),
        )
        self.assertEqual(decision.status, "FAIL")
        self.assertEqual(decision.checks["regime_coverage"], "FAIL")

    def test_baseline_set_must_match_registration(self):
        decision = evaluate_gates(
            profile(),
            evidence(
                baseline_advantages={
                    "cash": "0.04",
                    "champion": "0.03",
                }
            ),
        )
        self.assertEqual(decision.status, "FAIL")
        self.assertEqual(decision.checks["baselines"], "FAIL")

    def test_primary_baseline_advantage_is_bound_to_net_advantage(self):
        decision = evaluate_gates(
            profile(),
            evidence(
                baseline_advantages={
                    "cash": "0.04",
                    "passive": "0.035",
                    "champion": "0.031",
                }
            ),
        )
        self.assertEqual(decision.status, "FAIL")
        self.assertEqual(decision.checks["primary_baseline"], "FAIL")

    def test_gate_decision_checks_are_immutable_after_evaluation(self):
        decision = evaluate_with_verified_bundle(profile(), evidence())
        self.assertEqual(decision.status, "INCONCLUSIVE")
        with self.assertRaises(TypeError):
            decision.checks["net_advantage"] = "FAIL"
        self.assertEqual(decision.checks["net_advantage"], "PASS")
        with self.assertRaises(TypeError):
            decision.provenance["evidence_graph_digest"] = "forged"

    def test_gate_decision_defensively_copies_mutable_checks(self):
        checks = {"net_advantage": "PASS"}
        decision = GateDecision(
            status="PASS",
            reasons=("registered gate passed",),
            checks=checks,
        )
        checks["net_advantage"] = "FAIL"
        self.assertEqual(decision.checks["net_advantage"], "PASS")

    def test_forged_gate_decision_statuses_fail_closed(self):
        with self.assertRaisesRegex(ValueError, "status"):
            GateDecision(
                status="APPROVED",
                reasons=("forged",),
                checks={"net_advantage": "PASS"},
            )
        with self.assertRaisesRegex(ValueError, "check status"):
            GateDecision(
                status="PASS",
                reasons=("forged",),
                checks={"net_advantage": "APPROVED"},
            )

    def test_empty_selection_controls_are_invalid_protocol(self):
        with self.assertRaises(ValueError):
            GateProfile.create(
                profile_id="bad",
                minimum_net_advantage="0.01",
                max_drawdown="0.1",
                max_adverse_cost_loss="0.03",
                min_power="0.8",
                primary_baseline_id="champion",
                baseline_ids=(),
                selection_correction="holm-v1",
                max_trials=10,
                required_regimes=("normal",),
            )
        with self.assertRaises(ValueError):
            GateProfile.create(
                profile_id="bad",
                minimum_net_advantage="0.01",
                max_drawdown="0.1",
                max_adverse_cost_loss="0.03",
                min_power="0.8",
                primary_baseline_id="champion",
                baseline_ids=("champion",),
                selection_correction="",
                max_trials=10,
                required_regimes=("normal",),
            )

    def test_multi_trial_protocol_cannot_register_noop_multiplicity_treatment(self):
        for correction in ("none", "UNADJUSTED", "disabled", "n/a"):
            with self.subTest(correction=correction):
                with self.assertRaisesRegex(ValueError, "multiplicity treatment"):
                    GateProfile.create(
                        profile_id="bad-multiplicity",
                        minimum_net_advantage="0.01",
                        max_drawdown="0.10",
                        max_adverse_cost_loss="0.03",
                        min_power="0.80",
                        primary_baseline_id="champion",
                        baseline_ids=("champion",),
                        selection_correction=correction,
                        max_trials=2,
                        required_regimes=("normal",),
                    )

        with self.assertRaisesRegex(ValueError, "multiplicity treatment"):
            GateProfile(
                profile_id="direct-bad-multiplicity",
                minimum_net_advantage="0.01",
                max_drawdown="0.10",
                max_adverse_cost_loss="0.03",
                min_power="0.80",
                primary_baseline_id="champion",
                baseline_ids=("champion",),
                selection_correction="none",
                max_trials=2,
                required_regimes=("normal",),
                require_complete_trials=True,
                require_causal_audit=True,
                require_financial_invariants=True,
            )

    def test_single_trial_protocol_may_explicitly_register_no_adjustment(self):
        single = GateProfile.create(
            profile_id="single-trial",
            minimum_net_advantage="0.01",
            max_drawdown="0.10",
            max_adverse_cost_loss="0.03",
            min_power="0.80",
            primary_baseline_id="champion",
            baseline_ids=("champion",),
            selection_correction="none",
            max_trials=1,
            required_regimes=("normal",),
        )
        self.assertEqual(single.selection_correction, "none")

    def test_independent_review_cannot_be_synthesized_by_gate_report_payload(self):
        with self.assertRaisesRegex(ValueError, "scientific report evidence kind"):
            gate_report_payload("independent_review", profile(), evidence())

    def test_self_published_favorable_bundle_without_independent_trust_is_inconclusive(self):
        gate_profile = profile()
        base_evidence = evidence()
        with TemporaryDirectory() as directory:
            store = ArtifactStore(directory)
            refs = _publish_bundle(store, gate_profile, base_evidence)
            decision = evaluate_gates(
                gate_profile,
                replace(base_evidence, evidence_refs=refs),
                artifact_store=store,
            )
            self.assertEqual(decision.status, "INCONCLUSIVE")
            self.assertEqual(decision.checks["evidence_bundle"], "INCONCLUSIVE")

    def test_invalid_supplied_independent_review_fails(self):
        gate_profile = profile()
        base_evidence = evidence()
        with TemporaryDirectory() as directory:
            store = ArtifactStore(directory)
            refs = _publish_bundle(store, gate_profile, base_evidence)
            decision = evaluate_gates(
                gate_profile,
                replace(base_evidence, evidence_refs=refs),
                artifact_store=store,
                evidence_root=directory,
                expected_source_sha=SOURCE_SHA,
            )
            self.assertEqual(decision.status, "FAIL")
            self.assertEqual(decision.checks["evidence_bundle"], "FAIL")

    def test_same_bundle_uuid_foreign_graph_cannot_reuse_graph_a_review(self):
        gate_profile = profile()
        base_evidence = evidence()
        with TemporaryDirectory() as directory:
            store = ArtifactStore(directory)
            refs_a = _publish_bundle(store, gate_profile, base_evidence)

            foreign_id = str(uuid4())
            foreign_manifest = store.publish_bytes(
                artifact_id=foreign_id,
                data=b"foreign-run-model",
                media_type="application/octet-stream",
                rights={"storage": True, "export": False},
                source_refs=[f"git:{SOURCE_SHA}"],
                metadata={
                    "evidence_kind": "model",
                    "profile_id": gate_profile.profile_id,
                    "evidence_bundle_id": BUNDLE_ID,
                },
            )
            refs_b = dict(refs_a)
            refs_b["model"] = GateEvidenceRef(
                artifact_id=foreign_id,
                sha256=foreign_manifest["sha256"],
            )

            approved_requirement = []
            observed_requirements = []

            def authorize_graph_a(*args, **kwargs):
                requirement_id = kwargs["requirement_id"]
                observed_requirements.append(requirement_id)
                if not approved_requirement:
                    approved_requirement.append(requirement_id)
                    return _accepted_review(*args, **kwargs)
                return (
                    _accepted_review(*args, **kwargs)
                    if requirement_id == approved_requirement[0]
                    else (False, {})
                )

            with patch.object(
                gate_module,
                "_verify_independent_review",
                side_effect=authorize_graph_a,
            ):
                decision_a = evaluate_gates(
                    gate_profile,
                    replace(base_evidence, evidence_refs=refs_a),
                    artifact_store=store,
                    evidence_root=directory,
                    expected_source_sha=SOURCE_SHA,
                )
                decision_b = evaluate_gates(
                    gate_profile,
                    replace(base_evidence, evidence_refs=refs_b),
                    artifact_store=store,
                    evidence_root=directory,
                    expected_source_sha=SOURCE_SHA,
                )

            self.assertEqual(decision_a.status, "INCONCLUSIVE")
            self.assertEqual(decision_b.status, "FAIL")
            self.assertEqual(len(observed_requirements), 2)
            self.assertNotEqual(observed_requirements[0], observed_requirements[1])
            self.assertEqual(dict(decision_b.provenance), {})

    def test_terminal_bundle_consumer_does_not_use_split_manifest_payload_reads(self):
        gate_profile = profile()
        base_evidence = evidence()
        with TemporaryDirectory() as directory:
            store = ArtifactStore(directory)
            refs = _publish_bundle(store, gate_profile, base_evidence)

            def forbidden(*args, **kwargs):
                raise AssertionError("split artifact read API must not be used")

            store.load_manifest = forbidden
            store.read_bytes = forbidden
            with patch.object(
                gate_module,
                "_verify_independent_review",
                side_effect=_accepted_review,
            ):
                decision = evaluate_gates(
                    gate_profile,
                    replace(base_evidence, evidence_refs=refs),
                    artifact_store=store,
                    evidence_root=directory,
                    expected_source_sha=SOURCE_SHA,
                )
            self.assertEqual(decision.status, "INCONCLUSIVE")

    def test_hostile_decimal_subclass_is_rejected_before_gate_dispatch(self):
        class HostileDecimal(Decimal):
            def is_finite(self):
                return True

        with self.assertRaisesRegex(TypeError, "Decimal, string or integer"):
            GateProfile.create(
                profile_id="hostile-decimal",
                minimum_net_advantage=HostileDecimal("0.01"),
                max_drawdown="0.10",
                max_adverse_cost_loss="0.03",
                min_power="0.80",
                primary_baseline_id="champion",
                baseline_ids=("champion",),
                selection_correction="holm-v1",
                max_trials=2,
                required_regimes=("normal",),
            )

    def test_report_decimal_bytes_are_context_invariant_and_exact_values_do_not_collide(self):
        gate_profile = profile()
        first = evidence(
            dependence_aware_lower_bound="0.02000000000000000001",
            net_advantage="0.03000000000000000001",
            baseline_advantages={
                "cash": "0.04000000000000000001",
                "passive": "0.03500000000000000001",
                "champion": "0.03000000000000000001",
            },
        )
        second = evidence(
            dependence_aware_lower_bound="0.02000000000000000002",
            net_advantage="0.03000000000000000002",
            baseline_advantages={
                "cash": "0.04000000000000000002",
                "passive": "0.03500000000000000002",
                "champion": "0.03000000000000000002",
            },
        )

        with localcontext() as context:
            context.prec = 3
            context.rounding = ROUND_UP
            first_low = gate_report_payload("metrics", gate_profile, first)
            second_low = gate_report_payload("metrics", gate_profile, second)
        with localcontext() as context:
            context.prec = 50
            context.rounding = ROUND_DOWN
            first_high = gate_report_payload("metrics", gate_profile, first)
            second_high = gate_report_payload("metrics", gate_profile, second)

        self.assertEqual(first_low, first_high)
        self.assertEqual(second_low, second_high)
        self.assertNotEqual(first_low, second_low)

    def test_evidence_bundle_id_requires_canonical_uuid(self):
        with self.assertRaisesRegex(ValueError, "evidence_bundle_id"):
            evidence(evidence_bundle_id="not-a-uuid")


if __name__ == "__main__":
    unittest.main()


class FrozenScientificQualificationTests(unittest.TestCase):
    def test_missing_frozen_time_remains_inconclusive(self):
        decision = evaluate_with_verified_bundle(profile(), evidence())
        self.assertEqual(decision.checks["frozen_qualification_cut"], "INCONCLUSIVE")
        self.assertEqual(decision.status, "INCONCLUSIVE")

    def test_future_artifact_cannot_enter_earlier_qualification(self):
        p, e = profile(), evidence()
        with TemporaryDirectory() as directory:
            store = ArtifactStore(directory)
            refs = _publish_bundle(store, p, e)
            with patch.object(gate_module, "_verify_independent_review", side_effect=AssertionError("future evidence reached review")):
                decision = evaluate_gates(p, replace(e, evidence_refs=refs), artifact_store=store,
                    evidence_root=directory, expected_source_sha=SOURCE_SHA, qualification_at="2000-01-01T00:00:00Z")
            self.assertEqual(decision.checks["evidence_bundle"], "FAIL")
            self.assertEqual(decision.status, "FAIL")
            self.assertEqual(dict(decision.provenance), {})

    def test_review_of_one_frozen_cut_cannot_authorize_another(self):
        p, e = profile(), evidence()
        approved = []
        def review(*args, **kwargs):
            identity = kwargs["requirement_id"]
            if not approved:
                approved.append(identity)
            return _accepted_review(*args, **kwargs) if identity == approved[0] else (False, {})
        with TemporaryDirectory() as directory:
            store = ArtifactStore(directory)
            refs = _publish_bundle(store, p, e)
            with patch.object(gate_module, "_verify_independent_review", side_effect=review):
                common = dict(artifact_store=store, evidence_root=directory, expected_source_sha=SOURCE_SHA)
                first = evaluate_gates(p, replace(e, evidence_refs=refs), qualification_at="2100-01-01T00:00:00Z", **common)
                second = evaluate_gates(p, replace(e, evidence_refs=refs), qualification_at="2100-01-01T00:00:01Z", **common)
        self.assertEqual(first.checks["evidence_bundle"], "PASS")
        self.assertEqual(first.checks["semantic_owner_evidence"], "INCONCLUSIVE")
        self.assertEqual(first.status, "INCONCLUSIVE")
        self.assertEqual(first.provenance["qualification_at"], "2100-01-01T00:00:00Z")
        self.assertEqual(second.status, "FAIL")

    def test_later_signed_review_is_rejected_before_trust_dispatch(self):
        from mvp.tests.test_qualification_attestation import root, attestation, sign
        from mvp.autotrade_mvp.qualification_attestation import SignedQualificationAttestation
        a = attestation(root())
        receipt = SignedQualificationAttestation(attestation=a, signature_b64=sign(a))
        payload = json.dumps({"attestation": a.canonical_payload(), "signature_b64": receipt.signature_b64}).encode()
        with TemporaryDirectory() as directory:
            store = ArtifactStore(directory)
            with patch.object(gate_module, "verify_canonical_qualification_attestation", side_effect=AssertionError("future review reached trust")):
                accepted, provenance = gate_module._verify_independent_review(payload, artifact_store=store,
                    evidence_root=directory, expected_source_sha=SOURCE_SHA, evidence_bundle_id=BUNDLE_ID,
                    requirement_id="test-frozen-cut", qualification_at="2026-09-25T02:10:30Z")
        self.assertFalse(accepted)
        self.assertEqual(dict(provenance), {})

    def test_mutated_and_hidden_frozen_input_are_rejected(self):
        p, e = profile(), evidence()
        object.__setattr__(e, "trials_attempted", True)
        with self.assertRaisesRegex(ValueError, "non-negative integer"):
            evaluate_gates(p, e)
        e = evidence()
        object.__setattr__(e, "future_owner_override", True)
        with self.assertRaisesRegex(TypeError, "unexpected state"):
            evaluate_gates(p, e)
        class EvidenceSubclass(EvaluationEvidence):
            pass
        hostile = EvidenceSubclass(**evidence().__dict__)
        with self.assertRaisesRegex(TypeError, "exact EvaluationEvidence"):
            evaluate_gates(p, hostile)

    def test_timezone_and_scalar_cut_types_are_exact(self):
        for cutoff in (True, "2026-10-03T00:00:00", "nonsense", " 2026-10-03T00:00:00Z"):
            with self.subTest(cutoff=cutoff), self.assertRaises(ValueError):
                evaluate_gates(profile(), evidence(), qualification_at=cutoff)


class NestedFrozenReviewEvidenceTests(unittest.TestCase):
    def test_backdated_review_cannot_qualify_later_published_nested_evidence(self):
        from mvp.tests.test_qualification_attestation import root, attestation, sign, publish
        from mvp.autotrade_mvp.qualification_attestation import SignedQualificationAttestation
        a = attestation(root())
        receipt = SignedQualificationAttestation(attestation=a, signature_b64=sign(a))
        payload = json.dumps({"attestation": a.canonical_payload(), "signature_b64": receipt.signature_b64}).encode()
        with TemporaryDirectory() as directory:
            store = ArtifactStore(directory)
            publish(store)
            with patch.object(gate_module, "verify_canonical_qualification_attestation", side_effect=AssertionError("future nested evidence reached trust")):
                accepted, provenance = gate_module._verify_independent_review(payload, artifact_store=store,
                    evidence_root=directory, expected_source_sha=SOURCE_SHA, evidence_bundle_id=BUNDLE_ID,
                    requirement_id="test-frozen-nested-cut", qualification_at="2026-09-30T00:00:00Z")
        self.assertFalse(accepted)
        self.assertEqual(dict(provenance), {})
