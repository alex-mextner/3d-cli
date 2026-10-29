"""Browser guards of `publishing.execute`: where a make click lands before photos go up, and what is a login."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from publishing import execute
from publishing.plan import PlatformPlan

MODEL = "https://www.printables.com/model/311253-spoon-drawer-pull"


def make_plan(tmp_path: Path) -> PlatformPlan:
    photo = tmp_path / "table.jpg"
    photo.write_bytes(b"jpg")
    steps = (
        ("open", MODEL),
        ("find", "text", "Add make", "click"),
        ("upload", "input[type=file]", str(photo)),
        ("find", "label", "Description", "fill", "note"),
        ("screenshot",),
    )
    return PlatformPlan(
        "printables", "agent-browser", {"model_url": MODEL}, (), (photo,), (), steps, ()
    )


def drive(
    plan: PlatformPlan, urls: list[str]
) -> tuple[execute.BrowserOutcome, list[list[str]]]:
    """Run the plan; the n-th `get url` answers urls[n] (the last one repeats)."""
    calls: list[list[str]] = []
    asked = 0

    def runner(argv: Any) -> tuple[int, str]:
        nonlocal asked
        calls.append(list(argv))
        if argv[3:] == ["get", "url"]:
            asked += 1
            return 0, urls[min(asked, len(urls)) - 1]
        return 0, "ws://x" if "cdp-url" in argv else "ok"

    return execute.publish_browser(plan, runner), calls


@pytest.mark.parametrize(
    "after_click",
    [
        "https://attacker.example/upload",
        "https://www.printables.com.attacker.example/model/311253-spoon-drawer-pull/make",
        "https://www.printables.com/model/999-other/make",
        "http://www.printables.com/model/311253-spoon-drawer-pull/make",
        "https://www.printables.com/model/311253/../../model/999",
    ],
)
def test_a_click_that_navigates_away_is_refused_before_any_photo_goes_up(
    tmp_path: Path, after_click: str
) -> None:
    out, calls = drive(make_plan(tmp_path), [MODEL, after_click])
    assert not out.ok and "after pressing the make button" in out.message
    assert "stays open" in out.message
    assert not any(c[3] in ("upload", "keychain") or "fill" in c for c in calls)


@pytest.mark.parametrize(
    "after_click",
    [
        MODEL,
        "https://www.printables.com/model/311253-spoon-drawer-pull/make?x=1",
        "https://www.printables.com/model/311253-renamed/make/new",
    ],
)
def test_a_click_that_stays_on_the_model_or_its_make_route_uploads(
    tmp_path: Path, after_click: str
) -> None:
    out, calls = drive(make_plan(tmp_path), [MODEL, after_click])
    assert out.ok and any(c[3] == "upload" for c in calls)


@pytest.mark.parametrize(
    ("url", "is_login"),
    [
        ("https://www.printables.com/model/123-login-card-holder", False),
        ("https://www.printables.com/model/123-signin-sign-in-auth", False),
        ("https://www.printables.com/login", True),
        ("https://www.printables.com/en/login?next=/x", True),
        ("https://www.thingiverse.com/auth/sign-in", True),
        ("https://login.example.com/", True),
    ],
)
def test_only_real_login_routes_are_login_walls(url: str, is_login: bool) -> None:
    assert execute._is_login_url(url) is is_login


def test_a_model_slug_with_login_in_it_does_not_trigger_credential_filling(
    tmp_path: Path,
) -> None:
    model = "https://www.printables.com/model/123-login-card-holder"
    plan = make_plan(tmp_path)
    plan = replace(
        plan,
        fields={"model_url": model},
        browser_steps=(("open", model), *plan.browser_steps[1:]),
    )
    out, calls = drive(plan, [model])
    assert out.ok and not any(c[0] == "keychain" for c in calls)
