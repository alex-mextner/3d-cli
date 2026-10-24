"""fits_sources — where the correction rows of `fits.resolve` come from, one module per source (self-registering).

WHAT: `fits.resolve` is pure: it applies the table's rows plus the extra rows it is handed. This package is the
one place that hands it the rest: the manual rows a user keeps with `3d fits corrections add`
(`manual.py`) and any other source that drops a module here, e.g. the calibration rows of `3d calibrate fit record`
(`calibration.py`, slice S2 #573).

CONTRACT (the hook for S2): a module `lib/fits_sources/<name>.py` defines `SOURCE = Source(name, summary,
load)`. `load(query)` returns `fits.Correction` rows with `source` set to their real origin (`calibration` for a
measured row: it then outranks a manual and a default row of the same specificity, and its provenance says
`calibrated`). It may use the query (printer/material/nozzle/layer) to read less, or ignore it: `fits.resolve`
does the matching. A source that cannot read its store raises a `ThreeDError` (exit 2/127), it never returns a
partial list silently. `discover()` imports every non-underscore module here, so adding a source edits no
shared file. Modules stay stdlib-only at the top (lazy imports inside `load`): `tests/test_imports.py`.

ACCESSED VIA: `fits_sources.resolve(...)` from `3d fits`, `3d fit-check --fit`, `3d fit-niche --fit` - all three
share this one entry so the same inputs give the same clearance.
"""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fits import Correction, FitTable, Resolution


@dataclass(frozen=True)
class Query:
    """What the caller is resolving; a source may use it to read less."""

    printer: str | None = None
    material: str | None = None
    nozzle: float | None = None
    layer: float | None = None


@dataclass(frozen=True)
class Source:
    name: str
    summary: str
    load: Callable[[Query], list[Correction]]


def discover() -> dict[str, Source]:
    """name -> Source for every `fits_sources/<name>.py` that defines SOURCE (names are unique)."""
    found: dict[str, Source] = {}
    for info in pkgutil.iter_modules(__path__):
        if info.name.startswith("_"):
            continue
        module = importlib.import_module(f"{__name__}.{info.name}")
        source = getattr(module, "SOURCE", None)
        if isinstance(source, Source):
            if source.name in found:
                raise ValueError(f"duplicate fits source {source.name!r}")
            found[source.name] = source
    return dict(sorted(found.items()))


def collect(query: Query) -> list[Correction]:
    """The rows of every registered source, in source-name order."""
    rows: list[Correction] = []
    for source in discover().values():
        rows.extend(source.load(query))
    return rows


def resolve(
    fit_type: str,
    shape: str,
    printer: str | None = None,
    material: str | None = None,
    nozzle: float | None = None,
    layer: float | None = None,
    *,
    table: FitTable | None = None,
    command: str = "fits",
) -> Resolution:
    """`fits.resolve` with the rows of every registered source (manual, calibration, ...) applied."""
    import fits

    rows = collect(Query(printer, material, nozzle, layer))
    return fits.resolve(
        fit_type,
        shape,
        printer,
        material,
        nozzle,
        layer,
        extra_corrections=rows,
        table=table,
        command=command,
    )
