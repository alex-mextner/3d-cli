# `3d solid`: STL to a local faceted CAD solid

Convert one closed STL shell to STEP without Fusion, a cloud account, automatic downloads or analytic-reconstruction claims. This is **faceted B-rep**: curved surfaces remain planar triangle faces. Original feature history is not recovered. Analytic recognition is tracked separately in #58.

## Install and run

```powershell
uv sync --python 3.12 --extra dev --extra solid
.\.venv\Scripts\python.exe bin/3d solid "input mesh.stl" --units mm -o "new solid.step" --json
```

On POSIX use `.venv/bin/python` instead. `3d solid --help` is available without OCP. The optional solid extra uses tested `cadquery-ocp==8.0.1.0.0`, available on Python 3.11 through 3.14. Existing non-solid commands retain the Python 3.10 floor.

Input units are mandatory: `mm`, `cm`, `m` or `in`; output is millimetres. Default tolerance is 0.000001 mm and default budget is 5,000 triangles (maximum configurable budget: 50,000). Set `--tolerance MM` or `--max-faces N` explicitly; tolerances larger than 0.01% of the model extent are rejected. Files over 25 MiB are rejected. The native geometry worker has a 120-second time budget.

## Geometry contract

The loader merges only exactly equal vertex coordinates, as STL repeats them. It does not close holes, discard triangles, use approximate welding or combine separate shells. Degenerate, duplicate, nonfinite, open or inconsistently wound input is rejected. Separate shells and internal cavity shells are not yet supported; a through-hole within one connected shell is supported and tested. A globally inward orientation is reversed and reported.

After sewing planar faces into one B-rep solid, the saved STEP is reopened independently. Both representations must be valid, contain exactly one positive-volume solid, and preserve mesh bounds and volume within the recorded tolerance. Publication is atomic and refuses any existing destination, including a destination created during conversion. Input is never modified; its hash describes the captured bytes actually loaded.

The JSON success status is `CONVERTED`, not a general manufacturing PASS. `representation` is `faceted-brep`. It includes source/output hashes, units, normalization, volume, dimensions and STEP readback. Mesh self-intersections, structural strength, manufacturing suitability and analytic recognition remain explicitly unverified. A valid B-rep alone does not establish those properties.

These commands skip unrelated first-run OpenSCAD downloads. This is not yet the project-wide network sandbox tracked in #62. QymCAD itself has its own update settings.

Exit codes: 0 converted; 1 failed geometry gate; 2 invalid request; 127 missing optional local dependency. Choose a fresh output path; overwriting is deliberately not offered.

## Merge coplanar triangles

Add `--merge-coplanar` to combine neighboring faces on the same plane after sewing. A cube becomes six planar faces instead of twelve triangle faces. Polygonal hole walls stay polygonal; this is not cylinder fitting. The JSON `coplanar_merge` records whether it was requested and the before/after face counts. Linear tolerance is the selected millimetre tolerance; angular tolerance is 1e-12 radians. After unification, solid validity, volume, dimensions and STEP roundtrip are checked again. No failed unification is hidden by silently reverting to another representation.

The CLI isolates worker output in a staging directory beside the destination. Only a successful worker exit, finite report, independent readback verdict and matching artifact hash allow the parent to publish the final STEP. A worker timeout or error cannot publish the requested output.

## Validation and filesystem limits

The sewing stage must retain the input triangle count and welded vertex count before any requested merge. Vertex positions are compared in both directions. Input edges at or below ten times the linear tolerance are rejected rather than risking silently collapsed features. The saved STEP is checked with a fresh reader in the same geometry process, not a separate verification process. Bounds and centroid use a distance tolerance; volume and surface area use a relative tolerance of 0.000001, independent of a raised linear tolerance. These checks are substantial consistency gates, not a complete proof of surface equivalence or absence of self-intersections.

Output is published with an exclusive same-filesystem hard link. Local NTFS and ext4 are supported; filesystems without hard-link support (including typical exFAT/FAT32 removable storage) fail explicitly. There is deliberately no non-atomic copy fallback. Convert onto a supported local filesystem and copy a completed result separately when required.

The byte/triangle budgets and worker timeout are not an OS memory or network sandbox. Abrupt machine shutdown or process kill can leave staging directories. Routine cleanup failures are returned as warnings, without turning a valid published file into a reported conversion failure.

Coplanar merging uses the documented strict angular tolerance and does not infer an ideal plane from noisy or rounded coordinates. Rotated float32 STL geometry may retain more planar faces. Analytic reconstruction remains a separate capability under issue #58.
