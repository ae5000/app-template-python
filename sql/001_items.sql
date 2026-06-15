-- Items table — created on startup, safe to re-run (IF NOT EXISTS).
-- Add new columns in subsequent numbered files (002_*.sql, etc.).
-- Never DROP or TRUNCATE in migration files.

CREATE TABLE IF NOT EXISTS items (
    id         TEXT        PRIMARY KEY,
    name       TEXT        NOT NULL,
    value      TEXT        NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
