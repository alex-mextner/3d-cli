"""3d web — start the local interactive 3D-project dashboard (FastAPI + SSE + three.js).

A browser dashboard to browse projects, view models in three.js, scrub constants with
live re-render, read the spec, set colors, play animations, and watch AI agents work live
(Claude / Codex / opencode log adapters). All of the real logic lives in `lib/web/`
(server.py + webconfig.py + adapters/…); this module is the thin registry command that
parses flags and launches uvicorn.

The web tier is OPTIONAL: FastAPI/uvicorn are heavy deps NOT required for the core
geometry pipeline, so they are LAZY-imported inside run() — keeping the module stdlib-only
at import time (the registry imports every command on every `3d` invocation). If they are
absent, run() raises a structured MissingDependency naming the exact install command and
that only the `web` tier degrades.
"""
from __future__ import annotations

import pathlib

from cli.registry import Command
from errors import InvalidArgument, MissingDependency, UsageError

USAGE = """3d web [options]
  Start the local web dashboard for 3D-modeling projects and live agent activity
  (FastAPI + Server-Sent-Events + a three.js SPA). All Python, async.

Options:
  --root DIR     project root to scan   (default: from ~/.config/3d-cli/web.json, else cwd)
  --port N       listen port            (default: 8733 or config)
  --host H       bind host              (default: 127.0.0.1 or config)
  --config PATH  print/use a specific config file path  (default: ~/.config/3d-cli/web.json)
  --open         open the dashboard in your browser once it starts

Config: ~/.config/3d-cli/web.json  (the same config dir as the first-run bootstrap marker;
created with defaults on first run).

Examples:
  3d web --root ~/models --open
  3d web --port 9000"""


def run(argv: list[str]) -> int:  # noqa: C901
    if argv and argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0

    root: str | None = None
    port: int | None = None
    host: str | None = None
    config: str | None = None
    do_open = False

    i = 0
    n = len(argv)
    while i < n:
        a = argv[i]
        if a == "--root":
            root = argv[i + 1] if i + 1 < n else ""
            i += 2
        elif a == "--host":
            host = argv[i + 1] if i + 1 < n else ""
            i += 2
        elif a == "--config":
            config = argv[i + 1] if i + 1 < n else ""
            i += 2
        elif a == "--port":
            raw = argv[i + 1] if i + 1 < n else ""
            try:
                port = int(raw)
            except ValueError as e:
                raise InvalidArgument(
                    "--port", raw, ["an integer (e.g. 8733)"], command="web"
                ) from e
            i += 2
        elif a == "--open":
            do_open = True
            i += 1
        else:
            print(USAGE)
            raise UsageError(f"unknown option '{a}'", command="web")

    # Lazy imports: fastapi/uvicorn are the optional `web` tier. Importing them (and the
    # server module, which imports fastapi at its top) only here keeps this command module
    # stdlib-only at registry-discovery time.
    try:
        import fastapi  # noqa: F401
        import uvicorn  # noqa: F401
    except ImportError as e:
        raise MissingDependency(
            "fastapi + uvicorn (the `web` dashboard tier)",
            install="pip install fastapi uvicorn markdown pyyaml  (or: uv run --with fastapi,uvicorn,markdown,pyyaml 3d web)",
            degrades="only `3d web` is unavailable; the core geometry/render/check pipeline is unaffected",
            command="web",
        ) from e

    if config:
        # webconfig.config_path() honors this override (see THREED_WEB_CONFIG there).
        import os

        os.environ["THREED_WEB_CONFIG"] = str(pathlib.Path(config).expanduser().resolve())

    from web import webconfig
    from web.server import create_app

    cfg = webconfig.load_or_create(default_root=root)
    if root:
        cfg.project_root = str(pathlib.Path(root).expanduser().resolve())
    if port:
        cfg.port = port
    if host:
        cfg.host = host

    app = create_app(cfg)
    url = f"http://{cfg.host}:{cfg.port}/"
    print(f"3d web — serving {cfg.project_root}")
    print(f"  dashboard: {url}   (config: {webconfig.config_path()})")
    if do_open:
        import threading
        import webbrowser

        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    uvicorn.run(app, host=cfg.host, port=cfg.port, log_level="info")
    return 0


COMMAND = Command(
    name="web",
    group="ENVIRONMENT",
    summary="start the local browser dashboard (FastAPI + SSE + three.js) for projects + agents",
    usage=USAGE,
    run=run,
)
