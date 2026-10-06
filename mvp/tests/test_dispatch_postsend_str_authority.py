from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp import dispatch as dispatch_module
from mvp.autotrade_mvp.dispatch import (
    ExactJsonTransportResponse,
    GuardedDispatcher,
)
from mvp.autotrade_mvp.persistence import JournalStore


class PostsendStrAuthorityTests(unittest.TestCase):
    @staticmethod
    def _dispatcher(path: str) -> GuardedDispatcher:
        return GuardedDispatcher(
            JournalStore(path),
            environment="SIMULATION",
            account_id="acct",
            owner_token="owner",
        )

    def _dispatch(self, dispatcher, *, attempt_id, transport):
        return dispatcher.dispatch(
            attempt_id=attempt_id,
            intent_id="intent-1",
            intent_hash="sha256:" + "1" * 64,
            provider="provider",
            request={"side": "BUY", "quantity": "1"},
            now="2026-10-06T18:30:00Z",
            authority_check=lambda *_args: (True, "allowed"),
            transport_send=transport,
            submission_scope={"endpoint": "/orders"},
        )

    @staticmethod
    def _events(path: str, dispatcher: GuardedDispatcher, attempt_id: str):
        return JournalStore.load_events(
            JournalStore(path),
            "submission_attempt",
            dispatcher._aggregate_id(attempt_id),
        )

    def test_exact_response_uses_pretransport_str_builtin(self):
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
                response = ExactJsonTransportResponse(
                    b'{"accepted":true}',
                    http_status=200,
                )
                dispatch_module.str = hostile_str
                return response

            try:
                result = self._dispatch(
                    dispatcher,
                    attempt_id="postsend-str-exact-a1",
                    transport=transport,
                )
            finally:
                if had_global:
                    dispatch_module.str = original
                else:
                    vars(dispatch_module).pop("str", None)

            self.assertEqual(callbacks, 0)
            self.assertEqual(result.status, "SENT")
            self.assertEqual(result.reason, "sent_confirmed")
            self.assertEqual(result.response, {"accepted": True})
            self.assertEqual(
                [event["event_type"] for event in self._events(
                    path,
                    dispatcher,
                    "postsend-str-exact-a1",
                )],
                ["SubmissionPrepared", "SubmissionSending", "SubmissionSent"],
            )

    def test_transport_exception_uses_pretransport_str_builtin(self):
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
                raise RuntimeError("provider result lost after send")

            try:
                result = self._dispatch(
                    dispatcher,
                    attempt_id="postsend-str-error-a1",
                    transport=transport,
                )
            finally:
                if had_global:
                    dispatch_module.str = original
                else:
                    vars(dispatch_module).pop("str", None)

            self.assertEqual(callbacks, 0)
            self.assertEqual(result.status, "UNKNOWN")
            self.assertEqual(result.reason, "transport_result_ambiguous")
            events = self._events(
                path,
                dispatcher,
                "postsend-str-error-a1",
            )
            self.assertEqual(
                [event["event_type"] for event in events],
                ["SubmissionPrepared", "SubmissionSending", "SubmissionUnknown"],
            )
            self.assertEqual(
                events[-1]["payload"]["reason"],
                "transport_exception_after_send_barrier:RuntimeError",
            )


if __name__ == "__main__":
    unittest.main()
