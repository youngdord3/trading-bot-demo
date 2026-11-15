"""Test offline TDD per order_executor.py (firma, whitelist, gate DB, 5 fasi idempotenti e algo stop)."""
import hashlib
import hmac
import json
import unittest
from decimal import Decimal as D
from unittest.mock import patch

from binance_public import MarketFilters
from order_executor import (
    ALLOWED_ENDPOINTS,
    AmbiguousOrderError,
    OrderExecutor,
    OrderRejectedError,
    TradeAPIError,
    TradeClient,
    TradingDisabledError,
    make_client_algo_id,
    make_client_order_id,
    quantize_stop_price,
)
from risk_checks import Action, RiskLimits
from strategy import TsmomParams

BTC_FILTERS = MarketFilters(
    tick_size=D("0.1"),
    market_step_size=D("0.001"),
    market_min_qty=D("0.001"),
    market_max_qty=D("120"),
    min_notional=D("100"),
)
DEFAULT_LIMITS = RiskLimits(
    max_notional_usdt=D("2000"),
    max_open_positions=3,
    max_daily_loss_usdt=D("250"),
    enforce_btc_qty_cap=False,
)


class FakeCursor:
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

        if "SELECT id, client_order_id, exchange_order_id, symbol, candle_open_time_ms" in norm:
            # lookup by (symbol, candle_open_time_ms, action) or active list
            if "WHERE order_kind = 'MARKET' AND status IN" in norm:
                self._rows = [
                    self.db._order_tuple(o)
                    for o in self.db.orders
                    if o["order_kind"] == "MARKET" and o["status"] in ("PENDING", "NEW", "PARTIALLY_FILLED")
                ]
                return
            if "WHERE symbol = %s AND candle_open_time_ms = %s AND action = %s" in norm:
                sym, c_ms, act = params
                self._rows = [
                    self.db._order_tuple(o)
                    for o in self.db.orders
                    if o["symbol"] == sym
                    and o["candle_open_time_ms"] == c_ms
                    and o["action"] == act
                    and o["order_kind"] == "MARKET"
                    and o["status"] != "FAILED"
                ]
                return

        if norm.startswith("INSERT INTO bot.orders"):
            (
                client_order_id,
                exchange_order_id,
                symbol,
                candle_open_time_ms,
                order_kind,
                action,
                side,
                reduce_only,
                requested_qty,
                executed_qty,
                avg_fill_price,
                trigger_price,
                status,
                signal_reason,
                risk_checks,
            ) = params
            for existing in self.db.orders:
                if existing["client_order_id"] == client_order_id:
                    raise RuntimeError("UNIQUE_CLIENT_ORDER_ID")
                if (
                    order_kind == "MARKET"
                    and status != "FAILED"
                    and existing["order_kind"] == "MARKET"
                    and existing["status"] != "FAILED"
                    and existing["symbol"] == symbol
                    and existing["candle_open_time_ms"] == candle_open_time_ms
                    and existing["action"] == action
                ):
                    raise RuntimeError("UNIQUE_CANDLE_MARKET_ACTION")
            oid = len(self.db.orders) + 1
            self.db.orders.append({
                "id": oid,
                "client_order_id": client_order_id,
                "exchange_order_id": exchange_order_id,
                "symbol": symbol,
                "candle_open_time_ms": candle_open_time_ms,
                "order_kind": order_kind,
                "action": action,
                "side": side,
                "reduce_only": reduce_only,
                "requested_qty": D(str(requested_qty)),
                "executed_qty": D(str(executed_qty)),
                "avg_fill_price": D(str(avg_fill_price)) if avg_fill_price is not None else None,
                "trigger_price": D(str(trigger_price)) if trigger_price is not None else None,
                "status": status,
                "signal_reason": signal_reason,
                "risk_checks": risk_checks.obj if hasattr(risk_checks, "obj") else risk_checks,
                "error_code": None,
            })
            self._rows = [(oid,)]
            return

        if norm.startswith("UPDATE bot.orders SET"):
            status, ex_id, ex_qty, avg_p, err_code, client_order_id = params
            for o in self.db.orders:
                if o["client_order_id"] == client_order_id:
                    o["status"] = status
                    o["exchange_order_id"] = ex_id
                    o["executed_qty"] = D(str(ex_qty))
                    o["avg_fill_price"] = D(str(avg_p)) if avg_p is not None else None
                    o["error_code"] = err_code
            return

        if norm.startswith("SELECT ") and "FROM bot.live_positions WHERE symbol = %s" in norm:
            sym = params[0]
            pos = self.db.live_positions.get(sym)
            if pos is not None:
                self._rows = [(
                    pos["symbol"],
                    pos["side"],
                    pos["quantity"],
                    pos["entry_price"],
                    pos["entry_client_order_id"],
                    pos["signal_reason"],
                    pos["candle_open_time_ms"],
                    pos["stop_price"],
                    pos["trail_distance"],
                    pos["best_price"],
                    pos["stop_client_algo_id"],
                    pos["stop_algo_id"],
                    pos["stop_algo_status"],
                    pos["risk_checks"],
                )]
            return

        if norm.startswith("INSERT INTO bot.live_positions"):
            (
                sym,
                side,
                qty,
                entry_price,
                entry_cid,
                signal_reason,
                c_ms,
                stop_price,
                trail_distance,
                best_price,
                stop_caid,
                stop_aid,
                stop_astatus,
                risk_checks,
            ) = params
            if sym in self.db.live_positions:
                raise RuntimeError("DUPLICATE_LIVE_POSITION")
            self.db.live_positions[sym] = {
                "symbol": sym,
                "side": side,
                "quantity": D(str(qty)),
                "entry_price": D(str(entry_price)),
                "entry_client_order_id": entry_cid,
                "signal_reason": signal_reason,
                "candle_open_time_ms": c_ms,
                "stop_price": D(str(stop_price)),
                "trail_distance": D(str(trail_distance)),
                "best_price": D(str(best_price)),
                "stop_client_algo_id": stop_caid,
                "stop_algo_id": stop_aid,
                "stop_algo_status": stop_astatus,
                "risk_checks": risk_checks.obj if hasattr(risk_checks, "obj") else risk_checks,
            }
            return

        if norm.startswith("UPDATE bot.live_positions SET stop_price"):
            stop_price, stop_caid, stop_aid, stop_astatus, sym = params
            pos = self.db.live_positions[sym]
            pos["stop_price"] = D(str(stop_price))
            pos["stop_client_algo_id"] = stop_caid
            pos["stop_algo_id"] = stop_aid
            pos["stop_algo_status"] = stop_astatus
            return

        if norm.startswith("UPDATE bot.live_positions SET best_price"):
            best_price, stop_price, sym = params
            pos = self.db.live_positions[sym]
            pos["best_price"] = D(str(best_price))
            pos["stop_price"] = D(str(stop_price))
            return

        if norm.startswith("DELETE FROM bot.live_positions WHERE symbol = %s"):
            self.db.live_positions.pop(params[0], None)
            return

        if norm.startswith("INSERT INTO bot.audit_events"):
            self.db.audit_events.append((params[0], params[1].obj if hasattr(params[1], "obj") else params[1]))
            return

        raise AssertionError(f"Unexpected SQL in test: {norm}")

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class FakeConn:
    def __init__(self, status="ACTIVE", orders_enabled=True):
        self.status = status
        self.orders_enabled = orders_enabled
        self.orders = []
        self.live_positions = {}
        self.audit_events = []
        self.commits = 0
        self.commit_snapshots = []

    def _order_tuple(self, o):
        return (
            o["id"],
            o["client_order_id"],
            o["exchange_order_id"],
            o["symbol"],
            o["candle_open_time_ms"],
            o["action"],
            o["side"],
            o["reduce_only"],
            o["requested_qty"],
            o["executed_qty"],
            o["avg_fill_price"],
            o["status"],
            o["signal_reason"],
            o["risk_checks"],
        )

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1
        self.commit_snapshots.append([dict(o) for o in self.orders])

    def rollback(self):
        pass


class FakeTradeClient(TradeClient):
    def __init__(self):
        super().__init__(api_key="k" * 20, api_secret="s" * 32, time_offset_ms=0)
        self.calls = []
        self.on_request = None

    def request(self, method: str, path: str, params: dict | None = None):
        if (method, path) not in ALLOWED_ENDPOINTS:
            raise TradeAPIError("ENDPOINT_NOT_ALLOWED")
        self.calls.append((method, path, dict(params or {})))
        if self.on_request is not None:
            return self.on_request(method, path, dict(params or {}))
        raise AssertionError(f"Unhandled request: {method} {path}")


class TestTradeClientAndPurity(unittest.TestCase):
    def test_whitelist_and_host_and_secret_non_leak(self):
        with self.assertRaises(TradeAPIError) as host_ctx:
            TradeClient(host="fapi.binance.com", api_key="k" * 20, api_secret="s" * 32)
        self.assertEqual(host_ctx.exception.code, "HOST_NOT_ALLOWED")

        client = FakeTradeClient()
        with self.assertRaises(TradeAPIError) as ep_ctx:
            client.request("POST", "/sapi/v1/capital/withdraw/apply", {})
        self.assertEqual(ep_ctx.exception.code, "ENDPOINT_NOT_ALLOWED")

        # ID deterministici entro il limite Binance di 36 caratteri
        cid = make_client_order_id("BTCUSDT", 1_763_164_800_000, Action.OPEN_LONG)
        caid = make_client_algo_id("AVAXUSDT", 1_763_164_800_000, "init")
        self.assertLessEqual(len(cid), 36)
        self.assertLessEqual(len(caid), 36)
        self.assertEqual(cid, make_client_order_id("BTCUSDT", 1_763_164_800_000, Action.OPEN_LONG))

    def test_quantize_stop_price_defensive_rounding(self):
        self.assertEqual(quantize_stop_price("LONG", D("95123.49"), D("0.1")), D("95123.4"))
        self.assertEqual(quantize_stop_price("SHORT", D("95123.41"), D("0.1")), D("95123.5"))

    def test_signed_query_hmac_and_margin_type_idempotent(self):
        captured = {}

        class FakeResp:
            status = 400
            def read(self, _n):
                return json.dumps({"code": -4046, "msg": "No need to change margin type."}).encode("utf-8")

        class FakeHTTPS:
            def __init__(self, *_a, **_kw):
                pass
            def request(self, method, target, body=None, headers=None):
                captured["method"] = method
                captured["target"] = target
                captured["body"] = body.decode("ascii") if body else ""
                captured["headers"] = dict(headers or {})
            def getresponse(self):
                return FakeResp()
            def close(self):
                pass

        client = TradeClient(api_key="my_demo_api_key_1234", api_secret="my_super_secret_key_for_signing_test")
        client.set_time_offset_ms(29_780)
        with patch("order_executor.HTTPSConnection", FakeHTTPS):
            changed = client.set_margin_type("BTCUSDT", "ISOLATED")
        self.assertFalse(changed)
        self.assertEqual(captured["method"], "POST")
        self.assertIn("symbol=BTCUSDT", captured["body"])
        self.assertIn("marginType=ISOLATED", captured["body"])
        q, sig = captured["body"].rsplit("&signature=", 1)
        expected = hmac.new(b"my_super_secret_key_for_signing_test", q.encode("ascii"), hashlib.sha256).hexdigest()
        self.assertEqual(sig, expected)


class TestOrderExecutorFivePhase(unittest.TestCase):
    def test_gate_blocks_paused_and_halted_before_any_http(self):
        client = FakeTradeClient()
        ex = OrderExecutor(client, TsmomParams())

        for state in [("PAUSED", False), ("HALTED", False), ("ACTIVE", False)]:
            conn = FakeConn(status=state[0], orders_enabled=state[1])
            with self.assertRaises(TradingDisabledError):
                ex.execute_open_market(
                    conn=conn,
                    symbol="BTCUSDT",
                    candle_open_time_ms=1_763_164_800_000,
                    action=Action.OPEN_LONG,
                    qty=D("0.015"),
                    ref_price=D("90000"),
                    atr14=D("2000"),
                    filters=BTC_FILTERS,
                    risk_limits=DEFAULT_LIMITS,
                    open_positions=0,
                    daily_net_pnl_usdt=D("0"),
                    signal_reason="D1_TSMOM_ERF_TRAIL",
                )
        self.assertEqual(client.calls, [])

    def test_five_phase_open_commits_pending_before_post_and_places_stop_algo(self):
        conn = FakeConn(status="ACTIVE", orders_enabled=True)
        client = FakeTradeClient()
        seen_pending_before_post = []

        def handler(method, path, params):
            if (method, path) == ("POST", "/fapi/v1/order"):
                # Verifica invariante anti-fantasma: PENDING deve essere gia' committato nel DB
                self.assertGreaterEqual(conn.commits, 1)
                self.assertEqual(conn.commit_snapshots[0][0]["status"], "PENDING")
                seen_pending_before_post.append(True)
                return {
                    "orderId": 99001122,
                    "clientOrderId": params["newClientOrderId"],
                    "symbol": "BTCUSDT",
                    "status": "FILLED",
                    "executedQty": "0.015",
                    "avgPrice": "90100.0",
                    "cumQuote": "1351.5",
                }
            if (method, path) == ("POST", "/fapi/v1/algoOrder"):
                self.assertEqual(params["algoType"], "CONDITIONAL")
                self.assertEqual(params["type"], "STOP_MARKET")
                self.assertEqual(params["closePosition"], "true")
                self.assertNotIn("quantity", params)
                self.assertNotIn("reduceOnly", params)
                self.assertEqual(params["side"], "SELL")
                # 90100 - 1.5 * 2000 = 87100.0 (quantizzato tick 0.1)
                self.assertEqual(params["triggerPrice"], "87100")
                return {
                    "algoId": 777001,
                    "clientAlgoId": params["clientAlgoId"],
                    "algoStatus": "NEW",
                }
            raise AssertionError((method, path))

        client.on_request = handler
        ex = OrderExecutor(client, TsmomParams())
        res = ex.execute_open_market(
            conn=conn,
            symbol="BTCUSDT",
            candle_open_time_ms=1_763_164_800_000,
            action=Action.OPEN_LONG,
            qty=D("0.015"),
            ref_price=D("90000"),
            atr14=D("2000"),
            filters=BTC_FILTERS,
            risk_limits=DEFAULT_LIMITS,
            open_positions=0,
            daily_net_pnl_usdt=D("0"),
            signal_reason="D1_TSMOM_ERF_TRAIL",
        )

        self.assertEqual(seen_pending_before_post, [True])
        self.assertEqual(res["status"], "FILLED")
        self.assertEqual(conn.orders[0]["status"], "FILLED")
        self.assertEqual(conn.orders[0]["exchange_order_id"], 99001122)
        pos = conn.live_positions["BTCUSDT"]
        self.assertEqual(pos["side"], "LONG")
        self.assertEqual(pos["quantity"], D("0.015"))
        self.assertEqual(pos["entry_price"], D("90100.0"))
        self.assertEqual(pos["stop_price"], D("87100.0"))
        self.assertEqual(pos["stop_algo_id"], 777001)
        self.assertEqual(pos["stop_algo_status"], "NEW")

    def test_network_timeout_triggers_get_recovery_without_duplicate_order(self):
        conn = FakeConn(status="ACTIVE", orders_enabled=True)
        client = FakeTradeClient()
        post_count = 0

        def handler(method, path, params):
            nonlocal post_count
            if (method, path) == ("POST", "/fapi/v1/order"):
                post_count += 1
                raise TradeAPIError("TRANSPORT_ERROR")
            if (method, path) == ("GET", "/fapi/v1/order"):
                return {
                    "orderId": 880011,
                    "clientOrderId": params["origClientOrderId"],
                    "symbol": "BTCUSDT",
                    "status": "FILLED",
                    "executedQty": "0.015",
                    "avgPrice": "90050.0",
                }
            if (method, path) == ("POST", "/fapi/v1/algoOrder"):
                return {"algoId": 777002, "clientAlgoId": params["clientAlgoId"], "algoStatus": "NEW"}
            raise AssertionError((method, path))

        client.on_request = handler
        ex = OrderExecutor(client, TsmomParams())
        res = ex.execute_open_market(
            conn=conn,
            symbol="BTCUSDT",
            candle_open_time_ms=1_763_164_800_000,
            action=Action.OPEN_LONG,
            qty=D("0.015"),
            ref_price=D("90000"),
            atr14=D("2000"),
            filters=BTC_FILTERS,
            risk_limits=DEFAULT_LIMITS,
            open_positions=0,
            daily_net_pnl_usdt=D("0"),
            signal_reason="D1_TSMOM_ERF_TRAIL",
        )
        self.assertEqual(post_count, 1)
        self.assertEqual(res["status"], "FILLED")
        self.assertEqual(conn.live_positions["BTCUSDT"]["entry_price"], D("90050.0"))
        self.assertEqual(conn.live_positions["BTCUSDT"]["stop_algo_id"], 777002)

    def test_timeout_with_missing_order_marks_failed_cleanly_on_recovery(self):
        conn = FakeConn(status="ACTIVE", orders_enabled=True)
        client = FakeTradeClient()

        def handler(method, path, params):
            if (method, path) == ("POST", "/fapi/v1/order"):
                raise TradeAPIError("TRANSPORT_ERROR")
            if (method, path) == ("GET", "/fapi/v1/order"):
                return None  # -2013 Order does not exist
            raise AssertionError((method, path))

        client.on_request = handler
        ex = OrderExecutor(client, TsmomParams())
        with self.assertRaises(OrderRejectedError):
            ex.execute_open_market(
                conn=conn,
                symbol="BTCUSDT",
                candle_open_time_ms=1_763_164_800_000,
                action=Action.OPEN_LONG,
                qty=D("0.015"),
                ref_price=D("90000"),
                atr14=D("2000"),
                filters=BTC_FILTERS,
                risk_limits=DEFAULT_LIMITS,
                open_positions=0,
                daily_net_pnl_usdt=D("0"),
                signal_reason="D1_TSMOM_ERF_TRAIL",
            )
        self.assertEqual(conn.orders[0]["status"], "FAILED")
        self.assertEqual(conn.live_positions, {})

    def test_close_sends_reduce_only_cancels_stop_algo_and_deletes_position(self):
        conn = FakeConn(status="ACTIVE", orders_enabled=True)
        conn.live_positions["BTCUSDT"] = {
            "symbol": "BTCUSDT",
            "side": "LONG",
            "quantity": D("0.015"),
            "entry_price": D("90000"),
            "entry_client_order_id": "m_OL_BTCUSDT_1763164800000",
            "signal_reason": "D1_TSMOM_ERF_TRAIL",
            "candle_open_time_ms": 1_763_164_800_000,
            "stop_price": D("87000.0"),
            "trail_distance": D("4000"),
            "best_price": D("93000"),
            "stop_client_algo_id": "s_BTCUSDT_1763164800000_init",
            "stop_algo_id": 777001,
            "stop_algo_status": "NEW",
            "risk_checks": {},
        }
        client = FakeTradeClient()

        def handler(method, path, params):
            if (method, path) == ("GET", "/fapi/v1/algoOrder"):
                return {"algoId": 777001, "algoStatus": "NEW"}
            if (method, path) == ("POST", "/fapi/v1/order"):
                self.assertEqual(params["side"], "SELL")
                self.assertEqual(params["reduceOnly"], "true")
                self.assertEqual(params["quantity"], "0.015")
                return {
                    "orderId": 99001199,
                    "clientOrderId": params["newClientOrderId"],
                    "symbol": "BTCUSDT",
                    "status": "FILLED",
                    "executedQty": "0.015",
                    "avgPrice": "89000.0",
                }
            if (method, path) == ("DELETE", "/fapi/v1/algoOrder"):
                self.assertEqual(params["algoId"], 777001)
                return {"algoId": 777001, "algoStatus": "CANCELED"}
            raise AssertionError((method, path))

        client.on_request = handler
        ex = OrderExecutor(client, TsmomParams())
        res = ex.execute_close_market(
            conn=conn,
            symbol="BTCUSDT",
            candle_open_time_ms=1_763_251_200_000,
            action=Action.CLOSE_LONG,
            position_qty=D("0.015"),
            signal_reason="TRAIL_EXIT",
        )
        self.assertEqual(res["status"], "FILLED")
        self.assertEqual(conn.live_positions, {})
        self.assertEqual(conn.orders[-1]["reduce_only"], True)

    def test_replace_stop_algo_order_cancels_then_places_tightened_stop(self):
        conn = FakeConn(status="ACTIVE", orders_enabled=True)
        conn.live_positions["BTCUSDT"] = {
            "symbol": "BTCUSDT",
            "side": "LONG",
            "quantity": D("0.015"),
            "entry_price": D("90000"),
            "entry_client_order_id": "m_OL_BTCUSDT_1763164800000",
            "signal_reason": "D1_TSMOM_ERF_TRAIL",
            "candle_open_time_ms": 1_763_164_800_000,
            "stop_price": D("87000.0"),
            "trail_distance": D("4000"),
            "best_price": D("90000"),
            "stop_client_algo_id": "s_BTCUSDT_1763164800000_init",
            "stop_algo_id": 777001,
            "stop_algo_status": "NEW",
            "risk_checks": {},
        }
        client = FakeTradeClient()

        def handler(method, path, params):
            if (method, path) == ("GET", "/fapi/v1/algoOrder"):
                return {"algoId": 777001, "algoStatus": "NEW"}
            if (method, path) == ("DELETE", "/fapi/v1/algoOrder"):
                self.assertEqual(params["algoId"], 777001)
                return {"algoId": 777001, "algoStatus": "CANCELED"}
            if (method, path) == ("POST", "/fapi/v1/algoOrder"):
                self.assertEqual(params["triggerPrice"], "91000")
                self.assertEqual(params["closePosition"], "true")
                return {"algoId": 777009, "clientAlgoId": params["clientAlgoId"], "algoStatus": "NEW"}
            raise AssertionError((method, path))

        client.on_request = handler
        ex = OrderExecutor(client, TsmomParams())
        out = ex.replace_stop_algo_order(
            conn=conn,
            symbol="BTCUSDT",
            candle_open_time_ms=1_763_251_200_000,
            new_stop_price=D("91000.04"),
            new_best_price=D("95000.0"),
            filters=BTC_FILTERS,
        )
        self.assertTrue(out["replaced"])
        pos = conn.live_positions["BTCUSDT"]
        self.assertEqual(pos["stop_price"], D("91000.0"))
        self.assertEqual(pos["best_price"], D("95000.0"))
        self.assertEqual(pos["stop_algo_id"], 777009)

    def test_sync_triggered_algo_stop_records_fill_and_clears_position(self):
        conn = FakeConn(status="ACTIVE", orders_enabled=True)
        conn.live_positions["BTCUSDT"] = {
            "symbol": "BTCUSDT",
            "side": "LONG",
            "quantity": D("0.015"),
            "entry_price": D("90000"),
            "entry_client_order_id": "m_OL_BTCUSDT_1763164800000",
            "signal_reason": "D1_TSMOM_ERF_TRAIL",
            "candle_open_time_ms": 1_763_164_800_000,
            "stop_price": D("87000.0"),
            "trail_distance": D("4000"),
            "best_price": D("90000"),
            "stop_client_algo_id": "s_BTCUSDT_1763164800000_init",
            "stop_algo_id": 777001,
            "stop_algo_status": "NEW",
            "risk_checks": {},
        }
        client = FakeTradeClient()

        def handler(method, path, params):
            if (method, path) == ("GET", "/fapi/v1/algoOrder"):
                return {
                    "algoId": 777001,
                    "clientAlgoId": "s_BTCUSDT_1763164800000_init",
                    "algoStatus": "TRIGGERED",
                    "actualOrderId": "99005555",
                    "actualPrice": "86990.5",
                    "actualQty": "0.015",
                    "triggerPrice": "87000.0",
                }
            raise AssertionError((method, path))

        client.on_request = handler
        ex = OrderExecutor(client, TsmomParams())
        filled = ex.sync_triggered_algo_stop(conn, "BTCUSDT")
        self.assertIsNotNone(filled)
        self.assertEqual(filled["status"], "FILLED")
        self.assertEqual(filled["avg_fill_price"], D("86990.5"))
        self.assertEqual(conn.live_positions, {})
        self.assertEqual(conn.orders[-1]["order_kind"], "STOP_MARKET")
        self.assertEqual(conn.orders[-1]["reduce_only"], True)
        self.assertEqual(conn.orders[-1]["exchange_order_id"], 99005555)


if __name__ == "__main__":
    unittest.main()
