"""fake_tool_bins.py — autouse fixtures: no test can run a real tg, tg-ctl, omp, tmux, systemd-run
or omp-steer.

Registered for the whole suite via `pytest_plugins` in tests/printops_fixtures.py. Tg messages the
user's phone, tg-ctl and omp-steer steer live omp sessions, tmux/omp/systemd-run start sessions and
units; on 2026-10-23 a pytest run sent a real "CRITICAL u1 ..." Telegram message through the
tg_fallback sink (ticket https://github.com/alex-mextner/3d-cli/issues/536).

  _fake_bin_dir     session scope: one dir of recording fake executables per pytest process
  _fake_tool_bins   autouse: per test, the fakes go first on PATH (so `3d` and subprocess children
                    inherit them), $FAKE_BIN_LOG names the test's log, and the fixed-dir fallbacks of
                    find_tool are emptied so nothing falls through to ~/.local/bin or /usr/bin
"""

from __future__ import annotations

import hashlib
import importlib
import os
import sys
from pathlib import Path

import pytest

FAKE_TOOLS = ("tg", "tg-ctl", "omp", "tmux", "systemd-run", "omp-steer")
# modules whose find_tool() falls back to fixed install dirs (~/.local/bin, /usr/bin) after PATH
_TOOL_DIR_MODULES = ("printer_ops.printer_agent", "printer_ops.watch_sinks")

_FAKE_BIN_SOURCE = """#!{python}
import json, os, sys
name = {name!r}
log = os.environ.get("FAKE_BIN_LOG")
if log:
    with open(log, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({{"tool": name, "argv": sys.argv[1:]}}) + "\\n")
if name == "systemd-run":  # a resource scope: run the wrapped command (lib/cli/heavy.py, the fake tmux)
    valued = ("-p", "--property", "--unit", "-u", "--slice", "--description", "--working-directory",
              "-E", "--setenv", "--uid", "--gid", "--nice")
    args, i = sys.argv[1:], 0
    while i < len(args) and args[i] != "--" and args[i].startswith("-"):
        i += 2 if args[i] in valued else 1
    if i < len(args) and args[i] == "--":
        i += 1
    if i < len(args):
        os.execvp(args[i], args[i:])
if name == "tg-ctl" and sys.argv[1:4] == ["omp-live", "status", "--json"]:
    print("[]")
if name == "tmux" and sys.argv[1:2] == ["has-session"]:
    sys.exit(1)
"""


@pytest.fixture(scope="session")
def _fake_bin_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """`<root>/bin` holds the fakes, `<root>/logs` one log per test (each xdist worker has its own)."""
    root = tmp_path_factory.mktemp("fake-bin")
    bindir = root / "bin"
    bindir.mkdir()
    for name in FAKE_TOOLS:
        exe = bindir / name
        exe.write_text(
            _FAKE_BIN_SOURCE.format(python=sys.executable, name=name), encoding="utf-8"
        )
        exe.chmod(0o755)
    (root / "logs").mkdir()
    return root


@pytest.fixture(autouse=True)
def _fake_tool_bins(
    _fake_bin_dir: Path, request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Every call of a fake appends one JSON line {tool, argv} to the returned per-test log
    ($FAKE_BIN_LOG). A test that prepends its own fake bin to PATH after this still wins."""
    log = (
        _fake_bin_dir
        / "logs"
        / (hashlib.sha1(request.node.nodeid.encode()).hexdigest()[:16] + ".log")
    )
    log.unlink(missing_ok=True)
    monkeypatch.setenv("FAKE_BIN_LOG", str(log))
    monkeypatch.setenv(
        "PATH", f"{_fake_bin_dir / 'bin'}{os.pathsep}{os.environ.get('PATH', '')}"
    )
    for modname in _TOOL_DIR_MODULES:
        try:
            module = importlib.import_module(modname)
        except ImportError:
            continue
        if hasattr(module, "_TOOL_DIRS"):
            monkeypatch.setattr(module, "_TOOL_DIRS", ())
    return log
