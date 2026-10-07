\set ON_ERROR_STOP on
\getenv paperpassword COREPAPERTRADERDBPASSWORD
\if :{?paperpassword}
\else
\warn 'COREPAPERTRADERDBPASSWORD non impostata'
SELECT 'MISSINGPASSWORD'::int;
\endif

BEGIN;

DO $$
BEGIN
    IF (SELECT max(version) FROM bot.schema_migrations) <> 4 THEN
        RAISE EXCEPTION 'Attesa schema versione 4';
    END IF;
END $$;

CREATE TABLE bot.paperprocessedcandles (
    symbol              text    NOT NULL,
    interval            text    NOT NULL,
    candleopentimems    bigint  NOT NULL CHECK (candleopentimems > 0),
    signal              text    NOT NULL CHECK (signal IN ('BUY', 'SELL', 'HOLD')),
    closeprice          numeric(24, 8) NOT NULL CHECK (closeprice > 0),
    processedat         timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (symbol, interval, candleopentimems)
);

CREATE TABLE bot.papertrades (
    id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    symbol              text NOT NULL,
    action              text NOT NULL
        CHECK (action IN ('OPENLONG', 'CLOSELONG', 'OPENSHORT', 'CLOSESHORT', 'OPEN_LONG', 'CLOSE_LONG', 'OPEN_SHORT', 'CLOSE_SHORT')),
    side                text NOT NULL CHECK (side IN ('BUY', 'SELL')),
    quantity            numeric(24, 8) NOT NULL CHECK (quantity > 0),
    fillprice           numeric(24, 8) NOT NULL CHECK (fillprice > 0),
    notionalusdt        numeric(24, 8) NOT NULL CHECK (notionalusdt > 0),
    feeusdt             numeric(24, 8) NOT NULL CHECK (feeusdt >= 0),
    realizedpnlusdt     numeric(24, 8) NOT NULL DEFAULT 0,
    signalreason        text NOT NULL,
    candleopentimems    bigint NOT NULL,
    riskchecks          jsonb NOT NULL DEFAULT '{}'::jsonb,
    createdat           timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT papertradessidematchesaction CHECK (
        (action IN ('OPENLONG', 'CLOSESHORT', 'OPEN_LONG', 'CLOSE_SHORT') AND side = 'BUY') OR
        (action IN ('OPENSHORT', 'CLOSELONG', 'OPEN_SHORT', 'CLOSE_LONG') AND side = 'SELL')),
    CONSTRAINT papertradesnoduplicate UNIQUE (symbol, candleopentimems, action)
);

CREATE INDEX papertradescreatedatidx ON bot.papertrades (createdat DESC);

-- View compatibili per convenzioni snake_case
CREATE VIEW bot.paper_processed_candles AS SELECT * FROM bot.paperprocessedcandles;
CREATE VIEW bot.paper_trades AS SELECT * FROM bot.papertrades;

REVOKE ALL ON bot.paperprocessedcandles, bot.papertrades FROM PUBLIC;
REVOKE ALL ON bot.paper_processed_candles, bot.paper_trades FROM PUBLIC;

CREATE ROLE corepapertrader
    LOGIN PASSWORD :'paperpassword'
    NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;

GRANT CONNECT ON DATABASE trading_demo TO corepapertrader;
GRANT USAGE ON SCHEMA bot TO corepapertrader;
GRANT SELECT ON bot.system_state TO corepapertrader;
GRANT SELECT ON bot.schema_migrations TO corepapertrader;
-- Append-only: niente UPDATE/DELETE
GRANT SELECT, INSERT ON bot.paperprocessedcandles TO corepapertrader;
GRANT SELECT, INSERT ON bot.papertrades TO corepapertrader;
GRANT SELECT, INSERT ON bot.paper_processed_candles TO corepapertrader;
GRANT SELECT, INSERT ON bot.paper_trades TO corepapertrader;

ALTER ROLE corepapertrader SET statement_timeout = '5s';

INSERT INTO bot.schema_migrations (version) VALUES (5);
INSERT INTO bot.audit_events (event_type, details) VALUES (
    'PAPER_TRADING_SCHEMA_DEPLOYED',
    '{"tables":["paperprocessedcandles","papertrades"],"role":"corepapertrader"}'::jsonb
);

COMMIT;
