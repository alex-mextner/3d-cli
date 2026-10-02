"""run_gate.py — the `dev run test` gate: ruff, pytest, then mypy; all must pass.

Invoked by rig.yaml scripts.test through the dev runner. Extra argv is forwarded to pytest.
Pytest covers unit tests, CLI smoke tests, and any e2e tests under tests/e2e/.

CI-integrity contract (why this file resolves its own root):
  This gate MUST test the tree it physically lives in. A stale `REPO_ROOT` leaking from
  another agent's shell (e.g. pointing at main or a sibling worktree) used to silently win
  over the local tree, so the gate checked the WRONG files and reported a green mypy that
  never saw the new code. Root is therefore derived from THIS file's own directory (no git
  call), never blindly from the environment; a mismatching `REPO_ROOT` is ignored with a
  warning.
  Interpreter/venv selection is pinned upstream in rig.yaml (`UV_PROJECT_ENVIRONMENT=.venv`)
  so uv cannot borrow a sibling worktree's `.venv`; `_warn_on_foreign_venv` is the belt-and
  -suspenders that surfaces it if some other caller still slips a foreign interpreter in.
"""
from __future__ import annotations

import os
import subprocess
import sys


def _resolve_root() -> str:
    """The tree this gate file lives in — authoritative, independent of the caller's env.

    The root is derived purely from THIS file's own location (``<root>/tests/run_gate.py``),
    with symlinks resolved. That is exactly "the tree this gate lives in", so it can never be
    redirected by a leaked/stale ``REPO_ROOT`` from another worktree's session, nor by git
    reporting some parent-repo/submodule toplevel. ``REPO_ROOT`` is honored only when it
    agrees; a disagreeing value is reported and ignored.
    """
    here = os.path.dirname(os.path.realpath(__file__))
    root = os.path.dirname(here)

    env_root = os.environ.get("REPO_ROOT")
    if env_root and os.path.realpath(env_root) != root:
        print(
            f"warning: ignoring REPO_ROOT={env_root!r} — it does not match the tree this "
            f"gate lives in; testing {root} instead.",
            file=sys.stderr, flush=True,
        )
    return root


def _warn_on_foreign_venv(root: str) -> None:
    """Loudly surface a `.venv` borrowed from OUTSIDE this tree (the sibling-worktree bug).

    rig.yaml pins `UV_PROJECT_ENVIRONMENT=.venv` so uv uses the local tree's environment; this
    is the belt-and-suspenders diagnostic for other callers. We key on the exact bug signature
    — an interpreter whose prefix is a `.venv` directory sitting outside `root` (i.e. another
    worktree's venv). A `uv run --with` ephemeral overlay (prefix basename is a cache hash, not
    `.venv`) is deliberately NOT flagged, so this never cries wolf on the normal gate path.
    Files are still checked by absolute path under `root`, so this is a warning, not a failure.
    """
    prefix = os.path.realpath(sys.prefix)
    if os.path.basename(prefix) != ".venv":
        return  # not a project venv (e.g. a uv --with cache overlay) — nothing to flag
    try:
        inside = os.path.commonpath([prefix, root]) == root
    except ValueError:
        inside = False  # different drives (Windows) or mixed abs/relative — treat as foreign
    if not inside:
        print(
            f"warning: interpreter prefix {prefix} is a .venv OUTSIDE this tree ({root}). The "
            f"gate checks this tree's files but with a sibling environment's installed deps. "
            f"Ensure UV_PROJECT_ENVIRONMENT/VIRTUAL_ENV are not pointing at another worktree.",
            file=sys.stderr, flush=True,
        )


def main(argv: list[str]) -> int:
    root = _resolve_root()
    _warn_on_foreign_venv(root)
    env = dict(os.environ)
    # Propagate the AUTHORITATIVE root downstream, overriding any stale/leaked value. pytest's
    # conftest.py `setdefault`s REPO_ROOT and many tests spawn `bin/3d` from it, so a leaked
    # REPO_ROOT would otherwise send those child processes at the wrong tree even though this
    # gate targets the right files by absolute path.
    env["REPO_ROOT"] = root
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
