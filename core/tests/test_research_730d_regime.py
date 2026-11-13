"""Test unitari offline per research_730d_regime.py (Passo A.9)."""
import unittest

import research_730d_regime as rr
import research_signals as rs


def _synth_1h(bars, dc_upper=105.0, dc_lower=95.0, basic_trend=1, strict_trend=1):
    n = len(bars)
    return rr.Features1HStrict(
        bars=bars,
        times=[b.open_time for b in bars],
        ema50=[100.0] * n,
        atr14=[2.0] * n,
        adx14=[28.0] * n,
        donchian_high={20: [dc_upper] * n, 36: [dc_upper] * n, 48: [dc_upper] * n},
        donchian_low={20: [dc_lower] * n, 36: [dc_lower] * n, 48: [dc_lower] * n},
        trend_4h_basic=[basic_trend] * n,
        trend_4h_strict=[strict_trend] * n,
    )


def _synth_4h(
    bars,
    ema20_val=102.0,
    ema50_val=100.0,
    ema200_val=95.0,
    atr_val=2.0,
    atr_sma_val=1.8,
    adx_val=26.0,
    dc_upper=105.0,
    dc_lower=95.0,
):
    n = len(bars)
    return rr.Features4H(
        bars=bars,
        times=[b.open_time for b in bars],
        ema20=[ema20_val] * n,
        ema50=[ema50_val] * n,
        ema200=[ema200_val] * n,
        atr14=[atr_val] * n,
        atr_sma20=[atr_sma_val] * n,
        adx14=[adx_val] * n,
        donchian_high={10: [dc_upper] * n, 20: [dc_upper] * n},
        donchian_low={10: [dc_lower] * n, 20: [dc_lower] * n},
    )


class HelpersAndGridTests(unittest.TestCase):
    def test_sma_optional_with_leading_nones(self):
        out = rr.sma_optional([None, None, 2.0, 4.0, 6.0], 2)
        self.assertEqual(out, [None, None, None, 3.0, 5.0])

    def test_build_grid_all_modes_have_8_combinations(self):
        for mode in rr.MODES:
            grid = rr.build_grid(mode)
            self.assertEqual(len(grid), 8)
            self.assertTrue(all(p.mode == mode for p in grid))
        with self.assertRaises(ValueError):
            rr.build_grid("unknown_mode")

    def test_params_to_dict_formats_donchian_and_keltner(self):
        d1 = rr.params_to_dict(rr.RegimeParams("h4_donchian_trail", 20.0, 25.0, 3.0))
        self.assertEqual(d1["dc_period"], 20)
        self.assertNotIn("kc_mult", d1)
        d2 = rr.params_to_dict(rr.RegimeParams("h4_keltner_trail", 1.5, 20.0, 2.0))
        self.assertEqual(d2["kc_mult"], 1.5)
        self.assertNotIn("dc_period", d2)


class Signal1HAnd4HTests(unittest.TestCase):
    def setUp(self):
        self.bars_1h = [
            rs.Bar(0, 100.0, 101.0, 99.0, 100.0, 1.0),
            rs.Bar(rs.HOUR_MS, 100.0, 107.0, 99.5, 106.0, 1.0),
        ]
        self.bars_4h = [
            rs.Bar(0, 100.0, 103.0, 99.0, 102.0, 1.0),
            rs.Bar(rs.FOUR_HOUR_MS, 102.0, 107.0, 101.5, 106.0, 1.0),
        ]

    def test_signal_1h_basic_vs_strict_regime_filter(self):
        feat_chop_4h = _synth_1h(self.bars_1h, basic_trend=1, strict_trend=0)
        p_basic = rr.RegimeParams("h1_is_gated", 20.0, 18.0, 2.0)
        p_strict = rr.RegimeParams("h1_regime_strict", 20.0, 22.0, 2.0)

        self.assertEqual(rr.signal_1h(feat_chop_4h, 1, p_basic), 1)
        self.assertEqual(rr.signal_1h(feat_chop_4h, 1, p_strict), 0)

        feat_aligned = _synth_1h(self.bars_1h, basic_trend=1, strict_trend=1)
        self.assertEqual(rr.signal_1h(feat_aligned, 1, p_strict), 1)

    def test_signal_4h_donchian_long_and_short(self):
        p_dc = rr.RegimeParams("h4_donchian_trail", 10.0, 20.0, 2.0)
        feat_long = _synth_4h(self.bars_4h, ema50_val=100.0, ema200_val=95.0, dc_upper=105.0)
        self.assertEqual(rr.signal_4h(feat_long, 1, p_dc), 1)

        bars_short = [
            rs.Bar(0, 100.0, 101.0, 99.0, 100.0, 1.0),
            rs.Bar(rs.FOUR_HOUR_MS, 96.0, 96.5, 93.5, 94.0, 1.0),
        ]
        feat_short = _synth_4h(
            bars_short, ema50_val=100.0, ema200_val=105.0, dc_upper=105.0, dc_lower=95.0
        )
        self.assertEqual(rr.signal_4h(feat_short, 1, p_dc), -1)

    def test_signal_4h_keltner_requires_volatility_expansion_and_band_cross(self):
        p_kc = rr.RegimeParams("h4_keltner_trail", 1.5, 20.0, 2.0)
        feat_ok = _synth_4h(self.bars_4h, atr_val=2.0, atr_sma_val=1.8)
        self.assertEqual(rr.signal_4h(feat_ok, 1, p_kc), 1)

        feat_low_vol = _synth_4h(self.bars_4h, atr_val=2.0, atr_sma_val=2.5)
        self.assertEqual(rr.signal_4h(feat_low_vol, 1, p_kc), 0)


class ExecutionAndQualificationTests(unittest.TestCase):
    def test_open_position_cfg_respects_notional_cap_non_linearity(self):
        cfg_half = rr.make_exec_config("h1_is_gated", risk_fraction=0.005)
        pos = rr.open_position_cfg("BTCUSDT", 1, 0, 100.0, 0.01, 2.0, cfg_half)
        self.assertAlmostEqual(pos.qty * pos.entry_price, rs.MAX_NOTIONAL_USDT)

    def test_classify_is_result_strict_gate_filters_weak_pf_and_high_dd(self):
        weak_pf = {"trades": 30, "profit_factor": 1.12, "net_pnl_usdt": 35.0, "max_drawdown_pct": 9.0}
        self.assertEqual(rr.classify_is_result(weak_pf, 20, strict_gate=False), "QUALIFIED")
        self.assertEqual(
            rr.classify_is_result(weak_pf, 20, strict_gate=True), "WEAK_IS_PROFIT_FACTOR"
        )

        high_dd = {"trades": 30, "profit_factor": 1.40, "net_pnl_usdt": 80.0, "max_drawdown_pct": 18.5}
        self.assertEqual(
            rr.classify_is_result(high_dd, 20, strict_gate=True), "IS_DRAWDOWN_TOO_HIGH"
        )

        strong = {"trades": 30, "profit_factor": 1.40, "net_pnl_usdt": 80.0, "max_drawdown_pct": 11.2}
        self.assertEqual(rr.classify_is_result(strong, 20, strict_gate=True), "QUALIFIED")

    def test_simulate_series_4h_opens_and_trails_stop(self):
        bars_4h = [
            rs.Bar(0, 100.0, 102.0, 99.0, 101.0, 1.0),
            rs.Bar(rs.FOUR_HOUR_MS, 101.0, 107.0, 100.5, 106.0, 1.0),
            rs.Bar(2 * rs.FOUR_HOUR_MS, 106.0, 115.0, 105.5, 114.0, 1.0),
            rs.Bar(3 * rs.FOUR_HOUR_MS, 114.0, 114.5, 109.0, 109.5, 1.0),
        ]
        feat_4h = _synth_4h(bars_4h, dc_upper=105.0, atr_val=2.0)
        cfg_4h = rr.make_exec_config("h4_donchian_trail")
        params = rr.RegimeParams("h4_donchian_trail", 10.0, 20.0, 2.0)
        trades = rr.simulate_series("BTCUSDT", feat_4h, params, 0, 4 * rs.FOUR_HOUR_MS, cfg_4h)
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0].reason, "TRAIL")
        self.assertGreater(trades[0].net_pnl, 0.0)

    def test_build_features_all_aggregates_1h_to_4h(self):
        bars_1h = [
            rs.Bar(i * rs.HOUR_MS, 100.0 + i * 0.1, 101.0 + i * 0.1, 99.0 + i * 0.1, 100.5 + i * 0.1, 1.0)
            for i in range(120)
        ]
        feat_1h, feat_4h = rr.build_features_all(bars_1h)
        self.assertEqual(len(feat_1h.bars), 120)
        self.assertEqual(len(feat_4h.bars), 30)

    def test_diagnose_730d_baseline_on_empty_fold_returns_no_candidate(self):
        bars_1h = [rs.Bar(i * rs.HOUR_MS, 100.0, 100.0, 100.0, 100.0, 1.0) for i in range(60)]
        feat_1h, feat_4h = rr.build_features_all(bars_1h)
        fmap = {"BTCUSDT": (feat_1h, feat_4h)}
        diag = rr.diagnose_730d_baseline(fmap, [(0, 30 * rs.HOUR_MS, 60 * rs.HOUR_MS)])
        self.assertEqual(diag["losing_folds"], [])
        self.assertEqual(diag["folds"][0]["status"], "NO_IS_CANDIDATE")

    def test_evaluate_mode_horizon_returns_rejected_on_flat_market(self):
        bars_1h = [rs.Bar(i * rs.HOUR_MS, 100.0, 100.0, 100.0, 100.0, 1.0) for i in range(60)]
        feat_1h, feat_4h = rr.build_features_all(bars_1h)
        fmap = {"BTCUSDT": (feat_1h, feat_4h)}
        report = rr.evaluate_mode_horizon(
            fmap, "h4_donchian_trail", 730, [(0, 30 * rs.HOUR_MS, 60 * rs.HOUR_MS)]
        )
        self.assertEqual(report["verdict"], "REJECTED")
        self.assertEqual(report["folds_evaluated"], 0)
        self.assertIn("TOO_FEW_TRADES", report["reasons"])


if __name__ == "__main__":
    unittest.main()
