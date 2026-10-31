"""
Measurement: everything we can learn about one photograph.

Two things live here that `wedding_album.quality` does not provide.

1. A noise-robust sharpness. The shared `quality.compute_sharpness` is the
   variance of the Laplacian, which responds to any rapid change between
   neighbouring pixels - and sensor grain is exactly that. On a test batch it
   read grainy frames as roughly 25x sharper than they were, and they took
   every top slot while being described as having "crisp detail".

   The fix is to measure at several scales and keep the smallest result. Grain
   averages away under downsampling and real detail survives, so a photo has
   to look sharp at *every* scale. Measured against a clean reference frame,
   this cut grain inflation from 11.7x to 1.3x at extreme noise while leaving
   blur sensitivity untouched, because for a blurred photo the full scale is
   already the minimum.

   Three other fixes were tried first and all failed; see docs/STORY.md before
   reaching for a simpler one.

2. Face probing. Enough to answer "is there a face, is it big enough, is it
   lit, is it sharp, is it facing the camera" - which is what separates a
   photograph of people from a sharp photograph of the back of a head.

   This uses YuNet only, not SFace. Identity is irrelevant here, and skipping
   the recognition model halves the cost.

   What YuNet cannot do: eyes open or closed, and smiling or not. It returns
   five landmarks - two eye centres, the nose tip and two mouth corners - and
   there is no eyelid or mouth-shape information in that. Anything claiming to
   score a facial expression from these five points would be guessing.
"""

from __future__ import annotations

import math
import os
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Sequence, Tuple

import cv2
import numpy as np

from wedding_album import config, faces as album_faces, quality
from wedding_album.quality import QualityMetrics


ProgressFn = Optional[Callable[[str, int, int], None]]


# Scales at which sharpness is measured. Grain dies with each halving; real
# detail does not.
SHARPNESS_SCALES: Tuple[float, ...] = (1.0, 0.5, 0.25, 0.125)

# Below this many pixels on the short edge, a Laplacian variance is measuring
# resampling artefacts rather than the photograph, so the scale is skipped.
MIN_SCALE_PX = 48


# ============================================================================
# SHARPNESS
# ============================================================================

def _laplacian_variance(gray: np.ndarray) -> float:
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def sharpness_multiscale(
    gray: np.ndarray,
    scales: Sequence[float] = SHARPNESS_SCALES,
) -> float:
    """
    Noise-robust sharpness: the lowest Laplacian variance across several scales.

    A grainy photo scores high at full scale and collapses as soon as it is
    downsampled, because averaging neighbouring pixels destroys grain. A truly
    sharp photo holds its score. Taking the minimum therefore demands that the
    detail survive every scale, which grain cannot fake.

    Blur is still caught at full sensitivity: a blurred photo is already at its
    weakest at full scale, so the minimum is unchanged.
    """
    if gray is None or gray.size == 0:
        return 0.0

    readings: List[float] = []

    for scale in scales:
        if scale >= 1.0:
            view = gray
        else:
            if min(gray.shape[:2]) * scale < MIN_SCALE_PX:
                continue
            view = cv2.resize(gray, None, fx=scale, fy=scale,
                              interpolation=cv2.INTER_AREA)
        readings.append(_laplacian_variance(view))

    return min(readings) if readings else 0.0


def region_sharpness(gray: np.ndarray, box: Tuple[int, int, int, int]) -> float:
    """
    Sharpness of one rectangle, measured the same noise-robust way.

    Needed because a photo can be sharp overall and soft exactly where it
    matters: the background in focus and the couple's faces not.
    """
    x, y, w, h = box
    ih, iw = gray.shape[:2]
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(iw, x + w), min(ih, y + h)

    if x1 - x0 < 12 or y1 - y0 < 12:
        return 0.0

    return sharpness_multiscale(gray[y0:y1, x0:x1])


def region_brightness(gray: np.ndarray, box: Tuple[int, int, int, int]) -> float:
    """Mean brightness of one rectangle, 0-255. 0.0 when the box is unusable."""
    x, y, w, h = box
    ih, iw = gray.shape[:2]
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(iw, x + w), min(ih, y + h)

    if x1 - x0 < 4 or y1 - y0 < 4:
        return 0.0

    return float(np.mean(gray[y0:y1, x0:x1]))


# ============================================================================
# QUALITY, WITH THE CORRECTED SHARPNESS
# ============================================================================

@dataclass
class PhotoMetrics(QualityMetrics):
    """
    Everything `QualityMetrics` holds, plus the raw readings a diagnosis needs.

    Subclassing matters: `quality.score_collection` reads its weighted metrics
    by name off whatever it is given, so these instances score exactly as the
    shared code expects while carrying the extra detail.

    `exposure` is a single 0-1 verdict, which is right for scoring and useless
    for explaining. "Too dark" and "highlights blown out" are opposite faults
    that can produce the same exposure number, so the components are kept.
    """

    brightness: float = 0.0        # mean grey level, 0-255
    blown: float = 0.0             # fraction of pixels at or near pure white
    crushed: float = 0.0           # fraction of pixels at or near pure black


def measure(filepath: str) -> PhotoMetrics:
    """
    Measure one photo. Never raises; failures come back on the result.

    Mirrors `quality.measure` and substitutes the noise-robust sharpness. Every
    other metric comes from the shared, tested functions.

    Module level and picklable, so it can run in a worker process.
    """
    result = PhotoMetrics(filepath=filepath)

    try:
        bgr = quality.load_for_analysis(filepath)
        if bgr is None:
            result.error = "unreadable or not an image"
            return result

        result.height, result.width = bgr.shape[:2]
        gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)

        result.sharpness = sharpness_multiscale(gray)        # the one change
        result.exposure = quality.compute_exposure(gray)
        result.contrast = quality.compute_contrast(gray)
        result.saturation = quality.compute_saturation(bgr)
        result.noise = quality.compute_noise(gray)
        result.phash = quality.compute_phash(bgr)

        pixels = gray.size or 1
        result.brightness = float(np.mean(gray))
        result.blown = float(np.count_nonzero(gray >= 250)) / pixels
        result.crushed = float(np.count_nonzero(gray <= 5)) / pixels

        result.ok = True

    except Exception as exc:  # noqa: BLE001 - reported, never hidden
        result.error = f"{type(exc).__name__}: {exc}"

    return result


def measure_all(
    paths: Sequence[str],
    workers: int = 1,
    progress: ProgressFn = None,
) -> List[PhotoMetrics]:
    """Measure a batch, in parallel when asked."""
    total = len(paths)
    out: List[PhotoMetrics] = []

    def report() -> None:
        if progress:
            progress("measuring photos", len(out), total)

    report()

    if workers <= 1 or total <= 2:
        for path in paths:
            out.append(measure(path))
            report()
        return out

    with ProcessPoolExecutor(max_workers=workers) as pool:
        for metrics in pool.map(measure, paths):
            out.append(metrics)
            report()

    return out


# ============================================================================
# FACES
# ============================================================================

@dataclass
class FaceInfo:
    """One detected face, with the readings that decide if it is usable."""

    box: Tuple[int, int, int, int] = (0, 0, 0, 0)
    score: float = 0.0
    size_ratio: float = 0.0        # longest face edge / longest image edge
    brightness: float = 0.0        # 0-255, mean over the face box
    sharpness: float = 0.0         # noise-robust, over the face box
    tilt_degrees: float = 0.0      # 0 is level; from the two eye centres
    frontality: float = 1.0        # 1.0 square to camera, towards 0 turned away

    @property
    def is_prominent(self) -> bool:
        return self.size_ratio >= config.FACE_PROMINENT_SIZE_RATIO


@dataclass
class FaceReading:
    """Every face found in one photo, plus frame context for comparison."""

    filepath: str
    faces: List[FaceInfo] = field(default_factory=list)
    frame_brightness: float = 0.0
    frame_sharpness: float = 0.0
    ok: bool = False
    error: str = ""

    @property
    def count(self) -> int:
        return len(self.faces)

    @property
    def prominent_count(self) -> int:
        return sum(1 for f in self.faces if f.is_prominent)

    @property
    def main(self) -> Optional[FaceInfo]:
        """The largest face: the subject of the photograph, if there is one."""
        return self.faces[0] if self.faces else None


def _geometry(landmarks: np.ndarray) -> Tuple[float, float]:
    """
    Head tilt in degrees and a frontality estimate, from YuNet's 5 landmarks.

    Landmark order is right eye, left eye, nose, right mouth corner, left mouth
    corner. Tilt is the angle of the line between the eyes. Frontality compares
    the nose's horizontal position against the midpoint of the eyes: on a face
    square to the camera the nose sits between them, and as the head turns it
    slides towards one side.

    This is geometry, not expression. It cannot tell whether the eyes are open.
    """
    try:
        right_eye = landmarks[0]
        left_eye = landmarks[1]
        nose = landmarks[2]
    except (IndexError, TypeError):
        return 0.0, 1.0

    dx = float(left_eye[0] - right_eye[0])
    dy = float(left_eye[1] - right_eye[1])
    eye_distance = math.hypot(dx, dy)

    tilt = abs(math.degrees(math.atan2(dy, dx)))
    if tilt > 90.0:
        tilt = 180.0 - tilt

    if eye_distance < 1e-6:
        return tilt, 1.0

    midpoint_x = (float(right_eye[0]) + float(left_eye[0])) / 2.0
    offset = abs(float(nose[0]) - midpoint_x) / eye_distance
    # An offset of half the eye separation is a markedly turned head.
    frontality = max(0.0, min(1.0, 1.0 - offset / 0.5))

    return tilt, frontality


class FaceProbe:
    """
    YuNet detection plus per-face readings.

    One per process and reused: building the net costs far more than running
    it. SFace is deliberately not loaded - identity is not needed here.
    """

    def __init__(self) -> None:
        if not os.path.isfile(config.YUNET_MODEL):
            raise album_faces.ModelsMissing(
                "Face model not found. Run "
                "`python3 -m wedding_album download-models`."
            )

        cv2.utils.logging.setLogLevel(cv2.utils.logging.LOG_LEVEL_ERROR)
        self._detector = cv2.FaceDetectorYN.create(
            model=config.YUNET_MODEL,
            config="",
            input_size=(320, 320),
            score_threshold=config.FACE_SCORE_THRESHOLD,
            nms_threshold=config.FACE_NMS_THRESHOLD,
            top_k=config.FACE_TOP_K,
        )

    def read(self, filepath: str) -> FaceReading:
        """Probe one photo. Never raises."""
        reading = FaceReading(filepath=filepath)

        try:
            img = cv2.imread(filepath, cv2.IMREAD_COLOR)
            if img is None or img.size == 0:
                reading.error = "unreadable or not an image"
                return reading

            h, w = img.shape[:2]
            longest = max(h, w)
            if longest > config.FACE_DETECT_MAX_DIM:
                scale = config.FACE_DETECT_MAX_DIM / longest
                img = cv2.resize(img, None, fx=scale, fy=scale,
                                 interpolation=cv2.INTER_AREA)

            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            reading.frame_brightness = float(np.mean(gray))
            reading.frame_sharpness = sharpness_multiscale(gray)

            ih, iw = gray.shape[:2]
            longest = max(ih, iw)
            self._detector.setInputSize((iw, ih))

            _, raw = self._detector.detect(img)
            if raw is None:
                reading.ok = True          # no faces is a valid answer
                return reading

            minimum = config.FACE_MIN_SIZE_RATIO * longest

            for row in raw:
                x, y, fw, fh = (float(v) for v in row[:4])
                if max(fw, fh) < minimum:
                    continue               # background bystander

                box = (int(round(x)), int(round(y)),
                       int(round(fw)), int(round(fh)))
                tilt, frontality = _geometry(
                    np.asarray(row[4:14], dtype=np.float64).reshape(5, 2))

                reading.faces.append(FaceInfo(
                    box=box,
                    score=float(row[-1]),
                    size_ratio=max(fw, fh) / longest,
                    brightness=region_brightness(gray, box),
                    sharpness=region_sharpness(gray, box),
                    tilt_degrees=tilt,
                    frontality=frontality,
                ))

            # Largest first: the subject of the photograph leads.
            reading.faces.sort(key=lambda f: f.size_ratio, reverse=True)
            reading.ok = True

        except Exception as exc:  # noqa: BLE001
            reading.error = f"{type(exc).__name__}: {exc}"

        return reading


_WORKER_PROBE: Optional[FaceProbe] = None


def read_faces(filepath: str) -> FaceReading:
    """
    Module-level entry point usable with ProcessPoolExecutor.map.

    The OpenCV net cannot be pickled, so it can never be handed to a worker;
    each process builds its own on first use.
    """
    global _WORKER_PROBE
    if _WORKER_PROBE is None:
        _WORKER_PROBE = FaceProbe()
    return _WORKER_PROBE.read(filepath)


def faces_available() -> bool:
    return os.path.isfile(config.YUNET_MODEL)


def read_faces_all(
    paths: Sequence[str],
    workers: int = 1,
    progress: ProgressFn = None,
) -> List[FaceReading]:
    """
    Probe a batch for faces, in parallel when asked.

    Returns an empty list when the model is absent, so the caller can carry on
    scoring on technical quality alone rather than failing outright.
    """
    if not faces_available():
        return []

    total = len(paths)
    out: List[FaceReading] = []

    def report() -> None:
        if progress:
            progress("looking for faces", len(out), total)

    report()

    if workers <= 1 or total <= 2:
        probe = FaceProbe()
        for path in paths:
            out.append(probe.read(path))
            report()
        return out

    with ProcessPoolExecutor(max_workers=workers) as pool:
        for reading in pool.map(read_faces, paths):
            out.append(reading)
            report()

    return out
