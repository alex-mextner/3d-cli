"""The firmware docs cannot drift from the shipped profiles and the schema constants."""

from __future__ import annotations

from pathlib import Path

import pytest
from printer_fw.profiles import (
    BACKUP_VIA,
    GUARD_KINDS,
    PROBE_KINDS,
    READ_ONLY_COMMANDS,
    STEP_KINDS,
    Profile,
    load_profiles,
)

REPO = Path(__file__).resolve().parents[1]
SCHEMA_DOC = (REPO / "docs" / "printers" / "firmware-profiles.md").read_text(
    encoding="utf-8"
)
COMMAND_DOC = (REPO / "docs" / "commands" / "printers-fw.md").read_text(
    encoding="utf-8"
)
PROFILES = load_profiles()


def table_of(profile: Profile) -> str:
    """The profile's section of the shipped-profiles table (up to the next profile heading)."""
    head = f"### `{profile.id}`"
    start = SCHEMA_DOC.index(head)
    nxt = SCHEMA_DOC.find("\n### `", start + 1)
    end = nxt if nxt != -1 else SCHEMA_DOC.index("\n## What is not verified")
    return SCHEMA_DOC[start:end]


@pytest.mark.parametrize("profile", list(PROFILES.values()), ids=lambda p: p.id)
def test_every_profile_has_a_section_listing_its_states_transitions_steps_guards_and_sources(
    profile: Profile,
) -> None:
    section = table_of(profile)
    for state in profile.states:
        assert f"| `{state.id}` |" in section, f"state {state.id}"
    for transition in profile.transitions:
        assert (
            f"`{transition.from_state}` → `{transition.to_state}` ({transition.id})"
            in section
        )
        for step in (*transition.steps, *transition.optional_steps):
            assert f"`{step.id}` ({step.kind}" in section, f"step {step.id}"
    for guard in profile.guards:
        assert f"| `{guard.id}` |" in section, f"guard {guard.id}"
    for url in profile.sources:
        assert f"<{url}>" in section, f"source {url}"
    assert profile.path is not None
    assert f"lib/data/firmware/{profile.path.name}" in section
    assert ("**no**" in section) == (not profile.capabilities.klipper_possible)


def test_every_schema_constant_is_documented() -> None:
    for word in (*PROBE_KINDS, *STEP_KINDS, *GUARD_KINDS, *BACKUP_VIA):
        assert f"`{word}`" in SCHEMA_DOC or word in SCHEMA_DOC, word
    for command in READ_ONLY_COMMANDS:
        assert command in SCHEMA_DOC, f"probe command word {command}"
    for key in (
        "absent_ok",
        "down_probes",
        "down_wait_s",
        "adapter_services",
        "idle_probe",
        "snippets",
        "{ref: ",
    ):
        assert key in SCHEMA_DOC, key


def test_the_command_doc_lists_every_subcommand_and_exit_code() -> None:
    for sub in ("status", "plan", "backup", "apply", "profiles"):
        assert f"## `{sub}`" in COMMAND_DOC
    for fragment in (
        "`127`",
        "THREED_FW_SERVICE_PORTS",
        "--no-ssh",
        "NEXT: do the step, then re-run the same command",
    ):
        assert fragment in COMMAND_DOC
