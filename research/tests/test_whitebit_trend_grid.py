from datetime import UTC, datetime, timedelta
from decimal import Decimal
import unittest

from tools.screen_whitebit_trend_grid import (
    default_grid,
    locked_final_grid_screen,
)


def bars(count=2000):
    start = datetime(2026, 1, 1, tzinfo=UTC)
    result = []
    price = Decimal("100")
    for index in range(count):
        price *= Decimal("1.0001")
        opened = start + timedelta(hours=index)
        close = price.quantize(Decimal("0.00000001"))
        result.append({"opened_at": opened, "closed_at": opened+timedelta(hours=1),
            "open": str(close), "high": str((close*Decimal("1.001")).quantize(Decimal("0.00000001"))),
            "low": str((close*Decimal("0.999")).quantize(Decimal("0.00000001"))),
            "close": str(close), "volume": "1000"})
    return result


class WhitebitTrendGridTests(unittest.TestCase):
    def test_default_grid_is_unique_and_locked_final(self):
        value = locked_final_grid_screen(
            bars(), train_count=1200, development_count=400,
            dataset_sha256="a"*64, configurations=default_grid())
        self.assertEqual(value["configuration_count"], 6)
        self.assertEqual(len({row["configuration_sha256"] for row in value["screened"]}), 6)
        self.assertFalse(value["final_consumed"])
        self.assertEqual(value["selected_candidate"], "CASH")
        self.assertFalse(value["network_orders"])
        self.assertFalse(value["trading_authority"])


if __name__ == "__main__":
    unittest.main()

