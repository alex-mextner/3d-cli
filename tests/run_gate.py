"""run_gate.py — the `3d test` gate: pytest then mypy, both must pass.

Invoked by the `test` command through pyrun so pytest/mypy resolve via the same
.venv/uv/system tiers as every other python tool. Extra argv is forwarded to pytest.
"""
from __future__ import annotations

import os
import subprocess
import sys


def main(argv: list[str]) -> int:
    root = os.environ.get("REPO_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = dict(os.environ)
    env["MYPYPATH"] = os.path.join(root, "lib")

    print("=== pytest ===", flush=True)
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

    ok = rc_pytest == 0 and rc_mypy == 0
    print()
    print(">>> TEST: PASS" if ok else f">>> TEST: FAIL (pytest rc={rc_pytest}, mypy rc={rc_mypy})")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
