"""
Ranking a batch of photographs, and choosing a shortlist from it.

Two different questions get two different answers, and conflating them is the
mistake this module exists to avoid.

**The ranking** puts every photo in order, best to worst, 1 to N. It is a
straight sort by score and it includes everything - duplicates, rejects, the
blurred frames. This is the answer to "show me all 50 in order".

**The shortlist** is a handful of photos chosen to work together. It is not the
top N of the ranking, because a batch of wedding photos contains bursts: six
frames of the same pose, seconds apart. The top five scores would be five
frames of whichever pose was lit best, which is one photograph five times.

So look-alike frames are bundled first, and picks are taken round-robin: the
best of the strongest bundle, then the best of the next, and only once every
bundle has contributed does it come back for second-best. That yields five
different photographs.

Both answers come from one pass, so nothing is measured twice.

Quality measurement lives in `metrics.py`, which reuses `wedding_album.quality`
and corrects its noise-blind sharpness. Fault naming lives in `diagnose.py`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence

import numpy as np

from wedding_album import config, quality
from wedding_album.quality import phash_distance

from . import diagnose, metrics as measurement
from .diagnose import Issue, Thresholds
from .metrics import FaceReading, PhotoMetrics


ProgressFn = Optional[Callable[[str, int, int], None]]

DISPLAY_METRICS = ("sharpness", "exposure", "contrast", "saturation", "noise")

# Scoring weights for this application, applied only while it scores and then
# restored. `wedding_album`'s own defaults are left exactly as they are.
#
# Noise carries 0.25 here against 0.12 in the shared config, and that number
# only became usable once the sharpness metric was fixed. While sharpness was
# the plain Laplacian variance, grain inflated it so far that no weighting
# worked: the setting at which grain stopped winning was the setting at which
# blurred frames started winning instead. With `metrics.sharpness_multiscale`
# feeding it, the two faults separate and the ordering comes out as
# well-made, then grainy, then soft, then dark, then blown out.
#
# Both changes were needed. Neither is sufficient alone.
QUALITY_WEIGHTS = {
    "sharpness": 0.30,
    "exposure": 0.18,
    "contrast": 0.15,
    "saturation": 0.12,
    "noise": 0.25,
}

STRENGTH_TEXT = {
    "sharpness": "crisp detail",
    "exposure": "well exposed",
    "contrast": "strong contrast",
    "saturation": "rich colour",
    "noise": "clean, low noise",
}

WEAKNESS_TEXT = {
    "sharpness": "soft focus",
    "exposure": "exposure problems",
    "contrast": "flat contrast",
    "saturation": "muted colour",
    "noise": "visible noise",
}


# ============================================================================
# RESULT TYPES
# ============================================================================

@dataclass
class Candidate:
    """One photograph, measured, placed, ranked and diagnosed."""

    filepath: str
    score: float = 0.0

    # Position in the full ranking, 1 = best. Every photo has one.
    rank: int = 0

    # Look-alike bundle this photo belongs to.
    group: int = 0
    group_size: int = 1

    # Shortlist membership. Only a few photos have these.
    is_pick: bool = False
    pick_rank: int = 0
    reason: str = ""

    is_duplicate: bool = False
    duplicate_of: str = ""

    breakdown: Dict[str, float] = field(default_factory=dict)
    issues: List[Issue] = field(default_factory=list)
    headline: str = ""

    face_count: int = -1            # -1 means no face probe was run
    face_ratio: float = 0.0         # largest face as a fraction of the frame

    width: int = 0
    height: int = 0

    @property
    def name(self) -> str:
        return os.path.basename(self.filepath)

    @property
    def percent(self) -> float:
        return round(self.score * 100, 1)

    @property
    def major_issues(self) -> List[Issue]:
        return [i for i in self.issues if i.is_major]

    @property
    def is_usable(self) -> bool:
        """No major fault. A minor one is a compromise, not a reject."""
        return not self.major_issues


@dataclass
class Group:
    """Photographs in the batch that show much the same thing."""

    index: int = 0
    members: List[Candidate] = field(default_factory=list)

    @property
    def best(self) -> Candidate:
        return self.members[0]          # kept sorted, best first

    @property
    def size(self) -> int:
        return len(self.members)


@dataclass
class Result:
    """Everything one run produced."""

    ranked: List[Candidate] = field(default_factory=list)   # all, best to worst
    picks: List[Candidate] = field(default_factory=list)    # the shortlist
    groups: List[Group] = field(default_factory=list)
    failures: List[PhotoMetrics] = field(default_factory=list)
    issue_tally: Dict[str, int] = field(default_factory=dict)
    faces_probed: bool = False

    @property
    def total(self) -> int:
        return len(self.ranked)

    @property
    def best(self) -> Optional[Candidate]:
        """The single best photograph, or None on an empty batch."""
        return self.ranked[0] if self.ranked else None

    @property
    def usable_count(self) -> int:
        return sum(1 for c in self.ranked if c.is_usable)

    def summary(self) -> str:
        parts = [
            f"{self.total} photo(s) ranked",
            f"{len(self.groups)} distinct shot(s)",
            f"{len(self.picks)} shortlisted",
        ]
        flawed = self.total - self.usable_count
        if flawed:
            parts.append(f"{flawed} with a major fault")
        duplicates = sum(1 for c in self.ranked if c.is_duplicate)
        if duplicates:
            parts.append(f"{duplicates} near-duplicate(s)")
        if self.failures:
            parts.append(f"{len(self.failures)} unreadable")
        return ", ".join(parts)


# ============================================================================
# DISPLAY BREAKDOWN
# ============================================================================

def _scale_for_display(values: Sequence[float], inverted: bool) -> List[float]:
    """
    Rescale one metric to 0-1 across the batch, for the explanation only.

    This does not feed the ranking. `quality.score_collection` owns the score;
    this exists so the UI can show *why*, and it uses the same percentile
    clipping so the two never tell different stories.
    """
    arr = np.asarray(values, dtype=np.float64)
    lo_pct, hi_pct = config.NORMALIZE_PERCENTILES
    lo = float(np.percentile(arr, lo_pct))
    hi = float(np.percentile(arr, hi_pct))

    if hi - lo < 1e-9:
        scaled = np.full_like(arr, 0.5)
    else:
        scaled = np.clip((arr - lo) / (hi - lo), 0.0, 1.0)

    if inverted:
        scaled = 1.0 - scaled

    return [float(v) for v in scaled]


def score(measured: Sequence[PhotoMetrics]) -> Dict[str, float]:
    """
    Score the batch with this application's weights, then put them back.

    The weights live in module-level config in `wedding_album`, so they have to
    be swapped in around the call and restored afterwards. Restoring in a
    `finally` matters: leaving them applied would silently change how the album
    tool scores for the rest of the process.
    """
    previous = config.apply_overrides({"QUALITY_WEIGHTS": QUALITY_WEIGHTS})
    try:
        return quality.score_collection(measured)
    finally:
        config.apply_overrides(previous)


def _breakdowns(usable: Sequence[PhotoMetrics]) -> List[Dict[str, float]]:
    """Per-photo, per-metric 0-1 values, in the same order as `usable`."""
    columns: Dict[str, List[float]] = {}
    for name in DISPLAY_METRICS:
        raw = [getattr(m, name) for m in usable]
        if name == "exposure":
            # Already an absolute 0-1 judgement; rescaling it against the batch
            # would turn "every frame was dark" into "some were fine".
            columns[name] = [float(v) for v in raw]
        else:
            columns[name] = _scale_for_display(
                raw, inverted=name in config.INVERTED_METRICS)

    return [
        {name: round(columns[name][i], 4) for name in DISPLAY_METRICS}
        for i in range(len(usable))
    ]


def describe(candidate: Candidate, group: Group) -> str:
    """
    One plain sentence for why this photo made the shortlist.

    Leads with how it beat its own bundle, because that is the decision being
    explained, then names its strongest measured quality. A major fault is
    stated rather than papered over - the best of a weak batch is still the
    best available, not necessarily good.
    """
    context = (f"best of {group.size} similar frames" if group.size > 1
               else "the only frame of this shot")

    major = candidate.major_issues
    if major:
        return f"{context}; strongest available despite {major[0].label.lower()}"

    ranked = sorted(candidate.breakdown.items(), key=lambda kv: -kv[1])
    strengths = [STRENGTH_TEXT[n] for n, v in ranked[:2] if v >= 0.55]
    if strengths:
        return f"{context}; {' and '.join(strengths)}"

    worst = ranked[-1][0] if ranked else ""
    if worst:
        return f"{context}; strongest available despite {WEAKNESS_TEXT[worst]}"
    return context


# ============================================================================
# GROUPING
# ============================================================================

def build_groups(
    candidates: Sequence[Candidate],
    phashes: Dict[str, str],
    threshold: int = config.SHOT_GROUP_THRESHOLD,
) -> List[Group]:
    """
    Collapse look-alike frames into bundles, strongest photo first.

    Each bundle forms around its best remaining photo, so a bundle's
    representative is always the frame that would be picked from it.
    """
    ordered = sorted(candidates, key=lambda c: -c.score)
    groups: List[Group] = []

    for candidate in ordered:
        own_hash = phashes.get(candidate.filepath, "")
        placed = False

        if own_hash:
            for group in groups:
                leader_hash = phashes.get(group.best.filepath, "")
                if not leader_hash:
                    continue
                distance = phash_distance(own_hash, leader_hash)
                if distance > threshold:
                    continue
                # Tighter than the bundling threshold: close enough to be the
                # same shot is not the same as being the same photograph.
                if distance <= config.DUPLICATE_HASH_THRESHOLD:
                    candidate.is_duplicate = True
                    candidate.duplicate_of = group.best.filepath
                group.members.append(candidate)
                placed = True
                break

        if not placed:
            groups.append(Group(members=[candidate]))

    groups.sort(key=lambda g: -g.best.score)
    for i, group in enumerate(groups, 1):
        group.index = i
        group.members.sort(key=lambda c: -c.score)
        for member in group.members:
            member.group = i
            member.group_size = group.size

    return groups


# ============================================================================
# PICKING
# ============================================================================

def pick(
    groups: Sequence[Group],
    want: int,
    skip_duplicates: bool = True,
    avoid_major_faults: bool = True,
) -> List[Candidate]:
    """
    Choose `want` photos, spread round-robin across the bundles.

    Photos with a major fault are held back while clean ones remain, rather
    than excluded outright: a shortlist of five has to return five, and on a
    difficult batch the least-bad frame is the honest answer. Its `reason` then
    names the fault.
    """
    for group in groups:
        for member in group.members:
            member.is_pick = False
            member.pick_rank = 0
            member.reason = ""

    def tiers(group: Group) -> List[Candidate]:
        members = [m for m in group.members
                   if not (skip_duplicates and m.is_duplicate)]
        # Never let a bundle vanish: if every frame in it duplicates its own
        # leader, the leader is still a distinct shot worth offering.
        members = members or group.members[:1]
        if not avoid_major_faults:
            return members
        clean = [m for m in members if m.is_usable]
        flawed = [m for m in members if not m.is_usable]
        return clean + flawed

    eligible = [tiers(g) for g in groups]
    limit = max(0, min(want, sum(len(e) for e in eligible)))

    chosen: List[Candidate] = []
    depth = 0
    while len(chosen) < limit:
        took_any = False
        for position, members in enumerate(eligible):
            if len(chosen) >= limit:
                break
            if depth >= len(members):
                continue
            candidate = members[depth]
            candidate.is_pick = True
            candidate.pick_rank = len(chosen) + 1
            candidate.reason = describe(candidate, groups[position])
            chosen.append(candidate)
            took_any = True
        if not took_any:
            break                        # every bundle exhausted
        depth += 1

    return chosen


# ============================================================================
# ENTRY POINT
# ============================================================================

def run(
    paths: Sequence[str],
    want: int = 5,
    workers: int = 1,
    progress: ProgressFn = None,
    skip_duplicates: bool = True,
    use_faces: bool = True,
) -> Result:
    """
    Measure, rank and shortlist a batch.

    Unreadable files are reported on the result rather than scored, so a corrupt
    upload can neither win nor quietly vanish.

    `use_faces` adds face detection: whether anyone is in the frame, whether
    their face is lit, and whether it is in focus. It costs a second decode per
    photo and is skipped silently when the model is absent.
    """
    if not paths:
        return Result()

    measured = measurement.measure_all(paths, workers=workers, progress=progress)

    usable = [m for m in measured if m.ok]
    failures = [m for m in measured if not m.ok]
    if not usable:
        return Result(failures=failures)

    readings: Dict[str, FaceReading] = {}
    if use_faces and measurement.faces_available():
        for reading in measurement.read_faces_all(
                [m.filepath for m in usable], workers=workers, progress=progress):
            readings[reading.filepath] = reading

    if progress:
        progress("comparing shots", 0, len(usable))

    scores = score(measured)
    breakdowns = _breakdowns(usable)
    thresholds = Thresholds.from_batch(usable)

    candidates: List[Candidate] = []
    for i, m in enumerate(usable):
        reading = readings.get(m.filepath)
        issues = diagnose.diagnose(m, reading, thresholds)
        face_count = reading.count if reading and reading.ok else -1

        candidates.append(Candidate(
            filepath=m.filepath,
            score=scores.get(m.filepath, 0.0),
            breakdown=breakdowns[i],
            issues=issues,
            headline=diagnose.headline(issues, face_count),
            face_count=face_count,
            face_ratio=(reading.main.size_ratio
                        if reading and reading.ok and reading.main else 0.0),
            width=m.width,
            height=m.height,
        ))

    groups = build_groups(candidates, {m.filepath: m.phash for m in usable})
    picks = pick(groups, want, skip_duplicates=skip_duplicates)

    # The full ordering, best to worst. Ties break on filename so that two runs
    # over the same batch never disagree.
    ranked = sorted(candidates, key=lambda c: (-c.score, c.name))
    for position, candidate in enumerate(ranked, 1):
        candidate.rank = position

    if progress:
        progress("done", len(usable), len(usable))

    return Result(
        ranked=ranked,
        picks=picks,
        groups=groups,
        failures=failures,
        issue_tally=diagnose.tally([c.issues for c in ranked]),
        faces_probed=bool(readings),
    )
