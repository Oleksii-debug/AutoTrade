import unittest

from mvp.autotrade_mvp import durable_financial_bybit_sender as module
from mvp.autotrade_mvp.durable_financial_bybit_sender import (
    DurableFinancialBybitSenderError,
    DurableFinanciallyBoundBybitOrderSender,
)
from mvp.autotrade_mvp.durable_financial_request_binding import (
    DurableFinancialRequestBindingRegistry,
)
from mvp.autotrade_mvp.durable_financial_send_issuance import (
    DurableFinancialSendIssuanceError,
)
from mvp.autotrade_mvp.financial_send_authority import (
    FinancialSendAuthorityIssuer,
    FinanciallyBoundBybitOrderSender,
)
from mvp.autotrade_mvp.production_bybit import build_production_bybit_order_sender


_FORGED_CALLS = []


def _forged_require_store(_self):
    _FORGED_CALLS.append("require_store")
    raise AssertionError("forged registry store executable ran")


class DurableFinancialBybitProductSurfaceTests(unittest.TestCase):
    @staticmethod
    def _arguments(function):
        code = function.__code__
        return code.co_varnames[: code.co_argcount + code.co_kwonlyargcount]

    @staticmethod
    def _dispatch_shell():
        issuer = object.__new__(FinancialSendAuthorityIssuer)
        registry = object.__new__(DurableFinancialRequestBindingRegistry)
        sender = object.__new__(FinanciallyBoundBybitOrderSender)
        product = object.__new__(DurableFinanciallyBoundBybitOrderSender)
        runtime = object()
        store = object()

        object.__setattr__(
            issuer,
            "_FinancialSendAuthorityIssuer__runtime",
            runtime,
        )
        object.__setattr__(
            issuer,
            "_FinancialSendAuthorityIssuer__journal",
            store,
        )
        object.__setattr__(registry, "_store", store)
        object.__setattr__(
            sender,
            "_FinanciallyBoundBybitOrderSender__issuer",
            issuer,
        )
        object.__setattr__(
            sender,
            "_FinanciallyBoundBybitOrderSender__runtime",
            runtime,
        )
        object.__setattr__(
            sender,
            "_FinanciallyBoundBybitOrderSender__provider_environment",
            "TESTNET",
        )
        dispatch_function = FinanciallyBoundBybitOrderSender.__dict__["dispatch"]
        dispatch = dispatch_function.__get__(sender, FinanciallyBoundBybitOrderSender)
        object.__setattr__(
            product,
            "_DurableFinanciallyBoundBybitOrderSender__sender",
            sender,
        )
        object.__setattr__(
            product,
            "_DurableFinanciallyBoundBybitOrderSender__issuer",
            issuer,
        )
        object.__setattr__(
            product,
            "_DurableFinanciallyBoundBybitOrderSender__registry",
            registry,
        )
        object.__setattr__(
            product,
            "_DurableFinanciallyBoundBybitOrderSender__runtime",
            runtime,
        )
        object.__setattr__(
            product,
            "_DurableFinanciallyBoundBybitOrderSender__store",
            store,
        )
        object.__setattr__(
            product,
            "_DurableFinanciallyBoundBybitOrderSender__provider_environment",
            "TESTNET",
        )
        object.__setattr__(
            product,
            "_DurableFinanciallyBoundBybitOrderSender__sender_dispatch",
            dispatch,
        )
        return product

    def test_product_builder_requires_durable_financial_binding_registry(self):
        arguments = self._arguments(build_production_bybit_order_sender)
        self.assertIn("financial_issuer", arguments)
        self.assertIn("financial_binding_registry", arguments)
        self.assertNotIn("binding", arguments)
        self.assertNotIn("authority", arguments)
        self.assertNotIn("authority_check", arguments)

    def test_product_dispatch_resolves_by_admission_not_detached_authority(self):
        arguments = self._arguments(DurableFinanciallyBoundBybitOrderSender.dispatch)
        self.assertIn("admission_id", arguments)
        self.assertIn("action", arguments)
        self.assertIn("attempt_id", arguments)
        self.assertIn("intent_id", arguments)
        self.assertIn("intent_hash", arguments)
        self.assertNotIn("binding", arguments)
        self.assertNotIn("authority", arguments)
        self.assertNotIn("authority_check", arguments)
        self.assertNotIn("final_barrier_clock", arguments)
        self.assertNotIn("issuer", arguments)
        self.assertNotIn("registry", arguments)

    def test_product_sender_cannot_be_directly_constructed(self):
        with self.assertRaisesRegex(
            DurableFinancialBybitSenderError,
            "requires canonical composition",
        ):
            DurableFinanciallyBoundBybitOrderSender(object(), object(), object())

    def test_registry_executable_rebinding_is_caught_by_mint_before_execution(self):
        product = self._dispatch_shell()
        original = DurableFinancialRequestBindingRegistry._require_store
        _FORGED_CALLS.clear()
        try:
            DurableFinancialRequestBindingRegistry._require_store = _forged_require_store
            with self.assertRaisesRegex(
                DurableFinancialSendIssuanceError,
                "registry _require_store executable authority changed",
            ):
                product.dispatch(
                    admission_id="admission-1",
                    action="TRADE",
                    attempt_id="attempt-1",
                    intent_id="intent-1",
                    intent_hash="intent-hash-1",
                    request={},
                    now="2026-10-05T08:00:00Z",
                )
        finally:
            DurableFinancialRequestBindingRegistry._require_store = original
        self.assertEqual(_FORGED_CALLS, [])

    def test_structural_preflight_does_not_execute_registry_resolver(self):
        product = self._dispatch_shell()
        original = DurableFinancialRequestBindingRegistry._require_store
        _FORGED_CALLS.clear()
        try:
            DurableFinancialRequestBindingRegistry._require_store = _forged_require_store
            module._structural_authorities(
                object.__getattribute__(
                    product,
                    "_DurableFinanciallyBoundBybitOrderSender__sender",
                ),
                object.__getattribute__(
                    product,
                    "_DurableFinanciallyBoundBybitOrderSender__issuer",
                ),
                object.__getattribute__(
                    product,
                    "_DurableFinanciallyBoundBybitOrderSender__registry",
                ),
            )
        finally:
            DurableFinancialRequestBindingRegistry._require_store = original
        self.assertEqual(_FORGED_CALLS, [])


if __name__ == "__main__":
    unittest.main()
