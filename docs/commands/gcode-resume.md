# `3d gcode-resume` — continue a stopped print above the last intact layer

A multi-head print stopped (a head failed, a pause went wrong) and the part is still on the bed.
`3d gcode-resume` writes a new G-code from the original: the original header, a resume header,
and the original from a chosen layer on. It never contacts a printer.
Issue: [#138](https://github.com/alex-mextner/3d-cli/issues/138).

Z cannot be homed: on the Snapmaker U1 `G28 Z` probes the bed with the nozzle, which the part
forbids. So the printer must still hold the X/Y/Z homing, bed mesh and head offsets of the
original job: start the output with `CLEAR_PAUSE`, then `SDCARD_PRINT_FILE`, never after
`CANCEL_PRINT` (its `M84` drops the homing).

## Usage

```
3d gcode-resume FILE.gcode --where BYTE
3d gcode-resume FILE.gcode --table FIRST:LAST
3d gcode-resume FILE.gcode --printer NAME (--top-z Z | --layer N) [--paused-at BYTE] [--tower-top Z] [--no-tower] [--home-xy] [-o OUT]
```

- `--where BYTE` — line, layer, active head, target temperatures and per-head retraction at a
  byte offset (Klipper `virtual_sdcard.file_position` at the pause).
- `--table FIRST:LAST` — per layer: Z, line, byte, heads, objects and slicer minutes left.
- `--top-z Z` — the measured G-code Z of the top of the last intact layer; the resume starts at
  the first layer above it. `--layer N` resumes at layer N instead.
- `--printer NAME` — required to build: its `resume:` block in `printer-devices.yaml`.
- `--paused-at BYTE` — lifts the travel Z above everything printed before the pause and
  refuses a resume layer above the paused one; the paused layer's Z is the old tower top.
- `--tower-top Z` — height of the old prime tower still on the bed (default: Z of the
  `--paused-at` layer). One of the two is required when the plate has a prime tower.
- `--no-tower` — drops the prime tower; every tool change purges off the bed instead.
- `--home-xy` — adds `G28 X Y`, only if X/Y lost their homing (Z keeps its homing and mesh).
- `-o, --out FILE` — default `<FILE>_resume_L<N>.gcode` next to the input.

Exit: 0 written / reported, 1 safety check failed (Z outside `0..z_max_mm-1`, an extrusion over
the old tower; nothing written), 2 usage error, bad G-code, unknown printer or missing file.

## The old prime tower (issue #238)

A resume below the top of the old prime tower must not print the tower again: it would run into
the one still standing (2026-10-14: 41 layers of tower moves into it, spaghetti). Layers at or
below the old tower top print as with `--no-tower` — every `T<n>` purges off the bed, the tower
moves are dropped, and every tool change and every travel from an unknown point or across the
tower area is lifted to the tower top + 2 mm first. Above it the tower continues as sliced.
The tower area is found from every prime-tower extrusion, including Orca's one-axis moves and
G2/G3 arcs. A travel arc whose start the resume moved becomes a straight `G1` (its I/J centre
would shift); such an extruding arc is refused.

A final pass over the finished file tracks the absolute Z of every move (G90/G91, G92) and
refuses the whole file if any Z leaves `0..z_max_mm - 1` — e.g. the old tower near the Z ceiling
plus the slicer's 1.5 mm tool-change hop.

## What the resume header does

1. Bed back on at once (`SDCARD_PRINT_FILE` ran `TURN_OFF_HEATERS`), heads to print temperature.
2. `G1 Z` above the resume layer + 0.6 mm, the highest Z before the pause and the old tower top
   + 2 mm, plus `clearance_mm` (capped at `z_max_mm - 1`), before any X/Y move, tool change or
   purge (`T<n>` moves X/Y only at the current Z).
3. Bed wait, `SET_PRINT_STATS_INFO`, the profile's `start_commands`.
4. Every head the rest of the job uses: wait for temperature, pick it, purge off the bed with
   `<purge_macro> INDEX=n FORCE=1 TEMP=t LENGTH=purge_mm` — never `RETRACT_LENGTH` (the U1 macro
   does `| abs` on the raw string and cancels the job); the macro's default leaves the head
   `purge_retract_mm` back. Then leave it exactly as retracted as the original left it at the
   cut. The active head is picked last.
5. Pressure advance, fans, acceleration (`default_accel` when the plate set none), velocity,
   feed, M73, then the first X/Y/Z of the kept body.

The body drops the cut layer's preamble (it closes the object of the layer below and wipes it;
`EXCLUDE_OBJECT_END` with no object open would cancel the print), keeps each head's retraction
exact across tool changes, and rewrites the filament/time statistics for what is left
(off-bed purges included, so preflight sees every head the file uses). Progress (`M73 P`) is
rescaled to this file: `P0` at the start, `P = 100·(1 − R/R_cut)` after, `P100 R0` at the end, so
the printer and dashboards do not show the original's percentage from minute one.

## `printer-devices.yaml` `resume:` block

All keys are required (see [`3d printer`](printer.md) for the file):

```yaml
u1:
  adapter: moonraker
  host: 192.168.0.26
  model: Snapmaker U1
  resume:                   # Snapmaker/u1-klipper@10f2f69 macros
    purge_macro: SM_PRINT_PREEXTRUDE_FILAMENT  # INDEX=n FORCE=1 TEMP LENGTH: off-bed purge
    purge_mm: 30            # header purge per head
    purge_retract_mm: 0.5   # the macro's default RETRACT_LENGTH (never passed)
    no_tower_prime_mm: 12   # --no-tower: off-bed prime per tool change on top of the un-retract
    clearance_mm: 5         # above everything printed before any X/Y move of the header
    tower_margin_mm: 3      # margin around the (old) prime tower
    filament_diameter_mm: 1.75
    min_print_temp: 170     # an M109 at or above this is a print temperature
    default_accel: 10000    # M204 S when the plate set none before the cut
    z_max_mm: 275           # stepper_z.position_max; the output stays 1 mm below it
    start_commands: [DEFECT_DETECTION_START]
```

On the U1 the purge macro with `FORCE=1` also sets the once-per-job flag, so the original
per-change purge lines stay no-ops.

## Assumptions

Orca / Snapmaker Orca G-code: `;LAYER_CHANGE` `;Z:` … `;AFTER_LAYER_CHANGE` per layer,
`EXCLUDE_OBJECT` blocks, `M83` relative E (M82 is refused), absolute X/Y and I/J arcs
(relative X/Y under `G91`, radius-form `G2/G3 R` arcs and `G92 X/Y/Z` offsets are refused: the
tower, arc and Z checks could not track them), bare `T<n>` tool changes,
`; EXECUTABLE_BLOCK_START/END`, statistics before `; CONFIG_BLOCK_START`. Logical tools map 1:1
to heads, as in the original job.

## Examples

```
3d gcode-resume print/plate.gcode --where 41236998
3d gcode-resume print/plate.gcode --table 840:860
3d gcode-resume print/plate.gcode --printer u1 --top-z 170.2 --paused-at 41236998
3d gcode-resume print/plate.gcode --printer u1 --layer 817 --tower-top 103.7 --no-tower -o print/plate_resume.gcode
```
