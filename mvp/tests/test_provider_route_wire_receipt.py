from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from autotrade_runtime.artifacts import ArtifactStore

from mvp.autotrade_mvp.capabilities import (
    CapabilityClaim,
    EvidenceVerification,
    derive_capability_snapshot,
)
from mvp.autotrade_mvp.durable_capabilities import DurableCapabilityRegistry
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.provider_core import Surface
from mvp.autotrade_mvp.provider_route_reads import prepare_qualified_provider_read
from mvp.autotrade_mvp.provider_route_wire_receipt import (
    QualifiedBybitReadWireClient,
    QualifiedProviderReadWireError,
    QualifiedProviderReadWireReceipt,
)
from mvp.autotrade_mvp.provider_selection import select_provider
from mvp.autotrade_mvp.provider_transport import (
    AuthenticatedReadHttpRequest,
    UrllibJsonWireClient,
)
from mvp.tests.capability_test_support import fresh_test_admission
from mvp.tests.provider_qualification_test_support import (
    ExactQualificationProjectionHarness,
)
from mvp.tests.test_provider_route_dispatch import successor_spot_q
from mvp.tests.test_provider_selection import (
    NOW,
    accepted_spot_q,
    candidate,
    request as route_request,
)


_ARTIFACTS = {
    "DOCUMENTED": "71111111-1111-4111-8111-111111111111",
    "API": "72222222-2222-4222-8222-222222222222",
    "ACCOUNT": "73333333-3333-4333-8333-333333333333",
    "INSTRUMENT": "74444444-4444-4444-8444-444444444444",
}


def read_capability(snapshot_id, observed_at):
    claims = tuple(
        CapabilityClaim(
            source=source,
            provider_id="BYBIT",
            account_id="paper-account",
            entity_id="entity-1",
            environment="PAPER",
            provider_environment="TESTNET",
            instrument_version="instrument-v1",
            observed_at=observed_at,
            expires_at=observed_at + timedelta(minutes=10),
            supported_order_types=frozenset({"LIMIT"}),
            time_in_force=frozenset({"DAY"}),
            permission_scopes=frozenset({"ORDER.WRITE", "ACCOUNT.READ"}),
            position_mode="NET",
            native_protection=frozenset({"STOP_LOSS"}),
            rate_limit_policy_id="bybit-testnet-v1",
            data_entitlements=frozenset({"QUOTE", "BALANCES"}),
            evidence_ref={
                "artifact_id": _ARTIFACTS[source],
                "sha256": "sha256:" + {
                    "DOCUMENTED": "7",
                    "API": "8",
                    "ACCOUNT": "9",
                    "INSTRUMENT": "a",
                }[source] * 64,
                "observed_at": observed_at.isoformat().replace("+00:00", "Z"),
            },
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


class _ExactHttpResponse:
    def __init__(self, body=b'{"retCode":0,"result":{"balance":"10"}}', status=200):
        self._body = body
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit):
        return self._body


class _RecordingOpener:
    def __init__(self, *, body=b'{"retCode":0,"result":{"balance":"10"}}', status=200):
        self.body = body
        self.status = status
        self.calls = []

    def open(self, request, *, timeout):
        self.calls.append((request, timeout))
        return _ExactHttpResponse(self.body, self.status)


class QualifiedProviderWireReceiptTests(unittest.TestCase):
    def setup_route(self, directory):
        journal = JournalStore(Path(directory) / "journal.sqlite3")
        capabilities = DurableCapabilityRegistry(journal)
        capabilities.add(
            read_capability(
                "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                NOW - timedelta(minutes=1),
            )
        )
        evidence_root = Path(directory) / "evidence"
        harness = ExactQualificationProjectionHarness().start()
        self.addCleanup(harness.stop)
        qualifications = harness.registry(
            journal,
            evidence_store=ArtifactStore(evidence_root),
            evidence_root=evidence_root,
        )
        q1, receipt1, protocol1 = accepted_spot_q(
            ordinal=60,
            include_read_rule=True,
        )
        harness.register(
            protocol_key=protocol1.key,
            record=q1,
            receipt=receipt1,
        )
        qualifications._append_accepted(
            protocol_key=protocol1.key,
            record=q1,
            receipt=receipt1,
        )
        selection = select_provider(
            route_request(),
            [candidate()],
            at=NOW,
            capability_registry=capabilities,
            qualification_registry=qualifications,
        )
        self.assertEqual(selection.status, "SELECTED_UNAMBIGUOUS")
        route = selection.selected
        self.assertIsNotNone(route)
        binding = prepare_qualified_provider_read(
            route,
            capabilities,
            qualifications,
            surface=Surface.AUTHENTICATED_READ,
            endpoint="/v5/account/wallet-balance",
            query={"accountType": "UNIFIED"},
            at=NOW,
            permission_scope="ACCOUNT.READ",
        )
        return journal, capabilities, qualifications, route, binding, q1, harness

    @staticmethod
    def request():
        return AuthenticatedReadHttpRequest(
            url=(
                "https://api-testnet.bybit.com/v5/account/wallet-balance"
                "?accountType=UNIFIED"
            ),
            headers={
                "Accept": "application/json",
                "X-BAPI-API-KEY": "api-key",
                "X-BAPI-TIMESTAMP": "1700000000000",
                "X-BAPI-RECV-WINDOW": "5000",
                "X-BAPI-SIGN": "a" * 64,
            },
            timeout_seconds=15,
        )

    def terminal(self, route, binding, capabilities, qualifications, *, at=NOW):
        delegate = UrllibJsonWireClient()
        opener = _RecordingOpener()
        delegate._opener = opener
        terminal = QualifiedBybitReadWireClient(
            route=route,
            query_binding=binding,
            capability_registry=capabilities,
            qualification_registry=qualifications,
            delegate=delegate,
            clock_utc=lambda: at,
        )
        return terminal, opener

    def test_terminal_send_mints_exact_sealed_receipt(self):
        with TemporaryDirectory() as directory:
            _journal, capabilities, qualifications, route, binding, _q1, _harness = (
                self.setup_route(directory)
            )
            terminal, opener = self.terminal(
                route,
                binding,
                capabilities,
                qualifications,
            )
            response = terminal.send(self.request())
            self.assertEqual(response.http_status, 200)
            self.assertEqual(len(opener.calls), 1)
            receipt = terminal.receipt
            self.assertIsNotNone(receipt)
            self.assertIs(receipt.query_binding, binding)
            self.assertEqual(receipt.http_status, 200)
            self.assertEqual(receipt.response_bytes, response.body)
            self.assertTrue(receipt.response_sha256.startswith("sha256:"))
            self.assertTrue(receipt.request_sha256.startswith("sha256:"))
            self.assertTrue(
                receipt.receipt_id.startswith("qualified-provider-wire:sha256:")
            )

    def test_receipt_constructor_and_object_new_forgery_have_no_authority(self):
        with self.assertRaisesRegex(
            QualifiedProviderReadWireError,
            "must come from terminal transport",
        ):
            QualifiedProviderReadWireReceipt()
        forged = object.__new__(QualifiedProviderReadWireReceipt)
        with self.assertRaisesRegex(
            QualifiedProviderReadWireError,
            "authority is unavailable",
        ):
            _ = forged.receipt_id

    def test_q_superseded_after_prepare_blocks_before_network(self):
        with TemporaryDirectory() as directory:
            _journal, capabilities, qualifications, route, binding, q1, harness = (
                self.setup_route(directory)
            )
            q2, receipt2, protocol2 = successor_spot_q(
                old_qualification_id=q1.qualification_id,
                ordinal=61,
            )
            harness.register(
                protocol_key=protocol2.key,
                record=q2,
                receipt=receipt2,
            )
            qualifications._append_accepted(
                protocol_key=protocol2.key,
                record=q2,
                receipt=receipt2,
            )
            qualifications._append_supersession(
                old_id=q1.qualification_id,
                new_id=q2.qualification_id,
            )
            terminal, opener = self.terminal(
                route,
                binding,
                capabilities,
                qualifications,
                at=NOW + timedelta(seconds=2),
            )
            with self.assertRaisesRegex(
                QualifiedProviderReadWireError,
                "qualification is not exact current",
            ):
                terminal.send(self.request())
            self.assertEqual(opener.calls, [])
            self.assertIsNone(terminal.receipt)

    def test_c_superseded_after_prepare_blocks_before_network(self):
        with TemporaryDirectory() as directory:
            _journal, capabilities, qualifications, route, binding, _q1, _harness = (
                self.setup_route(directory)
            )
            capabilities.add(
                read_capability(
                    "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                    NOW + timedelta(seconds=30),
                )
            )
            terminal, opener = self.terminal(
                route,
                binding,
                capabilities,
                qualifications,
                at=NOW + timedelta(minutes=1),
            )
            with self.assertRaisesRegex(
                QualifiedProviderReadWireError,
                "does not authorize exact qualified read",
            ):
                terminal.send(self.request())
            self.assertEqual(opener.calls, [])
            self.assertIsNone(terminal.receipt)

    def test_request_relabel_is_rejected_before_network(self):
        with TemporaryDirectory() as directory:
            _journal, capabilities, qualifications, route, binding, _q1, _harness = (
                self.setup_route(directory)
            )
            terminal, opener = self.terminal(
                route,
                binding,
                capabilities,
                qualifications,
            )
            forged = AuthenticatedReadHttpRequest(
                url="https://api-testnet.bybit.com/v5/order/realtime?category=spot",
                headers=self.request().headers,
                timeout_seconds=15,
            )
            with self.assertRaisesRegex(
                QualifiedProviderReadWireError,
                "differs from exact qualified Bybit query",
            ):
                terminal.send(forged)
            self.assertEqual(opener.calls, [])
            self.assertIsNone(terminal.receipt)

    def test_receipt_mutation_is_detected(self):
        with TemporaryDirectory() as directory:
            _journal, capabilities, qualifications, route, binding, _q1, _harness = (
                self.setup_route(directory)
            )
            terminal, _opener = self.terminal(
                route,
                binding,
                capabilities,
                qualifications,
            )
            terminal.send(self.request())
            receipt = terminal.receipt
            self.assertIsNotNone(receipt)
            object.__setattr__(receipt, "http_status", 201)
            with self.assertRaisesRegex(
                QualifiedProviderReadWireError,
                "changed after send",
            ):
                _ = receipt.receipt_id

    def test_terminal_client_is_one_shot_no_retry(self):
        with TemporaryDirectory() as directory:
            _journal, capabilities, qualifications, route, binding, _q1, _harness = (
                self.setup_route(directory)
            )
            terminal, opener = self.terminal(
                route,
                binding,
                capabilities,
                qualifications,
            )
            terminal.send(self.request())
            with self.assertRaisesRegex(
                QualifiedProviderReadWireError,
                "one-shot",
            ):
                terminal.send(self.request())
            self.assertEqual(len(opener.calls), 1)


if __name__ == "__main__":
    unittest.main()
