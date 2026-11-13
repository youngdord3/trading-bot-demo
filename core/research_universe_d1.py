"""Ricerca su paniere allargato (12 perpetual Major 1d), filtro Forza Relativa Cross-Sectional
ed Efficiency Ratio / TSMOM (Passo A.12).

Modulo di sola ricerca: nessun accesso al database, nessun segreto, nessun ordine.
Scarica direttamente candele giornaliere 1d (780g = 720g + 60g warmup, 1 chiamata HTTP per simbolo)
e storico Funding Rate 8h (/fapi/v1/fundingRate) su 12 perpetual liquidi storici, valutando
sia su finestre bimestrali (360g / 720g) sia in modalita' continua (365g / 730g):
  1. d1_basket12_baseline:    Regola canonica 1d (dc=15, adx>=18, sl=1.5, trail=2.5) su 12 simboli.
  2. d1_rs_leader_trail:      Breakout 1d ristretto ai Top-4 leader di forza relativa (LONG) e
                              Bottom-4 laggard (SHORT) con filtro Efficiency Ratio (ER10 >= 0.25).
  3. d1_squeeze_er_trail:     Breakout 1d con filtro di efficienza direzionale di Kaufman
                              (ER10 >= 0.30, blocca i mercati laterali/whipsaw) e trailing 2.0*ATR.
  4. d1_tsmom_erf_trail:      Time-Series Momentum multi-orizzonte (15d + 30d concordi + ER10 >= 0.30
                              + Funding Anti-Crowding Gate) con trailing 2.0*ATR.
"""
import bisect
import os
import sys
import time
from typing import NamedTuple
from urllib.parse import urlencode

import research_d1_validation as rd
import research_paradigms as rp
import research_signals as rs

DEFAULT_UNIVERSE = (
    "BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,DOGEUSDT,XRPUSDT,"
    "ADAUSDT,AVAXUSDT,LINKUSDT,DOTUSDT,LTCUSDT,BCHUSDT"
)
UNIVERSE_SYMBOLS = [
    item.strip().upper()
    for item in os.environ.get("RU_SYMBOLS", DEFAULT_UNIVERSE).split(",")
    if item.strip()
]

BIMESTER_DAYS = 60
BIMESTERS_SHORT = 6
BIMESTERS_LONG = 12
WARMUP_DAYS = int(os.environ.get("RU_WARMUP_DAYS", "60"))

RISK_FRACTION = float(os.environ.get("RU_RISK_FRACTION", "0.005"))
MAX_CONCURRENT = int(os.environ.get("RU_MAX_CONCURRENT", "3"))
TOP_K_LEADERS = int(os.environ.get("RU_TOP_K_LEADERS", "4"))

SL_MULT = 1.5
MAX_BREAKOUT_ATR = 1.8

ARCHITECTURES = (
    "d1_basket12_baseline",
    "d1_rs_leader_trail",
    "d1_squeeze_er_trail",
    "d1_tsmom_erf_trail",
)


class UniverseD1Bundle(NamedTuple):
    symbol: str
    bars: list
    times: list
    ema20: list
    ema50: list
    atr14: list
    atr_sma20: list
    adx14: list
    er10: list
    ret15: list
    ret20: list
    ret30: list
    dc_high: dict
    dc_low: dict
    funding_last: list
    funding_sum3: list
    funding_map: dict


def fetch_bars_1d(symbol, start_ms, end_ms):
    """Scarica direttamente le candele 1d chiuse in [start_ms, end_ms) da fapi.binance.com."""
    bars = []
    cursor = start_ms
    while cursor < end_ms:
        query = urlencode({
            "symbol": symbol,
            "interval": "1d",
            "startTime": cursor,
            "endTime": end_ms - 1,
            "limit": rs.KLINES_LIMIT,
        })
        payload = rs._get_json(rs.KLINES_PATH + "?" + query)
        if not isinstance(payload, list) or not payload:
            break
        for row in payload:
            bars.append(rs.Bar(
                int(row[0]),
                float(row[1]),
                float(row[2]),
                float(row[3]),
                float(row[4]),
                float(row[5]),
            ))
        cursor = int(payload[-1][0]) + rs.DAY_MS
        if len(payload) < rs.KLINES_LIMIT:
            break
        time.sleep(rs.REQUEST_PAUSE_S)
    return bars


def count_gaps_1d(bars):
    return sum(
        1 for i in range(1, len(bars))
        if bars[i].open_time - bars[i - 1].open_time != rs.DAY_MS
    )


def efficiency_ratio(closes, period=10):
    """Kaufman Efficiency Ratio ER[i] = |close[i] - close[i-period]| / sum(|diff|).

    Valori vicini a 1.0 indicano trend direzionale pulito; valori < 0.25 indicano chop/whipsaw.
    """
    count = len(closes)
    out = [None] * count
    if period <= 0 or count <= period:
        return out
    for i in range(period, count):
        net_move = abs(closes[i] - closes[i - period])
        path_move = sum(abs(closes[k] - closes[k - 1]) for k in range(i - period + 1, i + 1))
        out[i] = (net_move / path_move) if path_move > 0 else 0.0
    return out


def simple_sma_optional(values, period):
    count = len(values)
    out = [None] * count
    if period <= 0 or count < period:
        return out
    window = []
    for i, v in enumerate(values):
        if v is None:
            window.clear()
            continue
        window.append(v)
        if len(window) > period:
            window.pop(0)
        if len(window) == period:
            out[i] = sum(window) / period
    return out


def pct_returns(closes, lookback):
    count = len(closes)
    out = [None] * count
    if lookback <= 0 or count <= lookback:
        return out
    for i in range(lookback, count):
        prev = closes[i - lookback]
        if prev > 0:
            out[i] = (closes[i] - prev) / prev
    return out


def build_universe_bundle(symbol, bars_1d, funding_points):
    closes = [b.close for b in bars_1d]
    highs = [b.high for b in bars_1d]
    lows = [b.low for b in bars_1d]
    times = [b.open_time for b in bars_1d]

    atr14 = rs.atr(highs, lows, closes, 14)
    dc_high = {}
    dc_low = {}
    for period in (10, 12, 15, 20):
        u, l = rs.donchian_bounds(highs, lows, period)
        dc_high[period] = u
        dc_low[period] = l

    f_last, f_sum3 = rp.align_funding_series(times, rs.DAY_MS, funding_points)
    funding_map = {p.funding_time: p.rate for p in funding_points}

    return UniverseD1Bundle(
        symbol=symbol,
        bars=bars_1d,
        times=times,
        ema20=rs.ema(closes, 20),
        ema50=rs.ema(closes, 50),
        atr14=atr14,
        atr_sma20=simple_sma_optional(atr14, 20),
        adx14=rs.adx(highs, lows, closes, 14),
        er10=efficiency_ratio(closes, 10),
        ret15=pct_returns(closes, 15),
        ret20=pct_returns(closes, 20),
        ret30=pct_returns(closes, 30),
        dc_high=dc_high,
        dc_low=dc_low,
        funding_last=f_last,
        funding_sum3=f_sum3,
        funding_map=funding_map,
    )


def build_cross_sectional_ranks(bundles_by_symbol, top_k=TOP_K_LEADERS):
    """Calcola per ogni timestamp giornaliero t i Top-K leader e Bottom-K laggard.

    Usa lo score causale `ret20[i] / (atr14[i] / close[i])` alla chiusura del giorno t.
    Ritorna (leaders_by_time, laggards_by_time) dove ogni valore e' un set di simboli.
    """
    time_to_scores = {}
    for symbol, bundle in bundles_by_symbol.items():
        for i, t_ms in enumerate(bundle.times):
            r20 = bundle.ret20[i]
            atr_val = bundle.atr14[i]
            close = bundle.bars[i].close
            if r20 is None or atr_val is None or atr_val <= 0 or close <= 0:
                continue
            vol_norm_ret = r20 / (atr_val / close)
            time_to_scores.setdefault(t_ms, []).append((vol_norm_ret, r20, symbol))

    leaders_by_time = {}
    laggards_by_time = {}
    for t_ms, entries in time_to_scores.items():
        if len(entries) < 2:
            continue
        ordered = sorted(entries)
        k = min(top_k, max(1, len(ordered) // 2))
        laggards_by_time[t_ms] = {sym for _score, r20, sym in ordered[:k] if r20 < 0}
        leaders_by_time[t_ms] = {sym for _score, r20, sym in ordered[-k:] if r20 > 0}
    return leaders_by_time, laggards_by_time


# ---------------------------------------------------------------------------
# Segnali delle 4 architetture 1d
# ---------------------------------------------------------------------------


def _funding_allows(bundle, index, side):
    fr = bundle.funding_last[index]
    fsum = bundle.funding_sum3[index]
    if side == 1 and (fr >= rp.FUNDING_CROWDED_LONG or fsum >= rp.FUNDING_CROWDED_LONG * 2.5):
        return False
    if side == -1 and (fr <= rp.FUNDING_CROWDED_SHORT or fsum <= rp.FUNDING_CROWDED_SHORT * 2.5):
        return False
    return True


def signal_architecture(
    architecture,
    bundle,
    index,
    leaders_by_time,
    laggards_by_time,
    dc_period=15,
    adx_min=18.0,
    er_min=0.30,
):
    if index < 1:
        return 0
    atr_val = bundle.atr14[index]
    adx_val = bundle.adx14[index]
    ema20_val = bundle.ema20[index]
    ema50_val = bundle.ema50[index]
    if any(v is None for v in (atr_val, adx_val, ema20_val, ema50_val)) or atr_val <= 0:
        return 0
    close = bundle.bars[index].close
    t_ms = bundle.times[index]

    if architecture == "d1_basket12_baseline":
        upper = bundle.dc_high[dc_period][index]
        lower = bundle.dc_low[dc_period][index]
        if upper is None or lower is None or adx_val < adx_min:
            return 0
        if close > ema20_val >= ema50_val and close > upper:
            if (close - upper) / atr_val <= MAX_BREAKOUT_ATR:
                return 1
        if close < ema20_val <= ema50_val and close < lower:
            if (lower - close) / atr_val <= MAX_BREAKOUT_ATR:
                return -1
        return 0

    if architecture == "d1_rs_leader_trail":
        er_val = bundle.er10[index]
        upper = bundle.dc_high[dc_period][index]
        lower = bundle.dc_low[dc_period][index]
        if er_val is None or upper is None or lower is None:
            return 0
        if adx_val < adx_min or er_val < 0.25:
            return 0
        if (
            bundle.symbol in leaders_by_time.get(t_ms, set())
            and close > ema20_val >= ema50_val
            and close > upper
            and (close - upper) / atr_val <= MAX_BREAKOUT_ATR
            and _funding_allows(bundle, index, 1)
        ):
            return 1
        if (
            bundle.symbol in laggards_by_time.get(t_ms, set())
            and close < ema20_val <= ema50_val
            and close < lower
            and (lower - close) / atr_val <= MAX_BREAKOUT_ATR
            and _funding_allows(bundle, index, -1)
        ):
            return -1
        return 0

    if architecture == "d1_squeeze_er_trail":
        er_val = bundle.er10[index]
        atr_sma = bundle.atr_sma20[index]
        upper = bundle.dc_high[dc_period][index]
        lower = bundle.dc_low[dc_period][index]
        if any(v is None for v in (er_val, atr_sma, upper, lower)):
            return 0
        if adx_val < adx_min or er_val < er_min or atr_val < 0.90 * atr_sma:
            return 0
        if (
            close > ema20_val >= ema50_val
            and close > upper
            and (close - upper) / atr_val <= MAX_BREAKOUT_ATR
            and _funding_allows(bundle, index, 1)
        ):
            return 1
        if (
            close < ema20_val <= ema50_val
            and close < lower
            and (lower - close) / atr_val <= MAX_BREAKOUT_ATR
            and _funding_allows(bundle, index, -1)
        ):
            return -1
        return 0

    if architecture == "d1_tsmom_erf_trail":
        er_val = bundle.er10[index]
        r15 = bundle.ret15[index]
        r30 = bundle.ret30[index]
        upper10 = bundle.dc_high[10][index]
        lower10 = bundle.dc_low[10][index]
        if any(v is None for v in (er_val, r15, r30, upper10, lower10)):
            return 0
        if adx_val < adx_min or er_val < er_min:
            return 0
        if (
            r15 > 0.03
            and r30 > 0.05
            and close > ema20_val >= ema50_val
            and close > upper10
            and (close - upper10) / atr_val <= MAX_BREAKOUT_ATR
            and _funding_allows(bundle, index, 1)
        ):
            return 1
        if (
            r15 < -0.03
            and r30 < -0.05
            and close < ema20_val <= ema50_val
            and close < lower10
            and (lower10 - close) / atr_val <= MAX_BREAKOUT_ATR
            and _funding_allows(bundle, index, -1)
        ):
            return -1
        return 0

    raise ValueError("INVALID_ARCHITECTURE: " + str(architecture))


# ---------------------------------------------------------------------------
# Simulazione portafoglio e valutazione bimestrale / continua / stress
# ---------------------------------------------------------------------------


def simulate_symbol_arch(
    architecture,
    bundle,
    leaders_by_time,
    laggards_by_time,
    start_ms,
    end_ms,
    dc_period=15,
    adx_min=18.0,
    er_min=0.30,
    trail_mult=2.0,
    risk_fraction=RISK_FRACTION,
    fee_rate=rs.TAKER_FEE_RATE,
    slippage_rate=rs.SLIPPAGE_RATE,
):
    effective_trail = 2.5 if architecture == "d1_basket12_baseline" else trail_mult
    cfg = rd.D1Config(
        architecture, dc_period, adx_min, effective_trail, risk_fraction, MAX_CONCURRENT, fee_rate, slippage_rate
    )
    low_idx = bisect.bisect_left(bundle.times, start_ms)
    high_idx = bisect.bisect_left(bundle.times, end_ms)
    trades = []
    position = None
    funding_acc = 0.0

    for i in range(low_idx, high_idx):
        bar = bundle.bars[i]
        if position is not None:
            funding_acc += rp.funding_pnl_for_bar(
                position, bar.open_time, rs.DAY_MS, bundle.funding_map, bar.close
            )
            fill = rd._exit_fill_d1(position, bar, slippage_rate)
            if fill is not None:
                trades.append(
                    rd._close_pos_d1(
                        position, fill[0], bar.open_time + rs.DAY_MS, fill[1], funding_acc, fee_rate
                    )
                )
                position = None
                funding_acc = 0.0
                continue
            position = rs.advance_trailing(position, bar)
            continue
        if i + 1 >= high_idx:
            continue
        side = signal_architecture(
            architecture,
            bundle,
            i,
            leaders_by_time,
            laggards_by_time,
            dc_period=dc_period,
            adx_min=adx_min,
            er_min=er_min,
        )
        if side == 0:
            continue
        next_bar = bundle.bars[i + 1]
        position = rd._open_pos_d1(
            bundle.symbol, side, next_bar.open_time, next_bar.open, bundle.atr14[i], cfg
        )
        funding_acc = 0.0

    if position is not None and high_idx > low_idx:
        last_bar = bundle.bars[high_idx - 1]
        price = last_bar.close * (1.0 - slippage_rate * position.side)
        trades.append(
            rd._close_pos_d1(
                position, price, last_bar.open_time + rs.DAY_MS, "END", funding_acc, fee_rate
            )
        )
    return trades


def run_architecture_window(
    architecture,
    bundles_by_symbol,
    leaders_by_time,
    laggards_by_time,
    start_ms,
    end_ms,
    dc_period=15,
    adx_min=18.0,
    er_min=0.30,
    trail_mult=2.0,
    risk_fraction=RISK_FRACTION,
    max_concurrent=MAX_CONCURRENT,
    fee_rate=rs.TAKER_FEE_RATE,
    slippage_rate=rs.SLIPPAGE_RATE,
):
    raw = []
    for symbol in sorted(bundles_by_symbol):
        raw.extend(
            simulate_symbol_arch(
                architecture,
                bundles_by_symbol[symbol],
                leaders_by_time,
                laggards_by_time,
                start_ms,
                end_ms,
                dc_period=dc_period,
                adx_min=adx_min,
                er_min=er_min,
                trail_mult=trail_mult,
                risk_fraction=risk_fraction,
                fee_rate=fee_rate,
                slippage_rate=slippage_rate,
            )
        )
    return rs.apply_max_concurrent(raw, max_concurrent)


def evaluate_architecture(
    architecture,
    bundles_by_symbol,
    leaders_by_time,
    laggards_by_time,
    horizon_days,
    windows,
    eval_mode="bimesters",
    fee_rate=rs.TAKER_FEE_RATE,
    slippage_rate=rs.SLIPPAGE_RATE,
):
    oos_trades = []
    winning_w = 0
    losing_w = 0
    for w_start, w_end in windows:
        w_trades = run_architecture_window(
            architecture,
            bundles_by_symbol,
            leaders_by_time,
            laggards_by_time,
            w_start,
            w_end,
            fee_rate=fee_rate,
            slippage_rate=slippage_rate,
        )
        oos_trades.extend(w_trades)
        wm = rs.metrics(w_trades, rs.INITIAL_EQUITY_USDT)
        if wm["net_pnl_usdt"] > 0:
            winning_w += 1
        elif wm["trades"] > 0:
            losing_w += 1

    summary = rs.metrics(oos_trades, rs.INITIAL_EQUITY_USDT)
    accepted, reasons = rs.evaluate_acceptance(summary)
    by_symbol = {}
    for trade in oos_trades:
        entry = by_symbol.setdefault(trade.symbol, {"trades": 0, "net_pnl_usdt": 0.0})
        entry["trades"] += 1
        entry["net_pnl_usdt"] = round(entry["net_pnl_usdt"] + trade.net_pnl, 4)

    return {
        "architecture": architecture,
        "eval_mode": eval_mode,
        "horizon_days": horizon_days,
        "windows_total": len(windows),
        "winning_windows": winning_w,
        "losing_windows": losing_w,
        "verdict": "ACCEPTED" if accepted else "REJECTED",
        "reasons": reasons,
        "out_of_sample": rs._json_safe(summary),
        "by_symbol": by_symbol,
    }


def evaluate_neighborhood(
    architecture,
    bundles_by_symbol,
    leaders_by_time,
    laggards_by_time,
    bimesters_360,
    bimesters_720,
):
    """Verifica stabilita' su 12 combinazioni nell'intorno dei parametri per l'architettura data."""
    combos = [
        (dc, adx, tr)
        for dc in (12, 15, 20)
        for adx in (16.0, 18.0)
        for tr in (2.0, 2.5)
    ]
    pos_360 = 0
    acc_360 = 0
    pos_720 = 0
    acc_720 = 0
    both_acc = 0

    for dc, adx, tr in combos:
        t360 = []
        for s_ms, e_ms in bimesters_360:
            t360.extend(
                run_architecture_window(
                    architecture,
                    bundles_by_symbol,
                    leaders_by_time,
                    laggards_by_time,
                    s_ms,
                    e_ms,
                    dc_period=dc,
                    adx_min=adx,
                    trail_mult=tr,
                )
            )
        m360 = rs.metrics(t360, rs.INITIAL_EQUITY_USDT)
        ok360, _ = rs.evaluate_acceptance(m360)

        t720 = []
        for s_ms, e_ms in bimesters_720:
            t720.extend(
                run_architecture_window(
                    architecture,
                    bundles_by_symbol,
                    leaders_by_time,
                    laggards_by_time,
                    s_ms,
                    e_ms,
                    dc_period=dc,
                    adx_min=adx,
                    trail_mult=tr,
                )
            )
        m720 = rs.metrics(t720, rs.INITIAL_EQUITY_USDT)
        ok720, _ = rs.evaluate_acceptance(m720)

        if m360["net_pnl_usdt"] > 0:
            pos_360 += 1
        if ok360:
            acc_360 += 1
        if m720["net_pnl_usdt"] > 0:
            pos_720 += 1
        if ok720:
            acc_720 += 1
        if ok360 and ok720:
            both_acc += 1

    return {
        "architecture": architecture,
        "combinations": len(combos),
        "positive_360d": pos_360,
        "accepted_360d": acc_360,
        "positive_720d": pos_720,
        "accepted_720d": acc_720,
        "both_horizons_accepted": both_acc,
    }


def main():
    for symbol in UNIVERSE_SYMBOLS:
        if not rs.SYMBOL_PATTERN.match(symbol):
            rs.log_event("invalid_symbol", symbol=symbol)
            return 2

    end_ms = (int(time.time() * 1000) // rs.DAY_MS) * rs.DAY_MS
    total_days = BIMESTERS_LONG * BIMESTER_DAYS + WARMUP_DAYS
    start_ms = end_ms - total_days * rs.DAY_MS
    min_bars_1d = total_days - 5

    bundles_by_symbol = {}
    for symbol in UNIVERSE_SYMBOLS:
        bars_1d = fetch_bars_1d(symbol, start_ms, end_ms)
        if len(bars_1d) < min_bars_1d:
            rs.log_event("symbol_skipped", symbol=symbol, bars_1d=len(bars_1d), min_bars=min_bars_1d)
            continue
        funding = rp.fetch_funding_rates(symbol, start_ms, end_ms)
        bundle = build_universe_bundle(symbol, bars_1d, funding)
        bundles_by_symbol[symbol] = bundle
        rs.log_event(
            "data_loaded",
            symbol=symbol,
            bars_1d=len(bars_1d),
            funding_points=len(funding),
            gaps_1d=count_gaps_1d(bars_1d),
        )

    if len(bundles_by_symbol) < 2:
        rs.log_event("no_data")
        return 2

    leaders_by_time, laggards_by_time = build_cross_sectional_ranks(bundles_by_symbol)
    bimesters_360 = rd.build_bimesters(end_ms, BIMESTERS_SHORT)
    bimesters_720 = rd.build_bimesters(end_ms, BIMESTERS_LONG)
    cont_365 = [(end_ms - 365 * rs.DAY_MS, end_ms)]
    cont_730 = [(end_ms - 730 * rs.DAY_MS, end_ms)]

    dual_bimester_accepted = []
    dual_continuous_accepted = []

    for arch in ARCHITECTURES:
        rep_b360 = evaluate_architecture(
            arch, bundles_by_symbol, leaders_by_time, laggards_by_time, 360, bimesters_360, "bimesters"
        )
        rep_b720 = evaluate_architecture(
            arch, bundles_by_symbol, leaders_by_time, laggards_by_time, 720, bimesters_720, "bimesters"
        )
        rs.log_event("universe_evaluation", **rep_b360)
        rs.log_event("universe_evaluation", **rep_b720)
        if rep_b360["verdict"] == "ACCEPTED" and rep_b720["verdict"] == "ACCEPTED":
            dual_bimester_accepted.append(arch)

        rep_c365 = evaluate_architecture(
            arch, bundles_by_symbol, leaders_by_time, laggards_by_time, 365, cont_365, "continuous"
        )
        rep_c730 = evaluate_architecture(
            arch, bundles_by_symbol, leaders_by_time, laggards_by_time, 730, cont_730, "continuous"
        )
        rs.log_event("universe_evaluation", **rep_c365)
        rs.log_event("universe_evaluation", **rep_c730)
        if rep_c365["verdict"] == "ACCEPTED" and rep_c730["verdict"] == "ACCEPTED":
            dual_continuous_accepted.append(arch)

        stab = evaluate_neighborhood(
            arch, bundles_by_symbol, leaders_by_time, laggards_by_time, bimesters_360, bimesters_720
        )
        rs.log_event("universe_neighborhood_stability", **stab)

    stress_passed = {}
    candidates_for_stress = sorted(set(dual_bimester_accepted + dual_continuous_accepted))
    if not candidates_for_stress:
        candidates_for_stress = ["d1_rs_leader_trail", "d1_squeeze_er_trail"]

    for arch in candidates_for_stress:
        passed_mults = []
        for mult in (1.5, 2.0):
            s_360 = evaluate_architecture(
                arch,
                bundles_by_symbol,
                leaders_by_time,
                laggards_by_time,
                360,
                bimesters_360,
                f"stress_{mult}x",
                fee_rate=rs.TAKER_FEE_RATE * mult,
                slippage_rate=rs.SLIPPAGE_RATE * mult,
            )
            s_720 = evaluate_architecture(
                arch,
                bundles_by_symbol,
                leaders_by_time,
                laggards_by_time,
                720,
                bimesters_720,
                f"stress_{mult}x",
                fee_rate=rs.TAKER_FEE_RATE * mult,
                slippage_rate=rs.SLIPPAGE_RATE * mult,
            )
            rs.log_event("universe_cost_stress", cost_multiplier=mult, **s_360)
            rs.log_event("universe_cost_stress", cost_multiplier=mult, **s_720)
            if s_360["verdict"] == "ACCEPTED" and s_720["verdict"] == "ACCEPTED":
                passed_mults.append(f"{mult}x")
        stress_passed[arch] = passed_mults

    confirmed = sorted(set(dual_bimester_accepted) & set(dual_continuous_accepted))
    rs.log_event(
        "universe_d1_summary",
        symbols_count=len(bundles_by_symbol),
        dual_bimester_accepted=dual_bimester_accepted,
        dual_continuous_accepted=dual_continuous_accepted,
        fully_confirmed_candidates=confirmed,
        stress_test_dual_passed=stress_passed,
        result="CANDIDATE_CONFIRMED" if (dual_bimester_accepted or dual_continuous_accepted) else "NO_CANDIDATE",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
