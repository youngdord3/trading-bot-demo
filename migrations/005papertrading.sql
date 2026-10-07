\set ON_ERROR_STOP on
\getenv paper_password CORE_PAPER_TRADER_DB_PASSWORD
\if :{?paper_password}
\else
\getenv paper_password COREPAPERTRADERDBPASSWORD
\endif

\if :{?paper_password}
\else
\warn 'CORE_PAPER_TRADER_DB_PASSWORD non impostata'
SELECT 'MISSING_PASSWORD'::int;
\endif

BEGIN;

DO $$
BEGIN
    IF (SELECT max(version) FROM bot.schema_migrations) NOT IN (4, 5) THEN
        RAISE EXCEPTION 'Attesa schema versione 4 o 5';
    END IF;
END $$;

DROP VIEW IF EXISTS bot.paper_processed_candles CASCADE;
DROP VIEW IF EXISTS bot.paper_trades CASCADE;
DROP TABLE IF EXISTS bot.paperprocessedcandles CASCADE;
DROP TABLE IF EXISTS bot.papertrades CASCADE;
DROP TABLE IF EXISTS bot.paper_processed_candles CASCADE;
DROP TABLE IF EXISTS bot.paper_trades CASCADE;

CREATE TABLE bot.paper_processed_candles (
    symbol              text    NOT NULL,
    interval            text    NOT NULL,
    candle_open_time_ms bigint  NOT NULL CHECK (candle_open_time_ms > 0),
    signal              text    NOT NULL CHECK (signal IN ('BUY', 'SELL', 'HOLD')),
    close_price         numeric(24, 8) NOT NULL CHECK (close_price > 0),
    processed_at        timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (symbol, interval, candle_open_time_ms)
);

CREATE TABLE bot.paper_trades (
    id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    symbol              text NOT NULL,
    action              text NOT NULL
        CHECK (action IN ('OPENLONG', 'CLOSELONG', 'OPENSHORT', 'CLOSESHORT', 'OPEN_LONG', 'CLOSE_LONG', 'OPEN_SHORT', 'CLOSE_SHORT')),
    side                text NOT NULL CHECK (side IN ('BUY', 'SELL')),
    quantity            numeric(24, 8) NOT NULL CHECK (quantity > 0),
    fill_price          numeric(24, 8) NOT NULL CHECK (fill_price > 0),
    notional_usdt       numeric(24, 8) NOT NULL CHECK (notional_usdt > 0),
    fee_usdt            numeric(24, 8) NOT NULL CHECK (fee_usdt >= 0),
    realized_pnl_usdt   numeric(24, 8) NOT NULL DEFAULT 0,
    signal_reason       text NOT NULL,
    candle_open_time_ms bigint NOT NULL,
    risk_checks         jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at          timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT paper_trades_side_matches_action CHECK (
        (action IN ('OPENLONG', 'CLOSESHORT', 'OPEN_LONG', 'CLOSE_SHORT') AND side = 'BUY') OR
        (action IN ('OPENSHORT', 'CLOSELONG', 'OPEN_SHORT', 'CLOSE_LONG') AND side = 'SELL')),
    CONSTRAINT paper_trades_no_duplicate UNIQUE (symbol, candle_open_time_ms, action)
);

CREATE INDEX paper_trades_created_at_idx ON bot.paper_trades (created_at DESC);

-- View di compatibilità per codice legacy senza underscore
CREATE VIEW bot.paperprocessedcandles AS SELECT * FROM bot.paper_processed_candles;
CREATE VIEW bot.papertrades AS SELECT * FROM bot.paper_trades;

REVOKE ALL ON bot.paper_processed_candles, bot.paper_trades FROM PUBLIC;
REVOKE ALL ON bot.paperprocessedcandles, bot.papertrades FROM PUBLIC;

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'core_paper_trader') THEN
        CREATE ROLE core_paper_trader
            NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;
    END IF;

    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'corepapertrader') THEN
        CREATE ROLE corepapertrader
            NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;
    END IF;
END $$;

ALTER ROLE core_paper_trader LOGIN PASSWORD :'paper_password';
ALTER ROLE corepapertrader LOGIN PASSWORD :'paper_password';

GRANT CONNECT ON DATABASE trading_demo TO core_paper_trader, corepapertrader;
GRANT USAGE ON SCHEMA bot TO core_paper_trader, corepapertrader;
GRANT SELECT ON bot.system_state TO core_paper_trader, corepapertrader;
GRANT SELECT ON bot.schema_migrations TO core_paper_trader, corepapertrader;
GRANT SELECT, INSERT ON bot.paper_processed_candles TO core_paper_trader, corepapertrader;
GRANT SELECT, INSERT ON bot.paper_trades TO core_paper_trader, corepapertrader;
GRANT SELECT, INSERT ON bot.paperprocessedcandles TO core_paper_trader, corepapertrader;
GRANT SELECT, INSERT ON bot.papertrades TO core_paper_trader, corepapertrader;

ALTER ROLE core_paper_trader SET statement_timeout = '5s';
ALTER ROLE corepapertrader SET statement_timeout = '5s';

DELETE FROM bot.schema_migrations WHERE version = 5;
INSERT INTO bot.schema_migrations (version) VALUES (5);

DELETE FROM bot.audit_events WHERE event_type = 'PAPER_TRADING_SCHEMA_DEPLOYED';
INSERT INTO bot.audit_events (event_type, details) VALUES (
    'PAPER_TRADING_SCHEMA_DEPLOYED',
    '{"tables":["paper_processed_candles","paper_trades"],"roles":["core_paper_trader","corepapertrader"]}'::jsonb
);

COMMIT;
