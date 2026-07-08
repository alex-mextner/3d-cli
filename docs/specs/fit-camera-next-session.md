# fit-camera: Continuation Plan — Next Session

Date: 2026-06-08

This spec captures the current state of the `fit-camera` workstream and the
plan for the next session. It is intended to allow a clean session start without
pulling context from a long conversation.

---

## Current state (as of 2026-06-08)

### What landed in `main`

| Commit | What | Status |
|--------|------|--------|
| `9af84c4` | Multi-attempt GrabCut (3 border configs) + degenerate rejection DURING search | In main |
| `ffa552f` | `--el-range` flag (default -45,85) + `ssim_masks()` reporting | In main |
| `014dd80` | Fail-closed proof status, `fit_status`, `diagnostic_only`, spatial-report, contour-first status | In main |
| `8efe24f` | Quality gate workflow documentation | In main |
| `6f878ad` | Mark old proof branch as superseded | In main |

`lib/fit_camera.py` is now ~750 lines. Key additions from other agents:
- `--objective contour` — uses boundary SDF/F1/Chamfer loss instead of area IoU
- `--spatial-report DIR` — writes `spatial_metrics.json`, `edge_overlay.png`, `proof_panel.png`
- `--mask-polarity dark|light` — handles white-subject masks from preprocess tiers
- `--backplate FILE` — shows original photo alongside masks in proof panels
- `lib/spatial_fit_metrics.py` — contour metrics (SDF, Chamfer, F1, Hausdorff p95)
- `lib/fit_camera_math.py` — extracted math helpers

### Branches NOT yet merged (each has a reason)

| Branch | What | Why not merged |
|--------|------|----------------|
| `roadmap/spatial-fit-experiments` | Experimental harness, view-bank retrieval, synthetic oracle | Needs rebase onto main; experimental only |
| `roadmap/e2e-fit-camera-readable` | Human-readable e2e workflow | One existing warning, needs rebase |
| `roadmap/fit-camera-proof` | Superseded — `014dd80` landed the safe parts | Do NOT merge; cherry-pick only if needed |
| `roadmap/mac-image3d-providers` | Research doc on Apple Silicon image-to-3D | Needs reconcile with 3d auth hf plan |
| `roadmap/review-discipline` | Process docs | Partially stale, uncommitted dirty diff |

### What the research says (key findings)

1. **Area IoU is not enough** — can be gamed by slivers, frame-fill, scale mismatch
2. **SSIM is a poor primary metric** — different lighting/texture in render vs real photo; global SSIM dominated by background area
3. **Boundary metrics are the right signal**: edge F1@r, symmetric Chamfer, SDF loss, Hausdorff p95
4. **Synthetic oracle works**: view-bank top-1 retrieval found hidden camera; local monotonicity held for asymmetric models
5. **Real Pantheon still fails**: `area_iou=0.24`, `edge_chamfer_px=25.4`, `boundary_sdf_loss_px=24.4` — correctly labelled warning/failure
6. **View-bank seeding is needed** before local contour refinement

---

## Acceptance criteria for "fit-camera done"

A result is accepted as **success** only when ALL of the following hold:

1. `fit_status=ok` in output JSON
2. `edge_f1 >= 0.70` at 4px tolerance
3. `edge_chamfer_px < 10.0`
4. `hausdorff_p95_px < 25.0`
5. `coverage_ratio` between 0.8 and 1.25
6. `touches_border = false` (unless reference also touches same border)
7. Visual proof panel shows original reference + fitted render + boundary overlay in the same frame — human inspection agrees they align
8. Telegram report sent with: original reference photo, same-frame fitted render, overlay, and spatial_metrics.json summary

A result is **diagnostic** if boundary metrics suggest plausibility but visual panel is unconvincing or real-photo case.

A result is **failure** if any metric is outside bounds or visual inspection fails.

---

## Next session plan

### Priority 1 — Merge what's ready (low risk, high value)

1. Rebase `roadmap/e2e-fit-camera-readable` onto main, resolve the existing warning, merge
2. Rebase `roadmap/mac-image3d-providers` research doc, merge after review

### Priority 2 — Integrate spatial experiments

3. Create local worktree from `origin/roadmap/spatial-fit-experiments`:
   ```bash
   3d worktree create roadmap/spatial-integration --base main
   ```
4. Cherry-pick the metrics/reporting pieces that work (NOT the experimental search modes)
5. Add view-bank seeding as a new `--seed-from-viewbank` flag (optional, off by default)
6. Ensure the full proof pipeline emits the 6-artifact package
7. Run synthetic oracle: must get top-1 view-bank hit AND visible overlay alignment

### Priority 3 — Real-photo negative controls

8. Run Pantheon front/oblique with `--objective contour --spatial-report`:
   - Must emit failure/warning, not success
   - Report to Telegram with full proof package
9. Add e2e test that asserts Pantheon front returns `fit_status=failure`

### Priority 4 — First real-photo success case

10. Find a simpler reference (mechanical part, cube, simple model) where the fit
    can actually succeed and produce a visual proof panel
11. Once that succeeds: run e2e test that asserts success, send Telegram proof

---

## Key file paths for quick orientation

```
lib/fit_camera.py               — main fitter (~750 lines)
lib/fit_camera_math.py          — cam math helpers
lib/spatial_fit_metrics.py      — boundary metrics (SDF, Chamfer, F1, Hausdorff)
lib/refmatch.py                 — compare pipeline (segment → fit → score → collage)
lib/preprocess_reference.py     — tiered segmentation (SAM2 > rembg > GrabCut)
lib/proxy_align.py              — image-to-3D proxy alignment
lib/commands/fit_camera.py      — thin CLI wrapper (flags: --el-range, --objective, etc.)
lib/commands/compare.py         — thin CLI wrapper for compare pipeline
docs/research/fit-camera.md     — experiment log (hypotheses, results, decisions)
docs/notes/spatial-fit-camera-experiments.md  — experiment plan + metrics table
docs/notes/active-worktrees-2026-06-06.md     — worktree status snapshot
```

---

## Do NOT do in next session

- Do NOT claim success on Pantheon front until visual panel agrees
- Do NOT use SSIM as primary metric (it's a secondary diagnostic only)
- Do NOT merge `roadmap/fit-camera-proof` wholesale — it's superseded
- Do NOT start new worktrees for fit-camera without first checking the above
  branches for useful existing work
- Do NOT skip multi-model review (`review -m codex -m gemini ...`) before commits

---

## Test gate (must pass before any merge)

```bash
dev run test   # ruff + pytest + mypy — all must pass, zero warnings
```

Current: 250+ tests, 0 skipped on the worktrees this session.
Post-spatial-integration: expect 2800+ tests.
