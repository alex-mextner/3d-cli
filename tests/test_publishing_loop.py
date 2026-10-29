"""The #579 publish loop: sources (`--of`), remix/make plans with library photos, the non-printable-parts guard,
the Cults route, recording of publications/relations, and the publish offer. No real site is contacted: Thingiverse
and Cults are loopback fakes, the browser routes run through a recording runner, the library is a tmp file."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from errors import GateFailure, InvalidArgument, UsageError
from library import actions, records
from library import db as librarydb
from marketplaces.base import MarketplaceError
from parts import NonPrintableParts
from publishing import execute, flow, guard, offer, plan
from publishing.source import load_source
from publishing.spec import load_spec
from publish_fixtures import (
    CULTS_KEY,
    CULTS_USER,
    HOSTED,
    YAML,
    FakeCults,
    FakeThingiverse,
    make_project,
    three_d_yaml,
)

PRINTABLES = "https://www.printables.com/model/311253-spoon-drawer-pull"
PCB_URL = "https://www.printables.com/model/9001-soil-probe-pcb"
PARTS = f"""\
  wall_plug: {{file: wall_plug.scad}}
  probe_pcb:
    file: vitamins/REF_probe.scad
    role: vitamin
    reference_source: {{url: {PCB_URL}, license: CC BY 4.0}}
"""


@pytest.fixture(autouse=True)
def hermetic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for var, sub in (
        ("XDG_CONFIG_HOME", "config"),
        ("XDG_STATE_HOME", "state"),
        ("XDG_DATA_HOME", "data"),
        ("XDG_CACHE_HOME", "cache"),
    ):
        monkeypatch.setenv(var, str(tmp_path / sub))
    monkeypatch.setenv("THREED_LIBRARY_DB", str(tmp_path / "library.db"))
    monkeypatch.setenv("THREED_KEYCHAIN", "off")
    monkeypatch.delenv("THREED_PUBLISH_OFFER", raising=False)


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    c = librarydb.open_library(tmp_path / "library.db")
    yield c
    c.close()


def model_json(
    directory: Path,
    *,
    license: str = "Creative Commons - Attribution",
    url: str = PRINTABLES,
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "model.json"
    path.write_text(
        json.dumps(
            {
                "source": {"site": "printables", "id": "311253", "url": url},
                "title": "Spoon pull",
                "author": {"name": "ann", "url": ""},
                "license": license,
                "files": [],
            }
        ),
        encoding="utf-8",
    )
    return path


def no_network(url: str) -> str:
    raise AssertionError(f"the marketplace must not be asked for {url}")


def evaluate(project: Path, platforms: str = "all", **opts: Any) -> plan.Evaluation:
    return plan.evaluate(
        project,
        plan.platform_list(platforms),
        no_network,
        plan.Options(platform_all=platforms == "all", **opts),
    )


# ------------------------------------------------------------------------------------------ sources
def test_of_reads_the_licence_from_model_json_file_or_directory_without_the_network(
    tmp_path: Path,
) -> None:
    path = model_json(tmp_path / "dl")
    for of in (str(path), str(path.parent)):
        src = load_source(of)
        assert (
            src.url,
            src.site,
            src.model_id,
            src.license,
            src.author,
            src.origin,
        ) == (
            PRINTABLES,
            "printables",
            "311253",
            "Creative Commons - Attribution",
            "ann",
            "model.json",
        )
    (tmp_path / "other.json").write_text("{}", encoding="utf-8")
    with pytest.raises(InvalidArgument, match="source.url"):
        load_source(str(tmp_path / "other.json"))


def test_of_a_url_takes_the_licence_the_library_recorded(
    conn: sqlite3.Connection,
) -> None:
    records.ensure_source(
        conn,
        site="printables",
        model_id="311253",
        url=PRINTABLES,
        license="CC BY-ND 4.0",
        author="ann",
    )
    src = load_source(PRINTABLES, conn)
    assert src.license == "CC BY-ND 4.0" and src.origin == "library"
    assert load_source("https://example.com/m/1", conn).origin == "url"
    with pytest.raises(Exception, match="not found"):
        load_source("nope.json")


# ------------------------------------------------------------------------------------------ remix / original
def test_remix_of_a_model_json_is_a_remix_with_the_recorded_licence(
    tmp_path: Path,
) -> None:
    project = make_project(tmp_path, YAML.replace("cc-by-nc", "cc-by-nc"))
    src = load_source(str(model_json(tmp_path / "dl", license="CC BY-NC 4.0")))
    ev = plan.evaluate(
        project,
        ["thingiverse"],
        flowless_lookup(src),
        plan.Options(kind="remix", of=src),
    )
    assert (
        ev.kind == "remix"
        and ev.remix is not None
        and ev.remix.source_url == PRINTABLES
    )
    assert ev.remix.problems == () and [p.platform for p in ev.plans] == ["thingiverse"]
    assert f"Remix of {PRINTABLES}" in ev.plans[0].fields["description"]


def flowless_lookup(src: Any) -> Any:
    from publishing.source import lookup_for

    return lookup_for(src)


def test_a_remix_of_a_no_derivatives_model_json_is_refused_until_permission(
    tmp_path: Path,
) -> None:
    project = make_project(tmp_path)
    src = load_source(str(model_json(tmp_path / "dl", license="CC BY-ND 4.0")))
    ev = plan.evaluate(
        project,
        ["thingiverse"],
        flowless_lookup(src),
        plan.Options(kind="remix", of=src),
    )
    assert any("forbids derivative works" in p for p in ev.all_problems())
    (project / "publish.yaml").write_text(
        YAML + "permission_ref: mail 2026-10-24\n", encoding="utf-8"
    )
    ok = plan.evaluate(
        project,
        ["thingiverse"],
        flowless_lookup(src),
        plan.Options(kind="remix", of=src),
    )
    assert ok.all_problems() == ()


def test_kinds_are_checked(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    with pytest.raises(UsageError, match="needs the source"):
        evaluate(project, "thingiverse", kind="remix")
    src = load_source(str(model_json(tmp_path / "dl")))
    with pytest.raises(UsageError, match="not an original"):
        evaluate(project, "thingiverse", kind="original", of=src)
    with pytest.raises(InvalidArgument):
        evaluate(project, "thingiverse", kind="clone")


# ------------------------------------------------------------------------------------------ photos
def test_library_photos_join_the_listing_images_and_missing_files_are_warnings(
    tmp_path: Path,
) -> None:
    project = make_project(tmp_path, images=False)
    table = tmp_path / "table.jpg"
    table.write_bytes(b"jpg")
    photos = (
        plan.Photo(table, "table", 70.0),
        plan.Photo(tmp_path / "gone.jpg", "bed"),
    )
    ev = evaluate(project, "thingiverse", photos=photos)
    assert ev.plans[0].problems == () and ev.plans[0].images == (table,)
    assert any("gone.jpg" in w and "file is gone" in w for w in ev.warnings)
    text = plan.render_text(ev, dry_run=True)
    assert "photos from the library (2)" in text and "(bed at Z 70)" in text
    assert plan.to_json(ev, dry_run=True)["photos"][0] == {
        "path": str(table),
        "kind": "table",
        "z_mm": 70.0,
    }


def test_a_cover_image_may_come_from_a_photo_alone(tmp_path: Path) -> None:
    project = make_project(tmp_path, images=False)
    assert evaluate(project, "makerworld").plans[0].problems  # no cover image
    table = tmp_path / "table.jpg"
    table.write_bytes(b"jpg")
    assert (
        evaluate(project, "makerworld", photos=(plan.Photo(table, "table"),))
        .plans[0]
        .problems
        == ()
    )


# ------------------------------------------------------------------------------------------ make
def test_a_make_plan_posts_the_photos_to_the_model_page_and_stops_before_the_last_click(
    tmp_path: Path,
) -> None:
    project = make_project(tmp_path)
    photo = tmp_path / "table.jpg"
    photo.write_bytes(b"jpg")
    render = tmp_path / "iso.png"
    render.write_bytes(b"png")
    src = load_source(str(model_json(tmp_path / "dl")))
    ev = evaluate(
        project,
        "all",
        kind="make",
        of=src,
        printer="k1se",
        filament="PLA",
        photos=(plan.Photo(photo, "table", 70.0), plan.Photo(render, "render")),
    )
    assert ev.kind == "make" and ev.remix is None and ev.fileset.files == ()
    (item,) = (
        ev.plans
    )  # `all` = the platforms that have a model page: only the source's
    assert item.platform == "printables" and item.problems == ()
    assert item.fields["model_url"] == PRINTABLES and item.fields["photos"] == [
        "table.jpg"
    ]  # a render is no make photo
    assert "Printed with printer k1se, filament PLA" in item.fields["note"]
    assert (
        item.browser_steps[0] == ("open", PRINTABLES)
        and item.browser_steps[1][2] == "Add make"
    )
    assert item.steps[-1].startswith("STOP before the final Post button")
    doc = plan.to_json(ev, dry_run=True)
    assert doc["kind"] == "make" and doc["source"]["url"] == PRINTABLES
    assert doc["platforms"]["printables"]["images"] == [str(photo)]


def test_a_make_needs_a_photo_and_a_model_page(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    src = load_source(str(model_json(tmp_path / "dl")))
    no_photo = evaluate(project, "printables", kind="make", of=src)
    assert any("at least one photo" in p for p in no_photo.all_problems())
    nowhere = evaluate(project, "thingiverse", kind="make", of=src)
    assert any("no model page on thingiverse" in p for p in nowhere.all_problems())
    assert any(
        "no model page to attach" in p
        for p in evaluate(project, "all", kind="make").all_problems()
    )


def test_a_make_of_an_nd_model_needs_no_permission(tmp_path: Path) -> None:
    """Printing a model is what every licence allows: the remix rules do not apply to a make."""
    project = make_project(tmp_path)
    photo = tmp_path / "t.jpg"
    photo.write_bytes(b"x")
    src = load_source(str(model_json(tmp_path / "dl", license="CC BY-NC-ND 4.0")))
    ev = evaluate(
        project, "printables", kind="make", of=src, photos=(plan.Photo(photo, "table"),)
    )
    assert ev.all_problems() == ()


# ------------------------------------------------------------------------------------------ guard
def pcb_project(tmp_path: Path, parts: str = PARTS) -> Path:
    project = make_project(tmp_path)
    three_d_yaml(project, parts)
    (project / "print" / "probe_pcb.stl").write_bytes(b"solid pcb\nendsolid pcb\n")
    return project


def test_a_pcb_reference_model_in_the_publish_set_is_refused_with_exit_1_and_the_plan_is_still_built(
    tmp_path: Path,
) -> None:
    ev = evaluate(pcb_project(tmp_path), "thingiverse")
    assert isinstance(ev.gate, NonPrintableParts) and ev.gate.exit_code == 1
    assert (
        "refusing to publish non-printable part(s): probe_pcb (vitamin)"
        in ev.gate.message
    )
    assert any("--drop-nonprintable" in r for r in ev.gate.remediation)
    assert "probe_pcb.stl" in [
        f.name for f in ev.fileset.files
    ]  # shown, because it would have been sent
    text = plan.render_text(ev, dry_run=True)
    assert (
        "probe_pcb" in text
        and "NOT printable" in text
        and "REFUSED — refusing to publish" in text
    )
    assert plan.to_json(ev, dry_run=True)["refused"] is True
    with pytest.raises(NonPrintableParts):
        flow.refuse_if_blocked(flow.Prepared(ev, tmp_path, "d", "all", ev.kind, None))


def test_drop_removes_the_files_of_non_printable_parts_and_lists_them(
    tmp_path: Path,
) -> None:
    ev = evaluate(pcb_project(tmp_path), "thingiverse", drop_nonprintable=True)
    assert ev.gate is None and "probe_pcb.stl" not in [f.name for f in ev.fileset.files]
    assert any("dropped probe_pcb.stl" in w for w in ev.warnings)
    assert "probe_pcb.stl" not in [p.name for p in ev.plans[0].files]


def test_an_override_needs_a_reason_names_a_real_part_and_is_listed(
    tmp_path: Path,
) -> None:
    project = pcb_project(tmp_path)
    with pytest.raises(UsageError, match="needs --reason"):
        evaluate(project, "thingiverse", allow_nonprintable=("probe_pcb",))
    with pytest.raises(InvalidArgument, match="nothing_here"):
        evaluate(
            project, "thingiverse", allow_nonprintable=("nothing_here",), reason="x"
        )
    ev = evaluate(
        project,
        "thingiverse",
        allow_nonprintable=("probe_pcb",),
        reason="assembly kit, reviewed",
    )
    assert ev.gate is None and "probe_pcb.stl" in [f.name for f in ev.fileset.files]
    assert any(
        "published on purpose: probe_pcb (vitamin) — reason: assembly kit, reviewed"
        in w
        for w in ev.warnings
    )


def test_a_stand_in_is_blocked_too_and_a_printed_part_is_not(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    three_d_yaml(
        project,
        "  wall_plug: {file: wall_plug.scad}\n  wall_plug_ghost: {file: ghost.scad, role: stand_in}\n",
    )
    (project / "print" / "wall_plug_ghost.stl").write_bytes(b"solid g\nendsolid g\n")
    ev = evaluate(project, "thingiverse")
    assert isinstance(ev.gate, NonPrintableParts) and [
        p.name for p in ev.gate.parts
    ] == ["wall_plug_ghost"]


def test_a_part_file_is_matched_by_the_stem_of_its_declared_file(
    tmp_path: Path,
) -> None:
    project = make_project(tmp_path)
    three_d_yaml(
        project,
        f"  board: {{file: vitamins/REF_board.scad, role: vitamin, reference_source: {{url: {PCB_URL}, license: CC0}}}}\n",
    )
    (project / "print" / "REF_board.stl").write_bytes(b"solid b\nendsolid b\n")
    assert isinstance(evaluate(project, "thingiverse").gate, NonPrintableParts)


def test_reference_models_are_attributed_and_their_licence_is_checked_like_a_remix(
    tmp_path: Path,
) -> None:
    project = pcb_project(tmp_path)
    ev = evaluate(project, "thingiverse", drop_nonprintable=True)
    desc = ev.plans[0].fields["description"]
    assert (
        f"- probe_pcb: {PCB_URL} (CC BY 4.0)" in desc
        and "not part of the printable files" in desc
    )
    assert ev.all_problems() == ()
    nd = pcb_project(tmp_path / "nd", PARTS.replace("CC BY 4.0", "CC BY-ND 4.0"))
    bad = evaluate(nd, "thingiverse", drop_nonprintable=True)
    assert any(
        "reference model" in p and "forbids derivative works" in p
        for p in bad.all_problems()
    )
    sa = pcb_project(tmp_path / "sa", PARTS.replace("CC BY 4.0", "CC BY-SA 4.0"))
    assert any(
        "share-alike" in p
        for p in evaluate(sa, "thingiverse", drop_nonprintable=True).all_problems()
    )


def test_a_vitamin_without_a_reference_source_is_a_usage_error(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    three_d_yaml(project, "  board: {file: b.scad, role: vitamin}\n")
    with pytest.raises(InvalidArgument, match="reference_source"):
        guard.load_parts(project)


# ------------------------------------------------------------------------------------------ Cults
def hosted_project(tmp_path: Path) -> Path:
    return make_project(
        tmp_path,
        YAML.replace("cc-by-nc", "cc-by").replace("Household", "Home") + HOSTED,
    )


def test_cults_plan_needs_public_urls_for_every_file_and_image(tmp_path: Path) -> None:
    ev = evaluate(make_project(tmp_path), "cults")
    (problem,) = ev.plans[0].problems[:1]
    assert (
        "Cults pulls files and images from public https URLs" in problem
        and "wall_plug.3mf" in problem
    )
    ok = evaluate(hosted_project(tmp_path / "ok"), "cults")
    item = ok.plans[0]
    assert item.problems == ()
    assert item.fields["file_urls"] == [
        f"https://files.example.com/wall_plug.{e}" for e in ("3mf", "scad", "stl")
    ]
    assert item.fields["image_urls"] == ["https://files.example.com/iso.png"]
    assert (
        item.steps[2].startswith("POST")
        and "createCreation" in item.steps[2]
        and "downloadPrice: 0" in item.steps[2]
    )


def test_all_includes_cults_only_when_the_project_declares_hosted_urls(
    tmp_path: Path,
) -> None:
    assert [p.platform for p in evaluate(make_project(tmp_path), "all").plans] == [
        "makerworld",
        "printables",
        "thingiverse",
    ]
    assert [p.platform for p in evaluate(hosted_project(tmp_path / "h"), "all").plans][
        -1
    ] == "cults"


def test_cults_route_resolves_ids_then_creates_with_basic_auth(tmp_path: Path) -> None:
    item = evaluate(hosted_project(tmp_path), "cults").plans[0]
    fake = FakeCults()
    try:
        url = execute.publish_cults(
            item, "cc-by", credentials=(CULTS_USER, CULTS_KEY), api=fake.base
        )
    finally:
        fake.close()
    assert url == "https://cults3d.com/en/3d-model/home/wall-plug"
    assert [
        ("categories" in q, "licenses" in q, "createCreation" in q)
        for _, q, _ in fake.calls
    ] == [(True, False, False), (False, True, False), (False, False, True)]
    assert all(auth.startswith("Basic ") for auth, _, _ in fake.calls)
    variables = fake.calls[2][2]
    assert (
        variables["categoryId"] == "Q2F0ZWdvcnkvMQ=="
        and variables["licenseCode"] == "cc_by"
    )
    assert (
        variables["fileUrls"] == item.fields["file_urls"]
        and variables["imageUrls"] == item.fields["image_urls"]
    )
    assert variables["name"] == "Wall plug" and variables["tagNames"] == [
        "plug",
        "wall",
    ]


def test_cults_route_explains_an_unknown_category_licence_or_refusal(
    tmp_path: Path,
) -> None:
    item = evaluate(hosted_project(tmp_path), "cults").plans[0]
    fake = FakeCults()
    try:
        with pytest.raises(MarketplaceError, match="does not offer a licence"):
            execute.publish_cults(
                item, "gpl", credentials=(CULTS_USER, CULTS_KEY), api=fake.base
            )
        other = plan.PlatformPlan(
            **{**item.__dict__, "fields": {**item.fields, "category": "Toys"}}
        )
        with pytest.raises(MarketplaceError, match="no category named 'Toys'") as exc:
            execute.publish_cults(
                other, "cc-by", credentials=(CULTS_USER, CULTS_KEY), api=fake.base
            )
        assert "Home" in exc.value.remediation[0]
    finally:
        fake.close()
    bad = FakeCults(create="cults_create_error")
    try:
        with pytest.raises(MarketplaceError, match="Name is too short"):
            execute.publish_cults(
                item, "cc-by", credentials=(CULTS_USER, CULTS_KEY), api=bad.base
            )
    finally:
        bad.close()


# ------------------------------------------------------------------------------------------ recording
def prepared(project: Path, db: Path, **kw: Any) -> flow.Prepared:
    return flow.prepare(project, db_path=str(db), **kw)


def test_publishing_records_publication_remix_relation_and_renders_and_deletes_nothing(
    tmp_path: Path,
) -> None:
    project = make_project(tmp_path)
    db = tmp_path / "library.db"
    src = model_json(tmp_path / "dl", license="Creative Commons - Attribution")
    fake = FakeThingiverse()
    try:
        prep = prepared(
            project,
            db,
            platform="thingiverse",
            kind="remix",
            of=str(src),
        )
        flow.refuse_if_blocked(prep)
        outcomes = flow.send(prep, thingiverse_api=fake.base, token_for=lambda: "tok")
        again = flow.send(
            prepared(
                project,
                db,
                platform="thingiverse",
                kind="remix",
                of=str(src),
            ),
            thingiverse_api=fake.base,
            token_for=lambda: "tok",
        )
    finally:
        fake.close()
    assert [o.state for o in outcomes] == ["published"] and outcomes[
        0
    ].url == "https://www.thingiverse.com/thing:777"
    assert (
        again[0].publication != outcomes[0].publication
    )  # a second attempt is a second row
    c = librarydb.open_library(db)
    try:
        pubs = records.list_publications(c, "proj")
        assert [(p["platform"], p["state"]) for p in pubs] == [
            ("thingiverse", "published")
        ] * 2
        rels = records.list_relations(c, "proj")
        assert [(r["rel"], r["to"]["label"]) for r in rels] == [
            ("remix_of", f"source {PRINTABLES}")
        ]
        assert c.execute("SELECT license, author FROM model_source").fetchone()[:] == (
            "Creative Commons - Attribution",
            "ann",
        )
        assert [
            p["kind"]
            for p in records.photos_of(
                c, design_id=int(records.resolve(c, "design", "proj")["id"])
            )
        ] == ["render"]
    finally:
        c.close()


def test_a_failed_platform_stays_as_a_failed_row(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    db = tmp_path / "library.db"
    fake = FakeThingiverse(fail_publish=True)
    try:
        outcomes = flow.send(
            prepared(project, db, platform="thingiverse"),
            thingiverse_api=fake.base,
            token_for=lambda: "tok",
        )
    finally:
        fake.close()
    assert [(o.ok, o.state) for o in outcomes] == [
        (False, "failed")
    ] and "unpublished draft" in outcomes[0].message
    c = librarydb.open_library(db)
    try:
        assert [(p["platform"], p["state"]) for p in records.list_publications(c)] == [
            ("thingiverse", "failed")
        ]
    finally:
        c.close()


def test_the_chain_source_remix_make_is_in_the_library(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    db = tmp_path / "library.db"
    src = model_json(tmp_path / "dl")
    fake = FakeThingiverse()
    try:
        c = librarydb.open_library(db)
        records.ensure_design(c, "proj", str(project))
        records.add_make(c, "proj", printer="k1se", status="done")
        c.close()
        flow.send(
            prepared(
                project,
                db,
                platform="thingiverse",
                kind="remix",
                of=str(src),
            ),
            thingiverse_api=fake.base,
            token_for=lambda: "tok",
        )
    finally:
        fake.close()
    c = librarydb.open_library(db)
    try:
        graph = records.graph(c, "proj")
    finally:
        c.close()
    assert {(e["rel"], e["from"]["kind"], e["to"]["kind"]) for e in graph["edges"]} == {
        ("remix_of", "design", "model_source"),
        ("make_of", "make", "design"),
    }


def test_an_override_is_logged_as_a_library_event(tmp_path: Path) -> None:
    project = pcb_project(tmp_path)
    db = tmp_path / "library.db"
    fake = FakeThingiverse()
    try:
        prep = prepared(
            project,
            db,
            platform="thingiverse",
            allow=("probe_pcb",),
            reason="assembly kit",
        )
        flow.refuse_if_blocked(prep)
        flow.send(prep, thingiverse_api=fake.base, token_for=lambda: "tok")
    finally:
        fake.close()
    c = librarydb.open_library(db)
    try:
        (event,) = c.execute(
            "SELECT subject, payload FROM events WHERE kind = 'library.publish.override'"
        ).fetchall()
    finally:
        c.close()
    assert json.loads(event["payload"]) == {
        "kind": "original",
        "part": "probe_pcb",
        "reason": "assembly kit",
        "role": "vitamin",
    }


def test_a_make_post_is_recorded_as_a_make_publication(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    db = tmp_path / "library.db"
    src = model_json(tmp_path / "dl")
    table = tmp_path / "table.jpg"
    table.write_bytes(b"jpg")
    c = librarydb.open_library(db)
    make, _ = records.add_make(c, "proj", printer="k1se", status="done")
    records.add_photo(c, str(table), make=str(make["uid"]), kind="table", z_mm=70)
    c.close()
    prep = prepared(
        project,
        db,
        platform="printables",
        kind="make",
        of=str(src),
        make=str(make["uid"]),
    )
    assert prep.ev.all_problems() == () and prep.ev.plans[0].fields["photos"] == [
        "table.jpg"
    ]
    calls: list[list[str]] = []

    def runner(argv: Any) -> tuple[int, str]:
        calls.append(list(argv))
        return (
            0,
            "ws://x" if "cdp-url" in argv else "https://www.printables.com/model/1",
        )

    out = flow.send(prep, runner=runner)
    assert out[0].state == "draft" and out[0].ok
    assert any("upload" in c for c in calls) and not any(
        "Publish" in " ".join(c) or "Post" in " ".join(c) for c in calls
    )
    c = librarydb.open_library(db)
    try:
        assert [(p["platform"], p["state"]) for p in records.list_publications(c)] == [
            ("printables:make", "draft")
        ]
    finally:
        c.close()


# ------------------------------------------------------------------------------------------ actions
def test_the_publish_action_plans_by_default_and_reports_refusals(
    tmp_path: Path,
) -> None:
    project = make_project(tmp_path)
    db = str(tmp_path / "library.db")
    c = librarydb.open_library(db)
    records.ensure_design(c, "proj", str(project))
    c.close()
    res = actions.publish("proj", platforms=["thingiverse"], db_path=db)
    assert (
        res["ok"]
        and res["data"]["dry_run"]
        and res["data"]["publications"]
        == [{"platform": "thingiverse", "state": "planned", "url": None}]
    )
    assert (
        res["data"]["plan"]["schema"] == "3d.publish/1"
        and res["warnings"][0]["code"] == "dry_run"
    )
    c = librarydb.open_library(db)
    try:
        assert (
            c.execute("SELECT COUNT(*) FROM publication").fetchone()[0] == 0
        )  # a plan writes nothing
    finally:
        c.close()
    three_d_yaml(project, PARTS)
    (project / "print" / "probe_pcb.stl").write_bytes(b"solid\nendsolid\n")
    blocked = actions.invoke(
        "publish", design="proj", platforms=["thingiverse"], db_path=db
    )
    assert (
        not blocked["ok"]
        and blocked["errors"][0]["code"] == "refused"
        and "probe_pcb" in blocked["errors"][0]["message"]
    )
    assert (
        actions.invoke("publish", design="ghost", db_path=db)["errors"][0]["code"]
        == "error"
    )


def test_the_photo_action_registers_files_and_is_idempotent_under_a_source_key(
    tmp_path: Path,
) -> None:
    db = str(tmp_path / "library.db")
    img = tmp_path / "a.png"
    img.write_bytes(b"x")
    made = actions.queue("mole", db_path=db)
    first = actions.photo(
        str(img), make=made["data"]["make"], kind="render", source_key="k", db_path=db
    )
    second = actions.photo(
        str(img), make=made["data"]["make"], kind="render", source_key="k", db_path=db
    )
    assert first["ok"] and first["data"]["created"] and not second["data"]["created"]
    assert first["data"]["photo"] == second["data"]["photo"]
    with pytest.raises(InvalidArgument):
        actions.photo(
            None, make=made["data"]["make"], capture=True, design="mole", db_path=db
        )


# ------------------------------------------------------------------------------------------ offer
class Spy:
    """Records what the offer sent, instead of sending."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    def __call__(self, prep: flow.Prepared) -> list[flow.Outcome]:
        self.sent.append(prep.kind)
        return [
            flow.Outcome(
                p.platform, True, "published", f"https://x/{p.platform}", "ok", None
            )
            for p in prep.ev.plans
        ]


def offer_world(
    tmp_path: Path, *, source: str | None = None, remix: bool = False
) -> tuple[Path, Path]:
    """A project (3mf + scad + a preview) and a library that knows its design with a `printing` make."""
    project = make_project(
        tmp_path, "category: Household\n"
    )  # title, description, licence: the offer assumes them
    db = tmp_path / "library.db"
    c = librarydb.open_library(db)
    try:
        src_uid = None
        if source is not None:
            row, _ = records.ensure_source(
                c,
                site="printables",
                model_id="311253",
                url=PRINTABLES,
                license=source,
                author="ann",
            )
            src_uid = str(row["uid"])
            if remix:
                d = records.ensure_design(c, "proj", str(project))
                records.relate(
                    c, "remix_of", "design", str(d["uid"]), "model_source", src_uid
                )
        records.add_make(
            c,
            "proj",
            printer="k1se",
            filament="PLA",
            status="printing",
            source_uid=src_uid,
            project_dir=str(project),
        )
    finally:
        c.close()
    return project, db


def run(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    db: Path,
    project: Path | None = None,
    **kw: Any,
) -> tuple[int, str, Spy]:
    spy = Spy()
    kw.setdefault("trigger", "print")
    kw.setdefault("mode", None)
    kw.setdefault("yes", False)
    kw.setdefault("as_json", False)
    out: list[str] = []
    code = offer.run_offer(project, db_path=str(db), send=spy, out=out.append, **kw)
    return code, "\n".join(out), spy


def test_off_is_silent_and_does_not_even_open_the_library(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, text, spy = run(
        tmp_path,
        capsys,
        tmp_path / "never.db",
        project=make_project(tmp_path),
        mode="off",
    )
    assert (code, text, spy.sent) == (0, "", []) and not (
        tmp_path / "never.db"
    ).exists()
    code, text, _ = run(
        tmp_path,
        capsys,
        tmp_path / "never.db",
        project=make_project(tmp_path / "j"),
        mode="off",
        as_json=True,
    )
    assert json.loads(text) == {
        "schema": "3d.publish.offer/1",
        "mode": "off",
        "status": "off",
        "trigger": "print",
    }


def test_the_offer_is_for_all_platforms_with_cad_renders_and_photos_under_a_stated_default_licence(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project, db = offer_world(tmp_path)
    c = librarydb.open_library(db)
    make = c.execute("SELECT uid, id FROM make").fetchone()
    table = tmp_path / "table.jpg"
    table.write_bytes(b"jpg")
    records.add_photo(c, str(table), make=str(make["uid"]), kind="table", z_mm=70)
    c.close()
    code, text, spy = run(
        tmp_path,
        capsys,
        db,
        project=project,
        as_json=True,
        mode="ask",
        interactive=lambda: False,
    )
    doc = json.loads(text)
    assert (
        code == 0
        and spy.sent == []
        and doc["status"] == "offered"
        and doc["schema"] == "3d.publish.offer/1"
    )
    assert (
        doc["license"]["id"] == "cc-by"
        and doc["license"]["open"]
        and "default" in doc["license"]["why"]
    )
    (p,) = doc["plans"]
    assert p["kind"] == "original" and sorted(p["platforms"]) == [
        "makerworld",
        "printables",
        "thingiverse",
    ]
    assert (
        {f["role"] for f in p["files"]} == {"3mf", "scad", "stl"}
        and p["images"]
        and p["photos"][0]["kind"] == "table"
    )
    assert set(p["defaults_used"]) == {"title", "description", "license"}
    assert (
        doc["command"][0][:3] == ["3d", "models", "offer"]
        and "--trigger" in doc["command"][0]
        and doc["command"][0][-1] == "--yes"
    )
    argv = doc["command"][0]
    replay_code, _, replay = run(
        tmp_path,
        capsys,
        db,
        project=Path(argv[3]),
        trigger=argv[argv.index("--trigger") + 1],
        platform=argv[argv.index("--platform") + 1],
        make=argv[argv.index("--make") + 1] if "--make" in argv else None,
        yes=True,
        mode="ask",
    )
    assert replay_code == 0 and replay.sent == ["original"]


def test_ask_on_a_terminal_publishes_only_after_a_yes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project, db = offer_world(tmp_path)
    asked: list[str] = []
    for answer, expected in (("y", ["original"]), ("", ["original"]), ("n", [])):

        def reply(question: str, answer: str = answer) -> str:
            asked.append(question)
            return answer

        spy = Spy()
        out: list[str] = []
        code = offer.run_offer(
            project,
            trigger="print",
            mode="ask",
            yes=False,
            as_json=False,
            db_path=str(db),
            interactive=lambda: True,
            ask=reply,
            out=out.append,
            send=spy,
        )
        assert code == 0 and spy.sent == expected, answer
    assert (
        "[Y/n]" in asked[0]
        and "all platforms with CAD files, renders and photos" in asked[0]
    )


def test_without_a_terminal_ask_prints_the_plan_and_the_command_but_never_publishes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project, db = offer_world(tmp_path)
    code, text, spy = run(
        tmp_path, capsys, db, project=project, mode="ask", interactive=lambda: False
    )
    assert (
        code == 0
        and spy.sent == []
        and "not published: confirm" in text
        and "3d models offer" in text
        and "--yes" in text
    )


def test_auto_publishes_only_with_yes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project, db = offer_world(tmp_path)
    code, text, spy = run(
        tmp_path,
        capsys,
        db,
        project=project,
        mode="auto",
        interactive=lambda: True,
        as_json=True,
    )
    assert spy.sent == [] and json.loads(text)["status"] == "needs_confirmation"
    code, text, spy = run(
        tmp_path,
        capsys,
        db,
        project=project,
        mode="auto",
        yes=True,
        interactive=lambda: False,
        as_json=True,
    )
    doc = json.loads(text)
    assert (
        code == 0
        and spy.sent == ["original"]
        and doc["status"] == "published"
        and len(doc["outcomes"]) == 3
    )


def test_a_remix_offer_inherits_what_the_source_licence_requires(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project, db = offer_world(tmp_path, source="CC BY-NC-SA 4.0", remix=True)
    code, text, spy = run(
        tmp_path,
        capsys,
        db,
        project=project,
        trigger="modify",
        mode="ask",
        as_json=True,
        interactive=lambda: False,
    )
    doc = json.loads(text)
    assert (
        doc["license"]["id"] == "cc-by-nc-sa"
        and "inherited from the source" in doc["license"]["why"]
    )
    assert (
        doc["plans"][0]["kind"] == "remix"
        and doc["plans"][0]["remix"]["source"] == PRINTABLES
    )
    assert doc["plans"][0]["problems"] == [] or all(
        "licence" not in p for p in doc["plans"][0]["problems"]
    )


def test_an_nd_source_offers_the_plan_but_it_is_refused_so_nothing_is_sent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project, db = offer_world(tmp_path, source="CC BY-ND 4.0", remix=True)
    spy = Spy()
    out: list[str] = []
    with pytest.raises(UsageError, match="publish refused"):
        offer.run_offer(
            project,
            trigger="modify",
            mode="auto",
            yes=True,
            as_json=False,
            db_path=str(db),
            send=spy,
            out=out.append,
        )
    assert spy.sent == [] and "forbids derivative works" in "\n".join(out)


def test_a_print_of_somebody_elses_unmodified_model_offers_a_make_not_a_remix(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project, db = offer_world(tmp_path, source="CC BY 4.0")
    code, text, spy = run(
        tmp_path,
        capsys,
        db,
        project=project,
        mode="ask",
        as_json=True,
        interactive=lambda: False,
    )
    doc = json.loads(text)
    assert [p["kind"] for p in doc["plans"]] == ["make"] and doc["source"] == PRINTABLES
    assert (
        doc["plans"][0]["problems"]
        and "at least one photo" in doc["plans"][0]["problems"][0]
    )


def test_the_mode_default_comes_from_the_environment_then_the_config_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert offer.default_mode() == "ask"
    cfg = offer.config_path()
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text('{"offer": "off"}', encoding="utf-8")
    assert offer.default_mode() == "off"
    monkeypatch.setenv("THREED_PUBLISH_OFFER", "auto")
    assert offer.default_mode() == "auto"
    monkeypatch.setenv("THREED_PUBLISH_OFFER", "sometimes")
    with pytest.raises(InvalidArgument):
        offer.default_mode()


def test_the_bed_free_hook_offers_only_for_a_printer_the_library_knows_and_not_when_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, db = offer_world(tmp_path)
    monkeypatch.setenv("THREED_LIBRARY_DB", str(db))
    (line, photo_line) = offer.agent_lines("k1se")
    assert (
        "3d models offer --make" in line
        and "ASK before publishing" in line
        and "3d photo capture --make" in photo_line
    )
    assert offer.agent_lines("u1") == []  # no make there
    monkeypatch.setenv("THREED_PUBLISH_OFFER", "off")
    assert offer.agent_lines("k1se") == []


def test_the_subscription_message_carries_the_offer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from printer_ops.subscriptions import (
        bed_free_event,
        bed_free_message,
        make as make_sub,
    )

    project, db = offer_world(tmp_path)
    monkeypatch.setenv("THREED_LIBRARY_DB", str(db))
    event = bed_free_event(
        "k1se", {"bed": "clear", "plate": "present", "z": 70.0, "view": {}}, 1.0
    )
    sub = make_sub(
        session="s1",
        printers=["k1se"],
        created="2026-10-24T00:00:00Z",
        on="bed-free",
        once=True,
    )
    assert "4. Publish offer" in bed_free_message(event, sub)
    monkeypatch.setenv("THREED_PUBLISH_OFFER", "off")
    assert "Publish offer" not in bed_free_message(event, sub)


def test_after_modify_is_silent_when_off_and_a_stderr_hint_otherwise(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    project = make_project(tmp_path)
    out = project / "mole.deformed.stl"
    out.write_bytes(b"solid")
    monkeypatch.setenv("THREED_PUBLISH_OFFER", "off")
    offer.after_modify(out)
    silent = capsys.readouterr()
    assert (silent.out, silent.err) == ("", "")
    monkeypatch.setenv("THREED_PUBLISH_OFFER", "ask")
    offer.after_modify(out)
    err = capsys.readouterr().err
    assert (
        "3d models offer" in err
        and "--trigger modify" in err
        and "--publish off" in err
    )


def test_load_spec_defaults_are_listed_and_only_used_for_what_is_missing(
    tmp_path: Path,
) -> None:
    project = make_project(tmp_path, "title: Mine\ndescription: d\nlicense: cc0\n")
    spec = load_spec(project, {"title": "X", "description": "Y", "license": "cc-by"})
    assert (spec.title, spec.license.id, spec.defaults_used) == ("Mine", "cc0", ())
    bare = make_project(tmp_path / "bare", "")
    got = load_spec(bare, {"title": "X", "description": "Y", "license": "cc-by"})
    assert got.defaults_used == ("title", "description", "license")
    with pytest.raises(Exception, match="publish.yaml"):
        load_spec(bare)


def test_gate_failures_keep_exit_code_1(tmp_path: Path) -> None:
    ev = evaluate(pcb_project(tmp_path), "thingiverse")
    assert isinstance(ev.gate, GateFailure) and ev.gate.exit_code == 1
