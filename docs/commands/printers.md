# `3d printers` — the printer registry, one printer's dossier, its filament and its firmware

`list` shows the printer MODELS the tool knows (bed/nozzle/firmware used by `3d.yaml`, slicing and
project checks). `show <name>` is the one-screen dossier of a printer: its model spec, the machine
on the desk (owner, access, IP/ports, heads, AMS, axis limits, slicer profile, settings files),
where its web UI answers, what it is doing right now, its job queue, and its job history.

The other subcommands act on the machines of the fleet registry and are self-registering
(`lib/printers_cli/<sub>.py`, see [AGENTS.md](../../AGENTS.md)); every one works for every printer of
`fleet/printers.yaml` + `printer-devices.yaml`, nothing is keyed on a printer name:

- `spools`, `load`, `unload`, `swap`, `filament` — the filament in the printers (below).
- `fw` — firmware / root / Klipper-unlock plans per vendor profile: [printers-fw.md](printers-fw.md),
  profile schema in [../printers/firmware-profiles.md](../printers/firmware-profiles.md).

## Usage

```
3d printers <subcommand>
3d printers show <name> [--json] [--no-live] [--history N] [--secs S]
3d printers spools [PRINTER ...] [--no-live] [--strict] [--json]
3d printers load SPOOL PRINTER[/HEAD] [--replace-to LOC] [--dry-run] [--json]
3d printers unload PRINTER[/HEAD] [--to LOC] [--gross G] [--dry-run] [--json]
3d printers swap SPOOL PRINTER[/HEAD] --to LOC [--dry-run] [--json]
3d printers filament show|set ...
3d printers fw status|plan|backup|apply|profiles ...
```

| Subcommand | What |
|---|---|
| `list` | List known printer models with build volume, nozzle, and firmware. |
| `show <name>` | The dossier of one printer (below). |
| `spools` (alias `slots`) | What is loaded where: spool, remaining interval, active-spool agreement. |
| `load` / `unload` / `swap` | Put a spool into a printer slot, take it out, or exchange it (`--dry-run` everywhere). |
| `filament show` / `filament set` | What each head shows it holds; write one head's filament directly. |
| `fw` (alias `firmware`) | Firmware / root / Klipper unlock: status, plan, backup, apply. |

```bash
3d printers list
3d printers show "Prusa MK4"      # spec sheet of a model
3d printers show u1               # full dossier of the fleet printer u1
```

## `show <name>`

`<name>` is either

- a **model** from `3d printers list` (`"Snapmaker U1"`): its spec sheet, plus the full dossier of
  every fleet/device printer of that model (none → a line saying so), or
- a **fleet/device printer** from `3d printer list` (`u1`, `k1se`, `p1s`, ...): its model's spec
  sheet plus its own dossier.

A machine name wins over a model of the same name; otherwise names match exactly, then
case-insensitively when that is unambiguous. An unknown name is an `InvalidArgument` (exit 2)
listing both kinds of valid names. A broken `fleet/printers.yaml` does not stop model names from
working; a machine name then reports the fleet error itself.

Sections, in this order:

| Section | Content | Source |
|---|---|---|
| Model spec | build volume, nozzle, firmware, default material | `lib/data/printers.yaml` (+ user/project layers) |
| Machine | owner, access, adapter, address (`host:port`, `ws:` port), model, heads (nozzle diameter/material/type, `UNUSABLE: <reason>`), AMS, enclosure, chamber heater, plates, axis limits, `bridge_max_mm`, trusted-macro count, slicer profile, `watch` keys, camera snapshot URL, top camera, notes, sources, settings file paths | `fleet/printers.yaml` + `printer-devices.yaml` |
| Web UI | the URLs that actually answered, each identified by its page (`Fluidd`, `Mainsail`, `OctoPrint`, `Creality`, `Moonraker API`, else `web page`), plus the configured camera snapshot URL | read-only GETs |
| Live status | state and raw state, current file + progress, temperatures, head slots/filament — the same renderer as `3d printer status` | adapter `status()` |
| Queue | Moonraker's job queue: state and queued jobs | `GET /server/job_queue/status` |
| History | lifetime totals (jobs, print time, filament) and the last N jobs (start, status, file, duration, filament) | `GET /server/history/totals`, `/server/history/list` |

**Web UI probe.** Moonraker printers: `http://host/`, `:4408` (Fluidd's default), `:4409`
(Mainsail's default), and the Moonraker API root (`/server/info` on the device port: Moonraker
version and Klippy state). creality-stock: its built-in web UI at `http://host:<port>/`. Only what
answered (HTTP 2xx/3xx, or 401/403 flagged `login required`) is listed. The probes run
concurrently with a short timeout (2 s). `THREED_WEBUI_PORTS=80,4408,4409` overrides the probed
web ports (1-8 ports, 1-65535; for a printer behind a port-forward; anything else is an
`InvalidArgument`, exit 2). `--json` lists the candidates that did not answer in `web_ui.errors`.

**It never fails because a printer is off.** A printer that does not answer prints
`unreachable: <reason>` in its Web UI / Live status / Queue / History sections and the command exits
0; the Model spec and Machine sections always print. A creality-stock printer prints
`n/a (creality-stock adapter does not expose a queue)` (likewise a job history). A fleet printer
with `access: offline` (other people's) is never contacted at all: its live sections say
`never contacted`.

**Secrets are never printed.** Facts come from an explicit whitelist; URLs are shown without
`user:password@` and with `token`/`key`/`password` query values masked; `watch` settings are shown
as key names only (an `exec:` command may carry tokens). `notes` and `sources` of the fleet file are
printed as written: keep secrets out of them.

### Flags

| Flag | What | Example |
|---|---|---|
| `--json` | the whole dossier as one JSON object: `query`, `model`, `model_error`, `fleet_error`, `printers[]` (each: `machine`, `web_ui`, `live`, `queue`, `history`; every live section carries `status` = `ok`/`unreachable`/`unsupported`/`skipped`, `error` and `note`) | `3d printers show u1 --json` |
| `--no-live` | static facts only, zero network (what `access: offline` printers get automatically) | `3d printers show u1 --no-live` |
| `--history N` | how many past jobs to list (default 5; `0` = only the lifetime totals) | `3d printers show k1se --history 10` |
| `--secs S` | seconds to wait per read and per web probe (default 10 for the printer, 2 for the probes); the same flag as `3d printer status`, for creality-stock it also sets the WebSocket listen window | `3d printers show k1se --secs 3` |

```bash
3d printers show u1 --json | jq '.printers[0].history.totals'
3d printers show u1 --no-live --json | jq '.printers[0].machine.heads[] | select(.usable | not)'
```

Reading a printer is the same GET-only access as `3d printer status`; `show` can never start,
cancel, upload, heat or move anything. See [printer.md](printer.md) for the commands that drive
a printer.

## Filament in the printers: `spools`, `load`, `unload`, `swap`, `filament`

These are the fleet-addressed front of `3d printops spool ...` and the printers' own filament
state. They talk to 3d-api (`PRINTOPS_API`/`PRINTOPS_TOKEN` or `~/.config/3d-cli/printops.yaml`, see
`3d printops --help`) and, for the live parts, to the printers through the normal adapters. A slot is
`PRINTER/HEAD` (`u1/T3`, an AMS slot `p1s/ams1/2`); the head may be left out for a printer with exactly
one slot (`k1se`). Heads are matched case-insensitively against the slots the fleet declares; an unknown
printer or slot is an `InvalidArgument` (exit 2) listing the valid ones.

### `spools [PRINTER ...]` (alias `slots`)

```bash
3d printers spools
3d printers spools k1se --strict
3d printers spools --no-live --json | jq '.printers[].slots[] | select(.spools == [])'
3d printers spools --json | jq '.printers[] | select(.active_spool.status == "mismatch") | .name'
```

Per printer and slot: what the printer reports loaded, the spool bound in Spoolman with its remaining
filament as an interval (mid ± σ g, from the PrintOps ledger), and the slot's open anomalies. Then one
line, **active spool**, says whether Moonraker's ONE active spool (the spool its own usage reporting books to)
is the spool PrintOps has bound:

| `active_spool.status` | Meaning |
|---|---|
| `agrees` | the active spool is the bound one (or none on both sides) |
| `mismatch` | usage is booked to the wrong spool, or none while a spool is bound |
| `not-applicable` | a multi-head printer (one active spool cannot follow tool changes: the U1), or a Moonraker without `[spoolman]` |
| `unreachable` | the printer did not answer (the report still prints, exit 0) |
| `skipped` | offline fleet printer (never contacted), or `--no-live` |

| Flag | What |
|---|---|
| `--no-live` | do not ask the printers; 3d-api data only |
| `--strict` | exit 1 when any printer's active spool disagrees (a gate for scripts/CI) |
| `--secs S` | seconds to wait per printer read (default 10) |
| `--json` | the report: `printers[]` (`name`, `model`, `access`, `adapter`, `state`, `slots[]`, `active_spool`) + `spools_error` |
| `--api URL`, `--token TOKEN` | 3d-api location and bearer token (default: env / `printops.yaml`) |

Reads only; it never writes to 3d-api or a printer. Exit codes: 0, 1 (`--strict` mismatch or 3d-api
unreachable/refused), 2 (usage).

### `load`, `unload`, `swap`

```bash
3d printers load 7 u1/T3
3d printers load 2 k1se
3d printers load 7 u1/T3 --replace-to WH:default --dry-run
3d printers unload u1/T3 --to WH:default --gross 803
3d printers unload k1se --to WH:default
3d printers swap 9 u1/T2 --to WH:default
3d printers swap 9 k1se --to WH:default --dry-run
```

A real run is the very `3d printops spool load|unload` call (same 3d-api rules, answers, warnings and
`--json` shape; see `3d printops spool --help` for what is written to the printer: the Snapmaker U1 head
filament, Moonraker's active spool on a single-head printer). What these add:

- **`--dry-run`** (all three): read-only. Prints what would be bound / moved out / written, and which
  rule would refuse it — a head the fleet marks unusable, an open fault on the head, an occupied slot
  without `--replace-to`, an archived spool, nothing to unload. Exit 1 when it predicts a refusal, so a
  script can ask first. Nothing is written to 3d-api or the printer (`--json` prints the plan:
  `steps`, `notes`, `refusals`, `occupants`).
- **`swap SPOOL SLOT --to LOCATION`**: the slot's spool goes to `LOCATION` and `SPOOL` takes its place in
  one 3d-api transaction (`load --replace-to`); `--to` is required so a spool never leaves without a place.
  An empty slot is a plain load (a note says so). To weigh the old spool on the way out run
  `unload --gross G` first, then `load`.
- `SPOOL` is a Spoolman id (`7` or `#7`; `3d printops spool ls` lists them).

Flags besides `--dry-run`: `--replace-to LOCATION` (load), `--to LOCATION` and `--gross G` (unload),
`--no-printer-write` (change Spoolman only), `--force-official` (load/swap: also overwrite a head
showing official RFID filament), `--json`, `--api`, `--token`. Exit codes: 0 done / clean dry run, 1 refused
or failed (a 3d-api rule, the printer, or a predicted refusal), 2 usage.

### `filament show` / `filament set`

```bash
3d printers filament show
3d printers filament show u1 --json | jq '.printers[0].slots[] | select(.present | not)'
3d printers filament set u1/T2 --color '#C62828' --material PETG --vendor Elegoo
3d printers filament set u1/T2 --color '#C62828' --material PETG --dry-run
3d printers filament set u1/T1 --color '#FFFFFF' --material PLA --force-official
```

`show` reads the filament of every head straight from the printers (default: every contactable fleet
printer): colour, material, vendor, sub-type. An unreachable printer prints `unreachable: <reason>` and the
command still exits 0; offline fleet printers are never contacted. `set` writes ONE head through
`Adapter.set_head_filament` (the Snapmaker U1's `SET_PRINT_FILAMENT_CONFIG`, verified by reading it back) and
prints the head's pressure advance afterwards: the firmware resets it on this write. Refused: a printer that
is not `access: full`, one that is printing/paused/unreadable, a head with official RFID filament without
`--force-official`; an adapter without head-filament writes (`creality-stock`) is an `UnsupportedOperation`
(exit 2). Vendor defaults to `Generic`, sub-type to `Basic`. `--dry-run` reads the head and the job state,
prints before/after and the refusals it predicts (exit 1 then). Spoolman is not touched: a spool bound to
that slot keeps its binding and PrintOps may report a slot mismatch; use `3d printers load` for a Spoolman
spool.

## Registry Layers

Later layers override earlier ones:

1. Built-in defaults.
2. `~/.config/3d-cli/printers.yaml`.
3. `./printers.yaml` next to the active project `3d.yaml`.

Fleet/device printers come from `fleet/printers.yaml`, `~/.config/3d-cli/printer-devices.yaml` and
`./printer-devices.yaml` (see [printer.md](printer.md), "Fleet registry").
