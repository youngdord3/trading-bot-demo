\set ON_ERROR_STOP on
\getenv reconciler_password CORE_RECONCILER_DB_PASSWORD

BEGIN;

CREATE TABLE bot.reconciliation_snapshots (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    captured_at timestamptz NOT NULL DEFAULT now(),

    environment text NOT NULL
        CHECK (environment = 'DEMO'),

    exchange_ok boolean NOT NULL,

    latency_ms integer
        CHECK (latency_ms IS NULL OR latency_ms >= 0),

    position_mode text
        CHECK (position_mode IS NULL OR position_mode IN ('ONE_WAY', 'HEDGE')),

    open_positions_count integer
        CHECK (open_positions_count IS NULL OR open_positions_count >= 0),

    open_orders_count integer
        CHECK (open_orders_count IS NULL OR open_orders_count >= 0),

    open_algo_orders_count integer
        CHECK (open_algo_orders_count IS NULL OR open_algo_orders_count >= 0),

    total_equity_usdt numeric(24, 8),

    error_code text,

    details jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX reconciliation_snapshots_captured_at_idx
    ON bot.reconciliation_snapshots (captured_at DESC);

REVOKE ALL ON bot.reconciliation_snapshots FROM PUBLIC;

CREATE ROLE core_reconciler
    LOGIN
    PASSWORD :'reconciler_password'
    NOSUPERUSER
    NOCREATEDB
    NOCREATEROLE
    NOINHERIT
    NOREPLICATION;

GRANT CONNECT ON DATABASE trading_demo TO core_reconciler;
GRANT USAGE ON SCHEMA bot TO core_reconciler;

GRANT SELECT ON bot.system_state TO core_reconciler;
GRANT INSERT ON bot.reconciliation_snapshots TO core_reconciler;

ALTER ROLE core_reconciler
    SET statement_timeout = '5s';

INSERT INTO bot.schema_migrations (version)
VALUES (3);

INSERT INTO bot.audit_events (event_type, details)
VALUES (
    'CORE_RECONCILER_PROVISIONED',
    '{"role":"core_reconciler","mode":"APPEND_ONLY_SNAPSHOTS"}'::jsonb
);

COMMIT;
