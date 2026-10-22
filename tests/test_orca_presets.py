"""orca_presets: finding a preset by name and flattening its `inherits` chain."""
from __future__ import annotations

from pathlib import Path

import pytest
from retarget_fixtures import write_presets

from errors import InvalidArgument
from slicing import orca_presets


@pytest.fixture
def presets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A preset dir, with the host's slicer data and repo profiles hidden from the search."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("THREED_PROFILES_DIR", str(tmp_path / "no-profiles"))
    return write_presets(tmp_path / "presets")


def test_fuzzy_query_finds_the_instantiated_preset_not_the_model_descriptor(presets: Path) -> None:
    ref = orca_presets.PresetCatalog([presets]).find("machine", "testk1")
    assert ref.name == "Test K1 0.4 nozzle"
    assert ref.origin == "extra"


def test_flatten_fills_inherited_keys_and_child_wins(presets: Path) -> None:
    catalog = orca_presets.PresetCatalog([presets])
    flat = catalog.flatten(catalog.find("machine", "Test K1 0.4 nozzle"))
    assert flat["machine_end_gcode"] == "END_PRINT"  # child overrides the base's BASE_END
    assert flat["gcode_flavor"] == "klipper"  # only in the abstract base
    assert flat["nozzle_diameter"] == ["0.4"]
    assert "name" not in flat and "inherits" not in flat and "instantiation" not in flat


def test_preset_found_by_its_json_name_when_the_file_stem_differs(tmp_path: Path, presets: Path) -> None:
    (presets / "vendor" / "process" / "0.20mm Test Process 0.4.json").write_text(
        '{"name": "0.20mm Renamed", "layer_height": "0.2"}', encoding="utf-8"
    )
    assert orca_presets.PresetCatalog([presets]).find("process", "0.20mm Renamed").name == "0.20mm Renamed"
    assert orca_presets.PresetCatalog([presets]).find("process", "0.20mm Test Process 0.4").name == "0.20mm Renamed"


def test_abstract_base_is_never_selected_but_still_inherited(presets: Path) -> None:
    with pytest.raises(InvalidArgument):
        orca_presets.PresetCatalog([presets]).find("process", "fdm_test_process")
    catalog = orca_presets.PresetCatalog([presets])
    flat = catalog.flatten(catalog.find("process", "0.20mm Test Process"))
    assert flat["support_top_z_distance"] == "0.2" and flat["layer_height"] == "0.2"


def test_repo_profiles_beat_system_presets_and_extra_dirs_beat_both(
    tmp_path: Path, presets: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "profiles"
    (repo / "orca" / "machine").mkdir(parents=True)
    (repo / "orca" / "machine" / "Test K1 0.4 nozzle.json").write_text(
        '{"name": "Test K1 0.4 nozzle", "printable_height": "999"}', encoding="utf-8"
    )
    monkeypatch.setenv("THREED_PROFILES_DIR", str(repo))
    assert orca_presets.PresetCatalog().find("machine", "testk1").origin == "repo"
    assert orca_presets.PresetCatalog([presets]).find("machine", "testk1").origin == "extra"


def test_unknown_preset_lists_what_was_found(presets: Path) -> None:
    with pytest.raises(InvalidArgument) as err:
        orca_presets.PresetCatalog([presets]).find("machine", "test-dual-typo-xyz")
    text = err.value.render(color=False)
    assert "test-dual-typo-xyz" in text and "Test K1 0.4 nozzle" in text and "--preset-dir" in text


def test_missing_parent_is_reported_with_the_child_path(tmp_path: Path, presets: Path) -> None:
    orphan = presets / "vendor" / "machine" / "Orphan 0.4 nozzle.json"
    orphan.write_text('{"name": "Orphan 0.4 nozzle", "inherits": "does not exist"}', encoding="utf-8")
    ref = orca_presets.PresetCatalog([presets]).find("machine", "orphan")
    with pytest.raises(InvalidArgument) as err:
        orca_presets.PresetCatalog([presets]).flatten(ref)
    assert "does not exist" in err.value.render(color=False)


def test_inherits_cycle_is_an_error_not_a_hang(presets: Path) -> None:
    (presets / "vendor" / "machine" / "Loop A.json").write_text('{"name": "Loop A", "inherits": "Loop B"}', encoding="utf-8")
    (presets / "vendor" / "machine" / "Loop B.json").write_text('{"name": "Loop B", "inherits": "Loop A"}', encoding="utf-8")
    with pytest.raises(InvalidArgument):
        catalog = orca_presets.PresetCatalog([presets])
        catalog.flatten(catalog.find("machine", "Loop A"))


def test_parent_is_taken_from_the_childs_own_vendor_folder(presets: Path) -> None:
    """Vendor folders each ship their own `fdm_machine_common`; the child must get ITS vendor's."""
    for vendor, flavor in (("Acme", "marlin"), ("Zeta", "klipper")):
        folder = presets / vendor / "machine"
        folder.mkdir(parents=True)
        (folder / "fdm_machine_common.json").write_text(
            f'{{"name": "fdm_machine_common", "instantiation": "false", "gcode_flavor": "{flavor}"}}', encoding="utf-8"
        )
        (folder / f"{vendor} Printer.json").write_text(
            f'{{"name": "{vendor} Printer", "inherits": "fdm_machine_common"}}', encoding="utf-8"
        )
    catalog = orca_presets.PresetCatalog([presets])
    assert catalog.flatten(catalog.find("machine", "Zeta Printer"))["gcode_flavor"] == "klipper"
    assert catalog.flatten(catalog.find("machine", "Acme Printer"))["gcode_flavor"] == "marlin"


def test_catalog_scans_each_kind_once(presets: Path) -> None:
    catalog = orca_presets.PresetCatalog([presets])
    first = catalog.find("machine", "testk1")
    (presets / "vendor" / "machine" / "Late 0.4 nozzle.json").write_text('{"name": "Late 0.4 nozzle"}', encoding="utf-8")
    assert catalog.find("machine", "testk1") == first
    with pytest.raises(InvalidArgument):
        catalog.find("machine", "late")  # added after the scan: not seen by this catalog
    assert orca_presets.PresetCatalog([presets]).find("machine", "late").name == "Late 0.4 nozzle"


def test_matches_lists_every_candidate_best_first(presets: Path) -> None:
    catalog = orca_presets.PresetCatalog([presets])
    names = [r.name for r in catalog.matches("machine", "test")]
    assert names == ["Test K1 0.4 nozzle", "Test Dual 0.4 nozzle"]  # shortest first among equal origins; model descriptor skipped
    assert catalog.matches("machine", "does-not-exist") == []
    assert [r.name for r in catalog.matches("machine", "Test Dual 0.4 nozzle")] == ["Test Dual 0.4 nozzle"]  # exact: no others


def test_stock_nozzle_is_a_number_not_a_substring(presets: Path) -> None:
    for name in ("Acme 0.45 nozzle", "Acme 0.4 nozzle", "Acme 0.2 nozzle"):
        (presets / "vendor" / "machine" / f"{name}.json").write_text(f'{{"name": "{name}"}}', encoding="utf-8")
    assert orca_presets.PresetCatalog([presets]).find("machine", "acme").name == "Acme 0.4 nozzle"
