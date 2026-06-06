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
    """Run real shell snippets with user config/data/cache isolated under tmp_path."""
    config_home = tmp_path / "xdg-config"
    app_config = config_home / "3d-cli"
    app_config.mkdir(parents=True, exist_ok=True)
    (app_config / ".bootstrapped").write_text("", encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)

    env = dict(os.environ)
    env.update({
        "CUBE": str(_CUBE),
        "HOME": str(home),
        "PYTHON": sys.executable,
        "PYTHONWARNINGS": "error",
        "REPO_ROOT": str(_REPO),
        "THREED": str(_THREED),
        "XDG_CACHE_HOME": str(tmp_path / "xdg-cache"),
        "XDG_CONFIG_HOME": str(config_home),
        "XDG_DATA_HOME": str(tmp_path / "xdg-data"),
    })
    env.pop("PYTHONPATH", None)
    return subprocess.run(
        ["/bin/sh", "-c", script],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_params_json_redirects_to_file(tmp_path: Path) -> None:
    """A user can capture model parameters as a durable artifact for later steps."""
    result = _run_shell('"$THREED" params "$CUBE" --json > params.json', tmp_path)

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    payload = json.loads((tmp_path / "params.json").read_text(encoding="utf-8"))
    assert [row["name"] for row in payload] == ["width", "depth", "height", "wall"]
    assert payload[0] == {
        "description": "outer width (mm)",
        "name": "width",
        "range": "10:40",
        "type": "integer",
        "value": "20",
    }
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


def test_params_redirect_then_ai_prompt_reuses_shell_derived_context(tmp_path: Path) -> None:
    """Redirect params, derive a wall note, and feed that note into an AI prompt bundle."""
    result = _run_shell(
        "\n".join(
            [
                "set -eu",
                '"$THREED" params "$CUBE" --json > params.json',
                "\"$PYTHON\" -c 'import json, pathlib; "
                "rows={row[\"name\"]: row for row in "
                "json.loads(pathlib.Path(\"params.json\").read_text(encoding=\"utf-8\"))}; "
                "print(\"wall={}mm range={}\".format(rows[\"wall\"][\"value\"], "
                "rows[\"wall\"][\"range\"]))' "
                "> wall-note.txt",
                '"$THREED" ai design review "$CUBE" --backend mock '
                '--context "$(cat wall-note.txt)" --json > prompt.json',
                "\"$PYTHON\" -c 'import json, pathlib; "
                "payload=json.loads(pathlib.Path(\"prompt.json\").read_text(encoding=\"utf-8\")); "
                "print(payload[\"backend\"]); "
                "print(payload[\"network_call\"]); "
                "print(payload[\"preflight_commands\"][0]); "
                "print(next(line for line in payload[\"prompt\"][\"user\"].splitlines() "
                "if \"wall=\" in line))' "
                "> prompt-summary.txt",
            ]
        ),
        tmp_path,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert (tmp_path / "wall-note.txt").read_text(encoding="utf-8") == "wall=2mm range=1.2:4\n"
    prompt = json.loads((tmp_path / "prompt.json").read_text(encoding="utf-8"))
    assert prompt["backend"] == "mock"
    assert prompt["network_call"] is False
    assert prompt["target"] == str(_CUBE)
    assert prompt["preflight_commands"] == [
        f"3d params {_CUBE} --json",
        f"3d check {_CUBE}",
    ]
    assert "wall=2mm range=1.2:4" in prompt["prompt"]["user"]
    assert (tmp_path / "prompt-summary.txt").read_text(encoding="utf-8").splitlines() == [
        "mock",
        "False",
        f"3d params {_CUBE} --json",
        "Additional context: wall=2mm range=1.2:4",
    ]


def test_opdag_template_can_be_redirected_planned_and_queried(tmp_path: Path) -> None:
    """Redirect an operation graph template, then plan and query build order from artifacts."""
    result = _run_shell(
        "\n".join(
            [
                "set -eu",
                '"$THREED" opdag template > graph.json',
                '"$THREED" opdag describe graph.json --json > describe.json',
                '"$THREED" opdag plan graph.json --json > plan.json',
                '"$THREED" opdag query graph.json finished --json > finished.json',
                "\"$PYTHON\" -c 'import json, pathlib; "
                "steps=json.loads(pathlib.Path(\"plan.json\").read_text(encoding=\"utf-8\")); "
                "node=json.loads(pathlib.Path(\"finished.json\").read_text(encoding=\"utf-8\")); "
                "print(\" -> \".join(step[\"id\"] for step in steps)); "
                "print(\"ancestors=\" + \",\".join(node[\"ancestors\"]))' "
                "> build-order.txt",
            ]
        ),
        tmp_path,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    graph = json.loads((tmp_path / "graph.json").read_text(encoding="utf-8"))
    assert graph == {
        "operations": [
            {"id": "base", "op": "cube", "deps": [], "params": {"size": [40, 20, 8]}},
            {"id": "cutout", "op": "difference", "deps": ["base"], "params": {"tool": "slot"}},
            {"id": "finished", "op": "union", "deps": ["cutout"], "params": {}},
        ]
    }
    describe = json.loads((tmp_path / "describe.json").read_text(encoding="utf-8"))
    assert describe["operations"] == 3
    assert describe["roots"] == ["base"]
    assert describe["leaves"] == ["finished"]
    assert describe["layers"] == [["base"], ["cutout"], ["finished"]]
    plan = json.loads((tmp_path / "plan.json").read_text(encoding="utf-8"))
    assert [(step["step"], step["id"], step["op"], step["deps"]) for step in plan] == [
        (1, "base", "cube", []),
        (2, "cutout", "difference", ["base"]),
        (3, "finished", "union", ["cutout"]),
    ]
    finished = json.loads((tmp_path / "finished.json").read_text(encoding="utf-8"))
    assert finished["id"] == "finished"
    assert finished["deps"] == ["cutout"]
    assert finished["ancestors"] == ["base", "cutout"]
    assert finished["descendants"] == []
    assert (tmp_path / "build-order.txt").read_text(encoding="utf-8").splitlines() == [
        "base -> cutout -> finished",
        "ancestors=base,cutout",
    ]
