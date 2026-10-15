"""settings.py — 3d-api configuration from the environment (stdlib; shared by the API and the CLI).

  PRINTOPS_DB           SQLite path      (default ~/.local/share/3d-cli/printops/printops.db;
                                          3d-api container: /data/printops.db)
  PRINTOPS_TOKENS_FILE  tokens JSON      (default: tokens.json next to the DB;
                                          container: /data/tokens.json)
  SPOOLMAN_URL          Spoolman root    (e.g. http://spoolman:8000; unset → health "degraded")
  PRINTOPS_VERSION      build identifier shown by /v1/health (deploy sets the git sha)
"""
from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from cli import paths


@dataclass(frozen=True)
class Settings:
    db_path: Path
    tokens_file: Path
    spoolman_url: str | None = None
    version: str = "dev"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if env is None else env
        db = Path(env.get("PRINTOPS_DB") or paths.data_dir() / "printops" / "printops.db").expanduser()
        tokens = Path(env.get("PRINTOPS_TOKENS_FILE") or db.parent / "tokens.json").expanduser()
        return cls(
            db_path=db,
            tokens_file=tokens,
            spoolman_url=env.get("SPOOLMAN_URL") or None,
            version=env.get("PRINTOPS_VERSION") or "dev",
        )
