# `3d printers` — inspect the printer registry, and see everything about one printer

`list` shows the printer MODELS the tool knows (bed/nozzle/firmware used by `3d.yaml`, slicing and
project checks). `show <name>` is the one-screen dossier of a printer: its model spec, the machine
on the desk (owner, access, IP/ports, heads, AMS, axis limits, slicer profile, settings files),
where its web UI answers, what it is doing right now, its job queue, and its job history.

## Usage

```
3d printers <subcommand>
3d printers show <name> [--json] [--no-live] [--history N] [--secs S]
```

| Subcommand | What |
|---|---|
| `list` | List known printer models with build volume, nozzle, and firmware. |
| `show <name>` | The dossier of one printer (below). |

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

## Registry Layers

Later layers override earlier ones:

1. Built-in defaults.
2. `~/.config/3d-cli/printers.yaml`.
3. `./printers.yaml` next to the active project `3d.yaml`.

Fleet/device printers come from `fleet/printers.yaml`, `~/.config/3d-cli/printer-devices.yaml` and
`./printer-devices.yaml` (see [printer.md](printer.md), "Fleet registry").
