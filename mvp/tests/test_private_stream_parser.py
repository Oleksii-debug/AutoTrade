import json
import unittest

from mvp.autotrade_mvp.private_stream_parser import (
    PrivateStreamParserError,
    parse_installed_private_stream_frame,
)
from mvp.autotrade_mvp.provider_qualification_authority import (
    PrivateStreamSemantics,
    ProviderQualificationAuthorityError,
)


Q = {
    "topic_id": "execution",
    "parser_id": "bybit-v5-private-execution",
    "parser_version": "1.0.0",
    "sequence_policy": "MONOTONIC_NONCONTIGUOUS",
    "sequence_scope": "symbol",
    "recovery_method_id": "snapshot-readback-v1",
}


def semantics(**overrides):
    selection = dict(Q)
    selection.update(overrides)
    return PrivateStreamSemantics(**selection)


def parse(frame: bytes, **overrides):
    return parse_installed_private_stream_frame(
        semantics=semantics(**overrides),
        frame_bytes=frame,
    )


def frame(
    *events,
    message_id="386825804_BTCUSDT_140612148849382",
    creation_time=1746270400355,
):
    return json.dumps(
        {
            "topic": "execution",
            "id": message_id,
            "creationTime": creation_time,
            "data": list(events),
        },
        separators=(",", ":"),
    ).encode("utf-8")


def event(
    *,
    exec_id,
    symbol="BTCUSDT",
    seq=140612148849382,
    category="linear",
    exec_time="1746270400353",
):
    return {
        "category": category,
        "symbol": symbol,
        "execId": exec_id,
        "orderId": "order-1",
        "execPrice": "95900.1",
        "execQty": "0.5",
        "execTime": exec_time,
        "seq": seq,
    }


class InstalledPrivateStreamParserTests(unittest.TestCase):
    def test_bybit_execution_derives_event_and_sequence_only_from_frame_bytes(self):
        raw = frame(event(exec_id="exec-1"))
        parsed = parse(raw)
        self.assertEqual(parsed.parser.parser_id, "bybit-v5-private-execution")
        self.assertEqual(parsed.provider_message_id, "386825804_BTCUSDT_140612148849382")
        self.assertEqual(parsed.provider_creation_time_ms, 1746270400355)
        self.assertEqual(len(parsed.events), 1)
        item = parsed.events[0]
        self.assertEqual(item.provider_event_id, "exec-1")
        self.assertEqual(item.provider_sequence, "140612148849382")
        self.assertEqual(item.sequence_scope_value, "BTCUSDT")
        self.assertEqual(item.provider_event_time_ms, "1746270400353")
        self.assertEqual(item.data_index, 0)
        self.assertTrue(parsed.frame_sha256.startswith("sha256:"))
        self.assertFalse(hasattr(item, "receive_ordinal"))

    def test_same_provider_sequence_on_different_symbols_remains_separately_scoped(self):
        parsed = parse(
            frame(
                event(exec_id="exec-a", symbol="BTCUSDT", seq=101),
                event(exec_id="exec-b", symbol="ETHUSDT", seq=101),
            )
        )
        self.assertEqual(
            [(e.provider_sequence, e.sequence_scope_value) for e in parsed.events],
            [("101", "BTCUSDT"), ("101", "ETHUSDT")],
        )

    def test_multiple_executions_with_same_scope_and_sequence_keep_exec_identity(self):
        parsed = parse(
            frame(
                event(exec_id="exec-a", symbol="BTCUSDT", seq=102),
                event(exec_id="exec-b", symbol="BTCUSDT", seq=102),
            )
        )
        self.assertEqual(
            [e.provider_event_id for e in parsed.events],
            ["exec-a", "exec-b"],
        )

    def test_unknown_or_semantically_retargeted_parser_selection_fails_closed(self):
        raw = frame(event(exec_id="exec-1"))
        with self.assertRaisesRegex(
            PrivateStreamParserError,
            "not source-installed",
        ):
            parse(raw, parser_version="2.0.0")
        with self.assertRaisesRegex(
            PrivateStreamParserError,
            "does not match installed Q semantics",
        ):
            parse(raw, sequence_policy="ARITHMETIC_SEQUENCE")
        with self.assertRaisesRegex(
            ProviderQualificationAuthorityError,
            "sequence_scope",
        ):
            parse(raw, sequence_scope=None)

    def test_topic_mismatch_and_caller_like_sequence_shape_fail_closed(self):
        raw = json.dumps(
            {
                "topic": "order",
                "id": "msg-1",
                "data": [event(exec_id="exec-1")],
            },
            separators=(",", ":"),
        ).encode()
        with self.assertRaisesRegex(
            PrivateStreamParserError,
            "requires exact execution topic",
        ):
            parse(raw)

        raw_string_seq = frame(event(exec_id="exec-1", seq="101"))
        with self.assertRaisesRegex(
            PrivateStreamParserError,
            "exact non-negative integer",
        ):
            parse(raw_string_seq)

    def test_duplicate_keys_and_duplicate_exec_id_fail_closed(self):
        raw = (
            b'{"topic":"execution","topic":"execution","id":"msg-1",'
            b'"data":[{"category":"linear","symbol":"BTCUSDT",'
            b'"execId":"exec-1","seq":1}]}'
        )
        with self.assertRaisesRegex(
            PrivateStreamParserError,
            "duplicate JSON keys",
        ):
            parse(raw)
        with self.assertRaisesRegex(
            PrivateStreamParserError,
            "repeats execId",
        ):
            parse(
                frame(
                    event(exec_id="exec-1", seq=1),
                    event(exec_id="exec-1", seq=2),
                )
            )

    def test_nonfinite_and_boolean_sequence_fail_closed(self):
        raw = (
            b'{"topic":"execution","id":"msg-1","creationTime":NaN,'
            b'"data":[{"category":"linear","symbol":"BTCUSDT",'
            b'"execId":"exec-1","seq":1}]}'
        )
        with self.assertRaisesRegex(
            PrivateStreamParserError,
            "non-finite",
        ):
            parse(raw)
        with self.assertRaisesRegex(
            PrivateStreamParserError,
            "exact non-negative integer",
        ):
            parse(frame(event(exec_id="exec-1", seq=True)))


    def test_provider_timestamps_are_mandatory_exact_frame_fields(self):
        missing_creation = json.dumps(
            {
                "topic": "execution",
                "id": "msg-1",
                "data": [event(exec_id="exec-1")],
            },
            separators=(",", ":"),
        ).encode()
        with self.assertRaisesRegex(
            PrivateStreamParserError,
            "creationTime",
        ):
            parse(missing_creation)
        with self.assertRaisesRegex(
            PrivateStreamParserError,
            "creationTime",
        ):
            parse(frame(event(exec_id="exec-1"), creation_time=1.5))
        with self.assertRaisesRegex(
            PrivateStreamParserError,
            "execTime",
        ):
            parse(frame(event(exec_id="exec-1", exec_time="001")))

    def test_category_and_long_sequence_are_bounded_by_installed_contract(self):
        with self.assertRaisesRegex(
            PrivateStreamParserError,
            "outside the installed product set",
        ):
            parse(frame(event(exec_id="exec-1", category="event")))
        with self.assertRaisesRegex(
            PrivateStreamParserError,
            "installed bound",
        ):
            parse(frame(event(exec_id="exec-1", seq=(1 << 63))))


if __name__ == "__main__":
    unittest.main()
