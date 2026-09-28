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
--
-- Each table gets 300 rows of random val, more than the default statistics
-- target of 100 distinct values, so ANALYZE emits full 101-bound
-- histograms rather than the 10-bound ones a 10-row table produces, and
-- the histograms differ per table: identical arrays repeated across 5,000
-- tables gzip down to almost nothing and understate the artifact size by
-- about 5x (confirmed live with sequential val). Seeded so loads are
-- reproducible. That is what makes the §13.4 artifact size budget
-- assertion in test_performance.py meaningful.

CREATE EXTENSION IF NOT EXISTS pg_stat_statements;

SELECT setseed(0.42);

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
            'SELECT (random() * 1000000)::int, ''row_'' || g '
            'FROM generate_series(1, 300) g',
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
