# `3d recover-blockout` — closed parametric-recovery loop (image → blockout params)

Turns a reference silhouette into tuned parameters of a parametric **blockout** template
(a temple/colonnade family: a column count plus continuous dimensions), as a staged,
honestly-gated loop. This is the generator the image→3D pipeline was missing: `fit-camera`
only fits a camera to a *fixed* model and `match` only nudges an *existing* assembly —
neither can invent structure. `recover-blockout` generates geometry and closes the loop
against boundary metrics **and** a semantic-feature veto.

## The loop

1. **perceive** — a VLM veto (`lib/ai/veto.py`, any `resolve_backend` backend) reads the
   critical discrete feature: the colonnade's **column count**. Discrete counts are what
   boundary optimizers are worst at, so perception pins them and the boundary fit recovers
   the continuous dimensions.
2. **generate** — `lib/ai/blockout.py` emits a temple `.scad` with that column count and
   default continuous dims. Every tunable is a top-level `name = number;` constant.
3. **contour-fit** — a **locked-framing view-bank pose fit** (fixed working distance +
   centroid look-at, coarse azimuth/elevation grid), ported from
   `tools/spatial_fit_experiment.py`'s view-bank retrieval. Locking the framing stops the
   free-pan/scale "cheat" that lets a wrong-sized model fake a match, so the silhouette
   scale stays faithful to the dimensions being recovered.
4. **monotonic-refine** — coordinate descent over the continuous dims. A step is accepted
   **iff** the boundary SDF loss strictly improves **and** the semantic veto still passes
   (an edit that merges or erases columns is reverted). Reuses `match_loop`'s
   strictly-better + changelog discipline.
5. **proof-status gate** — `recovery_status` is `ok` only when `fit_status == ok` **and**
   the veto passes **and** the 6-artifact proof panel is emitted; otherwise `warning` /
   `failed`.

## Usage

```
3d recover-blockout [<reference-image>] [options]
```

| Option | Default | What |
|---|---|---|
| `--synthetic` | off | Run the synthetic parametric-recovery acceptance milestone (hidden params + hidden camera from the same family, recovered without leaking them to the fitter; can report `recovery_status=ok`) |
| `<reference-image>` | — | Real-photo diagnostic mode; `recovery_status` is capped at `diagnostic` and never claims photo→model success |
| `--template NAME` | `temple` | Blockout family |
| `--out DIR` | `./recover_out` | Output directory |
| `--size WxH` | `240x200` | Render size |
| `--backend NAME` | auto | Veto AI backend: `claude` / `codex` / `opencode` / `ollama` / `mock` |

```bash
3d recover-blockout --synthetic --out recover/
3d recover-blockout --synthetic --size 200x160 --out recover/
3d recover-blockout photo.jpg --template temple --backend mock --out recover/
```

## Honesty contract

`--synthetic` is the acceptance milestone. Hidden parameters and a hidden azimuth/
elevation are drawn from the same template family and rendered to a reference; they are
used **only** for post-hoc scoring and are **never** passed to the pose fit or the refine.
The mock veto is configured with the hidden column count, standing in for a VLM reading
the reference. A fixed working distance is a shared rendering convention, not hidden data.

Real-photo input is **diagnostic-only**: `recovery_status` is capped at `diagnostic` and
no output ever claims a photo→model success. Treat every real-image run as a diagnostic
until the original reference, the recovered render, and the contour error map all visibly
align to a human reviewer.

This is a **closed parametric-recovery loop, synthetic-proven** — not photo→model.

## Artifacts (written to `--out`)

- `proof_panel.png` — the 6-artifact panel: reference, reference mask, recovered render,
  contour error map (reference red, model cyan), boundary metrics, and the proof-status
  cell (status + veto verdict).
- `recovered_render.png` — the recovered model rendered in the reference frame. If the
  final render FAILS, this file is not written and `result.json` reports
  `recovered_render: null` with `recovery_status: failed` (the veto fails closed on the
  absent render — never a silent pass against a stale PNG).
- `reference.png`, `reference_mask.png` — the human-inspectable inputs.
- `changelog.md` — one line per accepted refine step (param, old→new, loss, veto verdict).
- `result.json` — `recovered_params`, `pose`, `fit_status`, `veto`, `spatial_metrics`,
  `recovery_status`, and (synthetic mode) `synthetic.hidden_params` + the per-parameter
  recovered-vs-hidden error table and tolerances.

## Dependencies

Needs `openscad` plus `numpy`, `pillow`, and `scipy` (resolved via `pyrun`). The veto
backend is optional in `--synthetic` mode (it uses the deterministic mock); real-image
mode needs an available AI backend or `--backend mock`.
