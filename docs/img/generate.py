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
    """Watertight cube vs broken cube with hole."""
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="1024" height="1024" viewBox="0 0 1024 1024">
  <rect width="1024" height="1024" fill="{BG}"/>
  <!-- Watertight cube -->
  <g transform="translate(180, 320)">
    <rect x="0" y="0" width="280" height="280" fill="{SAGE}" stroke="{DARK}" stroke-width="4"/>
    <text x="140" y="340" font-size="36" text-anchor="middle" fill="{DARK}" font-family="sans-serif">Watertight</text>
    <text x="140" y="380" font-size="28" text-anchor="middle" fill="{SAGE}" font-family="sans-serif">PASS</text>
  </g>
  <!-- Broken cube with hole -->
  <g transform="translate(564, 320)">
    <rect x="0" y="0" width="280" height="280" fill="{TERRACOTTA}" stroke="{DARK}" stroke-width="4"/>
    <circle cx="140" cy="140" r="60" fill="{BG}" stroke="{DARK}" stroke-width="4"/>
    <text x="140" y="340" font-size="36" text-anchor="middle" fill="{DARK}" font-family="sans-serif">Hole (non-manifold)</text>
    <text x="140" y="380" font-size="28" text-anchor="middle" fill="{TERRACOTTA}" font-family="sans-serif">FAIL</text>
  </g>
  <!-- Title -->
  <text x="512" y="180" font-size="48" text-anchor="middle" fill="{DARK}" font-family="sans-serif">Manifold Check</text>
</svg>"""
    return svg


def section() -> str:
    """Cube cut by a plane, cut face highlighted."""
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="1024" height="1024" viewBox="0 0 1024 1024">
  <rect width="1024" height="1024" fill="{BG}"/>
  <!-- Cube -->
  <g transform="translate(362, 280)">
    <!-- Back face -->
    <rect x="60" y="60" width="240" height="240" fill="{STEEL}" opacity="0.4" stroke="{DARK}" stroke-width="3"/>
    <!-- Front face -->
    <rect x="0" y="0" width="240" height="240" fill="{STEEL}" opacity="0.6" stroke="{DARK}" stroke-width="3"/>
    <!-- Cut plane (highlighted face) -->
    <polygon points="240,0 300,60 300,300 240,240" fill="{HIGHLIGHT}" opacity="0.8" stroke="{DARK}" stroke-width="3"/>
    <!-- Top edge -->
    <line x1="0" y1="0" x2="60" y2="60" stroke="{DARK}" stroke-width="3"/>
    <!-- Right edge -->
    <line x1="240" y1="0" x2="300" y2="60" stroke="{DARK}" stroke-width="3"/>
    <!-- Bottom edge -->
    <line x1="240" y1="240" x2="300" y2="300" stroke="{DARK}" stroke-width="3"/>
    <!-- Left edge -->
    <line x1="0" y1="240" x2="60" y2="300" stroke="{DARK}" stroke-width="3"/>
  </g>
  <!-- Section label -->
  <text x="512" y="820" font-size="48" text-anchor="middle" fill="{DARK}" font-family="sans-serif">Cross-Section</text>
  <text x="512" y="880" font-size="32" text-anchor="middle" fill="{DARK}" font-family="sans-serif">Cut face = highlighted</text>
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
