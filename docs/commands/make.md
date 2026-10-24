# `3d make`

Records the prints (**makes**) of a design in the local [library database](../contracts/library-db.md):
`queued` → `printing` → `done` | `failed`. A make is the row that photos, publications and relations hang on
(umbrella #570, slice #579). It writes the local library file only — nothing here touches a printer or a website.

## Usage

```bash
3d make add --design NAME [--source-url URL|MODEL.JSON] [--printer P] [--filament F] [--status S] [--gcode FILE] [--project-dir DIR] [--db PATH] [--dry-run] [--json]
3d make status MAKE STATUS [--db PATH] [--json]
3d make list [--design D] [--db PATH] [--json]
```

## Subcommands

| Subcommand | What |
|---|---|
| `add` | Add a make of design `NAME` (the design is created when new). `--source-url` records the model it was printed from (a marketplace URL or the `model.json` of `3d models download`) as a `model_source` and links `design --derived_from--> source` and `make --make_of--> design`. Example: `3d make add --design mole --printer k1se --filament "Rapid PLA+" --status printing` |
| `status` | Move a make to `queued`, `printing`, `done` or `failed`; `finished_at` is set for `done`/`failed`. `MAKE` is the full uid or a unique prefix of at least 8 characters. Example: `3d make status 0199a1c2-7d3e done` |
| `list` | The makes with status, printer, filament, photo count and source. Example: `3d make list --design mole --json` |

## Options

| Option | What |
|---|---|
| `--design NAME` | The design (created when new); `list`: restrict to it. Example: `3d make add --design mole` |
| `--source-url SRC` | The source model (URL or `model.json`). Example: `3d make add --design mole --source-url https://www.printables.com/model/311253-spoon-drawer-pull` |
| `--printer P` | Printer name as in `3d printers`. Example: `3d make add --design mole --printer k1se` |
| `--filament F` | Filament description. Example: `3d make add --design mole --filament "Rapid PLA+"` |
| `--status S` | `queued` (default), `printing`, `done`, `failed`. Example: `3d make add --design mole --status done` |
| `--gcode FILE` | The sliced file of this print. Example: `3d make add --design mole --gcode print/mole_plate.gcode` |
| `--project-dir DIR` | The design's project directory (used by `3d models offer`). Example: `3d make add --design mole --project-dir projects/mole` |
| `--db PATH` | Library database file. Example: `3d make list --db /tmp/library.db` |
| `--dry-run` | `add`: validate and print the row that would be written; write nothing. Example: `3d make add --design mole --dry-run` |
| `--json` | Machine-readable output, `schema: "3d.make/1"`. Example: `3d make list --json` |

## Exit codes

`0` ok; `2` bad usage or an unknown make/design (the error lists the accepted values).

## Example

```bash
$ 3d make add --design mole --printer k1se --status printing --project-dir projects/mole --json
{ "schema": "3d.make/1", "created": true, "make": "0199a1c2-7d3e-7000-8000-0000000000a1", "status": "printing", ... }
$ 3d make status 0199a1c2-7d3e done
make 0199a1c2-7d3e-7000-8000-0000000000a1 is done
```

See also [`3d photo`](photo.md) (photos of a make), [`3d models publish|offer`](models.md) (publishing it) and
[`3d library`](library.md) (`makes`, `publications`, `relations`, `graph`).
