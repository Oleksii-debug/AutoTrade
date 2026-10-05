from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from research.autotrade_research.artifacts.store import ArtifactStore

from mvp.autotrade_mvp import bybit_risk_price_semantics as bridge_module
from mvp.autotrade_mvp.authority import (
    AuthoritativeRiskSnapshot,
    InstrumentVersionIdentity,
    RiskAuthorityRequest,
)
from mvp.autotrade_mvp.bybit_v5 import prepare_order_submission
from mvp.autotrade_mvp.bybit_risk_price_semantics import (
    BybitRiskPriceSemanticsError,
    compose_bybit_risk_price_semantics,
)
from mvp.autotrade_mvp.instruments import InstrumentRegistry
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.risk import RiskContext, RiskIntent
from mvp.autotrade_mvp.risk_policy_authority import (
    DurableRiskPolicyRegistry,
    RiskPolicyScope,
)
from mvp.autotrade_mvp.simulation_session import _risk_policy
from mvp.tests.test_bybit_v5 import READ_AT, submission_write_capability
from mvp.tests.test_instruments import (
    A,
    B,
    publish_metadata_evidence,
    spot,
)


ENTITY_POLICY = "bybit-spot-order-v1"
SCOPE = RiskPolicyScope(
    "BYBIT",
    "account-1",
    "PAPER",
    "TESTNET",
    ENTITY_POLICY,
    "SPOT",
)


class BybitRiskPriceSemanticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        root = Path(self.temp.name)
        self.artifact_store = ArtifactStore(root / "artifacts")
        base = replace(
            spot(symbol="BTCUSDT", venue_id="bybit"),
            provider_id="BYBIT",
        )
        evidence = publish_metadata_evidence(
            self.artifact_store,
            READ_AT - timedelta(hours=2),
            version=base,
            artifact_id=B,
        )
        self.instrument_registry = InstrumentRegistry()
        self.instrument_registry.add(
            replace(base, metadata_evidence=(evidence,))
        )

        self.store = JournalStore(root / "journal.sqlite3")
        registry = DurableRiskPolicyRegistry(self.store)
        registry.register(
            scope=SCOPE,
            policy_id="quantitative",
            version=1,
            policy=_risk_policy(),
            committed_at=READ_AT - timedelta(minutes=3),
        )
        registry.activate(
            scope=SCOPE,
            policy_id="quantitative",
            version=1,
            committed_at=READ_AT - timedelta(minutes=2),
        )
        self.resolved = registry.resolve_current(SCOPE)

    def tearDown(self):
        self.temp.cleanup()

    def _case(
        self,
        *,
        order_type="LIMIT",
        wire_side="BUY",
        risk_side="BUY",
        wire_price="100.00",
        risk_price="100",
        symbol="BTCUSDT",
    ):
        capability = submission_write_capability(
            account_id="account-1",
            environment="PAPER",
            instrument_version=f"{A}@1",
            provider_environment="TESTNET",
        )
        prepared = prepare_order_submission(
            capability=capability,
            at=READ_AT,
            provider_environment="TESTNET",
            product_family="SPOT",
            symbol=symbol,
            side=wire_side,
            order_type=order_type,
            quantity="2",
            client_order_id="client-order-1",
            time_in_force="GTC" if order_type == "LIMIT" else "IOC",
            **({"price": wire_price} if order_type == "LIMIT" else {}),
        )
        evaluated_at = READ_AT.isoformat().replace("+00:00", "Z")
        intent = RiskIntent.create(
            symbol="BTCUSDT",
            side=risk_side,
            quantity="2",
            price=risk_price,
            expected_state_version=1,
        )
        request = RiskAuthorityRequest(
            risk_intent=intent,
            account_id="account-1",
            environment="PAPER",
            provider_id="BYBIT",
            instrument_version=InstrumentVersionIdentity(A, 1),
            capability_snapshot_id=capability.snapshot_id,
            reconciliation_checkpoint_event_id="reconciliation-1",
            journal_sequence_cut=self.resolved.resolved_journal_sequence_cut,
            reservation_version=1,
            reservation_state_digest="reservation-state-1",
            authority_policy_id="authority-policy",
            authority_policy_version=1,
            evaluated_at=evaluated_at,
            resolved_risk_policy=self.resolved,
            provider_environment="TESTNET",
            entity_policy_id=ENTITY_POLICY,
            instrument_family="SPOT",
        )
        context = RiskContext.create(
            state_version=1,
            equity="1000",
            positions={},
            marks={"BTCUSDT": risk_price},
            margin_headroom="1",
            capability_allowed=True,
            borrow_available=True,
        )
        snapshot = AuthoritativeRiskSnapshot(
            context=context,
            risk_policy=self.resolved.policy,
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
            valid_until=(READ_AT + timedelta(minutes=5))
            .isoformat()
            .replace("+00:00", "Z"),
            evidence_refs={
                "PORTFOLIO": "portfolio-1",
                "MARKET": "market-1",
                "MARGIN": "margin-1",
                "POLICY": self.resolved.registration_event_id,
                "RECONCILIATION": "reconciliation-1",
                "CAPABILITY": capability.snapshot_id,
                "BORROW": "borrow-1",
            },
            resolved_risk_policy=self.resolved,
            provider_environment="TESTNET",
            entity_policy_id=ENTITY_POLICY,
            instrument_family="SPOT",
        )
        return request, snapshot, prepared

    def _compose(self, request, snapshot, prepared):
        return compose_bybit_risk_price_semantics(
            request,
            snapshot,
            prepared_request=prepared,
            instrument_registry=self.instrument_registry,
            artifact_store=self.artifact_store,
        )

    def test_limit_prepared_request_binds_exact_authenticated_semantics_into_snapshot(self):
        request, snapshot, prepared = self._case()
        bound = self._compose(request, snapshot, prepared)

        self.assertNotEqual(bound.snapshot_id, snapshot.snapshot_id)
        self.assertRegex(bound.price_semantics_digest, r"^sha256:[0-9a-f]{64}$")
        self.assertEqual(
            bound.evidence_refs["INSTRUMENT"],
            self.instrument_registry.exact(f"{A}@1").metadata_evidence_binding(),
        )
        self.assertIsNone(snapshot.price_semantics_digest)
        self.assertNotIn("INSTRUMENT", snapshot.evidence_refs)

    def test_market_keeps_risk_reference_price_but_binds_no_wire_price_semantics(self):
        request, snapshot, prepared = self._case(
            order_type="MARKET",
            risk_price="101.25",
        )
        self.assertNotIn("price", prepared.body)
        bound = self._compose(request, snapshot, prepared)

        self.assertRegex(bound.price_semantics_digest, r"^sha256:[0-9a-f]{64}$")
        self.assertEqual(
            bound.evidence_refs["INSTRUMENT"],
            self.instrument_registry.exact(f"{A}@1").metadata_evidence_binding(),
        )

    def test_side_substitution_is_rejected_before_semantic_binding(self):
        request, snapshot, prepared = self._case(
            wire_side="SELL",
            risk_side="BUY",
        )
        with self.assertRaisesRegex(
            BybitRiskPriceSemanticsError,
            "side differs",
        ):
            self._compose(request, snapshot, prepared)

    def test_limit_price_substitution_is_rejected_before_semantic_binding(self):
        request, snapshot, prepared = self._case(
            wire_price="100.01",
            risk_price="100",
        )
        with self.assertRaisesRegex(
            BybitRiskPriceSemanticsError,
            "LIMIT price differs",
        ):
            self._compose(request, snapshot, prepared)

    def test_capability_substitution_is_rejected_against_prepared_scope(self):
        request, snapshot, prepared = self._case()
        request = replace(request, capability_snapshot_id="different-capability")
        snapshot = replace(
            snapshot,
            capability_snapshot_id="different-capability",
            evidence_refs={
                **dict(snapshot.evidence_refs.items()),
                "CAPABILITY": "different-capability",
            },
        )
        with self.assertRaisesRegex(
            BybitRiskPriceSemanticsError,
            "provider scope differs",
        ):
            self._compose(request, snapshot, prepared)

    def test_preexisting_instrument_binding_must_equal_authenticated_metadata(self):
        request, snapshot, prepared = self._case()
        snapshot = replace(
            snapshot,
            evidence_refs={
                **dict(snapshot.evidence_refs.items()),
                "INSTRUMENT": "sha256:" + "9" * 64,
            },
        )
        with self.assertRaisesRegex(
            BybitRiskPriceSemanticsError,
            "different instrument metadata binding",
        ):
            self._compose(request, snapshot, prepared)

    def test_preexisting_price_digest_must_equal_fresh_composition(self):
        request, snapshot, prepared = self._case()
        snapshot = replace(
            snapshot,
            price_semantics_digest="sha256:" + "8" * 64,
            evidence_refs={
                **dict(snapshot.evidence_refs.items()),
                "INSTRUMENT": "sha256:" + "9" * 64,
            },
        )
        with self.assertRaisesRegex(
            BybitRiskPriceSemanticsError,
            "different price-semantics identity",
        ):
            self._compose(request, snapshot, prepared)

    def test_authenticated_symbol_must_equal_prepared_symbol(self):
        request, snapshot, prepared = self._case(symbol="ETHUSDT")
        with self.assertRaisesRegex(
            BybitRiskPriceSemanticsError,
            "symbol differs",
        ):
            self._compose(request, snapshot, prepared)

    def test_rebound_prepared_projection_fails_before_forged_execution(self):
        request, snapshot, prepared = self._case()
        calls = []

        def forged(*_args, **_kwargs):
            calls.append("forged")
            raise AssertionError("forged projection executed")

        with patch.object(bridge_module, "guarded_order_projection", forged):
            with self.assertRaisesRegex(
                BybitRiskPriceSemanticsError,
                "executable authority changed",
            ):
                self._compose(request, snapshot, prepared)
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
