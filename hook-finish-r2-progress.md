# Anti-slop pre-push r2 progress

## 2026-10-02 — intake

- Scope: reconcile the staged post-`a495bd4` repair with the bounded repository-scoped Python advisory contract.
- Guardrails: no production hook installation or push; one independent scoped review and at most one repair/re-review.
- First milestone in progress: preserve and inspect the current staged delta, ticket #55, and canonical Git invariants before changing code.
- Required evidence: canonical config and HEAD before/after disposable-fixture tests; actual installer plus temporary bare-remote push test.

## 2026-10-02 — assessment complete

- Preserved patch assessed: `scripts/hooks/pre-push` adds runtime shell/Python classification and source-rewriting `exec`; this conflicts with the bounded-contract ruling and will be removed.
- Preserved patch assessed: `scripts/install_pre_push_hook.py` hash recognition and locking are relevant, but hard-coded historical hashes and backup-chain mutation are not explicit installation metadata; replace with a small metadata-backed predecessor record.
- Preserved patch assessed: `scripts/outgoing_antislop.py` baseline cache, already-remote baseline, and `SyntaxError` tokenization handling preserve the outgoing-blob contract; retain subject to focused proof.
- Tests that exercise legacy migration, arbitrary modified anti-slop shims, or chained backup discovery are scope creep; replace with explicit unsupported-layout/no-mutation coverage.
- Canonical before-fixture evidence: HEAD `262e3f9aa103ecf59b6be6b95cf3b41e1b21db64`, tree `d285e0a73063efee4b0678b3ddb0e4d2d9a0ce49`, `core.bare=false`, local `core.hooksPath=/Users/ultra/xp/3d-cli/.git/hooks`.
- Ticket read/update is blocked because `$HOME/.local/bin/task` has no GitHub credential; do not create a duplicate or use a raw GitHub client.

## 2026-10-02 — bounded reconciliation complete

- Hook reduced from 514 lines to 75: it runs only the recorded predecessor once with unmodified stdin/argv, preserves its exit, then invokes the advisory once. No hook-content parsing, shell/Python transformation, `exec`, or backup-chain discovery remains.
- Installer records `pre-push.previous` and `pre-push.3d-antislop.json` SHA-256 metadata. It uses only local/worktree configuration, strips inherited `GIT_*` variables from its own Git queries, and refuses altered or ambiguous layouts before mutation.
- Focused red/green evidence: `test_installer_refuses_ambiguous_existing_predecessor_chain` failed before the metadata contract (installer returned 0), then passed after it.
- Focused suite: `python3 -m pytest -q tests/test_outgoing_antislop.py` — `15 passed`.
- Static checks: `py_compile`, Ruff, and targeted mypy passed.
- Disposable tests preserved canonical invariants: HEAD/tree/config/hooksPath exactly match the intake evidence; canonical status retains only pre-existing README/local assets.

## 2026-10-02 — release proof

- One independent read-only scoped review ran through OMP's `reviewer` agent (`IndependentHookReview`); direct `review` CLI was unavailable. It found two contract gaps: accepting a non-executable predecessor and silently ignoring an effective global-only hooks path.
- The single permitted repair added explicit no-mutation tests for both gaps. They were red before repair and green after; the installer now rejects both cases. A claimed subdirectory-path concern was not reproduced: existing local-relative and linked-worktree installer push tests cover Git's root-relative configuration behavior.
- Final normal gate: `timeout 600 $HOME/.local/bin/dev run test` completed with `3735 passed, 9 skipped, 2 warnings`; Ruff and mypy passed. The warnings are existing trimesh and Starlette deprecations, not hook output.
