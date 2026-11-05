-- Universal dataset schema. `id` is "<source>:<ref>", e.g. "kaggle:owner/slug",
-- so Hugging Face / UCI adapters can share these tables later.

CREATE TABLE IF NOT EXISTS datasets (
    id                 TEXT PRIMARY KEY,
    source             TEXT NOT NULL,
    ref                TEXT NOT NULL,
    title              TEXT,
    subtitle           TEXT,
    description        TEXT,
    owner              TEXT,
    creator            TEXT,
    url                TEXT,
    license            TEXT,
    size_bytes         INTEGER,
    download_count     INTEGER,
    vote_count         INTEGER,
    view_count         INTEGER,
    usability          REAL,
    last_updated       TEXT,     -- ISO 8601
    fetched_at         TEXT,     -- when list metadata was last synced
    details_fetched_at TEXT      -- when files/description were last synced
);

CREATE INDEX IF NOT EXISTS idx_datasets_source  ON datasets(source);
CREATE INDEX IF NOT EXISTS idx_datasets_votes   ON datasets(vote_count);
CREATE INDEX IF NOT EXISTS idx_datasets_updated ON datasets(last_updated);

CREATE TABLE IF NOT EXISTS dataset_tags (
    dataset_id TEXT NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
    tag        TEXT NOT NULL,
    PRIMARY KEY (dataset_id, tag)
);
CREATE INDEX IF NOT EXISTS idx_tags_tag ON dataset_tags(tag);

CREATE TABLE IF NOT EXISTS dataset_formats (
    dataset_id TEXT NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
    format     TEXT NOT NULL,
    PRIMARY KEY (dataset_id, format)
);

CREATE TABLE IF NOT EXISTS dataset_files (
    dataset_id TEXT NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
    name       TEXT NOT NULL,
    size_bytes INTEGER,
    PRIMARY KEY (dataset_id, name)
);

CREATE TABLE IF NOT EXISTS bookmarks (
    dataset_id TEXT NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
    added_by   TEXT NOT NULL,
    note       TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (dataset_id, added_by)
);

CREATE TABLE IF NOT EXISTS refresh_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    params      TEXT,
    fetched     INTEGER DEFAULT 0,
    new_count   INTEGER DEFAULT 0,
    status      TEXT,
    message     TEXT
);

-- Small key/value store, e.g. which seed snapshot this database was built from.
CREATE TABLE IF NOT EXISTS app_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
