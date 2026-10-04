import hmac
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from binance_private import PrivateReadError, ReadOnlyClient

def read_secret(name: str) -> str:
    value = Path(f"/run/secrets/{name}").read_text(
        encoding="utf-8"
    ).strip()

    if len(value) < 32:
        raise RuntimeError(f"Secret missing or too short: {name}")

    return value

DB_PASSWORD = read_secret("core_db_password")
API_TOKEN = read_secret("core_api_token")

DB_HOST = os.environ.get("DB_HOST", "postgres")
DB_NAME = os.environ.get("DB_NAME", "trading_demo")
DB_USER = "core_reader"

def read_database_status() -> dict:
    with psycopg.connect(
        host=DB_HOST,
        port=5432,
        dbname=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD,
        connect_timeout=3,
        options="-c statement_timeout=5000",
        row_factory=dict_row,
    ) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    environment,
                    status,
                    orders_enabled,
                    updated_at
                FROM bot.system_state
                WHERE id = 1
                """
            )
            state = cur.fetchone()

            cur.execute(
                """
                SELECT version
                FROM bot.schema_migrations
                ORDER BY version
                """
            )
            migrations = [
                row["version"] for row in cur.fetchall()
            ]

            cur.execute(
                """
                SELECT
                    current_user AS db_user,
                    current_setting(
                        'transaction_read_only'
                    ) AS transaction_read_only,
                    has_table_privilege(
                        current_user,
                        'bot.system_state',
                        'UPDATE'
                    ) AS can_update_state
                """
            )
            permissions = cur.fetchone()

    if state is None:
        raise RuntimeError("System state missing")

    checks = {
        "demo_environment": state["environment"] == "DEMO",
        "paused": state["status"] == "PAUSED",
        "orders_disabled": state["orders_enabled"] is False,
        "expected_schema": migrations == [1, 2, 3],
        "restricted_db_user": (
            permissions["db_user"] == DB_USER
        ),
        "read_only_transaction": (
            permissions["transaction_read_only"] == "on"
        ),
        "no_state_update_privilege": (
            permissions["can_update_state"] is False
        ),
    }

    return {
        "service": "trading-core",
        "phase": "DATABASE_READ_ONLY",
        "ready": all(checks.values()),
        "database_connected": True,
        "environment": state["environment"],
        "status": state["status"],
        "orders_enabled": state["orders_enabled"],
        "schema_versions": migrations,
        "db_user": permissions["db_user"],
        "checks": checks,
        "state_updated_at": state["updated_at"].isoformat(),
        "exchange_connected": False,
        "trading_ready": False,
    }

EXCHANGE_CACHE_TTL_SECONDS = 10.0
_exchange_lock = threading.Lock()
_exchange_last_check = 0.0
_exchange_connected_cached = False

def check_exchange_connected() -> bool:
    global _exchange_last_check, _exchange_connected_cached
    now = time.monotonic()
    with _exchange_lock:
        if (now - _exchange_last_check) < EXCHANGE_CACHE_TTL_SECONDS:
            return _exchange_connected_cached

        connected = False
        try:
            client = ReadOnlyClient()
            data = client.get("/fapi/v3/account")
            if isinstance(data, dict) and "totalMarginBalance" in data:
                connected = True
        except Exception:
            connected = False

        _exchange_last_check = time.monotonic()
        _exchange_connected_cached = connected
        return _exchange_connected_cached

def read_readiness_status() -> dict:
    payload = read_database_status()
    exchange_connected = check_exchange_connected()
    payload["exchange_connected"] = exchange_connected
    payload["checks"]["exchange_connected"] = exchange_connected
    payload["ready"] = bool(payload["ready"] and exchange_connected)
    payload["trading_ready"] = False
    return payload

class Handler(BaseHTTPRequestHandler):
    server_version = "TradingCore"
    sys_version = ""

    def log_message(self, format, *args):
        # Avoid logging request headers, tokens or raw URLs.
        return

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def send_json(self, status: int, payload: dict):
        body = json.dumps(payload).encode("utf-8")

        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def authenticated(self) -> bool:
        supplied = self.headers.get("Authorization", "")
        expected = f"Bearer {API_TOKEN}"

        return hmac.compare_digest(
            supplied.encode("utf-8"),
            expected.encode("utf-8"),
        )

    def do_GET(self):
        if self.path == "/health":
            self.send_json(
                200,
                {
                    "service": "trading-core",
                    "alive": True,
                    "trading_ready": False,
                },
            )
            return

        if not self.authenticated():
            self.send_json(401, {"error": "UNAUTHORIZED"})
            return

        if self.path != "/ready":
            self.send_json(404, {"error": "NOT_FOUND"})
            return

        try:
            payload = read_readiness_status()
        except Exception:
            # Do not expose internal errors or credentials.
            self.send_json(
                503,
                {
                    "ready": False,
                    "error": "READINESS_CHECK_FAILED",
                    "trading_ready": False,
                },
            )
            return

        self.send_json(
            200 if payload["ready"] else 503,
            payload,
        )

    def do_POST(self):
        self.send_json(
            405,
            {"error": "WRITE_ENDPOINTS_NOT_IMPLEMENTED"},
        )

if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", 8000), Handler)
    server.daemon_threads = True

    print(
        "Trading Core started: READ_ONLY; "
        "Binance Futures read-only connector enabled; no order endpoints.",
        flush=True,
    )

    server.serve_forever()
