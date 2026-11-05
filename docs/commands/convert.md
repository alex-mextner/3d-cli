# `3d convert` — one command for every model-format conversion

Converts one or more model files into a result file; the output format comes from the
result's extension. The flagship route is **multicolour scad → Orca project 3MF**: every
`color()` group of the OpenSCAD source becomes its own object with per-object `extruder`
metadata and its authored colour, so an AMS / multi-filament slicer prints the model in
colours instead of one uniform blob.
Issue: [#741](https://github.com/alex-mextner/3d-cli/issues/741) (multicolour projects
in general: [#89](https://github.com/alex-mextner/3d-cli/issues/89)).

## Usage

```
3d convert <IN...> <OUT> [options]
```

One or more inputs, the **LAST argument is the result**. Inputs: `.scad .stl .3mf .usdz .obj`.

| Option | Default | What |
|---|---|---|
| `--force` | off | Overwrite an existing OUT. Without it an existing target is refused (exit 2) with the ready-to-run command that uses a free name. |
| `-D k=v` | — | OpenSCAD define for `.scad` inputs (repeatable). |
| `--color r,g,b` | `0.78,0.74,0.66` | Diffuse colour `0..1` for a single-colour `.usdz` output; authored colours are kept. |
| `--bed MM` | `270` | Square bed size for the `.3mf` project layout (same default as [`3d arrange`](arrange.md)). |
| `--to PRINTER` | — | `3mf→3mf` only: retarget the project to that machine preset. Delegates to [`3d retarget`](retarget.md); its `--set`, `--preset`, `--keep`, `--margin`, `--process`, `--filament`, `--preset-dir`, `--include-non-printable` and `-v` options pass through. |

```bash
3d convert mole.scad mole.3mf                  # multicolour Orca project (one object per colour)
3d convert mole.scad mole.usdz                 # AR Quick Look package, one coloured mesh per colour
3d convert part.stl copy.stl                   # lossless same-format copy
3d convert model.scad out.stl -D 'depth=40'    # parametric export
3d convert a.stl b.stl merged.3mf              # merge several inputs into one project
3d convert model.scad shot.png                 # render as png
3d convert spoon.3mf spoon-u1.3mf --to u1      # retarget alias (`3d retarget` under the hood)
3d convert spoon.3mf spoon-u1 --to u1          # same, extension filled from the input
```
## Output formats

| Extension | What you get |
|---|---|
| `.stl` | Binary mesh. A single `.scad` goes straight through OpenSCAD (the [`3d export`](export.md) route); a project `.3mf` uses its placed world coordinates (`slicing/retarget_3mf.export_stl`); anything else (or several inputs) is merged through the colour-aware mesh worker, inputs laid out side by side. |
| `.3mf` | A true Orca/Bambu **project** 3MF (plates, object names, per-object metadata) built by `lib/orca_project_3mf.py`. From `.scad`/`.stl`/`.usdz`/`.obj` inputs every colour group becomes its own object with per-object `extruder` metadata (1-based, first-appearance colour order; uncoloured geometry prints on extruder 1). From a `.3mf` without retarget options the project is repackaged unchanged (a rename). |
| `.usdz` | AR Quick Look package; one coloured mesh per colour group (`lib/usdz.py`). |
| `.png` | A render of the model (exactly one input; `.scad`/`.stl` render directly, other inputs are staged through an STL). |
| *(none)* | Filled from the conversion: a single `.stl`/`.3mf` input keeps its own format, anything else becomes `.3mf`. |

Not writable: `.scad` (use [`3d import`](import.md) for a wrapper) and the planned export
formats (`obj`, `ply`, `glb`, …) — refused with the accepted list.

## Multicolour scad → 3MF

`lib/convert_mesh.py` renders the scad through OpenSCAD's 3MF export, which keeps every
`color()` as a per-triangle base material, reads that back colour-grouped
(`lib/dimension_model.py`), and emits one project object per colour:

- each object carries `<metadata key="extruder" value="N"/>` (Orca's per-object
  filament slot) plus its authored colour as the 3MF base material;
- the colour groups keep their authored relative positions — the overlap between
  adjacent colour groups is exactly what fuses the colours in the print;
- the assembly is laid flat (min z = 0) and centred on the bed; several inputs are
  placed side by side with a 12 mm gap;
- equal colours share one extruder slot, across inputs too.

Open the result in an Orca-family slicer, map the extruder slots to your filaments
(AMS slots or the filament list) and slice.

## Collision behaviour

- An output that would overwrite **one of its own inputs** never fails: the same
  extension is legal, the output silently becomes the next free suffix
  (`3d convert a.stl a.stl` writes `a-2.stl`).
- Any **other existing target** is refused (exit 2) with the exact ready-to-run command
  using a free name, plus the `--force` variant. `--force` overwrites.
- The file may appear later: every route writes a staging file beside the target first
  and commits it only after re-checking that the target still does not exist, so a
  failed or refused conversion never leaves a half-written output (and never clobbers a
  file that appeared mid-run).

## Exit codes

- `0` — converted
- `1` — the conversion itself failed (OpenSCAD error, unreadable geometry, retarget did not fit)
- `2` — bad usage, unreadable input format, or a refused overwrite
