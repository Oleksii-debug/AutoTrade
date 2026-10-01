from datetime import datetime, timezone
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.capabilities import CapabilityRegistry
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.provider_transport import (
    KRAKEN_SPOT_ENDPOINT_POLICIES,
    KrakenSpotAuthenticatedReadTransport,
    KrakenSpotDurableNonceAllocator,
    KrakenSpotHttpTransport,
    ProviderEndpointPolicy,
    ProviderTransportScopeError,
)
from mvp.autotrade_mvp.windows_secrets import PersistentCredentialHandle


class _Resolver:
    def resolve_for_execution(self, *args, **kwargs):
        raise AssertionError("constructor must not resolve credentials")


def _handle(*, purpose: str) -> PersistentCredentialHandle:
    return PersistentCredentialHandle(
        handle_id=f"kraken-{purpose.lower()}",
        account_id="acct-kraken",
        provider="KRAKEN",
        environment="LIVE",
        purpose=purpose,
        generation=1,
    )


def _equal_custom_policy() -> ProviderEndpointPolicy:
    return ProviderEndpointPolicy(
        provider_id="KRAKEN",
        environment="LIVE",
        base_url="https://api.kraken.com",
        allowed_hosts=frozenset({"api.kraken.com"}),
    )


class KrakenSpotCanonicalPolicyAuthorityTests(unittest.TestCase):
    def _nonce(self, store: JournalStore, *, purpose: str):
        handle = _handle(purpose=purpose)
        allocator = KrakenSpotDurableNonceAllocator(
            journal=store,
            account_id="acct-kraken",
            environment="LIVE",
            credential_handle=handle,
            clock_millis=lambda: 100,
            clock_utc=lambda: datetime(2026, 10, 1, tzinfo=timezone.utc),
        )
        return handle, allocator

    def _write_transport(self, store: JournalStore, *, policy):
        handle, allocator = self._nonce(store, purpose="TRADE")
        return KrakenSpotHttpTransport(
            policy=policy,
            account_id="acct-kraken",
            capability_snapshot_id="cap-1",
            secret_resolver=_Resolver(),
            credential_handle=handle,
            session_token="session",
            origin="autotrade://execution",
            execution_identity="sender",
            nonce_allocator=allocator,
        )

    def _read_transport(self, store: JournalStore, *, policy):
        handle, allocator = self._nonce(store, purpose="READ")
        return KrakenSpotAuthenticatedReadTransport(
            policy=policy,
            account_id="acct-kraken",
            capability_snapshot_id="cap-1",
            capability_registry=CapabilityRegistry(),
            secret_resolver=_Resolver(),
            credential_handle=handle,
            session_token="session",
            origin="autotrade://execution",
            execution_identity="reader",
            nonce_allocator=allocator,
            clock_utc=lambda: datetime(2026, 10, 1, tzinfo=timezone.utc),
        )

    def test_write_rejects_equal_caller_created_live_policy(self):
        with TemporaryDirectory() as directory:
            with self.assertRaisesRegex(
                ProviderTransportScopeError,
                "AutoTrade-owned canonical KRAKEN LIVE policy",
            ):
                self._write_transport(
                    JournalStore(f"{directory}/journal.sqlite3"),
                    policy=_equal_custom_policy(),
                )

    def test_authenticated_read_rejects_equal_caller_created_live_policy(self):
        with TemporaryDirectory() as directory:
            with self.assertRaisesRegex(
                ProviderTransportScopeError,
                "AutoTrade-owned canonical KRAKEN LIVE policy",
            ):
                self._read_transport(
                    JournalStore(f"{directory}/journal.sqlite3"),
                    policy=_equal_custom_policy(),
                )

    def test_mutated_registered_policy_is_rejected_before_write_transport(self):
        policy = KRAKEN_SPOT_ENDPOINT_POLICIES["LIVE"]
        original = policy.base_url
        try:
            object.__setattr__(policy, "base_url", "https://attacker.example")
            with TemporaryDirectory() as directory:
                with self.assertRaisesRegex(
                    ProviderTransportScopeError,
                    "canonical Kraken Spot LIVE endpoint policy state changed",
                ):
                    self._write_transport(
                        JournalStore(f"{directory}/journal.sqlite3"),
                        policy=policy,
                    )
        finally:
            object.__setattr__(policy, "base_url", original)

    def test_mutated_registered_policy_is_rejected_before_authenticated_read(self):
        policy = KRAKEN_SPOT_ENDPOINT_POLICIES["LIVE"]
        original_hosts = policy.allowed_hosts
        try:
            object.__setattr__(policy, "allowed_hosts", frozenset({"attacker.example"}))
            with TemporaryDirectory() as directory:
                with self.assertRaisesRegex(
                    ProviderTransportScopeError,
                    "canonical Kraken Spot LIVE endpoint policy state changed",
                ):
                    self._read_transport(
                        JournalStore(f"{directory}/journal.sqlite3"),
                        policy=policy,
                    )
        finally:
            object.__setattr__(policy, "allowed_hosts", original_hosts)

    def test_transport_retains_fresh_policy_copy_after_selection(self):
        with TemporaryDirectory() as directory:
            registered = KRAKEN_SPOT_ENDPOINT_POLICIES["LIVE"]
            transport = self._write_transport(
                JournalStore(f"{directory}/journal.sqlite3"),
                policy=registered,
            )
            self.assertIsNot(transport.policy, registered)
            self.assertEqual(transport.policy, _equal_custom_policy())
            original = registered.base_url
            try:
                object.__setattr__(registered, "base_url", "https://attacker.example")
                self.assertEqual(transport.policy.base_url, "https://api.kraken.com")
            finally:
                object.__setattr__(registered, "base_url", original)


if __name__ == "__main__":
    unittest.main()
