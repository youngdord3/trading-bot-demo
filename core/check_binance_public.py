import http.client
import json
import ssl
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from urllib.error import HTTPError, URLError
from urllib.request import (
    HTTPRedirectHandler,
    ProxyHandler,
    Request,
    build_opener,
)

from app import read_database_status

BASE_URL = "https://demo-fapi.binance.com"
ALLOWED_PATHS = {
    "/fapi/v1/time",
    "/fapi/v1/exchangeInfo",
}
MAX_RESPONSE_BYTES = 5_000_000

class CheckFailed(Exception):
    pass

class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

# No implicit proxy configuration and no redirects to another host.
OPENER = build_opener(ProxyHandler({}), NoRedirects())

def public_get(path):
    if path not in ALLOWED_PATHS:
        raise CheckFailed("ENDPOINT_NOT_ALLOWED")

    request = Request(
        BASE_URL + path,
        method="GET",
        headers={
            "Accept": "application/json",
            "User-Agent": "TradingCore-Demo-ReadOnly/0.1",
        },
    )

    try:
        with OPENER.open(request, timeout=10) as response:
            if response.status != 200:
                raise CheckFailed("UNEXPECTED_HTTP_STATUS")

            raw = response.read(MAX_RESPONSE_BYTES + 1)

    except HTTPError as exc:
        # Do not print raw response bodies or retry automatically.
        raise CheckFailed(f"BINANCE_HTTP_{exc.code}") from None
    except (URLError, TimeoutError, OSError):
        raise CheckFailed("BINANCE_NETWORK_OR_TLS_ERROR") from None

    if len(raw) > MAX_RESPONSE_BYTES:
        raise CheckFailed("RESPONSE_TOO_LARGE")

    try:
        data = json.loads(raw)
    except (ValueError, UnicodeError):
        raise CheckFailed("INVALID_JSON") from None

    if not isinstance(data, dict):
        raise CheckFailed("INVALID_RESPONSE_SCHEMA")

    if "code" in data:
        raise CheckFailed("BINANCE_ERROR_RESPONSE")

    return data

def measure_clock():
    host = "demo-fapi.binance.com"

    connection = http.client.HTTPSConnection(
        host,
        port=443,
        timeout=10,
        context=ssl.create_default_context(),
    )

    samples = []

    def fetch_server_time(expected_socket):
        connection.request(
            "GET",
            "/fapi/v1/time",
            headers={
                "Accept": "application/json",
                "User-Agent": "TradingCore-Demo-ReadOnly/0.2",
                "Connection": "keep-alive",
            },
        )

        # Reject an automatic reconnection during a measured request.
        if connection.sock is not expected_socket:
            raise CheckFailed("CLOCK_CONNECTION_CHANGED")

        response = connection.getresponse()

        if response.status != 200:
            raise CheckFailed(
                f"BINANCE_CLOCK_HTTP_{response.status}"
            )

        if response.will_close:
            raise CheckFailed("CLOCK_KEEPALIVE_UNAVAILABLE")

        raw = response.read(65_537)

        if len(raw) > 65_536:
            raise CheckFailed("CLOCK_RESPONSE_TOO_LARGE")

        if connection.sock is not expected_socket:
            raise CheckFailed("CLOCK_CONNECTION_CHANGED")

        try:
            data = json.loads(raw)
        except (ValueError, UnicodeError):
            raise CheckFailed("INVALID_CLOCK_JSON") from None

        if not isinstance(data, dict) or "code" in data:
            raise CheckFailed("INVALID_CLOCK_RESPONSE")

        server_ms = data.get("serverTime")

        if type(server_ms) is not int or server_ms <= 0:
            raise CheckFailed("INVALID_SERVER_TIME")

        return server_ms

    try:
        # DNS, TCP and TLS setup are outside the timed samples.
        connection.connect()
        original_socket = connection.sock

        if original_socket is None:
            raise CheckFailed("CLOCK_CONNECTION_NOT_ESTABLISHED")

        # Warm-up request, deliberately excluded from the measurements.
        fetch_server_time(original_socket)

        for index in range(5):
            wall_start = time.time_ns() / 1_000_000
            monotonic_start = time.monotonic_ns()

            server_ms = fetch_server_time(original_socket)

            monotonic_end = time.monotonic_ns()
            wall_end = time.time_ns() / 1_000_000

            rtt_ms = (
                monotonic_end - monotonic_start
            ) / 1_000_000

            if abs((wall_end - wall_start) - rtt_ms) > 100:
                raise CheckFailed(
                    "LOCAL_CLOCK_CHANGED_DURING_TEST"
                )

            midpoint_ms = (wall_start + wall_end) / 2

            samples.append({
                "rtt_ms": rtt_ms,
                "offset_ms": server_ms - midpoint_ms,
            })

            if index < 4:
                time.sleep(0.2)

    except CheckFailed:
        raise
    except (
        OSError,
        http.client.HTTPException,
    ):
        raise CheckFailed(
            "CLOCK_NETWORK_OR_TLS_ERROR"
        ) from None
    finally:
        connection.close()

    best = min(samples, key=lambda sample: sample["rtt_ms"])

    uncertainty_ms = best["rtt_ms"] / 2
    conservative_bound_ms = (
        abs(best["offset_ms"]) + uncertainty_ms
    )

    return {
        "measurement_method": "PERSISTENT_HTTPS",
        "connection_reused": True,
        "samples": len(samples),
        "best_rtt_ms": round(best["rtt_ms"], 2),
        "estimated_offset_ms": round(best["offset_ms"], 2),
        "approximate_uncertainty_ms": round(
            uncertainty_ms, 2
        ),
        "conservative_bound_ms": round(
            conservative_bound_ms, 2
        ),
        "latency_ok": best["rtt_ms"] <= 1000,
        "clock_ok": conservative_bound_ms <= 500,
        "sample_details": [
            {
                "rtt_ms": round(sample["rtt_ms"], 2),
                "estimated_offset_ms": round(
                    sample["offset_ms"], 2
                ),
            }
            for sample in samples
        ],
    }

def positive_decimal(filters, filter_type, field):
    try:
        raw = filters[filter_type][field]

        if not isinstance(raw, str):
            raise ValueError

        number = Decimal(raw)

        if not number.is_finite() or number <= 0:
            raise ValueError

        return raw

    except (KeyError, TypeError, ValueError, InvalidOperation):
        raise CheckFailed(
            f"INVALID_FILTER_{filter_type}_{field}"
        ) from None

def read_symbol():
    data = public_get("/fapi/v1/exchangeInfo")
    symbols = data.get("symbols")

    if not isinstance(symbols, list):
        raise CheckFailed("INVALID_SYMBOL_LIST")

    matches = [
        symbol for symbol in symbols
        if isinstance(symbol, dict)
        and symbol.get("symbol") == "BTCUSDT"
    ]

    if len(matches) != 1:
        raise CheckFailed("BTCUSDT_NOT_UNIQUELY_FOUND")

    symbol = matches[0]
    raw_filters = symbol.get("filters")

    if not isinstance(raw_filters, list):
        raise CheckFailed("INVALID_FILTER_LIST")

    filters = {}

    for item in raw_filters:
        if not isinstance(item, dict):
            raise CheckFailed("INVALID_FILTER")

        name = item.get("filterType")

        if not isinstance(name, str) or name in filters:
            raise CheckFailed("INVALID_OR_DUPLICATE_FILTER")

        filters[name] = item

    values = {
        "tick_size": positive_decimal(
            filters, "PRICE_FILTER", "tickSize"
        ),
        "lot_step_size": positive_decimal(
            filters, "LOT_SIZE", "stepSize"
        ),
        "lot_min_qty": positive_decimal(
            filters, "LOT_SIZE", "minQty"
        ),
        "lot_max_qty": positive_decimal(
            filters, "LOT_SIZE", "maxQty"
        ),
        "market_step_size": positive_decimal(
            filters, "MARKET_LOT_SIZE", "stepSize"
        ),
        "market_min_qty": positive_decimal(
            filters, "MARKET_LOT_SIZE", "minQty"
        ),
        "market_max_qty": positive_decimal(
            filters, "MARKET_LOT_SIZE", "maxQty"
        ),
        "min_notional": positive_decimal(
            filters, "MIN_NOTIONAL", "notional"
        ),
    }

    for prefix in ("lot", "market"):
        if Decimal(values[f"{prefix}_min_qty"]) > Decimal(
            values[f"{prefix}_max_qty"]
        ):
            raise CheckFailed("INVALID_QUANTITY_RANGE")

    return {
        "symbol": symbol["symbol"],
        "status": symbol.get("status"),
        "contract_type": symbol.get("contractType"),
        "quote_asset": symbol.get("quoteAsset"),
        "margin_asset": symbol.get("marginAsset"),
        "filters": values,
    }

def main():
    try:
        database = read_database_status()

        if not database["ready"]:
            raise CheckFailed("DATABASE_BASELINE_NOT_READY")

        clock = measure_clock()
        symbol = read_symbol()

        checks = {
            "database_baseline_ok": True,
            "demo_public_api_reachable": True,
            "latency_ok": clock["latency_ok"],
            "clock_ok": clock["clock_ok"],
            "symbol_trading": symbol["status"] == "TRADING",
            "perpetual_contract": (
                symbol["contract_type"] == "PERPETUAL"
            ),
            "usdt_quote": symbol["quote_asset"] == "USDT",
            "usdt_margin": symbol["margin_asset"] == "USDT",
            "required_filters_valid": True,
        }

        passed = all(checks.values())

        result = {
            "phase": "BINANCE_PUBLIC_READ_ONLY",
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "environment": "DEMO",
            "base_url": BASE_URL,
            "passed": passed,
            "checks": checks,
            "clock": clock,
            "market": symbol,
            "account_connected": False,
            "orders_enabled": False,
            "trading_ready": False,
        }

        print(json.dumps(result, indent=2))
        return 0 if passed else 1

    except CheckFailed as exc:
        error = str(exc)
    except Exception:
        # Avoid leaking connection details or secrets.
        error = "LOCAL_CHECK_FAILED"

    print(json.dumps({
        "phase": "BINANCE_PUBLIC_READ_ONLY",
        "passed": False,
        "error": error,
        "orders_enabled": False,
        "trading_ready": False,
    }, indent=2))

    return 1

if __name__ == "__main__":
    sys.exit(main())
