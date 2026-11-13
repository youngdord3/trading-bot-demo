"""Report e controlli di coerenza multi-simbolo 1d sul paper trading. Sola lettura."""
import json
import os
import sys
import time
from decimal import Decimal

from common import db_connect
from paper_trader import INTERVAL, INTERVAL_MS, LIMITS, UNIVERSE_SYMBOLS

MAX_LAG_MS = 2 * INTERVAL_MS
EXPECT_RUNNING = os.environ.get("EXPECT_RUNNING", "1") == "1"


def evaluate_report(
    candle_stats_by_symbol: dict[str, tuple[int, int]],
    trades: list[tuple[int, str, str, Decimal, Decimal, Decimal]],
    orphan_trades: int,
    now_ms: int,
    expect_running: bool = EXPECT_RUNNING,
    universe_symbols: tuple[str, ...] = UNIVERSE_SYMBOLS,
) -> dict:
    """Valuta la coerenza di candele, alternanza aperture/chiusure e lag senza I/O."""
    problems: list[str] = []

    positions: dict[str, tuple[str, Decimal]] = {}
    for tid, sym, action, qty, _pnl, _fee in trades:
        if action.startswith("OPEN_"):
            if sym in positions:
                problems.append(f"OPEN_WHILE_POSITION_OPEN id={tid} symbol={sym}")
            positions[sym] = (action[len("OPEN_"):], qty)
        else:
            prev = positions.get(sym)
            if prev is None or prev[0] != action[len("CLOSE_"):] or prev[1] != qty:
                problems.append(f"CLOSE_MISMATCH id={tid} symbol={sym}")
            positions.pop(sym, None)

    if len(positions) > LIMITS.max_open_positions:
        problems.append(f"TOO_MANY_OPEN_POSITIONS={len(positions)}")

    if orphan_trades:
        problems.append(f"TRADES_WITHOUT_PROCESSED_CANDLE={orphan_trades}")

    worst_lag_ms = None
    total_candles = 0
    for sym in universe_symbols:
        last_open, count = candle_stats_by_symbol.get(sym, (0, 0))
        total_candles += count
        if last_open:
            lag_ms = now_ms - (last_open + INTERVAL_MS)
            if worst_lag_ms is None or lag_ms > worst_lag_ms:
                worst_lag_ms = lag_ms
            if expect_running and lag_ms > MAX_LAG_MS:
                problems.append(f"STALE_LAST_CANDLE_LAG_MS symbol={sym} lag={lag_ms}")
        elif expect_running:
            problems.append(f"NO_CANDLES_PROCESSED symbol={sym}")

    net = sum((p - f for _i, _s, _a, _q, p, f in trades), Decimal(0))
    fees = sum((f for _i, _s, _a, _q, _p, f in trades), Decimal(0))
    return {
        "interval": INTERVAL,
        "symbols_expected": len(universe_symbols),
        "symbols_processed": sum(1 for s in universe_symbols if candle_stats_by_symbol.get(s, (0, 0))[1] > 0),
        "candles_processed": total_candles,
        "trades": len(trades),
        "open_positions": {sym: side for sym, (side, _q) in positions.items()},
        "net_pnl_usdt": str(net),
        "fees_usdt": str(fees),
        "last_candle_lag_ms": worst_lag_ms,
        "problems": problems,
        "result": "OK" if not problems else "PROBLEMS",
    }


def main() -> int:
    with db_connect("core_paper_trader", "core_paper_trader_db_password") as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT symbol, COALESCE(MAX(candle_open_time_ms), 0), COUNT(*)
                           FROM bot.paper_processed_candles
                           WHERE interval = %s AND symbol = ANY(%s)
                           GROUP BY symbol""", (INTERVAL, list(UNIVERSE_SYMBOLS)))
            stats = {sym: (int(last_open), int(cnt)) for sym, last_open, cnt in cur.fetchall()}

            cur.execute("""SELECT id, symbol, action, quantity, realized_pnl_usdt, fee_usdt
                           FROM bot.paper_trades ORDER BY symbol, id""")
            trades = cur.fetchall()

            cur.execute("""SELECT COUNT(*) FROM bot.paper_trades t
                           WHERE NOT EXISTS (
                               SELECT 1 FROM bot.paper_processed_candles p
                               WHERE p.symbol = t.symbol
                                 AND p.candle_open_time_ms = t.candle_open_time_ms
                           )""")
            orphan_trades = cur.fetchone()[0]

    report = evaluate_report(stats, trades, orphan_trades, int(time.time() * 1000))
    print(json.dumps(report, indent=2))
    return 0 if report["result"] == "OK" else 1


if __name__ == "__main__":
    sys.exit(main())
