"""Test unitari offline per research_signals.py (Donchian Breakout + RSI Mean-Reversion)."""
import unittest

import research_signals as rs


def _synthetic_features(
    bars,
    ema50_val=100.0,
    ema200_val=90.0,
    rsi_vals=None,
    atr_val=2.0,
    adx_val=25.0,
    dc_upper=105.0,
    dc_lower=95.0,
    trend_4h_val=1,
):
    n = len(bars)
    return rs.Features(
        bars=bars,
        times=[b.open_time for b in bars],
        ema50=[ema50_val] * n,
        ema200=[ema200_val] * n,
        rsi14=rsi_vals if rsi_vals is not None else [50.0] * n,
        atr14=[atr_val] * n,
        adx14=[adx_val] * n,
        donchian_high={20: [dc_upper] * n, 48: [dc_upper] * n},
        donchian_low={20: [dc_lower] * n, 48: [dc_lower] * n},
        trend_4h=[trend_4h_val] * n,
    )


class DonchianAndGridTests(unittest.TestCase):
    def test_donchian_bounds_excludes_current_bar(self):
        highs = [10.0, 12.0, 11.0, 15.0]
        lows = [8.0, 9.0, 7.0, 10.0]
        upper, lower = rs.donchian_bounds(highs, lows, 2)
        self.assertEqual(upper, [None, None, 12.0, 12.0])
        self.assertEqual(lower, [None, None, 8.0, 7.0])

    def test_donchian_short_input_returns_none(self):
        upper, lower = rs.donchian_bounds([1.0, 2.0], [0.5, 1.5], 3)
        self.assertEqual(upper, [None, None])
        self.assertEqual(lower, [None, None])

    def test_build_grid_counts_and_modes(self):
        for mode in ("donchian_fixed", "donchian_trailing", "rsi_mr"):
            grid = rs.build_grid(mode)
            self.assertEqual(len(grid), 8)
            self.assertTrue(all(p.mode == mode for p in grid))
        with self.assertRaises(ValueError):
            rs.build_grid("invalid_mode")

    def test_params_to_dict_formats_fields(self):
        dc_dict = rs.params_to_dict(rs.SignalParams("donchian_fixed", 20.0, 18.0, 2.0))
        self.assertEqual(
            dc_dict,
            {
                "mode": "donchian_fixed",
                "dc_period": 20,
                "adx_min": 18.0,
                "sl_mult": 1.5,
                "exit_mult": 2.0,
            },
        )
        rsi_dict = rs.params_to_dict(rs.SignalParams("rsi_mr", 35.0, 1.5, 2.0))
        self.assertEqual(
            rsi_dict,
            {
                "mode": "rsi_mr",
                "rsi_thresh": 35.0,
                "sl_mult": 1.5,
                "exit_mult": 2.0,
            },
        )

    def test_aggregate_4h_complete_and_incomplete(self):
        bars = [
            rs.Bar(i * rs.HOUR_MS, float(i), i + 1.0, i - 1.0, i + 0.5, 1.0)
            for i in range(8)
        ]
        out = rs.aggregate_4h(bars)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0], rs.Bar(0, 0.0, 4.0, -1.0, 3.5, 4.0))
        self.assertEqual(len(rs.aggregate_4h(bars[:6])), 1)

    def test_last_closed_4h_bucket_no_lookahead(self):
        self.assertEqual(rs.last_closed_4h_bucket(2 * rs.HOUR_MS), -1)
        self.assertEqual(rs.last_closed_4h_bucket(3 * rs.HOUR_MS), 0)


class StageAndFunnelTests(unittest.TestCase):
    def setUp(self):
        self.dc_params = rs.SignalParams("donchian_fixed", 20.0, 18.0, 2.0)
        self.rsi_params = rs.SignalParams("rsi_mr", 35.0, 1.5, 2.0)
        self.bars = [
            rs.Bar(0, 100.0, 101.0, 99.0, 100.0, 1.0),
            rs.Bar(rs.HOUR_MS, 100.0, 107.0, 99.5, 106.0, 1.0),
        ]

    def test_donchian_stage_warmup_and_no_trend(self):
        feat_warmup = _synthetic_features(self.bars, atr_val=None)
        self.assertEqual(rs.evaluate_bar_stage(feat_warmup, 1, self.dc_params), ("WARMUP", 0))
        feat_no_trend = _synthetic_features(self.bars, trend_4h_val=0)
        self.assertEqual(rs.evaluate_bar_stage(feat_no_trend, 1, self.dc_params), ("NO_TREND_4H", 0))

    def test_donchian_stage_regime_filtered_and_no_trigger(self):
        feat_low_adx = _synthetic_features(self.bars, adx_val=14.0)
        self.assertEqual(
            rs.evaluate_bar_stage(feat_low_adx, 1, self.dc_params), ("REGIME_FILTERED", 0)
        )
        bars_inside = [
            rs.Bar(0, 100.0, 101.0, 99.0, 100.0, 1.0),
            rs.Bar(rs.HOUR_MS, 100.0, 104.0, 99.5, 103.0, 1.0),
        ]
        feat_inside = _synthetic_features(bars_inside)
        self.assertEqual(rs.evaluate_bar_stage(feat_inside, 1, self.dc_params), ("NO_TRIGGER", 0))

    def test_donchian_stage_stretch_filtered_and_signal_long(self):
        feat_ok = _synthetic_features(self.bars, dc_upper=105.0, atr_val=2.0)
        self.assertEqual(rs.evaluate_bar_stage(feat_ok, 1, self.dc_params), ("SIGNAL", 1))

        bars_stretched = [
            rs.Bar(0, 100.0, 101.0, 99.0, 100.0, 1.0),
            rs.Bar(rs.HOUR_MS, 100.0, 110.0, 99.5, 109.0, 1.0),
        ]
        feat_stretched = _synthetic_features(bars_stretched, dc_upper=105.0, atr_val=2.0)
        self.assertEqual(
            rs.evaluate_bar_stage(feat_stretched, 1, self.dc_params), ("STRETCH_FILTERED", 0)
        )

    def test_donchian_stage_signal_short(self):
        bars_short = [
            rs.Bar(0, 100.0, 101.0, 99.0, 100.0, 1.0),
            rs.Bar(rs.HOUR_MS, 96.0, 96.5, 93.5, 94.0, 1.0),
        ]
        feat_short = _synthetic_features(
            bars_short, ema50_val=100.0, dc_upper=105.0, dc_lower=95.0, atr_val=2.0, trend_4h_val=-1
        )
        self.assertEqual(rs.evaluate_bar_stage(feat_short, 1, self.dc_params), ("SIGNAL", -1))

    def test_rsi_mr_stage_long_and_short(self):
        bars_long = [
            rs.Bar(0, 100.0, 101.0, 99.0, 100.0, 1.0),
            rs.Bar(rs.HOUR_MS, 100.0, 102.0, 99.5, 101.0, 1.0),
        ]
        feat_long = _synthetic_features(
            bars_long, ema200_val=90.0, rsi_vals=[32.0, 38.0], atr_val=2.0, adx_val=25.0, trend_4h_val=1
        )
        self.assertEqual(rs.evaluate_bar_stage(feat_long, 1, self.rsi_params), ("SIGNAL", 1))

        bars_short = [
            rs.Bar(0, 100.0, 101.0, 99.0, 100.0, 1.0),
            rs.Bar(rs.HOUR_MS, 100.0, 100.5, 98.0, 99.0, 1.0),
        ]
        feat_short = _synthetic_features(
            bars_short, ema200_val=110.0, rsi_vals=[68.0, 62.0], atr_val=2.0, adx_val=25.0, trend_4h_val=-1
        )
        self.assertEqual(rs.evaluate_bar_stage(feat_short, 1, self.rsi_params), ("SIGNAL", -1))

    def test_rsi_mr_stage_regime_filtered_on_high_adx_or_ema200(self):
        feat_high_adx = _synthetic_features(
            self.bars, ema200_val=90.0, rsi_vals=[30.0, 34.0], adx_val=50.0, trend_4h_val=1
        )
        self.assertEqual(
            rs.evaluate_bar_stage(feat_high_adx, 1, self.rsi_params), ("REGIME_FILTERED", 0)
        )
        feat_below_ema200 = _synthetic_features(
            self.bars, ema200_val=110.0, rsi_vals=[30.0, 34.0], adx_val=25.0, trend_4h_val=1
        )
        self.assertEqual(
            rs.evaluate_bar_stage(feat_below_ema200, 1, self.rsi_params), ("REGIME_FILTERED", 0)
        )


class ExecutionAndWalkForwardTests(unittest.TestCase):
    def test_open_position_donchian_fixed_vs_trailing(self):
        p_fixed = rs.SignalParams("donchian_fixed", 20.0, 18.0, 2.0)
        pos_fixed = rs.open_position("BTCUSDT", 1, 0, 100.0, 2.0, p_fixed)
        entry = 100.0 * (1.0 + rs.SLIPPAGE_RATE)
        self.assertAlmostEqual(pos_fixed.stop_price, entry - 3.0)
        self.assertAlmostEqual(pos_fixed.take_price, entry + 6.0)
        self.assertEqual(pos_fixed.trail_distance, 0.0)

        p_trail = rs.SignalParams("donchian_trailing", 20.0, 18.0, 2.0)
        pos_trail = rs.open_position("BTCUSDT", 1, 0, 100.0, 2.0, p_trail)
        self.assertAlmostEqual(pos_trail.stop_price, entry - 3.0)
        self.assertEqual(pos_trail.take_price, 0.0)
        self.assertAlmostEqual(pos_trail.trail_distance, 4.0)

    def test_advance_trailing_long_and_short(self):
        p_trail = rs.SignalParams("donchian_trailing", 20.0, 18.0, 2.0)
        pos_long = rs.open_position("BTCUSDT", 1, 0, 100.0, 2.0, p_trail)
        pos_long = rs.advance_trailing(pos_long, rs.Bar(0, 100.0, 110.0, 99.5, 109.0, 1.0))
        self.assertAlmostEqual(pos_long.best_price, 110.0)
        self.assertAlmostEqual(pos_long.stop_price, 106.0)
        fill = rs.exit_fill(pos_long, rs.Bar(rs.HOUR_MS, 108.0, 108.5, 105.5, 106.0, 1.0))
        self.assertIsNotNone(fill)
        self.assertEqual(fill[1], "TRAIL")

        pos_short = rs.open_position("BTCUSDT", -1, 0, 100.0, 2.0, p_trail)
        pos_short = rs.advance_trailing(pos_short, rs.Bar(0, 100.0, 100.5, 90.0, 91.0, 1.0))
        self.assertAlmostEqual(pos_short.best_price, 90.0)
        self.assertAlmostEqual(pos_short.stop_price, 94.0)
        fill_s = rs.exit_fill(pos_short, rs.Bar(rs.HOUR_MS, 92.0, 94.5, 91.5, 94.0, 1.0))
        self.assertIsNotNone(fill_s)
        self.assertEqual(fill_s[1], "TRAIL")

    def test_simulate_rsi_exit_at_next_open(self):
        bars = [
            rs.Bar(0, 100.0, 101.0, 99.0, 100.0, 1.0),
            rs.Bar(rs.HOUR_MS, 100.0, 101.5, 99.5, 101.0, 1.0),
            rs.Bar(2 * rs.HOUR_MS, 101.0, 103.0, 100.5, 102.5, 1.0),
            rs.Bar(3 * rs.HOUR_MS, 102.5, 103.0, 101.5, 102.0, 1.0),
        ]
        feat = _synthetic_features(
            bars,
            ema200_val=90.0,
            rsi_vals=[32.0, 34.0, 70.0, 55.0],
            atr_val=5.0,
            adx_val=25.0,
            trend_4h_val=1,
        )
        params = rs.SignalParams("rsi_mr", 35.0, 2.0, 3.0)
        trades = rs.simulate("BTCUSDT", feat, params, 0, 4 * rs.HOUR_MS)
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0].reason, "RSI_EXIT")
        self.assertAlmostEqual(trades[0].exit_price, 102.5 * (1.0 - rs.SLIPPAGE_RATE))

    def test_classify_in_sample_outcomes(self):
        self.assertEqual(
            rs.classify_in_sample({"SIGNAL": 0}, {"trades": 0, "profit_factor": 0.0, "net_pnl_usdt": 0.0}),
            "ZERO_SIGNALS",
        )
        self.assertEqual(
            rs.classify_in_sample({"SIGNAL": 10}, {"trades": 12, "profit_factor": 1.4, "net_pnl_usdt": 20.0}),
            "TOO_FEW_TRADES_IN_SAMPLE",
        )
        self.assertEqual(
            rs.classify_in_sample({"SIGNAL": 30}, {"trades": 25, "profit_factor": 0.9, "net_pnl_usdt": -10.0}),
            "NO_EDGE_IN_SAMPLE",
        )
        self.assertEqual(
            rs.classify_in_sample({"SIGNAL": 30}, {"trades": 25, "profit_factor": 1.35, "net_pnl_usdt": 45.0}),
            "QUALIFIED",
        )

    def test_evaluate_acceptance_reasons(self):
        good = {
            "trades": 35,
            "profit_factor": 1.45,
            "max_drawdown_pct": 8.5,
            "win_rate": 0.42,
            "payoff_ratio": 1.5,
        }
        self.assertEqual(rs.evaluate_acceptance(good), (True, []))
        bad = {
            "trades": 12,
            "profit_factor": 0.95,
            "max_drawdown_pct": 18.0,
            "win_rate": 0.30,
            "payoff_ratio": 1.2,
        }
        self.assertEqual(
            rs.evaluate_acceptance(bad),
            (
                False,
                [
                    "TOO_FEW_TRADES",
                    "PROFIT_FACTOR_TOO_LOW",
                    "DRAWDOWN_TOO_HIGH",
                    "NO_EDGE_PROFILE",
                ],
            ),
        )

    def test_parse_modes_valid_and_invalid(self):
        self.assertEqual(
            rs.parse_modes(["research_signals.py"]),
            ["donchian_fixed", "donchian_trailing", "rsi_mr"],
        )
        self.assertEqual(
            rs.parse_modes(["research_signals.py", "donchian_fixed,rsi_mr"]),
            ["donchian_fixed", "rsi_mr"],
        )
        with self.assertRaises(ValueError):
            rs.parse_modes(["research_signals.py", "unknown_mode"])


if __name__ == "__main__":
    unittest.main()
