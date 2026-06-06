"""Operation DAG support for planned model build steps.

The graph format is intentionally small and JSON-native:

    {"operations": [{"id": "base", "op": "cube", "deps": []}]}

Each operation has a stable string id, an optional operation kind (`op`), optional
dependency ids (`deps`), and optional JSON-style params. The helpers here validate
that shape, reject cycles, and return dict/list payloads that command wrappers can
print as text or JSON without knowing graph internals.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping

from errors import InputNotFound, InvalidArgument, UsageError


@dataclass(frozen=True)
class Operation:
    id: str
    op: str
    deps: tuple[str, ...]
    params: dict[str, Any]


@dataclass(frozen=True)
class OperationGraph:
    operations: tuple[Operation, ...]
    by_id: dict[str, Operation]
    needed_by: dict[str, list[str]]

    def ids(self) -> list[str]:
        return [op.id for op in self.operations]


def load_graph(path: str) -> OperationGraph:
    try:
        with open(path, encoding="utf-8") as f:
            payload = json.load(f)
    except FileNotFoundError:
        raise InputNotFound(path, command="opdag")
    except IsADirectoryError:
        raise UsageError(
            f"expected a graph JSON file, got a directory: {path}",
            command="opdag",
            remediation=["Pass a file created from `3d opdag template`, not a directory."],
        )
    except (OSError, UnicodeDecodeError) as e:
        raise UsageError(
            f"could not read graph JSON file {path}: {getattr(e, 'strerror', None) or e}",
            command="opdag",
            remediation=["Check file permissions and pass a readable graph JSON file."],
        )
    except json.JSONDecodeError as e:
        raise UsageError(
            f"invalid JSON in {path}: line {e.lineno}, column {e.colno}",
            command="opdag",
            remediation=["Use `3d opdag template` to see the expected operation graph shape."],
        )
    return parse_graph(payload)


def parse_graph(payload: Any) -> OperationGraph:
    if not isinstance(payload, Mapping):
        raise UsageError(
            "operation graph must be a JSON object",
            command="opdag",
            remediation=["Expected: {\"operations\": [{\"id\": \"base\", \"deps\": []}]}"],
        )
    raw_ops = payload.get("operations")
    if not isinstance(raw_ops, list) or not raw_ops:
        raise UsageError(
            "operation graph needs a non-empty 'operations' list",
            command="opdag",
            remediation=["Add at least one operation with an 'id' field."],
        )

    ops: list[Operation] = []
    seen: set[str] = set()
    for idx, raw in enumerate(raw_ops, start=1):
        op = _parse_operation(raw, idx)
        if op.id in seen:
            raise UsageError(
                f"duplicate operation id '{op.id}'",
                command="opdag",
                remediation=["Operation ids must be unique because dependencies refer to them."],
            )
        seen.add(op.id)
        ops.append(op)

    by_id = {op.id: op for op in ops}
    for op in ops:
        missing = [dep for dep in op.deps if dep not in by_id]
        if missing:
            raise UsageError(
                f"operation '{op.id}' depends on unknown id '{missing[0]}'",
                command="opdag",
                remediation=[f"Accepted operation ids: {', '.join(by_id)}"],
            )

    needed_by: dict[str, list[str]] = {op.id: [] for op in ops}
    for op in ops:
        for dep in op.deps:
            needed_by[dep].append(op.id)

    graph = OperationGraph(tuple(ops), by_id, needed_by)
    _topological_ids(graph)
    return graph


def describe(graph: OperationGraph) -> dict[str, object]:
    roots = [op.id for op in graph.operations if not op.deps]
    leaves = [op.id for op in graph.operations if not graph.needed_by[op.id]]
    edge_count = sum(len(op.deps) for op in graph.operations)
    return {
        "operations": len(graph.operations),
        "edges": edge_count,
        "roots": roots,
        "leaves": leaves,
        "layers": layers(graph),
    }


def plan(graph: OperationGraph) -> list[dict[str, object]]:
    steps: list[dict[str, object]] = []
    for idx, op_id in enumerate(_topological_ids(graph), start=1):
        op = graph.by_id[op_id]
        steps.append({"step": idx, "id": op.id, "op": op.op, "deps": list(op.deps)})
    return steps


def query(graph: OperationGraph, op_id: str) -> dict[str, object]:
    if op_id not in graph.by_id:
        raise InvalidArgument("--node", op_id, graph.ids(), command="opdag")
    op = graph.by_id[op_id]
    return {
        "id": op.id,
        "op": op.op,
        "deps": list(op.deps),
        "needed_by": list(graph.needed_by[op.id]),
        "ancestors": _reachable_upstream(graph, op.id),
        "descendants": _reachable_downstream(graph, op.id),
        "params": dict(op.params),
    }


def layers(graph: OperationGraph) -> list[list[str]]:
    ranks: dict[str, int] = {}
    for op_id in _topological_ids(graph):
        op = graph.by_id[op_id]
        ranks[op_id] = 0 if not op.deps else 1 + max(ranks[dep] for dep in op.deps)

    out: list[list[str]] = []
    for op in graph.operations:
        rank = ranks[op.id]
        while len(out) <= rank:
            out.append([])
        out[rank].append(op.id)
    return out


def _parse_operation(raw: Any, idx: int) -> Operation:
    if not isinstance(raw, Mapping):
        raise UsageError(f"operation #{idx} must be an object", command="opdag")
    raw_id = raw.get("id")
    if not isinstance(raw_id, str) or not raw_id.strip():
        raise UsageError(
            f"operation #{idx} needs a non-empty string id",
            command="opdag",
            remediation=["Example: {\"id\": \"base\", \"op\": \"cube\"}"],
        )
    raw_op = raw.get("op", "operation")
    if not isinstance(raw_op, str) or not raw_op.strip():
        raise UsageError(f"operation '{raw_id}' has a non-string op", command="opdag")

    raw_deps = raw.get("deps", [])
    if not isinstance(raw_deps, list) or not all(isinstance(dep, str) for dep in raw_deps):
        raise UsageError(
            f"operation '{raw_id}' deps must be a list of operation ids",
            command="opdag",
        )
    if len(set(raw_deps)) != len(raw_deps):
        raise UsageError(
            f"operation '{raw_id}' repeats a dependency",
            command="opdag",
            remediation=["List each dependency id once."],
        )

    raw_params = raw.get("params", {})
    if not isinstance(raw_params, Mapping):
        raise UsageError(f"operation '{raw_id}' params must be an object", command="opdag")

    return Operation(raw_id.strip(), raw_op.strip(), tuple(raw_deps), dict(raw_params))


def _topological_ids(graph: OperationGraph) -> list[str]:
    remaining_deps = {op.id: len(op.deps) for op in graph.operations}
    ready = [op.id for op in graph.operations if remaining_deps[op.id] == 0]
    ordered: list[str] = []

    while ready:
        current = ready.pop(0)
        ordered.append(current)
        for child in graph.needed_by[current]:
            remaining_deps[child] -= 1
            if remaining_deps[child] == 0:
                ready.append(child)

    if len(ordered) != len(graph.operations):
        cycle_ids = [op.id for op in graph.operations if op.id not in ordered]
        raise UsageError(
            f"operation graph contains a dependency cycle involving: {', '.join(cycle_ids)}",
            command="opdag",
            remediation=["Remove at least one dependency edge so every operation can be ordered."],
        )
    return ordered


def _reachable_upstream(graph: OperationGraph, op_id: str) -> list[str]:
    wanted: set[str] = set()

    def visit(current: str) -> None:
        for dep in graph.by_id[current].deps:
            if dep not in wanted:
                wanted.add(dep)
                visit(dep)

    visit(op_id)
    return [candidate for candidate in _topological_ids(graph) if candidate in wanted]


def _reachable_downstream(graph: OperationGraph, op_id: str) -> list[str]:
    wanted: set[str] = set()

    def visit(current: str) -> None:
        for child in graph.needed_by[current]:
            if child not in wanted:
                wanted.add(child)
                visit(child)

    visit(op_id)
    return [candidate for candidate in _topological_ids(graph) if candidate in wanted]
