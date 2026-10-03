from datetime import datetime, timedelta, timezone
from decimal import Decimal
import unittest
from uuid import uuid4

from mvp.autotrade_mvp.capabilities import CapabilitySnapshot
from mvp.autotrade_mvp.whitebit import (
    WhiteBitAdapterError,
    WhiteBitMarketRules,
    WhiteBitOrderIntent,
    prepare_order_request,
    validate_intent_market_rules,
)


NOW = datetime(2026, 9, 24, 20, tzinfo=timezone.utc)


def market_rules() -> WhiteBitMarketRules:
    return WhiteBitMarketRules(
        market="BTC_USDT",
        market_type="SPOT",
        is_tradfi_futures=False,
        is_collateral=False,
        trades_enabled=True,
        step_size=Decimal("1"),
        tick_size=Decimal("1"),
        min_amount=Decimal("1"),
        min_total=Decimal("1"),
        max_total=Decimal("1000"),
        delisted_at=None,
    )


def non_admitting_capability() -> CapabilitySnapshot:
    return CapabilitySnapshot(
        snapshot_id=str(uuid4()),
        provider_id="WHITEBIT",
        account_id="account-1",
        entity_id="global",
        environment="PAPER",
        instrument_version="BTC_USDT:v1",
        observed_at=NOW - timedelta(minutes=1),
        expires_at=NOW + timedelta(minutes=1),
        supported_order_types=frozenset({"LIMIT"}),
        time_in_force=frozenset({"GTC"}),
        permission_scopes=frozenset({"ORDER_WRITE"}),
        position_mode="NET",
        native_protection=frozenset(),
        rate_limit_policy_id="whitebit-test",
        data_entitlements=frozenset(),
        evidence=(),
        status="UNKNOWN",
        sources=frozenset(),
    )


class WhiteBitMarketRuleAuthorityTests(unittest.TestCase):
    def test_direct_market_rules_enforce_canonical_financial_invariants(self):
        with self.assertRaisesRegex(WhiteBitAdapterError, "step_size must be positive"):
            WhiteBitMarketRules(
                market="BTC_USDT",
                market_type="SPOT",
                is_tradfi_futures=False,
                is_collateral=False,
                trades_enabled=True,
                step_size=Decimal("0"),
                tick_size=Decimal("1"),
                min_amount=Decimal("1"),
                min_total=Decimal("1"),
                max_total=Decimal("1000"),
                delisted_at=None,
            )

        with self.assertRaisesRegex(WhiteBitAdapterError, "must be boolean"):
            WhiteBitMarketRules(
                market="BTC_USDT",
                market_type="SPOT",
                is_tradfi_futures=False,
                is_collateral=1,
                trades_enabled=True,
                step_size=Decimal("1"),
                tick_size=Decimal("1"),
                min_amount=Decimal("1"),
                min_total=Decimal("1"),
                max_total=Decimal("1000"),
                delisted_at=None,
            )

        with self.assertRaisesRegex(
            WhiteBitAdapterError,
            "is_tradfi_futures must be true exactly",
        ):
            WhiteBitMarketRules(
                market="RIVN_PERP",
                market_type="TRADFIFUTURES",
                is_tradfi_futures=False,
                is_collateral=False,
                trades_enabled=True,
                step_size=Decimal("1"),
                tick_size=Decimal("1"),
                min_amount=Decimal("1"),
                min_total=Decimal("1"),
                max_total=None,
                delisted_at=None,
            )

    def test_direct_market_rules_reject_text_subclass_before_virtual_reads(self):
        calls = []

        class HostileText(str):
            def strip(self, *args, **kwargs):
                calls.append("strip")
                return super().strip(*args, **kwargs)

            def upper(self):
                calls.append("upper")
                return super().upper()

        with self.assertRaisesRegex(
            WhiteBitAdapterError,
            "market must use exact text",
        ):
            WhiteBitMarketRules(
                market=HostileText("BTC_USDT"),
                market_type="SPOT",
                is_tradfi_futures=False,
                is_collateral=False,
                trades_enabled=True,
                step_size=Decimal("1"),
                tick_size=Decimal("1"),
                min_amount=Decimal("1"),
                min_total=Decimal("1"),
                max_total=Decimal("1000"),
                delisted_at=None,
            )
        self.assertEqual(calls, [])

    def test_provider_market_rules_reject_mapping_subclass_before_virtual_reads(self):
        calls = []

        class HostilePayload(dict):
            def __getitem__(self, key):
                calls.append(key)
                if key == "tradesEnabled":
                    return True
                return super().__getitem__(key)

        payload = HostilePayload(
            {
                "name": "BTC_USDT",
                "type": "SPOT",
                "isTradFiFutures": False,
                "isCollateral": False,
                "tradesEnabled": False,
                "stepSize": "1",
                "tickSize": "1",
                "minAmount": "1",
                "minTotal": "1",
                "maxTotal": "1000",
                "delistedAt": None,
            }
        )
        with self.assertRaises(TypeError):
            WhiteBitMarketRules.from_provider(payload)
        self.assertEqual(calls, [])

    def test_prepare_rejects_market_rule_subclass_before_virtual_fields(self):
        intent = WhiteBitOrderIntent.create(
            instrument_version="BTC_USDT:v1",
            product_family="SPOT",
            market="BTC_USDT",
            side="BUY",
            order_type="LIMIT",
            amount="1",
            price="10",
        )
        calls = []

        class HostileRules(WhiteBitMarketRules):
            def __getattribute__(self, name):
                if name in {
                    "market",
                    "market_type",
                    "trades_enabled",
                    "step_size",
                    "tick_size",
                }:
                    calls.append(name)
                return super().__getattribute__(name)

        rules = HostileRules(
            market="BTC_USDT",
            market_type="SPOT",
            is_tradfi_futures=False,
            is_collateral=False,
            trades_enabled=True,
            step_size=Decimal("1"),
            tick_size=Decimal("1"),
            min_amount=Decimal("1"),
            min_total=Decimal("1"),
            max_total=Decimal("1000"),
            delisted_at=None,
        )
        with self.assertRaises(TypeError):
            prepare_order_request(
                intent,
                client_order_id="cid-1",
                account_id="account-1",
                environment="PAPER",
                capability=non_admitting_capability(),
                market_rules=rules,
                at=NOW,
            )
        self.assertEqual(calls, [])

    def test_market_rule_subclass_cannot_virtualize_provider_admission(self):
        intent = WhiteBitOrderIntent.create(
            instrument_version="BTC_USDT:v1",
            product_family="SPOT",
            market="BTC_USDT",
            side="BUY",
            order_type="LIMIT",
            amount="1",
            price="10",
        )
        calls = []

        class HostileRules(WhiteBitMarketRules):
            def __getattribute__(self, name):
                if name in {
                    "market",
                    "market_type",
                    "is_collateral",
                    "trades_enabled",
                    "step_size",
                    "tick_size",
                    "min_amount",
                    "min_total",
                    "max_total",
                    "delisted_at",
                }:
                    calls.append(name)
                    if name == "trades_enabled":
                        return True
                return super().__getattribute__(name)

        rules = HostileRules(
            market="BTC_USDT",
            market_type="SPOT",
            is_tradfi_futures=False,
            is_collateral=False,
            trades_enabled=False,
            step_size=Decimal("1"),
            tick_size=Decimal("1"),
            min_amount=Decimal("1"),
            min_total=Decimal("1"),
            max_total=Decimal("1000"),
            delisted_at=None,
        )

        with self.assertRaises(TypeError):
            validate_intent_market_rules(intent, rules, at=NOW)

        self.assertEqual(calls, [])

    def test_prepare_rejects_intent_subclass_before_authority_field_dispatch(self):
        calls = []

        class HostileIntent(WhiteBitOrderIntent):
            def __getattribute__(self, name):
                if name in {
                    "instrument_version",
                    "product_family",
                    "market",
                    "side",
                    "order_type",
                    "amount",
                    "price",
                    "activation_price",
                    "time_in_force",
                    "post_only",
                    "reduce_only",
                    "position_side",
                }:
                    calls.append(name)
                return super().__getattribute__(name)

        intent = HostileIntent(
            instrument_version="BTC_USDT:v1",
            product_family="SPOT",
            market="BTC_USDT",
            side="BUY",
            order_type="LIMIT",
            amount=Decimal("1"),
            price=Decimal("10"),
        )

        with self.assertRaises(TypeError):
            prepare_order_request(
                intent,
                client_order_id="cid-1",
                account_id="account-1",
                environment="PAPER",
                capability=non_admitting_capability(),
                market_rules=market_rules(),
                at=NOW,
            )

        self.assertEqual(calls, [])

    def test_prepare_rejects_capability_subclass_before_virtual_admission(self):
        calls = []

        class HostileCapability(CapabilitySnapshot):
            def __getattribute__(self, name):
                if name in {
                    "provider_id",
                    "account_id",
                    "environment",
                    "instrument_version",
                    "admits",
                    "snapshot_id",
                }:
                    calls.append(name)
                    if name == "admits":
                        return lambda **_kwargs: True
                return super().__getattribute__(name)

        capability = HostileCapability(
            snapshot_id=str(uuid4()),
            provider_id="WHITEBIT",
            account_id="account-1",
            entity_id="global",
            environment="PAPER",
            instrument_version="BTC_USDT:v1",
            observed_at=NOW - timedelta(minutes=1),
            expires_at=NOW + timedelta(minutes=1),
            supported_order_types=frozenset({"LIMIT"}),
            time_in_force=frozenset({"GTC"}),
            permission_scopes=frozenset({"ORDER_WRITE"}),
            position_mode="NET",
            native_protection=frozenset(),
            rate_limit_policy_id="whitebit-test",
            data_entitlements=frozenset(),
            evidence=(),
            status="UNKNOWN",
            sources=frozenset(),
        )
        intent = WhiteBitOrderIntent.create(
            instrument_version="BTC_USDT:v1",
            product_family="SPOT",
            market="BTC_USDT",
            side="BUY",
            order_type="LIMIT",
            amount="1",
            price="10",
        )

        with self.assertRaises(TypeError):
            prepare_order_request(
                intent,
                client_order_id="cid-1",
                account_id="account-1",
                environment="PAPER",
                capability=capability,
                market_rules=market_rules(),
                at=NOW,
            )

        self.assertEqual(calls, [])

    def test_prepare_ignores_instance_shadow_of_capability_admits(self):
        capability = non_admitting_capability()
        calls = []

        def forged_admits(**_kwargs):
            calls.append("admits")
            return True

        object.__setattr__(capability, "admits", forged_admits)
        intent = WhiteBitOrderIntent.create(
            instrument_version="BTC_USDT:v1",
            product_family="SPOT",
            market="BTC_USDT",
            side="BUY",
            order_type="LIMIT",
            amount="1",
            price="10",
        )

        with self.assertRaisesRegex(
            WhiteBitAdapterError,
            "exact capability evidence does not admit this order",
        ):
            prepare_order_request(
                intent,
                client_order_id="cid-1",
                account_id="account-1",
                environment="PAPER",
                capability=capability,
                market_rules=market_rules(),
                at=NOW,
            )

        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
