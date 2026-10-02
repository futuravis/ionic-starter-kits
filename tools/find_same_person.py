"""
Which unnamed clusters are probably someone you have already named.

One person can form several clusters when they genuinely look different, and no
similarity threshold can join them: the groom in a turban for the ceremony and
bare-headed at the reception never appear in the same photograph, so there is
nothing to compare. He showed up as two clusters here, and the bride may well
have done the same - her bridal look is not her getting-ready look.

This ranks every unnamed cluster against each named person, so the near misses
are visible instead of being left in Unsorted. It also writes a side-by-side
picture for each candidate, because this is a judgement to make by eye.

    python3 -m tools.find_same_person '<root>'
"""

from __future__ import annotations

import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wedding_album import config, store  # noqa: E402


# Below the clustering threshold, but close enough to be worth a look.
FLOOR = 0.14


def main() -> int:
    root = os.path.expanduser(
        sys.argv[1] if len(sys.argv) > 1 else "~/Desktop/candid marriage")

    blob = store.load_people(root)
    if not blob:
        print("no people.json; run the batch first", file=sys.stderr)
        return 2

    people = blob.get("people", [])
    named = [p for p in people
             if p.get("role", "unknown") not in ("unknown", "ignore")
             and p.get("centroid")]
    unnamed = [p for p in people
               if p.get("role", "unknown") == "unknown" and p.get("centroid")]

    if not named:
        print("nobody is named yet")
        return 1

    print(f"\n{len(named)} named, {len(unnamed)} unnamed")
    print(f"threshold for automatic merging is "
          f"{config.FACE_MATCH_THRESHOLD}; anything above {FLOOR} is worth "
          f"checking by eye\n")

    preview_dir = os.path.join(root, config.STATE_DIR_NAME, "same_person_checks")
    os.makedirs(preview_dir, exist_ok=True)

    any_found = False

    for person in named:
        centroid = np.asarray(person["centroid"], dtype=np.float32)
        scored = []
        for other in unnamed:
            similarity = float(np.dot(centroid,
                                      np.asarray(other["centroid"],
                                                 dtype=np.float32)))
            if similarity >= FLOOR:
                scored.append((similarity, other))

        label = f"{person['id']} {person.get('name') or ''} " \
                f"({person.get('role')}, {person.get('photo_count')} photos)"
        if not scored:
            print(f"{label}: no near matches - looks like one cluster covers them")
            continue

        any_found = True
        scored.sort(key=lambda pair: -pair[0])
        print(f"{label}:")
        for similarity, other in scored[:6]:
            verdict = "very likely the same" if similarity >= 0.22 else "possible"
            print(f"    {other['id']:<6} {other.get('photo_count'):>4} photos  "
                  f"similarity {similarity:.3f}  {verdict}")
            _write_pair(root, preview_dir, person, other, similarity)

    if any_found:
        print(f"\nSide-by-side pictures: {preview_dir}")
        print("If two are the same person, give both crops the same name in")
        print("Name_These_People and run:  names --apply")
    return 0


def _write_pair(root: str, out_dir: str, a: dict, b: dict,
                similarity: float) -> None:
    """A single image showing both faces, for a judgement by eye."""
    import cv2

    base = os.path.join(root, config.STATE_DIR_NAME, "people_preview")
    tiles = []
    for person in (a, b):
        folder = os.path.join(base, person["id"])
        if not os.path.isdir(folder):
            return
        files = sorted(f for f in os.listdir(folder) if f.lower().endswith(".jpg"))
        if not files:
            return
        img = cv2.imread(os.path.join(folder, files[0]))
        if img is None:
            return
        img = cv2.resize(img, (220, 220))
        caption = f"{person['id']} {person.get('name') or ''}".strip()
        cv2.rectangle(img, (0, 196), (220, 220), (0, 0, 0), -1)
        cv2.putText(img, caption, (5, 213), cv2.FONT_HERSHEY_SIMPLEX, 0.5,
                    (255, 255, 255), 1, cv2.LINE_AA)
        tiles.append(img)

    name = f"{similarity:.3f}_{a['id']}_vs_{b['id']}.jpg"
    cv2.imwrite(os.path.join(out_dir, name), np.hstack(tiles))


if __name__ == "__main__":
    sys.exit(main())
