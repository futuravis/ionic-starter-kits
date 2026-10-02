"""
Why two clusters that look like the same person were kept apart.

Only one rule prevents a merge: two faces in a single photograph cannot be the
same person. That rule is sound, but it relies on the detections being real. A
face detected twice in one frame, or a printed photograph of the couple hanging
at the venue, would make one person look like two people standing together - and
would then keep them apart forever, however alike their faces are.

    python3 -m tools.why_not_merged '<root>' p02 p10
"""

from __future__ import annotations

import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wedding_album import config, people, scanner, store  # noqa: E402


def main() -> int:
    if len(sys.argv) < 4:
        print(__doc__)
        return 2

    root = os.path.expanduser(sys.argv[1])
    want_a, want_b = sys.argv[2], sys.argv[3]

    cache = store.FaceCache(root)
    if not cache.load():
        print("no face cache", file=sys.stderr)
        return 2

    shoots = scanner.find_shoots(root)
    photos = [p for _, ph in shoots for p in ph]
    results = [r for r in (cache.get(p) for p in photos) if r]

    found = people.cluster(results)
    people.carry_labels(found, root)
    by_id = {p.id: p for p in found}

    a, b = by_id.get(want_a), by_id.get(want_b)
    if not a or not b:
        print(f"unknown id(s); have {len(found)} clusters", file=sys.stderr)
        return 2

    similarity = float(np.dot(a.centroid, b.centroid))
    print(f"\n{a.id} ({a.photo_count} photos, role={a.role!r}) vs "
          f"{b.id} ({b.photo_count} photos, role={b.role!r})")
    print(f"centroid similarity {similarity:.3f}, merge threshold "
          f"{config.FACE_MATCH_THRESHOLD}")
    print(f"cohesion: {a.id} {a.cohesion():.3f}, {b.id} {b.cohesion():.3f}")

    shared = sorted(a.photos & b.photos)
    print(f"\nphotos containing both: {len(shared)}")

    if not shared:
        print("  none - so the same-photo rule is not what kept them apart")
        return 0

    print("  the same-photo rule blocked the merge because of these frames:\n")
    for path in shared[:10]:
        print(f"    {os.path.relpath(path, root)}")
        faces_a = [r for r in a.refs if r.filepath == path]
        faces_b = [r for r in b.refs if r.filepath == path]
        for ref in faces_a:
            print(f"        {a.id}: face #{ref.face_index} "
                  f"size={ref.size_ratio:.3f} score={ref.score:.2f}")
        for ref in faces_b:
            print(f"        {b.id}: face #{ref.face_index} "
                  f"size={ref.size_ratio:.3f} score={ref.score:.2f}")
        # If the two faces sit on top of each other, one is a duplicate
        # detection of a single person rather than a second person.
        for ra in faces_a:
            for rb in faces_b:
                same = float(np.dot(ra.embedding, rb.embedding))
                print(f"        face-to-face similarity {same:.3f}"
                      + ("   <-- these are the same face" if same > 0.6 else ""))

    if len(shared) > 10:
        print(f"    ... and {len(shared) - 10} more")

    return 0


if __name__ == "__main__":
    sys.exit(main())
