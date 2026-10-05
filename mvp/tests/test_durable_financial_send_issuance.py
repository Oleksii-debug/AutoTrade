from __future__ import annotations

import inspect
from types import SimpleNamespace
import unittest

from mvp.autotrade_mvp import durable_financial_send_issuance as module
from mvp.autotrade_mvp.durable_financial_request_binding import (
    DurableFinancialRequestBindingRegistry,
)
from mvp.autotrade_mvp.financial_send_authority import FinancialSendAuthorityIssuer
from mvp.tests.test_financial_send_authority import binding


_FORGED_CALLS: list[str] = []


def _forged_resolve(self, admission_id):
    _FORGED_CALLS.append(f"resolve:{admission_id}")
    raise AssertionError("forged durable binding resolver executed")


def _forged_issue(self, **_kwargs):
    _FORGED_CALLS.append("issue")
    raise AssertionError("forged financial authority mint executed")


def _forged_current(self):
    _FORGED_CALLS.append("current")
    raise AssertionError("forged issuer currentness executable ran")


def _forged_runtime(self):
    _FORGED_CALLS.append("runtime")
    raise AssertionError("forged issuer runtime getter executed")


def _forged_load_payload(self, admission_id):
    _FORGED_CALLS.append(f"load:{admission_id}")
    raise AssertionError("forged durable binding payload reader executed")


class DurableFinancialSendIssuanceTests(unittest.TestCase):
    @staticmethod
    def _shells():
        return (
            object.__new__(FinancialSendAuthorityIssuer),
            object.__new__(DurableFinancialRequestBindingRegistry),
        )

    def test_internal_composition_resolves_persisted_binding_and_forwards_exact_material(self):
        issuer, registry = self._shells()
        journal = object()
        material = binding()
        calls = []
        sentinel = object()

        def resolve(actual_registry, admission_id):
            calls.append(("resolve", actual_registry, admission_id))
            return material

        def issue(actual_issuer, **kwargs):
            calls.append(("issue", actual_issuer, kwargs))
            return sentinel

        result = module._issue_with_authorities(
            issuer,
            registry,
            admission_id="admission-1",
            intent_id="intent-1",
            intent_hash="intent-hash-1",
            action="TRADE",
            require_store=lambda actual: journal if actual is registry else None,
            runtime_getter=lambda actual: SimpleNamespace(journal=journal)
            if actual is issuer
            else None,
            resolve=resolve,
            issue=issue,
        )

        self.assertIs(result, sentinel)
        self.assertEqual(calls[0], ("resolve", registry, "admission-1"))
        self.assertEqual(calls[1][0:2], ("issue", issuer))
        kwargs = calls[1][2]
        self.assertIs(kwargs["binding"], material)
        self.assertEqual(kwargs["admission_id"], "admission-1")
        self.assertEqual(kwargs["intent_id"], "intent-1")
        self.assertEqual(kwargs["intent_hash"], "intent-hash-1")
        self.assertEqual(kwargs["action"], "TRADE")

    def test_cross_store_composition_fails_before_binding_resolution_or_mint(self):
        issuer, registry = self._shells()
        registry_journal = object()
        issuer_journal = object()
        calls = []

        with self.assertRaisesRegex(
            module.DurableFinancialSendIssuanceError,
            "share one exact JournalStore",
        ):
            module._issue_with_authorities(
                issuer,
                registry,
                admission_id="admission-1",
                intent_id="intent-1",
                intent_hash="intent-hash-1",
                action="TRADE",
                require_store=lambda _registry: registry_journal,
                runtime_getter=lambda _issuer: SimpleNamespace(journal=issuer_journal),
                resolve=lambda *_args: calls.append("resolve"),
                issue=lambda *_args, **_kwargs: calls.append("issue"),
            )
        self.assertEqual(calls, [])

    def test_noncanonical_resolved_material_fails_before_mint(self):
        issuer, registry = self._shells()
        journal = object()
        calls = []
        with self.assertRaisesRegex(
            module.DurableFinancialSendIssuanceError,
            "exact FinancialRequestBindingMaterial",
        ):
            module._issue_with_authorities(
                issuer,
                registry,
                admission_id="admission-1",
                intent_id="intent-1",
                intent_hash="intent-hash-1",
                action="TRADE",
                require_store=lambda _registry: journal,
                runtime_getter=lambda _issuer: SimpleNamespace(journal=journal),
                resolve=lambda *_args: object(),
                issue=lambda *_args, **_kwargs: calls.append("issue"),
            )
        self.assertEqual(calls, [])

    def test_public_mint_surface_has_no_caller_binding_argument(self):
        parameters = inspect.signature(
            module.issue_persisted_financial_send_authority
        ).parameters
        self.assertNotIn("binding", parameters)
        self.assertEqual(
            tuple(parameters),
            ("issuer", "registry", "admission_id", "intent_id", "intent_hash", "action"),
        )

    def test_registry_resolve_rebinding_fails_before_forged_executable(self):
        issuer, registry = self._shells()
        original = DurableFinancialRequestBindingRegistry.resolve
        _FORGED_CALLS.clear()
        try:
            DurableFinancialRequestBindingRegistry.resolve = _forged_resolve
            with self.assertRaisesRegex(
                module.DurableFinancialSendIssuanceError,
                "registry resolve executable authority changed",
            ):
                module.issue_persisted_financial_send_authority(
                    issuer,
                    registry,
                    admission_id="admission-1",
                    intent_id="intent-1",
                    intent_hash="intent-hash-1",
                    action="TRADE",
                )
        finally:
            DurableFinancialRequestBindingRegistry.resolve = original
        self.assertEqual(_FORGED_CALLS, [])

    def test_registry_reader_rebinding_fails_before_forged_executable(self):
        issuer, registry = self._shells()
        original = DurableFinancialRequestBindingRegistry._load_payload
        _FORGED_CALLS.clear()
        try:
            DurableFinancialRequestBindingRegistry._load_payload = _forged_load_payload
            with self.assertRaisesRegex(
                module.DurableFinancialSendIssuanceError,
                "registry _load_payload executable authority changed",
            ):
                module.issue_persisted_financial_send_authority(
                    issuer,
                    registry,
                    admission_id="admission-1",
                    intent_id="intent-1",
                    intent_hash="intent-hash-1",
                    action="TRADE",
                )
        finally:
            DurableFinancialRequestBindingRegistry._load_payload = original
        self.assertEqual(_FORGED_CALLS, [])

    def test_registry_same_function_code_mutation_fails_before_forged_executable(self):
        issuer, registry = self._shells()
        target = DurableFinancialRequestBindingRegistry.resolve
        original_code = target.__code__
        _FORGED_CALLS.clear()
        try:
            target.__code__ = _forged_resolve.__code__
            with self.assertRaisesRegex(
                module.DurableFinancialSendIssuanceError,
                "registry resolve executable authority changed",
            ):
                module.issue_persisted_financial_send_authority(
                    issuer,
                    registry,
                    admission_id="admission-1",
                    intent_id="intent-1",
                    intent_hash="intent-hash-1",
                    action="TRADE",
                )
        finally:
            target.__code__ = original_code
        self.assertEqual(_FORGED_CALLS, [])

    def test_issuer_mint_rebinding_fails_before_forged_executable(self):
        issuer, registry = self._shells()
        original = FinancialSendAuthorityIssuer.issue
        _FORGED_CALLS.clear()
        try:
            FinancialSendAuthorityIssuer.issue = _forged_issue
            with self.assertRaisesRegex(
                module.DurableFinancialSendIssuanceError,
                "issuer mint executable authority changed",
            ):
                module.issue_persisted_financial_send_authority(
                    issuer,
                    registry,
                    admission_id="admission-1",
                    intent_id="intent-1",
                    intent_hash="intent-hash-1",
                    action="TRADE",
                )
        finally:
            FinancialSendAuthorityIssuer.issue = original
        self.assertEqual(_FORGED_CALLS, [])

    def test_issuer_currentness_rebinding_fails_before_forged_executable(self):
        issuer, registry = self._shells()
        original = FinancialSendAuthorityIssuer._require_current
        _FORGED_CALLS.clear()
        try:
            FinancialSendAuthorityIssuer._require_current = _forged_current
            with self.assertRaisesRegex(
                module.DurableFinancialSendIssuanceError,
                "issuer currentness executable authority changed",
            ):
                module.issue_persisted_financial_send_authority(
                    issuer,
                    registry,
                    admission_id="admission-1",
                    intent_id="intent-1",
                    intent_hash="intent-hash-1",
                    action="TRADE",
                )
        finally:
            FinancialSendAuthorityIssuer._require_current = original
        self.assertEqual(_FORGED_CALLS, [])

    def test_issuer_runtime_descriptor_rebinding_fails_before_forged_getter(self):
        issuer, registry = self._shells()
        original = FinancialSendAuthorityIssuer.__dict__["runtime"]
        _FORGED_CALLS.clear()
        try:
            FinancialSendAuthorityIssuer.runtime = property(_forged_runtime)
            with self.assertRaisesRegex(
                module.DurableFinancialSendIssuanceError,
                "issuer runtime executable authority changed",
            ):
                module.issue_persisted_financial_send_authority(
                    issuer,
                    registry,
                    admission_id="admission-1",
                    intent_id="intent-1",
                    intent_hash="intent-hash-1",
                    action="TRADE",
                )
        finally:
            FinancialSendAuthorityIssuer.runtime = original
        self.assertEqual(_FORGED_CALLS, [])

    def test_non_exact_authority_objects_are_rejected_before_callbacks(self):
        issuer, registry = self._shells()
        with self.assertRaisesRegex(TypeError, "issuer must be exact"):
            module.issue_persisted_financial_send_authority(
                object(),
                registry,
                admission_id="admission-1",
                intent_id="intent-1",
                intent_hash="intent-hash-1",
                action="TRADE",
            )
        with self.assertRaisesRegex(TypeError, "registry must be exact"):
            module.issue_persisted_financial_send_authority(
                issuer,
                object(),
                admission_id="admission-1",
                intent_id="intent-1",
                intent_hash="intent-hash-1",
                action="TRADE",
            )


if __name__ == "__main__":
    unittest.main()
