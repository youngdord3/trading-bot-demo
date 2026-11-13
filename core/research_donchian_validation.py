"""Validazione multi-orizzonte (365g e 730g), matrice di rischio e stress test costi
per la strategia Donchian Trailing 1h + filtro 4h (Passo A.8).

Modulo di sola ricerca: nessun accesso al database, nessun segreto, nessun ordine.
Scarica una sola volta 730g + 30g warmup di candele 1h (18240 barre per simbolo)
e verifica senza modificare la griglia di segnale ne' i criteri OOS a priori:
  1. Orizzonte 365g (3 fold 180g/60g) e 730g (9 fold 180g/60g).
  2. Matrice di profili di rischio (risk_fraction 0.50%-1.00%, max_concurrent 2-3),
     rispettando fedelmente il tetto MAX_NOTIONAL_USDT di research_signals.py.
  3. Stress test costi di esecuzione (1.5x taker fee e slippage).
"""
import bisect
import os
import sys
import time
from typing import NamedTuple

import research_signals as rs

DAYS_LONG = int(os.environ.get("RV_DAYS_LONG", "730"))
DAYS_SHORT = int(os.environ.get("RV_DAYS_SHORT", "365"))
WARMUP_DAYS = int(os.environ.get("RV_WARMUP_DAYS", "30"))
TRAIN_DAYS = int(os.environ.get("RV_TRAIN_DAYS", "180"))
TEST_DAYS = int(os.environ.get("RV_TEST_DAYS", "60"))

STRESS_FEE_MULT = float(os.environ.get("RV_STRESS_FEE_MULT", "1.5"))
STRESS_SLIPPAGE_MULT = float(os.environ.get("RV_STRESS_SLIPPAGE_MULT", "1.5"))


class RiskProfile(NamedTuple):
    name: str
    risk_fraction: float
    max_concurrent: int


class ExecConfig(NamedTuple):
    risk_fraction: float
    fee_rate: float
    slippage_rate: float
    max_leverage: float = rs.MAX_LEVERAGE
    initial_equity: float = rs.INITIAL_EQUITY_USDT


RISK_PROFILES = (
    RiskProfile("baseline_100bps_3pos", 0.0100, 3),
    RiskProfile("moderate_075bps_3pos", 0.0075, 3),
    RiskProfile("moderate_075bps_2pos", 0.0075, 2),
    RiskProfile("conservative_050bps_3pos", 0.0050, 3),
    RiskProfile("conservative_050bps_2pos", 0.0050, 2),
)


def open_position_cfg(symbol, side, entry_time, raw_open, atr_value, params, cfg):
    """Apre una posizione a next-open applicando risk_fraction, cap nozionale e slippage."""
    entry_price = raw_open * (1.0 + cfg.slippage_rate * side)
    sl_mult = (
        rs.DONCHIAN_SL_MULT
        if params.mode in ("donchian_fixed", "donchian_trailing")
        else params.param_b
    )
    stop_dist = sl_mult * atr_value
    if stop_dist <= 0 or entry_price <= 0:
        return None

    risk_usdt = cfg.initial_equity * cfg.risk_fraction
    max_notional = cfg.initial_equity * cfg.max_leverage
    qty = min(risk_usdt / stop_dist, max_notional / entry_price)
    stop_price = entry_price - side * stop_dist

    if params.mode == "donchian_trailing":
        trail_dist = params.exit_mult * atr_value
        take_price = 0.0
    else:
        trail_dist = 0.0
        take_price = entry_price + side * params.exit_mult * stop_dist

    return rs.Position(
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


def exit_fill_cfg(position, bar, slippage_rate):
    """Verifica stop/take sulla barra corrente applicando slippage_rate su uscite a mercato."""
    if position.side == 1:
        if bar.low <= position.stop_price:
            reason = (
                "TRAIL"
                if position.trail_distance > 0 and position.stop_price > position.entry_price
                else "SL"
            )
            return (min(position.stop_price, bar.open) * (1.0 - slippage_rate), reason)
        if position.take_price > 0 and bar.high >= position.take_price:
            return (position.take_price, "TP")
        return None

    if bar.high >= position.stop_price:
        reason = (
            "TRAIL"
            if position.trail_distance > 0 and position.stop_price < position.entry_price
            else "SL"
        )
        return (max(position.stop_price, bar.open) * (1.0 + slippage_rate), reason)
    if position.take_price > 0 and bar.low <= position.take_price:
        return (position.take_price, "TP")
    return None


def close_position_cfg(position, exit_price, exit_time, reason, fee_rate):
    """Calcola PnL netto e commissioni con fee_rate esplicito."""
    gross = (exit_price - position.entry_price) * position.qty * position.side
    fees = (position.entry_price + exit_price) * position.qty * fee_rate
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


def simulate_cfg(symbol, features, params, start_ms, end_ms, cfg):
    """Simula un simbolo in [start_ms, end_ms) rispettando fedelmente la logica causale e i cap."""
    low_index = bisect.bisect_left(features.times, start_ms)
    high_index = bisect.bisect_left(features.times, end_ms)
    trades = []
    position = None
    next_entry_index = low_index

    for i in range(low_index, high_index):
        bar = features.bars[i]
        if position is not None:
            fill = exit_fill_cfg(position, bar, cfg.slippage_rate)
            if fill is not None:
                trades.append(
                    close_position_cfg(position, fill[0], bar.open_time + rs.HOUR_MS, fill[1], cfg.fee_rate)
                )
                position = None
                next_entry_index = i + rs.COOLDOWN_BARS
                continue
            if params.mode == "rsi_mr" and features.rsi14[i] is not None and i + 1 < high_index:
                if (position.side == 1 and features.rsi14[i] >= rs.RSI_EXIT_LONG) or (
                    position.side == -1 and features.rsi14[i] <= rs.RSI_EXIT_SHORT
                ):
                    next_bar = features.bars[i + 1]
                    exit_px = next_bar.open * (1.0 - cfg.slippage_rate * position.side)
                    trades.append(
                        close_position_cfg(position, exit_px, next_bar.open_time, "RSI_EXIT", cfg.fee_rate)
                    )
                    position = None
                    next_entry_index = i + 1 + rs.COOLDOWN_BARS
                    continue
            position = rs.advance_trailing(position, bar)
            continue

        if i < next_entry_index or i + 1 >= high_index:
            continue
        side = rs.signal_at(features, i, params)
        if side == 0:
            continue
        next_bar = features.bars[i + 1]
        position = open_position_cfg(
            symbol, side, next_bar.open_time, next_bar.open, features.atr14[i], params, cfg
        )

    if position is not None and high_index > low_index:
        last_bar = features.bars[high_index - 1]
        price = last_bar.close * (1.0 - cfg.slippage_rate * position.side)
        trades.append(
            close_position_cfg(position, price, last_bar.open_time + rs.HOUR_MS, "END", cfg.fee_rate)
        )
    return trades


def _raw_window_trades(features_by_symbol, params, start_ms, end_ms, cfg, cache):
    key = ("trades", params, start_ms, end_ms, cfg)
    if key not in cache:
        trades = []
        for symbol in sorted(features_by_symbol):
            trades.extend(simulate_cfg(symbol, features_by_symbol[symbol], params, start_ms, end_ms, cfg))
        cache[key] = trades
    return cache[key]


def _cached_funnel(features_by_symbol, params, start_ms, end_ms, cache):
    key = ("funnel", params, start_ms, end_ms)
    if key not in cache:
        cache[key] = rs.funnel_counts(features_by_symbol, params, start_ms, end_ms)
    return cache[key]


def run_window_profile(
    features_by_symbol,
    params,
    start_ms,
    end_ms,
    profile,
    fee_rate=rs.TAKER_FEE_RATE,
    slippage_rate=rs.SLIPPAGE_RATE,
    cache=None,
):
    """Simula i simboli con ExecConfig del profilo e applica il limite di posizioni simultanee."""
    store = cache if cache is not None else {}
    cfg = ExecConfig(profile.risk_fraction, fee_rate, slippage_rate)
    raw_trades = _raw_window_trades(features_by_symbol, params, start_ms, end_ms, cfg, store)
    return rs.apply_max_concurrent(raw_trades, profile.max_concurrent)


def select_params_profile(
    features_by_symbol,
    train_start,
    train_end,
    profile,
    fee_rate=rs.TAKER_FEE_RATE,
    slippage_rate=rs.SLIPPAGE_RATE,
    cache=None,
):
    """Seleziona i parametri migliori in-sample per donchian_trailing sotto profilo e costi dati."""
    store = cache if cache is not None else {}
    best = None
    outcomes = {}
    for params in rs.build_grid("donchian_trailing"):
        funnel = _cached_funnel(features_by_symbol, params, train_start, train_end, store)
        trades = run_window_profile(
            features_by_symbol,
            params,
            train_start,
            train_end,
            profile,
            fee_rate=fee_rate,
            slippage_rate=slippage_rate,
            cache=store,
        )
        result = rs.metrics(trades, rs.INITIAL_EQUITY_USDT)
        outcome = rs.classify_in_sample(funnel, result)
        outcomes[outcome] = outcomes.get(outcome, 0) + 1
        if outcome != "QUALIFIED":
            continue
        key = (min(result["profit_factor"], rs.PROFIT_FACTOR_CAP), result["net_pnl_usdt"])
        if best is None or key > best[0]:
            best = (key, params, result)
    if best is None:
        return (None, None, outcomes)
    return (best[1], best[2], outcomes)


def evaluate_horizon_profile(
    features_by_symbol,
    horizon_days,
    folds,
    profile,
    fee_rate=rs.TAKER_FEE_RATE,
    slippage_rate=rs.SLIPPAGE_RATE,
    cache=None,
):
    """Valuta un profilo su tutti i fold di un orizzonte temporale."""
    store = cache if cache is not None else {}
    oos_trades = []
    evaluated_folds = 0
    total_outcomes = {}
    fold_details = []

    for fold_idx, (train_start, train_end, test_end) in enumerate(folds, start=1):
        params, _, fold_outcomes = select_params_profile(
            features_by_symbol,
            train_start,
            train_end,
            profile,
            fee_rate=fee_rate,
            slippage_rate=slippage_rate,
            cache=store,
        )
        for k, v in fold_outcomes.items():
            total_outcomes[k] = total_outcomes.get(k, 0) + v
        if params is None:
            fold_details.append({"fold": fold_idx, "status": "NO_IS_CANDIDATE"})
            continue
        evaluated_folds += 1
        fold_trades = run_window_profile(
            features_by_symbol,
            params,
            train_end,
            test_end,
            profile,
            fee_rate=fee_rate,
            slippage_rate=slippage_rate,
            cache=store,
        )
        oos_trades.extend(fold_trades)
        fold_m = rs.metrics(fold_trades, rs.INITIAL_EQUITY_USDT)
        fold_details.append({
            "fold": fold_idx,
            "status": "EVALUATED",
            "params": rs.params_to_dict(params),
            "oos_trades": fold_m["trades"],
            "oos_pf": rs._json_safe(fold_m)["profit_factor"],
            "oos_net_pnl": round(fold_m["net_pnl_usdt"], 2),
            "oos_dd_pct": round(fold_m["max_drawdown_pct"], 2),
        })

    summary = rs.metrics(oos_trades, rs.INITIAL_EQUITY_USDT)
    accepted, reasons = rs.evaluate_acceptance(summary)
    by_symbol = {}
    for trade in oos_trades:
        entry = by_symbol.setdefault(trade.symbol, {"trades": 0, "net_pnl_usdt": 0.0})
        entry["trades"] += 1
        entry["net_pnl_usdt"] = round(entry["net_pnl_usdt"] + trade.net_pnl, 4)

    return {
        "horizon_days": horizon_days,
        "profile": profile.name,
        "risk_fraction": profile.risk_fraction,
        "max_concurrent": profile.max_concurrent,
        "folds_total": len(folds),
        "folds_evaluated": evaluated_folds,
        "in_sample_outcomes": total_outcomes,
        "folds": fold_details,
        "verdict": "ACCEPTED" if accepted else "REJECTED",
        "reasons": reasons,
        "out_of_sample": rs._json_safe(summary),
        "by_symbol": by_symbol,
    }


def _select_stress_profiles(accepted_names, reports_by_key):
    """Sceglie i profili da sottoporre a stress test: tutti gli accettati o il miglior 365d."""
    chosen = [p for p in RISK_PROFILES if p.name in accepted_names]
    if chosen:
        return chosen
    best_profile = None
    best_score = None
    for profile in RISK_PROFILES:
        rep = reports_by_key.get((profile.name, DAYS_SHORT))
        if rep is None or rep["out_of_sample"]["trades"] < rs.ACCEPT_MIN_TRADES:
            continue
        score = (rep["out_of_sample"]["profit_factor"], rep["out_of_sample"]["net_pnl_usdt"])
        if best_score is None or score > best_score:
            best_score = score
            best_profile = profile
    return [best_profile] if best_profile is not None else []


def main():
    for symbol in rs.SYMBOLS:
        if not rs.SYMBOL_PATTERN.match(symbol):
            rs.log_event("invalid_symbol", symbol=symbol)
            return 2

    end_ms = (int(time.time() * 1000) // rs.HOUR_MS) * rs.HOUR_MS
    start_long_ms = end_ms - (DAYS_LONG + WARMUP_DAYS) * rs.DAY_MS
    min_bars_long = (WARMUP_DAYS + TRAIN_DAYS + TEST_DAYS) * rs.BARS_PER_DAY

    features_by_symbol = {}
    for symbol in rs.SYMBOLS:
        bars = rs.fetch_bars_1h(symbol, start_long_ms, end_ms)
        if len(bars) < min_bars_long:
            rs.log_event("symbol_skipped", symbol=symbol, bars_1h=len(bars), min_bars=min_bars_long)
            continue
        features_by_symbol[symbol] = rs.build_features(bars)
        rs.log_event("data_loaded", symbol=symbol, bars_1h=len(bars), gaps=rs.count_gaps_1h(bars))

    if not features_by_symbol:
        rs.log_event("no_data")
        return 2

    folds_365 = rs.build_folds(end_ms - DAYS_SHORT * rs.DAY_MS, end_ms, TRAIN_DAYS, TEST_DAYS)
    folds_730 = rs.build_folds(end_ms - DAYS_LONG * rs.DAY_MS, end_ms, TRAIN_DAYS, TEST_DAYS)
    horizons = [(DAYS_SHORT, folds_365), (DAYS_LONG, folds_730)]

    cache = {}
    reports_by_key = {}
    accepted_365 = []
    accepted_730 = []

    for profile in RISK_PROFILES:
        for horizon_days, folds in horizons:
            report = evaluate_horizon_profile(
                features_by_symbol, horizon_days, folds, profile, cache=cache
            )
            reports_by_key[(profile.name, horizon_days)] = report
            rs.log_event("profile_evaluation", **report)
            if report["verdict"] == "ACCEPTED":
                if horizon_days == DAYS_SHORT:
                    accepted_365.append(profile.name)
                elif horizon_days == DAYS_LONG:
                    accepted_730.append(profile.name)

    dual_accepted = [name for name in accepted_365 if name in accepted_730]

    stress_passed_365 = []
    stress_passed_730 = []
    stress_fee = rs.TAKER_FEE_RATE * STRESS_FEE_MULT
    stress_slip = rs.SLIPPAGE_RATE * STRESS_SLIPPAGE_MULT

    for profile in _select_stress_profiles(set(accepted_365) | set(accepted_730), reports_by_key):
        for horizon_days, folds in horizons:
            stress_report = evaluate_horizon_profile(
                features_by_symbol,
                horizon_days,
                folds,
                profile,
                fee_rate=stress_fee,
                slippage_rate=stress_slip,
                cache=cache,
            )
            rs.log_event(
                "cost_stress_test",
                fee_mult=STRESS_FEE_MULT,
                slippage_mult=STRESS_SLIPPAGE_MULT,
                **stress_report,
            )
            if stress_report["verdict"] == "ACCEPTED":
                if horizon_days == DAYS_SHORT:
                    stress_passed_365.append(profile.name)
                elif horizon_days == DAYS_LONG:
                    stress_passed_730.append(profile.name)

    rs.log_event(
        "validation_summary",
        accepted_365d=accepted_365,
        accepted_730d=accepted_730,
        dual_horizon_accepted=dual_accepted,
        stress_test_365d_accepted=stress_passed_365,
        stress_test_730d_accepted=stress_passed_730,
        result="CANDIDATE_CONFIRMED" if dual_accepted else "ONLY_365D_OR_REJECTED",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
