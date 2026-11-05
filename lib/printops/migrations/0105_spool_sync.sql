-- 0105_spool_sync: what PrintOps last PROCESSED of each Spoolman spool — the base of the
-- three-way compare that tells a change somebody else made in Spoolman (web UI, Moonraker
-- `[spoolman]`, a tag scanner) from one PrintOps made itself (printops.spool_mirror,
-- printops.spool_sync).
--
-- Spoolman keeps the current value of every spool field; PrintOps keeps no second copy of it.
-- This row is a bookmark, not a copy: it holds only the fields whose change PrintOps must react
-- to (a ledger row, an event), as they were when PrintOps last looked or wrote. Rows are written
-- by the sync engine and by the one Spoolman write path (SyncedSpoolman), in the same
-- transaction as the PrintOps change they belong to.

CREATE TABLE spool_sync (
    spool_id INTEGER PRIMARY KEY,       -- Spoolman spool id
    state TEXT NOT NULL CHECK (state IN ('present', 'deleted')),
    remaining_g REAL,                   -- Spoolman remaining_weight when last processed (NULL: none)
    last_used TEXT,                     -- Spoolman last_used then: moves on /use and /measure, not on PATCH
    archived INTEGER NOT NULL CHECK (archived IN (0, 1)),
    location TEXT NOT NULL,             -- Spoolman location text then ('' = none); a slot is `slot:<printer>/<head>`
    seen_at TEXT NOT NULL               -- ISO-8601 UTC of the last write of this row
);
