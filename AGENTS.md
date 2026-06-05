# AGENTS.md — `3d-cli`

Instructions for AI agents (and humans) working in this repository. English only.

## What this is

`3d` is a scriptable, cross-platform CLI for AI-assisted parametric 3D modeling in
OpenSCAD: camera-locked renders, cross-sections, silhouette scoring, a forced-monotonic
match loop, and manifold / printability / collision verification gates. The entry point
is `bin/3d` — a **thin typed Python dispatcher** that discovers self-registering command
modules from `lib/commands/<name>.py`. Heavy python tools (render/mesh/collision/…) live
in `lib/*.py` and run through `cli.pyrun`.

## Adding a command (the command-authoring contract)

A command is ONE module `lib/commands/<name>.py` that defines a module-level `COMMAND`:

```python
from __future__ import annotations
from cli.registry import Command

def run(argv: list[str]) -> int:    # argv = everything after the subcommand name
    ...                              # do the work; return a process exit code
    return 0

COMMAND = Command(
    name="mything",
    group="GEOMETRY & EXPORT",       # which `3d help` section it appears under
    summary="one-line help in `3d help`",
    usage="mything <file> [options]",
    run=run,
    aliases=("mt",),                  # optional
)
```

Discovery globs `lib/commands/*.py`, imports each, reads `COMMAND`. **Adding a command
requires ZERO edits to `bin/3d` or any shared file** — just the new module.

HARD RULES for command modules (enforced by `tests/test_imports.py`):
- **Stdlib-only + import-light at module top level.** Discovery imports EVERY command on
  EVERY `3d` invocation, so a top-level `import trimesh`/`numpy`/`cv2` would slow/break
  ALL commands and defeat the offline `3d help`/`render` guarantee. Reach heavy deps and
  external binaries (openscad/magick/slicer) via subprocess (`cli.pyrun.exec_tool` /
  `run_tool`) or a LAZY import inside `run()` — never at the top.
- **Raise structured errors, don't `sys.exit` with ad-hoc strings.** Use `lib/errors.py`:
  `MissingDependency` (exit 127), `InvalidArgument`/`UsageError`/`InputNotFound` (exit 2),
  `GateFailure` (exit 1). Every NEW error you write MUST use these — they carry WHAT/WHY,
  the remediation, the accepted values, and the install command. The dispatcher renders
  them (no bare traceback) and maps the exit code.
- **`--help`/no-args print usage** and return 0 / 1 respectively.

Aliases: declare `aliases=(...)` (e.g. `check` aliases `acceptance`), OR write a tiny
dedicated module whose `run()` reshapes argv and calls the target's `run` (e.g.
`commands/multi.py` → `commands.render.run([file, "--multi", ...])`). Both are fine.

Shared support modules: `cli/registry.py` (the registry), `cli/dispatch.py` (routing +
error rendering), `cli/env.py` (tool discovery, OS/install table, bootstrap),
`cli/pyrun.py` (run a `lib/*.py` tool with its deps), `cli/imaging.py` (ImageMagick
orchestration + the pure score math).

## Testing

```bash
3d test            # ruff, pytest (unit + CLI smoke harness), then mypy — all must pass
3d test -k errors  # forward args to pytest
```

Unit tests live in `tests/` (registry/alias resolution, errors formatting, score IoU/AE
math, param parsing, env helpers). `tests/test_cli_smoke.py` runs `3d <cmd> --help` for
EVERY registered command and the safe commands on `examples/cube.scad` (skipping when a
tool is absent). `tests/test_imports.py` enforces the stdlib-only rule. `3d test` also runs
ruff over `lib/ + tests/` and mypy over `bin/3d + lib/ + tests/` — keep both clean.

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
- **Zero warnings.** Both `mypy` and `ruff` must run clean with **no warnings** before any
  commit. Treat every linter warning as an error — fix it immediately. Do not add blanket
  ignores or noqa markers to silence real issues. Run the full lint gate:
  ```bash
  uv run ruff check lib/ tests/ && uv run mypy lib/ tests/
  ```
- **Async where it genuinely helps.** Independent OpenSCAD renders (multi-angle batches,
  fit-camera candidate evals, match-loop candidate evals) run concurrently via `asyncio`
  + `asyncio.create_subprocess_exec` / `gather`, bounded by a semaphore (~`os.cpu_count()`).
  Keep a correct, ordered single-render path; do NOT async-ify trivially-sequential code.
- Run a bundled python tool through `cli.pyrun` (`exec_tool`/`run_tool("<deps>",
  "tool.py", args)`) — it resolves `.venv` → `uv` → system, the same tiers as the old
  `lib/pyrun` shim.

### No new bash
The CLI is Python everywhere. There are no `lib/cmd_*.sh` / `common.sh` / `pyrun` shims
anymore. Don't add bash; write a Python command module. (Cross-platform care still
applies: robust binary discovery on PATH + macOS app bundles, clear errors with the exact
install command, graceful degrade — never a silent false PASS. `cli/env.py` is the home
for tool discovery + the OS/install table.)

### First-run bootstrap
On ANY `3d` invocation, if `~/.config/3d-cli/.bootstrapped` is absent, the dispatcher
auto-installs the OpenSCAD libraries (BOSL2, NopSCADlib) into the repo `libs/` ONCE,
quietly (one-line notice), then touches the marker. It is **idempotent** and **non-fatal
if offline** (must never block `render`/`help`). `OPENSCADPATH` is auto-exported from
`libs/` by `cli/env.export_openscadpath()` (called in the dispatcher before any
subprocess) so `include <BOSL2/std.scad>` resolves with no manual step.

## Commit discipline (mandatory, every change)

1. **Atomic commits** — one logical change each. Message form: `<area>: <what changed>`
   (e.g. `render: compute --view camera from bounding box`). No "update"/"fix" vagueness.
2. **Before every commit** run `codex exec review --uncommitted` (use
   `timeout 1200 codex exec review --uncommitted` if slow), READ its findings, and fix
   the real issues before committing. Codex is a peer reviewer, not a rubber stamp.
3. **Push regularly — do NOT let work sit only on your local machine.** This project works
   directly on `main` (that is where all history lives — no feature-branch dance). After each
   commit or small batch, push: `git push origin main`. Pushing often means the work survives a
   crash and is always visible. Never end a working session with unpushed commits — finish with
   local `main` level with `origin/main`.
4. Don't mix unrelated changes in one commit.

Co-Authored-By trailer on commits: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`

## Command surface (post-refactor)

- `render` is the unified view/section command: `--view <name>`, `--multi [outdir]`,
  `--section [--plane …] [--color] [--keep …] [--module …]`, `--cam` manual override.
- `check` is the unified verification command (the acceptance master gate): with no
  selection flags it runs ALL applicable gates (manifold, consistency, printability,
  collision, silhouette); `--mesh/--printability/--collision/--manifold/--silhouette`
  select a subset; `--skip X` excludes. Prints a per-gate breakdown + overall PASS/FAIL.
- `multi`/`section` are thin command modules forwarding to `render --multi`/`--section`;
  `acceptance` is a declared **alias** of `check`. `mesh`/`printability`/`collision` are
  first-class commands (also reachable as the corresponding `check` selectors).
- `setup` and `libs install` are **removed** (the first-run bootstrap + `3d doctor`'s
  per-item install commands replace them); `libs path` / `libs list` stay (info). `doctor`
  stays (read-only). `3d test` runs the test gate.
- `web` starts the local dashboard (FastAPI + SSE + three.js SPA) — one **thin frontend**
  over the same `lib/` core (architecture §10). `commands/web.py` is the registry command
  (stdlib-only at top level; lazy-imports the optional web tier and raises a structured
  `MissingDependency` if fastapi/uvicorn are absent); the app lives in `lib/web/`. Config is
  `~/.config/3d-cli/web.json` — the same dir as the bootstrap marker. See `docs/commands/web.md`.

## Verification before "done"

Run the full list, not a subset:
- `3d render f --view left`, `--view 3-4`, `--multi`, `--section --plane YZ --color`
  produce correct PNGs on an **asymmetric** model (cube is square in XY — left/front look
  identical on a cube; verify direction on `-D depth=40` or an L-shaped fixture). A section
  PNG must visibly show the **cavity** — "a PNG exists" is not proof it cut.
- `3d check examples/cube.scad` runs all gates by default; `--mesh` alone runs only mesh.
- First-run bootstrap: `rm ~/.config/3d-cli/.bootstrapped` then any `3d` cmd re-bootstraps.
- `3d test` green (ruff + pytest + mypy); aliases still work; the smoke harness covers every
  command's `--help`.
- Everything committed atomically, codex-reviewed, and PUSHED to origin.
