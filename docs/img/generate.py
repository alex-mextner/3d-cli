#!/usr/bin/env python3
"""Generate flat technical illustrations for the glossary.

Usage:
    python docs/img/generate.py

Produces docs/img/{iou,manifold,section,fit-camera}.png
"""
from __future__ import annotations

import os
import sys

# Palette from docs/DESIGN.md
SAGE = "#A3B59A"
TERRACOTTA = "#E6A57E"
STEEL = "#6B8E9B"
DARK = "#2A2A2A"
BG = "#F8F5F0"
HIGHLIGHT = "#D4C4A8"


def _save_svg(svg: str, path: str) -> None:
    # Write SVG directly; GitHub renders SVG inline in markdown.
    svg_path = path.replace(".png", ".svg")
    with open(svg_path, "w") as f:
        f.write(svg)
    print(f"generate: saved {svg_path}")


def iou() -> str:
    """Two overlapping circles: IoU = intersection / union."""
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="1024" height="1024" viewBox="0 0 1024 1024">
  <rect width="1024" height="1024" fill="{BG}"/>
  <!-- Circle A (sage) -->
  <circle cx="420" cy="512" r="280" fill="{SAGE}" opacity="0.6" stroke="{DARK}" stroke-width="3"/>
  <!-- Circle B (terracotta) -->
  <circle cx="604" cy="512" r="280" fill="{TERRACOTTA}" opacity="0.6" stroke="{DARK}" stroke-width="3"/>
  <!-- Intersection label -->
  <text x="512" y="530" font-size="36" text-anchor="middle" fill="{DARK}" font-family="sans-serif">Intersection</text>
  <!-- Union label -->
  <text x="512" y="200" font-size="36" text-anchor="middle" fill="{DARK}" font-family="sans-serif">Union</text>
  <!-- Formula -->
  <text x="512" y="920" font-size="48" text-anchor="middle" fill="{DARK}" font-family="sans-serif">IoU = |A ∩ B| / |A ∪ B|</text>
</svg>"""
    return svg


def manifold() -> str:
    """Manifold mesh (closed, watertight) vs non-manifold (broken edges, floating vertices, self-intersection)."""
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="1024" height="1024" viewBox="0 0 1024 1024">
  <rect width="1024" height="1024" fill="{BG}"/>
  <!-- Manifold mesh (clean closed quad) -->
  <g transform="translate(180, 300)">
    <text x="140" y="-40" font-size="36" text-anchor="middle" fill="{DARK}" font-family="sans-serif">Manifold (PASS)</text>
    <!-- A clean quad made of two triangles -->
    <polygon points="40,40 240,40 240,240 40,240" fill="{SAGE}" opacity="0.3" stroke="{SAGE}" stroke-width="2"/>
    <!-- Diagonal edge -->
    <line x1="40" y1="40" x2="240" y2="240" stroke="{SAGE}" stroke-width="2"/>
    <!-- Vertices -->
    <circle cx="40" cy="40" r="6" fill="{SAGE}"/>
    <circle cx="240" cy="40" r="6" fill="{SAGE}"/>
    <circle cx="240" cy="240" r="6" fill="{SAGE}"/>
    <circle cx="40" cy="240" r="6" fill="{SAGE}"/>
    <circle cx="140" cy="140" r="6" fill="{SAGE}"/>
    <!-- Labels -->
    <text x="140" y="280" font-size="24" text-anchor="middle" fill="{DARK}" font-family="sans-serif">closed watertight</text>
    <text x="140" y="310" font-size="24" text-anchor="middle" fill="{DARK}" font-family="sans-serif">every edge shared by 2 faces</text>
  </g>
  <!-- Non-manifold mesh (broken + floating + self-intersection) -->
  <g transform="translate(564, 300)">
    <text x="140" y="-40" font-size="36" text-anchor="middle" fill="{DARK}" font-family="sans-serif">Non-Manifold (FAIL)</text>
    <!-- Self-intersection: two triangles crossing -->
    <polygon points="40,40 240,240 240,40" fill="{TERRACOTTA}" opacity="0.2" stroke="{TERRACOTTA}" stroke-width="2"/>
    <polygon points="40,240 240,40 40,40" fill="{TERRACOTTA}" opacity="0.2" stroke="{TERRACOTTA}" stroke-width="2"/>
    <line x1="40" y1="40" x2="240" y2="240" stroke="{TERRACOTTA}" stroke-width="3"/>
    <line x1="40" y1="240" x2="240" y2="40" stroke="{TERRACOTTA}" stroke-width="3"/>
    <!-- Broken contour: disconnected edge -->
    <line x1="40" y1="240" x2="120" y2="240" stroke="{TERRACOTTA}" stroke-width="3" stroke-dasharray="8,4"/>
    <text x="80" y="265" font-size="20" text-anchor="middle" fill="{TERRACOTTA}" font-family="sans-serif">broken edge</text>
    <!-- Floating vertex -->
    <circle cx="200" cy="120" r="8" fill="{TERRACOTTA}"/>
    <text x="200" y="105" font-size="20" text-anchor="middle" fill="{TERRACOTTA}" font-family="sans-serif">floating vertex</text>
    <!-- Self-intersection label -->
    <text x="140" y="180" font-size="20" text-anchor="middle" fill="{TERRACOTTA}" font-family="sans-serif">self-intersection</text>
    <!-- Labels -->
    <text x="140" y="280" font-size="24" text-anchor="middle" fill="{DARK}" font-family="sans-serif">broken / floating / intersecting</text>
    <text x="140" y="310" font-size="24" text-anchor="middle" fill="{DARK}" font-family="sans-serif">not printable</text>
  </g>
  <!-- Title -->
  <text x="512" y="180" font-size="48" text-anchor="middle" fill="{DARK}" font-family="sans-serif">Manifold Check</text>
</svg>"""
    return svg


def section() -> str:
    """Cube with a hemispherical cavity, cut by a plane, showing the cross-section."""
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="1024" height="1024" viewBox="0 0 1024 1024">
  <rect width="1024" height="1024" fill="{BG}"/>
  
  <!-- SIDE VIEW: cube with hidden spherical cavity and cutting plane -->
  <g transform="translate(300, 420)">
    <text x="0" y="-180" font-size="32" text-anchor="middle" fill="{DARK}" font-family="sans-serif">Side View</text>
    <!-- Cube outline -->
    <rect x="-100" y="-100" width="200" height="200" fill="none" stroke="{DARK}" stroke-width="3"/>
    <!-- Hidden spherical cavity (hidden lines) -->
    <circle cx="0" cy="-20" r="60" fill="none" stroke="{DARK}" stroke-width="2" stroke-dasharray="6,4"/>
    <!-- Cutting plane line A-A -->
    <line x1="0" y1="-140" x2="0" y2="140" stroke="{TERRACOTTA}" stroke-width="3" stroke-dasharray="12,6"/>
    <!-- A-A label -->
    <text x="15" y="-130" font-size="24" text-anchor="start" fill="{TERRACOTTA}" font-family="sans-serif">A-A</text>
    <!-- Direction arrows -->
    <polygon points="-12,-130 0,-140 12,-130" fill="none" stroke="{TERRACOTTA}" stroke-width="2"/>
    <polygon points="-12,130 0,140 12,130" fill="none" stroke="{TERRACOTTA}" stroke-width="2"/>
    <!-- Labels -->
    <text x="-90" y="-120" font-size="20" text-anchor="start" fill="{DARK}" font-family="sans-serif">cube</text>
    <text x="70" y="-20" font-size="20" text-anchor="start" fill="{DARK}" font-family="sans-serif">cavity</text>
  </g>

  <!-- SECTION VIEW A-A: cube cut through center showing hemispherical cavity -->
  <g transform="translate(724, 420)">
    <text x="0" y="-180" font-size="32" text-anchor="middle" fill="{DARK}" font-family="sans-serif">Section View A-A</text>
    <!-- Cube cross-section (hatched solid material) -->
    <rect x="-100" y="-100" width="200" height="200" fill="{HIGHLIGHT}" opacity="0.3" stroke="{DARK}" stroke-width="3"/>
    <!-- HATCHING: 45° lines on cut face -->
    <line x1="-90" y1="-90" x2="-70" y2="-70" stroke="{DARK}" stroke-width="1" opacity="0.35"/>
    <line x1="-70" y1="-90" x2="-50" y2="-70" stroke="{DARK}" stroke-width="1" opacity="0.35"/>
    <line x1="-50" y1="-90" x2="-30" y2="-70" stroke="{DARK}" stroke-width="1" opacity="0.35"/>
    <line x1="-30" y1="-90" x2="-10" y2="-70" stroke="{DARK}" stroke-width="1" opacity="0.35"/>
    <line x1="-10" y1="-90" x2="10" y2="-70" stroke="{DARK}" stroke-width="1" opacity="0.35"/>
    <line x1="10" y1="-90" x2="30" y2="-70" stroke="{DARK}" stroke-width="1" opacity="0.35"/>
    <line x1="30" y1="-90" x2="50" y2="-70" stroke="{DARK}" stroke-width="1" opacity="0.35"/>
    <line x1="50" y1="-90" x2="70" y2="-70" stroke="{DARK}" stroke-width="1" opacity="0.35"/>
    <line x1="70" y1="-90" x2="90" y2="-70" stroke="{DARK}" stroke-width="1" opacity="0.35"/>
    <line x1="90" y1="-90" x2="110" y2="-70" stroke="{DARK}" stroke-width="1" opacity="0.35"/>
    <line x1="-90" y1="-70" x2="-70" y2="-50" stroke="{DARK}" stroke-width="1" opacity="0.35"/>
    <line x1="-70" y1="-70" x2="-50" y2="-50" stroke="{DARK}" stroke-width="1" opacity="0.35"/>
    <line x1="-50" y1="-70" x2="-30" y2="-50" stroke="{DARK}" stroke-width="1" opacity="0.35"/>
    <line x1="-30" y1="-70" x2="-10" y2="-50" stroke="{DARK}" stroke-width="1" opacity="0.35"/>
    <line x1="-10" y1="-70" x2="10" y2="-50" stroke="{DARK}" stroke-width="1" opacity="0.35"/>
    <line x1="10" y1="-70" x2="30" y2="-50" stroke="{DARK}" stroke-width="1" opacity="0.35"/>
    <line x1="30" y1="-70" x2="50" y2="-50" stroke="{DARK}" stroke-width="1" opacity="0.35"/>
    <line x1="50" y1="-70" x2="70" y2="-50" stroke="{DARK}" stroke-width="1" opacity="0.35"/>
    <line x1="70" y1="-70" x2="90" y2="-50" stroke="{DARK}" stroke-width="1" opacity="0.35"/>
    <line x1="90" y1="-70" x2="110" y2="-50" stroke="{DARK}" stroke-width="1" opacity="0.35"/>
    <line x1="-90" y1="-50" x2="-70" y2="-30" stroke="{DARK}" stroke-width="1" opacity="0.35"/>
    <line x1="-70" y1="-50" x2="-50" y2="-30" stroke="{DARK}" stroke-width="1" opacity="0.35"/>
    <line x1="-50" y1="-50" x2="-30" y2="-30" stroke="{DARK}" stroke-width="1" opacity="0.35"/>
    <line x1="-30" y1="-50" x2="-10" y2="-30" stroke="{DARK}" stroke-width="1" opacity="0.35"/>
    <line x1="-10" y1="-50" x2="10" y2="-30" stroke="{DARK}" stroke-width="1" opacity="0.35"/>
    <line x1="10" y1="-50" x2="30" y2="-30" stroke="{DARK}" stroke-width="1" opacity="0.35"/>
    <line x1="30" y1="-50" x2="50" y2="-30" stroke="{DARK}" stroke-width="1" opacity="0.35"/>
    <line x1="50" y1="-50" x2="70" y2="-30" stroke="{DARK}" stroke-width="1" opacity="0.35"/>
    <line x1="70" y1="-50" x2="90" y2="-30" stroke="{DARK}" stroke-width="1" opacity="0.35"/>
    <line x1="90" y1="-50" x2="110" y2="-30" stroke="{DARK}" stroke-width="1" opacity="0.35"/>
    <line x1="-90" y1="-30" x2="-70" y2="-10" stroke="{DARK}" stroke-width="1" opacity="0.35"/>
    <line x1="-70" y1="-30" x2="-50" y2="-10" stroke="{DARK}" stroke-width="1" opacity="0.35"/>
    <line x1="-50" y1="-30" x2="-30" y2="-10" stroke="{DARK}" stroke-width="1" opacity="0.35"/>
    <line x1="-30" y1="-30" x2="-10" y2="-10" stroke="{DARK}" stroke-width="1" opacity="0.35"/>
    <line x1="-10" y1="-30" x2="10" y2="-10" stroke="{DARK}" stroke-width="1" opacity="0.35"/>
    <line x1="10" y1="-30" x2="30" y2="-10" stroke="{DARK}" stroke-width="1" opacity="0.35"/>
    <line x1="30" y1="-30" x2="50" y2="-10" stroke="{DARK}" stroke-width="1" opacity="0.35"/>
    <line x1="50" y1="-30" x2="70" y2="-10" stroke="{DARK}" stroke-width="1" opacity="0.35"/>
    <line x1="70" y1="-30" x2="90" y2="-10" stroke="{DARK}" stroke-width="1" opacity="0.35"/>
    <line x1="90" y1="-30" x2="110" y2="-10" stroke="{DARK}" stroke-width="1" opacity="0.35"/>
    <line x1="-90" y1="-10" x2="-70" y2="10" stroke="{DARK}" stroke-width="1" opacity="0.35"/>
    <line x1="-70" y1="-10" x2="-50" y2="10" stroke="{DARK}" stroke-width="1" opacity="0.35"/>
    <line x1="-50" y1="-10" x2="-30" y2="10" stroke="{DARK}" stroke-width="1" opacity="0.35"/>
    <line x1="-30" y1="-10" x2="-10" y2="10" stroke="{DARK}" stroke-width="1" opacity="0.35"/>
    <line x1="-10" y1="-10" x2="10" y2="10" stroke="{DARK}" stroke-width="1" opacity="0.35"/>
    <line x1="10" y1="-10" x2="30" y2="10" stroke="{DARK}" stroke-width="1" opacity="0.35"/>
    <line x1="30" y1="-10" x2="50" y2="10" stroke="{DARK}" stroke-width="1" opacity="0.35"/>
    <line x1="50" y1="-10" x2="70" y2="10" stroke="{DARK}" stroke-width="1" opacity="0.35"/>
    <line x1="70" y1="-10" x2="90" y2="10" stroke="{DARK}" stroke-width="1" opacity="0.35"/>
    <line x1="90" y1="-10" x2="110" y2="10" stroke="{DARK}" stroke-width="1" opacity="0.35"/>
    <line x1="-90" y1="10" x2="-70" y2="30" stroke="{DARK}" stroke-width="1" opacity="0.35"/>
    <line x1="-70" y1="10" x2="-50" y2="30" stroke="{DARK}" stroke-width="1" opacity="0.35"/>
    <line x1="-50" y1="10" x2="-30" y2="30" stroke="{DARK}" stroke-width="1" opacity="0.35"/>
    <line x1="-30" y1="10" x2="-10" y2="30" stroke="{DARK}" stroke-width="1" opacity="0.35"/>
    <line x1="-10" y1="10" x2="10" y2="30" stroke="{DARK}" stroke-width="1" opacity="0.35"/>
    <line x1="10" y1="10" x2="30" y2="30" stroke="{DARK}" stroke-width="1" opacity="0.35"/>
    <line x1="30" y1="10" x2="50" y2="30" stroke="{DARK}" stroke-width="1" opacity="0.35"/>
    <line x1="50" y1="10" x2="70" y2="30" stroke="{DARK}" stroke-width="1" opacity="0.35"/>
    <line x1="70" y1="10" x2="90" y2="30" stroke="{DARK}" stroke-width="1" opacity="0.35"/>
    <line x1="90" y1="10" x2="110" y2="30" stroke="{DARK}" stroke-width="1" opacity="0.35"/>
    <line x1="-90" y1="30" x2="-70" y2="50" stroke="{DARK}" stroke-width="1" opacity="0.35"/>
    <line x1="-70" y1="30" x2="-50" y2="50" stroke="{DARK}" stroke-width="1" opacity="0.35"/>
    <line x1="-50" y1="30" x2="-30" y2="50" stroke="{DARK}" stroke-width="1" opacity="0.35"/>
    <line x1="-30" y1="30" x2="-10" y2="50" stroke="{DARK}" stroke-width="1" opacity="0.35"/>
    <line x1="-10" y1="30" x2="10" y2="50" stroke="{DARK}" stroke-width="1" opacity="0.35"/>
    <line x1="10" y1="30" x2="30" y2="50" stroke="{DARK}" stroke-width="1" opacity="0.35"/>
    <line x1="30" y1="30" x2="50" y2="50" stroke="{DARK}" stroke-width="1" opacity="0.35"/>
    <line x1="50" y1="30" x2="70" y2="50" stroke="{DARK}" stroke-width="1" opacity="0.35"/>
    <line x1="70" y1="30" x2="90" y2="50" stroke="{DARK}" stroke-width="1" opacity="0.35"/>
    <line x1="90" y1="30" x2="110" y2="50" stroke="{DARK}" stroke-width="1" opacity="0.35"/>
    <line x1="-90" y1="50" x2="-70" y2="70" stroke="{DARK}" stroke-width="1" opacity="0.35"/>
    <line x1="-70" y1="50" x2="-50" y2="70" stroke="{DARK}" stroke-width="1" opacity="0.35"/>
    <line x1="-50" y1="50" x2="-30" y2="70" stroke="{DARK}" stroke-width="1" opacity="0.35"/>
    <line x1="-30" y1="50" x2="-10" y2="70" stroke="{DARK}" stroke-width="1" opacity="0.35"/>
    <line x1="-10" y1="50" x2="10" y2="70" stroke="{DARK}" stroke-width="1" opacity="0.35"/>
    <line x1="10" y1="50" x2="30" y2="70" stroke="{DARK}" stroke-width="1" opacity="0.35"/>
    <line x1="30" y1="50" x2="50" y2="70" stroke="{DARK}" stroke-width="1" opacity="0.35"/>
    <line x1="50" y1="50" x2="70" y2="70" stroke="{DARK}" stroke-width="1" opacity="0.35"/>
    <line x1="70" y1="50" x2="90" y2="70" stroke="{DARK}" stroke-width="1" opacity="0.35"/>
    <line x1="90" y1="50" x2="110" y2="70" stroke="{DARK}" stroke-width="1" opacity="0.35"/>
    <line x1="-90" y1="70" x2="-70" y2="90" stroke="{DARK}" stroke-width="1" opacity="0.35"/>
    <line x1="-70" y1="70" x2="-50" y2="90" stroke="{DARK}" stroke-width="1" opacity="0.35"/>
    <line x1="-50" y1="70" x2="-30" y2="90" stroke="{DARK}" stroke-width="1" opacity="0.35"/>
    <line x1="-30" y1="70" x2="-10" y2="90" stroke="{DARK}" stroke-width="1" opacity="0.35"/>
    <line x1="-10" y1="70" x2="10" y2="90" stroke="{DARK}" stroke-width="1" opacity="0.35"/>
    <line x1="10" y1="70" x2="30" y2="90" stroke="{DARK}" stroke-width="1" opacity="0.35"/>
    <line x1="30" y1="70" x2="50" y2="90" stroke="{DARK}" stroke-width="1" opacity="0.35"/>
    <line x1="50" y1="70" x2="70" y2="90" stroke="{DARK}" stroke-width="1" opacity="0.35"/>
    <line x1="70" y1="70" x2="90" y2="90" stroke="{DARK}" stroke-width="1" opacity="0.35"/>
    <line x1="90" y1="70" x2="110" y2="90" stroke="{DARK}" stroke-width="1" opacity="0.35"/>
    <!-- Hemispherical cavity (unhatched, filled with background color) -->
    <path d="M -60,-80 A 60,60 0 0,1 60,-80 Z" fill="{BG}" stroke="{DARK}" stroke-width="2"/>
    <!-- Labels -->
    <text x="0" y="130" font-size="24" text-anchor="middle" fill="{DARK}" font-family="sans-serif">hemispherical cavity</text>
    <text x="0" y="160" font-size="20" text-anchor="middle" fill="{DARK}" font-family="sans-serif">cut face hatched</text>
  </g>
  <!-- Title -->
  <text x="512" y="920" font-size="48" text-anchor="middle" fill="{DARK}" font-family="sans-serif">Cross-Section</text>
  <text x="512" y="970" font-size="28" text-anchor="middle" fill="{DARK}" font-family="sans-serif">Cut plane reveals hemispherical cavity inside cube</text>
</svg>"""
    return svg


def fit_camera() -> str:
    """Camera frustum aiming at object with azimuth/elevation arrows."""
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="1024" height="1024" viewBox="0 0 1024 1024">
  <rect width="1024" height="1024" fill="{BG}"/>
  <!-- Object (cube) -->
  <g transform="translate(462, 462)">
    <rect x="0" y="0" width="100" height="100" fill="{SAGE}" stroke="{DARK}" stroke-width="3"/>
    <circle cx="50" cy="50" r="30" fill="none" stroke="{DARK}" stroke-width="2"/>
  </g>
  <!-- Camera frustum -->
  <g>
    <polygon points="200,200 824,200 612,512 412,512" fill="none" stroke="{STEEL}" stroke-width="4" stroke-dasharray="10,5"/>
    <!-- Camera icon -->
    <rect x="160" y="160" width="80" height="60" rx="8" fill="{STEEL}" stroke="{DARK}" stroke-width="3"/>
    <circle cx="200" cy="190" r="16" fill="{BG}" stroke="{DARK}" stroke-width="2"/>
  </g>
  <!-- Azimuth arrow -->
  <path d="M 512,350 A 160,160 0 0,1 672,350" fill="none" stroke="{TERRACOTTA}" stroke-width="4" marker-end="url(#arrow)"/>
  <text x="592" y="330" font-size="28" text-anchor="middle" fill="{TERRACOTTA}" font-family="sans-serif">azimuth</text>
  <!-- Elevation arrow -->
  <path d="M 350,512 A 160,160 0 0,0 350,352" fill="none" stroke="{TERRACOTTA}" stroke-width="4" marker-end="url(#arrow)"/>
  <text x="320" y="432" font-size="28" text-anchor="middle" fill="{TERRACOTTA}" font-family="sans-serif">elevation</text>
  <!-- Arrow marker -->
  <defs>
    <marker id="arrow" markerWidth="10" markerHeight="10" refX="9" refY="3" orient="auto" markerUnits="strokeWidth">
      <path d="M0,0 L0,6 L9,3 z" fill="{TERRACOTTA}"/>
    </marker>
  </defs>
  <!-- Title -->
  <text x="512" y="920" font-size="48" text-anchor="middle" fill="{DARK}" font-family="sans-serif">Fit Camera</text>
</svg>"""
    return svg


def main() -> int:
    out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)))
    os.makedirs(out_dir, exist_ok=True)

    _save_svg(iou(), os.path.join(out_dir, "iou.png"))
    _save_svg(manifold(), os.path.join(out_dir, "manifold.png"))
    _save_svg(section(), os.path.join(out_dir, "section.png"))
    _save_svg(fit_camera(), os.path.join(out_dir, "fit-camera.png"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
