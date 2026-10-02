# Wedding Photo Tools

Two applications that choose photographs, sharing one quality-measurement core.
Both run entirely on this machine: no API key, no cloud service, no per-image
cost, and no photograph ever leaves the computer.

| Application | Job | Status |
|---|---|---|
| `photo_shortlist` | Drop in 20-50 photos, get back the best 3-5 | Working, one known defect |
| `wedding_album` | Organise a whole wedding into an album tree | Working, 55 tests passing |

---

## Which one do I want?

**`photo_shortlist`** if you have a pile of photos and want the best few out of
it. One batch in, a handful out, reviewed in a browser. This is the smaller and
simpler of the two.

**`wedding_album`** if you have an entire wedding to organise. It detects faces,
groups them into people, lets you label those people by relationship, splits
the day into sections, and writes an album folder tree. Much more capable, much
more to learn.

---

## Install

Requires Python 3.9 or newer and about 40 MB for the face models.

```bash
git clone <your-repo-url>
cd ionic-starter-kit

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Then download the face models

**`wedding_album` will not run without this step.** The ONNX weights are 38 MB
of binary and are deliberately excluded from version control, so a fresh clone
has to fetch them:

```bash
python3 -m wedding_album download-models
```

`photo_shortlist` does not need the models today, because it scores on
technical quality only and does not look for faces.

---

## Running `photo_shortlist`

### In a browser

```bash
python3 -m photo_shortlist
```

Opens `http://127.0.0.1:8767/`. Drag photos onto the page, choose how many you
want, press the button. Picks appear with a score and a sentence explaining each
choice, and the Download button bundles them into a zip with a notes file.

### On the terminal

```bash
python3 -m photo_shortlist pick ~/Pictures/wedding --want 5
```

Useful for a quick check or a script. Options: `--want N`, `--workers N`,
`--keep-duplicates`.

### How it chooses

Two stages, and the second one is the point.

A batch of wedding photos usually contains bursts: six frames of the same pose,
seconds apart. Picking the five highest scores outright returns five frames of
whichever pose was lit best, which is one photograph five times over.

So look-alike frames are bundled first, using a perceptual hash. Then picks are
taken round-robin: the best of the strongest bundle, then the best of the next,
and only once every bundle has contributed does it come back for second-best.
The result is five genuinely different photographs.

---

## Running `wedding_album`

```bash
python3 -m wedding_album serve          # web UI on 127.0.0.1:8766
python3 -m wedding_album shoots  SRC    # what folders would be processed
python3 -m wedding_album batch   SRC    # organise every shoot folder
python3 -m wedding_album quality SRC    # just the quality ranking
python3 -m wedding_album faces   SRC    # detect faces and report
python3 -m wedding_album people  SRC    # group faces into people
python3 -m wedding_album label   SRC    # assign relationship roles
python3 -m wedding_album organize SRC   # copy the album and write the report
```

`python3 -m wedding_album --help` lists every command and option.

---

## Known defect

**`photo_shortlist` ranks grainy photos too highly.** Sharpness is measured as
the variance of the Laplacian, and sensor grain registers as fine detail, so a
noisy frame reads as a sharp one. Measured at roughly 25x inflation on a test
batch: the three grainiest images took the top three places and were described
as having "crisp detail".

Re-weighting the metrics does not fix it. The weight at which grain stops
winning is the weight at which blurred photos start winning instead, because
one contaminated measurement cannot reject two opposite faults.

The fix is to take the *minimum* Laplacian variance across four image scales.
Grain vanishes when a photo is downsampled and real detail survives, so a photo
must look sharp at every scale. Measured: grain inflation falls to about 1.1x
while blur is still rejected at full sensitivity. Not yet implemented.

`wedding_album` shares the same measurement and therefore the same weakness,
though its face and section logic dilute the effect.

---

## Tests

```bash
python3 -m tests.test_selection      # 22 checks - shot grouping and picking
python3 -m tests.test_timeline       # 14 checks - capture-time windows
python3 -m tests.test_accounting     # 11 checks - every photo accounted for
python3 -m tests.test_overrides      #  8 checks - runtime config overrides
python3 -m tests.test_priority       #         - config validation
```

**Do not trust `unittest discover` on this project.** It reports
`Ran 0 tests - OK`, which looks like a pass but means nothing ran: these files
are self-checking scripts rather than `unittest` classes. Run them by name.

---

## Verified environment

Pinned in `requirements.txt` and confirmed working together:

| Component | Version | Note |
|---|---|---|
| macOS | 26.6.2 | 14 cores |
| Python | 3.9.6 | Past end of life, see below |
| opencv-python | 5.0.0.93 | `cv2.__version__` reports `5.0.0` |
| numpy | 2.0.2 | |
| Pillow | 11.3.0 | legacy script only |
| ImageHash | 4.3.2 | legacy script only |

Versions are pinned exactly rather than as ranges. These applications score
photographs relative to the batch, so a different OpenCV could change which
photo is called best without anything appearing to break.

**Python 3.9 reached end of life on 31 October 2025** and no longer receives
security updates. It works here, but a move to 3.12 or newer should be planned.
It is also why the code carries `from __future__ import annotations`.

---

## Security

Both web UIs bind to `127.0.0.1`, so only this machine can reach them. **That
loopback binding is the entire security boundary - there is no authentication.**

Do not change the host to `0.0.0.0` on a shared network. The image endpoints
would let anyone reachable read photographs from the directory being worked on.
Making either UI network-reachable requires adding authentication first.

Path traversal is guarded separately: image requests are resolved and must land
inside the directory being worked on, and uploaded filenames are reduced to a
basename, so a crafted request cannot walk out to arbitrary files.

`photo_shortlist` holds uploads in a temporary directory and deletes them when
the server stops. Nothing is kept between sessions.

---

## Layout

```
photo_shortlist/        the small app: batch in, best few out
    __main__.py         CLI entry - serve, or pick on the terminal
    shortlist.py        bundling look-alikes, round-robin picking
    app.py              web server, upload, thumbnails, zip download
    ui.html             the single page
    metrics.py          noise-robust sharpness  (NOT YET WRITTEN)

wedding_album/          the full album tool
    quality.py          photographic measurement       <- shared
    scanner.py          finding image files            <- shared
    config.py           tunable settings               <- shared
    pipeline.py         parallel stage runners         <- shared
    faces.py            YuNet detection, SFace embedding
    people.py           clustering faces into people
    sections.py         which album page a photo belongs to
    selection.py        per-section picking
    organizer.py        copying the album, writing reports
    server.py  ui.html  the album web UI
    ...

tests/                  self-checking scripts, run by name
tools/                  one-off diagnostics for tuning thresholds
models/                 ONNX weights - NOT in git, must be downloaded
image_curator.py        superseded standalone script, kept for reference
```

`photo_shortlist` reuses four files from `wedding_album` and modifies none of
them. It is a separate package on purpose: the album tool does a different job
and has passing tests, and extending it would have put those at risk for no
gain.
