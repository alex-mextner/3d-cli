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
flags with `--json`. `queue`, `photo` (register an existing file), `search` and `download` (S5, #576) are
implemented; the other actions raise `NotImplementedByDesign(owner_issue)` until their slice lands (`OWNERS` maps
action -> issue). `search` takes `deep` and `idempotency_key` besides the contract arguments and returns a flat
`results[]` (site, model_id, url, title, rank, image_url, license, paid, cad, size_mm, reference_suitable) plus the
session uid; a site that failed is a `site_failed` warning and only "no site answered" is `ok: false`. `download`
takes `role` / `vitamin_kind` / `deep` and records the `model_source`.

## Migration 0010 - search core (S5, #576)

`lib/library/migrations/0010_search_core.sql` (numbered apart from 0001 so slices that add tables later do not
clash) adds:

- `search_session.meta` - JSON object: `schema`, `effective_query`, `limit`, `sites`, `deep`, `vitamin`,
  `elapsed_s`, `timings`, `errors`, `warnings` of the run;
- `search_result.record` - JSON object: the full result card of `3d models search --json` (so `3d models searches
  show ID --json` round-trips what the search printed); `analytics` holds its deterministic facts;
- `model_source.role` - `printed | vitamin | reference | stand_in` or NULL (the user did not say), the part role of
  `docs/contracts/parts.md`; `model_source.vitamin_kind` - `pcb | connector | screw | wire`;
  `model_source.analytics` - JSON object with the file facts and mesh measurements of the downloaded files;
- `analytics_cache(key PRIMARY KEY, sha256, analytics, created_at)` - deep mesh measurements keyed by
  `file:<site>:<model id>:<name>:<size>` (a marketplace file, so a repeat `--deep` search needs no download) and by
  `sha:<sha256>` (content, so a local file or a re-uploaded copy is not measured twice), and the Thingiverse follow-up answers (licence, counts, file list) by `thingiverse:<id>` for 6 hours (`LibraryDetailStore`). Not exported; a cache.

Idempotency keys: a session's `source_key` is `search:<sha256(idempotency_key, query)>` when the caller gives
`idempotency_key` (none = a fresh session per search); a result's is `<session uid>:<site>:<model id>`; a
`model_source`'s is `source:<site>:<model id>` (a repeat download updates that row). `3d library status` lists
the applied migrations `[1, 10]`. `invoke(name,
**kwargs)` is the never-raising edge for UIs.
