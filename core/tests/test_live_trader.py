"""Test offline TDD per live_trader.py (preflight 12 simboli, gate PAUSED/ACTIVE, cicli 1d e polling rapido)."""
import json
import unittest
from decimal import Decimal as D

from binance_public import MarketFilters
from order_executor import OrderExecutor, TradeClient, quantize_stop_price
from strategy import STRATEGY_REASON, Signal, TsmomParams, UNIVERSE_SYMBOLS, evaluate_tsmom_erf, parse_closed_candles
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


def _make_downtrend_raw_klines(n=221, start_open_ms=1_700_000_000_000, step_ms=86_400_000, wobble=0.0):
    rows = []
    price = 1000.0
    for i in range(n):
        ot = start_open_ms + i * step_ms
        ct = ot + step_ms - 1
        if i < n - 45:
            o = price
            c = price - 0.05
            h = o + 0.3
            l = c - 0.4
            price = c
        else:
            step = -3.0 + (wobble if (n - 12 <= i < n - 3 and i % 2 == 1) else 0.0)
            o = price
            c = price + step
            h = max(o, c) + 0.2
            l = min(o, c) - 0.2
            price = c
        rows.append([ot, f"{o:.4f}", f"{h:.4f}", f"{l:.4f}", f"{c:.4f}", "1000", ct])
    return rows


def _make_custom_uptrend_raw_klines(
    n=221,
    start_open_ms=1_700_000_000_000,
    step_ms=86_400_000,
    base_price=100.0,
    trend_step=3.0,
    wick=0.2,
    wobble=0.0,
):
    """Genera serie rialzista controllata per testare er10 diversi e ATR basso (cap 2.0*equity)."""
    rows = []
    price = base_price
    for i in range(n):
        ot = start_open_ms + i * step_ms
        ct = ot + step_ms - 1
        if i < n - 45:
            o = price
            c = price + 0.05
            h = c + wick
            l = o - wick
            price = c
        else:
            # Piccola oscillazione nella finestra finale (-11..-4) riduce ER10 mantenendo il breakout finale
            step = trend_step
            if wobble and (n - 11 <= i <= n - 4):
                step = trend_step - wobble if i % 2 == 0 else -(wobble * 0.6)
            elif i == n - 2 and wobble:
                step = trend_step * 1.15
            o = price
            c = price + step
            h = max(o, c) + wick
            l = min(o, c) - wick
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
    def __init__(self, equity="5816.60", dual=False, sym_configs=None, available_balance=None):
        self.equity = equity
        self.dual = dual
        self.available_balance = available_balance
        self.sym_configs = sym_configs if sym_configs is not None else [
            {"symbol": s, "marginType": "ISOLATED", "leverage": 7}
            for s in UNIVERSE_SYMBOLS
        ]
        self.sync_calls = 0

    def sync_time_offset(self):
        self.sync_calls += 1
        return 29780

    def get(self, path, params=None):
        if path == "/fapi/v3/account":
            out = {"totalMarginBalance": self.equity}
            if self.available_balance is not None:
                out["availableBalance"] = self.available_balance
            return out
        if path == "/fapi/v1/positionSide/dual":
            return {"dualSidePosition": self.dual}
        if path == "/fapi/v1/symbolConfig":
            return self.sym_configs
        raise AssertionError(path)


class FakeLiveTradeClient(TradeClient):
    def __init__(self, avg_price_by_symbol=None):
        super().__init__(api_key="k" * 20, api_secret="s" * 32, time_offset_ms=0)
        self.requests = []
        self.algo_status_by_id = {}
        self.avg_price_by_symbol = avg_price_by_symbol or {}

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
            avg = self.avg_price_by_symbol.get(
                p["symbol"],
                "90000.0" if p["symbol"] == "BTCUSDT" else "244.0",
            )
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
    def test_preflight_sets_only_non_matching_symbols_to_isolated_7x(self):
        sym_cfg = [{"symbol": s, "marginType": "ISOLATED", "leverage": 7} for s in UNIVERSE_SYMBOLS]
        sym_cfg[1]["marginType"] = "CROSSED"
        sym_cfg[1]["leverage"] = 20
        sym_cfg[5]["leverage"] = 10

        read_client = FakeReadClient(sym_configs=sym_cfg)
        trade_client = FakeLiveTradeClient()
        res = lt.ensure_account_preflight(read_client, trade_client, UNIVERSE_SYMBOLS, target_leverage=7)
        self.assertEqual(res["margin_updated"], [UNIVERSE_SYMBOLS[1]])
        self.assertEqual(res["leverage_updated"], [UNIVERSE_SYMBOLS[1], UNIVERSE_SYMBOLS[5]])

        # Leva 2x deve essere rifiutata se i limiti V2 prevedono fino a 3 * 2.0 * equity (6.0x)
        with self.assertRaises(lt.FatalError) as low_lev_ctx:
            lt.ensure_account_preflight(read_client, trade_client, UNIVERSE_SYMBOLS, target_leverage=2)
        self.assertIn("INSUFFICIENT_LEVERAGE_FOR_COMBINED_EXPOSURE", str(low_lev_ctx.exception))

        read_hedge = FakeReadClient(dual=True)
        with self.assertRaises(lt.FatalError):
            lt.ensure_account_preflight(read_hedge, trade_client, UNIVERSE_SYMBOLS, target_leverage=7)

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

    def test_active_cycle_sizes_from_real_equity_opens_position_and_respects_max_same_side_and_concurrent(self):
        raw_long = _make_trending_raw_klines()
        raw_short = _make_downtrend_raw_klines()
        last_closed_close_ms = raw_long[-2][6]
        syms = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"]
        # 4 segnali LONG identici -> MAX_SAME_SIDE_POSITIONS = 2 ne apre solo 2
        raw_map = {s: list(raw_long) for s in syms}
        pub = FakePublicClient(server_now_ms=last_closed_close_ms + 15_000, raw_klines_by_sym=raw_map)
        market = lt.LiveMarket(symbols=syms, public_client=pub)
        market.refresh()

        conn = FakeLiveConn(status="ACTIVE", orders_enabled=True)
        read_client = FakeReadClient(equity="5816.60")
        trade_client = FakeLiveTradeClient()
        executor = OrderExecutor(trade_client, TsmomParams())

        processed = lt.process_cycle(conn, market, read_client, executor)
        self.assertTrue(processed)
        self.assertEqual(len(conn.live_positions), 2)
        self.assertEqual(set(conn.live_positions.keys()), {"BTCUSDT", "ETHUSDT"})
        for pos in conn.live_positions.values():
            self.assertIsNotNone(pos["stop_algo_id"])
            self.assertEqual(pos["stop_algo_status"], "NEW")

        # Aggiungendo un segnale SHORT su SOLUSDT nella direzione opposta si arriva a max_open_positions = 3
        raw_map_mix = {
            "BTCUSDT": list(raw_long),
            "ETHUSDT": list(raw_long),
            "SOLUSDT": list(raw_short),
            "XRPUSDT": list(raw_short),
        }
        pub_mix = FakePublicClient(server_now_ms=last_closed_close_ms + 15_000, raw_klines_by_sym=raw_map_mix)
        market_mix = lt.LiveMarket(symbols=syms, public_client=pub_mix)
        market_mix.refresh()
        conn_mix = FakeLiveConn(status="ACTIVE", orders_enabled=True)
        processed_mix = lt.process_cycle(conn_mix, market_mix, FakeReadClient(equity="5816.60"), OrderExecutor(FakeLiveTradeClient(), TsmomParams()))
        self.assertTrue(processed_mix)
        self.assertEqual(len(conn_mix.live_positions), 3)
        self.assertEqual(set(conn_mix.live_positions.keys()), {"BTCUSDT", "ETHUSDT", "SOLUSDT"})

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

    def test_restart_persists_initial_stop_price_and_1R_for_profit_ratchet_never_widens(self):
        base_raw = _make_custom_uptrend_raw_klines(n=225, base_price=100.0, trend_step=3.0, wick=0.2)
        entry_close_str = base_raw[220][4]
        pub1 = FakePublicClient(
            server_now_ms=base_raw[220][6] + 10_000,
            raw_klines_by_sym={"ETHUSDT": base_raw[:222]},
        )
        m1 = lt.LiveMarket(symbols=["ETHUSDT"], public_client=pub1)
        m1.refresh()

        conn = FakeLiveConn(status="ACTIVE", orders_enabled=True)
        tc1 = FakeLiveTradeClient(avg_price_by_symbol={"ETHUSDT": entry_close_str})
        rc = FakeReadClient(equity="5816.60")
        self.assertTrue(lt.process_cycle(conn, m1, rc, OrderExecutor(tc1, TsmomParams())))

        pos = conn.live_positions["ETHUSDT"]
        entry = pos["entry_price"]
        init_stop = D(pos["risk_checks"]["initial_stop_price"])
        r1 = D(pos["risk_checks"]["initial_risk_distance"])
        trail_dist = pos["trail_distance"]
        self.assertGreater(r1, D("0"))
        self.assertEqual(r1, abs(entry - init_stop))
        self.assertEqual(pos["stop_price"], init_stop)

        # Barra 221: tocca +2.5R -> ratchet 0.80x; initial_stop_price e 1R restano immutabili
        best_25r = entry + D("2.5") * r1
        bar_221 = list(base_raw[221])
        bar_221[2] = f"{best_25r:.4f}"
        bar_221[3] = f"{entry + D('0.5'):.4f}"
        bar_221[4] = f"{entry + D('2.0') * r1:.4f}"
        raw_step2 = base_raw[:221] + [bar_221, base_raw[222]]
        pub2 = FakePublicClient(server_now_ms=bar_221[6] + 10_000, raw_klines_by_sym={"ETHUSDT": raw_step2})
        m2 = lt.LiveMarket(symbols=["ETHUSDT"], public_client=pub2)
        m2.refresh()
        self.assertTrue(lt.process_cycle(conn, m2, rc, OrderExecutor(tc1, TsmomParams())))

        expected_stop_25r = quantize_stop_price("LONG", best_25r - D("0.80") * trail_dist, ETH_F.tick_size)
        self.assertEqual(pos["stop_price"], expected_stop_25r)
        self.assertEqual(D(pos["risk_checks"]["initial_stop_price"]), init_stop)
        self.assertEqual(D(pos["risk_checks"]["initial_risk_distance"]), r1)

        # Simula RESTART completo (nuovo Market + nuovo OrderExecutor, solo DB persistito)
        # Barra 222: pullback con high < best_25r e low > stop -> lo stop NON deve allargarsi
        bar_222 = list(base_raw[222])
        bar_222[2] = f"{entry + D('1.5') * r1:.4f}"
        bar_222[3] = f"{expected_stop_25r + D('0.5'):.4f}"
        bar_222[4] = f"{entry + D('1.2') * r1:.4f}"
        raw_step3 = raw_step2[:-1] + [bar_222, base_raw[223]]
        pub3 = FakePublicClient(server_now_ms=bar_222[6] + 10_000, raw_klines_by_sym={"ETHUSDT": raw_step3})
        m3 = lt.LiveMarket(symbols=["ETHUSDT"], public_client=pub3)
        m3.refresh()
        tc2 = FakeLiveTradeClient(avg_price_by_symbol={"ETHUSDT": entry_close_str})
        lt.process_cycle(conn, m3, rc, OrderExecutor(tc2, TsmomParams()))
        self.assertEqual(conn.live_positions["ETHUSDT"]["stop_price"], expected_stop_25r)

        # Barra 223 post-restart: tocca +4.0R su 1R iniziale -> ratchet 0.60x
        best_40r = entry + D("4.0") * r1
        bar_223 = list(base_raw[223])
        bar_223[2] = f"{best_40r:.4f}"
        bar_223[3] = f"{expected_stop_25r + D('1.0'):.4f}"
        bar_223[4] = f"{entry + D('3.5') * r1:.4f}"
        raw_step4 = raw_step3[:-1] + [bar_223, base_raw[224]]
        pub4 = FakePublicClient(server_now_ms=bar_223[6] + 10_000, raw_klines_by_sym={"ETHUSDT": raw_step4})
        m4 = lt.LiveMarket(symbols=["ETHUSDT"], public_client=pub4)
        m4.refresh()
        lt.process_cycle(conn, m4, rc, OrderExecutor(tc2, TsmomParams()))
        expected_stop_40r = quantize_stop_price("LONG", best_40r - D("0.60") * trail_dist, ETH_F.tick_size)
        self.assertEqual(conn.live_positions["ETHUSDT"]["stop_price"], expected_stop_40r)
        self.assertGreater(expected_stop_40r, expected_stop_25r)

    def test_0000_utc_cycle_order_exits_first_updates_side_counts_then_sorted_er10_entries(self):
        raw_btc = _make_custom_uptrend_raw_klines(n=221, base_price=80000.0, trend_step=200.0, wick=100.0)
        raw_eth = _make_custom_uptrend_raw_klines(n=221, base_price=200.0, trend_step=2.0, wick=0.2)
        prev_bar = raw_btc[-3]
        # BTCUSDT viola lo stop LONG (87000) sulla candela chiusa 00:00 UTC -> deve chiudersi in Step 1
        raw_btc[-2][3] = "86000.0"
        raw_btc[-2][4] = "86500.0"
        raw_eth[-2][3] = "210.0"

        raw_sol = _make_custom_uptrend_raw_klines(n=221, base_price=100.0, trend_step=3.0, wick=0.2, wobble=1.8)
        raw_doge = _make_custom_uptrend_raw_klines(n=221, base_price=100.0, trend_step=3.0, wick=0.2, wobble=1.95)
        raw_avax = _make_custom_uptrend_raw_klines(n=221, base_price=100.0, trend_step=3.0, wick=0.2, wobble=0.0)

        now_ms = raw_btc[-2][6] + 10_000
        # Verifica prerequisito: tutti e 3 i candidati flat danno BUY con avax.er10 > sol.er10
        ev_avax = evaluate_tsmom_erf(parse_closed_candles(raw_avax, now_ms, 86_400_000), [], TsmomParams())
        ev_sol = evaluate_tsmom_erf(parse_closed_candles(raw_sol, now_ms, 86_400_000), [], TsmomParams())
        ev_doge = evaluate_tsmom_erf(parse_closed_candles(raw_doge, now_ms, 86_400_000), [], TsmomParams())
        self.assertEqual((ev_avax.signal, ev_sol.signal, ev_doge.signal), (Signal.BUY, Signal.BUY, Signal.BUY))
        self.assertGreater(ev_avax.er10, ev_sol.er10)

        syms = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "DOGEUSDT", "AVAXUSDT"]
        pub = FakePublicClient(
            server_now_ms=now_ms,
            raw_klines_by_sym={
                "BTCUSDT": raw_btc, "ETHUSDT": raw_eth, "SOLUSDT": raw_sol,
                "DOGEUSDT": raw_doge, "AVAXUSDT": raw_avax,
            },
        )
        market = lt.LiveMarket(symbols=syms, public_client=pub)
        market.refresh()

        conn = FakeLiveConn(status="ACTIVE", orders_enabled=True)
        for s, pb in (("BTCUSDT", raw_btc[-3]), ("ETHUSDT", raw_eth[-3])):
            conn.processed_candles[(s, "1d", pb[0])] = ("BUY", D(pb[4]))
        conn.live_positions["BTCUSDT"] = {
            "symbol": "BTCUSDT", "side": "LONG", "quantity": D("0.02"), "entry_price": D("89000"),
            "entry_client_order_id": "m_OL_BTC_1", "signal_reason": STRATEGY_REASON,
            "candle_open_time_ms": prev_bar[0], "stop_price": D("87000.0"),
            "trail_distance": D("2000"), "best_price": D("90000"),
            "stop_client_algo_id": "s_btc_1", "stop_algo_id": 777001, "stop_algo_status": "NEW",
            "risk_checks": {"initial_stop_price": "87000", "initial_risk_distance": "2000"},
        }
        conn.live_positions["ETHUSDT"] = {
            "symbol": "ETHUSDT", "side": "LONG", "quantity": D("1.0"), "entry_price": D("210"),
            "entry_client_order_id": "m_OL_ETH_1", "signal_reason": STRATEGY_REASON,
            "candle_open_time_ms": prev_bar[0], "stop_price": D("200.0"),
            "trail_distance": D("10"), "best_price": D("220"),
            "stop_client_algo_id": "s_eth_1", "stop_algo_id": 777002, "stop_algo_status": "NEW",
            "risk_checks": {"initial_stop_price": "204", "initial_risk_distance": "6"},
        }

        tc = FakeLiveTradeClient()
        processed = lt.process_cycle(conn, market, FakeReadClient(), OrderExecutor(tc, TsmomParams()))
        self.assertTrue(processed)

        # Ordine mercato #1 = CLOSE_LONG su BTCUSDT (uscite prima); poi #2 = OPEN_LONG su AVAXUSDT (er10 massimo)
        market_orders = [(o["action"], o["symbol"]) for o in conn.orders if o["order_kind"] == "MARKET"]
        self.assertEqual(market_orders[0], ("CLOSE_LONG", "BTCUSDT"))
        self.assertEqual(market_orders[1], ("OPEN_LONG", "AVAXUSDT"))
        self.assertEqual(len(market_orders), 2)
        # Contatori aggiornati: ETHUSDT + AVAXUSDT -> MAX_SAME_SIDE_POSITIONS = 2 blocca SOL/DOGE
        self.assertEqual(set(conn.live_positions.keys()), {"ETHUSDT", "AVAXUSDT"})

        # Verifica tie-breaker su sym_idx a parita' esatta di er10
        raw_tie = _make_custom_uptrend_raw_klines(n=221, base_price=100.0, trend_step=3.0, wick=0.2, wobble=0.0)
        syms_tie = ["XRPUSDT", "LINKUSDT", "DOTUSDT"]
        pub_tie = FakePublicClient(
            server_now_ms=raw_tie[-2][6] + 10_000,
            raw_klines_by_sym={s: list(raw_tie) for s in syms_tie},
        )
        m_tie = lt.LiveMarket(symbols=syms_tie, public_client=pub_tie)
        m_tie.refresh()
        conn_tie = FakeLiveConn(status="ACTIVE", orders_enabled=True)
        lt.process_cycle(conn_tie, m_tie, FakeReadClient(), OrderExecutor(FakeLiveTradeClient(), TsmomParams()))
        self.assertEqual(list(conn_tie.live_positions.keys()), ["XRPUSDT", "LINKUSDT"])

    def test_leverage_supports_combined_exposure_with_2x_equity_cap_without_margin_errors(self):
        # ATR bassissimo (<0.5% stop) forza tutte e 3 le posizioni sul cap dinamico 2.0*equity ciascuna (~6.0*equity totali)
        raw_btc = _make_custom_uptrend_raw_klines(n=221, base_price=1000.0, trend_step=3.0, wick=0.1)
        raw_eth = _make_custom_uptrend_raw_klines(n=221, base_price=1000.0, trend_step=3.0, wick=0.1)
        # Short da 1300 -> 1165 con step -3.0 e wick 0.1: 1.5*ATR/close ~ 0.41% < 0.5%
        raw_sol = []
        p = 1300.0
        for i in range(221):
            ot = 1_700_000_000_000 + i * 86_400_000
            ct = ot + 86_400_000 - 1
            step = -0.05 if i < 221 - 45 else -3.0
            o, c = p, p + step
            raw_sol.append([ot, f"{o:.4f}", f"{max(o, c) + 0.1:.4f}", f"{min(o, c) - 0.1:.4f}", f"{c:.4f}", "1000", ct])
            p = c

        equity = D("5816.60")
        syms = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
        pub = FakePublicClient(
            server_now_ms=raw_btc[-2][6] + 10_000,
            raw_klines_by_sym={"BTCUSDT": raw_btc, "ETHUSDT": raw_eth, "SOLUSDT": raw_sol},
        )
        market = lt.LiveMarket(symbols=syms, public_client=pub)
        market.refresh()

        conn = FakeLiveConn(status="ACTIVE", orders_enabled=True)
        read_client = FakeReadClient(equity=str(equity), available_balance=str(equity))
        tc = FakeLiveTradeClient(avg_price_by_symbol={
            "BTCUSDT": raw_btc[-2][4],
            "ETHUSDT": raw_eth[-2][4],
            "SOLUSDT": raw_sol[-2][4],
        })
        processed = lt.process_cycle(conn, market, read_client, OrderExecutor(tc, TsmomParams()))
        self.assertTrue(processed)
        self.assertEqual(len(conn.live_positions), 3)

        combined_notional = D("0")
        for sym, pos in conn.live_positions.items():
            notional = pos["quantity"] * pos["entry_price"]
            combined_notional += notional
            # Cap dinamico 2.0 * equity (supera ampiamente il vecchio tetto statico 2000 USDT)
            self.assertGreater(notional, D("11000"))
            self.assertLessEqual(notional, equity * D("2.0"))
        self.assertGreater(combined_notional, equity * D("5.8"))
        # A 7x il margine combinato resta entro il saldo disponibile senza errori di margine
        self.assertLess(combined_notional / D(lt.TARGET_LEVERAGE), equity)

        # Verifica protezione margine disponibile residuo insufficiente
        conn_low_margin = FakeLiveConn(status="ACTIVE", orders_enabled=True)
        read_low = FakeReadClient(equity=str(equity), available_balance="400.00")
        lt.process_cycle(conn_low_margin, market, read_low, OrderExecutor(FakeLiveTradeClient(), TsmomParams()))
        self.assertEqual(conn_low_margin.live_positions, {})

        # Stop molto ampio (~19% da ombre ribassiste ampie) riduce la leva isolata sotto 7x e la ripristina a chiusura
        raw_hi_vol = []
        hp = 100.0
        for i in range(222):
            ot = 1_700_000_000_000 + i * 86_400_000
            ct = ot + 86_400_000 - 1
            if i < 180:
                o, c, h, l = hp, hp + 0.1, hp + 0.3, hp - 0.1
            else:
                o = hp
                c = hp * 1.03
                h = c * 1.001
                l = o * 0.87
            raw_hi_vol.append([ot, f"{o:.4f}", f"{h:.4f}", f"{l:.4f}", f"{c:.4f}", "1000", ct])
            hp = c
        pub_hv = FakePublicClient(
            server_now_ms=raw_hi_vol[220][6] + 10_000,
            raw_klines_by_sym={"SOLUSDT": raw_hi_vol[:222]},
        )
        m_hv = lt.LiveMarket(symbols=["SOLUSDT"], public_client=pub_hv)
        m_hv.refresh()
        conn_hv = FakeLiveConn(status="ACTIVE", orders_enabled=True)
        tc_hv = FakeLiveTradeClient(avg_price_by_symbol={"SOLUSDT": raw_hi_vol[220][4]})
        lt.process_cycle(conn_hv, m_hv, FakeReadClient(equity="5816.60"), OrderExecutor(tc_hv, TsmomParams()))
        sol_pos = conn_hv.live_positions["SOLUSDT"]
        self.assertLess(sol_pos["risk_checks"]["leverage"], lt.TARGET_LEVERAGE)
        lev_calls = [int(p["leverage"]) for _m, path, p in tc_hv.requests if path == "/fapi/v1/leverage"]
        self.assertIn(sol_pos["risk_checks"]["leverage"], lev_calls)

        # Chiusura successiva ripristina la leva standard TARGET_LEVERAGE (7)
        exit_bar = [
            raw_hi_vol[221][0], raw_hi_vol[221][1], raw_hi_vol[221][2],
            f"{sol_pos['stop_price'] - D('5'):.4f}", f"{sol_pos['stop_price'] - D('2'):.4f}",
            "1000", raw_hi_vol[221][6],
        ]
        open_next = [exit_bar[0] + 86_400_000, "10", "10", "10", "10", "1000", exit_bar[6] + 86_400_000]
        pub_hv2 = FakePublicClient(
            server_now_ms=exit_bar[6] + 10_000,
            raw_klines_by_sym={"SOLUSDT": raw_hi_vol[:221] + [exit_bar, open_next]},
        )
        m_hv2 = lt.LiveMarket(symbols=["SOLUSDT"], public_client=pub_hv2)
        m_hv2.refresh()
        lt.process_cycle(conn_hv, m_hv2, FakeReadClient(equity="5816.60"), OrderExecutor(tc_hv, TsmomParams()))
        self.assertEqual(conn_hv.live_positions, {})
        self.assertEqual(int([p["leverage"] for _m, path, p in tc_hv.requests if path == "/fapi/v1/leverage"][-1]), lt.TARGET_LEVERAGE)


if __name__ == "__main__":
    unittest.main()
