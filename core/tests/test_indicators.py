"""Test offline degli indicatori puri."""
import unittest
from decimal import Decimal as D

from indicators import adx, atr, ema, rsi, sma, true_range
from strategy import compute_ema


def _d(values):
    return [D(str(v)) for v in values]


class IndicatorsTest(unittest.TestCase):
    def test_sma_values(self):
        self.assertEqual(sma(_d([1, 2, 3, 4, 5]), 3), [None, None, D(2), D(3), D(4)])

    def test_ema_values(self):
        self.assertEqual(ema(_d([1, 2, 3, 4, 5]), 3), [None, None, D(2), D(3), D(4)])

    def test_ema_matches_strategy_compute_ema(self):
        values = _d([100, 101.5, 99.2, 103.7, 104.1, 102.3, 105.9, 107.4, 106.2, 108.8])
        self.assertEqual(ema(values, 4)[3:], compute_ema(values, 4))

    def test_true_range_values(self):
        tr = true_range(_d([10, 12, 11]), _d([8, 9, 7]), _d([9, 11, 8]))
        self.assertEqual(tr, [D(2), D(3), D(4)])

    def test_atr_values(self):
        out = atr(_d([10, 12, 11]), _d([8, 9, 7]), _d([9, 11, 8]), 2)
        self.assertEqual(out, [None, D("2.5"), D("3.25")])

    def test_adx_strong_trend_is_100(self):
        highs = _d([10 + i for i in range(12)])
        lows = _d([8 + i for i in range(12)])
        closes = _d([9 + i for i in range(12)])
        out = adx(highs, lows, closes, 3)
        for value in out[5:]:
            self.assertEqual(value, D(100))

    def test_adx_flat_market_is_zero(self):
        out = adx(_d([10] * 12), _d([8] * 12), _d([9] * 12), 3)
        for value in out[5:]:
            self.assertEqual(value, D(0))

    def test_adx_warm_up_index(self):
        out = adx(_d([10 + i for i in range(12)]), _d([8 + i for i in range(12)]), _d([9 + i for i in range(12)]), 3)
        self.assertEqual(len(out), 12)
        self.assertEqual(out[:5], [None] * 5)
        self.assertIsNotNone(out[5])

    def test_rsi_values(self):
        self.assertEqual(rsi(_d([10, 13, 12]), 2), [None, None, D(75)])

    def test_rsi_extremes(self):
        self.assertEqual(rsi(_d([1, 2, 3, 4]), 2)[2:], [D(100), D(100)])
        self.assertEqual(rsi(_d([5, 4, 3, 2]), 2)[2:], [D(0), D(0)])
        self.assertEqual(rsi(_d([7, 7, 7, 7]), 2)[2:], [D(50), D(50)])

    def test_short_input_returns_all_none(self):
        self.assertEqual(sma(_d([1, 2]), 3), [None, None])
        self.assertEqual(ema(_d([1, 2]), 3), [None, None])
        self.assertEqual(atr(_d([2, 3]), _d([1, 2]), _d([1, 2]), 3), [None, None])
        self.assertEqual(adx(_d([2, 3, 4]), _d([1, 2, 3]), _d([1, 2, 3]), 2), [None] * 3)
        self.assertEqual(rsi(_d([1, 2]), 2), [None, None])

    def test_invalid_period_raises(self):
        for func in (sma, ema, rsi):
            with self.assertRaises(ValueError):
                func(_d([1, 2, 3]), 0)
        with self.assertRaises(ValueError):
            atr(_d([1]), _d([1]), _d([1]), 0)
        with self.assertRaises(ValueError):
            adx(_d([1]), _d([1]), _d([1]), 0)

    def test_length_mismatch_raises(self):
        with self.assertRaises(ValueError):
            true_range(_d([1, 2]), _d([1]), _d([1, 2]))


if __name__ == "__main__":
    unittest.main()
