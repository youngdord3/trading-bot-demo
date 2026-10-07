"""Report e controlli di coerenza sul paper trading. Sola lettura."""
import json
import os
import sys
import time
from decimal import Decimal

from common import db_connect

SYMBOL = "BTCUSDT"
INTERVAL, INTERVAL_MS = "15m", 900_000
MAX_LAG_MS = 2 * INTERVAL_MS
EXPECT_RUNNING = os.environ.get("EXPECT_RUNNING", "1") == "1"


def main() -> int:
    problems = []
    with db_connect("core_paper_trader", "core_paper_trader_db_password") as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT COALESCE(MAX(candle_open_time_ms), 0), COUNT(*)
                           FROM bot.paper_processed_candles
                           WHERE symbol = %s AND interval = %s""", (SYMBOL, INTERVAL))
            last_open, n_candles = cur.fetchone()

            cur.execute("""SELECT id, action, quantity, realized_pnl_usdt, fee_usdt
                           FROM bot.paper_trades WHERE symbol = %s ORDER BY id""", (SYMBOL,))
            trades = cur.fetchall()

            cur.execute("""SELECT COUNT(*) FROM bot.paper_trades t
                           LEFT JOIN bot.paper_processed_candles p
                             ON p.symbol = t.symbol AND p.interval = %s
                            AND p.candle_open_time_ms = t.candle_open_time_ms
                           WHERE t.symbol = %s AND p.symbol IS NULL""", (INTERVAL, SYMBOL))
            orphan_trades = cur.fetchone()[0]

    # Le operazioni devono alternare apertura e chiusura della stessa direzione e quantità.
    pos = None
    for tid, action, qty, _pnl, _fee in trades:
        if action.startswith("OPEN_"):
            if pos is not None:
                problems.append(f"OPEN_WHILE_POSITION_OPEN id={tid}")
            pos = (action[len("OPEN_"):], qty)
        else:
            if pos is None or pos[0] != action[len("CLOSE_"):] or pos[1] != qty:
                problems.append(f"CLOSE_MISMATCH id={tid}")
            pos = None

    if orphan_trades:
        problems.append(f"TRADES_WITHOUT_PROCESSED_CANDLE={orphan_trades}")

    lag_ms = None
    if last_open:
        lag_ms = int(time.time() * 1000) - (last_open + INTERVAL_MS)
        if EXPECT_RUNNING and lag_ms > MAX_LAG_MS:
            problems.append(f"STALE_LAST_CANDLE_LAG_MS={lag_ms}")
    elif EXPECT_RUNNING:
        problems.append("NO_CANDLES_PROCESSED")

    net = sum((p - f for _i, _a, _q, p, f in trades), Decimal(0))
    fees = sum((f for _i, _a, _q, _p, f in trades), Decimal(0))
    print(json.dumps({
        "candles_processed": n_candles,
        "trades": len(trades),
        "open_position": pos[0] if pos else "FLAT",
        "net_pnl_usdt": str(net),
        "fees_usdt": str(fees),
        "last_candle_lag_ms": lag_ms,
        "problems": problems,
        "result": "OK" if not problems else "PROBLEMS",
    }, indent=2))
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())
