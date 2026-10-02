"""
How the same-person threshold changes the number of people found.

Motivated by a real failure: the groom appeared as three separate clusters
(p02 with 442 photos, p05 with 186, p23 with 30), so "label the top fourteen
faces" would have meant naming the same man three times and still leaving his
photos scattered.

A stricter threshold splits one person across several clusters; a looser one
risks merging two people. The same-photo constraint makes loosening safer than
it sounds - two faces in one photograph can never be the same person, so the
couple can never merge into each other no matter how alike the embeddings look.

    python3 -m tools.threshold_sweep '<root>'
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wedding_album import people, scanner, store  # noqa: E402


def main() -> int:
    root = os.path.expanduser(
        sys.argv[1] if len(sys.argv) > 1 else "~/Desktop/candid marriage")

    cache = store.FaceCache(root)
    if not cache.load():
        print("no face cache; run the batch first", file=sys.stderr)
        return 2

    shoots = scanner.find_shoots(root)
    photos = [p for _, ph in shoots for p in ph]
    results = [r for r in (cache.get(p) for p in photos) if r]
    print(f"{len(results)} photos, "
          f"{sum(r.count for r in results)} faces\n")

    # Cluster count alone is a trap. Fewer clusters looks like less
    # fragmentation but is equally consistent with two people merged into one,
    # and merging is the worse error: it puts a guest's photos under someone
    # else's name. Cohesion - how tightly members sit around their own centre -
    # is what distinguishes the two, so it is reported alongside.
    print(f"{'threshold':>10} {'people':>7} {'mean cohesion':>14} "
          f"{'worst of top 10':>16} {'impure':>7} {'1-2 photos':>11}")
    print("-" * 74)

    for threshold in (0.24, 0.28, 0.32, 0.363, 0.40, 0.45, 0.50):
        found = people.cluster(results, threshold=threshold)
        big = [p for p in found if p.face_count >= 5]
        cohesions = [p.cohesion() for p in big] or [1.0]
        top10 = sorted(big, key=lambda p: -p.photo_count)[:10]
        worst = min((p.cohesion() for p in top10), default=1.0)
        impure = sum(1 for p in big if p.cohesion() < 0.60)
        rare = sum(1 for p in found if p.photo_count <= 2)

        print(f"{threshold:>10.3f} {len(found):>7} "
              f"{sum(cohesions) / len(cohesions):>14.3f} {worst:>16.3f} "
              f"{impure:>7} {rare:>11}")

    print("\n  mean cohesion   average tightness of clusters with 5+ faces")
    print("  worst of top 10 the least tight of the ten biggest clusters")
    print("  impure          clusters below 0.60, which usually means two")
    print("                  people ended up in one")
    print("\nPrefer the loosest threshold that keeps the big clusters tight.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
