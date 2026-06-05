"""run_gate.py — the `3d test` gate: ruff, pytest, then mypy; all must pass.

Invoked by the `test` command through pyrun so ruff/pytest/mypy resolve via the same
.venv/uv/system tiers as every other python tool. Extra argv is forwarded to pytest.
Pytest covers unit tests, CLI smoke tests, and any e2e tests under tests/e2e/.
"""
from __future__ import annotations

import os
import subprocess
import sys


def main(argv: list[str]) -> int:
    root = os.environ.get("REPO_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = dict(os.environ)
    env["MYPYPATH"] = os.path.join(root, "lib")

    print("=== ruff ===", flush=True)
    rc_ruff = subprocess.run(
        [sys.executable, "-m", "ruff", "check", os.path.join(root, "lib"), os.path.join(root, "tests")],
        cwd=root, env=env,
    ).returncode

    print("=== pytest (unit + CLI smoke + e2e) ===", flush=True)
    rc_pytest = subprocess.run(
        [sys.executable, "-m", "pytest", os.path.join(root, "tests"), *argv],
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

    ok = rc_ruff == 0 and rc_pytest == 0 and rc_mypy == 0
    print()
    if ok:
        print(">>> TEST: PASS")
    else:
        print(f">>> TEST: FAIL (ruff rc={rc_ruff}, pytest rc={rc_pytest}, mypy rc={rc_mypy})")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
