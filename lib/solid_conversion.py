"""Bounded STL -> faceted B-rep conversion; no implicit repair or network access."""
from __future__ import annotations
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

def convert_stl(source: Path, output: Path, *, units: str, tolerance: float = 1e-6, max_faces: int = 5000, merge_coplanar: bool = False) -> dict[str, Any]:
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
    try:
        import numpy as np
        import trimesh
        from scipy.spatial import cKDTree
        from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakePolygon, BRepBuilderAPI_MakeSolid, BRepBuilderAPI_Sewing
        from OCP.IFSelect import IFSelect_RetDone
        from OCP.Interface import Interface_Static
        from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer
        from OCP.TopAbs import TopAbs_SHELL
        from OCP.TopExp import TopExp_Explorer
        from OCP.TopoDS import TopoDS
        from OCP.gp import gp_Pnt
    except ImportError as exc:
        raise MissingDependency("optional local solid-conversion dependencies", command="solid",
            install="uv sync --extra solid --python 3.12", degrades="STL-to-solid cannot run; no download was attempted") from exc
    with source.open("rb") as stream:
        source_bytes = stream.read(MAX_BYTES + 1)
    if len(source_bytes) > MAX_BYTES:
        raise GateFailure("STL exceeds the 25 MiB input limit", command="solid")
    source_hash = hashlib.sha256(source_bytes).hexdigest()
    raw = trimesh.load_mesh(io.BytesIO(source_bytes), file_type="stl", process=False)
    if not isinstance(raw, trimesh.Trimesh) or not len(raw.faces):
        raise GateFailure("STL contains no triangle mesh", command="solid")
    if len(raw.faces) > max_faces:
        raise GateFailure(f"mesh has {len(raw.faces)} faces; budget is {max_faces}", command="solid")
    vertices = np.asarray(raw.vertices, dtype=np.float64) * UNITS[units]
    if not np.isfinite(vertices).all():
        raise GateFailure("mesh contains nonfinite coordinates", command="solid")
    # STL repeats vertex records. Weld exactly equal coordinates only, never by rounding.
    unique, inverse = np.unique(vertices, axis=0, return_inverse=True)
    faces = inverse[np.asarray(raw.faces)]
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
    volume = abs(volume)
    sewing = BRepBuilderAPI_Sewing(tolerance)
    for face in faces:
        polygon = BRepBuilderAPI_MakePolygon()
        for vertex in unique[face]:
            polygon.Add(gp_Pnt(*[float(v) for v in vertex]))
        polygon.Close()
        if not polygon.IsDone():
            raise GateFailure("could not construct a triangle boundary", command="solid")
        builder = BRepBuilderAPI_MakeFace(polygon.Wire())
        if not builder.IsDone():
            raise GateFailure("could not construct a planar triangle face", command="solid")
        sewing.Add(builder.Face())
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
    builder = BRepBuilderAPI_MakeSolid(shells[0])
    if not builder.IsDone():
        raise GateFailure("could not construct a solid from the shell", command="solid")
    solid = builder.Solid()
    built = inspect_shape(solid)
    if built["volume_mm3"] < 0:
        solid.Reverse()
        built = inspect_shape(solid)
    if not _valid_solid_stats(built):
        raise GateFailure("constructed B-rep failed solid validity checks", command="solid")
    if built["face_count"] != len(faces):
        raise GateFailure("sewing changed the input triangle face count before an explicit merge", command="solid")
    brep_vertices = np.asarray(_shape_vertices(solid))
    allowed_distance = max(tolerance * 10, span * 1e-8)
    if len(brep_vertices) != len(unique):
        raise GateFailure("sewing changed the input vertex count", command="solid")
    vertex_delta = max(float(cKDTree(brep_vertices).query(unique)[0].max()), float(cKDTree(unique).query(brep_vertices)[0].max()))
    if vertex_delta > allowed_distance:
        raise GateFailure("sewn B-rep vertices moved beyond the accepted tolerance", command="solid")
    faces_before_merge = built["face_count"]
    if merge_coplanar:
        from OCP.ShapeUpgrade import ShapeUpgrade_UnifySameDomain
        merger = ShapeUpgrade_UnifySameDomain(solid, True, True, False)
        merger.SetLinearTolerance(tolerance)
        merger.SetAngularTolerance(1e-12)
        merger.Build()
        solid = merger.Shape()
        if solid.IsNull():
            raise GateFailure("coplanar unification returned no geometry", command="solid")
        built = inspect_shape(solid)
        if not _valid_solid_stats(built) or built["face_count"] > faces_before_merge:
            raise GateFailure("coplanar unification did not preserve a valid single solid", command="solid")
    expected_bounds = np.concatenate([unique.min(axis=0), unique.max(axis=0)])
    allowed_distance = max(tolerance * 10, span * 1e-8)
    allowed_volume = volume * 1e-6
    area = float(mesh.area)
    allowed_area = area * 1e-6
    expected_center = np.asarray(mesh.center_mass)
    if not np.allclose(built["bounds_mm"], expected_bounds, atol=allowed_distance, rtol=0) or abs(built["volume_mm3"] - volume) > allowed_volume:
        raise GateFailure("constructed solid does not preserve mesh dimensions or volume", command="solid")
    if abs(built["surface_area_mm2"] - area) > allowed_area or not np.allclose(built["centroid_mm"], expected_center, atol=allowed_distance, rtol=0):
        raise GateFailure("constructed solid does not preserve surface area or centroid", command="solid")
    warnings: list[str] = []
    with workspace(output.parent, ".3d-solid-", warnings) as temp:
        temporary = Path(temp) / "model.step"
        Interface_Static.SetCVal_s("write.step.unit", "MM")
        writer = STEPControl_Writer()
        if writer.Transfer(solid, STEPControl_AsIs) != IFSelect_RetDone or writer.Write(str(temporary)) != IFSelect_RetDone:
            raise GateFailure("OpenCASCADE failed to write STEP", command="solid")
        reopened = read_step(temporary)
        if not _valid_solid_stats(reopened):
            raise GateFailure("saved STEP failed independent validity checks", command="solid")
        if not np.allclose(reopened["bounds_mm"], expected_bounds, atol=allowed_distance, rtol=0) or abs(reopened["volume_mm3"] - volume) > allowed_volume:
            raise GateFailure("STEP roundtrip changed dimensions or volume beyond tolerance", command="solid")
        if abs(reopened["surface_area_mm2"] - area) > allowed_area or not np.allclose(reopened["centroid_mm"], expected_center, atol=allowed_distance, rtol=0):
            raise GateFailure("STEP roundtrip changed surface area or centroid beyond tolerance", command="solid")
        output_hash = hashlib.sha256(temporary.read_bytes()).hexdigest()
        # Same-filesystem link is atomic and fails if the destination appeared meanwhile.
        try:
            os.link(temporary, output)
        except OSError as exc:
            raise ThreeDError(f"could not publish STEP without overwriting existing data: {exc}", command="solid") from exc
    return {"status": "CONVERTED", "representation": "faceted-brep", "source": str(source),
            "output": str(output), "input_units": units, "output_units": "mm", "tolerance_mm": tolerance,
            "input_triangle_count": len(faces), "output_face_count": reopened["face_count"],
            "geometry_checks": {"max_premerge_vertex_delta_mm": vertex_delta, "relative_volume_tolerance": 1e-6, "relative_area_tolerance": 1e-6}, "warnings": warnings, "coplanar_merge": {"requested": merge_coplanar, "faces_before": faces_before_merge, "faces_after": built["face_count"]}, "solid_count": reopened["solid_count"], "volume_mm3": reopened["volume_mm3"],
            "extents_mm": reopened["extents_mm"], "step_roundtrip": reopened,
            "source_sha256": source_hash,
            "output_sha256": output_hash,
            "normalization": {"exact_coordinate_weld": True, "vertices_before": len(vertices), "vertices_after": len(unique), "orientation_reversed": reversed_orientation},
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
