# Library database contract (umbrella #570, S0 #571)

## Engine decision: a separate local SQLite file, not a PrintOps migration range

The library lives in `data_dir()/library/library.db` (`$THREED_LIBRARY_DB` overrides; `3d library path`). It
reuses the PrintOps *code* - `printops.db.open_db` (WAL, `foreign_keys=ON`, 5 s busy timeout),
`migrate` (checksummed `NNNN_<name>.sql`), `transaction()` (`BEGIN IMMEDIATE`) and `printops.events.emit` - but
not the PrintOps *database*. Reasons:

1. **Offline agents.** PrintOps runs in 3d-api on the Odroid and the CLI reaches it over HTTP; a search, a
   download or a queued make must be recorded on the machine that does it, with no server.
2. **No numbering clash.** PrintOps migrations use 0001..0100 and are applied by 3d-api to its own file; a
   shared range would couple every library change to a 3d-api deploy. The library has its own directory
   `lib/library/migrations/`, numbered from `0001`.
3. **Different data.** Searches and photo paths are per-machine and re-creatable; spools and the ledger are
   fleet truth and must not be mixed with them.

The library migration `0001_library.sql` carries its own copy of the append-only `events` table (same shape),
so `printops.events.emit` works on a library connection. Events are named `library.<table>.created`.

## Tables

`search_session`, `search_result` (`UNIQUE(session_id, site, model_id)`), `model_source`
(`UNIQUE(site, model_id)`), `design`, `make` (`status queued|printing|done|failed`), `photo` (`kind
table|bed|render`, belongs to a make or a design), `publication`, `relation`
(`rel remix_of|make_of|derived_from`; endpoints are full uids of a design, make or model_source).

Conventions: full UUIDv7 `uid` on every row (never `uid[:8]`, #452); ISO-8601 UTC timestamps with ms;
`json_valid` CHECKs on every JSON column (`analytics`, `files`); `source_key UNIQUE` is the idempotency key of
imports - `library.db.insert_row(conn, table, values, source_key=K)` returns the first row again with
`created=False`.

## Actions

`lib/library/actions.py` fixes eight functions - `search, rank, download, open_slicer, launch_agent, queue,
photo, publish` - returning `{schema: "3d.action/1", action, ok, data, errors[], warnings[]}`. They never
print, prompt or exit. The TUI (S7) binds keys to `ACTIONS[name]` and the CLI exposes the same functions as
flags with `--json`. `queue`, `photo` (register a file, or `capture=True`: the table photo with the printer's bed lifted, restored
afterwards; dry run by default) and `publish` (plan by default, `dry_run=False` records the rows) are implemented
(slice #579); the other actions raise `NotImplementedByDesign(owner_issue)` until their slice lands (`OWNERS` maps
action -> issue, `None` = implemented).

## Writers of makes, photos, publications and relations (#579)

`library/records.py` is the one place that writes them; `3d make`, `3d photo` and `3d models publish --yes` call
it. Relations: `make --make_of--> design` (every make), `design --derived_from--> model_source` (a make printed from
a download), `design --remix_of--> model_source` (a published remix). `publication.platform` is the platform name,
or `<platform>:make` for a make post; a publication goes `planned` -> `draft|published|failed` and is never deleted.
Read them with `3d library makes|publications|relations list` and `3d library graph DESIGN`. `invoke(name,
**kwargs)` is the never-raising edge for UIs.
