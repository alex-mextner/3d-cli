"""Unit tests for config.py — collision config loader."""
from __future__ import annotations

import json
import pathlib


from config import load


def test_load_full_config(tmp_path: pathlib.Path) -> None:
    cfg = {
        "pair_scad": "pair.scad",
        "parts": ["a", "b"],
        "phases": [{"phase": 0, "name": "home"}, {"phase": 1, "name": "fwd"}],
        "intended": [["a", "b"]],
        "eps_mm3": 1.0,
        "touch_tol_mm": 0.1,
        "contact_max_mm3": 5.0,
        "frame_check": {
            "timeline": "timeline.py",
            "timeline_fn": "pose",
            "frames": 40,
            "pose_phase_sentinel": -1,
            "pose_vars": {"x": "X_POS"},
        },
        "viz": {
            "outdir": "../previews",
            "name_prefix": "collision",
        },
    }
    p = tmp_path / "collision.json"
    p.write_text(json.dumps(cfg))
    (tmp_path / "pair.scad").write_text("")
    (tmp_path / "timeline.py").write_text("")
    c = load(str(p))
    assert c.config_path == p.resolve()
    assert c.parts == ["a", "b"]
    assert c.phases == {0: "home", 1: "fwd"}
    assert c.intended == {frozenset(["a", "b"])}
    assert c.eps == 1.0
    assert c.frame is not None
    assert c.frame.timeline_fn == "pose"
    assert c.frame.frames == 40
    assert c.viz is not None
    assert c.viz.name_prefix == "collision"


def test_load_minimal_config(tmp_path: pathlib.Path) -> None:
    cfg = {
        "pair_scad": "pair.scad",
        "parts": ["a"],
        "phases": [{"phase": 0, "name": "home"}],
        "intended": [],
        "eps_mm3": 0.5,
        "touch_tol_mm": 0.05,
        "contact_max_mm3": 2.0,
    }
    p = tmp_path / "collision.json"
    p.write_text(json.dumps(cfg))
    (tmp_path / "pair.scad").write_text("")
    c = load(str(p))
    assert c.frame is None
    assert c.viz is None


def test_load_path_is_pathlib(tmp_path: pathlib.Path) -> None:
    cfg = {
        "pair_scad": "pair.scad",
        "parts": ["a"],
        "phases": [{"phase": 0, "name": "home"}],
        "intended": [],
        "eps_mm3": 0.5,
        "touch_tol_mm": 0.05,
        "contact_max_mm3": 2.0,
    }
    p = tmp_path / "collision.json"
    p.write_text(json.dumps(cfg))
    (tmp_path / "pair.scad").write_text("")
    c = load(p)
    assert c.config_path == p.resolve()
