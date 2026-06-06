"""Tests for `3d init` — the project scaffolder (lib/commands/init.py, ROADMAP §28).

Drives the non-interactive (--no-input) mode in a tmp dir and asserts:
  - the written 3d.yaml round-trips through project.load_project (check_files=True);
  - the directory skeleton + .gitignore are created;
  - re-running is idempotent (tops up, never clobbers user edits);
  - --reference copies the image into references/ and records project.reference;
  - flags override defaults.

projects_registry is imported guarded inside the command, so these tests pass whether
or not that module exists yet (it is authored by a parallel task).
"""
from __future__ import annotations

import os

import pytest

import commands.init as init
import project


@pytest.fixture(autouse=True)
def _isolate_registry(tmp_path, monkeypatch):
    """Point the projects registry (which honors XDG_CONFIG_HOME via cli.paths) at a tmp dir so
    scaffolding in a test never writes the developer's real ~/.config/3d-cli/projects.json."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "_xdg"))


def _run(args, cwd):
    """Run init with cwd temporarily switched to `cwd` (init scaffolds into cwd or path)."""
    old = os.getcwd()
    os.chdir(cwd)
    try:
        return init.run(args)
    finally:
        os.chdir(old)


def test_help_returns_zero(capsys):
    assert init.run(["--help"]) == 0
    out = capsys.readouterr().out
    assert "3d init" in out
    assert "Example" in out  # §11: every command carries a concrete example


def test_no_input_scaffolds_loadable_project(tmp_path):
    rc = _run(["--no-input", "--name", "widget"], tmp_path)
    assert rc == 0
    assert (tmp_path / "3d.yaml").is_file()
    # The headless core must accept it with file-checking ON (no dangling part files).
    p = project.load_project(tmp_path)
    assert p.name == "widget"
    assert p.units == "mm"
    assert p.parts == {}


def test_scaffold_includes_empty_project_config_sections(tmp_path):
    _run(["--no-input", "--name", "widget", "--no-git"], tmp_path)
    p = project.load_project(tmp_path)
    assert p.anchors == {}
    assert p.sections == {}
    assert p.loads == {}
    assert p.gates == []
    assert set(p.raw) >= {"project", "parts", "anchors", "sections", "loads", "gates"}


def test_directory_skeleton_created(tmp_path):
    _run(["--no-input"], tmp_path)
    for d in ("parts", "references", "previews"):
        assert (tmp_path / d).is_dir(), f"missing skeleton dir {d}"


def test_gitignore_written(tmp_path):
    _run(["--no-input"], tmp_path)
    gi = tmp_path / ".gitignore"
    assert gi.is_file()
    body = gi.read_text(encoding="utf-8")
    assert "previews/" in body


def test_flags_override_defaults(tmp_path):
    _run(
        ["--no-input", "--name", "gear", "--printer", "X1C", "--material", "PETG", "--units", "cm"],
        tmp_path,
    )
    p = project.load_project(tmp_path)
    assert p.name == "gear"
    assert p.printer == "X1C"
    assert p.material == "PETG"
    assert p.units == "cm"


def test_bed_flag_parsed(tmp_path):
    _run(["--no-input", "--bed", "256,256,256"], tmp_path)
    p = project.load_project(tmp_path)
    assert p.bed == [256.0, 256.0, 256.0]


def test_positional_path_scaffolds_subdir(tmp_path):
    sub = tmp_path / "newproj"
    rc = _run(["newproj", "--no-input", "--name", "sub"], tmp_path)
    assert rc == 0
    assert (sub / "3d.yaml").is_file()
    p = project.load_project(sub)
    assert p.name == "sub"


def test_idempotent_does_not_clobber_existing_yaml(tmp_path):
    _run(["--no-input", "--name", "first"], tmp_path)
    # Simulate the user editing the project.
    yaml_path = tmp_path / "3d.yaml"
    edited = yaml_path.read_text(encoding="utf-8") + "\n# user note\n"
    yaml_path.write_text(edited, encoding="utf-8")
    # Re-run with a DIFFERENT name; the existing file must be left intact.
    rc = _run(["--no-input", "--name", "second"], tmp_path)
    assert rc == 0
    after = yaml_path.read_text(encoding="utf-8")
    assert "# user note" in after
    assert project.load_project(tmp_path).name == "first"


def test_idempotent_does_not_clobber_existing_gitignore(tmp_path):
    gi = tmp_path / ".gitignore"
    gi.write_text("my-custom-ignore\n", encoding="utf-8")
    _run(["--no-input", "--no-git"], tmp_path)
    assert gi.read_text(encoding="utf-8") == "my-custom-ignore\n"


def test_idempotent_tops_up_missing_pieces(tmp_path):
    """Re-running recreates a deleted skeleton dir + .gitignore WITHOUT touching an edited
    3d.yaml — the 'tops up missing pieces, never clobbers' requirement, both halves."""
    import shutil

    _run(["--no-input", "--name", "first", "--no-git"], tmp_path)
    yaml_path = tmp_path / "3d.yaml"
    yaml_path.write_text(yaml_path.read_text(encoding="utf-8") + "\n# user note\n", encoding="utf-8")
    shutil.rmtree(tmp_path / "parts")
    (tmp_path / ".gitignore").unlink()
    rc = _run(["--no-input", "--name", "second", "--no-git"], tmp_path)
    assert rc == 0
    assert (tmp_path / "parts").is_dir()          # topped up
    assert (tmp_path / ".gitignore").is_file()     # topped up
    assert "# user note" in yaml_path.read_text(encoding="utf-8")  # not clobbered
    assert project.load_project(tmp_path).name == "first"


def test_bare_init_non_tty_uses_defaults_without_hanging(tmp_path):
    """Without --no-input in a non-TTY context (pytest stdin), init must NOT call input()
    (which would hang the swarm) — it must fall through to defaults. Pins the no-hang guard."""
    sub = tmp_path / "agent-proj"
    sub.mkdir()
    rc = _run(["--no-git"], sub)  # no --no-input, no flags
    assert rc == 0
    p = project.load_project(sub)
    assert p.name == "agent-proj"
    assert p.units == "mm"


def test_reference_copied_and_recorded(tmp_path):
    src = tmp_path / "pantheon.jpg"
    src.write_bytes(b"\xff\xd8\xff\xe0fake-jpeg")
    _run(["--no-input", "--name", "pantheon", "--reference", str(src)], tmp_path)
    copied = tmp_path / "references" / "pantheon.jpg"
    assert copied.is_file()
    assert copied.read_bytes() == b"\xff\xd8\xff\xe0fake-jpeg"
    p = project.load_project(tmp_path)
    assert p.raw["project"]["reference"] == "references/pantheon.jpg"


def test_reference_missing_raises(tmp_path):
    from errors import InputNotFound

    with pytest.raises(InputNotFound):
        _run(["--no-input", "--reference", str(tmp_path / "nope.jpg")], tmp_path)


def test_invalid_units_raises(tmp_path):
    from errors import InvalidArgument

    with pytest.raises(InvalidArgument):
        _run(["--no-input", "--units", "furlongs"], tmp_path)


def test_bad_bed_raises(tmp_path):
    from errors import InvalidArgument

    with pytest.raises(InvalidArgument):
        _run(["--no-input", "--bed", "256,256"], tmp_path)


def test_name_defaults_to_dir(tmp_path):
    sub = tmp_path / "my-thing"
    sub.mkdir()
    _run(["--no-input"], sub)
    p = project.load_project(sub)
    assert p.name == "my-thing"


def test_no_git_skips_git_init(tmp_path):
    _run(["--no-input", "--no-git"], tmp_path)
    assert not (tmp_path / ".git").exists()


# --------------------------------------------------------------------------------------------
# Agent environment (ROADMAP §28): .mcp.json, skills, AGENTS.md + CLAUDE.md symlink, pre-commit.
# --------------------------------------------------------------------------------------------
def test_agent_env_installed_by_default(tmp_path):
    """`3d init --no-input` installs the full agent environment alongside the skeleton."""
    _run(["--no-input", "--name", "widget", "--no-git"], tmp_path)
    assert (tmp_path / ".mcp.json").is_file()
    assert (tmp_path / ".claude" / "skills" / "openscad" / "SKILL.md").is_file()
    assert (tmp_path / ".claude" / "skills" / "fdm-printability" / "SKILL.md").is_file()
    assert (tmp_path / "AGENTS.md").is_file()
    hook = tmp_path / "hooks" / "pre-commit"
    assert hook.is_file()
    assert os.access(hook, os.X_OK), "pre-commit hook must be executable"


def test_claude_md_is_symlink_to_agents(tmp_path):
    _run(["--no-input", "--no-git"], tmp_path)
    claude = tmp_path / "CLAUDE.md"
    assert claude.is_symlink(), "CLAUDE.md must be a symlink"
    assert os.readlink(claude) == "AGENTS.md", "CLAUDE.md must point to AGENTS.md (relative)"
    # It must resolve to the same content as AGENTS.md.
    assert claude.read_text(encoding="utf-8") == (tmp_path / "AGENTS.md").read_text(encoding="utf-8")


def test_agents_md_substitutes_project_name_and_has_lessons(tmp_path):
    _run(["--no-input", "--name", "pantheon", "--printer", "X1C", "--material", "ABS", "--no-git"],
         tmp_path)
    body = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    assert "pantheon" in body                 # {{PROJECT_NAME}} substituted
    assert "{{PROJECT_NAME}}" not in body     # no leftover placeholders
    assert "{{PRINTER}}" not in body
    assert "{{MATERIAL}}" not in body
    assert "X1C" in body and "ABS" in body
    assert "MANDATORY" in body                # the hard-won lessons block is present


def test_agent_env_reinstall_is_idempotent(tmp_path):
    """Re-running tops up nothing and raises nothing; existing agent files are unchanged."""
    _run(["--no-input", "--name", "first", "--no-git"], tmp_path)
    targets = [
        tmp_path / ".mcp.json",
        tmp_path / ".claude" / "skills" / "openscad" / "SKILL.md",
        tmp_path / ".claude" / "skills" / "fdm-printability" / "SKILL.md",
        tmp_path / "AGENTS.md",
        tmp_path / "hooks" / "pre-commit",
    ]
    before = {t: t.read_bytes() for t in targets}
    claude_before = os.readlink(tmp_path / "CLAUDE.md")
    rc = _run(["--no-input", "--name", "second", "--no-git"], tmp_path)
    assert rc == 0
    for t, content in before.items():
        assert t.read_bytes() == content, f"{t} was clobbered on re-run"
    assert os.readlink(tmp_path / "CLAUDE.md") == claude_before


def test_agent_env_tops_up_deleted_symlink(tmp_path):
    """A deleted CLAUDE.md symlink is recreated on re-run even though AGENTS.md still exists."""
    _run(["--no-input", "--no-git"], tmp_path)
    (tmp_path / "CLAUDE.md").unlink()
    assert (tmp_path / "AGENTS.md").is_file()
    _run(["--no-input", "--no-git"], tmp_path)
    claude = tmp_path / "CLAUDE.md"
    assert claude.is_symlink()
    assert os.readlink(claude) == "AGENTS.md"


def test_opt_out_flags_skip_each_piece(tmp_path):
    _run(["--no-input", "--no-git", "--no-mcp", "--no-skills", "--no-agents", "--no-hooks"],
         tmp_path)
    assert not (tmp_path / ".mcp.json").exists()
    assert not (tmp_path / ".claude").exists()
    assert not (tmp_path / "AGENTS.md").exists()
    assert not (tmp_path / "CLAUDE.md").exists()
    assert not (tmp_path / "hooks" / "pre-commit").exists()
    # The base skeleton is still scaffolded.
    assert (tmp_path / "3d.yaml").is_file()
