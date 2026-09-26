-- f_perf_5k_tables (§12 "Performance" acceptance criterion): f_star
-- inflated to 5,000 tables, proving pgspec's single-pass, set-based
-- catalog queries scale with table count rather than doing a per-table
-- round trip (§11.5's "huge schemas" edge case).
--
-- Creating 5,000 tables inside one transaction exhausts
-- max_locks_per_transaction (each CREATE TABLE holds an ACCESS EXCLUSIVE
-- lock until the transaction ends): confirmed live ("out of shared
-- memory... You might need to increase max_locks_per_transaction") rather
-- than assumed. Fixed with a real PROCEDURE (not a DO block, which cannot
-- COMMIT internally) that commits every 250 tables to release locks as it
-- goes. This must be loaded via psql (each statement dispatched as its own
-- top-level message), not as one multi-statement string: the simple query
-- protocol wraps a whole multi-statement string in an implicit
-- transaction, which would break CALL's internal COMMIT the same way a
-- multi-statement dispatch breaks DETACH PARTITION CONCURRENTLY.

CREATE EXTENSION IF NOT EXISTS pg_stat_statements;

CREATE PROCEDURE build_perf_tables() LANGUAGE plpgsql AS $$
DECLARE
    i integer;
BEGIN
    FOR i IN 0..4999 LOOP
        EXECUTE format(
            'CREATE TABLE perf_table_%1$s (id serial PRIMARY KEY, val integer, txt text)',
            lpad(i::text, 5, '0')
        );
        EXECUTE format(
            'INSERT INTO perf_table_%1$s (val, txt) '
            'SELECT g, ''row_'' || g FROM generate_series(1, 10) g',
            lpad(i::text, 5, '0')
        );
        IF i % 250 = 0 THEN
            COMMIT;
        END IF;
    END LOOP;
END;
$$;

CALL build_perf_tables();
DROP PROCEDURE build_perf_tables();

ANALYZE;
