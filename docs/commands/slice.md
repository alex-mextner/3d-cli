# `3d slice` — slice to G-code

Sends a model ([`.stl`](GLOSSARY.md#stl), [`.3mf`](GLOSSARY.md#3mf), or `.scad`) to the installed [slicer](GLOSSARY.md#slicer) and produces G-code. Slicers are auto-detected in priority order: **OrcaSlicer** > **Bambu Studio** > **PrusaSlicer** (PATH and macOS app bundles). A `.scad` input is exported to STL first via `3d export`.

**Why it exists.** The old pipeline assumed PrusaSlicer only. Modern workflows use OrcaSlicer / Bambu Studio, which take `.json` profiles and different CLI flags. This command abstracts the slicer differences so the same invocation works on any machine.

## Usage

```
3d slice <model.stl|.3mf|.scad> [options]
```

| Option | Default | What |
|---|---|---|
| `-o, --out PATH` | `<model>.gcode` | Output `.gcode`. The slicer writes to a scratch dir first; on success the result is moved here. |
| `--dry-run` | off | Sliceability gate: slice to temp, verify G-code was produced, report OK/FAIL + est. time / filament, then discard the G-code. |
| `--check` | — | Deprecated alias for `--dry-run`. |
| `--list-profiles` | — | List slicer profile files 3d can see, then exit. |
| `--profile FILES` | — | Slicer config file(s). `.ini` for PrusaSlicer; `.json` for OrcaSlicer / Bambu Studio. Comma-separated files for machine/process/filament profiles. |
| `--printer NAME` | — | Printer/machine preset name (best-effort, slicer-flag unverified). Prefer explicit `--profile` files for repeatability. |
| `-D k=v` | — | Pass-through define for `.scad` export (repeatable) |

### Profile export steps

In the slicer GUI, choose the printer, process, and filament/material presets, then use the export config / export preset action. Pass the exported `.ini` / `.json` file(s) to `--profile`:

```bash
3d slice part.stl -o part.gcode
3d slice part.scad --dry-run
3d slice part.3mf --profile "machine.json,process.json,filament.json"
3d slice --list-profiles
```

## Environment

- `SLICER=/path/to/binary` forces a specific slicer.
