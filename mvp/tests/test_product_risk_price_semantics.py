from dataclasses import replace
from datetime import timedelta
import gc
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from weakref import ref as weakref_ref

from research.autotrade_research.artifacts.store import ArtifactStore

from mvp.autotrade_mvp import bybit_v5 as bybit_module
from mvp.autotrade_mvp import instruments as instruments_module
from mvp.autotrade_mvp import product_risk_price_semantics as price_semantics_module
from mvp.autotrade_mvp.authority import (
    AuthoritativeRiskSnapshot,
    AuthorityConflict,
    AuthorityService,
    RiskAuthorityRequest,
)
from mvp.autotrade_mvp.bybit_v5 import (
    BybitPreparedSubmission,
    prepare_order_submission,
)
from mvp.autotrade_mvp.instruments import InstrumentRegistry, InstrumentRegistryError
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.provider_core import ProviderCoreError
from mvp.autotrade_mvp.product_risk_price_semantics import (
    ProductRiskPriceSemanticsBinding,
    ProductRiskPriceSemanticsComposer,
    ProductRiskPriceSemanticsError,
)
from mvp.autotrade_mvp.risk import RiskContext, RiskIntent
from mvp.autotrade_mvp.risk_policy_authority import (
    DurableRiskPolicyRegistry,
    RiskPolicyScope,
)
from mvp.autotrade_mvp.simulation_session import _risk_policy
from mvp.tests.test_bybit_v5 import READ_AT, submission_write_capability
from mvp.tests.test_instruments import A, B, publish_metadata_evidence, spot


ENTITY_POLICY = "linear-order-v1"
SCOPE = RiskPolicyScope(
    "BYBIT",
    "bybit-account",
    "PAPER",
    "DEMO",
    ENTITY_POLICY,
    "EQUITY",
)


def _utc_text(point):
    return point.isoformat().replace("+00:00", "Z")


class ProductRiskPriceSemanticsCompositionTests(unittest.TestCase):
    def _authorities(self, directory):
        store = JournalStore(Path(directory) / "journal.sqlite3")
        policy_registry = DurableRiskPolicyRegistry(store)
        committed = READ_AT - timedelta(minutes=3)
        policy_registry.register(
            scope=SCOPE,
            policy_id="quantitative",
            version=1,
            policy=_risk_policy(),
            committed_at=committed,
        )
        policy_registry.activate(
            scope=SCOPE,
            policy_id="quantitative",
            version=1,
            committed_at=committed + timedelta(seconds=1),
        )
        resolved = policy_registry.resolve_current(SCOPE)

        artifact_store = ArtifactStore(Path(directory) / "artifacts")
        raw = replace(
            spot(symbol="BTCUSDT", venue_id="bybit"),
            provider_id="BYBIT",
            provider_symbol="BTCUSDT",
        )
        evidence = publish_metadata_evidence(
            artifact_store,
            READ_AT - timedelta(minutes=2),
            version=raw,
            artifact_id=B,
        )
        registry = InstrumentRegistry()
        registry.add(replace(raw, metadata_evidence=(evidence,)))
        return store, resolved, registry, artifact_store

    def _prepared(
        self,
        *,
        symbol="BTCUSDT",
        side="BUY",
        quantity="1",
        order_type="LIMIT",
        price="100.00",
        capability=None,
    ):
        cap = capability or submission_write_capability(
            account_id="bybit-account",
            environment="PAPER",
            instrument_version=f"{A}@1",
            provider_environment="DEMO",
        )
        return cap, prepare_order_submission(
            capability=cap,
            at=READ_AT,
            provider_environment=cap.provider_environment,
            product_family="SPOT",
            symbol=symbol,
            side=side,
            order_type=order_type,
            quantity=quantity,
            client_order_id="risk-price-composer-1",
            time_in_force="GTC" if order_type == "LIMIT" else "IOC",
            price=price if order_type == "LIMIT" else None,
        )

    def _request(
        self,
        resolved,
        capability,
        *,
        symbol="BTCUSDT",
        side="BUY",
        quantity="1",
        price="100.00",
        reduce_only=False,
    ):
        return RiskAuthorityRequest(
            risk_intent=RiskIntent.create(
                symbol=symbol,
                side=side,
                quantity=quantity,
                price=price,
                expected_state_version=1,
                reduce_only=reduce_only,
            ),
            account_id="bybit-account",
            environment="PAPER",
            provider_id="BYBIT",
            instrument_version=(A, 1),
            capability_snapshot_id=capability.snapshot_id,
            reconciliation_checkpoint_event_id="reconciled",
            journal_sequence_cut=resolved.resolved_journal_sequence_cut,
            reservation_version=0,
            reservation_state_digest="reservation-state",
            authority_policy_id="authority",
            authority_policy_version=1,
            evaluated_at=_utc_text(READ_AT),
            resolved_risk_policy=resolved,
            provider_environment="DEMO",
            entity_policy_id=ENTITY_POLICY,
            instrument_family="EQUITY",
        )

    def _snapshot(self, request, resolved):
        return AuthoritativeRiskSnapshot(
            context=RiskContext.create(
                state_version=1,
                equity="1000",
                positions={},
                marks={"BTCUSDT": "100"},
                margin_headroom="1",
                capability_allowed=True,
                borrow_available=True,
            ),
            risk_policy=resolved.policy,
            account_id=request.account_id,
            environment=request.environment,
            provider_id=request.provider_id,
            instrument_version=request.instrument_version,
            capability_snapshot_id=request.capability_snapshot_id,
            reconciliation_checkpoint_event_id=(
                request.reconciliation_checkpoint_event_id
            ),
            journal_sequence_cut=request.journal_sequence_cut,
            reservation_version=request.reservation_version,
            reservation_state_digest=request.reservation_state_digest,
            authority_policy_id=request.authority_policy_id,
            authority_policy_version=request.authority_policy_version,
            evaluated_at=request.evaluated_at,
            valid_until=_utc_text(READ_AT + timedelta(minutes=1)),
            evidence_refs={
                "PORTFOLIO": "account-cut",
                "MARKET": "market-cut",
                "MARGIN": "margin-cut",
                "POLICY": resolved.registration_event_id,
                "RECONCILIATION": request.reconciliation_checkpoint_event_id,
                "CAPABILITY": request.capability_snapshot_id,
                "BORROW": "borrow-cut",
            },
            resolved_risk_policy=resolved,
            provider_environment=request.provider_environment,
            entity_policy_id=request.entity_policy_id,
            instrument_family=request.instrument_family,
        )

    def test_limit_composition_binds_prepared_request_to_causal_instrument(self):
        with TemporaryDirectory() as directory:
            _, resolved, registry, artifacts = self._authorities(directory)
            capability, prepared = self._prepared()
            request = self._request(resolved, capability)
            composer = ProductRiskPriceSemanticsComposer(registry, artifacts)

            binding = composer.compose(request, prepared)

            self.assertEqual(binding.order_type, "LIMIT")
            self.assertEqual(binding.prepared_body_sha256, prepared.body_sha256)
            self.assertEqual(binding.instrument_version, f"{A}@1")
            self.assertRegex(binding.price_semantics_digest, r"^sha256:[0-9a-f]{64}$")
            self.assertRegex(
                binding.instrument_evidence_binding,
                r"^sha256:[0-9a-f]{64}$",
            )

    def test_market_has_distinct_no_wire_price_semantics_without_erasing_risk_price(self):
        with TemporaryDirectory() as directory:
            _, resolved, registry, artifacts = self._authorities(directory)
            capability, limit_prepared = self._prepared()
            request = self._request(resolved, capability)
            composer = ProductRiskPriceSemanticsComposer(registry, artifacts)
            limit_binding = composer.compose(request, limit_prepared)

            _, market_prepared = self._prepared(
                order_type="MARKET",
                price=None,
                capability=capability,
            )
            market_binding = composer.compose(request, market_prepared)

            self.assertEqual(market_binding.order_type, "MARKET")
            self.assertEqual(market_binding.risk_price, request.risk_intent.price)
            self.assertNotEqual(
                market_binding.price_semantics_digest,
                limit_binding.price_semantics_digest,
            )
            self.assertNotIn("price", market_prepared.body)

    def test_limit_wire_price_must_equal_already_risked_price(self):
        with TemporaryDirectory() as directory:
            _, resolved, registry, artifacts = self._authorities(directory)
            capability, prepared = self._prepared(price="101.00")
            request = self._request(resolved, capability, price="100.00")
            composer = ProductRiskPriceSemanticsComposer(registry, artifacts)

            with self.assertRaisesRegex(
                ProductRiskPriceSemanticsError,
                "already-risked price",
            ):
                composer.compose(request, prepared)

    def test_side_and_quantity_substitution_fail_before_binding(self):
        with TemporaryDirectory() as directory:
            _, resolved, registry, artifacts = self._authorities(directory)
            composer = ProductRiskPriceSemanticsComposer(registry, artifacts)
            for updates, message in (
                ({"side": "SELL"}, "side differs"),
                ({"quantity": "2"}, "quantity differs"),
            ):
                with self.subTest(updates=updates):
                    capability, prepared = self._prepared(**updates)
                    request = self._request(resolved, capability)
                    with self.assertRaisesRegex(
                        ProductRiskPriceSemanticsError,
                        message,
                    ):
                        composer.compose(request, prepared)

    def test_quantity_must_already_match_causal_instrument_grid(self):
        with TemporaryDirectory() as directory:
            _, resolved, registry, artifacts = self._authorities(directory)
            capability, prepared = self._prepared(quantity="0.0005")
            request = self._request(
                resolved,
                capability,
                quantity="0.0005",
            )
            composer = ProductRiskPriceSemanticsComposer(registry, artifacts)

            with self.assertRaisesRegex(
                InstrumentRegistryError,
                "minimum_quantity|quantity_step",
            ):
                composer.compose(request, prepared)

    def test_prepared_symbol_must_match_causal_instrument_symbol(self):
        with TemporaryDirectory() as directory:
            _, resolved, registry, artifacts = self._authorities(directory)
            capability, prepared = self._prepared(symbol="ETHUSDT")
            request = self._request(
                resolved,
                capability,
                symbol="ETHUSDT",
            )
            composer = ProductRiskPriceSemanticsComposer(registry, artifacts)

            with self.assertRaisesRegex(
                ProductRiskPriceSemanticsError,
                "causal instrument authority",
            ):
                composer.compose(request, prepared)

    def test_provider_domain_capability_and_instrument_are_cross_bound(self):
        with TemporaryDirectory() as directory:
            _, resolved, registry, artifacts = self._authorities(directory)
            capability, prepared = self._prepared()
            request = self._request(resolved, capability)
            composer = ProductRiskPriceSemanticsComposer(registry, artifacts)

            other_capability, other_account_prepared = self._prepared(
                capability=submission_write_capability(
                    account_id="other-account",
                    environment="PAPER",
                    instrument_version=f"{A}@1",
                    provider_environment="DEMO",
                )
            )
            request_for_other_capability = replace(
                request,
                capability_snapshot_id=other_capability.snapshot_id,
            )
            with self.assertRaisesRegex(
                ProductRiskPriceSemanticsError,
                "account differs",
            ):
                composer.compose(
                    request_for_other_capability,
                    other_account_prepared,
                )

            testnet_capability = submission_write_capability(
                account_id="bybit-account",
                environment="PAPER",
                instrument_version=f"{A}@1",
                provider_environment="TESTNET",
            )
            _, testnet_prepared = self._prepared(capability=testnet_capability)
            request_for_testnet_capability = replace(
                request,
                capability_snapshot_id=testnet_capability.snapshot_id,
            )
            with self.assertRaisesRegex(
                ProductRiskPriceSemanticsError,
                "provider environment differs",
            ):
                composer.compose(
                    request_for_testnet_capability,
                    testnet_prepared,
                )

            changed_capability = replace(
                request,
                capability_snapshot_id="other-capability",
            )
            with self.assertRaisesRegex(
                ProductRiskPriceSemanticsError,
                "capability differs",
            ):
                composer.compose(changed_capability, prepared)

            other_version = replace(request, instrument_version=(A, 2))
            with self.assertRaisesRegex(
                ProductRiskPriceSemanticsError,
                "instrument version differs",
            ):
                composer.compose(other_version, prepared)

    def test_unissued_prepared_clone_cannot_mint_risk_price_authority(self):
        with TemporaryDirectory() as directory:
            _, resolved, registry, artifacts = self._authorities(directory)
            capability, prepared = self._prepared()
            request = self._request(resolved, capability)
            forged = object.__new__(BybitPreparedSubmission)
            for name in (
                "endpoint",
                "body",
                "account_id",
                "environment",
                "provider_environment",
                "capability_snapshot_id",
                "entity_id",
                "instrument_version",
                "body_sha256",
            ):
                object.__setattr__(
                    forged,
                    name,
                    object.__getattribute__(prepared, name),
                )

            with self.assertRaisesRegex(
                ProviderCoreError,
                "prepared submission authority changed",
            ):
                ProductRiskPriceSemanticsComposer(
                    registry,
                    artifacts,
                ).compose(request, forged)

    def test_binding_attaches_digest_and_canonical_instrument_evidence_to_snapshot(self):
        with TemporaryDirectory() as directory:
            _, resolved, registry, artifacts = self._authorities(directory)
            capability, prepared = self._prepared()
            request = self._request(resolved, capability)
            base = self._snapshot(request, resolved)
            composer = ProductRiskPriceSemanticsComposer(registry, artifacts)
            binding = composer.compose(request, prepared)

            bound = composer.bind_snapshot(request, base, binding, prepared)

            self.assertNotEqual(bound.snapshot_id, base.snapshot_id)
            self.assertEqual(
                bound.price_semantics_digest,
                binding.price_semantics_digest,
            )
            self.assertEqual(
                bound.evidence_refs["INSTRUMENT"],
                binding.instrument_evidence_binding,
            )

    def test_replaced_binding_cannot_forge_authenticated_digest(self):
        with TemporaryDirectory() as directory:
            _, resolved, registry, artifacts = self._authorities(directory)
            capability, prepared = self._prepared()
            request = self._request(resolved, capability)
            base = self._snapshot(request, resolved)
            composer = ProductRiskPriceSemanticsComposer(registry, artifacts)
            binding = composer.compose(request, prepared)
            forged = replace(
                binding,
                price_semantics_digest="sha256:" + "0" * 64,
            )

            with self.assertRaisesRegex(
                ProductRiskPriceSemanticsError,
                "binding authority changed",
            ):
                composer.bind_snapshot(request, base, forged, prepared)

    def test_post_issue_binding_mutation_fails_closed(self):
        with TemporaryDirectory() as directory:
            _, resolved, registry, artifacts = self._authorities(directory)
            capability, prepared = self._prepared()
            request = self._request(resolved, capability)
            base = self._snapshot(request, resolved)
            composer = ProductRiskPriceSemanticsComposer(registry, artifacts)
            binding = composer.compose(request, prepared)
            object.__setattr__(
                binding,
                "instrument_evidence_binding",
                "sha256:" + "1" * 64,
            )

            with self.assertRaisesRegex(
                ProductRiskPriceSemanticsError,
                "binding authority changed",
            ):
                composer.bind_snapshot(request, base, binding, prepared)

    def test_binding_is_scoped_to_issuing_composer(self):
        with TemporaryDirectory() as directory:
            _, resolved, registry, artifacts = self._authorities(directory)
            capability, prepared = self._prepared()
            request = self._request(resolved, capability)
            base = self._snapshot(request, resolved)
            issuer = ProductRiskPriceSemanticsComposer(registry, artifacts)
            other = ProductRiskPriceSemanticsComposer(registry, artifacts)
            binding = issuer.compose(request, prepared)

            with self.assertRaisesRegex(
                ProductRiskPriceSemanticsError,
                "binding authority changed",
            ):
                other.bind_snapshot(request, base, binding, prepared)

    def test_binding_getattribute_rebinding_fails_before_execution(self):
        with TemporaryDirectory() as directory:
            _, resolved, registry, artifacts = self._authorities(directory)
            capability, prepared = self._prepared()
            request = self._request(resolved, capability)
            base = self._snapshot(request, resolved)
            composer = ProductRiskPriceSemanticsComposer(registry, artifacts)
            binding = composer.compose(request, prepared)
            callbacks = []

            def forged_getattribute(_self, _name):
                callbacks.append(True)
                raise AssertionError("forged binding attribute reader executed")

            with patch.object(
                ProductRiskPriceSemanticsBinding,
                "__getattribute__",
                forged_getattribute,
            ):
                with self.assertRaisesRegex(
                    ProductRiskPriceSemanticsError,
                    "binding authority changed",
                ):
                    composer.bind_snapshot(request, base, binding, prepared)
            self.assertEqual(callbacks, [])

    def test_same_body_different_canonical_preparation_cannot_rebind_snapshot(self):
        with TemporaryDirectory() as directory:
            _, resolved, registry, artifacts = self._authorities(directory)
            capability, prepared = self._prepared()
            request = self._request(resolved, capability)
            base = self._snapshot(request, resolved)
            composer = ProductRiskPriceSemanticsComposer(registry, artifacts)
            binding = composer.compose(request, prepared)

            other_capability = submission_write_capability(
                account_id="bybit-account",
                environment="PAPER",
                instrument_version=f"{A}@1",
                provider_environment="DEMO",
            )
            _, other_prepared = self._prepared(capability=other_capability)
            self.assertEqual(prepared.body_sha256, other_prepared.body_sha256)
            self.assertNotEqual(
                prepared.capability_snapshot_id,
                other_prepared.capability_snapshot_id,
            )

            with self.assertRaisesRegex(
                ProductRiskPriceSemanticsError,
                "binding authority changed",
            ):
                composer.bind_snapshot(
                    request,
                    base,
                    binding,
                    other_prepared,
                )

    def test_binding_collection_releases_composer_authorities_without_followup_call(self):
        with TemporaryDirectory() as directory:
            _, resolved, registry, artifacts = self._authorities(directory)
            capability, prepared = self._prepared()
            request = self._request(resolved, capability)
            registry_ref = weakref_ref(registry)
            artifacts_ref = weakref_ref(artifacts)
            composer = ProductRiskPriceSemanticsComposer(registry, artifacts)
            binding = composer.compose(request, prepared)

            del binding
            del composer
            del registry
            del artifacts
            gc.collect()

            self.assertIsNone(registry_ref())
            self.assertIsNone(artifacts_ref())

    def test_product_helper_rebinding_fails_before_forged_execution(self):
        with TemporaryDirectory() as directory:
            _, resolved, registry, artifacts = self._authorities(directory)
            capability, prepared = self._prepared()
            request = self._request(resolved, capability)
            composer = ProductRiskPriceSemanticsComposer(registry, artifacts)
            callbacks = []

            def forged(*_args, **_kwargs):
                callbacks.append(True)
                return None

            for name in (
                "_require_module_authority",
                "_instant",
                "_exact_decimal",
                "_instrument_ref",
            ):
                with self.subTest(name=name):
                    with patch.object(price_semantics_module, name, forged):
                        with self.assertRaisesRegex(
                            ProductRiskPriceSemanticsError,
                            "binding authority changed",
                        ):
                            composer.compose(request, prepared)
                    self.assertEqual(callbacks, [])

    def test_snapshot_replace_rebinding_fails_before_forged_execution(self):
        with TemporaryDirectory() as directory:
            _, resolved, registry, artifacts = self._authorities(directory)
            capability, prepared = self._prepared()
            request = self._request(resolved, capability)
            base = self._snapshot(request, resolved)
            composer = ProductRiskPriceSemanticsComposer(registry, artifacts)
            binding = composer.compose(request, prepared)
            callbacks = []

            def forged(*_args, **_kwargs):
                callbacks.append(True)
                return base

            with patch.object(price_semantics_module, "replace", forged):
                with self.assertRaisesRegex(
                    ProductRiskPriceSemanticsError,
                    "binding authority changed",
                ):
                    composer.bind_snapshot(request, base, binding, prepared)
            self.assertEqual(callbacks, [])

    def test_snapshot_binding_rejects_a_different_canonical_prepared_request(self):
        with TemporaryDirectory() as directory:
            _, resolved, registry, artifacts = self._authorities(directory)
            capability, prepared = self._prepared()
            request = self._request(resolved, capability)
            base = self._snapshot(request, resolved)
            composer = ProductRiskPriceSemanticsComposer(registry, artifacts)
            binding = composer.compose(request, prepared)
            _, other_prepared = self._prepared(
                order_type="MARKET",
                price=None,
                capability=capability,
            )

            with self.assertRaisesRegex(
                ProductRiskPriceSemanticsError,
                "prepared request differs",
            ):
                composer.bind_snapshot(
                    request,
                    base,
                    binding,
                    other_prepared,
                )

    def test_conflicting_instrument_evidence_cannot_be_overwritten(self):
        with TemporaryDirectory() as directory:
            _, resolved, registry, artifacts = self._authorities(directory)
            capability, prepared = self._prepared()
            request = self._request(resolved, capability)
            base = self._snapshot(request, resolved)
            refs = dict(base.evidence_refs.items())
            refs["INSTRUMENT"] = "sha256:" + "0" * 64
            base = replace(base, evidence_refs=refs)
            composer = ProductRiskPriceSemanticsComposer(registry, artifacts)
            binding = composer.compose(request, prepared)

            with self.assertRaisesRegex(
                ProductRiskPriceSemanticsError,
                "INSTRUMENT evidence differs",
            ):
                composer.bind_snapshot(request, base, binding, prepared)

    def test_composer_does_not_relax_paper_live_generic_resolver_block(self):
        with TemporaryDirectory() as directory:
            store, resolved, registry, artifacts = self._authorities(directory)
            capability, prepared = self._prepared()
            request = self._request(resolved, capability)
            base = self._snapshot(request, resolved)
            composer = ProductRiskPriceSemanticsComposer(registry, artifacts)
            bound = composer.bind_snapshot(
                request,
                base,
                composer.compose(request, prepared),
                prepared,
            )
            calls = []

            def generic(_request):
                calls.append(True)
                return bound

            service = AuthorityService(
                store,
                risk_policy_scope=SCOPE,
                risk_authority_resolver=generic,
            )
            with self.assertRaisesRegex(
                AuthorityConflict,
                "product-owned authoritative risk resolver",
            ):
                service._resolve_authoritative_risk_snapshot(request)
            self.assertEqual(calls, [])

    def test_public_price_evidence_rebinding_fails_before_callback(self):
        with TemporaryDirectory() as directory:
            _, resolved, registry, artifacts = self._authorities(directory)
            capability, prepared = self._prepared()
            request = self._request(resolved, capability)
            callbacks = []

            def forged(*_args, **_kwargs):
                callbacks.append(True)
                raise AssertionError("forged price evidence executed")

            with patch.object(
                instruments_module,
                "authenticated_price_semantics_evidence",
                forged,
            ):
                with self.assertRaisesRegex(
                    ProductRiskPriceSemanticsError,
                    "instrument price-semantics authority changed",
                ):
                    ProductRiskPriceSemanticsComposer(
                        registry,
                        artifacts,
                    ).compose(request, prepared)
            self.assertEqual(callbacks, [])

    def test_executable_rebinding_fails_closed_before_forged_verifier_runs(self):
        with TemporaryDirectory() as directory:
            _, resolved, registry, artifacts = self._authorities(directory)
            capability, prepared = self._prepared()
            request = self._request(resolved, capability)
            callbacks = []

            def forged(_prepared):
                callbacks.append(True)

            with patch.object(
                bybit_module,
                "require_canonical_bybit_prepared_submission",
                forged,
            ):
                with self.assertRaisesRegex(
                    ProductRiskPriceSemanticsError,
                    "prepared-request authority changed",
                ):
                    ProductRiskPriceSemanticsComposer(
                        registry,
                        artifacts,
                    ).compose(request, prepared)
            self.assertEqual(callbacks, [])


if __name__ == "__main__":
    unittest.main()
