"""
Compare two shoot folders by who is actually in them.

Used to decide whether two similarly named folders are the same family
photographed twice or two different groups. Face clusters answer that far more
reliably than the spelling of a folder name does.

    python3 -m tools.compare_folders '<root>' '<relative/a>' '<relative/b>'
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wedding_album import people, scanner, store  # noqa: E402


def main() -> int:
    if len(sys.argv) < 4:
        print(__doc__)
        return 2

    root = os.path.expanduser(sys.argv[1])
    a_rel, b_rel = sys.argv[2], sys.argv[3]

    cache = store.FaceCache(root)
    if not cache.load():
        print("no face cache; run batch first", file=sys.stderr)
        return 2

    shoots = dict(scanner.find_shoots(root))
    all_photos = [p for ph in shoots.values() for p in ph]
    results = [r for r in (cache.get(p) for p in all_photos) if r]

    found = people.cluster(results)
    people.carry_labels(found, root)

    def who(rel: str):
        folder = os.path.join(root, rel)
        files = set(shoots.get(folder, []))
        if not files:
            print(f"  ! no photos found for {rel}")
            return {}, 0
        hits = {}
        for person in found:
            overlap = files & person.photos
            if overlap:
                hits[person.id] = (len(overlap), person.role, person.name)
        return hits, len(files)

    a_hits, a_n = who(a_rel)
    b_hits, b_n = who(b_rel)

    print(f"\nA: {a_rel}  ({a_n} photos)")
    for pid, (n, role, name) in sorted(a_hits.items(),
                                       key=lambda kv: -kv[1][0])[:12]:
        print(f"    {pid}  in {n:>3}  role={role:<8} {name}")

    print(f"\nB: {b_rel}  ({b_n} photos)")
    for pid, (n, role, name) in sorted(b_hits.items(),
                                       key=lambda kv: -kv[1][0])[:12]:
        print(f"    {pid}  in {n:>3}  role={role:<8} {name}")

    shared = set(a_hits) & set(b_hits)
    only_a = set(a_hits) - set(b_hits)
    only_b = set(b_hits) - set(a_hits)

    print(f"\npeople in both   : {len(shared)}  {sorted(shared)}")
    print(f"only in A        : {len(only_a)}  {sorted(only_a)}")
    print(f"only in B        : {len(only_b)}  {sorted(only_b)}")

    union = set(a_hits) | set(b_hits)
    if union:
        overlap = 100.0 * len(shared) / len(union)
        print(f"\noverlap: {overlap:.0f}% of the people appear in both")
        if overlap >= 50:
            print("  -> likely the same group of people")
        elif shared:
            print("  -> some people in common, but largely different groups")
        else:
            print("  -> no people in common")

    return 0


if __name__ == "__main__":
    sys.exit(main())
