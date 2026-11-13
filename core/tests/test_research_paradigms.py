"""Test unitari offline per research_paradigms.py (Passo A.10)."""
import unittest

import research_paradigms as rp
import research_signals as rs


def _make_bars_1h(count, base=100.0, step=0.1):
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


class FundingAndAggregationTests(unittest.TestCase):
    def test_aggregate_span_4h_and_1d(self):
        bars = _make_bars_1h(48, base=100.0, step=0.0)
        bars_4h = rp.aggregate_span(bars, rs.FOUR_HOUR_MS)
        bars_1d = rp.aggregate_span(bars, rs.DAY_MS)
        self.assertEqual(len(bars_4h), 12)
        self.assertEqual(len(bars_1d), 2)

    def test_align_funding_series_is_causal_and_computes_sum3(self):
        points = [
            rp.FundingPoint(8 * rs.HOUR_MS, 0.0001),
            rp.FundingPoint(16 * rs.HOUR_MS, 0.0002),
            rp.FundingPoint(24 * rs.HOUR_MS, 0.0003),
            rp.FundingPoint(32 * rs.HOUR_MS, -0.0001),
        ]
        bar_times = [0, 8 * rs.HOUR_MS, 24 * rs.HOUR_MS, 32 * rs.HOUR_MS]
        last_out, sum3_out = rp.align_funding_series(bar_times, rs.HOUR_MS, points)
        self.assertEqual(last_out[0], 0.0)
        self.assertEqual(sum3_out[0], 0.0)
        self.assertAlmostEqual(last_out[1], 0.0001)
        self.assertAlmostEqual(sum3_out[1], 0.0001)
        self.assertAlmostEqual(last_out[2], 0.0003)
        self.assertAlmostEqual(sum3_out[2], 0.0006)
        self.assertAlmostEqual(last_out[3], -0.0001)
        self.assertAlmostEqual(sum3_out[3], 0.0004)

    def test_funding_pnl_for_bar_long_pays_and_short_receives_when_positive(self):
        fmap = {8 * rs.HOUR_MS: 0.001}
        pos_long = rp.open_pos_sized("BTCUSDT", 1, 0, 100.0, 2.0, 1.5, 3.0)
        pos_short = rp.open_pos_sized("BTCUSDT", -1, 0, 100.0, 2.0, 1.5, 3.0)

        pnl_long = rp.funding_pnl_for_bar(pos_long, 7 * rs.HOUR_MS, rs.HOUR_MS, fmap, 100.0)
        pnl_short = rp.funding_pnl_for_bar(pos_short, 7 * rs.HOUR_MS, rs.HOUR_MS, fmap, 100.0)
        self.assertLess(pnl_long, 0.0)
        self.assertGreater(pnl_short, 0.0)
        self.assertAlmostEqual(pnl_long, -pnl_short)

    def test_close_pos_with_funding_adds_accrued_cashflow(self):
        pos = rp.open_pos_sized("BTCUSDT", 1, 0, 100.0, 2.0, 1.5, 3.0)
        t_no_fund = rp.close_pos_with_funding(pos, 102.0, rs.HOUR_MS, "TRAIL", 0.0)
        t_with_fund = rp.close_pos_with_funding(pos, 102.0, rs.HOUR_MS, "TRAIL", 1.25)
        self.assertAlmostEqual(t_with_fund.net_pnl - t_no_fund.net_pnl, 1.25)

    def test_open_pos_sized_respects_notional_cap(self):
        pos = rp.open_pos_sized("BTCUSDT", 1, 0, 100.0, 0.01, 1.5, 3.0)
        self.assertAlmostEqual(pos.qty * pos.entry_price, rs.MAX_NOTIONAL_USDT)

    def test_returns_over_handles_warmup_and_computes_pct_change(self):
        out = rp._returns_over([100.0, 110.0, 121.0], 1)
        self.assertIsNone(out[0])
        self.assertAlmostEqual(out[1], 0.10)
        self.assertAlmostEqual(out[2], 0.10)


class SignalsAndParadigmsTests(unittest.TestCase):
    def test_signal_d1_triggers_on_breakout_and_blocks_low_adx(self):
        bars_1d = [rs.Bar(i * rs.DAY_MS, 100.0, 102.0, 99.0, 101.0, 1.0) for i in range(60)]
        bars_1d[-1] = rs.Bar(59 * rs.DAY_MS, 101.0, 104.5, 100.5, 104.0, 1.0)
        feat_1d = rp.DailyFeatures(
            bars=bars_1d,
            times=[b.open_time for b in bars_1d],
            ema20=[101.5] * 60,
            ema50=[100.0] * 60,
            atr14=[2.0] * 60,
            adx14=[25.0] * 60,
            dc_high_15=[103.0] * 60,
            dc_low_15=[98.0] * 60,
            dc_high_20=[103.0] * 60,
            dc_low_20=[98.0] * 60,
        )
        self.assertEqual(rp.signal_d1(feat_1d, 59, dc_period=15), 1)
        feat_low_adx = feat_1d._replace(adx14=[12.0] * 60)
        self.assertEqual(rp.signal_d1(feat_low_adx, 59, dc_period=15), 0)

    def test_signal_funding_squeeze_4h_long_and_short(self):
        bars_4h = [
            rs.Bar(0, 100.0, 101.0, 99.0, 99.5, 1.0),
            rs.Bar(rs.FOUR_HOUR_MS, 99.5, 103.0, 99.0, 102.5, 1.0),
        ]
        feat_long = rp.H4Features(
            bars=bars_4h,
            times=[0, rs.FOUR_HOUR_MS],
            ema20=[100.0, 100.0],
            ema50=[99.0, 99.0],
            ema200=[95.0, 95.0],
            rsi14=[45.0, 52.0],
            atr14=[2.0, 2.0],
            adx14=[22.0, 22.0],
            dc_high_10=[102.0, 102.0],
            dc_low_10=[98.0, 98.0],
            ret_6=[0.01, 0.02],
            ret_12=[0.02, 0.03],
            funding_last=[-0.0003, -0.0003],
            funding_sum3=[-0.0008, -0.0008],
        )
        self.assertEqual(rp.signal_funding_squeeze_4h(feat_long, 1), 1)

        bars_short = [
            rs.Bar(0, 100.0, 101.0, 99.0, 100.5, 1.0),
            rs.Bar(rs.FOUR_HOUR_MS, 100.5, 101.0, 97.5, 98.0, 1.0),
        ]
        feat_short = feat_long._replace(
            bars=bars_short,
            funding_last=[0.0005, 0.0005],
            funding_sum3=[0.0012, 0.0012],
        )
        self.assertEqual(rp.signal_funding_squeeze_4h(feat_short, 1), -1)

    def test_simulate_cross_sectional_rs_opens_long_leader_and_short_laggard(self):
        bars_up = _make_bars_1h(320, base=100.0, step=0.2)
        bars_dn = _make_bars_1h(320, base=200.0, step=-0.2)
        b_up = rp.build_symbol_bundle(bars_up, [])
        b_dn = rp.build_symbol_bundle(bars_dn, [])
        bundles = {"AAAUSDT": b_up, "BBBUSDT": b_dn}

        trades = rp.simulate_cross_sectional_rs(
            bundles, 200 * rs.HOUR_MS, 300 * rs.HOUR_MS, hold_bars_4h=6, min_spread=0.01
        )
        self.assertGreaterEqual(len(trades), 2)
        sides_by_sym = {t.symbol: t.side for t in trades[:2]}
        self.assertEqual(sides_by_sym["AAAUSDT"], 1)
        self.assertEqual(sides_by_sym["BBBUSDT"], -1)

    def test_simulate_fixed_h1_funding_gate_blocks_crowded_long(self):
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
        bundle_crowded = rp.SymbolBundle(
            feat_1h=feat_1h,
            funding_1h=[0.0005, 0.0005, 0.0005],
            funding_sum3_1h=[0.0015, 0.0015, 0.0015],
            feat_4h=None,
            feat_1d=None,
            funding_map={},
        )
        trades_canonical = rp.simulate_fixed_h1(
            "BTCUSDT", bundle_crowded, 0, 3 * rs.HOUR_MS, funding_gated=False
        )
        trades_gated = rp.simulate_fixed_h1(
            "BTCUSDT", bundle_crowded, 0, 3 * rs.HOUR_MS, funding_gated=True
        )
        self.assertEqual(len(trades_canonical), 1)
        self.assertEqual(len(trades_gated), 0)

    def test_run_paradigm_window_invalid_paradigm_raises(self):
        with self.assertRaises(ValueError):
            rp.run_paradigm_window({}, "invalid_paradigm", 0, rs.HOUR_MS)

    def test_evaluate_paradigm_horizon_returns_structured_report(self):
        bars_flat = _make_bars_1h(120, base=100.0, step=0.0)
        bundle = rp.build_symbol_bundle(bars_flat, [])
        bundles = {"BTCUSDT": bundle, "ETHUSDT": bundle}
        folds = [(0, 60 * rs.HOUR_MS, 120 * rs.HOUR_MS)]
        for paradigm in rp.PARADIGMS:
            report = rp.evaluate_paradigm_horizon(bundles, paradigm, 365, folds)
            self.assertEqual(report["paradigm"], paradigm)
            self.assertEqual(report["verdict"], "REJECTED")
            self.assertIn("TOO_FEW_TRADES", report["reasons"])


if __name__ == "__main__":
    unittest.main()
