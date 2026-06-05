# `3d doctor` — read-only health / compatibility report

Reports which dependencies are present or missing for the full `3d` pipeline, and prints the exact install command for your OS. Read-only — it never installs anything.

**Why it exists.** Instead of a vague "something is broken" error at runtime, `doctor` gives you a complete inventory up front. It also replaces the old `3d setup` command by printing per-item install commands rather than running them automatically.

## Usage

```
3d doctor
```

```bash
3d doctor
3d doctor | grep MISSING
3d doctor   # run before CI to verify the environment
```

## Checks

- **Core** — [`openscad`](GLOSSARY.md#openscad), `imagemagick (magick)`, `python3`
- **Python runtime** — `uv` (preferred), `pip`, `.venv`
- **Python mesh stack** — [`trimesh`](GLOSSARY.md#trimesh), [`manifold3d`](GLOSSARY.md#manifold3d), `numpy`, `scipy`, `rtree`, `pillow`, [`opencv`](GLOSSARY.md#opencv), `pyvista` (optional, for `collision --viz`)
- **Web dashboard** — `fastapi`, `uvicorn`, `markdown`, `pyyaml` (optional tier)
- **Slicer** — `OrcaSlicer` / `Bambu Studio` / `PrusaSlicer`
- **OpenSCAD libraries** — [`BOSL2`](GLOSSARY.md#bosl2), [`NopSCADlib`](GLOSSARY.md#nopscadlib) (auto-install on first run)

## Exit codes

- `0` — all required dependencies present
- `1` — one or more required items missing (informational, never crashes)
