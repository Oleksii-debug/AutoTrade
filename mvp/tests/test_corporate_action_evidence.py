from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from tempfile import TemporaryDirectory
import unittest
import weakref

import mvp.autotrade_mvp.corporate_action_evidence as corporate_action_evidence_module
from mvp.autotrade_mvp.corporate_action_evidence import (
    AuthoritativeCorporateAction,
    CorporateActionEvidenceConflict,
    CorporateActionEvidenceError,
    CorporateActionObservation,
    DurableCorporateActionEvidenceStore,
    resolve_authoritative_corporate_action,
)
from mvp.autotrade_mvp.capabilities import (
    CapabilityClaim,
    EvidenceVerification,
    derive_capability_snapshot,
)
from mvp.autotrade_mvp.corporate_actions import CorporateEvent
from mvp.autotrade_mvp.instruments import InstrumentRegistry, InstrumentVersion
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.provider_core import (
    ProviderResponseObservation,
    Surface,
    observe_authenticated_json_response,
    prepare_authenticated_read_query,
)
from mvp.tests.test_provider_transport import READ_NOW


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
    effective_from=None,
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
        effective_from=(
            datetime(2026, 1, 1, tzinfo=timezone.utc)
            if effective_from is None
            else effective_from
        ),
        status="ACTIVE",
    )


_SIMULATION_SNAPSHOT_ID = "77777777-7777-4777-8777-777777777777"
_SIMULATION_ARTIFACT_IDS = {
    "DOCUMENTED": "71111111-1111-4111-8111-111111111111",
    "API": "72222222-2222-4222-8222-222222222222",
    "ACCOUNT": "73333333-3333-4333-8333-333333333333",
    "INSTRUMENT": "74444444-4444-4444-8444-444444444444",
}


def simulation_read_capability():
    observed = READ_NOW - timedelta(minutes=1)
    expires = READ_NOW + timedelta(minutes=10)
    claims = tuple(
        CapabilityClaim(
            source=source,
            provider_id="BINANCE",
            account_id="acct-1",
            entity_id="entity-1",
            environment="SIMULATION",
            instrument_version="BTCUSDT@1",
            observed_at=observed,
            expires_at=expires,
            supported_order_types=frozenset({"LIMIT"}),
            time_in_force=frozenset({"GTC"}),
            permission_scopes=frozenset({"ORDER.READ"}),
            position_mode="NET",
            native_protection=frozenset(),
            rate_limit_policy_id="binance-simulation-v1",
            data_entitlements=frozenset({"ACCOUNT"}),
            evidence_ref={
                "artifact_id": _SIMULATION_ARTIFACT_IDS[source],
                "sha256": "sha256:" + "a" * 64,
                "observed_at": observed.isoformat().replace("+00:00", "Z"),
            },
        )
        for source in ("DOCUMENTED", "API", "ACCOUNT", "INSTRUMENT")
    )
    return derive_capability_snapshot(
        snapshot_id=_SIMULATION_SNAPSHOT_ID,
        claims=claims,
        observed_at=READ_NOW,
        evidence_verifier=lambda _claim: EvidenceVerification(valid=True),
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
        capability=simulation_read_capability(),
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
    expected_environment="SIMULATION",
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
        self.assertEqual(accepted.environment, "SIMULATION")
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

    def test_provider_observation_subclass_is_rejected_before_attribute_access(self):
        class ForgedObservation(ProviderResponseObservation):
            @property
            def evidence_ref(self):
                raise AssertionError("subclass evidence_ref must not execute")

            def require_scope(self, **_kwargs):
                raise AssertionError("subclass require_scope must not execute")

        forged = object.__new__(ForgedObservation)
        with self.assertRaisesRegex(
            CorporateActionEvidenceError,
            "exact sealed ProviderResponseObservation",
        ):
            resolve_authoritative_corporate_action(
                "evidence:forged",
                evidence_resolver=lambda _reference: forged,
                instrument_registry=canonical_registry(),
                expected_provider_id="BINANCE",
                expected_account_id="acct-1",
                expected_environment="SIMULATION",
                allowed_endpoints=frozenset({ENDPOINT}),
                permission_scope="ORDER.READ",
            )

    def test_provider_observation_instance_scope_shadow_is_rejected_before_dispatch(self):
        source = sealed_dividend()
        calls = []

        def forged_scope(**_kwargs):
            calls.append("called")
            raise AssertionError("shadowed source require_scope must not execute")

        object.__setattr__(source, "require_scope", forged_scope)
        with self.assertRaisesRegex(
            CorporateActionEvidenceError,
            "observation callback must not be shadowed",
        ):
            resolve(source)
        self.assertEqual(calls, [])

    def test_query_binding_instance_scope_shadow_is_rejected_before_dispatch(self):
        source = sealed_dividend()
        calls = []

        def forged_scope(**_kwargs):
            calls.append("called")
            raise AssertionError("shadowed binding require_scope must not execute")

        object.__setattr__(source.query_binding, "require_scope", forged_scope)
        with self.assertRaisesRegex(
            CorporateActionEvidenceError,
            "binding callback must not be shadowed",
        ):
            resolve(source)
        self.assertEqual(calls, [])

    def test_mutated_provider_payload_fails_before_hostile_mapping_dispatch(self):
        source = sealed_dividend()

        class HostilePayload(dict):
            calls = 0

            def _explode(self):
                type(self).calls += 1
                raise AssertionError("mutated payload must not be inspected")

            def __iter__(self):
                self._explode()

            def items(self):
                self._explode()

            def get(self, *_args, **_kwargs):
                self._explode()

            def __getitem__(self, _key):
                self._explode()

        hostile = HostilePayload()
        object.__setattr__(source, "payload", hostile)

        with self.assertRaisesRegex(
            CorporateActionEvidenceError,
            "provider observation authority mismatch",
        ):
            resolve(source)
        self.assertEqual(HostilePayload.calls, 0)

    def test_mutated_query_binding_fails_before_polymorphic_text_dispatch(self):
        source = sealed_dividend()

        class HostileText(str):
            calls = 0

            def strip(self, *_args, **_kwargs):
                type(self).calls += 1
                raise AssertionError("mutated query text must not dispatch")

            def upper(self, *_args, **_kwargs):
                type(self).calls += 1
                raise AssertionError("mutated query text must not dispatch")

        object.__setattr__(
            source.query_binding,
            "endpoint",
            HostileText(ENDPOINT),
        )
        with self.assertRaisesRegex(
            CorporateActionEvidenceError,
            "provider observation authority mismatch",
        ):
            resolve(source)
        self.assertEqual(HostileText.calls, 0)

    def test_late_module_global_provider_projection_decoys_do_not_run(self):
        source = sealed_dividend()
        calls = []
        original_projection = (
            corporate_action_evidence_module.provider_response_observation_projection
        )
        original_scope = (
            corporate_action_evidence_module.provider_response_observation_require_scope
        )

        def forged_projection(_source):
            calls.append("projection")
            raise AssertionError("late projection decoy must not execute")

        def forged_scope(_source, **_kwargs):
            calls.append("scope")
            raise AssertionError("late scope decoy must not execute")

        corporate_action_evidence_module.provider_response_observation_projection = (
            forged_projection
        )
        corporate_action_evidence_module.provider_response_observation_require_scope = (
            forged_scope
        )
        try:
            accepted = resolve(source)
        finally:
            corporate_action_evidence_module.provider_response_observation_projection = (
                original_projection
            )
            corporate_action_evidence_module.provider_response_observation_require_scope = (
                original_scope
            )

        self.assertEqual(calls, [])
        self.assertEqual(accepted.evidence_ref, source.evidence_ref)

    def test_late_module_global_instrument_registry_cannot_replace_canonical_registry(self):
        source = sealed_dividend()
        calls = []
        instrument = canonical_instrument()
        original = corporate_action_evidence_module.InstrumentRegistry

        class DecoyRegistry:
            @staticmethod
            def exact(_registry, _version_ref):
                calls.append("exact")
                return instrument

            @staticmethod
            def at(_registry, _instrument_id, _instant):
                calls.append("at")
                return instrument

        corporate_action_evidence_module.InstrumentRegistry = DecoyRegistry
        try:
            with self.assertRaisesRegex(TypeError, "exact InstrumentRegistry"):
                resolve(
                    source,
                    instrument_registry=DecoyRegistry(),
                )
        finally:
            corporate_action_evidence_module.InstrumentRegistry = original
        self.assertEqual(calls, [])

    def test_late_module_global_event_and_action_decoys_do_not_mint_authority(self):
        source = sealed_dividend()
        calls = []
        original_event = corporate_action_evidence_module.CorporateEvent
        original_action = corporate_action_evidence_module.AuthoritativeCorporateAction

        class DecoyEvent:
            @classmethod
            def create(cls, **_kwargs):
                calls.append("event")
                raise AssertionError("late CorporateEvent decoy must not execute")

        class DecoyAction:
            def __init__(self, **_kwargs):
                calls.append("action")
                raise AssertionError(
                    "late AuthoritativeCorporateAction decoy must not execute"
                )

        corporate_action_evidence_module.CorporateEvent = DecoyEvent
        corporate_action_evidence_module.AuthoritativeCorporateAction = DecoyAction
        try:
            accepted = resolve(source)
        finally:
            corporate_action_evidence_module.CorporateEvent = original_event
            corporate_action_evidence_module.AuthoritativeCorporateAction = original_action

        self.assertEqual(calls, [])
        self.assertIs(type(accepted), AuthoritativeCorporateAction)
        self.assertIs(type(accepted.event), CorporateEvent)

    def test_instrument_registry_subclass_is_rejected_before_registry_dispatch(self):
        class ForgedRegistry(InstrumentRegistry):
            def exact(self, _version_ref):
                raise AssertionError("subclass exact must not execute")

            def at(self, _instrument_id, _at):
                raise AssertionError("subclass at must not execute")

        source = sealed_dividend()
        forged = ForgedRegistry(versions=(canonical_instrument(),))
        with self.assertRaisesRegex(TypeError, "exact InstrumentRegistry"):
            resolve(
                source,
                instrument_registry=forged,
            )

    def test_instrument_registry_instance_shadow_is_rejected_before_dispatch(self):
        source = sealed_dividend()
        registry = canonical_registry()
        registry.exact = lambda _version_ref: (_ for _ in ()).throw(
            AssertionError("shadowed exact must not execute")
        )
        registry.at = lambda _instrument_id, _at: (_ for _ in ()).throw(
            AssertionError("shadowed at must not execute")
        )

        with self.assertRaisesRegex(TypeError, "must not be shadowed"):
            resolve(
                source,
                instrument_registry=registry,
            )

    def test_allowed_endpoints_subclass_is_rejected_before_iteration(self):
        class ForgedEndpoints(frozenset):
            def __iter__(self):
                raise AssertionError("subclass iteration must not execute")

        source = sealed_dividend()
        with self.assertRaisesRegex(TypeError, "exact non-empty frozenset"):
            resolve_authoritative_corporate_action(
                source.evidence_ref,
                evidence_resolver={source.evidence_ref: source}.__getitem__,
                instrument_registry=canonical_registry(),
                expected_provider_id="BINANCE",
                expected_account_id="acct-1",
                expected_environment="SIMULATION",
                allowed_endpoints=ForgedEndpoints({ENDPOINT}),
                permission_scope="ORDER.READ",
            )

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
                expected_environment="SIMULATION",
                allowed_endpoints=frozenset({ENDPOINT}),
                permission_scope="ORDER.READ",
            )

    def test_expected_provider_account_environment_scope_is_authoritative(self):
        source = sealed_dividend()
        for field, value in (
            ("expected_provider_id", "ALPACA"),
            ("expected_account_id", "other-account"),
            ("expected_environment", "REPLAY"),
        ):
            with self.subTest(field=field), self.assertRaisesRegex(
                CorporateActionEvidenceError, "scope mismatch"
            ):
                resolve(source, **{field: value})

    def test_paper_and_live_require_provider_origin_before_resolver_callback(self):
        source = sealed_dividend()
        for environment in ("PAPER", "LIVE"):
            calls = []

            def resolver(reference):
                calls.append(reference)
                return source

            with self.subTest(environment=environment), self.assertRaisesRegex(
                CorporateActionEvidenceError,
                "PAPER/LIVE corporate actions require durable provider-origin authority",
            ):
                resolve_authoritative_corporate_action(
                    source.evidence_ref,
                    evidence_resolver=resolver,
                    instrument_registry=canonical_registry(),
                    expected_provider_id="BINANCE",
                    expected_account_id="acct-1",
                    expected_environment=environment,
                    allowed_endpoints=frozenset({ENDPOINT}),
                    permission_scope="ORDER.READ",
                )
            self.assertEqual(calls, [])

    def test_production_firebreak_rejects_polymorphic_environment_without_callbacks(self):
        source = sealed_dividend()
        resolver_calls = []

        class HostileText(str):
            calls = 0

            def strip(self, *_args, **_kwargs):
                type(self).calls += 1
                raise AssertionError("hostile strip dispatched")

            def upper(self, *_args, **_kwargs):
                type(self).calls += 1
                raise AssertionError("hostile upper dispatched")

        def resolver(reference):
            resolver_calls.append(reference)
            return source

        with self.assertRaisesRegex(
            CorporateActionEvidenceError,
            "expected_environment must be canonical exact text",
        ):
            resolve_authoritative_corporate_action(
                source.evidence_ref,
                evidence_resolver=resolver,
                instrument_registry=canonical_registry(),
                expected_provider_id="BINANCE",
                expected_account_id="acct-1",
                expected_environment=HostileText("PAPER"),
                allowed_endpoints=frozenset({ENDPOINT}),
                permission_scope="ORDER.READ",
            )
        self.assertEqual(HostileText.calls, 0)
        self.assertEqual(resolver_calls, [])

    def test_authority_selector_text_subclasses_reject_before_callbacks_and_resolution(self):
        source = sealed_dividend()

        class HostileText(str):
            calls = 0

            def strip(self, *_args, **_kwargs):
                type(self).calls += 1
                raise AssertionError("hostile strip dispatched")

            def upper(self, *_args, **_kwargs):
                type(self).calls += 1
                raise AssertionError("hostile upper dispatched")

        cases = (
            ("evidence_ref", HostileText(source.evidence_ref)),
            ("expected_provider_id", HostileText("BINANCE")),
            ("expected_account_id", HostileText("acct-1")),
            ("permission_scope", HostileText("ORDER.READ")),
        )
        for field, hostile in cases:
            resolver_calls = []

            def resolver(reference):
                resolver_calls.append(reference)
                return source

            kwargs = {
                "evidence_resolver": resolver,
                "instrument_registry": canonical_registry(),
                "expected_provider_id": "BINANCE",
                "expected_account_id": "acct-1",
                "expected_environment": "SIMULATION",
                "allowed_endpoints": frozenset({ENDPOINT}),
                "permission_scope": "ORDER.READ",
            }
            reference = source.evidence_ref
            if field == "evidence_ref":
                reference = hostile
            else:
                kwargs[field] = hostile

            before = HostileText.calls
            with self.subTest(field=field), self.assertRaisesRegex(
                CorporateActionEvidenceError,
                "canonical exact text",
            ):
                resolve_authoritative_corporate_action(reference, **kwargs)
            self.assertEqual(HostileText.calls, before)
            self.assertEqual(resolver_calls, [])

    def test_arbitrary_normalizer_cannot_become_financial_authority(self):
        source = sealed_dividend()

        def forged(_source):
            return CorporateActionObservation(
                provider_id="BINANCE",
                account_id="acct-1",
                environment="SIMULATION",
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
                expected_environment="SIMULATION",
                allowed_endpoints=frozenset({ENDPOINT}),
                permission_scope="ORDER.READ",
            )

    def test_canonical_instrument_registry_binding_is_required(self):
        source = sealed_dividend()
        bad_registries = (
            canonical_registry(
                canonical_instrument(
                    instrument_id="22222222-2222-4222-8222-222222222222"
                )
            ),
            canonical_registry(
                canonical_instrument(),
                canonical_instrument(
                    version=2,
                    effective_from=READ_NOW,
                ),
            ),
            canonical_registry(
                canonical_instrument(provider_id="ALPACA")
            ),
        )
        for registry in bad_registries:
            with self.subTest(registry=registry), self.assertRaises(
                CorporateActionEvidenceError
            ):
                resolve(
                    source,
                    instrument_registry=registry,
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
                expected_environment="SIMULATION",
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
                expected_environment="SIMULATION",
                allowed_endpoints=frozenset({"/different/activity"}),
                permission_scope="ORDER.READ",
            )

    def test_wrong_permission_scope_is_rejected(self):
        source = sealed_dividend()
        with self.assertRaisesRegex(
            CorporateActionEvidenceError, "permission scope mismatch"
        ):
            resolve(source, permission_scope="CORPORATE.READ")

    def test_direct_binary_float_payload_is_rejected_before_normalization(self):
        source = sealed_dividend()
        with self.assertRaisesRegex(
            CorporateActionEvidenceError, "exact"
        ):
            CorporateActionObservation(
                provider_id="BINANCE",
                account_id="acct-1",
                environment="SIMULATION",
                provider_instrument_version=source.query_binding.instrument_version,
                instrument_id=INSTRUMENT_ID,
                instrument_version=1,
                external_event_id="corp-float",
                provider_revision="1",
                kind="CASH_DIVIDEND",
                effective_at=READ_NOW + timedelta(seconds=1),
                observed_at=READ_NOW + timedelta(seconds=2),
                raw_evidence_digest=source.response_sha256,
                payload={"per_share": 1.25, "currency": "USDT"},
                complete=True,
            )

    def test_sealed_json_number_is_canonicalized_to_exact_decimal_text(self):
        source = sealed_dividend(per_share=1.25)
        accepted = resolve(source)
        self.assertEqual(
            accepted.event.payload,
            {"per_share": "1.25", "currency": "USDT"},
        )

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
            environment="SIMULATION",
        )
        return journal, durable

    def test_prepared_mutation_uses_detached_issued_action_snapshot(self):
        accepted = self._accepted()
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            _journal, durable = self._store(path)
            plan = durable.prepare_record_mutation(accepted)

        self.assertIsNot(plan.accepted, accepted)
        self.assertIsNot(plan.accepted.event, accepted.event)
        self.assertEqual(plan.accepted, accepted)

        accepted.event.payload["per_share"] = "999"
        object.__setattr__(accepted, "provider_revision", "forged-revision")
        self.assertEqual(plan.accepted.provider_revision, "1")
        self.assertEqual(plan.accepted.event.payload["per_share"], "1.25")

    def test_module_global_decoys_cannot_mint_or_verify_corporate_action_authority(self):
        decoy_calls = []
        corporate_action_evidence_module._register_authoritative_corporate_action = (
            lambda _value: decoy_calls.append("register")
        )
        corporate_action_evidence_module._require_authoritative_corporate_action = (
            lambda _value: decoy_calls.append("require")
        )
        corporate_action_evidence_module._resolve_authoritative_corporate_action_impl = (
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("decoy resolver implementation must not execute")
            )
        )
        try:
            issued = self._accepted()
            self.assertEqual(decoy_calls, [])
            forged = AuthoritativeCorporateAction(**issued.__dict__)
            with TemporaryDirectory() as directory:
                path = f"{directory}/journal.sqlite3"
                journal, durable = self._store(path)
                with self.assertRaisesRegex(
                    CorporateActionEvidenceError,
                    "lacks canonical resolver issuance authority",
                ):
                    durable.record(forged)
                self.assertEqual(decoy_calls, [])
                self.assertEqual(
                    journal.load_events(
                        "corporate_action_evidence",
                        durable.aggregate_id,
                    ),
                    [],
                )
        finally:
            del corporate_action_evidence_module._register_authoritative_corporate_action
            del corporate_action_evidence_module._require_authoritative_corporate_action
            del corporate_action_evidence_module._resolve_authoritative_corporate_action_impl

    def test_manually_constructed_authoritative_action_cannot_reach_durable_store(self):
        issued = self._accepted()
        forged = AuthoritativeCorporateAction(**issued.__dict__)
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            journal, durable = self._store(path)
            with self.assertRaisesRegex(
                CorporateActionEvidenceError,
                "lacks canonical resolver issuance authority",
            ):
                durable.record(forged)
            self.assertEqual(
                journal.load_events(
                    "corporate_action_evidence",
                    durable.aggregate_id,
                ),
                [],
            )

    def test_post_issuance_event_payload_mutation_is_rejected_before_journal_mutation(self):
        accepted = self._accepted()
        accepted.event.payload["per_share"] = "999"
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            journal, durable = self._store(path)
            with self.assertRaisesRegex(
                CorporateActionEvidenceError,
                "changed after resolver issuance",
            ):
                durable.record(accepted)
            self.assertEqual(
                journal.load_events(
                    "corporate_action_evidence",
                    durable.aggregate_id,
                ),
                [],
            )

    def test_equal_polymorphic_scalar_cannot_replace_issued_exact_text(self):
        class EqualText(str):
            pass

        accepted = self._accepted()
        object.__setattr__(
            accepted,
            "provider_revision",
            EqualText(accepted.provider_revision),
        )
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            journal, durable = self._store(path)
            with self.assertRaisesRegex(
                CorporateActionEvidenceError,
                "provider_revision must remain exact text",
            ):
                durable.record(accepted)
            self.assertEqual(
                journal.load_events(
                    "corporate_action_evidence",
                    durable.aggregate_id,
                ),
                [],
            )

    def test_post_issuance_action_mutation_is_rejected_before_journal_mutation(self):
        accepted = self._accepted()
        object.__setattr__(accepted, "provider_revision", "forged-revision")
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            journal, durable = self._store(path)
            with self.assertRaisesRegex(
                CorporateActionEvidenceError,
                "changed after resolver issuance",
            ):
                durable.record(accepted)
            self.assertEqual(
                journal.load_events(
                    "corporate_action_evidence",
                    durable.aggregate_id,
                ),
                [],
            )

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

    def test_journal_store_subclass_is_rejected_before_evidence_reads(self):
        class ForgedJournalStore(JournalStore):
            pass

        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            with self.assertRaisesRegex(TypeError, "exact JournalStore"):
                DurableCorporateActionEvidenceStore(
                    ForgedJournalStore(path),
                    provider_id="BINANCE",
                    account_id="acct-1",
                    environment="SIMULATION",
                )

    def test_construction_time_journal_method_shadow_is_rejected(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            journal = JournalStore(path)
            journal.load_events = lambda *args, **kwargs: []
            with self.assertRaisesRegex(TypeError, "shadowed"):
                DurableCorporateActionEvidenceStore(
                    journal,
                    provider_id="BINANCE",
                    account_id="acct-1",
                    environment="SIMULATION",
                )

    def test_post_construction_journal_shadow_fails_before_mutation(self):
        accepted = self._accepted()
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            journal, durable = self._store(path)
            journal.commit_command = lambda **kwargs: (kwargs["command_id"], True, {})
            with self.assertRaisesRegex(TypeError, "shadowed"):
                durable.record(accepted)
            del journal.commit_command
            self.assertEqual(
                JournalStore.load_events(
                    journal,
                    "corporate_action_evidence",
                    durable.aggregate_id,
                ),
                [],
            )

    def test_post_construction_store_swap_fails_before_either_store_mutates(self):
        accepted = self._accepted()
        with TemporaryDirectory() as directory:
            first_path = f"{directory}/first.sqlite3"
            second_path = f"{directory}/second.sqlite3"
            first, durable = self._store(first_path)
            second = JournalStore(second_path)
            durable.store = second

            with self.assertRaisesRegex(
                CorporateActionEvidenceConflict,
                "composition was modified",
            ):
                durable.record(accepted)

            self.assertEqual(
                JournalStore.load_events(
                    first,
                    "corporate_action_evidence",
                    durable.aggregate_id,
                ),
                [],
            )
            self.assertEqual(
                JournalStore.load_events(
                    second,
                    "corporate_action_evidence",
                    durable.aggregate_id,
                ),
                [],
            )


    def test_coordinated_store_and_scope_retarget_cannot_replace_bound_authority(self):
        accepted = self._accepted()
        with TemporaryDirectory() as directory:
            first_path = f"{directory}/first.sqlite3"
            second_path = f"{directory}/second.sqlite3"
            first, durable = self._store(first_path)
            second = JournalStore(second_path)
            replacement = DurableCorporateActionEvidenceStore(
                second,
                provider_id="OTHER_PROVIDER",
                account_id="other-account",
                environment="LIVE",
            )
            first_aggregate_id = durable.aggregate_id
            second_aggregate_id = replacement.aggregate_id

            for name in (
                "_store_identity",
                "store",
                "provider_id",
                "account_id",
                "environment",
                "aggregate_id",
            ):
                vars(durable)[name] = vars(replacement)[name]

            with self.assertRaisesRegex(
                CorporateActionEvidenceConflict,
                "composition was modified",
            ):
                DurableCorporateActionEvidenceStore.record(durable, accepted)

            self.assertEqual(
                JournalStore.load_events(
                    first,
                    "corporate_action_evidence",
                    first_aggregate_id,
                ),
                [],
            )
            self.assertEqual(
                JournalStore.load_events(
                    second,
                    "corporate_action_evidence",
                    second_aggregate_id,
                ),
                [],
            )

    def test_durable_scope_binding_exposes_no_erasable_weakref_callback(self):
        with TemporaryDirectory() as directory:
            _, durable = self._store(f"{directory}/journal.sqlite3")
            registry_refs = weakref.getweakrefs(durable)
            self.assertTrue(registry_refs)
            self.assertTrue(
                all(ref.__callback__ is None for ref in registry_refs)
            )

    def test_explicit_reinit_cannot_retarget_durable_evidence_scope(self):
        accepted = self._accepted()
        with TemporaryDirectory() as directory:
            first_path = f"{directory}/first.sqlite3"
            second_path = f"{directory}/second.sqlite3"
            first, durable = self._store(first_path)
            second = JournalStore(second_path)

            with self.assertRaisesRegex(
                CorporateActionEvidenceConflict,
                "already initialized",
            ):
                DurableCorporateActionEvidenceStore.__init__(
                    durable,
                    second,
                    provider_id="OTHER_PROVIDER",
                    account_id="other-account",
                    environment="LIVE",
                )

            self.assertIs(durable.store, first)
            result = DurableCorporateActionEvidenceStore.record(
                durable,
                accepted,
            )
            self.assertTrue(result.inserted)

    def test_durable_evidence_store_subclass_is_rejected_at_construction(self):
        class ForgedEvidenceStore(DurableCorporateActionEvidenceStore):
            pass

        with TemporaryDirectory() as directory:
            journal = JournalStore(f"{directory}/journal.sqlite3")
            with self.assertRaisesRegex(TypeError, "exact canonical type"):
                ForgedEvidenceStore(
                    journal,
                    provider_id="BINANCE",
                    account_id="acct-1",
                    environment="SIMULATION",
                )



if __name__ == "__main__":
    unittest.main()
