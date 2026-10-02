"""
Converting a photograph to a vertical format without touching the subject.

The problem this solves: a 3:2 landscape frame has to become 9:16 portrait for
a reel or a story. Cropping to that shape shows 37% of the width, so the couple
either loses an arm or the composition is destroyed.

So nothing is cropped by default. The whole photograph is placed, unaltered and
at full width, on a 9:16 canvas, and the space above and below is filled with a
scaled, blurred, slightly darkened copy of the same frame. The subject keeps
every pixel it started with.

Three honest words about that fill
----------------------------------
This is NOT generative outpainting. It does not invent more floor, more wall or
more sky, because that needs a generative model and this package runs on
OpenCV alone - offline, no API key, no cloud. What it produces is a soft
blurred wash derived from the photograph's own colours, which is what phone
apps and broadcast graphics have used for years. It reads as a deliberate
backdrop rather than as extended scene.

If genuine scene extension is wanted, that is a different tool and a network
service, and it should be a deliberate decision rather than something slipped
in behind this function.

Placement
---------
Vertical position is chosen from the faces, not from the frame's centre. Faces
are placed a little above the middle, which is where an eye expects them and
where a phone's interface is least likely to cover them. With no face found it
falls back to centring, which is the safe answer rather than a guess.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

import cv2
import numpy as np

from wedding_album import config

from . import metrics as measurement


# 9:16, the shape of a phone screen.
DEFAULT_RATIO = (9, 16)

# Output height. 1920 keeps text and lashes crisp on a modern phone without
# producing files nobody can email.
DEFAULT_HEIGHT = 1920

# How hard the backdrop is blurred, as a fraction of canvas width. Strong
# enough that it reads as a wash and never competes with the subject.
BLUR_FRACTION = 0.06

# The backdrop is darkened so the photograph sits forward of it.
BACKDROP_DIM = 0.72

# Where a face should sit vertically, as a fraction of canvas height. Slightly
# above centre.
FACE_TARGET = 0.42

# A thin line between photograph and backdrop, so the edge looks intended.
EDGE_LINE = 2


# ============================================================================
# WHICH WAY UP
# ============================================================================

# Rotations tried when working out which way up a frame is, as the angle the
# stored pixels must be turned to stand upright.
_ROTATIONS = {
    0: None,
    90: cv2.ROTATE_90_CLOCKWISE,
    180: cv2.ROTATE_180,
    270: cv2.ROTATE_90_COUNTERCLOCKWISE,
}

# How much better a rotation's best face must score before the frame is turned.
# Without a margin, noise in the detector would spin correctly-oriented photos.
UPRIGHT_MARGIN = 0.06

# Longest edge used while testing rotations. Orientation is a coarse question
# and full resolution buys nothing but time.
UPRIGHT_PROBE_DIM = 1024


_DETECTOR = None


def _upright_detector():
    """
    One detector per process, built on first use.

    Constructing the net costs far more than running it, and orientation needs
    four runs per photo - so building it per call made straightening roughly
    four times slower than it needed to be. The OpenCV net cannot be pickled,
    so each worker process has to make its own.
    """
    global _DETECTOR
    if _DETECTOR is None:
        cv2.utils.logging.setLogLevel(cv2.utils.logging.LOG_LEVEL_ERROR)
        _DETECTOR = cv2.FaceDetectorYN.create(
            model=config.YUNET_MODEL, config="", input_size=(320, 320),
            score_threshold=config.FACE_SCORE_THRESHOLD,
            nms_threshold=config.FACE_NMS_THRESHOLD, top_k=config.FACE_TOP_K,
        )
    return _DETECTOR


def detect_upright(image: np.ndarray) -> int:
    """
    Degrees the stored pixels must be rotated to stand upright. 0 if unsure.

    Why this is needed at all: a camera held vertically records vertical pixels
    and is supposed to write an EXIF orientation tag saying so. These files
    carry orientation 1 - "normal" - on every frame, including the ones that
    are plainly sideways, so there is no metadata to trust and the answer has
    to be measured.

    Face detectors are trained on upright faces, so the rotation producing the
    most confident detection is the upright one. A frame with no face at any
    rotation returns 0: leaving a photograph alone is the safe answer, and
    guessing from image statistics would turn a sunset upside down.
    """
    if image is None or image.size == 0:
        return 0
    if not measurement.faces_available():
        return 0

    h, w = image.shape[:2]
    longest = max(h, w)
    if longest > UPRIGHT_PROBE_DIM:
        s = UPRIGHT_PROBE_DIM / longest
        probe_image = cv2.resize(image, None, fx=s, fy=s,
                                 interpolation=cv2.INTER_AREA)
    else:
        probe_image = image

    detector = _upright_detector()
    best_angle, best_score, upright_score = 0, 0.0, 0.0

    for angle, code in _ROTATIONS.items():
        view = probe_image if code is None else cv2.rotate(probe_image, code)
        vh, vw = view.shape[:2]
        detector.setInputSize((vw, vh))
        try:
            _, raw = detector.detect(view)
        except cv2.error:
            continue
        if raw is None or len(raw) == 0:
            continue

        score = max(float(row[-1]) for row in raw)
        if angle == 0:
            upright_score = score
        if score > best_score:
            best_angle, best_score = angle, score

    if best_score <= 0.0:
        return 0                          # no face anywhere; leave it alone
    if best_angle == 0:
        return 0
    if best_score - upright_score < UPRIGHT_MARGIN:
        return 0                          # not a clear enough win to act on

    return best_angle


def stand_upright(image: np.ndarray) -> Tuple[np.ndarray, int]:
    """Rotate a frame upright if it is confidently sideways. Returns the angle."""
    angle = detect_upright(image)
    if angle == 0:
        return image, 0
    return cv2.rotate(image, _ROTATIONS[angle]), angle


# ============================================================================
# STRAIGHTENING FILES IN PLACE
# ============================================================================

# Re-encode quality for a straightened frame. A 90 degree rotation moves pixels
# without altering them, but writing a JPEG again costs one generation of loss.
# 97 keeps that invisible; going lossless would need a JPEG-aware tool such as
# jpegtran, which is not a dependency here.
STRAIGHTEN_QUALITY = 97


@dataclass
class Straightened:
    """What straightening one file did."""

    path: str
    angle: int = 0                  # degrees applied; 0 means left alone
    before: Tuple[int, int] = (0, 0)
    after: Tuple[int, int] = (0, 0)
    ok: bool = False
    error: str = ""

    @property
    def name(self) -> str:
        return os.path.basename(self.path)

    @property
    def changed(self) -> bool:
        return self.ok and self.angle != 0


def straighten_file(path: str) -> Straightened:
    """
    Rotate one file upright, in place. Never raises.

    Only whole-quarter-turn rotations are applied, so no pixel is interpolated,
    nothing is cropped and nothing is stretched - the content is identical, just
    the right way up. A frame already upright, or one with no face to judge by,
    is left completely untouched and not even rewritten.

    Module level and picklable, so it can run in a worker process.
    """
    result = Straightened(path=path)

    try:
        image = cv2.imread(path, cv2.IMREAD_COLOR)
        if image is None or image.size == 0:
            result.error = "unreadable or not an image"
            return result

        result.before = (image.shape[1], image.shape[0])

        angle = detect_upright(image)
        if angle == 0:
            result.after = result.before
            result.ok = True
            return result               # untouched, not rewritten

        turned = cv2.rotate(image, _ROTATIONS[angle])
        if not cv2.imwrite(path, turned,
                           [int(cv2.IMWRITE_JPEG_QUALITY), STRAIGHTEN_QUALITY]):
            result.error = "could not write the rotated file"
            return result

        result.angle = angle
        result.after = (turned.shape[1], turned.shape[0])
        result.ok = True

    except Exception as exc:  # noqa: BLE001 - reported, never hidden
        result.error = f"{type(exc).__name__}: {exc}"

    return result


def straighten_all(
    paths: Sequence[str],
    workers: int = 1,
    progress=None,
) -> List[Straightened]:
    """
    Stand a whole batch upright, in place.

    Each worker process builds its own detector, which is why this is not
    simply a loop over `straighten_file` in one process when workers > 1.
    """
    from concurrent.futures import ProcessPoolExecutor

    total = len(paths)
    out: List[Straightened] = []

    def report() -> None:
        if progress:
            progress("standing photos upright", len(out), total)

    report()

    if workers <= 1 or total <= 2:
        for path in paths:
            out.append(straighten_file(path))
            report()
        return out

    with ProcessPoolExecutor(max_workers=workers) as pool:
        for item in pool.map(straighten_file, paths):
            out.append(item)
            report()

    return out


@dataclass
class Conversion:
    """What one conversion did, so the result can be checked rather than trusted."""

    source: str
    output: str
    source_size: Tuple[int, int] = (0, 0)       # width, height
    output_size: Tuple[int, int] = (0, 0)
    subject_box: Tuple[int, int, int, int] = (0, 0, 0, 0)   # where it was placed
    faces_found: int = 0
    cropped: bool = False
    pixels_kept: float = 1.0                    # fraction of the original kept
    ok: bool = False
    error: str = ""

    def describe(self) -> str:
        if not self.ok:
            return f"{os.path.basename(self.source)}: FAILED ({self.error})"
        sw, sh = self.source_size
        ow, oh = self.output_size
        return (
            f"{os.path.basename(self.source)}: {sw}x{sh} -> {ow}x{oh}, "
            f"{self.faces_found} face(s), "
            f"{self.pixels_kept * 100:.0f}% of the original kept"
            + (", cropped" if self.cropped else ", nothing cropped")
        )


# ============================================================================
# PLACEMENT
# ============================================================================

def _canvas_size(ratio: Tuple[int, int], height: int) -> Tuple[int, int]:
    width = int(round(height * ratio[0] / ratio[1]))
    if width % 2:
        width += 1                       # even dimensions keep encoders happy
    return width, height


def _vertical_offset(
    canvas_height: int,
    placed_height: int,
    faces: Sequence[measurement.FaceInfo],
    scale: float,
) -> int:
    """
    Where to put the top edge of the photograph on the canvas.

    With a face, position so it lands near FACE_TARGET. Without one, centre -
    a guess about composition is worse than the obvious answer.
    """
    slack = canvas_height - placed_height
    if slack <= 0:
        return 0
    if not faces:
        return slack // 2

    # The largest face leads; it is the subject.
    main = faces[0]
    face_centre_in_photo = (main.box[1] + main.box[3] / 2.0) * scale
    wanted = canvas_height * FACE_TARGET
    offset = int(round(wanted - face_centre_in_photo))

    return max(0, min(slack, offset))


def _backdrop(image: np.ndarray, width: int, height: int) -> np.ndarray:
    """
    A blurred wash filling the canvas, built from the photograph's own colours.

    Scaled to cover rather than fit, so there is no empty corner, then blurred
    hard and dimmed.
    """
    h, w = image.shape[:2]
    scale = max(width / w, height / h)
    covered = cv2.resize(image, (max(1, int(np.ceil(w * scale))),
                                 max(1, int(np.ceil(h * scale)))),
                         interpolation=cv2.INTER_LINEAR)

    ch, cw = covered.shape[:2]
    x0 = max(0, (cw - width) // 2)
    y0 = max(0, (ch - height) // 2)
    cropped = covered[y0:y0 + height, x0:x0 + width]

    # Blur on a small copy: a 150px kernel on a full-size frame is needlessly
    # slow and the result is identical once it is this soft.
    small = cv2.resize(cropped, (max(16, width // 8), max(16, height // 8)),
                       interpolation=cv2.INTER_AREA)
    radius = max(3, int(small.shape[1] * BLUR_FRACTION) | 1)
    small = cv2.GaussianBlur(small, (radius, radius), 0)
    washed = cv2.resize(small, (width, height), interpolation=cv2.INTER_LINEAR)

    return cv2.convertScaleAbs(washed, alpha=BACKDROP_DIM, beta=0)


# ============================================================================
# CONVERSION
# ============================================================================

def convert(
    source: str,
    output: str,
    ratio: Tuple[int, int] = DEFAULT_RATIO,
    height: int = DEFAULT_HEIGHT,
    faces: Optional[Sequence[measurement.FaceInfo]] = None,
    quality: int = 95,
) -> Conversion:
    """
    Write `source` as a vertical frame at `ratio`. Never raises.

    The photograph is placed whole and unscaled-in-proportion at full canvas
    width, so the subject is bit-for-bit the same content it started as, only
    resampled. The surrounding space becomes a blurred wash of its own colours.

    A source already taller than the target is fitted by height instead, which
    leaves a wash at the sides rather than squeezing the frame.
    """
    result = Conversion(source=source, output=output)

    try:
        image = cv2.imread(source, cv2.IMREAD_COLOR)
        if image is None or image.size == 0:
            result.error = "unreadable or not an image"
            return result

        sh, sw = image.shape[:2]
        result.source_size = (sw, sh)

        cw, ch = _canvas_size(ratio, height)
        result.output_size = (cw, ch)

        # Fit the whole photograph inside the canvas: scale by whichever edge
        # runs out first. This is the step that guarantees nothing is cropped.
        scale = min(cw / sw, ch / sh)
        pw = max(1, int(round(sw * scale)))
        ph = max(1, int(round(sh * scale)))

        interpolation = cv2.INTER_AREA if scale < 1.0 else cv2.INTER_LANCZOS4
        placed = cv2.resize(image, (pw, ph), interpolation=interpolation)

        canvas = _backdrop(image, cw, ch)

        if faces is None:
            faces = []
        result.faces_found = len(faces)

        y = _vertical_offset(ch, ph, faces, scale)
        x = max(0, (cw - pw) // 2)

        canvas[y:y + ph, x:x + pw] = placed
        result.subject_box = (x, y, pw, ph)

        if EDGE_LINE > 0:
            cv2.rectangle(canvas, (x, y), (x + pw - 1, y + ph - 1),
                          (28, 26, 23), EDGE_LINE)

        os.makedirs(os.path.dirname(os.path.abspath(output)), exist_ok=True)
        if not cv2.imwrite(output, canvas,
                           [int(cv2.IMWRITE_JPEG_QUALITY), quality]):
            result.error = "could not write the output file"
            return result

        result.cropped = False
        result.pixels_kept = 1.0
        result.ok = True

    except Exception as exc:  # noqa: BLE001 - reported, never hidden
        result.error = f"{type(exc).__name__}: {exc}"

    return result


def convert_cropped(
    source: str,
    output: str,
    ratio: Tuple[int, int] = DEFAULT_RATIO,
    height: int = DEFAULT_HEIGHT,
    faces: Optional[Sequence[measurement.FaceInfo]] = None,
    quality: int = 95,
) -> Conversion:
    """
    The other option: fill the frame edge to edge by cropping instead.

    Offered because a blurred wash is not always wanted, and refused silently
    by nobody - the result reports how much was thrown away, which for 3:2 into
    9:16 is around 62% of the width.

    The crop window is centred on the faces, so the subject survives even
    though the surroundings do not.
    """
    result = Conversion(source=source, output=output)

    try:
        image = cv2.imread(source, cv2.IMREAD_COLOR)
        if image is None or image.size == 0:
            result.error = "unreadable or not an image"
            return result

        sh, sw = image.shape[:2]
        result.source_size = (sw, sh)

        cw, ch = _canvas_size(ratio, height)
        result.output_size = (cw, ch)
        target = cw / ch

        # Largest rectangle of the target shape that fits inside the source.
        if sw / sh > target:
            box_h = sh
            box_w = int(round(sh * target))
        else:
            box_w = sw
            box_h = int(round(sw / target))

        if faces:
            main = faces[0]
            # Face boxes were measured on a copy capped at FACE_DETECT_MAX_DIM;
            # rebuild that scale so the coordinates land in the right place.
            longest = max(sw, sh)
            detect_scale = (longest / config.FACE_DETECT_MAX_DIM
                            if longest > config.FACE_DETECT_MAX_DIM else 1.0)
            fx = (main.box[0] + main.box[2] / 2.0) * detect_scale
            fy = (main.box[1] + main.box[3] / 2.0) * detect_scale
            x0 = int(round(fx - box_w / 2.0))
            # Leave headroom: a face centred in the crop looks low.
            y0 = int(round(fy - box_h * FACE_TARGET))
        else:
            x0 = (sw - box_w) // 2
            y0 = (sh - box_h) // 2

        x0 = max(0, min(sw - box_w, x0))
        y0 = max(0, min(sh - box_h, y0))

        window = image[y0:y0 + box_h, x0:x0 + box_w]
        scale = ch / box_h
        interpolation = cv2.INTER_AREA if scale < 1.0 else cv2.INTER_LANCZOS4
        canvas = cv2.resize(window, (cw, ch), interpolation=interpolation)

        os.makedirs(os.path.dirname(os.path.abspath(output)), exist_ok=True)
        if not cv2.imwrite(output, canvas,
                           [int(cv2.IMWRITE_JPEG_QUALITY), quality]):
            result.error = "could not write the output file"
            return result

        result.faces_found = len(faces or [])
        result.subject_box = (x0, y0, box_w, box_h)
        result.cropped = True
        result.pixels_kept = (box_w * box_h) / float(sw * sh)
        result.ok = True

    except Exception as exc:  # noqa: BLE001
        result.error = f"{type(exc).__name__}: {exc}"

    return result


# ============================================================================
# BATCH
# ============================================================================

def convert_all(
    paths: Sequence[str],
    directory: str,
    ratio: Tuple[int, int] = DEFAULT_RATIO,
    height: int = DEFAULT_HEIGHT,
    crop: bool = False,
    workers: int = 1,
    suffix: str = "_vertical",
) -> List[Conversion]:
    """
    Convert a batch, reusing one face probe across all of it.

    Faces are detected once per photo here rather than inside `convert`, so a
    caller that already has readings does not pay for them twice.
    """
    readings = {}
    if measurement.faces_available():
        for reading in measurement.read_faces_all(paths, workers=workers):
            readings[reading.filepath] = reading

    worker = convert_cropped if crop else convert
    out: List[Conversion] = []

    for path in paths:
        stem, _ = os.path.splitext(os.path.basename(path))
        destination = os.path.join(directory, f"{stem}{suffix}.jpg")
        reading = readings.get(path)
        out.append(worker(
            path, destination, ratio=ratio, height=height,
            faces=(reading.faces if reading and reading.ok else []),
        ))

    return out
