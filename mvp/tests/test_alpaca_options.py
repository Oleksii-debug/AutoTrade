from datetime import datetime, timedelta, timezone
import unittest

from mvp.autotrade_mvp.alpaca import AlpacaAdapterError, AlpacaOrderIntent
from mvp.autotrade_mvp.alpaca_options import (
    AlpacaOptionAccountEvidence,
    parse_polled_option_activity,
    require_option_entitlement,
)


NOW = datetime(2026, 9, 25, 0, tzinfo=timezone.utc)


def option_intent():
    return AlpacaOrderIntent.create(
        instrument_version="AAPL_OPT:v1",
        asset_class="OPTION",
        symbol="AAPL261218C00250000",
        side="BUY",
        order_type="LIMIT",
        time_in_force="GTC",
        quantity="1",
        limit_price="5.25",
        position_intent="buy_to_open",
    )


def account(**overrides):
    values = {
        "account_id": "paper-account",
        "environment": "PAPER",
        "observed_at": NOW - timedelta(minutes=5),
        "expires_at": NOW + timedelta(minutes=5),
        "options_trading_level": 2,
        "trading_blocked": False,
        "account_blocked": False,
        "source_sha256": "a" * 64,
    }
    values.update(overrides)
    return AlpacaOptionAccountEvidence(**values)


class AlpacaOptionEvidenceTests(unittest.TestCase):
    def test_fresh_level_evidence_admits_matching_option_intent(self):
        intent = option_intent()
        self.assertIs(
            require_option_entitlement(
                intent,
                account=account(),
                required_level=2,
                at=NOW,
            ),
            intent,
        )

    def test_stale_or_blocked_account_fails_closed(self):
        with self.assertRaisesRegex(AlpacaAdapterError, "does not admit"):
            require_option_entitlement(
                option_intent(),
                account=account(expires_at=NOW - timedelta(minutes=1)),
                required_level=2,
                at=NOW,
            )
        with self.assertRaisesRegex(AlpacaAdapterError, "does not admit"):
            require_option_entitlement(
                option_intent(),
                account=account(trading_blocked=True),
                required_level=2,
                at=NOW,
            )

    def test_insufficient_option_level_fails_closed(self):
        with self.assertRaisesRegex(AlpacaAdapterError, "does not admit"):
            require_option_entitlement(
                option_intent(),
                account=account(options_trading_level=1),
                required_level=2,
                at=NOW,
            )

    def test_non_option_intent_cannot_use_option_entitlement(self):
        equity = AlpacaOrderIntent.create(
            instrument_version="AAPL:v1",
            asset_class="EQUITY",
            symbol="AAPL",
            side="BUY",
            order_type="MARKET",
            time_in_force="DAY",
            quantity="1",
        )
        with self.assertRaisesRegex(AlpacaAdapterError, "OPTION intents"):
            require_option_entitlement(
                equity,
                account=account(),
                required_level=1,
                at=NOW,
            )

    def test_assignment_is_parsed_from_polled_activity(self):
        result = parse_polled_option_activity(
            {
                "activity_type": "OPASN",
                "id": "assignment-1",
                "symbol": "AAPL261218C00250000",
                "transaction_time": "2026-09-24T23:59:00Z",
            },
            observed_at=NOW,
        )
        self.assertEqual(result.activity_type, "OPTION_ASSIGNMENT")
        self.assertEqual(result.symbol, "AAPL261218C00250000")
        self.assertEqual(result.provider_activity_id, "assignment-1")

    def test_unknown_activity_does_not_become_assignment(self):
        with self.assertRaisesRegex(AlpacaAdapterError, "unsupported polled"):
            parse_polled_option_activity(
                {
                    "activity_type": "FILL",
                    "id": "fill-1",
                    "symbol": "AAPL261218C00250000",
                    "transaction_time": "2026-09-24T23:59:00Z",
                },
                observed_at=NOW,
            )


if __name__ == "__main__":
    unittest.main()
