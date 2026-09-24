import tempfile
import unittest
from pathlib import Path

from mvp.autotrade_mvp.sealed_forward import SealedForwardStore


class SealedForwardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "forward.sqlite"
        self.store = SealedForwardStore(self.path)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def seal(self, **changes):
        values = dict(
            prediction_id="p1",
            candidate_hash="sha256:candidate",
            input_hash="sha256:input",
            decision_time="2026-09-24T10:00:00Z",
            deadline="2026-09-24T10:00:05Z",
            sealed_at="2026-09-24T10:00:02Z",
            prediction={"direction": "UP", "score": "0.7"},
        )
        values.update(changes)
        return self.store.seal(**values)

    def test_prediction_is_sealed_before_outcome_and_survives_reopen(self):
        prediction = self.seal()
        self.store.reconcile_outcome(
            prediction_id="p1",
            outcome_available_at="2026-09-24T11:00:00Z",
            reconciled_at="2026-09-24T11:00:03Z",
            outcome={"return": "0.01"},
            evidence_id="fill-and-mark-1",
        )
        self.store.close()
        self.store = SealedForwardStore(self.path)
        replayed, outcome = self.store.paired_record("p1")
        self.assertEqual(replayed.prediction_hash, prediction.prediction_hash)
        self.assertEqual(outcome.evidence_id, "fill-and-mark-1")
        self.assertEqual(self.store.audit(), ())

    def test_late_prediction_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "not sealed before deadline"):
            self.seal(sealed_at="2026-09-24T10:00:06Z")

    def test_prediction_before_decision_time_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "before decision_time"):
            self.seal(sealed_at="2026-09-24T09:59:59Z")

    def test_prediction_identity_cannot_be_rewritten(self):
        self.seal()
        self.seal()
        with self.assertRaisesRegex(ValueError, "prediction identity conflict"):
            self.seal(prediction={"direction": "DOWN"})

    def test_outcome_must_be_later_and_reconciled(self):
        self.seal()
        with self.assertRaisesRegex(ValueError, "after prediction"):
            self.store.reconcile_outcome(
                prediction_id="p1",
                outcome_available_at="2026-09-24T10:00:01Z",
                reconciled_at="2026-09-24T10:00:03Z",
                outcome={"return": "0"},
                evidence_id="e1",
            )
        with self.assertRaisesRegex(ValueError, "before it is available"):
            self.store.reconcile_outcome(
                prediction_id="p1",
                outcome_available_at="2026-09-24T11:00:00Z",
                reconciled_at="2026-09-24T10:59:59Z",
                outcome={"return": "0"},
                evidence_id="e1",
            )

    def test_outcome_identity_cannot_be_rewritten(self):
        self.seal()
        kwargs=dict(
            prediction_id="p1",
            outcome_available_at="2026-09-24T11:00:00Z",
            reconciled_at="2026-09-24T11:00:03Z",
            outcome={"return": "0.01"},
            evidence_id="e1",
        )
        self.store.reconcile_outcome(**kwargs)
        self.store.reconcile_outcome(**kwargs)
        with self.assertRaisesRegex(ValueError, "outcome identity conflict"):
            self.store.reconcile_outcome(**{**kwargs, "outcome": {"return": "0.02"}})

    def test_scoring_without_outcome_is_impossible(self):
        self.seal()
        with self.assertRaisesRegex(ValueError, "no reconciled outcome"):
            self.store.paired_record("p1")


if __name__ == "__main__":
    unittest.main()
