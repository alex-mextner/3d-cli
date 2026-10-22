from pathlib import Path
import sys
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from printer_ops.devices import Device
from printer_ops.watch_run import settings_for
from printer_ops.watch_sinks import Dispatcher
from errors import InvalidArgument


def device(watch):
    return Device(
        name="u1",
        adapter="moonraker",
        host="printer",
        port=7125,
        model="Snapmaker U1",
        source=Path("fixture"),
        watch=watch,
    )


def test_fallback_is_on_unless_explicitly_disabled():
    d = Dispatcher()
    d.add_settings("u1", settings_for(device({}), {}))
    assert any(r.sink.name == "printer-agent" for r in d.routes.values())


def test_operator_can_disable_auto_agent_spawn_without_disabling_subscribers():
    cfg = settings_for(device({"agent_fallback": False, "tg_fallback": False}), {})
    d = Dispatcher()
    d.add_settings("u1", cfg)
    assert any(r.sink.name == "subscriptions" for r in d.routes.values())
    assert not any(r.sink.name in ("printer-agent", "tg") for r in d.routes.values())


@pytest.mark.parametrize("value", ["false", 0, None, []])
def test_non_boolean_policy_is_rejected(value):
    with pytest.raises(InvalidArgument):
        settings_for(device({"agent_fallback": value}), {})
