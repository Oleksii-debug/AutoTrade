from functools import partial
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import unittest
from uuid import uuid4

from mvp.autotrade_mvp.capabilities import (
    CapabilityClaim,
    EvidenceVerification,
    derive_capability_snapshot,
)
from mvp.autotrade_mvp.ibkr_web import (
    IbkrAbsenceEvidence,
    IbkrBrokerageSessionStatus,
    IbkrContractIdentity,
    IbkrExecutionEvidence,
    IbkrReplyRequest,
    IbkrWebAdapterError,
    IbkrWebOrderIntent,
    execution_to_reconciliation_fill,
    parse_cancel_response,
    parse_order_submission_response,
    parse_web_api_trades,
    prepare_normalized_order,
    prepare_reply_confirmation,
    record_order_submission_result,
)


NOW = datetime(2026, 9, 24, 20, tzinfo=timezone.utc)


def capability(*, account_id="U1234567", order_types=("MARKET", "LIMIT", "STOP", "STOP_LIMIT")):
    observed_at = NOW - timedelta(hours=1)
    claims = tuple(
        CapabilityClaim(
            source=source,
            provider_id="IBKR",
            account_id=account_id,
            entity_id="web-api",
            environment="PAPER",
            instrument_version="AAPL-CONID-265598:v1",
            observed_at=observed_at,
            expires_at=NOW + timedelta(hours=1),
            supported_order_types=frozenset(order_types),
            time_in_force=frozenset({"DAY", "GTC", "IOC"}),
            permission_scopes=frozenset({"ORDER_WRITE"}),
            position_mode="NET",
            native_protection=frozenset(),
            rate_limit_policy_id="ibkr-web-paper",
            data_entitlements=frozenset({"ORDERS", "EXECUTIONS"}),
            evidence_ref={
                "artifact_id": str(uuid4()),
                "sha256": "sha256:" + "e" * 64,
                "observed_at": observed_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
                "source_uri": "https://www.interactivebrokers.com/docs/web-api/v1/endpoints/orders/place-order",
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


# Bind only test fixtures; production APIs require explicit account/environment scope.
parse_web_api_trades = partial(parse_web_api_trades, environment="PAPER")
execution_to_reconciliation_fill = partial(
    execution_to_reconciliation_fill,
    environment="PAPER",
)


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

    def test_direct_intent_cannot_bypass_exact_or_regulatory_invariants(self):
        contract = IbkrContractIdentity(conid=265598)
        with self.assertRaisesRegex(IbkrWebAdapterError, "exact decimal"):
            IbkrWebOrderIntent(
                instrument_version="AAPL-CONID-265598:v1",
                account_id="U1234567",
                contract=contract,
                side="BUY",
                order_type="MARKET",
                time_in_force="DAY",
                quantity=1.0,
            )
        with self.assertRaisesRegex(IbkrWebAdapterError, "manual_indicator is required"):
            IbkrWebOrderIntent(
                instrument_version="AAPL-CONID-265598:v1",
                account_id="U1234567",
                contract=contract,
                side="BUY",
                order_type="MARKET",
                time_in_force="DAY",
                quantity=Decimal("1"),
                regulatory_manual_indicator_required=True,
            )
        normalized = IbkrWebOrderIntent(
            instrument_version=" AAPL-CONID-265598:v1 ",
            account_id=" U1234567 ",
            contract=contract,
            side="buy",
            order_type="limit",
            time_in_force="day",
            quantity="1.25",
            limit_price="220.10",
        )
        self.assertEqual(normalized.side, "BUY")
        self.assertEqual(normalized.order_type, "LIMIT")
        self.assertEqual(normalized.quantity, Decimal("1.25"))
        self.assertEqual(normalized.limit_price, Decimal("220.10"))

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
            maximum_session_age_seconds=30,
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
            maximum_session_age_seconds=30,
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
                maximum_session_age_seconds=30,
            )

    def test_stale_session_observation_cannot_authorize(self):
        intent = IbkrWebOrderIntent.create(
            instrument_version="AAPL-CONID-265598:v1",
            account_id="U1234567",
            contract=IbkrContractIdentity(conid=265598),
            side="BUY",
            order_type="MARKET",
            time_in_force="DAY",
            quantity="1",
        )
        with self.assertRaisesRegex(IbkrWebAdapterError, "session evidence is stale"):
            prepare_normalized_order(
                intent,
                client_order_id="at-stale-session",
                capability=capability(),
                session=ready_session(observed_at=NOW - timedelta(seconds=31)),
                at=NOW,
                maximum_session_age_seconds=30,
            )

    def test_session_freshness_policy_must_be_explicit_and_valid(self):
        intent = IbkrWebOrderIntent.create(
            instrument_version="AAPL-CONID-265598:v1",
            account_id="U1234567",
            contract=IbkrContractIdentity(conid=265598),
            side="BUY",
            order_type="MARKET",
            time_in_force="DAY",
            quantity="1",
        )
        with self.assertRaisesRegex(IbkrWebAdapterError, "non-negative integer"):
            prepare_normalized_order(
                intent,
                client_order_id="at-invalid-session-policy",
                capability=capability(),
                session=ready_session(),
                at=NOW,
                maximum_session_age_seconds=True,
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
                maximum_session_age_seconds=30,
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

    def test_execution_permanent_order_id_must_be_positive_integer(self):
        for invalid in (None, True, 0, -1, "778899"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(IbkrWebAdapterError):
                    IbkrExecutionEvidence.create(
                        execution_id="0001.123.01",
                        permanent_order_id=invalid,
                        account_id="U1234567",
                        quantity="0.5",
                        price="220.10",
                    )

    def test_incomplete_execution_surfaces_do_not_prove_absence(self):
        evidence = IbkrAbsenceEvidence(
            exact_client_order_lookup_complete=True,
            exact_client_order_absent=True,
            open_orders_complete=True,
            completed_orders_complete=True,
            executions_complete=False,
            account_activity_complete=True,
            consistency_horizon_satisfied=True,
            exclusion_semantics_qualified=False,
            order_found=False,
        )
        self.assertEqual(evidence.verdict(), "INCONCLUSIVE")

    def test_foundation_cannot_self_assert_exclusion_semantics_qualification(self):
        with self.assertRaisesRegex(
            IbkrWebAdapterError,
            "cannot self-assert exclusion semantics qualification",
        ):
            IbkrAbsenceEvidence(
                exact_client_order_lookup_complete=True,
                exact_client_order_absent=True,
                open_orders_complete=True,
                completed_orders_complete=True,
                executions_complete=True,
                account_activity_complete=True,
                consistency_horizon_satisfied=True,
                exclusion_semantics_qualified=True,
                order_found=False,
            )


    def test_complete_generic_surfaces_without_exact_lookup_are_still_inconclusive(self):
        evidence = IbkrAbsenceEvidence(
            exact_client_order_lookup_complete=False,
            exact_client_order_absent=False,
            open_orders_complete=True,
            completed_orders_complete=True,
            executions_complete=True,
            account_activity_complete=True,
            consistency_horizon_satisfied=True,
            exclusion_semantics_qualified=False,
            order_found=False,
        )
        self.assertEqual(evidence.verdict(), "INCONCLUSIVE")

    def test_absence_requires_qualified_exclusion_semantics(self):
        evidence = IbkrAbsenceEvidence(
            exact_client_order_lookup_complete=True,
            exact_client_order_absent=True,
            open_orders_complete=True,
            completed_orders_complete=True,
            executions_complete=True,
            account_activity_complete=True,
            consistency_horizon_satisfied=True,
            exclusion_semantics_qualified=False,
            order_found=False,
        )
        self.assertEqual(evidence.verdict(), "INCONCLUSIVE")

    def test_contradictory_exact_absence_and_found_order_is_rejected(self):
        with self.assertRaisesRegex(IbkrWebAdapterError, "conflicts"):
            IbkrAbsenceEvidence(
                exact_client_order_lookup_complete=True,
                exact_client_order_absent=True,
                open_orders_complete=True,
                completed_orders_complete=True,
                executions_complete=True,
                account_activity_complete=True,
                consistency_horizon_satisfied=True,
                exclusion_semantics_qualified=False,
                order_found=True,
            )

    def test_cancel_acknowledgement_never_proves_terminal_cancel(self):
        outcome = parse_cancel_response(
            provider_order_id="123456789",
            payload={"msg": "Request was submitted"},
        )
        self.assertTrue(outcome.acknowledged)
        self.assertFalse(outcome.terminal_cancel_proven)
        self.assertEqual(outcome.provider_order_id, "123456789")

    def test_cancel_provider_error_is_not_terminal_cancel(self):
        outcome = parse_cancel_response(
            provider_order_id="123456789",
            payload={"error": "Order cannot be cancelled"},
        )
        self.assertFalse(outcome.acknowledged)
        self.assertFalse(outcome.terminal_cancel_proven)
        self.assertEqual(outcome.message, "Order cannot be cancelled")

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

    def test_unrecorded_reply_message_cannot_prepare_second_request(self):
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
        with self.assertRaisesRegex(TypeError, "recorded"):
            prepare_reply_confirmation(
                outcome,
                expected_attempt_id="attempt-1",
                expected_account_id="U1234567",
                expected_client_order_id="coid-1",
                explicit_authorization=True,
            )

    def test_reply_identity_cannot_escape_reply_endpoint_or_mutate_confirmation_body(self):
        with self.assertRaisesRegex(IbkrWebAdapterError, "path segment"):
            parse_order_submission_response(
                [{"id": "../orders", "message": ["Confirm"], "messageIds": ["o1"]}]
            )
        with self.assertRaisesRegex(IbkrWebAdapterError, "path segment"):
            parse_order_submission_response(
                [{"id": "reply?confirmed=false", "message": ["Confirm"], "messageIds": ["o1"]}]
            )
        for unsafe_id in (".", ".."):
            with self.subTest(unsafe_id=unsafe_id), self.assertRaisesRegex(
                IbkrWebAdapterError, "path segment"
            ):
                parse_order_submission_response(
                    [{"id": unsafe_id, "message": ["Confirm"], "messageIds": ["o1"]}]
                )
        for invalid_reply_id in (None, True, 123):
            with self.subTest(reply_id=invalid_reply_id), self.assertRaisesRegex(
                IbkrWebAdapterError,
                "reply id must be a string",
            ):
                parse_order_submission_response(
                    [{"id": invalid_reply_id, "message": ["Confirm"], "messageIds": ["o1"]}]
                )

        base = {
            "endpoint": "/iserver/reply/safe-reply-id",
            "body": {"confirmed": True},
            "attempt_id": "attempt-1",
            "account_id": "U1234567",
            "client_order_id": "at-reply-direct",
            "response_sha256": "sha256:" + "a" * 64,
        }
        request = IbkrReplyRequest(**base)
        self.assertEqual(dict(request.body), {"confirmed": True})
        with self.assertRaisesRegex(IbkrWebAdapterError, "reply endpoint|path segment"):
            IbkrReplyRequest(**{**base, "endpoint": "/iserver/reply/../orders"})
        with self.assertRaisesRegex(IbkrWebAdapterError, "path segment"):
            IbkrReplyRequest(**{**base, "endpoint": "/iserver/reply/.."})
        with self.assertRaisesRegex(IbkrWebAdapterError, "confirmed=true"):
            IbkrReplyRequest(**{**base, "body": {"confirmed": False}})

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

    def test_recorded_ack_is_bound_to_attempt_and_never_fill(self):
        intent = IbkrWebOrderIntent.create(
            instrument_version="AAPL-CONID-265598:v1",
            account_id="U1234567",
            contract=IbkrContractIdentity(conid=265598),
            side="BUY",
            order_type="MARKET",
            time_in_force="DAY",
            quantity="1",
        )
        normalized = prepare_normalized_order(
            intent,
            client_order_id="at-ibkr-submit-1",
            capability=capability(),
            session=ready_session(),
            at=NOW,
            maximum_session_age_seconds=30,
        )
        recorded = record_order_submission_result(
            normalized,
            attempt_id="attempt-ibkr-1",
            account_id="U1234567",
            environment="PAPER",
            observed_at=NOW,
            response_body=(
                '[{"order_id":"1234567890","order_status":"Submitted",'
                '"encrypt_message":"1"}]'
            ),
        )
        self.assertEqual(recorded.outcome, "ACKNOWLEDGED")
        self.assertEqual(recorded.next_action, "OBSERVE_OR_RECONCILE")
        self.assertEqual(recorded.client_order_id, "at-ibkr-submit-1")
        self.assertEqual(recorded.provider_order_id, "1234567890")
        self.assertTrue(recorded.response_sha256.startswith("sha256:"))
        self.assertFalse(recorded.proves_fill)
        self.assertFalse(recorded.retry_same_economic_action)

    def test_ambiguous_ibkr_transport_is_unknown_and_reconcile_first(self):
        intent = IbkrWebOrderIntent.create(
            instrument_version="AAPL-CONID-265598:v1",
            account_id="U1234567",
            contract=IbkrContractIdentity(conid=265598),
            side="BUY",
            order_type="MARKET",
            time_in_force="DAY",
            quantity="1",
        )
        normalized = prepare_normalized_order(
            intent,
            client_order_id="at-ibkr-unknown",
            capability=capability(),
            session=ready_session(),
            at=NOW,
            maximum_session_age_seconds=30,
        )
        recorded = record_order_submission_result(
            normalized,
            attempt_id="attempt-ibkr-unknown",
            account_id="U1234567",
            environment="PAPER",
            observed_at=NOW,
            response_body=None,
            transport_ambiguous=True,
        )
        self.assertEqual(recorded.outcome, "UNKNOWN")
        self.assertEqual(recorded.next_action, "RECONCILE_FIRST")
        self.assertIsNone(recorded.response_sha256)
        self.assertIsNone(recorded.provider_order_id)
        self.assertFalse(recorded.retry_same_economic_action)

    def test_recorded_reply_requires_explicit_second_guarded_action(self):
        intent = IbkrWebOrderIntent.create(
            instrument_version="AAPL-CONID-265598:v1",
            account_id="U1234567",
            contract=IbkrContractIdentity(conid=265598),
            side="BUY",
            order_type="LIMIT",
            time_in_force="DAY",
            quantity="1",
            limit_price="220.10",
        )
        normalized = prepare_normalized_order(
            intent,
            client_order_id="at-ibkr-reply",
            capability=capability(),
            session=ready_session(),
            at=NOW,
            maximum_session_age_seconds=30,
        )
        recorded = record_order_submission_result(
            normalized,
            attempt_id="attempt-ibkr-reply",
            account_id="U1234567",
            environment="PAPER",
            observed_at=NOW,
            response_body=(
                '[{"id":"07a13a5a-4a48-44a5-bb25-5ab37b79186c",'
                '"message":["Confirm this order"],"isSuppressed":false,'
                '"messageIds":["o163"]}]'
            ),
        )
        self.assertEqual(recorded.outcome, "REPLY_REQUIRED")
        self.assertEqual(recorded.next_action, "EXPLICIT_REPLY_REQUIRED")
        self.assertEqual(
            recorded.reply_id,
            "07a13a5a-4a48-44a5-bb25-5ab37b79186c",
        )
        self.assertFalse(recorded.retry_same_economic_action)

        with self.assertRaisesRegex(IbkrWebAdapterError, "explicit authorization"):
            prepare_reply_confirmation(
                recorded,
                expected_attempt_id="attempt-ibkr-reply",
                expected_account_id="U1234567",
                expected_client_order_id="at-ibkr-reply",
                explicit_authorization=False,
            )

        request = prepare_reply_confirmation(
            recorded,
            expected_attempt_id="attempt-ibkr-reply",
            expected_account_id="U1234567",
            expected_client_order_id="at-ibkr-reply",
            explicit_authorization=True,
        )
        self.assertEqual(
            request.endpoint,
            "/iserver/reply/07a13a5a-4a48-44a5-bb25-5ab37b79186c",
        )
        self.assertEqual(dict(request.body), {"confirmed": True})
        self.assertEqual(request.attempt_id, "attempt-ibkr-reply")
        self.assertEqual(request.account_id, "U1234567")
        self.assertEqual(request.client_order_id, "at-ibkr-reply")
        self.assertEqual(request.response_sha256, recorded.response_sha256)

        for field, value in (
            ("expected_attempt_id", "other-attempt"),
            ("expected_account_id", "OTHER"),
            ("expected_client_order_id", "other-coid"),
        ):
            kwargs = {
                "expected_attempt_id": "attempt-ibkr-reply",
                "expected_account_id": "U1234567",
                "expected_client_order_id": "at-ibkr-reply",
                "explicit_authorization": True,
            }
            kwargs[field] = value
            with self.subTest(field=field), self.assertRaises(IbkrWebAdapterError):
                prepare_reply_confirmation(recorded, **kwargs)

    def test_recorded_submission_rejects_cross_account_binding(self):
        intent = IbkrWebOrderIntent.create(
            instrument_version="AAPL-CONID-265598:v1",
            account_id="U1234567",
            contract=IbkrContractIdentity(conid=265598),
            side="BUY",
            order_type="MARKET",
            time_in_force="DAY",
            quantity="1",
        )
        normalized = prepare_normalized_order(
            intent,
            client_order_id="at-ibkr-account",
            capability=capability(),
            session=ready_session(),
            at=NOW,
            maximum_session_age_seconds=30,
        )
        with self.assertRaisesRegex(IbkrWebAdapterError, "account"):
            record_order_submission_result(
                normalized,
                attempt_id="attempt-cross-account",
                account_id="OTHER",
                environment="PAPER",
                observed_at=NOW,
                response_body=(
                    '[{"order_id":"123","order_status":"Submitted",'
                    '"encrypt_message":"1"}]'
                ),
            )

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
            expected_account_id="U1234567",
            instrument="AAPL-CONID-265598:v1",
            fee_amount="-0.35",
            fee_currency="USD",
            trade_time="2026-09-24T20:00:01Z",
        )
        self.assertEqual(fill.provider_id, "IBKR")
        self.assertEqual(fill.account_id, "U1234567")
        self.assertEqual(fill.environment, "PAPER")
        self.assertEqual(fill.provider_execution_id, "0001.123.01")
        self.assertEqual(fill.client_order_id, "at-ibkr-1")
        self.assertEqual(fill.quantity, Decimal("0.5"))
        self.assertEqual(fill.price, Decimal("220.10"))
        self.assertEqual(fill.fee_amount, Decimal("-0.35"))
        self.assertEqual(fill.fee_currency, "USD")



    def test_web_api_trades_use_execution_identity_coid_and_explicit_fee_currency(self):
        rows = [
            {
                "execution_id": "0001.123.01",
                "order_ref": "at-ibkr-1",
                "account": "U1234567",
                "conid": 265598,
                "size": Decimal("0.5"),
                "price": "220.10",
                "commission": "-0.35",
                "trade_time": "2026-09-24T20:00:01Z",
            }
        ]
        fills = parse_web_api_trades(
            [rows[0], dict(rows[0])],
            expected_account_id="U1234567",
            instrument_versions_by_conid={265598: "AAPL-CONID-265598:v1"},
            fee_currency_by_execution_id={"0001.123.01": "USD"},
        )
        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0].provider_id, "IBKR")
        self.assertEqual(fills[0].account_id, "U1234567")
        self.assertEqual(fills[0].environment, "PAPER")
        self.assertEqual(fills[0].provider_execution_id, "0001.123.01")
        self.assertEqual(fills[0].client_order_id, "at-ibkr-1")
        self.assertEqual(fills[0].quantity, Decimal("0.5"))
        self.assertEqual(fills[0].fee_amount, Decimal("-0.35"))

    def test_web_api_trade_rejects_cross_account_unknown_conid_and_missing_fee_currency(self):
        row = {
            "execution_id": "exec-1",
            "order_ref": "at-ibkr-1",
            "account": "U1234567",
            "conid": 265598,
            "size": "1",
            "price": "100",
            "commission": "0.25",
            "trade_time": "2026-09-24T20:00:01Z",
        }
        with self.assertRaisesRegex(IbkrWebAdapterError, "account"):
            parse_web_api_trades(
                [row],
                expected_account_id="OTHER",
                instrument_versions_by_conid={265598: "AAPL:v1"},
                fee_currency_by_execution_id={"exec-1": "USD"},
            )
        with self.assertRaisesRegex(IbkrWebAdapterError, "unmapped IBKR conid"):
            parse_web_api_trades(
                [row],
                expected_account_id="U1234567",
                instrument_versions_by_conid={},
                fee_currency_by_execution_id={"exec-1": "USD"},
            )
        with self.assertRaisesRegex(IbkrWebAdapterError, "fee currency"):
            parse_web_api_trades(
                [row],
                expected_account_id="U1234567",
                instrument_versions_by_conid={265598: "AAPL:v1"},
                fee_currency_by_execution_id={},
            )

    def test_web_api_trade_rejects_binary_float_economics_and_conflicting_execution_id(self):
        base = {
            "execution_id": "exec-1",
            "order_ref": "at-ibkr-1",
            "account": "U1234567",
            "conid": 265598,
            "size": "1",
            "price": "100",
            "commission": "0.25",
            "trade_time": "2026-09-24T20:00:01Z",
        }
        with self.assertRaisesRegex(IbkrWebAdapterError, "exact decimal"):
            parse_web_api_trades(
                [dict(base, size=1.0)],
                expected_account_id="U1234567",
                instrument_versions_by_conid={265598: "AAPL:v1"},
                fee_currency_by_execution_id={"exec-1": "USD"},
            )
        with self.assertRaisesRegex(IbkrWebAdapterError, "conflicting"):
            parse_web_api_trades(
                [base, dict(base, size="2")],
                expected_account_id="U1234567",
                instrument_versions_by_conid={265598: "AAPL:v1"},
                fee_currency_by_execution_id={"exec-1": "USD"},
            )

    def test_reconciliation_fill_rejects_noncanonical_environment(self):
        execution = IbkrExecutionEvidence.create(
            execution_id="0001.998.01",
            permanent_order_id=778898,
            account_id="U1234567",
            quantity="1",
            price="100",
        )
        with self.assertRaisesRegex(ValueError, "environment"):
            execution_to_reconciliation_fill(
                execution,
                environment="UNKNOWN_ENV",
                client_order_id="at-ibkr-env",
                expected_account_id="U1234567",
                instrument="AAPL-CONID-265598:v1",
                fee_amount="0",
                fee_currency="USD",
                trade_time="2026-09-24T20:00:01Z",
            )

    def test_execution_cannot_cross_account_boundary_during_reconciliation(self):
        execution = IbkrExecutionEvidence.create(
            execution_id="0001.999.01",
            permanent_order_id=778899,
            account_id="U1234567",
            quantity="1",
            price="100",
        )
        with self.assertRaisesRegex(IbkrWebAdapterError, "account"):
            execution_to_reconciliation_fill(
                execution,
                client_order_id="at-ibkr-account",
                expected_account_id="OTHER",
                instrument="AAPL-CONID-265598:v1",
                fee_amount="0",
                fee_currency="USD",
                trade_time="2026-09-24T20:00:01Z",
            )

    def test_regulated_instrument_requires_manual_indicator_evidence(self):
        with self.assertRaisesRegex(IbkrWebAdapterError, "manual_indicator is required"):
            IbkrWebOrderIntent.create(
                instrument_version="AAPL-CONID-265598:v1",
                account_id="U1234567",
                contract=IbkrContractIdentity(conid=265598),
                side="BUY",
                order_type="MARKET",
                time_in_force="DAY",
                quantity="1",
                regulatory_manual_indicator_required=True,
            )

    def test_automated_manual_indicator_false_is_preserved_when_required(self):
        intent = IbkrWebOrderIntent.create(
            instrument_version="AAPL-CONID-265598:v1",
            account_id="U1234567",
            contract=IbkrContractIdentity(conid=265598),
            side="BUY",
            order_type="MARKET",
            time_in_force="DAY",
            quantity="1",
            regulatory_manual_indicator_required=True,
            manual_indicator=False,
            ext_operator="autotrade",
        )
        prepared = prepare_normalized_order(
            intent,
            client_order_id="at-regulated-1",
            capability=capability(),
            session=ready_session(),
            at=NOW,
            maximum_session_age_seconds=30,
        )
        self.assertIs(prepared.fields["manualIndicator"], False)
        self.assertEqual(prepared.fields["extOperator"], "autotrade")



if __name__ == "__main__":
    unittest.main()
