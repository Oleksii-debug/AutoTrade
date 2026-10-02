from __future__ import annotations

from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.authority import AuthorityService
from mvp.autotrade_mvp.dispatch import (
    ExactJsonTransportResponse,
    GuardedDispatcher,
    _issue_financial_authority_check,
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


    def test_direct_low_level_issuer_cannot_wrap_arbitrary_same_store_callback(self):
        for environment in ("PAPER", "LIVE"):
            with self.subTest(environment=environment), TemporaryDirectory() as directory:
                store = JournalStore(f"{directory}/journal.sqlite3")
                with self.assertRaisesRegex(TypeError, "exact AuthorityService"):
                    _issue_financial_authority_check(
                        lambda _hash, _now: (True, "allowed"),
                        store=store,
                        admission_id="forged",
                        account_id="acct",
                        environment=environment,
                        instrument_id="11111111-1111-4111-8111-111111111111",
                        instrument_version=1,
                        action="ORDER.SUBMIT",
                    )

                self.assertEqual(
                    JournalStore.load_events(
                        store,
                        "submission_attempt",
                        "unused",
                    ),
                    [],
                )


    def test_genuine_issued_authority_for_other_journal_is_zero_wire_before_prepared(self):
        for environment in ("PAPER", "LIVE"):
            with (
                self.subTest(environment=environment),
                TemporaryDirectory() as selected_directory,
                TemporaryDirectory() as foreign_directory,
            ):
                selected_store = JournalStore(f"{selected_directory}/journal.sqlite3")
                foreign_store = JournalStore(f"{foreign_directory}/journal.sqlite3")
                dispatcher = GuardedDispatcher(
                    selected_store,
                    environment=environment,
                    account_id="acct",
                    owner_token="selected-owner",
                    owner_epoch=1,
                )
                foreign_service = AuthorityService(foreign_store)
                issued_elsewhere = _issue_financial_authority_check(
                    foreign_service,
                    store=foreign_store,
                    admission_id="missing-admission",
                    account_id="acct",
                    environment=environment,
                    instrument_id="11111111-1111-4111-8111-111111111111",
                    instrument_version=1,
                    action="ORDER.SUBMIT",
                    provider_id="PROVIDER",
                    provider_environment=environment,
                )
                outbound = 0

                def transport(_client_order_id, _request, final_guard):
                    nonlocal outbound
                    final_guard()
                    outbound += 1
                    return ExactJsonTransportResponse(b'{"ok":true}')

                attempt_id = f"foreign-issued-authority-{environment.lower()}"
                with self.assertRaisesRegex(
                    PermissionError,
                    "another journal authority",
                ):
                    dispatcher.dispatch(
                        attempt_id=attempt_id,
                        intent_id="intent-1",
                        intent_hash="intent-hash-1",
                        provider="provider",
                        request={"symbol": "BTCUSDT", "quantity": "1"},
                        now="2026-09-24T18:00:00Z",
                        authority_check=issued_elsewhere,
                        transport_send=transport,
                        sender_check=lambda _owner, _epoch: None,
                    )

                self.assertEqual(outbound, 0)
                events = JournalStore.load_events(
                    selected_store,
                    "submission_attempt",
                    dispatcher._aggregate_id(attempt_id),
                )
                self.assertEqual(events, [])



if __name__ == "__main__":
    unittest.main()
