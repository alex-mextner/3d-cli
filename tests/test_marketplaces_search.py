"""Search-core tests for lib/marketplaces (#576, #504): 50+ results per site through the real clients against
the loopback fakes in tests/search_fixtures.py (HTTP boundary only), paging, the normalised card fields, the
bounded and budgeted Thingiverse follow-up requests and the #504 transport/printables fixes."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from marketplace_fixtures import write_fake_curl
from search_fixtures import BulkServer

from errors import UsageError
from marketplaces import catalog, printables, thingiverse, transport
from marketplaces.base import MarketplaceError, Model, ModelFile, collect_pages


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("THREED_KEYCHAIN", "off")
    monkeypatch.delenv("AXE_CURL_IMPERSONATE_DIR", raising=False)
    binary, _ = write_fake_curl(tmp_path / "curl-bin")
    monkeypatch.setenv("THREED_CURL_IMPERSONATE", str(binary))
    thingiverse.clear_cache()


def _use(server: BulkServer, monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in server.env().items():
        monkeypatch.setenv(key, value)


# -- 50 results per site ---------------------------------------------------------------------------------
def test_every_site_returns_fifty_results_with_the_normalised_card(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with BulkServer(total=120) as server:
        _use(server, monkeypatch)
        results = {
            r.site: r
            for r in catalog.search(
                "case", ["printables", "makerworld", "thingiverse", "cults"], 50
            )
        }
    assert {site: len(r.models) for site, r in results.items()} == dict.fromkeys(
        results, 50
    )
    assert all(r.error is None for r in results.values())
    for site, r in results.items():
        first = r.models[0]
        assert first.site == site and first.title and first.url.startswith("http")
        assert (
            first.image and first.summary or site == "makerworld"
        )  # MakerWorld search hits carry no summary
        assert first.files_known and first.files, site
        assert (
            first.license
            and first.date
            and first.likes is not None
            and first.downloads is not None
        )
        assert len({m.id for m in r.models}) == 50


def test_printables_card_has_images_files_counts_and_category(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with BulkServer(total=10) as server:
        _use(server, monkeypatch)
        models = printables.search("case", 10)
    m = models[0]  # id 1 lists "lid_1.stl" only; id 5 mixes CAD
    assert m.images == (
        "https://media.printables.com/media/prints/1/cover.png",
        "https://media.printables.com/media/prints/1/2.png",
    )
    assert (
        m.image == m.images[0]
        and m.makes == 0
        and m.category == "Gadgets"
        and m.date.startswith("2026-01-31")
    )
    assert [f.name for f in m.files] == ["lid_1.stl"] and m.files_known
    cad = next(x for x in models if x.id == "5")
    assert sorted(f.name for f in cad.files) == [
        "case_5.scad",
        "case_5.step",
        "case_5.stl",
    ]


def test_paging_goes_through_the_sites_paging_parameters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with BulkServer(total=120) as server:
        _use(server, monkeypatch)
        hundred = printables.search("case", 100)
        assert [m.id for m in hundred] == [str(i) for i in range(1, 101)]
        assert server.page_sizes["printables:search"] == [50, 50]
        assert len(catalog.search("case", ["cults"], 100)[0].models) == 100
        assert server.page_sizes["cults:search"] == [50, 50]
        assert len(catalog.search("case", ["makerworld"], 100)[0].models) == 100
        assert server.page_sizes["makerworld:search"] == [50, 50]


def test_a_short_last_page_ends_the_search_without_another_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with BulkServer(total=70) as server:
        _use(server, monkeypatch)
        models = printables.search("case", 100)
        assert len(models) == 70 and server.page_sizes["printables:search"] == [50, 50]


def test_collect_pages_drops_repeats_and_never_loops_forever() -> None:
    def always_same(offset: int, size: int) -> list[Model]:
        del offset
        return [Model(site="x", id="1", title="t", url="u")] * size

    assert [m.id for m in collect_pages(always_same, 30, 10)] == ["1"]


# -- file lists, CAD ------------------------------------------------------------------------------------
def test_cults_lists_blueprint_names_even_without_a_download_link(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with BulkServer(total=5) as server:
        _use(server, monkeypatch)
        models = catalog.search("case", ["cults"], 5)[0].models
    assert [f.name for f in models[1].files] == [
        "base_2.stl",
        "top_2.stl",
        "assembled_2.3mf",
    ]
    assert all(f.ref == "" for f in models[1].files) and models[1].files_known
    assert (
        models[0].paid is False and models[3].paid is True
    )  # i % 4 == 0 is priced in the fake


def test_makerworld_hits_carry_their_model_files_and_pictures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with BulkServer(total=5) as server:
        _use(server, monkeypatch)
        m = catalog.search("case", ["makerworld"], 5)[0].models[
            4
        ]  # id 5: CAD + mesh listing
    assert sorted(f.name for f in m.files) == [
        "case_5.scad",
        "case_5.step",
        "case_5.stl",
    ]
    assert [f.kind for f in sorted(m.files, key=lambda f: f.name)] == [
        "scad",
        "step",
        "stl",
    ]
    assert (
        m.images == ("https://cdn.example/mw/5.jpg", "https://cdn.example/mw/5-1.jpg")
        and m.makes == 1
    )


# -- Thingiverse: bounded, budgeted follow-ups ----------------------------------------------------------
def test_thingiverse_search_alone_costs_one_request_per_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with BulkServer(total=120) as server:
        _use(server, monkeypatch)
        result = catalog.search("case", ["thingiverse"], 100, details=False)[0]
    assert len(result.models) == 100 and not any(
        m.files_known or m.license for m in result.models
    )
    assert (
        server.calls["thingiverse:search"] == 2
        and server.calls["thingiverse:detail"] == 0
    )
    assert server.calls["thingiverse:files"] == 0 and result.warnings == ()


def test_thingiverse_fifty_results_make_at_most_one_detail_and_files_call_each_with_a_parallel_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with BulkServer(total=120, delay_s=0.02) as server:
        _use(server, monkeypatch)
        result = catalog.search("case", ["thingiverse"], 50)[0]
    assert len(result.models) == 50 and all(
        m.files_known and m.license for m in result.models
    )
    assert server.calls["thingiverse:search"] == 1
    assert (
        server.calls["thingiverse:detail"] == 50
        and server.calls["thingiverse:files"] == 50
    )
    assert 1 < server.max_in_flight <= 4, server.max_in_flight
    cad = [m for m in result.models if m.id == "5"][0]
    assert (
        cad.downloads == 35
        and cad.views == 150
        and cad.summary == "Thing 5 description"
    )
    assert result.warnings == ()


def test_thingiverse_second_search_is_served_from_the_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with BulkServer(total=20) as server:
        _use(server, monkeypatch)
        catalog.search("case", ["thingiverse"], 20)
        before = (server.calls["thingiverse:detail"], server.calls["thingiverse:files"])
        again = catalog.search("case", ["thingiverse"], 20)[0]
        assert (
            (server.calls["thingiverse:detail"], server.calls["thingiverse:files"])
            == before
            == (20, 20)
        )
    assert all(m.files_known for m in again.models)


def test_thingiverse_time_budget_stops_new_follow_ups_and_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("THREED_THINGIVERSE_BUDGET_S", "0")
    with BulkServer(total=30) as server:
        _use(server, monkeypatch)
        result = catalog.search("case", ["thingiverse"], 30)[0]
    assert len(result.models) == 30 and server.calls["thingiverse:detail"] == 0
    assert not any(m.files_known for m in result.models)
    assert result.warnings and result.warnings[0].startswith(
        "30 of 30 hits have no file list"
    )


def test_thingiverse_budget_env_falls_back_on_garbage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for raw in ("", "abc", "-5", "inf", "nan"):
        monkeypatch.setenv("THREED_THINGIVERSE_BUDGET_S", raw)
        assert thingiverse.budget_seconds() == thingiverse.DEFAULT_BUDGET_S, raw
    monkeypatch.setenv("THREED_THINGIVERSE_BUDGET_S", "2.5")
    assert thingiverse.budget_seconds() == 2.5


def test_a_failing_follow_up_keeps_the_hit_and_the_rest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    models = [
        Model(site="thingiverse", id=str(i), title=f"t{i}", url="u") for i in (1, 5)
    ]
    with BulkServer(total=10) as server:
        _use(server, monkeypatch)
        bad = [Model(site="thingiverse", id="999999", title="gone", url="u"), *models]
        enriched = thingiverse.enrich(bad)
    assert not enriched[0].files_known and enriched[0].license == ""
    assert all(m.files_known and m.license for m in enriched[1:])


# -- one failing site never hides the others -------------------------------------------------------------
def test_a_failing_site_lands_in_errors_and_the_others_still_return(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with BulkServer(total=60) as server:
        server.fail_sites.add("printables")
        _use(server, monkeypatch)
        results = {
            r.site: r
            for r in catalog.search("case", ["printables", "cults", "makerworld"], 50)
        }
    assert (
        results["printables"].error is not None and results["printables"].models == ()
    )
    assert (
        "search is down"
        in catalog.error_json("printables", results["printables"].error)["message"]
    )
    assert (
        len(results["cults"].models) == 50 and len(results["makerworld"].models) == 50
    )
    assert all(r.elapsed_s >= 0 for r in results.values())


# -- #504 follow-ups -------------------------------------------------------------------------------------
def test_impersonated_fetch_refuses_a_non_http_url() -> None:
    for url in ("ftp://example.com/x", "file:///etc/passwd", "gopher://example.com"):
        with pytest.raises(MarketplaceError, match="non-HTTP URL"):
            transport.fetch_impersonated("makerworld", url)


def test_curl_impersonate_path_is_validated(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    missing = tmp_path / "nope"
    monkeypatch.setenv("THREED_CURL_IMPERSONATE", str(missing))
    with pytest.raises(UsageError, match="not an executable file") as exc:
        transport.curl_impersonate_binary()
    assert "THREED_CURL_IMPERSONATE" in exc.value.remediation[0]
    plain = tmp_path / "plain"
    plain.write_text("x", encoding="utf-8")
    monkeypatch.setenv("THREED_CURL_IMPERSONATE", str(plain))
    with pytest.raises(UsageError):
        transport.curl_impersonate_binary()
    plain.chmod(0o755)
    assert transport.curl_impersonate_binary() == str(plain)
    assert os.access(plain, os.X_OK)


def test_printables_error_messages_survive_odd_shapes() -> None:
    file = ModelFile(name="a.stl", ref="1", kind="stl")
    answer = {
        "getDownloadLink": {
            "ok": False,
            "errors": [
                {"field": "id", "messages": ["File not found", None, 7]},
                {"messages": "not a list"},
                "junk",
                {},
                {"messages": ["Second"]},
            ],
        }
    }
    with pytest.raises(
        MarketplaceError, match=r"no download link for a\.stl: File not found; Second"
    ):
        printables.parse_download_link(answer, file)
    with pytest.raises(MarketplaceError, match=r"no download link for a\.stl$"):
        printables.parse_download_link({"getDownloadLink": {"ok": False}}, file)


def test_download_returns_written_file_records(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with BulkServer(total=5) as server:
        _use(server, monkeypatch)
        model, target, written = catalog.download(
            "https://www.printables.com/model/1-model-1", tmp_path / "out"
        )
    assert model.id == "1" and [w.name for w in written] == ["lid_1.stl"]
    only = written[0]
    assert (
        isinstance(only, catalog.WrittenFile)
        and only.size == len(server.stl)
        and only.kind == "stl"
    )
    assert (target / only.name).read_bytes() == server.stl and len(only.sha256) == 64
    assert only.to_json() == {
        "name": "lid_1.stl",
        "size": len(server.stl),
        "sha256": only.sha256,
        "kind": "stl",
    }


def test_download_can_select_files_and_rejects_unknown_names(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with BulkServer(total=5) as server:
        _use(server, monkeypatch)
        _, _, written = catalog.download(
            "https://www.printables.com/model/2-model-2",
            tmp_path / "one",
            select=["top_2.stl"],
        )
        assert [w.name for w in written] == ["top_2.stl"]
        assert (
            server.calls["printables:download-link"] == 1
        )  # only the selected file asked for a link
        with pytest.raises(UsageError, match="no file named 'nope.stl'"):
            catalog.download(
                "https://www.printables.com/model/2-model-2",
                tmp_path / "two",
                select=["nope.stl"],
            )
        assert not (tmp_path / "two").exists()


def test_a_broken_shared_auth_file_points_at_both_credential_commands(tmp_path: Path) -> None:
    """auth.json holds the Hugging Face token and the marketplace credentials: a broken file must not send a
    `3d models` user only to `3d auth hf login` (#504)."""
    from hf_auth import auth_path, read_auth_section

    path = auth_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(UsageError) as exc:
        read_auth_section("thingiverse")
    [step] = exc.value.remediation
    assert "3d auth hf login" in step and "3d models auth set SITE" in step


def test_marketplace_error_reason_is_the_message_without_the_site_prefix() -> None:
    err = MarketplaceError("cults", "http", "boom", remediation=["retry"])
    assert err.message == "cults: boom" and err.reason == "boom"
    assert err.to_json() == {"site": "cults", "kind": "http", "message": "boom", "remediation": ["retry"]}
