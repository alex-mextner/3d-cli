"""`3d printer bed reference NAME --refresh [--assume-empty WHY]`, `watch.bed.auto_reference` and the age of a
reference, end to end through bin/3d against a loopback fake Moonraker with a chamber light that serves real
U1 camera frames (tests/data/bed). The printer's bed ROI is a hand-drawn polygon (no camera calibration), so
the camera sees the whole bed by definition and nothing moves."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from .test_printer_bed_workflows import (
    HAND_ROI,
    LIGHT_OFF,
    LIGHT_ON,
    jpeg,
    u1_with_light,
    write_u1,
)
from .workflow_helper import run_cli

pytest.importorskip("numpy")
pytest.importorskip("cv2")

BED = f"{{enabled: true, interval_s: 0.2, roi: {HAND_ROI}, light: {{settle_s: 0}}"


def refs(tmp_path: Path) -> list[Path]:
    return sorted(tmp_path.glob("xdg-data/**/bed/u1/*.json"))


def journal(tmp_path: Path) -> list[dict[str, object]]:
    path = tmp_path / "xdg-state" / "3d-cli" / "bed" / "u1.refs.log"
    return (
        [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]
        if path.is_file()
        else []
    )


def test_refresh_replaces_the_reference_after_a_confident_clear_with_the_light_on(
    tmp_path: Path,
) -> None:
    moon = u1_with_light()
    with moon.serve():
        write_u1(tmp_path, moon, bed=BED + "}")
        assert (
            run_cli(
                tmp_path, "printer", "bed", "reference", "u1", "--capture"
            ).returncode
            == 0
        )
        moon.camera = jpeg("u1_z200_clear_1426.jpg")
        moon.scripts.clear()
        out = run_cli(
            tmp_path, "printer", "bed", "reference", "u1", "--refresh", "--json"
        )
        listed = run_cli(tmp_path, "printer", "bed", "reference", "u1")
        lit_after = moon.lit

    assert out.returncode == 0, out.stdout + out.stderr
    res = json.loads(out.stdout)
    assert res["refreshed"] is True and res["new_reference"]["light"] == "on"
    assert res["new_reference"]["plate"] == "default" and res["bed"] == "clear"
    assert (
        moon.scripts == [LIGHT_ON, LIGHT_OFF] and lit_after is False
    )  # the light is back as found
    assert len(refs(tmp_path)) == 1  # the capture was lit too: replaced, not added
    assert "plate default, light on" in listed.stdout
    [line] = journal(tmp_path)
    assert line["mode"] == "manual" and line["light"] == "on"


def test_refresh_refuses_parts_on_the_bed_and_stores_nothing_but_the_owners_word_does(
    tmp_path: Path,
) -> None:
    moon = u1_with_light()
    with moon.serve():
        write_u1(tmp_path, moon, bed=BED + "}")
        assert (
            run_cli(
                tmp_path, "printer", "bed", "reference", "u1", "--capture"
            ).returncode
            == 0
        )
        before = [p.read_text(encoding="utf-8") for p in refs(tmp_path)]
        moon.camera = jpeg("u1_occupied_1351.jpg")
        refused = run_cli(tmp_path, "printer", "bed", "reference", "u1", "--refresh")
        unchanged = [p.read_text(encoding="utf-8") for p in refs(tmp_path)]
        empty_why = run_cli(
            tmp_path,
            "printer",
            "bed",
            "reference",
            "u1",
            "--refresh",
            "--assume-empty",
            "  ",
        )
        assumed = run_cli(
            tmp_path,
            "printer",
            "bed",
            "reference",
            "u1",
            "--refresh",
            "--assume-empty",
            "plate wiped, I looked",
        )

    assert refused.returncode == 1
    assert "NOT refreshed" in refused.stdout and "nothing was stored" in refused.stdout
    assert unchanged == before and journal(tmp_path)[0]["mode"] == "assumed"
    assert empty_why.returncode == 2 and "needs the reason" in empty_why.stderr
    assert assumed.returncode == 0, assumed.stdout + assumed.stderr
    assert "the owner says it is empty: plate wiped, I looked" in assumed.stdout
    assert (
        json.loads(refs(tmp_path)[0].read_text(encoding="utf-8"))["assumed_empty"]
        == "plate wiped, I looked"
    )


def test_a_stale_reference_turns_parts_on_the_bed_into_unknown_with_the_refresh_command(
    tmp_path: Path,
) -> None:
    moon = u1_with_light()
    with moon.serve():
        write_u1(tmp_path, moon, bed=BED + "}")
        assert (
            run_cli(
                tmp_path, "printer", "bed", "reference", "u1", "--capture"
            ).returncode
            == 0
        )
        moon.camera = jpeg("u1_occupied_1351.jpg")
        fresh = json.loads(
            run_cli(tmp_path, "printer", "bed", "status", "u1", "--json").stdout
        )
        [meta] = refs(tmp_path)
        data = json.loads(meta.read_text(encoding="utf-8"))
        data["ts"] -= 45 * 86400
        meta.write_text(json.dumps(data), encoding="utf-8")
        stale = json.loads(
            run_cli(tmp_path, "printer", "bed", "status", "u1", "--json").stdout
        )
        listed = run_cli(tmp_path, "printer", "bed", "reference", "u1")

    assert fresh["bed"] == "occupied" and fresh["reference"]["stale"] is False
    assert stale["bed"] == "unknown" and stale["occupied_confident"] is False
    assert (
        stale["reference"]["age_days"] == pytest.approx(45.0, abs=0.1)
        and stale["reference"]["stale"] is True
    )
    assert "3d printer bed reference u1 --refresh" in stale["reason"]
    assert "STALE" in listed.stdout


def test_auto_reference_refreshes_after_a_confident_clear_status_and_is_off_by_default(
    tmp_path: Path,
) -> None:
    moon = u1_with_light()
    with moon.serve():
        write_u1(tmp_path, moon, bed=BED + "}")
        assert (
            run_cli(
                tmp_path, "printer", "bed", "reference", "u1", "--capture"
            ).returncode
            == 0
        )
        moon.camera = jpeg("u1_z200_clear_1412.jpg")
        off = json.loads(
            run_cli(tmp_path, "printer", "bed", "status", "u1", "--json").stdout
        )
        write_u1(tmp_path, moon, bed=BED + ", auto_reference: true}")
        on = json.loads(
            run_cli(tmp_path, "printer", "bed", "status", "u1", "--json").stdout
        )

    assert "auto_reference" not in off, off
    assert on["bed"] == "clear", on
    assert on["auto_reference"]["refreshed"] is True, on
    [line] = journal(tmp_path)
    assert line["mode"] == "auto" and line["light"] == "on"


def test_refresh_flags_are_validated(tmp_path: Path) -> None:
    moon = u1_with_light()
    with moon.serve():
        write_u1(tmp_path, moon, bed=BED + "}")
        both = run_cli(
            tmp_path, "printer", "bed", "reference", "u1", "--refresh", "--capture"
        )
        stray = run_cli(tmp_path, "printer", "bed", "status", "u1", "--refresh")
        listing = run_cli(
            tmp_path, "printer", "bed", "reference", "u1", "--assume-empty", "x"
        )
        helped = run_cli(tmp_path, "printer", "bed", "--help")

    assert both.returncode == 2 and "contradict" in both.stderr
    assert stray.returncode == 2 and "does not take --refresh" in stray.stderr
    assert listing.returncode == 2 and "does not take --assume-empty" in listing.stderr
    assert (
        "--refresh [--assume-empty" in helped.stdout
        and "reference_max_age_days" in helped.stdout
    )
