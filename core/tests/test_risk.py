import unittest
from decimal import Decimal as D
from binance_public import MarketFilters
from risk_checks import (Action, RiskLimits, RiskViolation, effective_limits,
                         plan_actions, safe_leverage_for_stop, size_open_quantity,
                         size_open_quantity_atr, validate_close,
                         validate_leverage_for_exposure, validate_open)
from strategy import Signal

F = MarketFilters(D("0.1"), D("0.001"), D("0.001"), D("120"), D("100"))
L = RiskLimits()


class TestRisk(unittest.TestCase):
    def test_plan(self):
        self.assertEqual(plan_actions(Signal.BUY, "FLAT"), [Action.OPEN_LONG])
        self.assertEqual(plan_actions(Signal.BUY, "SHORT"), [Action.CLOSE_SHORT, Action.OPEN_LONG])
        self.assertEqual(plan_actions(Signal.BUY, "LONG"), [])
        self.assertEqual(plan_actions(Signal.HOLD, "LONG"), [])

    def test_size_respects_min_notional(self):
        q = size_open_quantity(D("100000"), F, L)
        self.assertEqual(q, D("0.002"))
        self.assertGreaterEqual(q * D("100000"), F.min_notional)

    def test_open_blocked_when_position_open(self):
        with self.assertRaises(RiskViolation):
            validate_open(D("0.002"), D("100000"), F, L, 1, D(0))

    def test_open_blocked_on_daily_loss(self):
        with self.assertRaises(RiskViolation):
            validate_open(D("0.002"), D("100000"), F, L, 0, D("-100"))

    def test_close_always_allowed_with_open_position(self):
        # regressione: il prompt originale bloccava la chiusura
        self.assertEqual(validate_close(D("0.002"), D("0.002"))["result"], "PASS_CLOSE")

    def test_close_qty_mismatch(self):
        with self.assertRaises(RiskViolation):
            validate_close(D("0.001"), D("0.002"))

    def test_size_open_quantity_atr_risk_and_notional_caps(self):
        doge_f = MarketFilters(D("0.00001"), D("1"), D("1"), D("1000000"), D("5"))
        limits = RiskLimits(max_notional_usdt=D("500"), max_open_positions=3,
                            enforce_btc_qty_cap=False)
        # Risk-bound: 1000 * 0.01 = 10 USDT risk / 0.025 stop = 400 DOGE -> 80 USDT
        q_risk = size_open_quantity_atr(D("1000"), D("0.01"), D("0.025"), D("0.20"), doge_f, limits)
        self.assertEqual(q_risk, D("400"))

        # Notional-bound: 10000 * 0.05 = 500 USDT risk / 0.01 stop = 50000 DOGE, capped at 500/0.20 = 2500
        q_cap = size_open_quantity_atr(D("10000"), D("0.05"), D("0.01"), D("0.20"), doge_f, limits)
        self.assertEqual(q_cap, D("2500"))

        # Below min_notional (50 USDT on BTC)
        with self.assertRaises(RiskViolation):
            size_open_quantity_atr(D("1000"), D("0.01"), D("25000"), D("60000"), F, limits)

        # Invalid inputs
        with self.assertRaises(RiskViolation):
            size_open_quantity_atr(D("1000"), D("0.01"), D("0"), D("60000"), F, limits)

    def test_validate_open_multi_symbol_and_max_positions(self):
        doge_f = MarketFilters(D("0.00001"), D("1"), D("1"), D("1000000"), D("5"))
        limits = RiskLimits(max_notional_usdt=D("500"), max_open_positions=3,
                            enforce_btc_qty_cap=False)
        res = validate_open(D("600"), D("0.20"), doge_f, limits, 2, D("0"))
        self.assertEqual(res["result"], "PASS")
        self.assertEqual(res["notional_usdt"], "120.00")

        with self.assertRaises(RiskViolation):
            validate_open(D("600"), D("0.20"), doge_f, limits, 3, D("0"))

    def test_v2_dynamic_notional_cap_same_side_and_leverage_exposure(self):
        doge_f = MarketFilters(D("0.00001"), D("1"), D("1"), D("10000000"), D("5"))
        limits = RiskLimits(
            max_notional_usdt=D("20000"),
            max_open_positions=3,
            max_daily_loss_usdt=D("250"),
            enforce_btc_qty_cap=False,
            max_notional_equity_mult=D("2.0"),
            max_same_side_positions=2,
        )
        eff = effective_limits(D("5000"), limits)
        self.assertEqual(eff.max_notional_usdt, D("10000.0"))

        # Stop stretto (0.002 su price=1 -> 0.2%): risk_usdt=50 -> 25000 qty, cappato a 2.0*5000 = 10000 qty
        q_capped = size_open_quantity_atr(D("5000"), D("0.01"), D("0.002"), D("1"), doge_f, limits)
        self.assertEqual(q_capped, D("10000"))

        # Leva 2..6 non basta per 3 * 2.0 * equity * 1.10; 7 passa
        for bad_lev in (2, 5, 6):
            with self.assertRaises(RiskViolation):
                validate_leverage_for_exposure(bad_lev, limits)
        lev_info = validate_leverage_for_exposure(7, limits)
        self.assertEqual(lev_info["min_required_leverage"], 7)

        # Limite per direzione = 2
        with self.assertRaises(RiskViolation):
            validate_open(D("100"), D("1"), doge_f, eff, 2, D("0"), same_side_open_positions=2, side="LONG")

        # Guardia margine disponibile evita -2019
        with self.assertRaises(RiskViolation):
            validate_open(D("5000"), D("1"), doge_f, eff, 1, D("0"), available_margin_usdt=D("500"), leverage=7)

        # Safe leverage per stop molto ampio (> 10.7%) riduce sotto 7x per protezione liquidazione isolata
        self.assertEqual(safe_leverage_for_stop(D("0.05"), D("1"), 7), 7)
        self.assertEqual(safe_leverage_for_stop(D("0.20"), D("1"), 7), 3)


if __name__ == "__main__":
    unittest.main()
