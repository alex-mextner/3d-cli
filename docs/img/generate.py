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
    """Torus (donut) cut by a plane, showing the hollow cross-section."""
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="1024" height="1024" viewBox="0 0 1024 1024">
  <rect width="1024" height="1024" fill="{BG}"/>
  <!-- Torus viewed from side, cut by a vertical plane -->
  <g transform="translate(512, 420)">
    <!-- Outer ring of the torus -->
    <ellipse cx="0" cy="0" rx="260" ry="100" fill="none" stroke="{DARK}" stroke-width="4"/>
    <!-- Inner ring (the hole) -->
    <ellipse cx="0" cy="0" rx="140" ry="40" fill="none" stroke="{DARK}" stroke-width="4"/>
    <!-- The cut plane -->
    <line x1="0" y1="-150" x2="0" y2="150" stroke="{TERRACOTTA}" stroke-width="4" stroke-dasharray="15,8"/>
    <text x="30" y="-120" font-size="32" text-anchor="start" fill="{TERRACOTTA}" font-family="sans-serif">cut plane</text>
    <!-- Cross-section: two concentric circles (the cut face) -->
    <g transform="translate(0, 0)">
      <!-- Outer circle of the cross-section -->
      <circle cx="0" cy="0" r="80" fill="{HIGHLIGHT}" opacity="0.7" stroke="{DARK}" stroke-width="3"/>
      <!-- Inner circle (the hole) -->
      <circle cx="0" cy="0" r="40" fill="{BG}" opacity="0.9" stroke="{DARK}" stroke-width="3"/>
      <!-- Hatching to show the solid material -->
      <line x1="-70" y1="-30" x2="-50" y2="-30" stroke="{DARK}" stroke-width="1" opacity="0.4"/>
      <line x1="-70" y1="-10" x2="-50" y2="-10" stroke="{DARK}" stroke-width="1" opacity="0.4"/>
      <line x1="-70" y1="10" x2="-50" y2="10" stroke="{DARK}" stroke-width="1" opacity="0.4"/>
      <line x1="-70" y1="30" x2="-50" y2="30" stroke="{DARK}" stroke-width="1" opacity="0.4"/>
      <line x1="50" y1="-30" x2="70" y2="-30" stroke="{DARK}" stroke-width="1" opacity="0.4"/>
      <line x1="50" y1="-10" x2="70" y2="-10" stroke="{DARK}" stroke-width="1" opacity="0.4"/>
      <line x1="50" y1="10" x2="70" y2="10" stroke="{DARK}" stroke-width="1" opacity="0.4"/>
      <line x1="50" y1="30" x2="70" y2="30" stroke="{DARK}" stroke-width="1" opacity="0.4"/>
    </g>
    <!-- Labels -->
    <text x="180" y="-10" font-size="28" text-anchor="start" fill="{DARK}" font-family="sans-serif">outer wall</text>
    <text x="-90" y="0" font-size="24" text-anchor="middle" fill="{DARK}" font-family="sans-serif">hollow</text>
    <text x="180" y="80" font-size="28" text-anchor="start" fill="{DARK}" font-family="sans-serif">inner wall</text>
  </g>
  <!-- Title -->
  <text x="512" y="850" font-size="48" text-anchor="middle" fill="{DARK}" font-family="sans-serif">Cross-Section</text>
  <text x="512" y="910" font-size="32" text-anchor="middle" fill="{DARK}" font-family="sans-serif">Cut plane reveals the hollow interior</text>
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
