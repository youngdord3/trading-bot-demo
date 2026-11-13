"""Indicatori tecnici puri su Decimal. Nessun I/O.

Ogni funzione restituisce una lista lunga quanto l'input: out[i] usa solo dati
fino all'indice i (nessun lookahead). None indica il periodo di warm-up.
"""
from decimal import Decimal

ZERO = Decimal(0)
FIFTY = Decimal(50)
HUNDRED = Decimal(100)


def _require_period(period: int) -> None:
    if not isinstance(period, int) or period < 1:
        raise ValueError("INVALID_PERIOD")


def _require_same_length(*series) -> None:
    if len({len(s) for s in series}) != 1:
        raise ValueError("SERIES_LENGTH_MISMATCH")


def sma(values: list[Decimal], period: int) -> list:
    _require_period(period)
    out = [None] * len(values)
    if len(values) < period:
        return out
    window = sum(values[:period], ZERO)
    out[period - 1] = window / period
    for i in range(period, len(values)):
        window += values[i] - values[i - period]
        out[i] = window / period
    return out


def ema(values: list[Decimal], period: int) -> list:
    """Seed = SMA dei primi `period` valori, come strategy.compute_ema."""
    _require_period(period)
    out = [None] * len(values)
    if len(values) < period:
        return out
    k = Decimal(2) / Decimal(period + 1)
    prev = sum(values[:period], ZERO) / Decimal(period)
    out[period - 1] = prev
    for i in range(period, len(values)):
        prev = (values[i] - prev) * k + prev
        out[i] = prev
    return out


def true_range(highs: list[Decimal], lows: list[Decimal], closes: list[Decimal]) -> list[Decimal]:
    _require_same_length(highs, lows, closes)
    out = []
    for i in range(len(highs)):
        span = highs[i] - lows[i]
        if i == 0:
            out.append(span)
        else:
            prev_close = closes[i - 1]
            out.append(max(span, abs(highs[i] - prev_close), abs(lows[i] - prev_close)))
    return out


def atr(highs: list[Decimal], lows: list[Decimal], closes: list[Decimal], period: int = 14) -> list:
    """Average True Range con smoothing di Wilder."""
    _require_period(period)
    tr = true_range(highs, lows, closes)
    out = [None] * len(tr)
    if len(tr) < period:
        return out
    prev = sum(tr[:period], ZERO) / period
    out[period - 1] = prev
    for i in range(period, len(tr)):
        prev = (prev * (period - 1) + tr[i]) / period
        out[i] = prev
    return out


def _directional_index(sm_plus: Decimal, sm_minus: Decimal, sm_tr: Decimal) -> Decimal:
    if sm_tr == 0:
        return ZERO
    plus_di = HUNDRED * sm_plus / sm_tr
    minus_di = HUNDRED * sm_minus / sm_tr
    total = plus_di + minus_di
    if total == 0:
        return ZERO
    return HUNDRED * abs(plus_di - minus_di) / total


def adx(highs: list[Decimal], lows: list[Decimal], closes: list[Decimal], period: int = 14) -> list:
    """Average Directional Index di Wilder. Primo valore all'indice 2*period-1."""
    _require_period(period)
    tr = true_range(highs, lows, closes)
    n = len(tr)
    out = [None] * n
    if n < 2 * period:
        return out
    plus_dm = [ZERO] * n
    minus_dm = [ZERO] * n
    for i in range(1, n):
        up = highs[i] - highs[i - 1]
        down = lows[i - 1] - lows[i]
        if up > down and up > 0:
            plus_dm[i] = up
        elif down > up and down > 0:
            minus_dm[i] = down
    sm_tr = sum(tr[1:period + 1], ZERO)
    sm_plus = sum(plus_dm[1:period + 1], ZERO)
    sm_minus = sum(minus_dm[1:period + 1], ZERO)
    dx = [None] * n
    dx[period] = _directional_index(sm_plus, sm_minus, sm_tr)
    for i in range(period + 1, n):
        sm_tr = sm_tr - sm_tr / period + tr[i]
        sm_plus = sm_plus - sm_plus / period + plus_dm[i]
        sm_minus = sm_minus - sm_minus / period + minus_dm[i]
        dx[i] = _directional_index(sm_plus, sm_minus, sm_tr)
    first = 2 * period - 1
    prev = sum(dx[period:first + 1], ZERO) / period
    out[first] = prev
    for i in range(first + 1, n):
        prev = (prev * (period - 1) + dx[i]) / period
        out[i] = prev
    return out


def _rsi_value(avg_gain: Decimal, avg_loss: Decimal) -> Decimal:
    if avg_loss == 0:
        return HUNDRED if avg_gain > 0 else FIFTY
    return HUNDRED - HUNDRED / (1 + avg_gain / avg_loss)


def rsi(values: list[Decimal], period: int = 14) -> list:
    """Relative Strength Index di Wilder. Primo valore all'indice `period`."""
    _require_period(period)
    n = len(values)
    out = [None] * n
    if n < period + 1:
        return out
    gains, losses = ZERO, ZERO
    for i in range(1, period + 1):
        change = values[i] - values[i - 1]
        if change > 0:
            gains += change
        else:
            losses -= change
    avg_gain = gains / period
    avg_loss = losses / period
    out[period] = _rsi_value(avg_gain, avg_loss)
    for i in range(period + 1, n):
        change = values[i] - values[i - 1]
        gain = change if change > 0 else ZERO
        loss = -change if change < 0 else ZERO
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        out[i] = _rsi_value(avg_gain, avg_loss)
    return out
