"""Test unitari offline per research_donchian_validation.py (Passo A.8)."""
import unittest

import research_donchian_validation as rdv
import research_signals as rs


def _synthetic_features(bars, atr_val=2.0, dc_upper=105.0, dc_lower=95.0, trend_4h_val=1):
    n = len(bars)
    return rs.Features(
        bars=bars,
        times=[b.open_time for b in bars],
        ema50=[100.0] * n,
        ema200=[90.0] * n,
        rsi14=[55.0] * n,
        atr14=[atr_val] * n,
        adx14=[28.0] * n,
        donchian_high={20: [dc_upper] * n, 48: [dc_upper] * n},
        donchian_low={20: [dc_lower] * n, 48: [dc_lower] * n},
        trend_4h=[trend_4h_val] * n,
    )


class ExecConfigAndParityTests(unittest.TestCase):
    def setUp(self):
        self.bars_long = [
            rs.Bar(0, 100.0, 101.0, 99.0, 100.0, 1.0),
            rs.Bar(rs.HOUR_MS, 100.0, 106.5, 99.5, 106.0, 1.0),
            rs.Bar(2 * rs.HOUR_MS, 106.0, 114.0, 105.5, 113.0, 1.0),
            rs.Bar(3 * rs.HOUR_MS, 113.0, 113.5, 108.0, 108.5, 1.0),
        ]
        self.bars_short = [
            rs.Bar(0, 100.0, 101.0, 99.0, 100.0, 1.0),
            rs.Bar(rs.HOUR_MS, 100.0, 100.5, 93.5, 94.0, 1.0),
            rs.Bar(2 * rs.HOUR_MS, 94.0, 94.5, 86.0, 87.0, 1.0),
            rs.Bar(3 * rs.HOUR_MS, 87.0, 92.0, 86.5, 91.5, 1.0),
        ]
        self.params = rs.SignalParams("donchian_trailing", 20.0, 18.0, 2.0)

    def test_simulate_cfg_matches_research_signals_identically(self):
        feat_long = _synthetic_features(self.bars_long, trend_4h_val=1)
        feat_short = _synthetic_features(self.bars_short, trend_4h_val=-1)
        cfg = rdv.ExecConfig(rs.RISK_FRACTION, rs.TAKER_FEE_RATE, rs.SLIPPAGE_RATE)

        for sym, feat in (("BTCUSDT", feat_long), ("ETHUSDT", feat_short)):
            base_trades = rs.simulate(sym, feat, self.params, 0, 4 * rs.HOUR_MS)
            cfg_trades = rdv.simulate_cfg(sym, feat, self.params, 0, 4 * rs.HOUR_MS, cfg)
            self.assertEqual(len(cfg_trades), 1)
            self.assertEqual(cfg_trades, base_trades)

    def test_notional_cap_respected_when_risk_fraction_changes(self):
        # Con atr=0.05 (dc_upper=105.96 per restare sotto MAX_BREAKOUT_ATR) e SL=0.075,
        # entrambi i profili (1% e 0.5%) devono rispettare il tetto MAX_NOTIONAL_USDT=2000.
        feat = _synthetic_features(self.bars_long, atr_val=0.05, dc_upper=105.96, trend_4h_val=1)
        cfg_100 = rdv.ExecConfig(0.0100, rs.TAKER_FEE_RATE, rs.SLIPPAGE_RATE)
        cfg_050 = rdv.ExecConfig(0.0050, rs.TAKER_FEE_RATE, rs.SLIPPAGE_RATE)

        t100 = rdv.simulate_cfg("BTCUSDT", feat, self.params, 0, 4 * rs.HOUR_MS, cfg_100)[0]
        t050 = rdv.simulate_cfg("BTCUSDT", feat, self.params, 0, 4 * rs.HOUR_MS, cfg_050)[0]
        self.assertAlmostEqual(t100.qty, 2000.0 / t100.entry_price)
        self.assertAlmostEqual(t050.qty, t100.qty)

    def test_uncapped_half_risk_scales_qty_pnl_and_lowers_drawdown(self):
        feat = _synthetic_features(self.bars_long, atr_val=2.0, trend_4h_val=1)
        cfg_100 = rdv.ExecConfig(0.0100, rs.TAKER_FEE_RATE, rs.SLIPPAGE_RATE)
        cfg_050 = rdv.ExecConfig(0.0050, rs.TAKER_FEE_RATE, rs.SLIPPAGE_RATE)

        t100 = rdv.simulate_cfg("BTCUSDT", feat, self.params, 0, 4 * rs.HOUR_MS, cfg_100)
        t050 = rdv.simulate_cfg("BTCUSDT", feat, self.params, 0, 4 * rs.HOUR_MS, cfg_050)
        self.assertAlmostEqual(t050[0].qty, t100[0].qty * 0.5)
        self.assertAlmostEqual(t050[0].net_pnl, t100[0].net_pnl * 0.5)
        self.assertAlmostEqual(t050[0].fees, t100[0].fees * 0.5)

    def test_stress_costs_reduce_net_pnl_long_and_short(self):
        base_cfg = rdv.ExecConfig(0.0075, rs.TAKER_FEE_RATE, rs.SLIPPAGE_RATE)
        stress_cfg = rdv.ExecConfig(0.0075, rs.TAKER_FEE_RATE * 1.5, rs.SLIPPAGE_RATE * 1.5)

        feat_long = _synthetic_features(self.bars_long, trend_4h_val=1)
        t_long_base = rdv.simulate_cfg("BTCUSDT", feat_long, self.params, 0, 4 * rs.HOUR_MS, base_cfg)[0]
        t_long_stress = rdv.simulate_cfg("BTCUSDT", feat_long, self.params, 0, 4 * rs.HOUR_MS, stress_cfg)[0]
        self.assertGreater(t_long_stress.entry_price, t_long_base.entry_price)
        self.assertLess(t_long_stress.exit_price, t_long_base.exit_price)
        self.assertGreater(t_long_stress.fees, t_long_base.fees)
        self.assertLess(t_long_stress.net_pnl, t_long_base.net_pnl)

        feat_short = _synthetic_features(self.bars_short, trend_4h_val=-1)
        t_short_base = rdv.simulate_cfg("BTCUSDT", feat_short, self.params, 0, 4 * rs.HOUR_MS, base_cfg)[0]
        t_short_stress = rdv.simulate_cfg("BTCUSDT", feat_short, self.params, 0, 4 * rs.HOUR_MS, stress_cfg)[0]
        self.assertLess(t_short_stress.entry_price, t_short_base.entry_price)
        self.assertGreater(t_short_stress.exit_price, t_short_base.exit_price)
        self.assertGreater(t_short_stress.fees, t_short_base.fees)
        self.assertLess(t_short_stress.net_pnl, t_short_base.net_pnl)

    def test_tp_exit_keeps_exact_limit_price_under_slippage_stress(self):
        pos = rs.Position("BTCUSDT", 1, 0, 100.0, 5.0, 95.0, 106.0, 0.0, 100.0)
        bar = rs.Bar(rs.HOUR_MS, 100.5, 107.0, 99.0, 106.5, 1.0)
        fill = rdv.exit_fill_cfg(pos, bar, rs.SLIPPAGE_RATE * 1.5)
        self.assertEqual(fill, (106.0, "TP"))


class ProfileAndEvaluationTests(unittest.TestCase):
    def setUp(self):
        bars = [
            rs.Bar(0, 100.0, 101.0, 99.0, 100.0, 1.0),
            rs.Bar(rs.HOUR_MS, 100.0, 106.5, 99.5, 106.0, 1.0),
            rs.Bar(2 * rs.HOUR_MS, 106.0, 114.0, 105.5, 113.0, 1.0),
            rs.Bar(3 * rs.HOUR_MS, 113.0, 113.5, 108.0, 108.5, 1.0),
        ]
        self.features_map = {
            "AAAUSDT": _synthetic_features(bars),
            "BBBUSDT": _synthetic_features(bars),
            "CCCUSDT": _synthetic_features(bars),
        }
        self.params = rs.SignalParams("donchian_trailing", 20.0, 18.0, 2.0)

    def test_risk_profiles_ordering_and_validity(self):
        self.assertEqual(len(rdv.RISK_PROFILES), 5)
        for profile in rdv.RISK_PROFILES:
            self.assertGreater(profile.risk_fraction, 0.0)
            self.assertLessEqual(profile.risk_fraction, 0.01)
            self.assertIn(profile.max_concurrent, (2, 3))

    def test_run_window_profile_enforces_max_concurrent_and_reuses_cache(self):
        cache = {}
        prof_3 = rdv.RiskProfile("p3", 0.0075, 3)
        prof_2 = rdv.RiskProfile("p2", 0.0075, 2)

        trades_3 = rdv.run_window_profile(
            self.features_map, self.params, 0, 4 * rs.HOUR_MS, prof_3, cache=cache
        )
        self.assertEqual(len(trades_3), 3)
        self.assertEqual(len(cache), 1)

        trades_2 = rdv.run_window_profile(
            self.features_map, self.params, 0, 4 * rs.HOUR_MS, prof_2, cache=cache
        )
        self.assertEqual(len(trades_2), 2)
        self.assertEqual(len(cache), 1)

    def test_cached_funnel_reuses_computation(self):
        cache = {}
        f1 = rdv._cached_funnel(self.features_map, self.params, 0, 4 * rs.HOUR_MS, cache)
        f2 = rdv._cached_funnel(self.features_map, self.params, 0, 4 * rs.HOUR_MS, cache)
        self.assertIs(f1, f2)
        self.assertEqual(f1["bars"], 12)

    def test_select_params_profile_returns_none_when_too_few_trades(self):
        cache = {}
        prof = rdv.RiskProfile("p3", 0.01, 3)
        params, metrics_res, outcomes = rdv.select_params_profile(
            self.features_map, 0, 4 * rs.HOUR_MS, prof, cache=cache
        )
        self.assertIsNone(params)
        self.assertIsNone(metrics_res)
        self.assertEqual(sum(outcomes.values()), 8)

    def test_evaluate_horizon_profile_structure_on_no_candidate_folds(self):
        prof = rdv.RiskProfile("moderate_075bps_3pos", 0.0075, 3)
        folds = [(0, 2 * rs.HOUR_MS, 4 * rs.HOUR_MS)]
        report = rdv.evaluate_horizon_profile(self.features_map, 365, folds, prof)
        self.assertEqual(report["horizon_days"], 365)
        self.assertEqual(report["profile"], "moderate_075bps_3pos")
        self.assertEqual(report["folds_total"], 1)
        self.assertEqual(report["folds_evaluated"], 0)
        self.assertEqual(report["folds"], [{"fold": 1, "status": "NO_IS_CANDIDATE"}])
        self.assertEqual(report["verdict"], "REJECTED")
        self.assertIn("TOO_FEW_TRADES", report["reasons"])

    def test_select_stress_profiles_prefers_accepted_or_best_365d_fallback(self):
        reports = {
            ("baseline_100bps_3pos", 365): {"out_of_sample": {"trades": 100, "profit_factor": 1.2, "net_pnl_usdt": 100.0}},
            ("conservative_050bps_2pos", 365): {"out_of_sample": {"trades": 80, "profit_factor": 1.6, "net_pnl_usdt": 150.0}},
        }
        chosen_accepted = rdv._select_stress_profiles({"moderate_075bps_2pos"}, reports)
        self.assertEqual([p.name for p in chosen_accepted], ["moderate_075bps_2pos"])

        chosen_fallback = rdv._select_stress_profiles(set(), reports)
        self.assertEqual([p.name for p in chosen_fallback], ["conservative_050bps_2pos"])

    def test_folds_365_and_730_days_counts(self):
        self.assertEqual(len(rs.build_folds(0, 365 * rs.DAY_MS, 180, 60)), 3)
        self.assertEqual(len(rs.build_folds(0, 730 * rs.DAY_MS, 180, 60)), 9)


if __name__ == "__main__":
    unittest.main()
