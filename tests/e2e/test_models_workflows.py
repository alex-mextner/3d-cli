"""Readable e2e workflows for `3d models` (#401): the real bin/3d against loopback fakes of
Printables, MakerWorld, Thingiverse and Cults3D (tests/marketplace_fixtures.py) — search
across the sites, show, download with model.json, the paid-model refusal and the
missing-credential reasons. No real site is contacted."""

from __future__ import annotations

import json
import re
from pathlib import Path

from marketplace_fixtures import (
    CULTS_KEY,
    CULTS_USER,
    FILES,
    MAKERWORLD_TOKEN,
    THINGIVERSE_TOKEN,
    MarketplaceServer,
    write_fake_curl,
)

from .workflow_helper import REPO_ROOT, run_cli

SPOON = "https://www.printables.com/model/311253-spoon-drawer-pull"
CREDENTIALS = {
    "THINGIVERSE_TOKEN": THINGIVERSE_TOKEN,
    "CULTS_USERNAME": CULTS_USER,
    "CULTS_API_KEY": CULTS_KEY,
    "MAKERWORLD_TOKEN": MAKERWORLD_TOKEN,
}


def _env(server: MarketplaceServer, tmp_path: Path, **extra: str) -> dict[str, str]:
    """Loopback API URLs plus the fake curl; the host's own marketplace credentials are
    blanked (an empty variable counts as unset) so a developer's real token never leaks in."""
    binary, _ = write_fake_curl(tmp_path / "curl-bin")
    blank = dict.fromkeys(CREDENTIALS, "")
    return {
        **server.env(),
        "THREED_CURL_IMPERSONATE": str(binary),
        "THREED_KEYCHAIN": "off",
        **blank,
        **extra,
    }


def test_search_lists_all_four_sites_side_by_side(tmp_path: Path) -> None:
    """With every credential set, one search prints each site's hits with author, counts,
    license, price and URL; Cults' paid hit shows its EUR price."""
    with MarketplaceServer() as server:
        result = run_cli(
            tmp_path,
            "models",
            "search",
            "spoon drawer pull",
            "--limit",
            "2",
            env_extra=_env(server, tmp_path, **CREDENTIALS),
        )
    assert result.returncode == 0, result.stderr
    out = result.stdout
    for header in ("printables (2)", "makerworld (2)", "thingiverse (2)", "cults (2)"):
        assert header in out
    assert "by cluckOmatic  likes 58  downloads 180  CC0  free" in out
    assert SPOON in out
    assert (
        "by spoonmaker  likes 42  downloads 900  Creative Commons - Attribution  free"
        in out
    )
    assert (
        "Luxury spoon pull\n     by paidmaker  likes 7  downloads 30  CC BY  4.99 EUR"
        in out
    )


def test_search_without_credentials_reports_each_reason_and_keeps_the_rest(
    tmp_path: Path,
) -> None:
    """No tokens: Printables and MakerWorld still answer; Thingiverse and Cults print why they
    are unavailable and how to fix it; the command still exits 0."""
    with MarketplaceServer() as server:
        result = run_cli(
            tmp_path,
            "models",
            "search",
            "spoon drawer pull",
            env_extra=_env(server, tmp_path),
        )
        as_json = run_cli(
            tmp_path,
            "models",
            "search",
            "spoon drawer pull",
            "--json",
            env_extra=_env(server, tmp_path),
        )
    assert result.returncode == 0, result.stderr
    assert (
        "thingiverse: unavailable — the Thingiverse API needs an App Token"
        in result.stdout
    )
    assert (
        "-> Create a free app at https://www.thingiverse.com/apps/create"
        in result.stdout
    )
    assert (
        "cults: unavailable — the Cults3D GraphQL API needs a username and API key"
        in result.stdout
    )
    payload = json.loads(as_json.stdout)
    assert sorted(payload["results"]) == ["makerworld", "printables"]
    assert {(e["site"], e["kind"]) for e in payload["errors"]} == {
        ("thingiverse", "auth"),
        ("cults", "auth"),
    }


def test_single_failing_site_exits_with_its_structured_error(tmp_path: Path) -> None:
    """The user searches only Thingiverse without a token: the command exits 2 with the
    structured error naming the missing App Token instead of an empty list; with --json the
    same exit code comes with the error object on stdout."""
    with MarketplaceServer() as server:
        result = run_cli(
            tmp_path,
            "models",
            "search",
            "spoon",
            "--site",
            "thingiverse",
            env_extra=_env(server, tmp_path),
        )
        as_json = run_cli(
            tmp_path,
            "models",
            "search",
            "spoon",
            "--site",
            "thingiverse",
            "--json",
            env_extra=_env(server, tmp_path),
        )
    assert result.returncode == 2
    assert "thingiverse: the Thingiverse API needs an App Token" in result.stderr
    assert as_json.returncode == 2, as_json.stderr
    payload = json.loads(as_json.stdout)
    assert payload["results"] == {} and [e["kind"] for e in payload["errors"]] == [
        "auth"
    ]


def test_search_where_every_site_fails_exits_with_the_shared_code(
    tmp_path: Path,
) -> None:
    """Thingiverse and Cults both lack credentials: no site answered and every failure is
    an auth error, so the search exits 2; mixing in a site with a different failure
    (MakerWorld's Chrome-TLS curl cannot be started -> network) collapses to exit 1. Both
    still list every error in the JSON; a THREED_CURL_IMPERSONATE that names no executable is a
    usage error (exit 2) rather than a silent fallback (#504)."""
    broken = tmp_path / "curl-broken"
    broken.write_text("#!/bin/sh\nexit 7\n", encoding="utf-8")
    broken.chmod(0o755)
    with MarketplaceServer() as server:
        auth = run_cli(
            tmp_path,
            "models",
            "search",
            "spoon",
            "--site",
            "thingiverse,cults",
            "--json",
            env_extra=_env(server, tmp_path),
        )
        mixed = run_cli(
            tmp_path,
            "models",
            "search",
            "spoon",
            "--site",
            "thingiverse,makerworld",
            "--json",
            env_extra={
                **_env(server, tmp_path),
                "THREED_CURL_IMPERSONATE": str(broken),
            },
        )
        unset = run_cli(
            tmp_path,
            "models",
            "search",
            "spoon",
            "--site",
            "makerworld",
            "--json",
            env_extra={
                **_env(server, tmp_path),
                "THREED_CURL_IMPERSONATE": str(tmp_path / "missing"),
            },
        )
    assert auth.returncode == 2, auth.stderr
    assert [e["kind"] for e in json.loads(auth.stdout)["errors"]] == ["auth", "auth"]
    assert mixed.returncode == 1, mixed.stderr
    kinds = {e["site"]: e["kind"] for e in json.loads(mixed.stdout)["errors"]}
    assert kinds == {"thingiverse": "auth", "makerworld": "network"}
    assert unset.returncode == 2, unset.stderr
    [error] = json.loads(unset.stdout)["errors"]
    assert "THREED_CURL_IMPERSONATE is not an executable file" in error["message"]


def test_download_never_writes_into_a_non_empty_dir(tmp_path: Path) -> None:
    """--dir pointing at a directory that already holds files is refused (exit 2) before any
    file request and the files stay untouched; an existing empty directory is filled."""
    busy, empty = tmp_path / "busy", tmp_path / "empty"
    busy.mkdir()
    empty.mkdir()
    (busy / "keep.txt").write_text("mine", encoding="utf-8")
    with MarketplaceServer() as server:
        refused = run_cli(
            tmp_path,
            "models",
            "download",
            SPOON,
            "--dir",
            str(busy),
            env_extra=_env(server, tmp_path),
        )
        fetched = [path for _, path, _ in server.requests if path.startswith("/files/")]
        filled = run_cli(
            tmp_path,
            "models",
            "download",
            SPOON,
            "--dir",
            str(empty),
            env_extra=_env(server, tmp_path),
        )
    assert refused.returncode == 2
    assert "download directory is not empty" in refused.stderr
    assert fetched == [] and [p.name for p in busy.iterdir()] == ["keep.txt"]
    assert (busy / "keep.txt").read_text(encoding="utf-8") == "mine"
    assert filled.returncode == 0, filled.stderr
    assert sorted(p.name for p in empty.iterdir()) == ["model.json", "spoon_handle.stl"]


def test_show_prints_metadata_and_files(tmp_path: Path) -> None:
    """The user opens a Printables model and a MakerWorld design: license, files and sizes
    print as text, and `--json` gives the same model as an object."""
    with MarketplaceServer() as server:
        shown = run_cli(
            tmp_path, "models", "show", SPOON, env_extra=_env(server, tmp_path)
        )
        mw = run_cli(
            tmp_path,
            "models",
            "show",
            "https://makerworld.com/en/models/1871683",
            "--json",
            env_extra=_env(server, tmp_path),
        )
    assert shown.returncode == 0, shown.stderr
    assert (
        "  license   CC0" in shown.stdout
        and "    spoon_handle.stl (3306984 B)" in shown.stdout
    )
    design = json.loads(mw.stdout)
    assert design["author"] == "zentenro" and [f["name"] for f in design["files"]] == [
        "Handler-120.stl",
        "Handler-DG.stl",
    ]


def test_download_saves_files_and_model_json_for_each_site(tmp_path: Path) -> None:
    """Printables needs no login; Thingiverse/Cults/MakerWorld download with the configured
    credentials; every directory gets a model.json naming author, license and source."""
    cases = {
        SPOON: ("spoon_handle.stl", "cluckOmatic", "CC0"),
        "https://www.thingiverse.com/thing:5594736": (
            "spoon_pull.stl",
            "spoonmaker",
            "Creative Commons - Attribution",
        ),
        "https://cults3d.com/en/3d-model/home/spoon-drawer-pull": (
            "spoon-pull.stl",
            "kitchenfab",
            "CC BY",
        ),
        "https://makerworld.com/en/models/1871683": (
            "makerworld-1871683.zip",
            "zentenro",
            "Standard Digital File License",
        ),
    }
    with MarketplaceServer() as server:
        for n, (url, (name, author, lic)) in enumerate(cases.items()):
            dest = tmp_path / f"d{n}"
            result = run_cli(
                tmp_path,
                "models",
                "download",
                url,
                "--dir",
                str(dest),
                "--json",
                env_extra=_env(server, tmp_path, **CREDENTIALS),
            )
            assert result.returncode == 0, result.stderr
            assert (dest / name).read_bytes() == FILES[name]
            meta = json.loads((dest / "model.json").read_text(encoding="utf-8"))
            assert (meta["author"]["name"], meta["license"]) == (author, lic)
            assert meta["source"]["url"].startswith("https://")
            assert json.loads(result.stdout)["metadata"] == str(dest / "model.json")
    assert (tmp_path / "d1" / "spoon_pull_v2.stl").read_bytes() == FILES[
        "spoon_pull_v2.stl"
    ]


def test_download_default_dir_and_text_output(tmp_path: Path) -> None:
    """Without --dir the files land in ./<site>-<id>-<title>/ and the text output names the
    model, its author and license."""
    with MarketplaceServer() as server:
        result = run_cli(
            tmp_path, "models", "download", SPOON, env_extra=_env(server, tmp_path)
        )
    assert result.returncode == 0, result.stderr
    target = tmp_path / "printables-311253-spoon-drawer-pull"
    assert (target / "spoon_handle.stl").is_file() and (target / "model.json").is_file()
    assert result.stdout.startswith("Spoon Drawer Pull — cluckOmatic, CC0")


def test_paid_model_is_refused_before_any_file_request(tmp_path: Path) -> None:
    """The user asks for a priced Cults creation: the command refuses with the price, fetches
    no file and creates no directory."""
    with MarketplaceServer() as server:
        result = run_cli(
            tmp_path,
            "models",
            "download",
            "https://cults3d.com/en/3d-model/home/luxury-spoon-pull",
            "--dir",
            str(tmp_path / "lux"),
            env_extra=_env(server, tmp_path, **CREDENTIALS),
        )
        fetched = [path for _, path, _ in server.requests if path.startswith("/files/")]
    assert result.returncode == 1
    assert "is a paid model (4.99 EUR)" in result.stderr
    assert fetched == [] and not (tmp_path / "lux").exists()


def test_makerworld_download_without_login_says_how_to_log_in(tmp_path: Path) -> None:
    """MakerWorld lists files to anyone but serves them only to a logged-in account: without a
    token the download exits 2 and says how to get one."""
    with MarketplaceServer() as server:
        result = run_cli(
            tmp_path,
            "models",
            "download",
            "https://makerworld.com/en/models/1871683",
            env_extra=_env(server, tmp_path),
        )
    assert result.returncode == 2
    assert "MakerWorld only serves files to a logged-in account" in result.stderr


def test_bad_invocations_are_structured(tmp_path: Path) -> None:
    """Typos get structured errors: an unknown site lists the accepted ones, a foreign URL
    lists the accepted URL forms, and a bare `3d models` prints the usage."""
    site = run_cli(tmp_path, "models", "search", "x", "--site", "etsy")
    assert site.returncode == 2
    assert "accepted: printables, makerworld, thingiverse, cults" in site.stderr
    url = run_cli(tmp_path, "models", "show", "https://example.com/thing")
    assert url.returncode == 2
    assert "not a model URL of a supported site" in url.stderr
    limit = run_cli(tmp_path, "models", "search", "x", "--limit", "0")
    assert limit.returncode == 2
    assert "an integer 1..100" in limit.stderr
    bare = run_cli(tmp_path, "models")
    assert bare.returncode == 1
    assert bare.stdout.startswith("3d models <search|searches|rerank|followups|show|download|analyze|publish|auth>")


def test_auth_status_names_sources_but_never_values(tmp_path: Path) -> None:
    """`3d models auth status` through bin/3d: an env token and an auth.json key are listed by
    source name, the values never appear, and the switched-off keychain says so."""
    auth = tmp_path / "xdg-config" / "3d-cli" / "auth.json"
    auth.parent.mkdir(parents=True, exist_ok=True)
    auth.write_text(
        json.dumps({"cults": {"api_key": "cults-secret-x"}}), encoding="utf-8"
    )
    env = {
        **dict.fromkeys(CREDENTIALS, ""),
        "THREED_KEYCHAIN": "off",
        "THINGIVERSE_TOKEN": "tv-secret-x",
    }
    result = run_cli(tmp_path, "models", "auth", "status", env_extra=env)
    assert result.returncode == 0, result.stderr
    out = result.stdout
    assert "keychain: switched off (THREED_KEYCHAIN=off)" in out
    assert "thingiverse.token: env THINGIVERSE_TOKEN" in out
    assert "cults.api_key: auth.json cults.api_key" in out
    assert "cults.username: not configured" in out
    assert "secret-x" not in out + result.stderr
    as_json = json.loads(
        run_cli(tmp_path, "models", "auth", "status", "--json", env_extra=env).stdout
    )
    assert as_json["keychain"] is False
    assert as_json["sites"]["makerworld"]["token"] == []


def test_help_and_docs_list_the_same_flags_and_examples(tmp_path: Path) -> None:
    """`3d models --help` and docs/commands/models.md list the same flags, and every help
    example is copy-pasteable from the doc."""
    result = run_cli(tmp_path, "models", "--help")
    assert result.returncode == 0
    doc = (REPO_ROOT / "docs" / "commands" / "models.md").read_text(encoding="utf-8")
    flags = set(re.findall(r"--[a-z][a-z-]+", result.stdout))
    assert flags == {
        "--site",
        "--limit",
        "--dir",
        "--json", "--platform", "--dry-run", "--yes",
        "--images",
        "--columns",
        "--deep",
        "--deep-max-mb", "--deep-top",
        "--cad",
        "--for-vitamin",
        "--no-details",
        "--no-save",
        "--db", "--idempotency-key",
        "--file",
        "--role",
        "--vitamin",
        "--rank",
        "--classifier",
        "--planner",
        "--rewrite",
        "--max-queries",
        "--max-calls",
        "--token-budget",
        "--llm-timeout",
        "--batch-size",
        "--answer",
        "--follow",
        "--followup",
        "--refresh",
        "--no-llm",
        "--llm",
        "--ndjson",
        "--background",
        "--wait",
    }
    for flag in flags:
        assert f"`{flag}" in doc, f"{flag} undocumented in docs/commands/models.md"
    examples = re.findall(r"^  (3d models .+)$", result.stdout, re.M)
    assert len(examples) >= 7
    for example in examples:
        assert example in doc, (
            f"example missing from docs/commands/models.md: {example}"
        )
