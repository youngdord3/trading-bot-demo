"""Backtest avanzato offline: EMA crossover + filtro ADX + stop/take su ATR,
con walk-forward 70% in-sample / 30% out-of-sample.

Nessun accesso a DB o account. Dati: mainnet pubblico (default) o demo.
"""
import json
import os
import sys
import time
from dataclasses import dataclass
from decimal import Decimal
from itertools import product

from indicators import adx, atr, ema

ZERO = Decimal(0)
HUNDRED = Decimal(100)

SYMBOL = os.environ.get("BT_SYMBOL", "BTCUSDT")
DAYS = int(os.environ.get("BT_DAYS", "365"))
INTERVALS = [s.strip() for s in os.environ.get("BT_INTERVALS", "15m,1h,4h").split(",") if s.strip()]
QTY = Decimal(os.environ.get("BT_QTY", "0.002"))
FEE_RATE = Decimal(os.environ.get("BT_FEE", "0.0005"))   # taker per lato
SLIPPAGE = Decimal(os.environ.get("BT_SLIPPAGE", "0.0002"))
START_EQUITY = Decimal(os.environ.get("BT_START_EQUITY", "1000"))
DATA_SOURCE = os.environ.get("BT_DATA", "mainnet")

INTERVAL_MS = {"15m": 900_000, "1h": 3_600_000, "4h": 14_400_000}
PAGE_LIMIT = 1500
WARMUP_BARS = 200
MIN_BARS = 1000
ATR_PERIOD = 14
ADX_PERIOD = 14

# Griglia di ottimizzazione (solo in-sample).
GRID_EMA = ((9, 21), (12, 26), (20, 50))
GRID_ADX_MIN = (Decimal("20"), Decimal("25"), Decimal("30"))
GRID_SL_MULT = (Decimal("1.0"), Decimal("1.5"), Decimal("2.0"))
GRID_TP_MULT = (Decimal("2.0"), Decimal("3.0"), Decimal("4.0"))
MIN_TRADES_IN_SAMPLE = 50

# Criteri di accettazione out-of-sample, fissati a priori.
ACCEPT_MIN_PROFIT_FACTOR = Decimal("1.30")
ACCEPT_MIN_TRADES = 30
ACCEPT_MAX_DRAWDOWN_PCT = Decimal("15")
ACCEPT_MIN_WIN_RATE = Decimal("0.40")
ACCEPT_MIN_PAYOFF = Decimal("2.0")


@dataclass(frozen=True)
class Bar:
    open_time_ms: int
    close_time_ms: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal


@dataclass(frozen=True)
class Params:
    fast: int
    slow: int
    adx_min: Decimal
    sl_mult: Decimal
    tp_mult: Decimal


@dataclass(frozen=True)
class Costs:
    qty: Decimal
    fee_rate: Decimal
    slippage: Decimal


@dataclass(frozen=True)
class Indicators:
    ema: dict
    atr: list
    adx: list


@dataclass(frozen=True)
class Position:
    side: str
    entry_time_ms: int
    entry_price: Decimal
    stop: Decimal
    take: Decimal


@dataclass(frozen=True)
class Trade:
    side: str
    entry_time_ms: int
    exit_time_ms: int
    entry_price: Decimal
    exit_price: Decimal
    net: Decimal
    fees: Decimal
    reason: str


def parse_bars(raw: list, server_now_ms: int) -> list[Bar]:
    """Tiene solo candele chiuse, con OHLC completo, in ordine crescente."""
    out = []
    for k in raw:
        if not isinstance(k, list) or len(k) < 7:
            raise ValueError("INVALID_KLINE")
        bar = Bar(int(k[0]), int(k[6]), Decimal(str(k[1])), Decimal(str(k[2])), Decimal(str(k[3])), Decimal(str(k[4])))
        if bar.high < bar.low:
            raise ValueError("INVALID_KLINE")
        if bar.close_time_ms < server_now_ms:
            out.append(bar)
    for a, b in zip(out, out[1:]):
        if b.open_time_ms <= a.open_time_ms:
            raise ValueError("KLINES_NOT_SORTED")
    return out


def fetch_bars(client, interval: str) -> list[Bar]:
    interval_ms = INTERVAL_MS[interval]
    end = client.get_server_time_ms()
    cursor = end - DAYS * 86_400_000
    raw = []
    while cursor < end:
        batch = client.get_klines(SYMBOL, interval, PAGE_LIMIT, start_time_ms=cursor)
        if not batch:
            break
        raw.extend(batch)
        nxt = int(batch[-1][0]) + interval_ms
        if nxt <= cursor:
            break
        cursor = nxt
        time.sleep(0.25)   # rispetto dei rate limit
    return parse_bars(raw, end)


def build_indicators(bars: list[Bar], ema_periods) -> Indicators:
    closes = [b.close for b in bars]
    highs = [b.high for b in bars]
    lows = [b.low for b in bars]
    return Indicators(
        ema={p: ema(closes, p) for p in sorted(set(ema_periods))},
        atr=atr(highs, lows, closes, ATR_PERIOD),
        adx=adx(highs, lows, closes, ADX_PERIOD),
    )


def split_index(total: int) -> int:
    """Indice della prima candela out-of-sample (70/30)."""
    return total * 70 // 100


def _cross(fast: list, slow: list, j: int):
    if j < 1:
        return None
    if fast[j - 1] is None or slow[j - 1] is None or fast[j] is None or slow[j] is None:
        return None
    prev = fast[j - 1] - slow[j - 1]
    curr = fast[j] - slow[j]
    if prev <= 0 < curr:
        return "LONG"
    if prev >= 0 > curr:
        return "SHORT"
    return None


def _open_position(side: str, bar: Bar, atr_value: Decimal, params: Params, costs: Costs) -> Position:
    base = bar.open
    if side == "LONG":
        return Position(
            side,
            bar.open_time_ms,
            base * (1 + costs.slippage),
            base - params.sl_mult * atr_value,
            base + params.tp_mult * atr_value,
        )
    return Position(
        side,
        bar.open_time_ms,
        base * (1 - costs.slippage),
        base + params.sl_mult * atr_value,
        base - params.tp_mult * atr_value,
    )


def _exit_hit(position: Position, bar: Bar):
    """Stop prima del take (ipotesi prudente). Gap oltre lo stop: fill all'open."""
    if position.side == "LONG":
        if bar.low <= position.stop:
            return min(bar.open, position.stop), "STOP"
        if bar.high >= position.take:
            return position.take, "TAKE"
        return None
    if bar.high >= position.stop:
        return max(bar.open, position.stop), "STOP"
    if bar.low <= position.take:
        return position.take, "TAKE"
    return None


def _close_position(position: Position, raw_price: Decimal, exit_time_ms: int, reason: str, costs: Costs) -> Trade:
    if position.side == "LONG":
        exit_price = raw_price * (1 - costs.slippage)
        gross = (exit_price - position.entry_price) * costs.qty
    else:
        exit_price = raw_price * (1 + costs.slippage)
        gross = (position.entry_price - exit_price) * costs.qty
    fees = (position.entry_price + exit_price) * costs.qty * costs.fee_rate
    return Trade(position.side, position.entry_time_ms, exit_time_ms, position.entry_price, exit_price, gross - fees, fees, reason)


def simulate(bars: list[Bar], ind: Indicators, params: Params, start: int, end: int, costs: Costs) -> list[Trade]:
    """Simula sulle candele [start, end). Segnale a candela chiusa, fill all'open successivo.
    La posizione ancora aperta a fine segmento è chiusa all'ultimo close."""
    fast = ind.ema[params.fast]
    slow = ind.ema[params.slow]
    position = None
    pending_entry = None
    pending_exit = False
    trades = []
    for j in range(start, end):
        bar = bars[j]
        if pending_exit and position is not None:
            trades.append(_close_position(position, bar.open, bar.open_time_ms, "SIGNAL", costs))
            position = None
            pending_exit = False
        if pending_entry is not None and position is None:
            position = _open_position(pending_entry, bar, ind.atr[j - 1], params, costs)
            pending_entry = None
        if position is not None:
            hit = _exit_hit(position, bar)
            if hit is not None:
                trades.append(_close_position(position, hit[0], bar.close_time_ms, hit[1], costs))
                position = None
        cross = _cross(fast, slow, j)
        if cross is None:
            continue
        if position is not None and position.side != cross:
            pending_exit = True
        regime_ok = (ind.adx[j] is not None and ind.adx[j] >= params.adx_min
                     and ind.atr[j] is not None and ind.atr[j] > 0)
        if (position is None or pending_exit) and regime_ok:
            pending_entry = cross
    if position is not None:
        last = bars[end - 1]
        trades.append(_close_position(position, last.close, last.close_time_ms, "END", costs))
    return trades


def summarize(trades: list[Trade], start_equity: Decimal) -> dict:
    """Metriche su trade chiusi. Il drawdown è calcolato sull'equity realizzata."""
    wins = [t.net for t in trades if t.net > 0]
    losses = [-t.net for t in trades if t.net < 0]
    gross_win = sum(wins, ZERO)
    gross_loss = sum(losses, ZERO)
    equity = start_equity
    peak = start_equity
    max_dd = ZERO
    max_dd_pct = ZERO
    for t in trades:
        equity += t.net
        if equity > peak:
            peak = equity
        dd = peak - equity
        if dd > max_dd:
            max_dd = dd
        if peak > 0:
            dd_pct = dd / peak * HUNDRED
            if dd_pct > max_dd_pct:
                max_dd_pct = dd_pct
    count = len(trades)
    payoff = None
    if wins and losses:
        payoff = (gross_win / len(wins)) / (gross_loss / len(losses))
    return {
        "trades": count,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": Decimal(len(wins)) / count if count else ZERO,
        "profit_factor": gross_win / gross_loss if gross_loss > 0 else None,
        "payoff_ratio": payoff,
        "net_pnl": gross_win - gross_loss,
        "fees": sum((t.fees for t in trades), ZERO),
        "max_drawdown": max_dd,
        "max_drawdown_pct": max_dd_pct,
    }


def accept(metrics: dict):
    """Applica i criteri a priori. Ritorna (accettata, lista dei motivi di scarto)."""
    reasons = []
    if metrics["trades"] < ACCEPT_MIN_TRADES:
        reasons.append("TOO_FEW_TRADES")
    profit_factor = metrics["profit_factor"]
    if profit_factor is None:
        if metrics["net_pnl"] <= 0:
            reasons.append("PROFIT_FACTOR_TOO_LOW")
    elif profit_factor <= ACCEPT_MIN_PROFIT_FACTOR:
        reasons.append("PROFIT_FACTOR_TOO_LOW")
    if metrics["max_drawdown_pct"] >= ACCEPT_MAX_DRAWDOWN_PCT:
        reasons.append("DRAWDOWN_TOO_HIGH")
    payoff = metrics["payoff_ratio"]
    edge_ok = (metrics["win_rate"] > ACCEPT_MIN_WIN_RATE
               or (payoff is not None and payoff >= ACCEPT_MIN_PAYOFF))
    if not edge_ok:
        reasons.append("NO_EDGE_PROFILE")
    return (not reasons, reasons)


def optimize(bars: list[Bar], ind: Indicators, start: int, end: int, costs: Costs):
    """Sceglie in-sample i parametri col profit factor più alto (poi PnL netto)."""
    best = None
    tested = 0
    for (fast, slow), adx_min, sl_mult, tp_mult in product(
            GRID_EMA, GRID_ADX_MIN, GRID_SL_MULT, GRID_TP_MULT):
        tested += 1
        params = Params(fast, slow, adx_min, sl_mult, tp_mult)
        metrics = summarize(simulate(bars, ind, params, start, end, costs), START_EQUITY)
        if metrics["trades"] < MIN_TRADES_IN_SAMPLE or metrics["profit_factor"] is None:
            continue
        key = (metrics["profit_factor"], metrics["net_pnl"])
        if best is None or key > best[0]:
            best = (key, params, metrics)
    if best is None:
        return None, None, tested
    return best[1], best[2], tested


def _fmt(value, places: str = "0.01"):
    if value is None:
        return None
    return str(value.quantize(Decimal(places)))


def _fmt_metrics(metrics: dict) -> dict:
    return {
        "trades": metrics["trades"],
        "wins": metrics["wins"],
        "losses": metrics["losses"],
        "win_rate": _fmt(metrics["win_rate"], "0.0001"),
        "profit_factor": _fmt(metrics["profit_factor"]),
        "payoff_ratio": _fmt(metrics["payoff_ratio"]),
        "net_pnl": _fmt(metrics["net_pnl"]),
        "fees": _fmt(metrics["fees"]),
        "max_drawdown": _fmt(metrics["max_drawdown"]),
        "max_drawdown_pct": _fmt(metrics["max_drawdown_pct"]),
    }


def _fmt_params(params: Params) -> dict:
    return {
        "ema_fast": params.fast,
        "ema_slow": params.slow,
        "adx_min": str(params.adx_min),
        "sl_atr_mult": str(params.sl_mult),
        "tp_atr_mult": str(params.tp_mult),
    }


def run_interval(client, interval: str, costs: Costs) -> dict:
    bars = fetch_bars(client, interval)
    result = {"event": "backtest_advanced_result", "symbol": SYMBOL, "interval": interval, "days": DAYS, "candles": len(bars)}
    if len(bars) < MIN_BARS:
        result["verdict"] = "INSUFFICIENT_DATA"
        return result
    gaps = sum(1 for a, b in zip(bars, bars[1:]) if b.open_time_ms - a.open_time_ms != INTERVAL_MS[interval])
    split = split_index(len(bars))
    ind = build_indicators(bars, [p for pair in GRID_EMA for p in pair])
    params, in_sample, tested = optimize(bars, ind, WARMUP_BARS, split, costs)
    result.update({"gaps": gaps, "in_sample_candles": split - WARMUP_BARS, "out_of_sample_candles": len(bars) - split, "combinations_tested": tested})
    if params is None:
        result["verdict"] = "NO_CANDIDATE"
        return result
    out_sample = summarize(simulate(bars, ind, params, split, len(bars), costs), START_EQUITY)
    accepted, reasons = accept(out_sample)
    result.update({
        "params": _fmt_params(params),
        "in_sample": _fmt_metrics(in_sample),
        "out_of_sample": _fmt_metrics(out_sample),
        "verdict": "ACCEPTED" if accepted else "REJECTED",
        "reject_reasons": reasons,
    })
    return result


def main() -> int:
    from binance_public import DEMO_HOST, MAINNET_DATA_HOST, PublicClient

    unsupported = [i for i in INTERVALS if i not in INTERVAL_MS]
    if unsupported or not INTERVALS:
        print(json.dumps({"event": "backtest_advanced_error", "error": "UNSUPPORTED_INTERVAL", "intervals": unsupported}), flush=True)
        return 2
    client = PublicClient(MAINNET_DATA_HOST if DATA_SOURCE == "mainnet" else DEMO_HOST)
    costs = Costs(QTY, FEE_RATE, SLIPPAGE)
    results = []
    for interval in INTERVALS:
        result = run_interval(client, interval, costs)
        results.append(result)
        print(json.dumps(result), flush=True)
    accepted = [r["interval"] for r in results if r["verdict"] == "ACCEPTED"]
    print(json.dumps({"event": "backtest_advanced_summary", "accepted_intervals": accepted, "result": "CANDIDATE_FOUND" if accepted else "NO_STRATEGY_ACCEPTED"}), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
