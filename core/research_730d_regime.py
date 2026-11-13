"""Diagnosi 730 giorni fold-by-fold e ricerca regime/timeframe (Passo A.9).

Modulo di sola ricerca: nessun accesso al database, nessun segreto, nessun ordine.
Scarica una sola volta 730g + 30g warmup di candele 1h (18240 barre per simbolo)
ed esegue:
  1. Diagnosi fold-by-fold sui 9 fold (730g) del baseline 1h Donchian Trailing (0.50% risk),
     isolando i fold perdenti, i parametri scelti in-sample e il tasso di stop-loss.
  2. Confronto walk-forward su 730g (9 fold) e 365g (ultimi 3 fold) di 4 architetture
     (griglia 8 combinazioni ciascuna, sizing esatto con cap nozionale 2000 USDT):
     - h1_is_gated:         1h Donchian Trailing con filtro di qualifica IS rigoroso
                            (IS PF >= 1.25, IS DD <= 15%, evita di tradare fold senza edge).
     - h1_regime_strict:    1h Donchian Trailing con filtro macro 4h espansivo
                            (EMA20 > EMA50 > EMA200 4h, ADX14_4h >= 22, ATR14_4h >= 0.9*SMA50).
     - h4_donchian_trail:   Breakout Donchian nativo su candele 4h chiuse + trend EMA50/200 4h
                            e trailing stop ATR_4h.
     - h4_keltner_trail:    Breakout di volatilita' Keltner (EMA20 +/- k*ATR14) su candele 4h
                            in espansione di volatilita' con trailing stop ATR_4h.
"""
import bisect
import os
import sys
import time
from typing import NamedTuple

import research_signals as rs

DAYS_LONG = int(os.environ.get("RR_DAYS_LONG", "730"))
DAYS_SHORT = int(os.environ.get("RR_DAYS_SHORT", "365"))
WARMUP_DAYS = int(os.environ.get("RR_WARMUP_DAYS", "30"))
TRAIN_DAYS = int(os.environ.get("RR_TRAIN_DAYS", "180"))
TEST_DAYS = int(os.environ.get("RR_TEST_DAYS", "60"))

DEFAULT_RISK_FRACTION = float(os.environ.get("RR_RISK_FRACTION", "0.005"))
DEFAULT_MAX_CONCURRENT = int(os.environ.get("RR_MAX_CONCURRENT", "3"))

IS_STRICT_MIN_PF = float(os.environ.get("RR_IS_STRICT_MIN_PF", "1.25"))
IS_STRICT_MAX_DD_PCT = float(os.environ.get("RR_IS_STRICT_MAX_DD_PCT", "15.0"))
MIN_IS_TRADES_1H = int(os.environ.get("RR_MIN_IS_TRADES_1H", "20"))
MIN_IS_TRADES_4H = int(os.environ.get("RR_MIN_IS_TRADES_4H", "12"))

SL_MULT_FIXED = 1.5
MAX_BREAKOUT_ATR = 1.5
H4_COOLDOWN_BARS = 1

MODES = (
    "h1_is_gated",
    "h1_regime_strict",
    "h4_donchian_trail",
    "h4_keltner_trail",
)


class ExecConfig(NamedTuple):
    risk_fraction: float
    max_concurrent: int
    fee_rate: float
    slippage_rate: float
    bar_span_ms: int
    cooldown_bars: int


class RegimeParams(NamedTuple):
    mode: str
    param_a: float
    param_b: float
    exit_mult: float


class Features1HStrict(NamedTuple):
    bars: list
    times: list
    ema50: list
    atr14: list
    adx14: list
    donchian_high: dict
    donchian_low: dict
    trend_4h_basic: list
    trend_4h_strict: list


class Features4H(NamedTuple):
    bars: list
    times: list
    ema20: list
    ema50: list
    ema200: list
    atr14: list
    atr_sma20: list
    adx14: list
    donchian_high: dict
    donchian_low: dict


H1_DC_PERIODS = (20, 36, 48)
H4_DC_PERIODS = (10, 20)


def sma_optional(values, period):
    """Media mobile semplice su serie che puo' contenere None iniziali."""
    count = len(values)
    out = [None] * count
    if period <= 0 or count < period:
        return out
    window = []
    for i, val in enumerate(values):
        if val is None:
            window.clear()
            continue
        window.append(val)
        if len(window) > period:
            window.pop(0)
        if len(window) == period:
            out[i] = sum(window) / period
    return out


def build_grid(mode):
    """Restituisce le 8 combinazioni della famiglia richiesta."""
    if mode == "h1_is_gated":
        return [
            RegimeParams(mode, float(dc_period), adx_min, exit_mult)
            for dc_period in (20, 48)
            for adx_min in (18.0, 25.0)
            for exit_mult in (2.0, 3.0)
        ]
    if mode == "h1_regime_strict":
        return [
            RegimeParams(mode, float(dc_period), adx_min, exit_mult)
            for dc_period in (20, 36)
            for adx_min in (22.0, 28.0)
            for exit_mult in (2.0, 3.0)
        ]
    if mode == "h4_donchian_trail":
        return [
            RegimeParams(mode, float(dc_period), adx_min, exit_mult)
            for dc_period in H4_DC_PERIODS
            for adx_min in (20.0, 25.0)
            for exit_mult in (2.0, 3.0)
        ]
    if mode == "h4_keltner_trail":
        return [
            RegimeParams(mode, kc_mult, adx_min, exit_mult)
            for kc_mult in (1.5, 2.0)
            for adx_min in (20.0, 25.0)
            for exit_mult in (2.0, 3.0)
        ]
    raise ValueError("INVALID_MODE: " + str(mode))


def params_to_dict(params):
    if params.mode == "h4_keltner_trail":
        return {
            "mode": params.mode,
            "kc_mult": params.param_a,
            "adx_min": params.param_b,
            "sl_mult": SL_MULT_FIXED,
            "exit_mult": params.exit_mult,
        }
    return {
        "mode": params.mode,
        "dc_period": int(params.param_a),
        "adx_min": params.param_b,
        "sl_mult": SL_MULT_FIXED,
        "exit_mult": params.exit_mult,
    }


def _build_4h_trend_maps(bars_4h):
    closes = [b.close for b in bars_4h]
    highs = [b.high for b in bars_4h]
    lows = [b.low for b in bars_4h]
    ema20 = rs.ema(closes, 20)
    ema50 = rs.ema(closes, 50)
    ema200 = rs.ema(closes, 200)
    atr14 = rs.atr(highs, lows, closes, 14)
    atr_sma50 = sma_optional(atr14, 50)
    adx14 = rs.adx(highs, lows, closes, 14)

    basic_map = {}
    strict_map = {}
    for i, bar in enumerate(bars_4h):
        bucket = bar.open_time // rs.FOUR_HOUR_MS
        if ema20[i] is not None and ema50[i] is not None:
            if bar.close > ema20[i] and ema20[i] >= ema50[i]:
                basic_map[bucket] = 1
            elif bar.close < ema20[i] and ema20[i] <= ema50[i]:
                basic_map[bucket] = -1
            else:
                basic_map[bucket] = 0
        if any(
            v is None
            for v in (ema20[i], ema50[i], ema200[i], atr14[i], atr_sma50[i], adx14[i])
        ):
            continue
        if adx14[i] < 22.0 or atr_sma50[i] <= 0 or atr14[i] < 0.90 * atr_sma50[i]:
            strict_map[bucket] = 0
            continue
        if bar.close > ema20[i] and ema20[i] > ema50[i] > ema200[i]:
            strict_map[bucket] = 1
        elif bar.close < ema20[i] and ema20[i] < ema50[i] < ema200[i]:
            strict_map[bucket] = -1
        else:
            strict_map[bucket] = 0
    return basic_map, strict_map


def build_features_all(bars_1h):
    """Costruisce sia le feature 1h (con mappe 4h basic e strict) sia le feature 4h native."""
    closes_1h = [b.close for b in bars_1h]
    highs_1h = [b.high for b in bars_1h]
    lows_1h = [b.low for b in bars_1h]
    bars_4h = rs.aggregate_4h(bars_1h)
    basic_map, strict_map = _build_4h_trend_maps(bars_4h)

    dc_high_1h = {}
    dc_low_1h = {}
    for period in H1_DC_PERIODS:
        u, l = rs.donchian_bounds(highs_1h, lows_1h, period)
        dc_high_1h[period] = u
        dc_low_1h[period] = l

    feat_1h = Features1HStrict(
        bars=bars_1h,
        times=[b.open_time for b in bars_1h],
        ema50=rs.ema(closes_1h, 50),
        atr14=rs.atr(highs_1h, lows_1h, closes_1h, 14),
        adx14=rs.adx(highs_1h, lows_1h, closes_1h, 14),
        donchian_high=dc_high_1h,
        donchian_low=dc_low_1h,
        trend_4h_basic=[
            basic_map.get(rs.last_closed_4h_bucket(b.open_time), 0) for b in bars_1h
        ],
        trend_4h_strict=[
            strict_map.get(rs.last_closed_4h_bucket(b.open_time), 0) for b in bars_1h
        ],
    )

    closes_4h = [b.close for b in bars_4h]
    highs_4h = [b.high for b in bars_4h]
    lows_4h = [b.low for b in bars_4h]
    atr14_4h = rs.atr(highs_4h, lows_4h, closes_4h, 14)
    dc_high_4h = {}
    dc_low_4h = {}
    for period in H4_DC_PERIODS:
        u, l = rs.donchian_bounds(highs_4h, lows_4h, period)
        dc_high_4h[period] = u
        dc_low_4h[period] = l

    feat_4h = Features4H(
        bars=bars_4h,
        times=[b.open_time for b in bars_4h],
        ema20=rs.ema(closes_4h, 20),
        ema50=rs.ema(closes_4h, 50),
        ema200=rs.ema(closes_4h, 200),
        atr14=atr14_4h,
        atr_sma20=sma_optional(atr14_4h, 20),
        adx14=rs.adx(highs_4h, lows_4h, closes_4h, 14),
        donchian_high=dc_high_4h,
        donchian_low=dc_low_4h,
    )
    return feat_1h, feat_4h


# ---------------------------------------------------------------------------
# Segnali causali su 1h e su 4h nativo
# ---------------------------------------------------------------------------


def signal_1h(feat_1h, index, params):
    if index < 1:
        return 0
    atr_val = feat_1h.atr14[index]
    adx_val = feat_1h.adx14[index]
    ema50_val = feat_1h.ema50[index]
    if atr_val is None or adx_val is None or ema50_val is None or atr_val <= 0:
        return 0
    dc_period = int(params.param_a)
    upper = feat_1h.donchian_high[dc_period][index]
    lower = feat_1h.donchian_low[dc_period][index]
    if upper is None or lower is None:
        return 0
    trend = (
        feat_1h.trend_4h_strict[index]
        if params.mode == "h1_regime_strict"
        else feat_1h.trend_4h_basic[index]
    )
    if trend == 0 or adx_val < params.param_b:
        return 0
    close = feat_1h.bars[index].close
    if trend == 1 and close > ema50_val and close > upper:
        if (close - upper) / atr_val <= MAX_BREAKOUT_ATR:
            return 1
    if trend == -1 and close < ema50_val and close < lower:
        if (lower - close) / atr_val <= MAX_BREAKOUT_ATR:
            return -1
    return 0


def signal_4h(feat_4h, index, params):
    if index < 1:
        return 0
    atr_val = feat_4h.atr14[index]
    adx_val = feat_4h.adx14[index]
    ema20_val = feat_4h.ema20[index]
    ema50_val = feat_4h.ema50[index]
    ema200_val = feat_4h.ema200[index]
    if any(v is None for v in (atr_val, adx_val, ema20_val, ema50_val, ema200_val)) or atr_val <= 0:
        return 0
    if adx_val < params.param_b:
        return 0
    close = feat_4h.bars[index].close

    if params.mode == "h4_donchian_trail":
        dc_period = int(params.param_a)
        upper = feat_4h.donchian_high[dc_period][index]
        lower = feat_4h.donchian_low[dc_period][index]
        if upper is None or lower is None:
            return 0
        if close > ema50_val > ema200_val and close > upper:
            if (close - upper) / atr_val <= MAX_BREAKOUT_ATR:
                return 1
        if close < ema50_val < ema200_val and close < lower:
            if (lower - close) / atr_val <= MAX_BREAKOUT_ATR:
                return -1
        return 0

    # h4_keltner_trail
    atr_sma = feat_4h.atr_sma20[index]
    if atr_sma is None or atr_val < atr_sma:
        return 0
    prev_close = feat_4h.bars[index - 1].close
    prev_ema20 = feat_4h.ema20[index - 1]
    prev_atr = feat_4h.atr14[index - 1]
    if prev_ema20 is None or prev_atr is None:
        return 0
    kc_mult = params.param_a
    upper = ema20_val + kc_mult * atr_val
    lower = ema20_val - kc_mult * atr_val
    prev_upper = prev_ema20 + kc_mult * prev_atr
    prev_lower = prev_ema20 - kc_mult * prev_atr

    if ema20_val > ema50_val > ema200_val and prev_close <= prev_upper and close > upper:
        if (close - upper) / atr_val <= MAX_BREAKOUT_ATR:
            return 1
    if ema20_val < ema50_val < ema200_val and prev_close >= prev_lower and close < lower:
        if (lower - close) / atr_val <= MAX_BREAKOUT_ATR:
            return -1
    return 0


# ---------------------------------------------------------------------------
# Esecuzione parametrizzata fedele (rispetta il cap MAX_NOTIONAL_USDT)
# ---------------------------------------------------------------------------


def open_position_cfg(symbol, side, entry_time, raw_open, atr_value, exit_mult, cfg):
    entry_price = raw_open * (1.0 + cfg.slippage_rate * side)
    stop_dist = SL_MULT_FIXED * atr_value
    if stop_dist <= 0 or entry_price <= 0:
        return None
    risk_usdt = rs.INITIAL_EQUITY_USDT * cfg.risk_fraction
    qty = min(risk_usdt / stop_dist, rs.MAX_NOTIONAL_USDT / entry_price)
    stop_price = entry_price - side * stop_dist
    trail_dist = exit_mult * atr_value
    return rs.Position(
        symbol=symbol,
        side=side,
        entry_time=entry_time,
        entry_price=entry_price,
        qty=qty,
        stop_price=stop_price,
        take_price=0.0,
        trail_distance=trail_dist,
        best_price=entry_price,
    )


def exit_fill_cfg(position, bar, cfg):
    if position.side == 1:
        if bar.low <= position.stop_price:
            reason = (
                "TRAIL"
                if position.trail_distance > 0 and position.stop_price > position.entry_price
                else "SL"
            )
            return (min(position.stop_price, bar.open) * (1.0 - cfg.slippage_rate), reason)
        return None
    if bar.high >= position.stop_price:
        reason = (
            "TRAIL"
            if position.trail_distance > 0 and position.stop_price < position.entry_price
            else "SL"
        )
        return (max(position.stop_price, bar.open) * (1.0 + cfg.slippage_rate), reason)
    return None


def close_position_cfg(position, exit_price, exit_time, reason, cfg):
    gross = (exit_price - position.entry_price) * position.qty * position.side
    fees = (position.entry_price + exit_price) * position.qty * cfg.fee_rate
    return rs.Trade(
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


def simulate_series(symbol, feat, params, start_ms, end_ms, cfg):
    is_4h = params.mode.startswith("h4_")
    sig_fn = signal_4h if is_4h else signal_1h
    low_index = bisect.bisect_left(feat.times, start_ms)
    high_index = bisect.bisect_left(feat.times, end_ms)
    trades = []
    position = None
    next_entry_index = low_index

    for i in range(low_index, high_index):
        bar = feat.bars[i]
        if position is not None:
            fill = exit_fill_cfg(position, bar, cfg)
            if fill is not None:
                trades.append(
                    close_position_cfg(position, fill[0], bar.open_time + cfg.bar_span_ms, fill[1], cfg)
                )
                position = None
                next_entry_index = i + cfg.cooldown_bars
                continue
            position = rs.advance_trailing(position, bar)
            continue
        if i < next_entry_index or i + 1 >= high_index:
            continue
        side = sig_fn(feat, i, params)
        if side == 0:
            continue
        next_bar = feat.bars[i + 1]
        position = open_position_cfg(
            symbol, side, next_bar.open_time, next_bar.open, feat.atr14[i], params.exit_mult, cfg
        )

    if position is not None and high_index > low_index:
        last_bar = feat.bars[high_index - 1]
        price = last_bar.close * (1.0 - cfg.slippage_rate * position.side)
        trades.append(
            close_position_cfg(position, price, last_bar.open_time + cfg.bar_span_ms, "END", cfg)
        )
    return trades


def make_exec_config(mode, risk_fraction=DEFAULT_RISK_FRACTION, max_concurrent=DEFAULT_MAX_CONCURRENT):
    if mode.startswith("h4_"):
        return ExecConfig(
            risk_fraction=risk_fraction,
            max_concurrent=max_concurrent,
            fee_rate=rs.TAKER_FEE_RATE,
            slippage_rate=rs.SLIPPAGE_RATE,
            bar_span_ms=rs.FOUR_HOUR_MS,
            cooldown_bars=H4_COOLDOWN_BARS,
        )
    return ExecConfig(
        risk_fraction=risk_fraction,
        max_concurrent=max_concurrent,
        fee_rate=rs.TAKER_FEE_RATE,
        slippage_rate=rs.SLIPPAGE_RATE,
        bar_span_ms=rs.HOUR_MS,
        cooldown_bars=rs.COOLDOWN_BARS,
    )


def run_window_mode(features_map, params, start_ms, end_ms, cfg, cache=None):
    store = cache if cache is not None else {}
    key = (params, start_ms, end_ms, cfg.risk_fraction, cfg.fee_rate, cfg.slippage_rate)
    if key not in store:
        raw = []
        for symbol in sorted(features_map):
            feat_1h, feat_4h = features_map[symbol]
            feat = feat_4h if params.mode.startswith("h4_") else feat_1h
            raw.extend(simulate_series(symbol, feat, params, start_ms, end_ms, cfg))
        store[key] = raw
    return rs.apply_max_concurrent(store[key], cfg.max_concurrent)


def classify_is_result(is_metrics, min_trades, strict_gate=True):
    if is_metrics["trades"] < min_trades:
        return "TOO_FEW_TRADES_IN_SAMPLE"
    if is_metrics["profit_factor"] <= 1.0 or is_metrics["net_pnl_usdt"] <= 0.0:
        return "NO_EDGE_IN_SAMPLE"
    if strict_gate:
        if is_metrics["profit_factor"] < IS_STRICT_MIN_PF:
            return "WEAK_IS_PROFIT_FACTOR"
        if is_metrics["max_drawdown_pct"] > IS_STRICT_MAX_DD_PCT:
            return "IS_DRAWDOWN_TOO_HIGH"
    return "QUALIFIED"


def select_params_mode(features_map, mode, train_start, train_end, cfg, strict_gate=True, cache=None):
    min_trades = MIN_IS_TRADES_4H if mode.startswith("h4_") else MIN_IS_TRADES_1H
    best = None
    outcomes = {}
    for params in build_grid(mode):
        trades = run_window_mode(features_map, params, train_start, train_end, cfg, cache=cache)
        result = rs.metrics(trades, rs.INITIAL_EQUITY_USDT)
        outcome = classify_is_result(result, min_trades, strict_gate=strict_gate)
        outcomes[outcome] = outcomes.get(outcome, 0) + 1
        if outcome != "QUALIFIED":
            continue
        key = (min(result["profit_factor"], rs.PROFIT_FACTOR_CAP), result["net_pnl_usdt"])
        if best is None or key > best[0]:
            best = (key, params, result)
    if best is None:
        return (None, None, outcomes)
    return (best[1], best[2], outcomes)


def _safe_pf(value):
    return "inf" if value == float("inf") else round(value, 4)


def diagnose_730d_baseline(features_map, folds_730, cache=None):
    """Analizza i 9 fold del baseline 1h Donchian Trailing (senza strict gate) per isolare le cause di perdita."""
    cfg = make_exec_config("h1_is_gated")
    fold_diagnostics = []
    losing_folds = []

    for idx, (train_start, train_end, test_end) in enumerate(folds_730, start=1):
        params, is_m, _ = select_params_mode(
            features_map, "h1_is_gated", train_start, train_end, cfg, strict_gate=False, cache=cache
        )
        if params is None:
            fold_diagnostics.append({"fold": idx, "status": "NO_IS_CANDIDATE"})
            continue
        oos_trades = run_window_mode(features_map, params, train_end, test_end, cfg, cache=cache)
        oos_m = rs.metrics(oos_trades, rs.INITIAL_EQUITY_USDT)
        sl_count = sum(1 for t in oos_trades if t.reason == "SL")
        trail_count = sum(1 for t in oos_trades if t.reason == "TRAIL")
        sl_rate = round(sl_count / len(oos_trades), 4) if oos_trades else 0.0
        would_gate = (
            is_m["profit_factor"] < IS_STRICT_MIN_PF
            or is_m["max_drawdown_pct"] > IS_STRICT_MAX_DD_PCT
        )
        if oos_m["net_pnl_usdt"] < 0:
            losing_folds.append(idx)
        fold_diagnostics.append({
            "fold": idx,
            "status": "LOSS" if oos_m["net_pnl_usdt"] < 0 else "WIN",
            "params": {"dc_period": int(params.param_a), "adx_min": params.param_b, "exit_mult": params.exit_mult},
            "is_pf": _safe_pf(is_m["profit_factor"]),
            "is_dd_pct": round(is_m["max_drawdown_pct"], 4),
            "filtered_by_strict_is_gate": would_gate,
            "oos_trades": oos_m["trades"],
            "oos_pf": _safe_pf(oos_m["profit_factor"]),
            "oos_net_pnl": round(oos_m["net_pnl_usdt"], 4),
            "oos_dd_pct": round(oos_m["max_drawdown_pct"], 4),
            "sl_rate": sl_rate,
            "trail_exits": trail_count,
        })
    return {
        "losing_folds": losing_folds,
        "folds": fold_diagnostics,
    }


def evaluate_mode_horizon(features_map, mode, horizon_days, folds, cache=None):
    cfg = make_exec_config(mode)
    oos_trades = []
    evaluated_folds = 0
    total_outcomes = {}
    fold_details = []

    for idx, (train_start, train_end, test_end) in enumerate(folds, start=1):
        params, is_m, outcomes = select_params_mode(
            features_map, mode, train_start, train_end, cfg, strict_gate=True, cache=cache
        )
        for k, v in outcomes.items():
            total_outcomes[k] = total_outcomes.get(k, 0) + v
        if params is None:
            fold_details.append({"fold": idx, "status": "NO_IS_CANDIDATE"})
            continue
        evaluated_folds += 1
        fold_trades = run_window_mode(features_map, params, train_end, test_end, cfg, cache=cache)
        oos_trades.extend(fold_trades)
        fold_m = rs.metrics(fold_trades, rs.INITIAL_EQUITY_USDT)
        fold_details.append({
            "fold": idx,
            "status": "EVALUATED",
            "params": params_to_dict(params),
            "is_pf": _safe_pf(is_m["profit_factor"]),
            "oos_trades": fold_m["trades"],
            "oos_pf": _safe_pf(fold_m["profit_factor"]),
            "oos_net_pnl": round(fold_m["net_pnl_usdt"], 4),
            "oos_dd_pct": round(fold_m["max_drawdown_pct"], 4),
        })

    summary = rs.metrics(oos_trades, rs.INITIAL_EQUITY_USDT)
    accepted, reasons = rs.evaluate_acceptance(summary)
    by_symbol = {}
    for trade in oos_trades:
        entry = by_symbol.setdefault(trade.symbol, {"trades": 0, "net_pnl_usdt": 0.0})
        entry["trades"] += 1
        entry["net_pnl_usdt"] = round(entry["net_pnl_usdt"] + trade.net_pnl, 4)

    return {
        "mode": mode,
        "horizon_days": horizon_days,
        "folds_total": len(folds),
        "folds_evaluated": evaluated_folds,
        "in_sample_outcomes": total_outcomes,
        "verdict": "ACCEPTED" if accepted else "REJECTED",
        "reasons": reasons,
        "out_of_sample": rs._json_safe(summary),
        "by_symbol": by_symbol,
        "folds": fold_details,
    }


def main():
    for symbol in rs.SYMBOLS:
        if not rs.SYMBOL_PATTERN.match(symbol):
            rs.log_event("invalid_symbol", symbol=symbol)
            return 2

    end_ms = (int(time.time() * 1000) // rs.HOUR_MS) * rs.HOUR_MS
    start_long_ms = end_ms - (DAYS_LONG + WARMUP_DAYS) * rs.DAY_MS
    min_bars_long = (WARMUP_DAYS + TRAIN_DAYS + TEST_DAYS) * rs.BARS_PER_DAY

    features_map = {}
    for symbol in rs.SYMBOLS:
        bars = rs.fetch_bars_1h(symbol, start_long_ms, end_ms)
        if len(bars) < min_bars_long:
            rs.log_event("symbol_skipped", symbol=symbol, bars_1h=len(bars), min_bars=min_bars_long)
            continue
        feat_1h, feat_4h = build_features_all(bars)
        features_map[symbol] = (feat_1h, feat_4h)
        rs.log_event(
            "data_loaded",
            symbol=symbol,
            bars_1h=len(bars),
            bars_4h=len(feat_4h.bars),
            gaps_1h=rs.count_gaps_1h(bars),
        )

    if not features_map:
        rs.log_event("no_data")
        return 2

    folds_365 = rs.build_folds(end_ms - DAYS_SHORT * rs.DAY_MS, end_ms, TRAIN_DAYS, TEST_DAYS)
    folds_730 = rs.build_folds(end_ms - DAYS_LONG * rs.DAY_MS, end_ms, TRAIN_DAYS, TEST_DAYS)

    cache = {}
    diag = diagnose_730d_baseline(features_map, folds_730, cache=cache)
    rs.log_event("baseline_730d_fold_diagnosis", **diag)

    accepted_365 = []
    accepted_730 = []

    for mode in MODES:
        for horizon_days, folds in ((DAYS_SHORT, folds_365), (DAYS_LONG, folds_730)):
            report = evaluate_mode_horizon(features_map, mode, horizon_days, folds, cache=cache)
            rs.log_event("mode_evaluation", **report)
            if report["verdict"] == "ACCEPTED":
                if horizon_days == DAYS_SHORT:
                    accepted_365.append(mode)
                elif horizon_days == DAYS_LONG:
                    accepted_730.append(mode)

    dual_accepted = [m for m in accepted_365 if m in accepted_730]
    rs.log_event(
        "regime_research_summary",
        modes_tested=list(MODES),
        accepted_365d=accepted_365,
        accepted_730d=accepted_730,
        dual_horizon_accepted=dual_accepted,
        result="DUAL_HORIZON_CANDIDATE_FOUND" if dual_accepted else "NO_DUAL_HORIZON_CANDIDATE",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
