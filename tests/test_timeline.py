"""
Timeline checks: grouping must respect when a photo was taken.

The case that motivated this: the couple photographed during the portraits and
photographed again on stage two hours later look almost identical - same two
people, same clothes, same pose - and are two different moments of the evening.
Grouping on appearance alone treats the second as a repeat of the first and
throws it away.

    python3 -m tests.test_timeline
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta

from wedding_album import config, selection, timeline
from wedding_album.config import Section
from wedding_album.sections import Assignment


BASE = datetime(2026, 7, 1, 18, 0, 0)


def shot(name: str, quality: float, minutes: float) -> Assignment:
    a = Assignment(filepath=f"/c/{name}.JPG", section=Section.COUPLE,
                   reason="test", quality=quality)
    a._taken = BASE + timedelta(minutes=minutes)  # type: ignore[attr-defined]
    return a


def main() -> int:
    checks = []

    def check(ok, label, detail=""):
        checks.append(ok)
        print(f"  [{'pass' if ok else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))

    print("\ngap measurement")
    t0 = BASE
    check(timeline.minutes_apart(t0, t0 + timedelta(minutes=3)) == 3.0,
          "three minutes apart")
    check(timeline.minutes_apart(t0 + timedelta(minutes=3), t0) == 3.0,
          "order does not matter")
    check(timeline.minutes_apart(t0, None) == float("inf"),
          "unknown time is infinitely far, never merged by accident")

    print("\nsame moment or not")
    check(timeline.same_moment(t0, t0 + timedelta(minutes=1)), "1 min -> same")
    check(not timeline.same_moment(t0, t0 + timedelta(minutes=30)),
          "30 min -> different")
    check(not timeline.same_moment(t0, None), "unknown -> never the same moment")

    print("\nidentical-looking photos taken together stay one group")
    # Same hash, one minute apart: a burst.
    a, b = shot("burst_a", 0.9, 0), shot("burst_b", 0.8, 1)
    phash = "f" * 64
    hashes = {a.filepath: phash, b.filepath: phash}
    times = {a.filepath: a._taken, b.filepath: b._taken}
    groups = selection.group_shots([a, b], hashes, times=times)
    check(len(groups) == 1, "one group for a burst", f"got {len(groups)}")

    print("\nidentical-looking photos hours apart become separate groups")
    # THE case: same appearance, 2 hours apart. Portraits vs on stage.
    c, d = shot("portrait", 0.9, 0), shot("onstage", 0.85, 120)
    hashes = {c.filepath: phash, d.filepath: phash}
    times = {c.filepath: c._taken, d.filepath: d._taken}
    groups = selection.group_shots([c, d], hashes, times=times)
    check(len(groups) == 2,
          "two groups, so both moments survive", f"got {len(groups)}")

    picks = selection.choose(groups, per_section=2, alternates_per_pick=0)
    chosen = {os.path.basename(a.filepath) for a in picks[Section.COUPLE]}
    check(chosen == {"portrait.JPG", "onstage.JPG"},
          "both are picked, neither discarded as a repeat", str(sorted(chosen)))

    print("\nwithout timestamps, behaviour is unchanged")
    e, f = shot("x", 0.9, 0), shot("y", 0.8, 120)
    hashes = {e.filepath: phash, f.filepath: phash}
    groups = selection.group_shots([e, f], hashes, times={})
    check(len(groups) == 1,
          "falls back to appearance alone when times are absent",
          f"got {len(groups)}")

    print("\nthe window is configurable")
    g, h = shot("p", 0.9, 0), shot("q", 0.8, 10)
    hashes = {g.filepath: phash, h.filepath: phash}
    times = {g.filepath: g._taken, h.filepath: h._taken}
    check(len(selection.group_shots([g, h], hashes, times=times,
                                    window_minutes=4)) == 2,
          "10 min apart with a 4 min window -> two groups")
    check(len(selection.group_shots([g, h], hashes, times=times,
                                    window_minutes=20)) == 1,
          "same pair with a 20 min window -> one group")

    print("\nconfig")
    check(config.SHOT_GROUP_WINDOW_MINUTES > 0,
          f"window is {config.SHOT_GROUP_WINDOW_MINUTES} min")
    check(config.ORDER_PICKS_BY_TIME,
          "picks are numbered in capture order, so the album reads as a story")

    passed, total = sum(checks), len(checks)
    print(f"\n{passed}/{total} passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
