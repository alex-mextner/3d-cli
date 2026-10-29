"""Unit tests for lib/publishing: licence parsing, project file selection, the remix refusal
rules, the per-platform plans and their dry-run rendering, and the executors against a
loopback Thingiverse fake and a recording agent-browser runner. No real site is contacted."""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest
from publish_fixtures import YAML, FakeThingiverse, make_project

from errors import InputNotFound, InvalidArgument, UsageError
from marketplaces.base import MarketplaceError
from publishing import execute, plan
from publishing.licenses import LICENSES, parse_license
from publishing.remix import LicenseLookup, check_remix
from publishing.spec import PublishSpec, load_spec, select_files

SDFL_URL = "https://makerworld.com/en/models/2592818"


def lookup_returning(text: str) -> Callable[[str], str]:
    return lambda url: text


def lookup_failing(url: str) -> str:
    raise MarketplaceError("makerworld", "network", "cannot reach the site")


# -- licences -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Standard Digital File License", "sdfl"),
        ("SDFL", "sdfl"),
        ("CC BY-NC-SA 4.0", "cc-by-nc-sa"),
        ("BY-ND", "cc-by-nd"),
        ("Creative Commons - Attribution", "cc-by"),
        (
            "Creative Commons - Attribution - Non-Commercial - Share Alike",
            "cc-by-nc-sa",
        ),
        ("Creative Commons - Attribution - No Derivatives", "cc-by-nd"),
        ("cc-nc-nd", "cc-by-nc-nd"),
        ("cc", "cc-by"),
        ("BY", "cc-by"),
        ("pd0", "cc0"),
        ("Public Domain", "cc0"),
        ("GNU - GPL", "gpl"),
        ("GNU - LGPL", "lgpl"),
        ("All rights reserved", "arr"),
    ],
)
def test_parse_license_recognises_what_the_sites_print(
    text: str, expected: str
) -> None:
    parsed = parse_license(text)
    assert parsed is not None and parsed.id == expected


@pytest.mark.parametrize(
    "text",
    ["", "   ", "my own terms", "share-alike nd", "Creative Commons", "Creative Commons custom terms"],
)
def test_parse_license_returns_none_for_what_it_does_not_know(text: str) -> None:
    assert parse_license(text) is None


def test_licence_flags_match_the_licence_terms() -> None:
    assert (
        LICENSES["cc-by-nd"].forbids_derivatives
        and LICENSES["sdfl"].forbids_derivatives
    )
    assert (
        LICENSES["cc-by-nc-sa"].share_alike and LICENSES["cc-by-nc-sa"].non_commercial
    )
    assert not LICENSES["cc-by"].forbids_derivatives


# -- file selection -------------------------------------------------------------------


def test_select_files_takes_3mf_scad_stl_and_cad_but_never_references_or_gcode(
    tmp_path: Path,
) -> None:
    project = make_project(tmp_path)
    (project / "wall_plug.step").write_bytes(b"ISO-10303")
    (project / "wall_plug.f3d").write_bytes(b"f3d")
    (project / "wall_plug.blend").write_bytes(b"BLENDER")
    (project / ".hidden.scad").write_text("x")
    (project / "print" / "plate.stl").write_bytes(b"solid p\nendsolid p\n")
    found = select_files(project)
    assert [(f.role, f.path.relative_to(project).as_posix()) for f in found.files] == [
        ("3mf", "print/wall_plug.3mf"),
        ("scad", "wall_plug.scad"),
        ("stl", "print/plate.stl"),
        ("stl", "wall_plug.stl"),
        ("cad", "wall_plug.blend"),
        ("cad", "wall_plug.f3d"),
        ("cad", "wall_plug.step"),
    ]
    assert found.missing() == () and found.clashes == ()


def test_select_files_reports_missing_mandatory_roles(tmp_path: Path) -> None:
    found = select_files(make_project(tmp_path, mf3=False, scad=False))
    assert found.missing() == ("3mf", "scad")


def test_select_files_reports_two_files_with_the_same_upload_name(
    tmp_path: Path,
) -> None:
    project = make_project(tmp_path)
    (project / "print" / "wall_plug.stl").write_bytes(b"solid q\nendsolid q\n")
    assert select_files(project).clashes == ("wall_plug.stl",)


def test_select_files_skips_symlinks(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    outside = tmp_path / "secret.step"
    outside.write_bytes(b"secret")
    (project / "link.step").symlink_to(outside)
    assert all(f.name != "link.step" for f in select_files(project).files)


# -- publish.yaml ---------------------------------------------------------------------


def test_load_spec_reads_every_field_and_defaults_images_to_previews(
    tmp_path: Path,
) -> None:
    project = make_project(
        tmp_path, YAML + f"remix_of: {SDFL_URL}\npermission_ref: mail 2026-10-24\n"
    )
    spec = load_spec(project)
    assert (spec.title, spec.tags, spec.license.id, spec.category) == (
        "Wall plug",
        ("plug", "wall"),
        "cc-by-nc",
        "Household",
    )
    assert spec.remix_of == SDFL_URL and spec.permission_ref == "mail 2026-10-24"
    assert [i.name for i in spec.images] == [
        "iso.png"
    ]  # the .dims.json is not an image


def test_a_symlinked_print_folder_is_not_scanned(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "private.scad").write_text("cube(1);", encoding="utf-8")
    shutil.rmtree(project / "print")
    (project / "print").symlink_to(outside, target_is_directory=True)
    assert "private.scad" not in [f.name for f in select_files(project).files]


def test_a_print_folder_linked_to_references_is_not_scanned(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    refs = project / "references"
    refs.mkdir(exist_ok=True)
    (refs / "original.scad").write_text("cube(2);", encoding="utf-8")
    shutil.rmtree(project / "print")
    (project / "print").symlink_to(refs, target_is_directory=True)
    assert "original.scad" not in [f.name for f in select_files(project).files]


@pytest.mark.parametrize("tags", ["false", "0", '""', "{}"])
def test_malformed_tags_are_refused_not_dropped(tmp_path: Path, tags: str) -> None:
    project = make_project(tmp_path, YAML + f"tags: {tags}\n")
    with pytest.raises(InvalidArgument):
        load_spec(project)


def test_whitespace_only_required_fields_are_refused(tmp_path: Path) -> None:
    project = make_project(tmp_path, YAML.replace("title: Wall plug", 'title: "   "'))
    with pytest.raises(InvalidArgument):
        load_spec(project)


def test_a_form_step_that_lands_on_a_login_page_fills_the_login(
    tmp_path: Path,
) -> None:
    item = evaluate(make_project(tmp_path), "makerworld").plans[0]

    calls: list[list[str]] = []

    def bounced(argv: Sequence[str]) -> tuple[int, str]:
        calls.append(list(argv))
        if "get" in argv and "cdp-url" in argv:
            return 0, "ws://127.0.0.1:9222/devtools/browser/x"
        if "get" in argv and "url" in argv:
            # the home page is fine; after the failed Upload click the site shows its login page
            return 0, "https://makerworld.com/en/login" if len(calls) > 3 else "https://makerworld.com/en"
        return (1, "no element matches") if "click" in argv else (0, "")

    outcome = execute.publish_browser(item, bounced)
    assert not outcome.ok
    assert "press Sign in" in outcome.message
    assert any(c[:2] == ["keychain", "fill"] for c in calls)


def test_default_images_skip_symlinks_that_leave_the_project(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    outside = tmp_path / "private.png"
    outside.write_bytes(b"\x89PNG")
    (project / "previews" / "leak.png").symlink_to(outside)
    assert [i.name for i in load_spec(project).images] == ["iso.png"]


def test_load_spec_accepts_a_licence_written_as_text(tmp_path: Path) -> None:
    spec = load_spec(make_project(tmp_path, YAML.replace("cc-by-nc", "CC BY-SA 4.0")))
    assert spec.license.id == "cc-by-sa"


@pytest.mark.parametrize(
    "yaml_text, error",
    [
        ("", InputNotFound),
        ("- just\n- a list\n", UsageError),
        (YAML + "colour: red\n", UsageError),
        (YAML.replace("license: cc-by-nc\n", ""), InvalidArgument),
        (YAML.replace("cc-by-nc", "my own terms"), InvalidArgument),
        (YAML.replace("title: Wall plug\n", ""), InvalidArgument),
        (YAML + "remix_of: not-a-url\n", InvalidArgument),
        (YAML + "images: [../outside.png]\n", InvalidArgument),
        (YAML + "images: [previews/missing.png]\n", InputNotFound),
        (YAML.replace("[plug, wall]", "plug"), InvalidArgument),
        ("title: [unclosed\n", UsageError),
    ],
)
def test_load_spec_refuses_a_defective_publish_yaml(
    tmp_path: Path, yaml_text: str, error: type[Exception]
) -> None:
    project = make_project(tmp_path, yaml_text)
    with pytest.raises(error):
        load_spec(project)


# -- remix rules ----------------------------------------------------------------------


def spec_for(
    tmp_path: Path, extra: str = "", license_id: str = "cc-by-nc"
) -> PublishSpec:
    return load_spec(
        make_project(tmp_path, YAML.replace("cc-by-nc", license_id) + extra)
    )


def test_a_project_that_is_not_a_remix_has_no_remix_check(tmp_path: Path) -> None:
    assert check_remix(spec_for(tmp_path), lookup_failing) is None


def test_remix_of_a_no_derivatives_licence_is_refused_without_permission(
    tmp_path: Path,
) -> None:
    spec = spec_for(tmp_path, f"remix_of: {SDFL_URL}\n")
    verdict = check_remix(spec, lookup_returning("Standard Digital File License"))
    assert verdict is not None and len(verdict.problems) == 1
    assert (
        "forbids derivative works" in verdict.problems[0]
        and "permission_ref" in verdict.problems[0]
    )
    assert verdict.evidence == ("marketplace lists 'Standard Digital File License'",)


@pytest.mark.parametrize("source", ["BY-ND", "BY-NC-ND", "All rights reserved"])
def test_every_no_derivatives_licence_is_refused(tmp_path: Path, source: str) -> None:
    verdict = check_remix(
        spec_for(tmp_path, f"remix_of: {SDFL_URL}\n"), lookup_returning(source)
    )
    assert verdict is not None and verdict.problems


def test_permission_ref_lifts_the_no_derivatives_barrier_and_says_so(
    tmp_path: Path,
) -> None:
    spec = spec_for(
        tmp_path, f"remix_of: {SDFL_URL}\npermission_ref: https://example.com/mail/42\n"
    )
    verdict = check_remix(spec, lookup_returning("Standard Digital File License"))
    assert verdict is not None and verdict.problems == ()
    assert len(verdict.waived) == 1 and "forbids derivative works" in verdict.waived[0]


def test_an_unverifiable_source_licence_is_refused_not_assumed_free(
    tmp_path: Path,
) -> None:
    spec = spec_for(tmp_path, f"remix_of: {SDFL_URL}\n")
    verdict = check_remix(spec, lookup_failing)
    assert verdict is not None and "cannot tell which licence" in verdict.problems[0]
    assert "lookup failed" in verdict.evidence[0]


def test_a_source_licence_text_nobody_knows_is_refused_too(tmp_path: Path) -> None:
    verdict = check_remix(spec_for(tmp_path, f"remix_of: {SDFL_URL}\n"), lookup_returning("my own terms"))
    assert verdict is not None and "'my own terms' is not a licence this tool knows" in verdict.problems[0]


def test_a_declared_licence_stands_in_when_the_lookup_fails(tmp_path: Path) -> None:
    spec = spec_for(tmp_path, f"remix_of: {SDFL_URL}\nremix_license: CC BY\n")
    verdict = check_remix(spec, lookup_failing)
    assert verdict is not None and verdict.problems == ()


def test_a_declaration_never_loosens_what_the_site_says(tmp_path: Path) -> None:
    spec = spec_for(tmp_path, f"remix_of: {SDFL_URL}\nremix_license: CC BY\n")
    verdict = check_remix(spec, lookup_returning("Standard Digital File License"))
    assert verdict is not None and any(
        "forbids derivative works" in p for p in verdict.problems
    )


def test_a_non_commercial_source_needs_a_non_commercial_result_even_with_permission(
    tmp_path: Path,
) -> None:
    spec = spec_for(
        tmp_path, f"remix_of: {SDFL_URL}\npermission_ref: ok\n", license_id="cc-by"
    )
    verdict = check_remix(spec, lookup_returning("CC BY-NC-ND"))
    assert verdict is not None
    assert [p for p in verdict.problems if "non-commercial" in p] and not [
        p for p in verdict.problems if "forbids" in p
    ]


def test_a_share_alike_source_needs_a_share_alike_result(tmp_path: Path) -> None:
    refused = check_remix(
        spec_for(tmp_path, f"remix_of: {SDFL_URL}\n", "cc-by"),
        lookup_returning("CC BY-SA"),
    )
    assert refused is not None and "share-alike" in refused.problems[0]
    allowed = check_remix(
        spec_for(tmp_path / "b", f"remix_of: {SDFL_URL}\n", "cc-by-sa"),
        lookup_returning("CC BY-SA"),
    )
    assert allowed is not None and allowed.problems == ()


def test_a_by_sa_source_is_not_republished_under_by_nc_sa(tmp_path: Path) -> None:
    refused = check_remix(
        spec_for(tmp_path, f"remix_of: {SDFL_URL}\n", "cc-by-nc-sa"),
        lookup_returning("CC BY-SA"),
    )
    assert refused is not None
    assert any("publish under CC BY-SA" in p for p in refused.problems)


def test_two_projects_with_one_title_get_separate_browser_sessions(
    tmp_path: Path,
) -> None:
    first = evaluate(make_project(tmp_path / "a"), "makerworld").plans[0]
    second = evaluate(make_project(tmp_path / "b"), "makerworld").plans[0]
    assert execute._session_name(first) != execute._session_name(second)


def test_a_gpl_source_needs_a_gpl_result(tmp_path: Path) -> None:
    verdict = check_remix(
        spec_for(tmp_path, f"remix_of: {SDFL_URL}\n", "cc-by"),
        lookup_returning("GNU - GPL"),
    )
    assert verdict is not None and "GPL" in verdict.problems[0]


def test_a_permissive_source_allows_any_result(tmp_path: Path) -> None:
    verdict = check_remix(
        spec_for(tmp_path, f"remix_of: {SDFL_URL}\n"), lookup_returning("CC0")
    )
    assert verdict is not None and verdict.problems == ()


# -- plans ----------------------------------------------------------------------------


def evaluate(project: Path, platform: str = "all", lookup: LicenseLookup = lookup_failing) -> plan.Evaluation:
    return plan.evaluate(project, plan.platform_list(platform), lookup)


def test_a_complete_project_is_ready_on_every_platform(tmp_path: Path) -> None:
    ev = evaluate(make_project(tmp_path))
    assert ev.all_problems() == ()
    assert [p.platform for p in ev.plans] == ["makerworld", "printables", "thingiverse"]
    for item in ev.plans:
        assert [f.name for f in item.files] == [
            "wall_plug.3mf",
            "wall_plug.scad",
            "wall_plug.stl",
        ]
        assert [i.name for i in item.images] == ["iso.png"]


def test_platform_names_are_validated_and_all_expands() -> None:
    assert plan.platform_list("all") == ["makerworld", "printables", "thingiverse"]
    assert plan.platform_list("thingiverse") == ["thingiverse"]
    with pytest.raises(InvalidArgument):
        plan.platform_list("cults")


@pytest.mark.parametrize(
    "has_3mf, has_scad, role", [(False, True, ".3mf"), (True, False, ".scad")]
)
def test_a_project_without_3mf_or_scad_is_refused(
    tmp_path: Path, has_3mf: bool, has_scad: bool, role: str
) -> None:
    ev = evaluate(make_project(tmp_path, mf3=has_3mf, scad=has_scad))
    assert len(ev.problems) == 1 and f"no {role} file" in ev.problems[0]
    assert ev.all_problems()


def test_browser_platforms_need_an_image_and_thingiverse_needs_a_category(
    tmp_path: Path,
) -> None:
    ev = evaluate(
        make_project(tmp_path, YAML.replace("category: Household\n", ""), images=False)
    )
    problems = {p.platform: p.problems for p in ev.plans}
    assert (
        "cover image" in problems["makerworld"][0]
        and "cover image" in problems["printables"][0]
    )
    assert "category" in problems["thingiverse"][0]


def test_a_licence_a_platform_does_not_offer_refuses_that_platform_only(
    tmp_path: Path,
) -> None:
    ev = evaluate(make_project(tmp_path, YAML.replace("cc-by-nc", "sdfl")))
    problems = {p.platform: p.problems for p in ev.plans}
    assert (
        problems["makerworld"] == ()
        and problems["printables"]
        and problems["thingiverse"]
    )


def test_platform_field_mapping_uses_each_sites_own_names(tmp_path: Path) -> None:
    ev = evaluate(make_project(tmp_path, YAML.replace("cc-by-nc", "cc-by-nc-sa")))
    by_name = {p.platform: p for p in ev.plans}
    assert by_name["thingiverse"].fields["license"] == "cc-nc-sa"
    assert by_name["makerworld"].fields["license"] == "BY-NC-SA"
    assert by_name["printables"].fields["license"] == "CC BY-NC-SA"
    assert (
        by_name["thingiverse"].fields["name"] == "Wall plug"
        and by_name["thingiverse"].fields["is_wip"] is False
    )
    assert by_name["printables"].fields["summary"] == "A spiral plug."


def test_a_remix_credits_the_original_and_thingiverse_links_it_by_id(
    tmp_path: Path,
) -> None:
    remix = (
        YAML
        + "remix_of: https://www.thingiverse.com/thing:4000001\nremix_license: CC BY\n"
    )
    ev = evaluate(make_project(tmp_path, remix), lookup=lookup_failing)
    assert ev.all_problems() == ()
    thing = next(p for p in ev.plans if p.platform == "thingiverse").fields
    assert thing["is_remix"] is True and thing["ancestors"] == [4000001]
    assert "Remix of https://www.thingiverse.com/thing:4000001" in thing["description"]
    other = evaluate(
        make_project(
            tmp_path / "b", YAML + f"remix_of: {SDFL_URL}\nremix_license: CC BY\n"
        )
    )
    assert (
        "ancestors"
        not in next(p for p in other.plans if p.platform == "thingiverse").fields
    )


def test_a_refused_remix_blocks_every_platform(tmp_path: Path) -> None:
    ev = evaluate(
        make_project(tmp_path, YAML + f"remix_of: {SDFL_URL}\n"),
        lookup=lookup_returning("SDFL"),
    )
    assert len(ev.problems) == 1 and "forbids derivative works" in ev.problems[0]
    assert len(ev.all_problems()) == 1


def test_the_summary_is_cut_from_the_description_when_not_given(tmp_path: Path) -> None:
    long = "word " * 80
    ev = evaluate(
        make_project(tmp_path, YAML.replace("A spiral plug.", long)), "printables"
    )
    summary = str(ev.plans[0].fields["summary"])
    assert len(summary) <= plan.SUMMARY_LIMIT and summary.endswith("…")


def test_dry_run_text_lists_files_fields_steps_and_the_refusal(tmp_path: Path) -> None:
    ev = evaluate(
        make_project(tmp_path, YAML + f"remix_of: {SDFL_URL}\n"),
        lookup=lookup_returning("SDFL"),
    )
    text = plan.render_text(ev, dry_run=True)
    assert "DRY RUN, nothing is sent" in text
    for line in (
        "3mf   print/wall_plug.3mf",
        "scad  wall_plug.scad",
        "stl   wall_plug.stl",
        "images (1)",
        "== makerworld [REFUSED]",
        "== printables [REFUSED]",
        "== thingiverse [REFUSED]",
        "/files/0/uploadFile  (multipart: wall_plug.3mf)",
        "agent-browser open https://makerworld.com/en",
        "STOP before the final Publish button",
        "=> REFUSED, see below",
        "REFUSED — 1 problem(s)",
    ):
        assert line in text, line
    assert "original.scad" not in text and "gcode" not in text


def test_dry_run_text_prints_a_thingiverse_remix_link(tmp_path: Path) -> None:
    remix = YAML + "remix_of: https://www.thingiverse.com/thing:4000001\nremix_license: CC BY\n"
    text = plan.render_text(evaluate(make_project(tmp_path, remix), "thingiverse"), dry_run=True)
    assert "is_remix: True" in text and "ancestors: 4000001" in text


def test_dry_run_text_of_a_clean_project_says_ok(tmp_path: Path) -> None:
    text = plan.render_text(evaluate(make_project(tmp_path)), dry_run=True)
    assert (
        text.endswith("OK — rerun with --yes to publish.")
        and "[ready]" in text
        and "REFUSED" not in text
    )


def test_dry_run_json_carries_the_same_facts(tmp_path: Path) -> None:
    ev = evaluate(
        make_project(tmp_path, YAML + f"remix_of: {SDFL_URL}\n"),
        lookup=lookup_returning("SDFL"),
    )
    data = json.loads(json.dumps(plan.to_json(ev, dry_run=True)))
    assert data["refused"] is True and data["dry_run"] is True
    assert [f["path"] for f in data["files"]] == [
        "print/wall_plug.3mf",
        "wall_plug.scad",
        "wall_plug.stl",
    ]
    assert data["remix"]["source_licenses"] == ["sdfl"]
    assert set(data["platforms"]) == {"makerworld", "printables", "thingiverse"}


# -- Thingiverse executor against a loopback fake --------------------------------------


def test_thingiverse_publish_runs_create_upload_finalize_publish_in_order(
    tmp_path: Path,
) -> None:
    ev = evaluate(make_project(tmp_path), "thingiverse")
    fake = FakeThingiverse()
    try:
        url = execute.publish_thingiverse(ev.plans[0], token="tok", api=fake.base)
    finally:
        fake.close()
    assert url == "https://www.thingiverse.com/thing:777"
    paths = [c[1] for c in fake.calls]
    assert paths == [
        "/things",
        *["/files/0/uploadFile"] * 4,
        "/files/0/FinalizeFiles",
        "/things/777/publish",
    ]
    assert all(c[2]["Authorization"] == "Bearer tok" for c in fake.calls)
    created = json.loads(fake.calls[0][3])
    assert (
        created["name"] == "Wall plug"
        and created["license"] == "cc-nc"
        and created["tags"] == ["plug", "wall"]
    )
    assert (
        b'filename="wall_plug.3mf"' in fake.calls[1][3] and b"PK3mf" in fake.calls[1][3]
    )
    assert b'filename="iso.png"' in fake.calls[4][3]
    finalize = json.loads(fake.calls[5][3])
    assert finalize["target_id"] == 777 and finalize["target_type"] == "thing"
    assert [u["rank"] for u in finalize["pending_uploads"]] == [10, 20, 30, 40]


def test_a_file_that_vanishes_after_planning_leaves_a_named_draft(
    tmp_path: Path,
) -> None:
    ev = evaluate(make_project(tmp_path), "thingiverse")
    ev.plans[0].files[0].unlink()
    fake = FakeThingiverse()
    try:
        with pytest.raises(MarketplaceError) as caught:
            execute.publish_thingiverse(ev.plans[0], token="tok", api=fake.base)
    finally:
        fake.close()
    assert "thing 777 stays an unpublished draft" in caught.value.message


def test_thingiverse_refusal_to_publish_leaves_a_named_draft(tmp_path: Path) -> None:
    ev = evaluate(make_project(tmp_path), "thingiverse")
    fake = FakeThingiverse(fail_publish=True)
    try:
        with pytest.raises(MarketplaceError) as caught:
            execute.publish_thingiverse(ev.plans[0], token="tok", api=fake.base)
    finally:
        fake.close()
    assert "thing 777 stays an unpublished draft" in caught.value.message
    assert any("thing:777" in step for step in caught.value.remediation)


# -- browser executor with a recording runner -------------------------------------------


class Recorder:
    def __init__(
        self, *, url: str = "https://makerworld.com/en", fail_on: str = ""
    ) -> None:
        self.calls: list[list[str]] = []
        self.url, self.fail_on = url, fail_on

    def __call__(self, argv: Sequence[str]) -> tuple[int, str]:
        self.calls.append(list(argv))
        if "get" in argv and "url" in argv:
            return 0, self.url
        if "get" in argv and "cdp-url" in argv:
            return 0, "ws://127.0.0.1:9222/devtools/browser/x"
        if self.fail_on and self.fail_on in argv:
            return 1, "no element matches"
        return 0, "shot.png" if "screenshot" in argv else ""


def test_concurrent_browser_publishes_to_one_platform_do_not_interleave(
    tmp_path: Path,
) -> None:
    import threading
    import time

    item = evaluate(make_project(tmp_path), "makerworld").plans[0]
    active = 0
    overlap = False
    guard = threading.Lock()

    def runner(argv: Sequence[str]) -> tuple[int, str]:
        nonlocal active, overlap
        if "open" in argv:
            with guard:
                active += 1
                overlap = overlap or active > 1
            time.sleep(0.05)
        if "screenshot" in argv:
            with guard:
                active -= 1
            return 0, "shot.png"
        if "get" in argv and "url" in argv:
            return 0, "https://makerworld.com/en"
        return 0, ""

    threads = [
        threading.Thread(target=execute.publish_browser, args=(item, runner))
        for _ in range(2)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not overlap


def test_browser_publish_fills_the_form_and_stops_before_the_final_click(
    tmp_path: Path,
) -> None:
    item = evaluate(make_project(tmp_path), "makerworld").plans[0]
    rec = Recorder()
    outcome = execute.publish_browser(item, rec)
    assert outcome.ok and "press Publish yourself" in outcome.message
    assert rec.calls[0][:6] == [
        "agent-browser",
        "--session",
        execute._session_name(item),
        "--headed",
        "--restore",
        "open",
    ]
    flat = [" ".join(c) for c in rec.calls]
    assert any("upload input[type=file]" in c and "wall_plug.scad" in c for c in flat)
    assert any("find label Title fill Wall plug" in c for c in flat)
    assert not any("Publish" in c and "click" in c for c in flat)


def test_browser_publish_types_credentials_from_the_keychain_on_a_login_wall(
    tmp_path: Path,
) -> None:
    item = evaluate(make_project(tmp_path), "makerworld").plans[0]
    rec = Recorder(url="https://makerworld.com/en/login")
    outcome = execute.publish_browser(item, rec)
    assert (
        not outcome.ok
        and "press Sign in" in outcome.message
        and "2FA" in outcome.message
    )
    fills = [c for c in rec.calls if c[:2] == ["keychain", "fill"]]
    assert [c[2] for c in fills] == [
        "3d-cli/makerworld-user",
        "3d-cli/makerworld-password",
    ]
    assert all("--caller" in c and "3d-cli" in c for c in fills)
    assert not any("find" in c for c in rec.calls)  # no form steps on a login page


def test_browser_publish_stops_at_the_step_the_page_does_not_answer(
    tmp_path: Path,
) -> None:
    item = evaluate(make_project(tmp_path), "printables").plans[0]
    outcome = execute.publish_browser(
        item, Recorder(url="https://www.printables.com/", fail_on="Summary")
    )
    assert not outcome.ok
    assert (
        "finish the form by hand" in outcome.message and "summary:" in outcome.message
    )
