"""Human-readable e2e stories for practical 3d CLI workflows."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from .cli_helper import CUBE, REPO_ROOT, THREED


def _story_env(tmp_path: Path) -> dict[str, str]:
    config_home = tmp_path / "xdg-config"
    app_config = config_home / "3d-cli"
    app_config.mkdir(parents=True, exist_ok=True)
    (app_config / ".bootstrapped").write_text("", encoding="utf-8")

    env = dict(os.environ)
    env.update(
        {
            "HOME": str(tmp_path / "home"),
            "REPO_ROOT": str(REPO_ROOT),
            "XDG_CONFIG_HOME": str(config_home),
            "XDG_DATA_HOME": str(tmp_path / "xdg-data"),
            "PYTHONWARNINGS": "error",
        }
    )
    env.pop("PYTHONPATH", None)
    return env


def _run_3d(
    tmp_path: Path,
    *args: str,
    timeout: int = 15,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(THREED), *args],
        cwd=REPO_ROOT,
        env=_story_env(tmp_path),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _json_rows(stdout: str) -> list[dict[str, Any]]:
    payload = json.loads(stdout)
    assert isinstance(payload, list)
    assert all(isinstance(row, dict) for row in payload)
    return payload


def test_user_discovers_the_command_surface_before_installing_heavy_tools(
    tmp_path: Path,
) -> None:
    """A new user can read top-level and command help without OpenSCAD work."""
    help_result = _run_3d(tmp_path, "help")

    assert help_result.returncode == 0, help_result.stderr
    assert "USAGE" in help_result.stdout
    assert "RENDER & VIEW" in help_result.stdout
    assert "QA & GATES" in help_result.stdout
    assert "params" in help_result.stdout
    assert "check" in help_result.stdout
    assert "render" in help_result.stdout

    for command, expected_phrase in (
        ("params", "Customizer-style parameters"),
        ("check", "acceptance master gate"),
        ("render", "single view"),
    ):
        command_help = _run_3d(tmp_path, command, "--help")
        assert command_help.returncode == 0, command_help.stderr
        assert f"3d {command}" in command_help.stdout
        assert expected_phrase in command_help.stdout


def test_user_inspects_a_parametric_model_as_structured_json(tmp_path: Path) -> None:
    """The params command turns OpenSCAD Customizer comments into data."""
    result = _run_3d(tmp_path, "params", str(CUBE), "--json")

    assert result.returncode == 0, result.stderr
    rows = _json_rows(result.stdout)
    by_name = {row["name"]: row for row in rows}
    assert list(by_name) == ["width", "depth", "height", "wall"]
    assert by_name["width"] == {
        "name": "width",
        "value": "20",
        "type": "integer",
        "range": "10:40",
        "description": "outer width (mm)",
    }
    assert by_name["wall"]["value"] == "2"
    assert by_name["wall"]["description"] == "wall thickness (mm)"


def test_user_saves_params_json_for_the_next_shell_step(tmp_path: Path) -> None:
    """A shell workflow can redirect CLI JSON, then load it in another tool."""
    output_path = tmp_path / "cube-params.json"
    with output_path.open("w", encoding="utf-8") as stdout_file:
        result = subprocess.run(
            [sys.executable, str(THREED), "params", str(CUBE), "--json"],
            cwd=REPO_ROOT,
            env=_story_env(tmp_path),
            stdout=stdout_file,
            stderr=subprocess.PIPE,
            text=True,
            timeout=15,
        )

    assert result.returncode == 0, result.stderr
    rows = _json_rows(output_path.read_text(encoding="utf-8"))
    assert [row["name"] for row in rows] == ["width", "depth", "height", "wall"]
    assert {row["name"]: row["value"] for row in rows}["height"] == "16"


def test_user_gets_actionable_errors_for_wrong_command_or_file(
    tmp_path: Path,
) -> None:
    """Bad invocations fail with stable exit codes and user-facing text."""
    unknown = _run_3d(tmp_path, "definitely-not-a-command")

    assert unknown.returncode == 2
    assert unknown.stdout == ""
    assert "unknown command" in unknown.stderr
    assert "Traceback" not in unknown.stderr

    missing_file = _run_3d(tmp_path, "params", "examples/does-not-exist.scad")
    assert missing_file.returncode == 2
    assert "file not found" in missing_file.stderr
    assert "examples/does-not-exist.scad" in missing_file.stderr
    assert "Traceback" not in missing_file.stderr


def test_user_checks_the_local_environment_with_doctor(tmp_path: Path) -> None:
    """Doctor is a readable environment report, even when dependencies are absent."""
    result = _run_3d(tmp_path, "doctor", timeout=30)

    assert result.returncode in (0, 1)
    assert "3d doctor" in result.stdout
    assert "Core" in result.stdout
    assert "Python runtime" in result.stdout
    assert "OpenSCAD libraries" in result.stdout
    assert "install:" in result.stdout or "DOCTOR: PASS" in result.stdout
    assert "Traceback" not in result.stdout
    assert "Traceback" not in result.stderr


def test_user_exports_machine_capabilities_as_json(tmp_path: Path) -> None:
    """Hardware list gives scripts a stable machine/toolchain capability report."""
    result = _run_3d(tmp_path, "hardware", "list", "--json", timeout=30)

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["os"]
    assert payload["machine"]
    assert payload["cpu_count"] >= 1
    assert isinstance(payload["valid"], bool)
    names = {item["name"] for item in payload["items"]}
    assert {"openscad", "imagemagick", "slicer", "python3", "python mesh stack"} <= names
    assert "Traceback" not in result.stderr


def test_user_builds_an_ai_prompt_bundle_without_network_calls(tmp_path: Path) -> None:
    """AI review starts from deterministic CLI evidence instead of a blind prompt."""
    result = _run_3d(
        tmp_path,
        "ai",
        "design",
        "review",
        str(CUBE),
        "--backend",
        "mock",
        "--context",
        "tighten wall thickness before printing",
        "--json",
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["tool"] == "design"
    assert payload["operator"] == "review"
    assert payload["backend"] == "mock"
    assert payload["network_call"] is False
    assert payload["target"] == str(CUBE)
    assert payload["preflight_commands"] == [
        f"3d params {CUBE} --json",
        f"3d check {CUBE}",
    ]
    assert "tighten wall thickness" in payload["prompt"]["user"]
    assert "Traceback" not in result.stderr


def test_user_reads_an_ai_prompt_bundle_as_plain_text(tmp_path: Path) -> None:
    """The default AI output is readable enough to paste into a model session."""
    result = _run_3d(
        tmp_path,
        "ai",
        "design",
        "review",
        str(CUBE),
        "--backend=mock",
    )

    assert result.returncode == 0, result.stderr
    assert "3d ai" in result.stdout
    assert "offline prompt bundle" in result.stdout
    assert "backend: mock" in result.stdout
    assert "preflight plan:" in result.stdout
    assert f"3d params {CUBE} --json" in result.stdout
    assert "No network call has been made" in result.stdout
    assert "Traceback" not in result.stderr


def test_user_plans_animation_frames_before_rendering(tmp_path: Path) -> None:
    """Animation planning shows exactly which render commands will run."""
    outdir = tmp_path / "frames"
    result = _run_3d(
        tmp_path,
        "animate",
        str(CUBE),
        "--plan",
        "--frames",
        "3",
        "--view",
        "front",
        "--size",
        "640x480",
        "--outdir",
        str(outdir),
        "-D",
        "spin=0:180",
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["model"] == str(CUBE)
    assert payload["outdir"] == str(outdir)
    frames = payload["frames"]
    assert [frame["index"] for frame in frames] == [0, 1, 2]
    assert [frame["defines"] for frame in frames] == [["spin=0"], ["spin=90"], ["spin=180"]]
    assert frames[0]["render_argv"] == [
        str(CUBE),
        "--view",
        "front",
        "-o",
        str(outdir / "frame_0000.png"),
        "--size",
        "640x480",
        "-D",
        "spin=0",
    ]
    assert "Traceback" not in result.stderr


def test_user_records_and_queries_workflow_events(tmp_path: Path) -> None:
    """Events make CLI and agent work auditable as JSONL plus a readable table."""
    recorded = _run_3d(
        tmp_path,
        "events",
        "record",
        "--type",
        "cli.render",
        "--subject",
        str(CUBE),
        "--status",
        "pass",
        "--message",
        "rendered preview",
        "--data",
        "view=front",
        "--ts",
        "2026-06-05T12:00:00+00:00",
    )

    assert recorded.returncode == 0, recorded.stderr
    assert "Recorded event" in recorded.stdout

    jsonl = _run_3d(tmp_path, "events", "query", "--subject", str(CUBE))
    assert jsonl.returncode == 0, jsonl.stderr
    rows = [json.loads(line) for line in jsonl.stdout.splitlines()]
    assert len(rows) == 1
    assert rows[0]["type"] == "cli.render"
    assert rows[0]["subject"] == str(CUBE)
    assert rows[0]["status"] == "pass"
    assert rows[0]["message"] == "rendered preview"
    assert rows[0]["data"] == {"view": "front"}

    listed = _run_3d(tmp_path, "events", "list", "--type", "cli.render", "--limit", "1")
    assert listed.returncode == 0, listed.stderr
    assert "cli.render" in listed.stdout
    assert "pass" in listed.stdout
    assert str(CUBE) in listed.stdout
    assert "rendered preview" in listed.stdout

    path = _run_3d(tmp_path, "events", "path")
    assert path.returncode == 0, path.stderr
    assert path.stdout.strip().endswith("events.jsonl")
    assert "Traceback" not in recorded.stderr + jsonl.stderr + listed.stderr + path.stderr


def test_user_imports_external_meshes_into_an_openscad_workflow(tmp_path: Path) -> None:
    """Import turns a mesh into an inspectable SCAD wrapper or a conversion checklist."""
    mesh = tmp_path / "scan.stl"
    mesh.write_text("solid scan\nendsolid scan\n", encoding="utf-8")
    wrapper = tmp_path / "wrappers" / "scan.scad"

    wrapped = _run_3d(
        tmp_path,
        "import",
        str(mesh),
        "-o",
        str(wrapper),
        "--scale",
        "25.4",
        "--convexity",
        "12",
    )

    assert wrapped.returncode == 0, wrapped.stderr
    assert "action: wrapper" in wrapped.stdout
    assert f"output: {wrapper}" in wrapped.stdout
    wrapper_text = wrapper.read_text(encoding="utf-8")
    assert "// Generated by `3d import`." in wrapper_text
    assert "scale([25.4, 25.4, 25.4]) import(" in wrapper_text
    assert "convexity = 12" in wrapper_text

    obj = tmp_path / "scan.obj"
    obj.write_text("# obj placeholder\n", encoding="utf-8")
    planned = _run_3d(
        tmp_path,
        "import",
        str(obj),
        "--mode",
        "plan",
        "--scale",
        "25.4",
        "--convexity",
        "12",
    )

    assert planned.returncode == 0, planned.stderr
    assert "action: plan" in planned.stdout
    assert "Convert Wavefront OBJ mesh to STL or OFF" in planned.stdout
    assert "--scale 25.4" in planned.stdout
    assert "--convexity 12" in planned.stdout
    assert "3d check" in planned.stdout
    assert "Traceback" not in wrapped.stderr + planned.stderr


def test_user_gets_actionable_import_errors_for_unwrappable_formats(tmp_path: Path) -> None:
    """Import explains when a model needs conversion before OpenSCAD can wrap it."""
    obj = tmp_path / "scan.obj"
    obj.write_text("# obj placeholder\n", encoding="utf-8")

    result = _run_3d(tmp_path, "import", str(obj), "--mode", "wrapper")

    assert result.returncode == 2
    assert "import format" in result.stderr
    assert "accepted:" in result.stderr
    assert "3d import" in result.stderr
    assert "--mode plan" in result.stderr
    assert "Traceback" not in result.stderr


def test_user_normalizes_axis_plane_view_and_camera_inputs(tmp_path: Path) -> None:
    """Axis gives scripts stable camera and section vocabulary without rendering."""
    plane = _run_3d(tmp_path, "axis", "plane", "xy", "--json")

    assert plane.returncode == 0, plane.stderr
    assert json.loads(plane.stdout) == {
        "kind": "plane",
        "name": "XY",
        "normal_axis": "Z",
        "normal_vector": [0.0, 0.0, 1.0],
    }

    view = _run_3d(tmp_path, "axis", "view", "front-right")
    assert view.returncode == 0, view.stderr
    assert "kind: view" in view.stdout
    assert "name: front-right" in view.stdout
    assert "direction:" in view.stdout

    camera = _run_3d(tmp_path, "axis", "camera", "1,-1,1,0,0,0")
    assert camera.returncode == 0, camera.stderr
    assert "kind: camera" in camera.stdout
    assert "direction: -1.0,1.0,-1.0" in camera.stdout
    assert "Traceback" not in plane.stderr + view.stderr + camera.stderr


def test_user_tracks_materials_and_parts_before_planning_a_print(tmp_path: Path) -> None:
    """Inventory keeps local stock visible to shell scripts and agent workflows."""
    material = _run_3d(
        tmp_path,
        "inventory",
        "add",
        "material",
        "PLA",
        "--qty",
        "1",
        "--unit",
        "spool",
        "--location",
        "bin 2",
    )

    assert material.returncode == 0, material.stderr
    assert "Added material: PLA" in material.stdout

    part = _run_3d(
        tmp_path,
        "inventory",
        "add",
        "part",
        "M3 nut",
        "--qty",
        "25",
        "--material",
        "steel",
        "--notes",
        "drawer A",
    )

    assert part.returncode == 0, part.stderr
    assert "Added part: M3 nut" in part.stdout

    listing = _run_3d(tmp_path, "inventory", "list")
    assert listing.returncode == 0, listing.stderr
    assert "MATERIALS" in listing.stdout
    assert "PLA" in listing.stdout
    assert "PARTS" in listing.stdout
    assert "M3 nut" in listing.stdout

    shown = _run_3d(tmp_path, "inventory", "show", "part", "M3 nut")
    assert shown.returncode == 0, shown.stderr
    assert "material  steel" in shown.stdout
    assert "notes     drawer A" in shown.stdout
    assert "Traceback" not in material.stderr + part.stderr + listing.stderr + shown.stderr


def test_user_validates_project_joint_specs_before_motion_work(tmp_path: Path) -> None:
    """Kinematics turns a project joint map into stable JSON a script can inspect."""
    project_dir = tmp_path / "robot-arm"
    parts_dir = project_dir / "parts"
    parts_dir.mkdir(parents=True)
    for part_name in ("base", "arm", "gripper"):
        (parts_dir / f"{part_name}.scad").write_text("cube(1);\n", encoding="utf-8")
    (project_dir / "3d.yaml").write_text(
        """project:
  name: robot-arm
  units: mm
parts:
  base:
    file: parts/base.scad
  arm:
    file: parts/arm.scad
  gripper:
    file: parts/gripper.scad
kinematics:
  joints:
    shoulder:
      type: revolute
      parent: base
      child: arm
      axis: [0, 0, 2]
      limits: [-90, 90]
    slide:
      type: prismatic
      parent: arm
      child: gripper
      axis: [3, 0, 0]
      limits: [0, 25]
""",
        encoding="utf-8",
    )

    result = _run_3d(tmp_path, "kinematics", str(project_dir))

    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout)
    assert summary["project"] == "robot-arm"
    assert summary["joint_count"] == 2
    assert [joint["name"] for joint in summary["joints"]] == ["shoulder", "slide"]
    assert summary["joints"][0]["axis"] == [0.0, 0.0, 1.0]
    assert summary["joints"][0]["limits"] == {"max": 90.0, "min": -90.0, "units": "deg"}
    assert summary["joints"][1]["limits"]["units"] == "mm"
    assert "Traceback" not in result.stderr


def test_user_turns_gate_and_score_artifacts_into_a_readable_report(tmp_path: Path) -> None:
    """Report summarizes existing CLI artifacts without rerunning expensive tools."""
    check_log = tmp_path / "check.log"
    score_log = tmp_path / "score.log"
    report_json = tmp_path / "report.json"
    check_log.write_text(
        "MANIFOLD PASS mesh clean\nPRINTABILITY WARN brim suggested\n>>> CHECK: PASS\n",
        encoding="utf-8",
    )
    score_log.write_text("IoU=0.875\nAE=12\n", encoding="utf-8")

    text = _run_3d(tmp_path, "report", "--title", "Reference pass", str(check_log), str(score_log))

    assert text.returncode == 0, text.stderr
    assert "Reference pass" in text.stdout
    assert "Overall: WARN" in text.stdout
    assert "MANIFOLD PASS" in text.stdout
    assert "IoU=0.875" in text.stdout

    json_result = _run_3d(
        tmp_path,
        "report",
        "--json",
        str(check_log),
        str(score_log),
        "-o",
        str(report_json),
    )

    assert json_result.returncode == 0, json_result.stderr
    payload = json.loads(report_json.read_text(encoding="utf-8"))
    assert payload["overall"] == "WARN"
    assert [value["name"] for value in payload["values"]] == ["AE", "IoU"]
    assert "Traceback" not in text.stderr + json_result.stderr


def test_user_gets_animation_usage_when_model_is_missing(tmp_path: Path) -> None:
    """Animate without a model stays readable and does not touch render tools."""
    result = _run_3d(tmp_path, "animate")

    assert result.returncode == 1
    assert "3d animate <file.scad>" in result.stdout
    assert "Traceback" not in result.stderr
