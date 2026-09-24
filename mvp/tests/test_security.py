from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from types import MappingProxyType
import unittest

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
        self.boundary = SecurityBoundary(
            allowed_origins={"https://local.autotrade.invalid"},
            credential_vault=self.vault,
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
                purpose="TRADE",
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
                purpose="TRADE",
            )

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
                purpose="TRADE",
            )
        resolved = self.boundary.resolve_for_execution(
            self.owner.token,
            origin=self.owner.origin,
            handle=new_handle,
            execution_identity="windows-user-1",
            account_id="paper-1",
            provider="SIMULATED",
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
        bad = SecurityBoundary(
            allowed_origins={"https://local.autotrade.invalid"},
            credential_vault=self.vault,
            now=lambda: float("nan"),
        )
        with self.assertRaisesRegex(RuntimeError, "clock"):
            bad.create_session(
                subject="owner",
                role="OWNER",
                origin="https://local.autotrade.invalid",
            )

    def test_host_validator_binds_bearer_session_to_exact_actor(self):
        store = HostCommandStore(session_validator=self.boundary.validate_host_session)
        command = {
            "command_id": "11111111-1111-1111-1111-111111111111",
            "expected_state_version": "0",
            "idempotency_key": "security-integration",
            "actor": "owner",
            "session": self.owner.token,
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
            )
        with self.assertRaises(ValueError):
            SecurityBoundary(
                allowed_origins={"https://local.autotrade.invalid/path"},
                credential_vault=self.vault,
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
            )

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
                purpose="TRADE",
            ),
            "top-secret",
        )

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
