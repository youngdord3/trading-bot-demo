"""Esecutore ordini Binance USDS-M Futures Testnet/Demo con protocollo idempotente a 5 fasi e Stop Algo."""
import hashlib
import json
import os
import re
import time
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from http.client import HTTPException, HTTPSConnection

from binance_public import DEMO_HOST, MarketFilters
from common import build_signed_query, emit, parse_binance_json, read_secret, tls_context
from risk_checks import (
    Action,
    CLOSING,
    ORDER_SIDE,
    RiskLimits,
    RiskViolation,
    validate_close,
    validate_open,
)
from strategy import TsmomParams, initial_trailing_state

ALLOWED_HOSTS = frozenset({DEMO_HOST})
ALLOWED_ENDPOINTS = frozenset({
    ("POST", "/fapi/v1/order"),
    ("GET", "/fapi/v1/order"),
    ("DELETE", "/fapi/v1/order"),
    ("POST", "/fapi/v1/algoOrder"),
    ("GET", "/fapi/v1/algoOrder"),
    ("DELETE", "/fapi/v1/algoOrder"),
    ("GET", "/fapi/v1/openAlgoOrders"),
    ("POST", "/fapi/v1/marginType"),
    ("POST", "/fapi/v1/leverage"),
    ("GET", "/fapi/v1/userTrades"),
})
TIME_PATH = "/fapi/v1/time"
TIMEOUT_S = 10
RECV_WINDOW_MS = 5000
MAX_RESPONSE_BYTES = 1_000_000
USER_AGENT = "TradingCore-Demo-Executor/0.1"
ZERO = Decimal("0")
Q8 = Decimal("0.00000001")

_SHORT_ACTION = {
    Action.OPEN_LONG: "OL",
    Action.CLOSE_LONG: "CL",
    Action.OPEN_SHORT: "OS",
    Action.CLOSE_SHORT: "CS",
}
_MISSING_BINANCE_CODES = frozenset({-2011, -2013})


class TradeAPIError(Exception):
    def __init__(self, code: str, binance_code=None):
        if binance_code is None:
            m = re.search(r"_CODE_(-?\d+)$", str(code))
            if m:
                binance_code = int(m.group(1))
        super().__init__(code if binance_code is None else f"{code}_BINANCE_{binance_code}")
        self.code = code
        self.binance_code = binance_code


class TradingDisabledError(Exception):
    pass


class OrderRejectedError(Exception):
    pass


class AmbiguousOrderError(Exception):
    pass


def format_decimal(value: Decimal) -> str:
    """Formatta Decimal senza notazione scientifica o zeri decimali superflui."""
    d = Decimal(value).normalize()
    if d == ZERO:
        return "0"
    return format(d, "f")


def quantize_stop_price(side: str, stop_price: Decimal, tick_size: Decimal) -> Decimal:
    """Quantizza triggerPrice sul tick_size in direzione difensiva."""
    if side not in ("LONG", "SHORT"):
        raise RiskViolation("INVALID_POSITION_SIDE")
    if tick_size <= 0 or stop_price <= 0:
        raise RiskViolation("INVALID_STOP_OR_TICK")
    norm_tick = tick_size.normalize()
    rounding = ROUND_DOWN if side == "LONG" else ROUND_UP
    q = (stop_price / tick_size).to_integral_value(rounding=rounding) * norm_tick
    if q <= 0:
        raise RiskViolation("INVALID_QUANTIZED_STOP")
    return q


def make_client_order_id(symbol: str, candle_open_time_ms: int, action: Action) -> str:
    short = _SHORT_ACTION[action]
    candidate = f"m_{short}_{symbol}_{candle_open_time_ms}"
    if len(candidate) <= 36:
        return candidate
    digest = hashlib.sha256(candidate.encode("ascii")).hexdigest()[:16]
    return f"m_{short}_{digest}"


def make_client_algo_id(symbol: str, candle_open_time_ms: int, tag: str = "init") -> str:
    safe_tag = re.sub(r"[^A-Za-z0-9_-]", "", tag)[:6] or "s"
    candidate = f"s_{symbol}_{candle_open_time_ms}_{safe_tag}"
    if len(candidate) <= 36:
        return candidate
    digest = hashlib.sha256(f"{symbol}:{candle_open_time_ms}:{tag}".encode("ascii")).hexdigest()[:18]
    return f"s_{safe_tag}_{digest}"


def extract_market_fill(resp: dict) -> dict:
    """Normalizza la risposta Binance di /fapi/v1/order sullo stato DB consentito."""
    raw_status = str(resp.get("status", "NEW")).upper()
    if raw_status in ("REJECTED", "EXPIRED", "EXPIRED_IN_MATCH"):
        status = "FAILED"
    elif raw_status in ("PENDING", "NEW", "PARTIALLY_FILLED", "FILLED", "CANCELED", "FAILED"):
        status = raw_status
    else:
        status = "NEW"

    order_id = resp.get("orderId")
    exchange_order_id = int(order_id) if order_id is not None else None
    executed_qty = Decimal(str(resp.get("executedQty") or "0"))
    raw_avg = Decimal(str(resp.get("avgPrice") or "0"))
    raw_quote = Decimal(str(resp.get("cumQuote") or "0"))
    if raw_avg > ZERO:
        avg_fill_price = raw_avg
    elif executed_qty > ZERO and raw_quote > ZERO:
        avg_fill_price = (raw_quote / executed_qty).quantize(Q8)
    else:
        avg_fill_price = None

    return {
        "status": status,
        "exchange_order_id": exchange_order_id,
        "executed_qty": executed_qty,
        "avg_fill_price": avg_fill_price,
    }


def extract_algo_info(resp: dict) -> dict:
    algo_id = resp.get("algoId")
    actual_oid = resp.get("actualOrderId")
    actual_price_raw = resp.get("actualPrice")
    trigger_raw = resp.get("triggerPrice")
    return {
        "algo_id": int(algo_id) if algo_id is not None else None,
        "client_algo_id": resp.get("clientAlgoId"),
        "algo_status": str(resp.get("algoStatus") or "").upper() or None,
        "actual_order_id": int(actual_oid) if actual_oid not in (None, "", "0", 0) else None,
        "actual_price": Decimal(str(actual_price_raw)) if actual_price_raw not in (None, "", "0", 0) else None,
        "actual_qty": Decimal(str(resp.get("actualQty") or "0")),
        "trigger_price": Decimal(str(trigger_raw)) if trigger_raw not in (None, "", "0", 0) else None,
    }


def _to_json(data: dict | None) -> str:
    return json.dumps(data if data is not None else {})


def _is_ambiguous_api_error(err: TradeAPIError) -> bool:
    if err.code in ("TRANSPORT_ERROR", "INVALID_JSON", "RESPONSE_TOO_LARGE", "BODY_TOO_LARGE"):
        return True
    m = re.search(r"HTTP_(\d{3})", err.code)
    if m:
        status = int(m.group(1))
        return status >= 500 or status == 429
    return False


class TradeClient:
    """Client HTTPS firmato HMAC-SHA256 con whitelist stretta di endpoint e host Demo."""

    def __init__(
        self,
        host: str = DEMO_HOST,
        api_key: str | None = None,
        api_secret: str | bytes | None = None,
        time_offset_ms: int = 0,
    ):
        if host not in ALLOWED_HOSTS:
            raise TradeAPIError("HOST_NOT_ALLOWED")
        self.host = host
        self._ctx = tls_context()
        self._time_offset_ms = int(time_offset_ms)

        if api_key is None:
            key_secret_name = os.environ.get("TRADE_API_KEY_SECRET", "binance_api_key")
            api_key = read_secret(key_secret_name, min_len=16)
        if api_secret is None:
            sec_secret_name = os.environ.get("TRADE_API_SECRET_SECRET", "binance_api_secret")
            api_secret = read_secret(sec_secret_name, min_len=16)

        self._api_key = api_key
        self._api_secret = api_secret.encode("utf-8") if isinstance(api_secret, str) else api_secret

    def set_time_offset_ms(self, offset_ms: int) -> None:
        self._time_offset_ms = int(offset_ms)

    def sync_time_offset(self) -> int:
        conn = HTTPSConnection(self.host, 443, timeout=TIMEOUT_S, context=self._ctx)
        try:
            t0 = int(time.time() * 1000)
            conn.request("GET", TIME_PATH, headers={"Accept": "application/json", "User-Agent": USER_AGENT})
            resp = conn.getresponse()
            raw = resp.read(65_536)
            t1 = int(time.time() * 1000)
        except (OSError, TimeoutError, HTTPException) as exc:
            raise TradeAPIError("TRANSPORT_ERROR") from exc
        finally:
            conn.close()

        data = parse_binance_json(resp.status, raw, 65_536, TradeAPIError)
        server_ms = int(data["serverTime"])
        offset = server_ms - ((t0 + t1) // 2)
        self.set_time_offset_ms(offset)
        return offset

    def request(self, method: str, path: str, params: dict | None = None):
        method = method.upper()
        if (method, path) not in ALLOWED_ENDPOINTS:
            raise TradeAPIError("ENDPOINT_NOT_ALLOWED")

        now_ms = int(time.time() * 1000) + self._time_offset_ms
        query = build_signed_query(self._api_secret, params, now_ms=now_ms, recv_window_ms=RECV_WINDOW_MS)
        headers = {
            "Accept": "application/json",
            "X-MBX-APIKEY": self._api_key,
            "User-Agent": USER_AGENT,
        }
        if method == "POST":
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            target = path
            body = query.encode("ascii")
        else:
            target = f"{path}?{query}"
            body = None

        conn = HTTPSConnection(self.host, 443, timeout=TIMEOUT_S, context=self._ctx)
        try:
            conn.request(method, target, body=body, headers=headers)
            resp = conn.getresponse()
            raw = resp.read(MAX_RESPONSE_BYTES + 1)
        except (OSError, TimeoutError, HTTPException) as exc:
            raise TradeAPIError("TRANSPORT_ERROR") from exc
        finally:
            conn.close()

        return parse_binance_json(resp.status, raw, MAX_RESPONSE_BYTES, TradeAPIError)

    def set_margin_type(self, symbol: str, margin_type: str = "ISOLATED") -> bool:
        try:
            self.request("POST", "/fapi/v1/marginType", {"symbol": symbol, "marginType": margin_type})
            return True
        except TradeAPIError as exc:
            if exc.binance_code == -4046:
                return False
            raise

    def set_leverage(self, symbol: str, leverage: int = 2) -> dict:
        return self.request("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": int(leverage)})

    def place_market_order(
        self,
        symbol: str,
        side: str,
        quantity: Decimal,
        client_order_id: str,
        reduce_only: bool = False,
    ) -> dict:
        params = {
            "symbol": symbol,
            "side": side,
            "type": "MARKET",
            "quantity": format_decimal(quantity),
            "newClientOrderId": client_order_id,
            "newOrderRespType": "RESULT",
        }
        if reduce_only:
            params["reduceOnly"] = "true"
        return self.request("POST", "/fapi/v1/order", params)

    def get_order(
        self,
        symbol: str,
        client_order_id: str | None = None,
        order_id: int | None = None,
    ) -> dict | None:
        params: dict = {"symbol": symbol}
        if order_id is not None:
            params["orderId"] = int(order_id)
        elif client_order_id is not None:
            params["origClientOrderId"] = client_order_id
        else:
            raise ValueError("order_id or client_order_id required")
        try:
            return self.request("GET", "/fapi/v1/order", params)
        except TradeAPIError as exc:
            if exc.binance_code == -2013:
                return None
            raise

    def cancel_order(
        self,
        symbol: str,
        client_order_id: str | None = None,
        order_id: int | None = None,
    ) -> dict:
        params: dict = {"symbol": symbol}
        if order_id is not None:
            params["orderId"] = int(order_id)
        elif client_order_id is not None:
            params["origClientOrderId"] = client_order_id
        return self.request("DELETE", "/fapi/v1/order", params)

    def place_stop_algo_order(
        self,
        symbol: str,
        side: str,
        trigger_price: Decimal,
        client_algo_id: str,
        working_type: str = "MARK_PRICE",
    ) -> dict:
        return self.request("POST", "/fapi/v1/algoOrder", {
            "symbol": symbol,
            "side": side,
            "algoType": "CONDITIONAL",
            "type": "STOP_MARKET",
            "triggerPrice": format_decimal(trigger_price),
            "closePosition": "true",
            "workingType": working_type,
            "clientAlgoId": client_algo_id,
        })

    def get_algo_order(
        self,
        symbol: str,
        algo_id: int | None = None,
        client_algo_id: str | None = None,
    ) -> dict | None:
        params: dict = {"symbol": symbol}
        if algo_id is not None:
            params["algoId"] = int(algo_id)
        elif client_algo_id is not None:
            params["clientAlgoId"] = client_algo_id
        else:
            raise ValueError("algo_id or client_algo_id required")
        try:
            return self.request("GET", "/fapi/v1/algoOrder", params)
        except TradeAPIError as exc:
            if exc.binance_code in _MISSING_BINANCE_CODES:
                return None
            raise

    def cancel_algo_order(
        self,
        symbol: str,
        algo_id: int | None = None,
        client_algo_id: str | None = None,
    ) -> dict:
        params: dict = {"symbol": symbol}
        if algo_id is not None:
            params["algoId"] = int(algo_id)
        elif client_algo_id is not None:
            params["clientAlgoId"] = client_algo_id
        else:
            raise ValueError("algo_id or client_algo_id required")
        return self.request("DELETE", "/fapi/v1/algoOrder", params)


class OrderExecutor:
    """Esecutore ordini idempotente a 5 fasi + gestione protective STOP_MARKET Algo."""

    def __init__(self, client: TradeClient, strategy_params: TsmomParams | None = None):
        self.client = client
        self.params = strategy_params or TsmomParams()

    def assert_trading_enabled(self, conn) -> tuple[str, bool]:
        with conn.cursor() as cur:
            cur.execute("SELECT status, orders_enabled FROM bot.system_state WHERE id = 1")
            row = cur.fetchone()
        if row is None:
            raise TradingDisabledError("SYSTEM_STATE_MISSING")
        status, orders_enabled = row[0], bool(row[1])
        if status != "ACTIVE" or not orders_enabled:
            raise TradingDisabledError(f"TRADING_DISABLED_{status}_{orders_enabled}")
        return status, orders_enabled

    def _fetch_position(self, conn, symbol: str) -> dict | None:
        with conn.cursor() as cur:
            cur.execute("""SELECT symbol, side, quantity, entry_price, entry_client_order_id,
                                  signal_reason, candle_open_time_ms, stop_price,
                                  trail_distance, best_price, stop_client_algo_id,
                                  stop_algo_id, stop_algo_status, risk_checks
                           FROM bot.live_positions
                           WHERE symbol = %s""", (symbol,))
            row = cur.fetchone()
        if row is None:
            return None
        rc = row[13] if isinstance(row[13], dict) else (json.loads(row[13]) if row[13] else {})
        return {
            "symbol": row[0],
            "side": row[1],
            "quantity": Decimal(str(row[2])),
            "entry_price": Decimal(str(row[3])),
            "entry_client_order_id": row[4],
            "signal_reason": row[5],
            "candle_open_time_ms": int(row[6]),
            "stop_price": Decimal(str(row[7])),
            "trail_distance": Decimal(str(row[8])),
            "best_price": Decimal(str(row[9])),
            "stop_client_algo_id": row[10],
            "stop_algo_id": int(row[11]) if row[11] is not None else None,
            "stop_algo_status": row[12],
            "risk_checks": rc,
        }

    def _find_existing_market_order(self, conn, symbol: str, candle_open_time_ms: int, action: Action) -> dict | None:
        with conn.cursor() as cur:
            cur.execute("""SELECT id, client_order_id, exchange_order_id, symbol, candle_open_time_ms,
                                  action, side, reduce_only, requested_qty, executed_qty,
                                  avg_fill_price, status, signal_reason, risk_checks
                           FROM bot.orders
                           WHERE symbol = %s AND candle_open_time_ms = %s AND action = %s
                             AND order_kind = 'MARKET' AND status <> 'FAILED'""",
                        (symbol, candle_open_time_ms, action.value))
            row = cur.fetchone()
        if row is None:
            return None
        return self._row_to_order(row)

    def _row_to_order(self, row) -> dict:
        rc = row[13] if isinstance(row[13], dict) else (json.loads(row[13]) if row[13] else {})
        return {
            "id": int(row[0]),
            "client_order_id": row[1],
            "exchange_order_id": int(row[2]) if row[2] is not None else None,
            "symbol": row[3],
            "candle_open_time_ms": int(row[4]),
            "action": row[5],
            "side": row[6],
            "reduce_only": bool(row[7]),
            "requested_qty": Decimal(str(row[8])),
            "executed_qty": Decimal(str(row[9])),
            "avg_fill_price": Decimal(str(row[10])) if row[10] is not None else None,
            "status": row[11],
            "signal_reason": row[12],
            "risk_checks": rc,
        }

    def _insert_order_row(
        self,
        conn,
        *,
        client_order_id: str,
        exchange_order_id: int | None,
        symbol: str,
        candle_open_time_ms: int,
        order_kind: str,
        action: Action,
        reduce_only: bool,
        requested_qty: Decimal,
        executed_qty: Decimal,
        avg_fill_price: Decimal | None,
        trigger_price: Decimal | None,
        status: str,
        signal_reason: str,
        risk_checks: dict,
    ) -> None:
        side = ORDER_SIDE[action]
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO bot.orders (
                               client_order_id, exchange_order_id, symbol, candle_open_time_ms,
                               order_kind, action, side, reduce_only, requested_qty, executed_qty,
                               avg_fill_price, trigger_price, status, signal_reason, risk_checks
                           ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)""",
                        (
                            client_order_id,
                            exchange_order_id,
                            symbol,
                            candle_open_time_ms,
                            order_kind,
                            action.value,
                            side,
                            reduce_only,
                            requested_qty,
                            executed_qty,
                            avg_fill_price,
                            trigger_price,
                            status,
                            signal_reason,
                            _to_json(risk_checks),
                        ))

    def _update_order_status(
        self,
        conn,
        client_order_id: str,
        *,
        status: str,
        exchange_order_id: int | None = None,
        executed_qty: Decimal = ZERO,
        avg_fill_price: Decimal | None = None,
        error_code: str | None = None,
    ) -> None:
        with conn.cursor() as cur:
            cur.execute("""UPDATE bot.orders SET
                               status = %s,
                               exchange_order_id = %s,
                               executed_qty = %s,
                               avg_fill_price = %s,
                               error_code = %s,
                               updated_at = now()
                           WHERE client_order_id = %s""",
                        (status, exchange_order_id, executed_qty, avg_fill_price, error_code, client_order_id))

    def _audit(self, conn, event_type: str, details: dict) -> None:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO bot.audit_events (event_type, details) VALUES (%s, %s::jsonb)",
                        (event_type, _to_json(details)))

    def _persist_open_fill(
        self,
        conn,
        *,
        client_order_id: str,
        symbol: str,
        candle_open_time_ms: int,
        action: Action,
        fill: dict,
        atr14: Decimal,
        tick_size: Decimal,
        signal_reason: str,
        risk_checks: dict,
    ) -> dict:
        pos_side = "LONG" if action is Action.OPEN_LONG else "SHORT"
        fill_price = fill["avg_fill_price"]
        if fill_price is None or fill_price <= ZERO:
            raise TradeAPIError("MISSING_FILL_PRICE")
        trail_state = initial_trailing_state(pos_side, fill_price, atr14, self.params)
        q_stop = quantize_stop_price(pos_side, trail_state.stop_price, tick_size)
        init_risk_dist = abs(fill_price - q_stop)
        if init_risk_dist <= ZERO:
            init_risk_dist = trail_state.initial_risk_distance

        enriched_rc = dict(risk_checks or {})
        enriched_rc["atr14"] = format_decimal(atr14)
        enriched_rc["initial_stop_price"] = format_decimal(q_stop)
        enriched_rc["initial_risk_distance"] = format_decimal(init_risk_dist)

        self._update_order_status(
            conn,
            client_order_id,
            status=fill["status"],
            exchange_order_id=fill["exchange_order_id"],
            executed_qty=fill["executed_qty"],
            avg_fill_price=fill_price,
            error_code=None,
        )
        if self._fetch_position(conn, symbol) is None:
            with conn.cursor() as cur:
                cur.execute("""INSERT INTO bot.live_positions (
                                   symbol, side, quantity, entry_price, entry_client_order_id,
                                   signal_reason, candle_open_time_ms, stop_price, trail_distance,
                                   best_price, stop_client_algo_id, stop_algo_id, stop_algo_status,
                                   risk_checks
                               ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)""",
                            (
                                symbol,
                                pos_side,
                                fill["executed_qty"],
                                fill_price,
                                client_order_id,
                                signal_reason,
                                candle_open_time_ms,
                                q_stop,
                                trail_state.trail_distance,
                                trail_state.best_price,
                                None,
                                None,
                                None,
                                _to_json(enriched_rc),
                            ))
        self._audit(conn, "LIVE_ORDER_FILLED", {
            "symbol": symbol,
            "action": action.value,
            "client_order_id": client_order_id,
            "exchange_order_id": fill["exchange_order_id"],
            "qty": format_decimal(fill["executed_qty"]),
            "avg_price": format_decimal(fill_price),
        })
        conn.commit()
        return {
            "client_order_id": client_order_id,
            "status": fill["status"],
            "exchange_order_id": fill["exchange_order_id"],
            "executed_qty": fill["executed_qty"],
            "avg_fill_price": fill_price,
            "stop_price": q_stop,
            "initial_stop_price": q_stop,
            "initial_risk_distance": init_risk_dist,
        }

    def _persist_close_fill(
        self,
        conn,
        *,
        client_order_id: str,
        symbol: str,
        action: Action,
        fill: dict,
    ) -> dict:
        self._update_order_status(
            conn,
            client_order_id,
            status=fill["status"],
            exchange_order_id=fill["exchange_order_id"],
            executed_qty=fill["executed_qty"],
            avg_fill_price=fill["avg_fill_price"],
            error_code=None,
        )
        pos = self._fetch_position(conn, symbol)
        if pos is not None and pos["stop_algo_id"] is not None:
            self._cancel_algo_best_effort(symbol, pos["stop_algo_id"], pos["stop_client_algo_id"])
        with conn.cursor() as cur:
            cur.execute("DELETE FROM bot.live_positions WHERE symbol = %s", (symbol,))
        self._audit(conn, "LIVE_CLOSE_FILLED", {
            "symbol": symbol,
            "action": action.value,
            "client_order_id": client_order_id,
            "exchange_order_id": fill["exchange_order_id"],
            "qty": format_decimal(fill["executed_qty"]),
            "avg_price": format_decimal(fill["avg_fill_price"]) if fill["avg_fill_price"] else None,
        })
        conn.commit()
        return {
            "client_order_id": client_order_id,
            "status": fill["status"],
            "exchange_order_id": fill["exchange_order_id"],
            "executed_qty": fill["executed_qty"],
            "avg_fill_price": fill["avg_fill_price"],
        }

    def _cancel_algo_best_effort(self, symbol: str, algo_id: int | None, client_algo_id: str | None) -> None:
        if algo_id is None and client_algo_id is None:
            return
        try:
            self.client.cancel_algo_order(symbol, algo_id=algo_id, client_algo_id=client_algo_id)
        except TradeAPIError as exc:
            if exc.binance_code in _MISSING_BINANCE_CODES:
                return
            emit("ALGO_CANCEL_WARN", symbol=symbol, algo_id=algo_id, error=exc.code, binance_code=exc.binance_code)

    def _recover_single_order(
        self,
        conn,
        order_row: dict,
        tick_size: Decimal,
        atr_fallback: Decimal | None = None,
    ) -> dict:
        symbol = order_row["symbol"]
        cid = order_row["client_order_id"]
        action = Action(order_row["action"])
        resp = self.client.get_order(symbol, client_order_id=cid, order_id=order_row["exchange_order_id"])
        if resp is None:
            err = "ORDER_NOT_FOUND_BINANCE_-2013"
            self._update_order_status(conn, cid, status="FAILED", error_code=err)
            self._audit(conn, "LIVE_ORDER_FAILED_RECOVERY", {"symbol": symbol, "client_order_id": cid, "error": err})
            conn.commit()
            raise OrderRejectedError(err)

        fill = extract_market_fill(resp)
        if fill["status"] in ("FAILED", "CANCELED"):
            err = f"EXCHANGE_{fill['status']}"
            self._update_order_status(
                conn,
                cid,
                status=fill["status"],
                exchange_order_id=fill["exchange_order_id"],
                executed_qty=fill["executed_qty"],
                avg_fill_price=fill["avg_fill_price"],
                error_code=err,
            )
            conn.commit()
            raise OrderRejectedError(err)

        if fill["status"] != "FILLED":
            self._update_order_status(
                conn,
                cid,
                status=fill["status"],
                exchange_order_id=fill["exchange_order_id"],
                executed_qty=fill["executed_qty"],
                avg_fill_price=fill["avg_fill_price"],
            )
            conn.commit()
            raise AmbiguousOrderError(f"ORDER_IN_FLIGHT_{fill['status']}")

        if action in CLOSING:
            return self._persist_close_fill(conn, client_order_id=cid, symbol=symbol, action=action, fill=fill)

        rc = order_row["risk_checks"] or {}
        atr14 = Decimal(str(rc.get("atr14") or atr_fallback or ZERO))
        if atr14 <= ZERO:
            raise AmbiguousOrderError("MISSING_ATR_FOR_RECOVERY")
        res = self._persist_open_fill(
            conn,
            client_order_id=cid,
            symbol=symbol,
            candle_open_time_ms=order_row["candle_open_time_ms"],
            action=action,
            fill=fill,
            atr14=atr14,
            tick_size=tick_size,
            signal_reason=order_row["signal_reason"],
            risk_checks=rc,
        )
        self.ensure_stop_algo_order(conn, symbol, order_row["candle_open_time_ms"], tick_size, tag="init")
        return res

    def recover_pending_orders(
        self,
        conn,
        filters_by_symbol: dict[str, MarketFilters],
        atr_fallback_by_symbol: dict[str, Decimal] | None = None,
    ) -> list[dict]:
        """Riconcilia eventuali ordini PENDING/NEW/PARTIALLY_FILLED prima di qualsiasi nuovo trade."""
        with conn.cursor() as cur:
            cur.execute("""SELECT id, client_order_id, exchange_order_id, symbol, candle_open_time_ms,
                                  action, side, reduce_only, requested_qty, executed_qty,
                                  avg_fill_price, status, signal_reason, risk_checks
                           FROM bot.orders
                           WHERE order_kind = 'MARKET' AND status IN ('PENDING', 'NEW', 'PARTIALLY_FILLED')
                           ORDER BY id ASC""")
            rows = cur.fetchall()
        results = []
        for row in rows:
            order_row = self._row_to_order(row)
            sym = order_row["symbol"]
            f = filters_by_symbol[sym]
            atr_fb = (atr_fallback_by_symbol or {}).get(sym)
            try:
                results.append(self._recover_single_order(conn, order_row, f.tick_size, atr_fb))
            except OrderRejectedError as exc:
                results.append({"client_order_id": order_row["client_order_id"], "status": "FAILED", "error": str(exc)})
        return results

    def execute_open_market(
        self,
        conn,
        *,
        symbol: str,
        candle_open_time_ms: int,
        action: Action,
        qty: Decimal,
        ref_price: Decimal,
        atr14: Decimal,
        filters: MarketFilters,
        risk_limits: RiskLimits,
        open_positions: int,
        daily_net_pnl_usdt: Decimal,
        signal_reason: str,
        risk_checks: dict | None = None,
        same_side_open_positions: int | None = None,
        available_margin_usdt: Decimal | None = None,
        leverage: int | None = None,
    ) -> dict:
        if action in CLOSING:
            raise RiskViolation("EXPECTED_OPEN_ACTION")
        self.assert_trading_enabled(conn)
        if self._fetch_position(conn, symbol) is not None:
            raise RiskViolation(f"POSITION_ALREADY_OPEN: {symbol}")

        pos_side = "LONG" if action is Action.OPEN_LONG else "SHORT"
        rc = validate_open(
            qty,
            ref_price,
            filters,
            risk_limits,
            open_positions,
            daily_net_pnl_usdt,
            same_side_open_positions=same_side_open_positions,
            side=pos_side,
            available_margin_usdt=available_margin_usdt,
            leverage=leverage,
        )
        merged_rc = dict(rc)
        if risk_checks:
            merged_rc.update(risk_checks)
        merged_rc["atr14"] = format_decimal(atr14)

        existing = self._find_existing_market_order(conn, symbol, candle_open_time_ms, action)
        if existing is not None:
            if existing["status"] == "FILLED":
                return existing
            return self._recover_single_order(conn, existing, filters.tick_size, atr14)

        cid = make_client_order_id(symbol, candle_open_time_ms, action)
        self._insert_order_row(
            conn,
            client_order_id=cid,
            exchange_order_id=None,
            symbol=symbol,
            candle_open_time_ms=candle_open_time_ms,
            order_kind="MARKET",
            action=action,
            reduce_only=False,
            requested_qty=qty,
            executed_qty=ZERO,
            avg_fill_price=None,
            trigger_price=None,
            status="PENDING",
            signal_reason=signal_reason,
            risk_checks=merged_rc,
        )
        conn.commit()

        side = ORDER_SIDE[action]
        try:
            resp = self.client.place_market_order(symbol, side, qty, cid, reduce_only=False)
        except TradeAPIError as exc:
            if _is_ambiguous_api_error(exc):
                order_row = {
                    "id": 0,
                    "client_order_id": cid,
                    "exchange_order_id": None,
                    "symbol": symbol,
                    "candle_open_time_ms": candle_open_time_ms,
                    "action": action.value,
                    "side": side,
                    "reduce_only": False,
                    "requested_qty": qty,
                    "executed_qty": ZERO,
                    "avg_fill_price": None,
                    "status": "PENDING",
                    "signal_reason": signal_reason,
                    "risk_checks": merged_rc,
                }
                return self._recover_single_order(conn, order_row, filters.tick_size, atr14)

            err_code = str(exc)
            self._update_order_status(conn, cid, status="FAILED", error_code=err_code)
            self._audit(conn, "LIVE_ORDER_REJECTED", {"symbol": symbol, "client_order_id": cid, "error": err_code})
            conn.commit()
            raise OrderRejectedError(err_code) from exc

        fill = extract_market_fill(resp)
        if fill["status"] != "FILLED":
            self._update_order_status(
                conn,
                cid,
                status=fill["status"],
                exchange_order_id=fill["exchange_order_id"],
                executed_qty=fill["executed_qty"],
                avg_fill_price=fill["avg_fill_price"],
            )
            conn.commit()
            raise AmbiguousOrderError(f"UNEXPECTED_MARKET_STATUS_{fill['status']}")

        res = self._persist_open_fill(
            conn,
            client_order_id=cid,
            symbol=symbol,
            candle_open_time_ms=candle_open_time_ms,
            action=action,
            fill=fill,
            atr14=atr14,
            tick_size=filters.tick_size,
            signal_reason=signal_reason,
            risk_checks=merged_rc,
        )
        self.ensure_stop_algo_order(conn, symbol, candle_open_time_ms, filters.tick_size, tag="init")
        return res

    def execute_close_market(
        self,
        conn,
        *,
        symbol: str,
        candle_open_time_ms: int,
        action: Action,
        position_qty: Decimal,
        signal_reason: str,
        risk_checks: dict | None = None,
    ) -> dict:
        if action not in CLOSING:
            raise RiskViolation("EXPECTED_CLOSE_ACTION")
        self.assert_trading_enabled(conn)

        pos = self._fetch_position(conn, symbol)
        if pos is None:
            raise RiskViolation(f"NO_LIVE_POSITION: {symbol}")

        # Prima della chiusura MARKET verifica se lo stop protettivo si e' gia' attivato
        triggered = self.sync_triggered_algo_stop(conn, symbol)
        if triggered is not None:
            return triggered

        rc = validate_close(position_qty, pos["quantity"])
        merged_rc = dict(rc)
        if risk_checks:
            merged_rc.update(risk_checks)

        existing = self._find_existing_market_order(conn, symbol, candle_open_time_ms, action)
        if existing is not None:
            if existing["status"] == "FILLED":
                return existing
            return self._recover_single_order(conn, existing, Decimal("0.1"))

        cid = make_client_order_id(symbol, candle_open_time_ms, action)
        self._insert_order_row(
            conn,
            client_order_id=cid,
            exchange_order_id=None,
            symbol=symbol,
            candle_open_time_ms=candle_open_time_ms,
            order_kind="MARKET",
            action=action,
            reduce_only=True,
            requested_qty=position_qty,
            executed_qty=ZERO,
            avg_fill_price=None,
            trigger_price=None,
            status="PENDING",
            signal_reason=signal_reason,
            risk_checks=merged_rc,
        )
        conn.commit()

        side = ORDER_SIDE[action]
        try:
            resp = self.client.place_market_order(symbol, side, position_qty, cid, reduce_only=True)
        except TradeAPIError as exc:
            if exc.binance_code == -2022:
                # ReduceOnly rejected: possibile stop algo scattato mentre l'ordine era in volo
                triggered = self.sync_triggered_algo_stop(conn, symbol)
                if triggered is not None:
                    self._update_order_status(conn, cid, status="FAILED", error_code="SUPERSEDED_BY_STOP_ALGO")
                    conn.commit()
                    return triggered
            if _is_ambiguous_api_error(exc):
                order_row = {
                    "id": 0,
                    "client_order_id": cid,
                    "exchange_order_id": None,
                    "symbol": symbol,
                    "candle_open_time_ms": candle_open_time_ms,
                    "action": action.value,
                    "side": side,
                    "reduce_only": True,
                    "requested_qty": position_qty,
                    "executed_qty": ZERO,
                    "avg_fill_price": None,
                    "status": "PENDING",
                    "signal_reason": signal_reason,
                    "risk_checks": merged_rc,
                }
                return self._recover_single_order(conn, order_row, Decimal("0.1"))

            err_code = str(exc)
            self._update_order_status(conn, cid, status="FAILED", error_code=err_code)
            self._audit(conn, "LIVE_CLOSE_REJECTED", {"symbol": symbol, "client_order_id": cid, "error": err_code})
            conn.commit()
            raise OrderRejectedError(err_code) from exc

        fill = extract_market_fill(resp)
        if fill["status"] != "FILLED":
            self._update_order_status(
                conn,
                cid,
                status=fill["status"],
                exchange_order_id=fill["exchange_order_id"],
                executed_qty=fill["executed_qty"],
                avg_fill_price=fill["avg_fill_price"],
            )
            conn.commit()
            raise AmbiguousOrderError(f"UNEXPECTED_CLOSE_STATUS_{fill['status']}")

        return self._persist_close_fill(conn, client_order_id=cid, symbol=symbol, action=action, fill=fill)

    def ensure_stop_algo_order(
        self,
        conn,
        symbol: str,
        candle_open_time_ms: int,
        tick_size_or_filters: Decimal | MarketFilters,
        tag: str = "init",
    ) -> dict | None:
        pos = self._fetch_position(conn, symbol)
        if pos is None:
            return None
        if pos["stop_algo_id"] is not None:
            return {
                "algo_id": pos["stop_algo_id"],
                "client_algo_id": pos["stop_client_algo_id"],
                "algo_status": pos["stop_algo_status"],
                "stop_price": pos["stop_price"],
            }

        tick_size = tick_size_or_filters.tick_size if isinstance(tick_size_or_filters, MarketFilters) else tick_size_or_filters
        q_stop = quantize_stop_price(pos["side"], pos["stop_price"], tick_size)
        stop_side = "SELL" if pos["side"] == "LONG" else "BUY"
        caid = make_client_algo_id(symbol, candle_open_time_ms, tag)

        resp = self.client.place_stop_algo_order(symbol, stop_side, q_stop, caid)
        info = extract_algo_info(resp)
        with conn.cursor() as cur:
            cur.execute("""UPDATE bot.live_positions SET
                               stop_price = %s,
                               stop_client_algo_id = %s,
                               stop_algo_id = %s,
                               stop_algo_status = %s,
                               updated_at = now()
                           WHERE symbol = %s""",
                        (q_stop, info["client_algo_id"] or caid, info["algo_id"], info["algo_status"] or "NEW", symbol))
        conn.commit()
        return info

    def replace_stop_algo_order(
        self,
        conn,
        symbol: str,
        candle_open_time_ms: int,
        new_stop_price: Decimal,
        new_best_price: Decimal,
        filters: MarketFilters,
    ) -> dict:
        self.assert_trading_enabled(conn)
        pos = self._fetch_position(conn, symbol)
        if pos is None:
            raise RiskViolation(f"NO_LIVE_POSITION: {symbol}")

        q_new = quantize_stop_price(pos["side"], new_stop_price, filters.tick_size)
        q_old = quantize_stop_price(pos["side"], pos["stop_price"], filters.tick_size)
        tightened = (q_new > q_old) if pos["side"] == "LONG" else (q_new < q_old)

        improved_best = (
            new_best_price > pos["best_price"] if pos["side"] == "LONG" else new_best_price < pos["best_price"]
        )
        effective_best = new_best_price if improved_best else pos["best_price"]

        if not tightened:
            if improved_best:
                with conn.cursor() as cur:
                    cur.execute("""UPDATE bot.live_positions SET
                                       best_price = %s, stop_price = %s, updated_at = now()
                                   WHERE symbol = %s""", (effective_best, pos["stop_price"], symbol))
                conn.commit()
            return {"replaced": False, "stop_price": pos["stop_price"], "best_price": effective_best}

        if pos["stop_algo_id"] is not None or pos["stop_client_algo_id"] is not None:
            triggered = self.sync_triggered_algo_stop(conn, symbol)
            if triggered is not None:
                return {"replaced": False, "triggered": triggered}

            self._cancel_algo_best_effort(symbol, pos["stop_algo_id"], pos["stop_client_algo_id"])
            with conn.cursor() as cur:
                cur.execute("""UPDATE bot.live_positions SET
                                   stop_price = %s,
                                   stop_client_algo_id = %s,
                                   stop_algo_id = %s,
                                   stop_algo_status = %s,
                                   updated_at = now()
                               WHERE symbol = %s""", (q_new, None, None, "REPLACE_PENDING", symbol))
                cur.execute("""UPDATE bot.live_positions SET
                                   best_price = %s, stop_price = %s, updated_at = now()
                               WHERE symbol = %s""", (effective_best, q_new, symbol))
            conn.commit()
        else:
            with conn.cursor() as cur:
                cur.execute("""UPDATE bot.live_positions SET
                                   best_price = %s, stop_price = %s, updated_at = now()
                               WHERE symbol = %s""", (effective_best, q_new, symbol))
            conn.commit()

        info = self.ensure_stop_algo_order(conn, symbol, candle_open_time_ms, filters, tag="tr")
        return {"replaced": True, "stop_price": q_new, "best_price": effective_best, "algo": info}

    def sync_triggered_algo_stop(self, conn, symbol: str) -> dict | None:
        pos = self._fetch_position(conn, symbol)
        if pos is None or (pos["stop_algo_id"] is None and pos["stop_client_algo_id"] is None):
            return None

        resp = self.client.get_algo_order(
            symbol,
            algo_id=pos["stop_algo_id"],
            client_algo_id=pos["stop_client_algo_id"],
        )
        if resp is None:
            return None

        info = extract_algo_info(resp)
        if info["algo_status"] in ("CANCELED", "EXPIRED"):
            with conn.cursor() as cur:
                cur.execute("""UPDATE bot.live_positions SET
                                   stop_price = %s,
                                   stop_client_algo_id = %s,
                                   stop_algo_id = %s,
                                   stop_algo_status = %s,
                                   updated_at = now()
                               WHERE symbol = %s""",
                            (pos["stop_price"], None, None, info["algo_status"], symbol))
            conn.commit()
            return None

        if info["algo_status"] != "TRIGGERED":
            return None

        avg_price = info["actual_price"]
        exec_qty = info["actual_qty"]
        if (avg_price is None or exec_qty <= ZERO) and info["actual_order_id"] is not None:
            actual_ord = self.client.get_order(symbol, order_id=info["actual_order_id"])
            if actual_ord is not None:
                parsed = extract_market_fill(actual_ord)
                if avg_price is None:
                    avg_price = parsed["avg_fill_price"]
                if exec_qty <= ZERO:
                    exec_qty = parsed["executed_qty"]

        if exec_qty <= ZERO:
            exec_qty = pos["quantity"]
        if avg_price is None or avg_price <= ZERO:
            avg_price = pos["stop_price"]

        close_action = Action.CLOSE_LONG if pos["side"] == "LONG" else Action.CLOSE_SHORT
        cid = pos["stop_client_algo_id"] or f"algo_{symbol}_{pos['candle_open_time_ms']}"

        self._insert_order_row(
            conn,
            client_order_id=cid,
            exchange_order_id=info["actual_order_id"],
            symbol=symbol,
            candle_open_time_ms=pos["candle_open_time_ms"],
            order_kind="STOP_MARKET",
            action=close_action,
            reduce_only=True,
            requested_qty=pos["quantity"],
            executed_qty=exec_qty,
            avg_fill_price=avg_price,
            trigger_price=info["trigger_price"] or pos["stop_price"],
            status="FILLED",
            signal_reason="STOP_MARKET_ALGO",
            risk_checks={"algo_id": info["algo_id"], "algo_status": "TRIGGERED"},
        )
        with conn.cursor() as cur:
            cur.execute("DELETE FROM bot.live_positions WHERE symbol = %s", (symbol,))
        self._audit(conn, "LIVE_STOP_ALGO_FILLED", {
            "symbol": symbol,
            "action": close_action.value,
            "algo_id": info["algo_id"],
            "actual_order_id": info["actual_order_id"],
            "qty": format_decimal(exec_qty),
            "avg_price": format_decimal(avg_price),
        })
        conn.commit()
        return {
            "client_order_id": cid,
            "status": "FILLED",
            "order_kind": "STOP_MARKET",
            "exchange_order_id": info["actual_order_id"],
            "executed_qty": exec_qty,
            "avg_fill_price": avg_price,
        }
