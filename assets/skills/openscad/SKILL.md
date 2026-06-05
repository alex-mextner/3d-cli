---
name: openscad
description: Create, iterate, validate, preview, and export OpenSCAD 3D models with the `3d` CLI. Multi-angle visual validation, reference matching (fit-camera / score), parameter extraction, sections, and gated STL export. Use when designing or refining any 3D-printable model in a `3d` project.
allowed-tools:
  - Bash(./bin/3d *)
  - Bash(3d *)
  - Read
  - Write
  - Glob
---

# OpenSCAD Skill (driven by the `3d` CLI)

Design 3D models iteratively, with **visual validation at every step**. This skill drives the
`3d` CLI — there are no `.sh` scripts. Versioning is done by **git**, not filename indices
(`chair_001.scad`): keep one `parts/chair.scad`, commit each iteration.

## The `3d` commands you use here

| Command | Purpose |
|---|---|
| `3d validate <file.scad>` | Fast parse-only syntax check (no geometry render) |
| `3d preview <file.scad> [-o out.png]` | Fast throwntogether preview PNG (no CGAL) |
| `3d render <file.scad> --view <name>` | Single CGAL render of one view (iso/front/back/left/right/top/...) |
| `3d render --multi <outdir> --render` | Render all sides (front/back/left/right/top/iso) into `<outdir>` |
| `3d render <file.scad> --section` | True cross-section (STL-cut; `--color` for assembly mode) |
| `3d params <file.scad> [--json]` | Extract Customizer-style parameters |
| `3d export <file.scad> -o out.stl [-D k=v]` | Export STL/3MF **with** manifold / self-intersect validation |
| `3d check <file.scad>` | Master QA gate — runs every applicable check (manifold, fit, printability, ...) |
| `3d fit-camera <model.scad> <ref.jpg>` | Fit a camera to a reference photo, overlay your silhouette, report IoU |
| `3d score <model.scad> <ref.jpg>` | Silhouette AE + IoU against a reference (machine-parseable KEY=VALUE) |
| `3d printability <file.scad>` | Wall / feature / overhang / orientation flags (FDM) |

(The `openscad` MCP server is also wired in — see `.mcp.json`. Use MCP tools for quick one-off
renders; use the `3d` CLI for the iteration workflow below, because it carries the project's
gates, cameras, and reference-match pipeline.)

## Workflow — iterate with visual validation

### 1. Write the .scad file

Parametric, with Customizer comment hints so `3d params` can read them:

```openscad
include <BOSL2/std.scad>   // BOSL2 is available: fillets, threads, gears, rounding

width  = 50;   // [20:100] Width in mm
height = 30;   // [10:80]  Height in mm
$fn    = 96;   // smooth round parts — no faceted cylinders
```

### 2. Validate syntax (fast, no render)

```bash
3d validate parts/chair.scad
```

### 3. Render all sides and READ every PNG

```bash
3d render --multi previews/ --render            # or: 3d render parts/chair.scad --multi previews/ --render
```

Then **open every generated PNG** (iso, front, back, left, right, top). Syntax-clean does NOT
mean correct geometry. Multi-view catches:

- inverted normals / inside-out geometry,
- misaligned boolean operations, Z-fighting, floating geometry,
- proportion errors that a single view hides (a squat drum, a "wedding-cake" cone).

**Never skip the visual pass.** A model that looks right from the front can be badly wrong in 3D.

### 4. Match the reference — MANDATORY when the project has one

If `3d.yaml` has `project.reference` (or there are images under `references/`), compare on
**every** iteration:

```bash
3d fit-camera parts/chair.scad references/chair.jpg   # writes an overlay PNG + an IoU
3d score     parts/chair.scad references/chair.jpg    # AE + IoU, KEY=VALUE
```

Look at the **overlay images** and the **multi-view renders** — do not chase the single IoU
number (a degenerate zoomed-in camera can inflate it). Fix the GEOMETRY, not the camera.

### 5. Iterate — measure every round, and don't loop

Adjust the `.scad`, re-render, re-compare, **commit** each meaningful step (`git add -p &&
git commit`). Git is your version history — no `_001`/`_002` filenames.

**Every round, READ the metric and ask "did it actually get better?"** Track the comparison
numbers (`3d compare` IoU/SSIM, the collage) across rounds — keep a change only if the metric
improves; revert it if it regressed. Improving numbers = converging; flat/oscillating numbers =
you are **looping**, not progressing.

**If the metric has not improved for ~3 rounds, STOP nudging the same approach — brainstorm a
different one.** Step back and consider a structurally different strategy: different primitives or
decomposition, a different module breakdown, fixing proportions before detail, a different camera/
section to expose the real mismatch, or rebuilding the worst part from scratch. Small parameter
tweaks on a dead-end approach will keep the numbers flat forever. Changing the *approach* is what
breaks a plateau — wasting rounds on the same idea is the failure mode to avoid.

### 6. Extract parameters (optional)

```bash
3d params parts/chair.scad            # or --json for machine-readable
```

### 7. Gate, then export

```bash
3d check parts/chair.scad             # manifold + fit + printability — must PASS
3d export parts/chair.scad -o chair.stl
# override params:
3d export parts/chair.scad -o chair_wide.stl -D 'width=80'
```

`3d export` validates geometry (non-manifold, self-intersections, degenerate faces) and exits
non-zero on bad meshes. Run the **fdm-printability** skill's checklist before any STL.

## Cross-sections

```bash
3d render parts/chair.scad --section -o section.png            # generic STL-cut
3d render parts/chair.scad --section --color -o section.png    # colored assembly section
```

## Full pipeline

```
write .scad → validate → render --multi (read PNGs) → fit-camera/score (vs reference)
            → [iterate + commit] → check → export
```

## OpenSCAD quick reference

```openscad
// Shapes
cube([x, y, z]);  sphere(r=r);
cylinder(h=h, r=r);            // cylinder
cylinder(h=h, r1=rb, r2=rt);   // cone

// Transforms
translate([x,y,z]) obj();  rotate([rx,ry,rz]) obj();
scale([sx,sy,sz]) obj();   mirror([x,y,z]) obj();

// Booleans
union() { a(); b(); }
difference() { a(); b(); }     // a minus b
intersection() { a(); b(); }

// Advanced
linear_extrude(h) shape2d();
rotate_extrude() shape2d();
hull() { a(); b(); }           // convex hull
minkowski() { a(); b(); }      // rounded edges (slow)

// Color by role/material (read better, carries into preview/export)
color("SteelBlue") body();

// Smoothness
$fn = 96;  // higher = smoother curves

// 2D
circle(r=r);  square([x,y]);
polygon(points=[[x1,y1], ...]);
text("str", size=10);
```
