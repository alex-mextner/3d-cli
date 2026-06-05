#!/usr/bin/env python3
"""server.py — the FastAPI app for `3d web`: an interactive 3D-project dashboard.

Async throughout. Serves a vanilla-JS SPA (static/) and a JSON+SSE API:

  GET  /                              the SPA
  GET  /api/config                    current web config
  GET  /api/projects                  scanned projects under project_root
  GET  /api/project?path=…            one project (detail)
  GET  /api/spec?path=…               SPEC.md rendered to HTML
  GET  /api/constants?path=…          parsed constants.scad parameters
  POST /api/constants/apply           write changed constants back to disk
  GET  /api/colors?path=…             per-part colors from 3d.yaml (or {} )
  POST /api/colors                    write colors to 3d.yaml
  GET  /api/model.stl?path=…[&D…]     export a .scad to STL (cached) for three.js
  GET  /api/preview?path=…&kind=…     a preview/overlay PNG by name (analytical layers)
  GET  /api/animations?path=…         list animation files for a project
  GET  /api/animation?path=…          stream one animation file
  GET  /api/render-sse?path=…[&D…]    SSE: re-render progress + STL-ready (param changes)
  GET  /api/agents                    discovered agent sessions (+ project association)
  GET  /api/agents/sse                SSE: live normalized agent events

Graceful degradation: missing SPEC / 3d.yaml / animations / log sources never 500 — the
endpoint returns an empty/typed result and the UI adapts.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import pathlib
from collections.abc import AsyncIterator
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles

from . import constants_io, render_service, scan, webconfig
from .adapters import ALL_ADAPTERS
from .agent_manager import AgentManager

STATIC_DIR = pathlib.Path(__file__).resolve().parent / "static"


def _safe_scad(path: str, root: pathlib.Path) -> pathlib.Path:
    """Resolve a user-supplied path and confine it under the project root."""
    p = pathlib.Path(path).expanduser().resolve()
    try:
        p.relative_to(root)
    except ValueError as e:
        raise HTTPException(403, f"path outside project root: {p}") from e
    if not p.exists():
        raise HTTPException(404, f"not found: {p}")
    return p


def _parse_defines(items: list[str] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for it in items or []:
        if "=" in it:
            k, v = it.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def _sse(data: dict[str, Any], event: str | None = None) -> str:
    head = f"event: {event}\n" if event else ""
    return f"{head}data: {json.dumps(data)}\n\n"


def create_app(cfg: webconfig.WebConfig) -> FastAPI:
    root = pathlib.Path(cfg.project_root).expanduser().resolve()
    state = webconfig.state_dir()

    adapters = [cls() for cls in ALL_ADAPTERS]
    projects = scan.scan_projects(root)
    manager = AgentManager(
        adapters, [p.path for p in projects], inactive_after=30.0
    )

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        task = asyncio.create_task(manager.monitor(interval=6.0))
        try:
            yield
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            await manager.stop()

    app = FastAPI(title="3d web", version="0.1.0", lifespan=lifespan)
    app.state.cfg = cfg
    app.state.root = root
    app.state.manager = manager

    # ---- meta -----------------------------------------------------------
    @app.get("/api/config")
    async def api_config() -> JSONResponse:
        return JSONResponse({"project_root": str(root), "port": cfg.port, "host": cfg.host})

    @app.get("/api/projects")
    async def api_projects() -> JSONResponse:
        ps = scan.scan_projects(root)
        manager.set_projects([p.path for p in ps])
        return JSONResponse({"project_root": str(root), "projects": [p.to_dict() for p in ps]})

    @app.get("/api/project")
    async def api_project(path: str = Query(...)) -> JSONResponse:
        d = pathlib.Path(path).expanduser().resolve()
        try:
            d.relative_to(root)
        except ValueError as e:
            raise HTTPException(403, "outside root") from e
        for p in scan.scan_projects(root):
            if p.path == str(d):
                return JSONResponse(p.to_dict())
        raise HTTPException(404, "project not found")

    # ---- spec -----------------------------------------------------------
    @app.get("/api/spec", response_class=HTMLResponse)
    async def api_spec(path: str = Query(...)) -> HTMLResponse:
        p = _safe_scad(path, root)
        try:
            import markdown  # type: ignore
            html = markdown.markdown(
                p.read_text(encoding="utf-8"),
                extensions=["fenced_code", "tables", "toc"],
            )
        except Exception:
            # graceful: serve raw text in a <pre> if markdown is unavailable
            raw = p.read_text(encoding="utf-8", errors="replace")
            html = f"<pre>{raw}</pre>"
        return HTMLResponse(html)

    # ---- constants ------------------------------------------------------
    @app.get("/api/constants")
    async def api_constants(path: str = Query(...)) -> JSONResponse:
        p = _safe_scad(path, root)
        return JSONResponse({"path": str(p), "params": constants_io.parse_constants(p)})

    @app.post("/api/constants/apply")
    async def api_constants_apply(req: Request) -> JSONResponse:
        body = await req.json()
        p = _safe_scad(str(body.get("path", "")), root)
        changes = {str(k): str(v) for k, v in (body.get("changes") or {}).items()}
        applied = constants_io.apply_changes(p, changes)
        return JSONResponse({"applied": applied})

    # ---- colors / materials (3d.yaml) -----------------------------------
    @app.get("/api/colors")
    async def api_colors(path: str = Query(...)) -> JSONResponse:
        d = pathlib.Path(path).expanduser().resolve()
        try:
            d.relative_to(root)
        except ValueError as e:
            raise HTTPException(403, "outside root") from e
        yml = None
        for name in ("3d.yaml", "3d.yml"):
            if (d / name).is_file():
                yml = d / name
                break
        if yml is None:
            return JSONResponse({"colors": {}, "yaml": None})
        try:
            import yaml  # type: ignore
            data = yaml.safe_load(yml.read_text(encoding="utf-8")) or {}
            colors = data.get("colors", {}) if isinstance(data, dict) else {}
        except Exception:
            colors = {}
        return JSONResponse({"colors": colors, "yaml": str(yml)})

    @app.post("/api/colors")
    async def api_colors_set(req: Request) -> JSONResponse:
        body = await req.json()
        d = pathlib.Path(str(body.get("path", ""))).expanduser().resolve()
        try:
            d.relative_to(root)
        except ValueError as e:
            raise HTTPException(403, "outside root") from e
        colors = body.get("colors", {})
        try:
            import yaml  # type: ignore
        except Exception:
            return JSONResponse({"ok": False, "reason": "pyyaml not installed"}, status_code=200)
        yml = d / "3d.yaml"
        data: dict[str, Any] = {}
        if yml.is_file():
            try:
                data = yaml.safe_load(yml.read_text(encoding="utf-8")) or {}
            except Exception:
                data = {}
        data["colors"] = colors
        yml.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
        return JSONResponse({"ok": True, "yaml": str(yml)})

    # ---- model STL + previews -------------------------------------------
    @app.get("/api/model.stl")
    async def api_model(
        path: str = Query(...), D: list[str] | None = Query(default=None)
    ) -> FileResponse:
        p = _safe_scad(path, root)
        try:
            stl = await render_service.export_stl(p, state, defines=_parse_defines(D))
        except render_service.RenderError as e:
            raise HTTPException(422, str(e)) from e
        return FileResponse(str(stl), media_type="model/stl", filename=stl.name)

    @app.get("/api/preview")
    async def api_preview(path: str = Query(...)) -> FileResponse:
        p = _safe_scad(path, root)
        return FileResponse(str(p), media_type="image/png")

    @app.get("/api/overlays")
    async def api_overlays(path: str = Query(...)) -> JSONResponse:
        """Enumerate analytical-layer PNGs (silhouette/score/collision/debug renders) in a
        project's previews/ dir, so the viewer can offer them as comparable overlays."""
        d = pathlib.Path(path).expanduser().resolve()
        try:
            d.relative_to(root)
        except ValueError as e:
            raise HTTPException(403, "outside root") from e
        overlays: list[str] = []
        for sub in ("previews", "match", "verify"):
            pd = d / sub
            if pd.is_dir():
                overlays += [str(f) for f in sorted(pd.rglob("*.png"))]
        return JSONResponse({"overlays": overlays[:200]})

    # ---- animations -----------------------------------------------------
    @app.get("/api/animations")
    async def api_animations(path: str = Query(...)) -> JSONResponse:
        d = pathlib.Path(path).expanduser().resolve()
        for proj in scan.scan_projects(root):
            if proj.path == str(d):
                return JSONResponse({"animations": proj.animations})
        return JSONResponse({"animations": []})

    @app.get("/api/animation")
    async def api_animation(path: str = Query(...)) -> FileResponse:
        p = _safe_scad(path, root)
        mt = {"mp4": "video/mp4", "webm": "video/webm", "mov": "video/quicktime",
              "gif": "image/gif"}.get(p.suffix.lstrip(".").lower(), "application/octet-stream")
        return FileResponse(str(p), media_type=mt)

    # ---- render SSE (param-change re-render progress) -------------------
    @app.get("/api/render-sse")
    async def api_render_sse(
        path: str = Query(...), D: list[str] | None = Query(default=None)
    ) -> StreamingResponse:
        p = _safe_scad(path, root)
        defines = _parse_defines(D)

        async def gen() -> AsyncIterator[str]:
            yield _sse({"phase": "start", "file": str(p)}, event="render")
            try:
                stl = await render_service.export_stl(p, state, defines=defines)
                yield _sse(
                    {"phase": "done", "stl": str(stl),
                     "url": f"/api/model.stl?path={p}" + "".join(f"&D={k}={v}" for k, v in defines.items())},
                    event="render",
                )
            except render_service.RenderError as e:
                yield _sse({"phase": "error", "error": str(e)}, event="render")

        return StreamingResponse(gen(), media_type="text/event-stream")

    # ---- agents ---------------------------------------------------------
    @app.get("/api/agents")
    async def api_agents() -> JSONResponse:
        await manager.refresh()
        sessions = sorted(manager.sessions(), key=lambda t: t.ref.mtime, reverse=True)
        out: list[dict[str, Any]] = [
            {
                "key": f"{ts.ref.source}:{ts.ref.session_id}",
                "source": ts.ref.source,
                "session_id": ts.ref.session_id,
                "label": ts.ref.label,
                "cwd": ts.ref.cwd,
                "project": ts.project_path,
                "active": ts.active,
                "events": ts.event_count,
                "mtime": ts.ref.mtime,
            }
            for ts in sessions[:200]
        ]
        return JSONResponse({"sessions": out})

    @app.get("/api/agents/sse")
    async def api_agents_sse(request: Request) -> StreamingResponse:
        await manager.refresh()

        async def gen() -> AsyncIterator[str]:
            yield _sse({"phase": "connected"}, event="agent")
            agen = manager.subscribe()
            try:
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        ev = await asyncio.wait_for(agen.__anext__(), timeout=15.0)
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"  # heartbeat; also unblocks disconnect check
                        continue
                    yield _sse(ev.to_dict(), event="agent")
            finally:
                await agen.aclose()

        return StreamingResponse(gen(), media_type="text/event-stream")

    @app.post("/api/agents/tail")
    async def api_agents_tail(req: Request) -> JSONResponse:
        body = await req.json()
        key = str(body.get("key", ""))
        from_start = bool(body.get("from_start", True))
        ok = await manager.start_tail(key, from_start=from_start)
        return JSONResponse({"ok": ok})

    # ---- SPA ------------------------------------------------------------
    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        idx = STATIC_DIR / "index.html"
        if idx.is_file():
            return HTMLResponse(idx.read_text(encoding="utf-8"))
        return HTMLResponse("<h1>3d web</h1><p>static/index.html missing</p>")

    return app
