# E2E Test Shape

The generated matrix in `test_cli_matrix.py` gives breadth: many commands, working
directories, and environment variants are checked with cheap real-CLI invocations.

The readable workflow tests give depth. `test_cli_stories.py` holds broad cross-command
stories, while the `test_*_workflows.py` files group smaller scenarios by user domain:
render/check, reference preprocessing and score, fit-camera/match, slice/print planning,
object-model/web, and shell chains. They are short workflows a human can scan to learn
how the CLI is used: discover help, inspect parameters, save JSON for another shell step,
read structured errors, generate artifacts, and run `doctor` as an environment report.

Good e2e tests are user tasks, not status probes. They should show why `3d` is useful:
chain commands, redirect artifacts, pipe JSON into normal shell tools, then assert the
content a user would actually trust. Exit code alone is not an e2e assertion; inspect the
generated file, JSON fields, report rows, rule ids, anchors, styles, or planned steps.

Every user-visible command change needs e2e consideration:

- New commands, aliases, flags, and docs/help behavior must remain covered by the generated
  real-CLI matrix that calls `bin/3d` through canonical names and alias names.
- Workflows a user would copy into a terminal should get a readable story test with a
  docstring that describes the task in user terms.
- Shell-facing workflows should demonstrate shell redirection (`>`) and pipes (`|`) when
  they clarify how to chain `3d` with other tools.
- Optional external tools belong in workflow tests only when the test skips cleanly with
  a precise reason. A skipped OpenSCAD/ImageMagick/slicer/web-tier scenario is acceptable;
  a false pass or host-dependent failure is not.
- Prefer a few assertions a user would trust over many copy-pasted invocations: inspect
  PNG dimensions, JSON fields, KEY=VALUE score lines, generated overlays, project lists,
  profile guidance, and handoff files.
- Unit tests still own pure logic. They do not replace a real `bin/3d` e2e story for
  command behavior.
