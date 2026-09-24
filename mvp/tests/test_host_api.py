import json
from pathlib import Path
import unittest

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from mvp.autotrade_mvp.host_api import (
    EventGap,
    HostCommandStore,
    command_result_payload,
    operation_result_payload,
)


ROOT = Path(__file__).resolve().parents[2]


class HostCommandStateTests(unittest.TestCase):
    def setUp(self):
        self.sessions = {("session-a", "alice"), ("session-b", "bob")}
        self.store = HostCommandStore(
            session_validator=lambda session, actor: (session, actor) in self.sessions,
            action_authorizer=lambda session, actor, action, payload: (
                (session, actor) in self.sessions
            ),
            max_events=3,
            now=lambda: "2026-09-24T18:00:00Z",
        )

    @staticmethod
    def reconciliation_evidence(
        *,
        artifact_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        digest="sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        observed_at="2026-09-24T18:00:00Z",
    ):
        return {
            "artifact_id": artifact_id,
            "sha256": digest,
            "observed_at": observed_at,
        }

    @staticmethod
    def command(
        *,
        command_id="11111111-1111-1111-1111-111111111111",
        key="key-1",
        version="0",
        actor="alice",
        session="session-a",
        action="BLOCK_NEW_EXPOSURE",
        payload=None,
    ):
        return {
            "command_id": command_id,
            "expected_state_version": version,
            "idempotency_key": key,
            "actor": actor,
            "session": session,
            "action": action,
            "payload": payload or {},
        }

    def test_acceptance_is_not_reported_as_financial_completion(self):
        result = self.store.submit(self.command())
        self.assertEqual(result.status, "ACCEPTED")
        operation = self.store.get_operation(result.operation_id)
        self.assertEqual(operation.phase, "QUEUED")
        self.assertIn("financial_outcome_not_completed", operation.remaining_uncertainty)

    def test_identical_retry_is_idempotent_without_new_event_or_version(self):
        command = self.command()
        first = self.store.submit(command)
        second = self.store.submit(command)
        self.assertEqual(first, second)
        self.assertEqual(self.store.state_version, 1)
        self.assertEqual(self.store.cursor, 1)

    def test_changed_payload_under_same_idempotency_key_conflicts(self):
        self.store.submit(self.command(payload={"scope": "A"}))
        conflict = self.store.submit(self.command(payload={"scope": "B"}))
        self.assertEqual(conflict.status, "CONFLICT")
        self.assertIn("idempotency_key_conflict", conflict.reason_codes)
        self.assertEqual(self.store.state_version, 1)

    def test_same_command_identifier_with_different_request_conflicts(self):
        first = self.command(key="key-a")
        self.store.submit(first)
        changed = self.command(key="key-b", payload={"different": True})
        conflict = self.store.submit(changed)
        self.assertEqual(conflict.status, "CONFLICT")
        self.assertIn("command_id_conflict", conflict.reason_codes)

    def test_stale_state_prevents_two_sessions_lost_update(self):
        accepted = self.store.submit(self.command())
        self.assertEqual(accepted.state_version, "1")
        stale = self.store.submit(
            self.command(
                command_id="22222222-2222-2222-2222-222222222222",
                key="key-2",
                version="0",
                actor="bob",
                session="session-b",
            )
        )
        self.assertEqual(stale.status, "CONFLICT")
        self.assertIn("stale_state_version", stale.reason_codes)
        self.assertEqual(stale.state_version, "1")

    def test_authenticated_session_still_requires_action_authorization(self):
        store = HostCommandStore(
            session_validator=lambda session, actor: (session, actor) in self.sessions,
            action_authorizer=lambda session, actor, action, payload: (
                actor == "alice"
                and action == "BLOCK_NEW_EXPOSURE"
                and payload.get("scope") == "paper"
            ),
            now=lambda: "2026-09-24T18:00:00Z",
        )
        denied = store.submit(self.command(payload={"scope": "live"}))
        self.assertEqual(denied.status, "REJECTED")
        self.assertEqual(denied.reason_codes, ("action_not_authorized",))
        self.assertEqual(store.state_version, 0)
        self.assertEqual(store.cursor, 0)
        self.assertEqual(store.submit(self.command(payload={"scope": "live"})), denied)

        allowed = store.submit(
            self.command(
                command_id="55555555-5555-5555-5555-555555555555",
                key="key-authorized",
                payload={"scope": "paper"},
            )
        )
        self.assertEqual(allowed.status, "ACCEPTED")
        self.assertEqual(store.state_version, 1)
        self.assertEqual(store.cursor, 1)

    def test_revocation_blocks_new_command_but_exact_retry_stays_idempotent(self):
        authorization = {"allowed": True}
        store = HostCommandStore(
            session_validator=lambda session, actor: (session, actor) in self.sessions,
            action_authorizer=lambda session, actor, action, payload: authorization["allowed"],
            now=lambda: "2026-09-24T18:00:00Z",
        )
        command = self.command()
        accepted = store.submit(command)
        self.assertEqual(accepted.status, "ACCEPTED")
        self.assertEqual(store.cursor, 1)

        authorization["allowed"] = False
        self.assertEqual(store.submit(command), accepted)
        self.assertEqual(store.cursor, 1)
        denied = store.submit(
            self.command(
                command_id="66666666-6666-6666-6666-666666666666",
                key="key-after-revocation",
                version="1",
            )
        )
        self.assertEqual(denied.status, "REJECTED")
        self.assertEqual(denied.reason_codes, ("action_not_authorized",))
        self.assertEqual(store.state_version, 1)
        self.assertEqual(store.cursor, 1)

    def test_action_authorizer_is_mandatory_callable(self):
        with self.assertRaisesRegex(TypeError, "action_authorizer must be callable"):
            HostCommandStore(
                session_validator=lambda session, actor: True,
                action_authorizer=None,
            )

    def test_unsupported_action_is_rejected_without_state_mutation(self):
        command = self.command(action="ARBITRARY_PROVIDER_COMMAND")
        rejected = self.store.submit(command)
        self.assertEqual(rejected.status, "REJECTED")
        self.assertEqual(rejected.reason_codes, ("unsupported_action",))
        self.assertEqual(self.store.state_version, 0)
        self.assertEqual(self.store.cursor, 0)
        self.assertEqual(self.store.submit(command), rejected)

    def test_unauthorized_session_is_rejected_before_mutation(self):
        with self.assertRaises(PermissionError):
            self.store.submit(self.command(session="forged"))
        self.assertEqual(self.store.state_version, 0)
        self.assertEqual(self.store.cursor, 0)

    def test_operation_completion_is_separate_versioned_transition(self):
        accepted = self.store.submit(self.command())
        completed = self.store.update_operation(accepted.operation_id, "SUCCEEDED")
        self.assertEqual(completed.phase, "SUCCEEDED")
        self.assertEqual(self.store.snapshot()["state_version"], "2")
        self.assertEqual(completed.started_at, "2026-09-24T18:00:00Z")
        self.assertEqual(completed.updated_at, "2026-09-24T18:00:00Z")
        with self.assertRaises(ValueError):
            self.store.update_operation(accepted.operation_id, "FAILED")

    def test_unknown_preserves_uncertainty_and_can_only_resolve_terminally(self):
        accepted = self.store.submit(self.command())
        unknown = self.store.update_operation(
            accepted.operation_id,
            "UNKNOWN",
            remaining_uncertainty=("provider_outcome_unresolved",),
        )
        self.assertEqual(unknown.phase, "UNKNOWN")
        self.assertEqual(
            unknown.remaining_uncertainty,
            ("provider_outcome_unresolved",),
        )
        event = self.store.events_after("1")[0]
        self.assertEqual(
            event.payload["remaining_uncertainty"],
            ["provider_outcome_unresolved"],
        )
        with self.assertRaisesRegex(ValueError, "only resolve"):
            self.store.update_operation(
                accepted.operation_id,
                "RUNNING",
                remaining_uncertainty=("still_unknown",),
            )

        with self.assertRaisesRegex(
            ValueError,
            "requires new reconciliation evidence",
        ):
            self.store.update_operation(accepted.operation_id, "SUCCEEDED")

        resolved = self.store.update_operation(
            accepted.operation_id,
            "SUCCEEDED",
            evidence=(self.reconciliation_evidence(),),
        )
        self.assertEqual(resolved.phase, "SUCCEEDED")
        self.assertEqual(resolved.remaining_uncertainty, ())
        self.assertEqual(resolved.evidence, (self.reconciliation_evidence(),))

    def test_unknown_resolution_rejects_reused_or_malformed_evidence(self):
        accepted = self.store.submit(self.command())
        prior = self.reconciliation_evidence(
            artifact_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
            digest="sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        )
        self.store.update_operation(
            accepted.operation_id,
            "UNKNOWN",
            remaining_uncertainty=("provider_outcome_unresolved",),
            evidence=(prior,),
        )
        with self.assertRaisesRegex(ValueError, "requires new reconciliation evidence"):
            self.store.update_operation(
                accepted.operation_id,
                "FAILED",
                evidence=(prior,),
            )
        with self.assertRaisesRegex(ValueError, "artifact_id"):
            self.store.update_operation(
                accepted.operation_id,
                "FAILED",
                evidence=(
                    {
                        "artifact_id": "",
                        "sha256": "sha256:" + ("c" * 64),
                        "observed_at": "2026-09-24T18:00:00Z",
                    },
                ),
            )

    def test_unknown_requires_uncertainty_and_terminal_cannot_hide_it(self):
        accepted = self.store.submit(self.command())
        with self.assertRaisesRegex(ValueError, "preserve remaining uncertainty"):
            self.store.update_operation(accepted.operation_id, "UNKNOWN")
        with self.assertRaisesRegex(ValueError, "cannot retain unresolved uncertainty"):
            self.store.update_operation(
                accepted.operation_id,
                "FAILED",
                remaining_uncertainty=("provider_outcome_unresolved",),
            )

    def test_results_serialize_against_canonical_ui_schema(self):
        common = json.loads(
            (ROOT / "contracts/jsonschema/common.schema.json").read_text(
                encoding="utf-8"
            )
        )
        ui = json.loads(
            (ROOT / "contracts/jsonschema/ui.schema.json").read_text(
                encoding="utf-8"
            )
        )
        registry = Registry().with_resources(
            [
                (common["$id"], Resource.from_contents(common)),
                (ui["$id"], Resource.from_contents(ui)),
            ]
        )
        accepted = self.store.submit(self.command())
        operation = self.store.get_operation(accepted.operation_id)
        command_payload = command_result_payload(accepted)
        operation_payload = operation_result_payload(operation)

        Draft202012Validator(
            {"$ref": f"{ui['$id']}#/$defs/CommandResult"},
            registry=registry,
        ).validate(command_payload)
        Draft202012Validator(
            {"$ref": f"{ui['$id']}#/$defs/OperationResult"},
            registry=registry,
        ).validate(operation_payload)

        self.assertEqual(command_payload["field_errors"], [])
        self.assertNotIn("state_version", operation_payload)
        self.assertEqual(
            operation_payload["remaining_uncertainty"],
            ["financial_outcome_not_completed"],
        )

    def test_resumable_events_return_only_newer_items(self):
        accepted = self.store.submit(self.command())
        self.store.update_operation(accepted.operation_id, "RUNNING")
        self.store.update_operation(accepted.operation_id, "WAITING_EXTERNAL")
        events = self.store.events_after("1")
        self.assertEqual([item.cursor for item in events], [2, 3])

    def test_event_retention_gap_requires_resnapshot(self):
        accepted = self.store.submit(self.command())
        self.store.update_operation(accepted.operation_id, "RUNNING")
        self.store.update_operation(accepted.operation_id, "WAITING_EXTERNAL")
        self.store.update_operation(accepted.operation_id, "SUCCEEDED")
        with self.assertRaises(EventGap):
            self.store.events_after("0")
        self.assertEqual(self.store.snapshot()["event_cursor"], "4")

    def test_future_cursor_is_rejected(self):
        with self.assertRaises(ValueError):
            self.store.events_after("1")


if __name__ == "__main__":
    unittest.main()
