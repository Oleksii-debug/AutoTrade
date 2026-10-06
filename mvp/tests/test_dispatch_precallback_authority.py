from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp import dispatch as dispatch_module
from mvp.autotrade_mvp.dispatch import ExactJsonTransportResponse, GuardedDispatcher
from mvp.autotrade_mvp.persistence import JournalStore


class PreCallbackAuthorityTests(unittest.TestCase):
    @staticmethod
    def _dispatcher(path: str) -> GuardedDispatcher:
        return GuardedDispatcher(
            JournalStore(path),
            environment="SIMULATION",
            account_id="acct",
            owner_token="owner",
        )

    @staticmethod
    def _event_types(path: str, dispatcher: GuardedDispatcher, attempt_id: str):
        events = JournalStore.load_events(
            JournalStore(path),
            "submission_attempt",
            dispatcher._aggregate_id(attempt_id),
        )
        return [event["event_type"] for event in events]

    @staticmethod
    def _dispatch(dispatcher, attempt_id, authority_check, transport, **kwargs):
        return dispatcher.dispatch(
            attempt_id=attempt_id,
            intent_id="intent-1",
            intent_hash="sha256:" + "1" * 64,
            provider="provider",
            request={"side": "BUY", "quantity": "1"},
            now="2026-10-06T19:35:00Z",
            authority_check=authority_check,
            transport_send=transport,
            submission_scope={"endpoint": "/orders"},
            **kwargs,
        )

    def test_initial_authority_callback_cannot_poison_result_validator_baseline(self):
        original = dispatch_module._validated_authority_result
        forged_calls = 0
        outbound = 0

        def forged(_value):
            nonlocal forged_calls
            forged_calls += 1
            return True, "forged"

        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            dispatcher = self._dispatcher(path)

            def authority_check(_intent_hash, _now):
                dispatch_module._validated_authority_result = forged
                return True, "allowed"

            def transport(_client_order_id, _request, _final_guard):
                nonlocal outbound
                outbound += 1
                raise AssertionError("transport must remain zero-wire")

            try:
                with self.assertRaises(dispatch_module._DispatchAuthorityChanged):
                    self._dispatch(
                        dispatcher,
                        "precallback-initial-authority-a1",
                        authority_check,
                        transport,
                    )
            finally:
                dispatch_module._validated_authority_result = original

            self.assertIs(dispatch_module._validated_authority_result, original)
            self.assertEqual(forged_calls, 0)
            self.assertEqual(outbound, 0)
            self.assertEqual(
                self._event_types(
                    path,
                    dispatcher,
                    "precallback-initial-authority-a1",
                ),
                ["SubmissionPrepared"],
            )

    def test_final_barrier_clock_cannot_execute_rebound_instant_helper(self):
        original = dispatch_module._instant
        forged_calls = 0
        outbound = 0

        def forged(_value):
            nonlocal forged_calls
            forged_calls += 1
            raise AssertionError("forged _instant executed")

        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            dispatcher = self._dispatcher(path)

            def clock():
                dispatch_module._instant = forged
                return "2026-10-06T19:35:01Z"

            def transport(_client_order_id, _request, final_guard):
                nonlocal outbound
                final_guard()
                outbound += 1
                return ExactJsonTransportResponse(
                    b'{"accepted":true}',
                    http_status=200,
                )

            try:
                with self.assertRaises(dispatch_module._DispatchAuthorityChanged):
                    self._dispatch(
                        dispatcher,
                        "precallback-clock-a1",
                        lambda *_args: (True, "allowed"),
                        transport,
                        final_barrier_clock=clock,
                    )
            finally:
                dispatch_module._instant = original

            self.assertIs(dispatch_module._instant, original)
            self.assertEqual(forged_calls, 0)
            self.assertEqual(outbound, 0)
            self.assertEqual(
                self._event_types(path, dispatcher, "precallback-clock-a1"),
                ["SubmissionPrepared"],
            )

    def test_final_authority_callback_cannot_poison_result_validator(self):
        original = dispatch_module._validated_authority_result
        forged_calls = 0
        outbound = 0
        checks = 0

        def forged(_value):
            nonlocal forged_calls
            forged_calls += 1
            raise AssertionError("forged authority validator executed")

        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            dispatcher = self._dispatcher(path)

            def authority_check(_intent_hash, _now):
                nonlocal checks
                checks += 1
                if checks == 2:
                    dispatch_module._validated_authority_result = forged
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
                with self.assertRaises(dispatch_module._DispatchAuthorityChanged):
                    self._dispatch(
                        dispatcher,
                        "precallback-final-authority-a1",
                        authority_check,
                        transport,
                    )
            finally:
                dispatch_module._validated_authority_result = original

            self.assertEqual(checks, 2)
            self.assertIs(dispatch_module._validated_authority_result, original)
            self.assertEqual(forged_calls, 0)
            self.assertEqual(outbound, 0)
            self.assertEqual(
                self._event_types(
                    path,
                    dispatcher,
                    "precallback-final-authority-a1",
                ),
                ["SubmissionPrepared"],
            )


if __name__ == "__main__":
    unittest.main()
