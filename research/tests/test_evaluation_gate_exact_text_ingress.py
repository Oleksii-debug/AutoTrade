from dataclasses import replace
import unittest

from research.autotrade_research.evaluation.gates import (
    GateDecision,
    GateEvidenceRef,
)
from research.tests.test_evaluation_gates import evidence, profile


class HostileText(str):
    calls = 0

    @classmethod
    def reset(cls) -> None:
        cls.calls = 0

    def _callback(self, *_args, **_kwargs):
        type(self).calls += 1
        raise AssertionError("hostile text callback executed")

    strip = _callback
    lower = _callback
    upper = _callback
    startswith = _callback
    __eq__ = _callback
    __hash__ = _callback


class EvaluationGateExactTextIngressTests(unittest.TestCase):
    def setUp(self) -> None:
        HostileText.reset()

    def assertNoHostileCallbacks(self) -> None:
        self.assertEqual(HostileText.calls, 0)

    def test_gate_profile_identity_text_rejects_subclasses_before_dispatch(self):
        base = profile()
        for field, value, error in (
            ("profile_id", HostileText("gate-v1"), ValueError),
            ("primary_baseline_id", HostileText("champion"), ValueError),
            ("selection_correction", HostileText("holm-v1"), ValueError),
        ):
            with self.subTest(field=field):
                HostileText.reset()
                with self.assertRaises(error):
                    replace(base, **{field: value})
                self.assertNoHostileCallbacks()

    def test_gate_profile_collection_members_reject_subclasses_before_dispatch(self):
        base = profile()

        with self.assertRaises(TypeError):
            replace(
                base,
                baseline_ids=(
                    HostileText("cash"),
                    "passive",
                    "champion",
                ),
            )
        self.assertNoHostileCallbacks()

        HostileText.reset()
        with self.assertRaises(TypeError):
            replace(
                base,
                required_regimes=(HostileText("normal"), "stress"),
            )
        self.assertNoHostileCallbacks()

    def test_evaluation_evidence_identity_text_rejects_subclasses_before_dispatch(self):
        base = evidence()

        with self.assertRaises(ValueError):
            replace(base, registered_profile_id=HostileText("gate-v1"))
        self.assertNoHostileCallbacks()

        HostileText.reset()
        with self.assertRaises(ValueError):
            replace(
                base,
                selection_correction_applied=HostileText("holm-v1"),
            )
        self.assertNoHostileCallbacks()

        HostileText.reset()
        with self.assertRaises(TypeError):
            replace(
                base,
                regime_coverage=(HostileText("normal"), "stress"),
            )
        self.assertNoHostileCallbacks()

    def test_gate_evidence_ref_rejects_polymorphic_identity_before_uuid_or_digest_dispatch(self):
        with self.assertRaises(ValueError):
            GateEvidenceRef(
                artifact_id=HostileText(
                    "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
                ),
                sha256="sha256:" + "a" * 64,
            )
        self.assertNoHostileCallbacks()

        HostileText.reset()
        with self.assertRaises(ValueError):
            GateEvidenceRef(
                artifact_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                sha256=HostileText("sha256:" + "a" * 64),
            )
        self.assertNoHostileCallbacks()

    def test_gate_decision_text_rejects_subclasses_before_dispatch(self):
        with self.assertRaises(ValueError):
            GateDecision(
                status=HostileText("PASS"),
                reasons=("ok",),
                checks={"gate": "PASS"},
            )
        self.assertNoHostileCallbacks()

        HostileText.reset()
        with self.assertRaises(ValueError):
            GateDecision(
                status="PASS",
                reasons=(HostileText("ok"),),
                checks={"gate": "PASS"},
            )
        self.assertNoHostileCallbacks()

        HostileText.reset()
        with self.assertRaises(TypeError):
            GateDecision(
                status="PASS",
                reasons=("ok",),
                checks={"gate": HostileText("PASS")},
            )
        self.assertNoHostileCallbacks()


if __name__ == "__main__":
    unittest.main()
