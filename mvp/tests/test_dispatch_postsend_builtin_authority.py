from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp import dispatch as dispatch_module
from mvp.autotrade_mvp import persistence as persistence_module
from mvp.autotrade_mvp.dispatch import ExactJsonTransportResponse, GuardedDispatcher
from mvp.autotrade_mvp.persistence import JournalStore


class PostSendBuiltinAuthorityTests(unittest.TestCase):
    @staticmethod
    def _dispatcher(path: str) -> GuardedDispatcher:
        return GuardedDispatcher(
            JournalStore(path),
            environment="SIMULATION",
            account_id="acct",
            owner_token="owner",
        )

    @staticmethod
    def _allow(_intent_hash, _now):
        return True, "allowed"

    def _dispatch(self, dispatcher, *, attempt_id, transport):
        return dispatcher.dispatch(
            attempt_id=attempt_id,
            intent_id="intent-1",
            intent_hash="sha256:" + "1" * 64,
            provider="provider",
            request={"side": "BUY"},
            now="2026-10-06T18:15:00Z",
            authority_check=self._allow,
            transport_send=transport,
            submission_scope={"endpoint": "/orders"},
        )

    def _event_types(self, path, dispatcher, attempt_id):
        events = JournalStore.load_events(
            JournalStore(path),
            "submission_attempt",
            dispatcher._aggregate_id(attempt_id),
        )
        return [event["event_type"] for event in events], events

    def test_exact_response_uses_pretransport_type_builtin(self):
        callbacks = 0

        def hostile_type(*_args):
            nonlocal callbacks
            callbacks += 1
            raise AssertionError("rebound type executed")

        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            dispatcher = self._dispatcher(path)
            original = getattr(dispatch_module, "type", None)
            had_global = "type" in vars(dispatch_module)

            def transport(_client_order_id, _request, final_guard):
                final_guard()
                dispatch_module.type = hostile_type
                return ExactJsonTransportResponse(
                    b'{"accepted":true}',
                    http_status=200,
                )

            try:
                result = self._dispatch(
                    dispatcher,
                    attempt_id="postsend-type-exact-a1",
                    transport=transport,
                )
            finally:
                if had_global:
                    dispatch_module.type = original
                else:
                    del dispatch_module.type

            self.assertEqual(callbacks, 0)
            self.assertEqual(result.status, "SENT")
            self.assertEqual(result.reason, "sent_confirmed")
            self.assertEqual(result.response, {"accepted": True})
            event_types, _events = self._event_types(
                path,
                dispatcher,
                "postsend-type-exact-a1",
            )
            self.assertEqual(
                event_types,
                ["SubmissionPrepared", "SubmissionSending", "SubmissionSent"],
            )

    def test_legacy_response_uses_pretransport_isinstance_builtin(self):
        callbacks = 0

        def hostile_isinstance(*_args):
            nonlocal callbacks
            callbacks += 1
            raise AssertionError("rebound isinstance executed")

        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            dispatcher = self._dispatcher(path)
            original = getattr(dispatch_module, "isinstance", None)
            had_global = "isinstance" in vars(dispatch_module)

            def transport(_client_order_id, _request, final_guard):
                final_guard()
                dispatch_module.isinstance = hostile_isinstance
                return {"accepted": True}

            try:
                result = self._dispatch(
                    dispatcher,
                    attempt_id="postsend-isinstance-legacy-a1",
                    transport=transport,
                )
            finally:
                if had_global:
                    dispatch_module.isinstance = original
                else:
                    del dispatch_module.isinstance

            self.assertEqual(callbacks, 0)
            self.assertEqual(result.status, "SENT")
            self.assertEqual(result.reason, "sent_confirmed")
            self.assertEqual(result.response, {"accepted": True})
            event_types, _events = self._event_types(
                path,
                dispatcher,
                "postsend-isinstance-legacy-a1",
            )
            self.assertEqual(
                event_types,
                ["SubmissionPrepared", "SubmissionSending", "SubmissionSent"],
            )

    def test_transport_exception_uses_pretransport_type_builtin(self):
        callbacks = 0

        def hostile_type(*_args):
            nonlocal callbacks
            callbacks += 1
            raise AssertionError("rebound type executed")

        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            dispatcher = self._dispatcher(path)
            original = getattr(dispatch_module, "type", None)
            had_global = "type" in vars(dispatch_module)

            def transport(_client_order_id, _request, final_guard):
                final_guard()
                dispatch_module.type = hostile_type
                raise RuntimeError("provider result lost after send")

            try:
                result = self._dispatch(
                    dispatcher,
                    attempt_id="postsend-type-error-a1",
                    transport=transport,
                )
            finally:
                if had_global:
                    dispatch_module.type = original
                else:
                    del dispatch_module.type

            self.assertEqual(callbacks, 0)
            self.assertEqual(result.status, "UNKNOWN")
            self.assertEqual(result.reason, "transport_result_ambiguous")
            event_types, events = self._event_types(
                path,
                dispatcher,
                "postsend-type-error-a1",
            )
            self.assertEqual(
                event_types,
                ["SubmissionPrepared", "SubmissionSending", "SubmissionUnknown"],
            )
            self.assertEqual(
                events[-1]["payload"]["reason"],
                "transport_exception_after_send_barrier:RuntimeError",
            )

    def test_pre_guard_transport_exception_uses_pretransport_type_builtin(self):
        callbacks = 0

        def hostile_type(*_args):
            nonlocal callbacks
            callbacks += 1
            raise AssertionError("rebound type executed")

        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            dispatcher = self._dispatcher(path)
            original = getattr(dispatch_module, "type", None)
            had_global = "type" in vars(dispatch_module)

            def transport(_client_order_id, _request, _final_guard):
                dispatch_module.type = hostile_type
                raise RuntimeError("provider failed before final guard")

            try:
                result = self._dispatch(
                    dispatcher,
                    attempt_id="preguard-type-error-a1",
                    transport=transport,
                )
            finally:
                if had_global:
                    dispatch_module.type = original
                else:
                    del dispatch_module.type

            self.assertEqual(callbacks, 0)
            self.assertEqual(result.status, "BLOCKED")
            self.assertEqual(result.reason, "transport_failed_before_send")
            event_types, events = self._event_types(
                path,
                dispatcher,
                "preguard-type-error-a1",
            )
            self.assertEqual(
                event_types,
                ["SubmissionPrepared", "SubmissionBlocked"],
            )
            self.assertEqual(
                events[-1]["payload"]["reason"],
                "transport_failed_before_final_guard:RuntimeError",
            )


    def test_transport_builtin_shadows_are_removed_before_next_dispatch(self):
        callbacks = 0

        def hostile_type(*_args):
            nonlocal callbacks
            callbacks += 1
            raise AssertionError("rebound type executed")

        def hostile_isinstance(*_args):
            nonlocal callbacks
            callbacks += 1
            raise AssertionError("rebound isinstance executed")

        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            dispatcher = self._dispatcher(path)
            original_type = getattr(dispatch_module, "type", None)
            original_isinstance = getattr(dispatch_module, "isinstance", None)
            had_type = "type" in vars(dispatch_module)
            had_isinstance = "isinstance" in vars(dispatch_module)
            outbound = 0

            def poisoned_transport(_client_order_id, _request, final_guard):
                nonlocal outbound
                final_guard()
                outbound += 1
                dispatch_module.type = hostile_type
                dispatch_module.isinstance = hostile_isinstance
                return {"accepted": True}

            try:
                first = self._dispatch(
                    dispatcher,
                    attempt_id="postsend-builtin-restore-a1",
                    transport=poisoned_transport,
                )
                self.assertEqual(first.status, "SENT")
                self.assertEqual(first.reason, "sent_confirmed")
                self.assertEqual(callbacks, 0)
                if had_type:
                    self.assertIs(dispatch_module.type, original_type)
                else:
                    self.assertNotIn("type", vars(dispatch_module))
                if had_isinstance:
                    self.assertIs(dispatch_module.isinstance, original_isinstance)
                else:
                    self.assertNotIn("isinstance", vars(dispatch_module))

                def clean_transport(_client_order_id, _request, final_guard):
                    nonlocal outbound
                    final_guard()
                    outbound += 1
                    return ExactJsonTransportResponse(
                        b'{"accepted":true}',
                        http_status=200,
                    )

                second = self._dispatch(
                    dispatcher,
                    attempt_id="postsend-builtin-restore-a2",
                    transport=clean_transport,
                )
                self.assertEqual(second.status, "SENT")
                self.assertEqual(second.reason, "sent_confirmed")
                self.assertEqual(callbacks, 0)
                self.assertEqual(outbound, 2)
            finally:
                if had_type:
                    dispatch_module.type = original_type
                else:
                    vars(dispatch_module).pop("type", None)
                if had_isinstance:
                    dispatch_module.isinstance = original_isinstance
                else:
                    vars(dispatch_module).pop("isinstance", None)

    def test_type_shadow_transport_unknown_replays_after_restart_without_resend(self):
        callbacks = 0

        def hostile_type(*_args):
            nonlocal callbacks
            callbacks += 1
            raise AssertionError("rebound type executed")

        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            dispatcher = self._dispatcher(path)
            original = getattr(dispatch_module, "type", None)
            had_global = "type" in vars(dispatch_module)
            outbound = 0

            def transport(_client_order_id, _request, final_guard):
                nonlocal outbound
                final_guard()
                outbound += 1
                dispatch_module.type = hostile_type
                raise RuntimeError("provider result lost after send")

            try:
                first = self._dispatch(
                    dispatcher,
                    attempt_id="postsend-type-restart-a1",
                    transport=transport,
                )
                self.assertEqual(first.status, "UNKNOWN")
                self.assertEqual(first.reason, "transport_result_ambiguous")
                self.assertEqual(callbacks, 0)
                self.assertEqual(outbound, 1)
                if had_global:
                    self.assertIs(dispatch_module.type, original)
                else:
                    self.assertNotIn("type", vars(dispatch_module))

                restarted = GuardedDispatcher(
                    JournalStore(path),
                    environment="SIMULATION",
                    account_id="acct",
                    owner_token="owner-b",
                )
                replay = restarted.dispatch(
                    attempt_id="postsend-type-restart-a1",
                    intent_id="intent-1",
                    intent_hash="sha256:" + "1" * 64,
                    provider="provider",
                    request={"side": "BUY"},
                    now="2026-10-06T18:15:01Z",
                    authority_check=lambda *_args: (
                        _ for _ in ()
                    ).throw(AssertionError("restart must not re-authorize")),
                    transport_send=lambda *_args: (
                        _ for _ in ()
                    ).throw(AssertionError("restart must not resend")),
                    submission_scope={"endpoint": "/orders"},
                )
                self.assertEqual(replay.status, "UNKNOWN")
                self.assertEqual(
                    replay.reason,
                    "transport_exception_after_send_barrier:RuntimeError",
                )
                self.assertEqual(callbacks, 0)
                self.assertEqual(outbound, 1)
            finally:
                if had_global:
                    dispatch_module.type = original
                else:
                    vars(dispatch_module).pop("type", None)


    def test_persistence_error_reason_uses_pretransport_type_builtin(self):
        callbacks = 0

        def hostile_type(*_args):
            nonlocal callbacks
            callbacks += 1
            raise AssertionError("rebound type executed")

        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            dispatcher = self._dispatcher(path)
            original = getattr(dispatch_module, "type", None)
            had_global = "type" in vars(dispatch_module)

            def transport(_client_order_id, _request, final_guard):
                final_guard()
                response = ExactJsonTransportResponse(
                    b'{"accepted":true}',
                    http_status=200,
                )
                object.__setattr__(response, "http_status", True)
                dispatch_module.type = hostile_type
                return response

            try:
                result = self._dispatch(
                    dispatcher,
                    attempt_id="postsend-type-persistence-error-a1",
                    transport=transport,
                )
                self.assertEqual(callbacks, 0)
                if had_global:
                    self.assertIs(dispatch_module.type, original)
                else:
                    self.assertNotIn("type", vars(dispatch_module))
            finally:
                if had_global:
                    dispatch_module.type = original
                else:
                    vars(dispatch_module).pop("type", None)

            self.assertEqual(result.status, "UNKNOWN")
            self.assertEqual(result.reason, "sent_response_persistence_failed")
            event_types, events = self._event_types(
                path,
                dispatcher,
                "postsend-type-persistence-error-a1",
            )
            self.assertEqual(
                event_types,
                ["SubmissionPrepared", "SubmissionSending", "SubmissionUnknown"],
            )
            self.assertEqual(
                events[-1]["payload"]["reason"],
                "sent_response_persistence_failed:ValueError",
            )
            self.assertEqual(callbacks, 0)

    def test_masked_final_guard_failure_uses_pretransport_type_builtin(self):
        callbacks = 0
        authority_calls = 0

        def hostile_type(*_args):
            nonlocal callbacks
            callbacks += 1
            raise AssertionError("rebound type executed")

        def authority(_intent_hash, _now):
            nonlocal authority_calls
            authority_calls += 1
            if authority_calls == 1:
                return True, "allowed"
            return False, "operator_revoked"

        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            dispatcher = self._dispatcher(path)
            original = getattr(dispatch_module, "type", None)
            had_global = "type" in vars(dispatch_module)

            def transport(_client_order_id, _request, final_guard):
                try:
                    final_guard()
                except Exception:
                    dispatch_module.type = hostile_type
                    raise RuntimeError("wrapper masked guard failure")
                raise AssertionError("final guard unexpectedly allowed")

            try:
                result = dispatcher.dispatch(
                    attempt_id="postsend-type-masked-guard-a1",
                    intent_id="intent-1",
                    intent_hash="sha256:" + "1" * 64,
                    provider="provider",
                    request={"side": "BUY"},
                    now="2026-10-06T18:15:00Z",
                    authority_check=authority,
                    transport_send=transport,
                    submission_scope={"endpoint": "/orders"},
                )
                self.assertEqual(callbacks, 0)
                if had_global:
                    self.assertIs(dispatch_module.type, original)
                else:
                    self.assertNotIn("type", vars(dispatch_module))
            finally:
                if had_global:
                    dispatch_module.type = original
                else:
                    vars(dispatch_module).pop("type", None)

            self.assertEqual(authority_calls, 2)
            self.assertEqual(result.status, "UNKNOWN")
            self.assertEqual(result.reason, "provider_guard_contract_violation")
            event_types, events = self._event_types(
                path,
                dispatcher,
                "postsend-type-masked-guard-a1",
            )
            self.assertEqual(
                event_types,
                [
                    "SubmissionPrepared",
                    "SubmissionBlocked",
                    "SubmissionUnknown",
                ],
            )
            self.assertEqual(
                events[-1]["payload"]["reason"],
                "provider_wrapper_masked_final_guard_failure:RuntimeError",
            )
            self.assertEqual(callbacks, 0)


    def test_transport_exception_restores_exception_class_globals_before_matching(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            dispatcher = self._dispatcher(path)
            names = (
                "Exception",
                "ValueError",
                "TypeError",
                "DispatchBlocked",
                "_DispatchAuthorityChanged",
            )
            missing = object()
            original = {
                name: vars(dispatch_module).get(name, missing)
                for name in names
            }

            def transport(_client_order_id, _request, final_guard):
                final_guard()
                for name in names:
                    setattr(dispatch_module, name, object())
                raise RuntimeError("provider result lost after send")

            try:
                result = self._dispatch(
                    dispatcher,
                    attempt_id="postsend-exception-globals-a1",
                    transport=transport,
                )
                self.assertEqual(result.status, "UNKNOWN")
                self.assertEqual(result.reason, "transport_result_ambiguous")
                for name, expected in original.items():
                    if expected is missing:
                        self.assertNotIn(name, vars(dispatch_module))
                    else:
                        self.assertIs(vars(dispatch_module)[name], expected)
            finally:
                for name, expected in original.items():
                    if expected is missing:
                        vars(dispatch_module).pop(name, None)
                    else:
                        setattr(dispatch_module, name, expected)

            event_types, events = self._event_types(
                path,
                dispatcher,
                "postsend-exception-globals-a1",
            )
            self.assertEqual(
                event_types,
                ["SubmissionPrepared", "SubmissionSending", "SubmissionUnknown"],
            )
            self.assertEqual(
                events[-1]["payload"]["reason"],
                "transport_exception_after_send_barrier:RuntimeError",
            )

    def test_masked_guard_failure_uses_pretransport_int_builtin(self):
        callbacks = 0
        authority_calls = 0

        def hostile_int(*_args):
            nonlocal callbacks
            callbacks += 1
            raise AssertionError("rebound int executed")

        def authority(_intent_hash, _now):
            nonlocal authority_calls
            authority_calls += 1
            if authority_calls == 1:
                return True, "allowed"
            return False, "operator_revoked"

        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            dispatcher = self._dispatcher(path)
            original = getattr(dispatch_module, "int", None)
            had_global = "int" in vars(dispatch_module)

            def transport(_client_order_id, _request, final_guard):
                try:
                    final_guard()
                except Exception:
                    dispatch_module.int = hostile_int
                    raise RuntimeError("wrapper masked guard failure")
                raise AssertionError("final guard unexpectedly allowed")

            try:
                result = dispatcher.dispatch(
                    attempt_id="postsend-int-masked-guard-a1",
                    intent_id="intent-1",
                    intent_hash="sha256:" + "1" * 64,
                    provider="provider",
                    request={"side": "BUY"},
                    now="2026-10-06T18:15:00Z",
                    authority_check=authority,
                    transport_send=transport,
                    submission_scope={"endpoint": "/orders"},
                )
                self.assertEqual(callbacks, 0)
                if had_global:
                    self.assertIs(dispatch_module.int, original)
                else:
                    self.assertNotIn("int", vars(dispatch_module))
            finally:
                if had_global:
                    dispatch_module.int = original
                else:
                    vars(dispatch_module).pop("int", None)

            self.assertEqual(authority_calls, 2)
            self.assertEqual(result.status, "UNKNOWN")
            self.assertEqual(result.reason, "provider_guard_contract_violation")
            event_types, events = self._event_types(
                path,
                dispatcher,
                "postsend-int-masked-guard-a1",
            )
            self.assertEqual(
                event_types,
                [
                    "SubmissionPrepared",
                    "SubmissionBlocked",
                    "SubmissionUnknown",
                ],
            )
            self.assertEqual(
                events[-1]["payload"]["reason"],
                "provider_wrapper_masked_final_guard_failure:RuntimeError",
            )
            self.assertEqual(callbacks, 0)

    def test_pre_guard_dispatch_blocked_uses_pretransport_str_builtin(self):
        callbacks = 0

        def hostile_str(*_args):
            nonlocal callbacks
            callbacks += 1
            raise AssertionError("rebound str executed")

        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            dispatcher = self._dispatcher(path)
            original = getattr(dispatch_module, "str", None)
            had_global = "str" in vars(dispatch_module)

            def transport(_client_order_id, _request, _final_guard):
                error = dispatch_module.DispatchBlocked("provider wrapper blocked")
                dispatch_module.str = hostile_str
                raise error

            try:
                result = self._dispatch(
                    dispatcher,
                    attempt_id="preguard-str-dispatch-blocked-a1",
                    transport=transport,
                )
                self.assertEqual(callbacks, 0)
                if had_global:
                    self.assertIs(dispatch_module.str, original)
                else:
                    self.assertNotIn("str", vars(dispatch_module))
            finally:
                if had_global:
                    dispatch_module.str = original
                else:
                    vars(dispatch_module).pop("str", None)

            self.assertEqual(result.status, "BLOCKED")
            self.assertEqual(result.reason, "provider wrapper blocked")
            event_types, _events = self._event_types(
                path,
                dispatcher,
                "preguard-str-dispatch-blocked-a1",
            )
            self.assertEqual(event_types, ["SubmissionPrepared"])
            self.assertEqual(callbacks, 0)


    def test_exact_response_str_shadow_does_not_false_positive_decoder_change(self):
        callbacks = 0

        def hostile_str(*_args):
            nonlocal callbacks
            callbacks += 1
            raise AssertionError("rebound str executed")

        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            dispatcher = self._dispatcher(path)
            original = getattr(dispatch_module, "str", None)
            had_global = "str" in vars(dispatch_module)

            def transport(_client_order_id, _request, final_guard):
                final_guard()
                dispatch_module.str = hostile_str
                return ExactJsonTransportResponse(
                    b'{"accepted":true}',
                    http_status=200,
                )

            try:
                result = self._dispatch(
                    dispatcher,
                    attempt_id="postsend-str-exact-a1",
                    transport=transport,
                )
                self.assertEqual(callbacks, 0)
                if had_global:
                    self.assertIs(dispatch_module.str, original)
                else:
                    self.assertNotIn("str", vars(dispatch_module))
            finally:
                if had_global:
                    dispatch_module.str = original
                else:
                    vars(dispatch_module).pop("str", None)

            self.assertEqual(result.status, "SENT")
            self.assertEqual(result.reason, "sent_confirmed")
            self.assertEqual(result.response, {"accepted": True})
            event_types, _events = self._event_types(
                path,
                dispatcher,
                "postsend-str-exact-a1",
            )
            self.assertEqual(
                event_types,
                ["SubmissionPrepared", "SubmissionSending", "SubmissionSent"],
            )
            self.assertEqual(callbacks, 0)


    def test_post_send_helper_rebinding_is_unknown_and_restart_no_resend(self):
        surfaces = (
            "_canonical_journal_authority_snapshot",
            "_journal_store_call",
            "_envelope",
            "_detach_submission_json",
            "submission_attempt_aggregate_id",
            "_event_id",
            "_identity_digest",
            "_instant",
            "payload_digest",
            "canonical_json",
            "_canonical_submission_event_instant",
            "_exact_response_terminal_semantics_are_canonical",
            "_has_exact_response_markers",
            "uuid5",
            "NAMESPACE_URL",
            "DispatchOutcome",
        )
        for surface in surfaces:
            with self.subTest(surface=surface), TemporaryDirectory() as directory:
                path = f"{directory}/journal.sqlite3"
                dispatcher = self._dispatcher(path)
                original = getattr(dispatch_module, surface)
                hostile_calls = 0
                outbound = 0

                def forged(*_args, **_kwargs):
                    nonlocal hostile_calls
                    hostile_calls += 1
                    raise AssertionError(f"rebound {surface} executed")

                def transport(_client_order_id, _request, final_guard):
                    nonlocal outbound
                    final_guard()
                    outbound += 1
                    response = ExactJsonTransportResponse(
                        b'{"accepted":true}',
                        http_status=200,
                    )
                    setattr(dispatch_module, surface, forged)
                    return response

                attempt_id = f"postsend-helper-{surface}"
                try:
                    first = self._dispatch(
                        dispatcher,
                        attempt_id=attempt_id,
                        transport=transport,
                    )
                    self.assertIs(getattr(dispatch_module, surface), original)
                finally:
                    setattr(dispatch_module, surface, original)

                self.assertEqual(hostile_calls, 0)
                self.assertEqual(outbound, 1)
                self.assertEqual(first.status, "UNKNOWN")
                self.assertEqual(
                    first.reason,
                    "dispatcher_authority_changed_after_send_barrier",
                )
                event_types, _events = self._event_types(
                    path,
                    dispatcher,
                    attempt_id,
                )
                self.assertEqual(
                    event_types,
                    ["SubmissionPrepared", "SubmissionSending"],
                )

                restarted = GuardedDispatcher(
                    JournalStore(path),
                    environment="SIMULATION",
                    account_id="acct",
                    owner_token="owner-b",
                )
                replay = restarted.dispatch(
                    attempt_id=attempt_id,
                    intent_id="intent-1",
                    intent_hash="sha256:" + "1" * 64,
                    provider="provider",
                    request={"side": "BUY"},
                    now="2026-10-06T18:15:01Z",
                    authority_check=lambda *_args: (
                        _ for _ in ()
                    ).throw(AssertionError("restart must not re-authorize")),
                    transport_send=lambda *_args: (
                        _ for _ in ()
                    ).throw(AssertionError("restart must not resend")),
                    submission_scope={"endpoint": "/orders"},
                )
                self.assertEqual(replay.status, "UNKNOWN")
                self.assertEqual(
                    replay.reason,
                    "recovered_after_send_barrier_without_terminal_result",
                )
                self.assertEqual(hostile_calls, 0)
                self.assertEqual(outbound, 1)

    def test_post_send_helper_code_mutation_is_restored_and_unknown(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            dispatcher = self._dispatcher(path)
            helper = dispatch_module._detach_submission_json
            original_code = helper.__code__

            def forged(value, *, _active_containers=None):
                raise AssertionError("forged detach helper code executed")

            def transport(_client_order_id, _request, final_guard):
                final_guard()
                response = ExactJsonTransportResponse(
                    b'{"accepted":true}',
                    http_status=200,
                )
                helper.__code__ = forged.__code__
                return response

            try:
                result = self._dispatch(
                    dispatcher,
                    attempt_id="postsend-helper-code-a1",
                    transport=transport,
                )
                self.assertIs(helper.__code__, original_code)
            finally:
                helper.__code__ = original_code

            self.assertEqual(result.status, "UNKNOWN")
            self.assertEqual(
                result.reason,
                "dispatcher_authority_changed_after_send_barrier",
            )
            event_types, _events = self._event_types(
                path,
                dispatcher,
                "postsend-helper-code-a1",
            )
            self.assertEqual(
                event_types,
                ["SubmissionPrepared", "SubmissionSending"],
            )

    def test_post_send_helper_kwdefaults_mutation_is_restored_and_unknown(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            dispatcher = self._dispatcher(path)
            helper = dispatch_module._detach_submission_json
            original_kwdefaults = helper.__kwdefaults__
            self.assertIsInstance(original_kwdefaults, dict)
            baseline = dict(original_kwdefaults)

            def transport(_client_order_id, _request, final_guard):
                final_guard()
                response = ExactJsonTransportResponse(
                    b'{"accepted":true}',
                    http_status=200,
                )
                helper.__kwdefaults__["_active_containers"] = {"poison"}
                return response

            try:
                result = self._dispatch(
                    dispatcher,
                    attempt_id="postsend-helper-kwdefaults-a1",
                    transport=transport,
                )
                self.assertIs(helper.__kwdefaults__, original_kwdefaults)
                self.assertEqual(helper.__kwdefaults__, baseline)
            finally:
                helper.__kwdefaults__ = original_kwdefaults
                original_kwdefaults.clear()
                original_kwdefaults.update(baseline)

            self.assertEqual(result.status, "UNKNOWN")
            self.assertEqual(
                result.reason,
                "dispatcher_authority_changed_after_send_barrier",
            )
            event_types, _events = self._event_types(
                path,
                dispatcher,
                "postsend-helper-kwdefaults-a1",
            )
            self.assertEqual(
                event_types,
                ["SubmissionPrepared", "SubmissionSending"],
            )


    def test_post_send_dispatcher_method_code_mutation_is_restored_and_unknown(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            dispatcher = self._dispatcher(path)
            original_events = GuardedDispatcher._events
            original_code = original_events.__code__
            probe = []
            outbound = 0
            dispatch_module._dispatcher_executable_probe = probe

            def forged_events(self, attempt_id):
                _dispatcher_executable_probe.append(attempt_id)
                raise AssertionError("forged dispatcher method code executed")

            def transport(_client_order_id, _request, final_guard):
                nonlocal outbound
                final_guard()
                outbound += 1
                response = ExactJsonTransportResponse(
                    b'{"accepted":true}',
                    http_status=200,
                )
                original_events.__code__ = forged_events.__code__
                return response

            try:
                first = self._dispatch(
                    dispatcher,
                    attempt_id="postsend-dispatcher-code-a1",
                    transport=transport,
                )
                self.assertIs(GuardedDispatcher._events, original_events)
                self.assertIs(original_events.__code__, original_code)
            finally:
                original_events.__code__ = original_code
                vars(dispatch_module).pop("_dispatcher_executable_probe", None)

            self.assertEqual(probe, [])
            self.assertEqual(outbound, 1)
            self.assertEqual(first.status, "UNKNOWN")
            self.assertEqual(
                first.reason,
                "dispatcher_authority_changed_after_send_barrier",
            )
            event_types, _events = self._event_types(
                path,
                dispatcher,
                "postsend-dispatcher-code-a1",
            )
            self.assertEqual(
                event_types,
                ["SubmissionPrepared", "SubmissionSending"],
            )

            restarted = GuardedDispatcher(
                JournalStore(path),
                environment="SIMULATION",
                account_id="acct",
                owner_token="owner-b",
            )
            replay = restarted.dispatch(
                attempt_id="postsend-dispatcher-code-a1",
                intent_id="intent-1",
                intent_hash="sha256:" + "1" * 64,
                provider="provider",
                request={"side": "BUY"},
                now="2026-10-06T18:15:01Z",
                authority_check=lambda *_args: (
                    _ for _ in ()
                ).throw(AssertionError("restart must not re-authorize")),
                transport_send=lambda *_args: (
                    _ for _ in ()
                ).throw(AssertionError("restart must not resend")),
                submission_scope={"endpoint": "/orders"},
            )
            self.assertEqual(replay.status, "UNKNOWN")
            self.assertEqual(
                replay.reason,
                "recovered_after_send_barrier_without_terminal_result",
            )
            self.assertEqual(probe, [])
            self.assertEqual(outbound, 1)

    def test_post_send_staticmethod_code_mutation_is_restored_and_unknown(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            dispatcher = self._dispatcher(path)
            descriptor = GuardedDispatcher.__dict__["_outcome_from_terminal"]
            helper = descriptor.__func__
            original_code = helper.__code__
            probe = []
            outbound = 0
            dispatch_module._dispatcher_descriptor_probe = probe

            def forged_outcome(event, client_order_id):
                _dispatcher_descriptor_probe.append(client_order_id)
                raise AssertionError("forged staticmethod code executed")

            def transport(_client_order_id, _request, final_guard):
                nonlocal outbound
                final_guard()
                outbound += 1
                response = ExactJsonTransportResponse(
                    b'{"accepted":true}',
                    http_status=200,
                )
                helper.__code__ = forged_outcome.__code__
                return response

            try:
                first = self._dispatch(
                    dispatcher,
                    attempt_id="postsend-staticmethod-code-a1",
                    transport=transport,
                )
                self.assertIs(
                    GuardedDispatcher.__dict__["_outcome_from_terminal"],
                    descriptor,
                )
                self.assertIs(helper.__code__, original_code)
            finally:
                helper.__code__ = original_code
                vars(dispatch_module).pop("_dispatcher_descriptor_probe", None)

            self.assertEqual(probe, [])
            self.assertEqual(outbound, 1)
            self.assertEqual(first.status, "UNKNOWN")
            self.assertEqual(
                first.reason,
                "dispatcher_authority_changed_after_send_barrier",
            )
            event_types, _events = self._event_types(
                path,
                dispatcher,
                "postsend-staticmethod-code-a1",
            )
            self.assertEqual(
                event_types,
                ["SubmissionPrepared", "SubmissionSending"],
            )

            restarted = GuardedDispatcher(
                JournalStore(path),
                environment="SIMULATION",
                account_id="acct",
                owner_token="owner-b",
            )
            replay = restarted.dispatch(
                attempt_id="postsend-staticmethod-code-a1",
                intent_id="intent-1",
                intent_hash="sha256:" + "1" * 64,
                provider="provider",
                request={"side": "BUY"},
                now="2026-10-06T18:15:01Z",
                authority_check=lambda *_args: (
                    _ for _ in ()
                ).throw(AssertionError("restart must not re-authorize")),
                transport_send=lambda *_args: (
                    _ for _ in ()
                ).throw(AssertionError("restart must not resend")),
                submission_scope={"endpoint": "/orders"},
            )
            self.assertEqual(replay.status, "UNKNOWN")
            self.assertEqual(
                replay.reason,
                "recovered_after_send_barrier_without_terminal_result",
            )
            self.assertEqual(probe, [])
            self.assertEqual(outbound, 1)

    def test_postsend_builtin_module_shadows_are_restored_before_firebreak(self):
        callbacks = 0
        sentinel = object()
        originals = {
            name: vars(dispatch_module).get(name, sentinel)
            for name in ("list", "zip", "KeyError", "UnicodeError")
        }

        def hostile_list(*_args, **_kwargs):
            nonlocal callbacks
            callbacks += 1
            raise AssertionError("module-global list shadow executed")

        def hostile_zip(*_args, **_kwargs):
            nonlocal callbacks
            callbacks += 1
            raise AssertionError("module-global zip shadow executed")

        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            dispatcher = self._dispatcher(path)

            def transport(_client_order_id, _request, final_guard):
                final_guard()
                response = ExactJsonTransportResponse(
                    b'{"accepted":true}',
                    http_status=200,
                )
                dispatch_module.list = hostile_list
                dispatch_module.zip = hostile_zip
                dispatch_module.KeyError = object
                dispatch_module.UnicodeError = object
                return response

            try:
                result = self._dispatch(
                    dispatcher,
                    attempt_id="postsend-builtin-global-firebreak-a1",
                    transport=transport,
                )
                self.assertEqual(callbacks, 0)
                self.assertEqual(result.status, "SENT")
                self.assertEqual(result.reason, "sent_confirmed")
                for name, original in originals.items():
                    if original is sentinel:
                        self.assertNotIn(name, vars(dispatch_module))
                    else:
                        self.assertIs(getattr(dispatch_module, name), original)
            finally:
                for name, original in originals.items():
                    if original is sentinel:
                        vars(dispatch_module).pop(name, None)
                    else:
                        setattr(dispatch_module, name, original)

            event_types, _events = self._event_types(
                path,
                dispatcher,
                "postsend-builtin-global-firebreak-a1",
            )
            self.assertEqual(
                event_types,
                ["SubmissionPrepared", "SubmissionSending", "SubmissionSent"],
            )
            self.assertEqual(callbacks, 0)

    def test_transport_helper_rebinding_before_final_guard_is_zero_wire(self):
        surfaces = (
            "_canonical_journal_authority_snapshot",
            "_journal_store_call",
            "_envelope",
            "_detach_submission_json",
            "submission_attempt_aggregate_id",
            "_event_id",
            "_identity_digest",
            "_instant",
            "_prepared_lease_state",
            "_validated_authority_result",
            "payload_digest",
            "canonical_json",
            "_canonical_submission_event_instant",
            "_exact_response_terminal_semantics_are_canonical",
            "_has_exact_response_markers",
            "uuid5",
            "NAMESPACE_URL",
            "sha256",
            "datetime",
            "timezone",
            "timedelta",
            "DispatchOutcome",
        )
        for surface in surfaces:
            with self.subTest(surface=surface), TemporaryDirectory() as directory:
                path = f"{directory}/journal.sqlite3"
                dispatcher = self._dispatcher(path)
                original = getattr(dispatch_module, surface)
                hostile_calls = 0
                outbound = 0

                def forged(*_args, **_kwargs):
                    nonlocal hostile_calls
                    hostile_calls += 1
                    raise AssertionError(f"rebound {surface} executed")

                def transport(_client_order_id, _request, final_guard):
                    nonlocal outbound
                    setattr(dispatch_module, surface, forged)
                    final_guard()
                    outbound += 1
                    return ExactJsonTransportResponse(
                        b'{"accepted":true}',
                        http_status=200,
                    )

                try:
                    with self.assertRaises(PermissionError):
                        self._dispatch(
                            dispatcher,
                            attempt_id=f"preguard-helper-{surface}",
                            transport=transport,
                        )
                    self.assertIs(getattr(dispatch_module, surface), original)
                finally:
                    setattr(dispatch_module, surface, original)

                self.assertEqual(hostile_calls, 0)
                self.assertEqual(outbound, 0)
                event_types, _events = self._event_types(
                    path,
                    dispatcher,
                    f"preguard-helper-{surface}",
                )
                self.assertEqual(event_types, ["SubmissionPrepared"])

    def test_transport_helper_code_mutation_before_final_guard_is_zero_wire(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            dispatcher = self._dispatcher(path)
            helper = dispatch_module._prepared_lease_state
            original_code = helper.__code__
            outbound = 0

            def forged(*, prepared_at, now, lease_seconds):
                raise AssertionError("forged prepared lease helper executed")

            def transport(_client_order_id, _request, final_guard):
                nonlocal outbound
                helper.__code__ = forged.__code__
                final_guard()
                outbound += 1
                return ExactJsonTransportResponse(
                    b'{"accepted":true}',
                    http_status=200,
                )

            try:
                with self.assertRaises(PermissionError):
                    self._dispatch(
                        dispatcher,
                        attempt_id="preguard-helper-code-a1",
                        transport=transport,
                    )
                self.assertIs(helper.__code__, original_code)
            finally:
                helper.__code__ = original_code

            self.assertEqual(outbound, 0)
            event_types, _events = self._event_types(
                path,
                dispatcher,
                "preguard-helper-code-a1",
            )
            self.assertEqual(event_types, ["SubmissionPrepared"])


    def test_post_send_journal_load_code_mutation_is_restored_and_unknown(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            dispatcher = self._dispatcher(path)
            operation = JournalStore.load_events
            original_code = operation.__code__
            outbound = 0

            def forged_load_events(self, aggregate_type, aggregate_id):
                raise AssertionError("forged JournalStore.load_events code executed")

            def transport(_client_order_id, _request, final_guard):
                nonlocal outbound
                final_guard()
                outbound += 1
                response = ExactJsonTransportResponse(
                    b'{"accepted":true}',
                    http_status=200,
                )
                operation.__code__ = forged_load_events.__code__
                return response

            try:
                first = self._dispatch(
                    dispatcher,
                    attempt_id="postsend-journal-load-code-a1",
                    transport=transport,
                )
                self.assertIs(JournalStore.load_events, operation)
                self.assertIs(operation.__code__, original_code)
            finally:
                operation.__code__ = original_code

            self.assertEqual(outbound, 1)
            self.assertEqual(first.status, "UNKNOWN")
            self.assertEqual(
                first.reason,
                "dispatcher_authority_changed_after_send_barrier",
            )
            event_types, _events = self._event_types(
                path,
                dispatcher,
                "postsend-journal-load-code-a1",
            )
            self.assertEqual(
                event_types,
                ["SubmissionPrepared", "SubmissionSending"],
            )

            restarted = GuardedDispatcher(
                JournalStore(path),
                environment="SIMULATION",
                account_id="acct",
                owner_token="owner-b",
            )
            replay = restarted.dispatch(
                attempt_id="postsend-journal-load-code-a1",
                intent_id="intent-1",
                intent_hash="sha256:" + "1" * 64,
                provider="provider",
                request={"side": "BUY"},
                now="2026-10-06T18:15:01Z",
                authority_check=lambda *_args: (
                    _ for _ in ()
                ).throw(AssertionError("restart must not re-authorize")),
                transport_send=lambda *_args: (
                    _ for _ in ()
                ).throw(AssertionError("restart must not resend")),
                submission_scope={"endpoint": "/orders"},
            )
            self.assertEqual(replay.status, "UNKNOWN")
            self.assertEqual(
                replay.reason,
                "recovered_after_send_barrier_without_terminal_result",
            )
            self.assertEqual(outbound, 1)

    def test_journal_append_code_mutation_before_final_guard_is_zero_wire(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            dispatcher = self._dispatcher(path)
            operation = JournalStore.append_event
            original_code = operation.__code__
            outbound = 0

            def forged_append_event(
                self,
                envelope,
                *,
                outbox_topic=None,
                expected_journal_sequence=None,
                expected_whole_store_counts=None,
            ):
                raise AssertionError("forged JournalStore.append_event code executed")

            def transport(_client_order_id, _request, final_guard):
                nonlocal outbound
                operation.__code__ = forged_append_event.__code__
                final_guard()
                outbound += 1
                return ExactJsonTransportResponse(
                    b'{"accepted":true}',
                    http_status=200,
                )

            try:
                with self.assertRaises(PermissionError):
                    self._dispatch(
                        dispatcher,
                        attempt_id="preguard-journal-append-code-a1",
                        transport=transport,
                    )
                self.assertIs(JournalStore.append_event, operation)
                self.assertIs(operation.__code__, original_code)
            finally:
                operation.__code__ = original_code

            self.assertEqual(outbound, 0)
            event_types, _events = self._event_types(
                path,
                dispatcher,
                "preguard-journal-append-code-a1",
            )
            self.assertEqual(event_types, ["SubmissionPrepared"])


    def test_post_send_journal_kwdefaults_mutation_is_restored_and_unknown(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            dispatcher = self._dispatcher(path)
            operation = JournalStore.append_event
            original_kwdefaults = operation.__kwdefaults__
            self.assertIsInstance(original_kwdefaults, dict)
            baseline = dict(original_kwdefaults)
            outbound = 0

            def transport(_client_order_id, _request, final_guard):
                nonlocal outbound
                final_guard()
                outbound += 1
                response = ExactJsonTransportResponse(
                    b'{"accepted":true}',
                    http_status=200,
                )
                original_kwdefaults["expected_journal_sequence"] = 7
                return response

            try:
                first = self._dispatch(
                    dispatcher,
                    attempt_id="postsend-journal-kwdefaults-a1",
                    transport=transport,
                )
                self.assertIs(operation.__kwdefaults__, original_kwdefaults)
                self.assertEqual(operation.__kwdefaults__, baseline)
            finally:
                operation.__kwdefaults__ = original_kwdefaults
                original_kwdefaults.clear()
                original_kwdefaults.update(baseline)

            self.assertEqual(outbound, 1)
            self.assertEqual(first.status, "UNKNOWN")
            self.assertEqual(
                first.reason,
                "dispatcher_authority_changed_after_send_barrier",
            )
            event_types, _events = self._event_types(
                path,
                dispatcher,
                "postsend-journal-kwdefaults-a1",
            )
            self.assertEqual(
                event_types,
                ["SubmissionPrepared", "SubmissionSending"],
            )


    def test_post_send_builtin_shadow_and_journal_code_mutation_compose_safely(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            dispatcher = self._dispatcher(path)
            operation = JournalStore.load_events
            original_code = operation.__code__
            sentinel = object()
            original_zip = vars(dispatch_module).get("zip", sentinel)
            hostile_calls = 0
            outbound = 0

            def hostile_zip(*_args, **_kwargs):
                nonlocal hostile_calls
                hostile_calls += 1
                raise AssertionError("module-global zip shadow executed")

            def forged_load_events(self, aggregate_type, aggregate_id):
                nonlocal hostile_calls
                hostile_calls += 1
                raise AssertionError("forged JournalStore.load_events code executed")

            def transport(_client_order_id, _request, final_guard):
                nonlocal outbound
                final_guard()
                outbound += 1
                response = ExactJsonTransportResponse(
                    b'{"accepted":true}',
                    http_status=200,
                )
                dispatch_module.zip = hostile_zip
                operation.__code__ = forged_load_events.__code__
                return response

            try:
                first = self._dispatch(
                    dispatcher,
                    attempt_id="postsend-composed-builtin-journal-a1",
                    transport=transport,
                )
                self.assertIs(JournalStore.load_events, operation)
                self.assertIs(operation.__code__, original_code)
                if original_zip is sentinel:
                    self.assertNotIn("zip", vars(dispatch_module))
                else:
                    self.assertIs(dispatch_module.zip, original_zip)
            finally:
                operation.__code__ = original_code
                if original_zip is sentinel:
                    vars(dispatch_module).pop("zip", None)
                else:
                    dispatch_module.zip = original_zip

            self.assertEqual(hostile_calls, 0)
            self.assertEqual(outbound, 1)
            self.assertEqual(first.status, "UNKNOWN")
            self.assertEqual(
                first.reason,
                "dispatcher_authority_changed_after_send_barrier",
            )
            event_types, _events = self._event_types(
                path,
                dispatcher,
                "postsend-composed-builtin-journal-a1",
            )
            self.assertEqual(
                event_types,
                ["SubmissionPrepared", "SubmissionSending"],
            )

            restarted = GuardedDispatcher(
                JournalStore(path),
                environment="SIMULATION",
                account_id="acct",
                owner_token="owner-b",
            )
            replay = restarted.dispatch(
                attempt_id="postsend-composed-builtin-journal-a1",
                intent_id="intent-1",
                intent_hash="sha256:" + "1" * 64,
                provider="provider",
                request={"side": "BUY"},
                now="2026-10-06T19:48:01Z",
                authority_check=lambda *_args: (
                    _ for _ in ()
                ).throw(AssertionError("restart must not re-authorize")),
                transport_send=lambda *_args: (
                    _ for _ in ()
                ).throw(AssertionError("restart must not resend")),
                submission_scope={"endpoint": "/orders"},
            )
            self.assertEqual(replay.status, "UNKNOWN")
            self.assertEqual(
                replay.reason,
                "recovered_after_send_barrier_without_terminal_result",
            )
            self.assertEqual(hostile_calls, 0)
            self.assertEqual(outbound, 1)


    def test_dispatcher_staticmethod_code_mutation_before_guard_is_zero_wire(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            dispatcher = self._dispatcher(path)
            descriptor = GuardedDispatcher.__dict__["_outcome_from_terminal"]
            original_outcome = descriptor.__func__
            original_code = original_outcome.__code__
            probe = []
            outbound = 0
            dispatch_module._dispatcher_staticmethod_executable_probe = probe

            def forged_outcome(event, client_order_id):
                _dispatcher_staticmethod_executable_probe.append(
                    (event, client_order_id)
                )
                raise AssertionError("forged dispatcher staticmethod code executed")

            def transport(_client_order_id, _request, final_guard):
                nonlocal outbound
                original_outcome.__code__ = forged_outcome.__code__
                final_guard()
                outbound += 1
                return ExactJsonTransportResponse(
                    b'{"accepted":true}',
                    http_status=200,
                )

            try:
                with self.assertRaises(PermissionError):
                    self._dispatch(
                        dispatcher,
                        attempt_id="preguard-dispatcher-staticmethod-code-a1",
                        transport=transport,
                    )
                self.assertIs(original_outcome.__code__, original_code)
            finally:
                original_outcome.__code__ = original_code
                vars(dispatch_module).pop(
                    "_dispatcher_staticmethod_executable_probe",
                    None,
                )

            self.assertEqual(probe, [])
            self.assertEqual(outbound, 0)
            event_types, _events = self._event_types(
                path,
                dispatcher,
                "preguard-dispatcher-staticmethod-code-a1",
            )
            self.assertEqual(event_types, ["SubmissionPrepared"])


    def test_post_send_persistence_sqlite_global_rebinding_is_restored_unknown(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            dispatcher = self._dispatcher(path)
            original_sqlite3 = persistence_module.sqlite3
            hostile_calls = []
            outbound = 0

            class HostileSqlite:
                def __getattr__(self, name):
                    hostile_calls.append(name)
                    raise AssertionError(
                        "rebound persistence sqlite3 authority executed"
                    )

            def transport(_client_order_id, _request, final_guard):
                nonlocal outbound
                final_guard()
                outbound += 1
                response = ExactJsonTransportResponse(
                    b'{"accepted":true}',
                    http_status=200,
                )
                persistence_module.sqlite3 = HostileSqlite()
                return response

            try:
                first = self._dispatch(
                    dispatcher,
                    attempt_id="postsend-persistence-sqlite-global-a1",
                    transport=transport,
                )
                self.assertIs(persistence_module.sqlite3, original_sqlite3)
            finally:
                persistence_module.sqlite3 = original_sqlite3

            self.assertEqual(hostile_calls, [])
            self.assertEqual(outbound, 1)
            self.assertEqual(first.status, "UNKNOWN")
            self.assertEqual(
                first.reason,
                "dispatcher_authority_changed_after_send_barrier",
            )
            event_types, _events = self._event_types(
                path,
                dispatcher,
                "postsend-persistence-sqlite-global-a1",
            )
            self.assertEqual(
                event_types,
                ["SubmissionPrepared", "SubmissionSending"],
            )

            restarted = GuardedDispatcher(
                JournalStore(path),
                environment="SIMULATION",
                account_id="acct",
                owner_token="owner-b",
            )
            replay = restarted.dispatch(
                attempt_id="postsend-persistence-sqlite-global-a1",
                intent_id="intent-1",
                intent_hash="sha256:" + "1" * 64,
                provider="provider",
                request={"side": "BUY"},
                now="2026-10-06T19:52:01Z",
                authority_check=lambda *_args: (
                    _ for _ in ()
                ).throw(AssertionError("restart must not re-authorize")),
                transport_send=lambda *_args: (
                    _ for _ in ()
                ).throw(AssertionError("restart must not resend")),
                submission_scope={"endpoint": "/orders"},
            )
            self.assertEqual(replay.status, "UNKNOWN")
            self.assertEqual(
                replay.reason,
                "recovered_after_send_barrier_without_terminal_result",
            )
            self.assertEqual(hostile_calls, [])
            self.assertEqual(outbound, 1)

    def test_persistence_sqlite_global_rebinding_before_guard_is_zero_wire(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            dispatcher = self._dispatcher(path)
            original_sqlite3 = persistence_module.sqlite3
            hostile_calls = []
            outbound = 0

            class HostileSqlite:
                def __getattr__(self, name):
                    hostile_calls.append(name)
                    raise AssertionError(
                        "rebound persistence sqlite3 authority executed"
                    )

            def transport(_client_order_id, _request, final_guard):
                nonlocal outbound
                persistence_module.sqlite3 = HostileSqlite()
                final_guard()
                outbound += 1
                return ExactJsonTransportResponse(
                    b'{"accepted":true}',
                    http_status=200,
                )

            try:
                with self.assertRaises(PermissionError):
                    self._dispatch(
                        dispatcher,
                        attempt_id="preguard-persistence-sqlite-global-a1",
                        transport=transport,
                    )
                self.assertIs(persistence_module.sqlite3, original_sqlite3)
            finally:
                persistence_module.sqlite3 = original_sqlite3

            self.assertEqual(hostile_calls, [])
            self.assertEqual(outbound, 0)
            event_types, _events = self._event_types(
                path,
                dispatcher,
                "preguard-persistence-sqlite-global-a1",
            )
            self.assertEqual(event_types, ["SubmissionPrepared"])


    def test_post_send_persistence_sqlite_connect_mutation_is_restored_unknown(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            dispatcher = self._dispatcher(path)
            original_connect = persistence_module.sqlite3.connect
            hostile_calls = 0
            outbound = 0

            def hostile_connect(*_args, **_kwargs):
                nonlocal hostile_calls
                hostile_calls += 1
                raise AssertionError("mutated sqlite3.connect executed")

            def transport(_client_order_id, _request, final_guard):
                nonlocal outbound
                final_guard()
                outbound += 1
                response = ExactJsonTransportResponse(
                    b'{"accepted":true}',
                    http_status=200,
                )
                persistence_module.sqlite3.connect = hostile_connect
                return response

            try:
                first = self._dispatch(
                    dispatcher,
                    attempt_id="postsend-persistence-sqlite-connect-a1",
                    transport=transport,
                )
                self.assertIs(
                    persistence_module.sqlite3.connect,
                    original_connect,
                )
            finally:
                persistence_module.sqlite3.connect = original_connect

            self.assertEqual(hostile_calls, 0)
            self.assertEqual(outbound, 1)
            self.assertEqual(first.status, "UNKNOWN")
            self.assertEqual(
                first.reason,
                "dispatcher_authority_changed_after_send_barrier",
            )
            event_types, _events = self._event_types(
                path,
                dispatcher,
                "postsend-persistence-sqlite-connect-a1",
            )
            self.assertEqual(
                event_types,
                ["SubmissionPrepared", "SubmissionSending"],
            )

            restarted = GuardedDispatcher(
                JournalStore(path),
                environment="SIMULATION",
                account_id="acct",
                owner_token="owner-b",
            )
            replay = restarted.dispatch(
                attempt_id="postsend-persistence-sqlite-connect-a1",
                intent_id="intent-1",
                intent_hash="sha256:" + "1" * 64,
                provider="provider",
                request={"side": "BUY"},
                now="2026-10-06T19:56:01Z",
                authority_check=lambda *_args: (
                    _ for _ in ()
                ).throw(AssertionError("restart must not re-authorize")),
                transport_send=lambda *_args: (
                    _ for _ in ()
                ).throw(AssertionError("restart must not resend")),
                submission_scope={"endpoint": "/orders"},
            )
            self.assertEqual(replay.status, "UNKNOWN")
            self.assertEqual(
                replay.reason,
                "recovered_after_send_barrier_without_terminal_result",
            )
            self.assertEqual(hostile_calls, 0)
            self.assertEqual(outbound, 1)

    def test_persistence_sqlite_connect_mutation_before_guard_is_zero_wire(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            dispatcher = self._dispatcher(path)
            original_connect = persistence_module.sqlite3.connect
            hostile_calls = 0
            outbound = 0

            def hostile_connect(*_args, **_kwargs):
                nonlocal hostile_calls
                hostile_calls += 1
                raise AssertionError("mutated sqlite3.connect executed")

            def transport(_client_order_id, _request, final_guard):
                nonlocal outbound
                persistence_module.sqlite3.connect = hostile_connect
                final_guard()
                outbound += 1
                return ExactJsonTransportResponse(
                    b'{"accepted":true}',
                    http_status=200,
                )

            try:
                with self.assertRaises(PermissionError):
                    self._dispatch(
                        dispatcher,
                        attempt_id="preguard-persistence-sqlite-connect-a1",
                        transport=transport,
                    )
                self.assertIs(
                    persistence_module.sqlite3.connect,
                    original_connect,
                )
            finally:
                persistence_module.sqlite3.connect = original_connect

            self.assertEqual(hostile_calls, 0)
            self.assertEqual(outbound, 0)
            event_types, _events = self._event_types(
                path,
                dispatcher,
                "preguard-persistence-sqlite-connect-a1",
            )
            self.assertEqual(event_types, ["SubmissionPrepared"])


    def test_post_send_persistence_helper_code_mutation_is_restored_unknown(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            dispatcher = self._dispatcher(path)
            helper = persistence_module._require_exact_journal_store_state
            original_code = helper.__code__
            probe = []
            outbound = 0
            persistence_module._journal_helper_probe = probe

            def forged_state(value, *, subject):
                _journal_helper_probe.append(subject)
                raise AssertionError("mutated persistence helper code executed")

            def transport(_client_order_id, _request, final_guard):
                nonlocal outbound
                final_guard()
                outbound += 1
                response = ExactJsonTransportResponse(
                    b'{"accepted":true}',
                    http_status=200,
                )
                helper.__code__ = forged_state.__code__
                return response

            try:
                first = self._dispatch(
                    dispatcher,
                    attempt_id="postsend-persistence-helper-code-a1",
                    transport=transport,
                )
                self.assertIs(helper.__code__, original_code)
            finally:
                helper.__code__ = original_code
                vars(persistence_module).pop("_journal_helper_probe", None)

            self.assertEqual(probe, [])
            self.assertEqual(outbound, 1)
            self.assertEqual(first.status, "UNKNOWN")
            self.assertEqual(
                first.reason,
                "dispatcher_authority_changed_after_send_barrier",
            )
            event_types, _events = self._event_types(
                path,
                dispatcher,
                "postsend-persistence-helper-code-a1",
            )
            self.assertEqual(
                event_types,
                ["SubmissionPrepared", "SubmissionSending"],
            )

            restarted = GuardedDispatcher(
                JournalStore(path),
                environment="SIMULATION",
                account_id="acct",
                owner_token="owner-b",
            )
            replay = restarted.dispatch(
                attempt_id="postsend-persistence-helper-code-a1",
                intent_id="intent-1",
                intent_hash="sha256:" + "1" * 64,
                provider="provider",
                request={"side": "BUY"},
                now="2026-10-06T20:00:01Z",
                authority_check=lambda *_args: (
                    _ for _ in ()
                ).throw(AssertionError("restart must not re-authorize")),
                transport_send=lambda *_args: (
                    _ for _ in ()
                ).throw(AssertionError("restart must not resend")),
                submission_scope={"endpoint": "/orders"},
            )
            self.assertEqual(replay.status, "UNKNOWN")
            self.assertEqual(
                replay.reason,
                "recovered_after_send_barrier_without_terminal_result",
            )
            self.assertEqual(probe, [])
            self.assertEqual(outbound, 1)

    def test_persistence_helper_code_mutation_before_guard_is_zero_wire(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            dispatcher = self._dispatcher(path)
            helper = persistence_module._require_exact_journal_store_state
            original_code = helper.__code__
            probe = []
            outbound = 0
            persistence_module._journal_helper_probe = probe

            def forged_state(value, *, subject):
                _journal_helper_probe.append(subject)
                raise AssertionError("mutated persistence helper code executed")

            def transport(_client_order_id, _request, final_guard):
                nonlocal outbound
                helper.__code__ = forged_state.__code__
                final_guard()
                outbound += 1
                return ExactJsonTransportResponse(
                    b'{"accepted":true}',
                    http_status=200,
                )

            try:
                with self.assertRaises(PermissionError):
                    self._dispatch(
                        dispatcher,
                        attempt_id="preguard-persistence-helper-code-a1",
                        transport=transport,
                    )
                self.assertIs(helper.__code__, original_code)
            finally:
                helper.__code__ = original_code
                vars(persistence_module).pop("_journal_helper_probe", None)

            self.assertEqual(probe, [])
            self.assertEqual(outbound, 0)
            event_types, _events = self._event_types(
                path,
                dispatcher,
                "preguard-persistence-helper-code-a1",
            )
            self.assertEqual(event_types, ["SubmissionPrepared"])


if __name__ == "__main__":
    unittest.main()
