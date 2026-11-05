"""Static guards of the Spoolman ↔ PrintOps sync invariant (issue #704).

The invariant holds only while every write to a Spoolman spool goes through `SyncedSpoolman` (the
bookmark follows the write). These tests fail the build the day a route reaches around it, a second
place builds a Spoolman client, the bookmark table drifts from its dataclass, or the engine invents a
drift kind it does not declare. Each checker is a function of source text, so it is also run on
hand-written bad sources to prove it can fail."""

from __future__ import annotations

import ast
import re
import sqlite3
from collections.abc import Iterable
from dataclasses import fields
from pathlib import Path

import pytest

from printops import spool_mirror, spool_sync

LIB = Path(__file__).resolve().parents[1] / "lib"
PRINTOPS = LIB / "printops"
API = PRINTOPS / "api"

# Modules that may read `ApiState.spoolman` directly: reads, extra-field registry, health, the
# Spoolman liveness check of the schedule. A module that WRITES spools takes `state.synced(conn)` or
# the `get_spoolman` dependency, never this attribute.
RAW_SPOOLMAN_READERS = frozenset(
    {
        "app.py",  # create_app + the extra-field registration task
        "routes_core.py",  # /v1/health
        "routes_reconcile.py",  # reads
        "routes_tasks.py",
        "routes_calibrations.py",
        "routes_incidents.py",  # reads
        "routes_spool_sync.py",  # lifespan: schedule only when a Spoolman is configured
    }
)

# Where a Spoolman client is constructed. Everything else receives one.
HTTP_SPOOLMAN_BUILDERS = frozenset(
    {"printops/api/app.py"}
)  # create_app, from SPOOLMAN_URL
SYNCED_SPOOLMAN_BUILDERS = frozenset({"printops/api/state.py"})  # ApiState.synced(conn)


# -- (a) who touches ApiState.spoolman -------------------------------------------------------------------


def _is_state_expr(node: ast.expr) -> bool:
    """`get_state(...)`, `<anything>.state.printops`."""
    if isinstance(node, ast.Call):
        fn = node.func
        return (isinstance(fn, ast.Name) and fn.id == "get_state") or (
            isinstance(fn, ast.Attribute) and fn.attr == "get_state"
        )
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "printops"
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "state"
    )


def _state_names(tree: ast.AST) -> set[str]:
    """Names bound to an ApiState: `x = get_state(..)`, `x: ApiState`, an `ApiState` argument."""
    states: set[str] = set()
    for node in ast.walk(tree):
        states |= _state_names_bound_by(node)
    return states


def _state_names_bound_by(node: ast.AST) -> set[str]:
    if isinstance(node, ast.Assign) and _is_state_expr(node.value):
        return {t.id for t in node.targets if isinstance(t, ast.Name)}
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        is_state = ast.unparse(node.annotation) == "ApiState" or (
            node.value is not None and _is_state_expr(node.value)
        )
        return {node.target.id} if is_state else set()
    if (
        isinstance(node, ast.arg)
        and node.annotation is not None
        and ast.unparse(node.annotation) in ("ApiState", "'ApiState'")
    ):
        return {node.arg}
    return set()


def raw_spoolman_accesses(source: str) -> list[int]:
    """Line numbers of `.spoolman` read on an ApiState: a name bound to `get_state(..)` /
    `app.state.printops` or annotated `ApiState`, or such an expression itself."""
    tree = ast.parse(source)
    states = _state_names(tree)
    lines = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Attribute) and node.attr == "spoolman"):
            continue
        base = node.value
        if _is_state_expr(base) or (isinstance(base, ast.Name) and base.id in states):
            lines.append(node.lineno)
    return sorted(lines)


def modules_touching_raw_spoolman(directory: Path) -> dict[str, list[int]]:
    out = {}
    for path in sorted(directory.glob("*.py")):
        hits = raw_spoolman_accesses(path.read_text(encoding="utf-8"))
        if hits:
            out[path.name] = hits
    return out


def test_only_the_allowed_api_modules_touch_the_raw_spoolman() -> None:
    touching = modules_touching_raw_spoolman(API)
    offenders = sorted(set(touching) - RAW_SPOOLMAN_READERS)
    assert not offenders, (
        "these modules read `ApiState.spoolman` directly, which skips the bookmark: "
        + "; ".join(f"{m}:{touching[m]}" for m in offenders)
        + ". Write through `state.synced(conn)` (or the `get_spoolman` dependency) instead."
    )
    stale = sorted(RAW_SPOOLMAN_READERS - set(touching))
    assert not stale, (
        f"{stale} no longer touch `.spoolman`: drop them from RAW_SPOOLMAN_READERS so the "
        "allowlist stays exactly what is used"
    )


def test_the_raw_spoolman_checker_can_fail(tmp_path: Path) -> None:
    """Teeth: a route that reaches around the gateway is reported, in each shape it can take."""
    bad = {
        "routes_bad_direct.py": (
            "def f(request):\n    return get_state(request).spoolman.update('spool', 1, {})\n"
        ),
        "routes_bad_alias.py": (
            "def f(request):\n    st = get_state(request)\n    return st.spoolman\n"
        ),
        "routes_bad_param.py": (
            "from printops.api.state import ApiState\n"
            "def f(state: ApiState):\n    sm = state.spoolman\n    return sm\n"
        ),
        "routes_bad_app.py": ("def f(app):\n    return app.state.printops.spoolman\n"),
    }
    clean = {
        "routes_fine.py": (
            "def f(request, conn):\n    return get_state(request).synced(conn)\n"
            "def g(moved):\n    return moved.spoolman.to_dict()\n"  # not an ApiState
        ),
    }
    for name, text in {**bad, **clean}.items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    touching = modules_touching_raw_spoolman(tmp_path)
    assert sorted(touching) == sorted(bad), touching
    offenders = sorted(set(touching) - RAW_SPOOLMAN_READERS)
    assert offenders == sorted(bad)  # what the real test would report


# -- (b) who builds a Spoolman client ----------------------------------------------------------------------


def constructor_calls(source: str, names: Iterable[str]) -> list[tuple[str, int]]:
    wanted = set(names)
    out = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call):
            fn = node.func
            name = (
                fn.id
                if isinstance(fn, ast.Name)
                else fn.attr
                if isinstance(fn, ast.Attribute)
                else ""
            )
            if name in wanted:
                out.append((name, node.lineno))
    return out


def construction_sites(root: Path, name: str) -> set[str]:
    return {
        str(path.relative_to(root.parent))
        for path in sorted(root.rglob("*.py"))
        if constructor_calls(path.read_text(encoding="utf-8"), [name])
    }


def test_spoolman_clients_are_built_in_exactly_one_place_each() -> None:
    assert construction_sites(PRINTOPS, "HttpSpoolman") == HTTP_SPOOLMAN_BUILDERS
    assert construction_sites(PRINTOPS, "SyncedSpoolman") == SYNCED_SPOOLMAN_BUILDERS


def test_no_module_outside_printops_builds_one_either() -> None:
    """3d-api is the only place a SyncedSpoolman exists; the CLI side never builds either."""
    others = [p for p in LIB.rglob("*.py") if PRINTOPS not in p.parents]
    built = {
        str(p.relative_to(LIB))
        for p in others
        if constructor_calls(p.read_text(encoding="utf-8"), ["SyncedSpoolman"])
    }
    assert built == set()


def test_the_construction_checker_can_fail(tmp_path: Path) -> None:
    pkg = tmp_path / "printops"
    pkg.mkdir()
    (pkg / "sneaky.py").write_text(
        "from printops.spool_mirror import SyncedSpoolman\n"
        "def f(raw, conn):\n    return SyncedSpoolman(raw, conn)\n",
        encoding="utf-8",
    )
    (pkg / "qualified.py").write_text(
        "import printops.spoolman as sm\ndef f(url):\n    return sm.HttpSpoolman(url)\n",
        encoding="utf-8",
    )
    (pkg / "fine.py").write_text(
        "from printops.spool_mirror import SyncedSpoolman\n"
        "def f(sm: SyncedSpoolman):\n    return sm\n",  # an annotation is not a construction
        encoding="utf-8",
    )
    assert construction_sites(pkg, "SyncedSpoolman") == {"printops/sneaky.py"}
    assert construction_sites(pkg, "HttpSpoolman") == {"printops/qualified.py"}


# -- (c) the bookmark table ----------------------------------------------------------------------------------


def test_migration_0105_creates_the_bookmark_table_with_the_columns_of_seen() -> None:
    [path] = sorted((PRINTOPS / "migrations").glob("0105_*.sql"))
    assert path.name == "0105_spool_sync.sql"
    sql = path.read_text(encoding="utf-8")
    body = re.search(r"CREATE TABLE spool_sync \((.*?)\n\);", sql, re.S)
    assert body is not None, "0105 must CREATE TABLE spool_sync"
    columns = [
        m.group(1)
        for line in body.group(1).splitlines()
        if (m := re.match(r"\s+([a-z_]+)\s+(?:INTEGER|TEXT|REAL)\b", line))
    ]
    assert columns == [f.name for f in fields(spool_mirror.Seen)] + ["seen_at"]


def test_the_migrated_database_has_the_same_columns(
    printops_db: sqlite3.Connection,
) -> None:
    info = printops_db.execute("PRAGMA table_info(spool_sync)").fetchall()
    assert [r["name"] for r in info] == [f.name for f in fields(spool_mirror.Seen)] + [
        "seen_at"
    ]
    assert [r["name"] for r in info if r["pk"]] == ["spool_id"]


def test_a_bookmark_survives_a_round_trip_through_the_table(
    printops_db: sqlite3.Connection,
) -> None:
    """Every field of `Seen` is persisted and read back (a column missing from `store`/`load` would
    silently lose it)."""
    seen = spool_mirror.Seen(
        7, spool_mirror.PRESENT, 412.5, "2026-11-01T10:00:00Z", True, "slot:u1/T3"
    )
    spool_mirror.store(printops_db, seen)
    assert spool_mirror.load(printops_db, 7) == seen
    gone = spool_mirror.Seen(7, spool_mirror.DELETED, None, None, False, "")
    spool_mirror.store(printops_db, gone)
    assert spool_mirror.load(printops_db, 7) == gone
    assert spool_mirror.load_all(printops_db) == {7: gone}


# -- (d) the drift kinds ---------------------------------------------------------------------------------------


def _assigned_kinds(tree: ast.AST, var: str, constants: dict[str, str]) -> list[str]:
    """Every `KIND_*` constant a local `var` can be assigned: `var = KIND_X` or the matching
    position of a tuple assignment `a, var = (KIND_X, ..) if c else (KIND_Y, ..)`."""
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            for cand in _assigned_candidates(target, node.value, var):
                found += _kinds_named_in(cand, constants)
    return found


def _kinds_named_in(expr: ast.expr, constants: dict[str, str]) -> list[str]:
    return [
        constants[c.id]
        for c in ast.walk(expr)
        if isinstance(c, ast.Name) and c.id in constants
    ]


def _assigned_candidates(target: ast.expr, value: ast.expr, var: str) -> list[ast.expr]:
    """The expressions one assignment target binds to `var` (none when it does not bind it)."""
    if isinstance(target, ast.Name):
        return [value] if target.id == var else []
    if not isinstance(target, ast.Tuple):
        return []
    names = [e.id if isinstance(e, ast.Name) else None for e in target.elts]
    if var not in names:
        return []
    at = names.index(var)
    return [
        t.elts[at]
        for t in ast.walk(value)
        if isinstance(t, ast.Tuple) and len(t.elts) > at
    ]


def drift_kinds_used(source: str, constants: dict[str, str]) -> list[tuple[str, int]]:
    """The kind of every `Drift(...)` call: its second positional or `kind=` argument, a string, a
    `KIND_*` name, or a local variable assigned `KIND_*` names. Anything else (and a variable
    nothing resolves) is reported as '<dynamic>'."""
    tree = ast.parse(source)
    out = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "Drift"
        ):
            continue
        arg = (
            node.args[1]
            if len(node.args) > 1
            else next((k.value for k in node.keywords if k.arg == "kind"), None)
        )
        out += [(k, node.lineno) for k in _kinds_of_arg(tree, arg, constants)]
    return out


def _kinds_of_arg(tree: ast.AST, arg: ast.expr | None, constants: dict[str, str]) -> list[str]:
    """The kind strings one `Drift(...)` kind argument can hold; '<dynamic>' when none resolve."""
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        return [arg.value]
    if isinstance(arg, ast.Name) and arg.id in constants:
        return [constants[arg.id]]
    if isinstance(arg, ast.Name) and (kinds := _assigned_kinds(tree, arg.id, constants)):
        return kinds
    return ["<dynamic>"]


def kind_constants() -> dict[str, str]:
    return {
        name: value
        for name, value in vars(spool_sync).items()
        if name.startswith("KIND_") and isinstance(value, str)
    }


def test_kinds_are_unique_and_every_declared_constant_is_listed() -> None:
    assert len(spool_sync.KINDS) == len(set(spool_sync.KINDS))
    assert set(kind_constants().values()) == set(spool_sync.KINDS)
    assert len(set(kind_constants().values())) == len(
        kind_constants()
    )  # no two names, one kind
    assert set(spool_sync.KINDS) >= spool_sync.BLOCKING_KINDS


def test_every_drift_the_engine_builds_has_a_declared_kind_and_every_kind_is_built() -> (
    None
):
    source = (PRINTOPS / "spool_sync.py").read_text(encoding="utf-8")
    used = drift_kinds_used(source, kind_constants())
    assert used, "no Drift(...) call found: the scan is broken"
    undeclared = sorted({(k, n) for k, n in used if k not in spool_sync.KINDS})
    assert not undeclared, f"Drift(...) with a kind outside KINDS: {undeclared}"
    dead = sorted(set(spool_sync.KINDS) - {k for k, _ in used})
    assert not dead, f"KINDS declares kinds no Drift(...) ever uses: {dead}"


def test_the_kind_scan_can_fail() -> None:
    consts = {"KIND_A": "a", "KIND_B": "b"}
    src = (
        "Drift(1, KIND_A, 's', True, '')\n"
        "Drift(1, 'b', 's', True, '')\n"
        "Drift(1, kind='typo', summary='s')\n"
        "Drift(1, KIND_MISSING, 's', True, '')\n"
        "Drift(1, make_kind(), 's', True, '')\n"
    )
    assert [k for k, _ in drift_kinds_used(src, consts)] == [
        "a",
        "b",
        "typo",
        "<dynamic>",
        "<dynamic>",
    ]
    declared = {"a", "b"}
    assert [k for k, _ in drift_kinds_used(src, consts) if k not in declared] == [
        "typo",
        "<dynamic>",
        "<dynamic>",
    ]


@pytest.mark.parametrize("kind", spool_sync.KINDS)
def test_a_kind_is_a_stable_snake_case_name(kind: str) -> None:
    """Kinds are anomaly `kind`s, JSON `kind`s and docs anchors: lowercase words with underscores."""
    assert re.fullmatch(r"[a-z]+(_[a-z]+)*", kind)
