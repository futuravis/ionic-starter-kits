"""
Running a whole wedding, one shoot at a time.

A wedding is not one collection. The photographer already split the day into
folders - "pillayar pooja", "thali tie", "late night shots" - and each is a
separate occasion. Selecting across the lot produces a single album full of the
day's strongest frames and nothing at all from the quieter ceremonies.

So the work splits in two, along the seam that matters:

    identity is global     Rekha is Rekha in every ceremony, so faces are
                           clustered once across the entire collection and you
                           label each person a single time.

    selection is local     sections, shot groups and picks are computed inside
                           each shoot, and its album is written in place, next
                           to the photos it came from.

Getting that seam wrong in either direction is painful: label per folder and you
answer "who is this?" thirty times; select globally and the small ceremonies
vanish.

The existing folder structure is never modified. Each shoot gains a
BestImages_vN folder beside its photos; nothing is moved, renamed or deleted.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from datetime import datetime

from . import (
    config, organizer, people, pipeline, quality, scanner, sections, selection,
    timeline,
)
from .faces import FaceResult
from .people import Person


ProgressFn = Optional[Callable[[str, int, int], None]]


# ============================================================================
# RESULTS
# ============================================================================

@dataclass
class ShootResult:
    """What happened for one shoot folder."""

    folder: str
    photo_count: int = 0
    assignments: List[sections.Assignment] = field(default_factory=list)
    groups: List[selection.ShotGroup] = field(default_factory=list)
    picks: Dict[config.Section, List[sections.Assignment]] = field(default_factory=dict)
    album_dir: str = ""
    copied: int = 0
    errors: List[str] = field(default_factory=list)
    report: str = ""

    @property
    def name(self) -> str:
        return os.path.basename(self.folder.rstrip(os.sep)) or self.folder

    @property
    def pick_count(self) -> int:
        return sum(len(v) for v in self.picks.values())

    # ------------------------------------------------------------------
    # Every bucket count is derived from Assignment.bucket and never tracked
    # alongside it. Keeping a parallel tally invited exactly one bug: a blurred
    # object shot was counted once as an object and again as low quality, so the
    # reported totals disagreed with the folders actually written.
    # ------------------------------------------------------------------

    @property
    def bucket_counts(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for a in self.assignments:
            counts[a.bucket] = counts.get(a.bucket, 0) + 1
        return counts

    @property
    def duplicates_dropped(self) -> int:
        return self.bucket_counts.get("duplicate", 0)

    @property
    def low_quality(self) -> int:
        return self.bucket_counts.get("low_quality", 0)

    @property
    def objects(self) -> int:
        return self.bucket_counts.get("no_face", 0)

    @property
    def alternates(self) -> int:
        return self.bucket_counts.get("alternate", 0)

    @property
    def unused(self) -> int:
        return self.bucket_counts.get("unused", 0)

    @property
    def unreadable(self) -> int:
        return self.bucket_counts.get("unreadable", 0)

    @property
    def unreadable_files(self) -> List[str]:
        return [os.path.basename(a.filepath)
                for a in self.assignments if a.is_unreadable]

    @property
    def kept_count(self) -> int:
        """Photos that were still in contention when picking happened."""
        counts = self.bucket_counts
        return (counts.get("pick", 0) + counts.get("alternate", 0)
                + counts.get("unused", 0))

    @property
    def section_counts(self) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for a in self.assignments:
            out[a.section.value] = out.get(a.section.value, 0) + 1
        return out


@dataclass
class BatchResult:
    root: str
    shoots: List[ShootResult] = field(default_factory=list)
    people: List[Person] = field(default_factory=list)

    @property
    def total_photos(self) -> int:
        return sum(s.photo_count for s in self.shoots)

    @property
    def total_duplicates(self) -> int:
        return sum(s.duplicates_dropped for s in self.shoots)

    @property
    def total_low_quality(self) -> int:
        return sum(s.low_quality for s in self.shoots)

    @property
    def total_objects(self) -> int:
        return sum(s.objects for s in self.shoots)

    @property
    def total_picks(self) -> int:
        return sum(s.pick_count for s in self.shoots)

    @property
    def total_copied(self) -> int:
        return sum(s.copied for s in self.shoots)

    @property
    def unlabeled(self) -> List[Person]:
        return [p for p in self.people if not p.is_labeled]


# ============================================================================
# STAGE 1: GLOBAL ANALYSIS
# ============================================================================

def analyze_collection(
    root: str,
    workers: int = 1,
    overrides: Optional[dict] = None,
    progress: ProgressFn = None,
    limit_per_shoot: int = 0,
) -> Tuple[List[Tuple[str, List[str]]], List[FaceResult], List[Person],
           Dict[str, float], Dict[str, str]]:
    """
    Detect faces, cluster people and score quality across the whole collection.

    One pass over everything, because face identity has to be consistent between
    ceremonies and quality scores are only meaningful relative to a common pool.
    Shot grouping and picking deliberately happen later, per shoot.
    """
    shoots = scanner.find_shoots(root)
    if limit_per_shoot:
        shoots = [(folder, photos[:limit_per_shoot]) for folder, photos in shoots]

    all_photos = [p for _, photos in shoots for p in photos]
    if not all_photos:
        return shoots, [], [], {}, {}

    def report(stage: str):
        return (lambda done, total: progress(stage, done, total)) if progress else None

    results, _ = pipeline.analyze_faces(
        root, all_photos, workers=workers,
        progress=report("detecting faces"), overrides=overrides,
    )

    found = people.cluster(results)
    people.carry_labels(found, root)
    people.save(found, root)

    metrics = pipeline.analyze_quality(
        all_photos, workers=workers,
        progress=report("scoring quality"), overrides=overrides,
    )
    scores = quality.score_collection(metrics)
    phashes = {m.filepath: m.phash for m in metrics if m.ok}

    if progress:
        progress("reading capture times", 0, len(all_photos))
    times = timeline.read_times(root, all_photos)
    if progress:
        progress("reading capture times", len(all_photos), len(all_photos))

    return shoots, results, found, scores, phashes, times


# ============================================================================
# STAGE 2: PER-SHOOT SELECTION
# ============================================================================

def prepare_shoots(
    shoots: Sequence[Tuple[str, List[str]]],
    results: Sequence[FaceResult],
    found: Sequence[Person],
    scores: Dict[str, float],
    phashes: Dict[str, str],
    percent: float = config.DEFAULT_PICKS_PERCENT,
    per_section: int = 0,
    drop_duplicates: bool = config.DROP_DUPLICATES,
    times: Optional[Dict[str, "datetime"]] = None,
) -> List[ShootResult]:
    """
    Work out sections, duplicates, shot groups and picks for each shoot.

    Duplicates go first, as the user asked: a burst of the same frame is noise
    before it is anything else, and clearing it shrinks every later decision.
    """
    times = times or {}
    by_path = {r.filepath: r for r in results}
    out: List[ShootResult] = []

    for folder, photos in shoots:
        shoot = ShootResult(folder=folder, photo_count=len(photos))

        shoot_results = [by_path[p] for p in photos if p in by_path]
        if not shoot_results:
            out.append(shoot)
            continue

        assigned = sections.assign_all(shoot_results, found, scores)

        # --- 0. files that are not images at all ------------------------
        # A photo missing from `scores` could not be decoded. Flagging these
        # separately keeps them out of the quality discussion - there is no
        # photograph to judge - and surfaces a damaged original instead of
        # letting it fail quietly as "preview failed" much later on.
        for a in assigned:
            if a.filepath not in scores:
                a.is_unreadable = True

        # --- 1. duplicates ----------------------------------------------
        # Cleared first, as the noisiest problem. They stay in the assignment
        # list so they can be filed into the Duplicates bin for review; they are
        # simply barred from winning a pick.
        sections.mark_duplicates(assigned, phashes)

        # --- 2. the weakest slice ---------------------------------------
        sections.mark_low_quality(assigned)

        # --- 3. sections, poses, picks ----------------------------------
        shoot.assignments = assigned

        # Only photos still in contention are grouped and picked from.
        eligible = [
            a for a in assigned
            if not a.is_unreadable
            and not a.is_duplicate
            and not a.is_low_quality
            and a.section is not config.Section.NO_FACE
        ]
        shoot.groups = selection.group_shots(eligible, phashes, times=times)
        shoot.picks = selection.choose(
            shoot.groups,
            per_section=per_section,
            percent=percent,
            skip_duplicates=False,   # duplicates are already excluded above
        )
        out.append(shoot)

    return out


# ============================================================================
# STAGE 3: WRITE
# ============================================================================

def write_shoots(
    shoot_results: Sequence[ShootResult],
    clean: bool = False,
    name: Optional[str] = None,
    progress: ProgressFn = None,
    times: Optional[Dict[str, datetime]] = None,
) -> None:
    """
    Write each shoot's album inside its own folder.

    A failure in one shoot does not stop the others; it is recorded and the run
    continues, because a single unreadable file should not cost you the other
    twenty-eight ceremonies.
    """
    total = len(shoot_results)

    for index, shoot in enumerate(shoot_results, 1):
        if progress:
            progress(f"writing {shoot.name}", index, total)

        if not shoot.assignments:
            continue

        output_name = organizer.target_version_name(
            shoot.folder, clean=clean, explicit=name)

        try:
            clean_path = organizer.check_output(
                shoot.folder, clean=clean, output_name=output_name)
        except organizer.OutputExists as exc:
            shoot.errors.append(str(exc))
            continue

        plan = organizer.build_plan(
            shoot.assignments, shoot.folder,
            picks=shoot.picks, output_name=output_name, times=times,
        )
        shoot.album_dir = plan.album_dir

        copied, errors = organizer.execute(plan, clean_path=clean_path)
        shoot.copied = copied
        shoot.errors.extend(errors)
        shoot.report = organizer.write_report(shoot.assignments, plan, shoot.folder)


# ============================================================================
# REPORTING
# ============================================================================

def summarize(result: BatchResult) -> str:
    """Per-shoot table plus totals."""
    lines = [
        f"    {'shoot':<30} {'photos':>6} {'dupes':>6} {'lowQ':>5} {'objs':>5} "
        f"{'poses':>6} {'picks':>6}"
    ]

    for shoot in result.shoots:
        rel = os.path.relpath(shoot.folder, result.root)
        if len(rel) > 29:
            rel = "..." + rel[-26:]
        lines.append(
            f"    {rel:<30} {shoot.photo_count:>6} {shoot.duplicates_dropped:>6} "
            f"{shoot.low_quality:>5} {shoot.objects:>5} "
            f"{len(shoot.groups):>6} {shoot.pick_count:>6}"
        )

    lines.append(f"    {'-' * 30} {'-' * 6} {'-' * 6} {'-' * 5} {'-' * 5} "
                 f"{'-' * 6} {'-' * 6}")
    lines.append(
        f"    {'total':<30} {result.total_photos:>6} {result.total_duplicates:>6} "
        f"{result.total_low_quality:>5} {result.total_objects:>5} "
        f"{sum(len(s.groups) for s in result.shoots):>6} {result.total_picks:>6}"
    )

    if result.total_photos:
        kept = 100.0 * result.total_picks / result.total_photos
        lines.append(
            f"\n    {result.total_picks} picked ({kept:.1f}%), "
            f"{result.total_duplicates} duplicate(s), "
            f"{result.total_low_quality} low quality, "
            f"{result.total_objects} object shot(s), "
            f"out of {result.total_photos}"
        )

    broken = [(s, s.unreadable_files) for s in result.shoots if s.unreadable]
    if broken:
        total = sum(len(f) for _, f in broken)
        lines.append(f"\n    {total} file(s) could not be decoded at all "
                     f"(damaged originals, not low quality):")
        for shoot, files in broken:
            lines.append(f"        {os.path.relpath(shoot.folder, result.root)}: "
                         f"{', '.join(files[:4])}")
    return "\n".join(lines)
