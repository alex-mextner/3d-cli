# QymCAD and validated solid implementation plan

Goal: make two real local tools usable without depending on Fusion or cloud inference.
Spec: ../specs/2026-10-02-qymcad-solid-design.md
Architecture: self-registering CLI adapters, an isolated geometry worker, external QymCAD application, shared platform-aware executable discovery. Python; OpenCASCADE through OCP; native Windows verification.

1. Write failing public CLI tests for solid conversion and QymCAD discovery, plus the existing worktree-doctor Windows regression.
2. Add `lib/cli/venv_paths.py`; use it in `lib/commands/worktree.py` and `lib/cli/pyrun.py`. Verify native Windows and simulated POSIX layouts.
3. Add `lib/solid_conversion.py` for bounded input validation, faceted B-rep construction, independent STEP validation and non-destructive output publication. Add `lib/commands/solid.py` with structured errors and clean JSON.
4. Add `lib/qymcad_tools.py` and `lib/commands/qymcad.py` for honest capability reporting, executable selection and array-based explicit launch. No unverified file-argument contract.
5. Update command docs/catalog and optional geometry dependencies; run lint, types, targeted CLI/integration tests and the full gate. Keep pre-existing Windows gate failures separate from new-feature evidence.
6. Install a pinned official QymCAD Windows release in the user's application cache, retain release metadata/hash and verify launch. Never run a printer or CNC operation as part of CAD validation.
7. Record task evidence and links; preserve an unmerged branch until independent review and all required acceptance gates pass.

Review focus: inputs with spaces; NaN/Inf and degenerate faces; multi-shell cavities; output clobber/races; misleading JSON success or unsupported CAD capabilities.
