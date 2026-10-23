"""SshRunner against a fake `ssh` executable (no real ssh, no network)."""

from __future__ import annotations

import json
import stat
import sys
from pathlib import Path
from typing import Any

import pytest
from errors import MissingDependency
from printer_fw.runner import SSH_ERROR_RC, TIMEOUT_RC, CommandResult, SshRunner

FAKE = """#!{python}
import json, os, sys, time
data = sys.stdin.buffer.read() if not sys.stdin.isatty() else b""
with open(os.environ["FAKE_SSH_LOG"], "a") as fh:
    fh.write(json.dumps({{"argv": sys.argv[1:], "stdin": data.decode(errors="replace")}}) + "\\n")
if os.environ.get("FAKE_SSH_SLEEP"):
    time.sleep(float(os.environ["FAKE_SSH_SLEEP"]))
sys.stdout.write(os.environ.get("FAKE_SSH_OUT", ""))
sys.stderr.write(os.environ.get("FAKE_SSH_ERR", ""))
sys.exit(int(os.environ.get("FAKE_SSH_RC", "0")))
"""


@pytest.fixture
def fake_ssh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    exe = tmp_path / "bin" / "ssh"
    exe.parent.mkdir()
    exe.write_text(FAKE.format(python=sys.executable), encoding="utf-8")
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    log = tmp_path / "ssh.log"
    monkeypatch.setenv("PATH", f"{exe.parent}:/usr/bin:/bin")
    monkeypatch.setenv("FAKE_SSH_LOG", str(log))
    return log


def calls(log: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in log.read_text().splitlines()]


def test_an_argv_command_is_quoted_into_one_remote_command_with_batch_mode(
    fake_ssh: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_SSH_OUT", "hello\n")
    res = SshRunner().run(
        "10.0.0.5",
        "root",
        ["cat", "/path with space/printer.cfg"],
        timeout=30,
        port=2222,
    )
    assert res == CommandResult.text(0, "hello\n") and res.ok
    argv = calls(fake_ssh)[0]["argv"]
    assert argv == [
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=30",
        "-p",
        "2222",
        "root@10.0.0.5",
        "cat '/path with space/printer.cfg'",
    ]


def test_a_script_is_fed_to_sh_on_stdin_so_multiline_text_survives(
    fake_ssh: Path,
) -> None:
    script = "set -e\ncat > /tmp/x <<'EOF'\nline\nEOF\n"
    SshRunner().run("h", "root", script, timeout=5)
    call = calls(fake_ssh)[0]
    assert call["argv"][-3:] == ["root@h", "sh", "-s"]
    assert call["stdin"] == script


def test_stdin_is_only_for_argv_commands(fake_ssh: Path) -> None:
    with pytest.raises(ValueError, match="stdin"):
        SshRunner().run("h", "root", "true", timeout=5, stdin=b"x")
    SshRunner().run("h", "root", ["tar", "xzf", "-"], timeout=5, stdin=b"payload")
    assert calls(fake_ssh)[-1]["stdin"] == "payload"


def test_a_failing_command_is_a_result_not_an_exception(
    fake_ssh: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_SSH_RC", "255")
    monkeypatch.setenv("FAKE_SSH_ERR", "Permission denied (publickey).")
    res = SshRunner().run("h", "root", ["true"], timeout=5)
    assert res.rc == 255 and "Permission denied" in res.err and not res.ok


def test_a_hanging_ssh_is_killed_at_the_timeout(
    fake_ssh: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_SSH_SLEEP", "5")
    res = SshRunner().run("h", "root", ["true"], timeout=1)
    assert res.rc == TIMEOUT_RC and "timed out" in res.err


def test_a_missing_ssh_binary_is_a_missing_dependency(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))  # nothing on it
    with pytest.raises(MissingDependency) as err:
        SshRunner().run("h", "root", ["true"], timeout=5)
    assert err.value.exit_code == 127 and "ssh" in err.value.message
    with pytest.raises(MissingDependency):
        SshRunner().check_available()


def test_a_binary_that_cannot_start_is_reported_as_an_ssh_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    exe = tmp_path / "ssh"
    exe.write_text("#!/nonexistent/interpreter\n", encoding="utf-8")
    exe.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    res = SshRunner().run("h", "root", ["true"], timeout=5)
    assert res.rc == SSH_ERROR_RC


def test_check_available_passes_when_ssh_exists(fake_ssh: Path) -> None:
    SshRunner().check_available()
