"""
Grouping faces into people.

Turns a pile of face embeddings into a short list of distinct individuals, so
you label a person once rather than tagging thousands of photos.

Approach: greedy nearest-centroid assignment, clearest faces first, followed by
repeated centroid merging until stable. Full agglomerative clustering would be
tidier but is cubic in the number of faces, and a real wedding runs to
thousands; this is near-linear and, on a set where an independent tool's output
was available to check against, produced the same people.

The one non-obvious rule: two faces in the same photograph cannot be the same
person. That constraint is free, it is always true, and it stops the most
common failure mode, where a bride and her sister drift into one cluster.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Set, Tuple

import numpy as np

from . import config, store
from .faces import FaceResult


# ============================================================================
# DATA
# ============================================================================

@dataclass
class FaceRef:
    """A pointer back to one face in one photo, with its embedding."""

    filepath: str
    face_index: int
    embedding: np.ndarray
    size_ratio: float
    score: float

    @property
    def clarity(self) -> float:
        """
        How much to trust this face when forming clusters. Big, confident faces
        seed clusters; small or uncertain ones get attached to them.
        """
        return self.size_ratio * self.score


@dataclass
class Person:
    """A distinct individual discovered in the collection."""

    id: str
    refs: List[FaceRef] = field(default_factory=list)
    name: str = ""
    role: str = config.Role.UNKNOWN.value
    note: str = ""

    # -- geometry ------------------------------------------------------
    @property
    def centroid(self) -> np.ndarray:
        """Mean embedding, re-normalized so cosine stays a dot product."""
        stacked = np.vstack([r.embedding for r in self.refs])
        mean = stacked.mean(axis=0)
        norm = float(np.linalg.norm(mean))
        return mean / norm if norm > 1e-6 else mean

    @property
    def photos(self) -> Set[str]:
        return {r.filepath for r in self.refs}

    @property
    def face_count(self) -> int:
        return len(self.refs)

    @property
    def photo_count(self) -> int:
        return len(self.photos)

    @property
    def best_ref(self) -> FaceRef:
        """The clearest face, for use as this person's thumbnail."""
        return max(self.refs, key=lambda r: r.clarity)

    @property
    def is_labeled(self) -> bool:
        """Has been reviewed, whether or not it turned out to be a person."""
        return self.role != config.Role.UNKNOWN.value

    @property
    def is_person(self) -> bool:
        """
        A real human who should influence which section a photo goes to.

        False for Role.IGNORE, which covers statues, idols, posters and framed
        photographs that the face detector quite reasonably fires on.
        """
        return self.role not in {r.value for r in config.NON_PERSON_ROLES}

    def cohesion(self) -> float:
        """
        Mean similarity of members to the centroid. Low values suggest the
        cluster has swallowed more than one person.
        """
        if len(self.refs) < 2:
            return 1.0
        c = self.centroid
        return float(np.mean([np.dot(r.embedding, c) for r in self.refs]))

    def display(self) -> str:
        who = self.name or "(unnamed)"
        role = self.role if self.is_labeled else "unlabeled"
        return f"{self.id}  {who} [{role}]"


# ============================================================================
# COLLECTING FACES
# ============================================================================

def collect_refs(results: Sequence[FaceResult]) -> List[FaceRef]:
    """Flatten per-photo results into one list of embeddable faces."""
    refs: List[FaceRef] = []
    for result in results:
        if not result.ok:
            continue
        for index, face in enumerate(result.faces):
            if face.embedding is None:
                continue
            refs.append(
                FaceRef(
                    filepath=result.filepath,
                    face_index=index,
                    embedding=np.asarray(face.embedding, dtype=np.float32),
                    size_ratio=face.size_ratio,
                    score=face.score,
                )
            )
    return refs


# ============================================================================
# CLUSTERING
# ============================================================================

def _assign(refs: List[FaceRef], threshold: float) -> List[List[FaceRef]]:
    """
    Greedy pass: place each face in the most similar cluster it is allowed to
    join, or start a new one.
    """
    clusters: List[List[FaceRef]] = []
    centroids: List[np.ndarray] = []
    occupied: List[Set[str]] = []      # photos already represented per cluster

    # Clearest faces first so early clusters form around reliable evidence.
    for ref in sorted(refs, key=lambda r: r.clarity, reverse=True):
        best_i, best_sim = -1, threshold

        for i, centroid in enumerate(centroids):
            if ref.filepath in occupied[i]:
                continue                # same photo: cannot be the same person
            sim = float(np.dot(ref.embedding, centroid))
            if sim >= best_sim:
                best_i, best_sim = i, sim

        if best_i < 0:
            clusters.append([ref])
            centroids.append(ref.embedding.copy())
            occupied.append({ref.filepath})
        else:
            clusters[best_i].append(ref)
            occupied[best_i].add(ref.filepath)
            stacked = np.vstack([r.embedding for r in clusters[best_i]])
            mean = stacked.mean(axis=0)
            norm = float(np.linalg.norm(mean))
            centroids[best_i] = mean / norm if norm > 1e-6 else mean

    return clusters


def _merge(clusters: List[List[FaceRef]], threshold: float) -> List[List[FaceRef]]:
    """
    Repeatedly fuse the two most similar clusters until none are close enough.

    Greedy assignment can split one person across several clusters when their
    first few photos look unalike; this heals that.
    """
    def centroid_of(cluster: List[FaceRef]) -> np.ndarray:
        mean = np.vstack([r.embedding for r in cluster]).mean(axis=0)
        norm = float(np.linalg.norm(mean))
        return mean / norm if norm > 1e-6 else mean

    working = [list(c) for c in clusters]

    while True:
        centroids = [centroid_of(c) for c in working]
        photos = [{r.filepath for r in c} for c in working]

        best: Optional[Tuple[float, int, int]] = None
        for i in range(len(working)):
            for j in range(i + 1, len(working)):
                if photos[i] & photos[j]:
                    continue            # both appear in one photo: distinct people
                sim = float(np.dot(centroids[i], centroids[j]))
                if sim >= threshold and (best is None or sim > best[0]):
                    best = (sim, i, j)

        if best is None:
            return working

        _, i, j = best
        working[i].extend(working[j])
        del working[j]


def cluster(
    results: Sequence[FaceResult],
    threshold: float = config.FACE_MATCH_THRESHOLD,
) -> List[Person]:
    """
    Group every face in the collection into people, most-photographed first.

    Person ids are positional (p01, p02, ...) and so can shift when photos are
    added; carry_labels re-attaches your labels by face similarity instead of
    by id.
    """
    refs = collect_refs(results)
    if not refs:
        return []

    clusters = _merge(_assign(refs, threshold), threshold)

    # Most-photographed first: the couple should land at the top of the list a
    # user is about to label by hand.
    clusters.sort(key=lambda c: (len({r.filepath for r in c}), len(c)), reverse=True)

    return [Person(id=f"p{i:02d}", refs=refs_) for i, refs_ in enumerate(clusters, 1)]


# ============================================================================
# LABEL CONTINUITY
# ============================================================================

def carry_labels(people: List[Person], source_dir: str) -> int:
    """
    Re-apply labels from a previous run by matching saved centroids to the new
    clusters, so adding photos to a collection never costs you your labeling.

    Returns how many people got a label back.
    """
    blob = store.load_people(source_dir)
    if not blob:
        return 0

    previous = [
        (
            np.asarray(p["centroid"], dtype=np.float32),
            p.get("name", ""),
            p.get("role", config.Role.UNKNOWN.value),
            p.get("note", ""),
        )
        for p in blob.get("people", [])
        if p.get("centroid") and p.get("role", config.Role.UNKNOWN.value) != config.Role.UNKNOWN.value
    ]
    if not previous:
        return 0

    centroids = [p.centroid for p in people]
    taken: Set[int] = set()
    restored = 0

    # Best matches first, and one old label per new cluster.
    pairs = sorted(
        (
            (float(np.dot(centroids[i], old[0])), i, k)
            for i in range(len(people))
            for k, old in enumerate(previous)
        ),
        reverse=True,
    )

    used_old: Set[int] = set()
    for sim, i, k in pairs:
        if sim < config.FACE_MATCH_THRESHOLD:
            break
        if i in taken or k in used_old:
            continue
        _, name, role, note = previous[k]
        people[i].name, people[i].role, people[i].note = name, role, note
        taken.add(i)
        used_old.add(k)
        restored += 1

    return restored


# ============================================================================
# PERSISTENCE
# ============================================================================

def to_blob(people: List[Person], source_dir: str) -> dict:
    """Serialize people, including centroids so labels can be carried over."""
    return {
        "schema": 1,
        "source": source_dir,
        "people": [
            {
                "id": p.id,
                "name": p.name,
                "role": p.role,
                "note": p.note,
                "face_count": p.face_count,
                "photo_count": p.photo_count,
                "cohesion": round(p.cohesion(), 4),
                "centroid": [round(float(v), 6) for v in p.centroid],
                "thumbnail": os.path.basename(p.best_ref.filepath),
                "photos": sorted(os.path.basename(f) for f in p.photos),
            }
            for p in people
        ],
    }


def save(people: List[Person], source_dir: str) -> str:
    return store.save_people(source_dir, to_blob(people, source_dir))


def roles_by_photo(people: List[Person]) -> Dict[str, List[Person]]:
    """Invert to photo -> the labeled people appearing in it."""
    index: Dict[str, List[Person]] = {}
    for person in people:
        for photo in person.photos:
            index.setdefault(photo, []).append(person)
    return index
