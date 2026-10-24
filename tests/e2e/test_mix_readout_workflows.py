"""E2E: a user prints the tester card, photographs it and reads the swatches back (#416)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

cv2 = pytest.importorskip("cv2")
if not hasattr(cv2, "aruco") or not hasattr(cv2.aruco, "ArucoDetector"):
    pytest.skip("OpenCV >= 4.7 with aruco needed", allow_module_level=True)

from mix_photo_fixtures import phone_photo, scene, top_camera_frame, truths  # noqa: E402
from mix_tester_fixtures import SLOTS, write_base_project  # noqa: E402
from PIL import Image  # noqa: E402

from printops.color import hex_delta_e  # noqa: E402

from .workflow_helper import run_cli  # noqa: E402


def _tester(
    tmp_path: Path, name: str = "grey", target: str = "#808080"
) -> dict[str, Any]:
    write_base_project(tmp_path / "base.3mf")
    run = run_cli(
        tmp_path, "mix", "tester", "--target", target, "--slots", SLOTS,
        "--base-3mf", "base.3mf", "-o", f"out/{name}", "--count", "4",
    )  # fmt: skip
    assert run.returncode == 0, run.stdout + run.stderr
    manifest: dict[str, Any] = json.loads(
        (tmp_path / "out" / f"{name}.mix.json").read_text()
    )
    return manifest


def test_tester_writes_the_card_the_readout_needs(tmp_path: Path) -> None:
    """A user writes a tester and finds the printable card beside the project, with the id and layout the readout reads."""
    manifest = _tester(tmp_path)
    card = tmp_path / "out" / "grey.card.png"
    assert manifest["files"]["card"] == "grey.card.png" and card.is_file()
    w, h = manifest["readout"]["card_mm"]
    assert Image.open(card).size == (round(w * 10), round(h * 10))
    assert (
        manifest["id"]
        and manifest["readout"]["qr"]["payload"] == f"3dmix:{manifest['id']}"
    )


def test_readout_of_a_phone_photo_text_and_json_agree_and_every_swatch_is_within_de2(
    tmp_path: Path,
) -> None:
    """A user photographs the printed card with a phone: the text table and --json agree, every swatch is near its true colour, and the annotated PNG is written."""
    m = _tester(tmp_path)
    lin = scene(tmp_path / "out" / "grey.card.png", m, truths(m))
    phone_photo(tmp_path / "phone.jpg", lin)
    args = (
        "mix",
        "readout",
        "phone.jpg",
        "--manifest",
        "out/grey.mix.json",
        "-o",
        "out/grey-readout",
    )
    text = run_cli(tmp_path, *args)
    as_json = run_cli(tmp_path, *args, "--json")
    assert text.returncode == 0, text.stdout + text.stderr
    assert as_json.returncode == 0, as_json.stderr
    rec = json.loads(as_json.stdout)
    assert rec["schema"] == "mix-readout/1" and rec["manifest"]["id"] == m["id"]
    true = truths(m)
    for r in rec["swatches"]:
        assert hex_delta_e(r["measured_hex"], true[r["number"]]) < 2.0, r
        assert (
            f"{r['number']:>2}  {r['pattern'] or 'T' + str(r['slot']) + ' pure':<10} "
            f"{r['predicted_hex']:<9}  {r['measured_hex']:<9}  "
            f"{r['delta_e_predicted']:>7.2f}  {r['delta_e_target']:>9.2f}"
        ) in text.stdout
    assert text.stdout.splitlines()[0].startswith(
        f"mix readout grey.mix.json (manifest {m['id']}): {len(rec['swatches'])} swatches from phone.jpg"
    )
    assert "fiducials 4/4" in text.stdout and "QR match" in text.stdout
    for name in (
        "grey-readout.readout.json",
        "grey-readout.readout.png",
        "grey-readout.rectified.png",
    ):
        assert (tmp_path / "out" / name).is_file()
    assert (
        json.loads((tmp_path / "out" / "grey-readout.readout.json").read_text()) == rec
    )


def test_readout_of_a_top_camera_frame(tmp_path: Path) -> None:
    """A user reads a 16-bit top-camera frame of the same card: the same numbers within ΔE 2."""
    m = _tester(tmp_path)
    lin = scene(tmp_path / "out" / "grey.card.png", m, truths(m))
    top_camera_frame(tmp_path / "frame.png", lin)
    run = run_cli(
        tmp_path, "mix", "readout", "frame.png", "--manifest", "out/grey.mix.json",
        "-o", "out/top", "--json",
    )  # fmt: skip
    assert run.returncode == 0, run.stdout + run.stderr
    rec = json.loads(run.stdout)
    assert rec["photo"]["size"] == [1536, 1600]
    true = truths(m)
    assert all(
        hex_delta_e(r["measured_hex"], true[r["number"]]) < 2.0 for r in rec["swatches"]
    )


def test_a_photo_of_another_testers_card_is_refused(tmp_path: Path) -> None:
    """A user feeds the photo of one tester card to another tester's manifest: the QR names the other manifest and nothing is written."""
    a = _tester(tmp_path, "grey", "#808080")
    b = _tester(tmp_path, "terracotta", "#B5654F")
    assert a["id"] != b["id"]
    lin = scene(tmp_path / "out" / "terracotta.card.png", b, truths(b))
    phone_photo(tmp_path / "phone.jpg", lin)
    run = run_cli(
        tmp_path, "mix", "readout", "phone.jpg", "--manifest", "out/grey.mix.json",
        "-o", "out/refused",
    )  # fmt: skip
    assert run.returncode == 1
    assert f"names {b['id']!r}" in run.stderr and f"({a['id']})" in run.stderr
    assert not (tmp_path / "out" / "refused.readout.json").exists()
    ok = run_cli(
        tmp_path, "mix", "readout", "phone.jpg", "--manifest", "out/terracotta.mix.json",
        "-o", "out/ok",
    )  # fmt: skip
    assert ok.returncode == 0, ok.stderr


def test_a_photo_that_cannot_be_read_says_what_to_reshoot(tmp_path: Path) -> None:
    """A user sends a photo with no card in it: the readout says no fiducials were found and what to reshoot."""
    _tester(tmp_path)
    Image.new("RGB", (800, 600), (120, 120, 120)).save(tmp_path / "blank.png")
    run = run_cli(
        tmp_path, "mix", "readout", "blank.png", "--manifest", "out/grey.mix.json", "-o", "out/x"
    )  # fmt: skip
    assert run.returncode == 1
    assert "0 of 4 card fiducials found" in run.stderr and "reshoot" in run.stderr


def test_readout_argument_errors(tmp_path: Path) -> None:
    """A user gets the readout arguments wrong: each mistake is refused with the flag, the file or the swatch number to fix."""
    m = _tester(tmp_path)
    lin = scene(tmp_path / "out" / "grey.card.png", m, truths(m))
    phone_photo(tmp_path / "phone.jpg", lin)
    base = (
        "mix",
        "readout",
        "phone.jpg",
        "--manifest",
        "out/grey.mix.json",
        "-o",
        "out/x",
    )
    bare = run_cli(tmp_path, "mix", "readout")
    assert bare.returncode == 2
    assert "readout takes the photo first" in bare.stderr
    no_manifest = run_cli(tmp_path, "mix", "readout", "phone.jpg", "-o", "out/x")
    assert no_manifest.returncode == 2
    assert "--manifest" in no_manifest.stderr
    no_out = run_cli(
        tmp_path, "mix", "readout", "phone.jpg", "--manifest", "out/grey.mix.json"
    )
    assert no_out.returncode == 2
    assert "-o" in no_out.stderr
    bad_white = run_cli(tmp_path, *base, "--white", "grey")
    assert bad_white.returncode == 2
    assert "card, or the number" in bad_white.stderr
    level = run_cli(tmp_path, *base, "--white", "7", "--white-level", "200")
    assert (
        level.returncode == 2 and "--white-level goes with --white card" in level.stderr
    )
    gone = run_cli(
        tmp_path,
        "mix",
        "readout",
        "gone.jpg",
        "--manifest",
        "out/grey.mix.json",
        "-o",
        "out/x",
    )
    assert gone.returncode == 2
    assert "gone.jpg" in gone.stderr
    not_ref = run_cli(tmp_path, *base, "--white", "1")
    assert not_ref.returncode == 1
    assert "not a pure reference" in not_ref.stderr
    # a manifest from before the card existed
    old = json.loads((tmp_path / "out" / "grey.mix.json").read_text())
    del old["readout"], old["id"]
    (tmp_path / "old.mix.json").write_text(json.dumps(old))
    legacy = run_cli(
        tmp_path,
        "mix",
        "readout",
        "phone.jpg",
        "--manifest",
        "old.mix.json",
        "-o",
        "out/x",
    )
    assert legacy.returncode == 2
    assert "no readout card layout" in legacy.stderr
    (tmp_path / "junk.json").write_text("{not json")
    junk = run_cli(
        tmp_path,
        "mix",
        "readout",
        "phone.jpg",
        "--manifest",
        "junk.json",
        "-o",
        "out/x",
    )
    assert junk.returncode == 2
    assert "not JSON" in junk.stderr
    ext = run_cli(
        tmp_path,
        "mix",
        "readout",
        "phone.jpg",
        "--manifest",
        "out/grey.mix.json",
        "-o",
        "out/x.json",
    )
    assert ext.returncode == 2
    assert "without an extension" in ext.stderr


def test_the_readout_only_switch_is_not_accepted_by_the_other_subcommands(
    tmp_path: Path,
) -> None:
    """A user passes --no-qr-check to recipes: only readout takes it, the other commands refuse it."""
    run = run_cli(
        tmp_path,
        "mix",
        "recipes",
        "--target",
        "#808080",
        "--slots",
        SLOTS,
        "--no-qr-check",
    )
    assert run.returncode == 2
    assert "unknown option --no-qr-check" in run.stderr
