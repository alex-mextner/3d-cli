# `3d gcode-thumbnails` — the preview a printer screen shows for a G-code

A printer screen (Snapmaker U1, any Klipper/Moonraker host, Creality K1 stock firmware) shows a job's picture from base64 image blocks in the G-code header. Orca's GUI writes them; **the Orca-family CLI does not**: `--slice` calls `export_gcode(outfile, gcode_result, nullptr)` (Snapmaker Orca `src/Snapmaker_Orca.cpp:5185`, upstream OrcaSlicer `src/OrcaSlicer.cpp:7048`), and `GCode::_do_export` writes `THUMBNAIL_BLOCK`s only when that thumbnail callback is set. The profile's `thumbnails = 48x48/PNG, 300x300/PNG` is in the file's config block, the images are not.

## Usage

```
3d gcode-thumbnails check <file.gcode|file.3mf>... [--json]
3d gcode-thumbnails add   <file.gcode>... [--image PNG] [--size WxH/FMT]... [--out FILE] [--replace] [--preview PNG] [--3mf X.3mf [--plate N]] [--json]
3d gcode-thumbnails add   <file.3mf>... [--image PNG] [--json]
```

You rarely need to run this yourself: every path that produces or sends a G-code adds the thumbnails on its own (see [Layers](#layers-a-thumbnail-cannot-be-missed)).

| Subcommand / option | What |
|---|---|
| `check` | PASS/FAIL per file. PASS = every size of the profile's `; thumbnails =` is present as a valid block in the first MiB (what Moonraker reads). No spec in the file: 48x48, 96x96 and 300x300 PNG are required. A `.3mf`: every plate has `Metadata/plate_N.png` and `plate_N_small.png`. Exit 1 on any FAIL. |
| `add` | Insert the missing sizes. The file is rewritten atomically with its mode and mtime kept (a staleness check "G-code older than its project" must not be fooled). A file that already passes is left byte-identical. |
| `--image PNG` | Use this picture (e.g. a plate render; transparent margins trimmed) instead of drawing one from the G-code. Needs Pillow. |
| `--size WxH[/FMT]` | Sizes added on top of the required ones (repeatable). 1..4096 px per side, otherwise exit 2 (also applies to the profile's own spec). |
| `--3mf X.3mf [--plate N]` | Also add the missing `Metadata/plate_N.png` (512 px) and `plate_N_small.png` (128 px) to this project, drawn from the same render (written atomically, the project's mtime kept so it does not look newer than the G-code). Plate 1 also gets the package thumbnail relationship. |
| `add X.3mf` | A `.gcode.3mf` / sliced project: the missing plate pictures, drawn from each embedded `Metadata/plate_N.gcode` (or from `--image`). |
| `--out FILE` | Write the result to `FILE`, leave the input untouched (one input only). |
| `--preview PNG` | Also save the full-size G-code render. |
| `--replace` | Redraw every size, dropping the thumbnail blocks already in the head (e.g. after a better `--image`). |
| `--json` | List of `{path, ok, reason, required, present, missing, added}`. |

```bash
3d gcode-thumbnails check print/*.gcode            # which files would show a blank preview
3d gcode-thumbnails add print/u1_trees.gcode        # profile sizes, drawn from the toolpaths
3d gcode-thumbnails add plate.gcode --image plate.png
3d gcode-thumbnails add other.gcode --size 300x300/PNG --size 32x32/PNG
```

## Layers: a thumbnail cannot be missing

The Orca CLI writes no thumbnails, so nothing may depend on somebody remembering `add`. Five independent layers; each one alone gets a bare G-code to all required sizes, and none changes a G-code that already has them (it stays byte-identical; a block that is there is never duplicated or redrawn). The code is `lib/slicing/thumbnails.py`.

| Layer | Where | What it does |
|---|---|---|
| (a) ask the slicer | `slice_project` / `slice_stl_with_profiles` (`3d prep`, calibration), `3d slice` of a `.3mf`, `3d retarget --gcode`, PrusaSlicer via `--thumbnails 48x48,96x96,300x300` | The slicer's `thumbnails` setting also asks for 48x48, 96x96 and 300x300 PNG on top of what the preset names (the preset's own sizes and string/list shape are kept; a copy is slicing-ready, the user's file is never rewritten). A slicer that writes thumbnails (Orca GUI, PrusaSlicer, Bambu Studio) does; for every other one the config block names the sizes the checks require. |
| (b) draw after the slice | `3d slice`, `3d prep`, `3d retarget --gcode`, `3d calibrate gen` (all kinds), `3d gcode-resume` (its output copies the head of a G-code that may have had none) | Before the G-code is hashed, moved or recorded, the missing sizes are drawn from the toolpaths and inserted. A `.gcode.3mf` / `sliced.3mf` gets its missing plate pictures from the same render. A G-code that extrudes nothing visible (console macros) gets a neutral grey tile. A failure is a failed slice (`3d slice` exits 1, `3d prep` reports `thumbnails`) and names the repair command. `--dry-run` keeps no G-code and renders nothing. |
| (c) before upload / start | `3d printer upload`, `3d printer start` | The missing sizes are added to FILE **in place** (atomic, mode and mtime kept, a current slice-warnings record follows the new SHA-256) before the snapshot is hashed; the old and new SHA-256 are printed. The remote name is content-addressed, so the fixed bytes have a name of their own: a `start` after such a fix finds no stored copy and asks you to upload again. |
| (d) preflight | `3d print-preflight` | FAIL on a missing size, naming the exact command (`3d gcode-thumbnails add 'FILE'`). `--fix` runs it before the checks (the one option that writes). |
| (e) the gate | `tests/test_thumbnail_guard.py` (part of `dev run test` and the pre-commit fast gate) | Fails when a module of `lib/` runs a slicer (`--slice`, `--outputdir`, `-g --output`) without calling a layer-(b) function, unless it is listed as exempt with a reason (`slice-check` discards its G-code; the mix tester only reads a probe project back). |

Not covered by (b): the U1 calibration plate generator (`calibration_plates/u1_underlay.py`, [#724](https://github.com/alex-mextner/3d-cli/issues/724): the module is over the repo's lint file-length gate and cannot be committed until it is split; its G-code gets thumbnails from layers (c) and (d)), and a G-code that some other program writes straight to disk, outside `lib/`. Layers (c) and (d) catch it before it reaches a printer. A `3d mix tester` project is sliced in the Orca GUI, which writes its own thumbnails.

## Format written

Exactly Orca's own block (GCode/Thumbnails.hpp), right after `; HEADER_BLOCK_END` and its blank line (after existing thumbnail blocks, if any; at the file start when there is no header) — always before the first move:

```
; THUMBNAIL_BLOCK_START

;
; thumbnail begin 300x300 31428
; iVBORw0KGgoAAAANSUhEUgAAASwAAAEsCAYAAAB5fY51AABb2klEQVR4Ae29CVsk15Utiooq5hkSyA
; …                     (78 base64 characters per line)
; thumbnail end
; THUMBNAIL_BLOCK_END
```

PNG uses the tag `thumbnail`, JPG `thumbnail_JPG` (on a white background). QOI, BTT_TFT and ColPic are refused with an error. Moonraker validates the `LEN` field against the data and makes its own 32×32 (the Snapmaker U1 build also lists a 96×96) from the largest image.

Adding thumbnails changes the file's SHA-256 but not its toolpath: a `NAME.slice-warnings.json` ([`3d slice-warnings`](slice-warnings.md)) that matched the G-code before is rewritten for the new hash (with `--out`, written next to the output) and notes the step under `post_processed`. A stale or absent record is left alone.

## The picture drawn from the G-code

Without `--image`, every visible extrusion move (walls, surfaces, bridges, brim, support; not sparse/internal infill, prime tower, skirt or custom start G-code) is drawn in a true isometric view from the front right, z-buffered, in each tool's `filament_colour`, shaded from screen-space normals with darkened silhouette edges, on a transparent background — what the file actually prints, at about 10 s and 1.5 GB for a 115 MB U1 G-code. The picture is drawn at 2× the biggest thumbnail (supersampling) but at most 2048 px per side (about 4 Mpx, a bounded memory cost); a thumbnail above 1024 px is scaled up from that picture. Use `--image` for more detail.

## Printer preflights

`lib/gcode_thumbnails.py` is importable: `check(path) -> ThumbCheck(ok, required, present, missing, reason)` (stdlib only, raises `InputNotFound` for a missing file) and `ensure(path, image=None, sizes=None, out=None) -> ThumbCheck` (numpy). `lib/slicing/thumbnails.py` has the layer entry points: `finalize_slice(gcode, three_mf=None)` / `finalize_gcode` / `finalize_workdir` (b), `fix_in_place` (c), `spec_for` / `with_project_spec` / `with_machine_spec` (a).
