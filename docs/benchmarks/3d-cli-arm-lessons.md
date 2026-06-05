# 3d-cli benchmark arm — lessons

Analysis of the `3d-cli` arm of the controlled modeling benchmark. The arm was given a
photo-matching task (model the **Pantheon in Rome** from a front photo + an oblique photo)
and the full `3d` CLI, and told to iterate: render → look → fix the biggest mismatch →
drive the silhouette-IoU numbers up → stop after ~6-10 cycles.

Transcript:
`~/.claude/projects/-Users-ultra-xp-3d-cli/bc61f43a-c443-4937-ba2a-3816d648bf66/subagents/workflows/wf_1c39bdc2-6b1/agent-aa12922bb6de23c7d.jsonl`

## What the agent did

1. Read both reference photos, then read `3d --help`, `3d render --help`, `3d fit-camera --help`
   to learn the tooling before writing any geometry.
2. Consulted an advisor on its proportion plan, then wrote a first parametric `.scad`:
   cylindrical rotunda (plinth/wall/cornice), a domed top, and a projecting portico
   (podium, an 8-wide × 3-deep column grid, entablature, triangular pediment), with key
   dimensions parameterized at the top.
3. Iterated ~5 render/inspect cycles using `3d render --multi … --render` to see all six
   standard angles, reading the PNGs each time and fixing the biggest visible mismatch:
   - **Dome reworked from bulbous to shallow.** The first dome read as a near-full
     hemisphere ("looks like a beehive"). The agent rederived it as a true shallow
     spherical cap whose base radius is computed to sit flush on the top stepped ring
     (`R = (a² + f²)/(2f)`, center at `rings_top − (R − f)`), eliminating an overhang
     artifact (an "orange line" — the sphere bulging out past the rings) in the process.
   - Made columns thicker and the entablature taller (real Pantheon columns are massive),
     and tuned pediment height/width so the dome doesn't poke above it in the front view.
4. Ran `3d fit-camera` against both references for quantitative feedback: front IoU 0.706,
   oblique IoU 0.680. Read both overlay PNGs (render-cyan over reference-red) to see where
   the silhouettes diverged.
5. Recognized the front `fit-camera` result as a **degenerate, zoomed-in camera** that
   gamed IoU by framing a fragment of the drum rather than a sensible front view. Decided
   to stop chasing that number and judge by massing + the clean multi-views instead,
   treating IoU only as a tiebreaker.
6. Ran `3d check`: **MANIFOLD FAIL** (mesh-verified non-manifold). Fixed it by making every
   stacked/abutting piece **interpenetrate** instead of touching face-to-face and wrapping
   the assembly in `union()`: stepped rings overlap the ring below, the cap sinks into the
   top ring, columns sink into podium + entablature, pediment sinks into entablature, the
   portico is pushed into the curved drum. Re-ran `3d check`: **MANIFOLD PASS**.
   (Printability stayed FAIL — thin column features / dome overhangs — which is expected and
   irrelevant for an architectural massing model; watertight/manifold was the real gate.)
7. Finalized at iteration ~5 with a recognizable Pantheon: square-profile rotunda, shallow
   stepped dome with oculus, 8-wide Corinthian portico, entablature, triangular pediment.

Tools used: `render --multi --render`, `render --view … --ortho`, `validate`, `fit-camera`,
`check`.

## Friction / problems with the 3d CLI

- **`fit-camera` is gameable and its IoU can be untrustworthy.** The front fit converged
  on a degenerate zoomed-in viewpoint that maximizes silhouette IoU by framing a fragment
  of the model, not a sensible front view. The reported 0.706 said nothing useful about
  likeness; the agent had to recognize this and discount the number. Nothing in the tool
  output flagged the camera as degenerate (extreme zoom / tiny framed bbox / camera very
  close to the geometry) — the agent had to catch it by eye from the fit render.
- **The IoU is computed against an unsegmented reference.** The reference photos contain
  sky, ground, square paving, and adjacent buildings, so the "reference silhouette" the
  IoU is measured against is polluted. A correct model can therefore score a mediocre IoU
  (0.68-0.71 here), and a number that low while the render clearly resembles the photo is a
  signal the *measurement* is wrong, not the model. A `3d preprocess` command that produces
  a subject mask exists but was never surfaced as the natural first step for the reference.
- **Manifold failures are routine for assemblies and the message is terse.** Stacking
  parts so faces merely touch (ring-on-ring, column base-on-shaft, podium-on-drum) produces
  a non-manifold mesh. `3d check` reports `mesh-verified non-manifold` / `not watertight`
  but does not point at *which* joints are coincident-face, so the agent had to reason
  through every abutment and add overlaps manually. The fix ("interpenetrate then
  `union()`") is a learnable pattern that the tooling could hint at.
- **`check` lumps PRINTABILITY in with MANIFOLD and fails the whole gate.** For an
  architectural / display model, printability (walls, overhangs) is irrelevant, but
  `>>> CHECK: FAIL` fires on it anyway, which is noise when the meaningful gate (manifold)
  passes.
- **Slow renders lengthen the loop.** CGAL multi-view renders and `fit-camera` were run
  under 300-400 s timeouts; each render/inspect cycle is expensive, which caps how many
  iterations are practical. (The agent leaned on `--multi` async and `--ortho` single views
  to manage this.)
- **No single render|diff|reference artifact.** To compare, the agent had to read the
  multi-view PNGs, the fit overlay, and re-open the references separately and hold the
  comparison in its head; there is no one collage that puts render, difference, and
  reference side by side.

## Lessons

- **Judge likeness by GEOMETRY and the multi-views; use silhouette IoU only as a
  tiebreaker.** A single optimized-camera IoU can be inflated by a degenerate viewpoint and
  cannot be trusted on its own.
- **A number that disagrees with your eyes means the TOOL is misapplied, not that the model
  is bad.** Low IoU + render that clearly matches the photo ⇒ the reference wasn't
  segmented (sky/ground/neighbors pollute the mask). High IoU + nonsense view ⇒ a degenerate
  zoomed fit. Either way, fix *how the tool is used* before believing its score.
- **Segment the reference before scoring against it** (`3d preprocess <ref>` → subject mask),
  and compare against the mask, not the raw photo.
- **For exterior domes, default to a shallow spherical cap, not a hemisphere.** Derive the
  cap so its base radius is flush with whatever it springs from to avoid overhang artifacts.
- **Build assemblies to be manifold from the start: interpenetrate overlapping pieces, then
  `union()`.** Never let parts merely touch face-to-face — that is non-manifold.
- **Separate "is it watertight" from "is it FDM-printable."** For display models the
  printability FAIL is expected; manifold/watertight is the gate that matters.
- **Learn the tooling before modeling.** Reading `--help` for render/fit-camera up front
  paid off; pick the cheap loop (`--multi` async + `--ortho` singles) to keep iterations
  affordable.

## Proposed CLI UX improvements

1. **`fit-camera` should detect and warn on a degenerate / zoomed solution.** Heuristics:
   the fitted bbox fills the frame edge-to-edge, the camera distance is below a fraction of
   the model bbox diagonal, or the framed model bbox is a small fraction of the full bbox.
   Emit a `WARNING: fit camera looks degenerate (zoomed onto a fragment); treat IoU as
   unreliable, cross-check with --multi` line and optionally clamp the search to a sane
   distance / FOV range.
2. **`fit-camera` / `score` should segment the reference by default** (run `preprocess` /
   the subject-mask pipeline on the reference, or refuse and tell the user to, when the
   reference is clearly an unsegmented photo). Compare silhouette against the subject mask,
   not the raw image, and report the mask coverage so a polluted reference is visible.
3. **Add a `render|diff|reference` collage flag** (e.g. `3d score … --collage out.png` or
   `3d compare`) that writes one image with the locked-camera render, the reference (or its
   mask), and a per-pixel difference side by side — so likeness is judged from a single
   artifact instead of three separate reads.
4. **Make `3d check` separate gates and let callers select them** (e.g.
   `3d check --manifold-only`, or exit-code/section that distinguishes MANIFOLD FAIL from
   PRINTABILITY FAIL) so a watertight display model isn't reported as a hard FAIL purely on
   printability.
5. **Point manifold failures at the offending joints.** When `mesh` finds non-manifold
   edges, report their coordinates / nearest part bbox and hint the fix ("coincident faces
   between adjacent solids — interpenetrate and `union()`").
6. **Clearer multi-view defaults for likeness work.** Default `--multi` (or a dedicated
   `3d massing` view) to emit the orthographic front + a 3-4 oblique that mirror the usual
   reference angles, so the agent compares like-for-like without hand-picking cameras.
