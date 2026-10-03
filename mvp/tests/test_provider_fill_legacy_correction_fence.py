from decimal import Decimal
import unittest
from unittest.mock import patch

from mvp.autotrade_mvp.accounting import (
    AccountingConflict,
    JournalTransaction,
    Posting,
)
from mvp.autotrade_mvp import provider_activity_accounting as accounting_bridge


def correction_transactions():
    original_id = "provider-owned-transaction"
    reversal = JournalTransaction(
        transaction_id="provider-owned-reversal",
        cause_event_id="correction-evidence",
        reverses_transaction_id=original_id,
        postings=(
            Posting("CASH:USD", "USD", Decimal("100")),
            Posting("ASSET:ABC", "ABC", Decimal("-1")),
        ),
    )
    replacement = JournalTransaction(
        transaction_id="provider-owned-replacement",
        cause_event_id="correction-evidence",
        corrects_transaction_id=original_id,
        postings=(
            Posting("CASH:USD", "USD", Decimal("-110")),
            Posting("ASSET:ABC", "ABC", Decimal("1.1")),
        ),
    )
    return reversal, replacement


class ProviderFillLegacyCorrectionFenceTests(unittest.TestCase):
    def test_fresh_generic_correction_of_provider_owned_transaction_fails_before_delegate(self):
        reversal, replacement = correction_transactions()
        with (
            patch.object(
                accounting_bridge,
                "_provider_fill_transaction_is_bound",
                return_value=True,
            ),
            patch.object(
                accounting_bridge,
                "_legacy_provider_fill_correction_is_already_durable",
                return_value=False,
            ),
            patch.object(
                accounting_bridge._impl,
                "commit_economic_correction_with_settlement_replacement",
                side_effect=AssertionError("fresh legacy correction must not delegate"),
            ),
        ):
            with self.assertRaisesRegex(
                AccountingConflict,
                "reservation-aware correction authority",
            ):
                accounting_bridge.commit_economic_correction_with_settlement_replacement(
                    object(),
                    object(),
                    command_id="correction-command",
                    idempotency_key="correction-idempotency",
                    reversal=reversal,
                    replacement=replacement,
                    settlement_obligations=(),
                )

    def test_already_durable_legacy_correction_preserves_exact_retry_path(self):
        reversal, replacement = correction_transactions()
        with (
            patch.object(
                accounting_bridge,
                "_provider_fill_transaction_is_bound",
                return_value=True,
            ),
            patch.object(
                accounting_bridge,
                "_legacy_provider_fill_correction_is_already_durable",
                return_value=True,
            ),
            patch.object(
                accounting_bridge._impl,
                "commit_economic_correction_with_settlement_replacement",
                return_value=False,
            ) as delegate,
        ):
            inserted = accounting_bridge.commit_economic_correction_with_settlement_replacement(
                object(),
                object(),
                command_id="legacy-retry-command",
                idempotency_key="legacy-retry-idempotency",
                reversal=reversal,
                replacement=replacement,
                settlement_obligations=(),
            )
        self.assertFalse(inserted)
        delegate.assert_called_once()


if __name__ == "__main__":
    unittest.main()
