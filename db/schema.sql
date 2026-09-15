-- InstaWard Turso (libSQL) schema — mirror of app/db.py SCHEMA_SQL.
-- Applied automatically on boot (lifespan → init_schema). This file is for
-- manual use: fresh DBs, reviews, or `turso db shell <name> < db/schema.sql`.
-- NOTE: an early manual `uploaded_media` table may exist in older databases;
-- the app does not read it. The tables below are the live ones.

CREATE TABLE IF NOT EXISTS processed_posts (
    code TEXT PRIMARY KEY,
    source_pk TEXT NOT NULL,
    source_username TEXT NOT NULL,
    repost_pk TEXT,
    repost_code TEXT,
    posted_at INTEGER NOT NULL,
    archived INTEGER DEFAULT 0,
    archive_scanned INTEGER DEFAULT 0,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_processed_posted ON processed_posts(posted_at);
CREATE INDEX IF NOT EXISTS idx_processed_archive ON processed_posts(archived, archive_scanned, posted_at);

CREATE TABLE IF NOT EXISTS ig_sessions (
    username TEXT PRIMARY KEY,
    settings_json TEXT NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS pacing_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS login_breaker (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    blocked_until INTEGER DEFAULT 0,
    failure_count INTEGER DEFAULT 0,
    last_failure_at INTEGER DEFAULT 0
);
