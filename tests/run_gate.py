"""run_gate.py — the `dev run test` gate: ruff, shellcheck, yamllint, pytest, then mypy; all must pass.

Invoked by rig.yaml scripts.test through the dev runner. Extra argv is forwarded to pytest.
Pytest covers unit tests, CLI smoke tests, any e2e tests under tests/e2e/, and the Home
Assistant integration's own tests (integrations/homeassistant/tests/).

`--fast` (what the pre-commit hook passes) swaps the full pytest sweep for the tests the
changed files can affect plus a small smoke set; ruff, shellcheck and mypy still run in full.
The full suite stays in `dev run test` and in CI, whose green run `gh ship` waits for.

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

import ast
import os
import shutil
import subprocess
import sys

SHELLCHECK_INSTALL = "brew install shellcheck  /  sudo apt-get install shellcheck"
HA_INTEGRATION = "integrations/homeassistant"

# scripts/hooks/pre-commit looks for this exact line to tell a --fast-capable gate apart
# from an older branch's run_gate.py.
FAST_FLAG = "--fast"

# Always part of a --fast run: cheap whole-repo contracts (every command module imports
# stdlib-only, the lib/ layout rules) that a per-module selection would otherwise skip.
SMOKE_TESTS = ("tests/test_imports.py", "tests/test_architecture_structure.py")

# Changed files no test imports, mapped to the tests that pin them.
PATH_TESTS: dict[str, tuple[str, ...]] = {
    "bin/3d": ("tests/test_cli_smoke.py",),
    "rig.yaml": ("tests/test_test_gate_dependencies.py", "tests/test_run_gate.py"),
    "pyproject.toml": ("tests/test_test_gate_dependencies.py",),
    ".github/workflows/ci.yml": ("tests/test_test_gate_dependencies.py",),
    "tests/run_gate.py": (
        "tests/test_test_gate_dependencies.py",
        "tests/test_run_gate_fast.py",
        "tests/test_run_gate.py",
    ),
    "scripts/hooks/pre-commit": ("tests/test_install_dev_hooks.py",),
    "scripts/install-dev-hooks.sh": ("tests/test_install_dev_hooks.py",),
}


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


def changed_files(root: str) -> list[str]:
    """Repo-relative paths staged for commit plus unstaged edits (the gate judges the working tree).
    `--no-renames` keeps a renamed module's old path, whose importers must run too."""
    names: set[str] = set()
    for args in (["diff", "--cached"], ["diff"]):
        out = subprocess.run(
            ["git", *args, "--name-only", "--no-renames", "-z"], cwd=root, capture_output=True, check=True,
        ).stdout
        names.update(filter(None, out.decode("utf-8").split("\0")))
    return sorted(names)


def module_name(rel: str) -> str | None:
    """Dotted name a lib/, tests/ or packages/threed-vision/src Python file is imported under (all three are on sys.path)."""
    for prefix in ("lib/", "tests/", "packages/threed-vision/src/"):
        if rel.startswith(prefix) and rel.endswith(".py"):
            parts = rel[len(prefix):-len(".py")].split("/")
            if parts[-1] == "__init__":
                parts.pop()
            return ".".join(parts) or None
    return None


def _import_names(node: ast.Import) -> set[str]:
    return {alias.name for alias in node.names}


def _import_from_names(node: ast.ImportFrom, parts: list[str]) -> set[str]:
    """`from a import b` yields both `a` and `a.b`; a relative import is anchored in `parts`."""
    anchor = parts[:len(parts) - node.level + 1] if node.level else []
    base = ".".join([*anchor, *([node.module] if node.module else [])])
    found = {f"{base}.{alias.name}" if base else alias.name for alias in node.names}
    if base:
        found.add(base)
    return found


def _import_module_literal(node: ast.Call) -> set[str]:
    """The literal module of an `import_module("x")` call; empty for any other call."""
    if _called_name(node.func) != "import_module" or not node.args:
        return set()
    arg = node.args[0]
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        return {arg.value}
    return set()


def imported_modules(source: str, package: str) -> set[str]:
    """Every module `source` may import: `import a.b`, `from a import b` (both `a` and `a.b`),
    relative imports resolved against `package` (the dotted package the file lives in), and
    literal `import_module("x")` calls."""
    parts = package.split(".") if package else []
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found |= _import_names(node)
        elif isinstance(node, ast.ImportFrom):
            found |= _import_from_names(node, parts)
        elif isinstance(node, ast.Call):
            found |= _import_module_literal(node)
    return found


def _called_name(func: ast.expr) -> str | None:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _imports_any(imported: set[str], modules: set[str]) -> bool:
    return any(name == mod or name.startswith(mod + ".") for name in imported for mod in modules)


def _named_tests(module: str) -> set[str]:
    """Tests named after a lib module: test_<leaf>.py, test_<package>_<leaf>.py, e2e test_<leaf>_workflows.py."""
    parts = module.split(".")
    named = {f"tests/test_{parts[-1]}.py", f"tests/e2e/test_{parts[-1]}_workflows.py"}
    if len(parts) > 1:
        named.add(f"tests/test_{parts[-2]}_{parts[-1]}.py")
    return named


def _tests_tree(root: str) -> tuple[dict[str, set[str]], list[str]]:
    """Every Python file under tests/ (repo-relative path) -> the modules it imports, plus the
    files that do not parse (a work-in-progress edit), which pytest then reports as collection errors."""
    tree: dict[str, set[str]] = {}
    unparsable: list[str] = []
    tests_dir = os.path.join(root, "tests")
    for dirpath, dirnames, filenames in os.walk(tests_dir):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        package = os.path.relpath(dirpath, tests_dir).replace(os.sep, ".").strip(".")
        for filename in filenames:
            if not filename.endswith(".py"):
                continue
            rel = os.path.relpath(os.path.join(dirpath, filename), root).replace(os.sep, "/")
            try:
                with open(os.path.join(root, rel), encoding="utf-8") as fh:
                    tree[rel] = imported_modules(fh.read(), package)
            except (SyntaxError, UnicodeDecodeError):
                unparsable.append(rel)
    return tree, unparsable


def _directly_selected(changed: list[str], is_test: dict[str, bool]) -> tuple[set[str], set[str]]:
    """(test paths picked by the changed paths themselves, changed modules whose importers must run)."""
    selected: set[str] = set()
    affected: set[str] = set()
    for rel in changed:
        selected.update(PATH_TESTS.get(rel, ()))
        if is_test.get(rel):
            selected.add(rel)
            continue
        module = module_name(rel)
        if module is None:
            continue
        affected.add(module)
        if rel.startswith("lib/"):
            selected.update(_named_tests(module))
    return selected, affected


def _with_helper_importers(tree: dict[str, set[str]], is_test: dict[str, bool], affected: set[str]) -> set[str]:
    """`affected` plus every non-test tests/ helper (fixtures, e2e workflow helpers) that imports it, transitively."""
    helpers = {module_name(rel) or "": imported for rel, imported in tree.items() if not is_test[rel]}
    affected = set(affected)
    reached = set(affected)
    while reached:
        reached = {mod for mod, imported in helpers.items()
                   if mod not in affected and _imports_any(imported, affected)}
        affected |= reached
    return affected


def select_tests(root: str, changed: list[str]) -> list[str]:
    """Test paths a --fast run executes for `changed`: the whole tests/ dir when a conftest.py
    changed (its fixtures and hooks reach tests by directory, not by import), else the smoke
    set, changed test files, tests mapped in PATH_TESTS, tests named after a changed lib module,
    every test that imports a changed module directly or through a tests/ helper (fixtures, e2e
    helpers), and any tests/ file that does not parse."""
    if any(rel.startswith("tests/") and os.path.basename(rel) == "conftest.py" for rel in changed):
        return ["tests"]
    tree, unparsable = _tests_tree(root)
    is_test = {rel: os.path.basename(rel).startswith("test_") for rel in tree}
    selected, affected = _directly_selected(changed, is_test)
    selected |= {*SMOKE_TESTS, *unparsable}
    affected = _with_helper_importers(tree, is_test, affected)
    selected.update(rel for rel, imported in tree.items() if is_test[rel] and _imports_any(imported, affected))
    return sorted(rel for rel in selected if os.path.isfile(os.path.join(root, rel)))


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


def pytest_targets(root: str, fast: bool) -> list[str]:
    """Absolute pytest paths for this run, announcing the selection: the full sweep, or with --fast the changed-file selection."""
    if not fast:
        print("=== pytest (unit + CLI smoke + e2e + HA integration) ===", flush=True)
        return [os.path.join(root, "tests"), os.path.join(root, HA_INTEGRATION, "tests")]
    changed = changed_files(root)
    selected = select_tests(root, changed)
    if any(rel.startswith(HA_INTEGRATION + "/") for rel in changed):
        selected.append(f"{HA_INTEGRATION}/tests")
    print(f"=== pytest --fast: {len(selected)} test path(s) for the changed files "
          "(full suite: `dev run test`, CI) ===", flush=True)
    print("\n".join(f"  {rel}" for rel in selected), flush=True)
    return [os.path.join(root, rel) for rel in selected]


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
    fast = FAST_FLAG in argv
    pytest_args = [arg for arg in argv if arg != FAST_FLAG]

    print("=== ruff ===", flush=True)
    rc_ruff = subprocess.run(
        [sys.executable, "-m", "ruff", "check", os.path.join(root, "lib"), os.path.join(root, "tests"),
         os.path.join(root, "packages", "threed-vision", "src")],
        cwd=root, env=env,
    ).returncode

    print("=== shellcheck ===", flush=True)
    rc_shellcheck = run_shellcheck(root)

    print("=== yamllint (integrations/homeassistant) ===", flush=True)
    rc_yamllint = subprocess.run(
        [sys.executable, "-m", "yamllint", "--strict", "."], cwd=os.path.join(root, HA_INTEGRATION), env=env,
    ).returncode

    test_targets = pytest_targets(root, fast)
    rc_pytest = subprocess.run(
        [sys.executable, "-m", "pytest", *test_targets, *pytest_args],
        cwd=root, env=env,
    ).returncode

    print("=== mypy ===", flush=True)
    targets = [
        os.path.join(root, "bin", "3d"),
        os.path.join(root, "lib"),
        os.path.join(root, "tests"),
        os.path.join(root, "packages", "threed-vision", "src", "threed_vision"),
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
