from __future__ import annotations

import base64
import json
import unittest

import mvp.autotrade_mvp.trusted_chronology_cut as chronology


class TrustedChronologyTransitiveCallableAuthorityTests(unittest.TestCase):
    def test_measurement_parser_rejects_same_object_json_dependency_code_mutation(self) -> None:
        helper = json.detect_encoding
        original_code = helper.__code__

        def forged(_data):
            raise AssertionError("forged json.detect_encoding executed")

        self.assertEqual(forged.__code__.co_freevars, ())
        try:
            helper.__code__ = forged.__code__
            with self.assertRaisesRegex(
                RuntimeError,
                r"trusted chronology measurement parser module function executable changed",
            ):
                chronology.parse_challenge_bound_measurement(b"", challenge=None)
        finally:
            helper.__code__ = original_code

    def test_signed_receipt_parser_rejects_same_object_base64_dependency_code_mutation(self) -> None:
        helper = base64._bytes_from_decode_data
        original_code = helper.__code__

        def forged(_value):
            raise AssertionError("forged base64 helper executed")

        self.assertEqual(forged.__code__.co_freevars, ())
        try:
            helper.__code__ = forged.__code__
            with self.assertRaisesRegex(
                RuntimeError,
                r"trusted chronology signed receipt parser module function executable changed",
            ):
                chronology.parse_signed_qualification_attestation({})
        finally:
            helper.__code__ = original_code

    def test_measurement_parser_rejects_json_decoder_method_code_mutation(self) -> None:
        method = json.JSONDecoder.decode
        original_code = method.__code__

        def forged(self, _value, _w=None):
            raise AssertionError("forged JSONDecoder.decode executed")

        self.assertEqual(forged.__code__.co_freevars, ())
        try:
            method.__code__ = forged.__code__
            with self.assertRaisesRegex(
                RuntimeError,
                r"trusted chronology measurement parser external class executable changed: json\.decoder\.JSONDecoder\.decode",
            ):
                chronology.parse_challenge_bound_measurement(b"", challenge=None)
        finally:
            method.__code__ = original_code


if __name__ == "__main__":
    unittest.main()
