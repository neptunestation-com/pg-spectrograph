-- f_writeheavy (§12): update-churn table with low HOT fraction and
-- dead-tuple pressure. indexed_value has its own index, so updating it
-- defeats HOT (a HOT update requires no indexed column to change); the
-- repeated UPDATEs with no VACUUM in between build up dead tuples.

CREATE EXTENSION IF NOT EXISTS pg_stat_statements;

CREATE TABLE writeheavy_counters (
    id bigserial PRIMARY KEY,
    counter_value integer NOT NULL DEFAULT 0,
    indexed_value integer NOT NULL DEFAULT 0
);
CREATE INDEX writeheavy_counters_indexed_value_idx ON writeheavy_counters (indexed_value);

-- Autovacuum would otherwise eventually clean up the dead tuples this
-- fixture exists to demonstrate, making the scenario non-deterministic
-- over time (confirmed live: a passing test run turned up 0 dead tuples
-- on a later run against the same, by-then-longer-lived database).
ALTER TABLE writeheavy_counters SET (autovacuum_enabled = false);

INSERT INTO writeheavy_counters (counter_value, indexed_value)
SELECT i, i FROM generate_series(1, 10000) i;

ANALYZE;

-- Deliberately no VACUUM between these: every pass leaves the previous
-- pass's rows as dead tuples, and every update touches the indexed column,
-- so none of them can be HOT.
UPDATE writeheavy_counters SET indexed_value = indexed_value + 1;
UPDATE writeheavy_counters SET indexed_value = indexed_value + 1;
UPDATE writeheavy_counters SET indexed_value = indexed_value + 1;
UPDATE writeheavy_counters SET indexed_value = indexed_value + 1;
UPDATE writeheavy_counters SET indexed_value = indexed_value + 1;
