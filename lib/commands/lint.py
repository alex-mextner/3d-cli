"""3d lint — advisory repository and OpenSCAD model lint rules."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from cli.registry import Command
from errors import GateFailure, InvalidArgument, UsageError

USAGE = """3d lint [--all | paths...] [options]
  Run advisory lint rules over repository Python files and OpenSCAD model metadata.

Repository options:
  --all              scan lib/*.py and lib/**/*.py
  --json             print machine-readable repository findings
  --rule RULE        run one rule (repo: no-subject-leakage; model: see --list-rules)

Model options:
  --format text|json    report format for .scad model lint (default: text)
  --strict              fail when model warnings are present
  --off ID              disable a model rule for this run
  --warn ID             set a model rule to warning level for this run
  --error ID            set a model rule to error level for this run
  --list-rules          print registered model rules

Exit 0 = no findings, 1 = findings, 2 = invalid invocation.

Examples:
  3d lint --all
  3d lint lib/preprocess_reference.py
  3d lint --all --rule no-subject-leakage --json
  3d lint examples/cube.scad --format json
  3d lint bracket.scad --strict --error naming/id-kebab"""

REPO_RULES = frozenset({"no-subject-leakage"})


def run(argv: list[str]) -> int:
    if argv and argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0
    if not argv:
        print(USAGE)
        return 1

    options = _parse_args(argv)
    if options["help"]:
        print(USAGE)
        return 0
    if options["list_rules"]:
        _print_model_rules()
        return 0

    paths = options["paths"]
    scan_all = options["scan_all"]
    if scan_all and paths:
        raise UsageError("--all cannot be combined with explicit paths", command="lint")
    if not scan_all and not paths:
        print(USAGE)
        return 1

    _validate_path_mix(paths, options)
    if _should_run_model_lint(paths, options):
        return _run_model_lint(options)
    return _run_repository_lint(options)


def _parse_args(argv: list[str]) -> dict[str, Any]:
    options: dict[str, Any] = {
        "json_output": False,
        "scan_all": False,
        "paths": [],
        "rule_ids": [],
        "format": "text",
        "strict": False,
        "list_rules": False,
        "overrides": {},
        "model_flag_seen": False,
        "help": False,
    }
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in ("-h", "--help"):
            options["help"] = True
            i += 1
            continue
        if arg == "--all":
            options["scan_all"] = True
            i += 1
        elif arg == "--json":
            options["json_output"] = True
            i += 1
        elif arg == "--rule":
            options["rule_ids"].append(_need_value(argv, i, "--rule"))
            i += 2
        elif arg == "--format":
            options["model_flag_seen"] = True
            fmt = _need_value(argv, i, "--format")
            if fmt not in ("text", "json"):
                raise InvalidArgument("--format", fmt, ["text", "json"], command="lint")
            options["format"] = fmt
            i += 2
        elif arg == "--strict":
            options["model_flag_seen"] = True
            options["strict"] = True
            i += 1
        elif arg == "--off":
            options["model_flag_seen"] = True
            _set_level_override(options, argv, i, "--off", "off")
            i += 2
        elif arg == "--warn":
            options["model_flag_seen"] = True
            _set_level_override(options, argv, i, "--warn", "warn")
            i += 2
        elif arg == "--error":
            options["model_flag_seen"] = True
            _set_level_override(options, argv, i, "--error", "error")
            i += 2
        elif arg == "--list-rules":
            options["model_flag_seen"] = True
            options["list_rules"] = True
            i += 1
        elif arg.startswith("-"):
            raise UsageError(
                f"unknown option '{arg}'",
                command="lint",
                remediation=["Run `3d lint --help` for accepted options."],
            )
        else:
            options["paths"].append(Path(arg))
            i += 1
    return options


def _need_value(
    argv: list[str],
    index: int,
    flag: str,
) -> str:
    if index + 1 >= len(argv) or not argv[index + 1]:
        raise UsageError(
            f"{flag} needs a rule id" if flag == "--rule" else f"{flag} needs a value",
            command="lint",
            remediation=["Run `3d lint --help` for accepted options."],
        )
    value = argv[index + 1]
    if value.startswith("-"):
        raise UsageError(
            f"{flag} needs a rule id" if flag == "--rule" else f"{flag} needs a value",
            command="lint",
            remediation=["Run `3d lint --help` for accepted options."],
        )
    return value


def _set_level_override(
    options: dict[str, Any],
    argv: list[str],
    index: int,
    flag: str,
    level: str,
) -> None:
    rule_id = _need_value(argv, index, flag)
    options["overrides"][rule_id] = level


def _should_run_model_lint(paths: list[Path], options: dict[str, Any]) -> bool:
    if options["model_flag_seen"]:
        return True
    if options["scan_all"]:
        return False
    return any(path.suffix.lower() == ".scad" for path in paths)


def _validate_path_mix(paths: list[Path], options: dict[str, Any]) -> None:
    if options["scan_all"]:
        return
    scad_paths = [path for path in paths if path.suffix.lower() == ".scad"]
    if scad_paths and len(scad_paths) != len(paths):
        raise UsageError(
            "cannot mix .scad model lint inputs with repository lint inputs",
            command="lint",
            remediation=[
                "Run `3d lint model.scad` and `3d lint lib/file.py` as separate commands."
            ],
        )
    if options["model_flag_seen"] and paths and not scad_paths:
        raise UsageError(
            "model lint options require .scad input files",
            command="lint",
            remediation=["Use repository lint options for Python files, or pass a .scad model."],
        )


def _run_repository_lint(options: dict[str, Any]) -> int:
    rule_ids = options["rule_ids"]
    unknown_modelish_rules = [rule_id for rule_id in rule_ids if rule_id not in REPO_RULES]
    if unknown_modelish_rules:
        raise InvalidArgument("--rule", unknown_modelish_rules[0], sorted(REPO_RULES), command="lint")

    from linting import builtin_registry, lint_paths

    scan_paths = [] if options["scan_all"] else options["paths"]
    selected_rules = rule_ids or None
    findings = lint_paths(scan_paths, selected_rules)
    registry = builtin_registry()
    summary_rules = rule_ids or registry.ids()

    if options["json_output"]:
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


def _run_model_lint(options: dict[str, Any]) -> int:
    if options["scan_all"]:
        raise UsageError("--all only runs repository lint rules", command="lint")
    if options["json_output"] and options["format"] != "json":
        options["format"] = "json"

    from model_lint import Level, lint_file, reports_as_dict, resolve_levels

    overrides = {
        rule_id: Level(level)
        for rule_id, level in options["overrides"].items()
    }
    levels = resolve_levels(overrides)
    reports = [
        lint_file(str(path), levels=levels, rule_ids=options["rule_ids"] or None)
        for path in options["paths"]
    ]

    if options["format"] == "json":
        print(json.dumps(reports_as_dict(reports), indent=2, sort_keys=True))
    else:
        _print_model_text_report(reports, strict=options["strict"])

    if any(report.has_failures(strict=options["strict"]) for report in reports):
        raise GateFailure("lint findings failed", command="lint", silent=True)
    return 0


def _print_model_rules() -> None:
    from model_lint import DEFAULT_REGISTRY

    print("3d lint model rules")
    for lint_rule in DEFAULT_REGISTRY.rules():
        autofix = " autofix" if lint_rule.autofix else ""
        print(
            f"  {lint_rule.default_level.value:<5} {lint_rule.id:<28} "
            f"{lint_rule.category:<12} {lint_rule.summary}{autofix}"
        )


def _print_model_text_report(reports: list[Any], *, strict: bool) -> None:
    from model_lint import reports_summary

    for report in reports:
        print(f"lint: {report.path}")
        if not report.findings:
            print("  OK")
            continue
        for finding in report.findings:
            location = f"{finding.path}:{finding.line}:{finding.column}"
            print(
                f"  {finding.level.value.upper():<5} {finding.rule_id:<28} "
                f"{location}  {finding.message}"
            )
            if finding.hint:
                print(f"        {finding.hint}")
    summary = reports_summary(reports)
    print(f"summary: {summary['warnings']} warning(s), {summary['errors']} error(s)")
    if any(report.has_failures(strict=strict) for report in reports):
        print(">>> LINT: FAIL")
    elif summary["warnings"]:
        print(">>> LINT: WARN")
    else:
        print(">>> LINT: PASS")


COMMAND = Command(
    name="lint",
    group="QA & GATES",
    summary="run advisory repository and model lint rules",
    usage=USAGE,
    run=run,
)
