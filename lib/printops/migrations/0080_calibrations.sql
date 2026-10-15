-- 0080_calibrations.sql — calibration runs and records (#167, plan §7.2, §8).
-- One row per calibration: created `planned` when a run is started (POST /v1/calibrations/runs,
-- `3d calibrate gen`), turned `recorded` by its result. A manual entry is created recorded.
-- The scope snapshot (printer/head/nozzle/plate/filament/lot) is taken when the row is created,
-- so a later spool swap or Spoolman edit never changes what an old record was measured on.
-- Requirements and the `calibration_current` gate read the catalog (fleet/calibrations.yaml) and
-- the `calibration.invalidated` events (#166) on top of this table.

CREATE TABLE calibration (
    id                  INTEGER PRIMARY KEY,
    kind                TEXT NOT NULL,                     -- catalog kind id (flow_ratio, …)
    status              TEXT NOT NULL CHECK (status IN ('planned', 'recorded', 'abandoned')),
    printer             TEXT,                              -- fleet name; NULL for filament-only kinds
    head                TEXT,                              -- head label T1..Tn; NULL unless head-scoped
    nozzle_install_id   INTEGER,                           -- nozzle_install(id) of #166 when known
    nozzle_diameter_mm  REAL,
    nozzle_material     TEXT,
    nozzle_type         TEXT,
    plate               TEXT,
    filament_id         INTEGER,                           -- Spoolman filament id
    filament_name       TEXT,
    vendor              TEXT,
    material            TEXT,                              -- Spoolman filament material, upper-case
    lot_nr              TEXT,
    spool_id            INTEGER,                           -- the spool the run was printed with
    method              TEXT NOT NULL,
    params              TEXT NOT NULL DEFAULT '{}',        -- generator parameters (JSON object)
    gcode_path          TEXT,
    gcode_sha           TEXT,                              -- sha256 of the calibration G-code
    values_json         TEXT,                              -- JSON object; NULL until recorded
    evidence            TEXT NOT NULL DEFAULT '[]',        -- JSON list (photo URLs, VLM notes)
    applied_to          TEXT NOT NULL DEFAULT '[]',        -- JSON list [{target, before, after, at, confirmed_by}]
    note                TEXT,
    created_at          TEXT NOT NULL,
    created_by          TEXT NOT NULL,
    recorded_at         TEXT,
    recorded_by         TEXT,
    valid_until         TEXT,                              -- ISO-8601 UTC; NULL = no expiry
    superseded_by       INTEGER REFERENCES calibration(id),
    CHECK ((status = 'recorded') = (values_json IS NOT NULL AND recorded_at IS NOT NULL)),
    CHECK (head IS NULL OR printer IS NOT NULL)
);

CREATE INDEX calibration_by_scope ON calibration(kind, printer, head) WHERE status = 'recorded';
CREATE INDEX calibration_by_filament ON calibration(kind, filament_id) WHERE status = 'recorded';
