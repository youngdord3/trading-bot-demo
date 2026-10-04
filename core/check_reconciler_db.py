import json
import os
import sys
from pathlib import Path

import psycopg
from psycopg.rows import dict_row


def main():
    try:
        password = Path(
            "/run/secrets/core_reconciler_db_password"
        ).read_text(encoding="utf-8").strip()

        if not password:
            raise ValueError("EMPTY_DB_PASSWORD")

        with psycopg.connect(
            host=os.environ.get("DB_HOST", "postgres"),
            port=5432,
            dbname=os.environ.get("DB_NAME", "trading_demo"),
            user="core_reconciler",
            password=password,
            connect_timeout=3,
            options="-c statement_timeout=5000 -c lock_timeout=3000",
            row_factory=dict_row,
        ) as conn:
            with conn.cursor() as cur:
                cur.execute("SET TRANSACTION READ ONLY")

                cur.execute(
                    """
                    SELECT
                        current_user AS db_user,
                        session_user AS session_user,
                        r.rolsuper,
                        r.rolcreaterole,
                        r.rolcreatedb,
                        r.rolbypassrls
                    FROM pg_catalog.pg_roles AS r
                    WHERE r.rolname = current_user
                    """
                )
                identity = cur.fetchone()

                cur.execute(
                    """
                    SELECT
                        environment,
                        status,
                        orders_enabled
                    FROM bot.system_state
                    WHERE id = 1
                    """
                )
                state = cur.fetchone()

                cur.execute(
                    """
                    SELECT
                        has_table_privilege(
                            current_user,
                            'bot.reconciliation_snapshots',
                            'INSERT'
                        ) AS can_insert_snapshot,
                        has_table_privilege(
                            current_user,
                            'bot.system_state',
                            'UPDATE'
                        ) AS can_update_state,
                        has_any_column_privilege(
                            current_user,
                            'bot.system_state',
                            'UPDATE'
                        ) AS can_update_state_column,
                        has_table_privilege(
                            current_user,
                            'bot.audit_events',
                            'INSERT'
                        ) AS can_insert_audit,
                        has_any_column_privilege(
                            current_user,
                            'bot.audit_events',
                            'INSERT'
                        ) AS can_insert_audit_column
                    """
                )
                permissions = cur.fetchone()

                cur.execute(
                    """
                    SELECT
                        p.proname AS function_name,
                        pg_get_function_identity_arguments(p.oid)
                            AS identity_arguments,
                        pg_get_function_result(p.oid) AS result_type,
                        pg_get_userbyid(p.proowner) AS owner,
                        p.prosecdef AS security_definer,
                        p.proconfig AS settings,
                        has_function_privilege(
                            current_user, p.oid, 'EXECUTE'
                        ) AS can_execute
                    FROM pg_catalog.pg_proc AS p
                    JOIN pg_catalog.pg_namespace AS n
                        ON n.oid = p.pronamespace
                    WHERE n.nspname = 'bot'
                      AND p.proname IN (
                          'record_reconciliation_anomaly',
                          'trip_circuit_breaker'
                      )
                    ORDER BY p.proname, p.oid
                    """
                )
                functions = cur.fetchall()

                cur.execute(
                    """
                    SELECT
                        c.relname AS table_name,
                        a.attname AS column_name,
                        format_type(a.atttypid, a.atttypmod)
                            AS data_type,
                        a.attnotnull AS not_null,
                        pg_get_expr(d.adbin, d.adrelid)
                            AS default_expression
                    FROM pg_catalog.pg_class AS c
                    JOIN pg_catalog.pg_namespace AS n
                        ON n.oid = c.relnamespace
                    JOIN pg_catalog.pg_attribute AS a
                        ON a.attrelid = c.oid
                    LEFT JOIN pg_catalog.pg_attrdef AS d
                        ON d.adrelid = a.attrelid
                       AND d.adnum = a.attnum
                    WHERE n.nspname = 'bot'
                      AND c.relname = 'reconciliation_snapshots'
                      AND a.attnum > 0
                      AND NOT a.attisdropped
                    ORDER BY a.attnum
                    """
                )
                snapshot_columns = cur.fetchall()

                cur.execute(
                    """
                    SELECT
                        conname AS constraint_name,
                        pg_get_constraintdef(oid)
                            AS constraint_definition
                    FROM pg_catalog.pg_constraint
                    WHERE conrelid =
                        'bot.reconciliation_snapshots'::regclass
                    ORDER BY conname
                    """
                )
                snapshot_constraints = cur.fetchall()

        expected_functions = {
            "record_reconciliation_anomaly",
            "trip_circuit_breaker",
        }

        checks = {
            "identity_ok": (
                identity is not None
                and identity["db_user"] == "core_reconciler"
                and identity["session_user"] == "core_reconciler"
            ),
            "role_not_privileged": (
                identity is not None
                and not any(
                    identity[key]
                    for key in (
                        "rolsuper",
                        "rolcreaterole",
                        "rolcreatedb",
                        "rolbypassrls",
                    )
                )
            ),
            "demo_environment": (
                state is not None
                and state["environment"] == "DEMO"
            ),
            "baseline_paused": (
                state is not None
                and state["status"] == "PAUSED"
            ),
            "orders_disabled": (
                state is not None
                and state["orders_enabled"] is False
            ),
            "snapshot_insert_allowed": (
                permissions["can_insert_snapshot"] is True
            ),
            "direct_state_update_denied": (
                permissions["can_update_state"] is False
                and permissions["can_update_state_column"] is False
            ),
            "direct_audit_insert_denied": (
                permissions["can_insert_audit"] is False
                and permissions["can_insert_audit_column"] is False
            ),
            "functions_unambiguous": (
                len(functions) == 2
                and {f["function_name"] for f in functions}
                == expected_functions
            ),
            "function_security_ok": (
                len(functions) == 2
                and all(
                    f["owner"] == "bot_breaker"
                    and f["security_definer"] is True
                    and f["can_execute"] is True
                    and "search_path=pg_catalog, pg_temp"
                    in (f["settings"] or [])
                    for f in functions
                )
            ),
        }

        passed = all(checks.values())

        print(json.dumps(
            {
                "phase": "RECONCILER_DB_PREFLIGHT",
                "passed": passed,
                "checks": checks,
                "identity": identity,
                "state": state,
                "permissions": permissions,
                "functions": functions,
                "snapshot_columns": snapshot_columns,
                "snapshot_constraints": snapshot_constraints,
                "exchange_called": False,
                "database_writes": False,
            },
            indent=2,
        ))
        return 0 if passed else 1

    except Exception as exc:
        sqlstate = getattr(exc, "sqlstate", None)
        print(json.dumps(
            {
                "phase": "RECONCILER_DB_PREFLIGHT",
                "passed": False,
                "error_type": type(exc).__name__,
                "sqlstate": sqlstate,
            },
            indent=2,
        ))
        return 1


if __name__ == "__main__":
    sys.exit(main())
