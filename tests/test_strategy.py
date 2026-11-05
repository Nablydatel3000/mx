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
        prices = [100.0] * 30 + [90.0] * 20 + [400.0]
        bars = [DailyClose(date(2026, 1, 1) + timedelta(days=i), p) for i, p in enumerate(prices)]
        self.assertEqual(crossover(bars).target, 1)

    def test_no_cross(self):
        prices = [100.0] * 51
        bars = [DailyClose(date(2026, 1, 1) + timedelta(days=i), p) for i, p in enumerate(prices)]
        self.assertEqual(crossover(bars).target, 0)

class AdditionalStrategyTests(unittest.TestCase):
    def test_down_cross(self):
        prices = [100.0]*30+[101.0]*20+[1.0]
        bars = [DailyClose(date(2026,1,1)+timedelta(days=i),p) for i,p in enumerate(prices)]
        self.assertEqual(crossover(bars).target,-1)

    def test_invalid_order_duplicates_and_prices(self):
        valid=[DailyClose(date(2026,1,1)+timedelta(days=i),100.0) for i in range(51)]
        for b in [valid[::-1],valid[:-1]+[valid[-2]],valid[:-1]+[DailyClose(valid[-1].day,float('nan'))]]:
            with self.assertRaises(ValueError):crossover(b)
        for x in [float('nan'),float('inf'),0,-1,True]:
            with self.assertRaises(ValueError):sma([x],1)
        with self.assertRaises(ValueError):sma([1e308]*50,50)


if __name__ == "__main__":
    unittest.main()
