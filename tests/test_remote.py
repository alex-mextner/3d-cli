"""`3d remote`: exact rsync/ssh argv (no network: fake ssh/rsync on PATH record what they are asked to do)."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

from cli import remote
from errors import InvalidArgument, UsageError

REPO = Path(__file__).resolve().parent.parent
THREED = [sys.executable, str(REPO / "bin" / "3d")]
CONFIG = """default: heavy
hosts:
  heavy:
    ssh: ultra@box.example
    root: projects
    heavy: projects/bin/heavy
  plain:
    ssh: me@plain.example
    heavy: null
"""


@pytest.fixture
def fakebin(tmp_path: Path) -> Path:
    """Directory with `ssh` and `rsync` stubs that append their argv as a JSON line and exit with $FAKE_RC_<NAME>."""
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    for name in ("ssh", "rsync"):
        stub = bindir / name
        stub.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            f"open(os.environ['FAKE_LOG'], 'a').write(json.dumps(['{name}'] + sys.argv[1:]) + '\\n')\n"
            f"sys.exit(int(os.environ.get('FAKE_RC_{name.upper()}', '0')))\n"
        )
        stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    return bindir


def _run(
    tmp_path: Path, fakebin: Path, args: list[str], **env_extra: str
) -> tuple[subprocess.CompletedProcess[str], list[list[str]]]:
    cfg = tmp_path / "remote.yaml"
    cfg.write_text(CONFIG)
    log = tmp_path / "calls.jsonl"
    log.unlink(missing_ok=True)  # each call reports only its own ssh/rsync lines
    env = dict(os.environ)
    env.update(
        PATH=f"{fakebin}:{env['PATH']}",
        FAKE_LOG=str(log),
        THREED_REMOTE_CONFIG=str(cfg),
        **env_extra,
    )
    out = subprocess.run(
        [*THREED, "remote", *args],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        cwd=tmp_path,
    )
    calls = (
        [json.loads(line) for line in log.read_text().splitlines()]
        if log.exists()
        else []
    )
    return out, calls


def test_push_copies_into_the_work_dir_and_skips_git(
    tmp_path: Path, fakebin: Path
) -> None:
    out, calls = _run(
        tmp_path, fakebin, ["push", "--project", "chekhov", "cat.stl", "phones.json"]
    )
    assert out.returncode == 0, out.stderr
    (argv,) = calls
    assert argv[0] == "rsync" and argv[-1] == "ultra@box.example:projects/chekhov/work/"
    assert argv[-3:-1] == ["cat.stl", "phones.json"]
    assert ".git" in argv and "--mkpath" in argv


def test_run_wraps_the_command_in_heavy_and_quotes_it(
    tmp_path: Path, fakebin: Path
) -> None:
    out, calls = _run(
        tmp_path,
        fakebin,
        [
            "run",
            "--project",
            "p",
            "--name",
            "prep",
            "--wait",
            "--",
            "3d",
            "prep",
            "cat file.stl",
        ],
    )
    assert out.returncode == 0, out.stderr
    (argv,) = calls
    assert (
        argv[:5] == ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15"]
        and argv[5] == "ultra@box.example"
    )
    line = argv[6]
    assert line.startswith(
        'mkdir -p "$HOME"/projects/p/work "$HOME"/projects/p/out "$HOME"/projects/p/logs && cd "$HOME"/projects/p/work && '
    )
    assert line.endswith(
        "\"$HOME\"/projects/bin/heavy -n prep --wait -- 3d prep 'cat file.stl'"
    )


def test_run_push_then_command_then_pull_in_order(
    tmp_path: Path, fakebin: Path
) -> None:
    out, calls = _run(
        tmp_path,
        fakebin,
        [
            "run",
            "--project",
            "p",
            "--push",
            "m.stl",
            "--pull",
            "--to",
            "res",
            "--",
            "echo",
            "hi",
        ],
    )
    assert out.returncode == 0, out.stderr
    assert [c[0] for c in calls] == ["rsync", "ssh", "rsync"]
    assert calls[0][-2:] == ["m.stl", "ultra@box.example:projects/p/work/"]
    assert calls[2][-2:] == ["ultra@box.example:projects/p/out/", "res/"]


def test_failed_command_still_pulls_partial_results_and_keeps_its_exit_code(
    tmp_path: Path, fakebin: Path
) -> None:
    out, calls = _run(
        tmp_path,
        fakebin,
        ["run", "--project", "p", "--pull", "--", "false"],
        FAKE_RC_SSH="3",
    )
    assert out.returncode == 3
    assert [c[0] for c in calls] == [
        "ssh",
        "rsync",
    ]  # logs and partial outputs are the diagnosis


def test_no_pull_on_fail_and_failed_push_stops_before_running(
    tmp_path: Path, fakebin: Path
) -> None:
    out, calls = _run(
        tmp_path,
        fakebin,
        ["run", "--project", "p", "--pull", "--no-pull-on-fail", "--", "false"],
        FAKE_RC_SSH="3",
    )
    assert out.returncode == 3 and [c[0] for c in calls] == ["ssh"]
    out, calls = _run(
        tmp_path,
        fakebin,
        ["run", "--project", "p", "--push", "m.stl", "--", "echo"],
        FAKE_RC_RSYNC="23",
    )
    assert out.returncode == 23 and [c[0] for c in calls] == [
        "rsync"
    ]  # nothing ran on a half-copied input


def test_no_heavy_and_exec_run_the_bare_command(tmp_path: Path, fakebin: Path) -> None:
    _, calls = _run(
        tmp_path, fakebin, ["run", "--project", "p", "--no-heavy", "--", "nproc"]
    )
    assert "heavy" not in calls[0][6] and calls[0][6].endswith("&& nproc")
    _, calls = _run(tmp_path, fakebin, ["exec", "--project", "p", "--", "ls", "out"])
    assert calls[0][6].endswith("&& ls out") and "heavy" not in calls[0][6]


def test_pull_defaults_and_sub(tmp_path: Path, fakebin: Path) -> None:
    _, calls = _run(tmp_path, fakebin, ["pull", "--project", "p"])
    assert calls[0][-2:] == ["ultra@box.example:projects/p/out/", "./remote-out/"]
    _, calls = _run(
        tmp_path, fakebin, ["pull", "--project", "p", "--sub", "logs", "--to", "L"]
    )
    assert calls[0][-2:] == ["ultra@box.example:projects/p/logs/", "L/"]


def test_hosts_and_ad_hoc_target(tmp_path: Path, fakebin: Path) -> None:
    out, _ = _run(tmp_path, fakebin, ["hosts"])
    assert (
        "heavy (default): ultra@box.example" in out.stdout
        and "plain: me@plain.example" in out.stdout
    )
    _, calls = _run(
        tmp_path,
        fakebin,
        ["exec", "--host", "bob@elsewhere", "--project", "p", "--", "id"],
    )
    assert calls[0][5] == "bob@elsewhere"
    out, calls = _run(
        tmp_path, fakebin, ["exec", "--host", "nope", "--project", "p", "--", "id"]
    )
    assert (
        out.returncode == 2 and calls == []
    )  # unknown host name, not a user@host: refuse before ssh


def test_usage_errors_do_not_reach_the_network(tmp_path: Path, fakebin: Path) -> None:
    for args in (
        ["run"],
        ["run", "--project", "p"],
        ["push"],
        ["bogus"],
        ["run", "--frob", "--", "x"],
    ):
        out, calls = _run(tmp_path, fakebin, args)
        assert out.returncode == 2 and calls == [], args


def test_pick_host_rules_and_default_project() -> None:
    hosts = {"a": remote.Host("a", "u@a")}
    assert (
        remote.pick_host(hosts, None, None).ssh == "u@a"
    )  # the only host is the default
    assert remote.pick_host(hosts, None, "x@y").ssh == "x@y"
    with pytest.raises(UsageError):
        remote.pick_host({}, None, None)
    with pytest.raises(InvalidArgument):
        remote.pick_host(hosts, None, "nope")
    assert (
        remote.default_project(Path("/tmp/chekhov"), date(2026, 10, 20))
        == "chekhov-2026-10-20"
    )
