"""Unit tests for `3d printer` / `3d print-preflight` (lib/printer_ops, lib/commands/printer.py).

Every printer here is an in-process loopback fake (tests/printer_fakes.py); the tests prove
both the protocol frames and that refused operations send NOTHING to the printer.
Issues: https://github.com/alex-mextner/3d-cli/issues/90 (epic), #91-#96, #99.
"""

from __future__ import annotations

import json
import math
import socket
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from errors import InputNotFound, InvalidArgument, ThreeDError, UsageError
from printer_fakes import (
    FakeCreality,
    FakeHatch,
    FakeMoonraker,
    k1_state,
    u1_status,
    write_devices,
)
from printer_ops import base as pbase
from printer_ops import creality, devices, force_hatch, moonraker, move_bounds, preflight, ws
from printer_ops.base import (
    PrinterCommError,
    PrinterRefused,
    PrinterStatus,
    safe_remote_name,
)

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
def hatch(monkeypatch: pytest.MonkeyPatch) -> FakeHatch:
    """The owner's Telegram hatch for `printer start --force`; approves unless a test says otherwise."""
    fake = FakeHatch()
    monkeypatch.setattr(force_hatch, "load_hatch", lambda: fake)
    return fake


@pytest.fixture()
def gates_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """Command tests check printer logic, not the G-code gates with their own tests
    (thumbnails, overhangs, slicer warnings): pin those to PASS."""
    monkeypatch.setattr(preflight, "check_thumbnails", lambda p: preflight.Check("thumbnails", "PASS", "stub"))
    monkeypatch.setattr(preflight, "check_overhangs", lambda p, *_: preflight.Check("overhangs", "PASS", "stub"))
    monkeypatch.setattr(
        preflight, "check_slice_warnings", lambda o, g: preflight.Check("slicer warn", "PASS", "stub")
    )


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
        "k1se: {adapter: creality-stock, host: 192.168.0.27}\n",
        encoding="utf-8",
    )
    (tmp_path / "printer-devices.yaml").write_text(
        "k1se: {adapter: creality-stock, host: 10.0.0.9, ws_port: 9998}\n",
        encoding="utf-8",
    )
    d = devices.load_devices()
    assert (d["u1"].host, d["u1"].port, d["u1"].model) == (
        "192.168.0.26",
        7125,
        "Snapmaker U1",
    )
    assert (d["k1se"].host, d["k1se"].port, d["k1se"].ws_port) == ("10.0.0.9", 80, 9998)
    assert d["k1se"].source == tmp_path / "printer-devices.yaml"


@pytest.mark.parametrize(
    "text, exc",
    [
        ("u1: {adapter: octoprint, host: h}\n", InvalidArgument),
        ("u1: {adapter: moonraker}\n", devices.DeviceConfigError),
        ("u1: {adapter: moonraker, host: h, port: 'x'}\n", InvalidArgument),
        ("- just a list\n", devices.DeviceConfigError),
        ("u1: [1, 2]\n", devices.DeviceConfigError),
    ],
)
def test_devices_invalid_entries_fail_with_exit_2(
    env: Path, text: str, exc: type[ThreeDError]
) -> None:
    (env / "3d-cli").mkdir(parents=True)
    (env / "3d-cli" / "printer-devices.yaml").write_text(text, encoding="utf-8")
    with pytest.raises(exc) as info:
        devices.load_devices()
    assert info.value.exit_code == 2


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


@pytest.mark.parametrize(
    "raw, norm",
    [
        ("standby", "idle"),
        ("printing", "printing"),
        ("paused", "paused"),
        ("complete", "complete"),
        ("cancelled", "cancelled"),
        ("error", "error"),
        ("weird", "unknown"),
    ],
)
def test_moonraker_state_mapping(raw: str, norm: str) -> None:
    st = moonraker.parse_status(u1_status(raw, "a.gcode", 0.5))
    assert (st.state, st.raw_state, st.filename, st.progress) == (
        norm,
        raw,
        "a.gcode",
        0.5,
    )


def test_moonraker_klippy_not_ready_is_unknown_and_busy() -> None:
    s = u1_status()
    s["webhooks"] = {"state": "shutdown", "state_message": "MCU 'mcu' shutdown"}
    st = moonraker.parse_status(s)
    assert st.state == "unknown" and st.busy and st.raw_state == "klippy:shutdown"
    assert "MCU" in (st.message or "")


def test_moonraker_u1_slots_mark_broken_head_empty() -> None:
    st = moonraker.parse_status(u1_status())
    assert [(s.label, s.present, s.color) for s in st.slots] == [
        ("T1", True, "#000000"),
        ("T2", False, "#808080"),
        ("T3", True, "#1B5E20"),
        ("T4", True, "#FFFFFF"),
    ]
    assert st.temps["bed"] == (30.0, 0.0) and "extruder3" in st.temps


def test_moonraker_non_u1_printer_has_no_slots() -> None:
    s = u1_status()
    del s["print_task_config"]
    assert moonraker.parse_status(s).slots == []


def test_moonraker_upload_sends_print_false_and_verifies_size(
    moon: FakeMoonraker, tmp_path: Path
) -> None:
    a = moonraker.MoonrakerAdapter("u1", "127.0.0.1", moon.port)
    g = gcode(tmp_path)
    res = a.upload(g, "part.gcode")
    assert res.verified and res.size == g.stat().st_size
    up = [r for r in moon.writes() if r.path == "/server/files/upload"]
    assert (
        len(up) == 1
        and up[0].fields["print"] == b"false"
        and up[0].fields["root"] == b"gcodes"
    )
    assert up[0].fields["file"] == g.read_bytes()
    assert not [r for r in moon.writes() if r.path.startswith("/printer/print")]


def test_moonraker_upload_reply_claiming_start_is_an_error(
    moon: FakeMoonraker, tmp_path: Path
) -> None:
    moon.upload_reply = {
        "result": {"item": {"path": "part.gcode"}, "print_started": True}
    }
    with pytest.raises(PrinterCommError, match="STARTED or QUEUED"):
        moonraker.MoonrakerAdapter("u1", "127.0.0.1", moon.port).upload(
            gcode(tmp_path), "part.gcode"
        )


def test_moonraker_upload_size_mismatch_fails(
    moon: FakeMoonraker, tmp_path: Path
) -> None:
    moon.stored_size_delta = 3
    with pytest.raises(PrinterCommError, match="size mismatch"):
        moonraker.MoonrakerAdapter("u1", "127.0.0.1", moon.port).upload(
            gcode(tmp_path), "part.gcode"
        )


def test_moonraker_upload_timeout_reconciles_by_listing(
    moon: FakeMoonraker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
        ("/printer/print/start", {"filename": ["my part.gcode"]}),
        ("/printer/print/cancel", {}),
    ]


def test_moonraker_unreachable_is_a_comm_error() -> None:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    with pytest.raises(PrinterCommError) as info:
        moonraker.MoonrakerAdapter("u1", "127.0.0.1", port, timeout=1).status()
    assert info.value.exit_code == 1 and "ping 127.0.0.1" in info.value.render(
        color=False
    )


# ---- creality.py ------------------------------------------------------------------------


def k1_adapter(k1: FakeCreality) -> creality.CrealityStockAdapter:
    return creality.CrealityStockAdapter(
        "k1se",
        "127.0.0.1",
        k1.http_port,
        ws_port=k1.ws_port,
        listen_s=0.4,
        files_listen_s=0.6,
        confirm_s=1.0,
    )


def test_creality_status_merges_deltas_and_answers_heartbeat(k1: FakeCreality) -> None:
    k1.state = k1_state(1, 1, "roof.gcode")
    st = k1_adapter(k1).status()
    assert (st.state, st.filename, st.model) == ("printing", "roof.gcode", "K1 SE")
    assert st.progress == pytest.approx(0.37)
    assert st.temps == {
        "nozzle": (pytest.approx(210.12), 210.0),
        "bed": (pytest.approx(60.01), 60.0),
    }
    assert "ok" in k1.ws_received


@pytest.mark.parametrize(
    "state, device_state, norm",
    [
        (0, 0, "idle"),
        (1, 1, "printing"),
        (2, 0, "complete"),
        (0, 1, "busy"),
        (4, 0, "cancelled"),
        (5, 1, "paused"),
        (3, 0, "idle"),
        (3, 1, "busy"),
    ],
)
def test_creality_state_mapping(state: int, device_state: int, norm: str) -> None:
    st = creality.parse_status(k1_state(state, device_state))
    assert st.state == norm
    if (state, device_state) == (3, 0):
        assert "not mapped" in (st.message or "")


def test_creality_no_pushed_state_is_unknown() -> None:
    assert creality.parse_status({}).state == "unknown"


def test_creality_upload_sends_only_the_file_field(
    k1: FakeCreality, tmp_path: Path
) -> None:
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
        {
            "method": "set",
            "params": {
                "opGcodeFile": "printprt:/usr/data/printer_data/gcodes/roof.gcode"
            },
        },
        {"method": "set", "params": {"stop": 1}},
    ]


@pytest.mark.parametrize("reply", ["silent", "close"])
def test_creality_start_without_acknowledgement_is_an_error(
    k1: FakeCreality, reply: str
) -> None:
    """Review #132: a closed socket or silence after opGcodeFile is not a successful start."""
    k1.start_reply = reply
    with pytest.raises(PrinterCommError, match="no acknowledgement"):
        k1_adapter(k1).start("roof.gcode")


def test_creality_start_ack_must_name_this_file_in_a_running_state(
    k1: FakeCreality,
) -> None:
    """The pushed snapshot of a COMPLETED earlier run of the same file is no acknowledgement."""
    k1.state = k1_state(2, 0, "roof.gcode")
    k1.start_reply = "silent"
    with pytest.raises(PrinterCommError, match="no acknowledgement"):
        k1_adapter(k1).start("roof.gcode")


# ---- base.py ----------------------------------------------------------------------------


def test_safe_remote_name_is_content_addressed_and_rejects_paths(
    tmp_path: Path,
) -> None:
    g = gcode(tmp_path, "G28\n", "roof left.gcode")
    tag = pbase.file_sha256(g)[:16]
    assert safe_remote_name(g) == f"roof_left-{tag}.gcode"
    assert safe_remote_name(g, "roof.gcode") == f"roof-{tag}.gcode"
    assert (
        safe_remote_name(g, f"roof-{tag}.gcode") == f"roof-{tag}.gcode"
    )  # not tagged twice
    g.write_text("G28\nG1 X1\n", encoding="utf-8")
    assert safe_remote_name(g) != f"roof_left-{tag}.gcode"  # new content, new name
    for bad in ("../x.gcode", "a/b.gcode", "x.gcode?print=true", "x.stl"):
        with pytest.raises(InvalidArgument):
            safe_remote_name(g, bad)


def test_busy_states_include_unknown() -> None:
    assert {"printing", "paused", "busy", "unknown"} == pbase.BUSY_STATES


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


def test_gcode_meta_without_usage_line_falls_back_to_tool_changes(
    tmp_path: Path,
) -> None:
    meta = preflight.read_gcode_meta(
        gcode(
            tmp_path,
            "T1\nG1 X1\nT3\n; filament_colour = #111111;#222222;#333333;#444444\n",
        )
    )
    assert meta.used_tools == [1, 3]
    assert preflight.read_gcode_meta(
        gcode(tmp_path, "G1 X1\n", "single.gcode")
    ).used_tools == [0]


def test_slots_pass_when_loaded_colours_match(tmp_path: Path) -> None:
    checks = preflight.check_slots(preflight.read_gcode_meta(gcode(tmp_path)), u1_ps())
    assert [(c.name, c.status) for c in checks] == [
        ("slot T0", "PASS"),
        ("slot T2", "PASS"),
        ("slot T3", "PASS"),
    ]


def test_slots_fail_on_colour_material_and_empty_head(tmp_path: Path) -> None:
    meta = preflight.read_gcode_meta(gcode(tmp_path))
    meta.used_tools = [0, 1, 2, 3]
    st = u1_ps(T0={"color": "#FFFFFF"}, T3={"material": "PETG"})
    by = {c.name: c for c in preflight.check_slots(meta, st)}
    assert (
        by["slot T0"].status == "FAIL"
        and "G-code #000000, loaded #FFFFFF" in by["slot T0"].detail
    )
    assert by["slot T1"].status == "FAIL" and "T2 is used" in by["slot T1"].detail
    assert by["slot T3"].status == "FAIL" and "PETG" in by["slot T3"].detail
    assert by["slot T2"].status == "PASS"


def test_slots_skip_without_per_head_data(tmp_path: Path) -> None:
    st = creality.parse_status(k1_state())
    (c,) = preflight.check_slots(preflight.read_gcode_meta(gcode(tmp_path)), st)
    assert c.status == "SKIP"


def test_printer_free_check() -> None:
    assert (
        preflight.check_printer_free(
            moonraker.parse_status(u1_status("printing", "x.gcode"))
        ).status
        == "FAIL"
    )
    assert (
        preflight.check_printer_free(
            moonraker.parse_status(u1_status("complete"))
        ).status
        == "PASS"
    )


def test_thumbnails_use_the_real_gcode_thumbnails_checker(tmp_path: Path) -> None:
    """U1_GCODE asks for 48x48 and 300x300 previews and carries none: FAIL naming them."""
    bad = preflight.check_thumbnails(gcode(tmp_path))
    assert (
        bad.status == "FAIL"
        and "48x48" in bad.detail
        and "gcode-thumbnails add" in bad.detail
    )

    png = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
    blocks = "".join(
        f"; thumbnail begin {s}x{s} {len(png)}\n; {png}\n; thumbnail end\n"
        for s in (48, 300)
    )
    good = preflight.check_thumbnails(gcode(tmp_path, blocks + U1_GCODE, "good.gcode"))
    assert (good.status, good.detail) == ("PASS", "48x48, 300x300")


@pytest.mark.parametrize(
    "rc, out, status",
    [
        (0, {"status": "PASS", "pairs": [{"pair": "TA", "status": "PASS"}]}, "PASS"),
        (
            1,
            {
                "status": "FAIL",
                "pairs": [
                    {"pair": "TA", "status": "FAIL"},
                    {"pair": "TB", "status": "PASS"},
                ],
            },
            "FAIL",
        ),
        (2, {}, "FAIL"),
    ],
)
def test_halves_check_exit_codes_map_to_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    rc: int,
    out: dict[str, Any],
    status: str,
) -> None:
    monkeypatch.setattr(
        preflight, "run_halves_check", lambda p: (rc, json.dumps(out), "boom")
    )
    c = preflight.check_halves(tmp_path / "x.3mf")
    assert c.status == status
    if rc == 1:
        assert "TA" in c.detail and "TB" not in c.detail


def test_report_overall_and_json(tmp_path: Path) -> None:
    r = preflight.Report(
        "x", [preflight.Check("a", "PASS", ""), preflight.Check("b", "WARN", "")]
    )
    assert r.overall == "WARN"
    r.checks.append(preflight.Check("c", "FAIL", "bad"))
    assert r.to_json()["overall"] == "FAIL" and r.render().endswith("STATUS: FAIL")


# ---- commands/printer.py ----------------------------------------------------------------


def test_cmd_status_json(
    moon: FakeMoonraker, env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_devices(env, moon=moon)
    assert printer("status", "u1", "--json") == 0
    d = json.loads(capsys.readouterr().out)
    assert (
        d["printer"] == "u1"
        and d["state"] == "idle"
        and d["slots"][1]["present"] is False
    )
    assert d["temps"]["bed"] == {"actual": 30.0, "target": 0.0}


def test_cmd_list_without_config_prints_example(
    env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert printer("list") == 0
    assert "adapter: creality-stock" in capsys.readouterr().out


def test_cmd_upload_never_starts(
    moon: FakeMoonraker, env: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_devices(env, moon=moon)
    g = gcode(tmp_path, name="my part.gcode")
    assert printer("upload", "u1", str(g)) == 0
    assert safe_remote_name(g) in moon.files and safe_remote_name(g).startswith(
        "my_part-"
    )
    assert [r.path for r in moon.writes()] == ["/server/files/upload"]
    assert "NOT started" in capsys.readouterr().out


def test_cmd_upload_refused_while_creality_busy(
    k1: FakeCreality, env: Path, tmp_path: Path
) -> None:
    k1.state = k1_state(1, 1, "other.gcode")
    write_devices(env, k1=k1)
    with pytest.raises(PrinterRefused, match="upload refused"):
        printer("upload", "k1se", str(gcode(tmp_path)), "--secs", "0.3")
    assert k1.writes() == []


def test_cmd_upload_refused_over_the_running_file_on_moonraker(
    moon: FakeMoonraker, env: Path, tmp_path: Path
) -> None:
    g = gcode(tmp_path)
    moon.status = u1_status("printing", safe_remote_name(g))
    write_devices(env, moon=moon)
    with pytest.raises(PrinterRefused, match="overwrite the file being printed"):
        printer("upload", "u1", str(g))
    assert moon.writes() == []


def test_cmd_start_needs_yes_and_contacts_nothing(
    moon: FakeMoonraker, env: Path, tmp_path: Path
) -> None:
    write_devices(env, moon=moon)
    with pytest.raises(UsageError, match="needs --yes"):
        printer("start", "u1", str(gcode(tmp_path)))
    assert moon.requests == []


@pytest.mark.parametrize("state", ["printing", "paused"])
def test_cmd_start_refused_when_busy(
    moon: FakeMoonraker, env: Path, tmp_path: Path, state: str
) -> None:
    g = gcode(tmp_path)
    moon.put(safe_remote_name(g), g.read_bytes())
    moon.status = u1_status(state, "other.gcode")
    write_devices(env, moon=moon)
    with pytest.raises(PrinterRefused, match=f"is {state}"):
        printer("start", "u1", str(g), "--yes", "--force")
    assert moon.writes() == []


def test_cmd_start_refused_when_file_not_uploaded(
    moon: FakeMoonraker, env: Path, tmp_path: Path
) -> None:
    write_devices(env, moon=moon)
    with pytest.raises(PrinterRefused, match="Upload it first|absent"):
        printer("start", "u1", str(gcode(tmp_path)), "--yes")
    assert moon.writes() == []


def test_cmd_start_refuses_a_same_size_file_with_other_content(
    moon: FakeMoonraker, env: Path, tmp_path: Path, gates_ok: None
) -> None:
    """Review #132: a stale/overwritten remote file of the same length must not be started.
    (a) the local file changed after upload: its content-addressed name is no longer there;
    (b) the stored bytes under the right name differ: the read-back hash catches it."""
    g = gcode(tmp_path)
    write_devices(env, moon=moon)
    assert printer("upload", "u1", str(g)) == 0
    original = g.read_bytes()
    g.write_bytes(original.replace(b"X10", b"X99"))  # same length, different content
    with pytest.raises(PrinterRefused, match="absent"):
        printer("start", "u1", str(g), "--yes")

    g.write_bytes(original)
    tampered = original.replace(b"X20", b"X88")
    assert len(tampered) == len(original)
    moon.put(safe_remote_name(g), tampered)
    with pytest.raises(PrinterRefused, match="different content"):
        printer("start", "u1", str(g), "--yes", "--force")
    assert [r.path for r in moon.writes()] == ["/server/files/upload"]


def test_cmd_start_refused_on_preflight_fail_and_forced(
    moon: FakeMoonraker,
    env: Path,
    tmp_path: Path,
    gates_ok: None,
    hatch: FakeHatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    g = gcode(
        tmp_path,
        U1_GCODE.replace(
            "#1B5E20;#FFFFFF\n; filament_type", "#FF0000;#FFFFFF\n; filament_type"
        ),
    )
    moon.put(safe_remote_name(g), g.read_bytes())
    write_devices(env, moon=moon)
    with pytest.raises(PrinterRefused, match="preflight FAIL"):
        printer("start", "u1", str(g), "--yes")
    assert moon.writes() == []
    assert "G-code #FF0000, loaded #1B5E20" in capsys.readouterr().out

    assert hatch.requests == []  # a plain refusal asks nobody

    assert printer("start", "u1", str(g), "--yes", "--force") == 0
    out = capsys.readouterr().out
    assert "FORCED" in out and "slot T2" in out and "read-back sha256 matches" in out
    assert "owner approved on Telegram (test double approved)" in out
    assert [(r.path, r.query) for r in moon.writes()] == [
        ("/printer/print/start", {"filename": [safe_remote_name(g)]})
    ]
    (request,) = hatch.requests
    assert request["hook"] == "3d-printer-start-force" and request["critical"] is True
    assert request["context"]["printer"] == "u1" and request["context"]["file"] == str(g)
    assert "- slot T2:" in request["context"]["failing_checks"]


@pytest.mark.parametrize("verdict", ["deny", "not-requested"])
def test_cmd_start_force_without_telegram_approval_never_starts(
    moon: FakeMoonraker, env: Path, tmp_path: Path, gates_ok: None, hatch: FakeHatch, verdict: str
) -> None:
    """`--force` alone must not start a print that fails preflight: the owner's approval does."""
    hatch.verdict = verdict
    g = gcode(
        tmp_path,
        U1_GCODE.replace("#1B5E20;#FFFFFF\n; filament_type", "#FF0000;#FFFFFF\n; filament_type"),
    )
    moon.put(safe_remote_name(g), g.read_bytes())
    write_devices(env, moon=moon)
    with pytest.raises(PrinterRefused, match="--force does not override preflight FAIL by itself") as exc:
        printer("start", "u1", str(g), "--yes", "--force")
    assert "RIG_HATCH_REQUEST_3D_PRINTER_START_FORCE" in "\n".join(exc.value.remediation)
    assert "- slot T2:" in "\n".join(exc.value.remediation)
    assert moon.writes() == []


def test_cmd_start_happy_path_k1(
    k1: FakeCreality,
    env: Path,
    tmp_path: Path,
    gates_ok: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    g = gcode(tmp_path, "G28\nG1 X1\n", "roof.gcode")
    remote = safe_remote_name(g)
    k1.files[remote] = g.stat().st_size
    write_devices(env, k1=k1)
    assert printer("start", "k1se", str(g), "--yes", "--secs", "0.3") == 0
    assert [c["params"] for c in k1.ws_commands() if c.get("method") == "set"] == [
        {"opGcodeFile": f"printprt:/usr/data/printer_data/gcodes/{remote}"}
    ]
    out = capsys.readouterr().out
    assert "start acknowledged" in out and "content hash in the file name + size" in out


def test_cmd_start_k1_without_ack_reports_unknown(
    k1: FakeCreality, env: Path, tmp_path: Path, gates_ok: None
) -> None:
    g = gcode(tmp_path, "G28\nG1 X1\n", "roof.gcode")
    k1.files[safe_remote_name(g)] = g.stat().st_size
    k1.start_reply = "close"
    write_devices(env, k1=k1)
    with pytest.raises(PrinterCommError, match="outcome UNKNOWN") as info:
        printer("start", "k1se", str(g), "--yes", "--secs", "0.3")
    assert "Do NOT resend" in info.value.render(color=False)


def test_cmd_start_failure_is_reported_unknown_not_resent(
    moon: FakeMoonraker,
    env: Path,
    tmp_path: Path,
    gates_ok: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    g = gcode(tmp_path)
    moon.put(safe_remote_name(g), g.read_bytes())
    write_devices(env, moon=moon)
    calls: list[str] = []

    def boom(self: moonraker.MoonrakerAdapter, name: str) -> None:
        calls.append(name)
        raise self._comm_error("POST /printer/print/start", TimeoutError("timed out"))

    monkeypatch.setattr(moonraker.MoonrakerAdapter, "start", boom)
    with pytest.raises(PrinterCommError, match="outcome UNKNOWN"):
        printer("start", "u1", str(g), "--yes")
    assert calls == [safe_remote_name(g)]


def test_cmd_start_status_failure_after_ack_is_unknown(
    moon: FakeMoonraker,
    env: Path,
    tmp_path: Path,
    gates_ok: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review #132: the start was acknowledged, then the printer went quiet — still UNKNOWN."""
    g = gcode(tmp_path)
    moon.put(safe_remote_name(g), g.read_bytes())
    write_devices(env, moon=moon)
    real_status = moonraker.MoonrakerAdapter.status

    def status(self: moonraker.MoonrakerAdapter) -> PrinterStatus:
        if any(r.path == "/printer/print/start" for r in moon.writes()):
            raise self._comm_error("objects/query", TimeoutError("timed out"))
        return real_status(self)

    monkeypatch.setattr(moonraker.MoonrakerAdapter, "status", status)
    with pytest.raises(PrinterCommError, match="acknowledged.*outcome UNKNOWN") as info:
        printer("start", "u1", str(g), "--yes")
    assert "Do NOT resend" in info.value.render(color=False)
    assert len([r for r in moon.writes() if r.path == "/printer/print/start"]) == 1


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


def test_cmd_preflight_with_printer_fails_on_colour(
    moon: FakeMoonraker,
    env: Path,
    tmp_path: Path,
    gates_ok: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from commands.print_preflight import run
    from errors import GateFailure

    write_devices(env, moon=moon)
    moon.status["print_task_config"]["filament_color_rgba"][3] = "FFFF00FF"
    with pytest.raises(GateFailure):
        run([str(gcode(tmp_path)), "--printer", "u1"])
    assert "G-code #FFFFFF, loaded #FFFF00" in capsys.readouterr().out
    assert moon.writes() == []


# ---- move_bounds.py (issue #101) ----------------------------------------------------------

K1_LIMITS = ((-5.0, 229.0), (-0.5, 227.0))
K1_FOOTER = "; printable_area = 0x0,220x0,220x220,0x220\n; bed_exclude_area = 0x0\n"


def plate(
    body: str, *, start: str = "G28\nG90\n", end: str = "", footer: str = K1_FOOTER
) -> str:
    """A minimal Orca-shaped file: start G-code, one object on the plate, end G-code, config."""
    return (
        f";TYPE:Custom\n{start};LAYER_CHANGE\n;Z:0.2\n;TYPE:Outer wall\nEXCLUDE_OBJECT_START NAME=a\n"
        f"{body}EXCLUDE_OBJECT_END NAME=a\n;TYPE:Custom\n{end}{footer}"
    )


def bounds(tmp_path: Path, text: str, limits: Any = K1_LIMITS) -> Any:
    return move_bounds.check_bounds(gcode(tmp_path, text, "b.gcode"), limits)


def test_k1_spiral_zhop_arc_reaches_y_minus_084_and_fails(tmp_path: Path) -> None:
    """The arc that faulted the K1 SE ("Y out of range"): both end points are on the bed."""
    # like a real plate: travel to the wipe end, then the z-hop arc (no extrusion from an unknown spot)
    rep = bounds(
        tmp_path,
        plate(
            "G1 X62.83 Y2.774\nG17\nG3 Z1.36 I-.359 J-1.79 P1  F30000\nG1 X46.468 Y6.057\n"
        ),
    )
    assert rep.axis_out_count == 1 and rep.axis_out[0].line == 10
    assert rep.axis_out[0].y == pytest.approx(0.984 - math.hypot(0.359, 1.79), abs=1e-3)
    assert rep.axis_out[0].y == pytest.approx(-0.84, abs=0.01)
    assert rep.area_out_count == 1
    checks = preflight.check_moves(rep, K1_LIMITS, "test")
    assert [(c.name, c.status) for c in checks] == [
        ("axis travel", "FAIL"),
        ("print area", "FAIL"),
    ]
    assert "line 10 (62.47, -0.84)" in checks[0].detail


@pytest.mark.parametrize(
    "start, end, ij, cw, want",
    [
        (
            (1, 0),
            (0, 1),
            (-1, 0),
            False,
            {(1, 0), (0, 1)},
        ),  # ccw quarter: no extra extreme
        (
            (1, 0),
            (0, 1),
            (-1, 0),
            True,
            {(1, 0), (0, 1), (0, -1), (-1, 0)},
        ),  # cw: the long way round
        (
            (1, 0),
            (-1, 0),
            (-1, 0),
            False,
            {(1, 0), (-1, 0), (0, 1)},
        ),  # ccw half through +Y
        (
            (1, 0),
            (-1, 0),
            (-1, 0),
            True,
            {(1, 0), (-1, 0), (0, -1)},
        ),  # cw half through -Y
        (
            (1, 0),
            (1, 0),
            (-1, 0),
            True,
            {(1, 0), (0, 1), (-1, 0), (0, -1)},
        ),  # full circle
    ],
)
def test_arc_points_cover_the_real_extremes(
    start: tuple[float, float],
    end: tuple[float, float],
    ij: tuple[float, float],
    cw: bool,
    want: set[Any],
) -> None:
    pts = move_bounds.arc_points(start[0], start[1], end[0], end[1], ij[0], ij[1], cw)
    assert {(round(x, 6) + 0.0, round(y, 6) + 0.0) for x, y in pts} == want


def test_relative_moves_g92_and_g28(tmp_path: Path) -> None:
    body = (
        "G1 X100 Y100\nG91\nG1 X10 Y-150\nG90\n"  # relative: (110, -50)
        "G1 X50 Y50\nG92 X0 Y0\nG1 X-60 Y10\n"
    )  # G92: G-code -60 is machine -10
    rep = bounds(tmp_path, plate(body))
    assert [(v.x, v.y) for v in rep.axis_out] == [(110.0, -50.0), (-10.0, 60.0)]
    rep = bounds(tmp_path, plate("G1 X1 Y1\n", start="G28\nG91\nG1 X-500\nG90\n"))
    assert (
        rep.axis_out_count == 0
    )  # after G28 the position is unknown: a relative move is not checkable
    assert rep.unverified_count == 1 and "unknown position" in rep.unverified[0]


def test_a_known_axis_is_checked_while_the_other_is_unknown(tmp_path: Path) -> None:
    """Review finding: `G1 X300` right after G28 (Y unknown) must still fail on X."""
    rep = bounds(
        tmp_path, plate("G1 X10 Y10\n", start="G28\nG90\nG1 X300\nG1 X10 Y10\n")
    )
    assert rep.axis_out_count == 1 and str(rep.axis_out[0]) == "line 4 (300.00, ?)"


@pytest.mark.parametrize(
    "home, bad", [("G28 Z", 1), ("G28 Z0", 1), ("G28 X", 0), ("G28 Y", 1), ("G28", 0)]
)
def test_g28_forgets_only_the_homed_axes(tmp_path: Path, home: str, bad: int) -> None:
    """`G28 Z` keeps X and Y known, so a later `G1 X300` is still checked; homing X forgets X
    and makes the relative move uncheckable."""
    rep = bounds(
        tmp_path, plate(f"G1 X10 Y10\n{home}\nG91\nG1 X290\nG90\nG1 X10 Y10\n")
    )
    assert rep.axis_out_count == bad


def test_extruding_path_through_the_exclude_area_fails_travel_does_not(
    tmp_path: Path,
) -> None:
    """Review finding: both end points may be outside `bed_exclude_area` while the line or the
    arc between them deposits plastic inside it; travel moves over it are harmless."""
    p1s = "; printable_area = 0x0,256x0,256x256,0x256\n; bed_exclude_area = 0x0,18x0,18x28,0x28\n"
    body = (
        "M83\nG1 X30 Y40\n"
        "G1 X10 Y10 E1\n"  # ends inside: FAIL
        "G0 X30 Y40\nG1 X18 Y28 E1\n"  # touches the corner only: allowed
        "G1 X18 Y40 E1\n"  # along the edge line, outside: allowed
        "G0 X10 Y10\nG0 X30 Y10\n"  # travel through it: allowed
        "G0 X10 Y35\nG1 X25 Y10 E1\n"
    )  # both ends outside, crosses the corner: FAIL
    rep = bounds(tmp_path, plate(body, footer=p1s), None)
    assert [v.line for v in rep.excluded_in] == [10, 17]
    excl = [(0.0, 0.0), (18.0, 0.0), (18.0, 28.0), (0.0, 28.0)]
    for v in (
        rep.excluded_in
    ):  # each reported point is a place where plastic lands inside the area
        assert v.x is not None and v.y is not None
        assert move_bounds.inside(v.x, v.y, excl, boundary=False)
    checks = preflight.check_moves(rep, None, "")
    assert (
        checks[0].status == "SKIP"
        and checks[1].status == "FAIL"
        and "bed_exclude_area" in checks[1].detail
    )
    square = "; printable_area = 0x0,256x0,256x256,0x256\n; bed_exclude_area = 112x112,116x112,116x116,112x116\n"
    rep = bounds(
        tmp_path,
        plate("M83\nG1 X120 Y100\nG3 X120 Y100 I-20 J0 E5\n", footer=square),
        None,
    )
    assert (
        rep.excluded_in_count == 1
    )  # full circle r=20 about (100,100) passes (114.1, 114.1)
    rep = bounds(
        tmp_path,
        plate("M83\nG1 X120 Y100\nG3 X120 Y100 I-20 J0\n", footer=square),
        None,
    )
    assert rep.excluded_in_count == 0  # same circle as a travel (z-hop): allowed


def test_absolute_extrusion_counts_only_increasing_e(tmp_path: Path) -> None:
    p1s = "; printable_area = 0x0,256x0,256x256,0x256\n; bed_exclude_area = 0x0,18x0,18x28,0x28\n"
    body = "M82\nG92 E0\nG1 X30 Y40\nG1 X10 Y10 E5\nG1 X30 Y40 E4\nG1 X10 Y10 E4\n"
    rep = bounds(tmp_path, plate(body, footer=p1s), None)
    assert [v.line for v in rep.excluded_in] == [
        11
    ]  # E5 extrudes; E4 (retract) and E4 again do not


SQUARE = "; printable_area = 0x0,256x0,256x256,0x256\n; bed_exclude_area = 112x112,116x112,116x116,112x116\n"


@pytest.mark.parametrize(
    "body",
    [
        # relative E between absolute moves still advances E: the last move extrudes 0 -> 5
        "M82\nG92 E0\nG1 X100 Y100 E10\nM83\nG1 E-10\nM82\nG1 X114 Y114 E5\n",
        # a straight line through two opposite corners: no proper edge crossing, interior entered
        "M83\nG1 X100 Y100\nG1 X200 Y200 E1\n",
    ],
)
def test_exclude_area_regressions_from_review(tmp_path: Path, body: str) -> None:
    rep = bounds(tmp_path, plate(body, footer=SQUARE), None)
    assert rep.excluded_in_count == 1


def test_exclude_area_thinner_than_any_sampling_step_is_still_hit_by_an_arc(
    tmp_path: Path,
) -> None:
    """Review case: the circle r=100 about (100,100) passes x=199.94 at y=103.5, inside a
    0.04 x 0.2 mm rectangle a 5-degree chord would miss."""
    thin = (
        "; printable_area = 0x0,256x0,256x256,0x256\n"
        "; bed_exclude_area = 199.92x103.4,199.96x103.4,199.96x103.6,199.92x103.6\n"
    )
    rep = bounds(
        tmp_path,
        plate("M83\nG1 X200 Y100\nG3 X200 Y100 I-100 J0 E10\n", footer=thin),
        None,
    )
    assert rep.excluded_in_count == 1


@pytest.mark.parametrize(
    "area, body",
    [
        (
            "0x0,100x0,100x30,30x30,30x100,0x100",
            "G1 X10 Y90\nG1 X90 Y10 E1\n",
        ),  # line cuts the L's notch
        (
            "100x0,200x100,100x200,0x100",
            "G1 X180 Y100\nG3 X180 Y100 I-80 J0 E1\n",
        ),  # circle bulges out at 45°
    ],
)
def test_non_rectangular_printable_area_follows_the_whole_path(
    tmp_path: Path, area: str, body: str
) -> None:
    """Review case: both end points (and the arc's cardinal extremes) are inside the polygon,
    but the path between them leaves it."""
    rep = bounds(tmp_path, plate(body, footer=f"; printable_area = {area}\n"), None)
    assert rep.area_out_count == 1


def test_move_cuts_are_exact_on_a_square() -> None:
    sq = [(112.0, 112.0), (116.0, 112.0), (116.0, 116.0), (112.0, 116.0)]
    assert move_bounds.Move(100, 100, 200, 200).first_inside(sq) == pytest.approx(
        (114.0, 114.0)
    )
    assert (
        move_bounds.Move(100, 112, 200, 112).first_inside(sq) is None
    )  # along an edge
    assert (
        move_bounds.Move(100, 100, 112, 112).first_inside(sq) is None
    )  # ends on a corner


def test_area_check_covers_only_the_plate_but_axis_check_covers_everything(
    tmp_path: Path,
) -> None:
    # start G-code parks off the bed; the plate travels in first, then extrudes
    text = plate(
        "G1 X10 Y10\nG1 X20 Y20 E1\n", start="G28\nG1 X-3 Y-0.4\n", end="G1 X228 Y226\n"
    )
    rep = bounds(tmp_path, text)
    assert rep.area_out_count == 0 and rep.axis_out_count == 0
    rep = bounds(tmp_path, plate("G1 X10 Y10 E1\n", end="G1 X230 Y100\n"))
    assert rep.axis_out_count == 1 and rep.area_out_count == 0


def test_prime_tower_after_last_object_is_still_on_the_plate(tmp_path: Path) -> None:
    text = plate(
        "G1 X10 Y10 E1\n",
        end=";TYPE:Prime tower\nG1 X-1.47 Y100 E1\n;TYPE:Custom\nG1 X5 Y200\n",
    )
    rep = bounds(tmp_path, text, None)
    assert [(v.line, v.x) for v in rep.area_out] == [(12, -1.47)]


def test_custom_blocks_inside_the_plate_are_exempt_from_the_area_check(
    tmp_path: Path,
) -> None:
    rep = bounds(
        tmp_path,
        plate(
            "G1 X10 Y10 E1\n;TYPE:Custom\nG1 X-2 Y5\n;TYPE:Inner wall\nG1 X20 Y20 E1\n"
        ),
    )
    assert rep.area_out_count == 0 and rep.axis_out_count == 0


def test_offset_printable_area(tmp_path: Path) -> None:
    u1 = "; printable_area = 0.5x1,270.5x1,270.5x271,0.5x271\n"
    rep = bounds(
        tmp_path,
        plate("G1 X0.5 Y1 E1\nG1 X0.4 Y100 E1\nG1 X100 Y271.2 E1\n", footer=u1),
        None,
    )
    assert [(v.x, v.y) for v in rep.area_out] == [(0.4, 100.0), (100.0, 271.2)]


def test_r_arcs_and_other_planes_are_unverified(tmp_path: Path) -> None:
    rep = bounds(
        tmp_path, plate("G1 X10 Y10\nG2 X20 Y10 R5\nG18\nG2 X30 Y10 I5 K0\nG17\n")
    )
    assert rep.unverified_count == 2
    names = [c.name for c in preflight.check_moves(rep, K1_LIMITS, "t")]
    assert names[-1] == "unverified"


def test_missing_markers_and_area_are_warnings_not_pass(tmp_path: Path) -> None:
    rep = bounds(tmp_path, "G1 X10 Y10\n" + K1_FOOTER)
    assert preflight.check_moves(rep, K1_LIMITS, "t")[1].status == "WARN"
    rep = bounds(tmp_path, plate("G1 X10 Y10 E1\n", footer=""))
    assert preflight.check_moves(rep, K1_LIMITS, "t")[1].status == "WARN"


def test_parse_limits_and_polygon() -> None:
    assert move_bounds.parse_limits("-5:229,-0.5:227") == K1_LIMITS
    for bad in ("1:2", "5:1,0:1", "a:b,c:d", "1:2,3"):
        with pytest.raises(ValueError):
            move_bounds.parse_limits(bad)
    assert move_bounds.parse_polygon("0x0") is None
    assert move_bounds.parse_polygon("0x0,1x0,1x1") == [(0, 0), (1, 0), (1, 1)]


def test_device_axis_limits_and_limits_from_option(env: Path) -> None:
    from printer_ops import limits_from_option

    (env / "3d-cli").mkdir(parents=True)
    (env / "3d-cli" / "printer-devices.yaml").write_text(
        "k1se: {adapter: creality-stock, host: h, axis_limits: {x: [-5, 229], y: [-0.5, 227]}}\n"
        "u1: {adapter: moonraker, host: h}\n",
        encoding="utf-8",
    )
    assert limits_from_option("k1se", command="t")[0] == K1_LIMITS
    assert limits_from_option("-1:2,-3:4", command="t") == (
        ((-1.0, 2.0), (-3.0, 4.0)),
        "--axis-limits",
    )
    lim, why = limits_from_option("u1", command="t")
    assert lim is None and "no axis_limits for u1" in why
    with pytest.raises(InvalidArgument):
        limits_from_option("5:1,0:1", command="t")
    (env / "3d-cli" / "printer-devices.yaml").write_text(
        "k1se: {adapter: creality-stock, host: h, axis_limits: {x: [229, -5], y: [0, 1]}}\n",
        encoding="utf-8",
    )
    with pytest.raises(InvalidArgument):
        devices.load_devices()


def test_moonraker_reports_klipper_axis_limits(moon: FakeMoonraker) -> None:
    a = moonraker.MoonrakerAdapter("u1", "127.0.0.1", moon.port)
    assert a.axis_limits() == (
        ((-2.0, 272.0), (0.0, 274.0)),
        "Klipper stepper_x/stepper_y",
    )
    moon.settings = {}
    lim, why = a.axis_limits()
    assert lim is None and "position_max" in why
    a.configured_limits = K1_LIMITS
    assert a.axis_limits() == (K1_LIMITS, "printer-devices.yaml")


@pytest.mark.parametrize(
    "fields, norm",
    [
        ({"state": 5, "deviceState": 0}, "paused"),
        ({"state": 4, "deviceState": 0}, "cancelled"),
        ({"state": 7, "deviceState": 0}, "busy"),
        ({"state": 0, "deviceState": 0, "withSelfTest": 40}, "busy"),
        (
            {
                "state": 0,
                "deviceState": 0,
                "err": {"errcode": 2001, "key": 1, "value": ""},
            },
            "error",
        ),
        (
            {
                "state": 1,
                "deviceState": 1,
                "err": {"errcode": 2001, "key": 1, "value": ""},
            },
            "printing",
        ),
    ],
)
def test_creality_extra_state_codes(fields: dict[str, Any], norm: str) -> None:
    st = creality.parse_status({**k1_state(), **fields})
    assert st.state == norm


def test_cmd_start_k1_refuses_a_plate_that_leaves_the_axis(
    k1: FakeCreality,
    env: Path,
    tmp_path: Path,
    gates_ok: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    g = gcode(
        tmp_path,
        plate("G1 X62.83 Y2.774 E1\nG3 Z1.36 I-.359 J-1.79 P1\n"),
        "edge.gcode",
    )
    k1.files[safe_remote_name(g)] = g.stat().st_size
    write_devices(env, k1=k1)
    with pytest.raises(PrinterRefused, match="preflight FAIL"):
        printer("start", "k1se", str(g), "--yes", "--secs", "0.3")
    assert k1.writes() == []
    assert "axis travel" in capsys.readouterr().out


# ---- third review round ---------------------------------------------------------------------


def test_set_gcode_offset_shifts_later_moves(tmp_path: Path) -> None:
    """Klipper applies SET_GCODE_OFFSET to every later move: G-code X10 becomes machine X240."""
    rep = bounds(
        tmp_path, plate("G1 X10 Y10\nSET_GCODE_OFFSET X=230 MOVE=0\nG1 X10 Y10 E1\n")
    )
    assert [(v.x, v.y) for v in rep.axis_out] == [(240.0, 10.0)]
    rep = bounds(tmp_path, plate("G1 X10 Y10\nSET_GCODE_OFFSET X_ADJUST=-20 MOVE=1\n"))
    assert [(v.x, v.y) for v in rep.axis_out] == [
        (-10.0, 10.0)
    ]  # MOVE=1: the head moves there now
    rep = bounds(
        tmp_path, plate("G1 X10 Y10\nSET_GCODE_OFFSET X=5\nG92 X0\nG1 X10 Y10 E1\n")
    )
    assert (
        rep.axis_out_count == 0
        and rep.plate_extent is not None
        and rep.plate_extent[1] == 20.0
    )


def test_extruding_move_from_an_exempt_custom_block_checks_its_start(
    tmp_path: Path,
) -> None:
    """The custom block parks at X-3; the first wall line starts there: plastic outside 0..220."""
    body = ";TYPE:Custom\nG1 X-3 Y10\n;TYPE:Outer wall\nM83\nG1 X10 Y10 E1\n"
    rep = bounds(tmp_path, plate(body), None)
    assert [(v.x, v.y) for v in rep.area_out] == [(-3.0, 10.0)]
    travel = ";TYPE:Custom\nG1 X-3 Y10\n;TYPE:Outer wall\nG1 X10 Y10\n"  # a travel from there is fine
    assert bounds(tmp_path, plate(travel), None).area_out_count == 0


def test_plate_move_with_one_axis_unknown(tmp_path: Path) -> None:
    rep = bounds(tmp_path, plate("G1 X-3\nG1 X10\n", start="G28\n"), None)
    assert [(v.x, v.y) for v in rep.area_out] == [
        (-3.0, None)
    ]  # provably outside 0..220
    assert rep.unverified_count == 1  # X10 alone cannot prove containment


def test_exclude_area_fails_even_without_printable_area(tmp_path: Path) -> None:
    footer = "; bed_exclude_area = 0x0,18x0,18x28,0x28\n"
    rep = bounds(
        tmp_path, plate("M83\nG1 X30 Y40\nG1 X10 Y10 E1\n", footer=footer), None
    )
    checks = preflight.check_moves(rep, None, "")
    assert checks[1].status == "FAIL" and "bed_exclude_area" in checks[1].detail


@pytest.mark.parametrize(
    "reply",
    [
        {"status": ["bad"]},
        {"status": {"configfile": "bad"}},
        {"status": {"configfile": {"settings": {"stepper_x": [], "stepper_y": {}}}}},
    ],
)
def test_moonraker_malformed_config_gives_unknown_limits(
    moon: FakeMoonraker, reply: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    a = moonraker.MoonrakerAdapter("u1", "127.0.0.1", moon.port)
    monkeypatch.setattr(a, "_get", lambda path: reply)
    lim, why = a.axis_limits()
    assert lim is None and "position_max" in why


# ---- fourth review round --------------------------------------------------------------------


def test_line_numbered_gcode_is_checked(tmp_path: Path) -> None:
    rep = bounds(tmp_path, plate("N1 G1 X10 Y10*33\nN2 G1 X300 Y10*12\n"))
    assert [(v.x, v.y) for v in rep.axis_out] == [(300.0, 10.0)]


def test_restore_gcode_state_restores_relative_mode(tmp_path: Path) -> None:
    body = "G1 X200 Y10\nG91\nSAVE_GCODE_STATE NAME=a\nG90\nRESTORE_GCODE_STATE NAME=a\nG1 X100\nG90\n"
    rep = bounds(tmp_path, plate(body))
    assert [(v.x, v.y) for v in rep.axis_out] == [
        (300.0, 10.0)
    ]  # X100 is relative again
    moved = "G1 X10 Y10\nSAVE_GCODE_STATE NAME=p\nG1 X-4.9 Y10\nG1 X300\nRESTORE_GCODE_STATE NAME=p MOVE=1\nG91\nG1 X-20\nG90\n"
    rep = bounds(tmp_path, plate(moved))
    assert [(v.x, v.y) for v in rep.axis_out] == [
        (300.0, 10.0),
        (-10.0, 10.0),
    ]  # MOVE=1 went back to X10


def test_restore_of_an_unknown_state_is_unverified(tmp_path: Path) -> None:
    rep = bounds(
        tmp_path,
        plate("G1 X10 Y10\nRESTORE_GCODE_STATE NAME=macro\nG91\nG1 X10\nG90\n"),
    )
    assert rep.unverified_count >= 1 and "outside this file" in rep.unverified[0]


@pytest.mark.parametrize(
    "body, out",
    [
        ("G1 X10 Y90\nG1 X90 Y10\n", 1),  # travel line through the L's notch
        (
            "G1 X35 Y20\nG3 X35 Y20 I-15 J0\n",
            1,
        ),  # travel circle about (20,20) r=15 crosses the notch corner
        (
            "G1 X25 Y15\nG3 X25 Y15 I-10 J0\n",
            0,
        ),  # circle r=10 stays in the L's corner square
        (
            ";TYPE:Custom\nG1 X50 Y50\n;TYPE:Inner wall\nG1 X10 Y10\n",
            0,
        ),  # enters from a vendor park spot
    ],
)
def test_travel_cutting_a_concave_printable_area_fails(
    tmp_path: Path, body: str, out: int
) -> None:
    """Review case: a travel that starts and ends inside an L-shaped area but leaves it between."""
    l_shape = "; printable_area = 0x0,100x0,100x30,30x30,30x100,0x100\n"
    rep = bounds(tmp_path, plate(body, footer=l_shape), None)
    assert rep.area_out_count == out


# ---- fifth review round ---------------------------------------------------------------------


def test_restore_gcode_state_restores_the_extruder_coordinate(tmp_path: Path) -> None:
    body = (
        "M82\nG92 E0\nG1 X100 Y100\nSAVE_GCODE_STATE NAME=a\nG1 E10\n"
        "RESTORE_GCODE_STATE NAME=a\nG1 X114 Y114 E1\n"
    )
    rep = bounds(tmp_path, plate(body, footer=SQUARE), None)
    assert (
        rep.excluded_in_count == 1
    )  # E back to 0, so E1 extrudes into the excluded square


def test_saved_state_names_are_case_sensitive(tmp_path: Path) -> None:
    body = (
        "G1 X10 Y10\nSET_GCODE_OFFSET X=230\nSAVE_GCODE_STATE NAME=foo\nSET_GCODE_OFFSET X=0\n"
        "SAVE_GCODE_STATE NAME=FOO\nRESTORE_GCODE_STATE NAME=foo\nG1 X10 Y10\n"
    )
    rep = bounds(tmp_path, plate(body))
    assert [(v.x, v.y) for v in rep.axis_out] == [(240.0, 10.0)]


def test_g92_at_an_unknown_position_makes_later_moves_unverified(
    tmp_path: Path,
) -> None:
    rep = bounds(tmp_path, plate("G1 X10 Y10\n", start="G28\nG90\nG92 X0 Y0\n"))
    assert rep.axis_out_count == 0 and rep.unverified_count >= 1
    assert "G92 offset" in rep.unverified[0]


# ---- sixth review round ---------------------------------------------------------------------


def test_clockwise_arc_ending_on_the_start_angle_is_a_full_turn(tmp_path: Path) -> None:
    """Klipper plans `G2 X15 Y10 I10 J0` from (10,10) as a full clockwise turn about (20,10):
    it reaches X30 before ending at X15."""
    rep = bounds(
        tmp_path, plate("G1 X10 Y10\nG2 X15 Y10 I10 J0\n"), ((0.0, 29.0), (0.0, 29.0))
    )
    assert rep.axis_out_count == 1 and rep.axis_out[0].x == pytest.approx(30.0)
    a0, sweep = move_bounds.arc_sweep(10, 10, 15, 10, 10, 0, False)
    assert sweep == 0.0  # the same command counter-clockwise does not turn at all


def test_set_gcode_offset_e_shifts_the_extruder_coordinate(tmp_path: Path) -> None:
    body = "M82\nG92 E0\nG1 X10 Y10 E10\nSET_GCODE_OFFSET E=10\nG1 X14 Y14 E5\n"
    excl = "; printable_area = 0x0,256x0,256x256,0x256\n; bed_exclude_area = 12x12,16x12,16x16,12x16\n"
    rep = bounds(tmp_path, plate(body, footer=excl), None)
    assert rep.excluded_in_count == 1  # logical E is 0 after the offset: E5 extrudes


def test_indented_final_feature_is_still_on_the_plate(tmp_path: Path) -> None:
    text = plate("G1 X10 Y10 E1\n", end=";TYPE:Prime tower\n  G1 X226 Y10 E2\n")
    rep = bounds(tmp_path, text, None)
    assert [(v.x, v.y) for v in rep.area_out] == [(226.0, 10.0)]


# ---- seventh review round -------------------------------------------------------------------


def test_indented_feature_comment_puts_the_move_on_the_plate(tmp_path: Path) -> None:
    text = (
        ";TYPE:Custom\nG28\nG90\n;LAYER_CHANGE\n  ;TYPE:Outer wall\nG1 X10 Y10\nG1 X225 Y10 E1\n"
        "EXCLUDE_OBJECT_END NAME=a\n" + K1_FOOTER
    )
    rep = bounds(tmp_path, text, None)
    assert [(v.x, v.y) for v in rep.area_out] == [(225.0, 10.0)]


def test_first_extrusion_after_homing_still_checks_its_end_against_the_exclude_area(
    tmp_path: Path,
) -> None:
    rep = bounds(
        tmp_path,
        plate("M83\nG1 X114 Y114 E1\n", start="G28\nG90\n", footer=SQUARE),
        None,
    )
    assert rep.excluded_in_count == 1 and rep.unverified_count == 1


def test_offset_and_restore_moves_are_checked_against_the_area(tmp_path: Path) -> None:
    small = "; printable_area = 0x0,20x0,20x20,0x20\n"
    body = "G1 X10 Y10\nSET_GCODE_OFFSET X=15 MOVE=1\nSET_GCODE_OFFSET X=0 MOVE=1\n"
    rep = bounds(tmp_path, plate(body, footer=small), None)
    assert [(v.x, v.y) for v in rep.area_out] == [(25.0, 10.0)]
    l_shape = "; printable_area = 0x0,100x0,100x30,30x30,30x100,0x100\n"
    body = "G1 X10 Y90\nSAVE_GCODE_STATE NAME=s\nG1 X10 Y10\nG1 X90 Y10\nRESTORE_GCODE_STATE NAME=s MOVE=1\n"
    rep = bounds(tmp_path, plate(body, footer=l_shape), None)
    assert (
        rep.area_out_count == 1
    )  # the restore travel from (90,10) back to (10,90) cuts the notch


@pytest.mark.parametrize(
    "body",
    [
        "M82\nG92 E10\nG1 X100 Y100\nSET_GCODE_OFFSET E=-10 MOVE=1\nG1 X114 Y114 E15\n",
        "M82\nG92 E0\nM221 S200\nG1 X10 Y10\nSET_GCODE_OFFSET E=-10\nG1 X14 Y14 E7\n",
    ],
)
def test_unknown_logical_e_counts_e_words_as_extrusion(
    tmp_path: Path, body: str
) -> None:
    excl = (
        SQUARE
        if "X114" in body
        else SQUARE.replace(
            "112x112,116x112,116x116,112x116", "12x12,16x12,16x16,12x16"
        )
    )
    assert bounds(tmp_path, plate(body, footer=excl), None).excluded_in_count == 1


def test_firmware_arc_chords_through_an_exclude_area_fail(tmp_path: Path) -> None:
    """Review case: a 0.5 mm semicircle is ONE 1 mm chord in Klipper, straight through (100,100)."""
    tiny = (
        "; printable_area = 0x0,256x0,256x256,0x256\n"
        "; bed_exclude_area = 99.9x99.9,100.1x99.9,100.1x100.1,99.9x100.1\n"
    )
    rep = bounds(
        tmp_path,
        plate("M83\nG1 X100.5 Y100\nG3 X99.5 Y100 I-0.5 J0 E1\n", footer=tiny),
        None,
    )
    assert rep.excluded_in_count == 1
    chords = move_bounds.Move.arc_move(100.5, 100, 99.5, 100, -0.5, 0, False).chords()
    assert chords is not None and len(chords) == 1


# ---- eighth review round --------------------------------------------------------------------


def test_tool_change_and_activate_extruder_reset_the_extruder_coordinate(
    tmp_path: Path,
) -> None:
    excl = SQUARE.replace("112x112,116x112,116x116,112x116", "12x12,16x12,16x16,12x16")
    for switch in ("ACTIVATE_EXTRUDER EXTRUDER=extruder1", "T1"):
        body = f"M82\nG92 E0\nG1 X10 Y10 E10\n{switch}\nG1 X14 Y14 E5\n"
        assert (
            bounds(tmp_path, plate(body, footer=excl), None).excluded_in_count == 1
        ), switch


def test_extrusion_in_place_inside_the_exclude_area_fails(tmp_path: Path) -> None:
    excl = SQUARE.replace("112x112,116x112,116x116,112x116", "12x12,16x12,16x16,12x16")
    rep = bounds(tmp_path, plate("M83\nG1 X14 Y14\nG1 E10\n", footer=excl), None)
    assert [(v.x, v.y) for v in rep.excluded_in] == [(14.0, 14.0)]


def test_huge_and_helical_arcs_near_the_exclude_area_are_unverified_not_simulated(
    tmp_path: Path,
) -> None:
    huge = "M83\nG1 X100 Y100\nG2 X100 Y100 I100000000 J0 E1\n"
    # inside the circle's bounding box, off its (nearly vertical, x≈100) ideal path near the start
    far = "; printable_area = 0x0,256x0,256x256,0x256\n; bed_exclude_area = 200x0,201x0,201x1,200x1\n"
    rep = bounds(tmp_path, plate(huge, footer=far), None)
    assert any("chords not reproduced" in u for u in rep.unverified)
    tiny = (
        "; printable_area = 0x0,256x0,256x256,0x256\n"
        "; bed_exclude_area = 100.85x100.35,100.86x100.35,100.86x100.36,100.85x100.36\n"
    )
    rep = bounds(
        tmp_path,
        plate("M83\nG1 X101 Y100\nG3 X99 Y100 Z4 I-1 J0 E1\n", footer=tiny),
        None,
    )
    assert rep.excluded_in_count == 0 and any(
        "chords not reproduced" in u for u in rep.unverified
    )


def test_quoted_and_malformed_offset_values(tmp_path: Path) -> None:
    rep = bounds(tmp_path, plate('G1 X10 Y10\nSET_GCODE_OFFSET X="230"\nG1 X10 Y10\n'))
    assert [(v.x, v.y) for v in rep.axis_out] == [(240.0, 10.0)]
    rep = bounds(tmp_path, plate("G1 X10 Y10\nSET_GCODE_OFFSET X=abc\nG1 X10 Y10\n"))
    assert rep.axis_out_count == 0 and any("non-numeric" in u for u in rep.unverified)


# ---- review #132: macros hide moves -------------------------------------------------------


def test_macros_are_recorded_and_fail_unless_trusted(tmp_path: Path) -> None:
    """WIPE_NOZZLE may run `G1 X300` inside: its body is invisible, so an untrusted macro FAILs."""
    rep = bounds(
        tmp_path,
        plate(
            "G1 X10 Y10 E1\nWIPE_NOZZLE\nG1 X20 Y20\nwipe_nozzle SPEED=5\n"
            "SET_VELOCITY_LIMIT ACCEL=5000\nM600\nG4 P10\n"
        ),
    )
    assert rep.macros == {"WIPE_NOZZLE": [2, 9], "M600": [1, 13]}
    macros = preflight.check_macros(rep, frozenset())
    assert [c.status for c in macros] == [
        "FAIL"
    ] and "WIPE_NOZZLE×2 (line 9)" in macros[0].detail
    assert (
        preflight.check_macros(rep, frozenset({"WIPE_NOZZLE", "M600"}))[0].status
        == "PASS"
    )
    assert (
        preflight.check_macros(bounds(tmp_path, plate("G1 X10 Y10 E1\n")), frozenset())
        == []
    )


def test_position_and_e_are_unknown_after_a_macro(tmp_path: Path) -> None:
    rep = bounds(tmp_path, plate("G1 X10 Y10\nPARK_HEAD\nG91\nG1 X5\nG90\n"))
    assert rep.unverified_count == 1 and "unknown position" in rep.unverified[0]


def test_command_word_normalizes() -> None:
    cw = move_bounds.command_word
    assert [
        cw("G01 X1"),
        cw("g1"),
        cw("M0104 S200"),
        cw("T12"),
        cw("my_macro A=1"),
        cw("  ; x"),
        cw(""),
    ] == ["G1", "G1", "M104", "T12", "MY_MACRO", None, None]


def test_trusted_macros_come_from_the_device_config(env: Path, tmp_path: Path) -> None:
    from commands.gcode_bounds import run
    from errors import GateFailure
    from printer_ops import trusted_macros_for

    (env / "3d-cli").mkdir(parents=True)
    (env / "3d-cli" / "printer-devices.yaml").write_text(
        "k1se: {adapter: creality-stock, host: h, axis_limits: {x: [-5, 229], y: [-0.5, 227]},"
        " trusted_macros: [pause, START_PRINT]}\n",
        encoding="utf-8",
    )
    assert trusted_macros_for("k1se") == {"PAUSE", "START_PRINT"}
    assert (
        trusted_macros_for("-5:229,-0.5:227") == frozenset()
        and trusted_macros_for(None) == frozenset()
    )
    g = gcode(tmp_path, plate("G1 X10 Y10 E1\nPAUSE\nG1 X20 Y20\n"), "p.gcode")
    assert run([str(g), "--printer", "k1se"]) == 0
    with pytest.raises(GateFailure):
        run([str(g), "--axis-limits", "-5:229,-0.5:227"])
    (env / "3d-cli" / "printer-devices.yaml").write_text(
        "k1se: {adapter: creality-stock, host: h, trusted_macros: PAUSE}\n",
        encoding="utf-8",
    )
    with pytest.raises(InvalidArgument):
        devices.load_devices()


def test_bridge_limit_comes_from_flag_then_device_then_default(env: Path, tmp_path: Path) -> None:
    from overhang_fixtures import bridge
    from overhang_fixtures import plate as overhang_plate

    from printer_ops import bridge_max_for, device_for

    (env / "3d-cli").mkdir(parents=True)
    (env / "3d-cli" / "printer-devices.yaml").write_text(
        "u1: {adapter: moonraker, host: h, bridge_max_mm: 25}\nk1se: {adapter: creality-stock, host: h}\n",
        encoding="utf-8",
    )
    u1 = device_for("u1")
    assert bridge_max_for("30", u1, command="t") == (30.0, "--bridge-max")
    assert bridge_max_for(None, u1, command="t")[0] == 25.0
    assert bridge_max_for(None, device_for("k1se"), command="t") == (15.0, "default")
    assert bridge_max_for(None, device_for("-5:229,-0.5:227"), command="t") == (15.0, "default")
    with pytest.raises(InvalidArgument):
        bridge_max_for("0", u1, command="t")
    with pytest.raises(InvalidArgument):
        bridge_max_for("inf", u1, command="t")
    # a 20 mm span (the U1 porch door case): FAIL at the default, PASS with the printer's 25
    g = overhang_plate(tmp_path / "door.gcode", {"DOOR": bridge(gap=20.0)})
    at_default = preflight.check_overhangs(g)
    at_u1 = preflight.check_overhangs(g, 25.0, "u1 in printer-devices.yaml")
    assert at_default.status == "FAIL" and "bridge limit from default" in at_default.detail
    assert at_u1.status == "PASS" and "bridge limit from u1 in printer-devices.yaml" in at_u1.detail
    (env / "3d-cli" / "printer-devices.yaml").write_text("u1: {adapter: moonraker, host: h, bridge_max_mm: -1}\n")
    with pytest.raises(InvalidArgument):
        devices.load_devices()


def test_cmd_start_refuses_an_untrusted_macro_without_force(
    moon: FakeMoonraker, env: Path, tmp_path: Path, gates_ok: None, hatch: FakeHatch
) -> None:
    g = gcode(tmp_path, U1_GCODE.replace("G28\n", "G28\nWIPE_NOZZLE\n"))
    moon.put(safe_remote_name(g), g.read_bytes())
    write_devices(env, moon=moon)
    with pytest.raises(PrinterRefused, match="preflight FAIL"):
        printer("start", "u1", str(g), "--yes")
    assert moon.writes() == []
    assert printer("start", "u1", str(g), "--yes", "--force") == 0


def test_cmd_start_runs_the_printops_material_gates(
    moon: FakeMoonraker, env: Path, tmp_path: Path, gates_ok: None, capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`start` runs the same PrintOps gates as `print-preflight --printer` (#164): a plate sliced
    for another printer model is refused even without any 3d-api state (fleet facts only)."""
    fleet = tmp_path / "fleet.yaml"  # the fleet profile only; the address comes from write_devices
    fleet.write_text("u1:\n  model: Snapmaker U1\n  owner: own\n  access: full\n  adapter: moonraker\n"
                     "  host: 127.0.0.1\n  port: 9\n  heads:\n"
                     "    - {label: T1, index: 0, nozzle: {diameter_mm: 0.4, material: stainless, type: std}}\n"
                     "  ams: []\n  enclosure: none\n  chamber_heater: false\n  plates: [textured_pei]\n")
    monkeypatch.setenv("THREED_FLEET", str(fleet))
    monkeypatch.delenv("PRINTOPS_API", raising=False)
    g = gcode(tmp_path, U1_GCODE + "; printer_model = Creality K1 SE\n")
    moon.put(safe_remote_name(g), g.read_bytes())
    write_devices(env, moon=moon)
    with pytest.raises(PrinterRefused, match="preflight FAIL"):
        printer("start", "u1", str(g), "--yes")
    out = capsys.readouterr().out
    assert "printer_profile_match" in out and "'Creality K1 SE'" in out
    assert "fleet facts only" in out
    assert moon.writes() == []


# ---- review #132, second round ------------------------------------------------------------


def test_g1_and_t_lookalike_macros_are_macros(tmp_path: Path) -> None:
    cw = move_bounds.command_word
    assert [
        cw("G1_WIPE"),
        cw("M104_MOVE S1"),
        cw("G1X10Y10"),
        cw("T0"),
        cw("G38.2 X1"),
    ] == ["G1_WIPE", "M104_MOVE", "G1", "T0", "G38.2"]
    rep = bounds(tmp_path, plate("G1 X10 Y10 E1\nG1_WIPE\nT0\n"))
    assert set(rep.macros) == {"G1_WIPE", "T0"}


def test_printer_trust_is_not_borrowed_from_axis_limits_printer(
    env: Path, tmp_path: Path
) -> None:
    from commands.gcode_bounds import run
    from errors import GateFailure

    (env / "3d-cli").mkdir(parents=True)
    (env / "3d-cli" / "printer-devices.yaml").write_text(
        "a: {adapter: moonraker, host: h, axis_limits: {x: [0, 300], y: [0, 300]}}\n"
        "b: {adapter: moonraker, host: h, axis_limits: {x: [0, 300], y: [0, 300]}, trusted_macros: [WIPE]}\n",
        encoding="utf-8",
    )
    g = gcode(tmp_path, plate("G1 X10 Y10 E1\nWIPE\n"), "w.gcode")
    assert run([str(g), "--axis-limits", "b"]) == 0
    with pytest.raises(GateFailure):
        run([str(g), "--printer", "a", "--axis-limits", "b"])


def test_creality_cancel_finishing_on_the_same_file_is_no_ack(k1: FakeCreality) -> None:
    k1.state = k1_state(7, 1, "roof.gcode")
    k1.start_reply = "silent"
    with pytest.raises(PrinterCommError, match="no acknowledgement"):
        k1_adapter(k1).start("roof.gcode")


def test_cmd_start_unknown_state_after_ack_is_unknown(
    moon: FakeMoonraker,
    env: Path,
    tmp_path: Path,
    gates_ok: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    g = gcode(tmp_path)
    moon.put(safe_remote_name(g), g.read_bytes())
    write_devices(env, moon=moon)
    real_status = moonraker.MoonrakerAdapter.status

    def status(self: moonraker.MoonrakerAdapter) -> PrinterStatus:
        st = real_status(self)
        if any(r.path == "/printer/print/start" for r in moon.writes()):
            st.state, st.raw_state = "unknown", "klippy:shutdown"
        return st

    monkeypatch.setattr(moonraker.MoonrakerAdapter, "status", status)
    with pytest.raises(
        PrinterCommError, match="state is unknown.*outcome UNKNOWN"
    ) as info:
        printer("start", "u1", str(g), "--yes")
    assert "Do NOT resend" in info.value.render(color=False)


def test_cmd_start_preflights_the_same_bytes_it_compared(
    moon: FakeMoonraker,
    env: Path,
    tmp_path: Path,
    gates_ok: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review #132: the slicer rewrites FILE after the hash check; preflight must still see the
    bytes that are on the printer (here: off the axis), not the new safe-looking file."""
    a = U1_GCODE.replace("G1 X30 Y30 E1", "G1 X300 Y30 E1")
    g = gcode(tmp_path, a)
    moon.put(safe_remote_name(g), g.read_bytes())
    write_devices(env, moon=moon)
    real_hash = moonraker.MoonrakerAdapter.remote_sha256

    def hash_then_rewrite(self: moonraker.MoonrakerAdapter, name: str) -> str | None:
        digest = real_hash(self, name)
        g.write_text(
            a.replace("X300", "X030"), encoding="utf-8"
        )  # same length, now in bounds
        return digest

    monkeypatch.setattr(moonraker.MoonrakerAdapter, "remote_sha256", hash_then_rewrite)
    with pytest.raises(PrinterRefused, match="preflight FAIL"):
        printer("start", "u1", str(g), "--yes")
    assert moon.writes() == []


def test_ws_connection_reset_counts_as_closed() -> None:
    a, b = socket.socketpair()
    try:
        client = ws.WebSocket(a)
        b.close()
        with pytest.raises(ws.WebSocketClosed):
            client.recv(1.0)
        with pytest.raises(ws.WebSocketClosed):
            for _ in range(50):  # the first sends may still be buffered by the kernel
                client.send_text("x" * 65536)
    finally:
        a.close()


def test_cmd_upload_sends_the_bytes_it_named(
    moon: FakeMoonraker, env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review #132: a rewrite of FILE between naming and sending must not store other bytes
    under the first content's hash."""
    g = gcode(tmp_path)
    original = g.read_bytes()
    write_devices(env, moon=moon)
    real_status = moonraker.MoonrakerAdapter.status

    def status_then_rewrite(self: moonraker.MoonrakerAdapter) -> PrinterStatus:
        st = real_status(self)
        g.write_bytes(original.replace(b"X10", b"X99"))  # same length, other content
        return st

    monkeypatch.setattr(moonraker.MoonrakerAdapter, "status", status_then_rewrite)
    assert printer("upload", "u1", str(g)) == 0
    import hashlib

    (stored_name,) = moon.contents
    assert moon.contents[stored_name] == original
    assert stored_name == f"part-{hashlib.sha256(original).hexdigest()[:16]}.gcode"
