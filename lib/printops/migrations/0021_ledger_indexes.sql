-- 0021_ledger_indexes (#160): ledger.estimate finds a spool's newest anchor and newest weighing
-- without reading the rest of its history (a long-lived spool collects thousands of usage
-- rows). A separate file, not an edit of 0020: migrations are checksummed once applied.
CREATE INDEX ledger_anchor ON ledger (spool_id, id) WHERE kind = 'anchor';
CREATE INDEX ledger_weigh ON ledger (spool_id, id) WHERE source = 'weigh';
