# `3d props` — snap-off ribs for a part printed standing

A tall thin part printed upright — a wall panel, a roof half, a rim, a sign — rocks at every tool change and z-hop and can tip over hours into the print. `props` adds sloped snap-off ribs on the part's hidden back side and gates its stability: when the part is too tall for its base it FAILs before the print starts.

## Usage

```
3d props <part.stl|part.3mf> --back +y|-y [--object NAME] [--spacing 50] [--height 0.6]
         [--angle 55] [--gap 0.25] [--max-ratio 3] [-o OUT.3mf|OUT.stl] [--json]
```

```bash
3d props wall.stl --back +y
3d props wall.3mf --back -y -o wall_props.3mf
3d props plate.3mf --object roof_L --back +y --json | jq '{ratio, ok}'
```

The part must already be in its **print frame**: z up, standing on the bed, its thickness along y. `--back` names the side the props go on (the side nobody sees after assembly).

## What it builds

| Piece | Shape |
|---|---|
| ribs | one every ~`--spacing` mm along x, at least two (the first and last 10 mm in from the ends; a part shorter than 20 mm gets one in the middle). Each is a 1.6 mm plate across the part, from the bed up to `--height` × the part height. Its inner edge stands `--gap` behind the **hindmost** point of the back at that station — a straight edge, so a hollow underside or a ribbed back does not pull the rib into its cells, where it could not be snapped off. Its outer edge slopes down to the bed at `--angle`, so the rib prints without overhang. |
| tabs | up to three 0.8 mm square bridges per rib that tie it to the back, only at heights where the back reaches within 0.5 mm of the rib edge, nearest the bottom (5 mm up), middle and top (3 mm under the rib top) of the rib; each reaches 0.3 mm into the back as it stands at that height |
| foot | a 0.6 mm strip on the bed joining all ribs, 1 mm behind their inner edges |

The ribs and the foot are cut back from the part dilated by `--gap` in y and z, so only the tabs touch the part.

The back profile at each station is read from slices of the part every 1 mm in z through a window 1 mm wider than the rib.

## Stability gate

`ratio = part height / (part depth at the bed + rib reach)` must be at most `--max-ratio`. The depth at the bed is the y extent of the part's slice 0.01 mm above its bottom (what actually touches the bed; a part with no such slice has depth 0); the rib reach is the distance from the hindmost point of the back to the rib's outer edge on the bed.

With the default `--height 0.6` and `--angle 55`, the rib reach alone is about 0.42 × the part height, so the ratio of a part that gets ribs stays below 2.4 and the 3.0 gate passes. The gate bites when ribs are kept short (`--height`), steep (`--angle`), or when no rib fits (no material at any station; the reach is then 0).

## Output

Without `-o` only the report is printed. When the gate passes:

- `-o OUT.3mf` writes the input with the props as an **extra part of the same object** (named `<object> props`). Its extruder is the extruder of the object's largest part (from Orca/Bambu `Metadata/model_settings.config`; unset when the input has none, so the slicer uses the object's). Other objects of a 3MF are carried over, each keeping its `printable` flag. The output holds geometry, object/part names, extruders and the printable flags, with every mesh in world coordinates (millimetres); other slicer settings of the input project are not copied.
- `-o OUT.stl` writes the props alone (add it to the object as a part in the slicer).

Nothing is written when the gate fails.

## Options

- `--back +y|-y` — side the props go on; required
- `--object NAME` — the 3MF object to prop; required when the 3MF holds several objects (name from `model_settings.config`, else the `<object name>`)
- `--spacing MM` — rib spacing along x (default 50)
- `--height F` — rib height as a fraction of the part height, 0–1 (default 0.6)
- `--angle DEG` — slope of the rib's outer edge to the bed, above 0 and at most 89 (default 55)
- `--gap MM` — snap-off gap between props and part (default 0.25)
- `--max-ratio F` — stability limit (default 3)
- `-o OUT` — `.3mf` (props as an extra part) or `.stl` (props only)
- `--json` — JSON report on stdout: `stations`, `station_x`, `height` (rib height, mm), `reach`, `depth`, `tabs`, `ratio`, `max_ratio`, `ok`, `status`, `output`

## For other commands

`commands.props.check(path, back, *extra_args)` runs the gate in a subprocess and returns the exit code (print preflight, hooks).

## Exit codes

- `0` — `PASS`
- `1` — `FAIL`: height / base ratio above `--max-ratio`
- `2` — usage / load error (missing file, not a closed manifold, several 3MF objects without `--object`)

Ported from babushkin-dom `gen/standing_props.py`. Differences: the ratio uses the part depth at the bed rather than over the whole part; a tab reaches into the back as it stands at the tab's height (the original anchored every tab to the station's hindmost point, so a tab where the back is up to 0.5 mm lower could miss the part) and never dips under the bed; a part shorter than 20 mm gets one rib in the middle instead of two coinciding ones; a part with no rib is still gated (reach 0).
