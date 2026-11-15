"""Signal engine EMA crossover e D1 TSMOM + ER + Funding. Funzioni pure, nessun I/O."""
import bisect
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import NamedTuple

from indicators import ZERO, adx, atr, ema


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
    high: Decimal = ZERO
    low: Decimal = ZERO


class TsmomParams(NamedTuple):
    ema_fast: int = 20
    ema_slow: int = 50
    atr_period: int = 14
    adx_period: int = 14
    er_period: int = 10
    dc_period: int = 10
    ret_short: int = 15
    ret_long: int = 30
    adx_min: Decimal = Decimal("18")
    er_min: Decimal = Decimal("0.30")
    ret_short_min: Decimal = Decimal("0.03")
    ret_long_min: Decimal = Decimal("0.05")
    max_breakout_atr: Decimal = Decimal("1.8")
    sl_atr_mult: Decimal = Decimal("1.5")
    trail_atr_mult: Decimal = Decimal("2.25")
    ratchet_r1: Decimal = Decimal("2.5")
    ratchet_mult1: Decimal = Decimal("0.80")
    ratchet_r2: Decimal = Decimal("4.0")
    ratchet_mult2: Decimal = Decimal("0.60")
    funding_crowded_long: Decimal = Decimal("0.00030")
    funding_crowded_short: Decimal = Decimal("-0.00010")
    funding_sum3_mult: Decimal = Decimal("2.5")


class TrailingState(NamedTuple):
    side: str
    entry_price: Decimal
    stop_price: Decimal
    trail_distance: Decimal
    best_price: Decimal
    initial_risk_distance: Decimal = ZERO


class StrategyEvaluation(NamedTuple):
    signal: Signal
    atr14: Decimal | None
    reason: str
    er10: Decimal | None = None


def parse_closed_candles(raw: list, server_now_ms: int, interval_ms: int,
                         require_contiguous: bool = True) -> list[Candle]:
    """Tiene solo candele chiuse; verifica ordine e (opzionale) continuità."""
    out = []
    for k in raw:
        if not isinstance(k, list) or len(k) < 7:
            raise ValueError("INVALID_KLINE")
        c = Candle(
            open_time_ms=int(k[0]),
            close_time_ms=int(k[6]),
            open=Decimal(str(k[1])),
            close=Decimal(str(k[4])),
            high=Decimal(str(k[2])),
            low=Decimal(str(k[3])),
        )
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
    ema_vals = [sum(values[:period]) / Decimal(period)]
    for v in values[period:]:
        ema_vals.append((v - ema_vals[-1]) * k + ema_vals[-1])
    return ema_vals


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


def efficiency_ratio(closes: list[Decimal], period: int = 10) -> list[Decimal | None]:
    """Kaufman Efficiency Ratio ER[i] = |close[i] - close[i-period]| / sum(|diff|)."""
    if period <= 0:
        raise ValueError("INVALID_PERIOD")
    count = len(closes)
    out: list[Decimal | None] = [None] * count
    if count <= period:
        return out
    for i in range(period, count):
        net_move = abs(closes[i] - closes[i - period])
        path_move = sum((abs(closes[k] - closes[k - 1]) for k in range(i - period + 1, i + 1)), ZERO)
        out[i] = (net_move / path_move) if path_move > ZERO else ZERO
    return out


def _funding_allows(side: int, funding_points: list[tuple[int, Decimal]],
                    as_of_ms: int, params: TsmomParams) -> bool:
    """Blocca entrate quando l'ultimo funding o le ultime 24h indicano affollamento."""
    if not funding_points:
        return True
    times = [pt[0] for pt in funding_points]
    idx = bisect.bisect_right(times, as_of_ms) - 1
    if idx < 0:
        return True
    latest = funding_points[idx][1]
    sum3 = sum((pt[1] for pt in funding_points[max(0, idx - 2):idx + 1]), ZERO)
    if side == 1:
        limit3 = params.funding_crowded_long * params.funding_sum3_mult
        return latest < params.funding_crowded_long and sum3 < limit3
    if side == -1:
        limit3 = params.funding_crowded_short * params.funding_sum3_mult
        return latest > params.funding_crowded_short and sum3 > limit3
    return False


def evaluate_tsmom_erf(
    candles: list[Candle],
    funding_points: list[tuple[int, Decimal]],
    params: TsmomParams = TsmomParams(),
) -> StrategyEvaluation:
    """Valuta la strategia daily TSMOM + ER10 + Funding Anti-Crowding sull'ultima candela chiusa."""
    min_bars = max(params.ema_slow, params.ret_long + 1, params.adx_period * 2, params.dc_period + 1)
    if len(candles) < min_bars:
        return StrategyEvaluation(Signal.HOLD, None, "WARMUP", None)

    highs = [c.high for c in candles]
    lows = [c.low for c in candles]
    closes = [c.close for c in candles]

    atr14 = atr(highs, lows, closes, params.atr_period)[-1]
    adx14 = adx(highs, lows, closes, params.adx_period)[-1]
    ema20 = ema(closes, params.ema_fast)[-1]
    ema50 = ema(closes, params.ema_slow)[-1]
    er10 = efficiency_ratio(closes, params.er_period)[-1]
    if any(v is None for v in (atr14, adx14, ema20, ema50, er10)) or atr14 <= ZERO:
        return StrategyEvaluation(Signal.HOLD, None, "WARMUP", None)
    if adx14 < params.adx_min or er10 < params.er_min:
        return StrategyEvaluation(Signal.HOLD, atr14, "LOW_TREND_OR_ER", er10)

    close = closes[-1]
    prev15 = closes[-1 - params.ret_short]
    prev30 = closes[-1 - params.ret_long]
    if prev15 <= ZERO or prev30 <= ZERO:
        return StrategyEvaluation(Signal.HOLD, atr14, "INVALID_PRICE", er10)

    ret15 = (close - prev15) / prev15
    ret30 = (close - prev30) / prev30
    upper = max(highs[-1 - params.dc_period:-1])
    lower = min(lows[-1 - params.dc_period:-1])
    as_of_ms = candles[-1].close_time_ms + 1

    candidate = None
    if ret15 > params.ret_short_min and ret30 > params.ret_long_min and close > ema20 >= ema50 and close > upper:
        candidate = (1, (close - upper) / atr14, Signal.BUY)
    elif ret15 < -params.ret_short_min and ret30 < -params.ret_long_min and close < ema20 <= ema50 and close < lower:
        candidate = (-1, (lower - close) / atr14, Signal.SELL)

    if candidate is None:
        return StrategyEvaluation(Signal.HOLD, atr14, "NO_SIGNAL", er10)
    side, breakout_atr, signal = candidate
    if breakout_atr > params.max_breakout_atr:
        return StrategyEvaluation(Signal.HOLD, atr14, "OVEREXTENDED", er10)
    if not _funding_allows(side, funding_points, as_of_ms, params):
        return StrategyEvaluation(Signal.HOLD, atr14, "FUNDING_CROWDED", er10)
    return StrategyEvaluation(signal, atr14, STRATEGY_REASON, er10)


def initial_trailing_state(
    side: str,
    entry_price: Decimal,
    atr14: Decimal,
    params: TsmomParams = TsmomParams(),
    initial_stop_price: Decimal | None = None,
) -> TrailingState:
    """Costruisce lo stato iniziale SL + trailing ATR (con 1R iniziale) all'apertura di una posizione."""
    if side not in ("LONG", "SHORT"):
        raise ValueError(f"INVALID_SIDE: {side}")
    stop_dist = params.sl_atr_mult * atr14
    stop_price = (
        initial_stop_price
        if initial_stop_price is not None
        else (entry_price - stop_dist if side == "LONG" else entry_price + stop_dist)
    )
    risk_dist = abs(entry_price - stop_price)
    if risk_dist <= ZERO:
        risk_dist = stop_dist
    return TrailingState(
        side=side,
        entry_price=entry_price,
        stop_price=stop_price,
        trail_distance=params.trail_atr_mult * atr14,
        best_price=entry_price,
        initial_risk_distance=risk_dist,
    )


def check_bar_exit(
    state: TrailingState,
    candle: Candle,
    slippage: Decimal,
) -> tuple[Decimal, str] | None:
    """Verifica se la candela chiusa tocca lo stop iniziale ('SL') o trailing ('TRAIL')."""
    if state.side == "LONG":
        if candle.low <= state.stop_price:
            reason = "TRAIL" if state.trail_distance > ZERO and state.stop_price > state.entry_price else "SL"
            return min(state.stop_price, candle.open) * (Decimal(1) - slippage), reason
        return None
    if candle.high >= state.stop_price:
        reason = "TRAIL" if state.trail_distance > ZERO and state.stop_price < state.entry_price else "SL"
        return max(state.stop_price, candle.open) * (Decimal(1) + slippage), reason
    return None


def effective_trail_distance(
    state: TrailingState,
    best_price: Decimal,
    params: TsmomParams = TsmomParams(),
) -> Decimal:
    """Applica il profit_ratchet (2.5R -> 0.80x, 4.0R -> 0.60x) rispetto a 1R iniziale."""
    if state.trail_distance <= ZERO or state.initial_risk_distance <= ZERO:
        return state.trail_distance
    favorable = (
        best_price - state.entry_price
        if state.side == "LONG"
        else state.entry_price - best_price
    )
    open_r = favorable / state.initial_risk_distance
    if open_r >= params.ratchet_r2:
        return state.trail_distance * params.ratchet_mult2
    if open_r >= params.ratchet_r1:
        return state.trail_distance * params.ratchet_mult1
    return state.trail_distance


def advance_trailing(
    state: TrailingState,
    candle: Candle,
    params: TsmomParams = TsmomParams(),
) -> TrailingState:
    """Aggiorna il trailing stop a fine candela con ratchet su 1R iniziale e vincolo non-allargamento."""
    if state.trail_distance <= ZERO:
        return state
    if state.side == "LONG":
        best = max(state.best_price, candle.high)
        eff_distance = effective_trail_distance(state, best, params)
        stop = max(state.stop_price, best - eff_distance)
        return state._replace(stop_price=stop, best_price=best)
    if state.side == "SHORT":
        best = min(state.best_price, candle.low)
        eff_distance = effective_trail_distance(state, best, params)
        stop = min(state.stop_price, best + eff_distance)
        return state._replace(stop_price=stop, best_price=best)
    raise ValueError(f"INVALID_SIDE: {state.side}")


def replay_trailing_state(
    initial_state: TrailingState,
    bars_after_entry: list[Candle],
    params: TsmomParams = TsmomParams(),
) -> TrailingState:
    """Ricostruisce deterministicamente il trailing stop lungo le candele 1d post-entrata."""
    state = initial_state
    for bar in bars_after_entry:
        state = advance_trailing(state, bar, params)
    return state


def funding_pnl_between(
    side: str,
    qty: Decimal,
    start_exclusive_ms: int,
    end_inclusive_ms: int,
    funding_points: list[tuple[int, Decimal]],
    mark_price: Decimal,
) -> Decimal:
    """Calcola il cashflow di funding maturato nell'intervallo (start_exclusive_ms, end_inclusive_ms]."""
    if side not in ("LONG", "SHORT") or qty <= ZERO or not funding_points:
        return ZERO
    sign = Decimal(-1) if side == "LONG" else Decimal(1)
    notional = qty * mark_price
    return sum(
        (sign * notional * rate for t_ms, rate in funding_points
         if start_exclusive_ms < t_ms <= end_inclusive_ms),
        ZERO,
    )


STRATEGY_REASON = "D1_TSMOM_ERF_TRAIL"
UNIVERSE_SYMBOLS = (
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "DOGEUSDT", "XRPUSDT",
    "ADAUSDT", "AVAXUSDT", "LINKUSDT", "DOTUSDT", "LTCUSDT", "BCHUSDT",
)
