"""Fixtures for the `3d models publish` tests: a throwaway project directory laid out like a
garage-band project, and a loopback Thingiverse that records the create / upload / finalize /
publish calls. No test touches a real site."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

YAML = """\
title: Wall plug
description: A spiral plug.
tags: [plug, wall]
license: cc-by-nc
category: Household
"""


def make_project(
    root: Path,
    yaml_text: str = YAML,
    *,
    mf3: bool = True,
    scad: bool = True,
    images: bool = True,
) -> Path:
    project = root / "proj"
    (project / "print").mkdir(parents=True)
    (project / "previews").mkdir()
    (project / "references").mkdir()
    (project / "wall_plug.stl").write_bytes(b"solid x\nendsolid x\n")
    (project / "references" / "original.scad").write_text("// somebody else's code\n")
    (project / "print" / "wall_plug.gcode").write_text("G1\n")
    (project / "print" / "wall_plug.manifest.json").write_text("{}")
    if mf3:
        (project / "print" / "wall_plug.3mf").write_bytes(b"PK3mf")
    if scad:
        (project / "wall_plug.scad").write_text("cube(1);\n")
    if images:
        (project / "previews" / "iso.png").write_bytes(b"\x89PNG")
        (project / "previews" / "iso.png.dims.json").write_text("{}")
    if yaml_text:
        (project / "publish.yaml").write_text(yaml_text, encoding="utf-8")
    return project


class FakeThingiverse:
    """Records every request; answers the create / upload / finalize / publish calls."""

    def __init__(self, *, fail_publish: bool = False) -> None:
        self.calls: list[tuple[str, str, dict[str, str], bytes]] = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: object) -> None:
                return

            def do_POST(self) -> None:  # noqa: N802 — http.server API
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                fake.calls.append(("POST", self.path, dict(self.headers), body))
                reply: dict[str, object]
                if self.path == "/things":
                    reply, status = {"id": 777}, 200
                elif self.path == "/files/0/uploadFile":
                    reply, status = {"id": 100 + len(fake.calls)}, 200
                elif self.path.endswith("/publish") and fail_publish:
                    reply, status = {"errors": ["name is blank"]}, 200
                else:
                    reply, status = (
                        {"public_url": "https://www.thingiverse.com/thing:777"},
                        200,
                    )
                data = json.dumps(reply).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


DATA = Path(__file__).resolve().parent / "data" / "publish"
CULTS_USER, CULTS_KEY = "tester", "cults-test-key"
HOSTED = """\
hosted:
  wall_plug.3mf: https://files.example.com/wall_plug.3mf
  wall_plug.scad: https://files.example.com/wall_plug.scad
  wall_plug.stl: https://files.example.com/wall_plug.stl
  iso.png: https://files.example.com/iso.png
"""


def three_d_yaml(project: Path, parts: str) -> None:
    """`parts` is the YAML body under `parts:` (the roles of the project's parts)."""
    (project / "3d.yaml").write_text(
        "name: wall-plug\nparts:\n" + parts, encoding="utf-8"
    )


class FakeCults:
    """A loopback Cults3D GraphQL endpoint answering with the fixtures of tests/data/publish/ (responses
    shaped after the community-documented API, github.com/CheekyCodexConjurer/cults3d-api-docs — NOT recorded
    from a real account). Every request is kept in `calls` as (authorization header, query, variables)."""

    def __init__(self, *, create: str = "cults_create") -> None:
        self.calls: list[tuple[str, str, dict[str, object]]] = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: object) -> None:
                return

            def do_POST(self) -> None:  # noqa: N802 — http.server API
                body = json.loads(
                    self.rfile.read(int(self.headers.get("Content-Length") or 0))
                    or b"{}"
                )
                query = str(body.get("query") or "")
                fake.calls.append(
                    (
                        self.headers.get("Authorization", ""),
                        query,
                        body.get("variables") or {},
                    )
                )
                name = (
                    "cults_categories"
                    if "categories" in query
                    else "cults_licenses"
                    if "licenses" in query
                    else create
                )
                data = (DATA / f"{name}.json").read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}/graphql"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
