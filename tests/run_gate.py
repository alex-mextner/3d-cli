"""run_gate.py — the `dev run test` gate: ruff, shellcheck, yamllint, pytest, then mypy; all must pass.

Invoked by rig.yaml scripts.test through the dev runner. Extra argv is forwarded to pytest.
Pytest covers unit tests, CLI smoke tests, any e2e tests under tests/e2e/, and the Home
Assistant integration's own tests (integrations/homeassistant/tests/).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

SHELLCHECK_INSTALL = "brew install shellcheck  /  sudo apt-get install shellcheck"
HA_INTEGRATION = os.path.join("integrations", "homeassistant")


def shell_files(root: str) -> list[str]:
    """Git-tracked shell scripts: `*.sh` plus any other file with an sh/bash shebang."""
    listed = subprocess.run(
        ["git", "ls-files", "-z"], cwd=root, capture_output=True, check=True,
    ).stdout.decode("utf-8").split("\0")
    found: list[str] = []
    for rel in filter(None, listed):
        path = os.path.join(root, rel)
        if not os.path.isfile(path):
            continue  # deleted in the working tree but still in the index
        if rel.endswith(".sh"):
            found.append(rel)
            continue
        with open(path, "rb") as fh:
            first = fh.readline(128)
        if _is_shell_shebang(first):
            found.append(rel)
    return found


def _is_shell_shebang(line: bytes) -> bool:
    """True for `#!/bin/sh`, `#!/bin/bash -e`, `#!/usr/bin/env bash` and the like."""
    if not line.startswith(b"#!"):
        return False
    words = line[2:].split()
    if not words:
        return False
    interp = os.path.basename(words[0])
    if interp == b"env" and len(words) > 1:
        interp = words[1]
    return interp in (b"sh", b"bash")


def run_shellcheck(root: str) -> int:
    """Lint the tracked shell scripts; a missing binary is a loud SKIP, not a failure (CI installs it)."""
    exe = shutil.which("shellcheck")
    if exe is None:
        print(f"SKIP shellcheck: binary not found — install it ({SHELLCHECK_INSTALL}); CI runs it.",
              flush=True)
        return 0
    files = shell_files(root)
    if not files:
        return 0
    return subprocess.run([exe, *files], cwd=root).returncode


def main(argv: list[str]) -> int:
    root = os.environ.get("REPO_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = dict(os.environ)
    env["MYPYPATH"] = os.path.join(root, "lib")

    print("=== ruff ===", flush=True)
    rc_ruff = subprocess.run(
        [sys.executable, "-m", "ruff", "check", os.path.join(root, "lib"), os.path.join(root, "tests")],
        cwd=root, env=env,
    ).returncode

    print("=== shellcheck ===", flush=True)
    rc_shellcheck = run_shellcheck(root)

    print("=== yamllint (integrations/homeassistant) ===", flush=True)
    rc_yamllint = subprocess.run(
        [sys.executable, "-m", "yamllint", "--strict", "."], cwd=os.path.join(root, HA_INTEGRATION), env=env,
    ).returncode

    print("=== pytest (unit + CLI smoke + e2e + HA integration) ===", flush=True)
    rc_pytest = subprocess.run(
        [sys.executable, "-m", "pytest", os.path.join(root, "tests"),
         os.path.join(root, HA_INTEGRATION, "tests"), *argv],
        cwd=root, env=env,
    ).returncode

    print("=== mypy ===", flush=True)
    targets = [
        os.path.join(root, "bin", "3d"),
        os.path.join(root, "lib"),
        os.path.join(root, "tests"),
    ]
    rc_mypy = subprocess.run(
        [sys.executable, "-m", "mypy", "--config-file", os.path.join(root, "mypy.ini"), *targets],
        cwd=root, env=env,
    ).returncode

    ok = rc_ruff == 0 and rc_shellcheck == 0 and rc_yamllint == 0 and rc_pytest == 0 and rc_mypy == 0
    print()
    if ok:
        print(">>> TEST: PASS")
    else:
        print(f">>> TEST: FAIL (ruff rc={rc_ruff}, shellcheck rc={rc_shellcheck}, "
              f"yamllint rc={rc_yamllint}, pytest rc={rc_pytest}, mypy rc={rc_mypy})")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
