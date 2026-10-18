import unittest

from research.autotrade_research.evaluation.gates import GateDecision
from research.autotrade_research.evaluation.layers import (
    BLINDED_MARKET_REPLAY,
    CAUSAL_INFORMATION_REPLAY,
    FORWARD_PAPER,
    EvaluationLayerReceipt,
    EvaluationLayersError,
    ThreeLayerEvaluation,
    compose_three_layer_evaluation,
)
from research.autotrade_research.forward_paper import (
    ForwardOutcome,
    ForwardPaperAssessment,
    ForwardPaperEvidence,
    ForwardPaperProtocol,
    OperationalObservation,
    SealedPrediction,
    assess_forward_paper,
    forward_paper_protocol_hash,
)


BUILD = "a" * 40
SELECTED = "2026-09-30T12:00:00Z"


def h(char):
    return "sha256:" + (char * 64)


def gate(status, layer_tag):
    return GateDecision(
        status=status,
        reasons=(f"{layer_tag}:{status.lower()}",),
        checks={f"{layer_tag}_core": status},
        provenance={"layer": layer_tag},
    )


def historical(layer, status="PASS", evidence_char="1", protocol_char="2"):
    tag = "market" if layer == BLINDED_MARKET_REPLAY else "information"
    return EvaluationLayerReceipt.from_historical_gate(
        layer=layer,
        candidate_id="candidate-1",
        exact_build_sha=BUILD,
        candidate_selected_at=SELECTED,
        protocol_sha256=h(protocol_char),
        evidence_sha256=h(evidence_char),
        decision=gate(status, tag),
        model_training_cutoff_uncertainty="training cutoff not independently known",
    )


def forward_fixture(*, minimum_predictions=1, missed_deadline=False):
    protocol_hash = forward_paper_protocol_hash(
        campaign_id="campaign-1",
        exact_build_sha=BUILD,
        registered_at="2026-10-01T00:00:00Z",
        starts_at="2026-10-02T00:00:00Z",
        ends_at="2026-10-03T00:00:00Z",
        minimum_predictions=minimum_predictions,
        maximum_decision_latency_ms=100,
        required_provider_capabilities=("MARKET_DATA",),
        required_operational_cases=("NETWORK_RECOVERY",),
    )
    protocol = ForwardPaperProtocol.create(
        campaign_id="campaign-1",
        exact_build_sha=BUILD,
        protocol_hash=protocol_hash,
        registered_at="2026-10-01T00:00:00Z",
        starts_at="2026-10-02T00:00:00Z",
        ends_at="2026-10-03T00:00:00Z",
        minimum_predictions=minimum_predictions,
        maximum_decision_latency_ms=100,
        required_provider_capabilities=("MARKET_DATA",),
        required_operational_cases=("NETWORK_RECOVERY",),
    )
    prediction = SealedPrediction.create(
        prediction_id="prediction-1",
        provider_capability="MARKET_DATA",
        input_hash=h("3"),
        proposal_hash=h("4"),
        information_cutoff_at="2026-10-02T11:59:00Z",
        sealed_at="2026-10-02T12:00:00Z",
        decision_deadline_at=(
            "2026-10-02T11:59:59Z"
            if missed_deadline
            else "2026-10-02T12:01:00Z"
        ),
        outcome_horizon_end_at="2026-10-02T13:00:00Z",
        decision_latency_ms=50,
    )
    outcome = ForwardOutcome.create(
        prediction_id="prediction-1",
        outcome_hash=h("5"),
        outcome_available_at="2026-10-02T13:00:00Z",
        evaluated_at="2026-10-02T13:01:00Z",
    )
    observation = OperationalObservation.create(
        provider_capability="MARKET_DATA",
        case="NETWORK_RECOVERY",
        observed_at="2026-10-02T14:00:00Z",
        reconciled=True,
    )
    evidence = ForwardPaperEvidence.create(
        exact_build_sha=BUILD,
        protocol_hash=protocol_hash,
        observed_until="2026-10-03T00:00:00Z",
        predictions=(prediction,),
        outcomes=(outcome,),
        operational_observations=(observation,),
        costs_by_currency={"USD": "1.00"},
        costs_complete=True,
        account_reconciliation_complete=True,
    )
    assessment = assess_forward_paper(protocol, evidence)
    return protocol, evidence, assessment


def forward_receipt(*, minimum_predictions=1, missed_deadline=False):
    protocol, evidence, assessment = forward_fixture(
        minimum_predictions=minimum_predictions,
        missed_deadline=missed_deadline,
    )
    return EvaluationLayerReceipt.from_forward_paper(
        candidate_id="candidate-1",
        candidate_selected_at=SELECTED,
        protocol=protocol,
        evidence=evidence,
        assessment=assessment,
    )


def complete_layers(a_status="PASS", b_status="PASS"):
    return (
        historical(
            BLINDED_MARKET_REPLAY,
            a_status,
            evidence_char="1",
            protocol_char="2",
        ),
        historical(
            CAUSAL_INFORMATION_REPLAY,
            b_status,
            evidence_char="6",
            protocol_char="7",
        ),
        forward_receipt(),
    )


class ThreeEvaluationLayerTests(unittest.TestCase):
    def compose(self, layers=None):
        return compose_three_layer_evaluation(
            candidate_id="candidate-1",
            exact_build_sha=BUILD,
            candidate_selected_at=SELECTED,
            layers=complete_layers() if layers is None else layers,
        )

    def test_three_distinct_layers_can_compose(self):
        result = self.compose()
        self.assertEqual(
            tuple(item.layer for item in result.layers),
            (
                BLINDED_MARKET_REPLAY,
                CAUSAL_INFORMATION_REPLAY,
                FORWARD_PAPER,
            ),
        )
        self.assertEqual(result.evaluation_status, "PASS")
        self.assertTrue(result.all_three_layers_passed)

    def test_all_three_pass_still_does_not_establish_economic_edge(self):
        result = self.compose()
        self.assertEqual(result.economic_edge_status, "NOT_ESTABLISHED")
        self.assertTrue(result.forward_required_for_complete_evaluation)

    def test_historical_pass_cannot_substitute_for_forward_layer(self):
        a, b, _ = complete_layers()
        with self.assertRaisesRegex(
            EvaluationLayersError,
            "exactly three receipts",
        ):
            self.compose((a, b))

    def test_forward_pass_cannot_substitute_for_missing_information_layer(self):
        a, _, c = complete_layers()
        with self.assertRaisesRegex(
            EvaluationLayersError,
            "exactly three receipts",
        ):
            self.compose((a, c))

    def test_duplicate_layer_is_rejected(self):
        a, _, c = complete_layers()
        duplicate_a = historical(
            BLINDED_MARKET_REPLAY,
            evidence_char="8",
            protocol_char="9",
        )
        with self.assertRaisesRegex(EvaluationLayersError, "duplicate evaluation layer"):
            self.compose((a, duplicate_a, c))

    def test_one_evidence_bundle_cannot_authorize_two_layers(self):
        a, b, c = complete_layers()
        object.__setattr__(b, "evidence_sha256", a.evidence_sha256)
        with self.assertRaisesRegex(
            EvaluationLayersError,
            "evidence bundle cannot substitute",
        ):
            self.compose((a, b, c))

    def test_one_protocol_identity_cannot_authorize_two_layers(self):
        a, b, c = complete_layers()
        object.__setattr__(b, "protocol_sha256", a.protocol_sha256)
        with self.assertRaisesRegex(
            EvaluationLayersError,
            "independent protocol identity",
        ):
            self.compose((a, b, c))

    def test_one_authority_result_cannot_substitute_for_two_layers(self):
        a, b, c = complete_layers()
        object.__setattr__(b, "authority_result_sha256", a.authority_result_sha256)
        with self.assertRaisesRegex(
            EvaluationLayersError,
            "authority result cannot substitute",
        ):
            self.compose((a, b, c))

    def test_candidate_identity_must_match_all_layers(self):
        a, b, c = complete_layers()
        object.__setattr__(b, "candidate_id", "other")
        with self.assertRaisesRegex(EvaluationLayersError, "same candidate_id"):
            self.compose((a, b, c))

    def test_build_identity_must_match_all_layers(self):
        a, b, c = complete_layers()
        object.__setattr__(b, "exact_build_sha", "b" * 40)
        with self.assertRaisesRegex(EvaluationLayersError, "same exact build"):
            self.compose((a, b, c))

    def test_candidate_selection_cut_must_match_all_layers(self):
        a, b, c = complete_layers()
        object.__setattr__(
            b,
            "candidate_selected_at",
            "2026-09-30T12:00:01Z",
        )
        with self.assertRaisesRegex(
            EvaluationLayersError,
            "same candidate selection cut",
        ):
            self.compose((a, b, c))

    def test_mutated_information_mode_fails_closed_on_composition(self):
        a, b, c = complete_layers()
        object.__setattr__(b, "information_mode", "MARKET_ONLY")
        with self.assertRaisesRegex(EvaluationLayersError, "information_mode"):
            self.compose((a, b, c))

    def test_mutated_historical_forward_flag_fails_closed(self):
        a, b, c = complete_layers()
        object.__setattr__(a, "forward_events_unavailable_at_selection", True)
        with self.assertRaisesRegex(EvaluationLayersError, "forward-event"):
            self.compose((a, b, c))

    def test_historical_cutoff_uncertainty_is_mandatory(self):
        with self.assertRaisesRegex(
            EvaluationLayersError,
            "model_training_cutoff_uncertainty",
        ):
            EvaluationLayerReceipt.from_historical_gate(
                layer=BLINDED_MARKET_REPLAY,
                candidate_id="candidate-1",
                exact_build_sha=BUILD,
                candidate_selected_at=SELECTED,
                protocol_sha256=h("2"),
                evidence_sha256=h("1"),
                decision=gate("PASS", "market"),
                model_training_cutoff_uncertainty="",
            )

    def test_historical_gate_cannot_populate_forward_layer(self):
        with self.assertRaisesRegex(
            EvaluationLayersError,
            "only populate layer A or B",
        ):
            EvaluationLayerReceipt.from_historical_gate(
                layer=FORWARD_PAPER,
                candidate_id="candidate-1",
                exact_build_sha=BUILD,
                candidate_selected_at=SELECTED,
                protocol_sha256=h("2"),
                evidence_sha256=h("1"),
                decision=gate("PASS", "forward"),
                model_training_cutoff_uncertainty="unknown",
            )

    def test_inconsistent_gate_status_is_rejected(self):
        bad = GateDecision(
            status="PASS",
            reasons=("caller claimed pass",),
            checks={"causality": "INCONCLUSIVE"},
            provenance={"source": "test"},
        )
        with self.assertRaisesRegex(
            EvaluationLayersError,
            "contradicts its own check statuses",
        ):
            EvaluationLayerReceipt.from_historical_gate(
                layer=BLINDED_MARKET_REPLAY,
                candidate_id="candidate-1",
                exact_build_sha=BUILD,
                candidate_selected_at=SELECTED,
                protocol_sha256=h("2"),
                evidence_sha256=h("1"),
                decision=bad,
                model_training_cutoff_uncertainty="unknown",
            )

    def test_gate_decision_subclass_is_not_accepted_as_authority(self):
        class DerivedGateDecision(GateDecision):
            pass

        derived = DerivedGateDecision(
            status="PASS",
            reasons=("pass",),
            checks={"x": "PASS"},
        )
        with self.assertRaisesRegex(TypeError, "exact GateDecision"):
            EvaluationLayerReceipt.from_historical_gate(
                layer=BLINDED_MARKET_REPLAY,
                candidate_id="candidate-1",
                exact_build_sha=BUILD,
                candidate_selected_at=SELECTED,
                protocol_sha256=h("2"),
                evidence_sha256=h("1"),
                decision=derived,
                model_training_cutoff_uncertainty="unknown",
            )

    def test_forward_receipt_recomputes_canonical_assessment(self):
        protocol, evidence, assessment = forward_fixture()
        receipt = EvaluationLayerReceipt.from_forward_paper(
            candidate_id="candidate-1",
            candidate_selected_at=SELECTED,
            protocol=protocol,
            evidence=evidence,
            assessment=assessment,
        )
        self.assertEqual(receipt.status, "PASS")
        self.assertTrue(receipt.forward_events_unavailable_at_selection)
        self.assertEqual(receipt.economic_edge_status, "NOT_ESTABLISHED")

    def test_forward_receipt_rejects_self_authored_mismatched_assessment(self):
        protocol, evidence, _ = forward_fixture()
        forged = ForwardPaperAssessment(
            evidence_status="VALID",
            operational_status="INCONCLUSIVE",
            economic_edge_status="NOT_ESTABLISHED",
            reasons=("forged",),
            prediction_count=1,
            evaluated_outcome_count=1,
        )
        with self.assertRaisesRegex(
            EvaluationLayersError,
            "canonical recomputation",
        ):
            EvaluationLayerReceipt.from_forward_paper(
                candidate_id="candidate-1",
                candidate_selected_at=SELECTED,
                protocol=protocol,
                evidence=evidence,
                assessment=forged,
            )

    def test_forward_candidate_must_preexist_protocol_registration(self):
        protocol, evidence, assessment = forward_fixture()
        with self.assertRaisesRegex(
            EvaluationLayersError,
            "selected before the forward protocol",
        ):
            EvaluationLayerReceipt.from_forward_paper(
                candidate_id="candidate-1",
                candidate_selected_at="2026-10-01T00:00:01Z",
                protocol=protocol,
                evidence=evidence,
                assessment=assessment,
            )

    def test_forward_operational_failure_maps_to_layer_fail(self):
        receipt = forward_receipt(missed_deadline=True)
        self.assertEqual(receipt.status, "FAIL")

    def test_incomplete_forward_campaign_maps_to_inconclusive(self):
        receipt = forward_receipt(minimum_predictions=2)
        self.assertEqual(receipt.status, "INCONCLUSIVE")

    def test_historical_fail_makes_composite_fail(self):
        result = self.compose(complete_layers(a_status="FAIL"))
        self.assertEqual(result.historical_status, "FAIL")
        self.assertEqual(result.evaluation_status, "FAIL")

    def test_historical_inconclusive_makes_composite_inconclusive(self):
        result = self.compose(complete_layers(b_status="INCONCLUSIVE"))
        self.assertEqual(result.historical_status, "INCONCLUSIVE")
        self.assertEqual(result.evaluation_status, "INCONCLUSIVE")
        self.assertEqual(result.forward_status, "PASS")

    def test_forward_inconclusive_makes_composite_inconclusive(self):
        a = historical(
            BLINDED_MARKET_REPLAY,
            evidence_char="1",
            protocol_char="2",
        )
        b = historical(
            CAUSAL_INFORMATION_REPLAY,
            evidence_char="6",
            protocol_char="7",
        )
        c = forward_receipt(minimum_predictions=2)
        result = self.compose((a, b, c))
        self.assertEqual(result.historical_status, "PASS")
        self.assertEqual(result.forward_status, "INCONCLUSIVE")
        self.assertEqual(result.evaluation_status, "INCONCLUSIVE")

    def test_input_order_does_not_change_canonical_digest(self):
        a, b, c = complete_layers()
        first = self.compose((a, b, c))
        second = self.compose((c, a, b))
        self.assertEqual(first.layers, second.layers)
        self.assertEqual(first.digest, second.digest)

    def test_evidence_identity_change_changes_composite_digest(self):
        first = self.compose()
        a, b, c = complete_layers()
        object.__setattr__(b, "evidence_sha256", h("8"))
        second = self.compose((a, b, c))
        self.assertNotEqual(first.digest, second.digest)

    def test_layer_status_mapping_is_read_only(self):
        result = self.compose()
        statuses = result.layer_statuses
        with self.assertRaises(TypeError):
            statuses[FORWARD_PAPER] = "FAIL"

    def test_exact_tuple_is_required_by_authority_dataclass(self):
        layers = list(complete_layers())
        with self.assertRaisesRegex(TypeError, "exact tuple"):
            ThreeLayerEvaluation(
                candidate_id="candidate-1",
                exact_build_sha=BUILD,
                candidate_selected_at=SELECTED,
                layers=layers,
            )

    def test_forward_protocol_subclass_is_rejected(self):
        class DerivedProtocol(ForwardPaperProtocol):
            pass

        protocol, evidence, assessment = forward_fixture()
        derived = DerivedProtocol(
            campaign_id=protocol.campaign_id,
            exact_build_sha=protocol.exact_build_sha,
            protocol_hash=protocol.protocol_hash,
            registered_at=protocol.registered_at,
            starts_at=protocol.starts_at,
            ends_at=protocol.ends_at,
            minimum_predictions=protocol.minimum_predictions,
            maximum_decision_latency_ms=protocol.maximum_decision_latency_ms,
            required_provider_capabilities=protocol.required_provider_capabilities,
            required_operational_cases=protocol.required_operational_cases,
        )
        with self.assertRaisesRegex(TypeError, "exact ForwardPaperProtocol"):
            EvaluationLayerReceipt.from_forward_paper(
                candidate_id="candidate-1",
                candidate_selected_at=SELECTED,
                protocol=derived,
                evidence=evidence,
                assessment=assessment,
            )

    def test_mutated_forward_protocol_is_revalidated_before_use(self):
        protocol, evidence, assessment = forward_fixture()
        object.__setattr__(protocol, "exact_build_sha", "b" * 40)
        with self.assertRaises(ValueError):
            EvaluationLayerReceipt.from_forward_paper(
                candidate_id="candidate-1",
                candidate_selected_at=SELECTED,
                protocol=protocol,
                evidence=evidence,
                assessment=assessment,
            )

    def test_mutated_nested_prediction_is_revalidated_before_use(self):
        protocol, evidence, assessment = forward_fixture()
        object.__setattr__(evidence.predictions[0], "input_hash", "not-a-hash")
        with self.assertRaises(ValueError):
            EvaluationLayerReceipt.from_forward_paper(
                candidate_id="candidate-1",
                candidate_selected_at=SELECTED,
                protocol=protocol,
                evidence=evidence,
                assessment=assessment,
            )

    def test_forward_prediction_subclass_is_rejected_before_assessment(self):
        class DerivedPrediction(SealedPrediction):
            pass

        protocol, evidence, assessment = forward_fixture()
        original = evidence.predictions[0]
        derived = DerivedPrediction(
            prediction_id=original.prediction_id,
            provider_capability=original.provider_capability,
            input_hash=original.input_hash,
            proposal_hash=original.proposal_hash,
            information_cutoff_at=original.information_cutoff_at,
            sealed_at=original.sealed_at,
            decision_deadline_at=original.decision_deadline_at,
            outcome_horizon_end_at=original.outcome_horizon_end_at,
            decision_latency_ms=original.decision_latency_ms,
        )
        object.__setattr__(evidence, "predictions", (derived,))
        with self.assertRaisesRegex(TypeError, "exact SealedPrediction"):
            EvaluationLayerReceipt.from_forward_paper(
                candidate_id="candidate-1",
                candidate_selected_at=SELECTED,
                protocol=protocol,
                evidence=evidence,
                assessment=assessment,
            )

    def test_hostile_layer_text_is_rejected_before_membership_dispatch(self):
        callbacks = []

        class HostileLayer(str):
            def __hash__(self):
                callbacks.append("hash")
                return super().__hash__()

            def __eq__(self, other):
                callbacks.append("eq")
                return super().__eq__(other)

        with self.assertRaisesRegex(
            EvaluationLayersError,
            "layer must be non-empty exact text",
        ):
            EvaluationLayerReceipt.from_historical_gate(
                layer=HostileLayer(BLINDED_MARKET_REPLAY),
                candidate_id="candidate-1",
                exact_build_sha=BUILD,
                candidate_selected_at=SELECTED,
                protocol_sha256=h("2"),
                evidence_sha256=h("1"),
                decision=gate("PASS", "market"),
                model_training_cutoff_uncertainty="unknown",
            )
        self.assertEqual(callbacks, [])

    def test_mutated_historical_gate_mapping_is_rejected_before_iteration(self):
        callbacks = []

        class HostileChecks(dict):
            def __iter__(self):
                callbacks.append("iter")
                raise AssertionError("hostile GateDecision checks iterated")

        decision = gate("PASS", "market")
        object.__setattr__(decision, "checks", HostileChecks({"market_core": "PASS"}))
        with self.assertRaisesRegex(TypeError, "noncanonical retained fields"):
            EvaluationLayerReceipt.from_historical_gate(
                layer=BLINDED_MARKET_REPLAY,
                candidate_id="candidate-1",
                exact_build_sha=BUILD,
                candidate_selected_at=SELECTED,
                protocol_sha256=h("2"),
                evidence_sha256=h("1"),
                decision=decision,
                model_training_cutoff_uncertainty="unknown",
            )
        self.assertEqual(callbacks, [])

    def test_mutated_forward_containers_are_rejected_before_callbacks(self):
        callbacks = []

        class HostilePredictions(tuple):
            def __iter__(self):
                callbacks.append("predictions")
                raise AssertionError("hostile predictions iterated")

        class HostileCosts(dict):
            def items(self):
                callbacks.append("costs")
                raise AssertionError("hostile costs read")

        for field, hostile in (
            ("predictions", HostilePredictions(())),
            ("costs_by_currency", HostileCosts({"USD": "1"})),
        ):
            with self.subTest(field=field):
                protocol, evidence, assessment = forward_fixture()
                object.__setattr__(evidence, field, hostile)
                with self.assertRaisesRegex(TypeError, "noncanonical retained containers"):
                    EvaluationLayerReceipt.from_forward_paper(
                        candidate_id="candidate-1",
                        candidate_selected_at=SELECTED,
                        protocol=protocol,
                        evidence=evidence,
                        assessment=assessment,
                    )
        self.assertEqual(callbacks, [])


if __name__ == "__main__":
    unittest.main()
