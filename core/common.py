"""Utility condivise: log JSON, lettura secrets, firma HMAC, TLS, connessione DB, backoff."""
import hashlib
import hmac
import json
import os
import random
import ssl
import time
from pathlib import Path
from urllib.parse import urlencode

import psycopg


def emit(event: str, **fields) -> None:
    """Log strutturato su stdout, una riga JSON per evento."""
    print(json.dumps({"ts": int(time.time()), "event": event, **fields},
                     default=str), flush=True)


def read_secret(name: str, min_len: int = 16) -> str:
    value = Path(f"/run/secrets/{name}").read_text(encoding="utf-8").strip()
    if len(value) < min_len:
        raise RuntimeError(f"SECRET_INVALID_{name.upper()}")
    return value


def db_connect(user: str, secret_name: str) -> psycopg.Connection:
    """Connessione in autocommit: le transazioni si aprono esplicitamente
    con `with conn.transaction():`."""
    return psycopg.connect(
        host=os.environ.get("DB_HOST", "postgres"),
        dbname=os.environ.get("DB_NAME", "trading_demo"),
        user=user,
        password=read_secret(secret_name),
        connect_timeout=5,
        autocommit=True,
    )


def backoff_sleep(attempt: int, base: float = 2.0, cap: float = 60.0) -> None:
    time.sleep(min(cap, base * (2 ** max(0, attempt - 1))) + random.uniform(0, 1))


def tls_context() -> ssl.SSLContext:
    """Contesto TLS >= 1.2 condiviso dai client exchange."""
    ctx = ssl.create_default_context()
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


def build_signed_query(
    secret: bytes,
    params: dict | None = None,
    now_ms: int | None = None,
    recv_window_ms: int = 5000,
) -> str:
    """Serializza parametri con recvWindow/timestamp freschi e appende firma HMAC-SHA256."""
    query = dict(params or {})
    query["recvWindow"] = recv_window_ms
    query["timestamp"] = int(time.time() * 1000) if now_ms is None else now_ms
    encoded = urlencode(query)
    signature = hmac.new(secret, encoded.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{encoded}&signature={signature}"


def parse_binance_json(status: int, raw: bytes, max_bytes: int, err_cls: type[Exception]):
    """Valida dimensione, HTTP status, JSON e codici errore Binance senza esporre il payload grezzo."""
    if len(raw) > max_bytes:
        raise err_cls("RESPONSE_TOO_LARGE")
    if status in (301, 302, 307, 308):
        raise err_cls("REDIRECT_REFUSED")
    if status != 200:
        # Estrarre solo il codice errore numerico Binance se presente in JSON, mai il corpo grezzo.
        try:
            err_data = json.loads(raw)
        except (ValueError, UnicodeError):
            err_data = None
        if isinstance(err_data, dict) and type(err_data.get("code")) is int:
            raise err_cls(f"BINANCE_HTTP_{status}_CODE_{err_data['code']}")
        raise err_cls(f"HTTP_{status}")
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeError):
        raise err_cls("INVALID_JSON") from None
    if isinstance(data, dict) and "code" in data and "msg" in data:
        code = data["code"]
        if not (type(code) is int and code in (0, 200)):
            raise err_cls(f"BINANCE_CODE_{code}")
    return data
