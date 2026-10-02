"""
Collection scanning: find the photos worth processing.

Deliberately excludes our own output folders. The test collection already
contains a `best/` tree from an earlier organizer run, and scanning that would
count the same photograph several times over.
"""

from __future__ import annotations

import os
from typing import Dict, List, Tuple

from . import config


def _excluded(dirname: str) -> bool:
    """
    Whether to skip a directory entirely.

    Prefix matching matters here: album folders are versioned, so a second run
    must skip BestImages_v1 as well as the BestImages_v2 it is about to write.
    Without it, run two would ingest run one's output and count every photo
    twice over.
    """
    if dirname.startswith("."):
        return True
    lowered = dirname.lower()
    if lowered in config.EXCLUDED_DIR_NAMES:
        return True
    return any(lowered.startswith(p) for p in config.EXCLUDED_DIR_PREFIXES)


def find_images(source_dir: str) -> List[str]:
    """
    Walk `source_dir` and return absolute paths to every usable image,
    sorted for a stable, reproducible processing order.
    """
    if not os.path.isdir(source_dir):
        raise NotADirectoryError(f"Source directory not found: {source_dir}")

    images: List[str] = []

    for root, dirs, files in os.walk(source_dir):
        # Prune in place so os.walk never descends into excluded trees.
        dirs[:] = [d for d in dirs if not _excluded(d)]

        for name in files:
            if name.startswith("."):          # .DS_Store and friends
                continue
            ext = os.path.splitext(name)[1].lower()
            if ext in config.IMAGE_EXTENSIONS:
                images.append(os.path.join(root, name))

    return sorted(images)


# ============================================================================
# SHOOTS
# ============================================================================

def find_shoots(root: str) -> List[Tuple[str, List[str]]]:
    """
    Split a collection into shoots, one per folder that directly holds photos.

    A wedding is not one shoot. The photographer already grouped the day into
    folders - "thali tie", "pillayar pooja", "late night shots" - and each of
    those is a separate occasion that deserves its own selection. Treating the
    whole tree as a single collection produces one album with the best photos of
    the day and nothing from the quieter ceremonies, which is the wrong answer.

    Folders are judged by the photos *directly* inside them, so a parent that
    only contains subfolders is not itself a shoot and nesting works without
    double-counting. Every photo therefore belongs to exactly one shoot.

    Returns (folder, photos) pairs sorted by path.
    """
    if not os.path.isdir(root):
        raise NotADirectoryError(f"Source directory not found: {root}")

    shoots: List[Tuple[str, List[str]]] = []

    for current, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if not _excluded(d)]

        here = sorted(
            os.path.join(current, f)
            for f in files
            if not f.startswith(".")
            and os.path.splitext(f)[1].lower() in config.IMAGE_EXTENSIONS
        )
        if here:
            shoots.append((current, here))

    return sorted(shoots, key=lambda pair: pair[0])


def group_key(root: str, folder: str) -> Tuple[str, str]:
    """
    The (event, phase) a shoot belongs to, taken from its path.

    The photographer already grouped the day - event at the first level, phase at
    the second - so the album follows that rather than inventing a scheme of its
    own. Everything below the second level is detail within one phase:

        1_Reception_Eve/1_Getting_Ready/A.2 Get Ready Siva
            -> ("1_Reception_Eve", "1_Getting_Ready")
        1_Reception_Eve/3_Varaverpu/Varaverpu/Nalangu
            -> ("1_Reception_Eve", "3_Varaverpu")

    A shoot sitting directly in an event folder has no phase, and a shoot at the
    collection root has neither.
    """
    rel = os.path.relpath(folder, root)
    if rel in (".", ""):
        return "", ""

    parts = [p for p in rel.split(os.sep) if p not in ("", ".")]
    event = parts[0] if parts else ""
    phase = parts[1] if len(parts) > 1 else ""
    return event, phase


def group_shoots(root: str, shoots: List[Tuple[str, List[str]]]
                 ) -> List[Tuple[Tuple[str, str], List[Tuple[str, List[str]]]]]:
    """
    Collect shoots into their phases, keeping the order the folders imply.

    A phase is the unit the album is built from: Getting Ready is one page even
    though it was shot as four folders, so the best five come from the session as
    a whole rather than five from each part of it.
    """
    grouped: Dict[Tuple[str, str], List[Tuple[str, List[str]]]] = {}
    for folder, photos in shoots:
        grouped.setdefault(group_key(root, folder), []).append((folder, photos))

    # Folder names carry their own order (1_, 2_, A.1), so sorting the keys
    # reproduces the running order of the day.
    return [(key, grouped[key]) for key in sorted(grouped)]


def describe_shoots(root: str, shoots: List[Tuple[str, List[str]]]) -> str:
    """Human-readable shoot listing."""
    if not shoots:
        return f"No photos found under {root}"

    total = sum(len(photos) for _, photos in shoots)
    lines = [f"{len(shoots)} shoot(s), {total} photo(s):"]
    for folder, photos in shoots:
        rel = os.path.relpath(folder, root)
        lines.append(f"    {len(photos):>5}  {'.' if rel == '.' else rel}")
    return "\n".join(lines)


def describe(source_dir: str, images: List[str]) -> str:
    """Short human-readable summary of what the scan picked up."""
    if not images:
        return f"No images found under {source_dir}"

    by_dir: dict[str, int] = {}
    for path in images:
        rel = os.path.relpath(os.path.dirname(path), source_dir)
        by_dir[rel] = by_dir.get(rel, 0) + 1

    lines = [f"Found {len(images)} images in {len(by_dir)} folder(s):"]
    for rel in sorted(by_dir):
        shown = "." if rel == "." else rel
        lines.append(f"    {by_dir[rel]:>5}  {shown}")
    return "\n".join(lines)
