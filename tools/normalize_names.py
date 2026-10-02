"""
Tidy the collection's folder names.

Renames only. No photo is moved between folders, and the shape of the tree is
left alone, because the photographer's grouping is the one piece of information
the tool cannot reconstruct.

What it changes, and why each one is worth changing:

  whitespace   'candid marriage ' and 'pooja ' end in a space, ' A couple'
               begins with one. These are invisible in Finder and break shell
               commands and scripts constantly.

  spellings    Tamil ceremony names normalised to one transliteration each,
               as confirmed by the user: Arundhati Paarthal, Nalangu,
               Pillaiyar Pooja, Thaali Tie.

  consistency  'muralidharan family' and 'B Muralidaran Family' are the same
               family on two different days. Both are spelled Muralidharan;
               they stay as separate folders, since merging them would mix the
               reception into the wedding morning.

  ordering     the two events are prefixed so they sort in the order they
               happened. 'Marriage' currently sorts before 'Reception Eve'
               although the reception was the night before.

Deliberately NOT changed: the existing A.n / B / C / D / E prefixes, which are
the photographer's own convention - A.n for the get-ready sequence, letters for
families - and read perfectly well already.

Dry run by default. Nothing happens without --apply.

    python3 -m tools.normalize_names '<root>'
    python3 -m tools.normalize_names '<root>' --apply
"""

from __future__ import annotations

import os
import sys
from typing import List, Tuple


# (relative path, new basename). Deepest paths are renamed first so a parent
# rename cannot invalidate a child's path mid-run.
RENAMES: List[Tuple[str, str]] = [
    # --- whitespace -----------------------------------------------------
    ("Marriage/pooja ", "Pooja"),
    ("Reception Eve/reception/ A couple", "A couple"),
    ("Reception Eve/A.3Get Ready Couples", "A.3 Get Ready Couple"),

    # --- confirmed Tamil spellings --------------------------------------
    ("Marriage/arinthathi partha", "Arundhati Paarthal"),
    ("Marriage/pillayar pooja", "Pillaiyar Pooja"),
    ("Marriage/thali tie", "Thaali Tie"),
    ("Reception Eve/reception/varaverpu eve/nilangu", "Nalangu"),
    ("Reception Eve/reception/varaverpu eve", "Varaverpu"),

    # --- one spelling for one family ------------------------------------
    ("Marriage/muralidharan family", "Muralidharan Family"),
    ("Reception Eve/reception/B Muralidaran Family", "B Muralidharan Family"),

    # --- capitalisation, for folders that were left lower case ----------
    ("Marriage/get ready rekha", "Get Ready Rekha"),
    ("Marriage/Couple", "Couple"),
    ("Reception Eve/reception/late night shots", "Late Night Shots"),
    ("Reception Eve/reception/proposal and gift", "Proposal and Gift"),
    ("Reception Eve/reception/onStage", "On Stage"),
    ("Reception Eve/reception/Friends and kids", "Friends and Kids"),
    ("Reception Eve/reception/Friends and kids/kids", "Kids"),
    ("Reception Eve/reception/Friends and kids/friends", "Friends"),
    ("Reception Eve/reception/Peoples candids", "Peoples Candids"),
    ("Reception Eve/reception/Z Cake Cutting 1", "Z Cake Cutting 1"),
    ("Reception Eve/reception/Z cake cutting 2", "Z Cake Cutting 2"),
    ("Reception Eve/reception/Z couple unfiltered", "Z Couple Unfiltered"),
    ("Reception Eve/reception/varaverpu eve/Walking to stage", "Walking to Stage"),

    # --- events in the order they happened ------------------------------
    # Renamed last: everything above is addressed by its current path.
    ("Reception Eve", "1_Reception_Eve"),
    ("Marriage", "2_Muhurtham"),
]


def plan(root: str) -> List[Tuple[str, str, str]]:
    """(current path, new path, status) for each intended rename."""
    out = []
    # Deepest first, so children are renamed before their parents.
    for rel, new_name in sorted(RENAMES, key=lambda r: -r[0].count("/")):
        current = os.path.join(root, rel)
        target = os.path.join(os.path.dirname(current), new_name)

        if not os.path.isdir(current):
            status = "missing"
        elif current == target:
            status = "already correct"
        elif _same_entry(current, target):
            # macOS is case-insensitive but case-preserving, so 'kids' and
            # 'Kids' resolve to the same directory. A plain exists() check
            # would refuse every capitalisation fix as a collision, when in
            # fact os.rename handles them fine.
            status = "rename"
        elif os.path.exists(target):
            status = "TARGET EXISTS"
        else:
            status = "rename"
        out.append((current, target, status))
    return out


def _same_entry(a: str, b: str) -> bool:
    """Whether two paths name the same directory on disk."""
    try:
        return os.path.exists(b) and os.path.samefile(a, b)
    except OSError:
        return False


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2

    root = os.path.expanduser(sys.argv[1].rstrip("\n"))
    apply_it = "--apply" in sys.argv

    if not os.path.isdir(root):
        print(f"not a folder: {root!r}", file=sys.stderr)
        return 2

    steps = plan(root)

    print(f"\nroot: {root!r}")
    print(f"{len(steps)} rename(s) considered\n")

    width = max(len(os.path.relpath(c, root)) for c, _, _ in steps) + 2
    todo = 0
    for current, target, status in steps:
        rel = os.path.relpath(current, root)
        if status == "rename":
            todo += 1
            print(f"  {rel:<{width}} -> {os.path.basename(target)}")
        else:
            print(f"  {rel:<{width}}    [{status}]")

    # The root folder itself ends in a space.
    root_target = None
    if os.path.basename(root) != os.path.basename(root).strip():
        root_target = os.path.join(os.path.dirname(root),
                                   os.path.basename(root).strip())
        print(f"\n  the collection folder itself: "
              f"{os.path.basename(root)!r} -> {os.path.basename(root_target)!r}")

    if not apply_it:
        print(f"\nDry run. {todo} rename(s) would be applied.")
        print("Re-run with --apply to perform them.")
        print("\nNote: renaming invalidates the face cache, which is keyed by "
              "file path.\nRe-run the batch afterwards; your person labels "
              "survive, as they are\nmatched by face rather than by path.")
        return 0

    done = 0
    for current, target, status in steps:
        if status != "rename":
            continue
        try:
            os.rename(current, target)
            done += 1
            print(f"  renamed {os.path.relpath(target, root)}")
        except OSError as exc:
            print(f"  FAILED {os.path.relpath(current, root)}: {exc}",
                  file=sys.stderr)

    if root_target and not os.path.exists(root_target):
        try:
            os.rename(root, root_target)
            print(f"  renamed the collection folder to "
                  f"{os.path.basename(root_target)!r}")
        except OSError as exc:
            print(f"  FAILED renaming the collection folder: {exc}",
                  file=sys.stderr)

    print(f"\n{done} folder(s) renamed. No photos were moved.")
    print("Re-run the batch to rebuild the face cache under the new paths.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
