# `3d models`

Finds ready-made models on the four big marketplaces — [Printables](https://www.printables.com),
[MakerWorld](https://makerworld.com), [Thingiverse](https://www.thingiverse.com) and
[Cults3D](https://cults3d.com) — and downloads a free model's files together with a
`model.json` that records where they came from, who made them and under which license.
Paid models are listed with their price but never downloaded. `3d models publish` goes the
other way: it puts one of your own model projects (or a remix of a downloaded one, or a make of somebody's
model) on MakerWorld, Printables, Thingiverse and Cults3D, after the checks that must pass first (see
[Publishing](#publishing-a-model-project)); `3d models offer` proposes that after a print or a modify step.

## Usage

```bash
3d models search QUERY [--site S[,S...]] [--limit N] [--json] [--images[=PROTO]] [--columns C[,C...]]
                       [--deep] [--cad] [--for-vitamin KIND] [--no-details] [--no-save]
                       [--rank[=MODEL]] [--classifier M] [--planner M] [--rewrite sonnet|MODEL|off]
                       [--max-queries N] [--max-calls N] [--token-budget N] [--llm-timeout S] [--batch-size N]
                       [--answer KEY=VALUE]... [--no-llm] [--llm auto|require] [--ndjson] [--background|--wait]
                       [--tui [--plain]]
3d models searches list [--limit N] [--json]
3d models searches show ID [--json] [--images[=PROTO]] [--columns C[,C...]]
3d models rerank ID [--classifier M] [--planner M] [--rewrite sonnet|MODEL|off] [--answer KEY=VALUE]...
                    [--follow N]... [--refresh] [--llm auto|require] [--ndjson|--json] [--db PATH]
3d models followups ID [--json] [--answer KEY=VALUE]... [--follow N]...
3d models browse [SEARCH_ID] [--plain] [--dir D] [--slicer S] [--browser CMD] [--agent B] [--agent-mode M]
                 [--prompt TEXT] [--agent-cwd D] [--images[=PROTO]] [--no-llm] [--db PATH]
3d models act [SEARCH_ID] [--select N,N-M,SITE:ID|all] [--select-site S[,S]] [--select-verdict V[,V]]
              [--do list|preview|open|download|slicer|agent|queue] [--yes] [--dir D] [--slicer S] [--browser CMD]
              [--agent B] [--agent-mode M] [--prompt TEXT] [--agent-cwd D] [--answer KEY=VALUE]... [--follow N]...
              [--refine QUERY] [--filter TEXT] [--images[=PROTO]] [--json|--ndjson] [--db PATH]
3d models show URL [--json]
3d models download URL [--dir D] [--file NAME]... [--role ROLE [--vitamin KIND]] [--deep] [--json]
3d models analyze FILE... [--json] [--no-save]
3d models publish PROJECT_DIR --platform makerworld|printables|thingiverse|cults|all [--kind auto|original|remix|make] [--of SRC] [--make ID] [--dry-run | --yes] [--json]
3d models offer [PROJECT_DIR] [--trigger print|modify] [--publish auto|ask|off] [--yes] [--json]
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
- `browse` (also `search QUERY --tui`) is the interactive browser and `act` is its non-interactive twin: see
  "Browser and `act`" below and `docs/commands/tui.md`.
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
- `--platform P` — `publish` target: `makerworld`, `printables`, `thingiverse`, `cults`, a comma-separated list, or
  `all` (the first three, plus `cults` when `publish.yaml` has `hosted:` URLs) (required).
- `--dry-run` — `publish`: print the plan, send nothing. This is the default.
- `--yes` — `publish`: really send. Contradicts `--dry-run`. `offer`: the confirmation.
- `--kind K` — `publish`: `auto` (default), `original`, `remix`, `make`. Example: `--kind remix --of ./downloaded-model`.
- `--of SRC` — `publish`: the source model (URL, `model.json`, or a directory holding one).
- `--make ID` — `publish`/`offer`: the make (full uid or unique prefix) whose photos are used. Example: `--make 0199a1c2-7d3e`.
- `--design NAME` — `publish`/`offer`: the library design (default: `name:` of `3d.yaml`, else the directory name).
- `--note TEXT` — `publish --kind make`: the text of the make post. Example: `--note "printed in PLA, no supports"`.
- `--db PATH` — `publish`/`offer`: the library database (default: `3d library path`). Example: `--db /tmp/library.db`.
- `--allow-nonprintable P[,P]` / `--reason TEXT` — `publish`: publish vitamin/reference/stand_in parts on purpose
  (logged). Example: `--allow-nonprintable probe_pcb --reason "assembly kit"`.
- `--drop-nonprintable` — `publish`: leave non-printable parts' files out, with a warning.
- `--trigger T` — `offer`: `print` (default) or `modify`. `--publish M` — `offer`: `auto`, `ask`, `off`.
  `--printer P` — `offer`: find the make of that printer in the library.
- `--tui` — with `search`: open the interactive browser instead of printing the list; it ranks like `--rank`
  unless `--no-llm`. Needs a terminal on stdin and stdout and the `tui` extra (`uv sync --extra tui`); on a pipe, or
  with `THREED_NO_TUI=1`, the search prints exactly what it prints without `--tui`.
  Example: `3d models search "cable clip" --tui`
- `--plain` — the numbered prompt (commands typed on stdin) instead of the full-screen browser; what `browse`
  falls back to without a terminal. Example: `3d models browse latest --plain`
- `--select SEL` — rows `act` works on: ranking numbers (`1,3`), ranges (`2-5`), `SITE:ID` keys or `all`.
  Example: `3d models act latest --select 1,3,5-7 --do preview`
- `--select-site S[,S]` — only rows from these sites. Example: `3d models act latest --select-site printables --do list`
- `--select-verdict V[,V]` — only rows with these verdicts (`fits`, `maybe`, `no`, `unjudged`); several selectors
  must all match. Example: `3d models act latest --select-verdict fits --do list`
- `--do ACTION` (alias `--action`) — what `act` does with the rows: `list` (default), `preview`, `open` (alias
  `browser`), `download`, `slicer`, `agent`, `queue`. Example: `3d models act latest --select 1 --do download`
- `--yes` — really run `open`, `download`, `slicer`, `agent` and `queue`; without it `act` prints the plan and starts
  nothing. Example: `3d models act latest --select 1 --do slicer --yes`
- `--slicer S` — the slicer `s` / `slicer` opens: a kind (`orca`, `snapmaker-orca`, `creality-print`,
  `bambu-studio`) or a command with `{paths}` (default: `$THREED_SLICER`, `browser.json`, the first installed).
  Example: `3d models act latest --select 1 --do slicer --slicer snapmaker-orca`
- `--browser CMD` — the command that opens a page, `{url}` where the URL goes (default: `$THREED_BROWSER`,
  `browser.json`, `$BROWSER`, `xdg-open` / `open`).
  Example: `3d models act latest --select 1 --do open --browser "firefox {url}"`
- `--agent B` — the agent backend: `omp` (default), `claude`, `codex`, `opencode`.
  Example: `3d models act latest --select 1 --do agent --agent claude`
- `--agent-mode M` — `attach` (this terminal; the browser's default), `tmux` (a detached tmux session),
  `detached` (no terminal, log in the brief dir) or `auto` (attach on a terminal, else tmux, else detached; the
  default of `act`). Example: `3d models act latest --select 1 --do agent --agent-mode tmux --yes`
- `--prompt TEXT` — the task the agent is seeded with (default: judge the picks against the query).
  Example: `3d models act latest --select 1,2 --do agent --prompt "compare the two cases"`
- `--agent-cwd D` — directory the agent starts in (default: the folder the downloads share, else the current one).
  Example: `3d models act latest --select 1 --do agent --agent-cwd ~/projects/probe`
- `--refine QUERY` — search again as a child of this search and merge the hits (the `r` key).
  Example: `3d models act latest --refine "probe holder" --do list`
- `--filter TEXT` — keep rows whose title, site, verdict, licence, tags or rationale contain TEXT (the `/` key).
  Example: `3d models act latest --filter case --do list`
- `--session ID` — the saved search to work on; the same as the positional `SEARCH_ID` (`latest` is the newest
  search tree). Example: `3d models act --session latest --do list`
- `--json` — machine-readable output (schemas below). On failure `show`/`download` print nothing on stdout
  and the structured error goes to stderr, exactly as without `--json`.
  Example: `3d models search "cable clip" --json`

Exit codes (identical with and without `--json`):

| Code | Meaning |
|---|---|
| 0 | done; for `search`, at least one site answered |
| 1 | a site failed (Cloudflare block, not found, network, HTTP, parse), the model is paid, or every searched site failed for different reasons |
| 2 | bad usage (unknown flag/site, foreign URL, `--dir` is a file or non-empty), or credentials missing/refused — also when every searched site lacks credentials; for `publish`: a defective `publish.yaml` or a refusal (see below) |
| 1 (publish) | a part of `3d.yaml` that is `vitamin`/`reference`/`stand_in` has a file in the publish set, or a platform did not go through |
| 127 | no Chrome-TLS tool for MakerWorld (see below) |
| 127 | no Chrome-TLS tool for MakerWorld (see below); `textual` missing for the browser (`uv sync --extra tui`); no slicer, browser opener or agent program for `act` |

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

## Ranking

Everything here is optional: without `--rank` (and without `llm.enabled` in `ai.json`) `3d models search` is the
deterministic search above, byte for byte, and `--no-llm` forces that path even when ai.json enables ranking.
`omp` (the agent runner) is the model backend; it is never auto-picked, so a search never starts a subscription
CLI unless you ask for it.

```bash
3d models search "soil moisture sensor probe case" --rank
3d models search "little mole shaped box that holds a plant moisture probe" --rank --ndjson
3d models rerank latest --answer need_cad=yes --follow 1
3d models followups latest --json
```

**Two phases.** (1) The search runs (50 results per site when ranking is on and `--limit` is absent) and the
normal view is printed at once — before any model call. (2) A haiku-class **classifier** judges every result
from its title, description, tags, licence, price and the deterministic file analytics (CAD formats, STL/3MF/STEP,
multi-part, assembly, size class, measured bounding box with `--deep`): `verdict fits|maybe|no`, `score 0..1`
and a one-line `rationale`, as strict JSON (`3d.models.verdicts/1`). Results go in batches of 25, several
batches at once; every finished batch prints its verdict lines immediately, and at the end the ranking lists the
`fits` and `maybe` results, best first (fits, maybe, unjudged, no; by score; the search's own order breaks ties).
Verdicts are cached in the library DB per (question + answered constraints, model, result card), so a repeat run,
a `rerank` or another search that finds the same card makes no call. The text view is for people;
`--ndjson` / `--json` are for agents (below).

**Rewriting.** When fewer than `llm.rewrite_below` (default 3) results `fit` — or nothing was found — a
sonnet-class **planner** reworks the query: a descriptive phrase becomes marketplace vocabulary (technical
terms, synonyms, other languages). It may use omp's `web_search` and `read` (web fetch) tools and sees the top
results with their verdicts and every query already run. It returns up to `--max-queries` (default 4) queries
to run now: `rewrite` (same intent, new words), `wide` (the general category, similar models) and `narrow` (a
precise variant), always at least one `wide` and one `narrow`. They run in parallel, each as its own saved search
(`kind` rewrite|wide|narrow, `parent_id` = the original search), are merged into the result pool by (site, id),
and the new hits are judged too. A query equal (case, punctuation and spacing folded) to one already run in the
search tree is skipped and recorded as `skipped`. With enough fits the planner is still asked once for
**follow-ups** and **questions** only; `--rewrite off` skips the planner altogether.

**Follow-ups and questions.** Follow-up queries (`kind followup`, numbered 1..N) are suggestions that are not run
until you ask: `--follow N`. Questions are typed — `single` (one of `choices`), `multi` (a comma-separated
subset) or `text` — with a snake_case `key`. Answer one with `--answer KEY=VALUE` (repeatable, validated against
its question; a key nobody asked is stored as a free-text constraint). An answer is saved, applied to the
classifier as a constraint ("Constraints the user gave"), and re-judges the results; questions already answered
are never asked again. `3d models followups ID` lists them; with `--answer` / `--follow` it works like `rerank`.
The terminal UI (#578) binds the same actions (`library.actions.rank`) to keys.

**Guards and degradation.** `--max-calls N` (default 12), `--token-budget N` (default none), `--llm-timeout S`
(default 120 per call; the planner has its own `planner_timeout_s`, default 300) and `--batch-size N` bound the
cost. Invalid JSON is retried once (the retry names what was wrong; a partial answer re-asks only the missing
results); a timeout is not retried. If omp is missing, fails, times out, or answers invalid twice, the
deterministic results stay on screen, ONE line `LLM: unavailable (...)` says why, and the exit code is 0. With
`--llm require` a missing omp exits 127 before any request goes out. Every call (role, model, status, latency,
tokens) is saved in `llm_call`.

**Configuration.** One section of `~/.config/3d-cli/ai.json` (the file `3d ai` uses); every key is optional and
every flag overrides it:

```json
{"llm": {"enabled": false, "backend": "omp", "classifier": "haiku", "planner": "sonnet",
         "batch_size": 25, "parallel": 4, "timeout_s": 120, "planner_timeout_s": 300,
         "max_calls": 12, "token_budget": 0, "rewrite_below": 3, "max_queries": 4,
         "planner_tools": ["web_search", "read"], "thinking": "off",
         "prompts": {"classify": "...", "plan": "..."}}}
```

`prompts` replaces a system prompt; the replacement must keep the JSON contract of `3d.models.verdicts/1` /
`3d.models.plan/1`. Model names are whatever omp accepts (`haiku`, `sonnet`, `anthropic/claude-haiku-4-5`).
`$THREED_OMP` points at a specific omp binary (tests use a fake).

**Output modes.** Text (default); `--ndjson` streams `3d.models.rank/1` events, one JSON object per line, flushed
as they happen — `start`, `search`, `result`, `phase`, `verdict`, `plan`, `query`, `question`, `warning`, `done`
(schema and the order guarantees: `docs/contracts/search-events.md`); `--json` prints one
`3d.models.rank-result/1` document at the end (the root `3d.models.search/2` document and one per child search,
judged results carry `verdict`/`score`/`rationale`, plus `ranking`, `queries`, `followups`, `questions`, `llm`).
`--background` prints phase 1 and hands the judging to a detached `3d models rerank` process; read the outcome
with `3d models searches show ID`. `searches show` prints each verdict under its result.

Flags (each also in `3d models --help`):

- `--rank[=MODEL]` — rank with the classifier model; `3d models search "cable clip" --rank` or `--rank=haiku`.
- `--classifier M` / `--planner M` — the two models; `3d models search "cable clip" --classifier haiku --planner sonnet`.
- `--rewrite sonnet|MODEL|off` — the planner model, or no planner call; `3d models search "cable clip" --rank --rewrite off`.
- `--max-queries N` — queries the planner runs, >= 2; `3d models search "mole probe box" --rank --max-queries 6`.
- `--max-calls N` / `--token-budget N` — cost guards; `3d models search "cable clip" --rank --max-calls 4 --token-budget 200000`.
- `--llm-timeout S` / `--batch-size N` — per-call timeout and results per call; `3d models search "cable clip" --rank --llm-timeout 60 --batch-size 10`.
- `--answer KEY=VALUE` — answer a question (repeatable); `3d models rerank latest --answer need_cad=yes`.
- `--follow N` (alias `--followup`) — run suggested follow-up N; `3d models rerank latest --follow 1`.
- `--refresh` — ignore cached verdicts; `3d models rerank latest --refresh`.
- `--no-llm` — never start omp; `3d models search "cable clip" --no-llm`.
- `--llm auto|require` — `require` exits 127 before searching when omp is missing; `3d models search "cable clip" --llm require`.
- `--ndjson` — event stream; `3d models search "cable clip" --rank --ndjson`.
- `--background` / `--wait` — judge detached / before returning (default); `3d models search "cable clip" --rank --background`.

Exit codes are those of `search` (0 when any site answered); `--llm require` without omp is 127, bad `--answer`
values or flag combinations are 2. A model failure never changes the exit code.

## Browser and `act`

`3d models browse [SEARCH_ID]` and `3d models search QUERY --tui` open the interactive browser
(`docs/commands/tui.md`): the S6 ranking events stream into a list (results first, verdicts as the model answers),
arrows move, `space` selects, and `p b d s a f r / ? q` preview, open in the browser, download, open in the slicer,
launch an agent, show follow-ups and questions, refine, filter, help, quit. `browse` resumes any saved search tree
from the library DB (`latest` = the newest root). Without a terminal (stdout or stdin not a TTY, or
`THREED_NO_TUI=1`) `browse` is a numbered prompt; with a terminal but without the `tui` extra it exits 127 with the
install line, and `search --tui` on a pipe is just the normal search.

`3d models act [SEARCH_ID] --select ... --do ...` is the twin of every key, for agents and scripts. It reads the saved
search, runs the same `library.actions` functions the key runs (`preview`, `open_browser`, `download`,
`open_slicer`, `launch_agent`, `queue`) and prints ONE `3d.models.act/1` document:

```json
{"schema": "3d.models.act/1", "action": "download", "ok": true, "dry_run": false, "exit_code": 0,
 "search": "<root uid>", "query": "case",
 "selected": [{"n": 1, "key": "printables:7", "site": "printables", "id": "7", "title": "...", "url": "..."}],
 "results": [{"schema": "3d.action/1", "action": "download", "ok": true, "data": {"dir": "...", "files": []},
              "errors": [], "warnings": []}],
 "plan": [], "cli": "3d models act <uid> --do download --yes --select printables:7"}
```

`--do list` prints the numbered rows instead (`results[]`: `n, key, site, id, title, url, verdict, score, rationale,
size_mm, cad, license, price, found_by, downloaded`) with the planner's `questions[]`, `followups[]` and `queries[]`;
the `n` of a row is its position in the final ranking (fits, maybe, unjudged, no), the same number the browser shows.
`--answer KEY=VALUE`, `--follow N` and `--refine QUERY` run the ranking pipeline first (`--ndjson` streams its
`3d.models.rank/1` events before the final document), so one command can answer a question and act on the
re-ranked rows. Anything that starts something is a dry run until `--yes`: the document then has `dry_run: true`,
`results: []` and a `plan`. A model that was downloaded earlier (the library DB knows its folder) is reused, never
fetched twice; `slicer` and `queue` download first when needed, `agent` seeds the brief with the downloaded paths and
`model.json` provenance when they exist. Exit codes: 0 done (or a dry run), 1 an action failed (its `3d.action/1`
result holds the error), 2 bad usage (`--select 99`, nothing selected, unknown `--do`), 127 a program is missing
(slicer, browser opener, agent, or `textual`). The agent workflow is in `docs/agents/search-workflow.md`.

Configuration (`~/.config/3d-cli/browser.json`, or the file named by `$THREED_BROWSER_CONFIG`):

```json
{"slicer": "snapmaker-orca", "browser": "firefox {url}",
 "agent": {"backend": "omp", "command": ["omp", "@{brief}", "{prompt}"], "prompt": "Read {brief} ..."},
 "download_dir": "~/models"}
```

No path is hard-coded: the slicer is `--slicer`, `$THREED_SLICER`, the file, then the first of OrcaSlicer, Snapmaker
Orca, Creality Print, Bambu Studio found on `PATH` or in the usual app locations.

## Library DB

`search` writes one `search_session` (query, kind, `meta`: sites, limit, timings, errors, warnings) and one
`search_result` per hit (site, model id, URL, title, rank, image URL, `analytics`, and `record` — the full card,
which is what `searches show` returns). `download` writes a `model_source` (site, model id, URL, licence,
author, directory, files with sha256, `role`, `vitamin_kind`, `analytics`); one row per site+model, a repeat
download refreshes it. Deep meshes are cached in `analytics_cache` (by file key and by content sha256).
Ranking adds the verdict, score and rationale on each `search_result`, and (migration 0030) `llm_verdict_cache`,
`search_plan_query` (every planner query with its status), `search_question` (questions and answers) and
`llm_call` (the model-call ledger). Schema: `docs/contracts/library-db.md`. Persisting is best effort: an
unwritable database adds a warning and the search output is still printed.

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
3d models search "soil moisture sensor probe case" --tui
3d models browse latest
3d models act latest --do list --json
3d models act latest --select-verdict fits --do download --dir parts --yes
3d models show https://www.printables.com/model/311253-spoon-drawer-pull
3d models show https://makerworld.com/en/models/1871683 --json
3d models download https://www.printables.com/model/311253-spoon-drawer-pull
3d models download https://www.printables.com/model/311253-spoon-drawer-pull --dir parts/pull --json
3d models publish projects/wall-plugs --platform all --dry-run
3d models publish projects/wall-plugs --platform thingiverse --yes
3d models publish projects/mole --platform all --kind remix --of ./printables-311253-spoon-pull --dry-run --json
3d models publish projects/mole --platform printables --kind make --make 0199a1c2-7d3e-7000-8000-000000000001 --of https://www.printables.com/model/311253-spoon-drawer-pull --dry-run
3d models publish projects/mole --platform all --drop-nonprintable --dry-run
3d models publish projects/mole --platform all --allow-nonprintable probe_pcb --reason "assembly kit" --dry-run
3d models offer projects/mole --trigger print --json
3d models offer --printer k1se --publish off
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

## Publishing a model project

`3d models publish PROJECT_DIR --platform P` publishes the project in `PROJECT_DIR` — laid out
like a garage-band project — to one platform or to all three. **It is a dry run unless you pass
`--yes`**: the dry run prints, per platform, the exact file set, the fields and the steps a real
run would take, builds them from the same plan the real run executes, and sends nothing. Any
refusal exits 2, after printing the plan and a `REFUSED` list with each problem and its fix.

### `publish.yaml`

Metadata lives in `PROJECT_DIR/publish.yaml` (never credentials). Unknown keys are refused.

```yaml
title: "Spiral drywall plug, flat head Ø14, M3 bore, 50 mm"   # required
description: |                                                  # required
  Long description, plain text.
summary: "One line (Printables' short summary); default: the first 160 characters of the description"
tags: [drywall, anchor, plug]
license: cc-by-nc                # required: cc0, cc-by, cc-by-sa, cc-by-nd, cc-by-nc, cc-by-nc-sa,
                                 # cc-by-nc-nd, gpl, lgpl, bsd, sdfl, arr (or text such as "CC BY-SA 4.0")
category: Household              # required by Thingiverse (its full category name)
remix_of: https://makerworld.com/en/models/2592818   # set when the model is a remix
remix_license: Standard Digital File License         # optional, see below
permission_ref: ""               # URL / message id of the original author's written permission
images: [previews/wall_plug_iso.png]                 # default: every png/jpg in previews/
```

### The file set

The same set goes to every platform: **the sliced project `.3mf` and the `.scad` source are
mandatory**, and every `.stl` plus every other CAD file found (`.f3d`, `.f3z`, `.step`, `.stp`,
`.blend`) goes along. Only the project root and `print/` are scanned, hidden files and symlinks
are skipped, and `references/` (third-party material), G-code, manifests and run logs are never
included. Images are listed apart: `images:` in `publish.yaml`, else every `.png`/`.jpg` in
`previews/`. Two files with the same name (root and `print/`) are refused, as the upload names
must stay unique.

### Refusals (exit 2, nothing sent)

- no `.3mf` or no `.scad` in the project;
- a defective `publish.yaml` (missing title/description/license, unknown key, `images` outside
  the project, a licence that is not in the list);
- **a remix (`remix_of`) whose source licence forbids derivatives** — CC BY-ND, CC BY-NC-ND, the
  Standard Digital File License, all rights reserved — **or cannot be determined**, unless
  `permission_ref` names the original author's written permission. The source licence is read
  from the marketplace (`remix_of` must be a URL `3d models show` understands) and from
  `remix_license`; when both exist the stricter one counts, so a declaration never loosens what
  the site says. When the lookup fails and nothing is declared the remix is refused: an unknown
  licence is never assumed free. With `permission_ref` the dry run prints which barrier it lifted;
- conditions of a Creative Commons source that no permission waives: a non-commercial source
  needs a non-commercial `license`, a share-alike source a share-alike one; a GPL source needs
  GPL, an LGPL source LGPL or GPL;
- per platform: a licence the platform does not offer (see the table), no `category` for
  Thingiverse, no cover image for MakerWorld and Printables.

A remix always credits the original: the description gets a `Remix of URL` line (plus the
permission when given), and a Thingiverse source is also linked as `ancestors`.

### Routes per platform

| Platform | Route | What `--yes` does |
|---|---|---|
| Thingiverse | the official REST API: `POST /things`, `POST /files/0/uploadFile` per file and image, `POST /files/0/FinalizeFiles`, `POST /things/{id}/publish` | creates the thing, uploads, finalizes and publishes; a failure after the draft exists leaves it unpublished and names it |
| MakerWorld | no public upload API; the website driven with `agent-browser` in its own headed session per publication, `3d-publish-makerworld-<hash of title and files>` | opens the site, uploads the files, fills title, description, tags, licence and category, takes a screenshot and **stops before the final Publish button** |
| Printables | no public upload API (its GraphQL is internal); `agent-browser` in session `3d-publish-printables-<hash of title and files>` | the same, plus the summary |
| Cults3D | GraphQL API `createCreation`, HTTP Basic (nick + API key); files and images pulled from your `hosted:` URLs | creates a free creation, see [Cults3D](#cults3d) |

Thingiverse's token goes in the `Authorization: Bearer` header only. For the two browser sites
the login is passed with `keychain fill` from `3d-cli/<site>-user` and `3d-cli/<site>-password`
(store them once with `keychain set 3d-cli/makerworld-user --origin https://makerworld.com`, and
likewise `…-password`, `printables`): the value goes keychain → browser and never through this
process; the owner approves each fill, solves any 2FA/captcha and presses Publish. A step the
page no longer answers stops the run, leaves the window open and prints the form values to enter
by hand. Re-running keeps the login (`agent-browser --restore`).

Licence names sent per platform (canonical id → platform):

| `license` | Thingiverse | MakerWorld | Printables |
|---|---|---|---|
| `cc0` | `pd0` | Public Domain | CC0 |
| `cc-by` / `-sa` / `-nd` | `cc` / `cc-sa` / `cc-nd` | BY / BY-SA / BY-ND | CC BY / CC BY-SA / CC BY-ND |
| `cc-by-nc` / `-nc-sa` / `-nc-nd` | `cc-nc` / `cc-nc-sa` / `cc-nc-nd` | BY-NC / BY-NC-SA / BY-NC-ND | CC BY-NC / -NC-SA / -NC-ND |
| `gpl`, `lgpl`, `bsd` | same codes | not offered | GPL, LGPL, BSD |
| `sdfl` | not offered | Standard Digital File License | not offered |

Caveats: **no real publish has been run** — the Thingiverse calls follow its OpenAPI spec and
upload guide, and the MakerWorld/Printables click path and labels are unverified against the
live pages (both sit behind Cloudflare), which is why those runs stop for review. Printables'
licence list and MakerWorld's acceptance of `.scad`/`.f3d` attachments are unverified too.

### Credentials for `publish`

Thingiverse writes need the user's OAuth **access token with write scope** — not the read-only
App Token that `search`/`show` use. Create an app at https://www.thingiverse.com/apps/create,
open `https://www.thingiverse.com/login/oauth/authorize?client_id=<id>&redirect_uri=urn:ietf:wg:oauth:2.0:oob&response_type=code`,
POST `client_id`, `client_secret` and the `code` to `https://www.thingiverse.com/login/oauth/access_token`
and store the `access_token` with `3d models auth set thingiverse-upload`
(environment `THINGIVERSE_UPLOAD_TOKEN`, keychain item `3d-cli/thingiverse-upload-token`, or
`auth.json` `thingiverse-upload.token`). With `--yes` it is resolved before any platform starts,
so a missing token leaves nothing half-published.

### Cults3D

`--platform cults` (and `all` when `publish.yaml` has a `hosted:` map) uses Cults' GraphQL API with the nick and
API key of `3d models auth set cults`. **Cults pulls files and images from public https URLs; the API takes no
upload**, so every file and image of the plan needs an entry in `hosted:` (file name → URL) — a missing one is
a `REFUSED` problem, as are a missing `category` (its English name) and more than 10 files or images (bundle the
CAD files in a zip). `--yes` sends three requests: `categories` (to find the category id), `licenses` (the
licence whose name parses to the project's licence), then `createCreation(... downloadPrice: 0 ...)` — a free
creation. The API shapes follow the community-documented API; they are **unverified against a real account**
(#565) and the tests use fixtures of those shapes (`tests/data/publish/`), not recordings.

```yaml
hosted:
  wall_plug.3mf: https://files.example.com/wall_plug.3mf
  wall_plug.scad: https://files.example.com/wall_plug.scad
  iso.png: https://files.example.com/iso.png
```

### Remix and make posts (`--kind`, `--of`)

`--kind` is `auto` (default: a remix when `--of` or `remix_of` says so, else an original), `original`, `remix` or
`make`. `--of SRC` names the source: a model URL, a `model.json` written by `3d models download`, or the
directory holding one. The source's licence is read where it was recorded (the `model.json`, else the library's
`model_source` row, else the marketplace), and the #564 remix rules above apply unchanged: ND / NC-ND /
all-rights-reserved / unknown is refused without `permission_ref`; NC, SA and GPL conditions are inherited.
A `model.json` is trusted only when its `source.url` is an https page of a supported marketplace and the
`site` / `id` it declares match that URL; anything else is refused (exit 2) because the make flow opens that URL.

`--kind make --make ID --of SRC` posts a **make** — "I printed this" with the photos of make `ID` — to the model
page of the source (or of the design's own recorded publications): `--platform all` means the platforms that
have such a page. The remix rules do not apply (printing a model is what every licence allows); what is required
is a model page and at least one photo of the make (`render` photos do not count). The steps are the same
agent-browser route as a listing (open the page, press the site's make button, attach the photos, fill the
note, screenshot) and **stop before the final Post button**. Their labels (`Add make`, `Share your print`,
`I Made One`, `Add a make`) are unverified against the live pages. `--note TEXT` is the post text; printer and
filament come from the make. An explicit `--of` always wins over a publication the library already knows.

### Photos and renders in the listing

The photos of the library join the listing's images, so a listing carries CAD files, **renders** (`previews/`,
registered as `photo(kind=render)`) and **photos** (`table`/`bed`, with the bed height they were taken at):
those of the design and of all its makes for a listing, those of the make for a make post. A photo whose file is
gone is a warning, not an error. The dry run lists them (`photos from the library`; `photos` in `--json`). Take
them with [`3d photo`](photo.md).

### Non-printable parts never go out as printable models

Roles live in the project's `3d.yaml` (`parts.<name>.role`: `printed` (default), `vitamin`, `reference`,
`stand_in`; a `vitamin`/`reference` carries `reference_source: {url, license}`; see
[`parts.md`](../contracts/parts.md)). A file of the publish set (root and `print/`) belongs to a part when its
stem is the part's name or the stem of its `file:`.

- A file of a non-printable part in the set **refuses the publication with exit 1** (`GateFailure`; the plan is
  printed first, `--yes` does not lift it).
- `--drop-nonprintable` leaves those files out and lists each as a warning.
- `--allow-nonprintable PART[,PART] --reason TEXT` publishes them on purpose; without `--reason` it is refused
  (exit 2). The override is listed in the plan and written as a `library.publish.override` event (part, role,
  reason) in the library.
- Every part with a `reference_source` is **credited** in the description (`Reference models used (not part of
  the printable files): - probe_pcb: URL (licence)`), and its licence is judged like `remix_of`: an ND reference
  is refused without `permission_ref`, NC/SA/GPL conditions must be carried by the published `license`.

### What `--yes` records in the library

`--yes` writes to the local library (`--db PATH`, default `3d library path`): a `publication` row per platform
(`planned` before the attempt, then `draft` — a browser draft awaiting your click —, `published` or `failed`; a
make post is recorded as `<platform>:make`), the `remix_of` relation design → source (the source becomes a
`model_source` row when new), `make_of` make → design, and the project's renders as `photo(kind=render)`.
Nothing is deleted: a rerun adds rows. A dry run never creates or writes the library. Read it back with
`3d library publications|relations list`, `3d library graph DESIGN`.

### `3d models offer` — the publish offer after a print or a modify step

```bash
3d models offer [PROJECT_DIR] [--trigger print|modify] [--publish auto|ask|off] [--platform P] [--make ID | --design NAME | --printer P] [--yes] [--json]
```

Prepares the dry-run plan for the design — an original or remix listing with CAD files, renders and photos on all
platforms, plus a make post when the print was of somebody else's model — states the licence, and asks. Nothing is
ever published without a confirmation:

| `--publish` | Terminal | Not a terminal / `--json` |
|---|---|---|
| `ask` (default) | prints the summary, asks `Publish to all platforms with CAD files, renders and photos? [Y/n]` — the default is yes only for an open licence with no refusal | prints the plan and the exact `3d models offer … --yes` command (the offer, not `models publish`, fills the title, description and licence publish.yaml leaves out); publishes nothing |
| `auto` | publishes only together with `--yes` | `--yes` publishes; without it `status: needs_confirmation` |
| `off` | nothing at all: no output, the library is not even opened, exit 0 | the same (`{"status": "off"}` with `--json`) |

`--publish` defaults to `$THREED_PUBLISH_OFFER`, then `offer` in `<config dir>/publish.json` (`{"offer": "off"}`),
then `ask`. `--yes` with `ask` publishes (it is the confirmation). The final click of the MakerWorld/Printables
drafts stays yours as always; a refused plan (licence, missing 3mf/scad, non-printable part) exits 2/1 and sends
nothing.

**Licence default (stated in the offer, `defaults_used` in `--json`).** A design whose `publish.yaml` lacks
`license` — or has no `publish.yaml`; `title`/`description` are then assumed from the design name — is offered under
**CC BY** (open, available on every platform). A remix inherits what its source requires: share-alike → CC BY-SA,
non-commercial → CC BY-NC, both → CC BY-NC-SA, GPL/LGPL → the same. No category is ever assumed: Thingiverse
(and Cults) keep refusing until `category` is set. Which listing is offered: `trigger=modify` → a remix when the
design has a source, else an original; `trigger=print` → the original/remix listing unless the design is only
*derived from* a source (a plain print of a download), plus a make post when a model page is known.

**Hooks.** `3d printer subscribe --once --on bed-free` appends the offer to the message its agent receives when the
library knows a `printing`/`done` make on that printer and the mode is not `off`: the agent runs
`3d models offer --make ID --trigger print --json`, shows the user the plan and asks. `3d mesh deform` /
`fit-silhouette` call the offer when their output lands in a project: on a terminal the prompt of
`--trigger modify`, otherwise one hint line on stderr; silent when `off`. The exit code of the mesh step is never
changed.

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
| Thingiverse (publish) | `THINGIVERSE_UPLOAD_TOKEN` | `3d-cli/thingiverse-upload-token` | the OAuth flow described under "Credentials for `publish`" |
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
