from dataclasses import fields
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from research.autotrade_research.artifacts.store import ArtifactStore

from mvp.autotrade_mvp import instruments as instruments_module
from mvp.autotrade_mvp.authority import RiskAuthorityRequest
from mvp.autotrade_mvp.instruments import (
    InstrumentNotFound,
    InstrumentRegistry,
    InstrumentRegistryError,
    AuthenticatedPriceSemanticsEvidence,
    authenticated_price_semantics_digest,
    authenticated_price_semantics_evidence,
)
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.durable_financial_request_binding import (
    _require_admitted_price_semantics,
)
from mvp.tests.test_financial_send_authority import D7, binding
from mvp.tests.test_instruments import (
    A,
    B,
    publish_metadata_evidence,
    spot,
    when,
)
from mvp.tests.test_resolved_policy_financial_composition import (
    issue,
    request,
    snapshot,
)


class _HostileText(str):
    callbacks = 0

    def strip(self, *_args, **_kwargs):
        type(self).callbacks += 1
        raise AssertionError("hostile text strip executed")

    def upper(self):
        type(self).callbacks += 1
        raise AssertionError("hostile text upper executed")


class AuthenticatedInstrumentPriceSemanticsTests(unittest.TestCase):
    def _registry_with_evidence(self, directory: str):
        artifact_store = ArtifactStore(Path(directory) / "artifacts")
        evidence = publish_metadata_evidence(
            artifact_store,
            when(1),
            version=spot(),
            artifact_id=B,
        )
        registry = InstrumentRegistry()
        registry.add(spot(metadata_evidence=(evidence,)))
        return registry, artifact_store

    def _digest(self, registry, artifact_store, **updates):
        values = {
            "instrument_version": f"{A}@1",
            "evaluated_at": when(2),
            "provider_id": "SIMULATED",
            "entity_policy_id": "linear-order-v1",
            "side": "BUY",
            "order_type": "LIMIT",
            "price": "100.00",
        }
        values.update(updates)
        return authenticated_price_semantics_digest(
            registry,
            artifact_store,
            **values,
        )

    def test_limit_digest_is_causal_deterministic_and_instrument_owned(self):
        with TemporaryDirectory() as directory:
            registry, artifact_store = self._registry_with_evidence(directory)
            first = self._digest(registry, artifact_store)
            second = self._digest(registry, artifact_store)

            self.assertEqual(first, second)
            self.assertRegex(first, r"^sha256:[0-9a-f]{64}$")

            changed_policy = self._digest(
                registry,
                artifact_store,
                entity_policy_id="linear-order-v2",
            )
            changed_side = self._digest(
                registry,
                artifact_store,
                side="SELL",
            )
            self.assertNotEqual(first, changed_policy)
            self.assertNotEqual(first, changed_side)

    def test_evidence_pairs_digest_with_exact_authenticated_instrument_binding(self):
        with TemporaryDirectory() as directory:
            registry, artifact_store = self._registry_with_evidence(directory)
            evidence = authenticated_price_semantics_evidence(
                registry,
                artifact_store,
                instrument_version=f"{A}@1",
                evaluated_at=when(2),
                provider_id="SIMULATED",
                entity_policy_id="linear-order-v1",
                side="BUY",
                order_type="LIMIT",
                price="100.00",
            )

            self.assertIs(type(evidence), AuthenticatedPriceSemanticsEvidence)
            self.assertEqual(evidence.digest, self._digest(registry, artifact_store))
            self.assertEqual(evidence.instrument_version, f"{A}@1")
            self.assertEqual(
                evidence.instrument_metadata_binding,
                registry.exact(f"{A}@1").metadata_evidence_binding(),
            )
            self.assertEqual(evidence.provider_symbol, "ABC")
            self.assertEqual(evidence.price_constraint, "EXACT_ADMITTED_PRICE")

    def test_composer_authorities_are_closure_owned_against_coherent_module_rebinding(self):
        with TemporaryDirectory() as directory:
            registry, artifact_store = self._registry_with_evidence(directory)

            class ForgedRegistry:
                callbacks = 0

                @property
                def at_known(self):
                    type(self).callbacks += 1
                    raise AssertionError("forged registry callback executed")

            with patch.object(instruments_module, "InstrumentRegistry", ForgedRegistry()):
                with self.assertRaisesRegex(
                    InstrumentRegistryError,
                    "price-semantics executable authority changed",
                ):
                    self._digest(registry, artifact_store)
            self.assertEqual(ForgedRegistry.callbacks, 0)

            class ForgedArtifactStore:
                pass

            with patch.object(instruments_module, "ArtifactStore", ForgedArtifactStore):
                with self.assertRaisesRegex(
                    InstrumentRegistryError,
                    "price-semantics executable authority changed",
                ):
                    self._digest(registry, artifact_store)

    def test_digest_wrapper_does_not_delegate_to_rebindable_public_composer(self):
        with TemporaryDirectory() as directory:
            registry, artifact_store = self._registry_with_evidence(directory)
            calls = []

            def forged(*_args, **_kwargs):
                calls.append("forged")
                raise AssertionError("rebound public composer executed")

            with patch.object(
                instruments_module,
                "authenticated_price_semantics_evidence",
                forged,
            ):
                digest = self._digest(registry, artifact_store)
            self.assertRegex(digest, r"^sha256:[0-9a-f]{64}$")
            self.assertEqual(calls, [])

    def test_limit_price_is_validated_exactly_and_never_rounded(self):
        with TemporaryDirectory() as directory:
            registry, artifact_store = self._registry_with_evidence(directory)
            with self.assertRaisesRegex(
                InstrumentRegistryError,
                "price is not aligned to price_tick",
            ):
                self._digest(
                    registry,
                    artifact_store,
                    price="100.005",
                )

    def test_security_boundary_rejects_polymorphic_text_and_float_price(self):
        with TemporaryDirectory() as directory:
            registry, artifact_store = self._registry_with_evidence(directory)
            _HostileText.callbacks = 0
            with self.assertRaisesRegex(TypeError, "provider_id must be exact text"):
                self._digest(
                    registry,
                    artifact_store,
                    provider_id=_HostileText("SIMULATED"),
                )
            self.assertEqual(_HostileText.callbacks, 0)

            with self.assertRaisesRegex(TypeError, "LIMIT price must use exact"):
                self._digest(
                    registry,
                    artifact_store,
                    price=100.0,
                )

    def test_mutated_registry_rule_cannot_escape_metadata_reauthentication(self):
        with TemporaryDirectory() as directory:
            registry, artifact_store = self._registry_with_evidence(directory)
            selected = registry.exact(f"{A}@1")
            object.__setattr__(selected, "price_tick", Decimal("0.10"))

            with self.assertRaisesRegex(
                InstrumentRegistryError,
                "not bound to this instrument version",
            ):
                self._digest(registry, artifact_store)

    def test_metadata_must_be_causally_authenticated_before_digest_mint(self):
        with TemporaryDirectory() as directory:
            artifact_store = ArtifactStore(Path(directory) / "artifacts")
            registry = InstrumentRegistry()
            registry.add(spot())

            with self.assertRaisesRegex(InstrumentNotFound, "causally known"):
                self._digest(registry, artifact_store)

    def test_exact_admitted_instrument_version_and_provider_are_cross_bound(self):
        with TemporaryDirectory() as directory:
            registry, artifact_store = self._registry_with_evidence(directory)
            with self.assertRaisesRegex(
                InstrumentRegistryError,
                "differs from admitted instrument_version",
            ):
                self._digest(
                    registry,
                    artifact_store,
                    instrument_version=f"{A}@2",
                )
            with self.assertRaisesRegex(
                InstrumentRegistryError,
                "provider differs from financial provider",
            ):
                self._digest(
                    registry,
                    artifact_store,
                    provider_id="BYBIT",
                )

    def test_provider_symbol_is_cross_bound_when_required(self):
        with TemporaryDirectory() as directory:
            registry, artifact_store = self._registry_with_evidence(directory)
            with self.assertRaisesRegex(
                InstrumentRegistryError,
                "symbol differs from provider request",
            ):
                self._digest(
                    registry,
                    artifact_store,
                    provider_symbol="OTHER",
                )

    def test_market_has_distinct_no_wire_price_semantics(self):
        with TemporaryDirectory() as directory:
            registry, artifact_store = self._registry_with_evidence(directory)
            limit_digest = self._digest(registry, artifact_store)
            market_digest = self._digest(
                registry,
                artifact_store,
                order_type="MARKET",
                price=None,
            )
            self.assertNotEqual(limit_digest, market_digest)

            with self.assertRaisesRegex(
                InstrumentRegistryError,
                "require price to be absent",
            ):
                self._digest(
                    registry,
                    artifact_store,
                    order_type="MARKET",
                    price="100.00",
                )

    def test_unsupported_order_type_has_no_implicit_semantics(self):
        with TemporaryDirectory() as directory:
            registry, artifact_store = self._registry_with_evidence(directory)
            with self.assertRaisesRegex(
                InstrumentRegistryError,
                "no canonical financial price-semantics contract",
            ):
                self._digest(
                    registry,
                    artifact_store,
                    order_type="STOP",
                )

    def test_causal_lookup_rebinding_fails_before_forged_execution(self):
        with TemporaryDirectory() as directory:
            registry, artifact_store = self._registry_with_evidence(directory)

            def forged(*_args, **_kwargs):
                raise AssertionError("forged at_known executed")

            with patch.object(InstrumentRegistry, "at_known", forged):
                with self.assertRaisesRegex(
                    InstrumentRegistryError,
                    "price-semantics executable authority changed",
                ):
                    self._digest(registry, artifact_store)

    def test_price_grid_helper_rebinding_fails_before_forged_execution(self):
        with TemporaryDirectory() as directory:
            registry, artifact_store = self._registry_with_evidence(directory)

            def forged(*_args, **_kwargs):
                raise AssertionError("forged price-grid helper executed")

            for name in ("_decimal", "_is_exact_multiple"):
                with self.subTest(name=name):
                    with patch.object(instruments_module, name, forged):
                        with self.assertRaisesRegex(
                            InstrumentRegistryError,
                            "price-semantics executable authority changed",
                        ):
                            self._digest(registry, artifact_store)

    def test_exact_decimal_primitive_code_mutation_fails_before_execution(self):
        with TemporaryDirectory() as directory:
            registry, artifact_store = self._registry_with_evidence(directory)
            targets = (
                instruments_module.parse_bounded_exact_decimal,
                instruments_module.is_exact_decimal_multiple,
            )

            for target in targets:
                with self.subTest(target=target.__name__):
                    original_code = target.__code__

                    def forged(*_args, **_kwargs):
                        raise AssertionError("forged exact-decimal primitive executed")

                    self.assertEqual(
                        len(original_code.co_freevars),
                        len(forged.__code__.co_freevars),
                    )
                    try:
                        target.__code__ = forged.__code__
                        with self.assertRaisesRegex(
                            InstrumentRegistryError,
                            "price-semantics executable authority changed",
                        ):
                            self._digest(registry, artifact_store)
                    finally:
                        target.__code__ = original_code

    def test_transitive_serializer_helper_rebinding_fails_before_execution(self):
        with TemporaryDirectory() as directory:
            registry, artifact_store = self._registry_with_evidence(directory)
            callbacks = []

            def forged(*_args, **_kwargs):
                callbacks.append(True)
                raise AssertionError("forged transitive serializer helper executed")

            for name in ("_decimal_text", "_utc_text", "_thaw_jsonish"):
                with self.subTest(name=name):
                    with patch.object(instruments_module, name, forged):
                        with self.assertRaisesRegex(
                            InstrumentRegistryError,
                            "price-semantics executable authority changed",
                        ):
                            self._digest(registry, artifact_store)
            self.assertEqual(callbacks, [])

    def test_to_contract_dict_code_mutation_fails_before_execution(self):
        with TemporaryDirectory() as directory:
            registry, artifact_store = self._registry_with_evidence(directory)
            target = instruments_module.InstrumentVersion.to_contract_dict
            original_code = target.__code__

            def forged(_self):
                raise AssertionError("forged InstrumentVersion serializer executed")

            self.assertEqual(
                len(original_code.co_freevars),
                len(forged.__code__.co_freevars),
            )
            try:
                target.__code__ = forged.__code__
                with self.assertRaisesRegex(
                    InstrumentRegistryError,
                    "price-semantics executable authority changed",
                ):
                    self._digest(registry, artifact_store)
            finally:
                target.__code__ = original_code

    def test_detachment_fields_and_constructor_rebinding_fail_before_execution(self):
        with TemporaryDirectory() as directory:
            registry, artifact_store = self._registry_with_evidence(directory)
            callbacks = []

            def forged_fields(*_args, **_kwargs):
                callbacks.append("fields")
                raise AssertionError("forged dataclass fields executed")

            with patch.object(instruments_module, "fields", forged_fields):
                with self.assertRaisesRegex(
                    InstrumentRegistryError,
                    "price-semantics executable authority changed",
                ):
                    self._digest(registry, artifact_store)

            original_init = instruments_module.InstrumentVersion.__init__

            def forged_init(_self, *_args, **_kwargs):
                callbacks.append("init")
                raise AssertionError("forged InstrumentVersion constructor executed")

            with patch.object(
                instruments_module.InstrumentVersion,
                "__init__",
                forged_init,
            ):
                with self.assertRaisesRegex(
                    InstrumentRegistryError,
                    "price-semantics executable authority changed",
                ):
                    self._digest(registry, artifact_store)

            self.assertEqual(callbacks, [])
            self.assertIs(
                instruments_module.InstrumentVersion.__init__,
                original_init,
            )

    def test_instrument_version_class_rebinding_fails_before_causal_execution(self):
        with TemporaryDirectory() as directory:
            registry, artifact_store = self._registry_with_evidence(directory)
            original = instruments_module.InstrumentVersion

            class ForgedInstrumentVersion:
                validate_price = original.validate_price
                metadata_evidence_binding = original.metadata_evidence_binding
                to_contract_dict = original.to_contract_dict
                __init__ = original.__init__

            with patch.object(
                instruments_module,
                "InstrumentVersion",
                ForgedInstrumentVersion,
            ):
                with self.assertRaisesRegex(
                    InstrumentRegistryError,
                    "price-semantics executable authority changed",
                ):
                    self._digest(registry, artifact_store)
            self.assertIs(instruments_module.InstrumentVersion, original)

    def test_settlement_convention_payload_and_class_rebinding_fail_before_execution(self):
        with TemporaryDirectory() as directory:
            registry, artifact_store = self._registry_with_evidence(directory)
            callbacks = []
            original_type = instruments_module.SettlementConvention

            def forged_payload(_self):
                callbacks.append("payload")
                raise AssertionError("forged settlement payload executed")

            with patch.object(original_type, "payload", forged_payload):
                with self.assertRaisesRegex(
                    InstrumentRegistryError,
                    "price-semantics executable authority changed",
                ):
                    self._digest(registry, artifact_store)

            class ForgedSettlementConvention:
                payload = original_type.payload

            with patch.object(
                instruments_module,
                "SettlementConvention",
                ForgedSettlementConvention,
            ):
                with self.assertRaisesRegex(
                    InstrumentRegistryError,
                    "price-semantics executable authority changed",
                ):
                    self._digest(registry, artifact_store)

            self.assertEqual(callbacks, [])
            self.assertIs(instruments_module.SettlementConvention, original_type)

    def test_transitive_serializer_global_rebinding_fails_before_execution(self):
        with TemporaryDirectory() as directory:
            registry, artifact_store = self._registry_with_evidence(directory)
            callbacks = []

            def forged(*_args, **_kwargs):
                callbacks.append(True)
                raise AssertionError("forged transitive serializer global executed")

            replacements = (
                ("InstrumentRegistryError", RuntimeError, False),
                ("canonical_decimal_text", forged, False),
                ("ExactDecimalError", RuntimeError, False),
                ("timezone", object(), False),
                ("Mapping", object(), False),
                ("MappingProxyType", object(), False),
                ("DeliverableLeg", object(), False),
                ("dict", forged, True),
                ("str", forged, True),
                ("type", forged, True),
                ("int", forged, True),
                ("tuple", forged, True),
                ("getattr", forged, True),
                ("any", forged, True),
                ("isinstance", forged, True),
                ("InvalidOperation", RuntimeError, False),
                ("ValueError", RuntimeError, True),
                ("TypeError", RuntimeError, True),
                ("bool", forged, True),
                ("float", forged, True),
            )
            for name, replacement, create in replacements:
                with self.subTest(name=name):
                    with patch.object(
                        instruments_module,
                        name,
                        replacement,
                        create=create,
                    ):
                        with self.assertRaisesRegex(
                            InstrumentRegistryError,
                            "price-semantics executable authority changed",
                        ):
                            self._digest(registry, artifact_store)

            self.assertEqual(callbacks, [])

    def test_snapshot_binds_price_semantics_without_putting_it_in_request(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            resolved = issue(store)
            req = request(resolved)
            self.assertIsInstance(req, RiskAuthorityRequest)
            self.assertNotIn(
                "price_semantics_digest",
                {field.name for field in fields(RiskAuthorityRequest)},
            )

            baseline = snapshot(req)
            refs = dict(baseline.evidence_refs.items())
            refs["INSTRUMENT"] = "sha256:" + "9" * 64
            bound = snapshot(
                req,
                price_semantics_digest=D7,
                evidence_refs=refs,
            )

            self.assertNotEqual(baseline.snapshot_id, bound.snapshot_id)
            self.assertEqual(
                bound.evidence_payload()["price_semantics_digest"],
                D7,
            )
            self.assertEqual(
                _require_admitted_price_semantics(
                    bound.evidence_payload(),
                    binding(),
                ),
                D7,
            )

    def test_snapshot_price_semantics_requires_instrument_evidence_dimension(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            req = request(issue(store))
            with self.assertRaisesRegex(
                ValueError,
                "missing evidence dimensions: INSTRUMENT",
            ):
                snapshot(req, price_semantics_digest=D7)

    def test_snapshot_rejects_noncanonical_instrument_evidence_binding(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            req = request(issue(store))
            base = snapshot(req)
            refs = dict(base.evidence_refs.items())
            refs["INSTRUMENT"] = "instrument-metadata-binding"
            with self.assertRaisesRegex(
                ValueError,
                "INSTRUMENT evidence must be canonical",
            ):
                snapshot(
                    req,
                    price_semantics_digest=D7,
                    evidence_refs=refs,
                )

    def test_snapshot_rejects_noncanonical_price_semantics_digest(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            req = request(issue(store))
            base = snapshot(req)
            refs = dict(base.evidence_refs.items())
            refs["INSTRUMENT"] = "instrument-metadata-binding"
            with self.assertRaisesRegex(
                ValueError,
                "price_semantics_digest must be sha256",
            ):
                snapshot(
                    req,
                    price_semantics_digest="sha256:" + "A" * 64,
                    evidence_refs=refs,
                )


if __name__ == "__main__":
    unittest.main()
