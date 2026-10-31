"""
Naming what is wrong with a photograph.

The score says a photo placed 31st. That is not useful on its own: the question
anyone actually asks is *why*. This module turns the measurements into a short
list of named faults, in words a photographer would use.

Two kinds of threshold are used, deliberately mixed.

**Absolute**, for faults that are faults regardless of company. A frame whose
highlights are blown has lost that detail permanently, and it does not become
acceptable because every other frame in the batch is worse.

**Batch-relative**, for faults that only mean anything by comparison. Laplacian
variance has no universal "sharp" value - it depends on the subject, the lens
and the resolution - so focus is judged against the rest of the batch. This is
the same reasoning behind scoring being batch-relative in the first place.

What is deliberately NOT claimed here: closed eyes, and smiling or not. YuNet
returns five landmarks - two eye centres, a nose tip, two mouth corners - which
carry no eyelid or mouth-shape information. Scoring an expression from those
five points would be invention, so the faults below stop at face geometry,
lighting and focus.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

import numpy as np

from .metrics import FaceReading, PhotoMetrics


# ============================================================================
# ABSOLUTE THRESHOLDS
# ============================================================================

DARK_BRIGHTNESS = 62.0          # mean grey level below this reads as low light
BRIGHT_BRIGHTNESS = 202.0       # and above this as washed out
CRUSHED_MAJOR = 0.18            # fraction of pixels lost to pure black
CRUSHED_MINOR = 0.08
BLOWN_MAJOR = 0.10              # fraction lost to pure white
BLOWN_MINOR = 0.04
FLAT_CONTRAST = 26.0            # standard deviation of intensity

FACE_DARK_RATIO = 0.68          # face this much dimmer than frame = backlit
FACE_DARK_ABSOLUTE = 95.0
FACE_TURNED = 0.45              # frontality below this = turned away
FACE_TILTED = 26.0              # degrees of head tilt

# "Focus missed the face" needs two things to agree, because either alone is
# wrong.
#
# The frame ratio alone reports soft faces on sharp portraits: skin is smooth
# and a wedding frame is full of detail, so a face measures softer than its
# photograph whatever the focus did. On a real 40-photo shoot that fired on 8
# of them.
#
# The batch percentile alone is just as bad in the other direction - it always
# condemns the softest quarter of faces, even when every face in the batch is
# perfectly sharp.
#
# Requiring both means a face must be soft compared to other faces *and* soft
# compared to its own frame.
FACE_SOFT_RATIO = 0.35
FACE_SOFT_PERCENTILE = 25.0

# Batch percentiles for the comparative faults.
SOFT_PERCENTILE = 22.0
SLIGHTLY_SOFT_PERCENTILE = 42.0
GRAINY_PERCENTILE = 78.0

# How far above the batch median the noise must sit before grain is called out,
# so a batch of uniformly clean photos does not have most of it flagged.
GRAINY_MARGIN = 1.25


# ============================================================================
# ISSUE
# ============================================================================

@dataclass(frozen=True)
class Issue:
    """One named fault."""

    code: str                   # stable identifier, for filtering in the UI
    label: str                  # short human phrase
    severity: str               # "major" or "minor"
    detail: str = ""            # the measurement behind it

    @property
    def is_major(self) -> bool:
        return self.severity == "major"


# Ordered worst-first, so a photo's faults read in the order that matters.
SEVERITY_ORDER = {"major": 0, "minor": 1}


# ============================================================================
# BATCH CONTEXT
# ============================================================================

@dataclass
class Thresholds:
    """The comparative cut-offs, derived from this batch."""

    soft: float = 0.0
    slightly_soft: float = 0.0
    grainy: float = 0.0
    noise_median: float = 0.0
    face_soft: float = 0.0

    @classmethod
    def from_batch(
        cls,
        metrics: Sequence[PhotoMetrics],
        readings: Optional[Sequence[FaceReading]] = None,
    ) -> "Thresholds":
        usable = [m for m in metrics if m.ok]
        if len(usable) < 4:
            # Too few photos for percentiles to mean anything. Zeroed cut-offs
            # simply switch the comparative faults off rather than inventing a
            # verdict from three samples.
            return cls()

        sharpness = np.asarray([m.sharpness for m in usable], dtype=np.float64)
        noise = np.asarray([m.noise for m in usable], dtype=np.float64)

        # Faces are compared against other faces, never against whole frames.
        # Skin is smooth and a wedding frame is full of detail - jewellery,
        # embroidery, flowers - so a face always measures softer than the
        # photograph containing it, whatever the focus did. Judging a face
        # against the frame therefore reports "focus missed" on perfectly
        # sharp portraits.
        face_soft = 0.0
        if readings:
            face_sharpness = [
                r.main.sharpness for r in readings
                if r.ok and r.main and r.main.sharpness > 0
            ]
            if len(face_sharpness) >= 4:
                face_soft = float(np.percentile(face_sharpness,
                                                FACE_SOFT_PERCENTILE))

        return cls(
            soft=float(np.percentile(sharpness, SOFT_PERCENTILE)),
            slightly_soft=float(np.percentile(sharpness, SLIGHTLY_SOFT_PERCENTILE)),
            grainy=float(np.percentile(noise, GRAINY_PERCENTILE)),
            noise_median=float(np.median(noise)),
            face_soft=face_soft,
        )


# ============================================================================
# DIAGNOSIS
# ============================================================================

def _exposure_issues(m: PhotoMetrics) -> List[Issue]:
    issues: List[Issue] = []

    if m.brightness < DARK_BRIGHTNESS:
        issues.append(Issue(
            "too_dark", "Low light", "major",
            f"average brightness {m.brightness:.0f} of 255",
        ))
    elif m.brightness > BRIGHT_BRIGHTNESS:
        issues.append(Issue(
            "too_bright", "Washed out", "major",
            f"average brightness {m.brightness:.0f} of 255",
        ))

    if m.crushed >= CRUSHED_MAJOR:
        issues.append(Issue(
            "shadows_lost", "Shadow detail lost", "major",
            f"{m.crushed * 100:.0f}% of the frame is pure black",
        ))
    elif m.crushed >= CRUSHED_MINOR:
        issues.append(Issue(
            "shadows_dark", "Dark shadows", "minor",
            f"{m.crushed * 100:.0f}% of the frame is pure black",
        ))

    if m.blown >= BLOWN_MAJOR:
        issues.append(Issue(
            "highlights_lost", "Highlights blown out", "major",
            f"{m.blown * 100:.0f}% of the frame is pure white, "
            f"detail cannot be recovered",
        ))
    elif m.blown >= BLOWN_MINOR:
        issues.append(Issue(
            "highlights_hot", "Bright highlights", "minor",
            f"{m.blown * 100:.0f}% of the frame is pure white",
        ))

    return issues


def _focus_and_noise_issues(m: PhotoMetrics, t: Thresholds) -> List[Issue]:
    issues: List[Issue] = []

    if t.soft > 0:
        if m.sharpness <= t.soft:
            issues.append(Issue(
                "out_of_focus", "Out of focus", "major",
                "among the softest in this batch",
            ))
        elif m.sharpness <= t.slightly_soft:
            issues.append(Issue(
                "slightly_soft", "Slightly soft", "minor",
                "softer than most of this batch",
            ))

    # Both conditions are needed. A percentile alone misfires badly when the
    # batch has little spread: if most photos share near-identical noise, the
    # cut-off lands on that shared value and flags all of them at once. The
    # margin above the median demands that the grain actually stand out.
    if (t.grainy > 0 and m.noise >= t.grainy
            and m.noise > t.noise_median * GRAINY_MARGIN):
        issues.append(Issue(
            "grainy", "Grainy", "minor",
            "noticeably more sensor noise than the rest of this batch",
        ))

    if m.contrast < FLAT_CONTRAST:
        issues.append(Issue(
            "flat", "Flat, low contrast", "minor",
            f"intensity spread {m.contrast:.0f}",
        ))

    return issues


def _face_issues(reading: Optional[FaceReading],
                 thresholds: "Thresholds") -> List[Issue]:
    """
    Faults about the people in the photograph.

    Returns nothing at all when no face probe was run, which is different from
    finding no faces: absent data must not be reported as an absent subject.
    """
    if reading is None or not reading.ok:
        return []

    if not reading.faces:
        # Minor, not major, and deliberately so. A wedding set is full of
        # photographs with nobody in them - the venue, the rings, the cake,
        # the table settings - and they are not faults. Calling this major
        # would push every detail shot to the bottom of the ranking and
        # declare an entire batch broken.
        return [Issue(
            "no_face", "Nobody in frame", "minor",
            "no face found; this reads as a venue or detail shot",
        )]

    issues: List[Issue] = []
    main = reading.main

    if not main.is_prominent:
        issues.append(Issue(
            "face_small", "Faces are small", "minor",
            f"largest face is {main.size_ratio * 100:.0f}% of the frame; "
            f"this looks like a wide shot",
        ))

    # Backlighting: the classic wedding problem, a bright window behind the
    # couple and their faces in shadow. A frame-wide exposure reading misses
    # it entirely because the average looks fine.
    if (reading.frame_brightness > 0
            and main.brightness < reading.frame_brightness * FACE_DARK_RATIO
            and main.brightness < FACE_DARK_ABSOLUTE):
        issues.append(Issue(
            "face_dark", "Face in shadow", "major",
            f"face brightness {main.brightness:.0f} against "
            f"{reading.frame_brightness:.0f} for the frame, "
            f"which suggests backlighting",
        ))

    # Focus landing behind the subject: sharp background, soft faces. Both
    # comparisons must agree - see the note on FACE_SOFT_RATIO.
    softer_than_frame = (
        reading.frame_sharpness > 0
        and main.sharpness < reading.frame_sharpness * FACE_SOFT_RATIO
    )
    softer_than_other_faces = (
        thresholds.face_soft > 0 and main.sharpness <= thresholds.face_soft
    )
    if softer_than_frame and softer_than_other_faces:
        issues.append(Issue(
            "face_soft", "Face not in focus", "major",
            "softer than the other faces in this batch and softer than its "
            "own frame, so the focus has landed behind the subject",
        ))

    if main.frontality < FACE_TURNED:
        issues.append(Issue(
            "face_turned", "Head turned away", "minor",
            "the face is at a strong angle to the camera",
        ))

    if main.tilt_degrees > FACE_TILTED:
        issues.append(Issue(
            "face_tilted", "Head tilted", "minor",
            f"about {main.tilt_degrees:.0f} degrees off level",
        ))

    return issues


def diagnose(
    m: PhotoMetrics,
    reading: Optional[FaceReading] = None,
    thresholds: Optional[Thresholds] = None,
) -> List[Issue]:
    """
    Every fault found in one photograph, worst first.

    An unreadable file gets one issue and no measurements, because none exist.
    """
    if not m.ok:
        return [Issue("unreadable", "Could not be read", "major",
                      m.error or "unknown error")]

    thresholds = thresholds or Thresholds()

    issues = (
        _exposure_issues(m)
        + _focus_and_noise_issues(m, thresholds)
        + _face_issues(reading, thresholds)
    )

    issues.sort(key=lambda i: SEVERITY_ORDER.get(i.severity, 9))
    return issues


# ============================================================================
# SUMMARY
# ============================================================================

def headline(issues: Sequence[Issue], faces: int = -1) -> str:
    """
    One short phrase for a photo's condition, for a grid tile.

    Leads with the worst fault, because that is what decides whether a photo is
    usable. Says nothing about faces unless a probe actually ran.
    """
    if not issues:
        if faces > 1:
            return f"Clean, {faces} faces"
        if faces == 1:
            return "Clean, face in focus"
        return "No problems found"

    major = [i for i in issues if i.is_major]
    if major:
        rest = len(issues) - 1
        return major[0].label + (f" +{rest} more" if rest else "")

    return issues[0].label + (f" +{len(issues) - 1} more"
                              if len(issues) > 1 else "")


def tally(all_issues: Sequence[Sequence[Issue]]) -> Dict[str, int]:
    """How many photos carry each fault, for a batch-wide overview."""
    counts: Dict[str, int] = {}
    for issues in all_issues:
        for issue in issues:
            counts[issue.label] = counts.get(issue.label, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: -kv[1]))
