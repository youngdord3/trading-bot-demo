"""Validazione approfondita del paradigma Giornaliero 1d Donchian Trailing e Ibrido 1d+1h (Passo A.11).

Modulo di sola ricerca: nessun accesso al database, nessun segreto, nessun ordine.
Scarica una sola volta 780g di candele 1h (720g = 12 bimestri completi + 60g warmup = 18720 barre
per simbolo) e lo storico Funding Rate 8h (/fapi/v1/fundingRate), eseguendo 4 verifiche:
  1. Finestre continue bimestrali per regole fisse (senza sprecare 180g di train vuoto):
     - 360g (ultimi 6 bimestri da 60g) e 720g (tutti i 12 bimestri da 60g) + simulazione
       continua 365g/730g senza chiusure forzate ogni 60g, a 0.50% e 1.00% risk.
  2. Stabilita' dell'intorno dei parametri (Parameter Sensitivity Grid 18 combinazioni su 1d):
     - dc_period in (12, 15, 20), adx_min in (15.0, 18.0, 20.0), trail_mult in (2.5, 3.0)
     - Verifica che il risultato non dipenda da un singolo picco isolato (overfitting).
  3. Stress test costi di esecuzione (1.5x e 2.0x su taker fee + slippage, funding 8h incluso).
  4. Strategia Ibrida h1_d1_macro_gated:
     - Segnale 1h Donchian(20) + Funding Gate abilitato solo quando il trend Giornaliero (1d)
       chiuso il giorno precedente e' allineato (EMA20_1d >= EMA50_1d e ADX14_1d >= 18).
"""
import bisect
import os
import sys
import time
from typing import NamedTuple

import research_paradigms as rp
import research_signals as rs

BIMESTER_DAYS = 60
BIMESTERS_SHORT = 6    # 6 * 60g = 360g (1 anno completo diviso in 6 bimestri)
BIMESTERS_LONG = 12    # 12 * 60g = 720g (2 anni completi divisi in 12 bimestri)
WARMUP_DAYS = int(os.environ.get("RD_WARMUP_DAYS", "60"))

DC_PERIODS_1D = (12, 15, 20)
ADX_MINS_1D = (15.0, 18.0, 20.0)
TRAIL_MULTS_1D = (2.5, 3.0)

SL_MULT_1D = 1.5
MAX_BREAKOUT_ATR_1D = 1.8


class D1Config(NamedTuple):
    name: str
    dc_period: int
    adx_min: float
    trail_mult: float
    risk_fraction: float
    max_concurrent: int
    fee_rate: float
    slippage_rate: float


class ExtendedD1Features(NamedTuple):
    bars: list
    times: list
    ema20: list
    ema50: list
    atr14: list
    adx14: list
    dc_high: dict
    dc_low: dict


class ExtendedBundle(NamedTuple):
    feat_1h: rs.Features
    funding_1h: list
    funding_sum3_1h: list
    trend_1d_on_1h: list
    feat_1d: ExtendedD1Features
    funding_map: dict


CANONICAL_PROFILES = (
    D1Config("d1_canonical_050bps", 15, 18.0, 2.5, 0.005, 3, rs.TAKER_FEE_RATE, rs.SLIPPAGE_RATE),
    D1Config("d1_canonical_100bps", 15, 18.0, 2.5, 0.010, 3, rs.TAKER_FEE_RATE, rs.SLIPPAGE_RATE),
)


def last_closed_1d_bucket(open_time_1h):
    """Indice dell'ultimo bucket giornaliero 1d gia' chiuso alla chiusura della candela 1h."""
    return (open_time_1h + rs.HOUR_MS) // rs.DAY_MS - 1


def build_bimesters(end_ms, count, bimester_days=BIMESTER_DAYS):
    """Costruisce `count` finestre bimestrali contigue [start_ms, end_ms) fino a end_ms."""
    span_ms = bimester_days * rs.DAY_MS
    start_ms = end_ms - count * span_ms
    return [(start_ms + i * span_ms, start_ms + (i + 1) * span_ms) for i in range(count)]


def build_extended_bundle(bars_1h, funding_points):
    """Costruisce feature 1d multi-periodo e mappa il regime 1d causale sulle candele 1h."""
    feat_1h = rs.build_features(bars_1h)
    funding_1h, funding_sum3_1h = rp.align_funding_series(feat_1h.times, rs.HOUR_MS, funding_points)

    bars_1d = rp.aggregate_span(bars_1h, rs.DAY_MS)
    closes_1d = [b.close for b in bars_1d]
    highs_1d = [b.high for b in bars_1d]
    lows_1d = [b.low for b in bars_1d]
    ema20_1d = rs.ema(closes_1d, 20)
    ema50_1d = rs.ema(closes_1d, 50)
    atr14_1d = rs.atr(highs_1d, lows_1d, closes_1d, 14)
    adx14_1d = rs.adx(highs_1d, lows_1d, closes_1d, 14)

    dc_high = {}
    dc_low = {}
    for period in DC_PERIODS_1D:
        u, l = rs.donchian_bounds(highs_1d, lows_1d, period)
        dc_high[period] = u
        dc_low[period] = l

    feat_1d = ExtendedD1Features(
        bars=bars_1d,
        times=[b.open_time for b in bars_1d],
        ema20=ema20_1d,
        ema50=ema50_1d,
        atr14=atr14_1d,
        adx14=adx14_1d,
        dc_high=dc_high,
        dc_low=dc_low,
    )

    trend_map_1d = {}
    for i, bar in enumerate(bars_1d):
        if ema20_1d[i] is None or ema50_1d[i] is None or adx14_1d[i] is None:
            continue
        if adx14_1d[i] < 18.0:
            trend_map_1d[bar.open_time // rs.DAY_MS] = 0
        elif bar.close > ema20_1d[i] >= ema50_1d[i]:
            trend_map_1d[bar.open_time // rs.DAY_MS] = 1
        elif bar.close < ema20_1d[i] <= ema50_1d[i]:
            trend_map_1d[bar.open_time // rs.DAY_MS] = -1
        else:
            trend_map_1d[bar.open_time // rs.DAY_MS] = 0

    trend_1d_on_1h = [
        trend_map_1d.get(last_closed_1d_bucket(b.open_time), 0) for b in bars_1h
    ]
    funding_map = {p.funding_time: p.rate for p in funding_points}

    return ExtendedBundle(
        feat_1h=feat_1h,
        funding_1h=funding_1h,
        funding_sum3_1h=funding_sum3_1h,
        trend_1d_on_1h=trend_1d_on_1h,
        feat_1d=feat_1d,
        funding_map=funding_map,
    )


# ---------------------------------------------------------------------------
# Simulazione 1d parametrizzata (rispetta fee_rate, slippage_rate e funding 8h)
# ---------------------------------------------------------------------------


def signal_d1_cfg(feat_1d, index, cfg):
    if index < 1:
        return 0
    atr_val = feat_1d.atr14[index]
    adx_val = feat_1d.adx14[index]
    ema20_val = feat_1d.ema20[index]
    ema50_val = feat_1d.ema50[index]
    if any(v is None for v in (atr_val, adx_val, ema20_val, ema50_val)) or atr_val <= 0:
        return 0
    upper = feat_1d.dc_high[cfg.dc_period][index]
    lower = feat_1d.dc_low[cfg.dc_period][index]
    if upper is None or lower is None or adx_val < cfg.adx_min:
        return 0
    close = feat_1d.bars[index].close
    if close > ema20_val >= ema50_val and close > upper:
        if (close - upper) / atr_val <= MAX_BREAKOUT_ATR_1D:
            return 1
    if close < ema20_val <= ema50_val and close < lower:
        if (lower - close) / atr_val <= MAX_BREAKOUT_ATR_1D:
            return -1
    return 0


def _open_pos_d1(symbol, side, entry_time, raw_open, atr_val, cfg):
    entry_price = raw_open * (1.0 + cfg.slippage_rate * side)
    stop_dist = SL_MULT_1D * atr_val
    if stop_dist <= 0 or entry_price <= 0:
        return None
    risk_usdt = rs.INITIAL_EQUITY_USDT * cfg.risk_fraction
    qty = min(risk_usdt / stop_dist, rs.MAX_NOTIONAL_USDT / entry_price)
    stop_price = entry_price - side * stop_dist
    trail_dist = cfg.trail_mult * atr_val
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


def _exit_fill_d1(position, bar, slippage_rate):
    if position.side == 1:
        if bar.low <= position.stop_price:
            reason = (
                "TRAIL"
                if position.trail_distance > 0 and position.stop_price > position.entry_price
                else "SL"
            )
            return (min(position.stop_price, bar.open) * (1.0 - slippage_rate), reason)
        return None
    if bar.high >= position.stop_price:
        reason = (
            "TRAIL"
            if position.trail_distance > 0 and position.stop_price < position.entry_price
            else "SL"
        )
        return (max(position.stop_price, bar.open) * (1.0 + slippage_rate), reason)
    return None


def _close_pos_d1(position, exit_price, exit_time, reason, funding_accrued, fee_rate):
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
        net_pnl=gross - fees + funding_accrued,
        fees=fees,
        reason=reason,
    )


def simulate_d1_cfg(symbol, ext_bundle, start_ms, end_ms, cfg):
    feat = ext_bundle.feat_1d
    fmap = ext_bundle.funding_map
    low_idx = bisect.bisect_left(feat.times, start_ms)
    high_idx = bisect.bisect_left(feat.times, end_ms)
    trades = []
    position = None
    funding_acc = 0.0

    for i in range(low_idx, high_idx):
        bar = feat.bars[i]
        if position is not None:
            funding_acc += rp.funding_pnl_for_bar(
                position, bar.open_time, rs.DAY_MS, fmap, bar.close
            )
            fill = _exit_fill_d1(position, bar, cfg.slippage_rate)
            if fill is not None:
                trades.append(
                    _close_pos_d1(
                        position, fill[0], bar.open_time + rs.DAY_MS, fill[1], funding_acc, cfg.fee_rate
                    )
                )
                position = None
                funding_acc = 0.0
                continue
            position = rs.advance_trailing(position, bar)
            continue
        if i + 1 >= high_idx:
            continue
        side = signal_d1_cfg(feat, i, cfg)
        if side == 0:
            continue
        next_bar = feat.bars[i + 1]
        position = _open_pos_d1(symbol, side, next_bar.open_time, next_bar.open, feat.atr14[i], cfg)
        funding_acc = 0.0

    if position is not None and high_idx > low_index_safe(low_idx):
        last_bar = feat.bars[high_idx - 1]
        price = last_bar.close * (1.0 - cfg.slippage_rate * position.side)
        trades.append(
            _close_pos_d1(
                position, price, last_bar.open_time + rs.DAY_MS, "END", funding_acc, cfg.fee_rate
            )
        )
    return trades


def low_index_safe(idx):
    return idx


def evaluate_d1_continuous(bundles_by_symbol, horizon_days, end_ms, cfg):
    """Simulazione continua senza chiusure forzate intermedie ogni 60 giorni."""
    start_ms = end_ms - horizon_days * rs.DAY_MS
    trades = run_d1_window(bundles_by_symbol, start_ms, end_ms, cfg)
    summary = rs.metrics(trades, rs.INITIAL_EQUITY_USDT)
    accepted, reasons = rs.evaluate_acceptance(summary)
    by_symbol = {}
    for trade in trades:
        entry = by_symbol.setdefault(trade.symbol, {"trades": 0, "net_pnl_usdt": 0.0})
        entry["trades"] += 1
        entry["net_pnl_usdt"] = round(entry["net_pnl_usdt"] + trade.net_pnl, 4)
    return {
        "profile": cfg.name,
        "horizon_days": horizon_days,
        "mode": "continuous",
        "verdict": "ACCEPTED" if accepted else "REJECTED",
        "reasons": reasons,
        "out_of_sample": rs._json_safe(summary),
        "by_symbol": by_symbol,
    }


# ---------------------------------------------------------------------------
# Simulazione Ibrida h1_d1_macro_gated (1h Donchian + 1d Macro Trend + Funding Gate)
# ---------------------------------------------------------------------------


def simulate_h1_d1_macro_gated(symbol, ext_bundle, start_ms, end_ms, risk_fraction=0.005):
    """Esegue il segnale 1h Donchian(20, ADX>=25) + Funding Gate solo se il regime
    Giornaliero (1d) chiuso il giorno precedente concorda nella stessa direzione.
    """
    feat = ext_bundle.feat_1h
    funding_1h = ext_bundle.funding_1h
    funding_sum3 = ext_bundle.funding_sum3_1h
    trend_1d = ext_bundle.trend_1d_on_1h
    fmap = ext_bundle.funding_map
    params = rs.SignalParams("donchian_trailing", 20.0, 25.0, 3.0)

    low_idx = bisect.bisect_left(feat.times, start_ms)
    high_idx = bisect.bisect_left(feat.times, end_ms)
    trades = []
    position = None
    funding_acc = 0.0
    next_entry_idx = low_idx

    for i in range(low_idx, high_idx):
        bar = feat.bars[i]
        if position is not None:
            funding_acc += rp.funding_pnl_for_bar(
                position, bar.open_time, rs.HOUR_MS, fmap, bar.close
            )
            fill = rs.exit_fill(position, bar)
            if fill is not None:
                trades.append(
                    rp.close_pos_with_funding(
                        position, fill[0], bar.open_time + rs.HOUR_MS, fill[1], funding_acc
                    )
                )
                position = None
                funding_acc = 0.0
                next_entry_idx = i + rs.COOLDOWN_BARS
                continue
            position = rs.advance_trailing(position, bar)
            continue
        if i < next_entry_idx or i + 1 >= high_idx:
            continue
        side = rs.signal_at(feat, i, params)
        if side == 0 or trend_1d[i] != side:
            continue
        fr = funding_1h[i]
        fsum = funding_sum3[i]
        if side == 1 and (fr >= rp.FUNDING_CROWDED_LONG or fsum >= rp.FUNDING_CROWDED_LONG * 2.5):
            continue
        if side == -1 and (fr <= rp.FUNDING_CROWDED_SHORT or fsum <= rp.FUNDING_CROWDED_SHORT * 2.5):
            continue
        next_bar = feat.bars[i + 1]
        position = rp.open_pos_sized(
            symbol, side, next_bar.open_time, next_bar.open, feat.atr14[i], 1.5, 3.0, risk_frac=risk_fraction
        )
        funding_acc = 0.0

    if position is not None and high_idx > low_idx:
        last_bar = feat.bars[high_idx - 1]
        price = last_bar.close * (1.0 - rs.SLIPPAGE_RATE * position.side)
        trades.append(
            rp.close_pos_with_funding(
                position, price, last_bar.open_time + rs.HOUR_MS, "END", funding_acc
            )
        )
    return trades


# ---------------------------------------------------------------------------
# Valutatori bimestrali, studio di sensibilita' e stress test costi
# ---------------------------------------------------------------------------


def run_d1_window(bundles_by_symbol, start_ms, end_ms, cfg):
    raw = []
    for symbol in sorted(bundles_by_symbol):
        raw.extend(simulate_d1_cfg(symbol, bundles_by_symbol[symbol], start_ms, end_ms, cfg))
    return rs.apply_max_concurrent(raw, cfg.max_concurrent)


def evaluate_d1_bimesters(bundles_by_symbol, horizon_days, bimesters, cfg):
    oos_trades = []
    winning_bimesters = 0
    losing_bimesters = 0
    bimester_details = []

    for idx, (b_start, b_end) in enumerate(bimesters, start=1):
        b_trades = run_d1_window(bundles_by_symbol, b_start, b_end, cfg)
        oos_trades.extend(b_trades)
        bm = rs.metrics(b_trades, rs.INITIAL_EQUITY_USDT)
        if bm["net_pnl_usdt"] > 0:
            winning_bimesters += 1
        elif bm["trades"] > 0:
            losing_bimesters += 1
        bimester_details.append({
            "bimester": idx,
            "trades": bm["trades"],
            "pf": "inf" if bm["profit_factor"] == float("inf") else round(bm["profit_factor"], 4),
            "net_pnl": round(bm["net_pnl_usdt"], 4),
            "dd_pct": round(bm["max_drawdown_pct"], 4),
        })

    summary = rs.metrics(oos_trades, rs.INITIAL_EQUITY_USDT)
    accepted, reasons = rs.evaluate_acceptance(summary)
    by_symbol = {}
    for trade in oos_trades:
        entry = by_symbol.setdefault(trade.symbol, {"trades": 0, "net_pnl_usdt": 0.0})
        entry["trades"] += 1
        entry["net_pnl_usdt"] = round(entry["net_pnl_usdt"] + trade.net_pnl, 4)

    return {
        "profile": cfg.name,
        "horizon_days": horizon_days,
        "bimesters_total": len(bimesters),
        "winning_bimesters": winning_bimesters,
        "losing_bimesters": losing_bimesters,
        "verdict": "ACCEPTED" if accepted else "REJECTED",
        "reasons": reasons,
        "out_of_sample": rs._json_safe(summary),
        "by_symbol": by_symbol,
        "bimesters": bimester_details,
    }


def evaluate_d1_sensitivity_grid(bundles_by_symbol, bimesters_360, bimesters_720, risk_fraction=0.005):
    """Valuta le 18 combinazioni nell'intorno di (dc=15, adx=18, trail=2.5) su 360g e 720g."""
    grid_configs = [
        D1Config(
            f"dc{dc}_adx{int(adx)}_tr{str(tr).replace('.', '')}",
            dc,
            adx,
            tr,
            risk_fraction,
            3,
            rs.TAKER_FEE_RATE,
            rs.SLIPPAGE_RATE,
        )
        for dc in DC_PERIODS_1D
        for adx in ADX_MINS_1D
        for tr in TRAIL_MULTS_1D
    ]
    positive_360 = 0
    accepted_360 = 0
    positive_720 = 0
    accepted_720 = 0
    rows = []

    for cfg in grid_configs:
        rep_360 = evaluate_d1_bimesters(bundles_by_symbol, 360, bimesters_360, cfg)
        rep_720 = evaluate_d1_bimesters(bundles_by_symbol, 720, bimesters_720, cfg)
        m360 = rep_360["out_of_sample"]
        m720 = rep_720["out_of_sample"]
        if m360["net_pnl_usdt"] > 0:
            positive_360 += 1
        if rep_360["verdict"] == "ACCEPTED":
            accepted_360 += 1
        if m720["net_pnl_usdt"] > 0:
            positive_720 += 1
        if rep_720["verdict"] == "ACCEPTED":
            accepted_720 += 1
        rows.append({
            "config": cfg.name,
            "pnl_360d": m360["net_pnl_usdt"],
            "pf_360d": m360["profit_factor"],
            "trades_360d": m360["trades"],
            "verdict_360d": rep_360["verdict"],
            "pnl_720d": m720["net_pnl_usdt"],
            "pf_720d": m720["profit_factor"],
            "trades_720d": m720["trades"],
            "verdict_720d": rep_720["verdict"],
        })

    return {
        "total_configs": len(grid_configs),
        "positive_360d": positive_360,
        "accepted_360d": accepted_360,
        "positive_720d": positive_720,
        "accepted_720d": accepted_720,
        "both_horizons_accepted_count": sum(
            1 for r in rows if r["verdict_360d"] == "ACCEPTED" and r["verdict_720d"] == "ACCEPTED"
        ),
        "grid_results": rows,
    }


def evaluate_h1_d1_hybrid(bundles_by_symbol, horizon_days, bimesters):
    oos_trades = []
    winning_bimesters = 0
    losing_bimesters = 0
    for b_start, b_end in bimesters:
        raw = []
        for symbol in sorted(bundles_by_symbol):
            raw.extend(simulate_h1_d1_macro_gated(symbol, bundles_by_symbol[symbol], b_start, b_end))
        b_trades = rs.apply_max_concurrent(raw, 3)
        oos_trades.extend(b_trades)
        bm = rs.metrics(b_trades, rs.INITIAL_EQUITY_USDT)
        if bm["net_pnl_usdt"] > 0:
            winning_bimesters += 1
        elif bm["trades"] > 0:
            losing_bimesters += 1

    summary = rs.metrics(oos_trades, rs.INITIAL_EQUITY_USDT)
    accepted, reasons = rs.evaluate_acceptance(summary)
    by_symbol = {}
    for trade in oos_trades:
        entry = by_symbol.setdefault(trade.symbol, {"trades": 0, "net_pnl_usdt": 0.0})
        entry["trades"] += 1
        entry["net_pnl_usdt"] = round(entry["net_pnl_usdt"] + trade.net_pnl, 4)

    return {
        "mode": "h1_d1_macro_gated",
        "horizon_days": horizon_days,
        "bimesters_total": len(bimesters),
        "winning_bimesters": winning_bimesters,
        "losing_bimesters": losing_bimesters,
        "verdict": "ACCEPTED" if accepted else "REJECTED",
        "reasons": reasons,
        "out_of_sample": rs._json_safe(summary),
        "by_symbol": by_symbol,
    }


def main():
    for symbol in rs.SYMBOLS:
        if not rs.SYMBOL_PATTERN.match(symbol):
            rs.log_event("invalid_symbol", symbol=symbol)
            return 2

    end_ms = (int(time.time() * 1000) // rs.DAY_MS) * rs.DAY_MS
    total_days = BIMESTERS_LONG * BIMESTER_DAYS + WARMUP_DAYS
    start_ms = end_ms - total_days * rs.DAY_MS
    min_bars = total_days * rs.BARS_PER_DAY

    bundles_by_symbol = {}
    for symbol in rs.SYMBOLS:
        bars = rs.fetch_bars_1h(symbol, start_ms, end_ms)
        if len(bars) < min_bars:
            rs.log_event("symbol_skipped", symbol=symbol, bars_1h=len(bars), min_bars=min_bars)
            continue
        funding = rp.fetch_funding_rates(symbol, start_ms, end_ms)
        bundle = build_extended_bundle(bars, funding)
        bundles_by_symbol[symbol] = bundle
        rs.log_event(
            "data_loaded",
            symbol=symbol,
            bars_1h=len(bars),
            bars_1d=len(bundle.feat_1d.bars),
            funding_points=len(funding),
            gaps_1h=rs.count_gaps_1h(bars),
        )

    if not bundles_by_symbol:
        rs.log_event("no_data")
        return 2

    bimesters_360 = build_bimesters(end_ms, BIMESTERS_SHORT)
    bimesters_720 = build_bimesters(end_ms, BIMESTERS_LONG)

    dual_accepted_bimesters = []
    dual_accepted_continuous = []
    for cfg in CANONICAL_PROFILES:
        rep_360 = evaluate_d1_bimesters(bundles_by_symbol, 360, bimesters_360, cfg)
        rep_720 = evaluate_d1_bimesters(bundles_by_symbol, 720, bimesters_720, cfg)
        rs.log_event("d1_bimester_evaluation", **rep_360)
        rs.log_event("d1_bimester_evaluation", **rep_720)
        if rep_360["verdict"] == "ACCEPTED" and rep_720["verdict"] == "ACCEPTED":
            dual_accepted_bimesters.append(cfg.name)

        cont_365 = evaluate_d1_continuous(bundles_by_symbol, 365, end_ms, cfg)
        cont_730 = evaluate_d1_continuous(bundles_by_symbol, 730, end_ms, cfg)
        rs.log_event("d1_continuous_evaluation", **cont_365)
        rs.log_event("d1_continuous_evaluation", **cont_730)
        if cont_365["verdict"] == "ACCEPTED" and cont_730["verdict"] == "ACCEPTED":
            dual_accepted_continuous.append(cfg.name)

    sensitivity = evaluate_d1_sensitivity_grid(bundles_by_symbol, bimesters_360, bimesters_720)
    rs.log_event("d1_neighborhood_stability", **sensitivity)

    stress_passed = []
    base_cfg = CANONICAL_PROFILES[0]
    for mult in (1.5, 2.0):
        stress_cfg = base_cfg._replace(
            name=f"{base_cfg.name}_cost_{str(mult).replace('.', '')}x",
            fee_rate=rs.TAKER_FEE_RATE * mult,
            slippage_rate=rs.SLIPPAGE_RATE * mult,
        )
        s_360 = evaluate_d1_bimesters(bundles_by_symbol, 360, bimesters_360, stress_cfg)
        s_720 = evaluate_d1_bimesters(bundles_by_symbol, 720, bimesters_720, stress_cfg)
        rs.log_event("d1_cost_stress_test", cost_multiplier=mult, **s_360)
        rs.log_event("d1_cost_stress_test", cost_multiplier=mult, **s_720)
        if s_360["verdict"] == "ACCEPTED" and s_720["verdict"] == "ACCEPTED":
            stress_passed.append(f"{mult}x")

    hyb_360 = evaluate_h1_d1_hybrid(bundles_by_symbol, 360, bimesters_360)
    hyb_720 = evaluate_h1_d1_hybrid(bundles_by_symbol, 720, bimesters_720)
    rs.log_event("hybrid_h1_d1_evaluation", **hyb_360)
    rs.log_event("hybrid_h1_d1_evaluation", **hyb_720)

    confirmed = bool(dual_accepted_bimesters or dual_accepted_continuous)
    rs.log_event(
        "d1_validation_summary",
        dual_accepted_bimesters=dual_accepted_bimesters,
        dual_accepted_continuous=dual_accepted_continuous,
        neighborhood_both_accepted=sensitivity["both_horizons_accepted_count"],
        neighborhood_total=sensitivity["total_configs"],
        stress_test_dual_passed=stress_passed,
        hybrid_360d_verdict=hyb_360["verdict"],
        hybrid_720d_verdict=hyb_720["verdict"],
        result="D1_CANDIDATE_CONFIRMED" if confirmed else "NO_D1_CANDIDATE",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
