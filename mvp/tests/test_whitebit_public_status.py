from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import socket
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from mvp.autotrade_mvp import whitebit_public_status
from mvp.autotrade_mvp.whitebit_public_status import read_whitebit_public_status
from mvp.autotrade_mvp.host_network import SnapshotPrincipal
from mvp.autotrade_mvp.product_runtime import build_product


SCHEMA = "autotrade-whitebit-public-supervisor-v1"
NOW = datetime(2026, 10, 18, 8, 0, 10, tzinfo=timezone.utc)


def _json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _fixture(root: Path, *, heartbeat_at="2026-10-18T08:00:09+00:00") -> None:
    _json(root / "supervisor-state.json", {
        "schema": SCHEMA,
        "market": "BTC_USDT",
        "phase": "STARTING",
        "checkpointed_at": "2026-10-18T07:59:00+00:00",
        "observation_count": 0,
        "latest_evidence_ref": None,
        "public_data_only": True,
        "trading_authority": False,
    })
    _json(root / "latest-market.json", {
        "schema": SCHEMA,
        "market": "BTC_USDT",
        "venue": "WHITEBIT_SPOT",
        "bid_price": "100.00",
        "bid_quantity": "1.5",
        "ask_price": "101.00",
        "ask_quantity": "2.5",
        "source_event_at": "2026-10-18T08:00:08+00:00",
        "available_at": "2026-10-18T08:00:08+00:00",
        "published_at": "2026-10-18T08:00:08.1+00:00",
        "update_id": 42,
        "stream_generation": 3,
        "raw_evidence_ref": {
            "artifact_id": "11111111-1111-1111-1111-111111111111",
            "sha256": "sha256:" + "a" * 64,
            "source_uri": "wss://wss.whitebit.com/ws#bookTicker",
            "observed_at": "2026-10-18T08:00:08Z",
            "rights_id": "whitebit-public-websocket-internal-v1",
        },
        "public_data_only": True,
        "trading_authority": False,
    })
    row = {
        "schema": SCHEMA,
        "recorded_at_utc": heartbeat_at,
        "sequence": 7,
        "previous_sha256": "0" * 64,
        "event": "HEARTBEAT",
        "market": "BTC_USDT",
        "phase": "RUNNING",
        "connection_code": "SUBSCRIBED",
        "attempt": 1,
        "generation": 3,
        "observation_count": 1,
        "last_observed_at": "2026-10-18T08:00:08+00:00",
        "public_data_only": True,
        "trading_authority": False,
    }
    body = (json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n").encode()
    diagnostics = root / "diagnostics" / "whitebit-public-diagnostics.jsonl"
    diagnostics.parent.mkdir(parents=True, exist_ok=True)
    diagnostics.write_bytes(body)


class WhitebitPublicStatusTests(unittest.TestCase):
    def test_rotated_history_is_not_scanned_by_snapshot_hot_path(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "whitebit"
            _fixture(root)
            rotated = (
                root / "diagnostics" /
                "whitebit-public-diagnostics-00000000000000000006.jsonl"
            )
            rotated.write_bytes(b"not-json\n" + b"x" * (600 * 1024))

            status = read_whitebit_public_status(root, clock=lambda: NOW)

            self.assertEqual(status["health"]["status"], "READY")

    def test_concurrent_heartbeat_append_retries_one_bounded_coherent_cut(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "whitebit"
            _fixture(root)
            diagnostic = whitebit_public_status._latest_diagnostic(
                root / "diagnostics"
            )[0]
            reads = [
                (diagnostic, "a" * 64),
                (diagnostic, "b" * 64),
                (diagnostic, "c" * 64),
                (diagnostic, "c" * 64),
            ]
            with patch.object(
                whitebit_public_status,
                "_latest_diagnostic",
                side_effect=reads,
            ) as read:
                status = read_whitebit_public_status(root, clock=lambda: NOW)

            self.assertEqual(status["health"]["status"], "READY")
            self.assertEqual(read.call_count, 4)

    def test_product_host_snapshot_contains_read_only_whitebit_status(self):
        with TemporaryDirectory() as directory, TemporaryDirectory() as supervisor:
            root = Path(supervisor) / "whitebit"
            _fixture(root)
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]
            runtime, _ = build_product(
                directory,
                port=port,
                desktop_session_sink=lambda **_values: None,
                whitebit_public_state_dir=root,
            )
            try:
                durable = runtime.application.store.snapshot()
                snapshot = runtime.application._snapshot_provider(
                    durable,
                    SnapshotPrincipal(
                        actor="local-owner",
                        session="sid-" + "0" * 64,
                        role="OWNER",
                    ),
                )
                projected = snapshot["connection_freshness"]["whitebit_public"]
                self.assertEqual(
                    projected["health"]["component"],
                    "WHITEBIT_PUBLIC_SUPERVISOR",
                )
                self.assertIs(projected["public_data_only"], True)
                self.assertIs(projected["trading_authority"], False)
                self.assertNotIn("bid_price", json.dumps(projected))
                self.assertNotIn("credential", json.dumps(projected))
            finally:
                runtime.close()

    def test_current_public_supervisor_projects_latest_heartbeat_and_ready_health(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "whitebit"
            _fixture(root)
            before = {
                path.relative_to(root): (path.read_bytes(), path.stat().st_mtime_ns)
                for path in root.rglob("*") if path.is_file()
            }

            status = read_whitebit_public_status(root, clock=lambda: NOW)

            self.assertEqual(status["health"]["status"], "READY")
            self.assertEqual(status["heartbeat"]["phase"], "RUNNING")
            self.assertEqual(status["latest"]["market"], "BTC_USDT")
            self.assertEqual(status["latest"]["update_id"], 42)
            self.assertIs(status["public_data_only"], True)
            self.assertIs(status["trading_authority"], False)
            rendered = json.dumps(status, sort_keys=True)
            self.assertNotIn("bid_price", rendered)
            self.assertNotIn("raw_evidence_ref", rendered)
            self.assertNotIn("credential", rendered)
            after = {
                path.relative_to(root): (path.read_bytes(), path.stat().st_mtime_ns)
                for path in root.rglob("*") if path.is_file()
            }
            self.assertEqual(after, before, "status integration must remain read-only")

    def test_missing_or_malformed_status_fails_closed_without_reflecting_content(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "whitebit"
            root.mkdir()
            missing = read_whitebit_public_status(root, clock=lambda: NOW)
            self.assertEqual(missing["health"]["status"], "BLOCKED")
            self.assertIsNone(missing["heartbeat"])
            self.assertIsNone(missing["latest"])

            (root / "supervisor-state.json").write_text(
                '{"api_key":"secret-value"}', encoding="utf-8"
            )
            malformed = read_whitebit_public_status(root, clock=lambda: NOW)
            self.assertEqual(malformed["health"]["status"], "BLOCKED")
            self.assertNotIn("secret-value", json.dumps(malformed))

    def test_stale_heartbeat_never_claims_ready(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "whitebit"
            _fixture(root, heartbeat_at="2026-10-18T07:00:00+00:00")
            status = read_whitebit_public_status(root, clock=lambda: NOW)
            self.assertEqual(status["health"]["status"], "DEGRADED")
            self.assertEqual(
                status["health"]["reason_codes"],
                ["whitebit_public_heartbeat_stale"],
            )

    def test_broken_diagnostic_hash_chain_is_blocked(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "whitebit"
            _fixture(root)
            path = root / "diagnostics" / "whitebit-public-diagnostics.jsonl"
            first = path.read_bytes()
            row = json.loads(first)
            row["sequence"] = 8
            row["previous_sha256"] = "f" * 64
            path.write_bytes(
                first + (json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n").encode()
            )
            self.assertNotEqual(row["previous_sha256"], sha256(first).hexdigest())
            status = read_whitebit_public_status(root, clock=lambda: NOW)
            self.assertEqual(status["health"]["status"], "BLOCKED")
            self.assertEqual(
                status["health"]["reason_codes"],
                ["whitebit_public_status_unavailable"],
            )


if __name__ == "__main__":
    unittest.main()
