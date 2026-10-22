# Why one spoon on a K1 SE took over an hour (2026-10-22)

Evidence: the timestamped Main and `SpoonK1Slice` transcripts of the omp session that started at
17:29:33 UTC, the `orchestrator-stays-thin` hook config, the project 3MF and the sub-agent's slice
output. Clock = minutes after the first message. `[INFERENCE]` marks what no log proves.
Fix tracking: #471 (`3d retarget`).

## Timeline

| min | what happened |
|---|---|
| 0.0–0.8 | Main reads the 3MF (2 calls), then the `orchestrator-stays-thin` hook blocks a 12-segment `unzip -p … \| …` chain; Main delegates. Brief: "produce G-code **from `spoon_handle.stl`**; the earlier 3MF *shows hints*; orient, render, verify". |
| 0.8–9.4 | Sub re-derives instead of reusing: reads unrelated projects, builds a venv for trimesh (the `3d-cli` `.venv` lacks it), 8,000-pose SO(3) orientation search (48 s) → a **new** pose 132 mm tall, hits `fusermount` (AppImage) and the `rig` worktree guard, writes its own 160-line slice script (re-implements preset flattening). Slice #1 starts at 9.1. |
| 0.8–30.2 | Main sits in `wait` for 29 min: the user's first sight of progress. |
| 30.2 | User: "everything was already sliced, only the supports needed a tweak". Main relays; slice #1 (~21 min of compute) is cancelled unfinished. |
| 30–34 | Sub reads the 3MF properly: its orientation (as authored) is 220.6 x 210.2 x 68.6 mm on a 220 mm bed; a minimal tilt (11°, 209 x 209 x 117 mm) fixes it. Slice #2 starts at 34.1. |
| 34–74.5 | Slice #2 runs ~40 min (output files at 18:44 UTC): 1,465 layers over 117 mm, 62 MB G-code, support 854 m of extrusion vs 305 m of outer wall, est. print 8 h 8 min. |

Where the 74.5 min went: slicing ~61 min (**82 %**, one ~21-min run thrown away); re-derivation,
tooling friction and Main's own work ~13.5 min (18 %).

## Causes, ranked

1. **The slicer run dominated, and the first one was the wrong job.** 1,465 layers (≈0.08 mm, like the
   U1 project's `layer_height`) of a 117 mm tall tilt, with supports as long as the part itself. Slow on a loaded box (load average 13–20, `nice -n 10`,
   `CPUQuota`) `[INFERENCE]`. Nothing estimated cost before launching, nothing flagged "20 min, one part".
2. **The premise "just retune supports" was half wrong and nobody could see it in < 1 s.** The U1 project
   places a 299 mm spoon diagonally on a 270 mm bed; on the 220 mm K1 SE it needs a small tilt. No command
   answers "does this 3MF fit bed B?" — `3d retarget` (added in #471) does in 0.5 s (`does not fit … 215.5 x 215.3 mm
   at the closest Z rotation vs usable 210 x 210 mm`; 220.6 x 210.2 mm is the footprint as authored, 215.5 x 215.3 mm
   the best Z rotation, usable = bed minus a 5 mm margin per side; it fits with `--margin 2.2` or less).
3. **The brief told the sub to start from the mesh.** The 3MF — orientation, supports, walls — was demoted
   to "hints"; the first 30 min re-optimised an orientation the user had already chosen. Only the user's
   complaint turned it into "minimum change to the existing 3MF".
4. **No tool moves a project 3MF between printers**, and the installed Snapmaker Orca CLI cannot slice a
   *project* 3MF (exit 127 in 2 s, source and retargeted file alike; `slice-check.md` documents it), so
   the 3MF was abandoned for an STL + hand-flattened presets recipe.
5. **Hook + process ceremony.** `orchestrator-stays-thin` cost one blocked call (~25 s) but pushed a
   10-call read-only inspection into a sub-agent with no memory of the conversation, and made Main idle in
   `wait` while the user could not steer. The brief also demanded renders (viewer broken), a worktree,
   a commit and bounds checks for a one-part print: ~3 min of render scripts plus guard friction.

## Fixes

Tooling (added in #471): `3d retarget PROJECT.3mf --to k1se --keep 1 --preset easy-release-supports
[--process …] [--dry-run]` — real K1 SE preset through `inherits`, one extruder, kept object re-centred
(rotated on Z if needed) with a margin, validated, diff of changed keys, 1–3 s; fails with the numbers
when the part cannot fit. Follow-ups: minimal X/Y tilt seeded from the existing orientation (what the sub
did by hand in 4 min); a project-3MF slice path that works with the Snapmaker CLI (STL + flattened
presets from `orca_presets`); a layers × support-length estimate printed before any slice > 5 min.

Wording: the rules are in `AGENTS.md` § "A ready project 3MF exists: patch it, do not re-slice" (patch don't
re-slice, fast path for a single-part reprint, 0.20 mm slice policy). Copy them into the garage-band
skills too, and add to the delegation-brief template an **Existing artifacts** block (paths + "keep X,
change only Y") and a **time box** ("report after 10 min or any slice > 5 min").

Process: let `orchestrator-stays-thin` pass read-only inspection chains (`unzip -p`, `jq`, `head`), and make
Main post a status/ETA after any sub-agent runs > 10 min instead of an unbroken `wait`.
