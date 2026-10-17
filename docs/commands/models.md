# `3d models`

Finds ready-made models on the four big marketplaces — [Printables](https://www.printables.com),
[MakerWorld](https://makerworld.com), [Thingiverse](https://www.thingiverse.com) and
[Cults3D](https://cults3d.com) — and downloads a free model's files together with a
`model.json` that records where they came from, who made them and under which license.
Paid models are listed with their price but never downloaded.

## Usage

```bash
3d models search QUERY [--site S[,S...]] [--limit N] [--json]
3d models show URL [--json]
3d models download URL [--dir D] [--json]
3d models auth status [--json]
3d models auth set SITE
3d models auth delete SITE
```

- `search` asks every requested site at once and prints, per site, the title, author, likes,
  downloads, license, price and URL. A site that fails (missing token, Cloudflare block,
  network) prints its reason and the fix; the other sites still print. The command fails only
  when no site answered (a single `--site` that fails exits with that site's error).
- `show` prints one model's metadata and file list.
- `download` saves every file of a free model plus `model.json` into `--dir`, default
  `./<site>-<id>-<title>/`. A paid model (Cults price — an unknown price counts as paid —,
  Printables Store/Club, MakerWorld paid or point-redeemable) is refused before any file is
  requested. MakerWorld's **Exclusive** badge only means the design is published on
  MakerWorld alone; such designs are free and download like any other.
  Existing files are never overwritten: a `--dir` that is a file, a non-empty directory or
  the current directory is refused before any request (an existing empty directory, or a
  symlink to one, is fine). Files are fetched into a hidden `.<dir>.*.part` sibling that is
  renamed to the target only once every file and `model.json` are complete; a failed or
  truncated download leaves no files behind (missing parent directories of `--dir` stay).
  File names are kept unique case-insensitively (macOS/Windows) and cut to 200 UTF-8 bytes.
  With a MakerWorld token aboard, a redirect from the API is not followed (it is reported as
  an HTTP error), so the token never reaches another host.

Options:

- `--site S[,S...]` — sites to search: `printables`, `makerworld`, `thingiverse`, `cults`
  (default: all four).
- `--limit N` — results per site, 1..100 (default 10).
- `--dir D` — download directory (new or empty).
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
| 2 | bad usage (unknown flag/site, foreign URL, `--dir` is a file or non-empty), or credentials missing/refused — also when every searched site lacks credentials |
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
`THREED_MAKERWORLD_URL`.
