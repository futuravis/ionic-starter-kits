"""
Choosing which photos from a section actually make the album.

Two stages, because "best" and "a good set" are different questions.

1. Shot groups. Photos in a section are grouped by how alike they look, so a
   burst of one pose collapses into one group and a genuinely different setup
   forms its own.

2. Picks, taken round-robin across those groups. The first pick is the best
   photo of the strongest group, the second is the best of the next group, and
   only once every group has contributed does it come back for second-best.

The reason for the round-robin is the whole point of this module. Simply taking
the three highest scores from the bride's ten photos gave three frames of one
pose, because a good setup wins every top slot. Spreading across groups gives
three different photographs, which is what a get-ready page wants.

Everything not picked stays available as a runner-up rather than being thrown
away.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Sequence

from . import config, timeline
from .config import Section
from .quality import phash_distance
from .sections import Assignment


# ============================================================================
# SHOT GROUPS
# ============================================================================

@dataclass
class ShotGroup:
    """Photos in one section that show much the same thing."""

    section: Section
    index: int                                  # 1-based, per section
    members: List[Assignment] = field(default_factory=list)

    @property
    def best(self) -> Assignment:
        return self.members[0]                  # members are kept sorted

    @property
    def size(self) -> int:
        return len(self.members)

    def sort(self) -> None:
        self.members.sort(key=lambda a: -a.quality)


def group_shots(
    assignments: Sequence[Assignment],
    phashes: Dict[str, str],
    threshold: int = config.SHOT_GROUP_THRESHOLD,
    times: Optional[Dict[str, "datetime"]] = None,
    window_minutes: Optional[float] = None,
) -> List[ShotGroup]:
    """
    Group each section's photos by what they show *and* when they were taken.

    Both conditions must hold. Appearance alone merges moments that merely
    resemble each other: the couple during the portraits and the couple on stage
    two hours later are the same two people in the same clothes doing much the
    same thing, and are nevertheless different parts of the evening. Requiring
    them to be close in time keeps both.

    Grouping never crosses a section boundary either, so two photos on different
    album pages never compete for the same pick.

    Assigns `shot_group` on every assignment and returns the groups, ordered by
    section and then by their best photo.
    """
    times = times or {}
    if window_minutes is None:
        window_minutes = config.SHOT_GROUP_WINDOW_MINUTES
    by_section: Dict[Section, List[Assignment]] = {}
    for a in assignments:
        by_section.setdefault(a.section, []).append(a)

    groups: List[ShotGroup] = []

    for section in config.SECTION_ORDER:
        members = by_section.get(section)
        if not members:
            continue

        # Strongest first, so each group forms around its best photo and that
        # photo becomes the group's representative.
        ordered = sorted(members, key=lambda a: -a.quality)
        section_groups: List[ShotGroup] = []

        for candidate in ordered:
            chash = phashes.get(candidate.filepath, "")
            placed = False

            if chash:
                for group in section_groups:
                    leader = group.best.filepath
                    leader_hash = phashes.get(leader, "")
                    if not leader_hash:
                        continue
                    if phash_distance(chash, leader_hash) > threshold:
                        continue
                    # Looks alike; now it must also belong to the same moment.
                    if times and not timeline.same_moment(
                            times.get(candidate.filepath), times.get(leader),
                            window_minutes):
                        continue
                    group.members.append(candidate)
                    placed = True
                    break

            if not placed:
                section_groups.append(
                    ShotGroup(section=section, index=0, members=[candidate])
                )

        # Order groups by their best photo, then number them for display.
        section_groups.sort(key=lambda g: -g.best.quality)
        for i, group in enumerate(section_groups, 1):
            group.index = i
            group.sort()
            for member in group.members:
                member.shot_group = i
                member.shot_group_size = group.size

        groups.extend(section_groups)

    return groups


# ============================================================================
# PICKING
# ============================================================================

def target_count(
    available: int,
    per_section: int = 0,
    percent: float = 0.0,
) -> int:
    """
    How many photos to pick from a section holding `available` of them.

    A percentage wins when given, because it is the setting that survives a real
    wedding: the same 10% keeps 17 of the reception's 173 late-night frames and
    1 of a six-photo ceremony, where any fixed number is wrong for one of them.

    Always at least MIN_PICKS_PER_SECTION, never more than exists.
    """
    if available <= 0:
        return 0

    if percent and percent > 0:
        wanted = int(math.ceil(available * percent / 100.0))
    elif per_section and per_section > 0:
        wanted = per_section
    else:
        return available                        # keep everything

    return max(config.MIN_PICKS_PER_SECTION, min(wanted, available))


def choose(
    groups: Sequence[ShotGroup],
    per_section: int = config.DEFAULT_PICKS_PER_SECTION,
    skip_duplicates: bool = False,
    percent: float = 0.0,
    alternates_per_pick: int = config.ALTERNATES_PER_PICK,
) -> Dict[Section, List[Assignment]]:
    """
    Pick photos per section, spread across shot groups.

    Size comes from `percent` when given, otherwise `per_section`; 0 for both
    keeps everything. Returns section -> picks in order, and sets `is_pick`,
    `pick_rank` and `pick_reason` on the assignments so the report and the UI can
    explain every choice.
    """
    picks: Dict[Section, List[Assignment]] = {}

    by_section: Dict[Section, List[ShotGroup]] = {}
    for group in groups:
        by_section.setdefault(group.section, []).append(group)

    for section, section_groups in by_section.items():
        # Reset, so re-running with a different limit does not leave stale flags.
        for group in section_groups:
            for member in group.members:
                member.is_pick = False
                member.pick_rank = 0
                member.pick_reason = ""
                member.is_alternate = False
                member.alternate_for = ""

        eligible = [
            [m for m in g.members if not (skip_duplicates and m.is_duplicate)]
            for g in section_groups
        ]

        chosen: List[Assignment] = []
        limit = target_count(
            sum(len(e) for e in eligible),
            per_section=per_section,
            percent=percent,
        )

        # Round-robin: one from each group before anyone gets a second.
        round_index = 0
        while len(chosen) < limit:
            took_any = False
            for group_number, members in enumerate(eligible, 1):
                if len(chosen) >= limit:
                    break
                if round_index >= len(members):
                    continue
                candidate = members[round_index]
                candidate.is_pick = True
                candidate.pick_rank = len(chosen) + 1
                candidate.pick_reason = (
                    f"best of shot group {group_number}"
                    if round_index == 0 else
                    f"#{round_index + 1} of shot group {group_number}"
                )
                chosen.append(candidate)
                took_any = True
            if not took_any:
                break                            # every group exhausted
            round_index += 1

        picks[section] = chosen
        _mark_alternates(chosen, eligible, alternates_per_pick)

    return picks


def _mark_alternates(
    chosen: Sequence[Assignment],
    eligible: Sequence[List[Assignment]],
    per_pick: int,
) -> None:
    """
    Keep a few runners-up, drawn only from groups that produced a pick.

    Confining alternatives to picked groups is what keeps an album an album. The
    alternative to a chosen photo is the next-best frame of the same moment,
    which is what someone reviewing the choice wants to see; a photo from a group
    that lost outright is not a second option, it is just another photo.
    """
    if per_pick <= 0:
        return

    picked = {a.filepath for a in chosen}

    for members in eligible:
        # Which pick, if any, came out of this group?
        owner = next((m for m in members if m.filepath in picked), None)
        if owner is None:
            continue

        taken = 0
        for member in members:
            if taken >= per_pick:
                break
            if member.filepath in picked:
                continue
            member.is_alternate = True
            member.alternate_for = owner.filepath
            taken += 1


# ============================================================================
# REPORTING
# ============================================================================

def summarize(groups: Sequence[ShotGroup],
              picks: Dict[Section, List[Assignment]]) -> str:
    """Per-section group and pick breakdown."""
    lines = [f"    {'section':<14} {'photos':>6} {'groups':>7} {'picks':>6}"]

    by_section: Dict[Section, List[ShotGroup]] = {}
    for group in groups:
        by_section.setdefault(group.section, []).append(group)

    total_photos = total_groups = total_picks = 0

    for section in config.SECTION_ORDER:
        section_groups = by_section.get(section)
        if not section_groups:
            continue
        photos = sum(g.size for g in section_groups)
        n_picks = len(picks.get(section, []))
        total_photos += photos
        total_groups += len(section_groups)
        total_picks += n_picks
        lines.append(
            f"    {section.value:<14} {photos:>6} {len(section_groups):>7} {n_picks:>6}"
        )

    lines.append(f"    {'-' * 14} {'-' * 6} {'-' * 7} {'-' * 6}")
    lines.append(f"    {'total':<14} {total_photos:>6} {total_groups:>7} {total_picks:>6}")
    return "\n".join(lines)


def describe_groups(groups: Sequence[ShotGroup], limit: int = 0) -> str:
    """Group-by-group detail, for eyeballing why picks landed where they did."""
    import os

    lines: List[str] = []
    current: Optional[Section] = None

    for group in groups:
        if group.section != current:
            current = group.section
            lines.append(f"\n  {current.value}")

        head = group.best
        lines.append(
            f"    group {group.index} - {group.size} shot(s), "
            f"best {os.path.basename(head.filepath)} ({head.quality * 100:.1f})"
        )
        shown = group.members if not limit else group.members[:limit]
        for member in shown:
            marks = []
            if member.is_pick:
                marks.append(f"PICK #{member.pick_rank}")
            if member.is_duplicate:
                marks.append("dup")
            suffix = f"   [{', '.join(marks)}]" if marks else ""
            lines.append(
                f"        {member.quality * 100:5.1f}  "
                f"{os.path.basename(member.filepath)}{suffix}"
            )

    return "\n".join(lines)
