"""ImageMagick 6 handling in lib/perceptual_metrics.py (Ubuntu ships IM6: no `magick`)."""

from __future__ import annotations

import perceptual_metrics as pm
import pytest


class _Done:
    def __init__(self, returncode: int, stderr: str = "", stdout: str = "") -> None:
        self.returncode = returncode
        self.stderr = stderr
        self.stdout = stdout


def test_psnr_images_resizes_with_convert_and_caps_identical_on_imagemagick6(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """IM6 has only `compare`: it prints `0` for identical images (exit 0) and cannot resize."""
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs: object) -> _Done:
        calls.append(cmd)
        if cmd[0] == "identify":
            return _Done(0, stdout="10x10")
        if cmd[0] == "convert":
            with open(cmd[-1], "wb") as out:
                out.write(b"resized")
            return _Done(0)
        return _Done(0, "0")

    monkeypatch.setattr(pm, "_find_magick", lambda: "compare")
    monkeypatch.setattr(pm.subprocess, "run", fake_run)
    result = pm.psnr_images("/a.png", "/b.png")
    assert result["value"] == pm.PSNR_CAP_DB
    assert result["capped"] is True
    convert = next(c for c in calls if c[0] == "convert")
    assert convert[:2] == ["convert", "/b.png"] and "10x10!" in convert
    compare = next(c for c in calls if c[0] == "compare")
    assert compare[-2] == convert[-1] != "/b.png"
    assert not any(c[0] == "compare" and "-resize" in c for c in calls)


def test_psnr_images_reads_the_value_when_images_differ(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(cmd: list[str], **_kwargs: object) -> _Done:
        return _Done(
            0 if cmd[0] == "identify" else 1, "" if cmd[0] == "identify" else "31.5"
        )

    monkeypatch.setattr(pm, "_find_magick", lambda: "compare")
    monkeypatch.setattr(pm.subprocess, "run", fake_run)
    result = pm.psnr_images("/a.png", "/b.png")
    assert result["value"] == pytest.approx(31.5)
    assert result["capped"] is False


def test_psnr_images_does_not_report_a_compare_error_as_identical(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(cmd: list[str], **_kwargs: object) -> _Done:
        if cmd[0] == "identify":
            return _Done(0)
        return _Done(2, "compare: unable to open image `/a.png'")

    monkeypatch.setattr(pm, "_find_magick", lambda: "compare")
    monkeypatch.setattr(pm.subprocess, "run", fake_run)
    with pytest.raises(ValueError, match="could not parse PSNR"):
        pm.psnr_images("/a.png", "/b.png")
