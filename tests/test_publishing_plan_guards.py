"""Plan guards of `3d models publish`: a permission_ref stays with the source it names, a make post does not
depend on the listing images, library photos must be pictures, and Cults image names stay unique."""

from __future__ import annotations

from pathlib import Path

import pytest

from errors import InputNotFound
from publish_fixtures import YAML, make_project
from publishing import plan
from publishing.source import SourceRef, lookup_for

URL_A = "https://www.printables.com/model/1"
URL_B = "https://www.printables.com/model/2"


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


def source(url: str, license_text: str = "CC BY-ND 4.0") -> SourceRef:
    return SourceRef(
        url, "printables", url.rsplit("/", 1)[1], license_text, "ann", "m", "url"
    )


def run(
    project: Path,
    platforms: list[str],
    opts: plan.Options,
    src: SourceRef | None = None,
) -> plan.Evaluation:
    def no_network(url: str) -> str:
        raise AssertionError(f"the marketplace must not be asked for {url}")

    return plan.evaluate(
        project, platforms, lookup_for(src) if src else no_network, opts
    )


def test_a_permission_for_one_source_does_not_cover_a_replacement_of(
    tmp_path: Path,
) -> None:
    project = make_project(
        tmp_path, YAML + f"remix_of: {URL_A}\npermission_ref: mail from A\n"
    )
    b = source(URL_B)
    ev = run(project, ["thingiverse"], plan.Options(kind="remix", of=b), b)
    assert any("forbids derivative works" in p for p in ev.all_problems())
    assert "mail from A" not in ev.plans[0].fields["description"]


def test_a_permission_still_covers_the_source_it_names(tmp_path: Path) -> None:
    project = make_project(
        tmp_path, YAML + f"remix_of: {URL_A}\npermission_ref: mail from A\n"
    )
    a = source(URL_A)
    ev = run(project, ["thingiverse"], plan.Options(kind="remix", of=a), a)
    assert ev.all_problems() == ()


def test_the_declared_licence_is_dropped_with_the_declared_source(
    tmp_path: Path,
) -> None:
    project = make_project(
        tmp_path,
        YAML
        + f"remix_of: {URL_A}\npermission_ref: mail from A\nremix_license: CC BY-ND 4.0\n",
    )
    b = source(URL_B, "CC BY 4.0")
    ev = run(project, ["thingiverse"], plan.Options(kind="remix", of=b), b)
    assert ev.all_problems() == ()


def test_another_spelling_of_the_same_model_keeps_its_permission(
    tmp_path: Path,
) -> None:
    declared = "https://www.printables.com/model/311253-spoon-drawer-pull"
    project = make_project(
        tmp_path, YAML + f"remix_of: {declared}\npermission_ref: mail from A\n"
    )
    same = source("https://www.printables.com/model/311253")
    ev = run(project, ["thingiverse"], plan.Options(kind="remix", of=same), same)
    assert ev.all_problems() == ()


def test_an_http_declared_source_is_the_same_model_as_its_https_of(
    tmp_path: Path,
) -> None:
    project = make_project(
        tmp_path,
        YAML
        + "remix_of: http://www.printables.com/model/1\n"
        + "permission_ref: mail from A\nremix_license: CC BY-ND 4.0\n",
    )
    a = source(URL_A)
    ev = run(project, ["thingiverse"], plan.Options(kind="remix", of=a), a)
    assert ev.all_problems() == ()
    assert ev.spec.permission_ref == "mail from A"
    assert ev.spec.remix_license == "CC BY-ND 4.0"


def test_an_http_declared_source_of_another_model_still_loses_its_permission(
    tmp_path: Path,
) -> None:
    project = make_project(
        tmp_path,
        YAML
        + "remix_of: http://www.printables.com/model/1\npermission_ref: mail from A\n",
    )
    b = source(URL_B)
    ev = run(project, ["thingiverse"], plan.Options(kind="remix", of=b), b)
    assert ev.spec.permission_ref == ""
    assert any("forbids derivative works" in p for p in ev.all_problems())


def test_a_make_does_not_depend_on_the_listing_images(tmp_path: Path) -> None:
    project = make_project(tmp_path, YAML + "images: [previews/gone.png]\n")
    photo = tmp_path / "table.jpg"
    photo.write_bytes(b"jpg")
    opts = plan.Options(
        kind="make",
        of=source(URL_A),
        targets=(("printables", URL_A),),
        photos=(plan.Photo(photo, "table"),),
    )
    assert run(project, ["printables"], opts).all_problems() == ()
    with pytest.raises(InputNotFound):
        run(project, ["printables"], plan.Options(kind="original"))


def test_a_library_photo_that_is_not_a_picture_is_never_listed(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    scad = project / "wall_plug.scad"
    ev = run(
        project,
        ["thingiverse"],
        plan.Options(photos=(plan.Photo(scad, "render"),)),
    )
    assert scad not in ev.plans[0].images
    assert any("not a" in w and "wall_plug.scad" in w for w in ev.warnings)


def hosted_yaml(*names: str) -> str:
    urls = "".join(f"  {n}: https://cdn.example.com/{n}\n" for n in names)
    return YAML.replace("cc-by-nc", "cc-by").replace("Household", "Home") + (
        "hosted:\n" + urls
    )


def test_cults_images_sharing_a_basename_are_a_plan_problem(tmp_path: Path) -> None:
    project = make_project(
        tmp_path,
        hosted_yaml(
            "wall_plug.stl", "wall_plug.3mf", "wall_plug.scad", "table.jpg", "iso.png"
        ),
    )
    photos = []
    for n in (1, 2):
        d = tmp_path / f"make-{n}"
        d.mkdir()
        (d / "table.jpg").write_bytes(b"jpg")
        photos.append(plan.Photo(d / "table.jpg", "table"))
    ev = run(project, ["cults"], plan.Options(photos=tuple(photos)))
    assert any(
        "two Cults images are named table.jpg" in p for p in ev.plans[0].problems
    )
