from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from typing import Any, NoReturn

from errors import InvalidArgument

_NAME = r"[A-Za-z_][A-Za-z0-9_-]*"
_NAME_RE = re.compile(rf"^{_NAME}$")
_ANNOTATION_RE = re.compile(r"//\s*@(?P<kind>[A-Za-z]+)\b(?P<body>.*)$")
_HEX_COLOR_RE = re.compile(r"^#(?:[0-9A-Fa-f]{3}|[0-9A-Fa-f]{6})$")
_ANCHOR_RE = re.compile(
    rf"^\s*(?P<name>{_NAME})\s+"
    r"pos=(?P<pos>\[[^\]]+\])\s+"
    r"dir=(?P<direction>\[[^\]]+\])"
    r"(?:\s+note=(?P<note>\"(?:[^\"\\]|\\.)*\"))?\s*$"
)


@dataclass(frozen=True)
class Anchor:
    name: str
    pos: tuple[float, float, float]
    direction: tuple[float, float, float]
    note: str | None = None
    line: int | None = None


@dataclass(frozen=True)
class ObjectNode:
    index: int
    id: str | None
    classes: tuple[str, ...]
    anchors: tuple[Anchor, ...]
    style: dict[str, str] = field(default_factory=dict)
    line: int | None = None
    code: str | None = None


@dataclass(frozen=True)
class ObjectModel:
    source: str | None
    nodes: tuple[ObjectNode, ...]


@dataclass
class _Pending:
    id: str | None = None
    classes: list[str] = field(default_factory=list)
    anchors: list[Anchor] = field(default_factory=list)
    style: dict[str, str] = field(default_factory=dict)

    def has_data(self) -> bool:
        return bool(self.id or self.classes or self.anchors or self.style)

    def clear(self) -> None:
        self.id = None
        self.classes.clear()
        self.anchors.clear()
        self.style.clear()


def parse_scad_annotations(text: str, *, source: str | None = None) -> ObjectModel:
    """Parse supported `// @...` annotations from OpenSCAD source.

    Standalone annotation comments apply to the next nonblank OpenSCAD code line.
    Inline annotation comments apply to the code on that same line.
    """
    nodes: list[ObjectNode] = []
    pending = _Pending()
    seen_ids: set[str] = set()

    for line_no, raw_line in enumerate(text.splitlines(), start=1):
        code, annotation = _split_annotation(raw_line)
        code_text = code.strip()
        if annotation is not None:
            _apply_annotation(pending, annotation, line_no)
        if code_text.startswith("//"):
            continue
        if code_text and pending.has_data():
            node = _make_node(len(nodes), pending, line_no, code_text, seen_ids)
            nodes.append(node)
            pending.clear()

    return ObjectModel(source=source, nodes=tuple(nodes))


def select_nodes(model: ObjectModel, selector: str) -> list[ObjectNode]:
    parsed = _parse_selector(selector)
    matched: list[ObjectNode] = []
    for node in model.nodes:
        if _matches(node, parsed):
            matched.append(node)
    return matched


def model_to_dict(model: ObjectModel, nodes: list[ObjectNode] | tuple[ObjectNode, ...]) -> dict[str, Any]:
    node_docs = [_node_to_dict(node) for node in nodes]
    anchors: list[dict[str, Any]] = []
    styles: list[dict[str, Any]] = []
    for node in nodes:
        node_ref = node.id if node.id is not None else f"node-{node.index}"
        for anchor in node.anchors:
            anchors.append(_anchor_to_dict(anchor, node_ref))
        if node.style:
            styles.append({"node": node_ref, "style": dict(sorted(node.style.items()))})
    return {
        "source": model.source,
        "nodes": node_docs,
        "anchors": anchors,
        "styles": styles,
    }


def _split_annotation(line: str) -> tuple[str, str | None]:
    comment_at = _comment_start(line)
    if comment_at is None:
        return line, None
    match = _ANNOTATION_RE.search(line, comment_at)
    if match is None:
        return line, None
    return line[:match.start()], f"{match.group('kind')} {match.group('body').strip()}".strip()


def _comment_start(line: str) -> int | None:
    in_string = False
    escaped = False
    for idx, char in enumerate(line):
        if escaped:
            escaped = False
            continue
        if char == "\\" and in_string:
            escaped = True
            continue
        if char == '"':
            in_string = not in_string
            continue
        if not in_string and char == "/" and idx + 1 < len(line) and line[idx + 1] == "/":
            return idx
    return None


def _apply_annotation(pending: _Pending, annotation: str, line_no: int) -> None:
    kind, _, body = annotation.partition(" ")
    if kind == "id":
        value = body.strip()
        _require_name(value, "@id", line_no)
        pending.id = value
        return
    if kind == "class":
        classes = tuple(part for part in body.split() if part)
        if not classes:
            raise _invalid_annotation("@class", body, line_no, "Provide at least one class name.")
        for class_name in classes:
            _require_name(class_name, "@class", line_no)
            if class_name not in pending.classes:
                pending.classes.append(class_name)
        return
    if kind == "anchor":
        pending.anchors.append(_parse_anchor(body, line_no))
        return
    if kind == "color":
        color = body.strip()
        if not (_HEX_COLOR_RE.match(color) or _NAME_RE.match(color)):
            raise _invalid_annotation("@color", body, line_no, "Use a CSS-style name or #RGB/#RRGGBB hex color.")
        pending.style["color"] = color
        return
    raise _invalid_annotation(f"@{kind}", body, line_no, "Supported annotations are @id, @class, @anchor, and @color.")


def _parse_anchor(body: str, line_no: int) -> Anchor:
    match = _ANCHOR_RE.match(body)
    if match is None:
        raise _invalid_annotation(
            "@anchor",
            body,
            line_no,
            'Use: // @anchor <name> pos=[x,y,z] dir=[x,y,z] optional note="...".',
        )
    note_text = match.group("note")
    note: str | None = None
    if note_text is not None:
        try:
            parsed_note = ast.literal_eval(note_text)
        except (SyntaxError, ValueError) as exc:
            raise _invalid_annotation("@anchor", body, line_no, "note must be a valid quoted string.") from exc
        if not isinstance(parsed_note, str):
            raise _invalid_annotation("@anchor", body, line_no, 'note must be a quoted string.')
        note = parsed_note
    return Anchor(
        name=match.group("name"),
        pos=_parse_vector(match.group("pos"), "@anchor pos", line_no),
        direction=_parse_vector(match.group("direction"), "@anchor dir", line_no),
        note=note,
        line=line_no,
    )


def _parse_vector(value: str, flag: str, line_no: int) -> tuple[float, float, float]:
    parts = [part.strip() for part in value.strip()[1:-1].split(",")]
    if len(parts) != 3 or any(part == "" for part in parts):
        raise _invalid_annotation(flag, value, line_no, "Vector values must have exactly three numbers.")
    try:
        x, y, z = (float(part) for part in parts)
    except ValueError as exc:
        raise _invalid_annotation(flag, value, line_no, "Vector values must be numeric.") from exc
    return (x, y, z)


def _make_node(index: int, pending: _Pending, line: int, code: str, seen_ids: set[str]) -> ObjectNode:
    if pending.id is not None:
        if pending.id in seen_ids:
            raise InvalidArgument(
                "@id",
                pending.id,
                ["unique id"],
                command="om",
                extra=f"Duplicate @id before line {line}; ids must be unique within one object model.",
            )
        seen_ids.add(pending.id)
    return ObjectNode(
        index=index,
        id=pending.id,
        classes=tuple(pending.classes),
        anchors=tuple(pending.anchors),
        style=dict(pending.style),
        line=line,
        code=code,
    )


def _node_to_dict(node: ObjectNode) -> dict[str, Any]:
    return {
        "index": node.index,
        "id": node.id,
        "classes": list(node.classes),
        "line": node.line,
        "code": node.code,
        "anchors": [_anchor_to_dict(anchor, None) for anchor in node.anchors],
        "style": dict(sorted(node.style.items())),
    }


def _anchor_to_dict(anchor: Anchor, node_ref: str | None) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "name": anchor.name,
        "pos": list(anchor.pos),
        "dir": list(anchor.direction),
        "note": anchor.note,
        "line": anchor.line,
    }
    if node_ref is not None:
        doc["node"] = node_ref
    return doc


def _require_name(value: str, flag: str, line_no: int) -> None:
    if not _NAME_RE.match(value):
        raise _invalid_annotation(flag, value, line_no, "Use letters, digits, underscore, and dash; start with a letter or underscore.")


def _invalid_annotation(flag: str, got: str, line_no: int, extra: str) -> InvalidArgument:
    return InvalidArgument(
        flag,
        got,
        ["// @id <id>", "// @class <class...>", '// @anchor <name> pos=[x,y,z] dir=[x,y,z] note="..."', "// @color <name-or-hex>"],
        command="om",
        extra=f"Line {line_no}: {extra}",
    )


@dataclass(frozen=True)
class _Selector:
    id: str | None
    classes: tuple[str, ...]


def _parse_selector(selector: str) -> _Selector:
    expr = selector.strip()
    if not expr:
        raise InvalidArgument(
            "selector",
            selector,
            ["#id", ".class", ".class.other"],
            command="om",
            extra="Provide a selector such as #body or .structural.removable.",
        )
    if any(char.isspace() for char in expr):
        raise InvalidArgument(
            "selector",
            selector,
            ["#id", ".class", ".class.other"],
            command="om",
            extra="Descendant selectors are reserved for the future object-model tree; this slice supports only flat #id, .class, and .a.b selectors.",
        )
    if "(" in expr or ")" in expr:
        raise InvalidArgument(
            "selector",
            selector,
            ["#id", ".class", ".class.other"],
            command="om",
            extra="Transform/query operations are not implemented yet; pass a raw selector such as #body or .structural.removable.",
        )
    if expr.startswith("#"):
        ident = expr[1:]
        _require_selector_name(ident, selector)
        return _Selector(id=ident, classes=())
    if expr.startswith("."):
        parts = expr.split(".")
        classes = tuple(parts[1:])
        if not classes or any(part == "" for part in classes):
            _raise_bad_selector(selector)
        for class_name in classes:
            _require_selector_name(class_name, selector)
        return _Selector(id=None, classes=classes)
    _raise_bad_selector(selector)


def _require_selector_name(value: str, selector: str) -> None:
    if not _NAME_RE.match(value):
        _raise_bad_selector(selector)


def _raise_bad_selector(selector: str) -> NoReturn:
    raise InvalidArgument(
        "selector",
        selector,
        ["#id", ".class", ".class.other"],
        command="om",
        extra="Supported selectors are #id, .class, and combined class selectors like .structural.removable.",
    )


def _matches(node: ObjectNode, selector: _Selector) -> bool:
    if selector.id is not None:
        return node.id == selector.id
    node_classes = set(node.classes)
    return all(class_name in node_classes for class_name in selector.classes)
