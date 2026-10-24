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
3d models search QUERY [--site S[,S...]] [--limit N] [--json]
3d models show URL [--json]
3d models download URL [--dir D] [--json]
3d models publish PROJECT_DIR --platform makerworld|printables|thingiverse|cults|all [--kind auto|original|remix|make] [--of SRC] [--make ID] [--dry-run | --yes] [--json]
3d models offer [PROJECT_DIR] [--trigger print|modify] [--publish auto|ask|off] [--yes] [--json]
3d models auth status [--json]
3d models auth set SITE
3d models auth delete SITE
```

- `search` asks every requested site at once and prints, per site, the title, author, likes,
  downloads, license, price and URL. A site that fails (missing token, Cloudflare block,
  network) prints its reason and the fix; the other sites still print. The command fails only
  when no site answered (a single `--site` that fails exits with that site's error).
  MakerWorld search hits carry no price, so they show `price unknown`; `show` and
  `download` read the full design and know.
- `show` prints one model's metadata and file list.
- `download` saves every file of a free model plus `model.json` into `--dir`, default
  `./<site>-<id>-<title>/`. A paid model (Cults price — an unknown price counts as paid —,
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

Options:

- `--site S[,S...]` — sites to search: `printables`, `makerworld`, `thingiverse`, `cults`
  (default: all four).
- `--limit N` — results per site, 1..100 (default 10).
- `--dir D` — download directory (new or empty).
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
- `--json` — machine-readable output: `search` prints
  `{"query", "results": {site: [model...]}, "errors": [{"site", "kind", "message", "remediation"}]}`
  where `kind` is `auth`, `blocked`, `paid`, `not-found`, `http`, `network`, `parse` or
  `dependency`; `show` prints the model object and `download` prints
  `{"model", "dir", "files", "metadata"}`. On failure `show`/`download` print nothing on stdout
  and the structured error goes to stderr, exactly as without `--json`.

Exit codes (identical with and without `--json`):

| Code | Meaning |
|---|---|
| 0 | done; for `search`, at least one site answered |
| 1 | a site failed (Cloudflare block, not found, network, HTTP, parse), the model is paid, or every searched site failed for different reasons |
| 2 | bad usage (unknown flag/site, foreign URL, `--dir` is a file or non-empty), or credentials missing/refused — also when every searched site lacks credentials; for `publish`: a defective `publish.yaml` or a refusal (see below) |
| 1 (publish) | a part of `3d.yaml` that is `vitamin`/`reference`/`stand_in` has a file in the publish set, or a platform did not go through |
| 127 | no Chrome-TLS tool for MakerWorld (see below) |

`search` exits non-zero only when no site answered; its JSON still lists every site's error.

Accepted URLs: `https://www.printables.com/model/<id>-<slug>`,
`https://makerworld.com/en/models/<id>-<slug>`, `https://www.thingiverse.com/thing:<id>`,
`https://cults3d.com/en/3d-model/<category>/<slug>`.

## Examples

```bash
3d models search "spoon drawer pull"
3d models search "spoon drawer pull" --site printables,makerworld --limit 5
3d models search "cable clip" --site thingiverse --json
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
| MakerWorld | no public upload API; the website driven with `agent-browser` in its own headed session `3d-publish-makerworld` | opens the site, uploads the files, fills title, description, tags, licence and category, takes a screenshot and **stops before the final Publish button** |
| Printables | no public upload API (its GraphQL is internal); `agent-browser` in session `3d-publish-printables` | the same, plus the summary |
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

`--kind make --make ID --of SRC` posts a **make** — "I printed this" with the photos of make `ID` — to the model
page of the source (or of the design's own recorded publications): `--platform all` means the platforms that
have such a page. The remix rules do not apply (printing a model is what every licence allows); what is required
is a model page and at least one photo of the make (`render` photos do not count). The steps are the same
agent-browser route as a listing (open the page, press the site's make button, attach the photos, fill the
note, screenshot) and **stop before the final Post button**. Their labels (`Add make`, `Share your print`,
`I Made One`, `Add a make`) are unverified against the live pages. `--note TEXT` is the post text; printer and
filament come from the make.

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
| `ask` (default) | prints the summary, asks `Publish to all platforms with CAD files, renders and photos? [Y/n]` — the default is yes only for an open licence with no refusal | prints the plan and the exact `3d models publish … --yes` commands; publishes nothing |
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
`THREED_MAKERWORLD_URL`.
