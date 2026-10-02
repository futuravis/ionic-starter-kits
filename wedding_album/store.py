"""
Per-collection state: a cache for face analysis and a home for person labels.

State lives in a dot-directory inside the collection itself so a folder of
photos stays self-describing; move the folder and its labels travel with it.
The scanner skips this directory, so cached state is never mistaken for photos.

    <collection>/.album_state/
        faces.json        per-photo face metadata, keyed by path+mtime+size
        faces.npy         the 128-d embeddings, one row per cached face
        people.json       person clusters and the roles you assigned them

Face detection is by far the slowest stage, so the cache is what makes the
label-review-relabel loop bearable on a few thousand photos.
"""

from __future__ import annotations

import json
import os
from typing import Dict, List, Optional, Tuple

import numpy as np

from . import config
from .faces import Face, FaceResult


# ============================================================================
# PATHS
# ============================================================================

def state_dir(source_dir: str) -> str:
    return os.path.join(source_dir, config.STATE_DIR_NAME)


def ensure_state_dir(source_dir: str) -> str:
    path = state_dir(source_dir)
    os.makedirs(path, exist_ok=True)
    return path


def _faces_json(source_dir: str) -> str:
    return os.path.join(state_dir(source_dir), "faces.json")


def _faces_npy(source_dir: str) -> str:
    return os.path.join(state_dir(source_dir), "faces.npy")


def people_json(source_dir: str) -> str:
    return os.path.join(state_dir(source_dir), "people.json")


# ============================================================================
# FINGERPRINTING
# ============================================================================

def fingerprint(filepath: str) -> str:
    """
    Cheap identity for a file: size and modification time.

    Avoids hashing megabytes of JPEG on every run. If a photo is edited or
    replaced, this changes and the cached faces are recomputed.
    """
    try:
        st = os.stat(filepath)
        return f"{st.st_size}:{int(st.st_mtime)}"
    except OSError:
        return ""


# ============================================================================
# FACE CACHE
# ============================================================================

class FaceCache:
    """
    Stores FaceResults keyed by absolute path, invalidated by fingerprint.

    Embeddings are held in one contiguous float32 array rather than in JSON;
    128 floats per face as text would bloat the file and lose precision.
    """

    SCHEMA = 2

    def __init__(self, source_dir: str) -> None:
        self.source_dir = source_dir
        self._entries: Dict[str, dict] = {}
        self._embeddings: np.ndarray = np.zeros((0, 128), dtype=np.float32)
        # Settings that invalidate the whole cache when changed, since they
        # alter which faces are found at all.
        self._settings = {
            "detect_max_dim": config.FACE_DETECT_MAX_DIM,
            "score_threshold": config.FACE_SCORE_THRESHOLD,
            "nms_threshold": config.FACE_NMS_THRESHOLD,
            "min_size_ratio": config.FACE_MIN_SIZE_RATIO,
        }

    # ------------------------------------------------------------------
    def load(self) -> bool:
        """Read cache from disk. Returns False if absent or unusable."""
        jpath, npath = _faces_json(self.source_dir), _faces_npy(self.source_dir)
        if not (os.path.isfile(jpath) and os.path.isfile(npath)):
            return False

        try:
            with open(jpath, "r", encoding="utf-8") as fh:
                blob = json.load(fh)
            if blob.get("schema") != self.SCHEMA:
                return False
            if blob.get("settings") != self._settings:
                # Detector settings changed; every cached detection is suspect.
                return False
            self._entries = blob.get("entries", {})
            self._embeddings = np.load(npath).astype(np.float32)
            return True
        except (OSError, ValueError, KeyError):
            self._entries = {}
            self._embeddings = np.zeros((0, 128), dtype=np.float32)
            return False

    # ------------------------------------------------------------------
    def save(self) -> None:
        ensure_state_dir(self.source_dir)
        with open(_faces_json(self.source_dir), "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "schema": self.SCHEMA,
                    "settings": self._settings,
                    "entries": self._entries,
                },
                fh,
                indent=1,
            )
        np.save(_faces_npy(self.source_dir), self._embeddings)

    # ------------------------------------------------------------------
    def get(self, filepath: str) -> Optional[FaceResult]:
        """Return a cached result for this exact file version, else None."""
        entry = self._entries.get(filepath)
        if entry is None or entry.get("fingerprint") != fingerprint(filepath):
            return None

        result = FaceResult(filepath=filepath, ok=entry["ok"], error=entry.get("error", ""))
        for rec in entry.get("faces", []):
            row = rec.get("row", -1)
            embedding = None
            if 0 <= row < len(self._embeddings):
                embedding = self._embeddings[row]
            result.faces.append(
                Face(
                    x=rec["x"], y=rec["y"], w=rec["w"], h=rec["h"],
                    score=rec["score"],
                    embedding=embedding,
                    size_ratio=rec["size_ratio"],
                )
            )
        return result

    # ------------------------------------------------------------------
    def put(self, result: FaceResult) -> None:
        """Add or replace the cached entry for a photo."""
        rows: List[np.ndarray] = []
        records: List[dict] = []
        start = len(self._embeddings)

        for face in result.faces:
            row = -1
            if face.embedding is not None:
                row = start + len(rows)
                rows.append(np.asarray(face.embedding, dtype=np.float32))
            records.append(
                {
                    "x": face.x, "y": face.y, "w": face.w, "h": face.h,
                    "score": round(float(face.score), 4),
                    "size_ratio": round(float(face.size_ratio), 5),
                    "row": row,
                }
            )

        if rows:
            self._embeddings = np.vstack([self._embeddings, np.vstack(rows)])

        self._entries[result.filepath] = {
            "fingerprint": fingerprint(result.filepath),
            "ok": result.ok,
            "error": result.error,
            "faces": records,
        }

    # ------------------------------------------------------------------
    def prune(self, keep: List[str]) -> int:
        """
        Drop entries for photos no longer in the collection and compact the
        embedding array, which otherwise grows forever across re-runs.
        """
        keep_set = set(keep)
        stale = [p for p in self._entries if p not in keep_set]
        for path in stale:
            del self._entries[path]

        # Rebuild the embedding array against surviving rows only.
        new_rows: List[np.ndarray] = []
        for entry in self._entries.values():
            for rec in entry.get("faces", []):
                old = rec.get("row", -1)
                if 0 <= old < len(self._embeddings):
                    rec["row"] = len(new_rows)
                    new_rows.append(self._embeddings[old])
                else:
                    rec["row"] = -1

        self._embeddings = (
            np.vstack(new_rows).astype(np.float32) if new_rows
            else np.zeros((0, 128), dtype=np.float32)
        )
        return len(stale)

    # ------------------------------------------------------------------
    @property
    def size(self) -> int:
        return len(self._entries)


# ============================================================================
# PEOPLE / LABELS
# ============================================================================

def load_people(source_dir: str) -> Optional[dict]:
    path = people_json(source_dir)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def save_people(source_dir: str, blob: dict) -> str:
    ensure_state_dir(source_dir)
    path = people_json(source_dir)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(blob, fh, indent=1)
    return path


def existing_labels(source_dir: str) -> Dict[str, Tuple[str, str, str]]:
    """
    Recover name/role/note keyed by person id from a previous run, so
    re-clustering after adding photos does not throw away your labeling.
    """
    blob = load_people(source_dir)
    if not blob:
        return {}
    return {
        p["id"]: (p.get("name", ""), p.get("role", config.Role.UNKNOWN.value), p.get("note", ""))
        for p in blob.get("people", [])
    }
