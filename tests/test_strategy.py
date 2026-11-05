from datetime import date, timedelta
import unittest

from src.gldrubf_bot.strategy import DailyClose, crossover, sma


class StrategyTests(unittest.TestCase):
    def test_sma(self):
        self.assertEqual(sma([1, 2, 3], 2), 2.5)

    def test_requires_warmup(self):
        with self.assertRaises(ValueError):
            crossover([DailyClose(date(2026, 1, 1), 100.0)])

    def test_up_cross(self):
        prices = [100.0] * 30 + [90.0] * 20 + [200.0]
        bars = [DailyClose(date(2026, 1, 1) + timedelta(days=i), p) for i, p in enumerate(prices)]
        self.assertEqual(crossover(bars).target, 1)

    def test_no_cross(self):
        prices = [100.0] * 51
        bars = [DailyClose(date(2026, 1, 1) + timedelta(days=i), p) for i, p in enumerate(prices)]
        self.assertEqual(crossover(bars).target, 0)


if __name__ == "__main__":
    unittest.main()
