-- 0081_calibration_mix.sql — the `mix` scope of colour-mix calibrations (#412, plan §20.4).
-- A `mix_color` record is the measured wall colour of one mix on one printer. `mix` is the
-- canonical key of printops/mix_key.py (`h0.12:f3.f12.f7`, `h0.12:lz:f1=25,f4=75`); NULL for
-- every kind without `mix` in its catalog scope. A newer record of the same printer and key
-- supersedes the older one.

ALTER TABLE calibration ADD COLUMN mix TEXT;

CREATE INDEX calibration_by_mix ON calibration(kind, printer, mix) WHERE status = 'recorded' AND mix IS NOT NULL;
