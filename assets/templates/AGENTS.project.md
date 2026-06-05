# {{PROJECT_NAME}} — agent guide

This is a `3d` project — parametric OpenSCAD modeling driven by the `3d` CLI. You design
parts as `.scad` files under `parts/`, validate them visually, match them to references, gate
them, and export printable STLs.

Default printer: **{{PRINTER}}**.  Default material: **{{MATERIAL}}**.

## How this project is set up

- **`3d.yaml`** is the source of truth. Read it first. It holds `project.name`, `project.units`,
  `project.printer`, `project.material`, the build `bed`, an optional `project.reference` image,
  and the `parts:` map. Every `3d` command reads it. Resolve printer/material against
  `printers.yaml` / `materials.yaml` if present.
- **`parts/`** — the `.scad` models.  **`references/`** — reference photos to match against.
  **`previews/`** — rendered PNGs (git-ignored).
- **Skills** (`.claude/skills/`):
  - `openscad` — the iteration workflow (write → validate → `3d render --multi` → fit-camera/score
    → `3d check` → `3d export`). Use it for every modeling task.
  - `fdm-printability` — design-for-printability checklist (walls / overhangs / clearances /
    strength). Run it before exporting any STL.
- **OpenSCAD MCP** (`.mcp.json`) — the `openscad` MCP server for quick one-off renders. Prefer the
  `3d` CLI for the real iteration loop (it carries the cameras, gates, and reference pipeline).
- **Pre-commit hook** (`hooks/`) — runs `3d check` on every staged `.scad` and blocks the commit
  on FAIL. Bypass only with `git commit --no-verify`.

## Modeling lessons (read before you build)

1. **Always work against the reference — this is MANDATORY, not optional.** If the project has
   reference image(s) (`project.reference` in `3d.yaml`, or files under `references/`), you MUST
   compare your model to EVERY reference on every iteration. Use `3d render --multi <outdir> --render`
   to see all sides, `3d fit-camera <model.scad> <ref.jpg>` to overlay your silhouette on each photo
   (writes an overlay PNG + an IoU), and `3d score <model.scad> <ref.jpg>`. Never declare a model
   "done" without having compared it to each reference image and looked at the overlays.
2. **Optimize the GEOMETRY, not the camera.** `fit-camera`'s IoU can be inflated by a degenerate
   zoomed-in camera — do NOT chase that single number. Judge with the multi-view renders and the
   overlay images. A model that matches one 2D silhouette can be badly wrong in 3D (e.g. a squat
   drum, a conical "wedding-cake" dome). Check the model from ALL angles, not one.
3. **Get proportions first, detail second.** Lock the gross massing (relative sizes of the major
   volumes) before adding ornament. A plain model with correct proportions reads far better than a
   detailed one with wrong proportions.
4. **Use colors.** Apply `color()` to parts by material/role — a single grey blob is hard to read and
   review, and color carries into preview/export/photoreal. Pull colors/materials from `3d.yaml`.
5. **Detail to the right level.** Set `$fn` high enough that round parts are smooth (no faceted
   cylinders, no stepped cone where a smooth dome belongs); add fillets/chamfers and the features the
   reference actually shows (fluting, capitals, coffers) as far as time allows. BOSL2 is available
   (`include <BOSL2/std.scad>`) for fillets/threads/gears.
6. **Keep it manifold and printable.** Run `3d check` — overlap parts that should be joined
   (interpenetrate then `union()`) so the mesh is watertight; fix any non-manifold before exporting.
   Run the `fdm-printability` skill's checklist (walls / overhangs / clearances) before an STL.
   Build assemblies manifold from the start: never let pieces merely TOUCH face-to-face (ring-on-ring,
   column base-on-shaft, podium-on-drum) — that is non-manifold; make them interpenetrate, then `union()`.
   For an exterior dome, default to a SHALLOW spherical cap, not a hemisphere, and derive its base radius
   to sit flush on whatever it springs from (no overhanging bulge).
7. **Iterate until the tool-measured similarity to the reference is >90%** (e.g. segmented-silhouette IoU /
   score), not until it merely "looks ok". Don't stop early.
8. **If a tool's number looks wrong, the TOOL is being misapplied — fix HOW you use it, don't trust the
   number.** Signs: similarity <50% while the render clearly resembles the photo (the reference wasn't
   segmented — sky/ground/adjacent buildings pollute the mask), or a very high IoU from a degenerate
   zoomed-in fit-camera. Fixes: segment the reference first (`3d preprocess <ref>` -> subject mask) and
   compare against the MASK; reject a fit-camera solution that zoomed onto a fragment; cross-check with the
   multi-view render and the render|diff|reference collage. A comparison you can't trust visually is telling
   you the tool is misconfigured, not that the model is good.
