"""Real-CLI stories for `3d fit-profile`, `3d fit-silhouette` and `3d view-check` (photo masks -> 3-D params -> mesh -> check).

The stage runs in the interpreter of this test run (`THREED_VISION_PYTHON`), so no vision venv is needed: the closed-form path
needs only numpy + OpenCV + Pillow. Masks are synthetic (a calibrated top-down shot of a known tool) or recorded carpet silhouettes.
"""

from __future__ import annotations

import json
import struct
import subprocess
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pytest

pytest.importorskip("cv2")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import vision_fit_synth as S  # noqa: E402

from threed_vision.io import save_png  # noqa: E402

from .workflow_helper import run_cli, run_shell  # noqa: E402

ENV = {"THREED_VISION_PYTHON": sys.executable}


def cli(tmp_path: Path, *argv: str) -> subprocess.CompletedProcess[str]:
    return run_cli(tmp_path, *argv, env_extra=ENV)


def write_scene(
    tmp_path: Path, name: str = "box_elongated", angle: float = 30.0
) -> dict[str, Any]:
    """A top-down photo of a known tool: mask.png + cam.json ({K, pose: identity}); returns the ground truth."""
    sc = S.topdown_scene(name, angle)
    save_png(tmp_path / "mask.png", sc["views"][0]["mask"])
    (tmp_path / "cam.json").write_text(
        json.dumps({"K": S.KF.tolist(), "pose": "identity"}), encoding="utf-8"
    )
    return sc


def test_fit_profile_json_goes_through_a_shell_redirect(tmp_path: Path) -> None:
    """A user measures a segmented screwdriver (1.263 mm/px), saves the JSON and reads it back in a script."""
    save_png(tmp_path / "mask.png", S.recorded_mask("screwdriver"))
    py = sys.executable
    r = run_shell(
        f"THREED_VISION_PYTHON={py} {py} \"$THREED\" fit-profile mask.png --mm-per-px 1.263 --json > profile.json"
        f" && {py} -c \"import json; d=json.load(open('profile.json')); print(d['kind'], round(d['length_mm']))\"",
        tmp_path,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.split() == ["lathe_profile", "255"]


def test_fit_profile_json_schema_and_instance_reference(tmp_path: Path) -> None:
    (tmp_path / "masks").mkdir()
    save_png(tmp_path / "masks" / "020.png", S.recorded_mask("multimeter"))
    (tmp_path / "instances.json").write_text(
        json.dumps({"instances": [{"id": 20, "mask": "masks/020.png"}]}),
        encoding="utf-8",
    )
    r = cli(
        tmp_path, "fit-profile", "instances.json#20", "--mm-per-px", "1.263", "--json"
    )
    assert r.returncode == 0, r.stdout + r.stderr
    out = json.loads(r.stdout)
    assert out["kind"] == "box" and out["iou_recon"] > 0.97
    assert out["length_mm"] == pytest.approx(140.3, rel=0.01)
    assert {
        "max_width_mm",
        "rect_mm",
        "stations",
        "center_px",
        "axis_deg",
        "time_ms",
    } <= set(out)
    human = cli(tmp_path, "fit-profile", "masks/020.png", "--mm-per-px", "1.263")
    assert human.returncode == 0 and "kind=box" in human.stdout


def test_fit_profile_bad_input_exits_2(tmp_path: Path) -> None:
    save_png(tmp_path / "empty.png", np.zeros((40, 40), bool))
    r = cli(tmp_path, "fit-profile", "empty.png", "--mm-per-px", "1")
    assert r.returncode == 2 and "foreground" in r.stderr
    r = cli(tmp_path, "fit-profile", "nope.png", "--mm-per-px", "1")
    assert r.returncode == 2 and "not found" in r.stderr


def test_fit_silhouette_photo_to_mesh_story(tmp_path: Path) -> None:
    """Top-down photo of an elongated box -> fit.json (params, pose, confidence) + overlay + an STL in mm."""
    sc = write_scene(tmp_path)
    r = cli(
        tmp_path,
        "fit-silhouette",
        "--view",
        "mask.png:cam.json",
        "--planar",
        "--known-depth",
        str(sc["depth"]),
        "--emit",
        "stl",
        "-o",
        "out",
        "--json",
    )
    assert r.returncode == 0, r.stdout + r.stderr
    fit = json.loads(r.stdout)
    assert fit == json.loads(
        (tmp_path / "out" / "fit.json").read_text(encoding="utf-8")
    )
    assert fit["template"]["name"] == "box" and fit["method"] == "closed_form"
    assert fit["dims_mm"]["length_mm"] == pytest.approx(sc["spec"]["L"], rel=0.03)
    assert (
        fit["confidence"]["status"] == "single_view"
        and fit["confidence"]["heldout_iou"] is None
    )
    assert fit["views"][0]["mask"] == "mask.png" and fit["mesh"] == "model.stl"
    assert (tmp_path / "out" / "overlay-0.png").stat().st_size > 100
    stl = (tmp_path / "out" / "model.stl").read_bytes()
    n_tri = struct.unpack("<I", stl[80:84])[0]
    assert n_tri > 100 and len(stl) == 84 + 50 * n_tri  # a complete binary STL
    xyz = np.frombuffer(
        b"".join(stl[84 + 50 * i + 12 : 84 + 50 * i + 48] for i in range(n_tri)), "<f4"
    ).reshape(-1, 3)
    assert xyz[:, 0].max() - xyz[:, 0].min() == pytest.approx(
        fit["dims_mm"]["length_mm"], rel=1e-4
    )  # object frame: X = length


def test_fit_silhouette_user_overrides_win_exactly(tmp_path: Path) -> None:
    sc = write_scene(tmp_path)
    r = cli(
        tmp_path,
        "fit-silhouette",
        "--view",
        "mask.png:cam.json",
        "--planar",
        "--known-depth",
        str(sc["depth"]),
        "--fixed-length-mm",
        "236",
        "--fixed-width-mm",
        "31.5",
        "--fixed-thickness-mm",
        "9.25",
        "--iou-accept",
        "0",
        "--json",
    )
    assert r.returncode == 0, r.stdout + r.stderr
    d = json.loads(r.stdout)["dims_mm"]
    assert (d["length_mm"], d["max_width_mm"], d["thickness_mm"]) == pytest.approx(
        (236.0, 31.5, 9.25), rel=1e-12
    )


def test_fit_silhouette_low_confidence_exits_3_but_still_writes_the_fit(
    tmp_path: Path,
) -> None:
    sc = write_scene(tmp_path, "pliers_flat")
    r = cli(
        tmp_path,
        "fit-silhouette",
        "--view",
        "mask.png:cam.json",
        "--planar",
        "--known-depth",
        str(sc["depth"]),
        "--iou-accept",
        "0",
        "--min-confidence",
        "0.9999",
        "-o",
        "out",
        "--json",
    )
    assert r.returncode == 3, r.stdout + r.stderr
    assert json.loads(r.stdout)["confidence"]["status"] == "low"
    assert (tmp_path / "out" / "fit.json").is_file()


def test_fit_silhouette_input_errors(tmp_path: Path) -> None:
    write_scene(tmp_path)
    r = cli(tmp_path, "fit-silhouette", "--view", "mask.png:cam.json", "--json")
    assert r.returncode == 2 and "known_depth" in r.stderr
    r = cli(
        tmp_path,
        "fit-silhouette",
        "--view",
        "mask.png:cam.json",
        "--known-depth",
        "650",
        "--emit",
        "stl",
    )
    assert r.returncode == 2 and "-o" in r.stderr
    r = cli(tmp_path, "fit-silhouette", "--view", "mask.png", "--known-depth", "650")
    assert r.returncode == 2 and "MASK:CAM.json" in r.stderr
    (tmp_path / "badcam.json").write_text(
        json.dumps({"K": S.KF.tolist()}), encoding="utf-8"
    )
    r = cli(
        tmp_path,
        "fit-silhouette",
        "--view",
        "mask.png:badcam.json",
        "--known-depth",
        "650",
    )
    assert r.returncode == 2 and "pose" in r.stderr


def test_view_check_single_photo_then_extra_views(tmp_path: Path) -> None:
    """The fit of one photo is `unverified` until another view exists; a wrong second view exits 3 with an overlay."""
    sc = write_scene(tmp_path)
    fit = cli(
        tmp_path,
        "fit-silhouette",
        "--view",
        "mask.png:cam.json",
        "--planar",
        "--known-depth",
        str(sc["depth"]),
        "-o",
        "out",
    )
    assert fit.returncode == 0, fit.stdout + fit.stderr
    solo = cli(tmp_path, "view-check", "out/fit.json", "--json")
    assert solo.returncode == 0, solo.stdout + solo.stderr
    res = json.loads(solo.stdout)
    assert (
        res["mode"] == "single_photo"
        and res["status"] == "unverified"
        and res["views"] == []
    )
    assert res["needs_views"] and res["sanity"]["ok"] is True

    same = cli(
        tmp_path, "view-check", "out/fit.json", "--view", "mask.png:cam.json", "--json"
    )
    assert same.returncode == 0, same.stdout + same.stderr
    ok = json.loads(same.stdout)
    assert (
        ok["status"] == "ok"
        and ok["views"][0]["iou"] > 0.9
        and ok["views"][0]["flag"] is False
    )

    wrong = np.rot90(
        sc["views"][0]["mask"]
    ).copy()  # an object that is NOT what was fitted
    save_png(tmp_path / "wrong.png", wrong)
    bad = cli(
        tmp_path,
        "view-check",
        "out/fit.json",
        "--view",
        "wrong.png:cam.json",
        "--overlay",
        "check.png",
        "--json",
    )
    assert bad.returncode == 3, bad.stdout + bad.stderr
    res = json.loads(bad.stdout)
    assert (
        res["status"] == "flagged"
        and res["views"][0]["flag"] is True
        and res["min_iou"] < 0.75
    )
    assert res["needs_views"] and (tmp_path / "check.png").stat().st_size > 100
    strict = cli(tmp_path, "view-check", "out/fit.json", "--view", "mask.png:cam.json", "--flag-below", "0.9999")
    assert strict.returncode == 3 and "FLAG" in strict.stdout  # even the fitted photo is below a perfect-IoU bar


def test_view_check_errors(tmp_path: Path) -> None:
    r = cli(tmp_path, "view-check", "missing.json")
    assert r.returncode == 2 and "not found" in r.stderr
    (tmp_path / "junk.json").write_text("{}", encoding="utf-8")
    r = cli(tmp_path, "view-check", "junk.json")
    assert r.returncode == 2 and "not a threed_vision fit" in r.stderr


def test_commands_have_help_and_are_listed(tmp_path: Path) -> None:
    for name in ("fit-profile", "fit-silhouette", "view-check"):
        r = cli(tmp_path, name, "--help")
        assert r.returncode == 0 and f"3d {name}" in r.stdout
    listing = cli(tmp_path, "help")
    assert all(
        n in listing.stdout for n in ("fit-profile", "fit-silhouette", "view-check")
    )


def test_fit_silhouette_heldout_template_and_every_mesh_format(tmp_path: Path) -> None:
    """--heldout makes heldout_iou, --template overrides the kind, --emit writes every format, --mesh-frame world bakes the pose in."""
    sc = write_scene(tmp_path)
    base = ["fit-silhouette", "--view", "mask.png:cam.json", "--planar", "--known-depth", str(sc["depth"]), "--iou-accept", "0"]
    r = cli(tmp_path, *base, "--heldout", "mask.png:cam.json", "--template", "flat", "--emit", "obj", "--mesh-frame", "world", "-o", "out", "--json")
    assert r.returncode == 0, r.stdout + r.stderr
    fit = json.loads(r.stdout)
    assert fit["template"]["name"] == "flat" and fit["confidence"]["heldout_iou"] > 0.9 and fit["confidence"]["status"] == "ok"
    obj = (tmp_path / "out" / "model.obj").read_text(encoding="utf-8")
    zs = [float(line.split()[3]) for line in obj.splitlines() if line.startswith("v ")]
    assert min(zs) > 0.5 * sc["depth"]  # world frame: the object sits at the plane depth, not at the origin
    for fmt in ("glb", "scad"):
        r = cli(tmp_path, *base, "--emit", fmt, "-o", f"out_{fmt}")
        assert r.returncode == 0, r.stdout + r.stderr
        assert (tmp_path / f"out_{fmt}" / f"model.{fmt}").stat().st_size > 500


def test_fit_profile_options_reach_the_algorithm(tmp_path: Path) -> None:
    save_png(tmp_path / "m.png", S.recorded_mask("multimeter"))
    r = cli(tmp_path, "fit-profile", "m.png", "--mm-per-px", "1", "--k", "5", "--nbins", "40", "--axis-deg", "0", "--json")
    assert r.returncode == 0, r.stdout + r.stderr
    out = json.loads(r.stdout)
    assert len(out["stations"]) == 5 and abs(out["axis_deg"]) in (0.0, 180.0)


def test_fit_silhouette_stations_seed_and_search_flags_are_wired(tmp_path: Path) -> None:
    sc = write_scene(tmp_path)
    r = cli(
        tmp_path, "fit-silhouette", "--view", "mask.png:cam.json", "--planar", "--known-depth", str(sc["depth"]), "--iou-accept", "0",
        "--stations", "6", "--seed", "5", "--max-tries", "1", "--no-cma", "--device", "cpu", "--json",
    )
    assert r.returncode == 0, r.stdout + r.stderr
    fit = json.loads(r.stdout)
    assert len(fit["params"]["w_mm"]) == 6 and fit["template"]["stations"] == 6
    r = cli(tmp_path, "fit-silhouette", "--view", "mask.png:cam.json", "--planar", "--known-depth", str(sc["depth"]), "--device", "cuda", "--iou-accept", "1.01")
    assert r.returncode in (2, 3, 127, 0)  # strict cuda without torch/GPU: structured error or a fit, never a traceback
    assert "Traceback" not in r.stderr
