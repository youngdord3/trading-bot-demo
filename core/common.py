"""Utility condivise: log JSON, lettura secrets, connessione DB, backoff."""
import json
import os
import random
import time
from pathlib import Path

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
