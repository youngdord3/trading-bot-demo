"""Paper trader: registra nel DB le operazioni che FAREBBE. Non invia nulla a Binance."""
import signal as os_signal
import sys
import threading
import time
from decimal import Decimal

import psycopg
from psycopg.types.json import Jsonb

from binance_public import DEMO_HOST, PublicAPIError, PublicClient
from common import backoff_sleep, db_connect, emit
from risk_checks import (CLOSING, ORDER_SIDE, RiskLimits, RiskViolation,
                         plan_actions, size_open_quantity, validate_close, validate_open)
from strategy import STRATEGY_REASON, evaluate_crossover, parse_closed_candles

SYMBOL = "BTCUSDT"            # fisso: i limiti di rischio sono in BTC
INTERVAL, INTERVAL_MS = "15m", 900_000
WINDOW = 200                  # stessa finestra di backtest e live
FEE_RATE = Decimal("0.0005")  # taker per lato, come nel backtest
SLIPPAGE = Decimal("0.0002")  # come nel backtest
LIMITS = RiskLimits()
STRATEGY_FN = evaluate_crossover   # per cambiare strategia: sostituire qui (e STRATEGY_REASON)
MAX_CONSECUTIVE_FAILURES = 10
LOCK_KEY = "paper_trader"
CLOSE_GRACE_S = 3.0           # attesa dopo la chiusura perché Binance pubblichi la candela
RETRY_WINDOW_MS = 90_000      # entro questo tempo dalla chiusura si riprova ogni 5 s
HALTED_POLL_S = 30.0
FILTERS_TTL_S = 6 * 3600
OFFSET_TTL_S = 3600
Q8 = Decimal("0.00000001")    # numeric(24, 8)


class FatalError(Exception):
    """Errore non recuperabile: il processo si ferma senza ritentare."""


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


def next_delay_s(now_ms: int, processed: bool, halted: bool = False) -> float:
    """Quanto dormire prima del prossimo ciclo."""
    if halted:
        return HALTED_POLL_S
    since_close = now_ms % INTERVAL_MS
    if not processed and since_close < RETRY_WINDOW_MS:
        return 5.0   # appena dopo la chiusura: la candela nuova potrebbe non essere ancora visibile
    return (INTERVAL_MS - since_close) / 1000 + CLOSE_GRACE_S


# ---------- mercato (client pubblico + cache) ----------

class Market:
    """Client pubblico con cache di filtri di mercato e offset orario."""

    def __init__(self):
        self.client = PublicClient(DEMO_HOST)
        self.filters = None
        self.filters_at = None
        self.offset_ms = 0
        self.offset_at = None

    def refresh(self) -> None:
        now = time.monotonic()
        if self.offset_at is None or now - self.offset_at > OFFSET_TTL_S:
            self.offset_ms, rtt_ms = self.client.measure_time_offset_ms()
            self.offset_at = now
            emit("TIME_OFFSET", offset_ms=self.offset_ms, rtt_ms=rtt_ms)
        if self.filters is None or now - self.filters_at > FILTERS_TTL_S:
            self.filters = self.client.load_filters(SYMBOL)
            self.filters_at = now
            emit("FILTERS_LOADED", min_notional=self.filters.min_notional,
                 market_step_size=self.filters.market_step_size)

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


def last_processed(conn) -> int:
    with conn.cursor() as cur:
        cur.execute("""SELECT COALESCE(MAX(candle_open_time_ms), 0)
                       FROM bot.paper_processed_candles
                       WHERE symbol = %s AND interval = %s""", (SYMBOL, INTERVAL))
        return int(cur.fetchone()[0])


def current_position(conn):
    """Ritorna (side, quantity, entry_price) dall'ultimo record di paper_trades."""
    with conn.cursor() as cur:
        cur.execute("""SELECT action, quantity, fill_price FROM bot.paper_trades
                       WHERE symbol = %s ORDER BY id DESC LIMIT 1""", (SYMBOL,))
        row = cur.fetchone()
    if row is None or row[0].startswith("CLOSE_"):
        return "FLAT", Decimal(0), None
    return ("LONG" if row[0] == "OPEN_LONG" else "SHORT"), row[1], row[2]


def daily_net_pnl(conn) -> Decimal:
    """PnL netto (realizzato meno commissioni) dall'inizio del giorno UTC."""
    with conn.cursor() as cur:
        cur.execute("""SELECT COALESCE(SUM(realized_pnl_usdt - fee_usdt), 0)
                       FROM bot.paper_trades
                       WHERE symbol = %s
                         AND created_at >= date_trunc('day', now() AT TIME ZONE 'UTC')
                                           AT TIME ZONE 'UTC'""", (SYMBOL,))
        return Decimal(cur.fetchone()[0])


def insert_trade(cur, action, qty: Decimal, fill: Decimal, pnl: Decimal,
                 candle_open_ms: int, checks: dict) -> Decimal:
    """Inserisce l'operazione e ritorna la commissione calcolata."""
    notional = q8(qty * fill)
    fee = q8(notional * FEE_RATE)
    cur.execute("""INSERT INTO bot.paper_trades
                   (symbol, action, side, quantity, fill_price, notional_usdt, fee_usdt,
                    realized_pnl_usdt, signal_reason, candle_open_time_ms, risk_checks)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (SYMBOL, action.value, ORDER_SIDE[action], qty, fill, notional, fee,
                 pnl, STRATEGY_REASON, candle_open_ms, Jsonb(checks)))
    return fee


# ---------- un ciclo di lavoro ----------

def process_latest_candle(conn, market: Market) -> bool:
    """Elabora l'ultima candela chiusa se non già vista. True se ha elaborato qualcosa."""
    client = market.client
    server_now = client.get_server_time_ms()
    raw = client.get_klines(SYMBOL, INTERVAL, WINDOW + 1)
    candles = parse_closed_candles(raw, server_now, INTERVAL_MS)[-WINDOW:]
    if len(candles) < WINDOW:
        raise ValueError("NOT_ENOUGH_CLOSED_CANDLES")
    last = candles[-1]
    if last.open_time_ms <= last_processed(conn):
        return False

    sig = STRATEGY_FN([c.close for c in candles])
    base_price = client.get_price(SYMBOL)   # ≈ open della candela successiva
    f = market.filters
    events = []

    # Candela + operazioni nella stessa transazione: o c'è tutto o non c'è niente.
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("""INSERT INTO bot.paper_processed_candles
                       (symbol, interval, candle_open_time_ms, signal, close_price)
                       VALUES (%s, %s, %s, %s, %s)
                       ON CONFLICT DO NOTHING""",
                    (SYMBOL, INTERVAL, last.open_time_ms, sig.value, last.close))
        if cur.rowcount == 0:
            return False   # già elaborata (dedup a livello DB)

        side, qty, entry = current_position(conn)
        daily = daily_net_pnl(conn)

        for action in plan_actions(sig, side):
            fill = fill_price(action, base_price)
            if action in CLOSING:
                try:
                    checks = validate_close(qty, qty)
                except RiskViolation as e:
                    raise FatalError(f"POSITION_INCONSISTENT: {e}") from None
                pnl = realized_pnl(side, entry, fill, qty)
                fee = insert_trade(cur, action, qty, fill, pnl, last.open_time_ms, checks)
                daily += pnl - fee
                events.append(("PAPER_TRADE", dict(
                    action=action.value, quantity=qty, fill_price=fill,
                    realized_pnl=pnl, fee=fee)))
                side, qty, entry = "FLAT", Decimal(0), None
            else:
                try:
                    open_qty = size_open_quantity(fill, f, LIMITS)
                    checks = validate_open(open_qty, fill, f, LIMITS,
                                           0 if side == "FLAT" else 1, daily)
                except RiskViolation as e:
                    events.append(("RISK_BLOCKED", dict(action=action.value, reason=str(e))))
                    continue
                fee = insert_trade(cur, action, open_qty, fill, Decimal(0),
                                   last.open_time_ms, checks)
                daily -= fee
                side = "LONG" if action.value == "OPEN_LONG" else "SHORT"
                qty, entry = open_qty, fill
                events.append(("PAPER_TRADE", dict(
                    action=action.value, quantity=open_qty, fill_price=fill,
                    realized_pnl=Decimal(0), fee=fee)))

    # Log solo dopo il commit: i log descrivono solo ciò che è davvero nel DB.
    for name, fields in events:
        emit(name, candle_open_time_ms=last.open_time_ms, **fields)
    emit("CANDLE_PROCESSED", candle_open_time_ms=last.open_time_ms,
         signal=sig.value, close=last.close, position_after=side,
         daily_net_pnl=daily)
    return True


# ---------- loop principale ----------

def main() -> int:
    stop = threading.Event()
    for sig_num in (os_signal.SIGINT, os_signal.SIGTERM):
        os_signal.signal(sig_num, lambda *_: stop.set())

    market = Market()
    conn = None
    failures = 0
    emit("PAPER_TRADER_START", symbol=SYMBOL, interval=INTERVAL, window=WINDOW,
         strategy=STRATEGY_REASON)
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
                    processed = process_latest_candle(conn, market)
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
            stop.wait(next_delay_s(market.now_ms(), processed, halted))
    finally:
        if conn is not None and not conn.closed:
            conn.close()
    emit("PAPER_TRADER_STOP")
    return 0


if __name__ == "__main__":
    sys.exit(main())
