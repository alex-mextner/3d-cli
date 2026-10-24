"""fits_sources.calibration — the coupon rows of `3d calibrate fit` (kind `fit_clearance`), for `fits.resolve`.

WHAT: turns every current `fit_clearance` calibration record into correction rows of `source: calibration`
  (smallest free gap minus the table clearance; for a press fit the largest gripping gap), so a printer x material
  that printed a coupon resolves as `status: calibrated` with the record's `measured_at` and its id in the row's
  note. The store is 3d-api's calibration table plus the offline buffer (calibrate_fit_store.py).
WHY: S1 left this hook open (lib/fits_sources/__init__.py): one source module, no shared file edited.
INVARIANTS: stdlib-only at the top (tests/test_imports.py); a source that cannot read its store raises a
  ThreeDError and never returns a partial list silently (an unreachable 3d-api falls back to the buffer with a
  stderr note); `THREED_FIT_CALIBRATION_API=off` keeps a run away from 3d-api.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from . import Query, Source

if TYPE_CHECKING:
    from fits import Correction


def _load(_query: Query) -> list[Correction]:
    import calibrate_fit_store as store

    rows: list[Correction] = []
    for record in store.read_all().records:
        rows.extend(store.rows_of(record))
    return rows


SOURCE = Source(
    "calibration",
    "coupon rows of `3d calibrate fit` (3d-api calibration records + the offline buffer)",
    _load,
)
