"""Tests for lib/library/search.py and sources.py (#576): the search core end to end against the loopback
marketplaces — records, filters, persistence and the round trip, idempotency keys, deep analytics with the
library cache, vitamin presets, and downloads recorded as `model_source` with a role and licence."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from marketplace_fixtures import write_fake_curl
from search_fixtures import BulkServer

from errors import InvalidArgument, UsageError
from library import actions, analytics, search, sources
from library import db as librarydb
from marketplaces import thingiverse

LEGACY_KEYS = [
    "site",
    "id",
    "title",
    "url",
    "author",
    "author_url",
    "likes",
    "downloads",
    "license",
    "price",
    "paid",
    "summary",
    "tags",
    "image",
    "files",
]
ALL = ("printables", "makerworld", "thingiverse", "cults")


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("THREED_KEYCHAIN", "off")
    monkeypatch.delenv("AXE_CURL_IMPERSONATE_DIR", raising=False)
    binary, _ = write_fake_curl(tmp_path / "curl-bin")
    monkeypatch.setenv("THREED_CURL_IMPERSONATE", str(binary))
    thingiverse.clear_cache()


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> Any:
    with BulkServer(total=120) as srv:
        for key, value in srv.env().items():
            monkeypatch.setenv(key, value)
        yield srv


@pytest.fixture
def dbpath(tmp_path: Path) -> str:
    return str(tmp_path / "library.db")


def _run(query: str, db: str | None, **kw: Any) -> dict[str, Any]:
    options = search.SearchOptions(
        sites=kw.pop("sites", ALL), db_path=db, save=db is not None, **kw
    )
    return search.run_search(query, options).doc


# -- records ---------------------------------------------------------------------------------------------
def test_fifty_results_per_site_with_every_new_field(
    server: BulkServer, dbpath: str
) -> None:
    doc = _run("case", dbpath, limit=50)
    assert (
        doc["schema"] == "3d.models.search/2"
        and doc["query"] == "case"
        and doc["errors"] == []
    )
    assert {site: len(recs) for site, recs in doc["results"].items()} == dict.fromkeys(
        ALL, 50
    )
    assert set(doc["timings"]) == set(ALL) and doc["elapsed_s"] >= 0
    for recs in doc["results"].values():
        for rec in recs:
            assert set(LEGACY_KEYS) <= set(rec)  # old --json consumers keep every key
            assert isinstance(rec["cad"], bool) and isinstance(rec["cad_formats"], list)
            assert (
                isinstance(rec["analytics"], dict)
                and rec["rank"] >= 1
                and "size_mm" not in rec
            )
            assert rec["image"] and rec["summary"] or rec["site"] == "makerworld"
            assert all(
                {"name", "size", "ref", "kind", "ext", "class"} <= set(f)
                for f in rec["files"]
            )


def test_cad_is_true_exactly_when_a_cad_format_file_is_listed(
    server: BulkServer, dbpath: str
) -> None:
    doc = _run("case", dbpath, limit=50)
    for recs in doc["results"].values():
        for rec in recs:
            expected = sorted(
                {
                    f["ext"]
                    for f in rec["files"]
                    if analytics.file_class(f["name"]) == "cad"
                }
            )
            assert rec["cad"] is bool(expected) and rec["cad_formats"] == expected
    printables = {r["id"]: r for r in doc["results"]["printables"]}
    assert (
        printables["5"]["cad_formats"] == ["scad", "step"]
        and printables["1"]["cad"] is False
    )
    assert printables["4"]["analytics"]["archives_unlisted"] == 1  # a zip nobody opened
    assert (
        printables["2"]["analytics"]["is_assembly"]
        and printables["2"]["analytics"]["is_multi_part"]
    )


def test_an_unknown_file_list_is_not_reported_as_no_cad(
    server: BulkServer, dbpath: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("THREED_THINGIVERSE_BUDGET_S", "0")
    doc = _run("case", dbpath, sites=("thingiverse",), limit=5)
    rec = doc["results"]["thingiverse"][0]
    assert rec["cad"] is False and rec["analytics"] == {
        "schema": analytics.SCHEMA,
        "files_known": False,
        "reference": rec["analytics"]["reference"],
    }
    assert rec["analytics"]["reference"]["reasons"] == ["file list unknown"]
    assert doc["warnings"][0]["site"] == "thingiverse"


def test_cad_filter_keeps_only_results_with_cad(
    server: BulkServer, dbpath: str
) -> None:
    doc = _run("case", dbpath, sites=("printables", "cults"), limit=50, only_cad=True)
    for recs in doc["results"].values():
        assert recs and all(r["cad"] for r in recs)
        assert [r["rank"] for r in recs] == sorted(
            r["rank"] for r in recs
        )  # original relevance order kept
    assert (
        len(doc["results"]["printables"]) == 20
    )  # ids i with i % 5 in (0, 3) among 1..50


def test_a_failing_site_is_reported_and_the_others_still_return(
    server: BulkServer, dbpath: str
) -> None:
    server.fail_sites.add("printables")
    run = search.run_search(
        "case", search.SearchOptions(sites=ALL, limit=20, db_path=dbpath)
    )
    assert sorted(run.doc["results"]) == ["cults", "makerworld", "thingiverse"]
    assert [e["site"] for e in run.doc["errors"]] == ["printables"] and list(
        run.failures
    ) == ["printables"]
    assert run.doc["errors"][0]["kind"] == "http"


# -- persistence -----------------------------------------------------------------------------------------
def test_a_search_is_saved_with_all_results_and_round_trips(
    server: BulkServer, dbpath: str
) -> None:
    doc = _run("case", dbpath, limit=50)
    assert doc["search_id"]
    conn = librarydb.open_library(dbpath)
    try:
        assert conn.execute("SELECT COUNT(*) FROM search_session").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM search_result").fetchone()[0] == 200
        row = conn.execute(
            "SELECT * FROM search_result WHERE site = 'printables' AND model_id = '5'"
        ).fetchone()
        assert (
            row["title"] == "Printables model 5"
            and row["rank"] == 5
            and row["image_url"]
        )
        assert json.loads(row["analytics"])["cad"] is True
        stored = search.session_doc(conn, doc["search_id"])
    finally:
        conn.close()
    for key in (
        "schema",
        "query",
        "effective_query",
        "search_id",
        "limit",
        "sites",
        "deep",
        "vitamin",
        "timings",
        "results",
        "errors",
        "warnings",
        "elapsed_s",
    ):
        assert stored[key] == doc[key], key
    assert (
        stored["kind"] == "user"
        and stored["parent"] is None
        and stored["created_at"].endswith("Z")
    )


def test_failed_sites_round_trip_as_errors_not_as_empty_results(
    server: BulkServer, dbpath: str
) -> None:
    server.fail_sites.add("cults")
    doc = _run("case", dbpath, sites=("printables", "cults"), limit=5)
    conn = librarydb.open_library(dbpath)
    try:
        stored = search.session_doc(conn, "latest")
    finally:
        conn.close()
    assert (
        list(stored["results"]) == ["printables"] and stored["errors"] == doc["errors"]
    )
    assert stored["errors"][0]["site"] == "cults"


def test_idempotency_key_returns_the_first_session_and_never_duplicates_rows(
    server: BulkServer, dbpath: str
) -> None:
    first = _run(
        "case", dbpath, sites=("printables",), limit=10, idempotency_key="run-1"
    )
    again = _run(
        "case", dbpath, sites=("printables",), limit=10, idempotency_key="run-1"
    )
    other = _run(
        "case", dbpath, sites=("printables",), limit=10, idempotency_key="run-2"
    )
    plain_a = _run("case", dbpath, sites=("printables",), limit=10)
    plain_b = _run("case", dbpath, sites=("printables",), limit=10)
    assert first["search_id"] == again["search_id"] != other["search_id"]
    assert (
        plain_a["search_id"] != plain_b["search_id"]
    )  # no key: every search is its own session
    conn = librarydb.open_library(dbpath)
    try:
        assert conn.execute("SELECT COUNT(*) FROM search_session").fetchone()[0] == 4
        assert conn.execute("SELECT COUNT(*) FROM search_result").fetchone()[0] == 40
    finally:
        conn.close()


def test_no_save_writes_no_database(server: BulkServer, tmp_path: Path) -> None:
    doc = _run("case", None, sites=("printables",), limit=3)
    assert doc["search_id"] is None and len(doc["results"]["printables"]) == 3
    assert not (tmp_path / "library.db").exists()


def test_an_unwritable_library_adds_a_warning_and_keeps_the_results(
    server: BulkServer, tmp_path: Path
) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("a file, not a directory", encoding="utf-8")
    doc = _run("case", str(blocker / "library.db"), sites=("printables",), limit=3)
    assert doc["search_id"] is None and len(doc["results"]["printables"]) == 3
    assert (
        doc["warnings"][-1]["site"] == "library"
        and "not saved" in doc["warnings"][-1]["message"]
    )


def test_sessions_resolve_by_number_uid_and_latest_and_list_newest_first(
    server: BulkServer, dbpath: str
) -> None:
    a = _run("alpha", dbpath, sites=("printables",), limit=2)
    b = _run(
        "beta",
        dbpath,
        sites=("cults",),
        limit=3,
        kind="narrow",
        parent_session=a["search_id"],
    )
    conn = librarydb.open_library(dbpath)
    try:
        assert search.session_doc(conn, "latest")["search_id"] == b["search_id"]
        assert search.session_doc(conn, "1")["search_id"] == a["search_id"]
        stored = search.session_doc(conn, a["search_id"])
        assert stored["query"] == "alpha"
        assert search.session_doc(conn, b["search_id"])["parent"] == a["search_id"]
        listed = search.list_sessions(conn)
        assert [s["query"] for s in listed] == ["beta", "alpha"]
        assert listed[0]["counts"] == {"cults": 3} and listed[1]["sites"] == [
            "printables"
        ]
        with pytest.raises(UsageError, match="no saved search 'nope'"):
            search.session_doc(conn, "nope")
    finally:
        conn.close()


def test_search_kind_is_validated() -> None:
    with pytest.raises(InvalidArgument, match="kind"):
        search.run_search("x", search.SearchOptions(kind="bogus", save=False))


# -- Thingiverse follow-ups persist across processes -------------------------------------------------------
def test_thingiverse_follow_ups_are_cached_in_the_library_between_runs(server: BulkServer, dbpath: str) -> None:
    first = _run("case", dbpath, sites=("thingiverse",), limit=20)
    assert server.calls["thingiverse:detail"] == server.calls["thingiverse:files"] == 20
    thingiverse.clear_cache()  # a new process remembers nothing in memory
    again = _run("case", dbpath, sites=("thingiverse",), limit=20)
    assert server.calls["thingiverse:detail"] == server.calls["thingiverse:files"] == 20  # no new follow-up request
    assert again["results"] == first["results"] | {"thingiverse": again["results"]["thingiverse"]}
    assert all(r["analytics"]["files_known"] and r["license"] for r in again["results"]["thingiverse"])
    assert [f["name"] for f in again["results"]["thingiverse"][1]["files"]] == ["base_2.stl", "top_2.stl",
                                                                                 "assembled_2.3mf"]
    thingiverse.clear_cache()
    _run("case", None, sites=("thingiverse",), limit=20)  # --no-save: the stored answers are not used
    assert server.calls["thingiverse:detail"] == 40


def test_a_stored_follow_up_answer_expires(server: BulkServer, dbpath: str, monkeypatch: pytest.MonkeyPatch) -> None:
    _run("case", dbpath, sites=("thingiverse",), limit=5)
    thingiverse.clear_cache()
    monkeypatch.setattr(search, "DETAIL_MAX_AGE_S", -1.0)
    _run("case", dbpath, sites=("thingiverse",), limit=5)
    assert server.calls["thingiverse:detail"] == 10


def test_the_detail_store_reads_garbage_and_a_dead_database_as_a_miss(tmp_path: Path) -> None:
    store = search.LibraryDetailStore(str(tmp_path / "blocker" / "library.db"))
    (tmp_path / "blocker").write_text("a file", encoding="utf-8")
    assert store.get("thingiverse:1") is None
    store.put("thingiverse:1", {"thing": {}, "files": []})  # no error either
    store.close()
    ok = search.LibraryDetailStore(str(tmp_path / "library.db"))
    ok.put("thingiverse:9", {"thing": "not an object"})
    assert ok.get("thingiverse:9") == {"thing": "not an object"}
    assert thingiverse._stored(ok, "9") is None  # a malformed entry is ignored, not trusted
    ok.close()


# -- deep analytics --------------------------------------------------------------------------------------
def test_deep_measures_the_smallest_mesh_and_the_second_run_uses_the_cache(
    server: BulkServer, dbpath: str
) -> None:
    first = _run(
        "case", dbpath, sites=("printables", "thingiverse"), limit=5, deep=True
    )
    rec = first["results"]["printables"][
        0
    ]  # id 1: lid_1.stl (150 kB) -> the generated 10x20x30 cube
    assert rec["size_mm"] == [10.0, 20.0, 30.0]
    deep = rec["analytics"]["deep"]
    assert (
        deep["status"] == "ok"
        and deep["watertight"]
        and deep["manifold"]
        and deep["cached"] is False
    )
    assert (
        deep["volume_mm3"] == pytest.approx(6000.0)
        and deep["file"] == "lid_1.stl"
        and len(deep["sha256"]) == 64
    )
    assert rec["analytics"]["reference"]["dims_known"] is True
    assert first["results"]["thingiverse"][0]["size_mm"] == [10.0, 20.0, 30.0]
    zip_only = next(
        r for r in first["results"]["printables"] if r["id"] == "4"
    )  # only a zip is listed
    assert zip_only["analytics"]["deep"] == {
        "status": "skipped",
        "reason": "no mesh file of known size within the cap",
    }
    assert "size_mm" not in zip_only
    downloads = (server.mesh_downloads, server.calls["printables:download-link"])
    assert downloads[0] > 0

    second = _run(
        "case", dbpath, sites=("printables", "thingiverse"), limit=5, deep=True
    )
    assert (
        server.mesh_downloads,
        server.calls["printables:download-link"],
    ) == downloads  # no network at all
    again = second["results"]["printables"][0]
    assert (
        again["size_mm"] == [10.0, 20.0, 30.0]
        and again["analytics"]["deep"]["cached"] is True
    )


def test_deep_skips_paid_listings_and_login_gated_sites_with_a_reason(
    server: BulkServer, dbpath: str
) -> None:
    doc = _run("case", dbpath, sites=("makerworld", "cults"), limit=5, deep=True)
    assert doc["results"]["makerworld"][0]["analytics"]["deep"]["reason"].startswith(
        "makerworld serves"
    )
    cults = {r["id"]: r for r in doc["results"]["cults"]}
    assert cults["creation-4"]["analytics"]["deep"] == {
        "status": "skipped",
        "reason": "paid listing",
    }
    assert cults["creation-1"]["analytics"][
        "deep"
    ] == {  # Cults lists no file sizes: nothing to pick
        "status": "skipped",
        "reason": "no mesh file of known size within the cap",
    }
    assert server.mesh_downloads == 0


def test_deep_never_downloads_above_the_size_cap(
    server: BulkServer, dbpath: str
) -> None:
    doc = _run(
        "case", dbpath, sites=("printables",), limit=5, deep=True, deep_max_bytes=1000
    )
    assert server.mesh_downloads == 0
    assert {r["analytics"]["deep"]["status"] for r in doc["results"]["printables"]} == {
        "skipped"
    }


def test_deep_top_limits_how_many_results_are_measured(
    server: BulkServer, dbpath: str
) -> None:
    doc = _run("case", dbpath, sites=("printables",), limit=10, deep=True, deep_top=2)
    measured = [
        r["id"] for r in doc["results"]["printables"] if "deep" in r["analytics"]
    ]
    assert measured == ["1", "2"]


# -- vitamin presets -------------------------------------------------------------------------------------
def test_vitamin_preset_adds_its_keyword_unless_the_query_has_one() -> None:
    assert search.effective_query("esp32 devkit", "pcb") == "esp32 devkit pcb"
    assert search.effective_query("esp32 board", "pcb") == "esp32 board"
    assert search.effective_query("m3 screw", "screw") == "m3 screw"
    assert search.effective_query("anything", None) == "anything"
    with pytest.raises(InvalidArgument, match="--for-vitamin"):
        search.check_vitamin("gearbox")


def test_for_vitamin_keeps_only_reference_suitable_results_and_measures_them(
    server: BulkServer, dbpath: str
) -> None:
    doc = _run(
        "esp32 devkit", dbpath, sites=("printables", "cults"), limit=10, vitamin="pcb"
    )
    assert (
        doc["effective_query"] == "esp32 devkit pcb"
        and doc["vitamin"] == "pcb"
        and doc["deep"] is True
    )
    printables = doc["results"]["printables"]
    assert printables and all(r["reference_suitable"] for r in printables)
    assert all(
        r["analytics"]["reference"]["dims_known"] for r in printables
    )  # measured: small STEP/mesh, known dims
    assert all(r["size_mm"] == [10.0, 20.0, 30.0] for r in printables)
    assert {r["id"] for r in printables}.isdisjoint(
        {"4", "9"}
    )  # zip-only listings never qualify
    cults = doc["results"]["cults"]
    assert all(
        not r["paid"] for r in cults
    )  # a priced creation is never a reference model


# -- downloads recorded as model_source ------------------------------------------------------------------
PRINTABLES_1 = "https://www.printables.com/model/1-model-1"


def test_reference_download_writes_model_source_with_role_and_licence(
    server: BulkServer, dbpath: str, tmp_path: Path
) -> None:
    result = sources.download_model(
        PRINTABLES_1,
        tmp_path / "ref",
        role="reference",
        vitamin_kind="pcb",
        db_path=dbpath,
    )
    assert (
        result["source"]["role"] == "reference"
        and result["source"]["license"] == "CC-BY"
    )
    assert result["warnings"] == []
    mesh = result["analytics"]["files"][0]["mesh"]  # reference parts are measured
    assert mesh["bbox_mm"] == [10.0, 20.0, 30.0] and mesh["manifold"]
    conn = librarydb.open_library(dbpath)
    try:
        row = conn.execute("SELECT * FROM model_source").fetchone()
        assert (
            row["site"],
            row["model_id"],
            row["role"],
            row["vitamin_kind"],
            row["license"],
        ) == ("printables", "1", "reference", "pcb", "CC-BY")
        assert (
            row["author"] == "Maker"
            and row["url"] == "https://www.printables.com/model/1-model-1"
        )
        assert (
            Path(row["dir"]) == (tmp_path / "ref").resolve()
            and len(row["sha256"]) == 64
        )
        files = json.loads(row["files"])
        assert [f["name"] for f in files] == ["lid_1.stl"] and files[0][
            "sha256"
        ] == row["sha256"]
        assert json.loads(row["analytics"])["facts"]["has_stl"] is True
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM events WHERE kind = 'library.model_source.created'"
            ).fetchone()[0]
            == 1
        )
    finally:
        conn.close()


def test_downloading_the_same_model_again_updates_its_single_source_row(
    server: BulkServer, dbpath: str, tmp_path: Path
) -> None:
    sources.download_model(PRINTABLES_1, tmp_path / "a", db_path=dbpath)
    again = sources.download_model(
        PRINTABLES_1,
        tmp_path / "b",
        role="vitamin",
        vitamin_kind="screw",
        db_path=dbpath,
    )
    assert again["source"]["created"] is False
    conn = librarydb.open_library(dbpath)
    try:
        rows = conn.execute(
            "SELECT role, vitamin_kind, dir FROM model_source"
        ).fetchall()
    finally:
        conn.close()
    assert [(r["role"], r["vitamin_kind"]) for r in rows] == [("vitamin", "screw")]
    assert Path(rows[0]["dir"]) == (tmp_path / "b").resolve()


def test_download_without_a_role_records_the_source_without_one_and_no_save_records_nothing(
    server: BulkServer, dbpath: str, tmp_path: Path
) -> None:
    plain = sources.download_model(PRINTABLES_1, tmp_path / "a", db_path=dbpath)
    assert plain["source"]["role"] is None
    unsaved = sources.download_model(
        "https://www.printables.com/model/2-model-2",
        tmp_path / "b",
        save=False,
        db_path=dbpath,
    )
    assert unsaved["source"] is None
    conn = librarydb.open_library(dbpath)
    try:
        assert [
            r["model_id"] for r in conn.execute("SELECT model_id FROM model_source")
        ] == ["1"]
    finally:
        conn.close()


def test_role_and_vitamin_kind_are_validated_before_any_download(
    tmp_path: Path,
) -> None:
    with pytest.raises(InvalidArgument, match="--role"):
        sources.download_model(PRINTABLES_1, tmp_path / "x", role="gadget")
    with pytest.raises(InvalidArgument, match="--vitamin"):
        sources.download_model(
            PRINTABLES_1, tmp_path / "x", role="reference", vitamin_kind="gearbox"
        )
    with pytest.raises(InvalidArgument, match="only with --role"):
        sources.download_model(
            PRINTABLES_1, tmp_path / "x", role="printed", vitamin_kind="pcb"
        )
    assert not (tmp_path / "x").exists()


def test_a_reference_download_without_a_licence_warns_instead_of_inventing_one(
    server: BulkServer, dbpath: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = sources.download_model(
        "https://www.thingiverse.com/thing:1",
        tmp_path / "t",
        role="reference",
        db_path=dbpath,
    )
    assert (
        result["model"]["license"] == "Creative Commons - Attribution"
    )  # Thingiverse detail carries it
    assert result["warnings"] == []
    # A listing that names no licence: blank it between the site's answer and the recording.
    from marketplaces import catalog

    real_download = catalog.download

    def blank_licence(url: str, dest: Path | None, **kw: Any) -> Any:
        from dataclasses import replace

        model, target, written = real_download(url, dest, **kw)
        return replace(model, license=""), target, written

    monkeypatch.setattr(catalog, "download", blank_licence)
    blank = sources.download_model(
        PRINTABLES_1, tmp_path / "blank", role="reference", db_path=dbpath
    )
    assert [w["code"] for w in blank["warnings"]] == ["license_missing"]
    conn = librarydb.open_library(dbpath)
    try:
        row = conn.execute(
            "SELECT license FROM model_source WHERE site = 'printables' AND model_id = '1'"
        ).fetchone()
    finally:
        conn.close()
    assert row["license"] is None


def test_a_paid_model_is_refused_and_recorded_nowhere(
    server: BulkServer, dbpath: str, tmp_path: Path
) -> None:
    from marketplaces.base import MarketplaceError

    with pytest.raises(MarketplaceError, match="paid"):
        sources.download_model(
            "https://cults3d.com/en/3d-model/home/creation-4",
            tmp_path / "p",
            db_path=dbpath,
        )
    conn = librarydb.open_library(dbpath)
    try:
        assert conn.execute("SELECT COUNT(*) FROM model_source").fetchone()[0] == 0
    finally:
        conn.close()


def test_analyze_paths_measures_meshes_and_caches_by_content(
    server: BulkServer, dbpath: str, tmp_path: Path
) -> None:
    from search_fixtures import cube_stl

    a, b = tmp_path / "a.stl", tmp_path / "copy.stl"
    a.write_bytes(cube_stl(4, 5, 6))
    b.write_bytes(cube_stl(4, 5, 6))
    conn = librarydb.open_library(dbpath)
    try:
        report = sources.analyze_paths([a], conn, deep=True)
        assert report["files"][0]["mesh"]["cached"] is False and report["files"][0][
            "mesh"
        ]["bbox_mm"] == [4.0, 5.0, 6.0]
        same = sources.analyze_paths(
            [b], conn, deep=True
        )  # a different name, the same bytes
        assert same["files"][0]["mesh"]["cached"] is True
        names_only = sources.analyze_paths([a], None, deep=False)
        assert "mesh" not in names_only["files"][0] and names_only["facts"]["has_stl"]
    finally:
        conn.close()


def test_analyze_paths_opens_archives(tmp_path: Path) -> None:
    import zipfile

    pack = tmp_path / "pack.zip"
    with zipfile.ZipFile(pack, "w") as zf:
        zf.writestr("model.step", b"x" * 20)
    report = sources.analyze_paths([pack], None, deep=True)
    assert report["facts"]["cad"] is True and report["files"][0]["members"] == 1


# -- actions ---------------------------------------------------------------------------------------------
def test_search_action_returns_a_flat_result_list_and_the_session(
    server: BulkServer, dbpath: str
) -> None:
    res = actions.invoke(
        "search", query="case", sites=["printables", "cults"], limit=5, db_path=dbpath
    )
    assert res["ok"] and res["errors"] == [] and res["data"]["session"]
    assert len(res["data"]["results"]) == 10
    first = res["data"]["results"][0]
    assert {
        "site",
        "model_id",
        "url",
        "title",
        "rank",
        "image_url",
        "cad",
        "reference_suitable",
    } <= set(first)


def test_search_action_reports_failed_sites_as_warnings_and_total_failure_as_not_ok(
    server: BulkServer, dbpath: str
) -> None:
    server.fail_sites.update({"printables"})
    partial = actions.invoke(
        "search", query="case", sites=["printables", "cults"], limit=3, db_path=dbpath
    )
    assert partial["ok"] and [w["code"] for w in partial["warnings"]] == ["site_failed"]
    total = actions.invoke(
        "search", query="case", sites=["printables"], limit=3, db_path=dbpath
    )
    assert total["ok"] is False and total["errors"][0]["code"] == "site_failed"
    bad_kind = actions.invoke("search", query="case", kind="bogus", db_path=dbpath)
    assert (
        bad_kind["ok"] is False and bad_kind["errors"][0]["code"] == "invalid_argument"
    )


def test_download_action_records_a_model_source(
    server: BulkServer, dbpath: str, tmp_path: Path
) -> None:
    res = actions.invoke(
        "download",
        url=PRINTABLES_1,
        dest_dir=str(tmp_path / "act"),
        role="reference",
        vitamin_kind="connector",
        db_path=dbpath,
    )
    assert res["ok"], res
    data = res["data"]
    assert data["role"] == "reference" and data["license"] == "CC-BY" and data["source"]
    assert [f["name"] for f in data["files"]] == ["lid_1.stl"] and set(
        data["files"][0]
    ) == {"name", "size", "sha256"}
    missing_dir = actions.invoke(
        "download", url=PRINTABLES_1, dest_dir=str(tmp_path / "act"), db_path=dbpath
    )
    assert (
        missing_dir["ok"] is False and missing_dir["errors"][0]["code"] == "error"
    )  # non-empty directory


def test_migration_0010_adds_the_search_core_columns(dbpath: str) -> None:
    conn = librarydb.open_library(dbpath)
    try:

        def columns(table: str) -> set[str]:
            return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}

        assert {"meta"} <= columns("search_session") and {"record"} <= columns(
            "search_result"
        )
        assert {"role", "vitamin_kind", "analytics"} <= columns("model_source")
        assert {"key", "sha256", "analytics"} == columns("analytics_cache") - {
            "created_at"
        }
        status = librarydb.status(conn, dbpath)
        assert status["migrations"] == [1, 10] and status["migrations_pending"] == []
        with pytest.raises(Exception, match="CHECK"):
            conn.execute(
                "INSERT INTO model_source (uid, site, model_id, url, files, role, created_at) "
                "VALUES ('u', 's', 'm', 'x', '[]', 'gadget', 'now')"
            )
    finally:
        conn.close()
