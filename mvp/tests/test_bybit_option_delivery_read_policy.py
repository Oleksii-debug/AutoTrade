from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlsplit
import unittest

from mvp.autotrade_mvp.capabilities import (
    CapabilityClaim,
    EvidenceVerification,
    derive_capability_snapshot,
)
from mvp.autotrade_mvp.provider_core import (
    AuthenticatedReadQueryBinding,
    Surface,
    prepare_authenticated_read_query,
)
from mvp.autotrade_mvp.provider_transport import (
    BYBIT_V5_ENDPOINT_POLICIES,
    BybitV5AuthenticatedReadSigner,
    ProviderTransportScopeError,
)

from mvp.tests.capability_test_support import fresh_test_admission

NOW = datetime(2026, 10, 6, 0, 0, tzinfo=timezone.utc)
ENDPOINT = "/v5/asset/delivery-record"
_INSTRUMENT_VERSION = "95555555-5555-4555-8555-555555555555@1"
_ARTIFACT_IDS = {
    "DOCUMENTED": "81111111-1111-4111-8111-111111111111",
    "API": "82222222-2222-4222-8222-222222222222",
    "ACCOUNT": "83333333-3333-4333-8333-333333333333",
    "INSTRUMENT": "84444444-4444-4444-8444-444444444444",
}

class _StringSubclass(str):
    pass



def option_delivery_capability():
    observed = NOW - timedelta(minutes=2)
    expires = NOW + timedelta(minutes=10)
    claims = tuple(
        CapabilityClaim(
            source=source,
            provider_id="BYBIT",
            account_id="acct-option",
            entity_id="option-lifecycle-btc",
            environment="PAPER",
            provider_environment="TESTNET",
            instrument_version=_INSTRUMENT_VERSION,
            observed_at=observed,
            expires_at=expires,
            supported_order_types=frozenset({"LIMIT"}),
            time_in_force=frozenset({"GTC"}),
            permission_scopes=frozenset({"ACCOUNT.READ"}),
            position_mode="NET",
            native_protection=frozenset(),
            rate_limit_policy_id="bybit-option-delivery-test-v1",
            data_entitlements=frozenset({"ACTIVITIES"}),
            evidence_ref={
                "artifact_id": _ARTIFACT_IDS[source],
                "sha256": "sha256:" + "a" * 64,
                "observed_at": observed.isoformat().replace("+00:00", "Z"),
            },
        )
        for source in ("DOCUMENTED", "API", "ACCOUNT", "INSTRUMENT")
    )
    return fresh_test_admission(derive_capability_snapshot(
        snapshot_id="88888888-8888-4888-8888-888888888888",
        claims=claims,
        observed_at=NOW - timedelta(minutes=1),
        evidence_verifier=lambda _claim: EvidenceVerification(valid=True),
    ))


def delivery_binding(
    query,
    *,
    surface=Surface.ACTIVITIES,
    permission_scope="ACCOUNT.READ",
):
    return prepare_authenticated_read_query(
        capability=option_delivery_capability(),
        surface=surface,
        endpoint=ENDPOINT,
        query=query,
        at=NOW,
        permission_scope=permission_scope,
    )


def sign(query, *, surface=Surface.ACTIVITIES, permission_scope="ACCOUNT.READ"):
    return BybitV5AuthenticatedReadSigner.sign(
        policy=BYBIT_V5_ENDPOINT_POLICIES["TESTNET"],
        query_binding=delivery_binding(
            query,
            surface=surface,
            permission_scope=permission_scope,
        ),
        credential_plaintext='{"api_key":"test-key","api_secret":"test-secret"}',
        timestamp_ms=1791244800000,
    )


class BybitOptionDeliveryReadPolicyTests(unittest.TestCase):
    def test_unissued_exact_binding_clone_is_rejected_before_signing(self):
        prepared = delivery_binding({"category": "option"})
        forged = object.__new__(AuthenticatedReadQueryBinding)
        for field in (
            "provider_id",
            "account_id",
            "entity_id",
            "environment",
            "capability_snapshot_id",
            "instrument_version",
            "surface",
            "endpoint",
            "query",
            "prepared_at",
            "permission_scope",
            "query_digest",
        ):
            object.__setattr__(forged, field, getattr(prepared, field))

        with self.assertRaisesRegex(
            ProviderTransportScopeError,
            "construction authority is unavailable",
        ):
            BybitV5AuthenticatedReadSigner.sign(
                policy=BYBIT_V5_ENDPOINT_POLICIES["TESTNET"],
                query_binding=forged,
                credential_plaintext='{"api_key":"test-key","api_secret":"test-secret"}',
                timestamp_ms=1791244800000,
            )

    def test_minimal_option_delivery_query_is_admitted_on_activity_surface(self):
        signed = sign({"category": "option"})
        parsed = urlsplit(signed.url)
        self.assertEqual(parsed.path, ENDPOINT)
        self.assertEqual(parse_qsl(parsed.query), [("category", "option")])
        self.assertEqual(signed.method, "GET")
        self.assertEqual(signed.body, b"")

    def test_documented_option_delivery_query_bounds_are_canonical(self):
        start = 1700000000000
        end = start + 30 * 24 * 60 * 60 * 1000
        query = {
            "category": "option",
            "symbol": "BTC-29DEC22-16000-P",
            "startTime": str(start),
            "endTime": str(end),
            "expDate": "29DEC22",
            "limit": "50",
            "cursor": "132791%3A0%2C132791%3A0",
        }
        signed = sign(query)
        decoded_query = dict(parse_qsl(urlsplit(signed.url).query))
        expected_decoded = dict(query)
        expected_decoded["cursor"] = "132791:0,132791:0"
        self.assertEqual(decoded_query, expected_decoded)
        self.assertIn(
            "cursor=132791%3A0%2C132791%3A0",
            urlsplit(signed.url).query,
        )
        self.assertNotIn("%253A", signed.url)
        self.assertNotIn("%252C", signed.url)

    def test_category_is_required_and_option_only(self):
        cases = (
            ({"symbol": "BTC-29DEC22-16000-P"}, "requires category=option"),
            ({"category": "linear"}, "requires category=option"),
            ({"category": "OPTION"}, "requires category=option"),
            ({"category": _StringSubclass("option")}, "requires category=option"),
        )
        for query, message in cases:
            with self.subTest(query=query):
                with self.assertRaisesRegex(ProviderTransportScopeError, message):
                    sign(query)

    def test_unknown_query_fields_fail_closed(self):
        with self.assertRaisesRegex(
            ProviderTransportScopeError,
            "unsupported fields: accountType",
        ):
            sign({"category": "option", "accountType": "UNIFIED"})

    def test_symbol_must_be_uppercase_canonical_provider_text(self):
        for symbol in (
            "btc-29DEC22-16000-P",
            "BTC 29DEC22 16000 P",
            "-BTC-29DEC22-16000-P",
            "BTC-29DEC22-16000-P-",
            "A" * 161,
        ):
            with self.subTest(symbol=symbol):
                with self.assertRaisesRegex(
                    ProviderTransportScopeError,
                    "symbol must be uppercase canonical provider text",
                ):
                    sign({"category": "option", "symbol": symbol})

    def test_time_range_uses_canonical_milliseconds_and_maximum_thirty_days(self):
        start = 1700000000000
        cases = (
            (
                {"category": "option", "startTime": "01"},
                "startTime must be canonical integer text",
            ),
            (
                {"category": "option", "startTime": "9" * 4301},
                "startTime must be canonical integer text",
            ),
            (
                {
                    "category": "option",
                    "startTime": str(start),
                    "endTime": str(start - 1),
                },
                "endTime cannot precede startTime",
            ),
            (
                {
                    "category": "option",
                    "startTime": str(start),
                    "endTime": str(start + 30 * 24 * 60 * 60 * 1000 + 1),
                },
                "time range exceeds 30 days",
            ),
        )
        for query, message in cases:
            with self.subTest(query=query):
                with self.assertRaisesRegex(ProviderTransportScopeError, message):
                    sign(query)

    def test_limit_is_canonical_integer_in_documented_range(self):
        for limit in ("0", "51", "01", "-1", "1.0"):
            with self.subTest(limit=limit):
                with self.assertRaisesRegex(
                    ProviderTransportScopeError,
                    "limit",
                ):
                    sign({"category": "option", "limit": limit})

    def test_expiry_date_requires_documented_ddmmmyy_shape(self):
        for exp_date in (
            "00DEC22",
            "31APR22",
            "30FEB22",
            "29FEB23",
            "29dec22",
            "29XYZ22",
            "29DEC2022",
            "DEC29",
        ):
            with self.subTest(exp_date=exp_date):
                with self.assertRaisesRegex(
                    ProviderTransportScopeError,
                    "expDate must use DDMMMYY",
                ):
                    sign({"category": "option", "expDate": exp_date})

    def test_expiry_date_accepts_real_leap_day(self):
        signed = sign({"category": "option", "expDate": "29FEB24"})
        self.assertEqual(
            dict(parse_qsl(urlsplit(signed.url).query)),
            {"category": "option", "expDate": "29FEB24"},
        )

    def test_cursor_is_nonempty_canonical_opaque_text(self):
        for cursor in (
            "",
            " cursor",
            "cursor ",
            "two words",
            "\t",
            "a&b",
            "a=b",
            "%ZZ",
            "%3a",
            "%",
        ):
            with self.subTest(cursor=repr(cursor)):
                with self.assertRaisesRegex(
                    ProviderTransportScopeError,
                    "cursor must be canonical opaque percent-encoded text",
                ):
                    sign({"category": "option", "cursor": cursor})

    def test_delivery_endpoint_requires_activity_surface_and_account_read_permission(self):
        with self.assertRaisesRegex(
            ProviderTransportScopeError,
            "surface does not match Bybit policy",
        ):
            sign(
                {"category": "option"},
                surface=Surface.AUTHENTICATED_READ,
            )
        with self.assertRaisesRegex(
            ProviderTransportScopeError,
            "permission scope does not match Bybit endpoint policy",
        ):
            sign(
                {"category": "option"},
                permission_scope="ORDER.READ",
            )


if __name__ == "__main__":
    unittest.main()
