"""E2E: `3d printer todo` against a real uvicorn 3d-api (#281), and without it (the JSONL buffer).

The user sees the U1's broken T2 as a repair task that blocks the head, cannot tick it off with
`done` (it names the command that records the repair), adds a manual task with a dependency,
finishes it, imports yesterday's buffer (dry run first, twice = nothing new), and — with 3d-api
down — adds and finishes a task offline into the buffer, then imports it once 3d-api is back."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from printops_fixtures import LiveApi

from .workflow_helper import run_cli

FLEET = """\
u1:
  model: Snapmaker U1
  owner: own
  access: offline
  heads:
    - {label: T1, index: 0, nozzle: {diameter_mm: 0.4, material: stainless, type: std}}
    - label: T2
      index: 1
      nozzle: {diameter_mm: 0.4, material: stainless, type: std}
      usable: false
      reason: "head T2 does not work"
    - {label: T3, index: 2, nozzle: {diameter_mm: 0.4, material: stainless, type: std}}
k1se:
  model: Creality K1 SE
  owner: own
  access: offline
  heads:
    - {label: T1, index: 0, nozzle: {diameter_mm: 0.4, material: hardened, type: std}}
"""

T2 = "01a1414f-a5c7-7458-a04e-9d95ee795639"
T3 = "01a1414f-a5c7-7458-a04e-9d967eed2692"
BUFFER = [
    {
        "uid": T2,
        "schema": "printops.printer_task/1",
        "op": "open",
        "task_uid": T2,
        "printer": "u1",
        "head": "T2",
        "component": "head",
        "type": "repair",
        "title": "T2 head faulty — repair or replace",
        "blocks": {"scope": "head", "head": "T2", "slot": None},
        "when": "any",
        "needs_human": True,
        "source": "fault",
        "source_key": "fault:fleet:u1/T2",
        "depends_on": [],
        "refs": ["tg#22149"],
        "recorded_at": "2026-10-15T20:45:01Z",
        "actor": "cli:ultra",
    },
    {
        "uid": T3,
        "schema": "printops.printer_task/1",
        "op": "open",
        "task_uid": T3,
        "printer": "u1",
        "head": "T3",
        "slot": "T3",
        "type": "setup",
        "title": "Fix slot T3 filament label",
        "blocks": {"scope": "slot", "head": "T3", "slot": "T3"},
        "when": "any",
        "needs_human": True,
        "source": "manual",
        "source_key": None,
        "depends_on": [],
        "refs": [],
        "recorded_at": "2026-10-15T20:45:01Z",
        "actor": "cli:ultra",
    },
]


@pytest.fixture
def todo(tmp_path: Path, live_api: LiveApi, monkeypatch: pytest.MonkeyPatch) -> Any:
    fleet, rules = tmp_path / "printers.yaml", tmp_path / "maintenance-rules.yaml"
    fleet.write_text(FLEET, encoding="utf-8")
    rules.write_text("rules: []\n", encoding="utf-8")
    monkeypatch.setenv(
        "THREED_FLEET", str(fleet)
    )  # the live 3d-api thread reads it too
    monkeypatch.setenv("THREED_MAINTENANCE_RULES", str(rules))
    base = {"THREED_FLEET": str(fleet), "THREED_MAINTENANCE_RULES": str(rules)}
    online = {
        **base,
        "PRINTOPS_API": live_api.url,
        "PRINTOPS_TOKEN": live_api.tokens["rw"],
    }

    def cli(*argv: str, offline: bool = False) -> tuple[int, str, str]:
        env = {**base, "PRINTOPS_API": "", "PRINTOPS_TOKEN": ""} if offline else online
        r = run_cli(tmp_path, "printer", "todo", *argv, env_extra=env)
        return r.returncode, r.stdout, r.stderr

    return cli


def test_user_works_the_u1_list_and_imports_the_buffer(
    todo: Any, tmp_path: Path
) -> None:
    code, out, err = todo("list", "u1")
    assert code == 0, err
    assert out.startswith("u1: 1 open, 1 blocking")
    assert (
        "#1     OPEN      u1/T2      repair      Repair u1/T2: head T2 does not work"
        in out
    )
    assert (
        "BLOCKS head T2 · any time · human · from fault — closes with: 3d printops maint fault close 1"
        in out
    )

    code, out, err = todo("done", "1")
    assert code == 1
    assert (
        "derived from a fault" in err
        and "3d printops maint fault close 1 --fixed-by MAINT_ID" in err
    )

    code, out, err = todo(
        "add",
        "k1se",
        "--title",
        "X mechanics check",
        "--type",
        "inspect",
        "--when",
        "idle",
        "--duration",
        "20",
        "--component",
        "kinematics",
    )
    assert code == 0, err
    assert out.startswith(
        "added #2     OPEN      k1se       inspect     X mechanics check"
    )
    code, out, err = todo(
        "add",
        "k1se",
        "--title",
        "Cold SHAPER_CALIBRATE X",
        "--type",
        "calibration",
        "--depends-on",
        "2",
        "--no-human",
        "--when",
        "idle",
    )
    assert code == 0, err
    code, out, err = todo("list", "k1se")
    assert code == 0, err
    assert "#3     WAITING   k1se       calibration Cold SHAPER_CALIBRATE X" in out
    assert "when idle · no human needed · waits for #2" in out
    code, out, err = todo("dismiss", "3")
    assert code == 2 and "--reason is required" in err
    code, out, err = todo("done", "2", "--note", "feet tightened")
    assert code == 0, err
    assert out.startswith("done #2     DONE") and "note: feet tightened" in out
    code, out, err = todo("list", "k1se", "--json")
    assert code == 0, err
    data = json.loads(out)
    assert data["summary"]["open"] == 1 and data["tasks"][0]["waiting_on"] == []

    buffer = tmp_path / "u1.jsonl"
    buffer.write_text("".join(json.dumps(r) + "\n" for r in BUFFER), encoding="utf-8")
    code, out, err = todo("import", str(buffer), "--dry-run")
    assert code == 0, err
    assert out.startswith(
        f"DRY RUN (rolled back) {buffer}: 2 lines — 1 created, 1 merged"
    )
    assert "line 1   open        #1     merged" in out
    code, out, err = todo("import", str(buffer))
    assert code == 0, err
    assert (
        f"{buffer}: 2 lines — 1 created, 1 merged, 0 applied, 0 refused, 0 notices, 0 already imported"
        in out
    )
    code, out, err = todo("import", str(buffer), "--json")
    assert code == 0, err
    [rep] = json.loads(out)
    assert rep["added"] == 0 and rep["skipped"] == 2
    code, out, err = todo("list", "u1")
    assert code == 0, err
    assert (
        "u1/T3 slot T3" not in out
        and "Fix slot T3 filament label" in out
        and "BLOCKS slot T3" in out
    )


def test_without_3d_api_the_buffer_takes_the_operations_and_import_moves_them(
    todo: Any, tmp_path: Path
) -> None:
    code, out, err = todo(
        "add",
        "u1",
        "--title",
        "Refine camera height",
        "--type",
        "calibration",
        "--kinds",
        "camera_height",
        offline=True,
    )
    assert code == 0, err
    path = tmp_path / "xdg-data" / "3d-cli" / "printer-todo" / "u1.jsonl"
    assert f"3d-api is not configured: open recorded in the buffer {path}" in err
    [line] = path.read_text(encoding="utf-8").splitlines()
    rec = json.loads(line)
    assert rec["schema"] == "printops.printer_task/1" and rec["uid"] == rec["task_uid"]

    code, out, err = todo("list", offline=True)
    assert code == 0, err
    assert (
        "showing the buffer" in err
        and f"{rec['uid'][:8]} OPEN" in out
        and "Refine camera height" in out
    )
    code, out, err = todo("done", "1", offline=True)
    assert code == 2 and "name the task by its uid" in err
    code, out, err = todo(
        "done", rec["uid"][:13], "--note", "done by hand", offline=True
    )
    assert code == 0, err
    assert "done recorded in the buffer" in err
    assert len(path.read_text(encoding="utf-8").splitlines()) == 2

    code, out, err = todo("import", str(path))
    assert code == 0, err
    assert "2 lines — 1 created, 0 merged, 1 applied" in out
    code, out, err = todo("list", "u1", "--all", "--json")
    assert code == 0, err
    done = next(t for t in json.loads(out)["tasks"] if t["uid"] == rec["uid"])
    assert (done["status"], done["note"], done["source"]) == (
        "done",
        "done by hand",
        "import",
    )


def test_the_old_buffer_directory_moves_once_and_a_second_one_stops_with_import_commands(
    todo: Any, tmp_path: Path
) -> None:
    """The 2026-10-15/16 buffers sit in ~/.local/share/3d/printer-todo: the first offline `list`
    moves them (notice journal included) to ~/.local/share/3d-cli/printer-todo and says so; when
    both directories hold files the command stops and names the imports that merge them."""
    old = tmp_path / "xdg-data" / "3d" / "printer-todo"
    new = tmp_path / "xdg-data" / "3d-cli" / "printer-todo"
    old.mkdir(parents=True)
    (old / "u1.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in BUFFER), encoding="utf-8"
    )
    (old / "u1.notices.jsonl").write_text("", encoding="utf-8")

    code, out, err = todo("list", "u1", offline=True)
    assert code == 0, err
    assert f"moved the printer-todo buffers from {old} to {new}" in err
    assert f"showing the buffer {new}" in err and "Fix slot T3 filament label" in out
    assert (
        sorted(p.name for p in new.iterdir()) == ["u1.jsonl", "u1.notices.jsonl"]
        and not old.exists()
    )
    code, out, err = todo("list", "u1", offline=True)
    assert code == 0 and "moved" not in err

    code, out, err = todo("import", "--dry-run")
    assert code == 0, err
    assert (
        f"DRY RUN (rolled back) {new / 'u1.jsonl'}: 2 lines — 1 created, 1 merged"
        in out
    )

    old.mkdir(parents=True)
    (old / "k1se.jsonl").write_text("", encoding="utf-8")
    code, out, err = todo("import")
    assert code == 1
    assert f"printer-todo buffers exist in both {old} (old location) and {new}" in err
    assert (
        f"3d printer todo import {old / 'k1se.jsonl'}" in err
        and f"3d printer todo import {new / 'u1.jsonl'}" in err
    )
    assert (old / "k1se.jsonl").is_file() and (new / "u1.jsonl").is_file()
    before = (new / "u1.jsonl").read_bytes()
    code, out, err = todo("add", "u1", "--title", "t", offline=True)
    assert code == 1 and "exist in both" in err
    assert (new / "u1.jsonl").read_bytes() == before and (
        old / "k1se.jsonl"
    ).read_text() == ""
    assert f"mv {old} {old}.imported" in err and "rm -r" not in err
    # the remedy itself runs during the conflict: an explicit FILE never triggers the move
    code, out, err = todo("import", str(old / "k1se.jsonl"))
    assert code == 0, err
    assert f"{old / 'k1se.jsonl'}: 0 lines" in out
