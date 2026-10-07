"""Backtest offline. Nessun accesso a DB o account. Dati: mainnet pubblico (default) o demo."""
import json
import os
import sys
import time
from decimal import Decimal

from binance_public import DEMO_HOST, MAINNET_DATA_HOST, PublicClient
from risk_checks import CLOSING, ORDER_SIDE, plan_actions
from strategy import Signal, evaluate_crossover, parse_closed_candles

SYMBOL = "BTCUSDT"
INTERVAL, INTERVAL_MS = "15m", 900_000
WINDOW = 200
DAYS = int(os.environ.get("BACKTEST_DAYS", "90"))
QTY = Decimal(os.environ.get("BACKTEST_QTY", "0.002"))
FEE_RATE = Decimal(os.environ.get("BACKTEST_FEE", "0.0005"))      # taker per lato
SLIPPAGE = Decimal(os.environ.get("BACKTEST_SLIPPAGE", "0.0002"))
HOST = MAINNET_DATA_HOST if os.environ.get("BACKTEST_DATA", "mainnet") == "mainnet" else DEMO_HOST


def fetch_history(client):
    end = client.get_server_time_ms()
    cursor = end - DAYS * 86_400_000
    raw = []
    while cursor < end:
        batch = client.get_klines(SYMBOL, INTERVAL, 1500, start_time_ms=cursor)
        if not batch:
            break
        raw.extend(batch)
        nxt = int(batch[-1][0]) + INTERVAL_MS
        if nxt <= cursor:
            break
        cursor = nxt
        time.sleep(0.25)   # rispetto dei rate limit
    return parse_closed_candles(raw, end, INTERVAL_MS, require_contiguous=False)


def main():
    candles = fetch_history(PublicClient(HOST))
    gaps = sum(1 for a, b in zip(candles, candles[1:])
               if b.open_time_ms - a.open_time_ms != INTERVAL_MS)
    side, entry = "FLAT", None
    net, peak, max_dd, fees = Decimal(0), Decimal(0), Decimal(0), Decimal(0)
    wins, losses, gross_win, gross_loss = 0, 0, Decimal(0), Decimal(0)

    for i in range(WINDOW - 1, len(candles) - 1):
        window = [c.close for c in candles[i - WINDOW + 1:i + 1]]
        sig = evaluate_crossover(window)
        if sig is Signal.HOLD:
            continue
        base = candles[i + 1].open
        for action in plan_actions(sig, side):
            px = base * (1 + SLIPPAGE) if ORDER_SIDE[action] == "BUY" else base * (1 - SLIPPAGE)
            fee = px * QTY * FEE_RATE
            fees += fee
            net -= fee
            if action in CLOSING:
                pnl = (px - entry) * QTY if side == "LONG" else (entry - px) * QTY
                net += pnl
                if pnl - fee > 0:
                    wins += 1; gross_win += pnl - fee
                else:
                    losses += 1; gross_loss += -(pnl - fee)
                side, entry = "FLAT", None
            else:
                side, entry = ("LONG" if action.value == "OPEN_LONG" else "SHORT"), px
            peak = max(peak, net)
            max_dd = max(max_dd, peak - net)

    first, last = candles[WINDOW - 1].close, candles[-1].close
    rt = wins + losses
    print(json.dumps({
        "data_host": HOST, "days": DAYS, "candles": len(candles), "gaps": gaps,
        "round_trips": rt, "wins": wins, "losses": losses,
        "win_rate_pct": f"{wins / rt * 100:.1f}" if rt else "N/A",
        "profit_factor": f"{gross_win / gross_loss:.2f}" if gross_loss > 0 else "N/A",
        "net_pnl_usdt": str(net.quantize(Decimal("0.01"))),
        "fees_usdt": str(fees.quantize(Decimal("0.01"))),
        "max_drawdown_usdt": str(max_dd.quantize(Decimal("0.01"))),
        "buy_and_hold_usdt": str(((last - first) * QTY).quantize(Decimal("0.01"))),
        "open_position_at_end": side,
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
