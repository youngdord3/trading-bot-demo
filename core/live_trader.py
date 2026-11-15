"""Live trader multi-simbolo 1d su Binance Demo Futures con OrderExecutor 5-fasi e Stop Algo."""
import json
import os
import signal as os_signal
import sys
import threading
import time
from decimal import Decimal

import psycopg

from binance_private import PrivateReadError, ReadOnlyClient
from binance_public import DEMO_HOST, MarketFilters, PublicAPIError, PublicClient
from common import backoff_sleep, db_connect, emit
from order_executor import (
    AmbiguousOrderError,
    OrderExecutor,
    OrderRejectedError,
    TradeAPIError,
    TradeClient,
    TradingDisabledError,
)
from risk_checks import (
    Action,
    RiskLimits,
    RiskViolation,
    effective_limits,
    required_initial_margin_usdt,
    safe_leverage_for_stop,
    size_open_quantity_atr,
    validate_close,
    validate_leverage_for_exposure,
    validate_open,
)
from strategy import (
    STRATEGY_REASON,
    UNIVERSE_SYMBOLS,
    Candle,
    Signal,
    TrailingState,
    TsmomParams,
    advance_trailing,
    check_bar_exit,
    evaluate_tsmom_erf,
    parse_closed_candles,
)

INTERVAL, INTERVAL_MS = "1d", 86_400_000
WINDOW = 220
FUNDING_LIMIT = 500
SLIPPAGE = Decimal("0.0002")
ENTRY_GRACE_MS = 3_600_000
RISK_FRACTION = Decimal(os.environ.get("TRADE_RISK_FRACTION", "0.01"))
MAX_NOTIONAL_EQUITY_MULT = Decimal(os.environ.get("TRADE_MAX_NOTIONAL_EQUITY_MULT", "2.0"))
MAX_SAME_SIDE_POSITIONS = int(os.environ.get("TRADE_MAX_SAME_SIDE_POSITIONS", "2"))
TARGET_LEVERAGE = int(os.environ.get("TRADE_LEVERAGE", "7"))
TARGET_MARGIN_TYPE = "ISOLATED"
LIMITS = RiskLimits(
    max_notional_usdt=Decimal(os.environ.get("TRADE_MAX_NOTIONAL_USDT", "20000")),
    max_open_positions=int(os.environ.get("TRADE_MAX_OPEN_POSITIONS", "3")),
    max_daily_loss_usdt=Decimal(os.environ.get("TRADE_MAX_DAILY_LOSS_USDT", "250")),
    enforce_btc_qty_cap=False,
    max_notional_equity_mult=MAX_NOTIONAL_EQUITY_MULT,
    max_same_side_positions=MAX_SAME_SIDE_POSITIONS,
)
STRATEGY_PARAMS = TsmomParams()

MAX_CONSECUTIVE_FAILURES = 10
LOCK_KEY = "live_trader"
CLOSE_GRACE_S = 3.0
RETRY_WINDOW_MS = 90_000
FAST_POLL_S = 60.0
HALTED_POLL_S = 30.0
MAX_POLL_SLEEP_S = 3600.0
FILTERS_TTL_S = 6 * 3600
OFFSET_TTL_S = 3600
ZERO = Decimal(0)


class FatalError(Exception):
    """Errore strutturale non recuperabile: ferma live_trader in sicurezza."""


def next_live_delay_s(
    now_ms: int,
    processed: bool,
    has_open_positions: bool = False,
    halted: bool = False,
    interval_ms: int = INTERVAL_MS,
) -> float:
    if halted:
        return HALTED_POLL_S
    since_close = now_ms % interval_ms
    if not processed and since_close < RETRY_WINDOW_MS:
        daily_delay = 5.0
    else:
        daily_delay = (interval_ms - since_close) / 1000 + CLOSE_GRACE_S
    if has_open_positions:
        return min(daily_delay, FAST_POLL_S)
    return min(daily_delay, MAX_POLL_SLEEP_S)


class LiveMarket:
    """Cache pubblica filtri di mercato + sincronia clock verso Binance Demo."""

    def __init__(self, symbols=UNIVERSE_SYMBOLS, public_client=None):
        self.symbols = list(symbols)
        self.client = public_client if public_client is not None else PublicClient(DEMO_HOST)
        self.filters_by_symbol: dict[str, MarketFilters] = {}
        self.filters_at = None
        self.offset_ms = 0
        self.offset_at = None

    def refresh(self, read_client=None, trade_client=None) -> None:
        now = time.monotonic()
        if self.offset_at is None or now - self.offset_at > OFFSET_TTL_S:
            self.offset_ms, rtt_ms = self.client.measure_time_offset_ms()
            self.offset_at = now
            if read_client is not None:
                read_client.set_time_offset_ms(self.offset_ms)
            if trade_client is not None:
                trade_client.set_time_offset_ms(self.offset_ms)
            emit("TIME_OFFSET", offset_ms=self.offset_ms, rtt_ms=rtt_ms)
        if not self.filters_by_symbol or self.filters_at is None or now - self.filters_at > FILTERS_TTL_S:
            self.filters_by_symbol = self.client.load_filters_many(self.symbols)
            self.filters_at = now
            emit("FILTERS_LOADED", symbols=len(self.filters_by_symbol))

    def now_ms(self) -> int:
        return int(time.time() * 1000) + self.offset_ms


def ensure_account_preflight(
    read_client: ReadOnlyClient,
    trade_client: TradeClient,
    symbols: list[str] | tuple[str, ...],
    target_leverage: int = TARGET_LEVERAGE,
    limits: RiskLimits = LIMITS,
    open_symbols: frozenset[str] | set[str] = frozenset(),
) -> dict:
    """Verifica leva/esposizione, ONE_WAY e applica ISOLATED + leva concordata sui simboli flat."""
    try:
        validate_leverage_for_exposure(target_leverage, limits)
    except RiskViolation as exc:
        raise FatalError(str(exc)) from exc

    dual = read_client.get("/fapi/v1/positionSide/dual")
    if dual.get("dualSidePosition") is not False:
        raise FatalError("HEDGE_MODE_NOT_SUPPORTED")

    raw_cfg = read_client.get("/fapi/v1/symbolConfig")
    cfg_map = {item["symbol"]: item for item in raw_cfg if isinstance(item, dict) and "symbol" in item}
    margin_updated = []
    leverage_updated = []

    for sym in symbols:
        cfg = cfg_map.get(sym, {})
        cur_margin = str(cfg.get("marginType") or "").upper()
        cur_lev = int(cfg["leverage"]) if cfg.get("leverage") is not None else None
        if sym in open_symbols:
            if cur_margin and cur_margin != TARGET_MARGIN_TYPE:
                raise FatalError(f"OPEN_POSITION_NOT_ISOLATED:{sym}:{cur_margin}")
            continue
        if cur_margin != TARGET_MARGIN_TYPE:
            trade_client.set_margin_type(sym, TARGET_MARGIN_TYPE)
            margin_updated.append(sym)
        if cur_lev != target_leverage:
            trade_client.set_leverage(sym, target_leverage)
            leverage_updated.append(sym)

    if margin_updated or leverage_updated:
        emit("LIVE_PREFLIGHT_UPDATED", margin_updated=margin_updated, leverage_updated=leverage_updated,
             target_leverage=target_leverage)
    return {"margin_updated": margin_updated, "leverage_updated": leverage_updated}


def connect_and_lock():
    conn = db_connect("core_trader", "core_trader_db_password")
    with conn.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(hashtext(%s))", (LOCK_KEY,))
        if not cur.fetchone()[0]:
            conn.close()
            raise FatalError("ANOTHER_LIVE_TRADER_RUNNING")
    return conn


def fetch_state(conn) -> tuple[str, bool]:
    with conn.cursor() as cur:
        cur.execute("SELECT status, orders_enabled FROM bot.system_state WHERE id = 1")
        row = cur.fetchone()
    if row is None:
        raise FatalError("SYSTEM_STATE_MISSING")
    return row[0], bool(row[1])


def last_processed(conn, symbol: str, interval: str = INTERVAL) -> int:
    with conn.cursor() as cur:
        cur.execute("""SELECT COALESCE(MAX(candle_open_time_ms), 0)
                       FROM bot.live_processed_candles
                       WHERE symbol = %s AND interval = %s""", (symbol, interval))
        return int(cur.fetchone()[0])


def insert_processed_candle(conn, symbol: str, candle_open_time_ms: int, signal_str: str, close_price: Decimal) -> bool:
    with conn.cursor() as cur:
        cur.execute("""INSERT INTO bot.live_processed_candles (symbol, interval, candle_open_time_ms, signal, close_price)
                       VALUES (%s, %s, %s, %s, %s)
                       ON CONFLICT (symbol, interval, candle_open_time_ms) DO NOTHING
                       RETURNING candle_open_time_ms""",
                    (symbol, INTERVAL, candle_open_time_ms, signal_str, close_price))
        inserted = cur.fetchone() is not None
    conn.commit()
    return inserted


def open_positions_map(conn) -> dict[str, dict]:
    with conn.cursor() as cur:
        cur.execute("""SELECT symbol, side, quantity, entry_price, entry_client_order_id,
                              signal_reason, candle_open_time_ms, stop_price,
                              trail_distance, best_price, stop_client_algo_id,
                              stop_algo_id, stop_algo_status, risk_checks
                       FROM bot.live_positions""")
        rows = cur.fetchall()
    out = {}
    for row in rows:
        rc = row[13] if isinstance(row[13], dict) else (json.loads(row[13]) if row[13] else {})
        out[row[0]] = {
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
    return out


def daily_realized_pnl(conn) -> Decimal:
    with conn.cursor() as cur:
        cur.execute("""SELECT COALESCE(SUM(
                           CASE
                               WHEN c.action = 'CLOSE_LONG' THEN (c.avg_fill_price - e.avg_fill_price) * c.executed_qty
                               ELSE (e.avg_fill_price - c.avg_fill_price) * c.executed_qty
                           END
                       ), 0)
                       FROM bot.orders c
                       JOIN LATERAL (
                           SELECT avg_fill_price
                           FROM bot.orders o
                           WHERE o.symbol = c.symbol
                             AND o.action IN ('OPEN_LONG', 'OPEN_SHORT')
                             AND o.status = 'FILLED'
                             AND o.id < c.id
                             AND o.avg_fill_price IS NOT NULL
                           ORDER BY o.id DESC
                           LIMIT 1
                       ) e ON true
                       WHERE c.status = 'FILLED'
                         AND c.action IN ('CLOSE_LONG', 'CLOSE_SHORT')
                         AND c.avg_fill_price IS NOT NULL
                         AND c.updated_at >= date_trunc('day', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'""")
        val = cur.fetchone()[0]
    return Decimal(str(val or "0"))


def _restore_default_leverage(executor: OrderExecutor, sym: str, pos: dict) -> None:
    """Ripristina la leva standard se ridotta temporaneamente su uno stop ampio."""
    used_lev = (pos.get("risk_checks") or {}).get("leverage")
    if used_lev is None or int(used_lev) == TARGET_LEVERAGE:
        return
    try:
        executor.client.set_leverage(sym, TARGET_LEVERAGE)
    except Exception as exc:
        emit("LIVE_LEVERAGE_RESTORE_FAILED", symbol=sym, target_leverage=TARGET_LEVERAGE, error=str(exc))


def _initial_risk_distance(pos: dict) -> Decimal:
    """Ricava 1R iniziale da risk_checks senza usare lo stop trailing corrente."""
    rc = pos.get("risk_checks") or {}
    raw_risk = rc.get("initial_risk_distance")
    if raw_risk is not None:
        val = Decimal(str(raw_risk))
        if val > ZERO:
            return val
    raw_stop = rc.get("initial_stop_price")
    if raw_stop is not None:
        val = abs(pos["entry_price"] - Decimal(str(raw_stop)))
        if val > ZERO:
            return val
    return ZERO


def direction_counts(open_map: dict[str, dict]) -> dict[str, int]:
    """Conta le posizioni aperte per direzione ('LONG' e 'SHORT')."""
    longs = sum(1 for p in open_map.values() if p["side"] == "LONG")
    shorts = sum(1 for p in open_map.values() if p["side"] == "SHORT")
    return {"LONG": longs, "SHORT": shorts}


def _initial_available_margin(
    account: dict,
    equity_usdt: Decimal,
    open_map: dict[str, dict],
    target_leverage: int,
) -> Decimal:
    raw_avail = account.get("availableBalance")
    if raw_avail is not None:
        return Decimal(str(raw_avail))
    used_margin = ZERO
    for pos in open_map.values():
        lev = max(1, int((pos.get("risk_checks") or {}).get("leverage") or target_leverage))
        used_margin += (pos["quantity"] * pos["entry_price"]) / Decimal(lev)
    return max(ZERO, equity_usdt - used_margin)


def _sync_open_algo_stops(conn, executor: OrderExecutor, open_map: dict[str, dict]) -> dict[str, dict]:
    for sym in list(open_map):
        pos = open_map[sym]
        res = executor.sync_triggered_algo_stop(conn, sym)
        if res is not None:
            emit("LIVE_ALGO_STOP_SYNCED", symbol=sym, avg_price=res["avg_fill_price"], qty=res["executed_qty"])
            _restore_default_leverage(executor, sym, pos)
            open_map.pop(sym, None)
    return open_map


def _process_open_symbol_bars(
    conn,
    market: LiveMarket,
    executor: OrderExecutor,
    sym: str,
    pending: list[Candle],
    open_map: dict[str, dict],
) -> tuple[bool, list[Candle]]:
    processed_any = False
    for idx, bar in enumerate(pending):
        pos = open_map.get(sym)
        if pos is None:
            return processed_any, pending[idx:]

        if bar.open_time_ms <= pos["candle_open_time_ms"]:
            if insert_processed_candle(conn, sym, bar.open_time_ms, Signal.HOLD.value, bar.close):
                processed_any = True
            continue

        state = TrailingState(
            side=pos["side"],
            entry_price=pos["entry_price"],
            stop_price=pos["stop_price"],
            trail_distance=pos["trail_distance"],
            best_price=pos["best_price"],
            initial_risk_distance=_initial_risk_distance(pos),
        )
        exit_hit = check_bar_exit(state, bar, SLIPPAGE)
        if exit_hit is None:
            new_state = advance_trailing(state, bar, STRATEGY_PARAMS)
            rep = executor.replace_stop_algo_order(
                conn,
                sym,
                bar.open_time_ms,
                new_state.stop_price,
                new_state.best_price,
                market.filters_by_symbol[sym],
            )
            if rep.get("triggered"):
                open_map.pop(sym, None)
                _restore_default_leverage(executor, sym, pos)
                insert_processed_candle(conn, sym, bar.open_time_ms, Signal.HOLD.value, bar.close)
                processed_any = True
                return processed_any, pending[idx + 1:]

            pos["stop_price"] = rep["stop_price"]
            pos["best_price"] = rep["best_price"]
            if insert_processed_candle(conn, sym, bar.open_time_ms, Signal.HOLD.value, bar.close):
                processed_any = True
                emit("LIVE_CANDLE_PROCESSED", symbol=sym, candle_open_time_ms=bar.open_time_ms,
                     signal="HOLD", close=bar.close, position_after=pos["side"],
                     stop_price=pos["stop_price"], best_price=pos["best_price"])
            continue

        _raw_fill, exit_reason = exit_hit
        close_action = Action.CLOSE_LONG if pos["side"] == "LONG" else Action.CLOSE_SHORT
        validate_close(pos["quantity"], pos["quantity"])
        executor.execute_close_market(
            conn,
            symbol=sym,
            candle_open_time_ms=bar.open_time_ms,
            action=close_action,
            position_qty=pos["quantity"],
            signal_reason=exit_reason,
            risk_checks={"bar_close": str(bar.close), "trail_stop": str(pos["stop_price"])},
        )
        open_map.pop(sym, None)
        _restore_default_leverage(executor, sym, pos)
        insert_processed_candle(conn, sym, bar.open_time_ms, Signal.HOLD.value, bar.close)
        processed_any = True
        emit("LIVE_EXIT_FILLED", symbol=sym, action=close_action.value, reason=exit_reason,
             candle_open_time_ms=bar.open_time_ms)
        return processed_any, pending[idx + 1:]

    return processed_any, []


def _record_risk_block(
    conn,
    sym: str,
    bar: Candle,
    signal: Signal,
    action: Action,
    reason: str,
) -> bool:
    inserted = insert_processed_candle(conn, sym, bar.open_time_ms, signal.value, bar.close)
    if inserted:
        emit("LIVE_RISK_BLOCKED", symbol=sym, candle_open_time_ms=bar.open_time_ms,
             action=action.value, reason=reason)
    return inserted


def _open_flat_candidate(
    conn,
    market: LiveMarket,
    executor: OrderExecutor,
    sym: str,
    bar: Candle,
    eval_res,
    open_map: dict[str, dict],
    side_counts: dict[str, int],
    cycle_limits: RiskLimits,
    equity_usdt: Decimal,
    daily_pnl: Decimal,
    avail_margin: Decimal,
) -> tuple[bool, Decimal]:
    action = Action.OPEN_LONG if eval_res.signal is Signal.BUY else Action.OPEN_SHORT
    side = "LONG" if action is Action.OPEN_LONG else "SHORT"

    if len(open_map) >= cycle_limits.max_open_positions:
        did = _record_risk_block(conn, sym, bar, eval_res.signal, action,
                                 f"MAX_POSITIONS_REACHED:{len(open_map)}")
        return did, avail_margin

    if (
        cycle_limits.max_same_side_positions is not None
        and side_counts[side] >= cycle_limits.max_same_side_positions
    ):
        did = _record_risk_block(conn, sym, bar, eval_res.signal, action,
                                 f"MAX_SAME_SIDE_REACHED:{side}:{side_counts[side]}")
        return did, avail_margin

    ref_price = market.client.get_price(sym)
    f = market.filters_by_symbol[sym]
    stop_dist = STRATEGY_PARAMS.sl_atr_mult * eval_res.atr14
    trade_leverage = safe_leverage_for_stop(stop_dist, ref_price, TARGET_LEVERAGE)
    try:
        qty = size_open_quantity_atr(equity_usdt, RISK_FRACTION, stop_dist, ref_price, f, cycle_limits)
        checks = validate_open(
            qty,
            ref_price,
            f,
            cycle_limits,
            len(open_map),
            daily_pnl,
            same_side_open_positions=side_counts[side],
            side=side,
            available_margin_usdt=avail_margin,
            leverage=trade_leverage,
        )
    except RiskViolation as exc:
        return _record_risk_block(conn, sym, bar, eval_res.signal, action, str(exc)), avail_margin

    checks["equity_usdt"] = str(equity_usdt)
    checks["er10"] = str(eval_res.er10) if eval_res.er10 is not None else "0"
    checks["leverage"] = trade_leverage
    checks["max_notional_cap_usdt"] = str(cycle_limits.max_notional_usdt)

    leverage_changed = False
    if trade_leverage != TARGET_LEVERAGE:
        executor.client.set_leverage(sym, trade_leverage)
        leverage_changed = True

    try:
        executor.execute_open_market(
            conn,
            symbol=sym,
            candle_open_time_ms=bar.open_time_ms,
            action=action,
            qty=qty,
            ref_price=ref_price,
            atr14=eval_res.atr14,
            filters=f,
            risk_limits=cycle_limits,
            open_positions=len(open_map),
            daily_net_pnl_usdt=daily_pnl,
            signal_reason=STRATEGY_REASON,
            risk_checks=checks,
            same_side_open_positions=side_counts[side],
            available_margin_usdt=avail_margin,
            leverage=trade_leverage,
        )
    except OrderRejectedError as exc:
        if leverage_changed:
            _restore_default_leverage(executor, sym, {"risk_checks": {"leverage": trade_leverage}})
        emit("LIVE_ORDER_REJECTED", symbol=sym, action=action.value, error=str(exc))
        return insert_processed_candle(conn, sym, bar.open_time_ms, eval_res.signal.value, bar.close), avail_margin

    open_map.update(open_positions_map(conn))
    side_counts.clear()
    side_counts.update(direction_counts(open_map))
    used_margin = required_initial_margin_usdt(qty * ref_price, trade_leverage, Decimal("1.0"))
    new_avail_margin = max(ZERO, avail_margin - used_margin)
    insert_processed_candle(conn, sym, bar.open_time_ms, eval_res.signal.value, bar.close)
    emit("LIVE_ENTRY_FILLED", symbol=sym, action=action.value, qty=qty, er10=eval_res.er10,
         leverage=trade_leverage, candle_open_time_ms=bar.open_time_ms, open_positions=len(open_map))
    return True, new_avail_margin


def process_cycle(
    conn,
    market: LiveMarket,
    read_client: ReadOnlyClient,
    executor: OrderExecutor,
    *,
    allow_preflight: bool = True,
) -> bool:
    """Ciclo live singolo: sync/open exits prima, poi contatori direzionali e ingressi ordinati (-er10, sym_idx)."""
    executor.recover_pending_orders(conn, market.filters_by_symbol)
    open_map = _sync_open_algo_stops(conn, executor, open_positions_map(conn))

    status, orders_enabled = fetch_state(conn)
    if status != "ACTIVE" or not orders_enabled:
        emit("LIVE_TRADING_SKIPPED", status=status, orders_enabled=orders_enabled, open_positions=len(open_map))
        return False

    if allow_preflight:
        ensure_account_preflight(
            read_client,
            executor.client,
            market.symbols,
            target_leverage=TARGET_LEVERAGE,
            limits=LIMITS,
            open_symbols=frozenset(open_map),
        )

    for sym, pos in open_map.items():
        if pos["stop_algo_id"] is None:
            executor.ensure_stop_algo_order(
                conn, sym, pos["candle_open_time_ms"], market.filters_by_symbol[sym], tag="init"
            )

    server_now = market.client.get_server_time_ms()
    candidate_syms = []
    last_by_sym = {}
    for sym in market.symbols:
        last_ms = last_processed(conn, sym, INTERVAL)
        last_by_sym[sym] = last_ms
        if last_ms == 0 or server_now >= last_ms + 2 * INTERVAL_MS:
            candidate_syms.append(sym)

    if not candidate_syms:
        return False

    pending_by_sym: dict[str, tuple[list[Candle], list[Candle], list[tuple[int, Decimal]]]] = {}
    for sym in candidate_syms:
        raw = market.client.get_klines(sym, INTERVAL, WINDOW + 1)
        candles = parse_closed_candles(raw, server_now, INTERVAL_MS)[-WINDOW:]
        if len(candles) < WINDOW:
            raise ValueError(f"NOT_ENOUGH_CLOSED_CANDLES:{sym}")
        last_ms = last_by_sym[sym]
        pending = [candles[-1]] if last_ms == 0 else [c for c in candles if c.open_time_ms > last_ms]
        if not pending:
            continue
        funding = market.client.get_funding_rates(sym, limit=FUNDING_LIMIT)
        pending_by_sym[sym] = (candles, pending, funding)

    if not pending_by_sym:
        return False

    # Step 1: gestisci prima uscite e trailing sulle posizioni aperte.
    processed_any = False
    flat_by_ts: dict[int, list[tuple[int, str, Candle]]] = {}
    for sym_idx, sym in enumerate(market.symbols):
        if sym not in pending_by_sym:
            continue
        _candles, pending, _funding = pending_by_sym[sym]
        if sym in open_map:
            did_work, pending = _process_open_symbol_bars(conn, market, executor, sym, pending, open_map)
            processed_any = processed_any or did_work
        for bar in pending:
            flat_by_ts.setdefault(bar.open_time_ms, []).append((sym_idx, sym, bar))

    # Step 2: aggiorna saldo/equity/margine e contatori per direzione dopo eventuali uscite.
    account = read_client.get("/fapi/v3/account")
    equity_usdt = Decimal(str(account["totalMarginBalance"]))
    daily_pnl = daily_realized_pnl(conn)
    cycle_limits = effective_limits(equity_usdt, LIMITS)
    side_counts = direction_counts(open_map)
    avail_margin = _initial_available_margin(account, equity_usdt, open_map, TARGET_LEVERAGE)

    # Step 3: valuta ingressi flat ordinati per (-er10, sym_idx) rispettando MAX_SAME_SIDE_POSITIONS = 2.
    for open_ms in sorted(flat_by_ts):
        scored_candidates = []
        for sym_idx, sym, bar in flat_by_ts[open_ms]:
            if sym in open_map:
                continue
            if server_now - bar.close_time_ms > ENTRY_GRACE_MS:
                if insert_processed_candle(conn, sym, bar.open_time_ms, Signal.HOLD.value, bar.close):
                    processed_any = True
                    emit("LIVE_CANDLE_STALE_SKIP", symbol=sym, candle_open_time_ms=bar.open_time_ms)
                continue
            candles, _pending, funding = pending_by_sym[sym]
            bar_idx = next(idx for idx, c in enumerate(candles) if c.open_time_ms == bar.open_time_ms)
            eval_res = evaluate_tsmom_erf(candles[:bar_idx + 1], funding, STRATEGY_PARAMS)
            if eval_res.signal is Signal.HOLD or eval_res.atr14 is None or eval_res.er10 is None:
                if insert_processed_candle(conn, sym, bar.open_time_ms, Signal.HOLD.value, bar.close):
                    processed_any = True
                continue
            scored_candidates.append((sym_idx, sym, bar, eval_res))

        scored_candidates.sort(key=lambda item: (-item[3].er10, item[0]))
        side_counts = direction_counts(open_map)
        for _sym_idx, sym, bar, eval_res in scored_candidates:
            if sym in open_map:
                continue
            did_work, avail_margin = _open_flat_candidate(
                conn,
                market,
                executor,
                sym,
                bar,
                eval_res,
                open_map,
                side_counts,
                cycle_limits,
                equity_usdt,
                daily_pnl,
                avail_margin,
            )
            processed_any = processed_any or did_work

    return processed_any


def main() -> int:
    stop = threading.Event()
    for sig_num in (os_signal.SIGINT, os_signal.SIGTERM):
        os_signal.signal(sig_num, lambda *_: stop.set())

    market = LiveMarket()
    read_client = ReadOnlyClient()
    trade_client = TradeClient()
    executor = OrderExecutor(trade_client, STRATEGY_PARAMS)
    conn = None
    failures = 0
    preflight_done = False

    emit("LIVE_TRADER_START", symbols=len(market.symbols), interval=INTERVAL, window=WINDOW,
         strategy=STRATEGY_REASON, risk_fraction=RISK_FRACTION, target_leverage=TARGET_LEVERAGE,
         margin_type=TARGET_MARGIN_TYPE, max_open_positions=LIMITS.max_open_positions,
         max_notional_usdt=LIMITS.max_notional_usdt)
    try:
        while not stop.is_set():
            halted, processed, has_open = False, False, False
            try:
                if conn is None or conn.closed:
                    conn = connect_and_lock()
                    emit("LIVE_DB_CONNECTED")
                market.refresh(read_client=read_client, trade_client=trade_client)
                status, orders_enabled = fetch_state(conn)
                if status == "HALTED":
                    halted = True
                    emit("LIVE_HALTED_SKIP", status=status, orders_enabled=orders_enabled)
                else:
                    processed = process_cycle(
                        conn, market, read_client, executor, allow_preflight=not preflight_done
                    )
                    if status == "ACTIVE" and orders_enabled:
                        preflight_done = True
                    has_open = bool(open_positions_map(conn))
                failures = 0
            except FatalError as exc:
                emit("LIVE_TRADER_FATAL", error=str(exc))
                return 2
            except (TradingDisabledError, AmbiguousOrderError, TradeAPIError, PrivateReadError,
                    PublicAPIError, psycopg.Error, OSError, ValueError, ArithmeticError, RuntimeError) as exc:
                failures += 1
                emit("LIVE_TRADER_ERROR", error=type(exc).__name__, detail=str(exc), consecutive_failures=failures)
                if isinstance(exc, psycopg.OperationalError) and conn is not None:
                    try:
                        conn.close()
                    except psycopg.Error:
                        pass
                    conn = None
                if failures >= MAX_CONSECUTIVE_FAILURES:
                    emit("LIVE_TRADER_ABORT", consecutive_failures=failures)
                    return 1
                backoff_sleep(failures)
                continue

            delay_s = next_live_delay_s(market.now_ms(), processed, has_open, halted, INTERVAL_MS)
            stop.wait(delay_s)
    finally:
        if conn is not None and not conn.closed:
            conn.close()
    emit("LIVE_TRADER_STOP")
    return 0


if __name__ == "__main__":
    sys.exit(main())
