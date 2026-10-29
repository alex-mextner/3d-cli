"""Regression tests: the modify-offer hook resolves the same output file as the tool, finds a project by a
relative path, quotes hostile paths in its hint, and a malformed model.json source URL is a usage error."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from commands import make as make_cmd
from commands import mesh as mesh_cmd
from errors import InvalidArgument
from library import db as librarydb
from library import records
from publishing import offer_run
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
    monkeypatch.setenv("THREED_PUBLISH_OFFER", "ask")


def _project(root: Path, name: str) -> Path:
    project = root / name
    project.mkdir(parents=True)
    (project / "3d.yaml").write_text("name: x\n")
    return project


@pytest.mark.parametrize(
    "spelling",
    [
        lambda out: ["-o", out],
        lambda out: [f"-o{out}"],
        lambda out: ["--out", out],
        lambda out: [f"--out={out}"],
    ],
)
def test_offer_target_follows_every_output_spelling_across_projects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spelling: object
) -> None:
    a, b = _project(tmp_path, "A"), _project(tmp_path, "B")
    (a / "in.stl").write_bytes(b"solid")
    out = str(b / "output.stl")
    monkeypatch.chdir(a)
    argv = ["deform", "in.stl", "spec.yaml", *spelling(out)]  # type: ignore[operator]
    assert mesh_cmd._offer_target(argv) == Path(out)


def test_offer_target_default_output_stays_beside_the_mesh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = _project(tmp_path, "A")
    monkeypatch.chdir(a)
    assert mesh_cmd._offer_target(["deform", "in.stl", "spec.yaml"]) == Path(
        "in.deformed.stl"
    )


def test_relative_project_dir_finds_the_design_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path, "custom_dir")
    db = tmp_path / "lib.db"
    conn = librarydb.open_library(db)
    try:
        src, _ = records.ensure_source(
            conn,
            site="printables",
            model_id="1",
            url="https://www.printables.com/model/1-x",
            license="CC BY-ND 4.0",
            author="ann",
        )
        d = records.ensure_design(conn, "other_name", str(project))
        records.relate(
            conn, "remix_of", "design", str(d["uid"]), "model_source", str(src["uid"])
        )
    finally:
        conn.close()
    monkeypatch.chdir(tmp_path)
    _name, _make, _proj, src_url, remixed, _pub = offer_run._from_library(
        Path("./custom_dir"), None, None, None, str(db)
    )
    assert (src_url, remixed) == ("https://www.printables.com/model/1-x", True)


@pytest.mark.parametrize(
    "dirname", ["p; printf REVIEW_INJECTED; #", "with space", "$(touch x)"]
)
def test_modify_hint_quotes_the_project_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], dirname: str
) -> None:
    project = _project(tmp_path, dirname)
    out = project / "m.deformed.stl"
    out.write_bytes(b"solid")
    offer_run.after_modify(out)
    err = capsys.readouterr().err
    import shlex

    line = err.split("`")[1]
    assert shlex.split(line) == [
        "3d",
        "models",
        "offer",
        str(project.resolve()),
        "--trigger",
        "modify",
        "--json",
    ]


def test_json_source_with_a_malformed_url_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "model.json"
    path.write_text(json.dumps({"source": {"url": "https://["}}))
    with pytest.raises(InvalidArgument) as exc:
        load_source(str(path))
    assert exc.value.exit_code == 2
    with pytest.raises(InvalidArgument) as exc2:
        make_cmd.run(["add", "--design", "d", "--source-url", str(path), "--json"])
    assert exc2.value.exit_code == 2
