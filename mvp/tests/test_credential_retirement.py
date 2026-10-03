from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import unittest

from mvp.autotrade_mvp.credential_retirement import (
    CredentialRetirementError,
    require_retired_trade_credential_generation,
)
from mvp.autotrade_mvp.windows_secrets import (
    PersistentCredentialHandle,
    ProtectedCredentialVault,
)


class DeterministicProtector:
    PREFIX = b"test-protected-v1:"

    def protect(self, plaintext: bytes, *, entropy: bytes) -> bytes:
        return self.PREFIX + sha256(entropy).digest() + plaintext[::-1]

    def unprotect(self, ciphertext: bytes, *, entropy: bytes) -> bytes:
        expected = self.PREFIX + sha256(entropy).digest()
        if not ciphertext.startswith(expected):
            raise OSError("scope entropy mismatch")
        return ciphertext[len(expected):][::-1]


class CredentialRetirementTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "credentials.json"
        self.vault = ProtectedCredentialVault(
            self.path,
            protector=DeterministicProtector(),
        )

    def _register(self, *, purpose: str = "TRADE") -> PersistentCredentialHandle:
        return self.vault.register(
            handle_id="cred-wp49",
            owner_identity="windows-user-1",
            account_id="paper-1",
            provider="SIMULATED",
            environment="PAPER",
            purpose=purpose,
            secret_value="secret-v1",
        )

    def test_active_exact_trade_generation_is_not_retired(self) -> None:
        handle = self._register()

        with self.assertRaisesRegex(
            CredentialRetirementError,
            "still active",
        ):
            require_retired_trade_credential_generation(self.vault, handle)

    def test_revoked_exact_trade_generation_is_observed_live(self) -> None:
        handle = self._register()
        self.vault.revoke(handle, execution_identity="windows-user-1")

        observed = require_retired_trade_credential_generation(self.vault, handle)

        self.assertEqual(observed.retired_handle, handle)
        self.assertEqual(observed.disposition, "REVOKED")
        self.assertEqual(observed.observed_generation, handle.generation)
        self.assertFalse(observed.observed_active)

    def test_rotated_old_trade_generation_is_observed_as_superseded(self) -> None:
        handle = self._register()
        current = self.vault.rotate(
            handle,
            execution_identity="windows-user-1",
            new_secret_value="secret-v2",
        )

        observed = require_retired_trade_credential_generation(self.vault, handle)

        self.assertEqual(observed.disposition, "SUPERSEDED")
        self.assertEqual(observed.observed_generation, current.generation)
        self.assertTrue(observed.observed_active)

    def test_snapshot_cannot_be_replayed_after_vault_rollback(self) -> None:
        handle = self._register()
        active_generation_bytes = self.path.read_bytes()
        self.vault.revoke(handle, execution_identity="windows-user-1")
        first = require_retired_trade_credential_generation(self.vault, handle)
        self.assertEqual(first.disposition, "REVOKED")

        # Model recovery of an older vault file. A captured snapshot must not be
        # accepted as a durable takeover credential after the selected live
        # vault no longer proves retirement.
        self.path.write_bytes(active_generation_bytes)

        with self.assertRaisesRegex(
            CredentialRetirementError,
            "still active",
        ):
            require_retired_trade_credential_generation(self.vault, handle)

    def test_future_generation_claim_fails_closed_on_older_live_vault(self) -> None:
        handle = self._register()
        forged_future = PersistentCredentialHandle(
            handle_id=handle.handle_id,
            account_id=handle.account_id,
            provider=handle.provider,
            environment=handle.environment,
            purpose=handle.purpose,
            generation=handle.generation + 1,
        )

        with self.assertRaisesRegex(
            CredentialRetirementError,
            "older than the selected retired handle",
        ):
            require_retired_trade_credential_generation(self.vault, forged_future)

    def test_scope_substitution_cannot_reuse_retirement(self) -> None:
        handle = self._register()
        self.vault.revoke(handle, execution_identity="windows-user-1")
        wrong_scope = PersistentCredentialHandle(
            handle_id=handle.handle_id,
            account_id="paper-2",
            provider=handle.provider,
            environment=handle.environment,
            purpose=handle.purpose,
            generation=handle.generation,
        )

        with self.assertRaisesRegex(
            CredentialRetirementError,
            "scope does not match",
        ):
            require_retired_trade_credential_generation(self.vault, wrong_scope)

    def test_read_credential_retirement_cannot_satisfy_sender_fence(self) -> None:
        handle = self._register(purpose="READ")
        self.vault.revoke(handle, execution_identity="windows-user-1")

        with self.assertRaisesRegex(
            CredentialRetirementError,
            "TRADE credential",
        ):
            require_retired_trade_credential_generation(self.vault, handle)

    def test_retirement_observation_serializes_with_active_execution_lease(self) -> None:
        handle = self._register()
        entered = Event()
        release = Event()

        def hold_lease() -> None:
            with self.vault.lease(
                handle,
                execution_identity="windows-user-1",
                account_id=handle.account_id,
                provider=handle.provider,
                environment=handle.environment,
                purpose=handle.purpose,
            ):
                entered.set()
                self.assertTrue(release.wait(timeout=5))

        with ThreadPoolExecutor(max_workers=2) as executor:
            lease_future = executor.submit(hold_lease)
            self.assertTrue(entered.wait(timeout=2))
            observation_future = executor.submit(
                require_retired_trade_credential_generation,
                self.vault,
                handle,
            )
            with self.assertRaises(FuturesTimeoutError):
                observation_future.result(timeout=0.2)
            release.set()
            self.assertIsNone(lease_future.result(timeout=2))
            with self.assertRaisesRegex(
                CredentialRetirementError,
                "still active",
            ):
                observation_future.result(timeout=2)

    def test_vault_subclass_cannot_override_retirement_reader(self) -> None:
        handle = self._register()
        self.vault.revoke(handle, execution_identity="windows-user-1")
        poisoned_calls = 0

        class PoisonedVault(ProtectedCredentialVault):
            def _load(self):
                nonlocal poisoned_calls
                poisoned_calls += 1
                return {"version": self.FORMAT_VERSION, "records": {}}

        # Construction is intentionally bypassed: the regression is about the
        # retirement function rejecting the subclass before any virtual method
        # can become an authority seam.
        poisoned = object.__new__(PoisonedVault)

        with self.assertRaisesRegex(
            TypeError,
            "exact ProtectedCredentialVault",
        ):
            require_retired_trade_credential_generation(poisoned, handle)
        self.assertEqual(poisoned_calls, 0)


if __name__ == "__main__":
    unittest.main()
