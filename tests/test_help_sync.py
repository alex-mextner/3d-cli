"""Focused regressions for command help / docs sync."""
from __future__ import annotations

import importlib
from pathlib import Path
from typing import cast


ROOT = Path(__file__).resolve().parents[1]


def _doc(name: str) -> str:
    return (ROOT / "docs" / "commands" / f"{name}.md").read_text(encoding="utf-8")


def test_render_usage_keeps_section_headers_and_options_aligned() -> None:
    usage = cast(str, getattr(importlib.import_module("commands.render"), "USAGE"))
    assert "\n  --colorscheme NAME" in usage
    assert "\n  --render" in usage
    assert "\nSection options:" in usage
    assert "\n   --colorscheme NAME" not in usage
    assert "\n   Section options:" not in usage


def test_updated_help_examples_are_reflected_in_command_docs() -> None:
    expected = {
        "doctor": ("3d doctor | grep MISSING", "run before CI"),
        "multi": ("3d multi bracket.scad --render --size 1200x900", "3d multi bracket.scad -D 'depth=40'"),
        "overlay": ("3d overlay preview.png photo.jpg", "3d overlay render.png ref.jpg -o diff/"),
        "pack": ("3d pack --bed 220x220 --part bracket=60x40", "jq '.placements | length'"),
        "params": ("jq '.[] | {name, value}'", "3d params bracket.scad --json > params.json"),
        "procurement": (
            "3d procurement plan --bom bom.yaml --inventory inventory.yaml",
            "jq -r '.items[].sku'",
        ),
        "print": ("3d print part.stl --printer \"Prusa MK4\" --dry-run", "print-plan.json"),
        "validate": ("3d validate bracket.scad", "fast CI check"),
        "workspaces": ("3d workspaces show shop --json > workspace.json", "3d workspaces list --json |"),
    }
    for name, snippets in expected.items():
        text = _doc(name)
        for snippet in snippets:
            assert snippet in text, f"{snippet!r} missing from docs/commands/{name}.md"


def test_reference_backplate_workflow_documents_existing_command_story() -> None:
    text = (ROOT / "docs" / "workflows" / "reference-backplate.md").read_text(
        encoding="utf-8"
    )
    required_snippets = (
        "3d preprocess refs/bracket-side.jpg -o work/backplate/",
        "3d fit-camera model.scad refs/bracket-side.jpg",
        "CAM=\"$(jq -r .camera_arg work/backplate/camera.json)\"",
        "3d render model.scad",
        "3d overlay work/backplate/render-001.png refs/bracket-side.jpg",
        "3d silhouette model.scad",
        "3d score work/backplate/model-mask-001.png work/backplate/mask.png",
        "3d compare model.scad refs/bracket-side.jpg",
        "Remaining Gaps",
    )
    for snippet in required_snippets:
        assert snippet in text, f"{snippet!r} missing from reference backplate workflow"
