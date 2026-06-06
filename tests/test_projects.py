"""Tests for the project registry core + `3d projects` command (ROADMAP §28/§9).

The registry writes to cli.paths.config_dir()/projects.json — every test redirects
XDG_CONFIG_HOME into tmp_path so it never touches the real ~/.config/3d-cli.
"""
from __future__ import annotations

import json

import pytest

import commands.projects as projects_cmd
import projects_registry as reg
from projects_registry import ProjectRegistryError


@pytest.fixture(autouse=True)
def _sandbox_config(monkeypatch, tmp_path):
    """Redirect config_dir() into the test sandbox so the real user config is untouched."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    return tmp_path


def _make_project_dir(tmp_path, name="widget", *, with_yaml=True):
    d = tmp_path / name
    d.mkdir(parents=True, exist_ok=True)
    if with_yaml:
        (d / "3d.yaml").write_text(f"project:\n  name: {name}\n", encoding="utf-8")
    return d


# ---- core registry: list_projects -------------------------------------------

def test_empty_when_no_registry_file(tmp_path):
    assert reg.list_projects() == []


def test_corrupt_registry_treated_as_empty(tmp_path):
    rp = reg.registry_path()
    rp.parent.mkdir(parents=True, exist_ok=True)
    rp.write_text("{ this is not json", encoding="utf-8")
    assert reg.list_projects() == []


# ---- core registry: add -----------------------------------------------------

def test_add_writes_into_sandbox_not_home(tmp_path):
    d = _make_project_dir(tmp_path)
    reg.add(str(d))
    rp = reg.registry_path()
    # The registry file lands under the sandboxed XDG_CONFIG_HOME, not real $HOME.
    assert str(tmp_path / "cfg") in str(rp)
    assert rp.is_file()
    doc = json.loads(rp.read_text(encoding="utf-8"))
    assert doc["projects"][0]["path"] == str(d.resolve())


def test_add_returns_had_yaml_true(tmp_path):
    d = _make_project_dir(tmp_path, with_yaml=True)
    entry = reg.add(str(d))
    assert entry["had_yaml"] is True
    assert entry["path"] == str(d.resolve())
    assert entry["added"]


def test_add_without_yaml_flags_had_yaml_false(tmp_path):
    d = _make_project_dir(tmp_path, name="bare", with_yaml=False)
    entry = reg.add(str(d))
    assert entry["had_yaml"] is False


def test_add_dedup_on_resolved_path(tmp_path):
    d = _make_project_dir(tmp_path)
    reg.add(str(d))
    reg.add(str(d) + "/")  # different spelling, same resolved dir
    assert len(reg.list_projects()) == 1


def test_add_nonexistent_dir_raises(tmp_path):
    with pytest.raises(ProjectRegistryError):
        reg.add(str(tmp_path / "does-not-exist"))


def test_add_file_not_dir_raises(tmp_path):
    f = tmp_path / "afile.txt"
    f.write_text("x", encoding="utf-8")
    with pytest.raises(ProjectRegistryError):
        reg.add(str(f))


# ---- core registry: name resolution -----------------------------------------

def test_name_read_live_from_yaml(tmp_path):
    d = _make_project_dir(tmp_path, name="myproj")
    reg.add(str(d))
    items = reg.list_projects()
    assert items[0]["name"] == "myproj"


def test_name_falls_back_to_basename_without_yaml(tmp_path):
    d = _make_project_dir(tmp_path, name="bare", with_yaml=False)
    reg.add(str(d))
    items = reg.list_projects()
    assert items[0]["name"] == "bare"


def test_corrupt_yaml_falls_back_to_basename(tmp_path):
    d = _make_project_dir(tmp_path, name="broken", with_yaml=False)
    (d / "3d.yaml").write_text("project:\n  name: [unclosed\n", encoding="utf-8")
    reg.add(str(d))
    items = reg.list_projects()
    assert items[0]["name"] == "broken"


# ---- core registry: remove --------------------------------------------------

def test_remove_unregisters(tmp_path):
    d = _make_project_dir(tmp_path)
    reg.add(str(d))
    reg.remove(str(d))
    assert reg.list_projects() == []


def test_remove_matches_on_resolved_path(tmp_path):
    d = _make_project_dir(tmp_path)
    reg.add(str(d))
    reg.remove(str(d) + "/")  # different spelling resolves to the same entry
    assert reg.list_projects() == []


def test_remove_unregistered_raises(tmp_path):
    with pytest.raises(ProjectRegistryError):
        reg.remove(str(tmp_path / "never-added"))


def test_is_registered(tmp_path):
    d = _make_project_dir(tmp_path)
    assert not reg.is_registered(str(d))
    reg.add(str(d))
    assert reg.is_registered(str(d))


# ---- command frontend -------------------------------------------------------

def test_cmd_help_returns_zero(capsys):
    assert projects_cmd.run(["--help"]) == 0
    out = capsys.readouterr().out
    assert "projects" in out
    # Help must carry why + example per §11.
    assert "Example" in out or "example" in out


def test_cmd_no_args_shows_usage_nonzero(capsys):
    assert projects_cmd.run([]) == 1


def test_cmd_unknown_subcommand_raises():
    with pytest.raises(ProjectRegistryError):
        projects_cmd.run(["frobnicate"])


def test_cmd_add_then_list(tmp_path, capsys):
    d = _make_project_dir(tmp_path, name="gizmo")
    assert projects_cmd.run(["add", str(d)]) == 0
    capsys.readouterr()  # drain add output
    assert projects_cmd.run(["list"]) == 0
    out = capsys.readouterr().out
    assert "gizmo" in out
    assert str(d.resolve()) in out
    assert "ADDED" in out
    # The stored ISO timestamp's date appears (the year of the run).
    import datetime
    assert str(datetime.datetime.now(datetime.timezone.utc).year) in out


def test_cmd_add_warns_when_no_yaml(tmp_path, capsys):
    d = _make_project_dir(tmp_path, name="noyaml", with_yaml=False)
    assert projects_cmd.run(["add", str(d)]) == 0
    err = capsys.readouterr().err
    assert "3d.yaml" in err


def test_cmd_list_empty(capsys):
    assert projects_cmd.run(["list"]) == 0
    out = capsys.readouterr().out
    assert "No projects" in out or "no projects" in out


def test_cmd_add_missing_path_arg_raises():
    with pytest.raises(ProjectRegistryError):
        projects_cmd.run(["add"])


def test_cmd_remove(tmp_path, capsys):
    d = _make_project_dir(tmp_path)
    projects_cmd.run(["add", str(d)])
    capsys.readouterr()
    assert projects_cmd.run(["remove", str(d)]) == 0
    assert reg.list_projects() == []
