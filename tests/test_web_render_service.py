"""Unit tests for web.render_service — async OpenSCAD export/render."""
from __future__ import annotations

import asyncio
import pathlib
from typing import Any

import pytest

from web import render_service


def test_cache_dir(tmp_path: pathlib.Path) -> None:
    scad = tmp_path / "model.scad"
    scad.write_text("cube(1);")
    d = render_service._cache_dir(tmp_path, scad)
    assert d.is_dir()
    assert scad.stem in d.name


def test_defines_suffix() -> None:
    assert render_service._defines_suffix(None) == ""
    assert render_service._defines_suffix({"a": "1"}).startswith("-")
    assert render_service._defines_suffix({"a": "1", "b": "2"}) == render_service._defines_suffix({"b": "2", "a": "1"})


def test_export_stl_missing_file(monkeypatch: Any, tmp_path: pathlib.Path) -> None:
    scad = tmp_path / "model.scad"
    with pytest.raises(render_service.RenderError):
        asyncio.run(render_service.export_stl(scad, tmp_path))


def test_export_stl_success(monkeypatch: Any, tmp_path: pathlib.Path) -> None:
    scad = tmp_path / "model.scad"
    scad.write_text("cube(1);")
    out = tmp_path / "model.stl"
    out.write_text("fake")
    monkeypatch.setattr(render_service, "_cache_dir", lambda state_dir, scad: tmp_path)

    async def fake_run(cmd, **kw):
        return 0, "ok"

    monkeypatch.setattr(render_service, "_run", fake_run)
    monkeypatch.setattr(render_service, "_openscad", lambda: "/usr/bin/openscad")
    result = asyncio.run(render_service.export_stl(scad, tmp_path))
    assert result.suffix == ".stl"


def test_export_stl_fail(monkeypatch: Any, tmp_path: pathlib.Path) -> None:
    scad = tmp_path / "model.scad"
    scad.write_text("cube(1);")

    async def fake_run(cmd, **kw):
        return 1, "error"

    monkeypatch.setattr(render_service, "_run", fake_run)
    monkeypatch.setattr(render_service, "_openscad", lambda: "/usr/bin/openscad")
    with pytest.raises(render_service.RenderError):
        asyncio.run(render_service.export_stl(scad, tmp_path))


def test_run_timeout(monkeypatch: Any, tmp_path: pathlib.Path) -> None:
    import asyncio as aio
    async def fake_create(*a, **kw):
        class P:
            async def communicate(self):
                await aio.sleep(1000)
                return b"", b""
            async def wait(self):
                pass
            def kill(self):
                pass
            returncode = 0
        return P()
    monkeypatch.setattr(aio, "create_subprocess_exec", fake_create)
    with pytest.raises(render_service.RenderError):
        asyncio.run(render_service._run(["echo", "hi"], timeout=0.01))


def test_render_view_success(monkeypatch: Any, tmp_path: pathlib.Path) -> None:
    scad = tmp_path / "model.scad"
    scad.write_text("cube(1);")
    out = tmp_path / "model-iso.png"
    out.write_text("fake")
    monkeypatch.setattr(render_service, "_cache_dir", lambda state_dir, scad: tmp_path)

    async def fake_run(cmd, **kw):
        return 0, "ok"

    monkeypatch.setattr(render_service, "_run", fake_run)
    result = asyncio.run(render_service.render_view(scad, tmp_path))
    assert result.suffix == ".png"


def test_render_view_fail(monkeypatch: Any, tmp_path: pathlib.Path) -> None:
    scad = tmp_path / "model.scad"
    scad.write_text("cube(1);")

    async def fake_run(cmd, **kw):
        return 1, "error"

    monkeypatch.setattr(render_service, "_run", fake_run)
    with pytest.raises(render_service.RenderError):
        asyncio.run(render_service.render_view(scad, tmp_path))
