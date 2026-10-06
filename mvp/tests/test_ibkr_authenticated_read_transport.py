from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta
import unittest

from mvp.autotrade_mvp.capabilities import (
    CapabilityRegistry,
    EvidenceVerification,
    derive_capability_snapshot,
)
from mvp.autotrade_mvp.provider_core import (
    Surface,
    prepare_authenticated_read_query,
)
from mvp.autotrade_mvp.provider_transport import (
    AuthenticatedReadWireResponse,
    IBKR_WEB_ENDPOINT_POLICIES,
    IbkrWebAuthenticatedReadSigner,
    IbkrWebAuthenticatedReadTransport,
    ProviderTransportScopeError,
    UrllibJsonWireClient,
)
from mvp.autotrade_mvp.windows_secrets import PersistentCredentialHandle
from mvp.tests.capability_test_support import fresh_test_admission
from mvp.tests.test_durable_capabilities import NOW, claim


def _ibkr_snapshot(snapshot_id, observed_at):
    claims = tuple(
        replace(
            claim(
                source,
                observed_at=observed_at,
                provider_id="IBKR",
            ),
            data_entitlements=frozenset({"ACCOUNT", "SESSION"}),
        )
        for source in ("DOCUMENTED", "API", "ACCOUNT", "INSTRUMENT")
    )
    return fresh_test_admission(
        derive_capability_snapshot(
            snapshot_id=snapshot_id,
            claims=claims,
            observed_at=observed_at,
            evidence_verifier=lambda _claim: EvidenceVerification(valid=True),
        )
    )


def _binding(snapshot, endpoint, at):
    return prepare_authenticated_read_query(
        capability=snapshot,
        surface=Surface.AUTHENTICATED_READ,
        endpoint=endpoint,
        query={},
        at=at,
        permission_scope="ORDER.READ",
    )


class _Wire:
    def __init__(self, body):
        self.body = body
        self.requests = []

    def send(self, request):
        self.requests.append(request)
        return AuthenticatedReadWireResponse(
            http_status=200,
            body=self.body,
        )


class _SecretResolver:
    def __init__(self, token="opaque.session-token", on_enter=None):
        self.token = token
        self.on_enter = on_enter
        self.calls = []

    @contextmanager
    def lease_for_execution(self, token, **kwargs):
        self.calls.append((token, kwargs))
        if self.on_enter is not None:
            self.on_enter()
        yield self.token


class IbkrWebAuthenticatedReadTransportTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = _ibkr_snapshot(
            "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            NOW,
        )
        self.at = NOW + timedelta(seconds=1)

    def test_status_uses_exact_oauth2_host_empty_post_and_bearer(self):
        request = IbkrWebAuthenticatedReadSigner.sign(
            policy=IBKR_WEB_ENDPOINT_POLICIES["PAPER"],
            query_binding=_binding(
                self.snapshot,
                "/iserver/auth/status",
                self.at,
            ),
            credential_plaintext="abc.DEF_123-opaque",
        )

        self.assertEqual(
            request.url,
            "https://api.ibkr.com/v1/api/iserver/auth/status",
        )
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.body, b"")
        self.assertEqual(
            request.headers["Authorization"],
            "Bearer abc.DEF_123-opaque",
        )
        self.assertEqual(request.headers["Content-Length"], "0")
        self.assertEqual(request.headers["Content-Type"], "application/json")

    def test_status_content_length_survives_urllib_wire_construction(self):
        class Response:
            status = 200

            def __init__(self):
                from io import BytesIO

                self.body = BytesIO(
                    b'{"success":{"value":{"connected":true}}}'
                )

            def read(self, size=-1):
                return self.body.read(size)

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        class Opener:
            def __init__(self):
                self.requests = []

            def open(self, request, *, timeout):
                self.requests.append((request, timeout))
                return Response()

        request = IbkrWebAuthenticatedReadSigner.sign(
            policy=IBKR_WEB_ENDPOINT_POLICIES["PAPER"],
            query_binding=_binding(
                self.snapshot,
                "/iserver/auth/status",
                self.at,
            ),
            credential_plaintext="opaque-token",
        )
        client = UrllibJsonWireClient(max_response_bytes=1024)
        opener = Opener()
        client._opener = opener

        response = client.send(request)

        self.assertEqual(response.http_status, 200)
        self.assertEqual(len(opener.requests), 1)
        outbound, timeout = opener.requests[0]
        self.assertEqual(outbound.get_method(), "POST")
        self.assertIsNone(outbound.data)
        self.assertEqual(outbound.get_header("Content-length"), "0")
        self.assertEqual(timeout, 15)

    def test_accounts_uses_exact_oauth2_host_queryless_get(self):
        request = IbkrWebAuthenticatedReadSigner.sign(
            policy=IBKR_WEB_ENDPOINT_POLICIES["PAPER"],
            query_binding=_binding(
                self.snapshot,
                "/iserver/accounts",
                self.at,
            ),
            credential_plaintext="opaque-token",
        )

        self.assertEqual(
            request.url,
            "https://api.ibkr.com/v1/api/iserver/accounts",
        )
        self.assertEqual(request.method, "GET")
        self.assertEqual(request.body, b"")
        self.assertNotIn("Content-Length", request.headers)
        self.assertNotIn("Content-Type", request.headers)

    def test_bearer_prefix_whitespace_and_wrong_environment_fail_closed(self):
        binding = _binding(
            self.snapshot,
            "/iserver/accounts",
            self.at,
        )
        for credential in (
            "Bearer already-prefixed",
            " leading",
            "trailing ",
            "contains space",
            "contains\nnewline",
        ):
            with self.subTest(credential=credential):
                with self.assertRaisesRegex(
                    ProviderTransportScopeError,
                    "canonical token text",
                ):
                    IbkrWebAuthenticatedReadSigner.sign(
                        policy=IBKR_WEB_ENDPOINT_POLICIES["PAPER"],
                        query_binding=binding,
                        credential_plaintext=credential,
                    )

        with self.assertRaisesRegex(
            ProviderTransportScopeError,
            "does not match exact environment",
        ):
            IbkrWebAuthenticatedReadSigner.sign(
                policy=IBKR_WEB_ENDPOINT_POLICIES["LIVE"],
                query_binding=binding,
                credential_plaintext="opaque-token",
            )

    def test_transport_leases_read_secret_rechecks_capability_and_observes_bytes(self):
        registry = CapabilityRegistry()
        registry.add(self.snapshot)
        resolver = _SecretResolver()
        wire = _Wire(
            b'{"success":{"value":{"connected":true,"authenticated":true,'
            b'"established":true,"competing":false}}}'
        )
        handle = PersistentCredentialHandle(
            account_id="paper-account",
            provider="IBKR",
            environment="PAPER",
            purpose="READ",
            generation=1,
        )
        transport = IbkrWebAuthenticatedReadTransport(
            policy=IBKR_WEB_ENDPOINT_POLICIES["PAPER"],
            account_id="paper-account",
            capability_snapshot_id=self.snapshot.snapshot_id,
            capability_registry=registry,
            secret_resolver=resolver,
            credential_handle=handle,
            session_token="vault-session",
            origin="test",
            execution_identity="read-status",
            clock_utc=lambda: NOW + timedelta(seconds=2),
            wire_client=wire,
        )
        binding = _binding(
            self.snapshot,
            "/iserver/auth/status",
            self.at,
        )

        observation = transport(binding)

        self.assertEqual(observation.provider_id, "IBKR")
        self.assertEqual(observation.account_id, "paper-account")
        self.assertEqual(observation.environment, "PAPER")
        self.assertEqual(len(wire.requests), 1)
        self.assertEqual(wire.requests[0].method, "POST")
        self.assertEqual(len(resolver.calls), 1)
        _session, lease = resolver.calls[0]
        self.assertEqual(lease["provider"], "IBKR")
        self.assertEqual(lease["purpose"], "READ")
        self.assertEqual(lease["provider_environment"], "PAPER")

    def test_capability_change_during_secret_lease_blocks_wire(self):
        registry = CapabilityRegistry()
        registry.add(self.snapshot)

        def supersede():
            registry.add(
                _ibkr_snapshot(
                    "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                    NOW + timedelta(seconds=2),
                )
            )

        resolver = _SecretResolver(on_enter=supersede)
        wire = _Wire(b'{"accounts":["paper-account"]}')
        handle = PersistentCredentialHandle(
            account_id="paper-account",
            provider="IBKR",
            environment="PAPER",
            purpose="READ",
            generation=1,
        )
        transport = IbkrWebAuthenticatedReadTransport(
            policy=IBKR_WEB_ENDPOINT_POLICIES["PAPER"],
            account_id="paper-account",
            capability_snapshot_id=self.snapshot.snapshot_id,
            capability_registry=registry,
            secret_resolver=resolver,
            credential_handle=handle,
            session_token="vault-session",
            origin="test",
            execution_identity="read-accounts",
            clock_utc=lambda: NOW + timedelta(seconds=3),
            wire_client=wire,
        )
        binding = _binding(
            self.snapshot,
            "/iserver/accounts",
            self.at,
        )

        with self.assertRaisesRegex(
            ProviderTransportScopeError,
            "no longer valid",
        ):
            transport(binding)

        self.assertEqual(wire.requests, [])


if __name__ == "__main__":
    unittest.main()
