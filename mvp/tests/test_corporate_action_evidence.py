from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import unittest

from mvp.autotrade_mvp.corporate_action_evidence import (
    CorporateActionEvidenceError,
    CorporateActionObservation,
    resolve_authoritative_corporate_action,
)
from mvp.autotrade_mvp.corporate_actions import CorporateEvent
from mvp.autotrade_mvp.instruments import InstrumentVersion
from mvp.autotrade_mvp.provider_core import (
    Surface,
    observe_authenticated_json_response,
    prepare_authenticated_read_query,
)
from mvp.tests.test_provider_transport import READ_NOW, verified_read_capability


ENDPOINT = "/sapi/v1/asset/corporate-action"
INSTRUMENT_ID = "11111111-1111-4111-8111-111111111111"


def _instant(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(
        timezone.utc
    )


def canonical_instrument(
    *,
    instrument_id=INSTRUMENT_ID,
    version=1,
    provider_id="BINANCE",
):
    return InstrumentVersion(
        instrument_id=instrument_id,
        version=version,
        provider_id=provider_id,
        venue_id="BINANCE",
        provider_symbol="BTCUSDT",
        asset_class="CASH_EQUITY",
        base_currency="BTC",
        quote_currency="USDT",
        settlement_currency="USDT",
        quantity_unit="BTC",
        contract_multiplier=Decimal("1"),
        price_tick=Decimal("0.01"),
        quantity_step=Decimal("0.00000001"),
        minimum_quantity=Decimal("0.00000001"),
        calendar_id="CONTINUOUS_24_7",
        timezone_id="UTC",
        effective_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
        status="ACTIVE",
    )


def sealed_dividend(
    *,
    external_event_id="corp-1",
    revision="1",
    observed_offset=2,
    effective_offset=1,
):
    binding = prepare_authenticated_read_query(
        capability=verified_read_capability(),
        surface=Surface.ACTIVITIES,
        endpoint=ENDPOINT,
        query={"symbol": "BTCUSDT"},
        at=READ_NOW,
        permission_scope="ORDER.READ",
    )
    payload = {
        "external_event_id": external_event_id,
        "provider_revision": revision,
        "instrument_id": INSTRUMENT_ID,
        "instrument_version": 1,
        "effective_at": (
            READ_NOW + timedelta(seconds=effective_offset)
        ).isoformat().replace("+00:00", "Z"),
        "kind": "CASH_DIVIDEND",
        "per_share": "1.25",
        "currency": "USDT",
        "source_sequence": 7,
        "complete": True,
    }
    return observe_authenticated_json_response(
        query_binding=binding,
        http_status=200,
        response_bytes=json.dumps(
            payload, sort_keys=True, separators=(",", ":")
        ).encode("utf-8"),
        observed_at=READ_NOW + timedelta(seconds=observed_offset),
    )


def normalize(source):
    payload = source.payload
    return CorporateActionObservation(
        provider_id=source.provider_id,
        account_id=source.account_id,
        environment=source.environment,
        provider_instrument_version=source.query_binding.instrument_version,
        instrument_id=payload["instrument_id"],
        instrument_version=payload["instrument_version"],
        external_event_id=payload["external_event_id"],
        provider_revision=payload["provider_revision"],
        kind=payload["kind"],
        effective_at=_instant(payload["effective_at"]),
        observed_at=_instant(source.observed_at),
        raw_evidence_digest=source.response_sha256,
        payload={
            "per_share": payload["per_share"],
            "currency": payload["currency"],
        },
        complete=payload["complete"],
        source_sequence=payload["source_sequence"],
    )


def resolve(
    source,
    *,
    normalizer=normalize,
    permission_scope="ORDER.READ",
    instrument_resolver=lambda _observation: canonical_instrument(),
    expected_provider_id="BINANCE",
    expected_account_id="acct-1",
    expected_environment="PAPER",
):
    return resolve_authoritative_corporate_action(
        source.evidence_ref,
        evidence_resolver={source.evidence_ref: source}.__getitem__,
        normalizer=normalizer,
        instrument_resolver=instrument_resolver,
        expected_provider_id=expected_provider_id,
        expected_account_id=expected_account_id,
        expected_environment=expected_environment,
        allowed_endpoints=frozenset({ENDPOINT}),
        permission_scope=permission_scope,
    )


class CorporateActionEvidenceBoundaryTests(unittest.TestCase):
    def test_sealed_provider_evidence_creates_bound_corporate_event(self):
        source = sealed_dividend()
        accepted = resolve(source)

        self.assertEqual(accepted.provider_id, "BINANCE")
        self.assertEqual(accepted.account_id, "acct-1")
        self.assertEqual(accepted.environment, "PAPER")
        self.assertEqual(
            accepted.provider_instrument_version,
            source.query_binding.instrument_version,
        )
        self.assertEqual(accepted.raw_evidence_digest, source.response_sha256)
        self.assertEqual(accepted.evidence_ref, source.evidence_ref)
        self.assertEqual(accepted.query_digest, source.query_binding.query_digest)
        self.assertEqual(
            accepted.capability_snapshot_id,
            source.query_binding.capability_snapshot_id,
        )
        self.assertTrue(accepted.provenance_digest.startswith("sha256:"))
        self.assertEqual(len(accepted.provenance_digest), 71)

        event = accepted.event
        self.assertIsInstance(event, CorporateEvent)
        self.assertEqual(event.event_id, "corp-1")
        self.assertEqual(event.instrument_id, INSTRUMENT_ID)
        self.assertEqual(event.instrument_version, 1)
        self.assertEqual(event.kind, "CASH_DIVIDEND")
        self.assertEqual(event.source_sequence, 7)
        self.assertEqual(
            event.payload,
            {"per_share": "1.25", "currency": "USDT"},
        )
        self.assertIn(accepted.provenance_digest, event.source_revision)

    def test_resolution_is_deterministic_for_same_sealed_evidence(self):
        source = sealed_dividend()
        first = resolve(source)
        second = resolve(source)
        self.assertEqual(first, second)
        self.assertEqual(first.event, second.event)

    def test_locally_constructed_event_is_not_provider_evidence(self):
        local = CorporateEvent.create(
            event_id="local-1",
            instrument_id=INSTRUMENT_ID,
            instrument_version=1,
            kind="CASH_DIVIDEND",
            effective_date=(READ_NOW + timedelta(seconds=1)).date(),
            effective_at=READ_NOW + timedelta(seconds=1),
            source_revision="caller-says-valid",
            payload={"per_share": "1.25", "currency": "USDT"},
        )
        with self.assertRaisesRegex(
            CorporateActionEvidenceError, "sealed ProviderResponseObservation"
        ):
            resolve_authoritative_corporate_action(
                "provider-read:sha256:" + "a" * 64,
                evidence_resolver=lambda _ref: local,
                normalizer=lambda _source: None,
                instrument_resolver=lambda _observation: canonical_instrument(),
                expected_provider_id="BINANCE",
                expected_account_id="acct-1",
                expected_environment="PAPER",
                allowed_endpoints=frozenset({ENDPOINT}),
                permission_scope="ORDER.READ",
            )

    def test_expected_provider_account_environment_scope_is_authoritative(self):
        source = sealed_dividend()
        for field, value in (
            ("expected_provider_id", "ALPACA"),
            ("expected_account_id", "other-account"),
            ("expected_environment", "LIVE"),
        ):
            with self.subTest(field=field), self.assertRaisesRegex(
                CorporateActionEvidenceError, "scope mismatch"
            ):
                resolve(source, **{field: value})

    def test_changed_provider_scope_from_normalizer_fails_closed(self):
        source = sealed_dividend()

        def wrong_account(value):
            item = normalize(value)
            return CorporateActionObservation(
                **{**item.__dict__, "account_id": "other-account"}
            )

        with self.assertRaisesRegex(
            CorporateActionEvidenceError, "does not match sealed provider evidence"
        ):
            resolve(source, normalizer=wrong_account)

    def test_changed_raw_digest_from_normalizer_fails_closed(self):
        source = sealed_dividend()

        def wrong_digest(value):
            item = normalize(value)
            return CorporateActionObservation(
                **{
                    **item.__dict__,
                    "raw_evidence_digest": "sha256:" + "0" * 64,
                }
            )

        with self.assertRaisesRegex(
            CorporateActionEvidenceError, "does not match sealed provider evidence"
        ):
            resolve(source, normalizer=wrong_digest)

    def test_canonical_instrument_binding_is_required(self):
        source = sealed_dividend()

        for resolver in (
            lambda _observation: canonical_instrument(
                instrument_id="22222222-2222-4222-8222-222222222222"
            ),
            lambda _observation: canonical_instrument(version=2),
            lambda _observation: canonical_instrument(provider_id="ALPACA"),
        ):
            with self.subTest(resolver=resolver), self.assertRaisesRegex(
                CorporateActionEvidenceError, "canonical instrument binding"
            ):
                resolve(source, instrument_resolver=resolver)

    def test_instrument_resolver_failure_is_fail_closed(self):
        source = sealed_dividend()

        def broken(_observation):
            raise RuntimeError("registry unavailable")

        with self.assertRaisesRegex(
            CorporateActionEvidenceError, "instrument could not be resolved"
        ):
            resolve(source, instrument_resolver=broken)

    def test_incomplete_provider_fact_cannot_authorize_event(self):
        source = sealed_dividend()

        def incomplete(value):
            item = normalize(value)
            return CorporateActionObservation(
                **{**item.__dict__, "complete": False}
            )

        with self.assertRaisesRegex(
            CorporateActionEvidenceError, "incomplete"
        ):
            resolve(source, normalizer=incomplete)

    def test_pre_effective_announcement_preserves_causal_observation_time(self):
        source = sealed_dividend(observed_offset=1, effective_offset=5)
        accepted = resolve(source)

        self.assertLess(
            _instant(accepted.observed_at),
            accepted.event.effective_at,
        )
        self.assertEqual(
            accepted.observed_at,
            source.observed_at,
        )
        self.assertIn(
            accepted.provenance_digest,
            accepted.event.source_revision,
        )

    def test_wrong_endpoint_is_rejected_before_normalization(self):
        source = sealed_dividend()
        with self.assertRaisesRegex(
            CorporateActionEvidenceError, "endpoint is not allowed"
        ):
            resolve_authoritative_corporate_action(
                source.evidence_ref,
                evidence_resolver={source.evidence_ref: source}.__getitem__,
                normalizer=normalize,
                instrument_resolver=lambda _observation: canonical_instrument(),
                expected_provider_id="BINANCE",
                expected_account_id="acct-1",
                expected_environment="PAPER",
                allowed_endpoints=frozenset({"/different/activity"}),
                permission_scope="ORDER.READ",
            )

    def test_wrong_permission_scope_is_rejected(self):
        source = sealed_dividend()
        with self.assertRaisesRegex(
            CorporateActionEvidenceError, "permission scope mismatch"
        ):
            resolve(source, permission_scope="CORPORATE.READ")

    def test_binary_float_in_normalized_financial_payload_is_rejected(self):
        source = sealed_dividend()

        def binary_float(value):
            item = normalize(value)
            return CorporateActionObservation(
                **{
                    **item.__dict__,
                    "payload": {
                        "per_share": 1.25,
                        "currency": "USDT",
                    },
                }
            )

        with self.assertRaisesRegex(
            CorporateActionEvidenceError, "normalization failed"
        ):
            resolve(source, normalizer=binary_float)

    def test_observation_carries_optional_lifecycle_and_correction_identity(self):
        source = sealed_dividend(revision="2")

        def lifecycle(value):
            item = normalize(value)
            return CorporateActionObservation(
                **{
                    **item.__dict__,
                    "announcement_at": READ_NOW - timedelta(days=3),
                    "record_at": READ_NOW - timedelta(days=1),
                    "ex_at": item.effective_at,
                    "pay_at": item.effective_at + timedelta(days=2),
                    "corrects_external_event_id": "corp-old",
                }
            )

        accepted = resolve(source, normalizer=lifecycle)
        self.assertEqual(accepted.corrects_external_event_id, "corp-old")
        self.assertEqual(accepted.provider_revision, "2")
        self.assertIn(accepted.provenance_digest, accepted.event.source_revision)


if __name__ == "__main__":
    unittest.main()
