"""Focused regressions for command help / docs sync."""
from __future__ import annotations

from pathlib import Path

from commands import render


ROOT = Path(__file__).resolve().parents[1]


def _doc(name: str) -> str:
    return (ROOT / "docs" / "commands" / f"{name}.md").read_text(encoding="utf-8")


def test_render_usage_keeps_section_headers_and_options_aligned() -> None:
    assert "\n  --colorscheme NAME" in render.USAGE
    assert "\n  --render" in render.USAGE
    assert "\nSection options:" in render.USAGE
    assert "\n   --colorscheme NAME" not in render.USAGE
    assert "\n   Section options:" not in render.USAGE


def test_updated_help_examples_are_reflected_in_command_docs() -> None:
    expected = {
        "doctor": ("3d doctor | grep MISSING", "run before CI"),
        "multi": ("3d multi bracket.scad --render --size 1200x900", "3d multi bracket.scad -D 'depth=40'"),
        "overlay": ("3d overlay preview.png photo.jpg", "3d overlay render.png ref.jpg -o diff/"),
        "params": ("jq '.[] | {name, value}'", "3d params bracket.scad --json > params.json"),
        "validate": ("3d validate bracket.scad", "fast CI check"),
    }
    for name, snippets in expected.items():
        text = _doc(name)
        for snippet in snippets:
            assert snippet in text, f"{snippet!r} missing from docs/commands/{name}.md"
