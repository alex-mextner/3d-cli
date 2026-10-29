"""E2E: `3d mix recipes|tester --shelf` against a real 3d-api over FakeSpoolman (#420)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from mix_tester_fixtures import write_base_project
from printops_fixtures import LiveApi

from printops.client import ApiClient
from printops.spoolman import ensure_extra_fields

from .workflow_helper import run_cli

FLEET = """\
u1:
  model: Snapmaker U1
  owner: own
  access: full
  adapter: moonraker
  host: 127.0.0.1
  port: 9
  axis_limits: {x: [0, 271], y: [0, 335]}
  heads:
    - {label: T1, index: 0, usable: true}
    - {label: T2, index: 1, usable: true}
    - {label: T3, index: 2, usable: true}
    - {label: T4, index: 3, usable: true}
  ams: []
  enclosure: partial
  chamber_heater: false
  plates: [textured_pei]
"""


@pytest.fixture
def fleet(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "fleet.yaml"
    path.write_text(FLEET, encoding="utf-8")
    monkeypatch.setenv("THREED_FLEET", str(path))
    return path


def _receive(
    api: ApiClient,
    name: str,
    colour: str,
    *,
    material: str = "PLA",
    net_g: int = 1000,
    head: str | None = None,
) -> int:
    got = api.post(
        "/v1/spools",
        {
            "vendor": "Elegoo",
            "material": material,
            "name": name,
            "color_hex": colour,
            "net_g": net_g,
        },
    )
    if head:
        api.post(f"/v1/printers/u1/slots/{head}/load", {"spool_id": got["id"]})
    return int(got["id"])


def _stock(live: LiveApi, *, four_loaded: bool) -> dict[str, int]:
    ensure_extra_fields(live.spoolman)
    api = ApiClient(live.url, live.tokens["rw"])
    ids = {
        "black": _receive(api, "Black", "#000000", head="T1"),
        "white": _receive(api, "White", "#FFFFFF", head="T3"),
        "yellow": _receive(api, "Snapdragon", "#F6C700", head="T4"),
    }
    if four_loaded:
        ids["blue"] = _receive(api, "Blue", "#1565C0", head="T2")
    ids["red"] = _receive(api, "Red", "#D32F2F")
    ids["red_empty"] = _receive(api, "Red empty", "#D32F2F", net_g=5)
    ids["red_petg"] = _receive(api, "Red PETG", "#D32F2F", material="PETG")
    return ids


def test_shelf_names_the_spool_the_head_and_both_errors(
    tmp_path: Path, fleet: Path, live_api: LiveApi
) -> None:
    """Alex wants orange; the loaded black, white and yellow cannot get there. `--shelf` names the red
    spool from the shelf, the free head T2 to load it on and the colour error before and after, and
    leaves out the empty red spool and the PETG one. The tester plate is built for that loadout."""
    ids = _stock(live_api, four_loaded=False)
    env = {
        "PRINTOPS_API": live_api.url,
        "PRINTOPS_TOKEN": live_api.tokens["rw"],
        "THREED_FLEET": str(fleet),
    }
    args = ("mix", "recipes", "--target", "#E59520", "--printer", "u1", "--shelf")

    text = run_cli(tmp_path, *args, env_extra=env)
    as_json = run_cli(tmp_path, *args, "--json", env_extra=env)
    assert text.returncode == 0, text.stdout + text.stderr
    assert f'option 1: load spool {ids["red"]} "Red" #D32F2F on T2' in text.stdout
    assert f"3d printops spool load {ids['red']} u1/T2" in text.stdout
    assert "--replace-to" not in text.stdout
    assert f"left out: spool {ids['red_empty']} Red empty: empty" in text.stdout
    assert f"left out: spool {ids['red_petg']} Red PETG: PETG is not PLA" in text.stdout
    shelf = json.loads(as_json.stdout)["shelf"]
    (opt,) = shelf["options"]
    assert (
        opt["load"][0]["spool_id"] == ids["red"]
        and opt["load"][0]["head"] == 2
        and opt["unload"] == []
    )
    assert opt["best"]["delta_e"] < 4 < shelf["baseline"]["best"]["delta_e"]

    write_base_project(tmp_path / "base.3mf")
    out = run_cli(
        tmp_path, "mix", "tester", "--target", "#E59520", "--printer", "u1", "--shelf",
        "-o", "out/orange", "--base-3mf", "base.3mf", "--count", "4", env_extra=env,
    )  # fmt: skip
    assert out.returncode == 0, out.stdout + out.stderr
    assert "the plate is built for option 1: load its spools first" in out.stdout
    manifest = json.loads((tmp_path / "out" / "orange.mix.json").read_text())
    red_slot = next(s for s in manifest["slots"] if s["spool_id"] == ids["red"])
    assert (
        red_slot["slot"] == 2
        and red_slot["hex"] == "#D32F2F"
        and isinstance(red_slot["filament_id"], int)
    )
    assert any(
        "2" in c["pattern"] for c in manifest["candidates"]
    )  # the red spool is in a mix


def test_shelf_with_every_head_loaded_says_which_spool_comes_out(
    tmp_path: Path, fleet: Path, live_api: LiveApi
) -> None:
    """With all four heads loaded the plan names the loaded spool to take out and tells Alex to give
    `--replace-to LOCATION` for it."""
    ids = _stock(live_api, four_loaded=True)
    env = {
        "PRINTOPS_API": live_api.url,
        "PRINTOPS_TOKEN": live_api.tokens["rw"],
        "THREED_FLEET": str(fleet),
    }
    r = run_cli(
        tmp_path,
        "mix",
        "recipes",
        "--target",
        "#E59520",
        "--printer",
        "u1",
        "--shelf",
        env_extra=env,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert (
        f'load spool {ids["red"]} "Red" #D32F2F on T' in r.stdout
        and ", taking out spool " in r.stdout
    )
    assert (
        f"3d printops spool load {ids['red']} u1/T" in r.stdout
        and "--replace-to LOCATION" in r.stdout
    )


def test_shelf_flags_are_checked(tmp_path: Path) -> None:
    """`--shelf` needs --printer; the shelf options need --shelf."""
    no_printer = run_cli(
        tmp_path,
        "mix",
        "recipes",
        "--target",
        "#808080",
        "--slots",
        "1=#000000",
        "--shelf",
    )
    orphan = run_cli(
        tmp_path,
        "mix",
        "recipes",
        "--target",
        "#808080",
        "--slots",
        "1=#000000",
        "--max-swaps",
        "1",
    )
    assert no_printer.returncode == 2
    assert "pass --printer NAME" in no_printer.stderr
    assert orphan.returncode == 2
    assert "need --shelf" in orphan.stderr
