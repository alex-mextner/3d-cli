"""Native pixel failures must blind one frame without concealing controller bugs."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from test_printer_guard import JOB, Clock, ScriptedPrinter, score

from printer_ops.vision import Frame, FrameScore, jpeg_frame

# Valid marker framing does not guarantee a decodable image (no SOF/table data here).
UNDECODABLE = (b"\xff\xd8\xff\xe0\x00\x10" + b"\x00" * 14
               + b"\xff\xda\x00\x08" + b"\x00" * 6 + b"\x12\x34" * 8 + b"\xff\xd9")


@pytest.fixture
def runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    cv2 = pytest.importorskip("cv2")
    native = pytest.importorskip("onnxruntime.capi.onnxruntime_pybind11_state")
    import printer_ops
    import vision_detect
    from printer_ops import devices

    printer, clock = ScriptedPrinter(), Clock()
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(devices, "get_device", lambda _name: SimpleNamespace(
        name="u1", adapter="moonraker", access="full", watch={}, host="127.0.0.1", port=1))
    monkeypatch.setattr(printer_ops, "make_adapter", lambda _device: printer)
    monkeypatch.setattr(vision_detect.time, "time", clock)
    monkeypatch.setattr(vision_detect.time, "sleep", clock.sleep)
    monkeypatch.setattr(vision_detect, "_calibration", lambda _device, _args: None)

    class Camera:
        def keepalive(self, _now: float) -> bool:
            return False

        def grab(self) -> Frame:
            return Frame(clock.now, f"frame-{clock.now}".encode(), clock.now - 1)

    monkeypatch.setattr(vision_detect, "_device_camera", lambda _device, _args: Camera())
    return SimpleNamespace(vision=vision_detect, printer=printer, clock=clock, cv2=cv2, native=native)


@pytest.mark.parametrize("failure", ["decode", "opencv", "Fail", "RuntimeException", "EngineError", "EPFail"])
def test_scoring_failure_resets_evidence_and_fresh_frames_can_recover(
    runtime: Any, failure: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    class Scorer:
        geometry = None
        verifier = object()
        calls = 0

        def __call__(self, _jpeg: bytes) -> FrameScore:
            self.calls += 1
            assert self.calls < 130, "guard never recovered after a pixel failure"
            if self.calls <= 80:
                return FrameScore(0.0, q=0.0)
            if self.calls == 81:
                return score(q=8, p=8)  # exactly one critical candidate
            if self.calls == 82:
                if failure == "decode":
                    assert jpeg_frame(UNDECODABLE) == UNDECODABLE
                    runtime.vision.decode(UNDECODABLE)
                    pytest.fail("invalid image unexpectedly decoded")
                if failure == "opencv":
                    raise runtime.cv2.error("frame processing failed")
                raise getattr(runtime.native, failure)("inference failed")
            if self.calls in (83, 84):
                return score(q=0.4, p=0.5)  # old trend must not make these actionable
            return score()

    monkeypatch.setattr(runtime.vision, "build_scorer", lambda *_args, **_kwargs: Scorer())
    assert runtime.vision.main(["guard", "u1", "--job-id", JOB, "--yes"]) == 0
    reports = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert reports[81]["confirmations"] == 1
    assert reports[82]["outcome"] == "blind" and reports[82]["confirmations"] == 0
    assert reports[82]["camera_ready"] is False and reports[82]["score_error"]
    assert all(r["outcome"] == "observing" and r["confirmations"] == 0 for r in reports[83:85])
    assert reports[-1]["outcome"] == "paused" and runtime.printer.pauses == 1


@pytest.mark.parametrize("failure", [ValueError, TypeError, RuntimeError, AssertionError, "InvalidArgument", "InvalidGraph"])
def test_unexpected_scoring_failures_are_not_swallowed(
    runtime: Any, failure: type[Exception] | str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    error_type = getattr(runtime.native, failure) if isinstance(failure, str) else failure

    class BrokenScorer:
        geometry = None
        verifier = object()

        def __call__(self, _jpeg: bytes) -> FrameScore:
            raise error_type("implementation failure")

    monkeypatch.setattr(runtime.vision, "build_scorer", lambda *_args, **_kwargs: BrokenScorer())
    with pytest.raises(error_type, match="implementation failure"):
        runtime.vision.main(["guard", "u1", "--job-id", JOB, "--yes"])
    assert runtime.printer.pauses == 0


@pytest.mark.parametrize("boundary", ["identity", "status_write", "actuator"])
def test_native_errors_outside_scoring_are_not_swallowed(
    runtime: Any, boundary: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from printer_ops import guard as policy

    class Scorer:
        geometry = None
        verifier = object()

        def __call__(self, _jpeg: bytes) -> FrameScore:
            return score()

    monkeypatch.setattr(runtime.vision, "build_scorer", lambda *_args, **_kwargs: Scorer())
    error = runtime.cv2.error("outside scoring")

    def fail(*_args: Any, **_kwargs: Any) -> Any:
        raise error

    if boundary == "identity":
        monkeypatch.setattr(runtime.printer, "status", fail)
    elif boundary == "status_write":
        monkeypatch.setattr(policy, "_write_result", fail)
    else:
        monkeypatch.setattr(runtime.printer, "pause", fail)
    with pytest.raises(runtime.cv2.error, match="outside scoring"):
        runtime.vision.main(["guard", "u1", "--job-id", JOB, "--yes"])
