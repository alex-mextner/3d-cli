# `3d arrange` - split parts and pack them onto bed-sized plates

Takes a `.3mf` (parts in their assembled positions) or one-or-more `.stl` files, splits each
object into **connected bodies** so each glyph / loose part packs independently, lays every
part flat (drops its min-Z to 0), and shelf-packs the XY footprints onto square plates that
fit the printer bed. It writes **one print-ready `.3mf` per plate**
(`<prefix>_plate1.3mf`, `_plate2.3mf`, ...), each a `trimesh.Scene` of the placed parts as
**separate objects**, flat on `z = 0`, non-overlapping, centered, and within the bed. Every
written `.3mf` is reloaded to verify its object count and that all geometry stays on the bed.

This solves the common problem where a `.3mf` storing parts in their assembled positions
dumps everything onto **one** plate, which overflows the bed (default `270x270` mm, the
Snapmaker U1).

## Usage

```bash
3d arrange <input> [options]
```

`<input>` is a single `.3mf`, or one-or-more `.stl` files (do not mix the two).

| Option | Default | What |
|---|---|---|
| `--bed MM` | `270` | Square bed size in millimeters (Snapmaker U1) |
| `--gap MM` | `6` | Clearance between part footprints |
| `--margin MM` | `8` | Keep-out margin from each bed edge (usable area = `bed - 2*margin`) |
| `-o, --out PREFIX` | `<first-input>_arranged` | Output prefix; writes `<PREFIX>_plate1.3mf`, `_plate2.3mf`, ... |
| `--json` | off | Emit a machine-readable plan + verification instead of a table |

## Examples

```bash
3d arrange assembly.3mf
3d arrange assembly.3mf --bed 270 --gap 6 --margin 8 -o out/tray
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
