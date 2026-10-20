# Prep session ledger — Chekhov cat phone stand, 2026-10-20

Evidence for ticket #431 (`3d prep`). Source: the omp orchestration session
`2026-10-20T15-10-58-995Z_01a159dd-9fb3-7305-9e71-37cb8dc35a65` and its subagent transcripts
(`CatGeometry`, `U1MixPipeline`, `RemoteSetup`, `MixCliOwner`, `PhoneSpecs`). Every number below was
computed from the transcripts' own timestamps (UTC+2 local time, `HH:MM`) or quoted from a tool result;
where a figure is an inference it says so.

## What the user asked, and what existed after 2 h 22 min

17:15 — "find the Chekhov cat phone stand project and print it; mixed colours (pink, green-ish eyes, dark
grey), 4 walls / 15 % infill, adaptive layers 0.08-0.32, avoid supports, check common phones fit and it
stands on its own, send proofs".

At 19:22 the user wrote "2 hours already!!! what did you do, where did the time go?". At 19:37 (2 h 22 min)
state of the real model: repaired and adapted STL plus proof images, **no 3MF, no G-code, no print**. The
colour/3MF/slice pipeline (71 min of a second agent) had only ever run on throwaway test shapes.

## Timeline by phase (wall clock)

Critical path = `CatGeometry` (17:19 → 19:29, 130 min and still re-running at 19:37). `U1MixPipeline`
(17:19 → 18:30, 71 min) ran in parallel and was off the critical path but delivered nothing for the real cat.

| # | Phase | Window | min | What happened |
|---|---|---|---|---|
| 0 | Orchestrator: find project, plan, dispatch | 17:15:07 → 17:19:01 | 3.9 | 3 `orchestrator-stays-thin` hook blocks, a `find / -xdev` that ran 300 s in the background |
| 1 | Env set-up (CatGeometry) | 17:19:05 → 17:20:38 | 1.5 | raw `git worktree add` denied by rig guard; new venv; pymeshlab, matplotlib missing in 3d-cli's venv |
| 2 | Mesh diagnose + repair | 17:20:38 → 17:35:12 | 14.6 | 411 213 faces, 61 debris shells, 15 non-manifold + 3 open edges. `repair.py` run 5×: 99 s, 118 s, a pymeshlab `not 2-manifold` failure, 54 s; Blender renders and matplotlib sections to look at it |
| 3 | Phone-fit solver prototype + dimension research | 17:35:12 → 17:43:42 | 8.5 | rigid-slab rest solver written from scratch (`phonefit.py`, 262 lines) |
| 4 | Cavity fill + overhang analysis + 45° adaptation | 17:43:42 → 18:12:09 | 28.5 | `fill_cavity.py` run 7×, `adapt_overhangs.py` 5×, each 28-56 s (Python ray loop voxelizer, manifold boolean on 411k faces); 6 min lost to non-watertight booleans (`clean()` debugging) |
| 5 | Phone cradle + physical simulation | 18:12:09 → 18:27:39 | 15.5 | `pybullet` built from source → `cc1plus` ate memory, mem-sentinel alert 18:14, process killed by hand; Blender rigid-body sim 3 runs (41-143 s); first `final.stl` at 18:27 |
| 6 | Commit / ticket / review (geometry) | 18:27:39 → 18:29 | 1.5 | commit blocked (no ticket id); asked the orchestrator; background review 290 s |
| 7 | Phone-fit table (28 phones x cases), stability, landmarks, layer heights | 18:28:40 → 18:45:21 | 16.7 | `phone_fit.py` 4× (161 s each), `stability.py` 3×, `lh2.py` 45 s |
| 8 | Proof images A + B | 18:45:21 → 18:53:20 | 8.0 | `make_proofs_a.py` 3× (41-45 s each) |
| 9 | **Nested-flock deadlock** | 18:53:20 → 19:18:50 | 25.5 | `proofs_lib.HEAVY` wraps in `flock /tmp/3d-cli-heavy.lock`; the command was already wrapped in the same `flock`; the inner call waited for its own parent until the 1500 s command timeout |
| 10 | Proofs B + verification re-run | 19:18:50 → 19:22:47 | 4.0 | `verify_final.py` 226 s |
| 11 | Image review, fixes, `run_all.sh`, report | 19:22:47 → 19:27:23 | 4.6 | non-manifold-vertex detector added, report written |
| 12 | User asks about the rectangular cut-out → organic scoop, full re-run | 19:26:34 → 19:37+ | 10+ | whole chain from `adapt_phone.py` re-run (no cache) |
| — | **CatGeometry total** | 17:19:05 → 19:29 | **130** | LLM turn time ≈ 55 min over 207 turns (avg 16 s), tools + waits ≈ 75 min (turn time summed over the transcript, which runs past 19:29) |
| A | U1: discover existing assets | 17:19:04 → 17:26:48 | 7.7 | read babushkin-dom `gen/`, snap-orca sources; headless smoke slice failed (`calc_exclude_triangles`, exit 2) |
| B | U1: write colours / layer profile / test model / 3MF builder | 17:26:48 → 17:36:00 | 9.2 | `colours.py` (10 runs), `layer_profile.py`, `testmodel.py`, `build_3mf.py` (13 runs) |
| C | U1: slice test shapes + G-code verifier | 17:36:00 → 17:58:33 | 22.5 | `verify_gcode.py` written (477 lines) and run 14×; floating-region warning bisected; 36-44 s per slice |
| D | U1: preflight, renders, RECIPE.md | 17:58:33 → 18:02:58 | 4.4 | |
| E | U1: commit gates, 3 staged multi-model reviews, ticket creation | 18:02:58 → 18:29:40 | 26.7 | review-before-commit gate → 583 s review; ticket gate; `gh issue create` blocked; `task new` needed 4 attempts (acceptance, links) → HYP-1751 at 18:16; 3 more reviews (243 s, 220 s, 118 s) |
| F | U1: rework after the user's mix-rule correction | 19:29 → 19:34+ | 5+ | "extra colour must never print on adjacent layers": `colours.py` search and `verify_gcode.py` re-written, full rebuild |
| — | **U1MixPipeline total** | 17:19:04 → 18:30:06 | **71** | LLM turn time ≈ 41 min of it (inference: the 100 min the transcript sums include a 59 min parked gap) |
| R | RemoteSetup (parallel, user-requested) | 19:05:29 → 19:25:08 | 19.7 | provisioned the 16-core box by hand, user-space only |
| W | Waiting on the user (T2 head question) | 17:22:51 → 17:45:40 | 22.8 | non-blocking, agents continued; two `tg` gate rejections first (decision format, file attach) |

Orchestrator itself: 142 min wall, only 9 min of model turns (57 turns); the rest `wait`. Gate rejections
seen in the orchestrator transcript: 3 `orchestrator-stays-thin`, 2 `tg` intent gate, 1 `tg` dictionary gate,
3 hook hatches (`RIG_HATCH_REQUEST_*`).

### Repeated work (counts of identical script invocations)

| Script | Runs | Per-run time | Why re-run |
|---|---|---|---|
| `fill_cavity.py` | 7 | 28-56 s | fixing failures; shape tweaks; no cache between steps |
| `repair.py` | 5 | 12-118 s | tool failures, dilated cut |
| `adapt_overhangs.py` | 5 | 30-37 s | angle / head relief tuning |
| `build_final.py` | 4 | 12-146 s | every upstream edit re-ran everything |
| `phone_fit.py` | 4 | 161 s | notch change, table changes |
| `make_proofs_a/b.py`, `verify_final.py`, `run_all.sh` | 3 each | 41-226 s | proofs regenerated per edit |
| `verify_gcode.py` / `build_3mf.py` / `colours.py` | 14 / 13 / 10 | 27-44 s | test-shape tuning, then the mix-rule correction |

## What was rediscovered or re-written although it already existed

Found by comparing the new scripts with `~/xp/3d-cli` (main at 17:15, branches at 19:29):

| Re-written | Already existed | Evidence |
|---|---|---|
| Colour-recipe search `colours.py` (136 lines) incl. the pattern rules | `slicing/mix_model.py` + `slicing/mix_pattern.py` on `color/mix-*` branches (epic #410, #116, #411) | the branches were found with `git branch -a` at 19:29:29 — 2 h 14 min after the start — only after the user complained the mix was wrong; `grep "Full Spectrum\|U1" README.md` returned "No matches" at 17:17 |
| G-code bounds / head-sequence / overhang / thumbnails gates | `3d gcode-bounds`, `3d gcode-overhangs`, `slicing/head_sequence.py`, `3d print-preflight` | U1 agent ran them at 17:50 as a *second* layer next to its own `verify_gcode.py` |
| Orca CLI wrapper `cli_copy` / `slicing_copy` and the quirk list (plate block segfault, layer-range segfault, `calc_exclude_triangles`) | `projects/babushkin-dom/gen/verify_slices.py` (garage-band, uncommitted), no home in 3d-cli | rediscovered by a failing smoke slice at 17:22:34 (exit 2), fixed after reading babushkin's script |
| Mesh repair (`repair.py`) | `3d mesh` diagnoses only; the repair lived in the user's Snapmaker-Orca fork (MeshFixLib ticket #432) | pymeshlab absent in 3d-cli's venv, 5 runs |
| Stability / tip-over, standing height gate | `3d props` has a height/base stability gate for standing parts | CatGeometry did not look; wrote `stability.py` (117 lines) |
| Heavy lock one-liner | AGENTS.md `flock … systemd-run …` typed by hand 14 times, once nested | the deadlock (phase 9) |

## Ranked bottlenecks

Minutes are wall clock on the path to a sliced, verified plate (critical path first); "avoidable" is what a
one-command flow removes, estimated from the phase windows above.

| Rank | Bottleneck | Minutes | Cause | Fixed by |
|---|---|---|---|---|
| 1 | Geometry operators re-written and re-run by hand: repair, cavity fill, 45° adaptation, overhang re-analysis (phases 2 + 4) | 43.1 | no reusable repair/adaptation library; Python-loop voxelizer 25-50 s per call; no step cache so each tweak re-ran the chain | `prep.meshfix`, `prep.adapt`, `prep.voxel` (411k faces in 1.1 s), `prep.cache` |
| 2 | Fit / stability / simulation solvers from scratch (phases 3, 5, 7) | ≈ 41 | no table-driven fit check; physics sim tried via `pybullet` source build | `prep.fit`, `prep.stability` |
| 3 | Colour → 3MF → slice → G-code gate pipeline built from scratch (phases A-D) | 43.8 (off critical path, but real cat still not sliced) | no home for the U1 / Orca project builder, layer-height profile, quirks, verifier | `prep.regions`, `prep.layers`, `prep.project3mf`, `slicing.orca_cli`, `prep.gcode_gates` |
| 4 | Commit, review and ticket gates (phases 6, E; tg gates in orchestrator) | ≈ 34 | pre-commit review hatch, ticket requirement, `task new` field rules, tg formats — process, not tooling | not fixable by 3d-cli; `3d prep` cuts the number of throwaway scripts that need committing (27 scripts → 0) |
| 5 | Nested `flock` deadlock (phase 9) | 25.5 (+4 re-run) | wrapper applied twice on one lock file; no timeout until the 1500 s command limit | `3d heavy` re-entrant + bounded wait, `THREED_HEAVY_HELD` |
| 6 | Proof pack assembled by hand (phases 8, 10, 11) | 16.6 (+ the deadlock) | each picture a new Blender/matplotlib script | `prep.raster`, `prep.panels`, `prep.report` |
| 7 | Discovery / rediscovery of existing tooling (phases 0, A; mix rule) | ≈ 17 (+ 2 h 14 min until the mix branches were found) | no README pointer to U1 / Full Spectrum flow; unmerged branches | README Recipes + `docs/workflows/prep.md`; mix lib consumed lazily |
| 8 | Environment (phases 1, 5 build) | ≈ 6 | per-session venv, missing pymeshlab/matplotlib, `pybullet` compile → memory alert | `3d prep` runs in 3d-cli's own env (`uv sync --extra prep`) |
| 9 | Remote box provisioning (phase R, parallel) | 19.7 | hand-rolled | `3d remote` + documented `remote.yaml` |
| 10 | Waiting for a decision by the user (phase W) | 22.8 (non-blocking) | one open question (T2 head) | already non-blocking; fleet fix in PR #434 |

Ranks 1-3 are the 128 minutes of tool-writing that the evidence session spent re-deriving mesh, physics
and slicing steps; ranks 5-6 are another 46 minutes of plumbing. A command that carries those steps with
a cache turns "a new script per question" into "re-run the changed step".

## After: `3d prep` on the same STL

See `docs/workflows/prep.md` for the flow and the measured per-step table on the original
`cat phone holder full.stl` (section "Measured on the Chekhov STL").
