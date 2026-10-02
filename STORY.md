# Project Story

Why this code looks the way it does. Written for whoever picks it up next, so
the reasoning behind the awkward parts is not lost and the dead ends are not
walked again.

---

## Version history

| Version | What it was | Why it was replaced |
|---|---|---|
| **v0** `image_curator.py` | One standalone script. Technical quality only, Haar-cascade faces. | Faces broke silently on OpenCV 5. Still in the repo for reference, imported by nothing. |
| **v1** `wedding_album` | A proper package. ONNX face models, people clustering, relationship labelling, album trees. | Not replaced. Working, 55 checks passing. Too large for "give me the best 5". |
| **v2** `photo_shortlist` | Small app. Batch in, best few out, browser review. | Scope grew: a shortlist alone did not answer "why is this one 31st?". |
| **v3** `photo_shortlist` | Full 1-to-N ranking, named faults per photo, face-aware checks, hero best photo, CSV export. | Current. 39 checks passing. |

### What v2 to v3 changed

The brief moved from "give me the best 5" to "rank all 50 and tell me what is
wrong with each". Three things followed from that.

`metrics.py` gained face probing, because most of the faults worth naming are
about people: a face in shadow from backlighting, or focus that landed on the
background instead of the couple. A frame-wide exposure reading misses
backlighting entirely, since the average looks fine.

`diagnose.py` is new. It turns measurements into named faults, and it is where
the judgement calls live - which faults are major, which are comparative, and
what a threshold should be.

`shortlist.py` now returns the ranking *and* the shortlist from one pass. They
answer different questions and the code says so.

Each version kept the one piece worth keeping: photographic measurement. That
is why `photo_shortlist` imports `wedding_album.quality` rather than carrying
its own copy.

---

## v0 to v1: the silent failure

The original `image_curator.py` scored photos and also tried to detect faces
with Haar cascades. OpenCV 5 stopped shipping those cascades. The face step
raised an exception on every single image, a broad `except` swallowed it, and
every score quietly came back as zero.

The script reported success the whole time.

Two lasting consequences:

- Face work moved to the YuNet and SFace ONNX models in `faces.py`, and
  `quality.py` carries a comment stating it uses no cascades, so nobody
  reintroduces them.
- Failures are now reported on the result object rather than caught and
  discarded. `QualityMetrics` has `ok` and `error` fields, and an unreadable
  photo is listed as unreadable instead of scoring zero and silently losing.

The lesson worth carrying: a photo that cannot be read and a photo that is bad
are different outcomes, and collapsing them hides bugs for weeks.

---

## v1: what a real wedding forced

Several designs in `wedding_album` look over-engineered until you see the input
that caused them. The tests in `tests/` exist to pin these down.

**Percentages, not fixed counts.** The same setting has to handle a six-photo
ceremony and an 809-photo reception. Any fixed number is wrong for one of them,
so selection sizes by percentage with a floor of one photo per section, which
is why a small ceremony is never dropped entirely.

**Grouping needs time as well as appearance.** Appearance alone merged the
couple during portraits with the couple on stage two hours later: same two
people, same clothes, same pose, different part of the evening. Grouping now
requires frames to be close in time too.

**Round-robin picking.** Taking the three best of the bride's ten photos
returned three frames of one pose, because a well-lit setup wins every top
slot. Picks are now spread across groups.

**Alternates only from groups that produced a pick.** Without that rule the
alternates folder grew back into a copy of the whole collection. The useful
alternative to a chosen photo is the next-best frame of the same moment, not
some unrelated photo that lost outright.

---

## v1 to v2: why a new package

The brief was: 20-50 photos in, best 3-5 out, simple, same day.

`wedding_album` could nearly do it, but it is built around a wedding: folder
conventions per shoot, face clustering, people labelling, section assignment,
album trees. Reaching the simple behaviour through it meant either a pile of
flags or bending logic that currently passes 55 checks.

So `photo_shortlist` is a separate package that reuses four files
(`quality`, `scanner`, `config`, `pipeline`) and modifies none of them. The
album tool is untouched and its tests still pass.

The grouping logic is deliberately reimplemented rather than imported.
`wedding_album.selection` groups *within a section* and needs `Assignment`
objects carrying a section and a capture time. A flat batch of uploads has
neither, and manufacturing fake sections to satisfy the signature would have
been worse than thirty lines of simpler code.

---

## The grain defect

The honest centre of this document. Found by testing, and fixed - but only
after three wrong turns, and in the end it needed two changes rather than one.

### Symptom

On a 29-photo test batch, the three deliberately grainy images took the top
three places and were described as having "crisp detail".

```
1   83.9   noisy_scene17.jpg    "crisp detail and well exposed"
2   83.8   noisy_scene16.jpg    "crisp detail and well exposed"
3   83.8   noisy_scene15.jpg    "crisp detail and rich colour"
```

### Cause

Sharpness is the variance of the Laplacian, which responds to any rapid change
between neighbouring pixels. Grain is exactly that. Measured side by side at
the same scale:

| Photo | Laplacian variance |
|---|---|
| clean, sharp | 157 - 205 |
| grainy | 4694 |

Roughly 25x inflation, purely from noise. With sharpness weighted at 0.35 and
noise at 0.12, grain wins comfortably.

### Three fixes that did not work

Recorded so nobody spends the afternoon on them again.

**Re-weighting the metrics.** The obvious move, and it fails on a squeeze. At
`sharpness 0.25 / noise 0.25` the grainy photos still took the top three. At
`sharpness 0.20 / noise 0.32` they were beaten — by the blurred photos, which
rose to the top instead. One contaminated measurement cannot be asked to reject
two opposite faults.

**Subtracting the noise floor.** `lapvar - noise²` goes negative for clean
photos, because the noise estimator is itself inflated by real edges. For a
good frame, `noise²` was 712 against a Laplacian variance of 395.

**Dividing by the noise floor.** `lapvar / (1 + noise²)` separated grain from
detail correctly, then broke on blown-out frames: clipped white regions have no
local variation, the noise floor reads 0.00, and the division leaves an
enormous score. Blown-out photos went to the top.

Each fix traded one failure for another. That is the signature of attacking the
score instead of the measurement.

### The fix: two changes, neither sufficient alone

Measure sharpness at several scales and keep the **minimum**.

Grain is high-frequency and averages away under downsampling. Real detail
survives. So a photo has to look sharp at *every* scale, which grain cannot
fake and blur cannot pass. Measured against a clean reference frame:

| Variant | Full scale | Min across 4 scales |
|---|---|---|
| clean sharp | 402 | 402 |
| noise σ=8 | 552  (1.4x) | 437  (1.1x) |
| noise σ=16 | 989  (2.5x) | 447  (1.1x) |
| noise σ=30 | 2379 (5.9x) | 472  (1.2x) |
| noise σ=46 | 4707 (11.7x) | 515  (1.3x) |
| blur 5px | 39   (0.10x) | 39   (0.10x) |
| blur 29px | 1.6  (0.00x) | 1.6  (0.00x) |

Grain inflation collapses from 11.7x to 1.3x even at absurd noise levels, and
blur sensitivity is completely unaffected because for a blurred photo the full
scale is already the minimum.

This lives in `photo_shortlist/metrics.py` as `sharpness_multiscale`, which
substitutes the value before `quality.score_collection` sees it.

**It was not enough on its own.** With the measurement fixed, grainy frames
dropped from ranks 1-3 to ranks 1-2 of 14 - better, but still winning. The
remaining 1.4x edge was enough, because noise only carried 0.12 of the score.

**And then re-weighting worked.** The very fix dismissed above. Raising noise
to 0.25 with the corrected sharpness in place:

| weights | good | grainy | blurry | dark | blown |
|---|---|---|---|---|---|
| sharp .35 / noise .12 | 3-8 | **1-2** | 9-10 | 11-12 | 13-14 |
| sharp .30 / noise .25 | **1-6** | 7-8 | 9-10 | 11-12 | 13-14 |

The earlier conclusion was not wrong, it was conditional: re-weighting cannot
fix a contaminated measurement, because the contaminated metric is the one
being weighted. Fix the measurement and the squeeze disappears. The lesson is
that "this approach does not work" sometimes means "not yet".

These weights are applied in `shortlist.score` and restored in a `finally`, so
they never leak into `wedding_album`'s scoring. A test pins that.

On the full 28-photo batch, grainy frames now sit at rank 15.

### Left undone deliberately

`wedding_album` still has the original weakness. It shares
`quality.compute_sharpness` and its weights are untouched. Its face and section
logic dilute the effect, so it is latent rather than visible there. Fixing it
would change which photos existing albums pick, so that is a decision for
whoever owns that tool, not a change to slip in quietly.

---

## A note on the test images

Every number above came from images generated by a script, not real
photographs. That script is not in the repo; it built 29 frames with one
deliberate fault each — blurred, blown out, crushed, grainy — plus a
near-identical burst and a corrupt file.

The first version of that generator was too clean to be useful. Its backdrops
were smooth gradients, so the noise estimator read exactly `0.00`, and any
noise-aware tuning calibrated against it would have been meaningless. It was
rewritten to carry fine texture everywhere, because a real photograph has no
perfectly flat regions.

Even so, synthetic images prove the code runs and that the maths behaves. They
do not prove the picks look right to a photographer. **The app has never been
run on real wedding photos.** Until it has, "best" means "best by five
measurements", and whether that matches what a couple would choose is unknown.

---

## Decisions deliberately left open

| Question | Position |
|---|---|
| Face-aware faults | **Done.** Face presence, size, lighting, focus, tilt and frontality are all reported. Faces do not yet influence the *score*, only the fault list - so a sharp photo of someone's back still ranks well, it is just labelled. |
| Eyes-closed and expressions | **Not possible with the current model.** YuNet gives five landmarks with no eyelid or mouth-shape data. A blink will score well. Would need a different model. |
| Auto-correcting photos | Not done, and not attempted. The app names faults; it does not brighten, sharpen or straighten anything. Whether it should is an open product question. |
| RAW files | Not supported. CR2 and NEF are rejected. Needs a new dependency. |
| Fixing grain in `wedding_album` | Not done. Would change existing album output. |
| Python 3.9 | Past end of life since 31 October 2025. Works, but a move to 3.12+ should be planned. |
| Authentication | None. Both UIs are safe only because they bind to `127.0.0.1`. |
| Real photographs | **Still the biggest gap.** Nothing here has been tested on an actual wedding photo. |
