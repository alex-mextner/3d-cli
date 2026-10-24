"""E2E: a user reads the target colour off a mockup photo and feeds it to the tester (#415)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from mix_target_fixtures import PATCH, WHITE, photo
from mix_tester_fixtures import SLOTS, write_base_project

from printops.color import hex_delta_e
from slicing.mix_tester import validate_manifest

from .workflow_helper import run_cli

BOX = ",".join(map(str, PATCH))
WHITE_BOX = ",".join(map(str, WHITE))
PATCH_HEX = "#B5654F"


def test_target_text_and_json_agree_and_hit_the_known_patch(tmp_path: Path) -> None:
    """A user points at a wall patch of a warm-lit photo: text and --json name the same colour."""
    photo(tmp_path / "mockup.png", PATCH_HEX, (1.0, 0.82, 0.55))
    args = ("mix", "target", "mockup.png", "--box", BOX, "--white", WHITE_BOX)
    text = run_cli(tmp_path, *args)
    as_json = run_cli(tmp_path, *args, "--json")
    assert text.returncode == 0, text.stdout + text.stderr
    assert as_json.returncode == 0, as_json.stderr
    d = json.loads(as_json.stdout)
    assert d["schema"] == "mix-target/1"
    assert hex_delta_e(d["target"], PATCH_HEX) < 1.0
    first = text.stdout.splitlines()[0]
    assert first.startswith(
        f"mix target {d['target']}  (photo {tmp_path / 'mockup.png'}, 320x240, box {d['box']}"
    )
    assert f"sha256 {d['sha256']}" in text.stdout
    assert (
        d["sha256"]
        == hashlib.sha256((tmp_path / "mockup.png").read_bytes()).hexdigest()
    )
    assert (
        f"gain {d['gain'][0]:.3f} {d['gain'][1]:.3f} {d['gain'][2]:.3f}" in text.stdout
    )


def test_target_of_a_render_skips_the_white_balance(tmp_path: Path) -> None:
    photo(tmp_path / "render.png", PATCH_HEX)
    run = run_cli(
        tmp_path, "mix", "target", "render.png", "--box", BOX, "--no-wb", "--json"
    )
    assert run.returncode == 0, run.stderr
    d = json.loads(run.stdout)
    assert d["kind"] == "render" and d["white_box"] is None and d["gain"] is None
    assert hex_delta_e(d["target"], PATCH_HEX) < 1.0
    text = run_cli(tmp_path, "mix", "target", "render.png", "--box", BOX, "--no-wb")
    assert "white balance: none (render)" in text.stdout


def test_target_refuses_a_photo_without_a_white_reference_and_bad_boxes(
    tmp_path: Path,
) -> None:
    photo(tmp_path / "mockup.png", PATCH_HEX)
    missing = run_cli(tmp_path, "mix", "target", "mockup.png", "--box", BOX)
    assert missing.returncode == 2
    assert "--white" in missing.stderr and "--no-wb" in missing.stderr
    both = run_cli(
        tmp_path,
        "mix",
        "target",
        "mockup.png",
        "--box",
        BOX,
        "--white",
        WHITE_BOX,
        "--no-wb",
    )
    assert both.returncode == 2
    outside = run_cli(
        tmp_path, "mix", "target", "mockup.png", "--box", "300,200,900,900", "--no-wb"
    )
    assert outside.returncode == 2 and "outside the 320x240 image" in outside.stderr
    nofile = run_cli(tmp_path, "mix", "target", "nope.png", "--box", BOX, "--no-wb")
    assert nofile.returncode == 2 and "nope.png" in nofile.stderr


def test_tester_target_photo_records_image_box_and_sha256_in_the_manifest(
    tmp_path: Path,
) -> None:
    """`mix tester --target-photo` targets the photo's colour and the manifest says where it came from."""
    write_base_project(tmp_path / "base.3mf")
    img = photo(tmp_path / "mockup.png", PATCH_HEX, (1.0, 0.82, 0.55))
    args = (
        "mix", "tester", "--target-photo", "mockup.png", "--box", BOX, "--white", WHITE_BOX,
        "--slots", SLOTS, "--base-3mf", "base.3mf", "-o", "out/wall", "--count", "4",
    )  # fmt: skip
    text = run_cli(tmp_path, *args)
    as_json = run_cli(tmp_path, *args, "--json")
    assert text.returncode == 0, text.stdout + text.stderr
    summary = json.loads(as_json.stdout)
    manifest = json.loads((tmp_path / "out" / "wall.mix.json").read_text())
    assert validate_manifest(manifest) == []
    assert hex_delta_e(manifest["target"], PATCH_HEX) < 1.0
    src = manifest["target_source"]
    assert src["image"] == str((tmp_path / "mockup.png").resolve()) and src[
        "box"
    ] == list(PATCH)
    assert src["white_box"] == list(WHITE) and src["white_balance"] is True
    assert src["sha256"] == hashlib.sha256(img.read_bytes()).hexdigest()
    assert summary["target"] == manifest["target"]
    assert summary["target_source"] == src
    assert text.stdout.splitlines()[0].startswith(
        f"mix tester for {manifest['target']}:"
    )
    assert (
        f"target from photo {src['image']} (sha256 {src['sha256'][:12]})" in text.stdout
    )

    # the same target as `3d mix target` prints
    target = json.loads(
        run_cli(
            tmp_path,
            "mix",
            "target",
            "mockup.png",
            "--box",
            BOX,
            "--white",
            WHITE_BOX,
            "--json",
        ).stdout
    )
    assert target["target"] == manifest["target"]


def test_recipes_take_the_target_photo_and_a_hex_target_has_no_source(
    tmp_path: Path,
) -> None:
    photo(tmp_path / "mockup.png", PATCH_HEX, (0.9, 0.9, 1.0))
    photo_run = run_cli(
        tmp_path, "mix", "recipes", "--target-photo", "mockup.png", "--box", BOX,
        "--white", WHITE_BOX, "--slots", SLOTS, "--json",
    )  # fmt: skip
    assert photo_run.returncode == 0, photo_run.stderr
    payload = json.loads(photo_run.stdout)
    assert hex_delta_e(payload["target"], PATCH_HEX) < 1.0
    assert payload["target_source"]["sha256"]
    hex_run = run_cli(
        tmp_path, "mix", "recipes", "--target", "#808080", "--slots", SLOTS, "--json"
    )
    assert "target_source" not in json.loads(hex_run.stdout)


def test_target_and_target_photo_are_exclusive_and_photo_flags_need_the_photo(
    tmp_path: Path,
) -> None:
    photo(tmp_path / "mockup.png", PATCH_HEX)
    both = run_cli(
        tmp_path, "mix", "recipes", "--target", "#808080", "--target-photo", "mockup.png",
        "--box", BOX, "--no-wb", "--slots", SLOTS,
    )  # fmt: skip
    assert both.returncode == 2 and "not both" in both.stderr
    stray = run_cli(
        tmp_path,
        "mix",
        "recipes",
        "--target",
        "#808080",
        "--box",
        BOX,
        "--slots",
        SLOTS,
    )
    assert stray.returncode == 2 and "only goes with --target-photo" in stray.stderr


def test_target_option_errors_name_the_fix(tmp_path: Path) -> None:
    photo(tmp_path / "mockup.png", PATCH_HEX)
    no_box = run_cli(tmp_path, "mix", "target", "mockup.png", "--no-wb")
    assert no_box.returncode == 2 and "--box" in no_box.stderr
    level_no_white = run_cli(
        tmp_path, "mix", "target", "mockup.png", "--box", BOX, "--no-wb", "--white-level", "240"
    )  # fmt: skip
    assert (
        level_no_white.returncode == 2
        and "--white-level needs --white" in level_no_white.stderr
    )
    too_high = run_cli(
        tmp_path, "mix", "target", "mockup.png", "--box", BOX, "--white", WHITE_BOX, "--white-level", "300"
    )  # fmt: skip
    assert too_high.returncode == 2 and "--white-level" in too_high.stderr
    missing = run_cli(
        tmp_path, "mix", "recipes", "--target-photo", "gone.png", "--box", BOX, "--no-wb", "--slots", SLOTS
    )  # fmt: skip
    assert missing.returncode == 2 and "gone.png" in missing.stderr
    no_region = run_cli(
        tmp_path, "mix", "recipes", "--target-photo", "mockup.png", "--no-wb", "--slots", SLOTS
    )  # fmt: skip
    assert no_region.returncode == 2 and "--box" in no_region.stderr
    not_image = tmp_path / "notes.txt"
    not_image.write_text("not a picture")
    bad = run_cli(tmp_path, "mix", "target", "notes.txt", "--box", BOX, "--no-wb")
    assert bad.returncode == 2 and "cannot be read as an image" in bad.stderr


def test_white_level_changes_the_exposure_of_the_target(tmp_path: Path) -> None:
    photo(tmp_path / "mockup.png", PATCH_HEX, (1.0, 0.82, 0.55))
    base = ("mix", "target", "mockup.png", "--box", BOX, "--white", WHITE_BOX, "--json")
    low = json.loads(run_cli(tmp_path, *base, "--white-level", "200").stdout)
    default = json.loads(run_cli(tmp_path, *base).stdout)
    assert low["white_level"] == 200 and default["white_level"] == 235
    assert low["lab"][0] < default["lab"][0]


def test_a_manifest_with_a_broken_target_source_is_invalid(tmp_path: Path) -> None:
    write_base_project(tmp_path / "base.3mf")
    photo(tmp_path / "mockup.png", PATCH_HEX)
    args = (
        "mix", "tester", "--target-photo", "mockup.png", "--box", BOX, "--no-wb",
        "--slots", SLOTS, "--base-3mf", "base.3mf", "-o", "out/wall",
    )  # fmt: skip
    assert run_cli(tmp_path, *args).returncode == 0
    manifest = json.loads((tmp_path / "out" / "wall.mix.json").read_text())
    assert manifest["target_source"]["kind"] == "render"
    assert manifest["target_source"]["white_box"] is None
    assert validate_manifest(manifest) == []
    manifest["target_source"]["sha256"] = "nope"
    assert any("sha256" in e for e in validate_manifest(manifest))


def test_reader_script_reports_bad_arguments_as_a_usage_error(tmp_path: Path) -> None:
    """A malformed --box reaches the reader and comes back as exit 2 with the reason, not a traceback."""
    photo(tmp_path / "mockup.png", PATCH_HEX)
    run = run_cli(tmp_path, "mix", "target", "mockup.png", "--box", "1,2,3", "--no-wb")
    assert run.returncode == 2 and "x0,y0,x1,y1" in run.stderr
    assert "Traceback" not in run.stderr
