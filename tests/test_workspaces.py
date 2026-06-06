"""Tests for web workspace metadata used by the future dashboard project picker."""
from __future__ import annotations

import json

import pytest

import commands.workspaces as workspaces_cmd
import workspaces
from errors import UsageError


@pytest.fixture(autouse=True)
def _sandbox_config(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    return tmp_path


def _make_project(tmp_path, name: str = "widget"):
    d = tmp_path / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "3d.yaml").write_text(f"project:\n  name: {name}\n", encoding="utf-8")
    return d


def test_empty_when_no_workspaces_file() -> None:
    assert workspaces.list_workspaces() == []


def test_corrupt_workspaces_file_treated_as_empty() -> None:
    p = workspaces.workspaces_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{ not json", encoding="utf-8")
    assert workspaces.list_workspaces() == []


def test_create_writes_workspace_metadata_in_config_dir(tmp_path) -> None:
    project = _make_project(tmp_path, "gizmo")
    entry = workspaces.create_workspace("shop", root=tmp_path, projects=[project])

    assert entry["name"] == "shop"
    assert entry["root"] == str(tmp_path.resolve())
    assert entry["project_count"] == 1
    assert entry["projects"][0]["name"] == "gizmo"
    assert entry["projects"][0]["path"] == str(project.resolve())
    assert str(tmp_path / "cfg") in str(workspaces.workspaces_path())

    doc = json.loads(workspaces.workspaces_path().read_text(encoding="utf-8"))
    assert doc["workspaces"][0]["projects"] == [str(project.resolve())]


def test_create_rejects_duplicate_names(tmp_path) -> None:
    workspaces.create_workspace("shop", root=tmp_path)
    with pytest.raises(UsageError):
        workspaces.create_workspace("shop", root=tmp_path)


def test_create_rejects_missing_root(tmp_path) -> None:
    with pytest.raises(UsageError):
        workspaces.create_workspace("shop", root=tmp_path / "missing")


def test_show_workspace_raises_for_unknown_name() -> None:
    with pytest.raises(UsageError):
        workspaces.get_workspace("missing")


def test_project_name_falls_back_to_basename_without_yaml(tmp_path) -> None:
    project = tmp_path / "bare"
    project.mkdir()
    entry = workspaces.create_workspace("shop", root=tmp_path, projects=[project])
    assert entry["projects"][0]["name"] == "bare"
    assert entry["projects"][0]["has_yaml"] is False


def test_cmd_help_and_no_args(capsys) -> None:
    assert workspaces_cmd.run(["--help"]) == 0
    assert "workspaces" in capsys.readouterr().out
    assert workspaces_cmd.run([]) == 1


def test_cmd_create_show_and_list_json(tmp_path, capsys) -> None:
    project = _make_project(tmp_path, "gizmo")

    assert workspaces_cmd.run(["create", "shop", "--root", str(tmp_path), "--project", str(project)]) == 0
    out = capsys.readouterr().out
    assert "Created workspace: shop" in out

    assert workspaces_cmd.run(["show", "shop", "--json"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["workspace"]["name"] == "shop"
    assert shown["workspace"]["projects"][0]["name"] == "gizmo"

    assert workspaces_cmd.run(["list", "--json"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed["workspaces"][0]["name"] == "shop"
    assert listed["workspaces"][0]["project_count"] == 1


def test_cmd_create_accepts_multiple_projects_after_one_flag(tmp_path, capsys) -> None:
    left = _make_project(tmp_path, "left")
    right = _make_project(tmp_path, "right")

    assert (
        workspaces_cmd.run(
            ["create", "shop", "--root", str(tmp_path), "--project", str(left), str(right), "--json"]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)

    assert payload["workspace"]["project_count"] == 2
    assert [p["name"] for p in payload["workspace"]["projects"]] == ["left", "right"]


def test_cmd_unknown_subcommand_raises() -> None:
    with pytest.raises(UsageError):
        workspaces_cmd.run(["rename", "a", "b"])


def test_cmd_create_rejects_missing_flag_values(tmp_path) -> None:
    with pytest.raises(UsageError):
        workspaces_cmd.run(["create", "shop", "--root"])
    with pytest.raises(UsageError):
        workspaces_cmd.run(["create", "shop", "--root", str(tmp_path), "--project"])


def test_cmd_show_rejects_extra_args(tmp_path) -> None:
    workspaces.create_workspace("shop", root=tmp_path)
    with pytest.raises(UsageError):
        workspaces_cmd.run(["show", "shop", "extra"])
