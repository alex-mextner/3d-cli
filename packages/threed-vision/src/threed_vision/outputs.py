"""On-disk result of a segmentation: instances.json + labels.png + masks/NNN.png + overlay.jpg (shared by `segment` and `detect --masks`)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import io
from .segment import instances_to_labelmap


def write_result(
    out_dir: Path, image_path: str, rgb: Any, result: dict[str, Any]
) -> dict[str, Any]:
    """Write instances.json + labels.png + masks/NNN.png + overlay.jpg; returns the instances.json document."""
    import cv2

    h, w = rgb.shape[:2]
    masks_dir = out_dir / "masks"
    masks_dir.mkdir(parents=True, exist_ok=True)
    for stale in masks_dir.glob(
        "*.png"
    ):  # a re-run into the same DIR must not leave masks of a previous run
        stale.unlink()
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
