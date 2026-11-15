"""Ablation mirata e stress test sui candidati V2 emersi su 2021-2026.

Legge direttamente dalla cache locale in backups/research_cache/ (zero chiamate API).
Verifica in dettaglio:
  1. Plateau del Signal Ranking (score composito vs solo RS vs solo ER10) per escludere overfitting.
  2. Effetto isolato di max_same_side=2, max_open=3 vs 4, funding simmetrico vs asimmetrico,
     short scaling (1.0 / 0.75 / 0.60), e trailing stop (static 2.0/2.25/2.5, ratchet sopra +2.5R,
     clamped dynamic ATR = min(atr_entry, atr_now)).
  3. Portafoglio ibrido/combinato (50% A2_TSMOM + 50% RS_LEADER_MAX2) e risk sizing (1.0% vs 1.25%).
  4. Audit completo per anno (2021..2026), peggior mese/trade, max underwater days,
     Taker vs Maker Limit e Stress Costi 1.5x / 2.0x.
"""

from datetime import datetime, timezone
import json
import math
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import research_signals as rs
import research_v2_2021_2026 as rv2


def _eval_rank_mode(tf, idx, rank_mode):
    atr_val = tf.atr14[idx]
    adx_val = tf.adx14[idx]
    er_val = tf.er[idx]
    ema_f = tf.ema_fast[idx]
    ema_s = tf.ema_slow[idx]
    r_s = tf.ret_short[idx]
    r_m = tf.ret_med[idx]
    r_l = tf.ret_long[idx]
    upper = tf.dc_high_10[idx]
    lower = tf.dc_low_10[idx]
    if any(v is None for v in (atr_val, adx_val, er_val, ema_f, ema_s, r_s, r_m, r_l, upper, lower)):
        return 0, 0.0
    if atr_val <= 0 or adx_val < 18.0 or er_val < 0.30:
        return 0, 0.0
    close = tf.bars[idx].close
    if close <= 0:
        return 0, 0.0
    vol_pct = atr_val / close

    def score_for(direction):
        if rank_mode == "er_only":
            return er_val
        if rank_mode == "rs_only":
            return (direction * r_m) / max(vol_pct, 1e-6)
        return er_val * max(0.0, direction * r_m) / max(vol_pct, 1e-6)

    if (
        r_s > 0.03
        and r_l > 0.05
        and close > ema_f >= ema_s
        and close > upper
        and (close - upper) / atr_val <= 1.8
    ):
        return 1, score_for(1)
    if (
        r_s < -0.03
        and r_l < -0.05
        and close < ema_f <= ema_s
        and close < lower
        and (lower - close) / atr_val <= 1.8
    ):
        return -1, score_for(-1)
    return 0, 0.0


def run_custom_backtest(
    feats_1d,
    start_ms,
    end_ms,
    *,
    name,
    compounding=False,
    initial_equity=1000.0,
    risk_long=0.01,
    risk_short=0.01,
    max_open=3,
    max_same_side=2,
    rank_mode="composite",      # "tuple", "composite", "rs_only", "er_only"
    funding_mode="v1_asym",     # "v1_asym", "sym_30bps"
    trail_style="static",       # "static", "clamped_dyn", "ratchet_static"
    trail_mult=2.0,
    sl_mult=1.5,
    fee_rate=0.0005,
    slippage_rate=0.0002,
    stop_slippage_rate=0.0002,
    max_notional_mult=2.0,
):
    all_times = sorted({t for tf in feats_1d.values() for t in tf.times if start_ms <= t < end_ms})
    equity_realized = initial_equity
    open_positions = {}
    closed_trades = []
    equity_curve = []
    pending = []

    for step_idx, t_ms in enumerate(all_times):
        if pending:
            base_eq = equity_realized if compounding else initial_equity
            for _pkey, sym, sym_idx, side, atr_sig in pending:
                if len(open_positions) >= max_open:
                    break
                if sym in open_positions:
                    continue
                if sum(1 for p in open_positions.values() if p.side == side) >= max_same_side:
                    continue
                tf = feats_1d[sym]
                bar_idx = tf.time_to_idx.get(t_ms)
                if bar_idx is None:
                    continue
                bar = tf.bars[bar_idx]
                entry_price = bar.open * (1.0 + slippage_rate * side)
                stop_dist = sl_mult * atr_sig
                if stop_dist <= 0 or entry_price <= 0:
                    continue
                rf = risk_long if side == 1 else risk_short
                qty = min((max(0.0, base_eq) * rf) / stop_dist, (max(0.0, base_eq) * max_notional_mult) / entry_price)
                if qty <= 0:
                    continue
                open_positions[sym] = rv2.OpenPos(
                    symbol=sym,
                    sym_idx=sym_idx,
                    side=side,
                    entry_time=t_ms,
                    entry_price=entry_price,
                    qty=qty,
                    stop_price=entry_price - side * stop_dist,
                    initial_risk_dist=stop_dist,
                    trail_dist_entry=trail_mult * atr_sig,
                    best_price=entry_price,
                )
            pending = []

        to_close = []
        for sym, pos in open_positions.items():
            tf = feats_1d[sym]
            bar_idx = tf.time_to_idx.get(t_ms)
            if bar_idx is None:
                continue
            bar = tf.bars[bar_idx]
            pos.bars_held += 1
            pos.funding_accrued += rv2.rp.funding_pnl_for_bar(
                pos, bar.open_time, rs.DAY_MS, tf.funding_map, bar.close
            )
            exit_px = None
            reason = None
            if pos.side == 1 and bar.low <= pos.stop_price:
                reason = "TRAIL" if pos.stop_price > pos.entry_price else "SL"
                exit_px = min(pos.stop_price, bar.open) * (1.0 - stop_slippage_rate)
            elif pos.side == -1 and bar.high >= pos.stop_price:
                reason = "TRAIL" if pos.stop_price < pos.entry_price else "SL"
                exit_px = max(pos.stop_price, bar.open) * (1.0 + stop_slippage_rate)

            if exit_px is not None:
                to_close.append((sym, pos, exit_px, t_ms + rs.DAY_MS, reason))
            else:
                if pos.side == 1:
                    best = max(pos.best_price, bar.high)
                    open_r = (best - pos.entry_price) / pos.initial_risk_dist
                else:
                    best = min(pos.best_price, bar.low)
                    open_r = (pos.entry_price - best) / pos.initial_risk_dist

                atr_now = tf.atr14[bar_idx]
                if trail_style == "clamped_dyn" and atr_now is not None and atr_now > 0:
                    dist = min(pos.trail_dist_entry, trail_mult * atr_now)
                elif trail_style == "ratchet_static":
                    scale = 1.0
                    if open_r >= 4.0:
                        scale = 0.60
                    elif open_r >= 2.5:
                        scale = 0.80
                    dist = pos.trail_dist_entry * scale
                else:
                    dist = pos.trail_dist_entry

                pos.best_price = best
                if pos.side == 1:
                    pos.stop_price = max(pos.stop_price, best - dist)
                else:
                    pos.stop_price = min(pos.stop_price, best + dist)

        for sym, pos, exit_px, exit_t, reason in to_close:
            gross = (exit_px - pos.entry_price) * pos.qty * pos.side
            fees = (pos.entry_price + exit_px) * pos.qty * fee_rate
            net = gross - fees + pos.funding_accrued
            equity_realized += net
            closed_trades.append(rv2.ClosedTrade(
                symbol=sym,
                side=pos.side,
                entry_time=pos.entry_time,
                exit_time=exit_t,
                entry_price=pos.entry_price,
                exit_price=exit_px,
                qty=pos.qty,
                net_pnl=net,
                gross_pnl=gross,
                fees=fees,
                funding=pos.funding_accrued,
                reason=reason,
            ))
            del open_positions[sym]

        unrealized = 0.0
        for sym, pos in open_positions.items():
            tf = feats_1d[sym]
            bar_idx = tf.time_to_idx.get(t_ms)
            if bar_idx is not None:
                unrealized += (tf.bars[bar_idx].close - pos.entry_price) * pos.qty * pos.side + pos.funding_accrued
        equity_curve.append((t_ms + rs.DAY_MS, equity_realized + unrealized, len(open_positions)))

        if step_idx + 1 < len(all_times):
            cands = []
            for sym, tf in feats_1d.items():
                if sym in open_positions:
                    continue
                bar_idx = tf.time_to_idx.get(t_ms)
                if bar_idx is None or bar_idx < 1:
                    continue
                side, score = _eval_rank_mode(tf, bar_idx, rank_mode)
                if side == 0 or not rv2._funding_ok(tf, bar_idx, side, funding_mode):
                    continue
                pkey = (0, tf.sym_idx) if rank_mode == "tuple" else (-score, tf.sym_idx)
                cands.append((pkey, sym, tf.sym_idx, side, tf.atr14[bar_idx]))
            cands.sort(key=lambda x: x[0])
            pending = cands

    if open_positions and all_times:
        last_t = all_times[-1]
        for sym, pos in list(open_positions.items()):
            tf = feats_1d[sym]
            bar_idx = tf.time_to_idx.get(last_t)
            if bar_idx is None:
                continue
            exit_px = tf.bars[bar_idx].close * (1.0 - slippage_rate * pos.side)
            gross = (exit_px - pos.entry_price) * pos.qty * pos.side
            fees = (pos.entry_price + exit_px) * pos.qty * fee_rate
            net = gross - fees + pos.funding_accrued
            equity_realized += net
            closed_trades.append(rv2.ClosedTrade(
                symbol=sym,
                side=pos.side,
                entry_time=pos.entry_time,
                exit_time=last_t + rs.DAY_MS,
                entry_price=pos.entry_price,
                exit_price=exit_px,
                qty=pos.qty,
                net_pnl=net,
                gross_pnl=gross,
                fees=fees,
                funding=pos.funding_accrued,
                reason="END",
            ))
            del open_positions[sym]
        if equity_curve:
            equity_curve[-1] = (equity_curve[-1][0], equity_realized, 0)

    return closed_trades, equity_curve


def _max_underwater_days(equity_curve):
    peak = None
    peak_t = None
    max_uw_days = 0
    for t_end, eq, _ in equity_curve:
        if peak is None or eq >= peak:
            peak = eq
            peak_t = t_end
        elif peak_t is not None:
            days = (t_end - peak_t) / rs.DAY_MS
            if days > max_uw_days:
                max_uw_days = days
    return round(max_uw_days, 1)


def _worst_month_pct(equity_curve, initial_equity):
    monthly = {}
    for t_end, eq, _ in equity_curve:
        dt = datetime.fromtimestamp((t_end - 1) / 1000, tz=timezone.utc)
        monthly[(dt.year, dt.month)] = eq
    prev = initial_equity
    worst = 0.0
    for k in sorted(monthly):
        eq = monthly[k]
        ret = (eq - prev) / prev * 100.0 if prev > 0 else 0.0
        if ret < worst:
            worst = ret
        prev = eq
    return round(worst, 2)


def main():
    now_ms = int(time.time() * 1000)
    end_ms_1d = (now_ms // rs.DAY_MS) * rs.DAY_MS
    end_ms_6h = (now_ms // rv2.SIX_HOUR_MS) * rv2.SIX_HOUR_MS

    raw_data = {}
    for sym in rv2.UNIVERSE_SYMBOLS:
        raw_data[sym] = rv2.load_or_download_symbol(sym, end_ms_1d, end_ms_6h, refresh=False)

    btc_1d_bars = raw_data["BTCUSDT"][0]
    btc_1d_ret = rv2.pct_returns([b.close for b in btc_1d_bars], 1)
    btc_1d_map = {btc_1d_bars[i].open_time: btc_1d_ret[i] for i in range(len(btc_1d_bars))}
    feats_1d = {
        sym: rv2.build_tf_features(sym, idx, raw_data[sym][0], rs.DAY_MS, raw_data[sym][2], btc_1d_map, is_h6=False)
        for idx, sym in enumerate(rv2.UNIVERSE_SYMBOLS)
    }

    years_full = (end_ms_1d - rv2.EVAL_START_MS) / (365.0 * rs.DAY_MS)
    years_h2 = (end_ms_1d - rv2.SPLIT_OOS_MS) / (365.0 * rs.DAY_MS)

    experiments = [
        ("V1_tuple_3side", dict(rank_mode="tuple", max_same_side=3, funding_mode="v1_asym", trail_style="static", trail_mult=2.0)),
        ("A2_score_max2side", dict(rank_mode="composite", max_same_side=2, funding_mode="v1_asym", trail_style="static", trail_mult=2.0)),
        ("A2_rs_only_max2", dict(rank_mode="rs_only", max_same_side=2, funding_mode="v1_asym", trail_style="static", trail_mult=2.0)),
        ("A2_er_only_max2", dict(rank_mode="er_only", max_same_side=2, funding_mode="v1_asym", trail_style="static", trail_mult=2.0)),
        ("A2_sym_funding", dict(rank_mode="composite", max_same_side=2, funding_mode="sym_30bps", trail_style="static", trail_mult=2.0)),
        ("A2_sym_short075", dict(rank_mode="composite", max_same_side=2, funding_mode="sym_30bps", risk_short=0.0075, trail_style="static", trail_mult=2.0)),
        ("A2_sym_short060", dict(rank_mode="composite", max_same_side=2, funding_mode="sym_30bps", risk_short=0.0060, trail_style="static", trail_mult=2.0)),
        ("A2_v1f_short075", dict(rank_mode="composite", max_same_side=2, funding_mode="v1_asym", risk_short=0.0075, trail_style="static", trail_mult=2.0)),
        ("A2_trail_225", dict(rank_mode="composite", max_same_side=2, funding_mode="v1_asym", trail_style="static", trail_mult=2.25)),
        ("A2_trail_250", dict(rank_mode="composite", max_same_side=2, funding_mode="v1_asym", trail_style="static", trail_mult=2.50)),
        ("A2_clamped_dyn", dict(rank_mode="composite", max_same_side=2, funding_mode="v1_asym", trail_style="clamped_dyn", trail_mult=2.0)),
        ("A2_ratchet_static", dict(rank_mode="composite", max_same_side=2, funding_mode="v1_asym", trail_style="ratchet_static", trail_mult=2.0)),
        ("A2_maxopen4_2side", dict(rank_mode="composite", max_open=4, max_same_side=2, funding_mode="v1_asym", trail_style="static", trail_mult=2.0)),
    ]

    print("=== ABLATION NON-COMPOUNDED (BASE 1000 USDT, TAKER 5bps + 2bps SLIP, 2021-2026) ===")
    hdr = (
        f"{'Variant':<22} {'Trd':>4} {'WR%':>5} {'PF':>5} {'Ret%':>6} {'SR0':>5} {'Sort':>5} "
        f"{'MtMDD%':>6} {'Calm':>5} {'UWDays':>6} {'WMo%':>6} {'Long$':>7} {'Short$':>7} | {'SR21_23':>7} {'SR24_26':>7}"
    )
    print(hdr)
    print("-" * len(hdr))

    for label, kw in experiments:
        tr, eq = run_custom_backtest(feats_1d, rv2.EVAL_START_MS, end_ms_1d, name=label, compounding=False, **kw)
        mf = rv2.compute_metrics(tr, eq, 1000.0, years_full)
        tr1, eq1 = run_custom_backtest(feats_1d, rv2.EVAL_START_MS, rv2.SPLIT_OOS_MS, name=label, compounding=False, **kw)
        m1 = rv2.compute_metrics(tr1, eq1, 1000.0, 3.0)
        tr2, eq2 = run_custom_backtest(feats_1d, rv2.SPLIT_OOS_MS, end_ms_1d, name=label, compounding=False, **kw)
        m2 = rv2.compute_metrics(tr2, eq2, 1000.0, years_h2)
        uwd = _max_underwater_days(eq)
        wmo = _worst_month_pct(eq, 1000.0)
        print(
            f"{label:<22} {mf['trades']:>4d} {mf['win_rate_pct']:>5.1f} {mf['profit_factor']:>5.2f} {mf['total_ret_pct']:>6.1f} "
            f"{mf['sharpe_0']:>5.2f} {mf['sortino']:>5.2f} {mf['mtm_dd_pct']:>6.2f} {mf['calmar']:>5.2f} {uwd:>6.1f} {wmo:>6.2f} "
            f"{mf['long_pnl']:>7.1f} {mf['short_pnl']:>7.1f} | {m1['sharpe_0']:>7.2f} {m2['sharpe_0']:>7.2f}"
        )

    # Suite Compounded + Risk Scaling + Cost Stress + Maker Limit su top 3 candidati vs V1
    print("\n=== COMPOUNDED + STRESS TEST 1.5x/2.0x + MAKER LIMIT (2021-2026) ===")
    comp_cases = [
        ("V1_live_100pct", dict(compounding=True, rank_mode="tuple", max_same_side=3, funding_mode="v1_asym", trail_style="static", trail_mult=2.0)),
        ("A2_score_2side_100pct", dict(compounding=True, rank_mode="composite", max_same_side=2, funding_mode="v1_asym", trail_style="static", trail_mult=2.0)),
        ("A2_score_2side_125pct", dict(compounding=True, risk_long=0.0125, risk_short=0.0125, rank_mode="composite", max_same_side=2, funding_mode="v1_asym", trail_style="static", trail_mult=2.0)),
        ("A2_sym_s075_100pct", dict(compounding=True, risk_long=0.01, risk_short=0.0075, rank_mode="composite", max_same_side=2, funding_mode="sym_30bps", trail_style="static", trail_mult=2.0)),
        ("A2_sym_s075_125pct", dict(compounding=True, risk_long=0.0125, risk_short=0.009, rank_mode="composite", max_same_side=2, funding_mode="sym_30bps", trail_style="static", trail_mult=2.0)),
        ("A2_2side_MAKER", dict(compounding=True, rank_mode="composite", max_same_side=2, funding_mode="v1_asym", trail_style="static", trail_mult=2.0, fee_rate=0.0002, slippage_rate=0.00005)),
        ("A2_2side_STRESS_1.5x", dict(compounding=True, rank_mode="composite", max_same_side=2, funding_mode="v1_asym", trail_style="static", trail_mult=2.0, fee_rate=0.00075, slippage_rate=0.0003, stop_slippage_rate=0.0003)),
        ("A2_2side_STRESS_2.0x", dict(compounding=True, rank_mode="composite", max_same_side=2, funding_mode="v1_asym", trail_style="static", trail_mult=2.0, fee_rate=0.0010, slippage_rate=0.0004, stop_slippage_rate=0.0004)),
    ]
    print(hdr)
    print("-" * len(hdr))
    for label, kw in comp_cases:
        tr, eq = run_custom_backtest(feats_1d, rv2.EVAL_START_MS, end_ms_1d, name=label, **kw)
        mf = rv2.compute_metrics(tr, eq, 1000.0, years_full)
        tr1, eq1 = run_custom_backtest(feats_1d, rv2.EVAL_START_MS, rv2.SPLIT_OOS_MS, name=label, **kw)
        m1 = rv2.compute_metrics(tr1, eq1, 1000.0, 3.0)
        tr2, eq2 = run_custom_backtest(feats_1d, rv2.SPLIT_OOS_MS, end_ms_1d, name=label, **kw)
        m2 = rv2.compute_metrics(tr2, eq2, 1000.0, years_h2)
        uwd = _max_underwater_days(eq)
        wmo = _worst_month_pct(eq, 1000.0)
        print(
            f"{label:<22} {mf['trades']:>4d} {mf['win_rate_pct']:>5.1f} {mf['profit_factor']:>5.2f} {mf['total_ret_pct']:>6.1f} "
            f"{mf['sharpe_0']:>5.2f} {mf['sortino']:>5.2f} {mf['mtm_dd_pct']:>6.2f} {mf['calmar']:>5.2f} {uwd:>6.1f} {wmo:>6.2f} "
            f"{mf['long_pnl']:>7.1f} {mf['short_pnl']:>7.1f} | {m1['sharpe_0']:>7.2f} {m2['sharpe_0']:>7.2f}"
        )
        if label in ("V1_live_100pct", "A2_score_2side_100pct", "A2_sym_s075_100pct", "A2_score_2side_125pct"):
            print("  by_year:", json.dumps(mf["by_year"]))

    return 0


if __name__ == "__main__":
    sys.exit(main())
