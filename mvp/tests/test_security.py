import unittest

from mvp.autotrade_mvp.security import SecurityBoundary


class SecurityBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.clock = [1000.0]
        self.boundary = SecurityBoundary(
            allowed_origins={"https://local.autotrade.invalid"},
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


    def test_only_read_and_trade_secret_purposes_can_be_registered(self):
        for purpose in (
            "WITHDRAWAL",
            "WITHDRAW",
            "TRANSFER",
            "EXTERNAL_TRANSFER",
            "PAYOUT",
            "ADMIN",
            "UNKNOWN",
        ):
            with self.subTest(purpose=purpose), self.assertRaisesRegex(
                PermissionError, "Only READ and TRADE"
            ):
                self.boundary.register_secret(
                    self.owner.token,
                    origin=self.owner.origin,
                    owner_identity="windows-user-1",
                    account_id="paper-1",
                    provider="SIMULATED",
                    purpose=purpose,
                    secret_value="must-not-exist",
                )

        read_handle = self.boundary.register_secret(
            self.owner.token,
            origin=self.owner.origin,
            owner_identity="windows-user-1",
            account_id="paper-1",
            provider="SIMULATED",
            purpose="read",
            secret_value="read-secret",
        )
        self.assertEqual(read_handle.purpose, "READ")


    def test_redaction_masks_sensitive_strings_under_neutral_keys(self):
        payload = {
            "message": "Authorization: Bearer should-never-log",
            "nested": [
                "safe status",
                "api_key=should-never-log",
                {"detail": "refresh_token: should-never-log"},
            ],
        }
        redacted = self.boundary.redact(payload)
        self.assertEqual(redacted["message"], "[REDACTED]")
        self.assertEqual(redacted["nested"][0], "safe status")
        self.assertEqual(redacted["nested"][1], "[REDACTED]")
        self.assertEqual(redacted["nested"][2]["detail"], "[REDACTED]")
        self.assertNotIn("should-never-log", repr(redacted))


if __name__ == "__main__":
    unittest.main()
