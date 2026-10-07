import hashlib
import hmac
import json
import ssl
import time
from http.client import HTTPSConnection
from pathlib import Path
from urllib.parse import urlencode

HOST = "demo-fapi.binance.com"
RECV_WINDOW_MS = 5000
TIMEOUT_S = 10
MAX_RESPONSE_BYTES = 2_000_000

# Soli endpoint GET. Nessun altro metodo esiste in questo modulo.
ALLOWED_GET_PATHS = {
    "/fapi/v3/account",
    "/fapi/v3/positionRisk",
    "/fapi/v1/openOrders",
    "/fapi/v1/openAlgoOrders",
    "/fapi/v1/positionSide/dual",
    "/fapi/v1/symbolConfig",
}

class PrivateReadError(Exception):
    pass

def _read_secret(name):
    value = Path(f"/run/secrets/{name}").read_text(encoding="utf-8").strip()
    if len(value) < 16:
        raise PrivateReadError(f"SECRET_MISSING_{name.upper()}")
    return value

class ReadOnlyClient:
    def __init__(self, time_offset_ms: int = 0):
        self._time_offset_ms = time_offset_ms
        self._key = _read_secret("binance_api_key")
        self._secret = _read_secret("binance_api_secret").encode("utf-8")
        self._ctx = ssl.create_default_context()
        self._ctx.minimum_version = ssl.TLSVersion.TLSv1_2

    def get(self, path, params=None):
        if path not in ALLOWED_GET_PATHS:
            raise PrivateReadError("ENDPOINT_NOT_ALLOWED")

        query = dict(params or {})
        query["recvWindow"] = RECV_WINDOW_MS
        query["timestamp"] = int(time.time() * 1000) + self._time_offset_ms  # fresco a ogni chiamata
        encoded = urlencode(query)
        signature = hmac.new(
            self._secret, encoded.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        target = f"{path}?{encoded}&signature={signature}"

        conn = HTTPSConnection(HOST, 443, timeout=TIMEOUT_S, context=self._ctx)
        try:
            conn.request(
                "GET",
                target,
                headers={
                    "X-MBX-APIKEY": self._key,
                    "Accept": "application/json",
                    "User-Agent": "TradingCore-Demo-ReadOnly/0.2",
                },
            )
            resp = conn.getresponse()
            raw = resp.read(MAX_RESPONSE_BYTES + 1)
        except (OSError, ssl.SSLError, TimeoutError):
            raise PrivateReadError("NETWORK_OR_TLS_ERROR") from None
        finally:
            conn.close()

        if len(raw) > MAX_RESPONSE_BYTES:
            raise PrivateReadError("RESPONSE_TOO_LARGE")
        if resp.status in (301, 302, 307, 308):
            raise PrivateReadError("REDIRECT_REFUSED")
        if resp.status != 200:
            # Mai stampare il corpo: può contenere dettagli dell'account.
            raise PrivateReadError(f"HTTP_{resp.status}")

        try:
            data = json.loads(raw)
        except (ValueError, UnicodeError):
            raise PrivateReadError("INVALID_JSON") from None
        if isinstance(data, dict) and "code" in data and "msg" in data:
            raise PrivateReadError(f"BINANCE_CODE_{data['code']}")
        return data
