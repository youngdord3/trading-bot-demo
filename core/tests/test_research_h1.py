import unittest
from decimal import Decimal

import research_h1 as rh


def d(value):
    return Decimal(str(value))


def make_market(trend_value):
    count = 6
    opens = [d(100), d(100), d(100), d(100), d(101), d(101)]
    highs = [d(101), d(101), d(101), d(101), d(105), d(102)]
    lows = [d(99), d(99), d(99), d(99), d(100), d(100)]
    closes = [d(100), d(100), d(100), d(100), d(104), d(101)]
    return {
        "TESTUSDT": {
            "ots": [i * rh.H1_MS for i in range(count)],
            "opens": opens,
            "highs": highs,
            "lows": lows,
            "closes": closes,
            "atr": [d(2)] * count,
            "adx": [d(30)] * count,
            "trend": [trend_value] * count,
            "emas": {
                2: [d(1), d(1), d(2), d(2), d(2), d(2)],
                3: [d("1.5")] * count,
            },
        }
    }


class IndicatorTests(unittest.TestCase):
    def test_ema_seed_and_recursion(self):
        out = rh.ema([d(1), d(2), d(3), d(4), d(5)], 3)
        self.assertEqual(out, [None, None, d(2), d(3), d(4)])

    def test_ema_short_series_all_none(self):
        self.assertEqual(rh.ema([d(1), d(2)], 3), [None, None])

    def test_atr_constant_range(self):
        out = rh.atr([d(11)] * 20, [d(9)] * 20, [d(10)] * 20, 14)
        self.assertIsNone(out[12])
        self.assertEqual(out[13], d(2))
        self.assertEqual(out[19], d(2))

    def test_adx_strong_uptrend_is_100(self):
        highs = [d(i + 1) for i in range(40)]
        lows = [d(i) for i in range(40)]
        closes = [d(i) + d("0.5") for i in range(40)]
        out = rh.adx(highs, lows, closes, 14)
        self.assertIsNone(out[26])
        self.assertEqual(out[27], d(100))
        self.assertEqual(out[39], d(100))


class AggregationTests(unittest.TestCase):
    def test_aggregate_4h_only_complete_buckets(self):
        bars = [
            (i * rh.H1_MS, d(10 + i), d(20 + i), d(5 - i), d(15 + i))
            for i in range(6)
        ]
        self.assertEqual(rh.aggregate_4h(bars), [(0, d(10), d(23), d(2), d(18))])

    def test_map_h4_index_no_look_ahead(self):
        open_times = [i * rh.H1_MS for i in range(8)]
        out = rh.map_h4_index(open_times, [0, rh.H4_MS])
        self.assertEqual(out, [-1, -1, -1, 0, 0, 0, 0, 1])


class SizingTests(unittest.TestCase):
    def test_size_position_risk_based(self):
        self.assertEqual(rh.size_position(d(1000), d(100), d(5)), d(2))

    def test_size_position_leverage_cap(self):
        expected = d(1000) * rh.MAX_LEVERAGE / rh.MAX_POSITIONS / d(100)
        self.assertEqual(rh.size_position(d(1000), d(100), d("0.1")), expected)

    def test_size_position_rejects_below_min_notional(self):
        self.assertIsNone(rh.size_position(d(1), d(100), d(5)))


class ExitTests(unittest.TestCase):
    def test_check_exit_stop_wins_when_both_hit(self):
        position = {"side": 1, "stop": d(95), "take": d(110)}
        self.assertEqual(rh.check_exit(position, d(100), d(111), d(94)), (d(95), "STOP"))

    def test_check_exit_gap_fills_at_open(self):
        position = {"side": 1, "stop": d(95), "take": d(110)}
        self.assertEqual(rh.check_exit(position, d(90), d(92), d(89)), (d(90), "STOP_GAP"))

    def test_check_exit_short_take(self):
        position = {"side": -1, "stop": d(105), "take": d(90)}
        self.assertEqual(rh.check_exit(position, d(100), d(101), d(89)), (d(90), "TAKE"))

    def test_update_trailing_only_tightens(self):
        position = {"side": 1, "stop": d(95), "atr": d(2), "extreme": d(100)}
        rh.update_trailing(position, d(110), d(108), d(2))
        self.assertEqual(position["stop"], d(106))
        rh.update_trailing(position, d(105), d(103), d(2))
        self.assertEqual(position["stop"], d(106))


class MetricsTests(unittest.TestCase):
    def test_compute_metrics(self):
        trades = [
            {"net_pnl": d(10), "fees": d(0)},
            {"net_pnl": d(-5), "fees": d(0)},
            {"net_pnl": d(20), "fees": d(0)},
            {"net_pnl": d(-5), "fees": d(0)},
        ]
        metrics = rh.compute_metrics(trades, d(1000))
        self.assertEqual(metrics["trades"], 4)
        self.assertEqual(metrics["win_rate"], d("0.5"))
        self.assertEqual(metrics["profit_factor"], d(3))
        self.assertEqual(metrics["payoff_ratio"], d(3))
        self.assertEqual(metrics["net_pnl_usdt"], d(20))
        self.assertEqual(metrics["max_drawdown_usdt"], d(5))

    def test_evaluate_verdict_accept_and_reject(self):
        good = {
            "trades": 40,
            "profit_factor": d("1.5"),
            "max_drawdown_pct": d(10),
            "win_rate": d("0.45"),
            "payoff_ratio": d("1.2"),
        }
        self.assertEqual(rh.evaluate_verdict(good), ("ACCEPTED", []))
        bad = {
            "trades": 10,
            "profit_factor": d("1.0"),
            "max_drawdown_pct": d(20),
            "win_rate": d("0.3"),
            "payoff_ratio": d("1.0"),
        }
        self.assertEqual(rh.evaluate_verdict(bad), ("REJECTED", [
            "TOO_FEW_TRADES",
            "PROFIT_FACTOR_TOO_LOW",
            "DRAWDOWN_TOO_HIGH",
            "NO_EDGE_PROFILE",
        ]))

    def test_classify_grid_result(self):
        def funnel(raw, passed):
            out = dict.fromkeys(rh.FUNNEL_KEYS, 0)
            out["raw_signals"] = raw
            out["signals_passed"] = passed
            return out

        def metrics(trades, pf):
            return {"trades": trades, "profit_factor": d(pf)}

        self.assertEqual(rh.classify_grid_result(funnel(10, 5), metrics(5, 2)), "TOO_FEW_RAW_SIGNALS")
        self.assertEqual(rh.classify_grid_result(funnel(100, 10), metrics(10, 2)), "FILTERS_TOO_STRICT")
        self.assertEqual(rh.classify_grid_result(funnel(100, 50), metrics(20, 2)), "CAPACITY_LIMITED")
        self.assertEqual(rh.classify_grid_result(funnel(100, 50), metrics(40, "0.8")), "NO_EDGE_IN_SAMPLE")
        self.assertEqual(rh.classify_grid_result(funnel(100, 50), metrics(40, "1.2")), "QUALIFIED")


class SimulationTests(unittest.TestCase):
    def test_simulate_long_take_profit(self):
        params = (2, 3, d(0), d(2))
        trades, funnel = rh.simulate(make_market(1), params, 0, 6 * rh.H1_MS, rh.EXIT_FIXED)
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["reason"], "TAKE")
        self.assertEqual(trades[0]["entry_ts"], 3 * rh.H1_MS)
        self.assertEqual(trades[0]["exit_ts"], 4 * rh.H1_MS)
        self.assertGreater(trades[0]["net_pnl"], 0)
        self.assertEqual(funnel["raw_signals"], 1)
        self.assertEqual(funnel["entries"], 1)
        self.assertEqual(funnel["pending_discarded"], 0)

    def test_simulate_trend_filter_blocks_signal(self):
        params = (2, 3, d(0), d(2))
        trades, funnel = rh.simulate(make_market(-1), params, 0, 6 * rh.H1_MS, rh.EXIT_FIXED)
        self.assertEqual(trades, [])
        self.assertEqual(funnel["raw_signals"], 1)
        self.assertEqual(funnel["blocked_trend"], 1)
        self.assertEqual(funnel["entries"], 0)


if __name__ == "__main__":
    unittest.main()
