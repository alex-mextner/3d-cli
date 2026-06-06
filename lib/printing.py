"""Compatibility wrapper for slicing.printing."""
from __future__ import annotations

from slicing.printing import (
    ACCEPTED_INPUT_EXTENSIONS,
    ACCEPTED_PROFILE_EXTENSIONS,
    COMMAND_NAME,
    DryRunPlan,
    JobFields,
    ProfileFields,
    build_dry_run_plan,
    plan_to_json,
)

__all__ = [
    "ACCEPTED_INPUT_EXTENSIONS",
    "ACCEPTED_PROFILE_EXTENSIONS",
    "COMMAND_NAME",
    "DryRunPlan",
    "JobFields",
    "ProfileFields",
    "build_dry_run_plan",
    "plan_to_json",
]
