import ssl
import time
from http.client import HTTPSConnection
from pathlib import Path

from common import build_signed_query, parse_binance_json, tls_context

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
    def __init__(self, time_offset_ms: int = 0,
                 api_key: str | None = None,
                 api_secret: str | None = None):
        self._time_offset_ms = time_offset_ms
        self._key = api_key if api_key is not None else _read_secret("binance_api_key")
        raw_secret = api_secret if api_secret is not None else _read_secret("binance_api_secret")
        self._secret = raw_secret.encode("utf-8")
        self._ctx = tls_context()

    def set_time_offset_ms(self, offset_ms: int) -> None:
        """Aggiorna lo skew locale->Binance per firmare timestamp entro recvWindow."""
        self._time_offset_ms = int(offset_ms)

    def sync_time_offset(self, public_client=None) -> int:
        """Misura e applica l'offset orario verso l'endpoint pubblico /fapi/v1/time."""
        if public_client is None:
            from binance_public import PublicClient
            public_client = PublicClient(HOST)
        offset_ms, _rtt_ms = public_client.measure_time_offset_ms()
        self.set_time_offset_ms(offset_ms)
        return self._time_offset_ms

    def get(self, path, params=None):
        if path not in ALLOWED_GET_PATHS:
            raise PrivateReadError("ENDPOINT_NOT_ALLOWED")

        now_ms = int(time.time() * 1000) + self._time_offset_ms
        signed = build_signed_query(self._secret, params, now_ms=now_ms, recv_window_ms=RECV_WINDOW_MS)
        target = f"{path}?{signed}"

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

        return parse_binance_json(resp.status, raw, MAX_RESPONSE_BYTES, PrivateReadError)
