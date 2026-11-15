import json
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from app import read_database_status
from binance_private import PrivateReadError, ReadOnlyClient
from strategy import UNIVERSE_SYMBOLS

TARGET_LEVERAGE = int(os.environ.get("TARGET_LEVERAGE", "2"))


class CheckFailed(Exception):
    pass


def parse_decimal(value, default="0"):
    try:
        if value is None:
            return Decimal(default)
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(default)


def evaluate_account_snapshot(
    account_data: dict,
    dual_data: dict,
    sym_configs: list,
    positions: list,
    open_orders: list,
    algo_orders: list,
    target_leverage: int = TARGET_LEVERAGE,
    symbols: tuple[str, ...] = UNIVERSE_SYMBOLS,
) -> dict:
    """Valuta la configurazione Binance Futures sull'intero paniere di simboli senza I/O."""
    one_way_mode = dual_data.get("dualSidePosition") is False
    total_margin_balance = parse_decimal(account_data.get("totalMarginBalance", "0"))
    available_balance = parse_decimal(account_data.get("availableBalance", "0"))
    equity_positive = total_margin_balance > 0

    cfg_by_sym = {
        str(item.get("symbol", "")): item
        for item in sym_configs
        if isinstance(item, dict)
    }
    non_isolated: list[str] = []
    wrong_leverage: list[str] = []
    for sym in symbols:
        cfg = cfg_by_sym.get(sym, {})
        if str(cfg.get("marginType", "")).upper() != "ISOLATED":
            non_isolated.append(sym)
        try:
            lev = int(cfg.get("leverage", 0))
        except (ValueError, TypeError):
            lev = 0
        if lev != target_leverage:
            wrong_leverage.append(sym)

    open_positions = [
        p for p in positions
        if isinstance(p, dict) and parse_decimal(p.get("positionAmt", "0")) != 0
    ]

    checks = {
        "database_baseline_ok": True,
        "one_way_mode": one_way_mode,
        "all_symbols_isolated": len(non_isolated) == 0,
        "all_symbols_target_leverage": len(wrong_leverage) == 0,
        "no_open_positions": len(open_positions) == 0,
        "no_open_orders": len(open_orders) == 0,
        "no_open_algo_orders": len(algo_orders) == 0,
        "equity_positive": equity_positive,
    }
    passed = all(checks.values())
    return {
        "phase": "BINANCE_ACCOUNT_READ_ONLY",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "environment": "DEMO",
        "passed": passed,
        "checks": checks,
        "non_isolated_symbols": non_isolated,
        "wrong_leverage_symbols": wrong_leverage,
        "target_leverage": target_leverage,
        "account_summary": {
            "total_equity_usdt": str(total_margin_balance),
            "available_margin_usdt": str(available_balance),
            "position_mode": "ONE_WAY" if one_way_mode else "HEDGE",
            "symbols_checked": len(symbols),
            "open_positions_count": len(open_positions),
            "open_orders_count": len(open_orders),
            "open_algo_orders_count": len(algo_orders),
        },
        "orders_enabled": False,
        "trading_ready": False,
    }


def main():
    try:
        database = read_database_status()
        if not database.get("ready"):
            raise CheckFailed("DATABASE_BASELINE_NOT_READY")

        client = ReadOnlyClient()
        client.sync_time_offset()

        dual_data = client.get("/fapi/v1/positionSide/dual")
        if not isinstance(dual_data, dict) or "dualSidePosition" not in dual_data:
            raise CheckFailed("INVALID_POSITION_SIDE_DUAL_RESPONSE")

        sym_config_data = client.get("/fapi/v1/symbolConfig")
        if not isinstance(sym_config_data, list):
            raise CheckFailed("INVALID_SYMBOL_CONFIG_RESPONSE")

        pos_data = client.get("/fapi/v3/positionRisk")
        if not isinstance(pos_data, list):
            raise CheckFailed("INVALID_POSITION_RISK_RESPONSE")

        account_data = client.get("/fapi/v3/account")
        if not isinstance(account_data, dict):
            raise CheckFailed("INVALID_ACCOUNT_RESPONSE")

        open_orders_data = client.get("/fapi/v1/openOrders")
        if not isinstance(open_orders_data, list):
            raise CheckFailed("INVALID_OPEN_ORDERS_RESPONSE")

        open_algo_data = client.get("/fapi/v1/openAlgoOrders")
        if isinstance(open_algo_data, list):
            algo_orders_list = open_algo_data
        elif isinstance(open_algo_data, dict) and "orders" in open_algo_data:
            algo_orders_list = open_algo_data["orders"]
        elif isinstance(open_algo_data, dict) and open_algo_data.get("total") == 0:
            algo_orders_list = []
        else:
            algo_orders_list = []

        result = evaluate_account_snapshot(
            account_data=account_data,
            dual_data=dual_data,
            sym_configs=sym_config_data,
            positions=pos_data,
            open_orders=open_orders_data,
            algo_orders=algo_orders_list,
            target_leverage=TARGET_LEVERAGE,
            symbols=UNIVERSE_SYMBOLS,
        )
        result["orders_enabled"] = database.get("orders_enabled", False)
        result["trading_ready"] = database.get("trading_ready", False)
        print(json.dumps(result, indent=2))
        return 0 if result["passed"] else 1

    except CheckFailed as exc:
        error = str(exc)
    except PrivateReadError as exc:
        error = str(exc)
    except Exception as exc:
        error = f"CHECK_EXECUTION_ERROR_{type(exc).__name__}"

    print(json.dumps({
        "phase": "BINANCE_ACCOUNT_READ_ONLY",
        "passed": False,
        "error": error,
        "orders_enabled": False,
        "trading_ready": False,
    }, indent=2))
    return 1


if __name__ == "__main__":
    sys.exit(main())
