import unittest

import research_mtf as rs


def make_trade(symbol, entry_time, exit_time, net_pnl):
    return rs.Trade(symbol, 1, entry_time, exit_time, 100.0, 101.0, 1.0, net_pnl, 0.1, "TP")


def rising_series(count):
    highs = [10.0 + i for i in range(count)]
    lows = [9.0 + i for i in range(count)]
    closes = [9.5 + i for i in range(count)]
    return highs, lows, closes


class IndicatorTests(unittest.TestCase):
    def test_ema_constant_series(self):
        out = rs.ema([5.0] * 30, 9)
        self.assertAlmostEqual(out[-1], 5.0)

    def test_ema_warmup_is_none(self):
        out = rs.ema([1.0, 2.0, 3.0, 4.0], 3)
        self.assertEqual(out[:2], [None, None])
        self.assertAlmostEqual(out[2], 2.0)

    def test_rsi_rising_is_100(self):
        _, _, closes = rising_series(40)
        self.assertAlmostEqual(rs.rsi(closes, 14)[-1], 100.0)

    def test_atr_constant_true_range(self):
        highs, lows, closes = rising_series(40)
        self.assertAlmostEqual(rs.atr(highs, lows, closes, 14)[-1], 1.5)

    def test_adx_pure_trend_is_100(self):
        highs, lows, closes = rising_series(40)
        out = rs.adx(highs, lows, closes, 14)
        self.assertIsNone(out[26])
        self.assertAlmostEqual(out[-1], 100.0)


class AggregationTests(unittest.TestCase):
    def _bars(self, count):
        return [
            rs.Bar(i * rs.BASE_MS, float(i), i + 1.0, i - 1.0, i + 0.5, 1.0)
            for i in range(count)
        ]

    def test_aggregate_complete_buckets(self):
        out = rs.aggregate(self._bars(8), rs.HOUR_MS)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0], rs.Bar(0, 0.0, 4.0, -1.0, 3.5, 4.0))
        self.assertEqual(out[1], rs.Bar(rs.HOUR_MS, 4.0, 8.0, 3.0, 7.5, 4.0))

    def test_aggregate_drops_incomplete_bucket(self):
        self.assertEqual(len(rs.aggregate(self._bars(6), rs.HOUR_MS)), 1)

    def test_last_closed_bucket_has_no_look_ahead(self):
        self.assertEqual(rs.last_closed_bucket(3 * rs.BASE_MS, rs.HOUR_MS), 0)
        self.assertEqual(rs.last_closed_bucket(2 * rs.BASE_MS, rs.HOUR_MS), -1)


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.params = rs.Params(20.0, 2.0, 2.0)
        self.long = rs.open_position("BTCUSDT", 1, 0, 100.0, 1.0, self.params)
        self.short = rs.open_position("BTCUSDT", -1, 0, 100.0, 1.0, self.params)

    def test_long_sizing_from_risk(self):
        entry = 100.0 * (1.0 + rs.SLIPPAGE_RATE)
        self.assertAlmostEqual(self.long.entry_price, entry)
        self.assertAlmostEqual(self.long.qty, rs.RISK_PER_TRADE_USDT / 2.0)
        self.assertAlmostEqual(self.long.stop_price, entry - 2.0)
        self.assertAlmostEqual(self.long.take_price, entry + 4.0)

    def test_notional_cap(self):
        position = rs.open_position("BTCUSDT", 1, 0, 100.0, 0.01, self.params)
        self.assertAlmostEqual(position.qty * position.entry_price, rs.MAX_NOTIONAL_USDT)

    def test_stop_wins_when_both_levels_hit(self):
        price, reason = rs.exit_fill(self.long, rs.Bar(0, 100.0, 110.0, 90.0, 100.0, 1.0))
        self.assertEqual(reason, "SL")
        self.assertAlmostEqual(price, self.long.stop_price * (1.0 - rs.SLIPPAGE_RATE))

    def test_gap_through_stop_fills_at_open(self):
        price, reason = rs.exit_fill(self.long, rs.Bar(0, 95.0, 96.0, 94.0, 95.0, 1.0))
        self.assertEqual(reason, "SL")
        self.assertAlmostEqual(price, 95.0 * (1.0 - rs.SLIPPAGE_RATE))

    def test_take_profit_fill(self):
        price, reason = rs.exit_fill(self.long, rs.Bar(0, 101.0, 105.0, 100.0, 104.0, 1.0))
        self.assertEqual(reason, "TP")
        self.assertAlmostEqual(price, self.long.take_price)

    def test_short_is_symmetric(self):
        entry = 100.0 * (1.0 - rs.SLIPPAGE_RATE)
        self.assertAlmostEqual(self.short.stop_price, entry + 2.0)
        self.assertAlmostEqual(self.short.take_price, entry - 4.0)
        price, reason = rs.exit_fill(self.short, rs.Bar(0, 100.0, 102.0, 99.0, 101.0, 1.0))
        self.assertEqual(reason, "SL")
        self.assertAlmostEqual(price, self.short.stop_price * (1.0 + rs.SLIPPAGE_RATE))

    def test_stop_loss_costs_about_one_risk_unit(self):
        price, reason = rs.exit_fill(self.long, rs.Bar(0, 100.0, 100.5, 97.0, 98.0, 1.0))
        trade = rs.close_position(self.long, price, rs.BASE_MS, reason)
        self.assertLess(trade.net_pnl, -rs.RISK_PER_TRADE_USDT)
        self.assertGreater(trade.net_pnl, -1.1 * rs.RISK_PER_TRADE_USDT)


class PortfolioTests(unittest.TestCase):
    def test_max_concurrent_skips_overlapping_trades(self):
        trades = [
            make_trade("AAAUSDT", 0, 10, 1.0),
            make_trade("BBBUSDT", 1, 10, 1.0),
            make_trade("CCCUSDT", 2, 10, 1.0),
            make_trade("DDDUSDT", 10, 20, 1.0),
        ]
        kept = rs.apply_max_concurrent(trades, 2)
        self.assertEqual([trade.symbol for trade in kept], ["AAAUSDT", "BBBUSDT", "DDDUSDT"])

    def test_metrics_profit_factor_and_drawdown(self):
        trades = [
            make_trade("AAAUSDT", 0, 1, 10.0),
            make_trade("AAAUSDT", 1, 2, -5.0),
            make_trade("AAAUSDT", 2, 3, -5.0),
            make_trade("AAAUSDT", 3, 4, 20.0),
        ]
        result = rs.metrics(trades, 1000.0)
        self.assertEqual(result["trades"], 4)
        self.assertAlmostEqual(result["profit_factor"], 3.0)
        self.assertAlmostEqual(result["payoff_ratio"], 3.0)
        self.assertAlmostEqual(result["win_rate"], 0.5)
        self.assertAlmostEqual(result["net_pnl_usdt"], 20.0)
        self.assertAlmostEqual(result["max_drawdown_usdt"], 10.0)

    def test_accept_rules(self):
        good = {
            "profit_factor": 1.5,
            "trades": 40,
            "max_drawdown_pct": 10.0,
            "win_rate": 0.45,
            "payoff_ratio": 1.2,
        }
        self.assertTrue(rs.accept(good))
        self.assertFalse(rs.accept(dict(good, profit_factor=1.2)))
        self.assertFalse(rs.accept(dict(good, trades=10)))
        self.assertFalse(rs.accept(dict(good, max_drawdown_pct=20.0)))
        self.assertFalse(rs.accept(dict(good, win_rate=0.3)))
        self.assertTrue(rs.accept(dict(good, win_rate=0.3, payoff_ratio=2.5)))

    def test_build_folds_365_days(self):
        folds = rs.build_folds(0, 365 * rs.DAY_MS, 180, 60)
        self.assertEqual(len(folds), 3)
        self.assertEqual(folds[0], (0, 180 * rs.DAY_MS, 240 * rs.DAY_MS))
        self.assertEqual(folds[1][1], folds[0][2])


class SimulationTests(unittest.TestCase):
    def test_flat_market_produces_no_trades(self):
        bars = [rs.Bar(i * rs.BASE_MS, 100.0, 100.0, 100.0, 100.0, 1.0) for i in range(3000)]
        features = rs.build_features(bars)
        trades = rs.simulate("BTCUSDT", features, rs.Params(18.0, 1.5, 1.5), 0, 3000 * rs.BASE_MS)
        self.assertEqual(trades, [])


if __name__ == "__main__":
    unittest.main()
