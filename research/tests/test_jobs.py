from contextlib import closing
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import uuid4

from autotrade_research.artifacts.store import ArtifactStore
from autotrade_research.jobs import (
    JobBudgetError,
    JobConflictError,
    JobLeaseError,
    ResearchJobStore,
    _require_immutable_artifact_ref,
)


def digest(text: str) -> str:
    return "sha256:" + sha256(text.encode("utf-8")).hexdigest()


def checkpoint_ref(artifact_id: str, payload: str) -> str:
    return f"artifact:{artifact_id}@{digest(payload)}"


def resolution_proof(
    directory,
    *,
    job_id,
    generation,
    verdict,
    artifact_id=None,
    output_refs=None,
):
    artifact_store = ArtifactStore(Path(directory) / "external-resolution-artifacts")
    proof = {
        "schema_version": "1.0.0",
        "artifact_kind": "RESEARCH_JOB_EXTERNAL_RESOLUTION",
        "job_id": job_id,
        "generation": generation,
        "verdict": verdict,
    }
    canonical_outputs = [] if output_refs is None else sorted(output_refs)
    if verdict == "PROVEN_SUCCEEDED":
        proof["output_refs"] = canonical_outputs
    manifest = artifact_store.publish_bytes(
        artifact_id=str(uuid4()) if artifact_id is None else artifact_id,
        data=json.dumps(
            proof,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8"),
        media_type="application/json",
        rights={"storage": True, "export": False},
        metadata={
            "artifact_kind": proof["artifact_kind"],
            "job_id": job_id,
            "generation": generation,
            "verdict": verdict,
            **(
                {"output_refs": canonical_outputs}
                if verdict == "PROVEN_SUCCEEDED"
                else {}
            ),
        },
    )
    return (
        artifact_store,
        f"artifact:{manifest['artifact_id']}@{manifest['sha256']}",
    )


def result_artifact(
    artifact_store,
    *,
    job_id,
    generation,
    input_hashes,
    data=b"accepted-result",
    artifact_id=None,
):
    manifest = artifact_store.publish_bytes(
        artifact_id=str(uuid4()) if artifact_id is None else artifact_id,
        data=data,
        media_type="application/octet-stream",
        rights={"storage": True, "export": False},
        source_refs=input_hashes,
        metadata={
            "artifact_kind": "RESEARCH_JOB_RESULT",
            "job_id": job_id,
            "job_generation": generation,
        },
    )
    return f"artifact:{manifest['artifact_id']}@{manifest['sha256']}"


class ResearchJobStoreTests(unittest.TestCase):

    def test_immutable_artifact_ref_requires_canonical_uuid_text(self):
        canonical = (
            "artifact:aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa@sha256:"
            + "b" * 64
        )
        self.assertEqual(
            _require_immutable_artifact_ref(canonical, "ref"),
            canonical,
        )
        with self.assertRaisesRegex(ValueError, "canonical UUID text"):
            _require_immutable_artifact_ref(
                "artifact:AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA@sha256:"
                + "b" * 64,
                "ref",
            )


    def setUp(self):
        self.now = datetime(2026, 9, 24, 16, 0, tzinfo=timezone.utc)

    def _enqueue(self, store: ResearchJobStore, key: str = "same"):
        return store.enqueue(
            kind="research.replay",
            dedupe_key=key,
            input_hashes=[digest("dataset")],
            resource_budget={"wall_seconds": 60, "memory_bytes": 1024},
            lease_requeueable=True,
            now=self.now,
        )

    def test_duplicate_submission_is_idempotent_but_conflict_is_rejected(self):
        with TemporaryDirectory() as directory:
            store = ResearchJobStore(Path(directory) / "jobs.sqlite3")
            first, inserted = self._enqueue(store)
            second, inserted_again = self._enqueue(store)
            self.assertTrue(inserted)
            self.assertFalse(inserted_again)
            self.assertEqual(first["job_id"], second["job_id"])
            with self.assertRaises(JobConflictError):
                store.enqueue(
                    kind="research.replay",
                    dedupe_key="same",
                    input_hashes=[digest("different")],
                    resource_budget={"wall_seconds": 60, "memory_bytes": 1024},
                    now=self.now,
                )

    def test_only_one_worker_holds_the_live_lease(self):
        with TemporaryDirectory() as directory:
            store = ResearchJobStore(Path(directory) / "jobs.sqlite3")
            self._enqueue(store)
            first = store.claim("worker-a", now=self.now, lease_seconds=30)
            second = store.claim("worker-b", now=self.now, lease_seconds=30)
            self.assertIsNotNone(first)
            self.assertIsNone(second)
            self.assertEqual(first["owner"], "worker-a")
            self.assertEqual(first["state"], "RUNNING")

    def test_expired_lease_fences_stale_worker_and_requeues(self):
        with TemporaryDirectory() as directory:
            store = ResearchJobStore(Path(directory) / "jobs.sqlite3")
            job, _ = self._enqueue(store)
            first = store.claim("worker-a", now=self.now, lease_seconds=10)
            self.assertEqual(store.requeue_expired(now=self.now + timedelta(seconds=11)), 1)
            second = store.claim("worker-b", now=self.now + timedelta(seconds=11), lease_seconds=30)
            self.assertEqual(second["generation"], "2")
            with self.assertRaises(JobLeaseError):
                store.succeed(
                    job["job_id"],
                    worker_id="worker-a",
                    generation=int(first["generation"]),
                    output_refs=[
                        "artifact:11111111-1111-4111-8111-111111111111@sha256:"
                        + "1" * 64
                    ],
                    now=self.now + timedelta(seconds=12),
                )
            artifacts = ArtifactStore(Path(directory) / "accepted-results")
            accepted_ref = result_artifact(
                artifacts,
                job_id=job["job_id"],
                generation=int(second["generation"]),
                input_hashes=[digest("dataset")],
                artifact_id="22222222-2222-4222-8222-222222222222",
            )
            self.assertTrue(
                store.succeed(
                    job["job_id"],
                    worker_id="worker-b",
                    generation=int(second["generation"]),
                    output_refs=[accepted_ref],
                    artifact_store=artifacts,
                    now=self.now + timedelta(seconds=12),
                )
            )

    def test_external_job_cannot_self_authorize_automatic_lease_retry(self):
        with TemporaryDirectory() as directory:
            store = ResearchJobStore(Path(directory) / "jobs.sqlite3")
            with self.assertRaisesRegex(
                ValueError,
                "not qualified for automatic lease requeue",
            ):
                store.enqueue(
                    kind="research.external_annotation",
                    dedupe_key="unsafe-self-retry",
                    input_hashes=[digest("dataset")],
                    resource_budget={"wall_seconds": 60},
                    lease_requeueable=True,
                    now=self.now,
                )
            self.assertIsNone(
                store.claim("worker-a", now=self.now, lease_seconds=10)
            )

    def test_expired_non_idempotent_job_is_not_requeued(self):
        with TemporaryDirectory() as directory:
            store = ResearchJobStore(Path(directory) / "jobs.sqlite3")
            job, _ = store.enqueue(
                kind="research.external_annotation",
                dedupe_key="external-side-effect",
                input_hashes=[digest("dataset")],
                resource_budget={"wall_seconds": 60},
                now=self.now,
            )
            claimed = store.claim("worker-a", now=self.now, lease_seconds=10)
            self.assertFalse(claimed["lease_requeueable"])

            self.assertEqual(
                store.requeue_expired(now=self.now + timedelta(seconds=11)),
                0,
            )
            waiting = store.get(job["job_id"])
            self.assertEqual(waiting["state"], "WAITING_EXTERNAL")
            self.assertEqual(waiting["generation"], "2")
            self.assertEqual(
                waiting["error"]["code"],
                "LEASE_EXPIRED_NON_IDEMPOTENT",
            )
            self.assertIsNone(
                store.claim(
                    "worker-b",
                    now=self.now + timedelta(seconds=12),
                    lease_seconds=30,
                )
            )

    def test_v1_migration_defaults_existing_jobs_to_non_requeueable(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "jobs.sqlite3"
            job_id = str(uuid4())
            with closing(sqlite3.connect(path)) as connection, connection:
                connection.executescript(
                    """
                    CREATE TABLE schema_migrations(
                        version INTEGER PRIMARY KEY,
                        applied_at TEXT NOT NULL
                    );
                    INSERT INTO schema_migrations(version, applied_at)
                    VALUES(1, '2026-09-24T15:00:00Z');
                    CREATE TABLE jobs (
                        job_id TEXT PRIMARY KEY,
                        kind TEXT NOT NULL,
                        dedupe_key TEXT NOT NULL UNIQUE,
                        input_hashes_json TEXT NOT NULL,
                        state TEXT NOT NULL,
                        generation INTEGER NOT NULL CHECK (generation > 0),
                        attempt INTEGER NOT NULL CHECK (attempt >= 0),
                        owner TEXT,
                        lease_until TEXT,
                        checkpoint_ref TEXT,
                        resource_budget_json TEXT NOT NULL,
                        resource_usage_json TEXT NOT NULL,
                        output_refs_json TEXT NOT NULL,
                        error_json TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    """
                )
                connection.execute(
                    """
                    INSERT INTO jobs(
                        job_id, kind, dedupe_key, input_hashes_json, state, generation,
                        attempt, owner, lease_until, checkpoint_ref, resource_budget_json,
                        resource_usage_json, output_refs_json, error_json, created_at, updated_at
                    ) VALUES(?, 'research.external_annotation', 'legacy', ?, 'RUNNING', 1,
                             1, 'legacy-worker', ?, NULL, ?, '{}', '[]', NULL, ?, ?)
                    """,
                    (
                        job_id,
                        '["' + digest("dataset") + '"]',
                        "2026-09-24T15:59:00Z",
                        '{"wall_seconds":60.0}',
                        "2026-09-24T15:00:00Z",
                        "2026-09-24T15:00:00Z",
                    ),
                )

            store = ResearchJobStore(path)
            migrated = store.get(job_id)
            self.assertFalse(migrated["lease_requeueable"])
            self.assertEqual(store.requeue_expired(now=self.now), 0)
            self.assertEqual(store.get(job_id)["state"], "WAITING_EXTERNAL")

    def test_requeueability_is_part_of_dedupe_identity(self):
        with TemporaryDirectory() as directory:
            store = ResearchJobStore(Path(directory) / "jobs.sqlite3")
            self._enqueue(store, "retry-contract")
            with self.assertRaises(JobConflictError):
                store.enqueue(
                    kind="research.replay",
                    dedupe_key="retry-contract",
                    input_hashes=[digest("dataset")],
                    resource_budget={"wall_seconds": 60, "memory_bytes": 1024},
                    lease_requeueable=False,
                    now=self.now,
                )


    def test_waiting_external_can_requeue_only_with_immutable_proof_not_run(self):
        with TemporaryDirectory() as directory:
            store = ResearchJobStore(Path(directory) / "jobs.sqlite3")
            job, _ = store.enqueue(
                kind="research.external_annotation",
                dedupe_key="prove-not-run",
                input_hashes=[digest("dataset")],
                resource_budget={"wall_seconds": 60},
                now=self.now,
            )
            claimed = store.claim("worker-a", now=self.now, lease_seconds=10)
            old_generation = int(claimed["generation"])
            store.requeue_expired(now=self.now + timedelta(seconds=11))
            waiting = store.get(job["job_id"])
            self.assertEqual(waiting["state"], "WAITING_EXTERNAL")
            self.assertEqual(int(waiting["generation"]), old_generation + 1)

            artifact_store, evidence = resolution_proof(
                directory,
                job_id=job["job_id"],
                generation=int(store.get(job["job_id"])["generation"]),
                verdict="PROVEN_NOT_RUN",
                artifact_id="11111111-1111-4111-8111-111111111111",
            )
            self.assertTrue(
                store.resolve_waiting_external(
                    job["job_id"],
                    generation=int(store.get(job["job_id"])["generation"]),
                    verdict="PROVEN_NOT_RUN",
                    evidence_ref=evidence,
                    artifact_store=artifact_store,
                    now=self.now + timedelta(seconds=12),
                )
            )
            queued = store.get(job["job_id"])
            self.assertEqual(queued["state"], "QUEUED")
            self.assertEqual(
                queued["external_resolution"]["evidence_ref"],
                evidence,
            )

            reclaimed = store.claim(
                "worker-b",
                now=self.now + timedelta(seconds=13),
                lease_seconds=30,
            )
            self.assertEqual(reclaimed["job_id"], job["job_id"])
            self.assertEqual(int(reclaimed["generation"]), old_generation + 1)
            with self.assertRaises(JobLeaseError):
                store.succeed(
                    job["job_id"],
                    worker_id="worker-a",
                    generation=old_generation,
                    output_refs=[
                        "artifact:11111111-1111-4111-8111-111111111111@sha256:"
                        + "1" * 64
                    ],
                    now=self.now + timedelta(seconds=14),
                )

    def test_waiting_external_proven_success_is_terminal_and_idempotent(self):
        with TemporaryDirectory() as directory:
            store = ResearchJobStore(Path(directory) / "jobs.sqlite3")
            job, _ = store.enqueue(
                kind="research.external_annotation",
                dedupe_key="prove-success",
                input_hashes=[digest("dataset")],
                resource_budget={"wall_seconds": 60},
                now=self.now,
            )
            store.claim("worker-a", now=self.now, lease_seconds=10)
            store.requeue_expired(now=self.now + timedelta(seconds=11))
            generation = int(store.get(job["job_id"])["generation"])
            artifact_store = ArtifactStore(
                Path(directory) / "external-resolution-artifacts"
            )
            outputs = [
                result_artifact(
                    artifact_store,
                    job_id=job["job_id"],
                    generation=generation,
                    input_hashes=[digest("dataset")],
                )
            ]
            artifact_store, evidence = resolution_proof(
                directory,
                job_id=job["job_id"],
                generation=generation,
                verdict="PROVEN_SUCCEEDED",
                artifact_id="22222222-2222-4222-8222-222222222222",
                output_refs=outputs,
            )
            resolved_at = self.now + timedelta(seconds=12)
            self.assertTrue(
                store.resolve_waiting_external(
                    job["job_id"],
                    generation=generation,
                    verdict="PROVEN_SUCCEEDED",
                    evidence_ref=evidence,
                    artifact_store=artifact_store,
                    output_refs=outputs,
                    now=resolved_at,
                )
            )
            result = store.get(job["job_id"])
            self.assertEqual(result["state"], "SUCCEEDED")
            self.assertEqual(result["output_refs"], outputs)
            self.assertFalse(
                store.resolve_waiting_external(
                    job["job_id"],
                    generation=int(store.get(job["job_id"])["generation"]),
                    verdict="PROVEN_SUCCEEDED",
                    evidence_ref=evidence,
                    output_refs=outputs,
                    now=resolved_at + timedelta(seconds=30),
                )
            )
            with self.assertRaises(JobConflictError):
                store.resolve_waiting_external(
                    job["job_id"],
                    generation=int(store.get(job["job_id"])["generation"]),
                    verdict="PROVEN_SUCCEEDED",
                    evidence_ref=evidence,
                    output_refs=[
                        "artifact:77777777-7777-4777-8777-777777777777@sha256:"
                        + "7" * 64
                    ],
                    now=resolved_at + timedelta(seconds=31),
                )
            with self.assertRaises(JobConflictError):
                store.resolve_waiting_external(
                    job["job_id"],
                    generation=int(store.get(job["job_id"])["generation"]),
                    verdict="PROVEN_SUCCEEDED",
                    evidence_ref=(
                        "artifact:22222222-2222-4222-8222-222222222222@sha256:"
                        + "c" * 64
                    ),
                    output_refs=outputs,
                    now=resolved_at,
                )

    def test_old_external_resolution_cannot_be_reused_after_new_ambiguous_attempt(self):
        with TemporaryDirectory() as directory:
            store = ResearchJobStore(Path(directory) / "jobs.sqlite3")
            job, _ = store.enqueue(
                kind="research.external_annotation",
                dedupe_key="generation-bound-resolution",
                input_hashes=[digest("dataset")],
                resource_budget={"wall_seconds": 60},
                now=self.now,
            )
            first_claim = store.claim("worker-a", now=self.now, lease_seconds=10)
            store.requeue_expired(now=self.now + timedelta(seconds=11))
            first_waiting = store.get(job["job_id"])
            first_waiting_generation = int(first_waiting["generation"])
            self.assertEqual(
                first_waiting_generation,
                int(first_claim["generation"]) + 1,
            )
            first_artifact_store, first_evidence = resolution_proof(
                directory,
                job_id=job["job_id"],
                generation=first_waiting_generation,
                verdict="PROVEN_NOT_RUN",
                artifact_id="44444444-4444-4444-8444-444444444444",
            )
            self.assertTrue(
                store.resolve_waiting_external(
                    job["job_id"],
                    generation=first_waiting_generation,
                    verdict="PROVEN_NOT_RUN",
                    evidence_ref=first_evidence,
                    artifact_store=first_artifact_store,
                    now=self.now + timedelta(seconds=12),
                )
            )

            second_claim = store.claim(
                "worker-b",
                now=self.now + timedelta(seconds=13),
                lease_seconds=10,
            )
            self.assertEqual(
                int(second_claim["generation"]),
                first_waiting_generation,
            )
            store.requeue_expired(now=self.now + timedelta(seconds=24))
            second_waiting = store.get(job["job_id"])
            second_waiting_generation = int(second_waiting["generation"])
            self.assertEqual(
                second_waiting_generation,
                first_waiting_generation + 1,
            )

            with self.assertRaisesRegex(JobLeaseError, "generation is stale"):
                store.resolve_waiting_external(
                    job["job_id"],
                    generation=first_waiting_generation,
                    verdict="PROVEN_NOT_RUN",
                    evidence_ref=first_evidence,
                    now=self.now + timedelta(seconds=25),
                )
            still_waiting = store.get(job["job_id"])
            self.assertEqual(still_waiting["state"], "WAITING_EXTERNAL")
            self.assertEqual(
                int(still_waiting["generation"]),
                second_waiting_generation,
            )

            second_artifact_store, second_evidence = resolution_proof(
                directory,
                job_id=job["job_id"],
                generation=second_waiting_generation,
                verdict="PROVEN_NOT_RUN",
                artifact_id="55555555-5555-4555-8555-555555555555",
            )
            self.assertTrue(
                store.resolve_waiting_external(
                    job["job_id"],
                    generation=second_waiting_generation,
                    verdict="PROVEN_NOT_RUN",
                    evidence_ref=second_evidence,
                    artifact_store=second_artifact_store,
                    now=self.now + timedelta(seconds=26),
                )
            )

    def test_valid_looking_self_asserted_resolution_ref_cannot_requeue(self):
        with TemporaryDirectory() as directory:
            store = ResearchJobStore(Path(directory) / "jobs.sqlite3")
            job, _ = store.enqueue(
                kind="research.external_annotation",
                dedupe_key="self-asserted-proof",
                input_hashes=[digest("dataset")],
                resource_budget={"wall_seconds": 60},
                now=self.now,
            )
            store.claim("worker-a", now=self.now, lease_seconds=10)
            store.requeue_expired(now=self.now + timedelta(seconds=11))
            fake = (
                "artifact:66666666-6666-4666-8666-666666666666@sha256:"
                + "a" * 64
            )
            with self.assertRaisesRegex(
                JobConflictError,
                "matching immutable artifact evidence",
            ):
                store.resolve_waiting_external(
                    job["job_id"],
                    generation=int(store.get(job["job_id"])["generation"]),
                    verdict="PROVEN_NOT_RUN",
                    evidence_ref=fake,
                    now=self.now + timedelta(seconds=12),
                )
            self.assertEqual(store.get(job["job_id"])["state"], "WAITING_EXTERNAL")

    def test_resolution_artifact_must_bind_exact_job_generation_and_verdict(self):
        with TemporaryDirectory() as directory:
            store = ResearchJobStore(Path(directory) / "jobs.sqlite3")
            job, _ = store.enqueue(
                kind="research.external_annotation",
                dedupe_key="wrong-proof-semantics",
                input_hashes=[digest("dataset")],
                resource_budget={"wall_seconds": 60},
                now=self.now,
            )
            store.claim("worker-a", now=self.now, lease_seconds=10)
            store.requeue_expired(now=self.now + timedelta(seconds=11))
            generation = int(store.get(job["job_id"])["generation"])
            artifact_store, evidence_ref = resolution_proof(
                directory,
                job_id=job["job_id"],
                generation=generation + 1,
                verdict="PROVEN_NOT_RUN",
            )
            with self.assertRaisesRegex(
                JobConflictError,
                "matching immutable artifact evidence",
            ):
                store.resolve_waiting_external(
                    job["job_id"],
                    generation=generation,
                    verdict="PROVEN_NOT_RUN",
                    evidence_ref=evidence_ref,
                    artifact_store=artifact_store,
                    now=self.now + timedelta(seconds=12),
                )
            self.assertEqual(store.get(job["job_id"])["state"], "WAITING_EXTERNAL")

    def test_external_success_rejects_nonexistent_output_artifact(self):
        with TemporaryDirectory() as directory:
            store = ResearchJobStore(Path(directory) / "jobs.sqlite3")
            job, _ = store.enqueue(
                kind="research.external_annotation",
                dedupe_key="missing-output-artifact",
                input_hashes=[digest("dataset")],
                resource_budget={"wall_seconds": 60},
                now=self.now,
            )
            store.claim("worker-a", now=self.now, lease_seconds=10)
            store.requeue_expired(now=self.now + timedelta(seconds=11))
            generation = int(store.get(job["job_id"])["generation"])
            nonexistent_output = (
                "artifact:88888888-8888-4888-8888-888888888888@sha256:"
                + "8" * 64
            )
            artifact_store, evidence_ref = resolution_proof(
                directory,
                job_id=job["job_id"],
                generation=generation,
                verdict="PROVEN_SUCCEEDED",
                output_refs=[nonexistent_output],
            )
            with self.assertRaisesRegex(
                JobConflictError,
                "verified immutable output artifacts",
            ):
                store.resolve_waiting_external(
                    job["job_id"],
                    generation=generation,
                    verdict="PROVEN_SUCCEEDED",
                    evidence_ref=evidence_ref,
                    artifact_store=artifact_store,
                    output_refs=[nonexistent_output],
                    now=self.now + timedelta(seconds=12),
                )
            self.assertEqual(store.get(job["job_id"])["state"], "WAITING_EXTERNAL")

    def test_success_proof_cannot_be_rebound_to_different_outputs(self):
        with TemporaryDirectory() as directory:
            store = ResearchJobStore(Path(directory) / "jobs.sqlite3")
            job, _ = store.enqueue(
                kind="research.external_annotation",
                dedupe_key="proof-output-binding",
                input_hashes=[digest("dataset")],
                resource_budget={"wall_seconds": 60},
                now=self.now,
            )
            store.claim("worker-a", now=self.now, lease_seconds=10)
            store.requeue_expired(now=self.now + timedelta(seconds=11))
            generation = int(store.get(job["job_id"])["generation"])
            artifacts = ArtifactStore(
                Path(directory) / "external-resolution-artifacts"
            )
            output_a = result_artifact(
                artifacts,
                job_id=job["job_id"],
                generation=generation,
                input_hashes=[digest("dataset")],
                data=b"output-a",
            )
            output_b = result_artifact(
                artifacts,
                job_id=job["job_id"],
                generation=generation,
                input_hashes=[digest("dataset")],
                data=b"output-b",
            )
            artifacts, evidence = resolution_proof(
                directory,
                job_id=job["job_id"],
                generation=generation,
                verdict="PROVEN_SUCCEEDED",
                output_refs=[output_a],
            )
            with self.assertRaisesRegex(
                JobConflictError,
                "matching immutable artifact evidence",
            ):
                store.resolve_waiting_external(
                    job["job_id"],
                    generation=generation,
                    verdict="PROVEN_SUCCEEDED",
                    evidence_ref=evidence,
                    artifact_store=artifacts,
                    output_refs=[output_b],
                    now=self.now + timedelta(seconds=12),
                )
            self.assertEqual(store.get(job["job_id"])["state"], "WAITING_EXTERNAL")

    def test_success_outputs_are_canonicalized_and_duplicates_rejected(self):
        with TemporaryDirectory() as directory:
            store = ResearchJobStore(Path(directory) / "jobs.sqlite3")
            job, _ = store.enqueue(
                kind="research.external_annotation",
                dedupe_key="canonical-success-outputs",
                input_hashes=[digest("dataset")],
                resource_budget={"wall_seconds": 60},
                now=self.now,
            )
            store.claim("worker-a", now=self.now, lease_seconds=10)
            store.requeue_expired(now=self.now + timedelta(seconds=11))
            generation = int(store.get(job["job_id"])["generation"])
            artifacts = ArtifactStore(
                Path(directory) / "external-resolution-artifacts"
            )
            output_a = result_artifact(
                artifacts,
                job_id=job["job_id"],
                generation=generation,
                input_hashes=[digest("dataset")],
                data=b"a",
            )
            output_b = result_artifact(
                artifacts,
                job_id=job["job_id"],
                generation=generation,
                input_hashes=[digest("dataset")],
                data=b"b",
            )
            artifacts, evidence = resolution_proof(
                directory,
                job_id=job["job_id"],
                generation=generation,
                verdict="PROVEN_SUCCEEDED",
                output_refs=[output_a, output_b],
            )
            self.assertTrue(
                store.resolve_waiting_external(
                    job["job_id"],
                    generation=generation,
                    verdict="PROVEN_SUCCEEDED",
                    evidence_ref=evidence,
                    artifact_store=artifacts,
                    output_refs=[output_b, output_a],
                    now=self.now + timedelta(seconds=12),
                )
            )
            self.assertEqual(
                store.get(job["job_id"])["output_refs"],
                sorted([output_a, output_b]),
            )

            other, _ = store.enqueue(
                kind="research.external_annotation",
                dedupe_key="duplicate-success-outputs",
                input_hashes=[digest("dataset-2")],
                resource_budget={"wall_seconds": 60},
                now=self.now,
            )
            store.claim("worker-b", now=self.now, lease_seconds=10)
            store.requeue_expired(now=self.now + timedelta(seconds=11))
            with self.assertRaisesRegex(ValueError, "must not contain duplicates"):
                store.resolve_waiting_external(
                    other["job_id"],
                    generation=int(store.get(other["job_id"])["generation"]),
                    verdict="PROVEN_SUCCEEDED",
                    evidence_ref=evidence,
                    artifact_store=artifacts,
                    output_refs=[output_a, output_a],
                    now=self.now + timedelta(seconds=12),
                )

    def test_waiting_external_resolution_rejects_weak_evidence_and_output_mismatch(self):
        with TemporaryDirectory() as directory:
            store = ResearchJobStore(Path(directory) / "jobs.sqlite3")
            job, _ = store.enqueue(
                kind="research.external_annotation",
                dedupe_key="bad-resolution",
                input_hashes=[digest("dataset")],
                resource_budget={"wall_seconds": 60},
                now=self.now,
            )
            store.claim("worker-a", now=self.now, lease_seconds=10)
            store.requeue_expired(now=self.now + timedelta(seconds=11))

            with self.assertRaisesRegex(ValueError, "immutable artifact"):
                store.resolve_waiting_external(
                    job["job_id"],
                    generation=int(store.get(job["job_id"])["generation"]),
                    verdict="PROVEN_NOT_RUN",
                    evidence_ref="ticket-123",
                    now=self.now + timedelta(seconds=12),
                )
            evidence = (
                "artifact:33333333-3333-4333-8333-333333333333@sha256:"
                + "d" * 64
            )
            with self.assertRaisesRegex(ValueError, "requires output_refs"):
                store.resolve_waiting_external(
                    job["job_id"],
                    generation=int(store.get(job["job_id"])["generation"]),
                    verdict="PROVEN_SUCCEEDED",
                    evidence_ref=evidence,
                    now=self.now + timedelta(seconds=12),
                )
            with self.assertRaisesRegex(ValueError, "only for PROVEN_SUCCEEDED"):
                store.resolve_waiting_external(
                    job["job_id"],
                    generation=int(store.get(job["job_id"])["generation"]),
                    verdict="PROVEN_FAILED",
                    evidence_ref=evidence,
                    output_refs=["unexpected"],
                    now=self.now + timedelta(seconds=12),
                )

    def test_cancellation_blocks_late_publication(self):
        with TemporaryDirectory() as directory:
            store = ResearchJobStore(Path(directory) / "jobs.sqlite3")
            job, _ = self._enqueue(store)
            claimed = store.claim("worker-a", now=self.now, lease_seconds=30)
            self.assertTrue(store.cancel(job["job_id"], now=self.now + timedelta(seconds=1)))
            with self.assertRaises(JobLeaseError):
                store.succeed(
                    job["job_id"],
                    worker_id="worker-a",
                    generation=int(claimed["generation"]),
                    output_refs=[
                        "artifact:11111111-1111-4111-8111-111111111111@sha256:"
                        + "1" * 64
                    ],
                    now=self.now + timedelta(seconds=2),
                )
            self.assertEqual(store.get(job["job_id"])["state"], "CANCELLED")

    def test_checkpoint_enforces_declared_resource_budget(self):
        with TemporaryDirectory() as directory:
            store = ResearchJobStore(Path(directory) / "jobs.sqlite3")
            job, _ = self._enqueue(store)
            claimed = store.claim("worker-a", now=self.now, lease_seconds=30)
            updated = store.checkpoint(
                job["job_id"],
                worker_id="worker-a",
                generation=int(claimed["generation"]),
                checkpoint_ref=checkpoint_ref("11111111-1111-4111-8111-111111111111", "checkpoint-1"),
                resource_usage={"wall_seconds": 20, "memory_bytes": 512},
                now=self.now + timedelta(seconds=1),
            )
            self.assertEqual(
                updated["checkpoint_ref"],
                checkpoint_ref("11111111-1111-4111-8111-111111111111", "checkpoint-1"),
            )
            with self.assertRaises(JobBudgetError):
                store.checkpoint(
                    job["job_id"],
                    worker_id="worker-a",
                    generation=int(claimed["generation"]),
                    checkpoint_ref=checkpoint_ref("22222222-2222-4222-8222-222222222222", "checkpoint-2"),
                    resource_usage={"wall_seconds": 61, "memory_bytes": 512},
                    now=self.now + timedelta(seconds=2),
                )

    def test_checkpoint_rejects_mutable_reference_before_journal_mutation(self):
        with TemporaryDirectory() as directory:
            store = ResearchJobStore(Path(directory) / "jobs.sqlite3")
            job, _ = self._enqueue(store, "immutable-checkpoint")
            claimed = store.claim("worker-a", now=self.now, lease_seconds=30)
            before = store.get(job["job_id"])
            with self.assertRaisesRegex(ValueError, "immutable artifact"):
                store.checkpoint(
                    job["job_id"],
                    worker_id="worker-a",
                    generation=int(claimed["generation"]),
                    checkpoint_ref="artifact:mutable-checkpoint",
                    resource_usage={"wall_seconds": 1},
                    now=self.now + timedelta(seconds=1),
                )
            after = store.get(job["job_id"])
            self.assertNotIn("checkpoint_ref", after)
            self.assertEqual(after["resource_usage"], before["resource_usage"])

    def test_checkpoint_resource_usage_is_monotonic_and_sparse_updates_preserve_totals(self):
        with TemporaryDirectory() as directory:
            store = ResearchJobStore(Path(directory) / "jobs.sqlite3")
            job, _ = self._enqueue(store, "monotonic-usage")
            claimed = store.claim("worker-a", now=self.now, lease_seconds=30)
            generation = int(claimed["generation"])

            first = store.checkpoint(
                job["job_id"],
                worker_id="worker-a",
                generation=generation,
                checkpoint_ref=checkpoint_ref("11111111-1111-4111-8111-111111111111", "checkpoint-1"),
                resource_usage={"wall_seconds": 20, "memory_bytes": 512},
                now=self.now + timedelta(seconds=1),
            )
            self.assertEqual(first["resource_usage"]["wall_seconds"], 20.0)
            self.assertEqual(first["resource_usage"]["memory_bytes"], 512.0)

            second = store.checkpoint(
                job["job_id"],
                worker_id="worker-a",
                generation=generation,
                checkpoint_ref=checkpoint_ref("22222222-2222-4222-8222-222222222222", "checkpoint-2"),
                resource_usage={"memory_bytes": 768},
                now=self.now + timedelta(seconds=2),
            )
            self.assertEqual(second["resource_usage"]["wall_seconds"], 20.0)
            self.assertEqual(second["resource_usage"]["memory_bytes"], 768.0)

            with self.assertRaises(JobBudgetError):
                store.checkpoint(
                    job["job_id"],
                    worker_id="worker-a",
                    generation=generation,
                    checkpoint_ref=checkpoint_ref("33333333-3333-4333-8333-333333333333", "checkpoint-regression"),
                    resource_usage={"wall_seconds": 19},
                    now=self.now + timedelta(seconds=3),
                )
            current = store.get(job["job_id"])
            self.assertEqual(current["resource_usage"]["wall_seconds"], 20.0)
            self.assertEqual(current["resource_usage"]["memory_bytes"], 768.0)

    def test_only_one_result_is_accepted_for_a_generation(self):
        with TemporaryDirectory() as directory:
            store = ResearchJobStore(Path(directory) / "jobs.sqlite3")
            job, _ = self._enqueue(store)
            claimed = store.claim("worker-a", now=self.now, lease_seconds=30)
            generation = int(claimed["generation"])
            artifacts = ArtifactStore(Path(directory) / "results")
            accepted_ref = result_artifact(
                artifacts,
                job_id=job["job_id"],
                generation=generation,
                input_hashes=[digest("dataset")],
                artifact_id="11111111-1111-4111-8111-111111111111",
            )
            conflicting_ref = result_artifact(
                artifacts,
                job_id=job["job_id"],
                generation=generation,
                input_hashes=[digest("dataset")],
                data=b"different-result",
                artifact_id="22222222-2222-4222-8222-222222222222",
            )
            self.assertTrue(
                store.succeed(
                    job["job_id"],
                    worker_id="worker-a",
                    generation=generation,
                    output_refs=[accepted_ref],
                    artifact_store=artifacts,
                    now=self.now + timedelta(seconds=1),
                )
            )
            self.assertFalse(
                store.succeed(
                    job["job_id"],
                    worker_id="worker-a",
                    generation=generation,
                    output_refs=[accepted_ref],
                    artifact_store=artifacts,
                    now=self.now + timedelta(seconds=2),
                )
            )
            with self.assertRaises(JobConflictError):
                store.succeed(
                    job["job_id"],
                    worker_id="worker-a",
                    generation=generation,
                    output_refs=[conflicting_ref],
                    artifact_store=artifacts,
                    now=self.now + timedelta(seconds=3),
                )

    def test_direct_succeed_requires_canonical_immutable_unique_artifact_refs(self):
        with TemporaryDirectory() as directory:
            store = ResearchJobStore(Path(directory) / "jobs.sqlite3")
            job, _ = self._enqueue(store, "canonical-direct-success")
            claimed = store.claim("worker-a", now=self.now, lease_seconds=30)
            generation = int(claimed["generation"])

            with self.assertRaisesRegex(ValueError, "immutable artifact"):
                store.succeed(
                    job["job_id"],
                    worker_id="worker-a",
                    generation=generation,
                    output_refs=["artifact:mutable-name"],
                    now=self.now + timedelta(seconds=1),
                )
            duplicate = (
                "artifact:33333333-3333-4333-8333-333333333333@sha256:"
                + "3" * 64
            )
            with self.assertRaisesRegex(ValueError, "must not contain duplicates"):
                store.succeed(
                    job["job_id"],
                    worker_id="worker-a",
                    generation=generation,
                    output_refs=[duplicate, duplicate],
                    now=self.now + timedelta(seconds=1),
                )

    def test_direct_succeed_canonicalizes_result_order_for_idempotent_retry(self):
        with TemporaryDirectory() as directory:
            store = ResearchJobStore(Path(directory) / "jobs.sqlite3")
            job, _ = self._enqueue(store, "canonical-direct-order")
            claimed = store.claim("worker-a", now=self.now, lease_seconds=30)
            generation = int(claimed["generation"])
            artifacts = ArtifactStore(Path(directory) / "ordered-results")
            first = result_artifact(
                artifacts,
                job_id=job["job_id"],
                generation=generation,
                input_hashes=[digest("dataset")],
                data=b"first",
                artifact_id="44444444-4444-4444-8444-444444444444",
            )
            second = result_artifact(
                artifacts,
                job_id=job["job_id"],
                generation=generation,
                input_hashes=[digest("dataset")],
                data=b"second",
                artifact_id="55555555-5555-4555-8555-555555555555",
            )
            self.assertTrue(
                store.succeed(
                    job["job_id"],
                    worker_id="worker-a",
                    generation=generation,
                    output_refs=[second, first],
                    artifact_store=artifacts,
                    now=self.now + timedelta(seconds=1),
                )
            )
            self.assertEqual(store.get(job["job_id"])["output_refs"], [first, second])
            self.assertFalse(
                store.succeed(
                    job["job_id"],
                    worker_id="worker-a",
                    generation=generation,
                    output_refs=[first, second],
                    artifact_store=artifacts,
                    now=self.now + timedelta(seconds=2),
                )
            )

    def test_financial_send_cannot_be_queued_as_generic_job(self):
        with TemporaryDirectory() as directory:
            store = ResearchJobStore(Path(directory) / "jobs.sqlite3")
            with self.assertRaises(ValueError):
                store.enqueue(
                    kind="financial.order_send",
                    dedupe_key="forbidden",
                    input_hashes=[digest("order")],
                    resource_budget={"wall_seconds": 1},
                    now=self.now,
                )

    def test_records_survive_reopen(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "jobs.sqlite3"
            first_store = ResearchJobStore(path)
            job, _ = self._enqueue(first_store, "persisted")
            second_store = ResearchJobStore(path)
            self.assertEqual(second_store.get(job["job_id"])["dedupe_key"], "persisted")

    def test_crash_after_artifact_publish_retries_without_double_acceptance(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            jobs = ResearchJobStore(root / "jobs.sqlite3")
            artifacts = ArtifactStore(root / "artifacts")
            job, _ = self._enqueue(jobs, "artifact-crash")
            claimed = jobs.claim("worker-a", now=self.now, lease_seconds=30)
            generation = int(claimed["generation"])
            rights = {"storage": True, "export": False}

            with patch.object(jobs, "succeed", side_effect=RuntimeError("simulated crash")):
                with self.assertRaises(RuntimeError):
                    jobs.publish_result_bytes(
                        job["job_id"],
                        worker_id="worker-a",
                        generation=generation,
                        artifact_store=artifacts,
                        data=b"candidate-result",
                        media_type="application/octet-stream",
                        rights=rights,
                        now=self.now + timedelta(seconds=1),
                    )

            self.assertEqual(jobs.get(job["job_id"])["state"], "RUNNING")
            self.assertEqual(len(list((root / "artifacts" / "manifests").glob("*.json"))), 1)

            manifest, accepted = jobs.publish_result_bytes(
                job["job_id"],
                worker_id="worker-a",
                generation=generation,
                artifact_store=artifacts,
                data=b"candidate-result",
                media_type="application/octet-stream",
                rights=rights,
                now=self.now + timedelta(seconds=2),
            )
            self.assertTrue(accepted)
            final = jobs.get(job["job_id"])
            self.assertEqual(final["state"], "SUCCEEDED")
            self.assertEqual(len(final["output_refs"]), 1)
            self.assertIn(manifest["artifact_id"], final["output_refs"][0])
            self.assertEqual(len(list((root / "artifacts" / "manifests").glob("*.json"))), 1)

    def test_different_result_bytes_cannot_replace_crash_published_artifact(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            jobs = ResearchJobStore(root / "jobs.sqlite3")
            artifacts = ArtifactStore(root / "artifacts")
            job, _ = self._enqueue(jobs, "artifact-conflict")
            claimed = jobs.claim("worker-a", now=self.now, lease_seconds=30)
            generation = int(claimed["generation"])
            rights = {"storage": True, "export": False}

            with patch.object(jobs, "succeed", side_effect=RuntimeError("simulated crash")):
                with self.assertRaises(RuntimeError):
                    jobs.publish_result_bytes(
                        job["job_id"],
                        worker_id="worker-a",
                        generation=generation,
                        artifact_store=artifacts,
                        data=b"first-result",
                        media_type="application/octet-stream",
                        rights=rights,
                        now=self.now + timedelta(seconds=1),
                    )

            with self.assertRaises(ValueError):
                jobs.publish_result_bytes(
                    job["job_id"],
                    worker_id="worker-a",
                    generation=generation,
                    artifact_store=artifacts,
                    data=b"different-result",
                    media_type="application/octet-stream",
                    rights=rights,
                    now=self.now + timedelta(seconds=2),
                )
            self.assertEqual(jobs.get(job["job_id"])["state"], "RUNNING")



if __name__ == "__main__":
    unittest.main()
