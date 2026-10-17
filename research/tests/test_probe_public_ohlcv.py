"""CLI contract: no credentials, no trading route, bounded public output."""
import io
import json
from pathlib import Path
import runpy
import sys
import types
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch


class ProbeTests(unittest.TestCase):
    def test_public_probe_emits_only_inert_market_data(self):
        root = Path(__file__).resolve().parents[2]
        path = root / "tools" / "probe_public_ohlcv.py"
        # run_path gives a named namespace without executing the __main__ branch.
        api = runpy.run_path(str(path), run_name="research_probe")
        class PublicClient:
            def __init__(self, cfg):
                assert "apiKey" not in cfg
                assert "secret" not in cfg
            def fetch_ohlcv(self, symbol, timeframe, limit):
                return [[1000, 100.0, 101.0, 99.0, 100.5, 10.0]]
            def close(self): pass
        output = io.StringIO()
        with patch.dict(sys.modules, {"ccxt": types.SimpleNamespace(binance=PublicClient)}):
            with redirect_stdout(output):
                self.assertEqual(api["main"](["--exchange", "binance", "--symbol", "BTC/USDT", "--limit", "1"]), 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result["execution_authority"], "NONE")
        self.assertEqual(result["bars"][0]["close"], "100.5")
        self.assertEqual(result["environment"], "PUBLIC_MARKET_DATA_ONLY")

if __name__ == "__main__":
    unittest.main()
