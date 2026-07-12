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
| `--backend NAME` | `ai.json`, else first available | AI critic backend: `claude` \| `codex` \| `opencode` \| `ollama` \| `mock` |
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
3d match model.scad ref.jpg --rounds 8 --backend codex
3d match model.scad ref.jpg --rounds 8 --ortho --cam 130,-600,52,130,0,52
```

## Critic backends

The LLM critic is **backend-agnostic** (`lib/ai/`). `--backend` (or the `backend` field in
`~/.config/3d-cli/ai.json`) selects one of `claude` / `codex` / `opencode` / `ollama` /
`mock`. With no selection the loop auto-picks the first available in that order
(claude-first — there is deliberately no hard `codex` dependency). Codex remains
bit-for-bit identical when selected: prompt on stdin, the overlay attached with `-i`,
same output parsing.

**Vision capability.** `codex` and `ollama` (with a vision model) receive the `overlay.png`
render/reference composite; `claude -p` and `opencode` are text-only and the loop prints a
`CRITIC: WARNING …` line noting the overlay was NOT sent (the critique falls back to the
metrics + changelog). A model-less `ollama` (no `model` in `ai.json`) is treated as
unavailable so auto-pick skips it instead of no-op'ing every round.

**Mock / offline.** `--backend mock`, or setting `$THREED_AI_MOCK_RESPONSE`, uses a
deterministic offline stub (never a network call). The env var **overrides** a configured
`backend`, so a stray `ai.json` can never make the test-suite hit a real model. A present
but malformed `ai.json` fails closed with a structured error rather than silently
auto-picking.

## Implementation notes

`lib/match_loop.py` shells back out to `bin/3d` for `render` / `score` / `mesh`, so it needs no heavy deps itself. It runs via `pyrun` with no deps so the same `.venv` / `uv` / system resolution applies uniformly. The critic call goes through `lib/ai/backends.py` (`resolve_backend`), which shells out to each backend's CLI (or Ollama's stdlib HTTP endpoint).
