"""
Naming people by renaming photographs of them.

Typing `label --set p05=family:Priya` means holding a cluster id in your head
while looking at a face. This turns it around: the tool writes out one portrait
per unidentified person, and you name them the way you would name anything else
on your computer - drag the picture into a folder, type the person's name.

    <collection>/Name_These_People/
        _Unidentified/      every unnamed face starts here
        01_Bride/           drag the bride's face in
        02_Groom/
        03_Family/
        04_Friends/
        05_Kids/
        06_Not_A_Person/    statues, posters, a wristwatch
        READ_ME.txt

Drag `p05_in_186_photos.jpg` into `03_Family` and rename it `Priya.jpg`, or
`Priya - Sister.jpg` to keep the relation as a note. Then:

    python3 -m wedding_album names --apply

The file you renamed is matched back to its cluster by content, not by filename,
so you are free to call it anything. Matching tries the file's checksum first
and falls back to recognising the face in it, which means the label survives
even if you crop or re-save the image on the way through.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

import cv2
import numpy as np

from . import config, labels, preview, store
from .config import Role
from .people import Person


NAMING_DIR = "Name_These_People"
UNIDENTIFIED = "_Unidentified"
MANIFEST = "_manifest.json"

# Folder name -> the role it assigns. Numbered so they sort sensibly in Finder.
ROLE_DIRS: List[Tuple[str, Role]] = [
    ("01_Bride", Role.BRIDE),
    ("02_Groom", Role.GROOM),
    ("03_Family", Role.FAMILY),
    ("04_Friends", Role.FRIEND),
    ("05_Kids", Role.KID),
    ("06_Not_A_Person", Role.IGNORE),
]

READ_ME = """HOW TO NAME PEOPLE
==================

Everyone the tool found but cannot name is in _Unidentified. The filename says
how many photos each face appears in, so the ones at the top of the list are
worth your time first - naming a face that appears in 200 photos files all 200.

To name someone:

  1. Look at the picture in _Unidentified.
  2. Drag it into the folder for their relationship to the wedding:
       01_Bride, 02_Groom, 03_Family, 04_Friends, 05_Kids
     Sisters, in-laws, cousins and parents all go in 03_Family. What matters is
     the relationship, not the exact relation.
  3. Rename it to their name, for example  Priya.jpg
     To keep the relation as a note as well:  Priya - Sister.jpg

Faces that are not people - a statue, a poster, a wristwatch the camera focused
on - go in 06_Not_A_Person. You do not need to rename those.

Leave anyone you do not recognise where they are. Unnamed faces do no harm; they
simply keep their photos in the Unsorted section.

When you are done:

    python3 -m wedding_album names --apply --source '<this collection>'

You can rename the files however you like. They are matched back by what the
picture contains, not by what it is called.
"""


# ============================================================================
# EXPORT
# ============================================================================

@dataclass
class ExportResult:
    directory: str
    written: int = 0
    skipped: int = 0


def naming_dir(root: str) -> str:
    return os.path.join(root, NAMING_DIR)


def export_faces(
    root: str,
    people: Sequence[Person],
    results: Sequence,
    only_unlabeled: bool = True,
    clean: bool = False,
) -> ExportResult:
    """
    Write one portrait per person into _Unidentified, plus the empty role folders.

    A manifest records which cluster each file came from, keyed by the file's
    checksum, so a renamed file can be traced back without relying on its name.
    """
    import cv2

    base = naming_dir(root)
    unidentified = os.path.join(base, UNIDENTIFIED)

    if clean and os.path.isdir(base):
        shutil.rmtree(base)

    os.makedirs(unidentified, exist_ok=True)
    for folder, _ in ROLE_DIRS:
        os.makedirs(os.path.join(base, folder), exist_ok=True)

    with open(os.path.join(base, "READ_ME.txt"), "w", encoding="utf-8") as fh:
        fh.write(READ_ME)

    preview.register_boxes(results)

    manifest: Dict[str, dict] = {}
    out = ExportResult(directory=base)

    # Most-photographed first, so the valuable faces sort to the top.
    ordered = sorted(people, key=lambda p: -p.photo_count)

    for person in ordered:
        if only_unlabeled and person.is_labeled:
            out.skipped += 1
            continue

        crop = _best_crop(person)
        if crop is None:
            out.skipped += 1
            continue

        name = f"{person.id}_in_{person.photo_count}_photos.jpg"
        path = os.path.join(unidentified, name)
        if not cv2.imwrite(path, crop, [int(cv2.IMWRITE_JPEG_QUALITY), 92]):
            out.skipped += 1
            continue

        manifest[_checksum(path)] = {
            "person": person.id,
            "photos": person.photo_count,
            "centroid": [round(float(v), 6) for v in person.centroid],
        }
        out.written += 1

    with open(os.path.join(base, MANIFEST), "w", encoding="utf-8") as fh:
        json.dump({"schema": 1, "files": manifest}, fh, indent=1)

    return out


def _best_crop(person: Person):
    """The clearest face this person has, cropped with a little margin."""
    import cv2

    for ref in sorted(person.refs, key=lambda r: r.clarity, reverse=True)[:5]:
        img = preview._load_detect_space(ref.filepath)
        if img is None:
            continue
        box = preview._box_for(ref.filepath, ref.face_index)
        if box is None:
            continue
        patch = preview.crop_face(img, box, margin=0.7)
        if patch is None:
            continue
        return cv2.resize(patch, (400, 400), interpolation=cv2.INTER_AREA)
    return None


def _checksum(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ============================================================================
# READ BACK
# ============================================================================

@dataclass
class NameEntry:
    person_id: str
    role: str
    name: str
    note: str
    source_file: str
    matched_by: str


_COPY_SUFFIX = re.compile(r"[\s_-]*(?:\(\d+\)|\d+|copy)\s*$", re.IGNORECASE)


def parse_filename(filename: str) -> Tuple[str, str]:
    """
    Split a filename into a person's name and an optional relation note.

        Priya.jpg            -> ("Priya", "")
        Priya - Sister.jpg   -> ("Priya", "Sister")
        Siva 2.jpg           -> ("Siva", "")

    The trailing number is stripped on purpose, and it is what makes one person
    out of several clusters. The same face can form more than one cluster when it
    genuinely looks different - the groom in a turban during the ceremony and
    bare-headed at the reception never appear in the same photograph, so no
    similarity threshold can join them. Naming both crops "Siva", which a folder
    forces you to write as Siva.jpg and Siva 2.jpg, tells the tool they are the
    same man.
    """
    stem = os.path.splitext(os.path.basename(filename))[0].strip()

    note = ""
    for separator in (" - ", " -- ", " | "):
        if separator in stem:
            stem, _, note = stem.partition(separator)
            stem, note = stem.strip(), note.strip()
            break

    cleaned = _COPY_SUFFIX.sub("", stem).strip()
    # Keep the original if stripping consumed everything, e.g. a file called "2".
    return (cleaned or stem), note


def collect(root: str) -> Tuple[List[NameEntry], List[str]]:
    """
    Read the role folders and work out who each file is.

    Returns the entries found and a list of problems worth reporting.
    """
    base = naming_dir(root)
    problems: List[str] = []

    if not os.path.isdir(base):
        return [], [f"{NAMING_DIR} does not exist yet; run names --export first"]

    manifest = {}
    manifest_path = os.path.join(base, MANIFEST)
    if os.path.isfile(manifest_path):
        try:
            with open(manifest_path, encoding="utf-8") as fh:
                manifest = json.load(fh).get("files", {})
        except (OSError, ValueError):
            problems.append("manifest unreadable; falling back to face matching")

    entries: List[NameEntry] = []
    seen: Dict[str, str] = {}

    for folder, role in ROLE_DIRS:
        folder_path = os.path.join(base, folder)
        if not os.path.isdir(folder_path):
            continue

        for filename in sorted(os.listdir(folder_path)):
            if filename.startswith(".") or not _is_image(filename):
                continue
            path = os.path.join(folder_path, filename)

            person_id, how = _identify(path, manifest, root)
            if not person_id:
                problems.append(
                    f"{folder}/{filename}: could not tell who this is "
                    f"(no checksum match and no recognisable face)"
                )
                continue

            if person_id in seen:
                problems.append(
                    f"{folder}/{filename}: {person_id} was already named in "
                    f"{seen[person_id]}; ignoring the second one"
                )
                continue
            seen[person_id] = f"{folder}/{filename}"

            name, note = parse_filename(filename)

            # An unedited export still carries its pNN_in_N_photos name, which
            # is a filename rather than a person.
            if name.startswith(person_id + "_in_") or name == person_id:
                name = ""

            # A statue or a wristwatch has no name, and the README tells you not
            # to bother renaming these. Whatever the file happens to be called
            # is not a person's name, so it is discarded rather than stored.
            if role is Role.IGNORE:
                name, note = "", ""

            entries.append(NameEntry(
                person_id=person_id, role=role.value, name=name, note=note,
                source_file=f"{folder}/{filename}", matched_by=how,
            ))

    return entries, problems


def _is_image(filename: str) -> bool:
    return os.path.splitext(filename)[1].lower() in config.IMAGE_EXTENSIONS


def _identify(path: str, manifest: dict, root: str) -> Tuple[str, str]:
    """
    Which cluster this picture shows.

    Checksum first, since an untouched file matches exactly. If the file was
    edited or re-saved, recognise the face in it instead and match that against
    the stored centroids - which is why renaming can be completely free-form.
    """
    entry = manifest.get(_checksum(path))
    if entry:
        return entry.get("person", ""), "checksum"

    return _identify_by_face(path, manifest), "face"


def _identify_by_face(path: str, manifest: dict) -> str:
    from .faces import FaceAnalyzer

    if not manifest:
        return ""

    try:
        analyzer = _analyzer()
        result = analyzer.analyze(path)
    except Exception:  # noqa: BLE001
        return ""

    if not result.ok or not result.faces:
        return ""

    # Use the most central face, and only that one.
    #
    # The crop is cut with a generous margin so a human can recognise who they
    # are looking at, and that margin frequently catches a neighbour who may be
    # the *larger* of the two. Taking the biggest face therefore picks the wrong
    # person, and taking the best match across all of them is worse still: it
    # confidently returns the neighbour whenever the neighbour matches more
    # strongly, which silently labels the wrong guest.
    #
    # The crop is centred on its subject by construction, so proximity to the
    # centre identifies them reliably where size and similarity do not.
    image = cv2.imread(path)
    if image is None:
        return ""
    height, width = image.shape[:2]
    scale = config.FACE_DETECT_MAX_DIM / max(height, width)
    centre = (width * min(scale, 1.0) / 2.0, height * min(scale, 1.0) / 2.0)

    def distance_from_centre(face) -> float:
        fx = face.x + face.w / 2.0
        fy = face.y + face.h / 2.0
        return ((fx - centre[0]) ** 2 + (fy - centre[1]) ** 2) ** 0.5

    subject = min(result.faces, key=distance_from_centre)
    if subject.embedding is None:
        return ""

    best_id, best_score = "", config.FACE_MATCH_THRESHOLD
    for entry in manifest.values():
        centroid = entry.get("centroid")
        if not centroid:
            continue
        score = float(np.dot(subject.embedding,
                             np.asarray(centroid, dtype=np.float32)))
        if score >= best_score:
            best_id, best_score = entry.get("person", ""), score

    return best_id


_ANALYZER = None


def _analyzer():
    global _ANALYZER
    if _ANALYZER is None:
        from .faces import FaceAnalyzer
        _ANALYZER = FaceAnalyzer()
    return _ANALYZER


# ============================================================================
# APPLY
# ============================================================================

def apply(root: str, entries: Sequence[NameEntry],
          force: bool = False) -> Tuple[List[str], List[str]]:
    """Turn the entries into labels. Returns (changes, errors)."""
    if not entries:
        return [], []

    assignments = [
        labels.Assignment(person_id=e.person_id, role=e.role,
                          name=e.name, note=e.note)
        for e in entries
    ]

    try:
        _, changes = labels.apply(root, assignments, force=force)
        return changes, []
    except labels.LabelError as exc:
        return [], [str(exc)]


def summarize(entries: Sequence[NameEntry], problems: Sequence[str]) -> str:
    lines = []
    if entries:
        lines.append(f"    {'person':<7} {'role':<8} {'matched':<9} name")
        for e in entries:
            label = e.name or "(no name given)"
            if e.note:
                label += f"  [{e.note}]"
            lines.append(f"    {e.person_id:<7} {e.role:<8} {e.matched_by:<9} {label}")
    else:
        lines.append("    Nothing named yet. Drag faces from _Unidentified into a "
                     "role folder.")

    if problems:
        lines.append("")
        for problem in problems:
            lines.append(f"    ! {problem}")

    return "\n".join(lines)
