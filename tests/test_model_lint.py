from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from errors import InvalidArgument
from model_lint import Level, lint_file, lint_source, resolve_levels

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_THREED = os.path.join(_REPO, "bin", "3d")


def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["REPO_ROOT"] = _REPO
    return subprocess.run(
        [sys.executable, _THREED, *args],
        capture_output=True,
        text=True,
        env=env,
    )


def test_lint_source_reports_model_tag_hygiene() -> None:
    report = lint_source(
        "fixture.scad",
        """// @part Main_Body
// @anchor clip-1
// @anchor clip-1
// @color
// @material PETG
cube([10, 10, 10]);
""",
    )

    findings = {(f.rule_id, f.line, f.message) for f in report.findings}
    assert any(rule == "naming/id-kebab" and line == 1 for rule, line, _ in findings)
    assert any(rule == "object-model/duplicate-id" and line == 3 for rule, line, _ in findings)
    assert any(rule == "object-model/tag-missing-value" and line == 4 for rule, line, _ in findings)
    assert any(rule == "object-model/unknown-tag" and line == 5 for rule, line, _ in findings)
    assert report.error_count == 0
    assert report.warning_count == 4


def test_documented_object_model_tags_are_known() -> None:
    report = lint_source(
        "fixture.scad",
        """// @id boiler
// @class structural
// @view front-left
""",
    )

    assert report.findings == []


def test_lint_source_handles_inline_annotations_but_not_string_literals() -> None:
    report = lint_source(
        "fixture.scad",
        """text("// @id Not_Metadata");
cube(1); // @id Bad_Name
sphere(1); // @id Bad_Name
""",
    )

    assert [(finding.rule_id, finding.line) for finding in report.findings] == [
        ("naming/id-kebab", 2),
        ("naming/id-kebab", 3),
        ("object-model/duplicate-id", 3),
    ]


def test_level_overrides_can_disable_or_promote_rules() -> None:
    levels = resolve_levels({"naming/id-kebab": Level.OFF, "object-model/unknown-tag": Level.ERROR})
    report = lint_source(
        "fixture.scad",
        """// @part Bad_Name
// @material PETG
""",
        levels=levels,
    )

    assert [f.rule_id for f in report.findings] == ["object-model/unknown-tag"]
    assert report.error_count == 1
    assert report.warning_count == 0
    assert report.has_failures(strict=False)


def test_unknown_rule_override_raises_structured_error() -> None:
    with pytest.raises(InvalidArgument) as err:
        resolve_levels({"missing/rule": Level.OFF})

    assert "missing/rule" in err.value.render(color=False)
    assert "accepted:" in err.value.render(color=False)


def test_lint_file_scans_real_file(tmp_path: Path) -> None:
    path = os.path.join(str(tmp_path), "part.scad")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("// @part shell\ncube(1);\n")

    report = lint_file(path)

    assert report.path == path
    assert report.findings == []


def test_lint_command_json_and_strict_exit(tmp_path: Path) -> None:
    path = os.path.join(str(tmp_path), "part.scad")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("// @part Bad_Name\ncube(1);\n")

    normal = _run(["lint", path, "--format", "json"])
    assert normal.returncode == 0, normal.stderr
    payload = json.loads(normal.stdout)
    assert payload["summary"]["warnings"] == 1
    assert payload["summary"]["errors"] == 0
    assert payload["files"][0]["findings"][0]["rule_id"] == "naming/id-kebab"

    strict = _run(["lint", path, "--strict"])
    assert strict.returncode == 1
    assert ">>> LINT: FAIL" in strict.stdout
    assert "Traceback" not in strict.stderr


def test_lint_command_help_works_after_model_path() -> None:
    result = _run(["lint", "model.scad", "--help"])

    assert result.returncode == 0
    assert "3d lint [--all | paths...]" in result.stdout
    assert result.stderr == ""


def test_lint_command_rejects_mixed_repo_and_model_paths(tmp_path: Path) -> None:
    py_path = tmp_path / "core.py"
    scad_path = tmp_path / "part.scad"
    py_path.write_text("value = 1\n", encoding="utf-8")
    scad_path.write_text("// @part shell\ncube(1);\n", encoding="utf-8")

    result = _run(["lint", str(py_path), str(scad_path)])

    assert result.returncode == 2
    assert "cannot mix .scad model lint inputs" in result.stderr
    assert "Traceback" not in result.stderr


def test_lint_command_rejects_model_options_for_python_file(tmp_path: Path) -> None:
    py_path = tmp_path / "core.py"
    py_path.write_text("value = 1\n", encoding="utf-8")

    result = _run(["lint", str(py_path), "--format", "json"])

    assert result.returncode == 2
    assert "model lint options require .scad input files" in result.stderr
    assert "Traceback" not in result.stderr


def test_lint_command_missing_file_is_structured() -> None:
    r = _run(["lint", "/no/such/model.scad"])

    assert r.returncode == 2
    assert "lint: file not found" in r.stderr
    assert "Traceback" not in r.stderr
