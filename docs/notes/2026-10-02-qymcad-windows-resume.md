# QymCAD / solid continuation on Windows and Ubuntu

The owner sold the MacBook Pro. Do not connect to that device or start work there. Continue only on Ultra-win or its Ubuntu WSL. Preserve the existing Windows feature worktree.

Plan: `docs/superpowers/plans/2026-10-02-qymcad-solid.md`.
Scope: deliver and harden #56, #57, #64; retain #58-#63 as distinct open requirements.

Verified starting evidence: 60 related tests passed in the prior session. Full native Windows gate reached 2356 passes then stopped at 10 POSIX-shell/path test failures; mypy had three POSIX os API errors in old tests. No merge or independent approval was recorded.

Ruling: use a same-laptop Ubuntu verification checkout for the complete POSIX gate, plus native Windows CAD integration tests. Do not label unrelated native portability failures as a green full Windows gate.
Ruling: close stdin for OMP/review subprocesses. Native OMP currently lists no configured models; this is a configuration finding, not proof that the owner's account has no allowance. Do not copy any credentials from the sold Mac.

## Publication hardening
Six regressions failed before the fix: timeout after write, missing report, nonzero worker exit, wrong hash, missing STEP and NaN volume from readback. The command now stages worker output and only the parent publishes after exit/status/artifact-hash validation. Geometry validation explicitly rejects nonfinite metrics.

## Independent review and decisions

The configured Windows OMP catalogue is empty; Claude CLI is already authorized. A real read-only Sonnet review of the supplied source package found two high-severity risks and several bounded follow-ups. No test execution was claimed by the reviewer.

Fixed with witnessed failing regressions: collapsed/missing premerge face acceptance, missing small-edge resolution guard, cleanup errors reversing published success, malformed parent metrics, tolerance-dependent excessive volume allowance, parent JSON errors and immediate QymCAD startup exit. Also fixed Windows execvp argument/exit propagation. A focused follow-up run passed 39 tests after the fixes.

Ruling: retain hard-link-only fail-closed atomic publication. The suggested exclusive-copy fallback would expose a partial final file, weakening the approved commit contract. Document supported filesystems instead.
Ruling: preserve strict coplanar semantics; no automatic tolerance inflation or false cylinder reconstruction. General analytic fitting stays in #58.
Ruling: document same-process fresh-reader verification and lack of OS memory/network isolation accurately; comprehensive offline enforcement remains #62.

The first Ubuntu snapshot gate reached 2654 passes, 23 skips and five failures: one missing architecture-map entry for new code and four pre-existing ImageMagick unit-test mocks patching the wrong namespace. Mypy passed. Added the architecture-map entries and corrected the unit-test injection site without masking the defect through a system installation.

## Follow-up review and real geometry

The second independent Sonnet static review returned APPROVE with no remaining blocker in the bounded feature branch. Its non-blocking test note was addressed: the missing-artifact and wrong-hash tests now provide valid full metrics, and assert the actual rejected condition. A destination-race test also verifies another writer's work is preserved.

Native evidence on an L-shaped mounting bracket with three holes: 428 input triangles, 210 welded input vertices; faceted STEP retained 428 faces; --merge-coplanar produced 104 faces. Both reopened as one valid solid with preserved bounds, volume, area and centroid. Final planar dimensions 60 x 40 x 32.5 mm; volume 19914.444632293154 mm3. This is mechanical conversion evidence, not structural or manufacturing acceptance.

Remaining explicitly non-blocking follow-ups: roundtrip topology-count assertion, QymCAD single-instance zero-exit semantics and richer invalid-config diagnostics, semantic managed-release ordering, optional vertex-fan validation, strict rotated-float STL coplanarity behavior. Full analytic recognition, offline sandboxing, DAG execution, stable anchors, kinematics and bundles remain tracked separately. No Mac contact was made.
