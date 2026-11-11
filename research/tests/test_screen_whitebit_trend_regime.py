from pathlib import Path
import unittest
from unittest.mock import patch

from tools.screen_whitebit_trend_regime import authenticated_bars


class WhitebitTrendRegimeBridgeTests(unittest.TestCase):
    def test_authenticated_rows_become_exact_utc_bars(self):
        raw = ("open_time_utc,open,high,low,close,volume\n"
               "2025-01-01T00:00:00Z,100,102,99,101,5\n"
               "2025-01-01T01:00:00Z,101,103,100,102,6\n").encode()
        manifest = {"market": "BTC_USDT", "interval": "1h"}
        with (patch("tools.screen_whitebit_trend_regime._read_regular",
                    side_effect=[b"manifest", raw]),
              patch("tools.screen_whitebit_trend_regime._csv_path",
                    return_value=Path("bars.csv")),
              patch("tools.screen_whitebit_trend_regime._manifest",
                    return_value=manifest),
              patch("tools.screen_whitebit_trend_regime._authenticate_manifest",
                    return_value=(3600, "a"*64)) as authenticate,
              patch("tools.screen_whitebit_trend_regime._rows",
                    return_value=[(1735689600, "101"), (1735693200, "102")]) as rows):
            bars, digest, returned_manifest = authenticated_bars(Path("cut.manifest.json"))
        authenticate.assert_called_once_with(manifest, raw)
        rows.assert_called_once_with(raw, manifest=manifest, step=3600)
        self.assertEqual(digest, "a"*64)
        self.assertIs(returned_manifest, manifest)
        self.assertEqual(bars[0]["opened_at"].isoformat(), "2025-01-01T00:00:00+00:00")
        self.assertEqual(bars[0]["closed_at"].isoformat(), "2025-01-01T01:00:00+00:00")
        self.assertEqual(bars[1]["volume"], "6")

    def test_second_decode_cannot_change_authenticated_close(self):
        raw = ("open_time_utc,open,high,low,close,volume\n"
               "2025-01-01T00:00:00Z,100,102,99,101,5\n").encode()
        with (patch("tools.screen_whitebit_trend_regime._read_regular",
                    side_effect=[b"manifest", raw]),
              patch("tools.screen_whitebit_trend_regime._csv_path",
                    return_value=Path("bars.csv")),
              patch("tools.screen_whitebit_trend_regime._manifest", return_value={}),
              patch("tools.screen_whitebit_trend_regime._authenticate_manifest",
                    return_value=(3600, "b"*64)),
              patch("tools.screen_whitebit_trend_regime._rows",
                    return_value=[(1735689600, "999")])):
            with self.assertRaisesRegex(ValueError, "close changed"):
                authenticated_bars(Path("cut.manifest.json"))


if __name__ == "__main__":
    unittest.main()
