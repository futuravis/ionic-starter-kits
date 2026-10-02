"""
Assigning roles to the people found by clustering.

This is the one stage that needs a human. The tool can work out that the same
face appears in 18 photographs; only you know that face is the bride, and only
you can tell that a confident, perfectly cohesive cluster is a brass idol
rather than a guest.

What you tell it is a *relationship*, not an identity. A finer relation such as
"Sister" or "Mother-in-law" is kept as a note and never changes where a photo
goes: sisters, in-laws and cousins all file under Family, which is how an album
actually reads.

Labels are written into .album_state/people.json alongside each cluster's
centroid, so re-clustering after adding photos carries them forward rather than
making you start again.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from . import config, store


# Roles that may only belong to one person in a collection.
UNIQUE_ROLES = {config.Role.BRIDE.value, config.Role.GROOM.value}


def _key(name: str) -> str:
    """Normalised name, so "Siva" and " siva " are one person."""
    return " ".join(str(name).split()).casefold()


class LabelError(ValueError):
    """A label assignment that cannot be applied."""


# ============================================================================
# PARSING
# ============================================================================

@dataclass
class Assignment:
    person_id: str
    role: str
    name: str = ""
    note: str = ""


def parse_assignment(spec: str) -> Assignment:
    """
    Parse one `--set` value.

        p01=bride
        p01=bride:Rekha
        p04=family:Priya:Sister
        p03=ignore

    Role is required; name and note are optional. A note needs a name before
    it, if only an empty one.
    """
    if "=" not in spec:
        raise LabelError(f"expected pNN=role[:name[:note]], got {spec!r}")

    person_id, _, rest = spec.partition("=")
    person_id = person_id.strip()
    parts = [p.strip() for p in rest.split(":", 2)]

    role = parts[0].lower()
    valid = {r.value for r in config.ASSIGNABLE_ROLES}
    if role not in valid:
        raise LabelError(
            f"unknown role {role!r} for {person_id}; choose from {', '.join(sorted(valid))}"
        )

    return Assignment(
        person_id=person_id,
        role=role,
        name=parts[1] if len(parts) > 1 else "",
        note=parts[2] if len(parts) > 2 else "",
    )


def parse_assignments(specs: Sequence[str]) -> List[Assignment]:
    return [parse_assignment(s) for s in specs]


# ============================================================================
# APPLYING
# ============================================================================

def apply(
    source_dir: str,
    assignments: Sequence[Assignment],
    force: bool = False,
) -> Tuple[dict, List[str]]:
    """
    Write assignments into people.json.

    Operates on the saved clusters rather than re-running detection, so
    labeling is instant and repeatable. Returns the updated blob and a list of
    human-readable changes.

    Raises LabelError if an id is unknown, or if a bride/groom role would be
    given to a second person, unless force is set.
    """
    blob = store.load_people(source_dir)
    if not blob:
        raise LabelError(
            "No people found yet. Run `python3 -m wedding_album people` first."
        )

    by_id: Dict[str, dict] = {p["id"]: p for p in blob.get("people", [])}

    unknown = [a.person_id for a in assignments if a.person_id not in by_id]
    if unknown:
        raise LabelError(
            f"unknown person id(s): {', '.join(unknown)}. "
            f"Known: {', '.join(sorted(by_id))}"
        )

    # Bride and groom must each be one *person* - but a person can be several
    # clusters. The same face splits when it genuinely looks different: the groom
    # in a turban for the ceremony and bare-headed at the reception never share a
    # photograph, so nothing can merge them automatically. Counting distinct
    # names rather than distinct clusters lets you say both are Siva, while still
    # catching the real mistake of handing the role to a second person.
    if not force:
        for role in UNIQUE_ROLES:
            claiming = [a for a in assignments if a.role == role]
            claiming_ids = {a.person_id for a in claiming}

            # Names being claimed now, plus names already holding the role and
            # not being reassigned away from it.
            names = {_key(a.name) for a in claiming if a.name}
            unnamed = [a.person_id for a in claiming if not a.name]

            reassigned = {a.person_id for a in assignments if a.role != role}
            for pid, person in by_id.items():
                if pid in claiming_ids or pid in reassigned:
                    continue
                if person.get("role") != role:
                    continue
                if person.get("name"):
                    names.add(_key(person["name"]))
                else:
                    unnamed.append(pid)

            distinct = len(names) + len(unnamed)
            if distinct > 1:
                who = sorted(names) + unnamed
                raise LabelError(
                    f"{role} would be {distinct} different people "
                    f"({', '.join(who)}). Give every cluster of the same person "
                    f"the same name, or pass --force."
                )

    changes: List[str] = []
    for a in assignments:
        person = by_id[a.person_id]
        before = (person.get("role", ""), person.get("name", ""), person.get("note", ""))

        person["role"] = a.role
        # An empty name or note leaves the previous value alone, so you can
        # change a role without retyping who someone is.
        if a.name:
            person["name"] = a.name
        if a.note:
            person["note"] = a.note

        after = (person["role"], person.get("name", ""), person.get("note", ""))
        if before != after:
            changes.append(
                f"{a.person_id}: {_describe(before)} -> {_describe(after)}"
            )

    store.save_people(source_dir, blob)
    return blob, changes


def _describe(triple: Tuple[str, str, str]) -> str:
    role, name, note = triple
    out = role or "unlabeled"
    if name:
        out += f" ({name})"
    if note:
        out += f" [{note}]"
    return out


def clear(source_dir: str, person_ids: Optional[Sequence[str]] = None) -> int:
    """Reset labels to unlabeled. All people if person_ids is None."""
    blob = store.load_people(source_dir)
    if not blob:
        raise LabelError("No people found yet.")

    targets = set(person_ids) if person_ids else None
    cleared = 0
    for person in blob.get("people", []):
        if targets is not None and person["id"] not in targets:
            continue
        if person.get("role", config.Role.UNKNOWN.value) != config.Role.UNKNOWN.value:
            person["role"] = config.Role.UNKNOWN.value
            person["name"] = ""
            person["note"] = ""
            cleared += 1

    store.save_people(source_dir, blob)
    return cleared


# ============================================================================
# REPORTING
# ============================================================================

def summary(source_dir: str) -> str:
    """Current labeling state, and what still needs attention."""
    blob = store.load_people(source_dir)
    if not blob:
        return "No people found yet. Run `python3 -m wedding_album people` first."

    people = blob.get("people", [])
    if not people:
        return "No people in people.json."

    lines = [f"    {'id':<5} {'role':<8} {'photos':>6}  name / note", ""]
    for p in people:
        role = p.get("role", config.Role.UNKNOWN.value)
        shown = "-" if role == config.Role.UNKNOWN.value else role
        label = p.get("name", "")
        if p.get("note"):
            label = f"{label} [{p['note']}]".strip()
        lines.append(
            f"    {p['id']:<5} {shown:<8} {p.get('photo_count', 0):>6}  {label}"
        )

    unlabeled = [p for p in people if p.get("role", config.Role.UNKNOWN.value)
                 == config.Role.UNKNOWN.value]
    ignored = [p for p in people if p.get("role") == config.Role.IGNORE.value]

    lines.append("")
    lines.append(f"    {len(people) - len(unlabeled)} labeled, {len(unlabeled)} to go"
                 + (f", {len(ignored)} marked not-a-person" if ignored else ""))

    for role in (config.Role.BRIDE.value, config.Role.GROOM.value):
        if not any(p.get("role") == role for p in people):
            lines.append(f"    No {role} assigned yet.")

    return "\n".join(lines)


def who_next(source_dir: str, limit: int = 15) -> str:
    """
    The unidentified people worth naming, most valuable first.

    A wedding throws up a long tail of strangers - half the clusters in the test
    collection appeared in one or two photos - and labelling them all is wasted
    effort. What matters is the handful who recur, because each of those names
    moves a hundred photos out of Unsorted at once. This ranks them so that
    effort goes where it pays.
    """
    blob = store.load_people(source_dir)
    if not blob:
        return "No people found yet. Run the batch first."

    unknown = [p for p in blob.get("people", [])
               if p.get("role", config.Role.UNKNOWN.value) == config.Role.UNKNOWN.value]
    if not unknown:
        return "Everyone has been identified."

    unknown.sort(key=lambda p: -p.get("photo_count", 0))
    common = [p for p in unknown if p.get("photo_count", 0) >= config.UNSORTED_COMMON_MIN_PHOTOS]
    rare = len(unknown) - len(common)

    lines = [
        f"    {len(unknown)} unidentified: {len(common)} recur often, "
        f"{rare} appear in fewer than {config.UNSORTED_COMMON_MIN_PHOTOS} photos",
        "",
        f"    {'id':<6} {'photos':>7} {'cohesion':>9}  worth naming?",
    ]

    for person in unknown[:limit]:
        count = person.get("photo_count", 0)
        cohesion = person.get("cohesion", 0.0)
        if cohesion < 0.55:
            note = "check first - low cohesion, may be two people merged"
        elif count >= config.UNSORTED_COMMON_MIN_PHOTOS:
            note = f"yes - naming them files {count} photos"
        else:
            note = "optional - a passer-by"
        lines.append(f"    {person['id']:<6} {count:>7} {cohesion:>9.2f}  {note}")

    if len(unknown) > limit:
        lines.append(f"    ... and {len(unknown) - limit} more")

    lines.append("")
    lines.append("    Face crops: <collection>/.album_state/people_preview/")
    lines.append("    Label with: python3 -m wedding_album label "
                 "--set pNN=family:Name")
    return "\n".join(lines)


def roles_map(source_dir: str) -> Dict[str, dict]:
    """person id -> its saved record, for the section stage."""
    blob = store.load_people(source_dir)
    if not blob:
        return {}
    return {p["id"]: p for p in blob.get("people", [])}


# ============================================================================
# INTERACTIVE
# ============================================================================

def interactive(source_dir: str) -> int:
    """
    Walk through unlabeled people one at a time, prompting for a role.

    Needs a real terminal, so it is meant to be run by hand after looking at
    .album_state/people_preview/ALL_PEOPLE.jpg.
    """
    blob = store.load_people(source_dir)
    if not blob:
        raise LabelError("No people found yet.")

    choices = {
        "b": config.Role.BRIDE.value,
        "g": config.Role.GROOM.value,
        "f": config.Role.FAMILY.value,
        "r": config.Role.FRIEND.value,
        "k": config.Role.KID.value,
        "x": config.Role.IGNORE.value,
    }

    print("For each person, look at .album_state/people_preview/<id>_sheet.jpg")
    print("  [b]ride  [g]room  [f]amily  f[r]iend  [k]id  "
          "[x] not a person  [s]kip  [q]uit\n")

    changed = 0
    for person in blob.get("people", []):
        if person.get("role", config.Role.UNKNOWN.value) != config.Role.UNKNOWN.value:
            continue

        print(f"{person['id']}: seen in {person.get('photo_count', 0)} photo(s), "
              f"cohesion {person.get('cohesion', 0):.3f}")

        answer = input("    role> ").strip().lower()
        if answer == "q":
            break
        if answer in ("", "s"):
            continue
        if answer not in choices:
            print(f"    '{answer}' is not a choice; skipping")
            continue

        person["role"] = choices[answer]
        if person["role"] != config.Role.IGNORE.value:
            person["name"] = input("    name> ").strip() or person.get("name", "")
            note = input("    relation note (optional)> ").strip()
            if note:
                person["note"] = note
        changed += 1

    store.save_people(source_dir, blob)
    return changed
