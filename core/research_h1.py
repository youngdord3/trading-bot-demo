"""Ricerca walk-forward: segnale EMA cross su 1h, filtro trend 4h, diagnosi a imbuto.

Uso:
    python -u research_h1.py            # uscita SL/TP fissi su ATR (Passo A.5)
    python -u research_h1.py trailing   # trailing stop su ATR (Passo A.6)

Solo dati pubblici, nessun segreto, nessun accesso al database.
"""
import http.client
import json
import sys
import time
from decimal import Decimal

HOST = "fapi.binance.com"
ALLOWED_HOSTS = frozenset({"fapi.binance.com"})
HTTP_TIMEOUT_S = 20
MAX_RETRIES = 6
PAGE_LIMIT = 1500
PAGE_PAUSE_S = 0.25

SYMBOLS = ("BNBUSDT", "BTCUSDT", "DOGEUSDT", "ETHUSDT", "SOLUSDT")

H1_MS = 3_600_000
H4_MS = 4 * H1_MS
DAY_MS = 24 * H1_MS

DAYS = 365
WARMUP_DAYS = 30
TRAIN_DAYS = 180
TEST_DAYS = 60
FOLDS = 3

INITIAL_EQUITY = Decimal("1000")
RISK_PER_TRADE = Decimal("0.01")
MAX_LEVERAGE = Decimal("2")
MAX_POSITIONS = 3
MIN_NOTIONAL = Decimal("5")
FEE_RATE = Decimal("0.0005")
SLIPPAGE = Decimal("0.0002")

ATR_PERIOD = 14
ADX_PERIOD = 14
H4_EMA_FAST = 20
H4_EMA_SLOW = 50
SL_ATR = Decimal("1.5")

EXIT_FIXED = "fixed"
EXIT_TRAILING = "trailing"

# (ema_fast, ema_slow, adx_min, exit_mult)
# exit_mult: multiplo ATR del take profit (fixed) o distanza del trailing (trailing)
GRID = (
    (12, 26, Decimal("0"), Decimal("2")),
    (12, 26, Decimal("0"), Decimal("3")),
    (12, 26, Decimal("20"), Decimal("2")),
    (12, 26, Decimal("20"), Decimal("3")),
    (20, 50, Decimal("0"), Decimal("2")),
    (20, 50, Decimal("0"), Decimal("3")),
    (20, 50, Decimal("20"), Decimal("2")),
    (20, 50, Decimal("20"), Decimal("3")),
)

MIN_IN_SAMPLE_TRADES = 30
MIN_IN_SAMPLE_PF = Decimal("1.0")

OOS_MIN_TRADES = 30
OOS_MIN_PF = Decimal("1.30")
OOS_MAX_DD_PCT = Decimal("15.0")
OOS_MIN_WIN_RATE = Decimal("0.40")
OOS_MIN_PAYOFF = Decimal("2.0")

NO_LOSS_RATIO = Decimal("999")

FUNNEL_KEYS = (
    "raw_signals",
    "blocked_in_position",
    "blocked_warmup",
    "blocked_trend",
    "blocked_adx",
    "signals_passed",
    "blocked_max_positions",
    "blocked_sizing",
    "pending_discarded",
    "entries",
)


def log(event, **fields):
    record = {"event": event}
    record.update(fields)
    print(json.dumps(record, sort_keys=True), flush=True)


# ---------------------------------------------------------------- indicatori


def ema(values, period):
    out = [None] * len(values)
    if period <= 0 or len(values) < period:
        return out
    k = Decimal(2) / (period + 1)
    prev = sum(values[:period]) / period
    out[period - 1] = prev
    for i in range(period, len(values)):
        prev = (values[i] - prev) * k + prev
        out[i] = prev
    return out


def true_ranges(highs, lows, closes):
    out = []
    for i in range(len(highs)):
        if i == 0:
            out.append(highs[i] - lows[i])
        else:
            prev_close = closes[i - 1]
            out.append(max(
                highs[i] - lows[i],
                abs(highs[i] - prev_close),
                abs(lows[i] - prev_close),
            ))
    return out


def atr(highs, lows, closes, period):
    n = len(highs)
    out = [None] * n
    if period <= 0 or n < period:
        return out
    tr = true_ranges(highs, lows, closes)
    prev = sum(tr[:period]) / period
    out[period - 1] = prev
    for i in range(period, n):
        prev = (prev * (period - 1) + tr[i]) / period
        out[i] = prev
    return out


def adx(highs, lows, closes, period):
    n = len(highs)
    out = [None] * n
    if period <= 0 or n < 2 * period:
        return out
    zero = Decimal(0)
    hundred = Decimal(100)
    tr = true_ranges(highs, lows, closes)
    plus_dm = [zero] * n
    minus_dm = [zero] * n
    for i in range(1, n):
        up = highs[i] - highs[i - 1]
        down = lows[i - 1] - lows[i]
        if up > down and up > 0:
            plus_dm[i] = up
        if down > up and down > 0:
            minus_dm[i] = down
    tr_s = sum(tr[1:period + 1])
    plus_s = sum(plus_dm[1:period + 1])
    minus_s = sum(minus_dm[1:period + 1])
    dx = [None] * n
    for i in range(period, n):
        if i > period:
            tr_s = tr_s - tr_s / period + tr[i]
            plus_s = plus_s - plus_s / period + plus_dm[i]
            minus_s = minus_s - minus_s / period + minus_dm[i]
        if tr_s == 0:
            dx[i] = zero
            continue
        plus_di = hundred * plus_s / tr_s
        minus_di = hundred * minus_s / tr_s
        total = plus_di + minus_di
        if total == 0:
            dx[i] = zero
        else:
            dx[i] = hundred * abs(plus_di - minus_di) / total
    first = 2 * period - 1
    prev = sum(dx[period:first + 1]) / period
    out[first] = prev
    for i in range(first + 1, n):
        prev = (prev * (period - 1) + dx[i]) / period
        out[i] = prev
    return out


# ---------------------------------------------------------------- dati


def http_get_json(path):
    if HOST not in ALLOWED_HOSTS:
        raise RuntimeError("host_not_allowed")
    last_error = "unknown"
    for attempt in range(MAX_RETRIES):
        status = None
        body = b""
        conn = http.client.HTTPSConnection(HOST, timeout=HTTP_TIMEOUT_S)
        try:
            conn.request("GET", path, headers={
                "Accept": "application/json",
                "User-Agent": "trading-bot-demo-research",
            })
            response = conn.getresponse()
            status = response.status
            body = response.read()
        except (OSError, http.client.HTTPException) as exc:
            last_error = type(exc).__name__
        finally:
            conn.close()
        if status == 200:
            return json.loads(body)
        if status is not None:
            last_error = "http_%d" % status
            if status == 418 or (400 <= status < 500 and status != 429):
                raise RuntimeError("request_rejected_" + last_error)
        time.sleep(min(2 ** attempt, 30))
    raise RuntimeError("request_failed_" + last_error)


def fetch_klines_1h(symbol, start_ms, end_ms):
    bars = []
    cursor = start_ms
    while cursor < end_ms:
        path = (
            "/fapi/v1/klines?symbol=%s&interval=1h&startTime=%d&endTime=%d&limit=%d"
            % (symbol, cursor, end_ms - 1, PAGE_LIMIT)
        )
        rows = http_get_json(path)
        if not rows:
            break
        last_open_time = cursor - H1_MS
        for row in rows:
            open_time = int(row[0])
            if open_time < cursor or open_time >= end_ms:
                continue
            bars.append((
                open_time,
                Decimal(row[1]),
                Decimal(row[2]),
                Decimal(row[3]),
                Decimal(row[4]),
            ))
            last_open_time = open_time
        if last_open_time < cursor:
            break
        cursor = last_open_time + H1_MS
        if len(rows) < PAGE_LIMIT:
            break
        time.sleep(PAGE_PAUSE_S)
    return bars


def count_gaps(bars):
    gaps = 0
    for i in range(1, len(bars)):
        if bars[i][0] - bars[i - 1][0] != H1_MS:
            gaps += 1
    return gaps


def aggregate_4h(bars):
    buckets = {}
    for bar in bars:
        start = (bar[0] // H4_MS) * H4_MS
        buckets.setdefault(start, []).append(bar)
    out = []
    for start in sorted(buckets):
        group = sorted(buckets[start], key=lambda b: b[0])
        if len(group) != 4:
            continue
        out.append((
            start,
            group[0][1],
            max(b[2] for b in group),
            min(b[3] for b in group),
            group[-1][4],
        ))
    return out


def map_h4_index(open_times_1h, h4_starts):
    """Per ogni candela 1h, indice dell'ultima candela 4h gia' chiusa (-1 se nessuna)."""
    out = []
    j = -1
    for open_time in open_times_1h:
        close_time = open_time + H1_MS
        while j + 1 < len(h4_starts) and h4_starts[j + 1] + H4_MS <= close_time:
            j += 1
        out.append(j)
    return out


def prepare_symbol(bars, ema_periods):
    open_times = [b[0] for b in bars]
    opens = [b[1] for b in bars]
    highs = [b[2] for b in bars]
    lows = [b[3] for b in bars]
    closes = [b[4] for b in bars]

    h4 = aggregate_4h(bars)
    h4_closes = [b[4] for b in h4]
    h4_fast = ema(h4_closes, H4_EMA_FAST)
    h4_slow = ema(h4_closes, H4_EMA_SLOW)
    h4_trend = []
    for j in range(len(h4)):
        if h4_fast[j] is None or h4_slow[j] is None:
            h4_trend.append(None)
        elif h4_closes[j] > h4_slow[j] and h4_fast[j] > h4_slow[j]:
            h4_trend.append(1)
        elif h4_closes[j] < h4_slow[j] and h4_fast[j] < h4_slow[j]:
            h4_trend.append(-1)
        else:
            h4_trend.append(0)

    h4_index = map_h4_index(open_times, [b[0] for b in h4])
    trend = [None if j < 0 else h4_trend[j] for j in h4_index]

    return {
        "ots": open_times,
        "opens": opens,
        "highs": highs,
        "lows": lows,
        "closes": closes,
        "atr": atr(highs, lows, closes, ATR_PERIOD),
        "adx": adx(highs, lows, closes, ADX_PERIOD),
        "trend": trend,
        "emas": {period: ema(closes, period) for period in ema_periods},
    }


# ---------------------------------------------------------------- simulazione


def raw_cross(fast, slow, i):
    if i < 1:
        return 0
    f0, s0, f1, s1 = fast[i - 1], slow[i - 1], fast[i], slow[i]
    if f0 is None or s0 is None or f1 is None or s1 is None:
        return 0
    if f0 <= s0 and f1 > s1:
        return 1
    if f0 >= s0 and f1 < s1:
        return -1
    return 0


def size_position(equity, entry_price, stop_distance):
    if equity <= 0 or entry_price <= 0 or stop_distance <= 0:
        return None
    quantity = equity * RISK_PER_TRADE / stop_distance
    max_notional = equity * MAX_LEVERAGE / MAX_POSITIONS
    if quantity * entry_price > max_notional:
        quantity = max_notional / entry_price
    if quantity * entry_price < MIN_NOTIONAL:
        return None
    return quantity


def check_exit(position, bar_open, bar_high, bar_low):
    """Ritorna (prezzo, motivo) oppure (None, None). Se SL e TP cadono nella stessa candela vince lo SL."""
    stop = position["stop"]
    take = position["take"]
    if position["side"] == 1:
        if bar_open <= stop:
            return bar_open, "STOP_GAP"
        if bar_low <= stop:
            return stop, "STOP"
        if take is not None:
            if bar_open >= take:
                return bar_open, "TAKE_GAP"
            if bar_high >= take:
                return take, "TAKE"
    else:
        if bar_open >= stop:
            return bar_open, "STOP_GAP"
        if bar_high >= stop:
            return stop, "STOP"
        if take is not None:
            if bar_open <= take:
                return bar_open, "TAKE_GAP"
            if bar_low <= take:
                return take, "TAKE"
    return None, None


def update_trailing(position, bar_high, bar_low, trail_mult):
    distance = trail_mult * position["atr"]
    if position["side"] == 1:
        if bar_high > position["extreme"]:
            position["extreme"] = bar_high
        candidate = position["extreme"] - distance
        if candidate > position["stop"]:
            position["stop"] = candidate
    else:
        if bar_low < position["extreme"]:
            position["extreme"] = bar_low
        candidate = position["extreme"] + distance
        if candidate < position["stop"]:
            position["stop"] = candidate


def close_position(symbol, position, raw_price, reason, exit_ts):
    side = position["side"]
    quantity = position["quantity"]
    entry_price = position["entry"]
    if side == 1:
        exit_price = raw_price * (1 - SLIPPAGE)
    else:
        exit_price = raw_price * (1 + SLIPPAGE)
    gross = (exit_price - entry_price) * quantity * side
    fees = (entry_price + exit_price) * quantity * FEE_RATE
    return {
        "symbol": symbol,
        "side": side,
        "entry_ts": position["entry_ts"],
        "exit_ts": exit_ts,
        "reason": reason,
        "fees": fees,
        "net_pnl": gross - fees,
    }


def simulate(market, params, start_ms, end_ms, exit_mode):
    fast_period, slow_period, adx_min, exit_mult = params
    funnel = dict.fromkeys(FUNNEL_KEYS, 0)
    equity = INITIAL_EQUITY
    positions = {}
    pending = {}
    trades = []
    last_index = {}

    index_by_ts = {}
    timeline = set()
    for symbol, data in market.items():
        mapping = {}
        for i, open_time in enumerate(data["ots"]):
            if start_ms <= open_time < end_ms:
                mapping[open_time] = i
        index_by_ts[symbol] = mapping
        timeline.update(mapping)

    symbols = sorted(market)
    for ts in sorted(timeline):
        for symbol in symbols:
            i = index_by_ts[symbol].get(ts)
            if i is None:
                continue
            data = market[symbol]
            last_index[symbol] = i
            bar_open = data["opens"][i]
            bar_high = data["highs"][i]
            bar_low = data["lows"][i]

            # 1) ingresso pendente: fill all'open della candela successiva al segnale
            order = pending.pop(symbol, None)
            if order is not None:
                side, atr_value = order
                if len(positions) >= MAX_POSITIONS:
                    funnel["blocked_max_positions"] += 1
                else:
                    if side == 1:
                        entry_price = bar_open * (1 + SLIPPAGE)
                    else:
                        entry_price = bar_open * (1 - SLIPPAGE)
                    stop_distance = SL_ATR * atr_value
                    quantity = size_position(equity, entry_price, stop_distance)
                    if quantity is None:
                        funnel["blocked_sizing"] += 1
                    else:
                        take = None
                        if exit_mode == EXIT_FIXED:
                            take = entry_price + side * exit_mult * atr_value
                        positions[symbol] = {
                            "side": side,
                            "entry": entry_price,
                            "quantity": quantity,
                            "stop": entry_price - side * stop_distance,
                            "take": take,
                            "atr": atr_value,
                            "extreme": entry_price,
                            "entry_ts": ts,
                        }
                        funnel["entries"] += 1

            # 2) gestione posizione aperta
            position = positions.get(symbol)
            if position is not None:
                price, reason = check_exit(position, bar_open, bar_high, bar_low)
                if price is not None:
                    trade = close_position(symbol, position, price, reason, ts)
                    equity += trade["net_pnl"]
                    trades.append(trade)
                    del positions[symbol]
                elif exit_mode == EXIT_TRAILING:
                    update_trailing(position, bar_high, bar_low, exit_mult)

            # 3) segnale sulla candela chiusa
            signal = raw_cross(data["emas"][fast_period], data["emas"][slow_period], i)
            if signal == 0:
                continue
            funnel["raw_signals"] += 1
            if symbol in positions:
                funnel["blocked_in_position"] += 1
                continue
            atr_value = data["atr"][i]
            adx_value = data["adx"][i]
            trend_value = data["trend"][i]
            if atr_value is None or adx_value is None or trend_value is None or atr_value <= 0:
                funnel["blocked_warmup"] += 1
            elif trend_value != signal:
                funnel["blocked_trend"] += 1
            elif adx_value < adx_min:
                funnel["blocked_adx"] += 1
            else:
                pending[symbol] = (signal, atr_value)
                funnel["signals_passed"] += 1

    funnel["pending_discarded"] += len(pending)
    for symbol in sorted(positions):
        i = last_index[symbol]
        data = market[symbol]
        trade = close_position(
            symbol, positions[symbol], data["closes"][i], "WINDOW_END", data["ots"][i]
        )
        trades.append(trade)

    return trades, funnel


# ---------------------------------------------------------------- metriche


def compute_metrics(trades, initial_equity):
    zero = Decimal(0)
    gross_profit = zero
    gross_loss = zero
    fees = zero
    wins = 0
    losses = 0
    equity = initial_equity
    peak = initial_equity
    max_dd_usdt = zero
    max_dd_pct = zero

    for trade in trades:
        pnl = trade["net_pnl"]
        fees += trade["fees"]
        if pnl > 0:
            wins += 1
            gross_profit += pnl
        else:
            losses += 1
            gross_loss += -pnl
        equity += pnl
        if equity > peak:
            peak = equity
        drawdown = peak - equity
        if drawdown > max_dd_usdt:
            max_dd_usdt = drawdown
        if peak > 0:
            drawdown_pct = drawdown / peak * 100
            if drawdown_pct > max_dd_pct:
                max_dd_pct = drawdown_pct

    count = len(trades)
    win_rate = Decimal(wins) / count if count else zero
    if gross_loss > 0:
        profit_factor = gross_profit / gross_loss
    else:
        profit_factor = NO_LOSS_RATIO if gross_profit > 0 else zero
    avg_win = gross_profit / wins if wins else zero
    avg_loss = gross_loss / losses if losses else zero
    if avg_loss > 0:
        payoff_ratio = avg_win / avg_loss
    else:
        payoff_ratio = NO_LOSS_RATIO if avg_win > 0 else zero

    return {
        "trades": count,
        "win_rate": win_rate,
        "profit_factor": profit_factor,
        "payoff_ratio": payoff_ratio,
        "net_pnl_usdt": gross_profit - gross_loss,
        "fees_usdt": fees,
        "max_drawdown_usdt": max_dd_usdt,
        "max_drawdown_pct": max_dd_pct,
    }


def classify_grid_result(funnel, metrics):
    if metrics["trades"] < MIN_IN_SAMPLE_TRADES:
        if funnel["raw_signals"] < MIN_IN_SAMPLE_TRADES:
            return "TOO_FEW_RAW_SIGNALS"
        if funnel["signals_passed"] < MIN_IN_SAMPLE_TRADES:
            return "FILTERS_TOO_STRICT"
        return "CAPACITY_LIMITED"
    if metrics["profit_factor"] <= MIN_IN_SAMPLE_PF:
        return "NO_EDGE_IN_SAMPLE"
    return "QUALIFIED"


def evaluate_verdict(metrics):
    reasons = []
    if metrics["trades"] < OOS_MIN_TRADES:
        reasons.append("TOO_FEW_TRADES")
    if metrics["profit_factor"] < OOS_MIN_PF:
        reasons.append("PROFIT_FACTOR_TOO_LOW")
    if metrics["max_drawdown_pct"] > OOS_MAX_DD_PCT:
        reasons.append("DRAWDOWN_TOO_HIGH")
    if metrics["win_rate"] < OOS_MIN_WIN_RATE and metrics["payoff_ratio"] < OOS_MIN_PAYOFF:
        reasons.append("NO_EDGE_PROFILE")
    return ("ACCEPTED" if not reasons else "REJECTED"), reasons


def metrics_to_json(metrics):
    out = {}
    for key, value in metrics.items():
        out[key] = value if isinstance(value, int) else float(round(value, 4))
    return out


def params_to_json(params):
    return {
        "ema_fast": params[0],
        "ema_slow": params[1],
        "adx_min": float(params[2]),
        "exit_mult": float(params[3]),
    }


# ---------------------------------------------------------------- main


def run(exit_mode):
    now_ms = int(time.time() * 1000)
    end_ms = (now_ms // H1_MS) * H1_MS
    data_start_ms = end_ms - (DAYS + WARMUP_DAYS) * DAY_MS
    ema_periods = sorted({period for combo in GRID for period in combo[:2]})

    market = {}
    for symbol in SYMBOLS:
        bars = fetch_klines_1h(symbol, data_start_ms, end_ms)
        log("data_loaded", symbol=symbol, bars_1h=len(bars), gaps=count_gaps(bars))
        market[symbol] = prepare_symbol(bars, ema_periods)

    diagnosis = {}
    oos_trades = []
    folds_with_candidate = 0

    for fold in range(FOLDS):
        test_start = end_ms - (FOLDS - fold) * TEST_DAYS * DAY_MS
        test_end = test_start + TEST_DAYS * DAY_MS
        train_start = test_start - TRAIN_DAYS * DAY_MS

        best = None
        for params in GRID:
            trades, funnel = simulate(market, params, train_start, test_start, exit_mode)
            metrics = compute_metrics(trades, INITIAL_EQUITY)
            outcome = classify_grid_result(funnel, metrics)
            diagnosis[outcome] = diagnosis.get(outcome, 0) + 1
            log(
                "is_grid_result",
                fold=fold + 1,
                params=params_to_json(params),
                funnel=funnel,
                metrics=metrics_to_json(metrics),
                outcome=outcome,
            )
            if outcome == "QUALIFIED":
                key = (metrics["profit_factor"], metrics["net_pnl_usdt"])
                if best is None or key > best[0]:
                    best = (key, params, metrics)

        if best is None:
            log("fold_result", fold=fold + 1, status="NO_IS_CANDIDATE")
            continue

        folds_with_candidate += 1
        _, params, is_metrics = best
        trades, funnel = simulate(market, params, test_start, test_end, exit_mode)
        oos_metrics = compute_metrics(trades, INITIAL_EQUITY)
        oos_trades.extend(trades)
        log(
            "fold_result",
            fold=fold + 1,
            status="EVALUATED",
            params=params_to_json(params),
            in_sample=metrics_to_json(is_metrics),
            out_of_sample=metrics_to_json(oos_metrics),
            funnel=funnel,
        )

    log("diagnosis_summary", combinations=FOLDS * len(GRID), outcomes=diagnosis)

    summary = compute_metrics(oos_trades, INITIAL_EQUITY)
    verdict, reasons = evaluate_verdict(summary)
    by_symbol = {}
    for trade in oos_trades:
        entry = by_symbol.setdefault(trade["symbol"], {"trades": 0, "net_pnl_usdt": Decimal(0)})
        entry["trades"] += 1
        entry["net_pnl_usdt"] += trade["net_pnl"]
    for entry in by_symbol.values():
        entry["net_pnl_usdt"] = float(round(entry["net_pnl_usdt"], 4))

    log(
        "walk_forward_summary",
        exit_mode=exit_mode,
        folds=FOLDS,
        folds_with_candidate=folds_with_candidate,
        symbols=list(SYMBOLS),
        by_symbol=by_symbol,
        out_of_sample=metrics_to_json(summary),
        reasons=reasons,
        verdict=verdict,
    )
    return verdict


def main(argv):
    exit_mode = EXIT_FIXED
    if len(argv) > 1:
        if argv[1] not in (EXIT_FIXED, EXIT_TRAILING):
            log("usage_error", allowed=[EXIT_FIXED, EXIT_TRAILING])
            return 2
        exit_mode = argv[1]
    run(exit_mode)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
