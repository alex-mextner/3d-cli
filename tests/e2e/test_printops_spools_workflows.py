"""E2E stories for `3d printops spool|tare` and `3d lint --printops` data rules through `bin/3d`,
against a real uvicorn 3d-api talking HTTP to FakeSpoolman (issues #160, #163)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from fleet_fixtures import REPO_FLEET
from printops_fixtures import LiveApi

from printops import db, locations

from .workflow_helper import run_cli

FLEET = """\
u1:
  model: Snapmaker U1
  owner: own
  access: full
  adapter: moonraker
  host: 127.0.0.1
  port: 9
  heads:
    - {label: T1, index: 0, usable: true}
    - {label: T2, index: 1, usable: false, reason: T2 is broken}
    - {label: T3, index: 2, usable: true}
    - {label: T4, index: 3, usable: true}
"""


@pytest.fixture
def shop(tmp_path: Path, live_api: LiveApi, monkeypatch: pytest.MonkeyPatch) -> LiveApi:
    """The live 3d-api with a fleet (U1, T2 broken) and the default warehouse + one shelf."""
    fleet = tmp_path / "fleet.yaml"
    fleet.write_text(FLEET, encoding="utf-8")
    monkeypatch.setenv("THREED_FLEET", str(fleet))  # the API runs in this process
    conn = db.open_db(live_api.settings.db_path)
    try:
        locations.add(conn, "WH:default", "warehouse", actor="e2e")
        locations.add(conn, "WH:A", "zone", actor="e2e")
        locations.add(conn, "WH:A/S1", "shelf", actor="e2e")
    finally:
        conn.close()
    return live_api


def _cli(
    tmp_path: Path, live: LiveApi, *args: str, token: str | None = "rw"
) -> subprocess.CompletedProcess[str]:
    env = {"PRINTOPS_API": live.url, "THREED_FLEET": ""}
    if token:
        env["PRINTOPS_TOKEN"] = live.tokens[token]
    return run_cli(tmp_path, *args, env_extra=env)


def test_spool_story_receive_load_print_unload_weigh_empty(
    tmp_path: Path, shop: LiveApi
) -> None:
    """A spool arrives, goes on a shelf, into U1 T3, a print books its estimate, it comes back
    to the shelf and is weighed (the interval collapses), and finally runs out."""
    got = _cli(
        tmp_path,
        shop,
        "printops",
        "spool",
        "receive",
        "--vendor",
        "Elegoo",
        "--material",
        "PLA+",
        "--name",
        "RAPID PLA+ Green",
        "--color",
        "#4FE44D",
        "--net",
        "1000",
        "--price",
        "1499",
        "--location",
        "WH:default",
        "--json",
    )
    assert got.returncode == 0, got.stderr
    sid = str(json.loads(got.stdout)["id"])

    moved = _cli(tmp_path, shop, "printops", "spool", "move", sid, "WH:A/S1")
    broken = _cli(tmp_path, shop, "printops", "spool", "load", sid, "u1/T2")
    loaded = _cli(tmp_path, shop, "printops", "spool", "load", sid, "u1/T3")
    used = _cli(
        tmp_path,
        shop,
        "printops",
        "spool",
        "use",
        sid,
        "100",
        "--source",
        "gcode_estimate",
        "--job",
        "plate-3",
    )
    listed = _cli(tmp_path, shop, "printops", "spool", "ls", "--loaded")
    unloaded = _cli(
        tmp_path,
        shop,
        "printops",
        "spool",
        "unload",
        "u1/T3",
        "--to",
        "WH:A/S1",
        "--gross",
        "974",
    )
    shown = _cli(tmp_path, shop, "printops", "spool", "show", sid)
    emptied = _cli(
        tmp_path, shop, "printops", "spool", "empty", sid, "--note", "ran out"
    )

    assert moved.returncode == 0 and "WH:A/S1" in moved.stdout, moved.stderr
    assert (
        broken.returncode == 1
        and "T2 is broken" in broken.stderr
        and "HTTP 409" in broken.stderr
    )
    assert loaded.returncode == 0 and "in u1/T3" in loaded.stdout, loaded.stderr
    assert used.returncode == 0 and "900 ± 22 g (878–922)" in used.stdout, used.stderr
    assert (
        listed.stdout.startswith(f"#{sid}")
        and "Elegoo PLA+ RAPID PLA+ Green #4FE44D  in u1/T3" in listed.stdout
    )
    assert unloaded.returncode == 0, unloaded.stderr
    assert (
        "WH:A/S1  820 ± 10 g (810–830)" in unloaded.stdout
        and "ledger was off by -80 g" in unloaded.stdout
    )
    assert "tare       154 ± 10 g (SpoolmanDB" in shown.stdout
    assert "weigh            = 820.0 g (gross 974 − tare 154)" in shown.stdout
    assert (
        "gcode_estimate   -100.0 g ±10" in shown.stdout
        and "job_id=plate-3" in shown.stdout
    )
    assert emptied.returncode == 0 and "[archived]" in emptied.stdout
    events = _cli(tmp_path, shop, "printops", "events", "--last", "50", "--json")
    kinds = [json.loads(line)["kind"] for line in events.stdout.splitlines()]
    kinds = [k for k in kinds if k.startswith(("spool.", "slot."))]
    assert kinds == [
        "spool.received",
        "spool.moved",
        "slot.loaded",
        "spool.used",
        "slot.unloaded",
        "spool.measured",
        "spool.empty",
    ]


def test_import_owned_dry_run_apply_rerun_and_lint(
    tmp_path: Path, shop: LiveApi
) -> None:
    """The user imports the spools babushkin-dom lists as owned: the dry run shows the plan and
    writes nothing, --apply creates them, a re-run changes nothing; lint then reports the
    never-weighed spools and the one without a tare."""
    plates = tmp_path / "babushkin-dom" / "gen" / "plates.json"
    plates.parent.mkdir(parents=True)
    plates.write_text(
        json.dumps(
            {
                "owned": {"белый": "#FFFFFF", "зелёный Elegoo": "#4FE44D"},
                "bought": {
                    "зелёный Elegoo": "Elegoo RAPID PLA+ Green (Game Centar, 1499 дин)"
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    dry = _cli(
        tmp_path,
        shop,
        "printops",
        "spool",
        "import-owned",
        str(plates),
        "--location",
        "WH:default",
    )
    writes_after_dry = [
        w for w in shop.spoolman.writes() if not w[1].startswith("/field")
    ]
    applied = _cli(
        tmp_path,
        shop,
        "printops",
        "spool",
        "import-owned",
        str(plates),
        "--location",
        "WH:default",
        "--apply",
    )
    rerun = _cli(
        tmp_path,
        shop,
        "printops",
        "spool",
        "import-owned",
        str(plates),
        "--location",
        "WH:default",
        "--apply",
    )
    lint = _cli(
        tmp_path,
        shop,
        "lint",
        "--printops",
        "--fleet",
        str(tmp_path / "fleet.yaml"),
        "--json",
    )
    tare_ls = _cli(tmp_path, shop, "printops", "tare", "ls", "elegoo")

    assert dry.returncode == 0, dry.stderr
    assert (
        "+ babushkin-dom:owned:белый: no vendor PLA 'белый' #FFFFFF (NEW filament) → WH:default"
        in dry.stdout
    )
    assert (
        "Elegoo (NEW vendor) PLA+ 'RAPID PLA+ Green' #4FE44D (NEW filament) → WH:default, price 1499"
        in dry.stdout
    )
    assert (
        "2 change(s), 0 already imported (DRY RUN" in dry.stdout
        and writes_after_dry == []
    )
    assert (
        applied.returncode == 0
        and "2 change(s)" in applied.stdout
        and "(applied)" in applied.stdout
    )
    assert (
        rerun.returncode == 0
        and "0 change(s), 2 already imported (applied)" in rerun.stdout
    )
    assert len(shop.spoolman.find("spool")) == 2

    report = json.loads(lint.stdout)
    rules = sorted(
        (f["rule"], f["subject"])
        for f in report["findings"]
        if f["rule"].startswith("spools/")
    )
    assert rules == [
        ("spools/no-tare", "spool:1"),
        ("spools/not-weighed", "spool:1"),
        ("spools/not-weighed", "spool:2"),
    ]
    assert lint.returncode == 0 and "2 spool(s)" in report["summary"]["data"]
    assert "ELEGOO" in tare_ls.stdout and "154 ± 10 g" in tare_ls.stdout


def test_lint_printops_without_api_skips_data_rules(tmp_path: Path) -> None:
    out = run_cli(
        tmp_path,
        "lint",
        "--printops",
        env_extra={"THREED_FLEET": str(REPO_FLEET), "PRINTOPS_API": ""},
    )
    assert out.returncode == 0, out.stdout + out.stderr
    assert "data rules: skipped (no 3d-api configured" in out.stdout
