# Wedding Photo Tools

Applications that choose photographs, sharing one quality-measurement core.
All run entirely on this machine: no API key, no cloud service, no per-image
cost, and no photograph ever leaves the computer.

| Application | Job | Status |
|---|---|---|
| **`photo_funnel`** | **The main app.** Review a shoot in twelve steps, narrowing to one photograph | v0.04, 48 checks |
| `photo_shortlist` | Rank a batch, name each photo's faults; the engine the others build on | 81 checks |
| `photo_studio` | Earlier three-step gallery UI on the same engine | superseded by the funnel |
| `wedding_album` | Organise a whole wedding into an album tree | 55 checks |

Validated on a real 245-photo ceremony as well as synthetic test images.

---

## Features

Everything runs locally on standard-library Python + OpenCV (no web
framework, no cloud). Every tool is **non-destructive** — originals are only
ever read or copied — and every web UI binds to `127.0.0.1` only.

### `photo_funnel` — the main app

Review a shoot in twelve named steps, each removing photos for one stated
reason, with its thresholds exposed and the exact frames it took shown.

- **Twelve-step funnel:** Add → Straighten → Duplicates → Exposure → Focus →
  Subject → Expressions → Grain → Narrow → People → Per moment → The best.
- **One-click analysis:** `Choose folder…` opens a native folder dialog, and a
  single action finds, copies, straightens, measures and runs the whole first
  half of the funnel.
- **Auto-straighten:** detects sideways frames and rotates working copies
  upright in whole quarter-turns; manual 90/180/270 rotation is also available.
- **Duplicate handling:** clusters near-identical frames by perceptual hash and
  keeps one per cluster (sharpest face or best score); you can swap which frame
  survives, or restore the automatic choice.
- **Exposure, focus, subject & grain checks:** absolute thresholds for exposure
  (blown/dark/shadow/hot), batch-relative percentiles for focus and grain, and
  face-aware subject checks (face in focus/shadow, head turned, nobody in
  frame).
- **Smart narrowing:** keep the top-N by score, band photos by face count
  (solo/duo/small/large), cluster into "moments" and fill per-band quotas
  round-robin so picks are genuinely different photographs.
- **Live thresholds:** photos are measured once and cached by path, mtime and
  size; every slider re-runs the whole funnel in ~5 ms.
- **Nothing is lost:** every step carries its rejected pile, and any frame can
  be claimed into a separate **Manually selected** set that never alters the
  funnel's own result.
- **Non-overwriting export:** suggests a destination and versioned folder name
  (`BestImages_vN`), refuses to overwrite, and writes numbered selects,
  `Manually_Selected/`, optional rejects grouped by step, optional 9:16
  versions, a notes file and a ranking CSV.

> **Note:** Step 7 (Expressions) is disabled — detecting closed eyes/smiles
> needs models that are not installed, so the step passes everything through.

### `photo_shortlist` — rank and diagnose a batch

- **Full ranking** of every photo 1→N by score, plus the **single best** photo.
- **Shortlist** built round-robin across look-alike bundles, so N picks are N
  *different* photographs rather than N frames of one pose.
- **Named faults** in a photographer's words (dark, washed out, soft, grainy,
  face in shadow, head turned, nobody in frame, …) and a batch-wide fault tally.
- **Noise-robust sharpness** measured across four scales so grain doesn't read
  as sharpness.
- **Browser and terminal:** drag-drop web UI, or `pick FOLDER` on the command
  line (`--want`, `--workers`, `--keep-duplicates`, `--no-faces`,
  `--picks-only`, `--drop-worst`).
- **9:16 conversion:** place the whole frame on a vertical canvas with a
  blurred colour wash (`convert`), or an edge-to-edge crop (`convert_cropped`).
- **Exports:** zip of picks with a notes file and the full-ranking CSV.

### `photo_studio` — three-step gallery (superseded by the funnel)

- Reuses the `photo_shortlist` engine and shares its in-memory batch.
- Static-file gallery site with a per-photo **detail / lightbox** view showing
  a photo and its shot-group siblings.
- In-place straighten, analyse (want / skip-duplicates / use-faces /
  drop-worst), and zip/CSV export.

### `wedding_album` — organise a whole wedding

- **Face detection & people grouping:** YuNet detection + SFace embeddings,
  clustered into distinct people (with the "two faces in one photo can't be the
  same person" constraint).
- **Labelling:** assign relationship roles (bride/groom/family/friend/ignore)
  interactively, by setting IDs, or by renaming exported face images.
- **Sections:** assign each photo to an album section by relationship priority,
  with an audit reason per photo.
- **Timeline aware:** reads EXIF capture time so look-alikes taken hours apart
  count as different moments and picks read in chronological order.
- **Album organization:** copy into a versioned `BestImages_vN/` tree with
  picks, runners-up and a written report; `batch` processes a whole collection
  at once (flat or per-shoot album trees).
- **CLI subcommands:** `scan`, `quality`, `download-models`, `faces`, `people`,
  `label`, `names`, `sections`, `organize`, `batch`, `shoots`, `serve`.

### `tools/` — diagnostics for tuning

- `ceremony_order.py` — reconstruct the day's running order from EXIF times.
- `compare_folders.py` — decide if two shoot folders hold the same people.
- `find_same_person.py` — surface near-miss clusters of the same person.
- `why_not_merged.py` — explain why two look-alike clusters were kept apart.
- `threshold_sweep.py` — show how the same-person threshold changes people
  counts.
- `regroup.py` — restructure a flat collection into event → phase → shoot.
- `normalize_names.py` — tidy collection folder names (rename only).
- `drive_api.py` / `drive_batch.py` — exercise the web API the way the browser
  does.

---

## Photo Funnel

```bash
source .venv/bin/activate
python3 -m photo_funnel            # opens http://127.0.0.1:8770/
```

Open a folder (or drop photos in), then walk the steps. Each one removes
photos for a single stated reason, exposes its thresholds with an **i**
popover explaining how to choose them, and shows exactly which frames it took.

```
 1 Add          2 Straighten    3 Duplicates    4 Exposure
 5 Focus        6 Subject       7 Expressions   8 Grain
 9 Narrow      10 People       11 Per moment   12 The best
```

On a real 245-photo ceremony: 87 frames rotated upright, then
245 -> 194 -> 166 -> 129 -> 107 -> 18 -> 17 -> 6 -> 1.

- **Your originals are never modified.** Photos are copied into a working
  directory first; rotation rewrites the copies only. Verified by comparing
  file modification times and sizes before and after a full run.
- **Measured once, decided instantly.** Photos are measured on entering step 3
  and cached by path, modification time and size. Every slider after that
  recomputes the whole funnel in about 5 ms.
- **Nothing is lost.** Every step carries the rejected pile. Any frame can be
  claimed into **Manually selected**, a separate set that never changes the
  funnel's own result.
- **Saving never overwrites.** Step 12 suggests a destination and folder name
  (`BestImages_vN`, `Muhurtham_Selects`, ...), refuses a folder that exists,
  and writes numbered selects, `Manually_Selected/`, optional rejects grouped
  by step, optional 9:16 versions, notes and a ranking CSV.
- **Deep links:** `http://127.0.0.1:8770/#5` opens step 5.

**Step 7, Expressions, is not available.** Reading expressions and closed eyes
needs two models that are not installed. The step passes everything through.

---

## Which one do I want?

**`photo_shortlist`** if you have a pile of photos and want to know which are
worth keeping. It ranks the whole batch 1 to N, shows the single best one,
names what is wrong with each, and shortlists the best few across different
shots. One batch in, reviewed in a browser. The smaller and simpler of the two.

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

Opens `http://127.0.0.1:8767/`. Drag photos on, choose how many to shortlist,
press the button. The page then shows:

- **the single best photo**, large, with its score and faults
- **the shortlist**, spread across different shots
- **every photo ranked 1 to N**, best to worst, filterable by whether it has a
  major fault or made the shortlist
- **a batch-wide fault summary** - how many are dark, soft, grainy and so on
- **the look-alike bundles**, so you can see which frames competed

Downloads are a zip of the shortlist, with a notes file explaining each choice,
and a CSV of the full ranking.

Measured: 29 photos in 0.7 seconds including face detection, on 14 cores.

### On the terminal

```bash
python3 -m photo_shortlist pick ~/Pictures/wedding --want 5
```

Prints the best photo, the shortlist, the full ranking and the fault summary.
Options: `--want N`, `--workers N`, `--keep-duplicates`, `--no-faces`,
`--picks-only`.

### Ranking and shortlist are different questions

The ranking is a straight sort by score, and it includes everything.

The shortlist is not simply the top N of it. A batch of wedding photos contains
bursts - six frames of the same pose, seconds apart - and the five highest
scores would be five frames of whichever pose was lit best. That is one
photograph five times, not a shortlist.

So look-alike frames are bundled by perceptual hash, and picks are taken
round-robin: the best of the strongest bundle, then the best of the next, and
only once every bundle has contributed does it come back for second-best. The
result is five genuinely different photographs.

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

## What it reports

Every photo gets a rank, a score and a list of named faults. The faults are:

| Fault | Severity | How it is decided |
|---|---|---|
| Low light | major | mean brightness below 62 of 255 |
| Washed out | major | mean brightness above 202 of 255 |
| Shadow detail lost | major | 18%+ of the frame is pure black |
| Highlights blown out | major | 10%+ of the frame is pure white |
| Out of focus | major | among the softest fifth of the batch |
| Face in shadow | major | face much dimmer than the frame: backlighting |
| Face not in focus | major | frame sharper than the face: focus landed behind |
| Slightly soft | minor | softer than most of the batch |
| Grainy | minor | noise above the batch's 78th percentile *and* 25% above its median |
| Flat, low contrast | minor | intensity spread below 26 |
| Dark shadows / Bright highlights | minor | milder clipping |
| Nobody in frame | minor | no face found; reads as a venue or detail shot |
| Head turned away / Head tilted | minor | from eye and nose landmark geometry |

Absolute thresholds are used where a fault is a fault regardless of company:
blown highlights are gone whatever else is in the batch. Comparative
thresholds are used for focus and grain, because Laplacian variance has no
universal "sharp" value. Below four photos the comparative faults switch off
rather than invent a verdict from three samples.

**"Nobody in frame" is minor on purpose.** A wedding set is full of
photographs with no people in them, and calling that major would push every
ring, cake and venue shot to the bottom.

### What it cannot tell you

**Closed eyes and expressions are not detected.** YuNet returns five
landmarks - two eye centres, a nose tip and two mouth corners - which carry no
eyelid or mouth-shape information. A technically perfect photo of a blink will
score well. Anything claiming to grade a smile from those five points would be
guessing, so the app does not try.

RAW files are not supported either; CR2 and NEF are rejected.

---

## A fixed defect worth knowing about

`photo_shortlist` used to rank grainy photos first. Sharpness was the variance
of the Laplacian, and sensor grain registers as fine detail, so a noisy frame
read as a sharp one - measured at about 12x inflation, with the grainiest
frames taking the top slots while being described as having "crisp detail".

It took two changes, and neither works alone:

1. **Sharpness is now the minimum Laplacian variance across four scales.**
   Grain averages away under downsampling and real detail survives, so a photo
   must look sharp at every scale. Inflation fell from 12x to 1.4x.

2. **Noise now carries 0.25 of the score here, against 0.12 in the shared
   config.** That weight was unusable before: while sharpness was contaminated,
   the setting at which grain stopped winning was the setting at which blurred
   frames started winning. Fixing the measurement first removed that squeeze.

Grainy frames went from ranks 1-3 to rank 15 of 28. The ordering now comes out
well-made, then grainy, then soft, then dark, then blown out. Pinned by
`tests/test_shortlist.py`.

**`wedding_album` still has the original weakness.** It shares
`quality.compute_sharpness`, and its own weights are untouched. Its face and
section logic dilute the effect, so it is latent rather than visible there.
Fixing it would change which photos existing albums pick, so that decision was
deliberately left open rather than taken quietly.

---

## Tests

```bash
python3 -m tests.test_shortlist      # 39 checks - ranking, faults, face geometry
python3 -m tests.test_selection      # 22 checks - shot grouping and picking
python3 -m tests.test_timeline       # 14 checks - capture-time windows
python3 -m tests.test_accounting     # 11 checks - every photo accounted for
python3 -m tests.test_overrides      #  8 checks - runtime config overrides
python3 -m tests.test_priority       #          - config validation
```

Run everything with `python3 -m tests.run_all`.

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
photo_shortlist/        the small app: batch in, ranked and diagnosed
    __main__.py         CLI entry - serve, or pick on the terminal
    metrics.py          noise-robust sharpness, face probing
    diagnose.py         turning measurements into named faults
    shortlist.py        ranking, look-alike bundling, round-robin picking
    app.py              web server, upload, thumbnails, zip and CSV export
    ui.html             the single page

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
legacy/image_curator.py superseded standalone script, kept for reference
```

`photo_shortlist` reuses four files from `wedding_album` and modifies none of
them. It is a separate package on purpose: the album tool does a different job
and has passing tests, and extending it would have put those at risk for no
gain.
