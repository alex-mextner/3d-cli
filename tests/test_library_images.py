"""Tests for lib/library/images.py: result thumbnails fetched from the loopback server, encoded per protocol,
cached as finished escape sequences, with every failure degrading to "no picture"."""

from __future__ import annotations

import base64
import io
import re
from pathlib import Path

import pytest
from PIL import Image
from search_fixtures import BulkServer, tiny_png

from library import images


@pytest.fixture(autouse=True)
def _cache(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))


def _png_requests(server: BulkServer) -> int:
    return sum(1 for _, path, _ in server.requests if path == "/files/pic.png")


def test_images_are_fetched_encoded_and_cached_as_finished_sequences() -> None:
    with BulkServer(total=1) as server:
        url = server.file_url("pic.png")
        first = images.render_images(
            [("thingiverse", url), ("thingiverse", url), ("cults", "")], "kitty"
        )
        assert list(first) == [url] and first[url].startswith("\x1b_Ga=T,f=100,c=20")
        assert _png_requests(server) == 1  # the duplicate URL is fetched once
        again = images.render_images([("thingiverse", url)], "kitty")
        assert (
            again == first and _png_requests(server) == 1
        )  # a cache hit makes no request
        other = images.render_images([("thingiverse", url)], "iterm2", cols=12)
        assert (
            other[url].startswith("\x1b]1337;File=inline=1;")
            and "width=12" in other[url]
        )
        assert _png_requests(server) == 2  # another protocol is another cache entry


def test_the_kitty_picture_is_the_fetched_png_shrunk_to_the_thumbnail_width() -> None:
    with BulkServer(total=1) as server:
        url = server.file_url("pic.png")
        sequence = images.render_images([("thingiverse", url)], "kitty")[url]
    payload = "".join(p for _, p in re.findall(r"\x1b_G([^;]*);([^\x1b]*)\x1b\\", sequence))
    decoded = Image.open(io.BytesIO(base64.b64decode(payload)))
    assert decoded.size == (8, 6) and decoded.getpixel((0, 0)) == (220, 40, 40)


def test_the_thumbnail_tool_gives_the_same_answer_when_the_interpreter_has_no_pillow(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    with BulkServer(total=1) as server:
        url = server.file_url("pic.png")
        direct = images.render_images([("thingiverse", url)], "sixel")
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "second-cache"))  # an empty cache: encode again
        monkeypatch.setattr(images, "_pillow_here", lambda: False)
        through_tool = images.render_images([("thingiverse", url)], "sixel")
    assert direct[url].startswith('\x1bPq"1;1;8;6') and through_tool == direct


def test_unreachable_oversized_and_undecodable_images_are_simply_absent(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    with BulkServer(total=1) as server:
        missing = server.file_url("does-not-exist.png")
        broken = server.file_url("cube.stl")  # fetched fine, but not an image
        good = server.file_url("pic.png")
        result = images.render_images([("t", missing), ("t", broken), ("t", good), ("t", "ftp://example.com/x.png")],
                                      "kitty")
        monkeypatch.setattr(images, "MAX_IMAGE_BYTES", 10)
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "small-cache"))
        capped = images.render_images([("t", good)], "kitty")
    assert list(result) == [good]
    assert capped == {}


def test_a_spent_time_budget_fetches_nothing() -> None:
    with BulkServer(total=1) as server:
        assert (
            images.render_images(
                [("t", server.file_url("pic.png"))], "kitty", budget_s=0.0
            )
            == {}
        )
        assert _png_requests(server) == 0


def test_the_png_fixture_is_a_valid_png() -> None:
    assert Image.open(io.BytesIO(tiny_png(5, 3))).size == (5, 3)
