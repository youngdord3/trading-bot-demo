"""Pianificazione azioni e controlli di rischio (seconda linea dopo il DB)."""
from dataclasses import dataclass, replace
from decimal import Decimal, ROUND_CEILING, ROUND_DOWN, ROUND_UP
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
    enforce_btc_qty_cap: bool = True
    max_quantity: Decimal | None = None
    max_notional_equity_mult: Decimal | None = None
    max_same_side_positions: int | None = None


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


def effective_limits(equity_usdt: Decimal, limits: RiskLimits) -> RiskLimits:
    """Restituisce RiskLimits con max_notional_usdt scalato dinamicamente sull'equity."""
    if limits.max_notional_equity_mult is None:
        return limits
    if limits.max_notional_equity_mult <= 0 or equity_usdt <= 0:
        raise RiskViolation("INVALID_DYNAMIC_NOTIONAL_LIMIT")
    return replace(limits, max_notional_usdt=equity_usdt * limits.max_notional_equity_mult)


def required_initial_margin_usdt(
    notional_usdt: Decimal,
    leverage: int,
    margin_buffer: Decimal = Decimal("1.05"),
) -> Decimal:
    """Calcola il margine iniziale richiesto includendo il cuscinetto per fee e slippage."""
    if leverage <= 0:
        raise RiskViolation(f"INVALID_LEVERAGE: {leverage}")
    return (notional_usdt / Decimal(leverage)) * margin_buffer


def validate_leverage_for_exposure(
    target_leverage: int,
    limits: RiskLimits,
    equity_usdt: Decimal | None = None,
    margin_buffer: Decimal = Decimal("1.10"),
) -> dict:
    """Verifica che target_leverage supporti l'esposizione massima combinata senza errori di margine."""
    if target_leverage <= 0:
        raise RiskViolation(f"INVALID_LEVERAGE: {target_leverage}")
    if limits.max_notional_equity_mult is not None:
        max_combined_mult = Decimal(limits.max_open_positions) * limits.max_notional_equity_mult
        min_required_ratio = max_combined_mult * margin_buffer
        min_leverage = int(min_required_ratio.to_integral_value(rounding=ROUND_CEILING))
        if Decimal(target_leverage) < min_required_ratio:
            raise RiskViolation(
                f"INSUFFICIENT_LEVERAGE_FOR_COMBINED_EXPOSURE: lev={target_leverage}, "
                f"max_combined_mult={max_combined_mult}, min_required={min_leverage}"
            )
        return {
            "target_leverage": target_leverage,
            "max_combined_notional_mult": str(max_combined_mult),
            "min_required_leverage": min_leverage,
        }
    if equity_usdt is not None and equity_usdt > 0:
        max_combined_notional = Decimal(limits.max_open_positions) * limits.max_notional_usdt
        req_margin = required_initial_margin_usdt(max_combined_notional, target_leverage, margin_buffer)
        if req_margin > equity_usdt:
            raise RiskViolation(
                f"INSUFFICIENT_LEVERAGE_FOR_COMBINED_EXPOSURE: lev={target_leverage}, "
                f"max_combined={max_combined_notional}, equity={equity_usdt}"
            )
        return {"target_leverage": target_leverage, "max_combined_notional": str(max_combined_notional)}
    return {"target_leverage": target_leverage}


def safe_leverage_for_stop(
    stop_distance: Decimal,
    price: Decimal,
    target_leverage: int,
    safety_ratio: Decimal = Decimal("0.75"),
) -> int:
    """Riduce la leva ISOLATED se 1.5*ATR/price eccede la soglia di sicurezza rispetto alla liquidazione."""
    if target_leverage <= 1 or stop_distance <= 0 or price <= 0:
        return max(1, target_leverage)
    stop_pct = stop_distance / price
    if stop_pct * Decimal(target_leverage) <= safety_ratio:
        return target_leverage
    max_safe = int(safety_ratio / stop_pct)
    return max(1, min(target_leverage, max_safe))


def size_open_quantity_atr(
    equity_usdt: Decimal,
    risk_fraction: Decimal,
    stop_distance: Decimal,
    price: Decimal,
    f: MarketFilters,
    limits: RiskLimits,
) -> Decimal:
    """Calcola la quantita' su rischio ATR fisso (e.g. 1% equity / 1.5*ATR), arrotondata per difetto."""
    if equity_usdt <= 0 or risk_fraction <= 0:
        raise RiskViolation("INVALID_RISK_BUDGET")
    if stop_distance <= 0:
        raise RiskViolation("INVALID_STOP_DISTANCE")
    if price <= 0:
        raise RiskViolation("INVALID_PRICE")
    eff = effective_limits(equity_usdt, limits)
    risk_usdt = equity_usdt * risk_fraction
    caps = [risk_usdt / stop_distance, eff.max_notional_usdt / price, f.market_max_qty]
    if eff.enforce_btc_qty_cap:
        caps.append(eff.max_quantity_btc)
    if eff.max_quantity is not None:
        caps.append(eff.max_quantity)
    qty = floor_to_step(min(caps), f.market_step_size)
    if qty < f.market_min_qty or qty * price < f.min_notional:
        raise RiskViolation(f"BELOW_MIN_NOTIONAL_OR_QTY: qty={qty}, notional={qty * price}")
    return qty


def validate_open(
    qty: Decimal,
    price: Decimal,
    f: MarketFilters,
    limits: RiskLimits,
    open_positions: int,
    daily_net_pnl_usdt: Decimal,
    *,
    same_side_open_positions: int | None = None,
    side: str | None = None,
    available_margin_usdt: Decimal | None = None,
    leverage: int | None = None,
) -> dict:
    if qty != floor_to_step(qty, f.market_step_size):
        raise RiskViolation(f"QTY_NOT_ON_STEP: {qty}")
    if not f.market_min_qty <= qty <= f.market_max_qty:
        raise RiskViolation(f"QTY_OUT_OF_EXCHANGE_RANGE: {qty}")
    if limits.enforce_btc_qty_cap and qty > limits.max_quantity_btc:
        raise RiskViolation(f"ABOVE_RISK_MAX_QTY: {qty}")
    if limits.max_quantity is not None and qty > limits.max_quantity:
        raise RiskViolation(f"ABOVE_RISK_MAX_QTY: {qty}")
    notional = qty * price
    if notional < f.min_notional:
        raise RiskViolation(f"BELOW_MIN_NOTIONAL: {notional}")
    if notional > limits.max_notional_usdt:
        raise RiskViolation(f"ABOVE_RISK_MAX_NOTIONAL: {notional}")
    if open_positions >= limits.max_open_positions:
        raise RiskViolation(f"MAX_POSITIONS_REACHED: {open_positions}")
    if (
        limits.max_same_side_positions is not None
        and same_side_open_positions is not None
        and same_side_open_positions >= limits.max_same_side_positions
    ):
        tag = f":{side}" if side else ""
        raise RiskViolation(f"MAX_SAME_SIDE_REACHED{tag}: {same_side_open_positions}")
    if daily_net_pnl_usdt <= -limits.max_daily_loss_usdt:
        raise RiskViolation(f"DAILY_LOSS_LIMIT: {daily_net_pnl_usdt}")
    if available_margin_usdt is not None and leverage is not None:
        req_margin = required_initial_margin_usdt(notional, leverage)
        if req_margin > available_margin_usdt:
            raise RiskViolation(
                f"INSUFFICIENT_AVAILABLE_MARGIN: required={req_margin}, available={available_margin_usdt}"
            )
    return {"quantity": str(qty), "notional_usdt": str(notional),
            "open_positions_before": open_positions,
            "daily_net_pnl_usdt": str(daily_net_pnl_usdt), "result": "PASS"}


def validate_close(qty: Decimal, position_qty: Decimal) -> dict:
    if position_qty <= 0 or qty != position_qty:
        raise RiskViolation(f"CLOSE_QTY_MISMATCH: {qty} vs {position_qty}")
    return {"quantity": str(qty), "result": "PASS_CLOSE"}
