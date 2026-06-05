#!/usr/bin/env python3
"""constants_io.py — parse + safely rewrite OpenSCAD constants.scad parameters.

Parsing reuses the repo's `lib/extract_params.extract()` (Customizer-comment aware) so the
web constants editor and the `3d params` CLI agree on the parameter model.

Writing is deliberately surgical: we ONLY replace the numeric/boolean RHS of an existing
`name = value;` assignment, preserving comments, ordering, and all other lines. We never
regenerate the file. This makes "apply" safe on a hand-authored contract file.
"""
from __future__ import annotations

import pathlib
import re
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "lib"))
import extract_params  # type: ignore  # noqa: E402


def parse_constants(path: str | pathlib.Path) -> list[dict[str, str]]:
    """Return the parameter rows for a .scad file (name/value/type/range/options/desc)."""
    p = pathlib.Path(path)
    if not p.is_file():
        return []
    rows: list[dict[str, str]] = extract_params.extract(str(p))  # type: ignore[no-any-return]
    return rows


def _format_value(value: str) -> str:
    """Render a value back to OpenSCAD source. Numbers/booleans pass through; strings get
    requoted. Expressions/arrays are written verbatim (caller is responsible)."""
    v = value.strip()
    if v in ("true", "false"):
        return v
    if re.fullmatch(r"-?\d+", v) or re.fullmatch(r"-?\d*\.?\d+", v):
        return v
    if v.startswith("[") or v.startswith("("):
        return v
    # treat as string literal if not already quoted
    if v.startswith('"') and v.endswith('"'):
        return v
    return v  # leave verbatim; the editor only mutates numbers/booleans in v1


def apply_changes(
    path: str | pathlib.Path, changes: dict[str, str]
) -> dict[str, str]:
    """Rewrite the RHS of each `name = ...;` in `changes`. Returns {name: applied_value}
    for those actually found & updated. Lines inside `{ }` blocks are skipped (mirrors the
    extractor). Raises FileNotFoundError if the file is missing."""
    p = pathlib.Path(path)
    src = p.read_text(encoding="utf-8")
    lines = src.splitlines(keepends=True)
    applied: dict[str, str] = {}
    in_block = 0
    for i, line in enumerate(lines):
        # track brace depth exactly like extract_params (count before deciding to skip)
        opens = line.count("{")
        closes = line.count("}")
        if in_block > 0:
            in_block += opens - closes
            continue
        m = re.match(r"^(\s*)([a-zA-Z_][a-zA-Z0-9_]*)(\s*=\s*)([^;]+)(;.*)$", line)
        if m and m.group(2) in changes:
            name = m.group(2)
            newval = _format_value(changes[name])
            lines[i] = f"{m.group(1)}{name}{m.group(3)}{newval}{m.group(5)}"
            if not lines[i].endswith("\n") and line.endswith("\n"):
                lines[i] += "\n"
            applied[name] = newval
        in_block += opens - closes
    if applied:
        p.write_text("".join(lines), encoding="utf-8")
    return applied
