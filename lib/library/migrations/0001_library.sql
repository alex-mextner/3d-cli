-- 0001_library.sql — the local library database (umbrella #570, S0 #571; docs/library-db.md).
-- A separate SQLite file (data_dir()/library/library.db), NOT the PrintOps database: it must work offline
-- for agents and never share a migration range with 3d-api. Own numbering 0001.. (checksummed by
-- printops.db.migrate like every PrintOps migration).
--
-- Conventions: `uid` is a full UUIDv7 (never uid[:8], #452); every timestamp is ISO-8601 UTC with ms
-- (printops.db.utc_now); JSON columns carry json_valid CHECKs; `source_key` is the idempotency key of an
-- import (a second import of the same key finds the first row); relations point at rows by full uid because a
-- relation endpoint can be a design, a make or a model source.

-- Same shape as PrintOps `events` so printops.events.emit works on this connection unchanged.
CREATE TABLE events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    kind TEXT NOT NULL,
    subject TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(payload) AND json_type(payload) = 'object'),
    actor TEXT NOT NULL
);

CREATE INDEX events_kind_id ON events (kind, id);
CREATE INDEX events_subject_id ON events (subject, id);

CREATE TRIGGER events_no_update BEFORE UPDATE ON events
BEGIN
    SELECT RAISE(ABORT, 'events are append-only');
END;

CREATE TRIGGER events_no_delete BEFORE DELETE ON events
BEGIN
    SELECT RAISE(ABORT, 'events are append-only');
END;

CREATE TABLE search_session (
    id          INTEGER PRIMARY KEY,
    uid         TEXT NOT NULL UNIQUE,
    source_key  TEXT UNIQUE,
    query       TEXT NOT NULL,
    parent_id   INTEGER REFERENCES search_session(id) ON DELETE SET NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('wide', 'narrow', 'followup', 'rewrite', 'user')),
    created_at  TEXT NOT NULL
);

CREATE INDEX search_session_by_parent ON search_session(parent_id);

CREATE TABLE search_result (
    id          INTEGER PRIMARY KEY,
    uid         TEXT NOT NULL UNIQUE,
    source_key  TEXT UNIQUE,
    session_id  INTEGER NOT NULL REFERENCES search_session(id) ON DELETE CASCADE,
    site        TEXT NOT NULL,
    model_id    TEXT NOT NULL,
    url         TEXT NOT NULL,
    title       TEXT NOT NULL,
    rank        INTEGER,
    score       REAL,
    verdict     TEXT,
    rationale   TEXT,
    analytics   TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(analytics)),
    image_url   TEXT,
    created_at  TEXT NOT NULL,
    UNIQUE (session_id, site, model_id)
);

CREATE TABLE model_source (
    id          INTEGER PRIMARY KEY,
    uid         TEXT NOT NULL UNIQUE,
    source_key  TEXT UNIQUE,
    site        TEXT NOT NULL,
    model_id    TEXT NOT NULL,
    url         TEXT NOT NULL,
    license     TEXT,
    author      TEXT,
    dir         TEXT,
    files       TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(files)),
    sha256      TEXT,
    created_at  TEXT NOT NULL,
    UNIQUE (site, model_id)
);

CREATE TABLE design (
    id          INTEGER PRIMARY KEY,
    uid         TEXT NOT NULL UNIQUE,
    source_key  TEXT UNIQUE,
    name        TEXT NOT NULL UNIQUE,
    project_dir TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE make (
    id          INTEGER PRIMARY KEY,
    uid         TEXT NOT NULL UNIQUE,
    source_key  TEXT UNIQUE,
    design_id   INTEGER NOT NULL REFERENCES design(id) ON DELETE CASCADE,
    source_id   INTEGER REFERENCES model_source(id) ON DELETE SET NULL,
    printer     TEXT,
    filament    TEXT,
    status      TEXT NOT NULL CHECK (status IN ('queued', 'printing', 'done', 'failed')),
    queued_at   TEXT NOT NULL,
    finished_at TEXT,
    gcode       TEXT,
    created_at  TEXT NOT NULL
);

CREATE INDEX make_by_design ON make(design_id);
CREATE INDEX make_by_status ON make(status, queued_at);

CREATE TABLE photo (
    id          INTEGER PRIMARY KEY,
    uid         TEXT NOT NULL UNIQUE,
    source_key  TEXT UNIQUE,
    make_id     INTEGER REFERENCES make(id) ON DELETE CASCADE,
    design_id   INTEGER REFERENCES design(id) ON DELETE CASCADE,
    path        TEXT NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('table', 'bed', 'render')),
    z_mm        REAL,
    taken_at    TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    CHECK (make_id IS NOT NULL OR design_id IS NOT NULL)
);

CREATE INDEX photo_by_make ON photo(make_id);
CREATE INDEX photo_by_design ON photo(design_id);

CREATE TABLE publication (
    id          INTEGER PRIMARY KEY,
    uid         TEXT NOT NULL UNIQUE,
    source_key  TEXT UNIQUE,
    design_id   INTEGER NOT NULL REFERENCES design(id) ON DELETE CASCADE,
    platform    TEXT NOT NULL,
    url         TEXT,
    state       TEXT NOT NULL CHECK (state IN ('planned', 'draft', 'published', 'failed', 'removed')),
    created_at  TEXT NOT NULL
);

CREATE INDEX publication_by_design ON publication(design_id, platform);

CREATE TABLE relation (
    id          INTEGER PRIMARY KEY,
    uid         TEXT NOT NULL UNIQUE,
    source_key  TEXT UNIQUE,
    from_kind   TEXT NOT NULL CHECK (from_kind IN ('design', 'make', 'model_source')),
    from_id     TEXT NOT NULL,                       -- full uid of the row of from_kind
    to_kind     TEXT NOT NULL CHECK (to_kind IN ('design', 'make', 'model_source')),
    to_id       TEXT NOT NULL,
    rel         TEXT NOT NULL CHECK (rel IN ('remix_of', 'make_of', 'derived_from')),
    created_at  TEXT NOT NULL,
    UNIQUE (from_kind, from_id, to_kind, to_id, rel),
    CHECK (from_kind <> to_kind OR from_id <> to_id)
);

CREATE INDEX relation_by_to ON relation(to_kind, to_id);
