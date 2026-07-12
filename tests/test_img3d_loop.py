"""Unit tests for the failure-handling of the closed image->3D loop (lib/img3d_loop.py).

These pin the silent-false-PASS holes shut WITHOUT OpenSCAD:
  - render_blockout must not fall through to a stale PNG when a render fails;
  - write_recovery_panel must not crash on a None (failed) model render;
  - recover() must record a failure status (not a silent previous-candidate pass) when
    every render fails.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("PIL")

# fit_camera (imported transitively by img3d_loop) resolves openscad at IMPORT time and
# sys.exits if it is absent, so point OPENSCAD at any existing binary FOR THE DURATION OF
# THE IMPORT only. Restoring it immediately keeps an openscad-less CI able to import the
# pure helpers WITHOUT the stub leaking into os.environ — where it would be inherited by
# the real `bin/3d render` subprocesses that later e2e tests spawn (a python interpreter
# fed OpenSCAD's flags fails every render). Do NOT hoist this into a module-level
# setdefault: that mutates the process env for the whole pytest run.
_prev_openscad = os.environ.get("OPENSCAD")
os.environ["OPENSCAD"] = sys.executable

import img3d_loop  # noqa: E402
from ai.backends import MockBackend  # noqa: E402
from ai.blockout import BlockoutParams  # noqa: E402

if _prev_openscad is None:
    os.environ.pop("OPENSCAD", None)
else:
    os.environ["OPENSCAD"] = _prev_openscad


def _fake_proc(returncode: int) -> subprocess.CompletedProcess[bytes]:
    return subprocess.CompletedProcess(args=["openscad"], returncode=returncode)


def _ref() -> tuple[Any, Any]:
    refm = np.zeros((16, 24), dtype=np.uint8)
    refm[4:12, 6:18] = 1
    reference_rgb = np.full((16, 24, 3), 200, dtype=np.int16)
    return refm, reference_rgb


def test_render_blockout_does_not_fall_through_to_stale_png(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A FAILED render (nonzero rc, no fresh output) must return None, never the previous
    candidate's leftover cand.png sitting under the reused fixed name."""
    stale = tmp_path / "cand.png"
    stale.write_bytes(b"stale-previous-candidate")  # leftover from a prior render

    def fake_run(*_a: Any, **_k: Any) -> subprocess.CompletedProcess[bytes]:
        return _fake_proc(returncode=1)  # fails, writes nothing

    monkeypatch.setattr(img3d_loop.subprocess, "run", fake_run)
    arr = img3d_loop.render_blockout(
        BlockoutParams(n_columns=4), 0.0, 0.0, (32, 32), str(tmp_path),
        name="cand", center=[0.0, 0.0, 0.0],
    )
    assert arr is None
    assert not stale.exists(), "stale target must be removed before a failing render"


def test_render_blockout_removes_partial_output_on_nonzero_rc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even if OpenSCAD leaves a NON-EMPTY partial file, a nonzero rc is a FAILURE and the
    partial output is removed — so no path-based reader can score it as a render."""

    def fake_run(cmd: list[str], **_k: Any) -> subprocess.CompletedProcess[bytes]:
        out = cmd[cmd.index("-o") + 1]
        Path(out).write_bytes(b"partial-nonempty")  # bogus non-empty output + error rc
        return _fake_proc(returncode=1)

    monkeypatch.setattr(img3d_loop.subprocess, "run", fake_run)
    out_png = tmp_path / "cand.png"
    arr = img3d_loop.render_blockout(
        BlockoutParams(n_columns=4), 0.0, 0.0, (32, 32), str(tmp_path),
        name="cand", center=[0.0, 0.0, 0.0],
    )
    assert arr is None
    assert not out_png.exists(), "a failed render's partial output must be removed"


def test_render_blockout_none_on_empty_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 0-byte output with rc 0 is still a FAILURE (empty stub, not a render)."""

    def fake_run(cmd: list[str], **_k: Any) -> subprocess.CompletedProcess[bytes]:
        out = cmd[cmd.index("-o") + 1]
        Path(out).write_bytes(b"")  # empty stub, rc 0
        return _fake_proc(returncode=0)

    monkeypatch.setattr(img3d_loop.subprocess, "run", fake_run)
    arr = img3d_loop.render_blockout(
        BlockoutParams(n_columns=4), 0.0, 0.0, (32, 32), str(tmp_path),
        name="cand", center=[0.0, 0.0, 0.0],
    )
    assert arr is None


def test_veto_candidate_fails_closed_when_render_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refine candidate whose render fails must fail the dispose-gate veto — even with a
    stale/partial veto_cand.png on disk and a canned backend that would report a match."""
    stale = tmp_path / "veto_cand.png"
    stale.write_bytes(b"\x89PNG\r\n\x1a\nstale-veto-candidate")  # leftover, non-empty
    monkeypatch.setattr(img3d_loop, "render_blockout", lambda *a, **k: None)

    backend = MockBackend(response=json.dumps({"column_count": 5}))
    veto = img3d_loop._veto_candidate(
        BlockoutParams(n_columns=5), 0.0, 0.0, (24, 16), str(tmp_path),
        backend, {"column_count": 5.0}, img3d_loop.FEATURE_SPECS["temple"],
    )
    assert not veto.passed, "a failed candidate render must fail the veto closed"


def test_write_recovery_panel_survives_failed_model_render(tmp_path: Path) -> None:
    """A None model_rgb (final render failed) must yield a diagnostic panel, not a crash."""
    refm, reference_rgb = _ref()
    panel = tmp_path / "proof_panel.png"
    img3d_loop.write_recovery_panel(
        str(panel), reference_rgb, refm, None,
        {"edge_f1@4": 0.0}, "failed", "veto FAIL: render missing",
    )
    assert panel.exists()


def test_recover_records_failure_when_all_renders_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If every blockout render fails, recover() must land a `failed` status with a veto
    that did NOT pass — not a silent success carried over from a prior candidate."""
    monkeypatch.setattr(img3d_loop, "render_blockout", lambda *a, **k: None)
    monkeypatch.setattr(img3d_loop, "model_centroid", lambda *a, **k: [0.0, 0.0, 0.0])

    refm, reference_rgb = _ref()
    backend = MockBackend(response=json.dumps({"column_count": 5}))
    out_dir = tmp_path / "out"

    result = img3d_loop.recover(
        refm, reference_rgb, template="temple", expected_columns=5,
        backend=backend, out_dir=str(out_dir), size=(24, 16), tmp=str(tmp_path),
    )
    assert result["recovery_status"] == "failed"
    assert result["veto"]["passed"] is False
    assert Path(result["proof_panel"]).exists()
    # the failed final render left no recovered_render.png; the veto fails CLOSED on it
    assert not (out_dir / "recovered_render.png").exists()
    assert result["recovered_render"] is None  # honest schema: null, not an absent path


def test_recover_does_not_pass_veto_on_stale_render_in_reused_out_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reused --out dir with a leftover recovered_render.png from a prior successful run
    must NOT satisfy the veto when THIS run's final render fails. The stale artifact is
    removed before the final render, so the veto fails closed on an absent file."""
    monkeypatch.setattr(img3d_loop, "render_blockout", lambda *a, **k: None)
    monkeypatch.setattr(img3d_loop, "model_centroid", lambda *a, **k: [0.0, 0.0, 0.0])

    refm, reference_rgb = _ref()
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    stale = out_dir / "recovered_render.png"
    stale.write_bytes(b"\x89PNG\r\n\x1a\nprior-successful-run")  # leftover render

    # A MockBackend that would happily report the expected count from a text-only prompt.
    backend = MockBackend(response=json.dumps({"column_count": 5}))
    result = img3d_loop.recover(
        refm, reference_rgb, template="temple", expected_columns=5,
        backend=backend, out_dir=str(out_dir), size=(24, 16), tmp=str(tmp_path),
    )
    assert result["veto"]["passed"] is False, "must not pass against a stale prior render"
    assert result["recovery_status"] == "failed"
    assert not stale.exists(), "stale render must be removed before the final render"
