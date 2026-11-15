"""Test offline TDD per live_trader.py (preflight 12 simboli, gate PAUSED/ACTIVE, cicli 1d e polling rapido)."""
import json
import unittest
from decimal import Decimal as D

from binance_public import MarketFilters
from order_executor import OrderExecutor, TradeClient
from strategy import STRATEGY_REASON, TsmomParams, UNIVERSE_SYMBOLS
import live_trader as lt

BTC_F = MarketFilters(D("0.1"), D("0.001"), D("0.001"), D("120"), D("100"))
ETH_F = MarketFilters(D("0.01"), D("0.001"), D("0.001"), D("1000"), D("20"))
DEFAULT_F = MarketFilters(D("0.001"), D("0.1"), D("0.1"), D("100000"), D("5"))


def _make_trending_raw_klines(n=221, start_open_ms=1_700_000_000_000, step_ms=86_400_000):
    # Primi n-1 chiusi in forte trend rialzista pulito + ultimo aperto non chiuso
    rows = []
    price = 100.0
    for i in range(n):
        ot = start_open_ms + i * step_ms
        ct = ot + step_ms - 1
        # Prime 180 barre laterali/leggermente positive, poi trend forte e lineare nell'ultimo mese
        if i < n - 45:
            o = price
            c = price + 0.05
            h = c + 0.4
            l = o - 0.3
            price = c
        else:
            o = price
            c = price + 3.0
            h = c + 0.5
            l = o - 0.2
            price = c
        rows.append([ot, f"{o:.4f}", f"{h:.4f}", f"{l:.4f}", f"{c:.4f}", "1000", ct])
    return rows


class FakePublicClient:
    def __init__(self, server_now_ms, raw_klines_by_sym=None, funding_rate="0.0001"):
        self.server_now_ms = server_now_ms
        self.raw_klines_by_sym = raw_klines_by_sym or {}
        self.funding_rate = funding_rate
        self.klines_calls = 0

    def measure_time_offset_ms(self):
        return 29780, 15

    def get_server_time_ms(self):
        return self.server_now_ms

    def load_filters_many(self, symbols):
        out = {}
        for s in symbols:
            if s == "BTCUSDT":
                out[s] = BTC_F
            elif s == "ETHUSDT":
                out[s] = ETH_F
            else:
                out[s] = DEFAULT_F
        return out

    def get_klines(self, symbol, interval, limit):
        self.klines_calls += 1
        if symbol in self.raw_klines_by_sym:
            return self.raw_klines_by_sym[symbol]
        return _make_trending_raw_klines()

    def get_funding_rates(self, symbol, limit=500):
        return [(self.server_now_ms - 8 * 3600_000, D(self.funding_rate))]

    def get_price(self, symbol):
        rows = self.raw_klines_by_sym.get(symbol)
        if rows and len(rows) >= 2:
            return D(str(rows[-2][4]))
        return D("244")


class FakeReadClient:
    def __init__(self, equity="5816.60", dual=False, sym_configs=None):
        self.equity = equity
        self.dual = dual
        self.sym_configs = sym_configs if sym_configs is not None else [
            {"symbol": s, "marginType": "ISOLATED", "leverage": 2}
            for s in UNIVERSE_SYMBOLS
        ]
        self.sync_calls = 0

    def sync_time_offset(self):
        self.sync_calls += 1
        return 29780

    def get(self, path, params=None):
        if path == "/fapi/v3/account":
            return {"totalMarginBalance": self.equity}
        if path == "/fapi/v1/positionSide/dual":
            return {"dualSidePosition": self.dual}
        if path == "/fapi/v1/symbolConfig":
            return self.sym_configs
        raise AssertionError(path)


class FakeLiveTradeClient(TradeClient):
    def __init__(self):
        super().__init__(api_key="k" * 20, api_secret="s" * 32, time_offset_ms=0)
        self.requests = []
        self.algo_status_by_id = {}

    def sync_time_offset(self):
        return 29780

    def request(self, method, path, params=None):
        p = dict(params or {})
        self.requests.append((method, path, p))
        if (method, path) == ("POST", "/fapi/v1/marginType"):
            return {"code": 200, "msg": "success"}
        if (method, path) == ("POST", "/fapi/v1/leverage"):
            return {"symbol": p["symbol"], "leverage": p["leverage"]}
        if (method, path) == ("POST", "/fapi/v1/order"):
            avg = "90000.0" if p["symbol"] == "BTCUSDT" else "244.0"
            return {
                "orderId": 1000 + len(self.requests),
                "clientOrderId": p["newClientOrderId"],
                "symbol": p["symbol"],
                "status": "FILLED",
                "executedQty": p["quantity"],
                "avgPrice": avg,
            }
        if (method, path) == ("POST", "/fapi/v1/algoOrder"):
            aid = 8000 + len(self.requests)
            self.algo_status_by_id[aid] = {"algoId": aid, "clientAlgoId": p["clientAlgoId"], "algoStatus": "NEW"}
            return self.algo_status_by_id[aid]
        if (method, path) == ("GET", "/fapi/v1/algoOrder"):
            aid = p.get("algoId")
            return self.algo_status_by_id.get(aid, {"algoId": aid, "algoStatus": "NEW"})
        if (method, path) == ("DELETE", "/fapi/v1/algoOrder"):
            return {"algoId": p.get("algoId"), "algoStatus": "CANCELED"}
        raise AssertionError((method, path, p))


class FakeLiveCursor:
    def __init__(self, db):
        self.db = db
        self._rows = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params=None):
        norm = " ".join(sql.split())
        params = params or ()
        self._rows = []

        if "FROM bot.system_state" in norm:
            self._rows = [(self.db.status, self.db.orders_enabled)]
            return

        if "SELECT COALESCE(MAX(candle_open_time_ms), 0) FROM bot.live_processed_candles" in norm:
            sym, interval = params
            vals = [k[2] for k in self.db.processed_candles if k[0] == sym and k[1] == interval]
            self._rows = [(max(vals) if vals else 0,)]
            return

        if "INSERT INTO bot.live_processed_candles" in norm:
            sym, interval, ms, sig, close_p = params
            key = (sym, interval, ms)
            if key not in self.db.processed_candles:
                self.db.processed_candles[key] = (sig, close_p)
                self._rows = [(1,)]
            return

        if "SELECT COALESCE(SUM(" in norm and "FROM bot.orders" in norm:
            self._rows = [(D("0"),)]
            return

        if norm.startswith("SELECT ") and "FROM bot.live_positions WHERE symbol = %s" in norm:
            pos = self.db.live_positions.get(params[0])
            if pos is not None:
                self._rows = [self.db._pos_tuple(pos)]
            return

        if norm.startswith("SELECT ") and "FROM bot.live_positions" in norm:
            self._rows = [self.db._pos_tuple(p) for p in self.db.live_positions.values()]
            return

        if "FROM bot.orders" in norm and "WHERE order_kind = 'MARKET' AND status IN" in norm:
            self._rows = [self.db._order_tuple(o) for o in self.db.orders if o["order_kind"] == "MARKET" and o["status"] in ("PENDING", "NEW", "PARTIALLY_FILLED")]
            return

        if "FROM bot.orders" in norm and "WHERE symbol = %s AND candle_open_time_ms = %s AND action = %s" in norm:
            sym, cms, act = params
            self._rows = [
                self.db._order_tuple(o)
                for o in self.db.orders
                if o["symbol"] == sym and o["candle_open_time_ms"] == cms and o["action"] == act and o["order_kind"] == "MARKET" and o["status"] != "FAILED"
            ]
            return

        if norm.startswith("INSERT INTO bot.orders"):
            (cid, ex_id, sym, cms, kind, act, side, ro, req_q, exec_q, avg_p, trig_p, st, reason, rc) = params
            oid = len(self.db.orders) + 1
            self.db.orders.append({
                "id": oid, "client_order_id": cid, "exchange_order_id": ex_id, "symbol": sym,
                "candle_open_time_ms": cms, "order_kind": kind, "action": act, "side": side,
                "reduce_only": ro, "requested_qty": D(str(req_q)), "executed_qty": D(str(exec_q)),
                "avg_fill_price": D(str(avg_p)) if avg_p is not None else None,
                "trigger_price": D(str(trig_p)) if trig_p is not None else None,
                "status": st, "signal_reason": reason,
                "risk_checks": json.loads(rc) if isinstance(rc, str) else rc,
            })
            return

        if norm.startswith("UPDATE bot.orders SET"):
            st, ex_id, ex_q, avg_p, err_c, cid = params
            for o in self.db.orders:
                if o["client_order_id"] == cid:
                    o.update({"status": st, "exchange_order_id": ex_id, "executed_qty": D(str(ex_q)),
                              "avg_fill_price": D(str(avg_p)) if avg_p is not None else None, "error_code": err_c})
            return

        if norm.startswith("INSERT INTO bot.live_positions"):
            (sym, side, qty, ep, ecid, reason, cms, sp, td, bp, scaid, said, sas, rc) = params
            self.db.live_positions[sym] = {
                "symbol": sym, "side": side, "quantity": D(str(qty)), "entry_price": D(str(ep)),
                "entry_client_order_id": ecid, "signal_reason": reason, "candle_open_time_ms": cms,
                "stop_price": D(str(sp)), "trail_distance": D(str(td)), "best_price": D(str(bp)),
                "stop_client_algo_id": scaid, "stop_algo_id": said, "stop_algo_status": sas,
                "risk_checks": json.loads(rc) if isinstance(rc, str) else rc,
            }
            return

        if norm.startswith("UPDATE bot.live_positions SET stop_price"):
            sp, scaid, said, sas, sym = params
            pos = self.db.live_positions[sym]
            pos.update({"stop_price": D(str(sp)), "stop_client_algo_id": scaid, "stop_algo_id": said, "stop_algo_status": sas})
            return

        if norm.startswith("UPDATE bot.live_positions SET best_price"):
            bp, sp, sym = params
            pos = self.db.live_positions[sym]
            pos.update({"best_price": D(str(bp)), "stop_price": D(str(sp))})
            return

        if norm.startswith("DELETE FROM bot.live_positions WHERE symbol = %s"):
            self.db.live_positions.pop(params[0], None)
            return

        if norm.startswith("INSERT INTO bot.audit_events"):
            self.db.audit_events.append((params[0], params[1]))
            return

        raise AssertionError(f"Unexpected SQL: {norm}")

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class FakeLiveConn:
    def __init__(self, status="ACTIVE", orders_enabled=True):
        self.status = status
        self.orders_enabled = orders_enabled
        self.processed_candles = {}
        self.orders = []
        self.live_positions = {}
        self.audit_events = []

    def _pos_tuple(self, p):
        return (
            p["symbol"], p["side"], p["quantity"], p["entry_price"], p["entry_client_order_id"],
            p["signal_reason"], p["candle_open_time_ms"], p["stop_price"], p["trail_distance"],
            p["best_price"], p["stop_client_algo_id"], p["stop_algo_id"], p["stop_algo_status"],
            p["risk_checks"],
        )

    def _order_tuple(self, o):
        return (
            o["id"], o["client_order_id"], o["exchange_order_id"], o["symbol"], o["candle_open_time_ms"],
            o["action"], o["side"], o["reduce_only"], o["requested_qty"], o["executed_qty"],
            o["avg_fill_price"], o["status"], o["signal_reason"], o["risk_checks"],
        )

    def cursor(self):
        return FakeLiveCursor(self)

    def transaction(self):
        return self

    def commit(self):
        pass

    def rollback(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class TestLiveTrader(unittest.TestCase):
    def test_preflight_sets_only_non_matching_symbols_to_isolated_2x(self):
        sym_cfg = [{"symbol": s, "marginType": "ISOLATED", "leverage": 2} for s in UNIVERSE_SYMBOLS]
        sym_cfg[1]["marginType"] = "CROSSED"
        sym_cfg[1]["leverage"] = 20
        sym_cfg[5]["leverage"] = 10

        read_client = FakeReadClient(sym_configs=sym_cfg)
        trade_client = FakeLiveTradeClient()
        res = lt.ensure_account_preflight(read_client, trade_client, UNIVERSE_SYMBOLS, target_leverage=2)
        self.assertEqual(res["margin_updated"], [UNIVERSE_SYMBOLS[1]])
        self.assertEqual(res["leverage_updated"], [UNIVERSE_SYMBOLS[1], UNIVERSE_SYMBOLS[5]])

        read_hedge = FakeReadClient(dual=True)
        with self.assertRaises(lt.FatalError):
            lt.ensure_account_preflight(read_hedge, trade_client, UNIVERSE_SYMBOLS, target_leverage=2)

    def test_paused_blocks_new_entries_and_still_syncs_triggered_algo_stop(self):
        raw = _make_trending_raw_klines()
        last_closed_close_ms = raw[-2][6]
        pub = FakePublicClient(server_now_ms=last_closed_close_ms + 10_000, raw_klines_by_sym={"BTCUSDT": raw})
        market = lt.LiveMarket(symbols=["BTCUSDT"], public_client=pub)
        market.refresh()

        conn = FakeLiveConn(status="PAUSED", orders_enabled=False)
        conn.live_positions["BTCUSDT"] = {
            "symbol": "BTCUSDT", "side": "LONG", "quantity": D("0.02"), "entry_price": D("90000"),
            "entry_client_order_id": "m_OL_BTC_1", "signal_reason": STRATEGY_REASON,
            "candle_open_time_ms": raw[-3][0], "stop_price": D("87000.0"),
            "trail_distance": D("4000"), "best_price": D("90000"),
            "stop_client_algo_id": "s_btc_1", "stop_algo_id": 777001, "stop_algo_status": "NEW",
            "risk_checks": {},
        }
        trade_client = FakeLiveTradeClient()
        trade_client.algo_status_by_id[777001] = {
            "algoId": 777001, "clientAlgoId": "s_btc_1", "algoStatus": "TRIGGERED",
            "actualOrderId": "998877", "actualPrice": "86980.0", "actualQty": "0.02",
        }
        executor = OrderExecutor(trade_client, TsmomParams())
        processed = lt.process_cycle(conn, market, FakeReadClient(), executor, allow_preflight=False)
        self.assertFalse(processed)
        self.assertEqual(conn.live_positions, {})
        self.assertEqual(conn.orders[-1]["order_kind"], "STOP_MARKET")
        self.assertEqual(pub.klines_calls, 0)  # PAUSED non scarica neppure candele inutili

    def test_active_cycle_sizes_from_real_equity_opens_position_and_respects_max_concurrent(self):
        raw = _make_trending_raw_klines()
        last_closed_close_ms = raw[-2][6]
        syms = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"]
        raw_map = {s: list(raw) for s in syms}
        pub = FakePublicClient(server_now_ms=last_closed_close_ms + 15_000, raw_klines_by_sym=raw_map)
        market = lt.LiveMarket(symbols=syms, public_client=pub)
        market.refresh()

        conn = FakeLiveConn(status="ACTIVE", orders_enabled=True)
        read_client = FakeReadClient(equity="5816.60")
        trade_client = FakeLiveTradeClient()
        executor = OrderExecutor(trade_client, TsmomParams())

        processed = lt.process_cycle(conn, market, read_client, executor)
        self.assertTrue(processed)
        # max_open_positions = 3 su 4 simboli con segnale BUY
        self.assertEqual(len(conn.live_positions), 3)
        self.assertEqual(set(conn.live_positions.keys()), {"BTCUSDT", "ETHUSDT", "SOLUSDT"})
        # Tutte le posizioni hanno stop_algo_id protettivo registrato
        for pos in conn.live_positions.values():
            self.assertIsNotNone(pos["stop_algo_id"])
            self.assertEqual(pos["stop_algo_status"], "NEW")

    def test_open_position_tightens_stop_and_closes_when_stop_hit_on_closed_candle(self):
        raw = _make_trending_raw_klines()
        prev_bar = raw[-3]
        exit_bar_open = raw[-2][0]
        # Forza la barra appena chiusa a violare lo stop LONG (87000)
        raw[-2][3] = "86000.0"
        raw[-2][4] = "86500.0"
        pub = FakePublicClient(server_now_ms=raw[-2][6] + 10_000, raw_klines_by_sym={"BTCUSDT": raw})
        market = lt.LiveMarket(symbols=["BTCUSDT"], public_client=pub)
        market.refresh()

        conn = FakeLiveConn(status="ACTIVE", orders_enabled=True)
        conn.processed_candles[("BTCUSDT", "1d", prev_bar[0])] = ("BUY", D(prev_bar[4]))
        conn.live_positions["BTCUSDT"] = {
            "symbol": "BTCUSDT", "side": "LONG", "quantity": D("0.02"), "entry_price": D("90000"),
            "entry_client_order_id": "m_OL_BTC_1", "signal_reason": STRATEGY_REASON,
            "candle_open_time_ms": prev_bar[0], "stop_price": D("87000.0"),
            "trail_distance": D("4000"), "best_price": D("91000"),
            "stop_client_algo_id": "s_btc_1", "stop_algo_id": 777001, "stop_algo_status": "NEW",
            "risk_checks": {},
        }
        trade_client = FakeLiveTradeClient()
        executor = OrderExecutor(trade_client, TsmomParams())

        processed = lt.process_cycle(conn, market, FakeReadClient(), executor)
        self.assertTrue(processed)
        self.assertEqual(conn.live_positions, {})
        self.assertEqual(conn.orders[-1]["action"], "CLOSE_LONG")
        self.assertTrue(conn.orders[-1]["reduce_only"])
        self.assertEqual(conn.orders[-1]["candle_open_time_ms"], exit_bar_open)
        self.assertIn(("DELETE", "/fapi/v1/algoOrder"), [(m, p) for m, p, _ in trade_client.requests])

    def test_fast_poll_delay_with_open_positions_and_skips_heavy_klines_midday(self):
        # 12h dopo chiusura giornaliera: nessun nuovo 1d chiuso
        raw = _make_trending_raw_klines()
        closed_open_ms = raw[-2][0]
        closed_end_ms = raw[-2][6]
        midday_now_ms = closed_end_ms + 12 * 3600_000
        pub = FakePublicClient(server_now_ms=midday_now_ms, raw_klines_by_sym={"BTCUSDT": raw})
        market = lt.LiveMarket(symbols=["BTCUSDT"], public_client=pub)
        market.refresh()

        conn = FakeLiveConn(status="ACTIVE", orders_enabled=True)
        conn.processed_candles[("BTCUSDT", "1d", closed_open_ms)] = ("BUY", D(raw[-2][4]))
        conn.live_positions["BTCUSDT"] = {
            "symbol": "BTCUSDT", "side": "LONG", "quantity": D("0.02"), "entry_price": D("90000"),
            "entry_client_order_id": "m_OL_BTC_1", "signal_reason": STRATEGY_REASON,
            "candle_open_time_ms": closed_open_ms, "stop_price": D("87000.0"),
            "trail_distance": D("4000"), "best_price": D("91000"),
            "stop_client_algo_id": "s_btc_1", "stop_algo_id": 777001, "stop_algo_status": "NEW",
            "risk_checks": {},
        }
        executor = OrderExecutor(FakeLiveTradeClient(), TsmomParams())
        processed = lt.process_cycle(conn, market, FakeReadClient(), executor)
        self.assertFalse(processed)
        self.assertEqual(pub.klines_calls, 0)

        delay = lt.next_live_delay_s(midday_now_ms, processed=False, has_open_positions=True, halted=False)
        self.assertEqual(delay, lt.FAST_POLL_S)
        delay_flat = lt.next_live_delay_s(midday_now_ms, processed=False, has_open_positions=False, halted=False)
        self.assertEqual(delay_flat, lt.MAX_POLL_SLEEP_S)

    def test_open_position_advances_trailing_stop_algo_on_favorable_candle(self):
        raw = _make_trending_raw_klines()
        prev_bar = raw[-3]
        raw[-2][2] = "260.0000"  # high avanza fortemente rispetto a best_price=240
        raw[-2][3] = "239.0000"  # low resta sopra stop_price=220
        raw[-2][4] = "255.0000"
        pub = FakePublicClient(server_now_ms=raw[-2][6] + 10_000, raw_klines_by_sym={"BTCUSDT": raw})
        market = lt.LiveMarket(symbols=["BTCUSDT"], public_client=pub)
        market.refresh()

        conn = FakeLiveConn(status="ACTIVE", orders_enabled=True)
        conn.processed_candles[("BTCUSDT", "1d", prev_bar[0])] = ("BUY", D(prev_bar[4]))
        conn.live_positions["BTCUSDT"] = {
            "symbol": "BTCUSDT", "side": "LONG", "quantity": D("1.0"), "entry_price": D("230"),
            "entry_client_order_id": "m_OL_BTC_1", "signal_reason": STRATEGY_REASON,
            "candle_open_time_ms": prev_bar[0], "stop_price": D("220.0"),
            "trail_distance": D("10.0"), "best_price": D("240.0"),
            "stop_client_algo_id": "s_btc_1", "stop_algo_id": 777001, "stop_algo_status": "NEW",
            "risk_checks": {},
        }
        trade_client = FakeLiveTradeClient()
        executor = OrderExecutor(trade_client, TsmomParams())

        processed = lt.process_cycle(conn, market, FakeReadClient(), executor)
        self.assertTrue(processed)
        pos = conn.live_positions["BTCUSDT"]
        # best_price aggiornato a 260 -> new stop = 260 - 10 = 250.0
        self.assertEqual(pos["best_price"], D("260.0"))
        self.assertEqual(pos["stop_price"], D("250.0"))
        self.assertNotEqual(pos["stop_algo_id"], 777001)
        methods_paths = [(m, p) for m, p, _ in trade_client.requests]
        self.assertIn(("DELETE", "/fapi/v1/algoOrder"), methods_paths)
        self.assertIn(("POST", "/fapi/v1/algoOrder"), methods_paths)


if __name__ == "__main__":
    unittest.main()
