from __future__ import annotations

from datetime import datetime, timedelta, timezone
import http.client
import json
from pathlib import Path
import socket
from tempfile import TemporaryDirectory
from threading import Thread
import unittest
from unittest.mock import patch
from uuid import uuid4

from mvp.autotrade_mvp.capabilities import (
    CapabilityClaim,
    CapabilityError,
    EvidenceVerification,
    derive_current_process_capability_snapshot,
    derive_capability_snapshot,
)
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.product_runtime import build_product
from mvp.autotrade_mvp.provider_transport import TradingWireResponse
from mvp.autotrade_mvp.security import SecurityBoundary
from mvp.autotrade_mvp.runtime_modes import (
    canonical_runtime_mode_payload,
    execute_runtime_mode_action,
)
from mvp.autotrade_mvp.whitebit import (
    WhiteBitCredentialBoundary,
    WhiteBitMarketRules,
)
from mvp.autotrade_mvp.whitebit_manual_live_gateway import (
    WhiteBitManualLiveGateway,
    WhiteBitManualLiveGatewayError,
    whitebit_spot_rules_from_public_snapshot,
)
from mvp.autotrade_mvp.whitebit_live_composition import (
    WhiteBitCurrentCapabilityProvider,
    WhiteBitManualQuotaGate,
)
from mvp.autotrade_mvp.whitebit_trade_setup import (
    install_whitebit_trade_binding,
    load_whitebit_trade_binding,
)
from mvp.autotrade_mvp.windows_secrets import ProtectedCredentialVault
from mvp.tests.capability_test_support import fresh_test_admission


NOW = datetime(2026, 10, 20, 20, 0, tzinfo=timezone.utc)


class ReversibleProtector:
    def protect(self, plaintext: bytes, *, entropy: bytes) -> bytes:
        return bytes(value ^ entropy[index % len(entropy)] for index, value in enumerate(plaintext))

    def unprotect(self, ciphertext: bytes, *, entropy: bytes) -> bytes:
        return self.protect(ciphertext, entropy=entropy)


class RecordingWire:
    def __init__(self, *, timeout: bool = False):
        self.requests = []
        self.timeout = timeout

    def send(self, request):
        self.requests.append(request)
        if self.timeout:
            raise TimeoutError("response lost after possible provider acceptance")
        return TradingWireResponse(
            http_status=200, body=b'{"orderId":123,"status":"success"}'
        )


def capability():
    observed = NOW - timedelta(minutes=5)
    claims = tuple(
        CapabilityClaim(
            source=source,
            provider_id="WHITEBIT",
            account_id="whitebit-primary",
            entity_id="whitebit-spot-manual",
            environment="LIVE",
            provider_environment="LIVE",
            instrument_version="whitebit-spot-current-v1",
            observed_at=observed,
            expires_at=NOW + timedelta(hours=1),
            supported_order_types=frozenset({"MARKET", "LIMIT"}),
            time_in_force=frozenset({"GTC"}),
            permission_scopes=frozenset({"ACCOUNT_READ", "ORDER_WRITE"}),
            position_mode="NET",
            native_protection=frozenset(),
            rate_limit_policy_id="whitebit-v4-manual",
            data_entitlements=frozenset({"ORDERS", "BALANCES"}),
            evidence_ref={
                "artifact_id": str(uuid4()),
                "sha256": "sha256:" + "a" * 64,
                "observed_at": observed.isoformat().replace("+00:00", "Z"),
                "source_uri": "https://docs.whitebit.com/api-reference/trading/create-limit-order",
            },
        )
        for source in ("DOCUMENTED", "API", "ACCOUNT", "INSTRUMENT")
    )
    return fresh_test_admission(derive_capability_snapshot(
        snapshot_id=str(uuid4()),
        claims=claims,
        observed_at=observed,
        evidence_verifier=lambda _claim: EvidenceVerification(valid=True),
    ))


def rules(market):
    return WhiteBitMarketRules.from_provider({
        "name": market,
        "type": "spot",
        "isTradFiFutures": False,
        "isCollateral": False,
        "tradesEnabled": True,
        "stepSize": "0.000001",
        "tickSize": "0.01",
        "minAmount": "0.00001",
        "minTotal": "1",
        "maxTotal": "0",
        "delistedAt": None,
    })


def public_quote(market):
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
        "available_at": "2026-10-20T20:00:00Z",
        "published_at": "2026-10-20T20:00:00Z",
        "evidence_sha256": "sha256:" + "c" * 64,
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
            "metadata_observed_at": "2026-10-20T20:00:00Z",
            "metadata_sha256": "sha256:" + "d" * 64,
        },
    }


def draft(command_id, *, action="BUY", market="BTC_USDT"):
    return {
        "schema": "autotrade-manual-whitebit-v1",
        "draft_id": command_id,
        "source": "MANUAL",
        "action": action,
        "provider": "WHITEBIT",
        "environment": "LIVE",
        "market": market,
        "product_family": "SPOT",
        "quantity": None if action == "CANCEL" else "0.001",
        "order_type": None if action == "CANCEL" else "LIMIT",
        "limit_price": None if action == "CANCEL" else "10000.00",
        "client_order_id": "manual-existing-target" if action == "CANCEL" else "manual-draft-display",
        "prepared_at": "2026-10-20T19:59:00Z",
        "expires_at": "2026-10-20T20:01:00Z",
        "quote": {"public_data_only": True},
        "instrument": {"market": market},
        "execution_performed": False,
    }


class WhiteBitManualLiveGatewayTests(unittest.TestCase):
    def test_current_process_admission_rejects_caller_authored_verifier(self):
        observed = NOW - timedelta(seconds=1)
        claims = tuple(
            CapabilityClaim(
                source=source,
                provider_id="WHITEBIT",
                account_id="whitebit-primary",
                entity_id="whitebit-spot-manual",
                environment="LIVE",
                provider_environment="LIVE",
                instrument_version="whitebit-spot-current-v1",
                observed_at=observed,
                expires_at=NOW + timedelta(minutes=5),
                supported_order_types=frozenset({"MARKET", "LIMIT"}),
                time_in_force=frozenset({"GTC"}),
                permission_scopes=frozenset({"ACCOUNT_READ", "ORDER_WRITE"}),
                position_mode="NET",
                native_protection=frozenset(),
                rate_limit_policy_id="whitebit-v4-manual",
                data_entitlements=frozenset({"ORDERS", "BALANCES"}),
                evidence_ref={
                    "artifact_id": str(uuid4()),
                    "sha256": "sha256:" + "a" * 64,
                    "observed_at": observed.isoformat().replace("+00:00", "Z"),
                },
            )
            for source in ("DOCUMENTED", "API", "ACCOUNT", "INSTRUMENT")
        )
        with self.assertRaisesRegex(CapabilityError, "authenticated ArtifactStore"):
            derive_current_process_capability_snapshot(
                snapshot_id=str(uuid4()),
                claims=claims,
                observed_at=observed,
                evidence_verifier=lambda _claim: EvidenceVerification(valid=True),
            )

    def test_protected_current_capability_uses_read_only_evidence(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            vault = ProtectedCredentialVault(
                root / "credentials.json", protector=ReversibleProtector()
            )
            install_whitebit_trade_binding(
                vault,
                data_dir=root,
                api_key="test-trade-key",
                api_secret="test-trade-secret",
                permissions=frozenset({"INFO", "TRADING"}),
                ip_whitelist_enabled=True,
                observed_at=NOW,
                evidence_ref="test:whitebit-exact-endpoints",
            )
            calls = []

            def account_evidence():
                calls.append("READ_ONLY_ACCOUNT")
                return {
                    "schema": "autotrade-whitebit-live-account-evidence-v1",
                    "provider": "WHITEBIT",
                    "environment": "LIVE",
                    "account_id": "whitebit-primary",
                    "observed_at": NOW.isoformat().replace("+00:00", "Z"),
                    "verified_account_binding": True,
                    "read_routes": [
                        "/api/v4/main-account/balance",
                        "/api/v4/trade-account/balance",
                    ],
                    "source_sha256": ["sha256:" + "1" * 64, "sha256:" + "2" * 64],
                    "contains_balances": False,
                    "contains_secrets": False,
                }

            provider = WhiteBitCurrentCapabilityProvider(
                JournalStore(root / "journal.sqlite3"),
                data_dir=root,
                credential_vault=vault,
                public_snapshot_provider=public_quote,
                account_evidence_provider=account_evidence,
                clock_utc=lambda: NOW,
            )
            snapshot = provider("BTC_USDT")
            self.assertEqual(snapshot.status, "VERIFIED")
            self.assertTrue(snapshot.admits(
                at=NOW,
                order_type="LIMIT",
                time_in_force="GTC",
                permission_scope="ORDER_WRITE",
            ))
            self.assertEqual(calls, ["READ_ONLY_ACCOUNT"])
            self.assertTrue((root / "state" / "capability-evidence").is_dir())

    def test_manual_quota_keeps_cancel_lane_separate(self):
        now = [0.0]
        gate = WhiteBitManualQuotaGate(monotonic=lambda: now[0])
        for _ in range(10):
            gate("WHITEBIT", "whitebit-primary", "LIVE", "ORDER_WRITE")
        with self.assertRaisesRegex(PermissionError, "exhausted"):
            gate("WHITEBIT", "whitebit-primary", "LIVE", "ORDER_WRITE")
        gate("WHITEBIT", "whitebit-primary", "LIVE", "ORDER_CANCEL_RESERVED")

    def test_current_public_snapshot_converts_to_exact_spot_rules(self):
        converted = whitebit_spot_rules_from_public_snapshot({
            "schema": "autotrade-whitebit-public-quote-v1",
            "market": "ETH_USDT",
            "venue": "WHITEBIT_SPOT",
            "public_data_only": True,
            "trading_authority": False,
            "instrument": {
                "schema": "autotrade-whitebit-spot-instrument-v1",
                "market": "ETH_USDT",
                "minimum_quantity": "0.0001",
                "minimum_notional": "5",
                "quantity_precision": 4,
                "quote_precision": 2,
                "trades_enabled": True,
                "delisted_at": None,
            },
        })
        self.assertEqual(converted.market, "ETH_USDT")
        self.assertEqual(str(converted.step_size), "0.0001")
        self.assertEqual(str(converted.tick_size), "0.01")
        self.assertFalse(converted.is_collateral)

    def composition(self, directory, *, wire=None):
        journal = JournalStore(Path(directory) / "journal.sqlite3")
        vault = ProtectedCredentialVault(
            Path(directory) / "credentials.json", protector=ReversibleProtector()
        )
        origin = "http://127.0.0.1:8765"
        boundary = SecurityBoundary(
            allowed_origins={origin},
            credential_vault=vault,
            session_authorizer=lambda subject, role, supplied_origin: (
                subject == "local-owner" and role == "OWNER" and supplied_origin == origin
            ),
            now=lambda: NOW.timestamp(),
        )
        session = boundary.create_session(
            subject="local-owner", role="OWNER", origin=origin, ttl_seconds=3600
        )
        handle = boundary.register_secret(
            session.token,
            origin=origin,
            owner_identity="local-owner",
            account_id="whitebit-primary",
            provider="WHITEBIT",
            environment="LIVE",
            provider_environment="LIVE",
            purpose="TRADE",
            secret_value=json.dumps({"api_key": "test-api-key", "api_secret": "test-api-secret"}),
        )
        credential = WhiteBitCredentialBoundary(
            credential_binding_id=handle.handle_id,
            credential_generation=handle.generation,
            account_id=handle.account_id,
            permissions=frozenset({"INFO", "TRADING"}),
            ip_whitelist_enabled=True,
            environment="LIVE",
            observed_at=NOW,
            evidence_ref="test:whitebit-key-settings",
        )
        cap = capability()
        quota = []
        gateway = WhiteBitManualLiveGateway(
            journal,
            security_boundary=boundary,
            credential_handle=handle,
            credential_boundary_provider=lambda: credential,
            capability_provider=lambda _market: cap,
            market_rules_provider=rules,
            session_token_provider=lambda: session.token,
            origin_provider=lambda: origin,
            quota_gate=lambda *args: quota.append(args),
            clock_utc=lambda: NOW,
            clock_millis=lambda: 1_729_454_400_000,
            wire_client=wire or RecordingWire(),
        )
        return gateway, cap, quota, journal

    def test_limit_order_uses_real_guarded_transport_once(self):
        with TemporaryDirectory() as directory:
            wire = RecordingWire()
            gateway, cap, quota, _journal = self.composition(directory, wire=wire)
            command_id = str(uuid4())
            command = {
                "schema": "autotrade-canonical-manual-command-v1",
                "command_id": command_id,
                "source": "MANUAL_CONFIRMED",
                "capability_snapshot_id": cap.snapshot_id,
                "draft": draft(command_id),
            }
            first = gateway(command)
            second = gateway(command)
            self.assertEqual(first["outcome"], "SENT")
            self.assertEqual(second["outcome"], "SENT")
            self.assertFalse(first["fill_confirmed"])
            self.assertEqual(len(wire.requests), 1)
            self.assertEqual(quota, [("WHITEBIT", "whitebit-primary", "LIVE", "ORDER_WRITE")])
            self.assertEqual(json.loads(wire.requests[0].body)["market"], "BTC_USDT")

    def test_unknown_is_durable_and_never_retried(self):
        with TemporaryDirectory() as directory:
            wire = RecordingWire(timeout=True)
            gateway, cap, _quota, _journal = self.composition(directory, wire=wire)
            command_id = str(uuid4())
            command = {
                "schema": "autotrade-canonical-manual-command-v1",
                "command_id": command_id,
                "source": "ASSISTED_CONFIRMED",
                "capability_snapshot_id": cap.snapshot_id,
                "draft": draft(command_id),
            }
            first = gateway(command)
            second = gateway(command)
            self.assertEqual(first["outcome"], "UNKNOWN")
            self.assertEqual(second["outcome"], "UNKNOWN")
            self.assertTrue(second["requires_reconciliation"])
            self.assertEqual(len(wire.requests), 1)

    def test_cancel_uses_reserved_quota_and_exact_spot_endpoint(self):
        with TemporaryDirectory() as directory:
            wire = RecordingWire()
            gateway, cap, quota, _journal = self.composition(directory, wire=wire)
            command_id = str(uuid4())
            result = gateway({
                "schema": "autotrade-canonical-manual-command-v1",
                "command_id": command_id,
                "source": "MANUAL_CONFIRMED",
                "capability_snapshot_id": cap.snapshot_id,
                "draft": draft(command_id, action="CANCEL", market="SOL_USDT"),
            })
            self.assertEqual(result["outcome"], "SENT")
            self.assertEqual(quota[-1][-1], "ORDER_CANCEL_RESERVED")
            self.assertTrue(wire.requests[0].url.endswith("/api/v4/order/cancel"))
            body = json.loads(wire.requests[0].body)
            self.assertEqual(body["market"], "SOL_USDT")
            self.assertEqual(body["clientOrderId"], "manual-existing-target")

    def test_rejects_unlisted_market_and_extra_financial_route_fields(self):
        with TemporaryDirectory() as directory:
            wire = RecordingWire()
            gateway, cap, _quota, _journal = self.composition(directory, wire=wire)
            command_id = str(uuid4())
            unsupported = draft(command_id, market="DOGE_USDT")
            with self.assertRaisesRegex(WhiteBitManualLiveGatewayError, "allowlist"):
                gateway({
                    "schema": "autotrade-canonical-manual-command-v1",
                    "command_id": command_id,
                    "source": "MANUAL_CONFIRMED",
                    "capability_snapshot_id": cap.snapshot_id,
                    "draft": unsupported,
                })
            dangerous = draft(command_id)
            dangerous["withdrawal_address"] = "forbidden"
            with self.assertRaisesRegex(WhiteBitManualLiveGatewayError, "fields differ"):
                gateway({
                    "schema": "autotrade-canonical-manual-command-v1",
                    "command_id": command_id,
                    "source": "MANUAL_CONFIRMED",
                    "capability_snapshot_id": cap.snapshot_id,
                    "draft": dangerous,
                })
            self.assertEqual(wire.requests, [])

    def test_capability_projection_is_ready_without_secret_material(self):
        with TemporaryDirectory() as directory:
            gateway, cap, _quota, _journal = self.composition(directory)
            projection = gateway.capability_projection()
            self.assertTrue(projection["current"])
            self.assertEqual(projection["capability_snapshot_id"], cap.snapshot_id)
            self.assertEqual(projection["credential_purpose"], "TRADE")
            self.assertEqual(projection["forbidden_permissions"], [])
            self.assertNotIn("api", json.dumps(projection).lower())


class WhiteBitTradeSetupTests(unittest.TestCase):
    def test_setup_persists_only_non_secret_binding_metadata(self):
        with TemporaryDirectory() as directory:
            vault = ProtectedCredentialVault(
                Path(directory) / "credentials.json", protector=ReversibleProtector()
            )
            handle, boundary = install_whitebit_trade_binding(
                vault,
                data_dir=directory,
                api_key="visible-only-to-vault",
                api_secret="secret-only-to-vault",
                permissions=frozenset({"INFO", "TRADING"}),
                ip_whitelist_enabled=True,
                observed_at=NOW,
                evidence_ref="operator:test-settings",
            )
            binding_bytes = (Path(directory) / "whitebit-live-trade-binding.json").read_text("utf-8")
            self.assertNotIn("visible-only-to-vault", binding_bytes)
            self.assertNotIn("secret-only-to-vault", binding_bytes)
            loaded_handle, loaded_boundary = load_whitebit_trade_binding(
                vault, data_dir=directory
            )
            self.assertEqual(loaded_handle, handle)
            self.assertEqual(loaded_boundary, boundary)

    def test_setup_rejects_broad_or_non_ip_restricted_key_before_storage(self):
        for permissions, ip_enabled in (
            (frozenset({"INFO", "TRADING", "WITHDRAW"}), True),
            (frozenset({"INFO", "TRADING"}), False),
        ):
            with self.subTest(permissions=permissions, ip_enabled=ip_enabled):
                with TemporaryDirectory() as directory:
                    vault = ProtectedCredentialVault(
                        Path(directory) / "credentials.json", protector=ReversibleProtector()
                    )
                    with self.assertRaisesRegex(ValueError, "permissions|IP whitelist"):
                        install_whitebit_trade_binding(
                            vault,
                            data_dir=directory,
                            api_key="key",
                            api_secret="secret",
                            permissions=permissions,
                            ip_whitelist_enabled=ip_enabled,
                            observed_at=NOW,
                            evidence_ref="operator:test-settings",
                        )
                    self.assertEqual(
                        vault.export_reattachment_manifest()["records"], []
                    )


class ControlledLiveGateway:
    def __init__(self):
        self.calls = []

    def capability_projection(self):
        return {
            "provider_id": "WHITEBIT",
            "environment": "LIVE",
            "account_id": "whitebit-primary",
            "permission_scopes": [
                "ACCOUNT_READ", "ORDER_MANAGEMENT", "ORDER_WRITE",
            ],
            "capability_snapshot_id": "controlled-current-capability",
            "current": True,
            "credential_profile": "controlled-protected-trade-handle",
            "credential_purpose": "TRADE",
            "forbidden_permissions": [],
            "blocker": None,
        }

    def __call__(self, command):
        self.calls.append(dict(command))
        return {
            "schema": "autotrade-whitebit-manual-live-result-v1",
            "outcome": "SENT",
            "reason": "sent_confirmed",
            "client_order_id": "at-controlled",
            "provider_send_may_have_occurred": True,
            "fill_confirmed": False,
            "requires_reconciliation": False,
            "attempt_id": "controlled-attempt",
        }


class RuntimeClient:
    def __init__(self, directory, *, gateway_factory=None):
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        self.port = probe.getsockname()[1]
        probe.close()
        self.origin = f"http://127.0.0.1:{self.port}"
        self.runtime, url = build_product(
            directory,
            port=self.port,
            desktop_session_sink=lambda **_kwargs: None,
            whitebit_live_gateway_factory=gateway_factory,
        )
        self.worker = Thread(target=self.runtime.serve_forever)
        self.worker.start()
        self.cookie = None
        status, _value, headers = self.request(
            "POST",
            "/api/v1/session",
            {"pairing_code": url.split("#pair=")[1]},
        )
        if status != 200:
            raise AssertionError("product pairing failed")
        self.cookie = headers["Set-Cookie"].split(";", 1)[0]

    def request(self, method, path, payload=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {"Origin": self.origin}
        if self.cookie is not None:
            headers["Cookie"] = self.cookie
        if payload is not None:
            headers["Content-Type"] = "application/json"
        try:
            connection.request(
                method,
                path,
                None if payload is None else json.dumps(payload),
                headers,
            )
            response = connection.getresponse()
            raw = response.read()
            value = json.loads(raw) if "json" in response.getheader("Content-Type", "") else raw
            return response.status, value, dict(response.getheaders())
        finally:
            connection.close()

    def close(self):
        self.runtime.close()
        self.worker.join(timeout=10)
        if self.worker.is_alive():
            raise AssertionError("product worker did not stop")


class WhiteBitManualRuntimeCompositionTests(unittest.TestCase):
    def current_quote(self, _client, market):
        value = public_quote(market)
        value["available_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        value["published_at"] = value["available_at"]
        return value

    def factory(self, gateway):
        def build(**kwargs):
            self.assertIsInstance(kwargs["journal"], JournalStore)
            self.assertIsInstance(kwargs["security_boundary"], SecurityBoundary)
            self.assertTrue(callable(kwargs["public_snapshot_provider"]))
            self.assertTrue(callable(kwargs["session_token_provider"]))
            self.assertTrue(callable(kwargs["origin_provider"]))
            return gateway
        return build

    def test_absent_binding_keeps_runtime_available_but_order_entry_disabled(self):
        with TemporaryDirectory() as directory:
            client = RuntimeClient(directory)
            try:
                status, value, _headers = client.request("GET", "/api/v1/manual/status")
                self.assertEqual(status, 200)
                self.assertFalse(value["order_entry_enabled"])
                self.assertFalse(value["canonical_sender_bound"])
                self.assertIsNotNone(value["activation_blocker"])
            finally:
                client.close()

    def test_valid_controlled_composition_enables_and_confirm_reaches_wire_once(self):
        gateway = ControlledLiveGateway()
        with TemporaryDirectory() as directory, patch(
            "mvp.autotrade_mvp.product_runtime.read_whitebit_live_spot_snapshot",
            side_effect=self.current_quote,
        ):
            client = RuntimeClient(directory, gateway_factory=self.factory(gateway))
            try:
                status, value, _headers = client.request("GET", "/api/v1/manual/status")
                self.assertEqual(status, 200)
                self.assertTrue(value["order_entry_enabled"])
                request_id = str(uuid4())
                status, prepared, _headers = client.request(
                    "POST", "/api/v1/manual/prepare", {
                        "request_id": request_id,
                        "market": "BTC_USDT",
                        "action": "BUY",
                        "quantity": "0.001",
                        "order_type": "LIMIT",
                        "limit_price": "59000",
                    },
                )
                self.assertEqual(status, 200)
                confirm = {
                    "draft_id": prepared["draft"]["draft_id"],
                    "confirmation": "CONFIRM REAL WHITEBIT ORDER",
                }
                self.assertEqual(client.request("POST", "/api/v1/manual/confirm", confirm)[0], 200)
                self.assertEqual(client.request("POST", "/api/v1/manual/confirm", confirm)[0], 200)
                self.assertEqual(len(gateway.calls), 1)
            finally:
                client.close()

    def test_paused_runtime_blocks_before_controlled_gateway(self):
        gateway = ControlledLiveGateway()
        with TemporaryDirectory() as directory, patch(
            "mvp.autotrade_mvp.product_runtime.read_whitebit_live_spot_snapshot",
            side_effect=self.current_quote,
        ):
            client = RuntimeClient(directory, gateway_factory=self.factory(gateway))
            try:
                status, prepared, _headers = client.request(
                    "POST", "/api/v1/manual/prepare", {
                        "request_id": str(uuid4()),
                        "market": "TON_USDT",
                        "action": "SELL",
                        "quantity": "1",
                        "order_type": "LIMIT",
                        "limit_price": "5.00",
                    },
                )
                self.assertEqual(status, 200)
                command_id = str(uuid4())
                payload = canonical_runtime_mode_payload(
                    client.runtime.journal,
                    {"mode": "PAUSED"},
                    command_id,
                    "simulation-account",
                    "SIMULATION",
                )
                execute_runtime_mode_action(
                    client.runtime.journal,
                    payload,
                    datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                )
                status, _value, _headers = client.request(
                    "POST", "/api/v1/manual/confirm", {
                        "draft_id": prepared["draft"]["draft_id"],
                        "confirmation": "CONFIRM REAL WHITEBIT ORDER",
                    },
                )
                self.assertEqual(status, 403)
                self.assertEqual(gateway.calls, [])
            finally:
                client.close()


if __name__ == "__main__":
    unittest.main()
