-- 0071_printer_tasks (#281): one to-do list per printer (plan §7.5), next to the maintenance
-- domain of 0070. Same vocabulary: `printer` = fleet/printers.yaml name, `head` = T1..Tn,
-- `component` as in maintenance. Validation, derivation, events and the JSONL import live in
-- lib/printops/printer_tasks.py; the CHECKs here are the last line of defence.

CREATE TABLE printer_task (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    uid TEXT NOT NULL UNIQUE,              -- UUIDv7; the same id in the JSONL buffer and the DB
    printer TEXT NOT NULL,
    head TEXT,
    slot TEXT,
    component TEXT,
    type TEXT NOT NULL CHECK (type IN ('calibration', 'inspect', 'maintenance', 'repair', 'setup', 'other')),
    title TEXT NOT NULL CHECK (length(title) > 0),
    why TEXT,
    blocks TEXT NOT NULL DEFAULT 'none' CHECK (blocks IN ('none', 'printer', 'head', 'slot')),
    "when" TEXT NOT NULL DEFAULT 'any' CHECK ("when" IN ('idle', 'before_print', 'any')),
    when_note TEXT,
    duration_min INTEGER CHECK (duration_min IS NULL OR duration_min > 0),
    needs_human INTEGER NOT NULL DEFAULT 1 CHECK (needs_human IN (0, 1)),
    source TEXT NOT NULL CHECK (source IN ('manual', 'calibration', 'maintenance_rule', 'fault',
                                           'calibration_followup', 'import')),
    -- What a derived task stands for, always with the printer and the full scope:
    -- 'calib:k1se/T1/flow_ratio/nozzle:4/filament:12[/lot:A7]', 'rule:u1/<rule>/<head|->/<baseline>',
    -- 'fault:<external_key|id>'. Unique among open tasks only: the same need after a close opens a
    -- new row and the history stays.
    source_key TEXT,
    depends_on TEXT NOT NULL DEFAULT '[]',         -- JSON [task id]
    refs TEXT NOT NULL DEFAULT '[]',               -- JSON [string]: issues, chat refs, merged buffer uids
    calibration_kinds TEXT NOT NULL DEFAULT '[]',  -- JSON [catalog kind]
    spools TEXT NOT NULL DEFAULT '[]',             -- JSON [{name, spool_id}] as the buffer names them
    spool_id INTEGER,                              -- Spoolman spool the task is about (one spool)
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'in_progress', 'snoozed', 'done', 'dismissed')),
    snoozed_until TEXT,                            -- NULL while snoozed = until reopened
    closed_at TEXT,
    closed_by TEXT,
    note TEXT,
    calibration_id INTEGER,                        -- the record that did it (table of 0080)
    maintenance_id INTEGER REFERENCES maintenance (id),
    fault_id INTEGER REFERENCES fault (id),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (blocks != 'head' OR head IS NOT NULL),
    CHECK (blocks != 'slot' OR slot IS NOT NULL),
    CHECK ((status IN ('done', 'dismissed')) = (closed_at IS NOT NULL)),
    CHECK (status = 'snoozed' OR snoozed_until IS NULL)
);

CREATE UNIQUE INDEX printer_task_open_key ON printer_task (source_key)
    WHERE source_key IS NOT NULL AND closed_at IS NULL;
CREATE INDEX printer_task_printer ON printer_task (printer, status, id);

-- Every imported buffer line (printops.printer_task/1) by its uid: a second import adds nothing.
CREATE TABLE printer_task_op (
    uid TEXT PRIMARY KEY,
    task_uid TEXT NOT NULL,
    task_id INTEGER REFERENCES printer_task (id),  -- NULL: refused (reason in outcome)
    op TEXT NOT NULL CHECK (op IN ('open', 'update', 'done', 'dismiss', 'snooze')),
    outcome TEXT NOT NULL,                 -- created | merged | applied | refused: …
    line TEXT NOT NULL,                    -- the buffer line as read
    imported_at TEXT NOT NULL,
    imported_by TEXT NOT NULL
);

-- One row per idle period of a printer (#283): the claim that makes the idle notice at-most-once.
-- task_set_sha = sha256 of the task uids sorted ascending, joined by '\n'.
CREATE TABLE printer_idle_notice (
    printer TEXT NOT NULL,
    idle_since TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('pending', 'sent', 'failed')),
    claimed_at TEXT NOT NULL,
    sent_at TEXT,
    tg_message_id INTEGER,
    task_uids TEXT NOT NULL DEFAULT '[]',
    task_set_sha TEXT,
    host TEXT,
    PRIMARY KEY (printer, idle_since)
);
