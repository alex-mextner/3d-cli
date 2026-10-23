"""Cheap SAM2 mask generation: a point grid and box prompts decoded at the model's own 256x256 mask resolution.

`SAM2AutomaticMaskGenerator` upsamples every candidate mask (3 per grid point) to the photo's full resolution and scores it
there; on a 2000x1500 photo that post-processing, not the network, is the cost. Here the predicted-IoU / stability / NMS
filters run on the decoder's 256x256 logits (the logits the full-resolution masks are bilinearly upsampled from) and only
the surviving masks are upsampled.

All functions take a `SAM2ImagePredictor` whose image is already set; torch / sam2 / torchvision are imported lazily.
"""

from __future__ import annotations

import copy
import weakref
from collections.abc import Iterator
from typing import Any

import numpy as np

PRED_IOU_THRESH = 0.8
STABILITY_THRESH = 0.9
STABILITY_OFFSET = 1.0
BOX_NMS_THRESH = 0.6
MIN_REGION_AREA = 100  # holes / islands smaller than this (px at the processed resolution) are cleaned like SAM's AMG does
POINT_BATCH = 64  # decoder batch: 64 points x 3 masks x 256 x 256 logits = 50 MB
UPSAMPLE_BATCH = 16  # masks upsampled to photo size per step (16 x 3 MP floats = 200 MB of GPU memory)
BOX_CORNER_LABELS = (
    2,
    3,
)  # SAM2 prompt labels of a box's top-left and bottom-right corner


_HALF: weakref.WeakKeyDictionary[Any, Any] = weakref.WeakKeyDictionary()


def _decoder(model: Any) -> Any:
    """The mask decoder to run: a resident fp16 copy on CUDA (a third of the fp32 time: autocast would re-cast the weights on
    every call), the model's own decoder on CPU. The copy is built once per SAM2 model; the original stays fp32."""
    import torch

    if next(model.sam_mask_decoder.parameters()).device.type != "cuda":
        return model.sam_mask_decoder
    if model not in _HALF:
        _HALF[model] = copy.deepcopy(model.sam_mask_decoder).to(torch.float16)
    return _HALF[model]


def _decode(pred: Any, coords: Any, labels: Any, multimask: bool) -> tuple[Any, Any]:
    """Prompt encoder + mask decoder for a batch of independent prompts -> (low-res logits (B,C,256,256), predicted IoU (B,C))."""
    model = pred.model
    feats = pred._features
    decoder = _decoder(model)
    dtype = next(decoder.parameters()).dtype
    sparse, dense = model.sam_prompt_encoder(
        points=(coords, labels), boxes=None, masks=None
    )
    low, iou, _, _ = decoder(
        image_embeddings=feats["image_embed"][-1].unsqueeze(0).to(dtype),
        image_pe=model.sam_prompt_encoder.get_dense_pe().to(dtype),
        sparse_prompt_embeddings=sparse.to(dtype),
        dense_prompt_embeddings=dense.to(dtype),
        multimask_output=multimask,
        repeat_image=True,
        high_res_features=[
            f[-1].unsqueeze(0).to(dtype) for f in feats["high_res_feats"]
        ],
    )
    return low.float(), iou.float()


def _upsample(logits: Any, hw: tuple[int, int]) -> Any:
    import torch.nn.functional as F

    return (
        F.interpolate(
            logits[:, None].float(), hw, mode="bilinear", align_corners=False
        )[:, 0]
        > 0
    )


def _chunks(n: int, size: int) -> Iterator[slice]:
    for i in range(0, n, size):
        yield slice(i, min(i + size, n))


def grid_points(hw: tuple[int, int], points_per_side: int) -> np.ndarray:
    """(N, 2) pixel coordinates (x, y) of a `points_per_side`^2 grid in the cell centres of an image of size `hw`."""
    from sam2.utils.amg import build_point_grid

    h, w = hw
    return np.asarray(build_point_grid(points_per_side) * np.array([w, h]), np.float32)


def gate_points(
    points_px: np.ndarray,
    objectness: np.ndarray,
    hw: tuple[int, int],
    min_objectness: float,
) -> np.ndarray:
    """Keep the grid points that fall on (or next to) a patch whose OWLv2 objectness is at least `min_objectness`.

    `objectness` is OWLv2's (g, g) sigmoid map over the image padded to a square of side max(h, w). A point is kept when the max of
    its 3x3 patch neighbourhood passes, so an object's border still seeds masks; the bare carpet is not decoded at all.
    """
    import cv2

    h, w = hw
    g = objectness.shape[0]
    near = cv2.dilate(objectness.astype(np.float32), np.ones((3, 3), np.uint8))
    side = float(max(h, w))
    cx = np.clip((points_px[:, 0] / side * g).astype(int), 0, g - 1)
    cy = np.clip((points_px[:, 1] / side * g).astype(int), 0, g - 1)
    return points_px[near[cy, cx] >= min_objectness]


def grid_masks(
    pred: Any,
    hw: tuple[int, int],
    points_px: np.ndarray,
    points_per_batch: int = POINT_BATCH,
) -> list[dict[str, Any]]:
    """Automatic masks from one point prompt per row of `points_px` (x, y pixels): `[{"mask" bool HxW, "stability" float}]`.

    Same thresholds as the stock generator (predicted IoU > 0.8, stability >= 0.9, box NMS 0.6); candidates that are empty at
    256x256 resolution are dropped. Results are ordered by predicted IoU, best first. Written against sam2 1.1.0: the
    `SAM2ImagePredictor` internals (`_features`, `_transforms`, `model.sam_*`) are the ones its own `_predict` uses.
    """
    import torch
    from sam2.utils.amg import batched_mask_to_box, calculate_stability_score
    from torchvision.ops import nms

    h, w = hw
    if not len(points_px):
        return []
    pts = torch.as_tensor(points_px, dtype=torch.float32, device=pred.device)
    coords = pred._transforms.transform_coords(pts, normalize=True, orig_hw=hw)
    logit_parts, iou_parts, stab_parts, box_parts = [], [], [], []
    with torch.inference_mode():
        for sl in _chunks(len(coords), points_per_batch):
            c = coords[sl]
            low, iou = _decode(
                pred,
                c[:, None, :],
                torch.ones(len(c), 1, dtype=torch.int, device=c.device),
                True,
            )
            low, iou = low.flatten(0, 1), iou.flatten(0, 1)
            keep = iou > PRED_IOU_THRESH
            low, iou = low[keep], iou[keep]
            if not len(low):
                continue
            stab = calculate_stability_score(low, 0.0, STABILITY_OFFSET)
            keep = stab >= STABILITY_THRESH
            low, iou, stab = low[keep], iou[keep], stab[keep]
            if not len(low):
                continue
            masks = low > 0
            boxes = batched_mask_to_box(masks)
            # a one-column mask has a zero-extent box at 256x256 (~8 px on the photo): keep it, drop only empty ones
            keep = masks.flatten(1).any(dim=1)
            logit_parts.append(low[keep].half())
            iou_parts.append(iou[keep])
            stab_parts.append(stab[keep])
            box_parts.append(boxes[keep])
        if not logit_parts:
            return []
        logits, iou, stab, boxes = (
            torch.cat(p) for p in (logit_parts, iou_parts, stab_parts, box_parts)
        )
        order = nms(boxes.float(), iou, BOX_NMS_THRESH)
        logits, stab = logits[order], stab[order]
        out: list[dict[str, Any]] = []
        for sl in _chunks(len(logits), UPSAMPLE_BATCH):
            masks = _upsample(logits[sl], (h, w)).cpu().numpy()
            out += [
                {"mask": clean_mask(m), "stability": float(s)}
                for m, s in zip(masks, stab[sl].tolist(), strict=True)
            ]
    return out


def box_masks(
    pred: Any,
    hw: tuple[int, int],
    boxes_xyxy: np.ndarray,
    chunk: int = UPSAMPLE_BATCH,
) -> list[tuple[np.ndarray, float]]:
    """One SAM2 mask per box prompt, batched on the decoder: `[(bool HxW, predicted IoU)]` in box order (single-mask output)."""
    import torch

    if not len(boxes_xyxy):
        return []
    out: list[tuple[np.ndarray, float]] = []
    with torch.inference_mode():
        b = pred._transforms.transform_boxes(
            torch.as_tensor(boxes_xyxy, dtype=torch.float32, device=pred.device),
            normalize=True,
            orig_hw=hw,
        )
        labels = torch.tensor([BOX_CORNER_LABELS], dtype=torch.int, device=b.device)
        for sl in _chunks(len(b), chunk):
            # (B, 2, 2): a box prompt is its two corners
            corners = b[sl]
            low, iou = _decode(pred, corners, labels.repeat(len(corners), 1), False)
            masks = _upsample(low[:, 0], hw).cpu().numpy()
            out += [
                (m, float(s)) for m, s in zip(masks, iou[:, 0].tolist(), strict=True)
            ]
    return out


def clean_mask(mask: np.ndarray, min_area: int = MIN_REGION_AREA) -> np.ndarray:
    """Fill holes and drop islands smaller than `min_area` px (SAM's `min_mask_region_area`), working on the mask's bbox only.

    Like SAM's `remove_small_regions`, a mask whose every island is small keeps its largest one instead of vanishing.
    """
    import cv2

    rows, cols = np.flatnonzero(mask.any(axis=1)), np.flatnonzero(mask.any(axis=0))
    if not len(rows):
        return mask
    y0, y1, x0, x1 = rows[0], rows[-1] + 1, cols[0], cols[-1] + 1
    sub = np.pad(mask[y0:y1, x0:x1], 1).astype(np.uint8)
    # fill small holes: components of the background, except the one around the frame (the outside of the mask)
    _, lab, stats, _ = cv2.connectedComponentsWithStats(
        (sub == 0).astype(np.uint8), connectivity=8
    )
    holes = np.flatnonzero(stats[1:, cv2.CC_STAT_AREA] < min_area) + 1
    holes = holes[holes != lab[0, 0]]
    if len(holes):
        sub[np.isin(lab, holes)] = 1
    # drop small islands, keeping the largest component when all of them are small
    _, lab, stats, _ = cv2.connectedComponentsWithStats(sub, connectivity=8)
    areas = stats[1:, cv2.CC_STAT_AREA]
    small = np.flatnonzero(areas < min_area) + 1
    if len(small) == len(areas):
        small = small[small != int(np.argmax(areas)) + 1]
    if len(small):
        sub[np.isin(lab, small)] = 0
    out = np.zeros_like(mask)
    out[y0:y1, x0:x1] = sub[1:-1, 1:-1] > 0
    return out
