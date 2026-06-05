"""3d test — run the test gate: pytest (unit + CLI smoke) then mypy.

Delegates to tests/run_gate.py through pyrun so pytest/mypy resolve via the same
.venv/uv/system tiers as every other python tool. Both must pass for exit 0.
"""
from __future__ import annotations

import os

from cli.env import repo_root
from cli.pyrun import exec_tool
from cli.registry import Command

USAGE = """3d test [pytest-args...]
  Run the test gate: pytest (unit tests + CLI smoke harness) then mypy.
  Both must pass for exit 0. Extra args are forwarded to pytest.

  mypy runs over bin/3d + lib/ + tests/ against mypy.ini.

Examples:
  3d test
  3d test -k registry           # only the registry tests
  3d test -x -q"""


def run(argv: list[str]) -> int:
    if argv and argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0
    os.environ.setdefault("REPO_ROOT", repo_root())
    runner = os.path.join(repo_root(), "tests", "run_gate.py")
    # fastapi/uvicorn/markdown/pyyaml: the `web` tests + mypy over lib/web need them
    # importable in the gate's runtime (uv resolves them per-call here).
    return exec_tool("pytest,mypy,fastapi,uvicorn,markdown,pyyaml,httpx", runner, list(argv))


COMMAND = Command(
    name="test",
    group="ENVIRONMENT",
    summary="run the test gate: pytest (unit + CLI smoke) then mypy",
    usage=USAGE,
    run=run,
)
