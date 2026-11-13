"""Ricerca nuovi paradigmi strutturali su 730g e 365g (Passo A.10):
1. fixed_h1_canonical:      Regola fissa 1h (dc=20, adx>=25, sl=1.5, trail=3.0) senza
                            ri-ottimizzazione rolling 180g (elimina l'inversione IS->OOS).
2. fixed_h1_funding_gated:  Regola fissa 1h + filtro anti-crowding su Funding Rate 8h
                            (/fapi/v1/fundingRate) con accredito/addebito funding reale.
3. fixed_d1_donchian_trail: Breakout Donchian nativo su candele giornaliere (1d) senza
                            ottimizzazione rolling (dc=15d, EMA20>=EMA50, trail 2.5*ATR_1d).
4. funding_squeeze_4h:      Strategia contrarian/carry su eccessi di Funding Rate 8h +
                            conferma strutturale del prezzo su 4h (EMA20 + RSI).
5. cross_sectional_rs_4h:   Strategia Market-Neutral Long Leader / Short Laggard su 4h
                            (neutralizza il beta direzionale del mercato crypto).

Modulo di sola ricerca: nessun accesso al database, nessun segreto, nessun ordine.
Scarica una sola volta candele 1h (730g + 60g warmup = 18960 barre per simbolo) e lo
storico pubblico dei funding rate da fapi.binance.com (/fapi/v1/fundingRate).
"""
import bisect
import os
import sys
import time
from typing import NamedTuple
from urllib.parse import urlencode

import research_signals as rs

FUNDING_PATH = "/fapi/v1/fundingRate"
FUNDING_LIMIT = 1000
EIGHT_HOUR_MS = 8 * rs.HOUR_MS

DAYS_LONG = int(os.environ.get("RP_DAYS_LONG", "730"))
DAYS_SHORT = int(os.environ.get("RP_DAYS_SHORT", "365"))
WARMUP_DAYS = int(os.environ.get("RP_WARMUP_DAYS", "60"))
TRAIN_DAYS = int(os.environ.get("RP_TRAIN_DAYS", "180"))
TEST_DAYS = int(os.environ.get("RP_TEST_DAYS", "60"))

RISK_FRACTION = float(os.environ.get("RP_RISK_FRACTION", "0.005"))
MAX_CONCURRENT = int(os.environ.get("RP_MAX_CONCURRENT", "3"))

# Soglie Funding Rate per 8h (0.0003 = +0.03% per 8h, -0.0001 = -0.01% per 8h)
FUNDING_CROWDED_LONG = 0.00030
FUNDING_CROWDED_SHORT = -0.00010
FUNDING_EXTREME_HIGH = 0.00035
FUNDING_EXTREME_LOW = -0.00015

PARADIGMS = (
    "fixed_h1_canonical",
    "fixed_h1_funding_gated",
    "fixed_d1_donchian_trail",
    "funding_squeeze_4h",
    "cross_sectional_rs_4h",
)


class FundingPoint(NamedTuple):
    funding_time: int
    rate: float


class DailyFeatures(NamedTuple):
    bars: list
    times: list
    ema20: list
    ema50: list
    atr14: list
    adx14: list
    dc_high_15: list
    dc_low_15: list
    dc_high_20: list
    dc_low_20: list


class H4Features(NamedTuple):
    bars: list
    times: list
    ema20: list
    ema50: list
    ema200: list
    rsi14: list
    atr14: list
    adx14: list
    dc_high_10: list
    dc_low_10: list
    ret_6: list
    ret_12: list
    funding_last: list
    funding_sum3: list


class SymbolBundle(NamedTuple):
    feat_1h: rs.Features
    funding_1h: list
    funding_sum3_1h: list
    feat_4h: H4Features
    feat_1d: DailyFeatures
    funding_map: dict


def fetch_funding_rates(symbol, start_ms, end_ms):
    """Scarica lo storico funding rate 8h pubblico da /fapi/v1/fundingRate."""
    points = []
    cursor = start_ms
    while cursor < end_ms:
        query = urlencode({
            "symbol": symbol,
            "startTime": cursor,
            "endTime": end_ms - 1,
            "limit": FUNDING_LIMIT,
        })
        payload = rs._get_json(FUNDING_PATH + "?" + query)
        if not isinstance(payload, list) or not payload:
            break
        for row in payload:
            ftime = (int(row["fundingTime"]) // rs.HOUR_MS) * rs.HOUR_MS
            points.append(FundingPoint(ftime, float(row["fundingRate"])))
        next_cursor = int(payload[-1]["fundingTime"]) + 1000
        if next_cursor <= cursor or len(payload) < FUNDING_LIMIT:
            break
        cursor = next_cursor
        time.sleep(rs.REQUEST_PAUSE_S)
    return points


def aggregate_span(bars_1h, span_ms):
    """Aggrega candele 1h in candele complete di durata span_ms (es. 4h o 1d)."""
    needed = span_ms // rs.HOUR_MS
    out = []
    bucket = None
    current = None
    count = 0
    for bar in bars_1h:
        key = bar.open_time // span_ms
        if key != bucket:
            if current is not None and count == needed:
                out.append(current)
            bucket = key
            current = rs.Bar(key * span_ms, bar.open, bar.high, bar.low, bar.close, bar.volume)
            count = 1
        else:
            current = rs.Bar(
                current.open_time,
                current.open,
                max(current.high, bar.high),
                min(current.low, bar.low),
                bar.close,
                current.volume + bar.volume,
            )
            count += 1
    if current is not None and count == needed:
        out.append(current)
    return out


def align_funding_series(bar_times, span_ms, funding_points):
    """Allinea in modo causale l'ultimo funding rate noto e la somma degli ultimi 3 (24h)."""
    f_times = [p.funding_time for p in funding_points]
    f_rates = [p.rate for p in funding_points]
    last_out = []
    sum3_out = []
    for open_time in bar_times:
        close_time = open_time + span_ms
        idx = bisect.bisect_right(f_times, close_time) - 1
        if idx < 0:
            last_out.append(0.0)
            sum3_out.append(0.0)
        else:
            last_out.append(f_rates[idx])
            start_idx = max(0, idx - 2)
            sum3_out.append(sum(f_rates[start_idx:idx + 1]))
    return last_out, sum3_out


def _returns_over(closes, lookback):
    out = [None] * len(closes)
    for i in range(lookback, len(closes)):
        if closes[i - lookback] > 0:
            out[i] = (closes[i] - closes[i - lookback]) / closes[i - lookback]
    return out


def build_symbol_bundle(bars_1h, funding_points):
    """Costruisce tutte le feature 1h, 4h, 1d e la mappa funding per un simbolo."""
    feat_1h = rs.build_features(bars_1h)
    funding_1h, funding_sum3_1h = align_funding_series(feat_1h.times, rs.HOUR_MS, funding_points)

    bars_4h = aggregate_span(bars_1h, rs.FOUR_HOUR_MS)
    closes_4h = [b.close for b in bars_4h]
    highs_4h = [b.high for b in bars_4h]
    lows_4h = [b.low for b in bars_4h]
    times_4h = [b.open_time for b in bars_4h]
    dc_hi_10, dc_lo_10 = rs.donchian_bounds(highs_4h, lows_4h, 10)
    f_last_4h, f_sum3_4h = align_funding_series(times_4h, rs.FOUR_HOUR_MS, funding_points)

    feat_4h = H4Features(
        bars=bars_4h,
        times=times_4h,
        ema20=rs.ema(closes_4h, 20),
        ema50=rs.ema(closes_4h, 50),
        ema200=rs.ema(closes_4h, 200),
        rsi14=rs.rsi(closes_4h, 14),
        atr14=rs.atr(highs_4h, lows_4h, closes_4h, 14),
        adx14=rs.adx(highs_4h, lows_4h, closes_4h, 14),
        dc_high_10=dc_hi_10,
        dc_low_10=dc_lo_10,
        ret_6=_returns_over(closes_4h, 6),
        ret_12=_returns_over(closes_4h, 12),
        funding_last=f_last_4h,
        funding_sum3=f_sum3_4h,
    )

    bars_1d = aggregate_span(bars_1h, rs.DAY_MS)
    closes_1d = [b.close for b in bars_1d]
    highs_1d = [b.high for b in bars_1d]
    lows_1d = [b.low for b in bars_1d]
    dc_hi_15, dc_lo_15 = rs.donchian_bounds(highs_1d, lows_1d, 15)
    dc_hi_20, dc_lo_20 = rs.donchian_bounds(highs_1d, lows_1d, 20)

    feat_1d = DailyFeatures(
        bars=bars_1d,
        times=[b.open_time for b in bars_1d],
        ema20=rs.ema(closes_1d, 20),
        ema50=rs.ema(closes_1d, 50),
        atr14=rs.atr(highs_1d, lows_1d, closes_1d, 14),
        adx14=rs.adx(highs_1d, lows_1d, closes_1d, 14),
        dc_high_15=dc_hi_15,
        dc_low_15=dc_lo_15,
        dc_high_20=dc_hi_20,
        dc_low_20=dc_lo_20,
    )

    funding_map = {p.funding_time: p.rate for p in funding_points}
    return SymbolBundle(
        feat_1h=feat_1h,
        funding_1h=funding_1h,
        funding_sum3_1h=funding_sum3_1h,
        feat_4h=feat_4h,
        feat_1d=feat_1d,
        funding_map=funding_map,
    )


# ---------------------------------------------------------------------------
# Esecuzione con calcolo esatto di slippage, fee e funding rate 8h
# ---------------------------------------------------------------------------


def open_pos_sized(symbol, side, entry_time, raw_open, atr_val, sl_mult, trail_mult, risk_frac=RISK_FRACTION):
    entry_price = raw_open * (1.0 + rs.SLIPPAGE_RATE * side)
    stop_dist = sl_mult * atr_val
    if stop_dist <= 0 or entry_price <= 0:
        return None
    risk_usdt = rs.INITIAL_EQUITY_USDT * risk_frac
    qty = min(risk_usdt / stop_dist, rs.MAX_NOTIONAL_USDT / entry_price)
    stop_price = entry_price - side * stop_dist
    trail_dist = trail_mult * atr_val
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


def funding_pnl_for_bar(position, bar_open_ms, span_ms, funding_map, mark_price):
    """Calcola il cashflow di funding maturato nell'intervallo (bar_open_ms, bar_open_ms + span_ms].

    Su Binance Futures: se funding_rate > 0, i LONG (side=1) pagano e gli SHORT (side=-1) incassano.
    """
    if not funding_map:
        return 0.0
    total = 0.0
    cursor = ((bar_open_ms // rs.HOUR_MS) + 1) * rs.HOUR_MS
    end_ms = bar_open_ms + span_ms
    while cursor <= end_ms:
        rate = funding_map.get(cursor)
        if rate is not None:
            notional = position.qty * mark_price
            total += -position.side * notional * rate
        cursor += rs.HOUR_MS
    return total


def close_pos_with_funding(position, exit_price, exit_time, reason, funding_accrued):
    gross = (exit_price - position.entry_price) * position.qty * position.side
    fees = (position.entry_price + exit_price) * position.qty * rs.TAKER_FEE_RATE
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


# ---------------------------------------------------------------------------
# Paradigmi 1 e 2: Regola Fissa Canonica 1h (pura vs Funding Anti-Crowding Gate)
# ---------------------------------------------------------------------------


def simulate_fixed_h1(symbol, bundle, start_ms, end_ms, funding_gated=False):
    """Regola fissa canonica (dc=20, adx>=25, sl=1.5, trail=3.0) senza ri-ottimizzazione IS.

    Se funding_gated=True, blocca i breakout nella direzione gia' sovraffollata
    sul mercato dei perpetual (funding_last >= +0.03% per long, <= -0.01% per short).
    """
    feat = bundle.feat_1h
    funding_1h = bundle.funding_1h
    funding_sum3 = bundle.funding_sum3_1h
    fmap = bundle.funding_map
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
            funding_acc += funding_pnl_for_bar(position, bar.open_time, rs.HOUR_MS, fmap, bar.close)
            fill = rs.exit_fill(position, bar)
            if fill is not None:
                trades.append(
                    close_pos_with_funding(
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
        if side == 0:
            continue
        if funding_gated:
            fr = funding_1h[i]
            fsum = funding_sum3[i]
            if side == 1 and (fr >= FUNDING_CROWDED_LONG or fsum >= FUNDING_CROWDED_LONG * 2.5):
                continue
            if side == -1 and (fr <= FUNDING_CROWDED_SHORT or fsum <= FUNDING_CROWDED_SHORT * 2.5):
                continue
        next_bar = feat.bars[i + 1]
        position = open_pos_sized(
            symbol, side, next_bar.open_time, next_bar.open, feat.atr14[i], 1.5, 3.0
        )
        funding_acc = 0.0

    if position is not None and high_idx > low_idx:
        last_bar = feat.bars[high_idx - 1]
        price = last_bar.close * (1.0 - rs.SLIPPAGE_RATE * position.side)
        trades.append(
            close_pos_with_funding(
                position, price, last_bar.open_time + rs.HOUR_MS, "END", funding_acc
            )
        )
    return trades


# ---------------------------------------------------------------------------
# Paradigma 3: Regola Fissa Macro Giornaliera (1d Donchian Breakout + Trailing)
# ---------------------------------------------------------------------------


def signal_d1(feat_1d, i, dc_period=15, adx_min=18.0):
    if i < 1:
        return 0
    atr_val = feat_1d.atr14[i]
    adx_val = feat_1d.adx14[i]
    ema20_val = feat_1d.ema20[i]
    ema50_val = feat_1d.ema50[i]
    if any(v is None for v in (atr_val, adx_val, ema20_val, ema50_val)) or atr_val <= 0:
        return 0
    upper = feat_1d.dc_high_15[i] if dc_period == 15 else feat_1d.dc_high_20[i]
    lower = feat_1d.dc_low_15[i] if dc_period == 15 else feat_1d.dc_low_20[i]
    if upper is None or lower is None or adx_val < adx_min:
        return 0
    close = feat_1d.bars[i].close
    if close > ema20_val >= ema50_val and close > upper:
        if (close - upper) / atr_val <= 1.8:
            return 1
    if close < ema20_val <= ema50_val and close < lower:
        if (lower - close) / atr_val <= 1.8:
            return -1
    return 0


def simulate_fixed_d1(symbol, bundle, start_ms, end_ms, dc_period=15, trail_mult=2.5):
    feat = bundle.feat_1d
    fmap = bundle.funding_map
    low_idx = bisect.bisect_left(feat.times, start_ms)
    high_idx = bisect.bisect_left(feat.times, end_ms)
    trades = []
    position = None
    funding_acc = 0.0

    for i in range(low_idx, high_idx):
        bar = feat.bars[i]
        if position is not None:
            funding_acc += funding_pnl_for_bar(position, bar.open_time, rs.DAY_MS, fmap, bar.close)
            fill = rs.exit_fill(position, bar)
            if fill is not None:
                trades.append(
                    close_pos_with_funding(
                        position, fill[0], bar.open_time + rs.DAY_MS, fill[1], funding_acc
                    )
                )
                position = None
                funding_acc = 0.0
                continue
            position = rs.advance_trailing(position, bar)
            continue
        if i + 1 >= high_idx:
            continue
        side = signal_d1(feat, i, dc_period=dc_period)
        if side == 0:
            continue
        next_bar = feat.bars[i + 1]
        position = open_pos_sized(
            symbol, side, next_bar.open_time, next_bar.open, feat.atr14[i], 1.5, trail_mult
        )
        funding_acc = 0.0

    if position is not None and high_idx > low_idx:
        last_bar = feat.bars[high_idx - 1]
        price = last_bar.close * (1.0 - rs.SLIPPAGE_RATE * position.side)
        trades.append(
            close_pos_with_funding(
                position, price, last_bar.open_time + rs.DAY_MS, "END", funding_acc
            )
        )
    return trades


# ---------------------------------------------------------------------------
# Paradigma 4: Funding Rate Crowding Squeeze / Contrarian Carry su 4h
# ---------------------------------------------------------------------------


def signal_funding_squeeze_4h(feat_4h, i, pos_thresh=FUNDING_EXTREME_HIGH, neg_thresh=FUNDING_EXTREME_LOW):
    """Entra contro il posizionamento estremo sul funding quando il prezzo conferma la rottura:
    - Se funding 8h/24h <= neg_thresh (short affollati) e close > EMA20_4h -> LONG
    - Se funding 8h/24h >= pos_thresh (long euforici) e close < EMA20_4h con RSI < 58 -> SHORT
    """
    if i < 1:
        return 0
    atr_val = feat_4h.atr14[i]
    ema20_val = feat_4h.ema20[i]
    ema50_val = feat_4h.ema50[i]
    rsi_val = feat_4h.rsi14[i]
    if any(v is None for v in (atr_val, ema20_val, ema50_val, rsi_val)) or atr_val <= 0:
        return 0
    bar = feat_4h.bars[i]
    prev_bar = feat_4h.bars[i - 1]
    f_last = feat_4h.funding_last[i]
    f_sum3 = feat_4h.funding_sum3[i]
    dc_hi = feat_4h.dc_high_10[i]

    if (f_last <= neg_thresh or f_sum3 <= neg_thresh * 2.0) and bar.close > ema20_val and bar.close > bar.open:
        if prev_bar.close <= ema20_val or (dc_hi is not None and bar.close > dc_hi):
            return 1

    if (f_last >= pos_thresh or f_sum3 >= pos_thresh * 2.2) and bar.close < ema20_val and bar.close < bar.open:
        if rsi_val < 58.0:
            return -1
    return 0


def simulate_funding_squeeze_4h(symbol, bundle, start_ms, end_ms, trail_mult=2.5):
    feat = bundle.feat_4h
    fmap = bundle.funding_map
    low_idx = bisect.bisect_left(feat.times, start_ms)
    high_idx = bisect.bisect_left(feat.times, end_ms)
    trades = []
    position = None
    funding_acc = 0.0
    next_entry_idx = low_idx

    for i in range(low_idx, high_idx):
        bar = feat.bars[i]
        if position is not None:
            funding_acc += funding_pnl_for_bar(position, bar.open_time, rs.FOUR_HOUR_MS, fmap, bar.close)
            fill = rs.exit_fill(position, bar)
            if fill is not None:
                trades.append(
                    close_pos_with_funding(
                        position, fill[0], bar.open_time + rs.FOUR_HOUR_MS, fill[1], funding_acc
                    )
                )
                position = None
                funding_acc = 0.0
                next_entry_idx = i + 1
                continue
            position = rs.advance_trailing(position, bar)
            continue
        if i < next_entry_idx or i + 1 >= high_idx:
            continue
        side = signal_funding_squeeze_4h(feat, i)
        if side == 0:
            continue
        next_bar = feat.bars[i + 1]
        position = open_pos_sized(
            symbol, side, next_bar.open_time, next_bar.open, feat.atr14[i], 1.5, trail_mult
        )
        funding_acc = 0.0

    if position is not None and high_idx > low_idx:
        last_bar = feat.bars[high_idx - 1]
        price = last_bar.close * (1.0 - rs.SLIPPAGE_RATE * position.side)
        trades.append(
            close_pos_with_funding(
                position, price, last_bar.open_time + rs.FOUR_HOUR_MS, "END", funding_acc
            )
        )
    return trades


# ---------------------------------------------------------------------------
# Paradigma 5: Cross-Sectional Relative Strength (Market-Neutral Long/Short)
# ---------------------------------------------------------------------------


def simulate_cross_sectional_rs(bundles_by_symbol, start_ms, end_ms, hold_bars_4h=6, min_spread=0.035):
    """Strategia Market-Neutral su candele 4h (ribilanciata ogni hold_bars_4h * 4h = 24h):
    - Classifica i simboli per forza relativa normalizzata per volatilita' (ret_12 / atr_pct)
    - Se lo spread relativo tra il leader (#1) e il laggard (ultimo) supera min_spread
      con leader positivo e laggard negativo, apre simultaneamente:
        * LONG sul leader + SHORT sul laggard (beta-neutral).
    """
    symbols = sorted(bundles_by_symbol)
    if len(symbols) < 2:
        return []
    ref_feat = bundles_by_symbol[symbols[0]].feat_4h
    low_idx = bisect.bisect_left(ref_feat.times, start_ms)
    high_idx = bisect.bisect_left(ref_feat.times, end_ms)

    trades = []
    step = max(1, hold_bars_4h)
    i = low_idx
    while i + step < high_idx:
        t_open = ref_feat.times[i]
        scored = []
        valid = True
        for sym in symbols:
            f4 = bundles_by_symbol[sym].feat_4h
            if i >= len(f4.bars) or f4.times[i] != t_open:
                valid = False
                break
            ret12 = f4.ret_12[i]
            atr_val = f4.atr14[i]
            ema50_val = f4.ema50[i]
            close = f4.bars[i].close
            if ret12 is None or atr_val is None or ema50_val is None or atr_val <= 0 or close <= 0:
                valid = False
                break
            atr_pct = atr_val / close
            score = ret12 / atr_pct if atr_pct > 0 else 0.0
            scored.append((score, ret12, sym))

        if not valid or len(scored) < 2:
            i += 1
            continue

        scored.sort()
        _, laggard_ret, laggard_sym = scored[0]
        _, leader_ret, leader_sym = scored[-1]

        if (leader_ret - laggard_ret) < min_spread or leader_ret <= 0 or laggard_ret >= 0:
            i += 1
            continue

        for side, sym in ((1, leader_sym), (-1, laggard_sym)):
            bundle = bundles_by_symbol[sym]
            f4 = bundle.feat_4h
            fmap = bundle.funding_map
            entry_bar = f4.bars[i + 1]
            pos = open_pos_sized(
                sym,
                side,
                entry_bar.open_time,
                entry_bar.open,
                f4.atr14[i],
                1.5,
                2.5,
                risk_frac=RISK_FRACTION * 0.75,
            )
            if pos is None:
                continue
            funding_acc = 0.0
            closed = False
            for j in range(i + 1, min(i + 1 + step, high_idx)):
                bar = f4.bars[j]
                funding_acc += funding_pnl_for_bar(
                    pos, bar.open_time, rs.FOUR_HOUR_MS, fmap, bar.close
                )
                fill = rs.exit_fill(pos, bar)
                if fill is not None:
                    trades.append(
                        close_pos_with_funding(
                            pos, fill[0], bar.open_time + rs.FOUR_HOUR_MS, fill[1], funding_acc
                        )
                    )
                    closed = True
                    break
                pos = rs.advance_trailing(pos, bar)
            if not closed:
                exit_idx = min(i + step, high_idx - 1)
                last_bar = f4.bars[exit_idx]
                px = last_bar.close * (1.0 - rs.SLIPPAGE_RATE * pos.side)
                trades.append(
                    close_pos_with_funding(
                        pos, px, last_bar.open_time + rs.FOUR_HOUR_MS, "TIME_REBAL", funding_acc
                    )
                )
        i += step

    return trades


# ---------------------------------------------------------------------------
# Valutazione unificata su 365g (3 fold) e 730g (9 fold)
# ---------------------------------------------------------------------------


def run_paradigm_window(bundles_by_symbol, paradigm, start_ms, end_ms):
    if paradigm not in PARADIGMS:
        raise ValueError("INVALID_PARADIGM: " + str(paradigm))
    if paradigm == "cross_sectional_rs_4h":
        return simulate_cross_sectional_rs(bundles_by_symbol, start_ms, end_ms)

    raw = []
    for symbol in sorted(bundles_by_symbol):
        bundle = bundles_by_symbol[symbol]
        if paradigm == "fixed_h1_canonical":
            raw.extend(simulate_fixed_h1(symbol, bundle, start_ms, end_ms, funding_gated=False))
        elif paradigm == "fixed_h1_funding_gated":
            raw.extend(simulate_fixed_h1(symbol, bundle, start_ms, end_ms, funding_gated=True))
        elif paradigm == "fixed_d1_donchian_trail":
            raw.extend(simulate_fixed_d1(symbol, bundle, start_ms, end_ms))
        elif paradigm == "funding_squeeze_4h":
            raw.extend(simulate_funding_squeeze_4h(symbol, bundle, start_ms, end_ms))
    return rs.apply_max_concurrent(raw, MAX_CONCURRENT)


def evaluate_paradigm_horizon(bundles_by_symbol, paradigm, horizon_days, folds):
    oos_trades = []
    winning_folds = 0
    losing_folds = 0
    fold_details = []

    for idx, (_train_start, train_end, test_end) in enumerate(folds, start=1):
        fold_trades = run_paradigm_window(bundles_by_symbol, paradigm, train_end, test_end)
        oos_trades.extend(fold_trades)
        fm = rs.metrics(fold_trades, rs.INITIAL_EQUITY_USDT)
        if fm["net_pnl_usdt"] > 0:
            winning_folds += 1
        elif fm["trades"] > 0:
            losing_folds += 1
        fold_details.append({
            "fold": idx,
            "trades": fm["trades"],
            "pf": "inf" if fm["profit_factor"] == float("inf") else round(fm["profit_factor"], 4),
            "net_pnl": round(fm["net_pnl_usdt"], 4),
            "dd_pct": round(fm["max_drawdown_pct"], 4),
        })

    summary = rs.metrics(oos_trades, rs.INITIAL_EQUITY_USDT)
    accepted, reasons = rs.evaluate_acceptance(summary)
    by_symbol = {}
    for trade in oos_trades:
        entry = by_symbol.setdefault(trade.symbol, {"trades": 0, "net_pnl_usdt": 0.0})
        entry["trades"] += 1
        entry["net_pnl_usdt"] = round(entry["net_pnl_usdt"] + trade.net_pnl, 4)

    return {
        "paradigm": paradigm,
        "horizon_days": horizon_days,
        "folds_total": len(folds),
        "winning_folds": winning_folds,
        "losing_folds": losing_folds,
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

    bundles_by_symbol = {}
    for symbol in rs.SYMBOLS:
        bars = rs.fetch_bars_1h(symbol, start_long_ms, end_ms)
        if len(bars) < min_bars_long:
            rs.log_event("symbol_skipped", symbol=symbol, bars_1h=len(bars), min_bars=min_bars_long)
            continue
        funding = fetch_funding_rates(symbol, start_long_ms, end_ms)
        bundle = build_symbol_bundle(bars, funding)
        bundles_by_symbol[symbol] = bundle
        rs.log_event(
            "data_loaded",
            symbol=symbol,
            bars_1h=len(bars),
            bars_4h=len(bundle.feat_4h.bars),
            bars_1d=len(bundle.feat_1d.bars),
            funding_points=len(funding),
            gaps_1h=rs.count_gaps_1h(bars),
        )

    if not bundles_by_symbol:
        rs.log_event("no_data")
        return 2

    folds_365 = rs.build_folds(end_ms - DAYS_SHORT * rs.DAY_MS, end_ms, TRAIN_DAYS, TEST_DAYS)
    folds_730 = rs.build_folds(end_ms - DAYS_LONG * rs.DAY_MS, end_ms, TRAIN_DAYS, TEST_DAYS)

    accepted_365 = []
    accepted_730 = []

    for paradigm in PARADIGMS:
        for horizon_days, folds in ((DAYS_SHORT, folds_365), (DAYS_LONG, folds_730)):
            report = evaluate_paradigm_horizon(bundles_by_symbol, paradigm, horizon_days, folds)
            rs.log_event("paradigm_evaluation", **report)
            if report["verdict"] == "ACCEPTED":
                if horizon_days == DAYS_SHORT:
                    accepted_365.append(paradigm)
                elif horizon_days == DAYS_LONG:
                    accepted_730.append(paradigm)

    dual_accepted = [p for p in accepted_365 if p in accepted_730]
    rs.log_event(
        "paradigms_summary",
        paradigms_tested=list(PARADIGMS),
        accepted_365d=accepted_365,
        accepted_730d=accepted_730,
        dual_horizon_accepted=dual_accepted,
        result="DUAL_HORIZON_CANDIDATE_FOUND" if dual_accepted else "NO_DUAL_HORIZON_CANDIDATE",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
