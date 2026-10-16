# `3d visible-faces` — the visible side of a part prints with no support and no sag

Decorative parts (facades, panels, figurines) have a side everyone looks at. A global overhang fraction (see [printability](printability.md)) cannot tell a hidden back overhang from a sag on that face. `visible-faces` takes the visible side as a half-space of the print frame and fails when anything on it will not print clean — first on the model, then on the actual sliced G-code.

## Usage

```
3d visible-faces PART.stl[,VOLUME2.stl...]|PLATE.3mf [...] --visible SPEC [options]
3d visible-faces --gcode PLATE.gcode --visible [OBJ:]SPEC [options]
```

```bash
3d visible-faces wall_red.stl,wall_white.stl --visible 'y<4.8'
3d visible-faces wall.stl --visible 'y<4.8' --openings windows.json --hidden covered.json
3d visible-faces plate.3mf --visible 'WALL*:y<104.8' --visible 'ROOF:all'
3d visible-faces --gcode plate_1.gcode --visible 'WALL1:y<104.8' --json | jq .status
```

## The visible side

- `--visible SPEC` — `x<V`, `x>V`, `y<V`, `y>V` (mm) or `all` (every face visible). Prefix `OBJ:` (a glob) to bind it to a 3MF object, STL part (file stem) or G-code object label; Orca labels also match without their `_id_N_copy_M` suffix and before a `_—_` separator. A bare `SPEC` applies to every part/object without its own. Repeatable. Objects with no matching `--visible` are listed as *not gated*. A pattern that matches nothing is a FAIL — checked against the G-code objects when `--gcode` is given (a planned object missing from the slice), else against the mesh parts (a typo).
- `--hidden [OBJ:]FILE` — JSON `[[[u, z], ...], ...]`: polygons of the visible side that are covered (another part stands in front, the ground under a house). `u` is the other bed axis: `x` for `y<V` and `all`, `y` for `x<V`.
- `--openings [OBJ:]FILE` — JSON `[[u0, u1, z0, z1], ...]`: openings (windows) where short bridges such as glazing bars are allowed.

Frames: the mesh pass works in the input's own coordinates (an STL as given; a 3MF per build item with its transform and unit, i.e. bed coordinates in mm), the G-code pass in bed coordinates. Over a raft (support below the object's first own layer), G-code z is shifted back to model z (first object layer = one layer height) before hidden polygons and openings are applied; without a raft G-code z is model z.

## Mesh pass (slicer view)

Comma-joined STLs are the colour volumes of one part and are unioned (manifold3d), so the interfaces between colours are not surfaces; a 3MF object's components are unioned the same way. The solid is sliced every `--layer`; each layer is the union of three planes 0.01 mm apart, so a CSG hairline (a horizontal face lying in the plane, a slit under 0.02 mm) is not printable geometry.

Layers start at the bed (z 0, or the part's lowest point if it is below the bed): a part that starts above the bed has nothing under its first layer and fails. On the visible side, the part of layer N outside layer N−1 grown by the self-supporting reach (`layer × tan 45° + 0.1 mm`, Miter offset) is an unsupported piece. A piece over `--min-area` that survives a −0.05 mm offset (wider than 0.1 mm somewhere) **FAILS**, listed with area, centre and x/y bounds and z. In effect a layer may step out up to reach + 0.1 mm: ~0.32 mm per 0.12 mm layer (≈69°) — a modelled 45° or 60° slope passes, a horizontal ledge or a 70° underside fails (at 0.3 mm layers a 60° underside fails too).

A piece inside a declared opening (±0.3 mm in u, `z0 ≤ z ≤ z1 + 0.3`) whose both ends rest on the layer below and whose span (piece + the reach eaten off each side) is ≤ `--bar-span` is a **bridge by design**: reported, not a failure.

## G-code pass (actual slice)

Objects come from the slicer's object labels: `EXCLUDE_OBJECT_START NAME=…` / `EXCLUDE_OBJECT_END`, Bambu Studio's `; start printing object, unique label id: N` / `M624` … `M625` blocks (named by their `; printing object NAME` comment; an object pattern matches the name before ` — ` too), `M486`, `; printing object …`; a file without them is one object named `plate`. Layer z comes from `;Z:` (or `; Z_HEIGHT:`) comments, else the move's Z; feature types from `;TYPE:` (or `; FEATURE:`). `G0/G1` and arcs `G2/G3` (I/J) are followed, with relative (`M83`) and absolute (`M82`, `G92 E`) extrusion. Brim, skirt, prime/wipe tower and custom moves are not the object.

- **Support**: a `Support*` extrusion at or above the object's first own layer (support below it is the raft) with any point of the line or arc on the visible side **FAILS**. Support printed outside any object block is charged to every gated object whose extrusion bounding box (+1 mm) it touches.
- **Islands**: each layer is rasterised at 0.05 mm (line centres dilated 4 px = a 0.4 mm line). The layer below becomes a footprint (closing 24 px + fill holes, so sparse-infill gaps and enclosed voids count as carried). An island of the layer more than 0.35 mm (7 px) outside the footprint that reaches the outer 0.6 mm skin band of its layer, over `--min-area`, centred on the visible side, **FAILS**: a modelled 45° chamfer steps 0.14 mm per 0.12 mm layer, a dropped wall sliver under a relief or a sag steps ≥ 0.4 mm. A layer more than 1.5 layer heights above the previous one has nothing under it. Islands inside an opening anchored within 1 mm past both ends and no longer than `--bar-span` are bridges by design.

## Options

- `--layer MM` — layer height (default 0.12)
- `--min-area MM2` — smallest piece/island reported (default 0.5)
- `--bar-span MM` — longest bridge allowed inside an opening (default 10)
- `--json` — JSON report: `status`, `mesh[]` (`pieces[]` with `area_mm2`, `x`, `y`, `z`, `bbox`; `bridges[]` with `span_mm`), `gcode[]` (`support[]`, `islands[]`, `bridges[]`, `error`), `skipped`, and the thresholds used

## Use from other commands

`commands.visible_faces.check(args)` runs the gate with the same arguments in a subprocess (heavy deps resolved by `cli.pyrun`) and returns the exit code — for a print preflight or a hook. A part or G-code file that does not exist raises `InputNotFound` before the subprocess starts.

## Exit codes

- `0` — `PASS`
- `1` — `FAIL` (an unsupported piece, support move or island on the visible side; a gated G-code object with no extrusion or no match)
- `2` — usage / load error (missing file, bad `--visible`, unreadable JSON or mesh, a volume that is not a closed manifold, nothing gated)
