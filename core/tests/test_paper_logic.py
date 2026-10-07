import unittest
from decimal import Decimal as D

from paper_trader import FatalError, fill_price, next_delay_s, realized_pnl
from risk_checks import Action

M = 900_000
BASE = 1000 * M   # un multiplo esatto dell'intervallo = istante di chiusura


class TestPaperLogic(unittest.TestCase):
    def test_fill_price_slippage_always_against_us(self):
        self.assertEqual(fill_price(Action.OPEN_LONG, D("100000")), D("100020"))
        self.assertEqual(fill_price(Action.CLOSE_SHORT, D("100000")), D("100020"))
        self.assertEqual(fill_price(Action.OPEN_SHORT, D("100000")), D("99980"))
        self.assertEqual(fill_price(Action.CLOSE_LONG, D("100000")), D("99980"))

    def test_realized_pnl_long(self):
        self.assertEqual(realized_pnl("LONG", D(100), D(101), D(2)), D(2))
        self.assertEqual(realized_pnl("LONG", D(100), D(99), D(2)), D(-2))

    def test_realized_pnl_short(self):
        self.assertEqual(realized_pnl("SHORT", D(100), D(99), D(2)), D(2))
        self.assertEqual(realized_pnl("SHORT", D(100), D(101), D(2)), D(-2))

    def test_realized_pnl_invalid_side(self):
        with self.assertRaises(FatalError):
            realized_pnl("FLAT", D(100), D(101), D(2))

    def test_delay_waits_for_next_close(self):
        self.assertAlmostEqual(next_delay_s(BASE + 10_000, True), 893.0)

    def test_delay_retries_fast_right_after_close_if_nothing_new(self):
        self.assertEqual(next_delay_s(BASE + 10_000, False), 5.0)

    def test_delay_when_nothing_new_far_from_close(self):
        self.assertAlmostEqual(next_delay_s(BASE + 300_000, False), 603.0)

    def test_delay_when_halted(self):
        self.assertEqual(next_delay_s(BASE + 10_000, False, halted=True), 30.0)


if __name__ == "__main__":
    unittest.main()
