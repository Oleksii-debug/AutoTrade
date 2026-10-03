from __future__ import annotations

import unittest

from mvp.autotrade_mvp.bybit_credential_revocation import (
    BybitCredentialRevocationError,
    PRODUCT_DERIVATIVES,
    PRODUCT_SPOT,
    STATE_ACCEPTED,
    STATE_INCONCLUSIVE,
    STATE_REVOKED,
    classify_bybit_credential_revocation_response,
    require_bybit_credential_revoked,
)


def _body(code: int) -> bytes:
    return (
        '{"retCode":' + str(code) + ',"retMsg":"provider-result","result":{}}'
    ).encode("utf-8")


class BybitCredentialRevocationSemanticsTests(unittest.TestCase):
    def test_invalid_key_is_revoked_only_for_exact_attested_domain(self):
        for product in (PRODUCT_SPOT, PRODUCT_DERIVATIVES):
            result = classify_bybit_credential_revocation_response(
                http_status=200,
                response_bytes=_body(10003),
                product_family=product,
            )
            self.assertEqual(result.state, STATE_REVOKED)
            self.assertEqual(result.ret_code, 10003)
            self.assertEqual(
                result.reason_code,
                "BYBIT_API_KEY_INVALID_FOR_EXACT_DOMAIN",
            )

    def test_expiry_codes_are_product_specific(self):
        spot = classify_bybit_credential_revocation_response(
            http_status=200,
            response_bytes=_body(-2015),
            product_family=PRODUCT_SPOT,
        )
        self.assertEqual(spot.state, STATE_REVOKED)
        self.assertEqual(spot.reason_code, "BYBIT_SPOT_API_KEY_EXPIRED")

        wrong_spot = classify_bybit_credential_revocation_response(
            http_status=200,
            response_bytes=_body(-2015),
            product_family=PRODUCT_DERIVATIVES,
        )
        self.assertEqual(wrong_spot.state, STATE_INCONCLUSIVE)

        derivatives = classify_bybit_credential_revocation_response(
            http_status=200,
            response_bytes=_body(33004),
            product_family=PRODUCT_DERIVATIVES,
        )
        self.assertEqual(derivatives.state, STATE_REVOKED)
        self.assertEqual(
            derivatives.reason_code,
            "BYBIT_DERIVATIVES_API_KEY_EXPIRED",
        )

        wrong_derivatives = classify_bybit_credential_revocation_response(
            http_status=200,
            response_bytes=_body(33004),
            product_family=PRODUCT_SPOT,
        )
        self.assertEqual(wrong_derivatives.state, STATE_INCONCLUSIVE)

    def test_success_is_hard_old_credential_acceptance(self):
        result = classify_bybit_credential_revocation_response(
            http_status=200,
            response_bytes=_body(0),
            product_family=PRODUCT_DERIVATIVES,
        )
        self.assertEqual(result.state, STATE_ACCEPTED)
        with self.assertRaisesRegex(
            BybitCredentialRevocationError,
            "still accepted",
        ):
            require_bybit_credential_revoked(
                http_status=200,
                response_bytes=_body(0),
                product_family=PRODUCT_DERIVATIVES,
            )

    def test_ambiguous_auth_codes_never_become_revocation(self):
        for code in (10004, 10005, 10007, 10010, 10016):
            with self.subTest(code=code):
                result = classify_bybit_credential_revocation_response(
                    http_status=200,
                    response_bytes=_body(code),
                    product_family=PRODUCT_DERIVATIVES,
                )
                self.assertEqual(result.state, STATE_INCONCLUSIVE)
                with self.assertRaisesRegex(
                    BybitCredentialRevocationError,
                    "inconclusive",
                ):
                    require_bybit_credential_revoked(
                        http_status=200,
                        response_bytes=_body(code),
                        product_family=PRODUCT_DERIVATIVES,
                    )

    def test_http_only_auth_failure_is_inconclusive(self):
        for status in (401, 403, 429, 500):
            with self.subTest(status=status):
                result = classify_bybit_credential_revocation_response(
                    http_status=status,
                    response_bytes=_body(10003),
                    product_family=PRODUCT_SPOT,
                )
                self.assertEqual(result.state, STATE_INCONCLUSIVE)
                self.assertIsNone(result.ret_code)

    def test_malformed_or_ambiguous_json_fails_closed(self):
        bad = (
            b'{"retCode":10003,"retCode":0}',
            b'{"retCode":"10003"}',
            b'{"retMsg":"missing"}',
            b'[]',
            b'{"retCode":NaN}',
            b'\xff',
        )
        for payload in bad:
            with self.subTest(payload=payload):
                with self.assertRaises(BybitCredentialRevocationError):
                    classify_bybit_credential_revocation_response(
                        http_status=200,
                        response_bytes=payload,
                        product_family=PRODUCT_SPOT,
                    )

    def test_bool_status_product_subclass_and_nonbytes_fail(self):
        class Product(str):
            pass

        with self.assertRaises(BybitCredentialRevocationError):
            classify_bybit_credential_revocation_response(
                http_status=True,
                response_bytes=_body(10003),
                product_family=PRODUCT_SPOT,
            )
        with self.assertRaises(BybitCredentialRevocationError):
            classify_bybit_credential_revocation_response(
                http_status=200,
                response_bytes=_body(10003),
                product_family=Product(PRODUCT_SPOT),
            )
        with self.assertRaises(TypeError):
            classify_bybit_credential_revocation_response(
                http_status=200,
                response_bytes=bytearray(_body(10003)),
                product_family=PRODUCT_SPOT,
            )

    def test_exact_revocation_requirement_accepts_only_narrow_codes(self):
        result = require_bybit_credential_revoked(
            http_status=200,
            response_bytes=_body(10003),
            product_family=PRODUCT_DERIVATIVES,
        )
        self.assertEqual(result.state, STATE_REVOKED)


if __name__ == "__main__":
    unittest.main()
