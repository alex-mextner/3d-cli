-- 0001_core (#158): the append-only event log every PrintOps slice writes to.
-- schema_migrations is created by printops.db.migrate() itself (it must exist before any
-- migration can be recorded). API tokens are NOT stored here: they live in the tokens file
-- (PRINTOPS_TOKENS_FILE, sha256 only) so a token can be minted before the DB exists and
-- rotated without a migration.

-- AUTOINCREMENT: ids are never reused, so `since=<id>` cursors held by SSE/WS clients stay valid.
CREATE TABLE events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,            -- ISO-8601 UTC, e.g. 2026-10-14T16:20:00.123Z
    kind TEXT NOT NULL,          -- dotted, plan §11.2: spool.measured, slot.mismatch, …
    subject TEXT NOT NULL,       -- what it is about: "spool:12", "printer:u1/T3", …
    payload TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(payload) AND json_type(payload) = 'object'),
    actor TEXT NOT NULL          -- who caused it: token name, "system", "cli:<user>", …
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
