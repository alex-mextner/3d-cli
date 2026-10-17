-- 0100_settings_history.sql — the append-only journal of printer setting changes (#264, plan §7.4).
-- One row per change: a calibration value, a slicer profile key, firmware, fleet entry, spool
-- binding, maintenance step. Rows are never edited: a correction or a rollback is a new row
-- (`reverts` points at the row it undoes). `uid` (UUID, written by the CLI into the offline
-- buffer) is the idempotency key of `3d settings import`.

CREATE TABLE settings_change (
    id                  INTEGER PRIMARY KEY,
    uid                 TEXT NOT NULL UNIQUE,
    at                  TEXT NOT NULL,                     -- ISO-8601 UTC (ms): when the change happened
    recorded_at         TEXT NOT NULL,                     -- when it was written down (buffer line or API)
    printer             TEXT NOT NULL,                     -- fleet name
    head                TEXT,
    plate               TEXT,
    nozzle_install_id   INTEGER,
    spool_id            INTEGER,                           -- Spoolman spool (resolved binding for calibrations)
    filament_id         INTEGER,
    lot_nr              TEXT,
    kind                TEXT NOT NULL,                     -- calibration kind | slicer_profile | firmware | fleet | …
    key                 TEXT NOT NULL,                     -- what changed, e.g. filament_flow_ratio, printer.cfg
    old                 TEXT,                              -- JSON
    new                 TEXT,                              -- JSON
    status              TEXT NOT NULL CHECK (status IN ('measured', 'measured_not_applied', 'applied',
                                                        'restored', 'observed', 'rolled_back')),
    source              TEXT NOT NULL,                     -- accelerometer, photo, user_visual_choice, backup, …
    actor               TEXT NOT NULL,
    command             TEXT,
    evidence            TEXT NOT NULL DEFAULT '[]',        -- JSON list
    calibration_id      INTEGER REFERENCES calibration(id),
    maintenance_id      INTEGER,
    reverts             INTEGER REFERENCES settings_change(id),
    entry               TEXT NOT NULL,                     -- the full JSON entry as written (method, measured, notes, …)
    imported_at         TEXT,                              -- set when the row came from a buffer import
    imported_by         TEXT
);

CREATE INDEX settings_change_by_printer ON settings_change(printer, at);
CREATE INDEX settings_change_by_kind ON settings_change(kind, printer);

CREATE TRIGGER settings_change_no_update BEFORE UPDATE ON settings_change
BEGIN
    SELECT RAISE(ABORT, 'settings_change is append-only');
END;

CREATE TRIGGER settings_change_no_delete BEFORE DELETE ON settings_change
BEGIN
    SELECT RAISE(ABORT, 'settings_change is append-only');
END;
