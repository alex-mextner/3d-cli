# `3d lint` — advisory repository and model lint rules

Runs lightweight advisory lint rules. This is not the full acceptance gate; use
`dev run test` for the repository gate and `3d check` for geometry/printability acceptance.

## Usage

```
3d lint [--all | paths...] [options]
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
