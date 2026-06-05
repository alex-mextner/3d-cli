"""3d lint — run advisory lint rules over .scad / .py / project files.

WHAT: scans source files for hygiene issues (subject leakage, naming conventions,
  style violations) and prints human-readable or JSON findings.

WHY: before the acceptance gates (`3d check`) catch geometric defects, lint catches
  the code-quality and project-hygiene issues that make a model hard to maintain or
  share — hardcoded subject names, leaked internal references, inconsistent naming.
  Advisory, not fatal: exit 0 = clean, 1 = findings, 2 = bad invocation.

Examples:
  3d lint --all                       # scan lib/*.py and lib/**/*.py
  3d lint lib/preprocess_reference.py # scan one file
  3d lint --all --rule no-subject-leakage --json   # JSON output, one rule

ROADMAP §25: "3d lint — runs a configurable set of model checks (geometry, printability,
  naming, object-model hygiene, convention conformance). Distinct from check (the
  correctness/acceptance gates): lint is advisory/style/best-practice with levels
  (error|warn|off), like a code linter."
"""
from __future__ import annotations

import json
from pathlib import Path

from cli.registry import Command
from errors import UsageError

USAGE = """3d lint [--all | paths...] [--json] [--rule RULE]
  Run advisory lint rules.

Options:
  --all              scan lib/*.py and lib/**/*.py
  --json             print machine-readable findings
  --rule RULE        run one rule (accepted: no-subject-leakage)

Exit 0 = no findings, 1 = findings, 2 = invalid invocation.

Examples:
  3d lint --all
  3d lint lib/preprocess_reference.py
  3d lint --all --rule no-subject-leakage --json"""


def run(argv: list[str]) -> int:
    if argv and argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0

    json_output = False
    scan_all = False
    paths: list[Path] = []
    rule_ids: list[str] = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--all":
            scan_all = True
            i += 1
        elif arg == "--json":
            json_output = True
            i += 1
        elif arg == "--rule":
            if i + 1 >= len(argv) or not argv[i + 1] or argv[i + 1].startswith("-"):
                raise UsageError("--rule needs a rule id", command="lint")
            rule_ids.append(argv[i + 1])
            i += 2
        elif arg in ("-h", "--help"):
            print(USAGE)
            return 0
        elif arg.startswith("-"):
            raise UsageError(f"unknown option '{arg}'", command="lint")
        else:
            paths.append(Path(arg))
            i += 1

    if not argv:
        print(USAGE)
        return 1
    if scan_all and paths:
        raise UsageError("--all cannot be combined with explicit paths", command="lint")
    if not scan_all and not paths:
        print(USAGE)
        return 1

    from linting import builtin_registry, lint_paths

    selected_rules = rule_ids or None
    scan_paths = [] if scan_all else paths
    findings = lint_paths(scan_paths, selected_rules)
    registry = builtin_registry()
    summary_rules = rule_ids or registry.ids()

    if json_output:
        payload = {
            "summary": {"findings": len(findings), "rules": summary_rules},
            "findings": [finding.as_dict() for finding in findings],
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
    elif findings:
        print(f"lint: FAIL ({len(findings)} finding{'s' if len(findings) != 1 else ''})")
        for finding in findings:
            print(
                f"{finding.path}:{finding.line}:{finding.column}: "
                f"{finding.rule_id}: {finding.message}"
            )
            print(f"  text: {finding.text}")
            print(f"  remediation: {finding.remediation}")
    else:
        print(f"lint: PASS ({len(summary_rules)} rule{'s' if len(summary_rules) != 1 else ''})")

    return 1 if findings else 0


COMMAND = Command(
    name="lint",
    group="QA & GATES",
    summary="run advisory lint rules",
    usage=USAGE,
    run=run,
)
