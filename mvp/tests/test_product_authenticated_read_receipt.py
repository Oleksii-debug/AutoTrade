from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
import unittest

import mvp.autotrade_mvp.provider_core as provider_core
import mvp.autotrade_mvp.provider_transport as provider_transport
from unittest.mock import patch

from mvp.autotrade_mvp.capabilities import CapabilityRegistry
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


class ProductAuthenticatedReadReceiptTests(unittest.TestCase):
    def _product(self):
        capability = verified_read_capability()
        registry = CapabilityRegistry()
        registry.add(capability)
        security = object.__new__(SecurityBoundary)
        point = READ_NOW + timedelta(seconds=1)
        transport = build_product_credential_transport(
            BinanceSpotAuthenticatedReadTransport,
            security_boundary=security,
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
