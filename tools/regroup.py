"""
Introduce a middle level: event -> phase -> shoot.

The collection currently has two levels, and the second one is doing two
different jobs at once. Under 1_Reception_Eve/reception sit fifteen folders that
mix three unrelated kinds of thing: ceremonies that happened at a moment
(Varaverpu, Cake Cutting), family groupings photographed on request
(B Muralidharan, D Raja), and candids that roam across the whole evening
(Peoples Candids, Late Night Shots). Flat, they are impossible to read in order.

A phase level separates them:

    1_Reception_Eve/
        1_Getting_Ready/     A.1 Rekha, A.2 Siva, A.3 Couple, A.4 Outdoor
        2_Couple_Portraits/  Z Couple Unfiltered, A couple
        3_Varaverpu/         Varaverpu, and its Nalangu / Walking to Stage
        4_On_Stage/          On Stage
        5_Cake_Cutting/      Z Cake Cutting 1, Z Cake Cutting 2
        6_Proposal_and_Gift/
        7_Families/          B / C / D / E, Family Photos
        8_Friends_and_Kids/
        9_Candids/           Peoples Candids, Late Night Shots

Getting_Ready deliberately keeps all four A.n folders together, including
"Outdoor". The shoot moved outside only because the power failed; it is one
session, not two, and splitting it across phases would misrepresent the evening.

Phases are numbered by when they happened, from EXIF, so the folders read in
order. The `reception` wrapper disappears - it grouped nothing, since everything
inside it was the reception.

Moves whole folders. No photo is moved between folders, nothing is renamed
beyond its parent, and albums stay per shoot because a shoot is still any folder
holding photos.

Dry run by default.

    python3 -m tools.regroup '<root>'
    python3 -m tools.regroup '<root>' --apply
"""

from __future__ import annotations

import os
import shutil
import sys
from typing import Dict, List, Tuple


# event -> phase -> the shoot folders that belong in it, given as paths
# relative to the event folder.
PLAN: Dict[str, List[Tuple[str, List[str]]]] = {
    "1_Reception_Eve": [
        ("1_Getting_Ready", [
            "A.1 Get Ready Rekha",
            "A.2 Get Ready Siva",
            "A.3 Get Ready Couple",
            "A.4 Get Ready Outdoor",
        ]),
        ("2_Couple_Portraits", [
            "reception/Z Couple Unfiltered",
            "reception/A couple",
        ]),
        ("3_Varaverpu", [
            "reception/Varaverpu",
        ]),
        ("4_On_Stage", [
            "reception/On Stage",
        ]),
        ("5_Cake_Cutting", [
            "reception/Z Cake Cutting 1",
            "reception/Z Cake Cutting 2",
        ]),
        ("6_Proposal_and_Gift", [
            "reception/Proposal and Gift",
        ]),
        ("7_Families", [
            "reception/B Muralidharan Family",
            "reception/C Velmurugan Family",
            "reception/D Raja Family",
            "reception/E Partha Family",
            "reception/Family Photos",
        ]),
        ("8_Friends_and_Kids", [
            "reception/Friends and Kids",
        ]),
        ("9_Candids", [
            "reception/Peoples Candids",
            "reception/Late Night Shots",
        ]),
    ],
    "2_Muhurtham": [
        ("1_Getting_Ready", [
            "Get Ready Rekha",
        ]),
        ("2_Couple_Portraits", [
            "Couple",
        ]),
        ("3_Ceremonies", [
            "Ceremony",
            "Pillaiyar Pooja",
            "Pooja",
            "Arundhati Paarthal",
            "Thaali Tie",
        ]),
        ("4_Families", [
            "Muralidharan Family",
            "Raja Family",
        ]),
        ("5_Candids", [
            "Peoples",
        ]),
    ],
}


def plan_moves(root: str) -> List[Tuple[str, str, str]]:
    """(source, destination, status) for every folder move."""
    out: List[Tuple[str, str, str]] = []

    for event, phases in PLAN.items():
        event_dir = os.path.join(root, event)
        if not os.path.isdir(event_dir):
            out.append((event_dir, "", "event missing"))
            continue

        for phase, shoots in phases:
            phase_dir = os.path.join(event_dir, phase)
            for rel in shoots:
                src = os.path.join(event_dir, rel)
                dst = os.path.join(phase_dir, os.path.basename(rel))

                if not os.path.isdir(src):
                    status = "missing"
                elif os.path.exists(dst):
                    status = "TARGET EXISTS"
                else:
                    status = "move"
                out.append((src, dst, status))

    return out


# Photos found loose in an event folder, and the phase they belong to.
#
# 7Y4A2145.JPG was taken at 18:16, one minute after A.4 Get Ready Outdoor ends
# at 18:15 and seven before the couple portraits begin. It is the tail of the
# getting-ready session. Left where it is, the event folder itself would count
# as a shoot and grow its own one-photo album at the top level.
LOOSE_PHOTO_PHASE: Dict[str, str] = {
    "1_Reception_Eve": "1_Getting_Ready/A.4 Get Ready Outdoor",
}


def loose_photos(root: str) -> List[str]:
    """Photos sitting directly in an event folder, with no shoot of their own."""
    from wedding_album import config

    found = []
    for event in PLAN:
        event_dir = os.path.join(root, event)
        if not os.path.isdir(event_dir):
            continue
        for name in sorted(os.listdir(event_dir)):
            path = os.path.join(event_dir, name)
            if os.path.isfile(path) and not name.startswith("."):
                if os.path.splitext(name)[1].lower() in config.IMAGE_EXTENSIONS:
                    found.append(path)
    return found


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    root = os.path.expanduser(sys.argv[1])
    apply_it = "--apply" in sys.argv

    if not os.path.isdir(root):
        print(f"not a folder: {root!r}", file=sys.stderr)
        return 2

    moves = plan_moves(root)
    todo = [m for m in moves if m[2] == "move"]

    print(f"\nroot: {root}")
    print(f"{len(moves)} folder(s) considered, {len(todo)} to move\n")

    current_phase = None
    for src, dst, status in moves:
        phase = os.path.basename(os.path.dirname(dst)) if dst else "?"
        event = os.path.basename(os.path.dirname(os.path.dirname(dst))) if dst else "?"
        key = f"{event}/{phase}"
        if key != current_phase:
            current_phase = key
            print(f"  {key}")
        label = os.path.relpath(src, root)
        if status == "move":
            print(f"      {label}")
        else:
            print(f"      {label}   [{status}]")

    stray = loose_photos(root)
    if stray:
        print(f"\n  {len(stray)} photo(s) loose in an event folder, to be filed "
              f"by capture time:")
        for path in stray:
            event = os.path.basename(os.path.dirname(path))
            target = LOOSE_PHOTO_PHASE.get(event)
            where = target if target else "left in place (no rule)"
            print(f"      {os.path.relpath(path, root)}  ->  {where}")

    if not apply_it:
        print(f"\nDry run. {len(todo)} folder(s) would move.")
        print("Re-run with --apply to perform the moves.")
        print("\nAfterwards: albums are keyed by path, so re-run the batch. "
              "Person labels\nsurvive - they are matched by face.")
        return 0

    done = 0
    for src, dst, status in moves:
        if status != "move":
            continue
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        try:
            shutil.move(src, dst)
            done += 1
            print(f"  moved {os.path.relpath(dst, root)}")
        except OSError as exc:
            print(f"  FAILED {os.path.relpath(src, root)}: {exc}", file=sys.stderr)

    # File the loose photos, now that their destination phase exists.
    for path in loose_photos(root):
        event = os.path.basename(os.path.dirname(path))
        target_rel = LOOSE_PHOTO_PHASE.get(event)
        if not target_rel:
            continue
        target_dir = os.path.join(root, event, target_rel)
        if not os.path.isdir(target_dir):
            print(f"  skipped {os.path.basename(path)}: "
                  f"{target_rel} does not exist", file=sys.stderr)
            continue
        try:
            shutil.move(path, os.path.join(target_dir, os.path.basename(path)))
            print(f"  filed {os.path.basename(path)} into {target_rel}")
        except OSError as exc:
            print(f"  FAILED {os.path.basename(path)}: {exc}", file=sys.stderr)

    # The reception wrapper should now be empty of shoots.
    for event in PLAN:
        wrapper = os.path.join(root, event, "reception")
        if os.path.isdir(wrapper):
            leftovers = [e for e in os.listdir(wrapper) if not e.startswith(".")]
            if leftovers:
                print(f"\n  {os.path.relpath(wrapper, root)} still holds: "
                      f"{', '.join(leftovers)}")
            else:
                ds = os.path.join(wrapper, ".DS_Store")
                if os.path.isfile(ds):
                    os.remove(ds)
                os.rmdir(wrapper)
                print(f"  removed the now-empty "
                      f"{os.path.relpath(wrapper, root)} wrapper")

    print(f"\n{done} folder(s) moved. No photos were moved between folders.")
    print("Re-run the batch to rebuild albums under the new paths.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
