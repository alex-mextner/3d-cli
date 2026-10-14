-- 0010_locations (#159): the storage-location tree (plan §5.2, §10.2).
-- A location is addressed by its path ("WH:A/S2/B3/04", "slot:u1/T3", "dryer:1"); the path is
-- also what Spoolman's native `location` (≤ 64 chars) holds for every spool stored there, and
-- the spool's `printops_location_id` extra field holds `id`. Grammar, parent derivation and the
-- Spoolman sync live in lib/printops/locations.py; the CHECKs here are the last line of defence.

CREATE TABLE location (
    id INTEGER PRIMARY KEY AUTOINCREMENT,           -- never reused: printed NFC tags carry loc-<id>
    path TEXT NOT NULL COLLATE NOCASE UNIQUE CHECK (length(path) BETWEEN 3 AND 64),
    kind TEXT NOT NULL CHECK (kind IN ('warehouse', 'zone', 'shelf', 'box', 'slot', 'printer', 'ams',
                                       'printer_slot', 'dryer', 'dry_box')),
    parent_id INTEGER REFERENCES location (id) ON DELETE RESTRICT,
    tag_uid TEXT UNIQUE,                            -- NFC UID of the NTAG213 stuck on the cell (upper hex)
    qr TEXT,                                        -- payload of the label last printed for it
    capacity INTEGER CHECK (capacity IS NULL OR capacity > 0),  -- spools it holds; NULL = not limited
    humidity_sensor_entity TEXT,                    -- HA entity id of the RH sensor in it (dry boxes)
    note TEXT,
    source TEXT NOT NULL DEFAULT 'manual' CHECK (source IN ('manual', 'fleet')),  -- fleet = materialised from fleet/printers.yaml
    archived INTEGER NOT NULL DEFAULT 0 CHECK (archived IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (parent_id IS NULL OR parent_id <> id)
);

CREATE INDEX location_parent ON location (parent_id);
