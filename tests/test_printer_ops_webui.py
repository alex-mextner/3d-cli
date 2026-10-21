"""printer_ops.webui: which web UIs of a printer answer, identified by their page; credential-free URLs."""

from __future__ import annotations

from pathlib import Path

import pytest
from printer_fakes import (
    FLUIDD_HTML,
    MAINSAIL_HTML,
    FakeMoonraker,
    FakeWebServer,
    free_port,
)

from errors import InvalidArgument
from printer_ops.devices import Device
from printer_ops.webui import identify, page_title, probe, redact_url, web_ports


def _device(adapter: str, port: int) -> Device:
    return Device("p", adapter, "127.0.0.1", port, None, Path("printer-devices.yaml"))


@pytest.mark.parametrize(
    ("html", "kind"),
    [
        (FLUIDD_HTML, "Fluidd"),
        (MAINSAIL_HTML, "Mainsail"),
        ("<html><head><title>OctoPrint Login</title></head></html>", "OctoPrint"),
        ("<html><head><title>Creality K1 SE</title></head></html>", "Creality"),
        # an SPA shell without a useful title is still told apart by its bundle names
        (
            "<html><title>Printer</title><script src=/assets/mainsail-1.js></script></html>",
            "Mainsail",
        ),
        ("<html><head><title>nginx welcome</title></head></html>", "web page"),
        ("no markup at all", "web page"),
    ],
)
def test_identify_tells_the_web_uis_apart(html: str, kind: str) -> None:
    assert identify(page_title(html), html) == kind


def test_page_title_is_one_trimmed_line() -> None:
    assert page_title("<TITLE>\n  Fluidd \n  UI </TITLE>") == "Fluidd UI"
    assert page_title("<html></html>") is None


def test_redact_url_drops_credentials_and_masks_secret_query_values() -> None:
    url = "http://user:pw@cam.local:8080/?action=snapshot&token=abc123&api_key=k&fps=5"
    out = redact_url(url)
    assert (
        "user" not in out
        and "pw" not in out
        and "abc123" not in out
        and "api_key=k" not in out
    )
    assert (
        out.startswith("http://cam.local:8080/?")
        and "action=snapshot" in out
        and "fps=5" in out
    )


@pytest.mark.parametrize("url", ["http://cam/snapshot.cgi?usr=admin&pwd=hunter2&res=1", "http://cam/?user=a&passwd=b"])
def test_redact_url_masks_camera_style_credential_parameters(url: str) -> None:
    out = redact_url(url)
    assert "hunter2" not in out and "admin" not in out and "passwd=b" not in out and "user=a" not in out


def test_redact_url_does_not_echo_an_unparseable_url() -> None:
    assert redact_url("http://cam:80800/?token=SECRET") == "(invalid URL)"
    assert redact_url("http://[cam/?token=SECRET") == "(invalid URL)"


def test_web_ports_default_and_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("THREED_WEBUI_PORTS", raising=False)
    assert web_ports() == (80, 4408, 4409)
    monkeypatch.setenv("THREED_WEBUI_PORTS", "8080, 8081, 8080")
    assert web_ports() == (8080, 8081)


@pytest.mark.parametrize("value", ["not-a-port", "0", "70000", "-1", ",", "1,2,3,4,5,6,7,8,9"])
def test_a_bad_web_ports_override_is_an_invalid_argument(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("THREED_WEBUI_PORTS", value)
    with pytest.raises(InvalidArgument, match="THREED_WEBUI_PORTS"):
        web_ports()


def test_a_trailing_comma_in_the_web_ports_override_is_harmless(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("THREED_WEBUI_PORTS", "80,")
    assert web_ports() == (80,)


def test_redact_url_keeps_value_less_query_parameters() -> None:
    assert "snapshot" in redact_url("http://cam/cgi-bin/snapshot.cgi?snapshot&res=1")


def test_probe_lists_only_what_answered_in_probe_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    moon, fluidd, mainsail = (
        FakeMoonraker(),
        FakeWebServer(FLUIDD_HTML),
        FakeWebServer(MAINSAIL_HTML),
    )
    gone = free_port()
    with moon.serve(), fluidd.serve(), mainsail.serve():
        monkeypatch.setenv(
            "THREED_WEBUI_PORTS", f"{mainsail.port},{gone},{fluidd.port}"
        )
        result = probe(_device("moonraker", moon.port))

    assert [(w.kind, w.url) for w in result.found] == [
        ("Mainsail", f"http://127.0.0.1:{mainsail.port}/"),
        ("Fluidd", f"http://127.0.0.1:{fluidd.port}/"),
        ("Moonraker API", f"http://127.0.0.1:{moon.port}/"),
    ]
    assert result.found[2].note == "Moonraker v0.9.3-fake, klippy ready"
    assert len(result.errors) == 1 and str(gone) in result.errors[0]
    assert result.tried == 4


def test_probe_skips_a_server_that_has_no_page_but_flags_a_login_wall(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing, locked = (
        FakeWebServer(status=404),
        FakeWebServer(html="<title>OctoPrint</title>", status=401),
    )
    with missing.serve(), locked.serve():
        monkeypatch.setenv("THREED_WEBUI_PORTS", f"{missing.port},{locked.port}")
        result = probe(_device("moonraker", free_port()))

    assert [(w.kind, w.http_status, w.note) for w in result.found] == [
        ("OctoPrint", 401, "login required")
    ]
    assert result.tried == 3  # the dead Moonraker API root counts as tried and failed
    assert len(result.errors) == 1


def test_probe_of_a_creality_stock_printer_asks_only_its_built_in_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from printer_fakes import FakeCreality

    k1 = FakeCreality()
    unrelated = FakeWebServer()
    with k1.serve(), unrelated.serve():
        monkeypatch.setenv(
            "THREED_WEBUI_PORTS", str(unrelated.port)
        )  # a moonraker-only probe list
        result = probe(_device("creality-stock", k1.http_port))

    assert [(w.kind, w.title) for w in result.found] == [("Creality", "Creality K1 SE")]
    assert unrelated.requests == []
