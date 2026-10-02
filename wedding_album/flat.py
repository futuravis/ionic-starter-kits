"""
The flat album: one tree, three levels, three folders per phase.

    BestImages_v1/
    ├── 1_Reception_Eve/
    │   ├── 1_Getting_Ready/
    │   │   ├── Top/       001_Bride_7Y4A2103.JPG
    │   │   ├── Review/    r01_Couple_7Y4A2135.JPG
    │   │   └── Others/    dup_Bride_7Y4A3401.JPG
    │   └── 2_Couple_Portraits/ ...
    └── 2_Muhurtham/ ...

Replaces a layout that went eight levels deep before reaching a photograph.
Which section a photo belongs to now lives in its filename rather than costing a
folder level, and Top is ordered by capture time so the phase reads forward
through the evening.

One album for the whole wedding, written at the collection root, instead of
thirty-six scattered through the source folders.
"""

from __future__ import annotations

import csv
import os
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Sequence, Tuple

from . import config, organizer, scanner, sections, selection
from .sections import Assignment


REPORT_NAME = "_album_report.csv"

# Shortened section names for filenames. The numeric prefixes exist to order
# folders, which a filename does not need.
SECTION_TAG = {
    config.Section.BRIDE: "Bride",
    config.Section.GROOM: "Groom",
    config.Section.COUPLE: "Couple",
    config.Section.FAMILY: "Family",
    config.Section.FRIENDS: "Friends",
    config.Section.KIDS: "Kids",
    config.Section.GROUP: "Group",
    config.Section.NO_FACE: "NoFace",
    config.Section.UNSORTED: "Unknown",
}


@dataclass
class PhaseAlbum:
    """One phase of the day, and what the album keeps from it."""

    event: str
    phase: str
    photo_count: int = 0
    top: List[Assignment] = field(default_factory=list)
    review: List[Assignment] = field(default_factory=list)
    others: List[Assignment] = field(default_factory=list)
    groups: List[selection.ShotGroup] = field(default_factory=list)

    @property
    def label(self) -> str:
        return f"{self.event}/{self.phase}" if self.phase else self.event

    @property
    def directory(self) -> str:
        parts = [p for p in (self.event, self.phase) if p]
        return os.path.join(*parts) if parts else "_Root"


def build(
    root: str,
    assignments_by_shoot: Dict[str, List[Assignment]],
    phashes: Dict[str, str],
    times: Optional[Dict[str, datetime]] = None,
    picks_per_group: int = config.PICKS_PER_GROUP,
    percent: float = 0.0,
) -> List[PhaseAlbum]:
    """
    Decide what each phase contributes to the album.

    Selection happens per phase, not per shoot: Getting Ready is one page, so the
    best five come from the whole session rather than five from each of its four
    folders.
    """
    times = times or {}
    shoots = [(folder, []) for folder in assignments_by_shoot]
    grouped = scanner.group_shoots(root, [(f, p) for f, p in shoots])

    albums: List[PhaseAlbum] = []

    for (event, phase), members in grouped:
        pooled: List[Assignment] = []
        for folder, _ in members:
            pooled.extend(assignments_by_shoot.get(folder, []))

        album = PhaseAlbum(event=event, phase=phase, photo_count=len(pooled))

        eligible = [
            a for a in pooled
            if not a.is_unreadable and not a.is_duplicate
            and not a.is_low_quality
            and a.section is not config.Section.NO_FACE
        ]

        album.groups = selection.group_shots(eligible, phashes, times=times)
        picks = selection.choose(
            album.groups,
            per_section=0,
            percent=percent,
            alternates_per_pick=config.ALTERNATES_PER_PICK,
        )

        # choose() works per section; the phase wants a single ranked set, so the
        # per-section picks are pooled and trimmed to the phase's allowance,
        # strongest first and never twice from one moment until every moment has
        # been offered.
        candidates = [a for group in picks.values() for a in group]
        album.top = _trim(candidates, picks_per_group, percent, len(eligible))

        top_paths = {a.filepath for a in album.top}
        album.review = sorted(
            (a for a in eligible if a.filepath not in top_paths
             and (a.is_alternate or a.is_pick)),
            key=lambda a: -a.quality,
        )

        album.others = sorted(
            (a for a in pooled
             if a.is_duplicate or a.is_low_quality
             or a.section is config.Section.NO_FACE),
            key=lambda a: (a.bucket, -a.quality),
        )

        if times and config.ORDER_PICKS_BY_TIME:
            album.top.sort(key=lambda a: (times.get(a.filepath) or datetime.max,
                                          os.path.basename(a.filepath)))

        albums.append(album)

    return albums


def _trim(candidates: Sequence[Assignment], per_group: int,
          percent: float, available: int) -> List[Assignment]:
    """
    Cut the phase's candidates down to its allowance.

    Takes one photo per shot group before any group offers a second, so five
    picks are five different moments rather than five frames of the best one.
    """
    wanted = selection.target_count(available, per_section=per_group,
                                   percent=percent)
    if wanted <= 0:
        return []

    # Spread across sections first, then across moments within each.
    #
    # Ranking on quality alone gave Getting Ready five photographs of the bride
    # and none of the groom, in a phase that literally contains a folder called
    # "Get Ready Siva" - her portraits simply scored higher. A page of the album
    # should cover who was there before it optimises for the sharpest frame, so
    # each section is offered a place in turn, and only then does a section get a
    # second.
    by_section: Dict[config.Section, Dict[int, List[Assignment]]] = {}
    for a in sorted(candidates, key=lambda a: -a.quality):
        by_section.setdefault(a.section, {}).setdefault(a.shot_group, []).append(a)

    # Sections in album order, so Bride precedes Groom precedes Couple.
    order = [s for s in config.SECTION_ORDER if s in by_section]

    # Within a section, moments ranked by their best photo.
    queues: Dict[config.Section, List[Assignment]] = {}
    for section in order:
        groups = by_section[section]
        group_order = sorted(groups, key=lambda g: -groups[g][0].quality)
        flat_queue: List[Assignment] = []
        depth = 0
        while True:
            took = False
            for group in group_order:
                members = groups[group]
                if depth < len(members):
                    flat_queue.append(members[depth])
                    took = True
            if not took:
                break
            depth += 1
        queues[section] = flat_queue

    chosen: List[Assignment] = []
    position = 0
    while len(chosen) < wanted:
        took = False
        for section in order:
            if len(chosen) >= wanted:
                break
            queue = queues[section]
            if position < len(queue):
                chosen.append(queue[position])
                took = True
        if not took:
            break
        position += 1

    return chosen


# ============================================================================
# WRITING
# ============================================================================

def write(
    root: str,
    albums: Sequence[PhaseAlbum],
    output_name: str,
    clean: bool = False,
    preview_others: bool = True,
) -> Tuple[int, List[str], str]:
    """Copy the album into place. Returns (files written, errors, album path)."""
    album_dir = os.path.join(root, output_name)

    if os.path.isdir(album_dir):
        if not clean:
            raise organizer.OutputExists(
                f"{album_dir} already exists. Use a new name or pass --clean."
            )
        prefix, _ = organizer.split_version(os.path.basename(album_dir))
        if prefix != config.OUTPUT_DIR_PREFIX:
            raise RuntimeError(f"refusing to remove {album_dir}")
        shutil.rmtree(album_dir)

    errors: List[str] = []
    written = 0

    for album in albums:
        base = os.path.join(album_dir, album.directory)

        # Top is copied at full resolution - it is what goes to the printer.
        # Review and Others are previews: they exist to be looked through, and at
        # full size they would dwarf the album. On this collection Review alone
        # would have been 697 originals, around 5.5 GB, to hold photographs you
        # have not chosen. The filename names the original and the report records
        # its path, so promoting one is a copy away.
        jobs = [
            (config.TOP_DIR_NAME, album.top, False,
             lambda i, a: f"{i:03d}_{SECTION_TAG.get(a.section, 'Photo')}_"
                          f"{os.path.basename(a.filepath)}"),
            (config.REVIEW_DIR_NAME, album.review, preview_others,
             lambda i, a: f"r{i:02d}_{SECTION_TAG.get(a.section, 'Photo')}_"
                          f"{os.path.basename(a.filepath)}"),
            (config.OTHERS_DIR_NAME, album.others, preview_others,
             lambda i, a: f"{_others_tag(a)}_{SECTION_TAG.get(a.section, 'Photo')}_"
                          f"{os.path.basename(a.filepath)}"),
        ]

        for folder, items, as_preview, namer in jobs:
            if not items:
                continue
            target = os.path.join(base, folder)
            os.makedirs(target, exist_ok=True)

            for index, assignment in enumerate(items, 1):
                if assignment.is_unreadable:
                    continue
                destination = os.path.join(target, namer(index, assignment))
                try:
                    if as_preview and config.PREVIEW_REVIEW_BINS:
                        if organizer._write_preview(assignment.filepath, destination):
                            written += 1
                        else:
                            errors.append(
                                f"{os.path.basename(assignment.filepath)}: "
                                f"could not be read")
                    else:
                        shutil.copy2(assignment.filepath, destination)
                        written += 1
                except OSError as exc:
                    errors.append(f"{os.path.basename(assignment.filepath)}: {exc}")

    report = _write_report(root, album_dir, albums)
    return written, errors, album_dir


def _others_tag(a: Assignment) -> str:
    if a.is_duplicate:
        return "dup"
    if a.is_low_quality:
        return "soft"
    return "noface"


def _write_report(root: str, album_dir: str,
                  albums: Sequence[PhaseAlbum]) -> str:
    os.makedirs(album_dir, exist_ok=True)
    path = os.path.join(album_dir, REPORT_NAME)

    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow([
            "event", "phase", "bin", "position", "section", "filename",
            "quality", "shot_group", "reason", "people", "source_path",
        ])
        for album in albums:
            for folder, items in ((config.TOP_DIR_NAME, album.top),
                                  (config.REVIEW_DIR_NAME, album.review),
                                  (config.OTHERS_DIR_NAME, album.others)):
                for index, a in enumerate(items, 1):
                    writer.writerow([
                        album.event, album.phase, folder, index,
                        SECTION_TAG.get(a.section, ""),
                        os.path.basename(a.filepath),
                        f"{a.quality:.4f}", a.shot_group, a.reason,
                        "; ".join(a.person_names), a.filepath,
                    ])
    return path


# ============================================================================
# REPORTING
# ============================================================================

def summarize(albums: Sequence[PhaseAlbum]) -> str:
    lines = [f"    {'event / phase':<40} {'photos':>7} {'top':>5} "
             f"{'review':>7} {'others':>7}"]

    totals = [0, 0, 0, 0]
    for album in albums:
        label = album.label
        if len(label) > 39:
            label = "..." + label[-36:]
        lines.append(
            f"    {label:<40} {album.photo_count:>7} {len(album.top):>5} "
            f"{len(album.review):>7} {len(album.others):>7}"
        )
        totals[0] += album.photo_count
        totals[1] += len(album.top)
        totals[2] += len(album.review)
        totals[3] += len(album.others)

    lines.append(f"    {'-' * 40} {'-' * 7} {'-' * 5} {'-' * 7} {'-' * 7}")
    lines.append(f"    {'total':<40} {totals[0]:>7} {totals[1]:>5} "
                 f"{totals[2]:>7} {totals[3]:>7}")
    return "\n".join(lines)
