"""Bounded STL -> faceted B-rep conversion; no implicit repair or network access."""
from __future__ import annotations
from dataclasses import dataclass
import hashlib
import io
import json
import math
import os
from pathlib import Path
import sys
from typing import Any
from errors import GateFailure, InputNotFound, MissingDependency, ThreeDError, UsageError
from solid_artifacts import workspace

UNITS = {"mm": 1.0, "cm": 10.0, "m": 1000.0, "in": 25.4}
MAX_BYTES = 25 * 1024 * 1024

def _shape_vertices(shape: Any) -> list[tuple[float, float, float]]:
    from OCP.BRep import BRep_Tool
    from OCP.TopAbs import TopAbs_VERTEX
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS
    points = set()
    explorer = TopExp_Explorer(shape, TopAbs_VERTEX)
    while explorer.More():
        point = BRep_Tool.Pnt_s(TopoDS.Vertex(explorer.Current()))
        points.add((point.X(), point.Y(), point.Z()))
        explorer.Next()
    return sorted(points)

def inspect_shape(shape: Any) -> dict[str, Any]:
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib
    from OCP.BRepCheck import BRepCheck_Analyzer
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps
    from OCP.TopAbs import TopAbs_SOLID, TopAbs_FACE
    from OCP.TopExp import TopExp_Explorer
    props = GProp_GProps()
    BRepGProp.VolumeProperties_s(shape, props)
    surface = GProp_GProps()
    BRepGProp.SurfaceProperties_s(shape, surface)
    center = props.CentreOfMass()
    bounds = Bnd_Box()
    BRepBndLib.AddOptimal_s(shape, bounds, False, False)
    low, high = bounds.CornerMin(), bounds.CornerMax()
    coordinates = [low.X(), low.Y(), low.Z(), high.X(), high.Y(), high.Z()]
    explorer = TopExp_Explorer(shape, TopAbs_SOLID)
    count = 0
    while explorer.More():
        count += 1
        explorer.Next()
    face_explorer = TopExp_Explorer(shape, TopAbs_FACE)
    face_count = 0
    while face_explorer.More():
        face_count += 1
        face_explorer.Next()
    return {"valid": bool(BRepCheck_Analyzer(shape).IsValid()), "solid_count": count, "face_count": face_count,
            "volume_mm3": float(props.Mass()), "surface_area_mm2": float(surface.Mass()),
            "centroid_mm": [center.X(), center.Y(), center.Z()], "vertex_count": len(_shape_vertices(shape)), "bounds_mm": coordinates,
            "extents_mm": [coordinates[i + 3] - coordinates[i] for i in range(3)]}

def _bounds(unique: Any) -> Any:
    import numpy as np
    return np.concatenate([unique.min(axis=0), unique.max(axis=0)])

def _valid_solid_stats(stats: dict[str, Any]) -> bool:
    return (stats["valid"] is True and stats["solid_count"] == 1
            and math.isfinite(stats["volume_mm3"]) and stats["volume_mm3"] > 0
            and math.isfinite(stats["surface_area_mm2"]) and stats["surface_area_mm2"] > 0
            and all(math.isfinite(value) for value in stats["centroid_mm"])
            and all(math.isfinite(value) for value in stats["bounds_mm"])
            and all(math.isfinite(value) and value > 0 for value in stats["extents_mm"]))

def read_step(path: Path) -> dict[str, Any]:
    from OCP.IFSelect import IFSelect_RetDone
    from OCP.STEPControl import STEPControl_Reader
    reader = STEPControl_Reader()
    if reader.ReadFile(str(path)) != IFSelect_RetDone:
        raise GateFailure("STEP readback could not read the saved file", command="solid")
    reader.SetSystemLengthUnit(1.0)
    if reader.TransferRoots() < 1:
        raise GateFailure("STEP readback found no transferable roots", command="solid")
    return inspect_shape(reader.OneShape())

@dataclass(frozen=True)
class _Normalized:
    """STL welded into indexed vertices/faces plus the reference measurements the B-rep must keep."""
    unique: Any
    faces: Any
    span: float
    mesh: Any
    volume: float
    reversed_orientation: bool
    vertices_before: int


@dataclass(frozen=True)
class _Reference:
    bounds: Any
    volume: float
    area: float
    center: Any
    distance: float

    def preserved(self, stats: dict[str, Any]) -> tuple[bool, bool]:
        """Return (dimensions+volume kept, area+centroid kept) for measured B-rep stats."""
        import numpy as np
        shape_ok = (np.allclose(stats["bounds_mm"], self.bounds, atol=self.distance, rtol=0)
                    and abs(stats["volume_mm3"] - self.volume) <= self.volume * 1e-6)
        surface_ok = (abs(stats["surface_area_mm2"] - self.area) <= self.area * 1e-6
                      and np.allclose(stats["centroid_mm"], self.center, atol=self.distance, rtol=0))
        return bool(shape_ok), bool(surface_ok)


def _validate_request(source: Path, output: Path, units: str, tolerance: float, max_faces: int) -> None:
    if units not in UNITS or not math.isfinite(tolerance) or tolerance <= 0 or not 1 <= max_faces <= 50000:
        raise UsageError("units, positive finite tolerance and face budget are required", command="solid")
    if not source.is_file():
        raise InputNotFound(str(source), command="solid")
    if source.suffix.lower() != ".stl" or output.suffix.lower() not in (".step", ".stp"):
        raise UsageError("input must be STL and output must be STEP (.step or .stp)", command="solid")
    if output.exists() or output.is_symlink():
        raise UsageError("output already exists; choose a new path to preserve existing work", command="solid")
    if not output.parent.is_dir():
        raise UsageError("output directory does not exist", command="solid")
    if source.stat().st_size > MAX_BYTES:
        raise GateFailure("STL exceeds the 25 MiB input limit", command="solid")


def _require_dependencies() -> None:
    try:
        import numpy  # noqa: F401
        import trimesh  # noqa: F401
        from scipy.spatial import cKDTree  # noqa: F401
        from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakePolygon, BRepBuilderAPI_MakeSolid, BRepBuilderAPI_Sewing  # noqa: F401
        from OCP.IFSelect import IFSelect_RetDone  # noqa: F401
        from OCP.Interface import Interface_Static  # noqa: F401
        from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer  # noqa: F401
        from OCP.TopAbs import TopAbs_SHELL  # noqa: F401
        from OCP.TopExp import TopExp_Explorer  # noqa: F401
        from OCP.TopoDS import TopoDS  # noqa: F401
        from OCP.gp import gp_Pnt  # noqa: F401
    except ImportError as exc:
        raise MissingDependency("optional local solid-conversion dependencies", command="solid",
            install="uv sync --extra solid --python 3.12", degrades="STL-to-solid cannot run; no download was attempted") from exc


def _read_source(source: Path) -> tuple[bytes, str]:
    with source.open("rb") as stream:
        source_bytes = stream.read(MAX_BYTES + 1)
    if len(source_bytes) > MAX_BYTES:
        raise GateFailure("STL exceeds the 25 MiB input limit", command="solid")
    return source_bytes, hashlib.sha256(source_bytes).hexdigest()


def _load_scaled_vertices(source_bytes: bytes, units: str, max_faces: int) -> tuple[Any, Any]:
    import numpy as np
    import trimesh
    raw = trimesh.load_mesh(io.BytesIO(source_bytes), file_type="stl", process=False)
    if not isinstance(raw, trimesh.Trimesh) or not len(raw.faces):
        raise GateFailure("STL contains no triangle mesh", command="solid")
    if len(raw.faces) > max_faces:
        raise GateFailure(f"mesh has {len(raw.faces)} faces; budget is {max_faces}", command="solid")
    vertices = np.asarray(raw.vertices, dtype=np.float64) * UNITS[units]
    if not np.isfinite(vertices).all():
        raise GateFailure("mesh contains nonfinite coordinates", command="solid")
    return vertices, np.asarray(raw.faces)


def _weld_and_guard(vertices: Any, raw_faces: Any, tolerance: float) -> tuple[Any, Any, float]:
    import numpy as np
    # STL repeats vertex records. Weld exactly equal coordinates only, never by rounding.
    unique, inverse = np.unique(vertices, axis=0, return_inverse=True)
    faces = inverse[raw_faces]
    triangles = unique[faces]
    cross = np.cross(triangles[:, 1] - triangles[:, 0], triangles[:, 2] - triangles[:, 0])
    span = float(np.ptp(unique, axis=0).max())
    if span <= 0 or not math.isfinite(span):
        raise GateFailure("mesh has no finite extent", command="solid")
    if tolerance > span * 1e-4:
        raise UsageError("tolerance exceeds 0.01% of the model extent", command="solid")
    if np.any(np.linalg.norm(cross, axis=1) <= max(span * span * 1e-14, 1e-18)):
        raise GateFailure("mesh has degenerate triangles; no triangles were silently removed", command="solid")
    edge_lengths = np.linalg.norm(triangles - np.roll(triangles, 1, axis=1), axis=2)
    if np.any(edge_lengths <= 10 * tolerance):
        raise GateFailure("a triangle edge is below the 10-times-tolerance resolution guard; lower tolerance or repair the source", command="solid")
    if len(np.unique(np.sort(faces, axis=1), axis=0)) != len(faces):
        raise GateFailure("mesh contains duplicate triangles", command="solid")
    return unique, faces, span


def _normalize_mesh(source_bytes: bytes, units: str, tolerance: float, max_faces: int) -> _Normalized:
    import numpy as np
    import trimesh
    vertices, raw_faces = _load_scaled_vertices(source_bytes, units, max_faces)
    unique, faces, span = _weld_and_guard(vertices, raw_faces, tolerance)
    mesh = trimesh.Trimesh(vertices=unique, faces=faces, process=False)
    if not mesh.is_watertight or not mesh.is_winding_consistent:
        raise GateFailure("mesh must be closed and consistently wound; no hole filling was attempted", command="solid")
    components = trimesh.graph.connected_components(mesh.face_adjacency, nodes=np.arange(len(faces)), min_len=1)
    if len(components) != 1:
        raise GateFailure("multiple shells/components or cavities are not supported; convert separately", command="solid")
    volume = float(mesh.volume)
    if not math.isfinite(volume) or abs(volume) <= span ** 3 * 1e-12:
        raise GateFailure("mesh has zero or nonfinite signed volume", command="solid")
    reversed_orientation = volume < 0
    if reversed_orientation:
        faces = faces[:, ::-1]
    return _Normalized(unique, faces, span, mesh, abs(volume), reversed_orientation, len(vertices))


def _triangle_face(points: Any) -> Any:
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakePolygon
    from OCP.gp import gp_Pnt
    polygon = BRepBuilderAPI_MakePolygon()
    for vertex in points:
        polygon.Add(gp_Pnt(*[float(v) for v in vertex]))
    polygon.Close()
    if not polygon.IsDone():
        raise GateFailure("could not construct a triangle boundary", command="solid")
    builder = BRepBuilderAPI_MakeFace(polygon.Wire())
    if not builder.IsDone():
        raise GateFailure("could not construct a planar triangle face", command="solid")
    return builder.Face()


def _sewn_shell(unique: Any, faces: Any, tolerance: float) -> Any:
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Sewing
    from OCP.TopAbs import TopAbs_SHELL
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS
    sewing = BRepBuilderAPI_Sewing(tolerance)
    for face in faces:
        sewing.Add(_triangle_face(unique[face]))
    sewing.Perform()
    if sewing.NbFreeEdges() or sewing.NbMultipleEdges():
        raise GateFailure("sewn B-rep has open or multiply used edges", command="solid")
    explorer = TopExp_Explorer(sewing.SewedShape(), TopAbs_SHELL)
    shells = []
    while explorer.More():
        shells.append(TopoDS.Shell(explorer.Current()))
        explorer.Next()
    if len(shells) != 1:
        raise GateFailure("sewing did not produce exactly one shell", command="solid")
    return shells[0]


def _build_solid(norm: _Normalized, tolerance: float) -> tuple[Any, dict[str, Any], float]:
    """Sew the triangles into one solid and prove sewing moved no vertex beyond tolerance."""
    import numpy as np
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeSolid
    from scipy.spatial import cKDTree
    builder = BRepBuilderAPI_MakeSolid(_sewn_shell(norm.unique, norm.faces, tolerance))
    if not builder.IsDone():
        raise GateFailure("could not construct a solid from the shell", command="solid")
    solid = builder.Solid()
    built = inspect_shape(solid)
    if built["volume_mm3"] < 0:
        solid.Reverse()
        built = inspect_shape(solid)
    if not _valid_solid_stats(built):
        raise GateFailure("constructed B-rep failed solid validity checks", command="solid")
    if built["face_count"] != len(norm.faces):
        raise GateFailure("sewing changed the input triangle face count before an explicit merge", command="solid")
    brep_vertices = np.asarray(_shape_vertices(solid))
    if len(brep_vertices) != len(norm.unique):
        raise GateFailure("sewing changed the input vertex count", command="solid")
    vertex_delta = max(float(cKDTree(brep_vertices).query(norm.unique)[0].max()), float(cKDTree(norm.unique).query(brep_vertices)[0].max()))
    if vertex_delta > max(tolerance * 10, norm.span * 1e-8):
        raise GateFailure("sewn B-rep vertices moved beyond the accepted tolerance", command="solid")
    return solid, built, vertex_delta


def _unify_coplanar(solid: Any, tolerance: float, faces_before: int) -> tuple[Any, dict[str, Any]]:
    from OCP.ShapeUpgrade import ShapeUpgrade_UnifySameDomain
    merger = ShapeUpgrade_UnifySameDomain(solid, True, True, False)
    merger.SetLinearTolerance(tolerance)
    merger.SetAngularTolerance(1e-12)
    merger.Build()
    merged = merger.Shape()
    if merged.IsNull():
        raise GateFailure("coplanar unification returned no geometry", command="solid")
    built = inspect_shape(merged)
    if not _valid_solid_stats(built) or built["face_count"] > faces_before:
        raise GateFailure("coplanar unification did not preserve a valid single solid", command="solid")
    return merged, built


def _check_built_preserves(built: dict[str, Any], reference: _Reference) -> None:
    shape_ok, surface_ok = reference.preserved(built)
    if not shape_ok:
        raise GateFailure("constructed solid does not preserve mesh dimensions or volume", command="solid")
    if not surface_ok:
        raise GateFailure("constructed solid does not preserve surface area or centroid", command="solid")


def _write_step(solid: Any, temporary: Path) -> None:
    from OCP.IFSelect import IFSelect_RetDone
    from OCP.Interface import Interface_Static
    from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer
    Interface_Static.SetCVal_s("write.step.unit", "MM")
    writer = STEPControl_Writer()
    if writer.Transfer(solid, STEPControl_AsIs) != IFSelect_RetDone or writer.Write(str(temporary)) != IFSelect_RetDone:
        raise GateFailure("OpenCASCADE failed to write STEP", command="solid")


def _publish_step(solid: Any, output: Path, reference: _Reference, warnings: list[str]) -> tuple[dict[str, Any], str]:
    """Write to a temp STEP, re-read and re-verify it, then link it atomically to the output path."""
    with workspace(output.parent, ".3d-solid-", warnings) as temp:
        temporary = Path(temp) / "model.step"
        _write_step(solid, temporary)
        reopened = read_step(temporary)
        if not _valid_solid_stats(reopened):
            raise GateFailure("saved STEP failed independent validity checks", command="solid")
        shape_ok, surface_ok = reference.preserved(reopened)
        if not shape_ok:
            raise GateFailure("STEP roundtrip changed dimensions or volume beyond tolerance", command="solid")
        if not surface_ok:
            raise GateFailure("STEP roundtrip changed surface area or centroid beyond tolerance", command="solid")
        output_hash = hashlib.sha256(temporary.read_bytes()).hexdigest()
        # Same-filesystem link is atomic and fails if the destination appeared meanwhile.
        try:
            os.link(temporary, output)
        except OSError as exc:
            raise ThreeDError(f"could not publish STEP without overwriting existing data: {exc}", command="solid") from exc
    return reopened, output_hash


def convert_stl(source: Path, output: Path, *, units: str, tolerance: float = 1e-6, max_faces: int = 5000, merge_coplanar: bool = False) -> dict[str, Any]:
    _validate_request(source, output, units, tolerance, max_faces)
    _require_dependencies()
    source_bytes, source_hash = _read_source(source)
    norm = _normalize_mesh(source_bytes, units, tolerance, max_faces)
    solid, built, vertex_delta = _build_solid(norm, tolerance)
    faces_before_merge = built["face_count"]
    if merge_coplanar:
        solid, built = _unify_coplanar(solid, tolerance, faces_before_merge)
    reference = _Reference(
        bounds=_bounds(norm.unique), volume=norm.volume, area=float(norm.mesh.area),
        center=norm.mesh.center_mass, distance=max(tolerance * 10, norm.span * 1e-8))
    _check_built_preserves(built, reference)
    warnings: list[str] = []
    reopened, output_hash = _publish_step(solid, output, reference, warnings)
    return {"status": "CONVERTED", "representation": "faceted-brep", "source": str(source),
            "output": str(output), "input_units": units, "output_units": "mm", "tolerance_mm": tolerance,
            "input_triangle_count": len(norm.faces), "output_face_count": reopened["face_count"],
            "geometry_checks": {"max_premerge_vertex_delta_mm": vertex_delta, "relative_volume_tolerance": 1e-6, "relative_area_tolerance": 1e-6}, "warnings": warnings, "coplanar_merge": {"requested": merge_coplanar, "faces_before": faces_before_merge, "faces_after": built["face_count"]}, "solid_count": reopened["solid_count"], "volume_mm3": reopened["volume_mm3"],
            "extents_mm": reopened["extents_mm"], "step_roundtrip": reopened,
            "source_sha256": source_hash,
            "output_sha256": output_hash,
            "normalization": {"exact_coordinate_weld": True, "vertices_before": norm.vertices_before, "vertices_after": len(norm.unique), "orientation_reversed": norm.reversed_orientation},
            "not_verified": ["mesh self-intersections", "analytic surface recovery", "original feature history", "structural or manufacturing fitness"]}

def worker(request: dict[str, Any], report_path: Path) -> int:
    try:
        report = convert_stl(Path(request["source"]), Path(request["output"]), units=request["units"], tolerance=request["tolerance"], max_faces=request["max_faces"], merge_coplanar=request.get("merge_coplanar", False))
        code = 0
    except ThreeDError as exc:
        report = {"status": "FAILED", "error": exc.message, "remediation": exc.remediation}
        code = exc.exit_code
    except Exception as exc:
        report = {"status": "FAILED", "error": f"geometry conversion failed: {type(exc).__name__}: {exc}"}
        code = 1
    report_path.write_text(json.dumps(report, allow_nan=False), encoding="utf-8")
    return code

if __name__ == "__main__":
    if len(sys.argv) != 4 or sys.argv[1] != "--worker":
        raise SystemExit("Use 3d solid --help")
    raise SystemExit(worker(json.loads(sys.argv[2]), Path(sys.argv[3])))
