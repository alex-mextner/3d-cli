from __future__ import annotations

import inspect
import json
import subprocess
import sys
from pathlib import Path

import pytest

from library import actions
from library import db as librarydb

NAMES = (
    "search",
    "rank",
    "download",
    "open_slicer",
    "launch_agent",
    "queue",
    "photo",
    "publish",
    "open_browser",
    "preview",
)


@pytest.fixture
def dbpath(tmp_path: Path) -> str:
    return str(tmp_path / "library.db")


def test_registry_has_exactly_the_ten_actions_with_owners() -> None:
    assert tuple(actions.ACTIONS) == NAMES
    assert set(actions.OWNERS) == set(NAMES)
    for name, fn in actions.ACTIONS.items():
        assert fn.__name__ == name and fn.__doc__
        sig = inspect.signature(fn)
        assert all(
            p.annotation is not inspect.Parameter.empty for p in sig.parameters.values()
        )
        assert sig.return_annotation in ("ActionResult", actions.ActionResult)


def test_actions_module_imports_without_heavy_deps_or_library_db() -> None:
    code = (
        "import sys; sys.modules['trimesh'] = None; sys.modules['numpy'] = None; sys.modules['cv2'] = None\n"
        "import library.actions\n"
        "assert 'library.db' not in sys.modules\n"
        "print('OK')\n"
    )
    lib = Path(__file__).resolve().parents[1] / "lib"
    r = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=lib,
        env={"PYTHONPATH": str(lib), "PATH": ""},
    )
    assert r.returncode == 0 and "OK" in r.stdout, r.stderr


@pytest.mark.parametrize(
    "name,owner,kwargs",
    [
        ("photo", 579, {"capture": True}),
        ("publish", 579, {"design": "d"}),
    ],
)
def test_unimplemented_actions_name_their_owning_issue(
    name: str, owner: int, kwargs: dict[str, object]
) -> None:
    with pytest.raises(actions.NotImplementedByDesign) as exc:
        actions.ACTIONS[name](**kwargs)
    assert exc.value.owner_issue == owner and f"#{owner}" in str(exc.value)
    assert actions.OWNERS[name] == owner
    res = actions.invoke(name, **kwargs)
    assert (
        res["schema"] == "3d.action/1" and res["ok"] is False and res["action"] == name
    )
    assert (
        res["errors"][0]["code"] == "not_implemented"
        and f"#{owner}" in res["errors"][0]["remediation"][0]
    )
    assert res["data"] == {} and res["warnings"] == []
    json.dumps(res)


def test_queue_creates_design_and_queued_make_idempotently(dbpath: str) -> None:
    res = actions.queue(
        "soil-sensor-case",
        printer="k1se",
        filament="PLA",
        db_path=dbpath,
        source_key="k1",
    )
    assert (
        res["ok"]
        and res["schema"] == "3d.action/1"
        and res["errors"] == []
        and res["warnings"] == []
    )
    assert res["data"]["status"] == "queued" and res["data"]["created"] is True
    again = actions.queue(
        "soil-sensor-case", printer="k1se", db_path=dbpath, source_key="k1"
    )
    assert (
        again["data"]["make"] == res["data"]["make"]
        and again["data"]["created"] is False
    )
    third = actions.queue(
        "soil-sensor-case", db_path=dbpath
    )  # a new make of the same design
    assert (
        third["data"]["design"] == res["data"]["design"]
        and third["data"]["make"] != res["data"]["make"]
    )
    conn = librarydb.open_library(dbpath)
    assert conn.execute("SELECT COUNT(*) FROM design").fetchone()[0] == 1
    assert [r["status"] for r in conn.execute("SELECT status FROM make")] == [
        "queued",
        "queued",
    ]


def test_queue_unknown_source_is_an_error_result(dbpath: str) -> None:
    res = actions.queue("d", source="0000", db_path=dbpath)
    assert res["ok"] is False and res["errors"][0]["code"] == "not_found"


def test_photo_registers_an_existing_file_for_a_make(
    dbpath: str, tmp_path: Path
) -> None:
    made = actions.queue("case", db_path=dbpath)["data"]["make"]
    img = tmp_path / "table.jpg"
    img.write_bytes(b"x")
    res = actions.photo(str(img), make=made, kind="table", z_mm=12.5, db_path=dbpath)
    assert res["ok"] and res["data"]["kind"] == "table"
    row = librarydb.open_library(dbpath).execute("SELECT * FROM photo").fetchone()
    assert (
        row["z_mm"] == 12.5 and row["make_id"] is not None and row["design_id"] is None
    )
    missing = actions.photo(str(img), make="nope", db_path=dbpath)
    assert missing["ok"] is False and missing["errors"][0]["code"] == "not_found"


def test_invoke_turns_bad_input_into_results_never_exceptions(dbpath: str) -> None:
    assert actions.invoke("nope")["errors"][0]["code"] == "unknown_action"
    assert (
        actions.invoke("photo", path="x.jpg", kind="selfie", make="m")["errors"][0][
            "code"
        ]
        == "invalid_argument"
    )
    assert (
        actions.invoke("photo", path="x.jpg", db_path=dbpath)["errors"][0]["code"]
        == "invalid_argument"
    )
    assert (
        actions.invoke("launch_agent", prompt="x", backend="vim")["errors"][0]["code"]
        == "invalid_argument"
    )
    assert actions.invoke("queue", bogus=1)["errors"][0]["code"] == "invalid_argument"
