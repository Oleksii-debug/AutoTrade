from __future__ import annotations

from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.authority import AuthorityService
from mvp.autotrade_mvp.dispatch import (
    ExactJsonTransportResponse,
    GuardedDispatcher,
)
from mvp.autotrade_mvp.persistence import JournalStore


class PaperLiveAuthorityIssuanceTests(unittest.TestCase):
    def test_caller_minted_financial_and_sender_callbacks_are_zero_wire(self):
        for environment in ("PAPER", "LIVE"):
            with self.subTest(environment=environment), TemporaryDirectory() as directory:
                store = JournalStore(f"{directory}/journal.sqlite3")
                dispatcher = GuardedDispatcher(
                    store,
                    environment=environment,
                    account_id="acct",
                    owner_token="caller-owner",
                    owner_epoch=1,
                )
                outbound = 0

                def transport(_client_order_id, _request, final_guard):
                    nonlocal outbound
                    final_guard()
                    outbound += 1
                    return ExactJsonTransportResponse(b'{"ok":true}')

                try:
                    result = dispatcher.dispatch(
                        attempt_id=f"forged-authority-{environment.lower()}",
                        intent_id="intent-1",
                        intent_hash="intent-hash-1",
                        provider="provider",
                        request={"symbol": "BTCUSDT", "quantity": "1"},
                        now="2026-09-24T18:00:00Z",
                        authority_check=lambda _hash, _now: (True, "allowed"),
                        transport_send=transport,
                        sender_check=lambda _owner, _epoch: None,
                    )
                except PermissionError:
                    result = None

                self.assertEqual(outbound, 0)
                if result is not None:
                    self.assertNotEqual(result.status, "SENT")

                events = JournalStore.load_events(
                    store,
                    "submission_attempt",
                    dispatcher._aggregate_id(
                        f"forged-authority-{environment.lower()}"
                    ),
                )
                self.assertNotIn(
                    "SubmissionSending",
                    [event["event_type"] for event in events],
                )


    def _issued_missing_admission_guard(
        self,
        store,
        *,
        environment: str,
        account_id: str,
    ):
        authority = AuthorityService(store)
        return authority.dispatch_guard(
            "missing-admission",
            account_id=account_id,
            environment=environment,
            instrument_id="CONTRACT:INSTRUMENT",
            instrument_version=1,
            action="ORDER.SUBMIT",
        )

    def test_issued_financial_authority_is_bound_to_exact_account_scope(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            guard = self._issued_missing_admission_guard(
                store,
                environment="PAPER",
                account_id="other-account",
            )
            dispatcher = GuardedDispatcher(
                store,
                environment="PAPER",
                account_id="acct",
                owner_token="owner",
            )
            outbound = 0

            def transport(_client_order_id, _request, _final_guard):
                nonlocal outbound
                outbound += 1
                return ExactJsonTransportResponse(b'{"ok":true}')

            with self.assertRaisesRegex(
                PermissionError,
                "another dispatcher scope",
            ):
                dispatcher.dispatch(
                    attempt_id="wrong-account-authority",
                    intent_id="intent-1",
                    intent_hash="intent-hash-1",
                    provider="provider",
                    request={},
                    now="2026-09-24T18:00:00Z",
                    authority_check=guard,
                    transport_send=transport,
                    sender_check=lambda _owner, _epoch: None,
                )
            self.assertEqual(outbound, 0)
            self.assertEqual(
                JournalStore.load_events(
                    store,
                    "submission_attempt",
                    dispatcher._aggregate_id("wrong-account-authority"),
                ),
                [],
            )

    def test_issued_financial_authority_is_bound_to_exact_environment_scope(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            guard = self._issued_missing_admission_guard(
                store,
                environment="LIVE",
                account_id="acct",
            )
            dispatcher = GuardedDispatcher(
                store,
                environment="PAPER",
                account_id="acct",
                owner_token="owner",
            )

            with self.assertRaisesRegex(
                PermissionError,
                "another dispatcher scope",
            ):
                dispatcher.dispatch(
                    attempt_id="wrong-environment-authority",
                    intent_id="intent-1",
                    intent_hash="intent-hash-1",
                    provider="provider",
                    request={},
                    now="2026-09-24T18:00:00Z",
                    authority_check=guard,
                    transport_send=lambda *_args: (_ for _ in ()).throw(
                        AssertionError("scope mismatch must be zero-wire")
                    ),
                    sender_check=lambda _owner, _epoch: None,
                )
            self.assertEqual(
                JournalStore.load_events(
                    store,
                    "submission_attempt",
                    dispatcher._aggregate_id("wrong-environment-authority"),
                ),
                [],
            )

    def test_same_scope_issued_authority_reaches_authority_decision(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            guard = self._issued_missing_admission_guard(
                store,
                environment="PAPER",
                account_id="acct",
            )
            dispatcher = GuardedDispatcher(
                store,
                environment="PAPER",
                account_id="acct",
                owner_token="owner",
            )
            result = dispatcher.dispatch(
                attempt_id="same-scope-authority",
                intent_id="intent-1",
                intent_hash="intent-hash-1",
                provider="provider",
                request={},
                now="2026-09-24T18:00:00Z",
                authority_check=guard,
                transport_send=lambda *_args: (_ for _ in ()).throw(
                    AssertionError("missing admission must be zero-wire")
                ),
                sender_check=lambda _owner, _epoch: None,
            )
            self.assertEqual(result.status, "BLOCKED")
            self.assertEqual(result.reason, "admission_missing")
            self.assertEqual(
                [
                    event["event_type"]
                    for event in JournalStore.load_events(
                        store,
                        "submission_attempt",
                        dispatcher._aggregate_id("same-scope-authority"),
                    )
                ],
                ["SubmissionPrepared", "SubmissionBlocked"],
            )


if __name__ == "__main__":
    unittest.main()
