from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.corporate_action_evidence import (
    CorporateActionEvidenceConflict,
    CorporateActionEvidenceError,
    CorporateActionObservation,
    DurableCorporateActionEvidenceStore,
    resolve_authoritative_corporate_action,
)
from mvp.autotrade_mvp.corporate_actions import CorporateEvent
from mvp.autotrade_mvp.instruments import InstrumentRegistry, InstrumentVersion
from mvp.autotrade_mvp.persistence import JournalStore
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
    kind="CASH_DIVIDEND",
    per_share="1.25",
    currency="USDT",
    source_sequence=7,
    complete=True,
    corrects=None,
    announcement_at=None,
    record_at=None,
    ex_at=None,
    pay_at=None,
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
        "kind": kind,
        "per_share": per_share,
        "currency": currency,
        "source_sequence": source_sequence,
        "complete": complete,
    }
    for name, value in (
        ("corrects_external_event_id", corrects),
        ("announcement_at", announcement_at),
        ("record_at", record_at),
        ("ex_at", ex_at),
        ("pay_at", pay_at),
    ):
        if value is not None:
            payload[name] = (
                value.isoformat().replace("+00:00", "Z")
                if isinstance(value, datetime)
                else value
            )
    return observe_authenticated_json_response(
        query_binding=binding,
        http_status=200,
        response_bytes=json.dumps(
            payload, sort_keys=True, separators=(",", ":")
        ).encode("utf-8"),
        observed_at=READ_NOW + timedelta(seconds=observed_offset),
    )


def canonical_registry(*versions):
    selected = versions or (canonical_instrument(),)
    return InstrumentRegistry(versions=selected)


def resolve(
    source,
    *,
    permission_scope="ORDER.READ",
    instrument_registry=None,
    expected_provider_id="BINANCE",
    expected_account_id="acct-1",
    expected_environment="PAPER",
):
    return resolve_authoritative_corporate_action(
        source.evidence_ref,
        evidence_resolver={source.evidence_ref: source}.__getitem__,
        instrument_registry=(
            canonical_registry()
            if instrument_registry is None
            else instrument_registry
        ),
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
                instrument_registry=canonical_registry(),
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

    def test_arbitrary_normalizer_cannot_become_financial_authority(self):
        source = sealed_dividend()

        def forged(_source):
            return CorporateActionObservation(
                provider_id="BINANCE",
                account_id="acct-1",
                environment="PAPER",
                provider_instrument_version=source.query_binding.instrument_version,
                instrument_id=INSTRUMENT_ID,
                instrument_version=1,
                external_event_id="forged-event",
                provider_revision="forged-revision",
                kind="MERGER_CASH",
                effective_at=READ_NOW,
                observed_at=_instant(source.observed_at),
                raw_evidence_digest=source.response_sha256,
                payload={"amount": "999999", "currency": "USDT"},
                complete=True,
            )

        with self.assertRaisesRegex(TypeError, "normalizer"):
            resolve_authoritative_corporate_action(
                source.evidence_ref,
                evidence_resolver={source.evidence_ref: source}.__getitem__,
                instrument_registry=canonical_registry(),
                normalizer=forged,
                expected_provider_id="BINANCE",
                expected_account_id="acct-1",
                expected_environment="PAPER",
                allowed_endpoints=frozenset({ENDPOINT}),
                permission_scope="ORDER.READ",
            )

    def test_canonical_instrument_registry_binding_is_required(self):
        source = sealed_dividend()
        bad_versions = (
            canonical_instrument(
                instrument_id="22222222-2222-4222-8222-222222222222"
            ),
            canonical_instrument(version=2),
            canonical_instrument(provider_id="ALPACA"),
        )
        for version in bad_versions:
            with self.subTest(version=version), self.assertRaises(
                CorporateActionEvidenceError
            ):
                resolve(
                    source,
                    instrument_registry=canonical_registry(version),
                )

    def test_caller_instrument_resolver_is_rejected(self):
        source = sealed_dividend()
        with self.assertRaisesRegex(TypeError, "instrument_resolver"):
            resolve_authoritative_corporate_action(
                source.evidence_ref,
                evidence_resolver={source.evidence_ref: source}.__getitem__,
                instrument_registry=canonical_registry(),
                instrument_resolver=lambda _observation: canonical_instrument(),
                expected_provider_id="BINANCE",
                expected_account_id="acct-1",
                expected_environment="PAPER",
                allowed_endpoints=frozenset({ENDPOINT}),
                permission_scope="ORDER.READ",
            )

    def test_incomplete_provider_fact_cannot_authorize_event(self):
        source = sealed_dividend(complete=False)
        with self.assertRaisesRegex(
            CorporateActionEvidenceError, "incomplete"
        ):
            resolve(source)

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
                instrument_registry=canonical_registry(),
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

    def test_binary_float_in_sealed_financial_payload_is_rejected(self):
        source = sealed_dividend(per_share=1.25)
        with self.assertRaisesRegex(
            CorporateActionEvidenceError, "exact"
        ):
            resolve(source)

    def test_observation_carries_provider_lifecycle_and_correction_identity(self):
        effective = READ_NOW + timedelta(seconds=1)
        source = sealed_dividend(
            revision="2",
            corrects="corp-old",
            announcement_at=READ_NOW - timedelta(days=3),
            record_at=READ_NOW - timedelta(days=1),
            ex_at=effective,
            pay_at=effective + timedelta(days=2),
        )
        accepted = resolve(source)
        self.assertEqual(accepted.corrects_external_event_id, "corp-old")
        self.assertEqual(accepted.provider_revision, "2")
        self.assertIn(accepted.provenance_digest, accepted.event.source_revision)



class DurableCorporateActionEvidenceStoreTests(unittest.TestCase):
    def _accepted(
        self,
        *,
        external_event_id="corp-1",
        revision="1",
        observed_offset=2,
        corrects=None,
    ):
        source = sealed_dividend(
            external_event_id=external_event_id,
            revision=revision,
            observed_offset=observed_offset,
            corrects=corrects,
        )
        return resolve(source)

    def _store(self, path, *, account_id="acct-1"):
        journal = JournalStore(path)
        durable = DurableCorporateActionEvidenceStore(
            journal,
            provider_id="BINANCE",
            account_id=account_id,
            environment="PAPER",
        )
        return journal, durable

    def test_evidence_is_exactly_once_across_restart(self):
        accepted = self._accepted()
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            journal, durable = self._store(path)
            first = durable.record(accepted)
            self.assertTrue(first.inserted)
            self.assertEqual(first.aggregate_version, 1)

            reopened, restarted = self._store(path)
            replay = restarted.record(accepted)
            self.assertFalse(replay.inserted)
            self.assertEqual(replay.event_id, first.event_id)
            self.assertEqual(replay.provenance_digest, first.provenance_digest)
            events = reopened.load_events(
                "corporate_action_evidence",
                restarted.aggregate_id,
            )
            self.assertEqual(len(events), 1)
            self.assertEqual(
                events[0]["payload"]["evidence_ref"],
                accepted.evidence_ref,
            )
            self.assertEqual(
                events[0]["payload"]["provenance_digest"],
                accepted.provenance_digest,
            )

    def test_durable_scope_mismatch_fails_before_journal_mutation(self):
        accepted = self._accepted()
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            journal, durable = self._store(path, account_id="other-account")
            with self.assertRaisesRegex(
                CorporateActionEvidenceConflict, "durable scope"
            ):
                durable.record(accepted)
            self.assertEqual(
                journal.load_events(
                    "corporate_action_evidence",
                    durable.aggregate_id,
                ),
                [],
            )

    def test_same_external_identity_with_changed_evidence_conflicts(self):
        original = self._accepted()
        changed = self._accepted(
            external_event_id="corp-1",
            revision="2",
            observed_offset=3,
        )
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            journal, durable = self._store(path)
            durable.record(original)
            with self.assertRaisesRegex(
                CorporateActionEvidenceConflict, "reused with changed evidence"
            ):
                durable.record(changed)
            self.assertEqual(
                len(
                    journal.load_events(
                        "corporate_action_evidence",
                        durable.aggregate_id,
                    )
                ),
                1,
            )

    def test_correction_requires_one_retained_target_and_fresh_evidence(self):
        missing_target = self._accepted(
            external_event_id="corp-2",
            revision="2",
            observed_offset=3,
            corrects="corp-missing",
        )
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            journal, durable = self._store(path)
            with self.assertRaisesRegex(
                CorporateActionEvidenceConflict, "target"
            ):
                durable.record(missing_target)
            self.assertEqual(
                journal.load_events(
                    "corporate_action_evidence",
                    durable.aggregate_id,
                ),
                [],
            )

            original = self._accepted()
            durable.record(original)
            correction = self._accepted(
                external_event_id="corp-2",
                revision="2",
                observed_offset=3,
                corrects="corp-1",
            )
            result = durable.record(correction)
            self.assertTrue(result.inserted)
            self.assertEqual(result.aggregate_version, 2)
            self.assertEqual(result.corrects_external_event_id, "corp-1")

            restarted_journal, restarted = self._store(path)
            replay = restarted.record(correction)
            self.assertFalse(replay.inserted)
            self.assertEqual(replay.event_id, result.event_id)
            self.assertEqual(
                len(
                    restarted_journal.load_events(
                        "corporate_action_evidence",
                        restarted.aggregate_id,
                    )
                ),
                2,
            )

    def test_second_correction_for_same_provider_fact_is_rejected(self):
        original = self._accepted()
        correction = self._accepted(
            external_event_id="corp-2",
            revision="2",
            observed_offset=3,
            corrects="corp-1",
        )
        second = self._accepted(
            external_event_id="corp-3",
            revision="3",
            observed_offset=4,
            corrects="corp-1",
        )
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            journal, durable = self._store(path)
            durable.record(original)
            durable.record(correction)
            with self.assertRaisesRegex(
                CorporateActionEvidenceConflict, "already has a correction"
            ):
                durable.record(second)
            self.assertEqual(
                len(
                    journal.load_events(
                        "corporate_action_evidence",
                        durable.aggregate_id,
                    )
                ),
                2,
            )

    def test_correction_cannot_change_immutable_action_identity(self):
        original = self._accepted()
        correction = self._accepted(
            external_event_id="corp-2",
            revision="2",
            observed_offset=3,
            corrects="corp-1",
        )

        changed_event = CorporateEvent.create(
            event_id=correction.event.event_id,
            instrument_id=correction.event.instrument_id,
            instrument_version=correction.event.instrument_version,
            kind="SPLIT",
            effective_date=correction.event.effective_date,
            effective_at=correction.event.effective_at,
            source_revision=correction.event.source_revision,
            source_sequence=correction.event.source_sequence,
            payload={"numerator": "2", "denominator": "1"},
        )
        changed = type(correction)(
            **{
                **correction.__dict__,
                "event": changed_event,
            }
        )
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            journal, durable = self._store(path)
            durable.record(original)
            with self.assertRaisesRegex(
                CorporateActionEvidenceConflict, "immutable action identity"
            ):
                durable.record(changed)
            self.assertEqual(
                len(
                    journal.load_events(
                        "corporate_action_evidence",
                        durable.aggregate_id,
                    )
                ),
                1,
            )


if __name__ == "__main__":
    unittest.main()
