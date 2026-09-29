-- f_pgfr: a synthesized pg_flight_recorder v2 history (§14's definition of
-- done: "pgfr augmentation produces the temporal section from a synthesized
-- 7-day history, and the batch detector locates the scripted weekday spike
-- with correct phase"; extended for temporal v1.2, issue #5). Loaded by
-- conftest.py's pgfr_dsn into a container built from pgfr's own Dockerfile,
-- with pgfr_record and pgfr_analyze installed at the pinned SHA, whenever
-- this file's content digest differs from the one recorded on the database.
--
-- pgfr is append-only into uniform archive tables
-- (captured_at, key, key_hash, row_hash, schema_id, payload), payload being
-- a positional jsonb array in payload_schemas.columns order, so history is
-- synthesized by inserting rows shaped exactly like the collector's and read
-- back through pgfr's own presentation views. Rollups are NOT synthesized:
-- pgfr_record.run_tier('medium') is called at the end and closes every past
-- daily bucket that has raw rows but no rollup row yet, so the rollup path
-- (rollup_deltas, dictionary-encoded first/last values) is pgfr's real code.
-- This is a test seam, not a pgfr feature (issue #3, decision B).
--
-- What it writes, all ending at now():
--   * Group A, 7 days at 5-minute spacing: a_pg_stat_wal (wal_bytes at a
--     ~100 kB/s baseline with a mild wobble, 12x from 13:00 to 13:30 UTC
--     on weekdays: the scripted batch), a_pg_stat_database (smooth
--     counters: 50 commits/s plus 1 rollback/s, 200 blocks read/s, 10 kB/s
--     temp), a_pg_stat_bgwriter (one timed checkpoint per sample, plus a
--     requested one per sample during the batch).
--   * Group B, hourly: a_pg_stat_all_tables for 12 synthetic user tables
--     over 28 days (table i inserts 100*i, updates 100*i, deletes 50*i rows
--     per hour; n_dead_tup climbs by 150*i per hour and drops to zero every
--     6+i hours as autovacuum_count increments), and a_pg_stat_statements
--     for 25 synthetic statements over 14 days (statements 16-20 stop and
--     21-25 start exactly one week in, so the week-over-week top-set churn
--     is 10/25 = 0.4; odd statements carry a diurnal factor).
--   * Group C rollup, hourly over 28 days: r_pg_stat_activity state_active
--     and state_idle, 8 active backends during weekday business hours
--     (09:00-17:00 UTC) and 2 otherwise, at 60 samples per hour.
--   * ledger_runs: fast-tier runs every scheduled tick over 28 days minus a
--     2-hour hole on day 3, medium-tier runs likewise minus a 1-hour hole,
--     so pgfr_analyze.coverage() reports fractions just under 1.0 and
--     coverage_gaps() reports the holes.
--
-- Real captures pg_cron appended since install are deleted first for the
-- synthesized targets: interleaved with counters orders of magnitude larger
-- they would read as resets and phantom spikes. Real captures resume after
-- the synthesized series ends and read as one reset followed by tiny real
-- rates, which is harmless.
--
-- Archives are partitioned by captured_at and pgfr only creates partitions
-- ahead of now(), so the partitions the backfill needs are created here with
-- pgfr's own naming helper, matching what maintain_partitions() would have
-- made had it been running at the time.

SET TIME ZONE 'UTC';

CREATE FUNCTION pg_temp.ensure_partitions(p_base text, p_from timestamptz, p_to timestamptz)
RETURNS void
LANGUAGE plpgsql AS $$
DECLARE
    v_unit   text;
    v_logged boolean;
    v_lower  timestamptz;
    v_upper  timestamptz;
    v_child  text;
BEGIN
    SELECT pgfr_record._partition_unit(t.retention), t.logged
      INTO v_unit, v_logged
      FROM pgfr_record._partition_targets() AS t
     WHERE t.parent_table = p_base;
    IF v_unit IS NULL THEN
        RAISE EXCEPTION 'f_pgfr: % is not a pgfr_record partitioned target', p_base;
    END IF;
    v_lower := date_trunc(v_unit, p_from);
    WHILE v_lower <= p_to LOOP
        v_upper := v_lower + ('1 ' || v_unit)::interval;
        v_child := pgfr_record._partition_child_name(p_base, v_lower, v_unit);
        IF to_regclass('pgfr_record.' || quote_ident(v_child)) IS NULL THEN
            EXECUTE format(
                'CREATE %s TABLE pgfr_record.%I PARTITION OF pgfr_record.%I FOR VALUES FROM (%L) TO (%L)',
                CASE WHEN v_logged THEN '' ELSE 'UNLOGGED' END, v_child, p_base, v_lower, v_upper
            );
        END IF;
        v_lower := v_upper;
    END LOOP;
END;
$$;

-- The current capture payload schema for a source view.
CREATE FUNCTION pg_temp.capture_schema(p_view text, OUT schema_id smallint, OUT columns text[], OUT types text[])
LANGUAGE sql AS $$
    SELECT schema_id, columns, type_names
      FROM pgfr_record.payload_schemas
     WHERE source_view = p_view AND kind = 'capture'
     ORDER BY schema_id DESC
     LIMIT 1
$$;

-- A positional payload from a name-keyed object: unspecified numeric
-- columns default to 0, everything else (timestamps, text, names) to null,
-- so the presentation view's per-column casts always succeed.
CREATE FUNCTION pg_temp.payload(p_cols text[], p_types text[], p_values jsonb)
RETURNS jsonb
LANGUAGE sql AS $$
    SELECT jsonb_agg(
               coalesce(
                   p_values -> c,
                   CASE WHEN t IN ('smallint', 'integer', 'bigint', 'numeric', 'real',
                                   'double precision', 'oid', 'boolean')
                        THEN to_jsonb(0)
                        ELSE 'null'::jsonb
                   END)
               ORDER BY ord)
      FROM unnest(p_cols, p_types) WITH ORDINALITY AS u(c, t, ord)
$$;

CREATE FUNCTION pg_temp.append(p_table text, p_t timestamptz, p_key jsonb, p_schema smallint, p_payload jsonb)
RETURNS void
LANGUAGE plpgsql AS $$
BEGIN
    EXECUTE format(
        'INSERT INTO pgfr_record.%I (captured_at, key, key_hash, row_hash, schema_id, payload)
         VALUES ($1, $2, $3, $4, $5, $6)', p_table)
    USING p_t, p_key,
          CASE WHEN p_key IS NULL THEN NULL ELSE hashtextextended(p_key::text, 0) END,
          hashtextextended(p_schema::text || ':' || p_payload::text, 0),
          p_schema, p_payload;
END;
$$;

DO $$
DECLARE
    v_now        timestamptz := now();
    v_hour       timestamptz := date_trunc('hour', now());
    v_start_7d   timestamptz := now() - interval '7 days';
    v_start_14d  timestamptz := date_trunc('hour', now()) - interval '14 days';
    v_shift      timestamptz := date_trunc('hour', now()) - interval '7 days';
    v_start_28d  timestamptz := date_trunc('hour', now()) - interval '28 days';
    v_step       interval    := interval '5 minutes';
    v_elapsed    numeric     := extract(epoch FROM interval '5 minutes');
    v_reset      timestamptz := now() - interval '30 days';
    v_fast_gap   timestamptz := date_trunc('day', now() - interval '5 days') + interval '9 hours';
    v_medium_gap timestamptz := date_trunc('day', now() - interval '10 days') + interval '14 hours';
    v_fast       interval;
    v_medium     interval;
    v_datid      oid;
    v_t          timestamptz;
    v_i          int;
    v_n          int;
    v_wal        record;
    v_db         record;
    v_bgw        record;
    v_tbl        record;
    v_stm        record;
    v_wal_rate   numeric;
    v_wal_bytes  numeric := 0;
    v_commits    numeric := 0;
    v_rollbacks  numeric := 0;
    v_blks       numeric := 0;
    v_temp       numeric := 0;
    v_ckpt_timed numeric := 0;
    v_ckpt_req   numeric := 0;
    v_ins        numeric[] := array_fill(0::numeric, ARRAY[12]);
    v_upd        numeric[] := array_fill(0::numeric, ARRAY[12]);
    v_del        numeric[] := array_fill(0::numeric, ARRAY[12]);
    v_dead       numeric[] := array_fill(0::numeric, ARRAY[12]);
    v_autovac    numeric[] := array_fill(0::numeric, ARRAY[12]);
    v_seq        numeric[] := array_fill(0::numeric, ARRAY[12]);
    v_idx        numeric[] := array_fill(0::numeric, ARRAY[12]);
    v_calls      numeric[] := array_fill(0::numeric, ARRAY[25]);
    v_active     boolean;
    v_rate       numeric;
    v_backends   int;
BEGIN
    SELECT * INTO v_wal FROM pg_temp.capture_schema('pg_catalog.pg_stat_wal');
    SELECT * INTO v_db  FROM pg_temp.capture_schema('pg_catalog.pg_stat_database');
    SELECT * INTO v_bgw FROM pg_temp.capture_schema('pg_catalog.pg_stat_bgwriter');
    SELECT * INTO v_tbl FROM pg_temp.capture_schema('pg_catalog.pg_stat_all_tables');
    SELECT * INTO v_stm FROM pg_temp.capture_schema('pg_stat_statements');
    IF v_wal.schema_id IS NULL OR v_db.schema_id IS NULL OR v_bgw.schema_id IS NULL
       OR v_tbl.schema_id IS NULL OR v_stm.schema_id IS NULL THEN
        RAISE EXCEPTION 'f_pgfr: pgfr_record payload schemas not found; is pgfr_record installed with pg_stat_statements present?';
    END IF;
    SELECT oid INTO v_datid FROM pg_database WHERE datname = current_database();

    PERFORM pg_temp.ensure_partitions('a_pg_stat_wal', v_start_7d, v_now);
    PERFORM pg_temp.ensure_partitions('a_pg_stat_database', v_start_7d, v_now);
    PERFORM pg_temp.ensure_partitions('a_pg_stat_bgwriter', v_start_7d, v_now);
    PERFORM pg_temp.ensure_partitions('a_pg_stat_all_tables', v_start_28d, v_now);
    PERFORM pg_temp.ensure_partitions('a_pg_stat_statements', v_start_14d, v_now);
    PERFORM pg_temp.ensure_partitions('r_pg_stat_all_tables', v_start_28d, v_now);
    PERFORM pg_temp.ensure_partitions('r_pg_stat_statements', v_start_14d, v_now);
    PERFORM pg_temp.ensure_partitions('r_pg_stat_activity', v_start_28d, v_now);
    PERFORM pg_temp.ensure_partitions('ledger_runs', v_start_28d, v_now);

    DELETE FROM pgfr_record.a_pg_stat_wal;
    DELETE FROM pgfr_record.a_pg_stat_database;
    DELETE FROM pgfr_record.a_pg_stat_bgwriter;
    DELETE FROM pgfr_record.a_pg_stat_all_tables;
    DELETE FROM pgfr_record.a_pg_stat_statements;
    DELETE FROM pgfr_record.r_pg_stat_all_tables;
    DELETE FROM pgfr_record.r_pg_stat_statements;
    DELETE FROM pgfr_record.r_pg_stat_activity;
    DELETE FROM pgfr_record.ledger_runs WHERE tier IN ('fast', 'medium');

    -- Group A: 7 days at 5-minute spacing.
    v_t := v_start_7d;
    WHILE v_t <= v_now LOOP
        v_wal_rate := 100000 * (1 + 0.1 * sin(extract(epoch FROM v_t) / 3600.0));
        IF extract(isodow FROM v_t) <= 5
           AND v_t::time >= time '13:00' AND v_t::time < time '13:30' THEN
            v_wal_rate := v_wal_rate * 12;
            v_ckpt_req := v_ckpt_req + 1;
        END IF;
        v_wal_bytes  := v_wal_bytes + v_wal_rate * v_elapsed;
        v_commits    := v_commits + 50 * v_elapsed;
        v_rollbacks  := v_rollbacks + 1 * v_elapsed;
        v_blks       := v_blks + 200 * v_elapsed;
        v_temp       := v_temp + 10000 * v_elapsed;
        v_ckpt_timed := v_ckpt_timed + 1;

        PERFORM pg_temp.append('a_pg_stat_wal', v_t, NULL, v_wal.schema_id,
            pg_temp.payload(v_wal.columns, v_wal.types, jsonb_build_object(
                'wal_bytes',   round(v_wal_bytes),
                'wal_records', floor(v_wal_bytes / 8192),
                'wal_fpi',     floor(v_wal_bytes / 65536),
                'stats_reset', v_reset)));

        PERFORM pg_temp.append('a_pg_stat_database', v_t, jsonb_build_object('datid', v_datid::bigint), v_db.schema_id,
            pg_temp.payload(v_db.columns, v_db.types, jsonb_build_object(
                'datid',         v_datid::bigint,
                'datname',       current_database(),
                'xact_commit',   round(v_commits),
                'xact_rollback', round(v_rollbacks),
                'blks_read',     round(v_blks),
                'blks_hit',      round(v_blks * 100),
                'temp_bytes',    round(v_temp),
                'stats_reset',   v_reset)));

        PERFORM pg_temp.append('a_pg_stat_bgwriter', v_t, NULL, v_bgw.schema_id,
            pg_temp.payload(v_bgw.columns, v_bgw.types, jsonb_build_object(
                'checkpoints_timed', v_ckpt_timed,
                'checkpoints_req',   v_ckpt_req,
                'stats_reset',       v_reset)));

        v_t := v_t + v_step;
    END LOOP;

    -- Group B: 12 synthetic user tables, hourly over 28 days. Fake relids
    -- in a schema pgspec treats as user data; pgfr never resolves them.
    v_t := v_start_28d;
    v_n := 0;
    WHILE v_t <= v_hour LOOP
        v_n := v_n + 1;
        FOR v_i IN 1..12 LOOP
            v_ins[v_i]  := v_ins[v_i] + 100 * v_i;
            v_upd[v_i]  := v_upd[v_i] + 100 * v_i;
            v_del[v_i]  := v_del[v_i] + 50 * v_i;
            v_seq[v_i]  := v_seq[v_i] + 5;
            v_idx[v_i]  := v_idx[v_i] + 500 * v_i;
            v_dead[v_i] := v_dead[v_i] + 150 * v_i;
            IF v_n % (6 + v_i) = 0 THEN
                v_dead[v_i]    := 0;
                v_autovac[v_i] := v_autovac[v_i] + 1;
            END IF;
            PERFORM pg_temp.append('a_pg_stat_all_tables', v_t,
                jsonb_build_object('relid', 900000 + v_i), v_tbl.schema_id,
                pg_temp.payload(v_tbl.columns, v_tbl.types, jsonb_build_object(
                    'relid',            900000 + v_i,
                    'schemaname',       'app',
                    'relname',          format('synthetic_t%s', lpad(v_i::text, 2, '0')),
                    'seq_scan',         v_seq[v_i],
                    'idx_scan',         v_idx[v_i],
                    'n_tup_ins',        v_ins[v_i],
                    'n_tup_upd',        v_upd[v_i],
                    'n_tup_del',        v_del[v_i],
                    'n_live_tup',       100000 * v_i + v_ins[v_i] - v_del[v_i],
                    'n_dead_tup',       v_dead[v_i],
                    'autovacuum_count', v_autovac[v_i])));
        END LOOP;
        v_t := v_t + interval '1 hour';
    END LOOP;

    -- Statement mixture: 25 synthetic statements, hourly over 14 days.
    -- Debounced like pgfr: a row is written only while a statement is
    -- active (its counters changing). Statement i has base rate
    -- 1000 * (26 - i) calls per hour; odd statements carry a diurnal factor.
    v_t := v_start_14d;
    WHILE v_t <= v_hour LOOP
        FOR v_i IN 1..25 LOOP
            v_active := (v_i <= 15)
                        OR (v_i BETWEEN 16 AND 20 AND v_t < v_shift)
                        OR (v_i BETWEEN 21 AND 25 AND v_t >= v_shift);
            IF NOT v_active THEN
                CONTINUE;
            END IF;
            v_rate := 1000 * (26 - v_i);
            IF v_i % 2 = 1 THEN
                v_rate := v_rate * (1 + 0.5 * sin(2 * pi() * extract(hour FROM v_t) / 24.0));
            END IF;
            v_calls[v_i] := v_calls[v_i] + v_rate;
            PERFORM pg_temp.append('a_pg_stat_statements', v_t,
                jsonb_build_object('userid', 10, 'dbid', v_datid::bigint, 'queryid', 1000 + v_i, 'toplevel', true),
                v_stm.schema_id,
                pg_temp.payload(v_stm.columns, v_stm.types, jsonb_build_object(
                    'userid',          10,
                    'dbid',            v_datid::bigint,
                    'toplevel',        true,
                    'queryid',         1000 + v_i,
                    'query',           format('SELECT * FROM app.synthetic_t%s WHERE id = $1', lpad(v_i::text, 2, '0')),
                    'calls',           round(v_calls[v_i]),
                    'rows',            round(v_calls[v_i]),
                    'total_exec_time', round(v_calls[v_i] * 0.5, 3))));
        END LOOP;
        v_t := v_t + interval '1 hour';
    END LOOP;

    -- Group C rollup: hourly pg_stat_activity stats over 28 days, 60
    -- fast-tier samples per hour, 8 active backends in weekday business
    -- hours and 2 otherwise, plus 10 idle backends throughout.
    v_t := v_start_28d;
    WHILE v_t <= v_hour LOOP
        v_backends := CASE WHEN extract(isodow FROM v_t) <= 5
                            AND extract(hour FROM v_t) BETWEEN 9 AND 16 THEN 8 ELSE 2 END;
        INSERT INTO pgfr_record.r_pg_stat_activity (bucket_start, stat_name, value, sample_count)
        VALUES (v_t, 'state_active', v_backends * 60, (v_backends + 10) * 60),
               (v_t, 'state_idle',   10 * 60,         (v_backends + 10) * 60);
        v_t := v_t + interval '1 hour';
    END LOOP;

    -- Ledger: one run per scheduled tick, with one hole per tier.
    SELECT pgfr_record._cron_schedule_to_interval(j.schedule) INTO v_fast
      FROM cron.job AS j WHERE j.jobname = 'pgfr_tier_fast';
    SELECT pgfr_record._cron_schedule_to_interval(j.schedule) INTO v_medium
      FROM cron.job AS j WHERE j.jobname = 'pgfr_tier_medium';
    v_fast   := coalesce(v_fast, interval '1 minute');
    v_medium := coalesce(v_medium, interval '5 minutes');

    INSERT INTO pgfr_record.ledger_runs (tier, captured_at, finished_at)
    SELECT 'fast', g, g + interval '200 milliseconds'
      FROM generate_series(v_start_28d, v_now, v_fast) AS g
     WHERE g < v_fast_gap OR g >= v_fast_gap + interval '2 hours';

    INSERT INTO pgfr_record.ledger_runs (tier, captured_at, finished_at)
    SELECT 'medium', g, g + interval '1 second'
      FROM generate_series(v_start_28d, v_now, v_medium) AS g
     WHERE g < v_medium_gap OR g >= v_medium_gap + interval '1 hour';
END;
$$;

-- Close the daily Group B rollup buckets from the synthesized raw rows
-- through pgfr's own collector: run_tier() self-heals every bucket within
-- raw retention that has data but no rollup row yet.
SELECT pgfr_record.run_tier('medium');

-- Record which version of this file the database holds, so conftest.py
-- reloads when the fixture changes and skips when it hasn't. A database
-- comment rather than a table: a table would be captured by pgfr as a user
-- relation and pollute the Group B metrics this fixture exists to test.
COMMENT ON DATABASE :"DBNAME" IS :'digest';
