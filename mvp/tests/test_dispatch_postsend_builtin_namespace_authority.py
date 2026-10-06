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
