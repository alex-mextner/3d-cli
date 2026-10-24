# `3d retarget` — move a slicer project 3MF to another printer

Takes an Orca / Snapmaker Orca / Bambu Studio project `.3mf` made for printer A and writes a
`.3mf` that is ready for printer B in about a second. The orientation, supports and tuned
settings already in the file are kept; only what the target printer forces changes. It never
touches a printer.
Issue: [#471](https://github.com/alex-mextner/3d-cli/issues/471).

Why: a Snapmaker U1 project of a spoon (oriented, supports set) already existed, yet printing it
on a Creality K1 SE took over an hour of agent time because nothing could retarget a 3MF, so the
orientation and supports were re-derived. See
[`docs/notes/2026-10-22-slow-spoon-k1se-analysis.md`](../notes/2026-10-22-slow-spoon-k1se-analysis.md).
When a ready project exists: **patch it (`3d retarget`), do not re-slice from the mesh.**

## Usage

```
3d retarget <project.3mf> --to PRINTER [options]
```

| Option | Default | What |
|---|---|---|
| `--to PRINTER` | required | Target **machine preset**: exact name or a fuzzy fragment (`k1se` finds `Creality K1 SE 0.4 nozzle - Copy`). The preset's `inherits` chain is resolved, so bed, start/end G-code and limits are the real ones. |
| `-o, --out PATH` | `<project>.<printer>.3mf` next to the input | Output `.3mf`. |
| `--keep N[,N...]` | all | Keep only these objects (1-based, the numbering the command prints). Dropped objects, their mesh parts, settings, variable layer-height lines and plate instances are removed. |
| `--margin MM` | `5` | Clearance from every bed edge. The brim/skirt is not added for you: raise it when printing with a wide brim. |
| `--process NAME` | keep the source's | Also apply a process (print) preset, e.g. `"0.20mm Standard @Creality K1 SE"`. |
| `--filament NAME` | keep the source's | Also apply a filament preset, e.g. `"Orange PLA @K1SE"`. |
| `--preset NAME` | — | Named setting set, repeatable. `easy-release-supports`: same-material supports that peel off — `enable_support=1`, `support_top_z_distance=0.2`, `support_bottom_z_distance=0.2`, `support_interface_top_layers=2`, `support_interface_bottom_layers=0`, `support_interface_spacing=0.5`, `support_interface_pattern=rectilinear`. |
| `--set KEY=VALUE` | — | Override one project setting, repeatable, applied after presets. An unknown KEY is an error with close matches (the slicer would silently ignore it). A list key takes the value in every slot; use JSON for explicit lists: `--set 'nozzle_temperature=["215"]'`. |
| `--preset-dir DIR` | — | Extra preset directory (repeatable) holding `machine/ process/ filament/ *.json`, searched before everything else. |
| `--gcode PATH` | — | Also slice the result to `PATH` with the installed Orca-family slicer (placed objects as an STL + the retargeted settings as presets; see below), then run `3d gcode-bounds` on it. |
| `--bounds-printer NAME` | — | Fleet printer for that `gcode-bounds` run (axis limits, trusted macros), e.g. `k1se`. |
| `--include-non-printable` | off | Keep objects tagged as vitamins / reference models / stand-ins (`3d:role`, written by [`3d assembly position`](assembly.md)); without it a project that keeps one is refused (`NonPrintableParts`, exit 1). The override is logged. Example: `3d retarget fit-test.3mf --to k1se --include-non-printable`. |
| `--dry-run` | off | Do everything but write the `.3mf`; same report and exit code. |
| `-v, --verbose` | off | List every changed key instead of the first few per group. |

```bash
3d retarget spoon.3mf --to k1se --keep 1 --preset easy-release-supports -o spoon.k1se.3mf
3d retarget spoon.3mf --to k1se --keep 1 --preset easy-release-supports --set support_interface_spacing=0.4
3d retarget part.3mf --to "Creality K1 SE 0.4 nozzle" --process "0.20mm Standard @Creality K1 SE" --filament "Orange PLA @K1SE"
3d retarget part.3mf --to k1se --margin 8 --preset-dir ./my-presets
3d retarget part.3mf --to k1se --keep 1 --gcode part.gcode --bounds-printer k1se
3d retarget big.3mf --to k1se --dry-run          # exit 1 and the footprint-vs-bed numbers if it cannot fit
```

## What it changes

1. **Printer keys** — every machine-scope key the target preset defines (`printable_area`,
   `printable_height`, `printer_model`, `printer_settings_id`, nozzle, start/end G-code, limits,
   retraction, ...) replaces the source's. G-code templates the target does not define
   (`before_layer_change_gcode`, `time_lapse_gcode`, ...) are **cleared**, so a macro of the source
   printer (a U1's `DEFECT_DETECTION_DETECT`) cannot reach a printer that lacks it. Which keys are
   machine / filament keys: `lib/data/orca-setting-scopes.json`.
2. **One extruder, one filament** — for a single-extruder target every per-extruder and
   per-filament list shrinks to one entry (the kept object's filament slot), slot assignments
   (`support_filament`, per-object `extruder`, `filament_maps`) become 1, the prime tower is
   switched off, and per-triangle colour paint marks are removed. A multi-extruder target keeps the
   filaments (refused if the project has more filaments than the target has extruders).
3. **Objects** — `--keep` selects them; the kept ones move together (relative layout preserved) so
   their footprint's convex hull is centred in the bed with the margin. The original orientation is
   kept when it fits; otherwise the smallest Z rotation that fits is used (angles are scanned every
   0.5° plus the hull-edge angles). All kept objects must sit on one plate (else exit 2); they end up on
   plate 1, and surviving instances of an object are renumbered from 0. **Z-height / tilt is
   never changed.** Part taller than `printable_height`, or a footprint that fits at no Z rotation,
   is **exit 1** with the numbers: footprint at the closest rotation, usable area, and the largest
   `--margin` that would fit.
4. **Presets and overrides** — `--process`, `--filament`, then `--preset`, then `--set`. Without
   `--process` / `--filament` the source's process and filament settings stay (a note says so);
   `print_compatible_printers` is cleared so the slicer does not mark the project incompatible.
5. **Stale previews and G-code** — `Metadata/plate_*.png|json`, `top_*`, `pick_*` show the old layout and are
   dropped; the slicer redraws them on open. G-code parts of a sliced project
   (`Metadata/plate_N.gcode[.md5]`) were sliced for the source printer and are dropped too. A variable layer-height profile follows its object
   (renumbered) and is called out, because it overrides `layer_height`.

## Validation and report

After writing, the output is read back and checked: every XML part is well-formed, the settings are
valid JSON with the required keys, `printable_area` equals the target bed, one extruder/filament
for a single-extruder target, every build item has its mesh part, and the **world-space footprint
and top of every build item are inside the bed and `printable_height`**. A failed check prints each
problem, removes the output and exits 1.

The report lists the presets used (with where they were found), each object kept/dropped, the fit
(rotation, move, footprint vs bed), every changed key grouped by origin (`machine`, `filament`,
`process`, `collapse`, `preset NAME`, `--set`) as `key: old -> new`, notes, and the elapsed time.

**`--gcode`.** The Snapmaker Orca CLI (`01.10.01.50` here) exits 127 on a *project* 3MF — the untouched
U1 source and the retargeted file alike ([`slice-check`](slice-check.md#slicer-quirks-verified-on-macos-2026-06)
documents it) — but slices an STL given `--load-settings` / `--load-filaments`. So `--gcode` exports the
placed printing volumes as a binary STL in bed coordinates, splits the retargeted `project_settings.config`
into machine / process / filament presets (process and filament name the machine as their only compatible
printer), runs `<slicer> --datadir <tmp> --load-settings machine;process --load-filaments filament --arrange 0
--orient 0 --slice 0` (`APPIMAGE_EXTRACT_AND_RUN=1`, `QT_QPA_PLATFORM=offscreen`) and then
[`3d gcode-bounds`](gcode-bounds.md) on the result; `--bounds-printer k1se` gives that run the printer's
axis limits and trusted macros (`START_PRINT`/`END_PRINT` are macros: without it that check says FAIL).
Variable layer-height profiles, per-object settings and modifiers are not carried into this slice: it
uses the project-level settings only. The `.3mf` is written and validated either way.

Only `normal_part` volumes count toward the footprint and height; modifiers, negative parts and
support blockers do not.

Not checked or rewritten: `bed_exclude_area` of the target, brim/skirt extents (use `--margin`), the
`different_settings_to_system` / `inherits_group` lineage keys (they only drive the slicer's
"modified preset" markers), and anything the slicer decides while slicing — run `--gcode` or `3d slice-check` for that.

## Where presets are found

Best first: `--preset-dir DIR`, the repo `profiles/` dir (see [`3d profiles`](profiles.md)), each
slicer's user presets, each slicer's `system/` presets (OrcaSlicer, Snapmaker Orca, Bambu Studio).
Among several matches the better origin wins, then a 0.4 mm nozzle, then the shorter name; the
report lists the others (`also matched: ...`) so a fragment like `k1` that could mean a K1 Max is visible.
The output is written to a staging file, validated, and only then moved onto `-o`, so an existing file
there survives a failed run. A
printer with no preset on this machine (a Bambu A1 without Bambu Studio / OrcaSlicer ever opened)
is an exit-2 error listing what was found: open the slicer once or pass `--preset-dir`.

Exit: `0` ok, `1` does not fit / failed validation / slicing or G-code bounds failed, `2` bad usage
or unknown preset or `--set` key.
