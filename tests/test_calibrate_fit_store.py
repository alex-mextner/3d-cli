"""calibrate_fit_store + fits_sources.calibration: fit_clearance records in 3d-api and the offline buffer (#573)."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from fleet_fixtures import REPO_FLEET
from printops_fakes import FakeSpoolman

import calibrate_fit as cf
import calibrate_fit_store as store
import fit_coupon
import fits_sources
from errors import ThreeDError
from printops.client import ApiClient, ApiError, Response
from printops.spoolman import ensure_extra_fields, encode_extra


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("THREED_FLEET", str(REPO_FLEET))
    monkeypatch.setenv("THREED_FITS_CORRECTIONS", str(tmp_path / "fits-corrections.yaml"))
    monkeypatch.delenv("PRINTOPS_POLICY", raising=False)
    monkeypatch.delenv("PRINTOPS_CALIBRATIONS", raising=False)


def readings(*picks: tuple[str, str, str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for fit, shape, result in picks:
        out += [r.to_dict() for r in cf.parse_result(result, fit_coupon.ladders((fit,), (shape,))[0])]
    return out


def new_record(**kw: Any) -> store.NewRecord:
    base: dict[str, Any] = {
        "printer": "k1se", "head": "T1", "material": "PLA", "nozzle_mm": 0.4,
        "readings": readings(("sliding", "round", "step=3")),
    }
    base.update(kw)
    return store.NewRecord(**base)


def resolved(**kw: Any) -> Any:
    return fits_sources.resolve("sliding", "round", "k1se", "PLA", 0.4, **kw)


def correction(res: Any) -> dict[str, Any]:
    return next(p for p in res.provenance if p["kind"] == "correction")


# --------------------------------------------------------------------------- offline buffer
def test_offline_a_record_lands_in_the_settings_history_buffer_and_fits_resolve_reads_it(tmp_path: Path) -> None:
    assert correction(resolved())["status"] == "default, not calibrated"
    written = store.write(new_record(), None)
    assert written.origin == "buffer" and written.id.startswith("buffer:") and written.uid
    path = Path(written.where)
    assert path == tmp_path / "data" / "3d-cli" / "printer-history" / "k1se.jsonl"
    line = json.loads(path.read_text())
    assert line["schema"] == "printops.settings_change/1" and line["kind"] == "fit_clearance"
    assert line["status"] == "measured" and line["head"] == "T1" and line["nozzle"] == {"diameter_mm": 0.4}
    assert json.loads(line["new"]["readings"])[0]["fit_type"] == "sliding"

    res = resolved()
    entry = correction(res)
    assert res.clearance_mm == 0.15 and entry["status"] == "calibrated" and entry["source"] == "calibration"
    assert entry["measured_at"] == line["at"] and written.id in entry["note"] and "offline buffer" in entry["note"]
    assert entry["delta_mm"] == -0.05 and entry["fallback"] is False


def test_the_newest_record_of_a_scope_wins_and_other_materials_are_untouched() -> None:
    store.write(new_record(readings=readings(("sliding", "round", "step=3"))), None)
    store.write(new_record(readings=readings(("sliding", "round", "step=5"))), None)
    assert resolved().clearance_mm == 0.25
    store.write(new_record(material="PETG", readings=readings(("sliding", "round", "step=2"))), None)
    assert resolved().clearance_mm == 0.25  # the PETG coupon never touches the PLA numbers
    assert fits_sources.resolve("sliding", "round", "k1se", "PETG", 0.4).clearance_mm == 0.1


def test_a_record_for_one_nozzle_does_not_answer_another() -> None:
    store.write(new_record(), None)
    wide = fits_sources.resolve("sliding", "round", "k1se", "PLA", 0.6)
    assert correction(wide)["status"] == "default, not calibrated"


def test_a_press_ladder_is_resolved_as_signed_interference() -> None:
    store.write(new_record(readings=readings(("press", "round", "step=5"))), None)
    res = fits_sources.resolve("press", "round", "k1se", "PLA", 0.4)
    assert res.clearance_mm == -0.05 and correction(res)["delta_mm"] == 0.05


def test_read_all_lists_records_newest_first_and_ignores_other_kinds_and_journal_only_lines(tmp_path: Path) -> None:
    store.write(new_record(), None)
    path = tmp_path / "data" / "3d-cli" / "printer-history" / "k1se.jsonl"
    other = {"schema": "printops.settings_change/1", "uid": "other-1", "printer": "k1se", "at": "2026-10-24T10:00:00Z",
             "kind": "flow_ratio", "key": "flow", "status": "measured", "source": "t", "actor": "t", "new": {"flow_ratio": 0.9}}
    observed = {**other, "uid": "obs-1", "kind": "fit_clearance", "status": "observed", "new": {"readings": "[]"}}
    with path.open("a") as fh:
        fh.write(json.dumps(other) + "\n" + json.dumps(observed) + "\n")
    got = store.read_all().records
    assert [r.origin for r in got] == ["buffer"] and got[0].material == "PLA" and got[0].nozzle_mm == 0.4


def test_a_torn_buffer_line_stops_the_read_naming_file_and_line(tmp_path: Path) -> None:
    store.write(new_record(), None)
    path = tmp_path / "data" / "3d-cli" / "printer-history" / "k1se.jsonl"
    with path.open("a") as fh:
        fh.write('{"schema": "printops.settings_change/1", "uid": "x", "kind": "fit_cl\n')
    with pytest.raises(ThreeDError) as e:
        fits_sources.collect(fits_sources.Query())
    assert str(path) in e.value.message and "line 2" in e.value.message


def test_a_malformed_readings_value_is_an_error_not_a_silent_skip(tmp_path: Path) -> None:
    store.write(new_record(), None)
    path = tmp_path / "data" / "3d-cli" / "printer-history" / "k1se.jsonl"
    line = json.loads(path.read_text())
    line["new"]["readings"] = json.dumps([{"fit_type": "sliding", "shape": "round", "gap_mm": 0.1, "result": "stuck"}])
    path.write_text(json.dumps(line) + "\n")
    with pytest.raises(ThreeDError, match="not one of"):
        store.read_all()


def test_the_calibration_source_is_registered_next_to_manual() -> None:
    sources = fits_sources.discover()
    assert {"manual", "calibration"} <= set(sources)
    assert "fit_clearance" not in sources["calibration"].summary and "3d calibrate fit" in sources["calibration"].summary


# --------------------------------------------------------------------------- 3d-api
class Down:
    """A transport whose host is unreachable (what urllib raises as ApiError status 0)."""

    def request(self, method: str, url: str, headers: Mapping[str, str], body: bytes | None, timeout: float) -> Response:
        raise ApiError(f"3d-api at {url} is unreachable: refused")

    def stream_lines(self, *a: Any, **k: Any) -> Any:  # pragma: no cover - never streamed
        raise AssertionError


def load_spool(sm: FakeSpoolman, material: str, name: str, slot: str, vendor: str = "Elegoo") -> int:
    ensure_extra_fields(sm)
    v = sm.create("vendor", {"name": vendor})
    f = sm.create("filament", {"name": name, "vendor_id": v["id"], "material": material, "density": 1.24,
                               "diameter": 1.75, "weight": 1000})
    return int(sm.create("spool", {"filament_id": f["id"], "lot_nr": "L1", "extra": {"printops_slot": encode_extra(slot)}})["id"])


def test_api_a_record_is_a_calibration_scoped_to_head_nozzle_and_the_bound_filament(
    api_client: ApiClient, fake_spoolman: FakeSpoolman
) -> None:
    load_spool(fake_spoolman, "PLA", "PLA Grey", "k1se/T1")
    written = store.write(new_record(), api_client)
    assert written.origin == "3d-api" and written.id.startswith("#")
    rec = written.record
    assert rec["kind"] == "fit_clearance" and rec["status"] == "recorded" and rec["method"] == "fit_coupon"
    assert (rec["printer"], rec["head"], rec["filament"]["material"]) == ("k1se", "T1", "PLA")
    assert rec["nozzle"]["diameter_mm"] == 0.4
    assert json.loads(rec["values"]["readings"])[2] == {"fit_type": "sliding", "gap_mm": 0.15, "result": "free", "shape": "round"}

    read = store.read_all(api_client).records
    assert [(r.id, r.origin, r.material, r.nozzle_mm) for r in read] == [(written.id, "3d-api", "PLA", 0.4)]
    (row,) = store.rows_of(read[0])
    assert (row.fit_type, row.shape, row.delta_mm, row.source, row.printer) == ("sliding", "round", -0.05, "calibration", "k1se")
    assert row.measured_at == rec["recorded_at"] and written.id in row.note

    again = store.write(new_record(readings=readings(("sliding", "round", "step=5"))), api_client)
    current = store.read_all(api_client).records
    assert [r.id for r in current] == [again.id]  # the newer record superseded the older one


def test_api_refuses_a_material_that_is_not_the_loaded_one_before_storing(
    api_client: ApiClient, fake_spoolman: FakeSpoolman
) -> None:
    load_spool(fake_spoolman, "PETG", "PETG Black", "k1se/T1")
    from errors import UsageError

    with pytest.raises(UsageError, match="PETG loaded"):
        store.write(new_record(material="PLA"), api_client)
    assert store.read_all(api_client).records == []


def test_api_without_a_bound_spool_says_what_to_pass(api_client: ApiClient, fake_spoolman: FakeSpoolman) -> None:
    ensure_extra_fields(fake_spoolman)
    with pytest.raises(ThreeDError) as e:
        store.write(new_record(), api_client)
    assert "refused the record" in e.value.message and any("--spool" in r for r in e.value.remediation)


def test_an_unreachable_api_falls_back_to_the_buffer_and_the_read_says_so(capsys: pytest.CaptureFixture[str]) -> None:
    down = ApiClient("http://api.invalid", "t", Down())
    written = store.write(new_record(), down)
    assert written.origin == "buffer"
    read = store.read_all(down)
    assert [r.origin for r in read.records] == ["buffer"] and "unreachable" in read.notes[0]
    assert "fit calibrations come from the offline buffer only" in capsys.readouterr().err


def test_an_api_error_other_than_unreachable_is_never_hidden_by_the_buffer(api_client: ApiClient) -> None:
    api_client.token = "wrong"
    with pytest.raises(ThreeDError, match="refused"):
        store.read_all(api_client)


def test_the_buffer_line_is_not_double_counted_after_3d_settings_import(
    api_client: ApiClient, fake_spoolman: FakeSpoolman, tmp_path: Path
) -> None:
    spool_id = load_spool(fake_spoolman, "PLA", "PLA Grey", "k1se/T1")
    written = store.write(new_record(), None)
    assert written.uid is not None
    lines = Path(written.where).read_text().splitlines()
    before = store.read_all(api_client).records
    assert [r.origin for r in before] == ["buffer"]
    report = api_client.post("/v1/settings/import", {"lines": lines, "spools": {written.uid: spool_id}})
    assert report["counts"]["imported"] == 1 and report["counts"]["calibrations"] == 1, report
    after = store.read_all(api_client).records
    assert [(r.origin, r.uid) for r in after] == [("3d-api", written.uid)]  # the API copy replaces the buffer line
    (row,) = store.rows_of(after[0])
    assert row.delta_mm == -0.05 and row.measured_at is not None and row.measured_at.startswith("2026-")


def test_the_api_switch_off_means_no_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRINTOPS_API", "http://127.0.0.1:9")
    assert store.api_client() is None  # the suite default is `off`
    monkeypatch.setenv(store.ENV_API, "on")
    client = store.api_client()
    assert client is not None and client.timeout == store.READ_TIMEOUT_S
