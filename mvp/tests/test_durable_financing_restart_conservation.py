from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.durable_financing import (
    DurableFinancingBook,
    _event_payload,
    _revision_book_digest,
)
from mvp.autotrade_mvp.financing import (
    FinancingConflict,
    FinancingEvent,
    FinancingRevisionBook,
    book_financing_delta,
)
from mvp.autotrade_mvp.persistence import JournalStore, payload_digest
from mvp.autotrade_mvp.provider_activity_accounting import DurableProviderEconomicBook


BASE = datetime(2026, 9, 29, 0, 0, tzinfo=timezone.utc)
CHARGE_ID = "restart-financing-charge"


class DurableFinancingRestartConservationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.path = Path(self.temp.name) / "journal.sqlite3"
        self.store = JournalStore(self.path)
        self.economic = DurableProviderEconomicBook(
            self.store,
            provider_id="BYBIT",
            account_id="acct-1",
            environment="SIMULATION",
        )
        self.financing = DurableFinancingBook(
            self.store,
            self.economic,
            provider_id="BYBIT",
            account_id="acct-1",
            environment="SIMULATION",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _event(self, *, kind: str = "FINAL", amount: str = "1.20") -> FinancingEvent:
        return FinancingEvent.create(
            charge_id=CHARGE_ID,
            revision=1,
            kind=kind,
            effective_at=BASE,
            available_at=BASE,
            unit="BTC",
            amount=amount,
            source_account="BORROW_LIABILITY:BTC",
            evidence_ref="artifact:restart-conservation",
        )

    def _append_financing_only(
        self,
        event: FinancingEvent,
        *,
        event_id: str = "restart-financing-event-r1",
    ) -> tuple[str, Decimal]:
        book = FinancingRevisionBook()
        update = book.record(event)
        aggregate_id = self.financing._aggregate_id(event.charge_id)
        payload = _event_payload(
            provider_id="BYBIT",
            account_id="acct-1",
            environment="SIMULATION",
            event=event,
            artifact_id="restart-conservation-artifact",
            artifact_digest="sha256:" + "a" * 64,
            charge_scope_type="ACCOUNT",
            charge_scope_id="acct-1",
            previous_revision_digest=_revision_book_digest([]),
            resulting_revision_digest=_revision_book_digest(list(book.events)),
            resulting_final_charge=update.current_final_charge,
            economic_delta=update.economic_delta,
        )
        self.store.append_event(
            {
                "event_id": event_id,
                "event_type": "ProviderFinancingRevisionAccepted",
                "aggregate_type": "provider_financing_charge",
                "aggregate_id": aggregate_id,
                "aggregate_version": "1",
                "committed_at": BASE.isoformat(),
                "payload": payload,
                "payload_hash": payload_digest(payload),
            }
        )
        return aggregate_id, update.economic_delta

    def _reopened(self) -> tuple[DurableProviderEconomicBook, DurableFinancingBook]:
        store = JournalStore(self.path)
        economic = DurableProviderEconomicBook(
            store,
            provider_id="BYBIT",
            account_id="acct-1",
            environment="SIMULATION",
        )
        financing = DurableFinancingBook(
            store,
            economic,
            provider_id="BYBIT",
            account_id="acct-1",
            environment="SIMULATION",
        )
        return economic, financing

    def test_restart_rejects_final_revision_missing_required_economics(self) -> None:
        self._append_financing_only(self._event())
        _, restarted = self._reopened()

        with self.assertRaisesRegex(FinancingConflict, "missing its economic posting"):
            restarted.latest(CHARGE_ID)

    def test_restart_rejects_economics_for_indicated_revision(self) -> None:
        event = self._event(kind="INDICATED", amount="2.50")
        _, delta = self._append_financing_only(event)
        self.assertEqual(delta, Decimal("0"))
        self.economic.append(
            book_financing_delta(
                transaction_id="stray-indicated-economic",
                cause_event_id="restart-financing-event-r1",
                unit="BTC",
                source_account="BORROW_LIABILITY:BTC",
                economic_delta="1",
            )
        )
        _, restarted = self._reopened()

        with self.assertRaisesRegex(FinancingConflict, "non-economic"):
            restarted.latest(CHARGE_ID)

    def test_restart_rejects_changed_financing_economic_transaction(self) -> None:
        event = self._event()
        aggregate_id, _ = self._append_financing_only(event)
        wrong = self.financing._economic_transaction(
            aggregate_id=aggregate_id,
            event_id="restart-financing-event-r1",
            event=event,
            economic_delta=Decimal("1.19"),
        )
        self.economic.append(wrong)
        _, restarted = self._reopened()

        with self.assertRaisesRegex(FinancingConflict, "does not match canonical"):
            restarted.latest(CHARGE_ID)

    def test_restart_rejects_duplicate_economics_for_one_financing_cause(self) -> None:
        event = self._event()
        aggregate_id, delta = self._append_financing_only(event)
        expected = self.financing._economic_transaction(
            aggregate_id=aggregate_id,
            event_id="restart-financing-event-r1",
            event=event,
            economic_delta=delta,
        )
        duplicate = book_financing_delta(
            transaction_id="duplicate-financing-economic",
            cause_event_id="restart-financing-event-r1",
            unit="BTC",
            source_account="BORROW_LIABILITY:BTC",
            economic_delta="0.01",
        )
        self.economic.append_batch((expected, duplicate))
        _, restarted = self._reopened()

        with self.assertRaisesRegex(FinancingConflict, "duplicate economic postings"):
            restarted.latest(CHARGE_ID)

    def test_live_read_refreshes_economic_projection_before_conservation_check(self) -> None:
        event = self._event()
        aggregate_id, delta = self._append_financing_only(event)

        # Mutate the shared durable economic authority through a separate facade,
        # leaving self.economic intentionally stale until financing replay refreshes it.
        separate_economic = DurableProviderEconomicBook(
            self.store,
            provider_id="BYBIT",
            account_id="acct-1",
            environment="SIMULATION",
        )
        separate_economic.append(
            self.financing._economic_transaction(
                aggregate_id=aggregate_id,
                event_id="restart-financing-event-r1",
                event=event,
                economic_delta=delta,
            )
        )

        latest = self.financing.latest(CHARGE_ID)
        self.assertIsNotNone(latest)
        self.assertEqual(latest, event)
        self.assertEqual(len(self.economic.transactions), 1)


if __name__ == "__main__":
    unittest.main()
