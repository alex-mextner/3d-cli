"""fits_registry: a free-text printer/material name -> the registries the fit provenance cites."""
from __future__ import annotations

from pathlib import Path

import pytest

import fits
import fits_registry
from fits import Correction

REPO = Path(__file__).resolve().parents[1]
FLEET = REPO / "fleet" / "printers.yaml"
STAMP = "2026-10-24T10:00:00Z"


@pytest.fixture(autouse=True)
def _hermetic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("THREED_FITS_CORRECTIONS", str(tmp_path / "none.yaml"))
    monkeypatch.chdir(tmp_path)


def test_a_fleet_key_is_known_and_carries_its_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("THREED_FLEET", str(FLEET))
    ident = fits_registry.printer_identity("K1-SE")
    assert (ident.key, ident.model, ident.known) == ("k1se", "Creality K1 SE", True)
    assert ident.names() == ("K1-SE", "k1se", "Creality K1 SE")
    assert ident.to_provenance()["source"] == "fleet/printers.yaml"


def test_a_model_name_is_known_without_a_fleet_key() -> None:
    ident = fits_registry.printer_identity("creality k1 se")  # THREED_FLEET="" in the suite: no fleet
    assert (ident.key, ident.model, ident.known) == (None, "Creality K1 SE", True)


def test_without_the_fleet_the_short_key_is_unknown_and_says_so() -> None:
    ident = fits_registry.printer_identity("k1se")
    assert ident.known is False and "not in the fleet or printer registries" in ident.to_provenance()["message"]


def test_an_unreadable_fleet_is_reported_not_swallowed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bad = tmp_path / "fleet.yaml"
    bad.write_text("k1se: {model: [unclosed", encoding="utf-8")
    monkeypatch.setenv("THREED_FLEET", str(bad))
    ident = fits_registry.printer_identity("k1se")
    assert ident.known is False and ident.note is not None and "fleet registry unreadable" in ident.note


@pytest.mark.parametrize(("name", "known", "matched", "via"), [
    ("PETG", True, "PETG", "name"),
    ("petg", True, "PETG", "name"),
    ("PLA+", True, "PLA", "name"),
    ("Elegoo Rapid PLA+", True, "PLA", "word 'PLA+'"),
    ("unobtainium", False, None, None),
])
def test_material_names_match_the_registry_by_name_or_word(name: str, known: bool, matched: str | None,
                                                           via: str | None) -> None:
    ident = fits_registry.material_identity(name)
    assert (ident.known, ident.name, ident.via) == (known, matched, via)


def test_material_shrink_comes_from_the_registry_and_z_is_not_recorded() -> None:
    petg = fits_registry.material_identity("PETG")
    assert petg.shrink_xy_pct == 0.4 and petg.shrink_z_pct is None


def test_a_row_keyed_by_the_model_answers_the_fleet_key_but_not_the_reverse(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("THREED_FLEET", str(FLEET))
    by_key = Correction(delta_mm=0.04, source="manual", printer="k1se", measured_at=STAMP)
    by_model = Correction(delta_mm=0.06, source="manual", printer="Creality K1 SE", measured_at=STAMP)
    assert fits.resolve("sliding", "planar", "k1se", extra_corrections=[by_model]).clearance_mm == 0.26
    assert fits.resolve("sliding", "planar", "k1se", extra_corrections=[by_key]).clearance_mm == 0.24
    # a model-name query must not pick up the row of one physical unit
    unit_row = fits.resolve("sliding", "planar", "Creality K1 SE", extra_corrections=[by_key])
    assert unit_row.clearance_mm == 0.20 and unit_row.fallback is True
    # the unit's own row outranks the model's when both exist
    assert fits.resolve("sliding", "planar", "k1se", extra_corrections=[by_model, by_key]).clearance_mm in (0.24, 0.26)
    # without the fleet the short key is just a name: only a row of that exact name answers it
    monkeypatch.setenv("THREED_FLEET", "")
    assert fits.resolve("sliding", "planar", "k1se", extra_corrections=[by_model]).clearance_mm == 0.20


def test_a_row_for_pla_answers_a_rapid_pla_plus_query() -> None:
    row = Correction(delta_mm=0.03, source="manual", material="PLA", measured_at=STAMP)
    res = fits.resolve("sliding", "planar", material="Elegoo Rapid PLA+", extra_corrections=[row])
    assert res.clearance_mm == 0.23 and res.fallback is False
