from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal

from errors import InvalidArgument

FormatStatus = Literal["supported", "planned"]
ExportRoute = Literal["openscad", "usdz", "planned"]


@dataclass(frozen=True)
class ExportFormat:
    key: str
    label: str
    extensions: tuple[str, ...]
    status: FormatStatus
    route: ExportRoute
    summary: str
    openscad_format: str | None = None
    selector: str | None = None
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExportPlan:
    input_path: str
    output_path: str
    format: ExportFormat
    steps: tuple[str, ...]


_FORMATS: tuple[ExportFormat, ...] = (
    ExportFormat(
        key="stl",
        label="STL",
        extensions=("stl",),
        status="supported",
        route="openscad",
        summary="mesh export for slicers; binary by default, ASCII with --ascii",
        openscad_format="binstl",
        selector="--stl",
        notes=("validated with mesh_check when binary STL and the mesh stack is available",),
    ),
    ExportFormat(
        key="3mf",
        label="3MF",
        extensions=("3mf",),
        status="supported",
        route="openscad",
        summary="rich print mesh format for slicers",
        openscad_format="3mf",
        selector="--3mf",
    ),
    ExportFormat(
        key="off",
        label="OFF",
        extensions=("off",),
        status="supported",
        route="openscad",
        summary="OpenSCAD-native mesh interchange",
        openscad_format="off",
        selector="--off",
    ),
    ExportFormat(
        key="amf",
        label="AMF",
        extensions=("amf",),
        status="supported",
        route="openscad",
        summary="OpenSCAD-native additive manufacturing mesh",
        openscad_format="amf",
        selector="--amf",
    ),
    ExportFormat(
        key="usdz",
        label="USDZ",
        extensions=("usdz",),
        status="supported",
        route="usdz",
        summary="Apple AR Quick Look export with the integrated color-capable USDZ path",
        selector="--usdz",
        notes=("runs OpenSCAD to a validated STL, then converts with the USD/trimesh stack",),
    ),
    ExportFormat(
        key="obj",
        label="OBJ",
        extensions=("obj",),
        status="planned",
        route="planned",
        summary="viewer-friendly mesh interchange",
        selector="--obj",
    ),
    ExportFormat(
        key="ply",
        label="PLY",
        extensions=("ply",),
        status="planned",
        route="planned",
        summary="mesh/scan/debug interchange with optional vertex color",
        selector="--ply",
    ),
    ExportFormat(
        key="glb",
        label="GLB",
        extensions=("glb",),
        status="planned",
        route="planned",
        summary="binary glTF for web and three.js viewers",
        selector="--glb",
    ),
    ExportFormat(
        key="gltf",
        label="glTF",
        extensions=("gltf",),
        status="planned",
        route="planned",
        summary="JSON glTF for web viewers",
        selector="--gltf",
    ),
    ExportFormat(
        key="step",
        label="STEP",
        extensions=("step", "stp"),
        status="planned",
        route="planned",
        summary="CAD B-rep handoff format",
        selector="--step",
    ),
    ExportFormat(
        key="brep",
        label="BREP",
        extensions=("brep",),
        status="planned",
        route="planned",
        summary="OpenCASCADE boundary-representation exchange",
        selector="--brep",
    ),
    ExportFormat(
        key="svg",
        label="SVG",
        extensions=("svg",),
        status="planned",
        route="planned",
        summary="2D silhouette/section profile export",
        selector="--svg",
    ),
)


def list_export_formats() -> tuple[ExportFormat, ...]:
    return _FORMATS


def selector_map() -> dict[str, str]:
    return {fmt.selector: fmt.key for fmt in _FORMATS if fmt.selector}


def accepted_format_keys() -> list[str]:
    return [fmt.key for fmt in _FORMATS]


def accepted_extensions() -> list[str]:
    exts: list[str] = []
    for fmt in _FORMATS:
        exts.extend(f".{ext}" for ext in fmt.extensions)
    return exts


def _normalize_format(raw: str) -> str:
    value = raw.strip().lower()
    if value.startswith("--"):
        value = value[2:]
    if value.startswith("."):
        value = value[1:]
    if value == "stp":
        return "step"
    return value


def get_format(key: str) -> ExportFormat:
    normalized = _normalize_format(key)
    for fmt in _FORMATS:
        if normalized == fmt.key or normalized in fmt.extensions:
            return fmt
    raise InvalidArgument(
        "--format",
        key,
        accepted_format_keys(),
        command="export",
        extra="Run '3d export --list-formats' to see status and pipeline notes.",
    )


def infer_format(output_path: str, *, explicit_format: str) -> ExportFormat:
    if explicit_format:
        return get_format(explicit_format)
    if output_path:
        if "." not in os.path.basename(output_path):
            raise InvalidArgument(
                "output extension",
                "(none)",
                accepted_extensions(),
                command="export",
                extra="Add an output extension or pass --format explicitly.",
            )
        ext = output_path.rsplit(".", 1)[-1]
        try:
            return get_format(ext)
        except InvalidArgument:
            raise InvalidArgument(
                "output extension",
                "." + ext,
                accepted_extensions(),
                command="export",
                extra="Use --format only when selecting a format without a matching output extension.",
            ) from None
    return get_format("stl")


def default_output_path(input_path: str, fmt: ExportFormat) -> str:
    base = input_path[: -len(".scad")] if input_path.endswith(".scad") else input_path
    return base + "." + fmt.extensions[0]


def build_export_plan(
    input_path: str,
    output_path: str,
    explicit_format: str,
    *,
    ascii_stl: bool,
) -> ExportPlan:
    fmt = infer_format(output_path, explicit_format=explicit_format)
    if explicit_format and output_path and "." in os.path.basename(output_path):
        ext = output_path.rsplit(".", 1)[-1].lower()
        if ext not in fmt.extensions:
            raise InvalidArgument(
                "output extension",
                "." + ext,
                [f".{accepted}" for accepted in fmt.extensions],
                command="export",
                extra="Output extension must match the selected export format.",
            )
    out = output_path or default_output_path(input_path, fmt)
    steps: tuple[str, ...]
    if fmt.route == "openscad":
        if fmt.key == "stl":
            scad_format = "asciistl" if ascii_stl else "binstl"
        else:
            scad_format = fmt.openscad_format or ""
        export_flag = f"--export-format {scad_format} " if scad_format else ""
        steps = (
            f"OpenSCAD export: openscad {export_flag}-o {out} {input_path}",
            "Validate OpenSCAD warnings for manifold, self-intersection, and degenerate geometry.",
        )
        if fmt.key == "stl" and not ascii_stl:
            steps = (*steps, "Run mesh_check.py when the Python mesh stack is available.")
    elif fmt.route == "usdz":
        steps = (
            f"OpenSCAD intermediate STL export: openscad --export-format binstl -o <temp>.stl {input_path}",
            "Validate the intermediate STL with the same geometry checks as binary STL export.",
            f"Convert the validated STL to color-capable USDZ with lib/usdz.py -> {out}.",
        )
    else:
        steps = (
            f"{fmt.label} export is planned but not implemented yet.",
            "Use a supported mesh format now, or keep this plan as the roadmap target.",
        )
    return ExportPlan(input_path=input_path, output_path=out, format=fmt, steps=steps)
