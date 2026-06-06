#!/usr/bin/env python3
"""Proxy mesh alignment for image-to-3D reference matching.

This tool compares a CAD mesh with a rough generated proxy mesh, estimates a transform
from proxy -> CAD, and writes deterministic scores/artifacts. It is intentionally
provider-agnostic: TRELLIS, ZeroGPU, manual GLB files, and future local image-to-3D
models all feed the same mesh alignment core.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import trimesh
from scipy.spatial import cKDTree


@dataclass(frozen=True)
class MeshCloud:
    path: Path
    points: np.ndarray
    mesh_vertices: np.ndarray
    mesh_faces: np.ndarray
    bbox_center: np.ndarray
    bbox_extents: np.ndarray
    bbox_diagonal: float
    centroid: np.ndarray
    n_vertices: int
    n_faces: int
    components: int
    euler_number: int | None
    watertight: bool
    radial_histogram: np.ndarray


@dataclass(frozen=True)
class Candidate:
    yaw: float
    pitch: float
    roll: float
    scale: float
    rotation: np.ndarray
    translation: np.ndarray
    chamfer_mean: float
    chamfer_p95: float
    hausdorff_max: float
    radial_histogram_l2: float
    topology_penalty: float

    @property
    def objective(self) -> float:
        return self.chamfer_mean + 0.15 * self.radial_histogram_l2 + self.topology_penalty


@dataclass(frozen=True)
class ProjectionScore:
    name: str
    edge_f1_at_3: float
    edge_chamfer_px: float
    coverage_ratio: float
    centroid_delta_px: float


@dataclass(frozen=True)
class QualityGate:
    status: str
    reasons: tuple[str, ...]
    projection_scores: tuple[ProjectionScore, ...]
    projection_edge_f1_at_3: float
    projection_edge_chamfer_px: float
    projection_coverage_ratio: float
    candidate_objective_gap: float | None


def _as_mesh(loaded: Any) -> trimesh.Trimesh:
    if isinstance(loaded, trimesh.Scene):
        mesh = loaded.dump(concatenate=True)
    else:
        mesh = loaded
    if not isinstance(mesh, trimesh.Trimesh) or mesh.is_empty:
        raise ValueError("input did not load as a non-empty mesh")
    return mesh


def _components(mesh: trimesh.Trimesh) -> int:
    try:
        return int(len(mesh.split(only_watertight=False)))
    except Exception:
        return 1


def _euler_number(mesh: trimesh.Trimesh) -> int | None:
    try:
        value = getattr(mesh, "euler_number")
        return int(value)
    except Exception:
        return None


def _sample_mesh(mesh: trimesh.Trimesh, samples: int, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    try:
        points, _faces = trimesh.sample.sample_surface(mesh, samples, seed=rng)
    except TypeError:
        np.random.seed(seed)
        points, _faces = trimesh.sample.sample_surface(mesh, samples)
    return np.asarray(points, dtype=np.float64)


def _radial_histogram(points: np.ndarray, bins: int = 24) -> np.ndarray:
    radii = np.linalg.norm(points, axis=1)
    max_radius = max(float(radii.max(initial=0.0)), 1e-9)
    hist, _edges = np.histogram(radii / max_radius, bins=bins, range=(0.0, 1.0), density=False)
    hist = hist.astype(np.float64)
    total = float(hist.sum())
    return hist / total if total else hist


def load_cloud(path: Path, samples: int, seed: int) -> MeshCloud:
    mesh = _as_mesh(trimesh.load(path, force="mesh"))
    bounds = np.asarray(mesh.bounds, dtype=np.float64)
    center = (bounds[0] + bounds[1]) / 2.0
    extents = np.maximum(bounds[1] - bounds[0], 1e-9)
    diagonal = float(np.linalg.norm(extents))
    scale = diagonal if diagonal > 1e-9 else 1.0

    points = (_sample_mesh(mesh, samples, seed) - center) / scale
    vertices = (np.asarray(mesh.vertices, dtype=np.float64) - center) / scale
    centroid = np.asarray(mesh.centroid, dtype=np.float64)
    return MeshCloud(
        path=path,
        points=points,
        mesh_vertices=vertices,
        mesh_faces=np.asarray(mesh.faces, dtype=np.int64),
        bbox_center=center,
        bbox_extents=extents,
        bbox_diagonal=diagonal,
        centroid=centroid,
        n_vertices=int(len(mesh.vertices)),
        n_faces=int(len(mesh.faces)),
        components=_components(mesh),
        euler_number=_euler_number(mesh),
        watertight=bool(mesh.is_watertight),
        radial_histogram=_radial_histogram(points),
    )


def rotation_matrix(yaw: float, pitch: float, roll: float) -> np.ndarray:
    y, p, r = [math.radians(v) for v in (yaw, pitch, roll)]
    cz, sz = math.cos(y), math.sin(y)
    cy, sy = math.cos(p), math.sin(p)
    cx, sx = math.cos(r), math.sin(r)
    rz = np.array([[cz, -sz, 0.0], [sz, cz, 0.0], [0.0, 0.0, 1.0]])
    ry = np.array([[cy, 0.0, sy], [0.0, 1.0, 0.0], [-sy, 0.0, cy]])
    rx = np.array([[1.0, 0.0, 0.0], [0.0, cx, -sx], [0.0, sx, cx]])
    return rz @ ry @ rx


def _umeyama(source: np.ndarray, target: np.ndarray) -> tuple[float, np.ndarray, np.ndarray]:
    src_mean = source.mean(axis=0)
    tgt_mean = target.mean(axis=0)
    src0 = source - src_mean
    tgt0 = target - tgt_mean
    cov = (tgt0.T @ src0) / max(len(source), 1)
    u, singular, vh = np.linalg.svd(cov)
    sign = np.ones(3)
    if np.linalg.det(u @ vh) < 0:
        sign[-1] = -1
    rot = u @ np.diag(sign) @ vh
    variance = float((src0 * src0).sum() / max(len(source), 1))
    scale = float((singular * sign).sum() / variance) if variance > 1e-12 else 1.0
    trans = tgt_mean - scale * (src_mean @ rot.T)
    return scale, rot, trans


def _chamfer(a: np.ndarray, b: np.ndarray) -> tuple[float, float, float]:
    tree_b = cKDTree(b)
    tree_a = cKDTree(a)
    da, _ = tree_b.query(a, k=1)
    db, _ = tree_a.query(b, k=1)
    both = np.concatenate([da, db])
    return float(both.mean()), float(np.percentile(both, 95)), float(both.max(initial=0.0))


def _edge(mask: np.ndarray) -> np.ndarray:
    from scipy.ndimage import binary_erosion

    return mask & ~binary_erosion(mask)


def _centroid(mask: np.ndarray) -> np.ndarray:
    coords = np.argwhere(mask)
    if len(coords) == 0:
        return np.array([math.inf, math.inf], dtype=np.float64)
    return coords.mean(axis=0)


def _rasterize_projection(
    points: np.ndarray,
    axes: tuple[int, int],
    bounds: tuple[np.ndarray, np.ndarray],
    size: int = 256,
    faces: np.ndarray | None = None,
) -> np.ndarray:
    xy = points[:, axes]
    lo, hi = bounds
    span = np.maximum(hi - lo, 1e-9)
    norm = np.clip((xy - lo) / span, 0.0, 1.0)
    pixel_points = np.column_stack((norm[:, 0] * (size - 1), (1.0 - norm[:, 1]) * (size - 1)))
    if faces is not None and len(faces) > 0:
        mask = np.zeros((size, size), dtype=bool)
        for face in faces:
            triangle = pixel_points[face]
            _fill_triangle_mask(mask, triangle)
        return mask
    px = np.rint(pixel_points[:, 0]).astype(np.int64)
    py = np.rint(pixel_points[:, 1]).astype(np.int64)
    mask = np.zeros((size, size), dtype=bool)
    mask[py, px] = True
    return mask


def _triangle_mask(triangle: np.ndarray, size: int) -> np.ndarray:
    mask = np.zeros((size, size), dtype=bool)
    _fill_triangle_mask(mask, triangle)
    return mask


def _fill_triangle_mask(mask: np.ndarray, triangle: np.ndarray) -> None:
    size = mask.shape[0]
    min_x = max(int(math.floor(float(triangle[:, 0].min()))), 0)
    max_x = min(int(math.ceil(float(triangle[:, 0].max()))), size - 1)
    min_y = max(int(math.floor(float(triangle[:, 1].min()))), 0)
    max_y = min(int(math.ceil(float(triangle[:, 1].max()))), size - 1)
    if min_x > max_x or min_y > max_y:
        return
    yy, xx = np.mgrid[min_y : max_y + 1, min_x : max_x + 1]
    x0, y0 = triangle[0]
    x1, y1 = triangle[1]
    x2, y2 = triangle[2]
    denom = (y1 - y2) * (x0 - x2) + (x2 - x1) * (y0 - y2)
    if abs(float(denom)) < 1e-9:
        return
    a = ((y1 - y2) * (xx - x2) + (x2 - x1) * (yy - y2)) / denom
    b = ((y2 - y0) * (xx - x2) + (x0 - x2) * (yy - y2)) / denom
    c = 1.0 - a - b
    local = (a >= -1e-9) & (b >= -1e-9) & (c >= -1e-9)
    mask[min_y : max_y + 1, min_x : max_x + 1] |= local


def _edge_f1(render_edge: np.ndarray, ref_edge: np.ndarray, tolerance_px: float) -> float:
    from scipy.ndimage import distance_transform_edt

    if not render_edge.any() or not ref_edge.any():
        return 1.0 if not render_edge.any() and not ref_edge.any() else 0.0
    dist_to_ref = distance_transform_edt(~ref_edge)
    dist_to_render = distance_transform_edt(~render_edge)
    precision = float((dist_to_ref[render_edge] <= tolerance_px).mean()) if render_edge.any() else 0.0
    recall = float((dist_to_render[ref_edge] <= tolerance_px).mean()) if ref_edge.any() else 0.0
    return 0.0 if precision + recall == 0.0 else 2.0 * precision * recall / (precision + recall)


def _edge_chamfer_px(a_edge: np.ndarray, b_edge: np.ndarray) -> float:
    from scipy.ndimage import distance_transform_edt

    if not a_edge.any() or not b_edge.any():
        return math.inf
    dist_to_b = distance_transform_edt(~b_edge)
    dist_to_a = distance_transform_edt(~a_edge)
    return float((dist_to_b[a_edge].mean() + dist_to_a[b_edge].mean()) / 2.0)


def _projection_score(
    cad_vertices: np.ndarray,
    cad_faces: np.ndarray,
    aligned_vertices: np.ndarray,
    proxy_faces: np.ndarray,
    axes: tuple[int, int],
    name: str,
) -> ProjectionScore:
    bounds = _projection_bounds(cad_vertices, aligned_vertices, axes)
    cad_mask = _rasterize_projection(cad_vertices, axes, bounds, faces=cad_faces)
    proxy_mask = _rasterize_projection(aligned_vertices, axes, bounds, faces=proxy_faces)
    cad_edge = _edge(cad_mask)
    proxy_edge = _edge(proxy_mask)
    cad_area = int(cad_mask.sum())
    proxy_area = int(proxy_mask.sum())
    coverage = float(proxy_area / cad_area) if cad_area else math.inf
    return ProjectionScore(
        name=name,
        edge_f1_at_3=_edge_f1(proxy_edge, cad_edge, 3.0),
        edge_chamfer_px=_edge_chamfer_px(proxy_edge, cad_edge),
        coverage_ratio=coverage,
        centroid_delta_px=float(np.linalg.norm(_centroid(proxy_mask) - _centroid(cad_mask))),
    )


def _quality_gate(cad: MeshCloud, proxy: MeshCloud, candidates: list[Candidate]) -> QualityGate:
    best = candidates[0]
    aligned = best.scale * (proxy.mesh_vertices @ best.rotation.T) + best.translation
    projection_scores = (
        _projection_score(cad.mesh_vertices, cad.mesh_faces, aligned, proxy.mesh_faces, (0, 1), "XY"),
        _projection_score(cad.mesh_vertices, cad.mesh_faces, aligned, proxy.mesh_faces, (0, 2), "XZ"),
        _projection_score(cad.mesh_vertices, cad.mesh_faces, aligned, proxy.mesh_faces, (1, 2), "YZ"),
    )
    edge_f1 = float(min(score.edge_f1_at_3 for score in projection_scores))
    edge_chamfer = float(max(score.edge_chamfer_px for score in projection_scores))
    coverages = [score.coverage_ratio for score in projection_scores if math.isfinite(score.coverage_ratio)]
    coverage_ratio = float(max(abs(value - 1.0) for value in coverages)) if coverages else math.inf
    gap = None
    if len(candidates) > 1 and candidates[1].objective > 1e-12:
        gap = float((candidates[1].objective - best.objective) / candidates[1].objective)

    reject: list[str] = []
    warn: list[str] = []
    if best.chamfer_p95 > 0.14:
        reject.append(f"3D chamfer_p95={best.chamfer_p95:.3f} > 0.140")
    elif best.chamfer_p95 > 0.08:
        warn.append(f"3D chamfer_p95={best.chamfer_p95:.3f} > 0.080")
    if edge_f1 < 0.55:
        reject.append(f"projection edge_f1@3={edge_f1:.3f} < 0.550")
    elif edge_f1 < 0.72:
        warn.append(f"projection edge_f1@3={edge_f1:.3f} < 0.720")
    if edge_chamfer > 18.0:
        reject.append(f"projection edge_chamfer_px={edge_chamfer:.1f} > 18.0")
    elif edge_chamfer > 9.0:
        warn.append(f"projection edge_chamfer_px={edge_chamfer:.1f} > 9.0")
    if coverage_ratio > 0.45:
        reject.append(f"projection coverage drift={coverage_ratio:.2f} > 0.45")
    elif coverage_ratio > 0.25:
        warn.append(f"projection coverage drift={coverage_ratio:.2f} > 0.25")
    if best.topology_penalty >= 0.08:
        warn.append(f"topology_penalty={best.topology_penalty:.3f}")
    if gap is not None and gap < 0.04:
        warn.append(f"candidate objective gap={gap:.3f} < 0.040; pose may be ambiguous")

    status = "reject" if reject else "warning" if warn else "ok"
    return QualityGate(
        status=status,
        reasons=tuple(reject + warn),
        projection_scores=projection_scores,
        projection_edge_f1_at_3=edge_f1,
        projection_edge_chamfer_px=edge_chamfer,
        projection_coverage_ratio=coverage_ratio,
        candidate_objective_gap=gap,
    )


def _topology_penalty(cad: MeshCloud, proxy: MeshCloud) -> float:
    penalty = 0.0
    if cad.components != proxy.components:
        penalty += 0.08 * abs(cad.components - proxy.components)
    if cad.euler_number is not None and proxy.euler_number is not None:
        penalty += 0.02 * min(abs(cad.euler_number - proxy.euler_number), 5)
    if cad.watertight != proxy.watertight:
        penalty += 0.03
    return penalty


def evaluate_candidate(
    cad: MeshCloud,
    proxy: MeshCloud,
    yaw: float,
    pitch: float,
    roll: float,
    icp_steps: int,
) -> Candidate:
    base_rot = rotation_matrix(yaw, pitch, roll)
    moving = proxy.points @ base_rot.T
    target = cad.points
    scale = 1.0
    rot = np.eye(3)
    trans = np.zeros(3)
    target_tree = cKDTree(target)

    for _step in range(max(0, icp_steps)):
        current = scale * (moving @ rot.T) + trans
        _dist, idx = target_tree.query(current, k=1)
        next_scale, next_rot, next_trans = _umeyama(moving, target[idx])
        if not np.isfinite(next_scale):
            break
        scale, rot, trans = next_scale, next_rot, next_trans

    aligned = scale * (moving @ rot.T) + trans
    chamfer_mean, chamfer_p95, hausdorff_max = _chamfer(aligned, target)
    total_rot = rot @ base_rot
    radial_l2 = float(np.linalg.norm(cad.radial_histogram - proxy.radial_histogram))
    return Candidate(
        yaw=yaw,
        pitch=pitch,
        roll=roll,
        scale=scale,
        rotation=total_rot,
        translation=trans,
        chamfer_mean=chamfer_mean,
        chamfer_p95=chamfer_p95,
        hausdorff_max=hausdorff_max,
        radial_histogram_l2=radial_l2,
        topology_penalty=_topology_penalty(cad, proxy),
    )


def parse_values(raw: str) -> list[float]:
    values = [float(part.strip()) for part in raw.split(",") if part.strip()]
    if not values:
        raise argparse.ArgumentTypeError("expected at least one comma-separated number")
    return values


def yaw_values(step: float) -> list[float]:
    if not math.isfinite(step) or step < 1.0 or step > 360:
        raise argparse.ArgumentTypeError("--yaw-step must be in [1, 360]")
    count = max(1, int(math.ceil(360.0 / step)))
    return [round(i * step, 8) for i in range(count) if i * step < 360.0]


def _original_space_transform(candidate: Candidate, cad: MeshCloud, proxy: MeshCloud) -> dict[str, Any]:
    scale = float(candidate.scale * cad.bbox_diagonal / proxy.bbox_diagonal)
    rotation = candidate.rotation
    translation = cad.bbox_center + cad.bbox_diagonal * candidate.translation - scale * (proxy.bbox_center @ rotation.T)
    return {
        "convention": "row_vector: cad_point = proxy_point @ matrix_3x3 + translation",
        "matrix_3x3": (scale * rotation.T).tolist(),
        "uniform_scale": scale,
        "rotation_matrix_internal": rotation.tolist(),
        "rotation_matrix_internal_convention": "normalized_aligned = scale * (normalized_proxy @ rotation_matrix_internal.T) + translation",
        "translation": translation.tolist(),
    }


def apply_original_transform(points: np.ndarray, candidate: Candidate, cad: MeshCloud, proxy: MeshCloud) -> np.ndarray:
    scale = float(candidate.scale * cad.bbox_diagonal / proxy.bbox_diagonal)
    matrix = scale * candidate.rotation.T
    translation = cad.bbox_center + cad.bbox_diagonal * candidate.translation - scale * (proxy.bbox_center @ candidate.rotation.T)
    return points @ matrix + translation


def _candidate_dict(candidate: Candidate, cad: MeshCloud, proxy: MeshCloud) -> dict[str, Any]:
    return {
        "initial_rotation_deg": {
            "yaw": candidate.yaw,
            "pitch": candidate.pitch,
            "roll": candidate.roll,
        },
        "transform_proxy_to_cad_normalized": {
            "convention": "row_vector: normalized_cad_point = normalized_proxy_point @ matrix_3x3 + translation",
            "matrix_3x3": (candidate.scale * candidate.rotation.T).tolist(),
            "scale": candidate.scale,
            "rotation_matrix_internal": candidate.rotation.tolist(),
            "rotation_matrix_internal_convention": "normalized_aligned = scale * (normalized_proxy @ rotation_matrix_internal.T) + translation",
            "translation": candidate.translation.tolist(),
        },
        "transform_proxy_to_cad_original": _original_space_transform(candidate, cad, proxy),
        "error": {
            "objective": candidate.objective,
            "chamfer_mean": candidate.chamfer_mean,
            "chamfer_p95": candidate.chamfer_p95,
            "hausdorff_max": candidate.hausdorff_max,
            "radial_histogram_l2": candidate.radial_histogram_l2,
            "topology_penalty": candidate.topology_penalty,
        },
    }


def _projection_dict(score: ProjectionScore) -> dict[str, Any]:
    return {
        "name": score.name,
        "edge_f1@3": _json_float(score.edge_f1_at_3),
        "edge_chamfer_px": _json_float(score.edge_chamfer_px),
        "coverage_ratio": _json_float(score.coverage_ratio),
        "centroid_delta_px": _json_float(score.centroid_delta_px),
    }


def _json_float(value: float | None) -> float | None:
    return value if value is not None and math.isfinite(value) else None


def _quality_dict(gate: QualityGate) -> dict[str, Any]:
    return {
        "status": gate.status,
        "reasons": list(gate.reasons),
        "projection_edge_f1@3_min": _json_float(gate.projection_edge_f1_at_3),
        "projection_edge_chamfer_px_max": _json_float(gate.projection_edge_chamfer_px),
        "projection_coverage_drift_max": _json_float(gate.projection_coverage_ratio),
        "candidate_objective_gap": _json_float(gate.candidate_objective_gap),
        "projection_scores": [_projection_dict(score) for score in gate.projection_scores],
    }


def _mesh_dict(cloud: MeshCloud) -> dict[str, Any]:
    return {
        "path": str(cloud.path),
        "vertices": cloud.n_vertices,
        "faces": cloud.n_faces,
        "components": cloud.components,
        "euler_number": cloud.euler_number,
        "watertight": cloud.watertight,
        "bbox_center": cloud.bbox_center.tolist(),
        "bbox_extents": cloud.bbox_extents.tolist(),
        "bbox_diagonal": cloud.bbox_diagonal,
    }


def _project(
    points: np.ndarray,
    axes: tuple[int, int],
    size: int = 420,
    bounds: tuple[np.ndarray, np.ndarray] | None = None,
) -> list[tuple[float, float]]:
    xy = points[:, axes]
    if bounds is None:
        lo = xy.min(axis=0)
        hi = xy.max(axis=0)
    else:
        lo, hi = bounds
    span = np.maximum(hi - lo, 1e-9)
    norm = (xy - lo) / span
    pad = 24
    return [(float(pad + x * (size - 2 * pad)), float(size - (pad + y * (size - 2 * pad)))) for x, y in norm]


def _projection_bounds(cad: np.ndarray, aligned: np.ndarray, axes: tuple[int, int]) -> tuple[np.ndarray, np.ndarray]:
    xy = np.vstack([cad[:, axes], aligned[:, axes]])
    lo = xy.min(axis=0)
    hi = xy.max(axis=0)
    pad = np.maximum((hi - lo) * 0.05, 1e-6)
    return lo - pad, hi + pad


def _draw_projection(
    draw: Any,
    cad: np.ndarray,
    aligned: np.ndarray,
    axes: tuple[int, int],
    origin: tuple[int, int],
    title: str,
) -> None:
    ox, oy = origin
    size = 420
    draw.rectangle([ox, oy, ox + size, oy + size], fill=(250, 250, 250), outline=(180, 180, 180))
    draw.text((ox + 12, oy + 10), title, fill=(20, 20, 20))
    bounds = _projection_bounds(cad, aligned, axes)
    for x, y in _project(cad, axes, size=size, bounds=bounds):
        draw.point((ox + x, oy + y), fill=(220, 40, 40))
    for x, y in _project(aligned, axes, size=size, bounds=bounds):
        draw.point((ox + x, oy + y), fill=(20, 120, 220))
    draw.text((ox + 12, oy + size - 24), "red=CAD  blue=aligned proxy", fill=(20, 20, 20))


def write_proof(cad: MeshCloud, proxy: MeshCloud, best: Candidate, gate: QualityGate, out_dir: Path) -> Path:
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return write_svg_proof(cad, proxy, best, gate, out_dir)

    aligned = best.scale * (proxy.points @ best.rotation.T) + best.translation
    image = Image.new("RGB", (840, 900), "white")
    draw = ImageDraw.Draw(image)
    _draw_projection(draw, cad.points, aligned, (0, 1), (0, 0), "XY projection")
    _draw_projection(draw, cad.points, aligned, (0, 2), (420, 0), "XZ projection")
    _draw_projection(draw, cad.points, aligned, (1, 2), (0, 420), "YZ projection")
    draw.text((432, 448), "Best proxy -> CAD alignment", fill=(20, 20, 20))
    draw.text((432, 474), f"Chamfer mean: {best.chamfer_mean:.5f}", fill=(20, 20, 20))
    draw.text((432, 498), f"Chamfer p95:  {best.chamfer_p95:.5f}", fill=(20, 20, 20))
    draw.text((432, 522), f"Objective:     {best.objective:.5f}", fill=(20, 20, 20))
    draw.text((432, 546), f"Initial yaw/pitch/roll: {best.yaw:g}/{best.pitch:g}/{best.roll:g}", fill=(20, 20, 20))
    draw.text((432, 584), f"Quality gate: {gate.status}", fill=(20, 20, 20))
    draw.text((432, 608), f"Min edge F1@3: {gate.projection_edge_f1_at_3:.3f}", fill=(20, 20, 20))
    draw.text((432, 632), f"Max edge Chamfer px: {gate.projection_edge_chamfer_px:.2f}", fill=(20, 20, 20))
    reason = gate.reasons[0] if gate.reasons else "proxy accepted as a coarse prior"
    draw.text((432, 670), reason[:62], fill=(150, 45, 45) if gate.reasons else (45, 110, 45))
    draw.text((432, 708), "Reject/warn before fit-camera if proxy render contours do not match.", fill=(70, 70, 70))
    path = out_dir / "alignment_proof.png"
    image.save(path)
    return path


def _svg_points(points: np.ndarray, axes: tuple[int, int], color: str, offset_x: int, offset_y: int) -> str:
    projected = _project(points, axes, size=360)
    return "\n".join(
        f'<circle cx="{offset_x + x:.1f}" cy="{offset_y + y:.1f}" r="0.9" fill="{color}" />'
        for x, y in projected[:: max(1, len(projected) // 700)]
    )


def _svg_points_with_bounds(
    points: np.ndarray,
    axes: tuple[int, int],
    bounds: tuple[np.ndarray, np.ndarray],
    color: str,
    offset_x: int,
    offset_y: int,
) -> str:
    projected = _project(points, axes, size=360, bounds=bounds)
    return "\n".join(
        f'<circle cx="{offset_x + x:.1f}" cy="{offset_y + y:.1f}" r="0.9" fill="{color}" />'
        for x, y in projected[:: max(1, len(projected) // 700)]
    )


def write_svg_proof(cad: MeshCloud, proxy: MeshCloud, best: Candidate, gate: QualityGate, out_dir: Path) -> Path:
    aligned = best.scale * (proxy.points @ best.rotation.T) + best.translation
    panels = [
        ("XY projection", (0, 1), 0, 0),
        ("XZ projection", (0, 2), 380, 0),
        ("YZ projection", (1, 2), 0, 380),
    ]
    body = [
        '<svg xmlns="http://www.w3.org/2000/svg" width="760" height="800" viewBox="0 0 760 800">',
        '<rect width="760" height="800" fill="white" />',
    ]
    for title, axes, ox, oy in panels:
        body.append(f'<rect x="{ox}" y="{oy}" width="360" height="360" fill="#fafafa" stroke="#bbb" />')
        body.append(f'<text x="{ox + 12}" y="{oy + 24}" font-size="14" fill="#222">{title}</text>')
        bounds = _projection_bounds(cad.points, aligned, axes)
        body.append(_svg_points_with_bounds(cad.points, axes, bounds, "#dc2828", ox, oy))
        body.append(_svg_points_with_bounds(aligned, axes, bounds, "#1478dc", ox, oy))
        body.append(f'<text x="{ox + 12}" y="{oy + 344}" font-size="12" fill="#222">red=CAD blue=aligned proxy</text>')
    body.extend(
        [
            '<text x="392" y="404" font-size="16" fill="#222">Best proxy -&gt; CAD alignment</text>',
            f'<text x="392" y="432" font-size="13" fill="#222">Chamfer mean: {best.chamfer_mean:.5f}</text>',
            f'<text x="392" y="454" font-size="13" fill="#222">Chamfer p95: {best.chamfer_p95:.5f}</text>',
            f'<text x="392" y="476" font-size="13" fill="#222">Objective: {best.objective:.5f}</text>',
            f'<text x="392" y="498" font-size="13" fill="#222">Initial yaw/pitch/roll: {best.yaw:g}/{best.pitch:g}/{best.roll:g}</text>',
            f'<text x="392" y="536" font-size="13" fill="#222">Quality gate: {gate.status}</text>',
            f'<text x="392" y="558" font-size="13" fill="#222">Min edge F1@3: {gate.projection_edge_f1_at_3:.3f}</text>',
            f'<text x="392" y="580" font-size="13" fill="#222">Max edge Chamfer px: {gate.projection_edge_chamfer_px:.2f}</text>',
            '<text x="392" y="620" font-size="12" fill="#555">SVG fallback written because Pillow is unavailable.</text>',
            "</svg>",
        ]
    )
    path = out_dir / "alignment_proof.svg"
    path.write_text("\n".join(body) + "\n", encoding="utf-8")
    return path


def run(args: argparse.Namespace) -> int:
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    cad = load_cloud(Path(args.cad), args.samples, seed=11)
    proxy = load_cloud(Path(args.proxy), args.samples, seed=29)
    candidates = [
        evaluate_candidate(cad, proxy, yaw, pitch, roll, args.icp_steps)
        for yaw in yaw_values(args.yaw_step)
        for pitch in parse_values(args.pitch)
        for roll in parse_values(args.roll)
    ]
    candidates.sort(key=lambda item: item.objective)
    best = candidates[0]
    gate = _quality_gate(cad, proxy, candidates)
    proof = write_proof(cad, proxy, best, gate, out_dir)
    result = {
        "schema": "3d-cli.proxy-align.v1",
        "cad": _mesh_dict(cad),
        "proxy": _mesh_dict(proxy),
        "best": _candidate_dict(best, cad, proxy),
        "quality_gate": _quality_dict(gate),
        "top_candidates": [_candidate_dict(c, cad, proxy) for c in candidates[: min(8, len(candidates))]],
        "artifacts": {"proof": str(proof), "proof_png": str(proof) if proof.suffix == ".png" else None},
        "notes": [
            "Distances are in normalized CAD/proxy bounding-box diagonal units.",
            "quality_gate.status must be ok before the proxy is trusted as a camera prior.",
            "This is a coarse 3D spatial prior; final photo matching should still use contour fit-camera diagnostics.",
        ],
    }
    path = out_dir / "result.json"
    path.write_text(json.dumps(result, allow_nan=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if args.json:
        print(path)
    else:
        print(json.dumps(result, allow_nan=False, indent=2, sort_keys=True))
    return 1 if gate.status == "reject" else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cad", required=True)
    parser.add_argument("--proxy", required=True)
    parser.add_argument("--out", default="proxy-align")
    parser.add_argument("--samples", type=int, default=2500)
    parser.add_argument("--yaw-step", type=float, default=45.0)
    parser.add_argument("--pitch", default="0")
    parser.add_argument("--roll", default="0")
    parser.add_argument("--icp-steps", type=int, default=10)
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.samples < 100:
        parser.error("--samples must be >= 100")
    if args.icp_steps < 0:
        parser.error("--icp-steps must be >= 0")
    try:
        return run(args)
    except Exception as exc:
        print(f"proxy_align: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
