"""`run_gate.py --fast`: the pre-commit hook's test selection (changed files -> affected tests)."""
from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

import run_gate


def _write(root: Path, rel: str, text: str = "") -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A miniature repo: lib modules, the smoke tests, and tests importing in each supported way."""
    for rel in run_gate.SMOKE_TESTS:
        _write(tmp_path, rel)
    _write(tmp_path, "lib/geo.py")
    _write(tmp_path, "lib/cli/__init__.py")
    _write(tmp_path, "lib/cli/resources.py")
    _write(tmp_path, "lib/commands/__init__.py")
    _write(tmp_path, "lib/commands/import.py")
    _write(tmp_path, "tests/test_geo.py")  # named after lib/geo.py, imports nothing
    _write(tmp_path, "tests/test_uses_geo.py", "def test_x():\n    from geo import area\n")
    _write(tmp_path, "tests/test_cli_resources.py")  # test_<package>_<leaf>.py
    _write(tmp_path, "tests/test_from_cli.py", "from cli import resources\n")
    _write(tmp_path, "tests/test_dynamic.py", "import importlib\nimportlib.import_module('commands.import')\n")
    _write(tmp_path, "tests/test_unrelated.py", "import json\n")
    _write(tmp_path, "tests/geo_fixtures.py", "import geo\n")
    _write(tmp_path, "tests/test_via_fixture.py", "from geo_fixtures import make\n")
    _write(tmp_path, "tests/e2e/__init__.py")
    _write(tmp_path, "tests/e2e/helper.py", "from geo_fixtures import make\n")
    _write(tmp_path, "tests/e2e/test_flow.py", "from .helper import run\n")
    _write(tmp_path, "tests/e2e/test_geo_workflows.py")
    return tmp_path


def test_lib_module_selects_named_importing_and_helper_reached_tests(repo: Path) -> None:
    assert run_gate.select_tests(str(repo), ["lib/geo.py"]) == sorted([
        *run_gate.SMOKE_TESTS,
        "tests/test_geo.py",
        "tests/test_uses_geo.py",
        "tests/test_via_fixture.py",
        "tests/e2e/test_flow.py",  # relative import of a helper that imports a geo fixture
        "tests/e2e/test_geo_workflows.py",
    ])


def test_package_module_matches_from_package_import_and_package_named_test(repo: Path) -> None:
    selected = run_gate.select_tests(str(repo), ["lib/cli/resources.py"])
    assert "tests/test_from_cli.py" in selected
    assert "tests/test_cli_resources.py" in selected
    assert "tests/test_uses_geo.py" not in selected


def test_literal_import_module_call_counts_as_an_import(repo: Path) -> None:
    assert "tests/test_dynamic.py" in run_gate.select_tests(str(repo), ["lib/commands/import.py"])


def test_package_init_change_reaches_its_submodule_importers(repo: Path) -> None:
    assert "tests/test_dynamic.py" in run_gate.select_tests(str(repo), ["lib/commands/__init__.py"])


def test_changed_and_path_mapped_tests_run_and_unrelated_files_get_only_the_smoke_set(repo: Path) -> None:
    _write(repo, "tests/test_cli_smoke.py")
    assert run_gate.select_tests(str(repo), ["tests/test_unrelated.py", "bin/3d"]) == sorted([
        *run_gate.SMOKE_TESTS, "tests/test_unrelated.py", "tests/test_cli_smoke.py",
    ])
    assert run_gate.select_tests(str(repo), ["README.md"]) == sorted(run_gate.SMOKE_TESTS)


def test_deleted_test_file_is_not_passed_to_pytest(repo: Path) -> None:
    assert "tests/test_gone.py" not in run_gate.select_tests(str(repo), ["tests/test_gone.py"])


@pytest.mark.parametrize("conftest", ["tests/conftest.py", "tests/e2e/conftest.py"])
def test_any_conftest_change_runs_the_whole_suite(repo: Path, conftest: str) -> None:
    assert run_gate.select_tests(str(repo), ["lib/geo.py", conftest]) == ["tests"]


def test_unparsable_tests_file_is_handed_to_pytest_instead_of_crashing(repo: Path) -> None:
    _write(repo, "tests/test_wip.py", "def test_x(:\n")
    (repo / "tests" / "latin1_fixtures.py").write_bytes(b"name = '\xe9'\n")
    selected = run_gate.select_tests(str(repo), ["README.md"])
    assert {"tests/test_wip.py", "tests/latin1_fixtures.py"} <= set(selected)


def test_hook_detects_fast_support_by_the_exact_flag_line() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    line = f'FAST_FLAG = "{run_gate.FAST_FLAG}"'
    assert line in (repo_root / "tests" / "run_gate.py").read_text(encoding="utf-8").splitlines()
    assert f"*'{line}'*" in (repo_root / "scripts" / "hooks" / "pre-commit").read_text(encoding="utf-8")


def test_relative_imports_resolve_against_the_package() -> None:
    assert run_gate.imported_modules("from .helper import run\nfrom .. import top\n", "e2e.sub") >= {
        "e2e.sub.helper", "e2e.sub.helper.run", "e2e", "e2e.top",
    }


def test_package_init_relative_import_resolves_inside_its_own_package(repo: Path) -> None:
    # tests/e2e/__init__.py lives in package e2e, so `from . import helper` is e2e.helper.
    assert "e2e.helper" in run_gate.imported_modules("from . import helper\n", "e2e")
    _write(repo, "tests/e2e/__init__.py", "from . import helper\n")
    _write(repo, "tests/test_pkg.py", "import e2e\n")
    assert "tests/test_pkg.py" in run_gate.select_tests(str(repo), ["tests/e2e/helper.py"])


@pytest.fixture
def git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[..., None]:
    """`git <args>` in a fresh repo at tmp_path, isolated from the user's git config (the
    hook-exported GIT_DIR/GIT_INDEX_FILE are already stripped by tests/conftest.py)."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")

    def run(*args: str) -> None:
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    run("init", "-q")
    return run


def _commit_all(git: Callable[..., None]) -> None:
    git("add", ".")
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")


def test_changed_files_lists_staged_and_unstaged_paths(tmp_path: Path, git: Callable[..., None]) -> None:
    _write(tmp_path, "lib/a.py")
    _write(tmp_path, "lib/b.py")
    _commit_all(git)
    _write(tmp_path, "lib/a.py", "x = 1\n")
    git("add", "lib/a.py")
    _write(tmp_path, "lib/b.py", "y = 2\n")  # unstaged
    assert run_gate.changed_files(str(tmp_path)) == ["lib/a.py", "lib/b.py"]


def test_changed_files_keeps_both_sides_of_a_rename(tmp_path: Path, git: Callable[..., None]) -> None:
    _write(tmp_path, "lib/old_name.py", "def f() -> int:\n    return 1\n" * 20)
    _commit_all(git)
    git("mv", "lib/old_name.py", "lib/new_name.py")
    # importers of the old name break on a rename, so they must still be selected
    assert run_gate.changed_files(str(tmp_path)) == ["lib/new_name.py", "lib/old_name.py"]


def test_fast_main_runs_only_the_selection_and_keeps_pytest_args(
    monkeypatch: pytest.MonkeyPatch, repo: Path,
) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setenv("REPO_ROOT", str(repo))
    monkeypatch.setattr(run_gate, "changed_files", lambda _root: ["lib/geo.py"])
    monkeypatch.setattr(run_gate, "run_shellcheck", lambda _root: 0)
    monkeypatch.setattr(run_gate.subprocess, "run", fake_run)

    assert run_gate.main(["--fast", "-x"]) == 0
    pytest_cmd = next(cmd for cmd in calls if cmd[1:3] == ["-m", "pytest"])
    assert pytest_cmd[3:] == [
        os.path.join(str(repo), rel) for rel in run_gate.select_tests(str(repo), ["lib/geo.py"])
    ] + ["-x"]
    assert any(cmd[1:3] == ["-m", "mypy"] for cmd in calls), "--fast still type-checks"


def test_full_main_runs_the_whole_tests_dir_and_the_ha_integration_tests(
        monkeypatch: pytest.MonkeyPatch, repo: Path) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setenv("REPO_ROOT", str(repo))
    monkeypatch.setattr(run_gate, "run_shellcheck", lambda _root: 0)
    monkeypatch.setattr(run_gate.subprocess, "run", fake_run)

    assert run_gate.main(["-k", "registry"]) == 0
    pytest_cmd = next(cmd for cmd in calls if cmd[1:3] == ["-m", "pytest"])
    assert pytest_cmd[3:] == [os.path.join(str(repo), "tests"),
                              os.path.join(str(repo), "integrations", "homeassistant", "tests"), "-k", "registry"]


def test_module_name_maps_threed_vision_package_files() -> None:
    assert run_gate.module_name("packages/threed-vision/src/threed_vision/io.py") == "threed_vision.io"
    assert run_gate.module_name("packages/threed-vision/src/threed_vision/commands/__init__.py") == "threed_vision.commands"
    assert run_gate.module_name("packages/threed-vision/pyproject.toml") is None
