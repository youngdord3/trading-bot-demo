"""Test offline del backtest avanzato: nessuna rete, indicatori iniettati a mano."""
import unittest
from decimal import Decimal as D

from backtest_advanced import (
    Bar, Costs, Indicators, Params, Trade,
    accept, parse_bars, simulate, split_index, summarize,
)

NO_COSTS = Costs(D(1), D(0), D(0))
PARAMS = Params(1, 2, D(25), D(1), D(2))
HEAD = [(100, 101, 99, 100), (100, 101, 99, 100), (100, 105, 99, 104)]
TAIL = [(120, 121, 119, 120), (120, 121, 119, 120)]


def _bars(rows):
    return [Bar(i * 60_000, i * 60_000 + 59_999, D(o), D(h), D(l), D(c))
            for i, (o, h, l, c) in enumerate(rows)]


def _ind(fast, adx_value=30):
    size = len(fast)
    return Indicators(
        ema={1: [D(v) for v in fast], 2: [D(1)] * size},
        atr=[D(10)] * size,
        adx=[D(adx_value)] * size,
    )


def _run(fourth_row, fast=(0, 2, 2, 2, 2, 2), costs=NO_COSTS, adx_value=30, tail=None):
    bars = _bars(HEAD + [fourth_row] + (tail or TAIL))
    return simulate(bars, _ind(fast, adx_value), PARAMS, 0, len(bars), costs)


def _trade(net):
    return Trade("LONG", 0, 1, D(100), D(100), D(net), D(0), "TAKE")


class ParseBarsTest(unittest.TestCase):
    def test_parse_bars_keeps_only_closed(self):
        raw = [
            [0, "100", "101", "99", "100.5", "1", 59_999],
            [60_000, "100.5", "102", "100", "101", "1", 119_999],
        ]
        bars = parse_bars(raw, 100_000)
        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0].high, D("101"))
        self.assertEqual(bars[0].low, D("99"))

    def test_parse_bars_rejects_invalid(self):
        with self.assertRaises(ValueError):
            parse_bars([[0, "100", "101"]], 100_000)
        with self.assertRaises(ValueError):
            parse_bars([[0, "100", "99", "101", "100", "1", 59_999]], 100_000)
        with self.assertRaises(ValueError):
            parse_bars([[60_000, "1", "2", "1", "1", "1", 61_000], [0, "1", "2", "1", "1", "1", 1_000]], 100_000)


class SimulateTest(unittest.TestCase):
    def test_long_take_profit(self):
        trades = _run((104, 121, 103, 120))
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0].side, "LONG")
        self.assertEqual(trades[0].reason, "TAKE")
        self.assertEqual(trades[0].net, D(20))

    def test_long_stop_loss(self):
        trades = _run((104, 105, 89, 95))
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0].reason, "STOP")
        self.assertEqual(trades[0].net, D(-10))

    def test_stop_wins_when_both_hit(self):
        trades = _run((104, 121, 89, 95))
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0].reason, "STOP")
        self.assertEqual(trades[0].net, D(-10))

    def test_gap_through_stop_fills_at_open(self):
        trades = _run((85, 86, 80, 82))
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0].reason, "STOP")
        self.assertEqual(trades[0].net, D(-15))

    def test_short_take_profit(self):
        trades = _run((96, 97, 79, 80), fast=(2, 0, 0, 0, 0, 0))
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0].side, "SHORT")
        self.assertEqual(trades[0].reason, "TAKE")
        self.assertEqual(trades[0].net, D(20))

    def test_adx_filter_blocks_entry(self):
        self.assertEqual(_run((104, 121, 103, 120), adx_value=10), [])

    def test_fees_and_slippage(self):
        costs = Costs(D(1), D("0.001"), D("0.01"))
        trades = _run((104, 121, 103, 120), costs=costs)
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0].entry_price, D("101"))
        self.assertEqual(trades[0].exit_price, D("118.8"))
        self.assertEqual(trades[0].fees, D("0.2198"))
        self.assertEqual(trades[0].net, D("17.5802"))

    def test_signal_exit_and_reverse(self):
        tail = [(102, 103, 101, 102), (102, 103, 101, 102)]
        trades = _run((104, 105, 103, 104), fast=(0, 2, 2, 0, 0, 0), tail=tail)
        self.assertEqual(len(trades), 2)
        self.assertEqual((trades[0].side, trades[0].reason), ("LONG", "SIGNAL"))
        self.assertEqual(trades[0].net, D(2))
        self.assertEqual((trades[1].side, trades[1].reason), ("SHORT", "END"))
        self.assertEqual(trades[1].net, D(0))


class MetricsTest(unittest.TestCase):
    def test_summarize_metrics(self):
        metrics = summarize([_trade(20), _trade(-10), _trade(5)], D(1000))
        self.assertEqual(metrics["trades"], 3)
        self.assertEqual(metrics["wins"], 2)
        self.assertEqual(metrics["losses"], 1)
        self.assertEqual(metrics["profit_factor"], D("2.5"))
        self.assertEqual(metrics["payoff_ratio"], D("1.25"))
        self.assertEqual(metrics["net_pnl"], D(15))
        self.assertEqual(metrics["max_drawdown"], D(10))

    def test_summarize_empty(self):
        metrics = summarize([], D(1000))
        self.assertEqual(metrics["trades"], 0)
        self.assertEqual(metrics["win_rate"], D(0))
        self.assertIsNone(metrics["profit_factor"])
        self.assertIsNone(metrics["payoff_ratio"])
        self.assertEqual(metrics["max_drawdown"], D(0))

    def test_acceptance_pass_and_fail(self):
        good = {"trades": 40, "profit_factor": D("1.5"), "net_pnl": D(100), "max_drawdown_pct": D(5), "win_rate": D("0.45"), "payoff_ratio": D("1.2")}
        bad = {"trades": 10, "profit_factor": D("1.1"), "net_pnl": D(5), "max_drawdown_pct": D(20), "win_rate": D("0.30"), "payoff_ratio": D("1.5")}
        self.assertEqual(accept(good), (True, []))
        self.assertEqual(accept(bad), (False, ["TOO_FEW_TRADES", "PROFIT_FACTOR_TOO_LOW", "DRAWDOWN_TOO_HIGH", "NO_EDGE_PROFILE"]))

    def test_split_index(self):
        self.assertEqual(split_index(1000), 700)
        self.assertEqual(split_index(7), 4)


if __name__ == "__main__":
    unittest.main()
