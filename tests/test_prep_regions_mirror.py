"""`mirror: x` landmarks: the two halves must stay on their own side of the X = 0 plane."""

from __future__ import annotations

from typing import Any

import pytest

from errors import InvalidArgument
from prep.regions import parse_colour_spec

FILAMENTS: list[dict[str, Any]] = [
    {"slot": 1, "key": "K", "name": "Black", "hex": "#111111"},
    {"slot": 2, "key": "W", "name": "White", "hex": "#f0f0f0"},
]


def spec_with(box: list[float]) -> None:
    parse_colour_spec(
        {
            "filaments": FILAMENTS,
            "default": {"key": "K"},
            "landmarks": {"nose": {"box": box, "mirror": "x"}},
            "regions": [],
        }
    )


@pytest.mark.parametrize(
    "box", [[-5, 5, 0, 1, 0, 1], [-5, 6, 0, 1, 0, 1], [-6, 5, 0, 1, 0, 1]]
)
def test_a_box_crossing_the_symmetry_plane_is_rejected_even_when_off_centre(
    box: list[float],
) -> None:
    with pytest.raises(InvalidArgument) as exc:
        spec_with(box)
    assert "X = 0" in exc.value.accepted[0]


@pytest.mark.parametrize(
    "box", [[0, 5, 0, 1, 0, 1], [-5, 0, 0, 1, 0, 1], [2, 9, 0, 1, 0, 1]]
)
def test_a_box_touching_or_clear_of_the_plane_is_accepted(box: list[float]) -> None:
    spec_with(box)
