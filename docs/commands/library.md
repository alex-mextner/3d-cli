# `3d library` - local model-library database

Bootstraps and inspects the local library database: search sessions and results, downloaded model sources,
designs, makes (the print queue), photos, publications and the relations between them (remix / make /
derived). The database is a separate SQLite file that works offline. This command creates, migrates and reads
it; the rows are written by the search, download, queue and publish actions
(`lib/library/actions.py`, see [the contract](../contracts/library-db.md)).

## Usage

```bash
3d library <path|migrate|status|export> [--db PATH] [--json]
```

## Subcommands

| Subcommand | What |
|---|---|
| `path` | Print the database path (`$THREED_LIBRARY_DB`, else `<data dir>/library/library.db`). Never creates it. Example: `3d library path` |
| `migrate` (alias `init`) | Create the database if needed and apply pending migrations; running it twice changes nothing. Example: `3d library migrate --json` |
| `status` | Path, applied and pending migrations, row counts per table. Migrates a fresh database first. Example: `3d library status --json` |
| `export` | Every table as JSON rows (JSON columns decoded) on stdout, for backup or an agent. Example: `3d library export > library-backup.json` |

## Options

| Option | What |
|---|---|
| `--db PATH` | Use this file instead of the default. Example: `3d library status --db /tmp/library.db` |
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
