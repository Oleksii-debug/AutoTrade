from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from research.autotrade_research.artifacts.store import ArtifactStore
from tools.replay_whitebit_bbo_learning import replay


class WhiteBitBboLearningReplayTests(unittest.TestCase):
    def test_authenticated_public_artifacts_replay_causally_without_orders(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = ArtifactStore(root / "artifacts")
            base = datetime.now(timezone.utc) - timedelta(minutes=10)
            for index in range(8):
                observed = base + timedelta(seconds=61 * index)
                source = observed - timedelta(milliseconds=10)
                row = {
                    "method": "bookTicker_update",
                    "params": [[
                        source.timestamp(), observed.timestamp(), "BTC_USDT", index + 1,
                        str(100 + index), "3", str(100.01 + index), "1",
                    ]],
                    "id": None,
                }
                store.publish_bytes(
                    artifact_id=str(uuid4()),
                    data=json.dumps(row).encode("utf-8"),
                    media_type="application/json",
                    rights={
                        "storage": True, "export": False,
                        "rights_id": "whitebit-public-websocket-internal-v1",
                    },
                    source_refs=["wss://wss.whitebit.com/ws#bookTicker"],
                    metadata={
                        "provider": "WHITEBIT", "market": "BTC_USDT",
                        "source_time": source.isoformat(),
                        "message_time": observed.isoformat(),
                        "observed_at": observed.isoformat().replace("+00:00", "Z"),
                        "stream_generation": "1", "update_id": str(index + 1),
                    },
                )
            result = replay(
                artifact_root=root / "artifacts",
                state_dir=root / "replay",
                maximum_artifacts=8,
                outcome_horizon_seconds=60,
            )
            self.assertEqual(result["authenticated_artifacts"], 8)
            self.assertGreaterEqual(result["episode_count"], 6)
            self.assertFalse(result["network_orders"])
            self.assertFalse(result["trading_authority"])
            self.assertTrue((root / "replay" / "episode-memory.sqlite3").is_file())
            self.assertTrue((root / "replay" / "replay-summary.json").is_file())


if __name__ == "__main__":
    unittest.main()

