\set ON_ERROR_STOP on
\getenv trader_password CORE_TRADER_DB_PASSWORD
\if :{?trader_password}
\else
\warn 'CORE_TRADER_DB_PASSWORD non impostata'
SELECT 'MISSING_PASSWORD'::int;
\endif

BEGIN;

DO $$
BEGIN
    IF (SELECT max(version) FROM bot.schema_migrations) NOT IN (5, 6) THEN
        RAISE EXCEPTION 'Attesa schema versione 5 o 6';
    END IF;
END $$;

ALTER TABLE bot.system_state DROP CONSTRAINT IF EXISTS system_state_orders_enabled_check;
ALTER TABLE bot.system_state DROP CONSTRAINT IF EXISTS system_state_status_check;
ALTER TABLE bot.system_state DROP CONSTRAINT IF EXISTS system_state_orders_status_coherence;

ALTER TABLE bot.system_state
    ADD CONSTRAINT system_state_status_check
        CHECK (status IN ('PAUSED', 'ACTIVE', 'HALTED')),
    ADD CONSTRAINT system_state_orders_status_coherence
        CHECK (NOT orders_enabled OR status = 'ACTIVE');

CREATE TABLE IF NOT EXISTS bot.live_processed_candles (
    symbol              text    NOT NULL,
    interval            text    NOT NULL,
    candle_open_time_ms bigint  NOT NULL CHECK (candle_open_time_ms > 0),
    signal              text    NOT NULL CHECK (signal IN ('BUY', 'SELL', 'HOLD')),
    close_price         numeric(24, 8) NOT NULL CHECK (close_price > 0),
    processed_at        timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (symbol, interval, candle_open_time_ms)
);

CREATE TABLE IF NOT EXISTS bot.orders (
    id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    client_order_id     text    NOT NULL UNIQUE,
    exchange_order_id   bigint  NULL,
    symbol              text    NOT NULL,
    candle_open_time_ms bigint  NOT NULL CHECK (candle_open_time_ms > 0),
    order_kind          text    NOT NULL DEFAULT 'MARKET'
        CHECK (order_kind IN ('MARKET', 'STOP_MARKET')),
    action              text    NOT NULL
        CHECK (action IN ('OPEN_LONG', 'CLOSE_LONG', 'OPEN_SHORT', 'CLOSE_SHORT')),
    side                text    NOT NULL CHECK (side IN ('BUY', 'SELL')),
    reduce_only         boolean NOT NULL DEFAULT false,
    requested_qty       numeric(24, 8) NOT NULL CHECK (requested_qty > 0),
    executed_qty        numeric(24, 8) NOT NULL DEFAULT 0 CHECK (executed_qty >= 0),
    avg_fill_price      numeric(24, 8) NULL CHECK (avg_fill_price IS NULL OR avg_fill_price > 0),
    trigger_price       numeric(24, 8) NULL CHECK (trigger_price IS NULL OR trigger_price > 0),
    status              text    NOT NULL
        CHECK (status IN ('PENDING', 'NEW', 'PARTIALLY_FILLED', 'FILLED', 'CANCELED', 'FAILED')),
    signal_reason       text    NOT NULL,
    risk_checks         jsonb   NOT NULL DEFAULT '{}'::jsonb,
    error_code          text    NULL,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT orders_side_matches_action CHECK (
        (action IN ('OPEN_LONG', 'CLOSE_SHORT') AND side = 'BUY') OR
        (action IN ('OPEN_SHORT', 'CLOSE_LONG') AND side = 'SELL')
    ),
    CONSTRAINT orders_reduce_only_matches_action CHECK (
        (action IN ('CLOSE_LONG', 'CLOSE_SHORT') AND reduce_only = true) OR
        (action IN ('OPEN_LONG', 'OPEN_SHORT') AND reduce_only = false)
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS orders_unique_candle_market_action_idx
    ON bot.orders (symbol, candle_open_time_ms, action)
    WHERE order_kind = 'MARKET' AND status <> 'FAILED';

CREATE INDEX IF NOT EXISTS orders_active_status_idx
    ON bot.orders (status, created_at DESC);

CREATE TABLE IF NOT EXISTS bot.live_positions (
    symbol                text PRIMARY KEY,
    side                  text NOT NULL CHECK (side IN ('LONG', 'SHORT')),
    quantity              numeric(24, 8) NOT NULL CHECK (quantity > 0),
    entry_price           numeric(24, 8) NOT NULL CHECK (entry_price > 0),
    entry_client_order_id text NOT NULL,
    signal_reason         text NOT NULL,
    candle_open_time_ms   bigint NOT NULL CHECK (candle_open_time_ms > 0),
    stop_price            numeric(24, 8) NOT NULL CHECK (stop_price > 0),
    trail_distance        numeric(24, 8) NOT NULL CHECK (trail_distance >= 0),
    best_price            numeric(24, 8) NOT NULL CHECK (best_price > 0),
    stop_client_algo_id   text NULL,
    stop_algo_id          bigint NULL,
    stop_algo_status      text NULL,
    risk_checks           jsonb NOT NULL DEFAULT '{}'::jsonb,
    opened_at             timestamptz NOT NULL DEFAULT now(),
    updated_at            timestamptz NOT NULL DEFAULT now()
);

REVOKE ALL ON bot.live_processed_candles, bot.orders, bot.live_positions FROM PUBLIC;

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'core_trader') THEN
        CREATE ROLE core_trader
            NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;
    END IF;
END $$;

ALTER ROLE core_trader LOGIN PASSWORD :'trader_password';
GRANT CONNECT ON DATABASE trading_demo TO core_trader;
GRANT USAGE ON SCHEMA bot TO core_trader;
GRANT SELECT ON bot.system_state, bot.schema_migrations TO core_trader;
GRANT SELECT, INSERT, UPDATE ON bot.live_processed_candles, bot.orders, bot.live_positions TO core_trader;
GRANT DELETE ON bot.live_positions TO core_trader;
GRANT INSERT ON bot.audit_events TO core_trader;
ALTER ROLE core_trader SET statement_timeout = '5s';

GRANT SELECT ON bot.orders, bot.live_positions TO core_reconciler;

DELETE FROM bot.schema_migrations WHERE version = 6;
INSERT INTO bot.schema_migrations (version) VALUES (6);

DELETE FROM bot.audit_events WHERE event_type = 'TRADING_SCHEMA_006_DEPLOYED';
INSERT INTO bot.audit_events (event_type, details) VALUES (
    'TRADING_SCHEMA_006_DEPLOYED',
    '{"tables":["live_processed_candles","orders","live_positions"],"roles":["core_trader"],"reconciler_select":["orders","live_positions"]}'::jsonb
);

COMMIT;
