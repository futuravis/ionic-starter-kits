"""
Visual review aids.

Clustering is the one stage whose output cannot be trusted from numbers alone:
a cohesion score of 0.71 tells you nothing about whether the tool merged two
sisters. So every discovered person gets a folder of face crops and a contact
sheet, and you decide by looking.

Crops are taken from the image resized to config.FACE_DETECT_MAX_DIM, the same
space the detector reported boxes in, so no coordinate rescaling is needed.
"""

from __future__ import annotations

import os
import shutil
from typing import List, Optional, Sequence

import cv2
import numpy as np

from . import config
from .people import Person


PREVIEW_DIR_NAME = "people_preview"

# Face crops are padded outward by this fraction of the box, since a tight
# crop on the eyes and mouth is hard for a human to recognize.
CROP_MARGIN = 0.45

THUMB_SIZE = 160
SHEET_COLUMNS = 8


def preview_dir(source_dir: str) -> str:
    return os.path.join(source_dir, config.STATE_DIR_NAME, PREVIEW_DIR_NAME)


# ============================================================================
# CROPPING
# ============================================================================

def _load_detect_space(filepath: str) -> Optional[np.ndarray]:
    """Decode and resize exactly as faces.FaceAnalyzer did."""
    img = cv2.imread(filepath, cv2.IMREAD_COLOR)
    if img is None or img.size == 0:
        return None
    h, w = img.shape[:2]
    longest = max(h, w)
    if longest > config.FACE_DETECT_MAX_DIM:
        scale = config.FACE_DETECT_MAX_DIM / longest
        img = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    return img


def crop_face(
    img: np.ndarray,
    box: tuple[int, int, int, int],
    margin: float = CROP_MARGIN,
) -> Optional[np.ndarray]:
    """Crop a padded square around a face box, clipped to the image."""
    x, y, w, h = box
    if w <= 0 or h <= 0:
        return None

    cx, cy = x + w / 2.0, y + h / 2.0
    side = max(w, h) * (1.0 + 2.0 * margin)

    ih, iw = img.shape[:2]
    x0 = int(max(0, round(cx - side / 2)))
    y0 = int(max(0, round(cy - side / 2)))
    x1 = int(min(iw, round(cx + side / 2)))
    y1 = int(min(ih, round(cy + side / 2)))

    if x1 - x0 < 8 or y1 - y0 < 8:
        return None
    return img[y0:y1, x0:x1]


# ============================================================================
# EXPORT
# ============================================================================

def export_people(
    people: Sequence[Person],
    source_dir: str,
    per_person: int = 12,
    clean: bool = True,
) -> str:
    """
    Write face crops for each person, clearest first, plus a contact sheet.

    Layout:
        .album_state/people_preview/
            p01/ 01_<photo>.jpg ...
            p01_sheet.jpg
            ALL_PEOPLE.jpg
    """
    out = preview_dir(source_dir)
    if clean and os.path.isdir(out):
        shutil.rmtree(out)
    os.makedirs(out, exist_ok=True)

    representatives: list[tuple[str, np.ndarray]] = []

    for person in people:
        person_dir = os.path.join(out, person.id)
        os.makedirs(person_dir, exist_ok=True)

        chosen = sorted(person.refs, key=lambda r: r.clarity, reverse=True)[:per_person]
        thumbs: List[np.ndarray] = []

        for rank, ref in enumerate(chosen, 1):
            img = _load_detect_space(ref.filepath)
            if img is None:
                continue
            box = _box_for(ref.filepath, ref.face_index)
            if box is None:
                continue
            patch = crop_face(img, box)
            if patch is None:
                continue

            name = f"{rank:02d}_{os.path.basename(ref.filepath)}"
            cv2.imwrite(os.path.join(person_dir, name), patch)
            thumbs.append(_square(patch, THUMB_SIZE))

        if thumbs:
            sheet = _grid(thumbs, SHEET_COLUMNS)
            cv2.imwrite(os.path.join(out, f"{person.id}_sheet.jpg"), sheet)
            representatives.append((person.id, thumbs[0]))

    if representatives:
        labelled = [_caption(img, pid) for pid, img in representatives]
        cv2.imwrite(os.path.join(out, "ALL_PEOPLE.jpg"), _grid(labelled, SHEET_COLUMNS))

    return out


# Boxes are needed for cropping but FaceRef intentionally carries only the
# embedding, so the analyzer's per-photo geometry is registered here by the
# caller before export.
_BOX_INDEX: dict[tuple[str, int], tuple[int, int, int, int]] = {}


def register_boxes(results) -> None:
    """Record face boxes from FaceResults so crops can be taken later."""
    _BOX_INDEX.clear()
    for result in results:
        if not result.ok:
            continue
        for index, face in enumerate(result.faces):
            _BOX_INDEX[(result.filepath, index)] = face.box


def _box_for(filepath: str, index: int) -> Optional[tuple[int, int, int, int]]:
    return _BOX_INDEX.get((filepath, index))


# ============================================================================
# IMAGE HELPERS
# ============================================================================

def _square(img: np.ndarray, size: int) -> np.ndarray:
    return cv2.resize(img, (size, size), interpolation=cv2.INTER_AREA)


def _caption(img: np.ndarray, text: str) -> np.ndarray:
    out = img.copy()
    h = out.shape[0]
    cv2.rectangle(out, (0, h - 24), (out.shape[1], h), (0, 0, 0), -1)
    cv2.putText(out, text, (5, h - 7), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 1,
                cv2.LINE_AA)
    return out


def _grid(tiles: Sequence[np.ndarray], columns: int) -> np.ndarray:
    """Tile equal-sized images into a contact sheet, padding the last row."""
    if not tiles:
        return np.zeros((1, 1, 3), dtype=np.uint8)

    size = tiles[0].shape[0]
    columns = max(1, min(columns, len(tiles)))
    rows = (len(tiles) + columns - 1) // columns

    blank = np.full((size, size, 3), 32, dtype=np.uint8)
    padded = list(tiles) + [blank] * (rows * columns - len(tiles))

    return np.vstack([
        np.hstack(padded[r * columns:(r + 1) * columns])
        for r in range(rows)
    ])
