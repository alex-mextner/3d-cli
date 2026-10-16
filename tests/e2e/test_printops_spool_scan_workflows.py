"""E2E stories for spool label photos: `3d printops spool scan` and `spool receive --from-photo`
through `bin/3d`, against a real uvicorn 3d-api talking HTTP to FakeSpoolman (issue #379)."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest
from printops_fixtures import LiveApi

from cli.env import find_magick, find_tesseract
from printops import db, locations

from .workflow_helper import REPO_ROOT, run_cli

LABELS = REPO_ROOT / "tests" / "data" / "spool_labels"
HAVE_OCR = find_tesseract() is not None and find_magick() is not None

K1_BLACK = """\
eSUN PLA+
Color: Black
Net Weight: 1KG
Diameter: 1.75mm
Printing Temp: 205-225°C
Bed Temp: 60-80°C
Batch No: ES2408A12
"""


@pytest.fixture
def shop(live_api: LiveApi) -> LiveApi:
    conn = db.open_db(live_api.settings.db_path)
    try:
        locations.add(conn, "WH:default", "warehouse", actor="e2e")
    finally:
        conn.close()
    return live_api


def _cli(tmp_path: Path, live: LiveApi, *args: str) -> subprocess.CompletedProcess[str]:
    env = {
        "PRINTOPS_API": live.url,
        "PRINTOPS_TOKEN": live.tokens["rw"],
        "THREED_FLEET": "",
    }
    return run_cli(tmp_path, *args, env_extra=env)


def _data_writes(live: LiveApi) -> list[tuple[str, str]]:
    """Spoolman writes other than 3d-api's start-up extra-field registration."""
    return [c for c in live.spoolman.writes() if not c[1].startswith("/field/")]


def _label(tmp_path: Path, text: str = K1_BLACK, name: str = "k1-black.txt") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def test_scanning_a_batch_of_labels_proposes_spools_and_writes_nothing(
    tmp_path: Path, shop: LiveApi
) -> None:
    """The user drops label texts into a folder and scans them: each gets its fields with the
    label line they came from and a ready receive command, the batch gets a table, Spoolman is
    untouched."""
    black = _label(tmp_path)
    white = _label(tmp_path, "SUNLU PLA White 1.75mm\nN.W. 1kg", "white.txt")

    got = _cli(
        tmp_path,
        shop,
        "printops",
        "spool",
        "scan",
        str(black),
        str(white),
        "--location",
        "WH:default",
    )

    assert got.returncode == 0, got.stderr
    assert "vendor    eSUN" in got.stdout and '"eSUN PLA+"' in got.stdout
    assert "Black #000000 (чёрный)" in got.stdout
    assert "nozzle    205–225 °C" in got.stdout
    assert (
        "→ 3d printops spool receive --vendor eSUN --material PLA+ --name Black --color '#000000'"
        " --lot ES2408A12 --location WH:default" in got.stdout
    )
    # the batch table
    assert re.search(
        r"\n2\s+white\.txt\s+SUNLU\s+PLA\s+White #FFFFFF\s+1000 g", got.stdout
    )
    assert "Nothing written" in got.stdout
    assert _data_writes(shop) == []


def test_scan_json_carries_fields_lines_and_the_receive_body(
    tmp_path: Path, shop: LiveApi
) -> None:
    """A script (the Telegram bot, the TUI) reads the proposal as JSON: every field has its
    value and label line, `missing` names what the label lacks, `receive` is the POST body."""
    got = _cli(
        tmp_path,
        shop,
        "printops",
        "spool",
        "scan",
        str(_label(tmp_path, "PETG Blue\nTPU")),
        "--json",
    )

    assert got.returncode == 0, got.stderr
    (prop,) = json.loads(got.stdout)["proposals"]
    assert prop["fields"]["material"] == {"value": "PETG", "line": "PETG Blue"}
    assert prop["missing"] == ["vendor", "net"]
    assert prop["receive"]["color_hex"] == "#0A2989"
    assert prop["ocr"] is None
    assert _data_writes(shop) == []


def test_receive_from_photo_waits_for_confirmation(
    tmp_path: Path, shop: LiveApi
) -> None:
    """Without a terminal and without --yes the proposal is shown and nothing is created; the
    error says how to confirm."""
    got = _cli(
        tmp_path,
        shop,
        "printops",
        "spool",
        "receive",
        "--from-photo",
        str(_label(tmp_path)),
    )

    assert got.returncode == 2
    assert "vendor    eSUN" in got.stdout
    assert "only after confirmation; nothing written" in got.stderr
    assert "--yes" in got.stderr
    assert _data_writes(shop) == []


def test_receive_from_photo_with_yes_creates_the_spool_with_overrides(
    tmp_path: Path, shop: LiveApi
) -> None:
    """The user confirms the K1 black spool, naming it in the inventory's own words: the flag
    beats the label, the rest comes from the label, the sealed net anchors the ledger."""
    got = _cli(
        tmp_path,
        shop,
        "printops",
        "spool",
        "receive",
        "--from-photo",
        str(_label(tmp_path)),
        "--name",
        "чёрный (K1)",
        "--location",
        "WH:default",
        "--yes",
        "--json",
    )

    assert got.returncode == 0, got.stderr
    res = json.loads(got.stdout)
    spool = res["spool"]
    assert res["proposal"]["receive"]["name"] == "чёрный (K1)"
    assert (spool["vendor"], spool["material"], spool["name"], spool["color_hex"]) == (
        "eSUN",
        "PLA+",
        "чёрный (K1)",
        "000000",
    )
    assert spool["lot_nr"] == "ES2408A12"
    assert (
        spool["comment"] == "from label k1-black.txt; nozzle 205–225 °C; bed 60–80 °C"
    )
    assert spool["remaining"]["anchor_source"] == "receive"
    assert "vendor    eSUN" in got.stderr  # the proposal, kept off the JSON stdout
    shown = _cli(tmp_path, shop, "printops", "spool", "ls")
    assert "eSUN PLA+ чёрный (K1) #000000  WH:default" in shown.stdout


def test_receive_from_a_label_without_material_asks_for_it(
    tmp_path: Path, shop: LiveApi
) -> None:
    """A label the rules cannot read enough of is refused before any prompt, naming what is
    missing."""
    got = _cli(
        tmp_path,
        shop,
        "printops",
        "spool",
        "receive",
        "--from-photo",
        str(_label(tmp_path, "some shop sticker")),
        "--yes",
    )

    assert got.returncode == 2
    assert "not read from the label: vendor, material, colour, net" in got.stderr
    assert _data_writes(shop) == []


def test_a_failed_receive_still_prints_the_proposal_json(
    tmp_path: Path, shop: LiveApi
) -> None:
    """A script that confirmed a spool into a location 3d-api does not know gets the proposal
    back as JSON with `spool: null`, and the API's refusal on stderr."""
    got = _cli(
        tmp_path,
        shop,
        "printops",
        "spool",
        "receive",
        "--from-photo",
        str(_label(tmp_path)),
        "--location",
        "WH:nowhere",
        "--yes",
        "--json",
    )

    assert got.returncode == 1
    out = json.loads(got.stdout)
    assert out["spool"] is None
    assert out["proposal"]["receive"]["location"] == "WH:nowhere"
    assert "WH:nowhere" in got.stderr
    assert _data_writes(shop) == []


def test_one_unreadable_label_does_not_cost_the_batch(
    tmp_path: Path, shop: LiveApi
) -> None:
    """A batch with a file that is not UTF-8 text and a path that does not exist still
    proposes the good label; the bad ones are listed as FAILED and the exit is 1."""
    bad = tmp_path / "bad.txt"
    bad.write_bytes(b"\xff\xfe\x00broken")

    got = _cli(
        tmp_path,
        shop,
        "printops",
        "spool",
        "scan",
        str(_label(tmp_path)),
        str(bad),
        str(tmp_path / "gone.jpg"),
    )

    assert got.returncode == 1
    assert "vendor    eSUN" in got.stdout
    assert "FAILED bad.txt: " in got.stdout and "is not UTF-8 text" in got.stdout
    assert "FAILED gone.jpg: file not found" in got.stdout
    assert _data_writes(shop) == []


@pytest.mark.skipif(not HAVE_OCR, reason="tesseract and ImageMagick are not installed")
def test_scanning_a_real_photo_runs_ocr(tmp_path: Path, shop: LiveApi) -> None:
    """A label photographed upside down is OCR'd from its best turn into the same proposal."""
    got = _cli(
        tmp_path,
        shop,
        "printops",
        "spool",
        "scan",
        str(LABELS / "synthetic-esun-upside-down.png"),
    )

    assert got.returncode == 0, got.stderr
    assert "(OCR, turned 180°" in got.stdout
    assert "net       1000 g" in got.stdout
    assert "lot       ES2408A12" in got.stdout


@pytest.mark.skipif(not HAVE_OCR, reason="tesseract and ImageMagick are not installed")
def test_a_photo_named_like_an_option_is_still_a_file(
    tmp_path: Path, shop: LiveApi
) -> None:
    """A photo whose name starts with a dash is read as the image, never as an ImageMagick
    option."""
    dashed = tmp_path / "-label.png"
    dashed.write_bytes((LABELS / "synthetic-esun-upside-down.png").read_bytes())

    got = _cli(tmp_path, shop, "printops", "spool", "scan", "./-label.png")

    assert got.returncode == 0, got.stderr
    assert "-label.png  (OCR, turned 180°" in got.stdout
    assert "vendor    eSUN" in got.stdout
