from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from uuid import uuid4

from mvp.autotrade_mvp.manual_trading import (
    ManualTradingController,
    ManualTradingUnavailable,
)
from mvp.autotrade_mvp.persistence import JournalStore


NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parents[2]


def quote(market="BTC_USDT"):
    base, quote_asset = market.split("_")
    return {
        "schema": "autotrade-whitebit-public-quote-v1",
        "status": "READY",
        "market": market,
        "venue": "WHITEBIT_SPOT",
        "bid_price": "60000",
        "bid_quantity": "1",
        "ask_price": "60001",
        "ask_quantity": "2",
        "mid_price": "60000.5",
        "available_at": "2026-10-20T12:00:00Z",
        "published_at": "2026-10-20T12:00:00Z",
        "update_id": 1,
        "stream_generation": 1,
        "evidence_sha256": "sha256:" + "a" * 64,
        "public_data_only": True,
        "trading_authority": False,
        "instrument": {
            "schema": "autotrade-whitebit-spot-instrument-v1",
            "market": market,
            "base_asset": base,
            "quote_asset": quote_asset,
            "minimum_quantity": "0.00001",
            "minimum_notional": "5",
            "quantity_precision": 5,
            "quote_precision": 2,
            "trades_enabled": True,
            "delisted_at": None,
            "metadata_observed_at": "2026-10-20T12:00:00Z",
            "metadata_sha256": "sha256:" + "b" * 64,
        },
    }


def admitted_capability():
    return {
        "provider_id": "WHITEBIT",
        "environment": "LIVE",
        "account_id": "whitebit-primary",
        "permission_scopes": ["ACCOUNT_READ", "ORDER_MANAGEMENT", "ORDER_WRITE"],
        "capability_snapshot_id": "capability-live-1",
        "current": True,
        "credential_profile": "whitebit-trade",
        "credential_purpose": "TRADE",
        "forbidden_permissions": [],
        "blocker": "not blocked",
    }


class ManualTradingTests(unittest.TestCase):
    def controller(self, directory, **kwargs):
        return ManualTradingController(
            JournalStore(Path(directory) / "journal.sqlite3"),
            quote_provider=quote,
            clock=lambda: NOW,
            **kwargs,
        )

    @staticmethod
    def buy_request():
        return {
            "request_id": str(uuid4()),
            "market": "BTC_USDT",
            "action": "BUY",
            "quantity": "0.001",
            "order_type": "LIMIT",
            "limit_price": "59000",
        }

    def test_public_market_and_draft_work_while_only_read_profile_is_stored(self):
        with TemporaryDirectory() as directory:
            controller = self.controller(directory)
            self.assertEqual(controller.market("BTC_USDT")["ask_price"], "60001")
            prepared = controller.prepare(self.buy_request())
            self.assertTrue(prepared["requires_separate_confirmation"])
            self.assertEqual(prepared["draft"]["environment"], "LIVE")
            self.assertFalse(prepared["draft"]["execution_performed"])
            status = controller.status()
            self.assertFalse(status["order_entry_enabled"])
            self.assertEqual(status["permission_scopes"], ["ACCOUNT_READ"])
            self.assertEqual(status["credential_profile"], "whitebit-primary")
            self.assertIn("separate TRADE-purpose", status["activation_blocker"])

    def test_unarmed_read_profile_blocks_confirm_before_any_sender_call(self):
        calls = []
        with TemporaryDirectory() as directory:
            controller = self.controller(
                directory,
                canonical_command_gateway=lambda command: calls.append(command),
            )
            draft = controller.prepare(self.buy_request())["draft"]
            with self.assertRaisesRegex(ManualTradingUnavailable, "READ-only"):
                controller.confirm({
                    "draft_id": draft["draft_id"],
                    "confirmation": "CONFIRM REAL WHITEBIT ORDER",
                })
            self.assertEqual(calls, [])

    def test_deposit_withdraw_and_transfer_can_never_be_prepared(self):
        with TemporaryDirectory() as directory:
            controller = self.controller(directory)
            for action in ("DEPOSIT", "WITHDRAW", "TRANSFER", "BALANCE_TRANSFER"):
                with self.subTest(action=action), self.assertRaisesRegex(
                    ValueError, "action is unsupported"
                ):
                    controller.prepare({
                        "request_id": str(uuid4()),
                        "market": "BTC_USDT",
                        "action": action,
                    })

    def test_unknown_or_disabled_market_is_rejected_before_durable_draft(self):
        with TemporaryDirectory() as directory:
            controller = ManualTradingController(
                JournalStore(Path(directory) / "journal.sqlite3"),
                quote_provider=lambda _market: (_ for _ in ()).throw(
                    ValueError("SPOT_MARKET_NOT_AVAILABLE")
                ),
                clock=lambda: NOW,
            )
            with self.assertRaisesRegex(ValueError, "SPOT_MARKET_NOT_AVAILABLE"):
                controller.prepare({
                    "request_id": str(uuid4()),
                    "market": "UNKNOWN_USDT",
                    "action": "CANCEL",
                    "client_order_id": "manual-existing-order",
                })

    def test_selected_market_and_constraints_reach_gateway_unchanged(self):
        calls = []
        with TemporaryDirectory() as directory:
            controller = self.controller(
                directory,
                capability_provider=admitted_capability,
                canonical_command_gateway=lambda command: calls.append(command) or {
                    "outcome": "ACKNOWLEDGED"
                },
                submission_guard=lambda: None,
            )
            for market in ("ETH_USDT", "SOL_USDT"):
                with self.subTest(market=market):
                    request = self.buy_request()
                    request["market"] = market
                    draft = controller.prepare(request)["draft"]
                    self.assertEqual(draft["market"], market)
                    self.assertEqual(draft["instrument"]["market"], market)
                    controller.confirm({
                        "draft_id": draft["draft_id"],
                        "confirmation": "CONFIRM REAL WHITEBIT ORDER",
                    })
                    self.assertEqual(calls[-1]["draft"]["market"], market)

    def test_market_and_instrument_constraints_fail_closed(self):
        with TemporaryDirectory() as directory:
            controller = self.controller(directory)
            for market in ("btc_usdt", "BTC-USDT", "BTC_USDT_EXTRA"):
                with self.subTest(market=market), self.assertRaisesRegex(
                    ValueError, "canonical WhiteBIT"
                ):
                    controller.market(market)
            for field, value, message in (
                ("quantity", "0.000001", "minimum_quantity"),
                ("quantity", "0.000011", "quantity precision"),
                ("limit_price", "59000.001", "quote precision"),
                ("quantity", "0.00001", "minimum_notional"),
            ):
                request = self.buy_request()
                request[field] = value
                with self.subTest(field=field, value=value), self.assertRaisesRegex(
                    ValueError, message
                ):
                    controller.prepare(request)

    def test_confirm_calls_injected_canonical_gateway_once_and_is_idempotent(self):
        calls = []

        def gateway(command):
            calls.append(dict(command))
            return {"outcome": "ACKNOWLEDGED", "provider_order_id": "wb-123"}

        with TemporaryDirectory() as directory:
            controller = self.controller(
                directory,
                capability_provider=admitted_capability,
                canonical_command_gateway=gateway,
                submission_guard=lambda: None,
            )
            draft = controller.prepare(self.buy_request())["draft"]
            request = {
                "draft_id": draft["draft_id"],
                "confirmation": "CONFIRM REAL WHITEBIT ORDER",
            }
            first = controller.confirm(request)
            second = controller.confirm(request)
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0]["command_id"], draft["draft_id"])
            self.assertEqual(calls[0]["capability_snapshot_id"], "capability-live-1")
            self.assertEqual(first["confirmation"]["result"]["provider_order_id"], "wb-123")
            self.assertEqual(second["confirmation"]["result"]["provider_order_id"], "wb-123")

    def test_runtime_mode_guard_runs_immediately_before_gateway_and_can_block(self):
        events = []
        with TemporaryDirectory() as directory:
            controller = self.controller(
                directory,
                capability_provider=admitted_capability,
                submission_guard=lambda: events.append("mode-guard"),
                canonical_command_gateway=lambda _command: events.append("gateway") or {
                    "outcome": "ACKNOWLEDGED"
                },
            )
            draft = controller.prepare(self.buy_request())["draft"]
            controller.confirm({
                "draft_id": draft["draft_id"],
                "confirmation": "CONFIRM REAL WHITEBIT ORDER",
            })
            self.assertEqual(events, ["mode-guard", "gateway"])

        with TemporaryDirectory() as directory:
            calls = []
            controller = self.controller(
                directory,
                capability_provider=admitted_capability,
                submission_guard=lambda: (_ for _ in ()).throw(
                    PermissionError("runtime mode RESEARCH_ONLY blocks orders")
                ),
                canonical_command_gateway=lambda command: calls.append(command),
            )
            draft = controller.prepare(self.buy_request())["draft"]
            with self.assertRaisesRegex(PermissionError, "RESEARCH_ONLY"):
                controller.confirm({
                    "draft_id": draft["draft_id"],
                    "confirmation": "CONFIRM REAL WHITEBIT ORDER",
                })
            self.assertEqual(calls, [])

    def test_assistant_can_prepare_but_never_confirms(self):
        calls = []
        with TemporaryDirectory() as directory:
            controller = self.controller(
                directory,
                capability_provider=admitted_capability,
                canonical_command_gateway=lambda command: calls.append(command),
                submission_guard=lambda: None,
            )
            result = controller.assistant_query({
                "request_id": str(uuid4()),
                "market": "BTC_USDT",
                "message": "купити 0.001 BTC",
            })
            self.assertIsNotNone(result["prepared"])
            self.assertFalse(result["execution_performed"])
            self.assertFalse(result["assistant_execution_authority"])
            self.assertEqual(calls, [])

    def test_account_projection_is_read_only_status_data(self):
        with TemporaryDirectory() as directory:
            controller = self.controller(
                directory,
                account_projection_provider=lambda: {
                    "balances": [{"asset": "USDT", "available": "100"}],
                    "open_orders": [],
                    "orders": [{"client_order_id": "manual-one", "status": "FILLED"}],
                },
            )
            status = controller.status()
            self.assertEqual(status["account_projection_status"], "READY")
            self.assertEqual(status["account"]["balances"][0]["available"], "100")

    def test_transient_private_read_failure_keeps_manual_market_screen_available(self):
        with TemporaryDirectory() as directory:
            controller = self.controller(
                directory,
                account_projection_provider=lambda: (_ for _ in ()).throw(
                    RuntimeError("secret remote detail")
                ),
            )
            status = controller.status()
            self.assertEqual(status["account_projection_status"], "UNAVAILABLE")
            self.assertIsNone(status["account"])
            self.assertFalse(status["order_entry_enabled"])

    def test_web_ticket_is_semantic_keyboard_first_and_explicitly_real(self):
        html = (ROOT / "web" / "src" / "index.html").read_text(encoding="utf-8")
        script = (ROOT / "web" / "src" / "app.js").read_text(encoding="utf-8")
        self.assertIn('id="manual-trading-heading" tabindex="-1"', html)
        self.assertIn('for="manual-quantity"', html)
        self.assertIn("Real WhiteBIT order ticket", html)
        self.assertIn("CONFIRM REAL WHITEBIT ORDER", script)
        self.assertIn('event.key.toLowerCase() === "t"', script)
        self.assertIn("assistant_execution_authority !== false", script)
        self.assertIn('byId("manual-market").value.split("_")', script)
        self.assertNotIn('available("BTC")', script)

    def test_host_contract_exposes_prepare_confirm_status_and_market_separately(self):
        contract = (ROOT / "contracts" / "openapi" / "host-api.yaml").read_text(
            encoding="utf-8"
        )
        for route in (
            "/api/v1/manual/market:",
            "/api/v1/manual/status:",
            "/api/v1/manual/prepare:",
            "/api/v1/manual/confirm:",
            "/api/v1/assistant/query:",
        ):
            self.assertIn(route, contract)


if __name__ == "__main__":
    unittest.main()
