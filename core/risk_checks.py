"""Pianificazione azioni e controlli di rischio (seconda linea dopo il DB)."""
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from enum import Enum

from binance_public import MarketFilters
from strategy import Signal


class Action(Enum):
    OPEN_LONG = "OPEN_LONG"
    CLOSE_LONG = "CLOSE_LONG"
    OPEN_SHORT = "OPEN_SHORT"
    CLOSE_SHORT = "CLOSE_SHORT"


ORDER_SIDE = {Action.OPEN_LONG: "BUY", Action.CLOSE_SHORT: "BUY",
              Action.OPEN_SHORT: "SELL", Action.CLOSE_LONG: "SELL"}
CLOSING = frozenset({Action.CLOSE_LONG, Action.CLOSE_SHORT})


@dataclass(frozen=True)
class RiskLimits:
    max_quantity_btc: Decimal = Decimal("0.01")
    max_notional_usdt: Decimal = Decimal("500")
    max_open_positions: int = 1
    max_daily_loss_usdt: Decimal = Decimal("100")
    notional_buffer: Decimal = Decimal("1.10")   # margine sopra il min_notional


class RiskViolation(Exception):
    pass


def plan_actions(signal: Signal, position_side: str) -> list[Action]:
    """position_side ∈ {'FLAT','LONG','SHORT'}. Strategia stop-and-reverse."""
    if position_side not in ("FLAT", "LONG", "SHORT"):
        raise RiskViolation("INVALID_POSITION_SIDE")
    if signal is Signal.BUY:
        return {"FLAT": [Action.OPEN_LONG],
                "SHORT": [Action.CLOSE_SHORT, Action.OPEN_LONG],
                "LONG": []}[position_side]
    if signal is Signal.SELL:
        return {"FLAT": [Action.OPEN_SHORT],
                "LONG": [Action.CLOSE_LONG, Action.OPEN_SHORT],
                "SHORT": []}[position_side]
    return []


def floor_to_step(q: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        raise RiskViolation("INVALID_STEP_SIZE")
    return (q / step).to_integral_value(rounding=ROUND_DOWN) * step


def ceil_to_step(q: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        raise RiskViolation("INVALID_STEP_SIZE")
    return (q / step).to_integral_value(rounding=ROUND_UP) * step


def size_open_quantity(price: Decimal, f: MarketFilters, limits: RiskLimits) -> Decimal:
    if price <= 0:
        raise RiskViolation("INVALID_PRICE")
    target = f.min_notional * limits.notional_buffer / price
    return ceil_to_step(max(target, f.market_min_qty), f.market_step_size)


def validate_open(qty: Decimal, price: Decimal, f: MarketFilters, limits: RiskLimits,
                  open_positions: int, daily_net_pnl_usdt: Decimal) -> dict:
    if qty != floor_to_step(qty, f.market_step_size):
        raise RiskViolation(f"QTY_NOT_ON_STEP: {qty}")
    if not f.market_min_qty <= qty <= f.market_max_qty:
        raise RiskViolation(f"QTY_OUT_OF_EXCHANGE_RANGE: {qty}")
    if qty > limits.max_quantity_btc:
        raise RiskViolation(f"ABOVE_RISK_MAX_QTY: {qty}")
    notional = qty * price
    if notional < f.min_notional:
        raise RiskViolation(f"BELOW_MIN_NOTIONAL: {notional}")
    if notional > limits.max_notional_usdt:
        raise RiskViolation(f"ABOVE_RISK_MAX_NOTIONAL: {notional}")
    if open_positions >= limits.max_open_positions:
        raise RiskViolation(f"MAX_POSITIONS_REACHED: {open_positions}")
    if daily_net_pnl_usdt <= -limits.max_daily_loss_usdt:
        raise RiskViolation(f"DAILY_LOSS_LIMIT: {daily_net_pnl_usdt}")
    return {"quantity": str(qty), "notional_usdt": str(notional),
            "open_positions_before": open_positions,
            "daily_net_pnl_usdt": str(daily_net_pnl_usdt), "result": "PASS"}


def validate_close(qty: Decimal, position_qty: Decimal) -> dict:
    if position_qty <= 0 or qty != position_qty:
        raise RiskViolation(f"CLOSE_QTY_MISMATCH: {qty} vs {position_qty}")
    return {"quantity": str(qty), "result": "PASS_CLOSE"}
