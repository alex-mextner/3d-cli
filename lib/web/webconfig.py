#!/usr/bin/env python3
"""webconfig.py — load/create the web dashboard config.

Config path: ~/.config/3d/web.json  — the SAME config dir the rest of the CLI uses for its
first-run bootstrap marker (~/.config/3d/.bootstrapped). One config dir for the whole tool.
A default is written on first run.
"""
from __future__ import annotations

import dataclasses
import json
import os
import pathlib

DEFAULT_PORT = 8733
DEFAULT_HOST = "127.0.0.1"


def config_dir() -> pathlib.Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    root = pathlib.Path(base) if base else pathlib.Path.home() / ".config"
    return root / "3d"


def config_path() -> pathlib.Path:
    """The web config file. `THREED_WEB_CONFIG` (set by `3d web --config PATH`) overrides
    the default ~/.config/3d/web.json."""
    override = os.environ.get("THREED_WEB_CONFIG")
    if override:
        return pathlib.Path(override).expanduser().resolve()
    return config_dir() / "web.json"


def state_dir() -> pathlib.Path:
    d = config_dir() / "web-state"
    d.mkdir(parents=True, exist_ok=True)
    return d


@dataclasses.dataclass(slots=True)
class WebConfig:
    project_root: str
    port: int = DEFAULT_PORT
    host: str = DEFAULT_HOST

    def to_dict(self) -> dict[str, object]:
        return dataclasses.asdict(self)


def load_or_create(default_root: str | None = None) -> WebConfig:
    p = config_path()
    if p.is_file():
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            d = {}
        return WebConfig(
            project_root=str(d.get("project_root") or default_root or os.getcwd()),
            port=int(d.get("port", DEFAULT_PORT)),
            host=str(d.get("host", DEFAULT_HOST)),
        )
    cfg = WebConfig(project_root=default_root or os.getcwd())
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cfg.to_dict(), indent=2) + "\n", encoding="utf-8")
    return cfg
