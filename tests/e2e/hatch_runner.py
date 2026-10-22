"""hatch_runner.py — `bin/3d` with the owner's Telegram hatch answered by a test double.

An e2e test cannot tap Approve on a real phone, and the shared hatch helper refuses every tg-ctl
outside the test sandbox. This runner is `python hatch_runner.py <3d args>`: it swaps
`printer_ops.force_hatch.load_hatch` for a fake whose verdict comes from THREED_TEST_HATCH_VERDICT
("approve" | "deny") and appends each request (hook id, context, command) as one JSON line to the
file in THREED_TEST_HATCH_LOG, then runs the real dispatcher. Everything else is the real CLI.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

_LIB = Path(__file__).resolve().parents[2] / "lib"
sys.path.insert(0, str(_LIB))

from cli.dispatch import main  # noqa: E402  (after sys.path setup)
from printer_ops import force_hatch  # noqa: E402


@dataclass(frozen=True)
class _Verdict:
    approved: bool
    should_stop: bool
    reason: str


class _FakeHatch:
    def request_hatch_approval(
        self,
        hook_id: str,
        context: dict[str, object],
        *,
        cwd: str,
        command: str,
        critical: bool,
    ) -> _Verdict:
        with open(os.environ["THREED_TEST_HATCH_LOG"], "a", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {
                        "hook": hook_id,
                        "context": context,
                        "command": command,
                        "critical": critical,
                    }
                )
                + "\n"
            )
        if os.environ["THREED_TEST_HATCH_VERDICT"] == "approve":
            return _Verdict(True, True, "test double approved")
        return _Verdict(False, True, "test double denied")


def _fake_hatch() -> _FakeHatch:
    return _FakeHatch()


if __name__ == "__main__":
    with patch.object(force_hatch, "load_hatch", _fake_hatch):
        sys.exit(main(sys.argv[1:]))
