"""usdz.py — headless STL -> USDZ converter (the format an iPhone/Mac rotates natively).

Purpose:
  Turn a printed-in-mm, Z-up STL mesh into a COLORED USDZ that Apple AR Quick Look
  shows upright with proper shading (tap the file in Messages/Files and drag to spin).

Accessed via:
  `lib/commands/usdz.py` (the `3d usdz` command). Kept as a separate module so the
  command stays import-light (stdlib-only at module top level — discovery imports every
  command on every `3d` call); the heavy deps live behind the function here.

Invariants (the naive export gets these wrong — mirrored from
  garage-band/tools/mesh_to_usdz.py):
    * Z-up model -> Y-up stage: vertex (x,y,z) -> (x, z, -y), and SetStageUpAxis(y).
    * SetStageMetersPerUnit(0.001) — models are authored in millimetres.
    * A UsdPreviewSurface material is bound per mesh; plain displayColor alone renders
      untextured/wrong in Quick Look (displayColor is kept only as a fallback).
    * SetDefaultPrim is required or Quick Look refuses to show the scene.
    * Packaged via pxr UsdUtils.CreateNewUsdzPackage — NO dependency on a system `usdzip`.

Heavy deps (trimesh, pxr) are imported LAZILY inside mesh_to_usdz so importing this
module costs nothing; `pxr` is the `usd-core` pip package.
"""
from __future__ import annotations

import re
from typing import Any, Tuple

from errors import MissingDependency

RGB = Tuple[float, float, float]


def _sanitize_prim_name(name: str) -> str:
    """Make `name` a valid USD prim token (alphanumeric/underscore, non-empty, non-leading-digit).

    Mesh names are often derived from a filename, so they carry dots/dashes that
    UsdGeom.Mesh.Define rejects. The reference never sanitizes because it uses
    hand-written names; here the name flows from the CLI input, so it must be cleaned.
    """
    cleaned = re.sub(r"[^A-Za-z0-9_]", "_", name)
    if not cleaned or cleaned[0].isdigit():
        cleaned = "m_" + cleaned
    return cleaned


def mesh_to_usdz(
    stl_path: str,
    usdz_out: str,
    *,
    color: RGB = (0.78, 0.74, 0.66),
    name: str = "part",
) -> int:
    """Load an STL and write a COLORED USDZ (Y-up, mm, UsdPreviewSurface). Returns face count.

    Args:
        stl_path: input mesh (STL or anything trimesh loads).
        usdz_out: output path; should end in `.usdz`.
        color:    diffuse RGB in 0..1 for the UsdPreviewSurface + displayColor fallback.
        name:     base prim name (sanitized to a valid USD token).

    Raises:
        MissingDependency: if `trimesh` or `pxr` (usd-core) is not installed.
        ValueError:        if the mesh is empty / has no faces, or packaging fails.
    """
    # Heavy deps, imported lazily so module import stays cheap and offline-safe.
    try:
        import trimesh  # noqa: F401  (heavy; lazy on purpose)
    except ImportError as e:
        raise MissingDependency(
            "trimesh",
            install="uv pip install trimesh",
            degrades="cannot read the STL to convert it to USDZ",
            command="usdz",
        ) from e
    try:
        # pxr ships in the `usd-core` wheel and has no type stubs.
        from pxr import (  # type: ignore[import-untyped]  # usd-core is untyped
            Gf,
            Sdf,
            Usd,
            UsdGeom,
            UsdShade,
            UsdUtils,
            Vt,
        )
    except ImportError as e:
        raise MissingDependency(
            "pxr (usd-core)",
            install="uv pip install usd-core",
            degrades="cannot build the USDZ package",
            command="usdz",
        ) from e

    import os
    import tempfile

    import numpy as np

    # force="mesh" collapses a multi-body Scene into one Trimesh, so .vertices/.faces
    # exist; trimesh's stub types load() as the abstract Geometry, hence the Any cast.
    mesh: "Any" = trimesh.load(stl_path, force="mesh")
    if getattr(mesh, "is_empty", False) or len(mesh.faces) == 0:
        raise ValueError(f"empty mesh (no faces): {stl_path}")

    # Z-up (model) -> Y-up (AR Quick Look): (x,y,z) -> (x, z, -y).
    V = mesh.vertices.astype(float)
    V = np.column_stack([V[:, 0], V[:, 2], -V[:, 1]])
    F = mesh.faces.astype(int)
    nfaces = int(len(F))

    prim_name = _sanitize_prim_name(name)
    usdz_out = os.path.abspath(usdz_out)

    with tempfile.TemporaryDirectory() as tmp:
        usdc = os.path.join(tmp, "scene.usdc")
        stage = Usd.Stage.CreateNew(usdc)
        UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.y)
        UsdGeom.SetStageMetersPerUnit(stage, 0.001)  # models authored in mm
        UsdGeom.Xform.Define(stage, "/scene")
        stage.SetDefaultPrim(stage.GetPrimAtPath("/scene"))  # Quick Look needs a default prim

        meshpath = f"/scene/{prim_name}"
        prim = UsdGeom.Mesh.Define(stage, meshpath)
        prim.CreatePointsAttr(Vt.Vec3fArray([Gf.Vec3f(*map(float, v)) for v in V]))
        prim.CreateFaceVertexCountsAttr(Vt.IntArray([3] * nfaces))
        prim.CreateFaceVertexIndicesAttr(Vt.IntArray([int(i) for f in F for i in f]))
        prim.CreateSubdivisionSchemeAttr(UsdGeom.Tokens.none)
        prim.CreateDisplayColorAttr(Vt.Vec3fArray([Gf.Vec3f(*color)]))  # fallback only

        # UsdPreviewSurface — this is what Quick Look actually shades with.
        mat = UsdShade.Material.Define(stage, f"/scene/mat_{prim_name}")
        shader = UsdShade.Shader.Define(stage, f"/scene/mat_{prim_name}/surf")
        shader.CreateIdAttr("UsdPreviewSurface")
        shader.CreateInput("diffuseColor", Sdf.ValueTypeNames.Color3f).Set(Gf.Vec3f(*color))
        shader.CreateInput("roughness", Sdf.ValueTypeNames.Float).Set(0.55)
        shader.CreateInput("metallic", Sdf.ValueTypeNames.Float).Set(0.0)
        mat.CreateSurfaceOutput().ConnectToSource(shader.ConnectableAPI(), "surface")
        binding = UsdShade.MaterialBindingAPI.Apply(prim.GetPrim())  # apply schema, then bind
        binding.Bind(mat)

        stage.GetRootLayer().Save()

        os.makedirs(os.path.dirname(usdz_out) or ".", exist_ok=True)
        ok = UsdUtils.CreateNewUsdzPackage(Sdf.AssetPath(usdc), usdz_out)
        if not ok or not os.path.isfile(usdz_out):
            raise ValueError(f"failed to write USDZ package: {usdz_out}")

    return nfaces
