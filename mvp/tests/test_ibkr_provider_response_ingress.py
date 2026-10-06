import unittest

from mvp.autotrade_mvp.ibkr_web import (
    IbkrWebAdapterError,
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


class IbkrProviderResponseIngressTests(unittest.TestCase):
    def setUp(self):
        _ExecutableDict.get_called = False
        _StringificationTrap.str_called = False
        _ExecutableStr.strip_called = False

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
