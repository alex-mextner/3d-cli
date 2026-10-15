# `3d gcode-overhangs` — extrusion printed on nothing

Checks a sliced G-code, offline, layer by layer: for every object, the extrusion that nothing
below it holds up. With supports off the slicer says nothing about it: a K1 SE overlay plate
whose bottom was not flat printed pieces of its underside in the air while every other gate
was green. [`3d print-preflight`](print-preflight.md) runs this check as its `overhangs`
gate, so [`3d printer start`](printer.md) refuses such a plate unless `--force`.

## Usage

```
3d gcode-overhangs FILE.gcode [--max-unsupported MM2] [--bridge-max MM] [--ledge-max MM]
                    [--min-area MM2] [--png PATH | --no-png] [--json]
```

```bash
3d gcode-overhangs print/k1se/k1_ovl1_v3.gcode
3d gcode-overhangs plate.gcode --max-unsupported 50 --png /tmp/plate-overhangs.png
3d gcode-overhangs plate.gcode --bridge-max 25
3d gcode-overhangs plate.gcode --json | jq '.objects[] | {name, status, unsupported_mm2}'
```

## What is measured

Objects come from the slicer's labels (`EXCLUDE_OBJECT_START`; Bambu Studio's
`; start printing object, unique label id: N` / `M624` … `M625` blocks, named by their
`; printing object NAME` comment; `M486`; `; printing object …`); a file without labels is one
object, `plate`. Extrusion before the first layer marker (a purge line in the start G-code),
skirt, brim, prime/wipe tower, ironing and custom blocks (`;TYPE:` or Bambu `; FEATURE:`) are
ignored; support and raft lines carry the object but are not checked themselves. Lines are
rasterised at 0.1 mm (coarser for very large objects) with their `;WIDTH:` (Bambu `; LINE_WIDTH:`).

| Term | Definition |
|---|---|
| carried | inside the layer below — the object's own lines plus its support/raft, raster gaps closed and enclosed gaps up to `--bridge-max` wide filled (a top skin over sparse infill) — grown by the 45° reach: `dz × tan 45° + half the line width`. The bed carries the first layer. |
| bridged | an uncarried run on a row, a column or a diagonal no longer than `--bridge-max` with carried material past both ends: a ceiling over a pocket, a bridge between walls. A strip growing out of one edge (a shallow underside, a cantilever) is anchored on one side only and is not a bridge. The slicer's `;TYPE:Bridge` / `Overhang wall` labels are not used: it labels a shallow underside printed in the air the same way. |
| step-out | uncarried, but no more than `--ledge-max` past the reach, or a piece no wider than `--ledge-max`: an edge one line wider than the layer below, printed as an overhang perimeter |
| island | the rest, a connected piece over `--min-area` (smaller pieces are grass-blade tips and raster noise) |

| Result | When |
|---|---|
| FAIL | the islands of one object add up to more than `--max-unsupported` mm² |
| WARN | smaller islands (short ceilings, tips), or edge step-outs over `--min-area` |
| PASS | every line carried or bridged |
| SKIP | no extruding moves, or spiral (vase) mode |

The report lists per object the island total, the step-out total, the base z and the five
largest pieces with their kind, z, centre x/y and area. The top-down map (PNG, default
`~/.cache/3d-cli/overhangs/NAME-<sha256>.png`) shows each object's footprint grey and the
islands red, darker = higher.

**Calibration.** The defaults (100 mm², 15 mm, 0.6 mm, 2 mm²) come from the babushkin-dom K1 SE
plates: the overlay `k1_ovl1_v3` FAILs with ~1940 mm² on nothing in 100 islands (z 0.24–3.84);
`k1_base_2`, `k1_base_3_v3` and `k1_base_4_v3` WARN with 0–15 mm² of small ceilings and
180–250 mm² of edge step-outs. They are fitted to those plates, not derived from print
physics; a visible side has the stricter rule of [`3d visible-faces --gcode`](visible-faces.md).

**Speed.** Each layer's carrier is a raster; only the pixels that stick out of it are tested
(reach by a k-d tree on the carrier's edge, bridges on sorted runs of those pixels), and the
layers are split over forked workers (`THREED_MAX_WORKERS`, default ≤ 6). A 30 MB K1 SE plate
takes about 7 s.

## Options

- `--max-unsupported MM2` — island area per object allowed before FAIL (default 100): `3d gcode-overhangs plate.gcode --max-unsupported 50`
- `--bridge-max MM` — longest span counted as a bridge, widest enclosed gap filled (default 15): `3d gcode-overhangs plate.gcode --bridge-max 25`
- `--ledge-max MM` — widest edge step-out counted as an overhang perimeter (default 0.6): `3d gcode-overhangs plate.gcode --ledge-max 0.4`
- `--min-area MM2` — smallest piece reported (default 2): `3d gcode-overhangs plate.gcode --min-area 0.5`
- `--png PATH` — write the map there: `3d gcode-overhangs plate.gcode --png /tmp/map.png`
- `--no-png` — no map: `3d gcode-overhangs plate.gcode --no-png`
- `--json` — JSON report (`status`, `detail`, `objects[]` with `unsupported_mm2`, `ledge_mm2`, `islands[]`, `png`): `3d gcode-overhangs plate.gcode --json`

Exit: 0 PASS, WARN or SKIP; 1 FAIL; 2 usage error or missing file.
