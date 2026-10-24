"""Structural analysis behind `3d strength` (closed-form screens + linear-elastic FEA).

Modules are import-light on purpose: `beams`/`hooks` are pure `math`; everything that needs
numpy/scipy/trimesh/scikit-fem imports it inside the function that uses it, and the command runs
the whole package through `cli.pyrun` (see lib/strength_run.py), never at `3d help` time.

  beams        closed-form beam / plate / pipe / bearing / shear formulas (ANALYTICAL_SCREEN)
  hooks        cantilever snap-hook strain, insertion force, permissible-strain check
  fdm          transversely isotropic FDM material card: stiffness, allowables, failure indices
  orientation  print (build) axis from a flag, a 3MF build transform, or the mesh
  model        bodies, mates -> contacts, loads and supports (3d.yaml or flags)
  mesh         tet meshing (fTetWild via pytetwild) and sizing
  fea          assembly, tied / frictionless contact, solve, stress recovery (scikit-fem)
  section      cross-section properties of a mesh along its span (beam path on STL input)
  viz          stress PNG
"""
