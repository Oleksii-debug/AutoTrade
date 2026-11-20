import unittest
from weekly_learn import choose, simulate, HOUR, WEEK


class WeeklyReplayTests(unittest.TestCase):
    def test_neighbors_exclude_just_settled_and_future_weeks(self):
        now = 110 * WEEK

        def candidate(t, value):
            return dict(
                time=t, features=[0.0] * 4, settled={"OPEN": value, "DIP": value}
            )

        data = [candidate(now - n * WEEK, 0.02) for n in range(2, 26)]
        expected = choose(now, [0.0] * 4, data)
        self.assertEqual(expected[0], "DIP")
        self.assertEqual(len(expected[1]), 12)
        for t in (now - WEEK, now, now + WEEK):
            data.append(candidate(t, 99999.0))
        self.assertEqual(choose(now, [0.0] * 4, data), expected)

    def test_intrabar_order_and_transaction_costs(self):
        bars = {t: (100, 100, 100, 100, 1) for t in range(0, WEEK + 1, HOUR)}
        bars[0] = (100, 110, 96.5, 100, 1)
        self.assertEqual(simulate(bars, 0, "OPEN", 0.0015)["reason"], "STOP")
        self.assertEqual(simulate(bars, 0, "DIP", 0.0015)["reason"], "DEADLINE")
        bars[0] = (100, 110, 99, 100, 1)
        sale = simulate(bars, 0, "OPEN", 0.0015)
        self.assertEqual(sale["reason"], "TARGET")
        self.assertGreater(sale["net"], 0)
        self.assertLess(sale["net"], 0.05)
        self.assertEqual(simulate(bars, 0, "DIP", 0.0015)["reason"], "NO_FILL")


if __name__ == "__main__":
    unittest.main()
