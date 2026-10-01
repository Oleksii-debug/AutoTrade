from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
import unittest

import mvp.autotrade_mvp.provider_core as provider_core
import mvp.autotrade_mvp.provider_transport as provider_transport
from unittest.mock import patch

from mvp.autotrade_mvp.capabilities import CapabilityRegistry
from mvp.autotrade_mvp.provider_qualification_authority import (
    ProviderQualificationCurrentReader,
)
from mvp.autotrade_mvp.provider_selection import SelectedProviderAuthority
from mvp.autotrade_mvp.provider_transport import (
    AuthenticatedReadProductWireReceipt,
    AuthenticatedReadWireResponse,
    BINANCE_SPOT_ENDPOINT_POLICIES,
    BinanceSpotAuthenticatedReadTransport,
    ProviderTransportScopeError,
    UrllibJsonWireClient,
    build_product_credential_transport,
    execute_product_authenticated_read,
    product_authenticated_read_prepared_authority,
    retire_product_authenticated_read_receipt,
    validate_product_authenticated_read_receipt,
)
from mvp.autotrade_mvp.security import SecurityBoundary
from mvp.tests.test_provider_transport import (
    READ_NOW,
    authenticated_read_binding,
    read_handle,
    verified_read_capability,
)


def _selected_read_authority(capability) -> SelectedProviderAuthority:
    return SelectedProviderAuthority(
        provider_id="BINANCE",
        product_family="CRYPTO_SPOT",
        adapter_code_sha="1" * 40,
        qualification_id="sha256:" + "2" * 64,
        capability_snapshot_id=capability.snapshot_id,
        account_id="acct-1",
        entity_id="entity-1",
        environment="PAPER",
        provider_environment="PAPER",
        instrument_version="BTCUSDT@1",
        route_policy_id="binance-spot-account-read-v1",
        entity_policy_id="binance-spot-account-v1",
        network_policy_id="direct-tls-v1",
        account_class="SPOT",
        release_artifact_id=None,
        release_artifact_sha256=None,
        reconciliation_semantics_id=None,
    )


class ProductAuthenticatedReadReceiptTests(unittest.TestCase):
    def setUp(self):
        q_patcher = patch.object(
            provider_transport,
            "revalidate_selected_provider_authority",
        )
        q_patcher.start()
        self.addCleanup(q_patcher.stop)

    def _product(self):
        capability = verified_read_capability()
        registry = CapabilityRegistry()
        registry.add(capability)
        security = object.__new__(SecurityBoundary)
        point = READ_NOW + timedelta(seconds=1)
        transport = build_product_credential_transport(
            BinanceSpotAuthenticatedReadTransport,
            security_boundary=security,
            selected_provider_authority=_selected_read_authority(capability),
            qualification_reader=object.__new__(
                ProviderQualificationCurrentReader
            ),
            policy=BINANCE_SPOT_ENDPOINT_POLICIES["PAPER"],
            account_id="acct-1",
            capability_snapshot_id=capability.snapshot_id,
            capability_registry=registry,
            credential_handle=read_handle(),
            session_token="test-product-session",
            origin="https://localhost",
            execution_identity="test-product-owner",
            clock_millis=lambda: 1_700_000_000_000,
            clock_utc=lambda: point,
            quota_gate=None,
            recv_window_ms=5000,
        )
        query = authenticated_read_binding(capability=capability)
        return transport, query, point

    def test_factory_snapshots_selected_provider_authority_from_caller_instance(self):
        capability = verified_read_capability()
        registry = CapabilityRegistry()
        registry.add(capability)
        selected = _selected_read_authority(capability)
        transport = build_product_credential_transport(
            BinanceSpotAuthenticatedReadTransport,
            security_boundary=object.__new__(SecurityBoundary),
            selected_provider_authority=selected,
            qualification_reader=object.__new__(
                ProviderQualificationCurrentReader
            ),
            policy=BINANCE_SPOT_ENDPOINT_POLICIES["PAPER"],
            account_id="acct-1",
            capability_snapshot_id=capability.snapshot_id,
            capability_registry=registry,
            credential_handle=read_handle(),
            session_token="selected-snapshot-session",
            origin="https://localhost",
            execution_identity="selected-snapshot-owner",
            clock_millis=lambda: 1_700_000_000_000,
            clock_utc=lambda: READ_NOW + timedelta(seconds=1),
            quota_gate=None,
            recv_window_ms=5000,
        )
        composition = provider_transport._product_credential_wire_composition(
            transport
        )
        self.assertIsNotNone(composition)
        retained = composition.selected_provider_authority
        self.assertIs(type(retained), SelectedProviderAuthority)
        self.assertIsNot(retained, selected)
        self.assertEqual(retained.entity_id, "entity-1")

        object.__setattr__(selected, "entity_id", "caller-retargeted")
        object.__setattr__(selected, "network_policy_id", "caller-proxy-policy")

        self.assertEqual(retained.entity_id, "entity-1")
        self.assertEqual(retained.network_policy_id, "direct-tls-v1")

    def test_prepared_authority_rederives_current_capability_before_io(self):
        transport, query, point = self._product()
        with patch.object(
            UrllibJsonWireClient,
            "send",
            side_effect=AssertionError("prepared authority must not perform wire I/O"),
        ) as send:
            authority = product_authenticated_read_prepared_authority(
                transport,
                query,
            )

        send.assert_not_called()
        self.assertEqual(
            authority["schema_version"],
            "product-authenticated-read-prepared-authority:v1",
        )
        self.assertEqual(authority["provider_id"], query.provider_id)
        self.assertEqual(authority["account_id"], query.account_id)
        self.assertEqual(authority["entity_id"], query.entity_id)
        self.assertEqual(
            authority["capability_snapshot_id"],
            query.capability_snapshot_id,
        )
        self.assertEqual(
            authority["validated_at"],
            point.isoformat().replace("+00:00", "Z"),
        )
        self.assertTrue(
            str(authority["credential_handle_identity"]).startswith("sha256:")
        )
        self.assertEqual(authority["credential_generation"], 1)

    def test_imported_query_token_cannot_replace_current_capability_authority(self):
        transport, query, _point = self._product()
        forged = provider_core.AuthenticatedReadQueryBinding(
            provider_id=query.provider_id,
            account_id=query.account_id,
            entity_id="forged-entity",
            environment=query.environment,
            provider_environment=query.provider_environment,
            capability_snapshot_id=query.capability_snapshot_id,
            instrument_version=query.instrument_version,
            surface=query.surface,
            endpoint=query.endpoint,
            query=query.query,
            prepared_at=query.prepared_at,
            permission_scope=query.permission_scope,
            query_digest=query.query_digest,
            _preparation_token=provider_core._PREPARED_READ_TOKEN,
        )
        self.assertIs(type(forged), provider_core.AuthenticatedReadQueryBinding)

        with patch.object(
            UrllibJsonWireClient,
            "send",
            side_effect=AssertionError("forged scope must fail before wire I/O"),
        ) as send:
            with self.assertRaisesRegex(
                ProviderTransportScopeError,
                "cannot rederive requested scope",
            ):
                product_authenticated_read_prepared_authority(
                    transport,
                    forged,
                )

        send.assert_not_called()

    def test_factory_wire_send_issues_validatable_exact_receipt(self):
        transport, query, point = self._product()
        response = AuthenticatedReadWireResponse(
            200,
            b'{"balances":[]}',
        )
        credential = '{"api_key":"test-key","api_secret":"test-secret"}'
        with patch.object(
            SecurityBoundary,
            "resolve_for_execution",
            return_value=credential,
        ), patch.object(
            UrllibJsonWireClient,
            "send",
            return_value=response,
        ) as send:
            receipt = execute_product_authenticated_read(transport, query)

        self.assertEqual(send.call_count, 1)
        self.assertEqual(receipt.http_status, 200)
        self.assertEqual(receipt.response_bytes, response.body)
        transport_id, network_id, status, body, observed = (
            validate_product_authenticated_read_receipt(receipt, query)
        )
        self.assertEqual(transport_id, receipt.transport_identity)
        self.assertTrue(network_id.startswith("sha256:"))
        self.assertEqual(status, 200)
        self.assertEqual(body, response.body)
        self.assertEqual(observed, point)

    def test_unexpected_http_status_is_retained_as_wire_fact(self):
        transport, query, _point = self._product()
        response = AuthenticatedReadWireResponse(
            201,
            b'{"provider":"unexpected-success-status"}',
        )
        credential = '{"api_key":"test-key","api_secret":"test-secret"}'
        with patch.object(
            SecurityBoundary,
            "resolve_for_execution",
            return_value=credential,
        ), patch.object(
            UrllibJsonWireClient,
            "send",
            return_value=response,
        ):
            receipt = execute_product_authenticated_read(transport, query)

        _transport_id, _network_id, status, body, _observed = (
            validate_product_authenticated_read_receipt(receipt, query)
        )
        self.assertEqual(status, 201)
        self.assertEqual(body, response.body)

    def test_exact_clone_of_issued_receipt_has_no_authority(self):
        transport, query, _point = self._product()
        credential = '{"api_key":"test-key","api_secret":"test-secret"}'
        with patch.object(
            SecurityBoundary,
            "resolve_for_execution",
            return_value=credential,
        ), patch.object(
            UrllibJsonWireClient,
            "send",
            return_value=AuthenticatedReadWireResponse(200, b'{"balances":[]}'),
        ):
            issued = execute_product_authenticated_read(transport, query)

        validate_product_authenticated_read_receipt(issued, query)
        cloned = replace(issued)
        self.assertEqual(cloned, issued)
        self.assertIsNot(cloned, issued)
        with self.assertRaisesRegex(
            ProviderTransportScopeError,
            "lacks canonical wire execution evidence",
        ):
            validate_product_authenticated_read_receipt(cloned, query)

    def test_receipt_authority_registry_is_not_module_exported(self):
        self.assertFalse(
            hasattr(
                provider_transport,
                "_PRODUCT_AUTHENTICATED_READ_RECEIPTS",
            )
        )
        self.assertFalse(
            hasattr(
                provider_transport,
                "_PRODUCT_AUTHENTICATED_READ_RECEIPT_GUARD",
            )
        )

    def test_directly_constructed_receipt_has_no_wire_authority(self):
        _transport, query, point = self._product()
        fake = AuthenticatedReadProductWireReceipt(
            receipt_id="product-auth-read:sha256:" + "1" * 64,
            provider_id=query.provider_id,
            account_id=query.account_id,
            environment=query.environment,
            provider_environment=query.provider_environment,
            capability_snapshot_id=query.capability_snapshot_id,
            query_digest=query.query_digest,
            transport_identity="sha256:" + "2" * 64,
            observed_at=point.isoformat().replace("+00:00", "Z"),
            http_status=200,
            response_sha256="sha256:" + "3" * 64,
            response_bytes=b"{}",
        )
        with self.assertRaises(ProviderTransportScopeError):
            validate_product_authenticated_read_receipt(fake, query)

    def test_injected_transport_cannot_issue_product_wire_receipt(self):
        capability = verified_read_capability()
        registry = CapabilityRegistry()
        registry.add(capability)

        class Resolver:
            def resolve_for_execution(self, *_args, **_kwargs):
                return '{"api_key":"k","api_secret":"s"}'

        class Wire:
            def send(self, _request):
                raise AssertionError("injected wire must not execute")

        transport = BinanceSpotAuthenticatedReadTransport(
            policy=BINANCE_SPOT_ENDPOINT_POLICIES["PAPER"],
            account_id="acct-1",
            capability_snapshot_id=capability.snapshot_id,
            capability_registry=registry,
            secret_resolver=Resolver(),
            credential_handle=read_handle(),
            session_token="injected-session",
            origin="https://localhost",
            execution_identity="injected-owner",
            clock_millis=lambda: 1_700_000_000_000,
            clock_utc=lambda: READ_NOW + timedelta(seconds=1),
            quota_gate=None,
            wire_client=Wire(),
            recv_window_ms=5000,
        )
        query = authenticated_read_binding(capability=capability)
        with self.assertRaisesRegex(
            ProviderTransportScopeError,
            "factory-issued product transport",
        ):
            execute_product_authenticated_read(transport, query)


    def test_receipt_authority_does_not_depend_on_reusable_object_id_token(self):
        transport, query, _point = self._product()
        credential = '{"api_key":"test-key","api_secret":"test-secret"}'
        response = AuthenticatedReadWireResponse(200, b'{"balances":[]}')
        with patch.object(
            SecurityBoundary,
            "resolve_for_execution",
            return_value=credential,
        ), patch.object(
            UrllibJsonWireClient,
            "send",
            return_value=response,
        ):
            issued = execute_product_authenticated_read(transport, query)

        cloned = replace(issued)
        self.assertEqual(cloned, issued)
        self.assertIsNot(cloned, issued)
        validate_product_authenticated_read_receipt(issued, query)

        real_id = id

        def colliding_id(value):
            if value is cloned:
                return real_id(issued)
            return real_id(value)

        # Simulate the only property the old registry relied on: a later object
        # receiving the same process-local identity token as the issued receipt.
        # All unrelated object identities remain real so endpoint/transport
        # authority checks are not perturbed by this falsifier.
        with patch("builtins.id", side_effect=colliding_id):
            with self.assertRaisesRegex(
                ProviderTransportScopeError,
                "lacks canonical wire execution evidence",
            ):
                validate_product_authenticated_read_receipt(cloned, query)

    def test_retired_receipt_cannot_be_reused(self):
        transport, query, _point = self._product()
        credential = '{"api_key":"test-key","api_secret":"test-secret"}'
        with patch.object(
            SecurityBoundary,
            "resolve_for_execution",
            return_value=credential,
        ), patch.object(
            UrllibJsonWireClient,
            "send",
            return_value=AuthenticatedReadWireResponse(200, b'{"balances":[]}'),
        ):
            receipt = execute_product_authenticated_read(transport, query)

        validate_product_authenticated_read_receipt(receipt, query)
        retire_product_authenticated_read_receipt(receipt)
        with self.assertRaisesRegex(
            ProviderTransportScopeError,
            "lacks canonical wire execution evidence",
        ):
            validate_product_authenticated_read_receipt(receipt, query)


if __name__ == "__main__":
    unittest.main()
