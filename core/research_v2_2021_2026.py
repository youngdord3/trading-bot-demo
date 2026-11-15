"""Backtest esteso 2021-2026 e ricerca upgrade V2 (senza toccare il live).

Scarica e cacha localmente su `backups/research_cache/` le candele pubbliche
`1d` e `6h` + `fundingRate` da `fapi.binance.com` per `UNIVERSE_SYMBOLS`
(`2020-09-25` -> oggi UTC, evaluation `2021-01-01` -> oggi) ed esegue:
  1. Audit storico reale della V1 (`D1_TSMOM_ERF_TRAIL`) anno per anno
     (2021, 2022 bear, 2023 sideways, 2024, 2025, 2026 YTD) e split triennali
     IS/OOS (`2021-2023` vs `2024-2026`).
  2. Confronto discrepancy V1 backtest (ordine alfabetico) vs live attuale
     (ordine tupla `BTC, ETH, SOL` e cap nozionale `2000/5816` vs `2x`).
  3. Matrice di ablation e upgrade V2 (senza grid-search mensile):
     - Signal ranking cross-sectional (`ER`, vol-norm momentum) al posto del FIFO
     - Cap esposizione correlata / max stessa direzione + filtro correlazione BTC
     - Trailing stop ATR dinamico, profit-ratchet (+2R/+4R) e time-stop
     - Asimmetria Long/Short (solo long, short ridotto 50-60%, short severo)
     - Timeframe `H6` (6h, stile AdaptiveTrend a regole fisse) vs `D1`
     - Analisi esecuzione (`Taker 5bps + 2bps slip` vs `Maker 2bps + 0.5bps`)
       e stress test costi `1.5x` / `2.0x`.
"""

import bisect
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import sys
import time
from urllib.parse import urlencode

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import research_paradigms as rp
import research_signals as rs

SIX_HOUR_MS = 6 * rs.HOUR_MS
CACHE_DIR = Path(__file__).resolve().parent.parent / "backups" / "research_cache"

UNIVERSE_SYMBOLS = (
    "BTCUSDT",
    "ETHUSDT",
    "SOLUSDT",
    "BNBUSDT",
    "DOGEUSDT",
    "XRPUSDT",
    "ADAUSDT",
    "AVAXUSDT",
    "LINKUSDT",
    "DOTUSDT",
    "LTCUSDT",
    "BCHUSDT",
)

WARMUP_START_MS = int(datetime(2020, 9, 25, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
EVAL_START_MS = int(datetime(2021, 1, 1, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
SPLIT_OOS_MS = int(datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)


# ---------------------------------------------------------------------------
# Cache & Download Dati Pubblici Binance Futures
# ---------------------------------------------------------------------------


def _cache_file(kind, symbol):
    return CACHE_DIR / f"{symbol}_{kind}.json"


def _fetch_klines_interval(symbol, interval, span_ms, start_ms, end_ms):
    bars = []
    cursor = start_ms
    while cursor < end_ms:
        query = urlencode({
            "symbol": symbol,
            "interval": interval,
            "startTime": cursor,
            "endTime": end_ms - 1,
            "limit": rs.KLINES_LIMIT,
        })
        payload = rs._get_json(rs.KLINES_PATH + "?" + query)
        if not isinstance(payload, list) or not payload:
            break
        for row in payload:
            bars.append([
                int(row[0]),
                float(row[1]),
                float(row[2]),
                float(row[3]),
                float(row[4]),
                float(row[5]),
            ])
        next_cursor = int(payload[-1][0]) + span_ms
        if next_cursor <= cursor or len(payload) < rs.KLINES_LIMIT:
            break
        cursor = next_cursor
        time.sleep(0.08)
    return bars


def _fetch_funding_all(symbol, start_ms, end_ms):
    points = []
    cursor = start_ms
    while cursor < end_ms:
        query = urlencode({
            "symbol": symbol,
            "startTime": cursor,
            "endTime": end_ms - 1,
            "limit": rp.FUNDING_LIMIT,
        })
        payload = rs._get_json(rp.FUNDING_PATH + "?" + query)
        if not isinstance(payload, list) or not payload:
            break
        for row in payload:
            ftime = (int(row["fundingTime"]) // rs.HOUR_MS) * rs.HOUR_MS
            points.append([ftime, float(row["fundingRate"])])
        next_cursor = int(payload[-1]["fundingTime"]) + 1000
        if next_cursor <= cursor or len(payload) < rp.FUNDING_LIMIT:
            break
        cursor = next_cursor
        time.sleep(0.08)
    return points


def load_or_download_symbol(symbol, end_ms_1d, end_ms_6h, refresh=False):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    p_1d = _cache_file("1d", symbol)
    p_6h = _cache_file("6h", symbol)
    p_fr = _cache_file("funding", symbol)

    if not refresh and p_1d.exists() and p_6h.exists() and p_fr.exists():
        raw_1d = json.loads(p_1d.read_text(encoding="utf-8"))
        raw_6h = json.loads(p_6h.read_text(encoding="utf-8"))
        raw_fr = json.loads(p_fr.read_text(encoding="utf-8"))
    else:
        raw_1d = _fetch_klines_interval(symbol, "1d", rs.DAY_MS, WARMUP_START_MS, end_ms_1d)
        raw_6h = _fetch_klines_interval(symbol, "6h", SIX_HOUR_MS, WARMUP_START_MS, end_ms_6h)
        raw_fr = _fetch_funding_all(symbol, WARMUP_START_MS, end_ms_6h)
        p_1d.write_text(json.dumps(raw_1d), encoding="utf-8")
        p_6h.write_text(json.dumps(raw_6h), encoding="utf-8")
        p_fr.write_text(json.dumps(raw_fr), encoding="utf-8")

    bars_1d = [rs.Bar(*row) for row in raw_1d if row[0] < end_ms_1d]
    bars_6h = [rs.Bar(*row) for row in raw_6h if row[0] < end_ms_6h]
    funding = [rp.FundingPoint(int(t), float(r)) for t, r in raw_fr if t <= end_ms_6h]
    return bars_1d, bars_6h, funding


# ---------------------------------------------------------------------------
# Feature Engineering Causale (D1 e H6)
# ---------------------------------------------------------------------------


def efficiency_ratio(closes, period):
    out = [None] * len(closes)
    if period <= 0 or len(closes) <= period:
        return out
    for i in range(period, len(closes)):
        net = abs(closes[i] - closes[i - period])
        path = sum(abs(closes[k] - closes[k - 1]) for k in range(i - period + 1, i + 1))
        out[i] = (net / path) if path > 0 else 0.0
    return out


def pct_returns(closes, lookback):
    out = [None] * len(closes)
    for i in range(lookback, len(closes)):
        prev = closes[i - lookback]
        if prev > 0:
            out[i] = (closes[i] - prev) / prev
    return out


def rolling_btc_corr(closes, btc_returns_by_time, times, window=30):
    """Correlazione rolling causale dei ritorni a 1 barra vs BTC."""
    n = len(closes)
    out = [None] * n
    sym_ret = pct_returns(closes, 1)
    for i in range(window, n):
        xs = []
        ys = []
        for k in range(i - window + 1, i + 1):
            r_s = sym_ret[k]
            r_b = btc_returns_by_time.get(times[k])
            if r_s is not None and r_b is not None:
                xs.append(r_s)
                ys.append(r_b)
        if len(xs) < max(15, window // 2):
            continue
        mx = sum(xs) / len(xs)
        my = sum(ys) / len(ys)
        cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
        vx = sum((x - mx) ** 2 for x in xs)
        vy = sum((y - my) ** 2 for y in ys)
        denom = math.sqrt(vx * vy)
        out[i] = (cov / denom) if denom > 0 else 0.0
    return out


@dataclass
class TimeframeFeatures:
    symbol: str
    sym_idx: int
    bars: list
    times: list
    time_to_idx: dict
    ema_fast: list
    ema_slow: list
    atr14: list
    adx14: list
    er: list
    ret_short: list
    ret_med: list
    ret_long: list
    dc_high_10: list
    dc_low_10: list
    dc_high_15: list
    dc_low_15: list
    dc_high_20: list
    dc_low_20: list
    funding_last: list
    funding_sum3: list
    funding_map: dict
    btc_corr: list


def build_tf_features(symbol, sym_idx, bars, span_ms, funding_points, btc_ret_map, is_h6=False):
    closes = [b.close for b in bars]
    highs = [b.high for b in bars]
    lows = [b.low for b in bars]
    times = [b.open_time for b in bars]

    if is_h6:
        # Su H6: 4 barre = 1g. Usiamo finestre equivalenti a ~10-20d e ~50d su H6 o native H6
        ema_fast = rs.ema(closes, 40)     # 10 giorni
        ema_slow = rs.ema(closes, 120)    # 30 giorni
        er_vals = efficiency_ratio(closes, 20)   # 5 giorni
        ret_s = pct_returns(closes, 28)   # 7 giorni
        ret_m = pct_returns(closes, 60)   # 15 giorni
        ret_l = pct_returns(closes, 120)  # 30 giorni
        dc10_h, dc10_l = rs.donchian_bounds(highs, lows, 20)  # 5g breakout
        dc15_h, dc15_l = rs.donchian_bounds(highs, lows, 40)  # 10g breakout
        dc20_h, dc20_l = rs.donchian_bounds(highs, lows, 60)  # 15g breakout
        corr_win = 120  # 30 giorni in barre 6h
    else:
        ema_fast = rs.ema(closes, 20)
        ema_slow = rs.ema(closes, 50)
        er_vals = efficiency_ratio(closes, 10)
        ret_s = pct_returns(closes, 15)
        ret_m = pct_returns(closes, 20)
        ret_l = pct_returns(closes, 30)
        dc10_h, dc10_l = rs.donchian_bounds(highs, lows, 10)
        dc15_h, dc15_l = rs.donchian_bounds(highs, lows, 15)
        dc20_h, dc20_l = rs.donchian_bounds(highs, lows, 20)
        corr_win = 30

    f_last, f_sum3 = rp.align_funding_series(times, span_ms, funding_points)
    f_map = {p.funding_time: p.rate for p in funding_points}
    corr_vals = rolling_btc_corr(closes, btc_ret_map, times, window=corr_win)

    return TimeframeFeatures(
        symbol=symbol,
        sym_idx=sym_idx,
        bars=bars,
        times=times,
        time_to_idx={t: idx for idx, t in enumerate(times)},
        ema_fast=ema_fast,
        ema_slow=ema_slow,
        atr14=rs.atr(highs, lows, closes, 14),
        adx14=rs.adx(highs, lows, closes, 14),
        er=er_vals,
        ret_short=ret_s,
        ret_med=ret_m,
        ret_long=ret_l,
        dc_high_10=dc10_h,
        dc_low_10=dc10_l,
        dc_high_15=dc15_h,
        dc_low_15=dc15_l,
        dc_high_20=dc20_h,
        dc_low_20=dc20_l,
        funding_last=f_last,
        funding_sum3=f_sum3,
        funding_map=f_map,
        btc_corr=corr_vals,
    )


# ---------------------------------------------------------------------------
# Strategia & Configurazione Portafoglio Realistico
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StrategyConfig:
    name: str
    timeframe: str = "1d"               # "1d" o "6h"
    initial_equity: float = 1000.0
    compounding: bool = True            # True = risk % su equity dinamica; False = fisso base 1000
    risk_fraction_long: float = 0.01
    risk_fraction_short: float = 0.01
    max_notional_mult: float = 2.0      # Cap nozionale per posizione in multipli di equity
    max_open: int = 3
    max_same_side: int = 3              # Es. 2 = max 2 long o 2 short simultanei
    max_btc_corr_same_side: float = 1.0 # <1.0 limita posizioni correlate con stessa direzione
    queue_mode: str = "tuple"           # "tuple", "alpha", "score", "rs_leader"
    signal_mode: str = "v1_tsmom"       # "v1_tsmom", "asym_short", "long_only", "rs_leader", "h6_tsmom"
    funding_mode: str = "v1_asym"       # "v1_asym", "sym_30bps"
    sl_mult: float = 1.5
    trail_mode: str = "static"          # "static", "dynamic_atr", "profit_ratchet"
    trail_mult: float = 2.0
    time_stop_bars: int = 0             # >0 chiude trade fermi (< min_r) dopo N barre
    time_stop_min_r: float = 0.25
    fee_rate: float = 0.0005            # Taker 5 bps standard Binance Futures VIP 0
    slippage_rate: float = 0.0002       # Slippage ingresso/stop
    stop_slippage_rate: float = 0.0002  # Slippage esplicito su STOP_MARKET
    adx_min: float = 18.0
    er_min: float = 0.30
    dc_period: int = 10                 # 10, 15, o 20 (su H6 mappato su 20/40/60)
    short_ret15_min: float = 0.03
    short_ret30_min: float = 0.05


@dataclass
class OpenPos:
    symbol: str
    sym_idx: int
    side: int
    entry_time: int
    entry_price: float
    qty: float
    stop_price: float
    initial_risk_dist: float
    trail_dist_entry: float
    best_price: float
    bars_held: int = 0
    funding_accrued: float = 0.0


@dataclass
class ClosedTrade:
    symbol: str
    side: int
    entry_time: int
    exit_time: int
    entry_price: float
    exit_price: float
    qty: float
    net_pnl: float
    gross_pnl: float
    fees: float
    funding: float
    reason: str


def _funding_ok(tf: TimeframeFeatures, idx: int, side: int, funding_mode: str) -> bool:
    fr = tf.funding_last[idx]
    fsum = tf.funding_sum3[idx]
    if funding_mode == "sym_30bps":
        if side == 1 and (fr >= 0.00030 or fsum >= 0.00075):
            return False
        if side == -1 and (fr <= -0.00030 or fsum <= -0.00075):
            return False
        return True
    # V1 esatto in core/strategy.py
    if side == 1 and (fr >= 0.00030 or fsum >= 0.00075):
        return False
    if side == -1 and (fr <= -0.00010 or fsum <= -0.00025):
        return False
    return True


def _donchian_bounds_for(tf: TimeframeFeatures, dc_period: int):
    if dc_period == 10:
        return tf.dc_high_10, tf.dc_low_10
    if dc_period == 15:
        return tf.dc_high_15, tf.dc_low_15
    return tf.dc_high_20, tf.dc_low_20


def evaluate_signal(tf: TimeframeFeatures, idx: int, cfg: StrategyConfig, rs_leaders=None, rs_laggards=None):
    """Ritorna (side, score) causali alla chiusura della barra `idx`."""
    if idx < 1:
        return 0, 0.0
    atr_val = tf.atr14[idx]
    adx_val = tf.adx14[idx]
    er_val = tf.er[idx]
    ema_f = tf.ema_fast[idx]
    ema_s = tf.ema_slow[idx]
    r_s = tf.ret_short[idx]
    r_m = tf.ret_med[idx]
    r_l = tf.ret_long[idx]
    dch, dcl = _donchian_bounds_for(tf, cfg.dc_period)
    upper = dch[idx]
    lower = dcl[idx]
    if any(v is None for v in (atr_val, adx_val, er_val, ema_f, ema_s, r_s, r_m, r_l, upper, lower)):
        return 0, 0.0
    if atr_val <= 0:
        return 0, 0.0
    close = tf.bars[idx].close
    if close <= 0:
        return 0, 0.0

    # Score continuo di qualita' trend normalizzata su volatilita'
    vol_pct = atr_val / close
    quality_long = er_val * max(0.0, r_m) / max(vol_pct, 1e-6)
    quality_short = er_val * max(0.0, -r_m) / max(vol_pct, 1e-6)

    if cfg.signal_mode == "rs_leader":
        if adx_val < cfg.adx_min or er_val < 0.25:
            return 0, 0.0
        t_ms = tf.times[idx]
        if (
            t_ms in rs_leaders
            and tf.symbol in rs_leaders[t_ms]
            and close > ema_f >= ema_s
            and close > upper
            and (close - upper) / atr_val <= 1.8
            and _funding_ok(tf, idx, 1, cfg.funding_mode)
        ):
            return 1, quality_long
        if (
            t_ms in rs_laggards
            and tf.symbol in rs_laggards[t_ms]
            and close < ema_f <= ema_s
            and close < lower
            and (lower - close) / atr_val <= 1.8
            and _funding_ok(tf, idx, -1, cfg.funding_mode)
        ):
            return -1, quality_short
        return 0, 0.0

    # Gate TSMOM+ERF (V1 o H6)
    if adx_val < cfg.adx_min or er_val < cfg.er_min:
        return 0, 0.0

    long_ok = (
        r_s > 0.03
        and r_l > 0.05
        and close > ema_f >= ema_s
        and close > upper
        and (close - upper) / atr_val <= 1.8
        and _funding_ok(tf, idx, 1, cfg.funding_mode)
    )
    if long_ok:
        return 1, quality_long

    if cfg.signal_mode == "long_only":
        return 0, 0.0

    short_ok = (
        r_s < -cfg.short_ret15_min
        and r_l < -cfg.short_ret30_min
        and close < ema_f <= ema_s
        and close < lower
        and (lower - close) / atr_val <= 1.8
        and _funding_ok(tf, idx, -1, cfg.funding_mode)
    )
    if short_ok:
        return -1, quality_short
    return 0, 0.0


def build_rs_sets(features_by_symbol, top_k=4):
    time_scores = {}
    for sym, tf in features_by_symbol.items():
        for i, t_ms in enumerate(tf.times):
            r20 = tf.ret_med[i]
            atr_val = tf.atr14[i]
            close = tf.bars[i].close
            if r20 is None or atr_val is None or atr_val <= 0 or close <= 0:
                continue
            score = r20 / (atr_val / close)
            time_scores.setdefault(t_ms, []).append((score, r20, sym))
    leaders = {}
    laggards = {}
    for t_ms, entries in time_scores.items():
        if len(entries) < 2:
            continue
        ordered = sorted(entries)
        k = min(top_k, max(1, len(ordered) // 2))
        laggards[t_ms] = {sym for _sc, r20, sym in ordered[:k] if r20 < 0}
        leaders[t_ms] = {sym for _sc, r20, sym in ordered[-k:] if r20 > 0}
    return leaders, laggards


# ---------------------------------------------------------------------------
# Simulatore Portafoglio Bar-by-Bar con Mark-to-Market
# ---------------------------------------------------------------------------


def _next_trail_stop(pos: OpenPos, bar: rs.Bar, current_atr: float, cfg: StrategyConfig) -> OpenPos:
    if pos.side == 1:
        best = max(pos.best_price, bar.high)
        open_r = (best - pos.entry_price) / pos.initial_risk_dist if pos.initial_risk_dist > 0 else 0.0
    else:
        best = min(pos.best_price, bar.low)
        open_r = (pos.entry_price - best) / pos.initial_risk_dist if pos.initial_risk_dist > 0 else 0.0

    if cfg.trail_mode == "static":
        dist = pos.trail_dist_entry
    elif cfg.trail_mode == "dynamic_atr":
        atr_now = current_atr if (current_atr is not None and current_atr > 0) else pos.trail_dist_entry / cfg.trail_mult
        dist = cfg.trail_mult * atr_now
    else:  # profit_ratchet
        atr_now = current_atr if (current_atr is not None and current_atr > 0) else pos.trail_dist_entry / cfg.trail_mult
        mult = cfg.trail_mult
        if open_r >= 4.0:
            mult = max(1.0, cfg.trail_mult * 0.55)
        elif open_r >= 2.5:
            mult = max(1.25, cfg.trail_mult * 0.75)
        dist = mult * atr_now

    if pos.side == 1:
        new_stop = max(pos.stop_price, best - dist)
    else:
        new_stop = min(pos.stop_price, best + dist)
    pos.best_price = best
    pos.stop_price = new_stop
    return pos


def run_portfolio_backtest(
    features_by_symbol: dict,
    cfg: StrategyConfig,
    start_ms: int,
    end_ms: int,
):
    span_ms = SIX_HOUR_MS if cfg.timeframe == "6h" else rs.DAY_MS
    all_times = sorted({
        t
        for tf in features_by_symbol.values()
        for t in tf.times
        if start_ms <= t < end_ms
    })
    rs_leaders, rs_laggards = build_rs_sets(features_by_symbol) if cfg.signal_mode == "rs_leader" else ({}, {})

    equity_realized = cfg.initial_equity
    open_positions = {}  # symbol -> OpenPos
    closed_trades = []
    equity_curve = []    # (time_ms, mtm_equity, open_count)
    pending_entries = [] # (priority_key, symbol, sym_idx, side, atr_val)

    for step_idx, t_ms in enumerate(all_times):
        # 1. Apri eventuali entrate generate alla chiusura barra precedente sull'open della barra `t_ms`
        if pending_entries:
            base_eq = equity_realized if cfg.compounding else cfg.initial_equity
            for _pkey, sym, sym_idx, side, atr_sig in pending_entries:
                if len(open_positions) >= cfg.max_open:
                    break
                if sym in open_positions:
                    continue
                same_side_open = [p for p in open_positions.values() if p.side == side]
                if len(same_side_open) >= cfg.max_same_side:
                    continue
                tf = features_by_symbol[sym]
                bar_idx = tf.time_to_idx.get(t_ms)
                if bar_idx is None:
                    continue
                if cfg.max_btc_corr_same_side < 1.0 and same_side_open:
                    c_val = tf.btc_corr[bar_idx - 1] if bar_idx > 0 else None
                    if c_val is not None and c_val >= cfg.max_btc_corr_same_side and len(same_side_open) >= 2:
                        continue
                bar = tf.bars[bar_idx]
                entry_price = bar.open * (1.0 + cfg.slippage_rate * side)
                stop_dist = cfg.sl_mult * atr_sig
                if stop_dist <= 0 or entry_price <= 0:
                    continue
                rf = cfg.risk_fraction_long if side == 1 else cfg.risk_fraction_short
                risk_usdt = max(0.0, base_eq) * rf
                max_notional = max(0.0, base_eq) * cfg.max_notional_mult
                qty = min(risk_usdt / stop_dist, max_notional / entry_price)
                if qty <= 0:
                    continue
                open_positions[sym] = OpenPos(
                    symbol=sym,
                    sym_idx=sym_idx,
                    side=side,
                    entry_time=t_ms,
                    entry_price=entry_price,
                    qty=qty,
                    stop_price=entry_price - side * stop_dist,
                    initial_risk_dist=stop_dist,
                    trail_dist_entry=cfg.trail_mult * atr_sig,
                    best_price=entry_price,
                )
            pending_entries = []

        # 2. Funding + Exit / Stop Check + Trailing Update sulla barra corrente `t_ms`
        to_close = []
        for sym, pos in open_positions.items():
            tf = features_by_symbol[sym]
            bar_idx = tf.time_to_idx.get(t_ms)
            if bar_idx is None:
                continue
            bar = tf.bars[bar_idx]
            pos.bars_held += 1
            pos.funding_accrued += rp.funding_pnl_for_bar(
                pos, bar.open_time, span_ms, tf.funding_map, bar.close
            )

            exit_px = None
            reason = None
            if pos.side == 1 and bar.low <= pos.stop_price:
                reason = "TRAIL" if pos.stop_price > pos.entry_price else "SL"
                exit_px = min(pos.stop_price, bar.open) * (1.0 - cfg.stop_slippage_rate)
            elif pos.side == -1 and bar.high >= pos.stop_price:
                reason = "TRAIL" if pos.stop_price < pos.entry_price else "SL"
                exit_px = max(pos.stop_price, bar.open) * (1.0 + cfg.stop_slippage_rate)
            elif cfg.time_stop_bars > 0 and pos.bars_held >= cfg.time_stop_bars:
                curr_r = ((bar.close - pos.entry_price) * pos.side) / pos.initial_risk_dist
                if curr_r < cfg.time_stop_min_r:
                    reason = "TIME_STOP"
                    exit_px = bar.close * (1.0 - cfg.slippage_rate * pos.side)

            if exit_px is not None:
                to_close.append((sym, pos, exit_px, t_ms + span_ms, reason))
            else:
                _next_trail_stop(pos, bar, tf.atr14[bar_idx], cfg)

        for sym, pos, exit_px, exit_t, reason in to_close:
            gross = (exit_px - pos.entry_price) * pos.qty * pos.side
            fees = (pos.entry_price + exit_px) * pos.qty * cfg.fee_rate
            net = gross - fees + pos.funding_accrued
            equity_realized += net
            closed_trades.append(ClosedTrade(
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

        # 3. Calcola Mark-to-Market Equity alla chiusura barra `t_ms`
        unrealized = 0.0
        for sym, pos in open_positions.items():
            tf = features_by_symbol[sym]
            bar_idx = tf.time_to_idx.get(t_ms)
            if bar_idx is not None:
                px = tf.bars[bar_idx].close
                unrealized += (px - pos.entry_price) * pos.qty * pos.side + pos.funding_accrued
        mtm_eq = equity_realized + unrealized
        equity_curve.append((t_ms + span_ms, mtm_eq, len(open_positions)))

        # 4. Genera segnali per la prossima apertura se non e' l'ultima barra
        if step_idx + 1 < len(all_times):
            candidates = []
            for sym, tf in features_by_symbol.items():
                if sym in open_positions:
                    continue
                bar_idx = tf.time_to_idx.get(t_ms)
                if bar_idx is None:
                    continue
                side, score = evaluate_signal(tf, bar_idx, cfg, rs_leaders, rs_laggards)
                if side == 0:
                    continue
                if cfg.queue_mode == "alpha":
                    pkey = (0, sym)
                elif cfg.queue_mode == "tuple":
                    pkey = (0, tf.sym_idx)
                else:  # "score" o "rs_leader"
                    pkey = (-score, tf.sym_idx)
                candidates.append((pkey, sym, tf.sym_idx, side, tf.atr14[bar_idx]))
            candidates.sort(key=lambda item: item[0])
            pending_entries = candidates

    # Chiusura di fine finestra sui mark finali
    if open_positions and all_times:
        last_t = all_times[-1]
        for sym, pos in list(open_positions.items()):
            tf = features_by_symbol[sym]
            bar_idx = tf.time_to_idx.get(last_t)
            if bar_idx is None:
                continue
            last_bar = tf.bars[bar_idx]
            exit_px = last_bar.close * (1.0 - cfg.slippage_rate * pos.side)
            gross = (exit_px - pos.entry_price) * pos.qty * pos.side
            fees = (pos.entry_price + exit_px) * pos.qty * cfg.fee_rate
            net = gross - fees + pos.funding_accrued
            equity_realized += net
            closed_trades.append(ClosedTrade(
                symbol=sym,
                side=pos.side,
                entry_time=pos.entry_time,
                exit_time=last_t + span_ms,
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


# ---------------------------------------------------------------------------
# Metriche Quantitative Complete (Sharpe, Sortino, Calmar, MtM DD, Anno x Anno)
# ---------------------------------------------------------------------------


def compute_metrics(trades: list, equity_curve: list, initial_equity: float, years_span: float):
    wins = [t.net_pnl for t in trades if t.net_pnl > 0]
    losses = [-t.net_pnl for t in trades if t.net_pnl <= 0]
    gp = sum(wins)
    gl = sum(losses)
    pf = (gp / gl) if gl > 0 else (float("inf") if gp > 0 else 0.0)
    wr = (len(wins) / len(trades)) if trades else 0.0
    payoff = ((gp / len(wins)) / (gl / len(losses))) if (wins and losses) else 0.0

    # Daily equity resampling per Sharpe/Sortino onesti (365 gg/anno)
    daily_close_eq = {}
    for t_end, eq, _open_cnt in equity_curve:
        day_key = (t_end - 1) // rs.DAY_MS
        daily_close_eq[day_key] = eq
    daily_vals = [daily_close_eq[k] for k in sorted(daily_close_eq)]

    peak = initial_equity
    mtm_dd_pct = 0.0
    for _t, eq, _oc in equity_curve:
        if eq > peak:
            peak = eq
        if peak > 0:
            dd = (peak - eq) / peak * 100.0
            if dd > mtm_dd_pct:
                mtm_dd_pct = dd

    daily_rets = []
    prev_eq = initial_equity
    for eq in daily_vals:
        daily_rets.append(((eq - prev_eq) / prev_eq) if prev_eq > 0 else 0.0)
        prev_eq = eq

    n_days = len(daily_rets)
    if n_days > 1:
        mean_r = sum(daily_rets) / n_days
        var_r = sum((r - mean_r) ** 2 for r in daily_rets) / (n_days - 1)
        std_r = math.sqrt(var_r)
        ann_vol_pct = std_r * math.sqrt(365.0) * 100.0
        sharpe_0 = (mean_r / std_r * math.sqrt(365.0)) if std_r > 0 else 0.0
        rf_daily = 0.045 / 365.0
        sharpe_rf = ((mean_r - rf_daily) / std_r * math.sqrt(365.0)) if std_r > 0 else 0.0
        downside = [min(0.0, r) ** 2 for r in daily_rets]
        down_std = math.sqrt(sum(downside) / n_days)
        sortino = (mean_r / down_std * math.sqrt(365.0)) if down_std > 0 else 0.0
    else:
        ann_vol_pct = sharpe_0 = sharpe_rf = sortino = 0.0

    final_eq = equity_curve[-1][1] if equity_curve else initial_equity
    total_ret_pct = (final_eq - initial_equity) / initial_equity * 100.0
    if years_span > 0 and final_eq > 0:
        cagr_pct = ((final_eq / initial_equity) ** (1.0 / years_span) - 1.0) * 100.0
    else:
        cagr_pct = 0.0
    calmar = (cagr_pct / mtm_dd_pct) if mtm_dd_pct > 0 else 0.0

    long_trades = [t for t in trades if t.side == 1]
    short_trades = [t for t in trades if t.side == -1]
    by_year = {}
    for t in trades:
        yr = datetime.fromtimestamp(t.exit_time / 1000, tz=timezone.utc).year
        slot = by_year.setdefault(yr, {"trades": 0, "net_pnl": 0.0, "wins": 0})
        slot["trades"] += 1
        slot["net_pnl"] += t.net_pnl
        if t.net_pnl > 0:
            slot["wins"] += 1

    return {
        "trades": len(trades),
        "win_rate_pct": round(wr * 100.0, 2),
        "profit_factor": round(pf, 3),
        "payoff": round(payoff, 2),
        "total_ret_pct": round(total_ret_pct, 2),
        "cagr_pct": round(cagr_pct, 2),
        "ann_vol_pct": round(ann_vol_pct, 2),
        "sharpe_0": round(sharpe_0, 2),
        "sharpe_rf45": round(sharpe_rf, 2),
        "sortino": round(sortino, 2),
        "mtm_dd_pct": round(mtm_dd_pct, 2),
        "calmar": round(calmar, 2),
        "fees_usdt": round(sum(t.fees for t in trades), 2),
        "funding_usdt": round(sum(t.funding for t in trades), 2),
        "long_trades": len(long_trades),
        "long_pnl": round(sum(t.net_pnl for t in long_trades), 2),
        "short_trades": len(short_trades),
        "short_pnl": round(sum(t.net_pnl for t in short_trades), 2),
        "by_year": {
            str(y): {
                "trades": v["trades"],
                "wr_pct": round(v["wins"] / v["trades"] * 100.0, 1) if v["trades"] else 0.0,
                "net_pnl": round(v["net_pnl"], 2),
            }
            for y, v in sorted(by_year.items())
        },
    }


def _year_slices(end_ms):
    slices = []
    for y in (2021, 2022, 2023, 2024, 2025):
        s = int(datetime(y, 1, 1, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
        e = int(datetime(y + 1, 1, 1, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
        slices.append((str(y), s, min(e, end_ms)))
    s26 = int(datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
    if end_ms > s26:
        slices.append(("2026YTD", s26, end_ms))
    return slices


def evaluate_suite(configs, feats_1d, feats_6h, end_ms_1d, end_ms_6h):
    results = []
    for cfg in configs:
        feats = feats_6h if cfg.timeframe == "6h" else feats_1d
        end_ms = end_ms_6h if cfg.timeframe == "6h" else end_ms_1d
        years_full = (end_ms - EVAL_START_MS) / (365.0 * rs.DAY_MS)

        tr_full, eq_full = run_portfolio_backtest(feats, cfg, EVAL_START_MS, end_ms)
        m_full = compute_metrics(tr_full, eq_full, cfg.initial_equity, years_full)

        tr_h1, eq_h1 = run_portfolio_backtest(feats, cfg, EVAL_START_MS, SPLIT_OOS_MS)
        m_h1 = compute_metrics(tr_h1, eq_h1, cfg.initial_equity, 3.0)

        years_h2 = (end_ms - SPLIT_OOS_MS) / (365.0 * rs.DAY_MS)
        tr_h2, eq_h2 = run_portfolio_backtest(feats, cfg, SPLIT_OOS_MS, end_ms)
        m_h2 = compute_metrics(tr_h2, eq_h2, cfg.initial_equity, years_h2)

        results.append({
            "name": cfg.name,
            "timeframe": cfg.timeframe,
            "compounding": cfg.compounding,
            "full_2021_2026": m_full,
            "triennio_2021_2023": m_h1,
            "triennio_2024_2026": m_h2,
        })
    return results


def print_summary_table(title, rows):
    print("\n=== " + title + " ===")
    hdr = (
        f"{'Strategy':<28} {'TF':>3} {'Trd':>4} {'WR%':>5} {'PF':>5} "
        f"{'Ret%':>7} {'CAGR%':>6} {'SR0':>5} {'Sort':>5} {'MtMDD%':>6} {'Calmar':>6} "
        f"{'Long$':>8} {'Short$':>8} | {'SR_21_23':>8} {'SR_24_26':>8}"
    )
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        f = r["full_2021_2026"]
        h1 = r["triennio_2021_2023"]
        h2 = r["triennio_2024_2026"]
        print(
            f"{r['name']:<28} {r['timeframe']:>3} {f['trades']:>4d} {f['win_rate_pct']:>5.1f} {f['profit_factor']:>5.2f} "
            f"{f['total_ret_pct']:>7.1f} {f['cagr_pct']:>6.1f} {f['sharpe_0']:>5.2f} {f['sortino']:>5.2f} "
            f"{f['mtm_dd_pct']:>6.2f} {f['calmar']:>6.2f} {f['long_pnl']:>8.1f} {f['short_pnl']:>8.1f} | "
            f"{h1['sharpe_0']:>8.2f} {h2['sharpe_0']:>8.2f}"
        )


def main():
    now_ms = int(time.time() * 1000)
    end_ms_1d = (now_ms // rs.DAY_MS) * rs.DAY_MS
    end_ms_6h = (now_ms // SIX_HOUR_MS) * SIX_HOUR_MS

    raw_data = {}
    for sym in UNIVERSE_SYMBOLS:
        b1d, b6h, fr = load_or_download_symbol(sym, end_ms_1d, end_ms_6h)
        raw_data[sym] = (b1d, b6h, fr)
        gaps_1d = sum(1 for i in range(1, len(b1d)) if b1d[i].open_time - b1d[i - 1].open_time != rs.DAY_MS)
        gaps_6h = sum(1 for i in range(1, len(b6h)) if b6h[i].open_time - b6h[i - 1].open_time != SIX_HOUR_MS)
        if gaps_1d or gaps_6h:
            print(f"[WARN] {sym}: gaps_1d={gaps_1d}, gaps_6h={gaps_6h}")
    print(f"Loaded {len(raw_data)} symbols up to {datetime.fromtimestamp(end_ms_1d/1000, tz=timezone.utc).strftime('%Y-%m-%d')} UTC.")

    # Serie ritorni BTC per correlazione causale
    btc_1d_bars = raw_data["BTCUSDT"][0]
    btc_1d_ret = pct_returns([b.close for b in btc_1d_bars], 1)
    btc_1d_map = {btc_1d_bars[i].open_time: btc_1d_ret[i] for i in range(len(btc_1d_bars))}

    btc_6h_bars = raw_data["BTCUSDT"][1]
    btc_6h_ret = pct_returns([b.close for b in btc_6h_bars], 1)
    btc_6h_map = {btc_6h_bars[i].open_time: btc_6h_ret[i] for i in range(len(btc_6h_bars))}

    feats_1d = {}
    feats_6h = {}
    for idx, sym in enumerate(UNIVERSE_SYMBOLS):
        b1d, b6h, fr = raw_data[sym]
        feats_1d[sym] = build_tf_features(sym, idx, b1d, rs.DAY_MS, fr, btc_1d_map, is_h6=False)
        feats_6h[sym] = build_tf_features(sym, idx, b6h, SIX_HOUR_MS, fr, btc_6h_map, is_h6=True)

    # Suite 1: Senza compounding (base 1000 USDT fissa, misura pura stabilita' 2021-2026)
    configs_flat = [
        StrategyConfig("V1_D1_alpha_oldsim", compounding=False, queue_mode="alpha"),
        StrategyConfig("V1_D1_live_tuple", compounding=False, queue_mode="tuple"),
        StrategyConfig("V1_D1_demo_cap_034x", compounding=False, queue_mode="tuple", max_notional_mult=0.3438),
        StrategyConfig("A1_D1_ranked_score", compounding=False, queue_mode="score"),
        StrategyConfig("A2_D1_ranked_max2side", compounding=False, queue_mode="score", max_same_side=2),
        StrategyConfig("A3_D1_ranked_corrcap", compounding=False, queue_mode="score", max_btc_corr_same_side=0.75),
        StrategyConfig("B1_D1_dyn_atr_trail", compounding=False, queue_mode="score", max_same_side=2, trail_mode="dynamic_atr"),
        StrategyConfig("B2_D1_profit_ratchet", compounding=False, queue_mode="score", max_same_side=2, trail_mode="profit_ratchet"),
        StrategyConfig("B3_D1_dyn_timestop12", compounding=False, queue_mode="score", max_same_side=2, trail_mode="dynamic_atr", time_stop_bars=12),
        StrategyConfig("C1_D1_long_only", compounding=False, queue_mode="score", signal_mode="long_only", max_same_side=2, trail_mode="dynamic_atr"),
        StrategyConfig("C2_D1_asym_short05", compounding=False, queue_mode="score", risk_fraction_short=0.005, funding_mode="sym_30bps", max_same_side=2, trail_mode="dynamic_atr"),
        StrategyConfig("C3_D1_strict_short05", compounding=False, queue_mode="score", risk_fraction_short=0.005, short_ret15_min=0.05, short_ret30_min=0.08, funding_mode="sym_30bps", max_same_side=2, trail_mode="dynamic_atr"),
        StrategyConfig("E1_D1_rs_leader_v1", compounding=False, queue_mode="rs_leader", signal_mode="rs_leader", dc_period=15),
        StrategyConfig("E2_D1_rs_dyn_asym", compounding=False, queue_mode="rs_leader", signal_mode="rs_leader", dc_period=15, risk_fraction_short=0.005, max_same_side=2, trail_mode="dynamic_atr"),
        StrategyConfig("D1_H6_tsmom_static", timeframe="6h", compounding=False, queue_mode="score", dc_period=15, max_same_side=2, trail_mult=2.5),
        StrategyConfig("D2_H6_tsmom_dyn", timeframe="6h", compounding=False, queue_mode="score", dc_period=15, max_same_side=2, trail_mode="dynamic_atr", trail_mult=2.5),
        StrategyConfig("D3_H6_asym_dyn", timeframe="6h", compounding=False, queue_mode="score", dc_period=15, risk_fraction_short=0.005, funding_mode="sym_30bps", max_same_side=2, trail_mode="dynamic_atr", trail_mult=2.5),
        StrategyConfig("D4_H6_strict_tr30", timeframe="6h", compounding=False, queue_mode="score", dc_period=20, risk_fraction_short=0.005, funding_mode="sym_30bps", max_same_side=2, trail_mode="dynamic_atr", trail_mult=3.0),
    ]

    flat_res = evaluate_suite(configs_flat, feats_1d, feats_6h, end_ms_1d, end_ms_6h)
    print_summary_table("CONFRONTO PURO 2021-2026 (NON-COMPOUNDED, BASE 1000 USDT, TAKER 5bps + 2bps SLIP)", flat_res)

    print("\n=== DETTAGLIO ANNO PER ANNO (BASE 1000 USDT) ===")
    for row in flat_res:
        if row["name"] in (
            "V1_D1_live_tuple",
            "V1_D1_demo_cap_034x",
            "A2_D1_ranked_max2side",
            "B1_D1_dyn_atr_trail",
            "C1_D1_long_only",
            "C2_D1_asym_short05",
            "C3_D1_strict_short05",
            "E2_D1_rs_dyn_asym",
            "D3_H6_asym_dyn",
            "D4_H6_strict_tr30",
        ):
            print(f"\n[{row['name']}] fees={row['full_2021_2026']['fees_usdt']} funding={row['full_2021_2026']['funding_usdt']}")
            print("  by_year:", json.dumps(row["full_2021_2026"]["by_year"]))

    # Suite 2: Compounding reale + Confronto Taker vs Maker vs Stress 1.5x/2.0x sui top candidate
    top_configs_comp = [
        StrategyConfig("V1_D1_live_comp", compounding=True, queue_mode="tuple"),
        StrategyConfig("V1_D1_democap_comp", compounding=True, queue_mode="tuple", max_notional_mult=0.3438),
        StrategyConfig("V2_D1_asym_dyn_comp", compounding=True, queue_mode="score", risk_fraction_short=0.005, funding_mode="sym_30bps", max_same_side=2, trail_mode="dynamic_atr"),
        StrategyConfig("V2_D1_asym_maker_comp", compounding=True, queue_mode="score", risk_fraction_short=0.005, funding_mode="sym_30bps", max_same_side=2, trail_mode="dynamic_atr", fee_rate=0.0002, slippage_rate=0.00005),
        StrategyConfig("V2_D1_asym_stress15x", compounding=True, queue_mode="score", risk_fraction_short=0.005, funding_mode="sym_30bps", max_same_side=2, trail_mode="dynamic_atr", fee_rate=0.00075, slippage_rate=0.0003, stop_slippage_rate=0.0003),
        StrategyConfig("V2_D1_longonly_comp", compounding=True, queue_mode="score", signal_mode="long_only", max_same_side=2, trail_mode="dynamic_atr"),
        StrategyConfig("V2_H6_asym_dyn_comp", timeframe="6h", compounding=True, queue_mode="score", dc_period=15, risk_fraction_short=0.005, funding_mode="sym_30bps", max_same_side=2, trail_mode="dynamic_atr", trail_mult=2.5),
        StrategyConfig("V2_H6_asym_maker_comp", timeframe="6h", compounding=True, queue_mode="score", dc_period=15, risk_fraction_short=0.005, funding_mode="sym_30bps", max_same_side=2, trail_mode="dynamic_atr", trail_mult=2.5, fee_rate=0.0002, slippage_rate=0.00005),
        StrategyConfig("V2_H6_asym_stress15x", timeframe="6h", compounding=True, queue_mode="score", dc_period=15, risk_fraction_short=0.005, funding_mode="sym_30bps", max_same_side=2, trail_mode="dynamic_atr", trail_mult=2.5, fee_rate=0.00075, slippage_rate=0.0003, stop_slippage_rate=0.0003),
    ]
    comp_res = evaluate_suite(top_configs_comp, feats_1d, feats_6h, end_ms_1d, end_ms_6h)
    print_summary_table("CONFRONTO COMPOUNDED REALISTICO 2021-2026 (TAKER vs MAKER vs STRESS 1.5x)", comp_res)

    out_path = CACHE_DIR / "v2_2021_2026_report.json"
    out_path.write_text(json.dumps({"flat": flat_res, "compounded": comp_res}, indent=2), encoding="utf-8")
    print(f"\nSaved full JSON report to {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
