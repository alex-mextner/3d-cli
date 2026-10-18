# `3d 3mf` — edit a project 3MF in a shell pipe

Takes a few parts out of an Orca / Bambu / Snapmaker Orca [project 3MF](GLOSSARY.md#3mf), turns
them, places them on a printer's bed and checks them, one step per command, the 3MF passing from
one command to the next on stdout/stdin. The chain continues into [`3d slice -`](slice.md),
[`3d print-preflight -`](print-preflight.md) and [`3d printer start NAME - --yes`](printer.md):

```bash
3d 3mf pick proj.3mf 'CN,SW*' | 3d 3mf orient --face-down top | 3d 3mf arrange --printer k1se \
  | 3d 3mf check --printer k1se | 3d slice - | 3d print-preflight - --printer k1se \
  | 3d printer start k1se - --yes
```

**Why it exists.** Reprinting two parts of a forty-part project otherwise means the slicer GUI:
copy the project, delete the other objects, rotate, re-arrange, export, upload, start. The pipe
does it in one line, with the 3MF checks and the full print-preflight in between, and the
printer starts only with `--yes`.

**What is kept.** The project's presets (`Metadata/project_settings.config`), filaments, mixed
filaments, per-object extruders and part settings are copied byte for byte. Only the build items,
the objects and plates of `Metadata/model_settings.config`, the object files nobody references any
more and Orca's per-object files (`layer_config_ranges.xml`, `layer_heights_profile.txt`,
`brim_ear_points.txt`, `cut_information.xml`, keyed by object index) change.

## Usage

```
3d 3mf ls [FILE] [--json]
3d 3mf pick [FILE] SELECTORS [-o OUT]
3d 3mf orient [FILE] (--face-down FACE | --rotate X,Y,Z) [--only SELECTORS] [--printer NAME] [-o OUT]
3d 3mf arrange [FILE] [--printer NAME | --bed WxD] [--gap MM] [--margin MM] [-o OUT]
3d 3mf check [FILE] [--printer NAME] [--json] [-o OUT]
```

`FILE` is a path, or `-` / nothing for stdin. The result goes to `-o OUT`, or to stdout when
stdout is not a terminal (`-o -` forces it); a terminal gets an error instead of binary. While
stdout carries the 3MF, every human line goes to stderr. An empty stdin (the command before
failed) is an error that says so, so the chain stops at the first broken step.

| Subcommand | What |
|---|---|
| `ls` | Objects of the build: id, plate, extruder, size, lowest corner, name; the printer preset's model. |
| `pick` | Keep only the matched objects. `SELECTORS`: comma-separated or several arguments; each is an object id (`1002`), a whole name (`'CN — CN'`) or the first word of a name (`CN`), with shell globs (`'SW*'`, `'wall_?'`), case-sensitive. A selector that matches nothing exits 2 and lists every name. |
| `orient` | Turn each object (or the `--only` ones) about its centre and drop it onto the bed (lowest point z = 0). `--face-down top\|bottom\|front\|back\|left\|right`: that side of the part, as it lies now, goes down; `largest`: the largest flat face of its convex hull. `--rotate X,Y,Z`: degrees about X, then Y, then Z. Refused for objects with height range modifiers, a variable layer height profile or brim ears (data in the object's own frame that a turn would move off its geometry). |
| `arrange` | Pack every object onto plate 1 with the shelf packer of [`3d arrange`](arrange.md), centred; the other plates and their files go. Bed: `--bed WxD`, else the project's `printable_area`, else `--printer`'s build volume. Exit 1 naming the parts that do not fit one plate. Objects are moved, never turned. |
| `check` | The 3MF gates below; in a pipe the 3MF is passed on only when nothing FAILs. |

| Option | Default | What |
|---|---|---|
| `-o, --out PATH` | stdout if not a terminal | Write the 3MF to PATH; `-` = stdout. |
| `--printer NAME` | — | A fleet printer (`k1se`, `u1`; [`3d printer list`](printer.md)) or a model of [`3d printers list`](printers.md) (`"Creality K1 SE"`). `orient` and `arrange` refuse (exit 1) a project whose printer preset is another model; `check` FAILs it; `arrange` and `check` take the bed from it when the 3MF has none. |
| `--bed WxD` | — | `arrange`: bed in mm, e.g. `220x220`. |
| `--gap MM` | `6` | `arrange`: clearance between parts. |
| `--margin MM` | `8` | `arrange`: keep-out from each bed edge. |
| `--only SELECTORS` | all | `orient`: turn only these objects. |
| `--json` | — | `ls` / `check`: JSON. `check --json` prints to stdout and passes nothing on unless `-o` is given. |

## `check` gates

| Gate | Status |
|---|---|
| `printer` | FAIL when the project's printer preset (`printer_model`) is not `--printer`'s model; SKIP without `--printer` or without a preset. |
| `plates` | WARN when objects sit on more than one plate (`3d slice` slices each plate; a pipe carries one G-code). |
| `bed fit` | per object: FAIL when it leaves its plate's printable area (objects on plate N are measured from plate N's origin in Orca's plate grid). |
| `height` | per object: FAIL above `printable_height` / the build volume. |
| `on bed` | per object: FAIL below the bed (the slicer cuts it off), WARN floating above it. |
| `overhang` | per object: WARN above 100 mm² of downward faces steeper than 45° from vertical that do not rest on the bed (supports, or another face down). The printed result is checked after slicing by the G-code `overhangs` gate of print-preflight. |

The mixed-part face gate of [#411](https://github.com/alex-mextner/3d-cli/issues/411) joins this
list when `3d mix check` lands.

## Examples

```bash
3d 3mf ls proj.3mf
3d 3mf pick proj.3mf 'CN,SW*' -o two.3mf
3d 3mf pick proj.3mf 1002 | 3d 3mf orient --face-down largest -o sw.3mf
3d 3mf orient two.3mf --rotate 90,0,0 --only CN -o two_turned.3mf
3d 3mf arrange two.3mf --bed 220x220 --gap 4 --margin 10 -o two_k1.3mf
3d 3mf check two_k1.3mf --printer k1se --json
3d 3mf pick proj.3mf 'CN,SW*' | 3d 3mf orient --face-down top | 3d 3mf arrange --printer k1se \
  | 3d 3mf check --printer k1se | 3d slice - | 3d print-preflight - --printer k1se \
  | 3d printer start k1se - --yes
```

Exit codes: `0` ok (`check`: PASS or WARN), `1` a gate FAILs, the parts do not fit or the project
is for another printer, `2` usage, an unreadable 3MF or a selector matching nothing.

Backend: `lib/slicing/project_3mf.py`; stdin/stdout plumbing: `lib/cli/pipe.py`.
