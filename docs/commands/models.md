# `3d models`

Finds ready-made models on the four big marketplaces — [Printables](https://www.printables.com),
[MakerWorld](https://makerworld.com), [Thingiverse](https://www.thingiverse.com) and
[Cults3D](https://cults3d.com) — and downloads a free model's files together with a
`model.json` that records where they came from, who made them and under which license.
Paid models are listed with their price but never downloaded.

## Usage

```bash
3d models search QUERY [--site S[,S...]] [--limit N] [--json] [--images[=PROTO]] [--columns C[,C...]]
                       [--deep] [--cad] [--for-vitamin KIND] [--no-details] [--no-save]
3d models searches list [--limit N] [--json]
3d models searches show ID [--json] [--images[=PROTO]] [--columns C[,C...]]
3d models show URL [--json]
3d models download URL [--dir D] [--file NAME]... [--role ROLE [--vitamin KIND]] [--deep] [--json]
3d models analyze FILE... [--json] [--no-save]
3d models auth status [--json]
3d models auth set SITE
3d models auth delete SITE
```

- `search` asks every requested site at once (up to `--limit`, 1..100, results each; paging happens inside
  the site clients) and prints, per site, the title, author, likes, downloads, licence, price, **CAD yes/no**,
  size, a file summary, a one-line description and the URL (see "Terminal view"). A site that fails (missing
  token, Cloudflare block, network) prints its reason and the fix; the other sites still print. The command
  fails only when no site answered (a single `--site` that fails exits with that site's error).
  MakerWorld search hits carry no price, so they show `price unknown`; `show` and `download` read the full
  design and know. Every search is saved in the library DB (`3d library`) unless `--no-save`.
- `searches list|show` read saved searches back (ID: the running number, the full uid or `latest`).
  `searches show ID --json` is the same document the search printed, so an agent can re-read it later.
- `show` prints one model's metadata, CAD yes/no and file list.
- `download` saves every file of a free model (or only the `--file` names) plus `model.json` into `--dir`,
  default `./<site>-<id>-<title>/`, and records a `model_source` row (URL, licence, author, files with sha256,
  role) in the library DB. A paid model (Cults price — an unknown price counts as paid —,
  Printables Store/Club, MakerWorld paid or point-redeemable) is refused before any file is
  requested. MakerWorld's **Exclusive** badge only means the design is published on
  MakerWorld alone; such designs are free and download like any other.
  Existing files are never overwritten: a `--dir` that is a file or a non-empty directory
  is refused before any request. An existing empty directory (also `.` or a symlink to one)
  is filled in place and keeps its mode, owner and ACLs. Files are fetched into a hidden
  `.part-*` directory inside the target and moved in (never replacing a name that appeared
  meanwhile) only once every file and `model.json` are complete; a failed or truncated
  download removes everything it created (missing parent directories of `--dir` stay).
  File names are kept unique case-insensitively (macOS/Windows) and cut to 200 UTF-8 bytes.
  With a MakerWorld token aboard, a redirect from the API is not followed (it is reported as
  an HTTP error), so the token never reaches another host.
- `analyze` measures local files without any network: for a mesh the bounding box (mm), volume,
  watertight/manifold flags, triangle and body counts and the estimated print footprint; for a zip/tar the
  member table.

Options:

- `--site S[,S...]` — sites to search: `printables`, `makerworld`, `thingiverse`, `cults`
  (default: all four). Example: `3d models search "cable clip" --site printables,cults`
- `--limit N` — results per site, 1..100 (default 10; `--limit 50` is cheap on every site, see
  "Results per site"). Example: `3d models search "cable clip" --limit 50`
- `--images[=PROTO]` — draw each result's cover in the terminal: `auto` (default for a bare `--images`),
  `kitty`, `iterm2`, `sixel` or `none`. `auto` needs a terminal on stdout and looks at `KITTY_WINDOW_ID`,
  `TERM`, `TERM_PROGRAM` and `LC_TERMINAL` (Kitty, Ghostty → kitty; iTerm2, WezTerm → iterm2; foot, mlterm,
  contour, a `TERM` containing `sixel` → sixel); inside tmux/screen it stays off (they swallow the escapes) and
  an explicit protocol forces it; `THREED_IMAGE_PROTOCOL` sets the default. With no support the text view is
  printed unchanged and a one-line note goes to stderr. Covers are fetched with a time budget, shrunk to about
  20 cells wide and cached (finished escape sequence) in `<cache dir>/model-images/`; kitty/sixel need Pillow,
  which runs through `cli.pyrun` when the system Python has none. The escape sequences are unit-tested; they
  have not been looked at in a graphical terminal in this repository's CI.
  Example: `3d models search "cable clip" --images=kitty`
- `--columns C[,C...]` — pick and order the shown fields (`site, author, likes, downloads, makes, views,
  license, price, date, category, cad, size, files, image, ref, description, tags, url`). The default is
  `author,likes,downloads,license,price,cad,size,files,image,description,url`.
  Example: `3d models search "cable clip" --columns author,cad,size,url`
- `--deep` — download the smallest mesh of each result (STL/3MF/OBJ/PLY with a listed size, at most
  `--deep-max-mb`, default 25) and measure it with trimesh: `size_mm`, volume, manifold, bodies. The answer
  is cached in the library DB by site/model/file/size, so a repeat run makes no download at all. Paid
  listings, MakerWorld (files only inside a login-gated archive) and Cults (no file sizes or links in a
  listing) are skipped with a reason.
  Example: `3d models search "m3 screw" --site printables --limit 5 --deep`
- `--deep-max-mb N` — size cap of `--deep` downloads. Example: `3d models search "gear" --deep --deep-max-mb 5`
- `--deep-top N` — with `--deep` or `--for-vitamin`, measure only the first N candidates per site.
  Example: `3d models search "gear" --deep --deep-top 3`
- `--cad` — keep only results whose file list has native CAD (STEP, F3D, SCAD, Blend, ...).
  Example: `3d models search "enclosure" --cad`
- `--for-vitamin KIND` — look for a **reference model of a non-printable part** (`pcb`, `connector`, `screw`,
  `wire`; umbrella #570): adds the kind's keyword to the query unless it is there, measures the small
  candidates (`--deep-top`, default 10 per site) and keeps only results that are `reference_suitable`: free, with
  a licence to record, with a STEP or mesh file, size class `small` or smaller (< 2 MB of mesh) and, once
  measured, no edge over 150 mm. `analytics.reference.dims_known` says whether the size was measured.
  Download the pick with `--role reference` so its source and licence are recorded.
  Example: `3d models search "esp32 devkit" --for-vitamin pcb`
- `--no-details` — skip the per-hit follow-up requests (Thingiverse: licence and file list; then a Thingiverse
  search costs exactly one request per page and its hits have `files_known: false`).
  Example: `3d models search "cable clip" --site thingiverse --no-details`
- `--no-save` — write nothing to the library DB (no saved search, no analytics cache, no `model_source`).
  Example: `3d models search "cable clip" --no-save`
- `--db PATH` — use this library database instead of `$THREED_LIBRARY_DB` / `3d library path`.
  Example: `3d models searches list --db /tmp/library.db`
- `--idempotency-key K` — a retried search with the same key and query returns the first saved session instead
  of adding one (without a key every search is its own session).
  Example: `3d models search "cable clip" --idempotency-key run-42`
- `--dir D` — download directory (new or empty). Example: `3d models download URL --dir parts/pull`
- `--file NAME` — download only this file of the model (repeatable; not available for MakerWorld, which
  serves one archive). Example: `3d models download URL --file pull.stl --file pull.step`
- `--role ROLE` — part role of the download: `printed`, `vitamin`, `reference` or `stand_in`
  (`docs/contracts/parts.md`), stored in `model_source.role`. `vitamin` and `reference` mean "a non-printable
  reference model": its meshes are measured and the licence is mandatory (a listing without one gives a
  `license_missing` warning and `license: null`, never an invented value).
  Example: `3d models download URL --role reference --vitamin pcb`
- `--vitamin KIND` — with `--role vitamin|reference`: `pcb`, `connector`, `screw` or `wire`.
  Example: `3d models download URL --role vitamin --vitamin screw`
- `--json` — machine-readable output (schemas below). On failure `show`/`download` print nothing on stdout
  and the structured error goes to stderr, exactly as without `--json`.
  Example: `3d models search "cable clip" --json`

Exit codes (identical with and without `--json`):

| Code | Meaning |
|---|---|
| 0 | done; for `search`, at least one site answered |
| 1 | a site failed (Cloudflare block, not found, network, HTTP, parse), the model is paid, or every searched site failed for different reasons |
| 2 | bad usage (unknown flag/site, foreign URL, `--dir` is a file or non-empty), or credentials missing/refused — also when every searched site lacks credentials |
| 127 | no Chrome-TLS tool for MakerWorld (see below) |

`search` exits non-zero only when no site answered; its JSON still lists every site's error.

Accepted URLs: `https://www.printables.com/model/<id>-<slug>`,
`https://makerworld.com/en/models/<id>-<slug>`, `https://www.thingiverse.com/thing:<id>`,
`https://cults3d.com/en/3d-model/<category>/<slug>`.

## Results per site

`--limit N` (1..100) is paged **inside** each site client: a request asks for at most 50 results, further pages
follow with the site's own offset/page parameter until `N` results, a short page or the site's `total`. A search
returns fewer than `N` only when the query has fewer hits on that site (the `moisture sensor case` query has 14
on Printables, 24 on Cults).

| Site | Paging | Per hit in the search answer | Extra requests |
|---|---|---|---|
| Printables | `searchPrints2(limit, offset)` | everything: counts, licence, tags, images, **file lists** (`stls`, `otherFiles`, ...) with sizes | none |
| MakerWorld | `search-service/select/design2?limit&offset` | counts, licence, tags, all pictures, the designer's raw model files (`designExtension.model_files`: name, size, type) | none |
| Thingiverse | `/search/{q}?per_page&page` — one request per page | counts, thumbnail; **no licence, no file list** | `enrich`: `GET /things/{id}` (licence, downloads, tags) and `GET /things/{id}/files` — at most one of each per hit (so at most 2 x N), 4 in flight, a 30 s wall-clock budget (`THREED_THINGIVERSE_BUDGET_S`, `0` = none), a 10-minute in-process cache and, unless `--no-save`, a 6-hour cache in the library DB (`analytics_cache`) so a repeat CLI search spends no follow-up requests; a hit whose calls fail or miss the budget keeps its search data, has `files_known: false`, and the site gets a warning. `--no-details` skips them |
| Cults3D | `creationsSearchBatch(limit, offset)` | counts, licence, price, tags, description, pictures, blueprint **file names** (no sizes, no links) | none |

Notes: Thingiverse needs an App Token and Cults a nick + API key (see Credentials). MakerWorld lists only the
raw files a designer attached (STL, STEP, F3D, ...); a design published only as a print profile (3MF) lists none,
which reads as `cad: false, files: []` although a printable 3MF exists. A Cults listing has no file sizes, so its
`size_class` is unknown and `--deep` cannot pick a mesh.

## Terminal view

```
printables (14)  0.4s
  1. Capacitive soil moisture sensor case
     by coxx  likes 41  downloads 493  CC-BY  free
     CAD: no  size ?  files 2 (2 stl, 348 KB) [multi-part]  [img]
     Case for capacitive soil moisture sensor v1.2 and v2.0
     https://www.printables.com/model/702396-capacitive-soil-moisture-sensor-case
saved: search 01a16ec8-… (3d models searches show 01a16ec8-…)
```

Line 1: rank and title. Line 2: statistics. Line 3: facts — `CAD: yes (step, f3d)` / `CAD: no` / `CAD: ?` (the file
list is unknown), `size 62x40x21 mm` (only after `--deep` measured a mesh, else `size ?`), the file summary with
`[multi-part]` / `[assembly]` flags, `[img]` / `[no img]`, and with `--for-vitamin` `ref: yes (dims known)`. Then
the description clipped to the terminal width (`COLUMNS` or the terminal; 120 when not a terminal) and the URL.
`--columns` picks and orders these fields.

## JSON

`3d models search ... --json` prints `3d.models.search/2` (every key of the previous version is unchanged):

```json
{
  "schema": "3d.models.search/2",
  "query": "moisture sensor case", "effective_query": "moisture sensor case",
  "search_id": "01a16ec8-…", "limit": 50, "sites": ["printables", "makerworld", "thingiverse", "cults"],
  "deep": false, "vitamin": null, "elapsed_s": 2.149,
  "timings": {"printables": 0.395, "makerworld": 1.095, "thingiverse": 0.149, "cults": 2.118},
  "results": {"printables": [{"…": "card"}]},
  "errors": [{"site": "thingiverse", "kind": "auth", "message": "…", "remediation": ["…"]}],
  "warnings": [{"site": "thingiverse", "message": "3 of 50 hits have no file list or license (…)"}]
}
```

`search_id` is the saved session's uid (`null` with `--no-save` or when the library DB cannot be written;
the reason is then in `warnings` under site `library`). A site that failed is under `errors` and absent from
`results`. A **card** (`results[site][i]`):

| Key | Meaning |
|---|---|
| `site, id, title, url, author, author_url, likes, downloads, license, price, paid, summary, tags, image, files` | as before (`files[]` entries keep `name, size, ref, kind`) |
| `files[].ext`, `files[].class` | lower-case extension and class: `cad`, `mesh`, `gcode`, `archive`, `drawing`, `image`, `document`, `other` |
| `date, images[], makes, views, category` | published date (ISO), every picture URL (cover first), counts and category when the site gives them |
| `files_known` | the site's answer carried a file list; `false` = not fetched (no file list is NOT "no files") |
| `rank` | 1-based position in the site's own relevance order (kept by `--cad` / `--for-vitamin` filters) |
| `cad` | `true` exactly when a native-CAD file is in `files[]` (`step stp iges igs brep f3d f3z fcstd scad blend sldprt sldasm ipt iam prt par catpart catproduct 3dm skp x_t x_b sat asm cq`; 2-D drawings and archives nobody opened do not count); `false` when `files_known` is false |
| `cad_formats[]` | the CAD extensions found, sorted |
| `size_mm` | `[x, y, z]` of the measured mesh; **absent**, never guessed, unless `--deep` measured one |
| `reference_suitable` | passes the reference-model tests (see `--for-vitamin`) |
| `analytics` | deterministic facts, below |

`analytics` (`schema: "3d.analytics/1"`; computed from names and sizes alone, no LLM): `files_known`,
`file_count`, `total_bytes`, `largest_file_bytes`, `formats{ext: n}`, `cad`, `cad_formats[]`, `parametric`
(SCAD/F3D/FreeCAD/Blend/... source), `has_stl`, `has_3mf`, `has_step`, `has_mesh`, `mesh_formats[]`, `has_gcode`,
`archives_unlisted` (archives whose members are not listed), `mesh_file_count`, `mesh_bytes`, `cad_bytes`,
`mesh_ratio`, `cad_ratio` (share of the listed bytes), `part_count` (distinct part names; `Lid.stl`, `lid.3mf`
and `Lid (1).stl` are one part), `is_multi_part`, `is_assembly` (+ `assembly_files[]`: a name token like
`assembled`/`complete`/`combined` or an assembly CAD format), `size_class` (`tiny` < 100 kB, `small` < 2 MB,
`medium` < 20 MB, `large` < 100 MB, `huge`, of the mesh bytes, else the CAD bytes, else all listed bytes),
`stl_triangles_est` (from binary STL sizes), `reference{suitable, dims_known, reasons[]}` and, with `--deep`,
`deep{}`: `status` (`ok`, `skipped` + `reason`, `error` + `reason`, `unavailable`), `file`, `size`, `sha256`,
`cached`, and for `ok`: `bbox_mm`, `triangles`, `vertices`, `watertight`, `winding_consistent`, `manifold`,
`volume_mm3` (only for a watertight mesh), `surface_area_mm2`, `body_count`, `degenerate_faces`, `fill_ratio`
(volume / bounding-box volume), `est_print_footprint_mm` and `est_print_height_mm` (lying on the smallest
extent), `dimension_class` (`tiny` < 25 mm, `small` < 75, `medium` < 180, `large` < 256, `oversize`) and
`units_suspect` (largest edge under 1 mm or over 2 m: probably metres or inches). 3MF files are read with the
standard library (core spec: components, transforms, units).

`searches list --json`: `{"schema": "3d.models.searches/1", "searches": [{"id", "search_id", "created_at",
"query", "kind", "sites", "counts": {site: n}}]}`. `searches show ID --json`: the search document above plus
`created_at`, `kind` (`user`, `wide`, `narrow`, `followup`, `rewrite`) and `parent`.
`analyze --json`: `{"schema": "3d.models.analyze/1", "facts": {...}, "files": [{"name", "class", "size",
"mesh": {...} | "members": n}]}`. `download --json`: `{"model", "dir", "files": [{"name", "size", "sha256", "kind"}],
"metadata", "analytics": {"facts", "files"}, "source": {"uid", "created", "role", "license"} | null,
"warnings": [{"code", "message"}]}`.

## Library DB

`search` writes one `search_session` (query, kind, `meta`: sites, limit, timings, errors, warnings) and one
`search_result` per hit (site, model id, URL, title, rank, image URL, `analytics`, and `record` — the full card,
which is what `searches show` returns). `download` writes a `model_source` (site, model id, URL, licence,
author, directory, files with sha256, `role`, `vitamin_kind`, `analytics`); one row per site+model, a repeat
download refreshes it. Deep meshes are cached in `analytics_cache` (by file key and by content sha256).
Schema: `docs/contracts/library-db.md`. Persisting is best effort: an unwritable database adds a warning and the
search output is still printed.

## Merge order with #564 (`models publish`)

Both slices edit `lib/commands/models.py` (`USAGE`, `SUBCOMMANDS`) and this file. They touch different regions
(`search`/`searches`/`show`/`download`/`analyze` here, `publish` there), so either may merge first; the second one
rebases and keeps both sets of lines. `SUBCOMMANDS` ends up with `publish` next to `analyze`.

## Examples

```bash
3d models search "spoon drawer pull"
3d models search "spoon drawer pull" --site printables,makerworld --limit 5
3d models search "cable clip" --site thingiverse --json
3d models search "moisture sensor case" --limit 50 --columns author,downloads,cad,size,url
3d models search "m3 screw" --site printables --limit 5 --deep --json
3d models search "esp32 devkit" --for-vitamin pcb
3d models search "cable clip" --images=kitty --limit 5
3d models searches list
3d models searches show latest --json
3d models show https://www.printables.com/model/311253-spoon-drawer-pull
3d models show https://makerworld.com/en/models/1871683 --json
3d models download https://www.printables.com/model/311253-spoon-drawer-pull
3d models download https://www.printables.com/model/311253-spoon-drawer-pull --dir parts/pull --json
3d models download https://www.printables.com/model/311253-spoon-drawer-pull --role reference --vitamin pcb
3d models analyze parts/pull/spoon_handle.stl --json
3d models auth set thingiverse
3d models auth status
```

`model.json` written by `download`:

```json
{
  "source": {"site": "printables", "id": "311253", "url": "https://www.printables.com/model/311253-spoon-drawer-pull"},
  "title": "Spoon Drawer Pull",
  "author": {"name": "cluckOmatic", "url": "https://www.printables.com/@cluckOmatic"},
  "license": "CC0",
  "price": "free",
  "tags": ["pull", "spoon", "drawer"],
  "summary": "Spoon Drawer Pull for contest",
  "downloaded_at": "2026-10-17T12:34:55+00:00",
  "files": [{"name": "spoon_handle.stl", "size": 3306984, "sha256": "a4aaf2a3…", "kind": "stl"}]
}
```

## How each site is reached

| Site | Route | Login |
|---|---|---|
| Printables | public GraphQL API `api.printables.com/graphql/` (the website itself is behind a Cloudflare challenge); files via the `getDownloadLink` mutation | none for free models |
| MakerWorld | the website's JSON API, sent with a Chrome TLS fingerprint (Cloudflare rejects plain HTTP clients) | search/show: none; download: the `token` cookie of a logged-in account |
| Thingiverse | REST API `api.thingiverse.com` (the website is a JavaScript app without data in its HTML) | App Token, always |
| Cults3D | official GraphQL API `cults3d.com/graphql` (the website answers every non-browser client with a Turnstile challenge) | Cults nick + API key, always |

## Credentials

Each value is looked up in this order:

1. its environment variable;
2. the OS keychain (macOS Keychain, Linux Secret Service) through
   [keychain-cli](https://git.hyperide.ai/ultrabricks/keychain-cli), item `3d-cli/<name>`;
   skipped silently when keychain-cli is not installed or the store is locked;
3. `~/.config/3d-cli/auth.json` (the file `3d auth` keeps at 0600):

```json
{
  "thingiverse": {"token": "<App Token>"},
  "cults": {"username": "<Cults nick>", "api_key": "<API key>"},
  "makerworld": {"token": "<token cookie>"}
}
```

| Site | Environment | Keychain item | Where to get it |
|---|---|---|---|
| Thingiverse | `THINGIVERSE_TOKEN` | `3d-cli/thingiverse-token` | create an app at https://www.thingiverse.com/apps/create and copy its App Token |
| Cults3D | `CULTS_USERNAME`, `CULTS_API_KEY` | `3d-cli/cults3d-user`, `3d-cli/cults3d-key` | https://cults3d.com/en/api/keys |
| MakerWorld | `MAKERWORLD_TOKEN` | `3d-cli/makerworld-token` | log in at https://makerworld.com, DevTools → Application → Cookies → `token` |

- `3d models auth set SITE` stores SITE's values in the keychain. Each value comes from a
  hidden prompt on a terminal, else from stdin, one line per field (Cults: username, then API
  key): `printf '%s\n' "$TOKEN" | 3d models auth set thingiverse`. Values never go through
  argv and are never printed. Without keychain-cli it exits 127.
- `3d models auth status [--json]` lists, per site and field, which sources hold a value
  (`env …`, `keychain …`, `auth.json …`) — names only, never values.
- `3d models auth delete SITE` removes SITE's keychain items.
- `THREED_KEYCHAIN=off` skips the keychain lookup (the test suite sets it).

## The MakerWorld TLS lane

MakerWorld's Cloudflare lets a request through only when its TLS/HTTP2 handshake looks like
Chrome's. `3d models` uses the first of:

1. `THREED_CURL_IMPERSONATE` — an explicit `curl_chromeNNN` binary;
2. `$AXE_CURL_IMPERSONATE_DIR/curl_chrome146` — the curl-impersonate 1.5.6 build pinned by the
   axe repo (`tooling/curl-impersonate-patched`, `nix build`), which its devenv shell exports;
3. a `curl_chromeNNN` on `PATH`;
4. the [curl_cffi](https://github.com/lexiforest/curl_cffi) Python binding of the same library,
   run as a separate tool through the repo `.venv` or `uv run --with curl_cffi` (fetched on
   first use).

Request headers (the MakerWorld token) reach the tool through a file in a private temp dir,
never through the process arguments. Neither lane solves a JavaScript/Turnstile challenge;
that would take a real browser, such as axe's Hero stack (`hero fetch|scrape`), which is
not wired in.

The API base URLs can be pointed elsewhere (mirrors, the test suite's loopback fakes) with
`THREED_PRINTABLES_API`, `THREED_THINGIVERSE_API`, `THREED_CULTS_API` and
`THREED_MAKERWORLD_URL`. `THREED_CURL_IMPERSONATE` must name an existing executable (a wrong path is a usage
error, exit 2, not a silent fallback); every URL a site hands out must be http(s) on both lanes.
