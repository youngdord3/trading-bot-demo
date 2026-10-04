\set ON_ERROR_STOP on

BEGIN;

CREATE ROLE bot_breaker
    NOLOGIN
    NOSUPERUSER
    NOCREATEDB
    NOCREATEROLE
    NOINHERIT
    NOREPLICATION
    NOBYPASSRLS;

GRANT USAGE ON SCHEMA bot TO bot_breaker;
GRANT INSERT ON bot.reconciliation_snapshots TO bot_breaker;
GRANT SELECT (id) ON bot.reconciliation_snapshots TO bot_breaker;
GRANT INSERT ON bot.audit_events TO bot_breaker;
GRANT SELECT ON bot.system_state TO bot_breaker;
GRANT UPDATE (status, orders_enabled, reason, updated_at)
    ON bot.system_state TO bot_breaker;

-- Fase 1: snapshot anomalo + audit event nella stessa transazione
CREATE FUNCTION bot.record_reconciliation_anomaly(
    p_error_code             text,
    p_latency_ms             integer DEFAULT NULL,
    p_position_mode          text    DEFAULT NULL,
    p_open_positions_count   integer DEFAULT NULL,
    p_open_orders_count      integer DEFAULT NULL,
    p_open_algo_orders_count integer DEFAULT NULL,
    p_total_equity_usdt      numeric DEFAULT NULL,
    p_details                jsonb   DEFAULT '{}'::jsonb
)
RETURNS bigint
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    v_snapshot_id bigint;
BEGIN
    IF p_error_code IS NULL OR p_error_code !~ '^[A-Z0-9]{1,64}$' THEN
        RAISE EXCEPTION 'error_code non valido' USING ERRCODE = '22023';
    END IF;
    IF p_details IS NULL
       OR jsonb_typeof(p_details) <> 'object'
       OR octet_length(p_details::text) > 16384 THEN
        RAISE EXCEPTION 'details non valido' USING ERRCODE = '22023';
    END IF;

    INSERT INTO bot.reconciliation_snapshots (
        environment, exchange_ok, latency_ms, position_mode,
        open_positions_count, open_orders_count, open_algo_orders_count,
        total_equity_usdt, error_code, details
    ) VALUES (
        'DEMO', false, p_latency_ms, p_position_mode,
        p_open_positions_count, p_open_orders_count, p_open_algo_orders_count,
        p_total_equity_usdt, p_error_code, p_details
    )
    RETURNING id INTO v_snapshot_id;

    INSERT INTO bot.audit_events (event_type, details)
    VALUES (
        'RECONCILIATION_ANOMALY',
        jsonb_build_object(
            'snapshot_id', v_snapshot_id,
            'error_code',  p_error_code,
            'latency_ms',  p_latency_ms
        )
    );

    RETURN v_snapshot_id;
END;
$$;

-- Fase 2: transizione unidirezionale verso HALTED (latch)
CREATE FUNCTION bot.trip_circuit_breaker(
    p_error_code text,
    p_details    jsonb DEFAULT '{}'::jsonb
)
RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
BEGIN
    IF p_error_code IS NULL OR p_error_code !~ '^[A-Z0-9]{1,64}$' THEN
        RAISE EXCEPTION 'error_code non valido' USING ERRCODE = '22023';
    END IF;
    IF p_details IS NULL
       OR jsonb_typeof(p_details) <> 'object'
       OR octet_length(p_details::text) > 16384 THEN
        RAISE EXCEPTION 'details non valido' USING ERRCODE = '22023';
    END IF;

    UPDATE bot.system_state
       SET status         = 'HALTED',
           orders_enabled = false,
           reason         = 'CIRCUIT_BREAKER: ' || p_error_code,
           updated_at     = now()
     WHERE id = 1
       AND status <> 'HALTED';

    IF NOT FOUND THEN
        RETURN false;  -- già HALTED: nessuna azione, nessun audit duplicato
    END IF;

    INSERT INTO bot.audit_events (event_type, details)
    VALUES (
        'CIRCUIT_BREAKER_TRIPPED',
        jsonb_build_object('error_code', p_error_code, 'details', p_details)
    );

    RETURN true;
END;
$$;

ALTER FUNCTION bot.record_reconciliation_anomaly(text, integer, text, integer, integer, integer, numeric, jsonb)
    OWNER TO bot_breaker;
ALTER FUNCTION bot.trip_circuit_breaker(text, jsonb)
    OWNER TO bot_breaker;

REVOKE ALL ON FUNCTION bot.record_reconciliation_anomaly(text, integer, text, integer, integer, integer, numeric, jsonb) FROM PUBLIC;
REVOKE ALL ON FUNCTION bot.trip_circuit_breaker(text, jsonb) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION bot.record_reconciliation_anomaly(text, integer, text, integer, integer, integer, numeric, jsonb) TO core_reconciler;
GRANT EXECUTE ON FUNCTION bot.trip_circuit_breaker(text, jsonb) TO core_reconciler;

INSERT INTO bot.schema_migrations (version)
VALUES (4);

COMMIT;
