-- 0010_search_core.sql — search core (umbrella #570, S5 #576): what a persisted search needs beyond S0's tables.
--   search_session.meta    sites, limit, flags, per-site timings, errors and warnings of the run (JSON object)
--   search_result.record   the full normalised result record (the `--json` card), so `3d models searches show`
--                          round-trips what the search printed
--   model_source.role      part role of a downloaded model (printed|vitamin|reference|stand_in, docs/contracts/
--                          parts.md); NULL = the user did not say
--   model_source.vitamin_kind  pcb|connector|screw|wire|... for a vitamin/reference download
--   model_source.analytics deterministic file analytics of the downloaded files (JSON object)
--   analytics_cache        deep (trimesh) analytics keyed by marketplace file (site:model:name:size) and by
--                          content sha256, so a second `--deep` run makes no network call
ALTER TABLE search_session ADD COLUMN meta TEXT NOT NULL DEFAULT '{}'
    CHECK (json_valid(meta) AND json_type(meta) = 'object');

ALTER TABLE search_result ADD COLUMN record TEXT NOT NULL DEFAULT '{}'
    CHECK (json_valid(record) AND json_type(record) = 'object');

ALTER TABLE model_source ADD COLUMN role TEXT
    CHECK (role IS NULL OR role IN ('printed', 'vitamin', 'reference', 'stand_in'));

ALTER TABLE model_source ADD COLUMN vitamin_kind TEXT;

ALTER TABLE model_source ADD COLUMN analytics TEXT NOT NULL DEFAULT '{}'
    CHECK (json_valid(analytics) AND json_type(analytics) = 'object');

CREATE TABLE analytics_cache (
    key         TEXT PRIMARY KEY,
    sha256      TEXT,
    analytics   TEXT NOT NULL CHECK (json_valid(analytics) AND json_type(analytics) = 'object'),
    created_at  TEXT NOT NULL
);

CREATE INDEX analytics_cache_by_sha ON analytics_cache(sha256);
