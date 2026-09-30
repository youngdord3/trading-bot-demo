BEGIN;

CREATE SCHEMA bot;

CREATE TABLE bot.schema_migrations (
    version integer PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE bot.system_state (
    id smallint PRIMARY KEY CHECK (id = 1),

    environment text NOT NULL
        CHECK (environment = 'DEMO'),

    status text NOT NULL
        CHECK (status IN ('PAUSED', 'HALTED')),

    orders_enabled boolean NOT NULL DEFAULT false
        CHECK (orders_enabled = false),

    reason text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE bot.audit_events (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    event_type text NOT NULL,
    details jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);

INSERT INTO bot.system_state (
    id,
    environment,
    status,
    orders_enabled,
    reason
)
VALUES (
    1,
    'DEMO',
    'PAUSED',
    false,
    'Bootstrap: execution engine not implemented'
);

INSERT INTO bot.audit_events (event_type, details)
VALUES (
    'SYSTEM_BOOTSTRAPPED',
    '{"environment":"DEMO","orders_enabled":false}'::jsonb
);

INSERT INTO bot.schema_migrations (version)
VALUES (1);

COMMIT;
