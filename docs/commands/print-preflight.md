# `3d print-preflight` — is this G-code ready for that printer?

Read-only checks before a print starts. [`3d printer start`](printer.md) runs the same checks
and refuses on FAIL unless `--force`. Issue: [#96](https://github.com/alex-mextner/3d-cli/issues/96).

## Usage

```
3d print-preflight FILE.gcode [--printer NAME] [--source-3mf FILE.3mf] [--json] [--secs S]
```

| Check | When | FAIL means |
|---|---|---|
| `thumbnails` | always | The G-code lacks preview images its `; thumbnails =` spec asks for (fix: `3d gcode-thumbnails add FILE`). WARN `UNVERIFIED` when the thumbnail checker is not installed. |
| `printer` | `--printer` | The printer is printing, paused, busy, or its state cannot be read. |
| `slot Tn` | `--printer` on a printer with per-head data (Snapmaker U1) | Tool *n* is used by the G-code but head T*n+1* is empty, or its colour/material differs from `; filament_colour` / `; filament_type`. |
| `halves` | `--source-3mf` | `3d halves-check FILE.3mf` failed or could not run. |

Tool numbering: G-code `Tn` and the *n*-th entry of `; filament_colour` are head index *n*,
which the U1 screen calls T*n+1*. Used tools are read from `; filament used [mm]` (non-zero
entries), else from `Tn` tool-change lines, else tool 0.

Options:

- `--printer NAME` — a printer from [`3d printer list`](printer.md): `3d print-preflight out/part.gcode --printer u1`
- `--source-3mf FILE` — run the halves-check on the source 3MF: `3d print-preflight out/base.gcode --printer u1 --source-3mf print/base.3mf`
- `--json` — JSON report: `3d print-preflight out/part.gcode --printer u1 --json | jq '.checks[] | select(.status=="FAIL")'`
- `--secs S` — Creality: seconds to listen for pushed state: `3d print-preflight out/roof.gcode --printer k1se --secs 5`

Exit: 0 PASS or WARN, 1 FAIL, 2 usage error or missing file.

## Examples

```bash
3d print-preflight out/part.gcode
3d print-preflight out/part.gcode --printer u1
3d print-preflight out/base.gcode --printer u1 --source-3mf print/base.3mf
3d print-preflight out/roof.gcode --printer k1se --secs 5
3d print-preflight out/part.gcode --printer u1 --json | jq '.checks[] | select(.status=="FAIL")'
```
