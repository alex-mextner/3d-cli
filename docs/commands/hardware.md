# `3d hardware` — inspect local machine capabilities

Reports the local OS, CPU count, and external tools that the CLI depends on. The command is
read-only: it does not install anything, change project files, or mutate the environment.

## Usage

```bash
3d hardware <list|validate> [--json]
```

| Subcommand | Exit | What |
|---|---:|---|
| `list` | `0` | Print the capability report even when required tools are missing. |
| `validate` | `0` or `1` | Print the report and fail when required capabilities are missing. |

| Option | What |
|---|---|
| `--json` | Emit a scriptable JSON report with OS, machine, CPU count, validity, and per-tool items. |

```bash
3d hardware list
3d hardware validate --json
```

## Reported Capabilities

The report includes OpenSCAD, ImageMagick, slicer availability, Python, `uv`, `.venv`,
`pip`, and the Python mesh stack used by mesh, check, printability, collision, and
preprocess workflows.

`list` is for diagnostics and inventory. `validate` is for CI or setup scripts that need a
non-zero exit when required capabilities are missing.
