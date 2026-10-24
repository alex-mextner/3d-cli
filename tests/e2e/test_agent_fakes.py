"""The recording fake tmux/omp/systemd-run that `run_cli` puts first on PATH (tests/e2e/workflow_helper.py)."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from .workflow_helper import _apply_env_extra, agent_fakebin, isolated_env


def test_fake_systemd_run_skips_property_values_and_honours_the_double_dash(tmp_path: Path) -> None:
    """`3d heavy` probes `systemd-run --user --scope -q -p MemoryMax=1G -- true`: the fake must run
    `true`, not try to exec `MemoryMax=1G`."""
    env = isolated_env(tmp_path)
    probe = subprocess.run(["systemd-run", "--user", "--scope", "-q", "-p", "MemoryMax=1G", "--", "true"],
                           env=env, capture_output=True, text=True, check=False)
    flags_only = subprocess.run(["systemd-run", "--user", "--scope"], env=env, capture_output=True, text=True, check=False)

    assert probe.returncode == 0, probe.stderr
    assert flags_only.returncode == 0, flags_only.stderr
    assert "Traceback" not in probe.stderr + flags_only.stderr


def test_env_extra_keeps_a_test_supplied_tool_ahead_of_the_agent_fakes(tmp_path: Path) -> None:
    """A test's own `tmux` on its PATH wins over the recording fake; the tools it did not fake
    (omp, systemd-run) still resolve to the recording fakes, never to a real binary."""
    own = tmp_path / "own"
    own.mkdir()
    (own / "tmux").write_text("#!/bin/sh\necho own-tmux\n", encoding="utf-8")
    (own / "tmux").chmod(0o755)
    env = isolated_env(tmp_path)
    _apply_env_extra(env, {"PATH": f"{own}{os.pathsep}{env['PATH']}"}, tmp_path)

    fakes = agent_fakebin(tmp_path)
    assert shutil.which("tmux", path=env["PATH"]) == str(own / "tmux")
    assert shutil.which("omp", path=env["PATH"]) == str(fakes / "omp")
    assert shutil.which("systemd-run", path=env["PATH"]) == str(fakes / "systemd-run")
