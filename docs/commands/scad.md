# `3d scad` — plain [OpenSCAD](GLOSSARY.md#openscad) tools

Eight small subcommands that drive the `openscad` binary directly: syntax check, previews, STL export with a geometry check, customizer-parameter listing, cross-sections and version numbering. They replace the standalone `validate.sh` / `preview.sh` / `multi-preview.sh` / `export-stl.sh` / `extract-params.sh` / `section.sh` / `section-color.sh` / `version-scad.sh` shell scripts with one tested implementation.

**Why it exists.** The CLI finds `openscad` (PATH, `$OPENSCAD`, macOS app bundles) and exports `OPENSCADPATH` (BOSL2, NopSCADlib) itself, so a model with `include <BOSL2/std.scad>` renders from any directory with no setup. OpenSCAD exits **0** when an `assert()` fails or an include is missing — it only prints `ERROR: Assertion ...` — so every subcommand scans the output for `ERROR:` / `Assertion` and exits 1 on a hit.

## Usage

```
3d scad validate <in.scad>
3d scad preview <in.scad> <out.png> [--camera=CAM] [--size=WxH] [--render] [-D 'var=val' ...]
3d scad multi-preview <in.scad> <out_dir/> [--render] [-D 'var=val' ...]
3d scad export-stl <in.scad> <out.stl> [--ascii] [-D 'var=val' ...]
3d scad params <in.scad> [--json]
3d scad section <in.scad> <out.png> --module='name()' [--plane=YZ|XZ|XY] [--keep=neg|pos] [--size=WxH] [-D 'var=val' ...]
3d scad section-color <assembly.scad> <out.png> [--plane=YZ|XZ|XY] [--keep=neg|pos] [--center=X,Y,Z] [--dist=N] [--size=WxH] [-D 'var=val' ...]
3d scad next-version <model-name>
```

Options are accepted as `--opt=value` or `--opt value`; `-D 'var=val'` (or `-Dvar=val`) is repeatable.

| Option | Meaning |
|---|---|
| `-D 'var=val'` | pass-through OpenSCAD define |
| `--render` | force CGAL/manifold render (no throwntogether cut-face artifacts) |
| `--size=WxH` | image size (preview / multi-preview 800x600, section 1200x900) |
| `--camera=CAM` | 7 values `tx,ty,tz,rx,ry,rz,dist` (gimbal) or 6 values `ex,ey,ez,cx,cy,cz` (VECTOR eye + center). Default `0,0,0,55,0,25,0`; previews pass `--autocenter --viewall`, so dist `0` still frames the model |
| `--ascii` | export-stl: ASCII STL instead of binary |

## Subcommands

- **`validate`** — runs OpenSCAD to an `echo` export (no geometry render), prints `✓ Syntax OK` plus any `echo()` output, or `✗ Validation failed` and the output (exit 1).
- **`preview`** — one PNG, `Tomorrow Night` colour scheme. Isometric by default. Common cameras: front `0,0,0,90,0,0,200`, top `0,0,0,0,0,0,200`.
- **`multi-preview`** — six PNGs `<name>_{iso,front,back,left,right,top}.png` in the output directory.
- **`export-stl`** — exports and checks the geometry. Prints the size, triangle count (binary STL) and a status line: `STATUS: PASSED — manifold, no self-intersections, ready for slicer`, or `STATUS: FAILED` with the cause. It exits 1 on any `ERROR:` / `Assertion`, on non-manifold or self-intersecting geometry, or when no STL was written. Degenerate faces are a warning only. A stale output file is removed first, so an old STL never passes for the new run.
- **`params`** — lists top-level `name = value; // [min:max] description` assignments (brace depth 0): type, range (`[min:max]` / `[min:step:max]`), options (`[a, b, c]`) and description. `--json` prints a JSON array. (`3d params` is the richer Customizer command; this is the script-compatible listing.)
- **`section`** — a mono cross-section of ONE module. It writes a unique `.section_tmp_*.scad` **next to the input** (`use <in.scad>` then `difference(){ module; cut-box; }`), renders with `--render` and a 6-param vector camera, and always deletes the temp file. `use`/`include` resolve against the file's own directory, not the cwd — that is why the temp file sits beside the model. `-D` overrides reach only variables the wrapper itself defines, not the `use`d model's own top-level variables (OpenSCAD limitation). For assemblies with top-level geometry, add a `cut` `-D` parameter or use `section-color`.
- **`section-color`** — a per-part coloured cross-section of an assembly; see [scad-section-color.md](scad-section-color.md).
- **`next-version`** — looks for `<name>_NNN.scad` in the current directory and prints the next free number (`Create: bracket_003.scad`).

| `--plane` / `--keep` | removes | `section` eye on |
|---|---|---|
| `YZ` neg (default) | +X half | +X |
| `XZ` neg | +Y half | +Y |
| `XY` neg | +Z half | +Z |

`--keep=pos` removes the negative half and flips the eye.

## Examples

```bash
3d scad export-stl box.scad box.stl -D 'width=80' -D 'height=60'
3d scad preview box.scad box.png --camera=0,0,0,90,0,0,0 --render
3d scad multi-preview box.scad previews/
3d scad section part.scad cut.png --module='shaft()' --plane=XZ --keep=pos
3d scad params box.scad --json | jq '.[].name'
3d scad next-version bracket
```

## Exit codes

`0` success · `1` OpenSCAD error / assert failure / geometry check failed · `2` bad arguments or missing input file · `127` OpenSCAD not installed.
