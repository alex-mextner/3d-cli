from __future__ import annotations

import ast
import re
from pathlib import Path

from e2e.cli_helper import ALIASES, COMMANDS
from e2e.test_cli_matrix import ALIAS_HELP_CASES, HELP_CASES


ROOT = Path(__file__).resolve().parents[1]


def _source(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def _test_functions(path: str) -> list[ast.FunctionDef]:
    module = ast.parse(_source(path), filename=path)
    return [
        node
        for node in module.body
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test_")
    ]


def test_generated_e2e_matrix_smokes_every_registered_command() -> None:
    covered = {
        case.argv[0]
        for case in HELP_CASES
        if len(case.argv) == 2 and case.argv[1] == "--help"
    }

    assert set(COMMANDS) <= covered


def test_generated_e2e_matrix_smokes_every_registered_alias() -> None:
    covered = {
        case.argv[0]
        for case in ALIAS_HELP_CASES
        if len(case.argv) == 2 and case.argv[1] == "--help"
    }

    assert set(ALIASES) <= covered


def test_shell_chain_e2e_uses_real_cli_redirection_and_pipes() -> None:
    text = _source("tests/e2e/test_shell_chains.py")

    assert '"$THREED"' in text
    assert re.search(r'"\$THREED"[^\'"\n]*>\s*[\w.-]+', text)
    assert re.search(r"2>\s*[\w.-]+", text)
    assert re.search(r'"\$THREED"[^\'"\n]*\|', text)
    assert "_run_shell(" in text


def test_readable_e2e_stories_have_human_workflow_docstrings() -> None:
    story_tests = _test_functions("tests/e2e/test_cli_stories.py")

    assert story_tests
    for test in story_tests:
        docstring = ast.get_docstring(test)
        assert docstring, f"{test.name} needs a user-workflow docstring"
        assert "todo" not in docstring.lower()
        assert not docstring.lower().startswith("test ")


def test_e2e_policy_docs_name_story_and_shell_chain_conventions() -> None:
    text = (
        _source("tests/e2e/README.md")
        + "\n"
        + _source("docs/rules/testing.md")
    ).lower()

    for phrase in (
        "bin/3d",
        "readable",
        "story",
        "shell redirection",
        "pipes",
        "new command",
        "flag",
        "alias",
    ):
        assert phrase in text
