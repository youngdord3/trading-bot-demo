import unittest
from decimal import Decimal as D
from strategy import (
    Candle,
    Signal,
    TsmomParams,
    advance_trailing,
    check_bar_exit,
    compute_ema,
    efficiency_ratio,
    evaluate_crossover,
    evaluate_tsmom_erf,
    initial_trailing_state,
    parse_closed_candles,
    replay_trailing_state,
)

M = 900_000
D1_MS = 86_400_000


def kl(i, close, closed=True):
    return [i * M, str(close), "0", "0", str(close), "0", i * M + M - 1]


def make_d1_uptrend(n: int = 70, step: D = D("2"), last_jump: D = D("2")) -> list[Candle]:
    candles: list[Candle] = []
    price = D("100")
    for i in range(n):
        t0 = (1_000 + i) * D1_MS
        t1 = t0 + D1_MS - 1
        inc = last_jump if i == n - 1 else step
        open_p = price
        close_p = price + inc
        high_p = max(open_p, close_p) + D("1")
        low_p = min(open_p, close_p) - D("0.5")
        candles.append(Candle(t0, t1, open_p, close_p, high_p, low_p))
        price = close_p
    return candles


def make_d1_downtrend(n: int = 70, step: D = D("2"), last_drop: D = D("2")) -> list[Candle]:
    candles: list[Candle] = []
    price = D("300")
    for i in range(n):
        t0 = (1_000 + i) * D1_MS
        t1 = t0 + D1_MS - 1
        dec = last_drop if i == n - 1 else step
        open_p = price
        close_p = price - dec
        high_p = max(open_p, close_p) + D("0.5")
        low_p = min(open_p, close_p) - D("1")
        candles.append(Candle(t0, t1, open_p, close_p, high_p, low_p))
        price = close_p
    return candles


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

    def test_parse_closed_candles_extracts_high_low(self):
        raw = [[0, "100", "105.5", "98.2", "102", "10", M - 1]]
        candles = parse_closed_candles(raw, 2 * M, M)
        self.assertEqual(len(candles), 1)
        self.assertEqual(candles[0].high, D("105.5"))
        self.assertEqual(candles[0].low, D("98.2"))

    def test_efficiency_ratio_trend_chop_and_flat(self):
        monotonic = [D(100 + i) for i in range(15)]
        er_mono = efficiency_ratio(monotonic, 10)
        self.assertEqual(er_mono[-1], D("1"))

        chop = [D(100 if i % 2 == 0 else 105) for i in range(15)]
        er_chop = efficiency_ratio(chop, 10)
        self.assertLess(er_chop[-1], D("0.15"))

        flat = [D(100) for _ in range(15)]
        er_flat = efficiency_ratio(flat, 10)
        self.assertEqual(er_flat[-1], D("0"))

    def test_tsmom_erf_warmup_is_hold(self):
        candles = make_d1_uptrend(n=45)
        res = evaluate_tsmom_erf(candles, [])
        self.assertEqual(res.signal, Signal.HOLD)
        self.assertEqual(res.reason, "WARMUP")

    def test_tsmom_erf_long_signal_and_funding_gate(self):
        candles = make_d1_uptrend(n=70, step=D("2"), last_jump=D("3"))
        neutral_funding = [(candles[-1].open_time_ms, D("0.00010"))]
        res = evaluate_tsmom_erf(candles, neutral_funding)
        self.assertEqual(res.signal, Signal.BUY)
        self.assertGreater(res.atr14, D("0"))

        crowded_last = [(candles[-1].open_time_ms, D("0.00030"))]
        self.assertEqual(evaluate_tsmom_erf(candles, crowded_last).signal, Signal.HOLD)

        crowded_sum3 = [
            (candles[-1].open_time_ms - 16 * 3_600_000, D("0.00026")),
            (candles[-1].open_time_ms - 8 * 3_600_000, D("0.00026")),
            (candles[-1].open_time_ms, D("0.00025")),
        ]
        self.assertEqual(evaluate_tsmom_erf(candles, crowded_sum3).signal, Signal.HOLD)

    def test_tsmom_erf_short_signal_and_funding_gate(self):
        candles = make_d1_downtrend(n=70, step=D("2"), last_drop=D("3"))
        neutral_funding = [(candles[-1].open_time_ms, D("0.00005"))]
        res = evaluate_tsmom_erf(candles, neutral_funding)
        self.assertEqual(res.signal, Signal.SELL)

        crowded_short = [(candles[-1].open_time_ms, D("-0.00010"))]
        self.assertEqual(evaluate_tsmom_erf(candles, crowded_short).signal, Signal.HOLD)

    def test_tsmom_erf_blocks_overextended_spike(self):
        candles = make_d1_uptrend(n=70, step=D("2"), last_jump=D("40"))
        res = evaluate_tsmom_erf(candles, [])
        self.assertEqual(res.signal, Signal.HOLD)
        self.assertEqual(res.reason, "OVEREXTENDED")

    def test_trailing_stop_and_replay_long_and_short(self):
        slip = D("0.0004")
        state = initial_trailing_state("LONG", D("100"), D("4"), TsmomParams())
        self.assertEqual(state.stop_price, D("94"))   # 100 - 1.5*4
        self.assertEqual(state.trail_distance, D("8"))  # 2.0*4

        bars = [
            Candle(1, 2, D("100"), D("106"), D("108"), D("99")),  # best=108 -> stop=100
            Candle(2, 3, D("106"), D("112"), D("115"), D("105")), # best=115 -> stop=107 (> entry)
            Candle(3, 4, D("112"), D("110"), D("113"), D("109")), # pullback, low=109 > 107 -> no exit
        ]
        replayed = replay_trailing_state(state, bars)
        self.assertEqual(replayed.best_price, D("115"))
        self.assertEqual(replayed.stop_price, D("107"))

        gap_down = Candle(4, 5, D("104"), D("103"), D("105"), D("102"))
        exit_hit = check_bar_exit(replayed, gap_down, slip)
        self.assertIsNotNone(exit_hit)
        fill, reason = exit_hit
        self.assertEqual(reason, "TRAIL")
        self.assertEqual(fill, D("104") * (D("1") - slip))

        short_state = initial_trailing_state("SHORT", D("200"), D("4"), TsmomParams())
        self.assertEqual(short_state.stop_price, D("206"))
        short_adv = advance_trailing(short_state, Candle(1, 2, D("198"), D("185"), D("199"), D("184")))
        self.assertEqual(short_adv.stop_price, D("192"))
        gap_up = Candle(2, 3, D("195"), D("196"), D("197"), D("194"))
        s_exit = check_bar_exit(short_adv, gap_up, slip)
        self.assertEqual(s_exit, (D("195") * (D("1") + slip), "TRAIL"))


if __name__ == "__main__":
    unittest.main()
