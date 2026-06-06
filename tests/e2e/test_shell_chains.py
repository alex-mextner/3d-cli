from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_THREED = _REPO / "bin" / "3d"
_CUBE = _REPO / "examples" / "cube.scad"


def _run_shell(script: str, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    config_home = tmp_path / "xdg-config"
    app_config = config_home / "3d-cli"
    app_config.mkdir(parents=True, exist_ok=True)
    (app_config / ".bootstrapped").write_text("", encoding="utf-8")

    env = dict(os.environ)
    env.update({
        "CUBE": str(_CUBE),
        "PYTHON": sys.executable,
        "REPO_ROOT": str(_REPO),
        "THREED": str(_THREED),
        "XDG_CONFIG_HOME": str(config_home),
        "XDG_DATA_HOME": str(tmp_path / "xdg-data"),
    })
    return subprocess.run(
        ["/bin/sh", "-c", script],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_params_json_redirects_to_file(tmp_path: Path) -> None:
    """A user can capture model parameters as a durable artifact for later steps."""
    result = _run_shell('"$THREED" params "$CUBE" --json > params.json', tmp_path)

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    payload = json.loads((tmp_path / "params.json").read_text(encoding="utf-8"))
    assert [row["name"] for row in payload] == ["width", "depth", "height", "wall"]
    assert {row["name"]: row["description"] for row in payload}["wall"] == "wall thickness (mm)"


def test_params_json_pipe_feeds_python_stdin(tmp_path: Path) -> None:
    """A user can pipe CLI JSON into normal scripting tools without temp files."""
    result = _run_shell(
        '"$THREED" params "$CUBE" --json | '
        "\"$PYTHON\" -c 'import json, sys; "
        "rows=json.load(sys.stdin); "
        "print(\"x\".join(row[\"value\"] for row in rows[:3]))'",
        tmp_path,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "20x20x16\n"


def test_shell_chain_turns_model_params_into_print_and_bed_plans(tmp_path: Path) -> None:
    """A small automation chain can extract model data, plan layout, then plan printing."""
    result = _run_shell(
        '"$THREED" params "$CUBE" --json > params.json && '
        "\"$PYTHON\" -c 'import json; "
        "rows={row[\"name\"]: row for row in json.load(open(\"params.json\"))}; "
        "print(\"cube=\" + rows[\"width\"][\"value\"] + \"x\" + rows[\"depth\"][\"value\"] + \":2\")' "
        "> footprint.txt && "
        '"$THREED" pack --bed 90x60 --gap 4 --part "$(cat footprint.txt)" --json > pack.json && '
        '"$THREED" print "$CUBE" --printer "Prusa MK4" --dry-run --job-name cube-fit-check --copies 2 '
        "> print-plan.json && "
        "\"$PYTHON\" -c 'import json; "
        "pack=json.load(open(\"pack.json\")); plan=json.load(open(\"print-plan.json\")); "
        "print(str(len(pack[\"placements\"])) + \":\" + plan[\"printer\"][\"name\"] + \":\" + plan[\"job\"][\"name\"])'",
        tmp_path,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "2:Prusa MK4:cube-fit-check\n"
    pack = json.loads((tmp_path / "pack.json").read_text(encoding="utf-8"))
    assert [(placement["name"], placement["index"]) for placement in pack["placements"]] == [
        ("cube", 1),
        ("cube", 2),
    ]
    assert pack["bed"] == {"depth": 60.0, "width": 90.0}
    plan = json.loads((tmp_path / "print-plan.json").read_text(encoding="utf-8"))
    assert plan["steps"][:2] == ["validate input", "slice model"]
    assert "upload job" in plan["steps"]
    assert "start print" not in plan["steps"]


def test_object_model_query_composes_with_pipe_filters(tmp_path: Path) -> None:
    """A user can treat named 3D features as shell-readable data."""
    model = tmp_path / "fixture.scad"
    model.write_text(
        """// @id base
// @class printed structural
// @anchor left pos=[0,0,0] dir=[0,0,1]
// @anchor right pos=[30,0,0] dir=[0,0,1]
// @color red
cube([30, 12, 4]);
""",
        encoding="utf-8",
    )

    result = _run_shell(
        f'"$THREED" om {shlex.quote(str(model))} .printed > om.json && '
        "cat om.json | "
        "\"$PYTHON\" -c 'import json, sys; "
        "doc=json.load(sys.stdin); "
        "print(doc[\"nodes\"][0][\"id\"] + \":\" + \",\".join(a[\"name\"] for a in doc[\"anchors\"]))'",
        tmp_path,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "base:left,right\n"
    payload = json.loads((tmp_path / "om.json").read_text(encoding="utf-8"))
    assert payload["styles"] == [{"node": "base", "style": {"color": "red"}}]


def test_unknown_command_with_redirection_preserves_exit_code(tmp_path: Path) -> None:
    """A failed shell step keeps stdout/stderr split and returns the command failure."""
    result = _run_shell(
        '"$THREED" definitely-not-a-command > stdout.txt 2> stderr.txt',
        tmp_path,
    )

    assert result.returncode == 2
    assert (tmp_path / "stdout.txt").read_text(encoding="utf-8") == ""
    assert "unknown command" in (tmp_path / "stderr.txt").read_text(encoding="utf-8")
