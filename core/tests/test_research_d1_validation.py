"""Test unitari offline per research_d1_validation.py (Passo A.11)."""
import unittest

import research_d1_validation as rd
import research_paradigms as rp
import research_signals as rs


def _make_bars_1h(count, base=100.0, step=0.05):
    return [
        rs.Bar(
            i * rs.HOUR_MS,
            base + i * step,
            base + i * step + 1.0,
            base + i * step - 0.5,
            base + i * step + 0.5,
            1.0,
        )
        for i in range(count)
    ]


class BimestersAndCausalBucketTests(unittest.TestCase):
    def test_last_closed_1d_bucket_has_no_lookahead(self):
        self.assertEqual(rd.last_closed_1d_bucket(22 * rs.HOUR_MS), -1)
        self.assertEqual(rd.last_closed_1d_bucket(23 * rs.HOUR_MS), 0)
        self.assertEqual(rd.last_closed_1d_bucket(46 * rs.HOUR_MS), 0)
        self.assertEqual(rd.last_closed_1d_bucket(47 * rs.HOUR_MS), 1)

    def test_build_bimesters_6_and_12_contiguous_windows(self):
        end_ms = 1000 * rs.DAY_MS
        b6 = rd.build_bimesters(end_ms, 6)
        b12 = rd.build_bimesters(end_ms, 12)
        self.assertEqual(len(b6), 6)
        self.assertEqual(len(b12), 12)
        self.assertEqual(b6[-1][1], end_ms)
        self.assertEqual(b6[0][0], end_ms - 360 * rs.DAY_MS)
        for a, b in zip(b12, b12[1:]):
            self.assertEqual(a[1], b[0])

    def test_build_extended_bundle_populates_multi_period_donchian_and_1d_trend(self):
        bars_1h = _make_bars_1h(24 * 30, base=100.0, step=0.1)
        bundle = rd.build_extended_bundle(bars_1h, [])
        self.assertEqual(len(bundle.feat_1d.bars), 30)
        for period in rd.DC_PERIODS_1D:
            self.assertIn(period, bundle.feat_1d.dc_high)
            self.assertIn(period, bundle.feat_1d.dc_low)
        self.assertEqual(len(bundle.trend_1d_on_1h), len(bars_1h))


class D1ExecutionAndSignalTests(unittest.TestCase):
    def setUp(self):
        self.cfg_050 = rd.CANONICAL_PROFILES[0]
        self.cfg_100 = rd.CANONICAL_PROFILES[1]

    def test_signal_d1_cfg_long_and_short(self):
        bars_1d = [rs.Bar(i * rs.DAY_MS, 100.0, 102.0, 99.0, 101.0, 1.0) for i in range(25)]
        bars_1d[-1] = rs.Bar(24 * rs.DAY_MS, 101.0, 105.0, 100.5, 104.5, 1.0)
        feat_long = rd.ExtendedD1Features(
            bars=bars_1d,
            times=[b.open_time for b in bars_1d],
            ema20=[102.0] * 25,
            ema50=[100.0] * 25,
            atr14=[2.0] * 25,
            adx14=[22.0] * 25,
            dc_high={12: [103.0] * 25, 15: [103.0] * 25, 20: [103.0] * 25},
            dc_low={12: [97.0] * 25, 15: [97.0] * 25, 20: [97.0] * 25},
        )
        self.assertEqual(rd.signal_d1_cfg(feat_long, 24, self.cfg_050), 1)

        bars_short = list(bars_1d)
        bars_short[-1] = rs.Bar(24 * rs.DAY_MS, 99.0, 99.5, 95.0, 95.5, 1.0)
        feat_short = feat_long._replace(
            bars=bars_short,
            ema20=[98.0] * 25,
            ema50=[100.0] * 25,
        )
        self.assertEqual(rd.signal_d1_cfg(feat_short, 24, self.cfg_050), -1)

    def test_open_pos_d1_scales_with_risk_and_respects_notional_cap(self):
        p50 = rd._open_pos_d1("BTCUSDT", 1, 0, 100.0, 2.0, self.cfg_050)
        p100 = rd._open_pos_d1("BTCUSDT", 1, 0, 100.0, 2.0, self.cfg_100)
        self.assertAlmostEqual(p100.qty, p50.qty * 2.0)

        p_capped = rd._open_pos_d1("BTCUSDT", 1, 0, 100.0, 0.01, self.cfg_100)
        self.assertAlmostEqual(p_capped.qty * p_capped.entry_price, rs.MAX_NOTIONAL_USDT)

    def test_cost_stress_reduces_net_pnl_on_d1_trade(self):
        pos = rd._open_pos_d1("BTCUSDT", 1, 0, 100.0, 2.0, self.cfg_050)
        t_base = rd._close_pos_d1(pos, 106.0, rs.DAY_MS, "TRAIL", 0.0, rs.TAKER_FEE_RATE)
        t_2x = rd._close_pos_d1(pos, 106.0, rs.DAY_MS, "TRAIL", 0.0, rs.TAKER_FEE_RATE * 2.0)
        self.assertAlmostEqual(t_2x.fees, t_base.fees * 2.0)
        self.assertLess(t_2x.net_pnl, t_base.net_pnl)

    def test_simulate_h1_d1_macro_gated_blocks_when_1d_trend_disagrees(self):
        bars = [
            rs.Bar(0, 100.0, 101.0, 99.0, 100.0, 1.0),
            rs.Bar(rs.HOUR_MS, 100.0, 107.0, 99.5, 106.0, 1.0),
            rs.Bar(2 * rs.HOUR_MS, 106.0, 108.0, 105.0, 107.0, 1.0),
        ]
        feat_1h = rs.Features(
            bars=bars,
            times=[b.open_time for b in bars],
            ema50=[100.0] * 3,
            ema200=[90.0] * 3,
            rsi14=[55.0] * 3,
            atr14=[2.0] * 3,
            adx14=[28.0] * 3,
            donchian_high={20: [105.0] * 3, 48: [105.0] * 3},
            donchian_low={20: [95.0] * 3, 48: [95.0] * 3},
            trend_4h=[1] * 3,
        )
        bundle_aligned = rd.ExtendedBundle(
            feat_1h=feat_1h,
            funding_1h=[0.0, 0.0, 0.0],
            funding_sum3_1h=[0.0, 0.0, 0.0],
            trend_1d_on_1h=[1, 1, 1],
            feat_1d=None,
            funding_map={},
        )
        bundle_flat_1d = bundle_aligned._replace(trend_1d_on_1h=[0, 0, 0])

        self.assertEqual(
            len(rd.simulate_h1_d1_macro_gated("BTCUSDT", bundle_aligned, 0, 3 * rs.HOUR_MS)), 1
        )
        self.assertEqual(
            len(rd.simulate_h1_d1_macro_gated("BTCUSDT", bundle_flat_1d, 0, 3 * rs.HOUR_MS)), 0
        )

    def test_evaluate_d1_sensitivity_grid_counts_18_combinations(self):
        bars_flat = _make_bars_1h(24 * 25, base=100.0, step=0.0)
        bundle = rd.build_extended_bundle(bars_flat, [])
        bundles = {"BTCUSDT": bundle}
        b_windows = [(0, 25 * rs.DAY_MS)]
        grid_summary = rd.evaluate_d1_sensitivity_grid(bundles, b_windows, b_windows)
        self.assertEqual(grid_summary["total_configs"], 18)
        self.assertEqual(len(grid_summary["grid_results"]), 18)

    def test_evaluate_d1_bimesters_and_continuous_on_flat_market(self):
        bars_flat = _make_bars_1h(24 * 30, base=100.0, step=0.0)
        bundle = rd.build_extended_bundle(bars_flat, [])
        bundles = {"BTCUSDT": bundle}
        b_windows = [(0, 30 * rs.DAY_MS)]
        rep_b = rd.evaluate_d1_bimesters(bundles, 30, b_windows, self.cfg_050)
        rep_c = rd.evaluate_d1_continuous(bundles, 30, 30 * rs.DAY_MS, self.cfg_050)
        self.assertEqual(rep_b["verdict"], "REJECTED")
        self.assertEqual(rep_c["verdict"], "REJECTED")
        self.assertEqual(rep_c["mode"], "continuous")

    def test_exit_fill_d1_long_and_short_trail_vs_sl(self):
        pos_l = rd._open_pos_d1("BTCUSDT", 1, 0, 100.0, 2.0, self.cfg_050)
        fill_sl = rd._exit_fill_d1(pos_l, rs.Bar(0, 99.0, 99.5, 95.0, 96.0, 1.0), rs.SLIPPAGE_RATE)
        self.assertEqual(fill_sl[1], "SL")

        pos_l_tr = pos_l._replace(stop_price=105.0)
        fill_tr = rd._exit_fill_d1(pos_l_tr, rs.Bar(0, 106.0, 106.5, 104.0, 104.5, 1.0), rs.SLIPPAGE_RATE)
        self.assertEqual(fill_tr[1], "TRAIL")

        pos_s = rd._open_pos_d1("BTCUSDT", -1, 0, 100.0, 2.0, self.cfg_050)
        fill_s_sl = rd._exit_fill_d1(pos_s, rs.Bar(0, 101.0, 105.0, 100.5, 104.0, 1.0), rs.SLIPPAGE_RATE)
        self.assertEqual(fill_s_sl[1], "SL")

    def test_evaluate_h1_d1_hybrid_returns_structured_report(self):
        bars_flat = _make_bars_1h(24 * 30, base=100.0, step=0.0)
        bundle = rd.build_extended_bundle(bars_flat, [])
        bundles = {"BTCUSDT": bundle}
        rep = rd.evaluate_h1_d1_hybrid(bundles, 30, [(0, 30 * rs.DAY_MS)])
        self.assertEqual(rep["mode"], "h1_d1_macro_gated")
        self.assertEqual(rep["verdict"], "REJECTED")


if __name__ == "__main__":
    unittest.main()
