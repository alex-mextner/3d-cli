-- 0070_maintenance (#166): printer components, maintenance log, faults (plan §7.1, §7.3).
-- `printer` is the fleet/printers.yaml name; `head` the head label (T1..Tn), NULL = the whole
-- printer. Validation, fleet seeding and the events live in lib/printops/maintenance.py; the
-- CHECKs here are the last line of defence.

-- What is mounted on a printer: nozzles per head (the scope of flow/PA calibrations), PTFE,
-- hotend, extruder gear, plates, belts… A replacement closes the old row (removed_at) and inserts
-- a new one, so the rows are the install history. installed_at NULL = "installed at an unknown
-- date" (seeded from the fleet file, which only knows what is mounted now).
CREATE TABLE component (
    id INTEGER PRIMARY KEY AUTOINCREMENT,  -- never reused: calibration records point at nozzle installs
    printer TEXT NOT NULL,
    head TEXT,
    kind TEXT NOT NULL CHECK (kind IN ('nozzle', 'hotend', 'ptfe', 'extruder', 'plate', 'belts',
                                       'kinematics', 'fan', 'head', 'other')),
    label TEXT,                            -- free text: model / brand / plate type
    diameter_mm REAL CHECK (diameter_mm IS NULL OR (diameter_mm > 0 AND diameter_mm <= 2)),
    material TEXT,                         -- nozzle: brass | stainless | hardened | ruby | tungsten
    type TEXT,                             -- nozzle: std | hf
    installed_at TEXT,
    removed_at TEXT,
    install_reason TEXT,
    removal_reason TEXT,
    source TEXT NOT NULL CHECK (source IN ('fleet', 'manual')),
    maintenance_id INTEGER REFERENCES maintenance (id),  -- the replacement that installed it
    created_at TEXT NOT NULL,
    CHECK (kind = 'nozzle' OR (diameter_mm IS NULL AND material IS NULL AND type IS NULL)),
    CHECK (removed_at IS NULL OR installed_at IS NULL OR removed_at >= installed_at)
);

-- One mounted component of a kind per head (or per printer); plates are many per printer, one per label.
CREATE UNIQUE INDEX component_active ON component (printer, COALESCE(head, ''), kind)
    WHERE removed_at IS NULL AND kind != 'plate';
CREATE UNIQUE INDEX component_active_plate ON component (printer, label)
    WHERE removed_at IS NULL AND kind = 'plate';
CREATE INDEX component_printer ON component (printer, head, kind, id);

-- The calibrations engine (#167) scopes flow/PA records by nozzle install.
CREATE VIEW nozzle_install AS
    SELECT id, printer, head, diameter_mm, material, type, installed_at, removed_at,
           install_reason AS reason, removal_reason, source
    FROM component WHERE kind = 'nozzle';

CREATE TABLE maintenance (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    printer TEXT NOT NULL,
    head TEXT,
    component TEXT NOT NULL CHECK (component IN ('nozzle', 'hotend', 'ptfe', 'extruder', 'plate', 'belts',
                                                 'kinematics', 'fan', 'head', 'other', 'printer')),
    kind TEXT NOT NULL CHECK (kind IN ('clean', 'lube', 'replace', 'tighten', 'repair', 'inspect',
                                       'firmware_update', 'move')),
    at TEXT NOT NULL,                      -- when the work was done (ISO-8601 UTC)
    actor TEXT NOT NULL,                   -- who did / logged it (token name)
    parts_used TEXT NOT NULL DEFAULT '[]', -- JSON list of strings
    notes TEXT,
    rule_id TEXT,                          -- the maintenance-rules.yaml rule it satisfies (resets its counter)
    triggers_calibrations TEXT NOT NULL DEFAULT '[]',  -- JSON [{"trigger", "kinds": [..]|null}] emitted as calibration.invalidated
    created_at TEXT NOT NULL
);

CREATE INDEX maintenance_printer ON maintenance (printer, at);

CREATE TABLE fault (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    printer TEXT NOT NULL,
    head TEXT,
    component TEXT,
    at TEXT NOT NULL,                      -- when it was noticed
    symptom TEXT NOT NULL CHECK (length(symptom) > 0),
    root_cause TEXT,
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'fixed', 'wontfix')),
    fixed_by_maintenance_id INTEGER REFERENCES maintenance (id),
    closed_at TEXT,
    source TEXT NOT NULL CHECK (source IN ('fleet', 'manual')),
    external_key TEXT UNIQUE,              -- seeding idempotency, e.g. 'fleet:u1/T2'
    opened_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK ((status = 'open') = (closed_at IS NULL)),
    CHECK (fixed_by_maintenance_id IS NULL OR status = 'fixed')
);

CREATE INDEX fault_printer ON fault (printer, status, id);

-- maintenance.due is emitted once per rule × printer × head × baseline (the last matching
-- maintenance, 0 = none logged): logging the maintenance starts a new baseline.
CREATE TABLE maintenance_due (
    rule_id TEXT NOT NULL,
    printer TEXT NOT NULL,
    head TEXT NOT NULL DEFAULT '',
    baseline_id INTEGER NOT NULL,
    due_at TEXT NOT NULL,
    reason TEXT NOT NULL,
    event_id INTEGER NOT NULL,
    PRIMARY KEY (rule_id, printer, head, baseline_id)
);
