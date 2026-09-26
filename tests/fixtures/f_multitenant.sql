-- f_multitenant (§12): LIST-partitioned tenant-per-partition, including a
-- DEFAULT partition (a case the canary fixture's own RANGE-partitioned
-- table doesn't cover: default_partition_exists is always False there).

CREATE EXTENSION IF NOT EXISTS pg_stat_statements;

CREATE TABLE tenant_data (
    id bigserial,
    tenant_id text NOT NULL,
    payload text,
    created_at timestamptz NOT NULL DEFAULT now()
) PARTITION BY LIST (tenant_id);

CREATE TABLE tenant_data_acme PARTITION OF tenant_data FOR VALUES IN ('acme');
CREATE TABLE tenant_data_globex PARTITION OF tenant_data FOR VALUES IN ('globex');
CREATE TABLE tenant_data_initech PARTITION OF tenant_data FOR VALUES IN ('initech');
CREATE TABLE tenant_data_default PARTITION OF tenant_data DEFAULT;

INSERT INTO tenant_data (tenant_id, payload)
SELECT
    (ARRAY['acme', 'globex', 'initech', 'some_unlisted_tenant'])[(random() * 3 + 1)::int],
    'payload_' || i
FROM generate_series(1, 30000) i;

ANALYZE;
