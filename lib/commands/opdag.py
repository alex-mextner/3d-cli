"""3d opdag — describe, plan, and query model operation DAGs."""
from __future__ import annotations

import json
from typing import Callable

import opdag
from cli.registry import Command
from errors import UsageError

USAGE = """3d opdag <subcommand> <graph.json> [options]
  Describe, plan, and query an operation DAG for model build steps.

Subcommands:
  describe <graph.json> [--json]       summarize roots, leaves, edges, and layers
  plan <graph.json> [--json]           print dependency-ordered build steps
  query <graph.json> <node> [--json]   inspect one operation's neighbors and closure
  template                             print a minimal graph JSON template

Graph shape:
  {"operations": [{"id": "base", "op": "cube", "deps": [], "params": {}}]}

Examples:
  3d opdag describe build.json
  3d opdag plan build.json --json
  3d opdag query build.json final"""


def run(argv: list[str]) -> int:
    if not argv:
        print(USAGE)
        return 1
    sub = argv[0]
    if sub in ("-h", "--help", "help"):
        print(USAGE)
        return 0
    if sub == "template":
        if len(argv) != 1:
            raise UsageError(
                f"opdag template does not take arguments: {' '.join(argv[1:])}",
                command="opdag",
                remediation=["Run `3d opdag template` with no extra arguments."],
            )
        print(json.dumps(_template(), indent=2))
        return 0
    if sub == "describe":
        if _subcommand_help(argv[1:]):
            print(USAGE)
            return 0
        graph_path, as_json = _graph_args(argv[1:], sub)
        description = opdag.describe(opdag.load_graph(graph_path))
        _print_json_or_text(description, as_json, _format_describe)
        return 0
    if sub == "plan":
        if _subcommand_help(argv[1:]):
            print(USAGE)
            return 0
        graph_path, as_json = _graph_args(argv[1:], sub)
        build_plan = opdag.plan(opdag.load_graph(graph_path))
        _print_json_or_text(build_plan, as_json, _format_plan)
        return 0
    if sub == "query":
        if _subcommand_help(argv[1:]):
            print(USAGE)
            return 0
        graph_path, node, as_json = _query_args(argv[1:])
        node_report = opdag.query(opdag.load_graph(graph_path), node)
        _print_json_or_text(node_report, as_json, _format_query)
        return 0

    raise UsageError(
        f"unknown opdag subcommand '{sub}'",
        command="opdag",
        remediation=["Run `3d opdag --help` for the available subcommands."],
    )


def _graph_args(argv: list[str], subcommand: str) -> tuple[str, bool]:
    if not argv:
        raise UsageError(
            f"opdag {subcommand} needs a graph JSON file",
            command="opdag",
            remediation=[f"Example: 3d opdag {subcommand} build.json"],
        )
    graph_path = argv[0]
    as_json = False
    for arg in argv[1:]:
        if arg == "--json":
            as_json = True
        else:
            raise UsageError(
                f"unknown option for opdag {subcommand}: {arg}",
                command="opdag",
                remediation=[f"Run `3d opdag {subcommand} --help` for usage."],
            )
    return graph_path, as_json


def _query_args(argv: list[str]) -> tuple[str, str, bool]:
    if len(argv) < 2:
        raise UsageError(
            "opdag query needs a graph JSON file and node id",
            command="opdag",
            remediation=["Example: 3d opdag query build.json final"],
        )
    graph_path = argv[0]
    node = argv[1]
    as_json = False
    for arg in argv[2:]:
        if arg == "--json":
            as_json = True
        else:
            raise UsageError(
                f"unknown option for opdag query: {arg}",
                command="opdag",
                remediation=["Run `3d opdag query --help` for usage."],
            )
    return graph_path, node, as_json


def _subcommand_help(argv: list[str]) -> bool:
    return bool(argv and argv[0] in ("-h", "--help", "help"))


def _print_json_or_text(value: object, as_json: bool, formatter: Callable[[object], str]) -> None:
    if as_json:
        print(json.dumps(value, indent=2, sort_keys=True))
    else:
        print(formatter(value))


def _format_describe(value: object) -> str:
    data = value if isinstance(value, dict) else {}
    lines = [f"OPDAG: {data['operations']} operations, {data['edges']} dependencies"]
    lines.append("Roots: " + _join(data["roots"]))
    lines.append("Leaves: " + _join(data["leaves"]))
    lines.append("Layers:")
    for idx, layer in enumerate(data["layers"], start=1):
        lines.append(f"  {idx}. {_join(layer)}")
    return "\n".join(lines)


def _format_plan(value: object) -> str:
    steps = value if isinstance(value, list) else []
    lines: list[str] = []
    for raw in steps:
        step = raw if isinstance(raw, dict) else {}
        deps = step.get("deps", [])
        suffix = f" <- {_join(deps)}" if deps else ""
        lines.append(f"{step['step']}. {step['id']} [{step['op']}]{suffix}")
    return "\n".join(lines)


def _format_query(value: object) -> str:
    data = value if isinstance(value, dict) else {}
    lines = [f"{data['id']} [{data['op']}]"]
    lines.append("Deps: " + _join(data["deps"]))
    lines.append("Needed by: " + _join(data["needed_by"]))
    lines.append("Ancestors: " + _join(data["ancestors"]))
    lines.append("Descendants: " + _join(data["descendants"]))
    if data["params"]:
        lines.append("Params: " + json.dumps(data["params"], sort_keys=True))
    return "\n".join(lines)


def _join(value: object) -> str:
    if isinstance(value, list):
        return ", ".join(str(item) for item in value) or "-"
    return "-"


def _template() -> dict[str, object]:
    return {
        "operations": [
            {"id": "base", "op": "cube", "deps": [], "params": {"size": [40, 20, 8]}},
            {"id": "cutout", "op": "difference", "deps": ["base"], "params": {"tool": "slot"}},
            {"id": "finished", "op": "union", "deps": ["cutout"], "params": {}},
        ]
    }


COMMAND = Command(
    name="opdag",
    group="GEOMETRY & EXPORT",
    summary="describe / plan / query model operation DAGs",
    usage=USAGE,
    run=run,
)
