from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.accounting import EconomicBook, book_equity_fill, project_equity_position
from mvp.autotrade_mvp.corporate_action_accounting import commit_authoritative_corporate_action
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.tests.test_corporate_action_accounting import (
    READ_NOW,
    economic_book,
    evidence_store,
    pure_book,
    resolve_action,
    sealed_action,
)


def ordinary_fill(*, suffix: str, side: str, quantity: str, price: str, offset: int):
    return book_equity_fill(
        transaction_id=f"ordinary-fill-{suffix}",
        cause_event_id=f"provider-fill:ordinary-fill-{suffix}",
        instrument="BTCUSDT",
        settlement_currency="USDT",
        side=side,
        quantity=quantity,
        price=price,
        economic_effective_at=(READ_NOW + timedelta(seconds=offset)).isoformat().replace(
            "+00:00", "Z"
        ),
        economic_order_key=f"provider:BINANCE:execution:ordinary-{suffix}",
        observed_at=(READ_NOW + timedelta(seconds=offset + 1)).isoformat().replace(
            "+00:00", "Z"
        ),
    )


def later_split():
    # Put the second action on the next UTC day so retained corporate-event
    # chronology does not depend on a fabricated same-day source sequence.
    return resolve_action(
        sealed_action(
            external_event_id="corp-later-split",
            revision="2",
            kind="SPLIT",
            numerator="2",
            denominator="1",
            effective_offset=86401,
            observed_offset=86402,
        )
    )


class DurableCorporateActionPrestateTests(unittest.TestCase):
    def _commit_first_split(self, store, economics, pure):
        first = resolve_action(
            sealed_action(
                external_event_id="corp-first-split",
                revision="1",
                kind="SPLIT",
                numerator="2",
                denominator="1",
            )
        )
        result = commit_authoritative_corporate_action(
            store=store,
            evidence_store=evidence_store(store),
            economic_book=economics,
            corporate_book=pure,
            accepted=first,
        )
        pure.apply(result.accepted_event)
        self.assertEqual(pure.state.quantity, Decimal("20"))
        self.assertEqual(pure.state.total_basis, Decimal("1000"))
        return result

    def test_later_split_derives_quantity_and_basis_after_intervening_fill(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            store = JournalStore(path)
            economics = economic_book(store)
            pure = pure_book()
            self._commit_first_split(store, economics, pure)

            economics.append(
                ordinary_fill(
                    suffix="buy-after-split",
                    side="BUY",
                    quantity="10",
                    price="200",
                    offset=10,
                )
            )
            before_later = project_equity_position(
                EconomicBook(economics.transactions),
                instrument="BTCUSDT",
                settlement_currency="USDT",
            )
            self.assertEqual(before_later.quantity, Decimal("30"))
            self.assertEqual(before_later.open_cost_basis, Decimal("3000"))
            self.assertEqual(pure.state.quantity, Decimal("20"))
            self.assertEqual(pure.state.total_basis, Decimal("1000"))

            accepted = later_split()
            result = commit_authoritative_corporate_action(
                store=store,
                evidence_store=evidence_store(store),
                economic_book=economics,
                corporate_book=pure,
                accepted=accepted,
            )

            self.assertEqual(result.transition.before.quantity, Decimal("30"))
            self.assertEqual(result.transition.before.total_basis, Decimal("3000"))
            self.assertEqual(result.next_state.quantity, Decimal("60"))
            self.assertEqual(result.next_state.total_basis, Decimal("3000"))
            after_later = project_equity_position(
                EconomicBook(economics.transactions),
                instrument="BTCUSDT",
                settlement_currency="USDT",
            )
            self.assertEqual(after_later.quantity, Decimal("60"))
            self.assertEqual(after_later.open_cost_basis, Decimal("3000"))

            reopened = JournalStore(path)
            restarted = economic_book(reopened)
            retry = commit_authoritative_corporate_action(
                store=reopened,
                evidence_store=evidence_store(reopened),
                economic_book=restarted,
                corporate_book=pure,
                accepted=accepted,
            )
            self.assertFalse(retry.inserted)
            self.assertEqual(retry.transition.before.quantity, Decimal("30"))
            self.assertEqual(retry.transition.before.total_basis, Decimal("3000"))
            self.assertEqual(retry.next_state.quantity, Decimal("60"))
            self.assertEqual(len(restarted.transactions), 4)

    def test_round_trip_quantity_cannot_hide_changed_durable_basis(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            store = JournalStore(path)
            economics = economic_book(store)
            pure = pure_book()
            self._commit_first_split(store, economics, pure)

            economics.append(
                ordinary_fill(
                    suffix="roundtrip-buy",
                    side="BUY",
                    quantity="10",
                    price="200",
                    offset=10,
                )
            )
            economics.append(
                ordinary_fill(
                    suffix="roundtrip-sell",
                    side="SELL",
                    quantity="10",
                    price="150",
                    offset=20,
                )
            )
            before_later = project_equity_position(
                EconomicBook(economics.transactions),
                instrument="BTCUSDT",
                settlement_currency="USDT",
            )
            # Durable quantity has returned to the stale pure-book quantity, but
            # FIFO basis has materially changed. Quantity equality alone cannot
            # authorize the next corporate action.
            self.assertEqual(before_later.quantity, Decimal("20"))
            self.assertEqual(before_later.open_cost_basis, Decimal("2500"))
            self.assertEqual(pure.state.quantity, Decimal("20"))
            self.assertEqual(pure.state.total_basis, Decimal("1000"))

            accepted = later_split()
            result = commit_authoritative_corporate_action(
                store=store,
                evidence_store=evidence_store(store),
                economic_book=economics,
                corporate_book=pure,
                accepted=accepted,
            )
            self.assertEqual(result.transition.before.quantity, Decimal("20"))
            self.assertEqual(result.transition.before.total_basis, Decimal("2500"))
            self.assertEqual(result.next_state.quantity, Decimal("40"))
            self.assertEqual(result.next_state.total_basis, Decimal("2500"))

            activation = next(
                event
                for event in store.load_events_by_aggregate_type(
                    "corporate_action_activation"
                )
                if event["payload"]["external_event_id"] == "corp-later-split"
            )
            digest = activation["payload"]["entitlement_position_digest"]
            self.assertTrue(isinstance(digest, str) and bool(digest))

            reopened = JournalStore(path)
            restarted = economic_book(reopened)
            retry = commit_authoritative_corporate_action(
                store=reopened,
                evidence_store=evidence_store(reopened),
                economic_book=restarted,
                corporate_book=pure,
                accepted=accepted,
            )
            self.assertFalse(retry.inserted)
            retry_activation = next(
                event
                for event in reopened.load_events_by_aggregate_type(
                    "corporate_action_activation"
                )
                if event["payload"]["external_event_id"] == "corp-later-split"
            )
            self.assertEqual(
                retry_activation["payload"]["entitlement_position_digest"],
                digest,
            )
            self.assertEqual(len(restarted.transactions), 5)
            projection = project_equity_position(
                EconomicBook(restarted.transactions),
                instrument="BTCUSDT",
                settlement_currency="USDT",
            )
            self.assertEqual(projection.quantity, Decimal("40"))
            self.assertEqual(projection.open_cost_basis, Decimal("2500"))


if __name__ == "__main__":
    unittest.main()
