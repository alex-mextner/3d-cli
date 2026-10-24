"""`3d colour-regions` through bin/3d: label files and a mesh in, colour volumes, regions.json and gate verdicts out."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("trimesh")
pytest.importorskip("scipy")
pytest.importorskip("skimage")

import trimesh  # noqa: E402
from colour_regions_fixtures import (  # noqa: E402
    SPHERE_RULES,
    centroid_z,
    fine_box,
    save_labels,
    save_mesh,
    sphere,
    sphere_labels,
    write_rules,
)

from colour_regions.surface import load_mesh  # noqa: E402
from .workflow_helper import run_cli  # noqa: E402

CR = ("colour-regions",)
LOCKLESS = ("--no-lock",)


def _ball(tmp_path: Path) -> tuple[Path, Path, Path]:
    m = sphere(subdivisions=4)
    fl, names = sphere_labels(m)
    return (
        save_mesh(m, tmp_path / "ball.ply"),
        save_labels(tmp_path / "ball.npz", fl, names, m),
        write_rules(tmp_path / "rules.json", SPHERE_RULES),
    )


def test_build_then_verify_on_a_smooth_ball_passes_gate_two_and_fails_gate_one(tmp_path: Path) -> None:
    """A user builds colour volumes from labels and reads regions.json; on a smooth ball the volumes follow the labels
    (gate 2) but no border sits on a relief edge (gate 1), so the verdict is FAIL with exit 1."""
    mesh, labels, rules = _ball(tmp_path)
    work = tmp_path / "work"
    built = run_cli(tmp_path, *CR, "build", str(mesh), "--track", f"sculpt={labels}", "--rules", str(rules), "--out", str(work), "--json", *LOCKLESS)
    assert built.returncode == 0, built.stderr
    doc = json.loads(built.stdout)
    assert [r["name"] for r in doc["regions"]] == ["top", "bottom"]
    on_disk = json.loads((work / "regions.json").read_text(encoding="utf-8"))
    assert on_disk["regions"][1]["assign"] == {"mix": "WR"}
    for region in on_disk["regions"]:
        patch = load_mesh(work / region["shape"]["path"])
        assert patch.is_volume and patch.volume > 100
    gates = tmp_path / "gates.json"
    verdict = run_cli(tmp_path, *CR, "verify", str(mesh), "--work", str(work), "--report", str(gates), "--samples", "1500", *LOCKLESS)
    assert verdict.returncode == 1, verdict.stdout + verdict.stderr
    assert "GATE 2 volumes follow the labels" in verdict.stdout and "->  PASS" in verdict.stdout
    result = json.loads(gates.read_text(encoding="utf-8"))
    assert result["pass"] is False and result["volumes"]["pass"] is True and result["volumes"]["agree"] >= 0.97
    assert result["edges"]["real_pass"] is False and result["edges"]["gated_share"] < 0.2


def test_a_label_file_with_the_wrong_face_count_exits_2_and_says_how_to_fix_it(tmp_path: Path) -> None:
    """One label short of the mesh: exit 2, both counts and the `labels@mesh` remedy in the message, nothing written."""
    mesh, _, rules = _ball(tmp_path)
    m = sphere(subdivisions=4)
    fl, names = sphere_labels(m)
    short = save_labels(tmp_path / "short.npz", fl[:-1], names)
    work = tmp_path / "work"
    r = run_cli(tmp_path, *CR, "build", str(mesh), "--track", f"sculpt={short}", "--rules", str(rules), "--out", str(work), *LOCKLESS)
    assert r.returncode == 2
    assert f"{len(fl) - 1} face labels but the geometry has {len(fl)} faces" in r.stderr
    assert "labels.npz@mesh.stl" in r.stderr
    assert not (work / "regions.json").exists()


def test_verify_exits_1_when_the_border_is_not_on_the_relief_and_0_when_it_is(tmp_path: Path) -> None:
    """On a box, a zone ending exactly at the top rim passes gate 1; the same zone drawn 1.2 mm down the sides fails with exit 1."""
    box = fine_box()
    mesh = save_mesh(box, tmp_path / "box.ply")
    rules = write_rules(
        tmp_path / "rules.json", [{"label": "zone", "group": "zone", "assign": {"key": "W"}, "depth_mm": 1.0, "pitch_mm": 0.3}, {"label": "rest", "group": "d", "assign": None}]
    )
    z = centroid_z(box)
    verdicts = {}
    for name, z_min in (("on_rim", 5.9), ("beside", 4.8)):
        labels = save_labels(tmp_path / f"{name}.npz", np.where(z > z_min, 0, 1).astype(np.int16), ["zone", "rest"], box)
        work = tmp_path / name
        built = run_cli(tmp_path, *CR, "build", str(mesh), "--track", f"t={labels}", "--rules", str(rules), "--out", str(work), "--quiet", *LOCKLESS)
        assert built.returncode == 0, built.stderr
        verdicts[name] = run_cli(tmp_path, *CR, "verify", str(mesh), "--work", str(work), "--samples", "1500", *LOCKLESS)
    assert verdicts["on_rim"].returncode == 0, verdicts["on_rim"].stdout
    assert "PASS (the gate has teeth)" in verdicts["on_rim"].stdout and "GATE 2" in verdicts["on_rim"].stdout
    assert verdicts["beside"].returncode == 1
    assert "gated share" in verdicts["beside"].stdout and "FAIL" in verdicts["beside"].stdout


def test_keeping_approved_borders_through_classify_transfer_class_and_shell_volume(tmp_path: Path) -> None:
    """An approved build's coloured part becomes labels on its skin, rides to a re-meshed copy by nearest face, and the new
    labels cut a colour volume."""
    old = sphere(subdivisions=4)
    z = centroid_z(old)
    skin = save_mesh(old, tmp_path / "skin.ply")
    part = trimesh.Trimesh(np.asarray(old.vertices) + [4.0, 0.0, 0.0], np.asarray(old.faces)[z > 0], process=False)
    part_path = save_mesh(part, tmp_path / "white.ply")
    classes = tmp_path / "approved.npz"
    r = run_cli(tmp_path, *CR, "classify", str(skin), "--part", f"white={part_path}", "--out", str(classes), "--shift", "4,0,0", "--json", *LOCKLESS)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["counts"]["white"] == int((z > 0).sum())

    new = sphere(subdivisions=3, jitter=0.02)
    new_path = save_mesh(new, tmp_path / "new.ply")
    white = tmp_path / "white.npz"
    r = run_cli(
        tmp_path, *CR, "transfer-class", str(new_path), "--source", f"{classes}@{skin}", "--positive", "white", "--as", "white_approved",
        "--out", str(white), "--json", *LOCKLESS,
    )
    assert r.returncode == 0, r.stderr
    got = np.load(white)
    zn = centroid_z(new)
    away = np.abs(zn) > 1.2
    assert json.loads(str(got["names"])) == ["white_approved"]
    assert (got["face_label"][away] == np.where(zn[away] > 0, 0, -1)).all()
    assert json.loads(r.stdout)["new_positive_faces"] == int((got["face_label"] == 0).sum())

    volume = tmp_path / "white_zone.ply"
    r = run_cli(
        tmp_path, *CR, "shell-volume", str(new_path), "--labels", str(white), "--select", "white_.*", "--depth", "1.0", "--pitch", "0.3",
        "--out", str(volume), "--json", *LOCKLESS,
    )
    assert r.returncode == 0, r.stderr
    info = json.loads(r.stdout)
    assert info["watertight"] is True and info["labels"] == ["white_approved"]
    assert load_mesh(volume).is_volume


def test_transfer_then_snap_write_label_files_with_the_expected_labels(tmp_path: Path) -> None:
    """Labels are moved to a re-meshed copy by nearest face, and a zone drawn beside a box rim is snapped onto the rim."""
    old = sphere(subdivisions=4)
    new = sphere(subdivisions=3, jitter=0.02)
    save_mesh(old, tmp_path / "old.ply")
    new_path = save_mesh(new, tmp_path / "new.ply")
    save_labels(tmp_path / "old.npz", (centroid_z(old) > 0).astype(np.int16), ["lower", "upper"])
    moved = tmp_path / "moved.npz"
    r = run_cli(tmp_path, *CR, "transfer", str(new_path), "--labels", f"{tmp_path / 'old.npz'}@{tmp_path / 'old.ply'}", "--out", str(moved), "--tol", "0.25", "--json", *LOCKLESS)
    assert r.returncode == 0, r.stderr
    stats = json.loads(r.stdout)
    assert stats["mode"] == "nearest-face" and stats["dropped_far"] == 0
    assert stats["labels"]["upper"] + stats["labels"]["lower"] == len(new.faces)

    box = fine_box()
    box_path = save_mesh(box, tmp_path / "box.ply")
    zone = save_labels(tmp_path / "zone.npz", np.where(centroid_z(box) > 4.8, 0, -1).astype(np.int16), ["zone"], box)
    snapped = tmp_path / "snapped.npz"
    r = run_cli(tmp_path, *CR, "snap", str(box_path), "--labels", str(zone), "--out", str(snapped), "--band", "2.5", "--fixed", "nothing_.*", "--json", *LOCKLESS)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["changed_faces"] > 0
    after = np.load(snapped)["face_label"]
    zc = centroid_z(box)
    centre = np.asarray(box.triangles_center)
    interior = (zc > 5.99) & (np.abs(centre[:, 0]) < 5) & (np.abs(centre[:, 1]) < 5)
    assert (after[interior] == 0).all() and (after[zc < 4.0] == -1).all()
    assert (after != np.where(zc > 4.8, 0, -1)).any()  # the border moved off the flat side face


def test_help_lists_every_subcommand_and_no_arguments_exits_1(tmp_path: Path) -> None:
    """`--help` documents the label file and every subcommand; a bare call prints the usage and exits 1; a typo exits 2."""
    helped = run_cli(tmp_path, *CR, "--help")
    assert helped.returncode == 0
    for sub in ("build", "shell-volume", "classify", "transfer", "transfer-class", "snap", "verify"):
        assert f"3d colour-regions {sub} " in helped.stdout
    assert "face_label" in helped.stdout
    assert run_cli(tmp_path, *CR).returncode == 1
    typo = run_cli(tmp_path, *CR, "bild", "x.stl", *LOCKLESS)
    assert typo.returncode == 2 and "build" in typo.stderr
