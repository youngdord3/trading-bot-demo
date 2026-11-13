"""Ricerca multi-famiglia su 1h + filtro 4h (Passo A.7): Donchian Breakout e RSI Pullback.

Modulo di sola ricerca: nessun accesso al database, nessun segreto, nessun ordine.
Scarica una sola volta le candele 1h da fapi.binance.com (365g + 30g warmup = 9480 barre
per simbolo), costruisce il filtro causale 4h senza look-ahead e valuta in walk-forward
scorrevole (180g train / 60g test x 3 fold, 8 combinazioni per famiglia):
  - donchian_fixed:    Breakout canale di Donchian 1h in trend 4h, SL/TP fissi su ATR
  - donchian_trailing: Breakout canale di Donchian 1h in trend 4h, SL iniziale + trailing ATR
  - rsi_mr:            Mean-reversion su pullback RSI 1h in direzione del trend 4h + EMA200
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

HOUR_MS = 60 * 60 * 1000
FOUR_HOUR_MS = 4 * HOUR_MS
DAY_MS = 24 * HOUR_MS
BARS_PER_DAY = DAY_MS // HOUR_MS

SYMBOL_PATTERN = re.compile(r"^[A-Z0-9]{5,20}$")
VALID_MODES = ("donchian_fixed", "donchian_trailing", "rsi_mr")


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
WARMUP_DAYS = _env_int("RS_WARMUP_DAYS", 30)
TRAIN_DAYS = _env_int("RS_TRAIN_DAYS", 180)
TEST_DAYS = _env_int("RS_TEST_DAYS", 60)

INITIAL_EQUITY_USDT = _env_float("RS_INITIAL_EQUITY_USDT", 1000.0)
RISK_FRACTION = _env_float("RS_RISK_FRACTION", 0.01)
MAX_LEVERAGE = _env_float("RS_MAX_LEVERAGE", 2.0)
TAKER_FEE_RATE = _env_float("RS_TAKER_FEE_RATE", 0.0005)
SLIPPAGE_RATE = _env_float("RS_SLIPPAGE_RATE", 0.0002)
MAX_CONCURRENT_POSITIONS = _env_int("RS_MAX_CONCURRENT_POSITIONS", 3)
COOLDOWN_BARS = _env_int("RS_COOLDOWN_BARS", 2)
MIN_IN_SAMPLE_TRADES = _env_int("RS_MIN_IN_SAMPLE_TRADES", 20)

RISK_PER_TRADE_USDT = INITIAL_EQUITY_USDT * RISK_FRACTION
MAX_NOTIONAL_USDT = INITIAL_EQUITY_USDT * MAX_LEVERAGE

DONCHIAN_SL_MULT = 1.5
MAX_BREAKOUT_ATR = 1.5
RSI_MAX_ADX = 45.0
RSI_EXIT_LONG = 68.0
RSI_EXIT_SHORT = 32.0
PROFIT_FACTOR_CAP = 10.0

# Criteri di accettazione Out-of-Sample fissati a priori (invariati).
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


class SignalParams(NamedTuple):
    mode: str
    param_a: float
    param_b: float
    exit_mult: float


class Position(NamedTuple):
    symbol: str
    side: int
    entry_time: int
    entry_price: float
    qty: float
    stop_price: float
    take_price: float
    trail_distance: float
    best_price: float


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
    ema50: list
    ema200: list
    rsi14: list
    atr14: list
    adx14: list
    donchian_high: dict
    donchian_low: dict
    trend_4h: list


DONCHIAN_PERIODS = (20, 48)


def build_grid(mode):
    """Restituisce le 8 combinazioni della famiglia richiesta."""
    if mode in ("donchian_fixed", "donchian_trailing"):
        return [
            SignalParams(mode, float(dc_period), adx_min, exit_mult)
            for dc_period in DONCHIAN_PERIODS
            for adx_min in (18.0, 25.0)
            for exit_mult in (2.0, 3.0)
        ]
    if mode == "rsi_mr":
        return [
            SignalParams(mode, rsi_thresh, sl_mult, exit_mult)
            for rsi_thresh in (35.0, 40.0)
            for sl_mult in (1.5, 2.0)
            for exit_mult in (2.0, 3.0)
        ]
    raise ValueError("INVALID_MODE: " + str(mode))


def params_to_dict(params):
    if params.mode in ("donchian_fixed", "donchian_trailing"):
        return {
            "mode": params.mode,
            "dc_period": int(params.param_a),
            "adx_min": params.param_b,
            "sl_mult": DONCHIAN_SL_MULT,
            "exit_mult": params.exit_mult,
        }
    return {
        "mode": params.mode,
        "rsi_thresh": params.param_a,
        "sl_mult": params.param_b,
        "exit_mult": params.exit_mult,
    }


class RateLimitedError(RuntimeError):
    """Binance ha risposto 418/429: interruzione immediata senza retry."""


def log_event(event, **fields):
    record = {"event": event}
    record.update(fields)
    print(json.dumps(record, sort_keys=True), flush=True)


# ---------------------------------------------------------------------------
# Indicatori puri causali (nessun look-ahead)
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


def donchian_bounds(highs, lows, period):
    """Canale di Donchian sulle `period` candele PRECEDENTI [i - period, i).

    Esclude la barra i per consentire il test causale `close[i] > upper[i]`.
    """
    count = len(highs)
    upper = [None] * count
    lower = [None] * count
    if period <= 0 or count <= period:
        return upper, lower
    for i in range(period, count):
        upper[i] = max(highs[i - period:i])
        lower[i] = min(lows[i - period:i])
    return upper, lower


# ---------------------------------------------------------------------------
# Aggregazione 1h -> 4h senza look-ahead
# ---------------------------------------------------------------------------


def aggregate_4h(bars_1h):
    """Aggrega candele 1h in candele 4h complete (4 barre 1h per bucket)."""
    needed = FOUR_HOUR_MS // HOUR_MS
    out = []
    bucket = None
    current = None
    count = 0
    for bar in bars_1h:
        key = bar.open_time // FOUR_HOUR_MS
        if key != bucket:
            if current is not None and count == needed:
                out.append(current)
            bucket = key
            current = Bar(key * FOUR_HOUR_MS, bar.open, bar.high, bar.low, bar.close, bar.volume)
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


def last_closed_4h_bucket(open_time_1h):
    """Indice dell'ultimo bucket 4h chiuso alla chiusura della candela 1h."""
    return (open_time_1h + HOUR_MS) // FOUR_HOUR_MS - 1


def _trend_map_4h(bars_4h):
    closes = [bar.close for bar in bars_4h]
    ema20 = ema(closes, 20)
    ema50 = ema(closes, 50)
    out = {}
    for i, bar in enumerate(bars_4h):
        if ema20[i] is None or ema50[i] is None:
            continue
        trend = 0
        if bar.close > ema20[i] and ema20[i] >= ema50[i]:
            trend = 1
        elif bar.close < ema20[i] and ema20[i] <= ema50[i]:
            trend = -1
        out[bar.open_time // FOUR_HOUR_MS] = trend
    return out


def build_features(bars_1h):
    closes = [bar.close for bar in bars_1h]
    highs = [bar.high for bar in bars_1h]
    lows = [bar.low for bar in bars_1h]
    map_4h = _trend_map_4h(aggregate_4h(bars_1h))
    dc_high = {}
    dc_low = {}
    for period in DONCHIAN_PERIODS:
        upper, lower = donchian_bounds(highs, lows, period)
        dc_high[period] = upper
        dc_low[period] = lower
    return Features(
        bars=bars_1h,
        times=[bar.open_time for bar in bars_1h],
        ema50=ema(closes, 50),
        ema200=ema(closes, 200),
        rsi14=rsi(closes, 14),
        atr14=atr(highs, lows, closes, 14),
        adx14=adx(highs, lows, closes, 14),
        donchian_high=dc_high,
        donchian_low=dc_low,
        trend_4h=[map_4h.get(last_closed_4h_bucket(bar.open_time), 0) for bar in bars_1h],
    )


# ---------------------------------------------------------------------------
# Segnali, diagnosi a imbuto ed esecuzione simulata
# ---------------------------------------------------------------------------


def evaluate_bar_stage(features, index, params):
    """Classifica la barra `index` nello stadio dell'imbuto e ritorna (stage, side).

    Possibili stage:
      WARMUP -> NO_TREND_4H -> REGIME_FILTERED -> NO_TRIGGER -> STRETCH_FILTERED -> SIGNAL
    """
    if index < 1:
        return ("WARMUP", 0)
    atr_val = features.atr14[index]
    adx_val = features.adx14[index]
    ema50_val = features.ema50[index]
    ema200_val = features.ema200[index]
    rsi_val = features.rsi14[index]
    rsi_prev = features.rsi14[index - 1]
    if any(v is None for v in (atr_val, adx_val, ema50_val, ema200_val, rsi_val, rsi_prev)) or atr_val <= 0:
        return ("WARMUP", 0)

    if params.mode in ("donchian_fixed", "donchian_trailing"):
        dc_period = int(params.param_a)
        upper = features.donchian_high[dc_period][index]
        lower = features.donchian_low[dc_period][index]
        if upper is None or lower is None:
            return ("WARMUP", 0)
        trend_4h = features.trend_4h[index]
        if trend_4h == 0:
            return ("NO_TREND_4H", 0)
        if adx_val < params.param_b:
            return ("REGIME_FILTERED", 0)
        close = features.bars[index].close
        if trend_4h == 1 and close > ema50_val and close > upper:
            if (close - upper) / atr_val > MAX_BREAKOUT_ATR:
                return ("STRETCH_FILTERED", 0)
            return ("SIGNAL", 1)
        if trend_4h == -1 and close < ema50_val and close < lower:
            if (lower - close) / atr_val > MAX_BREAKOUT_ATR:
                return ("STRETCH_FILTERED", 0)
            return ("SIGNAL", -1)
        return ("NO_TRIGGER", 0)

    # Modalita' rsi_mr: pullback RSI in trend 4h + struttura EMA200 1h
    trend_4h = features.trend_4h[index]
    if trend_4h == 0:
        return ("NO_TREND_4H", 0)
    bar = features.bars[index]
    if adx_val > RSI_MAX_ADX:
        return ("REGIME_FILTERED", 0)
    if trend_4h == 1 and bar.close <= ema200_val:
        return ("REGIME_FILTERED", 0)
    if trend_4h == -1 and bar.close >= ema200_val:
        return ("REGIME_FILTERED", 0)

    rsi_thresh = params.param_a
    if trend_4h == 1 and min(rsi_prev, rsi_val) <= rsi_thresh and bar.close > bar.open:
        if (bar.close - bar.low) / atr_val > MAX_BREAKOUT_ATR * 2.0:
            return ("STRETCH_FILTERED", 0)
        return ("SIGNAL", 1)
    short_thresh = 100.0 - rsi_thresh
    if trend_4h == -1 and max(rsi_prev, rsi_val) >= short_thresh and bar.close < bar.open:
        if (bar.high - bar.close) / atr_val > MAX_BREAKOUT_ATR * 2.0:
            return ("STRETCH_FILTERED", 0)
        return ("SIGNAL", -1)
    return ("NO_TRIGGER", 0)


def signal_at(features, index, params):
    _, side = evaluate_bar_stage(features, index, params)
    return side


def funnel_counts(features_by_symbol, params, start_ms, end_ms):
    counts = {
        "bars": 0,
        "WARMUP": 0,
        "NO_TREND_4H": 0,
        "REGIME_FILTERED": 0,
        "NO_TRIGGER": 0,
        "STRETCH_FILTERED": 0,
        "SIGNAL": 0,
    }
    for symbol in sorted(features_by_symbol):
        feat = features_by_symbol[symbol]
        low_idx = bisect.bisect_left(feat.times, start_ms)
        high_idx = bisect.bisect_left(feat.times, end_ms)
        for i in range(low_idx, high_idx):
            counts["bars"] += 1
            stage, _ = evaluate_bar_stage(feat, i, params)
            counts[stage] += 1
    return counts


def open_position(symbol, side, entry_time, raw_open, atr_value, params):
    """Entrata a mercato sull'apertura successiva con slippage sfavorevole."""
    entry_price = raw_open * (1.0 + SLIPPAGE_RATE * side)
    sl_mult = DONCHIAN_SL_MULT if params.mode in ("donchian_fixed", "donchian_trailing") else params.param_b
    stop_dist = sl_mult * atr_value
    if stop_dist <= 0 or entry_price <= 0:
        return None
    qty = min(RISK_PER_TRADE_USDT / stop_dist, MAX_NOTIONAL_USDT / entry_price)
    stop_price = entry_price - side * stop_dist
    if params.mode == "donchian_trailing":
        trail_dist = params.exit_mult * atr_value
        take_price = 0.0
    else:
        trail_dist = 0.0
        take_price = entry_price + side * params.exit_mult * stop_dist
    return Position(
        symbol=symbol,
        side=side,
        entry_time=entry_time,
        entry_price=entry_price,
        qty=qty,
        stop_price=stop_price,
        take_price=take_price,
        trail_distance=trail_dist,
        best_price=entry_price,
    )


def exit_fill(position, bar):
    """Verifica stop/take sulla barra corrente prima di qualsiasi update trailing."""
    if position.side == 1:
        if bar.low <= position.stop_price:
            reason = "TRAIL" if position.trail_distance > 0 and position.stop_price > position.entry_price else "SL"
            return (min(position.stop_price, bar.open) * (1.0 - SLIPPAGE_RATE), reason)
        if position.take_price > 0 and bar.high >= position.take_price:
            return (position.take_price, "TP")
        return None
    if bar.high >= position.stop_price:
        reason = "TRAIL" if position.trail_distance > 0 and position.stop_price < position.entry_price else "SL"
        return (max(position.stop_price, bar.open) * (1.0 + SLIPPAGE_RATE), reason)
    if position.take_price > 0 and bar.low <= position.take_price:
        return (position.take_price, "TP")
    return None


def advance_trailing(position, bar):
    """Aggiorna il trailing stop a fine candela (vale solo dalla candela successiva)."""
    if position.trail_distance <= 0:
        return position
    if position.side == 1:
        best = max(position.best_price, bar.high)
        new_stop = max(position.stop_price, best - position.trail_distance)
        return position._replace(stop_price=new_stop, best_price=best)
    best = min(position.best_price, bar.low)
    new_stop = min(position.stop_price, best + position.trail_distance)
    return position._replace(stop_price=new_stop, best_price=best)


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
    """Simula un simbolo nella finestra [start_ms, end_ms). Nessun look-ahead."""
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
                trades.append(close_position(position, fill[0], bar.open_time + HOUR_MS, fill[1]))
                position = None
                next_entry_index = i + COOLDOWN_BARS
                continue
            if params.mode == "rsi_mr" and features.rsi14[i] is not None and i + 1 < high_index:
                if (position.side == 1 and features.rsi14[i] >= RSI_EXIT_LONG) or (
                    position.side == -1 and features.rsi14[i] <= RSI_EXIT_SHORT
                ):
                    next_bar = features.bars[i + 1]
                    exit_px = next_bar.open * (1.0 - SLIPPAGE_RATE * position.side)
                    trades.append(close_position(position, exit_px, next_bar.open_time, "RSI_EXIT"))
                    position = None
                    next_entry_index = i + 1 + COOLDOWN_BARS
                    continue
            position = advance_trailing(position, bar)
            continue
        if i < next_entry_index or i + 1 >= high_index:
            continue
        side = signal_at(features, i, params)
        if side == 0:
            continue
        next_bar = features.bars[i + 1]
        position = open_position(
            symbol, side, next_bar.open_time, next_bar.open, features.atr14[i], params
        )
    if position is not None and high_index > low_index:
        last_bar = features.bars[high_index - 1]
        price = last_bar.close * (1.0 - SLIPPAGE_RATE * position.side)
        trades.append(close_position(position, price, last_bar.open_time + HOUR_MS, "END"))
    return trades


def apply_max_concurrent(trades, max_open):
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


def evaluate_acceptance(result):
    reasons = []
    if result["trades"] < ACCEPT_MIN_TRADES:
        reasons.append("TOO_FEW_TRADES")
    if result["profit_factor"] < ACCEPT_MIN_PROFIT_FACTOR:
        reasons.append("PROFIT_FACTOR_TOO_LOW")
    if result["max_drawdown_pct"] > ACCEPT_MAX_DRAWDOWN_PCT:
        reasons.append("DRAWDOWN_TOO_HIGH")
    if result["win_rate"] < ACCEPT_MIN_WIN_RATE and result["payoff_ratio"] < ACCEPT_MIN_PAYOFF_RATIO:
        reasons.append("NO_EDGE_PROFILE")
    return (not reasons, reasons)


def build_folds(start_ms, end_ms, train_days, test_days):
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


def classify_in_sample(funnel, is_metrics):
    if funnel["SIGNAL"] == 0:
        return "ZERO_SIGNALS"
    if is_metrics["trades"] < MIN_IN_SAMPLE_TRADES:
        return "TOO_FEW_TRADES_IN_SAMPLE"
    if is_metrics["profit_factor"] <= 1.0 or is_metrics["net_pnl_usdt"] <= 0.0:
        return "NO_EDGE_IN_SAMPLE"
    return "QUALIFIED"


def select_params(features_by_symbol, mode, train_start, train_end):
    """Valuta la griglia sul fold IS, restituendo (best_params, best_metrics, outcomes_list)."""
    best = None
    outcomes = []
    for params in build_grid(mode):
        funnel = funnel_counts(features_by_symbol, params, train_start, train_end)
        result = metrics(
            run_window(features_by_symbol, params, train_start, train_end),
            INITIAL_EQUITY_USDT,
        )
        outcome = classify_in_sample(funnel, result)
        outcomes.append((params, funnel, result, outcome))
        if outcome != "QUALIFIED":
            continue
        key = (min(result["profit_factor"], PROFIT_FACTOR_CAP), result["net_pnl_usdt"])
        if best is None or key > best[0]:
            best = (key, params, result)
    if best is None:
        return (None, None, outcomes)
    return (best[1], best[2], outcomes)


def _json_safe(result):
    out = {}
    for key, value in result.items():
        if isinstance(value, float):
            out[key] = "inf" if value == float("inf") else round(value, 4)
        else:
            out[key] = value
    return out


# ---------------------------------------------------------------------------
# Download candele pubbliche 1h
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


def fetch_bars_1h(symbol, start_ms, end_ms):
    bars = []
    cursor = start_ms
    while cursor < end_ms:
        query = urlencode({
            "symbol": symbol,
            "interval": "1h",
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
        cursor = int(payload[-1][0]) + HOUR_MS
        if len(payload) < KLINES_LIMIT:
            break
        time.sleep(REQUEST_PAUSE_S)
    return bars


def count_gaps_1h(bars):
    return sum(
        1 for i in range(1, len(bars))
        if bars[i].open_time - bars[i - 1].open_time != HOUR_MS
    )


def evaluate_mode(features_by_symbol, folds, mode):
    outcome_counts = {}
    total_combinations = 0
    oos_trades = []

    for number, (train_start, train_end, test_end) in enumerate(folds, start=1):
        params, in_sample, outcomes = select_params(features_by_symbol, mode, train_start, train_end)
        fold_outcomes = {}
        for _, _, _, outcome in outcomes:
            total_combinations += 1
            outcome_counts[outcome] = outcome_counts.get(outcome, 0) + 1
            fold_outcomes[outcome] = fold_outcomes.get(outcome, 0) + 1

        if params is None:
            log_event(
                "fold_result",
                mode=mode,
                fold=number,
                status="NO_IS_CANDIDATE",
                outcomes=fold_outcomes,
            )
            continue

        fold_trades = run_window(features_by_symbol, params, train_end, test_end)
        oos_trades.extend(fold_trades)
        log_event(
            "fold_result",
            mode=mode,
            fold=number,
            status="EVALUATED",
            params=params_to_dict(params),
            outcomes=fold_outcomes,
            in_sample=_json_safe(in_sample),
            out_of_sample=_json_safe(metrics(fold_trades, INITIAL_EQUITY_USDT)),
        )

    log_event(
        "diagnosis_summary",
        mode=mode,
        combinations=total_combinations,
        outcomes=outcome_counts,
    )

    summary = metrics(oos_trades, INITIAL_EQUITY_USDT)
    accepted, reasons = evaluate_acceptance(summary)
    by_symbol = {}
    for trade in oos_trades:
        entry = by_symbol.setdefault(trade.symbol, {"trades": 0, "net_pnl_usdt": 0.0})
        entry["trades"] += 1
        entry["net_pnl_usdt"] = round(entry["net_pnl_usdt"] + trade.net_pnl, 4)

    verdict = "ACCEPTED" if accepted else "REJECTED"
    log_event(
        "walk_forward_summary",
        mode=mode,
        verdict=verdict,
        reasons=reasons,
        folds=len(folds),
        symbols=sorted(features_by_symbol),
        out_of_sample=_json_safe(summary),
        by_symbol=by_symbol,
    )
    return verdict, summary


def parse_modes(argv):
    if len(argv) <= 1 or argv[1] == "all":
        return list(VALID_MODES)
    requested = [item.strip() for item in argv[1].split(",") if item.strip()]
    for mode in requested:
        if mode not in VALID_MODES:
            raise ValueError("INVALID_MODE: " + mode)
    return requested


def main(argv=None):
    argv = argv if argv is not None else sys.argv
    try:
        modes = parse_modes(argv)
    except ValueError as exc:
        log_event("invalid_mode", error=str(exc), valid_modes=list(VALID_MODES))
        return 2

    for symbol in SYMBOLS:
        if not SYMBOL_PATTERN.match(symbol):
            log_event("invalid_symbol", symbol=symbol)
            return 2

    end_ms = (int(time.time() * 1000) // HOUR_MS) * HOUR_MS
    start_ms = end_ms - (DAYS + WARMUP_DAYS) * DAY_MS
    min_bars = (WARMUP_DAYS + TRAIN_DAYS + TEST_DAYS) * BARS_PER_DAY

    features_by_symbol = {}
    for symbol in SYMBOLS:
        bars = fetch_bars_1h(symbol, start_ms, end_ms)
        if len(bars) < min_bars:
            log_event("symbol_skipped", symbol=symbol, bars_1h=len(bars), min_bars=min_bars)
            continue
        features_by_symbol[symbol] = build_features(bars)
        log_event("data_loaded", symbol=symbol, bars_1h=len(bars), gaps=count_gaps_1h(bars))

    if not features_by_symbol:
        log_event("no_data")
        return 2

    folds = build_folds(start_ms + WARMUP_DAYS * DAY_MS, end_ms, TRAIN_DAYS, TEST_DAYS)
    if not folds:
        log_event("no_folds", days=DAYS, train_days=TRAIN_DAYS, test_days=TEST_DAYS)
        return 2

    accepted_modes = []
    for mode in modes:
        verdict, _ = evaluate_mode(features_by_symbol, folds, mode)
        if verdict == "ACCEPTED":
            accepted_modes.append(mode)

    log_event(
        "research_signals_summary",
        modes_tested=modes,
        accepted_modes=accepted_modes,
        result="CANDIDATE_FOUND" if accepted_modes else "NO_STRATEGY_ACCEPTED",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
