from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock
import unittest

import mvp.autotrade_mvp.production_financial_host as financial_host_module
from mvp.autotrade_mvp.dispatch import GuardedDispatcher
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.production_financial_host import (
    HostLifetimeGuardedDispatcher,
    HostLifetimeProviderSecretResolver,
    ProductionFinancialHostRuntime,
)
from mvp.autotrade_mvp.production_host import ProductionHostConfig, ProductionHostRuntime
from mvp.autotrade_mvp.recovery import RecoveryController
from mvp.autotrade_mvp.security import SecurityBoundary


_FORGED_SENDER_CALLS: list[tuple[object, object]] = []
_FORGED_DISPATCH_CALLS: list[object] = []
_FORGED_LEASE_CALLS: list[object] = []


def _forged_validate_sender(_self, owner_id, owner_epoch):
    _FORGED_SENDER_CALLS.append((owner_id, owner_epoch))


def _forged_guarded_dispatch(_self, **kwargs):
    _FORGED_DISPATCH_CALLS.append(kwargs)
    return "forged"


def _forged_security_lease(_self, *args, **kwargs):
    _FORGED_LEASE_CALLS.append((args, kwargs))
    raise AssertionError("forged credential lease executed")


class _LeaseBoundary:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    @contextmanager
    def lease_for_execution(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        yield "secret"


class ProductionFinancialAuthorityBindingTests(unittest.TestCase):
    def _components(self, directory: str):
        journal = JournalStore(Path(directory) / "authority.sqlite3")
        config = ProductionHostConfig(
            journal_path=journal.path,
            account_id="account-1",
            environment="PAPER",
            host_id="host-a",
            bind_host="127.0.0.1",
            bind_port=19071,
            public_origin="http://127.0.0.1:19071",
        )
        host = ProductionHostRuntime(
            config=config,
            journal=journal,
            application=Mock(),
            server=Mock(),
            instance_fence=Mock(),
            admission_gate=Mock(),
        )
        recovery = RecoveryController(
            owner_store=journal,
            owner_scope="PAPER:account-1",
        )
        owner = recovery.start("host-a")
        core = GuardedDispatcher(
            journal,
            environment="PAPER",
            account_id="account-1",
            owner_token="host-a",
            owner_epoch=1,
        )
        guarded = HostLifetimeGuardedDispatcher(
            core,
            recovery_controller=recovery,
            owner=owner,
        )
        resolver = HostLifetimeProviderSecretResolver(
            _LeaseBoundary(),
            account_id="account-1",
            environment="PAPER",
        )
        runtime = ProductionFinancialHostRuntime(
            host=host,
            recovery_controller=recovery,
            owner=owner,
            provider_secret_resolver=resolver,
            dispatcher=guarded,
        )
        return journal, config, host, recovery, owner, core, guarded, resolver, runtime

    def test_recovery_validator_class_rebinding_fails_closed(self):
        with TemporaryDirectory() as directory:
            *_, guarded, _resolver, _runtime = self._components(directory)
            original = RecoveryController.validate_sender

            def replacement(_self, _owner_id, _owner_epoch):
                raise AssertionError("replacement sender validator executed")

            RecoveryController.validate_sender = replacement
            try:
                with self.assertRaisesRegex(
                    PermissionError,
                    "validator authority changed",
                ):
                    guarded._require_dispatch_authority()
            finally:
                RecoveryController.validate_sender = original

    def test_recovery_validator_same_function_code_mutation_fails_before_execution(self):
        with TemporaryDirectory() as directory:
            *_, guarded, _resolver, _runtime = self._components(directory)
            canonical = financial_host_module._CANONICAL_VALIDATE_SENDER
            original_code = canonical.__code__
            _FORGED_SENDER_CALLS.clear()
            try:
                canonical.__code__ = _forged_validate_sender.__code__
                with self.assertRaisesRegex(
                    PermissionError,
                    "validator code changed",
                ):
                    guarded._require_dispatch_authority()
            finally:
                canonical.__code__ = original_code
            self.assertEqual(_FORGED_SENDER_CALLS, [])

    def test_guarded_dispatch_same_function_code_mutation_fails_before_execution(self):
        with TemporaryDirectory() as directory:
            *_, guarded, _resolver, _runtime = self._components(directory)
            canonical = financial_host_module._CANONICAL_GUARDED_DISPATCH
            original_code = canonical.__code__
            _FORGED_DISPATCH_CALLS.clear()
            try:
                canonical.__code__ = _forged_guarded_dispatch.__code__
                with self.assertRaisesRegex(
                    PermissionError,
                    "dispatcher executable code changed",
                ):
                    guarded._require_dispatch_authority()
            finally:
                canonical.__code__ = original_code
            self.assertEqual(_FORGED_DISPATCH_CALLS, [])

    def test_mutated_owner_boolean_epoch_is_not_equivalent_to_generation_one(self):
        with TemporaryDirectory() as directory:
            *_, owner, _core, guarded, _resolver, _runtime = self._components(directory)
            object.__setattr__(owner, "epoch", True)
            with self.assertRaisesRegex(
                PermissionError,
                "positive exact integer",
            ):
                guarded._require_dispatch_authority()

    def test_dispatcher_scope_retarget_fails_closed(self):
        with TemporaryDirectory() as directory:
            *_, core, guarded, _resolver, _runtime = self._components(directory)
            core.account_id = "other-account"
            with self.assertRaisesRegex(
                PermissionError,
                "account changed after composition",
            ):
                guarded._require_dispatch_authority()

    def test_dispatcher_journal_retarget_fails_closed(self):
        with TemporaryDirectory() as directory:
            *_, core, guarded, _resolver, _runtime = self._components(directory)
            core.store = JournalStore(Path(directory) / "retargeted.sqlite3")
            with self.assertRaisesRegex(
                PermissionError,
                "journal changed after composition",
            ):
                guarded._require_dispatch_authority()

    def test_runtime_config_object_replacement_fails_closed(self):
        with TemporaryDirectory() as directory:
            _journal, config, host, *_rest, runtime = self._components(directory)
            host.config = ProductionHostConfig(
                journal_path=config.journal_path,
                account_id=config.account_id,
                environment=config.environment,
                host_id=config.host_id,
                bind_host=config.bind_host,
                bind_port=config.bind_port,
                public_origin=config.public_origin,
            )
            with self.assertRaisesRegex(
                PermissionError,
                "config authority changed",
            ):
                _ = runtime.config

    def test_runtime_config_scalar_mutation_fails_closed(self):
        with TemporaryDirectory() as directory:
            _journal, config, _host, *_rest, runtime = self._components(directory)
            object.__setattr__(config, "host_id", "host-retargeted")
            with self.assertRaisesRegex(
                PermissionError,
                "identity changed after composition",
            ):
                _ = runtime.owner

    def test_runtime_journal_retarget_fails_closed(self):
        with TemporaryDirectory() as directory:
            _journal, _config, host, *_rest, runtime = self._components(directory)
            host.journal = JournalStore(Path(directory) / "other.sqlite3")
            with self.assertRaisesRegex(
                PermissionError,
                "journal changed after composition",
            ):
                _ = runtime.journal

    def test_runtime_authority_references_are_read_only_properties(self):
        with TemporaryDirectory() as directory:
            *_, runtime = self._components(directory)
            for name in (
                "host",
                "recovery_controller",
                "owner",
                "provider_secret_resolver",
                "dispatcher",
            ):
                with self.subTest(name=name):
                    with self.assertRaises(AttributeError):
                        setattr(runtime, name, object())

    def test_duck_boundary_method_retarget_does_not_redirect_retained_lease(self):
        boundary = _LeaseBoundary()
        resolver = HostLifetimeProviderSecretResolver(
            boundary,
            account_id="account-1",
            environment="PAPER",
        )
        hostile_calls: list[str] = []

        @contextmanager
        def hostile(*_args, **_kwargs):
            hostile_calls.append("called")
            yield "hostile"

        boundary.lease_for_execution = hostile  # type: ignore[method-assign]
        with resolver.lease_for_execution(
            "token",
            origin="http://127.0.0.1:19071",
            handle=object(),
            execution_identity="host-a",
            provider="BYBIT",
            provider_environment="TESTNET",
        ) as plaintext:
            self.assertEqual(plaintext, "secret")

        self.assertEqual(hostile_calls, [])
        self.assertEqual(len(boundary.calls), 1)
        _, kwargs = boundary.calls[0]
        self.assertEqual(kwargs["account_id"], "account-1")
        self.assertEqual(kwargs["environment"], "PAPER")
        self.assertEqual(kwargs["purpose"], "TRADE")

    def test_security_lease_class_rebinding_is_detected(self):
        original = SecurityBoundary.lease_for_execution

        def replacement(_self, *_args, **_kwargs):
            raise AssertionError("replacement credential lease executed")

        SecurityBoundary.lease_for_execution = replacement
        try:
            with self.assertRaisesRegex(
                PermissionError,
                "credential lease authority changed",
            ):
                financial_host_module._require_security_lease_executable()
        finally:
            SecurityBoundary.lease_for_execution = original

    def test_security_lease_same_function_code_mutation_is_detected_before_execution(self):
        canonical = financial_host_module._CANONICAL_SECURITY_LEASE
        original_code = canonical.__code__
        _FORGED_LEASE_CALLS.clear()
        try:
            canonical.__code__ = _forged_security_lease.__code__
            with self.assertRaisesRegex(
                PermissionError,
                "credential lease code changed",
            ):
                financial_host_module._require_security_lease_executable()
        finally:
            canonical.__code__ = original_code
        self.assertEqual(_FORGED_LEASE_CALLS, [])


if __name__ == "__main__":
    unittest.main()
