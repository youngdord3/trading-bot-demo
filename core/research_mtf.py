"""Ricerca strategia multi-timeframe (MTF) con walk-forward a finestre scorrevoli.

Modulo di sola ricerca: nessun accesso al database, nessun segreto, nessun ordine.
Scarica candele pubbliche 15m da fapi.binance.com, aggrega 1h e 4h in locale
(senza look-ahead) e valuta una strategia trend-following con SL/TP su ATR.
"""
import bisect
import http.client
import json
import os
import re
import sys
import time
from typing import NamedTuple
from urllib.parse import urlencode

API_HOST = "fapi.binance.com"
KLINES_PATH = "/fapi/v1/klines"
KLINES_LIMIT = 1500
HTTP_TIMEOUT_S = 20
MAX_RETRIES = 4
REQUEST_PAUSE_S = 0.25

BASE_MS = 15 * 60 * 1000
HOUR_MS = 60 * 60 * 1000
FOUR_HOUR_MS = 4 * HOUR_MS
DAY_MS = 24 * HOUR_MS
BARS_PER_DAY = DAY_MS // BASE_MS

SYMBOL_PATTERN = re.compile(r"^[A-Z0-9]{5,20}$")


def _env_str(name, default):
    return os.environ.get(name, default).strip()


def _env_int(name, default):
    return int(os.environ.get(name, str(default)))


def _env_float(name, default):
    return float(os.environ.get(name, str(default)))


SYMBOLS = [
    item.strip().upper()
    for item in _env_str("RS_SYMBOLS", "BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,DOGEUSDT").split(",")
    if item.strip()
]
DAYS = _env_int("RS_DAYS", 365)
WARMUP_DAYS = _env_int("RS_WARMUP_DAYS", 15)
TRAIN_DAYS = _env_int("RS_TRAIN_DAYS", 180)
TEST_DAYS = _env_int("RS_TEST_DAYS", 60)

INITIAL_EQUITY_USDT = _env_float("RS_INITIAL_EQUITY_USDT", 1000.0)
RISK_FRACTION = _env_float("RS_RISK_FRACTION", 0.01)
MAX_LEVERAGE = _env_float("RS_MAX_LEVERAGE", 2.0)
TAKER_FEE_RATE = _env_float("RS_TAKER_FEE_RATE", 0.0005)
SLIPPAGE_RATE = _env_float("RS_SLIPPAGE_RATE", 0.0002)
MAX_CONCURRENT_POSITIONS = _env_int("RS_MAX_CONCURRENT_POSITIONS", 3)
COOLDOWN_BARS = _env_int("RS_COOLDOWN_BARS", 8)
MIN_IN_SAMPLE_TRADES = _env_int("RS_MIN_IN_SAMPLE_TRADES", 30)

RISK_PER_TRADE_USDT = INITIAL_EQUITY_USDT * RISK_FRACTION
MAX_NOTIONAL_USDT = INITIAL_EQUITY_USDT * MAX_LEVERAGE

# Parametri fissi della strategia (non ottimizzati, ripresi dal vecchio bot).
RSI_OVERBOUGHT = 72.0
RSI_OVERSOLD = 28.0
PULLBACK_MAX_ATR = 1.5
PROFIT_FACTOR_CAP = 10.0

# Criteri di accettazione Out-of-Sample fissati a priori.
ACCEPT_MIN_PROFIT_FACTOR = 1.30
ACCEPT_MIN_TRADES = 30
ACCEPT_MAX_DRAWDOWN_PCT = 15.0
ACCEPT_MIN_WIN_RATE = 0.40
ACCEPT_MIN_PAYOFF_RATIO = 2.0


class Bar(NamedTuple):
    open_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float


class Params(NamedTuple):
    adx_min: float
    sl_mult: float
    rr: float


class Position(NamedTuple):
    symbol: str
    side: int
    entry_time: int
    entry_price: float
    qty: float
    stop_price: float
    take_price: float


class Trade(NamedTuple):
    symbol: str
    side: int
    entry_time: int
    exit_time: int
    entry_price: float
    exit_price: float
    qty: float
    net_pnl: float
    fees: float
    reason: str


class Features(NamedTuple):
    bars: list
    times: list
    ema9: list
    ema21: list
    ema50: list
    ema200: list
    rsi14: list
    atr14: list
    adx14: list
    trend_1h: list
    trend_4h: list


# Griglia volutamente piccola (8 combinazioni) per limitare l'overfitting.
GRID = [
    Params(adx_min, sl_mult, rr)
    for adx_min in (18.0, 25.0)
    for sl_mult in (1.5, 2.5)
    for rr in (1.5, 2.5)
]


class RateLimitedError(RuntimeError):
    """Binance ha risposto 418/429: ci si ferma subito, senza riprovare."""


def log_event(event, **fields):
    record = {"event": event}
    record.update(fields)
    print(json.dumps(record, sort_keys=True), flush=True)


# ---------------------------------------------------------------------------
# Indicatori puri (causali: il valore all'indice i usa solo dati fino a i)
# ---------------------------------------------------------------------------


def ema(values, period):
    count = len(values)
    out = [None] * count
    if period <= 0 or count < period:
        return out
    current = sum(values[:period]) / period
    out[period - 1] = current
    k = 2.0 / (period + 1)
    for i in range(period, count):
        current = values[i] * k + current * (1.0 - k)
        out[i] = current
    return out


def _rsi_value(avg_gain, avg_loss):
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def rsi(values, period):
    count = len(values)
    out = [None] * count
    if period <= 0 or count <= period:
        return out
    gain = 0.0
    loss = 0.0
    for i in range(1, period + 1):
        diff = values[i] - values[i - 1]
        if diff > 0:
            gain += diff
        else:
            loss -= diff
    avg_gain = gain / period
    avg_loss = loss / period
    out[period] = _rsi_value(avg_gain, avg_loss)
    for i in range(period + 1, count):
        diff = values[i] - values[i - 1]
        up = diff if diff > 0 else 0.0
        down = -diff if diff < 0 else 0.0
        avg_gain = (avg_gain * (period - 1) + up) / period
        avg_loss = (avg_loss * (period - 1) + down) / period
        out[i] = _rsi_value(avg_gain, avg_loss)
    return out


def _true_ranges(highs, lows, closes):
    count = len(closes)
    out = [0.0] * count
    for i in range(1, count):
        out[i] = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
    return out


def atr(highs, lows, closes, period):
    count = len(closes)
    out = [None] * count
    if period <= 0 or count <= period:
        return out
    ranges = _true_ranges(highs, lows, closes)
    current = sum(ranges[1:period + 1]) / period
    out[period] = current
    for i in range(period + 1, count):
        current = (current * (period - 1) + ranges[i]) / period
        out[i] = current
    return out


def _dx_value(smooth_plus, smooth_minus, smooth_tr):
    if smooth_tr == 0:
        return 0.0
    plus_di = 100.0 * smooth_plus / smooth_tr
    minus_di = 100.0 * smooth_minus / smooth_tr
    total = plus_di + minus_di
    if total == 0:
        return 0.0
    return 100.0 * abs(plus_di - minus_di) / total


def adx(highs, lows, closes, period):
    count = len(closes)
    out = [None] * count
    if period <= 0 or count < 2 * period:
        return out
    ranges = _true_ranges(highs, lows, closes)
    plus_dm = [0.0] * count
    minus_dm = [0.0] * count
    for i in range(1, count):
        up_move = highs[i] - highs[i - 1]
        down_move = lows[i - 1] - lows[i]
        if up_move > down_move and up_move > 0:
            plus_dm[i] = up_move
        if down_move > up_move and down_move > 0:
            minus_dm[i] = down_move
    smooth_tr = sum(ranges[1:period + 1])
    smooth_plus = sum(plus_dm[1:period + 1])
    smooth_minus = sum(minus_dm[1:period + 1])
    dx = [0.0] * count
    dx[period] = _dx_value(smooth_plus, smooth_minus, smooth_tr)
    for i in range(period + 1, count):
        smooth_tr = smooth_tr - smooth_tr / period + ranges[i]
        smooth_plus = smooth_plus - smooth_plus / period + plus_dm[i]
        smooth_minus = smooth_minus - smooth_minus / period + minus_dm[i]
        dx[i] = _dx_value(smooth_plus, smooth_minus, smooth_tr)
    current = sum(dx[period:2 * period]) / period
    out[2 * period - 1] = current
    for i in range(2 * period, count):
        current = (current * (period - 1) + dx[i]) / period
        out[i] = current
    return out


# ---------------------------------------------------------------------------
# Aggregazione multi-timeframe senza look-ahead
# ---------------------------------------------------------------------------


def aggregate(bars, span_ms):
    """Aggrega candele 15m in candele di durata span_ms. Scarta i bucket incompleti."""
    needed = span_ms // BASE_MS
    out = []
    bucket = None
    current = None
    count = 0
    for bar in bars:
        key = bar.open_time // span_ms
        if key != bucket:
            if current is not None and count == needed:
                out.append(current)
            bucket = key
            current = Bar(key * span_ms, bar.open, bar.high, bar.low, bar.close, bar.volume)
            count = 1
        else:
            current = Bar(
                current.open_time,
                current.open,
                max(current.high, bar.high),
                min(current.low, bar.low),
                bar.close,
                current.volume + bar.volume,
            )
            count += 1
    if current is not None and count == needed:
        out.append(current)
    return out


def last_closed_bucket(open_time, span_ms):
    """Indice dell'ultimo bucket span_ms gia' chiuso alla chiusura della candela 15m."""
    return (open_time + BASE_MS) // span_ms - 1


def _trend_map_1h(bars_1h):
    closes = [bar.close for bar in bars_1h]
    ema20 = ema(closes, 20)
    ema50 = ema(closes, 50)
    out = {}
    for i, bar in enumerate(bars_1h):
        if ema20[i] is None or ema50[i] is None:
            continue
        trend = 0
        if ema20[i] > ema50[i] and bar.close > ema20[i]:
            trend = 1
        elif ema20[i] < ema50[i] and bar.close < ema20[i]:
            trend = -1
        out[bar.open_time // HOUR_MS] = trend
    return out


def _trend_map_4h(bars_4h):
    closes = [bar.close for bar in bars_4h]
    ema20 = ema(closes, 20)
    out = {}
    for i, bar in enumerate(bars_4h):
        if ema20[i] is None:
            continue
        trend = 0
        if bar.close > ema20[i]:
            trend = 1
        elif bar.close < ema20[i]:
            trend = -1
        out[bar.open_time // FOUR_HOUR_MS] = trend
    return out


def build_features(bars):
    closes = [bar.close for bar in bars]
    highs = [bar.high for bar in bars]
    lows = [bar.low for bar in bars]
    map_1h = _trend_map_1h(aggregate(bars, HOUR_MS))
    map_4h = _trend_map_4h(aggregate(bars, FOUR_HOUR_MS))
    return Features(
        bars=bars,
        times=[bar.open_time for bar in bars],
        ema9=ema(closes, 9),
        ema21=ema(closes, 21),
        ema50=ema(closes, 50),
        ema200=ema(closes, 200),
        rsi14=rsi(closes, 14),
        atr14=atr(highs, lows, closes, 14),
        adx14=adx(highs, lows, closes, 14),
        trend_1h=[map_1h.get(last_closed_bucket(bar.open_time, HOUR_MS), 0) for bar in bars],
        trend_4h=[map_4h.get(last_closed_bucket(bar.open_time, FOUR_HOUR_MS), 0) for bar in bars],
    )


# ---------------------------------------------------------------------------
# Segnale, esecuzione simulata, metriche
# ---------------------------------------------------------------------------


def signal_at(features, index, adx_min):
    """Ritorna 1 (long), -1 (short) o 0 alla chiusura della candela index."""
    values = (
        features.ema9[index],
        features.ema21[index],
        features.ema50[index],
        features.ema200[index],
        features.rsi14[index],
        features.atr14[index],
        features.adx14[index],
    )
    if any(value is None for value in values):
        return 0
    ema9_value, ema21_value, ema50_value, ema200_value, rsi_value, atr_value, adx_value = values
    if atr_value <= 0 or adx_value < adx_min:
        return 0
    close = features.bars[index].close
    stretch = (close - ema21_value) / atr_value
    trend_1h = features.trend_1h[index]
    trend_4h = features.trend_4h[index]
    if (
        trend_4h == 1
        and trend_1h == 1
        and close > ema200_value
        and ema9_value > ema21_value > ema50_value
        and 0 <= stretch < PULLBACK_MAX_ATR
        and rsi_value < RSI_OVERBOUGHT
    ):
        return 1
    if (
        trend_4h == -1
        and trend_1h == -1
        and close < ema200_value
        and ema9_value < ema21_value < ema50_value
        and -PULLBACK_MAX_ATR < stretch <= 0
        and rsi_value > RSI_OVERSOLD
    ):
        return -1
    return 0


def open_position(symbol, side, entry_time, raw_open, atr_value, params):
    """Entrata a mercato sull'apertura successiva, con slippage sfavorevole."""
    entry_price = raw_open * (1.0 + SLIPPAGE_RATE * side)
    distance = params.sl_mult * atr_value
    if distance <= 0 or entry_price <= 0:
        return None
    qty = min(RISK_PER_TRADE_USDT / distance, MAX_NOTIONAL_USDT / entry_price)
    return Position(
        symbol=symbol,
        side=side,
        entry_time=entry_time,
        entry_price=entry_price,
        qty=qty,
        stop_price=entry_price - side * distance,
        take_price=entry_price + side * params.rr * distance,
    )


def exit_fill(position, bar):
    """Ritorna (prezzo, motivo) se la candela chiude la posizione, altrimenti None.

    Se stop e take sono toccati nella stessa candela vince lo stop (ipotesi prudente).
    Un gap oltre lo stop viene eseguito all'apertura, non al livello dello stop.
    """
    if position.side == 1:
        if bar.low <= position.stop_price:
            return (min(position.stop_price, bar.open) * (1.0 - SLIPPAGE_RATE), "SL")
        if bar.high >= position.take_price:
            return (position.take_price, "TP")
        return None
    if bar.high >= position.stop_price:
        return (max(position.stop_price, bar.open) * (1.0 + SLIPPAGE_RATE), "SL")
    if bar.low <= position.take_price:
        return (position.take_price, "TP")
    return None


def close_position(position, exit_price, exit_time, reason):
    gross = (exit_price - position.entry_price) * position.qty * position.side
    fees = (position.entry_price + exit_price) * position.qty * TAKER_FEE_RATE
    return Trade(
        symbol=position.symbol,
        side=position.side,
        entry_time=position.entry_time,
        exit_time=exit_time,
        entry_price=position.entry_price,
        exit_price=exit_price,
        qty=position.qty,
        net_pnl=gross - fees,
        fees=fees,
        reason=reason,
    )


def simulate(symbol, features, params, start_ms, end_ms):
    """Simula un simbolo nella finestra [start_ms, end_ms). Una posizione alla volta."""
    low_index = bisect.bisect_left(features.times, start_ms)
    high_index = bisect.bisect_left(features.times, end_ms)
    trades = []
    position = None
    next_entry_index = low_index
    for i in range(low_index, high_index):
        bar = features.bars[i]
        if position is not None:
            fill = exit_fill(position, bar)
            if fill is not None:
                trades.append(close_position(position, fill[0], bar.open_time + BASE_MS, fill[1]))
                position = None
                next_entry_index = i + COOLDOWN_BARS
            continue
        if i < next_entry_index or i + 1 >= high_index:
            continue
        side = signal_at(features, i, params.adx_min)
        if side == 0:
            continue
        next_bar = features.bars[i + 1]
        position = open_position(
            symbol, side, next_bar.open_time, next_bar.open, features.atr14[i], params
        )
    if position is not None and high_index > low_index:
        last_bar = features.bars[high_index - 1]
        price = last_bar.close * (1.0 - SLIPPAGE_RATE * position.side)
        trades.append(close_position(position, price, last_bar.open_time + BASE_MS, "END"))
    return trades


def apply_max_concurrent(trades, max_open):
    """Scarta i trade che aprirebbero oltre max_open posizioni contemporanee."""
    kept = []
    open_exits = []
    for trade in sorted(trades, key=lambda item: (item.entry_time, item.symbol)):
        open_exits = [exit_time for exit_time in open_exits if exit_time > trade.entry_time]
        if len(open_exits) >= max_open:
            continue
        open_exits.append(trade.exit_time)
        kept.append(trade)
    return kept


def metrics(trades, initial_equity):
    ordered = sorted(trades, key=lambda item: (item.exit_time, item.symbol))
    wins = [trade.net_pnl for trade in ordered if trade.net_pnl > 0]
    losses = [-trade.net_pnl for trade in ordered if trade.net_pnl <= 0]
    gross_profit = sum(wins)
    gross_loss = sum(losses)
    if gross_loss > 0:
        profit_factor = gross_profit / gross_loss
    else:
        profit_factor = float("inf") if gross_profit > 0 else 0.0
    avg_win = gross_profit / len(wins) if wins else 0.0
    avg_loss = gross_loss / len(losses) if losses else 0.0
    if avg_loss > 0:
        payoff_ratio = avg_win / avg_loss
    else:
        payoff_ratio = float("inf") if wins else 0.0
    equity = initial_equity
    peak = initial_equity
    max_drawdown = 0.0
    max_drawdown_pct = 0.0
    for trade in ordered:
        equity += trade.net_pnl
        if equity > peak:
            peak = equity
        drawdown = peak - equity
        if drawdown > max_drawdown:
            max_drawdown = drawdown
        if peak > 0 and drawdown / peak * 100.0 > max_drawdown_pct:
            max_drawdown_pct = drawdown / peak * 100.0
    return {
        "trades": len(ordered),
        "win_rate": len(wins) / len(ordered) if ordered else 0.0,
        "profit_factor": profit_factor,
        "payoff_ratio": payoff_ratio,
        "net_pnl_usdt": equity - initial_equity,
        "fees_usdt": sum(trade.fees for trade in ordered),
        "max_drawdown_usdt": max_drawdown,
        "max_drawdown_pct": max_drawdown_pct,
    }


def accept(result):
    return (
        result["profit_factor"] >= ACCEPT_MIN_PROFIT_FACTOR
        and result["trades"] >= ACCEPT_MIN_TRADES
        and result["max_drawdown_pct"] <= ACCEPT_MAX_DRAWDOWN_PCT
        and (
            result["win_rate"] >= ACCEPT_MIN_WIN_RATE
            or result["payoff_ratio"] >= ACCEPT_MIN_PAYOFF_RATIO
        )
    )


# ---------------------------------------------------------------------------
# Walk-forward a finestre scorrevoli
# ---------------------------------------------------------------------------


def build_folds(start_ms, end_ms, train_days, test_days):
    """Ritorna una lista di (train_start, train_end, test_end); il test parte a train_end."""
    folds = []
    cursor = start_ms
    while cursor + (train_days + test_days) * DAY_MS <= end_ms:
        train_end = cursor + train_days * DAY_MS
        folds.append((cursor, train_end, train_end + test_days * DAY_MS))
        cursor += test_days * DAY_MS
    return folds


def run_window(features_by_symbol, params, start_ms, end_ms):
    trades = []
    for symbol in sorted(features_by_symbol):
        trades.extend(simulate(symbol, features_by_symbol[symbol], params, start_ms, end_ms))
    return apply_max_concurrent(trades, MAX_CONCURRENT_POSITIONS)


def select_params(features_by_symbol, train_start, train_end):
    """Sceglie i parametri migliori sul solo In-Sample. Ritorna (params, metriche) o (None, None)."""
    best = None
    for params in GRID:
        result = metrics(
            run_window(features_by_symbol, params, train_start, train_end),
            INITIAL_EQUITY_USDT,
        )
        if result["trades"] < MIN_IN_SAMPLE_TRADES or result["profit_factor"] <= 1.0:
            continue
        key = (min(result["profit_factor"], PROFIT_FACTOR_CAP), result["net_pnl_usdt"])
        if best is None or key > best[0]:
            best = (key, params, result)
    if best is None:
        return (None, None)
    return (best[1], best[2])


def _json_safe(result):
    out = {}
    for key, value in result.items():
        if isinstance(value, float):
            out[key] = "inf" if value == float("inf") else round(value, 4)
        else:
            out[key] = value
    return out


# ---------------------------------------------------------------------------
# Download dati pubblici
# ---------------------------------------------------------------------------


def _get_json(path):
    last_error = "unknown"
    for attempt in range(MAX_RETRIES):
        connection = http.client.HTTPSConnection(API_HOST, timeout=HTTP_TIMEOUT_S)
        try:
            connection.request("GET", path, headers={"Accept": "application/json"})
            response = connection.getresponse()
            body = response.read()
            if response.status == 200:
                return json.loads(body)
            if response.status in (418, 429):
                raise RateLimitedError("rate limit Binance, HTTP " + str(response.status))
            last_error = "HTTP " + str(response.status)
        except (OSError, http.client.HTTPException, ValueError) as exc:
            last_error = type(exc).__name__ + ": " + str(exc)
        finally:
            connection.close()
        time.sleep(2 ** attempt)
    raise RuntimeError("download fallito dopo " + str(MAX_RETRIES) + " tentativi: " + last_error)


def fetch_bars(symbol, start_ms, end_ms):
    """Scarica le candele 15m chiuse con open_time in [start_ms, end_ms)."""
    bars = []
    cursor = start_ms
    while cursor < end_ms:
        query = urlencode({
            "symbol": symbol,
            "interval": "15m",
            "startTime": cursor,
            "endTime": end_ms - 1,
            "limit": KLINES_LIMIT,
        })
        payload = _get_json(KLINES_PATH + "?" + query)
        if not isinstance(payload, list) or not payload:
            break
        for row in payload:
            bars.append(Bar(
                int(row[0]),
                float(row[1]),
                float(row[2]),
                float(row[3]),
                float(row[4]),
                float(row[5]),
            ))
        cursor = int(payload[-1][0]) + BASE_MS
        if len(payload) < KLINES_LIMIT:
            break
        time.sleep(REQUEST_PAUSE_S)
    return bars


def count_gaps(bars):
    return sum(
        1 for i in range(1, len(bars))
        if bars[i].open_time - bars[i - 1].open_time != BASE_MS
    )


def main():
    for symbol in SYMBOLS:
        if not SYMBOL_PATTERN.match(symbol):
            log_event("invalid_symbol", symbol=symbol)
            return 2

    end_ms = (int(time.time() * 1000) // BASE_MS) * BASE_MS
    start_ms = end_ms - (DAYS + WARMUP_DAYS) * DAY_MS
    min_bars = (WARMUP_DAYS + TRAIN_DAYS + TEST_DAYS) * BARS_PER_DAY

    features_by_symbol = {}
    for symbol in SYMBOLS:
        bars = fetch_bars(symbol, start_ms, end_ms)
        if len(bars) < min_bars:
            log_event("symbol_skipped", symbol=symbol, bars_15m=len(bars), min_bars=min_bars)
            continue
        features_by_symbol[symbol] = build_features(bars)
        log_event("data_loaded", symbol=symbol, bars_15m=len(bars), gaps=count_gaps(bars))

    if not features_by_symbol:
        log_event("no_data")
        return 2

    folds = build_folds(start_ms + WARMUP_DAYS * DAY_MS, end_ms, TRAIN_DAYS, TEST_DAYS)
    if not folds:
        log_event("no_folds", days=DAYS, train_days=TRAIN_DAYS, test_days=TEST_DAYS)
        return 2

    oos_trades = []
    for number, (train_start, train_end, test_end) in enumerate(folds, start=1):
        params, in_sample = select_params(features_by_symbol, train_start, train_end)
        if params is None:
            log_event("fold_result", fold=number, status="NO_IS_CANDIDATE")
            continue
        fold_trades = run_window(features_by_symbol, params, train_end, test_end)
        oos_trades.extend(fold_trades)
        log_event(
            "fold_result",
            fold=number,
            status="TESTED",
            params=params._asdict(),
            in_sample=_json_safe(in_sample),
            out_of_sample=_json_safe(metrics(fold_trades, INITIAL_EQUITY_USDT)),
        )

    summary = metrics(oos_trades, INITIAL_EQUITY_USDT)
    by_symbol = {}
    for trade in oos_trades:
        entry = by_symbol.setdefault(trade.symbol, {"trades": 0, "net_pnl_usdt": 0.0})
        entry["trades"] += 1
        entry["net_pnl_usdt"] = round(entry["net_pnl_usdt"] + trade.net_pnl, 4)

    log_event(
        "walk_forward_summary",
        verdict="ACCEPTED" if accept(summary) else "REJECTED",
        folds=len(folds),
        symbols=sorted(features_by_symbol),
        out_of_sample=_json_safe(summary),
        by_symbol=by_symbol,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
