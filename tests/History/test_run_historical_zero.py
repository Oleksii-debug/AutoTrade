"""Provider-free historical adapter tests: synthetic fixtures, NO trading evidence."""
import csv
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from io import BytesIO, StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from tools.historical_public_klines import collect_month, HistoricalArchiveError
from tools.run_historical_zero import load_verified_months, main


def full_month_fixture(month="2024-01"):
    origin = datetime.fromisoformat(month+"-01T00:00:00+00:00")
    limit = (datetime(origin.year+1,1,1,tzinfo=timezone.utc)
             if origin.month == 12 else datetime(origin.year,origin.month+1,1,tzinfo=timezone.utc))
    csv_text = StringIO()
    writer = csv.writer(csv_text,lineterminator="\n")
    i = 0
    while origin+timedelta(hours=i) < limit:
        t = int((origin+timedelta(hours=i)).timestamp())
        open_millis = t*1000 if origin.year == 2024 else t*1_000_000
        unit = 1000 if origin.year == 2024 else 1_000_000
        writer.writerow([str(open_millis),"100","102","99","101","1",
                         str(open_millis+3600*unit-1),"101","5","1","101","0"])
        i += 1
    filename=f"BTCUSDT-1h-{month}.zip"
    buf=BytesIO()
    with ZipFile(buf,"w") as z:
        z.writestr(filename[:-4]+".csv",csv_text.getvalue())
    raw=buf.getvalue()
    check=f"{sha256(raw).hexdigest()}  {filename}\n".encode()
    return raw,check,i


class HistoricalZeroTests(unittest.TestCase):
    def _dataset(self,tmp):
        archive, checksum, count=full_month_fixture()
        def no_network(url,limit):
            return checksum if url.endswith(".CHECKSUM") else archive
        manifest=collect_month(symbol="BTCUSDT",interval="1h",month="2024-01",
                               output=Path(tmp),fetch=no_network)
        self.assertTrue(manifest["usable_as_complete_causal_interval"])
        self.assertEqual(manifest["row_count"],count)
        return count

    def test_full_realistic_calendar_passes_source_digest_and_clock(self):
        with TemporaryDirectory() as tmp:
            count=self._dataset(tmp)
            times, prices, digest=load_verified_months(Path(tmp),"BTCUSDT","1h","2024-01","2024-01")
            self.assertEqual(count,744)
            self.assertEqual(len(prices),count)
            self.assertEqual(times[0].isoformat(),"2024-01-01T00:00:00+00:00")
            self.assertEqual(times[-1].isoformat(),"2024-01-31T23:00:00+00:00")
            self.assertEqual(len(digest),64)

    def test_entrypoint_reuses_existing_zero_financial_engine_without_live_route(self):
        with TemporaryDirectory() as tmp:
            self._dataset(tmp)
            with patch("tools.run_historical_zero.run_autonomous_simulation",return_value={
                 "status":"SUCCEEDED","environment":"SIMULATION","mode":"ZERO","completed_episodes":744
            }) as sim:
                status=main(["--data-root",tmp,"--symbol","BTCUSDT","--interval","1h",
                             "--from-month","2024-01","--through-month","2024-01",
                             "--state-dir",str(Path(tmp)/"state"),"--run-id","trial"])
            self.assertEqual(status,0)
            args,kw=sim.call_args
            self.assertEqual(len(args[0]),744)
            self.assertEqual(kw["now"],"2024-01-01T01:00:00Z")
            self.assertEqual(kw["observation_interval_seconds"],3600)
            # $250 research target comes from the FIRST COMPLETED close (101),
            # never the last/future candles; canonical engine handles fills.
            self.assertEqual(kw["instrument_profile"],"FRACTIONAL_SPOT_RESEARCH")
            self.assertEqual(kw["target_quantity"],"2.47524752")
            self.assertTrue(kw["run_id"].startswith("trial-"))
            self.assertEqual(len(kw["run_id"]),len("trial-")+16)

    def test_tamper_and_missing_archive_rejected(self):
        with TemporaryDirectory() as tmp:
            self._dataset(tmp)
            path=Path(tmp)/"spot/BTCUSDT/1h/2024-01.ohlcv.csv"
            path.write_bytes(path.read_bytes()+b"bad")
            with self.assertRaisesRegex(HistoricalArchiveError,"digest mismatch"):
                load_verified_months(Path(tmp),"BTCUSDT","1h","2024-01","2024-01")
            with self.assertRaises(HistoricalArchiveError):
                load_verified_months(Path(tmp),"BTCUSDT","1h","2024-01","2024-02")

    def test_wrong_market_cannot_be_relabelled(self):
        with TemporaryDirectory() as tmp:
            self._dataset(tmp)
            manifest=Path(tmp)/"spot/BTCUSDT/1h/2024-01.manifest.json"
            j=json.loads(manifest.read_text())
            j["market"]="PROVIDER_QUALIFIED_LIVE"
            manifest.write_text(json.dumps(j))
            with self.assertRaisesRegex(HistoricalArchiveError,"source identity"):
                load_verified_months(Path(tmp),"BTCUSDT","1h","2024-01","2024-01")


if __name__ == "__main__":
    unittest.main()
