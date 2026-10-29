"""`3d mix record` (#417): the calibration records a readout yields — API bodies and buffer lines."""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
from mix_fit_fixtures import simulated_readout

from printops import history_buffer as hb
from slicing import mix_record
from slicing.mix_fit import fit

SHA = "c" * 64
AT = "2026-10-24T12:00:00Z"


def _plan(readout: dict, fit_rec: dict | None = None) -> mix_record.Plan:
    return mix_record.plan(
        readout,
        SHA,
        "u1",
        readout_name="wall.readout.json",
        at=AT,
        fit=fit_rec,
        actor="cli:test",
    )


def test_filament_colours_come_first_then_the_mixes(tmp_path: Path) -> None:
    pl = _plan(simulated_readout(tmp_path))
    assert [i.kind for i in pl.items] == ["filament_color"] * 3 + ["mix_color"] * 4
    assert pl.skipped == []
    fc = pl.items[0].body
    assert (
        fc["kind"] == "filament_color"
        and fc["method"] == "mix_tester_photo"
        and fc["printer"] == "u1"
    )
    assert (
        fc["filament_id"] == 21
        and fc["values"]["hex"] == "#1E1E21"
        and fc["values"]["spoolman_hex"] == "#000000"
    )
    assert set(fc["values"]) == {
        "hex",
        "spoolman_hex",
        "delta_e",
        "lab_l",
        "lab_a",
        "lab_b",
    }
    mc = pl.items[3].body
    assert (
        mc["kind"] == "mix_color"
        and mc["mix"].startswith("h0.12:f")
        and "filament_id" not in mc
    )
    assert set(mc["values"]) == {"hex", "predicted_hex", "delta_e_predicted"}


def test_every_record_carries_the_readout_sha_the_photo_and_both_as_evidence(
    tmp_path: Path,
) -> None:
    r = simulated_readout(tmp_path)
    for item in _plan(r).items:
        p = item.body["params"]
        assert (
            p["readout_sha256"] == SHA
            and p["photo_sha256"] == "a" * 64
            and p["swatch"] == item.number
        )
        assert (
            p["manifest_id"] == r["manifest"]["id"] and p["source"] == "3d mix record"
        )
        assert item.body["evidence"] == [
            f"photo phone.jpg sha256:{'a' * 64}",
            f"readout wall.readout.json sha256:{SHA}",
        ]
        assert (
            item.line["params"] == p and item.line["evidence"] == item.body["evidence"]
        )


def test_a_fit_replaces_the_measured_pure_colour_of_the_filament_record(
    tmp_path: Path,
) -> None:
    r = simulated_readout(tmp_path)
    fitted = fit([r])
    by_fid = {f["filament_id"]: f["fitted_hex"] for f in fitted["filaments"]}
    pl = _plan(r, fitted)
    for item in pl.items[:3]:
        assert item.body["values"]["hex"] == by_fid[item.body["filament_id"]]
        assert item.body["values"]["hex"] != next(
            s["measured_hex"]
            for s in r["swatches"]
            if s["kind"] == "reference" and s["number"] == item.number
        )
    assert [i.body["values"]["hex"] for i in pl.items[3:]] == [
        s["measured_hex"] for s in r["swatches"] if s["kind"] == "candidate"
    ]


def test_the_buffer_line_is_a_settings_change_the_importer_parses(
    tmp_path: Path,
) -> None:
    for item in _plan(simulated_readout(tmp_path)).items:
        (line,) = hb.parse_lines([__import__("json").dumps(item.line)])
        d = line.data
        assert (
            d["schema"] == hb.SCHEMA
            and line.uid == item.uid
            and d["status"] == "measured"
        )
        assert (
            d["printer"] == "u1"
            and d["kind"] == item.kind
            and d["new"] == item.body["values"]
        )
        assert d["key"] == f"{item.kind}/{item.scope}" and d["source"] == "photo"
        assert ("filament_id" in d) is (item.kind == "filament_color") and (
            "mix" in d
        ) is (item.kind == "mix_color")


def test_uids_are_stable_per_readout_printer_kind_and_swatch(tmp_path: Path) -> None:
    r = simulated_readout(tmp_path)
    a, b = _plan(r), _plan(copy.deepcopy(r))
    assert [i.uid for i in a.items] == [i.uid for i in b.items]
    assert len({i.uid for i in a.items}) == len(a.items)
    other = mix_record.plan(r, "d" * 64, "u1", readout_name="x", at=AT)
    assert not {i.uid for i in a.items} & {i.uid for i in other.items}
    k1 = mix_record.plan(r, SHA, "k1se", readout_name="x", at=AT)
    assert not {i.uid for i in a.items} & {
        i.uid for i in k1.items
    }  # another printer, another uid


def test_two_heads_of_one_filament_do_not_share_a_uid(tmp_path: Path) -> None:
    """White PLA on two heads is one Spoolman filament: two reference swatches, two records, two uids."""
    r = simulated_readout(tmp_path, filament_ids=(21, 23, 23))
    pl = _plan(r)
    refs = [i for i in pl.items if i.kind == "filament_color"]
    assert len(refs) == 3 and len({i.uid for i in refs}) == 3


def test_the_buffer_helper_lists_uids_and_refuses_a_bad_printer_name_before_reading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert hb.buffered_uids("u1") == set()
    items = _plan(simulated_readout(tmp_path / "t")).items
    hb.append("u1", items[0].line)
    assert hb.buffered_uids("u1") == {items[0].uid}
    for bad in ("../x", ".hidden", ""):
        with pytest.raises(hb.HistoryBufferError, match="not a fleet printer name"):
            hb.buffered_uids(bad)


def test_swatches_without_a_filament_id_or_mix_key_are_skipped_with_the_reason(
    tmp_path: Path,
) -> None:
    r = simulated_readout(tmp_path)
    for s in r["swatches"]:
        if s["kind"] == "reference" and s["slot"] == 4:
            s["calibration"] = {"kind": "filament_color", "filament_id": None}
        if s["kind"] == "candidate" and "4" in s["pattern"]:
            s["calibration"] = {"kind": "mix_color", "mix": None}
    pl = _plan(r)
    assert [i.kind for i in pl.items].count("filament_color") == 2
    assert any("T4 pure" in t and "no Spoolman filament id" in t for t in pl.skipped)
    assert any("no mix key" in t for t in pl.skipped)
    for s in r["swatches"]:
        s["calibration"] = {}
    with pytest.raises(mix_record.RecordError, match="yields no record"):
        _plan(r)


def test_bad_readouts_and_printers_are_refused(tmp_path: Path) -> None:
    r = simulated_readout(tmp_path)
    with pytest.raises(mix_record.RecordError, match="schema must be mix-readout/1"):
        _plan({**r, "schema": "x"})
    with pytest.raises(mix_record.RecordError, match="printer is required"):
        mix_record.plan(r, SHA, "", readout_name="x", at=AT)


def test_existing_finds_the_record_of_the_same_readout_and_swatch(
    tmp_path: Path,
) -> None:
    items = _plan(simulated_readout(tmp_path)).items
    stored = [{"id": 7, "kind": i.kind, "params": i.body["params"]} for i in items[:2]]
    assert mix_record.existing(items[0], stored, SHA)["id"] == 7  # type: ignore[index]
    assert mix_record.existing(items[1], stored, SHA)["id"] == 7  # type: ignore[index]
    assert mix_record.existing(items[2], stored, SHA) is None
    assert mix_record.existing(items[0], stored, "e" * 64) is None
    assert mix_record.existing(items[3], stored, SHA) is None  # another kind
