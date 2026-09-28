-- f_pgfr: a synthesized 7-day pg_flight_recorder v2 history (§14's
-- definition of done: "pgfr augmentation produces the temporal section from
-- a synthesized 7-day history, and the batch detector locates the scripted
-- weekday spike with correct phase"). Loaded once by conftest.py's pgfr_dsn
-- into a container built from pgfr's own Dockerfile, with pgfr_record and
-- pgfr_analyze installed at the pinned SHA.
--
-- pgfr is append-only into uniform archive tables
-- (captured_at, key, key_hash, row_hash, schema_id, payload), payload being
-- a positional jsonb array in payload_schemas.columns order, so history is
-- synthesized by inserting rows shaped exactly like the collector's and read
-- back through pgfr's own presentation views. This is a test seam, not a
-- pgfr feature (issue #3, decision B).
--
-- What it writes, all ending at now():
--   * a_pg_stat_wal: 7 days at 5-minute spacing. wal_bytes grows at a
--     ~100 kB/s baseline with a mild deterministic wobble, and at 12x that
--     rate from 13:00 to 13:30 UTC on weekdays (the scripted batch).
--   * a_pg_stat_database: the same 7 days for this database, smooth
--     counters (50 commits/s plus 1 rollback/s, 200 blocks read/s, 10 kB/s
--     of temp), so tps / blks_read_rate / temp_bytes_rate get a real
--     series too.
--   * ledger_runs: one fast-tier run per scheduled tick over the same
--     window, minus a 2-hour hole on day 3, so pgfr_analyze.coverage()
--     reports a fraction just under 1.0 and coverage_gaps() reports the
--     hole.
--
-- The few real captures pg_cron has already appended between install and
-- this load are deleted first: interleaved with a synthesized counter that
-- is orders of magnitude larger they would read as resets and phantom
-- spikes. Real captures resume appending after the synthesized series ends
-- and read as one reset followed by tiny real rates, which is harmless.
--
-- Group A archives are monthly-partitioned and pgfr only creates partitions
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

DO $$
DECLARE
    v_end        timestamptz := now();
    v_start      timestamptz := now() - interval '7 days';
    v_step       interval    := interval '5 minutes';
    v_elapsed    numeric     := extract(epoch FROM interval '5 minutes');
    v_reset      timestamptz := now() - interval '8 days';
    v_gap_start  timestamptz := date_trunc('day', now() - interval '5 days') + interval '9 hours';
    v_gap_end    timestamptz;
    v_fast       interval;
    v_wal_schema smallint;
    v_wal_cols   text[];
    v_db_schema  smallint;
    v_db_cols    text[];
    v_datid      oid;
    v_key        jsonb;
    v_t          timestamptz;
    v_wal_rate   numeric;
    v_wal_bytes  numeric := 0;
    v_commits    numeric := 0;
    v_rollbacks  numeric := 0;
    v_blks       numeric := 0;
    v_temp       numeric := 0;
    v_payload    jsonb;
BEGIN
    v_gap_end := v_gap_start + interval '2 hours';

    SELECT schema_id, columns INTO v_wal_schema, v_wal_cols
      FROM pgfr_record.payload_schemas
     WHERE source_view = 'pg_catalog.pg_stat_wal' AND kind = 'capture'
     ORDER BY schema_id DESC LIMIT 1;
    SELECT schema_id, columns INTO v_db_schema, v_db_cols
      FROM pgfr_record.payload_schemas
     WHERE source_view = 'pg_catalog.pg_stat_database' AND kind = 'capture'
     ORDER BY schema_id DESC LIMIT 1;
    IF v_wal_schema IS NULL OR v_db_schema IS NULL THEN
        RAISE EXCEPTION 'f_pgfr: pgfr_record payload schemas not found; is pgfr_record installed?';
    END IF;

    SELECT oid INTO v_datid FROM pg_database WHERE datname = current_database();
    v_key := jsonb_build_object('datid', v_datid::bigint);

    PERFORM pg_temp.ensure_partitions('a_pg_stat_wal', v_start, v_end);
    PERFORM pg_temp.ensure_partitions('a_pg_stat_database', v_start, v_end);
    PERFORM pg_temp.ensure_partitions('ledger_runs', v_start, v_end);

    DELETE FROM pgfr_record.a_pg_stat_wal;
    DELETE FROM pgfr_record.a_pg_stat_database;
    DELETE FROM pgfr_record.ledger_runs WHERE tier = 'fast';

    v_t := v_start;
    WHILE v_t <= v_end LOOP
        v_wal_rate := 100000 * (1 + 0.1 * sin(extract(epoch FROM v_t) / 3600.0));
        IF extract(isodow FROM v_t) <= 5
           AND v_t::time >= time '13:00' AND v_t::time < time '13:30' THEN
            v_wal_rate := v_wal_rate * 12;
        END IF;
        v_wal_bytes := v_wal_bytes + v_wal_rate * v_elapsed;
        v_commits   := v_commits + 50 * v_elapsed;
        v_rollbacks := v_rollbacks + 1 * v_elapsed;
        v_blks      := v_blks + 200 * v_elapsed;
        v_temp      := v_temp + 10000 * v_elapsed;

        SELECT jsonb_agg(
                   CASE c
                       WHEN 'wal_bytes'   THEN to_jsonb(round(v_wal_bytes))
                       WHEN 'wal_records' THEN to_jsonb(floor(v_wal_bytes / 8192))
                       WHEN 'wal_fpi'     THEN to_jsonb(floor(v_wal_bytes / 65536))
                       WHEN 'stats_reset' THEN to_jsonb(v_reset)
                       ELSE to_jsonb(0)
                   END ORDER BY ord)
          INTO v_payload
          FROM unnest(v_wal_cols) WITH ORDINALITY AS u(c, ord);
        INSERT INTO pgfr_record.a_pg_stat_wal
            (captured_at, key, key_hash, row_hash, schema_id, payload)
        VALUES
            (v_t, NULL, NULL,
             hashtextextended(v_wal_schema::text || ':' || v_payload::text, 0),
             v_wal_schema, v_payload);

        SELECT jsonb_agg(
                   CASE c
                       WHEN 'datid'                 THEN to_jsonb(v_datid::bigint)
                       WHEN 'datname'               THEN to_jsonb(current_database())
                       WHEN 'xact_commit'           THEN to_jsonb(round(v_commits))
                       WHEN 'xact_rollback'         THEN to_jsonb(round(v_rollbacks))
                       WHEN 'blks_read'             THEN to_jsonb(round(v_blks))
                       WHEN 'blks_hit'              THEN to_jsonb(round(v_blks * 100))
                       WHEN 'temp_bytes'            THEN to_jsonb(round(v_temp))
                       WHEN 'stats_reset'           THEN to_jsonb(v_reset)
                       WHEN 'checksum_last_failure' THEN 'null'::jsonb
                       ELSE to_jsonb(0)
                   END ORDER BY ord)
          INTO v_payload
          FROM unnest(v_db_cols) WITH ORDINALITY AS u(c, ord);
        INSERT INTO pgfr_record.a_pg_stat_database
            (captured_at, key, key_hash, row_hash, schema_id, payload)
        VALUES
            (v_t, v_key, hashtextextended(v_key::text, 0),
             hashtextextended(v_db_schema::text || ':' || v_payload::text, 0),
             v_db_schema, v_payload);

        v_t := v_t + v_step;
    END LOOP;

    SELECT pgfr_record._cron_schedule_to_interval(j.schedule) INTO v_fast
      FROM cron.job AS j
     WHERE j.jobname = 'pgfr_tier_fast';
    v_fast := coalesce(v_fast, interval '1 minute');

    INSERT INTO pgfr_record.ledger_runs (tier, captured_at, finished_at)
    SELECT 'fast', g, g + interval '200 milliseconds'
      FROM generate_series(v_start, v_end, v_fast) AS g
     WHERE g < v_gap_start OR g >= v_gap_end;
END;
$$;
