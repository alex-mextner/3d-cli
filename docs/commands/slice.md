# `3d slice` — slice to G-code

Sends a model ([`.stl`](GLOSSARY.md#stl), [`.3mf`](GLOSSARY.md#3mf), or `.scad`) to the installed [slicer](GLOSSARY.md#slicer) and produces G-code. Slicers are auto-detected in priority order: **OrcaSlicer** > **Bambu Studio** > **PrusaSlicer** (PATH and macOS app bundles). A `.scad` input is exported to STL first via `3d export`.

**Why it exists.** The old pipeline assumed PrusaSlicer only. Modern workflows use OrcaSlicer / Bambu Studio, which take `.json` profiles and different CLI flags. This command abstracts the slicer differences so the same invocation works on any machine.

## Usage

```
3d slice <model.stl|.3mf|.scad|-> [options]
```

| Option | Default | What |
|---|---|---|
| `-o, --out PATH` | `<model>.gcode`; stdout for a `-` model | Output `.gcode`. The slicer writes to a scratch dir first; on success the result is moved here. `-o -` writes it to stdout. |
| `--dry-run` | off | Sliceability gate: slice to temp, verify G-code was produced, report OK/FAIL + est. time / filament, then discard the G-code. |
| `--check` | — | Deprecated alias for `--dry-run`. |
| `--list-profiles` | — | List slicer profile files 3d can see, then exit. |
| `--profile FILES` | — | Slicer config file(s). `.ini` for PrusaSlicer; `.json` for OrcaSlicer / Bambu Studio. Comma-separated files for machine/process/filament profiles. |
| `--printer NAME` | — | Printer/machine preset name (best-effort, slicer-flag unverified). Prefer explicit `--profile` files for repeatability. |
| `--material NAME` | `pla` for `--printer bambu-a1` auto-pick | Material selector for default profile auto-pick. Supported today: `pla`, `petg` with `--printer bambu-a1`. |
| `-D k=v` | — | Pass-through define for `.scad` export (repeatable) |

### Profile export steps

In the slicer GUI, choose the printer, process, and filament/material presets, then use the export config / export preset action. Pass the exported `.ini` / `.json` file(s) to `--profile`:

```bash
3d slice part.stl -o part.gcode
3d slice part.scad --dry-run
3d slice part.3mf --printer bambu-a1 --material pla --dry-run
3d slice part.3mf --profile "machine.json,process.json,filament.json"
3d slice --list-profiles
```

For Bambu A1, if `--profile` is omitted, `3d slice --printer bambu-a1 --material pla|petg`
searches project and installed-slicer profile folders for a complete machine/process/filament
set and passes those files to the slicer. If any piece is missing, the error explains which
forms are accepted and how to export the profiles from the slicer GUI.

## Slicer warnings

When G-code is kept (not `--dry-run`), `3d slice` records every warning and error the slicer
printed — its log, the Orca-family `result.json` and, for a `.gcode.3mf`, its `slice_info` —
in `NAME.slice-warnings.json` next to the G-code, with the G-code's SHA-256, and lists the
open ones. [`3d print-preflight`](print-preflight.md) and `3d printer start` FAIL until each
is resolved or accepted with a reason ([`3d slice-warnings accept`](slice-warnings.md)).

## In a pipe

`3d slice -` reads a 3MF from stdin — the output of [`3d 3mf`](3mf.md) — and writes the G-code to
stdout (`-o -` does the same for a file model); the report goes to stderr. The 3MF must hold
one plate (`3d 3mf arrange` puts the objects on one); more plates FAIL. The G-code is also kept
in `~/.cache/3d-cli/pipe/<sha256[:16]>/NAME.gcode` (NAME = the objects' first words, which also
names the file on the printer) with its `NAME.slice-warnings.json` and the source `NAME.3mf`, so
[`3d print-preflight -`](print-preflight.md) and [`3d printer start PRINTER - --yes`](printer.md),
receiving the same bytes, find the slicer-warnings record and the project for the head-sequence
gate. Stash directories older than a week are removed.

```bash
3d 3mf pick proj.3mf 'CN,SW*' | 3d 3mf arrange --printer u1 | 3d slice - | 3d print-preflight - > part.gcode
3d slice part.3mf -o - | 3d print-preflight - --printer k1se > checked.gcode
```

## Environment

- `SLICER=/path/to/binary` forces a specific slicer.
