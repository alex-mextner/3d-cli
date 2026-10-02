---
name: blender-reference-reconstruction
description: Watertight, reference-locked Blender reconstruction from front/three-quarter/side images with monotonic visual fitting, topology gates, and regular USDZ checkpoints.
when_to_use: A user supplies one or more reference renders/images and requires close visual matching, especially after rejected plausible-but-wrong or hole-ridden models.
---

# Blender Reference Reconstruction

The reference is the contract. Do not optimize plausibility; optimize measured agreement while preserving watertight topology.

## Non-negotiable geometry rules

1. Keep one closed MASTER mesh per logical solid part. Use material slots for color regions; do not split one solid into color-piece meshes.
2. Never delete a color submesh to simplify appearance. If a shell is redundant, rebuild/merge it while preserving a closed logical part.
3. Preserve vertex connectivity during fitting. Prefer transforms/deformations over topology edits after a part passes QA.
4. Every accepted geometry checkpoint must pass: boundary_edges=0, components=1, finite coordinates, positive volume, no degenerate faces, no inconsistent winding, no suspicious non-adjacent overlaps.
5. Failed topology or degraded required views means revert to the last accepted `.blend`.

## Reference contract

- Front view locks X/Z silhouette and facial landmarks.
- Side view locks Y/Z depth and head/ear profile.
- Three-quarter view resolves ambiguity and must not be sacrificed to improve only front or side.
- Camera directions and handedness must be sanity-checked with visible face landmarks before any metric is trusted.
- Camera framing may be fitted first, then frozen; do not hide geometry errors with later camera-scale tricks.
## Fitting loop

1. Preserve the last accepted baseline and render evidence.
2. Render flat validation passes for front, three-quarter, and side with locked cameras.
3. Measure global silhouette plus local regions: head, ears, eyes, muzzle, torso, paws, tail.
4. Change one failure class at a time. Prefer parameterized Blender scripts over hand-edited opaque state.
5. Accept only when the target metric improves and no required view crosses its regression budget.
6. Run topology QA immediately after acceptance; topology failure invalidates the visual gain.
7. Save a versioned `.blend`, renders, overlay/metrics, and change note.

## Eyes, ears, and face

- Eye shape is judged from the visible socket/eyelid silhouette, not iris geometry alone.
- Iris/pupil surfaces may be separate closed display meshes, but the skull/eye socket remains one closed head MASTER.
- Ear root must penetrate/seat inside the head volume without leaving open shell boundaries; verify side view and root topology.
- Expression changes come from eyelid/snout/mouth geometry together; never tune pupils to fake expression.

## Export/checkpoint policy

- Produce a diagnostic USDZ after each accepted major milestone so the user can inspect it early.
- Final USDZ is blocked until all required view evidence and topology gates pass.
- Validate USDZ by ZIP CRC plus native Blender USD import round-trip topology QA.
- Share checkpoints through the configured delivery path; if using Tailscale/Funnel, verify the public URL with an actual HTTP download and compare SHA-256 with the source before sending the link.
- Never claim 99.99% without an explicit metric whose measured value actually reaches it. Report per-view/local metrics instead.

## Local project helpers

Use the repository's `scripts/blender/qa_topology.py`, `guarded_export.py`, locked render scripts, and project-specific fit scripts. Keep reusable methods here; keep asset-specific constants in the project folder.
## Anti-overfitting acceptance rules
- A registered/height-normalized silhouette IoU is diagnostic only; it may never override visual quality or fixed-frame agreement.
- Never apply a global vertical-band deformation field to the whole character merely to raise IoU; it can make the body skinny/tall while scoring better.
- Preserve the last user-approved visual checkpoint and regularize edits against it. If a metric improves but recognizability/proportions degrade, reject the edit.
- Geometry changes must be local to one semantic region (head, ears, eyes, muzzle, torso, paws, tail) unless a reference proves a whole-body proportion error.
- Acceptance requires: fixed-camera overlay improvement in the edited region, no visible regression in adjacent regions/views, topology gate pass, and no feature/landmark regression.
- Camera fitting is a preflight step. Once geometry fitting starts, do not move cameras to make later geometry look better.

- Blender coordinate rule: never apply a world-space center directly to raw `vertex.co`; either transform vertex to world space and back through `matrix_world.inverted()`, or operate fully in object-local space. Coordinate-space mistakes can displace facial features while topology still passes.

## Reference integrity gate
- Verify every local reference crop against the original supplied source before geometry fitting; record size/hash and inspect it visually.
- Never derive masks from a corrupt/truncated JPEG. A mask/source mismatch invalidates all pixel metrics from that view.
- Before fitting, render the mask contour directly over the source crop and inspect all three views.
- `registered` overlays that crop/resize/recenter the render are diagnostic only. Acceptance uses a strict fixed-frame overlay with no post-render geometric transform.
- Test ORTHO vs PERSP during camera preflight; choose/freeze the projection that best matches the reference before geometry edits.