"""E2E: a user fits a tester readout and records the measured colours: bin/3d → real uvicorn 3d-api (#417)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fleet_fixtures import REPO_FLEET
from mix_fit_fixtures import TRUTH, simulated_readout
from printops_fixtures import LiveApi
from test_printops_settings_history import add_spool

from printops.color import hex_delta_e

from .workflow_helper import run_cli


@pytest.fixture(autouse=True)
def repo_files(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("THREED_FLEET", str(REPO_FLEET))
    monkeypatch.delenv("PRINTOPS_POLICY", raising=False)
    monkeypatch.delenv("PRINTOPS_CALIBRATIONS", raising=False)


def _env(api: LiveApi) -> dict[str, str]:
    return {"PRINTOPS_API": api.url, "PRINTOPS_TOKEN": api.tokens["rw"]}


def _load_u1(api: LiveApi) -> tuple[int, int, int]:
    """Black, green and white PLA loaded on heads T1, T3, T4 of the U1; their Spoolman filament ids."""
    ids = []
    for head, name, color in (
        ("T1", "black", "000000"),
        ("T3", "green", "1B5E20"),
        ("T4", "white", "FFFFFF"),
    ):
        spool = add_spool(api.spoolman, f"PLA {name}", color, slot=f"u1/{head}")
        ids.append(int(api.spoolman.get("spool", spool)["filament"]["id"]))
    return ids[0], ids[1], ids[2]


def _readout(tmp_path: Path, api: LiveApi) -> Path:
    ids = _load_u1(api)
    path = tmp_path / "wall.readout.json"
    path.write_text(
        json.dumps(simulated_readout(tmp_path / "tester", filament_ids=ids))
    )
    return path


def _calibrations(tmp_path: Path, api: LiveApi, kind: str) -> list[dict[str, Any]]:
    run = run_cli(
        tmp_path,
        "calibrate",
        "ls",
        "--kind",
        kind,
        "--all",
        "--json",
        env_extra=_env(api),
    )
    assert run.returncode == 0, run.stderr
    rows: list[dict[str, Any]] = json.loads(run.stdout)["calibrations"]
    return rows


def test_user_fits_a_readout_and_the_colours_land_in_3d_api_once(
    tmp_path: Path, live_api: LiveApi
) -> None:
    """The user read a tester of black, green and white PLA. `mix fit` says how far the nominal colours are
    off and writes the fit; `mix record` without --yes only plans; with --yes it writes the filament colours
    and then the mixes to 3d-api, where `calibrate ls` shows them; the same readout again adds nothing."""
    readout = _readout(tmp_path, live_api)
    env = _env(live_api)

    fit = run_cli(tmp_path, "mix", "fit", "wall.readout.json", "-o", "out/wall")
    assert fit.returncode == 0, fit.stdout + fit.stderr
    rec = json.loads((tmp_path / "out" / "wall.fit.json").read_text())
    assert rec["schema"] == "mix-fit/1" and rec["summary"]["improved"] is True
    assert (
        fit.stdout.splitlines()[0]
        == "mix fit of 1 readout(s): 3 filaments, 4 mixes (lambda 0.1)"
    )
    sm = rec["summary"]
    assert (
        f"mean mix error: nominal {sm['mean_delta_e_nominal']:.2f} > pure {sm['mean_delta_e_pure']:.2f} "
        f"> fitted {sm['mean_delta_e_fitted']:.2f}"
    ) in fit.stdout
    for f in rec["filaments"]:
        assert hex_delta_e(f["fitted_hex"], TRUTH[f["slot"]]) < 1.5
        assert (
            f"{f['nominal_hex']:<9} {f['pure_hex']:<9} {f['fitted_hex']:<9}"
            in fit.stdout
        )

    plan = run_cli(
        tmp_path, "mix", "record", "wall.readout.json", "--printer", "u1", env_extra=env
    )
    assert plan.returncode == 0, plan.stderr
    assert (
        "would record filament_color" in plan.stdout
        and "add --yes to write these records" in plan.stdout
    )
    assert _calibrations(tmp_path, live_api, "filament_color") == []

    args = (
        "mix",
        "record",
        "wall.readout.json",
        "--printer",
        "u1",
        "--fit",
        "out/wall.fit.json",
        "--yes",
        "--json",
    )
    first = run_cli(tmp_path, *args, env_extra=env)
    assert first.returncode == 0, first.stdout + first.stderr
    summary = json.loads(first.stdout)
    assert (
        summary["mode"] == "api"
        and [r["status"] for r in summary["records"]] == ["recorded"] * 7
    )
    assert [r["kind"] for r in summary["records"]] == ["filament_color"] * 3 + [
        "mix_color"
    ] * 4
    fc = _calibrations(tmp_path, live_api, "filament_color")
    mc = _calibrations(tmp_path, live_api, "mix_color")
    assert len(fc) == 3 and len(mc) == 4
    fitted = {f["filament_id"]: f["fitted_hex"] for f in rec["filaments"]}
    for row in fc:
        assert row["values"]["hex"] == fitted[row["filament"]["filament_id"]]
        assert row["params"]["readout_sha256"] == summary["sha256"]
        assert row["evidence"][1].startswith("readout wall.readout.json sha256:")
    assert all(r["printer"] == "u1" and r["method"] == "mix_tester_photo" for r in mc)
    assert readout.is_file()

    again = run_cli(tmp_path, *args, env_extra=env)
    assert again.returncode == 0, again.stderr
    assert [r["status"] for r in json.loads(again.stdout)["records"]] == ["exists"] * 7
    assert len(_calibrations(tmp_path, live_api, "filament_color")) == 3
    assert len(_calibrations(tmp_path, live_api, "mix_color")) == 4
    listed = run_cli(tmp_path, "calibrate", "ls", "--kind", "mix_color", env_extra=env)
    assert listed.stdout.count("mix h0.12:") == 4


def test_offline_record_writes_the_buffer_once_and_the_import_brings_it_in(
    tmp_path: Path, live_api: LiveApi
) -> None:
    """No 3d-api at the printer: `mix record --offline --yes` appends to the settings-history buffer (a repeat
    adds nothing), and later `settings import` makes the same records, once; an unreachable API falls back
    to the buffer by itself."""
    _readout(tmp_path, live_api)
    env = _env(live_api)
    buffer = tmp_path / "xdg-data" / "3d-cli" / "printer-history" / "u1.jsonl"
    args = ("mix", "record", "wall.readout.json", "--printer", "u1", "--yes")

    first = run_cli(tmp_path, *args, "--offline", env_extra=env)
    assert first.returncode == 0, first.stdout + first.stderr
    assert (
        first.stdout.count("  buffered ") == 7
        and "next: `3d settings import`" in first.stdout
    )
    lines = buffer.read_text().splitlines()
    assert len(lines) == 7 and {json.loads(x)["kind"] for x in lines} == {
        "filament_color",
        "mix_color",
    }
    assert (
        _calibrations(tmp_path, live_api, "mix_color") == []
    )  # nothing reached 3d-api yet

    again = run_cli(tmp_path, *args, "--offline", env_extra=env)
    assert again.returncode == 0 and again.stdout.count("already buffered") == 7
    assert buffer.read_text().splitlines() == lines

    down = run_cli(
        tmp_path, *args, env_extra={**env, "PRINTOPS_API": "http://127.0.0.1:9"}
    )
    assert down.returncode == 0, down.stdout + down.stderr
    assert (
        "3d-api unreachable" in down.stdout
        and down.stdout.count("already buffered") == 7
    )

    imported = run_cli(tmp_path, "settings", "import", env_extra=env)
    assert imported.returncode == 0, imported.stdout + imported.stderr
    assert "7 imported (7 calibration records), 0 skipped, 0 refused" in imported.stdout
    assert len(_calibrations(tmp_path, live_api, "filament_color")) == 3
    mixes = _calibrations(tmp_path, live_api, "mix_color")
    assert len(mixes) == 4 and all(
        r["mix"].startswith("h0.12:f") and r["printer"] == "u1" for r in mixes
    )
    repeat = run_cli(tmp_path, "settings", "import", env_extra=env)
    assert "0 imported" in repeat.stdout and "7 skipped" in repeat.stdout

    # the API path now finds nothing to add for the same readout: the buffer's records are the readout's
    online = run_cli(tmp_path, *args, "--json", env_extra=env)
    assert [r["status"] for r in json.loads(online.stdout)["records"]] == ["exists"] * 7


def _assert_fit_colours_offline(tmp_path: Path) -> None:
    """`mix recipes --fit` marks every slot of the fit as measured, nominal colour kept beside it."""
    offline = run_cli(
        tmp_path, "mix", "recipes", "--target", "#808080", "--slots", "1=#000000,3=#1B5E20,4=#FFFFFF",
        "--fit", "out/wall.fit.json", "--json",
    )  # fmt: skip
    assert offline.returncode == 0, offline.stderr
    fitted = {s["slot"]: s for s in json.loads(offline.stdout)["slots"]}
    assert all(fitted[n]["measured"] is True for n in (1, 3, 4))
    assert fitted[1]["nominal_hex"] == "#000000" and fitted[1]["hex"] != "#000000"


def test_recipes_and_tester_show_and_use_the_measured_colours(
    tmp_path: Path, live_api: LiveApi
) -> None:
    """After recording, `mix recipes --printer u1` marks the measured filament colours (nominal kept beside
    them) and the mixes that have a record show their measured colour; --fit does the same offline."""
    _readout(tmp_path, live_api)
    env = _env(live_api)
    fit = run_cli(tmp_path, "mix", "fit", "wall.readout.json", "-o", "out/wall")
    assert fit.returncode == 0, fit.stderr
    record = run_cli(
        tmp_path,
        "mix",
        "record",
        "wall.readout.json",
        "--printer",
        "u1",
        "--yes",
        env_extra=env,
    )
    assert record.returncode == 0, record.stdout + record.stderr

    args = (
        "mix",
        "recipes",
        "--target",
        "#808080",
        "--printer",
        "u1",
        "--count",
        "20",
        "--json",
    )
    run = run_cli(tmp_path, *args, env_extra=env)
    assert run.returncode == 0, run.stdout + run.stderr
    data = json.loads(run.stdout)
    slots = {s["slot"]: s for s in data["slots"]}
    assert (
        slots[4]["measured"] is True
        and slots[4]["nominal_hex"] == "#FFFFFF"
        and slots[4]["hex"] == "#EFEBE1"
    )
    assert "measured" not in {
        k for s in data["slots"] for k in s if not s.get("nominal_hex")
    }
    measured = [c for c in data["candidates"] if c.get("measured")]
    assert measured, data["candidates"]
    stored = {
        r["mix"]: r["values"]["hex"]
        for r in _calibrations(tmp_path, live_api, "mix_color")
    }
    assert {c["hex"] for c in measured} <= set(stored.values())
    text = run_cli(tmp_path, *args[:-1], env_extra=env)
    assert (
        "T4 #EFEBE1 measured (nominal #FFFFFF)" in text.stdout
        and "  measured" in text.stdout
    )

    _assert_fit_colours_offline(tmp_path)


def test_mix_record_and_fit_argument_errors(tmp_path: Path, live_api: LiveApi) -> None:
    """A user gets the record or fit arguments wrong: each mistake is refused naming what to pass."""
    _readout(tmp_path, live_api)
    env = _env(live_api)
    (tmp_path / "junk.json").write_text("{nope")
    (tmp_path / "other.json").write_text(json.dumps({"schema": "mix-tester/9"}))
    badnum = {
        "schema": "mix-readout/1",
        "swatches": [
            {
                "kind": "candidate",
                "number": 1,
                "calibration": {"mix": "h0.12:f1,f2"},
                "measured_hex": "#808080",
                "predicted_hex": "#777777",
                "delta_e_predicted": "oops",
            }
        ],
    }
    (tmp_path / "badnum.json").write_text(json.dumps(badnum))
    (tmp_path / "torn.json").write_text(json.dumps({"schema": "mix-readout/1", "swatches": [None]}))
    cases = [
        (("mix", "fit", "torn.json"), "malformed readout"),
        (("mix", "record", "torn.json", "--printer", "u1"), "malformed readout"),
        (("mix", "record", "badnum.json", "--printer", "u1"), "malformed readout"),
        (("mix", "record"), "record takes the readout JSON first"),
        (("mix", "record", "wall.readout.json"), "record needs --printer NAME"),
        (("mix", "record", "gone.json", "--printer", "u1"), "gone.json"),
        (("mix", "record", "junk.json", "--printer", "u1"), "not JSON"),
        (
            ("mix", "record", "other.json", "--printer", "u1"),
            "schema must be mix-readout/1",
        ),
        (
            (
                "mix",
                "record",
                "wall.readout.json",
                "--printer",
                "u1",
                "--fit",
                "junk.json",
            ),
            "not JSON",
        ),
        (("mix", "fit"), "fit takes the readout JSON first"),
        (("mix", "fit", "gone.json"), "gone.json"),
        (("mix", "fit", "other.json"), "schema must be mix-readout/1"),
        (("mix", "fit", "wall.readout.json", "--lambda", "-1"), "--lambda"),
        (
            ("mix", "fit", "wall.readout.json", "-o", "out/x.json"),
            "without an extension",
        ),
        (
            (
                "mix",
                "recipes",
                "--target",
                "#808080",
                "--slots",
                "1=#000000,4=#FFFFFF",
                "--yes",
            ),
            "unknown option --yes",
        ),
    ]
    for argv, fragment in cases:
        run = run_cli(tmp_path, *argv, env_extra=env)
        assert run.returncode == 2, (argv, run.stdout, run.stderr)
        assert fragment in run.stderr, (argv, run.stderr)
