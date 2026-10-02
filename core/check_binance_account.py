import json
import sys
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from app import read_database_status
from binance_private import PrivateReadError, ReadOnlyClient

class CheckFailed(Exception):
    pass

def parse_decimal(value, default="0"):
    try:
        if value is None:
            return Decimal(default)
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(default)

def main():
    try:
        # 1. Verifica stato iniziale database
        database = read_database_status()
        if not database.get("ready"):
            raise CheckFailed("DATABASE_BASELINE_NOT_READY")

        # 2. Inizializzazione client firmato di sola lettura
        client = ReadOnlyClient()

        # 3. Lettura modalità posizione (One-way vs Dual/Hedge)
        dual_data = client.get("/fapi/v1/positionSide/dual")
        if not isinstance(dual_data, dict) or "dualSidePosition" not in dual_data:
            raise CheckFailed("INVALID_POSITION_SIDE_DUAL_RESPONSE")
        # In Binance Futures: dualSidePosition == False significa One-way mode
        one_way_mode = dual_data["dualSidePosition"] is False

        # 4. Lettura configurazione simbolo BTCUSDT
        sym_config_data = client.get("/fapi/v1/symbolConfig", {"symbol": "BTCUSDT"})
        if isinstance(sym_config_data, list) and len(sym_config_data) > 0:
            sym_config = sym_config_data[0]
        elif isinstance(sym_config_data, dict):
            sym_config = sym_config_data
        else:
            sym_config = {}

        # 5. Lettura rischio posizione per BTCUSDT
        pos_data = client.get("/fapi/v3/positionRisk", {"symbol": "BTCUSDT"})
        if not isinstance(pos_data, list):
            raise CheckFailed("INVALID_POSITION_RISK_RESPONSE")

        # 6. Lettura saldo ed equity account
        account_data = client.get("/fapi/v3/account")
        if not isinstance(account_data, dict):
            raise CheckFailed("INVALID_ACCOUNT_RESPONSE")

        # 7. Lettura ordini aperti ordinari
        open_orders_data = client.get("/fapi/v1/openOrders", {"symbol": "BTCUSDT"})
        if not isinstance(open_orders_data, list):
            raise CheckFailed("INVALID_OPEN_ORDERS_RESPONSE")

        # 8. Lettura ordini aperti algo
        open_algo_data = client.get("/fapi/v1/openAlgoOrders", {"symbol": "BTCUSDT"})
        if isinstance(open_algo_data, list):
            algo_orders_list = open_algo_data
        elif isinstance(open_algo_data, dict) and "orders" in open_algo_data:
            algo_orders_list = open_algo_data["orders"]
        elif isinstance(open_algo_data, dict) and open_algo_data.get("total") == 0:
            algo_orders_list = []
        else:
            algo_orders_list = []

        # Analisi dei controlli
        # Equity positiva
        total_margin_balance = parse_decimal(account_data.get("totalMarginBalance", "0"))
        available_balance = parse_decimal(account_data.get("availableBalance", "0"))
        equity_positive = total_margin_balance > 0

        # Posizioni aperte per BTCUSDT
        open_positions = []
        isolated_margin_flags = []
        leverage_values = []

        for p in pos_data:
            if not isinstance(p, dict):
                continue
            amt = parse_decimal(p.get("positionAmt", "0"))
            if amt != 0:
                open_positions.append(p)

            # Verifica margine isolato (sia booleano che stringa)
            is_isolated = p.get("isolated") is True or str(p.get("marginType", "")).lower() == "isolated"
            isolated_margin_flags.append(is_isolated)

            # Verifica leva
            try:
                lev = int(p.get("leverage", 0))
                leverage_values.append(lev)
            except (ValueError, TypeError):
                pass

        # Margine isolato da symbolConfig o da positionRisk
        config_margin_type = str(sym_config.get("marginType", "")).upper()
        if config_margin_type:
            isolated_margin = (config_margin_type == "ISOLATED")
        else:
            isolated_margin = all(isolated_margin_flags) if isolated_margin_flags else False

        # Leva 1x da symbolConfig o da positionRisk
        try:
            config_leverage = int(sym_config.get("leverage", 0))
        except (ValueError, TypeError):
            config_leverage = 0

        if config_leverage > 0:
            leverage_1x = (config_leverage == 1)
        else:
            leverage_1x = all(lev == 1 for lev in leverage_values) if leverage_values else False

        no_open_positions = (len(open_positions) == 0)
        no_open_orders = (len(open_orders_data) == 0)
        no_open_algo_orders = (len(algo_orders_list) == 0)

        checks = {
            "database_baseline_ok": True,
            "one_way_mode": one_way_mode,
            "isolated_margin": isolated_margin,
            "leverage_1x": leverage_1x,
            "no_open_positions": no_open_positions,
            "no_open_orders": no_open_orders,
            "no_open_algo_orders": no_open_algo_orders,
            "equity_positive": equity_positive,
        }

        passed = all(checks.values())

        result = {
            "phase": "BINANCE_ACCOUNT_READ_ONLY",
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "environment": "DEMO",
            "passed": passed,
            "checks": checks,
            "account_summary": {
                "total_equity_usdt": str(total_margin_balance),
                "available_margin_usdt": str(available_balance),
                "position_mode": "ONE_WAY" if one_way_mode else "HEDGE",
                "btcusdt_margin_type": "ISOLATED" if isolated_margin else "CROSSED",
                "btcusdt_leverage": config_leverage or (leverage_values[0] if leverage_values else 0),
                "open_positions_count": len(open_positions),
                "open_orders_count": len(open_orders_data),
                "open_algo_orders_count": len(algo_orders_list),
            },
            "orders_enabled": False,
            "trading_ready": False,
        }

        print(json.dumps(result, indent=2))
        return 0 if passed else 1

    except CheckFailed as exc:
        error = str(exc)
    except PrivateReadError as exc:
        error = str(exc)
    except Exception as exc:
        # Non esporre segreti o traceback sensibili
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
