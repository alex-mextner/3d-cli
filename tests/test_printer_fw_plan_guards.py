"""BFS planning (reachable and unreachable targets, --also) and the machine-checked guards."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest
import yaml
from errors import GateFailure, InvalidArgument, UsageError
from printer_fw import guards
from printer_fw.engine import plan_for
from printer_fw.plan import (
    Plan,
    Unreachable,
    build_plan,
    reachable_states,
    shortest_path,
)
from printer_fw.probes import Endpoint
from printer_fw.profiles import (
    DATA_DIR,
    Profile,
    ProfileError,
    Transition,
    load_profiles,
    parse_profile,
)
from printer_fw_fakes import FakeHttp, context, make_backends, target

PROFILES = load_profiles()
K1, U1, BAMBU = (
    PROFILES["creality-k1"],
    PROFILES["snapmaker-u1"],
    PROFILES["bambu-lab-p1-a1"],
)


def with_step(profile: Profile, step_id: str, **changes: Any) -> Profile:
    def patch(steps: tuple[Any, ...]) -> tuple[Any, ...]:
        return tuple(replace(s, **changes) if s.id == step_id else s for s in steps)

    return replace(
        profile,
        transitions=tuple(
            replace(t, steps=patch(t.steps), optional_steps=patch(t.optional_steps))
            for t in profile.transitions
        ),
    )


# ---- planning --------------------------------------------------------------------------------


def test_the_shortest_path_is_the_chain_of_transitions() -> None:
    plan = build_plan(K1, "stock", "klipper-ready")
    assert [t.id for t in plan.transitions] == [
        "enable-root",
        "install-helper-script",
        "install-moonraker",
    ]
    assert plan.step_ids() == [
        "enable-root-on-screen",
        "install-ssh-key",
        "fetch-helper-script",
        "remove-preinstalled-moonraker-fluidd",
        "install-moonraker-nginx",
        "install-fluidd",
        "narrow-trusted-clients",
    ]
    assert (
        build_plan(K1, "helper-installed", "klipper-ready").transitions[0].id
        == "install-moonraker"
    )


def test_a_target_equal_to_the_current_state_is_an_empty_plan() -> None:
    plan = build_plan(K1, "rooted", "rooted")
    assert plan.empty and plan.transitions == ()


def test_an_unreachable_target_is_a_refusal_listing_what_is_reachable() -> None:
    with pytest.raises(Unreachable) as err:
        build_plan(K1, "klipper-ready", "stock")
    assert isinstance(err.value, GateFailure) and err.value.exit_code == 1
    assert "no path from 'klipper-ready' to 'stock'" in err.value.message
    assert shortest_path(K1, "klipper-ready", "stock") is None
    assert reachable_states(K1, "stock") == [
        "rooted",
        "helper-installed",
        "klipper-ready",
    ]
    assert reachable_states(K1, "klipper-ready") == []


def test_an_unknown_state_is_an_invalid_argument_with_the_accepted_states() -> None:
    with pytest.raises(InvalidArgument) as err:
        build_plan(K1, "stock", "warp-drive")
    assert err.value.exit_code == 2 and "klipper-ready" in err.value.message


def test_klipper_is_refused_honestly_on_bambu() -> None:
    with pytest.raises(InvalidArgument) as err:
        build_plan(BAMBU, "stock", "klipper-ready")
    assert any("Klipper cannot be installed" in r for r in err.value.remediation)


def test_bambu_offers_only_what_is_possible() -> None:
    plan = build_plan(BAMBU, "stock", "developer-mode")
    assert plan.step_ids() == [
        "enable-lan-only-mode",
        "enable-developer-mode-on-screen",
    ]
    assert all(p.step.kind == "manual" and not p.step.automatable for p in plan.steps)


def test_breadth_first_prefers_the_shorter_path() -> None:
    data = yaml.safe_load((DATA_DIR / "creality-k1.yaml").read_text())
    short = {
        **data["transitions"][0],
        "id": "shortcut",
        "from": "stock",
        "to": "klipper-ready",
    }
    short["steps"] = [{**short["steps"][0], "id": "shortcut-step"}]
    data["transitions"].append(short)
    data["guards"] = [g for g in data["guards"] if g["kind"] != "step_requires_step"]
    profile = parse_profile(data, "k1.yaml")
    assert [
        t.id for t in build_plan(profile, "stock", "klipper-ready").transitions
    ] == ["shortcut"]


def test_also_adds_optional_steps_of_transitions_on_the_path_only() -> None:
    plan = build_plan(
        K1, "helper-installed", "klipper-ready", ["install-save-zoffset-macros"]
    )
    assert (
        plan.step_ids()[-1] == "install-save-zoffset-macros" and plan.steps[-1].optional
    )
    with pytest.raises(InvalidArgument) as err:
        build_plan(
            K1, "klipper-ready", "klipper-ready", ["install-save-zoffset-macros"]
        )
    assert "not on the path" in err.value.remediation[0]
    with pytest.raises(InvalidArgument):
        build_plan(K1, "stock", "klipper-ready", ["not-a-step"])


def test_maintenance_transitions_carry_optional_steps_for_the_state_they_act_on() -> (
    None
):
    plan = build_plan(U1, "extended", "extended", ["upgrade-extended-firmware"])
    assert plan.step_ids() == ["upgrade-extended-firmware"]
    after_install = build_plan(U1, "stock", "extended", ["reset-extended-to-defaults"])
    assert after_install.step_ids() == [
        "local-update-extended-firmware",
        "reset-extended-to-defaults",
    ]
    assert all(isinstance(t, Transition) for t in after_install.transitions)


def test_plan_for_never_touches_the_printer_when_the_state_is_given() -> None:
    be, runner, http, upload = make_backends()
    ctx = context(U1, target(model="Snapmaker U1"), be)
    report = plan_for(ctx, "extended", (), "stock")
    assert (
        report.plan.step_ids() == ["local-update-extended-firmware"]
        and report.detection is None
    )
    assert runner.calls == [] and http.requests == [] and upload.calls == []


def test_plan_for_detects_with_read_only_probes_and_executes_nothing() -> None:
    http = FakeHttp()
    http.set("GET", 7125, "/machine/system_info", 200, {"result": {}})
    http.set("GET", 80, "/", 200, "")
    be, runner, http, upload = make_backends(http=http)
    ctx = context(U1, target(model="Snapmaker U1"), be)
    report = plan_for(ctx, "extended", (), None)
    assert report.detection is not None and report.detection.state == "stock"
    assert http.mutations() == [] and runner.mutating() == [] and upload.calls == []


def test_plan_for_an_offline_printer_needs_an_assumed_state() -> None:
    be, runner, http, _ = make_backends()
    ctx = context(BAMBU, target(model="Bambu Lab P1S", access="offline"), be)
    with pytest.raises(UsageError, match="offline fleet printer"):
        plan_for(ctx, "developer-mode", (), None)
    assert plan_for(ctx, "developer-mode", (), "stock").plan.step_ids()
    assert runner.calls == [] and http.requests == []


def test_plan_for_reports_a_forbidden_also_instead_of_a_plan() -> None:
    be, *_ = make_backends()
    ctx = context(
        K1, target(model="Creality K1 SE", adapter="creality-stock", port=80), be
    )
    report = plan_for(
        ctx, "klipper-ready", ("remove-creality-web-interface",), "helper-installed"
    )
    assert [v.guard.id for v in report.violations] == [
        "no-remove-creality-web-interface"
    ]
    assert report.plan.empty


# ---- guards: forbidden options -----------------------------------------------------------------


def test_forbidden_helper_script_actions_are_refused_with_the_runbook_reason() -> None:
    bad = guards.forbidden_options(
        K1,
        ["remove-creality-web-interface", "install-guppy-screen"],
        "helper-installed",
        "klipper-ready",
    )
    assert {v.guard.id for v in bad} == {
        "no-remove-creality-web-interface",
        "no-guppy-screen",
    }
    text = bad[0].render()
    assert (
        "web-server" in text
        and "creality-stock adapter" in text
        and "k1se-root-upgrade.md" in text
    )


def test_allowed_options_pass_the_forbidden_option_guards() -> None:
    assert (
        guards.forbidden_options(
            K1,
            ["install-usb-camera", "install-save-zoffset-macros"],
            "helper-installed",
            "klipper-ready",
        )
        == []
    )


def test_u1_login_switch_and_factory_reset_are_refused() -> None:
    ids = {
        v.guard.id
        for v in guards.forbidden_options(
            U1, ["enable-require-login", "factory-reset"], "extended", "extended"
        )
    }
    assert ids == {"no-require-login", "no-touchscreen-factory-reset"}


def test_applies_when_limits_a_guard_to_its_states() -> None:
    assert guards.forbidden_options(U1, ["factory-reset"], "stock", "extended") == []
    assert guards.forbidden_options(U1, ["enable-require-login"], "stock", "extended")


def test_a_plan_that_contains_a_forbidden_step_id_is_refused() -> None:
    data = yaml.safe_load((DATA_DIR / "creality-k1.yaml").read_text())
    steps = data["transitions"][2]["steps"]  # install-moonraker transition
    steps.append({**steps[0], "id": "remove-creality-web-interface"})
    profile = parse_profile(data, "k1.yaml")
    plan = build_plan(profile, "helper-installed", "klipper-ready")
    assert [v.guard.id for v in guards.plan_violations(profile, plan)] == [
        "no-remove-creality-web-interface"
    ]


# ---- guards: command and step requirements -------------------------------------------------------


def test_fluidd_install_must_fix_permissions_at_load_and_at_plan_time() -> None:
    data = yaml.safe_load((DATA_DIR / "creality-k1.yaml").read_text())
    for t in data["transitions"]:
        for s in t["steps"]:
            if s["id"] == "install-fluidd":
                s["command"] = s["command"].replace(
                    "chmod -R a+rX /usr/data/fluidd", "true"
                )
    with pytest.raises(ProfileError, match="does not match /chmod -R a"):
        parse_profile(data, "k1.yaml")
    broken = with_step(
        K1, "install-fluidd", command="set -e\n/usr/data/k1drive.sh install_fluidd\n"
    )
    plan = build_plan(broken, "helper-installed", "klipper-ready")
    assert [v.guard.id for v in guards.plan_violations(broken, plan)] == [
        "fluidd-needs-world-readable"
    ]
    ok_plan = build_plan(K1, "helper-installed", "klipper-ready")
    assert guards.plan_violations(K1, ok_plan) == []


def test_moonraker_install_requires_the_trusted_clients_step_in_the_plan() -> None:
    full = build_plan(K1, "helper-installed", "klipper-ready")
    assert guards.plan_violations(K1, full) == []
    trimmed = replace(
        full,
        steps=tuple(s for s in full.steps if s.step.id != "narrow-trusted-clients"),
    )
    assert [v.guard.id for v in guards.plan_violations(K1, trimmed)] == [
        "narrow-trusted-clients-after-moonraker"
    ]


def test_ssl_verification_may_not_be_switched_off() -> None:
    plan = build_plan(K1, "rooted", "helper-installed")
    assert guards.plan_violations(K1, plan) == []
    bad = with_step(
        K1,
        "fetch-helper-script",
        command="git config --global http.sslVerify false\ngit clone x y\n",
    )
    assert [
        v.guard.id
        for v in guards.plan_violations(
            bad, build_plan(bad, "rooted", "helper-installed")
        )
    ] == ["no-ssl-verify-off"]


# ---- guards: run-time ------------------------------------------------------------------------------


def k1_ep() -> tuple[Endpoint, FakeHttp]:
    http = FakeHttp()
    http.set("GET", 80, "/info", 200, {"model": "K1"})
    ep = context(
        K1,
        target(model="Creality K1 SE", adapter="creality-stock", port=80),
        make_backends(http=http)[0],
    ).ep
    return ep, http


def test_the_stock_web_server_invariant_holds_and_breaks() -> None:
    ep, http = k1_ep()
    step = K1.step("install-fluidd")
    assert step is not None
    be, *_ = make_backends(http=http)
    assert guards.check_invariants(K1, step[1], ep, be) == []
    http.drop(80)  # the web-server was stopped
    http.set("GET", 4408, "/", 200, "")
    broken = guards.check_invariants(K1, step[1], ep, make_backends(http=http)[0])
    assert [g.id for g, _ in broken] == ["keep-creality-web-server-up"]


def test_u1_force_logins_must_stay_off() -> None:
    http = FakeHttp()
    http.set(
        "GET",
        7125,
        "/server/config",
        200,
        {"result": {"config": {"authorization": {"trusted_clients": ["10.0.0.0/8"]}}}},
    )
    tgt = target(model="Snapmaker U1")
    be, *_ = make_backends(http=http)
    ep = context(U1, tgt, be).ep
    step = U1.step("switch-to-backup-firmware")
    assert step is not None
    assert guards.check_invariants(U1, step[1], ep, be) == []
    http.set(
        "GET",
        7125,
        "/server/config",
        200,
        {"result": {"config": {"authorization": {"force_logins": True}}}},
    )
    bad = guards.check_invariants(U1, step[1], ep, be)
    assert [g.id for g, _ in bad] == ["keep-force-logins-off"]
    http.set(
        "GET",
        7125,
        "/server/config",
        200,
        {"result": {"config": {"authorization": {"force_logins": False}}}},
    )
    assert guards.check_invariants(U1, step[1], ep, be) == []


def test_an_invariant_that_cannot_be_read_counts_as_violated() -> None:
    http = FakeHttp()  # moonraker unreachable
    be, *_ = make_backends(http=http)
    ep = context(U1, target(model="Snapmaker U1"), be).ep
    step = U1.step("switch-to-backup-firmware")
    assert step is not None
    assert [g.id for g, _ in guards.check_invariants(U1, step[1], ep, be)] == [
        "keep-force-logins-off"
    ]


def test_require_idle_applies_to_risky_steps_only() -> None:
    risky = K1.step("fetch-helper-script")
    low = K1.step("install-ssh-key")
    assert risky is not None and low is not None
    assert guards.idle_guard(K1, risky[1]) is not None
    assert guards.idle_guard(K1, low[1]) is None
    guard = guards.any_idle_guard(K1)
    assert guard is not None and "Do not start while the K1 prints" in guard.reason


def test_violation_json_quotes_reason_and_sources() -> None:
    v = guards.forbidden_options(
        K1, ["install-guppy-screen"], "helper-installed", "klipper-ready"
    )[0]
    doc = v.to_json()
    assert (
        doc["guard"] == "no-guppy-screen"
        and doc["sources"]
        and "Guppy" in str(doc["reason"])
    )


def test_plan_json_shape_is_stable() -> None:
    from printer_fw.render import plan_json

    doc: dict[str, Any] = plan_json(build_plan(U1, "stock", "extended"))
    assert doc["from"] == "stock" and doc["to"] == "extended"
    assert doc["path"] == ["stock", "extended"]
    step = doc["steps"][0]
    assert step["kind"] == "manual" and step["backups"] and step["rollback"]


def test_plan_dataclass_exposes_step_ids() -> None:
    assert isinstance(build_plan(K1, "stock", "rooted"), Plan)
