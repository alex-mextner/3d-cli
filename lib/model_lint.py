"""Source-level model lint rules for OpenSCAD object-model annotations."""
from __future__ import annotations

import re
from dataclasses import dataclass, replace
from enum import Enum
from typing import Callable, Mapping, Sequence

from errors import InputNotFound, InvalidArgument


class Level(str, Enum):
    OFF = "off"
    WARN = "warn"
    ERROR = "error"


@dataclass(frozen=True)
class ModelTag:
    path: str
    line: int
    column: int
    tag: str
    value: str
    identifier: str | None


@dataclass(frozen=True)
class LintFinding:
    path: str
    line: int
    column: int
    rule_id: str
    level: Level
    message: str
    hint: str = ""

    def as_dict(self) -> dict[str, str | int]:
        data: dict[str, str | int] = {
            "path": self.path,
            "line": self.line,
            "column": self.column,
            "rule_id": self.rule_id,
            "level": self.level.value,
            "message": self.message,
        }
        if self.hint:
            data["hint"] = self.hint
        return data


@dataclass(frozen=True)
class LintReport:
    path: str
    findings: list[LintFinding]

    @property
    def warning_count(self) -> int:
        return sum(1 for finding in self.findings if finding.level == Level.WARN)

    @property
    def error_count(self) -> int:
        return sum(1 for finding in self.findings if finding.level == Level.ERROR)

    def has_failures(self, *, strict: bool) -> bool:
        return self.error_count > 0 or (strict and self.warning_count > 0)

    def as_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "summary": {
                "warnings": self.warning_count,
                "errors": self.error_count,
            },
            "findings": [finding.as_dict() for finding in self.findings],
        }


@dataclass(frozen=True)
class LintContext:
    path: str
    text: str
    tags: list[ModelTag]


CheckFn = Callable[[LintContext], list[LintFinding]]


@dataclass(frozen=True)
class LintRule:
    id: str
    category: str
    summary: str
    default_level: Level
    check: CheckFn
    autofix: bool = False


class LintRegistry:
    def __init__(self) -> None:
        self._rules: dict[str, LintRule] = {}

    def add(self, rule: LintRule) -> None:
        if rule.id in self._rules:
            raise ValueError(f"duplicate lint rule id: {rule.id}")
        self._rules[rule.id] = rule

    def get(self, rule_id: str) -> LintRule | None:
        return self._rules.get(rule_id)

    def rules(self) -> list[LintRule]:
        return sorted(self._rules.values(), key=lambda rule: rule.id)

    def ids(self) -> list[str]:
        return [rule.id for rule in self.rules()]


DEFAULT_REGISTRY = LintRegistry()

_TAG_RE = re.compile(r"^\s*//\s*@(?P<tag>[A-Za-z][A-Za-z0-9_-]*)(?P<value>.*)$")
_ASSIGNMENT_RE = re.compile(r"^(?:id|name)\s*=\s*(?P<value>[A-Za-z0-9_.-]+)")
_KEBAB_ID_RE = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")

KNOWN_MODEL_TAGS = frozenset({"anchor", "class", "color", "id", "part", "section", "view"})
IDENTIFIER_TAGS = frozenset({"anchor", "id", "part", "section", "view"})


def rule(
    rule_id: str,
    *,
    category: str,
    summary: str,
    default_level: Level = Level.WARN,
    autofix: bool = False,
) -> Callable[[CheckFn], CheckFn]:
    def decorate(check: CheckFn) -> CheckFn:
        DEFAULT_REGISTRY.add(
            LintRule(
                id=rule_id,
                category=category,
                summary=summary,
                default_level=default_level,
                check=check,
                autofix=autofix,
            )
        )
        return check

    return decorate


def parse_level(raw: Level | str) -> Level:
    if isinstance(raw, Level):
        return raw
    try:
        return Level(raw)
    except ValueError:
        raise InvalidArgument("level", raw, [level.value for level in Level], command="lint") from None


def resolve_levels(
    overrides: Mapping[str, Level | str] | None = None,
    *,
    registry: LintRegistry = DEFAULT_REGISTRY,
) -> dict[str, Level]:
    levels = {rule.id: rule.default_level for rule in registry.rules()}
    if not overrides:
        return levels
    accepted = registry.ids()
    for rule_id, raw_level in overrides.items():
        if rule_id not in levels:
            raise InvalidArgument("rule", rule_id, accepted, command="lint")
        levels[rule_id] = parse_level(raw_level)
    return levels


def lint_file(
    path: str,
    *,
    levels: Mapping[str, Level] | None = None,
    rule_ids: Sequence[str] | None = None,
    registry: LintRegistry = DEFAULT_REGISTRY,
) -> LintReport:
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except FileNotFoundError:
        raise InputNotFound(path, command="lint") from None
    except OSError as exc:
        raise InputNotFound(path, command="lint") from exc
    return lint_source(path, text, levels=levels, rule_ids=rule_ids, registry=registry)


def lint_source(
    path: str,
    text: str,
    *,
    levels: Mapping[str, Level] | None = None,
    rule_ids: Sequence[str] | None = None,
    registry: LintRegistry = DEFAULT_REGISTRY,
) -> LintReport:
    selected = _selected_rules(rule_ids, registry=registry)
    active_levels = dict(levels) if levels is not None else resolve_levels(registry=registry)
    context = LintContext(path=path, text=text, tags=_parse_model_tags(path, text))
    findings: list[LintFinding] = []
    for lint_rule in selected:
        level = active_levels.get(lint_rule.id, lint_rule.default_level)
        if level == Level.OFF:
            continue
        for finding in lint_rule.check(context):
            findings.append(replace(finding, level=level))
    findings.sort(key=lambda finding: (finding.path, finding.line, finding.column, finding.rule_id))
    return LintReport(path=path, findings=findings)


def reports_summary(reports: Sequence[LintReport]) -> dict[str, int]:
    warnings = sum(report.warning_count for report in reports)
    errors = sum(report.error_count for report in reports)
    return {"warnings": warnings, "errors": errors}


def reports_as_dict(reports: Sequence[LintReport]) -> dict[str, object]:
    return {
        "summary": reports_summary(reports),
        "files": [report.as_dict() for report in reports],
    }


def _selected_rules(rule_ids: Sequence[str] | None, *, registry: LintRegistry) -> list[LintRule]:
    if rule_ids is None:
        return registry.rules()
    accepted = registry.ids()
    selected: list[LintRule] = []
    for rule_id in rule_ids:
        lint_rule = registry.get(rule_id)
        if lint_rule is None:
            raise InvalidArgument("rule", rule_id, accepted, command="lint")
        selected.append(lint_rule)
    return selected


def _parse_model_tags(path: str, text: str) -> list[ModelTag]:
    tags: list[ModelTag] = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        comment_at = _comment_start(line)
        if comment_at is None:
            continue
        comment = line[comment_at:]
        match = _TAG_RE.match(comment)
        if match is None:
            continue
        tag = match.group("tag")
        raw_value = match.group("value").strip()
        column = comment_at + comment.index("@") + 1
        tags.append(
            ModelTag(
                path=path,
                line=line_no,
                column=column,
                tag=tag,
                value=raw_value,
                identifier=_extract_identifier(raw_value) if tag in IDENTIFIER_TAGS else None,
            )
        )
    return tags


def _comment_start(line: str) -> int | None:
    in_string = False
    escaped = False
    for idx, char in enumerate(line):
        if escaped:
            escaped = False
            continue
        if char == "\\" and in_string:
            escaped = True
            continue
        if char == '"':
            in_string = not in_string
            continue
        if not in_string and char == "/" and idx + 1 < len(line) and line[idx + 1] == "/":
            return idx
    return None


def _extract_identifier(value: str) -> str | None:
    if not value:
        return None
    assignment = _ASSIGNMENT_RE.match(value)
    if assignment is not None:
        return assignment.group("value")
    return value.split()[0]


@rule(
    "object-model/unknown-tag",
    category="object-model",
    summary="semantic comment tags must be part of the known object-model skeleton",
)
def _unknown_model_tags(context: LintContext) -> list[LintFinding]:
    findings: list[LintFinding] = []
    accepted = ", ".join("@" + tag for tag in sorted(KNOWN_MODEL_TAGS))
    for tag in context.tags:
        if tag.tag not in KNOWN_MODEL_TAGS:
            findings.append(
                LintFinding(
                    path=tag.path,
                    line=tag.line,
                    column=tag.column,
                    rule_id="object-model/unknown-tag",
                    level=Level.WARN,
                    message=f"unknown model tag '@{tag.tag}'",
                    hint=f"Use one of: {accepted}.",
                )
            )
    return findings


@rule(
    "object-model/tag-missing-value",
    category="object-model",
    summary="model comment tags need a value so later tools can address them",
)
def _missing_tag_values(context: LintContext) -> list[LintFinding]:
    findings: list[LintFinding] = []
    for tag in context.tags:
        if tag.tag in KNOWN_MODEL_TAGS and not tag.value:
            findings.append(
                LintFinding(
                    path=tag.path,
                    line=tag.line,
                    column=tag.column,
                    rule_id="object-model/tag-missing-value",
                    level=Level.WARN,
                    message=f"@{tag.tag} is missing a value",
                    hint=f"Add a value, for example `// @{tag.tag} body-shell`.",
                )
            )
    return findings


@rule(
    "naming/id-kebab",
    category="naming",
    summary="object-model ids should be stable kebab-case identifiers",
)
def _ids_are_kebab_case(context: LintContext) -> list[LintFinding]:
    findings: list[LintFinding] = []
    for tag in context.tags:
        if tag.tag not in IDENTIFIER_TAGS or tag.identifier is None:
            continue
        if _KEBAB_ID_RE.match(tag.identifier) is None:
            findings.append(
                LintFinding(
                    path=tag.path,
                    line=tag.line,
                    column=tag.column,
                    rule_id="naming/id-kebab",
                    level=Level.WARN,
                    message=f"'{tag.identifier}' should be kebab-case",
                    hint="Use lowercase letters, numbers, and '-' separators.",
                )
            )
    return findings


@rule(
    "object-model/duplicate-id",
    category="object-model",
    summary="object-model ids should be unique within a model source",
)
def _ids_are_unique(context: LintContext) -> list[LintFinding]:
    findings: list[LintFinding] = []
    seen: dict[str, ModelTag] = {}
    for tag in context.tags:
        if tag.tag not in IDENTIFIER_TAGS or tag.identifier is None:
            continue
        previous = seen.get(tag.identifier)
        if previous is None:
            seen[tag.identifier] = tag
            continue
        findings.append(
            LintFinding(
                path=tag.path,
                line=tag.line,
                column=tag.column,
                rule_id="object-model/duplicate-id",
                level=Level.WARN,
                message=f"duplicate object-model id '{tag.identifier}'",
                hint=f"First seen on line {previous.line}.",
            )
        )
    return findings
