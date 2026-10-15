-- 0060_incidents (#165): structured incidents (plan §6) and the printing exposure that turns
-- incident counts into rates (per kg printed, per print-hour).
--
-- An incident carries a SNAPSHOT of what was printing when it happened (printer/head, spool,
-- filament, vendor, lot, job, layer, G-code temperatures/speed/fan, drying state, humidity), so
-- later moves, reloads or Spoolman edits never rewrite history. `context` keeps the provenance
-- of every auto-filled field and the warnings of the auto-fill.

CREATE TABLE incident (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,                 -- when it happened, ISO-8601 UTC
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('draft', 'open', 'resolved', 'dismissed')),
    reported_by TEXT NOT NULL CHECK (reported_by IN ('voice', 'telegram', 'cli', 'station', 'watch',
                                                     'vision', 'api')),
    confidence REAL NOT NULL DEFAULT 1 CHECK (confidence >= 0 AND confidence <= 1),
    kind TEXT NOT NULL CHECK (kind IN ('clog', 'stringing', 'oozing_blobs', 'warping', 'delamination',
                                       'under_extrusion', 'over_extrusion', 'moisture_popping',
                                       'brittleness', 'poor_bed_adhesion', 'layer_shift', 'colour_off',
                                       'runout', 'tangle', 'heat_creep', 'nozzle_drag', 'other')),
    severity TEXT NOT NULL CHECK (severity IN ('cosmetic', 'part_lost', 'print_lost', 'hardware_risk')),
    -- snapshot at the moment of the incident
    printer TEXT,
    head TEXT,                        -- head label (T3) or AMS slot (ams1/2)
    spool_id INTEGER,                 -- Spoolman ids
    filament_id INTEGER,
    vendor_id INTEGER,
    vendor TEXT,
    material TEXT,
    filament_name TEXT,
    lot_nr TEXT,
    job_id TEXT,                      -- printer-native job id (Moonraker history job_id), if known
    job_file TEXT,
    layer INTEGER,
    z_mm REAL,
    feature TEXT,                     -- slicer ;TYPE: (Outer wall, Sparse infill, Bridge, …)
    nozzle_temp REAL,
    bed_temp REAL,
    chamber_temp REAL,
    speed_mm_s REAL,
    volumetric_mm3_s REAL,
    fan_pct REAL,
    days_since_dried REAL,
    dry_box_rh REAL,
    room_rh REAL,
    -- outcome
    resolution_kind TEXT CHECK (resolution_kind IS NULL OR resolution_kind IN (
        'dried', 'temp_change', 'speed_change', 'retraction_change', 'cleaned_nozzle', 'replaced_nozzle',
        'glue_plate', 'recalibrated', 'discarded_spool', 'none')),
    resolution_text TEXT,
    resolved_at TEXT,
    linked_calibration_id INTEGER,    -- calibration record (#167), when the fix was a calibration
    photos TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(photos) AND json_type(photos) = 'array'),
    tags TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(tags) AND json_type(tags) = 'array'),
    note TEXT,
    context TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(context) AND json_type(context) = 'object'),
    source_key TEXT UNIQUE,           -- idempotency key of an automatic report (watch event key)
    actor TEXT NOT NULL,
    CHECK ((status = 'resolved') = (resolved_at IS NOT NULL)),
    CHECK (status != 'resolved' OR resolution_kind IS NOT NULL)
);

CREATE INDEX incident_at ON incident (at);
CREATE INDEX incident_printer_at ON incident (printer, head, at);
CREATE INDEX incident_spool ON incident (spool_id);
CREATE INDEX incident_status ON incident (status, at);

-- Printing exposure: who printed how long and how much, per printer head, with the spool it
-- printed from (snapshot). One row per finished job and head. Rows are immutable once synced:
-- a finished Moonraker job never changes. Sources: `moonraker` (history sync, read-only GET),
-- `manual` (offline printers), `job` (the job ledger, #170).
CREATE TABLE exposure (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL CHECK (source IN ('moonraker', 'manual', 'job')),
    source_key TEXT NOT NULL,         -- moonraker: <printer>:<job_id>:<head>
    printer TEXT NOT NULL,
    head TEXT,
    spool_id INTEGER,
    filament_id INTEGER,
    vendor_id INTEGER,
    vendor TEXT,
    material TEXT,
    filament_name TEXT,
    lot_nr TEXT,
    job_id TEXT,
    job_file TEXT,
    job_status TEXT,                  -- completed | cancelled | error | klippy_shutdown | …
    started_at TEXT NOT NULL,
    ended_at TEXT,
    hours REAL NOT NULL CHECK (hours >= 0),
    grams REAL CHECK (grams IS NULL OR grams >= 0),
    note TEXT,
    actor TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    UNIQUE (source, source_key)
);

CREATE INDEX exposure_started ON exposure (started_at);
CREATE INDEX exposure_printer ON exposure (printer, head, started_at);
