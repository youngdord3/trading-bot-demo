\set ON_ERROR_STOP on
\getenv core_password CORE_DB_PASSWORD

BEGIN;

CREATE ROLE core_reader
    LOGIN
    PASSWORD :'core_password'
    NOSUPERUSER
    NOCREATEDB
    NOCREATEROLE
    NOINHERIT
    NOREPLICATION;

GRANT CONNECT ON DATABASE trading_demo TO core_reader;
GRANT USAGE ON SCHEMA bot TO core_reader;

GRANT SELECT ON
    bot.system_state,
    bot.schema_migrations
TO core_reader;

ALTER ROLE core_reader
    SET default_transaction_read_only = on;

ALTER ROLE core_reader
    SET statement_timeout = '5s';

INSERT INTO bot.schema_migrations (version)
VALUES (2);

INSERT INTO bot.audit_events (event_type, details)
VALUES (
    'CORE_READER_PROVISIONED',
    '{"role":"core_reader","mode":"READ_ONLY"}'::jsonb
);

COMMIT;
