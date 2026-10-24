# `3d library` - local model-library database

Bootstraps and inspects the local library database: search sessions and results, downloaded model sources,
designs, makes (the print queue), photos, publications and the relations between them (remix / make /
derived). The database is a separate SQLite file that works offline. This command creates, migrates and reads
it; the rows are written by the search, download, queue, photo and publish actions
(`lib/library/actions.py`, see [the contract](../contracts/library-db.md)), by [`3d make`](make.md) and
[`3d photo`](photo.md).

## Usage

```bash
3d library <path|migrate|status|export|makes|publications|relations|graph> [--db PATH] [--json]
```

## Subcommands

| Subcommand | What |
|---|---|
| `path` | Print the database path (`$THREED_LIBRARY_DB`, else `<data dir>/library/library.db`). Never creates it. Example: `3d library path` |
| `migrate` (alias `init`) | Create the database if needed and apply pending migrations; running it twice changes nothing. Example: `3d library migrate --json` |
| `status` | Path, applied and pending migrations, row counts per table. Migrates a fresh database first. Example: `3d library status --json` |
| `export` | Every table as JSON rows (JSON columns decoded) on stdout, for backup or an agent. Example: `3d library export > library-backup.json` |
| `makes`, `publications`, `relations`, `graph` | Read-only views of the makes, publications and relations, and the provenance graph of a design; see below. Example: `3d library graph mole` |

## Options

| Option | What |
|---|---|
| `--db PATH` | Use this file instead of the default. Example: `3d library status --db /tmp/library.db` |
| `--design D` | Restrict `makes`/`publications`/`relations list` to one design (name or uid). Example: `3d library makes list --design mole` |
| `--json` | Machine-readable output with `schema: "3d.library/1"` (`export` is always JSON). Example: `3d library path --json` |

## Exit codes

`0` ok; `2` unknown subcommand or option (the error lists the accepted values).

## Example

```bash
$ 3d library status --json --db /tmp/library.db
{
  "counts": {"design": 0, "make": 0, "model_source": 0, "photo": 0, "publication": 0, "relation": 0,
             "search_result": 0, "search_session": 0},
  "events": 0,
  "migrations": [1],
  "migrations_pending": [],
  "path": "/tmp/library.db",
  "schema": "3d.library/1"
}
```

## Makes, publications, relations, graph (read-only)

The rows that [`3d make`](make.md), [`3d photo`](photo.md) and [`3d models publish`](models.md) write, read back.
Nothing here writes or deletes a row.

```bash
3d library makes list [--design D] [--db PATH] [--json]
3d library publications list [--design D] [--db PATH] [--json]
3d library relations list [--design D] [--db PATH] [--json]
3d library graph DESIGN [--db PATH] [--json]
```

| Subcommand | What | Schema (`--json`) |
|---|---|---|
| `makes list` | The print queue and history: uid, design, status, printer, filament, photo count, source. Example: `3d library makes list --json` | `3d.library.makes/1` |
| `publications list` | Where a design was published: platform, state (`planned`, `draft`, `published`, `failed`, `removed`), URL. A make post is recorded as `<platform>:make`. Example: `3d library publications list --design mole` | `3d.library.publications/1` |
| `relations list` | `remix_of` / `make_of` / `derived_from` links with labelled endpoints (full uids in `--json`). Example: `3d library relations list --json` | `3d.library.relations/1` |
| `graph DESIGN` | The provenance around one design, walked in both directions: **source ← remix ← make** (`design --remix_of--> model_source`, `make --make_of--> design`, `design --derived_from--> model_source`). Text is an indented list, sources first. Example: `3d library graph mole --json` | `3d.library.graph/1` |

`--design D` restricts a listing to one design (name or uid). How the rows get there: `3d make add` writes
`make` + `make_of` (+ `derived_from` when a source is given); `3d models publish --yes` writes a `publication` row
per platform (`planned` before the attempt, then `draft`/`published`/`failed`; a rerun adds a row, nothing is
deleted), the `remix_of` relation of a remix and the project's renders as `photo(kind=render)`.
