"""printer_ops.vision_status + GET /v1/printers/{name}/vision: the camera verdict Home Assistant shows."""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fleet_fixtures import REPO_FLEET

from printer_ops.vision_status import SCHEMA, VisionStatus, read_status


def event(
    kind: str, severity: str = "warning", text: str = "t", ts: float = 100.0
) -> Any:
    return SimpleNamespace(kind=kind, severity=severity, text=text, ts=ts)


def score(
    p: float = 0.4,
    q: float | None = 0.2,
    luma: float = 120.0,
    kinds: tuple[str, ...] = (),
) -> Any:
    return SimpleNamespace(
        p=p, q=q, luma=luma, findings=[SimpleNamespace(kind=k) for k in kinds]
    )


def test_one_cycle_writes_the_verdict_and_the_counters_accumulate(
    tmp_path: Path,
) -> None:
    path = tmp_path / "vision" / "k1se.json"
    status = VisionStatus("k1se", path, started=50.0)

    first = status.update(
        state="printing",
        filename="a.gcode",
        score=score(),
        events=[],
        trend=0.12345,
        camera_error=None,
        now=60.0,
    )
    second = status.update(
        state="printing",
        filename="a.gcode",
        score=score(0.9, 0.8, kinds=("part_moved", "part_moved")),
        events=[event("vision_spaghetti"), event("vision_camera_dark", "info")],
        trend=0.5,
        camera_error=None,
        now=70.0,
    )

    assert (
        first["frames"] == 1
        and first["events"] == {"total": 0, "by_kind": {}}
        and first["trend"] == 0.1235
    )
    assert second["frames"] == 2 and second["findings"] == ["part_moved"]
    assert second["events"] == {
        "total": 2,
        "by_kind": {"vision_camera_dark": 1, "vision_spaghetti": 1},
    }
    assert second["last_event"] == {
        "kind": "vision_camera_dark",
        "severity": "info",
        "text": "t",
        "ts": 100.0,
    }
    assert read_status("k1se", path) == second and second["schema"] == SCHEMA
    assert [p.name for p in path.parent.iterdir()] == [
        "k1se.json"
    ]  # the temp file is gone


def test_a_cycle_without_a_frame_keeps_the_state_and_blanks_the_verdict(
    tmp_path: Path,
) -> None:
    status = VisionStatus("u1", tmp_path / "u1.json")
    doc = status.update(
        state="paused",
        filename=None,
        score=None,
        events=[],
        trend=None,
        camera_error="no frame",
        now=1.0,
    )

    assert (doc["state"], doc["p"], doc["q"], doc["frames"], doc["camera_error"]) == (
        "paused",
        None,
        None,
        0,
        "no frame",
    )


def test_a_non_finite_number_never_reaches_the_json(tmp_path: Path) -> None:
    status = VisionStatus("u1", tmp_path / "u1.json")
    doc = status.update(
        state="printing",
        filename="f",
        score=score(float("nan"), float("inf"), 1.0),
        events=[],
        trend=float("nan"),
        camera_error=None,
        now=1.0,
    )

    assert (doc["p"], doc["q"], doc["trend"]) == (None, None, None)
    json.loads((tmp_path / "u1.json").read_text(encoding="utf-8"))  # strict JSON


def test_an_unwritable_status_path_does_not_stop_the_loop(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    status = VisionStatus("u1", blocker / "u1.json")

    status.update(
        state="printing",
        filename="f",
        score=score(),
        events=[],
        trend=0.0,
        camera_error=None,
        now=1.0,
    )

    assert "vision status" in capsys.readouterr().err


def test_a_missing_or_foreign_file_reads_as_none(tmp_path: Path) -> None:
    assert read_status("u1", tmp_path / "none.json") is None
    other = tmp_path / "other.json"
    other.write_text(json.dumps({"schema": 99}), encoding="utf-8")
    assert read_status("u1", other) is None
    other.write_text("{not json", encoding="utf-8")
    assert read_status("u1", other) is None


def test_the_api_serves_the_status_with_its_age_and_404s_unknown_or_never_run(
    printops_client: Any,
    printops_tokens: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("THREED_FLEET", str(REPO_FLEET))
    auth = {"authorization": f"Bearer {printops_tokens['read']}"}

    never = printops_client.get("/v1/printers/k1se/vision", headers=auth)
    VisionStatus("k1se", started=1.0).update(
        state="printing",
        filename="a.gcode",
        score=score(),
        events=[event("vision_spaghetti")],
        trend=0.3,
        camera_error=None,
        now=time.time() - 30,
    )
    got = printops_client.get("/v1/printers/k1se/vision", headers=auth)
    unknown = printops_client.get("/v1/printers/nope/vision", headers=auth)
    anonymous = printops_client.get("/v1/printers/k1se/vision")

    assert never.status_code == 404 and "has not run" in never.json()["detail"]
    body = got.json()
    assert (
        got.status_code == 200
        and body["printer"] == "k1se"
        and body["state"] == "printing"
        and body["events"]["total"] == 1
    )
    assert 29.0 <= body["age_s"] <= 40.0
    assert unknown.status_code == 404 and "unknown printer" in unknown.json()["detail"]
    assert anonymous.status_code == 401


LIVENESS_KEYS = (
    "last_poll_ok",
    "unreachable_since",
    "reachable",
    "last_frame_ts",
    "supervising",
    "job",
    "interval_s",
)


def test_the_liveness_keys_are_written_with_what_the_cycle_passes(
    tmp_path: Path,
) -> None:
    path = tmp_path / "vision" / "k1se.json"
    status = VisionStatus("k1se", path, started=50.0)

    doc = status.update(
        state="printing",
        filename="a.gcode",
        score=score(),
        events=[],
        trend=0.1,
        camera_error=None,
        now=200.0,
        last_poll_ok=199.9,
        unreachable_since=None,
        reachable=True,
        last_frame_ts=199.5,
        supervising=True,
        job={"file": "a.gcode", "started": 120.0},
        interval_s=10.0,
    )

    assert (doc["last_poll_ok"], doc["unreachable_since"], doc["reachable"]) == (
        199.9,
        None,
        True,
    )
    assert (doc["last_frame_ts"], doc["supervising"], doc["interval_s"]) == (
        199.5,
        True,
        10.0,
    )
    assert (
        doc["job"] == {"file": "a.gcode", "started": 120.0}
        and doc["schema"] == SCHEMA == 1
    )
    assert read_status("k1se", path) == doc


def test_an_unreachable_printer_still_gets_a_complete_document(tmp_path: Path) -> None:
    status = VisionStatus("k1se", tmp_path / "k1se.json", started=50.0)

    doc = status.update(
        state=None,
        filename=None,
        score=None,
        events=[],
        trend=0.0,
        camera_error=None,
        now=300.0,
        last_poll_ok=250.0,
        unreachable_since=260.0,
        reachable=False,
        last_frame_ts=249.0,
        supervising=False,
        job=None,
        interval_s=10.0,
    )

    assert (doc["reachable"], doc["unreachable_since"], doc["last_poll_ok"]) == (
        False,
        260.0,
        250.0,
    )
    assert (doc["supervising"], doc["job"], doc["state"]) == (False, None, None)
    assert (
        doc["ts"] == 300.0
    )  # written this cycle: the file's age says the loop is alive


def test_old_style_calls_get_neutral_liveness_defaults_and_bad_numbers_never_reach_the_json(
    tmp_path: Path,
) -> None:
    status = VisionStatus("k1se", tmp_path / "k1se.json")

    plain = status.update(
        state="printing",
        filename="f",
        score=score(),
        events=[],
        trend=0.0,
        camera_error=None,
        now=1.0,
    )
    assert all(key in plain for key in LIVENESS_KEYS)
    assert (
        plain["last_poll_ok"],
        plain["last_frame_ts"],
        plain["job"],
        plain["supervising"],
    ) == (None, None, None, False)

    odd = status.update(
        state="printing",
        filename="f",
        score=None,
        events=[],
        trend=0.0,
        camera_error=None,
        now=2.0,
        last_poll_ok=float("nan"),
        last_frame_ts=float("inf"),
        job={"file": "f", "started": float("nan")},
        interval_s=float("nan"),
    )
    assert (odd["last_poll_ok"], odd["last_frame_ts"], odd["interval_s"]) == (
        None,
        None,
        None,
    )
    assert odd["job"] == {"file": "f", "started": None}
    json.loads((tmp_path / "k1se.json").read_text(encoding="utf-8"))  # strict JSON
