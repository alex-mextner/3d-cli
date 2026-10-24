#!/bin/bash
# build_standins.sh PROJECT_DIR — export the mole's stand-in STLs (board, housing, wires) into a cache dir; prints the dir.
# Reads the project read-only; writes only under ${XDG_CACHE_HOME:-~/.cache}/3d-cli/assembly-demo.
set -euo pipefail
project=${1:?usage: build_standins.sh PROJECT_DIR}
out=${XDG_CACHE_HOME:-$HOME/.cache}/3d-cli/assembly-demo
mkdir -p "$out"
cat >"$out/standins.scad" <<SCAD
include <$project/constants.scad>
include <$project/vitamins/REF_stand_in_hardware.scad>
which = "pcb";
translate([0, 0, pcb_zc(pr_t)]) {
  if (which == "pcb") pr_pcb();
  if (which == "housing") pr_housing();
  if (which == "wire_red") pr_wire(-1);
  if (which == "wire_brown") pr_wire(1);
}
SCAD
for part in pcb housing wire_red wire_brown; do
  openscad --backend=manifold --render -D "which=\"$part\"" -o "$out/$part.stl" "$out/standins.scad" >/dev/null 2>&1
done
echo "$out"
