# integrations/homeassistant

Source of truth for every 3D artifact of the Home Assistant config; `3d ha deploy` converges it onto the HA host
(`manifest.yaml`, docs: `docs/commands/ha.md`).

## Scheduled Spoolman + PrintOps backup (`packages/printops_backup.yaml`)

Home Assistant is the only scheduler of the backup — no cron job, no systemd timer. At 04:00 the automation
`printops_backup_daily` runs `script.printops_backup_run`, which calls 3d-api `POST /v1/backup/run`; 3d-api runs
`3d printops backup run` as a niced child (SQLite online backup, gzip level 1) into a verified, rotated set on the backup disk.

**One place to edit:** the two `EDIT HERE` spots of the package — `variables:` of the script (`api_url`, `target`, `subdir`,
`keep_daily/weekly/monthly`, `min_free_gb`) and the `at:` of the automation. `3d ha deploy` reloads script/automation/
rest_command/input_datetime without a restart.

Secrets: `printops_api_auth: "Bearer <3d-api token, scopes read,write>"` (the same secret `printops_tasks.yaml` uses).

Failure path: HA raises the persistent notification `printops_backup_failed` when 3d-api is unreachable or the run failed
(wrong/missing disk, no space, integrity), 3d-api opens the PrintOps anomaly `backup_failed`, and `printops_backup_stale`
fires after 36 h without a good run. A good run dismisses both.

Disk and restore: `docs/commands/printops.md#backup`. `3d init --env` can install this package and the Spoolman integration
(`docs/commands/init.md`); Home Assistant core has no Spoolman integration, the community one
(`Disane87/spoolman-homeassistant`, HACS) is not vendored here (`vendor.lock`).
