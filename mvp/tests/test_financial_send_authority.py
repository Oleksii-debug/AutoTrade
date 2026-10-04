from dataclasses import replace
import unittest

from mvp.autotrade_mvp.financial_request_binding import FinancialRequestBindingMaterial
from mvp.autotrade_mvp.financial_send_authority import (
    FinancialSendAuthority,
    FinancialSendAuthorityError,
    require_exact_bybit_financial_request,
)
from mvp.autotrade_mvp.persistence import payload_digest


RS = "risk-snapshot:sha256:" + "1" * 64
RD = "risk:sha256:" + "2" * 64
AC = "provider-account-cut:sha256:" + "3" * 64
PS = "provider-financial-scope:sha256:" + "4" * 64
Q = "provider-qualification:sha256:" + "5" * 64
D6 = "sha256:" + "6" * 64
D7 = "sha256:" + "7" * 64
D8 = "sha256:" + "8" * 64


def exact_request():
    body = {
        "category": "linear",
        "symbol": "BTCUSDT",
        "side": "Buy",
        "orderType": "Limit",
        "qty": "2",
        "timeInForce": "GTC",
        "orderLinkId": "client-order-1",
        "price": "30000",
        "reduceOnly": False,
        "positionIdx": 0,
    }
    body_sha = payload_digest(body)
    request = {
        "endpoint": "/v5/order/create",
        "body": body,
        "account_id": "account-1",
        "environment": "PAPER",
        "provider_environment": "TESTNET",
        "capability_snapshot_id": "capability-1",
        "entity_id": "entity-1",
        "capability_snapshot_ids": ["capability-1"],
        "instrument_versions": ["7"],
        "body_sha256": body_sha,
    }
    return request, body_sha


def exact_scope():
    return {
        "provider_id": "BYBIT",
        "account_id": "account-1",
        "environment": "PAPER",
        "provider_environment": "TESTNET",
        "capability_snapshot_id": "capability-1",
    }


def binding():
    request, body_sha = exact_request()
    scope = exact_scope()
    return FinancialRequestBindingMaterial(
        risk_snapshot_id=RS,
        risk_decision_id=RD,
        admitted_journal_sequence_cut=41,
        account_cut_id=AC,
        account_cut_digest=D6,
        account_head_journal_sequence=37,
        reservation_id="reservation-1",
        reservation_scope_digest=D7,
        reservation_version=3,
        reservation_state_digest=D8,
        provider_scope_digest=PS,
        provider_id="BYBIT",
        account_id="account-1",
        runtime_environment="PAPER",
        provider_environment="TESTNET",
        entity_policy_id="linear-order-v1",
        instrument_id="00000000-0000-0000-0000-000000000101",
        instrument_version=7,
        quantity_unit="CONTRACT",
        equivalent_exposure_digest=D6,
        capability_snapshot_id="capability-1",
        qualification_identity_digest=Q,
        client_order_id="client-order-1",
        side="BUY",
        quantity="2",
        price="30000",
        price_semantics_digest=D7,
        order_type="LIMIT",
        time_in_force="GTC",
        reduce_only=False,
        trigger_protection_digest=D8,
        endpoint="/v5/order/create",
        query_sha256=payload_digest({}),
        body_sha256=body_sha,
        request_sha256=payload_digest(request),
        submission_scope_digest=payload_digest(scope),
    )


class ExactBybitFinancialRequestTests(unittest.TestCase):
    def test_exact_prepared_projection_and_scope_are_admitted(self):
        request, _ = exact_request()
        require_exact_bybit_financial_request(
            binding(),
            request,
            exact_scope(),
            provider_environment="TESTNET",
        )

    def test_request_retarget_fails_even_when_body_shape_stays_valid(self):
        request, _ = exact_request()
        retargeted = dict(request)
        retargeted["account_id"] = "account-2"
        with self.assertRaisesRegex(
            FinancialSendAuthorityError,
            "request digest differs",
        ):
            require_exact_bybit_financial_request(
                binding(),
                retargeted,
                exact_scope(),
                provider_environment="TESTNET",
            )

    def test_economics_retarget_fails_against_binding_even_with_rebound_request_digest(self):
        request, _ = exact_request()
        changed_body = dict(request["body"])
        changed_body["qty"] = "3"
        changed_request = dict(request)
        changed_request["body"] = changed_body
        changed_request["body_sha256"] = payload_digest(changed_body)
        rebound = replace(
            binding(),
            request_sha256=payload_digest(changed_request),
            body_sha256=payload_digest(changed_body),
        )
        with self.assertRaisesRegex(
            FinancialSendAuthorityError,
            "quantity differs",
        ):
            require_exact_bybit_financial_request(
                rebound,
                changed_request,
                exact_scope(),
                provider_environment="TESTNET",
            )

    def test_submission_scope_retarget_fails_closed(self):
        request, _ = exact_request()
        retargeted_scope = dict(exact_scope())
        retargeted_scope["provider_environment"] = "DEMO"
        with self.assertRaisesRegex(
            FinancialSendAuthorityError,
            "submission scope differs",
        ):
            require_exact_bybit_financial_request(
                binding(),
                request,
                retargeted_scope,
                provider_environment="TESTNET",
            )

    def test_provider_environment_retarget_fails_before_dispatch(self):
        request, _ = exact_request()
        with self.assertRaisesRegex(
            FinancialSendAuthorityError,
            "provider environment differs",
        ):
            require_exact_bybit_financial_request(
                binding(),
                request,
                exact_scope(),
                provider_environment="DEMO",
            )

    def test_direct_capability_construction_is_rejected(self):
        with self.assertRaisesRegex(
            FinancialSendAuthorityError,
            "must be minted",
        ):
            FinancialSendAuthority(
                issuer_identity=object(),
                binding=binding(),
                admission_id="admission-1",
                intent_id="intent-1",
                intent_hash="intent-hash-1",
                action="TRADE",
                authority_check=lambda _intent_hash, _now: (True, "allowed"),
            )


if __name__ == "__main__":
    unittest.main()
