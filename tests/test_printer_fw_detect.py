"""Probe kinds and state detection (fake HTTP/ssh/sockets; no network)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from printer_fw.detect import Detection, detect_state
from printer_fw.probes import (
    ERROR,
    FAIL,
    OK,
    SKIPPED,
    Endpoint,
    evaluate,
    evaluate_group,
    poll,
)
from printer_fw.profiles import Probe, Profile, SshSettings, load_profiles
from printer_fw_fakes import FakeHttp, FakeRunner, ep_of, make_backends, target

PROFILES = load_profiles()
WEB, MOON, SSH = 80, 7125, 22
SSH_SETTINGS = SshSettings("root", "key only")
EP = Endpoint(
    "10.0.0.5", {"web": WEB, "moon": MOON, "ssh": SSH}, "moonraker", SSH_SETTINGS
)


def probe(
    kind: str,
    *,
    service: str = "",
    path: str = "",
    json_path: str = "",
    equals: str | None = None,
    regex: str | None = None,
    command: str = "",
    negate: bool = False,
    expect_status: tuple[int, ...] = (200,),
    prompt: str = "",
) -> Probe:
    return Probe(
        kind=kind,
        id="p",
        title="p",
        negate=negate,
        service=service,
        path=path,
        expect_status=expect_status,
        json_path=json_path,
        equals=equals,
        regex=regex,
        command=command,
        prompt=prompt,
    )


def run_probe(
    p: Probe,
    http: FakeHttp | None = None,
    runner: FakeRunner | None = None,
    *,
    use_ssh: bool = True,
    confirm: Callable[[str], bool] | None = None,
) -> tuple[str, str]:
    be, *_ = make_backends(runner, http, use_ssh=use_ssh, confirm=confirm)
    r = evaluate(p, EP, be)
    return r.outcome, r.detail


# ---- probe kinds -----------------------------------------------------------------------------


def test_http_get_status_json_path_and_regex() -> None:
    http = FakeHttp()
    http.set(
        "GET",
        MOON,
        "/info",
        200,
        {"result": {"state": "ready", "items": [{"v": "1.6.0"}]}},
    )
    ok = probe(
        "http_get",
        service="moon",
        path="/info",
        json_path="result.state",
        equals="ready",
    )
    assert run_probe(ok, http)[0] == OK
    assert (
        run_probe(
            probe(
                "http_get",
                service="moon",
                path="/info",
                json_path="result.items.0.v",
                regex=r"^1\.6",
            ),
            http,
        )[0]
        == OK
    )
    assert (
        run_probe(
            probe(
                "http_get",
                service="moon",
                path="/info",
                json_path="result.state",
                equals="error",
            ),
            http,
        )[0]
        == FAIL
    )
    assert (
        run_probe(
            probe("http_get", service="moon", path="/info", json_path="result.nothing"),
            http,
        )[0]
        == FAIL
    )
    assert (
        run_probe(probe("http_get", service="moon", path="/info", regex="ready"), http)[
            0
        ]
        == OK
    )
    assert (
        run_probe(probe("http_get", service="moon", path="/missing"), http)[0] == FAIL
    )  # HTTP 404


def test_http_get_expected_status_list_and_non_json_body() -> None:
    http = FakeHttp()
    http.set("GET", WEB, "/gone", 403, "forbidden")
    assert (
        run_probe(
            probe("http_get", service="web", path="/gone", expect_status=(403, 404)),
            http,
        )[0]
        == OK
    )
    assert run_probe(probe("http_get", service="web", path="/gone"), http)[0] == FAIL
    http.set("GET", WEB, "/text", 200, "plain text")
    out, detail = run_probe(
        probe("http_get", service="web", path="/text", json_path="a"), http
    )
    assert out == FAIL and "not JSON" in detail


def test_a_refused_connection_is_a_definite_fail_but_a_timeout_is_an_error() -> None:
    assert (
        run_probe(probe("http_get", service="web", path="/"), FakeHttp())[0] == FAIL
    )  # nothing listens

    class Slow(FakeHttp):
        def __call__(self, *a: object, **k: object) -> tuple[int, bytes]:
            raise TimeoutError("timed out")

    assert run_probe(probe("http_get", service="web", path="/"), Slow())[0] == ERROR


def test_negate_flips_ok_and_fail_but_not_error_or_skipped() -> None:
    http = FakeHttp()
    http.set("GET", WEB, "/x", 200, "")
    assert (
        run_probe(probe("http_get", service="web", path="/x", negate=True), http)[0]
        == FAIL
    )
    assert (
        run_probe(probe("http_get", service="web", path="/y", negate=True), http)[0]
        == OK
    )

    class Slow(FakeHttp):
        def __call__(self, *a: object, **k: object) -> tuple[int, bytes]:
            raise TimeoutError("t")

    assert (
        run_probe(probe("http_get", service="web", path="/x", negate=True), Slow())[0]
        == ERROR
    )
    assert (
        run_probe(probe("ssh_file_exists", path="/a", negate=True), use_ssh=False)[0]
        == SKIPPED
    )


def test_adapter_kind_compares_the_registry_adapter() -> None:
    assert run_probe(probe("adapter_kind", equals="moonraker"))[0] == OK
    assert run_probe(probe("adapter_kind", equals="creality-stock"))[0] == FAIL


def test_tcp_open_and_banner() -> None:
    http = FakeHttp()
    http.set("GET", WEB, "/", 200, "")
    assert run_probe(probe("tcp_open", service="web"), http)[0] == OK
    assert run_probe(probe("tcp_open", service="moon"), http)[0] == FAIL
    http.routes[("BANNER", SSH, "")] = (200, "SSH-2.0-dropbear_2020.81")
    assert (
        run_probe(probe("tcp_banner", service="ssh", regex=r"^SSH-2\.0-"), http)[0]
        == OK
    )
    http.routes[("BANNER", SSH, "")] = (200, "")
    out, detail = run_probe(
        probe("tcp_banner", service="ssh", regex=r"^SSH-2\.0-"), http
    )
    assert out == FAIL and "no banner" in detail
    http.routes[("BANNER", SSH, "")] = (200, "HTTP/1.1 400")
    assert (
        run_probe(probe("tcp_banner", service="ssh", regex=r"^SSH-2\.0-"), http)[0]
        == FAIL
    )


def test_ssh_probes_distinguish_missing_from_unreachable() -> None:
    runner = (
        FakeRunner()
        .on("test -e /there", rc=0)
        .on("test -e /absent", rc=1)
        .on("test -e /noauth", rc=255, err="Permission denied (publickey).")
    )
    assert run_probe(probe("ssh_file_exists", path="/there"), runner=runner)[0] == OK
    assert run_probe(probe("ssh_file_exists", path="/absent"), runner=runner)[0] == FAIL
    out, detail = run_probe(probe("ssh_file_exists", path="/noauth"), runner=runner)
    assert out == ERROR and "Permission denied" in detail


def test_ssh_command_checks_exit_code_and_output() -> None:
    runner = FakeRunner().on("cat /etc/v", rc=0, out="version=1.3.5.22\n")
    assert (
        run_probe(
            probe("ssh_command", command="cat /etc/v", regex=r"1\.3\.5"), runner=runner
        )[0]
        == OK
    )
    assert (
        run_probe(
            probe("ssh_command", command="cat /etc/v", regex=r"2\.0"), runner=runner
        )[0]
        == FAIL
    )
    assert (
        run_probe(probe("ssh_command", command="cat /etc/other"), runner=runner)[0]
        == FAIL
    )  # rc 1 by default


def test_ssh_probes_are_skipped_without_ssh_and_never_contact_the_runner() -> None:
    runner = FakeRunner()
    assert (
        run_probe(probe("ssh_command", command="true"), runner=runner, use_ssh=False)[0]
        == SKIPPED
    )
    assert runner.calls == []


def test_confirm_needs_a_human() -> None:
    p = probe("confirm", prompt="done?")
    assert run_probe(p)[0] == SKIPPED
    assert run_probe(p, confirm=lambda q: True)[0] == OK
    assert run_probe(p, confirm=lambda q: False)[0] == FAIL


def test_group_all_stops_at_the_first_failure_and_any_at_the_first_success() -> None:
    http = FakeHttp()
    http.set("GET", WEB, "/a", 200, "")
    a, b = (
        probe("http_get", service="web", path="/a"),
        probe("http_get", service="web", path="/b"),
    )
    be, *_ = make_backends(http=http)
    ok, results = evaluate_group([b, a], "all", EP, be)
    assert not ok and len(results) == 1
    ok, results = evaluate_group([b, a], "any", EP, be)
    assert ok and len(results) == 2
    assert evaluate_group([], "all", EP, be)[0] is True
    assert evaluate_group([], "any", EP, be)[0] is False


def test_poll_waits_for_a_rebooting_printer_and_gives_up_at_the_deadline() -> None:
    http = FakeHttp()
    be, *_ = make_backends(http=http)
    p = probe("http_get", service="web", path="/up")
    hits = {"n": 0}

    def up() -> tuple[int, str]:
        hits["n"] += 1
        return (200, "") if hits["n"] >= 3 else (503, "")

    http.routes[("GET", WEB, "/up")] = up
    ok, _ = poll([p], EP, be, wait_s=60, interval_s=5)
    assert ok and hits["n"] == 3
    http.routes[("GET", WEB, "/up")] = (503, "")
    ok, results = poll([p], EP, be, wait_s=12, interval_s=5)
    assert not ok and results[0].outcome == FAIL


# ---- detection on the shipped profiles ---------------------------------------------------------


def u1_world(*, extended: bool = False) -> FakeHttp:
    http = FakeHttp()
    http.set("GET", MOON, "/machine/system_info", 200, {"result": {}})
    http.set("GET", WEB, "/", 200, "<html>fluidd</html>")
    if extended:
        http.set(
            "GET",
            WEB,
            "/firmware-config/api/status",
            200,
            {
                "version": {
                    "title": "System Information",
                    "items": [{"label": "Base", "value": "1.6.0"}],
                }
            },
        )
    return http


def detect(
    profile_id: str,
    http: FakeHttp,
    runner: FakeRunner | None = None,
    *,
    model: str,
    adapter: str = "moonraker",
    port: int = MOON,
    use_ssh: bool = True,
) -> tuple[Detection, FakeRunner]:
    profile = PROFILES[profile_id]
    tgt = target(model=model, adapter=adapter, port=port)
    be, r, *_ = make_backends(runner, http, use_ssh=use_ssh)
    return detect_state(profile, ep_of(profile, tgt), be), r


def detect_k1(
    http: FakeHttp, runner: FakeRunner | None = None, *, use_ssh: bool = True
) -> tuple[Detection, FakeRunner]:
    return detect(
        "creality-k1",
        http,
        runner,
        model="Creality K1 SE",
        adapter="creality-stock",
        port=WEB,
        use_ssh=use_ssh,
    )


def test_u1_stock_vs_extended_vs_unreachable() -> None:
    stock, _ = detect("snapmaker-u1", u1_world(), model="Snapmaker U1")
    assert stock.state == "stock" and stock.matched == ("stock",)
    ext, _ = detect("snapmaker-u1", u1_world(extended=True), model="Snapmaker U1")
    assert ext.state == "extended" and ext.matched == ("extended",)
    down, _ = detect("snapmaker-u1", FakeHttp(), model="Snapmaker U1")
    assert (
        down.state is None
        and down.matched == ()
        and any("no state matched" in n for n in down.notes)
    )


def k1_world(*, banner: bool = False, moonraker: bool = False) -> FakeHttp:
    http = FakeHttp()
    http.set("GET", WEB, "/info", 200, {"model": "K1 SE"})
    http.set("GET", 4408, "/", 200, "<html/>")
    if banner:
        http.routes[("BANNER", SSH, "")] = (200, "SSH-2.0-dropbear_2020.81")
    else:
        http.set("GET", SSH, "/", 200, "")  # port open, no banner
    if moonraker:
        http.set(
            "GET", MOON, "/server/info", 200, {"result": {"klippy_state": "ready"}}
        )
    return http


def test_k1_walks_stock_rooted_helper_klipper() -> None:
    stock, r = detect_k1(k1_world())
    assert stock.state == "stock"
    assert r.calls == []  # no ssh banner -> the ssh states are not even probed

    rooted_runner = FakeRunner().on("test -d /usr/data", rc=0)
    rooted, _ = detect_k1(k1_world(banner=True), rooted_runner)
    assert rooted.state == "rooted" and rooted.matched == ("stock", "rooted")

    helper_runner = (
        FakeRunner()
        .on("test -d /usr/data", rc=0)
        .on("test -e /usr/data/helper-script/helper.sh", rc=0)
    )
    helper, _ = detect_k1(k1_world(banner=True), helper_runner)
    assert helper.state == "helper-installed"

    ready, _ = detect_k1(k1_world(banner=True, moonraker=True), helper_runner)
    assert ready.state == "klipper-ready" and ready.matched == (
        "stock",
        "rooted",
        "helper-installed",
        "klipper-ready",
    )


def test_a_banner_without_a_working_key_is_a_partial_detection_not_rooted() -> None:
    runner = FakeRunner().on(
        "test -d /usr/data", rc=255, err="Permission denied (publickey)."
    )
    det, _ = detect_k1(k1_world(banner=True), runner)
    assert det.state == "stock" and det.partial
    assert any("could not tell" in n for n in det.notes)


def test_no_ssh_skips_ssh_probes_and_says_so() -> None:
    det, r = detect_k1(k1_world(banner=True), FakeRunner(), use_ssh=False)
    assert det.state == "stock" and det.partial and r.calls == []


def test_bambu_states_are_not_machine_detectable() -> None:
    det, _ = detect("bambu-lab-p1-a1", FakeHttp(), model="Bambu Lab P1S")
    assert det.state is None and any("--from" in n for n in det.notes)


def test_detection_json_carries_every_probe_outcome() -> None:
    det, _ = detect("snapmaker-u1", u1_world(), model="Snapmaker U1")
    doc: dict[str, Any] = det.to_json()
    assert doc["state"] == "stock"
    evidence = {e["state"]: e for e in doc["evidence"]}
    assert evidence["extended"]["matched"] is False
    assert evidence["stock"]["probes"][0]["outcome"] == "ok"


@pytest.mark.parametrize("profile", list(PROFILES.values()), ids=lambda p: p.id)
def test_every_profile_detects_only_with_known_services(profile: Profile) -> None:
    services = set(profile.services)
    for s in profile.states:
        for p in s.detection.probes if s.detection else ():
            assert not p.service or p.service in services
