"""3d om — query .scad object-model annotations and print matching nodes as JSON.

WHAT: parses // @id, // @class, // @anchor, // @color comments from an OpenSCAD file
  and runs a CSS-like selector (#id, .class, .class.other) to return the matching nodes.

WHY: the object model (§5) adds an HTML/CSS-like addressing layer over geometry — id,
  class, anchors, selectors — so you can name features in the .scad and then query them
  from the CLI, web, or AI tools without re-parsing the source each time.

Examples:
  3d om part.scad '#valve'              # find the node with @id valve
  3d om part.scad '.structural'         # all nodes tagged structural
  3d om part.scad '.structural.removable'  # intersection of two classes

ROADMAP §18: "3d om — object-model query & transform language (jq for 3D).
  Reads a model from a file arg or stdin, applies a chained expression, and emits a
  model document to stdout that downstream 3d commands consume. Pipes compose in the
  shell; jq is the explicit analogy."
"""
from __future__ import annotations

import json
import os

from cli.registry import Command
from errors import InputNotFound, UsageError

USAGE = """3d om <file.scad> <selector>
  Query .scad object-model annotations and print matching nodes/anchors/styles as JSON.

Supported annotations:
  // @id <id>
  // @class <class...>
  // @anchor <name> pos=[x,y,z] dir=[x,y,z] optional note="..."
  // @color <name-or-hex>

Supported selectors:
  #id
  .class
  .class.other

Transform operations and descendant selectors are reserved and currently rejected."""


def run(argv: list[str]) -> int:
    if not argv:
        print(USAGE)
        return 1
    if argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0
    if len(argv) != 2:
        raise UsageError(
            "expected <file.scad> and <selector>",
            command="om",
            remediation=["Run: 3d om <file.scad> '#id' or 3d om <file.scad> '.class'"],
        )
    path, selector = argv
    if not os.path.isfile(path):
        raise InputNotFound(path, command="om")

    from object_model import model_to_dict, parse_scad_annotations, select_nodes

    with open(path, encoding="utf-8") as fh:
        model = parse_scad_annotations(fh.read(), source=path)
    selected = select_nodes(model, selector)
    print(json.dumps(model_to_dict(model, selected), indent=2, sort_keys=True))
    return 0


COMMAND = Command(
    name="om",
    group="GEOMETRY & EXPORT",
    summary="query .scad object-model annotations as JSON",
    usage=USAGE,
    run=run,
)
