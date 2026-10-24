# `3d photo`

Photos of makes and designs in the local [library database](../contracts/library-db.md): add a file, **capture
the table photo of a finished print with the bed lifted to a comfortable viewing height**, register the project's
renders, list. Every path goes through the `photo` action of `lib/library/actions.py` — the same door as the TUI
and the publish flow — and the photos join the listing of `3d models publish` (umbrella #570, slice #579).

## Usage

```bash
3d photo add FILE (--make ID | --design NAME) [--kind table|bed|render] [--z-mm Z] [--db PATH] [--json]
3d photo capture --make ID [--printer P] [--lift auto|MM] [--camera URL] [--part-height MM] [--out FILE] [--yes] [--json]
3d photo renders PROJECT_DIR --design NAME [--yes] [--json]
3d photo list [--make ID | --design NAME] [--db PATH] [--json]
```

## `capture` — the table photo with a lifted bed

After a print the Snapmaker U1 rests at Z 200, where its camera sees 15 % of the plate. `capture` reuses the guarded
Z move of `3d printer bed status --best-view` (`lib/bed_view.py`): it raises the bed to a viewing height, takes a
**fresh** camera frame, puts the bed back and stores `photo(kind=table, z_mm)` for the make.

- **Dry run by default.** Without `--yes` the printer is only *read*: the planned lift is reported, nothing moves,
  no frame is taken, no row is written.
- `--lift MM` is the toolhead Z to photograph at; `--lift auto` (default) picks, from the printer's camera
  calibration, the Z inside the allowed range that shows the plate best (nearest to where the bed is). A printer
  without a calibration refuses `auto` — it never guesses; give `--lift MM`.
- **Limits.** Allowed Z is `floor .. ceiling`: floor = the height of the tallest thing on the bed (`--part-height`,
  `watch.bed.auto_view_part_mm`, or the last job's `object_height`) + 15 mm clearance; ceiling = the printer's
  `resume.z_max_mm` − 5. An unknown part height or a lift outside the range is refused (exit 1), nothing moves.
- **Guards.** Refused (exit 1, nothing moved, the camera is not asked) while the printer is printing, paused or
  busy, when Klipper is not ready, or when Z is not homed (the check never homes). The move is `G90`, `G1 Z.. F600`,
  `M400` only, re-checked against a fresh printer read right before each send.
- **Restore.** The bed goes back to where it was in a `finally` (also after a camera error or SIGTERM); a marker
  file lets the next bed check undo an interrupted run. A restore that is refused (a print started meanwhile) is
  reported: the photo is kept, the result is `ok: false` with code `bed_not_restored`, exit 1.
- A camera that returns the same frame as before the move yields a `stale_frame` warning (`fresh: false`).

Real moves need `--yes`; the agent that wrote this slice never ran one: the lift is verified against a simulated
Moonraker (see Tests below) and still needs a live check ([#565](https://github.com/alex-mextner/3d-cli/issues/565)).

## Subcommands

| Subcommand | What |
|---|---|
| `add` | Register an existing photo of a make or a design. Example: `3d photo add shots/mole_table.jpg --make 0199a1c2-7d3e --kind table --z-mm 70` |
| `capture` | The table photo described above. Example: `3d photo capture --make 0199a1c2-7d3e --lift 70 --part-height 40 --yes` |
| `renders` | Register the project's `previews/*.png\|jpg` as `photo(kind=render)` of the design; dry run lists them, `--yes` registers (idempotent). Example: `3d photo renders projects/mole --design mole --yes` |
| `list` | The photos of a make, of a design (with its makes), or all. Example: `3d photo list --design mole --json` |

## Options

| Option | What |
|---|---|
| `--make ID` | The make (full uid or a unique prefix of >= 8 characters). Example: `3d photo list --make 0199a1c2-7d3e` |
| `--design NAME` | The design (name or uid). Example: `3d photo add mole.png --design mole --kind render` |
| `--kind K` | `table` (default), `bed`, `render`. Example: `3d photo add bed.jpg --make 0199a1c2-7d3e --kind bed` |
| `--z-mm Z` | The bed Z (mm) the photo was taken at. Example: `3d photo add t.jpg --make 0199a1c2-7d3e --z-mm 70` |
| `--printer P` | `capture`: the printer (default: the make's). Example: `3d photo capture --make 0199a1c2-7d3e --printer k1se` |
| `--lift L` | `capture`: `auto` (default) or the toolhead Z in mm. Example: `3d photo capture --make 0199a1c2-7d3e --lift 70` |
| `--camera URL` | `capture`: snapshot URL/path instead of the printer's configured camera. Example: `3d photo capture --make 0199a1c2-7d3e --camera /server/files/camera/monitor.jpg` |
| `--part-height MM` | `capture`: the height of the tallest thing on the bed (the floor of the lift). Example: `3d photo capture --make 0199a1c2-7d3e --part-height 40` |
| `--out FILE` | `capture`: where to save the frame (default `<data dir>/library/photos/<make>/<time>-table.jpg`). Example: `3d photo capture --make 0199a1c2-7d3e --out /tmp/table.jpg --yes` |
| `--yes` | `capture`: really move the bed and take the photo; `renders`: register. Example: `3d photo renders projects/mole --design mole --yes` |
| `--db PATH` | Library database file. Example: `3d photo list --db /tmp/library.db` |
| `--json` | The `3d.action/1` result of the photo action (`list`: `3d.photo/1`). Example: `3d photo capture --make 0199a1c2-7d3e --json` |

## Exit codes

`0` ok; `1` a guard refused the lift (printer busy, not homed, outside the Z range, unknown part height), the bed
could not be restored, or the camera gave no frame; `2` bad usage or an unknown make/design; `127` missing dependency.

## Tests

`tests/test_bed_photo.py` (guards, limits, restore after an error, restore refused when a print started, dry run
sends nothing, `auto` picking) and `tests/e2e/test_publish_loop_workflows.py` (the real `bin/3d` against
`tests/photo_fixtures.py`, a loopback Moonraker that simulates the Z axis and serves a changing camera snapshot).
