from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.recovery import RecoveryController
from mvp.tests.test_provider_route_dispatch import (
    ProviderRouteDispatchTests,
    successor_spot_q,
)


class ProviderRouteRecoveryProvenanceTests(unittest.TestCase):
    def _fixture(self, directory: str):
        fixture = ProviderRouteDispatchTests(
            methodName="test_success_binds_q_and_c_into_durable_submission_scope"
        )
        self.addCleanup(fixture.doCleanups)
        return fixture, fixture.setup_route(directory)

    def test_restart_retains_q1_that_governed_send_when_q2_supersedes_after_barrier(self):
        with TemporaryDirectory() as directory:
            fixture, values = self._fixture(directory)
            (
                journal,
                capabilities,
                qualifications,
                route,
                dispatcher,
                q1,
                harness,
            ) = values
            q2_box = []

            def transport(_client_id, _request, final_guard):
                # Q1 is re-resolved by the irreversible final guard first. Once
                # that barrier has passed, a later Q2 cannot make this attempt
                # retry-safe or rewrite which qualification governed the send.
                final_guard()
                q2, receipt2, protocol2 = successor_spot_q(
                    old_qualification_id=q1.qualification_id,
                    ordinal=42,
                )
                harness.register(
                    protocol_key=protocol2.key,
                    record=q2,
                    receipt=receipt2,
                )
                qualifications._append_accepted(
                    protocol_key=protocol2.key,
                    record=q2,
                    receipt=receipt2,
                )
                qualifications._append_supersession(
                    old_id=q1.qualification_id,
                    new_id=q2.qualification_id,
                )
                q2_box.append(q2)
                raise RuntimeError("provider response lost after send barrier")

            outcome = fixture.dispatch(
                dispatcher,
                route,
                capabilities,
                qualifications,
                transport,
            )
            self.assertEqual(outcome.status, "UNKNOWN")
            self.assertEqual(len(q2_box), 1)
            q2 = q2_box[0]

            aggregate_id = dispatcher._aggregate_id("attempt-route-1")
            before_restart = journal.load_events(
                "submission_attempt",
                aggregate_id,
            )
            self.assertEqual(
                [event["event_type"] for event in before_restart],
                ["SubmissionPrepared", "SubmissionSending", "SubmissionUnknown"],
            )
            prepared_scope = before_restart[0]["payload"]["submission_scope"]
            self.assertEqual(
                prepared_scope["provider_route_qualification_id"],
                q1.qualification_id,
            )
            self.assertNotEqual(
                prepared_scope["provider_route_qualification_id"],
                q2.qualification_id,
            )
            self.assertEqual(
                prepared_scope["provider_route_capability_snapshot_id"],
                route.capability_snapshot_id,
            )
            self.assertEqual(
                prepared_scope["provider_route_adapter_code_sha"],
                route.candidate.adapter_code_sha,
            )
            self.assertEqual(
                prepared_scope["provider_route_packaged_artifact_digest"],
                route.candidate.packaged_artifact_digest,
            )
            self.assertEqual(
                prepared_scope["provider_route_protocol_id"],
                route.candidate.protocol_id,
            )
            self.assertEqual(
                prepared_scope["provider_route_protocol_version"],
                route.candidate.protocol_version,
            )

            # Simulate process restart through the real startup path. start()
            # creates the new durable owner fence and immediately reconstructs
            # sticky UNKNOWN sends from this PAPER:account journal scope.
            reopened = JournalStore(journal.path)
            recovery = RecoveryController(
                owner_store=reopened,
                owner_scope="PAPER:paper-account",
            )
            owner = recovery.start("host-restarted")
            self.assertEqual(owner.owner_id, "host-restarted")
            self.assertEqual(owner.epoch, 1)
            self.assertIn("attempt-route-1", recovery.unresolved_attempts)
            self.assertIn("provider_uncertainty", recovery.reason_codes)

            # Startup may append recovery-owner evidence, but it must not rewrite
            # the original submission aggregate or silently substitute current Q2
            # for the exact Q1 that governed the possible send.
            after_restart = reopened.load_events(
                "submission_attempt",
                aggregate_id,
            )
            self.assertEqual(after_restart, before_restart)
            recovered_scope = after_restart[0]["payload"]["submission_scope"]
            self.assertEqual(
                recovered_scope["provider_route_qualification_id"],
                q1.qualification_id,
            )
            self.assertNotEqual(
                recovered_scope["provider_route_qualification_id"],
                q2.qualification_id,
            )
            self.assertEqual(
                recovered_scope["provider_route_capability_snapshot_id"],
                route.capability_snapshot_id,
            )


if __name__ == "__main__":
    unittest.main()
