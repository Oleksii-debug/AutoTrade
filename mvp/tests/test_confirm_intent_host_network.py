from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
import http.client
import json
from pathlib import Path
import socket
from tempfile import TemporaryDirectory
from threading import Thread
import unittest

from mvp.autotrade_mvp.authority import (
    AuthorityPolicy,
    AuthorityService,
    InstrumentVersionIdentity,
)
from mvp.autotrade_mvp.host_network import (
    AuthenticatedHostApplication,
    AuthenticatedHostServer,
    header_principal_resolver,
    public_session_reference,
)
from mvp.autotrade_mvp.pending_intent_financial_binding import (
    DurablePendingIntentFinancialBindingRegistry,
)
from mvp.autotrade_mvp.pending_intents import DurablePendingIntentRegistry
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.risk import RiskIntent, RiskPolicy
from mvp.autotrade_mvp.risk_policy_authority import (
    DurableRiskPolicyRegistry,
    RiskPolicyScope,
)
from mvp.autotrade_mvp.security import SecurityBoundary
from mvp.autotrade_mvp.windows_secrets import ProtectedCredentialVault


NOW = datetime(2026, 10, 5, 1, 30, tzinfo=timezone.utc)
NOW_TEXT = NOW.isoformat().replace("+00:00", "Z")
INSTRUMENT_ID = "11111111-1111-1111-1111-111111111111"
COMMAND_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


class DeterministicProtector:
    PREFIX = b"confirm-intent-network-test-v1:"

    def protect(self, plaintext: bytes, *, entropy: bytes) -> bytes:
        return self.PREFIX + sha256(entropy).digest() + plaintext[::-1]

    def unprotect(self, ciphertext: bytes, *, entropy: bytes) -> bytes:
        expected = self.PREFIX + sha256(entropy).digest()
        if not ciphertext.startswith(expected):
            raise OSError("scope entropy mismatch")
        return ciphertext[len(expected) :][::-1]


class ConfirmIntentHostNetworkTests(unittest.TestCase):
    @staticmethod
    def _risk_policy() -> RiskPolicy:
        return RiskPolicy.create(
            max_abs_position="100",
            max_single_notional="1000",
            max_gross_leverage="5",
            max_net_leverage="5",
            max_daily_loss="500",
            max_drawdown_fraction="0.5",
            max_data_age_seconds="30",
            max_fx_age_seconds="30",
            min_margin_headroom="0.1",
            max_stress_loss="500",
            allowed_actions=["TRADE"],
        )

    @staticmethod
    def _scope() -> RiskPolicyScope:
        return RiskPolicyScope(
            provider_id="TEST",
            account_id="acct-1",
            environment="PAPER",
            provider_environment="PAPER",
            entity_policy_id="entity-policy-1",
            instrument_family="SPOT",
        )

    def _setup_financial_authority(self, store: JournalStore) -> None:
        authority = AuthorityService(store)
        authority.register_policy(
            AuthorityPolicy.create(
                policy_id="authority-policy-1",
                account_id="acct-1",
                environments={"PAPER"},
                instruments={InstrumentVersionIdentity(INSTRUMENT_ID, 9)},
                actions={"ORDER.SUBMIT"},
                max_notional="1000",
                valid_from=(NOW - timedelta(minutes=5)).isoformat().replace(
                    "+00:00", "Z"
                ),
                expires_at=(NOW + timedelta(hours=1)).isoformat().replace(
                    "+00:00", "Z"
                ),
                autonomous=False,
                protection_only=False,
                version=3,
            )
        )
        risk_registry = DurableRiskPolicyRegistry(store)
        scope = self._scope()
        risk_registry.register(
            scope=scope,
            policy_id="risk-policy-1",
            version=1,
            policy=self._risk_policy(),
            committed_at=NOW - timedelta(seconds=4),
        )
        risk_registry.activate(
            scope=scope,
            policy_id="risk-policy-1",
            version=1,
            committed_at=NOW - timedelta(seconds=3),
        )
        pending = DurablePendingIntentRegistry(store)
        pending.register(
            pending_intent_id="pending-host-network-confirm-1",
            account_id="acct-1",
            environment="PAPER",
            policy_id="authority-policy-1",
            authority_policy_version=3,
            instrument_id=INSTRUMENT_ID,
            instrument_version=9,
            authority_action="ORDER.SUBMIT",
            notional="202.50",
            risk_intent=RiskIntent.create(
                symbol="BTCUSD",
                side="BUY",
                quantity="2",
                price="101.25",
                expected_state_version=7,
                reduce_only=False,
                action="TRADE",
                instrument_type="SPOT",
            ),
            registered_at=NOW - timedelta(seconds=1),
            expires_at=NOW + timedelta(minutes=10),
        )
        DurablePendingIntentFinancialBindingRegistry(store).bind(
            pending,
            pending_intent_id="pending-host-network-confirm-1",
            account_id="acct-1",
            environment="PAPER",
            authority_policy_id="authority-policy-1",
            authority_policy_version=3,
            resolved_risk_policy=risk_registry.resolve_current(scope),
            reservation_requirements={"CASH:USD": "202.50"},
            at=NOW,
        )

    @staticmethod
    def _command(session_token: str, *, payload: dict[str, object]) -> dict[str, object]:
        return {
            "command_id": COMMAND_ID,
            "expected_state_version": "0",
            "idempotency_key": "confirm-intent-network-key-1",
            "actor": "owner-1",
            "session": public_session_reference(session_token),
            "account_id": "acct-1",
            "environment": "PAPER",
            "action": "CONFIRM_INTENT",
            "payload": payload,
        }

    def test_http_confirm_intent_rejects_financial_override_then_executes_server_owned_intent(self) -> None:
        with TemporaryDirectory() as directory:
            probe = socket.socket()
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
            probe.close()
            origin = f"http://127.0.0.1:{port}"
            path = str(Path(directory) / "journal.sqlite3")
            self._setup_financial_authority(JournalStore(path))

            vault = ProtectedCredentialVault(
                Path(directory) / "credentials.json",
                protector=DeterministicProtector(),
            )
            boundary = SecurityBoundary(
                allowed_origins={origin},
                credential_vault=vault,
                session_authorizer=lambda subject, role, paired_origin: True,
                now=lambda: 1000.0,
            )
            owner = boundary.create_session(
                subject="owner-1",
                role="OWNER",
                origin=origin,
                ttl_seconds=600,
            )
            app = AuthenticatedHostApplication(
                JournalStore(path),
                security_boundary=boundary,
                account_id="acct-1",
                environment="PAPER",
                host_id="host-confirm-network-1",
                public_origin=origin,
                principal_resolver=header_principal_resolver,
                snapshot_provider=lambda durable, principal: {},
                now=lambda: NOW_TEXT,
            )
            server = AuthenticatedHostServer(("127.0.0.1", port), app)
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(server.shutdown)
            self.addCleanup(server.server_close)

            headers = {
                "Authorization": "AutoTrade-Session " + owner.token,
                "X-AutoTrade-Actor": "owner-1",
                "Origin": origin,
                "Accept": "application/json",
                "Content-Type": "application/json",
            }
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
            self.addCleanup(connection.close)

            injected = self._command(
                owner.token,
                payload={
                    "pending_intent_id": "pending-host-network-confirm-1",
                    "notional": "999999",
                },
            )
            connection.request(
                "POST",
                "/api/v1/commands",
                body=json.dumps(injected).encode("utf-8"),
                headers=headers,
            )
            rejected_response = connection.getresponse()
            rejected = json.loads(rejected_response.read().decode("utf-8"))
            self.assertEqual(rejected_response.status, 400)
            self.assertEqual(rejected, {"error": "INVALID_REQUEST"})
            self.assertEqual(app.store.state_version, 0)
            self.assertNotIn(COMMAND_ID, AuthorityService(JournalStore(path))._confirmations)

            canonical = self._command(
                owner.token,
                payload={"pending_intent_id": "pending-host-network-confirm-1"},
            )
            connection.request(
                "POST",
                "/api/v1/commands",
                body=json.dumps(canonical).encode("utf-8"),
                headers=headers,
            )
            accepted_response = connection.getresponse()
            accepted = json.loads(accepted_response.read().decode("utf-8"))
            self.assertEqual(accepted_response.status, 200)
            self.assertEqual(accepted["status"], "ACCEPTED")
            operation_id = accepted["operation_id"]

            connection.request(
                "GET",
                "/api/v1/operations/" + operation_id,
                headers={
                    "Authorization": "AutoTrade-Session " + owner.token,
                    "X-AutoTrade-Actor": "owner-1",
                    "Origin": origin,
                    "Accept": "application/json",
                },
            )
            operation_response = connection.getresponse()
            operation = json.loads(operation_response.read().decode("utf-8"))
            self.assertEqual(operation_response.status, 200)
            self.assertEqual(operation["phase"], "SUCCEEDED")
            self.assertEqual(operation["remaining_uncertainty"], [])
            self.assertEqual(
                operation["affected_refs"],
                ["authority-confirmation:" + COMMAND_ID],
            )
            self.assertEqual(
                operation["evidence"][0]["event_type"],
                "AuthorityConfirmationAdded",
            )

            confirmation = AuthorityService(JournalStore(path))._confirmations[COMMAND_ID]
            self.assertEqual(confirmation.account_id, "acct-1")
            self.assertEqual(str(confirmation.notional), "202.50")
            self.assertIsNotNone(confirmation.financial_binding_hash)

            rendered = json.dumps(operation, sort_keys=True)
            self.assertNotIn(owner.token, rendered)
            self.assertNotIn("999999", rendered)


if __name__ == "__main__":
    unittest.main()
