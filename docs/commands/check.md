# `3d check` — master acceptance gate

The unified verification command. With no selectors it runs **all** applicable gates (manifold, consistency, printability, plus collision / silhouette when their data is supplied). With selectors it runs only the chosen subset. Prints a per-gate breakdown and a single `PASS` / `FAIL` verdict.

**Why it exists.** Previously there were separate bash scripts for every gate. A single `check` command means one invocation gives you the full picture before slicing or printing, and the exit code is the single source of truth for CI.

## Usage

```
3d check <file.scad> [more parts...] [options]
```

### Core-gate selectors (any combination runs ONLY those core gates)

| Flag | Gate |
|---|---|
| `--manifold` / `--mesh` | Manifold / watertight |
| `--consistency` | `assert()` consistency (grep `ERROR:` / `Assertion`) |
| `--printability` | FDM printability (walls / overhangs) |
| `--skip GATE` | Exclude a gate (repeatable) — the way to get a subset |

### Data-driven gates (run whenever their data is supplied)

| Flag | Gate |
|---|---|
| `--collision CFG.json` | Collision / penetration (HARD; runs when config given) |
| `--silhouette` / `--ref IMAGE` | Silhouette IoU / AE vs reference (ADVISORY; runs when `--ref` given) |

### Other options

| Option | Default | What |
|---|---|---|
| `--part FILE` | — | Additional part to gate (or just pass extra positional files) |
| `--ref IMAGE` | — | Reference image for the silhouette gate |
| `--cam ex,..,cz` | `125,-330,52,125,28,44` | 6-param vector camera for silhouette render |
| `--size WxH` | `1100x480` | Silhouette render size |
| `-D k=v` | — | Pass-through define (repeatable) |

```bash
3d check examples/cube.scad                 # all applicable gates
3d check examples/cube.scad --mesh          # only the manifold gate
3d check asm.scad --skip printability
3d check asm.scad --collision verify/collision.json --ref ref.jpg
```

## Exit codes

- `0` — all **HARD** gates passed
- `1` — at least one HARD gate failed

## Implementation notes

Gate sub-steps shell out to `bin/3d <gate>` (or OpenSCAD directly) and parse the same stdout markers the original bash version relied on (`>>> MESH CHECK: FAIL`, `ModuleNotFoundError` → `SKIP`, etc.). This preserves exact behavior and graceful degradation when the mesh stack is unavailable.
