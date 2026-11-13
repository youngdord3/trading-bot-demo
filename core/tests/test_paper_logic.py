import unittest
from contextlib import contextmanager
from decimal import Decimal as D

import paper_report
from binance_public import MarketFilters
from paper_trader import (
    INTERVAL,
    INTERVAL_MS,
    UNIVERSE_SYMBOLS,
    FatalError,
    close_legacy_positions,
    fill_price,
    next_delay_s,
    open_positions_map,
    process_latest_candles,
    realized_pnl,
)
from risk_checks import Action

M = 900_000
BASE = 1000 * M   # un multiplo esatto dell'intervallo = istante di chiusura
D1_MS = 86_400_000


def _raw_d1_uptrend(n: int = 220, start_day: int = 20_000, step: D = D("2"),
                    last_high: D | None = None, last_low: D | None = None) -> list[list]:
    rows = []
    price = D("100")
    for i in range(n):
        t0 = (start_day + i) * D1_MS
        t1 = t0 + D1_MS - 1
        open_p = price
        close_p = price + step
        high_p = max(open_p, close_p) + D("1") if last_high is None or i < n - 1 else last_high
        low_p = min(open_p, close_p) - D("0.5") if last_low is None or i < n - 1 else last_low
        rows.append([t0, str(open_p), str(high_p), str(low_p), str(close_p), "0", t1])
        price = close_p
    return rows


class FakeCursor:
    def __init__(self, state: dict):
        self.state = state
        self._one = None
        self._all = []
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql: str, params=()):
        q = " ".join(sql.split())
        self.rowcount = 0
        if "FROM bot.paper_processed_candles" in q and "COALESCE(MAX" in q:
            sym, interval = params
            vals = [r[2] for r in self.state["candles"] if r[0] == sym and r[1] == interval]
            self._one = (max(vals) if vals else 0,)
        elif "FROM bot.paper_trades" in q and "DISTINCT ON (symbol)" in q:
            latest = {}
            for row in self.state["trades"]:
                latest[row["symbol"]] = row
            self._all = [
                (r["symbol"], r["action"], r["quantity"], r["fill_price"],
                 r["signal_reason"], r["candle_open_time_ms"], r["risk_checks"])
                for r in latest.values()
            ]
        elif "SUM(realized_pnl_usdt - fee_usdt)" in q:
            total = sum((r["realized_pnl_usdt"] - r["fee_usdt"] for r in self.state["trades"]), D(0))
            self._one = (total,)
        elif q.startswith("INSERT INTO bot.paper_processed_candles"):
            sym, interval, ts, sig, close_p = params
            key = (sym, interval, ts)
            if any((r[0], r[1], r[2]) == key for r in self.state["candles"]):
                self.rowcount = 0
            else:
                self.state["candles"].append((sym, interval, ts, sig, close_p))
                self.rowcount = 1
        elif q.startswith("INSERT INTO bot.paper_trades"):
            sym, action, side, qty, fill, notional, fee, pnl, reason, ts, checks = params
            checks_obj = checks.obj if hasattr(checks, "obj") else checks
            self.state["trades"].append({
                "id": len(self.state["trades"]) + 1,
                "symbol": sym,
                "action": action,
                "side": side,
                "quantity": qty,
                "fill_price": fill,
                "notional_usdt": notional,
                "fee_usdt": fee,
                "realized_pnl_usdt": pnl,
                "signal_reason": reason,
                "candle_open_time_ms": ts,
                "risk_checks": dict(checks_obj),
            })
            self.rowcount = 1

    def fetchone(self):
        return self._one

    def fetchall(self):
        return list(self._all)


class FakeConn:
    def __init__(self, initial_trades=None, initial_candles=None):
        self.state = {
            "trades": list(initial_trades or []),
            "candles": list(initial_candles or []),
        }

    def cursor(self):
        return FakeCursor(self.state)

    @contextmanager
    def transaction(self):
        yield


class FakeClient:
    def __init__(self, server_now_ms: int, klines_by_sym: dict, prices_by_sym: dict,
                 funding_by_sym: dict | None = None):
        self.server_now_ms = server_now_ms
        self.klines_by_sym = klines_by_sym
        self.prices_by_sym = prices_by_sym
        self.funding_by_sym = funding_by_sym or {}

    def get_server_time_ms(self) -> int:
        return self.server_now_ms

    def get_klines(self, symbol: str, interval: str, limit: int) -> list:
        return list(self.klines_by_sym[symbol])[-limit:]

    def get_price(self, symbol: str) -> D:
        return self.prices_by_sym[symbol]

    def get_funding_rates(self, symbol: str, limit: int = 10) -> list[tuple[int, D]]:
        return list(self.funding_by_sym.get(symbol, []))


class FakeMarket:
    def __init__(self, symbols, client, filters_by_symbol):
        self.symbols = list(symbols)
        self.client = client
        self.filters_by_symbol = filters_by_symbol


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

    def test_close_legacy_position_inserts_migration_close_and_syncs_d1(self):
        btc_klines = _raw_d1_uptrend(225)
        last_close = btc_klines[-1][6]
        client = FakeClient(
            server_now_ms=last_close + 60_000,
            klines_by_sym={"BTCUSDT": btc_klines},
            prices_by_sym={"BTCUSDT": D("68000")},
        )
        filters = {"BTCUSDT": MarketFilters(D("0.1"), D("0.001"), D("0.001"), D("120"), D("50"))}
        market = FakeMarket(["BTCUSDT"], client, filters)
        conn = FakeConn(
            initial_candles=[("BTCUSDT", "15m", 1794603600000, "SELL", D("68600"))],
            initial_trades=[{
                "id": 1, "symbol": "BTCUSDT", "action": "OPEN_SHORT", "side": "SELL",
                "quantity": D("0.0009"), "fill_price": D("68598"), "notional_usdt": D("61.73"),
                "fee_usdt": D("0.03"), "realized_pnl_usdt": D("0"),
                "signal_reason": "EMA_9_21_CROSSOVER",
                "candle_open_time_ms": 1794603600000, "risk_checks": {},
            }],
        )
        closed = close_legacy_positions(conn, market)
        self.assertEqual(closed, 1)
        self.assertEqual(open_positions_map(conn), {})
        close_trade = conn.state["trades"][-1]
        self.assertEqual(close_trade["action"], "CLOSE_SHORT")
        self.assertEqual(close_trade["signal_reason"], "STRATEGY_MIGRATION")
        self.assertEqual(close_trade["risk_checks"]["migrated_from"], "EMA_9_21_CROSSOVER")
        self.assertEqual(close_trade["candle_open_time_ms"], btc_klines[-1][0])

    def test_stale_bootstrap_records_hold_without_opening_trade(self):
        doge_klines = _raw_d1_uptrend(225, step=D("2"))
        last_close = doge_klines[-1][6]
        client = FakeClient(
            server_now_ms=last_close + 10 * 3_600_000,  # 10h dopo la chiusura 1d -> fuori ENTRY_GRACE_MS
            klines_by_sym={"DOGEUSDT": doge_klines},
            prices_by_sym={"DOGEUSDT": D("600")},
        )
        filters = {"DOGEUSDT": MarketFilters(D("0.00001"), D("1"), D("1"), D("1000000"), D("5"))}
        market = FakeMarket(["DOGEUSDT"], client, filters)
        conn = FakeConn()
        self.assertTrue(process_latest_candles(conn, market))
        self.assertEqual(len(conn.state["trades"]), 0)
        self.assertEqual(conn.state["candles"][-1][3], "HOLD")

    def test_fresh_entry_trailing_advance_and_exit_with_funding(self):
        # Forza last_processed > 0 per entrare su barra 220, poi avanza trailing sopra entry e chiude su TRAIL
        all_rows = _raw_d1_uptrend(225, step=D("2"))
        entry_rows = all_rows[:221]
        entry_close_ms = entry_rows[-1][6]
        filters = {"DOGEUSDT": MarketFilters(D("0.001"), D("0.01"), D("0.01"), D("100000"), D("5"))}
        prev_open_ms = entry_rows[-2][0]
        conn = FakeConn(initial_candles=[("DOGEUSDT", INTERVAL, prev_open_ms, "HOLD", D(entry_rows[-2][4]))])

        c1 = FakeClient(
            server_now_ms=entry_close_ms + 30_000,
            klines_by_sym={"DOGEUSDT": entry_rows},
            prices_by_sym={"DOGEUSDT": D(entry_rows[-1][4])},
            funding_by_sym={"DOGEUSDT": [(entry_close_ms + 1, D("0.00005"))]},
        )
        self.assertTrue(process_latest_candles(conn, FakeMarket(["DOGEUSDT"], c1, filters)))
        self.assertEqual(len(conn.state["trades"]), 1)
        open_tr = conn.state["trades"][0]
        self.assertEqual(open_tr["action"], "OPEN_LONG")
        self.assertIn("initial_stop_price", open_tr["risk_checks"])
        self.assertIn("trail_distance", open_tr["risk_checks"])

        # Barra successiva spinge il massimo a 565 -> stop trailing sale ampiamente sopra entry (542.1)
        bar1 = list(all_rows[221])
        bar1[2] = "565"
        bar1[4] = "560"
        bar1_rows = entry_rows + [bar1]
        bar1_close_ms = bar1[6]
        c2 = FakeClient(
            server_now_ms=bar1_close_ms + 30_000,
            klines_by_sym={"DOGEUSDT": bar1_rows},
            prices_by_sym={"DOGEUSDT": D("560")},
            funding_by_sym={"DOGEUSDT": [(bar1_close_ms + 1, D("0.00010"))]},
        )
        self.assertTrue(process_latest_candles(conn, FakeMarket(["DOGEUSDT"], c2, filters)))
        self.assertEqual(len(conn.state["trades"]), 1)

        # Barra ancora dopo apre in ribasso e rompe il trailing stop -> chiude con TRAIL e include funding
        bar2 = list(all_rows[222])
        bar2[1] = "552"
        bar2[2] = "553"
        bar2[3] = "400"
        bar2[4] = "500"
        bar2_rows = bar1_rows + [bar2]
        bar2_close_ms = bar2[6]
        c3 = FakeClient(
            server_now_ms=bar2_close_ms + 30_000,
            klines_by_sym={"DOGEUSDT": bar2_rows},
            prices_by_sym={"DOGEUSDT": D("500")},
            funding_by_sym={"DOGEUSDT": [
                (bar1[0] + 28_800_000, D("0.00010")),
                (bar2[0] + 28_800_000, D("0.00020")),
            ]},
        )
        self.assertTrue(process_latest_candles(conn, FakeMarket(["DOGEUSDT"], c3, filters)))
        self.assertEqual(len(conn.state["trades"]), 2)
        close_tr = conn.state["trades"][1]
        self.assertEqual(close_tr["action"], "CLOSE_LONG")
        self.assertEqual(close_tr["signal_reason"], "TRAIL")
        self.assertLess(D(close_tr["risk_checks"]["funding_pnl_usdt"]), D("0"))

    def test_enforces_max_open_positions_3_across_symbols(self):
        syms = list(UNIVERSE_SYMBOLS[:4])
        rows = _raw_d1_uptrend(225, step=D("2"))
        prev_open_ms = rows[-2][0]
        last_close_ms = rows[-1][6]
        conn = FakeConn(initial_candles=[(s, INTERVAL, prev_open_ms, "HOLD", D(rows[-2][4])) for s in syms])
        filters = {s: MarketFilters(D("0.0001"), D("0.01"), D("0.01"), D("100000"), D("5")) for s in syms}
        client = FakeClient(
            server_now_ms=last_close_ms + 30_000,
            klines_by_sym={s: rows for s in syms},
            prices_by_sym={s: D(rows[-1][4]) for s in syms},
        )
        process_latest_candles(conn, FakeMarket(syms, client, filters))
        self.assertEqual(len(open_positions_map(conn)), 3)

    def test_paper_report_evaluate_report(self):
        now_ms = 20_050 * D1_MS + 1_000
        open_ms = 20_049 * D1_MS
        stats = {s: (open_ms, 1) for s in UNIVERSE_SYMBOLS}
        trades = [
            (1, "BTCUSDT", "OPEN_SHORT", D("0.001"), D("0"), D("0.03")),
            (2, "BTCUSDT", "CLOSE_SHORT", D("0.001"), D("1.0"), D("0.03")),
        ]
        rep = paper_report.evaluate_report(stats, trades, orphan_trades=0, now_ms=now_ms, expect_running=True)
        self.assertEqual(rep["result"], "OK")
        self.assertEqual(rep["problems"], [])

        bad = paper_report.evaluate_report(
            {s: (open_ms, 1) for s in UNIVERSE_SYMBOLS[:11]}, trades, orphan_trades=1, now_ms=now_ms, expect_running=True
        )
        self.assertEqual(bad["result"], "PROBLEMS")
        self.assertTrue(any("TRADES_WITHOUT_PROCESSED_CANDLE" in p for p in bad["problems"]))
        self.assertTrue(any("NO_CANDLES_PROCESSED" in p for p in bad["problems"]))


if __name__ == "__main__":
    unittest.main()
