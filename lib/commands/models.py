"""3d models — search, inspect and download models on 3D-model marketplaces.

ACCESSED VIA: `3d models search|show|download`. Thin CLI over lib/marketplaces/ (one client
per site: Printables, MakerWorld, Thingiverse, Cults3D). The site clients are imported
inside each subcommand so `3d help` loads only the stdlib-only `marketplaces.base`.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from cli.registry import Command
from errors import InvalidArgument, UsageError
from marketplaces.base import SITES, MarketplaceError, Model

DEFAULT_LIMIT = 10

USAGE = """3d models <search|show|download|auth> [options]
  Find and fetch 3D models on Printables, MakerWorld, Thingiverse and Cults3D.

Subcommands:
  search QUERY [--site S[,S...]] [--limit N] [--json]
                        search each site (default: all four) and list title, author,
                        likes, downloads, license, price and URL; a site that fails is
                        reported with its reason and the other sites still print
  show URL [--json]     one model's metadata and file list
  download URL [--dir D] [--json]
                        save every file of a free model plus model.json (source URL,
                        author, license, sha256 per file) into D (default
                        ./<site>-<id>-<title>/; a non-empty D is refused, nothing is
                        overwritten); paid models are refused
  auth status [--json]  which sources hold each site's credentials (names only, never values)
  auth set SITE         store SITE's token/key in the OS keychain (keychain-cli); the value
                        is read from a hidden prompt, or from stdin one line per field
  auth delete SITE      remove SITE's items from the OS keychain

Options:
  --site S[,S...]       sites to search: printables, makerworld, thingiverse, cults
  --limit N             results per site (default 10)
  --dir D               download directory
  --json                machine-readable output

Credentials (environment variable, else OS keychain via keychain-cli, else
~/.config/3d-cli/auth.json):
  printables            none
  makerworld            search/show: none; download: MAKERWORLD_TOKEN / 3d-cli/makerworld-token
                        / makerworld.token
  thingiverse           THINGIVERSE_TOKEN / 3d-cli/thingiverse-token / thingiverse.token
  cults                 CULTS_USERNAME + CULTS_API_KEY / 3d-cli/cults3d-user + 3d-cli/cults3d-key
                        / cults.username + cults.api_key
MakerWorld needs a Chrome TLS fingerprint: curl_chrome146 on PATH (or
AXE_CURL_IMPERSONATE_DIR) or the curl_cffi Python package.

Exit codes (the same with --json):
  0    done; for search, at least one site answered
  1    a site failed (blocked, not found, network), a paid model, or every searched
       site failed for different reasons
  2    bad usage, a non-empty --dir, or missing/refused credentials (also when every
       searched site lacks credentials)
  127  no Chrome-TLS tool for MakerWorld

Examples:
  3d models search "spoon drawer pull"
  3d models search "spoon drawer pull" --site printables,makerworld --limit 5
  3d models search "cable clip" --site thingiverse --json
  3d models show https://www.printables.com/model/311253-spoon-drawer-pull
  3d models show https://makerworld.com/en/models/1871683 --json
  3d models download https://www.printables.com/model/311253-spoon-drawer-pull
  3d models download https://www.printables.com/model/311253-spoon-drawer-pull --dir parts/pull --json
  3d models auth set thingiverse
  3d models auth status"""


def _parse(argv: list[str], *, value_flags: set[str], bool_flags: set[str]) -> tuple[list[str], dict[str, str]]:
    positional: list[str] = []
    opts: dict[str, str] = {}
    i = 0
    while i < len(argv):
        arg = argv[i]
        name, eq, inline = arg.partition("=")
        if name in value_flags:
            if eq:
                opts[name] = inline
            elif i + 1 < len(argv):
                i += 1
                opts[name] = argv[i]
            else:
                raise UsageError(f"{name} needs a value", command="models",
                                 remediation=["Run '3d models --help' for examples."])
        elif arg in bool_flags:
            opts[arg] = "1"
        elif arg.startswith("--"):
            raise UsageError(f"unknown option {arg}", command="models",
                             remediation=["Run '3d models --help' for the accepted options."])
        else:
            positional.append(arg)
        i += 1
    return positional, opts


def _one_positional(positional: list[str], what: str) -> str:
    if len(positional) != 1:
        hint = ('Quote a multi-word query: 3d models search "spoon drawer pull"' if what == "QUERY"
                else f"Pass exactly one {what}.")
        raise UsageError(f"expected exactly one {what}, got {len(positional)}", command="models",
                         remediation=[hint])
    return positional[0]


def _sites(raw: str | None) -> list[str]:
    if raw is None:
        return list(SITES)
    sites = [s.strip().lower() for s in raw.split(",") if s.strip()]
    for site in sites:
        if site not in SITES:
            raise InvalidArgument("--site", site, SITES, command="models")
    if not sites:
        raise InvalidArgument("--site", raw, SITES, command="models")
    return list(dict.fromkeys(sites))


def _limit(raw: str | None) -> int:
    if raw is None:
        return DEFAULT_LIMIT
    if not (raw.isascii() and raw.isdecimal()) or not 1 <= int(raw) <= 100:
        raise InvalidArgument("--limit", raw, ["an integer 1..100"], command="models")
    return int(raw)


def _stats(model: Model) -> str:
    parts = [f"by {model.author or '?'}"]
    if model.likes is not None:
        parts.append(f"likes {model.likes}")
    if model.downloads is not None:
        parts.append(f"downloads {model.downloads}")
    parts.append(model.license or "license ?")
    parts.append(model.price or "free")
    return "  ".join(parts)


def _search(argv: list[str]) -> int:
    from marketplaces import catalog

    positional, opts = _parse(argv, value_flags={"--site", "--limit"}, bool_flags={"--json"})
    query = _one_positional(positional, "QUERY")
    sites, limit = _sites(opts.get("--site")), _limit(opts.get("--limit"))
    results = catalog.search(query, sites, limit)
    failed = [r for r in results if r.error is not None]
    only = results[0].error if len(results) == 1 else None
    if only is not None and "--json" not in opts:
        raise only  # the only site failed: its structured error, printed once
    if "--json" in opts:
        print(json.dumps({
            "query": query,
            "results": {r.site: [m.to_json() for m in r.models] for r in results if r.error is None},
            "errors": [catalog.error_json(r.site, r.error) for r in results if r.error is not None],
        }, indent=2, ensure_ascii=False))
    else:
        for r in results:
            if r.error is not None:
                reason = r.error.reason if isinstance(r.error, MarketplaceError) else r.error.message
                print(f"{r.site}: unavailable — {reason}")
                for step in r.error.remediation:
                    print(f"  -> {step}")
                continue
            print(f"{r.site} ({len(r.models)})")
            for n, model in enumerate(r.models, start=1):
                print(f"  {n}. {model.title}\n     {_stats(model)}\n     {model.url}")
    if len(failed) == len(results):
        errors = [r.error for r in failed if r.error is not None]
        # Every reason is already on stdout (text or JSON): exit with the code the failures
        # share — 2 when every site wants credentials — else 1.
        codes = {e.exit_code for e in errors}
        return codes.pop() if len(codes) == 1 else 1
    return 0


def _print_model(model: Model) -> None:
    print(model.title)
    print(f"  site      {model.site} #{model.id}")
    print(f"  url       {model.url}")
    print(f"  author    {model.author}" + (f"  {model.author_url}" if model.author_url else ""))
    print(f"  license   {model.license or '?'}")
    print(f"  price     {model.price or 'free'}")
    if model.likes is not None or model.downloads is not None:
        print(f"  likes     {model.likes if model.likes is not None else '?'}"
              f"  downloads {model.downloads if model.downloads is not None else '?'}")
    if model.tags:
        print(f"  tags      {', '.join(model.tags)}")
    if model.summary:
        print(f"  summary   {model.summary}")
    print(f"  files     {len(model.files)}")
    for f in model.files:
        size = f" ({f.size} B)" if f.size is not None else ""
        print(f"    {f.name}{size}")


def _show(argv: list[str]) -> int:
    from marketplaces import catalog

    positional, opts = _parse(argv, value_flags=set(), bool_flags={"--json"})
    model = catalog.show(_one_positional(positional, "URL"))
    if "--json" in opts:
        print(json.dumps(model.to_json(), indent=2, ensure_ascii=False))
    else:
        _print_model(model)
    return 0


def _download(argv: list[str]) -> int:
    from marketplaces import catalog

    positional, opts = _parse(argv, value_flags={"--dir"}, bool_flags={"--json"})
    url = _one_positional(positional, "URL")
    dest = Path(opts["--dir"]).expanduser() if "--dir" in opts else None
    model, target, files = catalog.download(url, dest)
    if "--json" in opts:
        payload: dict[str, Any] = {"model": model.to_json(), "dir": str(target), "files": files,
                                   "metadata": str(target / "model.json")}
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0
    print(f"{model.title} — {model.author}, {model.license or 'license ?'}")
    for f in files:
        print(f"  {target / f['name']}  ({f['size']} B)")
    print(f"  {target / 'model.json'}")
    return 0


def _auth_site(positional: list[str]) -> str:
    from marketplaces.credentials import FIELDS

    site = _one_positional(positional, "SITE").strip().lower()
    if site not in FIELDS:
        raise InvalidArgument("SITE", site, list(FIELDS), command="models")
    return site


def _read_secret(prompt: str) -> str:
    """A hidden prompt on a terminal, else one line of stdin — never argv, never echoed."""
    import getpass
    import sys

    try:
        value = getpass.getpass(f"{prompt}: ") if sys.stdin.isatty() else sys.stdin.readline()
    except EOFError:
        value = ""  # Ctrl-D at the prompt: same answer as an empty value
    value = value.strip()
    if not value:
        raise UsageError(f"empty {prompt}", command="models",
                         remediation=["Type the value at the prompt, or pipe it: "
                                      "printf '%s\\n' \"$TOKEN\" | 3d models auth set SITE"])
    return value


def _auth(argv: list[str]) -> int:
    from marketplaces import credentials

    action, rest = (argv[0], argv[1:]) if argv else ("", [])
    if action == "status":
        positional, opts = _parse(rest, value_flags=set(), bool_flags={"--json"})
        if positional:
            raise UsageError("auth status takes no arguments", command="models",
                             remediation=["Run '3d models auth status'."])
        keychain = credentials.secret_store() is not None
        if keychain:
            state = "available"
        elif credentials.keychain_disabled():
            state = "switched off (THREED_KEYCHAIN=off)"
        else:
            state = "not available (keychain-cli not installed)"
        report = {site: {f.key: credentials.sources(f) for f in fields}
                  for site, fields in credentials.FIELDS.items()}
        if "--json" in opts:
            print(json.dumps({"keychain": keychain, "sites": report}, indent=2))
            return 0
        print(f"keychain: {state}")
        for site, fields in report.items():
            for key, found in fields.items():
                print(f"  {site}.{key}: {', '.join(found) if found else 'not configured'}")
        return 0
    if action == "set":
        positional, _ = _parse(rest, value_flags=set(), bool_flags=set())
        site = _auth_site(positional)
        store = credentials.require_store()
        values = [(f, _read_secret(f.prompt)) for f in credentials.FIELDS[site]]
        for field, value in values:
            store.set(field.keychain, value, label=f"3d-cli {field.prompt}")
            print(f"stored {field.keychain} in the keychain")
        return 0
    if action == "delete":
        positional, _ = _parse(rest, value_flags=set(), bool_flags=set())
        site = _auth_site(positional)
        store = credentials.require_store()
        for field in credentials.FIELDS[site]:
            print(f"{'deleted' if store.delete(field.keychain) else 'absent'} {field.keychain}")
        return 0
    raise InvalidArgument("auth action", action or "(none)", ["status", "set", "delete"], command="models")


SUBCOMMANDS = {"search": _search, "show": _show, "download": _download, "auth": _auth}


def run(argv: list[str]) -> int:
    if not argv:
        print(USAGE)
        return 1
    if argv[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0
    sub = SUBCOMMANDS.get(argv[0])
    if sub is None:
        raise InvalidArgument("subcommand", argv[0], list(SUBCOMMANDS), command="models")
    if any(a in ("-h", "--help") for a in argv[1:]):
        print(USAGE)
        return 0
    return sub(argv[1:])


COMMAND = Command(
    name="models",
    group="LIBRARIES",
    summary="search/show/download models on Printables, MakerWorld, Thingiverse, Cults3D",
    usage=USAGE,
    run=run,
)
