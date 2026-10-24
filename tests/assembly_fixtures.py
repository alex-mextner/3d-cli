"""Synthetic assemblies for the `3d assembly` tests: a peg in a pocket, a snap tab under a cap, an interlocked stack.

Every solid is built with manifold3d boxes and written as a binary STL; every spec is a plain dict
(`assembly.spec.parse_spec`) or a YAML file so the same fixture serves the unit tests and the `bin/3d` e2e stories.
Coordinates are mm in one assembly frame.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import trimesh
from manifold3d import Manifold


def box(lo: tuple[float, float, float], hi: tuple[float, float, float]) -> Manifold:
    size = (hi[0] - lo[0], hi[1] - lo[1], hi[2] - lo[2])
    return Manifold.cube(size, False).translate(lo)


def write_stl(path: Path, solid: Manifold) -> Path:
    mesh = solid.to_mesh()
    trimesh.Trimesh(mesh.vert_properties[:, :3], mesh.tri_verts, process=False).export(
        path
    )
    return path


def peg_pocket(
    tmp: Path,
    *,
    pocket: float = 10.6,
    peg: float = 10.0,
    peg_z0: float = 5.0,
    boss: bool = False,
) -> dict[str, Any]:
    """A 30x30x20 block with a square pocket open to +Z (floor at z = 5) and a 10 x 10 x 12 peg.

    `peg_z0` is the peg's bottom: 5.0 seats it on the floor (clean), 4.0 pushes it 1 mm into the floor (blocking).
    `boss=True` adds a 2 mm block intruding into the pocket from one wall (the deliberately blocking boss).
    Returns the spec document (paths relative to `tmp`).
    """
    half = pocket / 2
    socket = box((-15, -15, 0), (15, 15, 20)) - box((-half, -half, 5), (half, half, 21))
    if boss:
        socket = socket + box((half - 2.0, -2, 8), (half, 2, 12))
    write_stl(tmp / "socket.stl", socket)
    write_stl(
        tmp / "peg.stl",
        box((-peg / 2, -peg / 2, peg_z0), (peg / 2, peg / 2, peg_z0 + 12)),
    )
    return {
        "name": "peg-pocket",
        "parts": {"socket": {"mesh": "socket.stl"}, "peg": {"mesh": "peg.stl"}},
        "mates": [
            {
                "name": "peg_in_pocket",
                "a": "socket",
                "b": "peg",
                "kind": "sliding",
                "fit": "sliding",
                "insertion": {"part": "peg", "axis": "-Z", "distance_mm": 10},
            }
        ],
    }


def drawer(tmp: Path, *, lip: bool = False, axis: str = "+Y") -> dict[str, Any]:
    """A drawer (20 x 20 x 10) in a body open on one side, moving along +Y / -X / an oblique axis.

    Built along +Y first, then turned onto `axis` (`+Y`, `-X` or `oblique` = (1, 1, 0) / sqrt 2): the same
    mechanism in different directions. `lip=True` puts a 1.2 mm lip across the opening that the drawer cannot pass:
    the first blocking position is then where the drawer's front meets the lip.
    """
    body = box((-15, -15, -15), (15, 15, 15)) - box(
        (-10.4, -10.4, -10.4), (10.4, 16, 10.4)
    )
    if lip:
        body = (
            body
            + box((-10.4, 12.0, -10.4), (10.4, 13.2, 10.4))
            - box((-6, 11, -6), (6, 14, 6))
        )
    drawer_solid = box((-10, -10, -10), (10, 10, 10))
    out: dict[str, Any] = {"parts": {}, "mates": []}
    pose: dict[str, Any] = {}
    if axis == "-X":
        pose = {"rotate": [0, 0, 90]}  # +Y -> -X
        direction: Any = "-X"
    elif axis == "oblique":
        pose = {"rotate": [0, 0, -45]}  # +Y -> (sin45, cos45, 0) = (+X, +Y)/sqrt2
        direction = [0.7071067811865476, 0.7071067811865476, 0.0]
    else:
        direction = "+Y"
    write_stl(tmp / "body.stl", body)
    write_stl(tmp / "drawer.stl", drawer_solid)
    out["name"] = f"drawer{axis}"
    out["parts"] = {
        "body": {"mesh": "body.stl", "pose": pose},
        "drawer": {"mesh": "drawer.stl", "pose": pose},
    }
    # the drawer leaves toward `direction`; it is ASSEMBLED by moving opposite to it
    inward = (
        [-c for c in direction]
        if isinstance(direction, list)
        else {"+Y": "-Y", "-X": "+X"}[direction]
    )
    out["mates"] = [
        {
            "name": "slide",
            "a": "body",
            "b": "drawer",
            "kind": "sliding",
            "fit": "sliding",
            "insertion": {"part": "drawer", "axis": inward, "distance_mm": 12},
        }
    ]
    return out


SNAP_FLEX_LIMIT = 0.3


def snap_tab(
    tmp: Path,
    *,
    flex_limit: float = SNAP_FLEX_LIMIT,
    wall_behind_arm: float | None = None,
    covered: bool = True,
    release: str = "push",
    access: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """A base with one cantilever snap arm (1.0 thick, 6 wide, barb 0.5 high on +X) and a cap that snaps over it.

    The cap's cavity wall is 0.3 from the arm, so the barb (0.5) makes the arm flex 0.2 mm on the way in and out.
    `wall_behind_arm` puts a wall that many mm behind the arm's inner face (the arm cannot deflect into it).
    `covered=False` leaves the cap off the hook's side so a finger can reach it. Returns the spec document.
    """
    base = (
        box((-15, -15, 0), (15, 15, 10))
        + box((5.0, -3, 10), (6.0, 3, 19))
        + box((6.0, -3, 15.4), (6.5, 3, 16.0))
    )
    if wall_behind_arm is not None:
        base = base + box(
            (5.0 - wall_behind_arm - 1.0, -6, 10), (5.0 - wall_behind_arm, 6, 19)
        )
    write_stl(tmp / "base.stl", base)
    cap = (
        box((-15, -15, 10), (15, 15, 22))
        - box((-6.3, -3.5, 9), (6.3, 3.5, 21))
        - box((6.3, -3.5, 15.2), (6.8, 3.5, 16.2))
    )
    if not covered:
        cap = cap - box(
            (6.0, -15.5, 9), (15.5, 15.5, 23)
        )  # the cap is open on the hook's side
    write_stl(tmp / "cap.stl", cap)
    hook: dict[str, Any] = {
        "name": "h1",
        "point": [6.2, 0, 15.7],
        "deflect": "-X",
        "barb_mm": 0.5,
        "release": release,
        "region": {"min": [4.95, -3.2, 10], "max": [6.7, 3.2, 19.5]},
        "arm": {"thickness_mm": 1.0, "length_mm": 9.0, "width_mm": 6.0},
    }
    mate: dict[str, Any] = {
        "name": "cap_snap",
        "a": "base",
        "b": "cap",
        "kind": "snap",
        "fit": "snap",
        "seat": True,
        "insertion": {"part": "cap", "axis": "-Z", "distance_mm": 12},
        "snap": {"part": "base", "flex_limit_mm": flex_limit, "hooks": [hook]},
    }
    if access:
        mate["access"] = access
    return {
        "name": "snap-tab",
        "parts": {"base": {"mesh": "base.stl"}, "cap": {"mesh": "cap.stl"}},
        "mates": [mate],
    }


def board_in_case(
    tmp: Path, *, board_dx: float = 0.0, ribs: bool = True
) -> dict[str, Any]:
    """A case with a 20.2 mm pocket for a 19.5 mm board (stand-in), with crush ribs: designed interference.

    `board_dx` shifts the board sideways (the deliberately broken variant: +1 mm puts it into the wall).
    """
    case = box((-15, -15, 0), (15, 15, 12)) - box((-10.1, -12, 3), (10.1, 12, 13))
    rib = box((9.6, -1, 3), (10.1, 1, 12)) + box((-10.1, -1, 3), (-9.6, 1, 12))
    if ribs:
        case = case + rib
    write_stl(tmp / "case.stl", case)
    write_stl(tmp / "board.stl", box((-9.75, -10, 4), (9.75, 10, 5.6)))
    return {
        "name": "board-case",
        "parts": {
            "case": {"mesh": "case.stl", "role": "printed"},
            "board": {
                "mesh": "board.stl",
                "role": "stand_in",
                "pose": {"translate": [board_dx, 0, 0]},
            },
        },
        "mates": [
            {
                "name": "board_ribs",
                "a": "case",
                "b": "board",
                "kind": "press",
                "fit": "press",
                "zone": {"min": [-10.2, -1.5, 3], "max": [10.2, 1.5, 12]},
            }
        ],
    }


def write_spec(tmp: Path, doc: dict[str, Any], name: str = "assembly.yaml") -> Path:
    import yaml

    path = tmp / name
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    return path
