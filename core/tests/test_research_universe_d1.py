"""Test unitari offline per research_universe_d1.py (Passo A.12)."""
import unittest

import research_signals as rs
import research_universe_d1 as ru


def _make_bars_1d(count, base=100.0, step=1.0):
    return [
        rs.Bar(
            i * rs.DAY_MS,
            base + i * step,
            base + i * step + 1.5,
            base + i * step - 0.5,
            base + i * step + 1.0,
            100.0,
        )
        for i in range(count)
    ]


class EfficiencyRatioAndRankingTests(unittest.TestCase):
    def test_efficiency_ratio_pure_trend_is_one(self):
        closes = [100.0 + i * 2.0 for i in range(25)]
        er = ru.efficiency_ratio(closes, 10)
        self.assertEqual(er[:10], [None] * 10)
        for val in er[10:]:
            self.assertAlmostEqual(val, 1.0)

    def test_efficiency_ratio_alternating_chop_is_zero(self):
        closes = [100.0 if i % 2 == 0 else 102.0 for i in range(25)]
        er = ru.efficiency_ratio(closes, 10)
        for val in er[10:]:
            self.assertAlmostEqual(val, 0.0)

    def test_count_gaps_1d_detects_missing_day(self):
        bars = [
            rs.Bar(0, 100.0, 101.0, 99.0, 100.0, 1.0),
            rs.Bar(rs.DAY_MS, 100.0, 101.0, 99.0, 100.0, 1.0),
            rs.Bar(3 * rs.DAY_MS, 100.0, 101.0, 99.0, 100.0, 1.0),
        ]
        self.assertEqual(ru.count_gaps_1d(bars), 1)

    def test_build_cross_sectional_ranks_identifies_leaders_and_laggards(self):
        b_up1 = ru.build_universe_bundle("AAAUSDT", _make_bars_1d(40, base=100.0, step=2.0), [])
        b_up2 = ru.build_universe_bundle("BBBUSDT", _make_bars_1d(40, base=100.0, step=1.0), [])
        b_dn1 = ru.build_universe_bundle("CCCUSDT", _make_bars_1d(40, base=200.0, step=-1.0), [])
        b_dn2 = ru.build_universe_bundle("DDDUSDT", _make_bars_1d(40, base=200.0, step=-2.0), [])
        bundles = {"AAAUSDT": b_up1, "BBBUSDT": b_up2, "CCCUSDT": b_dn1, "DDDUSDT": b_dn2}

        leaders, laggards = ru.build_cross_sectional_ranks(bundles, top_k=1)
        t_check = 30 * rs.DAY_MS
        self.assertEqual(leaders[t_check], {"AAAUSDT"})
        self.assertEqual(laggards[t_check], {"DDDUSDT"})


class ArchitectureSignalAndSimulationTests(unittest.TestCase):
    def setUp(self):
        bars = _make_bars_1d(60, base=100.0, step=1.0)
        last_t = 59 * rs.DAY_MS
        bars[-1] = rs.Bar(last_t, 159.0, 161.2, 158.5, 160.8, 100.0)
        self.bundle_leader = ru.build_universe_bundle("SOLUSDT", bars, [])
        self.t_sig = last_t
        self.leaders = {self.t_sig: {"SOLUSDT"}}
        self.laggards = {self.t_sig: {"DOGEUSDT"}}

    def test_all_four_architectures_fire_long_on_clean_leader_breakout(self):
        for arch in ru.ARCHITECTURES:
            sig = ru.signal_architecture(
                arch, self.bundle_leader, 59, self.leaders, self.laggards
            )
            self.assertEqual(sig, 1, msg=f"Expected LONG signal for {arch}")

    def test_rs_leader_blocks_symbol_not_in_leaders_set(self):
        sig = ru.signal_architecture(
            "d1_rs_leader_trail", self.bundle_leader, 59, {self.t_sig: {"BTCUSDT"}}, self.laggards
        )
        self.assertEqual(sig, 0)

    def test_squeeze_er_blocks_when_efficiency_ratio_is_low(self):
        low_er_list = list(self.bundle_leader.er10)
        low_er_list[59] = 0.15
        bundle_chop = self.bundle_leader._replace(er10=low_er_list)
        sig = ru.signal_architecture(
            "d1_squeeze_er_trail", bundle_chop, 59, self.leaders, self.laggards
        )
        self.assertEqual(sig, 0)

    def test_tsmom_blocks_when_30d_return_is_negative(self):
        neg_r30 = list(self.bundle_leader.ret30)
        neg_r30[59] = -0.02
        bundle_div = self.bundle_leader._replace(ret30=neg_r30)
        sig = ru.signal_architecture(
            "d1_tsmom_erf_trail", bundle_div, 59, self.leaders, self.laggards
        )
        self.assertEqual(sig, 0)

    def test_funding_crowding_blocks_long_entry_on_protected_architectures(self):
        crowded_f = list(self.bundle_leader.funding_last)
        crowded_f[59] = 0.0006
        bundle_crowded = self.bundle_leader._replace(funding_last=crowded_f)
        for arch in ("d1_rs_leader_trail", "d1_squeeze_er_trail", "d1_tsmom_erf_trail"):
            self.assertEqual(
                ru.signal_architecture(arch, bundle_crowded, 59, self.leaders, self.laggards), 0
            )

    def test_invalid_architecture_raises_value_error(self):
        with self.assertRaises(ValueError):
            ru.signal_architecture("unknown_arch", self.bundle_leader, 59, self.leaders, self.laggards)

    def test_evaluate_architecture_and_neighborhood_on_flat_market(self):
        flat_bars = [rs.Bar(i * rs.DAY_MS, 100.0, 100.0, 100.0, 100.0, 1.0) for i in range(65)]
        b1 = ru.build_universe_bundle("BTCUSDT", flat_bars, [])
        b2 = ru.build_universe_bundle("ETHUSDT", flat_bars, [])
        bundles = {"BTCUSDT": b1, "ETHUSDT": b2}
        leaders, laggards = ru.build_cross_sectional_ranks(bundles)
        windows = [(0, 65 * rs.DAY_MS)]

        rep = ru.evaluate_architecture(
            "d1_rs_leader_trail", bundles, leaders, laggards, 65, windows, "bimesters"
        )
        self.assertEqual(rep["verdict"], "REJECTED")
        self.assertIn("TOO_FEW_TRADES", rep["reasons"])

        stab = ru.evaluate_neighborhood(
            "d1_rs_leader_trail", bundles, leaders, laggards, windows, windows
        )
        self.assertEqual(stab["combinations"], 12)
        self.assertEqual(stab["both_horizons_accepted"], 0)


if __name__ == "__main__":
    unittest.main()
