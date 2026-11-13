import unittest
from decimal import Decimal as D
from binance_public import MarketFilters
from risk_checks import (Action, RiskLimits, RiskViolation, plan_actions,
                         size_open_quantity, size_open_quantity_atr,
                         validate_close, validate_open)
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


if __name__ == "__main__":
    unittest.main()
