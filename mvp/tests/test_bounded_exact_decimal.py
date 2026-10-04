"""Pre-construction regression for the shared provider/domain numeric ingress."""

from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, localcontext
import json
from random import Random
import unittest
from unittest.mock import patch

import autotrade_numeric.exact_decimal as exact_decimal
import mvp.autotrade_mvp.exact_decimal as mvp_exact_decimal
from mvp.autotrade_mvp.exact_decimal import (
    ExactDecimalError,
    MAX_DECIMAL_TEXT_LENGTH,
    MAX_INTEGER_DIGITS,
    MAX_SCALE,
    MAX_SIGNIFICANT_DIGITS,
    parse_bounded_exact_decimal,
    parse_bounded_json_integer_token,
    parse_bounded_json_number_token,
    parse_canonical_decimal_text,
)


class HostileDecimal(Decimal):
    def is_finite(self):
        raise AssertionError("Decimal subclass method was dispatched")

    def as_tuple(self):
        raise AssertionError("Decimal subclass tuple method was dispatched")


class HostileText(str):
    def __len__(self):
        raise AssertionError("string subclass method was dispatched")

    def startswith(self, *args):
        raise AssertionError("string subclass prefix method was dispatched")


class HostileInt(int):
    def bit_length(self):
        raise AssertionError("integer subclass method was dispatched")


class BoundedExactDecimalTests(unittest.TestCase):
    def test_facade_and_neutral_ingress_share_exact_callable_identity(self):
        for name in (
            "parse_bounded_exact_decimal",
            "parse_bounded_json_number_token",
            "parse_bounded_json_integer_token",
        ):
            self.assertIs(getattr(mvp_exact_decimal, name), getattr(exact_decimal, name))

    def test_noncanonical_presentation_remains_exact(self):
        for value in (
            "0.0100", "3456.7000", "65000.10", "+001.2500",
            ".1250", "1.", "1e255", "1e-256",
            "-0.000001e+2", "9" * 256, "1e+2",
        ):
            with self.subTest(value_head=value[:12], size=len(value)):
                accepted = parse_bounded_exact_decimal(value)
                self.assertEqual(accepted.as_tuple(), Decimal(value).as_tuple())
        self.assertEqual(
            parse_bounded_exact_decimal("0e-99999999999999999999999999"),
            Decimal("0"),
        )
        self.assertEqual(
            parse_bounded_exact_decimal("-0000.000E+9999999999").as_tuple(),
            Decimal("0").as_tuple(),
        )
        self.assertEqual(
            format(parse_bounded_exact_decimal("0e-99999999999999"), "f"),
            "0",
        )

    def test_boundary_growth_is_algebraically_rejected(self):
        accepted = (
            "0." + "0" * (MAX_SCALE - 1) + "1",
            "-0." + "0" * (MAX_SCALE - 1) + "1",
            "9" * MAX_SIGNIFICANT_DIGITS,
            "1e" + str(MAX_INTEGER_DIGITS - 1),
        )
        for text in accepted:
            with self.subTest(case="accepted", prefix=text[:12]):
                parse_bounded_exact_decimal(text)
        rejected = (
            "0." + "0" * MAX_SCALE + "1",
            "9" * (MAX_SIGNIFICANT_DIGITS + 1),
            "1e" + str(MAX_INTEGER_DIGITS),
            "1e-" + str(MAX_SCALE + 1),
            "-0." + "0" * MAX_SCALE + "1",
            "9" * (MAX_DECIMAL_TEXT_LENGTH + 1),
            "1" * MAX_SIGNIFICANT_DIGITS + "e9",
        )
        for text in rejected:
            with self.subTest(case="rejected", prefix=text[:12], length=len(text)):
                with self.assertRaises(ExactDecimalError):
                    parse_bounded_exact_decimal(text)

    def test_invalid_rejected_before_decimal_constructor(self):
        invalid = (
            "1e256", "1e-257", "9" * 257,
            "0." + "0" * MAX_SCALE + "1",
            "1e999999999999999999", "1e-999999999999999999",
            "NaN", "Infinity", "1_000", " 1", "1 ", "1\n",
            "1\r\n", "1.2garbage", ".", "+", "1e",
            "1e+","1e-", "1.2.3",
        )
        with patch.object(
            exact_decimal,
            "Decimal",
            side_effect=AssertionError("constructed Decimal before preflight"),
        ):
            for text in invalid:
                with self.subTest(text_head=text[:16]):
                    with self.assertRaises(ExactDecimalError):
                        parse_bounded_exact_decimal(text)

    def test_exact_builtin_type_fence_precedes_virtual_dispatch(self):
        for value in (
            True, 1.5, None, object(), HostileText("1.5"),
            HostileInt(2), HostileDecimal("1.5"),
        ):
            with self.subTest(classname=type(value).__name__):
                with self.assertRaises(ExactDecimalError):
                    parse_bounded_exact_decimal(value)
        with self.assertRaises(TypeError):
            parse_bounded_exact_decimal("1", allow_exponent=1)
        with self.assertRaises(ExactDecimalError):
            parse_bounded_exact_decimal("1e2", allow_exponent=False)
        self.assertEqual(
            parse_bounded_exact_decimal("1.25", allow_exponent=False),
            Decimal("1.25"),
        )
        with self.assertRaises(ExactDecimalError):
            parse_bounded_exact_decimal("0e-9999", allow_exponent=False)

    def test_large_integer_preflight_does_not_construct_decimal(self):
        huge = 10**5000
        with patch.object(
            exact_decimal,
            "Decimal",
            side_effect=AssertionError("oversized int constructed Decimal"),
        ):
            with self.assertRaises(ExactDecimalError):
                parse_bounded_exact_decimal(huge)
        boundary = 10 ** (MAX_INTEGER_DIGITS - 1)
        self.assertEqual(
            parse_bounded_exact_decimal(boundary).as_tuple(),
            Decimal(boundary).as_tuple(),
        )
        self.assertEqual(parse_bounded_exact_decimal(0), Decimal("0"))
        self.assertEqual(parse_bounded_exact_decimal(Decimal("-0E-999999")), Decimal(0))

    def test_json_callbacks_enforce_strict_grammar_and_types(self):
        payload = json.loads(
            '{"price":65000.10,"fee":0.0100,"sequence":12345,"negative":-12}',
            parse_float=parse_bounded_json_number_token,
            parse_int=parse_bounded_json_integer_token,
        )
        self.assertEqual(payload["price"].as_tuple(), Decimal("65000.10").as_tuple())
        self.assertEqual(payload["fee"].as_tuple(), Decimal("0.0100").as_tuple())
        self.assertIs(type(payload["sequence"]), int)
        self.assertEqual(payload["sequence"], 12345)
        self.assertEqual(payload["negative"], -12)
        self.assertEqual(parse_bounded_json_integer_token("-0"), 0)
        for malformed in (
            "+1", "01", "-01", "1.", ".1", "1\n",
            "NaN", "1_0", "1e+",
        ):
            with self.subTest(malformed=malformed):
                with self.assertRaises(ExactDecimalError):
                    parse_bounded_json_number_token(malformed)
        for malformed in (
            "+1", "01", "-01", "1.0", "1e0", "1\n",
            "9" * 257,
        ):
            with self.subTest(integer_malformed=malformed[:16]):
                with self.assertRaises(ExactDecimalError):
                    parse_bounded_json_integer_token(malformed)
        with patch.object(
            exact_decimal,
            "Decimal",
            side_effect=AssertionError("oversized JSON token constructed Decimal"),
        ):
            with self.assertRaises(ExactDecimalError):
                parse_bounded_json_number_token("1e256")
        with self.assertRaises(ExactDecimalError):
            parse_bounded_json_integer_token("9" * (MAX_INTEGER_DIGITS + 1))

    def test_hostile_ambient_decimal_context_does_not_round(self):
        values = ("3456.7000", "1e-256", "1e255", "0.0100")
        expected = tuple(Decimal(item).as_tuple() for item in values)
        for precision in (2, 6, 28, 80):
            for rounding in (ROUND_FLOOR, ROUND_CEILING):
                with self.subTest(precision=precision, rounding=rounding):
                    with localcontext() as context:
                        context.prec = precision
                        context.rounding = rounding
                        self.assertEqual(
                            tuple(
                                parse_bounded_exact_decimal(item).as_tuple()
                                for item in values
                            ),
                            expected,
                        )
                        with self.assertRaises(ExactDecimalError):
                            parse_bounded_exact_decimal("1e256")

    def test_seeded_preflight_matches_independent_decimal_tuple_geometry(self):
        # A bounded independent constructor oracle is safe here because all
        # generated tokens are short and explicit exponents stay within 400.
        rng = Random(20260930)
        for index in range(12000):
            left_count = rng.randrange(0, 263)
            right_count = rng.randrange(0, 263)
            if left_count == 0 and right_count == 0:
                left_count = 1
            left = "".join(str(rng.randrange(10)) for _ in range(left_count))
            right = "".join(str(rng.randrange(10)) for _ in range(right_count))
            mantissa = left
            if right_count or rng.randrange(2):
                mantissa += "." + right
            if not left:
                mantissa = "." + right
            sign = rng.choice(("", "-", "+"))
            exp = rng.choice((None, None, None, rng.randrange(-400, 401)))
            token = sign + mantissa
            if exp is not None:
                token += rng.choice(("e", "E")) + str(exp)

            expected_ok = False
            if len(token) <= MAX_DECIMAL_TEXT_LENGTH:
                value = Decimal(token)
                sign_bit, digits, exponent = value.as_tuple()
                if not any(digits):
                    expected_ok = True
                else:
                    expected_ok = (
                        len(digits) <= MAX_SIGNIFICANT_DIGITS
                        and exponent >= -MAX_SCALE
                        and max(len(digits) + exponent, 0) <= MAX_INTEGER_DIGITS
                    )
            try:
                result = parse_bounded_exact_decimal(token)
            except ExactDecimalError:
                self.assertFalse(expected_ok, msg=f"false rejection expected? case {index}")
            else:
                self.assertTrue(expected_ok, msg=f"unexpected admission case {index}")
                if not any(value.as_tuple().digits):
                    self.assertEqual(result.as_tuple(), Decimal(0).as_tuple())
                else:
                    self.assertEqual(
                        result.as_tuple(), value.as_tuple(),
                        msg=f"tuple mismatch case {index}",
                    )

    def test_strict_canonical_wire_authority_remains_distinct(self):
        for value in ("0.0100", "001.25", "+1", "1e2"):
            self.assertIsInstance(parse_bounded_exact_decimal(value), Decimal)
            with self.assertRaises(ExactDecimalError):
                parse_canonical_decimal_text(value)
        with self.assertRaises(ExactDecimalError):
            parse_bounded_json_number_token("1e256")


if __name__ == "__main__":
    unittest.main()
