from types import SimpleNamespace
import unittest
from unittest.mock import patch

from mvp.autotrade_mvp import science_qualification as science
from mvp.autotrade_mvp.science_qualification import (
    QualificationGate,
    ScientificQualificationInput,
    qualify_scientific_learning,
)


H = "sha256:" + "1" * 64
P = "sha256:" + "2" * 64
SOURCE = "a" * 40


def _gate(name: str) -> QualificationGate:
    evidence_hashes = (H, P) if name == "retention" else (H,)
    return QualificationGate(
        name,
        "PASS",
        evidence_hashes,
        H,
        H,
        H,
    )


def _value() -> ScientificQualificationInput:
    return ScientificQualificationInput(
        H,
        H,
        H,
        tuple(_gate(name) for name in science._REQUIRED_GATES),
        "ECONOMIC_EDGE_QUALIFIED",
        False,
        False,
        P,
        SOURCE,
    )


def _accepted_for(value: ScientificQualificationInput):
    requirements = tuple(science._required_signed_bindings(value))
    digests = tuple(science._required_evidence_digests(value))
    return SimpleNamespace(
        attestation_id="00000000-0000-0000-0000-000000000001",
        attestation_digest="sha256:" + "3" * 64,
        policy_id="sha256:" + "4" * 64,
        policy_version="1.0.0",
        trust_root_id="sha256:" + "5" * 64,
        result="PASS",
        requirement_ids=requirements,
        evidence_refs=tuple(SimpleNamespace(sha256=item) for item in digests),
    )


class ScientificInputSnapshotTests(unittest.TestCase):
    def test_terminal_science_rejects_polymorphic_input_before_trust(self):
        class HostileInput(ScientificQualificationInput):
            pass

        baseline = _value()
        hostile = HostileInput(
            baseline.candidate_hash,
            baseline.frozen_protocol_hash,
            baseline.input_snapshot_hash,
            baseline.gates,
            baseline.economic_claim,
            baseline.holdout_used_for_tuning,
            baseline.future_information_used_for_routing,
            baseline.population_coverage_hash,
            baseline.source_sha,
        )

        with self.assertRaises(TypeError):
            qualify_scientific_learning(hostile)

    def test_terminal_science_rejects_polymorphic_nested_gate(self):
        class HostileGate(QualificationGate):
            pass

        gates = list(_value().gates)
        original = gates[0]
        gates[0] = HostileGate(
            original.gate_id,
            original.status,
            original.evidence_hashes,
            original.candidate_hash,
            original.frozen_protocol_hash,
            original.input_snapshot_hash,
            original.reason_codes,
        )
        hostile = ScientificQualificationInput(
            H,
            H,
            H,
            tuple(gates),
            "ECONOMIC_EDGE_QUALIFIED",
            False,
            False,
            P,
            SOURCE,
        )

        with self.assertRaises(TypeError):
            qualify_scientific_learning(hostile)

    def test_verifier_side_mutation_cannot_change_terminal_science_decision(self):
        value = _value()
        accepted = _accepted_for(value)

        def verify_and_mutate(*_args, **_kwargs):
            # Reproduce the caller-object TOCTOU class after the signed snapshot
            # has been acquired by the production entrypoint.
            object.__setattr__(value, "economic_claim", "NONE")
            object.__setattr__(value, "holdout_used_for_tuning", True)
            object.__setattr__(value, "future_information_used_for_routing", True)
            return accepted

        with patch.object(
            science,
            "verify_canonical_qualification_attestation",
            side_effect=verify_and_mutate,
        ):
            result = qualify_scientific_learning(
                value,
                qualification_receipt=object(),
                evidence_store=object(),
                evidence_root="/independent/evidence/root",
                trusted_source_sha=SOURCE,
            )

        self.assertEqual(value.economic_claim, "NONE")
        self.assertTrue(value.holdout_used_for_tuning)
        self.assertTrue(value.future_information_used_for_routing)
        self.assertEqual(result.status, "PASS")
        self.assertTrue(result.economic_claim_accepted)
        self.assertIn(("holdout_usage", "PASS"), result.checks)
        self.assertIn(("routing_causality", "PASS"), result.checks)


if __name__ == "__main__":
    unittest.main()
