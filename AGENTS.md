# AGENTS.md — `3d-cli`

Instructions for AI agents (and humans) working in this repository. English only.

> **Portable dev rules live in the global agent-tools skills:**
> github.com/alex-mextner/agent-tools (`skills/universal/` + `skills/by-type/cli`).
> They cover the stack-agnostic discipline this file used to spell out at length — atomic
> commits, push-regularly, AI review before commit, dead-code investigation, visual-proof
> cycle, GAN critic loop, and the CLI-shaped skills `self-registering-commands`,
> `lazy-heavy-imports`, `structured-exit-codes`, `help-docs-sync`, `idempotent-bootstrap`.
> This file keeps only what is **specific to `3d-cli`**: its command-authoring contract,
> the OpenSCAD/render/section toolchain, the proof bar for fit-camera/match work, the exact
> test gate, and the project's command surface. Read both.

## What this is

`3d` is a scriptable, AI-assisted CLI for the whole 3D FDM lifecycle — modeling,
verification, matching, animation, simulation, conversion, slicing, and print monitoring.
The entry point
is `bin/3d` — a **thin typed Python dispatcher** that discovers self-registering command
modules from `lib/commands/<name>.py`. Heavy python tools (render/mesh/collision/…) live
in `lib/*.py` and run through `cli.pyrun`.

## Adding a command (the command-authoring contract)

> The patterns behind this contract are the global skills `cli/self-registering-commands`,
> `cli/lazy-heavy-imports`, `cli/structured-exit-codes`. Below is `3d-cli`'s concrete instance.

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
dev run test              # ruff, pytest (unit + CLI smoke harness), then mypy — all must pass
dev run test -- -k errors  # forward args to pytest
```

Unit tests live in `tests/` (registry/alias resolution, errors formatting, score IoU/AE
math, param parsing, env helpers). User-visible command behavior must also have e2e
coverage that calls `bin/3d`: every new command, flag, alias, shell-facing workflow, and
docs/help behavior needs at least one e2e test. Unit tests are still required for pure
logic. `tests/test_cli_smoke.py` runs `3d <cmd> --help` for EVERY registered command and
the safe commands on `examples/cube.scad` (skipping when a tool is absent). `tests/e2e/`
is part of the normal pytest sweep run by `dev run test`; do not maintain a separate matrix.
`tests/test_imports.py` enforces the stdlib-only rule. `dev run test` also runs ruff over
`lib/ + tests/` and mypy over `bin/3d + lib/ + tests/` — keep both clean.

## Agent worktrees

Agents MUST create new worktrees through the project CLI, not raw `git worktree add`:

```bash
3d worktree create roadmap/my-task --base main
cd ~/.config/superpowers/worktrees/3d-cli/roadmap-my-task
3d worktree doctor .
```

`3d worktree create` runs `uv sync --extra dev` in the new checkout and verifies that
`.venv/bin/ruff`, `.venv/bin/pytest`, and `.venv/bin/mypy` exist. This prevents the common
failure where a fresh worktree has only runtime deps, then pre-commit or `dev run test` fails
because `ruff`/`pytest`/`mypy` are missing. Use `--path DIR` only when the default
`~/.config/superpowers/worktrees/3d-cli/<branch>` location is unsuitable. Use raw git
worktree commands only when repairing `3d worktree` itself.

Roadmap and long-running tasks should be delegated to subagents whenever they can be split
into independent slices. Each subagent must work in its own worktree and must return the
branch/commit, changed files, tests, review status, merge blockers, and next action. If the
available subagent quota is full, do not drop the task or continue as if it were assigned:
record it in the active worktree/status note or roadmap queue with the intended branch
name, scope, dependencies, and launch order, then start it when capacity frees up.

## User proof/reporting requirements

The project owner wants progress reports in Telegram for this workstream. When reporting
`fit-camera`, spatial-awareness, image-to-3D, or proxy-alignment progress, send the report
through a trusted `tg` CLI path, not an arbitrary repo-local executable from `PATH`. Use
`$TG_CLI_PATH` only when it is an absolute, executable path that resolves outside the repo
or worktree and matches the trusted local installation (`~/.files/bin/tg` or its target
under `~/.files/repos/tg-cli/`). Otherwise use `~/.files/bin/tg` in the owner's local
environment. `command -v tg` may be used only to confirm that it resolves to the trusted
path. The recipient comes from the trusted `tg` CLI configuration (`~/.config/tg-cli/.env`,
`TG_CHAT_ID`); do not invent chat IDs in repo docs. Expected text-report shape is
`$TG_CLI --format html "<message>"`, with files/photos passed through the same trusted CLI.
Do not leave the only report in chat. If a trusted `tg` or its recipient config is
unavailable, say so explicitly in chat, include the unsent report text, and record that the
Telegram report is queued rather than silently treating it as delivered. Record queued
reports in the active worktree/status note; if no such note exists, create
`docs/notes/queued-telegram-reports.md` with timestamp, scope, intended recipient, and full
message body.

Every status report for this workstream, including single-worktree `fit-camera`,
spatial-awareness, image-to-3D, and proxy-alignment work, must immediately state: what is
being done now, current status, what is already done, what is unfinished or blocked, what
will happen next, which worktrees/branches are involved, and which verification/review
steps have passed. If work continues after the report, say that work continues and name
the next concrete step.

Do not call a diagnostic image a proof unless it includes the human-inspectable inputs and
outputs. This is the acceptance bar for claimed success reports, not a statement that the
current CLI already emits every artifact or every final schema field. For reference-matching
work, an accepted proof report MUST include:

- the original reference image, not only a mask or derived contour image;
- the model render in the same frame/camera;
- an overlay or error map that makes boundary mismatch visible;
- the reference mask or segmentation panel;
- the relevant JSON/metrics summary: boundary F1, symmetric contour Chamfer or SDF loss,
  p95 miss, coverage/bbox/crop/border diagnostics;
- a plain statement of whether this is success, warning, failure, or diagnostic-only.

When sending visual proof to Telegram, send the original reference and same-frame model
render before or alongside any diagnostic overlays. A Telegram proof report must name the
exact artifact paths and explain what the reviewer should see in the reference, render,
and overlay. Do not write vague claims such as "final PNGs look normal" without naming the
files and the visible evidence.

Instrumental-only panels such as masks, point clouds, proxy silhouettes, hulls, view-bank
heatmaps, or optimizer plots are useful diagnostics, but they are not success proof by
themselves. If the render and reference do not visibly align, say that the algorithm failed
or is still experimental. Do not describe such artifacts as "visually normal" or "proof".

Synthetic hidden-camera tests must not pass the hidden camera to the fitter. The hidden
camera is only for post-fit evaluation. Real-photo tests must be treated as failures until
the original reference, fitted render, and overlay all make sense to a reviewer.

Before `fit-camera`, spatial-awareness, image-to-3D proxy generation, or proxy alignment is
reported as complete, e2e tests must exercise the normal `bin/3d` user workflow and assert
the required proof artifacts, metrics, and explicit result label. If the command does not
yet emit a durable result label, the feature is not complete; report it as planned or
diagnostic work instead.

When reporting active worktrees, do not provide only a list. For each worktree include:
what changed, whether it is committed/pushed, whether it was reviewed, what verification
passed, why it is not merged yet, where the work is blocked or merely awaiting continued
work, and the next action needed. The report must make clear whether to continue, merge,
or delete the worktree.

## Engineering conventions

### Python
- **Typed.** Every Python module starts with `from __future__ import annotations` and has
  full type hints on functions (params + returns). Keep it **mypy-clean**:
  ```bash
  uv run --with mypy mypy lib/*.py
  ```
  Third-party libs without stubs (trimesh, manifold3d, open3d, cv2, scipy, pyvista) are
  handled by `mypy.ini` (`ignore_missing_imports` per module) — never a blanket
  `ignore_errors`, which would fake "clean". The full lint gate is `uv run ruff check
  lib/ tests/ && uv run mypy lib/ tests/` (also run by `dev run test`).
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

### Help text sync
> Generic rule: global skill `cli/help-docs-sync`. Project specifics:

Help text must be in sync between `lib/commands/<name>.py` (the `--help` USAGE string)
and `docs/commands/<name>.md` (the doc fragment). Every flag/option must have a concrete
example in both places. When a command's surface changes, both files must be updated in
the same commit.

### First-run bootstrap
> Generic rule: global skill `cli/idempotent-bootstrap` (idempotent, non-fatal if offline).
> Project specifics:

On ANY `3d` invocation, if `~/.config/3d-cli/.bootstrapped` is absent, the dispatcher
auto-installs the OpenSCAD libraries (BOSL2, NopSCADlib) into the repo `libs/` ONCE,
quietly (one-line notice), then touches the marker. It is **idempotent** and **non-fatal
if offline** (must never block `render`/`help`). `OPENSCADPATH` is auto-exported from
`libs/` by `cli/env.export_openscadpath()` (called in the dispatcher before any
subprocess) so `include <BOSL2/std.scad>` resolves with no manual step.

## Commit discipline (project specifics)

General commit hygiene — atomic commits, AI review before commit, the pre-commit
lint/type/test gate, and pushing regularly — is self-advertised by the agent-tools
skills (`atomic-commits`, `ai-review-before-commit`, `pre-commit-gate`,
`push-regularly`). Only the 3d-cli-specific overrides live here:

- **Direct-to-`main` workflow.** This project works directly on `main` — no
  feature-branch dance; that is where all history lives. After each commit or small
  batch, `git push origin main`. (This overrides the skills' default "push to a
  feature branch, open a PR".)
- **Review model roster + minimum bar.** The pre-commit `review` runner's baseline for
  this repo is `review -m codex -m gemini -m oc:fireworks/accounts/fireworks/routers/kimi-k2p6-turbo`
  (install/update from `https://github.com/alex-mextner/review-cli`). The minimum bar for
  THIS repo is Codex plus at least one independent non-Codex reviewer from a different
  provider/model family — a second Codex run does not count. If no independent non-Codex
  reviewer is available, record the provider-wide blocker; never silently treat
  single-review work as fully reviewed.
- **Co-Authored-By trailer** on commits:
  `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`

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
  stays (read-only). The repo test gate lives in `rig.yaml` and runs through `dev run test`.
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
- `dev run test` green (ruff + pytest + mypy); aliases still work; the smoke harness covers every
  command's `--help`.
- Everything committed atomically, codex-reviewed, and PUSHED to origin.
