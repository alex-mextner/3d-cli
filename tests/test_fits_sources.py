"""fits_sources: the manual correction store and the drop-in hook S2 uses for calibration rows."""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

import fits
import fits_sources
from errors import UsageError
from fits import Correction
from fits_sources import Query, manual

STAMP = "2026-10-24T10:00:00Z"


@pytest.fixture(autouse=True)
def _store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "fits-corrections.yaml"
    monkeypatch.setenv("THREED_FITS_CORRECTIONS", str(path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.chdir(tmp_path)
    return path


def row(delta: float = 0.05, **scope: object) -> Correction:
    fields: dict[str, object] = {
        "printer": "k1se",
        "material": "PETG",
        "nozzle_mm": 0.4,
        **scope,
    }
    return Correction(delta_mm=delta, source="manual", measured_at=STAMP, **fields)  # type: ignore[arg-type]


def test_the_store_defaults_to_the_config_dir_and_the_env_overrides_it(
    tmp_path: Path, _store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert manual.store_path() == _store
    monkeypatch.delenv("THREED_FITS_CORRECTIONS")
    assert (
        manual.store_path() == tmp_path / "config" / "3d-cli" / "fits-corrections.yaml"
    )


def test_an_absent_store_has_no_rows_and_is_not_created_by_reading(
    _store: Path,
) -> None:
    assert manual.load_rows() == [] and not _store.exists()


def test_add_persists_and_the_same_scope_replaces_the_row() -> None:
    assert manual.add(row(0.05)) is None
    replaced = manual.add(row(0.08, note="measured again"))
    assert replaced is not None and replaced.delta_mm == 0.05
    manual.add(row(0.02, nozzle_mm=0.6))
    rows = manual.load_rows()
    assert sorted((r.nozzle_mm, r.delta_mm) for r in rows) == [(0.4, 0.08), (0.6, 0.02)]
    assert [r.note for r in rows if r.nozzle_mm == 0.4] == ["measured again"]


def test_scope_ignores_case_and_punctuation_of_the_names() -> None:
    assert manual.scope(row(printer="K1-SE", material="petg")) == manual.scope(
        row(printer="k1se", material="PETG")
    )
    assert manual.scope(row(layer_height_mm=0.2)) != manual.scope(row())


def test_remove_deletes_by_scope_and_reports_a_miss() -> None:
    manual.add(row(0.05))
    assert manual.remove(row(0.0, nozzle_mm=0.6)) is None
    gone = manual.remove(row(0.0))
    assert gone is not None and gone.delta_mm == 0.05 and manual.load_rows() == []


def test_a_stored_row_is_a_correction_the_resolver_applies_exactly() -> None:
    base = fits_sources.resolve("sliding", "round").clearance_mm
    manual.add(row(0.07, material="PLA"))
    res = fits_sources.resolve("sliding", "round", "k1se", "PLA", 0.4)
    assert res.clearance_mm == round(base + 0.07, 4) and res.fallback is False
    corr = next(p for p in res.provenance if p["kind"] == "correction")
    assert (
        corr["source"] == "manual"
        and corr["status"] == "manual override"
        and corr["delta_mm"] == 0.07
    )
    # another printer, another nozzle: untouched
    assert (
        fits_sources.resolve("sliding", "round", "u1", "PLA", 0.4).clearance_mm == base
    )
    assert (
        fits_sources.resolve("sliding", "round", "k1se", "PLA", 0.6).clearance_mm
        == base
    )
    # the pure resolver never reads the store
    assert fits.resolve("sliding", "round", "k1se", "PLA", 0.4).clearance_mm == base


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("schema: [unclosed", "cannot read"),
        ("schema: 3d.fits/2\ncorrections: []\n", "schema"),
        ("schema: 3d.fits/1\ncorrections: {}\n", "corrections: expected a list"),
        ("schema: 3d.fits/1\ncorrections: [{printer: k1se}]\n", "missing required key"),
        (
            "schema: 3d.fits/1\ncorrections: [{printer: k1se, material: PLA, delta_mm: 0.1, source: calibration,"
            " measured_at: '2026-10-24T10:00:00Z'}]\n",
            "not 'manual'",
        ),
    ],
)
def test_a_broken_store_is_a_usage_error_naming_the_file(
    _store: Path, body: str, message: str
) -> None:
    _store.write_text(body, encoding="utf-8")
    with pytest.raises(UsageError, match=message) as err:
        manual.load_rows()
    assert str(_store) in str(err.value) or "cannot read" in str(err.value)


def test_writes_leave_no_temp_files_behind(_store: Path) -> None:
    manual.add(row())
    assert sorted(p.name for p in _store.parent.iterdir() if p.name != _store.name) == [
        f"{_store.name}.lock"
    ]


# --------------------------------------------------------------------------- the S2 hook
def test_discover_lists_the_manual_source_and_the_contract_is_one_module_one_source() -> (
    None
):
    found = fits_sources.discover()
    assert "manual" in found and found["manual"].name == "manual"
    assert all(isinstance(s, fits_sources.Source) for s in found.values())


def test_a_calibration_module_dropped_into_the_package_feeds_resolve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """what slice S2 does: add lib/fits_sources/calibration.py defining SOURCE; nothing else is edited"""
    drop = tmp_path / "dropins"
    drop.mkdir()
    (drop / "calibration.py").write_text(
        textwrap.dedent(
            """
            from fits import Correction
            from fits_sources import Query, Source


            def load(query: Query) -> list[Correction]:
                return [Correction(delta_mm=0.12, source="calibration", printer="k1se", material="PLA",
                                   nozzle_mm=0.4, fit_type="sliding", shape="round",
                                   measured_at="2026-10-24T11:00:00Z", note="smallest free gap 0.32 mm")]


            SOURCE = Source("calibration", "coupon rows", load)
            """
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(fits_sources, "__path__", [*fits_sources.__path__, str(drop)])
    monkeypatch.delitem(sys.modules, "fits_sources.calibration", raising=False)

    assert set(fits_sources.discover()) == {"calibration", "manual"}
    manual.add(
        row(0.30, material="PLA", fit_type="sliding", shape="round")
    )  # a manual row of the same scope
    res = fits_sources.resolve("sliding", "round", "k1se", "PLA", 0.4)
    corr = next(p for p in res.provenance if p["kind"] == "correction")
    assert (
        corr["source"] == "calibration" and corr["status"] == "calibrated"
    )  # calibration outranks manual
    assert res.clearance_mm == 0.32
    assert [
        p["source"] for p in res.provenance if p["kind"] == "correction_superseded"
    ] == ["manual"]
    assert fits_sources.collect(Query())[0].source == "calibration"
    monkeypatch.delitem(sys.modules, "fits_sources.calibration", raising=False)


def test_a_source_that_cannot_read_its_store_fails_loudly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(_query: Query) -> list[Correction]:
        raise UsageError("calibration store unreadable", command="fits")

    monkeypatch.setattr(
        fits_sources,
        "discover",
        lambda: {"x": fits_sources.Source("x", "broken", boom)},
    )
    with pytest.raises(UsageError, match="calibration store unreadable"):
        fits_sources.resolve("sliding", "planar")
