"""Additional unit tests for web.server — FastAPI endpoints."""
from __future__ import annotations

import pathlib
from typing import Any

import pytest

from web import webconfig
from web.server import create_app, _safe_scad, _parse_defines, _sse


def test_safe_scad_ok(tmp_path: pathlib.Path) -> None:
    f = tmp_path / "model.scad"
    f.write_text("cube(1);")
    assert _safe_scad(str(f), tmp_path) == f


def test_safe_scad_outside_root(tmp_path: pathlib.Path) -> None:
    f = tmp_path / "model.scad"
    f.write_text("cube(1);")
    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        _safe_scad("/etc/passwd", tmp_path)


def test_safe_scad_missing(tmp_path: pathlib.Path) -> None:
    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        _safe_scad(str(tmp_path / "nope.scad"), tmp_path)


def test_parse_defines() -> None:
    assert _parse_defines(None) == {}
    assert _parse_defines(["a=1", "b=2"]) == {"a": "1", "b": "2"}
    assert _parse_defines(["noequals"]) == {}


def test_sse() -> None:
    assert _sse({"a": 1}) == "data: {\"a\": 1}\n\n"
    assert _sse({"a": 1}, event="ev") == "event: ev\ndata: {\"a\": 1}\n\n"


def test_api_config(tmp_path: pathlib.Path) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    cfg = webconfig.WebConfig(project_root=str(tmp_path), port=1234, host="127.0.0.1")
    app = create_app(cfg)
    with TestClient(app) as c:
        r = c.get("/api/config")
        assert r.status_code == 200
        assert r.json()["project_root"] == str(tmp_path)


def test_api_projects(tmp_path: pathlib.Path) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    (tmp_path / "p1").mkdir()
    (tmp_path / "p1" / "model.scad").write_text("cube(1);")
    cfg = webconfig.WebConfig(project_root=str(tmp_path), port=1234, host="127.0.0.1")
    app = create_app(cfg)
    with TestClient(app) as c:
        r = c.get("/api/projects")
        assert r.status_code == 200
        names = [p["name"] for p in r.json()["projects"]]
        assert "p1" in names


def test_api_project(tmp_path: pathlib.Path) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    (tmp_path / "p1").mkdir()
    (tmp_path / "p1" / "model.scad").write_text("cube(1);")
    cfg = webconfig.WebConfig(project_root=str(tmp_path), port=1234, host="127.0.0.1")
    app = create_app(cfg)
    with TestClient(app) as c:
        r = c.get("/api/project", params={"path": str(tmp_path / "p1")})
        assert r.status_code == 200
        assert r.json()["name"] == "p1"


def test_api_project_outside_root(tmp_path: pathlib.Path) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    cfg = webconfig.WebConfig(project_root=str(tmp_path), port=1234, host="127.0.0.1")
    app = create_app(cfg)
    with TestClient(app) as c:
        r = c.get("/api/project", params={"path": "/etc"})
        assert r.status_code == 403


def test_api_project_not_found(tmp_path: pathlib.Path) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    cfg = webconfig.WebConfig(project_root=str(tmp_path), port=1234, host="127.0.0.1")
    app = create_app(cfg)
    with TestClient(app) as c:
        r = c.get("/api/project", params={"path": str(tmp_path / "p1")})
        assert r.status_code == 404


def test_api_spec(tmp_path: pathlib.Path) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    (tmp_path / "p1").mkdir()
    spec = tmp_path / "p1" / "SPEC.md"
    spec.write_text("# Title\n\nhello")
    cfg = webconfig.WebConfig(project_root=str(tmp_path), port=1234, host="127.0.0.1")
    app = create_app(cfg)
    with TestClient(app) as c:
        r = c.get("/api/spec", params={"path": str(spec)})
        assert r.status_code == 200
        assert "Title" in r.text


def test_api_spec_raw_when_markdown_missing(monkeypatch: Any, tmp_path: pathlib.Path) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    (tmp_path / "p1").mkdir()
    spec = tmp_path / "p1" / "SPEC.md"
    spec.write_text("raw text")
    cfg = webconfig.WebConfig(project_root=str(tmp_path), port=1234, host="127.0.0.1")
    app = create_app(cfg)
    with TestClient(app) as c:
        r = c.get("/api/spec", params={"path": str(spec)})
        assert r.status_code == 200
        assert "raw text" in r.text


def test_api_constants(tmp_path: pathlib.Path) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    (tmp_path / "p1").mkdir()
    scad = tmp_path / "p1" / "constants.scad"
    scad.write_text("x = 1;\n")
    cfg = webconfig.WebConfig(project_root=str(tmp_path), port=1234, host="127.0.0.1")
    app = create_app(cfg)
    with TestClient(app) as c:
        r = c.get("/api/constants", params={"path": str(scad)})
        assert r.status_code == 200
        assert r.json()["params"][0]["name"] == "x"


def test_api_constants_apply(tmp_path: pathlib.Path) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    (tmp_path / "p1").mkdir()
    scad = tmp_path / "p1" / "constants.scad"
    scad.write_text("x = 1;\n")
    cfg = webconfig.WebConfig(project_root=str(tmp_path), port=1234, host="127.0.0.1")
    app = create_app(cfg)
    with TestClient(app) as c:
        r = c.post("/api/constants/apply", json={"path": str(scad), "changes": {"x": "2"}})
        assert r.status_code == 200
        assert r.json()["applied"]["x"] == "2"


def test_api_colors_no_yaml(tmp_path: pathlib.Path) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    (tmp_path / "p1").mkdir()
    cfg = webconfig.WebConfig(project_root=str(tmp_path), port=1234, host="127.0.0.1")
    app = create_app(cfg)
    with TestClient(app) as c:
        r = c.get("/api/colors", params={"path": str(tmp_path / "p1")})
        assert r.status_code == 200
        assert r.json()["colors"] == {}


def test_api_colors_with_yaml(tmp_path: pathlib.Path) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    (tmp_path / "p1").mkdir()
    yml = tmp_path / "p1" / "3d.yaml"
    yml.write_text("colors:\n  a: '#ff0000'\n")
    cfg = webconfig.WebConfig(project_root=str(tmp_path), port=1234, host="127.0.0.1")
    app = create_app(cfg)
    with TestClient(app) as c:
        r = c.get("/api/colors", params={"path": str(tmp_path / "p1")})
        assert r.status_code == 200
        assert r.json()["colors"]["a"] == "#ff0000"


def test_api_colors_set(tmp_path: pathlib.Path) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    (tmp_path / "p1").mkdir()
    cfg = webconfig.WebConfig(project_root=str(tmp_path), port=1234, host="127.0.0.1")
    app = create_app(cfg)
    with TestClient(app) as c:
        r = c.post("/api/colors", json={"path": str(tmp_path / "p1"), "colors": {"a": "#00ff00"}})
        assert r.status_code == 200
        assert r.json()["ok"] is True


def test_api_overlays(tmp_path: pathlib.Path) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    (tmp_path / "p1").mkdir()
    (tmp_path / "p1" / "previews").mkdir()
    (tmp_path / "p1" / "previews" / "a.png").write_text("")
    cfg = webconfig.WebConfig(project_root=str(tmp_path), port=1234, host="127.0.0.1")
    app = create_app(cfg)
    with TestClient(app) as c:
        r = c.get("/api/overlays", params={"path": str(tmp_path / "p1")})
        assert r.status_code == 200
        assert len(r.json()["overlays"]) == 1


def test_api_animations(tmp_path: pathlib.Path) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    (tmp_path / "p1").mkdir()
    (tmp_path / "p1" / "anim.gif").write_text("")
    (tmp_path / "p1" / "3d.yaml").write_text("name: p1\n")
    cfg = webconfig.WebConfig(project_root=str(tmp_path), port=1234, host="127.0.0.1")
    app = create_app(cfg)
    with TestClient(app) as c:
        r = c.get("/api/animations", params={"path": str(tmp_path / "p1")})
        assert r.status_code == 200
        assert str(tmp_path / "p1" / "anim.gif") in r.json()["animations"]


def test_api_animation_file(tmp_path: pathlib.Path) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    (tmp_path / "p1").mkdir()
    gif = tmp_path / "p1" / "anim.gif"
    gif.write_text("fake")
    cfg = webconfig.WebConfig(project_root=str(tmp_path), port=1234, host="127.0.0.1")
    app = create_app(cfg)
    with TestClient(app) as c:
        r = c.get("/api/animation", params={"path": str(gif)})
        assert r.status_code == 200


def test_api_render_sse(tmp_path: pathlib.Path, monkeypatch: Any) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    (tmp_path / "p1").mkdir()
    scad = tmp_path / "p1" / "model.scad"
    scad.write_text("cube(1);")
    cfg = webconfig.WebConfig(project_root=str(tmp_path), port=1234, host="127.0.0.1")
    app = create_app(cfg)
    async def _fake_export_stl(scad, state, **kw):
        return pathlib.Path("/tmp/fake.stl")
    monkeypatch.setattr("web.render_service.export_stl", _fake_export_stl)
    with TestClient(app) as c:
        r = c.get("/api/render-sse", params={"path": str(scad)})
        assert r.status_code == 200
        assert "render" in r.text


def test_api_render_sse_error(tmp_path: pathlib.Path, monkeypatch: Any) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    (tmp_path / "p1").mkdir()
    scad = tmp_path / "p1" / "model.scad"
    scad.write_text("cube(1);")
    cfg = webconfig.WebConfig(project_root=str(tmp_path), port=1234, host="127.0.0.1")
    app = create_app(cfg)
    async def fail(*a, **kw):
        from web.render_service import RenderError
        raise RenderError("boom")
    monkeypatch.setattr("web.render_service.export_stl", fail)
    with TestClient(app) as c:
        r = c.get("/api/render-sse", params={"path": str(scad)})
        assert r.status_code == 200
        assert "error" in r.text


def test_api_agents_tail(monkeypatch: Any, tmp_path: pathlib.Path) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    cfg = webconfig.WebConfig(project_root=str(tmp_path), port=1234, host="127.0.0.1")
    app = create_app(cfg)
    with TestClient(app) as c:
        r = c.post("/api/agents/tail", json={"key": "raw:1", "from_start": True})
        assert r.status_code == 200


def test_index_no_static(monkeypatch: Any, tmp_path: pathlib.Path) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    cfg = webconfig.WebConfig(project_root=str(tmp_path), port=1234, host="127.0.0.1")
    app = create_app(cfg)
    with TestClient(app) as c:
        r = c.get("/")
        assert r.status_code == 200
        assert "3d web" in r.text
