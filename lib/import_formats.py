"""Import-format planning helpers for the `3d import` command.

The module is intentionally stdlib-only. It does not convert meshes itself; it either
writes an OpenSCAD wrapper for formats OpenSCAD can import directly, or returns a
conversion plan for common mesh/CAD formats that need an external converter first.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import os
import shlex
from typing import Literal

from errors import InputNotFound, InvalidArgument, UsageError

ImportAction = Literal["wrapper", "plan"]


@dataclass(frozen=True)
class ModelFormat:
    key: str
    extensions: tuple[str, ...]
    label: str
    direct_import: bool
    note: str


@dataclass(frozen=True)
class ImportPlan:
    input_path: str
    format: ModelFormat
    action: ImportAction
    output_path: str | None
    scale: float
    convexity: int
    steps: tuple[str, ...]


FORMATS: tuple[ModelFormat, ...] = (
    ModelFormat("stl", ("stl",), "STL mesh", True, "OpenSCAD can import STL meshes directly."),
    ModelFormat("off", ("off",), "OFF mesh", True, "OpenSCAD can import OFF meshes directly."),
    ModelFormat("amf", ("amf",), "AMF mesh", True, "OpenSCAD can import AMF meshes directly."),
    ModelFormat("3mf", ("3mf",), "3MF mesh", True, "OpenSCAD can import 3MF meshes directly."),
    ModelFormat("obj", ("obj",), "Wavefront OBJ mesh", False, "Convert to STL/OFF before OpenSCAD import."),
    ModelFormat("ply", ("ply",), "PLY mesh", False, "Convert to STL/OFF before OpenSCAD import."),
    ModelFormat("gltf", ("gltf", "glb"), "glTF/GLB scene", False, "Convert mesh geometry to STL/OFF first."),
    ModelFormat("dae", ("dae",), "Collada scene", False, "Convert mesh geometry to STL/OFF first."),
    ModelFormat("step", ("step", "stp"), "STEP CAD model", False, "Tessellate to STL/3MF before OpenSCAD import."),
    ModelFormat("iges", ("iges", "igs"), "IGES CAD model", False, "Tessellate to STL/3MF before OpenSCAD import."),
    ModelFormat("brep", ("brep",), "BREP CAD model", False, "Tessellate to STL/3MF before OpenSCAD import."),
    ModelFormat("fcstd", ("fcstd",), "FreeCAD document", False, "Export or tessellate to STL/3MF before OpenSCAD import."),
    ModelFormat("usdz", ("usdz", "usd", "usdc"), "USD/USDZ scene", False, "Extract mesh geometry and convert to STL/OFF first."),
)

_BY_EXTENSION = {ext: fmt for fmt in FORMATS for ext in fmt.extensions}


def accepted_extensions(*, direct_only: bool = False) -> list[str]:
    formats = [fmt for fmt in FORMATS if fmt.direct_import or not direct_only]
    return sorted("." + ext for fmt in formats for ext in fmt.extensions)


def detect_format(path: str, override: str | None = None) -> ModelFormat:
    raw = override or os.path.splitext(path)[1].lstrip(".")
    key = raw.lower().lstrip(".")
    fmt = _BY_EXTENSION.get(key)
    if fmt is None:
        raise InvalidArgument(
            "import format",
            "." + key if key else "(none)",
            accepted_extensions(),
            command="import",
            extra="Use --format to override files with missing or unusual extensions.",
        )
    return fmt


def _extension(path: str) -> str:
    return os.path.splitext(path)[1].lstrip(".").lower()


def default_wrapper_path(input_path: str) -> str:
    root, _ext = os.path.splitext(input_path)
    return root + ".import.scad"


def _is_scad_path(path: str) -> bool:
    return os.path.splitext(path)[1].lower() == ".scad"


def plan_import(
    input_path: str,
    *,
    out_path: str | None = None,
    format_override: str | None = None,
    mode: str = "auto",
    scale: float = 1.0,
    convexity: int = 10,
) -> ImportPlan:
    if mode not in ("auto", "wrapper", "plan"):
        raise InvalidArgument("--mode", mode, ["auto", "wrapper", "plan"], command="import")
    if not os.path.isfile(input_path):
        raise InputNotFound(input_path, command="import")
    if not math.isfinite(scale) or scale <= 0.0:
        raise InvalidArgument("--scale", str(scale), ["a finite positive number"], command="import")
    if convexity < 1:
        raise InvalidArgument("--convexity", str(convexity), ["an integer >= 1"], command="import")
    if out_path is not None and os.path.abspath(out_path) == os.path.abspath(input_path):
        raise InvalidArgument(
            "output path",
            out_path,
            ["a distinct .scad wrapper path"],
            command="import",
            extra="The generated wrapper must not overwrite the source model.",
        )
    if out_path is not None and not _is_scad_path(out_path):
        raise InvalidArgument(
            "output path",
            out_path,
            [".scad wrapper path"],
            command="import",
            extra="The generated wrapper is OpenSCAD source and must use a .scad filename.",
        )

    fmt = detect_format(input_path, format_override)
    if mode == "wrapper" and not fmt.direct_import:
        raise InvalidArgument(
            "import format",
            "." + fmt.extensions[0],
            accepted_extensions(direct_only=True),
            command="import",
            extra=f"{fmt.label} needs conversion first; run `3d import {input_path} --mode plan`.",
        )

    direct_wrapper = fmt.direct_import and mode in ("auto", "wrapper")
    direct_plan = fmt.direct_import and mode == "plan"
    action: ImportAction = "wrapper" if direct_wrapper else "plan"
    actual_ext = _extension(input_path)
    if (direct_wrapper or direct_plan) and actual_ext not in fmt.extensions:
        raise InvalidArgument(
            "input extension",
            "." + actual_ext if actual_ext else "(none)",
            ["." + ext for ext in fmt.extensions],
            command="import",
            extra=(
                f"OpenSCAD chooses the importer from the filename extension; rename or copy "
                f"the file to a supported suffix before wrapping it as {fmt.label}."
            ),
        )
    output = out_path
    if fmt.direct_import and output is None:
        output = default_wrapper_path(input_path)

    steps = (
        _wrapper_steps(input_path, output)
        if fmt.direct_import
        else _conversion_steps(input_path, fmt, output, scale=scale, convexity=convexity)
    )
    return ImportPlan(
        input_path=input_path,
        format=fmt,
        action=action,
        output_path=output,
        scale=scale,
        convexity=convexity,
        steps=tuple(steps),
    )


def _wrapper_steps(input_path: str, out_path: str | None) -> list[str]:
    wrapper = out_path or default_wrapper_path(input_path)
    quoted_wrapper = shlex.quote(wrapper)
    export_path = os.path.splitext(wrapper)[0] + ".stl"
    return [
        f"Write OpenSCAD wrapper: {wrapper}",
        f"Render/check it with: 3d render {quoted_wrapper} --view 3-4",
        f"Export printable mesh with: 3d export {quoted_wrapper} -o {shlex.quote(export_path)}",
    ]


def _conversion_steps(
    input_path: str,
    fmt: ModelFormat,
    out_path: str | None,
    *,
    scale: float,
    convexity: int,
) -> list[str]:
    root, _ext = os.path.splitext(input_path)
    mesh = root + ".stl"
    wrapper = out_path or root + ".import.scad"
    if fmt.key in ("step", "iges", "brep", "fcstd"):
        first = f"Tessellate {fmt.label} to a mesh such as {mesh} with FreeCAD, CAD Assistant, or another CAD converter."
    elif fmt.key == "usdz":
        first = f"Extract mesh geometry from {fmt.label}, then convert it to STL/OFF such as {mesh}."
    else:
        first = f"Convert {fmt.label} to STL or OFF (e.g. {mesh}) with Blender, trimesh, MeshLab, or another mesh converter."
    wrap_cmd = f"3d import {shlex.quote(mesh)} -o {shlex.quote(wrapper)}"
    if scale != 1.0:
        wrap_cmd += f" --scale {scale:g}"
    if convexity != 10:
        wrap_cmd += f" --convexity {convexity}"
    return [
        first,
        f"Generate the OpenSCAD wrapper with: {wrap_cmd}",
        f"Run verification after wrapping: 3d check {shlex.quote(wrapper)}",
    ]


def _scad_string(path: str) -> str:
    return '"' + path.replace("\\", "\\\\").replace('"', '\\"') + '"'


def wrapper_source(plan: ImportPlan) -> str:
    if plan.action != "wrapper" or plan.output_path is None:
        raise InvalidArgument("--mode", plan.action, ["wrapper"], command="import")

    out_dir = os.path.dirname(os.path.abspath(plan.output_path)) or os.getcwd()
    abs_input = os.path.abspath(plan.input_path)
    try:
        rel = os.path.relpath(abs_input, out_dir)
    except ValueError:
        rel = abs_input
    rel = rel.replace(os.sep, "/")
    imported = f"import({_scad_string(rel)}, convexity = {plan.convexity});"
    if plan.scale != 1.0:
        imported = f"scale([{plan.scale:g}, {plan.scale:g}, {plan.scale:g}]) {imported}"

    return "\n".join(
        [
            "// Generated by `3d import`.",
            f"// Source: {os.path.abspath(plan.input_path)}",
            f"// Format: {plan.format.label}",
            f"// Next: 3d render {shlex.quote(plan.output_path)} --view 3-4",
            "",
            imported,
            "",
        ]
    )


def write_wrapper(plan: ImportPlan) -> str:
    if plan.output_path is None:
        raise InvalidArgument("--mode", plan.action, ["wrapper"], command="import")
    try:
        os.makedirs(os.path.dirname(plan.output_path) or ".", exist_ok=True)
        with open(plan.output_path, "w", encoding="utf-8") as fh:
            fh.write(wrapper_source(plan))
    except OSError as exc:
        raise UsageError(
            f"could not write import wrapper: {plan.output_path}",
            command="import",
            remediation=["Check the output path and directory permissions."],
        ) from exc
    return plan.output_path


def render_plan(plan: ImportPlan) -> str:
    lines = [
        f"input: {plan.input_path}",
        f"format: {plan.format.label} ({'.' + plan.format.extensions[0]})",
        f"action: {plan.action}",
    ]
    if plan.output_path:
        lines.append(f"output: {plan.output_path}")
    lines.append("steps:")
    for step in plan.steps:
        lines.append(f"  - {step}")
    lines.append(f"note: {plan.format.note}")
    return "\n".join(lines)
