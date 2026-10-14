"""Readable e2e workflows for `3d profiles` (issue #180): a fake Snapmaker Orca data dir under a
temp XDG_CONFIG_HOME, a temp repo `profiles/` dir, and a fake `ps` that says whether the slicer
runs."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from profiles_fixtures import K1_MACHINE, U1_PROCESS, make_snapmaker, slicer_json

from .workflow_helper import REPO_ROOT, run_cli

U1 = str(U1_PROCESS["name"])
U1_REL = f"process/{U1}.json"


def _setup(tmp_path: Path) -> tuple[Path, Path, dict[str, str]]:
    user = make_snapmaker(tmp_path / "xdg-config")
    repo = tmp_path / "repo" / "profiles"
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    ps = fake_bin / "ps"
    ps.write_text(f"#!/bin/sh\ncat '{tmp_path / 'ps.out'}'\n", encoding="utf-8")
    ps.chmod(0o755)
    (tmp_path / "ps.out").write_text("bash\n", encoding="utf-8")
    env = {
        "THREED_PROFILES_DIR": str(repo),
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
    }
    return user, repo / "snapmaker-orca", env


def test_import_edit_in_repo_and_push_into_the_slicer(tmp_path: Path) -> None:
    """The user imports the Snapmaker Orca presets into the repo, raises the layer height of a
    process preset in the repo, is stopped while the slicer runs, closes it and pushes; the
    slicer's old file lands in the backup dir and the K1's printer address stays in the slicer."""
    user, repo, env = _setup(tmp_path)

    status = run_cli(tmp_path, "profiles", "status", env_extra=env)
    assert status.returncode == 0, status.stderr
    assert f"pull      {U1_REL}  (new in slicer)" in status.stdout
    assert "orca: OrcaSlicer not found" in status.stdout
    assert status.stdout.rstrip().endswith("3 to sync, 0 conflict(s)")

    no_tty = run_cli(tmp_path, "profiles", "sync", env_extra=env)
    assert no_tty.returncode == 2 and "--yes" in no_tty.stderr
    dry = run_cli(tmp_path, "profiles", "sync", "--dry-run", env_extra=env)
    assert dry.returncode == 0 and "nothing written" in dry.stdout
    assert not repo.exists()

    pulled = run_cli(tmp_path, "profiles", "sync", "--yes", env_extra=env)
    assert pulled.returncode == 0, pulled.stderr
    assert (repo / U1_REL).read_text(encoding="utf-8") == slicer_json(U1_PROCESS)
    machine = json.loads(
        (repo / "machine" / f"{K1_MACHINE['name']}.json").read_text(encoding="utf-8")
    )
    assert "print_host" not in machine
    assert (
        run_cli(tmp_path, "profiles", "status", env_extra=env)
        .stdout.rstrip()
        .endswith("clean")
    )

    (repo / U1_REL).write_text(
        slicer_json({**U1_PROCESS, "layer_height": "0.36"}), encoding="utf-8"
    )
    (tmp_path / "ps.out").write_text("bash\nsnapmaker-orca\n", encoding="utf-8")
    refused = run_cli(tmp_path, "profiles", "sync", "--yes", env_extra=env)
    assert refused.returncode == 1
    assert "Snapmaker Orca is running" in refused.stderr and "--force" in refused.stderr
    assert (
        json.loads((user / U1_REL).read_text(encoding="utf-8"))["layer_height"]
        == "0.32"
    )

    (tmp_path / "ps.out").write_text("bash\n", encoding="utf-8")
    pushed = run_cli(tmp_path, "profiles", "sync", "--yes", env_extra=env)
    assert pushed.returncode == 0, pushed.stderr
    assert f"push snapmaker-orca {U1_REL}: done" in pushed.stdout
    assert (
        json.loads((user / U1_REL).read_text(encoding="utf-8"))["layer_height"]
        == "0.36"
    )
    backup = re.search(r"^backup: (.+)$", pushed.stdout, re.M)
    assert backup is not None
    saved = Path(backup.group(1)) / "snapmaker-orca" / U1_REL
    assert json.loads(saved.read_text(encoding="utf-8"))["layer_height"] == "0.32"
    assert Path(backup.group(1)).is_relative_to(
        tmp_path / "xdg-cache" / "3d-cli" / "profiles-backup"
    )
    k1 = json.loads(
        (user / "machine" / f"{K1_MACHINE['name']}.json").read_text(encoding="utf-8")
    )
    assert k1["print_host"] == "192.168.0.27"
    assert (
        run_cli(tmp_path, "profiles", "status", env_extra=env)
        .stdout.rstrip()
        .endswith("clean")
    )


def test_conflict_is_shown_as_a_diff_and_left_alone(tmp_path: Path) -> None:
    """The same preset is edited in the repo and in the slicer: sync shows the diff, writes
    neither side and exits 1; once the user makes both sides equal, status is clean."""
    user, repo, env = _setup(tmp_path)
    assert run_cli(tmp_path, "profiles", "sync", "--yes", env_extra=env).returncode == 0
    (repo / U1_REL).write_text(
        slicer_json({**U1_PROCESS, "layer_height": "0.30"}), encoding="utf-8"
    )
    (user / U1_REL).write_text(
        slicer_json({**U1_PROCESS, "layer_height": "0.24"}), encoding="utf-8"
    )

    synced = run_cli(tmp_path, "profiles", "sync", "--yes", env_extra=env)
    assert synced.returncode == 1
    assert f"conflict  {U1_REL}  (changed in both)" in synced.stdout
    assert '-    "layer_height": "0.30",' in synced.stdout
    assert '+    "layer_height": "0.24",' in synced.stdout
    assert "1 conflict(s) left as they are" in synced.stderr
    assert (
        json.loads((repo / U1_REL).read_text(encoding="utf-8"))["layer_height"]
        == "0.30"
    )

    diff = run_cli(tmp_path, "profiles", "diff", U1, env_extra=env)
    assert diff.returncode == 0
    assert f"--- repo/snapmaker-orca/{U1_REL}" in diff.stdout
    assert f"+++ slicer/snapmaker-orca/{U1_REL}" in diff.stdout

    (repo / U1_REL).write_text(
        (user / U1_REL).read_text(encoding="utf-8"), encoding="utf-8"
    )
    status = run_cli(tmp_path, "profiles", "status", "--json", env_extra=env)
    assert json.loads(status.stdout)["clean"] is True


def test_named_slicer_that_is_not_installed_is_reported(tmp_path: Path) -> None:
    """The user names a slicer that never ran on this machine, a slicer the tool does not
    know, and a preset that does not exist: each is refused with exit 2 and a message saying
    what is missing and what is accepted."""
    _, _, env = _setup(tmp_path)
    result = run_cli(
        tmp_path, "profiles", "sync", "--slicer", "bambu", "--yes", env_extra=env
    )
    assert result.returncode == 2
    assert "Bambu Studio not found" in result.stderr
    bad = run_cli(tmp_path, "profiles", "sync", "--slicer", "prusa", env_extra=env)
    assert bad.returncode == 2 and "orca, snapmaker-orca, bambu, all" in bad.stderr
    missing = run_cli(tmp_path, "profiles", "diff", "No Such Preset", env_extra=env)
    assert missing.returncode == 2 and "no preset named" in missing.stderr


def test_help_and_docs_list_the_same_flags(tmp_path: Path) -> None:
    """The user reads `3d profiles --help` and docs/commands/profiles.md: both list the same
    flags, and every help example appears in the docs."""
    result = run_cli(tmp_path, "profiles", "--help")
    assert result.returncode == 0
    doc = (REPO_ROOT / "docs" / "commands" / "profiles.md").read_text(encoding="utf-8")
    flags = set(re.findall(r"--[a-z][a-z-]+", result.stdout))
    assert flags == set(re.findall(r"--[a-z][a-z-]+", doc)) - {"--help"}
    for example in re.findall(r"^  (3d profiles .+)$", result.stdout, re.M):
        assert example in doc, example
