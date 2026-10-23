# `3d printers doctor` — diagnose (and repair) the printers of the fleet

```
3d printers doctor [PRINTER ...|--all] [--fix] [--yes] [--json] [--check ID[,ID]]
                   [--skip ID[,ID]] [--fail-on error|warn|never] [--secs S] [--list-checks]
```

`3d printer doctor …` is the same command. Issue [#519](https://github.com/alex-mextner/3d-cli/issues/519).

One check per known way a printer goes wrong: it does not answer, Klipper sits in `shutdown`, a
Moonraker component failed, `trusted_clients` trusts half the internet, Spoolman is not reachable or
blocks the web UI's browser origin, Moonraker books filament to a different spool than PrintOps has
bound, the camera is dead, the watch services run with `--dry-run`, a stale `printer-devices.yaml`
entry silently replaces the fleet entry, a job sits paused for hours, the clock is wrong, the disk is
full. Every check is driven by the fleet registry (`fleet/printers.yaml` +
`~/.config/3d-cli/printer-devices.yaml`): adapter kind, access, head count, camera and Moonraker's own
`/server/info` decide where a check applies. No printer name, model or host appears in the engine.

**Default is read-only.** A plain run sends GET requests only (the tests prove it with request logs).
`--fix` repairs, under the fix's risk class (below), and verifies every repair by re-running its check.

## Arguments and flags

| Flag | Meaning | Example |
|---|---|---|
| `PRINTER ...` | printer names of the registry (`3d printer list`); none and no `--all` is a usage error that lists the valid names | `3d printers doctor u1 k1se` |
| `--all` | every printer of the registry, offline ones included (their network checks `skip`) | `3d printers doctor --all` |
| `--fix` | apply `safe` fixes; list `needs-yes` ones; print the steps of `manual-only` ones | `3d printers doctor k1se --fix` |
| `--yes` | with `--fix`: also apply `needs-yes` fixes (they restart something). Alone it is a usage error | `3d printers doctor k1se --fix --yes` |
| `--json` | machine-readable report, schema `printers.doctor/1` (below) | `3d printers doctor --all --json` |
| `--check ID[,ID]` | run only these checks (unknown id: usage error listing the valid ids) | `3d printers doctor u1 --check klippy-state,disk-space` |
| `--skip ID[,ID]` | run all but these | `3d printers doctor --all --skip camera-snapshot` |
| `--fail-on LEVEL` | exit 1 when a finding of this severity or worse remains: `error` (default), `warn`, `never` | `3d printers doctor --all --fail-on warn` |
| `--secs S` | listen window (seconds) of a Creality stock printer's WebSocket (default 3) | `3d printers doctor k1se --secs 5` |
| `--list-checks` | list every check (id, title, severity, applies-to, fix risk); with `--json` as `printers.doctor.checks/1` | `3d printers doctor --list-checks --json` |

### Exit codes

| Code | Meaning |
|---|---|
| 0 | no finding at or above `--fail-on` remains (after the fixes) |
| 1 | one remains. The report is already printed; the error is silent (`GateFailure(silent=True)`) so nothing is shown twice |
| 2 | usage error: unknown printer / check / option, no printer given, `--yes` without `--fix`, bad `--fail-on` / `--secs` |

A `fail` result gates by its severity (`info` never gates). A check that could not evaluate what it
should have (`error`: an endpoint answered garbage, a check crashed) counts as severity `error`. A
`skip` never gates: the check could not look, and says why (an unreachable printer, a tool that is
missing, a feature the printer does not have). A `skip` is never a pass.

## Fix risk classes

| Risk | `--fix` | `--fix --yes` | Rule |
|---|---|---|---|
| `safe` | applied | applied | idempotent, never mid-print, verified by the check, journalled |
| `needs-yes` | listed, **not** applied | applied | restarts something; refused while a job is printing or paused |
| `manual-only` | never | never | the exact steps are printed (and are in the JSON); 3d never executes them |

Each applied fix reports one of: **fixed** (the check passes again, `verified: true`), **not fixed after
apply** (`applied: true, verified: false`: the check still fails; the fix is journalled anyway because
it ran), **refused** (a safety rule stopped it before anything was sent, e.g. the printer is printing),
**not applied** (a `needs-yes` fix without `--yes`) or **failed** (the apply itself raised). A second
`--fix` run is a no-op: the check passes, so nothing is applied or journalled again.

### Journal

Every **applied** fix writes a `printops.settings_change/1` record (kind `doctor_fix`, key = the check id,
`old`/`new` = before/after as read back, evidence = check id + risk, actor `3d printers doctor`, source
`doctor`). It is appended to the offline buffer `~/.local/share/3d-cli/printer-history/<printer>.jsonl`
(`lib/printops/history_buffer.py`: one line, `O_APPEND` under `flock`, `fsync`) and, when 3d-api is
configured (`PRINTOPS_API` / `PRINTOPS_TOKEN` / `~/.config/3d-cli/printops.yaml`) and reachable, imported
through `POST /v1/settings/import` (the route behind `3d settings import`; idempotent by uid). The fix
report's `journal` says what happened: `imported`, `buffered` (written locally; 3d-api not configured,
unreachable, or it refused the line: `3d settings import` replays the buffer later) or `failed: …` (not
even the local append worked). A journal problem never undoes or fails the fix.

## Checks

Severity is the default; a check may report a lower or higher one for a finding (noted). "contacts"
means the check talks to the printer; an `access: offline` printer is never contacted, so those checks
`skip` with `never contacted (access: offline)`.

### `reachable` — the printer answers a status read
- **Applies to** every printer 3d may contact (adapter `moonraker` or `creality-stock`). Severity **error**.
- **Evidence** `adapter`, `host`, `port`, `state`, `raw_state` (pass) / the adapter's error (fail).
- **Fix** manual-only: power, `ping HOST`, the address in the file that defines the device (`Device.source`), the Moonraker service.
- Every other Moonraker check `skip`s naming this one when the host is down, instead of repeating the error.

### `klippy-state` — Klipper is ready
- **Applies to** Moonraker-family printers (`creality-stock`: skip, not applicable). Severity **error** unless `ready`.
- **Evidence** `klippy_state`, `klippy_connected`, `state_message` (from `/printer/info`).
- **Fix** **needs-yes**: `shutdown` → `POST /printer/firmware_restart`; `error` / `disconnected` → `POST /printer/restart` (the Klipper host service). Refused when `print_stats.state` is `printing` or `paused`, and for `startup` (still booting). Verified by `/server/info` reporting `ready` within a bounded poll (45 s, 1 s steps).

### `moonraker-components` — no failed components, no config warnings
- **Applies to** Moonraker. Severity **warn**.
- **Evidence** `failed_components`, `warnings` of `/server/info`.
- **Fix** manual-only: read `moonraker.log`, fix the named section of `moonraker.conf`, restart Moonraker.

### `trusted-clients-width` — `[authorization]` does not trust more than the LAN
- **Applies to** Moonraker (`GET /server/config`). Severity **warn**.
- **Rule** per `trusted_clients` entry (`ipaddress`): loopback (`127.0.0.0/8`, `::1/128`) and link-local (`169.254.0.0/16`, `fe80::/10`) are fine; an IPv4 network **wider than /16** is flagged (`10.0.0.0/8`, `172.16.0.0/12`, `192.0.0.0/8`, `0.0.0.0/0`); an IPv4 network outside the private ranges is flagged (public); IPv6 `::/0` and global ranges are flagged, unique-local passes; a bare address or a `/24` LAN passes; a hostname is listed as `unchecked`. A bare `*` in `cors_domains` is flagged.
- **Pinned** (tests): the real Snapmaker U1 list `172.18.0.0/16, 10.0.0.0/8, 127.0.0.0/8, 169.254.0.0/16, 172.16.0.0/12, 192.0.0.0/8, FE80::/10, ::1/128` flags exactly `10.0.0.0/8`, `172.16.0.0/12`, `192.0.0.0/8`; the real rooted K1 SE list `10.0.0.0/8, 127.0.0.0/8, 169.254.0.0/16, 172.16.0.0/12, 192.168.0.0/16, FE80::/10, ::1/128` flags `10.0.0.0/8`, `172.16.0.0/12`.
- **Evidence** `trusted_clients`, `cors_domains`, `flagged: [{entry, reason}]`, `unchecked`.
- **Fix** manual-only: edit `[authorization] trusted_clients` in `moonraker.conf`, restart Moonraker; the config root is read-only (or regenerated on boot) on some firmware, then change it in the firmware's template.

### `spoolman-link` — Moonraker is connected to Spoolman
- **Applies to** Moonraker whose `/server/info` lists the `spoolman` component (a printer without `[spoolman]` answers 404 on `/server/spoolman/*`: skip with that reason). Severity **error** when not connected, **warn** when usage reports are queued.
- **Evidence** `spoolman_connected`, `pending_reports` (count), `pending_spool_ids`, `spool_id`, `spoolman_server` (from `config.spoolman.server`).
- **Fix** manual-only: `curl SPOOLMAN/api/v1/health`, start Spoolman, fix `[spoolman] server:`, restart Moonraker.

### `spoolman-cors` — Spoolman admits the web UI's browser origin
- **Applies to** Moonraker with a `[spoolman] server:` and a Fluidd/Mainsail web UI (the origins come from `printer_ops.webui.probe`, reduced to `scheme://host:port`). Severity **error**; skip when no UI answered.
- **How** `GET <spoolman>/api/v1/info` with `Origin: <origin>`; Spoolman (FastAPI CORSMiddleware) answers `Access-Control-Allow-Origin` only for an allowed origin (or `*`); no header means the browser blocks Fluidd's Spoolman panel.
- **Evidence** `spoolman_url`, `origins: [{origin, status, access_control_allow_origin, admitted}]`, `missing`.
- **Fix** manual-only: set `PRINTOPS_SPOOLMAN_CORS_ORIGIN` (comma-separated full origins) for the `spoolman` service of `services/3d-api/compose.yaml` (it feeds `SPOOLMAN_CORS_ORIGIN`; `services/3d-api/README.md`), then `docker compose up -d --force-recreate spoolman`.

### `active-spool-binding` — Moonraker's active spool is the spool PrintOps bound to the head
- **Applies to** Moonraker printers with the `spoolman` component, **a single head** (`printops.head_sync.single_head_reason`: Moonraker keeps one active spool that cannot follow tool changes, so a multi-head printer is skipped with that reason, from the fleet entry's head count) and a configured 3d-api. Severity **error**.
- **How** the bound spool ids come from `GET /v1/printers/{p}` (`slots[].spools[].spool_id`); the active one from the adapter's `active_spool()`.
- **Evidence** `bound_spool_ids`, `active_spool_id`, `spoolman_connected`, `pending_reports`. More than one bound spool on a one-head printer is a finding the fix refuses to guess.
- **Fix** **safe**: `adapter.set_active_spool(bound id or None)`, read back by the adapter; refused while a job is printing or paused; journalled.

### `slot-binding-unknown` — PrintOps' bindings match what the printer reports
- **Applies to** contactable fleet printers with a configured 3d-api (offline printers: skip; printers 3d-api does not poll: skip). Severity **warn**. Does not contact the printer.
- **Finding** a spool is bound to a slot whose live reading is unknown (none yet) or empty, or the printer has an open reconciliation anomaly (`GET /v1/anomalies?printer=P&status=open`).
- **Evidence** `slots: [{slot, spools, problem}]`, `anomalies: [{id, kind, subject}]`.
- **Fix** manual-only: `3d printops slots PRINTER`, `3d printops anomalies --printer PRINTER`, reload/unload the spool, or `3d printops anomalies ack ID`.

### `heads-vs-faults` — fleet `usable` flags agree with the open faults
- **Applies to** fleet printers with heads and a configured 3d-api (`GET /v1/printers/{p}/faults`). Does not contact the printer.
- **Finding** **info**: a head `usable: false` in `fleet/printers.yaml` with no open fault (the fault is seeded by `3d printops maint sync-fleet`); **warn**: an open fault on a head the fleet still marks usable.
- **Evidence** `unusable_without_fault: [{head, reason}]`, `fault_on_usable_head: [{head, faults}]`.
- **Fix** manual-only: close the fault (`3d printops maint fault close ID`), open one (`… fault open PRINTER/HEAD --symptom TEXT`), or fix `usable:` in `fleet/printers.yaml`.

### `camera-config` — the `watch.vision` camera settings resolve
- **Applies to** printers with a camera (configured, or the model default of `lib/data/vision/cameras.yaml`); skip otherwise. Severity **error**. Reads config only (`printer_ops.camera.vision_problems`).
- **Evidence** `kind`, `source`, `keepalive` / `problems`. **Fix** manual-only: fix `watch.vision` in the file that defines the device.

### `camera-snapshot` — the snapshot URL answers a JPEG
- **Applies to** printers with a camera. Severity **error**. `GET` the resolved snapshot URL (`CameraSpec.snapshot_url`): HTTP 200 and the first bytes `FF D8`, within the request timeout. A camera that needs Moonraker's `camera.start_monitor` keepalive is woken once with the same call the vision service uses and asked again (up to 3 times); a camera without keepalive is asked once.
- **Evidence** `url` (credentials redacted), `kind`, `http_status`, `bytes`, `content_type`, `woken_with_keepalive`.
- **Fix** manual-only: what to restart (the streamer / the camera service) and which URL.

### `watch-units-dry-run` — the vision/watch user services deliver
- **Applies to** printers with a camera; needs `systemctl --user` on this host (without it: **skip**, never a pass). Severity **error**. Reuses `printer_ops/deploy_check.py` (`check_printer`, `fix_commands`; the logic of `3d printer vision doctor`): `3d-printer-vision@NAME` or `3d-printer-watch@NAME` running with `--dry-run` (on `ExecStart` or in the `WATCH_ARGS` of its EnvironmentFile) is an **error**; no running vision service is a **warn**.
- **Evidence** `units: [{unit, state, dry_run, source}]`, `warnings`. **Fix** manual-only: the commands of `deploy_check.fix_commands` (edit the unit, `daemon-reload`, restart, delivery test).

### `device-override-stale` — an override does not silently replace the fleet entry
- **Applies to** a printer that has both a fleet entry and an effective device from another file (`Device.source` differs from the fleet file). Severity **warn**. Reads config only.
- **Evidence** `fleet` and `effective` (`adapter`, `host`, `port`, `source`), `winner: "override"`. The override replaces the whole device part of the fleet entry (`printer_ops/devices.py`), so a fleet fix is invisible while it exists.
- **Fix** manual-only: delete the override entry, or move its values into the fleet entry.

### `print-stuck` — no job paused for hours
- **Applies to** Moonraker. Severity **warn**. `print_stats.state == paused` for longer than **2 h** (`PAUSED_WARN_S`). Klipper stamps no pause time, so the length is `total_duration − print_duration` (time since the job started minus time extruding; it includes the first minutes of heating, so it can only be a little high). "Printing with no progress" is not cheaply observable and is not checked.
- **Evidence** `state`, `filename`, `total_duration_s`, `print_duration_s`, `paused_for_s`, `threshold_s`. **Fix** manual-only: resume or cancel.

### `time-sync` — the printer's clock agrees with this host
- **Applies to** every contactable printer whose HTTP replies carry a `Date` header (Moonraker's do; else skip). `|skew| > 120 s` **warn**, `> 1 h` **error** (the header has 1 s resolution).
- **Evidence** `printer_time`, `local_time`, `skew_s`, `date_header`. **Fix** manual-only: NTP / `timedatectl` / the printer's time settings.

### `disk-space` — the G-code disk has room
- **Applies to** Moonraker (`GET /server/files/directory?path=gcodes` → `disk_usage`; skip when not reported). **warn** below 5 % or 200 MB free, **error** below 1 % or 50 MB (decimal MB).
- **Evidence** `total_bytes`, `used_bytes`, `free_bytes`, `free_pct`. **Fix** manual-only: delete old G-code / timelapses.

### `webui-presence` — Fluidd or Mainsail answers
- **Applies to** Moonraker (`printer_ops.webui.probe`: 80, 4408, 4409 or `THREED_WEBUI_PORTS`). Severity **info** when neither answers.
- **Evidence** `tried`, `found: [{kind, url, http_status}]`, `origins`. **Fix** manual-only: install one by the vendor path (the model's runbook in `docs/printers/`, e.g. the Creality Helper Script for K1-family printers).

## JSON schema `printers.doctor/1`

```json
{
  "schema": "printers.doctor/1",
  "generated_at": "2026-10-23T12:00:00Z",
  "printers": [
    {
      "name": "k1se", "access": "full", "adapter": "moonraker",
      "results": [
        {
          "check": "active-spool-binding",
          "status": "pass",                       // pass | fail | skip | error
          "severity": "error",                    // info | warn | error
          "summary": "Moonraker's active spool is spool #5, as PrintOps has it",
          "evidence": {"bound_spool_ids": [5], "active_spool_id": 5},
          "fix": {                                // null unless the check failed and has a fix
            "risk": "safe",                       // safe | needs-yes | manual-only
            "description": "set Moonraker's active spool to the bound spool …",
            "manual_steps": ["When the printer is idle: curl -X POST …"],
            "applied": true,
            "verified": true,                     // null when not applied
            "error": null,                        // "refused: …" | "not applied: …" | "failed: …" | "not fixed after apply: …"
            "journal": "imported"                 // imported | buffered | failed: … | null (nothing applied)
          }
        }
      ]
    }
  ],
  "summary": {"pass": 0, "fail": 0, "skip": 0, "error": 0, "fixed": 0, "remaining": 0}
}
```

`summary` counts the final results (after fixes); `fixed` is the number of verified fixes; `remaining`
is the number of findings at or above `--fail-on` (what the exit code is decided by). `skip` results carry
the reason in `summary`. `--list-checks --json` is
`{"schema": "printers.doctor.checks/1", "checks": [{id, title, severity, applies_to, contacts_printer, fix: {risk, description} | null}]}`.

## Adding a check

Drop one module in `lib/printer_doctor/checks/` (no other edit: not the engine, not the CLI, not this
list's code; `discover_checks()` imports every module whose name does not start with `_`). It defines
`CHECKS`:

```python
from printer_doctor.context import Context
from printer_doctor.model import Check, Fix, Verdict

def _run(ctx: Context) -> Verdict:
    info = ctx.server_info()                      # cached Moonraker GET; CannotSee -> skip/error with the reason
    return Verdict.ok("fine", x=1) if info.get("x") else Verdict.fail("x is off", "warn", x=0)

CHECKS = (
    Check(
        id="my-check", title="x is on", severity="warn", applies_to="Moonraker printers",
        applies=lambda ctx: ctx.moonraker_only(),  # None = applies, else the skip reason; decide from data, never a name
        run=_run,
        fix=Fix("manual-only", "switch x on", lambda ctx, result: ["open the UI", "switch x on"]),
    ),
)
```

Rules: `applies` decides from the adapter kind, capabilities and fleet/device data only (a test scans the
generic code for any fleet printer name, model or host literal); a check that cannot see raises
`CannotSee` (or returns `Verdict.skip`) and never reports a pass it could not verify; set
`contacts_printer=False` for a check that reads only config, 3d-api or systemd (it still runs for an
offline printer); a `safe` / `needs-yes` fix passes an `apply(ctx, result) -> FixChange(before, after)`
that is idempotent, raises `FixRefused` for anything unsafe (a running job) **before** sending a request,
and is verified by the check itself. Add a failing and a passing test on the loopback fakes
(`tests/printer_fakes.py`, `tests/printer_doctor_fakes.py`) and a section to this page; the test
`test_help_documents_every_flag_with_an_example_and_the_docs_agree` fails until every check id is
documented here.

## Not verified on real hardware

The restart fix (`POST /printer/firmware_restart`, `/printer/restart`), the active-spool write and the
camera keepalive were exercised against fakes that implement the documented Moonraker behaviour, not
against a physical printer. The Moonraker endpoints used (`/server/info`, `/server/config`,
`/server/spoolman/status`, `/server/files/directory`, `/printer/objects/query`) are documented at
<https://moonraker.readthedocs.io/en/latest/web_api/>.
