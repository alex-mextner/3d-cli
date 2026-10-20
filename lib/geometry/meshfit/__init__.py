"""meshfit — deterministic, GUI-free mesh deformation ("sculpting") driven by a declarative spec.

WHAT: a small library behind `3d mesh deform` / `3d mesh fit-silhouette`: named region masks with
  smooth falloff, sculpt-brush kernels (smooth / inflate / pinch / crease / scale-local), handle
  deformation (harmonic / biharmonic / ARAP), seeded fur relief with an overhang-safe mode,
  remesh / decimate / repair / symmetrize, a hard keep-out gate and a JSON report.

WHY: what a person does by hand in Blender sculpt mode must be reproducible from a text file:
  same input + same spec + same seed = byte-identical output, no GUI, no Blender.
  Research and benchmarks: docs/research/mesh-fit-2026-10-20.md.

Module top levels stay stdlib-only; numpy / scipy / trimesh are imported lazily where needed so the
package is importable under the repo's stdlib-only import contract.
"""

from __future__ import annotations
