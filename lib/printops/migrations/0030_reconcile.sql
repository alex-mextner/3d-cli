-- 0030_reconcile (#161): anomalies found by slot reconciliation (printer slot ↔ Spoolman binding).
-- One row per occurrence: a condition that clears is `resolved` (by the reconciler or a human) and
-- a later recurrence opens a new row. At most one open/ack row per dedupe_key at any time.

CREATE TABLE anomaly (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,              -- ISO-8601 UTC when it was first seen
    kind TEXT NOT NULL,            -- type_mismatch | color_mismatch | empty_with_spool | ... (printops.reconcile.KINDS)
    subject TEXT NOT NULL,         -- "slot:u1/T3" or "spool:12"
    scope TEXT NOT NULL,           -- fleet printer name, or "fleet" for bindings to unknown printers
    evidence TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(evidence) AND json_type(evidence) = 'object'),
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'ack', 'resolved')),
    dedupe_key TEXT NOT NULL,      -- kind + subject + the evidence that makes it "the same" anomaly
    last_seen_at TEXT NOT NULL,    -- last reconcile pass that still saw it
    acked_at TEXT,
    acked_by TEXT,
    resolved_at TEXT,
    resolved_by TEXT,              -- "reconcile" when the condition cleared, else the token name
    note TEXT
);

CREATE UNIQUE INDEX anomaly_live_key ON anomaly (dedupe_key) WHERE status IN ('open', 'ack');
CREATE INDEX anomaly_status_id ON anomaly (status, id);
CREATE INDEX anomaly_scope_status ON anomaly (scope, status);
