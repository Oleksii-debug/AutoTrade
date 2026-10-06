import unittest

from mvp.autotrade_mvp.host_api import EventGap, HostCommandStore, operation_result_payload


class HostCommandStateTests(unittest.TestCase):
    def setUp(self):
        self.sessions = {("session-a", "alice"), ("session-b", "bob")}
        self.store = HostCommandStore(
            account_id="paper-account-1",
            environment="PAPER",
            session_validator=lambda session, actor, origin, action: (session, actor) in self.sessions,
            request_origin_provider=lambda: "https://local.autotrade.invalid",
            max_events=3,
        )

    @staticmethod
    def command(
        *,
        command_id="11111111-1111-1111-1111-111111111111",
        key="key-1",
        version="0",
        actor="alice",
        session="session-a",
        account_id="paper-account-1",
        environment="PAPER",
        action="BLOCK_NEW_EXPOSURE",
        payload=None,
    ):
        return {
            "command_id": command_id,
            "expected_state_version": version,
            "idempotency_key": key,
            "actor": actor,
            "session": session,
            "account_id": account_id,
            "environment": environment,
            "action": action,
            "payload": payload or {},
        }

    def test_account_scope_rejects_str_subclass_without_strip_callback(self):
        callbacks = []

        class HostileText(str):
            def strip(self, *args, **kwargs):
                callbacks.append("strip")
                raise AssertionError("account_id strip callback must not run")

        with self.assertRaisesRegex(
            ValueError,
            "account_id must be a non-empty string",
        ):
            HostCommandStore(
                account_id=HostileText("paper-account-1"),
                environment="PAPER",
                session_validator=lambda session, actor, origin, action: True,
                request_origin_provider=lambda: "https://local.autotrade.invalid",
            )

        self.assertEqual(callbacks, [])


    def test_permission_ingress_requires_exact_origin_and_literal_true(self):
        callbacks = []

        class HostileOrigin(str):
            def strip(self, *args, **kwargs):
                callbacks.append("strip")
                raise AssertionError("request origin strip callback must not run")

        hostile_origin_store = HostCommandStore(
            account_id="paper-account-1",
            environment="PAPER",
            session_validator=lambda session, actor, origin, action: True,
            request_origin_provider=lambda: HostileOrigin(
                "https://local.autotrade.invalid"
            ),
        )
        with self.assertRaisesRegex(PermissionError, "origin is unavailable"):
            hostile_origin_store.submit(self.command())
        self.assertEqual(callbacks, [])
        self.assertEqual(hostile_origin_store.state_version, 0)

        class TruthyDecision:
            def __bool__(self):
                callbacks.append("bool")
                raise AssertionError("session decision truthiness must not run")

        non_boolean_store = HostCommandStore(
            account_id="paper-account-1",
            environment="PAPER",
            session_validator=lambda session, actor, origin, action: TruthyDecision(),
            request_origin_provider=lambda: "https://local.autotrade.invalid",
        )
        with self.assertRaisesRegex(PermissionError, "Session is not authorized"):
            non_boolean_store.submit(self.command())
        self.assertEqual(callbacks, [])
        self.assertEqual(non_boolean_store.state_version, 0)


    def test_v2_scope_is_required_canonical_and_matches_active_host(self):
        missing_account = self.command()
        missing_account.pop("account_id")
        with self.assertRaisesRegex(ValueError, "account_id"):
            self.store.submit(missing_account)

        missing_environment = self.command()
        missing_environment.pop("environment")
        with self.assertRaisesRegex(ValueError, "environment"):
            self.store.submit(missing_environment)

        with self.assertRaisesRegex(ValueError, "canonical Environment"):
            self.store.submit(self.command(environment="paper"))
        with self.assertRaisesRegex(ValueError, "active host"):
            self.store.submit(self.command(account_id="other-account"))
        with self.assertRaisesRegex(ValueError, "active host"):
            self.store.submit(self.command(environment="LIVE"))

        accepted = self.store.submit(self.command())
        self.assertEqual(accepted.status, "ACCEPTED")
        event = self.store.events_after(0)[0]
        self.assertEqual(event.payload["account_id"], "paper-account-1")
        self.assertEqual(event.payload["environment"], "PAPER")
        snapshot = self.store.snapshot()
        self.assertEqual(snapshot["account_id"], "paper-account-1")
        self.assertEqual(snapshot["environment"], "PAPER")

    def test_idempotency_scope_includes_actor_and_environment(self):
        first = self.store.submit(self.command(key="shared-key"))
        self.assertEqual(first.status, "ACCEPTED")
        second = self.store.submit(
            self.command(
                command_id="22222222-2222-2222-2222-222222222222",
                key="shared-key",
                version="1",
                actor="bob",
                session="session-b",
            )
        )
        self.assertEqual(second.status, "ACCEPTED")
        self.assertEqual(self.store.state_version, 2)

    def test_operation_identity_is_scoped_by_host_account(self):
        first = self.store.submit(self.command())

        other = HostCommandStore(
            account_id="paper-account-2",
            environment="PAPER",
            session_validator=lambda session, actor, origin, action: (
                session,
                actor,
            )
            in self.sessions,
            request_origin_provider=lambda: "https://local.autotrade.invalid",
            max_events=3,
        )
        second = other.submit(
            self.command(account_id="paper-account-2")
        )

        self.assertNotEqual(first.operation_id, second.operation_id)

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

    def test_unknown_or_noncanonical_action_is_rejected_before_mutation(self):
        for action in (
            "FUTURE_PRIVILEGED_ACTION",
            "block_new_exposure",
            " BLOCK_NEW_EXPOSURE",
        ):
            with self.subTest(action=action), self.assertRaisesRegex(
                ValueError,
                "host action",
            ):
                self.store.submit(self.command(action=action))
            self.assertEqual(self.store.state_version, 0)
            self.assertEqual(self.store.cursor, 0)

    def test_unauthorized_session_is_rejected_before_mutation(self):
        with self.assertRaises(PermissionError):
            self.store.submit(self.command(session="forged"))
        self.assertEqual(self.store.state_version, 0)
        self.assertEqual(self.store.cursor, 0)

    def test_internal_operation_version_does_not_drift_canonical_payload(self):
        accepted = self.store.submit(self.command())
        operation = self.store.get_operation(accepted.operation_id)
        self.assertEqual(operation.state_version, "1")
        payload = operation_result_payload(operation)
        self.assertNotIn("state_version", payload)
        self.assertEqual(
            set(payload),
            {
                "operation_id",
                "phase",
                "started_at",
                "updated_at",
                "affected_refs",
                "evidence",
                "remaining_uncertainty",
            },
        )

    def test_operation_completion_is_separate_versioned_transition(self):
        accepted = self.store.submit(self.command())
        completed = self.store.update_operation(accepted.operation_id, "SUCCEEDED")
        self.assertEqual(completed.phase, "SUCCEEDED")
        self.assertEqual(completed.state_version, "2")
        self.assertEqual(self.store.snapshot()["state_version"], "2")
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

        resolved = self.store.update_operation(accepted.operation_id, "SUCCEEDED")
        self.assertEqual(resolved.phase, "SUCCEEDED")
        self.assertEqual(resolved.remaining_uncertainty, ())

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

    def test_operation_evidence_text_arrays_reject_type_coercion(self):
        accepted = self.store.submit(self.command())
        with self.assertRaisesRegex(ValueError, "affected_refs must contain"):
            self.store.update_operation(
                accepted.operation_id,
                "RUNNING",
                affected_refs=(123,),
            )
        with self.assertRaisesRegex(ValueError, "remaining_uncertainty must contain"):
            self.store.update_operation(
                accepted.operation_id,
                "RUNNING",
                remaining_uncertainty=(123,),
            )
        self.assertEqual(self.store.state_version, 1)

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


    def test_expected_state_version_requires_canonical_sequence(self):
        for version in ("00", "01", "+0", "-0", " 0", "0 ", "\u0660"):
            with self.subTest(version=version), self.assertRaisesRegex(
                ValueError,
                "canonical Sequence",
            ):
                self.store.submit(self.command(version=version))
            self.assertEqual(self.store.state_version, 0)
            self.assertEqual(self.store.cursor, 0)


    def test_command_identity_text_rejects_str_subclasses_without_callbacks(self):
        callbacks = []

        class HostileText(str):
            def __bool__(self):
                callbacks.append("bool")
                raise AssertionError("command identity truthiness callback must not run")

            def strip(self, *args, **kwargs):
                callbacks.append("strip")
                raise AssertionError("command identity strip callback must not run")

            def __eq__(self, other):
                callbacks.append("eq")
                raise AssertionError("command identity equality callback must not run")

            def __hash__(self):
                callbacks.append("hash")
                raise AssertionError("command identity hash callback must not run")

        store = self.store
        cases = (
            ("command_id", "command_id", "11111111-1111-1111-1111-111111111111"),
            ("idempotency_key", "key", "key-1"),
            ("actor", "actor", "alice"),
            ("session", "session", "session-a"),
            ("account_id", "account_id", "paper-account-1"),
            ("environment", "environment", "PAPER"),
            ("action", "action", "BLOCK_NEW_EXPOSURE"),
        )
        for field, parameter, raw_value in cases:
            with self.subTest(field=field):
                callbacks.clear()
                with self.assertRaisesRegex(ValueError, "non-empty string"):
                    store.submit(
                        self.command(
                            **{parameter: HostileText(raw_value)}
                        )
                    )
                self.assertEqual(callbacks, [])
                self.assertEqual(store.state_version, 0)
                self.assertEqual(store.cursor, 0)


    def test_expected_state_version_rejects_str_subclass_before_truthiness(self):
        callbacks = []

        class HostileSequence(str):
            def __bool__(self):
                callbacks.append("bool")
                raise AssertionError("expected_state_version truthiness must not run")

        with self.assertRaisesRegex(ValueError, "canonical Sequence"):
            self.store.submit(self.command(version=HostileSequence("0")))
        self.assertEqual(callbacks, [])
        self.assertEqual(self.store.state_version, 0)
        self.assertEqual(self.store.cursor, 0)

    def test_event_cursor_rejects_noncanonical_sequence_text(self):
        self.store.submit(self.command())
        for after in ("01", "+1", "-0", " 0", "0 ", "\t0", "", True, 1.0, None):
            with self.subTest(after=after), self.assertRaisesRegex(
                ValueError,
                "canonical Sequence",
            ):
                self.store.events_after(after)
        self.assertEqual(
            [event.cursor for event in self.store.events_after("0")],
            [1],
        )
        self.assertEqual(
            [event.cursor for event in self.store.events_after(0)],
            [1],
        )


    def test_canonical_long_sequence_avoids_python_int_digit_limit(self):
        long_sequence = "9" * 5000
        result = self.store.submit(self.command(version=long_sequence))
        self.assertEqual(result.status, "CONFLICT")
        self.assertEqual(result.reason_codes, ("stale_state_version",))
        self.assertEqual(self.store.state_version, 0)
        with self.assertRaisesRegex(ValueError, "ahead of host state"):
            self.store.events_after(long_sequence)


if __name__ == "__main__":
    unittest.main()
