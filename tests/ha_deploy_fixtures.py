"""A local stand-in for the HA host that `3d ha deploy --host local` can run against.

`build_world` creates a 3d-cli-like source repo with integrations/homeassistant/, an HA repo
whose `3d-cli` submodule points at it (dests start as regular byte-identical files: the
pre-cutover shape), a bare `origin` for the HA repo, fake `docker`/`curl`/`gh` executables
that log their argv, and an HTTP server answering the HA API, the dashboard and go2rtc.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

GIT_ENV_LEAKS = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY",
    "GIT_NAMESPACE",
    "GIT_PREFIX",
)
SERVICES = ("cam-proxy",)
ENTITIES_OK = [
    {"entity_id": "sensor.printer_u1_status", "state": "printing"},
    {"entity_id": "sensor.printer_k1_raw", "state": "unavailable"},
    {"entity_id": "light.kitchen", "state": "on"},
]

PACKAGE = """template:
  - sensor:
      - name: Printer U1 Status
        state: "{{ 'printing' }}"
automation:
  - alias: Printer done
    trigger: []
    action: []
"""
SNIPPET = """lovelace:
  dashboards:
    3d-printers:
      mode: yaml
      filename: ui-3d-printers.yaml
"""
CONFIGURATION = """homeassistant:
  packages: !include_dir_named packages
lovelace:
  resource_mode: yaml
  dashboards:
    3d-printers:
      mode: yaml
      filename: ui-3d-printers.yaml
    other:
      mode: yaml
      filename: ui-other.yaml
http:
  login_attempts_threshold: !secret attempts
"""
COMPOSE_3D = """services:
  cam-proxy:
    image: example/cam:1
    volumes:
      - {ha}:/config:ro
"""
HA_COMPOSE = """include:
  - 3d-cli/integrations/homeassistant/compose/3d.docker-compose.yaml
services:
  homeassistant:
    image: example/ha:1
"""


def git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-c", "protocol.file.allow=always", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        env=clean_env(),
    )
    if proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} in {cwd}: {proc.stderr}")
    return proc.stdout.strip()


def clean_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in GIT_ENV_LEAKS}
    env.update(
        {
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.invalid",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
        }
    )
    env.update(extra or {})
    return env


class _Handler(BaseHTTPRequestHandler):
    server: FakeServer

    def do_GET(self) -> None:  # noqa: N802 — http.server API
        srv = self.server
        if self.path == "/api/states":
            seq = srv.state_sequence
            body = json.dumps(seq[min(srv.state_calls, len(seq) - 1)]).encode()
            srv.state_calls += 1
            self._send(200, body)
        elif self.path == "/3d-printers/overview":
            self._send(srv.dashboard_status, b"<html></html>")
        elif self.path == "/api/streams":
            self._send(
                200, json.dumps({"u1_camera": {"producers": [{"url": "x"}]}}).encode()
            )
        else:
            self._send(404, b"")

    def _send(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 — http.server API
        return


class FakeServer(ThreadingHTTPServer):
    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.state_sequence: list[list[dict[str, str]]] = [ENTITIES_OK]
        self.state_calls = 0
        self.dashboard_status = 200

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"


@dataclass
class World:
    root: Path
    src: Path
    ha: Path
    origin: Path
    log: Path
    server: FakeServer
    sha: str
    env: dict[str, str] = field(default_factory=dict)

    def commit_src(self, rel: str, text: str, message: str) -> str:
        path = self.src / "integrations/homeassistant" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        git(self.src, "add", "-A")
        git(self.src, "commit", "-q", "-m", message)
        self.sha = git(self.src, "rev-parse", "HEAD")
        return self.sha

    def calls(self, tool: str) -> list[str]:
        if not self.log.exists():
            return []
        return [
            line
            for line in self.log.read_text().splitlines()
            if line.startswith(tool + " ")
        ]


def _manifest(world_root: Path, server_url: str, origin: Path, ha: Path) -> str:
    return f"""ha_repo:
  host: local
  dir: {ha}
  github: test/ha
  origin: {origin}
  submodule: 3d-cli
  submodule_url: {world_root / "src"}
  container: fake-ha
  compose_include: 3d-cli/integrations/homeassistant/compose/3d.docker-compose.yaml
  compose_file: ha.docker-compose.yaml
  configuration: configuration.yaml
  deployed_marker: .3d-cli-deployed
  backups: {world_root / "backups"}
  entity_prefixes: [sensor.printer_, binary_sensor.printer_]
  ha_url: {server_url}
  dashboard_path: /3d-printers/overview
  go2rtc_api: {server_url}
  go2rtc_stream: u1_camera
files:
  - {{src: packages/3d_printers.yaml, dest: packages/3d_printers.yaml, mode: symlink}}
  - {{src: dashboards/ui-3d-printers.yaml, dest: ui-3d-printers.yaml, mode: symlink}}
  - {{src: scripts/cam.py, dest: scripts/cam.py, mode: symlink, services: [cam-proxy]}}
  - {{src: www/card/card.js, dest: www/card/card.js, mode: copy}}
internal: [manifest.yaml, vendor.lock, snippets/, compose/]
"""


FAKE_DOCKER = r"""#!/bin/bash
echo "docker $*" >> "$FAKE_LOG"
case "$1" in
  inspect)
    if [ "$2" = fake-ha ]; then
      printf '{"com.docker.compose.project":"fake","com.docker.compose.project.config_files":"%s/ha.docker-compose.yaml","com.docker.compose.project.working_dir":"%s"}\n' "$FAKE_HA_DIR" "$FAKE_HA_DIR"
      exit 0
    fi
    for s in $FAKE_SERVICES; do
      if [ "$2" = "fake-$s-1" ]; then
        printf '{"com.docker.compose.config-hash":"h-%s"}\t[{"Type":"bind","Source":"%s"}]\n' "$s" "$FAKE_HA_DIR"
        exit 0
      fi
    done
    echo "Error: No such object: $2" >&2; exit 1 ;;
  compose)
    if [[ " $* " == *" config --hash "* ]]; then
      for s in $FAKE_SERVICES; do echo "$s h-$s"; done
    fi
    exit 0 ;;
  exec)
    echo "${FAKE_CHECK_CONFIG_OUT:-Configuration valid}"
    exit "${FAKE_CHECK_CONFIG_RC:-0}" ;;
esac
"""
FAKE_CURL = """#!/bin/bash
echo "curl ${*: -1}" >> "$FAKE_LOG"
exit "${FAKE_CURL_RC:-0}"
"""
FAKE_GH = """#!/bin/bash
echo "gh $*" >> "$FAKE_LOG"
[ -n "${FAKE_GH_RC:-}" ] && exit "$FAKE_GH_RC"
if [ "$1 $2" = "pr create" ]; then echo "https://github.com/test/ha/pull/7"; fi
"""


def build_world(tmp_path: Path) -> World:
    server = FakeServer()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    src, ha, origin = tmp_path / "src", tmp_path / "ha", tmp_path / "ha-origin.git"
    integ = src / "integrations/homeassistant"
    files = {
        "manifest.yaml": _manifest(tmp_path, server.url, origin, ha),
        "vendor.lock": "{}\n",
        "snippets/configuration.3d.yaml": SNIPPET,
        "compose/3d.docker-compose.yaml": COMPOSE_3D.format(ha=ha),
        "packages/3d_printers.yaml": PACKAGE,
        "dashboards/ui-3d-printers.yaml": "views: []\n",
        "scripts/cam.py": "print('cam')\n",
        "www/card/card.js": "export {};\n",
    }
    for rel, text in files.items():
        (integ / rel).parent.mkdir(parents=True, exist_ok=True)
        (integ / rel).write_text(text, encoding="utf-8")
    src.mkdir(exist_ok=True)
    git(src, "init", "-q", "-b", "main")
    git(src, "add", "-A")
    git(src, "commit", "-q", "-m", "source")
    sha = git(src, "rev-parse", "HEAD")

    ha.mkdir()
    git(ha, "init", "-q", "-b", "main")
    (ha / "configuration.yaml").write_text(CONFIGURATION, encoding="utf-8")
    (ha / "ha.docker-compose.yaml").write_text(HA_COMPOSE, encoding="utf-8")
    (ha / ".env").write_text('HA_TOKEN="secret-token"\n', encoding="utf-8")
    # Like the real HA repo: www/ is ignored and its tracked files were added with -f.
    (ha / ".gitignore").write_text(".env\n.3d-cli-deployed\nwww/*\n", encoding="utf-8")
    # Pre-cutover shape: the dests are regular files with the source's bytes.
    for rel, dest in (
        ("packages/3d_printers.yaml", "packages/3d_printers.yaml"),
        ("dashboards/ui-3d-printers.yaml", "ui-3d-printers.yaml"),
        ("scripts/cam.py", "scripts/cam.py"),
        ("www/card/card.js", "www/card/card.js"),
    ):
        (ha / dest).parent.mkdir(parents=True, exist_ok=True)
        (ha / dest).write_bytes((integ / rel).read_bytes())
    git(ha, "submodule", "add", "-q", str(src), "3d-cli")
    git(ha, "add", "-A")
    git(ha, "add", "-f", "www/card/card.js")  # tracked despite www/*, as in the real repo
    git(ha, "commit", "-q", "-m", "ha config")
    git(tmp_path, "clone", "-q", "--bare", str(ha), str(origin))
    git(ha, "remote", "add", "origin", str(origin))
    git(ha, "fetch", "-q", "origin")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("docker", FAKE_DOCKER), ("curl", FAKE_CURL), ("gh", FAKE_GH)):
        (bin_dir / name).write_text(body, encoding="utf-8")
        (bin_dir / name).chmod(0o755)
    log = tmp_path / "calls.log"
    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "FAKE_LOG": str(log),
        "FAKE_HA_DIR": str(ha),
        "FAKE_SERVICES": " ".join(SERVICES),
        "XDG_CACHE_HOME": str(tmp_path / "xdg-cache"),
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    return World(tmp_path, src, ha, origin, log, server, sha, env)
