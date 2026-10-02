# Local QymCAD and STL-to-solid

User-directed scope, 2026-10-02: integrate QymCAD as an explicit local alternative to Fusion; add and improve STL-to-solid; execute new work on Ultra-win; preserve existing Mac delivery work. Tickets #56-#64 are the acceptance source. The user authorized direct implementation after the preceding design discussion; no claim is made that a separate written artifact received another approval.

## First independently usable slice
- `3d solid INPUT.stl --units mm|cm|m|in -o OUTPUT.step --json` creates a faceted B-rep, not recovered design history or analytic surfaces.
- Exact-coordinate vertex welding is reported; no broad mesh repair, hole filling or removal of bad triangles. Require one closed, consistently wound, nondegenerate component. Preserve source and existing output. Reject unsupported multiple shells/cavities and nonfinite coordinates.
- Maximum input 25 MiB and 5,000 faces by default; geometry tolerance is explicit, finite and positive. Conversion runs in a bounded worker with a 120-second timeout and no downloads.
- Independently reopen STEP and check solid count, validity, positive volume, bounds and agreement with input. A mesh is not labelled structurally safe or self-intersection-free merely because B-rep checks pass.
- Heavy geometry dependencies are optional and lazy. Pin the tested OCP distribution in the optional extra; unsupported Python versions get an actionable install error.
- `3d qymcad doctor --json` reports executable discovery and capabilities. `launch` starts the explicit local application; it does not promise file-opening or batch editing unless verified from upstream and live behavior. Dry-run never launches.
- QymCAD stays a separately installed AGPL application. Do not copy its code into this MIT repository. Preserve its source/license references in integration documentation.
- Native Windows `.venv/Scripts/*.exe` discovery must work without regressing POSIX `.venv/bin`.

## Deferred but tracked, not silently dropped
#58 analytic planes/cylinders and deviation evidence; #59 executable feature DAG; #60 persistent anchors and dependent parts; #61 actual kinematics; #62 enforced network policy; #63 portable versioned bundles. These are not complete merely because tickets or schemas exist.

## Proof
Real native-Windows public CLI tests; cube and nontrivial closed meshes; invalid, degenerate, open and disconnected meshes; preserved output after failure; units and finite-tolerance rejection; STEP readback independent from the conversion result. Record failing regression first, then passing output. Full repository gate and independent review are separate delivery requirements.

## Current execution constraint and review refinements

The owner subsequently sold the MacBook Pro and explicitly restricted continuation to Windows or Ubuntu. No further Mac access is authorized. The Windows feature worktree is the implementation source; Ubuntu WSL on the same laptop is the POSIX verification environment.

Review refinements: stage worker output and publish only after successful exit, finite checked metrics and matching hash; preserve commit outcome on cleanup errors; retain input faces and vertices before any explicit coplanar merge; reject source edges below ten times tolerance; compare area and centroid as well as volume and bounds. Relative volume and area tolerance is 1e-6, not loosened with a raised linear tolerance. Max configurable triangle budget is 50,000. Atomic hard-link publication requires a supporting filesystem. Same-process fresh STEP-reader validation is not presented as an independently implemented verifier. Native Windows tool subprocesses must preserve argument boundaries and exit codes.
