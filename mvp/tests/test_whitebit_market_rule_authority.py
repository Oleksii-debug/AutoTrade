from decimal import Decimal
import unittest

from mvp.autotrade_mvp.whitebit import (
    WhiteBitMarketRules,
    WhiteBitOrderIntent,
    validate_intent_market_rules,
)


class WhiteBitMarketRuleAuthorityTests(unittest.TestCase):
    def test_market_rule_subclass_cannot_virtualize_provider_admission(self):
        intent = WhiteBitOrderIntent.create(
            instrument_version="BTC_USDT:v1",
            product_family="SPOT",
            market="BTC_USDT",
            side="BUY",
            order_type="LIMIT",
            amount="1",
            price="10",
        )
        calls = []

        class HostileRules(WhiteBitMarketRules):
            def __getattribute__(self, name):
                if name in {
                    "market",
                    "market_type",
                    "is_collateral",
                    "trades_enabled",
                    "step_size",
                    "tick_size",
                    "min_amount",
                    "min_total",
                    "max_total",
                    "delisted_at",
                }:
                    calls.append(name)
                    if name == "trades_enabled":
                        return True
                return super().__getattribute__(name)

        rules = HostileRules(
            market="BTC_USDT",
            market_type="SPOT",
            is_tradfi_futures=False,
            is_collateral=False,
            trades_enabled=False,
            step_size=Decimal("1"),
            tick_size=Decimal("1"),
            min_amount=Decimal("1"),
            min_total=Decimal("1"),
            max_total=Decimal("1000"),
            delisted_at=None,
        )

        with self.assertRaises(TypeError):
            validate_intent_market_rules(
                intent,
                rules,
                at=__import__("datetime").datetime(
                    2026,
                    9,
                    24,
                    20,
                    tzinfo=__import__("datetime").timezone.utc,
                ),
            )

        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
