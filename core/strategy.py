"""Signal engine EMA crossover. Funzioni pure, nessun I/O."""
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum


class Signal(Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


@dataclass(frozen=True)
class Candle:
    open_time_ms: int
    close_time_ms: int
    open: Decimal
    close: Decimal


def parse_closed_candles(raw: list, server_now_ms: int, interval_ms: int,
                         require_contiguous: bool = True) -> list[Candle]:
    """Tiene solo candele chiuse; verifica ordine e (opzionale) continuità."""
    out = []
    for k in raw:
        if not isinstance(k, list) or len(k) < 7:
            raise ValueError("INVALID_KLINE")
        c = Candle(int(k[0]), int(k[6]), Decimal(str(k[1])), Decimal(str(k[4])))
        if c.close_time_ms < server_now_ms:
            out.append(c)
    for a, b in zip(out, out[1:]):
        if b.open_time_ms <= a.open_time_ms:
            raise ValueError("KLINES_NOT_SORTED")
        if require_contiguous and b.open_time_ms - a.open_time_ms != interval_ms:
            raise ValueError("KLINES_GAP")
    return out


def compute_ema(values: list[Decimal], period: int) -> list[Decimal]:
    """ema[j] si riferisce a values[period-1+j]. Seed = SMA."""
    if period < 1 or len(values) < period:
        return []
    k = Decimal(2) / Decimal(period + 1)
    ema = [sum(values[:period]) / Decimal(period)]
    for v in values[period:]:
        ema.append((v - ema[-1]) * k + ema[-1])
    return ema


def evaluate_crossover(closes: list[Decimal], fast: int = 9, slow: int = 21) -> Signal:
    if fast >= slow:
        raise ValueError("FAST_MUST_BE_LESS_THAN_SLOW")
    if len(closes) < slow + 1:
        return Signal.HOLD
    f = compute_ema(closes, fast)[slow - fast:]   # ora f[i] e s[i] = stessa candela
    s = compute_ema(closes, slow)
    prev, curr = f[-2] - s[-2], f[-1] - s[-1]
    if prev <= 0 < curr:
        return Signal.BUY
    if prev >= 0 > curr:
        return Signal.SELL
    return Signal.HOLD


STRATEGY_REASON = "EMA_9_21_CROSSOVER"
