"""Boundary guards of the publish flow: hosted URLs, malformed source URLs, a make post without a make,
and name clashes of files a non-printable drop already removed."""

from __future__ import annotations

from pathlib import Path

import pytest

from commands import make as make_cmd
from errors import InvalidArgument, UsageError
from library import actions, records
from library import db as librarydb
from publish_fixtures import YAML, make_project, three_d_yaml
from publishing import guard, spec
from publishing.source import load_source


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


def test_drop_nonprintable_clears_clashes_of_the_dropped_files(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    three_d_yaml(project, "  ghost: {file: ghost.scad, role: stand_in}\n")
    (project / "ghost.stl").write_bytes(b"solid g\nendsolid g\n")
    (project / "print" / "ghost.stl").write_bytes(b"solid g\nendsolid g\n")
    files = spec.select_files(project)
    assert files.clashes == ("ghost.stl",)
    _, kept = guard.evaluate_parts(project, spec.load_spec(project), files, drop=True)
    assert all(f.name != "ghost.stl" for f in kept.files)
    assert kept.clashes == ()


@pytest.mark.parametrize(
    "url",
    [
        "https://",
        "https:///missing-host",
        "https://user:pw@example.com/a.stl",
        "https://exa mple.com/a.stl",
    ],
)
def test_hosted_url_without_a_usable_host_is_refused(tmp_path: Path, url: str) -> None:
    project = make_project(tmp_path, YAML + f'hosted:\n  wall_plug.stl: "{url}"\n')
    with pytest.raises(InvalidArgument) as exc:
        spec.load_spec(project)
    assert exc.value.exit_code == 2


def test_hosted_https_url_is_accepted(tmp_path: Path) -> None:
    project = make_project(
        tmp_path, YAML + "hosted:\n  wall_plug.stl: https://cdn.example.com/a.stl\n"
    )
    assert spec.load_spec(project).hosted == (
        ("wall_plug.stl", "https://cdn.example.com/a.stl"),
    )


def test_load_source_refuses_a_malformed_url() -> None:
    with pytest.raises(InvalidArgument) as exc:
        load_source("https://[")
    assert exc.value.exit_code == 2


def test_make_add_refuses_a_malformed_source_url() -> None:
    with pytest.raises(InvalidArgument) as exc:
        make_cmd.run(["add", "--design", "mole", "--source-url", "https://[", "--json"])
    assert exc.value.exit_code == 2


def test_publish_kind_make_without_a_make_is_a_usage_error(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    c = librarydb.open_library(tmp_path / "library.db")
    records.ensure_design(c, "holder", str(project))
    for _ in range(2):
        m, _created = records.add_make(c, "holder", printer="k1se", status="done")
        photo = tmp_path / f"{m['uid']}.jpg"
        photo.write_bytes(b"jpg")
        records.add_photo(c, str(photo), make=str(m["uid"]), kind="table", z_mm=70)
    c.close()
    with pytest.raises(UsageError) as exc:
        actions.publish(
            "holder", kind="make", of="https://www.printables.com/model/1-x"
        )
    assert exc.value.exit_code == 2
