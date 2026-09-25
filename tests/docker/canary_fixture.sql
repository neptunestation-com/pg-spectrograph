-- Minimal canary fixture (Milestone 3): a small schema seeded with
-- distinctive literal values and identifier names, per §12's "no-values" and
-- "no-identifiers" canary scans. Every table/column/identifier name here, and
-- every literal value, is checked to never appear anywhere in a captured
-- artifact.

CREATE EXTENSION IF NOT EXISTS pg_stat_statements;

CREATE TABLE public.xq_secret_salaries (
    id serial PRIMARY KEY,
    employee_email text,
    employee_name text,
    salary_dollars numeric,
    external_uuid uuid
);

INSERT INTO public.xq_secret_salaries
    (employee_email, employee_name, salary_dollars, external_uuid)
VALUES
    ('alice.wonderland@canary-example.test', 'Alice Wonderland', 123456.78,
     '11111111-1111-1111-1111-111111111111'),
    ('bob.builder@canary-example.test', 'Bob Builder', 98765.43,
     '22222222-2222-2222-2222-222222222222'),
    ('carol.danvers@canary-example.test', 'Carol Danvers', 55555.55,
     '33333333-3333-3333-3333-333333333333');

ANALYZE public.xq_secret_salaries;
