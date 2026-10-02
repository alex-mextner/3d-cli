"""External QymCAD discovery without cloud calls or an invented automation API."""
from __future__ import annotations
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from typing import Any
from errors import MissingDependency, ThreeDError, UsageError

def discover(explicit: str | None = None) -> Path | None:
    supplied = explicit or os.environ.get("QYMCAD_EXECUTABLE")
    if supplied:
        path = Path(supplied).expanduser().resolve()
        if path.is_file() and (os.name == "nt" or os.access(path, os.X_OK)):
            return path
        if explicit:
            raise UsageError("the explicit QymCAD executable is not an executable file", command="qymcad")
        return None
    candidates = [shutil.which("qymcad"), shutil.which("qymcad.exe")]
    if sys.platform == "darwin":
        candidates.append("/Applications/QymCAD.app/Contents/MacOS/qymcad")
    managed = Path.home() / ".local" / "share" / "3d-cli" / "qymcad"
    if managed.is_dir():
        candidates.extend(str(p) for p in sorted(managed.glob("*/qymcad.exe"), reverse=True))
    for raw in candidates:
        if raw:
            path = Path(raw)
            if path.is_file() and (os.name == "nt" or os.access(path, os.X_OK)):
                return path.resolve()
    return None

def doctor(explicit: str | None = None) -> dict[str, Any]:
    path = discover(explicit)
    return {"available": path is not None, "executable": str(path) if path else None,
            "cloud_required": False, "integration": "external-local-application",
            "capabilities": {"gui_launch": path is not None, "headless_editing": False, "automatic_document_open": False, "cam": False},
            "note": "QymCAD may check for updates. Disable that in the application for offline use. No capability implies Fusion feature-history compatibility.",
            "upstream": "https://github.com/QymIs-Tech/QymCAD", "upstream_license": "AGPL-3.0-or-later"}

def launch(explicit: str | None = None, *, dry_run: bool = False) -> dict[str, Any]:
    path = discover(explicit)
    if path is None:
        raise MissingDependency("QymCAD", command="qymcad", install="Install the official QymCAD release, then set QYMCAD_EXECUTABLE to its absolute executable path.", degrades="local GUI fallback is unavailable; Fusion was not modified")
    result: dict[str, Any] = {"status": "PLANNED" if dry_run else "STARTED", "argv": [str(path)], "automatic_document_open": False}
    if not dry_run:
        try:
            logs = Path.home() / ".cache" / "3d-cli" / "qymcad-logs"
            logs.mkdir(parents=True, exist_ok=True)
            options: dict[str, Any]
            if sys.platform == "win32":
                options = {"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP}
            else:
                options = {"start_new_session": True}
            with tempfile.NamedTemporaryFile(prefix="launch-", suffix=".log", dir=logs, delete=False) as log:
                log_path = Path(log.name)
                process = subprocess.Popen([str(path)], cwd=path.parent, stdin=subprocess.DEVNULL, stdout=log, stderr=log, **options)
            try:
                early_exit = process.wait(timeout=0.75)
            except subprocess.TimeoutExpired:
                early_exit = None
            if early_exit is not None:
                tail = log_path.read_bytes()[-2000:].decode("utf-8", errors="replace").strip()
                raise ThreeDError(f"QymCAD exited during startup with code {early_exit}", command="qymcad",
                    remediation=[f"Inspect startup log: {log_path}", tail] if tail else [f"Inspect startup log: {log_path}"])
            result["startup_log"] = str(log_path)
        except OSError as exc:
            raise UsageError(f"QymCAD could not start: {exc}", command="qymcad") from exc
        result["pid"] = process.pid
    return result
