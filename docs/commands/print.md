# `3d print`

`3d print` validates the fields needed for a future printer job and emits a deterministic
dry-run plan as JSON. It does not contact a printer, upload files, or start hardware yet;
the command is a safe planning surface for scripts, agents, and CI.

## Usage

```bash
3d print <model.scad|model.stl|model.3mf|job.gcode> --printer NAME --dry-run [options]
```

Required:

- `--dry-run` keeps the command in planning mode.
- `--printer NAME` selects a printer from `3d printers list`.

Options:

- `--job-name NAME` sets the display name. The input file stem is used when omitted.
- `--material NAME` records a known material from `3d materials list`.
- `--copies N` records a positive copy count.
- `--start` records the intent to upload and start, instead of upload only.
- `--machine-profile FILE` records a slicer machine profile, `.json` or `.ini`.
- `--process-profile FILE` records a slicer process profile, `.json` or `.ini`.
- `--filament-profile FILE` records a slicer filament profile, `.json` or `.ini`.

## Examples

Plan a simple upload-only job:

```bash
3d print part.stl --printer "Prusa MK4" --dry-run
```

Plan a two-copy start intent from a 3MF project:

```bash
3d print part.3mf --printer "Bambu Lab A1" --dry-run --start --copies 2
```

Set the job display name and known material:

```bash
3d print bracket.stl --printer "Prusa MK4" --dry-run --job-name "left bracket" --material PLA
```

Save a plan for review or a later sender:

```bash
3d print bracket.stl --printer "Prusa MINI" --dry-run > print-plan.json
```

Inspect a saved plan in a shell pipeline:

```bash
cat print-plan.json | python3 -c 'import json,sys; print(json.load(sys.stdin)["steps"])'
```

Record explicit slicer profiles:

```bash
3d print bracket.stl --printer "Prusa MK4" --dry-run --machine-profile profiles/machine.json --process-profile profiles/process.ini --filament-profile profiles/pla.json
```

## Output

The JSON includes:

- `plan_id`: a stable hash of the normalized plan.
- `input_path` and `input_format`.
- `printer`: resolved printer name, bed volume, nozzle, firmware, and default material.
- `profiles`: resolved profile paths.
- `job`: name, material, copies, and start intent.
- `steps`: planned workflow steps such as `validate input`, `slice model`, `upload job`,
  and optionally `start print`.

For `.gcode` inputs, the plan skips the `slice model` step because the file is already a
printer job artifact.
