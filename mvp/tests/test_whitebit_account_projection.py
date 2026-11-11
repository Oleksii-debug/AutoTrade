from datetime import datetime, timezone
import unittest

from mvp.autotrade_mvp.whitebit_account_projection import (
    WhitebitAccountProjectionProvider,
    project_whitebit_account,
)
from research.autotrade_research.integrations.whitebit_private_readonly import (
    MAIN_BALANCE_PATH,
    TRADING_BALANCE_PATH,
    PrivateReadObservation,
)


def observation(body, path):
    return PrivateReadObservation(
        datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc),
        "a" * 64 if path == MAIN_BALANCE_PATH else "b" * 64,
        body,
        path=path,
        verified_account_binding=True,
    )


class WhitebitAccountProjectionTests(unittest.TestCase):
    def test_projects_only_nonzero_exact_read_balances(self):
        value = project_whitebit_account(
            observation(
                b'[{"ticker":"USDT","balance":"12.50"},{"ticker":"BTC","balance":"0"}]',
                MAIN_BALANCE_PATH,
            ),
            observation(
                b'{"BTC":{"available":"0.1","freeze":"0.02"},"USDT":{"available":"0","freeze":"0"}}',
                TRADING_BALANCE_PATH,
            ),
        )
        self.assertEqual(value["wallet_balances"], [{"asset": "USDT", "balance": "12.5"}])
        self.assertEqual(
            value["spot_balances"],
            [{"asset": "BTC", "available": "0.1", "frozen": "0.02"}],
        )
        self.assertEqual(value["balances"], value["spot_balances"])
        self.assertTrue(value["verified_account_binding"])
        self.assertTrue(value["read_only"])

    def test_rejects_duplicate_or_negative_provider_values(self):
        with self.assertRaises(ValueError):
            project_whitebit_account(
                observation(
                    b'[{"ticker":"USDT","balance":"1"},{"ticker":"USDT","balance":"2"}]',
                    MAIN_BALANCE_PATH,
                ),
                observation(b'{}', TRADING_BALANCE_PATH),
            )
        with self.assertRaises(ValueError):
            project_whitebit_account(
                observation(b'[]', MAIN_BALANCE_PATH),
                observation(b'{"USDT":{"available":"-1","freeze":"0"}}', TRADING_BALANCE_PATH),
            )

    def test_provider_uses_cache_and_returns_copies(self):
        calls = []

        class Credentials:
            api_key = "key"
            api_secret = "secret"

        class Reader:
            def __init__(self, key, secret, *, enable_network):
                calls.append((key, secret, enable_network))
            def __enter__(self): return self
            def __exit__(self, *_): return None
            def main_balance(self): return observation(b'[]', MAIN_BALANCE_PATH)
            def trading_balance(self): return observation(b'{}', TRADING_BALANCE_PATH)

        provider = WhitebitAccountProjectionProvider(
            credential_resolver=lambda **_: Credentials(),
            reader_factory=Reader,
            monotonic=lambda: 1.0,
        )
        first = provider()
        first["wallet_balances"].append({"asset": "BAD", "balance": "1"})
        second = provider()
        self.assertEqual(len(calls), 1)
        self.assertEqual(second["wallet_balances"], [])


if __name__ == "__main__":
    unittest.main()
