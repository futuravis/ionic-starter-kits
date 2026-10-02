"""
Work out the real running order of the day from the photographs themselves.

Folder names carry the photographer's intent but not reliably its order: some
are prefixed A.1/A.2, most are not, and alphabetical order puts "Ceremony"
before "varaverpu" when the varaverpu happened first.

EXIF DateTimeOriginal is the ground truth. This reads the capture time of every
photo, takes each shoot's median, and prints the day in the order it actually
happened, with a proposed prefix.

Read-only. Nothing is renamed.

    python3 -m tools.ceremony_order '/path/to/collection'
"""

from __future__ import annotations

import os
import statistics
import sys
from datetime import datetime
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wedding_album import scanner  # noqa: E402


EXIF_DATETIME_ORIGINAL = 36867
EXIF_DATETIME = 306


def capture_time(path: str) -> Optional[datetime]:
    """DateTimeOriginal if present, else the file's modification time."""
    try:
        from PIL import Image

        with Image.open(path) as img:
            exif = img.getexif()
            for tag in (EXIF_DATETIME_ORIGINAL, EXIF_DATETIME):
                raw = exif.get(tag)
                if raw:
                    try:
                        return datetime.strptime(str(raw), "%Y:%m:%d %H:%M:%S")
                    except ValueError:
                        continue
            # Some bodies only fill the sub-IFD.
            sub = exif.get_ifd(0x8769) if hasattr(exif, "get_ifd") else {}
            raw = sub.get(EXIF_DATETIME_ORIGINAL)
            if raw:
                try:
                    return datetime.strptime(str(raw), "%Y:%m:%d %H:%M:%S")
                except ValueError:
                    pass
    except Exception:  # noqa: BLE001 - fall through to mtime
        pass

    try:
        return datetime.fromtimestamp(os.path.getmtime(path))
    except OSError:
        return None


def shoot_times(root: str) -> List[Tuple[str, int, Optional[datetime],
                                        Optional[datetime], Optional[datetime]]]:
    """(folder, photo count, first, median, last) per shoot, in time order."""
    out = []
    for folder, photos in scanner.find_shoots(root):
        stamps = [t for t in (capture_time(p) for p in photos) if t]
        if not stamps:
            out.append((folder, len(photos), None, None, None))
            continue
        stamps.sort()
        median = stamps[len(stamps) // 2]
        out.append((folder, len(photos), stamps[0], median, stamps[-1]))

    dated = [r for r in out if r[3]]
    undated = [r for r in out if not r[3]]
    dated.sort(key=lambda r: r[3])
    return dated + undated


def main() -> int:
    root = os.path.expanduser(
        sys.argv[1] if len(sys.argv) > 1 else "~/Desktop/candid marriage"
    )
    rows = shoot_times(root)
    if not rows:
        print("no shoots found")
        return 1

    print(f"\n{len(rows)} shoots in the order they were photographed\n")
    print(f"{'#':>3}  {'when':<17} {'photos':>6}  shoot folder")
    print("-" * 86)

    for i, (folder, count, first, median, last) in enumerate(rows, 1):
        rel = os.path.relpath(folder, root)
        when = median.strftime("%d %b %H:%M") if median else "no date"
        print(f"{i:>3}  {when:<17} {count:>6}  {rel}")

    # Group by calendar day, since a wedding runs over more than one.
    print("\nby day")
    days: Dict[str, List[str]] = {}
    for folder, count, first, median, last in rows:
        key = median.strftime("%d %b %Y") if median else "undated"
        days.setdefault(key, []).append(
            f"{os.path.relpath(folder, root)} ({count})")
    for day in days:
        print(f"\n  {day}")
        for entry in days[day]:
            print(f"      {entry}")

    print("\nspans")
    for folder, count, first, median, last in rows:
        if not first:
            continue
        rel = os.path.relpath(folder, root)
        minutes = (last - first).total_seconds() / 60
        print(f"    {rel:<46} {first.strftime('%d %b %H:%M')} "
              f"-> {last.strftime('%H:%M')}  ({minutes:.0f} min)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
