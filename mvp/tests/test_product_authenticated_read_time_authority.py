from datetime import timedelta
import unittest
from unittest.mock import patch

from mvp.autotrade_mvp import provider_transport as provider_transport_module
from mvp.autotrade_mvp.capabilities import CapabilityRegistry
from mvp.autotrade_mvp.provider_qualification_authority import (
    ProviderQualificationCurrentReader,
)
from mvp.autotrade_mvp.provider_selection import SelectedProviderAuthority
from mvp.autotrade_mvp.provider_transport import (
    BINANCE_SPOT_ENDPOINT_POLICIES,
    BinanceSpotAuthenticatedReadTransport,
    ProviderTransportScopeError,
    build_product_credential_transport,
    product_authenticated_read_prepared_authority,
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


class ProductAuthenticatedReadTimeAuthorityTests(unittest.TestCase):
    def _product(self, *, registry, caller_clock):
        capability = verified_read_capability()
        return build_product_credential_transport(
            BinanceSpotAuthenticatedReadTransport,
            security_boundary=object.__new__(SecurityBoundary),
            selected_provider_authority=_selected_read_authority(capability),
            qualification_reader=object.__new__(
                ProviderQualificationCurrentReader
            ),
            policy=BINANCE_SPOT_ENDPOINT_POLICIES["PAPER"],
            account_id="acct-1",
            capability_snapshot_id=capability.snapshot_id,
            capability_registry=registry,
            credential_handle=read_handle(),
            session_token="product-time-authority-session",
            origin="https://localhost",
            execution_identity="product-time-authority-owner",
            clock_millis=lambda: 1_700_000_000_000,
            clock_utc=caller_clock,
            quota_gate=None,
            recv_window_ms=5000,
        )

    def test_factory_replaces_caller_clock_with_product_owned_clock(self):
        registry = CapabilityRegistry()
        registry.add(verified_read_capability())

        def caller_clock():
            return READ_NOW

        def product_clock():
            return READ_NOW + timedelta(seconds=1)

        with patch.object(
            provider_transport_module,
            "_current_authority_utc",
            new=product_clock,
        ):
            transport = self._product(
                registry=registry,
                caller_clock=caller_clock,
            )

        self.assertIs(
            object.__getattribute__(transport, "clock_utc"),
            product_clock,
        )
        self.assertIsNot(
            object.__getattribute__(transport, "clock_utc"),
            caller_clock,
        )

    def test_backdated_caller_clock_cannot_reauthorize_expired_product_read(self):
        capability = verified_read_capability()
        registry = CapabilityRegistry()
        registry.add(capability)
        query = authenticated_read_binding(capability=capability)
        expired_product_time = READ_NOW + timedelta(minutes=11)

        with patch.object(
            provider_transport_module,
            "_current_authority_utc",
            return_value=expired_product_time,
        ):
            transport = build_product_credential_transport(
                BinanceSpotAuthenticatedReadTransport,
                security_boundary=object.__new__(SecurityBoundary),
                selected_provider_authority=_selected_read_authority(capability),
                qualification_reader=object.__new__(
                    ProviderQualificationCurrentReader
                ),
                policy=BINANCE_SPOT_ENDPOINT_POLICIES["PAPER"],
                account_id="acct-1",
                capability_snapshot_id=capability.snapshot_id,
                capability_registry=registry,
                credential_handle=read_handle(),
                session_token="product-time-authority-expired",
                origin="https://localhost",
                execution_identity="product-time-authority-owner",
                clock_millis=lambda: 1_700_000_000_000,
                # This caller-selected instant is still inside the snapshot
                # validity window. Product admission must ignore it.
                clock_utc=lambda: READ_NOW,
                quota_gate=None,
                recv_window_ms=5000,
            )
            with self.assertRaisesRegex(
                ProviderTransportScopeError,
                "current capability",
            ):
                product_authenticated_read_prepared_authority(
                    transport,
                    query,
                )


if __name__ == "__main__":
    unittest.main()
