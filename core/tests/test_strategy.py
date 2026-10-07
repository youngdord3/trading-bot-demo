import unittest
from decimal import Decimal as D
from strategy import Signal, compute_ema, evaluate_crossover, parse_closed_candles

M = 900_000


def kl(i, close, closed=True):
    return [i * M, str(close), "0", "0", str(close), "0", i * M + M - 1]


class TestStrategy(unittest.TestCase):
    def test_ema_constant(self):
        self.assertTrue(all(v == D(5) for v in compute_ema([D(5)] * 30, 9)))

    def test_single_buy_on_v_shape(self):
        closes = [D(100 - i) for i in range(30)] + [D(70 + 3 * i) for i in range(30)]
        sig = [evaluate_crossover(closes[:n]) for n in range(22, len(closes) + 1)]
        self.assertEqual(sig.count(Signal.BUY), 1)
        self.assertEqual(sig.count(Signal.SELL), 0)

    def test_single_sell_on_inverted_v(self):
        closes = [D(100 + i) for i in range(30)] + [D(130 - 3 * i) for i in range(30)]
        sig = [evaluate_crossover(closes[:n]) for n in range(22, len(closes) + 1)]
        self.assertEqual(sig.count(Signal.SELL), 1)
        self.assertEqual(sig.count(Signal.BUY), 0)

    def test_open_candle_dropped(self):
        raw = [kl(i, 100) for i in range(5)]
        server_now = 4 * M + 10      # candela 4 ancora aperta
        self.assertEqual(len(parse_closed_candles(raw, server_now, M)), 4)

    def test_gap_detected(self):
        raw = [kl(0, 1), kl(1, 1), kl(3, 1)]
        with self.assertRaises(ValueError):
            parse_closed_candles(raw, 10 * M, M)

    def test_insufficient_data_is_hold(self):
        self.assertEqual(evaluate_crossover([D(1)] * 21), Signal.HOLD)


if __name__ == "__main__":
    unittest.main()
