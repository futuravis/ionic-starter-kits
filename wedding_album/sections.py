"""
Deciding which album section each photo belongs to.

An album designer puts a photograph on exactly one page, so this assigns
exactly one section per photo. No duplication: the same shot never appears in
both Couple and Family.

The rule is a priority walk, defined in config.SECTION_PRIORITY. Every decision
also records a human-readable reason, because a pile of sorted folders is
impossible to audit otherwise and "why is this in Family?" is the first
question anyone asks.

Two behaviours worth knowing:

* Clusters labeled Role.IGNORE contribute nothing. The test collection has a
  brass Ganesha idol that the detector confidently reads as a face in three
  frames; ignoring it lets those photos be judged on the couple actually in
  them.

* An unlabeled person does not override a named role. A photo of the bride with
  three strangers files under Bride, not Group, because the only relationship
  the tool actually knows about is hers. Labeling those strangers as family or
  friends moves the photo on the next run, which is the intended way to refine
  an album rather than a limitation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Set

from . import config
from .config import Role, Section
from .faces import FaceResult
from .people import Person


# ============================================================================
# RESULT
# ============================================================================

@dataclass
class Assignment:
    """Where one photo goes, and why."""

    filepath: str
    section: Section
    reason: str
    person_ids: List[str] = field(default_factory=list)
    person_names: List[str] = field(default_factory=list)
    quality: float = 0.0
    face_count: int = 0          # faces detected, before ignoring anything
    people_count: int = 0        # real people, after ignoring
    is_duplicate: bool = False
    duplicate_of: str = ""

    # Set by selection.py: which look-alike group within the section this photo
    # belongs to, and whether it was chosen for the album rather than held as a
    # runner-up.
    shot_group: int = 0
    shot_group_size: int = 0
    is_pick: bool = False
    pick_rank: int = 0
    pick_reason: str = ""

    # A runner-up kept alongside a pick, so there is a second option to fall
    # back on without carrying the entire collection into the album.
    is_alternate: bool = False
    alternate_for: str = ""

    # Set aside before picking as too soft, shaken or badly exposed to print.
    is_low_quality: bool = False

    # The most-photographed unidentified person in this frame, and how many
    # photos they appear in across the whole collection.
    #
    # Unsorted is otherwise a heap: it holds every photo whose people have not
    # been named, and nothing tells you which of those strangers is worth
    # naming. Filing by the recurring figure turns the heap into a short list of
    # faces, where one label can move a hundred photos at once.
    dominant_unknown: str = ""
    dominant_unknown_photos: int = 0

    # The file itself could not be decoded. Distinct from low quality: there is
    # no photograph here to judge, and nothing that can be copied or previewed.
    # The test collection contained one such file, 5.7 MB of zero bytes.
    is_unreadable: bool = False

    @property
    def bucket(self) -> str:
        """
        Which review bin this photo belongs to.

        Order matters: a blurred repeat is filed as a duplicate, because that is
        the more useful thing to know about it.
        """
        if self.is_unreadable:
            return "unreadable"
        if self.is_duplicate:
            return "duplicate"
        if self.is_low_quality:
            return "low_quality"
        if self.section == Section.NO_FACE:
            return "no_face"
        if self.is_pick:
            return "pick"
        if self.is_alternate:
            return "alternate"
        return "unused"

    @property
    def section_name(self) -> str:
        return self.section.value


# ============================================================================
# PER-PHOTO CONTEXT
# ============================================================================

@dataclass
class _Present:
    """The people in one photo, grouped by the role you gave them."""

    bride: List[Person] = field(default_factory=list)
    groom: List[Person] = field(default_factory=list)
    family: List[Person] = field(default_factory=list)
    friends: List[Person] = field(default_factory=list)
    kids: List[Person] = field(default_factory=list)
    unlabeled: List[Person] = field(default_factory=list)

    @property
    def all_real(self) -> List[Person]:
        return (self.bride + self.groom + self.family
                + self.friends + self.kids + self.unlabeled)

    @property
    def count(self) -> int:
        return len(self.all_real)

    @property
    def named(self) -> List[Person]:
        return self.bride + self.groom + self.family + self.friends + self.kids


def _gather(
    filepath: str,
    people: Sequence[Person],
) -> _Present:
    """Bucket the people appearing in one photo by role, dropping IGNORE."""
    present = _Present()

    for person in people:
        if filepath not in person.photos:
            continue
        if person.role == Role.IGNORE.value:
            continue                      # statue, poster, framed photo

        if person.role == Role.BRIDE.value:
            present.bride.append(person)
        elif person.role == Role.GROOM.value:
            present.groom.append(person)
        elif person.role == Role.FAMILY.value:
            present.family.append(person)
        elif person.role == Role.FRIEND.value:
            present.friends.append(person)
        elif person.role == Role.KID.value:
            present.kids.append(person)
        else:
            present.unlabeled.append(person)

    return present


def _names(people: Sequence[Person]) -> str:
    out = [p.name or p.id for p in people]
    if len(out) <= 3:
        return ", ".join(out)
    return ", ".join(out[:3]) + f" +{len(out) - 3} more"


def _solo_note(present: "_Present") -> str:
    """
    Describe a solo portrait honestly.

    Saying "alone" when the frame also holds unidentified people would be a
    small lie in the audit trail, and the whole point of the reason string is
    that it can be trusted.
    """
    n = len(present.unlabeled)
    if not n:
        return " alone"
    return f" with {n} unlabeled person(s); label them to refile this photo"


def _kid_is_focus(
    filepath: str,
    kids: Sequence[Person],
    boxes: Dict[str, List[float]],
) -> bool:
    """
    Whether a child is the subject rather than merely in frame.

    The spec asks for Kids when "a child is the focus", so a toddler in the
    background of a family portrait should not pull that photo out of Family.
    Focus is read as the child's face being prominent in the frame.
    """
    ratios = boxes.get(filepath, [])
    if not ratios:
        return bool(kids)

    for person in kids:
        for ref in person.refs:
            if ref.filepath == filepath and ref.size_ratio >= config.FACE_PROMINENT_SIZE_RATIO:
                return True
    return False


# ============================================================================
# THE PRIORITY WALK
# ============================================================================

def decide(
    filepath: str,
    people: Sequence[Person],
    face_count: int = 0,
    size_ratios: Optional[Dict[str, List[float]]] = None,
) -> tuple[Section, str, _Present]:
    """
    Pick the one section for this photo and explain the choice.

    Order comes from config.SECTION_PRIORITY:
        Kids > Family > Friends > Couple > Bride/Groom > Group
    """
    present = _gather(filepath, people)
    ratios = size_ratios or {}

    # Nobody real in frame: this photograph is about a thing, not a person.
    # Covers the cake, the pillaiyar idol, the panai, decor and detail shots.
    # No object detector is involved - the absence of people is the signal.
    if present.count == 0:
        if face_count:
            return (
                Section.NO_FACE,
                f"no people; {face_count} detected face(s) belong to "
                f"something marked not-a-person",
                present,
            )
        return Section.NO_FACE, "no people in frame", present

    # 1. Kids, when a child is actually the subject.
    if present.kids and _kid_is_focus(filepath, present.kids, ratios):
        return (
            Section.KIDS,
            f"child in focus: {_names(present.kids)}",
            present,
        )

    # 2. Family. Outranks Couple on purpose: a shot of the couple with a parent
    #    is the family's moment, per the worked example in the spec.
    if present.family:
        extra = ""
        if present.bride and present.groom:
            extra = " (couple present, but family takes priority)"
        elif present.kids:
            extra = " (child present but not in focus)"
        return (
            Section.FAMILY,
            f"family present: {_names(present.family)}{extra}",
            present,
        )

    # 3. Friends.
    if present.friends:
        extra = " (couple present, but friends take priority)" if (
            present.bride and present.groom) else ""
        return (
            Section.FRIENDS,
            f"friend present: {_names(present.friends)}{extra}",
            present,
        )

    # 4. The couple together, with nobody else labeled.
    if present.bride and present.groom:
        note = ""
        if present.unlabeled:
            note = f" (+{len(present.unlabeled)} unlabeled)"
        return (
            Section.COUPLE,
            f"bride and groom together{note}",
            present,
        )

    # 5. Solo portraits.
    if present.bride:
        return Section.BRIDE, f"bride{_solo_note(present)}", present
    if present.groom:
        return Section.GROOM, f"groom{_solo_note(present)}", present

    # 6. A gathering of people none of whom you have labeled.
    if present.count >= config.GROUP_MIN_PEOPLE:
        return (
            Section.GROUP,
            f"{present.count} unlabeled people",
            present,
        )

    # 7. One or two unlabeled people: not a group, not identifiable.
    return (
        Section.UNSORTED,
        f"{present.count} unlabeled person(s); label them to place this photo",
        present,
    )


# ============================================================================
# WHOLE COLLECTION
# ============================================================================

def assign_all(
    results: Sequence[FaceResult],
    people: Sequence[Person],
    quality_scores: Optional[Dict[str, float]] = None,
) -> List[Assignment]:
    """Assign every photo to exactly one section."""
    scores = quality_scores or {}

    size_ratios: Dict[str, List[float]] = {
        r.filepath: [f.size_ratio for f in r.faces] for r in results
    }
    face_counts: Dict[str, int] = {r.filepath: r.count for r in results}

    assignments: List[Assignment] = []

    for result in results:
        section, reason, present = decide(
            result.filepath,
            people,
            face_count=face_counts.get(result.filepath, 0),
            size_ratios=size_ratios,
        )

        named = present.named

        # Whoever recurs most across the collection is the one worth naming.
        dominant = max(
            present.unlabeled,
            key=lambda p: (p.photo_count, p.id),
            default=None,
        )

        assignments.append(
            Assignment(
                filepath=result.filepath,
                section=section,
                reason=reason,
                person_ids=[p.id for p in present.all_real],
                person_names=[p.name or p.id for p in named],
                quality=scores.get(result.filepath, 0.0),
                face_count=face_counts.get(result.filepath, 0),
                people_count=present.count,
                dominant_unknown=dominant.id if dominant else "",
                dominant_unknown_photos=dominant.photo_count if dominant else 0,
            )
        )

    return assignments


# ============================================================================
# NEAR-DUPLICATES
# ============================================================================

def mark_duplicates(
    assignments: Sequence[Assignment],
    phashes: Dict[str, str],
    threshold: int = config.DUPLICATE_HASH_THRESHOLD,
) -> int:
    """
    Flag near-identical shots within each section, keeping the best-scoring one.

    Wedding shoots are full of bursts of the same pose. Comparison is confined
    to one section, since two photos in different sections are different moments
    by definition and never compete.

    Returns how many were flagged. Nothing is deleted; step 6 decides what to do
    with the flag.
    """
    from .quality import phash_distance

    by_section: Dict[Section, List[Assignment]] = {}
    for a in assignments:
        by_section.setdefault(a.section, []).append(a)

    flagged = 0
    for group in by_section.values():
        # Best first, so the survivor of each burst is the strongest photo.
        ordered = sorted(group, key=lambda a: a.quality, reverse=True)
        keepers: List[Assignment] = []

        for candidate in ordered:
            chash = phashes.get(candidate.filepath, "")
            if not chash:
                keepers.append(candidate)
                continue

            duplicate_of = ""
            for keeper in keepers:
                khash = phashes.get(keeper.filepath, "")
                if khash and phash_distance(chash, khash) <= threshold:
                    duplicate_of = keeper.filepath
                    break

            if duplicate_of:
                candidate.is_duplicate = True
                candidate.duplicate_of = duplicate_of
                flagged += 1
            else:
                keepers.append(candidate)

    return flagged


# ============================================================================
# REPORTING
# ============================================================================

def mark_low_quality(
    assignments: Sequence[Assignment],
    percentile: float = config.LOW_QUALITY_PERCENTILE,
) -> int:
    """
    Set aside the weakest slice of the collection before any picking happens.

    A percentile, not a fixed score, because quality is measured relative to the
    collection it came from. Duplicates are left alone: they are already
    accounted for, and double-labelling them would only muddle the report.

    Returns how many were flagged.
    """
    if percentile <= 0:
        return 0

    candidates = [a for a in assignments
                  if not a.is_duplicate and not a.is_unreadable]
    if len(candidates) < 10:
        # Too few to talk about percentiles meaningfully; dropping 10% of six
        # photos is arbitrary rather than useful.
        return 0

    import numpy as np

    cutoff = float(np.percentile([a.quality for a in candidates], percentile))

    flagged = 0
    for a in candidates:
        if a.quality <= cutoff:
            a.is_low_quality = True
            flagged += 1
    return flagged


def tally(assignments: Sequence[Assignment]) -> Dict[Section, int]:
    counts = {s: 0 for s in config.SECTION_ORDER}
    for a in assignments:
        counts[a.section] = counts.get(a.section, 0) + 1
    return counts


def summarize(assignments: Sequence[Assignment]) -> str:
    counts = tally(assignments)
    total = len(assignments)
    dupes = sum(1 for a in assignments if a.is_duplicate)

    lines = [f"    {'section':<14} {'photos':>6}"]
    for section in config.SECTION_ORDER:
        n = counts.get(section, 0)
        if n:
            lines.append(f"    {section.value:<14} {n:>6}")

    lines.append(f"    {'-' * 14} {'-' * 6}")
    lines.append(f"    {'total':<14} {total:>6}")
    if dupes:
        lines.append(f"    ({dupes} flagged as near-duplicates)")

    # Every photo must land in exactly one section; assert it rather than
    # trusting it, since silent loss is the worst failure here.
    assigned = sum(counts.get(s, 0) for s in config.SECTION_ORDER)
    if assigned != total:
        lines.append(f"    WARNING: {total - assigned} photo(s) unaccounted for")

    return "\n".join(lines)
