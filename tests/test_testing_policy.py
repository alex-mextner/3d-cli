from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _normalized_policy_text(path: str) -> str:
    text = (ROOT / path).read_text(encoding="utf-8").lower().replace("`", "")
    return " ".join(text.split())


def test_testing_rules_require_e2e_bin3d_coverage_for_user_visible_behavior() -> None:
    for path in ("AGENTS.md", "docs/rules/testing.md"):
        text = _normalized_policy_text(path)
        assert "e2e" in text
        assert "bin/3d" in text
        assert "new command" in text
        assert "flag" in text
        assert "alias" in text
        assert "shell-facing workflow" in text
        assert "docs/help" in text
        assert "unit tests are still required" in text
        assert "pure logic" in text
