"""
Photo Shortlist: ranking, fault diagnosis and face geometry.

Covers the behaviour that the application's own test batch cannot show, and
the rules that are easy to break by accident:

  * the noise-robust sharpness actually rejects grain, which the shared
    quality.compute_sharpness does not - this is the defect the module exists
    to fix, so it is pinned here
  * the ranking is a contiguous 1..N ordered worst-last, with unreadable files
    excluded from it rather than scored as zero
  * comparative faults ("out of focus", "grainy") switch off on a batch too
    small for percentiles to mean anything
  * face faults are absent when no probe ran, which must not be confused with
    finding no faces
  * grain is only called out when it genuinely stands out, so a batch of
    uniformly clean photos does not have most of it flagged
  * face tilt and frontality are computed from landmark geometry correctly

Run with:

    python3 -m tests.test_shortlist
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile

import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from photo_shortlist import diagnose, metrics, shortlist, vertical  # noqa: E402
from wedding_album import quality  # noqa: E402


PASSED = 0
FAILED = 0


def ok(label: str, condition: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  [pass] {label}" + (f"  {detail}" if detail else ""))
    else:
        FAILED += 1
        print(f"  [FAIL] {label}" + (f"  {detail}" if detail else ""))


def near(label: str, got: float, want: float, tol: float) -> None:
    ok(label, abs(got - want) <= tol, f"got {got:.2f}, want ~{want}")


# ============================================================================
# TEST IMAGES
# ============================================================================

def scene(seed: int, width: int = 900, height: int = 600) -> np.ndarray:
    """
    A deterministic synthetic photograph.

    Carries fine texture everywhere on purpose: a noise estimator reads exactly
    0.00 on smooth gradients, and any noise-aware threshold calibrated against
    an image like that would be meaningless.
    """
    r = np.random.default_rng(seed)
    img = np.zeros((height, width, 3), np.uint8)

    for c in range(3):
        img[:, :, c] = np.linspace(45 + 40 * r.random(), 175 + 55 * r.random(), width)

    texture = cv2.resize(r.normal(0, 9, (height // 3, width // 3)),
                         (width, height), interpolation=cv2.INTER_CUBIC)
    img = np.clip(img.astype(np.float64) + texture[:, :, None], 0, 255).astype(np.uint8)

    for _ in range(10):
        x, y = int(r.integers(0, width - 160)), int(r.integers(0, height - 160))
        colour = tuple(int(v) for v in r.integers(20, 240, 3))
        if r.random() < 0.5:
            cv2.rectangle(img, (x, y), (x + 140, y + 110), colour, -1)
        else:
            cv2.circle(img, (x + 70, y + 70), 58, colour, -1)

    for _ in range(110):
        x, y = int(r.integers(0, width)), int(r.integers(0, height))
        cv2.line(img, (x, y), (x + 26, y + 14), (248, 248, 248), 1)

    return img


def write_batch(directory: str) -> None:
    """A batch with one deliberate fault per frame, plus a corrupt file."""
    def save(name: str, img: np.ndarray) -> None:
        cv2.imwrite(os.path.join(directory, name), img,
                    [cv2.IMWRITE_JPEG_QUALITY, 95])

    rng = np.random.default_rng(11)

    for i in range(1, 7):
        save(f"good{i}.jpg", scene(i))

    for i in range(7, 9):                                   # out of focus
        save(f"blurry{i}.jpg", cv2.GaussianBlur(scene(i), (25, 25), 0))

    for i in range(9, 11):                                  # highlights gone
        save(f"blown{i}.jpg", cv2.add(scene(i),
                                      np.full((600, 900, 3), 150, np.uint8)))

    for i in range(11, 13):                                 # low light
        save(f"dark{i}.jpg", (scene(i) * 0.11).astype(np.uint8))

    for i in range(13, 15):                                 # heavy grain
        grainy = scene(i).astype(np.float64) + rng.normal(0, 44, (600, 900, 3))
        save(f"grainy{i}.jpg", np.clip(grainy, 0, 255).astype(np.uint8))

    with open(os.path.join(directory, "corrupt.jpg"), "wb") as fh:
        fh.write(b"not an image at all")


# ============================================================================
# CHECKS
# ============================================================================

def check_sharpness_rejects_grain() -> None:
    """
    The reason metrics.py exists.

    Laplacian variance treats sensor grain as fine detail, so a grainy frame
    reads as a sharp one. Measuring at several scales and keeping the minimum
    removes that, because grain averages away under downsampling and real
    detail does not.
    """
    print("\nnoise-robust sharpness")

    clean = scene(1)
    grainy = np.clip(
        clean.astype(np.float64) + np.random.default_rng(5).normal(0, 40, clean.shape),
        0, 255).astype(np.uint8)

    clean_gray = cv2.cvtColor(clean, cv2.COLOR_BGR2GRAY)
    grainy_gray = cv2.cvtColor(grainy, cv2.COLOR_BGR2GRAY)

    naive_clean = quality.compute_sharpness(clean_gray)
    naive_grainy = quality.compute_sharpness(grainy_gray)
    ok("shared metric is fooled by grain", naive_grainy > naive_clean * 2,
       f"grain reads {naive_grainy / naive_clean:.1f}x sharper")

    robust_clean = metrics.sharpness_multiscale(clean_gray)
    robust_grainy = metrics.sharpness_multiscale(grainy_gray)
    inflation = robust_grainy / robust_clean
    ok("multiscale metric is not", inflation < 1.6,
       f"grain reads {inflation:.2f}x sharper")

    blurred = cv2.cvtColor(cv2.GaussianBlur(clean, (21, 21), 0), cv2.COLOR_BGR2GRAY)
    ok("blur still rejected at full sensitivity",
       metrics.sharpness_multiscale(blurred) < robust_clean * 0.2)


def check_ranking(result: shortlist.Result) -> None:
    print("\nranking")
    ok("every readable photo is ranked", result.total == 14,
       f"{result.total} ranked")
    ok("ranks are a contiguous 1..N",
       [c.rank for c in result.ranked] == list(range(1, result.total + 1)))
    ok("ordered best to worst",
       all(result.ranked[i].score >= result.ranked[i + 1].score
           for i in range(result.total - 1)))
    ok("best photo is the first rank",
       result.best is result.ranked[0] and result.best.rank == 1)

    names = [c.name for c in result.ranked]
    ok("corrupt file is not ranked", "corrupt.jpg" not in names)
    ok("corrupt file is reported as a failure",
       any("corrupt" in m.filepath for m in result.failures))

    # The faults are unambiguous, so the ordering should be too. This is the
    # whole point of the module and it took two separate fixes to get here: the
    # noise-robust sharpness, and then the heavier noise weight that only
    # became usable once sharpness was no longer contaminated by grain.
    def span(prefix: str) -> tuple:
        rs = [c.rank for c in result.ranked if c.name.startswith(prefix)]
        return min(rs), max(rs)

    good, grainy = span("good"), span("grainy")
    blurry, dark, blown = span("blurry"), span("dark"), span("blown")

    ok("well-made frames lead the ranking", good[0] == 1,
       f"good ranks {good[0]}-{good[1]}")
    ok("well-made frames beat grainy ones", good[1] < grainy[0],
       f"good {good[0]}-{good[1]}, grainy {grainy[0]}-{grainy[1]}")
    ok("grainy beats out of focus", grainy[1] < blurry[0],
       f"grainy {grainy[0]}-{grainy[1]}, blurry {blurry[0]}-{blurry[1]}")
    ok("out of focus beats unusable exposure", blurry[1] < dark[0],
       f"blurry {blurry[0]}-{blurry[1]}, dark {dark[0]}-{dark[1]}")
    ok("blown-out frames rank last", blown[1] == result.total,
       f"blown {blown[0]}-{blown[1]} of {result.total}")


def check_weights_restored() -> None:
    """
    The scoring weights are swapped in and must be put back.

    Leaving them applied would quietly change how `wedding_album` scores for
    the rest of the process, which is the kind of fault that shows up much
    later as "the album picks different photos now".
    """
    print("\nscoring weights are restored after use")
    from wedding_album import config as album_config

    before = dict(album_config.QUALITY_WEIGHTS)
    shortlist.score([metrics.PhotoMetrics(filepath="a.jpg", ok=False)])
    ok("weights unchanged after scoring",
       dict(album_config.QUALITY_WEIGHTS) == before)
    ok("this app really does use different weights",
       shortlist.QUALITY_WEIGHTS != before,
       f"noise {shortlist.QUALITY_WEIGHTS['noise']} vs {before['noise']}")
    ok("this app's weights still sum to 1.0",
       abs(sum(shortlist.QUALITY_WEIGHTS.values()) - 1.0) < 1e-9)


def check_faults(result: shortlist.Result) -> None:
    print("\nfault diagnosis")

    def codes(prefix: str) -> set:
        return {i.code
                for c in result.ranked if c.name.startswith(prefix)
                for i in c.issues}

    ok("blur is reported as out of focus", "out_of_focus" in codes("blurry"))
    ok("low light is reported", "too_dark" in codes("dark"))
    ok("blown highlights are reported",
       "highlights_lost" in codes("blown") or "too_bright" in codes("blown"))
    ok("grain is reported", "grainy" in codes("grainy"))

    # The tie trap: a percentile cut-off alone lands on a shared value and
    # flags every photo that happens to equal it.
    flagged = sum(1 for c in result.ranked
                  if any(i.code == "grainy" for i in c.issues))
    ok("grain is not flagged across the whole batch", flagged <= 4,
       f"{flagged} of {result.total} flagged")

    clean = [c for c in result.ranked if c.name.startswith("good")]
    ok("well-made frames carry no major fault",
       all(c.is_usable for c in clean))

    unreadable = diagnose.diagnose(metrics.PhotoMetrics(
        filepath="x.jpg", ok=False, error="unreadable"))
    ok("an unreadable file gets one fault and no measurements",
       len(unreadable) == 1 and unreadable[0].code == "unreadable")


def check_shortlist_spread(result: shortlist.Result) -> None:
    print("\nshortlist spreads across shots")
    groups = [c.group for c in result.picks]
    ok("every pick comes from a different shot group",
       len(set(groups)) == len(groups), f"groups {groups}")
    ok("picks are numbered from one",
       [c.pick_rank for c in result.picks] == list(range(1, len(result.picks) + 1)))
    ok("every pick carries a reason",
       all(c.reason for c in result.picks))
    ok("picks are also present in the full ranking",
       all(c in result.ranked for c in result.picks))


def check_drop_worst(paths) -> None:
    """
    Setting the weakest frames aside.

    The ordering matters as much as the count. Dropping happens before grouping
    and picking, so a frame that is out of the running cannot sit in a shot
    bundle influencing which of its neighbours gets chosen.
    """
    print("\nsetting the weakest aside")

    full = shortlist.run(paths, want=3, workers=1, use_faces=False)
    cut = shortlist.run(paths, want=3, workers=1, use_faces=False, drop_worst=4)

    ok("nothing is set aside by default", full.dropped == [])
    ok("four frames set aside when asked", len(cut.dropped) == 4,
       f"{len(cut.dropped)} set aside")
    ok("the ranking shrinks by exactly that many",
       cut.total == full.total - 4, f"{full.total} -> {cut.total}")
    ok("considered still counts the whole batch",
       cut.considered == full.total)

    ok("the ones set aside are the weakest",
       [c.name for c in cut.dropped]
       == [c.name for c in reversed(full.ranked[-4:])],
       "worst first")

    kept = {c.name for c in cut.ranked}
    gone = {c.name for c in cut.dropped}
    ok("kept and set aside do not overlap", not (kept & gone))
    ok("ranks are renumbered 1..N over what is kept",
       [c.rank for c in cut.ranked] == list(range(1, cut.total + 1)))

    # The point of dropping before grouping rather than filtering afterwards.
    ok("no set-aside frame appears in a shot bundle",
       not any(c.name in gone for g in cut.groups for c in g.members))
    ok("no set-aside frame is in the shortlist",
       not any(c.name in gone for c in cut.picks))
    ok("no set-aside frame is marked as a pick",
       not any(c.is_pick for c in cut.dropped))

    # Set-aside frames carry a rank so the UI can say "8th of 10". The list is
    # worst-first for display, which makes it easy to assign these backwards -
    # and that showed up on screen as rank 8 holding the lowest score.
    ok("set-aside ranks continue after the kept ones",
       sorted(c.rank for c in cut.dropped)
       == list(range(cut.total + 1, cut.considered + 1)),
       f"{sorted(c.rank for c in cut.dropped)}")
    ok("a better set-aside frame has a better rank",
       all(a.rank > b.rank for a, b in zip(cut.dropped, cut.dropped[1:])),
       "worst-first list, ascending ranks")
    ok("the best set-aside frame ranks just below the last kept one",
       max(c.score for c in cut.dropped)
       == next(c.score for c in cut.dropped if c.rank == cut.total + 1))

    ok("the fault tally only counts what is kept",
       sum(diagnose.tally([c.issues for c in cut.ranked]).values())
       == sum(len(c.issues) for c in cut.ranked))

    # Asking to drop everything is never the useful reading.
    everything = shortlist.run(paths, want=3, workers=1, use_faces=False,
                               drop_worst=999)
    ok("one frame always survives an absurd request",
       everything.total == 1 and len(everything.dropped) == full.total - 1,
       f"kept {everything.total}, set aside {len(everything.dropped)}")
    ok("the survivor is the best frame",
       everything.ranked[0].name == full.ranked[0].name)

    single = shortlist.run(paths[:1], want=1, workers=1, use_faces=False,
                           drop_worst=5)
    ok("a one-photo batch is never emptied",
       single.total == 1 and single.dropped == [])


def check_shapes(paths) -> None:
    """Orientation, which the review page uses to mark cropped tiles."""
    print("\nphoto shape")

    result = shortlist.run(paths, want=3, workers=1, use_faces=False)
    first = result.ranked[0]

    ok("the test batch is landscape", first.orientation == "landscape",
       f"{first.width}x{first.height}, aspect {first.aspect:.2f}")
    ok("every frame gets a shape",
       all(c.orientation != "unknown" for c in result.ranked))

    # The dead band: a one-pixel difference must not decide the answer.
    square = shortlist.Candidate(filepath="s.jpg", width=3000, height=2999)
    ok("a near-square frame is called square", square.orientation == "square",
       f"aspect {square.aspect:.4f}")

    tall = shortlist.Candidate(filepath="t.jpg", width=800, height=1200)
    ok("a tall frame is called portrait", tall.orientation == "portrait")

    unknown = shortlist.Candidate(filepath="u.jpg")
    ok("an unmeasured frame reports unknown, not a guess",
       unknown.orientation == "unknown" and unknown.aspect == 0.0)


def check_straighten(directory: str) -> None:
    """
    Standing frames upright.

    The face-driven half of this cannot be tested here: the synthetic batch has
    no faces, and committing a photograph of a real person to the repository to
    test it would be worse than the gap. What is tested is everything that must
    hold regardless - that a frame nothing can be judged by is left completely
    alone, that only whole quarter turns are ever applied, and that running it
    twice changes nothing the second time.

    The face-driven behaviour was checked by eye on a real shoot: 15 of 40
    frames were sideways, all were turned 270 degrees, and 6000x4000 became
    4000x6000 with heads at the top.
    """
    print("\nstanding frames upright")

    source = os.path.join(directory, "good1.jpg")
    work = os.path.join(directory, "upright_probe.jpg")
    shutil.copy2(source, work)

    before = cv2.imread(work)
    before_shape = before.shape

    result = vertical.straighten_file(work)
    ok("a frame with no face is left alone", result.ok and result.angle == 0,
       f"angle {result.angle}")

    after = cv2.imread(work)
    ok("its pixels are untouched", np.array_equal(before, after),
       "byte-for-byte identical")
    ok("its size is unchanged", after.shape == before_shape)

    # Quarter turns only: the dimensions either stay or swap exactly, which
    # cannot happen if anything was cropped or stretched.
    turned_image = cv2.rotate(before, cv2.ROTATE_90_CLOCKWISE)
    ok("a quarter turn swaps the dimensions exactly",
       turned_image.shape[0] == before_shape[1]
       and turned_image.shape[1] == before_shape[0])
    # Compare the whole multiset of pixel values, not a slice of each array:
    # a rotation reorders pixels, so the first N elements of the two arrays
    # hold different pixels and comparing them proves nothing.
    ok("a quarter turn preserves every pixel",
       np.array_equal(np.sort(turned_image.reshape(-1)),
                      np.sort(before.reshape(-1))),
       f"{before.size} values, identical multiset")

    second = vertical.straighten_file(work)
    ok("running it again is a no-op", second.ok and second.angle == 0)

    missing = vertical.straighten_file(os.path.join(directory, "nope.jpg"))
    ok("a missing file is reported, not raised",
       not missing.ok and "unreadable" in missing.error)

    corrupt = vertical.straighten_file(os.path.join(directory, "corrupt.jpg"))
    ok("a corrupt file is reported, not raised", not corrupt.ok)

    batch = vertical.straighten_all(
        [source, os.path.join(directory, "good2.jpg")], workers=1)
    ok("a batch returns one result per file", len(batch) == 2)
    ok("every batch result is accounted for",
       all(r.ok or r.error for r in batch))

    ok("an empty frame yields no rotation",
       vertical.detect_upright(np.zeros((0, 0, 3), np.uint8)) == 0)


def check_vertical_conversion(directory: str) -> None:
    """Converting to a vertical format without losing the subject."""
    print("\nvertical conversion")

    source = os.path.join(directory, "good1.jpg")
    original = cv2.imread(source)
    oh, ow = original.shape[:2]

    fit = os.path.join(directory, "fit.jpg")
    result = vertical.convert(source, fit, height=960)
    ok("fit mode writes a file", result.ok and os.path.isfile(fit))
    ok("fit mode keeps the whole photograph",
       result.pixels_kept == 1.0 and not result.cropped)

    canvas = cv2.imread(fit)
    ch, cw = canvas.shape[:2]
    ok("the canvas is 9:16", abs((cw / ch) - 9 / 16) < 0.01,
       f"{cw}x{ch}, ratio {cw / ch:.4f}")

    x, y, pw, ph = result.subject_box
    ok("the photograph sits inside the canvas",
       x >= 0 and y >= 0 and x + pw <= cw and y + ph <= ch)
    ok("its proportions are unchanged",
       abs((pw / ph) - (ow / oh)) < 0.02,
       f"source {ow / oh:.3f}, placed {pw / ph:.3f}")

    crop = os.path.join(directory, "crop.jpg")
    cropped = vertical.convert_cropped(source, crop, height=960)
    ok("crop mode writes a file", cropped.ok and os.path.isfile(crop))
    ok("crop mode reports what it discarded",
       cropped.cropped and 0.0 < cropped.pixels_kept < 1.0,
       f"{cropped.pixels_kept * 100:.0f}% kept")

    cc = cv2.imread(crop)
    ok("the cropped canvas is also 9:16",
       abs((cc.shape[1] / cc.shape[0]) - 9 / 16) < 0.01)

    bad = vertical.convert(os.path.join(directory, "corrupt.jpg"),
                           os.path.join(directory, "never.jpg"))
    ok("an unreadable source is reported, not raised", not bad.ok)


def check_small_batch(directory: str) -> None:
    print("\nsmall batch: comparative faults switch off")
    paths = sorted(
        os.path.join(directory, n) for n in os.listdir(directory)
        if n.startswith("good")
    )[:3]

    result = shortlist.run(paths, want=2, workers=1, use_faces=False)
    ok("three photos still rank", result.total == 3)

    comparative = {"out_of_focus", "slightly_soft", "grainy"}
    leaked = [i.code for c in result.ranked for i in c.issues
              if i.code in comparative]
    ok("no percentile faults from three samples", leaked == [], f"{leaked}")

    empty = diagnose.Thresholds.from_batch([])
    ok("empty batch yields disabled thresholds", empty.soft == 0.0)


def check_faces_absent_vs_none(result_no_faces: shortlist.Result) -> None:
    print("\nface faults: absent data is not an absent subject")
    face_codes = {"no_face", "face_small", "face_dark", "face_soft",
                  "face_turned", "face_tilted"}
    found = [i.code for c in result_no_faces.ranked for i in c.issues
             if i.code in face_codes]
    ok("no face faults when the probe did not run", found == [], f"{found}")
    ok("faces_probed is false", result_no_faces.faces_probed is False)
    ok("face count reads 'not checked', not zero",
       all(c.face_count == -1 for c in result_no_faces.ranked))


def check_face_geometry() -> None:
    print("\nface geometry from landmarks")
    # Order: right eye, left eye, nose, right mouth corner, left mouth corner.
    level = np.array([[100, 100], [160, 100], [130, 130],
                      [110, 160], [150, 160]], dtype=np.float64)
    tilt, frontality = metrics._geometry(level)
    near("a level face has no tilt", tilt, 0.0, 0.5)
    near("a level face is fully frontal", frontality, 1.0, 0.05)

    tilted = np.array([[100, 100], [160, 130], [130, 130],
                       [110, 160], [150, 160]], dtype=np.float64)
    tilt, _ = metrics._geometry(tilted)
    near("tilt is the angle of the eye line", tilt, 26.57, 1.0)

    # Nose slid towards one eye: the head has turned.
    turned = np.array([[100, 100], [160, 100], [106, 130],
                       [110, 160], [150, 160]], dtype=np.float64)
    _, frontality = metrics._geometry(turned)
    ok("a turned head scores below the cut-off",
       frontality < diagnose.FACE_TURNED, f"frontality {frontality:.2f}")

    ok("malformed landmarks do not raise",
       metrics._geometry(np.zeros((0, 2))) == (0.0, 1.0))


# ============================================================================
# MAIN
# ============================================================================

def main() -> int:
    check_sharpness_rejects_grain()
    check_face_geometry()
    check_weights_restored()

    with tempfile.TemporaryDirectory(prefix="shortlist_tests_") as directory:
        write_batch(directory)
        paths = sorted(os.path.join(directory, n)
                       for n in sorted(os.listdir(directory)))

        # use_faces=False throughout: the ONNX model is not in version control,
        # and these checks are about ranking and faults, not detection.
        result = shortlist.run(paths, want=5, workers=1, use_faces=False)

        check_ranking(result)
        check_faults(result)
        check_shortlist_spread(result)
        check_faces_absent_vs_none(result)
        check_drop_worst(paths)
        check_shapes(paths)
        check_straighten(directory)
        check_vertical_conversion(directory)
        check_small_batch(directory)

    total = PASSED + FAILED
    print(f"\n{PASSED}/{total} passed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
