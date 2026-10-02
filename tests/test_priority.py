"""
Priority-rule checks.

The test collection (A.4 Get Ready Outdoor) only contains the bride, the groom
and a statue, so it exercises Bride, Groom and Couple and nothing else. The
rules that actually encode the spec's intent - family outranking the couple,
a child having to be in focus, a statue counting for nothing - need cases
built by hand.

Run with:
    python3 -m tests.test_priority
"""

from __future__ import annotations

import sys

import numpy as np

from wedding_album import config
from wedding_album.config import Role, Section
from wedding_album.people import FaceRef, Person


PHOTO = "/collection/IMG_0001.JPG"

# Above and below config.FACE_PROMINENT_SIZE_RATIO (0.08).
BIG = 0.20
SMALL = 0.03


def person(pid: str, role: Role, name: str = "", size: float = BIG,
           photo: str = PHOTO) -> Person:
    """A person present in one photo at a given face size."""
    vec = np.zeros(128, dtype=np.float32)
    vec[hash(pid) % 128] = 1.0
    return Person(
        id=pid,
        refs=[FaceRef(filepath=photo, face_index=0, embedding=vec,
                      size_ratio=size, score=0.95)],
        name=name,
        role=role.value,
    )


def check(label: str, people, expected: Section, faces_detected: int = 0) -> bool:
    from wedding_album.sections import decide

    ratios = {PHOTO: [p.refs[0].size_ratio for p in people]}
    section, reason, _ = decide(
        PHOTO, people,
        face_count=faces_detected or len(people),
        size_ratios=ratios,
    )
    ok = section == expected
    mark = "pass" if ok else "FAIL"
    print(f"  [{mark}] {label}")
    print(f"         -> {section.value}  ({reason})")
    if not ok:
        print(f"         expected {expected.value}")
    return ok


def main() -> int:
    bride = lambda **kw: person("p01", Role.BRIDE, "Rekha", **kw)      # noqa: E731
    groom = lambda **kw: person("p02", Role.GROOM, "Siva", **kw)       # noqa: E731
    mother = lambda **kw: person("p03", Role.FAMILY, "Amma", **kw)     # noqa: E731
    sister = lambda **kw: person("p04", Role.FAMILY, "Sister", **kw)   # noqa: E731
    friend = lambda **kw: person("p05", Role.FRIEND, "Arun", **kw)     # noqa: E731
    kid = lambda **kw: person("p06", Role.KID, "Little one", **kw)     # noqa: E731
    idol = lambda **kw: person("p07", Role.IGNORE, "", **kw)           # noqa: E731
    stranger = lambda i=8, **kw: person(f"p{i:02d}", Role.UNKNOWN, "", **kw)  # noqa: E731

    results = []

    print("\nsolo and couple")
    results.append(check("bride alone", [bride()], Section.BRIDE))
    results.append(check("groom alone", [groom()], Section.GROOM))
    results.append(check("bride + groom", [bride(), groom()], Section.COUPLE))

    print("\nthe spec's worked example: family outranks couple")
    results.append(check("bride + groom + mother -> Family",
                         [bride(), groom(), mother()], Section.FAMILY))
    results.append(check("bride + mother -> Family",
                         [bride(), mother()], Section.FAMILY))
    results.append(check("relation note does not create its own section",
                         [bride(), sister()], Section.FAMILY))

    print("\nfriends")
    results.append(check("bride + groom + friend -> Friends",
                         [bride(), groom(), friend()], Section.FRIENDS))
    results.append(check("family outranks friends",
                         [mother(), friend()], Section.FAMILY))

    print("\nkids, which require the child to be in focus")
    results.append(check("child prominent -> Kids",
                         [bride(), kid(size=BIG)], Section.KIDS))
    results.append(check("child tiny in a family shot -> Family, not Kids",
                         [mother(), kid(size=SMALL)], Section.FAMILY))
    results.append(check("child tiny with couple -> Couple, not Kids",
                         [bride(), groom(), kid(size=SMALL)], Section.COUPLE))

    print("\nignored clusters contribute nothing")
    results.append(check("couple + idol -> Couple, not Group",
                         [bride(), groom(), idol()], Section.COUPLE))
    results.append(check("bride + idol -> Bride",
                         [bride(), idol()], Section.BRIDE))
    results.append(check("two strangers + idol -> Unsorted, idol ignored",
                         [stranger(8), stranger(9), idol()], Section.UNSORTED))
    # An idol on its own is a photograph of a thing. It goes to the no-face bin
    # rather than Unsorted, which is reserved for real people not yet named.
    results.append(check("idol alone -> no face found",
                         [idol()], Section.NO_FACE, faces_detected=1))

    print("\nunlabeled people")
    results.append(check("three strangers -> Group",
                         [stranger(8), stranger(9), stranger(10)], Section.GROUP))
    results.append(check("two strangers -> Unsorted (too few for a group)",
                         [stranger(8), stranger(9)], Section.UNSORTED))
    results.append(check("bride + stranger -> Bride (named role wins)",
                         [bride(), stranger(9)], Section.BRIDE))
    # No face found at all. Honestly named: this bin holds both genuine object
    # and detail shots and people photographed from behind or in profile, and
    # a face detector cannot tell those apart.
    results.append(check("no face found anywhere -> no face bin",
                         [], Section.NO_FACE, faces_detected=0))

    passed = sum(results)
    total = len(results)
    print(f"\n{passed}/{total} passed")

    if passed != total:
        return 1

    # The priority list and the section folders must stay in step.
    config.validate()
    print("config.validate() ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
