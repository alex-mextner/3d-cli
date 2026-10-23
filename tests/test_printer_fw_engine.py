"""`fw apply`: dry run by default, every refusal, every executed step (fake ssh/HTTP, no printer)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from errors import InvalidArgument, MissingDependency, UsageError
from printer_fw.backup import take_backup
from printer_fw.engine import (
    ApplyOptions,
    ApplyResult,
    Context,
    IdleVerdict,
    adapter_idle,
    run_apply,
)
from printer_fw.plan import Unreachable
from printer_fw.profiles import Profile, load_profiles, parse_profile
from printer_fw.runner import CommandResult
from printer_fw_fakes import (
    FakeHttp,
    FakeRunner,
    FakeUpload,
    context,
    ep_of,
    k1_http,
    k1_runner,
    make_backends,
    minimal_profile_data,
    target,
)

NOW = 1_800_000_000.0
HOUR = 3600.0
URL = "https://example.com/docs"
CMD = "echo hello > /tmp/x"
PROFILES = load_profiles()


def demo_profile() -> Profile:
    d = minimal_profile_data()
    d["ssh"] = {"user": "root", "credentials": "key only"}
    d["idle_probe"] = {
        "kind": "http_get",
        "service": "web",
        "path": "/state",
        "json_path": "state",
        "regex": "^standby$",
    }
    d["backups"] = [
        {
            "id": "cfg",
            "description": "config",
            "via": "http_get",
            "service": "web",
            "path": "/cfg",
            "optional": True,
        }
    ]
    d["states"].append(
        {
            "id": "final",
            "title": "Final",
            "detection": {
                "all": [{"kind": "http_get", "service": "web", "path": "/final"}]
            },
        }
    )
    d["guards"] = [
        {
            "id": "no-flash-while-printing",
            "kind": "require_idle",
            "reason": "never flash while printing",
            "sources": [URL],
        },
        {
            "id": "no-bad-option",
            "kind": "forbidden_option",
            "options": ["bad-option"],
            "reason": "bad-option breaks the adapter",
            "sources": [URL],
        },
    ]
    first = d["transitions"][0]["steps"][0]
    first["verify"] = [{"kind": "http_get", "service": "web", "path": "/one"}]
    d["transitions"][0]["steps"].append(
        {
            **first,
            "id": "flip-two",
            "title": "Flip two",
            "instruction": "Press the second button.",
            "verify": [{"kind": "http_get", "service": "web", "path": "/custom"}],
        }
    )
    d["transitions"].append(
        {
            "id": "to-final",
            "from": "custom",
            "to": "final",
            "title": "Automatic",
            "steps": [
                {
                    "id": "run-script",
                    "title": "Run the script",
                    "kind": "ssh_script",
                    "automatable": True,
                    "risk": "medium",
                    "backup": ["cfg"],
                    "command": CMD,
                    "preconditions": [{"kind": "ssh_file_exists", "path": "/ready"}],
                    "verify": {
                        "wait_s": 10,
                        "interval_s": 1,
                        "probes": [
                            {"kind": "http_get", "service": "web", "path": "/final"}
                        ],
                    },
                    "rollback": {"instruction": "undo it by hand"},
                }
            ],
        }
    )
    d["transitions"].append(
        {
            "id": "maintain-final",
            "from": "final",
            "to": "final",
            "title": "Maintenance",
            "steps": [],
            "optional_steps": [
                {
                    "id": "send-file",
                    "title": "Send the firmware file",
                    "kind": "http_upload",
                    "automatable": True,
                    "risk": "high",
                    "upload": {
                        "service": "web",
                        "path": "/up",
                        "accept_suffix": ".bin",
                        "min_bytes": 4,
                        "success_regex": "SUCCESS",
                    },
                    "verify": {
                        "wait_s": 5,
                        "interval_s": 1,
                        "probes": [
                            {"kind": "http_get", "service": "web", "path": "/final"}
                        ],
                    },
                    "rollback": {"instruction": "flash the old file"},
                }
            ],
        }
    )
    return parse_profile(d, "demo.yaml")


DEMO = demo_profile()


class World:
    """A demo printer: HTTP routes + ssh rules; `at(state)` sets which state's evidence exists."""

    def __init__(self, tmp_path: Path, state: str = "custom") -> None:
        self.tmp_path = tmp_path
        self.http, self.runner, self.upload = FakeHttp(), FakeRunner(), FakeUpload()
        self.http.set("GET", 80, "/", 200, "root")
        self.http.set("GET", 80, "/state", 200, {"state": "standby"})
        self.http.set("GET", 80, "/cfg", 200, "config-data")
        self.runner.on("test -e /ready", rc=0)
        self.runner.on(
            "echo hello",
            rc=0,
            out="done\n",
            effect=lambda: self.http.set("GET", 80, "/final", 200, ""),
        )
        self.at(state)

    def at(self, state: str) -> World:
        for path in ("/one", "/custom", "/final"):
            self.http.routes.pop(("GET", 80, path), None)
        if state in ("custom", "final"):
            self.http.set("GET", 80, "/custom", 200, "")
        if state == "final":
            self.http.set("GET", 80, "/final", 200, "")
        return self

    def be(self, **kw: Any):  # noqa: ANN202
        return make_backends(self.runner, self.http, self.upload, **kw)[0]

    def backup(
        self,
        tgt_name: str = "demo",
        *,
        now: float = NOW,
        state: str = "custom",
        profile: Profile = DEMO,
    ) -> None:
        tgt = target(tgt_name, "Test Model")
        take_backup(
            profile,
            ep_of(profile, tgt),
            self.be(),
            printer=tgt_name,
            model=tgt.model,
            state=state,
            dest_root=self.tmp_path,
            now=now,
        )


def opts(tmp_path: Path, to: str = "final", **kw: Any) -> ApplyOptions:
    base: dict[str, Any] = {"to": to, "backup_dir": tmp_path, "now": NOW + HOUR}
    return ApplyOptions(**{**base, **kw})


def ctx_for(w: World, **kw: Any) -> Context:
    return context(DEMO, target("demo", "Test Model"), w.be(), **kw)


def messages(result: ApplyResult) -> str:
    return "\n".join(e.message for e in result.events)


def run(w: World, **kw: Any) -> ApplyResult:
    o = {k: kw.pop(k) for k in list(kw) if k in ApplyOptions.__dataclass_fields__}
    return run_apply(ctx_for(w, **kw), opts(w.tmp_path, **o))


# ---- dry run is the default ---------------------------------------------------------------------


def test_a_dry_run_prints_the_exact_command_and_sends_nothing(tmp_path: Path) -> None:
    w = World(tmp_path)
    r = run(w)
    assert (r.exit_code, r.result, r.from_state) == (0, "dry-run", "custom")
    text = messages(r)
    assert CMD in text and "DRY RUN" in text and "nothing was changed" in text
    assert (
        w.runner.mutating() == [] and w.http.mutations() == [] and w.upload.calls == []
    )
    assert w.runner.calls == []  # not even a precondition probe: reads of the plan only


def test_a_dry_run_says_what_is_missing_before_it_could_run(tmp_path: Path) -> None:
    w = World(tmp_path)
    r = run(w)
    gate = next(e for e in r.events if e.data.get("gate") == "backup")
    assert gate.data["ok"] is False and "MISSING" in gate.message
    assert next(e for e in r.events if e.kind == "dry-run").data["ready"] is False
    w.backup()
    ready = run(w)
    assert next(e for e in ready.events if e.kind == "dry-run").data["ready"] is True


def test_a_dry_run_of_a_plan_with_nothing_to_do_is_a_noop(tmp_path: Path) -> None:
    r = run(World(tmp_path, "final"), to="final")
    assert (r.exit_code, r.result) == (0, "noop")


def test_a_dry_run_needs_no_confirmation_and_works_on_a_read_only_printer(
    tmp_path: Path,
) -> None:
    w = World(tmp_path)
    tgt = target("demo", "Test Model", access="read-only")
    r = run_apply(context(DEMO, tgt, w.be()), opts(tmp_path))
    assert r.exit_code == 0 and r.result == "dry-run"


# ---- typed printer name -------------------------------------------------------------------------------


def test_yes_without_a_typed_name_is_a_usage_error_in_a_script(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.backup()
    with pytest.raises(UsageError, match="--confirm demo") as err:
        run(w, yes=True)
    assert err.value.exit_code == 2 and w.runner.mutating() == []


def test_a_wrong_name_is_refused_with_exit_2(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.backup()
    with pytest.raises(UsageError, match="not the printer's name"):
        run(w, yes=True, confirm="other")
    assert w.runner.mutating() == []


def test_the_name_is_prompted_when_interactive(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.backup()
    prompts: list[str] = []

    def right(q: str) -> str:
        prompts.append(q)
        return "demo"

    assert (
        run(w, yes=True, ask=right).exit_code == 0
        and "Type the printer name (demo)" in prompts[0]
    )
    with pytest.raises(UsageError, match="not the printer's name"):
        run(w.at("custom"), yes=True, ask=lambda q: "nope")


# ---- refusals --------------------------------------------------------------------------------------------


def test_an_offline_fleet_printer_is_refused_and_never_contacted(
    tmp_path: Path,
) -> None:
    w = World(tmp_path)
    r = run_apply(
        context(DEMO, target("demo", "Test Model", access="offline"), w.be()),
        opts(tmp_path, yes=True, confirm="demo"),
    )
    assert (r.exit_code, r.result) == (
        1,
        "refused",
    ) and "never contacts it" in messages(r)
    assert w.runner.calls == [] and w.http.requests == []


def test_a_read_only_printer_is_never_changed(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.backup()
    r = run_apply(
        context(DEMO, target("demo", "Test Model", access="read-only"), w.be()),
        opts(tmp_path, yes=True, confirm="demo"),
    )
    assert (r.exit_code, r.result) == (1, "refused") and "never change it" in messages(
        r
    )
    assert w.runner.mutating() == []


def test_no_backup_refuses(tmp_path: Path) -> None:
    w = World(tmp_path)
    r = run(w, yes=True, confirm="demo")
    assert (r.exit_code, r.result) == (1, "refused")
    assert "REFUSED: no fresh backup: no backup of demo found" in messages(
        r
    ) and "fw backup demo" in messages(r)
    assert w.runner.mutating() == []


def test_a_stale_backup_refuses_and_a_fresh_one_passes(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.backup(now=NOW)
    stale = run(w, yes=True, confirm="demo", now=NOW + 25 * HOUR)
    assert (
        stale.exit_code == 1
        and "25.0 h old" in messages(stale)
        and w.runner.mutating() == []
    )
    assert (
        run(
            w, yes=True, confirm="demo", now=NOW + 25 * HOUR, max_age_s=48 * HOUR
        ).exit_code
        == 0
    )


def test_a_printing_printer_is_refused_quoting_the_guard(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.backup()
    r = run(
        w,
        yes=True,
        confirm="demo",
        idle=IdleVerdict(False, "adapter", "printing (printing)"),
    )
    assert (r.exit_code, r.result) == (1, "refused")
    text = messages(r)
    assert (
        "not idle (printing (printing))" in text
        and "never flash while printing" in text
        and URL in text
    )
    assert w.runner.mutating() == []


def test_an_unreadable_printer_counts_as_busy(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.backup()
    r = run(
        w,
        yes=True,
        confirm="demo",
        idle=IdleVerdict(
            False,
            "none",
            "adapter status unreadable: timed out; the profile has no busy probe",
        ),
    )
    assert (
        r.exit_code == 1 and "unreadable" in messages(r) and w.runner.mutating() == []
    )


def test_a_forbidden_also_option_is_refused_with_its_reason(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.backup()
    r = run(w, yes=True, confirm="demo", also=("bad-option",))
    assert (r.exit_code, r.result) == (1, "refused")
    assert "guard no-bad-option" in messages(
        r
    ) and "bad-option breaks the adapter" in messages(r)
    assert w.runner.mutating() == []
    dry = run(w, also=("bad-option",))
    assert dry.exit_code == 1  # also in a dry run: the plan itself is invalid


def test_an_unknown_also_option_is_a_usage_error_listing_the_accepted_ones(
    tmp_path: Path,
) -> None:
    w = World(tmp_path)
    with pytest.raises(InvalidArgument) as err:
        run(w, also=("not-a-step",))
    assert err.value.exit_code == 2 and "send-file" in err.value.message


def test_an_unreachable_target_is_refused(tmp_path: Path) -> None:
    w = World(tmp_path)
    with pytest.raises(Unreachable) as err:
        run(w, to="stock", yes=True, confirm="demo")
    assert err.value.exit_code == 1 and w.runner.mutating() == []


def test_a_state_that_cannot_be_detected_is_refused(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.http.routes.clear()  # nothing answers
    r = run(w, yes=True, confirm="demo")
    assert (r.exit_code, r.result) == (1, "refused") and "not detected" in messages(r)


def test_a_failing_precondition_stops_before_the_command(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.backup()
    w.runner.rules.insert(0, ("test -e /ready", CommandResult(1), None))
    r = run(w, yes=True, confirm="demo")
    assert (r.exit_code, r.result) == (1, "failed")
    assert "precondition failed" in messages(r) and "/ready" in messages(r)
    assert w.runner.mutating() == []


def test_a_step_whose_command_fails_stops_and_prints_the_rollback(
    tmp_path: Path,
) -> None:
    w = World(tmp_path)
    w.backup()
    w.runner.rules.insert(
        0,
        (
            "echo hello",
            CommandResult(3, b"", b"boom"),
            None,
        ),
    )
    r = run(w, yes=True, confirm="demo")
    assert (r.exit_code, r.result) == (1, "failed")
    assert "the command exited 3" in messages(
        r
    ) and "ROLLBACK: undo it by hand" in messages(r)


def test_a_failed_verification_stops_and_prints_the_rollback(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.backup()
    w.runner.rules.clear()
    w.runner.on("test -e /ready", rc=0).on(
        "echo hello", rc=0
    )  # runs fine, but /final never appears
    r = run(w, yes=True, confirm="demo")
    assert (r.exit_code, r.result) == (1, "failed")
    assert "verification failed" in messages(
        r
    ) and "ROLLBACK: undo it by hand" in messages(r)


def test_a_later_step_that_needs_a_backup_item_the_backup_lacks_is_refused_at_that_step(
    tmp_path: Path,
) -> None:
    w = World(tmp_path, "stock")
    w.http.routes.pop(("GET", 80, "/cfg"))
    w.http.set(
        "GET", 80, "/cfg", 404, ""
    )  # optional item absent: the backup is fine but lacks `cfg`
    w.backup(state="stock")

    def press(_q: str) -> str:
        w.at("custom")
        w.http.set("GET", 80, "/one", 200, "")
        return ""

    r = run(w, yes=True, confirm="demo", ask=press)
    assert (r.exit_code, r.result) == (
        1,
        "refused",
    ) and "needs backup items cfg" in messages(r)
    assert w.runner.mutating() == []


# ---- executing -------------------------------------------------------------------------------------------


def test_yes_runs_exactly_the_documented_command_and_verifies(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.backup()
    r = run(w, yes=True, confirm="demo")
    assert (r.exit_code, r.result, r.from_state, r.final_state) == (
        0,
        "applied",
        "custom",
        "final",
    )
    assert w.runner.mutating() == [CMD]
    assert (
        w.runner.texts().count(CMD) == 1
        and "test -e /ready" in w.runner.texts()[w.runner.texts().index(CMD) - 1]
    )
    assert all(c.user == "root" and c.host == "10.0.0.5" for c in w.runner.calls)
    assert "verified:" in messages(
        r
    ) and "done: demo is now in state final" in messages(r)


def test_the_run_json_is_an_ordered_event_list(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.backup()
    doc = run(w, yes=True, confirm="demo").to_json()
    kinds = [e["kind"] for e in doc["events"]]  # type: ignore[attr-defined]  # JSON-shaped
    assert doc["result"] == "applied" and doc["exit_code"] == 0
    assert (
        kinds.index("start")
        < kinds.index("gate")
        < kinds.index("step")
        < kinds.index("verify")
        < kinds.index("done")
    )


def test_a_missing_ssh_binary_stops_before_anything_runs(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.backup()
    w.runner.available = False
    with pytest.raises(MissingDependency) as err:
        run(w, yes=True, confirm="demo")
    assert err.value.exit_code == 127 and w.runner.mutating() == []


def test_an_upload_step_sends_the_file_the_user_supplied(tmp_path: Path) -> None:
    w = World(tmp_path, "final")
    w.backup(state="final")
    fw = tmp_path / "update.bin"
    fw.write_bytes(b"firmware-bytes")
    r = run(
        w, to="final", also=("send-file",), default_file=fw, yes=True, confirm="demo"
    )
    assert (r.exit_code, r.result) == (0, "applied")
    assert w.upload.calls == [
        ("http://10.0.0.5:80/up", "update.bin", len(b"firmware-bytes"))
    ]
    assert "NOT in the profile's known list" in messages(r)


def test_an_upload_step_is_checked_before_anything_runs(tmp_path: Path) -> None:
    w = World(tmp_path, "final")
    w.backup(state="final")
    with pytest.raises(UsageError, match="--file send-file=PATH"):
        run(w, to="final", also=("send-file",), yes=True, confirm="demo")
    small = tmp_path / "x.bin"
    small.write_bytes(b"ab")
    with pytest.raises(InvalidArgument, match="at least 4 bytes"):
        run(
            w,
            to="final",
            also=("send-file",),
            default_file=small,
            yes=True,
            confirm="demo",
        )
    wrong = tmp_path / "x.img"
    wrong.write_bytes(b"abcdefgh")
    with pytest.raises(InvalidArgument, match="ending in .bin"):
        run(
            w,
            to="final",
            also=("send-file",),
            default_file=wrong,
            yes=True,
            confirm="demo",
        )
    assert w.upload.calls == []


def test_an_upload_the_printer_rejects_stops(tmp_path: Path) -> None:
    w = World(tmp_path, "final")
    w.backup(state="final")
    fw = tmp_path / "update.bin"
    fw.write_bytes(b"firmware-bytes")
    w.upload.body = "ERROR: Failed with exit code 1"
    r = run(
        w,
        to="final",
        also=("send-file",),
        files={"send-file": fw},
        yes=True,
        confirm="demo",
    )
    assert (r.exit_code, r.result) == (
        1,
        "failed",
    ) and "did not report success" in messages(r)
    assert "ROLLBACK: flash the old file" in messages(r)


def test_a_connection_dropped_by_the_rebooting_printer_is_left_to_the_verification(
    tmp_path: Path,
) -> None:
    w = World(tmp_path, "final")
    w.backup(state="final")
    fw = tmp_path / "update.bin"
    fw.write_bytes(b"firmware-bytes")
    w.upload.raises = ConnectionResetError("reset by peer")
    assert (
        run(
            w,
            to="final",
            also=("send-file",),
            default_file=fw,
            yes=True,
            confirm="demo",
        ).exit_code
        == 0
    )


# ---- manual steps: stop, resume, interactive ------------------------------------------------------------


def test_a_manual_step_stops_a_script_and_the_run_resumes_where_the_user_left_off(
    tmp_path: Path,
) -> None:
    w = World(tmp_path, "stock")
    w.backup(state="stock")
    first = run(w, to="custom", yes=True, confirm="demo")
    assert (first.exit_code, first.result) == (1, "pending-manual")
    assert "Press the button." in messages(
        first
    ) and "NEXT: do the step, then re-run the same command" in messages(first)

    w.http.set("GET", 80, "/one", 200, "")  # the user did step one
    second = run(w, to="custom", yes=True, confirm="demo")
    text = messages(second)
    assert (
        second.result == "pending-manual"
        and "already done: its verification probe holds" in text
    )
    assert "Press the second button." in text

    w.http.set("GET", 80, "/custom", 200, "")  # and step two
    third = run(w, to="custom", yes=True, confirm="demo")
    assert (third.exit_code, third.result) == (0, "noop")
    assert w.runner.mutating() == []


def test_interactive_manual_steps_wait_for_enter_then_verify(tmp_path: Path) -> None:
    w = World(tmp_path, "stock")
    w.backup(state="stock")
    todo = [("/one", None), ("/custom", None)]
    prompts: list[str] = []

    def enter(q: str) -> str:
        prompts.append(q)
        if q.startswith("Type the printer name"):
            return "demo"
        path, _ = todo.pop(0)
        w.http.set("GET", 80, path, 200, "")
        return ""

    r = run(w, to="custom", yes=True, ask=enter)
    assert (r.exit_code, r.result) == (0, "applied")
    assert sum("Press Enter when done" in p for p in prompts) == 2


def test_interactive_manual_step_keeps_asking_until_the_probe_holds_or_the_user_quits(
    tmp_path: Path,
) -> None:
    w = World(tmp_path, "stock")
    w.backup(state="stock")
    answers = iter(["demo", "", "q"])  # name, Enter without doing it, quit
    r = run(w, to="custom", yes=True, ask=lambda q: next(answers))
    assert (r.exit_code, r.result) == (1, "pending-manual")
    assert "not verified yet" in messages(r) and "stopped by the user" in messages(r)


# ---- shipped profiles through the engine -----------------------------------------------------------------


def k1_world(
    tmp_path: Path, *, drop_web_after_clone: bool = False
) -> tuple[FakeHttp, FakeRunner, Context]:
    http, runner = k1_http(), k1_runner()
    http.routes[("BANNER", 22, "")] = (200, "SSH-2.0-dropbear_2020.81")
    runner.on("test -d /usr/data", rc=0)

    def cloned() -> None:
        runner.rules.insert(
            0,
            (
                "test -e /usr/data/helper-script/helper.sh",
                CommandResult(0),
                None,
            ),
        )
        if drop_web_after_clone:
            http.drop(80)  # the stock web-server died
            http.set("GET", 4408, "/", 200, "")

    runner.on("git clone --depth 1", rc=0, out="cloned\n", effect=cloned)
    tgt = target("alpha", "Creality K1 SE", adapter="creality-stock", port=80)
    k1 = PROFILES["creality-k1"]
    be, *_ = make_backends(runner, http)
    take_backup(
        k1,
        ep_of(k1, tgt),
        be,
        printer="alpha",
        model=tgt.model,
        state="rooted",
        dest_root=tmp_path,
        now=NOW,
    )
    return http, runner, context(k1, tgt, be)


def test_k1_runs_the_runbook_script_verbatim(tmp_path: Path) -> None:
    http, runner, ctx = k1_world(tmp_path)
    step = ctx.profile.step("fetch-helper-script")
    assert step is not None
    r = run_apply(ctx, opts(tmp_path, "helper-installed", yes=True, confirm="alpha"))
    assert (r.exit_code, r.result) == (0, "applied")
    assert runner.mutating() == [step[1].command]
    assert (
        "git clone --depth 1 https://github.com/Guilouz/Creality-Helper-Script.git /usr/data/helper-script"
        in step[1].command
    )
    assert http.mutations() == []


def test_k1_dry_run_shows_the_script_and_changes_nothing(tmp_path: Path) -> None:
    http, runner, ctx = k1_world(tmp_path)
    r = run_apply(ctx, opts(tmp_path, "helper-installed"))
    assert r.result == "dry-run" and "git clone --depth 1" in messages(r)
    assert runner.mutating() == [] and http.mutations() == []


def test_k1_refuses_the_helper_menu_action_that_breaks_the_creality_adapter(
    tmp_path: Path,
) -> None:
    http, runner, ctx = k1_world(tmp_path)
    r = run_apply(
        ctx,
        opts(
            tmp_path,
            "klipper-ready",
            also=("remove-creality-web-interface",),
            yes=True,
            confirm="alpha",
        ),
    )
    text = messages(r)
    assert (r.exit_code, r.result) == (1, "refused")
    assert (
        "Remove Creality Web Interface" in text
        and "creality-stock adapter" in text
        and "k1se-root-upgrade.md" in text
    )
    assert runner.mutating() == []


def test_k1_stops_when_a_step_takes_down_the_stock_web_server(tmp_path: Path) -> None:
    http, runner, ctx = k1_world(tmp_path, drop_web_after_clone=True)
    r = run_apply(ctx, opts(tmp_path, "helper-installed", yes=True, confirm="alpha"))
    assert (r.exit_code, r.result) == (1, "failed")
    assert "guard keep-creality-web-server-up" in messages(
        r
    ) and "after step fetch-helper-script" in messages(r)


def test_k1_apply_waits_for_a_working_key_before_anything_else(tmp_path: Path) -> None:
    """Root is on but no key is installed: the plan starts with the manual key step, which stops a script."""
    http, runner = k1_http(), k1_runner()
    http.routes[("BANNER", 22, "")] = (200, "SSH-2.0-dropbear_2020.81")
    runner.on("test -d /usr/data", rc=255, err="Permission denied (publickey).")
    tgt = target("alpha", "Creality K1 SE", adapter="creality-stock", port=80)
    k1 = PROFILES["creality-k1"]
    be, *_ = make_backends(runner, http)
    take_backup(
        k1,
        ep_of(k1, tgt),
        be,
        printer="alpha",
        model=tgt.model,
        state="stock",
        dest_root=tmp_path,
        now=NOW,
    )
    r = run_apply(
        context(k1, tgt, be), opts(tmp_path, "rooted", yes=True, confirm="alpha")
    )
    assert (r.exit_code, r.result) == (1, "pending-manual")
    text = messages(r)
    assert (
        "already done" in text
        and "authorized_keys" in text
        and "NEXT: do the step" in text
    )
    assert runner.mutating() == []


def u1_world(tmp_path: Path) -> tuple[FakeHttp, Context]:
    http = FakeHttp()
    http.set("GET", 7125, "/machine/system_info", 200, {"result": {}})
    http.set(
        "GET",
        7125,
        "/server/config",
        200,
        {"result": {"config": {"authorization": {"force_logins": False}}}},
    )
    for path in ("printer.cfg", "xyz_offset_calibration.cfg"):
        http.set("GET", 7125, f"/server/files/config/{path}", 200, "cfg")
    http.set(
        "GET",
        80,
        "/firmware-config/api/status",
        200,
        {"version": {"title": "System Information"}},
    )

    def reboot() -> tuple[int, str]:
        http.routes.pop(("GET", 80, "/firmware-config/api/status"), None)
        http.set("GET", 80, "/", 200, "stock ui")
        return (
            200,
            "=== switch-to-backup-firmware ===\nSUCCESS: Completed successfully\n",
        )

    http.routes[
        ("POST", 80, "/firmware-config/api/action/switch-to-backup-firmware")
    ] = reboot
    u1 = PROFILES["snapmaker-u1"]
    tgt = target("alpha", "Snapmaker U1")
    be, *_ = make_backends(None, http)
    take_backup(
        u1,
        ep_of(u1, tgt),
        be,
        printer="alpha",
        model=tgt.model,
        state="extended",
        dest_root=tmp_path,
        now=NOW,
    )
    return http, context(u1, tgt, be)


def test_u1_reverts_to_stock_with_the_documented_post(tmp_path: Path) -> None:
    http, ctx = u1_world(tmp_path)
    r = run_apply(ctx, opts(tmp_path, "stock", yes=True, confirm="alpha"))
    assert (r.exit_code, r.result, r.final_state) == (0, "applied", "stock")
    assert http.mutations() == [
        (
            "POST",
            "http://10.0.0.5:80/firmware-config/api/action/switch-to-backup-firmware",
        )
    ]


def test_u1_dry_run_revert_posts_nothing(tmp_path: Path) -> None:
    http, ctx = u1_world(tmp_path)
    r = run_apply(ctx, opts(tmp_path, "stock"))
    assert (
        r.result == "dry-run"
        and "switch-to-backup-firmware" in messages(r)
        and http.mutations() == []
    )


def test_u1_refuses_when_force_logins_is_on(tmp_path: Path) -> None:
    http, ctx = u1_world(tmp_path)
    http.set(
        "GET",
        7125,
        "/server/config",
        200,
        {"result": {"config": {"authorization": {"force_logins": True}}}},
    )
    r = run_apply(ctx, opts(tmp_path, "stock", yes=True, confirm="alpha"))
    assert (r.exit_code, r.result) == (
        1,
        "failed",
    ) and "guard keep-force-logins-off" in messages(r)
    assert http.mutations() == []


# ---- idle check: adapter, then the profile's idle probe ---------------------------------------------------


def test_the_idle_probe_decides_when_no_adapter_can_be_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("THREED_FLEET", "")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    w = World(tmp_path)
    tgt = target("demo", "Test Model")
    be = w.be()
    ep = ep_of(DEMO, tgt)
    idle = adapter_idle(tgt, DEMO, ep, be)
    assert idle.idle is True and idle.source == "idle-probe"
    w.http.set("GET", 80, "/state", 200, {"state": "printing"})
    busy = adapter_idle(tgt, DEMO, ep, be)
    assert busy.idle is False and "not provably idle" in busy.detail
    w.http.set("GET", 80, "/state", 200, "not json")  # garbage is never read as idle
    assert adapter_idle(tgt, DEMO, ep, be).idle is False
    w.http.set("GET", 80, "/state", 500, "")
    assert adapter_idle(tgt, DEMO, ep, be).idle is False
    w.http.drop(80)
    unreadable = adapter_idle(tgt, DEMO, ep, be)
    assert unreadable.idle is False and "unreadable" in unreadable.detail


def test_without_an_idle_probe_an_unreadable_printer_is_busy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("THREED_FLEET", "")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    p = PROFILES["bambu-lab-p1-a1"]
    tgt = target("demo", "Bambu Lab P1S")
    v = adapter_idle(tgt, p, ep_of(p, tgt), make_backends()[0])
    assert v.idle is False and "no idle probe" in v.detail
