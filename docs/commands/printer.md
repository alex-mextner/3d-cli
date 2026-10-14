# `3d printer` — drive the printers on your LAN

`3d printer` talks to the physical printers listed in `printer-devices.yaml`: it reads their
state, stores G-code on them, starts an uploaded file after an explicit `--yes`, and cancels
a job. `3d printers` (plural) is a different command: it lists printer **models** and their
build volumes; `3d printer` is about the machines themselves.

Epic: [#90](https://github.com/alex-mextner/3d-cli/issues/90).

## Usage

```
3d printer list
3d printer status NAME [--json] [--secs S]
3d printer upload NAME FILE.gcode [--as REMOTE]
3d printer start NAME FILE.gcode --yes [--as REMOTE] [--source-3mf X.3mf] [--force] [--secs S]
3d printer cancel NAME --yes
```

| Subcommand | What |
|---|---|
| `list` | Configured printers: name, adapter, address, model, and the file each came from. |
| `status NAME` | State, current file and progress, temperatures, and (U1) what each head holds. |
| `upload NAME FILE` | Store the file on the printer and verify its size there. Never starts a print. |
| `start NAME FILE --yes` | Start a file that was uploaded before. Runs [`3d print-preflight`](print-preflight.md) first. |
| `cancel NAME --yes` | Cancel the running or paused job. |

Options:

- `--json` — `status` as JSON: normalized `state`, `raw_state`, `filename`, `progress` (0..1),
  `temps`, `slots`, and the raw firmware payload.
  `3d printer status u1 --json | jq '.slots[] | {label, color, material}'`
- `--secs S` — Creality printers push their state over a WebSocket; listen this long (default 3).
  `3d printer status k1se --secs 5`
- `--as REMOTE` — the name stem on the printer (default: the local name with spaces turned into `_`).
  The stored name always gets the first 16 hex digits of the file's SHA-256 appended
  (`roof_left-1a2b3c4d5e6f7a8b.gcode`), so `start` finds exactly the bytes that were uploaded; pass the
  same `--as` to `start`.
  `3d printer upload k1se "out/roof left.gcode" --as roof_left.gcode`
- `--yes` — required by `start` and `cancel`: you confirm the physical action.
  `3d printer start u1 out/part.gcode --yes`
- `--force` — start even when preflight reports FAIL; the failing checks are printed as `FORCED`.
  `3d printer start k1se out/roof.gcode --yes --force`
- `--source-3mf FILE` — the project 3MF the G-code was sliced from; preflight then runs the
  head-sequence check on it and `3d halves-check` (without it, the head-sequence check uses
  `NAME.3mf` next to `NAME.gcode` when present).
  `3d printer start u1 out/base.gcode --yes --source-3mf print/base.3mf`

## Examples

```bash
3d printer list
3d printer status u1
3d printer status k1se --secs 5
3d printer status u1 --json | jq '.slots[] | {label, color, material}'
3d printer upload u1 out/part.gcode
3d printer upload k1se "out/roof left.gcode" --as roof_left.gcode
3d printer start u1 out/part.gcode --yes
3d printer start u1 out/base.gcode --yes --source-3mf print/base.3mf
3d printer start k1se out/roof.gcode --yes --force
3d printer cancel k1se --yes
```

## Configuring printers

`~/.config/3d-cli/printer-devices.yaml` (honours `XDG_CONFIG_HOME`); a `./printer-devices.yaml`
in the current directory overrides entries by name. Only printers listed here are ever
contacted — there is no network discovery.

```yaml
u1:
  adapter: moonraker        # Klipper + Moonraker (Snapmaker U1, Voron, ...)
  host: 192.168.0.26
  port: 7125                # default 7125
  model: Snapmaker U1
k1se:
  adapter: creality-stock   # Creality K1 family on stock firmware
  host: 192.168.0.27
  model: Creality K1 SE     # port defaults to 80, ws_port to 9999
  axis_limits: {x: [-5, 229], y: [-0.5, 227]}
  trusted_macros: [START_PRINT, END_PRINT, PAUSE]
```

`axis_limits` is the real X/Y travel (Klipper `stepper_x`/`stepper_y` `position_min`/`position_max`;
for the K1 SE from CrealityOfficial K1_Series_Klipper `config/K1_SE_CR4CU220812S12/printer.cfg`).
[`3d gcode-bounds`](gcode-bounds.md) and the preflight check every move against it. Moonraker
printers without `axis_limits` report them from their own Klipper config.

`resume:` holds the printer-specific values of [`3d gcode-resume`](gcode-resume.md) (off-bed
purge macro and lengths, travel clearance, start commands); see that page for the keys.

`trusted_macros` names the macros (vendor start/end, pause, toolchange `T0`…`Tn`, timelapse
commands) whose bodies you know keep the head on the bed and leave the coordinate modes
(G90/G91, G92, SET_GCODE_OFFSET) as they found them. The bounds check cannot see inside a
macro, so any macro in the G-code that is not listed makes preflight FAIL and `start` needs
`--force`. With `--printer A --axis-limits B` only A's list counts.

## Adapters

| Adapter | Printers | Transport |
|---|---|---|
| `moonraker` | Klipper/Moonraker: Snapmaker U1 and others | HTTP: `/printer/objects/query`, `/server/files/upload` (`print=false`), `/printer/print/start`, `/printer/print/cancel`. On the U1, `print_task_config` gives the colour and material in each head; a head without filament (the broken T2) shows as `empty`. |
| `creality-stock` | Creality K1 / K1 SE on stock firmware (no Moonraker) | HTTP `POST /upload/<name>` with only the `file` field; WebSocket `:9999` for state, file list, `opGcodeFile` start and `stop`. The heartbeat is answered automatically. States: 0 idle, 1 printing, 2 complete (seen on our K1 SE); 5 paused, 4 stopped, 7 cancel finishing, `withSelfTest` 1–99 self-test (from the [ha_creality_ws](https://github.com/3dg1luk43/ha_creality_ws/blob/a8e5491809227ff3ce72bc6b317cc75075e19b9f/custom_components/ha_creality_ws/utils.py#L640-L690) integration). A non-zero `err` code shows as `error` unless the printer is busy. |

The Creality upload sends **only** the `file` field: with an extra `path` field the K1 SE
stored 165–168 bytes more than it received
([#99](https://github.com/alex-mextner/3d-cli/issues/99)). The stored size is always compared
with the local file.

## Safety

([#95](https://github.com/alex-mextner/3d-cli/issues/95))

1. `upload` only stores the file. On Moonraker it sends `print=false` and fails loudly if the
   reply says a print was started or queued.
2. `start` and `cancel` need `--yes`. Without it nothing is sent to the printer.
3. `start` refuses while the printer is printing, paused, busy, or its state cannot be read.
   The file name on the printer carries the content hash, so a re-sliced local file never
   matches a stale upload; a missing file or a size difference refuses the start. Where the
   firmware can return the file (Moonraker), `start` reads it back and compares the SHA-256;
   different bytes refuse the start. The state is read again right before the start request.
4. `start` runs `3d print-preflight` — every G-code gate: the move-bounds check against the
   printer's axis limits and the macro check ([`3d gcode-bounds`](gcode-bounds.md)), the
   overhang check ([`3d gcode-overhangs`](gcode-overhangs.md)), the slicer-warnings record
   next to the G-code ([`3d slice-warnings`](slice-warnings.md)), the head-sequence check
   (project 3MF from `--source-3mf` or next to the G-code), thumbnails; a FAIL blocks the
   start unless `--force`.
5. `upload` refuses while a Creality printer is busy (the firmware does not protect the
   running job), and on any printer refuses to overwrite the file being printed.
6. `start` succeeds only when the printer acknowledges it: Moonraker's HTTP reply, or on
   Creality a pushed state naming this file as running within 15 s. No acknowledgement, a
   failed request, or an unreadable state right after an acknowledged start is reported as
   UNKNOWN; uploads that time out are checked against the printer's file list. Nothing is
   resent automatically: check `3d printer status NAME` first.

Exit codes: 0 done; 1 refused by a safety rule, preflight FAIL, or printer unreachable;
2 usage error, unknown printer, bad config, or missing file.
