"""On-disk result of a segmentation: instances.json + labels.png + masks/NNN.png + overlay.jpg (shared by `segment` and `detect --masks`)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import io
from .segment import instances_to_labelmap


def _remove_previous_masks(out_dir: Path) -> None:
    """Delete only the mask files a previous run listed in its instances.json (never other files in masks/)."""
    previous = out_dir / "instances.json"
    if not previous.is_file():
        return
    try:
        doc = json.loads(previous.read_text())
        rels = [str(r["mask"]) for r in doc["instances"]]
    except (OSError, ValueError, KeyError, TypeError):
        return
    for rel in rels:
        target = (out_dir / rel).resolve()
        if target.parent == (out_dir / "masks").resolve() and target.suffix == ".png":
            target.unlink(missing_ok=True)


def write_result(
    out_dir: Path, image_path: str, rgb: Any, result: dict[str, Any]
) -> dict[str, Any]:
    """Write instances.json + labels.png + masks/NNN.png + overlay.jpg; returns the instances.json document."""
    import cv2

    h, w = rgb.shape[:2]
    masks_dir = out_dir / "masks"
    masks_dir.mkdir(parents=True, exist_ok=True)
    _remove_previous_masks(out_dir)
    insts = result["instances"]
    records = []
    for inst in insts:
        rel = f"masks/{inst['id']:03d}.png"
        io.save_png(out_dir / rel, inst["mask"])
        records.append(
            {
                "id": inst["id"],
                "source": inst["source"],
                "bbox_xywh": inst["bbox_xywh"],
                "area_px": inst["area_px"],
                "bg_prob": round(inst["bg_prob"], 4),
                "stability": round(inst["stability"], 4),
                "mask": rel,
                "polygon": io.mask_polygon(inst["mask"]),
                **{k: inst[k] for k in ("label", "score", "box_xyxy") if k in inst},
            }
        )
    io.save_png(out_dir / "labels.png", instances_to_labelmap(insts, (h, w)))
    overlay = io.draw_overlay(rgb, [i["mask"] for i in insts], numbers=False)
    cv2.imwrite(
        str(out_dir / "overlay.jpg"),
        cv2.cvtColor(overlay, cv2.COLOR_RGB2BGR),
        [cv2.IMWRITE_JPEG_QUALITY, 90],
    )
    doc = {
        "image": {"path": str(image_path), "w": w, "h": h},
        **{k: v for k, v in result.items() if k != "instances"},
        "instances": records,
    }
    io.write_json(out_dir / "instances.json", doc)
    return doc
