import json
import os
import random
import re
import sys
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path

import psycopg

from binance_private import PrivateReadError, ReadOnlyClient


def to_code(text):
    code = re.sub(r"[^A-Z0-9]", "", str(text).upper())[:64]
    return code or "UNKNOWN"


tocode = to_code


def emit(payload):
    print(json.dumps(payload), flush=True)


def load_config():
    mode = os.environ.get("RECONCILER_MODE", os.environ.get("RECONCILERMODE", "once"))
    threshold = int(os.environ.get("TRIP_THRESHOLD", os.environ.get("TRIPTHRESHOLD", "3")))

    if mode not in ("once", "loop") or threshold < 1:
        raise ValueError("Invalid reconciler configuration")

    return mode, threshold


loadconfig = load_config


def db_connect():
    password = Path(
        "/run/secrets/core_reconciler_db_password"
    ).read_text(encoding="utf-8").strip()

    if not password:
        raise ValueError("Empty database password")

    return psycopg.connect(
        host=os.environ.get("DB_HOST", os.environ.get("DBHOST", "postgres")),
        dbname=os.environ.get("DB_NAME", os.environ.get("DBNAME", "trading_demo")),
        user="core_reconciler",
        password=password,
        connect_timeout=5,
        application_name="reconciler",
        autocommit=True,
    )


dbconnect = db_connect


def parse_decimal(value):
    if not isinstance(value, (str, int, float, Decimal)):
        raise PrivateReadError("UNPARSABLENUMBERS")

    if isinstance(value, bool):
        raise PrivateReadError("UNPARSABLENUMBERS")

    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise PrivateReadError("UNPARSABLENUMBERS") from None

    if not number.isfinite():
        raise PrivateReadError("UNPARSABLENUMBERS")

    return number


parsedecimal = parse_decimal


def require_records(value, error_code):
    if not isinstance(value, list):
        raise PrivateReadError(error_code)

    if any(not isinstance(item, dict) for item in value):
        raise PrivateReadError(error_code)

    return value


requirerecords = require_records


def observe(client):
    started = time.monotonic()

    account = client.get("/fapi/v3/account")
    positions = client.get("/fapi/v3/positionRisk")
    orders = client.get("/fapi/v1/openOrders")
    algo = client.get("/fapi/v1/openAlgoOrders")
    dual = client.get("/fapi/v1/positionSide/dual")

    latency_ms = int((time.monotonic() - started) * 1000)

    if not isinstance(account, dict):
        raise PrivateReadError("UNEXPECTEDSHAPEACCOUNT")

    if "totalMarginBalance" not in account:
        raise PrivateReadError("MISSINGEQUITY")

    positions = require_records(
        positions, "UNEXPECTEDSHAPEPOSITIONS"
    )
    orders = require_records(
        orders, "UNEXPECTEDSHAPEORDERS"
    )

    # Non convertire una risposta sconosciuta in una lista vuota.
    if isinstance(algo, dict):
        if "orders" in algo:
            algo = algo["orders"]
        elif "rows" in algo:
            algo = algo["rows"]
        else:
            raise PrivateReadError("UNEXPECTEDSHAPEALGO")

    algo = require_records(algo, "UNEXPECTEDSHAPEALGO")

    if not isinstance(dual, dict):
        raise PrivateReadError("UNEXPECTEDSHAPEMODE")

    dualside = dual.get("dualSidePosition")

    # Evita che la stringa "false" venga interpretata come True.
    if type(dualside) is not bool:
        raise PrivateReadError("UNEXPECTEDSHAPEMODE")

    equity = parse_decimal(account["totalMarginBalance"])

    open_positions_count = 0
    for position in positions:
        if "positionAmt" not in position:
            raise PrivateReadError("MISSINGPOSITIONAMOUNT")

        if parse_decimal(position["positionAmt"]) != 0:
            open_positions_count += 1

    return {
        "latency_ms": latency_ms,
        "position_mode": "HEDGE" if dualside else "ONE_WAY",
        "open_positions_count": open_positions_count,
        "open_orders_count": len(orders),
        "open_algo_orders_count": len(algo),
        "total_equity_usdt": equity,
    }


def evaluate(observation):
    # Fase pre-operativa: nessuna posizione, nessun ordine,
    # modalita ONE_WAY. Il saldo DEMO puo essere diverso da zero.
    if observation["position_mode"] != "ONE_WAY":
        return "UNEXPECTEDHEDGEMODE"

    if observation["open_positions_count"] != 0:
        return "UNEXPECTEDPOSITIONS"

    if (
        observation["open_orders_count"] != 0
        or observation["open_algo_orders_count"] != 0
    ):
        return "UNEXPECTEDORDERS"

    return None


def record_ok(cursor, observation):
    cursor.execute(
        """
        INSERT INTO bot.reconciliation_snapshots (
            environment,
            exchange_ok,
            latency_ms,
            position_mode,
            open_positions_count,
            open_orders_count,
            open_algo_orders_count,
            total_equity_usdt,
            error_code,
            details
        )
        VALUES (
            'DEMO', true, %s, %s, %s, %s, %s, %s,
            NULL, '{}'::jsonb
        )
        """,
        (
            observation["latency_ms"],
            observation["position_mode"],
            observation["open_positions_count"],
            observation["open_orders_count"],
            observation["open_algo_orders_count"],
            observation["total_equity_usdt"],
        ),
    )


recordok = record_ok


def record_anomaly(cursor, code, observation, details):
    observation = observation or {}

    cursor.execute(
        """
        SELECT bot.record_reconciliation_anomaly(
            %s, %s, %s, %s, %s, %s, %s, %s::jsonb
        )
        """,
        (
            code,
            observation.get("latency_ms"),
            observation.get("position_mode"),
            observation.get("open_positions_count"),
            observation.get("open_orders_count"),
            observation.get("open_algo_orders_count"),
            observation.get("total_equity_usdt"),
            json.dumps(details),
        ),
    )

    row = cursor.fetchone()
    if row is None or row[0] is None:
        raise RuntimeError("Missing anomaly snapshot id")

    return row[0]


recordanomaly = record_anomaly


def trip(cursor, code, details):
    cursor.execute(
        "SELECT bot.trip_circuit_breaker(%s, %s::jsonb)",
        (code, json.dumps(details)),
    )

    row = cursor.fetchone()
    if row is None or type(row[0]) is not bool:
        raise RuntimeError("Unexpected circuit breaker result")

    return row[0]


def cycle(client, connection, consecutive, threshold):
    observation = None

    try:
        observation = observe(client)
        code = evaluate(observation)
    except PrivateReadError as error:
        code = to_code(error)

    if code is None:
        with connection.transaction():
            with connection.cursor() as cursor:
                record_ok(cursor, observation)

        return 0, {
            "result": "OK",
            "latency_ms": observation["latency_ms"],
            "consecutive": 0,
        }

    code = to_code(code)
    consecutive += 1

    details = {
        "consecutive": consecutive,
        "threshold": threshold,
    }
    trip_requested = consecutive >= threshold
    tripped = False

    with connection.transaction():
        with connection.cursor() as cursor:
            snapshot_id = record_anomaly(
                cursor, code, observation, details
            )

            if trip_requested:
                tripped = trip(cursor, code, details)

    return consecutive, {
        "result": "ANOMALY",
        "error_code": code,
        "snapshot_id": str(snapshot_id),
        "consecutive": consecutive,
        "threshold": threshold,
        "trip_requested": trip_requested,
        "tripped": tripped,
    }


def main():
    try:
        mode, threshold = load_config()
    except (ValueError, OverflowError):
        emit({"fatal": "CONFIGINVALID"})
        return 2

    client = ReadOnlyClient()
    consecutive = 0

    with db_connect() as connection:
        while True:
            consecutive, output = cycle(
                client, connection, consecutive, threshold
            )
            emit(output)

            if mode == "once":
                return 0 if output["result"] == "OK" else 3

            time.sleep(60 + random.uniform(0, 10))


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        # Non stampare messaggi, traceback o credenziali.
        emit({"fatal": to_code(type(error).__name__)})
        sys.exit(1)
