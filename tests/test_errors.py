"""Unit tests for the structured error types + formatting + exit codes."""
from __future__ import annotations

from errors import (
    GateFailure,
    InputNotFound,
    InvalidArgument,
    MissingDependency,
    ThreeDError,
    UsageError,
)


def test_exit_codes() -> None:
    assert MissingDependency("x", install="i").exit_code == 127
    assert InvalidArgument("--p", "z", ["a"]).exit_code == 2
    assert UsageError("bad").exit_code == 2
    assert InputNotFound("f").exit_code == 2
    assert GateFailure("g").exit_code == 1
    assert ThreeDError("x").exit_code == 1


def test_invalid_argument_lists_accepted_values() -> None:
    e = InvalidArgument("--plane", "ZZ", ["YZ", "XZ", "XY"], command="render")
    msg = e.render(color=False)
    assert "got --plane='ZZ'" in msg
    assert "accepted: YZ, XZ, XY" in msg
    assert msg.startswith("render:")


def test_missing_dependency_shows_install_and_degradation() -> None:
    e = MissingDependency(
        "OpenSCAD", install="brew install --cask openscad",
        degrades="render cannot run", command="render",
    )
    msg = e.render(color=False)
    assert "OpenSCAD not found" in msg
    assert "Install: brew install --cask openscad" in msg
    assert "Without it: render cannot run" in msg


def test_input_not_found_has_remediation() -> None:
    e = InputNotFound("/x/y.scad", command="export")
    msg = e.render(color=False)
    assert "file not found: /x/y.scad" in msg
    assert "->" in msg  # a remediation bullet is present


def test_silent_gate_failure_renders_empty() -> None:
    assert GateFailure("FAIL", silent=True).render(color=False) == ""
    assert GateFailure("FAIL").render(color=False) != ""


def test_no_ansi_when_color_off() -> None:
    msg = UsageError("oops", command="render").render(color=False)
    assert "\033[" not in msg
