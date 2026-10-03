from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from types import MappingProxyType
import threading
import unittest
from unittest.mock import patch

from mvp.autotrade_mvp.host_api import HostCommandStore
from mvp.autotrade_mvp.security import SecurityBoundary
from mvp.autotrade_mvp.windows_secrets import ProtectedCredentialVault


class DeterministicProtector:
    PREFIX = b"security-boundary-test-v1:"

    def protect(self, plaintext: bytes, *, entropy: bytes) -> bytes:
        return self.PREFIX + sha256(entropy).digest() + plaintext[::-1]

    def unprotect(self, ciphertext: bytes, *, entropy: bytes) -> bytes:
        expected = self.PREFIX + sha256(entropy).digest()
        if not ciphertext.startswith(expected):
            raise OSError("scope entropy mismatch")
        return ciphertext[len(expected):][::-1]


class SecurityBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.vault_path = Path(self.directory.name) / "credentials.json"
        self.vault = ProtectedCredentialVault(
            self.vault_path,
            protector=DeterministicProtector(),
        )
        self.clock = [1000.0]
        self.session_authorizer = lambda subject, role, origin: True
        self.boundary = SecurityBoundary(
            allowed_origins={"https://local.autotrade.invalid"},
            credential_vault=self.vault,
            session_authorizer=self.session_authorizer,
            now=lambda: self.clock[0],
        )
        self.owner = self.boundary.create_session(
            subject="owner",
            role="OWNER",
            origin="https://local.autotrade.invalid",
            ttl_seconds=60,
        )

    def _credential(self):
        return self.boundary.register_secret(
            self.owner.token,
            origin=self.owner.origin,
            owner_identity="windows-user-1",
            account_id="paper-1",
            provider="SIMULATED",
            environment="PAPER",
            purpose="TRADE",
            secret_value="top-secret",
        )

    def test_empty_identity_and_scope_are_rejected(self):
        for field, overrides in (
            ("owner_identity", {"owner_identity": ""}),
            ("account_id", {"account_id": "   "}),
            ("provider", {"provider": ""}),
            ("purpose", {"purpose": ""}),
        ):
            values = {
                "owner_identity": "windows-user-1",
                "account_id": "paper-1",
                "provider": "SIMULATED",
                "environment": "PAPER",
                "purpose": "TRADE",
            }
            values.update(overrides)
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.boundary.register_secret(
                    self.owner.token,
                    origin=self.owner.origin,
                    secret_value="top-secret",
                    **values,
                )

    def test_researcher_cannot_resolve_trade_secret(self):
        handle = self._credential()
        researcher = self.boundary.create_session(
            subject="research",
            role="RESEARCHER",
            origin=self.owner.origin,
        )
        with self.assertRaises(PermissionError):
            self.boundary.resolve_for_execution(
                researcher.token,
                origin=researcher.origin,
                handle=handle,
                execution_identity="windows-user-1",
                account_id="paper-1",
                provider="SIMULATED",
                environment="PAPER",
                purpose="TRADE",
            )

    def test_wrong_identity_and_scope_fail_closed(self):
        handle = self._credential()
        with self.assertRaises(PermissionError):
            self.boundary.resolve_for_execution(
                self.owner.token,
                origin=self.owner.origin,
                handle=handle,
                execution_identity="windows-user-2",
                account_id="paper-1",
                provider="SIMULATED",
                environment="PAPER",
                purpose="TRADE",
            )
        with self.assertRaises(PermissionError):
            self.boundary.resolve_for_execution(
                self.owner.token,
                origin=self.owner.origin,
                handle=handle,
                execution_identity="windows-user-1",
                account_id="other",
                provider="SIMULATED",
                environment="PAPER",
                purpose="TRADE",
            )

    def test_paired_origin_alone_cannot_mint_privileged_session(self):
        boundary = SecurityBoundary(
            allowed_origins={"https://local.autotrade.invalid"},
            credential_vault=self.vault,
        )
        with self.assertRaisesRegex(PermissionError, "verifier"):
            boundary.create_session(
                subject="owner",
                role="OWNER",
                origin="https://local.autotrade.invalid",
            )

    def test_session_authorizer_binds_normalized_identity_role_and_origin(self):
        observed = []

        def authorize(subject, role, origin):
            observed.append((subject, role, origin))
            return subject == "alice" and role == "OPERATOR"

        boundary = SecurityBoundary(
            allowed_origins={"https://LOCAL.AUTOTRADE.INVALID:443/"},
            credential_vault=self.vault,
            session_authorizer=authorize,
            now=lambda: self.clock[0],
        )
        session = boundary.create_session(
            subject=" alice ",
            role="operator",
            origin="https://local.autotrade.invalid/",
        )
        self.assertEqual(session.subject, "alice")
        self.assertEqual(session.role, "OPERATOR")
        self.assertEqual(session.origin, "https://local.autotrade.invalid")
        self.assertEqual(
            observed,
            [("alice", "OPERATOR", "https://local.autotrade.invalid")],
        )
        with self.assertRaisesRegex(PermissionError, "not authenticated"):
            boundary.create_session(
                subject="alice",
                role="OWNER",
                origin="https://local.autotrade.invalid",
            )

    def test_session_authorizer_failure_is_fail_closed(self):
        def broken_authorizer(subject, role, origin):
            raise RuntimeError("identity provider unavailable")

        boundary = SecurityBoundary(
            allowed_origins={"https://local.autotrade.invalid"},
            credential_vault=self.vault,
            session_authorizer=broken_authorizer,
        )
        with self.assertRaisesRegex(PermissionError, "authentication failed"):
            boundary.create_session(
                subject="owner",
                role="OWNER",
                origin="https://local.autotrade.invalid",
            )

    def test_expired_session_is_rejected(self):
        handle = self._credential()
        self.clock[0] = 1060.0
        with self.assertRaisesRegex(PermissionError, "expired"):
            self.boundary.resolve_for_execution(
                self.owner.token,
                origin=self.owner.origin,
                handle=handle,
                execution_identity="windows-user-1",
                account_id="paper-1",
                provider="SIMULATED",
                environment="PAPER",
                purpose="TRADE",
            )

    def test_idle_timeout_expires_without_activity_and_successful_use_refreshes_it(self):
        session = self.boundary.create_session(
            subject="operator-idle",
            role="OPERATOR",
            origin=self.owner.origin,
            ttl_seconds=60,
            idle_timeout_seconds=10,
        )
        self.clock[0] = 1009.0
        touched = self.boundary.validate_session(
            session.token,
            origin=session.origin,
        )
        self.assertEqual(touched.idle_expires_at, 1019.0)

        self.clock[0] = 1018.0
        touched_again = self.boundary.validate_session(
            session.token,
            origin=session.origin,
        )
        self.assertEqual(touched_again.idle_expires_at, 1028.0)

        self.clock[0] = 1028.0
        with self.assertRaisesRegex(PermissionError, "idle timeout"):
            self.boundary.validate_session(session.token, origin=session.origin)
        with self.assertRaisesRegex(PermissionError, "Unknown session"):
            self.boundary.validate_session(session.token, origin=session.origin)

    def test_failed_origin_replay_does_not_keep_idle_session_alive(self):
        session = self.boundary.create_session(
            subject="operator-idle-origin",
            role="OPERATOR",
            origin=self.owner.origin,
            ttl_seconds=60,
            idle_timeout_seconds=10,
        )
        self.clock[0] = 1008.0
        with self.assertRaisesRegex(PermissionError, "origin"):
            self.boundary.validate_session(
                session.token,
                origin="https://evil.invalid",
            )
        self.clock[0] = 1010.0
        with self.assertRaisesRegex(PermissionError, "idle timeout"):
            self.boundary.validate_session(session.token, origin=session.origin)

    def test_refresh_reauthenticates_same_scope_rotates_token_and_revokes_old_token(self):
        observed = []

        def authorize(subject, role, origin):
            observed.append((subject, role, origin))
            return True

        boundary = SecurityBoundary(
            allowed_origins={self.owner.origin},
            credential_vault=self.vault,
            session_authorizer=authorize,
            now=lambda: self.clock[0],
        )
        session = boundary.create_session(
            subject="operator-refresh",
            role="OPERATOR",
            origin=self.owner.origin,
            ttl_seconds=60,
            idle_timeout_seconds=15,
        )
        self.clock[0] = 1005.0
        refreshed = boundary.refresh_session(
            session.token,
            origin=session.origin,
            ttl_seconds=120,
            idle_timeout_seconds=20,
        )
        self.assertNotEqual(refreshed.token, session.token)
        self.assertEqual(refreshed.subject, session.subject)
        self.assertEqual(refreshed.role, session.role)
        self.assertEqual(refreshed.origin, session.origin)
        self.assertEqual(refreshed.expires_at, 1125.0)
        self.assertEqual(refreshed.idle_expires_at, 1025.0)
        self.assertEqual(
            observed,
            [
                ("operator-refresh", "OPERATOR", self.owner.origin),
                ("operator-refresh", "OPERATOR", self.owner.origin),
            ],
        )
        with self.assertRaisesRegex(PermissionError, "Unknown session"):
            boundary.validate_session(session.token, origin=session.origin)
        self.assertEqual(
            boundary.validate_session(
                refreshed.token,
                required_roles={"OPERATOR"},
                origin=refreshed.origin,
            ).role,
            "OPERATOR",
        )

    def test_refresh_authentication_failure_keeps_existing_session_valid(self):
        calls = [0]

        def authorize(subject, role, origin):
            calls[0] += 1
            return calls[0] == 1

        boundary = SecurityBoundary(
            allowed_origins={self.owner.origin},
            credential_vault=self.vault,
            session_authorizer=authorize,
            now=lambda: self.clock[0],
        )
        session = boundary.create_session(
            subject="operator-refresh-fail",
            role="OPERATOR",
            origin=self.owner.origin,
            ttl_seconds=60,
            idle_timeout_seconds=20,
        )
        self.clock[0] = 1005.0
        with self.assertRaisesRegex(PermissionError, "not authenticated"):
            boundary.refresh_session(
                session.token,
                origin=session.origin,
                ttl_seconds=60,
            )
        self.clock[0] = 1020.0
        with self.assertRaisesRegex(PermissionError, "idle timeout"):
            boundary.validate_session(
                session.token,
                required_roles={"OPERATOR"},
                origin=session.origin,
            )

    def test_failed_refresh_does_not_roll_back_newer_concurrent_idle_state(self):
        entered = threading.Event()
        release = threading.Event()
        calls = [0]

        def authorize(subject, role, origin):
            calls[0] += 1
            if calls[0] == 2:
                entered.set()
                if not release.wait(timeout=2):
                    raise RuntimeError("test authorization barrier timed out")
                return False
            return True

        boundary = SecurityBoundary(
            allowed_origins={self.owner.origin},
            credential_vault=self.vault,
            session_authorizer=authorize,
            now=lambda: self.clock[0],
        )
        session = boundary.create_session(
            subject="operator-refresh-cas",
            role="OPERATOR",
            origin=self.owner.origin,
            ttl_seconds=60,
            idle_timeout_seconds=20,
        )
        self.clock[0] = 1005.0

        with ThreadPoolExecutor(max_workers=2) as executor:
            refresh = executor.submit(
                boundary.refresh_session,
                session.token,
                origin=session.origin,
                ttl_seconds=60,
            )
            self.assertTrue(entered.wait(timeout=1))
            self.clock[0] = 1010.0
            touched = boundary.validate_session(
                session.token,
                required_roles={"OPERATOR"},
                origin=session.origin,
            )
            self.assertEqual(touched.idle_expires_at, 1030.0)
            release.set()
            with self.assertRaisesRegex(PermissionError, "not authenticated"):
                refresh.result(timeout=2)

        self.clock[0] = 1026.0
        still_valid = boundary.validate_session(
            session.token,
            required_roles={"OPERATOR"},
            origin=session.origin,
        )
        self.assertEqual(still_valid.subject, session.subject)

    def test_concurrent_refreshes_mint_at_most_one_successor(self):
        barrier = threading.Barrier(2)
        block_refresh = [False]

        def authorize(subject, role, origin):
            if block_refresh[0]:
                barrier.wait(timeout=2)
            return True

        boundary = SecurityBoundary(
            allowed_origins={self.owner.origin},
            credential_vault=self.vault,
            session_authorizer=authorize,
            now=lambda: self.clock[0],
        )
        session = boundary.create_session(
            subject="operator-double-refresh",
            role="OPERATOR",
            origin=self.owner.origin,
        )
        block_refresh[0] = True

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(
                    boundary.refresh_session,
                    session.token,
                    origin=session.origin,
                )
                for _ in range(2)
            ]
            outcomes = []
            for future in futures:
                try:
                    outcomes.append(("ok", future.result(timeout=2)))
                except PermissionError as error:
                    outcomes.append(("denied", str(error)))

        winners = [value for status, value in outcomes if status == "ok"]
        denied = [value for status, value in outcomes if status == "denied"]
        self.assertEqual(len(winners), 1)
        self.assertEqual(len(denied), 1)
        self.assertIn("Unknown session", denied[0])
        self.assertNotEqual(winners[0].token, session.token)
        with self.assertRaisesRegex(PermissionError, "Unknown session"):
            boundary.validate_session(session.token, origin=session.origin)
        self.assertEqual(
            boundary.validate_session(
                winners[0].token,
                required_roles={"OPERATOR"},
                origin=winners[0].origin,
            ).subject,
            session.subject,
        )

    def test_refresh_cannot_revive_a_concurrently_revoked_session(self):
        entered = threading.Event()
        release = threading.Event()
        block_refresh = [False]

        def authorize(subject, role, origin):
            if block_refresh[0]:
                entered.set()
                if not release.wait(timeout=2):
                    raise RuntimeError("test authorization barrier timed out")
            return True

        boundary = SecurityBoundary(
            allowed_origins={self.owner.origin},
            credential_vault=self.vault,
            session_authorizer=authorize,
            now=lambda: self.clock[0],
        )
        session = boundary.create_session(
            subject="operator-refresh-revoke",
            role="OPERATOR",
            origin=self.owner.origin,
        )
        block_refresh[0] = True

        with ThreadPoolExecutor(max_workers=2) as executor:
            refresh = executor.submit(
                boundary.refresh_session,
                session.token,
                origin=session.origin,
            )
            self.assertTrue(entered.wait(timeout=1))
            boundary.revoke_session(session.token)
            release.set()
            with self.assertRaisesRegex(PermissionError, "Unknown session"):
                refresh.result(timeout=2)

        with self.assertRaisesRegex(PermissionError, "Unknown session"):
            boundary.validate_session(session.token, origin=session.origin)

    def test_session_creation_cannot_cross_concurrent_origin_unpair(self):
        entered = threading.Event()
        release = threading.Event()

        def authorize(subject, role, origin):
            if subject == "blocked-operator":
                entered.set()
                if not release.wait(timeout=2):
                    raise RuntimeError("test authorization barrier timed out")
            return True

        boundary = SecurityBoundary(
            allowed_origins={self.owner.origin},
            credential_vault=self.vault,
            session_authorizer=authorize,
            now=lambda: self.clock[0],
        )
        owner = boundary.create_session(
            subject="owner",
            role="OWNER",
            origin=self.owner.origin,
        )
        paired = boundary.pair_origin(
            owner.token,
            origin=owner.origin,
            new_origin="https://paired.autotrade.invalid",
        )

        with ThreadPoolExecutor(max_workers=2) as executor:
            create = executor.submit(
                boundary.create_session,
                subject="blocked-operator",
                role="OPERATOR",
                origin=paired,
            )
            self.assertTrue(entered.wait(timeout=1))
            boundary.unpair_origin(
                owner.token,
                origin=owner.origin,
                paired_origin=paired,
            )
            release.set()
            with self.assertRaisesRegex(PermissionError, "no longer paired"):
                create.result(timeout=2)

        with self.assertRaisesRegex(PermissionError, "not paired"):
            boundary.create_session(
                subject="operator-after-unpair",
                role="OPERATOR",
                origin=paired,
            )

    def test_session_creation_cannot_cross_unpair_repair_generation(self):
        entered = threading.Event()
        release = threading.Event()

        def authorize(subject, role, origin):
            if subject == "blocked-repair":
                entered.set()
                if not release.wait(timeout=2):
                    raise RuntimeError("test authorization barrier timed out")
            return True

        boundary = SecurityBoundary(
            allowed_origins={self.owner.origin},
            credential_vault=self.vault,
            session_authorizer=authorize,
            now=lambda: self.clock[0],
        )
        owner = boundary.create_session(
            subject="owner",
            role="OWNER",
            origin=self.owner.origin,
        )
        paired = boundary.pair_origin(
            owner.token,
            origin=owner.origin,
            new_origin="https://paired-generation.autotrade.invalid",
        )

        with ThreadPoolExecutor(max_workers=2) as executor:
            create = executor.submit(
                boundary.create_session,
                subject="blocked-repair",
                role="OPERATOR",
                origin=paired,
            )
            self.assertTrue(entered.wait(timeout=1))
            boundary.unpair_origin(
                owner.token,
                origin=owner.origin,
                paired_origin=paired,
            )
            self.assertEqual(
                boundary.pair_origin(
                    owner.token,
                    origin=owner.origin,
                    new_origin=paired,
                ),
                paired,
            )
            release.set()
            with self.assertRaisesRegex(PermissionError, "pairing changed"):
                create.result(timeout=2)

        replacement = boundary.create_session(
            subject="operator-after-repair",
            role="OPERATOR",
            origin=paired,
        )
        self.assertEqual(replacement.origin, paired)

    def test_refresh_cannot_cross_concurrent_origin_unpair(self):
        entered = threading.Event()
        release = threading.Event()
        blocked_subject = [""]

        def authorize(subject, role, origin):
            if subject == blocked_subject[0]:
                entered.set()
                if not release.wait(timeout=2):
                    raise RuntimeError("test authorization barrier timed out")
            return True

        boundary = SecurityBoundary(
            allowed_origins={self.owner.origin},
            credential_vault=self.vault,
            session_authorizer=authorize,
            now=lambda: self.clock[0],
        )
        owner = boundary.create_session(
            subject="owner",
            role="OWNER",
            origin=self.owner.origin,
        )
        paired = boundary.pair_origin(
            owner.token,
            origin=owner.origin,
            new_origin="https://paired-refresh.autotrade.invalid",
        )
        operator = boundary.create_session(
            subject="operator-unpair-refresh",
            role="OPERATOR",
            origin=paired,
        )
        blocked_subject[0] = operator.subject

        with ThreadPoolExecutor(max_workers=2) as executor:
            refresh = executor.submit(
                boundary.refresh_session,
                operator.token,
                origin=operator.origin,
            )
            self.assertTrue(entered.wait(timeout=1))
            boundary.unpair_origin(
                owner.token,
                origin=owner.origin,
                paired_origin=paired,
            )
            release.set()
            with self.assertRaisesRegex(PermissionError, "Unknown session"):
                refresh.result(timeout=2)

        with self.assertRaisesRegex(PermissionError, "Unknown session"):
            boundary.validate_session(operator.token, origin=operator.origin)

    def test_origin_binding_blocks_browser_replay(self):
        handle = self._credential()
        with self.assertRaisesRegex(PermissionError, "origin"):
            self.boundary.resolve_for_execution(
                self.owner.token,
                origin="https://evil.invalid",
                handle=handle,
                execution_identity="windows-user-1",
                account_id="paper-1",
                provider="SIMULATED",
                environment="PAPER",
                purpose="TRADE",
            )

    def test_rotation_invalidates_old_generation(self):
        old_handle = self._credential()
        new_handle = self.boundary.rotate_secret(
            self.owner.token,
            origin=self.owner.origin,
            handle_id=old_handle.handle_id,
            owner_identity="windows-user-1",
            new_secret_value="rotated-secret",
        )
        self.assertEqual(new_handle.generation, old_handle.generation + 1)
        with self.assertRaisesRegex(PermissionError, "stale"):
            self.boundary.resolve_for_execution(
                self.owner.token,
                origin=self.owner.origin,
                handle=old_handle,
                execution_identity="windows-user-1",
                account_id="paper-1",
                provider="SIMULATED",
                environment="PAPER",
                purpose="TRADE",
            )
        resolved = self.boundary.resolve_for_execution(
            self.owner.token,
            origin=self.owner.origin,
            handle=new_handle,
            execution_identity="windows-user-1",
            account_id="paper-1",
            provider="SIMULATED",
            environment="PAPER",
            purpose="TRADE",
        )
        self.assertEqual(resolved, "rotated-secret")

    def test_withdrawal_credentials_are_not_supported(self):
        with self.assertRaises(PermissionError):
            self.boundary.register_secret(
                self.owner.token,
                origin=self.owner.origin,
                owner_identity="windows-user-1",
                account_id="paper-1",
                provider="SIMULATED",
                environment="PAPER",
                purpose="WITHDRAWAL",
                secret_value="should-never-exist",
            )

    def test_diagnostic_redaction_is_recursive(self):
        value = {
            "account": "paper-1",
            "api_key": "abc",
            "nested": {
                "Authorization": "Bearer abc",
                "safe": "visible",
            },
            "rows": [{"refresh_token": "token", "count": 2}],
        }
        redacted = self.boundary.redact(value)
        self.assertEqual(redacted["account"], "paper-1")
        self.assertEqual(redacted["api_key"], "[REDACTED]")
        self.assertEqual(redacted["nested"]["Authorization"], "[REDACTED]")
        self.assertEqual(redacted["nested"]["safe"], "visible")
        self.assertEqual(redacted["rows"][0]["refresh_token"], "[REDACTED]")

    def test_handle_description_never_contains_secret_value(self):
        handle = self._credential()
        description = self.boundary.describe_handle(handle.handle_id)
        self.assertNotIn("secret", " ".join(description.keys()).lower())
        self.assertNotIn("top-secret", repr(description))


    def test_ttl_must_be_strict_integer_and_clock_must_be_trustworthy(self):
        for ttl in (True, 1.5, "60"):
            with self.subTest(ttl=ttl), self.assertRaises(ValueError):
                self.boundary.create_session(
                    subject="observer",
                    role="OBSERVER",
                    origin=self.owner.origin,
                    ttl_seconds=ttl,
                )
        for idle in (True, 1.5, "60", 0, 3601):
            with self.subTest(idle=idle), self.assertRaises(ValueError):
                self.boundary.create_session(
                    subject="observer",
                    role="OBSERVER",
                    origin=self.owner.origin,
                    ttl_seconds=60,
                    idle_timeout_seconds=idle,
                )
        bad = SecurityBoundary(
            allowed_origins={"https://local.autotrade.invalid"},
            credential_vault=self.vault,
            session_authorizer=self.session_authorizer,
            now=lambda: float("nan"),
        )
        with self.assertRaisesRegex(RuntimeError, "clock"):
            bad.create_session(
                subject="owner",
                role="OWNER",
                origin="https://local.autotrade.invalid",
            )

    def test_host_validator_binds_bearer_session_to_exact_actor(self):
        store = HostCommandStore(
            account_id="paper-account-1",
            environment="PAPER",
            session_validator=self.boundary.validate_host_session,
            request_origin_provider=lambda: self.owner.origin,
        )
        command = {
            "command_id": "11111111-1111-1111-1111-111111111111",
            "expected_state_version": "0",
            "idempotency_key": "security-integration",
            "actor": "owner",
            "session": self.owner.token,
            "account_id": "paper-account-1",
            "environment": "PAPER",
            "action": "BLOCK_NEW_EXPOSURE",
            "payload": {},
        }
        self.assertEqual(store.submit(command).status, "ACCEPTED")

        forged = dict(command)
        forged["command_id"] = "22222222-2222-2222-2222-222222222222"
        forged["idempotency_key"] = "security-forged"
        forged["expected_state_version"] = "1"
        forged["actor"] = "someone-else"
        with self.assertRaises(PermissionError):
            store.submit(forged)
        self.assertEqual(store.state_version, 1)

    def test_unknown_host_action_is_not_authorized_even_for_owner(self):
        self.assertFalse(
            self.boundary.validate_host_session(
                self.owner.token,
                self.owner.subject,
                self.owner.origin,
                "FUTURE_PRIVILEGED_ACTION",
            )
        )

    def test_host_validator_allows_operator_but_rejects_read_only_roles(self):
        store = HostCommandStore(
            account_id="paper-account-1",
            environment="PAPER",
            session_validator=self.boundary.validate_host_session,
            request_origin_provider=lambda: self.owner.origin,
        )
        operator = self.boundary.create_session(
            subject="operator",
            role="OPERATOR",
            origin=self.owner.origin,
        )
        accepted = {
            "command_id": "33333333-3333-3333-3333-333333333333",
            "expected_state_version": "0",
            "idempotency_key": "security-operator",
            "actor": "operator",
            "session": operator.token,
            "account_id": "paper-account-1",
            "environment": "PAPER",
            "action": "BLOCK_NEW_EXPOSURE",
            "payload": {},
        }
        self.assertEqual(store.submit(accepted).status, "ACCEPTED")

        for index, role in enumerate(("RESEARCHER", "OBSERVER"), start=4):
            session = self.boundary.create_session(
                subject=role.lower(),
                role=role,
                origin=self.owner.origin,
            )
            command = {
                "command_id": (
                    f"{index}{index}{index}{index}{index}{index}{index}{index}"
                    "-4444-4444-4444-444444444444"
                ),
                "expected_state_version": "1",
                "idempotency_key": f"security-{role.lower()}",
                "actor": role.lower(),
                "session": session.token,
                "account_id": "paper-account-1",
                "environment": "PAPER",
                "action": "BLOCK_NEW_EXPOSURE",
                "payload": {},
            }
            with self.subTest(role=role), self.assertRaises(PermissionError):
                store.submit(command)
            self.assertEqual(store.state_version, 1)

    def test_operator_cannot_grant_or_revoke_authority(self):
        operator = self.boundary.create_session(
            subject="operator",
            role="OPERATOR",
            origin=self.owner.origin,
        )
        current_session = [operator]
        store = HostCommandStore(
            account_id="paper-account-1",
            environment="PAPER",
            session_validator=self.boundary.validate_host_session,
            request_origin_provider=lambda: self.owner.origin,
        )

        for index, action in enumerate(("SET_AUTHORITY", "REVOKE_AUTHORITY"), start=1):
            command = {
                "command_id": f"bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbb{index}",
                "expected_state_version": "0",
                "idempotency_key": f"owner-only-{action.lower()}",
                "actor": current_session[0].subject,
                "session": current_session[0].token,
                "account_id": "paper-account-1",
                "environment": "PAPER",
                "action": action,
                "payload": {},
            }
            with self.subTest(action=action), self.assertRaises(PermissionError):
                store.submit(command)
            self.assertEqual(store.state_version, 0)

        current_session[0] = self.owner
        owner_command = {
            "command_id": "cccccccc-cccc-cccc-cccc-cccccccccccc",
            "expected_state_version": "0",
            "idempotency_key": "owner-set-authority",
            "actor": self.owner.subject,
            "session": self.owner.token,
            "account_id": "paper-account-1",
            "environment": "PAPER",
            "action": "SET_AUTHORITY",
            "payload": {},
        }
        self.assertEqual(store.submit(owner_command).status, "ACCEPTED")

    def test_unknown_host_action_fails_closed_before_mutation(self):
        for role in ("OWNER", "OPERATOR"):
            with self.subTest(role=role):
                session = (
                    self.owner
                    if role == "OWNER"
                    else self.boundary.create_session(
                        subject="operator-unknown",
                        role="OPERATOR",
                        origin=self.owner.origin,
                    )
                )
                store = HostCommandStore(
                    account_id="paper-account-1",
                    environment="PAPER",
                    session_validator=self.boundary.validate_host_session,
                    request_origin_provider=lambda: self.owner.origin,
                )
                command = {
                    "command_id": (
                        "dddddddd-dddd-dddd-dddd-dddddddddddd"
                        if role == "OWNER"
                        else "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"
                    ),
                    "expected_state_version": "0",
                    "idempotency_key": f"unknown-action-{role.lower()}",
                    "actor": session.subject,
                    "session": session.token,
                    "account_id": "paper-account-1",
                    "environment": "PAPER",
                    "action": "FUTURE_PRIVILEGED_ACTION",
                    "payload": {},
                }
                with self.assertRaisesRegex(
                    ValueError,
                    "unsupported or non-canonical host action",
                ):
                    store.submit(command)
                self.assertEqual(store.state_version, 0)
                self.assertEqual(store.events_after(0), ())

    def test_host_mutation_is_bound_to_current_request_origin(self):
        current_origin = ["https://evil.invalid"]
        store = HostCommandStore(
            account_id="paper-account-1",
            environment="PAPER",
            session_validator=self.boundary.validate_host_session,
            request_origin_provider=lambda: current_origin[0],
        )
        command = {
            "command_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            "expected_state_version": "0",
            "idempotency_key": "security-origin-binding",
            "actor": "owner",
            "session": self.owner.token,
            "account_id": "paper-account-1",
            "environment": "PAPER",
            "action": "BLOCK_NEW_EXPOSURE",
            "payload": {},
        }
        with self.assertRaises(PermissionError):
            store.submit(command)
        self.assertEqual(store.state_version, 0)

        current_origin[0] = self.owner.origin
        self.assertEqual(store.submit(command).status, "ACCEPTED")
        self.assertEqual(store.state_version, 1)

    def test_revoked_session_cannot_be_reused(self):
        token = self.owner.token
        self.boundary.revoke_session(token)
        with self.assertRaisesRegex(PermissionError, "Unknown session"):
            self.boundary.validate_session(token, origin=self.owner.origin)

    def test_origin_validation_requires_https_or_loopback_http(self):
        with self.assertRaises(ValueError):
            SecurityBoundary(
                allowed_origins={"http://remote.example"},
                credential_vault=self.vault,
                session_authorizer=self.session_authorizer,
            )
        with self.assertRaises(ValueError):
            SecurityBoundary(
                allowed_origins={"https://local.autotrade.invalid/path"},
                credential_vault=self.vault,
                session_authorizer=self.session_authorizer,
            )
        with self.assertRaises(ValueError):
            self.boundary.pair_origin(
                self.owner.token,
                origin=self.owner.origin,
                new_origin="https://user:pass@paired.autotrade.invalid",
            )

        loopback = SecurityBoundary(
            allowed_origins={"http://127.0.0.1:8765/"},
            credential_vault=self.vault,
            session_authorizer=self.session_authorizer,
        )
        session = loopback.create_session(
            subject="owner",
            role="OWNER",
            origin="http://127.0.0.1:8765",
        )
        self.assertEqual(session.origin, "http://127.0.0.1:8765")

    def test_origin_is_canonicalized_and_invalid_port_fails_closed(self):
        canonical = SecurityBoundary(
            allowed_origins={"https://LOCAL.AUTOTRADE.INVALID:443/"},
            credential_vault=self.vault,
            session_authorizer=self.session_authorizer,
        )
        session = canonical.create_session(
            subject="owner",
            role="OWNER",
            origin="https://local.autotrade.invalid",
        )
        self.assertEqual(session.origin, "https://local.autotrade.invalid")
        with self.assertRaisesRegex(ValueError, "port"):
            SecurityBoundary(
                allowed_origins={"https://local.autotrade.invalid:99999"},
                credential_vault=self.vault,
                session_authorizer=self.session_authorizer,
            )

    def test_diagnostic_redaction_masks_sensitive_labeled_strings(self):
        redacted = self.boundary.redact(
            {
                "message": "Authorization: Bearer should-never-log",
                "rows": [
                    "safe status",
                    "api_key=should-never-log",
                    {"detail": "refresh_token: should-never-log"},
                ],
            }
        )
        self.assertEqual(redacted["message"], "[REDACTED]")
        self.assertEqual(redacted["rows"][0], "safe status")
        self.assertEqual(redacted["rows"][1], "[REDACTED]")
        self.assertEqual(redacted["rows"][2]["detail"], "[REDACTED]")
        self.assertNotIn("should-never-log", repr(redacted))

    def test_diagnostic_redaction_covers_mapping_proxy_and_sets(self):
        self._credential()
        redacted = self.boundary.redact_for_diagnostics(
            MappingProxyType(
                {
                    "api_key": "must-hide-by-key",
                    "safe": {"top-secret"},
                    "nested": MappingProxyType({"note": "prefix top-secret suffix"}),
                }
            ),
            sensitive_values=("top-secret",),
        )
        rendered = repr(redacted)
        self.assertNotIn("top-secret", rendered)
        self.assertNotIn("must-hide-by-key", rendered)
        self.assertIn("[REDACTED]", rendered)

    def test_diagnostic_redaction_scrubs_embedded_secret_mapping_keys(self):
        redacted = self.boundary.redact(
            {
                "Authorization: Bearer bearer-key-secret": "bearer-value",
                "https://user:url-password@provider.invalid/path": "url-value",
                "api_key=first-key-secret": "first-value",
                "api_key=second-key-secret": "second-value",
                "safe": "visible",
            }
        )

        rendered = repr(redacted)
        for secret in (
            "bearer-key-secret",
            "url-password",
            "first-key-secret",
            "second-key-secret",
        ):
            self.assertNotIn(secret, rendered)
        self.assertIn("Authorization: [REDACTED]", redacted)
        self.assertIn("https://[REDACTED]@provider.invalid/path", redacted)
        self.assertIn("api_key=[REDACTED]", redacted)
        self.assertIn("api_key=[REDACTED] [2]", redacted)
        self.assertEqual(redacted["safe"], "visible")

    def test_diagnostic_redaction_fails_closed_if_key_sanitizer_errors(self):
        with patch(
            "mvp.autotrade_mvp.security._redact_embedded_secret_text",
            side_effect=RecursionError("pathological diagnostic key"),
        ):
            redacted = self.boundary.redact(
                {"safe-looking-key": "must-not-be-retained-under-failed-key"}
            )

        self.assertEqual(
            redacted,
            {"[REDACTED:KEY]": "[REDACTED]"},
        )

    def test_diagnostic_redaction_preserves_synthetic_key_collisions(self):
        class HostileKey:
            def __hash__(self):
                return 11

        redacted = self.boundary.redact(
            {
                "[UNSUPPORTED_KEY_1]": "safe-visible",
                HostileKey(): "must-never-surface",
            }
        )
        self.assertEqual(redacted["[UNSUPPORTED_KEY_1]"], "safe-visible")
        self.assertEqual(
            redacted["[UNSUPPORTED_KEY_1] [2]"],
            "[REDACTED]",
        )

    def test_diagnostic_redaction_never_executes_hostile_key_or_value_hooks(self):
        class HostileKey:
            def __hash__(self):
                return 7

            def __str__(self):
                raise AssertionError("diagnostic redaction must not call hostile __str__")

            def __repr__(self):
                raise AssertionError("diagnostic redaction must not retain hostile keys")

        class HostileValue:
            def __str__(self):
                raise AssertionError("diagnostic redaction must not call hostile __str__")

            def __repr__(self):
                raise AssertionError("diagnostic redaction must not retain hostile values")

        redacted = self.boundary.redact_for_diagnostics(
            {
                HostileKey(): "must-never-surface",
                "safe": HostileValue(),
            },
            sensitive_values=("must-never-surface",),
        )
        rendered = repr(redacted)
        self.assertNotIn("must-never-surface", rendered)
        self.assertIn("[REDACTED]", rendered)
        self.assertEqual(redacted["safe"], "[REDACTED:UNSUPPORTED]")

    def test_diagnostic_redaction_breaks_container_cycles_fail_closed(self):
        cyclic_dict = {}
        cyclic_dict["self"] = cyclic_dict
        cyclic_list = []
        cyclic_list.append(cyclic_list)

        redacted_dict = self.boundary.redact(cyclic_dict)
        redacted_list = self.boundary.redact(cyclic_list)

        self.assertEqual(redacted_dict["self"], "[REDACTED:CYCLE]")
        self.assertEqual(redacted_list[0], "[REDACTED:CYCLE]")

    def test_diagnostic_redaction_bounds_deep_acyclic_payloads(self):
        value: object = "safe-leaf"
        for _ in range(100):
            value = [value]

        redacted = self.boundary.redact(value)
        cursor = redacted
        for _ in range(65):
            self.assertIsInstance(cursor, list)
            cursor = cursor[0]
        self.assertEqual(cursor, "[REDACTED:DEPTH]")

    def test_diagnostic_redaction_scrubs_sensitive_values_from_mapping_keys(self):
        redacted = self.boundary.redact_for_diagnostics(
            {"prefix-top-secret-suffix": "visible"},
            sensitive_values=("top-secret",),
        )
        rendered = repr(redacted)
        self.assertNotIn("top-secret", rendered)
        self.assertEqual(
            redacted["prefix-[REDACTED]-suffix"],
            "visible",
        )

    def test_diagnostic_redaction_never_emits_binary_payload_bytes(self):
        redacted = self.boundary.redact(
            {"wire": b"Authorization: Bearer should-never-log"}
        )
        self.assertEqual(redacted["wire"], "[REDACTED:BINARY]")

    def test_unpair_then_repair_never_revives_old_token(self):
        paired = self.boundary.pair_origin(
            self.owner.token,
            origin=self.owner.origin,
            new_origin="https://paired.autotrade.invalid",
        )
        old = self.boundary.create_session(
            subject="operator",
            role="OPERATOR",
            origin=paired,
        )
        self.boundary.unpair_origin(
            self.owner.token,
            origin=self.owner.origin,
            paired_origin=paired,
        )
        self.boundary.pair_origin(
            self.owner.token,
            origin=self.owner.origin,
            new_origin=paired,
        )
        with self.assertRaises(PermissionError):
            self.boundary.validate_session(old.token, origin=paired)

    def test_session_revoke_cannot_cut_through_secret_registration(self):
        entered = threading.Event()
        release = threading.Event()
        original_register = self.vault.register

        def blocking_register(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise AssertionError("timed out waiting to release credential registration")
            return original_register(*args, **kwargs)

        self.vault.register = blocking_register
        with ThreadPoolExecutor(max_workers=2) as pool:
            registering = pool.submit(
                self.boundary.register_secret,
                self.owner.token,
                origin=self.owner.origin,
                owner_identity="windows-user-1",
                account_id="paper-register-race",
                provider="SIMULATED",
                environment="PAPER",
                purpose="READ",
                secret_value="register-race-secret",
            )
            self.assertTrue(entered.wait(5))
            revoking = pool.submit(
                self.boundary.revoke_session,
                self.owner.token,
            )
            self.assertFalse(
                revoking.done(),
                "session revocation must not cut through credential registration",
            )
            release.set()
            registered = registering.result(timeout=5)
            self.assertEqual(registered.account_id, "paper-register-race")
            revoking.result(timeout=5)

        with self.assertRaisesRegex(PermissionError, "Unknown session"):
            self.boundary.validate_session(
                self.owner.token,
                origin=self.owner.origin,
            )

    def test_session_revoke_cannot_cut_through_secret_rotation(self):
        handle = self._credential()
        entered = threading.Event()
        release = threading.Event()
        original_rotate = self.vault.rotate

        def blocking_rotate(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise AssertionError("timed out waiting to release credential rotation")
            return original_rotate(*args, **kwargs)

        self.vault.rotate = blocking_rotate
        with ThreadPoolExecutor(max_workers=2) as pool:
            rotating = pool.submit(
                self.boundary.rotate_secret,
                self.owner.token,
                origin=self.owner.origin,
                handle_id=handle.handle_id,
                owner_identity="windows-user-1",
                new_secret_value="rotated-race-secret",
            )
            self.assertTrue(entered.wait(5))
            revoking = pool.submit(
                self.boundary.revoke_session,
                self.owner.token,
            )
            self.assertFalse(
                revoking.done(),
                "session revocation must not cut through credential rotation",
            )
            release.set()
            rotated = rotating.result(timeout=5)
            self.assertEqual(rotated.generation, handle.generation + 1)
            revoking.result(timeout=5)

        with self.assertRaisesRegex(PermissionError, "Unknown session"):
            self.boundary.validate_session(
                self.owner.token,
                origin=self.owner.origin,
            )

    def test_session_revoke_cannot_cut_through_secret_revocation(self):
        handle = self._credential()
        entered = threading.Event()
        release = threading.Event()
        original_revoke = self.vault.revoke

        def blocking_revoke(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise AssertionError("timed out waiting to release credential revocation")
            return original_revoke(*args, **kwargs)

        self.vault.revoke = blocking_revoke
        with ThreadPoolExecutor(max_workers=2) as pool:
            revoking_secret = pool.submit(
                self.boundary.revoke_secret,
                self.owner.token,
                origin=self.owner.origin,
                handle_id=handle.handle_id,
                owner_identity="windows-user-1",
            )
            self.assertTrue(entered.wait(5))
            revoking_session = pool.submit(
                self.boundary.revoke_session,
                self.owner.token,
            )
            self.assertFalse(
                revoking_session.done(),
                "session revocation must not cut through credential revocation",
            )
            release.set()
            revoking_secret.result(timeout=5)
            revoking_session.result(timeout=5)

        with self.assertRaisesRegex(PermissionError, "Unknown session"):
            self.boundary.validate_session(
                self.owner.token,
                origin=self.owner.origin,
            )

    def test_session_revoke_cannot_linearize_mid_secret_resolution(self):
        handle = self._credential()
        entered = threading.Event()
        release = threading.Event()
        original_resolve = self.vault.resolve

        def blocking_resolve(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise AssertionError("timed out waiting to release credential resolution")
            return original_resolve(*args, **kwargs)

        self.vault.resolve = blocking_resolve
        with ThreadPoolExecutor(max_workers=2) as pool:
            resolving = pool.submit(
                self.boundary.resolve_for_execution,
                self.owner.token,
                origin=self.owner.origin,
                handle=handle,
                execution_identity="windows-user-1",
                account_id="paper-1",
                provider="SIMULATED",
                environment="PAPER",
                purpose="TRADE",
            )
            self.assertTrue(entered.wait(5))
            revoking = pool.submit(
                self.boundary.revoke_session,
                self.owner.token,
            )
            self.assertFalse(
                revoking.done(),
                "session revocation must not cut through an already-authorized vault call",
            )
            release.set()
            self.assertEqual(resolving.result(timeout=5), "top-secret")
            revoking.result(timeout=5)

        with self.assertRaisesRegex(PermissionError, "Unknown session"):
            self.boundary.validate_session(
                self.owner.token,
                origin=self.owner.origin,
            )

    def test_session_revoke_waits_only_for_credential_lease_acquisition(self):
        handle = self._credential()
        entering = threading.Event()
        allow_enter = threading.Event()
        in_use = threading.Event()
        allow_finish = threading.Event()
        original_lease = self.vault.lease

        @contextmanager
        def blocking_lease(*args, **kwargs):
            entering.set()
            if not allow_enter.wait(5):
                raise AssertionError("timed out waiting to enter credential lease")
            with original_lease(*args, **kwargs) as plaintext:
                yield plaintext

        self.vault.lease = blocking_lease

        def use_credential():
            with self.boundary.lease_for_execution(
                self.owner.token,
                origin=self.owner.origin,
                handle=handle,
                execution_identity="windows-user-1",
                account_id="paper-1",
                provider="SIMULATED",
                environment="PAPER",
                purpose="TRADE",
            ) as plaintext:
                in_use.set()
                if not allow_finish.wait(5):
                    raise AssertionError("timed out waiting to finish credential use")
                return plaintext

        with ThreadPoolExecutor(max_workers=2) as pool:
            using = pool.submit(use_credential)
            self.assertTrue(entering.wait(5))
            revoking = pool.submit(
                self.boundary.revoke_session,
                self.owner.token,
            )
            self.assertFalse(
                revoking.done(),
                "revocation must wait until the credential lease is acquired",
            )

            allow_enter.set()
            self.assertTrue(in_use.wait(5))
            revoking.result(timeout=5)
            self.assertFalse(
                using.done(),
                "revocation must not wait for already-linearized terminal credential use",
            )
            allow_finish.set()
            self.assertEqual(using.result(timeout=5), "top-secret")

        with self.assertRaisesRegex(PermissionError, "Unknown session"):
            self.boundary.validate_session(
                self.owner.token,
                origin=self.owner.origin,
            )

    def test_owner_can_pair_new_origin_and_non_owner_cannot(self):
        new_origin = "https://paired.autotrade.invalid"
        self.boundary.pair_origin(
            self.owner.token,
            origin=self.owner.origin,
            new_origin=new_origin,
        )
        observer = self.boundary.create_session(
            subject="observer",
            role="OBSERVER",
            origin=new_origin,
        )
        self.assertEqual(observer.origin, new_origin)
        with self.assertRaises(PermissionError):
            self.boundary.pair_origin(
                observer.token,
                origin=new_origin,
                new_origin="https://another.invalid",
            )

    def test_unpair_origin_revokes_all_sessions_from_that_origin(self):
        paired = self.boundary.pair_origin(
            self.owner.token,
            origin=self.owner.origin,
            new_origin="https://paired.autotrade.invalid",
        )
        operator = self.boundary.create_session(
            subject="operator",
            role="OPERATOR",
            origin=paired,
        )
        self.boundary.unpair_origin(
            self.owner.token,
            origin=self.owner.origin,
            paired_origin=paired,
        )
        with self.assertRaises(PermissionError):
            self.boundary.validate_session(operator.token, origin=paired)
        with self.assertRaises(PermissionError):
            self.boundary.create_session(
                subject="operator-2",
                role="OPERATOR",
                origin=paired,
            )

    def test_final_authenticated_origin_cannot_be_removed(self):
        with self.assertRaisesRegex(PermissionError, "final authenticated origin"):
            self.boundary.unpair_origin(
                self.owner.token,
                origin=self.owner.origin,
                paired_origin=self.owner.origin,
            )

    def test_unknown_credential_purpose_fails_closed(self):
        with self.assertRaisesRegex(PermissionError, "purpose"):
            self.boundary.register_secret(
                self.owner.token,
                origin=self.owner.origin,
                owner_identity="windows-user-1",
                account_id="paper-1",
                provider="SIMULATED",
                environment="PAPER",
                purpose="ARBITRARY_TOOL",
                secret_value="must-not-exist",
            )

    def test_required_roles_fail_closed_on_unknown_role(self):
        with self.assertRaisesRegex(PermissionError, "Unknown required role"):
            self.boundary.validate_session(
                self.owner.token,
                origin=self.owner.origin,
                required_roles={"OWNER", "SUPERUSER"},
            )

    def test_diagnostic_redaction_removes_known_secret_values_from_safe_keys(self):
        self._credential()
        redacted = self.boundary.redact_for_diagnostics(
            {
                "message": "provider returned top-secret in an unexpected field",
                "nested": ["top-secret", {"safe": "prefix top-secret suffix"}],
            },
            sensitive_values=("top-secret",),
        )
        self.assertNotIn("top-secret", repr(redacted))
        self.assertIn("[REDACTED]", repr(redacted))

    def test_rotated_and_revoked_secret_values_remain_redacted(self):
        old_handle = self._credential()
        new_handle = self.boundary.rotate_secret(
            self.owner.token,
            origin=self.owner.origin,
            handle_id=old_handle.handle_id,
            owner_identity="windows-user-1",
            new_secret_value="new-secret",
        )
        self.boundary.revoke_secret(
            self.owner.token,
            origin=self.owner.origin,
            handle_id=new_handle.handle_id,
            owner_identity="windows-user-1",
        )
        redacted = self.boundary.redact_for_diagnostics(
            {"message": "old=top-secret new=new-secret"},
            sensitive_values=("top-secret", "new-secret"),
        )
        rendered = repr(redacted)
        self.assertNotIn("top-secret", rendered)
        self.assertNotIn("new-secret", rendered)
        self.assertIn("[REDACTED]", rendered)

    def test_boundary_uses_vault_as_only_credential_authority(self):
        handle = self._credential()
        self.assertFalse(hasattr(self.boundary, "_records"))
        self.assertFalse(hasattr(self.boundary, "_secret_redactions"))
        raw = self.vault_path.read_text(encoding="utf-8")
        self.assertNotIn("top-secret", raw)
        self.assertEqual(
            self.boundary.resolve_for_execution(
                self.owner.token,
                origin=self.owner.origin,
                handle=handle,
                execution_identity="windows-user-1",
                account_id="paper-1",
                provider="SIMULATED",
                environment="PAPER",
                purpose="TRADE",
            ),
            "top-secret",
        )


    def test_execution_lease_uses_session_and_vault_authority(self):
        handle = self._credential()
        with self.boundary.lease_for_execution(
            self.owner.token,
            origin=self.owner.origin,
            handle=handle,
            execution_identity="windows-user-1",
            account_id="paper-1",
            provider="SIMULATED",
            environment="PAPER",
            purpose="TRADE",
        ) as plaintext:
            self.assertEqual(plaintext, "top-secret")

        researcher = self.boundary.create_session(
            subject="research",
            role="RESEARCHER",
            origin=self.owner.origin,
        )
        with self.assertRaises(PermissionError):
            with self.boundary.lease_for_execution(
                researcher.token,
                origin=researcher.origin,
                handle=handle,
                execution_identity="windows-user-1",
                account_id="paper-1",
                provider="SIMULATED",
                environment="PAPER",
                purpose="TRADE",
            ):
                self.fail("researcher must not receive execution credential lease")

    def test_stale_origin_session_is_invalid_even_without_origin_argument(self):
        paired = self.boundary.pair_origin(
            self.owner.token,
            origin=self.owner.origin,
            new_origin="https://paired.autotrade.invalid",
        )
        operator = self.boundary.create_session(
            subject="operator",
            role="OPERATOR",
            origin=paired,
        )
        self.boundary.unpair_origin(
            self.owner.token,
            origin=self.owner.origin,
            paired_origin=paired,
        )
        with self.assertRaises(PermissionError):
            self.boundary.validate_session(operator.token)


if __name__ == "__main__":
    unittest.main()
