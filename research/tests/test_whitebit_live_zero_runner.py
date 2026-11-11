from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from tools.run_whitebit_live_zero_learning import authenticated_latest


class AuthenticatedLatestTests(unittest.TestCase):
    def test_returns_the_authenticated_restart_snapshot_without_status_race(self):
        expected = {
            "schema": "autotrade-whitebit-public-supervisor-v1",
            "market": "BTC_USDT",
            "update_id": 7,
            "stream_generation": 2,
        }
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "supervisor-state.json").write_text(
                json.dumps({"market": "BTC_USDT"}), encoding="utf-8"
            )
            with patch(
                "tools.run_whitebit_live_zero_learning._read_restart_state",
                return_value=(7, expected),
            ) as reader:
                self.assertIs(authenticated_latest(root), expected)
            self.assertEqual(reader.call_count, 1)
            self.assertEqual(reader.call_args.kwargs["market"], "BTC_USDT")


if __name__ == "__main__":
    unittest.main()
