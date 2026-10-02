# `3d prep` - repair, auto-orient, and print-ready-export a model

Takes a `.scad` / `.stl` / `.3mf` model and prepares it for FDM printing in one pass, then
reports whether it is print-ready. It fills the gap left by the rest of the CLI: nothing else
did mesh repair, auto-orientation, or a single print-prep orchestrator.

The pipeline is:

1. **Load.** A `.scad` is exported to a temporary binary STL first (OpenSCAD); `.stl` / `.3mf`
   load directly.
2. **Mesh repair** (trimesh): merge coincident vertices, drop duplicate and degenerate
   (zero-area) faces, make the winding consistent, recompute outward normals, and fill
   boundary holes. The report shows the **before/after** watertight + winding state and how
   many hole loops were closed. It never claims success blindly - if the mesh is still
   non-watertight after repair, the report and the exit code say so.
3. **Auto-orient.** Every convex-hull face normal (the "rest on this face" candidates) plus
   the 6 axis-aligned directions are evaluated. Each candidate is scored by its **support
   overhang area fraction** - the same downward-face / 45deg test as `3d printability`, but
   excluding the faces that rest *on the bed* (those carry the part and never need support).
   Ties break by **lower build height**, then **smaller footprint**. The winner is applied:
   the resting face is rotated down and the part is dropped so `min-Z = 0`.
4. **Final report.** The shared printability analyzer (`printability_mesh.analyze`) and the
   manifold/watertight gate (`mesh_check`) are re-run on the prepared mesh.
5. **Emit.** The repaired + oriented STL is written to `--out` (default `<input>.prep.stl`),
   and, with `--3mf`, a print-ready single-plate Orca/Bambu project `.3mf` beside it.

**Scope:** `prep` does **not** generate supports, invoke a slicer, or pack multiple plates -
that is `3d slice-check` and `3d arrange`. It is repair + orient + single-body print-ready
export + report, nothing more.

The command exits `0` when the model is **READY** (watertight after repair *and* the
printability verdict is `PASS`) and `1` when it is **NOT READY** (still non-watertight, or a
HARD printability rule failed), so it gates a pipeline.

## Usage

```bash
3d prep <file.scad|.stl|.3mf> [options]
```

| Option | Default | What |
|---|---|---|
| `-o, --out PATH` | `<input>.prep.stl` | Output STL path. |
| `--3mf` | off | Also write a print-ready single-plate `.3mf` next to the STL (`<out-stem>.3mf`). |
| `--bed MM` | `270` | Square bed size (mm) recorded in the `.3mf`. |
| `--json` | off | Emit a machine-readable JSON summary instead of the table. |
| `-D k=v` | - | OpenSCAD variable define (repeatable; only for a `.scad` input). |

## Examples

```bash
# Prepare the sample model, writing examples/cube.prep.stl and a READY verdict:
3d prep bracket.scad

# Repair + orient a raw mesh to a chosen output path:
3d prep part.stl -o part.ready.stl

# Also emit a print-ready single-plate 3MF, with a 256 mm bed recorded:
3d prep part.3mf --3mf --bed 256

# Machine-readable summary (repair, orientation, printability, outputs, verdict):
3d prep part.stl --json

# Prep a parametric variant without editing the .scad:
3d prep bracket.scad -D 'depth=40'
```

## JSON summary

`--json` prints an object with `repair` (before/after watertight, winding, holes filled),
`orientation` (chosen down-direction, rotation degrees, overhang before/after and
improvement, build height, footprint), `printability` (the shared analyzer's per-check
verdicts), `mesh_check` (manifold/watertight/self-intersection detail), `outputs` (the
written file paths), `ready` (bool), and `reasons` (why it is not ready, if applicable).

## Exit codes

| Code | Meaning |
|---|---|
| `0` | READY - watertight after repair and printability `PASS`. |
| `1` | NOT READY - still non-watertight after repair, or a HARD printability rule failed. |
| `2` | Usage / IO error (missing, unreadable, or empty mesh; bad flag). |
| `127` | A required tool is missing (OpenSCAD for `.scad`, or the Python mesh runtime). |
