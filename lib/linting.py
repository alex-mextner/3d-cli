from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from errors import InputNotFound, InvalidArgument

SUBJECT_LEAKAGE_TERMS = tuple(
    "".join(parts)
    for parts in (
        ("loco", "motive"),
        ("boi", "ler"),
        ("smoke", "box"),
        ("fun", "nel"),
    )
)
NO_SUBJECT_LEAKAGE_REMEDIATION = (
    "Pass subject-specific terms as project data or mark the line as an example "
    "with 'e.g.' or 'example'."
)

RuleCheck = Callable[[Sequence[Path]], list["Finding"]]


@dataclass(frozen=True)
class Finding:
    rule_id: str
    path: Path
    line: int
    column: int
    term: str
    message: str
    remediation: str
    text: str

    def as_dict(self) -> dict[str, object]:
        return {
            "rule": self.rule_id,
            "path": str(self.path),
            "line": self.line,
            "column": self.column,
            "term": self.term,
            "message": self.message,
            "remediation": self.remediation,
            "text": self.text,
        }


@dataclass(frozen=True)
class Rule:
    id: str
    summary: str
    remediation: str
    check: RuleCheck


class RuleRegistry:
    def __init__(self) -> None:
        self._rules: dict[str, Rule] = {}

    def register(self, rule: Rule) -> None:
        if rule.id in self._rules:
            raise ValueError(f"duplicate lint rule: {rule.id}")
        self._rules[rule.id] = rule

    def get(self, rule_id: str) -> Rule:
        try:
            return self._rules[rule_id]
        except KeyError:
            raise InvalidArgument("--rule", rule_id, self.ids(), command="lint") from None

    def ids(self) -> list[str]:
        return sorted(self._rules)

    def selected(self, rule_ids: Sequence[str] | None = None) -> list[Rule]:
        if rule_ids is None:
            return [self._rules[rule_id] for rule_id in self.ids()]
        return [self.get(rule_id) for rule_id in rule_ids]


def builtin_registry() -> RuleRegistry:
    registry = RuleRegistry()
    registry.register(
        Rule(
            id="no-subject-leakage",
            summary="detect unmarked subject-specific terms in core files",
            remediation=NO_SUBJECT_LEAKAGE_REMEDIATION,
            check=check_no_subject_leakage,
        )
    )
    return registry


def default_scan_paths(root: Path | None = None) -> list[Path]:
    base = (root or repo_root()) / "lib"
    if not base.is_dir():
        return []
    return sorted(path for path in base.rglob("*.py") if path.is_file())


def lint_paths(paths: Sequence[Path | str], rule_ids: Sequence[str] | None = None) -> list[Finding]:
    registry = builtin_registry()
    scan_paths = expand_scan_paths(paths)
    findings: list[Finding] = []
    for rule in registry.selected(rule_ids):
        findings.extend(rule.check(scan_paths))
    return sorted(findings, key=lambda f: (str(f.path), f.line, f.column, f.rule_id))


def expand_scan_paths(paths: Sequence[Path | str]) -> list[Path]:
    if not paths:
        return default_scan_paths()

    expanded: list[Path] = []
    for raw_path in paths:
        path = Path(raw_path)
        if not path.exists():
            raise InputNotFound(str(path), command="lint")
        if path.is_dir():
            expanded.extend(
                sorted(
                    child
                    for child in path.rglob("*.py")
                    if child.is_file() and not is_exempt_path(child, root=path)
                )
            )
        elif path.is_file() and path.suffix == ".py":
            expanded.append(path)
    return expanded


def check_no_subject_leakage(paths: Sequence[Path]) -> list[Finding]:
    pattern = _subject_pattern()
    findings: list[Finding] = []
    for path in paths:
        if is_exempt_path(path):
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for lineno, line in enumerate(lines, start=1):
            if is_example_line(line):
                continue
            for match in pattern.finditer(line):
                term = match.group(0)
                findings.append(
                    Finding(
                        rule_id="no-subject-leakage",
                        path=path,
                        line=lineno,
                        column=match.start() + 1,
                        term=term,
                        message=f"subject-specific leakage term '{term}' in core file",
                        remediation=NO_SUBJECT_LEAKAGE_REMEDIATION,
                        text=line.strip(),
                    )
                )
    return findings


def is_example_line(line: str) -> bool:
    return bool(re.search(r"\be\.g\.|\bexample\b", line, flags=re.IGNORECASE))


def is_exempt_path(path: Path, *, root: Path | None = None) -> bool:
    probe = path
    if root is not None:
        try:
            probe = path.relative_to(root)
        except ValueError:
            probe = Path(path.name)
    elif path.is_absolute():
        try:
            probe = path.relative_to(repo_root())
        except ValueError:
            probe = Path(path.name)
    return any(part.lower() in {"docs", "tests"} for part in probe.parts)


def repo_root() -> Path:
    env_root = os.environ.get("REPO_ROOT")
    if env_root:
        return Path(env_root)
    return Path(__file__).resolve().parents[1]


def _subject_pattern() -> re.Pattern[str]:
    terms = "|".join(re.escape(term) for term in SUBJECT_LEAKAGE_TERMS)
    return re.compile(rf"(?<![A-Za-z0-9])({terms})(?![A-Za-z0-9])", flags=re.IGNORECASE)
