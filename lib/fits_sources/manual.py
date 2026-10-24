"""fits_sources.manual — the user's hand-entered correction rows (`3d fits corrections add|remove|list`).

WHAT: a YAML file `<config dir>/fits-corrections.yaml` (`$THREED_FITS_CORRECTIONS` overrides it) holding
`source: manual` rows in the shape of `lib/data/fits.yaml` `corrections:`. It is per machine on purpose: a
correction measured on one printer must not travel with the repository.

INVARIANTS:
  - Only `source: manual` rows live here; calibration rows come from the calibration source (S2), never from
    a hand-edited file, so a manual number can never pass for a measurement.
  - One row per scope (printer, material, nozzle, layer, shape, fit type): `add` on an existing scope replaces
    that row, `remove` deletes by scope. No duplicate scopes can exist, so "which row applies" has one answer.
  - Writes are atomic (temp file + rename) under an exclusive lock, so two agents adding rows do not lose one.
  - A broken file is an error (exit 2) naming the file, never an empty list.
"""

from __future__ import annotations

import fcntl
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

from errors import UsageError

from . import Query, Source

if TYPE_CHECKING:
    from fits import Correction

ENV = "THREED_FITS_CORRECTIONS"
FILENAME = "fits-corrections.yaml"


def store_path() -> Path:
    """The manual corrections file (not created by this call)."""
    env = os.environ.get(ENV)
    if env:
        return Path(env).expanduser()
    from cli.paths import config_dir

    return config_dir() / FILENAME


def scope(row: Correction) -> tuple[Any, ...]:
    """The identity of a row: everything but the number, the source, the date and the note."""
    from fits import norm_name

    return (
        norm_name(row.printer) if row.printer != "*" else "*",
        norm_name(row.material) if row.material != "*" else "*",
        row.nozzle_mm,
        row.layer_height_mm,
        row.shape,
        row.fit_type,
    )


def _read(path: Path) -> list[Correction]:
    import yaml

    from fits import SCHEMA, FitTableError, correction_from_dict

    if not path.is_file():
        return []
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise UsageError(
            f"cannot read {path}: {exc}",
            command="fits",
            remediation=[
                f"Fix the YAML, or delete {path} to drop every manual correction."
            ],
        ) from exc
    if doc is None:
        return []
    rows: list[Correction] = []
    try:
        if not isinstance(doc, dict) or doc.get("schema") != SCHEMA:
            raise FitTableError(f"expected a mapping with schema: {SCHEMA}")
        raw = doc.get("corrections")
        if not isinstance(raw, list):
            raise FitTableError("corrections: expected a list")
        for index, entry in enumerate(raw):
            row = correction_from_dict(entry, index=index)
            if row.source != "manual":
                raise FitTableError(
                    f"corrections[{index}].source: {row.source!r} is not 'manual' "
                    "(calibration rows come from `3d calibrate fit`)"
                )
            rows.append(row)
    except FitTableError as exc:
        raise UsageError(
            f"{path}: {exc}",
            command="fits",
            remediation=[
                "Rows are `3d fits corrections list --json` entries; "
                f"fix the file or delete {path}."
            ],
        ) from exc
    return rows


def load_rows(path: Path | None = None) -> list[Correction]:
    """Every manual row, in file order."""
    return _read(path or store_path())


def _write(path: Path, rows: list[Correction]) -> None:
    import yaml

    from fits import SCHEMA

    doc = {"schema": SCHEMA, "corrections": [r.to_dict() for r in rows]}
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(
                "# 3d fits manual corrections (3d fits corrections add|remove|list); see docs/commands/fits.md\n"
            )
            yaml.safe_dump(doc, fh, sort_keys=False)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_name(path.name + ".lock"), "a", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def add(row: Correction, path: Path | None = None) -> Correction | None:
    """Store `row`, replacing the row of the same scope; returns the replaced row (None for a new scope)."""
    target = path or store_path()
    with _locked(target):
        rows = _read(target)
        key = scope(row)
        replaced = next((r for r in rows if scope(r) == key), None)
        kept = [r for r in rows if scope(r) != key]
        _write(target, [*kept, row])
    return replaced


def remove(match: Correction, path: Path | None = None) -> Correction | None:
    """Delete the row whose scope equals `match`'s; returns it (None when there was none)."""
    target = path or store_path()
    with _locked(target):
        rows = _read(target)
        key = scope(match)
        removed = next((r for r in rows if scope(r) == key), None)
        if removed is not None:
            _write(target, [r for r in rows if scope(r) != key])
    return removed


def _load(_query: Query) -> list[Correction]:
    return load_rows()


SOURCE = Source(
    "manual",
    "rows entered with `3d fits corrections add` (per machine: <config dir>/fits-corrections.yaml)",
    _load,
)
