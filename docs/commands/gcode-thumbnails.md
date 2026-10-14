# `3d gcode-thumbnails` — the preview a printer screen shows for a G-code

A printer screen (Snapmaker U1, any Klipper/Moonraker host, Creality K1 stock firmware) shows a job's picture from base64 image blocks in the G-code header. Orca's GUI writes them; **the Orca-family CLI does not**: `--slice` calls `export_gcode(outfile, gcode_result, nullptr)` (Snapmaker Orca `src/Snapmaker_Orca.cpp:5185`, upstream OrcaSlicer `src/OrcaSlicer.cpp:7048`), and `GCode::_do_export` writes `THUMBNAIL_BLOCK`s only when that thumbnail callback is set. The profile's `thumbnails = 48x48/PNG, 300x300/PNG` is in the file's config block, the images are not.

## Usage

```
3d gcode-thumbnails check <file.gcode>... [--json]
3d gcode-thumbnails add   <file.gcode>... [--image PNG] [--size WxH/FMT]... [--out FILE] [--replace] [--preview PNG] [--json]
```

| Subcommand / option | What |
|---|---|
| `check` | PASS/FAIL per file. PASS = every size of the profile's `; thumbnails =` is present as a valid block in the first MiB (what Moonraker reads). No spec in the file: PASS when at least one thumbnail is present. Exit 1 on any FAIL. |
| `add` | Insert the missing sizes. The file is rewritten atomically with its mode and mtime kept (a staleness check "G-code older than its project" must not be fooled). A file that already passes is left byte-identical. |
| `--image PNG` | Use this picture (e.g. a plate render; transparent margins trimmed) instead of drawing one from the G-code. Needs Pillow. |
| `--size WxH[/FMT]` | Sizes for a G-code without a `; thumbnails =` spec (repeatable). |
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

Without `--image`, every visible extrusion move (walls, surfaces, bridges, brim, support; not sparse/internal infill, prime tower, skirt or custom start G-code) is drawn in a true isometric view from the front right, z-buffered, in each tool's `filament_colour`, shaded from screen-space normals with darkened silhouette edges, on a transparent background — what the file actually prints, at about 10 s and 1.5 GB for a 115 MB U1 G-code.

## Printer preflights

`lib/gcode_thumbnails.py` is importable: `check(path) -> ThumbCheck(ok, required, present, missing, reason)` (stdlib only, raises `InputNotFound` for a missing file) and `ensure(path, image=None, sizes=None, out=None) -> ThumbCheck` (numpy).
