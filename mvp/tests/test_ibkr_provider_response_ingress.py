import unittest

from mvp.autotrade_mvp.provider_core import ProviderResponseObservation
from mvp.autotrade_mvp.ibkr_web import (
    IbkrWebAdapterError,
    parse_cancel_response,
    parse_order_submission_response,
)


class _ExecutableDict(dict):
    get_called = False

    def get(self, key, default=None):
        type(self).get_called = True
        raise AssertionError("provider mapping callback executed")


class _StringificationTrap:
    str_called = False

    def __str__(self):
        type(self).str_called = True
        raise AssertionError("provider value stringification executed")


class _ExecutableStr(str):
    strip_called = False

    def strip(self, *args, **kwargs):
        type(self).strip_called = True
        raise AssertionError("provider string subclass callback executed")


class _EqualityTrap:
    eq_called = False

    def __eq__(self, other):
        type(self).eq_called = True
        raise AssertionError("provider value equality callback executed")


class _ExecutableList(list):
    iter_called = False

    def __iter__(self):
        type(self).iter_called = True
        raise AssertionError("provider sequence callback executed")


class _ExecutableBody(dict):
    iter_called = False

    def __iter__(self):
        type(self).iter_called = True
        raise AssertionError("reply body mapping callback executed")


class _ExecutableObservation(ProviderResponseObservation):
    scope_called = False

    def require_scope(self, **kwargs):
        type(self).scope_called = True
        raise AssertionError("forged observation scope callback executed")


class IbkrProviderResponseIngressTests(unittest.TestCase):
    def setUp(self):
        _ExecutableDict.get_called = False
        _StringificationTrap.str_called = False
        _ExecutableStr.strip_called = False
        _EqualityTrap.eq_called = False
        _ExecutableList.iter_called = False
        _ExecutableObservation.scope_called = False
        _ExecutableBody.iter_called = False

    def test_submission_parser_rejects_executable_mapping_before_callbacks(self):
        with self.assertRaisesRegex(TypeError, "exact object"):
            parse_order_submission_response(
                _ExecutableDict(
                    {
                        "order_id": "1234567890",
                        "order_status": "Submitted",
                    }
                )
            )
        self.assertFalse(_ExecutableDict.get_called)

    def test_acknowledgement_requires_documented_provider_string_fields(self):
        for field in ("order_id", "order_status"):
            payload = {
                "order_id": "1234567890",
                "order_status": "Submitted",
            }
            payload[field] = _StringificationTrap()
            with self.subTest(field=field), self.assertRaisesRegex(
                IbkrWebAdapterError, "provider text"
            ):
                parse_order_submission_response([payload])
            self.assertFalse(_StringificationTrap.str_called)

        with self.assertRaisesRegex(IbkrWebAdapterError, "provider text"):
            parse_order_submission_response(
                [{"order_id": 1234567890, "order_status": "Submitted"}]
            )

    def test_reply_message_and_message_ids_are_exact_provider_strings(self):
        for field, value in (
            ("message", [_StringificationTrap()]),
            ("messageIds", [_StringificationTrap()]),
        ):
            payload = {
                "id": "07a13a5a-4a48-44a5-bb25-5ab37b79186c",
                "message": ["Confirm this order"],
                "isSuppressed": False,
                "messageIds": ["o163"],
            }
            payload[field] = value
            with self.subTest(field=field), self.assertRaisesRegex(
                IbkrWebAdapterError, "provider text"
            ):
                parse_order_submission_response([payload])
            self.assertFalse(_StringificationTrap.str_called)

    def test_rejection_reason_is_not_coerced_from_caller_object(self):
        with self.assertRaisesRegex(IbkrWebAdapterError, "provider text"):
            parse_order_submission_response([{"error": _StringificationTrap()}])
        self.assertFalse(_StringificationTrap.str_called)

    def test_provider_string_subclasses_are_rejected_before_virtual_strip(self):
        with self.assertRaisesRegex(IbkrWebAdapterError, "provider text"):
            parse_order_submission_response(
                [
                    {
                        "order_id": _ExecutableStr("1234567890"),
                        "order_status": "Submitted",
                    }
                ]
            )
        self.assertFalse(_ExecutableStr.strip_called)

    def test_shape_classification_rejects_objects_before_equality_callbacks(self):
        with self.assertRaisesRegex(IbkrWebAdapterError, "provider text"):
            parse_order_submission_response(
                [{"order_id": _EqualityTrap(), "order_status": "Submitted"}]
            )
        self.assertFalse(_EqualityTrap.eq_called)

    def test_reply_sequences_reject_executable_subclasses_before_iteration(self):
        with self.assertRaisesRegex(IbkrWebAdapterError, "exact sequence"):
            parse_order_submission_response(
                [
                    {
                        "id": "07a13a5a-4a48-44a5-bb25-5ab37b79186c",
                        "message": _ExecutableList(["Confirm"]),
                        "messageIds": ["o163"],
                    }
                ]
            )
        self.assertFalse(_ExecutableList.iter_called)

    def test_cancel_ack_binds_exact_provider_ticket_and_inert_response(self):
        outcome = parse_cancel_response(
            provider_order_id="123456789",
            payload={
                "msg": "Request was submitted",
                "order_id": 123456789,
                "conid": 265598,
                "account": "U1234567",
            },
        )
        self.assertTrue(outcome.acknowledged)
        self.assertFalse(outcome.terminal_cancel_proven)
        self.assertEqual(outcome.provider_order_id, "123456789")
        self.assertEqual(outcome.message, "Request was submitted")

        for payload in (
            {"msg": "Request was submitted"},
            {"msg": "Request was submitted", "order_id": 987654321},
            {"msg": "Request was submitted", "order_id": "123456789"},
        ):
            with self.subTest(payload=payload), self.assertRaisesRegex(
                IbkrWebAdapterError, "order_id"
            ):
                parse_cancel_response(
                    provider_order_id="123456789",
                    payload=payload,
                )

    def test_cancel_response_rejects_executable_or_coercible_provider_values(self):
        with self.assertRaisesRegex(TypeError, "exact object"):
            parse_cancel_response(
                provider_order_id="123456789",
                payload=_ExecutableDict({"error": "rejected"}),
            )
        self.assertFalse(_ExecutableDict.get_called)

        with self.assertRaisesRegex(IbkrWebAdapterError, "provider text"):
            parse_cancel_response(
                provider_order_id="123456789",
                payload={"error": _StringificationTrap()},
            )
        self.assertFalse(_StringificationTrap.str_called)

    def test_cancel_requested_ticket_is_exact_and_non_executable(self):
        with self.assertRaisesRegex(
            IbkrWebAdapterError,
            "provider_order_id must be canonical exact text",
        ):
            parse_cancel_response(
                provider_order_id=" 123456789",
                payload={
                    "msg": "Request was submitted",
                    "order_id": 123456789,
                },
            )

        with self.assertRaisesRegex(
            IbkrWebAdapterError,
            "provider_order_id must be canonical exact text",
        ):
            parse_cancel_response(
                provider_order_id=_ExecutableStr("123456789"),
                payload={
                    "msg": "Request was submitted",
                    "order_id": 123456789,
                },
            )
        self.assertFalse(_ExecutableStr.strip_called)

    def test_trade_reconciliation_rejects_observation_subclass_before_callbacks(self):
        forged = object.__new__(_ExecutableObservation)
        from mvp.autotrade_mvp.ibkr_web import parse_web_api_trades

        with self.assertRaisesRegex(TypeError, "exact ProviderResponseObservation"):
            parse_web_api_trades(
                forged,
                instrument_versions_by_conid={265598: "AAPL:v1"},
                fee_currency_by_execution_id={"exec-1": "USD"},
            )
        self.assertFalse(_ExecutableObservation.scope_called)

    def test_reply_request_rejects_mapping_subclass_before_copy_callbacks(self):
        from mvp.autotrade_mvp.ibkr_web import IbkrReplyRequest

        with self.assertRaisesRegex(IbkrWebAdapterError, "confirmed=true"):
            IbkrReplyRequest(
                endpoint="/iserver/reply/safe-reply-id",
                body=_ExecutableBody({"confirmed": True}),
                attempt_id="attempt-1",
                account_id="U1234567",
                client_order_id="coid-1",
                response_sha256="sha256:" + "a" * 64,
            )
        self.assertFalse(_ExecutableBody.iter_called)

    def test_documented_ack_reply_and_reject_shapes_still_parse(self):
        ack = parse_order_submission_response(
            [{"order_id": "1234567890", "order_status": "Submitted"}]
        )
        self.assertEqual(ack.status, "ACKNOWLEDGED")
        self.assertEqual(ack.provider_order_id, "1234567890")
        self.assertEqual(ack.provider_order_status, "Submitted")

        reply = parse_order_submission_response(
            [
                {
                    "id": "07a13a5a-4a48-44a5-bb25-5ab37b79186c",
                    "message": ["Confirm this order"],
                    "isSuppressed": False,
                    "messageIds": ["o163"],
                }
            ]
        )
        self.assertEqual(reply.status, "REPLY_REQUIRED")
        self.assertEqual(reply.messages, ("Confirm this order",))
        self.assertEqual(reply.message_ids, ("o163",))

        rejected = parse_order_submission_response(
            [{"error": "order rejected"}]
        )
        self.assertEqual(rejected.status, "REJECTED")
        self.assertEqual(rejected.rejection_reason, "order rejected")


if __name__ == "__main__":
    unittest.main()
