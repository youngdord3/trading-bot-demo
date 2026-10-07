import unittest
from decimal import Decimal as D
from binance_public import MarketFilters
from risk_checks import (Action, RiskLimits, RiskViolation, plan_actions,
                         size_open_quantity, validate_close, validate_open)
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


if __name__ == "__main__":
    unittest.main()
