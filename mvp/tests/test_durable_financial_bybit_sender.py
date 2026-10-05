import unittest

from mvp.autotrade_mvp.durable_financial_bybit_sender import (
    DurableFinancialBybitSenderError,
    DurableFinanciallyBoundBybitOrderSender,
)
from mvp.autotrade_mvp.production_bybit import build_production_bybit_order_sender


class DurableFinancialBybitProductSurfaceTests(unittest.TestCase):
    @staticmethod
    def _arguments(function):
        code = function.__code__
        return code.co_varnames[: code.co_argcount + code.co_kwonlyargcount]

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


if __name__ == "__main__":
    unittest.main()
