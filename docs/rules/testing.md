# Testing

Ported from hyper-canvas-draft, adapted to ruff + pytest + mypy.

## TDD is mandatory (new features and bugfixes)
1. Write a failing test for the desired behavior.
2. Run it — confirm it fails for the RIGHT reason (not a syntax/import error). **Never skip this.**
3. Write only enough code to pass.
4. Run it — confirm green.
5. Refactor while staying green.

Never write the implementation first "to see if it works" and add tests after.

## Tests must exercise production code
- Never reimplement logic in a test. If you can't import a function, extract it from the source as an
  exported pure function first. Copy-pasted logic stays green while the real code is broken.
- Prefer testing the **core** (`lib`) directly (no subprocess); smoke-test each frontend (`cli`/`web`).
- User-visible command behavior also needs e2e coverage that calls `bin/3d`. Every new command,
  flag, alias, shell-facing workflow, and docs/help behavior needs at least one e2e test. Unit tests
  are still required for pure logic; they do not replace the shell-facing coverage.
- E2E stories are documentation as much as regression tests. Keep them readable: use
  user-workflow docstrings, real `bin/3d` invocations, and shell redirection (`>`) or pipes (`|`)
  when that demonstrates how the command composes with normal terminal tools.

## E2E story philosophy
E2E tests should read like examples we would be proud to show a new user. They are about
user tasks, not one command's exit code:
- Start from a practical 3D workflow: discover a model, extract parameters, query object-model
  annotations, plan a print, summarize artifacts, or hand structured evidence to another tool.
- Call the real `bin/3d`; when the workflow is shell-facing, exercise real shell composition
  such as `>`, `2>`, `|`, and sequential command chains.
- Inspect the artifact a user would trust: JSON keys and values, generated files, table rows,
  planned render/print steps, rule IDs, anchors, styles, or report summaries. Exit code alone
  is not an e2e assertion.
- For commands whose main output is structured JSON, cover representative content,
  selector/filter behavior, stdout/stderr separation, structured errors, and at least one
  redirect or pipeline story when another tool is expected to consume the output.
- Prefer one readable workflow test over several tiny command/status probes. A good e2e can run
  multiple commands when that is how a user solves the task.
- If the story describes how `3d` should work and it fails, fix the product unless the story is
  asking for behavior outside the documented command surface.

## Green main, always
- If `3d test` shows failures after your change — even in "unrelated" files — your change caused it
  (leaked fixtures, polluted globals, import side effects). Fix before committing.
- **Never delete a failing test** to go green. Investigate the root cause. If the test itself is
  wrong, raise it explicitly before changing it.
- Changing a test so the code passes is a RED FLAG — distinguish (a) test setup was wrong (fix setup)
  from (b) behavior changed (that's a regression, not a fix).

## The gate
`3d test` = ruff + pytest + mypy. Pytest includes unit tests, CLI smoke tests, and any e2e tests
under `tests/e2e/`; do not maintain a separate e2e matrix outside the gate. Unit-test the pure
functions (bbox→camera, axis math, score/IoU, strength formulas, `3d.yaml`/object-model loader,
selector resolution, op-DAG recompute, log adapters). CLI smoke harness: `3d <cmd> --help` for every
registered command + safe commands on `examples/`. Skip gracefully when an external tool is absent
(don't fail the suite for a missing optional dependency).

`tests/e2e/test_cli_matrix.py` is the breadth guard: it discovers registered commands and checks
their help through the real dispatcher. `tests/e2e/test_cli_stories.py` and
`tests/e2e/test_shell_chains.py` are the readable examples users and reviewers should be able to
scan before trusting a workflow.
