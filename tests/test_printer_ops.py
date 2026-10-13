"""Unit tests for `3d printer` / `3d print-preflight` (lib/printer_ops, lib/commands/printer.py).

Every printer here is an in-process loopback fake (tests/printer_fakes.py); the tests prove
both the protocol frames and that refused operations send NOTHING to the printer.
Issues: https://github.com/alex-mextner/3d-cli/issues/90 (epic), #91-#96, #99.
"""
from __future__ import annotations

import json
import socket
import sys
import types
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from errors import InputNotFound, InvalidArgument, UsageError
from printer_fakes import FakeCreality, FakeMoonraker, k1_state, u1_status, write_devices
from printer_ops import base as pbase
from printer_ops import creality, devices, moonraker, preflight, ws
from printer_ops.base import PrinterCommError, PrinterRefused, PrinterStatus, safe_remote_name

U1_GCODE = """; HEADER_BLOCK_START
; HEADER_BLOCK_END
G28
T0
G1 X10 Y10 E1
T2
G1 X20 Y20 E1
T3
G1 X30 Y30 E1
; filament used [mm] = 1828.27, 0.00, 706.80, 1835.80
; extruder_colour = #000000;#808080;#1B5E20;#FFFFFF
; filament_colour = #000000;#808080;#1B5E20;#FFFFFF
; filament_type = PLA;PLA;PLA;PLA
; thumbnails = 48x48/PNG, 300x300/PNG
"""


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    cfg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(cfg))
    monkeypatch.chdir(tmp_path)
    return cfg


@pytest.fixture()
def moon(env: Path) -> Iterator[FakeMoonraker]:
    fake = FakeMoonraker()
    with fake.serve():
        yield fake


@pytest.fixture()
def k1(env: Path) -> Iterator[FakeCreality]:
    fake = FakeCreality()
    with fake.serve():
        yield fake


@pytest.fixture()
def thumbs_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """Command tests check printer logic, not thumbnails: pin the thumbnail check to PASS."""
    monkeypatch.setattr(preflight, "check_thumbnails", lambda p: preflight.Check("thumbnails", "PASS", "stub"))


def gcode(tmp_path: Path, text: str = U1_GCODE, name: str = "part.gcode") -> Path:
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


def printer(*argv: str) -> int:
    from commands.printer import run

    return run(list(argv))


# ---- ws.py ------------------------------------------------------------------------------

@pytest.mark.parametrize("n", [0, 5, 125, 126, 300, 70000])
def test_ws_frames_roundtrip_for_every_length_encoding(n: int) -> None:
    payload = bytes(range(256)) * (n // 256) + bytes(range(n % 256))
    for mask in (True, False):
        frame = ws.encode_frame(ws.OP_TEXT, payload, mask=mask)
        parsed = ws.parse_frame(frame + b"rest")
        assert parsed is not None
        fin, op, data, used = parsed
        assert (fin, op, data) == (True, ws.OP_TEXT, payload)
        assert used == len(frame)


def test_ws_parse_frame_waits_for_complete_frame() -> None:
    frame = ws.encode_frame(ws.OP_TEXT, b"x" * 200, mask=True)
    for cut in (1, 3, 7, len(frame) - 1):
        assert ws.parse_frame(frame[:cut]) is None


def test_ws_recv_timeout_keeps_partial_frame_buffered() -> None:
    a, b = socket.socketpair()
    try:
        client = ws.WebSocket(a)
        frame = ws.encode_frame(ws.OP_TEXT, b'{"state": 1}', mask=False)
        b.sendall(frame[:5])
        assert client.recv(0.05) is None
        b.sendall(frame[5:])
        assert client.recv(1.0) == '{"state": 1}'
    finally:
        a.close()
        b.close()


# ---- devices.py -------------------------------------------------------------------------

def test_devices_user_file_and_project_override(env: Path, tmp_path: Path) -> None:
    (env / "3d-cli").mkdir(parents=True)
    (env / "3d-cli" / "printer-devices.yaml").write_text(
        "u1: {adapter: moonraker, host: 192.168.0.26, model: Snapmaker U1}\n"
        "k1se: {adapter: creality-stock, host: 192.168.0.27}\n", encoding="utf-8")
    (tmp_path / "printer-devices.yaml").write_text("k1se: {adapter: creality-stock, host: 10.0.0.9, ws_port: 9998}\n",
                                                   encoding="utf-8")
    d = devices.load_devices()
    assert (d["u1"].host, d["u1"].port, d["u1"].model) == ("192.168.0.26", 7125, "Snapmaker U1")
    assert (d["k1se"].host, d["k1se"].port, d["k1se"].ws_port) == ("10.0.0.9", 80, 9998)
    assert d["k1se"].source == tmp_path / "printer-devices.yaml"


@pytest.mark.parametrize("text, exc", [
    ("u1: {adapter: octoprint, host: h}\n", InvalidArgument),
    ("u1: {adapter: moonraker}\n", devices.DeviceConfigError),
    ("u1: {adapter: moonraker, host: h, port: 'x'}\n", InvalidArgument),
    ("- just a list\n", devices.DeviceConfigError),
    ("u1: [1, 2]\n", devices.DeviceConfigError),
])
def test_devices_invalid_entries_fail_with_exit_2(env: Path, text: str, exc: type[Exception]) -> None:
    (env / "3d-cli").mkdir(parents=True)
    (env / "3d-cli" / "printer-devices.yaml").write_text(text, encoding="utf-8")
    with pytest.raises(exc) as info:
        devices.load_devices()
    assert getattr(info.value, "exit_code") == 2


def test_devices_none_configured_gives_example(env: Path) -> None:
    with pytest.raises(UsageError) as info:
        devices.get_device("u1")
    assert "adapter: moonraker" in info.value.render(color=False)


def test_devices_unknown_name_lists_configured(moon: FakeMoonraker, env: Path) -> None:
    write_devices(env, moon=moon)
    with pytest.raises(InvalidArgument) as info:
        devices.get_device("k2")
    assert "u1" in info.value.message


# ---- moonraker.py -----------------------------------------------------------------------

@pytest.mark.parametrize("raw, norm", [
    ("standby", "idle"), ("printing", "printing"), ("paused", "paused"),
    ("complete", "complete"), ("cancelled", "cancelled"), ("error", "error"), ("weird", "unknown"),
])
def test_moonraker_state_mapping(raw: str, norm: str) -> None:
    st = moonraker.parse_status(u1_status(raw, "a.gcode", 0.5))
    assert (st.state, st.raw_state, st.filename, st.progress) == (norm, raw, "a.gcode", 0.5)


def test_moonraker_klippy_not_ready_is_unknown_and_busy() -> None:
    s = u1_status()
    s["webhooks"] = {"state": "shutdown", "state_message": "MCU 'mcu' shutdown"}
    st = moonraker.parse_status(s)
    assert st.state == "unknown" and st.busy and st.raw_state == "klippy:shutdown"
    assert "MCU" in (st.message or "")


def test_moonraker_u1_slots_mark_broken_head_empty() -> None:
    st = moonraker.parse_status(u1_status())
    assert [(s.label, s.present, s.color) for s in st.slots] == [
        ("T1", True, "#000000"), ("T2", False, "#808080"), ("T3", True, "#1B5E20"), ("T4", True, "#FFFFFF")]
    assert st.temps["bed"] == (30.0, 0.0) and "extruder3" in st.temps


def test_moonraker_non_u1_printer_has_no_slots() -> None:
    s = u1_status()
    del s["print_task_config"]
    assert moonraker.parse_status(s).slots == []


def test_moonraker_upload_sends_print_false_and_verifies_size(moon: FakeMoonraker, tmp_path: Path) -> None:
    a = moonraker.MoonrakerAdapter("u1", "127.0.0.1", moon.port)
    g = gcode(tmp_path)
    res = a.upload(g, "part.gcode")
    assert res.verified and res.size == g.stat().st_size
    up = [r for r in moon.writes() if r.path == "/server/files/upload"]
    assert len(up) == 1 and up[0].fields["print"] == b"false" and up[0].fields["root"] == b"gcodes"
    assert up[0].fields["file"] == g.read_bytes()
    assert not [r for r in moon.writes() if r.path.startswith("/printer/print")]


def test_moonraker_upload_reply_claiming_start_is_an_error(moon: FakeMoonraker, tmp_path: Path) -> None:
    moon.upload_reply = {"result": {"item": {"path": "part.gcode"}, "print_started": True}}
    with pytest.raises(PrinterCommError, match="STARTED or QUEUED"):
        moonraker.MoonrakerAdapter("u1", "127.0.0.1", moon.port).upload(gcode(tmp_path), "part.gcode")


def test_moonraker_upload_size_mismatch_fails(moon: FakeMoonraker, tmp_path: Path) -> None:
    moon.stored_size_delta = 3
    with pytest.raises(PrinterCommError, match="size mismatch"):
        moonraker.MoonrakerAdapter("u1", "127.0.0.1", moon.port).upload(gcode(tmp_path), "part.gcode")


def test_moonraker_upload_timeout_reconciles_by_listing(moon: FakeMoonraker, tmp_path: Path,
                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    g = gcode(tmp_path)
    a = moonraker.MoonrakerAdapter("u1", "127.0.0.1", moon.port)

    def timed_out(*args: Any, **kw: Any) -> tuple[int, bytes]:
        moon.files["part.gcode"] = g.stat().st_size  # the file did arrive
        raise TimeoutError("timed out")

    monkeypatch.setattr(moonraker, "http_upload", timed_out)
    res = a.upload(g, "part.gcode")
    assert res.verified and "timed out" in res.note

    def lost(*args: Any, **kw: Any) -> tuple[int, bytes]:
        raise TimeoutError("timed out")  # and the file never arrived

    moon.files.clear()
    monkeypatch.setattr(moonraker, "http_upload", lost)
    with pytest.raises(PrinterCommError, match="UNKNOWN"):
        a.upload(g, "part.gcode")


def test_moonraker_start_and_cancel_hit_their_endpoints(moon: FakeMoonraker) -> None:
    a = moonraker.MoonrakerAdapter("u1", "127.0.0.1", moon.port)
    a.start("my part.gcode")
    a.cancel()
    assert [(r.path, r.query) for r in moon.writes()] == [
        ("/printer/print/start", {"filename": ["my part.gcode"]}), ("/printer/print/cancel", {})]


def test_moonraker_unreachable_is_a_comm_error() -> None:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    with pytest.raises(PrinterCommError) as info:
        moonraker.MoonrakerAdapter("u1", "127.0.0.1", port, timeout=1).status()
    assert info.value.exit_code == 1 and "ping 127.0.0.1" in info.value.render(color=False)


# ---- creality.py ------------------------------------------------------------------------

def k1_adapter(k1: FakeCreality) -> creality.CrealityStockAdapter:
    return creality.CrealityStockAdapter("k1se", "127.0.0.1", k1.http_port, ws_port=k1.ws_port,
                                         listen_s=0.4, files_listen_s=0.6, confirm_s=1.0)


def test_creality_status_merges_deltas_and_answers_heartbeat(k1: FakeCreality) -> None:
    k1.state = k1_state(1, 1, "roof.gcode")
    st = k1_adapter(k1).status()
    assert (st.state, st.filename, st.model) == ("printing", "roof.gcode", "K1 SE")
    assert st.progress == pytest.approx(0.37)
    assert st.temps == {"nozzle": (pytest.approx(210.12), 210.0), "bed": (pytest.approx(60.01), 60.0)}
    assert "ok" in k1.ws_received


@pytest.mark.parametrize("state, device_state, norm", [
    (0, 0, "idle"), (1, 1, "printing"), (2, 0, "complete"), (0, 1, "busy"), (4, 0, "idle"), (5, 1, "busy")])
def test_creality_state_mapping(state: int, device_state: int, norm: str) -> None:
    st = creality.parse_status(k1_state(state, device_state))
    assert st.state == norm
    if state == 4:
        assert "not mapped" in (st.message or "")


def test_creality_no_pushed_state_is_unknown() -> None:
    assert creality.parse_status({}).state == "unknown"


def test_creality_upload_sends_only_the_file_field(k1: FakeCreality, tmp_path: Path) -> None:
    """Issue #99: the extra `path` field made the K1 SE store 165-168 extra bytes."""
    g = gcode(tmp_path)
    res = k1_adapter(k1).upload(g, "part.gcode")
    assert res.verified and k1.files["part.gcode"] == g.stat().st_size
    (post,) = [r for r in k1.http_requests if r.method == "POST"]
    assert post.path == "/upload/part.gcode" and set(post.fields) == {"file"}
    assert not [c for c in k1.ws_commands() if c.get("method") == "set"]


def test_creality_upload_size_mismatch_fails(k1: FakeCreality, tmp_path: Path) -> None:
    k1.stored_size_delta = 166
    with pytest.raises(PrinterCommError, match="size mismatch"):
        k1_adapter(k1).upload(gcode(tmp_path), "part.gcode")


def test_creality_start_and_stop_frames(k1: FakeCreality) -> None:
    a = k1_adapter(k1)
    a.start("roof.gcode")
    a.cancel()
    sets = [c for c in k1.ws_commands() if c.get("method") == "set"]
    assert sets == [
        {"method": "set", "params": {"opGcodeFile": "printprt:/usr/data/printer_data/gcodes/roof.gcode"}},
        {"method": "set", "params": {"stop": 1}},
    ]


# ---- base.py ----------------------------------------------------------------------------

def test_safe_remote_name_replaces_spaces_and_rejects_paths(tmp_path: Path) -> None:
    assert safe_remote_name(tmp_path / "roof left.gcode") == "roof_left.gcode"
    for bad in ("../x.gcode", "a/b.gcode", "x.gcode?print=true", "x.stl"):
        with pytest.raises(InvalidArgument):
            safe_remote_name(tmp_path / "ok.gcode", bad)


def test_busy_states_include_unknown() -> None:
    assert pbase.BUSY_STATES == {"printing", "paused", "busy", "unknown"}


# ---- preflight.py -----------------------------------------------------------------------

def u1_ps(**changes: Any) -> PrinterStatus:
    st = moonraker.parse_status(u1_status())
    for i, kw in changes.items():
        st.slots[int(i[1:])].__dict__.update(kw)
    return st


def test_gcode_meta_reads_colours_types_and_used_tools(tmp_path: Path) -> None:
    meta = preflight.read_gcode_meta(gcode(tmp_path))
    assert meta.used_tools == [0, 2, 3]
    assert meta.colours == ["#000000", "#808080", "#1B5E20", "#FFFFFF"]
    assert meta.types == ["PLA"] * 4


def test_gcode_meta_without_usage_line_falls_back_to_tool_changes(tmp_path: Path) -> None:
    meta = preflight.read_gcode_meta(gcode(tmp_path, "T1\nG1 X1\nT3\n; filament_colour = #111111;#222222;#333333;#444444\n"))
    assert meta.used_tools == [1, 3]
    assert preflight.read_gcode_meta(gcode(tmp_path, "G1 X1\n", "single.gcode")).used_tools == [0]


def test_slots_pass_when_loaded_colours_match(tmp_path: Path) -> None:
    checks = preflight.check_slots(preflight.read_gcode_meta(gcode(tmp_path)), u1_ps())
    assert [(c.name, c.status) for c in checks] == [("slot T0", "PASS"), ("slot T2", "PASS"), ("slot T3", "PASS")]


def test_slots_fail_on_colour_material_and_empty_head(tmp_path: Path) -> None:
    meta = preflight.read_gcode_meta(gcode(tmp_path))
    meta.used_tools = [0, 1, 2, 3]
    st = u1_ps(T0={"color": "#FFFFFF"}, T3={"material": "PETG"})
    by = {c.name: c for c in preflight.check_slots(meta, st)}
    assert by["slot T0"].status == "FAIL" and "G-code #000000, loaded #FFFFFF" in by["slot T0"].detail
    assert by["slot T1"].status == "FAIL" and "T2 is used" in by["slot T1"].detail
    assert by["slot T3"].status == "FAIL" and "PETG" in by["slot T3"].detail
    assert by["slot T2"].status == "PASS"


def test_slots_skip_without_per_head_data(tmp_path: Path) -> None:
    st = creality.parse_status(k1_state())
    (c,) = preflight.check_slots(preflight.read_gcode_meta(gcode(tmp_path)), st)
    assert c.status == "SKIP"


def test_printer_free_check() -> None:
    assert preflight.check_printer_free(moonraker.parse_status(u1_status("printing", "x.gcode"))).status == "FAIL"
    assert preflight.check_printer_free(moonraker.parse_status(u1_status("complete"))).status == "PASS"


def test_thumbnails_unverified_when_checker_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "gcode_thumbnails", None)  # import -> ImportError
    c = preflight.check_thumbnails(gcode(tmp_path))
    assert c.status == "WARN" and "UNVERIFIED" in c.detail


def test_thumbnails_use_the_gcode_thumbnails_checker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    t = types.SimpleNamespace
    calls: list[str] = []

    def check(path: str) -> Any:
        calls.append(path)
        if path.endswith("good.gcode"):
            return t(ok=True, present=[t(w=48, h=48), t(w=300, h=300)], missing=[], reason="")
        return t(ok=False, present=[], missing=[t(w=300, h=300)], reason="missing required thumbnails")

    monkeypatch.setitem(sys.modules, "gcode_thumbnails", types.SimpleNamespace(check=check))
    good = preflight.check_thumbnails(gcode(tmp_path, name="good.gcode"))
    bad = preflight.check_thumbnails(gcode(tmp_path, name="bad.gcode"))
    assert (good.status, good.detail) == ("PASS", "48x48, 300x300")
    assert bad.status == "FAIL" and "300x300" in bad.detail and "gcode-thumbnails add" in bad.detail
    assert len(calls) == 2


@pytest.mark.parametrize("rc, out, status", [
    (0, {"status": "PASS", "pairs": [{"pair": "TA", "status": "PASS"}]}, "PASS"),
    (1, {"status": "FAIL", "pairs": [{"pair": "TA", "status": "FAIL"}, {"pair": "TB", "status": "PASS"}]}, "FAIL"),
    (2, {}, "FAIL"),
])
def test_halves_check_exit_codes_map_to_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                               rc: int, out: dict[str, Any], status: str) -> None:
    monkeypatch.setattr(preflight, "run_halves_check", lambda p: (rc, json.dumps(out), "boom"))
    c = preflight.check_halves(tmp_path / "x.3mf")
    assert c.status == status
    if rc == 1:
        assert "TA" in c.detail and "TB" not in c.detail


def test_report_overall_and_json(tmp_path: Path) -> None:
    r = preflight.Report("x", [preflight.Check("a", "PASS", ""), preflight.Check("b", "WARN", "")])
    assert r.overall == "WARN"
    r.checks.append(preflight.Check("c", "FAIL", "bad"))
    assert r.to_json()["overall"] == "FAIL" and r.render().endswith("STATUS: FAIL")


# ---- commands/printer.py ----------------------------------------------------------------

def test_cmd_status_json(moon: FakeMoonraker, env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_devices(env, moon=moon)
    assert printer("status", "u1", "--json") == 0
    d = json.loads(capsys.readouterr().out)
    assert d["printer"] == "u1" and d["state"] == "idle" and d["slots"][1]["present"] is False
    assert d["temps"]["bed"] == {"actual": 30.0, "target": 0.0}


def test_cmd_list_without_config_prints_example(env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert printer("list") == 0
    assert "adapter: creality-stock" in capsys.readouterr().out


def test_cmd_upload_never_starts(moon: FakeMoonraker, env: Path, tmp_path: Path,
                                 capsys: pytest.CaptureFixture[str]) -> None:
    write_devices(env, moon=moon)
    assert printer("upload", "u1", str(gcode(tmp_path, name="my part.gcode"))) == 0
    assert "my_part.gcode" in moon.files
    assert [r.path for r in moon.writes()] == ["/server/files/upload"]
    assert "NOT started" in capsys.readouterr().out


def test_cmd_upload_refused_while_creality_busy(k1: FakeCreality, env: Path, tmp_path: Path) -> None:
    k1.state = k1_state(1, 1, "other.gcode")
    write_devices(env, k1=k1)
    with pytest.raises(PrinterRefused, match="upload refused"):
        printer("upload", "k1se", str(gcode(tmp_path)), "--secs", "0.3")
    assert k1.writes() == []


def test_cmd_upload_refused_over_the_running_file_on_moonraker(moon: FakeMoonraker, env: Path,
                                                                tmp_path: Path) -> None:
    moon.status = u1_status("printing", "part.gcode")
    write_devices(env, moon=moon)
    with pytest.raises(PrinterRefused, match="overwrite the file being printed"):
        printer("upload", "u1", str(gcode(tmp_path)))
    assert moon.writes() == []


def test_cmd_start_needs_yes_and_contacts_nothing(moon: FakeMoonraker, env: Path, tmp_path: Path) -> None:
    write_devices(env, moon=moon)
    with pytest.raises(UsageError, match="needs --yes"):
        printer("start", "u1", str(gcode(tmp_path)))
    assert moon.requests == []


@pytest.mark.parametrize("state", ["printing", "paused"])
def test_cmd_start_refused_when_busy(moon: FakeMoonraker, env: Path, tmp_path: Path, state: str) -> None:
    g = gcode(tmp_path)
    moon.files["part.gcode"] = g.stat().st_size
    moon.status = u1_status(state, "other.gcode")
    write_devices(env, moon=moon)
    with pytest.raises(PrinterRefused, match=f"is {state}"):
        printer("start", "u1", str(g), "--yes", "--force")
    assert moon.writes() == []


def test_cmd_start_refused_when_file_not_uploaded(moon: FakeMoonraker, env: Path, tmp_path: Path) -> None:
    write_devices(env, moon=moon)
    with pytest.raises(PrinterRefused, match="Upload it first|absent"):
        printer("start", "u1", str(gcode(tmp_path)), "--yes")
    assert moon.writes() == []


def test_cmd_start_refused_on_preflight_fail_and_forced(moon: FakeMoonraker, env: Path, tmp_path: Path,
                                                        thumbs_ok: None, capsys: pytest.CaptureFixture[str]) -> None:
    g = gcode(tmp_path, U1_GCODE.replace("#1B5E20;#FFFFFF\n; filament_type", "#FF0000;#FFFFFF\n; filament_type"))
    moon.files["part.gcode"] = g.stat().st_size
    write_devices(env, moon=moon)
    with pytest.raises(PrinterRefused, match="preflight FAIL"):
        printer("start", "u1", str(g), "--yes")
    assert moon.writes() == []
    assert "G-code #FF0000, loaded #1B5E20" in capsys.readouterr().out

    assert printer("start", "u1", str(g), "--yes", "--force") == 0
    out = capsys.readouterr().out
    assert "FORCED" in out and "slot T2" in out
    assert [(r.path, r.query) for r in moon.writes()] == [("/printer/print/start", {"filename": ["part.gcode"]})]


def test_cmd_start_happy_path_k1(k1: FakeCreality, env: Path, tmp_path: Path, thumbs_ok: None,
                                 capsys: pytest.CaptureFixture[str]) -> None:
    g = gcode(tmp_path, "G28\nG1 X1\n", "roof.gcode")
    k1.files["roof.gcode"] = g.stat().st_size
    write_devices(env, k1=k1)
    assert printer("start", "k1se", str(g), "--yes", "--secs", "0.3") == 0
    assert [c["params"] for c in k1.ws_commands() if c.get("method") == "set"] == [
        {"opGcodeFile": "printprt:/usr/data/printer_data/gcodes/roof.gcode"}]
    assert "start sent" in capsys.readouterr().out


def test_cmd_start_failure_is_reported_unknown_not_resent(moon: FakeMoonraker, env: Path, tmp_path: Path,
                                                          thumbs_ok: None, monkeypatch: pytest.MonkeyPatch) -> None:
    g = gcode(tmp_path)
    moon.files["part.gcode"] = g.stat().st_size
    write_devices(env, moon=moon)
    calls: list[str] = []

    def boom(self: moonraker.MoonrakerAdapter, name: str) -> None:
        calls.append(name)
        raise self._comm_error("POST /printer/print/start", TimeoutError("timed out"))

    monkeypatch.setattr(moonraker.MoonrakerAdapter, "start", boom)
    with pytest.raises(PrinterCommError, match="outcome UNKNOWN"):
        printer("start", "u1", str(g), "--yes")
    assert calls == ["part.gcode"]


def test_cmd_cancel_needs_yes_and_an_active_job(moon: FakeMoonraker, env: Path) -> None:
    write_devices(env, moon=moon)
    with pytest.raises(UsageError):
        printer("cancel", "u1")
    with pytest.raises(PrinterRefused, match="no job to cancel"):
        printer("cancel", "u1", "--yes")
    assert moon.writes() == []
    moon.status = u1_status("printing", "x.gcode")
    assert printer("cancel", "u1", "--yes") == 0
    assert [r.path for r in moon.writes()] == ["/printer/print/cancel"]


def test_cmd_missing_local_file(moon: FakeMoonraker, env: Path) -> None:
    write_devices(env, moon=moon)
    with pytest.raises(InputNotFound):
        printer("upload", "u1", "nope.gcode")


def test_cmd_preflight_with_printer_fails_on_colour(moon: FakeMoonraker, env: Path, tmp_path: Path,
                                                    thumbs_ok: None, capsys: pytest.CaptureFixture[str]) -> None:
    from commands.print_preflight import run
    from errors import GateFailure

    write_devices(env, moon=moon)
    moon.status["print_task_config"]["filament_color_rgba"][3] = "FFFF00FF"
    with pytest.raises(GateFailure):
        run([str(gcode(tmp_path)), "--printer", "u1"])
    assert "G-code #FFFFFF, loaded #FFFF00" in capsys.readouterr().out
    assert moon.writes() == []
