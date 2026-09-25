from decimal import Decimal
import unittest

from autotrade_research.forward_paper import (
    ForwardOutcome,
    ForwardPaperError,
    ForwardPaperEvidence,
    ForwardPaperProtocol,
    OperationalObservation,
    SealedPrediction,
    assess_forward_paper,
)


BUILD = "3cae63fac37820611cddd38128a91185cb271fff"
HASH_A = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64
HASH_C = "sha256:" + "c" * 64
HASH_D = "sha256:" + "d" * 64


class ForwardPaperQualificationTests(unittest.TestCase):
    def protocol(self, **overrides):
        values = dict(
            campaign_id="paper-campaign-1",
            exact_build_sha=BUILD,
            protocol_hash=HASH_A,
            starts_at="2026-09-24T20:00:00Z",
            ends_at="2026-09-24T21:00:00Z",
            minimum_predictions=2,
            maximum_decision_latency_ms=1000,
            required_provider_capabilities=("KRAKEN:SPOT:LIMIT", "IBKR:EQUITY:LIMIT"),
            required_operational_cases=("RECONNECT", "MANUAL_ACTIVITY"),
        )
        values.update(overrides)
        return ForwardPaperProtocol.create(**values)

    def predictions(self):
        return (
            SealedPrediction.create(
                prediction_id="pred-1",
                provider_capability="KRAKEN:SPOT:LIMIT",
                input_hash=HASH_B,
                proposal_hash=HASH_C,
                information_cutoff_at="2026-09-24T20:04:59Z",
                sealed_at="2026-09-24T20:05:00Z",
                decision_deadline_at="2026-09-24T20:05:02Z",
                outcome_horizon_end_at="2026-09-24T20:30:00Z",
                decision_latency_ms=400,
            ),
            SealedPrediction.create(
                prediction_id="pred-2",
                provider_capability="IBKR:EQUITY:LIMIT",
                input_hash=HASH_C,
                proposal_hash=HASH_D,
                information_cutoff_at="2026-09-24T20:09:59Z",
                sealed_at="2026-09-24T20:10:00Z",
                decision_deadline_at="2026-09-24T20:10:02Z",
                outcome_horizon_end_at="2026-09-24T20:40:00Z",
                decision_latency_ms=500,
            ),
        )

    def outcomes(self):
        return (
            ForwardOutcome.create(
                prediction_id="pred-1",
                outcome_hash=HASH_D,
                outcome_available_at="2026-09-24T20:30:01Z",
                evaluated_at="2026-09-24T20:31:00Z",
            ),
            ForwardOutcome.create(
                prediction_id="pred-2",
                outcome_hash=HASH_B,
                outcome_available_at="2026-09-24T20:40:01Z",
                evaluated_at="2026-09-24T20:41:00Z",
            ),
        )

    def operational(self):
        rows = []
        for capability in ("KRAKEN:SPOT:LIMIT", "IBKR:EQUITY:LIMIT"):
            rows.extend(
                [
                    OperationalObservation.create(
                        provider_capability=capability,
                        case="RECONNECT",
                        observed_at="2026-09-24T20:45:00Z",
                        reconciled=True,
                    ),
                    OperationalObservation.create(
                        provider_capability=capability,
                        case="MANUAL_ACTIVITY",
                        observed_at="2026-09-24T20:50:00Z",
                        reconciled=True,
                    ),
                ]
            )
        return tuple(rows)

    def evidence(self, **overrides):
        values = dict(
            exact_build_sha=BUILD,
            protocol_hash=HASH_A,
            observed_until="2026-09-24T21:01:00Z",
            predictions=self.predictions(),
            outcomes=self.outcomes(),
            operational_observations=self.operational(),
            costs_by_currency={"USD": "12.34", "EUR": Decimal("1.25")},
            costs_complete=True,
            account_reconciliation_complete=True,
        )
        values.update(overrides)
        return ForwardPaperEvidence.create(**values)

    def test_complete_mechanics_can_be_valid_without_claiming_edge(self):
        result = assess_forward_paper(self.protocol(), self.evidence())
        self.assertEqual(result.evidence_status, "VALID")
        self.assertEqual(result.operational_status, "PASS")
        self.assertEqual(result.economic_edge_status, "NOT_ESTABLISHED")
        self.assertEqual(result.prediction_count, 2)
        self.assertEqual(result.evaluated_outcome_count, 2)
        self.assertEqual(result.reasons, ())

    def test_campaign_before_registered_end_is_inconclusive(self):
        result = assess_forward_paper(
            self.protocol(),
            self.evidence(observed_until="2026-09-24T20:59:59Z"),
        )
        self.assertEqual(result.evidence_status, "INCONCLUSIVE")
        self.assertIn("campaign_window_not_finished", result.reasons)
        self.assertEqual(result.economic_edge_status, "NOT_ESTABLISHED")

    def test_missing_provider_capability_prevents_complete_evidence(self):
        result = assess_forward_paper(
            self.protocol(),
            self.evidence(predictions=(self.predictions()[0],), outcomes=(self.outcomes()[0],)),
        )
        self.assertEqual(result.evidence_status, "INCONCLUSIVE")
        self.assertIn("minimum_prediction_count_not_reached", result.reasons)
        self.assertIn("missing_provider_capability:IBKR:EQUITY:LIMIT", result.reasons)

    def test_missing_reconnect_or_manual_activity_is_inconclusive(self):
        observations = tuple(
            row for row in self.operational() if row.case != "MANUAL_ACTIVITY"
        )
        result = assess_forward_paper(
            self.protocol(),
            self.evidence(operational_observations=observations),
        )
        self.assertEqual(result.evidence_status, "INCONCLUSIVE")
        self.assertTrue(
            any(reason.startswith("missing_operational_case:") for reason in result.reasons)
        )

    def test_unreconciled_operational_case_is_real_failure_not_fake_pass(self):
        rows = list(self.operational())
        rows[0] = OperationalObservation.create(
            provider_capability=rows[0].provider_capability,
            case=rows[0].case,
            observed_at=rows[0].observed_at,
            reconciled=False,
        )
        result = assess_forward_paper(
            self.protocol(),
            self.evidence(operational_observations=rows),
        )
        self.assertEqual(result.operational_status, "FAIL")
        self.assertIn("unreconciled_operational_case", result.reasons)

    def test_late_decision_is_valid_negative_operational_evidence(self):
        predictions = list(self.predictions())
        predictions[0] = SealedPrediction.create(
            prediction_id="pred-1",
            provider_capability="KRAKEN:SPOT:LIMIT",
            input_hash=HASH_B,
            proposal_hash=HASH_C,
            information_cutoff_at="2026-09-24T20:04:59Z",
            sealed_at="2026-09-24T20:05:03Z",
            decision_deadline_at="2026-09-24T20:05:02Z",
            outcome_horizon_end_at="2026-09-24T20:30:00Z",
            decision_latency_ms=1200,
        )
        result = assess_forward_paper(
            self.protocol(),
            self.evidence(predictions=predictions),
        )
        self.assertEqual(result.evidence_status, "VALID")
        self.assertEqual(result.operational_status, "FAIL")
        self.assertIn("decision_deadline_missed", result.reasons)
        self.assertIn("decision_latency_budget_exceeded", result.reasons)

    def test_future_information_cutoff_is_rejected_at_seal_creation(self):
        with self.assertRaisesRegex(ForwardPaperError, "information cutoff"):
            SealedPrediction.create(
                prediction_id="pred-bad",
                provider_capability="KRAKEN:SPOT:LIMIT",
                input_hash=HASH_B,
                proposal_hash=HASH_C,
                information_cutoff_at="2026-09-24T20:05:01Z",
                sealed_at="2026-09-24T20:05:00Z",
                decision_deadline_at="2026-09-24T20:05:02Z",
                outcome_horizon_end_at="2026-09-24T20:30:00Z",
                decision_latency_ms=100,
            )

    def test_outcome_available_before_registered_horizon_invalidates_campaign(self):
        outcomes = list(self.outcomes())
        outcomes[0] = ForwardOutcome.create(
            prediction_id="pred-1",
            outcome_hash=HASH_D,
            outcome_available_at="2026-09-24T20:29:59Z",
            evaluated_at="2026-09-24T20:31:00Z",
        )
        result = assess_forward_paper(
            self.protocol(),
            self.evidence(outcomes=outcomes),
        )
        self.assertEqual(result.evidence_status, "INVALID")
        self.assertIn("outcome_available_before_registered_horizon", result.reasons)

    def test_exact_build_and_protocol_binding_fail_closed(self):
        wrong_build = "1" * 40
        result = assess_forward_paper(
            self.protocol(),
            self.evidence(exact_build_sha=wrong_build, protocol_hash=HASH_B),
        )
        self.assertEqual(result.evidence_status, "INVALID")
        self.assertIn("exact_build_sha_mismatch", result.reasons)
        self.assertIn("protocol_hash_mismatch", result.reasons)

    def test_duplicate_outcome_is_invalid_evidence(self):
        result = assess_forward_paper(
            self.protocol(),
            self.evidence(outcomes=self.outcomes() + (self.outcomes()[0],)),
        )
        self.assertEqual(result.evidence_status, "INVALID")
        self.assertIn("duplicate_outcome_for_prediction", result.reasons)

    def test_non_string_cost_currency_is_rejected(self):
        with self.assertRaisesRegex(TypeError, "currency keys must be strings"):
            self.evidence(costs_by_currency={840: "12.34"})

    def test_float_cost_is_rejected(self):
        with self.assertRaisesRegex(ForwardPaperError, "exact decimal"):
            self.evidence(costs_by_currency={"USD": 12.34})

    def test_cost_and_reconciliation_completeness_are_required(self):
        result = assess_forward_paper(
            self.protocol(),
            self.evidence(
                costs_complete=False,
                account_reconciliation_complete=False,
            ),
        )
        self.assertEqual(result.evidence_status, "INCONCLUSIVE")
        self.assertIn("actual_costs_incomplete", result.reasons)
        self.assertIn("account_reconciliation_incomplete", result.reasons)


    def test_evidence_cannot_include_records_after_observed_until(self):
        result = assess_forward_paper(
            self.protocol(),
            self.evidence(observed_until="2026-09-24T20:35:00Z"),
        )
        self.assertEqual(result.evidence_status, "INVALID")
        self.assertIn("outcome_after_observed_until", result.reasons)
        self.assertIn("operational_case_after_observed_until", result.reasons)

    def test_required_operational_cases_are_case_insensitively_unique(self):
        with self.assertRaisesRegex(ForwardPaperError, "case-insensitive duplicates"):
            self.protocol(required_operational_cases=("reconnect", "RECONNECT"))



    def test_complete_cost_flag_with_empty_ledger_is_inconclusive(self):
        result = assess_forward_paper(
            self.protocol(),
            self.evidence(costs_by_currency={}),
        )
        self.assertEqual(result.evidence_status, "INCONCLUSIVE")
        self.assertIn("actual_cost_ledger_empty", result.reasons)



    def test_uppercase_build_sha_is_rejected_not_normalized(self):
        with self.assertRaisesRegex(ForwardPaperError, "lowercase git SHA"):
            self.protocol(exact_build_sha=BUILD.upper())
        with self.assertRaisesRegex(ForwardPaperError, "lowercase git SHA"):
            self.evidence(exact_build_sha=BUILD.upper())

    def test_direct_prediction_construction_cannot_bypass_causal_cutoff(self):
        with self.assertRaisesRegex(ForwardPaperError, "information cutoff"):
            SealedPrediction(
                prediction_id="direct-bad",
                provider_capability="KRAKEN:SPOT:LIMIT",
                input_hash=HASH_B,
                proposal_hash=HASH_C,
                information_cutoff_at="2026-09-24T20:05:01Z",
                sealed_at="2026-09-24T20:05:00Z",
                decision_deadline_at="2026-09-24T20:05:02Z",
                outcome_horizon_end_at="2026-09-24T20:30:00Z",
                decision_latency_ms=100,
            )

    def test_direct_outcome_construction_cannot_precede_availability(self):
        with self.assertRaisesRegex(ForwardPaperError, "cannot precede"):
            ForwardOutcome(
                prediction_id="pred-1",
                outcome_hash=HASH_D,
                outcome_available_at="2026-09-24T20:30:01Z",
                evaluated_at="2026-09-24T20:30:00Z",
            )

    def test_direct_protocol_construction_cannot_disable_sample_requirement(self):
        with self.assertRaisesRegex(ForwardPaperError, "minimum_predictions"):
            ForwardPaperProtocol(
                campaign_id="direct-bad",
                exact_build_sha=BUILD,
                protocol_hash=HASH_A,
                starts_at="2026-09-24T20:00:00Z",
                ends_at="2026-09-24T21:00:00Z",
                minimum_predictions=0,
                maximum_decision_latency_ms=1000,
                required_provider_capabilities=("KRAKEN:SPOT:LIMIT",),
                required_operational_cases=("RECONNECT",),
            )


if __name__ == "__main__":
    unittest.main()
