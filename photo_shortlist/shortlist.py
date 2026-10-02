"""
Picking the best few photos out of a small batch.

Two stages, because "best" and "a good set" are different questions.

1. Grouping. Near-identical frames collapse into one group, so a burst of the
   same pose competes with itself rather than with the rest of the batch.

2. Picking, round-robin across those groups. The first pick is the best photo
   of the strongest group, the second is the best of the next group, and only
   once every group has contributed does it come back for second-best.

The round-robin is the whole point. Taking the top 5 scores outright gives five
frames of whichever pose was lit best, which is not a shortlist - it is one
photo, five times. Spreading across groups gives five different photographs.

This mirrors `wedding_album.selection`, minus the album machinery: no sections,
no capture-time window, no alternates. That module groups within a section and
needs `Assignment` objects to do it; here there is one flat batch of uploads,
so the same idea fits in a fraction of the code.

Quality scoring is NOT reimplemented. `wedding_album.quality` is the single
source of truth for what a photo scores.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence

import numpy as np

from wedding_album import config, pipeline, quality
from wedding_album.quality import QualityMetrics, phash_distance


ProgressFn = Optional[Callable[[str, int, int], None]]

# Metrics shown in the per-photo breakdown, in the order the UI lists them.
DISPLAY_METRICS = ("sharpness", "exposure", "contrast", "saturation", "noise")

# Plain-English name for each metric's strong side, used to explain a pick.
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
    """One uploaded photo, measured and placed."""

    filepath: str
    score: float = 0.0                 # 0-1, from quality.score_collection
    group: int = 0                      # 1-based group number
    group_size: int = 1
    is_pick: bool = False
    rank: int = 0                       # 1-based, only set on picks
    reason: str = ""
    is_duplicate: bool = False
    duplicate_of: str = ""
    breakdown: Dict[str, float] = field(default_factory=dict)
    width: int = 0
    height: int = 0

    @property
    def name(self) -> str:
        return os.path.basename(self.filepath)

    @property
    def percent(self) -> float:
        return round(self.score * 100, 1)


@dataclass
class Group:
    """Photos in the batch that show much the same thing."""

    index: int = 0
    members: List[Candidate] = field(default_factory=list)

    @property
    def best(self) -> Candidate:
        return self.members[0]          # members are kept sorted, best first

    @property
    def size(self) -> int:
        return len(self.members)


@dataclass
class Result:
    """Everything one shortlist run produced."""

    picks: List[Candidate] = field(default_factory=list)
    runners_up: List[Candidate] = field(default_factory=list)
    groups: List[Group] = field(default_factory=list)
    failures: List[QualityMetrics] = field(default_factory=list)

    @property
    def total(self) -> int:
        return sum(g.size for g in self.groups)

    def summary(self) -> str:
        parts = [
            f"{self.total} photo(s) analyzed",
            f"{len(self.groups)} distinct shot(s)",
            f"{len(self.picks)} picked",
        ]
        dupes = sum(1 for g in self.groups for m in g.members if m.is_duplicate)
        if dupes:
            parts.append(f"{dupes} near-duplicate(s)")
        if self.failures:
            parts.append(f"{len(self.failures)} unreadable")
        return ", ".join(parts)


# ============================================================================
# DISPLAY BREAKDOWN
# ============================================================================

def _scale_for_display(values: Sequence[float], inverted: bool) -> List[float]:
    """
    Rescale one metric to 0-1 across the batch, for the explanation text only.

    This deliberately does not feed the ranking. `quality.score_collection`
    owns the score; this exists so the UI can say *why* a photo won, and it
    uses the same percentile clipping so the two never tell different stories.
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


def _breakdowns(usable: Sequence[QualityMetrics]) -> List[Dict[str, float]]:
    """Per-photo, per-metric 0-1 values in the same order as `usable`."""
    columns: Dict[str, List[float]] = {}
    for name in DISPLAY_METRICS:
        raw = [getattr(m, name) for m in usable]
        if name == "exposure":
            # Already an absolute 0-1 judgement; rescaling it against the batch
            # would turn "every shot was dark" into "some were fine".
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
    One plain-English sentence for why this photo was picked.

    Leads with how it beat its own group, because that is the decision actually
    being explained, then names its strongest measured quality.
    """
    if group.size > 1:
        context = f"best of {group.size} similar frames"
    else:
        context = "the only frame of this shot"

    ranked = sorted(candidate.breakdown.items(), key=lambda kv: -kv[1])
    strengths = [STRENGTH_TEXT[n] for n, v in ranked[:2] if v >= 0.55]

    if strengths:
        return f"{context}; {' and '.join(strengths)}"

    # Nothing scored well. Say so rather than inventing praise - a shortlist
    # from a weak batch is still the best available, not necessarily good.
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
    Collapse near-identical frames into groups, strongest photo first.

    Each group forms around its best remaining photo, so the representative of
    a group is always the frame that would be picked from it.
    """
    ordered = sorted(candidates, key=lambda c: -c.score)
    groups: List[Group] = []

    for candidate in ordered:
        chash = phashes.get(candidate.filepath, "")
        placed = False

        if chash:
            for group in groups:
                leader_hash = phashes.get(group.best.filepath, "")
                if not leader_hash:
                    continue
                distance = phash_distance(chash, leader_hash)
                if distance > threshold:
                    continue
                # Tighter than the grouping threshold: alike enough to be the
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

def pick(groups: Sequence[Group], want: int,
         skip_duplicates: bool = True) -> List[Candidate]:
    """
    Choose `want` photos, spread round-robin across the groups.

    Returns fewer than `want` only when the batch genuinely holds fewer usable
    photos. Sets `is_pick`, `rank` and `reason` on the chosen candidates.
    """
    eligible: List[List[Candidate]] = []
    for group in groups:
        members = [m for m in group.members
                   if not (skip_duplicates and m.is_duplicate)]
        # Never let a group vanish: if every frame in it is a near-duplicate of
        # its own leader, the leader is still a distinct shot worth offering.
        eligible.append(members or group.members[:1])

    for group in groups:
        for member in group.members:
            member.is_pick = False
            member.rank = 0
            member.reason = ""

    available = sum(len(e) for e in eligible)
    limit = max(0, min(want, available))

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
            candidate.rank = len(chosen) + 1
            candidate.reason = describe(candidate, groups[position])
            chosen.append(candidate)
            took_any = True
        if not took_any:
            break                        # every group exhausted
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
) -> Result:
    """
    Measure a batch of photos and shortlist the best `want` of them.

    Unreadable files are reported on the result rather than treated as
    zero-quality photos, so a corrupt upload cannot quietly win or lose.
    """
    if not paths:
        return Result()

    def step(done: int, total: int) -> None:
        if progress:
            progress("scoring photos", done, total)

    metrics = pipeline.analyze_quality(list(paths), workers=workers,
                                       progress=step)

    usable = [m for m in metrics if m.ok]
    failures = [m for m in metrics if not m.ok]
    if not usable:
        return Result(failures=failures)

    if progress:
        progress("comparing shots", 0, len(usable))

    scores = quality.score_collection(metrics)
    breakdowns = _breakdowns(usable)
    phashes = {m.filepath: m.phash for m in usable}

    candidates = [
        Candidate(
            filepath=m.filepath,
            score=scores.get(m.filepath, 0.0),
            breakdown=breakdowns[i],
            width=m.width,
            height=m.height,
        )
        for i, m in enumerate(usable)
    ]

    groups = build_groups(candidates, phashes)
    picks = pick(groups, want, skip_duplicates=skip_duplicates)

    runners_up = sorted(
        (c for g in groups for c in g.members if not c.is_pick),
        key=lambda c: -c.score,
    )

    if progress:
        progress("done", len(usable), len(usable))

    return Result(picks=picks, runners_up=runners_up,
                  groups=groups, failures=failures)
