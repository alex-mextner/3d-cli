-- 0040_drying.sql — drying sessions (#162, plan §5.2): which spools went into which dryer, when,
-- at what temperature, and the optional humidity/weight readings before and after.
-- Spool state that frontends display (`last_dried_at`, `dry_state`) lives in Spoolman extra
-- fields; these tables are the history behind it.

CREATE TABLE drying_session (
    id                INTEGER PRIMARY KEY,
    dryer_location_id INTEGER NOT NULL REFERENCES location(id),
    started_at        TEXT NOT NULL,                      -- ISO-8601 UTC (db.utc_now format)
    ended_at          TEXT,                               -- NULL while the session is open
    temp_c            REAL NOT NULL CHECK (temp_c BETWEEN 20 AND 150),
    target_h          REAL NOT NULL CHECK (target_h > 0 AND target_h <= 96),
    rh_before         REAL CHECK (rh_before BETWEEN 0 AND 100),
    rh_after          REAL CHECK (rh_after BETWEEN 0 AND 100),
    source            TEXT NOT NULL,                      -- cli | api | ha | voice | station | bot
    note              TEXT,
    actor             TEXT NOT NULL,                      -- token name that started it
    finished_by       TEXT,                               -- token name that finished it
    timer_event_id    INTEGER REFERENCES events(id),      -- the `drying.finished` {trigger: timer} event
    CHECK (ended_at IS NULL OR ended_at >= started_at)
);

CREATE INDEX drying_session_open ON drying_session(started_at) WHERE ended_at IS NULL;

CREATE TABLE drying_spool (
    session_id    INTEGER NOT NULL REFERENCES drying_session(id),
    spool_id      INTEGER NOT NULL,                       -- Spoolman spool id
    weight_before REAL CHECK (weight_before > 0),         -- gross g on the scale
    weight_after  REAL CHECK (weight_after > 0),
    PRIMARY KEY (session_id, spool_id)
);

CREATE INDEX drying_spool_by_spool ON drying_spool(spool_id);
