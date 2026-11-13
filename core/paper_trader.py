"""Paper trader multi-simbolo 1d: registra nel DB le operazioni simulate. Non invia ordini."""
import signal as os_signal
import sys
import threading
import time
from decimal import Decimal
from typing import NamedTuple

import psycopg
from psycopg.types.json import Jsonb

from binance_public import DEMO_HOST, MarketFilters, PublicAPIError, PublicClient
from common import backoff_sleep, db_connect, emit
from risk_checks import (
    Action,
    ORDER_SIDE,
    RiskLimits,
    RiskViolation,
    size_open_quantity_atr,
    validate_close,
    validate_open,
)
from strategy import (
    STRATEGY_REASON,
    Candle,
    Signal,
    TrailingState,
    TsmomParams,
    advance_trailing,
    check_bar_exit,
    evaluate_tsmom_erf,
    funding_pnl_between,
    initial_trailing_state,
    parse_closed_candles,
    replay_trailing_state,
)

UNIVERSE_SYMBOLS = (
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "DOGEUSDT", "XRPUSDT",
    "ADAUSDT", "AVAXUSDT", "LINKUSDT", "DOTUSDT", "LTCUSDT", "BCHUSDT",
)
INTERVAL, INTERVAL_MS = "1d", 86_400_000
WINDOW = 220                  # 220 candele 1d (~7 mesi): copre warmup EMA50/ADX14/Ret30
FUNDING_LIMIT = 500           # ~166 giorni di funding 8h per gate anti-crowding + cashflow
FEE_RATE = Decimal("0.0005")  # taker per lato
SLIPPAGE = Decimal("0.0002")  # slippage simulato sfavorevole per lato
INITIAL_EQUITY_USDT = Decimal("1000")
RISK_FRACTION = Decimal("0.01")
ENTRY_GRACE_MS = 3_600_000    # finestra 1h post-chiusura 00:00 UTC entro cui aprire nuove entrate
MAX_POLL_SLEEP_S = 3600.0
LIMITS = RiskLimits(
    max_notional_usdt=Decimal("500"),
    max_open_positions=3,
    max_daily_loss_usdt=Decimal("100"),
    enforce_btc_qty_cap=False,
)
STRATEGY_PARAMS = TsmomParams()
LEGACY_MIGRATION_REASON = "STRATEGY_MIGRATION"

MAX_CONSECUTIVE_FAILURES = 10
LOCK_KEY = "paper_trader"
CLOSE_GRACE_S = 3.0           # attesa dopo la chiusura perché Binance pubblichi la candela
RETRY_WINDOW_MS = 90_000      # entro questo tempo dalla chiusura si riprova ogni 5 s
HALTED_POLL_S = 30.0
FILTERS_TTL_S = 6 * 3600
OFFSET_TTL_S = 3600
ZERO = Decimal(0)
Q8 = Decimal("0.00000001")    # numeric(24, 8)


class FatalError(Exception):
    """Errore non recuperabile: il processo si ferma senza ritentare."""


class OpenPosition(NamedTuple):
    symbol: str
    side: str
    quantity: Decimal
    entry_price: Decimal
    signal_reason: str
    candle_open_time_ms: int
    risk_checks: dict


# ---------- funzioni pure (testate) ----------

def q8(x: Decimal) -> Decimal:
    return x.quantize(Q8)


def fill_price(action, base: Decimal) -> Decimal:
    """Slippage sempre a sfavore: compri più caro, vendi più basso."""
    factor = 1 + SLIPPAGE if ORDER_SIDE[action] == "BUY" else 1 - SLIPPAGE
    return q8(base * factor)


def realized_pnl(side: str, entry: Decimal, fill: Decimal, qty: Decimal) -> Decimal:
    """PnL lordo (senza commissioni) alla chiusura."""
    if side == "LONG":
        return q8((fill - entry) * qty)
    if side == "SHORT":
        return q8((entry - fill) * qty)
    raise FatalError(f"INVALID_POSITION_SIDE_{side}")


def next_delay_s(now_ms: int, processed: bool, halted: bool = False,
                 interval_ms: int = 900_000) -> float:
    """Quanto dormire prima del prossimo ciclo."""
    if halted:
        return HALTED_POLL_S
    since_close = now_ms % interval_ms
    if not processed and since_close < RETRY_WINDOW_MS:
        return 5.0   # appena dopo la chiusura: la candela nuova potrebbe non essere ancora visibile
    return (interval_ms - since_close) / 1000 + CLOSE_GRACE_S


# ---------- mercato (client pubblico + cache) ----------

class Market:
    """Client pubblico multi-simbolo con cache di filtri di mercato e offset orario."""

    def __init__(self, symbols=UNIVERSE_SYMBOLS):
        self.symbols = list(symbols)
        self.client = PublicClient(DEMO_HOST)
        self.filters_by_symbol: dict[str, MarketFilters] = {}
        self.filters_at = None
        self.offset_ms = 0
        self.offset_at = None

    def refresh(self) -> None:
        now = time.monotonic()
        if self.offset_at is None or now - self.offset_at > OFFSET_TTL_S:
            self.offset_ms, rtt_ms = self.client.measure_time_offset_ms()
            self.offset_at = now
            emit("TIME_OFFSET", offset_ms=self.offset_ms, rtt_ms=rtt_ms)
        if not self.filters_by_symbol or self.filters_at is None or now - self.filters_at > FILTERS_TTL_S:
            self.filters_by_symbol = self.client.load_filters_many(self.symbols)
            self.filters_at = now
            emit("FILTERS_LOADED", symbols=len(self.filters_by_symbol))

    def now_ms(self) -> int:
        return int(time.time() * 1000) + self.offset_ms


# ---------- accesso al DB ----------

def connect_and_lock():
    conn = db_connect("core_paper_trader", "core_paper_trader_db_password")
    with conn.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(hashtext(%s))", (LOCK_KEY,))
        if not cur.fetchone()[0]:
            conn.close()
            raise FatalError("ANOTHER_PAPER_TRADER_RUNNING")
    return conn


def fetch_state(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT status, orders_enabled FROM bot.system_state WHERE id = 1")
        row = cur.fetchone()
    if row is None:
        raise FatalError("SYSTEM_STATE_MISSING")
    return row


def last_processed(conn, symbol: str, interval: str = INTERVAL) -> int:
    with conn.cursor() as cur:
        cur.execute("""SELECT COALESCE(MAX(candle_open_time_ms), 0)
                       FROM bot.paper_processed_candles
                       WHERE symbol = %s AND interval = %s""", (symbol, interval))
        return int(cur.fetchone()[0])


def open_positions_map(conn) -> dict[str, OpenPosition]:
    """Ritorna la mappa delle posizioni aperte per simbolo dall'ultimo trade per simbolo."""
    with conn.cursor() as cur:
        cur.execute("""SELECT DISTINCT ON (symbol)
                              symbol, action, quantity, fill_price,
                              signal_reason, candle_open_time_ms, risk_checks
                       FROM bot.paper_trades
                       ORDER BY symbol, id DESC""")
        rows = cur.fetchall()
    out: dict[str, OpenPosition] = {}
    for sym, action, qty, entry_px, reason, ts_ms, checks in rows:
        if action.startswith("OPEN_"):
            side = "LONG" if action == "OPEN_LONG" else "SHORT"
            out[sym] = OpenPosition(
                symbol=sym,
                side=side,
                quantity=qty,
                entry_price=entry_px,
                signal_reason=reason,
                candle_open_time_ms=int(ts_ms),
                risk_checks=dict(checks or {}),
            )
    return out


def daily_net_pnl(conn) -> Decimal:
    """PnL netto di portafoglio (realizzato meno commissioni) dall'inizio del giorno UTC."""
    with conn.cursor() as cur:
        cur.execute("""SELECT COALESCE(SUM(realized_pnl_usdt - fee_usdt), 0)
                       FROM bot.paper_trades
                       WHERE created_at >= date_trunc('day', now() AT TIME ZONE 'UTC')
                                         AT TIME ZONE 'UTC'""")
        return Decimal(cur.fetchone()[0])


def total_net_pnl(conn) -> Decimal:
    """PnL netto cumulativo su tutto lo storico di paper trading."""
    with conn.cursor() as cur:
        cur.execute("SELECT COALESCE(SUM(realized_pnl_usdt - fee_usdt), 0) FROM bot.paper_trades")
        return Decimal(cur.fetchone()[0])


def insert_processed_candle(cur, symbol: str, candle_open_ms: int,
                            signal_str: str, close_price: Decimal) -> bool:
    cur.execute("""INSERT INTO bot.paper_processed_candles
                   (symbol, interval, candle_open_time_ms, signal, close_price)
                   VALUES (%s, %s, %s, %s, %s)
                   ON CONFLICT DO NOTHING""",
                (symbol, INTERVAL, candle_open_ms, signal_str, close_price))
    return cur.rowcount == 1


def insert_trade(cur, symbol: str, action: Action, qty: Decimal, fill: Decimal,
                 pnl: Decimal, candle_open_ms: int, reason: str, checks: dict) -> Decimal:
    """Inserisce l'operazione e ritorna la commissione calcolata."""
    notional = q8(qty * fill)
    fee = q8(notional * FEE_RATE)
    cur.execute("""INSERT INTO bot.paper_trades
                   (symbol, action, side, quantity, fill_price, notional_usdt, fee_usdt,
                    realized_pnl_usdt, signal_reason, candle_open_time_ms, risk_checks)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (symbol, action.value, ORDER_SIDE[action], qty, fill, notional, fee,
                 pnl, reason, candle_open_ms, Jsonb(checks)))
    return fee


def _emit_events(events: list[tuple[str, dict]]) -> None:
    for name, fields in events:
        emit(name, **fields)


def _trailing_state_from_position(pos: OpenPosition, candles: list[Candle],
                                  current_open_ms: int) -> TrailingState:
    try:
        init_state = TrailingState(
            side=pos.side,
            entry_price=pos.entry_price,
            stop_price=Decimal(str(pos.risk_checks["initial_stop_price"])),
            trail_distance=Decimal(str(pos.risk_checks["trail_distance"])),
            best_price=Decimal(str(pos.risk_checks.get("best_price", pos.entry_price))),
        )
    except (KeyError, ArithmeticError) as e:
        raise FatalError(f"MISSING_TRAILING_STATE_{pos.symbol}: {e}") from None
    between = [c for c in candles if pos.candle_open_time_ms < c.open_time_ms < current_open_ms]
    return replay_trailing_state(init_state, between)


def close_legacy_positions(conn, market: Market) -> int:
    """Chiude all'avvio eventuali posizioni residue di strategie precedenti."""
    legacy = [
        (sym, pos) for sym, pos in open_positions_map(conn).items()
        if pos.signal_reason != STRATEGY_REASON
    ]
    if not legacy:
        return 0
    client = market.client
    server_now = client.get_server_time_ms()
    closed_count = 0
    for sym, pos in legacy:
        candles = parse_closed_candles(client.get_klines(sym, INTERVAL, 5), server_now, INTERVAL_MS)
        if not candles:
            raise ValueError(f"NO_CLOSED_D1_FOR_MIGRATION:{sym}")
        last = candles[-1]
        base_price = client.get_price(sym)
        action = Action.CLOSE_LONG if pos.side == "LONG" else Action.CLOSE_SHORT
        fill = fill_price(action, base_price)
        try:
            checks = validate_close(pos.quantity, pos.quantity)
        except RiskViolation as e:
            raise FatalError(f"LEGACY_POSITION_INCONSISTENT_{sym}: {e}") from None
        checks["migrated_from"] = pos.signal_reason
        pnl = realized_pnl(pos.side, pos.entry_price, fill, pos.quantity)
        with conn.transaction(), conn.cursor() as cur:
            insert_processed_candle(cur, sym, last.open_time_ms, Signal.HOLD.value, last.close)
            fee = insert_trade(cur, sym, action, pos.quantity, fill, pnl,
                               last.open_time_ms, LEGACY_MIGRATION_REASON, checks)
        emit("LEGACY_POSITION_CLOSED", symbol=sym, action=action.value,
             quantity=pos.quantity, fill_price=fill, realized_pnl=pnl, fee=fee,
             migrated_from=pos.signal_reason, candle_open_time_ms=last.open_time_ms)
        closed_count += 1
    return closed_count


# ---------- ciclo multi-simbolo ----------

def _process_open_symbol_bars(
    conn,
    sym: str,
    candles: list[Candle],
    pending: list[Candle],
    funding: list[tuple[int, Decimal]],
    open_map: dict[str, OpenPosition],
    daily_pnl: Decimal,
    equity_usdt: Decimal,
) -> tuple[bool, Decimal, Decimal, list[Candle]]:
    """Gestisce trailing stop/SL e funding sulle barre 1d chiuse per un simbolo aperto."""
    processed_any = False
    index_by_time = {c.open_time_ms: idx for idx, c in enumerate(candles)}

    for bar_pos, bar in enumerate(pending):
        pos = open_map.get(sym)
        if pos is None:
            return processed_any, daily_pnl, equity_usdt, pending[bar_pos:]

        bar_idx = index_by_time[bar.open_time_ms]
        state = _trailing_state_from_position(pos, candles[:bar_idx], bar.open_time_ms)
        exit_hit = check_bar_exit(state, bar, SLIPPAGE)

        if exit_hit is None:
            new_state = advance_trailing(state, bar)
            with conn.transaction(), conn.cursor() as cur:
                inserted = insert_processed_candle(cur, sym, bar.open_time_ms, Signal.HOLD.value, bar.close)
            if inserted:
                processed_any = True
                emit("CANDLE_PROCESSED", symbol=sym, candle_open_time_ms=bar.open_time_ms,
                     signal="HOLD", close=bar.close, position_after=pos.side,
                     stop_price=q8(new_state.stop_price), best_price=q8(new_state.best_price),
                     daily_net_pnl=daily_pnl)
            continue

        raw_fill, exit_reason = exit_hit
        fill = q8(raw_fill)
        action = Action.CLOSE_LONG if pos.side == "LONG" else Action.CLOSE_SHORT
        try:
            checks = validate_close(pos.quantity, pos.quantity)
        except RiskViolation as e:
            raise FatalError(f"POSITION_INCONSISTENT_{sym}: {e}") from None

        funding_pnl = sum(
            (funding_pnl_between(
                pos.side, pos.quantity, c.open_time_ms, c.open_time_ms + INTERVAL_MS, funding, c.close
            ) for c in candles[:bar_idx + 1] if c.open_time_ms > pos.candle_open_time_ms),
            ZERO,
        )
        price_pnl = realized_pnl(pos.side, pos.entry_price, fill, pos.quantity)
        total_pnl = q8(price_pnl + funding_pnl)
        checks["price_pnl_usdt"] = str(price_pnl)
        checks["funding_pnl_usdt"] = str(q8(funding_pnl))

        with conn.transaction(), conn.cursor() as cur:
            inserted = insert_processed_candle(cur, sym, bar.open_time_ms, Signal.HOLD.value, bar.close)
            if not inserted:
                continue
            fee = insert_trade(cur, sym, action, pos.quantity, fill, total_pnl,
                               bar.open_time_ms, exit_reason, checks)
        daily_pnl += total_pnl - fee
        equity_usdt += total_pnl - fee
        del open_map[sym]
        processed_any = True
        _emit_events([
            ("PAPER_TRADE", dict(
                symbol=sym, candle_open_time_ms=bar.open_time_ms,
                action=action.value, quantity=pos.quantity, fill_price=fill,
                realized_pnl=total_pnl, funding_pnl=q8(funding_pnl),
                fee=fee, exit_reason=exit_reason,
            )),
            ("CANDLE_PROCESSED", dict(
                symbol=sym, candle_open_time_ms=bar.open_time_ms,
                signal="HOLD", close=bar.close, position_after="FLAT",
                daily_net_pnl=daily_pnl,
            )),
        ])
        return processed_any, daily_pnl, equity_usdt, pending[bar_pos + 1:]

    return processed_any, daily_pnl, equity_usdt, []


def _process_flat_bar(
    conn,
    market: Market,
    sym: str,
    candles: list[Candle],
    bar_idx: int,
    bar: Candle,
    funding: list[tuple[int, Decimal]],
    server_now: int,
    open_map: dict[str, OpenPosition],
    daily_pnl: Decimal,
    equity_usdt: Decimal,
) -> tuple[bool, Decimal, Decimal]:
    """Valuta un potenziale nuovo ingresso su una barra 1d chiusa per un simbolo FLAT."""
    if server_now - bar.close_time_ms > ENTRY_GRACE_MS:
        with conn.transaction(), conn.cursor() as cur:
            inserted = insert_processed_candle(cur, sym, bar.open_time_ms, Signal.HOLD.value, bar.close)
        if inserted:
            emit("CANDLE_PROCESSED", symbol=sym, candle_open_time_ms=bar.open_time_ms,
                 signal="HOLD", close=bar.close, position_after="FLAT",
                 stale_bootstrap=True, daily_net_pnl=daily_pnl)
        return inserted, daily_pnl, equity_usdt

    eval_res = evaluate_tsmom_erf(candles[:bar_idx + 1], funding, STRATEGY_PARAMS)
    if eval_res.signal is Signal.HOLD or eval_res.atr14 is None:
        with conn.transaction(), conn.cursor() as cur:
            inserted = insert_processed_candle(cur, sym, bar.open_time_ms, Signal.HOLD.value, bar.close)
        if inserted:
            emit("CANDLE_PROCESSED", symbol=sym, candle_open_time_ms=bar.open_time_ms,
                 signal="HOLD", signal_detail=eval_res.reason, close=bar.close,
                 position_after="FLAT", daily_net_pnl=daily_pnl)
        return inserted, daily_pnl, equity_usdt

    action = Action.OPEN_LONG if eval_res.signal is Signal.BUY else Action.OPEN_SHORT
    side = "LONG" if action is Action.OPEN_LONG else "SHORT"
    if len(open_map) >= LIMITS.max_open_positions:
        with conn.transaction(), conn.cursor() as cur:
            inserted = insert_processed_candle(cur, sym, bar.open_time_ms, eval_res.signal.value, bar.close)
        if inserted:
            _emit_events([
                ("RISK_BLOCKED", dict(symbol=sym, candle_open_time_ms=bar.open_time_ms,
                                      action=action.value, reason=f"MAX_POSITIONS_REACHED: {len(open_map)}")),
                ("CANDLE_PROCESSED", dict(symbol=sym, candle_open_time_ms=bar.open_time_ms,
                                          signal=eval_res.signal.value, close=bar.close,
                                          position_after="FLAT", daily_net_pnl=daily_pnl)),
            ])
        return inserted, daily_pnl, equity_usdt

    base_price = market.client.get_price(sym)
    fill = fill_price(action, base_price)
    f = market.filters_by_symbol[sym]
    stop_dist = STRATEGY_PARAMS.sl_atr_mult * eval_res.atr14

    try:
        open_qty = size_open_quantity_atr(equity_usdt, RISK_FRACTION, stop_dist, fill, f, LIMITS)
        checks = validate_open(open_qty, fill, f, LIMITS, len(open_map), daily_pnl)
    except RiskViolation as e:
        with conn.transaction(), conn.cursor() as cur:
            inserted = insert_processed_candle(cur, sym, bar.open_time_ms, eval_res.signal.value, bar.close)
        if inserted:
            _emit_events([
                ("RISK_BLOCKED", dict(symbol=sym, candle_open_time_ms=bar.open_time_ms,
                                      action=action.value, reason=str(e))),
                ("CANDLE_PROCESSED", dict(symbol=sym, candle_open_time_ms=bar.open_time_ms,
                                          signal=eval_res.signal.value, close=bar.close,
                                          position_after="FLAT", daily_net_pnl=daily_pnl)),
            ])
        return inserted, daily_pnl, equity_usdt

    init_trail = initial_trailing_state(side, fill, eval_res.atr14, STRATEGY_PARAMS)
    checks.update({
        "atr14": str(q8(eval_res.atr14)),
        "initial_stop_price": str(q8(init_trail.stop_price)),
        "trail_distance": str(q8(init_trail.trail_distance)),
        "best_price": str(q8(init_trail.best_price)),
        "risk_usdt": str(q8(equity_usdt * RISK_FRACTION)),
        "equity_usdt": str(q8(equity_usdt)),
    })

    with conn.transaction(), conn.cursor() as cur:
        inserted = insert_processed_candle(cur, sym, bar.open_time_ms, eval_res.signal.value, bar.close)
        if not inserted:
            return False, daily_pnl, equity_usdt
        fee = insert_trade(cur, sym, action, open_qty, fill, ZERO,
                           bar.open_time_ms, STRATEGY_REASON, checks)

    daily_pnl -= fee
    equity_usdt -= fee
    open_map[sym] = OpenPosition(sym, side, open_qty, fill, STRATEGY_REASON, bar.open_time_ms, checks)
    _emit_events([
        ("PAPER_TRADE", dict(
            symbol=sym, candle_open_time_ms=bar.open_time_ms,
            action=action.value, quantity=open_qty, fill_price=fill,
            realized_pnl=ZERO, fee=fee, stop_price=q8(init_trail.stop_price),
        )),
        ("CANDLE_PROCESSED", dict(
            symbol=sym, candle_open_time_ms=bar.open_time_ms,
            signal=eval_res.signal.value, close=bar.close,
            position_after=side, daily_net_pnl=daily_pnl,
        )),
    ])
    return True, daily_pnl, equity_usdt


def process_latest_candles(conn, market: Market) -> bool:
    """Elabora le candele 1d chiuse su tutto l'universo. True se elabora almeno una candela."""
    client = market.client
    server_now = client.get_server_time_ms()
    open_map = open_positions_map(conn)
    equity_usdt = INITIAL_EQUITY_USDT + total_net_pnl(conn)
    daily_pnl = daily_net_pnl(conn)

    pending_by_sym: dict[str, tuple[list[Candle], list[Candle], list[tuple[int, Decimal]]]] = {}
    for sym in market.symbols:
        raw = client.get_klines(sym, INTERVAL, WINDOW + 1)
        candles = parse_closed_candles(raw, server_now, INTERVAL_MS)[-WINDOW:]
        if len(candles) < WINDOW:
            raise ValueError(f"NOT_ENOUGH_CLOSED_CANDLES:{sym}")
        last_ms = last_processed(conn, sym)
        pending = [candles[-1]] if last_ms == 0 else [c for c in candles if c.open_time_ms > last_ms]
        if not pending:
            continue
        funding = client.get_funding_rates(sym, limit=FUNDING_LIMIT)
        pending_by_sym[sym] = (candles, pending, funding)

    if not pending_by_sym:
        return False

    processed_any = False
    flat_queue: list[tuple[int, int, str, Candle]] = []

    # Fase 1: gestisci uscite e trailing sulle posizioni gia' aperte (libera eventuale slot).
    for sym_idx, sym in enumerate(market.symbols):
        if sym not in pending_by_sym:
            continue
        candles, pending, funding = pending_by_sym[sym]
        if sym in open_map:
            did_work, daily_pnl, equity_usdt, leftover = _process_open_symbol_bars(
                conn, sym, candles, pending, funding, open_map, daily_pnl, equity_usdt
            )
            processed_any = processed_any or did_work
            pending = leftover
        for bar in pending:
            flat_queue.append((bar.open_time_ms, sym_idx, sym, bar))

    # Fase 2: valuta nuove entrate in ordine cronologico e deterministico di liquidita'.
    flat_queue.sort(key=lambda item: (item[0], item[1]))
    for _open_ms, _sym_idx, sym, bar in flat_queue:
        if sym in open_map:
            continue
        candles, _pending, funding = pending_by_sym[sym]
        bar_idx = next(idx for idx, c in enumerate(candles) if c.open_time_ms == bar.open_time_ms)
        did_work, daily_pnl, equity_usdt = _process_flat_bar(
            conn, market, sym, candles, bar_idx, bar, funding, server_now, open_map, daily_pnl, equity_usdt
        )
        processed_any = processed_any or did_work

    return processed_any


# ---------- loop principale ----------

def main() -> int:
    stop = threading.Event()
    for sig_num in (os_signal.SIGINT, os_signal.SIGTERM):
        os_signal.signal(sig_num, lambda *_: stop.set())

    market = Market()
    conn = None
    failures = 0
    emit("PAPER_TRADER_START", symbols=len(market.symbols), interval=INTERVAL,
         window=WINDOW, strategy=STRATEGY_REASON, risk_fraction=RISK_FRACTION,
         max_open_positions=LIMITS.max_open_positions,
         max_notional_usdt=LIMITS.max_notional_usdt)
    try:
        while not stop.is_set():
            halted, processed = False, False
            try:
                if conn is None or conn.closed:
                    conn = connect_and_lock()
                    emit("DB_CONNECTED")
                market.refresh()
                status, orders_enabled = fetch_state(conn)
                if status == "HALTED":
                    halted = True
                    emit("PAPER_HALTED_SKIP", status=status, orders_enabled=orders_enabled)
                else:
                    close_legacy_positions(conn, market)
                    processed = process_latest_candles(conn, market)
                failures = 0
            except FatalError as e:
                emit("PAPER_TRADER_FATAL", error=str(e))
                return 2
            except (PublicAPIError, psycopg.Error, OSError, ValueError,
                    ArithmeticError, RuntimeError) as e:
                failures += 1
                emit("PAPER_TRADER_ERROR", error=type(e).__name__, detail=str(e),
                     consecutive_failures=failures)
                if isinstance(e, psycopg.OperationalError) and conn is not None:
                    try:
                        conn.close()
                    except psycopg.Error:
                        pass
                    conn = None   # al prossimo giro: riconnessione + nuovo advisory lock
                if failures >= MAX_CONSECUTIVE_FAILURES:
                    emit("PAPER_TRADER_ABORT", consecutive_failures=failures)
                    return 1
                backoff_sleep(failures)
                continue
            delay_s = min(next_delay_s(market.now_ms(), processed, halted, INTERVAL_MS), MAX_POLL_SLEEP_S)
            stop.wait(delay_s)
    finally:
        if conn is not None and not conn.closed:
            conn.close()
    emit("PAPER_TRADER_STOP")
    return 0


if __name__ == "__main__":
    sys.exit(main())
