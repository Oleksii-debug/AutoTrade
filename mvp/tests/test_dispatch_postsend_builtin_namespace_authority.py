import builtins
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp import dispatch as dispatch_module
from mvp.autotrade_mvp.dispatch import ExactJsonTransportResponse, GuardedDispatcher
from mvp.autotrade_mvp.persistence import JournalStore


class PostSendBuiltinNamespaceAuthorityTests(unittest.TestCase):
    @staticmethod
    def _dispatcher(path: str) -> GuardedDispatcher:
        return GuardedDispatcher(
            JournalStore(path),
            environment="SIMULATION",
            account_id="acct",
            owner_token="owner",
        )

    @staticmethod
    def _events(path: str, dispatcher: GuardedDispatcher, attempt_id: str):
        return JournalStore.load_events(
            JournalStore(path),
            "submission_attempt",
            dispatcher._aggregate_id(attempt_id),
        )

    def _dispatch(self, dispatcher, attempt_id, transport):
        return dispatcher.dispatch(
            attempt_id=attempt_id,
            intent_id="intent-1",
            intent_hash="sha256:" + "1" * 64,
            provider="provider",
            request={"side": "BUY", "quantity": "1"},
            now="2026-10-06T19:10:00Z",
            authority_check=lambda *_args: (True, "allowed"),
            transport_send=transport,
            submission_scope={"endpoint": "/orders"},
        )

    def test_postsend_builtin_namespace_mutation_is_restored_without_execution(self):
        callbacks = 0
        original_vars = builtins.vars

        def hostile_vars(*_args):
            nonlocal callbacks
            callbacks += 1
            raise AssertionError("mutated builtins.vars executed")

        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            dispatcher = self._dispatcher(path)

            def transport(_client_order_id, _request, final_guard):
                final_guard()
                builtins.vars = hostile_vars
                return ExactJsonTransportResponse(
                    b'{"accepted":true}',
                    http_status=200,
                )

            try:
                result = self._dispatch(
                    dispatcher,
                    "builtin-namespace-vars-a1",
                    transport,
                )
            finally:
                builtins.vars = original_vars

            self.assertEqual(callbacks, 0)
            self.assertIs(builtins.vars, original_vars)
            self.assertEqual(result.status, "UNKNOWN")
            self.assertEqual(
                result.reason,
                "dispatcher_authority_changed_after_send_barrier",
            )
            self.assertEqual(
                [event["event_type"] for event in self._events(
                    path,
                    dispatcher,
                    "builtin-namespace-vars-a1",
                )],
                ["SubmissionPrepared", "SubmissionSending"],
            )

    def test_postsend_added_builtin_name_is_removed_and_fails_closed(self):
        poison_name = "__autotrade_dispatch_test_extra_builtin__"
        self.assertNotIn(poison_name, builtins.__dict__)

        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            dispatcher = self._dispatcher(path)
            outbound = 0

            def poisoned_transport(_client_order_id, _request, final_guard):
                nonlocal outbound
                final_guard()
                outbound += 1
                builtins.__dict__[poison_name] = object()
                return ExactJsonTransportResponse(
                    b'{"accepted":true}',
                    http_status=200,
                )

            try:
                first = self._dispatch(
                    dispatcher,
                    "builtin-extra-name-a1",
                    poisoned_transport,
                )
                self.assertEqual(first.status, "UNKNOWN")
                self.assertEqual(
                    first.reason,
                    "dispatcher_authority_changed_after_send_barrier",
                )
                self.assertNotIn(poison_name, builtins.__dict__)
                self.assertEqual(
                    [
                        event["event_type"]
                        for event in self._events(
                            path,
                            dispatcher,
                            "builtin-extra-name-a1",
                        )
                    ],
                    ["SubmissionPrepared", "SubmissionSending"],
                )

                second = self._dispatch(
                    dispatcher,
                    "builtin-extra-name-a2",
                    lambda _client_order_id, _request, final_guard: (
                        final_guard(),
                        ExactJsonTransportResponse(
                            b'{"accepted":true}',
                            http_status=200,
                        ),
                    )[1],
                )
                self.assertEqual(second.status, "SENT")
                self.assertEqual(second.reason, "sent_confirmed")
                self.assertEqual(outbound, 1)
                self.assertNotIn(poison_name, builtins.__dict__)
            finally:
                builtins.__dict__.pop(poison_name, None)

    def test_pre_send_added_builtin_name_is_removed_and_zero_wire(self):
        poison_name = "__autotrade_dispatch_test_extra_builtin_preguard__"
        self.assertNotIn(poison_name, builtins.__dict__)

        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            dispatcher = self._dispatcher(path)
            outbound = 0

            def authority_check(*_args):
                builtins.__dict__[poison_name] = object()
                return True, "allowed"

            def transport(_client_order_id, _request, final_guard):
                nonlocal outbound
                final_guard()
                outbound += 1
                return ExactJsonTransportResponse(
                    b'{"accepted":true}',
                    http_status=200,
                )

            try:
                with self.assertRaises(PermissionError):
                    dispatcher.dispatch(
                        attempt_id="builtin-extra-name-preguard-a1",
                        intent_id="intent-1",
                        intent_hash="sha256:" + "1" * 64,
                        provider="provider",
                        request={"side": "BUY", "quantity": "1"},
                        now="2026-10-06T19:10:00Z",
                        authority_check=authority_check,
                        transport_send=transport,
                        submission_scope={"endpoint": "/orders"},
                    )
                self.assertEqual(outbound, 0)
                self.assertNotIn(poison_name, builtins.__dict__)
                self.assertEqual(
                    [
                        event["event_type"]
                        for event in self._events(
                            path,
                            dispatcher,
                            "builtin-extra-name-preguard-a1",
                        )
                    ],
                    ["SubmissionPrepared"],
                )
            finally:
                builtins.__dict__.pop(poison_name, None)

    def test_unlisted_standard_builtin_binding_is_restored_for_next_dispatch(self):
        original_object = builtins.object

        class PoisonObject:
            def __new__(cls, *_args, **_kwargs):
                raise AssertionError("poisoned builtins.object executed")

        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            dispatcher = self._dispatcher(path)

            def first_transport(_client_order_id, _request, final_guard):
                final_guard()
                builtins.object = PoisonObject
                return ExactJsonTransportResponse(
                    b'{"accepted":true}',
                    http_status=200,
                )

            try:
                first = self._dispatch(
                    dispatcher,
                    "builtin-object-binding-a1",
                    first_transport,
                )
                self.assertEqual(first.status, "UNKNOWN")
                self.assertEqual(
                    first.reason,
                    "dispatcher_authority_changed_after_send_barrier",
                )
                self.assertIs(builtins.object, original_object)

                second = self._dispatch(
                    dispatcher,
                    "builtin-object-binding-a2",
                    lambda _client_order_id, _request, final_guard: (
                        final_guard(),
                        ExactJsonTransportResponse(
                            b'{"accepted":true}',
                            http_status=200,
                        ),
                    )[1],
                )
                self.assertEqual(second.status, "SENT")
                self.assertEqual(second.reason, "sent_confirmed")
            finally:
                builtins.object = original_object

    def test_postsend_builtin_module_binding_is_clean_before_next_dispatch(self):
        original_module = dispatch_module._builtins

        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            dispatcher = self._dispatcher(path)

            def first_transport(_client_order_id, _request, final_guard):
                final_guard()
                dispatch_module._builtins = object()
                return ExactJsonTransportResponse(
                    b'{"accepted":true}',
                    http_status=200,
                )

            first = self._dispatch(
                dispatcher,
                "builtin-module-binding-a1",
                first_transport,
            )
            self.assertIs(dispatch_module._builtins, original_module)
            self.assertEqual(first.status, "SENT")
            self.assertEqual(first.reason, "sent_confirmed")

            second = self._dispatch(
                dispatcher,
                "builtin-module-binding-a2",
                lambda _client_order_id, _request, final_guard: (
                    final_guard(),
                    ExactJsonTransportResponse(
                        b'{"accepted":true}',
                        http_status=200,
                    ),
                )[1],
            )
            self.assertEqual(second.status, "SENT")
            self.assertEqual(second.reason, "sent_confirmed")
            self.assertIs(dispatch_module._builtins, original_module)

    def test_postsend_builtin_exception_mutation_cannot_reclassify_transport_failure(self):
        original_exception = builtins.Exception

        class ForgedException(BaseException):
            pass

        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            dispatcher = self._dispatcher(path)

            def transport(_client_order_id, _request, final_guard):
                final_guard()
                builtins.Exception = ForgedException
                raise RuntimeError("provider result lost after send")

            try:
                result = self._dispatch(
                    dispatcher,
                    "builtin-namespace-exception-a1",
                    transport,
                )
            finally:
                builtins.Exception = original_exception

            self.assertIs(builtins.Exception, original_exception)
            self.assertEqual(result.status, "UNKNOWN")
            self.assertEqual(
                result.reason,
                "dispatcher_authority_changed_after_send_barrier",
            )
            self.assertEqual(
                [event["event_type"] for event in self._events(
                    path,
                    dispatcher,
                    "builtin-namespace-exception-a1",
                )],
                ["SubmissionPrepared", "SubmissionSending"],
            )


if __name__ == "__main__":
    unittest.main()
