from __future__ import annotations

import json
from pathlib import Path

import pytest

from errors import InvalidArgument, UsageError


def _write_graph(tmp_path: Path, payload: dict[str, object]) -> str:
    path = tmp_path / "opdag.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


def _sample_graph() -> dict[str, object]:
    return {
        "operations": [
            {"id": "base", "op": "cube", "params": {"size": [40, 20, 8]}},
            {"id": "pocket", "op": "difference", "deps": ["base"]},
            {"id": "boss", "op": "cylinder", "deps": ["base"]},
            {"id": "finished", "op": "union", "deps": ["pocket", "boss"]},
        ]
    }


def test_opdag_summarizes_roots_leaves_edges_and_layers(tmp_path: Path) -> None:
    import opdag

    graph = opdag.load_graph(_write_graph(tmp_path, _sample_graph()))

    assert opdag.describe(graph) == {
        "operations": 4,
        "edges": 4,
        "roots": ["base"],
        "leaves": ["finished"],
        "layers": [["base"], ["pocket", "boss"], ["finished"]],
    }


def test_opdag_plans_dependencies_before_consumers(tmp_path: Path) -> None:
    import opdag

    graph = opdag.load_graph(_write_graph(tmp_path, _sample_graph()))

    assert opdag.plan(graph) == [
        {"step": 1, "id": "base", "op": "cube", "deps": []},
        {"step": 2, "id": "pocket", "op": "difference", "deps": ["base"]},
        {"step": 3, "id": "boss", "op": "cylinder", "deps": ["base"]},
        {"step": 4, "id": "finished", "op": "union", "deps": ["pocket", "boss"]},
    ]


def test_opdag_query_reports_neighborhood_and_transitive_sets(tmp_path: Path) -> None:
    import opdag

    graph = opdag.load_graph(_write_graph(tmp_path, _sample_graph()))

    assert opdag.query(graph, "base") == {
        "id": "base",
        "op": "cube",
        "deps": [],
        "needed_by": ["pocket", "boss"],
        "ancestors": [],
        "descendants": ["pocket", "boss", "finished"],
        "params": {"size": [40, 20, 8]},
    }


def test_opdag_rejects_cycles_with_structured_usage_error(tmp_path: Path) -> None:
    import opdag

    path = _write_graph(
        tmp_path,
        {
            "operations": [
                {"id": "a", "deps": ["b"]},
                {"id": "b", "deps": ["a"]},
            ]
        },
    )

    with pytest.raises(UsageError, match="cycle"):
        opdag.load_graph(path)


def test_opdag_rejects_directory_with_structured_usage_error(tmp_path: Path) -> None:
    import opdag

    with pytest.raises(UsageError, match="got a directory"):
        opdag.load_graph(str(tmp_path))


def test_opdag_rejects_non_utf8_graph_with_structured_usage_error(tmp_path: Path) -> None:
    import opdag

    graph = tmp_path / "graph.json"
    graph.write_bytes(b"\xff\xfe\x00")

    with pytest.raises(UsageError, match="could not read graph JSON file"):
        opdag.load_graph(str(graph))


def test_opdag_query_rejects_unknown_node_with_accepted_values(tmp_path: Path) -> None:
    import opdag

    graph = opdag.load_graph(_write_graph(tmp_path, _sample_graph()))

    with pytest.raises(InvalidArgument) as exc:
        opdag.query(graph, "missing")

    assert exc.value.accepted == ["base", "pocket", "boss", "finished"]


def test_opdag_command_help_and_no_args(capsys: pytest.CaptureFixture[str]) -> None:
    from commands import opdag as command

    assert command.run(["--help"]) == 0
    assert "3d opdag" in capsys.readouterr().out

    assert command.run(["describe", "--help"]) == 0
    assert "3d opdag" in capsys.readouterr().out

    assert command.run([]) == 1
    assert "Subcommands:" in capsys.readouterr().out


def test_opdag_template_rejects_extra_args_with_structured_error() -> None:
    from commands import opdag as command

    with pytest.raises(UsageError, match="does not take arguments"):
        command.run(["template", "extra"])
