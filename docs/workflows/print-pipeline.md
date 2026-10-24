# Print pipeline — every gate, in order

A part goes from model to printer through three groups of gates. Each gate is a `3d`
command that exits 1 on FAIL, so a shell script or an agent stops at the first broken one.
The G-code gates are not optional: [`3d print-preflight`](../commands/print-preflight.md) runs
all of them, and [`3d printer start`](../commands/printer.md) runs `print-preflight` and refuses
on FAIL unless the owner approves a [forced start](../commands/printer.md#forcing-a-start) on Telegram.

```
model gates ──> slice ──> G-code gates ──> print-preflight ──> printer start
```

## 1. Model gates (mesh / 3MF, before slicing)

| Order | Gate | Command | Catches |
|---|---|---|---|
| 1 | mesh | [`3d mesh PART.stl`](../commands/mesh.md) (or `3d check FILE.scad` for every model gate of a SCAD model) | non-manifold, open or inverted meshes |
| 2 | visible faces | [`3d visible-faces PART.stl --visible 'y<4.8'`](../commands/visible-faces.md) | support or sag on the side people look at |
| 3 | fits and sockets | [`3d fit-check --spec fits.yaml`](../commands/fit-check.md) | an insert already printed no longer fits its socket (size, gap, overlap, depth), or the socket is **not open** along the insertion direction — capped over the insert or undercut (`open: none` for an insert captured between parts) |
| 4 | halves | [`3d halves-check PLATE.3mf`](../commands/halves-check.md) | split halves that no longer mate |
| 5 | mixed colour on walls | [`3d mix check PLATE.3mf`](../commands/mix.md) | a part in a mixed filament (layer pattern or Local-Z) lying with its coloured face horizontal: a layer mix reads only on walls, a flat top shows one filament |

## 2. Slice, and record what the slicer said

[`3d slice`](../commands/slice.md) or the slicer CLI; [`3d slice-check PLATE.3mf`](../commands/slice-check.md)
checks that the project opens and slices headless.

A ready project 3MF made for another printer is not re-sliced from the mesh: retarget it first
(`3d retarget PROJECT.3mf --to PRINTER --keep N`, [`docs/commands/retarget.md`](../commands/retarget.md)) —
it keeps the orientation, supports and tuned settings and fails loudly when the part cannot fit.

Every slice records the slicer's own warnings next to the G-code, bound to its SHA-256:
`3d slice` does it itself; a project script that runs the slicer CLI keeps its stdout+stderr,
`result.json` and (with `--export-3mf`) the sliced 3MF, and after the last edit of the G-code
(`3d gcode-thumbnails add` moves a current record along) calls
[`3d slice-warnings record`](../commands/slice-warnings.md):

```bash
"$ORCA" --debug 2 --arrange 0 --slice 0 --outputdir "$TMP" --export-3mf sliced.3mf plate.3mf > "$TMP/slice.log" 2>&1
cp "$TMP/plate_1.gcode" out/plate.gcode && 3d gcode-thumbnails add out/plate.gcode
3d slice-warnings record out/plate.gcode --log "$TMP/slice.log" --result "$TMP/result.json" --sliced-3mf "$TMP/sliced.3mf"
3d slice-warnings accept out/plate.gcode wc264eaa3 'colours are set per head on the printer'
```

Warnings are never filtered: each one is resolved (fix the model or the settings, re-slice) or
accepted with a reason; an acceptance carries over to the same message in the next slice.

## 3. G-code gates (offline, on the sliced file)

All of these run inside `3d print-preflight FILE.gcode`; each is also a command of its own.

| Order | Gate | Command | Catches |
|---|---|---|---|
| 1 | move bounds | [`3d gcode-bounds FILE.gcode --printer NAME`](../commands/gcode-bounds.md) | a move or an arc bulge outside the axis travel; plate moves outside the printable area; untrusted macros |
| 2 | overhangs | [`3d gcode-overhangs FILE.gcode`](../commands/gcode-overhangs.md) | extrusion printed on nothing — a bottom that is not flat (the K1 overlay printed in the air), a cantilever, a span too long to bridge — which the slicer does not report with supports off |
| 3 | slicer warnings | [`3d slice-warnings check FILE.gcode`](../commands/slice-warnings.md) | a slicer warning or error for this exact G-code that is neither resolved nor accepted with a reason; no record, or a record of another slice |
| 4 | head sequence | [`3d slice-check PLATE.3mf --gcode FILE.gcode --check head-sequence`](../commands/slice-check.md) | an object printed on the wrong head on some layer (mixed-filament patterns); needs the project 3MF |
| 4a | mix face | [`3d mix check PLATE.3mf`](../commands/mix.md) (the same check, on the project 3MF next to the G-code) | a part in a mixed filament whose top outweighs half its walls; by the project 3MF (FAIL), else by the G-code alone for objects printed by several heads (WARN) |
| 4b | mix layers | [`3d mix gcode FILE.gcode --project PLATE.3mf`](../commands/mix.md) (the same check) | the additional colour printed on consecutive layers, a colour band or a mixed layer thicker than 0.12 mm, measured on the real per-layer heads and Z; works without the project for objects that alternate one head per layer |
| 5 | thumbnails | [`3d gcode-thumbnails check FILE.gcode`](../commands/gcode-thumbnails.md) | missing printer-screen previews |
| 6 | visible faces | [`3d visible-faces --gcode FILE.gcode --visible 'WALL*:y<104.8'`](../commands/visible-faces.md) | support or sagging islands on a visible side, in the real toolpaths |

## 4. `print-preflight`, then `printer start`

```bash
3d print-preflight out/plate.gcode --printer u1 --source-3mf out/plate.3mf --visible 'WALL*:y<104.8'
3d printer upload u1 out/plate.gcode
3d printer start u1 out/plate.gcode --yes --source-3mf out/plate.3mf
```

`print-preflight` adds the printer-side checks (printer free, loaded colours per head) and runs
every G-code gate above. A gate whose input is missing is reported as SKIP with what to pass —
the head-sequence and mix-face gates need the project 3MF (`--source-3mf`, or `NAME.3mf` next to
`NAME.gcode`), the visible-faces gate needs `--visible` — never as a silent PASS.
