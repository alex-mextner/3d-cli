"""Firmware profile schema: shipped profiles validate; bad fixtures are rejected with precise messages."""

from __future__ import annotations

import copy
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from printer_fw.profiles import (
    DATA_DIR,
    Matcher,
    Profile,
    ProfileError,
    is_url,
    load_profile_file,
    load_profiles,
    match_profile,
    parse_profile,
    read_only_command,
)
from printer_fw_fakes import minimal_profile, minimal_profile_data

Mutator = Callable[[dict[str, Any]], None]


def _step(data: dict[str, Any]) -> dict[str, Any]:
    step: dict[str, Any] = data["transitions"][0]["steps"][0]
    return step


def _ssh(data: dict[str, Any]) -> None:
    data["ssh"] = {"user": "root", "credentials": "documented default, key only"}


def _unknown_top(d: dict[str, Any]) -> None:
    d["colour"] = "red"


def _no_models(d: dict[str, Any]) -> None:
    del d["models"]


def _both_matchers(d: dict[str, Any]) -> None:
    d["models"] = [{"contains": "a", "regex": "b"}]


def _bad_regex(d: dict[str, Any]) -> None:
    d["models"] = [{"regex": "(unclosed"}]


def _unknown_probe_kind(d: dict[str, Any]) -> None:
    d["states"][0]["detection"]["all"][0]["kind"] = "telepathy"


def _unknown_service(d: dict[str, Any]) -> None:
    d["states"][0]["detection"]["all"][0]["service"] = "nope"


def _write_probe(d: dict[str, Any]) -> None:
    _ssh(d)
    d["states"][0]["detection"]["all"] = [
        {"kind": "ssh_command", "command": "rm -rf /usr/data"}
    ]


def _redirect_probe(d: dict[str, Any]) -> None:
    _ssh(d)
    d["states"][0]["detection"]["all"] = [
        {"kind": "ssh_command", "command": "cat /etc/passwd > /tmp/x"}
    ]


def _unknown_step_kind(d: dict[str, Any]) -> None:
    _step(d)["kind"] = "telepathy"


def _manual_automatable(d: dict[str, Any]) -> None:
    _step(d)["automatable"] = True


def _no_verify(d: dict[str, Any]) -> None:
    del _step(d)["verify"]


def _no_rollback(d: dict[str, Any]) -> None:
    _step(d)["risk"] = "high"


def _bad_transition_state(d: dict[str, Any]) -> None:
    d["transitions"][0]["to"] = "ghost"


def _duplicate_step(d: dict[str, Any]) -> None:
    d["transitions"].append(
        {
            **copy.deepcopy(d["transitions"][0]),
            "id": "again",
            "from": "custom",
            "to": "stock",
        }
    )


def _bad_guard_kind(d: dict[str, Any]) -> None:
    d["guards"] = [
        {
            "id": "g",
            "kind": "magic",
            "reason": "r",
            "sources": ["https://example.com/x"],
        }
    ]


def _guard_unknown_step(d: dict[str, Any]) -> None:
    d["guards"] = [
        {
            "id": "g",
            "kind": "step_requires_command",
            "step": "ghost",
            "pattern": "x",
            "reason": "r",
            "sources": ["https://example.com/x"],
        }
    ]


def _guard_pattern_missing(d: dict[str, Any]) -> None:
    d["guards"] = [
        {
            "id": "g",
            "kind": "step_requires_command",
            "step": "flip-it",
            "pattern": "chmod",
            "reason": "needs chmod",
            "sources": ["https://example.com/x"],
        }
    ]


def _bad_source(d: dict[str, Any]) -> None:
    d["sources"] = ["see the runbook"]


def _unknown_backup(d: dict[str, Any]) -> None:
    _step(d)["backup"] = ["ghost"]


def _manual_backup_required(d: dict[str, Any]) -> None:
    d["backups"] = [
        {
            "id": "note",
            "description": "d",
            "via": "manual",
            "instruction": "write it down",
        }
    ]
    _step(d)["backup"] = ["note"]


def _confirm_in_detection(d: dict[str, Any]) -> None:
    d["states"][0]["detection"]["all"] = [{"kind": "confirm", "prompt": "sure?"}]


def _ssh_step_without_ssh(d: dict[str, Any]) -> None:
    s = _step(d)
    s.update(kind="ssh_command", command="true", instruction="")


def _upload_field(d: dict[str, Any]) -> None:
    s = _step(d)
    s.update(
        kind="http_upload",
        automatable=True,
        upload={"service": "web", "path": "/up", "field": "firmware"},
    )


def _forbidden_without_options(d: dict[str, Any]) -> None:
    d["guards"] = [
        {
            "id": "g",
            "kind": "forbidden_option",
            "reason": "r",
            "sources": ["https://example.com/x"],
        }
    ]


def _maintenance_with_steps(d: dict[str, Any]) -> None:
    d["transitions"][0]["to"] = "stock"


def _manual_without_instruction(d: dict[str, Any]) -> None:
    del _step(d)["instruction"]


def _bad_port(d: dict[str, Any]) -> None:
    d["services"] = {"web": 70000}


def _guard_without_sources(d: dict[str, Any]) -> None:
    d["guards"] = [
        {"id": "g", "kind": "forbidden_option", "options": ["x"], "reason": "r"}
    ]


BAD: list[tuple[str, Mutator, str]] = [
    ("unknown top-level key", _unknown_top, "demo.yaml.colour: unknown key"),
    ("missing models", _no_models, "demo.yaml.models: is required"),
    ("both matchers", _both_matchers, "needs exactly one of `contains` or `regex`"),
    ("bad regex", _bad_regex, "is not a valid regex"),
    (
        "unknown probe kind",
        _unknown_probe_kind,
        "states[0].detection.all[0].kind: unknown probe kind 'telepathy'",
    ),
    ("unknown service", _unknown_service, "must name a profile service"),
    ("probe that deletes", _write_probe, "starts with 'rm'"),
    ("probe that redirects", _redirect_probe, "contains '>'"),
    ("unknown step kind", _unknown_step_kind, "unknown step kind 'telepathy'"),
    ("manual automatable", _manual_automatable, "a manual step cannot be automatable"),
    (
        "step without verify",
        _no_verify,
        "every step needs at least one verification probe",
    ),
    ("high risk without rollback", _no_rollback, "is required for a high-risk step"),
    ("transition to a ghost state", _bad_transition_state, "unknown state 'ghost'"),
    ("duplicate step id", _duplicate_step, "duplicate step id 'flip-it'"),
    ("unknown guard kind", _bad_guard_kind, "unknown guard kind 'magic'"),
    ("guard on a missing step", _guard_unknown_step, "unknown step 'ghost'"),
    (
        "guard pattern the step lacks",
        _guard_pattern_missing,
        "does not match /chmod/: needs chmod",
    ),
    ("source that is no URL", _bad_source, "must be an http(s) URL"),
    ("step needs unknown backup", _unknown_backup, "unknown backup id 'ghost'"),
    ("step requires a manual backup", _manual_backup_required, "is a manual backup"),
    (
        "confirm used for detection",
        _confirm_in_detection,
        "a state cannot be detected with it",
    ),
    ("ssh step without ssh block", _ssh_step_without_ssh, "needs an `ssh:` block"),
    ("upload part other than file", _upload_field, "must be `file`"),
    (
        "forbidden_option without options",
        _forbidden_without_options,
        "options: is required for forbidden_option",
    ),
    (
        "maintenance transition with steps",
        _maintenance_with_steps,
        "a maintenance transition",
    ),
    (
        "manual step without instruction",
        _manual_without_instruction,
        "instruction: is required for a manual step",
    ),
    ("service port out of range", _bad_port, "must be a port 1-65535"),
    ("guard without sources", _guard_without_sources, "sources: is required"),
]


def test_the_minimal_fixture_is_valid() -> None:
    assert minimal_profile().id == "demo-profile"


@pytest.mark.parametrize(
    ("mutate", "message"), [pytest.param(m, msg, id=name) for name, m, msg in BAD]
)
def test_a_bad_profile_is_rejected_with_a_precise_message(
    mutate: Mutator, message: str
) -> None:
    data = minimal_profile_data()
    mutate(data)
    with pytest.raises(ProfileError) as err:
        parse_profile(data, "demo.yaml")
    assert message in err.value.message
    assert err.value.exit_code == 2


def test_every_problem_is_reported_at_once() -> None:
    data = minimal_profile_data()
    _unknown_top(data)
    _bad_source(data)
    _no_verify(data)
    with pytest.raises(ProfileError) as err:
        parse_profile(data, "demo.yaml")
    for fragment in ("colour", "must be an http(s) URL", "every step needs"):
        assert fragment in err.value.message


def test_a_non_mapping_profile_is_rejected() -> None:
    with pytest.raises(ProfileError, match="must be a mapping"):
        parse_profile(["a list"], "demo.yaml")


def test_invalid_yaml_is_a_profile_error(tmp_path: Path) -> None:
    bad = tmp_path / "broken.yaml"
    bad.write_text("id: [unclosed\n", encoding="utf-8")
    with pytest.raises(ProfileError, match="invalid YAML"):
        load_profile_file(bad)


def test_duplicate_profile_ids_across_files_are_refused(tmp_path: Path) -> None:
    import yaml

    for name in ("one.yaml", "two.yaml"):
        (tmp_path / name).write_text(
            yaml.safe_dump(minimal_profile_data()), encoding="utf-8"
        )
    with pytest.raises(ProfileError, match="duplicate profile id"):
        load_profiles(tmp_path)


# ---- shipped profiles ------------------------------------------------------------------------


def test_every_shipped_profile_validates_and_ids_match_the_file_names_set() -> None:
    profiles = load_profiles()
    assert len(profiles) == len(list(DATA_DIR.glob("*.yaml"))) >= 4
    assert all(
        p.path is not None and p.path.parent == DATA_DIR for p in profiles.values()
    )


def _all_sources(p: Profile) -> list[str]:
    urls = list(p.sources)
    for s in p.states:
        urls += s.sources
    for t in p.transitions:
        urls += t.sources
    for _t, step, _opt in p.all_steps():
        urls += step.sources
    for g in p.guards:
        urls += g.sources
    return urls


@pytest.mark.parametrize("profile", list(load_profiles().values()), ids=lambda p: p.id)
def test_every_source_of_a_shipped_profile_is_a_wellformed_url(
    profile: Profile,
) -> None:
    urls = _all_sources(profile)
    assert urls
    assert all(is_url(u) for u in urls), [u for u in urls if not is_url(u)]


@pytest.mark.parametrize("profile", list(load_profiles().values()), ids=lambda p: p.id)
def test_shipped_profiles_obey_the_step_rules(profile: Profile) -> None:
    for _t, step, _opt in profile.all_steps():
        assert step.verify.probes, f"{step.id}: no verification probe"
        if step.automatable:
            assert step.kind != "manual" and (step.command or step.upload or step.post)
        if step.risk in ("medium", "high"):
            assert step.rollback is not None, f"{step.id}: no rollback"
        for b in step.backup:
            item = profile.backup_item(b)
            assert item is not None and item.via != "manual"


@pytest.mark.parametrize("profile", list(load_profiles().values()), ids=lambda p: p.id)
def test_every_shipped_probe_command_is_read_only(profile: Profile) -> None:
    probes = [p for s in profile.states if s.detection for p in s.detection.probes]
    for _t, step, _opt in profile.all_steps():
        probes += [*step.preconditions, *step.skip_if, *step.verify.probes]
    probes += [p for g in profile.guards for p in g.probes]
    probes += [profile.idle_probe] if profile.idle_probe else []
    assert all(
        read_only_command(p.command) is None for p in probes if p.kind == "ssh_command"
    )


@pytest.mark.parametrize("profile", list(load_profiles().values()), ids=lambda p: p.id)
def test_every_shipped_profile_says_whether_klipper_is_possible(
    profile: Profile,
) -> None:
    assert (
        isinstance(profile.capabilities.klipper_possible, bool)
        and profile.capabilities.notes.strip()
    )


def test_bambu_profiles_say_klipper_is_not_possible_and_offer_only_manual_steps() -> (
    None
):
    for pid in ("bambu-lab-p1-a1", "bambu-lab-x1"):
        p = load_profiles()[pid]
        assert p.capabilities.klipper_possible is False
        assert all(
            not s.automatable and s.kind == "manual" for _t, s, _o in p.all_steps()
        )


# ---- matchers --------------------------------------------------------------------------------


def test_matchers_apply_to_the_model_case_insensitively() -> None:
    assert Matcher(contains="test model").matches("The TEST Model 2")
    assert not Matcher(contains="test model").matches("other")
    assert Matcher(regex=r"^foo\s+bar$").matches("FOO   Bar")
    assert not Matcher(regex=r"^foo$").matches("foo bar")


def test_match_profile_uses_the_model_only_and_none_for_unknown_or_missing_models() -> (
    None
):
    profiles = {"demo-profile": minimal_profile()}
    assert match_profile(profiles, "Some Test Model") is profiles["demo-profile"]
    assert match_profile(profiles, "Prusa MK4") is None
    assert match_profile(profiles, None) is None
    assert match_profile(profiles, "") is None


def test_a_model_matching_two_profiles_is_an_error() -> None:
    a = minimal_profile()
    data = minimal_profile_data()
    data["id"] = "other"
    profiles = {"demo-profile": a, "other": parse_profile(data, "o.yaml")}
    with pytest.raises(ProfileError, match="matches several firmware profiles"):
        match_profile(profiles, "test model")


@pytest.mark.parametrize(
    ("command", "problem"),
    [
        ("cat /etc/os-release", None),
        ("test -d /usr/data", None),
        ("grep -q x /etc/passwd", None),
        ("rm -rf /", "starts with 'rm'"),
        ("cat /a > /b", "contains '>'"),
        ("cat /a; reboot", "contains ';'"),
        ("cat /a | sh", "contains '|'"),
        ("cat $(echo /a)", "contains '$('"),
        ("", "is empty"),
    ],
)
def test_read_only_command(command: str, problem: str | None) -> None:
    why = read_only_command(command)
    assert (why is None) if problem is None else (why is not None and problem in why)
