# `3d slice-check` — headless 3MF verify (opens? plates? slices?)

Verifies a [`.3mf`](GLOSSARY.md#3mf) (or `.stl`) **without a GUI**, using an OrcaSlicer-family CLI binary (OrcaSlicer > Bambu Studio > the Snapmaker Orca app, auto-detected on PATH and macOS app bundles). It answers three questions and returns a single exit code:

- **OPEN** — does the slicer parse the file? (via the CLI `--info`)
- **PLATES** — how many plates does it contain? (read from the 3MF's embedded metadata)
- **SLICE** — does `--slice 0` (all plates) produce G-code for every plate? (skippable)

**Why it exists.** A 3MF can be a valid zip yet fail to slice — geometry off the bed, an incompatible printer profile, a corrupt mesh, a file version newer than the CLI. The only trustworthy "it will slice on the printer" signal is to actually run the slicer headlessly. This wraps that so any 3MF can be verified in CI or from a subagent with no GUI, no clicking, and a clean pass/fail.

## Usage

```
3d slice-check <file.3mf|file.stl> [options]
```

| Option | Default | What |
|---|---|---|
| `--no-slice` | off | Only OPEN + PLATES (never invokes the slice path; fast). |
| `--plates N` | — | Assert the file has exactly `N` plates (FAIL otherwise). |
| `--slicer PATH` | auto-detect | Force a specific slicer binary (overrides `SLICER` env). |
| `--datadir DIR` | throwaway temp dir | Slicer config dir for the slice step. The default lets upstream OrcaSlicer self-initialise its bundled profiles. |
| `--timeout SECS` | `420` | Per-slicer-call timeout. |
| `--printer NAME` | — | Informational only; recorded in the report. |
| `--gcode FILE` | — | Sliced plate G-code of this project 3MF, for offline `--check`s. |
| `--check NAME` | — | Run an offline G-code check instead of OPEN/PLATES/SLICE (no slicer needed; repeatable). Available: `head-sequence`. |
| `--json` | off | Machine-readable result (only with `--check`). |

```bash
3d slice-check model.3mf                 # open + plate count + slice all plates
3d slice-check model.3mf --no-slice      # open + plate count only (fast, never slices)
3d slice-check model.3mf --plates 4      # also assert exactly 4 plates
3d slice-check model.3mf --slicer "/Applications/OrcaSlicer.app/Contents/MacOS/OrcaSlicer"
```

### `--check head-sequence`

```bash
3d slice-check plate.3mf --gcode plate.gcode --check head-sequence --json
```

Every object must print, layer by layer, only on heads its parts' filaments call for on that
layer. A planned head missing from a layer is not a FAIL (the 3MF carries no per-part height, so
a shorter part's head legitimately disappears); an unplanned head on a layer is.
A physical filament prints on its own head; a mixed filament (`mixed_filament_definitions`,
virtual id = physical count + 1 + row) with a manual pattern (`…,cm1,14`) prints one pattern
digit per layer, at any starting phase. Raft layers (`raft_layers`) are skipped. Ratio rows
(`…,cm0`, Local-Z) have no per-layer pattern: such objects are reported `local-z`, not checked.
Heads are read from `T<n>` (`T255`/`T1000` Bambu pseudo-changes ignored), objects from
`EXCLUDE_OBJECT_START NAME=`, layers from `;LAYER_CHANGE`, and only extruding `G1` moves
with X/Y count. Per object it reports ok/FAIL/local-z, heads, patterns and layer count; exit 1
on any FAIL. On a single-plate project, a project object absent from the G-code is a FAIL;
G-code with no extrusion inside object labels is a FAIL. A non-project input (STL, bare 3MF,
corrupt metadata) exits 2 with a usage error.

Why: a plate-level "heads used == heads planned" check stays green when a mix is printed with
another row's recipe (Snapmaker Orca 2.4.0 printed a 1:1 mix cube entirely in filament 1 on a
mixed-layer-height plate). For `3d print-preflight` (#96, not yet landed) the check is exposed
as `slicing.head_sequence.preflight_check(gcode, source_3mf)` → check `head_sequence` (SKIP
when there are no mixed rows and every object is single-head).

## How the checks work

- **Plate count** is read straight from the 3MF zip — no slicer needed, so it is always available:
  1. `Metadata/model_settings.config` `<plate>` entries (`plater_id`) — the plate list the slicer maintains, present even on an *unsliced* project.
  2. else the count of `Metadata/plate_N.png` / `plate_N.json` (sliced/GUI-saved projects; `plate_no_light_N.png` decoys are ignored).
  3. else (a bare mesh / STL / plain `3D/3dmodel.model`-only 3MF) → **1 implicit plate**.
- **Open** runs `<binary> --info <file>` and requires the geometry markers (`number_of_parts`, `manifold`, …) plus exit 0.
- **Slice** runs `<binary> --datadir <dir> --allow-newer-file --slice 0 --outputdir <dir> <file>` with `QT_QPA_PLATFORM=offscreen`. Success = **exit 0 AND at least one non-empty `plate_N.gcode`** produced. (A successful slice is near-silent in the log even at `--debug 2`, so the produced-file count + exit code are the authoritative signal, not log strings.)

## Exit codes

- `0` — every requested check passed
- `1` — a check FAILED (the slicer log tail is printed for a slice failure)
- `2` — bad usage / argument
- `127` — no slicer binary found

## Slicer quirks (verified on macOS, 2026-06)

- **The Snapmaker Orca app** (an Orca fork) opens 3MFs fine via `--info`, but its **CLI segfaults when it slices** — it looks up resolved system presets under `Resources/profiles/BBL/machine_full/`, which that build does not ship (the same class as [OrcaSlicer #2661](https://github.com/SoftFever/OrcaSlicer/issues/2661): an unresolvable `inherits` parent → null deref). So `slice-check` uses whatever Orca-family binary it finds for OPEN + PLATES, but **routes the SLICE step away from the Snapmaker fork** to upstream OrcaSlicer when both are present.
- **Upstream OrcaSlicer** slices a GUI-saved project 3MF headlessly (exit 0 + `plate_N.gcode`) when given a fresh/throwaway `--datadir` so it self-initialises its bundled profiles. A project 3MF carries its own embedded settings, so no external `--profile` is needed.
- A 3MF written by a newer Orca than the CLI needs `--allow-newer-file` (always passed) or it errors on a version gate.

## Environment

- `SLICER=/path/to/binary` forces a specific slicer (same as `--slicer`).
