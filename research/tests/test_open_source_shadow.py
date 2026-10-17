"""Dependency-isolated characterization for optional CCXT/River/Optuna bridges."""
import sys
import types
import unittest
from unittest.mock import patch

from autotrade_research.integrations.open_source_shadow import (
    optuna_shadow_threshold, public_ohlcv_ccxt, river_shadow_probability,
)


class PublicCcxtTests(unittest.TestCase):
    def test_public_only_and_decimal_candles(self):
        captured = []

        class Client:
            def __init__(self, config):
                captured.append(config)
            def fetch_ohlcv(self, symbol, timeframe, limit):
                captured.append((symbol, timeframe, limit))
                return [[1000, 100, 110, 95, 105, 8], [2000, 105, 108, 98, 100, 9]]
            def close(self):
                captured.append("closed")
        with patch.dict(sys.modules, {"ccxt": types.SimpleNamespace(binance=Client)}):
            rows = public_ohlcv_ccxt("binance", "BTC/USDT", limit=2)
        self.assertEqual(rows[0]["close"], "105")
        self.assertEqual(captured, [{"enableRateLimit": True, "timeout": 10000},
                                    ("BTC/USDT", "1h", 2), "closed"])
        self.assertRaises(ValueError, public_ohlcv_ccxt, "untrusted", "BTC/USDT")

    def test_corrupt_rows_fail_and_close_client(self):
        destroyed = []
        class Client:
            def __init__(self, config): pass
            def fetch_ohlcv(self, symbol, timeframe, limit):
                return [[1000, 100, 110, 95, 105, 8], [1000, 100, 110, 95, 105, 8]]
            def close(self): destroyed.append(True)
        with patch.dict(sys.modules, {"ccxt": types.SimpleNamespace(binance=Client)}):
            self.assertRaises(ValueError, public_ohlcv_ccxt, "binance", "BTC/USDT")
        self.assertEqual(destroyed, [True])


class RiverTests(unittest.TestCase):
    def test_train_only_closed_labels_and_no_authority(self):
        seen = []
        class LogisticRegression:
            def learn_one(self, row, label): seen.append((row, label))
            def predict_proba_one(self, row): return {True: 0.6}
        fake = types.SimpleNamespace(linear_model=types.SimpleNamespace(LogisticRegression=LogisticRegression))
        data = [{"observed_at_ms": 100, "label_available_at_ms": 200,
                 "label": 1, "features": {"momentum": "0.3"}}]
        with patch.dict(sys.modules, {"river": fake}):
            result = river_shadow_probability(data, {"momentum": "0.2"}, 201)
        self.assertEqual(result["probability_up"], "0.6")
        self.assertEqual(result["trading_authority"], "NONE")
        self.assertEqual(len(seen), 1)
        self.assertRaises(ValueError, river_shadow_probability, data, {"momentum": "0.2"}, 199)
        self.assertRaises(ValueError, river_shadow_probability, data, {"momentum": "nan"}, 201)


class OptunaTests(unittest.TestCase):
    def test_development_only_no_promotion(self):
        class Trial:
            def suggest_float(self, key, low, high): return 0.5
        class Study:
            def optimize(self, callback, n_trials, n_jobs, show_progress_bar):
                self.best_value = callback(Trial())
                self.best_params = {"threshold": 0.5}
        fake = types.SimpleNamespace(samplers=types.SimpleNamespace(TPESampler=lambda seed: seed),
                                     create_study=lambda **kwargs: Study())
        cases = [
            {"observed_at_ms": 10, "label_available_at_ms": 20, "label": 1, "signal": "0.8"},
            {"observed_at_ms": 11, "label_available_at_ms": 21, "label": 0, "signal": "0.2"},
        ]
        with patch.dict(sys.modules, {"optuna": fake}):
            result = optuna_shadow_threshold(cases, 30, trials=2)
        self.assertEqual(result["in_sample_accuracy"], "1.0")
        self.assertFalse(result["holdout_validated"])
        self.assertEqual(result["promotion_authority"], "NONE")
        self.assertRaises(ValueError, optuna_shadow_threshold, cases, 20)
        self.assertRaises(ValueError, optuna_shadow_threshold, cases, 30, 101)


if __name__ == "__main__":
    unittest.main()
