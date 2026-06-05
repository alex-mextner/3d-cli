# `3d match` — [forced-monotonic](GLOSSARY.md#forced-monotonic-loop) silhouette-match loop

Iteratively adjusts an `.scad` model’s numeric parameters to improve [silhouette](GLOSSARY.md#silhouette) match against a reference image. An LLM critic proposes one delta per round; the change is accepted **only if** the [IoU](GLOSSARY.md#iou) / [AE](GLOSSARY.md#ae) score strictly improves **and** the [manifold](GLOSSARY.md#manifold) gate passes. Otherwise it reverts. Every step is logged to a changelog.

**Why it exists.** Manual tuning of constants to match a reference photo is tedious. The loop automates the propose-evaluate-revert cycle with a strict monotonicity guarantee, so the model never regresses.

## Usage

```
3d match <assembly.scad> <reference> [options]
```

| Option | Default | What |
|---|---|---|
| `--rounds N` | `8` | Max rounds |
| `--dry-run` | off | Skip the critic; synthesise deterministic edits (smoke test) |
| `--constants FILE` | the assembly | File holding the tunable constants |
| `--params a,b,c` | — | Restrict which constants the critic may tune |
| `--metric iou\|ae` | `iou` | Primary metric |
| `--no-improve N` | `4` | Stop after N consecutive non-improving rounds |
| `--margin F` | `1e-4` | Strict-improvement margin |
| `--cam ex,..,cz` | — | 6-param vector camera for renders |
| `--size WxH` | `1200x900` | Render size |
| `--ortho` | off | Orthographic renders |
| `--work DIR` | `<assembly_dir>/match_work` | Work directory |

```bash
3d match model.scad ref.jpg --rounds 2 --dry-run
3d match model.scad ref.jpg --rounds 8 --ortho --cam 130,-600,52,130,0,52
```

## Implementation notes

`lib/match_loop.py` shells back out to `bin/3d` for `render` / `score` / `mesh`, so it needs no heavy deps itself. It runs via `pyrun` with no deps so the same `.venv` / `uv` / system resolution applies uniformly.
