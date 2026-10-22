"""Structured errors with stable exit codes (the `3d` CLI contract)."""
from __future__ import annotations

EXIT_OK = 0
EXIT_BAD_INPUT = 2
EXIT_LOW_CONFIDENCE = 3
EXIT_MISSING = 127


class VisionError(Exception):
    """Base error; `exit_code` is what the CLI exits with, `remediation` is printed after the message."""

    exit_code = 1

    def __init__(self, message: str, remediation: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.remediation = remediation

    def render(self) -> str:
        return self.message if not self.remediation else f"{self.message}\n  fix: {self.remediation}"


class BadInput(VisionError):
    exit_code = EXIT_BAD_INPUT


class LowConfidence(VisionError):
    """A stage finished but its confidence is below the caller's threshold (not a crash)."""

    exit_code = EXIT_LOW_CONFIDENCE


class MissingDependency(VisionError):
    """A python package, GPU runtime or model file is absent. Models are never downloaded implicitly."""

    exit_code = EXIT_MISSING
