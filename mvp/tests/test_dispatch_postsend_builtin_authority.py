from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp import dispatch as dispatch_module
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


if __name__ == "__main__":
    unittest.main()
