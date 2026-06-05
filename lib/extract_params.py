#!/usr/bin/env python3
"""Extract customizable parameters from an OpenSCAD file.

Customizer comment syntax recognized:
  param = value;  // [min:max] Description
  param = value;  // [min:step:max] Description
  param = value;  // [opt1, opt2] Dropdown
  param = value;  // Plain description

Usage:  extract_params.py input.scad [--json]
"""
import sys
import re
import json


def extract(path):
    rows = []
    in_block = 0
    with open(path) as f:
        for line in f:
            in_block += line.count("{") - line.count("}")
            if in_block > 0:
                continue
            m = re.match(r"^\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*=\s*([^;]+);\s*(?://\s*(.*))?", line)
            if not m:
                continue
            name, value, comment = m.group(1), m.group(2).strip(), m.group(3) or ""
            if value in ("true", "false"):
                t = "boolean"
            elif re.match(r"^-?\d+$", value):
                t = "integer"
            elif re.match(r"^-?\d*\.?\d+$", value):
                t = "number"
            elif value.startswith('"') and value.endswith('"'):
                t = "string"; value = value[1:-1]
            elif value.startswith("["):
                t = "array"
            else:
                t = "expression"
            range_v = options_v = ""
            desc = comment
            bm = re.match(r"\[([^\]]+)\]\s*(.*)", comment)
            if bm:
                bc, desc = bm.group(1), bm.group(2)
                if ":" in bc and "," not in bc:
                    range_v = bc
                else:
                    options_v = bc
            rows.append({
                "name": name, "value": value, "type": t,
                "range": range_v, "options": options_v, "description": desc,
            })
    return rows


def main(argv):
    if len(argv) < 2:
        print("usage: extract_params.py input.scad [--json]", file=sys.stderr)
        return 1
    path = argv[1]
    as_json = "--json" in argv[2:]
    rows = extract(path)
    if as_json:
        out = []
        for r in rows:
            o = {"name": r["name"], "value": r["value"], "type": r["type"]}
            if r["range"]:
                o["range"] = r["range"]
            if r["options"]:
                o["options"] = r["options"]
            if r["description"]:
                o["description"] = r["description"]
            out.append(o)
        print(json.dumps(out, indent=2))
    else:
        print(f"Parameters in: {path}")
        print("=" * 60)
        print(f"{'NAME':20s} {'VALUE':15s} {'TYPE':10s} CONSTRAINT/DESC")
        print("-" * 60)
        for r in rows:
            c = ""
            if r["range"]:
                c = f"[{r['range']}]"
            elif r["options"]:
                c = f"[{r['options']}]"
            if r["description"]:
                c = (c + " " if c else "") + r["description"]
            print(f"{r['name']:20s} {r['value']:15s} {r['type']:10s} {c}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
