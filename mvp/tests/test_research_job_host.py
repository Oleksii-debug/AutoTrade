from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.research_job_host import ResearchJobHostService
from mvp.autotrade_mvp.security import SecurityBoundary
from mvp.autotrade_mvp.windows_secrets import ProtectedCredentialVault
from research.autotrade_research.jobs import ResearchJobStore


ORIGIN = "https://local.autotrade.invalid"


class DeterministicProtector:
    PREFIX = b"research-job-host-test-v1:"

    def protect(self, plaintext: bytes, *, entropy: bytes) -> bytes:
        return self.PREFIX + sha256(entropy).digest() + plaintext[::-1]

    def unprotect(self, ciphertext: bytes, *, entropy: bytes) -> bytes:
        expected = self.PREFIX + sha256(entropy).digest()
        if not ciphertext.startswith(expected):
            raise OSError("scope entropy mismatch")
        return ciphertext[len(expected):][::-1]


def digest(label: str) -> str:
    return "sha256:" + sha256(label.encode("utf-8")).hexdigest()


class ResearchJobHostServiceTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        vault = ProtectedCredentialVault(
            root / "credentials.json",
            protector=DeterministicProtector(),
        )
        self.security = SecurityBoundary(
            allowed_origins={ORIGIN},
            credential_vault=vault,
            session_authorizer=lambda subject, role, origin: True,
        )
        self.owner = self.security.create_session(
            subject="owner",
            role="OWNER",
            origin=ORIGIN,
        )
        self.researcher = self.security.create_session(
            subject="researcher",
            role="RESEARCHER",
            origin=ORIGIN,
        )
        self.observer = self.security.create_session(
            subject="observer",
            role="OBSERVER",
            origin=ORIGIN,
        )
        self.jobs = ResearchJobStore(root / "jobs.sqlite3")
        self.origin = [ORIGIN]
        self.host = ResearchJobHostService(
            jobs=self.jobs,
            security=self.security,
            request_origin_provider=lambda: self.origin[0],
        )

    def enqueue(self):
        return self.host.enqueue(
            session=self.researcher.token,
            actor="researcher",
            kind="research.replay",
            dedupe_key="replay-1",
            input_hashes=[digest("dataset")],
            resource_budget={"wall_seconds": 60, "memory_bytes": 1024},
            lease_requeueable=True,
        )

    def test_researcher_can_enqueue_durable_research_job_and_observer_can_read(self):
        (created, inserted) = self.enqueue()
        self.assertTrue(inserted)
        self.assertEqual(created["kind"], "research.replay")
        observed = self.host.get(
            created["job_id"],
            session=self.observer.token,
            actor="observer",
        )
        self.assertEqual(observed["job_id"], created["job_id"])
        self.assertEqual(observed["state"], "QUEUED")

        reopened = ResearchJobStore(Path(self.directory.name) / "jobs.sqlite3")
        self.assertEqual(reopened.get(created["job_id"])["dedupe_key"], "replay-1")

    def test_host_service_does_not_turn_generic_jobs_into_financial_send_authority(self):
        with self.assertRaisesRegex(ValueError, "research"):
            self.host.enqueue(
                session=self.owner.token,
                actor="owner",
                kind="financial.order_send",
                dedupe_key="forbidden",
                input_hashes=[digest("order")],
                resource_budget={"wall_seconds": 1},
            )
        self.assertEqual(
            self.jobs.claim("worker", lease_seconds=1),
            None,
        )

    def test_authenticated_actor_must_match_session_subject(self):
        with self.assertRaisesRegex(PermissionError, "subject"):
            self.host.enqueue(
                session=self.researcher.token,
                actor="owner",
                kind="research.replay",
                dedupe_key="forged-actor",
                input_hashes=[digest("dataset")],
                resource_budget={"wall_seconds": 60},
            )

    def test_researcher_cannot_cancel_foreign_or_unowned_job_until_ownership_is_durable(self):
        created, _ = self.enqueue()
        with self.assertRaises(PermissionError):
            self.host.cancel(
                created["job_id"],
                session=self.researcher.token,
                actor="researcher",
            )
        self.assertEqual(self.jobs.get(created["job_id"])["state"], "QUEUED")

        self.assertTrue(
            self.host.cancel(
                created["job_id"],
                session=self.owner.token,
                actor="owner",
            )
        )
        self.assertEqual(self.jobs.get(created["job_id"])["state"], "CANCELLED")

    def test_current_request_origin_is_revalidated_for_every_host_operation(self):
        created, _ = self.enqueue()
        self.origin[0] = "https://evil.invalid"
        with self.assertRaises(PermissionError):
            self.host.get(
                created["job_id"],
                session=self.owner.token,
                actor="owner",
            )
        with self.assertRaises(PermissionError):
            self.host.cancel(
                created["job_id"],
                session=self.owner.token,
                actor="owner",
            )


if __name__ == "__main__":
    unittest.main()
