"""Real alpha checkpoint recovery through canonical jobs, artifacts and registry."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from importlib.util import find_spec
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

_REQUIRED_CORE = ("numpy", "threadpoolctl")
_missing_core = [name for name in _REQUIRED_CORE if find_spec(name) is None]
if _missing_core:
    raise unittest.SkipTest("alpha worker core dependencies missing: " + ", ".join(_missing_core))

from mvp.autotrade_mvp.alpha_research_worker import AlphaResearchWorker
from research.autotrade_research.jobs import ResearchJobStore, JobConflictError, JobLeaseError
from research.autotrade_research.science.registry import ScientificRegistry, ProtocolViolation
from tools.run_whitebit_alpha_research import demo_input


class WorkerFixture:
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.artifact_root = self.root / "artifacts"
        self.jobs = ResearchJobStore(self.root / "jobs.sqlite3", authoritative_artifact_root=self.artifact_root)
        self.registry = ScientificRegistry(self.root / "science.sqlite3")
        self.worker = AlphaResearchWorker(jobs=self.jobs, registry=self.registry, artifact_root=self.artifact_root)
        self.payload = json.loads(json.dumps(demo_input()))

    def another_worker(self):
        return AlphaResearchWorker(
            jobs=ResearchJobStore(self.root / "jobs.sqlite3", authoritative_artifact_root=self.artifact_root),
            registry=ScientificRegistry(self.root / "science.sqlite3"), artifact_root=self.artifact_root)


class AlphaWorkerGuardTests(WorkerFixture, unittest.TestCase):
    def test_repeat_submission_has_one_durable_job(self):
        first, created = self.worker.enqueue(self.payload, steps=2)
        second, created_again = self.another_worker().enqueue(self.payload, steps=2)
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(first["job_id"], second["job_id"])
        self.assertEqual(first["kind"], "research.alpha")
        self.assertTrue(first["lease_requeueable"])
        self.assertEqual(second["state"], "QUEUED")

    def test_frozen_configuration_is_a_distinct_job(self):
        first, _ = self.worker.enqueue(self.payload, steps=2)
        second, _ = self.worker.enqueue(self.payload, steps=3)
        self.assertNotEqual(first["job_id"], second["job_id"])
        self.assertNotEqual(first["input_hashes"], second["input_hashes"])

    def test_targeted_claim_does_not_take_an_unrelated_queued_job(self):
        other, _ = self.jobs.enqueue(
            kind="research.replay", dedupe_key="unrelated", input_hashes=["sha256:" + "b" * 64],
            resource_budget={"wall_seconds": 60}, lease_requeueable=True)
        target, _ = self.worker.enqueue(self.payload, steps=2)
        claimed = self.jobs.claim("alpha", job_id=target["job_id"])
        self.assertEqual(claimed["job_id"], target["job_id"])
        self.assertEqual(self.jobs.get(other["job_id"])["state"], "QUEUED")
        self.assertIsNone(self.jobs.claim("second", job_id=target["job_id"]))

    def test_invalid_input_and_configuration_do_not_enqueue(self):
        extra = deepcopy(self.payload)
        extra["credentials"] = "forbidden"
        for payload, config in ((extra, {}), (self.payload, {"steps": 0}),
                                (self.payload, {"backends": ("XGBOOST", "XGBOOST")}),
                                (self.payload, {"horizon": 0})):
            with self.subTest(config=config), self.assertRaises(ValueError):
                self.worker.enqueue(payload, **config)
        with sqlite3.connect(self.jobs.path) as con:
            self.assertEqual(con.execute("SELECT count(*) FROM jobs").fetchone()[0], 0)

    def test_worker_cannot_switch_the_jobs_artifact_authority(self):
        with self.assertRaises(JobConflictError):
            AlphaResearchWorker(jobs=self.jobs, registry=self.registry, artifact_root=self.root / "foreign")

    def test_paused_alpha_job_does_not_train(self):
        job, _ = self.worker.enqueue(self.payload, steps=2)
        self.jobs.pause(job["job_id"])
        with patch("mvp.autotrade_mvp.alpha_research_worker.build_alpha_packet",
                   side_effect=AssertionError("paused job trained")):
            with self.assertRaises(JobLeaseError):
                self.worker.run(job["job_id"])
        self.assertEqual(self.jobs.get(job["job_id"])["state"], "PAUSED")

    def test_expired_alpha_lease_is_fenced_and_retry_safe(self):
        job, _ = self.worker.enqueue(self.payload, steps=2)
        now = datetime.now(timezone.utc)
        old = self.jobs.claim("first", job_id=job["job_id"], now=now, lease_seconds=1)
        self.assertEqual(self.jobs.requeue_expired(now=now + timedelta(seconds=2)), 1)
        new = self.jobs.claim("second", job_id=job["job_id"], now=now + timedelta(seconds=2))
        self.assertEqual(int(new["generation"]), int(old["generation"]) + 1)
        with self.assertRaises(JobLeaseError):
            self.jobs.read_checkpoint_bytes(job["job_id"], worker_id="first",
                                            generation=int(old["generation"]), now=now + timedelta(seconds=2))

    def test_source_implementation_change_requires_new_submission(self):
        job, _ = self.worker.enqueue(self.payload, steps=2)
        with patch("mvp.autotrade_mvp.alpha_research_worker._implementation", return_value={"changed": "build"}):
            with self.assertRaisesRegex(ValueError, "implementation differs"):
                self.worker.run(job["job_id"])
        self.assertEqual(self.jobs.get(job["job_id"])["state"], "QUEUED")


_MISSING_NATIVE = [name for name in ("torch", "xgboost", "catboost", "shap") if find_spec(name) is None]


@unittest.skipIf(bool(_MISSING_NATIVE), "real native alpha backends missing: " + ", ".join(_MISSING_NATIVE))
class NativeAlphaWorkerTests(WorkerFixture, unittest.TestCase):
    def fit(self):
        job, _ = self.worker.enqueue(self.payload, steps=2)
        return job, self.worker.run(job["job_id"])

    def test_native_models_are_accepted_and_indexed_as_research_only(self):
        job, result = self.fit()
        self.assertEqual(self.jobs.get(job["job_id"])["state"], "SUCCEEDED")
        self.assertEqual(set(result["candidate"]["model_refs"]), {"DLINEAR", "XGBOOST", "CATBOOST"})
        self.assertFalse(result["live_authority"])
        self.assertFalse(result["automatic_promotion"])
        self.assertFalse(result["candidate"]["dag"]["result"]["live_authority_granted"])
        with sqlite3.connect(self.registry.path) as con:
            row = con.execute(
                "SELECT payload_json FROM research_model_candidates WHERE candidate_ref=?",
                (result["candidate_ref"],)).fetchone()
            self.assertEqual(json.loads(row[0]), result["candidate"])
            with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
                con.execute("DELETE FROM research_model_candidates")
            self.assertEqual(con.execute("SELECT count(*) FROM protocols").fetchone()[0], 0)
            self.assertEqual(con.execute("SELECT count(*) FROM evaluations").fetchone()[0], 0)

    def test_completed_job_restart_uses_native_inference_without_fit(self):
        job, result = self.fit()
        forbidden = AssertionError("completed job attempted training")
        with patch("mvp.autotrade_mvp.alpha_research_worker.build_alpha_packet", side_effect=forbidden), \
             patch("xgboost.XGBRegressor.fit", side_effect=forbidden), \
             patch("catboost.CatBoostRegressor.fit", side_effect=forbidden), \
             patch("torch.optim.Adam", side_effect=forbidden):
            self.assertEqual(self.another_worker().run(job["job_id"]), result)

    def test_restart_after_checkpoint_reuses_native_models_without_training(self):
        job, _ = self.worker.enqueue(self.payload, steps=2)
        with patch.object(self.registry, "register_research_model_candidate", side_effect=OSError("crash after checkpoint")):
            with self.assertRaisesRegex(OSError, "crash after checkpoint"):
                self.worker.run(job["job_id"])
        interrupted = self.jobs.get(job["job_id"])
        self.assertEqual(interrupted["state"], "RUNNING")
        self.assertIsNotNone(interrupted["checkpoint_ref"])
        self.jobs.pause(job["job_id"])
        self.jobs.resume(job["job_id"])
        forbidden = AssertionError("checkpoint recovery attempted fit")
        with patch("mvp.autotrade_mvp.alpha_research_worker.build_alpha_packet", side_effect=forbidden), \
             patch("xgboost.XGBRegressor.fit", side_effect=forbidden), \
             patch("catboost.CatBoostRegressor.fit", side_effect=forbidden), \
             patch("torch.optim.Adam", side_effect=forbidden):
            result = self.another_worker().run(job["job_id"])
        completed = self.jobs.get(job["job_id"])
        self.assertEqual(completed["state"], "SUCCEEDED")
        self.assertEqual(int(completed["generation"]), int(interrupted["generation"]) + 1)
        self.assertEqual(completed["checkpoint_ref"], interrupted["checkpoint_ref"])
        self.assertFalse(result["automatic_promotion"])

    def test_candidate_registry_rejects_authority_escalation(self):
        _job, result = self.fit()
        candidate = deepcopy(result["candidate"])
        candidate["automatic_promotion"] = True
        sources = ([candidate["request_ref"]] +
                   [candidate["model_refs"][name] for name in ("DLINEAR", "XGBOOST", "CATBOOST")] +
                   [candidate["dag"]["packet_ref"], candidate["dag"]["result_ref"]])
        reference, _ = self.worker._publish(candidate, "ALPHA_MODEL_CANDIDATE", sources)
        with self.assertRaises(ProtocolViolation):
            self.registry.register_research_model_candidate(reference, authoritative_artifact_root=self.artifact_root)


if __name__ == "__main__":
    unittest.main()
