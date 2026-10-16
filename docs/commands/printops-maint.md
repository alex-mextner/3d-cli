# `3d printops maint` — components, nozzle swaps, maintenance log, faults, due rules

What is mounted on each printer, what was done to it, what is broken, and what maintenance is
due ([#166](https://github.com/alex-mextner/3d-cli/issues/166), plan §7.1/§7.3). Like every
`3d printops` subcommand it is a thin client of **3d-api** (configuration:
[printops.md](printops.md#configuration)); reads need a token with `read`, changes `write`.
What is still to be done on a printer — repairs of open faults, due rules, needed calibrations,
manual tasks — is one list: [`3d printer todo`](printer.md#to-do-list-3d-printer-todo).

```
3d printops maint components PRINTER [--removed] [--json]
3d printops maint nozzle swap PRINTER/HEAD --reason TEXT [--diameter MM] [--material M] [--type std|hf]
                  [--label TEXT] [--at WHEN] [--notes TEXT] [--parts A,B] [--json]
3d printops maint log TARGET (--rule ID | --component C --kind K) [--at WHEN] [--notes TEXT] [--parts A,B]
                  [--label TEXT] [--invalidates KIND,KIND | --no-invalidate] [--fixes FAULT[,FAULT]] [--json]
3d printops maint history PRINTER [--limit N] [--json]
3d printops maint due [PRINTER] [--status due|soon|ok|no_counter] [--check] [--json]
3d printops maint fault ls [PRINTER] [--status open|fixed|wontfix|all] [--json]
3d printops maint fault open TARGET --symptom TEXT [--component C] [--at WHEN] [--json]
3d printops maint fault close ID [--root-cause TEXT] [--fixed-by MAINT_ID] [--wontfix] [--json]
3d printops maint sync-fleet [--json]
```

TARGET is `PRINTER` or `PRINTER/HEAD` (`u1`, `u1/T3`); printer names and head labels come from
`fleet/printers.yaml`. `--at` takes an ISO date or date-time (`2026-10-14`, `2026-10-14T18:30`);
without an offset it is your local time; the default is now; a time more than 5 minutes in the future is refused. Output
shows times in your local time zone (`--json` and the API keep UTC).

## Components and the fleet file

A **component** row is one part mounted on a printer or head: `nozzle` (diameter, material
`brass|stainless|hardened|ruby|tungsten`, type `std|hf`), `hotend`, `ptfe`, `extruder`, `plate`,
`belts`, `kinematics`, `fan`, `head`, `other`. A replacement closes the old row and opens a new
one, so the rows are the install history (`components --removed`).

3d-api seeds what `fleet/printers.yaml` knows and the DB never recorded — on the first request
after the file changed (a deploy) and on `sync-fleet`: every head's nozzle and every plate,
**installed at an unknown date**, and one open **fault** per `usable: false` head (the U1's T2:
`fleet:u1/T2`, symptom = the fleet `reason`). Seeding never overrides recorded history; when the
fleet file disagrees with it (a nozzle swapped since, a seeded fault closed) `sync-fleet` and
`3d lint --printops` (rule `maintenance/fleet-drift`) say so — update the fleet file.

A head is **usable** when the fleet says so and no fault is open on it (`components` shows both).

- `components PRINTER` — heads with usability, open faults and the nozzle; other mounted parts.
  `3d printops maint components u1`
- `nozzle swap PRINTER/HEAD --reason TEXT` — the new nozzle; unset `--diameter/--material/--type/--label`
  are kept from the old one (like-for-like). Logs a `nozzle replace` maintenance.
  `3d printops maint nozzle swap u1/T3 --reason "clogged" --material hardened`

## Calibration invalidation

A nozzle swap and component work invalidate calibrations: 3d-api emits one
`calibration.invalidated` event per trigger, subject `PRINTER[/HEAD]`, payload
`{printer, head|null, trigger, kinds|null, reason, maintenance_id, nozzle_install_id}`. The
calibration engine ([#167](https://github.com/alex-mextner/3d-cli/issues/167)) treats every record
of that scope made before the event as invalid; `kinds: null` means every calibration kind whose
catalog entry lists the trigger (`fleet/calibrations.yaml` `invalidates_on`).

- A nozzle swap fires `nozzle_swap`, plus `nozzle_diameter` when the diameter changes;
  `nozzle_install_id` is the new nozzle (the scope of flow / pressure-advance records).
- Other maintenance fires the trigger `fleet/maintenance-rules.yaml` `invalidates:` maps its
  component × kind to (PTFE replace → `ptfe`, belts tighten → `belts`, hotend replace → `hotend`, …).
- `--invalidates flow,pressure_advance` names the kinds instead (trigger: the mapped one, else
  `repair`); `--no-invalidate` fires nothing. The maintenance row keeps what it fired
  (`triggers_calibrations`).

## Maintenance log

- `log TARGET --component C --kind K` — kinds `clean lube replace tighten repair inspect
  firmware_update move`; components as above plus `printer` (the whole machine). `--kind replace`
  also records the new component (`--label` = the new part). A nozzle replacement is
  `nozzle swap`, not `log`. `--fixes 4` closes fault #4 as fixed by this maintenance — work on
  one head cannot fix another head's fault (printer-wide work can fix any fault of that printer),
  and work done before the fault was noticed cannot fix it.
  `3d printops maint log u1/T3 --component ptfe --kind replace --label "Capricorn XS" --fixes 4`
- `log TARGET --rule ID` — work a maintenance rule asks for; component and kind come from the
  rule and its counters restart. A `scope: head` rule needs `PRINTER/HEAD`, a printer-wide rule
  the printer only. `3d printops maint log u1 --rule u1-linear-shafts`
- `history PRINTER` — the log, newest first. `3d printops maint history u1 --limit 10`

## Maintenance rules and `due`

`fleet/maintenance-rules.yaml` (schema checked by `3d lint --printops`, rule
`maintenance/rules-schema`) lists tasks with thresholds — `every: {print_hours, filament_m,
tool_changes, jobs, days}`, due when **any** is reached since the rule was last done — and the
source of each interval (the U1 rules follow Snapmaker's
[core components maintenance guide](https://wiki.snapmaker.com/en/snapmaker_u1/troubleshooting/Core_Components_Maintenance_Guide),
the K1 SE rules Creality's [K1-series maintenance tips](https://wiki.creality.com/en/k1-flagship-series/k1c/maintenance-tips)).

A rule's **baseline** is the latest maintenance logged with `--rule ID`, or logged without a rule
with the same component and kind (a `scope: head` rule: on the same head). A rule never logged
counts from the printer's first job and its days are unknown: it is due ("never logged") until
logged once — use `--at` for work done earlier.

**Counters** are read READ-ONLY from the printer's job history: Moonraker (U1)
`GET /server/history/totals` and `GET /server/history/list?since=…`, per printer — print hours,
filament metres, jobs. Tool changes, per-head counters and printers without Moonraker (K1 SE
stock firmware, offline printers) stay **unknown** until the job ledger
([#170](https://github.com/alex-mextner/3d-cli/issues/170)); a rule whose every threshold is
unknown shows `no_counter`, never `ok`.

- `due [PRINTER]` — every rule × printer (× head) with status `due | soon (≥ soon_fraction) | ok |
  no_counter`, usage/limit per threshold, baseline, and for due/soon the `log` command that
  closes it. `3d printops maint due u1` ·
  `3d printops maint due u1 --json | jq '.[] | select(.status == "due") | .log_command'`
- `due [PRINTER] --check` — also announce newly due items (of PRINTER only, when given):
  `maintenance.due` (subject `PRINTER[/HEAD]`), once per rule × target × baseline. With `--json`
  it prints `{items, announced}`. 3d-api runs the same check every `check_every_min`.

## Faults

- `fault open TARGET --symptom TEXT` — something broken (`--component` optional).
  `3d printops maint fault open u1/T3 --symptom "under-extrusion after 2 h" --component extruder`
- `fault close ID` — fixed (optionally `--fixed-by MAINT_ID`, `--root-cause TEXT`) or `--wontfix`.
  `3d printops maint fault close 4 --root-cause "worn PTFE" --fixed-by 12`
- `fault ls [PRINTER]` — open faults (`--status all` for every one).

## REST and events

Scope `read` for GET, `write` otherwise:
`GET /v1/printers/{p}/components` (`?removed=true`),
`POST /v1/printers/{p}/components/nozzle-swap` `{head, reason, diameter_mm?, material?, type?, label?, at?, notes?, parts_used?}`,
`GET|POST /v1/printers/{p}/maintenance` (`{component?, kind?, rule?, head?, at?, notes?, parts_used?, label?, invalidates?, no_invalidate?, fixes?}`),
`GET /v1/printers/{p}/maintenance/due`, `GET /v1/maintenance/due` (`?status=`), `POST /v1/maintenance/check` (`?printer=`),
`POST /v1/maintenance/sync-fleet`,
`GET|POST /v1/printers/{p}/faults` (`?status=open|fixed|wontfix|all`; `{symptom, head?, component?, at?}`),
`POST /v1/faults/{id}/close` `{status?: fixed|wontfix, root_cause?, fixed_by?}`, `GET /v1/faults`,
`GET /v1/faults/{id}`.
Errors: 404 unknown printer/head/fault/rule, 409 conflicts (already closed, other printer, a date
before the mounted part), 422 invalid input, 503 broken fleet or rules file.

Events: `component.installed|removed`, `maintenance.logged`, `fault.opened|closed`,
`calibration.invalidated`, `maintenance.due`.

For the calibration engine, `printops.maintenance.current_nozzle_install(conn, printer, head)`
returns `{id, diameter_mm, material, type, installed_at}` (installed_at `None` = unknown date).
