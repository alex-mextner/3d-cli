-- 0106_drop_spool_import: `spool_import` (0020) was a shadow copy of the Spoolman spool extra field
-- `external_key`: `receive` has always written that extra in the same call that creates the spool,
-- so no data is lost by dropping the table. Imports and `receive` now ask Spoolman (the single
-- store for what it holds) whether a key is taken, inside the write transaction as before
-- (docs/printops/native-fields.md).
DROP TABLE spool_import;
