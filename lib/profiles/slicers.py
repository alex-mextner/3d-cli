"""slicers.py — where presets live: the three Orca-family slicers' data dirs and the repo
`profiles/` root.

DATA DIR (OrcaSlicer GUI_App.cpp, same code in Bambu Studio and Snapmaker Orca): Linux
`$XDG_CONFIG_HOME/<AppName>` (default `~/.config/<AppName>`); a Flatpak sandbox sets
XDG_CONFIG_HOME to `~/.var/app/<app-id>/config`; macOS `~/Library/Application Support/<AppName>`.
USER PRESETS (PresetBundle::load_presets): `<data>/user/<preset_folder>/{filament,process,machine}`
plus a `base/` subdir per kind, where `preset_folder` is `app.preset_folder` in `<data>/<AppName>.conf`
(a cloud user id when logged in) and `default` when empty.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from cli.env import repo_root

KINDS = ("filament", "process", "machine")


@dataclass(frozen=True)
class Slicer:
    id: str
    label: str
    app_name: str
    flatpak_ids: tuple[str, ...]
    process_names: tuple[str, ...]


SLICERS: dict[str, Slicer] = {
    s.id: s
    for s in (
        Slicer(
            "orca",
            "OrcaSlicer",
            "OrcaSlicer",
            ("com.orcaslicer.OrcaSlicer", "io.github.softfever.OrcaSlicer"),
            ("orca-slicer", "orcaslicer"),
        ),
        Slicer(
            "snapmaker-orca",
            "Snapmaker Orca",
            "Snapmaker_Orca",
            ("io.github.Snapmaker.Snapmaker_Orca",),
            ("snapmaker-orca", "snapmaker_orca", "snapmaker orca"),
        ),
        Slicer(
            "bambu",
            "Bambu Studio",
            "BambuStudio",
            ("com.bambulab.BambuStudio",),
            ("bambu-studio", "bambustudio", "bambu studio"),
        ),
    )
}


def profiles_root() -> Path:
    """The repo `profiles/` dir (`$THREED_PROFILES_DIR` overrides it, e.g. for tests)."""
    env = os.environ.get("THREED_PROFILES_DIR")
    if env:
        return Path(env).expanduser()
    return Path(repo_root()) / "profiles"


def data_dir_candidates(slicer: Slicer) -> list[Path]:
    home = Path.home()
    if sys.platform == "darwin":
        return [home / "Library" / "Application Support" / slicer.app_name]
    config_home = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")
    out = [config_home / slicer.app_name]
    out.extend(
        home / ".var" / "app" / app_id / "config" / slicer.app_name
        for app_id in slicer.flatpak_ids
    )
    return out


def find_data_dir(slicer: Slicer) -> Path | None:
    """The first candidate data dir that holds a `user/` dir (the slicer has run there)."""
    return next((d for d in data_dir_candidates(slicer) if (d / "user").is_dir()), None)


def _preset_folder(data_dir: Path, slicer: Slicer) -> str:
    conf = data_dir / f"{slicer.app_name}.conf"
    try:
        text = conf.read_text(encoding="utf-8")
    except OSError:
        return ""
    # AppConfig is JSON; tolerate a trailing `# MD5 checksum` line some versions append.
    body = "\n".join(line for line in text.splitlines() if not line.startswith("#"))
    try:
        app = json.loads(body).get("app", {})
    except (ValueError, AttributeError):
        return ""
    folder = app.get("preset_folder", "") if isinstance(app, dict) else ""
    return folder if isinstance(folder, str) else ""


def user_presets_dir(data_dir: Path, slicer: Slicer) -> Path:
    return data_dir / "user" / (_preset_folder(data_dir, slicer) or "default")


def running_processes(slicer: Slicer) -> list[str] | None:
    """Names of running processes of this slicer; None when `ps` is unavailable (unknown)."""
    ps = shutil.which("ps")
    if ps is None:
        return None
    try:
        out = subprocess.run(
            [ps, "-Ao", "comm="], capture_output=True, text=True, timeout=10, check=True
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    names = {n.lower() for n in slicer.process_names}
    # Linux prints the 15-char task name, macOS the executable path.
    return sorted(
        {
            line.strip()
            for line in out.splitlines()
            if os.path.basename(line.strip()).lower() in names
        }
    )
