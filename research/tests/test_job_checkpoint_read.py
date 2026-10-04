"""Authenticated state restoration through the canonical research job store."""
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
from autotrade_research.jobs import ResearchJobStore, JobConflictError, JobLeaseError
from autotrade_research.evaluation.replay.feeder import CausalDataset, CausalEvent, CausalFeeder, FeederCheckpoint

NOW = datetime(2026, 10, 3, 19, tzinfo=timezone.utc)
INPUTS = ["sha256:" + "a" * 64, "sha256:" + "b" * 64]


class JobCheckpointReadTests(unittest.TestCase):
    def fixture(self, root, *, input_hashes=INPUTS):
        path=Path(root)/"jobs.sqlite3"; artifacts=ArtifactStore(Path(root)/"artifacts")
        jobs=ResearchJobStore(path, authoritative_artifact_root=artifacts.root)
        job,_=jobs.enqueue(kind="research.replay",dedupe_key="replay",input_hashes=input_hashes,
                           resource_budget={"wall_seconds":60},lease_requeueable=True,now=NOW)
        claimed=jobs.claim("worker-a",now=NOW,lease_seconds=30)
        return path,artifacts,jobs,job,int(claimed["generation"])

    def publish(self, jobs, artifacts, job, generation, data=b"frozen-state"):
        return jobs.publish_checkpoint_bytes(job["job_id"],worker_id="worker-a",generation=generation,
            artifact_store=artifacts,data=data,media_type="application/json",
            rights={"storage":True,"export":False},resource_usage={"wall_seconds":1},now=NOW)

    def test_reopened_resumed_worker_reads_prior_generation_frozen_checkpoint(self):
        with TemporaryDirectory() as root:
            path,artifacts,jobs,job,generation=self.fixture(root)
            self.assertIsNone(jobs.read_checkpoint_bytes(job["job_id"],worker_id="worker-a",generation=generation,now=NOW))
            manifest,_=self.publish(jobs,artifacts,job,generation)
            jobs.pause(job["job_id"],now=NOW); jobs.resume(job["job_id"],now=NOW)
            reopened=ResearchJobStore(path,authoritative_artifact_root=artifacts.root)
            next_job=reopened.claim("worker-b",now=NOW)
            next_generation=int(next_job["generation"])
            self.assertGreater(next_generation,generation)
            with self.assertRaises(JobLeaseError):
                jobs.read_checkpoint_bytes(job["job_id"],worker_id="worker-a",generation=generation,now=NOW)
            read_manifest,data=reopened.read_checkpoint_bytes(job["job_id"],worker_id="worker-b",generation=next_generation,now=NOW)
            self.assertEqual(data,b"frozen-state");self.assertEqual(read_manifest,manifest)
            read_manifest["metadata"]["job_id"]="caller-mutated"
            self.assertEqual(reopened.read_checkpoint_bytes(job["job_id"],worker_id="worker-b",generation=next_generation,now=NOW)[0],manifest)

    def test_corrupt_checkpoint_bytes_are_not_returned_or_reenrolled(self):
        with TemporaryDirectory() as root:
            path,artifacts,jobs,job,generation=self.fixture(root)
            manifest,_=self.publish(jobs,artifacts,job,generation)
            before=jobs.get(job["job_id"])
            digest=manifest["sha256"].removeprefix("sha256:")
            (artifacts.objects/digest[:2]/digest).write_bytes(b"corrupt")
            with self.assertRaisesRegex(JobConflictError,"absent or corrupt"):
                jobs.read_checkpoint_bytes(job["job_id"],worker_id="worker-a",generation=generation,now=NOW)
            self.assertEqual(jobs.get(job["job_id"]),before)

    def test_foreign_future_boolean_or_changed_input_checkpoint_cannot_restore(self):
        for change in ("job", "future", "boolean", "inputs", "kind"):
            with self.subTest(change=change),TemporaryDirectory() as root:
                path,artifacts,jobs,job,generation=self.fixture(root)
                metadata={"artifact_kind":"RESEARCH_JOB_CHECKPOINT","job_id":job["job_id"],"job_generation":generation,"job_kind":"research.replay"}
                refs=INPUTS
                if change=="job":metadata["job_id"]=str(uuid4())
                if change=="future":metadata["job_generation"]=generation+1
                if change=="boolean":metadata["job_generation"]=True
                if change=="inputs":refs=["sha256:"+"c"*64]
                if change=="kind":metadata["job_kind"]="research.other"
                manifest=artifacts.publish_bytes(artifact_id=str(uuid4()),data=b"foreign-state",
                    media_type="application/json",rights={"storage":True,"export":False},source_refs=refs,metadata=metadata)
                ref=f"artifact:{manifest['artifact_id']}@{manifest['sha256']}"
                # Integrity-valid artifact plus corrupted job pointer must still
                # fail the semantic binding before a payload reaches execution.
                with sqlite3.connect(path) as connection:
                    connection.execute("UPDATE jobs SET checkpoint_ref=? WHERE job_id=?",(ref,job["job_id"]))
                before=jobs.get(job["job_id"])
                with self.assertRaisesRegex(JobConflictError,"enrolled job and inputs"):
                    jobs.read_checkpoint_bytes(job["job_id"],worker_id="worker-a",generation=generation,now=NOW)
                self.assertEqual(jobs.get(job["job_id"]),before)

    def test_cancelled_wrong_owner_expired_or_bool_generation_never_reads_artifact(self):
        with TemporaryDirectory() as root:
            path,artifacts,jobs,job,generation=self.fixture(root)
            self.publish(jobs,artifacts,job,generation)
            with patch.object(jobs,"_require_artifact_reader",side_effect=AssertionError("must validate lease first")):
                with self.assertRaises(JobLeaseError):
                    jobs.read_checkpoint_bytes(job["job_id"],worker_id="wrong",generation=generation,now=NOW)
                with self.assertRaises(JobLeaseError):
                    jobs.read_checkpoint_bytes(job["job_id"],worker_id="worker-a",generation=generation,now=NOW+timedelta(seconds=31))
                with self.assertRaisesRegex(ValueError,"positive exact integer"):
                    jobs.read_checkpoint_bytes(job["job_id"],worker_id="worker-a",generation=True,now=NOW)
                jobs.cancel(job["job_id"],now=NOW)
                with self.assertRaises(JobLeaseError):
                    jobs.read_checkpoint_bytes(job["job_id"],worker_id="worker-a",generation=generation,now=NOW)

    def test_reader_uses_retained_canonical_methods_before_any_worker_override(self):
        with TemporaryDirectory() as root:
            path,artifacts,jobs,job,generation=self.fixture(root)
            self.publish(jobs,artifacts,job,generation)
            def hostile(*args,**kwargs):raise AssertionError("worker override must not run")
            jobs._require_artifact_reader=hostile
            jobs._require_live_lease=hostile
            jobs._connect=hostile
            manifest,payload=ResearchJobStore.read_checkpoint_bytes(jobs,job["job_id"],worker_id="worker-a",generation=generation,now=NOW)
            self.assertEqual(payload,b"frozen-state")
            self.assertEqual(manifest["metadata"]["job_id"],job["job_id"])
        class HostileStore(ResearchJobStore):
            def __getattribute__(self,name):raise AssertionError("virtual store read")
        with self.assertRaisesRegex(TypeError,"exact ResearchJobStore"):
            ResearchJobStore.read_checkpoint_bytes(object.__new__(HostileStore),str(uuid4()),worker_id="worker",generation=1,now=NOW)

    def test_boolean_artifact_generation_cannot_be_enrolled_as_generation_one(self):
        with TemporaryDirectory() as root:
            path,artifacts,jobs,job,generation=self.fixture(root)
            manifest=artifacts.publish_bytes(artifact_id=str(uuid4()),data=b"boolean-generation",
                media_type="application/json",rights={"storage":True,"export":False},source_refs=INPUTS,
                metadata={"artifact_kind":"RESEARCH_JOB_CHECKPOINT","job_id":job["job_id"],"job_generation":True})
            with self.assertRaises(JobConflictError):
                jobs.checkpoint(job["job_id"],worker_id="worker-a",generation=generation,
                    checkpoint_ref=f"artifact:{manifest['artifact_id']}@{manifest['sha256']}",resource_usage={"wall_seconds":1},now=NOW)
            self.assertNotIn("checkpoint_ref",jobs.get(job["job_id"]))

    def test_research_feeder_checkpoint_job_restart_and_publication_equal_uninterrupted(self):
        def event(identifier,time):
            return CausalEvent.create(event_id=identifier,kind="TRADE",event_time=time,available_at=time,
                ingested_at=time,source_priority=0,source_sequence=0,payload={"price":identifier})
        dataset=CausalDataset.create(manifest_sha256=INPUTS[0],events=[
            event("100","2026-10-03T19:01:00Z"),event("101","2026-10-03T19:02:00Z"),event("102","2026-10-03T19:03:00Z")])
        source_hashes=[dataset.dataset_sha256,INPUTS[0],INPUTS[1]]
        outputs=[]
        for restart in (False,True):
            with self.subTest(restart=restart),TemporaryDirectory() as root:
                path,artifacts,jobs,job,generation=self.fixture(root,input_hashes=source_hashes)
                feeder=CausalFeeder(dataset,start_time=NOW)
                feeder.advance_to("2026-10-03T19:02:00Z")
                data=json.dumps(feeder.checkpoint().to_record(),sort_keys=True,separators=(",",":")).encode()
                self.publish(jobs,artifacts,job,generation,data)
                worker="worker-a"
                if restart:
                    jobs.pause(job["job_id"],now=NOW);jobs.resume(job["job_id"],now=NOW)
                    jobs=ResearchJobStore(path,authoritative_artifact_root=artifacts.root)
                    worker="worker-b";generation=int(jobs.claim(worker,now=NOW)["generation"])
                    manifest,payload=jobs.read_checkpoint_bytes(job["job_id"],worker_id=worker,generation=generation,now=NOW)
                    self.assertEqual(manifest["source_refs"],source_hashes)
                    feeder=CausalFeeder.restore(dataset=dataset,checkpoint=FeederCheckpoint.from_record(json.loads(payload)))
                feeder.advance_to("2026-10-03T19:03:00Z")
                output=json.dumps(feeder.checkpoint().to_record(),sort_keys=True,separators=(",",":")).encode()
                manifest,accepted=jobs.publish_result_bytes(job["job_id"],worker_id=worker,generation=generation,
                    artifact_store=artifacts,data=output,media_type="application/json",rights={"storage":True,"export":False},now=NOW)
                outputs.append((output,manifest["sha256"]))
                self.assertTrue(accepted)
                finished=jobs.get(job["job_id"])
                self.assertEqual(finished["state"],"SUCCEEDED")
                self.assertEqual(manifest["source_refs"],source_hashes)
                self.assertEqual(finished["output_refs"],[f"artifact:{manifest['artifact_id']}@{manifest['sha256']}"])
        self.assertEqual(outputs[0],outputs[1])


if __name__=="__main__":unittest.main()
