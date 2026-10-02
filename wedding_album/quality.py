"""
Technical quality scoring: is this a good photograph?

Answers only that question. Who appears in the photo is the section logic's
concern, which is why there is no face-based bonus in here.

Note on OpenCV: this module uses no Haar cascades. OpenCV 5 stopped shipping
them, and the previous curator's face step raised on every single image, was
swallowed by a broad `except`, and quietly zeroed every score. Face work now
lives in faces.py on the YuNet/SFace DNN models instead.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, asdict
from typing import Dict, List, Optional

import cv2
import numpy as np

from . import config


# ============================================================================
# RAW METRICS
# ============================================================================

@dataclass
class QualityMetrics:
    """Raw, un-normalized measurements for one photo."""

    filepath: str
    sharpness: float = 0.0
    exposure: float = 0.0      # already 0-1, needs no normalization
    contrast: float = 0.0
    saturation: float = 0.0
    noise: float = 0.0         # higher is worse
    width: int = 0
    height: int = 0
    phash: str = ""
    ok: bool = False           # False if the file could not be read
    error: str = ""

    def to_dict(self) -> Dict:
        return asdict(self)


def compute_sharpness(gray: np.ndarray) -> float:
    """Variance of the Laplacian. Higher means crisper detail."""
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def compute_exposure(gray: np.ndarray) -> float:
    """
    Exposure health in 0-1, where 1 is well exposed.

    Combines two faults: overall brightness drifting from the midtones, and
    detail lost to pure white or pure black. Clipping matters more than drift
    because it is unrecoverable, so it carries the larger penalty.
    """
    pixels = gray.size
    if pixels == 0:
        return 0.0

    drift = abs(float(np.mean(gray)) - 128.0) / 128.0

    blown = float(np.count_nonzero(gray >= 250)) / pixels
    crushed = float(np.count_nonzero(gray <= 5)) / pixels
    # A little clipping is normal (a bright sky, a dark suit); scale so that
    # roughly 10% clipped costs the full penalty.
    clipping = min(1.0, (blown + crushed) / 0.10)

    score = 1.0 - (0.4 * drift + 0.6 * clipping)
    return float(max(0.0, min(1.0, score)))


def compute_contrast(gray: np.ndarray) -> float:
    """Standard deviation of intensity. Higher means more dynamic range."""
    return float(np.std(gray))


def compute_saturation(bgr: np.ndarray) -> float:
    """Mean saturation in HSV. Higher means more vivid colour."""
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    return float(np.mean(hsv[:, :, 1]))


def compute_noise(gray: np.ndarray) -> float:
    """
    Sensor-noise estimate: robust sigma from the median absolute deviation of
    the Laplacian response. Returned raw, so higher means noisier; the weighted
    score inverts it.
    """
    lap = cv2.Laplacian(gray, cv2.CV_64F, ksize=3)
    return float(np.median(np.abs(lap)) * 1.4826)


def compute_phash(bgr: np.ndarray) -> str:
    """
    Perceptual hash for near-duplicate detection, computed with OpenCV on the
    already-decoded array so we never open the file a second time.

    This is the classic DCT pHash: shrink, take the low-frequency DCT block,
    threshold against its median, pack the bits to hex.
    """
    size = config.PHASH_SIZE
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    small = cv2.resize(gray, (size * 4, size * 4), interpolation=cv2.INTER_AREA)
    dct = cv2.dct(np.float32(small))
    block = dct[:size, :size].flatten()
    # Skip the DC term when choosing the threshold; it dwarfs the rest.
    median = np.median(block[1:])
    bits = block > median
    return "".join(
        f"{int(''.join('1' if b else '0' for b in bits[i:i + 4]), 2):x}"
        for i in range(0, len(bits), 4)
    )


def phash_distance(a: str, b: str) -> int:
    """
    Hamming distance between two hashes from compute_phash.

    Returns a deliberately large number for missing or mismatched hashes so a
    failed hash can never be mistaken for a duplicate.
    """
    if not a or not b or len(a) != len(b):
        return 10 ** 6
    return bin(int(a, 16) ^ int(b, 16)).count("1")


# ============================================================================
# PER-IMAGE ENTRY POINT
# ============================================================================

def load_for_analysis(filepath: str) -> Optional[np.ndarray]:
    """
    Decode an image and downscale it to the analysis size.

    Returns None when the file is unreadable or not actually an image, which
    the caller reports rather than silently treating as a zero-quality photo.
    """
    bgr = cv2.imread(filepath, cv2.IMREAD_COLOR)
    if bgr is None or bgr.size == 0:
        return None

    h, w = bgr.shape[:2]
    longest = max(h, w)
    if longest > config.ANALYSIS_MAX_DIM:
        scale = config.ANALYSIS_MAX_DIM / longest
        bgr = cv2.resize(bgr, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)

    return bgr


def measure(filepath: str) -> QualityMetrics:
    """Measure one photo. Never raises; failures come back on the result."""
    result = QualityMetrics(filepath=filepath)

    try:
        bgr = load_for_analysis(filepath)
        if bgr is None:
            result.error = "unreadable or not an image"
            return result

        result.height, result.width = bgr.shape[:2]
        gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)

        result.sharpness = compute_sharpness(gray)
        result.exposure = compute_exposure(gray)
        result.contrast = compute_contrast(gray)
        result.saturation = compute_saturation(bgr)
        result.noise = compute_noise(gray)
        result.phash = compute_phash(bgr)
        result.ok = True

    except Exception as exc:  # noqa: BLE001 - reported, not hidden
        result.error = f"{type(exc).__name__}: {exc}"

    return result


# ============================================================================
# NORMALIZATION ACROSS THE COLLECTION
# ============================================================================

def _robust_scale(values: List[float], inverted: bool) -> List[float]:
    """
    Scale to 0-1 using percentile clipping so a single freak value cannot
    compress everything else into a narrow band.
    """
    arr = np.asarray(values, dtype=np.float64)
    lo_pct, hi_pct = config.NORMALIZE_PERCENTILES
    lo = float(np.percentile(arr, lo_pct))
    hi = float(np.percentile(arr, hi_pct))

    if hi - lo < 1e-9:
        scaled = np.full_like(arr, 0.5)
    else:
        scaled = np.clip((arr - lo) / (hi - lo), 0.0, 1.0)

    if inverted:
        scaled = 1.0 - scaled

    return [float(v) for v in scaled]


def score_collection(metrics: List[QualityMetrics]) -> Dict[str, float]:
    """
    Turn raw measurements into one 0-1 quality score per photo.

    Normalization is relative to this collection: a score answers "how good is
    this shot compared to the others from the same shoot", which is what
    picking the best of a set actually calls for.

    Returns a mapping of filepath -> score. Unreadable photos are omitted.
    """
    usable = [m for m in metrics if m.ok]
    if not usable:
        return {}

    normalized: Dict[str, List[float]] = {}
    for name in config.QUALITY_WEIGHTS:
        if name == "exposure":
            # Already an absolute 0-1 judgement; rescaling it against the set
            # would turn "everything was slightly dark" into "some were fine".
            normalized[name] = [m.exposure for m in usable]
        else:
            normalized[name] = _robust_scale(
                [getattr(m, name) for m in usable],
                inverted=name in config.INVERTED_METRICS,
            )

    scores: Dict[str, float] = {}
    for i, m in enumerate(usable):
        scores[m.filepath] = float(
            sum(weight * normalized[name][i] for name, weight in config.QUALITY_WEIGHTS.items())
        )

    return scores


def explain(metrics: QualityMetrics) -> str:
    """One-line raw-metric dump, for eyeballing why a photo scored as it did."""
    if not metrics.ok:
        return f"{os.path.basename(metrics.filepath)}: FAILED ({metrics.error})"
    return (
        f"{os.path.basename(metrics.filepath)}: "
        f"sharp={metrics.sharpness:8.1f} "
        f"expo={metrics.exposure:.3f} "
        f"contrast={metrics.contrast:6.2f} "
        f"sat={metrics.saturation:6.2f} "
        f"noise={metrics.noise:6.2f} "
        f"{metrics.width}x{metrics.height}"
    )
