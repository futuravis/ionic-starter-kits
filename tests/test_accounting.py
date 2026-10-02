"""
Accounting check on the real collection.

The one property that must never break: every photo ends up in exactly one
bucket, and no photo disappears. Buckets are now visible folders, so a silent
loss would mean a photo the user can never find again in any of them.

    python3 -m tests.test_accounting
"""

from __future__ import annotations

import os
import sys
from collections import Counter

from wedding_album import (
    batch, config, people, pipeline, quality, scanner, store,
)

ROOT = os.path.expanduser("~/Desktop/candid marriage")


def main() -> int:
    checks = []

    def check(ok, label, detail=""):
        checks.append(ok)
        print(f"  [{'pass' if ok else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))

    shoots = scanner.find_shoots(ROOT)
    all_photos = [p for _, ph in shoots for p in ph]
    print(f"\n{len(shoots)} shoots, {len(all_photos)} photos")

    cache = store.FaceCache(ROOT)
    if not cache.load():
        print("  no face cache; run batch first", file=sys.stderr)
        return 2

    results = [r for r in (cache.get(p) for p in all_photos) if r]
    check(len(results) == len(all_photos),
          f"all {len(all_photos)} photos served from cache", str(len(results)))

    found = people.cluster(results)
    people.carry_labels(found, ROOT)

    metrics = pipeline.analyze_quality(all_photos, workers=pipeline.default_workers())
    scores = quality.score_collection(metrics)
    phashes = {m.filepath: m.phash for m in metrics if m.ok}

    shoot_results = batch.prepare_shoots(
        shoots, results, found, scores, phashes, percent=10)

    buckets = Counter()
    seen = Counter()
    for s in shoot_results:
        for a in s.assignments:
            buckets[a.bucket] += 1
            seen[a.filepath] += 1

    print("\nbuckets")
    for name, n in sorted(buckets.items(), key=lambda kv: -kv[1]):
        print(f"    {name:<12} {n:>5}")
    print(f"    {'TOTAL':<12} {sum(buckets.values()):>5}")

    print("\naccounting")
    check(sum(buckets.values()) == len(all_photos),
          f"assignments equal photos ({sum(buckets.values())} vs {len(all_photos)})")

    twice = [p for p, n in seen.items() if n > 1]
    check(not twice, "no photo assigned twice", f"{len(twice)} offenders")

    missing = set(all_photos) - set(seen)
    check(not missing, "no photo unaccounted for", f"{len(missing)} missing")

    print("\nbucket rules")
    for s in shoot_results:
        for a in s.assignments:
            if a.is_pick and (a.is_duplicate or a.is_low_quality):
                check(False, "a pick was also duplicate/low quality",
                      os.path.basename(a.filepath))
                break
        else:
            continue
        break
    else:
        check(True, "no pick is also a duplicate or low quality")

    objects = [a for s in shoot_results for a in s.assignments
               if a.section is config.Section.NO_FACE]
    check(all(not a.is_pick for a in objects),
          f"{len(objects)} object shots, none competing for a pick")
    check(all(a.people_count == 0 for a in objects),
          "object shots contain no real people")

    print("\ntotals match the batch summary")
    result = batch.BatchResult(root=ROOT, shoots=shoot_results, people=found)
    check(result.total_duplicates == buckets["duplicate"],
          f"duplicates {result.total_duplicates}")
    check(result.total_low_quality == buckets["low_quality"],
          f"low quality {result.total_low_quality}")
    check(result.total_objects == buckets["no_face"],
          f"objects {result.total_objects}")
    check(result.total_picks == buckets["pick"], f"picks {result.total_picks}")

    passed, total = sum(checks), len(checks)
    print(f"\n{passed}/{total} passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
