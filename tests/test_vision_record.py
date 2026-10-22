"""`3d printer vision record` (lib/vision_detect.py run_record) against the loopback Moonraker
fake: it saves frames while the printer prints OR is paused, says why it saves nothing otherwise
and, with --timeout, fails instead of waiting forever. Regression: on 2026-10-22 `record u1 OUT
--max 1` against a PAUSED U1 left an empty meta.jsonl and ran until the caller's 60 s timeout,
because only the `printing` state recorded and nothing was printed about it."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

pytest.importorskip("numpy")
pytest.importorskip("cv2")

import cv2  # noqa: E402
import numpy as np  # noqa: E402
from printer_fakes import FakeMoonraker, u1_status, write_devices  # noqa: E402

import vision_detect  # noqa: E402
from errors import GateFailure  # noqa: E402

OBJECT = [[10.0, 10.0], [60.0, 10.0], [60.0, 60.0], [10.0, 60.0]]


def _status(state: str) -> dict[str, object]:
    status = u1_status(state, "part.gcode")
    status["gcode_move"] = {"gcode_position": [30.0, 30.0, 5.3, 0.0]}
    status["toolhead"] = {"position": [30.0, 30.0, 10.4, 0.0]}
    status["exclude_object"] = {"objects": [{"name": "p", "polygon": OBJECT}]}
    return status


@pytest.fixture()
def moon(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeMoonraker]:
    cfg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(cfg))
    monkeypatch.chdir(tmp_path)
    fake = FakeMoonraker()
    ok, jpg = cv2.imencode(".jpg", np.full((36, 64, 3), 90, np.uint8))
    assert ok
    fake.camera = jpg.tobytes()
    with fake.serve():
        write_devices(cfg, moon=fake)
        yield fake


def _record(out: Path, *extra: str) -> int:
    return vision_detect.main(
        ["record", "u1", str(out), "--no-keepalive", "--interval", "0.5", *extra]
    )


@pytest.mark.parametrize("state", ["printing", "paused"])
def test_it_saves_a_frame_with_context_while_printing_or_paused(
    moon: FakeMoonraker, tmp_path: Path, state: str
) -> None:
    moon.status = _status(state)
    out = tmp_path / "rec"
    assert _record(out, "--max", "1", "--timeout", "10") == 0
    rows = [json.loads(line) for line in (out / "meta.jsonl").read_text().splitlines()]
    assert len(rows) == 1
    assert rows[0]["state"] == state
    assert rows[0]["z"] == 5.3
    assert rows[0]["objects"] == [[tuple(p) for p in OBJECT]] or rows[0]["objects"] == [OBJECT]
    assert (out / rows[0]["frame"]).stat().st_size > 0


def test_an_idle_printer_fails_fast_with_a_reason_instead_of_hanging(
    moon: FakeMoonraker, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    moon.status = _status("standby")
    out = tmp_path / "rec"
    with pytest.raises(GateFailure) as info:
        _record(out, "--max", "1", "--timeout", "1")
    assert "idle" in info.value.message
    assert (out / "meta.jsonl").read_text() == ""
    assert '"state": "idle"' in capsys.readouterr().err
