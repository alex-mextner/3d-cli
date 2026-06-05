# AGENTS.md — `3d-cli`

Instructions for AI agents (and humans) working in this repository. English only.

## What this is

`3d` is a scriptable, cross-platform CLI for AI-assisted parametric 3D modeling in
OpenSCAD: camera-locked renders, cross-sections, silhouette scoring, a forced-monotonic
match loop, and manifold / printability / collision verification gates. The entry point
is `bin/3d` (a bash dispatcher); subcommands live in `lib/cmd_<name>.sh` (bash) or are
thin wrappers around typed Python tools in `lib/` run through `lib/pyrun`.

## Engineering conventions

### Python
- **Typed.** Every Python module starts with `from __future__ import annotations` and has
  full type hints on functions (params + returns). Keep it **mypy-clean**:
  ```bash
  uv run --with mypy mypy lib/*.py
  ```
  Third-party libs without stubs (trimesh, manifold3d, open3d, cv2, scipy, pyvista) are
  handled by `mypy.ini` (`ignore_missing_imports` per module) — never a blanket
  `ignore_errors`, which would fake "clean".
- **Async where it genuinely helps.** Independent OpenSCAD renders (multi-angle batches,
  fit-camera candidate evals, match-loop candidate evals) run concurrently via `asyncio`
  + `asyncio.create_subprocess_exec` / `gather`, bounded by a semaphore (~`os.cpu_count()`).
  Keep a correct, ordered single-render path; do NOT async-ify trivially-sequential code.
- Run Python through `lib/pyrun "<deps>" script.py ...` (resolves `.venv` → `uv` → system).

### Bash
- `set -euo pipefail` where practical (at minimum `set -uo pipefail`, the repo standard).
- Quote everything; arrays for argv pass-through (`"${arr[@]+"${arr[@]}"}"`).
- **Cross-platform macOS + Linux.** No GNU-only flags without a fallback (macOS `readlink`
  has no `-f`; `sed -i` differs; etc.). Robust binary discovery (PATH + app bundles).
- Clear errors with the exact install command; degrade gracefully when an optional dep is
  absent (report the degradation, never a silent false PASS).

### First-run bootstrap
On ANY `3d` invocation, if `~/.config/3d/.bootstrapped` is absent, the dispatcher
auto-installs the OpenSCAD libraries (BOSL2, NopSCADlib) into the repo `libs/` ONCE,
quietly (one-line notice), then touches the marker. It is **idempotent** and **non-fatal
if offline** (must never block `render`/`help`). `OPENSCADPATH` is auto-exported from
`libs/` by `lib/common.sh` so `include <BOSL2/std.scad>` resolves with no manual step.

## Commit discipline (mandatory, every change)

1. **Atomic commits** — one logical change each. Message form: `<area>: <what changed>`
   (e.g. `render: compute --view camera from bounding box`). No "update"/"fix" vagueness.
2. **Before every commit** run `codex exec review --uncommitted` (use
   `timeout 1200 codex exec review --uncommitted` if slow), READ its findings, and fix
   the real issues before committing. Codex is a peer reviewer, not a rubber stamp.
3. **Push regularly** — after each commit or small batch: `git push origin main`. Finish
   with local `main` level with `origin/main`.
4. Don't mix unrelated changes in one commit.

Co-Authored-By trailer on commits: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`

## Command surface (post-refactor)

- `render` is the unified view/section command: `--view <name>`, `--multi [outdir]`,
  `--section [--plane …] [--color] [--keep …] [--module …]`, `--cam` manual override.
- `check` is the unified verification command (the acceptance master gate): with no
  selection flags it runs ALL applicable gates (manifold, consistency, printability,
  collision, silhouette); `--mesh/--printability/--collision/--manifold/--silhouette`
  select a subset; `--skip X` excludes. Prints a per-gate breakdown + overall PASS/FAIL.
- `multi`, `section`, `mesh`, `printability`, `collision`, `acceptance` remain as **thin
  aliases** forwarding to `render --multi` / `render --section` / `check --mesh` / etc.
- `libs install` is removed (bootstrap handles it); `libs path` / `libs list` stay (info).

## Verification before "done"

Run the full list, not a subset:
- `3d render f --view left`, `--view 3-4`, `--multi`, `--section --plane YZ --color`
  produce correct PNGs on an **asymmetric** model (cube is square in XY — left/front look
  identical on a cube; verify direction on `-D depth=40` or an L-shaped fixture). A section
  PNG must visibly show the **cavity** — "a PNG exists" is not proof it cut.
- `3d check examples/cube.scad` runs all gates by default; `--mesh` alone runs only mesh.
- First-run bootstrap: `rm ~/.config/3d/.bootstrapped` then any `3d` cmd re-bootstraps.
- `mypy` clean on the Python modules; aliases still work.
- Everything committed atomically, codex-reviewed, and PUSHED to origin.
