from datetime import datetime, timedelta, timezone
import json
from types import MappingProxyType
import unittest
from uuid import uuid4

from mvp.autotrade_mvp.binance_spot import parse_account_trades as parse_spot_account_trades
from mvp.autotrade_mvp.binance_usdm import (
    BinanceUsdmAdapterError,
    parse_order_ack as parse_usdm_order_ack,
)
from mvp.autotrade_mvp.capabilities import (
    CapabilityClaim,
    EvidenceVerification,
    derive_capability_snapshot,
)
from mvp.autotrade_mvp.provider_core import (
    Surface,
    observe_authenticated_json_response,
    prepare_authenticated_read_query,
)


NOW = datetime(2026, 10, 5, 8, tzinfo=timezone.utc)


def _spot_capability():
    observed_at = NOW - timedelta(hours=1)
    common = dict(
        provider_id="BINANCE",
        account_id="paper-1",
        entity_id="global",
        environment="PAPER",
        instrument_version="BTCUSDT:v1",
        observed_at=observed_at,
        expires_at=NOW + timedelta(hours=1),
        supported_order_types=frozenset({"LIMIT", "MARKET"}),
        time_in_force=frozenset({"GTC", "IOC", "FOK", "NONE"}),
        permission_scopes=frozenset({"TRADE.READ"}),
        position_mode="NET",
        native_protection=frozenset(),
        rate_limit_policy_id="binance-provider-ingress-regression",
        data_entitlements=frozenset({"TRADES"}),
    )
    claims = tuple(
        CapabilityClaim(
            source=source,
            **common,
            evidence_ref={
                "artifact_id": str(uuid4()),
                "sha256": "sha256:" + "d" * 64,
                "observed_at": observed_at.isoformat().replace("+00:00", "Z"),
                "source_uri": "https://developers.binance.com/docs/binance-spot-api-docs/rest-api/account-endpoints",
            },
        )
        for source in ("DOCUMENTED", "API", "ACCOUNT", "INSTRUMENT")
    )
    return derive_capability_snapshot(
        snapshot_id=str(uuid4()),
        claims=claims,
        observed_at=observed_at,
        evidence_verifier=lambda _claim: EvidenceVerification(valid=True),
    )


def _spot_trade_observation():
    query = prepare_authenticated_read_query(
        capability=_spot_capability(),
        surface=Surface.ACTIVITIES,
        endpoint="/api/v3/myTrades",
        query={"symbol": "BTCUSDT"},
        at=NOW,
        permission_scope="TRADE.READ",
    )
    rows = [
        {
            "symbol": "BTCUSDT",
            "id": 7,
            "orderId": 42,
            "isBuyer": True,
            "qty": "0.100",
            "price": "40000.00",
            "commission": "0.01",
            "commissionAsset": "USDT",
            "time": 1791187200123,
        }
    ]
    return observe_authenticated_json_response(
        query_binding=query,
        http_status=200,
        response_bytes=json.dumps(
            rows,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8"),
        observed_at=NOW,
    )


class BinanceProviderIngressRegressionTests(unittest.TestCase):
    def test_spot_accepts_canonical_frozen_authenticated_trade_payload(self):
        observation = _spot_trade_observation()

        self.assertIs(type(observation.payload), tuple)
        self.assertEqual(len(observation.payload), 1)
        self.assertIs(type(observation.payload[0]), MappingProxyType)
        self.assertIs(type(observation.payload[0]["id"]), int)
        self.assertIs(type(observation.payload[0]["orderId"]), int)

        fills = parse_spot_account_trades(
            observation,
            instrument_versions={"BTCUSDT": "BTCUSDT:v1"},
            client_ids_by_order_id={42: "spot-client-42"},
        )
        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0].client_order_id, "spot-client-42")
        self.assertEqual(fills[0].provider_execution_id, "BINANCE-SPOT:BTCUSDT:7")

    def test_usdm_ack_rejects_mapping_subclass_before_get_callback(self):
        callbacks = []

        class HostileResponse(dict):
            def get(self, key, default=None):
                callbacks.append(key)
                raise AssertionError("hostile response mapping callback executed")

        response = HostileResponse(
            {
                "symbol": "BTCUSDT",
                "orderId": 7,
                "clientOrderId": "usdm-client-7",
                "updateTime": 1791187200123,
            }
        )
        with self.assertRaisesRegex(BinanceUsdmAdapterError, "exact decoded object"):
            parse_usdm_order_ack(
                attempt_id=str(uuid4()),
                client_order_id="usdm-client-7",
                response=response,
            )
        self.assertEqual(callbacks, [])

    def test_usdm_ack_rejects_int_subclass_before_comparison_callback(self):
        callbacks = []

        class HostileInt(int):
            def __lt__(self, other):
                callbacks.append(("lt", other))
                raise AssertionError("hostile integer comparison executed")

        with self.assertRaisesRegex(BinanceUsdmAdapterError, "non-negative integer"):
            parse_usdm_order_ack(
                attempt_id=str(uuid4()),
                client_order_id="usdm-client-8",
                response={
                    "symbol": "BTCUSDT",
                    "orderId": HostileInt(8),
                    "clientOrderId": "usdm-client-8",
                    "updateTime": 1791187200123,
                },
            )
        self.assertEqual(callbacks, [])


if __name__ == "__main__":
    unittest.main()
