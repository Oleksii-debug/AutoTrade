"""#652: noncommitting cross-provider factory product-composition oracle.

Targets the post-#1144 current-main product transport successor. It proves
factory-only issuance, canonical-wire non-exposure, semantic endpoint-policy and
credential-handle sealing, and top-level authority-scope continuity under
object.__setattr__ restamping. No real credentials, signed calls, or outbound
I/O. No CI PASS is claimed until an integration owner references the exact tree
and runs the repository qualification gates.
"""
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import gc
import unittest
import weakref

import mvp.autotrade_mvp.provider_transport as provider_transport
from mvp.autotrade_mvp.capabilities import CapabilityRegistry
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.provider_qualification_authority import (
    ProviderQualificationCurrentReader,
)
from mvp.autotrade_mvp.provider_selection import SelectedProviderAuthority
from mvp.autotrade_mvp.security import SecurityBoundary
from mvp.autotrade_mvp.provider_transport import (
    ALPACA_ENDPOINT_POLICIES,
    BINANCE_SPOT_ENDPOINT_POLICIES,
    BYBIT_V5_ENDPOINT_POLICIES,
    KRAKEN_SPOT_ENDPOINT_POLICIES,
    WHITEBIT_ENDPOINT_POLICIES,
    AlpacaTradingHttpTransport,
    BinanceSpotAuthenticatedReadTransport,
    BinanceSpotHttpTransport,
    BybitV5AuthenticatedReadTransport,
    BybitV5HttpTransport,
    build_product_credential_transport,
    KrakenSpotAuthenticatedReadTransport,
    KrakenSpotDurableNonceAllocator,
    KrakenSpotHttpTransport,
    ProviderEndpointPolicy,
    ProviderTransportScopeError,
    UrllibJsonWireClient,
    WhiteBitDurableNonceAllocator,
    WhiteBitHttpTransport,
)
from mvp.autotrade_mvp.windows_secrets import PersistentCredentialHandle


_PRODUCT_READ_TRANSPORTS = (
    KrakenSpotAuthenticatedReadTransport,
    BybitV5AuthenticatedReadTransport,
    BinanceSpotAuthenticatedReadTransport,
)


def _product_read_authority(constructor, args):
    if constructor not in _PRODUCT_READ_TRANSPORTS:
        return {}
    handle = args["credential_handle"]
    provider_environment = args.get(
        "provider_environment",
        handle.provider_environment,
    )
    return {
        "selected_provider_authority": SelectedProviderAuthority(
            provider_id=handle.provider,
            product_family="TEST_PRODUCT",
            adapter_code_sha="1" * 40,
            qualification_id="sha256:" + "2" * 64,
            capability_snapshot_id=args["capability_snapshot_id"],
            account_id=args["account_id"],
            entity_id="test-entity",
            environment=handle.environment,
            provider_environment=provider_environment,
            instrument_version="TEST@1",
            route_policy_id="test-route-v1",
            entity_policy_id="test-entity-v1",
            network_policy_id="direct-tls-v1",
            account_class="TEST",
            release_artifact_id=None,
            release_artifact_sha256=None,
            reconciliation_semantics_id=None,
        ),
        "qualification_reader": object.__new__(
            ProviderQualificationCurrentReader
        ),
    }


class _InjectedWire:
    def __init__(self):
        self.requests = []

    def send(self, request):
        self.requests.append(request)
        raise AssertionError("injected wire received a signed provider request")


class _InjectedTestResolver:
    def resolve_for_execution(self, *args, **kwargs):
        raise AssertionError("construction unexpectedly resolved a test credential")


class ProductionCredentialWireSeparationTests(unittest.TestCase):
    def _cases(self, journal):
        now = lambda: datetime(2026, 9, 30, tzinfo=timezone.utc)
        millis = lambda: 1700000000000
        gate = lambda *_: None
        registry = CapabilityRegistry()
        # Deliberately uninitialized exact product resolver type: any premature
        # credential-resolution call fails. The boundary must decide *before* I/O.
        security = object.__new__(SecurityBoundary)

        def scoped(
            provider,
            environment,
            account,
            purpose,
            policy,
            *,
            provider_environment=None,
        ):
            return {
                "policy": policy,
                "account_id": account,
                "capability_snapshot_id": "test-capability",
                "secret_resolver": security,
                "credential_handle": PersistentCredentialHandle(
                    handle_id=f"cred-{provider.lower()}-{purpose.lower()}",
                    account_id=account, provider=provider,
                    environment=environment,
                    provider_environment=provider_environment,
                    purpose=purpose, generation=1,
                ),
                "session_token": "test-not-a-real-session",
                "origin": "https://localhost",
                "execution_identity": "test-owner",
                "quota_gate": gate,
            }

        wb = scoped("WHITEBIT", "LIVE", "acct-wb", "TRADE", WHITEBIT_ENDPOINT_POLICIES["LIVE"])
        wb["nonce_allocator"] = WhiteBitDurableNonceAllocator(
            journal=journal, account_id="acct-wb", environment="LIVE",
            clock_millis=millis, clock_utc=now,
        )
        kraken = []
        for purpose in ("TRADE", "READ"):
            k = scoped("KRAKEN", "LIVE", "acct-kraken", purpose, KRAKEN_SPOT_ENDPOINT_POLICIES["LIVE"])
            k["nonce_allocator"] = KrakenSpotDurableNonceAllocator(
                journal=journal, account_id="acct-kraken", environment="LIVE",
                credential_handle=k["credential_handle"], clock_millis=millis, clock_utc=now,
            )
            if purpose == "READ":
                k["capability_registry"] = registry
                k["clock_utc"] = now
            kraken.append(k)

        alpaca = scoped("ALPACA", "PAPER", "acct-alpaca", "TRADE", ALPACA_ENDPOINT_POLICIES["PAPER"])

        bybit = []
        for purpose in ("TRADE", "READ"):
            b = scoped(
                "BYBIT",
                "PAPER",
                "acct-bybit",
                purpose,
                BYBIT_V5_ENDPOINT_POLICIES["TESTNET"],
                provider_environment="TESTNET",
            )
            b.update(
                provider_environment="TESTNET", capability_registry=registry,
                clock_millis=millis, clock_utc=now,
            )
            bybit.append(b)

        binance = []
        for purpose in ("TRADE", "READ"):
            b = scoped("BINANCE", "PAPER", "acct-binance", purpose, BINANCE_SPOT_ENDPOINT_POLICIES["PAPER"])
            b["clock_millis"] = millis
            if purpose == "READ":
                b.update(capability_registry=registry, clock_utc=now)
            binance.append(b)

        return (
            ("whitebit-write", WhiteBitHttpTransport, wb),
            ("kraken-write", KrakenSpotHttpTransport, kraken[0]),
            ("kraken-read", KrakenSpotAuthenticatedReadTransport, kraken[1]),
            ("alpaca-write", AlpacaTradingHttpTransport, alpaca),
            ("bybit-write", BybitV5HttpTransport, bybit[0]),
            ("bybit-read", BybitV5AuthenticatedReadTransport, bybit[1]),
            ("binance-write", BinanceSpotHttpTransport, binance[0]),
            ("binance-read", BinanceSpotAuthenticatedReadTransport, binance[1]),
        )

    @staticmethod
    def _product(constructor, args):
        kwargs = dict(args)
        security = kwargs.pop("secret_resolver")
        return build_product_credential_transport(
            constructor,
            security_boundary=security,
            **_product_read_authority(constructor, kwargs),
            **kwargs,
        )

    def test_all_product_resolvers_refuse_injected_wire_before_credentials(self):
        with TemporaryDirectory() as directory:
            journal = JournalStore(Path(directory) / "journal.sqlite3")
            for name, constructor, args in self._cases(journal):
                with self.subTest(name=name):
                    wire = _InjectedWire()
                    try:
                        constructor(**args, wire_client=wire)
                    except (ProviderTransportScopeError, PermissionError) as error:
                        self.assertRegex(
                            str(error).lower(),
                            "wire|credential|inject|product|production|factory",
                        )
                    except TypeError as error:
                        # Valid when an injected wire is not a constructor option
                        # at all, rather than a false-positive earlier scope bug.
                        self.assertIn("wire_client", str(error))
                    else:
                        self.fail("product SecurityBoundary accepted an injected wire")
                    self.assertEqual(wire.requests, [])

    def test_all_default_product_wires_refuse_postconstruction_replacement(self):
        with TemporaryDirectory() as directory:
            journal = JournalStore(Path(directory) / "journal.sqlite3")
            for name, constructor, args in self._cases(journal):
                with self.subTest(name=name):
                    product = self._product(constructor, args)
                    with self.assertRaisesRegex(
                        ProviderTransportScopeError,
                        "does not expose canonical wire",
                    ):
                        _ = product.wire_client
                    self.assertIsNone(
                        object.__getattribute__(product, "_wire_client")
                    )
                    malicious = _InjectedWire()
                    with self.assertRaises((
                        AttributeError, TypeError,
                        ProviderTransportScopeError, PermissionError,
                    )):
                        product.wire_client = malicious
                    self.assertEqual(malicious.requests, [])

    def test_product_wire_object_is_not_publicly_reachable_for_in_place_mutation(self):
        with TemporaryDirectory() as directory:
            journal = JournalStore(Path(directory) / "journal.sqlite3")
            for name, constructor, args in self._cases(journal):
                with self.subTest(name=name):
                    product = self._product(constructor, args)
                    self.assertIsNone(
                        object.__getattribute__(product, "_wire_client")
                    )
                    with self.assertRaisesRegex(
                        ProviderTransportScopeError,
                        "does not expose canonical wire",
                    ):
                        _ = product.wire_client

                    # The internal authority still resolves one exact canonical
                    # wire for execution; only the module TCB can reach it.
                    _, internal_wire = provider_transport._credential_wire_authority(
                        product
                    )
                    self.assertIs(type(internal_wire), UrllibJsonWireClient)


    def test_injected_test_resolver_still_supports_zero_wire_construction(self):
        # Preserves explicit test injection. It cannot be treated as
        # production provider-origin or evidence-issuer authority.
        with TemporaryDirectory() as directory:
            journal = JournalStore(Path(directory) / "journal.sqlite3")
            for name, constructor, product_args in self._cases(journal):
                with self.subTest(name=name):
                    wire = _InjectedWire()
                    args = dict(product_args, secret_resolver=_InjectedTestResolver())
                    test_transport = constructor(**args, wire_client=wire)
                    self.assertIs(test_transport.wire_client, wire)
                    self.assertEqual(wire.requests, [])


    def test_product_private_wire_slot_itself_is_not_mutable(self):
        # A read-only public property is insufficient when the same object still
        # stores the authority-bearing wire in a caller-writable _wire_client.
        with TemporaryDirectory() as directory:
            journal = JournalStore(Path(directory) / "journal.sqlite3")
            for name, constructor, args in self._cases(journal):
                with self.subTest(name=name):
                    product = self._product(constructor, args)
                    malicious = _InjectedWire()
                    with self.assertRaises((
                        AttributeError, TypeError,
                        ProviderTransportScopeError, PermissionError,
                    )):
                        product._wire_client = malicious
                    self.assertIsNone(
                        object.__getattribute__(product, "_wire_client")
                    )
                    with self.assertRaisesRegex(
                        ProviderTransportScopeError,
                        "does not expose canonical wire",
                    ):
                        _ = product.wire_client
                    self.assertEqual(malicious.requests, [])

    def test_injected_transport_cannot_be_restamped_with_product_resolver(self):
        # Construction-time pair checking is not a final authority boundary if
        # secret_resolver remains mutable after a test wire has been accepted.
        with TemporaryDirectory() as directory:
            journal = JournalStore(Path(directory) / "journal.sqlite3")
            for name, constructor, product_args in self._cases(journal):
                with self.subTest(name=name):
                    injected_wire = _InjectedWire()
                    args = dict(
                        product_args,
                        secret_resolver=_InjectedTestResolver(),
                    )
                    transport = constructor(**args, wire_client=injected_wire)
                    product_security = product_args["secret_resolver"]
                    with self.assertRaises((
                        AttributeError, TypeError,
                        ProviderTransportScopeError, PermissionError,
                    )):
                        transport.secret_resolver = product_security
                    self.assertIsNot(transport.secret_resolver, product_security)
                    self.assertIs(transport.wire_client, injected_wire)
                    self.assertEqual(injected_wire.requests, [])


    def test_object_setattr_cannot_replace_issued_product_pair(self):
        with TemporaryDirectory() as directory:
            journal = JournalStore(Path(directory) / "journal.sqlite3")
            for name, constructor, args in self._cases(journal):
                with self.subTest(name=name):
                    product = self._product(constructor, args)
                    issued_security = args["secret_resolver"]
                    issued_wire = provider_transport._credential_wire_authority(product)[1]
                    malicious = _InjectedWire()
                    object.__setattr__(product, "_wire_client", malicious)
                    object.__setattr__(product, "secret_resolver", _InjectedTestResolver())
                    resolved_security, resolved_wire = (
                        provider_transport._credential_wire_authority(product)
                    )
                    self.assertIs(resolved_security, issued_security)
                    self.assertIs(resolved_wire, issued_wire)
                    self.assertIsNot(resolved_wire, malicious)
                    with self.assertRaisesRegex(
                        ProviderTransportScopeError,
                        "does not expose canonical wire",
                    ):
                        _ = product.wire_client
                    self.assertEqual(malicious.requests, [])

    def test_object_setattr_cannot_promote_injected_pair_to_product(self):
        with TemporaryDirectory() as directory:
            journal = JournalStore(Path(directory) / "journal.sqlite3")
            for name, constructor, product_args in self._cases(journal):
                with self.subTest(name=name):
                    injected_wire = _InjectedWire()
                    args = dict(
                        product_args,
                        secret_resolver=_InjectedTestResolver(),
                    )
                    transport = constructor(**args, wire_client=injected_wire)
                    object.__setattr__(
                        transport,
                        "secret_resolver",
                        product_args["secret_resolver"],
                    )
                    with self.assertRaisesRegex(
                        ProviderTransportScopeError,
                        "issued credential/wire composition",
                    ):
                        provider_transport._credential_wire_authority(transport)
                    self.assertEqual(injected_wire.requests, [])

    def test_product_registry_fails_closed_on_authority_scope_mutation(self):
        with TemporaryDirectory() as directory:
            journal = JournalStore(Path(directory) / "journal.sqlite3")
            for name, constructor, args in self._cases(journal):
                with self.subTest(name=name):
                    product = self._product(constructor, args)
                    object.__setattr__(
                        product,
                        "account_id",
                        product.account_id + "-mutated",
                    )
                    with self.assertRaisesRegex(
                        ProviderTransportScopeError,
                        "composition scope changed",
                    ):
                        provider_transport._credential_wire_authority(product)


    def test_direct_security_boundary_without_factory_is_not_product_authority(self):
        with TemporaryDirectory() as directory:
            journal = JournalStore(Path(directory) / "journal.sqlite3")
            for name, constructor, args in self._cases(journal):
                with self.subTest(name=name):
                    with self.assertRaisesRegex(
                        ProviderTransportScopeError,
                        "product factory|build_product_credential_transport|forbidden",
                    ):
                        constructor(**args)

    def test_factory_registry_releases_transport_lifetime(self):
        with TemporaryDirectory() as directory:
            journal = JournalStore(Path(directory) / "journal.sqlite3")
            name, constructor, args = self._cases(journal)[0]
            product = self._product(constructor, args)
            object_id = id(product)
            ref = weakref.ref(product)
            self.assertIn(object_id, provider_transport._PRODUCT_CREDENTIAL_WIRE)
            del product
            gc.collect()
            self.assertIsNone(ref())
            self.assertNotIn(object_id, provider_transport._PRODUCT_CREDENTIAL_WIRE)

    def test_factory_rejects_valid_looking_noncanonical_endpoint_policy(self):
        with TemporaryDirectory() as directory:
            journal = JournalStore(Path(directory) / "journal.sqlite3")
            for name, constructor, args in self._cases(journal):
                with self.subTest(name=name):
                    canonical = args["policy"]
                    rogue = ProviderEndpointPolicy(
                        provider_id=canonical.provider_id,
                        environment=canonical.environment,
                        base_url="https://attacker.invalid",
                        allowed_hosts=frozenset({"attacker.invalid"}),
                        timeout_seconds=canonical.timeout_seconds,
                    )
                    kwargs = dict(args, policy=rogue)
                    security = kwargs.pop("secret_resolver")
                    with self.assertRaisesRegex(
                        ProviderTransportScopeError,
                        "unchanged canonical registry policy",
                    ):
                        build_product_credential_transport(
                            constructor,
                            security_boundary=security,
                            **_product_read_authority(constructor, kwargs),
                            **kwargs,
                        )

    def test_in_place_endpoint_policy_mutation_cannot_change_product_network_scope(self):
        with TemporaryDirectory() as directory:
            journal = JournalStore(Path(directory) / "journal.sqlite3")
            for name, constructor, args in self._cases(journal):
                with self.subTest(name=name):
                    product = self._product(constructor, args)
                    raw_policy = object.__getattribute__(product, "policy")
                    original_base = raw_policy.base_url
                    original_hosts = raw_policy.allowed_hosts
                    self.assertEqual(product.policy.base_url, original_base)
                    object.__setattr__(
                        raw_policy,
                        "base_url",
                        "https://attacker.invalid",
                    )
                    object.__setattr__(
                        raw_policy,
                        "allowed_hosts",
                        frozenset({"attacker.invalid"}),
                    )
                    try:
                        # Runtime reads come from the captured semantic snapshot,
                        # not the caller-mutated canonical policy object.
                        self.assertEqual(product.policy.base_url, original_base)
                        self.assertEqual(
                            product.policy.allowed_hosts,
                            original_hosts,
                        )
                        with self.assertRaisesRegex(
                            ProviderTransportScopeError,
                            "policy state changed",
                        ):
                            provider_transport._credential_wire_authority(
                                product
                            )
                    finally:
                        object.__setattr__(
                            raw_policy,
                            "base_url",
                            original_base,
                        )
                        object.__setattr__(
                            raw_policy,
                            "allowed_hosts",
                            original_hosts,
                        )

    def test_in_place_credential_handle_mutation_cannot_retarget_product_secret(self):
        with TemporaryDirectory() as directory:
            journal = JournalStore(Path(directory) / "journal.sqlite3")
            for name, constructor, args in self._cases(journal):
                with self.subTest(name=name):
                    product = self._product(constructor, args)
                    raw_handle = object.__getattribute__(
                        product,
                        "credential_handle",
                    )
                    original_id = raw_handle.handle_id
                    original_generation = raw_handle.generation
                    self.assertEqual(
                        product.credential_handle.handle_id,
                        original_id,
                    )
                    object.__setattr__(
                        raw_handle,
                        "handle_id",
                        original_id + "-mutated",
                    )
                    object.__setattr__(
                        raw_handle,
                        "generation",
                        original_generation + 1,
                    )
                    try:
                        # Credential resolution receives a fresh value object from
                        # the captured semantic state, never the mutated raw handle.
                        self.assertEqual(
                            product.credential_handle.handle_id,
                            original_id,
                        )
                        self.assertEqual(
                            product.credential_handle.generation,
                            original_generation,
                        )
                        with self.assertRaisesRegex(
                            ProviderTransportScopeError,
                            "credential state changed",
                        ):
                            provider_transport._credential_wire_authority(
                                product
                            )
                    finally:
                        object.__setattr__(
                            raw_handle,
                            "handle_id",
                            original_id,
                        )
                        object.__setattr__(
                            raw_handle,
                            "generation",
                            original_generation,
                        )

    def test_top_level_product_scope_reads_are_registry_sealed_against_restamp(self):
        with TemporaryDirectory() as directory:
            journal = JournalStore(Path(directory) / "journal.sqlite3")
            for name, constructor, args in self._cases(journal):
                with self.subTest(name=name):
                    product = self._product(constructor, args)
                    raw = object.__getattribute__(product, "__dict__")
                    for field in (
                        "account_id",
                        "capability_snapshot_id",
                        "provider_environment",
                        "session_token",
                        "origin",
                        "execution_identity",
                        "recv_window_ms",
                        "capability_registry",
                        "nonce_allocator",
                        "clock_millis",
                        "clock_utc",
                        "quota_gate",
                    ):
                        if field not in raw:
                            continue
                        original = raw[field]
                        replacement = (
                            original + "-mutated"
                            if isinstance(original, str)
                            else object()
                        )
                        object.__setattr__(product, field, replacement)
                        try:
                            self.assertIs(
                                getattr(product, field),
                                original,
                            ) if not isinstance(original, str) else self.assertEqual(
                                getattr(product, field),
                                original,
                            )
                            with self.assertRaisesRegex(
                                ProviderTransportScopeError,
                                "composition (scope|authority) changed",
                            ):
                                provider_transport._credential_wire_authority(
                                    product
                                )
                        finally:
                            object.__setattr__(product, field, original)

    def test_factory_freezes_capability_and_provider_environment_scope(self):
        with TemporaryDirectory() as directory:
            journal = JournalStore(Path(directory) / "journal.sqlite3")
            for name, constructor, args in self._cases(journal):
                with self.subTest(name=name):
                    product = self._product(constructor, args)
                    original_capability = product.capability_snapshot_id
                    object.__setattr__(
                        product,
                        "capability_snapshot_id",
                        original_capability + "-mutated",
                    )
                    with self.assertRaisesRegex(
                        ProviderTransportScopeError,
                        "composition scope changed: capability_snapshot_id",
                    ):
                        provider_transport._credential_wire_authority(product)

            for name, constructor, args in self._cases(journal):
                if not name.startswith("bybit-"):
                    continue
                with self.subTest(name=name + "-provider-environment"):
                    product = self._product(constructor, args)
                    replacement = (
                        "DEMO"
                        if product.provider_environment == "TESTNET"
                        else "TESTNET"
                    )
                    object.__setattr__(
                        product,
                        "provider_environment",
                        replacement,
                    )
                    self.assertNotEqual(replacement, product.provider_environment)
                    self.assertEqual(
                        product.provider_environment,
                        args["provider_environment"],
                    )
                    with self.assertRaisesRegex(
                        ProviderTransportScopeError,
                        "composition scope changed: provider_environment",
                    ):
                        provider_transport._credential_wire_authority(product)


class _DelegatingSecurityBoundaryProxy:
    """Structural resolver that delegates to a real product SecurityBoundary."""

    def __init__(self, boundary):
        self._boundary = boundary

    def resolve_for_execution(self, *args, **kwargs):
        return self._boundary.resolve_for_execution(*args, **kwargs)


class FactoryOnlyProxyBypassTests(unittest.TestCase):
    def test_security_boundary_proxy_cannot_acquire_default_real_wire(self):
        """A TEST/INJECTED resolver must not silently receive the production wire.

        Factory-only issuance is bypassable if a wrapper around the exact product
        SecurityBoundary passes the public constructor and wire_client=None
        auto-selects UrllibJsonWireClient.  Construction must fail before any
        credential resolution or network call.
        """

        with TemporaryDirectory() as directory:
            journal = JournalStore(Path(directory) / "journal.sqlite3")
            helper = ProductionCredentialWireSeparationTests()
            for name, constructor, product_args in helper._cases(journal):
                with self.subTest(name=name):
                    args = dict(product_args)
                    args["secret_resolver"] = _DelegatingSecurityBoundaryProxy(
                        args["secret_resolver"]
                    )
                    with self.assertRaises((
                        ProviderTransportScopeError,
                        PermissionError,
                        TypeError,
                    )):
                        constructor(**args)



if __name__ == "__main__":
    unittest.main()
