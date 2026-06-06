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


def _assertions(test: ast.FunctionDef) -> list[str]:
    return [ast.unparse(node.test) for node in ast.walk(test) if isinstance(node, ast.Assert)]


def _is_substantive_assertion(assertion: str) -> bool:
    return "returncode" not in assertion and "Traceback" not in assertion


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
    assert re.search(r'"\$THREED"[^\'"\n]*>\s*[\w./-]+', text)
    assert re.search(r"2>\s*[\w./-]+", text)
    assert " | " in text
    assert "&&" in text
    assert "_run_shell(" in text
    assert "json.loads" in text
    assert ".read_text" in text


def test_readable_e2e_stories_have_human_workflow_docstrings() -> None:
    story_tests = _test_functions("tests/e2e/test_cli_stories.py")

    assert story_tests
    for test in story_tests:
        docstring = ast.get_docstring(test)
        assert docstring, f"{test.name} needs a user-workflow docstring"
        assert "todo" not in docstring.lower()
        assert not docstring.lower().startswith("test ")


def test_readable_e2e_stories_assert_more_than_exit_codes() -> None:
    story_tests = _test_functions("tests/e2e/test_cli_stories.py")

    shallow = [
        test.name
        for test in story_tests
        if not any(_is_substantive_assertion(assertion) for assertion in _assertions(test))
    ]

    assert shallow == []


def test_readable_e2e_stories_include_artifact_and_shell_examples() -> None:
    text = _source("tests/e2e/test_cli_stories.py")

    for token in ("json.loads", ".read_text", "subprocess.run", " | ", ">"):
        assert token in text


def test_e2e_policy_docs_name_story_and_shell_chain_conventions() -> None:
    text = (
        _source("tests/e2e/README.md")
        + "\n"
        + _source("docs/rules/testing.md")
    ).lower()

    for token in ("bin/3d", "readable", "artifact", "shell redirection", "pipes"):
        assert token in text
    for token_group in (
        ("story", "stories"),
        ("user task", "user tasks"),
        ("exit code", "return code"),
        ("not an e2e assertion", "not enough"),
        ("new command", "new commands"),
        ("flag", "flags"),
        ("alias", "aliases"),
    ):
        assert any(token in text for token in token_group)
