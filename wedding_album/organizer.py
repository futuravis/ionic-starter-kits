"""
Writing the album to disk.

Copies each photo into its section folder and writes a CSV recording every
decision. Originals are only ever read: nothing is moved, renamed or deleted in
the source collection, so a bad run costs you disk space and nothing else.

Output:

    <collection>/BestImages/
        01_Bride/001_7Y4A2107.JPG
        02_Groom/001_7Y4A2114.JPG
        03_Couple/001_7Y4A2136.JPG
        ...
        _album_report.csv

Filenames carry a rank prefix so that browsing a folder by name shows the
strongest photographs first, which no file manager will do by quality on its
own.
"""

from __future__ import annotations

import csv
import os
import re
import shutil
from datetime import datetime
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from . import config
from .config import Section
from .sections import Assignment


REPORT_NAME = "_album_report.csv"


# ============================================================================
# PLANNING
# ============================================================================

@dataclass
class Copy:
    """One intended file copy."""

    source: str
    destination: str
    section: Section
    rank: int
    assignment: Assignment
    is_pick: bool = True
    bucket: str = "pick"
    # Review bins get a downscaled JPEG rather than the original, so rejects
    # cannot outweigh the album on disk.
    preview_only: bool = False


@dataclass
class Plan:
    """Everything a run intends to do, before it does any of it."""

    album_dir: str
    copies: List[Copy] = field(default_factory=list)
    skipped_duplicates: List[Assignment] = field(default_factory=list)
    skipped_beyond_top: List[Assignment] = field(default_factory=list)

    @property
    def picks(self) -> List[Copy]:
        return [c for c in self.copies if c.is_pick]

    @property
    def runners_up(self) -> List[Copy]:
        return [c for c in self.copies if not c.is_pick]

    @property
    def sections_used(self) -> List[Section]:
        seen: List[Section] = []
        for c in self.copies:
            if c.section not in seen:
                seen.append(c.section)
        return [s for s in config.SECTION_ORDER if s in seen]

    def counts(self) -> Dict[Section, int]:
        out: Dict[Section, int] = {}
        for c in self.copies:
            out[c.section] = out.get(c.section, 0) + 1
        return out

    @property
    def total_bytes(self) -> int:
        total = 0
        for c in self.copies:
            try:
                total += os.path.getsize(c.source)
            except OSError:
                pass
        return total


def album_dir(source_dir: str, name: Optional[str] = None) -> str:
    return os.path.join(source_dir, name or config.OUTPUT_DIR_NAME)


# ----------------------------------------------------------------------------
# VERSIONED FOLDER NAMES
# ----------------------------------------------------------------------------

_VERSION_RE = re.compile(r"^(?P<prefix>.+?)_v(?P<n>\d+)$")


def split_version(name: str) -> Tuple[str, int]:
    """Split "BestImages_v3" into ("BestImages", 3). Unversioned names get 0."""
    match = _VERSION_RE.match(name)
    if not match:
        return name, 0
    return match.group("prefix"), int(match.group("n"))


def existing_versions(source_dir: str, prefix: str) -> List[int]:
    """Version numbers of album folders already present for this prefix."""
    if not os.path.isdir(source_dir):
        return []

    found: List[int] = []
    for entry in os.listdir(source_dir):
        if not os.path.isdir(os.path.join(source_dir, entry)):
            continue
        entry_prefix, number = split_version(entry)
        if entry_prefix == prefix and number > 0:
            found.append(number)
    return sorted(found)


def next_version_name(source_dir: str, prefix: Optional[str] = None) -> str:
    """
    Suggest the next unused versioned folder name.

    Versioning by default means a second run never destroys the first. You can
    compare BestImages_v1 against BestImages_v2 after changing a threshold,
    which is most of the point of having thresholds.
    """
    prefix = prefix or config.OUTPUT_DIR_PREFIX
    used = existing_versions(source_dir, prefix)
    return f"{prefix}_v{(used[-1] + 1) if used else 1}"


def latest_version_name(source_dir: str,
                        prefix: Optional[str] = None) -> Optional[str]:
    """The highest existing version, or None if nothing has been written yet."""
    prefix = prefix or config.OUTPUT_DIR_PREFIX
    used = existing_versions(source_dir, prefix)
    return f"{prefix}_v{used[-1]}" if used else None


def target_version_name(source_dir: str, clean: bool = False,
                        explicit: Optional[str] = None,
                        prefix: Optional[str] = None) -> str:
    """
    Decide which folder this run should write to.

    Asking to replace the album has to mean replacing the *existing* one.
    Combining "clean" with a freshly incremented name would quietly do neither:
    it would leave the old album untouched and write a new one beside it, which
    is the opposite of what was asked and doubles the disk used.
    """
    if explicit:
        return explicit
    if clean:
        return latest_version_name(source_dir, prefix) \
            or next_version_name(source_dir, prefix)
    return next_version_name(source_dir, prefix)


# Sorts undated photos last rather than crashing on a None comparison.
_LATEST = datetime.max


def _unsorted_bucket(a: Assignment) -> str:
    """
    Sub-folder within Unsorted, naming the face that recurs in it.

    Returns "" for every other section, so nothing else gains a level.

    Unsorted is the one section where the tool genuinely does not know what it is
    looking at, and a flat folder of strangers gives you no way in. Grouping by
    the recurring figure does: a folder labelled with a face that appears in 210
    photos is an obvious place to start, and naming that one person empties it.
    Faces seen once or twice are pooled, since they are passers-by and a folder
    each would only be noise.
    """
    if a.section is not Section.UNSORTED:
        return ""
    if a.dominant_unknown_photos >= config.UNSORTED_COMMON_MIN_PHOTOS:
        return f"{a.dominant_unknown}_in_{a.dominant_unknown_photos}_photos"
    return config.UNSORTED_OTHERS_DIR_NAME


def build_plan(
    assignments: Sequence[Assignment],
    source_dir: str,
    picks: Optional[Dict[Section, List[Assignment]]] = None,
    output_name: Optional[str] = None,
    times: Optional[Dict[str, datetime]] = None,
) -> Plan:
    """
    Work out every copy without performing any of them.

    When `picks` is given, chosen photos go to the section root numbered in pick
    order and everything else goes to <section>/2nd_Option/. Without it, every
    photo is treated as a pick and ranked by quality, which is the old flat
    behaviour.
    """
    plan = Plan(album_dir=album_dir(source_dir, output_name))

    by_section: Dict[Section, List[Assignment]] = {}
    for a in assignments:
        by_section.setdefault(a.section, []).append(a)

    best_dir = os.path.join(plan.album_dir, config.BEST_DIR_NAME)
    first_option_dir = os.path.join(best_dir, config.FIRST_OPTION_DIR_NAME)
    second_option_dir = os.path.join(best_dir, config.SECOND_OPTION_DIR_NAME)
    duplicates_dir = os.path.join(plan.album_dir, config.DUPLICATES_DIR_NAME)
    low_quality_dir = os.path.join(plan.album_dir, config.LOW_QUALITY_DIR_NAME)
    no_face_dir = os.path.join(plan.album_dir, config.NO_FACE_DIR_NAME)

    for section in config.SECTION_ORDER:
        members = by_section.get(section)
        if not members:
            continue

        if picks is None:
            chosen = sorted(members, key=lambda a: (-a.quality,
                                                    os.path.basename(a.filepath)))
            rest: List[Assignment] = []
        else:
            chosen = list(picks.get(section, []))
            chosen_paths = {a.filepath for a in chosen}
            # Only marked alternates come along. Everything else stays where it
            # is, which is the difference between an album and a second copy of
            # the collection.
            rest = sorted(
                (a for a in members
                 if a.filepath not in chosen_paths and a.is_alternate),
                key=lambda a: (a.shot_group, -a.quality),
            )

        # Number the picks in the order they were taken, so 001, 002, 003 walks
        # forward through the evening. The choice of which photo represents each
        # moment is still made on quality; only the sequence follows the clock,
        # because an album is a story and a ranking is not.
        if config.ORDER_PICKS_BY_TIME and times:
            chosen = sorted(
                chosen,
                key=lambda a: (times.get(a.filepath) or _LATEST,
                               os.path.basename(a.filepath)),
            )

        for rank, a in enumerate(chosen, 1):
            name = f"{rank:03d}_{os.path.basename(a.filepath)}"
            plan.copies.append(
                Copy(
                    source=a.filepath,
                    destination=os.path.join(
                        first_option_dir, section.value,
                        _unsorted_bucket(a), name),
                    section=section,
                    rank=rank,
                    assignment=a,
                    is_pick=True,
                )
            )

        # Runners-up keep their group number in the filename so it is obvious
        # which pick each one is an alternative to.
        for rank, a in enumerate(rest, 1):
            name = f"g{a.shot_group:02d}_{rank:03d}_{os.path.basename(a.filepath)}"
            plan.copies.append(
                Copy(
                    source=a.filepath,
                    destination=os.path.join(second_option_dir, section.value,
                                             _unsorted_bucket(a), name),
                    section=section,
                    rank=rank,
                    assignment=a,
                    is_pick=False,
                )
            )

        # --- review bins ------------------------------------------------
        # Everything the tool rejected, filed by the reason it was rejected, so
        # the classification can be checked by eye. Previews rather than full
        # copies: these exist to be looked at, not printed.
        bins = (
            ("duplicate", duplicates_dir, True),
            ("low_quality", low_quality_dir, True),
            ("no_face", no_face_dir, True),
        )
        for bucket, base, preview in bins:
            # Unreadable files are deliberately absent from every bin: there is
            # no image to copy or preview. They are reported instead, so a
            # damaged original is raised rather than silently skipped.
            batch_members = [a for a in members if a.bucket == bucket]
            if not batch_members:
                continue
            batch_members.sort(key=lambda a: -a.quality)
            for rank, a in enumerate(batch_members, 1):
                # Objects keep no section subfolder: they are already one page.
                target_dir = base if bucket == "no_face" else os.path.join(
                    base, section.value)
                name = f"{rank:03d}_{os.path.basename(a.filepath)}"
                plan.copies.append(
                    Copy(
                        source=a.filepath,
                        destination=os.path.join(target_dir, name),
                        section=section,
                        rank=rank,
                        assignment=a,
                        is_pick=False,
                        bucket=bucket,
                        preview_only=preview and config.PREVIEW_REVIEW_BINS,
                    )
                )

    return plan


# ============================================================================
# EXECUTION
# ============================================================================

class OutputExists(RuntimeError):
    """The album folder is already populated."""


def check_output(source_dir: str, clean: bool,
                 output_name: Optional[str] = None) -> Optional[str]:
    """
    Guard against silently mixing a new run into an old album.

    Returns a path to remove if cleaning was asked for, else raises when the
    folder already holds files. With versioned names this rarely fires, since
    each run writes a fresh folder.
    """
    path = album_dir(source_dir, output_name)
    if not os.path.isdir(path):
        return None

    existing = [
        e for e in os.listdir(path)
        if not e.startswith(".")
    ]
    if not existing:
        return None

    if clean:
        return path

    suggestion = next_version_name(source_dir, split_version(os.path.basename(path))[0])
    raise OutputExists(
        f"{path} already contains {len(existing)} entry/entries. "
        f"Use {suggestion} instead, or re-run with --clean to replace it."
    )


def execute(plan: Plan, clean_path: Optional[str] = None) -> Tuple[int, List[str]]:
    """
    Perform the copies. Returns how many succeeded and any error messages.

    Only ever removes the album folder, and only when explicitly asked: the
    source photographs are never touched.
    """
    errors: List[str] = []

    if clean_path:
        # Refuse to delete anything that is not one of our own album folders,
        # in case a caller ever passes something unexpected. Checked by prefix
        # because the names are versioned.
        base = os.path.basename(clean_path)
        prefix, _ = split_version(base)
        if prefix != config.OUTPUT_DIR_PREFIX and base != config.OUTPUT_DIR_NAME:
            raise RuntimeError(
                f"refusing to remove {clean_path}: "
                f"not a {config.OUTPUT_DIR_PREFIX} album folder"
            )
        shutil.rmtree(clean_path)

    # Create every directory the plan targets, including 2nd_Option subfolders.
    for c in plan.copies:
        os.makedirs(os.path.dirname(c.destination), exist_ok=True)

    done = 0
    for c in plan.copies:
        try:
            if c.preview_only:
                if _write_preview(c.source, c.destination):
                    done += 1
                else:
                    errors.append(f"{os.path.basename(c.source)}: preview failed")
            else:
                # copy2 preserves timestamps, which keeps the album sortable by
                # capture order as well as by rank.
                shutil.copy2(c.source, c.destination)
                done += 1
        except OSError as exc:
            errors.append(f"{os.path.basename(c.source)}: {exc}")

    return done, errors


def _write_preview(source: str, destination: str) -> bool:
    """
    Write a downscaled JPEG for a review bin.

    Keeping rejects at full resolution would cost more disk than the album
    itself, and nobody prints from the duplicates folder. The filename still
    names the original and the CSV records its path.
    """
    import cv2

    img = cv2.imread(source, cv2.IMREAD_REDUCED_COLOR_2)
    if img is None:
        img = cv2.imread(source, cv2.IMREAD_COLOR)
    if img is None:
        return False

    h, w = img.shape[:2]
    longest = max(h, w)
    if longest > config.REVIEW_PREVIEW_MAX_DIM:
        scale = config.REVIEW_PREVIEW_MAX_DIM / longest
        img = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)

    return bool(cv2.imwrite(destination, img,
                            [int(cv2.IMWRITE_JPEG_QUALITY), 88]))


# ============================================================================
# REPORT
# ============================================================================

def write_report(
    assignments: Sequence[Assignment],
    plan: Plan,
    source_dir: str,
) -> str:
    """
    Write a CSV covering every photo, including ones not copied.

    The reason column is the point of this file: it makes each decision
    auditable instead of asking anyone to trust a folder of results.
    """
    os.makedirs(plan.album_dir, exist_ok=True)
    path = os.path.join(plan.album_dir, REPORT_NAME)

    destinations = {c.source: c for c in plan.copies}
    skipped_dupes = {a.filepath for a in plan.skipped_duplicates}
    skipped_top = {a.filepath for a in plan.skipped_beyond_top}

    order = {s: i for i, s in enumerate(config.SECTION_ORDER)}

    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow([
            "section", "picked", "pick_rank", "pick_reason",
            "shot_group", "shot_group_size",
            "filename", "quality", "reason",
            "people", "faces_detected", "people_counted",
            "is_duplicate", "duplicate_of", "copied", "skip_reason",
            "source_path", "album_path",
        ])

        for a in sorted(assignments, key=lambda a: (order[a.section], -a.quality)):
            copy = destinations.get(a.filepath)

            if copy:
                copied, skip_reason = "yes", ""
            elif a.filepath in skipped_dupes:
                copied, skip_reason = "no", "near-duplicate"
            elif a.filepath in skipped_top:
                copied, skip_reason = "no", "beyond --top limit"
            else:
                copied, skip_reason = "no", ""

            writer.writerow([
                a.section_name,
                "yes" if a.is_pick else "no",
                a.pick_rank or "",
                a.pick_reason,
                a.shot_group or "",
                a.shot_group_size or "",
                os.path.basename(a.filepath),
                f"{a.quality:.4f}",
                a.reason,
                "; ".join(a.person_names),
                a.face_count,
                a.people_count,
                "yes" if a.is_duplicate else "no",
                os.path.basename(a.duplicate_of) if a.duplicate_of else "",
                copied,
                skip_reason,
                a.filepath,
                os.path.relpath(copy.destination, source_dir) if copy else "",
            ])

    return path


# ============================================================================
# SUMMARY
# ============================================================================

def describe_plan(plan: Plan) -> str:
    picks_by_section: Dict[Section, int] = {}
    rest_by_section: Dict[Section, int] = {}
    for c in plan.copies:
        target = picks_by_section if c.is_pick else rest_by_section
        target[c.section] = target.get(c.section, 0) + 1

    lines = [f"    {'section':<14} {'picks':>6} {'2nd_Option':>11}"]
    for section in config.SECTION_ORDER:
        p = picks_by_section.get(section, 0)
        r = rest_by_section.get(section, 0)
        if p or r:
            lines.append(f"    {section.value:<14} {p:>6} {r:>11}")

    lines.append(f"    {'-' * 14} {'-' * 6} {'-' * 11}")
    lines.append(
        f"    {'total':<14} {len(plan.picks):>6} {len(plan.runners_up):>11}"
    )

    if plan.skipped_duplicates:
        lines.append(f"    {len(plan.skipped_duplicates)} near-duplicate(s) left out")

    gb = plan.total_bytes / 1e9
    lines.append(f"\n    {len(plan.copies)} file(s), about {gb:.2f} GB")
    lines.append(f"    into {plan.album_dir}")
    return "\n".join(lines)
