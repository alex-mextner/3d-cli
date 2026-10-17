# `3d mix` — colour mixing by layers (Snapmaker Orca mixed filaments)

The U1 mixes colours by layers: Snapmaker Orca's mixed filaments (Full Spectrum) print **one**
physical filament per layer and alternate them from layer to layer, either by a manual pattern
(`431` = head 4, head 3, head 1, repeat) or by Local-Z ratio rows (sublayers). The eye mixes the
stripes only on **walls**; a horizontal top shows the last layer, one plain filament.
Epic: [#410](https://github.com/alex-mextner/3d-cli/issues/410), plan §20.

## Usage

```
3d mix check FILE.3mf [--json]
```

## `check` — mixed parts show their colour on walls

Lints a project 3MF before slicing ([#411](https://github.com/alex-mextner/3d-cli/issues/411)).
For every part whose filament is a mixed one (a `mixed_filament_definitions` row: manual pattern
or Local-Z ratio row; a part with extruder `0` takes its object's filament), its faces in bed
coordinates (build and component transforms applied) are summed by normal:

| Class | Faces | Why |
|---|---|---|
| top | looking up, within 30° of horizontal | shows the last layer: one filament |
| side | within 30° of vertical | shows every layer: the mix |
| slope | the rest looking up | terraces: each step shows one layer |

Faces looking down sit on the bed or under the part and are not counted.

- **FAIL** when top > side / 2: the part shows its colour mainly on a horizontal face. From any
  one direction a viewer sees at most about half of a part's walls but all of its top. Stand the
  part up so its coloured face is vertical.
- **WARN** when slope > side / 2 (a roof at 45°).
- **PASS** otherwise. A part in a physical filament is not checked.

Exit 1 on FAIL, 0 otherwise; 2 when the file is not an Orca-family project 3MF (needs
`3D/3dmodel.model`, `Metadata/project_settings.config`, `Metadata/model_settings.config`).

`3d print-preflight` runs the same check as its `mix face` line on the project 3MF next to the
G-code (`--source-3mf`, else `NAME.3mf` next to `NAME.gcode`); see
[print-preflight](print-preflight.md).

## Examples

```bash
3d mix check out/u1_tiles.3mf
3d mix check out/u1_tiles.3mf --json
```

A 20×20×3 mm tile lying flat in mix `431` next to a reference tile in a physical filament:

```
$ 3d mix check flat.3mf
mix check flat.3mf: FAIL
  FAIL PTA / PTA: filament 5 (431) — visible face horizontal: top 400 mm² > half the walls 120 mm²; stand the part up so its coloured face is vertical (a layer mix reads only on walls)
```

The same tile standing (20 wide, 3 deep, 20 tall): `PASS PTA / PTA: filament 5 (431) — walls 920 mm², top 60 mm²`.

`--json` prints `{check, status, mixed_rows, failures, parts: [{object, part, extruder, recipe,
top_mm2, side_mm2, slope_mm2, status, detail}]}`.
