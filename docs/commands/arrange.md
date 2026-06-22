# `3d arrange` - split parts and pack them onto bed-sized plates

Takes a `.3mf` (parts in their assembled positions) or one-or-more `.stl` files, splits each
object into **connected bodies** so each glyph / loose part packs independently, lays every
part flat (drops its min-Z to 0), and shelf-packs the XY footprints onto square plates that
fit the printer bed.

**By default (`--single`) it writes ONE multi-plate Orca/Bambu project `.3mf`**
(`<prefix>.3mf`) holding **every** build plate, with each part assigned to its plate.
OrcaSlicer-based slicers (including the Snapmaker U1's) open the single file and show all N
plates already arranged. The on-disk format matches the real OrcaSlicer exporter
(`_BBS_3MF_Exporter` in `src/libslic3r/Format/bbs_3mf.cpp`): an OPC zip with
`[Content_Types].xml`, `_rels/.rels`, `3D/3dmodel.model` (one `<object>`/positioned build
`<item>` per part) and `Metadata/model_settings.config` (one `<plate>` per build plate with
`plater_id` + a `<model_instance>` per part). Plates are positioned in Orca's virtual world
grid (`ceil(sqrt(N))` columns, plate stride = `bed * 1.2`).

**`--per-plate`** writes the legacy output instead: **one print-ready `.3mf` per plate**
(`<prefix>_plate1.3mf`, `_plate2.3mf`, ...), each a `trimesh.Scene` of the placed parts as
separate objects, flat on `z = 0`, non-overlapping, centered, within the bed.

Every written `.3mf` is reloaded and verified. The single project checks it is a valid zip
with the required entries, that the object count and plate count round-trip, that every object
is assigned to a plate, and that each plate's footprint fits the bed. Per-plate files check
object count and bed fit.

This solves the common problem where a `.3mf` storing parts in their assembled positions
dumps everything onto **one** plate, which overflows the bed (default `270x270` mm, the
Snapmaker U1).

When the connected-body split throws off **degenerate slivers** (a stray single triangle, a
zero-height shadow — OpenSCAD/CAD exports often emit these), they are **dropped** before
packing: a body with fewer than 4 faces, a zero-thickness bounding box, or a volume below
`--min-volume` (default `1` mm³) cannot print, and — worse — when it lands on its own plate
OrcaSlicer silently produces **no g-code for that whole plate** (exit `0`, no error). Every
dropped body is **logged to stderr** with its name, face count, bbox, and volume — it is
never silently truncated. Pass `--min-volume 0` to keep everything.

> Because no headless OrcaSlicer is available, the format is built to match the real Orca
> exporter source but is **best-effort**: verify in your slicer that it opens showing the
> expected number of plates.

## Usage

```bash
3d arrange <input> [options]
```

`<input>` is a single `.3mf`, or one-or-more `.stl` files (do not mix the two).

| Option | Default | What |
|---|---|---|
| `--single` | on | Write ONE multi-plate Orca/Bambu project `.3mf` (all plates in one file) |
| `--per-plate` | off | Write one print-ready `.3mf` per plate (legacy behavior) |
| `--bed MM` | `270` | Square bed size in millimeters (Snapmaker U1) |
| `--gap MM` | `6` | Clearance between part footprints |
| `--margin MM` | `8` | Keep-out margin from each bed edge (usable area = `bed - 2*margin`) |
| `--min-volume MM3` | `1` | Drop (and **log**) split connected bodies below this volume as degenerate slivers; bodies with `<4` faces or a zero-thickness bounding box are always dropped regardless. `0` disables the volume floor. |
| `-o, --out PREFIX` | `<first-input>_arranged` | Output path/prefix. single: `<PREFIX>.3mf`; per-plate: `<PREFIX>_plate1.3mf`, `_plate2.3mf`, ... |
| `--json` | off | Emit a machine-readable plan + verification instead of a table |

## Examples

```bash
3d arrange assembly.3mf
3d arrange assembly.3mf --per-plate -o out/tray
3d arrange assembly.3mf --bed 270 --gap 6 --margin 8 -o sign.3mf
3d arrange a.stl b.stl c.stl --bed 220
3d arrange assembly.3mf --json | jq '.plates | length'
```

## Exit codes

- `0` - plates written and verified (object count + bed fit).
- `1` - a single part is larger than the usable plate area; the message names the part and
  its size. Lower `--margin`, raise `--bed`, or split / reorient that part.
- `2` - usage or IO error (no input, unreadable file, unsupported extension, mixed inputs).
- `127` - no python runtime / trimesh available (resolved via the `.venv` / `uv` / system tiers).

The packer is a shelf / next-fit-decreasing-height layout (sort by footprint depth, fill a
row left-to-right, drop to a new shelf, start a new plate when the bed is full). It is a
bounded layout, not an optimal nesting solver.
