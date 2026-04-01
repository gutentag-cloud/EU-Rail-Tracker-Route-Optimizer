-- Enable PostGIS
CREATE EXTENSION IF NOT EXISTS postgis;

-- ── Stations ────────────────────────────────────────
CREATE TABLE IF NOT EXISTS stations (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    country     TEXT DEFAULT '',
    db_id       TEXT,
    uic         TEXT,
    is_main     BOOLEAN DEFAULT FALSE,
    geom        GEOMETRY(Point, 4326),
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_stations_geom
    ON stations USING GIST (geom);
CREATE INDEX IF NOT EXISTS idx_stations_name
    ON stations USING GIN (name gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_stations_country
    ON stations (country);
CREATE INDEX IF NOT EXISTS idx_stations_db_id
    ON stations (db_id);

-- Enable trigram extension for fuzzy search
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- ── Connections (edges) ─────────────────────────────
CREATE TABLE IF NOT EXISTS connections (
    id              SERIAL PRIMARY KEY,
    from_station_id TEXT REFERENCES stations(id),
    to_station_id   TEXT REFERENCES stations(id),
    distance_km     DOUBLE PRECISION,
    duration_min    DOUBLE PRECISION,
    operator        TEXT DEFAULT 'db',
    line_geom       GEOMETRY(LineString, 4326),
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (from_station_id, to_station_id, operator)
);

CREATE INDEX IF NOT EXISTS idx_conn_from ON connections (from_station_id);
CREATE INDEX IF NOT EXISTS idx_conn_to   ON connections (to_station_id);

-- ── Delay Records ───────────────────────────────────
CREATE TABLE IF NOT EXISTS delay_records (
    id          SERIAL PRIMARY KEY,
    station_id  TEXT REFERENCES stations(id),
    trip_id     TEXT,
    line_name   TEXT,
    delay_sec   INTEGER NOT NULL DEFAULT 0,
    recorded_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_delay_station
    ON delay_records (station_id);
CREATE INDEX IF NOT EXISTS idx_delay_time
    ON delay_records (recorded_at);

-- ── Track Geometry Cache ────────────────────────────
CREATE TABLE IF NOT EXISTS track_geometry (
    id          SERIAL PRIMARY KEY,
    from_lat    DOUBLE PRECISION,
    from_lon    DOUBLE PRECISION,
    to_lat      DOUBLE PRECISION,
    to_lon      DOUBLE PRECISION,
    geom        GEOMETRY(MultiLineString, 4326),
    fetched_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_track_geom
    ON track_geometry USING GIST (geom);

-- ── Materialized view: station delay stats ──────────
CREATE MATERIALIZED VIEW IF NOT EXISTS station_delay_stats AS
SELECT
    station_id,
    COUNT(*)                              AS total_records,
    AVG(delay_sec)                        AS avg_delay_sec,
    PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY delay_sec) AS median_delay_sec,
    MAX(delay_sec)                        AS max_delay_sec,
    COUNT(*) FILTER (WHERE delay_sec > 300) AS delayed_5min_count,
    COUNT(*) FILTER (WHERE delay_sec > 0)   AS any_delay_count
FROM delay_records
WHERE recorded_at > NOW() - INTERVAL '24 hours'
GROUP BY station_id;

-- Refresh periodically via cron or app
-- REFRESH MATERIALIZED VIEW station_delay_stats;
