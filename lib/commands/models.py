"""3d models — search, inspect and download models on 3D-model marketplaces.

ACCESSED VIA: `3d models search|searches|show|download|analyze|auth`. Thin CLI over lib/marketplaces/ (one client
per site: Printables, MakerWorld, Thingiverse, Cults3D) and lib/library/ (search core, file analytics,
persistence in the library DB). Everything heavy is imported inside each subcommand so `3d help` loads only
the stdlib-only `marketplaces.base`.
"""

from __future__ import annotations

import json
import shutil
import sys
from collections.abc import Collection
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cli.registry import Command
from errors import InputNotFound, InvalidArgument, ThreeDError, UsageError
from marketplaces.base import SITES, MarketplaceError, Model

DEFAULT_LIMIT = 10

USAGE = """3d models <search|searches|rerank|followups|show|download|analyze|auth> [options]
  Find and fetch 3D models on Printables, MakerWorld, Thingiverse and Cults3D.

Subcommands:
  search QUERY [--site S[,S...]] [--limit N] [--json] [--images[=PROTO]] [--columns C[,C...]]
               [--deep] [--cad] [--for-vitamin KIND] [--no-details] [--no-save]
               [--rank[=MODEL]] [--classifier M] [--planner M] [--rewrite sonnet|MODEL|off]
               [--answer KEY=VALUE]... [--no-llm] [--llm auto|require] [--ndjson] [--background]
                        search each site (default: all four, up to N results each, 1..100) and
                        list title, author, likes, downloads, licence, price, CAD yes/no, size,
                        file summary, description and URL; a site that fails is reported with
                        its reason and the other sites still print. The search is saved in the
                        library DB (`3d library`) unless --no-save. With --rank an LLM (via omp)
                        judges every result (fits / maybe / no + a one-line reason) AFTER the
                        results are printed, and reworks the query when few fit ("ranking" in
                        docs/commands/models.md)
  searches list [--limit N] [--json]
                        the saved searches, newest first (id, time, query, hits per site)
  searches show ID [--json] [--images[=PROTO]] [--columns C[,C...]]
                        one saved search with all its results (ID: number, full uid or `latest`)
  rerank ID [--classifier M] [--planner M] [--rewrite sonnet|MODEL|off] [--answer KEY=VALUE]...
         [--follow N]... [--refresh] [--llm auto|require] [--ndjson|--json] [--db PATH]
                        judge a saved search again (and its rewrite/follow-up searches): verdicts come
                        from the cache when nothing changed, a new --answer re-judges, --follow N runs
                        suggested follow-up N. Unavailable model: one `LLM: unavailable (...)` line
  followups ID [--json] [--answer KEY=VALUE]... [--follow N]...
                        the follow-up queries and clarifying questions the planner saved for a search;
                        with --answer / --follow it works like `rerank`
  show URL [--json]     one model's metadata and file list
  download URL [--dir D] [--file NAME]... [--role ROLE [--vitamin KIND]] [--deep] [--json]
                        save every file of a free model (or only the --file names) plus model.json
                        (source URL, author, license, sha256 per file) into D (default
                        ./<site>-<id>-<title>/; a non-empty D is refused, nothing is
                        overwritten); paid models are refused. The source (URL, licence, files,
                        role) is recorded as a `model_source` in the library DB unless --no-save
  analyze FILE... [--json] [--no-save]
                        deterministic analytics of local files: mesh bounding box, volume,
                        watertight/manifold, triangles, bodies, print footprint; archive members
  auth status [--json]  which sources hold each site's credentials (names only, never values)
  auth set SITE         store SITE's token/key in the OS keychain (keychain-cli); the value
                        is read from a hidden prompt, or from stdin one line per field
  auth delete SITE      remove SITE's items from the OS keychain

Options:
  --site S[,S...]       sites to search: printables, makerworld, thingiverse, cults
                        Example: 3d models search "cable clip" --site printables,cults
  --limit N             results per site (default 10, up to 100; paged inside each site)
                        Example: 3d models search "cable clip" --limit 50
  --images[=PROTO]      draw each result's picture in the terminal; PROTO = auto (default), kitty, iterm2,
                        sixel or none. auto needs a terminal on stdout; without support the text view
                        has no pictures. Example: 3d models search "cable clip" --images=kitty
  --columns C[,C...]    pick and order the shown fields: site, author, likes, downloads, makes, views,
                        license, price, date, category, cad, size, files, image, ref, description, tags, url
                        Example: 3d models search "cable clip" --columns author,cad,size,url
  --deep                download the smallest mesh of each result (STL/3MF/OBJ/PLY, up to --deep-max-mb)
                        and measure it with trimesh: `size_mm`, volume, watertight, bodies; cached in the
                        library DB, so a repeat run makes no download
                        Example: 3d models search "m3 screw" --site printables --limit 5 --deep
  --deep-max-mb N       largest mesh --deep may download (default 25)
                        Example: 3d models search "gear" --deep --deep-max-mb 5
  --deep-top N          with --deep or --for-vitamin, measure only the first N candidates per site
                        Example: 3d models search "gear" --deep --deep-top 3
  --cad                 keep only results whose file list has native CAD (STEP, F3D, SCAD, Blend, ...)
                        Example: 3d models search "enclosure" --cad
  --for-vitamin KIND    search for a non-printable part model (KIND: pcb, connector, screw, wire): adds
                        the kind's keyword to the query, measures the small candidates, and keeps only
                        results that are free, licensed, have STEP/mesh files, small, <= 150 mm; each result
                        gets `reference_suitable`. Example: 3d models search "esp32 devkit" --for-vitamin pcb
  --no-details          skip the per-hit follow-up requests (Thingiverse: licence and file list)
                        Example: 3d models search "cable clip" --site thingiverse --no-details
  --no-save             do not write the search / download / analytics cache to the library DB
                        Example: 3d models search "cable clip" --no-save
  --db PATH             use this library database file (default: $THREED_LIBRARY_DB or `3d library path`)
                        Example: 3d models searches list --db /tmp/library.db
  --idempotency-key K   a retried search with the same K and query returns the first saved session
                        Example: 3d models search "cable clip" --idempotency-key run-42
  --dir D               download directory
                        Example: 3d models download URL --dir parts/pull
  --file NAME           download only this file of the model (repeatable)
                        Example: 3d models download URL --file pull.stl --file pull.step
  --role ROLE           part role of the download (printed, vitamin, reference, stand_in), recorded in the
                        library DB; vitamin/reference mean "a non-printable reference model" and measure
                        the meshes. Example: 3d models download URL --role reference --vitamin pcb
  --vitamin KIND        with --role vitamin|reference: the kind (pcb, connector, screw, wire)
                        Example: 3d models download URL --role vitamin --vitamin screw
  --rank[=MODEL]        rank the results with the classifier model (default from ai.json `llm.classifier`,
                        else haiku) through omp, then show the ranking; 50 results per site unless --limit
                        Example: 3d models search "soil moisture sensor probe case" --rank
  --classifier M        classifier model (haiku-class), e.g. haiku or anthropic/claude-haiku-4-5
                        Example: 3d models search "cable clip" --classifier haiku
  --planner M           query planner model (sonnet-class); also --rewrite MODEL
                        Example: 3d models search "cable clip" --rank --planner sonnet
  --rewrite sonnet|MODEL|off
                        planner model, or `off` for no planner call (no rewrite, follow-ups or questions)
                        Example: 3d models search "cable clip" --rank --rewrite off
  --max-queries N       most queries the planner runs (>= 2: one wide and one narrow at least; default 4)
                        Example: 3d models search "little mole box for a plant probe" --rank --max-queries 6
  --max-calls N         most model calls per run (default 12; the guard against runaway cost)
                        Example: 3d models search "cable clip" --rank --max-calls 4
  --token-budget N      stop starting model calls once N tokens were used (default: no budget)
                        Example: 3d models search "cable clip" --rank --token-budget 200000
  --llm-timeout S       seconds one classifier call may take (default 120)
                        Example: 3d models search "cable clip" --rank --llm-timeout 60
  --batch-size N        results per classifier call (default 25)
                        Example: 3d models search "cable clip" --rank --batch-size 10
  --answer KEY=VALUE    answer a planner question (repeatable); saved, applied as a constraint, re-judges
                        Example: 3d models rerank latest --answer need_cad=yes
  --follow N            run suggested follow-up query N (repeatable; alias --followup)
                        Example: 3d models rerank latest --follow 1
  --refresh             ignore cached verdicts and judge again
                        Example: 3d models rerank latest --refresh
  --no-llm              never start omp: exactly the deterministic search (wins over ai.json llm.enabled)
                        Example: 3d models search "cable clip" --no-llm
  --llm auto|require    require: no omp -> exit 127 before the search (default auto: print the results and
                        one `LLM: unavailable (...)` line). Example: 3d models search "cable clip" --llm require
  --ndjson              stream events, one JSON object per line (`3d.models.rank/1`): result, verdict, plan,
                        query, question, done. Example: 3d models search "cable clip" --rank --ndjson
  --background          print the results, then judge in a detached `3d models rerank` process
                        Example: 3d models search "cable clip" --rank --background
  --wait                judge before returning (the default; accepted for scripts)
                        Example: 3d models search "cable clip" --rank --wait
  --json                machine-readable output (search: schema `3d.models.search/2`; with ranking
                        `3d.models.rank-result/1`)
                        Example: 3d models search "cable clip" --json

Credentials (environment variable, else OS keychain via keychain-cli, else
~/.config/3d-cli/auth.json):
  printables            none
  makerworld            search/show: none; download: MAKERWORLD_TOKEN / 3d-cli/makerworld-token
                        / makerworld.token
  thingiverse           THINGIVERSE_TOKEN / 3d-cli/thingiverse-token / thingiverse.token
  cults                 CULTS_USERNAME + CULTS_API_KEY / 3d-cli/cults3d-user + 3d-cli/cults3d-key
                        / cults.username + cults.api_key
MakerWorld needs a Chrome TLS fingerprint: curl_chrome146 on PATH (or
AXE_CURL_IMPERSONATE_DIR / THREED_CURL_IMPERSONATE) or the curl_cffi Python package.

Exit codes (the same with --json):
  0    done; for search, at least one site answered
  1    a site failed (blocked, not found, network), a paid model, or every searched
       site failed for different reasons
  2    bad usage, a non-empty --dir, or missing/refused credentials (also when every
       searched site lacks credentials)
  127  no Chrome-TLS tool for MakerWorld; `--llm require` without omp

Examples:
  3d models search "spoon drawer pull"
  3d models search "spoon drawer pull" --site printables,makerworld --limit 5
  3d models search "moisture sensor case" --limit 50 --columns author,downloads,cad,size,url
  3d models search "cable clip" --site thingiverse --json
  3d models search "soil moisture sensor probe case" --rank
  3d models search "little mole shaped box that holds a plant moisture probe" --rank --ndjson
  3d models rerank latest --answer need_cad=yes --follow 1
  3d models followups latest --json
  3d models searches show latest --json
  3d models show https://www.printables.com/model/311253-spoon-drawer-pull
  3d models show https://makerworld.com/en/models/1871683 --json
  3d models download https://www.printables.com/model/311253-spoon-drawer-pull
  3d models download https://www.printables.com/model/311253-spoon-drawer-pull --dir parts/pull --json
  3d models analyze parts/pull/spoon_handle.stl --json
  3d models auth set thingiverse
  3d models auth status"""


@dataclass
class Parsed:
    positional: list[str] = field(default_factory=list)
    opts: dict[str, str] = field(default_factory=dict)
    multi: dict[str, list[str]] = field(default_factory=dict)


def _parse(
    argv: list[str],
    *,
    value_flags: Collection[str] = (),
    bool_flags: Collection[str] = (),
    repeat_flags: Collection[str] = (),
    optional_flags: Collection[str] = (),
) -> Parsed:
    """Flags: `value_flags` take a value (`--x v` or `--x=v`), `repeat_flags` too but may repeat,
    `optional_flags` take a value only as `--x=v` (bare `--x` = "auto"), `bool_flags` take none."""
    parsed = Parsed()
    i = 0
    while i < len(argv):
        arg = argv[i]
        name, eq, inline = arg.partition("=")
        if name in value_flags or name in repeat_flags:
            if eq:
                value = inline
            elif i + 1 < len(argv):
                i += 1
                value = argv[i]
            else:
                raise UsageError(
                    f"{name} needs a value",
                    command="models",
                    remediation=["Run '3d models --help' for examples."],
                )
            if name in repeat_flags:
                parsed.multi.setdefault(name, []).append(value)
            else:
                parsed.opts[name] = value
        elif name in optional_flags:
            parsed.opts[name] = inline if eq else "auto"
        elif arg in bool_flags:
            parsed.opts[arg] = "1"
        elif arg.startswith("--"):
            raise UsageError(
                f"unknown option {arg}",
                command="models",
                remediation=["Run '3d models --help' for the accepted options."],
            )
        else:
            parsed.positional.append(arg)
        i += 1
    return parsed


def _one_positional(positional: list[str], what: str) -> str:
    if len(positional) != 1:
        hint = (
            'Quote a multi-word query: 3d models search "spoon drawer pull"'
            if what == "QUERY"
            else f"Pass exactly one {what}."
        )
        raise UsageError(
            f"expected exactly one {what}, got {len(positional)}",
            command="models",
            remediation=[hint],
        )
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


def _limit(raw: str | None, default: int = DEFAULT_LIMIT) -> int:
    if raw is None:
        return default
    if not (raw.isascii() and raw.isdecimal()) or not 1 <= int(raw) <= 100:
        raise InvalidArgument("--limit", raw, ["an integer 1..100"], command="models")
    return int(raw)


def _positive(flag: str, raw: str | None) -> int | None:
    if raw is None:
        return None
    if not (raw.isascii() and raw.isdecimal()) or int(raw) < 1:
        raise InvalidArgument(flag, raw, ["an integer >= 1"], command="models")
    return int(raw)


def _width() -> int:
    return shutil.get_terminal_size((120, 24)).columns


def _image_protocol(raw: str | None) -> str | None:
    """The terminal graphics protocol for `--images[=PROTO]`, or None (text only)."""
    from cli import termimage

    if raw is None or raw == "none":
        return None
    if raw == "auto":
        return termimage.detect(is_tty=sys.stdout.isatty())
    if raw not in termimage.PROTOCOLS:
        raise InvalidArgument(
            "--images", raw, ["auto", "none", *termimage.PROTOCOLS], command="models"
        )
    return raw


def _print_images_note() -> None:
    print(
        "images: no terminal graphics protocol detected (kitty, iterm2, sixel); "
        "text only. Force one with --images=kitty|iterm2|sixel.",
        file=sys.stderr,
    )


def _print_search(
    doc: dict[str, Any],
    failures: dict[str, ThreeDError],
    columns: tuple[str, ...],
    images: str | None,
) -> None:
    from library import images as imagelib
    from library import textview

    width = _width()
    pictures: dict[str, str] = {}
    protocol = _image_protocol(images)
    if images is not None and images != "none" and protocol is None:
        _print_images_note()
    if protocol is not None:
        pictures = imagelib.render_images(
            [
                (site, rec["image"])
                for site, recs in doc["results"].items()
                for rec in recs
                if rec.get("image")
            ],
            protocol,
        )
    for site in doc["sites"]:
        error = failures.get(site)
        if (
            error is None and site not in doc["results"]
        ):  # a persisted search whose site had failed
            failed = next((e for e in doc["errors"] if e["site"] == site), None)
            if failed is not None:
                print(f"{site}: unavailable — {failed['message']}")
                for step in failed.get("remediation", []):
                    print(f"  -> {step}")
            continue
        if error is not None:
            reason = (
                error.reason if isinstance(error, MarketplaceError) else error.message
            )
            print(f"{site}: unavailable — {reason}")
            for step in error.remediation:
                print(f"  -> {step}")
            continue
        records = doc["results"][site]
        took = doc["timings"].get(site)
        print(
            f"{site} ({len(records)})" + (f"  {took:.1f}s" if took is not None else "")
        )
        for warning in (w for w in doc["warnings"] if w["site"] == site):
            print(f"  note: {warning['message']}")
        for n, rec in enumerate(records, start=1):
            if rec.get("image") in pictures:
                sys.stdout.write(pictures[rec["image"]])
            for line in textview.render_result(rec, n, columns, width):
                print(line)
    for warning in (w for w in doc["warnings"] if w["site"] == "library"):
        print(f"note: {warning['message']}")
    if doc.get("search_id"):
        print(
            f"saved: search {doc['search_id']} (3d models searches show {doc['search_id']})"
        )


def _exit_for(failures: dict[str, ThreeDError], answered: int) -> int:
    """0 when any site answered; else the exit code every failure shares (2 for all-credentials), else 1."""
    if answered or not failures:
        return 0
    codes = {e.exit_code for e in failures.values()}
    return codes.pop() if len(codes) == 1 else 1


def _search(argv: list[str]) -> int:
    from library import rankcli
    from library import search as searchcore
    from library import textview

    p = _parse(
        argv,
        value_flags={
            "--site",
            "--limit",
            "--columns",
            "--for-vitamin",
            "--deep-max-mb",
            "--deep-top",
            "--db",
            "--idempotency-key",
            *rankcli.VALUE_FLAGS,
        },
        bool_flags={
            "--json",
            "--deep",
            "--cad",
            "--no-details",
            "--no-save",
            *rankcli.BOOL_FLAGS,
        },
        repeat_flags=rankcli.REPEAT_FLAGS,
        optional_flags={"--images", *rankcli.OPTIONAL_FLAGS},
    )
    query = _one_positional(p.positional, "QUERY")
    columns = textview.parse_columns(p.opts.get("--columns"))
    max_mb = _positive("--deep-max-mb", p.opts.get("--deep-max-mb"))
    vitamin = searchcore.check_vitamin(p.opts.get("--for-vitamin"))
    if vitamin is not None and "ref" not in columns and "--columns" not in p.opts:
        columns = (*columns, "ref")
    options = searchcore.SearchOptions(
        sites=_sites(p.opts.get("--site")),
        limit=_limit(p.opts.get("--limit")),
        details="--no-details" not in p.opts,
        deep="--deep" in p.opts,
        deep_top=_positive("--deep-top", p.opts.get("--deep-top")),
        deep_max_bytes=(max_mb or 25) * 1024 * 1024,
        only_cad="--cad" in p.opts,
        vitamin=vitamin,
        save="--no-save" not in p.opts,
        idempotency_key=p.opts.get("--idempotency-key"),
        db_path=p.opts.get("--db"),
    )
    rank = rankcli.rank_options(p.opts, p.multi)
    if rank is not None:
        return _search_ranked(p, query, options, rank, columns)
    run = searchcore.run_search(query, options)
    doc, failures = run.doc, run.failures
    only = (
        next(iter(failures.values())) if len(options.sites) == 1 and failures else None
    )
    if only is not None and "--json" not in p.opts:
        raise only  # the only site failed: its structured error, printed once
    if "--json" in p.opts:
        print(json.dumps(doc, indent=2, ensure_ascii=False))
    else:
        _print_search(doc, failures, columns, p.opts.get("--images"))
    return _exit_for(failures, len(doc["results"]))


def _stream(
    pipeline: Any, events: Any, p: Parsed, columns: tuple[str, ...], show: Any
) -> None:
    """Print one ranking run in the mode the flags chose: `--ndjson` events, `--json` document, or text."""
    from library import rankcli, rankflow

    if "--ndjson" in p.opts and "--json" in p.opts:
        raise UsageError(
            "--ndjson and --json are exclusive",
            command="models",
            remediation=[
                "Use --ndjson to stream events, --json for one document at the end."
            ],
        )
    if "--ndjson" in p.opts:
        rankcli.emit_ndjson(events)
    elif "--json" in p.opts:
        done = rankflow.collect(events)
        print(
            json.dumps(rankcli.result_doc(pipeline, done), indent=2, ensure_ascii=False)
        )
    else:
        rankcli.emit_text(events, show, width=_width())


def _search_ranked(
    p: Parsed, query: str, options: Any, rank: Any, columns: tuple[str, ...]
) -> int:
    """`search` with a ranking flag: phase 1 is the S5 view, phase 2 follows (docs/contracts/search-events.md)."""
    import dataclasses

    from library import rankcli, rankflow

    limit = rankcli.default_limit(p.opts, rank)
    if limit is not None:
        options = dataclasses.replace(options, limit=limit)
    if "--background" in p.opts and not (rank.enabled and options.save):
        raise UsageError(
            "--background needs ranking and the library database",
            command="models",
            remediation=["Add --rank, and drop --no-save / --no-llm."],
        )
    pipeline = rankflow.Pipeline(rank, options)
    events = pipeline.search(query)

    def show(_event: dict[str, Any], _extra: dict[str, Any]) -> None:
        only = (
            next(iter(pipeline.failures.values()))
            if len(options.sites) == 1 and pipeline.failures
            else None
        )
        if only is not None:
            raise only  # the only site failed: its structured error, printed once
        _print_search(pipeline.doc, pipeline.failures, columns, p.opts.get("--images"))

    if "--background" in p.opts:
        _background(pipeline, events, p, show)
    else:
        _stream(pipeline, events, p, columns, show)
    return _exit_for(pipeline.failures, len(pipeline.doc.get("results", {})))


def _background(pipeline: Any, events: Any, p: Parsed, show: Any) -> None:
    """Print phase 1, then hand the model work to a detached `3d models rerank` process."""
    import contextlib

    from library import rankcli

    reached: list[bool] = []

    def phase_one() -> Any:
        with contextlib.closing(events) as stream:
            for event in stream:
                if event["event"] == "phase":
                    reached.append(True)
                    return
                yield event

    rankcli.emit_text(phase_one(), show, width=_width())
    if reached and pipeline.root_uid is not None:
        tail: list[str] = []
        for flag in (
            "--classifier",
            "--planner",
            "--rewrite",
            "--max-queries",
            "--max-calls",
            "--token-budget",
            "--llm-timeout",
            "--batch-size",
            "--db",
        ):
            if flag in p.opts:
                tail += [flag, p.opts[flag]]
        if "--classifier" not in p.opts and p.opts.get("--rank", "auto") != "auto":
            tail += ["--classifier", p.opts["--rank"]]
        for key, value in pipeline.answers.items():
            tail += ["--answer", f"{key}={value}"]
        pid = rankcli.spawn_background(str(pipeline.root_uid), tail)
        print(
            f"ranking continues in the background (pid {pid}); read it with: 3d models searches show {pipeline.root_uid}"
        )


def _open_library(db_path: str | None) -> Any:
    from library import db as librarydb

    return librarydb.open_library(db_path)


def _searches(argv: list[str]) -> int:
    from library import search as searchcore
    from library import textview

    action, rest = (argv[0], argv[1:]) if argv else ("", [])
    if action == "list":
        p = _parse(rest, value_flags={"--limit", "--db"}, bool_flags={"--json"})
        if p.positional:
            raise UsageError(
                "searches list takes no arguments",
                command="models",
                remediation=["Run '3d models searches list [--limit N] [--json]'."],
            )
        conn = _open_library(p.opts.get("--db"))
        try:
            sessions = searchcore.list_sessions(
                conn, limit=_limit(p.opts.get("--limit"), 20)
            )
        finally:
            conn.close()
        if "--json" in p.opts:
            print(
                json.dumps(
                    {"schema": "3d.models.searches/1", "searches": sessions},
                    indent=2,
                    ensure_ascii=False,
                )
            )
            return 0
        if not sessions:
            print("no saved searches")
        for s in sessions:
            hits = (
                "  ".join(f"{site} {n}" for site, n in s["counts"].items())
                or "no results"
            )
            print(
                f"{s['id']:>4}  {s['created_at'][:19]}  {s['kind']:<8} {s['query']!r}  {hits}"
            )
            print(f"      {s['search_id']}")
        return 0
    if action == "show":
        p = _parse(
            rest,
            value_flags={"--columns", "--db"},
            bool_flags={"--json"},
            optional_flags={"--images"},
        )
        ref = _one_positional(p.positional, "ID")
        columns = textview.parse_columns(p.opts.get("--columns"))
        conn = _open_library(p.opts.get("--db"))
        try:
            doc = searchcore.session_doc(conn, ref)
        finally:
            conn.close()
        if "--json" in p.opts:
            print(json.dumps(doc, indent=2, ensure_ascii=False))
        else:
            print(
                f"search {doc['search_id']}  {doc['created_at'][:19]}  {doc['query']!r}"
            )
            _print_search(doc, {}, columns, p.opts.get("--images"))
            for q in doc.get("queries", []):
                where = f"  (search {q['search']})" if q.get("search") else ""
                print(f"query [{q['kind']}] {q['query']!r}  {q['status']}{where}")
            for q in doc.get("questions", []):
                choices = f"  [{' | '.join(q['choices'])}]" if q["choices"] else ""
                answer = f"  = {q['answer']}" if q["answer"] is not None else ""
                print(
                    f"question {q['key']} ({q['type']}): {q['prompt']}{choices}{answer}"
                )
        return 0
    raise InvalidArgument(
        "searches action", action or "(none)", ["list", "show"], command="models"
    )


def _print_model(model: Model) -> None:
    from library import search as searchcore

    record = searchcore.result_record(model, 0)
    print(model.title)
    print(f"  site      {model.site} #{model.id}")
    print(f"  url       {model.url}")
    print(
        f"  author    {model.author}"
        + (f"  {model.author_url}" if model.author_url else "")
    )
    print(f"  license   {model.license or '?'}")
    print(f"  price     {model.price or 'free'}")
    if model.date:
        print(f"  date      {model.date[:10]}")
    if model.category:
        print(f"  category  {model.category}")
    counts = [
        f"{label} {value}"
        for label, value in (
            ("likes", model.likes),
            ("downloads", model.downloads),
            ("makes", model.makes),
            ("views", model.views),
        )
        if value is not None
    ]
    if counts:
        print(f"  counts    {'  '.join(counts)}")
    if model.tags:
        print(f"  tags      {', '.join(model.tags)}")
    if model.summary:
        print(f"  summary   {model.summary}")
    if model.image:
        print(f"  image     {model.image}")
    if model.files_known:
        print(
            f"  CAD       {'yes (' + ', '.join(record['cad_formats']) + ')' if record['cad'] else 'no'}"
        )
    print(f"  files     {len(model.files)}")
    for f in model.files:
        size = f" ({f.size} B)" if f.size is not None else ""
        print(f"    {f.name}{size}")


def _show(argv: list[str]) -> int:
    from marketplaces import catalog

    p = _parse(argv, bool_flags={"--json"})
    model = catalog.show(_one_positional(p.positional, "URL"))
    if "--json" in p.opts:
        print(json.dumps(model.to_json(), indent=2, ensure_ascii=False))
    else:
        _print_model(model)
    return 0


def _print_mesh(mesh: dict[str, Any]) -> None:
    if mesh.get("status") != "ok":
        print(f"    mesh: {mesh.get('status')} — {mesh.get('reason', '')}")
        return
    x, y, z = mesh["bbox_mm"]
    volume = (
        f"{mesh['volume_mm3'] / 1000:.2f} cm3"
        if mesh["volume_mm3"] is not None
        else "n/a (not watertight)"
    )
    print(
        f"    bbox {x:g}x{y:g}x{z:g} mm  volume {volume}  triangles {mesh['triangles']}  bodies {mesh['body_count']}"
    )
    print(
        f"    watertight {'yes' if mesh['watertight'] else 'no'}  manifold {'yes' if mesh['manifold'] else 'no'}"
        f"  footprint {mesh['est_print_footprint_mm'][0]:g}x{mesh['est_print_footprint_mm'][1]:g} mm"
        f"  height {mesh['est_print_height_mm']:g} mm  [{mesh['dimension_class']}]"
        + ("  UNITS SUSPECT" if mesh["units_suspect"] else "")
    )


def _print_analytics(report: dict[str, Any]) -> None:
    facts = report["facts"]
    print(
        f"  CAD: {'yes (' + ', '.join(facts['cad_formats']) + ')' if facts['cad'] else 'no'}  "
        f"files {facts['file_count']}  parts {facts['part_count']}  size {facts['size_class']}"
        + ("  multi-part" if facts["is_multi_part"] else "")
        + ("  assembly" if facts["is_assembly"] else "")
    )
    for f in report["files"]:
        if "mesh" in f:
            print(f"  {f['name']}")
            _print_mesh(f["mesh"])


def _download(argv: list[str]) -> int:
    from library import sources

    p = _parse(
        argv,
        value_flags={"--dir", "--role", "--vitamin", "--db"},
        repeat_flags={"--file"},
        bool_flags={"--json", "--deep", "--no-save"},
    )
    url = _one_positional(p.positional, "URL")
    dest = Path(p.opts["--dir"]).expanduser() if "--dir" in p.opts else None
    result = sources.download_model(
        url,
        dest,
        select=p.multi.get("--file"),
        role=p.opts.get("--role"),
        vitamin_kind=p.opts.get("--vitamin"),
        deep="--deep" in p.opts,
        save="--no-save" not in p.opts,
        db_path=p.opts.get("--db"),
    )
    if "--json" in p.opts:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    model = result["model"]
    print(f"{model['title']} — {model['author']}, {model['license'] or 'license ?'}")
    target = Path(result["dir"])
    for f in result["files"]:
        print(f"  {target / f['name']}  ({f['size']} B)")
    print(f"  {result['metadata']}")
    _print_analytics(result["analytics"])
    source = result["source"]
    if source is not None:
        role = f"  role {source['role']}" if source["role"] else ""
        print(f"  recorded model_source {source['uid']}{role}")
    for warning in result["warnings"]:
        print(f"  warning: {warning['message']}", file=sys.stderr)
    return 0


def _analyze(argv: list[str]) -> int:
    from library import sources

    p = _parse(argv, value_flags={"--db"}, bool_flags={"--json", "--no-save"})
    if not p.positional:
        raise UsageError(
            "expected at least one FILE",
            command="models",
            remediation=["Run '3d models analyze part.stl [more files] [--json]'."],
        )
    paths = [Path(a).expanduser() for a in p.positional]
    for path in paths:
        if not path.is_file():
            raise InputNotFound(str(path), command="models")
    conn = None
    if "--no-save" not in p.opts:
        try:
            conn = _open_library(p.opts.get("--db"))
        except Exception as exc:  # noqa: BLE001 — the cache is optional: an unwritable DB must not block analysis
            print(
                f"note: library database unavailable, no analytics cache: {exc}",
                file=sys.stderr,
            )
    try:
        report = sources.analyze_paths(paths, conn, deep=True)
    finally:
        if conn is not None:
            conn.close()
    if "--json" in p.opts:
        print(
            json.dumps(
                {"schema": "3d.models.analyze/1", **report},
                indent=2,
                ensure_ascii=False,
            )
        )
        return 0
    _print_analytics(report)
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
        value = (
            getpass.getpass(f"{prompt}: ")
            if sys.stdin.isatty()
            else sys.stdin.readline()
        )
    except EOFError:
        value = ""  # Ctrl-D at the prompt: same answer as an empty value
    value = value.strip()
    if not value:
        raise UsageError(
            f"empty {prompt}",
            command="models",
            remediation=[
                "Type the value at the prompt, or pipe it: "
                "printf '%s\\n' \"$TOKEN\" | 3d models auth set SITE"
            ],
        )
    return value


def _auth(argv: list[str]) -> int:
    from marketplaces import credentials

    action, rest = (argv[0], argv[1:]) if argv else ("", [])
    if action == "status":
        p = _parse(rest, bool_flags={"--json"})
        if p.positional:
            raise UsageError(
                "auth status takes no arguments",
                command="models",
                remediation=["Run '3d models auth status'."],
            )
        keychain = credentials.secret_store() is not None
        if keychain:
            state = "available"
        elif credentials.keychain_disabled():
            state = "switched off (THREED_KEYCHAIN=off)"
        else:
            state = "not available (keychain-cli not installed)"
        report = {
            site: {f.key: credentials.sources(f) for f in fields}
            for site, fields in credentials.FIELDS.items()
        }
        if "--json" in p.opts:
            print(json.dumps({"keychain": keychain, "sites": report}, indent=2))
            return 0
        print(f"keychain: {state}")
        for site, fields in report.items():
            for key, found in fields.items():
                print(
                    f"  {site}.{key}: {', '.join(found) if found else 'not configured'}"
                )
        return 0
    if action == "set":
        site = _auth_site(_parse(rest).positional)
        store = credentials.require_store()
        values = [(f, _read_secret(f.prompt)) for f in credentials.FIELDS[site]]
        for item, value in values:
            store.set(item.keychain, value, label=f"3d-cli {item.prompt}")
            print(f"stored {item.keychain} in the keychain")
        return 0
    if action == "delete":
        site = _auth_site(_parse(rest).positional)
        store = credentials.require_store()
        for item in credentials.FIELDS[site]:
            print(
                f"{'deleted' if store.delete(item.keychain) else 'absent'} {item.keychain}"
            )
        return 0
    raise InvalidArgument(
        "auth action", action or "(none)", ["status", "set", "delete"], command="models"
    )


def _rank_flags() -> dict[str, Any]:
    from library import rankcli

    return {
        "value_flags": {"--db", "--site", "--limit", "--columns", *rankcli.VALUE_FLAGS},
        "bool_flags": {"--json", *rankcli.BOOL_FLAGS},
        "repeat_flags": rankcli.REPEAT_FLAGS,
        "optional_flags": {"--images"},
    }


def _rerank(argv: list[str]) -> int:
    from library import rankcli, rankflow
    from library import search as searchcore
    from library import textview

    p = _parse(argv, **_rank_flags())
    ref = _one_positional(p.positional, "ID")
    columns = textview.parse_columns(p.opts.get("--columns"))
    db_path = p.opts.get("--db")
    conn = _open_library(db_path)
    try:
        stored = searchcore.session_doc(conn, ref)
    finally:
        conn.close()
    sites = (
        _sites(p.opts["--site"])
        if "--site" in p.opts
        else list(stored["sites"] or SITES)
    )
    limit = _limit(
        p.opts.get("--limit"), int(stored["limit"] or rankcli.RANK_DEFAULT_LIMIT)
    )
    options = searchcore.SearchOptions(sites=sites, limit=limit, db_path=db_path)
    rank = rankcli.rank_options(p.opts, p.multi, rerank=True)
    assert rank is not None  # rerank=True always ranks
    pipeline = rankflow.Pipeline(rank, options)
    events = pipeline.session(str(stored["search_id"]))

    def show(_event: dict[str, Any], _extra: dict[str, Any]) -> None:
        print(
            f"search {pipeline.doc['search_id']}  {pipeline.doc['created_at'][:19]}  {pipeline.doc['query']!r}"
        )
        _print_search(pipeline.doc, {}, columns, p.opts.get("--images"))

    _stream(pipeline, events, p, columns, show)
    return 0


def _followups(argv: list[str]) -> int:
    from library import rankstore

    p = _parse(argv, **_rank_flags())
    if p.multi.get("--answer") or p.multi.get("--follow") or p.multi.get("--followup"):
        return _rerank(argv)  # answering or following IS a rerank
    ref = _one_positional(p.positional, "ID")
    conn = _open_library(p.opts.get("--db"))
    try:
        row = rankstore.session_row(conn, ref)
        root = int(row["id"])
        planned = rankstore.list_plan_queries(conn, root)
        doc = {
            "schema": "3d.models.followups/1",
            "search": row["uid"],
            "query": row["query"],
            "answers": rankstore.answers_map(conn, root),
            "questions": rankstore.list_questions(conn, root),
            "followups": [
                {k: v for k, v in q.items() if k != "row_id"}
                for q in planned
                if q["kind"] == "followup"
            ],
            "queries": [
                {k: v for k, v in q.items() if k != "row_id"}
                for q in planned
                if q["kind"] != "followup"
            ],
        }
    finally:
        conn.close()
    if "--json" in p.opts:
        print(json.dumps(doc, indent=2, ensure_ascii=False))
        return 0
    print(f"search {doc['search']}  {doc['query']!r}")
    if not (doc["questions"] or doc["followups"] or doc["queries"]):
        print(
            "nothing planned yet: run `3d models search QUERY --rank` or `3d models rerank ID`"
        )
    for q in doc["queries"]:
        print(
            f"query [{q['kind']}] {q['query']!r}  {q['status']}"
            + (f"  (search {q['search']})" if q["search"] else "")
        )
    for q in doc["questions"]:
        choices = f"  [{' | '.join(q['choices'])}]" if q["choices"] else ""
        answer = f"  = {q['answer']}" if q["answer"] is not None else ""
        print(f"question {q['key']} ({q['type']}): {q['prompt']}{choices}{answer}")
    for q in doc["followups"]:
        why = f"  — {q['rationale']}" if q["rationale"] else ""
        print(f"follow-up {q['n']}: {q['query']!r}  {q['status']}{why}")
    return 0


SUBCOMMANDS = {
    "search": _search,
    "searches": _searches,
    "rerank": _rerank,
    "followups": _followups,
    "show": _show,
    "download": _download,
    "analyze": _analyze,
    "auth": _auth,
}


def run(argv: list[str]) -> int:
    if not argv:
        print(USAGE)
        return 1
    if argv[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0
    sub = SUBCOMMANDS.get(argv[0])
    if sub is None:
        raise InvalidArgument(
            "subcommand", argv[0], list(SUBCOMMANDS), command="models"
        )
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
