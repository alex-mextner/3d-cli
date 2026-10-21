"""`3d printer todo …` (printops.todo_cli, #281): argument parsing, the online path against an
in-process 3d-api, and the offline JSONL buffer used while 3d-api is not configured or reachable."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fleet_fixtures import REPO_FLEET

from errors import InputNotFound, ThreeDError, UsageError
from printops import printer_task_buffer as buf
from printops import todo_cli
from printops.client import ApiClient

DEAD_API = "http://127.0.0.1:1"  # connection refused → ApiError status 0 (unreachable)


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Offline by default: no API URL anywhere, XDG dirs and fleet under tmp_path."""
    monkeypatch.setenv("THREED_FLEET", str(REPO_FLEET))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv("PRINTOPS_API", raising=False)
    monkeypatch.delenv("PRINTOPS_TOKEN", raising=False)
    return tmp_path


@pytest.fixture
def online(
    env: Path, monkeypatch: pytest.MonkeyPatch, api_client: ApiClient
) -> ApiClient:
    """The CLI talks to the in-process 3d-api (TestClient transport) with the rw token."""
    monkeypatch.setattr(
        ApiClient, "from_config", classmethod(lambda cls, **_kw: api_client)
    )
    return api_client


def _buffer_lines(printer: str) -> list[dict[str, Any]]:
    path = buf.buffer_path(printer)
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]


# -- run(): dispatch and usage -------------------------------------------------------------------
def test_run_without_arguments_prints_usage_and_fails(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert todo_cli.run([]) == 1
    assert "3d printer [-n PRINTER] todo|issues" in capsys.readouterr().out


@pytest.mark.parametrize("argv", [["--help"], ["help"], ["-h"], ["list", "--help"]])
def test_run_help_exits_zero(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert todo_cli.run(argv) == 0
    assert "todo|issues" in capsys.readouterr().out


def test_run_unknown_action_is_a_usage_error() -> None:
    with pytest.raises(UsageError, match="unknown todo action 'frobnicate'"):
        todo_cli.run(["frobnicate"])


def test_unknown_option_and_missing_value_are_usage_errors(env: Path) -> None:
    with pytest.raises(UsageError, match="unknown option --bogus"):
        todo_cli.run(["list", "--bogus"])
    with pytest.raises(UsageError, match="--title needs a value"):
        todo_cli.run(["add", "k1se", "--title"])


def test_list_alias_ls_behaves_like_list(
    env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert todo_cli.run(["ls", "k1se"]) == 0
    assert "no buffered tasks" in capsys.readouterr().out


# -- offline: add / list / close ---------------------------------------------------------------
def test_offline_add_writes_a_valid_buffer_line(
    env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = todo_cli.run(
        [
            "add",
            "k1se",
            "--title",
            "wipe the nozzle",
            "--why",
            "stringing",
            "--type",
            "maintenance",
            "--blocks",
            "head:T1",
            "--when",
            "idle",
            "--when-note",
            "after cooldown",
            "--duration",
            "15",
            "--ref",
            "wiki, manual",
            "--kinds",
            "flow",
            "--spool",
            "7",
            "--no-human",
            "--followup",
        ]
    )
    assert rc == 0
    (line,) = _buffer_lines("k1se")
    assert line["op"] == "open" and line["uid"] == line["task_uid"]
    assert line["printer_model"] == "Creality K1 SE"
    assert line["title"] == "wipe the nozzle" and line["type"] == "maintenance"
    assert line["blocks"] == {"scope": "head", "head": "T1", "slot": None}
    assert line["when"] == "idle" and line["duration_min"] == 15
    assert line["needs_human"] is False
    assert line["refs"] == ["wiki", "manual"] and line["calibration_kinds"] == ["flow"]
    assert line["source"] == "calibration_followup"
    assert line["spools"] == [{"name": None, "spool_id": 7}]
    err = capsys.readouterr().err
    assert "3d-api is not configured" in err and "todo import" in err


def test_offline_add_uses_the_named_printer_and_checks_it_agrees(env: Path) -> None:
    assert todo_cli.run(["add", "--title", "x"], printer="u1") == 0
    assert _buffer_lines("u1")[0]["printer"] == "u1"
    with pytest.raises(UsageError, match="two printers: -n u1 and k1se"):
        todo_cli.run(["add", "k1se", "--title", "x"], printer="u1")


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["add", "--title", "x"], "name the printer"),
        (["add", "k1se"], "--title is required"),
        (["add", "k1se", "--title", "x", "--blocks", "bogus"], "--blocks 'bogus'"),
        (["add", "k1se", "--title", "x", "--blocks", "printer:T1"], "--blocks"),
        (
            ["add", "k1se", "--title", "x", "--depends-on", "a"],
            "--depends-on needs task numbers",
        ),
        (
            ["add", "k1se", "--title", "x", "--depends-on", "3,4"],
            "takes task numbers from 3d-api",
        ),
        (["add", "nosuchprinter", "--title", "x"], "unknown printer 'nosuchprinter'"),
    ],
)
def test_offline_add_refusals_write_nothing(
    env: Path, argv: list[str], message: str
) -> None:
    with pytest.raises(UsageError, match=message):
        todo_cli.run(argv)
    assert not buf.buffer_dir().exists()


def test_offline_list_shows_buffered_tasks_by_short_uid(
    env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    todo_cli.run(
        ["add", "k1se", "--title", "first", "--why", "because", "--blocks", "printer"]
    )
    todo_cli.run(["add", "u1", "--title", "second", "--blocks", "slot:T3"])
    capsys.readouterr()
    assert todo_cli.run(["list"]) == 0
    cap = capsys.readouterr()
    assert "showing the buffer" in cap.err
    assert "k1se: 1 open, 1 blocking" in cap.out
    assert "u1: 1 open, 1 blocking" in cap.out
    assert "why: because" in cap.out
    assert "BLOCKS the printer" in cap.out and "BLOCKS slot T3" in cap.out
    uid = _buffer_lines("k1se")[0]["uid"]
    assert uid[:8] in cap.out

    assert todo_cli.run(["list", "u1"]) == 0
    only = capsys.readouterr().out
    assert "second" in only and "first" not in only


def test_offline_list_json(env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    todo_cli.run(["add", "k1se", "--title", "t"])
    capsys.readouterr()
    assert todo_cli.run(["list", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["offline"] is True and data["buffer"] == str(buf.buffer_dir())
    assert [t["title"] for t in data["tasks"]] == ["t"]


def test_offline_done_closes_by_uid_prefix_and_hides_it_from_list(
    env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    todo_cli.run(["add", "k1se", "--title", "t"])
    uid = _buffer_lines("k1se")[0]["uid"]
    capsys.readouterr()

    assert todo_cli.run(["done", uid[:8], "--note", "all good"]) == 0
    closing = _buffer_lines("k1se")[1]
    assert closing["op"] == "done" and closing["task_uid"] == uid
    assert closing["status"] == "done" and closing["note"] == "all good"
    assert closing["uid"] != uid

    capsys.readouterr()
    todo_cli.run(["list"])
    assert "no buffered tasks" in capsys.readouterr().out
    todo_cli.run(["list", "--all"])
    assert "DONE" in capsys.readouterr().out

    with pytest.raises(ThreeDError, match="already done"):
        todo_cli.run(["done", uid[:8]])


def test_offline_dismiss_requires_a_reason_and_records_it(env: Path) -> None:
    todo_cli.run(["add", "k1se", "--title", "t"])
    uid = _buffer_lines("k1se")[0]["uid"]
    with pytest.raises(UsageError, match="--reason is required"):
        todo_cli.run(["dismiss", uid[:8]])
    with pytest.raises(UsageError, match="--reason is required"):
        todo_cli.run(["dismiss", uid[:8], "--reason", "  "])
    assert todo_cli.run(["dismiss", uid[:8], "--reason", "not needed"]) == 0
    last = _buffer_lines("k1se")[-1]
    assert last["status"] == "dismissed" and last["reason"] == "not needed"


def test_offline_snooze_hides_the_task_until_it_expires(
    env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    todo_cli.run(["add", "k1se", "--title", "later"])
    uid = _buffer_lines("k1se")[0]["uid"]
    until = "2999-01-01T09:30:00Z"
    assert todo_cli.run(["snooze", uid, "--until", until, "--reason", "away"]) == 0
    last = _buffer_lines("k1se")[-1]
    assert last["status"] == "snoozed" and last["snoozed_until"] == until
    capsys.readouterr()
    todo_cli.run(["list", "k1se"])
    out = capsys.readouterr().out
    assert "1 snoozed" in out and "SNOOZED" in out
    assert "snoozed until 2999-01-01 09:30" in out

    # an expired snooze reads as open again
    todo_cli.run(["add", "k1se", "--title", "expired"])
    expired = _buffer_lines("k1se")[-1]["uid"]
    todo_cli.run(["snooze", expired, "--until", "2000-01-01T00:00:00Z"])
    capsys.readouterr()
    todo_cli.run(["list", "k1se", "--json"])
    tasks = {t["title"]: t for t in json.loads(capsys.readouterr().out)["tasks"]}
    assert tasks["expired"]["status"] == "open"
    assert tasks["expired"]["snoozed_until"] is None
    assert tasks["later"]["status"] == "snoozed"


def test_offline_close_refusals(env: Path) -> None:
    with pytest.raises(UsageError, match="task numbers live there"):
        todo_cli.run(["done", "12"])
    with pytest.raises(UsageError, match="no buffered task with uid 'abc'"):
        todo_cli.run(["done", "abc"])
    with pytest.raises(ThreeDError, match="reopen needs it"):
        todo_cli.run(["reopen", "abc"])
    todo_cli.run(["add", "k1se", "--title", "t"])
    uid = _buffer_lines("k1se")[0]["uid"]
    with pytest.raises(UsageError, match="is on k1se, not u1"):
        todo_cli.run(["done", uid[:8]], printer="u1")
    todo_cli.run(["done", uid[:8]], printer="k1se")
    with pytest.raises(UsageError, match="takes 1 positional"):
        todo_cli.run(["done"])


def test_offline_done_of_a_derived_task_points_at_the_work(env: Path) -> None:
    uid = buf.uuid7()
    now = buf.utc_now()
    line = {
        "uid": uid,
        "schema": buf.TASK_SCHEMA,
        "op": "open",
        "task_uid": uid,
        "printer": "k1se",
        "printer_model": "Creality K1 SE",
        "type": "calibration",
        "title": "calibrate flow",
        "when": "any",
        "needs_human": True,
        "calibration_kinds": ["flow"],
        "refs": [],
        "spools": [],
        "blocks": {"scope": "none", "head": None, "slot": None},
        "source": "calibration",
        "source_key": "flow",
        "depends_on": [],
        "status": "open",
        "at": now,
        "recorded_at": now,
        "actor": "t",
    }
    buf.append("k1se", line)
    with pytest.raises(ThreeDError, match="derived from a calibration") as exc:
        todo_cli.run(["done", uid[:8]])
    assert "dismiss ID --reason" in " ".join(exc.value.remediation or [])
    # dismissing is allowed
    assert todo_cli.run(["dismiss", uid[:8], "--reason", "skip"]) == 0


def test_offline_buffer_with_a_torn_line_is_reported_not_skipped(env: Path) -> None:
    path = buf.buffer_path("k1se")
    path.parent.mkdir(parents=True)
    path.write_text("{not json\n", encoding="utf-8")
    with pytest.raises(ThreeDError, match="k1se.jsonl: line 1") as exc:
        todo_cli.run(["list", "k1se"])
    assert exc.value.remediation


def test_offline_waiting_on_open_dependency_is_rendered(
    env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    first, second = buf.uuid7(), buf.uuid7()
    now = buf.utc_now()
    for uid, deps, title in ((first, [], "first"), (second, [first], "second")):
        buf.append(
            "k1se",
            {
                "uid": uid,
                "schema": buf.TASK_SCHEMA,
                "op": "open",
                "task_uid": uid,
                "printer": "k1se",
                "type": "other",
                "title": title,
                "depends_on": deps,
                "status": "open",
                "at": now,
                "recorded_at": now,
                "actor": "t",
            },
        )
    assert todo_cli.run(["list", "k1se"]) == 0
    out = capsys.readouterr().out
    assert "2 open (1 waiting)" in out and "WAITING" in out


def test_legacy_buffer_directory_is_moved_once(
    env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    old = buf.legacy_buffer_dir()
    old.mkdir(parents=True)
    uid = buf.uuid7()
    now = buf.utc_now()
    (old / "k1se.jsonl").write_text(
        json.dumps(
            {
                "uid": uid,
                "schema": buf.TASK_SCHEMA,
                "op": "open",
                "task_uid": uid,
                "printer": "k1se",
                "type": "other",
                "title": "legacy task",
                "status": "open",
                "at": now,
                "recorded_at": now,
                "actor": "t",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    assert todo_cli.run(["list"]) == 0
    cap = capsys.readouterr()
    assert "one-time move" in cap.err and "legacy task" in cap.out
    assert not old.exists() and (buf.buffer_dir() / "k1se.jsonl").is_file()


def test_legacy_and_new_buffers_both_populated_is_a_conflict(env: Path) -> None:
    for directory in (buf.legacy_buffer_dir(), buf.buffer_dir()):
        directory.mkdir(parents=True)
        (directory / "k1se.jsonl").write_text("", encoding="utf-8")
    with pytest.raises(ThreeDError, match="both") as exc:
        todo_cli.run(["list"])
    assert any("todo import" in r for r in exc.value.remediation or [])
    assert any(".imported" in r for r in exc.value.remediation or [])


def test_unreachable_api_falls_back_to_the_buffer(
    env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert todo_cli.run(["add", "k1se", "--title", "t", "--api", DEAD_API]) == 0
    assert "is not reachable" in capsys.readouterr().err
    assert len(_buffer_lines("k1se")) == 1
    assert todo_cli.run(["list", "--api", DEAD_API]) == 0
    assert "is not reachable" in capsys.readouterr().err
    uid = _buffer_lines("k1se")[0]["uid"]
    assert todo_cli.run(["done", uid[:8], "--api", DEAD_API]) == 0
    assert _buffer_lines("k1se")[-1]["op"] == "done"
    with pytest.raises(ThreeDError, match="reopen needs it"):
        todo_cli.run(["reopen", uid[:8], "--api", DEAD_API])
    with pytest.raises(UsageError, match="not reachable"):
        todo_cli.run(
            ["add", "k1se", "--title", "t", "--depends-on", "1", "--api", DEAD_API]
        )


# -- online: against an in-process 3d-api -------------------------------------------------------
def test_online_add_list_and_json(
    online: ApiClient, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        todo_cli.run(
            [
                "add",
                "k1se",
                "--title",
                "swap the nozzle",
                "--why",
                "worn",
                "--type",
                "maintenance",
                "--duration",
                "20",
                "--when",
                "before_print",
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert out.startswith("added #") and "swap the nozzle" in out
    assert "before a print" in out and "20 min" in out

    assert todo_cli.run(["list", "k1se"]) == 0
    listing = capsys.readouterr().out
    assert "swap the nozzle" in listing and "why: worn" in listing
    assert "k1se: " in listing

    assert todo_cli.run(["list", "k1se", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    mine = [t for t in data["tasks"] if t["title"] == "swap the nozzle"]
    assert len(mine) == 1 and mine[0]["duration_min"] == 20

    assert todo_cli.run(["list"]) == 0
    assert "swap the nozzle" in capsys.readouterr().out


def test_online_list_of_an_empty_printer_says_so(
    online: ApiClient, capsys: pytest.CaptureFixture[str]
) -> None:
    assert todo_cli.run(["list", "p1s"]) == 0
    assert "no open tasks on p1s" in capsys.readouterr().out
    assert todo_cli.run(["list", "p1s", "--all"]) == 0
    assert "no tasks on p1s" in capsys.readouterr().out


def test_online_add_json_and_dependencies(
    online: ApiClient, capsys: pytest.CaptureFixture[str]
) -> None:
    todo_cli.run(["add", "k1se", "--title", "one", "--json"])
    first = json.loads(capsys.readouterr().out)
    todo_cli.run(
        ["add", "k1se", "--title", "two", "--depends-on", str(first["id"]), "--json"]
    )
    second = json.loads(capsys.readouterr().out)
    assert second["depends_on"] == [first["id"]]
    todo_cli.run(["list", "k1se"])
    out = capsys.readouterr().out
    assert f"waits for #{first['id']}" in out and "WAITING" in out


def test_online_done_dismiss_snooze_reopen_lifecycle(
    online: ApiClient, capsys: pytest.CaptureFixture[str]
) -> None:
    ids = []
    for title in ("a", "b", "c"):
        todo_cli.run(["add", "k1se", "--title", title, "--json"])
        ids.append(json.loads(capsys.readouterr().out)["id"])
    a, b, c = ids

    assert todo_cli.run(["done", str(a), "--note", "finished"]) == 0
    out = capsys.readouterr().out
    assert out.startswith(f"done #{a}") and "note: finished" in out

    assert todo_cli.run(["dismiss", str(b), "--reason", "not needed"]) == 0
    assert capsys.readouterr().out.startswith(f"dismissed #{b}")

    until = "2999-02-03T04:05:00Z"
    assert todo_cli.run(["snooze", str(c), "--until", until, "--json"]) == 0
    snoozed = json.loads(capsys.readouterr().out)
    assert snoozed["status"] == "snoozed"

    assert todo_cli.run(["reopen", str(a)]) == 0
    assert capsys.readouterr().out.startswith(f"reopened #{a}")
    assert todo_cli.run(["list", "k1se", "--json"]) == 0
    open_titles = {t["title"] for t in json.loads(capsys.readouterr().out)["tasks"]}
    assert open_titles == {"a", "c"}  # c is snoozed: still active, b is dismissed


def test_online_errors_become_structured_cli_errors(online: ApiClient) -> None:
    with pytest.raises(ThreeDError):
        todo_cli.run(["done", "99999"])
    with pytest.raises(ThreeDError):
        todo_cli.run(["list", "nosuchprinter"])


def test_online_named_printer_must_own_the_task(
    online: ApiClient, capsys: pytest.CaptureFixture[str]
) -> None:
    todo_cli.run(["add", "k1se", "--title", "t", "--json"])
    tid = json.loads(capsys.readouterr().out)["id"]
    with pytest.raises(UsageError, match="is on k1se, not u1"):
        todo_cli.run(["done", str(tid)], printer="u1")
    assert todo_cli.run(["done", str(tid)], printer="k1se") == 0
    assert capsys.readouterr().out.startswith(f"done #{tid}")


def test_named_printer_check_needs_the_read_scope(
    env: Path,
    monkeypatch: pytest.MonkeyPatch,
    printops_client: Any,
    printops_tokens: dict[str, str],
) -> None:
    from printops_fixtures import TestClientTransport

    write_only = ApiClient(
        "http://testserver",
        printops_tokens["write"],
        TestClientTransport(printops_client),
    )
    monkeypatch.setattr(
        ApiClient, "from_config", classmethod(lambda cls, **_kw: write_only)
    )
    with pytest.raises(UsageError, match="needs a token with the read scope") as exc:
        todo_cli.run(["done", "1"], printer="k1se")
    assert "Drop -n" in " ".join(exc.value.remediation or [])


# -- import ----------------------------------------------------------------------------------------
def test_import_replays_buffer_into_3d_api_and_is_idempotent(
    online: ApiClient,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # write the buffer while 3d-api is "unreachable"
    monkeypatch.setattr(
        ApiClient,
        "from_config",
        classmethod(
            lambda cls, **_kw: (_ for _ in ()).throw(todo_cli.ApiConfigError("x"))
        ),
    )
    todo_cli.run(["add", "k1se", "--title", "buffered one"])
    todo_cli.run(["add", "u1", "--title", "buffered two"])
    capsys.readouterr()
    monkeypatch.setattr(
        ApiClient, "from_config", classmethod(lambda cls, **_kw: online)
    )

    assert todo_cli.run(["import", "--dry-run"]) == 0
    dry = capsys.readouterr().out
    assert "DRY RUN (rolled back)" in dry and "1 created" in dry

    assert todo_cli.run(["import"]) == 0
    first = capsys.readouterr().out
    assert "k1se.jsonl" in first and "u1.jsonl" in first
    assert "buffered one" in first and "buffered two" in first

    assert todo_cli.run(["import"]) == 0
    again = capsys.readouterr().out
    assert "already imported" in again
    assert "0 created" in again

    todo_cli.run(["list", "k1se", "--json"])
    titles = [t["title"] for t in json.loads(capsys.readouterr().out)["tasks"]]
    assert titles == ["buffered one"]


def test_import_of_one_printers_buffer_and_of_explicit_files(
    online: ApiClient,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    uid = buf.uuid7()
    now = buf.utc_now()
    record = {
        "uid": uid,
        "schema": buf.TASK_SCHEMA,
        "op": "open",
        "task_uid": uid,
        "printer": "k1se",
        "printer_model": "Creality K1 SE",
        "type": "other",
        "title": "from a file",
        "status": "open",
        "at": now,
        "recorded_at": now,
        "actor": "t",
    }
    buf.append("k1se", record)
    assert todo_cli.run(["import"], printer="k1se") == 0
    assert "from a file" in capsys.readouterr().out

    explicit = tmp_path / "explicit.jsonl"
    explicit.write_text(json.dumps(record) + "\n", encoding="utf-8")
    assert todo_cli.run(["import", str(explicit), "--json"]) == 0
    reports = json.loads(capsys.readouterr().out)
    assert reports[0]["file"] == str(explicit) and reports[0]["skipped"] == 1

    with pytest.raises(UsageError, match="a buffer file names its own printer"):
        todo_cli.run(["import", str(explicit)], printer="k1se")
    with pytest.raises(InputNotFound):
        todo_cli.run(["import", str(tmp_path / "missing.jsonl")])
    with pytest.raises(InputNotFound):
        todo_cli.run(["import"], printer="u1")


def test_import_without_buffer_or_api(env: Path, online: ApiClient) -> None:
    with pytest.raises(InputNotFound):
        todo_cli.run(["import"])


def test_import_without_api_configured_is_a_usage_error(env: Path) -> None:
    with pytest.raises(UsageError, match="no 3d-api URL"):
        todo_cli.run(["import"])


def test_offline_list_renders_note_and_source_details(
    env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    todo_cli.run(
        [
            "add",
            "k1se",
            "--title",
            "t",
            "--when",
            "idle",
            "--when-note",
            "after cooldown",
            "--followup",
        ]
    )
    capsys.readouterr()
    todo_cli.run(["list", "k1se"])
    out = capsys.readouterr().out
    assert "when idle (after cooldown)" in out
    assert "human" in out and "from calibration followup" in out


def test_offline_add_works_without_a_fleet_file(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("THREED_FLEET", str(env / "missing-fleet.yaml"))
    assert todo_cli.run(["add", "anything", "--title", "t"]) == 0
    assert _buffer_lines("anything")[0]["printer_model"] is None


def test_buffer_actor_falls_back_when_the_user_is_unknown(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_user() -> str:
        raise KeyError("uid not found")

    monkeypatch.setattr(todo_cli.getpass, "getuser", no_user)
    todo_cli.run(["add", "k1se", "--title", "t"])
    assert _buffer_lines("k1se")[0]["actor"] == "cli"


def test_online_rejected_add_is_an_error_not_a_buffered_task(
    online: ApiClient,
) -> None:
    with pytest.raises(ThreeDError):
        todo_cli.run(["add", "k1se", "--title", "t", "--type", "bogus"])
    assert not buf.buffer_dir().exists()


def test_online_named_printer_check_passes_other_api_errors_on(
    online: ApiClient,
) -> None:
    with pytest.raises(ThreeDError, match="404|unknown|no task"):
        todo_cli.run(["done", "99999"], printer="k1se")
