# `3d web` — local interactive dashboard

Start a local browser dashboard (FastAPI + Server-Sent-Events + a three.js SPA) for your
3D-modeling projects and for watching AI agents work live. All Python, async; the heavy
deps (fastapi/uvicorn) are the optional **web tier** — the core geometry/render/check
pipeline does not need them.

## Usage

```
3d web [options]
```

| Option | Default | What |
|---|---|---|
| `--root DIR` | config, else cwd | project root to scan |
| `--port N` | `8733` or config | listen port |
| `--host H` | `127.0.0.1` or config | bind host |
| `--config PATH` | `~/.config/3d/web.json` | use a specific config file |
| `--open` | off | open the dashboard in your browser once it starts |

```bash
3d web --root ~/models --open            # scan that root, open the dashboard
3d web --port 9000                       # override the default 8733
```

## Config

`~/.config/3d/web.json` — the **same** config dir the rest of the CLI uses for its
first-run bootstrap marker (`~/.config/3d/.bootstrapped`). One config dir for the whole
tool. Created with defaults on first run; holds `project_root`, `port`, `host`. Per-project
render/state caches live under `~/.config/3d/web-state/`.

## What the dashboard does

- **Projects browser** — scans the root for projects (dirs with a `*.scad` / `SPEC.md` /
  `3d.yaml`).
- **3D viewer** — exports the model to STL (reusing `lib/render.py` / OpenSCAD) and loads
  it in three.js (orbit/zoom/pan); toggle axes / bounding-box / wireframe and thumbnail
  analytical-layer PNGs from `previews/` `match/` `verify/`. Pick two overlays to compare
  with an A/B wipe slider.
- **Constants editor** — Figma-style scrubber inputs for `constants.scad` parameters
  (click-drag / wheel / arrows; **Shift** = fine step, **Alt** = coarse step), live
  debounced re-render over SSE, write-back on **Apply**.
- **Animations / colors / spec** — play generated mp4/gif, set per-part colors (read/write
  `3d.yaml`), render `SPEC.md` to HTML.
- **Agent activity** — a live SSE feed of AI agents working, via extensible `LogAdapter`s:
  **Claude** (`~/.claude/projects/**/*.jsonl` + subagent `*.output`) and **Codex**
  (`~/.codex/sessions/**/rollout-*.jsonl`) are fully parsed; **opencode** is best-effort
  (its `storage/` JSON tree); a **raw** tail is the universal fallback. Sessions
  auto-associate with projects by the paths/cwd they reference; inactive sessions are
  detected so a newer one can take over.

## Dependencies (optional web tier)

`fastapi`, `uvicorn` are required to serve; `markdown` (spec→HTML) and `pyyaml` (per-part
colors) degrade gracefully if absent. Resolved per-call by `uv`, or install for an offline
`.venv` path:

```bash
pip install fastapi uvicorn markdown pyyaml
```

If they're missing, `3d web` exits with a structured error naming the install command and
noting that only `3d web` is unavailable. `3d doctor` lists these under the **Web
dashboard** section (optional — a missing dep is a warning, not a failure).

## Architecture

Per the headless-core / thin-frontends design (architecture spec §10, ROADMAP §20), the
web app is one frontend over the same `lib/` core the CLI dispatches to:

- `lib/commands/web.py` — the thin registry command: parses flags, lazy-imports the web
  tier, launches uvicorn. Stdlib-only at module top level (registry discovery imports every
  command on every `3d` invocation).
- `lib/web/` — the app: `server.py` (FastAPI routes + SSE), `webconfig.py` (config),
  `render_service.py` (async OpenSCAD export, reusing `render.py`), `scan.py`,
  `constants_io.py`, `agent_manager.py`, `adapters/{base,claude,codex,opencode,raw}.py`,
  and `static/` (the SPA).
