"""Readable e2e workflows for `3d init --env` (the machine stage) through the real `bin/3d`.

The service is a local stand-in on a random 127.0.0.1 port; the user config is the isolated
`tmp_path/xdg-config`. No test reaches a real host, printer, ssh or docker.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from printops_env_fakes import GOOD_TOKEN, FakeService

from .workflow_helper import run_cli

FLEET = """\
voron:
  model: Voron 2.4
  owner: own
  access: full
  adapter: moonraker
  host: 10.9.9.3
  port: 7125
  heads: [{label: T1, index: 0}]
u1:
  model: Snapmaker U1
  owner: own
  access: full
  adapter: moonraker
  host: 10.9.9.1
  port: 7125
  heads: [{label: T1, index: 0}]
"""


def _config(tmp_path: Path) -> Path:
    path = tmp_path / "xdg-config" / "3d-cli" / "printops.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def test_init_help_points_at_the_environment_stage(tmp_path: Path) -> None:
    """A user reading `3d init --help` is pointed at `3d init --env`, whose own help lists every environment flag."""
    plain = run_cli(tmp_path, "init", "--help")
    assert plain.returncode == 0 and "3d init --env" in plain.stdout
    env = run_cli(tmp_path, "init", "--env", "--help")
    assert env.returncode == 0, env.stderr
    for flag in (
        "--spoolman TARGET",
        "--script-out PATH",
        "--api URL",
        "--token TOKEN",
        "--no-input",
        "--dry-run",
        "printer:NAME",
        "ssh:USER@HOST",
        "already configured",
    ):
        assert flag in env.stdout


def test_a_configured_machine_is_left_alone(tmp_path: Path) -> None:
    """A user re-running `3d init --env` on an already configured machine sees it reported as configured and nothing is rewritten."""
    svc = FakeService()
    try:
        cfg = _config(tmp_path)
        cfg.write_text(f"api: {svc.url}\ntoken: {GOOD_TOKEN}\n", encoding="utf-8")
        before = (cfg.read_bytes(), cfg.stat().st_mtime_ns)
        r = run_cli(tmp_path, "init", "--env", "--no-input", "--spoolman", "auto")
        assert r.returncode == 0, r.stderr
        assert "already configured" in r.stdout and "nothing changed" in r.stdout
        assert (cfg.read_bytes(), cfg.stat().st_mtime_ns) == before
        assert svc.requests and {m for m, _ in svc.requests} == {"GET"}
    finally:
        svc.close()


def test_an_unconfigured_agent_run_reports_the_commands_and_installs_nothing(
    tmp_path: Path,
) -> None:
    """An agent running `3d init --env --no-input` on an unconfigured machine gets the commands to run and nothing is installed."""
    fleet = tmp_path / "fleet.yaml"
    fleet.write_text(FLEET, encoding="utf-8")
    r = run_cli(
        tmp_path,
        "init",
        "--env",
        "--no-input",
        "--spoolman",
        "auto",
        env_extra={"THREED_FLEET": str(fleet)},
    )
    assert r.returncode == 0, r.stderr
    assert "installs nothing" in r.stdout
    assert (
        "--spoolman printer:voron" in r.stdout and "--spoolman printer:u1" in r.stdout
    )
    assert "NOT recommended" in r.stdout and "--spoolman script" in r.stdout
    assert not _config(tmp_path).exists()


def test_the_standalone_script_is_generated_even_on_a_configured_machine(
    tmp_path: Path,
) -> None:
    """A user asking for `--script-out` gets the standalone installer script even when the machine is already configured."""
    svc = FakeService()
    try:
        _config(tmp_path).write_text(
            f"api: {svc.url}\ntoken: {GOOD_TOKEN}\n", encoding="utf-8"
        )
        out = tmp_path / "out" / "spoolman-setup.sh"
        r = run_cli(
            tmp_path,
            "init",
            "--env",
            "--no-input",
            "--spoolman",
            "script",
            "--script-out",
            str(out),
        )
        assert r.returncode == 0, r.stderr
        assert (
            f"wrote {out}" in r.stdout
            and "--api http://SERVER:8765 --token TOKEN" in r.stdout
        )
        assert out.stat().st_mode & 0o111
        subprocess.run(["bash", "-n", str(out)], check=True)
        text = out.read_text(encoding="utf-8")
        assert "docker compose" in text and "payload()" in text
    finally:
        svc.close()


def test_connecting_to_a_running_service_writes_the_config_only_when_it_answers(
    tmp_path: Path,
) -> None:
    """A user pointing `3d init --env` at a running service gets the config written only after the health check answers."""
    dead = FakeService()
    dead_url = dead.url
    dead.close()
    refused = run_cli(
        tmp_path,
        "init",
        "--env",
        "--no-input",
        "--api",
        dead_url,
        "--token",
        GOOD_TOKEN,
    )
    assert refused.returncode != 0 and "Nothing was written" in refused.stderr
    assert not _config(tmp_path).exists()

    svc = FakeService()
    try:
        wrong = run_cli(
            tmp_path,
            "init",
            "--env",
            "--no-input",
            "--api",
            svc.url,
            "--token",
            "po_wrong",
        )
        assert wrong.returncode != 0 and not _config(tmp_path).exists()
        ok = run_cli(
            tmp_path,
            "init",
            "--env",
            "--no-input",
            "--api",
            svc.url,
            "--token",
            GOOD_TOKEN,
        )
        assert ok.returncode == 0, ok.stderr
        cfg = _config(tmp_path)
        assert cfg.stat().st_mode & 0o777 == 0o600 and GOOD_TOKEN in cfg.read_text(
            encoding="utf-8"
        )
        again = run_cli(tmp_path, "init", "--env", "--no-input", "--spoolman", "auto")
        assert again.returncode == 0 and "already configured" in again.stdout
    finally:
        svc.close()


def test_moving_to_a_new_server_does_not_carry_the_old_token_along(tmp_path: Path) -> None:
    """A user who re-runs `3d init --env --api NEW` without a token ends with a config that no longer holds the
    old server's bearer token, so the next printops command cannot send it to the new host."""
    svc = FakeService()
    try:
        cfg = _config(tmp_path)
        cfg.write_text("api: http://10.255.255.1:8765\ntoken: po_oldserver\n", encoding="utf-8")
        r = run_cli(tmp_path, "init", "--env", "--no-input", "--api", svc.url)
        assert r.returncode == 0, r.stderr
        written = cfg.read_text(encoding="utf-8")
        assert f"api: {svc.url}" in written and "po_oldserver" not in written and "token" not in written
        assert cfg.stat().st_mode & 0o777 == 0o600
        bak = cfg.with_name("printops.yaml.bak")
        assert "po_oldserver" in bak.read_text(encoding="utf-8") and bak.stat().st_mode & 0o777 == 0o600
    finally:
        svc.close()


def test_a_relative_deploy_dir_is_shown_as_the_absolute_path_compose_will_mount(tmp_path: Path) -> None:
    """A user typing `3d printops deploy --local --dry-run --dir ./state` sees the plan use the absolute directory
    under the folder they ran it in, not a path compose would resolve inside the checkout."""
    r = run_cli(tmp_path, "printops", "deploy", "--local", "--dry-run", "--dir", "./state")
    assert r.returncode == 0, r.stderr
    state = tmp_path.resolve() / "state"
    assert f"PRINTOPS_DATA_DIR={state}/data" in r.stdout and "=./state" not in r.stdout
    assert not state.exists()


def test_option_mistakes_exit_2_with_the_accepted_values(tmp_path: Path) -> None:
    """A user who mistypes `--spoolman` or combines clashing flags gets exit 2 and the accepted values on stderr."""
    bad = run_cli(tmp_path, "init", "--env", "--no-input", "--spoolman", "cloud")
    assert bad.returncode == 2
    assert "printer:NAME" in bad.stderr
    clash = run_cli(
        tmp_path,
        "init",
        "--env",
        "--no-input",
        "--api",
        "http://x:1",
        "--spoolman",
        "local",
    )
    assert clash.returncode == 2
    assert "cannot be combined" in clash.stderr


def test_printops_deploy_local_dry_run_prints_the_plan_and_runs_nothing(
    tmp_path: Path,
) -> None:
    """A user running `3d printops deploy --local --dry-run` sees the plan for this machine and nothing is run or created."""
    base = tmp_path / "printops"
    r = run_cli(
        tmp_path, "printops", "deploy", "--local", "--dry-run", "--dir", str(base)
    )
    assert r.returncode == 0, r.stderr
    assert "this machine" in r.stdout and "docker compose -p printops" in r.stdout
    assert "rsync" not in r.stdout and not base.exists()
    both = run_cli(tmp_path, "printops", "deploy", "--local", "--host", "me@nas")
    assert both.returncode == 2 and "exclusive" in both.stderr


def test_init_env_never_triggers_the_openscad_library_bootstrap(tmp_path: Path) -> None:
    """A status-only `3d init --env` (a dotfiles installer's report run) must not git-clone BOSL2/NopSCADlib or
    write the .bootstrapped marker; the project scaffold `3d init` still bootstraps."""
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    log = tmp_path / "git-calls.log"
    git = fakebin / "git"
    git.write_text(f'#!/bin/sh\necho "git $*" >> "{log}"\nexit 1\n', encoding="utf-8")
    git.chmod(0o755)
    fresh = tmp_path / "fresh-config"  # the helper's own config dir ships a ready marker; this one has none
    marker = fresh / "3d-cli" / ".bootstrapped"

    def run(*argv: str) -> subprocess.CompletedProcess[str]:
        shutil.rmtree(fresh, ignore_errors=True)
        log.unlink(missing_ok=True)
        path = f"{fakebin}{os.pathsep}{os.environ['PATH']}"
        return run_cli(tmp_path, *argv, env_extra={"PATH": path, "XDG_CONFIG_HOME": str(fresh)})

    env_run = run("init", "--env", "--no-input", "--spoolman", "auto")
    assert env_run.returncode == 0, env_run.stderr
    assert not log.exists() and not marker.exists()
    assert "libs" not in env_run.stderr

    project = run("init", str(tmp_path / "proj"), "--no-input", "--no-git")
    assert project.returncode == 0, project.stderr
    assert marker.exists()  # the bootstrap ran (its libs may already be present, so no clone is required)
