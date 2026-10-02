"""
When each photograph was taken.

Time is not decoration here, it is structure. A wedding album is a story told in
order, and two facts follow from that:

  * Two photos that look alike but were taken hours apart are different moments,
    not a repeated shot. The couple posing during the portraits at 18:23 and
    posing again on stage at 21:57 both belong in the album. Grouping on
    appearance alone would throw one of them away.

  * Within a section, the picks should read in the order they happened, so the
    album follows the evening rather than a ranking.

EXIF DateTimeOriginal is the source. Reading it is cheap - the header only, no
pixels - so the whole collection can be timed in a couple of seconds, and the
result is cached alongside the face data.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Dict, Iterable, Optional

from . import config, store


EXIF_DATETIME_ORIGINAL = 36867
EXIF_DATETIME = 306
EXIF_SUB_IFD = 0x8769

CACHE_NAME = "times.json"


# ============================================================================
# READING
# ============================================================================

def capture_time(path: str) -> Optional[datetime]:
    """
    When this photo was taken.

    Falls back to the file's modification time when EXIF is absent, which is
    better than nothing: a camera writes files in the order it shoots them.
    """
    try:
        from PIL import Image

        with Image.open(path) as img:
            exif = img.getexif()

            for tag in (EXIF_DATETIME_ORIGINAL, EXIF_DATETIME):
                raw = exif.get(tag)
                parsed = _parse(raw)
                if parsed:
                    return parsed

            # Most cameras write the original timestamp into the sub-IFD only.
            if hasattr(exif, "get_ifd"):
                sub = exif.get_ifd(EXIF_SUB_IFD) or {}
                parsed = _parse(sub.get(EXIF_DATETIME_ORIGINAL))
                if parsed:
                    return parsed
    except Exception:  # noqa: BLE001 - a damaged file should not stop the run
        pass

    try:
        return datetime.fromtimestamp(os.path.getmtime(path))
    except OSError:
        return None


def _parse(raw) -> Optional[datetime]:
    if not raw:
        return None
    try:
        return datetime.strptime(str(raw).strip(), "%Y:%m:%d %H:%M:%S")
    except ValueError:
        return None


# ============================================================================
# COLLECTION PASS, CACHED
# ============================================================================

def _cache_path(root: str) -> str:
    return os.path.join(store.state_dir(root), CACHE_NAME)


def read_times(root: str, photos: Iterable[str],
               use_cache: bool = True) -> Dict[str, datetime]:
    """
    Capture time for every photo, reading only what is not already cached.

    Keyed by path and validated by the same size:mtime fingerprint the face
    cache uses, so an edited or replaced file is re-read.
    """
    photos = list(photos)
    cached: Dict[str, dict] = {}

    path = _cache_path(root)
    if use_cache and os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                blob = json.load(fh)
            if blob.get("schema") == 1:
                cached = blob.get("entries", {})
        except (OSError, ValueError):
            cached = {}

    out: Dict[str, datetime] = {}
    fresh = 0

    for photo in photos:
        entry = cached.get(photo)
        fingerprint = store.fingerprint(photo)

        if entry and entry.get("fingerprint") == fingerprint:
            parsed = _parse_iso(entry.get("taken"))
            if parsed:
                out[photo] = parsed
                continue

        taken = capture_time(photo)
        fresh += 1
        cached[photo] = {
            "fingerprint": fingerprint,
            "taken": taken.isoformat() if taken else "",
        }
        if taken:
            out[photo] = taken

    if fresh:
        store.ensure_state_dir(root)
        keep = set(photos)
        try:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(
                    {"schema": 1,
                     "entries": {k: v for k, v in cached.items() if k in keep}},
                    fh, indent=1,
                )
        except OSError:
            pass

    return out


def _parse_iso(raw: str) -> Optional[datetime]:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


# ============================================================================
# COMPARISON
# ============================================================================

def minutes_apart(a: Optional[datetime], b: Optional[datetime]) -> float:
    """
    Gap between two photos in minutes.

    Returns a deliberately huge number when either time is unknown, so an
    undated photo is never merged into someone else's moment on the strength of
    looking similar.
    """
    if a is None or b is None:
        return float("inf")
    return abs((a - b).total_seconds()) / 60.0


def same_moment(a: Optional[datetime], b: Optional[datetime],
                window_minutes: float = None) -> bool:
    """Whether two photos are close enough in time to be the same moment."""
    if window_minutes is None:
        window_minutes = config.SHOT_GROUP_WINDOW_MINUTES
    return minutes_apart(a, b) <= window_minutes


def describe_span(times: Dict[str, datetime]) -> str:
    if not times:
        return "no timestamps"
    values = sorted(times.values())
    return (f"{values[0].strftime('%d %b %H:%M')} -> "
            f"{values[-1].strftime('%d %b %H:%M')}")
