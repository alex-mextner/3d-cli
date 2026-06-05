from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from errors import InvalidArgument, UsageError
from linting import builtin_registry, default_scan_paths, lint_paths

_REPO = Path(__file__).resolve().parents[1]
_THREED = _REPO / "bin" / "3d"


def _run(args: list[str], *, repo_root: Path = _REPO) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["REPO_ROOT"] = str(repo_root)
    return subprocess.run(
        [sys.executable, str(_THREED), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )


def test_builtin_registry_exposes_no_subject_leakage() -> None:
    registry = builtin_registry()

    assert registry.ids() == ["no-subject-leakage"]
    assert registry.get("no-subject-leakage").id == "no-subject-leakage"


def test_no_subject_leakage_reports_unmarked_core_terms(tmp_path: Path) -> None:
    source = tmp_path / "lib" / "core.py"
    source.parent.mkdir()
    source.write_text(
        "boiler_radius = 10\n"
        "smokebox_profile = {}\n"
        "# locomotive-specific code should not live in core\n",
        encoding="utf-8",
    )

    findings = lint_paths([source], rule_ids=["no-subject-leakage"])

    assert [(f.line, f.term.lower()) for f in findings] == [
        (1, "boiler"),
        (2, "smokebox"),
        (3, "locomotive"),
    ]
    assert findings[0].rule_id == "no-subject-leakage"
    assert findings[0].path == source
    assert "subject-specific leakage" in findings[0].message
    assert "Pass subject-specific terms as project data" in findings[0].remediation


def test_no_subject_leakage_ignores_terms_embedded_in_words(tmp_path: Path) -> None:
    source = tmp_path / "lib" / "core.py"
    source.parent.mkdir()
    source.write_text(
        "boilerplate = 'template text'\n"
        "prefunnel_suffix = 'not a standalone token'\n"
        "locomotive_part = 'still a token because underscore separates'\n",
        encoding="utf-8",
    )

    findings = lint_paths([source], rule_ids=["no-subject-leakage"])

    assert [(f.line, f.term.lower()) for f in findings] == [(3, "locomotive")]


def test_no_subject_leakage_allows_marked_examples_tests_and_docs(tmp_path: Path) -> None:
    lib_file = tmp_path / "lib" / "examples.py"
    test_file = tmp_path / "tests" / "test_subject.py"
    doc_file = tmp_path / "docs" / "topic.py"
    lib_file.parent.mkdir()
    test_file.parent.mkdir()
    doc_file.parent.mkdir()

    lib_file.write_text(
        "# e.g. a locomotive may have a boiler\n"
        "# Example: smokebox and funnel are subject features\n",
        encoding="utf-8",
    )
    test_file.write_text("assert 'boiler' == 'boiler'\n", encoding="utf-8")
    doc_file.write_text("topic = 'funnel'\n", encoding="utf-8")

    assert lint_paths([tmp_path], rule_ids=["no-subject-leakage"]) == []


def test_exempt_paths_are_relative_to_explicit_scan_root(tmp_path: Path) -> None:
    scan_root = tmp_path / "tests" / "checkout"
    core_file = scan_root / "lib" / "core.py"
    doc_file = scan_root / "docs" / "topic.py"
    core_file.parent.mkdir(parents=True)
    doc_file.parent.mkdir(parents=True)
    core_file.write_text("boiler_radius = 10\n", encoding="utf-8")
    doc_file.write_text("boiler_radius = 10\n", encoding="utf-8")

    findings = lint_paths([scan_root], rule_ids=["no-subject-leakage"])

    assert [(f.path, f.term.lower()) for f in findings] == [(core_file, "boiler")]


def test_default_scan_paths_recurses_python_files_under_lib_only(tmp_path: Path) -> None:
    lib_file = tmp_path / "lib" / "a.py"
    nested_file = tmp_path / "lib" / "commands" / "b.py"
    test_file = tmp_path / "tests" / "test_a.py"
    text_file = tmp_path / "lib" / "note.txt"
    for path in (lib_file, nested_file, test_file, text_file):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")

    assert default_scan_paths(tmp_path) == [lib_file, nested_file]


def test_lint_paths_rejects_unknown_rule(tmp_path: Path) -> None:
    with pytest.raises(InvalidArgument) as exc:
        lint_paths([tmp_path], rule_ids=["missing-rule"])

    assert exc.value.flag == "--rule"
    assert exc.value.accepted == ["no-subject-leakage"]


def test_lint_command_reports_findings_and_nonzero(tmp_path: Path) -> None:
    source = tmp_path / "core.py"
    source.write_text("name = 'smokebox'\n", encoding="utf-8")

    result = _run(["lint", str(source)])

    assert result.returncode == 1
    assert f"{source}:1" in result.stdout
    assert "no-subject-leakage" in result.stdout
    assert "smokebox" in result.stdout
    assert "Pass subject-specific terms as project data" in result.stdout


def test_lint_command_json_output(tmp_path: Path) -> None:
    source = tmp_path / "core.py"
    source.write_text("name = 'funnel'\n", encoding="utf-8")

    result = _run(["lint", "--json", "--rule", "no-subject-leakage", str(source)])

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["summary"] == {"findings": 1, "rules": ["no-subject-leakage"]}
    assert payload["findings"][0]["path"] == str(source)
    assert payload["findings"][0]["line"] == 1
    assert payload["findings"][0]["term"] == "funnel"


def test_lint_command_rule_requires_value() -> None:
    from commands.lint import run

    for argv in (["--rule", "--json"], ["--rule", ""]):
        with pytest.raises(UsageError) as got:
            run(argv)

        assert got.value.exit_code == 2
        assert "--rule needs a rule id" in got.value.message


def test_lint_command_no_args_prints_usage() -> None:
    result = _run(["lint"])

    assert result.returncode == 1
    assert "3d lint [--all | paths...]" in result.stdout


def test_lint_command_all_runs_default_scan(tmp_path: Path) -> None:
    lib_dir = tmp_path / "lib"
    lib_dir.mkdir()
    (lib_dir / "safe.py").write_text("value = 1\n", encoding="utf-8")

    result = _run(["lint", "--all"], repo_root=tmp_path)

    assert result.returncode == 0
    assert "lint: PASS" in result.stdout


def test_lint_command_all_honors_repo_root_env(tmp_path: Path) -> None:
    lib_dir = tmp_path / "lib"
    lib_dir.mkdir()
    source = lib_dir / "core.py"
    source.write_text("boiler_radius = 10\n", encoding="utf-8")

    result = _run(["lint", "--all"], repo_root=tmp_path)

    assert result.returncode == 1
    assert f"{source}:1" in result.stdout


def test_lint_command_rejects_all_with_paths(tmp_path: Path) -> None:
    source = tmp_path / "core.py"
    source.write_text("", encoding="utf-8")

    result = _run(["lint", "--all", str(source)])

    assert result.returncode == 2
    assert "--all cannot be combined with explicit paths" in result.stderr
