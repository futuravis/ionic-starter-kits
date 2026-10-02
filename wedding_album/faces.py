"""
Face detection and identity embedding.

Runs entirely locally on the CPU using OpenCV's bundled DNN engine with two
weight files from the OpenCV model zoo:

    YuNet  - detects faces, returns a box, 5 landmarks and a confidence
    SFace  - turns an aligned face crop into a 128-d identity vector

No API key, no account, no network access after the one-time model download,
and no photo ever leaves the machine.

Deliberately not Haar cascades: OpenCV 5 stopped shipping them, and the old
curator's cascade call raised on every image. These ONNX models also recognize
*who* a face is, which cascades never could, and identity is what the whole
album structure depends on.
"""

from __future__ import annotations

import os
import urllib.request
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

import cv2
import numpy as np

from . import config


# ============================================================================
# MODEL FILES
# ============================================================================

class ModelsMissing(RuntimeError):
    """Raised when the ONNX weight files are not present."""


def models_present() -> bool:
    return os.path.isfile(config.YUNET_MODEL) and os.path.isfile(config.SFACE_MODEL)


def require_models() -> None:
    if not models_present():
        raise ModelsMissing(
            "Face models not found. Run `python3 -m wedding_album download-models`."
        )


def download_models(force: bool = False) -> List[str]:
    """
    Fetch the two ONNX files into models/. Returns what was downloaded.

    The only step in this project that touches the network.
    """
    os.makedirs(config.MODELS_DIR, exist_ok=True)
    fetched: List[str] = []

    for url, dest in ((config.YUNET_URL, config.YUNET_MODEL),
                      (config.SFACE_URL, config.SFACE_MODEL)):
        name = os.path.basename(dest)
        if os.path.isfile(dest) and not force:
            size_mb = os.path.getsize(dest) / 1e6
            print(f"    {name} already present ({size_mb:.1f} MB)")
            continue

        print(f"    downloading {name} ...")
        tmp = dest + ".part"
        try:
            urllib.request.urlopen(url, timeout=60)  # noqa: S310 - fixed https URL
            urllib.request.urlretrieve(url, tmp)     # noqa: S310
            # Guard against a git-lfs pointer or an error page landing on disk
            # instead of real weights.
            with open(tmp, "rb") as fh:
                head = fh.read(4)
            if not head.startswith(b"\x08"):
                raise RuntimeError("downloaded file is not an ONNX model")
            os.replace(tmp, dest)
            fetched.append(dest)
            print(f"    saved {name} ({os.path.getsize(dest) / 1e6:.1f} MB)")
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)

    return fetched


# ============================================================================
# RESULTS
# ============================================================================

@dataclass
class Face:
    """One detected face within a photo."""

    x: int
    y: int
    w: int
    h: int
    score: float
    # L2-normalized 128-d identity vector. Cosine similarity between two of
    # these is a plain dot product.
    embedding: Optional[np.ndarray] = None
    # Face size as a fraction of the image's longest edge.
    size_ratio: float = 0.0

    @property
    def is_prominent(self) -> bool:
        """Large enough to be a subject rather than a bystander."""
        return self.size_ratio >= config.FACE_PROMINENT_SIZE_RATIO

    @property
    def box(self) -> Tuple[int, int, int, int]:
        return (self.x, self.y, self.w, self.h)


@dataclass
class FaceResult:
    """All faces found in one photo."""

    filepath: str
    faces: List[Face] = field(default_factory=list)
    ok: bool = False
    error: str = ""

    @property
    def count(self) -> int:
        return len(self.faces)

    @property
    def prominent_count(self) -> int:
        return sum(1 for f in self.faces if f.is_prominent)


# ============================================================================
# ANALYZER
# ============================================================================

class FaceAnalyzer:
    """
    Wraps the two models. Build one per process and reuse it: constructing the
    nets is far slower than running them.
    """

    def __init__(self) -> None:
        require_models()
        # OpenCV 5 logs a harmless target warning when these nets are built.
        cv2.utils.logging.setLogLevel(cv2.utils.logging.LOG_LEVEL_ERROR)

        self._detector = cv2.FaceDetectorYN.create(
            model=config.YUNET_MODEL,
            config="",
            input_size=(320, 320),
            score_threshold=config.FACE_SCORE_THRESHOLD,
            nms_threshold=config.FACE_NMS_THRESHOLD,
            top_k=config.FACE_TOP_K,
        )
        self._recognizer = cv2.FaceRecognizerSF.create(
            model=config.SFACE_MODEL,
            config="",
        )

    # ------------------------------------------------------------------
    def analyze(self, filepath: str, with_embeddings: bool = True) -> FaceResult:
        """Detect and optionally embed every face in one photo. Never raises."""
        result = FaceResult(filepath=filepath)

        try:
            img = cv2.imread(filepath, cv2.IMREAD_COLOR)
            if img is None or img.size == 0:
                result.error = "unreadable or not an image"
                return result

            # Downscale once; detect and align on the same array so landmark
            # coordinates stay valid for alignCrop.
            h, w = img.shape[:2]
            longest = max(h, w)
            if longest > config.FACE_DETECT_MAX_DIM:
                scale = config.FACE_DETECT_MAX_DIM / longest
                img = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)

            ih, iw = img.shape[:2]
            longest = max(ih, iw)
            self._detector.setInputSize((iw, ih))

            _, raw = self._detector.detect(img)
            if raw is None:
                result.ok = True          # a photo with no faces is a valid result
                return result

            min_px = config.FACE_MIN_SIZE_RATIO * longest

            for row in raw:
                x, y, fw, fh = (float(v) for v in row[:4])
                score = float(row[-1])

                if max(fw, fh) < min_px:
                    continue              # background bystander

                face = Face(
                    x=int(round(x)), y=int(round(y)),
                    w=int(round(fw)), h=int(round(fh)),
                    score=score,
                    size_ratio=max(fw, fh) / longest,
                )

                if with_embeddings:
                    face.embedding = self._embed(img, row)
                    if face.embedding is None:
                        continue          # unusable crop, drop the face

                result.faces.append(face)

            # Largest first: the subject of the photo leads.
            result.faces.sort(key=lambda f: f.size_ratio, reverse=True)
            result.ok = True

        except Exception as exc:  # noqa: BLE001 - reported, never hidden
            result.error = f"{type(exc).__name__}: {exc}"

        return result

    # ------------------------------------------------------------------
    def _embed(self, img: np.ndarray, detection_row: np.ndarray) -> Optional[np.ndarray]:
        """Align a face using its landmarks and return a normalized 128-d vector."""
        try:
            aligned = self._recognizer.alignCrop(img, detection_row)
            if aligned is None or aligned.size == 0:
                return None
            vec = self._recognizer.feature(aligned).flatten().astype(np.float32)
            norm = float(np.linalg.norm(vec))
            if norm < 1e-6:
                return None
            return vec / norm
        except cv2.error:
            return None


# ============================================================================
# SIMILARITY
# ============================================================================

def similarity(a: np.ndarray, b: np.ndarray) -> float:
    """
    Cosine similarity between two normalized embeddings.

    Compare against config.FACE_MATCH_THRESHOLD: above it, same person.
    """
    return float(np.dot(a, b))


def same_person(a: np.ndarray, b: np.ndarray) -> bool:
    return similarity(a, b) >= config.FACE_MATCH_THRESHOLD


# ============================================================================
# BATCH HELPER
# ============================================================================

# One analyzer per worker process, built on first use. The OpenCV net objects
# cannot be pickled, so they can never be passed into a worker; each process
# has to construct its own.
_WORKER_ANALYZER: Optional[FaceAnalyzer] = None


def analyze_one(filepath: str) -> FaceResult:
    """Module-level entry point usable with ProcessPoolExecutor.map."""
    global _WORKER_ANALYZER
    if _WORKER_ANALYZER is None:
        _WORKER_ANALYZER = FaceAnalyzer()
    return _WORKER_ANALYZER.analyze(filepath)


def describe(result: FaceResult) -> str:
    """One-line summary for eyeballing a photo's detections."""
    name = os.path.basename(result.filepath)
    if not result.ok:
        return f"{name}: FAILED ({result.error})"
    if not result.faces:
        return f"{name}: no faces"

    parts = [
        f"{f.size_ratio:.2f}@{f.score:.2f}{'*' if f.is_prominent else ''}"
        for f in result.faces
    ]
    return (
        f"{name}: {result.count} face(s) "
        f"({result.prominent_count} prominent)  " + " ".join(parts)
    )
