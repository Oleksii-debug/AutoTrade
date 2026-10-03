from decimal import Decimal
import unittest

from mvp.autotrade_mvp import binance_spot
from mvp.autotrade_mvp import kraken_spot


class HostileDecimal(Decimal):
    """Decimal subtype whose virtual semantics must never reach provider authority."""

    def is_finite(self):
        raise AssertionError("Decimal subclass is_finite() was dispatched")

    def __format__(self, format_spec):
        raise AssertionError("Decimal subclass __format__() was dispatched")

    def __eq__(self, other):
        raise AssertionError("Decimal subclass equality was dispatched")

    def __lt__(self, other):
        raise AssertionError("Decimal subclass ordering was dispatched")

    def __le__(self, other):
        raise AssertionError("Decimal subclass ordering was dispatched")

    def __gt__(self, other):
        raise AssertionError("Decimal subclass ordering was dispatched")

    def __ge__(self, other):
        raise AssertionError("Decimal subclass ordering was dispatched")


class ProviderDecimalIngressTests(unittest.TestCase):
    def test_binance_order_intent_rejects_decimal_subclass_before_virtual_dispatch(self):
        hostile = HostileDecimal("1.25")

        with self.assertRaises(binance_spot.BinanceSpotAdapterError):
            binance_spot.BinanceSpotOrderIntent.create(
                instrument_version="BTC-USDT:1",
                symbol="BTCUSDT",
                side="BUY",
                order_type="MARKET",
                quantity=hostile,
            )

        with self.assertRaises(binance_spot.BinanceSpotAdapterError):
            binance_spot.BinanceSpotOrderIntent.create(
                instrument_version="BTC-USDT:1",
                symbol="BTCUSDT",
                side="BUY",
                order_type="LIMIT",
                quantity=Decimal("1"),
                price=hostile,
            )

    def test_kraken_order_intent_rejects_decimal_subclass_before_virtual_dispatch(self):
        hostile = HostileDecimal("1.25")

        with self.assertRaises(kraken_spot.KrakenSpotAdapterError):
            kraken_spot.KrakenSpotOrderIntent.create(
                instrument_version="BTC-USD:1",
                pair="BTC/USD",
                side="BUY",
                order_type="MARKET",
                volume=hostile,
            )

        with self.assertRaises(kraken_spot.KrakenSpotAdapterError):
            kraken_spot.KrakenSpotOrderIntent.create(
                instrument_version="BTC-USD:1",
                pair="BTC/USD",
                side="BUY",
                order_type="LIMIT",
                volume=Decimal("1"),
                price=hostile,
            )


if __name__ == "__main__":
    unittest.main()
