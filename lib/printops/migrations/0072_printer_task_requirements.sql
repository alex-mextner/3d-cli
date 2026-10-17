-- 0072_printer_task_requirements (#387): a derived calibration task covers every missing calibration
-- of one head (source_key 'calib:<printer>/<head|->') instead of one kind × nozzle × filament × lot.
-- `requirements` lists what it covers, one entry per requirement key of the engine:
-- [{key: 'calib:k1se/T1/flow_ratio/nozzle:4/filament:12[/lot:A7]', kind, title, reason, spool_id,
--   spool, minutes}]. A person's dismissal sticks for exactly these keys. Empty for every other
-- task, and for the per-kind calibration tasks of 0071, which the next derivation supersedes.

ALTER TABLE printer_task ADD COLUMN requirements TEXT NOT NULL DEFAULT '[]';
