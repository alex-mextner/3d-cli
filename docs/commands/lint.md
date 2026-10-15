# `3d lint` — advisory repository and model lint rules

Runs lightweight advisory lint rules. This is not the full acceptance gate; use
`dev run test` for the repository gate and `3d check` for geometry/printability acceptance.

## Usage

```
3d lint [--all | paths...] [options]
3d lint --printops [--live] [--fleet FILE] [--json]
```

| Option | What |
|---|---|
| `--all` | Scan repository `lib/*.py` and `lib/**/*.py`. |
| `--json` | Print machine-readable output. For `.scad` model lint this is equivalent to `--format json`. |
| `--rule RULE` | Run one rule. Repository rule: `no-subject-leakage`; model rules are shown by `--list-rules`. |
| `--format text|json` | Model lint report format. |
| `--strict` | Fail when model warnings are present. |
| `--off ID` | Disable a model rule for this run. |
| `--warn ID` | Set a model rule to warning level for this run. |
| `--error ID` | Set a model rule to error level for this run. |
| `--list-rules` | Print registered model rules. |
| `--printops` | Lint the PrintOps data instead of code (below). |
| `--live` | With `--printops`: also compare each reachable printer's heads and nozzles with the fleet (read-only). |
| `--fleet FILE` | With `--printops`: lint FILE instead of `<repo>/fleet/printers.yaml`. |

```bash
3d lint --all
3d lint lib/preprocess_reference.py
3d lint --all --rule no-subject-leakage --json
3d lint examples/cube.scad --format json
3d lint bracket.scad --strict --error naming/id-kebab
3d lint bracket.scad --format json | jq '.files[].findings[].rule_id'
```

Model lint reads OpenSCAD metadata comments such as `// @part body-shell`,
`// @anchor clip-left`, and `// @view front-left`. It warns about unknown tags,
missing tag values, duplicate object-model ids, and non-kebab-case identifiers before
the model reaches heavier render/check loops.

Exit codes: 0 for no failing findings, 1 for repository findings or model findings that
fail the selected levels, 2 for invalid invocation.
(`--printops`: 1 only for errors; warnings are printed but pass.)

## PrintOps data (`--printops`)

Rules for [`fleet/printers.yaml`](../../fleet/printers.yaml) (see [`3d printer`](printer.md#fleet-registry)),
run in CI by `tests/e2e/test_printops_fleet_workflows.py` on the repo file and on broken fixtures:

| Rule | Checks |
|---|---|
| `fleet/schema` | the file parses; every key is known; owner/access/head/nozzle/AMS/plate values are in their sets; heads are T1..Tn at index 0..n-1; an unusable head has a reason; offline printers carry no device settings, full/read-only ones have `adapter` and `host` |
| `fleet/device` | the device settings are valid `printer-devices.yaml` settings |
| `fleet/model-known` | `model` is in [`3d printers list`](printers.md) |
| `fleet/live-topology` | `--live` only: head count = the printer's extruders, each head's `extruder` exists, nozzle diameters match the printer config. Read-only: one Moonraker `GET /printer/objects/query?configfile=settings` or one Creality status read per printer; offline printers are never probed; an unreachable printer is a warning |

```bash
3d lint --printops
3d lint --printops --live --json | jq '.findings[] | select(.level == "error")'
3d lint --printops --fleet tests/broken-fleet.yaml
```
