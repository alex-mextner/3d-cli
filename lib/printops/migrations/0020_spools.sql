-- 0020_spools (#160): the spool ledger — every change of a spool's remaining filament, with its
-- uncertainty. Spool identity lives in Spoolman (decision D2); `spool_id` is the Spoolman id.
--
-- Remaining is an interval, never a bare number (plan §5.3): the newest `anchor` row sets an
-- absolute value (a weighing, a sealed spool received with a known net, an `empty`), every
-- later `delta` row adds to it, and the half-width is the root-sum-square of the anchor's and
-- the deltas' confidence_g. `note` rows (e.g. drying water loss) change nothing.

-- AUTOINCREMENT: ids are never reused, so `since=<id>` cursors over the ledger stay valid.
CREATE TABLE ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,                 -- ISO-8601 UTC
    spool_id INTEGER NOT NULL,        -- Spoolman spool id
    kind TEXT NOT NULL CHECK (kind IN ('delta', 'anchor', 'note')),
    delta_g REAL NOT NULL,            -- change of remaining: usage is negative; for an anchor the
                                      -- jump from the previous estimate (drift, for reconcile)
    anchor_g REAL,                    -- anchor rows: the absolute remaining net filament
    source TEXT NOT NULL CHECK (source IN ('gcode_estimate', 'moonraker_live', 'printer_reported',
                                           'weigh', 'voice', 'manual', 'purge', 'receive', 'note')),
    confidence_g REAL NOT NULL CHECK (confidence_g >= 0),  -- ± grams (σ) of this row
    gross_g REAL,                     -- weigh rows: what the scale showed
    tare_g REAL,                      -- weigh rows: the empty-spool weight subtracted
    job_id TEXT,
    note TEXT,
    actor TEXT NOT NULL,
    CHECK ((kind = 'anchor') = (anchor_g IS NOT NULL)),
    CHECK (kind != 'note' OR (delta_g = 0 AND confidence_g = 0))
);

CREATE INDEX ledger_spool_id ON ledger (spool_id, id);

CREATE TRIGGER ledger_no_update BEFORE UPDATE ON ledger
BEGIN
    SELECT RAISE(ABORT, 'the ledger is append-only: book a correction instead');
END;

CREATE TRIGGER ledger_no_delete BEFORE DELETE ON ledger
BEGIN
    SELECT RAISE(ABORT, 'the ledger is append-only: book a correction instead');
END;

-- Imports (`3d printops spool import-owned`): source key → Spoolman spool. The UNIQUE key,
-- checked inside the receive transaction, makes an import idempotent even when two run at
-- once; Spoolman's spool extra `external_key` carries the same key for people and for a
-- PrintOps DB restored from an older backup.
CREATE TABLE spool_import (
    external_key TEXT PRIMARY KEY,
    spool_id INTEGER NOT NULL,        -- Spoolman spool id
    at TEXT NOT NULL,
    actor TEXT NOT NULL
);
