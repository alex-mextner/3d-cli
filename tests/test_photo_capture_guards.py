"""Guards of the library capture of the table photo: a bad `--part-height` is refused at the shared boundary
(CLI and `photo(capture=True)` alike) and default file names never collide."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from types import SimpleNamespace

import pytest

from commands import photo
from errors import InvalidArgument
from library import actions
from library import capture as capturing


@pytest.fixture(autouse=True)
def hermetic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for var, sub in (
        ("XDG_CONFIG_HOME", "config"),
        ("XDG_STATE_HOME", "state"),
        ("XDG_DATA_HOME", "data"),
        ("XDG_CACHE_HOME", "cache"),
    ):
        monkeypatch.setenv(var, str(tmp_path / sub))
    monkeypatch.setenv("THREED_LIBRARY_DB", str(tmp_path / "library.db"))


def test_the_action_refuses_a_negative_part_height_with_exit_2() -> None:
    with pytest.raises(InvalidArgument) as exc:
        actions.photo(
            capture=True, make="m1", part_height_mm=-100.0, lift=70.0, dry_run=True
        )
    assert exc.value.exit_code == 2 and "--part-height" in exc.value.message


def test_the_cli_refuses_a_negative_part_height_with_exit_2() -> None:
    with pytest.raises(InvalidArgument) as exc:
        photo.run(
            [
                "capture",
                "--make",
                "m1",
                "--part-height",
                "-100",
                "--lift",
                "70",
                "--yes",
            ]
        )
    assert exc.value.exit_code == 2


def test_two_default_names_of_one_make_and_kind_within_a_second_differ(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Frozen(dt.datetime):
        @classmethod
        def now(cls, tz: dt.tzinfo | None = None) -> Frozen:  # type: ignore[override]
            return cls(2026, 1, 1, 12, 0, 0, tzinfo=tz)

    monkeypatch.setattr(
        capturing, "dt", SimpleNamespace(datetime=Frozen, timezone=dt.timezone)
    )
    first, second = (
        capturing.default_out("mk", "table"),
        capturing.default_out("mk", "table"),
    )
    assert first != second
    assert first.parent == second.parent and first.suffix == ".jpg"


def test_capture_refuses_an_out_name_that_is_not_a_picture_file_name() -> None:
    with pytest.raises(InvalidArgument) as exc:
        actions.photo(
            capture=True, make="m1", out="/tmp/table", lift=70.0, dry_run=True
        )
    assert exc.value.exit_code == 2 and "--out" in exc.value.message
