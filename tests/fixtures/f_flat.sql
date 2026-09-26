-- f_flat (§12): denormalized single-wide-table app. No foreign keys at all,
-- the structural opposite of f_star.

CREATE EXTENSION IF NOT EXISTS pg_stat_statements;

CREATE TABLE flat_events (
    id bigserial PRIMARY KEY,
    user_id integer,
    session_id text,
    event_type text,
    event_ts timestamptz,
    device text,
    os text,
    browser text,
    country text,
    region text,
    city text,
    referrer text,
    utm_source text,
    utm_medium text,
    utm_campaign text,
    revenue numeric,
    quantity integer,
    metadata_a text,
    metadata_b text,
    metadata_c text
);

INSERT INTO flat_events (
    user_id, session_id, event_type, event_ts, device, os, browser,
    country, region, city, referrer, utm_source, utm_medium, utm_campaign,
    revenue, quantity, metadata_a, metadata_b, metadata_c
)
SELECT
    (random() * 9999 + 1)::int,
    'session_' || (random() * 99999)::int,
    (ARRAY['pageview', 'click', 'purchase', 'signup'])[(random() * 3 + 1)::int],
    timestamptz '2026-01-01' + (i || ' seconds')::interval,
    (ARRAY['desktop', 'mobile', 'tablet'])[(random() * 2 + 1)::int],
    (ARRAY['macos', 'windows', 'linux', 'ios', 'android'])[(random() * 4 + 1)::int],
    (ARRAY['chrome', 'firefox', 'safari', 'edge'])[(random() * 3 + 1)::int],
    (ARRAY['us', 'gb', 'de', 'jp', 'br'])[(random() * 4 + 1)::int],
    'region_' || (random() * 49)::int,
    'city_' || (random() * 999)::int,
    'https://example.test/' || (random() * 999)::int,
    (ARRAY['google', 'facebook', 'direct', 'email'])[(random() * 3 + 1)::int],
    (ARRAY['cpc', 'organic', 'referral'])[(random() * 2 + 1)::int],
    'campaign_' || (random() * 19)::int,
    (random() * 200)::numeric(10, 2),
    (random() * 5 + 1)::int,
    'meta_a_' || i,
    'meta_b_' || i,
    'meta_c_' || i
FROM generate_series(1, 100000) i;

ANALYZE;
