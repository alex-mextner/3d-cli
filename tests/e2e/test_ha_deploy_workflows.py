"""Readable e2e workflows for `3d ha deploy` (issue #156), against a local stand-in HA host."""

from __future__ import annotations

import os
from pathlib import Path

from ha_deploy_fixtures import build_world

from .workflow_helper import run_cli


def test_cutover_then_a_dry_run_reports_nothing_left(tmp_path: Path) -> None:
    """The HA repo still has the 3D files as regular files. A dry run lists the conversions and
    changes nothing; the deploy turns them into symlinks into the submodule, runs check_config,
    opens the HA-repo PR; the next dry run has 0 changes."""
    world = build_world(tmp_path)
    try:
        args = (
            "ha",
            "deploy",
            "--host",
            "local",
            "--ref",
            world.sha,
            "--source",
            str(world.src),
        )
        dry = run_cli(tmp_path, *args, "--dry-run", env_extra=world.env)
        assert dry.returncode == 0, dry.stderr
        assert "packages/3d_printers.yaml: re-point (symlink)" in dry.stdout
        assert "summary: 3 change(s)" in dry.stdout
        assert not (world.ha / "packages/3d_printers.yaml").is_symlink()

        real = run_cli(tmp_path, *args, env_extra=world.env)
        assert real.returncode == 0, real.stderr
        assert "check_config: Configuration valid" in real.stdout
        assert "HA repo PR: https://github.com/test/ha/pull/7" in real.stdout
        assert (
            os.readlink(world.ha / "scripts/cam.py")
            == "../3d-cli/integrations/homeassistant/scripts/cam.py"
        )

        again = run_cli(tmp_path, *args, "--dry-run", env_extra=world.env)
        assert again.returncode == 0, again.stderr
        assert "changes (0):" in again.stdout
    finally:
        world.server.shutdown()


def test_blocked_deploy_exits_1_and_names_the_problem(tmp_path: Path) -> None:
    """Someone trimmed configuration.yaml on the host and dropped the 3D dashboard. The deploy
    refuses before touching anything and says which key is missing."""
    world = build_world(tmp_path)
    try:
        (world.ha / "configuration.yaml").write_text(
            "homeassistant: {}\n", encoding="utf-8"
        )
        result = run_cli(
            tmp_path,
            "ha",
            "deploy",
            "--host",
            "local",
            "--ref",
            world.sha,
            "--source",
            str(world.src),
            env_extra=world.env,
        )
        assert result.returncode == 1
        assert "configuration.yaml: lovelace: missing" in result.stdout
        assert "blocking problem(s); nothing was changed" in result.stderr
    finally:
        world.server.shutdown()


def test_usage_errors_exit_2(tmp_path: Path) -> None:
    """A user mistypes the deploy flags: each mistake is a usage error naming what was wrong,
    and `--help` shows the flags instead of deploying anything."""
    missing = run_cli(tmp_path, "ha", "deploy", "--ref")
    assert missing.returncode == 2
    assert "--ref needs a value" in missing.stderr
    bad = run_cli(tmp_path, "ha", "deploy", "--frobnicate")
    assert bad.returncode == 2
    assert "unknown option '--frobnicate'" in bad.stderr
    sub = run_cli(tmp_path, "ha", "frob")
    assert sub.returncode == 2
    assert "unknown subcommand 'frob'" in sub.stderr
    helped = run_cli(tmp_path, "ha", "deploy", "--help")
    assert helped.returncode == 0
    assert "--dry-run" in helped.stdout
