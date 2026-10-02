#!/usr/bin/env python3
"""Advisory anti-slop checks for the content in a Git pre-push update.

Accessed via: ``scripts/hooks/pre-push`` installed into the repository's common
hooks directory, then Git's real pre-push ref-update protocol.

Assumptions: stdin contains Git's four-field ref-update records; local object
IDs resolve in the pushing repository; the analyzer must never read the index or
working tree as the content under review.

Past bugs: the first implementation used working-tree paths and reported old
findings from whole files. This version materializes local Git blobs and filters
all diagnostics to added lines in the outgoing diff.
"""
from __future__ import annotations

import ast
import io
import json
import re
import shutil
import subprocess
import sys
import tempfile
import tokenize
from dataclasses import dataclass
from pathlib import Path
from typing import Final

_OBJECT_ID_RE: Final[re.Pattern[str]] = re.compile(r"[0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?")
_ZERO_OBJECT_RE: Final[re.Pattern[str]] = re.compile(r"0{40}|0{64}")
_REMOTE_NAME_RE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9._-]+$")
_RELEVANT_SUFFIXES: Final[frozenset[str]] = frozenset(
    {".py", ".pyi", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx"}
)
_HUNK_RE: Final[re.Pattern[str]] = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
_TYPE_IGNORE_RE: Final[re.Pattern[str]] = re.compile(r"#\s*type:\s*ignore(?:\b|$)")


@dataclass(frozen=True)
class RefUpdate:
    local_ref: str
    local_sha: str
    remote_ref: str
    remote_sha: str


@dataclass(frozen=True)
class Change:
    status: str
    path: str
    old_path: str | None = None


@dataclass
class ChangeScope:
    update: RefUpdate
    change: Change
    changed_lines: set[int]


class GitError(RuntimeError):
    """A bounded Git query failed and cannot safely provide object content."""


class AnalyzerError(RuntimeError):
    """An analyzer could not inspect a materialized blob."""


def _warn(message: str) -> None:
    print(f"WARN anti-slop: {message}", file=sys.stderr)


def _run_git(
    repo: Path,
    args: list[str],
    *,
    input_bytes: bytes | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=repo,
            input=input_bytes,
            capture_output=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GitError(f"git {' '.join(args)} unavailable: {exc}") from exc
    if check and result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace").strip()
        raise GitError(f"git {' '.join(args)} exited {result.returncode}: {detail}")
    return result


def _repo_root() -> Path:
    result = _run_git(Path.cwd(), ["rev-parse", "--show-toplevel"])
    return Path(result.stdout.decode("utf-8").strip()).resolve()


def _parse_updates(payload: bytes) -> list[RefUpdate]:
    updates: list[RefUpdate] = []
    for raw_line in payload.splitlines():
        fields = raw_line.decode("utf-8", "replace").split()
        if len(fields) != 4:
            _warn("malformed ref-update input; skipping that record")
            continue
        updates.append(RefUpdate(*fields))
    return updates


def _is_zero_sha(object_id: str) -> bool:
    return _ZERO_OBJECT_RE.fullmatch(object_id) is not None


def _object_type(repo: Path, object_id: str) -> str | None:
    if _OBJECT_ID_RE.fullmatch(object_id) is None:
        return None
    result = _run_git(repo, ["cat-file", "-t", object_id], check=False)
    if result.returncode != 0:
        return None
    return result.stdout.decode("ascii", "replace").strip() or None


def _empty_tree(repo: Path) -> str:
    result = _run_git(repo, ["mktree"], input_bytes=b"")
    empty_tree = result.stdout.decode("ascii", "replace").strip()
    if not _OBJECT_ID_RE.fullmatch(empty_tree):
        raise GitError(f"git mktree returned invalid empty-tree object {empty_tree!r}")
    return empty_tree


def _comparison_base(repo: Path, update: RefUpdate, remote_name: str | None) -> str | None:
    if not _is_zero_sha(update.remote_sha):
        return update.remote_sha
    if remote_name is not None and _REMOTE_NAME_RE.fullmatch(remote_name):
        head_ref = f"refs/remotes/{remote_name}/HEAD"
        head_result = _run_git(repo, ["symbolic-ref", "--quiet", head_ref], check=False)
        if head_result.returncode == 0:
            head_target = head_result.stdout.decode("utf-8", "replace").strip()
            if head_target:
                merge_base = _run_git(
                    repo,
                    ["merge-base", head_target, update.local_sha],
                    check=False,
                )
                candidate = merge_base.stdout.decode("ascii", "replace").strip()
                if merge_base.returncode == 0 and _OBJECT_ID_RE.fullmatch(candidate):
                    return candidate
    parents = _run_git(
        repo,
        ["rev-list", "--parents", "-n", "1", update.local_sha],
        check=False,
    ).stdout.decode("ascii", "replace").split()
    if len(parents) == 1:
        return _empty_tree(repo)
    _warn(
        f"{update.remote_ref}: no remote-tracking baseline for new ref; using its first parent "
        "to avoid a whole-tree scan. Next: fetch the remote default branch for complete branch coverage"
    )
    return parents[1]


def _diff_args(old: str, local_sha: str) -> list[str]:
    return [
        "diff",
        "--name-status",
        "-z",
        "--find-renames",
        old,
        local_sha,
        "--",
    ]


def _changed_files(repo: Path, old: str, local_sha: str) -> list[Change]:
    result = _run_git(repo, _diff_args(old, local_sha))
    return _parse_changes(result.stdout)


def _line_ranges(repo: Path, old: str, local_sha: str, change: Change) -> set[int]:
    paths = [change.path]
    if change.old_path is not None and change.old_path != change.path:
        paths.insert(0, change.old_path)
    result = _run_git(
        repo,
        ["diff", "--no-color", "--unified=0", "--find-renames", old, local_sha, "--", *paths],
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace").strip()
        raise GitError(f"could not get changed lines for {change.path}: {detail}")
    ranges: set[int] = set()
    for line in result.stdout.decode("utf-8", "replace").splitlines():
        match = _HUNK_RE.match(line)
        if not match:
            continue
        start = int(match.group(1))
        count = int(match.group(2) or "1")
        ranges.update(range(start, start + count))
    return ranges


def _parse_changes(raw: bytes) -> list[Change]:
    fields = raw.split(b"\0")
    changes: list[Change] = []
    index = 0
    while index < len(fields) and fields[index]:
        status = fields[index].decode("ascii", "replace")
        index += 1
        if not status:
            continue
        if status[0] in {"R", "C"}:
            if index + 1 >= len(fields):
                _warn("truncated rename/copy record; skipping it")
                break
            old_path = fields[index].decode("utf-8", "surrogateescape")
            path = fields[index + 1].decode("utf-8", "surrogateescape")
            index += 2
        else:
            if index >= len(fields):
                _warn("truncated change record; skipping it")
                break
            old_path = None
            path = fields[index].decode("utf-8", "surrogateescape")
            index += 1
        changes.append(Change(status[0], path, old_path))
    return changes




def _blob(repo: Path, object_id: str, path: str) -> bytes:
    result = _run_git(repo, ["show", f"{object_id}:{path}"])
    return result.stdout


def _safe_temp_path(root: Path, relative_path: str) -> Path:
    path = Path(relative_path)
    if path.is_absolute() or ".." in path.parts:
        raise AnalyzerError(f"unsafe repository path {relative_path!r}")
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


def _report(path: str, line: int | None, rule: str, message: str, next_action: str) -> None:
    location = f"{path}:{line}" if line is not None else path
    _warn(f"{location}: {rule} — {message}. Next: {next_action}")


def _ruff_diagnostics(temp_path: Path, path: str, changed_lines: set[int], repo: Path) -> None:
    ruff = shutil.which("ruff")
    if ruff is None:
        _warn(f"{path}: analyzer dependency 'ruff' is missing; skipped deterministic lint. Next: install Ruff")
        return
    try:
        result = subprocess.run(
            [
                ruff,
                "check",
                "--isolated",
                "--output-format",
                "json",
                "--select",
                "E4,E7,E9,F,B,UP,SIM",
                str(temp_path),
            ],
            cwd=repo,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        _warn(f"{path}: Ruff analyzer failed ({exc}). Next: run Ruff locally and fix the analyzer environment")
        return
    if result.returncode not in {0, 1}:
        _warn(f"{path}: Ruff analyzer exited {result.returncode}: {result.stderr.strip()}. Next: run Ruff locally")
        return
    try:
        diagnostics = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as exc:
        _warn(f"{path}: Ruff returned invalid diagnostics ({exc}). Next: run Ruff locally")
        return
    if not isinstance(diagnostics, list):
        _warn(f"{path}: Ruff returned invalid diagnostics. Next: run Ruff locally")
        return
    for diagnostic in diagnostics:
        if not isinstance(diagnostic, dict):
            _warn(f"{path}: Ruff returned an invalid diagnostic entry. Next: run Ruff locally")
            continue
        location = diagnostic.get("location")
        if not isinstance(location, dict):
            continue
        row = location.get("row")
        if not isinstance(row, int) or isinstance(row, bool):
            continue
        if row not in changed_lines:
            continue
        code = str(diagnostic.get("code") or "ruff")
        message = str(diagnostic.get("message") or "deterministic lint finding")
        _report(path, row, f"anti-slop/{code}", message, f"inspect and correct {code}")



def _oxlint_availability(path: str, repo: Path) -> None:
    missing: list[str] = []
    if shutil.which("oxlint") is None:
        missing.append("no local oxlint executable")
    plugin = repo / "tools" / "oxlint" / "anti-slop" / "index.ts"
    if not plugin.is_file():
        missing.append(f"plugin is not provisioned at {plugin}")
    if missing:
        _warn(f"{path}: Oxlint anti-slop unavailable ({'; '.join(missing)}). Next: provision the JS/TS tool")
        return
    _warn(
        f"{path}: Oxlint anti-slop is provisioned but this bounded adapter does not invoke "
        "unfiltered JS/TS diagnostics. Next: run the repository Oxlint command"
    )

def _stdlib_diagnostics(source: str, path: str, changed_lines: set[int]) -> None:
    try:
        comment_lines = {
            token.start[0]
            for token in tokenize.generate_tokens(io.StringIO(source).readline)
            if token.type == tokenize.COMMENT and _TYPE_IGNORE_RE.search(token.string)
        }
    except (tokenize.TokenError, IndentationError):
        comment_lines = set()
    for line_number in sorted(comment_lines & changed_lines):
        _report(
            path,
            line_number,
            "anti-slop/no-type-ignore",
            "type suppression hides the checked contract",
            "replace the suppression with a checked type or a documented boundary invariant",
        )
    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError as exc:
        if exc.lineno is not None and exc.lineno in changed_lines:
            _report(path, exc.lineno, "anti-slop/syntax", exc.msg, "fix the syntax error before pushing")
        return
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if len(node.body) != 1:
            continue
        statement: ast.stmt = node.body[0]
        if statement.lineno not in changed_lines:
            continue
        if isinstance(statement, ast.Pass):
            _report(
                path,
                statement.lineno,
                "anti-slop/no-placeholder-pass",
                "function body is an unimplemented placeholder",
                "implement the function or remove the unfinished path",
            )
        if isinstance(statement, ast.Raise) and (
            isinstance(statement.exc, ast.Name) and statement.exc.id == "NotImplementedError"
            or (
                isinstance(statement.exc, ast.Call)
                and isinstance(statement.exc.func, ast.Name)
                and statement.exc.func.id == "NotImplementedError"
            )
        ):
            _report(
                path,
                statement.lineno,
                "anti-slop/no-placeholder-not-implemented",
                "function still raises NotImplementedError",
                "implement the function before pushing",
            )


def _analyze_change(
    repo: Path,
    update: RefUpdate,
    change: Change,
    changed_lines: set[int],
    temp_root: Path,
) -> None:
    if (
        not changed_lines
        or change.status == "D"
        or Path(change.path).suffix.lower() not in _RELEVANT_SUFFIXES
    ):
        return
    try:
        source_bytes = _blob(repo, update.local_sha, change.path)
        temp_path = _safe_temp_path(temp_root, change.path)
        temp_path.write_bytes(source_bytes)
        if change.path.lower().endswith((".py", ".pyi")):
            source = source_bytes.decode("utf-8", "replace")
            _stdlib_diagnostics(source, change.path, changed_lines)
            _ruff_diagnostics(temp_path, change.path, changed_lines, repo)
        else:
            _oxlint_availability(change.path, repo)
    except (AnalyzerError, GitError, OSError) as exc:
        _warn(f"{change.path}: analyzer failed ({exc}); skipped without blocking. Next: inspect the pushed blob locally")


def analyze(payload: bytes, repo: Path, remote_name: str | None = None) -> None:
    updates = _parse_updates(payload)
    scopes: dict[tuple[str, str], ChangeScope] = {}
    with tempfile.TemporaryDirectory(prefix="3d-antislop-") as temp_dir:
        temp_root = Path(temp_dir)
        for update in updates:
            if _is_zero_sha(update.local_sha):
                _warn(
                    f"{update.remote_ref}: ref deletion has no outgoing blob; skipped. "
                    "Next: inspect the remote ref deletion separately if it is consequential"
                )
                continue
            local_type = _object_type(repo, update.local_sha)
            if local_type != "commit":
                _warn(
                    f"{update.remote_ref}: local object {update.local_sha} is {local_type or 'unavailable'}, "
                    "not a commit; skipped. Next: push a commit object or inspect the ref manually"
                )
                continue
            if not _is_zero_sha(update.remote_sha) and _object_type(repo, update.remote_sha) != "commit":
                _warn(
                    f"{update.remote_ref}: remote object {update.remote_sha} is not a commit; skipped diff. "
                    "Next: inspect the remote ref before pushing"
                )
                continue
            old = _comparison_base(repo, update, remote_name)
            if old is None:
                continue
            try:
                changes = _changed_files(repo, old, update.local_sha)
            except GitError as exc:
                _warn(f"{update.remote_ref}: cannot enumerate outgoing files ({exc}). Next: inspect the ref diff locally")
                continue
            for change in changes:
                if change.status == "D" or Path(change.path).suffix.lower() not in _RELEVANT_SUFFIXES:
                    continue
                try:
                    changed_lines = _line_ranges(repo, old, update.local_sha, change)
                except GitError as exc:
                    _warn(f"{change.path}: cannot determine changed lines ({exc}). Next: inspect the ref diff locally")
                    continue
                key = (update.local_sha, change.path)
                scope = scopes.get(key)
                if scope is None:
                    scopes[key] = ChangeScope(update, change, changed_lines)
                else:
                    scope.changed_lines.update(changed_lines)
        for scope in scopes.values():
            _analyze_change(
                repo,
                scope.update,
                scope.change,
                scope.changed_lines,
                temp_root,
            )


def main(argv: list[str] | None = None) -> int:
    remote_name = argv[0] if argv and _REMOTE_NAME_RE.fullmatch(argv[0]) else None
    payload = sys.stdin.buffer.read()
    try:
        analyze(payload, _repo_root(), remote_name)
    except Exception as exc:
        _warn(f"hook analyzer failed ({exc}); skipped without blocking. Next: run the analyzer manually")
    return 0

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
