-- 001_init.sql — Technical Reference v2 §5.5 (MVP schema)
-- Applied automatically on first boot via postgres docker-entrypoint-initdb.d.
-- Later migrations: add NNN_name.sql here; a real migration runner (alembic)
-- is deferred until the schema actually changes post-M0.

BEGIN;

CREATE TABLE IF NOT EXISTS schema_migrations (
    version     TEXT PRIMARY KEY,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS queries (
    query_id     UUID PRIMARY KEY,
    question     TEXT        NOT NULL,
    aoi          JSONB       NOT NULL,           -- GeoJSON Polygon, §6.1
    aoi_hash     TEXT        NOT NULL,           -- §6.2
    dates_start  DATE,
    dates_end    DATE,
    depth        TEXT        NOT NULL DEFAULT 'standard'
                 CHECK (depth IN ('quick','standard','thorough')),
    status       TEXT        NOT NULL DEFAULT 'received'
                 CHECK (status IN ('received','routing','downloading','analyzing',
                                   'summarizing','done','failed','needs_clarification')),
    answer       TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_queries_status   ON queries (status);
CREATE INDEX IF NOT EXISTS idx_queries_aoi_hash ON queries (aoi_hash);

CREATE TABLE IF NOT EXISTS tasks (
    task_id     UUID PRIMARY KEY,
    query_id    UUID        NOT NULL REFERENCES queries (query_id) ON DELETE CASCADE,
    kind        TEXT        NOT NULL CHECK (kind IN ('download','analysis')),
    name        TEXT        NOT NULL,            -- e.g. wrap_licsbas, download_licsar
    status      TEXT        NOT NULL DEFAULT 'queued'
                CHECK (status IN ('queued','running','done','failed')),
    retries     INT         NOT NULL DEFAULT 0,
    error       TEXT,
    result_path TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_tasks_query_id ON tasks (query_id);
CREATE INDEX IF NOT EXISTS idx_tasks_status   ON tasks (status);

CREATE TABLE IF NOT EXISTS cached_data (
    id            SERIAL PRIMARY KEY,
    aoi_hash      TEXT        NOT NULL,
    dates_start   DATE        NOT NULL,
    dates_end     DATE        NOT NULL,
    product_type  TEXT        NOT NULL,          -- egms|licsar|hyp3|s1|s2|aux
    file_paths    JSONB       NOT NULL,
    checksums     JSONB       NOT NULL DEFAULT '{}'::jsonb,
    expiry_ts     TIMESTAMPTZ NOT NULL,
    last_accessed TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Cache lookup: aoi_hash equal AND requested range within cached range AND
-- product type equal (§6.2). This index serves that probe.
CREATE INDEX IF NOT EXISTS idx_cache_lookup
    ON cached_data (aoi_hash, product_type, dates_start, dates_end);
CREATE INDEX IF NOT EXISTS idx_cache_lru ON cached_data (last_accessed);

-- updated_at maintenance
CREATE OR REPLACE FUNCTION touch_updated_at() RETURNS trigger AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_queries_touch ON queries;
CREATE TRIGGER trg_queries_touch BEFORE UPDATE ON queries
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
DROP TRIGGER IF EXISTS trg_tasks_touch ON tasks;
CREATE TRIGGER trg_tasks_touch BEFORE UPDATE ON tasks
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

INSERT INTO schema_migrations (version) VALUES ('001_init')
    ON CONFLICT (version) DO NOTHING;

COMMIT;
