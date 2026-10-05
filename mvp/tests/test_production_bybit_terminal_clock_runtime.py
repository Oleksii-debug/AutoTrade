from __future__ import annotations

from datetime import datetime, timezone
import unittest

from mvp.autotrade_mvp import production_bybit


class ProductionBybitTerminalClockRuntimeTests(unittest.TestCase):
    def test_trusted_final_barrier_clock_runtime_dependencies_are_bound(self):
        dispatch_globals = production_bybit.ProductionBybitOrderSender.dispatch.__globals__

        self.assertIs(dispatch_globals.get("datetime"), datetime)
        self.assertIs(dispatch_globals.get("timezone"), timezone)


if __name__ == "__main__":
    unittest.main()
