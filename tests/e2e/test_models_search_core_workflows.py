"""Readable e2e workflows for the search core of `3d models` (#576): the real bin/3d against generated 50+-result
loopback fakes of the four sites (tests/search_fixtures.py). Search with 50 results per site and richer JSON,
saved searches that round-trip, the terminal view, deep file analytics, reference downloads recorded as
`model_source`, and the offline `analyze`. No real site is contacted."""

from __future__ import annotations

import json
import re
import zipfile
from pathlib import Path
from typing import Any

from marketplace_fixtures import write_fake_curl
from search_fixtures import BulkServer, cube_stl

from .workflow_helper import run_cli

SITES = ("printables", "makerworld", "thingiverse", "cults")
PRINTABLES_1 = "https://www.printables.com/model/1-model-1"


def _env(server: BulkServer, tmp_path: Path, **extra: str) -> dict[str, str]:
    binary, _ = write_fake_curl(tmp_path / "curl-bin")
    return {
        **server.env(),
        "THREED_CURL_IMPERSONATE": str(binary),
        "THREED_KEYCHAIN": "off",
        **extra,
    }


def _search_json(tmp_path: Path, server: BulkServer, *args: str) -> dict[str, Any]:
    result = run_cli(
        tmp_path,
        "models",
        "search",
        "case",
        "--json",
        *args,
        env_extra=_env(server, tmp_path),
    )
    assert result.returncode == 0, result.stderr
    doc: dict[str, Any] = json.loads(result.stdout)
    return doc


def test_search_returns_fifty_results_per_site_with_the_new_fields_and_keeps_the_old_keys(
    tmp_path: Path,
) -> None:
    """A user asks for 50 per site as JSON: every site returns 50 cards that still carry every key the previous
    version printed, plus image, summary, cad flags, analytics and a search id; size_mm is absent when
    nothing was measured."""
    with BulkServer(total=120) as server:
        doc = _search_json(tmp_path, server, "--limit", "50")
    assert (
        doc["schema"] == "3d.models.search/2"
        and doc["query"] == "case"
        and doc["errors"] == []
    )
    assert isinstance(doc["search_id"], str) and len(str(doc["search_id"])) == 36
    results = doc["results"]
    assert isinstance(results, dict) and sorted(results) == sorted(SITES)
    for site in SITES:
        cards = results[site]
        assert len(cards) == 50, site
        card = cards[4]  # id 5: CAD listing on every fake site
        for key in (
            "site",
            "id",
            "title",
            "url",
            "author",
            "likes",
            "downloads",
            "license",
            "price",
            "paid",
            "summary",
            "tags",
            "image",
            "files",
        ):
            assert key in card, (site, key)
        assert (
            card["cad"] is True
            and "step" in card["cad_formats"]
            and "size_mm" not in card
        )
        assert card["analytics"]["has_stl"] is True and card["files"][0]["class"] in (
            "mesh",
            "cad",
        )
        assert card["image"].startswith("http") and card["rank"] == 5


def test_a_saved_search_comes_back_exactly_through_searches_show(
    tmp_path: Path,
) -> None:
    """The search is saved; `searches list` names it and `searches show ID --json` returns the same results,
    errors and timings; `latest` and the numeric id reach the same session."""
    env_server = BulkServer(total=60)
    with env_server as server:
        first = _search_json(
            tmp_path, server, "--limit", "20", "--site", "printables,cults"
        )
        env = _env(server, tmp_path)
    listed = json.loads(
        run_cli(tmp_path, "models", "searches", "list", "--json", env_extra=env).stdout
    )
    assert listed["schema"] == "3d.models.searches/1" and len(listed["searches"]) == 1
    entry = listed["searches"][0]
    assert entry["search_id"] == first["search_id"] and entry["query"] == "case"
    assert entry["counts"] == {"cults": 20, "printables": 20}
    shown = run_cli(
        tmp_path,
        "models",
        "searches",
        "show",
        str(first["search_id"]),
        "--json",
        env_extra=env,
    )
    assert shown.returncode == 0, shown.stderr
    doc = json.loads(shown.stdout)
    for key in (
        "results",
        "errors",
        "warnings",
        "timings",
        "query",
        "sites",
        "limit",
        "search_id",
    ):
        assert doc[key] == first[key], key
    for ref in ("latest", "1"):
        assert (
            json.loads(
                run_cli(
                    tmp_path, "models", "searches", "show", ref, "--json", env_extra=env
                ).stdout
            )["search_id"]
            == first["search_id"]
        )
    text = run_cli(tmp_path, "models", "searches", "show", "latest", env_extra=env)
    assert (
        text.returncode == 0
        and "printables (20)" in text.stdout
        and "Printables model 5" in text.stdout
    )
    missing = run_cli(tmp_path, "models", "searches", "show", "nope", env_extra=env)
    assert missing.returncode == 2 and "no saved search 'nope'" in missing.stderr


def test_no_save_leaves_the_library_untouched_and_a_failing_site_does_not_hide_the_rest(
    tmp_path: Path,
) -> None:
    """--no-save writes no database; a site that errors is listed under errors[] while the others return."""
    db = tmp_path / "xdg-data" / "3d-cli" / "library" / "library.db"
    with BulkServer(total=30) as server:
        server.fail_sites.add("printables")
        doc = _search_json(tmp_path, server, "--no-save", "--site", "printables,cults")
    assert doc["search_id"] is None and not db.exists()
    assert sorted(doc["results"]) == ["cults"]
    assert [e["site"] for e in doc["errors"]] == ["printables"]


def test_the_terminal_view_shows_cad_size_files_description_and_obeys_columns(
    tmp_path: Path,
) -> None:
    """The text output lists title, author, counts, licence, CAD yes/no, file summary and a description line;
    --columns trims it to what the user asked for."""
    with BulkServer(total=30) as server:
        env = _env(server, tmp_path)
        default = run_cli(
            tmp_path,
            "models",
            "search",
            "case",
            "--site",
            "printables",
            "--limit",
            "5",
            env_extra=env,
        )
        trimmed = run_cli(
            tmp_path,
            "models",
            "search",
            "case",
            "--site",
            "printables",
            "--limit",
            "5",
            "--columns",
            "cad,url",
            "--no-save",
            env_extra=env,
        )
    assert default.returncode == 0, default.stderr
    out = default.stdout
    assert re.search(r"^printables \(5\)  \d+\.\ds$", out, re.M)
    assert (
        "  5. Printables model 5\n     by Maker  likes 5  downloads 50  CC-BY  free\n"
        in out
    )
    assert (
        "CAD: yes (scad, step)  size ?  files 3 (1 scad, 1 step, 1 stl, 1.3 MB)  [img]"
        in out
    )
    assert (
        "     Summary of printables model 5" in out
        and "https://www.printables.com/model/5-model-5" in out
    )
    assert (
        "CAD: no  size ?  files 1 (1 stl, 150 kB)" not in out
        and "files 1 (1 stl, 150 KB)" in out
    )
    assert re.search(r"^saved: search [0-9a-f-]{36}", out, re.M)
    assert (
        "  5. Printables model 5\n     CAD: yes (scad, step)\n     https://www.printables.com/model/5-model-5\n"
        in trimmed.stdout
    )
    assert "by Maker" not in trimmed.stdout and "saved:" not in trimmed.stdout


def test_images_flag_falls_back_to_text_and_draws_when_a_protocol_is_forced(
    tmp_path: Path,
) -> None:
    """--images without a terminal prints a note on stderr and the normal text; forcing kitty draws the picture
    escape sequence before each result."""
    with BulkServer(total=10) as server:
        env = _env(server, tmp_path)
        plain = run_cli(
            tmp_path,
            "models",
            "search",
            "case",
            "--site",
            "thingiverse",
            "--limit",
            "3",
            "--images",
            "--no-save",
            env_extra=env,
        )
        kitty = run_cli(
            tmp_path,
            "models",
            "search",
            "case",
            "--site",
            "thingiverse",
            "--limit",
            "3",
            "--images=kitty",
            "--no-save",
            env_extra=env,
        )
        bad = run_cli(
            tmp_path,
            "models",
            "search",
            "case",
            "--images=carrier-pigeon",
            env_extra=env,
        )
    assert plain.returncode == 0 and "\x1b_G" not in plain.stdout
    assert (
        "no terminal graphics protocol detected" in plain.stderr
        and "Thingiverse model 1" in plain.stdout
    )
    assert kitty.returncode == 0, kitty.stderr
    assert kitty.stdout.count("\x1b_Ga=T,f=100,c=20") == 3 and kitty.stderr == ""
    assert (
        bad.returncode == 2 and "carrier-pigeon" in bad.stderr and "kitty" in bad.stderr
    )


def test_deep_search_measures_meshes_and_the_repeat_run_makes_no_download(
    tmp_path: Path,
) -> None:
    """--deep downloads the smallest mesh of each result and measures it; the same search again answers from the
    library cache without touching the file servers."""
    with BulkServer(total=30) as server:
        first = _search_json(
            tmp_path, server, "--site", "printables", "--limit", "3", "--deep"
        )
        downloads = server.mesh_downloads
        second = _search_json(
            tmp_path, server, "--site", "printables", "--limit", "3", "--deep"
        )
        assert server.mesh_downloads == downloads == 3
    card = first["results"]["printables"][0]
    assert card["size_mm"] == [10.0, 20.0, 30.0]
    deep = card["analytics"]["deep"]
    assert (
        deep["watertight"] is True
        and deep["manifold"] is True
        and deep["volume_mm3"] == 6000.0
    )
    assert (
        deep["triangles"] == 12 and deep["body_count"] == 1 and deep["cached"] is False
    )
    assert second["results"]["printables"][0]["analytics"]["deep"]["cached"] is True


def test_for_vitamin_searches_for_reference_models_and_keeps_only_suitable_ones(
    tmp_path: Path,
) -> None:
    """A user looks for a PCB model for a vitamin part: the query gets the preset keyword, the small free STEP/mesh
    results are measured and kept, the rest is dropped."""
    with BulkServer(total=40) as server:
        doc = _search_json(
            tmp_path,
            server,
            "--site",
            "printables,cults",
            "--limit",
            "10",
            "--for-vitamin",
            "pcb",
        )
        bad = run_cli(
            tmp_path,
            "models",
            "search",
            "x",
            "--for-vitamin",
            "gearbox",
            env_extra=_env(server, tmp_path),
        )
    assert doc["effective_query"] == "case pcb" and doc["vitamin"] == "pcb"
    cards = doc["results"]["printables"]
    assert cards and all(
        c["reference_suitable"] and c["analytics"]["reference"]["dims_known"]
        for c in cards
    )
    assert all(not c["paid"] for c in doc["results"]["cults"])
    assert bad.returncode == 2 and "gearbox" in bad.stderr and "connector" in bad.stderr


def test_a_reference_download_is_recorded_with_role_licence_and_measurements(
    tmp_path: Path,
) -> None:
    """Downloading a model as a reference part writes its source (URL, licence, role, files with sha256) to the
    library and reports the measured size; `library export` shows the row."""
    db = tmp_path / "library.db"
    with BulkServer(total=10) as server:
        result = run_cli(
            tmp_path,
            "models",
            "download",
            PRINTABLES_1,
            "--dir",
            str(tmp_path / "ref"),
            "--role",
            "reference",
            "--vitamin",
            "pcb",
            "--db",
            str(db),
            "--json",
            env_extra=_env(server, tmp_path),
        )
    assert result.returncode == 0, result.stderr
    doc = json.loads(result.stdout)
    assert (
        doc["source"]["role"] == "reference"
        and doc["source"]["license"] == "CC-BY"
        and doc["warnings"] == []
    )
    assert doc["analytics"]["files"][0]["mesh"]["bbox_mm"] == [10.0, 20.0, 30.0]
    assert (tmp_path / "ref" / "lid_1.stl").is_file() and doc["files"][0][
        "name"
    ] == "lid_1.stl"
    exported = json.loads(
        run_cli(tmp_path, "library", "export", "--db", str(db)).stdout
    )
    [row] = exported["tables"]["model_source"]
    assert (
        row["site"],
        row["model_id"],
        row["role"],
        row["vitamin_kind"],
        row["license"],
    ) == ("printables", "1", "reference", "pcb", "CC-BY")
    assert (
        row["url"] == "https://www.printables.com/model/1-model-1"
        and row["files"][0]["name"] == "lid_1.stl"
    )


def test_download_can_pick_files_and_refuses_a_vitamin_without_a_role(
    tmp_path: Path,
) -> None:
    """--file downloads only the named files; --vitamin without a reference role is a usage error."""
    with BulkServer(total=10) as server:
        env = _env(server, tmp_path)
        picked = run_cli(
            tmp_path,
            "models",
            "download",
            "https://www.printables.com/model/2-model-2",
            "--dir",
            str(tmp_path / "two"),
            "--file",
            "top_2.stl",
            "--no-save",
            "--json",
            env_extra=env,
        )
        wrong = run_cli(
            tmp_path,
            "models",
            "download",
            PRINTABLES_1,
            "--vitamin",
            "pcb",
            env_extra=env,
        )
    assert picked.returncode == 0, picked.stderr
    assert [f["name"] for f in json.loads(picked.stdout)["files"]] == ["top_2.stl"]
    assert wrong.returncode == 2 and "--role" in wrong.stderr


def test_analyze_measures_local_files_offline(tmp_path: Path) -> None:
    """With no network at all, `analyze` reports bbox, volume and manifold flags of a closed and an open box and
    lists a zip's members."""
    closed, opened, pack = (
        tmp_path / "box.stl",
        tmp_path / "open.stl",
        tmp_path / "pack.zip",
    )
    closed.write_bytes(cube_stl(10, 20, 30))
    opened.write_bytes(cube_stl(10, 20, 30, open_top=True))
    with zipfile.ZipFile(pack, "w") as zf:
        zf.writestr("case.step", b"x" * 10)
    result = run_cli(
        tmp_path,
        "models",
        "analyze",
        str(closed),
        str(opened),
        str(pack),
        "--json",
        "--no-save",
    )
    assert result.returncode == 0, result.stderr
    doc = json.loads(result.stdout)
    assert (
        doc["schema"] == "3d.models.analyze/1" and doc["facts"]["cad"] is True
    )  # the zip's STEP member counts
    box, hole, archive = doc["files"]
    assert (
        box["mesh"]["bbox_mm"] == [10.0, 20.0, 30.0] and box["mesh"]["manifold"] is True
    )
    assert box["mesh"]["volume_mm3"] == 6000.0
    assert hole["mesh"]["watertight"] is False and hole["mesh"]["volume_mm3"] is None
    assert archive["members"] == 1
    text = run_cli(tmp_path, "models", "analyze", str(closed), "--no-save")
    assert (
        "bbox 10x20x30 mm  volume 6.00 cm3" in text.stdout
        and "watertight yes  manifold yes" in text.stdout
    )
    missing = run_cli(tmp_path, "models", "analyze", str(tmp_path / "nope.stl"))
    assert missing.returncode == 2 and "file not found" in missing.stderr


NEW_FLAGS = ("--images", "--columns", "--deep", "--deep-max-mb", "--deep-top", "--cad", "--for-vitamin",
             "--no-details", "--no-save", "--db", "--idempotency-key", "--file", "--role", "--vitamin")


def test_help_and_the_command_doc_show_every_new_flag_with_a_concrete_example(tmp_path: Path) -> None:
    """`3d models --help` and docs/commands/models.md name each new subcommand and flag, and each flag appears in
    at least one complete `3d models ...` example line (help-docs sync rule)."""
    out = run_cli(tmp_path, "models", "--help").stdout
    doc = (Path(__file__).resolve().parents[2] / "docs" / "commands" / "models.md").read_text(encoding="utf-8")
    for label, text in (("help", out), ("docs", doc)):
        examples = [line for line in text.splitlines() if re.search(r"\b3d models \w", line)]
        for flag in NEW_FLAGS:
            assert any(re.search(rf"{re.escape(flag)}(?![\w-])", line) for line in examples), (label, flag)
        for sub in ("searches list", "searches show", "analyze"):
            assert sub in text, (label, sub)
