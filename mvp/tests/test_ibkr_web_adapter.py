from datetime import datetime, timedelta, timezone
from decimal import Decimal
import unittest
from uuid import uuid4

from mvp.autotrade_mvp.capabilities import CapabilitySnapshot
from mvp.autotrade_mvp.ibkr_web import (
    IbkrAbsenceEvidence,
    IbkrBrokerageSessionStatus,
    IbkrContractIdentity,
    IbkrExecutionEvidence,
    IbkrWebAdapterError,
    IbkrWebOrderIntent,
    execution_to_reconciliation_fill,
    parse_order_submission_response,
    prepare_normalized_order,
    prepare_reply_confirmation,
)


NOW = datetime(2026, 9, 24, 20, tzinfo=timezone.utc)


def capability(*, account_id="U1234567", order_types=("MARKET", "LIMIT", "STOP", "STOP_LIMIT")):
    evidence = {
        "artifact_id": str(uuid4()),
        "sha256": "sha256:" + "e" * 64,
        "observed_at": "2026-09-24T19:00:00Z",
        "source_uri": "https://www.interactivebrokers.com/docs/web-api/v1/endpoints/orders/place-order",
    }
    return CapabilitySnapshot(
        snapshot_id=str(uuid4()),
        provider_id="IBKR",
        account_id=account_id,
        entity_id="web-api",
        environment="PAPER",
        instrument_version="AAPL-CONID-265598:v1",
        observed_at=NOW - timedelta(hours=1),
        expires_at=NOW + timedelta(hours=1),
        supported_order_types=frozenset(order_types),
        time_in_force=frozenset({"DAY", "GTC", "IOC"}),
        permission_scopes=frozenset({"ORDER_WRITE"}),
        position_mode="NET",
        native_protection=frozenset(),
        rate_limit_policy_id="ibkr-web-paper",
        data_entitlements=frozenset({"ORDERS", "EXECUTIONS"}),
        evidence=(evidence,),
        status="VERIFIED",
        sources=frozenset({"DOCUMENTED", "API", "ACCOUNT", "INSTRUMENT"}),
    )


def ready_session(**overrides):
    values = dict(
        connected=True,
        authenticated=True,
        established=True,
        competing=False,
        observed_at=NOW - timedelta(seconds=1),
    )
    values.update(overrides)
    return IbkrBrokerageSessionStatus(**values)


class IbkrWebAdapterTests(unittest.TestCase):
    def test_trade_session_requires_all_ready_flags_and_no_competitor(self):
        self.assertTrue(ready_session().trade_ready)
        for override in (
            {"connected": False},
            {"authenticated": False},
            {"established": False},
            {"competing": True},
        ):
            with self.subTest(override=override):
                with self.assertRaises(IbkrWebAdapterError):
                    ready_session(**override).require_trade_ready()

    def test_contract_identity_never_falls_back_to_ticker(self):
        self.assertEqual(IbkrContractIdentity(conid=265598).contract_key, "265598")
        self.assertEqual(
            IbkrContractIdentity(conidex="557335679@ZEROHASH").contract_key,
            "557335679@ZEROHASH",
        )
        with self.assertRaises(IbkrWebAdapterError):
            IbkrContractIdentity()
        with self.assertRaises(IbkrWebAdapterError):
            IbkrContractIdentity(conid=265598, conidex="265598@SMART")

    def test_normalized_limit_order_preserves_exact_decimal_outside_provider_double(self):
        intent = IbkrWebOrderIntent.create(
            instrument_version="AAPL-CONID-265598:v1",
            account_id="U1234567",
            contract=IbkrContractIdentity(conid=265598),
            side="BUY",
            order_type="LIMIT",
            time_in_force="DAY",
            quantity="1.25",
            limit_price="220.10",
        )
        prepared = prepare_normalized_order(
            intent,
            client_order_id="at-ibkr-1",
            capability=capability(),
            session=ready_session(),
            at=NOW,
        )
        self.assertEqual(prepared.endpoint, "/iserver/account/U1234567/orders")
        self.assertEqual(prepared.fields["conid"], 265598)
        self.assertEqual(prepared.fields["orderType"], "LMT")
        self.assertEqual(prepared.exact_quantity_text, "1.25")
        self.assertEqual(prepared.exact_limit_price_text, "220.10")
        self.assertFalse(prepared.provider_serialization_qualified)
        self.assertNotIn("quantity", prepared.fields)
        self.assertNotIn("price", prepared.fields)

    def test_crypto_like_routed_contract_uses_conidex(self):
        intent = IbkrWebOrderIntent.create(
            instrument_version="AAPL-CONID-265598:v1",
            account_id="U1234567",
            contract=IbkrContractIdentity(conidex="557335679@ZEROHASH"),
            side="SELL",
            order_type="MARKET",
            time_in_force="DAY",
            quantity="0.01",
        )
        prepared = prepare_normalized_order(
            intent,
            client_order_id="at-route-1",
            capability=capability(),
            session=ready_session(),
            at=NOW,
        )
        self.assertEqual(prepared.fields["conidex"], "557335679@ZEROHASH")
        self.assertNotIn("conid", prepared.fields)

    def test_binary_float_quantity_is_rejected(self):
        with self.assertRaises(IbkrWebAdapterError):
            IbkrWebOrderIntent.create(
                instrument_version="AAPL-CONID-265598:v1",
                account_id="U1234567",
                contract=IbkrContractIdentity(conid=265598),
                side="BUY",
                order_type="MARKET",
                time_in_force="DAY",
                quantity=1.0,
            )

    def test_future_session_observation_cannot_authorize(self):
        intent = IbkrWebOrderIntent.create(
            instrument_version="AAPL-CONID-265598:v1",
            account_id="U1234567",
            contract=IbkrContractIdentity(conid=265598),
            side="BUY",
            order_type="MARKET",
            time_in_force="DAY",
            quantity="1",
        )
        with self.assertRaisesRegex(IbkrWebAdapterError, "future"):
            prepare_normalized_order(
                intent,
                client_order_id="at-future-1",
                capability=capability(),
                session=ready_session(observed_at=NOW + timedelta(seconds=1)),
                at=NOW,
            )

    def test_account_capability_must_match_exact_account(self):
        intent = IbkrWebOrderIntent.create(
            instrument_version="AAPL-CONID-265598:v1",
            account_id="U1234567",
            contract=IbkrContractIdentity(conid=265598),
            side="BUY",
            order_type="MARKET",
            time_in_force="DAY",
            quantity="1",
        )
        with self.assertRaisesRegex(IbkrWebAdapterError, "account"):
            prepare_normalized_order(
                intent,
                client_order_id="at-account-1",
                capability=capability(account_id="OTHER"),
                session=ready_session(),
                at=NOW,
            )

    def test_execution_identity_uses_exec_id_and_perm_id(self):
        execution = IbkrExecutionEvidence.create(
            execution_id="0001.123.01",
            permanent_order_id=778899,
            account_id="U1234567",
            quantity="0.5",
            price="220.10",
        )
        self.assertEqual(execution.execution_id, "0001.123.01")
        self.assertEqual(execution.permanent_order_id, "778899")
        self.assertEqual(execution.quantity, Decimal("0.5"))

    def test_incomplete_execution_surfaces_do_not_prove_absence(self):
        evidence = IbkrAbsenceEvidence(
            open_orders_complete=True,
            completed_orders_complete=True,
            executions_complete=False,
            account_activity_complete=True,
            consistency_horizon_satisfied=True,
            order_found=False,
        )
        self.assertEqual(evidence.verdict(), "INCONCLUSIVE")

    def test_complete_order_execution_activity_evidence_can_prove_absence(self):
        evidence = IbkrAbsenceEvidence(
            open_orders_complete=True,
            completed_orders_complete=True,
            executions_complete=True,
            account_activity_complete=True,
            consistency_horizon_satisfied=True,
            order_found=False,
        )
        self.assertEqual(evidence.verdict(), "PROVEN_ABSENT")


    def test_acknowledgement_is_not_fill_or_retry_permission(self):
        outcome = parse_order_submission_response(
            [
                {
                    "order_id": "1234567890",
                    "order_status": "Submitted",
                    "encrypt_message": "1",
                }
            ]
        )
        self.assertEqual(outcome.status, "ACKNOWLEDGED")
        self.assertEqual(outcome.provider_order_id, "1234567890")
        self.assertFalse(outcome.proves_fill)
        self.assertFalse(outcome.retry_same_economic_action)

    def test_reply_message_requires_separate_explicit_guarded_authorization(self):
        outcome = parse_order_submission_response(
            [
                {
                    "id": "07a13a5a-4a48-44a5-bb25-5ab37b79186c",
                    "message": ["Order exceeds configured price constraint."],
                    "isSuppressed": False,
                    "messageIds": ["o163"],
                }
            ]
        )
        self.assertEqual(outcome.status, "REPLY_REQUIRED")
        self.assertFalse(outcome.proves_fill)
        with self.assertRaisesRegex(IbkrWebAdapterError, "explicit authorization"):
            prepare_reply_confirmation(outcome, explicit_authorization=False)
        request = prepare_reply_confirmation(outcome, explicit_authorization=True)
        self.assertEqual(
            request.endpoint,
            "/iserver/reply/07a13a5a-4a48-44a5-bb25-5ab37b79186c",
        )
        self.assertEqual(dict(request.body), {"confirmed": True})

    def test_ambiguous_ack_and_reply_shape_fails_closed(self):
        with self.assertRaisesRegex(IbkrWebAdapterError, "ambiguous"):
            parse_order_submission_response(
                [
                    {
                        "order_id": "123",
                        "order_status": "Submitted",
                        "id": "reply-1",
                        "message": ["Confirm"],
                    }
                ]
            )

    def test_explicit_provider_error_is_rejected_but_not_retryable(self):
        outcome = parse_order_submission_response([{"error": "order rejected"}])
        self.assertEqual(outcome.status, "REJECTED")
        self.assertEqual(outcome.rejection_reason, "order rejected")
        self.assertFalse(outcome.retry_same_economic_action)

    def test_unique_execution_maps_to_canonical_reconciliation_fill(self):
        execution = IbkrExecutionEvidence.create(
            execution_id="0001.123.01",
            permanent_order_id=778899,
            account_id="U1234567",
            quantity="0.5",
            price="220.10",
        )
        fill = execution_to_reconciliation_fill(
            execution,
            client_order_id="at-ibkr-1",
            instrument="AAPL-CONID-265598:v1",
            fee_amount="-0.35",
            fee_currency="USD",
            trade_time="2026-09-24T20:00:01Z",
        )
        self.assertEqual(fill.provider_execution_id, "0001.123.01")
        self.assertEqual(fill.client_order_id, "at-ibkr-1")
        self.assertEqual(fill.quantity, Decimal("0.5"))
        self.assertEqual(fill.price, Decimal("220.10"))
        self.assertEqual(fill.fee_amount, Decimal("-0.35"))
        self.assertEqual(fill.fee_currency, "USD")



if __name__ == "__main__":
    unittest.main()
