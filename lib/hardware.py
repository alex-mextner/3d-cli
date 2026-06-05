"""Hardware and toolchain capability summaries for the 3d CLI.

This module stays stdlib-only and delegates all tool discovery to cli.env so command
modules can import it without violating the registry's import-light contract.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
import platform
import shutil
from typing import Literal

from cli import env

Status = Literal["PASS", "WARN", "MISSING"]


@dataclass(frozen=True)
class HardwareItem:
    name: str
    capability: str
    status: Status
    detail: str
    required: bool
    install: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "capability": self.capability,
            "status": self.status,
            "detail": self.detail,
            "required": self.required,
            "install": self.install,
        }


@dataclass(frozen=True)
class HardwareReport:
    os_name: str
    machine: str
    cpu_count: int
    items: list[HardwareItem]

    def is_valid(self) -> bool:
        return not any(item.required and item.status == "MISSING" for item in self.items)

    def item(self, name: str) -> HardwareItem:
        for item in self.items:
            if item.name == name:
                return item
        raise KeyError(name)

    def to_dict(self) -> dict[str, object]:
        return {
            "os": self.os_name,
            "machine": self.machine,
            "cpu_count": self.cpu_count,
            "valid": self.is_valid(),
            "items": [item.to_dict() for item in self.items],
        }


def _path_detail(binary: str) -> str:
    if os.path.isabs(binary):
        return binary
    return shutil.which(binary) or binary


def _venv_python() -> str:
    return os.path.join(env.repo_root(), ".venv", "bin", "python")


def _build_mesh_stack_item(py: str | None, has_uv: bool, has_venv: bool) -> HardwareItem:
    if py is None:
        return HardwareItem(
            name="python mesh stack",
            capability="mesh/check/printability/collision/preprocess",
            status="MISSING",
            detail="no python runtime available",
            required=True,
            install=env.install_cmd("python3"),
        )

    missing = [mod for mod in env.PY_MESH_MODULES if not env.py_has_module(mod)]
    if not missing:
        return HardwareItem(
            name="python mesh stack",
            capability="mesh/check/printability/collision/preprocess",
            status="PASS",
            detail=f"all {len(env.PY_MESH_MODULES)} modules importable by {py}",
            required=True,
        )

    package_names = [env.pypkg_for(mod) for mod in missing]
    packages = ", ".join(package_names)
    if not has_venv and has_uv:
        return HardwareItem(
            name="python mesh stack",
            capability="mesh/check/printability/collision/preprocess",
            status="WARN",
            detail=f"missing from {py}: {packages}; uv resolves per-call",
            required=True,
        )
    return HardwareItem(
        name="python mesh stack",
        capability="mesh/check/printability/collision/preprocess",
        status="MISSING",
        detail=f"missing from {py}: {packages}",
        required=True,
        install=f"pip install {' '.join(package_names)}",
    )


def build_report() -> HardwareReport:
    os_name = env.detect_os()
    machine = platform.machine() or "unknown"
    cpu_count = os.cpu_count() or 1
    py = env.resolve_python()
    uv_disabled = bool(os.environ.get("PY3D_NO_UV"))
    uv_path = None if uv_disabled else shutil.which("uv")
    has_uv = uv_path is not None
    venv_py = _venv_python()
    has_venv = os.access(venv_py, os.X_OK)

    items: list[HardwareItem] = []

    osc = env.find_openscad()
    items.append(
        HardwareItem(
            name="openscad",
            capability="render/export/validate/check",
            status="PASS" if osc else "MISSING",
            detail=osc or "not found",
            required=True,
            install=None if osc else env.install_cmd("openscad"),
        )
    )

    magick = env.find_magick()
    items.append(
        HardwareItem(
            name="imagemagick",
            capability="silhouette/score/overlay",
            status="PASS" if magick else "MISSING",
            detail=_path_detail(magick) if magick else "not found",
            required=True,
            install=None if magick else env.install_cmd("imagemagick"),
        )
    )

    slicer = env.find_slicer()
    items.append(
        HardwareItem(
            name="slicer",
            capability="slice",
            status="PASS" if slicer else "MISSING",
            detail=f"{slicer[0]}: {slicer[1]}" if slicer else "not found",
            required=True,
            install=None if slicer else env.install_cmd("slicer"),
        )
    )

    items.append(
        HardwareItem(
            name="python3",
            capability="python tool runner",
            status="PASS" if py else "MISSING",
            detail=py or "not found",
            required=True,
            install=None if py else env.install_cmd("python3"),
        )
    )

    if uv_disabled:
        uv_item = HardwareItem(
            name="uv",
            capability="per-call python dependency resolution",
            status="WARN",
            detail="disabled by PY3D_NO_UV",
            required=False,
            install=env.install_cmd("uv"),
        )
    elif uv_path:
        uv_item = HardwareItem(
            name="uv",
            capability="per-call python dependency resolution",
            status="PASS",
            detail=uv_path,
            required=False,
        )
    else:
        uv_item = HardwareItem(
            name="uv",
            capability="per-call python dependency resolution",
            status="WARN",
            detail="not found",
            required=False,
            install=env.install_cmd("uv"),
        )
    items.append(uv_item)

    items.append(
        HardwareItem(
            name=".venv",
            capability="preferred local python environment",
            status="PASS" if has_venv else "WARN",
            detail=venv_py if has_venv else "absent; pyrun can fall back to uv or system python",
            required=False,
        )
    )

    pip = shutil.which("pip3") or shutil.which("pip")
    items.append(
        HardwareItem(
            name="pip",
            capability="manual python package installation",
            status="PASS" if pip else "WARN",
            detail=pip or "not found",
            required=False,
        )
    )

    items.append(_build_mesh_stack_item(py, has_uv, has_venv))

    return HardwareReport(
        os_name=os_name,
        machine=machine,
        cpu_count=cpu_count,
        items=items,
    )
