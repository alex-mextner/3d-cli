"""Generated e2e matrix for cheap, deterministic real-CLI invocations."""
from __future__ import annotations

import pytest

from .cli_helper import (
    ALIASES,
    COMMANDS,
    CWD_VARIANTS,
    ENV_VARIANTS,
    PARAMS_CASES,
    TOP_LEVEL_CASES,
    CliCase,
    run_3d,
)


HELP_CASES = [
    CliCase(
        id=f"{cmd}-help-{cwd_variant}-{env_variant}",
        argv=[cmd, "--help"],
        expected_returncodes=(0,),
        stdout_contains=(cmd,),
        cwd_variant=cwd_variant,
        env_variant=env_variant,
    )
    for cmd in COMMANDS
    for cwd_variant in CWD_VARIANTS
    for env_variant in ENV_VARIANTS
]

ALIAS_HELP_CASES = [
    CliCase(
        id=f"{alias}-alias-help-repo-baseline",
        argv=[alias, "--help"],
        expected_returncodes=(0,),
    )
    for alias in ALIASES
]

MATRIX_CASES = [*HELP_CASES, *ALIAS_HELP_CASES, *TOP_LEVEL_CASES, *PARAMS_CASES]


def _case_id(case: CliCase) -> str:
    return case.id


def test_generated_matrix_has_at_least_1000_cases() -> None:
    assert len(MATRIX_CASES) >= 1000


@pytest.mark.parametrize("case", MATRIX_CASES, ids=_case_id)
def test_cli_e2e_matrix(case: CliCase) -> None:
    result = run_3d(case)

    assert result.returncode in case.expected_returncodes, (
        f"{case.id} exited {result.returncode}\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )
    assert "Traceback" not in result.stdout
    assert "Traceback" not in result.stderr
    for expected in case.stdout_contains:
        assert expected in result.stdout
    for expected in case.stderr_contains:
        assert expected in result.stderr
