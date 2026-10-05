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
    authenticated_price_semantics_digest,
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
            refs["INSTRUMENT"] = "instrument-metadata-binding"
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
