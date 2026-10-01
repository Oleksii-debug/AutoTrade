from __future__ import annotations

from datetime import datetime, timezone
import inspect
from types import MappingProxyType, SimpleNamespace
import unittest
from unittest.mock import patch

from mvp.autotrade_mvp.capabilities import CapabilityRegistry
from mvp.autotrade_mvp.provider_core import AuthenticatedReadQueryBinding, Surface
from mvp.autotrade_mvp.provider_qualification_authority import (
    ProviderQualificationCurrentReader,
)
from mvp.autotrade_mvp.provider_selection import (
    ProviderSelectionError,
    SelectedProviderAuthority,
)
import mvp.autotrade_mvp.provider_transport as transport_module
from mvp.autotrade_mvp.provider_transport import (
    AlpacaAuthenticatedReadTransport,
    BinanceSpotAuthenticatedReadTransport,
    BybitV5AuthenticatedReadTransport,
    KrakenSpotAuthenticatedReadTransport,
    WhiteBitAuthenticatedReadTransport,
    ProviderTransportScopeError,
    _require_product_authenticated_read_authority,
    build_product_credential_transport,
)
from mvp.autotrade_mvp.security import SecurityBoundary


def selected() -> SelectedProviderAuthority:
    return SelectedProviderAuthority(
        provider_id="ALPACA",
        product_family="EQUITIES",
        adapter_code_sha="1" * 40,
        qualification_id="sha256:" + "2" * 64,
        capability_snapshot_id="capability-alpaca-1",
        account_id="acct-alpaca",
        entity_id="entity-alpaca",
        environment="PAPER",
        provider_environment="PAPER",
        instrument_version="AAPL@1",
        route_policy_id="alpaca-trading-v2",
        entity_policy_id="alpaca-account-v1",
        network_policy_id="direct-tls-v1",
        account_class="STANDARD",
        release_artifact_id=None,
        release_artifact_sha256=None,
        reconciliation_semantics_id=None,
    )


def query() -> AuthenticatedReadQueryBinding:
    # This oracle tests the terminal composition barrier, not provider-core
    # construction. Build an exact-type frozen assertion with canonical fields.
    value = object.__new__(AuthenticatedReadQueryBinding)
    fields = {
        "provider_id": "ALPACA",
        "account_id": "acct-alpaca",
        "entity_id": "entity-alpaca",
        "environment": "PAPER",
        "provider_environment": "PAPER",
        "capability_snapshot_id": "capability-alpaca-1",
        "instrument_version": "AAPL@1",
        "surface": Surface.ACTIVITIES,
        "endpoint": "/v2/account/activities/FILL",
        "query": MappingProxyType({"direction": "asc", "page_size": "100"}),
        "prepared_at": "2026-10-01T01:00:00Z",
        "permission_scope": "TRADE.READ",
        "query_digest": "sha256:" + "3" * 64,
    }
    for name, item in fields.items():
        object.__setattr__(value, name, item)
    return value


def product_transport(registry: CapabilityRegistry) -> AlpacaAuthenticatedReadTransport:
    value = object.__new__(AlpacaAuthenticatedReadTransport)
    object.__setattr__(value, "capability_registry", registry)
    return value


class ProductAuthenticatedReadDecisionCutTests(unittest.TestCase):
    def setUp(self):
        self.selected = selected()
        self.reader = object.__new__(ProviderQualificationCurrentReader)
        self.registry = CapabilityRegistry()
        self.transport = product_transport(self.registry)
        self.query = query()
        self.composition = SimpleNamespace(
            selected_provider_authority=self.selected,
            qualification_reader=self.reader,
        )

    def test_factory_product_read_requires_qc_authority_before_construction(self):
        boundary = object.__new__(SecurityBoundary)
        with self.assertRaisesRegex(
            ProviderTransportScopeError,
            "requires exact selected provider authority",
        ):
            build_product_credential_transport(
                AlpacaAuthenticatedReadTransport,
                security_boundary=boundary,
            )

    def test_terminal_barrier_rejects_noncanonical_network_policy(self):
        object.__setattr__(
            self.selected,
            "network_policy_id",
            "caller-proxy-policy-v1",
        )
        with patch.object(
            transport_module,
            "_product_credential_wire_composition",
            return_value=self.composition,
        ):
            with self.assertRaisesRegex(
                ProviderTransportScopeError,
                "canonical direct-TLS network policy",
            ):
                _require_product_authenticated_read_authority(
                    self.transport,
                    self.query,
                )

    def test_terminal_barrier_uses_process_time_and_revalidates_exact_qc(self):
        with patch.object(
            transport_module,
            "_product_credential_wire_composition",
            return_value=self.composition,
        ), patch.object(
            transport_module,
            "revalidate_selected_provider_authority",
        ) as revalidate:
            _require_product_authenticated_read_authority(
                self.transport,
                self.query,
            )

        self.assertEqual(revalidate.call_count, 1)
        kwargs = revalidate.call_args.kwargs
        self.assertIs(kwargs["qualification_reader"], self.reader)
        self.assertIs(kwargs["capability_registry"], self.registry)
        self.assertIs(type(kwargs["at"]), datetime)
        self.assertIsNotNone(kwargs["at"].tzinfo)
        self.assertEqual(kwargs["at"].utcoffset(), timezone.utc.utcoffset(kwargs["at"]))

    def test_q_or_c_change_fails_before_wire_cut(self):
        with patch.object(
            transport_module,
            "_product_credential_wire_composition",
            return_value=self.composition,
        ), patch.object(
            transport_module,
            "revalidate_selected_provider_authority",
            side_effect=ProviderSelectionError("Q1 or C1 changed"),
        ):
            with self.assertRaisesRegex(
                ProviderTransportScopeError,
                "Q.C authority changed before wire",
            ):
                _require_product_authenticated_read_authority(
                    self.transport,
                    self.query,
                )

    def test_route_rule_change_during_qc_cut_fails_closed(self):
        route1 = MappingProxyType(
            {
                "schema_version": "authenticated-read-route:v1",
                "provider_id": "ALPACA",
                "environment": "PAPER",
                "provider_environment": "PAPER",
                "surface": "ACTIVITIES",
                "endpoint": "/v2/account/activities/FILL",
                "permission_scope": "TRADE.READ",
                "data_entitlement": "TRADES",
                "success_statuses": [200],
                "route_digest": "sha256:" + "4" * 64,
            }
        )
        route2 = MappingProxyType(
            {
                **dict(route1),
                "route_digest": "sha256:" + "5" * 64,
            }
        )
        with patch.object(
            transport_module,
            "_product_credential_wire_composition",
            return_value=self.composition,
        ), patch.object(
            transport_module,
            "canonical_authenticated_read_route",
            side_effect=[route1, route2],
        ), patch.object(
            transport_module,
            "revalidate_selected_provider_authority",
        ):
            with self.assertRaisesRegex(
                ProviderTransportScopeError,
                "route authority changed during final Q.C cut",
            ):
                _require_product_authenticated_read_authority(
                    self.transport,
                    self.query,
                )

    def test_query_scope_cannot_escape_selected_authority(self):
        object.__setattr__(self.query, "account_id", "other-account")
        with patch.object(
            transport_module,
            "_product_credential_wire_composition",
            return_value=self.composition,
        ), patch.object(
            transport_module,
            "revalidate_selected_provider_authority",
        ) as revalidate:
            with self.assertRaisesRegex(
                ProviderTransportScopeError,
                "outside selected provider authority",
            ):
                _require_product_authenticated_read_authority(
                    self.transport,
                    self.query,
                )
        revalidate.assert_not_called()

    def test_all_product_read_sends_are_guarded_immediately_before_wire(self):
        for transport_type in (
            WhiteBitAuthenticatedReadTransport,
            KrakenSpotAuthenticatedReadTransport,
            AlpacaAuthenticatedReadTransport,
            BybitV5AuthenticatedReadTransport,
            BinanceSpotAuthenticatedReadTransport,
        ):
            source = inspect.getsource(transport_type.__call__)
            gate = source.rfind(
                "_require_product_authenticated_read_authority(self, query_binding)"
            )
            wire = source.rfind("_credential_wire_authority(self)[1].send")
            self.assertGreaterEqual(gate, 0, transport_type.__name__)
            self.assertGreater(wire, gate, transport_type.__name__)
            between = source[gate:wire]
            self.assertNotIn("quota_gate(", between)
            self.assertNotIn("resolve_for_execution(", between)


if __name__ == "__main__":
    unittest.main()
