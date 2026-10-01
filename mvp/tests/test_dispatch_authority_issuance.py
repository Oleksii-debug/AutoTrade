from __future__ import annotations

from tempfile import TemporaryDirectory
import unittest

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


if __name__ == "__main__":
    unittest.main()
