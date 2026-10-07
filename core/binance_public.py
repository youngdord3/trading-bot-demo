"""Client pubblico Binance Futures — solo GET, whitelist di host e path."""
import json
import ssl
import time
from dataclasses import dataclass
from decimal import Decimal
from http.client import HTTPSConnection
from urllib.parse import urlencode

DEMO_HOST = "demo-fapi.binance.com"
MAINNET_DATA_HOST = "fapi.binance.com"   # SOLO dati storici per backtest
ALLOWED_HOSTS = frozenset({DEMO_HOST, MAINNET_DATA_HOST})
ALLOWED_PATHS = frozenset({
    "/fapi/v1/time",
    "/fapi/v1/exchangeInfo",
    "/fapi/v1/klines",
    "/fapi/v1/ticker/price",
})
TIMEOUT_S = 10
MAX_RESPONSE_BYTES = 8_000_000   # exchangeInfo è grande
MAX_KLINES_LIMIT = 1500


class PublicAPIError(Exception):
    def __init__(self, code: str, binance_code=None):
        super().__init__(code if binance_code is None else f"{code}_BINANCE_{binance_code}")
        self.code = code
        self.binance_code = binance_code


@dataclass(frozen=True)
class MarketFilters:
    tick_size: Decimal
    market_step_size: Decimal   # MARKET_LOT_SIZE: vale per ordini MARKET
    market_min_qty: Decimal
    market_max_qty: Decimal
    min_notional: Decimal


class PublicClient:
    def __init__(self, host: str = DEMO_HOST):
        if host not in ALLOWED_HOSTS:
            raise PublicAPIError("HOST_NOT_ALLOWED")
        self.host = host
        self._ctx = ssl.create_default_context()
        self._ctx.minimum_version = ssl.TLSVersion.TLSv1_2

    def get(self, path: str, params: dict | None = None):
        if path not in ALLOWED_PATHS:
            raise PublicAPIError("ENDPOINT_NOT_ALLOWED")
        query = urlencode(params or {})
        target = f"{path}?{query}" if query else path
        conn = HTTPSConnection(self.host, 443, timeout=TIMEOUT_S, context=self._ctx)
        try:
            conn.request("GET", target, headers={
                "Accept": "application/json",
                "User-Agent": "TradingCore-Demo-Public/0.3",
            })
            resp = conn.getresponse()
            status = resp.status
            raw = resp.read(MAX_RESPONSE_BYTES + 1)
        except (OSError, TimeoutError):   # ssl.SSLError è sottoclasse di OSError
            raise PublicAPIError("NETWORK_OR_TLS_ERROR") from None
        finally:
            conn.close()

        if len(raw) > MAX_RESPONSE_BYTES:
            raise PublicAPIError("RESPONSE_TOO_LARGE")
        try:
            data = json.loads(raw)
        except ValueError:
            raise PublicAPIError(f"INVALID_JSON_HTTP_{status}") from None
        if status != 200:
            bc = data.get("code") if isinstance(data, dict) else None
            raise PublicAPIError(f"HTTP_{status}", bc)
        if isinstance(data, dict) and isinstance(data.get("code"), int) and data["code"] < 0:
            raise PublicAPIError("BINANCE_ERROR", data["code"])
        return data

    def get_server_time_ms(self) -> int:
        data = self.get("/fapi/v1/time")
        if isinstance(data, dict) and type(data.get("serverTime")) is int:
            return data["serverTime"]
        raise PublicAPIError("INVALID_TIME_RESPONSE")

    def measure_time_offset_ms(self, samples: int = 3) -> tuple[int, int]:
        """Ritorna (offset_ms, rtt_ms) prendendo il campione con RTT minimo.
        server_now ≈ local_now + offset."""
        best = None
        for _ in range(samples):
            t0 = time.time() * 1000
            st = self.get_server_time_ms()
            t1 = time.time() * 1000
            rtt = t1 - t0
            offset = int(st - (t0 + t1) / 2)
            if best is None or rtt < best[0]:
                best = (rtt, offset)
        return best[1], int(best[0])

    def get_klines(self, symbol: str, interval: str, limit: int,
                   start_time_ms: int | None = None) -> list:
        if not 1 <= limit <= MAX_KLINES_LIMIT:
            raise PublicAPIError("INVALID_LIMIT")
        params = {"symbol": symbol, "interval": interval, "limit": limit}
        if start_time_ms is not None:
            params["startTime"] = start_time_ms
        data = self.get("/fapi/v1/klines", params)
        if not isinstance(data, list):
            raise PublicAPIError("INVALID_KLINES_RESPONSE")
        return data

    def get_price(self, symbol: str) -> Decimal:
        data = self.get("/fapi/v1/ticker/price", {"symbol": symbol})
        if isinstance(data, dict) and "price" in data:
            price = Decimal(str(data["price"]))
            if price > 0:
                return price
        raise PublicAPIError("INVALID_PRICE_RESPONSE")

    def load_filters(self, symbol: str) -> MarketFilters:
        data = self.get("/fapi/v1/exchangeInfo")
        symbols = data.get("symbols", []) if isinstance(data, dict) else []
        matches = [s for s in symbols if isinstance(s, dict) and s.get("symbol") == symbol]
        if len(matches) != 1:
            raise PublicAPIError("SYMBOL_NOT_UNIQUE")
        s = matches[0]
        if s.get("status") != "TRADING" or s.get("contractType") != "PERPETUAL":
            raise PublicAPIError("SYMBOL_NOT_TRADABLE")
        f = {x["filterType"]: x for x in s.get("filters", [])
             if isinstance(x, dict) and "filterType" in x}
        try:
            return MarketFilters(
                tick_size=Decimal(f["PRICE_FILTER"]["tickSize"]),
                market_step_size=Decimal(f["MARKET_LOT_SIZE"]["stepSize"]),
                market_min_qty=Decimal(f["MARKET_LOT_SIZE"]["minQty"]),
                market_max_qty=Decimal(f["MARKET_LOT_SIZE"]["maxQty"]),
                min_notional=Decimal(f["MIN_NOTIONAL"]["notional"]),
            )
        except (KeyError, ArithmeticError):
            raise PublicAPIError("FILTERS_INCOMPLETE") from None
