"""Float-valued venue payload regression: CCXT deserializes JSON numerics as floats."""
import sys
import types
import unittest
from unittest.mock import patch

from autotrade_research.integrations.open_source_shadow import public_ohlcv_ccxt


class RealVenueNumericTests(unittest.TestCase):
    def test_float_prices_are_inert_decimal_strings(self):
        class PublicClient:
            def __init__(self, cfg): pass
            def fetch_ohlcv(self, symbol, timeframe, limit):
                return [[1000, 100.0, 110.0, 95.0, 105.0, 2.25]]
            def close(self): pass
        with patch.dict(sys.modules, {"ccxt": types.SimpleNamespace(kraken=PublicClient)}):
            rows = public_ohlcv_ccxt("kraken", "BTC/USD")
        self.assertEqual(rows[0]["close"], "105.0")
        self.assertEqual(rows[0]["volume"], "2.25")

    def test_nonfinite_venue_prices_are_rejected(self):
        class PublicClient:
            def __init__(self, cfg): pass
            def fetch_ohlcv(self, symbol, timeframe, limit):
                return [[1000, float("nan"), 110.0, 95.0, 105.0, 2.25]]
            def close(self): pass
        with patch.dict(sys.modules, {"ccxt": types.SimpleNamespace(kraken=PublicClient)}):
            with self.assertRaises(ValueError):
                public_ohlcv_ccxt("kraken", "BTC/USD")


if __name__ == "__main__":
    unittest.main()
