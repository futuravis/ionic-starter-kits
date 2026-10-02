"""
Selection sizing and alternate checks.

Covers the three rules that a real wedding forced on the design:

  * a percentage scales where a fixed count cannot - the same setting has to
    handle a six-photo ceremony and an 809-photo reception
  * every section keeps at least one photo, so a small ceremony is never
    dropped entirely
  * only groups that produced a pick contribute alternates, or the album grows
    back into a copy of the whole collection

Run with:
    python3 -m tests.test_selection
"""

from __future__ import annotations

import sys

from wedding_album import config, selection
from wedding_album.config import Section
from wedding_album.sections import Assignment


def shot(name: str, quality: float, section: Section = Section.BRIDE) -> Assignment:
    return Assignment(filepath=f"/c/{name}.JPG", section=section,
                      reason="test", quality=quality)


def make_groups(sizes, section: Section = Section.BRIDE):
    """Build shot groups with descending quality, sizes given per group."""
    groups = []
    q = 1.0
    for index, size in enumerate(sizes, 1):
        members = []
        for j in range(size):
            a = shot(f"g{index}_{j}", q, section)
            a.shot_group = index
            a.shot_group_size = size
            members.append(a)
            q -= 0.01
        groups.append(selection.ShotGroup(section=section, index=index,
                                          members=members))
    return groups


def main() -> int:
    checks = []

    def check(ok, label, detail=""):
        checks.append(ok)
        print(f"  [{'pass' if ok else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))

    print("\npercentage sizing scales with the shoot")
    for available, pct, want in [
        (6, 10, 1), (9, 10, 1), (25, 10, 3), (51, 10, 6),
        (136, 10, 14), (173, 10, 18), (809, 10, 81),
    ]:
        got = selection.target_count(available, percent=pct)
        check(got == want, f"{pct}% of {available} -> {got}", f"expected {want}")

    print("\nsmall sections are never emptied")
    check(selection.target_count(1, percent=1) == 1, "1 photo at 1% still keeps 1")
    check(selection.target_count(3, percent=3) >= config.MIN_PICKS_PER_SECTION,
          "floor respected")

    print("\nother modes")
    check(selection.target_count(173, per_section=3) == 3, "absolute count honoured")
    check(selection.target_count(173) == 173, "no limit keeps everything")
    check(selection.target_count(0, percent=50) == 0, "empty section stays empty")
    check(selection.target_count(5, percent=500) == 5, "cannot exceed what exists")

    print("\npicks spread across groups before repeating one")
    groups = make_groups([4, 2, 1])          # 7 photos, 3 distinct poses
    picks = selection.choose(groups, per_section=3, alternates_per_pick=0)
    chosen = picks[Section.BRIDE]
    used = [a.shot_group for a in chosen]
    check(sorted(used) == [1, 2, 3],
          "3 picks come from 3 different groups", str(used))

    print("\nonce every group has one, it comes back for seconds")
    groups = make_groups([4, 2, 1])
    picks = selection.choose(groups, per_section=5, alternates_per_pick=0)
    used = [a.shot_group for a in picks[Section.BRIDE]]
    check(used[:3] == [1, 2, 3] and sorted(used[3:]) == [1, 2],
          "round two revisits the larger groups", str(used))

    print("\nasking for more than exists is harmless")
    groups = make_groups([2, 1])
    picks = selection.choose(groups, per_section=99, alternates_per_pick=0)
    check(len(picks[Section.BRIDE]) == 3, "returns all 3 and stops",
          str(len(picks[Section.BRIDE])))

    print("\nalternates come only from groups that produced a pick")
    groups = make_groups([3, 2, 1, 4])       # 10 photos, 4 groups
    picks = selection.choose(groups, per_section=2, alternates_per_pick=1)
    picked_groups = {a.shot_group for a in picks[Section.BRIDE]}
    alternates = [m for g in groups for m in g.members if m.is_alternate]
    alt_groups = {a.shot_group for a in alternates}

    check(alt_groups <= picked_groups,
          "no alternate from an unpicked group",
          f"picked={sorted(picked_groups)} alts={sorted(alt_groups)}")
    check(len(alternates) <= len(picks[Section.BRIDE]),
          f"at most one alternate per pick ({len(alternates)} for "
          f"{len(picks[Section.BRIDE])} picks)")
    check(all(a.alternate_for for a in alternates),
          "each alternate records which pick it backs up")

    print("\na single-photo group yields no alternate")
    groups = make_groups([1])
    selection.choose(groups, per_section=1, alternates_per_pick=1)
    check(not any(m.is_alternate for g in groups for m in g.members),
          "nothing to fall back on, nothing marked")

    print("\nalternates can be switched off")
    groups = make_groups([3, 3])
    selection.choose(groups, per_section=2, alternates_per_pick=0)
    check(not any(m.is_alternate for g in groups for m in g.members),
          "zero means picks only")

    print("\nre-choosing clears previous flags")
    groups = make_groups([3, 3])
    selection.choose(groups, per_section=2, alternates_per_pick=1)
    selection.choose(groups, per_section=1, alternates_per_pick=0)
    picks_now = [m for g in groups for m in g.members if m.is_pick]
    alts_now = [m for g in groups for m in g.members if m.is_alternate]
    check(len(picks_now) == 1 and not alts_now,
          "no stale picks or alternates", f"{len(picks_now)} picks, {len(alts_now)} alts")

    passed, total = sum(checks), len(checks)
    print(f"\n{passed}/{total} passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
