# `3d strength` solver: library evaluation (#575)

Decision: **scikit-fem** (BSD-3, pure Python) for the finite-element bookkeeping and **pytetwild**
(fTetWild wheels, MPL-2.0) for tetrahedral meshes of arbitrary STL, both lazy-imported in the
`strength_run.py` tool that `3d strength` execs through `cli.pyrun`. Element stiffness, tied /
frictionless contact and stress recovery are ~600 lines of numpy on top (`lib/structural/fea.py`).
`3d help` imports none of it (`tests/test_imports.py` green). Install: `uv sync --extra fea`
(also in the `dev` extra); `pyrun` falls back to `uv run --with scikit-fem --with pytetwild`.

## Measurements (2026-10-24, Python 3.13, 4 threads, loaded shared workstation)

Install footprint (`uv pip install --target`, `--no-deps`) and licence from package metadata:

| Candidate | Size | Licence | Result |
|---|---|---|---|
| scikit-fem 12.0.2 | 1.2 MB | BSD | chosen: pure pip, numpy/scipy only (already core deps) |
| pytetwild 0.2.3 | 8.2 MB | MPL-2.0 | chosen: wheel, no system libs. **>= 0.3 imports pyvista at package import (vtk: 564 MB in site-packages), so it is capped `<0.3`** |
| SfePy 2026.3 | 40 MB | BSD | works; kept as the documented alternative |
| PyNiteFEA 3.2.0 | 1.2 MB | MIT | frame / beam elements only, no solids or contact: not usable for tet models |
| gmsh 4.15.2 | 104 MB | GPL-2+ | rejected: size and licence |
| tetgen 0.8 / meshpy | 0.9 / 1.7 MB | wrapper MIT, TetGen AGPL / Triangle non-free | rejected: licence |
| pygalmesh | pip install failed (CGAL native build) | | rejected |
| FEniCSx / dolfinx | not on PyPI (`uv` resolution fails) | | rejected: conda / apt only |
| CalculiX (`ccx`) | external binary, apt 2.21 candidate, not installed | GPL | ROADMAP s6 `3d fea` path, not needed here |

Timing, cantilever 100 x 10 x 10 mm, PLA E = 3300 MPa, analytic tip deflection 1.2121 mm. fTetWild
meshing + scikit-fem assembly + SuperLU direct solve:

| Element | Tets | DOFs | Mesh | Assemble | Solve | Tip error |
|---|---|---|---|---|---|---|
| P1 | 10 275 | 7 143 | 0.55 s | 0.22 s | 0.08 s | -7.7 % |
| P1 | 57 570 | 33 999 | 3.6 s | 2.1 s | 1.9 s | -2.6 % |
| P1 | 165 269 | 95 805 | 10.2 s | 9.0 s | 21.3 s | -1.5 % |
| P2 | 737 | 4 332 | 0.17 s | 0.14 s | 0.03 s | -0.8 % |
| P2 | 2 877 | 14 850 | 0.29 s | 0.40 s | 0.32 s | -0.3 % |
| P2 | 9 967 | 47 283 | 0.55 s | 1.2 s | 5.8 s | +0.6 % |

SfePy, same problem: P2 3 143 tets 0.32 s + 0.52 s (-0.3 %); P2 10 059 tets 0.11 s + 4.0 s (-0.3 %);
P1 57 095 tets 0.65 s + 2.4 s (-5.1 %). Same accuracy and speed class as scikit-fem, 30x the install.
(Contact was not evaluated in SfePy.)

So a 50k-tet linear mesh solves in ~8 s end to end, but linear tets bend too stiffly (-2.6 % there,
-7.7 % at 10k); quadratic tets give < 1 % with a few thousand elements. `3d strength` therefore
defaults to **P2 with ~6 000 tets**, sized from the thinnest wall, and offers `--order 1 --max-elements
50000`. The direct solver is the limit for compact solids: the real snap-hook sub-model below has
25 962 P2 tets (121 362 DOFs) and solves in 35 s, 2.6 GB RSS. Two measured pitfalls fixed in the code:
SuperLU with partial pivoting took 147 s on a penalty-coupled contact matrix that factorises in 4 s with
`SymmetricMode` and no pivoting, and fTetWild moves the surface by up to its envelope, so load / support
regions are matched with that slack (otherwise a cap facet occasionally missed its region and a benchmark
beam came out 22 % soft).

Contact: scikit-fem has none. The node-to-surface penalty (quadratic shape functions on both sides) is
ours: a beam cut in two and tied reproduces the monolithic tip deflection to 0.03 %.

## Benchmarks (tests)

| Case | Reference | Error |
|---|---|---|
| Cantilever tip deflection (P2, 2.6k tets) | PL^3/3EI | -0.14 % (limit 2 %) |
| Tapered cantilever tip | closed form (quad-checked) | +0.1..+4 % over meshes (limit 3 % on the pinned mesh) |
| Three-point bend | Euler-Bernoulli / Timoshenko | +3.9 % / +0.6 % (limits 5 % / 1.5 %) |
| Thick pipe hoop stress | Lame | within 5 % |
| Snap hook strain 1 mm from root | 1.5 t y/L^2 x (L-1)/L | within 10 % |
| Tied two-body beam | monolithic | +0.03 % (limit 4 %) |
| Frictionless stack shortening | P L / E A | within 5 % |
| Closed forms (const / tapered / three-point) | numerical quadrature | < 1e-6 (limit 1 %) |

## Smoke test: soil-sensor-case snap hook (garage-band #81)

`projects/soil-sensor-case/test/snap_coupon.scad` (`view="bottom"`, read-only, rendered to /tmp), PLA,
printed face up (build axis Z, arm along Z so the bending fibre crosses the layers), barb pushed
0.1 mm inward (barb_h - snap_gap), sub-model `--crop 14,-6,-3,20,6,7.5`, base plane fixed.

- Closed form (`--hook t=1.0,L=7,w=6,y=0.1`): root strain 0.306 % vs 0.9 % permissible, SF 2.94 PASS.
- FEA, 0.5 mm mesh: 0.8 % strain at the arm root corners (x = 17.9, y = +-3.0, z = 0), safety factor
  1.08 away from the load patch; the beam strain 2-3.5 mm up the arm (0.21 %) agrees with the formula
  (0.19 %). The mesh sweep (edge 0.8 / 0.65 / 0.5 mm) gives 1.23 / 1.16 / 1.08: the sharp slit corner
  at the arm root is a strain singularity that the closed form cannot see. Reading: the hook passes the
  beam formula but the unfilleted root is where it will crack in PLA across layers; a root fillet is the
  fix to model. The barb patch where the displacement is imposed is flagged `near_constraint`.
